"""Containment proof model and fail-closed release gate.

A containment proof is a verifier's signed statement about one run attempt's
boundary at one observation time. Verification here is authority-bearing, so:

- Only verifiers whose identity is present in a trust-anchor mapping can
  authorize anything. The production mapping is empty: no production verifier
  is provisioned on this host, so every production check refuses.
- Each proof must carry a MAC over all of its fields, computed with the
  verifier's anchor key. Constructing a ContainmentProof dataclass directly does
  not produce a valid MAC and therefore never verifies.
- Proofs marked simulated=True are refused unless a caller explicitly allows
  simulation. Only test code may do so; nothing under scripts/ does.
- Caller-supplied JSON, dictionaries, process identity observations and other
  non-ContainmentProof objects are refused by type.

Process identity (PID, PGID, start time) is an observation and is never a
containment proof. Occupancy must be zero and the verdict EMPTY for release.
"""
import dataclasses
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

VERDICT_EMPTY = 'EMPTY'
VERDICT_OCCUPIED = 'OCCUPIED'
VERDICT_UNKNOWN = 'UNKNOWN'
VERDICT_BREACHED = 'BREACHED'
VERDICTS = frozenset({VERDICT_EMPTY, VERDICT_OCCUPIED, VERDICT_UNKNOWN, VERDICT_BREACHED})

MAX_PROOF_AGE_NS = 30 * 10**9
MAX_CLOCK_SKEW_NS = 1 * 10**9
MIN_KEY_BYTES = 32
_MAC_DOMAIN = b'herdr-containment-proof-v1\n'

# No production verifier is provisioned. Operator-provisioned anchors would be
# installed here only after the independent review and host verification in
# reports/experiment-infra/containment/OPERATOR_REQUIREMENTS.md succeed.
PRODUCTION_TRUST_ANCHORS: Mapping[str, bytes] = MappingProxyType({})


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
    """What the supervisor issued a challenge for; a proof must match exactly."""
    run_id: str
    attempt_id: str
    boundary_id: str
    nonce: str
    epoch: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _canonical(proof: ContainmentProof) -> bytes:
    data = {f.name: getattr(proof, f.name) for f in dataclasses.fields(proof) if f.name != 'mac'}
    return _MAC_DOMAIN + json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')


def mac_for(proof: ContainmentProof, key: bytes) -> str:
    """MAC over every field except mac itself. Intended for trusted verifiers only."""
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError('verifier key must be at least %d bytes' % MIN_KEY_BYTES)
    return hmac.new(bytes(key), _canonical(proof), hashlib.sha256).hexdigest()


def seal(proof: ContainmentProof, key: bytes) -> ContainmentProof:
    """Return a copy of proof carrying a MAC from key. Only a verifier holding key can do this."""
    return dataclasses.replace(proof, mac=mac_for(proof, key))


def _refuse(reason: str) -> Decision:
    return Decision(False, reason)


def check_proof(proof: object, expectation: object, *, anchors: Mapping[str, bytes], now_ns: int,
                allow_simulated: bool = False, max_age_ns: int = MAX_PROOF_AGE_NS,
                max_skew_ns: int = MAX_CLOCK_SKEW_NS) -> Decision:
    """Shared checking logic. Not a production entry point.

    Production code must call verify_for_release(). Tests and the test-only
    simulator call this with their own anchors.
    """
    if type(proof) is not ContainmentProof:
        return _refuse('not a ContainmentProof issued by a verifier')
    if type(expectation) is not Expectation:
        return _refuse('missing or malformed expectation')
    if not isinstance(anchors, Mapping) or type(now_ns) is not int:
        return _refuse('malformed verification context')
    if proof.simulated and not allow_simulated:
        return _refuse('simulated proof is not accepted by this verifier')
    key = anchors.get(proof.verifier_id) if isinstance(proof.verifier_id, str) else None
    if key is None:
        return _refuse('proof verifier is not trusted')
    if not isinstance(proof.mac, str) or not hmac.compare_digest(mac_for(proof, key), proof.mac):
        return _refuse('proof MAC is invalid: tampered or sealed with a different key')
    if proof.run_id != expectation.run_id:
        return _refuse('proof run_id does not match the run')
    if proof.attempt_id != expectation.attempt_id:
        return _refuse('proof attempt_id does not match the attempt')
    if proof.boundary_id != expectation.boundary_id:
        return _refuse('proof boundary_id does not match the boundary')
    if proof.nonce != expectation.nonce:
        return _refuse('proof nonce does not match the challenge: stale or unsolicited')
    if type(proof.epoch) is not int or proof.epoch != expectation.epoch:
        return _refuse('proof epoch does not match the challenge: stale')
    if type(proof.observed_at_ns) is not int:
        return _refuse('proof observation time is malformed')
    age = now_ns - proof.observed_at_ns
    if age > max_age_ns:
        return _refuse('proof is stale')
    if age < -max_skew_ns:
        return _refuse('proof observation time is in the future')
    if proof.verdict not in VERDICTS:
        return _refuse('proof verdict is unknown')
    if type(proof.occupancy) is not int or proof.occupancy < 0:
        return _refuse('proof occupancy is malformed')
    if proof.verdict != VERDICT_EMPTY:
        return _refuse('proof verdict is %s, not EMPTY' % proof.verdict)
    if proof.occupancy != 0:
        return _refuse('proof reports %d descendant(s) still in the boundary' % proof.occupancy)
    return Decision(True, 'verified empty boundary for this run attempt')


def verify_for_release(proof: object, expectation: object, *, now_ns: Optional[int] = None) -> Decision:
    """The only production verification entry point. Refuses on this host."""
    if now_ns is None:
        now_ns = time.time_ns()
    return check_proof(proof, expectation, anchors=PRODUCTION_TRUST_ANCHORS, now_ns=now_ns,
                       allow_simulated=False)
