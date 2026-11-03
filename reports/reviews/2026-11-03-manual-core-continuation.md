# Manual Core continuation — evidence and release decisions

Implementation reviewed: `52ca202a25fa0c469ee858ff2fffeadd4a237b1d` (PR #2). Observations below are from 2026-11-03 around 10:05 UTC. This report does not certify later code changes. The existing [assessment](2026-11-03-core-experiment-release.md) and original reports remain preserved.

## Live environment

The integration agent was outside a Herdr-managed pane: `test "${HERDR_ENV:-}" = 1` failed. The installed Herdr skill directs an agent in this situation to stop and not inspect or control the focused session. No Herdr CLI session inventory, version, socket schema, creation, restart, pane control or agent lifecycle was inspected in this continuation. Previous mock results do not establish live functionality. A genuine Herdr-managed pane in a disposable session is required for those observations; do not set the variable manually.

The manual Core is a separate release scope: it does not require the custom automatic bootstrap. Nevertheless, the two disposable workspaces, agent status, pane/log association, detach/reattach and restoration have not been demonstrated in an authorized session. The automatic reconciler remains independently held; its server identity, creation identity and guarded ownership requirements are also unverified. No live adapter capability is claimed.

## CPU queue and host observations

The independent queue worker ran the existing queue/guard tests (15 passed) and an isolated Pueue 4.0.4 CPU harness. The orchestrator reran the harness at the reviewed SHA using absolute executable paths. It returned exit 0 with:

```json
{"daemon_exit":0,"daemon_stopped":true,"fence_created":true,"isolated_xdg":true,"pueue_task_id":"0","pueue_worker_index":"0","shutdown_command_exit":0,"task_finished":true}
```

Reproduce with `/absolute/path/to/python3.10 scripts/pueue_cpu_worker_integration.py --pueue /absolute/path/to/pueue --pueued /absolute/path/to/pueued --python /absolute/path/to/python3.10`. It starts and stops its own isolated daemon. This demonstrates the harness only: it does not establish production queue dispatch, a trusted queue-instance identity, crash recovery or resource release.

The current worker's `containment_probe.py` returned exit 2 and `BLOCKED`: cgroup `/sys/fs/cgroup/unified/system.slice/teleport.service` and its `cgroup.procs` were not writable, `cgroup.kill` was absent and the user systemd bus was unreachable. Linux was 5.4.0-216-generic and systemd was 245. The probe intentionally does not grant readiness based on writable paths. [Operator handoff](../p0/core-release/operator-handoff.md) gives the minimum site capability required; provisioning alone would not implement or independently validate a backend.

`python3` resolved to `/usr/bin/python3` version 3.8.10; `python3.10` was not on this shell's PATH. The absolute interpreter `/mnt/ssd/genos2/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10` returned 3.10.21 and ran the probe. This is not proof of daemon or pane interpreter consistency.

## Classification at the reviewed implementation SHA

| Classification | Verdict | Scope |
|---|---|---|
| OFFLINE_TESTED | PASS | Existing suite evidence plus the 15 queue/guard tests; this is not a live readiness result. |
| LIVE_HERDR_TESTED | BLOCKED | The required authorized Herdr pane was unavailable. |
| LIVE_QUEUE_TESTED | PASS | Isolated CPU-only Pueue harness only; production queue integration is BLOCKED. |
| SECURITY_REVIEWED | BLOCKED | No complete independent security review of operational integrations at this SHA. |
| OPERATIONALLY_READY | BLOCKED | Neither release has all required live evidence and independent verification. |

## Release decisions

| Research OS Manual Core | Verdict | Evidence gap |
|---|---|---|
| HERDR_WORKSPACE_READY | BLOCKED | No authorized live workspace inventory or isolated creation test. |
| MULTI_AGENT_READY | BLOCKED | No live two-agent lifecycle test. |
| TERMINAL_AND_LOGS_READY | BLOCKED | No verified pane/project/log association in Herdr. |
| GIT_WORKTREE_READY | BLOCKED | Git worktrees were used for development, but the Herdr-managed flow was not exercised. |
| CORE_RECOVERY_READY | BLOCKED | Detach, reattach and supported agent restoration not exercised. |
| Manual Core release | BLOCKED | All included capabilities require live evidence and independent review. |

| Research OS Experiment Infrastructure | Verdict | Evidence gap |
|---|---|---|
| RUN_OWNERSHIP_READY | BLOCKED | Registry and isolated task binding are not integrated into production dispatch; cross-database crash handling is incomplete. |
| CONTAINMENT_READY | BLOCKED | No verified backend or delegated containment boundary. |
| QUEUE_READY | BLOCKED | Production queue path remains held; identity and recovery are unverified. |
| PROVENANCE_READY | BLOCKED | Point-in-time artifacts are not bound immutably to the worker's execution. |
| APPROVAL_READY | BLOCKED | Run-specific authorization and independently managed trust are not connected to execution. |
| EXPERIMENT_RECOVERY_READY | BLOCKED | Descendant termination and safe resource release are not proven. |
| Experiment Infrastructure release | BLOCKED | Holds and independent verification requirements remain. |

The prior Opus request failed due to provider quota and is preserved in `../p0/final-integration/final-delta-opus-status.json`. Existing GPT-6 Luna reviews are fallback reviews of their explicitly recorded scopes, not Opus approval or a scientific certification. No GPU work was run.
