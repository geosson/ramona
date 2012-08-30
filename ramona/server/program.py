import sys, os, time, logging, shlex, signal, resource, fcntl, errno
import pyev
from ..config import config
from ..utils import parse_signals
from ..kmpsearch import KnuthMorrisPratt

#

L = logging.getLogger("subproc")

#

MAXFD = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
if (MAXFD == resource.RLIM_INFINITY): MAXFD = 1024

#

class program(object):

	DEFAULTS = {
		'command': None,
		'starttimeout': 1,
		'stoptimeout': 3,
		'stopsignal': 'INT,TERM,KILL',
	}


	class state_enum:
		'''Enum'''
		STOPPED = 0
		STARTING = 10
		RUNNING = 20
		STOPPING = 30
		FATAL = 200

		labels = {
			STOPPED: 'STOPPED',
			STARTING: 'STARTING',
			RUNNING: 'RUNNING',
			STOPPING: 'STOPPING',
			FATAL: 'FATAL',
		}


	def __init__(self, loop, config_section):
		_, self.ident = config_section.split(':', 2)
		self.state = program.state_enum.STOPPED
		self.pid = None

		self.launch_cnt = 0
		self.start_time = None
		self.stop_time = None
		self.term_time = None

		self.stdout = None
		self.stderr = None
		self.watchers = [
			pyev.Io(0, 0, loop, self.__read_stdfd, 0),
			pyev.Io(0, 0, loop, self.__read_stdfd, 1),
		]

		# Build configuration
		self.config = self.DEFAULTS.copy()
		self.config.update(config.items(config_section))

		cmd = self.config.get('command')
		if cmd is None:
			L.fatal("Program {0} doesn't specify command - don't know how to launch it".format(self.ident))
			sys.exit(2)

		self.cmdline = shlex.split(cmd)
		self.stopsignals = parse_signals(self.config['stopsignal'])
		if len(self.stopsignals) == 0: self.stopsignals = [signal.SIGTERM]
		self.act_stopsignals = None

		# Prepare log files
		self.log_out = None
		self.log_out_fname = os.path.join(config.get('server','logdir'), self.ident + '-out.log')
		self.log_err = None
		self.log_err_fname = os.path.join(config.get('server','logdir'), self.ident + '-err.log')

		# Log searching 
		self.kmp = KnuthMorrisPratt('error')


	def __repr__(self):
		return "<{0} {1} state={2} pid={3}>".format(self.__class__.__name__, self.ident, program.state_enum.labels[self.state],self.pid if self.pid is not None else '?')


	def spawn(self, cmd, args):
		self.stdout, stdout = os.pipe()
		self.stderr, stderr = os.pipe()

		pid = os.fork()
		if pid !=0:
			os.close(stdout)
			os.close(stderr)

			fl = fcntl.fcntl(self.stdout, fcntl.F_GETFL)
			fcntl.fcntl(self.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
			self.watchers[0].set(self.stdout, pyev.EV_READ)
			self.watchers[0].start()

			fl = fcntl.fcntl(self.stderr, fcntl.F_GETFL)
			fcntl.fcntl(self.stderr, fcntl.F_SETFL, fl | os.O_NONBLOCK)
			self.watchers[1].set(self.stderr, pyev.EV_READ)
			self.watchers[1].start()

			return pid

		stdin = os.open('/dev/null', os.O_RDONLY) # Open stdin
		os.dup2(stdin, 0)
		os.dup2(stdout, 1) # Prepare stdout
		os.dup2(stderr, 2) # Prepare stderr

		# Close all open file descriptors above standard ones.  This prevents the child from keeping
   		# open any file descriptors inherited from the parent.
		os.closerange(3, MAXFD)

		os.execvp(cmd, args)
		sys.exit(3)


	def start(self):
		'''Transition to state STARTING'''
		assert self.state == program.state_enum.STOPPED

		L.debug("{0} -> STARTING".format(self))

		self.log_out = open(self.log_out_fname,'a')
		self.log_err = open(self.log_err_fname,'a')

		self.pid = self.spawn(self.cmdline[0], self.cmdline) #TODO: self.cmdline[0] can be substituted by self.ident or any arbitrary string
		self.state = program.state_enum.STARTING
		self.start_time = time.time()
		self.stop_time = None
		self.term_time = None
		self.launch_cnt += 1


	def stop(self):
		'''Transition to state STOPPING'''
		assert self.pid is not None
		assert self.state in (program.state_enum.RUNNING, program.state_enum.STARTING)

		L.debug("{0} -> STOPPING".format(self))
		self.act_stopsignals = self.stopsignals[:]
		signal = self.get_next_stopsignal()
		try:
			os.kill(self.pid, signal)
		except:
			pass
		self.state = program.state_enum.STOPPING
		self.stop_time = time.time()


	def on_terminate(self, status):
		self.term_time = time.time()
		self.pid = None

		# Close log files
		self.log_out.close()
		self.log_out = None
		self.log_err.close()
		self.log_err = None

		# Close process stdout and stderr pipes
		self.watchers[0].stop()
		if self.stdout is not None:
			os.close(self.stdout)
			self.stdout = None

		self.watchers[1].stop()
		if self.stderr is not None:
			os.close(self.stderr)
			self.stderr = None

		# Handle state change properly
		if self.state == program.state_enum.STARTING:
			L.warning("{0} exited too quickly (-> FATAL)".format(self))
			self.state = program.state_enum.FATAL

		elif self.state == program.state_enum.STOPPING:
			L.debug("{0} -> STOPPED".format(self))
			self.state = program.state_enum.STOPPED

		else:
			L.warning("{0} exited unexpectedly (-> FATAL)".format(self))
			self.state = program.state_enum.FATAL


	def on_tick(self, now):
		# Switch starting programs into running state
		if self.state == program.state_enum.STARTING:
			if now - self.start_time >= self.config['starttimeout']:
				L.debug("{0} -> RUNNING".format(self))
				self.state = program.state_enum.RUNNING

		elif self.state == program.state_enum.STOPPING:
			if now - self.start_time >= self.config['stoptimeout']:
				L.warning("{0} is still terminating - sending another signal".format(self))
				signal = self.get_next_stopsignal()
				try:
					os.kill(self.pid, signal)
				except:
					pass


	def get_next_stopsignal(self):
		if len(self.act_stopsignals) == 0: return signal.SIGKILL
		return self.act_stopsignals.pop(0)


	def __read_stdfd(self, watcher, revents):
		while 1:
			try:
				data = os.read(watcher.fd, 4096)
			except OSError, e:
				if e.errno == errno.EAGAIN: return # No more data to read (would block)
				raise

			if len(data) == 0: # File descriptor is closed
				watcher.stop()
				os.close(watcher.fd)
				if watcher.data == 0: self.stderr = None
				elif watcher.data == 1: self.stdout = None
				return 

			if watcher.data == 0: self.log_out.write(data)
			elif watcher.data == 1: self.log_err.write(data)

			if watcher.data == 0: 
				i = self.kmp.search(data)
				if i >= 0:
					# Pattern detected in the data
					pass


###

class program_roaster(object):

	def __init__(self):
		self.roaster = []
		for config_section in config.sections():
			if config_section.find('program:') != 0: continue
			sp = program(self.loop, config_section)
			self.roaster.append(sp)


	def start_program(self):
		# Start processes that are STOPPED
		#TODO: Switch to allow starting state.FATAL programs too
		for p in self.roaster:
			if p.state not in (program.state_enum.STOPPED,): continue
			p.start()


	def stop_program(self):
		# Stop processes that are RUNNING and STARTING
		for p in self.roaster:
			if p.state not in (program.state_enum.RUNNING, program.state_enum.STARTING): continue
			p.stop()


	def restart_program(self):
		#TODO: This ...
		pass


	def on_terminate_program(self, pid, status):
		for p in self.roaster:
			if pid != p.pid: continue
			return p.on_terminate(status)
		else:
			L.warning("Unknown program died (pid={0}, status={1})".format(pid, status))


	def on_tick(self):
		'''Periodic check of program states'''
		now = time.time()
		for p in self.roaster:
			p.on_tick(now)
