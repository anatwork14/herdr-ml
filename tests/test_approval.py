"""Negative tests for run-specific execution approvals (test-only trust anchors).

Every key here is generated per test inside a temporary directory and labelled
TEST-ONLY. No production trust anchor is read or provisioned. Ownership checks
that need a different uid are exercised with a patched os.lstat, never by reading
system directories.
"""
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import approval as ap  # noqa: E402
import execution_manifest as em  # noqa: E402

NOW = dt.datetime(2026, 11, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
MANIFEST_SHA = 'a' * 64
ANCHOR_ID = 'TEST-ONLY-anchor'
DEFAULT_SIGNERS = {'approver-a': 'test-only-approver-a.key', 'revoker-b': 'test-only-revoker-b.key'}
_DEFAULT = object()


def write_anchor(directory, *, anchor_id=ANCHOR_ID, workers=('worker-1', 'worker-2'), signers=None,
                 revoker='revoker-b', key_bytes=32, anchor_mode=0o600):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    signers = dict(DEFAULT_SIGNERS if signers is None else signers)
    for filename in set(signers.values()):
        key = directory / filename
        key.write_bytes(secrets.token_bytes(key_bytes))
        os.chmod(key, 0o600)
    document = {'schema_version': 1, 'kind': ap.ANCHOR_KIND, 'anchor_id': anchor_id,
                'algorithm': ap.ALGORITHM, 'workers': sorted(workers), 'signers': signers,
                'revoker': revoker}
    path = directory / 'anchor.json'
    path.write_bytes(em.canonical_bytes(document))
    os.chmod(path, anchor_mode)
    return directory


class ApprovalFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp = Path(os.path.realpath(self.temp_dir.name))
        self.run_dir = self.temp / 'run'
        self.run_dir.mkdir()
        self.store_dir = self.temp / 'consumed'
        self.store_dir.mkdir(mode=0o700)
        self.state_dir = self.temp / 'state'
        self.state_dir.mkdir(mode=0o700)
        self.anchor_dir = write_anchor(self.temp / 'TEST-ONLY-trust')
        self.anchor = self.load(self.anchor_dir)
        ap.provision_revocation_state(str(self.state_dir), anchor_id=ANCHOR_ID)
        self.state = self.open_state(self.state_dir)
        self.store = self.open_store(self.store_dir)
        self.expected = {
            'worker_id': 'worker-1', 'run_id': 'run-1', 'attempt_id': 'attempt-1',
            'manifest_sha256': MANIFEST_SHA, 'resource_class': 'cpu-diagnostic',
            'scope': {'project': 'demo', 'operation': 'diagnostic-execution'},
            'output': {'root': 'run-output', 'path': 'result.json'}}
        self.revocations = self.revocation_list(sequence=1)

    def load(self, directory, **kwargs):
        options = {'repo_root': REPO, 'run_dir': self.run_dir, 'test_only': True}
        options.update(kwargs)
        return ap.load_trust_anchor(str(directory), **options)

    def open_state(self, directory, **kwargs):
        options = {'repo_root': REPO, 'run_dir': self.run_dir, 'anchor_id': ANCHOR_ID, 'test_only': True}
        options.update(kwargs)
        return ap.RevocationState(str(directory), **options)

    def open_store(self, directory, **kwargs):
        options = {'repo_root': REPO, 'run_dir': self.run_dir, 'test_only': True}
        options.update(kwargs)
        return ap.ConsumptionStore(str(directory), **options)

    def revocation_list(self, *, sequence, approval_ids=(), manifest_sha256s=(), anchor=None):
        return ap.issue_revocation(anchor or self.anchor, issued_at=NOW - dt.timedelta(hours=2),
                                   sequence=sequence, approval_ids=approval_ids,
                                   manifest_sha256s=manifest_sha256s)

    def issue(self, anchor=None, signer='approver-a', **overrides):
        fields = {'approval_id': 'appr-1', 'worker_id': 'worker-1', 'run_id': 'run-1',
                  'attempt_id': 'attempt-1', 'manifest_sha256': MANIFEST_SHA,
                  'resource_class': 'cpu-diagnostic',
                  'scope': {'project': 'demo', 'operation': 'diagnostic-execution'},
                  'output': {'root': 'run-output', 'path': 'result.json'},
                  'issued_at': NOW - dt.timedelta(minutes=1),
                  'expires_at': NOW + dt.timedelta(hours=1)}
        fields.update(overrides)
        return ap.issue_approval(anchor or self.anchor, signer, **fields)

    def verify(self, approval, *, revocations=_DEFAULT, state=None, store=None, now=None,
               **expected_overrides):
        expected = dict(self.expected, **expected_overrides)
        return ap.verify_approval(approval, self.anchor,
                                  revocation_bytes=self.revocations if revocations is _DEFAULT else revocations,
                                  revocation_state=state or self.state, expected=expected,
                                  store=store or self.store, now=now or NOW)

    def assertCode(self, code, func, *args, **kwargs):
        with self.assertRaises(em.VerificationError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.detail)
        return ctx.exception


class ApprovalBindingTests(ApprovalFixture):
    def test_valid_approval_authorizes_exactly_once(self):
        approval = self.issue()
        record = self.verify(approval)
        self.assertEqual(record['signer_id'], 'approver-a')
        self.assertEqual(record['worker_id'], 'worker-1')
        self.assertCode(ap.APPROVAL_CONSUMED, self.verify, approval)

    def test_attempt_replay_against_second_attempt_is_rejected_and_not_consumed(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_ATTEMPT_MISMATCH, self.verify, approval, attempt_id='attempt-2')
        self.assertEqual(self.verify(approval)['attempt_id'], 'attempt-1')

    def test_approval_issued_for_one_worker_cannot_be_consumed_by_another(self):
        approval = self.issue(worker_id='worker-1')
        # worker-2 is an authorized worker on this anchor, but the approval names worker-1.
        self.assertCode(ap.APPROVAL_WORKER_MISMATCH, self.verify, approval, worker_id='worker-2')
        # The refused attempt did not consume the approval; worker-1 can still use it.
        self.assertEqual(self.verify(approval)['worker_id'], 'worker-1')

    def test_second_approval_for_same_attempt_is_refused(self):
        self.verify(self.issue(approval_id='appr-1'))
        second = self.issue(approval_id='appr-2')
        self.assertCode(ap.APPROVAL_ATTEMPT_ALREADY_CLAIMED, self.verify, second)

    def test_expired_approval_is_rejected(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_EXPIRED, self.verify, approval, now=NOW + dt.timedelta(hours=1))
        self.assertCode(ap.APPROVAL_EXPIRED, self.verify, approval, now=NOW + dt.timedelta(days=3))

    def test_not_yet_valid_approval_is_rejected(self):
        self.assertCode(ap.APPROVAL_NOT_YET_VALID, self.verify, self.issue(), now=NOW - dt.timedelta(hours=1))

    def test_wrong_run_is_rejected(self):
        self.assertCode(ap.APPROVAL_RUN_MISMATCH, self.verify, self.issue(), run_id='run-2')

    def test_wrong_resource_class_is_rejected(self):
        self.assertCode(ap.APPROVAL_RESOURCE_MISMATCH, self.verify, self.issue(), resource_class='gpu-1')

    def test_changed_output_destination_is_rejected(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_DESTINATION_MISMATCH, self.verify, approval,
                        output={'root': 'run-output', 'path': 'other.json'})
        self.assertCode(ap.APPROVAL_DESTINATION_MISMATCH, self.verify, approval,
                        output={'root': 'elsewhere', 'path': 'result.json'})

    def test_wrong_scope_is_rejected(self):
        self.assertCode(ap.APPROVAL_SCOPE_MISMATCH, self.verify, self.issue(),
                        scope={'project': 'other', 'operation': 'diagnostic-execution'})

    def test_approval_for_another_manifest_is_rejected(self):
        self.assertCode(ap.APPROVAL_MANIFEST_MISMATCH, self.verify, self.issue(), manifest_sha256='b' * 64)

    def test_unauthorized_worker_id_is_rejected(self):
        self.assertCode(ap.APPROVAL_WORKER_UNAUTHORIZED, self.verify, self.issue(), worker_id='worker-9')

    def test_signer_not_provisioned_in_anchor_is_rejected(self):
        rogue_dir = write_anchor(self.temp / 'rogue', signers={'rogue': 'test-only-rogue.key'},
                                 revoker='rogue')
        rogue = self.load(rogue_dir)
        self.assertCode(ap.APPROVAL_SIGNER_UNAUTHORIZED, self.verify, self.issue(anchor=rogue, signer='rogue'))

    def test_unknown_anchor_is_rejected(self):
        other = self.load(write_anchor(self.temp / 'other-id', anchor_id='TEST-ONLY-other'))
        self.assertCode(ap.APPROVAL_UNKNOWN_ANCHOR, self.verify, self.issue(anchor=other))

    def test_non_string_signer_and_large_integers_return_reason_codes(self):
        body = json.loads(self.issue())
        body['signature']['signer_id'] = ['approver-a']
        self.assertCode(ap.APPROVAL_MALFORMED, self.verify, em.canonical_bytes(body))
        # Attacker-style bytes: plain json.dumps accepts the big integer; the verifier must refuse it.
        body = json.loads(self.issue())
        body['schema_version'] = 2 ** 60
        self.assertCode(ap.APPROVAL_MALFORMED, self.verify, json.dumps(body).encode('ascii'))
        body = json.loads(self.issue())
        body['signature']['value'] = 2 ** 60
        self.assertCode(ap.APPROVAL_MALFORMED, self.verify, json.dumps(body).encode('ascii'))


class SignatureAndSelfIssueTests(ApprovalFixture):
    def test_tampered_body_fails_signature(self):
        body = json.loads(self.issue())
        body['resource_class'] = 'gpu-1'
        self.assertCode(ap.APPROVAL_SIGNATURE_INVALID, self.verify, em.canonical_bytes(body),
                        resource_class='gpu-1')

    def test_non_ascii_or_malformed_signature_value_fails_with_reason_code(self):
        body = json.loads(self.issue())
        for value in ('é' * 64, 'AB' * 32, 'ab' * 31, 7):
            with self.subTest(value=value):
                body['signature']['value'] = value
                self.assertCode(ap.APPROVAL_SIGNATURE_INVALID, self.verify, em.canonical_bytes(body))

    def test_signature_from_different_key_material_is_rejected(self):
        other = self.load(write_anchor(self.temp / 'TEST-ONLY-other-keys'))
        self.assertCode(ap.APPROVAL_SIGNATURE_INVALID, self.verify, self.issue(anchor=other))

    def test_self_issued_approval_is_rejected_even_with_valid_signature(self):
        worker_key = secrets.token_bytes(32)
        self_anchor = ap.TrustAnchor(ANCHOR_ID, workers=['worker-1'], signers=['worker-1'],
                                     revoker='worker-1',
                                     verifier=ap.HmacSha256Verifier({'worker-1': worker_key}),
                                     test_only=True)
        approval = self.issue(anchor=self_anchor, signer='worker-1')
        self.assertCode(ap.APPROVAL_SELF_ISSUED, self.verify, approval)

    def test_trust_anchor_refuses_a_principal_that_is_both_worker_and_signer(self):
        bad = write_anchor(self.temp / 'TEST-ONLY-bad', workers=('approver-a',))
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, bad)

    def test_revoker_must_be_a_listed_signer(self):
        bad = write_anchor(self.temp / 'TEST-ONLY-norevoker', revoker='nobody')
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, bad)

    def test_truncated_and_noncanonical_approvals_are_rejected(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_MALFORMED, self.verify, approval[:-9])
        pretty = json.dumps(json.loads(approval), indent=2, sort_keys=True).encode('ascii')
        self.assertCode(ap.APPROVAL_NONCANONICAL, self.verify, pretty)

    def test_overlong_validity_window_is_malformed(self):
        self.assertCode(ap.APPROVAL_MALFORMED, self.verify,
                        self.issue(expires_at=NOW + dt.timedelta(days=30)))


class RevocationTests(ApprovalFixture):
    def test_revoked_approval_is_rejected(self):
        revoked = self.revocation_list(sequence=2, approval_ids=['appr-1'])
        self.assertCode(ap.APPROVAL_REVOKED, self.verify, self.issue(), revocations=revoked)

    def test_revoked_manifest_digest_invalidates_its_approvals(self):
        revoked = self.revocation_list(sequence=2, manifest_sha256s=[MANIFEST_SHA])
        self.assertCode(ap.APPROVAL_REVOKED, self.verify, self.issue(), revocations=revoked)

    def test_missing_revocation_list_fails_closed(self):
        self.assertCode(ap.REVOCATION_LIST_MISSING, self.verify, self.issue(), revocations=None)

    def test_forged_revocation_by_non_revoker_is_rejected(self):
        body = json.loads(self.revocation_list(sequence=2, approval_ids=['appr-1']))
        body['signature']['signer_id'] = 'approver-a'
        self.assertCode(ap.REVOCATION_UNAUTHENTICATED, self.verify, self.issue(),
                        revocations=em.canonical_bytes(body))

    def test_tampered_revocation_list_is_rejected(self):
        body = json.loads(self.revocation_list(sequence=2))
        body['revoked_approval_ids'] = ['appr-1']
        self.assertCode(ap.REVOCATION_UNAUTHENTICATED, self.verify, self.issue(),
                        revocations=em.canonical_bytes(body))
        truncated = self.revocation_list(sequence=2)[:-4]
        self.assertCode(ap.REVOCATION_MALFORMED, self.verify, self.issue(), revocations=truncated)

    def test_older_revocation_list_cannot_mask_newer_one(self):
        newer = self.revocation_list(sequence=2, approval_ids=['appr-1'])
        older = self.revocation_list(sequence=1)
        self.assertCode(ap.APPROVAL_REVOKED, self.verify, self.issue(), revocations=newer)
        self.assertCode(ap.REVOCATION_ROLLBACK, self.verify, self.issue(approval_id='appr-2'),
                        revocations=older)

    def test_rollback_check_applies_before_any_other_use_of_the_list(self):
        self.verify(self.issue(approval_id='appr-1'), revocations=self.revocation_list(sequence=5))
        self.assertCode(ap.REVOCATION_ROLLBACK, self.verify, self.issue(approval_id='appr-2'),
                        revocations=self.revocation_list(sequence=4, approval_ids=['appr-2']))

    def test_unauthenticated_list_does_not_advance_recorded_sequence(self):
        forged = json.loads(self.revocation_list(sequence=9))
        forged['signature']['value'] = '0' * 64
        self.assertCode(ap.REVOCATION_UNAUTHENTICATED, self.verify, self.issue(),
                        revocations=em.canonical_bytes(forged))
        self.assertEqual(self.state.last(), 0)

    def test_state_for_a_different_anchor_is_refused_with_its_own_code(self):
        other_state = self.open_state(self.state_dir, anchor_id='TEST-ONLY-other')
        self.assertCode(ap.REVOCATION_STATE_ANCHOR_MISMATCH, self.verify, self.issue(), state=other_state)
        self.assertEqual(self.state.last(), 0)

    def test_missing_revocation_state_fails_closed(self):
        empty = self.temp / 'state-empty'
        empty.mkdir(mode=0o700)
        state = self.open_state(empty)
        self.assertCode(ap.REVOCATION_STATE_MISSING, self.verify, self.issue(), state=state)

    def test_revocation_state_directory_must_not_be_group_writable(self):
        bad = self.temp / 'state-open'
        bad.mkdir(mode=0o700)
        os.chmod(bad, 0o770)
        self.assertCode(ap.REVOCATION_STATE_INVALID, self.open_state, bad)


class ConsumptionTests(ApprovalFixture):
    def test_missing_consumption_directory_is_reported_not_raised(self):
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.open_store, self.temp / 'absent-store')

    def test_consumption_directory_must_not_be_group_writable(self):
        bad = self.temp / 'store-open'
        bad.mkdir(mode=0o700)
        os.chmod(bad, 0o777)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.open_store, bad)

    def test_claims_are_private_and_fsynced_for_file_and_directory(self):
        real_fsync = os.fsync
        with mock.patch.object(ap.os, 'fsync', wraps=real_fsync) as fsync:
            self.verify(self.issue())
        self.assertGreaterEqual(fsync.call_count, 4)
        for name in (ap._claim_name('approval', 'appr-1', 'run-1', 'attempt-1'),
                     ap._claim_name('attempt', 'run-1', 'attempt-1')):
            self.assertEqual(stat.S_IMODE(os.lstat(self.store_dir / name).st_mode), 0o600)

    def test_planted_claim_with_open_mode_is_store_invalid(self):
        planted = self.store_dir / ap._claim_name('approval', 'appr-1', 'run-1', 'attempt-1')
        planted.write_bytes(b'{}')
        os.chmod(planted, 0o666)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.verify, self.issue())

    def test_store_inside_repository_or_run_directory_is_refused(self):
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.open_store, self.run_dir,
                        repo_root=self.temp, run_dir=self.run_dir)
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, self.open_store, self.store_dir,
                        repo_root=self.temp, run_dir=self.run_dir)


class TrustAnchorProvisioningTests(ApprovalFixture):
    def test_unprovisioned_anchor_is_reported_not_assumed(self):
        self.assertCode(ap.TRUST_ANCHOR_UNPROVISIONED, self.load, self.temp / 'absent')

    def test_relative_anchor_path_is_refused(self):
        self.assertCode(ap.TRUST_ANCHOR_INVALID, ap.load_trust_anchor, 'relative/anchor',
                        repo_root=REPO, run_dir=self.run_dir, test_only=True)

    def test_production_mode_refuses_test_only_anchor(self):
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir, test_only=False)

    def test_test_only_mode_requires_label(self):
        unlabelled = write_anchor(self.temp / 'unlabelled', anchor_id='prod-approval-anchor')
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, unlabelled)

    def test_anchor_inside_repository_or_run_directory_is_refused(self):
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir, repo_root=self.temp)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir, run_dir=self.temp)

    def test_group_or_world_readable_key_is_refused(self):
        key = self.anchor_dir / DEFAULT_SIGNERS['approver-a']
        os.chmod(key, 0o644)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir)
        os.chmod(key, 0o600)

    def test_short_key_is_refused(self):
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, write_anchor(self.temp / 'TEST-ONLY-short', key_bytes=16))

    def test_key_symlink_is_refused(self):
        linked = write_anchor(self.temp / 'TEST-ONLY-link', signers={'approver-a': 'real.key',
                                                                    'revoker-b': 'other.key'})
        (linked / 'real.key').rename(self.temp / 'real-moved.key')
        os.symlink(self.temp / 'real-moved.key', linked / 'real.key')
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, linked)

    def test_anchor_json_with_open_mode_is_refused(self):
        open_anchor = write_anchor(self.temp / 'TEST-ONLY-open', anchor_mode=0o666)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, open_anchor)
        self.assertEqual(self.load(self.anchor_dir).anchor_id, ANCHOR_ID)  # control: 0600 is accepted

    def test_symlink_in_parent_directory_of_anchor_is_refused(self):
        real_parent = self.temp / 'real-parent'
        real_parent.mkdir()
        write_anchor(real_parent / 'TEST-ONLY-linked')
        os.symlink(real_parent, self.temp / 'link-parent')
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.temp / 'link-parent' / 'TEST-ONLY-linked')

    def test_asymmetric_placeholder_fails_closed(self):
        placeholder = ap.UnprovisionedAsymmetricVerifier()
        self.assertCode(ap.TRUST_ANCHOR_UNPROVISIONED, placeholder.verify,
                        'approver-a', ap.DOMAIN_APPROVAL, b'{}', 'ab' * 32)


class ProductionOwnershipTests(ApprovalFixture):
    """Ownership checks use explicit uid allowlists; the verifier's own uid is not special."""

    def test_production_mode_requires_explicit_disjoint_uid_allowlists(self):
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir, test_only=False)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir, test_only=False,
                        admin_uids=(0,), worker_uids=None)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.anchor_dir, test_only=False,
                        admin_uids=(0, 1000), worker_uids=(1000,))

    def test_worker_owned_0755_directory_is_refused_when_not_an_admin_owner(self):
        worker_dir = self.temp / 'worker-owned'
        worker_dir.mkdir()
        os.chmod(worker_dir, 0o755)
        uid = os.getuid()
        # Our uid owns the directory. Here it is a worker uid, so it is refused.
        with self.assertRaises(em.VerificationError) as ctx:
            ap._check_directory(str(worker_dir), admin=(0,), workers=(uid,), test_only=False,
                                code=ap.TRUST_ANCHOR_INVALID)
        self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_INVALID)
        self.assertIn('owner', ctx.exception.detail)
        # Even when not listed as a worker, an owner outside the admin allowlist is refused.
        with self.assertRaises(em.VerificationError) as ctx:
            ap._check_ancestors(str(worker_dir), admin=(0,), workers=(), code=ap.TRUST_ANCHOR_INVALID)
        self.assertIn('owner', ctx.exception.detail)

    def test_key_owned_by_worker_uid_is_refused_in_production_mode(self):
        key = self.anchor_dir / DEFAULT_SIGNERS['approver-a']
        self.assertCode(ap.TRUST_ANCHOR_INVALID, ap._load_key, str(key), admin=(0,),
                        workers=(os.getuid(),), test_only=False)

    def _fake_lstat(self, uid, mode=0o040755):
        def fake(path, *args, **kwargs):
            return types.SimpleNamespace(st_mode=mode, st_uid=uid, st_dev=0, st_ino=0,
                                         st_size=0, st_mtime_ns=0, st_ctime_ns=0)
        return fake

    def test_root_owned_directory_passes_when_root_is_admin(self):
        with mock.patch.object(ap.os, 'lstat', self._fake_lstat(uid=0)):
            ap._check_directory('/fixture/root-owned', admin=(0,), workers=(1000,), test_only=False,
                                code=ap.TRUST_ANCHOR_INVALID)
            with self.assertRaises(em.VerificationError) as ctx:
                ap._check_directory('/fixture/root-owned', admin=(1000,), workers=(), test_only=False,
                                    code=ap.TRUST_ANCHOR_INVALID)
            self.assertIn('owner', ctx.exception.detail)

    def test_production_default_path_applies_the_same_ownership_rule(self):
        # Simulated metadata only: every path reports the given owner. The real default
        # location is never opened or listed.
        with mock.patch.object(ap.os, 'lstat', self._fake_lstat(uid=1000)):
            with self.assertRaises(em.VerificationError) as ctx:
                ap.load_trust_anchor(ap.DEFAULT_TRUST_ANCHOR_DIR, repo_root=REPO, run_dir=self.run_dir,
                                     admin_uids=(0,), worker_uids=(1000,))
        self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_INVALID)
        self.assertIn('is not an administrative owner', ctx.exception.detail)
        with mock.patch.object(ap.os, 'lstat', self._fake_lstat(uid=0)):
            with self.assertRaises(em.VerificationError) as ctx:
                ap.load_trust_anchor(ap.DEFAULT_TRUST_ANCHOR_DIR, repo_root=REPO, run_dir=self.run_dir,
                                     admin_uids=(0,), worker_uids=(1000,))
        self.assertEqual(ctx.exception.code, ap.TRUST_ANCHOR_INVALID)
        # Root ownership passed every directory check; the refusal is the anchor.json
        # file-type check, reached because the simulated entry is a directory.
        self.assertEqual(ctx.exception.detail, 'must be a regular file')


if __name__ == '__main__':
    unittest.main()
