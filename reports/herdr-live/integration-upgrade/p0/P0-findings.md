# P0 — pre-install verification of `herdr integration install claude` (Herdr 0.9.5)

Method: read-only analysis of the binary's strings, then enforced isolation. Production `/mnt/ssd/genos2/.claude` was bind-mounted read-only inside a private user+mount namespace (`unshare --user --map-root-user --mount`), and `HERDR_SOCKET_PATH` pointed at a nonexistent socket so no server was contacted. Raw outputs: `P0-probeA-install.out`, `P0-probeB-fallback.out`, `P0-probeC-empty-env.out`.

## Read-only analysis
- Environment mapping for Claude: `claude` ↔ `CLAUDE_CONFIG_DIR` (adjacent entries in the binary's agent table: `.claude`, `CLAUDE_CONFIG_DIR`, `.cursor`, `CURSOR_CONFIG_DIR`, ...).
- Install write destinations, from its success messages: the hook script (`installed claude integration hook to <dir>/hooks/herdr-agent-state.sh`) and the settings file (`ensured claude settings at <dir>/settings.json`). An update failure message names `claude settings`. No other Claude write path was found in the strings.
- Strings alone did not settle the fallback rule; that was tested below.

## Enforced isolation results
- **Probe A** (`CLAUDE_CONFIG_DIR` = private empty dir, production read-only): exit 0. Writes went only to the private dir (`hooks/herdr-agent-state.sh`, `settings.json`). The read-only check confirmed production refuses writes (`Read-only file system`).
- **Probe B** (`CLAUDE_CONFIG_DIR` unset, `HOME`=`/mnt/ssd/genos2`, production read-only): exit 1, `Read-only file system (os error 30)`. The installer falls back to `$HOME/.claude`, which is production.
- **Probe C** (`CLAUDE_CONFIG_DIR=""`): same as B. An empty value is treated as unset and falls back to production.

## Rule
Writes go to `$CLAUDE_CONFIG_DIR` when it is set to a non-empty value. Otherwise they go to `$HOME/.claude`. Relative paths were not tested.

## Production safety after the probes
- `sha256sum -c prod-claude-hashes-before.txt` = OK for `settings.json` and `hooks/herdr-agent-state.sh` after each probe.
- Probes did not contact the default Herdr server.

## Decision: GO for P1, under these guards
1. `CLAUDE_CONFIG_DIR` must be set to a non-empty, absolute, fresh private directory (mode 700).
2. Run the install inside the read-only production namespace, as in Probe A.
3. Set `HERDR_SOCKET_PATH` to a nonexistent path.
4. Verify the installed version and destinations after the run, and re-check production hashes.
