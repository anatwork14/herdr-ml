"""Transactional per-run ownership for a single trusted Linux host.

SQLite is authoritative; JSON files written by the runner are projections.
An active claim is never reclaimed here, even when its owner appears dead:
without verified containment, descendants may still be running.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import uuid


class RegistryError(Exception):
    pass


def _connect(folder):
    db = sqlite3.connect(str(Path(folder) / 'registry.sqlite3'), timeout=30, isolation_level=None)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('''CREATE TABLE IF NOT EXISTS run (
        id INTEGER PRIMARY KEY CHECK (id = 1), run_id TEXT NOT NULL,
        project TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        state TEXT NOT NULL, owner TEXT NOT NULL, owner_pid INTEGER NOT NULL,
        owner_host TEXT NOT NULL, owner_boot TEXT, owner_start TEXT,
        queue_task_id TEXT, result_json TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS child_identity (
        id INTEGER PRIMARY KEY CHECK (id=1), pid INTEGER NOT NULL,
        boot TEXT, start_ticks TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS process_namespaces (
        role TEXT PRIMARY KEY CHECK (role IN ('owner','child')),
        pid_namespace TEXT, mount_namespace TEXT)''')
    # Queue state is separate from execution ownership: an enqueue attempt is
    # durable before calling a queue, and an ambiguous response is never retried.
    db.execute('''CREATE TABLE IF NOT EXISTS queue_binding (
        id INTEGER PRIMARY KEY CHECK (id=1), run_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL, intent_token TEXT NOT NULL UNIQUE,
        phase TEXT NOT NULL CHECK (phase IN ('INTENT','BOUND','AMBIGUOUS','CLAIMED')),
        task_id TEXT UNIQUE, worker_id TEXT, fence TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        owner TEXT, state TEXT NOT NULL, detail TEXT)''')
    return db


def _identity():
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stat = Path('/proc/self/stat').read_text()
        start = stat[stat.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        boot, start = None, None
    return boot, start


def _namespaces(pid):
    """Capture procfs namespace identity; missing information is never proof."""
    try:
        return (os.readlink('/proc/%d/ns/pid' % pid),
                os.readlink('/proc/%d/ns/mnt' % pid))
    except OSError:
        return None, None


def queue_intent(folder, run_id, manifest_sha256):
    """Durably record intent before external enqueue; never reissue an intent."""
    token = uuid.uuid4().hex
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO queue_binding VALUES (1,?,?,?,\'INTENT\',NULL,NULL,NULL)',
                   (run_id, manifest_sha256, token))
        db.execute('INSERT INTO events(state,detail) VALUES (?,?)',
                   ('QUEUE_INTENT', json.dumps({'run_id': run_id, 'intent_token': token})))
        db.execute('COMMIT')
        return token
    except sqlite3.IntegrityError as exc:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise RegistryError('Queue intent already exists; never retry enqueue automatically') from exc
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def queue_bind_task(folder, intent_token, task_id):
    """Bind the one queue response to this run; task IDs are opaque strings."""
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256 or '\x00' in task_id:
        raise RegistryError('Invalid queue task ID')
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT phase FROM queue_binding WHERE id=1 AND intent_token=?",
                         (intent_token,)).fetchone()
        if row != ('INTENT',):
            raise RegistryError('Stale intent or queue response already resolved')
        db.execute("UPDATE queue_binding SET phase='BOUND',task_id=? WHERE id=1", (task_id,))
        db.execute('INSERT INTO events(state,detail) VALUES (?,?)',
                   ('QUEUE_BOUND', json.dumps({'task_id': task_id})))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def queue_mark_ambiguous(folder, intent_token, detail=''):
    """Fence an uncertain enqueue response. This state cannot be rebound/retried."""
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute("UPDATE queue_binding SET phase='AMBIGUOUS' WHERE id=1 AND intent_token=? AND phase='INTENT'",
                             (intent_token,)).rowcount
        if updated != 1:
            raise RegistryError('Stale intent or queue response already resolved')
        db.execute('INSERT INTO events(state,detail) VALUES (?,?)',
                   ('QUEUE_AMBIGUOUS', str(detail)[:1000]))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def queue_claim(folder, run_id, manifest_sha256, task_id, worker_id):
    """Claim a bound task once and return its fencing token; no stale takeover."""
    if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 256 or '\x00' in worker_id:
        raise RegistryError('Invalid worker identity')
    db = _connect(folder)
    fence = uuid.uuid4().hex
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute("UPDATE queue_binding SET phase='CLAIMED',worker_id=?,fence=? "
                             "WHERE id=1 AND run_id=? AND manifest_sha256=? AND task_id=? AND phase='BOUND'",
                             (worker_id, fence, run_id, manifest_sha256, task_id)).rowcount
        if updated != 1:
            raise RegistryError('Queue task/run/manifest mismatch, stale replay, or ambiguous enqueue')
        db.execute('INSERT INTO events(state,detail) VALUES (?,?)',
                   ('QUEUE_CLAIMED', json.dumps({'task_id': task_id, 'worker_id': worker_id,
                                                 'fence': fence})))
        db.execute('COMMIT')
        return fence
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def inspect_queue(folder):
    """Return queue lifecycle state even before an execution row exists."""
    db = _connect(folder)
    try:
        row = db.execute('SELECT run_id,manifest_sha256,intent_token,phase,task_id,worker_id,fence '
                         'FROM queue_binding WHERE id=1').fetchone()
        if row is None:
            return None
        return dict(zip(('run_id','manifest_sha256','intent_token','phase','task_id','worker_id','fence'), row))
    finally:
        db.close()


def claim(folder, run_id, project, digest, initial_state, *,
          queue_task_id=None, queue_worker_id=None, queue_fence=None):
    """Atomically create a run and claim it exactly once, never stealing owners."""
    if initial_state not in ('SUBMITTED', 'QUEUED'):
        raise RegistryError('Run is not eligible for execution')
    if initial_state == 'QUEUED' and not all((queue_task_id, queue_worker_id, queue_fence)):
        raise RegistryError('Queued worker requires bound task, worker identity, and fence')
    if initial_state == 'SUBMITTED' and any((queue_task_id, queue_worker_id, queue_fence)):
        raise RegistryError('Local execution cannot carry queue identity')
    if initial_state == 'QUEUED':
        check = _connect(folder)
        try:
            bound = check.execute("SELECT run_id,manifest_sha256,phase,task_id,worker_id,fence "
                                  "FROM queue_binding WHERE id=1").fetchone()
        finally:
            check.close()
        if bound != (run_id, digest, 'CLAIMED', queue_task_id, queue_worker_id, queue_fence):
            raise RegistryError('Queued run/task/worker fence mismatch')
    # Older deployed workers claim with mkdir. Acquire the same marker first,
    # atomically, so mixed versions cannot execute concurrently. Never delete
    # it automatically: a crash here needs manual investigation.
    try:
        (Path(folder) / 'execution.claim').mkdir(mode=0o700)
    except FileExistsError as exc:
        raise RegistryError('Execution already claimed; reconcile before retry') from exc
    directory = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    db = _connect(folder)
    token = uuid.uuid4().hex
    try:
        db.execute('BEGIN IMMEDIATE')
        if initial_state == 'QUEUED':
            bound = db.execute("SELECT run_id,manifest_sha256,phase,task_id,worker_id,fence "
                               "FROM queue_binding WHERE id=1").fetchone()
            if bound != (run_id, digest, 'CLAIMED', queue_task_id, queue_worker_id, queue_fence):
                raise RegistryError('Queued run/task/worker fence mismatch')
        if db.execute('SELECT 1 FROM run WHERE id=1').fetchone():
            raise RegistryError('Run already claimed; do not retry without reconciliation')
        boot, start = _identity()
        db.execute('INSERT INTO run VALUES (1,?,?,?,?,?,?,?,?,?,?,?)',
                   (run_id, project, digest, 'CLAIMED', token, os.getpid(),
                    socket.gethostname(), boot, start, queue_task_id, None))
        pid_ns, mount_ns = _namespaces(os.getpid())
        db.execute("INSERT INTO process_namespaces VALUES ('owner',?,?)", (pid_ns, mount_ns))
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)',
                   (token, 'CLAIMED', 'Atomic one-time owner acquisition'))
        db.execute('COMMIT')
        # Persist the new registry directory entry before any dispatch.
        directory = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return token
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def record_child(folder, token, pid):
    """Record forensic identity; this is not evidence of containment."""
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stat = Path('/proc/%d/stat' % pid).read_text()
        start = stat[stat.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        boot, start = None, None
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM run WHERE id=1 AND owner=? AND state='DISPATCHING'", (token,)).fetchone():
            raise RegistryError('Stale owner or illegal child registration')
        db.execute('INSERT INTO child_identity VALUES (1,?,?,?)', (pid, boot, start))
        pid_ns, mount_ns = _namespaces(pid)
        db.execute("INSERT INTO process_namespaces VALUES ('child',?,?)", (pid_ns, mount_ns))
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)',
                   (token, 'CHILD_IDENTIFIED', json.dumps({'pid': pid, 'boot': boot, 'start_ticks': start})))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def advance(folder, token, expected, target):
    if (expected, target) not in {('CLAIMED', 'DISPATCHING'), ('DISPATCHING', 'RUNNING')}:
        raise RegistryError('Illegal execution transition')
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute('UPDATE run SET state=? WHERE id=1 AND owner=? AND state=?',
                             (target, token, expected)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal execution state')
        db.execute('INSERT INTO events(owner,state) VALUES (?,?)', (token, target))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def finish(folder, token, result):
    state = result.get('state')
    if state != 'UNKNOWN':
        raise RegistryError('Invalid terminal state')
    data = json.dumps(result, sort_keys=True)
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute("UPDATE run SET state=?, result_json=? WHERE id=1 AND owner=? AND state IN ('RUNNING','DISPATCHING')",
                             (state, data, token)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal terminal state')
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)', (token, state, data))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def inspect(folder):
    path = Path(folder) / 'registry.sqlite3'
    if not path.is_file():
        return None
    db = _connect(folder)
    try:
        row = db.execute('SELECT run_id,project,manifest_sha256,state,owner_pid,owner_host,owner_boot,owner_start,queue_task_id,result_json FROM run WHERE id=1').fetchone()
        if row is None:
            return None
        fields = ('run_id','project','manifest_sha256','state','owner_pid','owner_host','owner_boot','owner_start','queue_task_id','result')
        result = dict(zip(fields, row))
        binding = db.execute('SELECT run_id,manifest_sha256,intent_token,phase,task_id,worker_id,fence '
                             'FROM queue_binding WHERE id=1').fetchone()
        result['queue_binding'] = (dict(zip(('run_id','manifest_sha256','intent_token','phase',
                                             'task_id','worker_id','fence'), binding))
                                   if binding else None)
        if result['result'] is not None:
            result['result'] = json.loads(result['result'])
        namespaces = {role: (pid_ns, mount_ns) for role, pid_ns, mount_ns in
                      db.execute('SELECT role,pid_namespace,mount_namespace FROM process_namespaces')}
        owner_ns = namespaces.get('owner', (None, None))
        result['owner_pid_namespace'], result['owner_mount_namespace'] = owner_ns
        child = db.execute('SELECT pid,boot,start_ticks FROM child_identity WHERE id=1').fetchone()
        result['child'] = dict(zip(('pid','boot','start_ticks'), child)) if child else None
        if result['child'] is not None:
            child_ns = namespaces.get('child', (None, None))
            result['child']['pid_namespace'], result['child']['mount_namespace'] = child_ns
        result['events'] = [dict(zip(('seq','at','state','detail'), item)) for item in db.execute('SELECT seq,at,state,detail FROM events ORDER BY seq')]
        return result
    finally:
        db.close()
