"""Run ownership vocabulary: canonical execution identity, fencing tokens,
state names, crash-point names and the shape of required proof inputs.

Pure functions only. SQLite transactions live in run_registry.py. Nothing here
proves containment, process liveness, queue state or resource release. Proofs
are VerifiedProof objects minted only by issue_proof() after a registered
verifier accepts raw evidence. No verifier is registered, so no production path
can obtain a proof today. A missing, forged or mismatched proof always fails closed.
"""
import dataclasses
import hashlib
import json
import re
import uuid
from typing import Optional


class RegistryError(Exception):
    """Fail-closed ownership error. run_registry re-exports this class."""


IDENTITY_VERSION = 2
STALE_TOKEN = 'Stale owner or malformed fencing token'

# Run-local states (run.state). UNKNOWN is the ambiguous/unknown-outcome state:
# a leader exit without verified whole-workload termination.
LOCAL_STATES = ('CLAIMED', 'DISPATCHING', 'RUNNING', 'UNKNOWN', 'FENCED',
                'COMPLETED', 'FAILED', 'INTERRUPTED')
VERIFIED_OUTCOMES = ('COMPLETED', 'FAILED', 'INTERRUPTED')

# Queue index phases (queue_binding.phase).
QUEUE_PHASES = ('INTENT', 'BOUND', 'CLAIMED', 'AMBIGUOUS', 'RETRYABLE')
AMBIGUOUS_ORIGINS = ('INTENT', 'CLAIMED')

WHOLE_WORKLOAD_TERMINATED = 'WHOLE_WORKLOAD_TERMINATED'
QUEUE_LOOKUP_TASK = 'QUEUE_LOOKUP_TASK'
QUEUE_LOOKUP_NO_TASK = 'QUEUE_LOOKUP_NO_TASK'

# Named boundaries that test-only hooks may interrupt. Unknown names are
# rejected so that a typo cannot silently turn a crash test into a no-op.
CRASH_POINTS = frozenset({
    'queue_intent:before_commit',
    'queue_intent:after_commit',
    'queue_bind_task:before_commit',
    'queue_claim:after_commit',
    'claim:before_commit',
    'claim:after_commit',
    'advance:before_commit',
    'finish:before_commit',
    'execute:claimed',
    'execute:child_running',
    'execute:leader_exited',
    'execute:before_release',
})

_HEX64 = re.compile(r'[0-9a-f]{64}')
_HEX32 = re.compile(r'[0-9a-f]{32}')
_TOKEN = re.compile(r'v2\.([0-9a-f]{32})\.([1-9][0-9]{0,17})')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,254}')


def validate_name(label, value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise RegistryError('Invalid %s' % label)
    return value


def validate_digest(value):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise RegistryError('Invalid manifest digest: expected 64 lowercase hex characters')
    return value


def validate_opaque_task(task_id):
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256 or '\x00' in task_id:
        raise RegistryError('Invalid queue task ID')
    return task_id


def new_attempt_id():
    return uuid.uuid4().hex


@dataclasses.dataclass(frozen=True)
class ExecutionIdentity:
    """Canonical binding of one execution attempt. Every field is validated.

    manifest_sha256 is the digest of the submission manifest bytes that the
    runner hashes today. The W4 manifest will supply its own digest through
    this same field; placeholders are rejected so that an unbound run cannot
    be claimed.
    """
    run_id: str
    project: str
    attempt_id: str
    manifest_sha256: str
    worker_id: str
    fence: int
    task_id: Optional[str] = None

    def validate(self):
        validate_name('run_id', self.run_id)
        validate_name('project', self.project)
        if not isinstance(self.attempt_id, str) or not _HEX32.fullmatch(self.attempt_id):
            raise RegistryError('Invalid attempt identifier')
        validate_digest(self.manifest_sha256)
        validate_name('worker_id', self.worker_id)
        if type(self.fence) is not int or not 1 <= self.fence < 2 ** 63:
            raise RegistryError('Invalid fencing token')
        if self.task_id is not None:
            validate_opaque_task(self.task_id)
        return self

    def canonical_json(self):
        self.validate()
        return json.dumps({
            'version': IDENTITY_VERSION, 'run_id': self.run_id, 'project': self.project,
            'attempt_id': self.attempt_id, 'manifest_sha256': self.manifest_sha256,
            'worker_id': self.worker_id, 'task_id': self.task_id, 'fence': self.fence,
        }, sort_keys=True, separators=(',', ':'))

    def sha256(self):
        return hashlib.sha256(self.canonical_json().encode('utf-8')).hexdigest()


def encode_token(attempt_id, fence):
    if (not isinstance(attempt_id, str) or not _HEX32.fullmatch(attempt_id) or
            type(fence) is not int or not 1 <= fence < 2 ** 63):
        raise RegistryError('Invalid fencing token')
    return 'v2.%s.%d' % (attempt_id, fence)


def decode_token(token):
    if not isinstance(token, str):
        raise RegistryError(STALE_TOKEN)
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise RegistryError(STALE_TOKEN)
    return match.group(1), int(match.group(2))


def classify_row(row):
    """CURRENT only when the row carries the v2 identity and it recomputes exactly.

    Anything else, including rows written before identity columns existed or
    rows whose stored hash no longer matches their fields, is LEGACY_FENCED:
    readable as history, never claimable.
    """
    try:
        if row.get('identity_version') != IDENTITY_VERSION:
            return 'LEGACY_FENCED'
        identity = ExecutionIdentity(
            run_id=row['run_id'], project=row['project'], attempt_id=row['attempt_id'],
            manifest_sha256=row['manifest_sha256'], worker_id=row['worker_id'],
            fence=row['fence'], task_id=row.get('queue_task_id'))
        digest = identity.sha256()
    except (RegistryError, KeyError, TypeError, AttributeError):
        return 'LEGACY_FENCED'
    return 'CURRENT' if digest == row.get('identity_sha256') else 'LEGACY_FENCED'


def fault(hook, point):
    """Test-only crash boundary. Production callers pass hook=None."""
    if hook is None:
        return
    if point not in CRASH_POINTS:
        raise RegistryError('Unknown crash point: %s' % point)
    hook(point)


# Proofs are minted only by issue_proof(), after the verifier registered for
# issuer_id accepts the raw evidence. _VERIFIED_ISSUERS is empty: no production
# verifier is registered, so no production path can obtain a proof today. A dict,
# a hand-built object or an unregistered issuer is refused. This is an in-process
# guard against accidental or naive forgery; it is not a security boundary against
# code that runs in the same process.
_VERIFIED_ISSUERS = {}
_ISSUE_KEY = object()


class VerifiedProof:
    """Immutable proof minted by issue_proof(). Construction without the module key fails."""
    __slots__ = ('kind', '_bindings')

    def __init__(self, key, kind, bindings):
        if key is not _ISSUE_KEY:
            raise RegistryError('Proof objects are issued only by a registered verifier')
        object.__setattr__(self, 'kind', kind)
        object.__setattr__(self, '_bindings', tuple(sorted(dict(bindings).items())))

    def __setattr__(self, name, value):
        raise RegistryError('Proof objects are immutable')

    def binding(self, name):
        return dict(self._bindings).get(name)


def issue_proof(issuer_id, kind, bindings, raw_evidence):
    """Return a VerifiedProof only if the registered verifier accepts raw_evidence."""
    verifier = _VERIFIED_ISSUERS.get(issuer_id)
    if not callable(verifier):
        raise RegistryError('No registered verifier for issuer %s' % issuer_id)
    if kind not in (WHOLE_WORKLOAD_TERMINATED, QUEUE_LOOKUP_TASK, QUEUE_LOOKUP_NO_TASK):
        raise RegistryError('Unknown proof kind')
    if verifier(kind, dict(bindings), raw_evidence) is not True:
        raise RegistryError('Verifier rejected the evidence')
    return VerifiedProof(_ISSUE_KEY, kind, bindings)


def require_containment_proof(proof, *, run_id, attempt_id, fence):
    """Require a VerifiedProof of whole-workload termination bound to run, attempt and fence."""
    if not isinstance(proof, VerifiedProof):
        raise RegistryError('Whole-workload termination proof absent or not issued by a registered verifier')
    if proof.kind != WHOLE_WORKLOAD_TERMINATED:
        raise RegistryError('Termination proof has the wrong kind')
    for name, value in (('run_id', run_id), ('attempt_id', attempt_id), ('fence', fence)):
        bound = proof.binding(name)
        if type(bound) is not type(value) or bound != value:
            raise RegistryError('Termination proof does not bind this run, attempt and fence')
    return True


def require_queue_lookup(proof, *, intent_token, queue_instance):
    """Require a VerifiedProof of queue lookup bound to one intent; return its kind.

    The task ID for QUEUE_LOOKUP_TASK comes only from the verified proof.
    """
    if not isinstance(proof, VerifiedProof):
        raise RegistryError('Queue lookup proof absent or not issued by a registered verifier; intent stays AMBIGUOUS')
    kind = proof.kind
    if kind not in (QUEUE_LOOKUP_TASK, QUEUE_LOOKUP_NO_TASK):
        raise RegistryError('Unknown queue lookup proof kind')
    if proof.binding('intent_token') != intent_token or proof.binding('queue_instance') != queue_instance:
        raise RegistryError('Queue lookup proof does not bind this intent')
    if kind == QUEUE_LOOKUP_TASK:
        validate_opaque_task(proof.binding('task_id'))
    elif proof.binding('task_id') is not None:
        raise RegistryError('No-task proof must not carry a task ID')
    return kind
