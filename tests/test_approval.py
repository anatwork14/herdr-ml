"""Negative tests for run-specific execution approvals (test-only trust anchors).

Every key here is generated per test inside a temporary directory and labelled
TEST-ONLY. No production trust anchor is read or provisioned by these tests.
"""
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import approval as ap  # noqa: E402
import execution_manifest as em  # noqa: E402

NOW = dt.datetime(2026, 11, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
MANIFEST_SHA = 'a' * 64
_DEFAULT = object()
DEFAULT_SIGNERS = {'approver-a': 'test-only-approver-a.key', 'revoker-b': 'test-only-revoker-b.key'}


def write_anchor(directory, *, anchor_id='TEST-ONLY-anchor', workers=('worker-1',),
                 signers=None, revoker='revoker-b', key_bytes=32):
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
    (directory / 'anchor.json').write_bytes(em.canonical_bytes(document))
    return directory


class ApprovalFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp = Path(os.path.realpath(self.temp_dir.name))
        self.run_dir = self.temp / 'run'
        self.run_dir.mkdir()
        self.store_dir = self.temp / 'consumed'
        self.store_dir.mkdir()
        self.anchor_dir = write_anchor(self.temp / 'TEST-ONLY-trust')
        self.anchor = self.load(self.anchor_dir)
        self.store = ap.ConsumptionStore(str(self.store_dir), repo_root=REPO, run_dir=self.run_dir)
        self.expected = {
            'worker_id': 'worker-1', 'run_id': 'run-1', 'attempt_id': 'attempt-1',
            'manifest_sha256': MANIFEST_SHA, 'resource_class': 'cpu-diagnostic',
            'scope': {'project': 'demo', 'operation': 'diagnostic-execution'},
            'output': {'root': 'run-output', 'path': 'result.json'}}
        self.revocations = ap.issue_revocation(self.anchor, issued_at=NOW - dt.timedelta(hours=2),
                                               approval_ids=[], manifest_sha256s=[])

    def load(self, directory, **kwargs):
        options = {'repo_root': REPO, 'run_dir': self.run_dir, 'test_only': True}
        options.update(kwargs)
        return ap.load_trust_anchor(str(directory), **options)

    def issue(self, anchor=None, signer='approver-a', **overrides):
        fields = {'approval_id': 'appr-1', 'run_id': 'run-1', 'attempt_id': 'attempt-1',
                  'manifest_sha256': MANIFEST_SHA, 'resource_class': 'cpu-diagnostic',
                  'scope': {'project': 'demo', 'operation': 'diagnostic-execution'},
                  'output': {'root': 'run-output', 'path': 'result.json'},
                  'issued_at': NOW - dt.timedelta(minutes=1),
                  'expires_at': NOW + dt.timedelta(hours=1)}
        fields.update(overrides)
        return ap.issue_approval(anchor or self.anchor, signer, **fields)

    def verify(self, approval, *, revocations=_DEFAULT, store=None, now=None, **expected_overrides):
        expected = dict(self.expected, **expected_overrides)
        return ap.verify_approval(approval, self.anchor,
                                  revocation_bytes=self.revocations if revocations is _DEFAULT else revocations,
                                  expected=expected, store=store or self.store, now=now or NOW)

    def assertCode(self, code, func, *args, **kwargs):
        with self.assertRaises(em.VerificationError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.detail)


class ApprovalBindingTests(ApprovalFixture):
    def test_valid_approval_authorizes_exactly_once(self):
        approval = self.issue()
        record = self.verify(approval)
        self.assertEqual(record['signer_id'], 'approver-a')
        self.assertEqual(record['worker_id'], 'worker-1')
        self.assertEqual(record['manifest_sha256'], MANIFEST_SHA)
        self.assertCode(ap.APPROVAL_CONSUMED, self.verify, approval)

    def test_attempt_replay_against_second_attempt_is_rejected_and_not_consumed(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_ATTEMPT_MISMATCH, self.verify, approval, attempt_id='attempt-2')
        # A failed check never burns the approval; the intended attempt still succeeds.
        self.assertEqual(self.verify(approval)['attempt_id'], 'attempt-1')

    def test_expired_approval_is_rejected(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_EXPIRED, self.verify, approval, now=NOW + dt.timedelta(hours=1))
        self.assertCode(ap.APPROVAL_EXPIRED, self.verify, approval, now=NOW + dt.timedelta(days=3))

    def test_not_yet_valid_approval_is_rejected(self):
        approval = self.issue()
        self.assertCode(ap.APPROVAL_NOT_YET_VALID, self.verify, approval, now=NOW - dt.timedelta(hours=1))

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
        other_id = write_anchor(self.temp / 'other-id', anchor_id='TEST-ONLY-other')
        other = self.load(other_id)
        self.assertCode(ap.APPROVAL_UNKNOWN_ANCHOR, self.verify, self.issue(anchor=other))


class SignatureAndSelfIssueTests(ApprovalFixture):
    def test_tampered_body_fails_signature(self):
        body = json.loads(self.issue())
        body['resource_class'] = 'gpu-1'
        self.assertCode(ap.APPROVAL_SIGNATURE_INVALID, self.verify, em.canonical_bytes(body),
                        resource_class='gpu-1')

    def test_non_ascii_or_malformed_signature_value_fails_with_reason_code(self):
        body = json.loads(self.issue())
        for value in ('\u00e9' * 64, 'AB' * 32, 'ab' * 31, 7):
            with self.subTest(value=value):
                body['signature']['value'] = value
                self.assertCode(ap.APPROVAL_SIGNATURE_INVALID, self.verify, em.canonical_bytes(body))

    def test_signature_from_different_key_material_is_rejected(self):
        other_dir = write_anchor(self.temp / 'TEST-ONLY-other-keys')
        other = self.load(other_dir)
        self.assertCode(ap.APPROVAL_SIGNATURE_INVALID, self.verify, self.issue(anchor=other))

    def test_self_issued_approval_is_rejected_even_with_valid_signature(self):
        # The signer key is held by the worker here only to show that validity is not enough.
        worker_key = secrets.token_bytes(32)
        self_anchor = ap.TrustAnchor('TEST-ONLY-anchor', workers=['worker-1'], signers=['worker-1'],
                                     revoker='worker-1', verifier=ap.HmacSha256Verifier({'worker-1': worker_key}),
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
        approval = self.issue(expires_at=NOW + dt.timedelta(days=30))
        self.assertCode(ap.APPROVAL_MALFORMED, self.verify, approval)


class RevocationTests(ApprovalFixture):
    def test_revoked_approval_is_rejected(self):
        approval = self.issue()
        revoked = ap.issue_revocation(self.anchor, issued_at=NOW - dt.timedelta(minutes=5),
                                      approval_ids=['appr-1'], manifest_sha256s=[])
        self.assertCode(ap.APPROVAL_REVOKED, self.verify, approval, revocations=revoked)

    def test_revoked_manifest_digest_invalidates_its_approvals(self):
        approval = self.issue()
        revoked = ap.issue_revocation(self.anchor, issued_at=NOW - dt.timedelta(minutes=5),
                                      approval_ids=[], manifest_sha256s=[MANIFEST_SHA])
        self.assertCode(ap.APPROVAL_REVOKED, self.verify, approval, revocations=revoked)

    def test_missing_revocation_list_fails_closed(self):
        self.assertCode(ap.REVOCATION_LIST_MISSING, self.verify, self.issue(), revocations=None)

    def test_forged_revocation_by_non_revoker_is_rejected(self):
        forged = ap.issue_revocation(self.anchor, issued_at=NOW - dt.timedelta(minutes=5),
                                     approval_ids=['appr-1'], manifest_sha256s=[])
        body = json.loads(forged)
        body['signature']['signer_id'] = 'approver-a'
        self.assertCode(ap.REVOCATION_UNAUTHENTICATED, self.verify, self.issue(),
                        revocations=em.canonical_bytes(body))

    def test_tampered_revocation_list_is_rejected(self):
        revoked = ap.issue_revocation(self.anchor, issued_at=NOW - dt.timedelta(minutes=5),
                                      approval_ids=[], manifest_sha256s=[])
        body = json.loads(revoked)
        body['revoked_approval_ids'] = []
        body['revoked_manifest_sha256'] = []
        body['issued_at'] = (NOW - dt.timedelta(hours=9)).strftime(ap.TIME_FORMAT)
        self.assertCode(ap.REVOCATION_UNAUTHENTICATED, self.verify, self.issue(),
                        revocations=em.canonical_bytes(body))
        # Truncated or noncanonical revocation bytes fail closed too.
        self.assertCode(ap.REVOCATION_MALFORMED, self.verify, self.issue(), revocations=revoked[:-4])


class ConsumptionTests(ApprovalFixture):
    def test_consumption_store_refuses_second_claim(self):
        self.store.claim('appr-x', {'n': 1})
        self.assertCode(ap.APPROVAL_CONSUMED, self.store.claim, 'appr-x', {'n': 2})

    def test_store_inside_repository_or_run_directory_is_refused(self):
        self.assertCode(ap.CONSUMPTION_STORE_INVALID, ap.ConsumptionStore,
                        str(self.run_dir), repo_root=self.temp, run_dir=self.run_dir)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, self.store_dir, repo_root=self.temp, run_dir=self.run_dir)


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
        short = write_anchor(self.temp / 'TEST-ONLY-short', key_bytes=16)
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, short)

    def test_key_symlink_is_refused(self):
        linked = write_anchor(self.temp / 'TEST-ONLY-link', signers={'approver-a': 'real.key',
                                                                    'revoker-b': 'other.key'})
        (linked / 'real.key').rename(self.temp / 'real-moved.key')
        os.symlink(self.temp / 'real-moved.key', linked / 'real.key')
        self.assertCode(ap.TRUST_ANCHOR_INVALID, self.load, linked)

    def test_production_mode_refuses_keys_owned_by_verifying_uid(self):
        key = self.anchor_dir / DEFAULT_SIGNERS['approver-a']
        self.assertCode(ap.TRUST_ANCHOR_INVALID, ap._load_key, str(key), os.geteuid(), False)

    def test_production_mode_refuses_writable_or_worker_owned_ancestors(self):
        self.assertCode(ap.TRUST_ANCHOR_INVALID, ap._check_ancestors, str(self.anchor_dir), os.geteuid())

    def test_asymmetric_placeholder_fails_closed(self):
        placeholder = ap.TrustAnchor('TEST-ONLY-asym', workers=['worker-1'], signers=['approver-a'],
                                     revoker='approver-a', verifier=ap.UnprovisionedAsymmetricVerifier(),
                                     test_only=True)
        self.assertCode(ap.TRUST_ANCHOR_UNPROVISIONED, placeholder.verifier.verify,
                        'approver-a', ap.DOMAIN_APPROVAL, b'{}', 'ab' * 32)


if __name__ == '__main__':
    unittest.main()
