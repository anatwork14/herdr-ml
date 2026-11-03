# Manual Core live Herdr integration — evidence

Scope: Research OS Manual Core gates only. Automatic bootstrap, queued runs, official runs and GPU execution were not enabled or run.

Orchestrator: the integration session in Herdr pane `w5:p2`, workspace `w5`, with `HERDR_ENV=1` verified before any control command. No Codex instance was used to control Herdr.

Versions: Herdr `0.9.5` (stable, protocol 22). Saved in `reports/herdr-live/recovery/post/herdr-version.txt` and `post/herdr-status.txt`. Implementation under test: `b51a6b328d136dee31f67b56f7369db9b0a14bf2`, the PR #2 head when the recovery test ran. Evidence commits: `6d6d16c` (round-2 raw artifacts), `ec3a4d3` and `21286ba` (report revisions). Round-1 artifacts are in `reports/herdr-live/round1/` (committed with this revision). Recovery evidence index: `reports/herdr-live/recovery/README.md`.

## Production isolation

- Baseline before the recovery run (`reports/herdr-live/recovery/baseline/`): `w1` (speculative-decoding starlight, 3 panes), `w3` (fbid, 1), `w5` (`~`, 2), `w6` (monitoring, 1). Production agents `w1:p1` (agy) and `w5:p2` (this session).
- Post-test (`reports/herdr-live/recovery/post/`): the same four workspaces, pane and tab counts, the same production agents, and only the `default` session running. The files are not byte-identical. Field-by-field differences: `focused` flags moved between `w1` and `w3` in the workspace, tab, pane and agent lists, and `w6:p1` `scroll.max_offset_from_bottom` changed from 0 to 13. No test command targeted those panes or changed focus, so the cause is unverified.
- Every state-changing recovery-run command targeted only `w9` and its panes. Read-only inventory commands (`workspace list`, `session list`, `agent list`, `tab list`, `pane list`) also ran against `w1`, `w3`, `w5` and `w6`. The round-1 run changed state only in `w7` and `w8`; its post-state is not separately inventoried.

## Live capability inventory

- Herdr `0.9.5`, server running and endpoint compatible (`post/herdr-status.txt`).
- Only `claude` was used as a test subject.

## Gate results

### HERDR_WORKSPACE_READY — PASS (live)

Round 1 (`w7`): `workspace create` returned `w7`, `w7:t1`, `w7:p1` (`reports/herdr-live/round1/t1-create.json`). `tab create` returned `w7:t2`, `w7:p2` (`reports/herdr-live/round1/t1-tab.json`). A command run through `pane run` with `wait-output` matched, and the read showed the fixture cwd and branch `main`. The pane read was printed in the session but not saved to a file.

Round 2 (`w9`): `workspace create` returned `w9`, `w9:t1`, `w9:p1`, exit 0 (`reports/herdr-live/recovery/run/01-workspace-create.json`).

### MULTI_AGENT_READY — PASS (live, with operator-approved trust prompts)

- Round 1: two Claude agents, `mlcore-a` and `mlcore-b`, were started in `w7`. Both returned `agent_not_ready` because of Claude's workspace-trust dialog for the new fixture folder. Enter confirmed the default "Yes" on both panes, and both reached `idle`.
- Round 1 prompts ran in parallel with `--wait`. Both returned `agent_status: done`, `interactive_ready: true`, and distinct session IDs (`t2-a-prompt.json`, `t2-b-prompt.json`).
- Prompt results are in `reports/herdr-live/round1/t2-a-prompt.json` and `t2-b-prompt.json`, and the split step in `t2-split.json`. Round 1 scrollback contained each agent's token (`MLCORE_A_OK` and `MLCORE_B_OK`), each appearing twice (prompt echo and reply). Those scrollback reads were printed but not saved.
- Round 2 ran one agent; see the recovery evidence.

Caveat: the trust dialog blocks `agent start` until confirmed, and a new folder needs its operator's approval before a managed Claude agent is usable in it. In round 2 the default cursor position was "No, exit", so Enter alone would have exited. The agent was confirmed only after `down` moved the cursor to "Yes" and the screen showed it.

### TERMINAL_AND_LOGS_READY — PASS (live)

- Round 1: `… | tee logs/t3.log` in `w7:p2` wrote three lines. The file on disk (`reports/herdr-live/round1/logs/t3.log`) contains exactly `MLCORE_LOG_LINE_1..3`. The pane read was printed but not saved.
- `pane get` reported `workspace_id: w7`, `tab_id: w7:t2`, and `cwd` and `foreground_cwd` equal to the fixture project. `pane process-info` reported the foreground `bash` process and its cwd. These outputs were printed but not saved.

Caveat: project association was verified only through pane cwd and workspace metadata. Herdr exposes no explicit project binding beyond that.

### GIT_WORKTREE_READY — PASS (live)

- Round 1: `herdr worktree create --cwd <fixture> --branch mlcore-wt-a --path <scratch>/mlcore-wt-a --no-focus` returned `worktree_created` in `w8`, with `is_linked_worktree: true` and branch `mlcore-wt-a` (`reports/herdr-live/round1/t4-wt.json`).
- `git worktree list` showed the main checkout on `main` at `3881ce0`, and the linked checkout on `mlcore-wt-a`. A commit made in the linked worktree (`db34709`, adding `WT_ONLY.txt`) was absent from the main checkout, and the main branch stayed at `3881ce0`.
- `herdr worktree remove --workspace w8` returned `worktree_removed`. The fixture branch was deleted afterward.

### CORE_RECOVERY_READY — NOT VERIFIED

The defined behaviors are: (a) detach and reattach of agent output, (b) supported restoration of an agent to its prior session, and (c) process survival across detach.

**The tests did not cover the agent's own pane.** Every observer and attach test in the recovery run targeted the disposable ticker terminal `term_65f5d4256e76d14` (pane `w9:p2`). The agent pane `w9:p1` was never observed, attached or detached. Requirement (a) and the agent-output part of (c) are therefore not verified.

**(a) Observer output, ticker terminal.** `herdr terminal session observe` was run three times under a 6 s timeout (`run/10-observer-{1,2,3}.out`, exit 124 each, empty stderr). Each capture begins with a full snapshot (`"full": true`, `"seq": 1`) and continues with delta frames (`"full": false`, seq 2–7). The deltas redraw changed cells: the ticker's counter and timestamp digits change in each capture, and in observer 2 the later deltas (seq 5–7) also redraw the command-echo rows. One delta's timestamp (`11:06:59.127Z`) matches the ticker's own log line for that second, so the deltas are real updates and not only the initial snapshot.

**(a′) Interactive attach and detach, ticker terminal.** `herdr terminal attach <terminal>` was run twice under a pseudo-terminal with a 120×40 window (`run/11-tui-attach-{1,2}.bin`, `.json`, driver `tui_attach.py`). Each attach shows the alternate-screen entry and exit and live ticker content. The `ctrl+b q` detach exited with code 0, and the recorded `detach_sent` to `end` gap is 1 ms (the driver polls at 0.2 s, so this is an upper bound from the timestamps). This is the direct terminal attach command used on the ticker terminal. It is not the main Herdr sidebar TUI, and not the agent pane.

**(b) Manual restoration via `--resume` after a clean exit.** The agent `mlrec-agent` (Claude, session `f6ae30ec-0f0c-4883-95de-74f71afa29d7`) was given a random token in a prompt and settled to `done` (token prompt visible in `run/06-pane-before-exit.txt`). It was exited with `/exit`. Claude printed `claude --resume f6ae30ec-0f0c-4883-95de-74f71afa29d7`, and the agent left the pane (`run/06-pane-after-exit.txt`). The restart command was `herdr agent start mlrec-agent --kind claude --pane w9:p1 --timeout 90000 -- --resume f6ae30ec-0f0c-4883-95de-74f71afa29d7`, exit 0, empty stderr. The JSON returned `agent_started`, the same session ID, and `argv` including `--resume` (`run/07-*`). A recall prompt with a unique marker was answered with the token after the marker (`run/08-recall-check.txt`: `marker_found=True token_after_marker=True`).

This is an operator-supplied `--resume` argument after a clean exit, passed through `agent start`. It is not a Herdr-native restore that discovers the session without that argument, and it has not been shown to be the only supported restore path.

**(c) Process survival, ticker terminal.** The ticker wrote a timestamped line every second (`run/ticker.log`, 124 contiguous lines at 1.00–1.03 s intervals). Each observer and attach window contains about five to six ticks, including the detached gaps (`run/survival.txt`). This shows the ticker process survived each detach. It is not evidence of streaming, which the delta frames show in (a). It is not evidence about the agent's process.

**Not tested:**
- Detach and reattach of the agent's own pane (`w9:p1`) while the agent runs. This is the main missing evidence for (a) and (c).
- A Herdr-native restore path that needs no operator-supplied `--resume` argument.
- A Herdr server restart with agents surviving it. The default server was deliberately not stopped; the user's instruction for this run was never to restart the production Herdr server.
- Crash recovery, rather than a clean `/exit`.
- The main Herdr sidebar TUI attach/detach.

**Verdict.** CORE_RECOVERY_READY is NOT VERIFIED. The ticker-terminal evidence does not establish the defined behaviors for the agent's own output, and restoration was manual. Passing it would need: an observer or attach on the agent's pane with a saved detach/reattach capture, plus a restore path shown without an operator-supplied `--resume` argument. Those would need one more agent start, which was outside the authorization for this run.

## Gate status

| Gate | Status | Evidence |
|---|---|---|
| HERDR_WORKSPACE_READY | PASS (live) | round 1 (`w7`) in `reports/herdr-live/round1/t1-create.json` and `t1-tab.json`; round 2 (`w9`) in `reports/herdr-live/recovery/run/01-*` |
| MULTI_AGENT_READY | PASS (live) | round 1 (two agents) in `reports/herdr-live/round1/t2-*-prompt.json`; the scrollback token reads were printed but not saved |
| TERMINAL_AND_LOGS_READY | PASS (live) | round 1 log file in `reports/herdr-live/round1/logs/t3.log`, and pane cwd/workspace association; pane metadata printed but not saved |
| GIT_WORKTREE_READY | PASS (live) | Herdr worktree create/remove and isolation, in `reports/herdr-live/round1/t4-wt.json` and the git output |
| CORE_RECOVERY_READY | NOT VERIFIED | agent-pane detach/reattach untested; restoration was manual via `--resume`; ticker-terminal evidence only |
| Manual Core release | BLOCKED pending evidence | four gates PASS; CORE_RECOVERY NOT VERIFIED |

Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) are unchanged and remain BLOCKED per `2026-11-03-manual-core-continuation.md`. This report does not change them.

## Prior independent reviews

**Round 1** (verdict APPROVE_WITH_CHANGES) found the observer and resume claims unsupported by saved files, and the cleanup incomplete. Those points were addressed in round 2 at `b51a6b3`.

**Round 2** (`ec3a4d3`, verdict APPROVE_WITH_CHANGES, with no CRITICAL findings) raised the following. Each was checked against the files before responding.
- HIGH, agent pane not tested: confirmed. The report now states that (a) and (c) used the ticker terminal, and CORE_RECOVERY is NOT VERIFIED.
- HIGH, restoration was manual: confirmed. (b) is scoped to manual `--resume` after a clean exit.
- HIGH, "the diff is empty": confirmed false. The inventory differences are listed above and attributed as unverified.
- MEDIUM, token source: confirmed. The token appears in the pane read `run/06-pane-before-exit.txt`, not in `run/05-prompt-token.json`. Corrected in the README.
- MEDIUM, delta wording: confirmed. Corrected to describe the observed redraws.
- LOW, implementation SHA: the PR head moved to `ec3a4d3` because of this evidence commit. The report now states `b51a6b3` as the implementation under test.
- LOW, version and command-group claims without a file: the version is now cited to `post/herdr-version.txt` and `post/herdr-status.txt`. The command-group and agent-kind lists were removed.
- LOW, "within about 1 ms": reworded to a recorded gap with a note on the polling bound.

**Round 3** (`21286ba`, verdict APPROVE_WITH_CHANGES, no CRITICAL). The reviewer confirmed the terminal targeting, the NOT VERIFIED verdict, and the spot-checked resume, recall, survival and inventory claims. Responses:
- HIGH, round-1 gates cited artifacts that were not in the repository: confirmed. The artifacts still existed in the orchestrator's scratchpad, so they were copied into `reports/herdr-live/round1/` (with `logs/t3.log` and `logs/t5.log`), and every cited path now points there.
- MEDIUM, "every recovery-run command targeted only w9": confirmed. Corrected to "every state-changing command", with the read-only inventory targets stated.
- LOW, evidence-commit list: `21286ba` added.
- LOW, the `HERDR_ENV` check has no saved file: now stated as in-session only.
- LOW, "supported direct terminal attach": reworded to the command used.
- LOW, the production-server rule: now attributed to the user's instruction for this run.

The round-3 fixes are documentation and artifact-copy changes. They were not independently re-reviewed. CORE_RECOVERY_READY remains NOT VERIFIED, and the Manual Core release remains BLOCKED.
