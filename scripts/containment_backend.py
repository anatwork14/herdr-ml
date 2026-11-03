"""Fail-closed contract for process containment backends.

No production backend is implemented here. Capability probes and caller supplied
claims cannot produce VERIFIED; the default backend always reports UNAVAILABLE.
A future backend must supply opaque backend proof and pass isolated end-to-end
validation before it can be wired into queue execution.
"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Protocol


class ContainmentStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


class ContainmentProof(Protocol):
    """Opaque proof object issued and validated by a concrete backend."""

    @property
    def run_id(self) -> str: ...


@dataclass(frozen=True)
class ContainmentResult:
    status: ContainmentStatus
    reason: str
    proof: Optional[ContainmentProof] = None

    def __post_init__(self):
        if self.status is ContainmentStatus.VERIFIED and self.proof is None:
            raise ValueError("VERIFIED requires an opaque backend proof")
        if self.status is ContainmentStatus.UNAVAILABLE and self.proof is not None:
            raise ValueError("UNAVAILABLE cannot carry a proof")


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
