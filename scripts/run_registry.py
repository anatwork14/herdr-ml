"""Transactional per-run ownership for a single trusted Linux host.

SQLite is authoritative; JSON files written by the runner are projections.
An active claim is never reclaimed here, even when its owner appears dead:
without verified containment, descendants may still be running.

Every mutating call presents a fencing token (v2.<attempt>.<fence>) and must
match the current owner, attempt and fence in the same BEGIN IMMEDIATE
transaction. Vocabulary, state names and proof shapes live in run_ownership.py.
The cross-database protocol between the shared queue index and the run-local
registry is described in reports/experiment-infra/RUN_OWNERSHIP.md.
"""
import json
import stat
import os
from pathlib import Path
import pwd
import socket
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_ownership  # noqa: E402
from run_ownership import RegistryError  # noqa: E402  (re-exported for callers)

RUN_TABLE = '''CREATE TABLE IF NOT EXISTS run (
        id INTEGER PRIMARY KEY CHECK (id = 1), run_id TEXT NOT NULL,
        project TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        state TEXT NOT NULL, owner TEXT NOT NULL, owner_pid INTEGER NOT NULL,
        owner_host TEXT NOT NULL, owner_boot TEXT, owner_start TEXT,
        queue_task_id TEXT, result_json TEXT,
        attempt_id TEXT, fence INTEGER, worker_id TEXT,
        identity_version INTEGER, identity_sha256 TEXT)'''

QUEUE_TABLE = '''CREATE TABLE IF NOT EXISTS queue_binding (
            run_id TEXT PRIMARY KEY, project TEXT NOT NULL, queue_instance TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL, intent_token TEXT NOT NULL UNIQUE,
            phase TEXT NOT NULL CHECK (phase IN ('INTENT','BOUND','CLAIMED','AMBIGUOUS','RETRYABLE')),
            task_id TEXT, worker_id TEXT, attempt_id TEXT, fence INTEGER NOT NULL DEFAULT 0,
            claimant TEXT, ambiguous_from TEXT, resolution TEXT, run_folder TEXT NOT NULL UNIQUE,
            UNIQUE(queue_instance, task_id))'''

QUEUE_EVENTS = '''CREATE TABLE IF NOT EXISTS queue_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            run_id TEXT NOT NULL, phase TEXT NOT NULL, detail TEXT)'''

INSPECT_FIELDS = ('run_id', 'project', 'manifest_sha256', 'state', 'owner_pid', 'owner_host',
                  'owner_boot', 'owner_start', 'queue_task_id', 'attempt_id', 'fence',
                  'worker_id', 'identity_version', 'identity_sha256')


REQUIRED_TABLES = ('run', 'child_identity', 'process_namespaces', 'events')


def _schema_tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _user_tables(db):
    return {name for name in _schema_tables(db) if not name.startswith('sqlite_')}


def _unreadable(detail='could not be read or written'):
    return RegistryError('REGISTRY_UNREADABLE: registry.sqlite3 %s; not reset' % detail)


def _close_quietly(db):
    if db is not None:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _connect(folder, create=True, *, _fault=None):
    """Open the run-local registry for writing. Mutators pass create=False and never create a file.

    A new store gets its schema in one BEGIN IMMEDIATE transaction, so an interrupted
    creation commits nothing. DDL runs only on a file with no user tables; an existing
    file gets no DDL, so a refused call leaves its bytes unchanged. An incomplete schema
    is refused before any write, not repaired. Any sqlite3
    error while opening or reading the store becomes RegistryError REGISTRY_UNREADABLE,
    and the file is left as it was.
    """
    path = Path(folder) / 'registry.sqlite3'
    if not create and not path.is_file():
        raise RegistryError('No registry record; refusing to create one on a mutation')
    db = None
    try:
        db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA synchronous=FULL')
        if 'run' not in _schema_tables(db):
            if not create:
                raise RegistryError('No run record in registry; refusing to create one on a mutation')
            if _user_tables(db):
                # A partial store is refused before any write; DDL only ever runs on an empty file.
                raise _unreadable('has an incomplete schema (missing run)')
            db.execute('BEGIN IMMEDIATE')
            try:
                user_tables = _user_tables(db)  # re-check under the write lock
                if user_tables and 'run' not in user_tables:
                    raise _unreadable('has an incomplete schema (missing run)')
                if not user_tables:
                    db.execute(RUN_TABLE)
                    run_ownership.fault(_fault, 'schema:after_run_table')
                    db.execute('''CREATE TABLE IF NOT EXISTS child_identity (
        id INTEGER PRIMARY KEY CHECK (id=1), pid INTEGER NOT NULL,
        boot TEXT, start_ticks TEXT)''')
                    db.execute('''CREATE TABLE IF NOT EXISTS process_namespaces (
        role TEXT PRIMARY KEY CHECK (role IN ('owner','child')),
        pid_namespace TEXT, mount_namespace TEXT)''')
                    db.execute('''CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        owner TEXT, state TEXT NOT NULL, detail TEXT)''')
                db.execute('COMMIT')
            except BaseException:
                _rollback(db)
                raise
        missing = [name for name in REQUIRED_TABLES if name not in _schema_tables(db)]
        if missing:
            raise _unreadable('has an incomplete schema (missing %s)' % ', '.join(missing))
    except sqlite3.Error as exc:
        _close_quietly(db)
        raise _unreadable() from exc
    except BaseException:
        _close_quietly(db)
        raise
    return db


def _open_readonly(folder):
    """Inspection never creates tables, migrates schema or writes to the registry."""
    path = (Path(folder) / 'registry.sqlite3').resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30, isolation_level=None)
    db.execute('PRAGMA busy_timeout=30000')
    return db


def _dict_rows(db, sql, params=()):
    cursor = db.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _optional_rows(db, sql):
    """Rows from a table that legacy schemas may lack; a missing table is simply empty."""
    try:
        return _dict_rows(db, sql)
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return []
        raise


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


def _rollback(db):
    if db.in_transaction:
        db.execute('ROLLBACK')


def _safe_rollback(db):
    try:
        _rollback(db)
    except sqlite3.Error:
        pass


def _rollback_or_unreadable(db):
    """Roll back on an error path. A failed ROLLBACK becomes REGISTRY_UNREADABLE, never a raw sqlite3 error."""
    try:
        _rollback(db)
    except sqlite3.Error as exc:
        raise _unreadable() from exc


def _decode_owner(token):
    """Every mutating call starts here; malformed or replayed tokens fail closed."""
    return run_ownership.decode_token(token)


def _queue_connect(folder, store_home=None):
    """Use one owner-private index shared by every run for this user/queue.

    store_home is an explicit test-only parameter; production callers leave it
    None so the index always lives under the effective account's home.
    """
    folder = Path(folder).resolve()
    if folder.parent.name != 'runs' or folder.parents[2].name != '.runtime':
        raise RegistryError('Queue binding requires a standard run folder')
    if store_home is None:
        try:
            account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
        except (KeyError, OSError) as exc:
            raise RegistryError('Cannot determine canonical home for queue registry') from exc
    else:
        account_home = Path(store_home).resolve(strict=True)
    home_info = account_home.stat()
    if (not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.geteuid() or
            home_info.st_mode & 0o022):
        raise RegistryError('Canonical account home is not owner-controlled')
    runtime = account_home
    try:
        for component in ('.local', 'state', 'herdr-ml'):
            parent = runtime
            runtime = runtime / component
            created = False
            try:
                runtime.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
            info = runtime.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                    info.st_mode & 0o022):
                raise RegistryError('Queue store path must contain owner-controlled, non-writable directories')
            if component == 'herdr-ml' and info.st_mode & 0o077:
                raise RegistryError('Queue registry directory must have mode 0700')
            if created:
                parent_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
    except OSError as exc:
        raise RegistryError('Shared per-user queue store directory is unavailable') from exc
    path = runtime / 'queue-registry.sqlite3'
    if not hasattr(os, 'O_NOFOLLOW'):
        raise RegistryError('Queue registry requires O_NOFOLLOW support')
    dir_fd = os.open(str(runtime), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = False
    file_fd = None
    db = None
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        try:
            file_fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            file_fd = os.open(str(path), flags)
        file_info = os.fstat(file_fd)
        if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid() or
                stat.S_IMODE(file_info.st_mode) != 0o600):
            raise RegistryError('Shared queue registry must be a mode-0600 owner-controlled regular file')
        if created:
            os.fsync(file_fd)
            os.fsync(dir_fd)
        db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        opened_info = path.stat()
        if (opened_info.st_dev, opened_info.st_ino) != (file_info.st_dev, file_info.st_ino):
            db.close()
            db = None
            raise RegistryError('Shared queue registry path changed while opening')
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA busy_timeout=30000')
            columns = {row[1] for row in db.execute('PRAGMA table_info(queue_binding)')}
            if columns and 'claimant' not in columns:
                # Pre-fencing queue rows have no project or claimant. Never reinterpret them.
                raise RegistryError('Legacy queue index schema; fail closed until migrated')
            db.execute(QUEUE_TABLE)
            db.execute(QUEUE_EVENTS)
        except sqlite3.DatabaseError as exc:
            raise RegistryError('Queue index unreadable or corrupt; fail closed') from exc
        os.fsync(file_fd)
        os.fsync(dir_fd)
        os.close(file_fd)
        file_fd = None
        result = db
        db = None
        return result
    except BaseException:
        if db is not None:
            db.close()
        if file_fd is not None:
            os.close(file_fd)
        raise
    finally:
        os.close(dir_fd)


def _valid_queue_instance(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(c in '0123456789abcdef' for c in value))


def _validate_queue_args(run_id, project, manifest_sha256, queue_instance):
    run_ownership.validate_name('run_id', run_id)
    run_ownership.validate_name('project', project)
    run_ownership.validate_digest(manifest_sha256)
    if not _valid_queue_instance(queue_instance):
        raise RegistryError('Invalid queue instance identity')


def _folder_key(folder):
    return str(Path(folder).resolve())


def _folder_project(folder):
    """Queue scope: the project component of <root>/.runtime/<project>/runs/<run_id>."""
    path = Path(folder).resolve()
    if len(path.parents) < 3 or path.parent.name != 'runs' or path.parents[2].name != '.runtime':
        raise RegistryError('Queue binding requires a standard run folder')
    return path.parents[1].name


def _queue_event(db, run_id, phase, detail):
    db.execute('INSERT INTO queue_events(run_id,phase,detail) VALUES (?,?,?)',
               (run_id, phase, json.dumps(detail, sort_keys=True)))


def _this_claimant():
    """Observation of this process only; used to refuse continuation by another process."""
    boot, start = _identity()
    if not boot or not start:
        return None
    return '%d:%s:%s' % (os.getpid(), boot, start)


def _mark_ambiguous(db, run_id, origin, reason):
    updated = db.execute("UPDATE queue_binding SET phase='AMBIGUOUS', ambiguous_from=? "
                         "WHERE run_id=? AND phase=?", (origin, run_id, origin)).rowcount
    if updated != 1:
        raise RegistryError('Queue row changed during fencing; rerun recovery')
    _queue_event(db, run_id, 'AMBIGUOUS', {'origin': origin, 'reason': reason})


def _local_claim_matches(folder, run_id, attempt_id, fence):
    """Read-only check that the run-local owner record exists and is CURRENT."""
    if not (Path(folder) / 'registry.sqlite3').is_file():
        return False
    try:
        db = _open_readonly(folder)
        try:
            rows = _optional_rows(db, 'SELECT run_id, project, manifest_sha256, worker_id, queue_task_id, '
                                      'attempt_id, fence, identity_version, identity_sha256 FROM run WHERE id=1')
        finally:
            db.close()
    except sqlite3.Error:
        return False
    if not rows:
        return False
    row = rows[0]
    return (row['run_id'] == run_id and row['attempt_id'] == attempt_id and
            row['fence'] == fence and run_ownership.classify_row(row) == 'CURRENT')


def queue_intent(folder, run_id, manifest_sha256, queue_instance, *, project=None,
                 _store_home=None, _fault=None):
    """Durably record intent BEFORE any external enqueue. An intent is never reissued.

    The queue scope project is the run folder's project component. An explicit
    project must agree with it, and a run_id already bound to another project
    is refused.
    """
    folder_project = _folder_project(folder)
    if project is not None and project != folder_project:
        raise RegistryError('Run folder belongs to a different project')
    _validate_queue_args(run_id, folder_project, manifest_sha256, queue_instance)
    project = folder_project
    token = uuid.uuid4().hex
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        other = db.execute('SELECT project FROM queue_binding WHERE run_id=?', (run_id,)).fetchone()
        if other is not None and other[0] != project:
            raise RegistryError('Run identity is already bound to another project')
        db.execute("INSERT INTO queue_binding (run_id, project, queue_instance, manifest_sha256, "
                   "intent_token, phase, run_folder) VALUES (?,?,?,?,?,'INTENT',?)",
                   (run_id, project, queue_instance, manifest_sha256, token, _folder_key(folder)))
        _queue_event(db, run_id, 'INTENT', {'intent_token': token, 'project': project})
        run_ownership.fault(_fault, 'queue_intent:before_commit')
        db.execute('COMMIT')
    except sqlite3.IntegrityError as exc:
        _rollback_or_unreadable(db)
        raise RegistryError('Queue intent or queue instance/task binding already exists; '
                            'never retry enqueue automatically') from exc
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()
    run_ownership.fault(_fault, 'queue_intent:after_commit')
    return token


def queue_bind_task(folder, intent_token, task_id, *, _store_home=None, _fault=None):
    """Bind the one queue response to this run. Task IDs are opaque and globally unique."""
    run_ownership.validate_opaque_task(task_id)
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT run_id FROM queue_binding WHERE intent_token=? AND run_folder=? "
                         "AND phase='INTENT'", (intent_token, _folder_key(folder))).fetchone()
        if row is None:
            raise RegistryError('Stale intent or queue response already resolved')
        run_id = row[0]
        db.execute("UPDATE queue_binding SET phase='BOUND', task_id=? WHERE run_id=? AND phase='INTENT'",
                   (task_id, run_id))
        _queue_event(db, run_id, 'BOUND', {'task_id': task_id})
        run_ownership.fault(_fault, 'queue_bind_task:before_commit')
        db.execute('COMMIT')
    except sqlite3.IntegrityError as exc:
        _rollback_or_unreadable(db)
        raise RegistryError('Queue task ID is already bound to another run') from exc
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def queue_mark_ambiguous(folder, intent_token, detail='', *, _store_home=None):
    """Fence an uncertain enqueue response. Such an intent is never rebound or retried."""
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT run_id FROM queue_binding WHERE intent_token=? AND run_folder=? "
                         "AND phase='INTENT'", (intent_token, _folder_key(folder))).fetchone()
        if row is None:
            raise RegistryError('Stale intent or queue response already resolved')
        _mark_ambiguous(db, row[0], 'INTENT', str(detail)[:1000])
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


ENQUEUE_EVENTS = ('ENQUEUE_ATTEMPTED', 'ENQUEUED', 'ENQUEUE_REFUSED', 'ENQUEUE_UNKNOWN')


def _enqueue_pending(db, run_id):
    """True when the last enqueue event for run_id is an attempt with no recorded outcome."""
    last = db.execute('SELECT phase FROM queue_events WHERE run_id=? AND phase IN (?,?,?,?) '
                      'ORDER BY seq DESC LIMIT 1', (run_id, *ENQUEUE_EVENTS)).fetchone()
    return last is not None and last[0] == 'ENQUEUE_ATTEMPTED'


def queue_enqueue_attempted(folder, intent_token, *, _store_home=None):
    """Durably record, BEFORE `pueue enqueue`, that a BOUND task is about to be released.

    Until an outcome is recorded, restart recovery treats the row as AMBIGUOUS.
    """
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT run_id, task_id FROM queue_binding WHERE intent_token=? AND run_folder=? "
                         "AND phase='BOUND'", (intent_token, _folder_key(folder))).fetchone()
        if row is None:
            raise RegistryError('Only a BOUND task can be released to a worker')
        if _enqueue_pending(db, row[0]):
            raise RegistryError('An earlier enqueue has no recorded outcome; never retried')
        _queue_event(db, row[0], 'ENQUEUE_ATTEMPTED', {'intent_token': intent_token, 'task_id': row[1]})
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def queue_enqueue_outcome(folder, intent_token, outcome, detail='', *, _store_home=None):
    """Record the outcome of the one enqueue call. Returns the resulting queue phase.

    outcome is ENQUEUED, ENQUEUE_REFUSED (daemon answered with an error) or
    ENQUEUE_UNKNOWN (timeout, OS error, lost response). An unknown outcome on a
    BOUND row fences it AMBIGUOUS through the normal fencing path; it is never retried.
    """
    if outcome not in ENQUEUE_EVENTS[1:]:
        raise RegistryError('Unknown enqueue outcome %r' % outcome)
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT run_id, phase FROM queue_binding WHERE intent_token=? AND run_folder=?',
                         (intent_token, _folder_key(folder))).fetchone()
        if row is None or not _enqueue_pending(db, row[0]):
            raise RegistryError('No pending enqueue attempt for this intent')
        run_id, phase = row
        _queue_event(db, run_id, outcome, {'intent_token': intent_token, 'detail': str(detail)[:1000]})
        if outcome == 'ENQUEUE_UNKNOWN' and phase == 'BOUND':
            _mark_ambiguous(db, run_id, 'BOUND', 'enqueue outcome unknown: %s' % str(detail)[:900])
        phase = db.execute('SELECT phase FROM queue_binding WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('COMMIT')
        return phase
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def queue_claim(folder, run_id, manifest_sha256, queue_instance, task_id, worker_id, *,
                _store_home=None, _fault=None):
    """Claim a bound task once. Issues a new attempt and a monotonic fence; never steals.

    Returns the fence. The attempt is read back from the queue row by the local
    claim, which verifies it under the queue write lock.
    """
    _validate_queue_args(run_id, _folder_project(folder), manifest_sha256, queue_instance)
    run_ownership.validate_opaque_task(task_id)
    run_ownership.validate_name('worker_id', worker_id)
    claimant = _this_claimant()
    if claimant is None:
        raise RegistryError('Process identity unavailable; cannot bind queue claimant')
    attempt_id = run_ownership.new_attempt_id()
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute(
            "UPDATE queue_binding SET phase='CLAIMED', worker_id=?, attempt_id=?, claimant=?, fence=fence+1 "
            "WHERE run_id=? AND manifest_sha256=? AND queue_instance=? AND task_id=? "
            "AND phase='BOUND' AND run_folder=?",
            (worker_id, attempt_id, claimant, run_id, manifest_sha256, queue_instance,
             task_id, _folder_key(folder))).rowcount
        if updated != 1:
            raise RegistryError('Queue task/run/manifest/project mismatch, stale replay, or ambiguous enqueue')
        fence = db.execute('SELECT fence FROM queue_binding WHERE run_id=?', (run_id,)).fetchone()[0]
        _queue_event(db, run_id, 'CLAIMED', {'task_id': task_id, 'worker_id': worker_id,
                                             'attempt_id': attempt_id, 'fence': fence})
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()
    run_ownership.fault(_fault, 'queue_claim:after_commit')
    return fence


def queue_claim_idempotent(folder, run_id, manifest_sha256, queue_instance, task_id, worker_id, *,
                           _store_home=None):
    """Claim a bound task, or return the existing claim when this same claimant repeats it.

    Returns (attempt_id, fence, replayed). A repeat by the same claimant process with
    identical run, manifest, queue instance, task and worker reads the CLAIMED row and
    writes nothing, so the attempt and fence never change. Any other caller, any field
    mismatch, and every non-BOUND/non-CLAIMED phase (INTENT, AMBIGUOUS, RETRYABLE) is
    refused. queue_claim keeps its single-shot behaviour for existing callers.
    """
    _validate_queue_args(run_id, _folder_project(folder), manifest_sha256, queue_instance)
    run_ownership.validate_opaque_task(task_id)
    run_ownership.validate_name('worker_id', worker_id)
    claimant = _this_claimant()
    if claimant is None:
        raise RegistryError('Process identity unavailable; cannot bind queue claimant')
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT run_id, phase, manifest_sha256, queue_instance, task_id, worker_id, '
                         'attempt_id, fence, claimant FROM queue_binding WHERE run_folder=?',
                         (_folder_key(folder),)).fetchone()
        if (row is None or row[0] != run_id or row[2] != manifest_sha256 or
                row[3] != queue_instance or row[4] != task_id):
            raise RegistryError('Queue task/run/manifest/project mismatch, stale replay, or ambiguous enqueue')
        phase = row[1]
        if phase == 'CLAIMED':
            if row[5] != worker_id or row[8] != claimant:
                raise RegistryError('Task already claimed by another worker or process; never stolen')
            db.execute('COMMIT')
            return row[6], row[7], True
        if phase != 'BOUND':
            raise RegistryError('Queue row is %s; only a BOUND task can be claimed' % phase)
        attempt_id = run_ownership.new_attempt_id()
        updated = db.execute(
            "UPDATE queue_binding SET phase='CLAIMED', worker_id=?, attempt_id=?, claimant=?, fence=fence+1 "
            "WHERE run_id=? AND phase='BOUND' AND run_folder=?",
            (worker_id, attempt_id, claimant, run_id, _folder_key(folder))).rowcount
        if updated != 1:
            raise RegistryError('Queue row changed during claim')
        fence = db.execute('SELECT fence FROM queue_binding WHERE run_id=?', (run_id,)).fetchone()[0]
        _queue_event(db, run_id, 'CLAIMED', {'task_id': task_id, 'worker_id': worker_id,
                                             'attempt_id': attempt_id, 'fence': fence})
        db.execute('COMMIT')
        return attempt_id, fence, False
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def queue_recover(folder, *, _store_home=None):
    """Restart recovery. Fences every intent without binding, every enqueue attempt without a
    recorded outcome, and every claim without local ownership.

    Nothing here retries, resubmits or resumes work. Returns the resulting phase, or None.
    """
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT run_id, phase, attempt_id, fence FROM queue_binding WHERE run_folder=?',
                         (_folder_key(folder),)).fetchone()
        if row is None:
            db.execute('COMMIT')
            return None
        run_id, phase, attempt_id, fence = row
        if phase == 'INTENT':
            _mark_ambiguous(db, run_id, 'INTENT', 'restart: intent without verified binding')
        elif phase == 'BOUND' and _enqueue_pending(db, run_id):
            _mark_ambiguous(db, run_id, 'BOUND', 'restart: enqueue attempted without recorded outcome')
        elif phase == 'CLAIMED' and not _local_claim_matches(folder, run_id, attempt_id, fence):
            _mark_ambiguous(db, run_id, 'CLAIMED', 'restart: claim without local ownership record')
        phase = db.execute('SELECT phase FROM queue_binding WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('COMMIT')
        return phase
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def queue_resolve_ambiguous(folder, proof, *, _store_home=None):
    """Move an AMBIGUOUS intent only on a VerifiedProof of queue lookup. Absence alone never qualifies."""
    db = _queue_connect(folder, _store_home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT run_id, intent_token, queue_instance, ambiguous_from FROM queue_binding "
                         "WHERE run_folder=? AND phase='AMBIGUOUS'", (_folder_key(folder),)).fetchone()
        if row is None:
            raise RegistryError('Only AMBIGUOUS queue rows can be resolved')
        run_id, intent_token, queue_instance, origin = row
        if origin != 'INTENT':
            # CLAIMED needs containment-verified termination; BOUND (enqueue outcome unknown) has no verifier.
            raise RegistryError('Only an INTENT ambiguity can be resolved by queue evidence; a claimed or '
                                'released ambiguity needs containment-verified termination')
        kind = run_ownership.require_queue_lookup(proof, intent_token=intent_token, queue_instance=queue_instance)
        if kind == run_ownership.QUEUE_LOOKUP_NO_TASK:
            db.execute("UPDATE queue_binding SET phase='RETRYABLE', resolution='NOT_SUBMITTED_VERIFIED' "
                       "WHERE run_id=? AND phase='AMBIGUOUS'", (run_id,))
            new_phase = 'RETRYABLE'
        else:
            task = proof.binding('task_id')
            db.execute("UPDATE queue_binding SET phase='BOUND', task_id=?, resolution='TASK_VERIFIED' "
                       "WHERE run_id=? AND phase='AMBIGUOUS'", (task, run_id))
            new_phase = 'BOUND'
        _queue_event(db, run_id, new_phase, {'resolved_from': 'AMBIGUOUS', 'kind': kind})
        db.execute('COMMIT')
        return new_phase
    except sqlite3.IntegrityError as exc:
        _rollback_or_unreadable(db)
        raise RegistryError('Queue task ID is already bound to another run') from exc
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def inspect_queue(folder, *, _store_home=None):
    """Return queue lifecycle state even before an execution row exists."""
    db = _queue_connect(folder, _store_home)
    try:
        rows = _dict_rows(db, 'SELECT run_id, project, queue_instance, manifest_sha256, intent_token, phase, '
                              'task_id, worker_id, attempt_id, fence, claimant, ambiguous_from, resolution '
                              'FROM queue_binding WHERE run_folder=?', (_folder_key(folder),))
    except sqlite3.Error as exc:
        raise _unreadable() from exc
    finally:
        db.close()
    return rows[0] if rows else None


def _verify_queued_claim(db, folder, run_id, digest, queue_instance, task_id, worker_id,
                         fence, attempt_id, claimant):
    """Return the queue-issued attempt when every presented field matches the CLAIMED row.

    Project is not compared here: the queue scope is the folder's project, and
    the local claim records its own project argument as the run identity.
    """
    row = db.execute('SELECT run_id, phase, manifest_sha256, queue_instance, task_id, worker_id, '
                     'attempt_id, fence, claimant FROM queue_binding WHERE run_folder=?',
                     (_folder_key(folder),)).fetchone()
    if (row is None or row[1] != 'CLAIMED' or row[0] != run_id or row[2] != digest or
            row[3] != queue_instance or row[4] != task_id or row[5] != worker_id or
            type(fence) is not int or row[7] != fence or
            (attempt_id is not None and row[6] != attempt_id) or row[8] != claimant):
        raise RegistryError('Queued run/task/worker/attempt/fence mismatch, stale replay, '
                            'ambiguous claim, or another claimant')
    return row[6]


def _create_claim_marker(folder):
    try:
        (Path(folder) / 'execution.claim').mkdir(mode=0o700)
    except FileExistsError as exc:
        raise RegistryError('Execution already claimed; reconcile before retry') from exc
    directory = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _insert_local_claim(folder, identity, token, fault_hook):
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM run WHERE id=1').fetchone():
            raise RegistryError('Run already claimed; do not retry without reconciliation')
        boot, start = _identity()
        digest = identity.sha256()
        db.execute('INSERT INTO run (id, run_id, project, manifest_sha256, state, owner, owner_pid, owner_host, '
                   'owner_boot, owner_start, queue_task_id, result_json, attempt_id, fence, worker_id, '
                   'identity_version, identity_sha256) VALUES (1,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)',
                   (identity.run_id, identity.project, identity.manifest_sha256, 'CLAIMED', token,
                    os.getpid(), socket.gethostname(), boot, start, identity.task_id,
                    identity.attempt_id, identity.fence, identity.worker_id,
                    run_ownership.IDENTITY_VERSION, digest))
        pid_ns, mount_ns = _namespaces(os.getpid())
        db.execute("INSERT INTO process_namespaces VALUES ('owner',?,?)", (pid_ns, mount_ns))
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)',
                   (token, 'CLAIMED', json.dumps({'attempt_id': identity.attempt_id,
                                                  'fence': identity.fence,
                                                  'identity_sha256': digest}, sort_keys=True)))
        run_ownership.fault(fault_hook, 'claim:before_commit')
        db.execute('COMMIT')
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def claim(folder, run_id, project, digest, initial_state, *,
          queue_instance=None, queue_task_id=None, queue_worker_id=None,
          queue_attempt_id=None, queue_fence=None, _store_home=None, _fault=None):
    """Atomically create a run and claim it exactly once, never stealing owners.

    A queued claim verifies the queue row under the queue write lock, adopts the
    attempt the queue issued, and holds that lock across the local commit, so
    recovery cannot fence the row between the two writes. Any failure after the
    queue row was verified fences it.
    """
    if initial_state not in ('SUBMITTED', 'QUEUED'):
        raise RegistryError('Run is not eligible for execution')
    if project != _folder_project(folder):
        raise RegistryError('Run project does not match the run folder project')
    queue_db = None
    try:
        if initial_state == 'QUEUED':
            if not all((queue_instance, queue_task_id, queue_worker_id, queue_fence)):
                raise RegistryError('Queued worker requires queue instance, task, worker, and fence')
            if not _valid_queue_instance(queue_instance):
                raise RegistryError('Invalid queue instance identity')
            claimant = _this_claimant()
            if claimant is None:
                raise RegistryError('Process identity unavailable; cannot verify queue claimant')
            queue_db = _queue_connect(folder, _store_home)
            queue_db.execute('BEGIN IMMEDIATE')
            attempt_id = _verify_queued_claim(queue_db, folder, run_id, digest, queue_instance,
                                              queue_task_id, queue_worker_id, queue_fence,
                                              queue_attempt_id, claimant)
            worker_id, fence = queue_worker_id, queue_fence
        else:
            if any((queue_instance, queue_task_id, queue_worker_id, queue_attempt_id, queue_fence)):
                raise RegistryError('Local execution cannot carry queue identity')
            worker_id, attempt_id, fence = 'local-%d' % os.getpid(), run_ownership.new_attempt_id(), 1
        identity = run_ownership.ExecutionIdentity(run_id, project, attempt_id, digest, worker_id,
                                                   fence, queue_task_id).validate()
        token = run_ownership.encode_token(attempt_id, fence)
        _create_claim_marker(folder)
        try:
            _insert_local_claim(folder, identity, token, _fault)
        except BaseException:
            if queue_db is not None:
                queue_db.execute("UPDATE queue_binding SET phase='AMBIGUOUS', ambiguous_from='CLAIMED' "
                                 "WHERE run_id=? AND phase='CLAIMED' AND attempt_id=? AND fence=?",
                                 (run_id, attempt_id, fence))
                _queue_event(queue_db, run_id, 'AMBIGUOUS',
                             {'origin': 'CLAIMED', 'reason': 'local claim failed after queue claim'})
                queue_db.execute('COMMIT')
            raise
        if queue_db is not None:
            queue_db.execute('COMMIT')
    except sqlite3.Error as exc:
        raise _unreadable() from exc
    finally:
        if queue_db is not None:
            _safe_rollback(queue_db)
            _close_quietly(queue_db)
    run_ownership.fault(_fault, 'claim:after_commit')
    return token


def _owner_row(db, token, attempt_id, fence):
    """Read the run row inside the caller's transaction and verify it before any mutation.

    Owner, attempt and fence must match the presented token, and the row must
    recompute as CURRENT. Legacy-schema rows are read with SELECT *, so every
    refusal here is a RegistryError rather than an OperationalError on a missing column.
    """
    rows = _dict_rows(db, 'SELECT * FROM run WHERE id=1')
    if (not rows or rows[0].get('owner') != token or rows[0].get('attempt_id') != attempt_id or
            rows[0].get('fence') != fence):
        raise RegistryError('Stale owner or illegal execution state')
    if run_ownership.classify_row(rows[0]) != 'CURRENT':
        raise RegistryError('Run identity fails verification; fenced')
    return rows[0]


def _child_recorded(db):
    return bool(_optional_rows(db, 'SELECT 1 AS present FROM child_identity WHERE id=1'))


def record_child(folder, token, pid):
    """Record forensic identity; this is not evidence of containment."""
    attempt_id, fence = _decode_owner(token)
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stat = Path('/proc/%d/stat' % pid).read_text()
        start = stat[stat.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        boot, start = None, None
    db = _connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        _owner_row(db, token, attempt_id, fence)
        if not db.execute("SELECT 1 FROM run WHERE id=1 AND owner=? AND attempt_id=? AND fence=? "
                          "AND state='DISPATCHING'", (token, attempt_id, fence)).fetchone():
            raise RegistryError('Stale owner or illegal child registration')
        try:
            db.execute('INSERT INTO child_identity VALUES (1,?,?,?)', (pid, boot, start))
        except sqlite3.IntegrityError as exc:
            raise RegistryError('Child identity already recorded') from exc
        pid_ns, mount_ns = _namespaces(pid)
        db.execute("INSERT INTO process_namespaces VALUES ('child',?,?)", (pid_ns, mount_ns))
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)',
                   (token, 'CHILD_IDENTIFIED', json.dumps({'pid': pid, 'boot': boot, 'start_ticks': start})))
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def advance(folder, token, expected, target, *, _fault=None):
    """Owner-only transition. The state guard is the CAS in the UPDATE; RUNNING also needs a child identity."""
    if (expected, target) not in {('CLAIMED', 'DISPATCHING'), ('DISPATCHING', 'RUNNING')}:
        raise RegistryError('Illegal execution transition')
    attempt_id, fence = _decode_owner(token)
    db = _connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        _owner_row(db, token, attempt_id, fence)
        if target == 'RUNNING' and not _child_recorded(db):
            raise RegistryError('RUNNING requires a recorded child identity')
        updated = db.execute('UPDATE run SET state=? WHERE id=1 AND owner=? AND attempt_id=? AND fence=? AND state=?',
                             (target, token, attempt_id, fence, expected)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal execution state')
        db.execute('INSERT INTO events(owner,state) VALUES (?,?)', (token, target))
        run_ownership.fault(_fault, 'advance:before_commit')
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def finish(folder, token, result, *, _fault=None):
    """Record a leader-observed outcome as UNKNOWN. Whole-workload completion is not claimed here."""
    if not isinstance(result, dict) or result.get('state') != 'UNKNOWN':
        raise RegistryError('Invalid terminal state')
    attempt_id, fence = _decode_owner(token)
    data = json.dumps(result, sort_keys=True)
    db = _connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        _owner_row(db, token, attempt_id, fence)
        updated = db.execute("UPDATE run SET state='UNKNOWN', result_json=? WHERE id=1 AND owner=? "
                             "AND attempt_id=? AND fence=? AND state IN ('RUNNING','DISPATCHING')",
                             (data, token, attempt_id, fence)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal terminal state')
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)', (token, 'UNKNOWN', data))
        run_ownership.fault(_fault, 'finish:before_commit')
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def verified_finish(folder, token, proof, result):
    """Record a leader outcome as verified only with a VerifiedProof of whole-workload termination.

    No verifier is registered, so every real call is refused today.
    """
    if not isinstance(result, dict) or result.get('state') not in run_ownership.VERIFIED_OUTCOMES:
        raise RegistryError('Verified completion requires a leader outcome state')
    attempt_id, fence = _decode_owner(token)
    data = json.dumps(result, sort_keys=True)
    db = _connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = _owner_row(db, token, attempt_id, fence)
        run_ownership.require_containment_proof(proof, run_id=row['run_id'], attempt_id=attempt_id, fence=fence)
        updated = db.execute("UPDATE run SET state=?, result_json=? WHERE id=1 AND owner=? AND attempt_id=? "
                             "AND fence=? AND state IN ('RUNNING','DISPATCHING')",
                             (result['state'], data, token, attempt_id, fence)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal terminal state')
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)', (token, result['state'], data))
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def supersede(folder, token, proof):
    """Revoke the presented authority after a VerifiedProof that its whole workload terminated.

    The run moves to FENCED with fence+1 and owner set to the successor token. FENCED
    is terminal for every mutator: each requires a state that FENCED lacks, so the
    derivable successor token cannot act. A retry needs a new run identity.
    """
    attempt_id, fence = _decode_owner(token)
    db = _connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = _owner_row(db, token, attempt_id, fence)
        run_ownership.require_containment_proof(proof, run_id=row['run_id'], attempt_id=attempt_id, fence=fence)
        new_fence = fence + 1
        new_digest = run_ownership.ExecutionIdentity(row['run_id'], row['project'], attempt_id,
                                                     row['manifest_sha256'], row['worker_id'], new_fence,
                                                     row['queue_task_id']).validate().sha256()
        new_token = run_ownership.encode_token(attempt_id, new_fence)
        updated = db.execute("UPDATE run SET state='FENCED', owner=?, fence=?, identity_sha256=? "
                             "WHERE id=1 AND owner=? AND attempt_id=? AND fence=? "
                             "AND state IN ('CLAIMED','DISPATCHING','RUNNING','UNKNOWN')",
                             (new_token, new_fence, new_digest, token, attempt_id, fence)).rowcount
        if updated != 1:
            raise RegistryError('Only unfinished or unknown executions can be fenced')
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)',
                   (new_token, 'FENCED', json.dumps({'previous_fence': fence, 'fence': new_fence},
                                                    sort_keys=True)))
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        _safe_rollback(db)
        raise _unreadable() from exc
    except BaseException:
        _rollback_or_unreadable(db)
        raise
    finally:
        db.close()


def inspect(folder):
    """Read-only view. Rows without a recomputable v2 identity are reported UNKNOWN (LEGACY_FENCED)."""
    if not (Path(folder) / 'registry.sqlite3').is_file():
        return None
    db = None
    try:
        db = _open_readonly(folder)
        rows = _optional_rows(db, 'SELECT * FROM run WHERE id=1')
        if not rows:
            return None
        row = rows[0]
        result = {key: row.get(key) for key in INSPECT_FIELDS}
        result['result'] = json.loads(row['result_json']) if row.get('result_json') is not None else None
        result['identity'] = run_ownership.classify_row(row)
        if result['identity'] != 'CURRENT':
            result['legacy_state'] = result['state']
            result['state'] = 'UNKNOWN'
        namespaces = {r['role']: (r['pid_namespace'], r['mount_namespace']) for r in
                      _optional_rows(db, 'SELECT role,pid_namespace,mount_namespace FROM process_namespaces')}
        owner_ns = namespaces.get('owner', (None, None))
        result['owner_pid_namespace'], result['owner_mount_namespace'] = owner_ns
        child = _optional_rows(db, 'SELECT pid,boot,start_ticks FROM child_identity WHERE id=1')
        result['child'] = child[0] if child else None
        if result['child'] is not None:
            child_ns = namespaces.get('child', (None, None))
            result['child']['pid_namespace'], result['child']['mount_namespace'] = child_ns
        result['events'] = _optional_rows(db, 'SELECT seq,at,state,detail FROM events ORDER BY seq')
        return result
    except sqlite3.Error as exc:
        raise _unreadable() from exc
    finally:
        _close_quietly(db)
