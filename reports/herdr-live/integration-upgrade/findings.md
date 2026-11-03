# Claude integration upgrade — read-only findings (stop point)

Herdr 0.9.5. Pane `w5:p2` verified (`HERDR_ENV=1`). Code at `3bc21e1`.

## Installed integration
- `herdr integration status` (saved): claude `outdated (v9 < v11)`, hook `/mnt/ssd/genos2/.claude/hooks/herdr-agent-state.sh`.
- The installed hook header (`prod-hook-header-v9.txt`) declares `HERDR_INTEGRATION_VERSION=9`.
- The v9 hook handles only the `session` action and only `SessionStart`. It reports `pane.report_agent_session` with `agent_session_id`, `agent_session_path` and `session_start_source`. It sends no resume command (`resume_argv`).

## Herdr-side requirements (from the binary, `strings`; not from docs)
- Default config (`herdr-default-config.toml`): `[session] resume_agents_on_restore` (commented, default on). Comment: "Requires official integrations that report session refs."
- Binary contains `enforce_agent_version`, `full_lifecycle_hook_authority`, `session_ref_from_report`, `parse_integration_version`. The exact minimum version for native restoration is NOT stated in any saved file. It was not confirmed.
- Binary contains `CLAUDE_CONFIG_DIR` in the Claude integration mapping. The install path honoring it was not verified by running an install.

## Why the earlier restore was inconclusive
- Cycle 2 restored the agent after a clean server stop with v9. Herdr recorded `agent_session` for the pane. The relaunch mechanism was not captured, so it is not known whether the native `session_ref` path or a fallback did it.

## Isolated-configuration upgrade: STOPPED before install
- Credentials live in `$CLAUDE_CONFIG_DIR/.credentials.json` (existence and mode checked; contents not read). A new empty isolated `CLAUDE_CONFIG_DIR` has no login.
- A test agent using the isolated config would need the production credential file copied or linked. The operator instruction forbids that without explicit authorization. So the isolated test cannot authenticate without authorization.
- Production `settings.json` and `herdr-agent-state.sh` hashes were captured (`prod-claude-hashes-before.txt`) and were unchanged at this check. Note that `settings.json` mtime is 18:14, before this run's first command; this session did not write it.
- No install was run. No production hook was changed. No default-server restart. No agent started.

## Gate status
- CORE_RECOVERY_READY stays OPEN (PENDING). Native restoration with a current integration is not demonstrated.

## Decision (Option 2)
- No credential link, copy or reuse. No login performed. No install. No test agent. Production v9 hooks unchanged (`sha256sum -c` OK at this check).
- Isolated authentication procedure documented, not run: `isolated-auth-procedure.md`.
- CORE_RECOVERY_READY = PENDING; Manual Core release = BLOCKED.

## Restoration limitations, by category
- Clean restart: tested. Clean `server stop` and start of a disposable named session with v9 hooks, cycle 2 (`cycle2/`). Agent came back idle with the same session ID and recalled the token. Not tested: up-to-date integration.
- Explicit Claude `--resume`: tested. Round 2 (`recovery/`): operator passed `--resume <id>` to `agent start` after a clean exit. Recall passed. This is manual and is not restoration.
- Herdr-native restoration: partially observed. Cycle 2 restored without an operator `--resume`, but the relaunch mechanism was not captured, the integration was outdated (v9 < v11), and `resume_argv` was absent. Whether this went through the native `session_ref` path is unknown. Not verified as supported.
- Server-crash recovery: NOT tested. Only clean `server stop` was run. A crash or kill of the named server has no evidence.
