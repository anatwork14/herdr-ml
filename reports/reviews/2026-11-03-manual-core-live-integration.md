# Manual Core live Herdr integration — evidence

Scope: Research OS Manual Core gates only. Automatic bootstrap, queued runs, official runs and GPU execution were not enabled or run.

Orchestrator: the integration session in Herdr pane `w5:p2`, workspace `w5`, with `HERDR_ENV=1` verified before any control command. No Codex instance was used to control Herdr.

Versions: Herdr `0.9.5` (stable, protocol 22, `herdr-version.txt` in the recovery evidence). Implementation under test: PR #2 head `b51a6b328d136dee31f67b56f7369db9b0a14bf2`. Recovery evidence: `reports/herdr-live/recovery/` (index in its `README.md`).

## Production isolation

- Baseline inventory before the recovery run (`reports/herdr-live/recovery/baseline/`): `w1` (speculative-decoding starlight, 3 panes), `w3` (fbid, 1), `w5` (`~`, 2), `w6` (monitoring, 1). Production agents `w1:p1` (agy) and `w5:p2` (this session).
- Post-test inventory (`reports/herdr-live/recovery/post/`) matches the baseline: the same four workspaces, pane and tab counts, production agents, and only the `default` session running.
- Every recovery-run command targeted only `w9` and its panes. This is a check of the command log plus the before/after inventories, not a byte-level comparison of each workspace's contents.
- The first-round run also targeted only `w7` and `w8`; its post-state is not separately inventoried in the recovery directory.

## Live capability inventory

- Herdr `0.9.5`, server running, endpoint compatible, no restart needed.
- Command groups: `agent`, `pane`, `workspace`, `tab`, `worktree`, `terminal`, `notification`, `integration`, `session`, `machine`, `api`.
- Agent kinds include `claude`, `codex`, `agy`. Only `claude` was used as a test subject.

## Gate results

### HERDR_WORKSPACE_READY — PASS (live)

Round 1 (`w7`): `workspace create` returned `w7`, `w7:t1`, `w7:p1` (`t1-create.json`). `tab create` returned `w7:t2`, `w7:p2` (`t1-tab.json`). A command run through `pane run` with `wait-output` matched, and the read showed the fixture cwd and branch `main`. The pane read was printed in the session but not saved to a file.

Round 2 (`w9`, recovery run): `workspace create` returned `w9`, `w9:t1`, `w9:p1`, exit 0 (`reports/herdr-live/recovery/run/01-workspace-create.json`).

### MULTI_AGENT_READY — PASS (live, with operator-approved trust prompts)

- Round 1: two Claude agents, `mlcore-a` and `mlcore-b`, were started in `w7`. Both returned `agent_not_ready` because of Claude's workspace-trust dialog for the new fixture folder. Enter confirmed the default "Yes" on both panes, and both reached `idle`.
- Round 1 prompts ran in parallel with `--wait`. Both returned `agent_status: done`, `interactive_ready: true`, and distinct session IDs (`t2-a-prompt.json`, `t2-b-prompt.json`).
- Round 1 scrollback contained each agent's token (`MLCORE_A_OK` and `MLCORE_B_OK`), each appearing twice (prompt echo and reply). These reads were printed but not saved.
- Round 2 (recovery) runs a single agent and is in the recovery evidence.

Caveat: the trust dialog blocks `agent start` until confirmed, and a new folder needs its operator's approval before a managed Claude agent is usable in it. In round 2 the default cursor position was "No, exit", so Enter alone would have exited. The agent was confirmed only after `down` moved the cursor to "Yes" and the screen showed it.

### TERMINAL_AND_LOGS_READY — PASS (live)

- Round 1: `… | tee logs/t3.log` in `w7:p2` wrote three lines. The file on disk contained exactly `MLCORE_LOG_LINE_1..3`, and the pane read showed the same lines.
- `pane get` reported `workspace_id: w7`, `tab_id: w7:t2`, and `cwd` and `foreground_cwd` equal to the fixture project. `pane process-info` reported the foreground `bash` process and its cwd. These outputs were printed but not saved.

Caveat: project association was verified only through pane cwd and workspace metadata. Herdr exposes no explicit project binding beyond that.

### GIT_WORKTREE_READY — PASS (live)

- Round 1: `herdr worktree create --cwd <fixture> --branch mlcore-wt-a --path <scratch>/mlcore-wt-a --no-focus` returned `worktree_created` in `w8`, with `is_linked_worktree: true` and branch `mlcore-wt-a` (`t4-wt.json`).
- `git worktree list` showed the main checkout on `main` at `3881ce0`, and the linked checkout on `mlcore-wt-a`. A commit made in the linked worktree (`db34709`, adding `WT_ONLY.txt`) was absent from the main checkout, and the main branch stayed at `3881ce0`.
- `herdr worktree remove --workspace w8` returned `worktree_removed`. The fixture branch was deleted afterward.

### CORE_RECOVERY_READY — evidence complete for the defined behaviors; verdict pending independent review

The defined recovery behaviors are: (a) detach and reattach of agent output, (b) supported restoration of an agent to its prior session, and (c) process survival across detach. Evidence for each is saved under `reports/herdr-live/recovery/`.

**(a) Detach and reattach, pane observer.** `herdr terminal session observe` was run three times under a 6 s timeout (`run/10-observer-{1,2,3}.out`, exit 124 each, empty stderr). Each capture begins with a full snapshot (`"full": true`, `"seq": 1`) and continues with delta frames (`"full": false`, seq 2–7). The deltas redraw only changed cells, such as the counter and timestamp digits, and one delta's timestamp (`11:06:59.127Z`) matches the ticker's own log line for that second. Each capture therefore shows an attach snapshot followed by real streamed updates.

**(a′) Full interactive attach and detach.** `herdr terminal attach <terminal>` was run twice under a pseudo-terminal with a 120×40 window (`run/11-tui-attach-{1,2}.bin`, `run/11-tui-attach-{1,2}.json`, driver `tui_attach.py`). Each attach shows the alternate-screen entry and exit and live ticker content. The `ctrl+b q` detach exited with code 0 within about 1 ms of the key. This is the supported direct terminal attach. It is not the main Herdr sidebar TUI, which was not exercised.

**(b) Supported agent restoration.** The agent `mlrec-agent` (Claude, session `f6ae30ec-0f0c-4883-95de-74f71afa29d7`) was given a random token in a prompt and settled to `done` (`run/05-prompt-token.json`). It was exited with `/exit`. Claude printed `claude --resume f6ae30ec-0f0c-4883-95de-74f71afa29d7`, and the agent left the pane (`run/06-pane-after-exit.txt`). The restart command was `herdr agent start mlrec-agent --kind claude --pane w9:p1 --timeout 90000 -- --resume f6ae30ec-0f0c-4883-95de-74f71afa29d7`, exit 0, empty stderr. Its JSON returned `agent_started`, the same session ID, and `argv` including `--resume` (`run/07-*`). A recall prompt with a unique marker was answered, and the token appears after the marker (`run/08-recall-check.txt`: `marker_found=True token_after_marker=True`).

**(c) Process survival.** A pane process wrote a timestamped line every second (`run/ticker.log`, 124 contiguous lines at 1.00–1.03 s intervals). Every observer and attach window contains about five to six ticks, including the detached gaps (`run/survival.txt`). This shows the pane process survived each detach. It is not evidence of streaming, which is shown by the delta frames in (a).

**Not tested:**
- A Herdr server restart with agents surviving it. The default server was deliberately not stopped, and the brief forbids restarting the production server.
- Restoration without an explicit `--resume` argument. Herdr's restore path depends on the session ID recorded by the agent.
- The main Herdr sidebar TUI attach/detach, as distinct from `terminal attach`.
- Restoration after a crash, rather than a clean `/exit`.

**Verdict.** The three defined behaviors (a, b, c) have saved evidence, so CORE_RECOVERY_READY is a candidate for PASS. Under the brief it is not converted automatically. Its status is `PENDING_INDEPENDENT_REVIEW`, pending the fresh review requested for this revision.

## Gate status

| Gate | Status | Evidence |
|---|---|---|
| HERDR_WORKSPACE_READY | PASS (live) | round 1 (`w7`) saved in `t1-create.json` and `t1-tab.json`; round 2 (`w9`) in `reports/herdr-live/recovery/run/01-*` |
| MULTI_AGENT_READY | PASS (live) | round 1 (two agents) saved in `t2-*-prompt.json`; the token reads were printed but not saved |
| TERMINAL_AND_LOGS_READY | PASS (live) | log file on disk and pane cwd/workspace association; pane metadata printed but not saved |
| GIT_WORKTREE_READY | PASS (live) | Herdr worktree create/remove and isolation, saved in `t4-wt.json` and the git output |
| CORE_RECOVERY_READY | PENDING_INDEPENDENT_REVIEW | (a), (a′), (b) and (c) have saved evidence; not converted to PASS until the fresh review. Not tested: server restart, restoration without `--resume`, sidebar TUI, crash recovery |
| Manual Core release | PENDING_INDEPENDENT_REVIEW | four gates PASS and CORE_RECOVERY awaiting review; the release is not declared ready until the review is complete |

Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) are unchanged and remain BLOCKED per `2026-11-03-manual-core-continuation.md`. This report does not change them.

## Prior independent review (round 1)

A fresh-context reviewer read the round-1 report against its saved evidence (verdict APPROVE_WITH_CHANGES). It found the observer claims and the resume claim unsupported by saved files, the production-untouched claim unverified without before/after snapshots, and the leftover branch and worktree cleanup to be incomplete. Round 2 addresses each point:
- Observer claims were re-run and saved, with the snapshot and delta structure decoded.
- The resume was re-run and saved, with its command, exit status, stdout, stderr and recall check.
- Before/after inventories are saved, and the diff is empty.
- Cleanup is recorded, including the ticker stop, the agent exit, and the workspace close.

## Round-2 review

Pending. A fresh review of this revision will be requested at the commit that contains it.
