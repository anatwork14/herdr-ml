# Experiment Infrastructure — task ledger

Base: PR #2 head `d3742661073ec126c84bd30c1e6ecc6bbcb94ab4`. Development branch `feat/experiment-infrastructure-p1`. Integration branch `feat/exp-integration` at `69ac8ca`.

Roles per the mission: Lead (architecture, integration, gate assessment); workers W1 to W4 (implementation, separate worktrees); independent reviewer (Opus 5.5, or a fallback identified as such).

Task status column: DONE means the task's work was delivered. It is not a gate result. Component statuses use only OFFLINE_TESTED, ISOLATED_CPU_VERIFIED, NOT_TESTED and BLOCKED; see `GATE_MATRIX.md`. No Experiment Infrastructure item is PASS.

| ID | Task | Owner | Depends on | Status | Component status | Evidence |
|---|---|---|---|---|---|---|
| P0.1 | Verify repository state and reviewed head | Lead | — | DONE | — | `BASELINE.md` |
| P0.2 | Create development worktree and branch | Lead | P0.1 | DONE | — | branch `feat/experiment-infrastructure-p1` at `d374266` |
| P0.3 | Baseline test run, interpreters, preflight | Lead | P0.2 | DONE (3.12 only) | — | `BASELINE.md`, `baseline-raw/` |
| P0.4 | Gate matrix, machine-readable gate file | Lead | P0.3 | DONE | — | `GATE_MATRIX.md`, `gate_matrix.json` |
| P0.5 | Stacked draft PR against PR #2 | Lead | P0.4 | DONE | — | PR #3 (draft, base `fix/p0-reliability-remediation`) |
| P1.1 | Run state machine specification | W1 | P0.5 | DONE | OFFLINE_TESTED | `RUN_OWNERSHIP.md` §3, §4; `feat/exp-w1-ownership` `ef673ac` |
| P1.2 | Unique ownership, fencing token, execution identity | W1 | P1.1 | DONE | OFFLINE_TESTED | `RUN_OWNERSHIP.md` §2 |
| P1.3 | Cross-database crash protocol (conservative, fail-closed) | W1 | P1.2 | DONE | OFFLINE_TESTED | `RUN_OWNERSHIP.md` §5 |
| P1.4 | Fault-injection tests (crash boundaries, races, corruption, PID reuse) | W1 | P1.3 | DONE | OFFLINE_TESTED | `RUN_OWNERSHIP.md` §6, §7; `w1-logs/` |
| P2.1 | Containment safety contract | W2 | P0.5 | DONE | OFFLINE_TESTED; containment itself BLOCKED | `CONTAINMENT.md`; `feat/exp-w2-containment` `ac9e2a5` |
| P2.2 | Host capability investigation (read-only) | Lead | P0.3 | DONE for this host | BLOCKED (no delegated cgroup) | `BASELINE.md`, `baseline-raw/containment-probe.json` |
| P2.3 | Simulator backend (not selectable in production) and tests | W2 | P2.1 | DONE | OFFLINE_TESTED (test-only; not containment evidence) | `CONTAINMENT.md`; `w2-logs/` |
| P2.4 | Operator requirements for host provisioning | Lead | P2.2 | DONE | — | `containment/OPERATOR_REQUIREMENTS.md` |
| P3.1 | Authoritative queue identity binding | W3 | P1.2 | DONE | ISOLATED_CPU_VERIFIED | `QUEUE.md` §2, §4; `feat/exp-w3-queue` `8c306d9` (merged `aaef2e2`) |
| P3.2 | Queue adapter (durable intent, idempotent claim, no replay after uncertainty) | W3 | P3.1 | DONE | OFFLINE_TESTED; private-daemon behaviour ISOLATED_CPU_VERIFIED; production dispatch BLOCKED | `QUEUE.md` §2, §6 |
| P3.3 | Isolated Pueue 4.0.4 tests (private daemon only) | W3 | P3.2 | DONE | ISOLATED_CPU_VERIFIED | `QUEUE.md` §3; `w3-logs/` |
| P4.1 | Canonical execution manifest | W4 | P1.2 | DONE | OFFLINE_TESTED | `PROVENANCE.md`; `feat/exp-w4-provenance` `f3414a2` |
| P4.2 | Worker-side binding verification | W4 | P4.1 | DONE | OFFLINE_TESTED | `PROVENANCE.md` |
| P4.3 | Independent approval protocol | W4 | P4.1 | DONE (verifier) | OFFLINE_TESTED with TEST-ONLY anchors; approval BLOCKED (no trust anchor) | `APPROVAL.md` |
| P4.4 | Negative tests (mutation, replay, forgery) | W4 | P4.2, P4.3 | DONE | OFFLINE_TESTED | `PROVENANCE.md` Tests, `APPROVAL.md` Tests; `w4-logs/` |
| P5 | Recovery model and deterministic fault injection | Lead | P1, P2, P3 | DONE | Recovery logic OFFLINE_TESTED; CPU termination tests ISOLATED_CPU_VERIFIED; EXPERIMENT_RECOVERY_READY BLOCKED | `RECOVERY.md`; `feat/exp-phase5-recovery` chain to `8552182`, carried by `a43502a` (merged `e3f7ae9`) |
| P6 | End-to-end offline CPU validation; live isolated run only if containment is verified | Lead | P1 to P5 | DONE (offline only; no live run, containment not verified) | OFFLINE_TESTED; crash boundaries and process accounting ISOLATED_CPU_VERIFIED; production fail-closed for a real worker path NOT_TESTED (`E2E_OFFLINE.md` §2, §7 item 1b) | `E2E_OFFLINE.md`; `feat/exp-phase6-e2e` `a43502a` (merged `69ac8ca`) |
| P7 | Review and fix loop | Lead + reviewer | each milestone | DONE | — | see below |
| INT | Integration of W1, W2, W4, W3, P5, P6 | Lead | P1 to P6 | DONE | 455 tests OK, exit 0, at `69ac8ca` | `INTEGRATION.md` |

## P7 review and fix loop, per milestone

| Milestone | Accepted SHA | Reviewer result | Fixes in the loop |
|---|---|---|---|
| W1 ownership (P1) | `ef673ac` | Closed | H1, M1 to M3, L1 to L3, N1 to N5, R1, R2 (`f4f713d` to `ef673ac`) |
| W2 containment (P2.1, P2.3) | `ac9e2a5` | Closed; no CRITICAL or HIGH on the reviewed revision | MEDIUM-1, MEDIUM-2, MEDIUM-5, LOW-4, LOW-6, LOW-7, R1 to R3 (`0cc3665` to `ac9e2a5`) |
| W4 provenance and approval (P4) | `f3414a2` | Closed | git isolation, worker binding, N1 (submodules), N2 (revocation state anchor) (`c75f212` to `f3414a2`) |
| W3 queue (P3) | `8c306d9` | Closed; one LOW residual accepted (legacy `ENQUEUE_REFUSED` rows) | A to D: private binary copy, reconcile errors, ambiguous enqueue timeout, non-zero exit ambiguous (`e90e3b1`, `6215ef0`, `8c306d9`) |
| P5 recovery | `8552182` (carried by `a43502a`) | Closed | quiescence before interrupted-launch fence, descendant lineage, pidfd re-check (`ef35357`, `8552182`) |
| P6 E2E offline | `a43502a` | Closed | explicit test anchor root, job recorded before release, containment evidence wording (`a43502a`) |

LOW residuals carried forward are listed under Known residuals.

## Tests

- Integration branch `69ac8ca`: `/mnt/ssd/genos2/.local/bin/python3.12 -m unittest discover -s tests` ran 455 tests, OK, exit 0 (Python 3.12.14).
- Python 3.10: NOT_TESTED (not installed).
- Python 3.11: NOT_TESTED (not installed; `~/.local/bin/python3.11` is a dangling symlink).

## Known residuals

1. In-process forgery paths (`object.__new__`, reading `_ISSUE_KEY`, mutating `_VERIFIED_ISSUERS`, the same-process HMAC key, module globals and private verifier fields) are out of the threat model.
2. Queue identity is a hash of the private daemon's certificate. It is not peer authentication of the socket.
3. A non-zero `pueue enqueue` exit is treated as ambiguous: `ENQUEUE_UNKNOWN`, row fenced AMBIGUOUS (`ambiguous_from=BOUND`), no retry, no resolution path.
4. Legacy `ENQUEUE_REFUSED` rows written by intermediate commit `e90e3b1` are not fenced by current code and could be released again. `queue_recover` must fence them AMBIGUOUS before any release that includes this code (reviewer LOW).
5. No `cgroup.kill` and no `cgroup.procs` enumeration; termination is by recorded identity only. Cgroup escape is not verified (BLOCKED on this host).
6. No external trust anchor is provisioned. APPROVAL_READY stays BLOCKED.
7. No production Pueue dispatch: `PRODUCTION_DISPATCH_ENABLED = False`, and the researchctl queued path is refused.
8. The production worker containment gate does not exist yet in `scripts/`. Fail-closed behaviour of a real worker path is NOT_TESTED.

## Blocked external dependencies

- **Containment host provisioning** (P2): requires an administrator-delegated cgroup v2 boundary or site-supported service. Not available on this host. Retained as BLOCKED. Documented in `containment/OPERATOR_REQUIREMENTS.md`.
- **Independent approval trust anchor** (P4.3): no independent authority exists in this repository. APPROVAL_READY stays BLOCKED until one is provisioned.

## Worker isolation

Workers must use separate worktrees and branches. Shared files (the registry schema, `researchctl.py`, the test index) are changed only by the Lead to avoid concurrent writes.

## Current status

Phases 0 to 7 are complete as development and offline verification work. All six gates (RUN_OWNERSHIP_READY, CONTAINMENT_READY, QUEUE_READY, PROVENANCE_READY, APPROVAL_READY, EXPERIMENT_RECOVERY_READY) are BLOCKED. No live acceptance criterion is met on this host. No Experiment Infrastructure item is PASS.
