"""Production worker execution boundary (P7.2): ordered, fail-closed checks.

Not reachable from researchctl or any CLI. No command-line flag or process
setting selects it. This file has no argument parser and reads no process
settings. Its only inputs are the arguments passed to launch().
Callers pass a LaunchRequest and the authorizer and containment backend
programmatically. Queued execution is refused (QUEUE_READY is BLOCKED).

Checks run in this order. Nothing is consumed, advanced or launched until
every check has passed:

  1 IDENTITY_MISMATCH        run_id and attempt_id must match the registry row.
  2 FENCE_STALE              the presented owner token must equal the durable row
                             owner, the row must recompute as CURRENT and state
                             must be CLAIMED.
  3 QUEUE_NOT_ENABLED        any queued run (queue_task_id set) is refused.
  4 MANIFEST_MISMATCH        execution_manifest.verify_before_execution: manifest
                             digest equals the registry row digest, plus source,
                             input, interpreter and runtime digests.
                             Output-destination failures raised inside that call
                             (symlink, escape, existing target) are reported with
                             OUTPUT_DESTINATION_INVALID and check number 7.
  5 APPROVAL_UNAVAILABLE     approval_gate(run_id, attempt_id, manifest_digest)
                             must return ApprovalDecision.GRANTED. Default is
                             fail-closed. This module does not consume approvals.
  6 CONTAINMENT_UNAVAILABLE  containment_backend.is_verified_for_execution must
                             accept the backend's establish() result.
  7 OUTPUT_DESTINATION_INVALID  output path must lie inside the run folder, with no
                             symlink on any existing component, the nearest
                             existing directories owned by us and not group or
                             world writable, and the target must not exist.

Only after all seven checks pass: the row moves CLAIMED to DISPATCHING, the
manifest's command is spawned, the child is recorded, the row moves to RUNNING,
the child's stdout is written to the output artifact, and the row is finished
as UNKNOWN. Containment is not verified on this path, so the outcome is never
COMPLETED.

Scope and limits: see reports/experiment-infra/PRODUCTION_WORKER_BOUNDARY.md.
"""
import dataclasses
import enum
import os
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containment_backend  # noqa: E402
import execution_manifest as em  # noqa: E402
import run_ownership  # noqa: E402
import run_registry  # noqa: E402

IDENTITY_MISMATCH = 'IDENTITY_MISMATCH'
FENCE_STALE = 'FENCE_STALE'
QUEUE_NOT_ENABLED = 'QUEUE_NOT_ENABLED'
MANIFEST_MISMATCH = 'MANIFEST_MISMATCH'
APPROVAL_UNAVAILABLE = 'APPROVAL_UNAVAILABLE'
CONTAINMENT_UNAVAILABLE = 'CONTAINMENT_UNAVAILABLE'
OUTPUT_DESTINATION_INVALID = 'OUTPUT_DESTINATION_INVALID'
LAUNCH_FAILED = 'LAUNCH_FAILED'

CHECK_CODES = (IDENTITY_MISMATCH, FENCE_STALE, QUEUE_NOT_ENABLED, MANIFEST_MISMATCH,
               APPROVAL_UNAVAILABLE, CONTAINMENT_UNAVAILABLE, OUTPUT_DESTINATION_INVALID)

_CHILD_ENV = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}


class ApprovalDecision(enum.Enum):
    GRANTED = 'GRANTED'
    DENIED = 'DENIED'
    UNAVAILABLE = 'UNAVAILABLE'


def fail_closed_approval_gate(run_id, attempt_id, manifest_digest):
    """Production default. Never grants, so every request refuses at check 5."""
    del run_id, attempt_id, manifest_digest
    return ApprovalDecision.UNAVAILABLE


def fail_closed_approval_commit(run_id, attempt_id, manifest_digest):
    """Production default. Never commits, so no launch can follow a grant."""
    del run_id, attempt_id, manifest_digest
    return False


class BoundaryRefusal(Exception):
    """One refusal carrying one reason code and the check number that produced it."""

    def __init__(self, code, check, detail=''):
        self.code = code
        self.check = check
        self.detail = detail
        super().__init__('%s (check %d): %s' % (code, check, detail) if detail else
                         '%s (check %d)' % (code, check))


@dataclasses.dataclass(frozen=True)
class LaunchRequest:
    run_folder: str
    run_id: str
    attempt_id: str
    fence_token: str
    worker_id: str
    manifest_bytes: bytes
    source_root: str
    seal_dir: str
    timeout_s: float = 3600.0


@dataclasses.dataclass(frozen=True)
class LaunchOutcome:
    pid: int
    output_path: str
    exit_status: int
    timed_out: bool


def _refuse(code, check, detail=''):
    raise BoundaryRefusal(code, check, detail)


# ---------------------------------------------------------------- check 1

def _read_row(folder):
    """Read-only view of the committed run row. Never creates or migrates the registry."""
    db_path = os.path.join(folder, 'registry.sqlite3')
    if not os.path.isfile(db_path) or os.path.islink(db_path):
        _refuse(IDENTITY_MISMATCH, 1, 'no registry row')
    try:
        db = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True,
                             timeout=5, isolation_level=None)
        try:
            cursor = db.execute('SELECT * FROM run WHERE id=1')
            names = [column[0] for column in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise BoundaryRefusal(IDENTITY_MISMATCH, 1, 'registry unreadable') from exc
    if len(rows) != 1:
        _refuse(IDENTITY_MISMATCH, 1, 'no run row')
    return rows[0]


def check_identity(request, row):
    try:
        run_ownership.validate_name('run_id', request.run_id)
    except run_ownership.RegistryError:
        _refuse(IDENTITY_MISMATCH, 1, 'malformed run_id')
    if row.get('run_id') != request.run_id or row.get('attempt_id') != request.attempt_id:
        _refuse(IDENTITY_MISMATCH, 1, 'run or attempt does not match the registry row')
    if Path(request.run_folder).name != request.run_id:
        _refuse(IDENTITY_MISMATCH, 1, 'run folder does not name this run')


# ---------------------------------------------------------------- check 2

def check_fence(request, row):
    if run_ownership.classify_row(row) != 'CURRENT':
        _refuse(FENCE_STALE, 2, 'row identity does not verify')
    try:
        attempt_id, fence = run_ownership.decode_token(request.fence_token)
    except run_ownership.RegistryError:
        _refuse(FENCE_STALE, 2, 'malformed fencing token')
    if (row.get('owner') != request.fence_token or attempt_id != row.get('attempt_id') or
            fence != row.get('fence')):
        _refuse(FENCE_STALE, 2, 'token is not the current owner, attempt and fence')
    if row.get('state') != 'CLAIMED':
        _refuse(FENCE_STALE, 2, 'run is not in CLAIMED state')


# ---------------------------------------------------------------- check 3

def check_not_queued(row):
    if row.get('queue_task_id') is not None:
        _refuse(QUEUE_NOT_ENABLED, 3, 'queued execution is disabled in this phase')


# ---------------------------------------------------------------- check 4

def check_manifest(request, row):
    """Bind the manifest to the registry digest, then verify source, inputs and runtime."""
    try:
        plan = em.verify_before_execution(
            request.manifest_bytes, expected_sha256=row['manifest_sha256'],
            run_id=request.run_id, attempt_id=request.attempt_id, worker_id=request.worker_id,
            source_root=request.source_root, run_dir=request.run_folder, seal_dir=request.seal_dir)
    except em.VerificationError as exc:
        if exc.code == em.OUTPUT_DESTINATION_INVALID:
            _refuse(OUTPUT_DESTINATION_INVALID, 7, exc.detail or exc.code)
        _refuse(MANIFEST_MISMATCH, 4, exc.code)
    except OSError as exc:
        _refuse(MANIFEST_MISMATCH, 4, 'unreadable input or source: %s' % exc.__class__.__name__)
    manifest = em.parse_manifest(request.manifest_bytes)
    source_real = os.path.realpath(request.source_root)
    cwd = os.path.realpath(os.path.join(source_real, manifest['command']['cwd']))
    if cwd != source_real and not cwd.startswith(source_real + os.sep):
        _refuse(MANIFEST_MISMATCH, 4, 'command cwd escapes the verified source root')
    return plan, manifest, cwd


# ---------------------------------------------------------------- check 5

def check_approval(gate, run_id, attempt_id, manifest_digest):
    """Non-consuming check. A GRANTED decision here is not a consumed approval."""
    try:
        decision = gate(run_id, attempt_id, manifest_digest)
    except Exception:  # fail closed on any authorizer fault
        decision = None
    if decision is not ApprovalDecision.GRANTED:
        _refuse(APPROVAL_UNAVAILABLE, 5, 'no run-specific grant')


def commit_approval(commit, run_id, attempt_id, manifest_digest):
    """Consumption point. Called only after checks 1-7 pass, immediately before spawn.

    The W3 approval owner implements the single-use claim behind this callable.
    Any exception or any return value other than True refuses without spawning.
    """
    try:
        committed = commit(run_id, attempt_id, manifest_digest)
    except Exception:  # fail closed
        committed = False
    if committed is not True:
        _refuse(APPROVAL_UNAVAILABLE, 5, 'approval could not be committed at launch')


# ---------------------------------------------------------------- check 6

def check_containment(backend, argv, run_id):
    try:
        result = backend.establish(argv, run_id=run_id)
    except Exception:  # fail closed on any backend fault
        result = None
    if not containment_backend.is_verified_for_execution(result):
        _refuse(CONTAINMENT_UNAVAILABLE, 6, 'no verified containment for execution')


# ---------------------------------------------------------------- check 7

def _owner_only_dir(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        _refuse(OUTPUT_DESTINATION_INVALID, 7, 'path component is a symlink or not a directory')
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        _refuse(OUTPUT_DESTINATION_INVALID, 7, 'directory is not owner-only writable')


def check_output(output_path, run_folder):
    run_real = os.path.realpath(run_folder)
    target = os.path.normpath(output_path)
    if not target.startswith(run_real + os.sep):
        _refuse(OUTPUT_DESTINATION_INVALID, 7, 'destination is outside the run folder')
    if os.path.islink(run_real):
        _refuse(OUTPUT_DESTINATION_INVALID, 7, 'run folder is a symlink')
    _owner_only_dir(run_real)
    parts = os.path.relpath(target, run_real).split(os.sep)
    current = run_real
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if not os.path.lexists(current):
            break
        _owner_only_dir(current)
    if os.path.lexists(target):
        _refuse(OUTPUT_DESTINATION_INVALID, 7, 'destination already exists or is a symlink')
    return target


# ---------------------------------------------------------------- launch (only after checks 1-7)

def _write_output(target, data):
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _spawn_and_collect(request, folder, argv, cwd, target):
    run_registry.advance(folder, request.fence_token, 'CLAIMED', 'DISPATCHING')
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=dict(_CHILD_ENV), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True)
    except OSError as exc:
        run_registry.finish(folder, request.fence_token,
                            {'state': 'UNKNOWN', 'detail': 'spawn failed: %s' % exc.__class__.__name__})
        _refuse(LAUNCH_FAILED, 8, 'spawn failed')
    run_registry.record_child(folder, request.fence_token, proc.pid)
    run_registry.advance(folder, request.fence_token, 'DISPATCHING', 'RUNNING')
    timed_out = False
    try:
        stdout, _stderr = proc.communicate(timeout=request.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # only the group this boundary created
        except ProcessLookupError:
            pass
        stdout, _stderr = proc.communicate()
    exit_status = proc.returncode
    _write_output(target, stdout)
    run_registry.finish(folder, request.fence_token, {
        'state': 'UNKNOWN', 'exit_status': exit_status, 'timed_out': timed_out,
        'output_bytes': len(stdout)})
    return LaunchOutcome(pid=proc.pid, output_path=target, exit_status=exit_status,
                         timed_out=timed_out)


# ---------------------------------------------------------------- entry point

def launch(request, *, approval_gate=None, approval_commit=None, containment=None):
    """Run checks 1-7 in order. Launch only if all pass. Raises BoundaryRefusal otherwise.

    approval_gate defaults to fail_closed_approval_gate (non-consuming check).
    approval_commit defaults to fail_closed_approval_commit (refuses, so nothing
    launches). It is the only place an approval may be consumed, and it runs only
    after checks 1-7 pass. containment defaults to containment_backend.get_backend(),
    which is UNAVAILABLE in production.
    """
    if not isinstance(request, LaunchRequest):
        _refuse(IDENTITY_MISMATCH, 1, 'request must be a LaunchRequest')
    folder = os.path.realpath(request.run_folder)
    row = _read_row(folder)
    check_identity(request, row)
    check_fence(request, row)
    check_not_queued(row)
    plan, manifest, cwd = check_manifest(request, row)
    gate = approval_gate if approval_gate is not None else fail_closed_approval_gate
    check_approval(gate, request.run_id, request.attempt_id, row['manifest_sha256'])
    backend = containment if containment is not None else containment_backend.get_backend()
    argv = list(manifest['command']['argv'])
    check_containment(backend, argv, request.run_id)
    target = check_output(plan['output_path'], folder)
    commit = approval_commit if approval_commit is not None else fail_closed_approval_commit
    commit_approval(commit, request.run_id, request.attempt_id, row['manifest_sha256'])
    return _spawn_and_collect(request, folder, argv, cwd, target)
