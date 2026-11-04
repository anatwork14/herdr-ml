# Experiment Infrastructure — gate matrix

Machine-readable status is in `gate_matrix.json`. This table is the human view.

The only gate status in use is BLOCKED. Component statuses use only this vocabulary:

- OFFLINE_TESTED: offline protocol or unit tests only. Does not authorize live use.
- ISOLATED_CPU_VERIFIED: exercised against real processes or a private Pueue daemon, CPU only, on this host. Not an operational status.
- NOT_TESTED: no test exercises it.
- BLOCKED: cannot be done or verified on this host, or depends on something that does not exist yet.

No Experiment Infrastructure item is PASS. Simulated results (W2 simulator, TEST-ONLY trust anchors, the E2E harness worker) are not operational status.

## Final status (integration branch `feat/exp-integration` at `69ac8ca`)

**All six gates are BLOCKED. No live acceptance criterion is met on this host.**

| Gate | Status | Blocker | Evidence |
|---|---|---|---|
| RUN_OWNERSHIP_READY | BLOCKED | No live acceptance criterion met on this host. Fencing, execution identity and the cross-database crash protocol are OFFLINE_TESTED only. `_VERIFIED_ISSUERS` is empty, so no production path can reach `COMPLETED`, `FAILED`, `INTERRUPTED` or `FENCED`. `CANCEL_REQUESTED` is not durable. researchctl does not call the queue functions. In-process forgery is out of the threat model. | `RUN_OWNERSHIP.md` |
| CONTAINMENT_READY | BLOCKED | No live acceptance criterion met on this host. No delegated cgroup v2 boundary, no `cgroup.kill`, no user systemd bus; the production backend is `UNAVAILABLE` and the production trust-anchor set is empty. Cgroup escape is not verified. The simulator is test-only and is not containment evidence. No production worker containment gate exists in `scripts/` yet. | `CONTAINMENT.md`, `baseline-raw/containment-probe.json`, `containment/OPERATOR_REQUIREMENTS.md` |
| QUEUE_READY | BLOCKED | No live acceptance criterion met on this host. Identity binding and private-daemon behaviour are ISOLATED_CPU_VERIFIED only (pinned Pueue 4.0.4, private daemon). `PRODUCTION_DISPATCH_ENABLED = False`; the researchctl queued path is still refused. Queue identity is a certificate hash, not peer authentication. AMBIGUOUS has no resolution path (no lookup verifier). Legacy `ENQUEUE_REFUSED` rows from `e90e3b1` must be fenced before release. | `QUEUE.md`, `INTEGRATION.md` |
| PROVENANCE_READY | BLOCKED | No live acceptance criterion met on this host. The manifest and worker-side verifier are OFFLINE_TESTED only and are not connected to `researchctl.py`. Checks are point-in-time; undeclared reads and post-check changes are not covered. | `PROVENANCE.md` |
| APPROVAL_READY | BLOCKED | No live acceptance criterion met on this host. No external trust anchor is provisioned; the production load fails closed with `TRUST_ANCHOR_UNPROVISIONED`. The verifier is OFFLINE_TESTED with TEST-ONLY anchors; production ownership checks are tested only with patched metadata. Not called by `researchctl.py`. | `APPROVAL.md`, `E2E_OFFLINE.md` §3 |
| EXPERIMENT_RECOVERY_READY | BLOCKED | No live acceptance criterion met on this host. No live containment, so release is always refused (`CONTAINMENT_UNAVAILABLE`). Termination is by recorded identity only (no cgroup, no `cgroup.kill`; unrecorded descendants are not found). Recovery logic is OFFLINE_TESTED; CPU termination is ISOLATED_CPU_VERIFIED. | `RECOVERY.md`, `E2E_OFFLINE.md` |

## Component status per phase

| Phase | Component | Source (accepted SHA) | Status |
|---|---|---|---|
| P1 | Run ownership, fencing, cross-database crash protocol (W1) | `feat/exp-w1-ownership` `ef673ac` | OFFLINE_TESTED |
| P1 | Live queue integration, live containment, cross-host | — | BLOCKED |
| P2.1 | Containment safety contract, proof model, release gate (W2) | `feat/exp-w2-containment` `ac9e2a5` | OFFLINE_TESTED |
| P2.3 | Simulator backend, test-only, not selectable in production (W2) | `ac9e2a5` | OFFLINE_TESTED |
| P2 | Containment on this host | — | BLOCKED |
| P3 | Queue adapter offline matrix: idempotent-claim refusals, binary pinning, owner-only root (W3) | `feat/exp-w3-queue` `8c306d9` (includes repairs `e90e3b1`, `6215ef0`; merged as `aaef2e2`) | OFFLINE_TESTED |
| P3 | Queue identity binding and private-daemon behaviour (durable intent, idempotent claim in a real task, AMBIGUOUS fencing, restart reconcile without resubmission) | `8c306d9` | ISOLATED_CPU_VERIFIED |
| P3 | Production dispatch, researchctl queued execution, AMBIGUOUS resolution | — | BLOCKED |
| P4 | Execution manifest and worker-side provenance verifier (W4) | `feat/exp-w4-provenance` `f3414a2` | OFFLINE_TESTED |
| P4 | Approval verifier with TEST-ONLY anchors | `f3414a2` | OFFLINE_TESTED |
| P4 | Approval with a production trust anchor | — | BLOCKED (no trust anchor) |
| P5 | Recovery: orphaned claims, interrupted launches, stale fence, release refusal | `feat/exp-phase5-recovery` chain `a362089` to `8552182`, carried by `a43502a` (merged as `e3f7ae9`) | OFFLINE_TESTED |
| P5 | CPU termination of recorded descendants; crash injection at 6 boundaries x 3 | same | ISOLATED_CPU_VERIFIED |
| P5 | Cgroup-level containment, escape detection, kernel-enumerated occupancy | — | BLOCKED |
| P6 | E2E offline chain, production predicate refusal under the harness worker, approval, artifact seal | `feat/exp-phase6-e2e` `a43502a` (merged as `69ac8ca`) | OFFLINE_TESTED |
| P6 | Crash at 9 boundaries x 3; process accounting | `a43502a` | ISOLATED_CPU_VERIFIED |
| P6 | Production fail-closed for a real worker path (`E2E_OFFLINE.md` §2, §7 item 1b): no production worker containment gate exists in `scripts/` | — | NOT_TESTED |
| P6 | Live containment, production approval anchor, production queue dispatch | — | BLOCKED |

## Tests

| Item | Result |
|---|---|
| Integration branch `69ac8ca`, `python3.12 -m unittest discover -s tests` (3.12.14) | Ran 455, OK, exit 0 |
| Python 3.10 | NOT_TESTED (not installed) |
| Python 3.11 | NOT_TESTED (not installed; `~/.local/bin/python3.11` is a dangling symlink) |

## Phase 0 baseline (superseded)

Phase 0 recorded all six gates BLOCKED at `d374266` with 120 tests OK. See `BASELINE.md`.

Manual Core gates (not in scope here, already decided): HERDR_WORKSPACE_READY, MULTI_AGENT_READY, TERMINAL_AND_LOGS_READY, GIT_WORKTREE_READY, CORE_RECOVERY_READY = PASS; Manual Core limited release = APPROVED (2026-11-04).
