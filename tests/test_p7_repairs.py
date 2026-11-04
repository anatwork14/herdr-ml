"""Regression tests for the P7 review repairs R1-R6. Markers unique per test; each test kills only
processes it started."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
import uuid
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / 'tests'))
import approval as ap  # noqa: E402
import containment_backend as cb  # noqa: E402
import execution_boundary as eb  # noqa: E402
import dispatch_guard  # noqa: E402
import queue_adapter  # noqa: E402
import researchctl  # noqa: E402
import run_recovery  # noqa: E402
import run_registry as registry  # noqa: E402
import test_execution_boundary as teb  # noqa: E402
from support import gated_launcher  # noqa: E402

DIGEST = 'a' * 64

OWNER_SRC = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, %r)
    import run_registry
    run_registry.claim(sys.argv[1], 'run-1', 'project-a', %r, 'SUBMITTED')
    sys.stdout.write('claimed\\n'); sys.stdout.flush()
    time.sleep(60)
''' % (str(SCRIPTS), DIGEST))


class OwnerLivenessFenceTests(unittest.TestCase):
    """R1: recovery_fence refuses a live owner and writes nothing."""

    def test_live_owner_is_refused_by_recovery_fence_and_never_signalled(self):
        marker = 'own-marker-%s' % uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(os.path.realpath(tmp)) / '.runtime' / 'project-a' / 'runs' / 'run-1'
            folder.mkdir(parents=True)
            owner = subprocess.Popen([sys.executable, '-I', '-c', OWNER_SRC, str(folder), marker],
                                     stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'claimed')
                entry = registry.inspect(folder)
                self.assertEqual(entry['owner_pid'], owner.pid)
                before = (folder / 'registry.sqlite3').read_bytes()
                with self.assertRaises(registry.OwnerLive) as ctx:
                    registry.recovery_fence(folder, (entry['owner_pid'], entry['owner_boot'],
                                                     entry['owner_start']), 'test')
                self.assertEqual(ctx.exception.code, registry.OWNER_LIVE)
                self.assertEqual(registry.inspect(folder)['state'], 'CLAIMED')
                self.assertEqual(registry.inspect(folder)['fence'], entry['fence'])
                self.assertEqual(before, (folder / 'registry.sqlite3').read_bytes())
                run_recovery.recover(folder, queued=False, grace_s=0.2)
                self.assertIsNone(owner.poll(), 'recover() must not signal a live owner')
                self.assertEqual(registry.inspect(folder)['state'], 'CLAIMED')
            finally:
                owner.kill()
                owner.wait(timeout=10)
                owner.stdout.close()

    def test_undeterminable_owner_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(os.path.realpath(tmp)) / '.runtime' / 'project-a' / 'runs' / 'run-1'
            folder.mkdir(parents=True)
            registry.claim(folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED')
            entry = registry.inspect(folder)
            with mock.patch.object(registry.process_identity, 'observe_process',
                                   return_value={'observation': 'UNKNOWN', 'reason': 'test'}):
                with self.assertRaises(registry.OwnerLive):
                    registry.recovery_fence(folder, (entry['owner_pid'], entry['owner_boot'],
                                                     entry['owner_start']), 'test')
            self.assertEqual(registry.inspect(folder)['state'], 'CLAIMED')


class GatedSpawnTests(teb.BoundaryFixture):
    """R2 and R3."""

    def launch(self, backend, **kw):
        with teb.verified_issuers():
            return eb.launch(self.request(), approval_gate=teb._GrantingGate(),
                             approval_commit=teb._CommitDouble(), containment=backend, **kw)

    def test_record_failure_kills_child_and_leaves_no_output(self):
        self.claim_local()
        backend = teb._VerifiedContainmentDouble()
        with mock.patch.object(registry, 'record_child', side_effect=registry.RegistryError('injected')):
            with self.assertRaises(eb.BoundaryRefusal) as ctx:
                self.launch(backend)
        self.assertEqual(ctx.exception.code, eb.LAUNCH_RECORD_FAILED)
        child = backend.last_child
        self.started_pids.append(child.pid)
        self.assertIsNotNone(child.returncode, 'child must be reaped')
        self.assertFalse(Path('/proc/%d' % child.pid).exists(), 'child pid must be gone')
        self.assertFalse(self.marker.exists(), 'the job must never have run')
        self.assertFalse(self.output.exists())
        self.assertEqual(self.pgrep_marker(), [])

    def test_child_is_blocked_until_identity_recorded(self):
        self.claim_local()
        backend = teb._VerifiedContainmentDouble()
        seen = {}
        real = registry.record_child

        def spy(folder, token, pid):
            seen['marker_before_record'] = self.marker.exists()
            return real(folder, token, pid)
        with mock.patch.object(registry, 'record_child', side_effect=spy):
            outcome = self.launch(backend)
        self.started_pids.append(outcome.pid)
        self.assertFalse(seen['marker_before_record'])
        self.assertTrue(self.marker.exists())

    def test_production_backend_cannot_spawn_even_if_checks_1_to_5_pass(self):
        self.claim_local()
        backend = cb.get_backend()
        with self.assertRaises(cb.ContainmentUnavailable):
            backend.spawn_gated([sys.executable, '-c', 'pass'], cwd=str(self.temp), env={}, run_id='run-1')
        with mock.patch.object(eb, 'check_containment'):  # checks 1-5 and 7 pass, check 6 skipped
            with self.assertRaises(eb.BoundaryRefusal) as ctx:
                self.launch(backend)
        self.assertEqual(ctx.exception.code, eb.CONTAINMENT_UNAVAILABLE)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(self.pgrep_marker(), [])

    def test_boundary_source_has_no_direct_spawn(self):
        source = (SCRIPTS / 'execution_boundary.py').read_text()
        self.assertNotIn('Popen(', source)


class DispatchDisabledTests(unittest.TestCase):
    """R6."""

    def test_constant_is_false(self):
        self.assertIs(dispatch_guard.PRODUCTION_DISPATCH_ENABLED, False)
        self.assertIs(queue_adapter.PRODUCTION_DISPATCH_ENABLED, False)

    def test_import_time_check_refuses_when_flag_is_true(self):
        # Import-time enforcement: execute the module source with the flag flipped to True.
        code = ("import sys; src = open(%r).read().replace('PRODUCTION_DISPATCH_ENABLED = False', "
                "'PRODUCTION_DISPATCH_ENABLED = True', 1); exec(compile(src, 'dispatch_guard', 'exec'))"
                % str(SCRIPTS / 'dispatch_guard.py'))
        code_guard = ("import sys; sys.path.insert(0, %r); import dispatch_guard; "
                      "dispatch_guard.PRODUCTION_DISPATCH_ENABLED = True; dispatch_guard.require_disabled()"
                      % str(SCRIPTS))
        for snippet in (code, code_guard):
            result = subprocess.run([sys.executable, '-I', '-c', snippet], capture_output=True, text=True,
                                    timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DispatchEnabled', result.stderr)

    def test_boundary_and_researchctl_refuse_when_flag_is_true(self):
        with mock.patch.object(dispatch_guard, 'PRODUCTION_DISPATCH_ENABLED', True):
            with self.assertRaises(eb.BoundaryRefusal) as ctx:
                eb.launch(object())
            self.assertEqual(ctx.exception.code, eb.DISPATCH_ENABLED)
            with self.assertRaises(researchctl.ResearchError):
                researchctl.require_supported_execution('diagnostic', 'local', submission=True)


if __name__ == '__main__':
    unittest.main()
