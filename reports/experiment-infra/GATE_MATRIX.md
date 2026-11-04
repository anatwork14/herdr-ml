# Experiment Infrastructure — gate matrix

Machine-readable status is in `gate_matrix.json`. This table is the human view. Statuses: PASS, FAIL, BLOCKED, NOT_TESTED, OFFLINE_TESTED (offline protocol tests only; does not authorize live use).

Phase 0 result: no gate is PASS.

| Gate | Status at Phase 0 | Blocker | Evidence |
|---|---|---|---|
| RUN_OWNERSHIP_READY | BLOCKED | Registry and isolated task binding not integrated; cross-database crash handling incomplete | `reports/reviews/2026-11-03-manual-core-continuation.md` |
| CONTAINMENT_READY | BLOCKED | Host has no delegated cgroup v2 boundary, no `cgroup.kill`, no user systemd bus; backend returns UNAVAILABLE by design | `baseline-raw/containment-probe.json` |
| QUEUE_READY | BLOCKED | Production queue path held; queue identity and recovery unverified | `reports/reviews/2026-11-03-manual-core-continuation.md` |
| PROVENANCE_READY | BLOCKED | Point-in-time artifacts not bound to worker execution | same |
| APPROVAL_READY | BLOCKED | Run-specific authorization and independent trust anchor not connected | same |
| EXPERIMENT_RECOVERY_READY | BLOCKED | Descendant termination and safe resource release not proven | same |

Manual Core gates (not in scope here, already decided): HERDR_WORKSPACE_READY, MULTI_AGENT_READY, TERMINAL_AND_LOGS_READY, GIT_WORKTREE_READY, CORE_RECOVERY_READY = PASS; Manual Core limited release = APPROVED (2026-11-04).
