# Experiment Infrastructure — task ledger

Base: PR #2 head `d3742661073ec126c84bd30c1e6ecc6bbcb94ab4`. Development branch `feat/experiment-infrastructure-p1`.

Roles per the mission: Lead (architecture, integration, gate assessment); workers W1 to W4 (implementation, separate worktrees); independent reviewer (Opus 5.5, or a fallback identified as such).

| ID | Task | Owner | Depends on | Status | Evidence |
|---|---|---|---|---|---|
| P0.1 | Verify repository state and reviewed head | Lead | — | DONE | `BASELINE.md` |
| P0.2 | Create development worktree and branch | Lead | P0.1 | DONE | branch `feat/experiment-infrastructure-p1` at `d374266` |
| P0.3 | Baseline test run, interpreters, preflight | Lead | P0.2 | DONE (3.12 only) | `BASELINE.md`, `baseline-raw/` |
| P0.4 | Gate matrix, machine-readable gate file | Lead | P0.3 | DONE | `GATE_MATRIX.md`, `gate_matrix.json` |
| P0.5 | Stacked draft PR against PR #2 | Lead | P0.4 | DONE | PR #3 (draft, base `fix/p0-reliability-remediation`) |
| P1.1 | Run state machine specification | W1 | P0.5 | NOT_STARTED | — |
| P1.2 | Unique ownership, fencing token, execution identity | W1 | P1.1 | NOT_STARTED | — |
| P1.3 | Cross-database crash protocol (conservative, fail-closed) | W1 | P1.2 | NOT_STARTED | — |
| P1.4 | Fault-injection tests (crash boundaries, races, corruption, PID reuse) | W1 | P1.3 | NOT_STARTED | — |
| P2.1 | Containment safety contract | W2 | P0.5 | NOT_STARTED | — |
| P2.2 | Host capability investigation (read-only) | Lead | P0.3 | DONE for this host | `BASELINE.md`, `baseline-raw/containment-probe.json` |
| P2.3 | Simulator backend (not selectable in production) and tests | W2 | P2.1 | NOT_STARTED | — |
| P2.4 | Operator requirements for host provisioning | Lead | P2.2 | DONE | `containment/OPERATOR_REQUIREMENTS.md` |
| P3.1 | Authoritative queue identity binding | W3 | P1.2 | NOT_STARTED | — |
| P3.2 | Queue adapter (durable intent, idempotent claim, no replay after uncertainty) | W3 | P3.1 | NOT_STARTED | — |
| P3.3 | Isolated Pueue 4.0.4 tests (private daemon only) | W3 | P3.2 | NOT_STARTED | — |
| P4.1 | Canonical execution manifest | W4 | P1.2 | NOT_STARTED | — |
| P4.2 | Worker-side binding verification | W4 | P4.1 | NOT_STARTED | — |
| P4.3 | Independent approval protocol | W4 | P4.1 | NOT_STARTED | — |
| P4.4 | Negative tests (mutation, replay, forgery) | W4 | P4.2, P4.3 | NOT_STARTED | — |
| P5 | Recovery model and deterministic fault injection | Lead | P1, P2, P3 | NOT_STARTED | — |
| P6 | End-to-end offline CPU validation; live isolated run only if containment is verified | Lead | P1 to P5 | NOT_STARTED | — |
| P7 | Review and fix loop | Lead + reviewer | each milestone | NOT_STARTED | — |

## Blocked external dependencies

- **Containment host provisioning** (P2): requires an administrator-delegated cgroup v2 boundary or site-supported service. Not available on this host. Retained as BLOCKED. Documented in `containment/OPERATOR_REQUIREMENTS.md`.
- **Independent approval trust anchor** (P4.3): no independent authority exists in this repository. APPROVAL_READY stays BLOCKED until one is provisioned.

## Worker isolation

Workers must use separate worktrees and branches. Shared files (the registry schema, `researchctl.py`, the test index) are changed only by the Lead to avoid concurrent writes.

## Current status

Phase 0 complete. Phases 1 to 6 are not started. No gate is PASS.
