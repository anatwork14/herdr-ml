# Manual Core live Herdr integration — evidence

Scope: Research OS Manual Core gates only. Automatic bootstrap, queued runs, official runs and GPU execution were not enabled or run.

Orchestrator: the integration session in Herdr pane `w5:p2`, workspace `w5`. The `HERDR_ENV=1` check passed before any control command (run in the session; not saved as a file). No Codex instance was used to control Herdr.

Versions: Herdr `0.9.5` (stable, protocol 22), saved in `reports/herdr-live/recovery/post/herdr-version.txt` and `post/herdr-status.txt`.

Commits and implementation: the PR #2 head when round 1 and the first recovery run were made was `b51a6b328d136dee31f67b56f7369db9b0a14bf2`. Cycle 2 started at `fbe35d41fb607e2d78f95fa928e0e7f27e43b68a`. Evidence and report commits: `6d6d16c` (round-2 raw artifacts), `ec3a4d3`, `21286ba`, `fbe35d4` (round-3 corrections and round-1 artifacts), and the cycle-2 commit that adds this revision.

Evidence indexes: `reports/herdr-live/recovery/README.md` (round 2) and `reports/herdr-live/recovery/cycle2/README.md` (cycle 2). Round-1 artifacts: `reports/herdr-live/round1/`.

## Production isolation

Round 1 and round 2 (`reports/herdr-live/recovery/`):
- Every state-changing command targeted only the disposable workspace (`w7`/`w8` in round 1, `w9` in round 2). Read-only inventory commands also ran against `w1`, `w3`, `w5` and `w6`.
- The post-test inventory matches the baseline on stable identifiers and counts. The files are not byte-identical: `focused` flags moved between `w1` and `w3`, and `w6:p1` `scroll.max_offset_from_bottom` changed from 0 to 13. No test command targeted those panes, so the cause is unverified.

Cycle 2 (`reports/herdr-live/recovery/cycle2/`, comparison in `post/comparison.txt`):
- Stable identifiers and operational fields are unchanged for workspaces, tabs, agents (name, pane, kind, session), sessions, and panes in `w1`, `w3`, `w5`, `w6`.
- Transient differences, not attributed: the default-session agy agent `w1:p1` went from `idle` to `done` with `completion_seq` 177 → 180 (the workspace and tab `agent_status` changed with it). `w1:p1` `scroll.max_offset_from_bottom` went 104 → 185, and `w6:p1` went 41 → 55. No focus difference appeared this cycle.
- Every cycle-2 command that named `w1:p1` or `w1` carried `--session mlrec-c2`, which is a separate server with its own `w1`. No default-session command wrote input, as far as the command log shows. The cause of the agy completion and the scroll changes is unverified and may be the user's own work.
- Isolation is therefore not claimed as byte-identical. Stable identifiers and operational state are unchanged.

## Live capability inventory

- Herdr `0.9.5`, server running and endpoint compatible.
- Claude integration installed is outdated (v9 < v11), per `herdr integration status`. It was not upgraded, because the hooks are global and also affect production agents.
- Only `claude` was used as a test subject.

## Gate results

### HERDR_WORKSPACE_READY — PASS (live)

- Round 1 (`w7`): `workspace create` returned `w7`, `w7:t1`, `w7:p1` (`reports/herdr-live/round1/t1-create.json`). `tab create` returned `w7:t2`, `w7:p2` (`round1/t1-tab.json`). `pane run` with `wait-output` matched, and the read showed the fixture cwd and branch `main`. The pane read was printed but not saved.
- Round 2 (`w9`): `workspace create` returned `w9`, `w9:t1`, `w9:p1`, exit 0 (`reports/herdr-live/recovery/run/01-workspace-create.json`).
- Cycle 2 (named session `mlrec-c2`): `workspace create` returned `w1`, pane `w1:p1`, exit 0 (`cycle2/named/01-workspace-create.json`).

### MULTI_AGENT_READY — PASS (live, with operator-approved trust prompts)

- Round 1: two Claude agents `mlcore-a` and `mlcore-b` started in `w7`. Both returned `agent_not_ready` at Claude's workspace-trust dialog. Enter confirmed the default "Yes", and both reached `idle`. Their prompts ran with `--wait` and returned `done` with distinct session IDs (`round1/t2-a-prompt.json`, `round1/t2-b-prompt.json`). The scrollback token reads were printed but not saved.
- Round 2 and cycle 2 each ran one agent, covered in the recovery evidence.

Caveat: the trust dialog blocks `agent start` until confirmed. In round 2 and cycle 2 the default cursor was on "No, exit", so Enter alone would have exited. Each agent was confirmed only after `down` moved the cursor to "Yes" and the screen showed it.

### TERMINAL_AND_LOGS_READY — PASS (live)

- Round 1: `… | tee logs/t3.log` in `w7:p2` wrote three lines. The file (`reports/herdr-live/round1/logs/t3.log`) contains exactly `MLCORE_LOG_LINE_1..3`. The pane read was printed but not saved.
- `pane get` reported the workspace, tab, and a cwd and `foreground_cwd` equal to the fixture project. `pane process-info` reported the foreground `bash` and its cwd. These outputs were printed but not saved.

Caveat: project association was verified only through pane cwd and workspace metadata. Herdr exposes no explicit project binding beyond that.

### GIT_WORKTREE_READY — PASS (live)

- Round 1: `herdr worktree create --cwd <fixture> --branch mlcore-wt-a --path <scratch>/mlcore-wt-a --no-focus` returned `worktree_created` in `w8`, with `is_linked_worktree: true` and branch `mlcore-wt-a` (`reports/herdr-live/round1/t4-wt.json`).
- `git worktree list` showed the main checkout on `main` at `3881ce0`, and the linked checkout on `mlcore-wt-a`. A commit in the linked worktree (`db34709`, adding `WT_ONLY.txt`) was absent from the main checkout. The main branch stayed at `3881ce0`.
- `herdr worktree remove --workspace w8` returned `worktree_removed`. The fixture branch was deleted afterward.

### CORE_RECOVERY_READY — PENDING_INDEPENDENT_REVIEW (candidate for PASS)

The defined behaviors are: (a) detach and reattach of agent output, (b) supported restoration of an agent to its prior session, and (c) process survival across detach. Cycle 2 tested all three on the Claude agent's own terminal, and is the first evidence for (a) and (c) on the agent pane. Not marked PASS until independent review.

**(a) Agent-terminal observer and attach (cycle 2, `cycle2/named/`).** The agent's terminal was `term_65f5d670305601` in the named session `mlrec-c2`, pane `w1:p1`.
- An observer ran 40 s on the agent terminal (exit 124). Turn 2 was sent during the window. The capture has 10 frames: seq 1 is a `full=true` snapshot containing the token from turn 1, and seq 2–10 are `full=false` deltas. Delta seq 9 contains a standalone `67`, the answer to turn 2 (`cycle2/observer-A-frames.txt`).
- The documented detach `ctrl+b q` was sent through a pty with `pty_attach_once.py` (exit 0). The recorded `detach_sent` to `end` gap was 1 ms, which is an upper bound given the 0.2 s poll (`named/08-pty-attach-1.json`).
- While detached, turn 3 was sent and settled to `idle` with the same session ID. The transcript contains `71` (`named/09-turn3-prompt.json`; the answer is at `named/17-recall-scrollback.txt`).
- Reattach (`named/10-pty-attach-2.json`, exit 0, 9501 bytes before detach), then recall of the token after a unique marker: `token_after_marker=True` (`named/11-recall-check.txt`).

Limit: the reattach is a pty attach with a snapshot read. It is not a long-running observer on the reattached view.

**(b) Herdr-native restoration (cycle 2).** The `herdr` 0.9.5 schema has a `resume_argv` field, described as the command to resume "after a Herdr restart", but it is integration-supplied, and the pane reported `resume_argv: null` and `restore_error: null` before and after the restart. Herdr persists the agent identity itself, in `sessions/mlrec-c2/session.json`: `agent_name`, `managed_agent_kind: claude`, and `agent_session.value` (`cycle2/README.md`, Test B).
- `herdr --session mlrec-c2 server stop` (exit 0) ended the session's Claude process. No Claude process with the fixture cwd remained.
- `herdr --session mlrec-c2 server` was started with no agent arguments. The agent came back as `c2-agent`, `w1:p1`, `claude`, `idle`, with the same session ID.
- A live process `claude --resume 7ddaa64d-8a22-40dc-872c-1d5fc5dc60a8` exists, and its ancestry is pane bash → `herdr --session mlrec-c2 server` → init (`named/16-process-ancestry.txt`). The `--resume` argument was supplied by Herdr, not by the operator.
- A recall prompt after the restart returned the token after the marker (`named/17-recall-check.txt`: `token_after_marker=True`).

Limits of (b): only a clean `server stop` was tested, not a crash. The restore relied on the persisted session state with an outdated Claude integration. Restore with an up-to-date integration, or with `resume_argv` reported, was not tested. The production default session was not restarted.

Round 2 (`reports/herdr-live/recovery/`, not the agent pane) also showed a manual `--resume` restart after a clean exit. That is superseded by cycle 2 for the restore question.

**(c) Process survival.** Cycle 2: the agent kept working through a pty detach, and turn 3 completed while the client was detached (`named/09-*`, `named/10-*`). Round 2: a ticker in a separate terminal wrote 124 contiguous timestamped lines, with about five to six ticks in each observer and attach window (`reports/herdr-live/recovery/run/survival.txt`). The ticker evidence is about the ticker process, not the agent. The cycle-2 evidence is about the agent's conversation state across the detach.

**Not tested:**
- A server crash (`kill -9`, power loss), as opposed to the clean `server stop` used here.
- Herdr-native restore with an up-to-date Claude integration, or with `resume_argv` reported.
- The main Herdr sidebar TUI attach/detach. Only `herdr terminal attach` on the agent terminal and on the ticker terminal was used.
- Restoration of the production default session. It was not stopped and must not be.

**Verdict.** The three defined behaviors have evidence on the agent's own pane. CORE_RECOVERY_READY is therefore a candidate for PASS, and its status is `PENDING_INDEPENDENT_REVIEW`. It is not marked PASS until a fresh review confirms the claims and their limits.

### Release decision

| Gate | Status | Evidence |
|---|---|---|
| HERDR_WORKSPACE_READY | PASS (live) | round 1 `round1/t1-*.json`; round 2 `recovery/run/01-*`; cycle 2 `cycle2/named/01-*` |
| MULTI_AGENT_READY | PASS (live) | round 1 `round1/t2-*`; round 2 and cycle 2 single-agent evidence in the recovery directories |
| TERMINAL_AND_LOGS_READY | PASS (live) | `round1/logs/t3.log`; pane cwd and workspace metadata |
| GIT_WORKTREE_READY | PASS (live) | `round1/t4-wt.json` and the git output |
| CORE_RECOVERY_READY | PENDING_INDEPENDENT_REVIEW (candidate PASS) | cycle 2 agent-pane detach/reattach, native restore after clean server stop, and continuity; limits above |
| Manual Core release | PENDING_INDEPENDENT_REVIEW | four gates PASS; CORE_RECOVERY awaiting review |

Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) are unchanged and remain BLOCKED per `2026-11-03-manual-core-continuation.md`. This report does not change them.

## Prior independent reviews

**Round 1** (verdict APPROVE_WITH_CHANGES) found the observer and resume claims unsupported by saved files and the cleanup incomplete. Addressed at `b51a6b3`.

**Round 2** (`ec3a4d3`, APPROVE_WITH_CHANGES, no CRITICAL) found: the agent pane untested (confirmed; cycle 2 now covers it), restoration was manual (confirmed; cycle 2 adds native restore), the false "diff is empty" claim (confirmed; corrected), the token source (confirmed; corrected), delta wording (confirmed; corrected), the implementation SHA (corrected), unsupported version and command-group claims (corrected), and the "within about 1 ms" wording (corrected).

**Round 3** (`21286ba`, APPROVE_WITH_CHANGES, no CRITICAL): the round-1 gates cited artifacts that were not in the repository (confirmed; now in `reports/herdr-live/round1/`), the "every recovery-run command" claim (confirmed; corrected to state-changing commands), the evidence-commit list (corrected), the unsaved `HERDR_ENV` check (now stated as in-session only), the direct-attach wording (corrected), and the production-server rule (attributed to the user's instruction). The round-3 corrections were not independently re-reviewed before this cycle.

**Cycle 2 review**: pending. It will cover the agent-pane and native-restore evidence, the round-3 corrections, and all five gate verdicts, at the commit that contains this revision.
