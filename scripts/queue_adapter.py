#!/usr/bin/env python3
"""Queue adapter for a PRIVATE, pinned Pueue 4.0.4 daemon (Phase 3, W3).

Scope and non-claims:
- Only a private daemon is supported: the caller names a 0700 root directory and
  the adapter writes its own config, state directory and socket inside it. The
  default Pueue socket, config and the user's environment are never used, and a
  system `pueue` from PATH is never called. Binaries are accepted only by path and
  only when their sha256 matches the pinned 4.0.4 release. Each verified binary is
  copied into <root>/bin (0700), the copy is re-verified, and only the copy is ever
  executed, so a writer to the original directory cannot swap it after the check.
- Production dispatch is disabled. `researchctl` does not import this module and
  its queued path stays BLOCKED by `require_supported_execution`.
- Nothing here proves containment, completion or provenance. Daemon observations
  are reported, never minted into a VerifiedProof.

Protocol (durable writes in run_registry):
1. `submit` commits INTENT (queue identity = sha256 of the daemon certificate)
   BEFORE calling `pueue add`. The task is added stashed with a label that binds it
   to run_id and intent token.
2. Any uncertain result (timeout, OS error, non-zero exit, unparseable task ID,
   daemon identity change) fences the intent AMBIGUOUS and raises
   AmbiguousSubmission. There is no automatic retry, and a later submit for the
   same run_id is refused by the registry.
3. `claim_attempt` is idempotent for the same claimant process and binds the queue
   task to the queue-issued attempt and fence in the run-local registry.
4. `release` records ENQUEUE_ATTEMPTED before `pueue enqueue`; a timeout or lost
   response fences the BOUND row AMBIGUOUS, and restart recovery does the same for
   an attempt with no recorded outcome. Never retried.
5. `reconcile` runs registry restart recovery, then only READS the daemon state.
   It never adds, enqueues, restarts or removes tasks. Daemon errors, timeouts and
   OS errors are reported as `daemon_error` with no state transition.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_ownership  # noqa: E402
import run_registry  # noqa: E402
from run_ownership import RegistryError  # noqa: E402

PUEUE_VERSION = '4.0.4'
PINNED_SHA256 = {
    'pueue': 'c1b10d7e4e62211075ddd0e1dc3e8cbfc5a43d662cb3be7402a28504e23fcb51',
    'pueued': '5afeff6adbafb909e8d54e2caff158e6966c2adffa2c09e60fd631cc51b60390',
}
# Production dispatch through any shared/default daemon is not implemented.
PRODUCTION_DISPATCH_ENABLED = False
LABEL_PREFIX = 'herdr-ml'
_TASK_ID = re.compile(r'[0-9]{1,18}')


class QueueAdapterError(RegistryError):
    """Adapter refusal. Subclasses RegistryError so callers see one error family."""


class BinaryUnavailable(QueueAdapterError):
    """Pinned binary missing or checksum mismatch. Never falls back to PATH."""


class AmbiguousSubmission(QueueAdapterError):
    """Submission outcome unknown. The intent is fenced AMBIGUOUS and never retried."""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_binary(path, role):
    """Return the resolved path of a pinned binary, or raise BinaryUnavailable."""
    if role not in PINNED_SHA256:
        raise BinaryUnavailable('Unknown binary role %r' % role)
    if path is None:
        raise BinaryUnavailable('%s path not given; refusing to search PATH' % role)
    try:
        resolved = Path(path).resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise BinaryUnavailable('%s binary missing: %s' % (role, path)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise BinaryUnavailable('%s binary is not a regular file' % role)
    actual = sha256_file(resolved)
    if actual != PINNED_SHA256[role]:
        raise BinaryUnavailable('%s checksum mismatch: %s' % (role, actual))
    return resolved


def private_copy(source, bin_dir, role):
    """Copy a verified binary into the owner-only bin_dir, re-verify the copy, return its path.

    The copy is written to a fresh temporary file and renamed over <bin_dir>/<role>, so
    a running daemon's executable is never truncated. The original path is never
    executed by the adapter.
    """
    verified = verify_binary(source, role)
    target = bin_dir / role
    temp = bin_dir / ('.%s.%d.tmp' % (role, os.getpid()))
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(fd, 'wb') as out, open(verified, 'rb') as src:
            for block in iter(lambda: src.read(1 << 20), b''):
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o700)
        os.replace(temp, target)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise BinaryUnavailable('%s binary could not be copied into the private root' % role) from exc
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BinaryUnavailable('%s private copy is not an owner-only regular file' % role)
    actual = sha256_file(target)
    if actual != PINNED_SHA256[role]:
        raise BinaryUnavailable('%s private copy checksum mismatch: %s' % (role, actual))
    return target


def task_label(run_id, intent_token):
    return '%s:%s:%s' % (LABEL_PREFIX, run_id, intent_token)


class PrivatePueue:
    """One private daemon rooted in a caller-owned 0700 directory.

    Layout: <root>/bin (verified private copies of pueue and pueued; the only binaries
    executed), <root>/pueue.yml, <root>/data (pueue_directory), <root>/run (runtime,
    socket `s.sock`, pid file), <root>/xdg (empty XDG/HOME sandbox). The socket path
    is relative to <root>/run, so long roots stay under the unix socket length limit;
    every client and daemon call runs with cwd=<root>/run.
    """

    def __init__(self, root, pueue, pueued, *, timeout=10.0):
        verify_binary(pueue, 'pueue')
        verify_binary(pueued, 'pueued')
        root = Path(root)
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077):
            raise QueueAdapterError('Private daemon root must be an owner-only (0700) directory')
        self.root = root.resolve(strict=True)
        default_config = Path(os.path.expanduser('~')) / '.config' / 'pueue'
        if self.root == default_config or default_config in self.root.parents:
            raise QueueAdapterError('Refusing to use the default Pueue configuration directory')
        self.timeout = timeout
        self.bin = self.root / 'bin'
        self.bin.mkdir(mode=0o700, exist_ok=True)
        info = self.bin.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077):
            raise QueueAdapterError('Private binary directory must be an owner-only (0700) directory')
        # Only these verified copies are executed; the caller's paths are never run.
        self.pueue = private_copy(pueue, self.bin, 'pueue')
        self.pueued = private_copy(pueued, self.bin, 'pueued')
        self.config = self.root / 'pueue.yml'
        self.data = self.root / 'data'
        self.runtime = self.root / 'run'
        self.sandbox = self.root / 'xdg'
        for path in (self.data, self.runtime, self.sandbox):
            path.mkdir(mode=0o700, exist_ok=True)
        config = {
            'shared': {
                'pueue_directory': str(self.data),
                'runtime_directory': str(self.runtime),
                'use_unix_socket': True,
                'unix_socket_path': 's.sock',
                'pid_path': str(self.runtime / 'pueue.pid'),
            },
            'daemon': {},
            'client': {'show_confirmation_questions': False},
        }
        # JSON is a subset of YAML; Pueue reads it with its YAML parser.
        self.config.write_text(json.dumps(config, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        os.chmod(self.config, 0o600)
        self._daemon = None

    # -- process environment -------------------------------------------------
    def env(self):
        """Sanitized environment: nothing from the caller, so no default socket or config leaks in."""
        return {
            'PATH': '/usr/bin:/bin',
            'HOME': str(self.sandbox),
            'XDG_CONFIG_HOME': str(self.sandbox),
            'XDG_DATA_HOME': str(self.sandbox),
            'XDG_RUNTIME_DIR': str(self.runtime),
            'PUEUE_CONFIG_PATH': str(self.config),
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'SHELL': '/bin/sh',
        }

    def socket_path(self):
        return self.runtime / 's.sock'

    def client(self, *args, timeout=None):
        """Run the pinned client against this daemon only. May raise TimeoutExpired/OSError."""
        return subprocess.run([str(self.pueue), '-c', str(self.config), *args],
                              cwd=str(self.runtime), env=self.env(), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              timeout=self.timeout if timeout is None else timeout, check=False)

    # -- daemon lifecycle (only the daemon this object started) ---------------
    def start(self, *, ready_timeout=10.0):
        if self._daemon is not None and self._daemon.poll() is None:
            raise QueueAdapterError('This private daemon is already running')
        if self.socket_path().exists():
            probe = self.client('status', timeout=2)
            if probe.returncode == 0:
                raise QueueAdapterError('A daemon already answers on this private socket; not ours')
        self._daemon = subprocess.Popen([str(self.pueued), '-c', str(self.config)],
                                        cwd=str(self.runtime), env=self.env(),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if self._daemon.poll() is not None:
                raise QueueAdapterError('Private daemon exited during startup (%s)' % self._daemon.returncode)
            try:
                if self.client('status', timeout=2).returncode == 0:
                    return self._daemon.pid
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.05)
        self.kill()
        raise QueueAdapterError('Private daemon did not become ready')

    @property
    def pid(self):
        return None if self._daemon is None else self._daemon.pid

    def running(self):
        return self._daemon is not None and self._daemon.poll() is None

    def stop(self):
        """Shut down through our own socket; returns (shutdown_exit, daemon_exit)."""
        if self._daemon is None:
            return None, None
        code = None
        if self._daemon.poll() is None:
            try:
                code = self.client('shutdown', timeout=5).returncode
            except (OSError, subprocess.TimeoutExpired):
                code = None
            try:
                self._daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()
        return code, self._daemon.returncode

    def kill(self):
        """Hard stop of the daemon this object started (simulated crash)."""
        if self._daemon is not None and self._daemon.poll() is None:
            self._daemon.kill()
            self._daemon.wait(timeout=5)

    # -- identity and state ----------------------------------------------------
    def instance_id(self):
        """Queue instance identity: sha256 of this daemon's certificate on disk.

        Stable across restarts of the same state directory; changes if the state is
        replaced. An on-disk identity, not an authenticated proof of the socket peer.
        """
        cert = self.data / 'certs' / 'daemon.cert'
        try:
            return hashlib.sha256(cert.read_bytes()).hexdigest()
        except OSError as exc:
            raise QueueAdapterError('Private daemon certificate missing; queue identity unknown') from exc

    def status(self):
        result = self.client('status', '--json')
        if result.returncode:
            raise QueueAdapterError('Private daemon status failed: %s' % result.stderr.strip()[-300:])
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise QueueAdapterError('Private daemon status was not JSON') from exc


# -- protocol -------------------------------------------------------------------

def submit(daemon, folder, run_id, manifest_sha256, command, *, _store_home=None):
    """Durable intent, then exactly one stashed `pueue add`. Never retried.

    Returns {'intent_token', 'task_id', 'queue_instance'} on a clean binding.
    """
    if not isinstance(daemon, PrivatePueue):
        raise QueueAdapterError('Only a PrivatePueue daemon is supported; production dispatch is disabled')
    if not command or not all(isinstance(part, str) and part for part in command):
        raise QueueAdapterError('Command must be a non-empty list of strings')
    instance = daemon.instance_id()
    intent = run_registry.queue_intent(folder, run_id, manifest_sha256, instance, _store_home=_store_home)
    # INTENT is committed. From here on, every uncertain outcome is fenced, not retried.

    def fence(reason):
        run_registry.queue_mark_ambiguous(folder, intent, reason, _store_home=_store_home)
        raise AmbiguousSubmission('Submission outcome unknown (%s); intent fenced AMBIGUOUS, not retried'
                                  % reason)

    try:
        result = daemon.client('add', '--stashed', '--print-task-id', '--escape',
                               '--label', task_label(run_id, intent), '--', *command)
    except subprocess.TimeoutExpired:
        fence('pueue add timed out')
    except OSError as exc:
        fence('pueue add could not run: %s' % exc.__class__.__name__)
    if result.returncode != 0:
        fence('pueue add exit %d' % result.returncode)
    task_id = result.stdout.strip()
    if not _TASK_ID.fullmatch(task_id):
        fence('unparseable task ID')
    try:
        same_instance = daemon.instance_id() == instance
    except QueueAdapterError:
        same_instance = False
    if not same_instance:
        fence('queue instance identity changed during submission')
    run_registry.queue_bind_task(folder, intent, task_id, _store_home=_store_home)
    return {'intent_token': intent, 'task_id': task_id, 'queue_instance': instance}


def release(daemon, folder, *, _store_home=None):
    """Enqueue the stashed task, only for a BOUND row on the same daemon instance.

    The attempt is recorded durably before `pueue enqueue`. A timeout, OS error or
    any non-zero exit (including a negative code from a signal) fences the row
    AMBIGUOUS (origin BOUND) and raises AmbiguousSubmission; nothing is retried.
    """
    row = run_registry.inspect_queue(folder, _store_home=_store_home)
    if row is None or row['phase'] != 'BOUND':
        raise QueueAdapterError('Only a BOUND task can be released to a worker')
    if daemon.instance_id() != row['queue_instance']:
        raise QueueAdapterError('Daemon instance does not match the bound queue identity')
    task = daemon.status()['tasks'].get(row['task_id'])
    if task is None or task.get('label') != task_label(row['run_id'], row['intent_token']):
        raise QueueAdapterError('Bound task is absent from the daemon or carries another label')
    # Durable marker BEFORE the external call: until an outcome is recorded, restart
    # recovery fences this row AMBIGUOUS.
    run_registry.queue_enqueue_attempted(folder, row['intent_token'], _store_home=_store_home)

    def unknown(reason):
        phase = run_registry.queue_enqueue_outcome(folder, row['intent_token'], 'ENQUEUE_UNKNOWN', reason,
                                                   _store_home=_store_home)
        raise AmbiguousSubmission('Enqueue outcome unknown (%s); queue row is %s, not retried'
                                  % (reason, phase))

    try:
        result = daemon.client('enqueue', row['task_id'])
    except subprocess.TimeoutExpired:
        unknown('pueue enqueue timed out')
    except OSError as exc:
        unknown('pueue enqueue could not run: %s' % exc.__class__.__name__)
    if result.returncode != 0:
        # The client also exits non-zero when the response is lost after the daemon
        # acted, and a signal gives a negative code: the task may be running, so
        # every non-zero exit is unknown, never a refusal.
        unknown('pueue enqueue exit %d: %s' % (result.returncode, (result.stderr or '').strip()[-300:]))
    run_registry.queue_enqueue_outcome(folder, row['intent_token'], 'ENQUEUED', _store_home=_store_home)
    return row['task_id']


def claim_attempt(folder, run_id, project, manifest_sha256, queue_instance, task_id, worker_id, *,
                  _store_home=None):
    """Idempotent queued claim. Binds queue (instance, task) to the queue-issued attempt and fence.

    The first call claims the queue row and creates the run-local owner record. A repeat
    from the same process returns the same owner token and writes nothing. Returns
    {'token', 'attempt_id', 'fence', 'replayed'}.
    """
    attempt_id, fence, replayed = run_registry.queue_claim_idempotent(
        folder, run_id, manifest_sha256, queue_instance, task_id, worker_id, _store_home=_store_home)
    if replayed and run_registry._local_claim_matches(folder, run_id, attempt_id, fence):
        return {'token': run_ownership.encode_token(attempt_id, fence), 'attempt_id': attempt_id,
                'fence': fence, 'replayed': True}
    token = run_registry.claim(folder, run_id, project, manifest_sha256, 'QUEUED',
                               queue_instance=queue_instance, queue_task_id=task_id,
                               queue_worker_id=worker_id, queue_attempt_id=attempt_id,
                               queue_fence=fence, _store_home=_store_home)
    return {'token': token, 'attempt_id': attempt_id, 'fence': fence, 'replayed': replayed}


def bound_identity(folder, *, _store_home=None):
    """Return the queue/run/attempt binding only when queue row and run-local row agree."""
    row = run_registry.inspect_queue(folder, _store_home=_store_home)
    if row is None or row['phase'] != 'CLAIMED':
        raise QueueAdapterError('No claimed queue binding')
    local = run_registry.inspect(folder)
    if (local is None or local.get('identity') != 'CURRENT' or local.get('run_id') != row['run_id'] or
            local.get('attempt_id') != row['attempt_id'] or local.get('fence') != row['fence'] or
            local.get('queue_task_id') != row['task_id'] or local.get('worker_id') != row['worker_id'] or
            local.get('manifest_sha256') != row['manifest_sha256']):
        raise QueueAdapterError('Queue binding and run-local identity disagree')
    return {'run_id': row['run_id'], 'project': row['project'], 'queue_instance': row['queue_instance'],
            'task_id': row['task_id'], 'worker_id': row['worker_id'], 'attempt_id': row['attempt_id'],
            'fence': row['fence'], 'identity_sha256': local.get('identity_sha256')}


def reconcile(daemon, folder, *, _store_home=None):
    """Restart reconciliation: registry recovery, then a read-only look at the private daemon.

    Never submits, enqueues, restarts or removes anything, and never resolves an
    AMBIGUOUS row (that needs a VerifiedProof, and no verifier is registered).
    Fencing requires quiescence: while a submitter, claimant or local owner of the
    run is live or undeterminable, run_registry.RecoveryDeferred is raised and
    nothing is written.
    """
    phase = run_registry.queue_recover(folder, _store_home=_store_home, require_quiescent=True)
    row = run_registry.inspect_queue(folder, _store_home=_store_home)
    report = {'phase': phase, 'resubmitted': False, 'instance_matches': None,
              'daemon_reachable': False, 'tasks_for_intent': [], 'bound_task_present': None,
              'daemon_task_count': None}
    if row is None:
        return report
    try:
        instance = daemon.instance_id()
        state = daemon.status()
    except QueueAdapterError as exc:
        report['daemon_error'] = str(exc)
        return report
    except subprocess.TimeoutExpired:
        report['daemon_error'] = 'Private daemon status timed out'
        return report
    except OSError as exc:
        report['daemon_error'] = 'Private daemon status could not run: %s' % exc.__class__.__name__
        return report
    report['daemon_reachable'] = True
    report['instance_matches'] = instance == row['queue_instance']
    tasks = state.get('tasks', {})
    report['daemon_task_count'] = len(tasks)
    if report['instance_matches']:
        label = task_label(row['run_id'], row['intent_token'])
        report['tasks_for_intent'] = sorted(tid for tid, task in tasks.items() if task.get('label') == label)
        if row['task_id'] is not None:
            report['bound_task_present'] = row['task_id'] in report['tasks_for_intent']
    return report
