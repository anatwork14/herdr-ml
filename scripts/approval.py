#!/usr/bin/env python3
"""Run-specific execution approval verification. OFFLINE_TESTED only.

An approval authorizes exactly one manifest digest for one run, one attempt,
one worker, one resource class, one scope and one output destination, within a
validity window, unless revoked, and only once (consumption is claimed by the
execution boundary). Verification must run in the trusted launcher, which holds
the verification material. The worker must never load signing or verification
keys and must never be able to write the consumption or revocation state.

The stdlib provides HMAC-SHA256 only. A symmetric MAC proves that the holder of
the signer's key produced the approval, so the verifier is as trusted as the
keys it holds. An asymmetric verifier can be plugged in through
SignatureVerifier once an external trust anchor is provisioned. Until then,
APPROVAL_READY stays BLOCKED.
"""
import abc
import datetime as dt
import hashlib
import hmac
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import execution_manifest as em  # noqa: E402
from execution_manifest import (VerificationError, canonical_bytes, digest_value,  # noqa: E402
                                parse_canonical, read_bounded, sha256_hex)

APPROVAL_KIND = 'herdr-ml-execution-approval'
REVOCATION_KIND = 'herdr-ml-revocation-list'
ANCHOR_KIND = 'herdr-ml-trust-anchor'
SCHEMA_VERSION = 1
ALGORITHM = 'HMAC-SHA256'
DOMAIN_APPROVAL = b'herdr-ml-approval-v1\n'
DOMAIN_REVOCATION = b'herdr-ml-revocation-list-v1\n'
# Operator-provisioned location outside the repository, run directories and the
# worker's writable scope. Production callers must pass it explicitly; nothing
# reads an environment override, so a worker cannot redirect verification.
DEFAULT_TRUST_ANCHOR_DIR = '/etc/herdr-ml/trust/execution-approval'
TEST_ONLY_PREFIX = 'TEST-ONLY-'
MIN_KEY_BYTES = 32
MAX_KEY_BYTES = 4096
MAX_ANCHOR_BYTES = 64 * 1024
MAX_APPROVAL_BYTES = 64 * 1024
MAX_REVOCATION_BYTES = 1024 * 1024
MAX_REVOKED = 4096
MAX_LIFETIME_SECONDS = 24 * 3600
TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

# Reason codes (approval and trust material). Manifest codes live in execution_manifest.
APPROVAL_MALFORMED = 'APPROVAL_MALFORMED'
APPROVAL_NONCANONICAL = 'APPROVAL_NONCANONICAL'
APPROVAL_UNKNOWN_ANCHOR = 'APPROVAL_UNKNOWN_ANCHOR'
APPROVAL_SELF_ISSUED = 'APPROVAL_SELF_ISSUED'
APPROVAL_SIGNER_UNAUTHORIZED = 'APPROVAL_SIGNER_UNAUTHORIZED'
APPROVAL_WORKER_UNAUTHORIZED = 'APPROVAL_WORKER_UNAUTHORIZED'
APPROVAL_SIGNATURE_INVALID = 'APPROVAL_SIGNATURE_INVALID'
APPROVAL_NOT_YET_VALID = 'APPROVAL_NOT_YET_VALID'
APPROVAL_EXPIRED = 'APPROVAL_EXPIRED'
APPROVAL_REVOKED = 'APPROVAL_REVOKED'
APPROVAL_RUN_MISMATCH = 'APPROVAL_RUN_MISMATCH'
APPROVAL_ATTEMPT_MISMATCH = 'APPROVAL_ATTEMPT_MISMATCH'
APPROVAL_MANIFEST_MISMATCH = 'APPROVAL_MANIFEST_MISMATCH'
APPROVAL_RESOURCE_MISMATCH = 'APPROVAL_RESOURCE_MISMATCH'
APPROVAL_SCOPE_MISMATCH = 'APPROVAL_SCOPE_MISMATCH'
APPROVAL_DESTINATION_MISMATCH = 'APPROVAL_DESTINATION_MISMATCH'
APPROVAL_CONSUMED = 'APPROVAL_CONSUMED'
REVOCATION_LIST_MISSING = 'REVOCATION_LIST_MISSING'
REVOCATION_MALFORMED = 'REVOCATION_MALFORMED'
REVOCATION_UNAUTHENTICATED = 'REVOCATION_UNAUTHENTICATED'
TRUST_ANCHOR_UNPROVISIONED = 'TRUST_ANCHOR_UNPROVISIONED'
TRUST_ANCHOR_INVALID = 'TRUST_ANCHOR_INVALID'
CONSUMPTION_STORE_INVALID = 'CONSUMPTION_STORE_INVALID'


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value):
    if not isinstance(value, str):
        raise VerificationError(APPROVAL_MALFORMED, 'timestamp')
    try:
        parsed = dt.datetime.strptime(value, TIME_FORMAT)
    except ValueError as exc:
        raise VerificationError(APPROVAL_MALFORMED, 'timestamp format') from exc
    return parsed.replace(tzinfo=dt.timezone.utc)


def _format_time(value):
    return value.astimezone(dt.timezone.utc).strftime(TIME_FORMAT)


def _keys(obj, keys, code):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise VerificationError(code, 'unexpected fields')


def _is_hex(value):
    return em._is_sha(value)


# ---------------------------------------------------------------- signature verifiers

class SignatureVerifier(abc.ABC):
    """Interface for approval signature verification.

    An asymmetric implementation (for example Ed25519 from an external,
    audited library) implements verify() using only public material. The
    stdlib implementation below is HMAC-SHA256 and holds signer keys.
    """
    algorithm = None

    @abc.abstractmethod
    def has_signer(self, signer_id):
        """True if this verifier holds material for signer_id."""

    @abc.abstractmethod
    def verify(self, signer_id, domain, message, value_hex):
        """Return True only for a valid signature over domain + message."""


class HmacSha256Verifier(SignatureVerifier):
    algorithm = ALGORITHM

    def __init__(self, signer_keys):
        self._keys = dict(signer_keys)

    def has_signer(self, signer_id):
        return signer_id in self._keys

    def sign(self, signer_id, domain, message):
        key = self._keys.get(signer_id)
        if key is None:
            raise VerificationError(APPROVAL_SIGNER_UNAUTHORIZED, 'no key for signer')
        return hmac.new(key, domain + message, hashlib.sha256).hexdigest()

    def verify(self, signer_id, domain, message, value_hex):
        if not self.has_signer(signer_id) or not isinstance(value_hex, str):
            return False
        if em.HEX64_RE.match(value_hex) is None:
            return False
        return hmac.compare_digest(self.sign(signer_id, domain, message), value_hex)


class UnprovisionedAsymmetricVerifier(SignatureVerifier):
    """Placeholder marking where an external asymmetric verifier is required."""
    algorithm = 'asymmetric-unprovisioned'

    def has_signer(self, signer_id):
        return False

    def verify(self, signer_id, domain, message, value_hex):
        raise VerificationError(TRUST_ANCHOR_UNPROVISIONED,
                                'stdlib has no Ed25519; provision an external verifier')


class TrustAnchor(object):
    def __init__(self, anchor_id, workers, signers, revoker, verifier, test_only=False):
        self.anchor_id = anchor_id
        self.workers = frozenset(workers)
        self.signers = frozenset(signers)
        self.revoker = revoker
        self.verifier = verifier
        self.test_only = test_only


# ---------------------------------------------------------------- trust anchor loading

def _within(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _check_location(real, repo_root, run_dir, code=TRUST_ANCHOR_INVALID):
    for forbidden in (repo_root, run_dir):
        if forbidden is None:
            raise VerificationError(code, 'repository and run locations are required')
        if _within(real, os.path.realpath(forbidden)):
            raise VerificationError(code, 'inside repository or run directory')


def _check_ancestors(real, uid):
    current = real
    while True:
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise VerificationError(TRUST_ANCHOR_INVALID, 'ancestor unreadable') from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise VerificationError(TRUST_ANCHOR_INVALID, 'ancestor is not a plain directory')
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or info.st_uid == uid:
            raise VerificationError(TRUST_ANCHOR_INVALID, 'ancestor writable or owned by worker')
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def load_trust_anchor(anchor_dir, *, repo_root, run_dir, test_only=False):
    """Load an operator-provisioned anchor. Production mode (test_only=False) requires
    root-or-admin ownership, no group/other write on ancestors and no worker ownership.

    test_only=True is for the test suite only: it requires the TEST-ONLY- label and a
    directory under the system temp directory, and it skips ownership checks.
    """
    if not _is_abs(anchor_dir):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'anchor directory must be absolute')
    try:
        info = os.lstat(anchor_dir)
    except FileNotFoundError as exc:
        raise VerificationError(TRUST_ANCHOR_UNPROVISIONED, 'no trust anchor provisioned') from exc
    except OSError as exc:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'anchor unreadable') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'anchor must be a plain directory')
    real = os.path.realpath(anchor_dir)
    _check_location(real, repo_root, run_dir)
    uid = os.geteuid()
    if test_only:
        if not _within(real, os.path.realpath(_tempdir())):
            raise VerificationError(TRUST_ANCHOR_INVALID, 'test-only anchors must live in the temp directory')
    else:
        _check_ancestors(real, uid)
    document = parse_canonical(
        read_bounded(os.path.join(real, 'anchor.json'), MAX_ANCHOR_BYTES,
                     missing=TRUST_ANCHOR_INVALID, unsafe=TRUST_ANCHOR_INVALID),
        MAX_ANCHOR_BYTES, TRUST_ANCHOR_INVALID, TRUST_ANCHOR_INVALID, TRUST_ANCHOR_INVALID)
    _keys(document, ('schema_version', 'kind', 'anchor_id', 'algorithm', 'workers', 'signers', 'revoker'),
          TRUST_ANCHOR_INVALID)
    if (type(document['schema_version']) is not int or document['schema_version'] != SCHEMA_VERSION
            or document['kind'] != ANCHOR_KIND or document['algorithm'] != ALGORITHM):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'anchor schema')
    anchor_id = document['anchor_id']
    if not em._is_id(anchor_id):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'anchor id')
    if test_only != anchor_id.startswith(TEST_ONLY_PREFIX):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'test-only label must match test_only mode')
    workers = document['workers']
    signers = document['signers']
    if (not isinstance(workers, list) or not workers or not all(em._is_id(w) for w in workers)
            or workers != sorted(set(workers))):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'workers')
    if not isinstance(signers, dict) or not signers or not all(
            em._is_id(k) and isinstance(v, str) and em._is_id(v) for k, v in signers.items()):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'signers')
    if set(workers) & set(signers):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'a principal cannot be both worker and signer')
    if document['revoker'] not in signers:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'revoker must be a listed signer')
    keys = {}
    for signer, filename in signers.items():
        keys[signer] = _load_key(os.path.join(real, filename), uid, test_only)
    return TrustAnchor(anchor_id, workers, signers, document['revoker'],
                       HmacSha256Verifier(keys), test_only=test_only)


def _tempdir():
    import tempfile
    return tempfile.gettempdir()


def _is_abs(path):
    return isinstance(path, (str, os.PathLike)) and os.path.isabs(os.fspath(path))


def _load_key(path, uid, test_only):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'key missing') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'key must be a regular file')
    if info.st_mode & 0o077:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'key readable or writable by group or others')
    if not test_only and info.st_uid == uid:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'key owned by the verifying (worker) uid')
    key = read_bounded(path, MAX_KEY_BYTES, missing=TRUST_ANCHOR_INVALID, unsafe=TRUST_ANCHOR_INVALID)
    if len(key) < MIN_KEY_BYTES:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'key too short')
    return key


# ---------------------------------------------------------------- revocations

def _signed_body(body, signature_key):
    return {k: v for k, v in body.items() if k != signature_key}


def load_revocations(anchor, data, *, now):
    if data is None:
        raise VerificationError(REVOCATION_LIST_MISSING, 'revocation list required')
    body = parse_canonical(data, MAX_REVOCATION_BYTES, REVOCATION_MALFORMED,
                           REVOCATION_MALFORMED, REVOCATION_MALFORMED)
    _keys(body, ('schema_version', 'kind', 'anchor_id', 'issued_at', 'revoked_approval_ids',
                 'revoked_manifest_sha256', 'signature'), REVOCATION_MALFORMED)
    signature = body['signature']
    _keys(signature, ('algorithm', 'signer_id', 'value'), REVOCATION_UNAUTHENTICATED)
    if (type(body['schema_version']) is not int or body['schema_version'] != SCHEMA_VERSION
            or body['kind'] != REVOCATION_KIND or body['anchor_id'] != anchor.anchor_id):
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'wrong anchor or kind')
    issued = _parse_time(body['issued_at'])
    if issued > now:
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'list is future-dated')
    ids = body['revoked_approval_ids']
    digests = body['revoked_manifest_sha256']
    if (not isinstance(ids, list) or not isinstance(digests, list) or len(ids) > MAX_REVOKED
            or len(digests) > MAX_REVOKED or not all(em._is_id(i) for i in ids)
            or not all(_is_hex(d) for d in digests) or ids != sorted(set(ids))
            or digests != sorted(set(digests))):
        raise VerificationError(REVOCATION_MALFORMED, 'revocation entries')
    if signature['signer_id'] != anchor.revoker or signature['algorithm'] != anchor.verifier.algorithm:
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'signer is not the revoker')
    message = canonical_bytes(_signed_body(body, 'signature'))
    if not anchor.verifier.verify(anchor.revoker, DOMAIN_REVOCATION, message, signature['value']):
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'signature invalid')
    return set(ids), set(digests)


# ---------------------------------------------------------------- consumption

class ConsumptionStore(object):
    """Single-use claims. The directory must be operated by the launcher, not the worker."""

    def __init__(self, directory, *, repo_root, run_dir):
        if not _is_abs(directory):
            raise VerificationError(CONSUMPTION_STORE_INVALID, 'directory must be absolute')
        real = os.path.realpath(directory)
        _check_location(real, repo_root, run_dir, CONSUMPTION_STORE_INVALID)
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise VerificationError(CONSUMPTION_STORE_INVALID, 'must be a plain directory')
        self.directory = real

    def claim(self, approval_id, record):
        path = os.path.join(self.directory, approval_id + '.consumed')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise VerificationError(APPROVAL_CONSUMED, 'approval already consumed') from exc
        try:
            payload = canonical_bytes(record)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)


# ---------------------------------------------------------------- approvals

def _approval_body(*, approval_id, anchor_id, run_id, attempt_id, manifest_sha256, resource_class,
                   scope, output, issued_at, expires_at):
    return {'schema_version': SCHEMA_VERSION, 'kind': APPROVAL_KIND, 'approval_id': approval_id,
            'anchor_id': anchor_id, 'run_id': run_id, 'attempt_id': attempt_id,
            'manifest_sha256': manifest_sha256, 'resource_class': resource_class,
            'scope': dict(scope), 'output_destination_sha256': digest_value(output),
            'issued_at': _format_time(issued_at), 'expires_at': _format_time(expires_at)}


def issue_approval(anchor, signer_id, *, approval_id, run_id, attempt_id, manifest_sha256,
                   resource_class, scope, output, issued_at, expires_at):
    """Approver-side signing. Runs with the signer's material, never in the worker."""
    if signer_id not in anchor.signers:
        raise VerificationError(APPROVAL_SIGNER_UNAUTHORIZED, 'signer not provisioned')
    body = _approval_body(approval_id=approval_id, anchor_id=anchor.anchor_id, run_id=run_id,
                          attempt_id=attempt_id, manifest_sha256=manifest_sha256,
                          resource_class=resource_class, scope=scope, output=output,
                          issued_at=issued_at, expires_at=expires_at)
    body['signature'] = {'algorithm': ALGORITHM, 'signer_id': signer_id,
                         'value': _sign(anchor, signer_id, DOMAIN_APPROVAL, body)}
    return canonical_bytes(body)


def issue_revocation(anchor, *, issued_at, approval_ids, manifest_sha256s):
    body = {'schema_version': SCHEMA_VERSION, 'kind': REVOCATION_KIND, 'anchor_id': anchor.anchor_id,
            'issued_at': _format_time(issued_at), 'revoked_approval_ids': sorted(set(approval_ids)),
            'revoked_manifest_sha256': sorted(set(manifest_sha256s))}
    body['signature'] = {'algorithm': ALGORITHM, 'signer_id': anchor.revoker,
                         'value': _sign(anchor, anchor.revoker, DOMAIN_REVOCATION, body)}
    return canonical_bytes(body)


def _sign(anchor, signer_id, domain, body):
    if not isinstance(anchor.verifier, HmacSha256Verifier):
        raise VerificationError(TRUST_ANCHOR_UNPROVISIONED, 'no signing support for this verifier')
    return anchor.verifier.sign(signer_id, domain, canonical_bytes(body))


def verify_approval(approval_bytes, anchor, *, revocation_bytes, expected, store, now=None):
    """Authorize one attempt. Raises VerificationError on any failure.

    expected keys: worker_id, run_id, attempt_id, manifest_sha256, resource_class,
    scope (dict), output (dict with root and path, the authorized destination).
    The consumption claim is the last step, so a failed check never burns an approval.
    """
    now = now or _utcnow()
    body = parse_canonical(approval_bytes, MAX_APPROVAL_BYTES, APPROVAL_MALFORMED,
                           APPROVAL_NONCANONICAL, APPROVAL_MALFORMED)
    _keys(body, ('schema_version', 'kind', 'approval_id', 'anchor_id', 'run_id', 'attempt_id',
                 'manifest_sha256', 'resource_class', 'scope', 'output_destination_sha256',
                 'issued_at', 'expires_at', 'signature'), APPROVAL_MALFORMED)
    signature = body['signature']
    _keys(signature, ('algorithm', 'signer_id', 'value'), APPROVAL_MALFORMED)
    if (type(body['schema_version']) is not int or body['schema_version'] != SCHEMA_VERSION
            or body['kind'] != APPROVAL_KIND):
        raise VerificationError(APPROVAL_MALFORMED, 'schema version or kind')
    for key in ('approval_id', 'run_id', 'attempt_id', 'resource_class'):
        if not em._is_id(body[key]):
            raise VerificationError(APPROVAL_MALFORMED, key)
    if not _is_hex(body['manifest_sha256']) or not _is_hex(body['output_destination_sha256']):
        raise VerificationError(APPROVAL_MALFORMED, 'digest fields')
    if not isinstance(body['scope'], dict) or set(body['scope']) != {'project', 'operation'}:
        raise VerificationError(APPROVAL_MALFORMED, 'scope')
    issued = _parse_time(body['issued_at'])
    expires = _parse_time(body['expires_at'])
    if expires <= issued or (expires - issued).total_seconds() > MAX_LIFETIME_SECONDS:
        raise VerificationError(APPROVAL_MALFORMED, 'validity window')

    if body['anchor_id'] != anchor.anchor_id:
        raise VerificationError(APPROVAL_UNKNOWN_ANCHOR)
    signer = signature['signer_id']
    if signer == expected['worker_id']:
        raise VerificationError(APPROVAL_SELF_ISSUED, 'a worker cannot issue its own approval')
    if signer not in anchor.signers:
        raise VerificationError(APPROVAL_SIGNER_UNAUTHORIZED)
    if expected['worker_id'] not in anchor.workers:
        raise VerificationError(APPROVAL_WORKER_UNAUTHORIZED)
    if signature['algorithm'] != anchor.verifier.algorithm:
        raise VerificationError(APPROVAL_SIGNATURE_INVALID, 'algorithm')
    message = canonical_bytes(_signed_body(body, 'signature'))
    if not anchor.verifier.verify(signer, DOMAIN_APPROVAL, message, signature['value']):
        raise VerificationError(APPROVAL_SIGNATURE_INVALID)

    if now < issued:
        raise VerificationError(APPROVAL_NOT_YET_VALID)
    if now >= expires:
        raise VerificationError(APPROVAL_EXPIRED)
    revoked_ids, revoked_digests = load_revocations(anchor, revocation_bytes, now=now)
    if body['approval_id'] in revoked_ids or body['manifest_sha256'] in revoked_digests:
        raise VerificationError(APPROVAL_REVOKED)
    if body['run_id'] != expected['run_id']:
        raise VerificationError(APPROVAL_RUN_MISMATCH)
    if body['attempt_id'] != expected['attempt_id']:
        raise VerificationError(APPROVAL_ATTEMPT_MISMATCH)
    if body['manifest_sha256'] != expected['manifest_sha256']:
        raise VerificationError(APPROVAL_MANIFEST_MISMATCH)
    if body['resource_class'] != expected['resource_class']:
        raise VerificationError(APPROVAL_RESOURCE_MISMATCH)
    if body['scope'] != expected['scope']:
        raise VerificationError(APPROVAL_SCOPE_MISMATCH)
    if body['output_destination_sha256'] != digest_value(expected['output']):
        raise VerificationError(APPROVAL_DESTINATION_MISMATCH)

    record = {'approval_sha256': sha256_hex(bytes(approval_bytes)), 'approval_id': body['approval_id'],
              'anchor_id': anchor.anchor_id, 'signer_id': signer,
              'worker_id': expected['worker_id'], 'run_id': body['run_id'],
              'attempt_id': body['attempt_id'], 'manifest_sha256': body['manifest_sha256']}
    store.claim(body['approval_id'], record)
    return record
