# Manual Core live Herdr integration — evidence

Scope: Research OS Manual Core gates only. Automatic bootstrap, queued runs, official runs and GPU execution were not enabled or run.

Orchestrator: the integration session in Herdr pane `w5:p2`, workspace `w5`, with `HERDR_ENV=1` verified before any control command. No Codex instance was used to control Herdr.

## Production isolation

- Before the tests, `herdr workspace list` showed `w1` (speculative-decoding starlight), `w3` (fbid), `w5` (this session), `w6` (monitoring). The `w1:p1` agy agent and the `w5:p2` session were present.
- After cleanup, `herdr workspace list` shows the same four workspaces with the same labels, saved in the orchestrator's scratchpad as `prod-after-workspaces.json`. `herdr session list` shows only `default` running (`prod-after-sessions.json`).
- All test commands targeted only `w7`, `w8` and their panes. No command in the test sequence named `w1`, `w3`, `w6` or `w5`, and no production pane was sent input. This is a check of the command log, not a byte-level before/after comparison of each workspace's state.

## Live capability inventory

- Herdr `0.9.5` (stable, protocol 22), server running, endpoint compatible, no restart needed.
- Command groups: `agent`, `pane`, `workspace`, `tab`, `worktree`, `terminal`, `notification`, `integration`, `session`, `machine`, `api`.
- Agent kinds include `claude`, `codex`, `agy`. Only `claude` was used as a test subject.

## Disposable resources

- Fixture git repository in the scratchpad (outside the project tree).
- Workspace `w7` with tabs `w7:t1` and `w7:t2`; panes `w7:p1`, `w7:p2`, `w7:p3`.
- Herdr worktree at the scratchpad path `mlcore-wt-a`, on branch `mlcore-wt-a`, workspace `w8`.
- Two Claude agents, `mlcore-a` and `mlcore-b`.
- Cleanup: the worktree was removed with `herdr worktree remove --workspace w8`, which also closed `w8`; `w7` was closed with `workspace close`; the `mlcore-wt-a` branch was deleted from the fixture; both agents were exited with `/exit`; the background ticker process was stopped.

## Gate results

### HERDR_WORKSPACE_READY — PASS (live)

- `herdr workspace create --cwd <fixture> --label mlcore-disposable-t1 --no-focus` returned `workspace_created` with `w7`, `w7:t1`, `w7:p1` (`t1-create.json`).
- `herdr tab create --workspace w7 --label mlcore-tab2 --no-focus` returned `w7:t2`, `w7:p2` (`t1-tab.json`). `tab list` and `workspace get` reported 2 tabs and 2 panes.
- `herdr pane run w7:p1 "pwd; git rev-parse --abbrev-ref HEAD; echo MLCORE_T1_MARK"` followed by `pane wait-output --match` matched. The read output showed the fixture cwd and branch `main`. The pane read was printed in the session but not saved to a file.

### MULTI_AGENT_READY — PASS (live, with one operator-approved trust prompt)

- `herdr agent start mlcore-a --kind claude --pane w7:p1` and `mlcore-b --kind claude --pane w7:p3` both returned `agent_not_ready`. Claude showed its workspace-trust dialog for the new fixture folder. Enter was sent to both panes; the default choice was "Yes, I trust this folder", and the folder was created for this test. Both then reached `idle`.
- `herdr agent prompt … --wait`, run in parallel, returned for both agents with `agent_status: done`, `interactive_ready: true`, and distinct Claude session IDs (`a88c1dbc-24d3-4df2-8cdc-2960ea32bcd9` for `mlcore-a`, `0fdd24df-31e4-4335-a2b6-e986a0ba3a5c` for `mlcore-b`) (`t2-a-prompt.json`, `t2-b-prompt.json`).
- A subsequent `agent read` of each agent's scrollback contained its token (`MLCORE_A_OK` and `MLCORE_B_OK`), each appearing twice (prompt echo and reply). These reads were printed in the session but not saved to a file.
- After the turn, `agent get` reported `idle` with `interactive_ready: true`.

Caveat: the trust dialog blocks `agent start` until confirmed. A new folder needs its operator approval before a managed Claude agent is usable in it.

### TERMINAL_AND_LOGS_READY — PASS (live)

- In `w7:p2`, `… | tee logs/t3.log` wrote three lines. The file on disk contained exactly `MLCORE_LOG_LINE_1..3`, and the pane read showed the same lines.
- `pane get w7:p2` reported `workspace_id: w7`, `tab_id: w7:t2`, and `cwd` and `foreground_cwd` equal to the fixture project. `pane process-info` reported the foreground `bash` process and its cwd. These outputs were printed but not saved to a file.

Caveat: project association was verified only through pane cwd and workspace metadata. Herdr exposes no explicit project binding beyond that.

### GIT_WORKTREE_READY — PASS (live)

- `herdr worktree create --cwd <fixture> --branch mlcore-wt-a --path <scratch>/mlcore-wt-a --no-focus` returned `worktree_created` in workspace `w8`, with `is_linked_worktree: true` and branch `mlcore-wt-a` (`t4-wt.json`).
- `git worktree list` showed the main checkout on `main` at `3881ce0`, and the linked checkout on `mlcore-wt-a`. A commit made in the linked worktree (`db34709`, adding `WT_ONLY.txt`) was absent from the main checkout, and the main branch stayed at `3881ce0`.
- `herdr worktree remove --workspace w8` returned `worktree_removed`.

### CORE_RECOVERY_READY — NOT VERIFIED (partial; the saved evidence supports only the pane-process survival point)

- Pane process survival across an observer detach: a background process in `w7:p2` appended to `logs/t5.log`, which reached 58 lines. This shows the pane process kept running. It is evidence of process survival only.
- `herdr terminal session observe <terminal>` was run twice under a 4 s timeout. Both runs produced byte-identical output (`t5-observe1.txt`, `t5-observe2.txt`), each a single full attach snapshot (`"full": true, "seq": 1`). The output contains no streamed lines, and the earlier per-interval line counts were not saved. This shows that an observer attaches and returns a snapshot. It does not show a live stream, and it does not show reattach continuity beyond the snapshot.
- Claude `--resume`: `mlcore-a` exited with `/exit`, and its pane returned to a shell. A restart with `herdr agent start mlcore-a --kind claude --pane w7:p1 -- --resume a88c1dbc-24d3-4df2-8cdc-2960ea32bcd9` returned `idle` with the same session ID. A follow-up prompt asked for the earlier token, and the scrollback then showed `MLCORE_A_OK` three times. No output from the resume start or the follow-up was saved to a file, so this result is not reproducible from the evidence on disk.

Not tested:
- A full TUI detach and reattach (`ctrl+b q` and reattach), which needs an interactive terminal.
- A Herdr server restart with agents surviving it. The default session server was deliberately not stopped.
- Restoration without an explicit `--resume` argument.

## Independent review

A fresh-context reviewer read this report against the saved evidence files (verdict: APPROVE_WITH_CHANGES). It found the observer claims and the resume claim unsupported by saved files, the production-untouched claim unverified without before/after snapshots, and the leftover branch and worktree cleanup to be incomplete. This revision corrects each of those points:
- The observer claims were reduced to what the saved files show.
- The resume claim was moved to NOT VERIFIED, because its evidence was not saved.
- The production-isolation claim was limited to the command log and the post-test snapshot.
- The leftover branch was deleted and is recorded above.

## Release decision

| Gate | Status | Evidence |
|---|---|---|
| HERDR_WORKSPACE_READY | PASS (live) | workspace and tab creation, saved in `t1-create.json` and `t1-tab.json`; pane output printed but not saved |
| MULTI_AGENT_READY | PASS (live) | two Claude agents, start, prompt, status, distinct sessions saved in `t2-*-prompt.json`; token reads printed but not saved |
| TERMINAL_AND_LOGS_READY | PASS (live) | log file on disk and pane cwd/workspace association; pane metadata printed but not saved |
| GIT_WORKTREE_READY | PASS (live) | Herdr worktree create/remove and isolation, saved in `t4-wt.json` and the git output |
| CORE_RECOVERY_READY | NOT VERIFIED | process survival is shown in the log; observer output is a snapshot only; Claude resume was not saved; full TUI detach and server restart were not exercised |
| Manual Core release | BLOCKED pending evidence | four gates PASS; CORE_RECOVERY needs a re-run that saves its output to files, followed by a fresh independent review |

Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) are unchanged and remain BLOCKED per `2026-11-03-manual-core-continuation.md`. This report does not change them.
