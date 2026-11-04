# Consolidated CORE_RECOVERY run (r2) — result

Herdr 0.9.5 (stable, protocol 22). Named disposable session `mlrec-close-r2` (created and deleted by this run). Isolated v11 Claude config (`p1-iso`, authenticated by the account owner's interactive login). This work did not modify production files; two of them (`settings.json` and the v9 hook) were hash-checked before and after, and no other production file was checked. The default Herdr server was not stopped or restarted: the default session shows `running: true` before and after (`baseline/sessions.json`, `post/sessions.json`), and no restart command was run against it. Times are UTC, 2026-11-04.

Agent starts: **1**. The first start returned `agent_not_ready` (`run/03-agent-start.err`). The trust dialog was then on screen (`run/04-trust-screen.txt`); it was resolved with `down`, verified on screen (`run/06-trust-selection.txt`), then `enter` (`run/07-*`). No second start was made.

## Verdict (for review)

- **(a) Detach and reattach of the agent's terminal, with output generated during the detached interval:** observed. The stamped observer frames show the answer rendering at 23.93–24.60 s, inside the detached window (17.31–28.58 s). The reattach capture (`run/18-pty-attach-2.bin`) shows the completed output. The reattach capture is a viewport snapshot, not a stream, so this is not evidence of live streaming to the attach client.
- **(b) Restoration after a clean named-server restart without operator `--resume`:** observed on v11. Same session `75971b1d-76df-47af-9603-291d3e9acb6d`; restored argv `claude --resume 75971b1d-…`; the named server is its grandparent. The mechanism is not proven: the server log records the restore but not the argv or the sender.
- **(c) Process survival across detach:** the claude process (pid 2089091, start time 475608787) kept the same identity from 17.8 s through 24.8 s, the detached window (`run/proc-poll.txt`), and a prompt sent while the client was detached was answered.
- **Cleanup:** `/exit` sent through the pane interface, both sends exit 0. Before the server stop, one identity poll of the restored claude pid (2095038) reported `GONE_OR_REUSED`, and the post-exit pane state shows no agent and a bash foreground. Per-second polling was not run (see Cleanup). Server stop exit 0; session deleted.
- **Production isolation:** stable identifiers identical before and after. Ephemeral changes reported separately (below).

Recommended gate verdict for CORE_RECOVERY_READY: **PENDING_INDEPENDENT_REVIEW** until a fresh review of this corrected revision. Manual Core release stays **BLOCKED**.

## Timeline (detached interval)

| Time (UTC) | Event | Evidence |
|---|---|---|
| 02:45:07.8 | Stamped observer started on `term_65f6a5b5d6bcb1` (`timeout 240 herdr --session mlrec-close-r2 terminal session observe <term>` piped through `run/stamp.py`); process poller started | `run/observer-stamped.txt`, `run/proc-poll.txt` |
| 02:45:14.235 | Attach 1 started (`pty_attach_once.py`, `recovery/cycle2/`, 3 s read) | `run/13-pty-attach-1.stdout` |
| 02:45:17.311 | `ctrl+b q` detach sent | `run/13-pty-attach-1.stdout` |
| 02:45:17.313 | Attach 1 exited, code 0; client detached | `run/13-pty-attach-1.stdout` |
| 02:45:17.318 | Timestamp taken after attach 1 (no prompt yet) | `run/13-detached-window-start.txt` |
| 02:45:21.523 | Output-producing prompt sent while detached (count 1–40, then done marker) | `run/14-detached-prompt-sent.txt` |
| 02:45:21.544 | First observer frame after the send: the prompt echo (contains the marker text) | `run/19-observer-timeline.txt` seq 3 |
| 02:45:23.930–24.600 | Observer frames 15–23 carry the answer, decoded text includes `…3940 DETACHED-WINDOW-DONE`; frame 23 at 24.600 still contains the marker | `run/19-observer-timeline.txt` seq 15–23 |
| 02:45:24.229 | Prompt returned, agent `idle`, same session | `run/15-detached-prompt.json`, `run/16-detached-prompt-returned.txt` |
| 02:45:28.579 | Attach 2 started | `run/18-pty-attach-2.stdout` |
| 02:45:32.638 | Second `ctrl+b q` sent | `run/18-pty-attach-2.stdout` |
| 02:45:32.639 | Attach 2 exited, code 0; reattach capture contains the done marker (`done_marker_in_bin True`) | `run/18-pty-attach-2.stdout`, `run/18-pty-attach-2.bin` |

Notes on the timeline:
- The prompt text contains the done marker, so the marker's first appearance (21.5 s) is the prompt echo, not the answer. The answer begins at frame 15 (23.930 s).
- The number check (1 to 40) is taken from the transcript, `run/43-recall-transcript.txt` lines 17–56, which shows the full sequence and the marker. The frame-level check is not used as evidence of the numbers.
- The detached interval before the prompt was about four seconds (17.313 to 21.523) with no prompt sent.
- The token is not in the reattach capture. The attach capture shows the current viewport; the token is earlier in the transcript.

## Restoration (b)

- Pre-stop: agent `idle`, session `75971b1d-…`, terminal `term_65f6a5b5d6bcb1` (`run/21-pre-stop-agent.json`, `run/23-pre-stop-process.json`).
- Clean stop: exit 0 (`run/27-stop.exit`). The pid 2089091 check after the stop reports `PID_GONE_AFTER_STOP` (`run/29-post-stop-process.txt`); the poller shows it gone from 02:45:56.935 (`run/proc-poll.txt`).
- Restart with no agent arguments (`run/31-restart-command.txt`): status running, `restart_needed: no` (`run/33-restart-status.txt`).
- Restored agent: `idle`, `interactive_ready: true`, same session `75971b1d-…`, new terminal `term_65f6a614c8db11` (`run/35-post-restart-agent.json`).
- Restored process: `claude --resume 75971b1d-…`, pid 2095038, parent bash 2094749, grandparent `herdr --session mlrec-close-r2 server` 2094659 (`run/37-restored-process.txt`). This scan matched only processes whose argv starts with `claude --resume`; `run/46-pre-exit-process-info.json` lists the same pid, 2095038, as the foreground process before exit.
- Environment: `run/38-restored-env-check.txt` reports `CLAUDE_CONFIG_DIR` matching the isolated config for pid 2095038 (count 1; variable names and match count only, no values saved). The first agent's environment (pid 2089091) was not captured in this run.
- Server log restore lines: `session restore evaluated ok, workspaces=1`; pane 1 respawned; agent changed to Claude with pgid 2095038 (`run/39-server-log-restore-lines.txt`). The server log does not record the argv or the sender.
- Session file field names only (`run/25-session-json-field-names.txt`): `active, collapsed_space_keys, selected, sidebar_section_split, sidebar_width, version, workspaces`. Contents not saved.

## Recall (exact token)

- Token written to `run/token.txt` (length 20) before the establishment prompt. The file has no timestamp of its own; the write happened in the command that precedes the prompt, and `run/10-token-prompt-sent-at.txt` holds the send time (02:44:56.506) and return time (02:44:58.914), in that order.
- Token-establishment prompt: exit 0, reply `● OK`, the transcript contains the token (`run/10-*`, `run/11-*`).
- Recall prompt: marker `RECALLR2_…` (`run/40-recall-marker.txt`); the prompt text contains **0** token occurrences (`run/41-recall-prompt-text.txt`). Exit 0 (`run/42-recall-prompt.exit`).
- Transcript: marker found; token after the marker in the reply, on reply line 63, which follows the marker line; total token occurrences 2 (`run/43-recall-transcript.txt`): once in the earlier echoed prompt, once in the reply.

## Cleanup (exit before stop)

- Pre-exit identity: pid 2095038, state S, pgrp 2095038, start time 475618827, no children (`run/44-pre-exit-identity.txt`). Agent and pane state captured (`run/45-*`, `run/46-*`).
- `/exit` sent with `pane send-text w1:p1 "/exit"` (sent at 02:46:44.207 per `run/47-exit-sent.txt`) and `pane send-keys w1:p1 enter`, both exit 0 (`run/48-*`, `run/49-*`).
- Identity check: one poll, taken 3.35 s after the send at 02:46:47.558, reported `GONE_OR_REUSED` (`run/50-exit-polls.txt`). `run/51-exit-result.txt` shows `elapsed=0s` because the loop's timer started after the first check, not from the send. This single poll does not distinguish "gone" from "reused pid". It is not the per-second polling the plan specified (PLAN.md section 1, step 8), and it was not run.
- Post-exit corroboration: `agent get close-agent` returns `agent_not_found` (`run/52-*`); the pane's foreground process is `bash` (`run/54-post-exit-process-info.json`, `run/55-post-exit-summary.txt`).
- The restored claude process is not a child of the test shell, so its own exit code is not available.
- Server stop: exit 0 (`run/57-stop2.exit`); pid 2095038 absent after the stop (`run/58-post-stop-check.txt`). Session delete: `deleted: true`; only `default` running (`run/59-session-delete.json`, `run/60-sessions-after-delete.json`).
- No process with the isolated `CLAUDE_CONFIG_DIR` remains at the post check (`post/isolated-config-process-count.txt`: 0).

## Production isolation (Part C)

- Stable identifiers identical before and after: workspaces, tabs, agents (name, pane, kind, session), session list, and panes `w1`, `w3`, `w5`, `w6` (`post/comparison.txt`).
- Ephemeral changes, reported separately and not used as gate inputs:
  - `focused`: the focused workspace, tab and pane moved between `w1` and `w3`. The cause is not recorded: no command log for this run is saved in the repository.
  - Default `w1:p1` `scroll.max_offset_from_bottom`: 397 → 405. This repeats the historical pattern (1235 → 1239 in `integration-upgrade/final/`). The account owner's authorization for this run states the historical change is accepted as unexplained transient metadata; that authorization is in the conversation record, not in a repository file. This run does not establish whether the change was caused by the test, and it is not evidence of proven isolation.
- Production hash check: both files OK before and after (`baseline/prod-hashes.txt`, `post/prod-hashes.txt`).
- The production pane's content was not read (no `pane read` against the default session); metadata commands only.

## Limits and untested items

- Crash recovery: not tested.
- Sidebar TUI: not tested.
- Launch attribution: not proven; inferred from ancestry only.
- Up-to-date production integration (v9 in production): not tested; the isolated config is v11.
- Single case: one agent, one detach window, one clean restart, one exit.
- Exit: one identity poll, plus post-exit pane state; per-second polling not run; the restored claude's own exit status not captured.
- Process poll granularity is 1 s.
- The observer is a separate subscription, so the frames show output generated in the window. The attach client's own reception of that output is shown only by the reattach viewport capture.

## Files

- `baseline/` — pre-test inventories and production hash check.
- `run/` — command outputs, exit codes and timestamps as listed above. `run/stamp.py` and `run/analyze_detached.py` are the analysis helpers; `run/observer-stamped.txt` and `run/proc-poll.txt` are the raw captures. The attach driver is `reports/herdr-live/recovery/cycle2/pty_attach_once.py`.
- `post/` — post-test inventories, `comparison.txt`, production hash check, isolated-config process count.
