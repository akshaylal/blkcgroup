# Copyright 2011 Google Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#  Utility functions for running the blkcgroup isolation test.
#
#  Experiments are parsed with the following grammar:
#     Experiment = Containers
#     Containers = Container { , Container }
#     Container  = Share [ Worker Repeat ]
#     Share      = Integer
#     Repeat     = [ * Integer ]
#     Worker     = rdseq [.Wmode] | rdrand Delay | wrseq [. Wmode] | sleep
#     Delay      = [ .delay Integer ]
#     Wmode     = buf | sync | dir
#
#  TODO:
#      Add support for io class
#      Add support for io limiting
#      Add support for nested containers
#      Do more testing on non fakenuma systems


import getopt, glob, logging, os, re, subprocess, sys, time
import cgroup, cpuset, error, utils

# Size of allocated containers for workers. We chose 360mb because it's small
# enough to allow lots of workers on systems with less memory, and it's
# significantly smaller than test files so we can have adequate memory pressure
# to force there to be disk traffic.
CONTAINER_MBYTES = 360

MAX_VALID_WEIGHT = 1000 # kernel limits the max value to be 1000 (min to 100)

TEST_CGROUP_PREFIX = 'blkcgroupt'

def usage(argv):
    """Prints usage information to stderr."""
    sys.stderr.write('%s [-ch]: Runs a blkcgroup isolation test\n'
                     '-c: Cleans test data before running\n'
                     '-h: Prints help information\n' % argv[0])


def delete_test_containers():
    """Deletes all test containers that could be created by this test."""
    cgroups = glob.glob('/dev/cgroup/%s*' % TEST_CGROUP_PREFIX)
    for cgroup in cgroups:
        os.rmdir(cgroup)


def setup_logging(debug=False):
    """Initializes the logger.

    Logs data to filename. Logs debug data if debug is True.
    """
    format_string = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(format=format_string,
                        stream=sys.stdout,
                        datefmt='%H:%M:%S')

    # Enable debug logs only if specified.
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)


def expect_delim(text, delim):
    """Require text to start with delim, and return text with delim stripped.
    """
    if text[0] != delim:
        raise ValueError, 'missing %s at %s' % (delim, text)
    return text[1:]


def parse_integer(text):
    """Split a string, parsing off the integer at the beginning.

    For example, '90% rdseq, 10% seq' becomes (90, '% rdseq, 10% seq')
    """
    value = re.match('\d*', text).group()
    return int(value), text[len(value):]


def parse_name(text):
    """Split an arbitrary (maybe empty) word from the beginning of a string."""
    lth = 0
    while text[lth] not in ' ,*%();':
        lth += 1
    return text[:lth], text[lth:]


def parse_containers(text):
    """Parse worker containers in an experiment."""
    containers = []
    while True:
        container = {}
        container['dtf'], text = parse_integer(text.lstrip())
        options, text = parse_name(text)
        # This is where we would hook in options for limiting and priority.

        worker, text = parse_name(text.lstrip())
        repeat = 0
        if worker:
            repeat = 1
            if text[0] == '*':
                repeat, text = parse_integer(text[1:])
            text = text.lstrip()
            container['worker'] = worker
        container['worker_repeat'] = repeat

        containers.append(container)
        if text[0] != ',':
            break
        text = text[1:]
    return containers, text


def parse_experiment(text):
    """Parse an experiment and require that all input is consumed."""
    exper, text = parse_containers(text + ';')
    text = expect_delim(text, ';')
    return exper


def plan_container_size(container):
    """Returns the target memory size of a given container.

    There will be more logic here once we have to deal with nested containers.
    """
    if 'worker' in container:
        return CONTAINER_MBYTES
    else:
      return 0


def setup_container(container, cname, device,
                    root_name, my_cpu_parent, my_io_parent):
    """Create a new os container for constraining and isolating the cpus, mem,
       and disk IO of one set of io workers, from other workers.
       An os container is a pairing of a cpuset cgroup with an io cgroup,
       generally from separate cgroup hierarchies.  (They can also be
       separate fields within a combined cgroup in a single hierarchy.)
       my_cpu_parent and my_io_parent describe the existing cpu and io
       cgroups of the new container's parent container.
    """
    # Create a new cpus+mem cgroup, below my_cpu_parent:
    mbytes = plan_container_size(container)
    blkio_shares = container['dtf']
    # TODO: extend or replace create_container with support for
    #       separately-mounted io cgroups
    if not my_io_parent.cpuset_hierarchy:
        raise ValueError, ('cpuset does not yet support '
                           'separate mount of io cgroups')
    path = cpuset.create_container_with_mbytes_and_specific_cpus(
                   device, cname, root=my_cpu_parent.name, mbytes=mbytes,
                   blkio_shares=blkio_shares, move_in=False, timeout=0)

    # Setup a view.
    cpu_cgroup = cgroup.cgroup('cpuset', path)

    if my_io_parent.cpuset_hierarchy:
        # Io subsystem is being controlled via main cpuset cgroup hierarchy
        # blkio_cgroup is a 2nd view of the same cpu/mem cgroup just created
        blkio_cgroup = cgroup.cgroup('blkio', path)
    else:
        # Io subsystem has its own cgroup hierarchy, separate from cpuset
        # create a new cgroup, below my_io_parent in Io's hierarchy
        blkio_cgroup = my_io_parent.new(cname)

    container['cpu_cgroup'] = cpu_cgroup
    container['blkio_cgroup'] = blkio_cgroup
    name = cpu_cgroup.name  # eg  default/g0/g1
    if root_name:  # remove default/
        name = name[len(root_name)+1:]
    container['name'] = name  # eg g0/g1


def setup_containers(tree, device,
                     root_name, my_cpu_parent, my_blkio_parent):
    """Recursive top-down tree walk, creating all containers & cgroups
       needed for one experiment.  my_*_parent describe the existing cpu
       cgroup and io cgroup of this subtree's parent container.
    """
    container_names = []
    for i, container in enumerate(tree):
        # Create next sibling container at this level
        setup_container(container, '%s%d' % (TEST_CGROUP_PREFIX, i), device,
                        root_name, my_cpu_parent, my_blkio_parent)
        container_names.append('%s%d' % (TEST_CGROUP_PREFIX, i))
    return container_names


def measure_containers(tree, device, timevals):
    """Measures the 'time' attribute for all containers for a given device.

    """
    for container in tree:
        for line in container['blkio_cgroup'].get_attr('time'):
            parts = line.split()
            if parts[0] == device:
                timevals[container['name']] = int(parts[-1])


def release_containers(exper):
    for container in exper:
        container['cpu_cgroup'].release()
        container['blkio_cgroup'].release()


def remove_file(file):
    if os.path.exists(file):
        os.remove(file)


def score_max_error(tree, timevals):
    """Find maximum DTF error across containers of tree, and achieved DTFs
    """
    total_time = 0
    for container in tree:
        total_time += timevals[container['name']]
    actual_weights_str = ''
    maxerr = 0

    for container in tree:
        # Calculate error.
        logging.debug('Calculate the max error for the experiment.')
        time = timevals[container['name']]
        actual_weight = time * MAX_VALID_WEIGHT / (total_time or 1)
        actual_weights_str += '%d' % actual_weight
        error = abs(actual_weight - int(container['dtf']))
        maxerr = max(maxerr, error)
        actual_weights_str += ', '

    actual_weights_str = actual_weights_str[:-2]  # Clip off last ', '
    return maxerr, actual_weights_str


def score_experiment(exper_num, exper, timevals, allowed_err):
    maxerr_weight, actual_weights  = score_max_error(exper, timevals)
    logging.info('experiment %d achieved DTFs: %s', exper_num, actual_weights)

    # Check if we passed or failed.
    passing = maxerr_weight <= allowed_err

    if passing:
        status = 'PASSED'
    else:
        status = 'FAILED'

    logging.info('experiment %d %s: max observed error is %d, '
                 'allowed is %d',
                 exper_num, status, maxerr_weight, allowed_err)

    return passing


def kill_slower_workers(fast_pid, cpu_cgroup, pids_file):
    moved_pids_file = pids_file + '.moved'
    try:
        os.rename(pids_file, moved_pids_file)
    except OSError:
        return
    logging.debug('fastest worker pid %d of container %s'
                  ' killing all slower workers',
                  fast_pid, cpu_cgroup.path)
    for line in open(moved_pids_file):
        pid = int(line.rstrip())
        if pid != fast_pid:
            utils.system('kill %d' % pid, ignore_status=True)


def run_worker(cmd, cpu_cgroup, blkio_cgroup, pids_file):
    # main of new process for running an independent worker shell
    logging.debug('Worker running command: %s' % cmd)
    cpu_cgroup.move_my_task_here()
    blkio_cgroup.move_my_task_here()
    p = subprocess.Popen(cmd.split(),
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    if pids_file:
        # TODO: append to pids_file via a direct python method
        utils.system('echo %d >> %s' % (p.pid, pids_file))
    logging.debug('running "%s" in container %s and io cgroup %s as pid %d',
                  cmd, cpu_cgroup.path, blkio_cgroup.path, p.pid)

    p.wait()
    if pids_file:
        kill_slower_workers(p.pid, cpu_cgroup, pids_file)
    logging.debug(p.stdout.read())


def actual_disk_device(ldevice):
    # get actual ide or sata device for some logical disk device
    tuner = '/usr/local/sbin/tunedisknames'
    if not os.path.exists(tuner):
        return ldevice
    disk_map = utils.system_output(tuner + ' printmap')
    for line in disk_map.splitlines():
        parts = line.split()
        if parts[1] == ldevice:
            return parts[0]
    raise ValueError("Could not find mapping for %s" % ldevice)


def device_holding_file(filename):
    mountpoint = os.path.abspath(filename)

    # Iterate over the path until we get the mount point.
    while not os.path.ismount(mountpoint):
        mountpoint = os.path.dirname(mountpoint)

    # Parse all the mount points and return the underlying device.
    for line in open('/proc/mounts').readlines():
        parts = line.split()
        if parts[0].startswith('/dev/') and parts[1] == mountpoint:
            # Partition = everythin beyond /dev/
            partition = parts[0][5:]
            device = partition.rstrip('0123456789')
            return device
    raise ValueError("Could not find device holding %s" % filename)


def enable_blkio_and_cfq(device):
    """Enable blkio and cfq, when not done by boot command."""
    # Ensure that the required device is valid block device.
    disk = os.path.join('/sys/block', device)
    if not os.path.exists(disk):
        raise error.Error('Machine does not have disk device ' + device)

    # Ensure the io cgroup is mounted.
    if not cgroup.mount_point('blkio'):
        raise error.Error('Kernel not compiled with blkio support')

    # Enable cfq scheduling on the block device.
    file = os.path.join(disk, 'queue/scheduler')
    if '[cfq]' in utils.read_one_line(file):
        logging.debug('cfq scheduler is already enabled on drive %s', device)
        return

    logging.info('Enabling cfq scheduler on drive %s', device)
    utils.write_one_line(file, 'cfq')

    if not os.path.exists('/dev/cgroup/blkio.weight'):
        raise error.Error('Kernel hasn\'t implemented blkio.weight')


def set_group_isolation(device, value):
    """Set the group_isolation setting for a device."""
    disk = os.path.join('/sys/block', device)
    if not os.path.exists(disk):
        raise error.Error('Machine does not have disk device ' + device)
    filename = os.path.join(disk, 'queue/iosched/group_isolation')
    logging.debug('Setting group_isolation for %s to %s' % (device, value))
    utils.write_one_line(filename, value)


def get_group_isolation(device):
    """Get the group_isolation setting for a device."""
    disk = os.path.join('/sys/block', device)
    if not os.path.exists(disk):
        raise error.Error('Machine does not have disk device ' + device)
    filename = os.path.join(disk, 'queue/iosched/group_isolation')
    return utils.read_one_line(filename)


class test_harness(object):
    def __init__(self, title):
        self.title = title


    def some_zeroed_input_file(self, prefix, mbytes):
        name = os.path.join(self.workdir,
                            '%s%d' % (prefix, self.input_file_count))
        self.input_file_count += 1
        # TODO: use actual disk file size, avoid rebuilding across iterations
        old_mbytes = self.existing_input_files.get(name, 0)
        if mbytes > old_mbytes:
            cmd = ('/bin/dd if=/dev/zero of=%s bs=1M seek=%d count=%d'
                   % (name, old_mbytes, mbytes-old_mbytes))
            utils.system(cmd)
            self.existing_input_files[name] = mbytes
        return name


    def output_file_name(self, n):
        return os.path.join(self.workdir, 'write%d' % n)


    def some_output_file(self):
        name = self.output_file_name(self.output_file_count)
        self.output_file_count += 1
        return name


    def remove_output_files(self):
        for n in xrange(self.output_file_count):
            remove_file(self.output_file_name(n))
        self.output_file_count = 0


    def setup_worker(self, worker, mbytes):
        # mbytes fixes the effective size of the worker's input or output file.
        #   For workers other than rdseq, this size gets scaled to give
        #   approximately the same run time as rdseq with unscaled mbytes.

        if len(worker.split('.', 1)) > 1:
          variant = worker.split('.', 1)[1]
        else:
          variant = ''

        # Sequential reads.
        if worker.startswith('rdseq'):
            file_name = self.some_zeroed_input_file('rddata', mbytes)
            extra_options = ''

            if variant == 'dir':
                extra_options += 'iflag=direct '

            cmd = ('/bin/dd if=%s of=/dev/null bs=1M count=%d %s' %
                   (file_name, mbytes, extra_options))

        # Random reads.
        elif worker.startswith('rdrand'):
            file_name = self.some_zeroed_input_file('rddata', mbytes)
            log_iosize = 16  # 64Kb/read, is about 8x slower than seq read
            # randomly read 12% of the records of the input file
            #   so that entire file does not get cached,
            #   and also total elapsed time is similar to rdseq
            count = ((mbytes << 20) >> log_iosize) // 8
            delayms = ''

            if variant.startswith('delay'):
                delayms = '-d %d ' % int(variant[5:])

            elif variant != '':
               raise ValueError, 'bad worker: ' + worker

            cmd = ('%s/rand_read -c %d %s %d %s' %
                   (self.srcdir, count, delayms, log_iosize, file_name))

        # Sequential write.
        elif worker.startswith('wrseq'):
            file_name = self.some_output_file()
            extra_options = ''

            if variant == 'sync':
                # Compensate for slower rate.
                mbytes //= 3
                extra_options += 'conv=fdatasync '
            elif variant == 'dir':
                extra_options += 'oflag=direct '
            else:
                # Buffered mode needs a bigger files which overflow fs cache.
                mbytes *= 2

            count = mbytes * 16  # 64K * 16 = 1M
            cmd = ('/bin/dd if=/dev/zero of=%s bs=64K count=%d %s' %
                   (file_name, count, extra_options))

        # Sleep op.
        elif worker == '' or worker == 'sleep':
            cmd = ''

        else:
            raise ValueError, 'unknown worker %s' % worker

        return cmd


    def setup_worker_files(self, seq_read_mb, tree, key_prefix):
        """Recursive top-down walk over an experiment's tree of containers,
           setting up the input data files needed by IO workers, and collecting
           the shell commands that will launch those workers.
           seq_read_mb determines the total sizes of all input/output files
               files for all workers within one container,
               and for all worker types, not just rdseq.
        """
        for c, container in enumerate(tree):
            cname = '%s%d' % (TEST_CGROUP_PREFIX, c)
            container_key = '%s%s' % (key_prefix, cname)
            cmds = []
            mult = container['worker_repeat']
            for w in xrange(mult):
                per_worker_mbytes = seq_read_mb // mult
                # Total I/O per container is unchanged by *1 vs *4 multiples
                cmd = self.setup_worker(container['worker'], per_worker_mbytes)
                cmds.append(cmd)
            container['worker_cmds'] = cmds


    def enum_worker_runners(self, tree, pids_file, timeout):
        """Recursive top-down walk over an experiment's tree of containers,
           gathering the tasks & arguments for launching all workers.
           pids_file is '' if slower workers should complete.
        """
        tasks = []
        for container in tree:
            for cmd in container['worker_cmds']:
                if cmd:
                    tasks.append([run_worker, cmd,
                                  container['cpu_cgroup'],
                                  container['blkio_cgroup'],  pids_file])
        if pids_file and timeout:
            # add pseudo worker to 1st container to timeout all workers,
            # shortens experiment when fastest worker was given low DTF share
            cmd = 'sleep %s' % timeout
            container = tree[0]
            tasks.append([run_worker, cmd, container['cpu_cgroup'],
                          container['blkio_cgroup'], pids_file])
        return tasks


    def run_worker_processes_in_parallel(self, runners):
        sys.stdout.flush()
        sys.stderr.flush()
        pids = []
        for task in runners:
            run_worker = task[0]
            args = task[1:]
            pid = os.fork()
            if not pid:  # we are child process
                try:
                    run_worker(*args)
                except Exception, e:
                    logging.error('child ended by exception %s', e)
                    sys.exit(1)
                sys.exit(0)
            # we are parent
            pids.append(pid)

        logging.debug('waiting for worker tasks')
        for pid in pids:
            pid, status = os.waitpid(pid, 0)


    def run_single_experiment(self, exper_num, experiment, seq_read_mb,
                              kill_slower, timeout, allowed_error,
                              pre_experiment_cb, post_experiment_cb):
        """Run a single experiment involving one round of concurrent execution
           of IO workers in competing containers.
        """
        logging.info('----- Running experiment %d: %s', exper_num, experiment)

        # Given the experiment parameters generate a exper map based off the
        # tests grammar.
        exper_key = 'x%d' % exper_num
        exper = parse_experiment(experiment)

        # Generate user space commands to be executed per worker.
        logging.info('Creating initial file set.')
        self.input_file_count = self.output_file_count = 0
        self.setup_worker_files(seq_read_mb, exper, exper_key + '-')
        if kill_slower:
            pids_file = os.path.join(self.workdir, 'pids_file')
            remove_file(pids_file)
            remove_file(pids_file + '.moved')
        else:
            pids_file = ''

        logging.info('Flush all read/write caches. This could take a minute.')
        utils.drop_caches()

        # Generate class cgroup_access objects or cpuset and blkio.
        parent_cpu_cgroup = cgroup.root_cgroup('cpuset')
        parent_blkio_cgroup  = cgroup.root_cgroup('blkio')

        logging.info('Create all required containers.')
        container_names = setup_containers(exper, self.device,
            parent_cpu_cgroup.name, parent_cpu_cgroup, parent_blkio_cgroup)
        if pre_experiment_cb:
            pre_experiment_cb(container_names)

        # Add all required workers  & parameters to the tasks list.
        runners = self.enum_worker_runners(exper, pids_file, timeout)

        logging.info('Run the actual experiment now, launching all worker '
                     'processes.')
        start_seconds = time.time()
        self.run_worker_processes_in_parallel(runners)

        logging.info('All workers have now completed or been killed by fastest '
                     'worker.')
        seconds_elapsed = time.time() - start_seconds
        logging.info('Experiment completed in %.1f seconds', seconds_elapsed)

        timevals = {}
        measure_containers(exper, utils.get_device_id(self.device), timevals)

        # Score the experiment.
        logging.debug('Scoring the experiment.')
        passing = score_experiment(exper_num, exper, timevals, allowed_error)
        if passing:
            self.passed_experiments += 1
        self.tried_experiments += 1

        if post_experiment_cb:
            post_experiment_cb(container_names)

        self.remove_output_files()
        release_containers(exper)


    def run_experiments(self, experiments, seq_read_mb, workvol,
                        kill_slower=False, timeout='',
                        pre_experiment_cb=None,
                        post_experiment_cb=None,
                        google_hacks=False):
        """Execute a previously-generated list of experiments.

        experiments: a list of (string, number) tuples to run as tests.
        seq_read_mb: controls the natural full duration of one experiment.
            This determines the combined effective sizes of all
            input/output data files for all workers within one container.
            For workers other than rdseq, this gets automatically adjusted
            to give run times approximately equal to rdseq.
        workvol: the mounted volume that will be tested.
        kill_slower: finished worker kills all unfinished sibling workers.
            This shortens runs but does not affect the DTF statistics.
        timeout = '': run fastest worker to completion
        timeout = '100s': kill fastest worker too after 100 seconds
            Keeps 25_25_25_25% experiment from taking 4x longer than 95_5%.
            This should be set longer than most experiments, and long enough
            to reach steady state and good measurements on all experiments.
        pre_experiment_cb: Callback to run after container creation, before the
                           experiment. Must take a list of cgroup names.
        post_experiment_cb: Callback to run after experiment, before container
                            deletion. Must take a list of cgroup names.
        google_hacks: Google-specific hacks for device lookup
        """

        try:
            opts, args = getopt.getopt(sys.argv[1:], 'ch', ['help'])
        except getopt.GetoptError, err:
            print str(err)
            usage(sys.argv)
            sys.exit(2)

        cleanup = False
        for o, a in opts:
            if o == '-c':
                cleanup = True
            elif o in ('-h', '--help'):
                usage(sys.argv)
                sys.exit()
            else:
                assert False, 'unhandled option: ' + o

        if cleanup:
            delete_test_containers()
        logging.info('Starting test "%s"', self.title)

        # Create the test directory on the workvol.
        if not os.path.exists(workvol):
            raise error.Error('Machine does not have %s' % workvol)

        self.workdir = os.path.join(workvol, 'blkcgroup_test_tmp')
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)
        else:
            # Remove all previous content from "workdir"s subdirectories.
            utils.system('rm -rf %s/*' % self.workdir)

        # Get get the underlying device name where the workvol is located.
        if google_hacks:
          self.device = actual_disk_device(device_holding_file(workvol))
        else:
          self.device = device_holding_file(workvol)

        enable_blkio_and_cfq(self.device)
        old_group_isolation = get_group_isolation(self.device)
        set_group_isolation(self.device, '1')

        logging.debug('Measuring IO on disk %s', self.device)

        # Setup test specific parameters.
        self.srcdir = os.path.dirname(__file__)
        self.input_file_count = self.output_file_count = 0
        self.existing_input_files = {}
        self.tried_experiments  = 0
        self.passed_experiments = 0

        logging.info('%d total experiment runs', len(experiments))

        # TODO: Before running all experiments, validate them to fail early on
        # a bad experiment list.

        # Iterate over all experiments.
        for i, experiment in enumerate(experiments):
            workers, allowed_error = experiment
            self.run_single_experiment(i, workers, seq_read_mb,
                                       kill_slower, timeout, allowed_error,
                                       pre_experiment_cb, post_experiment_cb)

        # Presenting results.
        logging.info('-----ran %d experiments, %d passed, %d failed',
                self.tried_experiments,  self.passed_experiments,
                self.tried_experiments - self.passed_experiments)

        # Cleanup.
        utils.system('rm -rf %s' % self.workdir)
        set_group_isolation(self.device, old_group_isolation)