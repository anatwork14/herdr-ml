"""TEST-ONLY offline end-to-end harness (Phase 6). Never import this from scripts/.

Drives one run through SUBMIT -> INTENT -> CLAIM -> CONTAINMENT_CHECK -> VALIDATION
-> EXECUTE -> ARTIFACT_SEAL, then the test runs RECOVER. Everything lives in a
private temp directory: a throwaway git repository with a trivial CPU job, input
files, a TEST-ONLY approval trust anchor, its revocation state and consumption
store, the run folder and a private queue-registry home. No queue daemon, GPU,
cgroup, network or production store is used. "Submitting" to the external queue
appends one line to a local log file, so any resubmission is countable.

Two containment gates exist:

- ProductionContainmentGate uses scripts/containment_backend.get_backend(),
  which is UNAVAILABLE on this host, and the production predicate
  is_verified_for_execution(). It always refuses here with CONTAINMENT_UNAVAILABLE.
- SimulatorContainmentGate uses tests/support/containment_sim.py with its
  test-only verifier. The simulator boundary has no members and does not contain
  the job process. A pass through it is OFFLINE_TESTED evidence of the control
  flow only; it is never containment evidence and never operational.

The worker runs as a separate process (python e2e_harness.py <config> <marker>),
so after it exits (normally, by refusal, or by an injected os._exit(77) at a named
crash point) its owner identity is gone and restart recovery can act on it.
"""
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

SUPPORT = Path(__file__).resolve().parent
TESTS = SUPPORT.parent
ROOT = TESTS.parent
SCRIPTS = ROOT / 'scripts'
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import approval as ap  # noqa: E402
import support.approval_fixtures as fx  # noqa: E402
import containment_backend  # noqa: E402
import execution_manifest as em  # noqa: E402
import run_ownership  # noqa: E402
import run_recovery  # noqa: E402
import run_registry  # noqa: E402

STAGES = ('SUBMIT', 'INTENT', 'CLAIM', 'CONTAINMENT_CHECK', 'VALIDATION', 'EXECUTE',
          'ARTIFACT_SEAL', 'RECOVER')
RUN_ID = 'run-1'
PROJECT = 'project-e2e'
ATTEMPT_LABEL = 'attempt-1'      # manifest/approval attempt label, declared at SUBMIT
WORKER_ID = 'worker-A'
TASK_ID = 'task-1'
QUEUE_INSTANCE = 'e' * 64
ANCHOR_ID = 'TEST-ONLY-e2e-anchor'
APPROVER = 'approver-a'
REVOKER = 'revoker-b'
OUTPUT = {'root': 'output', 'path': 'result.json'}
SEAL_KIND = 'herdr-ml-e2e-artifact-seal'
EVIDENCE_LABEL = 'OFFLINE_TESTED'
EXIT_REFUSED = 3
EXIT_CRASH = 77
JOB_WAIT_S = 60

# Seal verification codes (test harness only).
SEAL_MALFORMED = 'SEAL_MALFORMED'
SEAL_DIGEST_MISMATCH = 'SEAL_DIGEST_MISMATCH'
SEAL_BINDING_MISMATCH = 'SEAL_BINDING_MISMATCH'
ARTIFACT_DIGEST_MISMATCH = 'ARTIFACT_DIGEST_MISMATCH'
ARTIFACT_MISSING = 'ARTIFACT_MISSING'

# The trivial CPU job. It reads the SEALED dataset copy, writes a small JSON
# result to the authorized output path, then waits (bounded) for a go file so
# that a crash right after its identity is recorded leaves it alive for recovery.
JOB_SRC = r'''import hashlib, json, os, sys, time
marker = sys.argv[1]
data = open(os.environ['HERDR_E2E_INPUT'], 'rb').read()
result = {'input_sha256': hashlib.sha256(data).hexdigest(), 'byte_sum': sum(data),
          'length': len(data), 'marker': marker}
out = os.environ['HERDR_E2E_OUTPUT']
tmp = out + '.tmp'
with open(tmp, 'w') as handle:
    json.dump(result, handle, sort_keys=True)
    handle.flush()
    os.fsync(handle.fileno())
os.rename(tmp, out)
deadline = time.monotonic() + %d
while not os.path.exists(os.environ['HERDR_E2E_GO']):
    if time.monotonic() > deadline:
        os._exit(4)
    time.sleep(0.01)
''' % JOB_WAIT_S

# Start gate for the job. The worker launches the job through this shim, which blocks
# on an inherited pipe until the worker has recorded the job's identity (pid + start
# ticks) in spawned.jsonl, then execs the real argv (same pid and start time). If the
# worker dies before releasing the gate, its write end closes, the shim reads EOF and
# exits without ever running the job, so an unrecorded job cannot outlive the worker.
GATE_SRC = r'''import os, sys
fd = int(sys.argv[1])
go = os.read(fd, 1)
os.close(fd)
if go != b'1':
    os._exit(125)
os.execvp(sys.argv[2], sys.argv[2:])
'''
# Harness-local crash points (not part of scripts/run_ownership.CRASH_POINTS).
HARNESS_CRASH_POINTS = frozenset({'execute:spawned_unrecorded'})

GIT = ['git', '-c', 'user.name=test-only', '-c', 'user.email=test-only@example.invalid',
       '-c', 'commit.gpgsign=false']


class SealError(Exception):
    def __init__(self, code, detail=''):
        super().__init__('%s: %s' % (code, detail) if detail else code)
        self.code = code
        self.detail = detail


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _git(cwd, *args):
    subprocess.run(GIT + ['-C', str(cwd)] + list(args), check=True, capture_output=True, timeout=60)


def _write_private(path, data, mode=0o600):
    path = Path(path)
    tmp = path.with_name('.' + path.name + '.' + secrets.token_hex(4) + '.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(str(tmp), str(path))


def _append_line(path, payload):
    with open(path, 'a') as handle:
        handle.write(json.dumps(payload, sort_keys=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def read_lines(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- fixture (SUBMIT)

def write_test_anchor(directory):
    """A TEST-ONLY HMAC anchor in a temp directory. Keys are random per case."""
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    signers = {APPROVER: 'test-only-approver.key', REVOKER: 'test-only-revoker.key'}
    for filename in signers.values():
        _write_private(directory / filename, secrets.token_bytes(32))
    document = {'schema_version': 1, 'kind': ap.ANCHOR_KIND, 'anchor_id': ANCHOR_ID,
                'algorithm': ap.ALGORITHM, 'workers': [WORKER_ID], 'signers': signers,
                'revoker': REVOKER}
    _write_private(directory / 'anchor.json', em.canonical_bytes(document))
    return directory


def build_case(base, marker, *, containment, with_anchor, crash_point=None):
    """SUBMIT stage, performed by the submitter/approver (the test process).

    Creates the source repository, inputs, manifest and, when with_anchor, a
    TEST-ONLY anchor with an approval for this exact manifest, worker, run and
    attempt. Returns the case dict, which is also the worker's config.
    """
    base = Path(os.path.realpath(str(base)))
    base.mkdir(mode=0o700)
    source = base / 'source'
    source.mkdir()
    (source / 'job.py').write_text(JOB_SRC)
    _git(source, 'init', '-q')
    _git(source, 'add', '-A')
    _git(source, 'commit', '-q', '-m', 'test-only e2e job')
    data = base / 'data'
    data.mkdir()
    inputs = {'dataset_content': data / 'dataset.bin', 'dataset_manifest': data / 'dataset_manifest.json',
              'split_manifest': data / 'split.json'}
    inputs['dataset_content'].write_bytes(b'herdr-e2e-dataset:' + secrets.token_bytes(32))
    inputs['dataset_manifest'].write_text('{"files":1}\n')
    inputs['split_manifest'].write_text('{"test":[1],"train":[0]}\n')
    config = data / 'config.json'
    config.write_text('{"steps":1}\n')
    home = base / 'home'
    home.mkdir(mode=0o700)
    folder = base / '.runtime' / PROJECT / 'runs' / RUN_ID
    folder.mkdir(parents=True)
    private = base / 'private'
    private.mkdir(mode=0o700)

    manifest = em.build_manifest(
        run_id=RUN_ID, attempt_id=ATTEMPT_LABEL, project=PROJECT, operation='diagnostic-execution',
        resource_class='cpu-diagnostic', source_root=str(source), source_files=['job.py'],
        args=['-I', 'job.py', marker], cwd='.', config=str(config),
        inputs={k: str(v) for k, v in inputs.items()}, primary_seed=1, nondeterminism=[],
        output_root=OUTPUT['root'], output_path=OUTPUT['path'])
    manifest_bytes = em.encode_manifest(manifest)
    manifest_path = base / 'manifest.json'
    _write_private(manifest_path, manifest_bytes)
    digest = em.sha256_hex(manifest_bytes)

    case = {'base': str(base), 'source': str(source), 'home': str(home), 'folder': str(folder),
            'private': str(private), 'manifest': str(manifest_path), 'manifest_sha256': digest,
            'marker': marker, 'containment': containment, 'crash_point': crash_point,
            'stage_log': str(base / 'stages.jsonl'), 'spawned': str(base / 'spawned.jsonl'),
            'submissions': str(base / 'submissions.log'), 'go': str(private / 'go'),
            'seal': str(base / 'artifact.seal.json'), 'result_record': str(base / 'result.record.json'),
            'output': str(folder / OUTPUT['root'] / OUTPUT['path']),
            'anchor': None, 'approval': None, 'revocations': None, 'state': None, 'store': None}
    if with_anchor:
        anchor_dir = write_test_anchor(base / 'TEST-ONLY-anchor')
        state_dir = base / 'revocation-state'
        state_dir.mkdir(mode=0o700)
        store_dir = base / 'consumed'
        store_dir.mkdir(mode=0o700)
        anchor = ap.load_trust_anchor(str(anchor_dir), repo_root=str(source), run_dir=str(folder),
                                      **fx.PRODUCTION_UIDS)
        ap.provision_revocation_state(str(state_dir), anchor_id=ANCHOR_ID)
        now = _utc_now()
        approval = ap.issue_approval(
            anchor, APPROVER, approval_id='appr-e2e-1', worker_id=WORKER_ID, run_id=RUN_ID,
            attempt_id=ATTEMPT_LABEL, manifest_sha256=digest, resource_class='cpu-diagnostic',
            scope={'project': PROJECT, 'operation': 'diagnostic-execution'}, output=OUTPUT,
            issued_at=now - dt.timedelta(minutes=1), expires_at=now + dt.timedelta(hours=1))
        revocations = ap.issue_revocation(anchor, issued_at=now - dt.timedelta(minutes=1), sequence=1,
                                          approval_ids=(), manifest_sha256s=())
        _write_private(base / 'approval.json', approval)
        _write_private(base / 'revocations.json', revocations)
        case.update(anchor=str(anchor_dir), approval=str(base / 'approval.json'),
                    revocations=str(base / 'revocations.json'), state=str(state_dir), store=str(store_dir))
    config_path = base / 'worker-config.json'
    _write_private(config_path, json.dumps(case, sort_keys=True).encode())
    case['config'] = str(config_path)
    _append_line(case['stage_log'], {'stage': 'SUBMIT', 'status': 'PASS', 'manifest_sha256': digest})
    return case


def worker_argv(case):
    return [sys.executable, str(Path(__file__).resolve()), case['config'], case['marker']]


# --------------------------------------------------------------------------- containment gates

class ProductionContainmentGate:
    """The production selector and predicate. UNAVAILABLE on this host, so it always refuses."""
    name = 'production'

    def check(self, argv, *, run_id, attempt_id):
        del attempt_id
        result = containment_backend.get_backend().establish(list(argv), run_id=run_id)
        status = run_recovery.containment_status(result)
        if not containment_backend.is_verified_for_execution(result):
            return False, 'CONTAINMENT_' + status, {'status': status, 'reason': result.reason}
        return True, None, {'status': status}

    def after(self):
        return None


class SimulatorContainmentGate:
    """TEST-ONLY. The W2 simulator boundary verified by the simulator's own test verifier.

    The boundary has no members and does not contain the job. This exercises the
    control flow of a verified gate; it is not containment evidence.
    """
    name = 'simulator'

    def __init__(self):
        sys.path.insert(0, str(SUPPORT))
        import containment_sim  # noqa: E402  (test-only; never imported by scripts/)
        self._sim = containment_sim
        self._controller = None

    def check(self, argv, *, run_id, attempt_id):
        del argv
        boundary = self._sim.SimBoundary(None, run_id, attempt_id, real=False)
        controller = self._sim.Controller(boundary)
        proof = controller.verify()
        decision = self._sim.SIM_VERIFIER.preview(proof, controller.expectation)
        if not decision.allowed:
            return False, 'CONTAINMENT_SIMULATOR_REFUSED', {'reason': decision.reason}
        self._controller = controller
        return True, None, {'status': 'SIMULATED_VERIFIED', 'label': EVIDENCE_LABEL,
                            'operational': False, 'verdict': proof.verdict}

    def after(self):
        controller = self._controller
        controller.verify()
        completed = controller.complete()
        released = controller.release(controller.last_proof) if completed.allowed else completed
        return {'simulated_release': bool(released.allowed), 'reason': released.reason,
                'label': EVIDENCE_LABEL, 'operational': False}


def make_gate(name):
    if name == 'production':
        return ProductionContainmentGate()
    if name == 'simulator':
        return SimulatorContainmentGate()
    raise ValueError('unknown containment gate %r' % (name,))


# --------------------------------------------------------------------------- approval loading

def load_approval_inputs(case, *, repo_root, run_dir):
    """With no case anchor, the production default location and production ownership mode."""
    if case['anchor'] is None:
        anchor = ap.load_trust_anchor(ap.DEFAULT_TRUST_ANCHOR_DIR, repo_root=repo_root, run_dir=run_dir,
                                      admin_uids=(0,), worker_uids=(os.getuid(),))
        raise AssertionError('unreachable on this host: production anchor loaded %r' % anchor.anchor_id)
    # The production loader and stores, with explicit uid allowlists and no special directory.
    anchor = ap.load_trust_anchor(case['anchor'], repo_root=repo_root, run_dir=run_dir,
                                  **fx.PRODUCTION_UIDS)
    state = ap.RevocationState(case['state'], repo_root=repo_root, run_dir=run_dir,
                               anchor_id=anchor.anchor_id, **fx.PRODUCTION_UIDS)
    store = ap.ConsumptionStore(case['store'], repo_root=repo_root, run_dir=run_dir,
                                **fx.PRODUCTION_UIDS)
    return anchor, state, store


# --------------------------------------------------------------------------- artifact seal

def build_seal(case, plan, record_sha, output_path):
    artifact_sha, artifact_size = em._consume(output_path, missing=ARTIFACT_MISSING,
                                              unsafe=ARTIFACT_MISSING)
    seal = {'kind': SEAL_KIND, 'schema_version': 1, 'manifest_sha256': plan['manifest_sha256'],
            'run_id': plan['run_id'], 'attempt_id': plan['attempt_id'], 'worker_id': plan['worker_id'],
            'artifact': {'root': OUTPUT['root'], 'path': OUTPUT['path'], 'sha256': artifact_sha,
                         'size': artifact_size},
            'result_record_sha256': record_sha, 'evidence_label': EVIDENCE_LABEL, 'operational': False}
    data = em.canonical_bytes(seal)
    return seal, data, em.sha256_hex(data)


def verify_seal(seal_path, output_path, *, manifest_sha256, recorded):
    """Re-hash the artifact and check it against the seal and the registry-recorded hashes.

    recorded is the run registry's result_json (written by finish()).
    """
    try:
        data = Path(seal_path).read_bytes()
        seal = json.loads(data)
    except (OSError, ValueError) as exc:
        raise SealError(SEAL_MALFORMED, str(exc)) from exc
    if not isinstance(seal, dict) or em.canonical_bytes(seal) != data or seal.get('kind') != SEAL_KIND:
        raise SealError(SEAL_MALFORMED, 'not a canonical seal')
    if em.sha256_hex(data) != recorded.get('seal_sha256'):
        raise SealError(SEAL_DIGEST_MISMATCH, 'seal bytes differ from the recorded seal digest')
    if seal['manifest_sha256'] != manifest_sha256 or recorded.get('manifest_sha256') != manifest_sha256:
        raise SealError(SEAL_BINDING_MISMATCH, 'seal is bound to another manifest')
    if seal['artifact']['sha256'] != recorded.get('output_sha256'):
        raise SealError(ARTIFACT_DIGEST_MISMATCH, 'sealed hash differs from the recorded output hash')
    try:
        actual, size = em._consume(str(output_path), missing=ARTIFACT_MISSING, unsafe=ARTIFACT_MISSING)
    except em.VerificationError as exc:
        raise SealError(ARTIFACT_MISSING, exc.detail) from exc
    if actual != seal['artifact']['sha256'] or size != seal['artifact']['size']:
        raise SealError(ARTIFACT_DIGEST_MISMATCH, 'artifact bytes changed after sealing')
    return seal


# --------------------------------------------------------------------------- worker

class Refused(Exception):
    def __init__(self, stage, code, detail=''):
        super().__init__('%s %s' % (stage, code))
        self.stage, self.code, self.detail = stage, code, detail


def worker_main(config_path, marker):
    case = json.loads(Path(config_path).read_text())
    if case['marker'] != marker:
        raise SystemExit('marker mismatch')
    crash = case['crash_point']
    if crash is not None and crash not in run_ownership.CRASH_POINTS | HARNESS_CRASH_POINTS:
        raise SystemExit('unknown crash point %r' % crash)

    def hook(point):
        if point == crash:
            sys.stdout.write('CRASHED %s\n' % point)
            sys.stdout.flush()
            os._exit(EXIT_CRASH)

    def stage(name, status='PASS', **detail):
        _append_line(case['stage_log'], dict(detail, stage=name, status=status))

    folder, home = Path(case['folder']), Path(case['home'])
    digest = case['manifest_sha256']
    manifest_bytes = Path(case['manifest']).read_bytes()
    manifest = em.parse_manifest(manifest_bytes)
    argv = list(manifest['command']['argv'])
    try:
        # INTENT: durable intent, one external submission, bind.
        intent = run_registry.queue_intent(folder, RUN_ID, digest, QUEUE_INSTANCE, _store_home=home,
                                           _fault=hook)
        with open(case['submissions'], 'a') as handle:
            handle.write(TASK_ID + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        run_registry.queue_bind_task(folder, intent, TASK_ID, _store_home=home, _fault=hook)
        stage('INTENT')

        # CLAIM: queue claim then local claim (adopts the queue attempt and fence).
        fence = run_registry.queue_claim(folder, RUN_ID, digest, QUEUE_INSTANCE, TASK_ID, WORKER_ID,
                                         _store_home=home, _fault=hook)
        token = run_registry.claim(folder, RUN_ID, PROJECT, digest, 'QUEUED', queue_instance=QUEUE_INSTANCE,
                                   queue_task_id=TASK_ID, queue_worker_id=WORKER_ID, queue_fence=fence,
                                   _store_home=home, _fault=hook)
        stage('CLAIM', fence=fence)

        # CONTAINMENT CHECK: before any validation or execution.
        gate = make_gate(case['containment'])
        ok, code, evidence = gate.check(argv, run_id=RUN_ID, attempt_id=ATTEMPT_LABEL)
        if not ok:
            raise Refused('CONTAINMENT_CHECK', code, evidence)
        stage('CONTAINMENT_CHECK', gate=gate.name, evidence=evidence)

        # VALIDATION: execution manifest, then run-specific approval (consumed last).
        seal_dir = Path(case['private']) / 'sealed'
        seal_dir.mkdir(mode=0o700)
        try:
            plan = em.verify_before_execution(
                manifest_bytes, expected_sha256=digest, run_id=RUN_ID, attempt_id=ATTEMPT_LABEL,
                worker_id=WORKER_ID, source_root=case['source'], run_dir=str(folder), seal_dir=str(seal_dir))
            anchor, state, store = load_approval_inputs(case, repo_root=case['source'], run_dir=str(folder))
            record = ap.verify_approval(
                Path(case['approval']).read_bytes(), anchor,
                revocation_bytes=Path(case['revocations']).read_bytes(), revocation_state=state,
                expected={'worker_id': WORKER_ID, 'run_id': RUN_ID, 'attempt_id': ATTEMPT_LABEL,
                          'manifest_sha256': digest, 'resource_class': manifest['resource_class'],
                          'scope': {'project': manifest['project'], 'operation': manifest['operation']},
                          'output': manifest['output']},
                store=store)
        except em.VerificationError as exc:
            raise Refused('VALIDATION', exc.code, exc.detail)
        stage('VALIDATION', approval_id=record['approval_id'], anchor_id=record['anchor_id'])
        run_ownership.fault(hook, 'execute:claimed')

        # EXECUTE: the trivial CPU job on the sealed dataset copy, in a private directory.
        run_registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        Path(plan['output_path']).parent.mkdir(mode=0o700)
        env = {'PATH': '/usr/bin:/bin', 'HOME': case['private'], 'TMPDIR': case['private'],
               'HERDR_E2E_INPUT': plan['sealed_inputs']['dataset_content'],
               'HERDR_E2E_OUTPUT': plan['output_path'], 'HERDR_E2E_GO': case['go']}
        cwd = os.path.join(case['source'], manifest['command']['cwd'])
        # The job is held at the start gate until its identity is recorded; only then released.
        gate_read, gate_write = os.pipe()
        try:
            job = subprocess.Popen([sys.executable, '-I', '-c', GATE_SRC, str(gate_read)] + argv, cwd=cwd,
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(gate_read,),
                                   start_new_session=True)
        finally:
            os.close(gate_read)
        hook('execute:spawned_unrecorded')
        identity = run_recovery.read_identity(job.pid, role='leader')
        _append_line(case['spawned'], dict(identity, by='worker', what='job'))
        released = []

        def release_gate():
            if not released:
                released.append(True)
                os.write(gate_write, b'1')

        def hook_after_descendants_commit(point):
            # The descendants are durably recorded once this point is reached, so the job may
            # run from here; an injected crash at this point then leaves a recorded live job.
            if point == 'descendants:after_commit':
                release_gate()
                # Let the released job write its output (bounded), so the state at an injected
                # crash here is deterministic: job alive, output written.
                wait_until = time.monotonic() + 10
                while not os.path.exists(plan['output_path']) and time.monotonic() < wait_until:
                    time.sleep(0.01)
            hook(point)

        try:
            run_registry.record_child(folder, token, job.pid)
            run_registry.advance(folder, token, 'DISPATCHING', 'RUNNING')
            run_recovery.record_descendants(folder, token, [identity], _fault=hook_after_descendants_commit)
            # Release the gate only after the child and its descendants are recorded.
            release_gate()
        finally:
            os.close(gate_write)
        Path(case['go']).touch()
        exit_code = job.wait(timeout=JOB_WAIT_S + 30)
        stage('EXECUTE', exit_code=exit_code, pid=identity['pid'], start_ticks=identity['start_ticks'])
        run_ownership.fault(hook, 'execute:leader_exited')

        # ARTIFACT SEAL: hash the output, bind it to the manifest, record it in the registry.
        _rec, record_bytes, record_sha = em.build_result_record(
            plan, result_path=plan['output_path'], exit_status=exit_code, containment=None)
        seal, seal_bytes, seal_sha = build_seal(case, plan, record_sha, plan['output_path'])
        _write_private(case['result_record'], record_bytes)
        _write_private(case['seal'], seal_bytes)
        run_registry.finish(folder, token, {
            'state': 'UNKNOWN', 'leader_state': 'COMPLETED' if exit_code == 0 else 'FAILED',
            'exit_code': exit_code, 'containment': 'UNKNOWN', 'manifest_sha256': digest,
            'output_sha256': seal['artifact']['sha256'], 'result_record_sha256': record_sha,
            'seal_sha256': seal_sha, 'evidence_label': EVIDENCE_LABEL}, _fault=hook)
        stage('ARTIFACT_SEAL', output_sha256=seal['artifact']['sha256'], seal_sha256=seal_sha)

        # Post-run: simulator boundary release (test-only) and the production release gate.
        simulated = gate.after()
        released = run_recovery.release_run(folder, token, containment=run_recovery.production_containment(),
                                            _fault=hook)
        stage('RELEASE', status='REFUSED' if not released['released'] else 'PASS',
              reasons=released['reasons'], simulated=simulated)
    except Refused as exc:
        stage(exc.stage, status='REFUSED', code=exc.code)
        sys.stdout.write('REFUSED %s %s\n' % (exc.stage, exc.code))
        sys.stdout.flush()
        return EXIT_REFUSED
    sys.stdout.write('DONE\n')
    return 0


def run_worker(case, *, on_spawn, timeout=120):
    """Start the worker, record its identity via on_spawn before anything else, wait for it."""
    proc = subprocess.Popen(worker_argv(case), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, close_fds=True)
    try:
        on_spawn(proc, run_recovery.read_identity(proc.pid, role='worker'))
    except BaseException:
        proc.kill()
        proc.wait(timeout=10)
        raise
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=10)
        raise
    return proc.returncode, out, err


if __name__ == '__main__':
    raise SystemExit(worker_main(sys.argv[1], sys.argv[2]))
