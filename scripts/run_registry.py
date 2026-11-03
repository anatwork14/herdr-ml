"""Transactional per-run ownership for a single trusted Linux host.

SQLite is authoritative; JSON files written by the runner are projections.
An active claim is never reclaimed here, even when its owner appears dead:
without verified containment, descendants may still be running.
"""
import json
import stat
import os
from pathlib import Path
import pwd
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


def _queue_connect(folder):
    """Use one owner-private index shared by every run for this user/queue."""
    folder = Path(folder).resolve()
    if folder.parent.name != 'runs' or folder.parents[2].name != '.runtime':
        raise RegistryError('Queue binding requires a standard run folder')
    try:
        account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise RegistryError('Cannot determine canonical home for queue registry') from exc
    home_info = account_home.stat()
    if (not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.geteuid() or
            home_info.st_mode & 0o022):
        raise RegistryError('Canonical account home is not owner-controlled')
    runtime = account_home
    try:
        for component in ('.local', 'state', 'herdr-ml'):
            runtime = runtime / component
            try:
                runtime.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = runtime.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                    info.st_mode & 0o022):
                raise RegistryError('Queue store path must contain owner-controlled, non-writable directories')
    except OSError as exc:
        raise RegistryError('Shared per-user queue store directory is unavailable') from exc
    path = runtime / 'queue-registry.sqlite3'
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise RegistryError('Cannot inspect shared queue registry') from exc
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()):
        raise RegistryError('Shared queue registry must be an owner-controlled regular file')
    if existing is not None and existing.st_mode & 0o077:
        raise RegistryError('Shared queue registry must not be accessible by group or others')
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    os.chmod(path, 0o600)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('''CREATE TABLE IF NOT EXISTS queue_binding (
        run_id TEXT PRIMARY KEY, queue_instance TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        intent_token TEXT NOT NULL UNIQUE,
        phase TEXT NOT NULL CHECK (phase IN ('INTENT','BOUND','AMBIGUOUS','CLAIMED')),
        task_id TEXT, worker_id TEXT, fence TEXT,
        run_folder TEXT NOT NULL UNIQUE,
        UNIQUE(queue_instance,task_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS queue_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT NOT NULL, phase TEXT NOT NULL, detail TEXT)''')
    return db


def _valid_queue_instance(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(c in '0123456789abcdef' for c in value))


def queue_intent(folder, run_id, manifest_sha256, queue_instance):
    """Durably record intent before external enqueue; never reissue an intent."""
    if (not isinstance(run_id, str) or not run_id or len(run_id) > 255 or
            not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64 or
            any(c not in '0123456789abcdef' for c in manifest_sha256) or
            not _valid_queue_instance(queue_instance)):
        raise RegistryError('Invalid run identity, digest, or queue instance identity')
    token = uuid.uuid4().hex
    db = _queue_connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO queue_binding VALUES (?,?,?, ?,\'INTENT\',NULL,NULL,NULL,?)',
                   (run_id, queue_instance, manifest_sha256, token, str(Path(folder).resolve())))
        db.execute('INSERT INTO queue_events(run_id,phase,detail) VALUES (?,?,?)',
                   (run_id, 'INTENT', json.dumps({'intent_token': token})))
        db.execute('COMMIT')
        return token
    except sqlite3.IntegrityError as exc:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise RegistryError('Queue intent or queue instance/task binding already exists; never retry enqueue automatically') from exc
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
    db = _queue_connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT run_id,phase FROM queue_binding WHERE intent_token=? AND run_folder=?",
                         (intent_token, str(Path(folder).resolve()))).fetchone()
        if row is None or row[1] != 'INTENT':
            raise RegistryError('Stale intent or queue response already resolved')
        run_id = row[0]
        db.execute("UPDATE queue_binding SET phase='BOUND',task_id=? WHERE run_id=?", (task_id, run_id))
        db.execute('INSERT INTO queue_events(run_id,phase,detail) VALUES (?,?,?)',
                   (run_id, 'BOUND', json.dumps({'task_id': task_id})))
        db.execute('COMMIT')
    except sqlite3.IntegrityError as exc:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise RegistryError('Queue task ID is already bound to another run') from exc
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def queue_mark_ambiguous(folder, intent_token, detail=''):
    """Fence an uncertain enqueue response. This state cannot be rebound/retried."""
    db = _queue_connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT run_id FROM queue_binding WHERE intent_token=? AND run_folder=? AND phase='INTENT'",
                         (intent_token, str(Path(folder).resolve()))).fetchone()
        if row is None:
            raise RegistryError('Stale intent or queue response already resolved')
        run_id = row[0]
        db.execute("UPDATE queue_binding SET phase='AMBIGUOUS' WHERE run_id=?", (run_id,))
        db.execute('INSERT INTO queue_events(run_id,phase,detail) VALUES (?,?,?)',
                   (run_id, 'AMBIGUOUS', str(detail)[:1000]))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def queue_claim(folder, run_id, manifest_sha256, queue_instance, task_id, worker_id):
    """Claim a bound task once and return its fencing token; no stale takeover."""
    if not _valid_queue_instance(queue_instance):
        raise RegistryError('Invalid queue instance identity')
    if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 256 or '\x00' in worker_id:
        raise RegistryError('Invalid worker identity')
    db = _queue_connect(folder)
    fence = uuid.uuid4().hex
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute("UPDATE queue_binding SET phase='CLAIMED',worker_id=?,fence=? "
                             "WHERE run_id=? AND manifest_sha256=? AND queue_instance=? AND task_id=? AND phase='BOUND' AND run_folder=?",
                             (worker_id, fence, run_id, manifest_sha256, queue_instance, task_id,
                              str(Path(folder).resolve()))).rowcount
        if updated != 1:
            raise RegistryError('Queue task/run/manifest mismatch, stale replay, or ambiguous enqueue')
        db.execute('INSERT INTO queue_events(run_id,phase,detail) VALUES (?,?,?)',
                   (run_id, 'CLAIMED', json.dumps({'task_id': task_id, 'worker_id': worker_id,
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
    db = _queue_connect(folder)
    try:
        row = db.execute('SELECT run_id,queue_instance,manifest_sha256,intent_token,phase,task_id,worker_id,fence '
                         'FROM queue_binding WHERE run_folder=?',
                         (str(Path(folder).resolve()),)).fetchone()
        if row is None:
            return None
        return dict(zip(('run_id','queue_instance','manifest_sha256','intent_token','phase','task_id','worker_id','fence'), row))
    finally:
        db.close()


def _queue_matches(folder, run_id, digest, queue_instance, task_id, worker_id, fence):
    db = _queue_connect(folder)
    try:
        return db.execute("SELECT 1 FROM queue_binding WHERE run_folder=? AND run_id=? "
                          "AND manifest_sha256=? AND queue_instance=? AND phase='CLAIMED' AND task_id=? "
                          "AND worker_id=? AND fence=?",
                          (str(Path(folder).resolve()), run_id, digest, queue_instance, task_id,
                           worker_id, fence)).fetchone() is not None
    finally:
        db.close()


def claim(folder, run_id, project, digest, initial_state, *,
          queue_instance=None, queue_task_id=None, queue_worker_id=None, queue_fence=None):
    """Atomically create a run and claim it exactly once, never stealing owners."""
    if initial_state not in ('SUBMITTED', 'QUEUED'):
        raise RegistryError('Run is not eligible for execution')
    if initial_state == 'QUEUED' and not all((queue_instance, queue_task_id, queue_worker_id, queue_fence)):
        raise RegistryError('Queued worker requires queue instance, task, worker identity, and fence')
    if initial_state == 'SUBMITTED' and any((queue_instance, queue_task_id, queue_worker_id, queue_fence)):
        raise RegistryError('Local execution cannot carry queue identity')
    if initial_state == 'QUEUED':
        if not _queue_matches(folder, run_id, digest, queue_instance, queue_task_id,
                              queue_worker_id, queue_fence):
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
        if initial_state == 'QUEUED' and not _queue_matches(
                folder, run_id, digest, queue_instance, queue_task_id,
                queue_worker_id, queue_fence):
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
