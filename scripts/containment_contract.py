"""Containment proof model and fail-closed release gate.

A containment proof is a verifier's MAC-sealed statement about one run attempt's
boundary at one observation time.

What this module enforces:

- A Verifier object owns its trust anchors, its clock, its outstanding challenges
  and its consumed-nonce store. The production release function is a closure over
  its verifier object. Rebinding module globals does not change which verifier it
  uses. Editing private verifier fields from inside the process is NOT prevented.
- Challenges come only from Verifier.issue_challenge(). An Expectation built from
  a proof's own fields is refused. Issuing a new challenge for a boundary
  supersedes earlier ones for that boundary.
- verify_for_release consumes the nonce atomically on first success and refuses
  any second use (replay).
- Every text field must be an exact str and every counter an exact int (not
  bool), within bounds. Any unexpected exception becomes a refusal.
- Simulated proofs are refused unless the verifier was built to accept them.
  Only test code builds such a verifier.

What this module does NOT enforce:

- Authority against same-user code. The HMAC seal uses a key that lives in the
  same process as the checker. Any code running in the worker process can read
  the key or call the sealing function. In-process code is outside the threat
  model. Real authority requires a verifier running as a separate identity whose
  key is held outside the worker's reach. That is the trust-anchor requirement
  also stated for W4 APPROVAL.md (not verified from this worktree), and nothing
  like it is provisioned on this host.
- Kernel-enforced confinement. Occupancy is whatever the verifier observes. No
  kernel mechanism is consulted here.

Process identity (PID, PGID, start time) is an observation and is never a
containment proof. Occupancy must be zero and the verdict EMPTY for release.
"""
import dataclasses
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Set, Tuple

VERDICT_EMPTY = 'EMPTY'
VERDICT_OCCUPIED = 'OCCUPIED'
VERDICT_UNKNOWN = 'UNKNOWN'
VERDICT_BREACHED = 'BREACHED'
VERDICTS = frozenset({VERDICT_EMPTY, VERDICT_OCCUPIED, VERDICT_UNKNOWN, VERDICT_BREACHED})

MAX_PROOF_AGE_NS = 30 * 10**9
MAX_CLOCK_SKEW_NS = 1 * 10**9
MIN_KEY_BYTES = 32
MAX_TEXT_LEN = 256
MAX_INT = 2**63 - 1
MAX_OCCUPANCY = 2**20
MAX_WINDOW_NS = 24 * 3600 * 10**9  # no freshness or skew window may exceed 24 h
_MAC_DOMAIN = b'herdr-containment-proof-v1\n'

Key = Tuple[str, str, str, str, int]


@dataclass(frozen=True)
class ContainmentProof:
    run_id: str
    attempt_id: str
    boundary_id: str
    verifier_id: str
    observed_at_ns: int
    nonce: str
    epoch: int
    occupancy: int
    verdict: str
    simulated: bool
    mac: str = ''


@dataclass(frozen=True)
class Expectation:
    """A challenge the verifier issued. A proof must match it exactly."""
    run_id: str
    attempt_id: str
    boundary_id: str
    nonce: str
    epoch: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _text(value: object, *, maximum: int = MAX_TEXT_LEN) -> bool:
    return type(value) is str and 0 < len(value) <= maximum


def _bounded_int(value: object, *, high: int = MAX_INT) -> bool:
    return type(value) is int and 0 <= value <= high


def _proof_shape_error(proof: ContainmentProof) -> Optional[str]:
    for name in ('run_id', 'attempt_id', 'boundary_id', 'verifier_id', 'nonce', 'verdict', 'mac'):
        if not _text(getattr(proof, name)):
            return 'proof field %s is malformed' % name
    if proof.verdict not in VERDICTS:
        return 'proof verdict is unknown'
    if not _bounded_int(proof.observed_at_ns) or not _bounded_int(proof.epoch):
        return 'proof time or epoch is malformed'
    if not _bounded_int(proof.occupancy, high=MAX_OCCUPANCY):
        return 'proof occupancy is malformed'
    if type(proof.simulated) is not bool:
        return 'proof simulated flag is malformed'
    return None


def _expectation_shape_error(expectation: Expectation) -> Optional[str]:
    for name in ('run_id', 'attempt_id', 'boundary_id', 'nonce'):
        if not _text(getattr(expectation, name)):
            return 'expectation field %s is malformed' % name
    if not _bounded_int(expectation.epoch):
        return 'expectation epoch is malformed'
    return None


def _key_of(expectation: Expectation) -> Key:
    return (expectation.run_id, expectation.attempt_id, expectation.boundary_id,
            expectation.nonce, expectation.epoch)


def _canonical(proof: ContainmentProof) -> bytes:
    data = {f.name: getattr(proof, f.name) for f in dataclasses.fields(proof) if f.name != 'mac'}
    return _MAC_DOMAIN + json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')


def mac_for(proof: ContainmentProof, key: bytes) -> str:
    """HMAC over every field except mac. Only code holding key can produce a valid seal.

    The key is in-process material. See the module docstring for the threat model.
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError('verifier key must be at least %d bytes' % MIN_KEY_BYTES)
    return hmac.new(bytes(key), _canonical(proof), hashlib.sha256).hexdigest()


def seal(proof: ContainmentProof, key: bytes) -> ContainmentProof:
    """Return a copy of proof carrying a MAC from key. Used by trusted verifiers and test code."""
    return dataclasses.replace(proof, mac=mac_for(proof, key))


def _refuse(reason: str) -> Decision:
    return Decision(False, reason)


class Verifier:
    """Verifier-owned state: trust anchors, clock, outstanding challenges and consumed nonces."""

    def __init__(self, *, anchors: Mapping[str, bytes], clock: Callable[[], int],
                 allow_simulated: bool = False, max_age_ns: int = MAX_PROOF_AGE_NS,
                 max_skew_ns: int = MAX_CLOCK_SKEW_NS):
        if type(allow_simulated) is not bool:
            raise TypeError('allow_simulated must be a bool')
        for name, window, minimum in (('max_age_ns', max_age_ns, 1), ('max_skew_ns', max_skew_ns, 0)):
            if type(window) is not int:
                raise TypeError('%s must be an int' % name)
            if not minimum <= window <= MAX_WINDOW_NS:
                raise ValueError('%s must be between %d and %d nanoseconds' % (name, minimum, MAX_WINDOW_NS))
        copied = {}
        for verifier_id, key in dict(anchors).items():
            if not _text(verifier_id) or not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
                raise ValueError('trust anchor is malformed')
            copied[verifier_id] = bytes(key)
        self.__anchors = copied
        self.__clock = clock
        self.__allow_simulated = bool(allow_simulated)
        self.__max_age_ns = int(max_age_ns)
        self.__max_skew_ns = int(max_skew_ns)
        self.__mac_for = mac_for
        self.__lock = threading.Lock()
        self.__outstanding: Set[Key] = set()
        self.__consumed: Set[Key] = set()

    def now_ns(self) -> int:
        value = self.__clock()
        if not _bounded_int(value):
            raise ValueError('verifier clock returned a malformed time')
        return value

    def issue_challenge(self, *, run_id: str, attempt_id: str, boundary_id: str, epoch: int) -> Expectation:
        """Issue a fresh challenge. It supersedes earlier outstanding challenges for the same boundary."""
        if not (_text(run_id) and _text(attempt_id) and _text(boundary_id) and _bounded_int(epoch)):
            raise ValueError('challenge fields are malformed')
        expectation = Expectation(run_id=run_id, attempt_id=attempt_id, boundary_id=boundary_id,
                                  nonce=secrets.token_hex(16), epoch=epoch)
        with self.__lock:
            self.__outstanding = {k for k in self.__outstanding if k[2] != boundary_id}
            self.__outstanding.add(_key_of(expectation))
        return expectation

    def preview(self, proof: object, expectation: object) -> Decision:
        """Check without consuming the nonce. Used for completion, never for release."""
        return self.__evaluate(proof, expectation, consume=False)

    def verify_for_release(self, proof: object, expectation: object) -> Decision:
        """Check and consume the nonce atomically. A second call with the same challenge is refused."""
        return self.__evaluate(proof, expectation, consume=True)

    def __evaluate(self, proof: object, expectation: object, *, consume: bool) -> Decision:
        try:
            with self.__lock:
                return self.__decide(proof, expectation, consume)
        except Exception as exc:  # fail closed on any unexpected input or state
            return _refuse('verification failed closed (%s)' % type(exc).__name__)

    def __decide(self, proof: object, expectation: object, consume: bool) -> Decision:
        if type(proof) is not ContainmentProof:
            return _refuse('not a ContainmentProof issued by a verifier')
        if type(expectation) is not Expectation:
            return _refuse('missing or malformed expectation')
        shape = _proof_shape_error(proof) or _expectation_shape_error(expectation)
        if shape:
            return _refuse(shape)
        if proof.simulated and not self.__allow_simulated:
            return _refuse('simulated proof is not accepted by this verifier')
        key = self.__anchors.get(proof.verifier_id)
        if key is None:
            return _refuse('proof verifier is not trusted')
        if not hmac.compare_digest(self.__mac_for(proof, key), proof.mac):
            return _refuse('proof MAC is invalid: tampered or sealed with a different key')
        if proof.run_id != expectation.run_id:
            return _refuse('proof run_id does not match the run')
        if proof.attempt_id != expectation.attempt_id:
            return _refuse('proof attempt_id does not match the attempt')
        if proof.boundary_id != expectation.boundary_id:
            return _refuse('proof boundary_id does not match the boundary')
        if proof.nonce != expectation.nonce:
            return _refuse('proof nonce does not match the challenge: stale or unsolicited')
        if proof.epoch != expectation.epoch:
            return _refuse('proof epoch does not match the challenge: stale')
        age = self.now_ns() - proof.observed_at_ns
        if age > self.__max_age_ns:
            return _refuse('proof is stale')
        if age < -self.__max_skew_ns:
            return _refuse('proof observation time is in the future')
        if proof.verdict != VERDICT_EMPTY:
            return _refuse('proof verdict is %s, not EMPTY' % proof.verdict)
        if proof.occupancy != 0:
            return _refuse('proof reports %d descendant(s) still in the boundary' % proof.occupancy)
        challenge = _key_of(expectation)
        if challenge in self.__consumed:
            return _refuse('challenge nonce already consumed: replay refused')
        if challenge not in self.__outstanding:
            return _refuse('expectation was not issued by this verifier or was superseded')
        if consume:
            self.__consumed.add(challenge)
            self.__outstanding.discard(challenge)
        return Decision(True, 'verified empty boundary for this run attempt')


# Production verifier: no trust anchors are provisioned on this host, so every
# proof is refused. Anchors, clock and limits are fixed at construction.
_PRODUCTION_VERIFIER = Verifier(anchors={}, clock=time.time_ns, allow_simulated=False,
                                max_age_ns=MAX_PROOF_AGE_NS, max_skew_ns=MAX_CLOCK_SKEW_NS)


def _bind_release(verifier: Verifier) -> Callable[[object, object], Decision]:
    """Return a release function that holds verifier in its closure.

    Rebinding the module-level _PRODUCTION_VERIFIER name later does not change
    which verifier this function uses. In-process code that edits the verifier's
    private fields or rebinds this function's module name is outside the threat
    model (see docs/CONTAINMENT.md and reports/experiment-infra/CONTAINMENT.md).
    """
    def verify_for_release(proof: object, expectation: object) -> Decision:
        """The only production verification entry point. Refuses on this host.

        The production verifier supplies its own clock, anchors and challenge store.
        Callers cannot choose the time, the anchors, or the challenge: the expectation
        must have been issued by the production verifier.
        """
        return verifier.verify_for_release(proof, expectation)

    return verify_for_release


verify_for_release = _bind_release(_PRODUCTION_VERIFIER)
