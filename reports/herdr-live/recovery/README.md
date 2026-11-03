# Manual Core recovery validation — evidence index

Herdr: `0.9.5` (stable, protocol 22), server running and endpoint compatible (`post/herdr-version.txt`, `post/herdr-status.txt`).
Implementation under test: PR #2 head `b51a6b328d136dee31f67b56f7369db9b0a14bf2`. The Herdr skill check `test "${HERDR_ENV:-}" = 1` passed in pane `w5:p2`, workspace `w5`.
Scope: one disposable Claude test agent (`mlrec-agent`) in disposable workspace `w9`. No automatic bootstrap, official run, queued run or GPU work. The default Herdr server was not stopped.

## Sequence and exit codes

| Step | Command (arguments only) | Exit | Artifact |
|---|---|---|---|
| Baseline | `workspace list`, `session list`, `agent list`, `tab list`, `pane list --workspace w1/w3/w5/w6` | n/a | `baseline/` |
| 1 | `workspace create --cwd <fixture> --label mlrec-disposable --no-focus` | 0 | `run/01-*` |
| 2 | `agent start mlrec-agent --kind claude --pane w9:p1 --timeout 90000` (attempt 1) | 1 (`agent_not_ready`, trust dialog) | `run/02-*` |
| 2b | same command, attempt 2 in the same pane (attempt 1 had exited, see below) | 1 (`agent_not_ready`, trust dialog) | `run/02b-*` |
| 3 | `pane send-keys w9:p1 down` then read, then `pane send-keys w9:p1 enter` | 0 | `run/03b-*`, `run/04-*` |
| 4 | `agent prompt mlrec-agent "<random token>" --wait` | 0 | `run/05-*` |
| 5 | `pane send-text w9:p1 /exit`, `pane send-keys w9:p1 enter` | n/a | `run/06-*` |
| 6 | `agent start mlrec-agent --kind claude --pane w9:p1 -- --resume f6ae30ec-0f0c-4883-95de-74f71afa29d7` | 0 | `run/07-*` |
| 7 | `agent prompt mlrec-agent "<marker>: ... reply with the token"` | 0 | `run/08-*` |
| 8 | observer: `timeout 6 terminal session observe <terminal>`, three attaches | 124 each (timeout) | `run/10-*` |
| 9 | pty attach/detach: `tui_attach.py` (`terminal attach`, `ctrl+b q`), two attaches | 0 each | `run/11-*` |
| 10 | cleanup: `/exit` to agent, `ctrl+c` to ticker, `workspace close w9` | 0 | `run/12-*` |
| 11 | post inventory and baseline diff | n/a | `post/` |

Exit values are from the `.exit` files and the driver's JSON. The `timeout` exit 124 is the expected termination of an observer, not an error.

## Key results

- **Initial start, attempt 1 (`run/02-*`):** `agent_not_ready`, blocked at the Claude workspace-trust dialog. A later `agent read` showed the dialog with the cursor on "No, exit", and the Claude process had already exited. The cause of that exit is not confirmed; it followed a `down` key sent to the pane (`run/03-trust-down.json`).
- **Attempt 2 (`run/02b-*`):** same blocker. The cursor was on "No, exit". `down` moved it to "Yes, I trust this folder" (`run/03b-trust-down.json` and the screen read in the session). Enter confirmed it, and the agent became `idle` with session `f6ae30ec-0f0c-4883-95de-74f71afa29d7` (`run/04-agent-get-after-trust.json`).
- **Token:** the random token is in `run/token.txt`. It was given in prompt `run/05-prompt-token.json` and the agent settled to `done`.
- **Exit:** the pane printed `claude --resume f6ae30ec-0f0c-4883-95de-74f71afa29d7` (`run/06-pane-after-exit.txt`), and the agent was gone from the pane afterward.
- **Restart with `--resume`:** `run/07-restart-command.txt` (command), `run/07-restart.exit` (0), `run/07-restart.stdout` (`agent_started`, same session ID, `argv` contains `--resume`), `run/07-restart.stderr` (0 bytes), `run/07-restart-agent-get.json` (`idle`, `interactive_ready: true`).
- **Recall:** marker `run/08-recall-marker.txt`. The reply in `run/08-recall-scrollback.txt` contains the token after the marker. Check output: `run/08-recall-check.txt`: marker found and the token appears after the marker.
- **Observer (`run/10-*`):** each attach's first frame is a full snapshot (`"full": true`, `"seq": 1`). Later frames are deltas (`"full": false`, seq 2 to 7 in each capture). The delta frames redraw changed cells, such as the counter and timestamp digits. The timestamp in one delta (`11:06:59.127Z`) matches the ticker's own log line for that second. The analysis is in `run/10-observer-analysis.txt`.
- **Process survival (`run/survival.txt`, `run/ticker.log`):** the ticker wrote 124 contiguous lines at 1.00–1.03 s intervals. Each observer and TUI window contains about 5–6 ticks, including the detached gaps. This is process survival, not evidence of streaming.
- **Interactive TUI (`run/11-*`, `tui_attach.py`):** `herdr terminal attach` ran under a pty with an explicit 120×40 window size. Each attach shows the alternate-screen entry and exit (`1049h`/`1049l`) and live ticker content. `ctrl+b q` exited with code 0 within about 1 ms (`detach_sent` to `end`). The reattach is the second attach, which also shows live content.
- **Cleanup and post-state:** the agent was exited; the ticker was stopped; `w9` was closed (exit 0). `post/` matches `baseline/`: the same four workspaces (`w1`, `w3`, `w5`, `w6`) with the same pane and tab counts, the same two production agents (`w1:p1` agy, `w5:p2` claude), and only the `default` session running.

## Known failures and limits

- The two `agent start` attempts exited 1 with `agent_not_ready`. Both were caused by the trust dialog, not by the restart mechanism.
- The first attempt's Claude process exited during the trust step, so the agent was relaunched in the same pane. Only one Claude session was ever established, and that is the session used for recall.
- The first pty attempt failed with `terminal reported a zero-sized grid` because the pty had no window size. That failed run's output was overwritten by the successful run.
- No `ps`-based process check is recorded for the ticker. Survival is inferred from the pane's `process-info` (foreground `sleep` and `bash`) and the continuous timestamped log.
