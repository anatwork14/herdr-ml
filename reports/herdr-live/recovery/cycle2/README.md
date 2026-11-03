# Manual Core recovery validation — cycle 2 evidence

Herdr `0.9.5`, protocol 22. Cycle-2 start head `fbe35d41fb607e2d78f95fa928e0e7f27e43b68a`. The `HERDR_ENV` check passed in pane `w5:p2`, workspace `w5`, in-session; no file was saved for it.

Isolation design: the agent ran in a **named disposable session `mlrec-c2`** with its own server and socket (`sessions/mlrec-c2/`). Its workspace is also labelled `w1`, the same ID as a production workspace in the default session. Commands whose argv is recorded in `named/` carry `--session mlrec-c2`. The default session was not stopped, and the production server was not restarted. The named session was stopped and deleted at the end.

Caveat on the record: the complete argv log for the whole cycle was not saved. Argv is recorded only in files that store it (for example `named/08-*`, `named/10-*`) and in the report's narrative. The isolation claim is therefore limited to what the before/after inventories show (see `post/comparison.txt`).

## Agent identity

- Agent `c2-agent` (Claude), session `7ddaa64d-8a22-40dc-872c-1d5fc5dc60a8`.
- Pane `w1:p1` and terminal `term_65f5d670305601` in session `mlrec-c2`, before the restart. After the restart the terminal ID changed (see `named/14-agents-after-restart.json`).
- Token: `named/token.txt`.

## Test A — agent-pane detach and reattach

Target: the agent's own terminal `term_65f5d670305601`.

| Step | Command | Exit | Result | Artifact |
|---|---|---|---|---|
| Turn 1 (token; primes 53, 59, 61) | `agent prompt c2-agent … --wait` | 0 | `idle`; replies in scrollback | `named/06-*` |
| Observer, 40 s, turn 2 sent during it | `timeout 40 herdr --session mlrec-c2 terminal session observe term_65f5d670305601` | 124 | 10 frames: seq 1 `full=true` (contains token); seq 2–10 `full=false`; seq 9 contains `67` | `named/07-observerA.*`, `observer-A-frames.txt` |
| Detach, documented `ctrl+b q` | `pty_attach_once.py mlrec-c2 term_65f5d670305601 …` | 0 | `detach_sent` 11:18:55.116Z, `end` 11:18:55.117Z | `named/08-pty-attach-1.*` |
| Prompt while detached (turn 3) | `agent prompt … --wait` | 0 | `idle`, session `7ddaa64d-…`, answer `71` | `named/09-*`, `named/17-recall-scrollback.txt` |
| Reattach | `pty_attach_once.py …` | 0 | 9501 bytes before detach; `10-pty-attach-2.bin` contains the token and `71` | `named/10-pty-attach-2.*` |
| Continuity (recall after marker) | `agent prompt … --wait` | 0 | `marker_found=True token_after_marker=True` | `named/11-recall-check.txt` |

Limit: no output was observed during the detach window; no observer ran then. "Prompt while detached" means a prompt sent while the client was detached was answered.

## Test B — restoration after a clean named-server restart

Scope: one clean stop of the named server, on an outdated Claude integration (v9 < v11, per `herdr integration status`, which was not saved as a file). This is not evidence of general or supported restore behavior.

- `12-pane-before-stop.json`: the pane has no `resume_argv` or `restore_error` field at all.
- `13-stop.*`: `herdr --session mlrec-c2 server stop` exit 0 (`13-stop.exit`). `13-sessions-after-stop.json` shows `mlrec-c2` `running: false`.
- `14-restart-server.*` and `14-agents-after-restart.json`: after `herdr --session mlrec-c2 server` was started, `agent list` showed `c2-agent`, `w1:p1`, `claude`, `idle`, same session ID, new terminal ID.
- `15-pane-after-restart.txt`: the restored pane shows the prior transcript.
- `16-process-ancestry.txt`: a process `claude --resume 7ddaa64d-…` has parent chain bash → `herdr --session mlrec-c2 server` → init. This shows where the process sits. The relaunch mechanism and the argv Herdr used were not captured.
- `17-recall-check.txt`: `token_after_marker=True`.

Not captured: the schema text about `resume_argv`, the persisted state file contents, and whether the Claude process was ever absent during the restart window. The session directory was deleted in cleanup, so none of these can be re-read from it.

## Test C — production isolation

- `baseline/` was captured in one batch before the cycle's first command. No per-file timestamp was saved.
- `post/` was captured after cleanup. `post/comparison.txt` splits stable identifiers and operational fields from transient fields (focus, scroll, revision, seq, agent status).
- Stable: 0 differences in workspaces, tabs, agents (name, pane, kind, session), sessions, and panes `w1`, `w3`, `w5`, `w6`.
- Transient differences, not attributed: the default-session agy agent `w1:p1` `agent_status` `idle` → `done`, `completion_seq` and `state_change_seq` 177 → 180; `w1:p1` scroll 104 → 185; `w6:p1` scroll 41 → 55. The workspace and tab `agent_status` changed with the agy agent.
- The files are not byte-identical. Isolation is not established beyond the stable fields above; the cause of the transient changes is unexplained.

## Cleanup

- `c2-agent` exited with `/exit`; the `18-exit-*` outputs are empty files, so no exit capture is saved. A check for a Claude process with the fixture cwd found none.
- Named server stop at the end: `named/19-server-stop.stdout` and `.stderr` are empty, and no exit file was saved for step 19. The stop's exit status is not recorded.
- `named/20-session-delete.json`: `deleted: true`. `named/20-sessions-after-delete.json` lists only `default`, running.

## Files

- `named/` — raw outputs for the named-session agent. Argv is stored only where noted above.
- `pty_attach_once.py` — one attach and detach with the session passed explicitly.
- `observer-A-frames.txt` — decoded observer frame table.
- `baseline/`, `post/` — inventories before and after; `post/comparison.txt`.
