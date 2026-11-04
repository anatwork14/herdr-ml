"""Phase 5 experiment recovery: fenced orphan and interrupted-launch recovery,
identity-checked CPU process termination and a fail-closed release gate.

What this module does:

- recover() runs after a restart. A run-local row in CLAIMED, DISPATCHING,
  RUNNING or UNKNOWN whose owner process is observed gone is moved to
  RECOVERY_FENCED (fence+1). It is never reset to a claimable or ready state,
  never re-launched and never marked complete. Queue rows still in INTENT or
  BOUND with no local claim are fenced AMBIGUOUS; a CLAIMED row whose local
  owner was fenced becomes AMBIGUOUS (origin CLAIMED). Nothing is resubmitted.
- Descendant identities (pid, boot id, start time from /proc/<pid>/stat, pid
  namespace) are recorded by the current owner token only. Termination signals
  only processes whose CURRENT identity equals a recorded one, through a pidfd
  opened before the identity re-check, so a reused PID is never signalled.
- release_gate() refuses release unless every recorded descendant is verified
  gone AND containment is verified. No production containment verifier exists
  (containment_backend.is_verified_for_execution is always False here), so
  release is always refused on this host and recovery never completes a run.

What this module does NOT do:

- It cannot discover a descendant that was never recorded (no cgroup is used;
  no cgroup.kill; cgroup.procs is not read). A process that detached before it
  was recorded is invisible here. That is why containment remains required.
- A zombie counts as gone: it runs no code and its PID cannot be reused while
  it exists. A process in another PID namespace or on another boot is either
  UNVERIFIED (namespace differs) or GONE (boot differs).
"""
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containment_backend  # noqa: E402
import process_identity  # noqa: E402
import run_ownership  # noqa: E402
import run_registry  # noqa: E402
from run_ownership import RegistryError  # noqa: E402

GONE = 'GONE'
ZOMBIE = 'ZOMBIE'
ALIVE = 'ALIVE'
UNVERIFIED = 'UNVERIFIED'
GONE_STATES = (GONE, ZOMBIE)

REFUSE_NO_DESCENDANTS = 'NO_RECORDED_DESCENDANTS'
REFUSE_DESCENDANT = 'DESCENDANT_NOT_VERIFIED_GONE'
REFUSE_CONTAINMENT = 'CONTAINMENT_'  # suffixed with UNKNOWN or UNAVAILABLE

RECOVERY_TABLES = (
    '''CREATE TABLE IF NOT EXISTS descendants (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL, fence INTEGER NOT NULL,
        pid INTEGER NOT NULL, boot TEXT NOT NULL, start_ticks TEXT NOT NULL,
        pid_namespace TEXT NOT NULL, role TEXT NOT NULL,
        UNIQUE(attempt_id, pid, start_ticks))''',
    '''CREATE TABLE IF NOT EXISTS recovery_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        kind TEXT NOT NULL, detail TEXT)''',
)


# --- process identity -------------------------------------------------------

def _boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()


def _stat_fields(pid):
    stat = Path('/proc/%d/stat' % pid).read_text(encoding='ascii')
    end = stat.rfind(')')
    if end < 0 or not stat.startswith('%d (' % pid):
        raise ValueError('invalid process stat header')
    fields = stat[end + 2:].split()
    return fields[0], fields[19]


def read_identity(pid, role='descendant'):
    """Capture (pid, boot, start_ticks, pid_namespace) for a live PID from /proc now."""
    if type(pid) is not int or pid <= 0:
        raise RegistryError('Invalid PID')
    try:
        _, start = _stat_fields(pid)
        return {'pid': pid, 'boot': _boot_id(), 'start_ticks': start,
                'pid_namespace': os.readlink('/proc/self/ns/pid'), 'role': role}
    except (OSError, ValueError, IndexError) as exc:
        raise RegistryError('Process identity unavailable for PID %d' % pid) from exc


def _identity_valid(identity):
    return (isinstance(identity, dict) and type(identity.get('pid')) is int and identity['pid'] > 0 and
            process_identity._boot_valid(identity.get('boot')) and
            process_identity._ticks_valid(identity.get('start_ticks')) and
            process_identity._namespace_valid(identity.get('pid_namespace'), 'pid'))


def observe(identity):
    """Classify a recorded identity as GONE, ZOMBIE, ALIVE or UNVERIFIED. Sends no signal."""
    if not _identity_valid(identity):
        return UNVERIFIED, 'malformed or incomplete recorded identity'
    try:
        if os.readlink('/proc/self/ns/pid') != identity['pid_namespace']:
            return UNVERIFIED, 'observer is in a different PID namespace'
        if _boot_id() != identity['boot']:
            return GONE, 'host rebooted since the identity was recorded'
        try:
            state, start = _stat_fields(identity['pid'])
        except FileNotFoundError:
            return GONE, 'PID not present'
        if start != identity['start_ticks']:
            return GONE, 'PID reused: start time differs from the recorded identity'
        if state in ('Z', 'X', 'x'):
            return ZOMBIE, 'exited; zombie not yet reaped'
        return ALIVE, 'recorded identity matches a running process'
    except (OSError, ValueError, IndexError, UnicodeError):
        return UNVERIFIED, 'cannot read procfs safely'


def _signal_identity(identity, sig):
    """Signal only the process holding the recorded identity. Returns the observation or SIGNALLED.

    The pidfd is opened first and the identity re-checked afterwards, so the pidfd
    refers to the recorded process. If that process dies and the PID is reused
    after the check, pidfd_send_signal fails with ESRCH instead of reaching the
    new process.
    """
    try:
        fd = os.pidfd_open(identity['pid'])
    except ProcessLookupError:
        return GONE
    except OSError:
        return UNVERIFIED
    try:
        state, _ = observe(identity)
        if state != ALIVE:
            return state
        signal.pidfd_send_signal(fd, sig)
        return 'SIGNALLED'
    except ProcessLookupError:
        return GONE
    except OSError:
        return UNVERIFIED
    finally:
        os.close(fd)


def _wait_not_alive(entries, timeout_s):
    deadline = time.monotonic() + timeout_s
    while True:
        alive = [e for e in entries if observe(e['identity'])[0] == ALIVE]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.02)


def terminate_recorded(identities, *, grace_s=0.5, kill_wait_s=5.0, escalate=True):
    """SIGTERM, then (if escalate) SIGKILL, every recorded identity still alive; verify each.

    Never signals by name, pattern or bare PID. Returns a report whose all_gone is
    True only when every recorded identity is observed GONE or ZOMBIE afterwards.
    """
    entries = [{'identity': dict(i), 'signals': []} for i in identities]
    for entry in entries:
        if observe(entry['identity'])[0] == ALIVE:
            entry['signals'].append(('SIGTERM', _signal_identity(entry['identity'], signal.SIGTERM)))
    alive = _wait_not_alive(entries, grace_s)
    if escalate and alive:
        for entry in alive:
            entry['signals'].append(('SIGKILL', _signal_identity(entry['identity'], signal.SIGKILL)))
        _wait_not_alive(alive, kill_wait_s)
    for entry in entries:
        entry['final'], entry['reason'] = observe(entry['identity'])
    return {'all_gone': bool(entries) and all(e['final'] in GONE_STATES for e in entries),
            'entries': entries}


# --- recovery store ---------------------------------------------------------

def _recovery_connect(folder):
    path = Path(folder) / 'recovery.sqlite3'
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
        os.close(fd)
        db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA synchronous=FULL')
        for ddl in RECOVERY_TABLES:
            db.execute(ddl)
        return db
    except (OSError, sqlite3.Error) as exc:
        raise RegistryError('RECOVERY_UNREADABLE: recovery.sqlite3 could not be opened; not reset') from exc


def _event(db, kind, detail):
    db.execute('INSERT INTO recovery_events(kind, detail) VALUES (?,?)',
               (kind, json.dumps(detail, sort_keys=True, default=str)))


def _append_event(folder, kind, detail):
    db = _recovery_connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        _event(db, kind, detail)
        db.execute('COMMIT')
    except sqlite3.Error as exc:
        run_registry._safe_rollback(db)
        raise RegistryError('RECOVERY_UNREADABLE: event not recorded') from exc
    finally:
        db.close()


MAX_LINEAGE_DEPTH = 64


def _proc_lineage(pid):
    """(state, ppid, session, start_ticks) of a PID now, from /proc/<pid>/stat."""
    stat = Path('/proc/%d/stat' % pid).read_text(encoding='ascii')
    end = stat.rfind(')')
    if end < 0 or not stat.startswith('%d (' % pid):
        raise ValueError('invalid process stat header')
    fields = stat[end + 2:].split()
    return fields[0], int(fields[1]), int(fields[3]), fields[19]


def _lineage_refusal(leader, identity):
    """None when identity is the recorded leader, or a current process whose parent chain reaches
    the leader or, when the leader is a session leader (session id == its pid), whose session id
    equals the leader's. Otherwise the refusal reason.

    The leader must still be observable (running or an unreaped zombie) with its
    recorded start time; each presented identity must match the live process now.
    """
    if observe(leader)[0] not in (ALIVE, ZOMBIE):
        return 'recorded leader is not observable, so lineage cannot be verified'
    try:
        _, _, leader_session, leader_start = _proc_lineage(leader['pid'])
        if leader_start != leader['start_ticks']:
            return 'recorded leader PID was reused'
        if (identity['pid'], identity['start_ticks']) == (leader['pid'], leader['start_ticks']):
            return None
        if observe(identity)[0] not in (ALIVE, ZOMBIE):
            return 'PID %d does not match a current process' % identity['pid']
        _, ppid, session, start = _proc_lineage(identity['pid'])
        if start != identity['start_ticks']:
            return 'PID %d changed identity during verification' % identity['pid']
        # The session clause applies only when the leader leads its own session; otherwise the
        # session is shared with the leader's parent and siblings, so only the parent chain counts.
        if leader_session == leader['pid'] and session == leader_session:
            return None
        pid = ppid
        for _ in range(MAX_LINEAGE_DEPTH):
            if pid <= 1:
                break
            _, parent, _, pid_start = _proc_lineage(pid)
            if pid == leader['pid'] and pid_start == leader['start_ticks']:
                return None
            pid = parent
    except (OSError, ValueError, IndexError, UnicodeError):
        return 'cannot read procfs safely for PID %d' % identity['pid']
    return "PID %d is neither in the leader's session nor below it in the parent chain" % identity['pid']


def record_descendants(folder, token, identities, *, _fault=None):
    """Record descendant identities under the current owner token (DISPATCHING or RUNNING).

    Each identity must belong to the recorded leader: be the leader itself, have
    a parent chain reaching the leader, or share the leader's session id. A session
    match counts only when the leader leads its own session (its pid equals its session
    id); otherwise the session id is shared with unrelated processes and proves nothing
    (checked against /proc now, under the registry write lock). One outsider
    refuses the whole call and nothing is recorded, so recovery never signals it.

    The registry write lock is held across the recovery-store commit, so recovery
    fencing (which takes the same lock) cannot interleave: once a row is fenced,
    no stale worker can add a descendant.
    """
    identities = [dict(i) for i in identities]
    if not identities or not all(_identity_valid(i) for i in identities):
        raise RegistryError('Descendant identities must be complete (pid, boot, start_ticks, pid_namespace)')
    attempt_id, fence = run_registry._decode_owner(token)
    db = run_registry._connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = run_registry._owner_row(db, token, attempt_id, fence)
        if row['state'] not in ('DISPATCHING', 'RUNNING'):
            raise RegistryError('Descendants can be recorded only while DISPATCHING or RUNNING')
        child = run_registry._optional_rows(db, 'SELECT pid, boot, start_ticks FROM child_identity WHERE id=1')
        namespaces = run_registry._optional_rows(db, "SELECT pid_namespace FROM process_namespaces "
                                                     "WHERE role='child'")
        if not child or not namespaces:
            raise RegistryError('Descendants require a recorded leader identity')
        leader = dict(child[0], pid_namespace=namespaces[0]['pid_namespace'])
        for identity in identities:
            refusal = _lineage_refusal(leader, identity)
            if refusal is not None:
                raise RegistryError('Descendant lineage not verified: %s' % refusal)
        rdb = _recovery_connect(folder)
        try:
            rdb.execute('BEGIN IMMEDIATE')
            for identity in identities:
                rdb.execute('INSERT OR IGNORE INTO descendants(attempt_id, fence, pid, boot, start_ticks, '
                            'pid_namespace, role) VALUES (?,?,?,?,?,?,?)',
                            (attempt_id, fence, identity['pid'], identity['boot'], identity['start_ticks'],
                             identity['pid_namespace'], str(identity.get('role', 'descendant'))[:64]))
            _event(rdb, 'DESCENDANTS_RECORDED', {'attempt_id': attempt_id, 'fence': fence,
                                                 'pids': [i['pid'] for i in identities]})
            rdb.execute('COMMIT')
        except BaseException:
            run_registry._safe_rollback(rdb)
            raise
        finally:
            rdb.close()
        db.execute('ROLLBACK')
    except sqlite3.Error as exc:
        run_registry._safe_rollback(db)
        raise RegistryError('REGISTRY_UNREADABLE: descendants not recorded') from exc
    except BaseException:
        run_registry._safe_rollback(db)
        raise
    finally:
        db.close()
    run_ownership.fault(_fault, 'descendants:after_commit')


def recorded_identities(folder):
    """The leader child from the run registry plus every descendant recorded for this attempt."""
    entry = run_registry.inspect(folder)
    if entry is None:
        return []
    found = []
    child = entry.get('child')
    if child is not None:
        found.append({'pid': child.get('pid'), 'boot': child.get('boot'), 'start_ticks': child.get('start_ticks'),
                      'pid_namespace': child.get('pid_namespace'), 'role': 'leader'})
    if (Path(folder) / 'recovery.sqlite3').is_file():
        db = _recovery_connect(folder)
        try:
            rows = db.execute('SELECT pid, boot, start_ticks, pid_namespace, role FROM descendants '
                              'WHERE attempt_id=? ORDER BY seq', (entry.get('attempt_id'),)).fetchall()
        finally:
            db.close()
        found.extend({'pid': r[0], 'boot': r[1], 'start_ticks': r[2], 'pid_namespace': r[3], 'role': r[4]}
                     for r in rows)
    unique, seen = [], set()
    for identity in found:
        key = (identity['pid'], identity['start_ticks'])
        if key not in seen:
            seen.add(key)
            unique.append(identity)
    return unique


def inspect_recovery(folder):
    if not (Path(folder) / 'recovery.sqlite3').is_file():
        return {'descendants': [], 'events': []}
    db = _recovery_connect(folder)
    try:
        descendants = run_registry._dict_rows(db, 'SELECT * FROM descendants ORDER BY seq')
        events = run_registry._dict_rows(db, 'SELECT seq, kind, detail FROM recovery_events ORDER BY seq')
    finally:
        db.close()
    for event in events:
        event['detail'] = json.loads(event['detail']) if event['detail'] else None
    return {'descendants': descendants, 'events': events}


# --- release gate -----------------------------------------------------------

def containment_status(containment):
    """VERIFIED only through the production predicate; everything else is UNKNOWN or UNAVAILABLE."""
    if containment_backend.is_verified_for_execution(containment):
        return 'VERIFIED'
    if (isinstance(containment, containment_backend.ContainmentResult) and
            containment.status is containment_backend.ContainmentStatus.UNAVAILABLE):
        return 'UNAVAILABLE'
    return 'UNKNOWN'


def production_containment():
    """The containment this host can offer a run: the production backend is UNAVAILABLE."""
    backend = containment_backend.get_backend()
    if isinstance(backend, containment_backend.UnavailableContainmentBackend):
        return containment_backend.ContainmentResult(
            status=containment_backend.ContainmentStatus.UNAVAILABLE,
            reason='No production containment backend is configured')
    return None


def release_gate(identities, containment):
    """Return (reasons, observations). Release is allowed only when reasons is empty."""
    observations = []
    for identity in identities:
        state, reason = observe(identity)
        observations.append({'pid': identity.get('pid') if isinstance(identity, dict) else None,
                             'role': identity.get('role') if isinstance(identity, dict) else None,
                             'observation': state, 'reason': reason})
    reasons = []
    if not identities:
        reasons.append(REFUSE_NO_DESCENDANTS)
    if any(o['observation'] not in GONE_STATES for o in observations):
        reasons.append(REFUSE_DESCENDANT)
    status = containment_status(containment)
    if status != 'VERIFIED':
        reasons.append(REFUSE_CONTAINMENT + status)
    return reasons, observations


def release_run(folder, token, *, containment=None, _fault=None):
    """Worker-side release. Requires the current owner token and a recorded leader outcome (UNKNOWN).

    A stale, superseded or recovery-fenced token is refused with RegistryError. The
    gate result is recorded either way. The run state is never changed here: in
    particular, it never becomes COMPLETED.
    """
    run_ownership.fault(_fault, 'release:begin')
    attempt_id, fence = run_registry._decode_owner(token)
    db = run_registry._connect(folder, create=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = run_registry._owner_row(db, token, attempt_id, fence)
        if row['state'] != 'UNKNOWN':
            raise RegistryError('Release requires a recorded leader outcome')
        identities = recorded_identities(folder)
        reasons, observations = release_gate(identities, containment)
        kind = 'RELEASE_REFUSED' if reasons else 'RELEASE_GATE_PASSED'
        _append_event(folder, kind, {'attempt_id': attempt_id, 'fence': fence, 'reasons': reasons,
                                     'observations': observations})
        db.execute('ROLLBACK')
    except sqlite3.Error as exc:
        run_registry._safe_rollback(db)
        raise RegistryError('REGISTRY_UNREADABLE: release not evaluated') from exc
    except BaseException:
        run_registry._safe_rollback(db)
        raise
    finally:
        db.close()
    return {'released': not reasons, 'reasons': reasons, 'observations': observations}


# --- restart recovery -------------------------------------------------------

def _owner_liveness(entry):
    owner = {'pid': entry.get('owner_pid'), 'boot': entry.get('owner_boot'),
             'start_ticks': entry.get('owner_start'), 'pid_namespace': entry.get('owner_pid_namespace'),
             'mount_namespace': entry.get('owner_mount_namespace')}
    seen = process_identity.observe_process(owner, entry.get('owner_host'))
    if seen['observation'] == 'MATCH':
        return 'LIVE', seen
    if seen['observation'] in ('NOT_VISIBLE', 'IDENTITY_MISMATCH', 'MATCH_ZOMBIE'):
        return 'GONE', seen
    return 'UNKNOWN', seen


def recover(folder, *, queued=True, _store_home=None, grace_s=0.5, kill_wait_s=5.0):
    """Restart recovery for one run folder. Never resubmits, re-launches, reclaims or completes.

    Order: fence the local row of a dead owner, terminate and verify its recorded
    descendants, evaluate (and refuse) release, then fence the queue row.
    """
    report = {'owner': None, 'local_state': None, 'queue_phase': None, 'queue_deferred': None,
              'termination': None, 'release': None, 'completed': False}
    entry = run_registry.inspect(folder)
    if entry is not None and entry.get('identity') == 'CURRENT' and \
            entry['state'] in run_registry.RECOVERABLE_STATES:
        liveness, seen = _owner_liveness(entry)
        report['owner'] = liveness
        if liveness == 'GONE':
            run_registry.recovery_fence(folder, (entry['owner_pid'], entry['owner_boot'], entry['owner_start']),
                                        'restart: owner gone (%s) in state %s' % (seen['reason'], entry['state']))
            _append_event(folder, 'RECOVERY_FENCED', {'previous_state': entry['state'],
                                                      'owner_observation': seen})
        else:
            # A live or unobservable owner keeps its authority; nothing is signalled.
            _append_event(folder, 'RECOVERY_DEFERRED', {'owner': liveness, 'owner_observation': seen})
        entry = run_registry.inspect(folder)
    if entry is not None:
        report['local_state'] = entry['state']
    if entry is not None and entry.get('identity') == 'CURRENT' and entry['state'] == 'RECOVERY_FENCED':
        identities = recorded_identities(folder)
        termination = terminate_recorded(identities, grace_s=grace_s, kill_wait_s=kill_wait_s)
        _append_event(folder, 'TERMINATION', {
            'all_gone': termination['all_gone'],
            'entries': [{'pid': e['identity']['pid'], 'role': e['identity'].get('role'),
                         'signals': e['signals'], 'final': e['final'], 'reason': e['reason']}
                        for e in termination['entries']]})
        reasons, observations = release_gate(identities, production_containment())
        _append_event(folder, 'RELEASE_REFUSED' if reasons else 'RELEASE_GATE_PASSED',
                      {'reasons': reasons, 'observations': observations, 'by': 'recovery'})
        report['termination'] = termination
        report['release'] = {'released': not reasons, 'reasons': reasons}
    if queued:
        # Interrupted-launch recovery writes only when every submitter, claimant and
        # local owner of the run is observed gone. Each check runs inside the queue
        # store's write transaction that performs the fence (and creates the local
        # execution.claim marker), so a participant cannot appear in between. A live
        # or unobservable participant leaves every byte as it was: RECOVERY_DEFERRED.
        try:
            run_registry.queue_recover(folder, _store_home=_store_home, require_quiescent=True)
            phase, marker = run_registry.queue_fence_launch(folder, fence_marker=True, _store_home=_store_home)
            report['queue_phase'] = phase
            if marker:
                _append_event(folder, 'LAUNCH_FENCED', {'queue_phase': phase})
        except run_registry.RecoveryDeferred as exc:
            report['queue_deferred'] = str(exc)
            queue = run_registry.inspect_queue(folder, _store_home=_store_home)
            report['queue_phase'] = queue['phase'] if queue else None
    return report
