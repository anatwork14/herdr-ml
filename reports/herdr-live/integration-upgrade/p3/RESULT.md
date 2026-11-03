# P3 result — native restoration qualification (isolated Claude v11)

Herdr 0.9.5. Named server `mlrec-p3` only; default server never restarted. Isolated config `CLAUDE_CONFIG_DIR` (not reproduced here; path recorded in the session notes). Agent `p3-agent` (Claude), workspace `w1` in `mlrec-p3`, pane `w1:p1`.

## What was demonstrated
- Authentication in the isolated config: `claude auth status` → `loggedIn: true` (`claude.ai`). No credential file read or copied.
- Agent process and restored process both had `CLAUDE_CONFIG_DIR` equal to the isolated dir (environment check of the process, variable only).
- Session reference from the v11 integration: `79cab582-a6fc-4303-8424-469d60da4ac4` (`run/07-agent-ready.json`, `run/13-agent-before-stop.json`).
- Clean stop of `mlrec-p3` (exit 0, `run/15-stop.*`). No liveness capture of the agent process was taken before the restart.
- Restart of `mlrec-p3` with no agent arguments (`run/16-*`; the restart's stderr contains a "did you mean to open the Herdr TUI?" message and its exit code was not saved). No operator resume is recorded in any file under `run/`; the relaunch command itself was not captured. The same session ID came back (`run/17-agent-after-restart.json`), pane `w1:p1`, workspace `w1`.
- Restored process: `claude --resume 79cab582-…`, parent bash pane shell, grandparent `herdr --session mlrec-p3 server` (`run/18-restored-process.txt`).
- Server log after restart: "session restore evaluated … workspaces=1", pane respawned, "agent changed … Claude" (`run/20-server-log-after-restart.txt`).
- Restored transcript shows the earlier prompt and the reply `OK` (`run/21-restored-transcript.txt`), i.e. conversation history was restored with the session.

## What was NOT demonstrated
- Token recall. The single authorized prompt was sent with an EMPTY token, because `run/token.txt` was never written (defect recorded in `run/10-turn1-DEFECT.md`). No unique token exists in the conversation, and a second prompt was outside the authorization. So "a new prompt recalls the token" is not met.
- Launch attribution. The server log does not record the restored argv, and no saved record shows who or what sent the resume command into the pane shell. The process chain shows the server as ancestor and `--resume` in argv, and `pane get` shows `resume_argv` and `restore_error` absent (`run/19-pane-after-restart.json`). Attribution to Herdr's native mechanism is therefore INFERRED, not captured.
- Session-reference records: the server log has no explicit session-reference entry; the reference was seen in `agent get` only. The v11 hook's report content was not captured directly.
- Crash recovery (only a clean stop was run). Up-to-date integration restore beyond this one run. Sidebar TUI.

## Production and cleanup
- Production Claude `settings.json` and v9 hook: the file `prod-claude-hashes-before.txt` is a pre-test record and has no after-test entry. Its hashes match the current files (checked after the test), which shows the state at the time of checking, not a recorded after-test snapshot.
- Before/after inventories: `before/` and `after/`. Stable identifiers unchanged (`after/comparison.txt`: stable diffs 0 for all files). Transient differences listed there and unexplained (completion counters, scroll/viewport sizes, terminal title in `w5`).
- Cleanup: agent exited with `/exit`; `mlrec-p3` stopped (exit 0) and deleted (`run/25-delete.json`); only `default` remains.

## Verdict
- CORE_RECOVERY_READY: PENDING. A clean stop and restart kept the same session ID and restored the transcript. Token recall is not demonstrated. Relaunch attribution is inferred. Crash recovery is untested. The restart is evidence for a clean stop/restart sequence only.
- Manual Core release: BLOCKED.
