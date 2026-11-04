# Research OS release assessment — PR #2

This is an evidence-based status report, not a release authorization. Implementation SHA: `ce34ee415f2b83e7d3e7f2b76dc8a9b72b96a1a7`. Later commits may contain only reports; check PR head for its exact SHA. Official runs, queued execution and automatic Herdr bootstrap remain blocked.

## Research OS Core

| Gate | Verdict | Evidence / prerequisite |
|---|---|---|
| HERDR_WORKSPACE_READY | BLOCKED | Mock reconciler only; no authorized live session, verified adapter, server-incarnation identity or server-enforced parent guard. |
| MULTI_AGENT_READY | BLOCKED | No multiple-agent lifecycle test in a genuine isolated Herdr workspace. |
| TERMINAL_AND_LOGS_READY | BLOCKED | Local process logs were captured in a real CPU diagnostic, but Herdr pane status, lifecycle and durable terminal/log integration were not tested. |
| GIT_WORKTREE_READY | BLOCKED | Separate Git worktrees were used for implementation and review, but the Core's Herdr-managed worktree flow was not live-tested. |
| CORE_RECOVERY_READY | BLOCKED | Crash/recovery cases have mock tests; live Herdr recovery after partial workspace creation has not been verified. |

## Research OS Experiment Infrastructure

| Gate | Verdict | Evidence / prerequisite |
|---|---|---|
| RUN_OWNERSHIP_READY | BLOCKED | The standalone registry protocol bound task and worker fields during an isolated real-Pueue CPU harness. Production `researchctl` integration and safe crash recovery remain unverified; the two database transactions are not atomic together. |
| CONTAINMENT_READY | BLOCKED | Current production backend is UNAVAILABLE; no user-delegated cgroup v2 boundary or usable user systemd bus was found. A process group is not sufficient. |
| QUEUE_READY | BLOCKED | An isolated Pueue 4.0.4 CPU harness exercised a stashed task, task ID, worker index and registry fencing. It did not invoke the production `researchctl` queue path or establish trusted queue-instance identity, recovery, containment or slot release. |
| PROVENANCE_READY | BLOCKED | Signed manifests and artifact checks are point-in-time; there is no immutable execution-time binding of the actual source/data/split/environment to a worker. |
| APPROVAL_READY | BLOCKED | Run-specific authorization, independently administered trust anchors and independent reviewer approval are not connected to execution. |
| EXPERIMENT_RECOVERY_READY | BLOCKED | PID observations do not prove a descendant tree is stopped. No verified containment-backed recovery or release path exists. |

The actual local CPU smoke in `../p0/core-release/cpu-diagnostic.json` ran a harmless command in a temporary Git checkout; the leader printed its output, but the runner returned 2 and reported UNKNOWN because containment is unverified. `../p0/core-release/run_cpu_diagnostic.py` reproduces this limited result. No GPU job was run.

See `../p0/core-release/operator-handoff.md` for the minimum external prerequisites and verification commands. [Herdr's socket API documentation](https://herdr.dev/tui/docs/socket-api/) documents snapshot and inventory APIs; its published description does not by itself establish the stronger server-incarnation, immutable creation-cwd or guarded-adoption guarantees required by this reconciler. The installed API must be checked from a genuine Herdr-managed pane. A documentation absence is not proof that a capability is impossible.

## Independent review and verification

GPT-6 Luna fallback reviewers independently checked the containment contract at `250d015` and the integrated queue protocol at `f582ad9` and the escaped-argument harness at `ce34ee4`. No HIGH/CRITICAL issue was identified within those held scopes. They did not claim Opus approval. The most recent prior Opus request was rejected by the provider usage limit, recorded in `../p0/final-integration/final-delta-opus-status.json`; no identical request was repeatedly submitted. The full integrated suite passed 120 CPU-only tests on Python 3.10.21 and 3.12.14. Test logs and the isolated queue evidence are in `../p0/core-release/`.
