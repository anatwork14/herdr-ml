# Manual Core live Herdr integration — evidence

Scope: Research OS Manual Core gates only. Automatic bootstrap, queued runs, official runs and GPU execution were not enabled or run.

Orchestrator: the integration session in Herdr pane `w5:p2`, workspace `w5`. The `HERDR_ENV=1` check passed before control commands (run in-session; no file saved). No Codex instance controlled Herdr.

Versions: Herdr `0.9.5` (stable, protocol 22), saved in `reports/herdr-live/recovery/post/herdr-version.txt` and `post/herdr-status.txt`.

Code under test: round 1 and round 2 ran at PR #2 head `b51a6b328d136dee31f67b56f7369db9b0a14bf2`. Cycle 2 ran at `fbe35d41fb607e2d78f95fa928e0e7f27e43b68a`. Evidence-only commits since then: `6d6d16c`, `ec3a4d3`, `21286ba`, `fbe35d4`, `5682906`, and the commit that adds this revision.

Indexes: `reports/herdr-live/recovery/README.md` (round 2), `reports/herdr-live/recovery/cycle2/README.md` (cycle 2), `reports/herdr-live/gates/` (gate re-run), `reports/herdr-live/round1/` (round-1 artifacts).

## Production isolation

Not established as byte-identical. Stable identifiers are unchanged; transient fields changed in the default session, and the cause is unexplained.

- Round 1 and round 2: state-changing commands targeted only disposable workspaces (`w7`/`w8`, then `w9`). Read-only inventory commands also ran against `w1`, `w3`, `w5`, `w6`. The post-test files differ from the baseline: `focused` flags moved between `w1` and `w3`, and `w6:p1` `scroll.max_offset_from_bottom` went from 0 to 13. No command targeted those panes.
- Cycle 2 (`cycle2/post/comparison.txt`, reproduced by re-diffing the raw JSON): stable identifiers, names, panes, kinds, sessions and workspace/tab sets are unchanged. Transient differences: the default-session agy agent `w1:p1` went `idle` → `done`, with `completion_seq` and `state_change_seq` 177 → 180; `w1:p1` scroll offset 104 → 185; `w6:p1` scroll offset 41 → 55.
- The cycle-2 named-session commands carried `--session mlrec-c2`, and that is recorded in `cycle2/named/` only for the commands whose files store their argv. The complete command log for cycle 2 is not saved, so "no default-session command wrote input" is not established. The agy change is unexplained and may be the user's own work.
- The cycle-2 baseline was captured in one batch before the cycle's first command; no per-file timestamp was saved, so that ordering rests on the README.

## Live capability inventory

- Herdr `0.9.5`, server running and endpoint compatible.
- The installed Claude integration is outdated (v9 < v11), per `herdr integration status`. It was not upgraded: the hooks are global and also affect production agents. The status output itself was not saved as a file.
- Only `claude` was used as a test subject.

## Gate results

### HERDR_WORKSPACE_READY — PASS (live)

- Round 1: `workspace create` returned `w7`, `w7:t1`, `w7:p1` (`reports/herdr-live/round1/t1-create.json`). `tab create` returned `w7:t2` (`round1/t1-tab.json`).
- Gate re-run, named session `mlrec-gate` (`reports/herdr-live/gates/run2/steps/workspace-create.out`, `tab-create.out`, `workspace-get.out`): workspace and tab created and inspected.
- Round 1's `pane run` / `wait-output` / read output was printed but not saved. The gate re-run covers this with saved output (below).

### MULTI_AGENT_READY — PASS (live, prompts saved)

- Round 1 (`round1/t2-a-prompt.json`, `round1/t2-b-prompt.json`): two Claude agents returned `agent_status: done`, `interactive_ready: true`, with distinct session IDs `a88c1dbc-…` and `0fdd24df-…`.
- Not saved: the round-1 `agent_not_ready` responses and the trust-dialog confirmations. Those states are described from the session, not from files.
- Cycle 2 and round 2 each ran one agent, covered below.

Caveat: the trust dialog blocks `agent start`. The default cursor was on "No, exit" in later runs, so each agent was confirmed only after `down` moved it to "Yes".

### TERMINAL_AND_LOGS_READY — PASS (live, saved)

The re-run is `reports/herdr-live/gates/run2/`, produced by `reports/herdr-live/gates/run_gates.sh`. The script logs each step's argv, exit code and timestamps to `commands.log`.
- The pane command `… | tee logs/gate.log` ran in pane `w1:p1` of session `mlrec-gate`. `pane wait-output --match GATE_DONE` matched output (`steps/pane-wait.out`, `result.type: output_matched`).
- The log file `logs/gate.log` (copied to `run2/logs/gate.log`) contains exactly `GATE_LOG_LINE_1..3`. The `pane read` output (`steps/pane-read.out`) holds the command echo, the three log lines, the `GATE_DONE` marker, and a prompt.
- `pane get` (`steps/pane-get.out`) reports `workspace_id: w1`, `tab_id: w1:t1`, and the cwd equal to the fixture project. `pane process-info` is in `steps/pane-process-info.out`.

Caveat: a first run (`reports/herdr-live/gates/run1/`) produced a false match. `wait-output` matched the echoed command text, and no log was saved from that run. That run's output is kept as evidence of the defect; the script was then changed to print the marker in a form the echoed command cannot match, and `run2` is the valid result. Project association rests on pane cwd and workspace metadata, since Herdr has no separate project binding.

### GIT_WORKTREE_READY — PASS (live, saved)

Also in `reports/herdr-live/gates/run2/`:
- `worktree-create.out`: `worktree_created`, with `is_linked_worktree: true` and branch `gate-wt`.
- `worktree-list-before-commit.out`: the main checkout and the linked worktree.
- A file written and committed in the linked worktree gave `wt-head.out` = `0e4104c`. `main-head.out` = `42cb921` (unchanged).
- `isolation-check.out` = `MAIN_CLEAN`: the worktree-isolation check (a file written in the linked worktree is absent from the main checkout). This is not production isolation.
- `worktree-remove.out`: `worktree_removed`. `branch-delete` removed `gate-wt`. `worktree-list-after.out` shows only the main checkout, and `branch-after.out` is empty.

Round 1 also created and removed a worktree (`round1/t4-wt.json`); that round's git output was printed, not saved.

### CORE_RECOVERY_READY — PASS (criteria a–c; see the consolidated run below)

The defined behaviors are: (a) detach and reattach of agent output, (b) restoration of an agent to its prior session, and (c) process survival across detach. Cycle 2 tested these on the Claude agent's own terminal.

**(a) Agent-terminal observer and detach (`cycle2/named/`).** The agent terminal was `term_65f5d670305601`, pane `w1:p1`, in session `mlrec-c2`.
- A 40 s observer (exit 124, `07-observerA.*`) ran while turn 2 was sent. Its 10 frames are: seq 1, `full=true`, containing the turn-1 token; seq 2–10, `full=false` deltas. Delta seq 9 contains a standalone `67`, the turn-2 answer (`cycle2/observer-A-frames.txt`).
- Detach with the documented `ctrl+b q` through `pty_attach_once.py`: exit 0 (`08-pty-attach-1.json`, 9210 bytes before detach, `detach_sent` 11:18:55.116Z, `end` 11:18:55.117Z). The 1 ms is the recorded gap; the driver polls every 0.2 s, so it is an upper bound.
- Reattach (`10-pty-attach-2.json`, exit 0, 9501 bytes before detach; the `.bin` contains the token and `71`).

**(b) Restoration after a clean named-server restart (`cycle2/named/`, one run).** This is the only restore run. It used an outdated Claude integration and a clean stop, so it does not show general restore behavior.
- `herdr --session mlrec-c2 server stop` exited 0 (`13-stop.*`). The session showed `running: false`.
- `herdr --session mlrec-c2 server` was started with no agent arguments. `agent list` then showed `c2-agent`, `w1:p1`, `claude`, `idle`, with the same session ID (`14-agents-after-restart.json`).
- `pane get` before the stop (`12-pane-before-stop.json`) has neither a `resume_argv` nor a `restore_error` field.
- A `claude --resume 7ddaa64d-…` process exists, and its parent chain is bash → `herdr --session mlrec-c2 server` → init (`16-process-ancestry.txt`). The saved files do not record the relaunch mechanism or any argv Herdr used. The chain shows where the process sits, not how it was started.
- Recall after the restart (`17-recall-check.txt`): `token_after_marker=True`. The reattach capture `10-pty-attach-2.bin` also contains the token and the turn-3 answer `71`.
- The restored pane transcript (`15-pane-after-restart.txt`) shows the prior conversation.
- Not saved: the schema text about `resume_argv` ("after a Herdr restart"), and the `session.json` field list. Both were read during the run and quoted into the README; the session directory was deleted with the session.

**(c) Process survival.** On the agent pane, the saved evidence is that a prompt sent while the client was detached was answered: turn 3 was sent at `09-turn3-detached.ts` (11:18:55.144Z, after the detach ended at 55.117Z) and returned `idle` with session `7ddaa64d-…` and the answer `71` in the transcript (`17-recall-scrollback.txt`). No observer ran during the detach window, so no output was observed while detached. The round-2 ticker evidence (`reports/herdr-live/recovery/run/`) covers process survival on a separate terminal, not the agent.

**Not tested:**
- A server crash (kill, power loss), as opposed to the clean stop.
- Restore with an up-to-date Claude integration, or with `resume_argv` reported by the integration.
- The main Herdr sidebar TUI.
- Restoration of the production default session. It was not stopped and must not be.
- Output observed during the detach window on the agent pane.

**Final isolated cycle (`integration-upgrade/final/`, commit `c19c3e8`-`2dfcbb3`).** Run on the isolated v11 Claude config (P2 login by the account owner) in named session `mlrec-final`. One token prompt (exit 0, acknowledged `OK`); clean `server stop` (exit 0); restart with no agent arguments; the agent was restored in the same session `2d592efa-…`, with argv `claude --resume 2d592efa-…` and the server as grandparent (`run/21`). The server log records a restore with `workspaces=1` but not the argv or sender (`run/23`). One marker-only recall prompt returned the token (`run/26`, `run/28`). This is one case of clean-stop restoration on v11. It does not establish the restore mechanism, crash recovery, or the sidebar TUI.

Cleanup and isolation are not clean. The agent's exit before the stop was not verified: the `/exit` send outputs and exit codes were not saved. The zero-process count was taken after the stop. The default session's production pane `w1:p1` `scroll.max_offset_from_bottom` changed 1235 → 1239, and that change is unexplained. Stable inventories are otherwise identical, and two production files are hash-verified.

**Consolidated run (`reports/herdr-live/recovery-closure/r2/`, `2e17f9d` evidence, corrected `5cd49b4`).** Named session `mlrec-close-r2`, one agent start, isolated v11 config.
- (a) Detach and reattach with output produced during the detached window: the observer frames show the answer at 23.930–24.600 s, inside the window (17.313–28.579 s); reattach shows the completed output (viewport snapshot, not a stream).
- (b) Clean named-server restart without an operator `--resume`: same session `75971b1d-…`, restored argv `claude --resume 75971b1d-…`, named server as grandparent; recall after the restart returned the token after the marker.
- (c) Process survival across detach: the claude pid kept its identity from 17.8 s through 24.8 s and answered a prompt sent while detached.
- Cleanup: `/exit` sends exit 0; one identity poll (`GONE_OR_REUSED`) 3.35 s after the send, plus post-exit pane state; server stop exit 0; session deleted.
- Production: stable identifiers identical before and after; `w1:p1` scroll 397 → 405 and focus moves reported as ephemeral, the scroll change accepted as unexplained transient metadata by the owner's authorization.

**Verdict.** CORE_RECOVERY_READY is `PASS` on criteria (a)–(c), per the PLAN section 6 definition and the independent re-review at `5cd49b4`. Two deviations are recorded: exit was checked with one identity poll rather than the per-second polling of PLAN step 8, and the restore mechanism and launch attribution remain unproven. Crash recovery and the sidebar TUI were not tested. The owner approved a limited manual-only Manual Core release on 2026-11-04 (see the status below). The earlier reviews' findings are addressed in the corrected RESULT.md.

### Release decision

| Gate | Status | Evidence |
|---|---|---|
| HERDR_WORKSPACE_READY | PASS (live) | `round1/t1-*.json`; `gates/run2/steps/workspace-create.out`, `tab-create.out` |
| MULTI_AGENT_READY | PASS (live) | `round1/t2-*-prompt.json`; trust-dialog states unsaved |
| TERMINAL_AND_LOGS_READY | PASS (live) | `gates/run2/logs/gate.log`, `steps/pane-read.out`, `steps/pane-get.out` |
| GIT_WORKTREE_READY | PASS (live) | `gates/run2/steps/worktree-create.out`, `wt-head.out`, `isolation-check.out`, `worktree-remove.out`, `branch-after.out` |
| CORE_RECOVERY_READY | PASS (criteria a–c; reviewed at `5cd49b4`) | consolidated run `reports/herdr-live/recovery-closure/r2/RESULT.md`; deviations: one exit identity poll instead of per-second polling (PLAN step 8), not re-run; limits: crash recovery, sidebar TUI and launch attribution untested or unproven |
| Manual Core release | APPROVED — limited manual-only release (owner decision 2026-11-04, reviewed head `88a37f3`) | all five gates PASS with the limits above; scope, excluded capabilities, SHAs and deviations: `reports/reviews/2026-11-04-manual-core-owner-decision.md`; independent reviews: `reports/herdr-live/recovery-closure/review/independent-reviews.md` |

Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) are unchanged and remain BLOCKED per `2026-11-03-manual-core-continuation.md`.

## Prior independent reviews

- **Round 1** (APPROVE_WITH_CHANGES): observer and resume claims unsupported by saved files; cleanup incomplete. Addressed at `b51a6b3`.
- **Round 2** (`ec3a4d3`, APPROVE_WITH_CHANGES): agent pane untested; manual restore only; the false "diff is empty" claim; token source; delta wording; implementation SHA; unsupported version claims; "within about 1 ms". All addressed in later revisions.
- **Round 3** (`21286ba`, APPROVE_WITH_CHANGES): round-1 artifacts not in the repository (fixed in `fbe35d4`); the "every recovery-run command" claim (corrected); evidence-commit list; the unsaved `HERDR_ENV` check; wording of direct attach; the production-server rule.
- **Cycle-2 review** (`5682906`, APPROVE_WITH_CHANGES; CORE_RECOVERY PASS not supported at that revision). Findings and responses:
  - HIGH, `round1/logs/t3.log` missing from git: confirmed. `.gitignore` has `logs/`, so the files were never committed. They are now force-added (`round1/logs/t3.log`, `t5.log`). The TERMINAL gate now rests on the re-run, not on that file alone.
  - HIGH, "`--resume` supplied by Herdr" is not in any saved file: confirmed. Reworded: the relaunch mechanism was not captured.
  - HIGH, "no default-session command wrote input, as far as the command log shows" cites a log that was not saved: confirmed. Removed; the isolation claim is limited as stated above.
  - MEDIUM, unsaved trust and exit captures: confirmed; now marked unsaved in the text. The `02-agent-start.json`, `18-exit-*.json` and `19-server-stop.stdout` outputs are empty files.
  - MEDIUM, `session.json` cited from a deleted directory: confirmed; now marked README-reported.
  - MEDIUM, "process survival on the agent pane": confirmed overstated; reworded to "a prompt sent while detached was answered".
  - MEDIUM, GIT gate partly supported: confirmed; the re-run now saves the git and worktree outputs.
  - LOW items (MULTI_AGENT round-1 states, HERDR_WORKSPACE pane-run output, baseline timing, wording) are reflected above.

**Status (2026-11-04).** The re-run gate evidence and the cycle-2 wording were reviewed in the earlier rounds. CORE_RECOVERY_READY was reviewed at `5cd49b4` and is PASS on criteria (a)–(c). The owner approved a limited manual-only Manual Core release on this basis, with the deviations recorded in `reports/reviews/2026-11-04-manual-core-owner-decision.md`. Experiment-infrastructure holds remain BLOCKED.
