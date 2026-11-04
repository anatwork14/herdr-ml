"""Negative and positive tests for the production worker execution boundary.

Test-only fixtures live in per-test temporary directories. The approval gate and
the containment backend below are test doubles. The production module keeps
fail-closed defaults and no registered containment issuer. Each test has a
watchdog alarm and kills only processes it started. Markers are unique per test.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import containment_backend as cb  # noqa: E402
import execution_boundary as eb  # noqa: E402
import execution_manifest as em  # noqa: E402
import run_ownership  # noqa: E402
import run_registry as registry  # noqa: E402
from support import gated_launcher  # noqa: E402

GIT = ['git', '-c', 'user.name=test-only', '-c', 'user.email=test-only@example.invalid',
       '-c', 'commit.gpgsign=false']
ATTEMPT = '0123456789abcdef0123456789abcdef'
OTHER_ATTEMPT = 'fedcba9876543210fedcba9876543210'
QUEUE = 'c' * 64
TEST_ISSUER = object()
TIMEOUT_S = 30


def git(cwd, *args):
    subprocess.run(GIT + ['-C', str(cwd), *args], check=True, capture_output=True)


class _TestProof:
    def __init__(self, issuer, run_id):
        self.issuer = issuer
        self.run_id = run_id


class _VerifiedContainmentDouble:
    """Test double only. Verified solely under a patched test issuer set."""

    def establish(self, command, *, run_id):
        del command
        return cb.ContainmentResult(status=cb.ContainmentStatus.VERIFIED, reason='test double',
                                    proof=_TestProof(TEST_ISSUER, run_id))

    def spawn_gated(self, command, *, cwd, env, run_id):
        del run_id
        # Test-only launcher: spawning is available only through tests/support.
        child = gated_launcher.spawn_gated(command, cwd=cwd, env=env)
        self.last_child = child.proc
        return child


class _GrantingGate:
    def __init__(self, decision=None):
        self.decision = decision or eb.ApprovalDecision.GRANTED
        self.calls = []

    def __call__(self, run_id, attempt_id, manifest_digest):
        self.calls.append((run_id, attempt_id, manifest_digest))
        return self.decision


class _CommitDouble:
    """Records consumption. Stands in for W3's single-use claim in tests only."""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, run_id, attempt_id, manifest_digest):
        self.calls.append((run_id, attempt_id, manifest_digest))
        return self.result


class _RaisingGate:
    def __call__(self, run_id, attempt_id, manifest_digest):
        raise RuntimeError('authorizer fault')


def verified_issuers():
    return mock.patch.object(cb, '_VERIFIED_ISSUERS', frozenset([TEST_ISSUER]))


class BoundaryFixture(unittest.TestCase):
    def setUp(self):
        def _timeout(_signum, _frame):
            raise TimeoutError('test watchdog expired')
        previous = signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(120)
        self.addCleanup(lambda: (signal.alarm(0), signal.signal(signal.SIGALRM, previous)))
        self.started_pids = []
        self.addCleanup(self._kill_started)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(os.path.realpath(temp.name))
        self.home = self.temp / 'queue-home'
        self.home.mkdir(mode=0o700)
        self.source = self.temp / 'source'
        self.data = self.temp / 'data'
        self.source.mkdir()
        self.data.mkdir()
        self.marker_name = 'bnd-marker-%s' % uuid.uuid4().hex
        self.marker = self.temp / self.marker_name
        self.outside = self.temp / 'outside'
        self.outside.mkdir()
        (self.source / 'train.py').write_text(
            'import sys\n'
            'open(sys.argv[1], "w").close()\n'
            'print("ok")\n')
        git(self.source, 'init', '-q')
        git(self.source, 'add', '-A')
        git(self.source, 'commit', '-q', '-m', 'test-only fixture')
        self.config = self.data / 'config.json'
        self.config.write_text('{"lr": 0.1}\n')
        self.dataset = self.data / 'dataset.bin'
        self.dataset.write_bytes(b'dataset-bytes-v1')
        self.dataset_manifest = self.data / 'dataset_manifest.json'
        self.dataset_manifest.write_text('{"files": 1}\n')
        self.split = self.data / 'split.json'
        self.split.write_text('{"train": [0], "test": [1]}\n')
        self.client = self.data / 'client.json'
        self.client.write_text('{"clients": ["a", "b"]}\n')
        self.spec = {
            'run_id': 'run-1', 'attempt_id': ATTEMPT, 'project': 'demo',
            'operation': 'diagnostic-execution', 'resource_class': 'cpu-diagnostic',
            'source_root': str(self.source), 'source_files': ['train.py'],
            'args': ['-I', 'train.py', str(self.marker)], 'cwd': '.',
            'config': str(self.config),
            'inputs': {'dataset_content': str(self.dataset),
                       'dataset_manifest': str(self.dataset_manifest),
                       'split_manifest': str(self.split),
                       'client_manifest': str(self.client)},
            'primary_seed': 7, 'named_seeds': {'numpy': 7}, 'nondeterminism': [],
            'output_root': 'run-output', 'output_path': 'result.json'}
        self.manifest = em.build_manifest(**self.spec)
        self.data_bytes = em.encode_manifest(self.manifest)
        self.digest = em.sha256_hex(self.data_bytes)
        self.folder = self.temp / '.runtime' / 'demo' / 'runs' / 'run-1'
        self.folder.mkdir(parents=True)
        self.output = self.folder / 'run-output' / 'result.json'
        self.token = None

    def _kill_started(self):
        for pid in self.started_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def claim_local(self):
        with mock.patch.object(run_ownership, 'new_attempt_id', return_value=ATTEMPT):
            self.token = registry.claim(self.folder, 'run-1', 'demo', self.digest, 'SUBMITTED')
        return self.token

    def claim_queued(self):
        task = 'task-%s' % uuid.uuid4().hex[:8]
        with mock.patch.object(run_ownership, 'new_attempt_id', return_value=ATTEMPT):
            token = registry.queue_intent(self.folder, 'run-1', self.digest, QUEUE, _store_home=self.home)
            registry.queue_bind_task(self.folder, token, task, _store_home=self.home)
            fence = registry.queue_claim(self.folder, 'run-1', self.digest, QUEUE, task, 'worker-A',
                                         _store_home=self.home)
            self.token = registry.claim(self.folder, 'run-1', 'demo', self.digest, 'QUEUED',
                                        queue_instance=QUEUE, queue_task_id=task,
                                        queue_worker_id='worker-A', queue_fence=fence,
                                        _store_home=self.home)
        return self.token

    def request(self, **changes):
        base = dict(run_folder=str(self.folder), run_id='run-1', attempt_id=ATTEMPT,
                    fence_token=self.token, worker_id='worker-1', manifest_bytes=self.data_bytes,
                    source_root=str(self.source), seal_dir=self.seal_dir(), timeout_s=TIMEOUT_S)
        base.update(changes)
        return eb.LaunchRequest(**base)

    def seal_dir(self):
        path = tempfile.mkdtemp(dir=str(self.temp))
        return path

    def assertRefused(self, code, check, func, *args, planted=False, **kwargs):
        with self.assertRaises(eb.BoundaryRefusal) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.detail)
        self.assertEqual(ctx.exception.check, check, ctx.exception.detail)
        self.assertNoLaunch(planted=planted)

    def assertNoLaunch(self, planted=False):
        self.assertFalse(self.marker.exists(), 'job marker must not exist')
        if not planted:
            self.assertFalse(self.output.exists(), 'output artifact must not exist')
            self.assertFalse(self.output.parent.exists() and any(self.output.parent.iterdir()))
        self.assertEqual(self.pgrep_marker(), [], 'no process may carry the test marker')
        state = registry.inspect(self.folder)
        if state is not None:
            self.assertEqual(state['state'], 'CLAIMED', 'refusal must not advance or consume the run')

    def pgrep_marker(self):
        result = subprocess.run(['pgrep', '-f', self.marker_name], capture_output=True, text=True,
                                timeout=TIMEOUT_S)
        if result.returncode == 1:
            return []
        self.assertIn(result.returncode, (0, 1))
        return [int(p) for p in result.stdout.split()]


class DirectInvocationTests(BoundaryFixture):
    def test_default_approval_refuses_before_containment_and_launches_nothing(self):
        self.claim_local()
        self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, eb.launch, self.request())

    def test_production_containment_refuses_and_launches_nothing(self):
        self.claim_local()
        gate = _GrantingGate()
        self.assertRefused(eb.CONTAINMENT_UNAVAILABLE, 6, eb.launch, self.request(),
                           approval_gate=gate)
        self.assertEqual(len(gate.calls), 1)

    def test_no_public_flag_or_environment_selects_the_boundary(self):
        source = (SCRIPTS / 'execution_boundary.py').read_text()
        for forbidden in ('environ', 'getenv', 'argparse', '__main__', 'sys.argv'):
            self.assertNotIn(forbidden, source)


class CheckRefusalTests(BoundaryFixture):
    def test_check1_mismatched_run_refuses(self):
        self.claim_local()
        self.assertRefused(eb.IDENTITY_MISMATCH, 1, eb.launch,
                           self.request(run_id='run-2'), approval_gate=_GrantingGate())

    def test_check1_mismatched_attempt_refuses(self):
        self.claim_local()
        self.assertRefused(eb.IDENTITY_MISMATCH, 1, eb.launch,
                           self.request(attempt_id=OTHER_ATTEMPT), approval_gate=_GrantingGate())

    def test_check1_missing_registry_refuses(self):
        self.assertRefused(eb.IDENTITY_MISMATCH, 1, eb.launch,
                           self.request(fence_token='v2.%s.1' % ATTEMPT), approval_gate=_GrantingGate())

    def test_check2_stale_fence_refuses(self):
        self.claim_local()
        stale = run_ownership.encode_token(ATTEMPT, 2)
        self.assertRefused(eb.FENCE_STALE, 2, eb.launch,
                           self.request(fence_token=stale), approval_gate=_GrantingGate())

    def test_check2_forged_token_refuses(self):
        self.claim_local()
        self.assertRefused(eb.FENCE_STALE, 2, eb.launch,
                           self.request(fence_token='not-a-token'), approval_gate=_GrantingGate())

    def test_check3_queued_run_refuses_with_queue_not_enabled(self):
        self.claim_queued()
        self.assertRefused(eb.QUEUE_NOT_ENABLED, 3, eb.launch,
                           self.request(), approval_gate=_GrantingGate())

    def test_check4_mutated_manifest_bytes_refuse(self):
        self.claim_local()
        mutated = self.data_bytes.replace(b'"seeds"', b'"seedz"', 1)
        self.assertNotEqual(mutated, self.data_bytes)
        self.assertRefused(eb.MANIFEST_MISMATCH, 4, eb.launch,
                           self.request(manifest_bytes=mutated), approval_gate=_GrantingGate())

    def test_check4_mutated_source_file_refuses(self):
        self.claim_local()
        (self.source / 'train.py').write_text('print("changed")\n')
        self.assertRefused(eb.MANIFEST_MISMATCH, 4, eb.launch,
                           self.request(), approval_gate=_GrantingGate())

    def test_check4_mutated_input_refuses(self):
        self.claim_local()
        self.dataset.write_bytes(b'dataset-bytes-v2')
        self.assertRefused(eb.MANIFEST_MISMATCH, 4, eb.launch,
                           self.request(), approval_gate=_GrantingGate())

    def test_check5_authorizer_fault_refuses(self):
        self.claim_local()
        self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, eb.launch,
                           self.request(), approval_gate=_RaisingGate())

    def test_check5_denied_decision_refuses(self):
        self.claim_local()
        gate = _GrantingGate(eb.ApprovalDecision.DENIED)
        self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, eb.launch,
                           self.request(), approval_gate=gate)

    def test_check6_unverified_containment_refuses_and_never_commits_approval(self):
        self.claim_local()
        commit = _CommitDouble()
        with verified_issuers():
            self.assertRefused(eb.CONTAINMENT_UNAVAILABLE, 6, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               approval_commit=commit, containment=_NullContainment())
        self.assertEqual(commit.calls, [], 'a refused request must not consume the grant')

    def test_check7_refusal_never_commits_approval(self):
        self.claim_local()
        self.output.parent.mkdir(mode=0o700)
        os.chmod(self.output.parent, 0o777)
        commit = _CommitDouble()
        with verified_issuers():
            self.assertRefused(eb.OUTPUT_DESTINATION_INVALID, 7, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               approval_commit=commit, containment=_VerifiedContainmentDouble())
        self.assertEqual(commit.calls, [])

    def test_missing_or_failed_commit_refuses_at_check5_without_launch(self):
        self.claim_local()
        with verified_issuers():
            self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               containment=_VerifiedContainmentDouble())
            self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               approval_commit=_CommitDouble(result=False),
                               containment=_VerifiedContainmentDouble())

    def test_check7_symlinked_output_target_refuses(self):
        self.claim_local()
        self.output.parent.mkdir(mode=0o700)
        target = self.outside / 'result.json'
        target.write_text('planted')
        self.output.symlink_to(target)
        with verified_issuers():
            self.assertRefused(eb.OUTPUT_DESTINATION_INVALID, 7, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               containment=_VerifiedContainmentDouble(), planted=True)
        self.assertEqual(target.read_text(), 'planted')

    def test_check7_symlinked_output_directory_refuses(self):
        self.claim_local()
        (self.folder / 'run-output').symlink_to(self.outside, target_is_directory=True)
        with verified_issuers():
            self.assertRefused(eb.OUTPUT_DESTINATION_INVALID, 7, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               containment=_VerifiedContainmentDouble())
        self.assertFalse((self.outside / 'result.json').exists())

    def test_check7_group_or_world_writable_destination_dir_refuses(self):
        self.claim_local()
        self.output.parent.mkdir(mode=0o700)
        os.chmod(self.output.parent, 0o777)
        with verified_issuers():
            self.assertRefused(eb.OUTPUT_DESTINATION_INVALID, 7, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               containment=_VerifiedContainmentDouble())

    def test_check7_existing_output_refuses(self):
        self.claim_local()
        self.output.parent.mkdir(mode=0o700)
        self.output.write_text('earlier')
        with verified_issuers():
            self.assertRefused(eb.OUTPUT_DESTINATION_INVALID, 7, eb.launch,
                               self.request(), approval_gate=_GrantingGate(),
                               containment=_VerifiedContainmentDouble(), planted=True)
        self.assertEqual(self.output.read_text(), 'earlier')


class _NullContainment:
    def establish(self, command, *, run_id):
        del command, run_id
        return cb.ContainmentResult(status=cb.ContainmentStatus.UNAVAILABLE, reason='test: unavailable')


class PositivePathTests(BoundaryFixture):
    def test_all_checks_pass_then_launch_and_write_output_after_launch(self):
        self.claim_local()
        gate = _GrantingGate()
        commit = _CommitDouble()
        with verified_issuers():
            outcome = eb.launch(self.request(), approval_gate=gate, approval_commit=commit,
                                containment=_VerifiedContainmentDouble())
        self.started_pids.append(outcome.pid)
        self.assertEqual(commit.calls, [('run-1', ATTEMPT, self.digest)])
        self.assertEqual(outcome.exit_status, 0)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.output.read_text(), 'ok\n')
        self.assertEqual(stat_mode(self.output) & 0o077, 0)
        self.assertEqual(gate.calls, [('run-1', ATTEMPT, self.digest)])
        self.assertEqual(registry.inspect(self.folder)['state'], 'UNKNOWN')

    def test_second_launch_with_same_token_is_refused_as_stale(self):
        self.claim_local()
        with verified_issuers():
            outcome = eb.launch(self.request(), approval_gate=_GrantingGate(),
                                approval_commit=_CommitDouble(),
                                containment=_VerifiedContainmentDouble())
        self.started_pids.append(outcome.pid)
        self.assertEqual(self.pgrep_marker(), [])
        with verified_issuers():
            with self.assertRaises(eb.BoundaryRefusal) as ctx:
                eb.launch(self.request(), approval_gate=_GrantingGate(),
                          containment=_VerifiedContainmentDouble())
        self.assertEqual(ctx.exception.code, eb.FENCE_STALE)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


class IsolationTests(unittest.TestCase):
    def test_module_imports_no_test_support_or_simulator(self):
        source = (SCRIPTS / 'execution_boundary.py').read_text()
        self.assertNotIn('tests', source)
        self.assertNotIn('support', source)
        self.assertNotIn('containment_sim', source)
        code = ("import sys; sys.path.insert(0, %r); import execution_boundary; "
                "print('\\n'.join(sorted(m for m in sys.modules if 'support' in m or 'sim' in m)))"
                % str(SCRIPTS))
        result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True,
                                timeout=TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')

    def test_no_other_script_imports_the_boundary(self):
        for path in SCRIPTS.glob('*.py'):
            if path.name == 'execution_boundary.py':
                continue
            self.assertNotIn('execution_boundary', path.read_text(), path.name)


if __name__ == '__main__':
    unittest.main()
