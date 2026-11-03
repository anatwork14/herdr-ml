# Manual Core live Herdr integration — evidence

Scope: Research OS Manual Core gates only. Automatic bootstrap, queued runs, official runs and GPU execution were not enabled or run. Production workspaces (`w1`, `w3`, `w5`, `w6`) and their agents were not modified; `w5:p2` is the orchestrating session.

Orchestrator: the integration session in Herdr pane `w5:p2`, workspace `w5`, with `HERDR_ENV=1` verified before any control command. No Codex instance was used to control Herdr.

## Live capability inventory

- Client and server: Herdr `0.9.5` (stable, protocol 22), server running, endpoint compatible, no restart needed.
- Sessions: only the `default` session exists and runs; it was not stopped or altered.
- Production state before tests: `w1` (agy idle), `w3`, `w5` (this session, working), `w6`.
- Command groups present: `agent`, `pane`, `workspace`, `tab`, `worktree`, `terminal`, `notification`, `integration`, `session`, `machine`, `api`.
- Agent kinds available: including `claude`, `codex`, `agy`. Only `claude` was used as a test subject.

## Disposable resources

- Fixture git repository at `scratchpad/mlcore-fixture` (outside the project tree).
- Workspace `w7` with tabs `w7:t1` and `w7:t2`; panes `w7:p1`, `w7:p2`, `w7:p3`.
- Herdr worktree `scratchpad/mlcore-wt-a` (branch `mlcore-wt-a`), workspace `w8`.
- Two Claude agents `mlcore-a` and `mlcore-b`.
- All disposable resources were closed or removed after the tests; the worktree removal closed `w8`, and `w7` was closed explicitly.

## Gate results

### HERDR_WORKSPACE_READY — PASS (live)

- `herdr workspace create --cwd <fixture> --label mlcore-disposable-t1 --no-focus` returned `workspace_created` with `w7`, `w7:t1`, `w7:p1`.
- `herdr tab create --workspace w7 --label mlcore-tab2 --no-focus` returned `w7:t2`, `w7:p2`. `tab list` and `workspace get` reported 2 tabs and 2 panes.
- `herdr pane run w7:p1 "pwd; git rev-parse --abbrev-ref HEAD; echo MLCORE_T1_MARK"` followed by `pane wait-output --match` matched, and the read output showed the fixture cwd and branch `main`.

### MULTI_AGENT_READY — PASS (live, with one operator-approved trust prompt)

- `herdr agent start mlcore-a --kind claude --pane w7:p1` and `mlcore-b --kind claude --pane w7:p3` both returned `agent_not_ready` because Claude showed its workspace-trust dialog for the new fixture folder. The dialog was confirmed with Enter on both panes (the default option was "Yes, I trust this folder"; the folder was created for this test). Both then reached `idle`.
- `herdr agent prompt … --wait` run in parallel: both agents went through the working state and settled to `done`. Each reply contained its own token (`MLCORE_A_OK` for `mlcore-a`, `MLCORE_B_OK` for `mlcore-b`) in scrollback, and each had a distinct Claude session ID (`a88c1dbc…`, `0fdd24df…`).
- Status is tracked per name and per pane; `agent get` reported `interactive_ready: true` and `idle` after the turn.

Caveat: the trust dialog blocks `agent start` until confirmed. This is expected Claude behaviour, and the operator must approve a new folder before a managed agent is usable in it.

### TERMINAL_AND_LOGS_READY — PASS (live)

- In `w7:p2` the command `… | tee logs/t3.log` wrote three lines. The file on disk contained exactly `MLCORE_LOG_LINE_1..3`, and the pane read showed the same lines.
- `pane get w7:p2` reported `workspace_id: w7`, `tab_id: w7:t2`, and `cwd` / `foreground_cwd` equal to the fixture project.
- `pane process-info` reported the foreground `bash` process and its cwd.

Caveat: project association was verified through pane cwd and workspace metadata. Herdr does not expose an explicit project binding beyond that.

### GIT_WORKTREE_READY — PASS (live)

- `herdr worktree create --cwd <fixture> --branch mlcore-wt-a --path <scratch>/mlcore-wt-a --no-focus` returned `worktree_created`, workspace `w8`, `is_linked_worktree: true`, branch `mlcore-wt-a`.
- `git worktree list` showed the main checkout on `main` and the linked checkout on `mlcore-wt-a`. A commit made in the linked worktree (`db34709`) did not appear in the main checkout (`3881ce0`, `WT_ONLY.txt` absent), and the main branch was unchanged.
- `herdr worktree remove --workspace w8` returned `worktree_removed` and closed `w8`.

### CORE_RECOVERY_READY — PASS for the pane-observer and Claude-resume paths (live)

- Detach/reattach of pane output: a background process in `w7:p2` appended to `logs/t5.log`. `herdr terminal session observe <term>` was run with a 4 s timeout, ended by that timeout (exit 124), twice. The log grew throughout (4 → 10 → 13 lines during the two detach intervals), so the pane process survived detachment. The reattached observer received live stream bytes (7159 bytes per observation).
- Agent restoration: `mlcore-a` exited cleanly with `/exit`. Its pane returned to a shell and Claude printed its resume command. `herdr agent start mlcore-a --kind claude --pane w7:p1 -- --resume a88c1dbc-24d3-4df2-8cdc-2960ea32bcd9` returned `idle` with the same session ID. A follow-up prompt asked for the earlier token; the scrollback then contained `MLCORE_A_OK` three times (original reply, echo, and recall), which shows the resumed session retained its context.

Not tested (gaps that remain):
- A full TUI detach and reattach (`herdr` attach/detach with `ctrl+b q`), which needs an interactive terminal.
- A Herdr server restart with agents surviving it. The default session server was deliberately not stopped.
- Restoration without an explicit `--resume` argument.

## Independent review requested

Findings are assessed on this report and the evidence files in the orchestrator's scratchpad. No defect was demonstrated in the tests, so no code change was made for this report.

## Release decision

| Gate | Status | Evidence |
|---|---|---|
| HERDR_WORKSPACE_READY | PASS (live) | workspace/tab create, inspect, pane run output |
| MULTI_AGENT_READY | PASS (live) | two Claude agents, start, prompt/wait, status, distinct sessions |
| TERMINAL_AND_LOGS_READY | PASS (live) | pane output, log file on disk, pane cwd/workspace association |
| GIT_WORKTREE_READY | PASS (live) | Herdr worktree create/remove and isolation |
| CORE_RECOVERY_READY | PASS (partial) | pane detach/reattach and Claude `--resume` restoration; full TUI detach and server restart not exercised |
| Manual Core release | PENDING_INDEPENDENT_REVIEW | all five gates have live evidence; the release is not declared ready until independent review of this report and the scope gaps above is complete |

Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) are unchanged and remain BLOCKED per `2026-11-03-manual-core-continuation.md`. This report does not change them.
