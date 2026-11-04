# Consolidated CORE_RECOVERY run (r2) — result

Herdr 0.9.5 (stable, protocol 22). Named disposable session `mlrec-close-r2` (created and deleted by this run). Isolated v11 Claude config (`p1-iso`, authenticated by the account owner's interactive login). Production `/mnt/ssd/genos2/.claude` was not linked or modified by this work; its two hash-verified files were checked before and after. The default Herdr server was not stopped or restarted. Times are UTC, 2026-11-04.

Agent starts: **1** (the first start returned `agent_not_ready` at the workspace-trust dialog; the dialog was resolved with `down`, verified on screen, then `enter`, with no second start).

## Verdict (for review)

- **(a) Detach and reattach of agent output, with output produced during the detached interval:** observed on the agent's own terminal `term_65f6a5b5d6bcb1` (pane `w1:p1`, named session). See the timeline below.
- **(b) Restoration after a clean named-server restart without operator `--resume`:** observed on v11, same session `75971b1d-76df-47af-9603-291d3e9acb6d`, with restored argv `claude --resume 75971b1d-…` and the named server as grandparent. The mechanism is not proven; the server log records the restore but not the argv or sender.
- **(c) Process survival across detach:** the claude process (pid 2089091, start time 475608787, pgrp 2089091) was alive with the same identity throughout the detached window; a prompt sent while detached was answered.
- **Cleanup:** the restored claude process (pid 2095038, start time 475618827) was gone before the server stop, with `/exit` sent through the documented pane interface and both send exit codes 0. The server stop exit was 0, and the session was deleted.
- **Production isolation:** stable identifiers identical before and after. Ephemeral changes reported separately (below).

Recommended gate verdict for CORE_RECOVERY_READY: **PENDING_INDEPENDENT_REVIEW** until a fresh review at the commit that carries this evidence. Manual Core release stays **BLOCKED**.

## Timeline (detached interval)

| Time (UTC) | Event | Evidence |
|---|---|---|
| 02:45:07.8 | Stamped observer started on `term_65f6a5b5d6bcb1`; process poller started | `observer-stamped.txt`, `proc-poll.txt` |
| 02:45:14.235 | Attach 1 (`pty_attach_once.py`, 3 s read) started | `13-pty-attach-1.stdout` |
| 02:45:17.311 | `ctrl+b q` detach sent | `13-pty-attach-1.stdout` |
| 02:45:17.313 | Attach 1 exited, code 0; client detached | `13-pty-attach-1.stdout` |
| 02:45:17.318 | Timestamp taken after attach 1 (client detached, no prompt yet) | `13-detached-window-start.txt` |
| 02:45:21.523 | Output-producing prompt sent while detached (count 1–40, then done marker) | `14-detached-prompt-sent.txt` |
| 02:45:21.5–24.6 | Observer frames: prompt echo at 21.5; answer frames 21.9–24.5; frames stop at 24.6 | `19-observer-timeline.txt`, `20-observer-answer-check.txt` |
| 02:45:24.229 | Prompt returned, agent `idle`, same session | `15-detached-prompt.json`, `16-detached-prompt-returned.txt` |
| 02:45:28.579 | Attach 2 started | `18-pty-attach-2.stdout` |
| 02:45:32.638 | Second `ctrl+b q` sent | `18-pty-attach-2.stdout` |
| 02:45:32.639 | Attach 2 exited, code 0; reattach capture contains the done marker | `18-pty-attach-2.stdout`, `18-pty-attach-2.bin` |
| 02:45:17.8–24.8 | Process poll: claude pid 2089091 alive (state S or R), same start time 475608787 | `proc-poll.txt` |

Notes on the timeline:
- The prompt's text includes the done marker, so the marker's first appearance in the observer (21.5) is the prompt echo, not the answer. The answer frames are 21.9–24.5, identified by the frames containing `40` and by the marker at 23.9–24.4 (`19-observer-timeline.txt`). The number check is coarse: the frame check for `1..20` did not match the rendered output, and the reattach capture shows only the current viewport, not the full answer.
- The detached interval before the prompt was about four seconds (17.313 to 21.523) with no prompt and no agent output expected. The first observer frame after the send (21.544) is the prompt echo, consistent with the 21.523 send time.
- The token is not in the reattach capture. This is expected: the attach capture shows the current viewport; the token is earlier in the transcript.

## Restoration (b)

- Pre-stop: agent `idle`, session `75971b1d-…`, terminal `term_65f6a5b5d6bcb1` (`21-pre-stop-agent.json`, `23-pre-stop-process.json`).
- Clean stop: exit 0, pid 2089091 gone (`27-stop.exit`, `29-post-stop-process.txt`).
- Restart with no agent arguments (`31-restart-command.txt`): status running, `restart_needed: no` (`33-restart-status.txt`).
- Restored agent: `idle`, `interactive_ready: true`, same session `75971b1d-…`, new terminal `term_65f6a614c8db11` (`35-post-restart-agent.json`).
- Restored process: `claude --resume 75971b1d-…`, pid 2095038, parent bash 2094749, grandparent `herdr --session mlrec-close-r2 server` 2094659 (`37-restored-process.txt`). Environment `CLAUDE_CONFIG_DIR` matches the isolated config (count 1, names/values only; `38-restored-env-check.txt`).
- Server log restore lines: `session restore evaluated ok, workspaces=1`; pane 1 respawned; agent changed to Claude with pgid 2095038 (`39-server-log-restore-lines.txt`). The server log does not record the argv or the sender.
- Session file field names only (`25-session-json-field-names.txt`): `active, collapsed_space_keys, selected, sidebar_section_split, sidebar_width, version, workspaces`. Contents not saved.

## Recall (exact token)

- Token written to `token.txt` before any prompt (length 20, nonempty). Token-establishment prompt: exit 0, reply `● OK`, transcript contains the token (`10-*`, `11-*`).
- Recall prompt: marker `RECALLR2_…` (`40-recall-marker.txt`), text contains **0** token occurrences (`41-recall-prompt-text.txt`). Exit 0 (`42-recall-prompt.exit`).
- Transcript: marker found; token after the marker in the reply, on reply line 63, total token occurrences 2 (`43-recall-transcript.txt`): once in the earlier echoed prompt, once in the reply.

## Cleanup (verified exit before stop)

- Pre-exit identity: pid 2095038, state S, pgrp 2095038, start time 475618827, no children (`44-pre-exit-identity.txt`). Agent and pane state captured (`45-*`, `46-*`).
- `/exit` sent with `pane send-text w1:p1 "/exit"` and `pane send-keys w1:p1 enter`, both exit 0 (`48-*`, `49-*`).
- Identity poll: the original pid was gone at the first check (`50-exit-polls.txt`, `GONE_OR_REUSED`). The check ran a few seconds after the send, not at the send itself. The result is `exited_before_stop=true` (`51-exit-result.txt`).
- Post-exit: `agent get close-agent` returns `agent_not_found`; the pane's foreground process is `bash` (`52-*`, `53-*`, `54-*`, `55-post-exit-summary.txt`).
- Limitation: the restored claude process is not a child of the test shell, so its own exit code is not available. The evidence is that its identity was gone before the server stop, not a captured exit status.
- Server stop: exit 0; pid 2095038 absent after the stop (`57-stop2.exit`, `58-post-stop-check.txt`). Session delete: `deleted: true`; only `default` running (`59-session-delete.json`, `60-sessions-after-delete.json`).
- No process with the isolated `CLAUDE_CONFIG_DIR` remains (`post/isolated-config-process-count.txt`: 0).

## Production isolation (Part C)

- Stable identifiers, identical before and after: workspaces, tabs, agents (name, pane, kind, session), session list, and panes `w1`, `w3`, `w5`, `w6` (`post/comparison.txt`).
- Ephemeral changes, reported separately and not used as gate inputs:
  - `focused`: the focused workspace/tab/pane moved between `w1` and `w3` (the default session's focus, not changed by this run's commands; the cause is not recorded).
  - Default `w1:p1` `scroll.max_offset_from_bottom`: 397 → 405. This is a second instance of the historical pattern: the first was 1235 → 1239 in `integration-upgrade/final/`. The owner accepted it as unexplained transient metadata. This run does not show whether it is test-caused: the commands in this run targeted the named session. It is not evidence of proven isolation.
- Production hash check: both files OK before and after (`baseline/prod-hashes.txt`, `post/prod-hashes.txt`).
- The production pane was not read (no `pane read` against the default session); metadata commands only.

## Limits and untested items

- Crash recovery: not tested.
- Sidebar TUI: not tested.
- Launch attribution: not proven; inferred from ancestry only.
- Up-to-date production integration (v9 in production): not tested; the isolated config is v11.
- Single case: one agent, one detach window, one clean restart, one exit.
- Output observed during the detached interval: shown by the observer frames timeline and by the reattach capture containing the done marker; the observer is a separate subscription, so the frames show output generated in the window, not that the attach client was receiving it.
- Process poll granularity is 1 s, and the exit check's start after the send was not timestamped against the send command.

## Files

- `baseline/` — pre-test inventories and production hash check.
- `run/` — every command's output, exit code and timestamp, as above. `run/stamp.py` and `run/analyze_detached.py` are the analysis helpers; `run/observer-stamped.txt` and `run/proc-poll.txt` are the raw captures.
- `post/` — post-test inventories, `comparison.txt`, production hash check, isolated-config process count.
