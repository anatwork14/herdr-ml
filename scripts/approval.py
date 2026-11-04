#!/usr/bin/env python3
"""Run-specific execution approval verification. OFFLINE_TESTED only.

An approval authorizes one manifest digest for one worker, one run, one attempt,
one resource class, one scope and one output destination, inside a validity
window, unless revoked, and only once. The signed body names the worker, and the
verifier requires it to equal the worker the launcher intends to run; the
anchor's workers list is only an allowlist. The consumption store claims the
approval (approval_id, run_id, attempt_id) and the attempt (run_id, attempt_id),
so a second approval for the same attempt is refused.

Verification must run in the trusted launcher. The worker must never load
signing or verification keys and must never be able to write the consumption
state, the revocation sequence state or the revocation list.

Ownership checks use explicit uid allowlists supplied by the caller:
admin_uids (owners permitted for trust material) and worker_uids (uids the
worker runs as; these must not own any trust material). The verifier's own uid
is not special. Production mode requires both sets. Test-only mode skips
ownership checks and requires a TEST-ONLY label and a temp-directory anchor.

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
import secrets
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import execution_manifest as em  # noqa: E402
from execution_manifest import (VerificationError, canonical_bytes, digest_value,  # noqa: E402
                                parse_canonical, read_bounded, sha256_hex)

APPROVAL_KIND = 'herdr-ml-execution-approval'
REVOCATION_KIND = 'herdr-ml-revocation-list'
ANCHOR_KIND = 'herdr-ml-trust-anchor'
STATE_KIND = 'herdr-ml-revocation-sequence'
SCHEMA_VERSION = 1
ALGORITHM = 'HMAC-SHA256'
DOMAIN_APPROVAL = b'herdr-ml-approval-v1\n'
DOMAIN_REVOCATION = b'herdr-ml-revocation-list-v1\n'
# Operator-provisioned location outside the repository, run directories and the
# worker's writable scope. Production callers pass it explicitly. Nothing reads an
# environment override, so a worker cannot redirect verification.
DEFAULT_TRUST_ANCHOR_DIR = '/etc/herdr-ml/trust/execution-approval'
REVOCATION_STATE_FILE = 'revocation-sequence.json'
TEST_ONLY_PREFIX = 'TEST-ONLY-'
MIN_KEY_BYTES = 32
MAX_KEY_BYTES = 4096
MAX_ANCHOR_BYTES = 64 * 1024
MAX_STATE_BYTES = 4096
MAX_APPROVAL_BYTES = 64 * 1024
MAX_REVOCATION_BYTES = 1024 * 1024
MAX_REVOKED = 4096
MAX_LIFETIME_SECONDS = 24 * 3600
TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
APPROVAL_KEYS = ('schema_version', 'kind', 'approval_id', 'anchor_id', 'worker_id', 'run_id',
                 'attempt_id', 'manifest_sha256', 'resource_class', 'scope',
                 'output_destination_sha256', 'issued_at', 'expires_at', 'signature')
REVOCATION_KEYS = ('schema_version', 'kind', 'anchor_id', 'issued_at', 'sequence',
                   'revoked_approval_ids', 'revoked_manifest_sha256', 'signature')

# Reason codes (approval, trust material and state). Manifest codes live in execution_manifest.
APPROVAL_MALFORMED = 'APPROVAL_MALFORMED'
APPROVAL_NONCANONICAL = 'APPROVAL_NONCANONICAL'
APPROVAL_UNKNOWN_ANCHOR = 'APPROVAL_UNKNOWN_ANCHOR'
APPROVAL_SELF_ISSUED = 'APPROVAL_SELF_ISSUED'
APPROVAL_SIGNER_UNAUTHORIZED = 'APPROVAL_SIGNER_UNAUTHORIZED'
APPROVAL_WORKER_UNAUTHORIZED = 'APPROVAL_WORKER_UNAUTHORIZED'
APPROVAL_WORKER_MISMATCH = 'APPROVAL_WORKER_MISMATCH'
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
APPROVAL_ATTEMPT_ALREADY_CLAIMED = 'APPROVAL_ATTEMPT_ALREADY_CLAIMED'
REVOCATION_LIST_MISSING = 'REVOCATION_LIST_MISSING'
REVOCATION_MALFORMED = 'REVOCATION_MALFORMED'
REVOCATION_UNAUTHENTICATED = 'REVOCATION_UNAUTHENTICATED'
REVOCATION_ROLLBACK = 'REVOCATION_ROLLBACK'
REVOCATION_STATE_MISSING = 'REVOCATION_STATE_MISSING'
REVOCATION_STATE_INVALID = 'REVOCATION_STATE_INVALID'
REVOCATION_STATE_ANCHOR_MISMATCH = 'REVOCATION_STATE_ANCHOR_MISMATCH'
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


def _is_abs(path):
    return isinstance(path, (str, os.PathLike)) and os.path.isabs(os.fspath(path))


def _test_root(test_root, code):
    """The directory a test-only anchor or state must live under.

    It must be passed explicitly by the test; nothing is derived from the environment
    (TMPDIR) or tempfile.gettempdir().
    """
    if test_root is None:
        raise VerificationError(code, 'test-only mode requires an explicit test_root')
    if not _is_abs(test_root):
        raise VerificationError(code, 'test_root must be absolute')
    root = os.path.realpath(os.fspath(test_root))
    if root == os.sep:
        raise VerificationError(code, 'test_root cannot be the filesystem root')
    return root


def _within(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _plain_path(path, code):
    """Absolute, normalized, and without any symlink component."""
    if not _is_abs(path):
        raise VerificationError(code, 'path must be absolute')
    normalized = os.path.normpath(os.fspath(path))
    if os.path.realpath(normalized) != normalized:
        raise VerificationError(code, 'symlink component in path')
    return normalized


def _check_location(real, repo_root, run_dir, code=TRUST_ANCHOR_INVALID):
    for forbidden in (repo_root, run_dir):
        if forbidden is None:
            raise VerificationError(code, 'repository and run locations are required')
        if _within(real, os.path.realpath(forbidden)):
            raise VerificationError(code, 'inside repository or run directory')


def _uid_sets(admin_uids, worker_uids, code):
    """Production requires explicit, disjoint uid allowlists. The verifier's own uid is not used."""
    if admin_uids is None or worker_uids is None:
        raise VerificationError(code, 'administrative and worker uid allowlists are required')
    admin = frozenset(admin_uids)
    workers = frozenset(worker_uids)
    if not admin or not all(type(u) is int and u >= 0 for u in admin | workers):
        raise VerificationError(code, 'uid allowlists must hold non-negative integers; admin non-empty')
    if admin & workers:
        raise VerificationError(code, 'a uid cannot be both administrative and worker')
    return admin, workers


def _check_owner(info, path, admin, workers, code):
    if info.st_uid in workers or info.st_uid not in admin:
        raise VerificationError(code, 'owner uid %d of %s is not an administrative owner'
                                % (info.st_uid, path))


def _check_directory(path, *, admin, workers, test_only, code):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise VerificationError(code, 'directory unreadable') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise VerificationError(code, 'must be a plain directory')
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise VerificationError(code, 'group- or other-writable directory')
    if not test_only:
        _check_owner(info, path, admin, workers, code)
    return info


def _check_ancestors(real, *, admin, workers, code=TRUST_ANCHOR_INVALID):
    """Every directory from real up to the filesystem root must pass _check_directory."""
    current = real
    while True:
        _check_directory(current, admin=admin, workers=workers, test_only=False, code=code)
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _check_file(path, *, admin, workers, test_only, code):
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise VerificationError(code, 'file missing') from exc
    except OSError as exc:
        raise VerificationError(code, 'file unreadable') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError(code, 'must be a regular file')
    if info.st_mode & 0o077:
        raise VerificationError(code, 'readable or writable by group or others')
    if not test_only:
        _check_owner(info, path, admin, workers, code)
    return info


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- signature verifiers

class SignatureVerifier(abc.ABC):
    """Interface for approval signature verification.

    An asymmetric implementation (for example Ed25519 from an external, audited
    library) implements verify() using only public material. The stdlib
    implementation below is HMAC-SHA256 and holds signer keys.
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
        return isinstance(signer_id, str) and signer_id in self._keys

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

def _load_key(path, *, admin, workers, test_only):
    _check_file(path, admin=admin, workers=workers, test_only=test_only, code=TRUST_ANCHOR_INVALID)
    key = read_bounded(path, MAX_KEY_BYTES, missing=TRUST_ANCHOR_INVALID, unsafe=TRUST_ANCHOR_INVALID)
    if len(key) < MIN_KEY_BYTES:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'key too short')
    return key


def load_trust_anchor(anchor_dir, *, repo_root, run_dir, test_only=False, admin_uids=None,
                      worker_uids=None, test_root=None):
    """Load an operator-provisioned anchor.

    Production mode requires admin_uids and worker_uids. Every directory from the anchor
    up to the root, the anchor.json file and each key file must be owned by an admin uid
    that is not a worker uid, and must not be group- or other-writable (directories) or
    group- or other-accessible (files).

    test_only=True is for the test suite only: it requires the TEST-ONLY- label, a
    directory under the test_root the test passes explicitly (never TMPDIR or
    tempfile.gettempdir()), and skips ownership checks. test_root is refused in
    production mode.
    """
    anchor_dir = _plain_path(anchor_dir, TRUST_ANCHOR_INVALID)
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
    if test_only:
        admin, workers = (), ()
        if not _within(real, _test_root(test_root, TRUST_ANCHOR_INVALID)):
            raise VerificationError(TRUST_ANCHOR_INVALID, 'test-only anchors must live under test_root')
    else:
        if test_root is not None:
            raise VerificationError(TRUST_ANCHOR_INVALID, 'test_root is only valid in test-only mode')
        admin, workers = _uid_sets(admin_uids, worker_uids, TRUST_ANCHOR_INVALID)
        _check_ancestors(real, admin=admin, workers=workers)
    _check_directory(real, admin=admin, workers=workers, test_only=test_only, code=TRUST_ANCHOR_INVALID)
    anchor_file = os.path.join(real, 'anchor.json')
    _check_file(anchor_file, admin=admin, workers=workers, test_only=test_only, code=TRUST_ANCHOR_INVALID)
    document = parse_canonical(
        read_bounded(anchor_file, MAX_ANCHOR_BYTES, missing=TRUST_ANCHOR_INVALID, unsafe=TRUST_ANCHOR_INVALID),
        MAX_ANCHOR_BYTES, TRUST_ANCHOR_INVALID, TRUST_ANCHOR_INVALID, TRUST_ANCHOR_INVALID,
        TRUST_ANCHOR_INVALID)
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
    workers_listed = document['workers']
    signers = document['signers']
    if (not isinstance(workers_listed, list) or not workers_listed
            or not all(em._is_id(w) for w in workers_listed) or workers_listed != sorted(set(workers_listed))):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'workers')
    if not isinstance(signers, dict) or not signers or not all(
            em._is_id(k) and isinstance(v, str) and em._is_id(v) for k, v in signers.items()):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'signers')
    if set(workers_listed) & set(signers):
        raise VerificationError(TRUST_ANCHOR_INVALID, 'a principal cannot be both worker and signer')
    if document['revoker'] not in signers:
        raise VerificationError(TRUST_ANCHOR_INVALID, 'revoker must be a listed signer')
    keys = {}
    for signer, filename in signers.items():
        keys[signer] = _load_key(os.path.join(real, filename), admin=admin, workers=workers,
                                 test_only=test_only)
    return TrustAnchor(anchor_id, workers_listed, signers, document['revoker'],
                       HmacSha256Verifier(keys), test_only=test_only)


# ---------------------------------------------------------------- state directories

def _open_state_dir(directory, *, repo_root, run_dir, admin_uids, worker_uids, test_only, code,
                    test_root=None):
    real = _plain_path(directory, code)
    _check_location(real, repo_root, run_dir, code)
    if test_only:
        admin, workers = (), ()
        if not _within(real, _test_root(test_root, code)):
            raise VerificationError(code, 'test-only state must live under test_root')
    else:
        if test_root is not None:
            raise VerificationError(code, 'test_root is only valid in test-only mode')
        admin, workers = _uid_sets(admin_uids, worker_uids, code)
        _check_ancestors(real, admin=admin, workers=workers, code=code)
    _check_directory(real, admin=admin, workers=workers, test_only=test_only, code=code)
    return real, admin, workers


class RevocationState(object):
    """Monotonic revocation sequence recorded by the launcher, not the worker.

    Provisioning writes sequence 0 (provision_revocation_state). A missing file fails
    closed. A list with a lower sequence than the recorded one is refused.
    """

    def __init__(self, directory, *, repo_root, run_dir, anchor_id, admin_uids=None, worker_uids=None,
                 test_only=False, test_root=None):
        self.directory, self._admin, self._workers = _open_state_dir(
            directory, repo_root=repo_root, run_dir=run_dir, admin_uids=admin_uids,
            worker_uids=worker_uids, test_only=test_only, code=REVOCATION_STATE_INVALID,
            test_root=test_root)
        self.anchor_id = anchor_id
        self._test_only = test_only

    def _path(self):
        return os.path.join(self.directory, REVOCATION_STATE_FILE)

    def last(self):
        path = self._path()
        try:
            os.lstat(path)
        except FileNotFoundError as exc:
            raise VerificationError(REVOCATION_STATE_MISSING, 'revocation sequence not provisioned') from exc
        _check_file(path, admin=self._admin, workers=self._workers, test_only=self._test_only,
                    code=REVOCATION_STATE_INVALID)
        document = parse_canonical(
            read_bounded(path, MAX_STATE_BYTES, missing=REVOCATION_STATE_MISSING,
                         unsafe=REVOCATION_STATE_INVALID),
            MAX_STATE_BYTES, REVOCATION_STATE_INVALID, REVOCATION_STATE_INVALID,
            REVOCATION_STATE_INVALID, REVOCATION_STATE_INVALID)
        _keys(document, ('schema_version', 'kind', 'anchor_id', 'sequence'), REVOCATION_STATE_INVALID)
        if (type(document['schema_version']) is not int or document['schema_version'] != SCHEMA_VERSION
                or document['kind'] != STATE_KIND or document['anchor_id'] != self.anchor_id
                or not em._is_int(document['sequence']) or document['sequence'] < 0):
            raise VerificationError(REVOCATION_STATE_INVALID, 'state content')
        return document['sequence']

    def advance(self, sequence):
        """Record a newer sequence atomically; refuse a lower one."""
        last = self.last()
        if sequence < last:
            raise VerificationError(REVOCATION_ROLLBACK, 'list sequence is below the recorded sequence')
        if sequence == last:
            return
        payload = canonical_bytes({'schema_version': SCHEMA_VERSION, 'kind': STATE_KIND,
                                   'anchor_id': self.anchor_id, 'sequence': sequence})
        temp = os.path.join(self.directory, '.%s.%s.tmp' % (REVOCATION_STATE_FILE, secrets.token_hex(8)))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            _discard(temp)
            raise VerificationError(REVOCATION_STATE_INVALID, 'cannot record sequence') from exc
        os.close(fd)
        os.replace(temp, self._path())
        _fsync_dir(self.directory)


def provision_revocation_state(directory, *, anchor_id):
    """Create the sequence record at 0. Operator or test use only; refuses to overwrite."""
    fd = os.open(os.path.join(directory, REVOCATION_STATE_FILE),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, canonical_bytes({'schema_version': SCHEMA_VERSION, 'kind': STATE_KIND,
                                      'anchor_id': anchor_id, 'sequence': 0}))
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(directory)


# ---------------------------------------------------------------- consumption

def _claim_name(*parts):
    return sha256_hex(canonical_bytes(list(parts))) + '.claim'


class ConsumptionStore(object):
    """Single-use claims. The directory must be operated by the launcher, not the worker.

    Two claim files are created with O_EXCL and fsynced: one for the approval keyed by
    (approval_id, run_id, attempt_id), then one for the attempt keyed by
    (run_id, attempt_id). A second approval for the same attempt is refused.
    """

    def __init__(self, directory, *, repo_root, run_dir, admin_uids=None, worker_uids=None,
                 test_only=False, test_root=None):
        self.directory, self._admin, self._workers = _open_state_dir(
            directory, repo_root=repo_root, run_dir=run_dir, admin_uids=admin_uids,
            worker_uids=worker_uids, test_only=test_only, code=CONSUMPTION_STORE_INVALID,
            test_root=test_root)
        self._test_only = test_only

    def claim(self, approval_id, *, run_id, attempt_id, record):
        if not (em._is_id(approval_id) and em._is_id(run_id) and em._is_id(attempt_id)):
            raise VerificationError(CONSUMPTION_STORE_INVALID, 'identifiers')
        self._create(_claim_name('approval', approval_id, run_id, attempt_id),
                     APPROVAL_CONSUMED, record)
        self._create(_claim_name('attempt', run_id, attempt_id),
                     APPROVAL_ATTEMPT_ALREADY_CLAIMED, record)

    def _create(self, name, refusal, record):
        path = os.path.join(self.directory, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | getattr(os, 'O_CLOEXEC', 0), 0o600)
        except FileExistsError as exc:
            # An existing claim must still be a private, administrator-owned file.
            _check_file(path, admin=self._admin, workers=self._workers, test_only=self._test_only,
                        code=CONSUMPTION_STORE_INVALID)
            raise VerificationError(refusal, 'already claimed') from exc
        except OSError as exc:
            raise VerificationError(CONSUMPTION_STORE_INVALID, 'cannot create claim') from exc
        try:
            payload = canonical_bytes(record)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except (OSError, VerificationError) as exc:
            raise VerificationError(CONSUMPTION_STORE_INVALID, 'cannot record claim') from exc
        finally:
            os.close(fd)
        _fsync_dir(self.directory)


# ---------------------------------------------------------------- revocations

def load_revocations(anchor, data, *, now, state):
    """Authenticate a revocation list and advance the recorded sequence."""
    if state.anchor_id != anchor.anchor_id:
        raise VerificationError(REVOCATION_STATE_ANCHOR_MISMATCH, 'sequence state belongs to another anchor')
    if data is None:
        raise VerificationError(REVOCATION_LIST_MISSING, 'revocation list required')
    body = parse_canonical(data, MAX_REVOCATION_BYTES, REVOCATION_MALFORMED,
                           REVOCATION_MALFORMED, REVOCATION_MALFORMED, REVOCATION_MALFORMED)
    _keys(body, REVOCATION_KEYS, REVOCATION_MALFORMED)
    signature = body['signature']
    _keys(signature, ('algorithm', 'signer_id', 'value'), REVOCATION_UNAUTHENTICATED)
    if (type(body['schema_version']) is not int or body['schema_version'] != SCHEMA_VERSION
            or body['kind'] != REVOCATION_KIND or body['anchor_id'] != anchor.anchor_id):
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'wrong anchor or kind')
    if not em._is_int(body['sequence']) or body['sequence'] < 0:
        raise VerificationError(REVOCATION_MALFORMED, 'sequence')
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
    if (not isinstance(signature['signer_id'], str) or signature['signer_id'] != anchor.revoker
            or signature['algorithm'] != anchor.verifier.algorithm):
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'signer is not the revoker')
    message = canonical_bytes({k: v for k, v in body.items() if k != 'signature'})
    if not anchor.verifier.verify(anchor.revoker, DOMAIN_REVOCATION, message, signature['value']):
        raise VerificationError(REVOCATION_UNAUTHENTICATED, 'signature invalid')
    state.advance(body['sequence'])
    return set(ids), set(digests)


# ---------------------------------------------------------------- approvals

def _approval_body(*, approval_id, anchor_id, worker_id, run_id, attempt_id, manifest_sha256,
                   resource_class, scope, output, issued_at, expires_at):
    return {'schema_version': SCHEMA_VERSION, 'kind': APPROVAL_KIND, 'approval_id': approval_id,
            'anchor_id': anchor_id, 'worker_id': worker_id, 'run_id': run_id, 'attempt_id': attempt_id,
            'manifest_sha256': manifest_sha256, 'resource_class': resource_class,
            'scope': dict(scope), 'output_destination_sha256': digest_value(output),
            'issued_at': _format_time(issued_at), 'expires_at': _format_time(expires_at)}


def issue_approval(anchor, signer_id, *, approval_id, worker_id, run_id, attempt_id, manifest_sha256,
                   resource_class, scope, output, issued_at, expires_at):
    """Approver-side signing. Runs with the signer's material, never in the worker."""
    if signer_id not in anchor.signers:
        raise VerificationError(APPROVAL_SIGNER_UNAUTHORIZED, 'signer not provisioned')
    body = _approval_body(approval_id=approval_id, anchor_id=anchor.anchor_id, worker_id=worker_id,
                          run_id=run_id, attempt_id=attempt_id, manifest_sha256=manifest_sha256,
                          resource_class=resource_class, scope=scope, output=output,
                          issued_at=issued_at, expires_at=expires_at)
    body['signature'] = {'algorithm': ALGORITHM, 'signer_id': signer_id,
                         'value': _sign(anchor, signer_id, DOMAIN_APPROVAL, body)}
    return canonical_bytes(body)


def issue_revocation(anchor, *, issued_at, sequence, approval_ids, manifest_sha256s):
    body = {'schema_version': SCHEMA_VERSION, 'kind': REVOCATION_KIND, 'anchor_id': anchor.anchor_id,
            'issued_at': _format_time(issued_at), 'sequence': sequence,
            'revoked_approval_ids': sorted(set(approval_ids)),
            'revoked_manifest_sha256': sorted(set(manifest_sha256s))}
    body['signature'] = {'algorithm': ALGORITHM, 'signer_id': anchor.revoker,
                         'value': _sign(anchor, anchor.revoker, DOMAIN_REVOCATION, body)}
    return canonical_bytes(body)


def _sign(anchor, signer_id, domain, body):
    if not isinstance(anchor.verifier, HmacSha256Verifier):
        raise VerificationError(TRUST_ANCHOR_UNPROVISIONED, 'no signing support for this verifier')
    return anchor.verifier.sign(signer_id, domain, canonical_bytes(body))


def verify_approval(approval_bytes, anchor, *, revocation_bytes, revocation_state, expected, store,
                    now=None):
    """Authorize one attempt. Raises VerificationError on any failure.

    expected keys: worker_id, run_id, attempt_id, manifest_sha256, resource_class,
    scope (dict), output (dict with root and path, the authorized destination).
    The consumption claim is the last step, so a failed check never burns an approval.
    """
    now = now or _utcnow()
    body = parse_canonical(approval_bytes, MAX_APPROVAL_BYTES, APPROVAL_MALFORMED,
                           APPROVAL_NONCANONICAL, APPROVAL_MALFORMED, APPROVAL_MALFORMED)
    _keys(body, APPROVAL_KEYS, APPROVAL_MALFORMED)
    signature = body['signature']
    _keys(signature, ('algorithm', 'signer_id', 'value'), APPROVAL_MALFORMED)
    if (type(body['schema_version']) is not int or body['schema_version'] != SCHEMA_VERSION
            or body['kind'] != APPROVAL_KIND):
        raise VerificationError(APPROVAL_MALFORMED, 'schema version or kind')
    for key in ('approval_id', 'worker_id', 'run_id', 'attempt_id', 'resource_class'):
        if not em._is_id(body[key]):
            raise VerificationError(APPROVAL_MALFORMED, key)
    if not _is_hex(body['manifest_sha256']) or not _is_hex(body['output_destination_sha256']):
        raise VerificationError(APPROVAL_MALFORMED, 'digest fields')
    if not isinstance(body['scope'], dict) or set(body['scope']) != {'project', 'operation'}:
        raise VerificationError(APPROVAL_MALFORMED, 'scope')
    if not em._is_id(signature['signer_id']):
        raise VerificationError(APPROVAL_MALFORMED, 'signer_id')
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
    message = canonical_bytes({k: v for k, v in body.items() if k != 'signature'})
    if not anchor.verifier.verify(signer, DOMAIN_APPROVAL, message, signature['value']):
        raise VerificationError(APPROVAL_SIGNATURE_INVALID)
    if body['worker_id'] != expected['worker_id']:
        raise VerificationError(APPROVAL_WORKER_MISMATCH, 'approval names a different worker')

    if now < issued:
        raise VerificationError(APPROVAL_NOT_YET_VALID)
    if now >= expires:
        raise VerificationError(APPROVAL_EXPIRED)
    revoked_ids, revoked_digests = load_revocations(anchor, revocation_bytes, now=now,
                                                    state=revocation_state)
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
    store.claim(body['approval_id'], run_id=body['run_id'], attempt_id=body['attempt_id'], record=record)
    return record
