"""Fail-closed process-containment backend contract.

No production backend or verification capability is registered. The public
backend selector therefore always returns UNAVAILABLE. This module's Python
types are an interface contract, not a security boundary: production execution
must never trust a caller-constructed result or caller-supplied data as proof.
A future backend needs independently reviewed implementation and isolated E2E
proof before a private verification capability is registered and connected.
"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Protocol


class ContainmentStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


# Intentionally empty: no production issuer currently has authority to verify.
_VERIFIED_ISSUERS = frozenset()


class ContainmentProof(Protocol):
    """Opaque backend proof; arbitrary non-null values are not verification."""

    @property
    def run_id(self) -> str: ...

    @property
    def issuer(self) -> object: ...


@dataclass(frozen=True)
class ContainmentResult:
    status: ContainmentStatus
    reason: str
    proof: Optional[ContainmentProof] = None

    def __post_init__(self):
        if self.status is ContainmentStatus.VERIFIED:
            issuer = getattr(self.proof, "issuer", None)
            if not any(issuer is trusted for trusted in _VERIFIED_ISSUERS):
                raise ValueError("No registered production issuer can verify containment")
        elif self.proof is not None:
            raise ValueError("UNAVAILABLE cannot carry a proof")


def is_verified_for_execution(result: object) -> bool:
    """Fail-closed authorization predicate for any future execution adapter."""
    if not isinstance(result, ContainmentResult):
        return False
    if result.status is not ContainmentStatus.VERIFIED:
        return False
    issuer = getattr(result.proof, "issuer", None)
    return any(issuer is trusted for trusted in _VERIFIED_ISSUERS)


class ContainmentBackend(Protocol):
    """Interface for a backend that owns the complete workload boundary."""

    def establish(self, command: List[str], *, run_id: str) -> ContainmentResult:
        """Establish containment before launching command, or report unavailable.

        Implementations must bind proof to this run and its complete descendant
        tree. A capability hint, PID observation, caller-provided JSON, or leader
        exit is not proof. This interface alone does not authorize queue execution.
        """
        ...


class UnavailableContainmentBackend:
    """Safe default until a backend is implemented and independently validated."""

    def establish(self, command: List[str], *, run_id: str) -> ContainmentResult:
        del command, run_id
        return ContainmentResult(
            status=ContainmentStatus.UNAVAILABLE,
            reason="No production containment backend is configured",
        )


def get_backend() -> ContainmentBackend:
    """Select the only currently available production backend."""
    return UnavailableContainmentBackend()
