"""Crash consistency for approval consumption and the launch handoff contract.

All keys are generated per test inside a temporary directory and labelled TEST-ONLY.
No production trust anchor, verifier or key is read or provisioned. Every test has a
hard timeout (SIGALRM), so a hang fails the test instead of the suite.
"""
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval as ap  # noqa: E402
import execution_manifest as em  # noqa: E402
from support.approval_test_mode import TEST_MODE  # noqa: E402

NOW = dt.datetime(2026, 11, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
MANIFEST_SHA = 'a' * 64
ANCHOR_ID = 'TEST-ONLY-consistency'
SCOPE = {'project': 'demo', 'operation': 'diagnostic-execution'}
OUTPUT = {'root': 'run-output', 'path': 'result.json'}
TIMEOUT_SECONDS = 60
EXIT_CRASH = 77


class Crash(Exception):
    """Raised by a named fault hook to simulate a process dying at that boundary."""


def crash_at(point):
    fired = []

    def hook(name):
        if name == point:
            fired.append(name)
            raise Crash(name)
    hook.fired = fired
    return hook


def snapshot(directory):
    """Every entry in a directory with its bytes and permission bits."""
    result = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        with open(path, 'rb') as handle:
            data = handle.read()
        result[name] = (data, stat.S_IMODE(os.lstat(path).st_mode))
    return result


def write_anchor(directory, *, anchor_id=ANCHOR_ID):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    signers = {'approver-a': 'test-only-approver-a.key', 'revoker-b': 'test-only-revoker-b.key'}
    for filename in set(signers.values()):
        key = directory / filename
        key.write_bytes(secrets.token_bytes(32))
        os.chmod(key, 0o600)
    document = {'schema_version': 1, 'kind': ap.ANCHOR_KIND, 'anchor_id': anchor_id,
                'algorithm': ap.ALGORITHM, 'workers': ['worker-1', 'worker-2'], 'signers': signers,
                'revoker': 'revoker-b'}
    path = directory / 'anchor.json'
    path.write_bytes(em.canonical_bytes(document))
    os.chmod(path, 0o600)
    return directory


class TimedCase(unittest.TestCase):
    def setUp(self):
        self._previous = signal.signal(signal.SIGALRM, self._on_timeout)
        signal.alarm(TIMEOUT_SECONDS)
        self.addCleanup(self._disarm)

    @staticmethod
    def _on_timeout(signum, frame):
        raise TimeoutError('test exceeded %d seconds' % TIMEOUT_SECONDS)

    def _disarm(self):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._previous)


class ConsistencyFixture(TimedCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp = Path(os.path.realpath(self.temp_dir.name))
        self.run_dir = self.temp / 'run'
        self.run_dir.mkdir()
        self.store_dir = self.temp / 'consumed'
        self.store_dir.mkdir(mode=0o700)
        self.state_dir = self.temp / 'state'
        self.state_dir.mkdir(mode=0o700)
        self.anchor = ap.load_trust_anchor(str(write_anchor(self.temp / 'TEST-ONLY-trust')),
                                           **self.opts())
        ap.provision_revocation_state(str(self.state_dir), anchor_id=ANCHOR_ID)
        self.state = ap.RevocationState(str(self.state_dir), anchor_id=ANCHOR_ID, **self.opts())
        self.store = ap.ConsumptionStore(str(self.store_dir), **self.opts())
        self.rev_bytes = self.revocation(sequence=1)

    def opts(self):
        return {'repo_root': REPO, 'run_dir': self.run_dir, 'test_only': True,
                'test_root': str(self.temp)}

    def revocation(self, *, sequence, approval_ids=(), manifest_sha256s=()):
        return ap.issue_revocation(self.anchor, issued_at=NOW - dt.timedelta(hours=2),
                                   sequence=sequence, approval_ids=approval_ids,
                                   manifest_sha256s=manifest_sha256s)

    def issue(self, approval_id='appr-1', attempt_id='attempt-1', **overrides):
        fields = {'approval_id': approval_id, 'worker_id': 'worker-1', 'run_id': 'run-1',
                  'attempt_id': attempt_id, 'manifest_sha256': MANIFEST_SHA,
                  'resource_class': 'cpu-diagnostic', 'scope': SCOPE, 'output': OUTPUT,
                  'issued_at': NOW - dt.timedelta(minutes=1),
                  'expires_at': NOW + dt.timedelta(hours=1)}
        fields.update(overrides)
        return ap.issue_approval(self.anchor, 'approver-a', **fields)

    def ctx(self, store=None):
        return ap.AttemptContext(store=store or self.store, anchor=self.anchor,
                                 revocation_state=self.state, revocations=lambda: self.rev_bytes,
                                 worker_id='worker-1', resource_class='cpu-diagnostic',
                                 scope=SCOPE, output=OUTPUT, test_capability=TEST_MODE)

    def consume(self, approval, *, attempt_id='attempt-1', now=NOW, fault=None, store=None,
                manifest=MANIFEST_SHA):
        return ap.consume_for_attempt('run-1', attempt_id, approval, manifest, now,
                                      ctx=self.ctx(store), _fault=fault)

    def assertCode(self, code, func, *args, **kwargs):
        with self.assertRaises(em.VerificationError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.detail)
        return ctx.exception

    def reopen_store(self):
        return ap.ConsumptionStore(str(self.store_dir), **self.opts())


class CrashBeforeClaimTests(ConsistencyFixture):
    def test_crash_before_claim_consumes_nothing(self):
        approval = self.issue()
        hook = crash_at('claim:before_attempt_claim')
        with self.assertRaises(Crash):
            self.consume(approval, fault=hook)
        self.assertEqual(hook.fired, ['claim:before_attempt_claim'])
        self.assertEqual(snapshot(self.store_dir), {})
        self.assertIsNone(self.reopen_store().read_attempt('run-1', 'attempt-1'))
        self.assertEqual(self.consume(approval)['approval_id'], 'appr-1')


class CrashAfterClaimWriteTests(ConsistencyFixture):
    def test_crash_after_attempt_claim_fences_and_never_reuses(self):
        approval = self.issue(approval_id='appr-1')
        other = self.issue(approval_id='appr-2')
        with self.assertRaises(Crash):
            self.consume(approval, fault=crash_at('claim:after_attempt_claim'))
        store = self.reopen_store()
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_CONSUMED_NOT_LAUNCHED)
        # Neither the same approval nor a replacement may be used for the attempt.
        self.assertCode(ap.ATTEMPT_UNLAUNCHED, self.consume, approval, store=store)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.consume, other, store=store)
        # Recovery fences the unlaunched consumption; the fence is terminal.
        fenced = store.fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched')
        self.assertEqual(fenced['state'], ap.STATE_FENCED)
        self.assertCode(ap.ATTEMPT_FENCED, self.consume, approval, store=store)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.consume, other, store=store)
        self.assertCode(ap.ATTEMPT_FENCED, store.mark_launched, 'run-1', 'attempt-1',
                        approval_sha256=ap.sha256_hex(bytes(approval)))

    def test_crash_after_approval_claim_leaves_attempt_unlaunched_and_refuses_replay(self):
        approval = self.issue(approval_id='appr-1')
        other = self.issue(approval_id='appr-2')
        with self.assertRaises(Crash):
            self.consume(approval, fault=crash_at('claim:after_approval_claim'))
        store = self.reopen_store()
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval, store=store)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.consume, other, store=store)
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_CONSUMED_NOT_LAUNCHED)

    def test_crash_while_opening_attempt_claim_is_refused_without_reuse(self):
        approval = self.issue(approval_id='appr-1')
        other = self.issue(approval_id='appr-2')
        with self.assertRaises(Crash):
            self.consume(approval, fault=crash_at('claim:attempt_opened'))
        before = snapshot(self.store_dir)
        self.assertEqual(len(before), 1)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, approval)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, other)
        self.assertEqual(snapshot(self.store_dir), before)

    def test_crash_while_opening_approval_claim_refuses_replay(self):
        approval = self.issue(approval_id='appr-1')
        other = self.issue(approval_id='appr-2')
        with self.assertRaises(Crash):
            self.consume(approval, fault=crash_at('claim:approval_opened'))
        before = snapshot(self.store_dir)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, approval)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.consume, other)
        self.assertEqual(snapshot(self.store_dir), before)

    def test_process_death_after_attempt_claim_is_durable_across_processes(self):
        approval = self.issue(approval_id='appr-1')
        pid = os.fork()
        if pid == 0:
            code = 3
            try:
                self.consume(approval, fault=lambda point: os._exit(EXIT_CRASH)
                             if point == 'claim:after_attempt_claim' else None)
            except BaseException:
                code = 4
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), EXIT_CRASH)
        store = self.reopen_store()
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_CONSUMED_NOT_LAUNCHED)
        self.assertCode(ap.ATTEMPT_UNLAUNCHED, self.consume, approval, store=store)


class LaunchAndFenceCrashTests(ConsistencyFixture):
    def consumed(self, approval_id='appr-1'):
        approval = self.issue(approval_id=approval_id)
        record = self.consume(approval)
        return approval, record

    def test_crash_before_launch_record_stays_consumed_not_launched(self):
        approval, record = self.consumed()
        hook = crash_at('launch:before_replace')
        with self.assertRaises(Crash):
            self.reopen_store().mark_launched('run-1', 'attempt-1', approval_sha256=record['approval_sha256'],
                                              _fault=hook)
        store = self.reopen_store()
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_CONSUMED_NOT_LAUNCHED)
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval, store=store)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.consume, self.issue('appr-2'), store=store)
        store.fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched')
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval, store=store)

    def test_crash_after_launch_record_is_launched_and_not_relaunchable(self):
        approval, record = self.consumed()
        with self.assertRaises(Crash):
            self.reopen_store().mark_launched('run-1', 'attempt-1', approval_sha256=record['approval_sha256'],
                                              _fault=crash_at('launch:after_replace'))
        store = self.reopen_store()
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_LAUNCHED)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, store.mark_launched, 'run-1', 'attempt-1',
                        approval_sha256=record['approval_sha256'])
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, store.fence_unlaunched, 'run-1', 'attempt-1',
                        reason='recovery-unlaunched')

    def test_crash_before_fence_record_recovery_can_run_again(self):
        approval, _ = self.consumed()
        with self.assertRaises(Crash):
            self.reopen_store().fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched',
                                                 _fault=crash_at('fence:before_replace'))
        store = self.reopen_store()
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_CONSUMED_NOT_LAUNCHED)
        self.assertEqual(store.fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched')['state'],
                         ap.STATE_FENCED)
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval, store=store)

    def test_crash_after_fence_record_is_terminal_and_idempotent(self):
        approval, _ = self.consumed()
        with self.assertRaises(Crash):
            self.reopen_store().fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched',
                                                 _fault=crash_at('fence:after_replace'))
        store = self.reopen_store()
        self.assertEqual(store.read_attempt('run-1', 'attempt-1')['state'], ap.STATE_FENCED)
        self.assertEqual(store.fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched')['state'],
                         ap.STATE_FENCED)
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval, store=store)


class AuthorizationBoundaryTests(ConsistencyFixture):
    def test_two_different_approvals_for_one_attempt_second_is_refused(self):
        first = self.issue(approval_id='appr-1')
        second = self.issue(approval_id='appr-2')
        self.consume(first)
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.consume, second)
        self.assertFalse(os.path.exists(os.path.join(
            self.store_dir, ap._claim_name('approval', 'appr-2', 'run-1', 'attempt-1'))))

    def test_replay_of_consumed_approval_is_refused(self):
        approval = self.issue()
        self.consume(approval)
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval)
        self.assertCode(ap.APPROVAL_ATTEMPT_MISMATCH, self.consume, approval, attempt_id='attempt-2')

    def test_revocation_during_authorization_is_refused_and_writes_no_claim(self):
        approval = self.issue()
        # The revocation arrives after the launcher started authorizing, before the boundary.
        self.rev_bytes = self.revocation(sequence=2, approval_ids=['appr-1'])
        self.assertCode(ap.APPROVAL_REVOKED, self.consume, approval)
        self.assertEqual(snapshot(self.store_dir), {})
        self.assertCode(ap.APPROVAL_REVOKED, self.consume, approval)

    def test_revoked_manifest_during_authorization_is_refused(self):
        approval = self.issue()
        self.rev_bytes = self.revocation(sequence=2, manifest_sha256s=[MANIFEST_SHA])
        self.assertCode(ap.APPROVAL_REVOKED, self.consume, approval)
        self.assertEqual(snapshot(self.store_dir), {})

    def test_expiration_at_execution_boundary_is_refused_and_writes_nothing(self):
        approval = self.issue()
        expired_at = NOW + dt.timedelta(hours=1)
        self.assertCode(ap.APPROVAL_EXPIRED, self.consume, approval, now=expired_at)
        self.assertCode(ap.APPROVAL_EXPIRED, self.consume, approval, now=expired_at + dt.timedelta(minutes=5))
        self.assertEqual(snapshot(self.store_dir), {})
        # One second before expiry the same approval is still usable.
        self.assertEqual(self.consume(approval, now=expired_at - dt.timedelta(seconds=1))['attempt_id'],
                         'attempt-1')

    def test_not_yet_valid_at_boundary_is_refused(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_NOT_YET_VALID, self.consume, approval, now=NOW - dt.timedelta(hours=1))
        self.assertEqual(snapshot(self.store_dir), {})

    def test_wrong_manifest_digest_at_boundary_is_refused_and_writes_nothing(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_MANIFEST_MISMATCH, self.consume, approval, manifest='b' * 64)
        self.assertEqual(snapshot(self.store_dir), {})

    def test_naive_clock_or_bad_identifiers_are_malformed(self):
        approval = self.issue()
        with self.assertRaises(em.VerificationError) as ctx:
            ap.consume_for_attempt('run-1', 'attempt-1', approval, MANIFEST_SHA,
                                   NOW.replace(tzinfo=None), ctx=self.ctx())
        self.assertEqual(ctx.exception.code, ap.APPROVAL_MALFORMED)
        self.assertCode(ap.APPROVAL_MALFORMED, self.consume, approval, attempt_id='bad id!')
        self.assertEqual(snapshot(self.store_dir), {})


class ParallelClaimTests(ConsistencyFixture):
    def _race(self, approvals):
        """Fork one child per approval; each consumes for the same attempt at once."""
        self.state.advance(1)  # revocation sequence already recorded, so children only claim
        pids = []
        for approval in approvals:
            pid = os.fork()
            if pid == 0:
                code = 4
                try:
                    self.consume(approval)
                    code = 0
                except em.VerificationError:
                    code = 3
                except BaseException:
                    code = 5
                os._exit(code)
            pids.append(pid)
        codes = []
        for pid in pids:
            _, status = os.waitpid(pid, 0)
            codes.append(os.WEXITSTATUS(status))
        return codes

    def test_parallel_distinct_approvals_for_one_attempt_exactly_one_wins(self):
        approvals = [self.issue(approval_id='appr-%d' % i) for i in range(6)]
        codes = self._race(approvals)
        self.assertEqual(codes.count(0), 1, codes)
        self.assertNotIn(5, codes)
        self.assertEqual(len(snapshot(self.store_dir)), 2)  # one attempt claim, one approval claim

    def test_parallel_same_approval_exactly_one_wins(self):
        approval = self.issue(approval_id='appr-1')
        codes = self._race([approval] * 6)
        self.assertEqual(codes.count(0), 1, codes)
        self.assertNotIn(5, codes)
        self.assertEqual(len(snapshot(self.store_dir)), 2)


class CorruptionAndPermissionTests(ConsistencyFixture):
    def consumed_store(self):
        approval = self.issue(approval_id='appr-1')
        self.consume(approval)
        return approval

    def test_corrupted_attempt_claim_is_refused_and_bytes_unchanged(self):
        approval = self.consumed_store()
        attempt_path = os.path.join(self.store_dir, ap._claim_name('attempt', 'run-1', 'attempt-1'))
        os.chmod(attempt_path, 0o600)
        with open(attempt_path, 'wb') as handle:
            handle.write(b'{"state":"launched"')
        before = snapshot(self.store_dir)
        self.assertCode(ap.APPROVAL_CONSUMED, self.consume, approval)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, self.issue('appr-2'))
        self.assertEqual(snapshot(self.store_dir), before)

    def test_claim_with_forged_state_or_wrong_kind_is_refused_and_bytes_unchanged(self):
        approval = self.consumed_store()
        attempt_path = os.path.join(self.store_dir, ap._claim_name('attempt', 'run-1', 'attempt-1'))
        with open(attempt_path, 'rb') as handle:
            record = json.loads(handle.read())
        record['state'] = 'unknown-state'
        os.chmod(attempt_path, 0o600)
        with open(attempt_path, 'wb') as handle:
            handle.write(em.canonical_bytes(record))
        before = snapshot(self.store_dir)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, self.issue('appr-2'))
        self.assertEqual(snapshot(self.store_dir), before)

    def test_claim_renamed_to_another_key_is_refused(self):
        approval = self.consumed_store()
        approval_path = os.path.join(self.store_dir, ap._claim_name('approval', 'appr-1', 'run-1', 'attempt-1'))
        other_path = os.path.join(self.store_dir, ap._claim_name('approval', 'appr-9', 'run-1', 'attempt-1'))
        os.rename(approval_path, other_path)
        before = snapshot(self.store_dir)
        # The attempt is still consumed, so the original approval is refused as not reusable.
        self.assertCode(ap.ATTEMPT_UNLAUNCHED, self.consume, approval)
        # The mis-keyed claim is refused when the approval it names is presented.
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, self.issue('appr-9'))
        self.assertEqual(snapshot(self.store_dir), before)

    def test_group_readable_claim_is_refused_and_bytes_unchanged(self):
        self.consumed_store()
        attempt_path = os.path.join(self.store_dir, ap._claim_name('attempt', 'run-1', 'attempt-1'))
        os.chmod(attempt_path, 0o640)
        before = snapshot(self.store_dir)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.consume, self.issue('appr-2'))
        self.assertEqual(snapshot(self.store_dir), before)

    def test_wrong_directory_permissions_refuse_the_store(self):
        self.consumed_store()
        before = snapshot(self.store_dir)
        for mode in (0o770, 0o777):
            os.chmod(self.store_dir, mode)
            self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.reopen_store)
            self.assertEqual(snapshot(self.store_dir), before)
        os.chmod(self.store_dir, 0o700)
        self.assertEqual(snapshot(self.store_dir), before)


class HandoffContractTests(ConsistencyFixture):
    """The handoff binds the manifest, run, attempt, worker and the consumption record."""

    def consumed_record(self):
        return self.consume(self.issue(approval_id='appr-1'))

    def handoff(self, record, *, key_id='launcher-key', value='test-only-accept', **overrides):
        fields = {'anchor_id': ANCHOR_ID, 'run_id': 'run-1', 'attempt_id': 'attempt-1',
                  'worker_id': 'worker-1', 'manifest_sha256': MANIFEST_SHA,
                  'approval_sha256': record['approval_sha256'],
                  'grant_sha256': ap.grant_digest(record),
                  'issued_at': NOW - dt.timedelta(minutes=1), 'expires_at': NOW + dt.timedelta(minutes=10),
                  'key_id': key_id}
        fields.update(overrides)
        body = ap.handoff_body(**fields)
        body['signature']['value'] = value
        return em.canonical_bytes(body)

    def expected(self, **overrides):
        values = {'anchor_id': ANCHOR_ID, 'worker_id': 'worker-1', 'run_id': 'run-1',
                  'attempt_id': 'attempt-1', 'manifest_sha256': MANIFEST_SHA}
        values.update(overrides)
        return values

    def verifier(self):
        return ap.TestOnlyHandoffVerifier('TEST-ONLY-handoff', ['launcher-key'])

    def test_production_has_no_handoff_verifier_and_refuses_everything(self):
        record = self.consumed_record()
        default = ap.select_handoff_verifier(test_only=False)
        self.assertIsInstance(default, ap.UnprovisionedHandoffVerifier)
        self.assertCode(ap.HANDOFF_VERIFIER_UNPROVISIONED, ap.verify_handoff, self.handoff(record), default,
                        store=self.store, now=NOW, expected=self.expected())

    def test_handoff_authorized_by_hmac_material_or_json_alone_is_refused(self):
        record = self.consumed_record()
        hmac_verifier = ap.HmacSha256Verifier({'launcher-key': secrets.token_bytes(32)})
        self.assertCode(ap.HANDOFF_VERIFIER_UNPROVISIONED, ap.verify_handoff, self.handoff(record),
                        hmac_verifier, store=self.store, now=NOW, expected=self.expected())
        self.assertCode(ap.HANDOFF_VERIFIER_UNPROVISIONED, ap.verify_handoff, self.handoff(record),
                        ap.select_handoff_verifier(test_only=True), store=self.store, now=NOW,
                        expected=self.expected())
        # Caller-supplied JSON with a plausible signature value is not enough for the real refusal path
        # either: the test-only verifier accepts only its own fixed value.
        self.assertCode(ap.HANDOFF_SIGNATURE_INVALID, ap.verify_handoff, self.handoff(record, value='ab' * 32),
                        self.verifier(), mode=TEST_MODE, store=self.store, now=NOW, expected=self.expected())

    def test_handoff_with_symmetric_algorithm_label_is_refused(self):
        record = self.consumed_record()
        body = json.loads(self.handoff(record))
        body['signature']['algorithm'] = 'HMAC-SHA256'
        self.assertCode(ap.HANDOFF_SIGNATURE_INVALID, ap.verify_handoff, em.canonical_bytes(body),
                        self.verifier(), mode=TEST_MODE, store=self.store, now=NOW, expected=self.expected())

    def test_handoff_binds_manifest_run_attempt_worker_and_anchor(self):
        record = self.consumed_record()
        for overrides in ({'run_id': 'run-2'}, {'attempt_id': 'attempt-2'}, {'manifest_sha256': 'b' * 64},
                          {'worker_id': 'worker-2'}, {'anchor_id': 'TEST-ONLY-other'}):
            with self.subTest(overrides=overrides):
                data = self.handoff(record, **overrides)
                self.assertCode(ap.HANDOFF_BINDING_MISMATCH, ap.verify_handoff, data, self.verifier(), mode=TEST_MODE,
                                store=self.store, now=NOW, expected=self.expected())
        self.assertEqual(ap.verify_handoff(self.handoff(record), self.verifier(), mode=TEST_MODE, store=self.store,
                                           now=NOW, expected=self.expected())['attempt_id'], 'attempt-1')

    def test_handoff_for_wrong_consumption_record_is_refused(self):
        record = self.consumed_record()
        data = self.handoff(record, grant_sha256='c' * 64)
        self.assertCode(ap.HANDOFF_CONSUMPTION_MISMATCH, ap.verify_handoff, data, self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW, expected=self.expected())

    def test_handoff_without_consumption_is_refused(self):
        record = self.consumed_record()
        data = self.handoff(record, attempt_id='attempt-2')
        self.assertCode(ap.HANDOFF_NOT_CONSUMED, ap.verify_handoff, data, self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW, expected=self.expected(attempt_id='attempt-2'))

    def test_handoff_after_fence_or_launch_is_refused(self):
        record = self.consumed_record()
        data = self.handoff(record)
        self.store.fence_unlaunched('run-1', 'attempt-1', reason='recovery-unlaunched')
        self.assertCode(ap.HANDOFF_FENCED, ap.verify_handoff, data, self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW, expected=self.expected())
        other = self.consume(self.issue(approval_id='appr-2', attempt_id='attempt-2'), attempt_id='attempt-2')
        launched = self.handoff(other, attempt_id='attempt-2')
        self.store.mark_launched('run-1', 'attempt-2', approval_sha256=other['approval_sha256'])
        self.assertCode(ap.HANDOFF_ALREADY_LAUNCHED, ap.verify_handoff, launched, self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW, expected=self.expected(attempt_id='attempt-2'))

    def test_handoff_expiry_and_clock_bounds(self):
        record = self.consumed_record()
        expired = self.handoff(record, expires_at=NOW + dt.timedelta(seconds=1))
        self.assertCode(ap.HANDOFF_EXPIRED, ap.verify_handoff, expired, self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW + dt.timedelta(seconds=1), expected=self.expected())
        self.assertCode(ap.HANDOFF_NOT_YET_VALID, ap.verify_handoff, self.handoff(record), self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW - dt.timedelta(hours=1), expected=self.expected())

    def test_noncanonical_handoff_is_refused(self):
        record = self.consumed_record()
        pretty = json.dumps(json.loads(self.handoff(record)), indent=2, sort_keys=True).encode('ascii')
        self.assertCode(ap.HANDOFF_NONCANONICAL, ap.verify_handoff, pretty, self.verifier(), mode=TEST_MODE,
                        store=self.store, now=NOW, expected=self.expected())

    def test_test_only_verifier_is_not_selectable_in_production(self):
        self.assertCode(ap.HANDOFF_VERIFIER_NOT_SELECTABLE, ap.select_handoff_verifier,
                        test_only=False, injected=self.verifier())
        self.assertIs(ap.select_handoff_verifier(test_only=True, injected=self.verifier()).__class__,
                      ap.TestOnlyHandoffVerifier)

    def test_test_only_verifier_requires_test_only_label(self):
        self.assertCode(ap.HANDOFF_VERIFIER_NOT_SELECTABLE, ap.TestOnlyHandoffVerifier, 'prod-handoff', ['k'])


if __name__ == '__main__':
    unittest.main()
