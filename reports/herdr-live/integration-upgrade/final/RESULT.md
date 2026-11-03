# Final isolated recovery qualification — result

Herdr 0.9.5 (stable, protocol 22). Isolated Claude config: the `p1-iso` config (v11 hook, authenticated by the account owner's interactive login in P2). Production `/mnt/ssd/genos2/.claude` was not read, changed, or linked. The default Herdr server was not stopped or restarted. Named disposable session: `mlrec-final`, deleted at the end.

## Verdict

- **Clean named-server restoration (one case): demonstrated** on this run only.
- **CORE_RECOVERY_READY: PENDING.** It may move to PASS only after an independent review confirms the restoration evidence below. Crash recovery and sidebar TUI were not tested.
- **Manual Core release: BLOCKED.**

## What was demonstrated

| Step | Evidence | Result |
|---|---|---|
| Token written before any prompt | `run/token.txt`, `run/08-prompt-token-length.txt` | nonempty, length 25 |
| Token-establishment prompt, exactly one | `run/09-token-prompt.exit` | exit 0, idle, same session |
| Token acknowledged | `run/10-transcript-after-establish.txt` | prompt contained the token; reply `● OK` |
| Clean stop of `mlrec-final` | `run/15-stop.exit`, `run/16-post-stop-process.txt` | exit 0; agent process gone |
| Restart with no agent arguments and no operator `--resume` | `run/17-restart-command.txt`, `run/19-restart-status.txt` | command was `herdr --session mlrec-final server`; server running, `restart_needed: no` |
| Agent restored, same session | `run/20-post-restart-agent.json` | `idle`, session `2d592efa-10f5-41dc-a7df-c0a3b08aeb7e`, new terminal `term_65f622e9058191` |
| Restored process argv and ancestry | `run/21-restored-process.txt` | `claude --resume 2d592efa-…`; parent `bash` (pid 356269); grandparent `herdr --session mlrec-final server` (pid 356210); `CLAUDE_CONFIG_DIR` match 1 |
| Server log, restore lines | `run/23-server-log-restore-lines.txt` | `session restore evaluated ok, workspaces=1`; pane 1 respawned; agent changed to Claude with pgid 356542 |
| Pane metadata after restore | `run/24-post-restart-pane.json` | `agent_session` source `herdr:claude`, value `2d592efa-…`; no `restore_error` or `resume_argv` field |
| Recall prompt, exactly one, marker only | `run/26-recall-prompt-text.txt`, `run/27-recall-prompt.exit` | prompt text contains 0 occurrences of the token; exit 0, idle, same session |
| Continuity | `run/28-recall-transcript.txt` | marker found; the token appears after the marker in the reply (`● MLFINAL_…`); total token occurrences 2 |

## Cleanup and production isolation

- Agent exit: `/exit` sent to pane `w1:p1` (`run/29-exit-text.json`, `run/30-exit-enter.json`; no `.exit` files were saved for these two calls). `after/isolated-config-process-count.txt`: **0** processes still have the isolated `CLAUDE_CONFIG_DIR`. (`run/31-agent-exit-check.txt` used a cwd pattern that was not validated; the environment-based count is the stronger check.)
- Named server stop: exit 0 (`run/32-stop.exit`).
- Named session delete: `deleted: true` (`run/33-delete.json`). `after/sessions.json` lists only `default`, running.
- Production files: `sha256sum -c` against `reports/herdr-live/integration-upgrade/prod-claude-hashes-before.txt` returns OK for `/mnt/ssd/genos2/.claude/settings.json` and `/mnt/ssd/genos2/.claude/hooks/herdr-agent-state.sh` (`after/prod-hashes.txt`).
- Inventory comparison, `baseline/` vs `after/`: workspaces, tabs, agents, sessions, and panes `w3`, `w5`, `w6` are identical. Pane `w1:p1` `scroll.max_offset_from_bottom` changed 1235 → 1239. This is a transient field; its cause is not identified and no command in this cycle targeted that pane's scroll.

## Limits and what remains inferred

- **Launch attribution is inferred, not proven.** The restored process's ancestry shows the mlrec-final server as its grandparent, and its argv is `claude --resume <id>`. The server log does not record the argv or the sender of the resume. The claim is that Herdr restored the agent after a clean restart without operator-supplied `--resume`; the mechanism is not proven.
- **Crash recovery: not tested.** Only a clean `server stop` was exercised.
- **Sidebar TUI: not tested.**
- **Single case.** One agent, one clean restart, one isolated config (v11). Not evidence of general or supported restore behavior, and not evidence about the production v9 integration.
- **Settings drift in the isolated config:** the isolated `settings.json` gained a `theme` key from onboarding; the hooks array is identical to the P1 copy (`run/00-note.txt`).
- **Exit status of the `/exit` sends** was not saved as files; the agent's absence is shown by the isolated-config process count.
- Earlier cycles are not superseded by this one. Round 2 (`reports/herdr-live/recovery/`) restarted the agent by an operator `agent start … -- --resume`, so it is not native restoration. Cycle 2 (`recovery/cycle2/`) restored after a clean stop, but its relaunch mechanism and argv were not captured, so it does not establish the claim here either.
