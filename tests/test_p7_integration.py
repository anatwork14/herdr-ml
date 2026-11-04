"""P7 integration tests: approval adapter on the boundary and the late review fixes.

All keys are generated per test inside a 0700 temporary directory and labelled TEST-ONLY.
Markers are unique per test and every test has a watchdog (see BoundaryFixture).
"""
import datetime as dt
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
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
import execution_boundary as eb  # noqa: E402
import process_identity  # noqa: E402
import researchctl  # noqa: E402
import run_recovery  # noqa: E402
import run_registry as registry  # noqa: E402
import test_approval_consistency as tac  # noqa: E402
import test_execution_boundary as teb  # noqa: E402

NOW_OFFSET = dt.timedelta(minutes=1)


def tree_snapshot(directory):
    result = {}
    for root, dirs, files in os.walk(directory):
        for name in sorted(dirs + files):
            path = os.path.join(root, name)
            info = os.lstat(path)
            data = b''
            if stat.S_ISREG(info.st_mode):
                with open(path, 'rb') as handle:
                    data = handle.read()
            result[path] = (data, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    return result


class AdapterFixture(teb.BoundaryFixture):
    def setUp(self):
        super().setUp()
        self.claim_local()
        self.run_dir = self.temp / 'approval-run'
        self.run_dir.mkdir()
        self.store_dir = self.temp / 'consumed'
        self.store_dir.mkdir(mode=0o700)
        self.state_dir = self.temp / 'state'
        self.state_dir.mkdir(mode=0o700)
        opts = {'repo_root': REPO, 'run_dir': self.run_dir, 'test_only': True, 'test_root': str(self.temp)}
        self.anchor = ap.load_trust_anchor(str(tac.write_anchor(self.temp / 'TEST-ONLY-trust')), **opts)
        ap.provision_revocation_state(str(self.state_dir), anchor_id=tac.ANCHOR_ID)
        self.rev_state = ap.RevocationState(str(self.state_dir), anchor_id=tac.ANCHOR_ID, **opts)
        self.store = ap.ConsumptionStore(str(self.store_dir), **opts)
        self.rev_bytes = ap.issue_revocation(self.anchor, issued_at=self.now() - dt.timedelta(hours=2),
                                             sequence=1, approval_ids=(), manifest_sha256s=())
        self.ctx = ap.AttemptContext(store=self.store, anchor=self.anchor, revocation_state=self.rev_state,
                                     revocations=lambda: self.rev_bytes, worker_id='worker-1',
                                     resource_class='cpu-diagnostic', scope=tac.SCOPE, output=tac.OUTPUT)
        self.approval = ap.issue_approval(
            self.anchor, 'approver-a', approval_id='appr-1', worker_id='worker-1', run_id='run-1',
            attempt_id=teb.ATTEMPT, manifest_sha256=self.digest, resource_class='cpu-diagnostic',
            scope=tac.SCOPE, output=tac.OUTPUT, issued_at=self.now() - NOW_OFFSET,
            expires_at=self.now() + dt.timedelta(hours=1))
        self.adapter = ap.BoundaryApprovalAdapter(ctx=self.ctx, approval=self.approval,
                                                  decisions=eb.ApprovalDecision, clock=self.now)

    @staticmethod
    def now():
        return dt.datetime.now(dt.timezone.utc)

    def launch(self, adapter=None, **extra):
        adapter = adapter or self.adapter
        with teb.verified_issuers():
            return eb.launch(self.request(), approval_gate=adapter.gate, approval_commit=adapter.commit,
                             approval_launched=adapter.launched, containment=teb._VerifiedContainmentDouble(),
                             **extra)


class ApprovalAdapterTests(AdapterFixture):
    def test_gate_grants_and_writes_nothing_bytes_unchanged(self):
        before = (tree_snapshot(self.store_dir), tree_snapshot(self.state_dir))
        decision = self.adapter.gate('run-1', teb.ATTEMPT, self.digest)
        self.assertIs(decision, eb.ApprovalDecision.GRANTED)
        self.assertEqual(before, (tree_snapshot(self.store_dir), tree_snapshot(self.state_dir)))
        self.assertEqual(self.state_dir.joinpath(ap.REVOCATION_STATE_FILE).read_bytes(),
                         before[1][str(self.state_dir / ap.REVOCATION_STATE_FILE)][0])
        self.assertEqual(self.store.read_attempt('run-1', teb.ATTEMPT), None)

    def test_gate_refuses_wrong_manifest_revoked_and_already_consumed(self):
        self.assertIs(self.adapter.gate('run-1', teb.ATTEMPT, 'b' * 64), eb.ApprovalDecision.UNAVAILABLE)
        self.rev_bytes = ap.issue_revocation(self.anchor, issued_at=self.now() - dt.timedelta(minutes=5),
                                             sequence=2, approval_ids=['appr-1'], manifest_sha256s=())
        before = tree_snapshot(self.state_dir)
        self.assertIs(self.adapter.gate('run-1', teb.ATTEMPT, self.digest), eb.ApprovalDecision.UNAVAILABLE)
        self.assertEqual(before, tree_snapshot(self.state_dir), 'a refusing gate must not advance state')
        self.rev_bytes = ap.issue_revocation(self.anchor, issued_at=self.now() - dt.timedelta(hours=2),
                                             sequence=1, approval_ids=(), manifest_sha256s=())
        self.assertTrue(self.adapter.commit('run-1', teb.ATTEMPT, self.digest))
        self.assertIs(self.adapter.gate('run-1', teb.ATTEMPT, self.digest), eb.ApprovalDecision.UNAVAILABLE)

    def test_gate_rejects_rolled_back_revocation_sequence_without_writing(self):
        self.rev_state.advance(5)
        before = tree_snapshot(self.state_dir)
        self.assertIs(self.adapter.gate('run-1', teb.ATTEMPT, self.digest), eb.ApprovalDecision.UNAVAILABLE)
        self.assertEqual(before, tree_snapshot(self.state_dir))

    def test_boundary_never_spawns_when_commit_fails(self):
        with mock.patch.object(ap, 'consume_for_attempt',
                               side_effect=ap.VerificationError(ap.APPROVAL_CONSUMED, 'injected')):
            self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, self.launch)
        self.assertIsNone(self.store.read_attempt('run-1', teb.ATTEMPT))

    def test_boundary_never_spawns_when_commit_returns_non_success(self):
        for bad in (None, False, {}, {'approval_sha256': ''}):
            with self.subTest(result=bad), mock.patch.object(ap, 'consume_for_attempt', return_value=bad):
                self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, self.launch)

    def test_commit_only_after_all_checks_and_gate_alone_never_consumes(self):
        # check 7 fails (output exists): gate ran, commit must not have.
        self.output.parent.mkdir(mode=0o700)
        self.output.write_text('planted')
        with mock.patch.object(ap, 'consume_for_attempt', wraps=ap.consume_for_attempt) as consume:
            self.assertRefused(eb.OUTPUT_DESTINATION_INVALID, 7, self.launch, planted=True)
        consume.assert_not_called()
        self.assertEqual(os.listdir(self.store_dir), [])

    def test_full_launch_consumes_once_then_marks_launched(self):
        outcome = self.launch()
        self.started_pids.append(outcome.pid)
        self.assertEqual(outcome.exit_status, 0)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.store.read_attempt('run-1', teb.ATTEMPT)['state'], ap.STATE_LAUNCHED)

    def test_default_provider_stays_fail_closed(self):
        with teb.verified_issuers():
            self.assertRefused(eb.APPROVAL_UNAVAILABLE, 5, eb.launch, self.request(),
                               containment=teb._VerifiedContainmentDouble())
        self.assertIs(eb.fail_closed_approval_gate('run-1', teb.ATTEMPT, self.digest),
                      eb.ApprovalDecision.UNAVAILABLE)
        self.assertIs(eb.fail_closed_approval_commit('run-1', teb.ATTEMPT, self.digest), False)


class TestAnchorContextTests(AdapterFixture):
    """LOW-1: test anchors need an explicit, private, owned test context."""

    def opts(self, root):
        return {'repo_root': REPO, 'run_dir': self.run_dir, 'test_only': True, 'test_root': str(root)}

    def test_test_root_must_be_0700_owned_directory(self):
        loose = self.temp / 'loose'
        loose.mkdir()
        os.chmod(loose, 0o755)
        trust = tac.write_anchor(loose / 'TEST-ONLY-trust')
        with self.assertRaises(ap.VerificationError) as ctx:
            ap.load_trust_anchor(str(trust), **self.opts(loose))
        self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_INVALID)
        os.chmod(loose, 0o700)
        ap.load_trust_anchor(str(trust), **self.opts(loose))
        for bad in ('/', str(trust / 'anchor.json'), str(self.temp / 'missing')):
            with self.subTest(root=bad), self.assertRaises(ap.VerificationError):
                ap.load_trust_anchor(str(trust), **self.opts(bad))

    def test_state_directories_also_require_a_private_test_root(self):
        loose = self.temp / 'loose2'
        loose.mkdir()
        os.chmod(loose, 0o755)
        state = loose / 'state'
        state.mkdir(mode=0o700)
        with self.assertRaises(ap.VerificationError):
            ap.ConsumptionStore(str(state), **self.opts(loose))

    def test_test_anchor_with_non_test_state_is_refused_before_any_claim(self):
        fake_store = types.SimpleNamespace(_test_only=False, claim=mock.Mock())
        with self.assertRaises(ap.VerificationError) as ctx:
            ap.verify_approval(self.approval, self.anchor, revocation_bytes=self.rev_bytes,
                               revocation_state=self.rev_state,
                               expected={'worker_id': 'worker-1', 'run_id': 'run-1',
                                         'attempt_id': teb.ATTEMPT, 'manifest_sha256': self.digest,
                                         'resource_class': 'cpu-diagnostic', 'scope': tac.SCOPE,
                                         'output': tac.OUTPUT},
                               store=fake_store, now=self.now())
        self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_INVALID)
        fake_store.claim.assert_not_called()
        bad_ctx = ap.AttemptContext(store=fake_store, anchor=self.anchor, revocation_state=self.rev_state,
                                    revocations=lambda: self.rev_bytes, worker_id='worker-1',
                                    resource_class='cpu-diagnostic', scope=tac.SCOPE, output=tac.OUTPUT)
        with self.assertRaises(ap.VerificationError):
            ap.consume_for_attempt('run-1', teb.ATTEMPT, self.approval, self.digest, self.now(), ctx=bad_ctx)
        fake_store.claim.assert_not_called()


class RecordChildTests(teb.BoundaryFixture):
    """MEDIUM-1: only this process or its own unreaped child can be recorded."""

    def test_parent_pid_is_refused_writes_nothing_and_recovery_never_signals_it(self):
        token = self.claim_local()
        registry.advance(self.folder, token, 'CLAIMED', 'DISPATCHING')
        db_path = self.folder / 'registry.sqlite3'
        before = db_path.read_bytes()
        victim = os.getppid()
        with self.assertRaises(registry.RegistryError) as ctx:
            registry.record_child(self.folder, token, victim)
        self.assertIn(registry.CHILD_NOT_OWN_CHILD, str(ctx.exception))
        self.assertEqual(before, db_path.read_bytes())
        self.assertEqual(run_recovery.recorded_identities(self.folder), [])
        signaled = []
        with mock.patch.object(os, 'kill', side_effect=lambda pid, sig: signaled.append(pid)), \
                mock.patch.object(os, 'killpg', side_effect=lambda pid, sig: signaled.append(pid)):
            try:
                run_recovery.recover(self.folder, queued=False, grace_s=0.1, kill_wait_s=0.1)
            except Exception:  # recovery may refuse; it must still not signal
                pass
        self.assertNotIn(victim, signaled)

    def test_unrelated_gone_and_invalid_pids_are_refused(self):
        token = self.claim_local()
        registry.advance(self.folder, token, 'CLAIMED', 'DISPATCHING')
        for pid in (1, 2 ** 22 + 12345, 0, -1, True):
            with self.subTest(pid=pid), self.assertRaises(registry.RegistryError):
                registry.record_child(self.folder, token, pid)

    def test_own_child_is_accepted_and_reaped_child_is_refused(self):
        token = self.claim_local()
        registry.advance(self.folder, token, 'CLAIMED', 'DISPATCHING')
        child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(%s)' % 30,
                                  self.marker_name], start_new_session=True)
        self.started_pids.append(child.pid)
        registry.record_child(self.folder, token, child.pid)
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        with self.assertRaises(registry.RegistryError):
            registry.record_child(self.folder, token, child.pid)


class GatedLaunchTests(unittest.TestCase):
    """MEDIUM-2: record before release; a failed record means the job never runs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(os.path.realpath(temp.name))
        self.marker = self.temp / ('gate-marker-%s' % uuid.uuid4().hex)
        self.argv = [sys.executable, '-I', '-c', 'import sys; open(sys.argv[1], "w").close()', str(self.marker)]
        self.children = []
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                child.wait(timeout=10)
            except Exception:
                pass

    def test_identity_is_recorded_before_the_gate_is_released(self):
        events = []

        def recorded(child):
            self.children.append(child)
            events.append(('recorded', child.pid, self.marker.exists()))
            time.sleep(0.5)  # a released job would have created the marker by now
            events.append(('after_wait', self.marker.exists()))
            self.assertEqual(run_recovery.read_identity(child.pid, role='leader')['pid'], child.pid)

        with open(os.devnull, 'wb') as out:
            child = researchctl.start_gated(self.argv, cwd=str(self.temp), stdout=out, stderr=out,
                                            recorded=recorded)
        self.assertEqual(child.wait(timeout=30), 0)
        self.assertEqual(events[0][2], False)
        self.assertEqual(events[1], ('after_wait', False))
        self.assertTrue(self.marker.exists())

    def test_failed_record_never_releases_the_job(self):
        def recorded(child):
            self.children.append(child)
            raise RuntimeError('worker died before recording')

        with open(os.devnull, 'wb') as out, self.assertRaises(RuntimeError):
            researchctl.start_gated(self.argv, cwd=str(self.temp), stdout=out, stderr=out, recorded=recorded)
        self.assertEqual(self.children[0].wait(timeout=30), 125)
        self.assertFalse(self.marker.exists())

    def test_missing_executable_exits_127_after_release(self):
        with open(os.devnull, 'wb') as out:
            child = researchctl.start_gated(['/nonexistent/prog'], cwd=str(self.temp), stdout=out, stderr=out,
                                            recorded=lambda c: self.children.append(c))
        self.assertEqual(child.wait(timeout=30), 127)

    def test_execution_stays_disabled(self):
        self.assertFalse(eb.__dict__.get('PRODUCTION_DISPATCH_ENABLED', False))
        source = (SCRIPTS / 'researchctl.py').read_text()
        self.assertNotIn('PRODUCTION_DISPATCH_ENABLED = True', source)


class ClaimantNamespaceTests(teb.BoundaryFixture):
    """LOW-4: an observer in another pid namespace must not read a live claimant as GONE."""

    def test_claimant_records_namespaces_and_is_live_in_its_own_namespace(self):
        claimant = registry._this_claimant()
        head, pid_ns, mount_ns = claimant.split('|')
        self.assertEqual(pid_ns, os.readlink('/proc/self/ns/pid'))
        self.assertEqual(mount_ns, os.readlink('/proc/self/ns/mnt'))
        self.assertEqual(registry._claimant_liveness(claimant), 'LIVE')

    def test_observer_in_another_namespace_defers_instead_of_gone(self):
        head = registry._this_claimant().split('|')[0]
        foreign = '%s|pid:[4026539999]|mnt:[4026539998]' % head
        self.assertEqual(registry._claimant_liveness(foreign), 'UNKNOWN')
        dead = '%d:%s:%s|pid:[4026539999]|mnt:[4026539998]' % (2 ** 22 + 77, head.split(':')[1], '1')
        self.assertEqual(registry._claimant_liveness(dead), 'UNKNOWN')

    def test_gone_claimant_in_same_namespace_is_gone(self):
        pid_ns = os.readlink('/proc/self/ns/pid')
        mount_ns = os.readlink('/proc/self/ns/mnt')
        boot = registry._this_claimant().split('|')[0].split(':')[1]
        dead = '%d:%s:%s|%s|%s' % (2 ** 22 + 77, boot, '1', pid_ns, mount_ns)
        self.assertEqual(registry._claimant_liveness(dead), 'GONE')


if __name__ == '__main__':
    unittest.main()
