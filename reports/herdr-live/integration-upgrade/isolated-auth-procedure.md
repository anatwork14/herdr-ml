# Isolated Claude authentication procedure (documented, NOT performed)

Purpose: a future test agent that uses an upgraded integration without touching the production `~/.claude`. This procedure has not been run. Running it, installing upgraded hooks, or starting a test agent needs separate authorization.

1. Create an empty config directory outside the production tree, for example `$SCRATCH/claude-config-iso` (mode 700). Do not copy, link or reference `/mnt/ssd/genos2/.claude/.credentials.json` or any other production credential file.
2. Start Claude with that directory only, and complete Claude's official interactive login in the user's own terminal (`! claude` style, or the user runs it). The login writes its credentials into the isolated directory.
3. Verify isolation before any install: `CLAUDE_CONFIG_DIR=<iso> claude --version`; confirm the production `settings.json` and hook hashes in `prod-claude-hashes-before.txt` still match.
4. Install the upgraded integration only with `CLAUDE_CONFIG_DIR=<iso> herdr integration install claude`. Check afterwards that only files under `<iso>` changed (compare the production hashes again). Do not run this if the install's effect on the config directory is not confirmed from the output.
5. Remove the isolated directory when the test is finished.

Known gap: the install path honoring `CLAUDE_CONFIG_DIR` was not verified by running an install. Step 4's post-install hash check is what would verify it.
