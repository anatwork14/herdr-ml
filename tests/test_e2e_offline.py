"""Phase 6: offline end-to-end CPU validation of the experiment chain.

SUBMIT -> INTENT -> CLAIM -> CONTAINMENT_CHECK -> VALIDATION -> EXECUTE -> ARTIFACT_SEAL -> RECOVER,
driven by tests/support/e2e_harness.py with disposable CPU processes in private temp
directories. Evidence labels: OFFLINE_TESTED / ISOLATED_CPU_VERIFIED only. Nothing here
is operational: the production containment backend is UNAVAILABLE, no production
trust anchor exists, PRODUCTION_DISPATCH_ENABLED stays False and researchctl's queued
path stays blocked.

Process accounting: the test records every process it starts (the worker) and every
process the worker starts (the job) by pid plus start time. tearDown first checks that
none of them is still alive, terminates any survivor by its recorded identity only
(pidfd), and asserts `pgrep -f <marker>` is empty. Nothing is signalled by name.
"""
import ast
import functools
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
for _path in (str(SCRIPTS), str(ROOT / 'tests')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import approval as ap  # noqa: E402
import containment_backend  # noqa: E402
import execution_manifest as em  # noqa: E402
import queue_adapter  # noqa: E402
import researchctl  # noqa: E402
import run_ownership  # noqa: E402
import run_recovery  # noqa: E402
import run_registry  # noqa: E402
from support import e2e_harness as h  # noqa: E402
from support.approval_test_mode import TEST_MODE  # noqa: E402
from support import process_tree  # noqa: E402

RegistryError = run_ownership.RegistryError
REPEATS = 3
MARKER_PREFIX = 'herdr-e2e-test-'

# (label, crash point, chain stage interrupted, local state left by the crash,
#  queue ambiguous origin after recovery, approval consumed, job alive at crash,
#  output written, seal written)
BOUNDARIES = (
    ('after_intent', 'queue_intent:after_commit', 'INTENT', None, 'INTENT', False, False, False, False),
    ('after_bind', 'queue_bind_task:after_commit', 'INTENT', None, 'BOUND', False, False, False, False),
    ('after_queue_claim', 'queue_claim:after_commit', 'CLAIM', None, 'CLAIMED', False, False, False, False),
    ('after_local_claim', 'claim:after_commit', 'CLAIM', 'CLAIMED', 'CLAIMED', False, False, False, False),
    ('after_validation', 'execute:claimed', 'VALIDATION', 'CLAIMED', 'CLAIMED', True, False, False, False),
    ('after_child_recorded', 'descendants:after_commit', 'EXECUTE', 'RUNNING', 'CLAIMED', True, True, True,
     False),
    ('after_leader_exit', 'execute:leader_exited', 'EXECUTE', 'RUNNING', 'CLAIMED', True, False, True, False),
    ('after_seal_recorded', 'finish:after_commit', 'ARTIFACT_SEAL', 'UNKNOWN', 'CLAIMED', True, False, True,
     True),
    ('before_release', 'release:begin', 'ARTIFACT_SEAL', 'UNKNOWN', 'CLAIMED', True, False, True, True),
)
PASS_COUNTS = {}


def timeout(seconds):
    """Fail the test (TimeoutError) if it runs longer than seconds. Main thread only (SIGALRM)."""
    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            def expire(_signum, _frame):
                raise TimeoutError('%s exceeded its %ds timeout' % (func.__name__, seconds))
            previous = signal.signal(signal.SIGALRM, expire)
            signal.alarm(seconds)
            try:
                return func(*args, **kwargs)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
        wrapper.timeout_seconds = seconds
        return wrapper
    return decorate


class E2EBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        self.marker = MARKER_PREFIX + uuid.uuid4().hex
        self.started = []    # identities (pid + start ticks) of every process this test caused
        self.handles = []
        self.cases = 0

    def tearDown(self):
        alive = [i for i in self.started if run_recovery.observe(i)[0] not in run_recovery.GONE_STATES]
        if alive:
            run_recovery.terminate_recorded(alive, grace_s=0.1, kill_wait_s=5.0)
        for handle in self.handles:
            if handle.poll() is None:
                handle.kill()
            handle.wait(timeout=10)
        remaining = [i for i in self.started if run_recovery.observe(i)[0] not in run_recovery.GONE_STATES]
        self.assertEqual(remaining, [], 'recorded processes still alive after cleanup')
        self.assertEqual(alive, [], 'processes started by this test were alive at teardown')
        self.assertEqual(process_tree.wait_no_marked(self.marker), [],
                         'marked processes started by this test are still running')

    # --- helpers ------------------------------------------------------------

    def new_case(self, *, containment, with_anchor=True, crash_point=None):
        self.cases += 1
        return h.build_case(self.root / ('case-%d' % self.cases), self.marker, containment=containment,
                            with_anchor=with_anchor, crash_point=crash_point)

    def run_case(self, case):
        def on_spawn(proc, identity):
            self.handles.append(proc)
            self.started.append(identity)
        code, out, err = h.run_worker(case, on_spawn=on_spawn, timeout=150)
        for entry in h.read_lines(case['spawned']):
            identity = {k: entry[k] for k in ('pid', 'boot', 'start_ticks', 'pid_namespace', 'role')}
            self.started.append(identity)
        return code, out, err

    def job_identities(self, case):
        return [e for e in h.read_lines(case['spawned']) if e.get('what') == 'job']

    def stages(self, case):
        return [(e['stage'], e['status']) for e in h.read_lines(case['stage_log'])]

    def submissions(self, case):
        path = Path(case['submissions'])
        return path.read_text().splitlines() if path.exists() else []

    def consumed_claims(self, case):
        if case['store'] is None:
            return []
        return sorted(p.name for p in Path(case['store']).iterdir())

    def recover(self, case):
        return run_recovery.recover(case['folder'], _store_home=case['home'], grace_s=0.3)

    def assert_no_new_launch(self, case):
        """No resubmission, relaunch or reclaim is possible after recovery."""
        folder, home, digest = case['folder'], case['home'], case['manifest_sha256']
        with self.assertRaises(RegistryError):
            run_registry.queue_intent(folder, h.RUN_ID, digest, h.QUEUE_INSTANCE, _store_home=home)
        with self.assertRaises(RegistryError):
            run_registry.queue_claim(folder, h.RUN_ID, digest, h.QUEUE_INSTANCE, h.TASK_ID, 'worker-B',
                                     _store_home=home)
        for initial in ('SUBMITTED', 'QUEUED'):
            queued = initial == 'QUEUED'
            with self.assertRaises(RegistryError):
                run_registry.claim(folder, h.RUN_ID, h.PROJECT, digest, initial,
                                   queue_instance=h.QUEUE_INSTANCE if queued else None,
                                   queue_task_id=h.TASK_ID if queued else None,
                                   queue_worker_id=h.WORKER_ID if queued else None,
                                   queue_fence=1 if queued else None, _store_home=home)
        self.assertTrue((Path(folder) / 'execution.claim').is_dir())


class FailClosedContainmentTests(E2EBase):
    """Item 1: the production backend (UNAVAILABLE) refuses at CONTAINMENT_CHECK; nothing executes."""

    @timeout(180)
    def test_production_containment_refuses_before_execute(self):
        # A valid TEST-ONLY approval is supplied, so containment is the only barrier.
        case = self.new_case(containment='production', with_anchor=True)
        code, out, err = self.run_case(case)
        self.assertEqual(code, h.EXIT_REFUSED, out + err)
        self.assertIn('REFUSED CONTAINMENT_CHECK CONTAINMENT_UNAVAILABLE', out)
        self.assertEqual(self.stages(case), [('SUBMIT', 'PASS'), ('INTENT', 'PASS'), ('CLAIM', 'PASS'),
                                             ('CONTAINMENT_CHECK', 'REFUSED')])
        refusal = h.read_lines(case['stage_log'])[-1]
        self.assertEqual(refusal['code'], 'CONTAINMENT_UNAVAILABLE')
        # Nothing executed: no job process, no child identity, no output, no seal, approval unused.
        self.assertEqual(self.job_identities(case), [])
        local = run_registry.inspect(case['folder'])
        self.assertEqual(local['state'], 'CLAIMED')
        self.assertIsNone(local['child'])
        self.assertIsNone(local['result'])
        self.assertFalse(Path(case['output']).exists())
        self.assertFalse(Path(case['output']).parent.exists())
        self.assertFalse(Path(case['seal']).exists())
        self.assertFalse(Path(case['result_record']).exists())
        self.assertFalse((Path(case['private']) / 'sealed').exists())
        self.assertEqual(self.consumed_claims(case), [])
        queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
        self.assertEqual((queue['phase'], queue['task_id'], queue['fence']), ('CLAIMED', h.TASK_ID, 1))
        self.assertEqual(self.submissions(case), [h.TASK_ID])

        # RECOVER: the dead worker's claim is fenced, never completed and never re-launched.
        report = self.recover(case)
        self.assertEqual(report['owner'], 'GONE')
        self.assertEqual(report['local_state'], 'RECOVERY_FENCED')
        self.assertFalse(report['completed'])
        self.assertEqual(report['termination']['entries'], [])
        self.assertEqual(report['release']['reasons'], ['NO_RECORDED_DESCENDANTS', 'CONTAINMENT_UNAVAILABLE'])
        queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
        self.assertEqual((queue['phase'], queue['ambiguous_from']), ('AMBIGUOUS', 'CLAIMED'))
        self.assertEqual(run_registry.inspect(case['folder'])['state'], 'RECOVERY_FENCED')
        self.assert_no_new_launch(case)
        self.assertEqual(self.submissions(case), [h.TASK_ID])
        self.assertFalse(Path(case['output']).exists())

    @timeout(30)
    def test_production_gate_and_dispatch_guards_stay_closed(self):
        self.assertIsInstance(containment_backend.get_backend(), containment_backend.UnavailableContainmentBackend)
        ok, code, evidence = h.ProductionContainmentGate().check(['true'], run_id='r', attempt_id='a')
        self.assertEqual((ok, code, evidence['status']), (False, 'CONTAINMENT_UNAVAILABLE', 'UNAVAILABLE'))
        self.assertEqual(containment_backend._VERIFIED_ISSUERS, frozenset())
        self.assertIs(queue_adapter.PRODUCTION_DISPATCH_ENABLED, False)
        with self.assertRaises(researchctl.ResearchError):
            researchctl.require_supported_execution('diagnostic', 'gpu0')
        with self.assertRaises(researchctl.ResearchError):
            researchctl.require_supported_execution('diagnostic', 'gpu0', submission=True)
        with self.assertRaises(researchctl.ResearchError):
            researchctl.require_supported_execution('official', None)

    @timeout(30)
    def test_scripts_never_import_test_only_harness_or_simulator(self):
        for path in sorted(SCRIPTS.iterdir()):
            if path.suffix not in ('.py', '.sh'):
                continue
            text = path.read_text(errors='replace')
            for name in ('e2e_harness', 'containment_sim', 'tests/support', 'TEST-ONLY-e2e'):
                self.assertNotIn(name, text, '%s references %s' % (path.name, name))


class SpawnRecordTests(E2EBase):
    """The job is recorded before it is released, so a worker death in between leaves no orphan."""

    @timeout(180)
    def test_worker_killed_between_popen_and_record_leaves_no_orphan(self):
        case = self.new_case(containment='simulator', with_anchor=True,
                             crash_point='execute:spawned_unrecorded')
        code, out, err = self.run_case(case)
        self.assertEqual(code, h.EXIT_CRASH, out + err)
        self.assertIn('CRASHED execute:spawned_unrecorded', out)
        # The job was started but never recorded; held at the start gate, it exits on EOF.
        self.assertEqual(self.job_identities(case), [])
        self.assertIsNone(run_registry.inspect(case['folder'])['child'])
        self.assertEqual(process_tree.wait_no_marked(self.marker), [],
                         'unrecorded job outlived the worker')
        self.assertFalse(Path(case['output']).exists())
        # tearDown then asserts nothing recorded or marked is alive.


class SimulatedVerifiedPathTests(E2EBase):
    """Item 2: with the TEST-ONLY simulator gate the chain reaches EXECUTE and ARTIFACT_SEAL (OFFLINE_TESTED)."""

    @timeout(180)
    def test_simulator_path_reaches_execute_and_seal_offline_only(self):
        case = self.new_case(containment='simulator', with_anchor=True)
        code, out, err = self.run_case(case)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.stages(case), [
            ('SUBMIT', 'PASS'), ('INTENT', 'PASS'), ('CLAIM', 'PASS'), ('CONTAINMENT_CHECK', 'PASS'),
            ('VALIDATION', 'PASS'), ('EXECUTE', 'PASS'), ('ARTIFACT_SEAL', 'PASS'), ('RELEASE', 'REFUSED')])
        log = {e['stage']: e for e in h.read_lines(case['stage_log'])}
        # The gate pass is labelled simulated, OFFLINE_TESTED and not operational.
        evidence = log['CONTAINMENT_CHECK']['evidence']
        self.assertEqual((evidence['status'], evidence['label'], evidence['operational']),
                         ('SIMULATED_VERIFIED', 'OFFLINE_TESTED', False))
        self.assertEqual(log['EXECUTE']['exit_code'], 0)
        # The simulator released its own (empty) boundary; the production release gate still refuses.
        self.assertTrue(log['RELEASE']['simulated']['simulated_release'])
        self.assertIs(log['RELEASE']['simulated']['operational'], False)
        self.assertEqual(log['RELEASE']['reasons'], ['CONTAINMENT_UNAVAILABLE'])
        # The job ran on the sealed dataset copy and produced the output.
        output = json.loads(Path(case['output']).read_text())
        dataset = Path(case['base']) / 'data' / 'dataset.bin'
        self.assertEqual(output['input_sha256'], em.sha256_hex(dataset.read_bytes()))
        self.assertEqual(output['marker'], self.marker)
        # Result record: containment UNKNOWN, status UNKNOWN (never COMPLETED offline).
        record = json.loads(Path(case['result_record']).read_text())
        self.assertEqual(record['containment']['status'], 'UNKNOWN')
        self.assertEqual(record['status'], 'UNKNOWN')
        self.assertEqual(record['manifest_sha256'], case['manifest_sha256'])
        seal = json.loads(Path(case['seal']).read_text())
        self.assertEqual((seal['evidence_label'], seal['operational']), ('OFFLINE_TESTED', False))
        documents = [seal, record] + h.read_lines(case['stage_log'])
        flags, texts = [], []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'operational':
                        flags.append(item)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                texts.append(value)
        walk(documents)
        self.assertTrue(flags)
        self.assertTrue(all(flag is False for flag in flags))
        self.assertFalse([t for t in texts if 'OPERATIONAL' in t.upper()])
        local = run_registry.inspect(case['folder'])
        self.assertEqual(local['state'], 'UNKNOWN')
        self.assertEqual(local['result']['evidence_label'], 'OFFLINE_TESTED')
        self.assertEqual(len(self.consumed_claims(case)), 2)
        # Job is gone (it exited), worker is gone.
        for identity in self.started:
            self.assertIn(run_recovery.observe(identity)[0], run_recovery.GONE_STATES)

        # RECOVER: terminal RECOVERY_FENCED, never COMPLETED, no resubmission.
        report = self.recover(case)
        self.assertEqual(report['local_state'], 'RECOVERY_FENCED')
        self.assertFalse(report['completed'])
        self.assertTrue(report['termination']['all_gone'])
        self.assertEqual(report['release']['reasons'], ['CONTAINMENT_UNAVAILABLE'])
        queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
        self.assertEqual((queue['phase'], queue['ambiguous_from']), ('AMBIGUOUS', 'CLAIMED'))
        self.assert_no_new_launch(case)
        self.assertEqual(self.submissions(case), [h.TASK_ID])
        self.assertNotIn(run_registry.inspect(case['folder'])['state'], run_ownership.VERIFIED_OUTCOMES)


class ApprovalTrustAnchorTests(E2EBase):
    """Item 3: no external anchor refuses; a TEST-ONLY temp anchor verifies; production cannot use it."""

    @timeout(180)
    def test_without_trust_anchor_validation_refuses_and_nothing_executes(self):
        case = self.new_case(containment='simulator', with_anchor=False)
        code, out, err = self.run_case(case)
        self.assertEqual(code, h.EXIT_REFUSED, out + err)
        self.assertIn('REFUSED VALIDATION TRUST_ANCHOR_UNPROVISIONED', out)
        self.assertEqual(self.stages(case)[-1], ('VALIDATION', 'REFUSED'))
        self.assertEqual(self.job_identities(case), [])
        self.assertFalse(Path(case['output']).exists())
        self.assertEqual(run_registry.inspect(case['folder'])['state'], 'CLAIMED')
        report = self.recover(case)
        self.assertEqual(report['local_state'], 'RECOVERY_FENCED')
        self.assertFalse(report['completed'])

    @timeout(60)
    def test_default_location_is_unprovisioned(self):
        with self.assertRaises(em.VerificationError) as ctx:
            ap.load_trust_anchor(ap.DEFAULT_TRUST_ANCHOR_DIR, repo_root=str(ROOT), run_dir=str(self.root),
                                 admin_uids=(0,), worker_uids=(os.getuid(),))
        self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_UNPROVISIONED)

    @timeout(60)
    def test_test_only_anchor_verifies_once_and_production_mode_refuses_it(self):
        case = self.new_case(containment='simulator', with_anchor=True)
        source, folder = case['source'], case['folder']
        anchor, state, store = h.load_approval_inputs(case, repo_root=source, run_dir=folder)
        self.assertTrue(anchor.test_only)
        self.assertTrue(anchor.anchor_id.startswith(ap.TEST_ONLY_PREFIX))
        expected = {'worker_id': h.WORKER_ID, 'run_id': h.RUN_ID, 'attempt_id': h.ATTEMPT_LABEL,
                    'manifest_sha256': case['manifest_sha256'], 'resource_class': 'cpu-diagnostic',
                    'scope': {'project': h.PROJECT, 'operation': 'diagnostic-execution'}, 'output': h.OUTPUT}
        approval = Path(case['approval']).read_bytes()
        revocations = Path(case['revocations']).read_bytes()
        record = ap.verify_approval(approval, anchor, revocation_bytes=revocations, revocation_state=state,
                                    expected=expected, store=store, test_capability=TEST_MODE)
        self.assertEqual(record['anchor_id'], h.ANCHOR_ID)
        with self.assertRaises(em.VerificationError) as ctx:
            ap.verify_approval(approval, anchor, revocation_bytes=revocations, revocation_state=state,
                               expected=expected, store=store, test_capability=TEST_MODE)
        self.assertEqual(ctx.exception.code, ap.APPROVAL_CONSUMED)
        # Production mode (test_only=False, the default) refuses the same temp anchor.
        for kwargs in ({}, {'admin_uids': (os.getuid(),), 'worker_uids': (os.getuid() + 1,)}):
            with self.assertRaises(em.VerificationError) as ctx:
                ap.load_trust_anchor(case['anchor'], repo_root=source, run_dir=folder, **kwargs)
            self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_INVALID)
        # test_only mode refuses an anchor outside the temp directory.
        with self.assertRaises(em.VerificationError) as ctx:
            ap.load_trust_anchor(ap.DEFAULT_TRUST_ANCHOR_DIR, repo_root=source, run_dir=folder, test_only=True)
        self.assertIn(ctx.exception.code, (ap.TRUST_ANCHOR_UNPROVISIONED, ap.TRUST_ANCHOR_INVALID))

    @timeout(30)
    def test_production_code_cannot_select_a_test_anchor(self):
        source = (SCRIPTS / 'approval.py').read_text()
        tree = ast.parse(source)
        # The default anchor location is a constant and nothing in approval.py reads the environment.
        self.assertEqual(ap.DEFAULT_TRUST_ANCHOR_DIR, '/etc/herdr-ml/trust/execution-approval')
        env_reads = [n for n in ast.walk(tree)
                     if (isinstance(n, ast.Attribute) and n.attr in ('environ', 'getenv', 'environb'))
                     or (isinstance(n, ast.Name) and n.id in ('environ', 'getenv'))]
        self.assertEqual(env_reads, [])
        load = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'load_trust_anchor')
        defaults = dict(zip([a.arg for a in load.args.kwonlyargs], load.args.kw_defaults))
        self.assertIsInstance(defaults['test_only'], ast.Constant)
        self.assertIs(defaults['test_only'].value, False)
        # No production script passes test_only=True or names a TEST-ONLY anchor, and only the
        # approval module itself calls the anchor loader (researchctl does not import approval).
        for path in sorted(SCRIPTS.glob('*.py')):
            module = ast.parse(path.read_text())
            for node in ast.walk(module):
                if isinstance(node, ast.keyword) and node.arg == 'test_only':
                    self.assertFalse(isinstance(node.value, ast.Constant) and node.value.value is True,
                                     '%s passes test_only=True' % path.name)
                if isinstance(node, ast.Call):
                    name = getattr(node.func, 'attr', getattr(node.func, 'id', None))
                    if name in ('load_trust_anchor', 'provision_revocation_state'):
                        self.assertEqual(path.name, 'approval.py', '%s calls %s' % (path.name, name))
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] + [getattr(node, 'module', None) or '']
                    if path.name == 'researchctl.py':
                        self.assertNotIn('approval', names)


class ArtifactSealTests(E2EBase):
    """Item 4: the sealed hash equals the recorded output hash; tampering after sealing fails."""

    @timeout(180)
    def test_seal_matches_recorded_output_and_tampering_fails(self):
        case = self.new_case(containment='simulator', with_anchor=True)
        code, out, err = self.run_case(case)
        self.assertEqual(code, 0, out + err)
        recorded = run_registry.inspect(case['folder'])['result']
        output = Path(case['output'])
        seal = h.verify_seal(case['seal'], output, manifest_sha256=case['manifest_sha256'], recorded=recorded)
        actual = em.sha256_hex(output.read_bytes())
        self.assertEqual(seal['artifact']['sha256'], actual)
        self.assertEqual(recorded['output_sha256'], actual)
        record = Path(case['result_record']).read_bytes()
        self.assertEqual(em.sha256_hex(record), recorded['result_record_sha256'])
        self.assertEqual(json.loads(record)['result_artifact']['sha256'], actual)
        self.assertEqual(seal['result_record_sha256'], recorded['result_record_sha256'])

        # Tamper with the output after sealing.
        original = output.read_bytes()
        output.write_bytes(original.replace(b'"byte_sum": ', b'"byte_sum": 1'))
        with self.assertRaises(h.SealError) as ctx:
            h.verify_seal(case['seal'], output, manifest_sha256=case['manifest_sha256'], recorded=recorded)
        self.assertEqual(ctx.exception.code, h.ARTIFACT_DIGEST_MISMATCH)
        output.write_bytes(original + b' ')
        with self.assertRaises(h.SealError) as ctx:
            h.verify_seal(case['seal'], output, manifest_sha256=case['manifest_sha256'], recorded=recorded)
        self.assertEqual(ctx.exception.code, h.ARTIFACT_DIGEST_MISMATCH)
        output.unlink()
        with self.assertRaises(h.SealError) as ctx:
            h.verify_seal(case['seal'], output, manifest_sha256=case['manifest_sha256'], recorded=recorded)
        self.assertEqual(ctx.exception.code, h.ARTIFACT_MISSING)
        output.write_bytes(original)
        h.verify_seal(case['seal'], output, manifest_sha256=case['manifest_sha256'], recorded=recorded)

        # Tamper with the seal itself, or present it for another manifest.
        seal_path = Path(case['seal'])
        sealed = json.loads(seal_path.read_bytes())
        sealed['artifact']['sha256'] = em.sha256_hex(b'forged')
        seal_path.write_bytes(em.canonical_bytes(sealed))
        with self.assertRaises(h.SealError) as ctx:
            h.verify_seal(seal_path, output, manifest_sha256=case['manifest_sha256'], recorded=recorded)
        self.assertEqual(ctx.exception.code, h.SEAL_DIGEST_MISMATCH)
        with self.assertRaises(h.SealError) as ctx:
            h.verify_seal(seal_path, output, manifest_sha256='0' * 64,
                          recorded=dict(recorded, seal_sha256=em.sha256_hex(seal_path.read_bytes())))
        self.assertEqual(ctx.exception.code, h.SEAL_BINDING_MISMATCH)
        report = self.recover(case)
        self.assertEqual(report['local_state'], 'RECOVERY_FENCED')


class InjectedBoundaryRecoveryTests(E2EBase):
    """Item 5: crash at each stage boundary, recover, assert an allowed terminal state; x3 each."""

    def check_boundary(self, label, point, stage, crashed_state, origin, consumed, job_alive, output, sealed):
        passes = 0
        for iteration in range(REPEATS):
            with self.subTest(boundary=label, iteration=iteration):
                case = self.new_case(containment='simulator', with_anchor=True, crash_point=point)
                code, out, err = self.run_case(case)
                self.assertEqual(code, h.EXIT_CRASH, out + err)
                self.assertIn('CRASHED ' + point, out)
                self.assertNotIn('DONE', out)
                before = run_registry.inspect(case['folder'])
                self.assertEqual(before['state'] if before else None, crashed_state)
                self.assertEqual(len(self.consumed_claims(case)), 2 if consumed else 0)
                jobs = self.job_identities(case)
                self.assertEqual(len(jobs), 1 if stage in ('EXECUTE', 'ARTIFACT_SEAL') else 0)
                if jobs:
                    self.assertEqual(run_recovery.observe(jobs[0])[0] == run_recovery.ALIVE, job_alive)
                self.assertEqual(Path(case['output']).exists(), output)
                self.assertEqual(Path(case['seal']).exists(), sealed)
                submitted = self.submissions(case)

                report = self.recover(case)
                again = self.recover(case)

                self.assertFalse(report['completed'])
                self.assertFalse(again['completed'])
                queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
                self.assertEqual((queue['phase'], queue['ambiguous_from']), ('AMBIGUOUS', origin))
                self.assertEqual(again['queue_phase'], 'AMBIGUOUS')
                local = run_registry.inspect(case['folder'])
                if crashed_state is None:
                    self.assertIsNone(local)
                    self.assertIsNone(report['termination'])
                else:
                    self.assertEqual(report['owner'], 'GONE')
                    self.assertEqual((local['state'], local['identity'], local['fence']),
                                     ('RECOVERY_FENCED', 'CURRENT', 2))
                    self.assertEqual(again['local_state'], 'RECOVERY_FENCED')
                    self.assertIn('CONTAINMENT_UNAVAILABLE', report['release']['reasons'])
                    if jobs:
                        # Recorded identities (leader child + descendant) were terminated and verified gone.
                        self.assertTrue(report['termination']['all_gone'], report['termination'])
                        self.assertTrue(report['termination']['entries'])
                        self.assertEqual(report['release']['reasons'], ['CONTAINMENT_UNAVAILABLE'])
                    else:
                        # Nothing was ever launched, so nothing is recorded and nothing is signalled.
                        self.assertEqual(report['termination']['entries'], [])
                        self.assertEqual(report['release']['reasons'],
                                         ['NO_RECORDED_DESCENDANTS', 'CONTAINMENT_UNAVAILABLE'])
                    if local['result'] is not None:
                        self.assertEqual(local['result']['state'], 'UNKNOWN')
                    events = [e['kind'] for e in run_recovery.inspect_recovery(case['folder'])['events']]
                    self.assertIn('RECOVERY_FENCED', events)
                    self.assertNotIn('RELEASE_GATE_PASSED', events)
                    self.assertNotIn(local['state'], run_ownership.VERIFIED_OUTCOMES)
                if sealed:
                    h.verify_seal(case['seal'], case['output'], manifest_sha256=case['manifest_sha256'],
                                  recorded=local['result'])
                # Every recorded job is gone right after recovery, before any teardown cleanup.
                for job in jobs:
                    self.assertIn(run_recovery.observe(job)[0], run_recovery.GONE_STATES)
                self.assertEqual(process_tree.wait_no_marked(self.marker), [])
                # No resubmission, relaunch or reclaim; the approval cannot be reused.
                self.assertEqual(self.submissions(case), submitted)
                self.assertLessEqual(len(submitted), 1)
                self.assert_no_new_launch(case)
                self.assertEqual(len(self.consumed_claims(case)), 2 if consumed else 0)
                passes += 1
        PASS_COUNTS[label] = passes
        self.assertEqual(passes, REPEATS)

    @timeout(300)
    def test_crash_after_intent(self):
        self.check_boundary(*BOUNDARIES[0])

    @timeout(300)
    def test_crash_after_bind(self):
        self.check_boundary(*BOUNDARIES[1])

    @timeout(300)
    def test_crash_after_queue_claim(self):
        self.check_boundary(*BOUNDARIES[2])

    @timeout(300)
    def test_crash_after_local_claim(self):
        self.check_boundary(*BOUNDARIES[3])

    @timeout(300)
    def test_crash_after_validation_before_dispatch(self):
        self.check_boundary(*BOUNDARIES[4])

    @timeout(300)
    def test_crash_with_job_running(self):
        self.check_boundary(*BOUNDARIES[5])

    @timeout(300)
    def test_crash_after_job_exit_before_seal(self):
        self.check_boundary(*BOUNDARIES[6])

    @timeout(300)
    def test_crash_after_seal_recorded(self):
        self.check_boundary(*BOUNDARIES[7])

    @timeout(300)
    def test_crash_before_release(self):
        self.check_boundary(*BOUNDARIES[8])

    @classmethod
    def tearDownClass(cls):
        if os.environ.get('HERDR_E2E_REPORT'):
            sys.stderr.write('E2E_BOUNDARY_PASSES %s\n' % json.dumps(PASS_COUNTS, sort_keys=True))


class TimeoutCoverageTests(unittest.TestCase):
    @timeout(30)
    def test_every_e2e_test_has_a_timeout(self):
        module = sys.modules[__name__]
        missing = []
        for name in dir(module):
            cls = getattr(module, name)
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
                for attr in dir(cls):
                    if attr.startswith('test_') and not hasattr(getattr(cls, attr), 'timeout_seconds'):
                        missing.append('%s.%s' % (name, attr))
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
