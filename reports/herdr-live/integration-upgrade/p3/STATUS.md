# P3 status — STOPPED at token prompt (permission denied)

Captured so far:
- Preconditions: isolated dir private (700); `claude auth status` loggedIn=true (claude.ai); v11 hook and settings match P1 copies (`run/` and `before/`).
- Production Claude settings/hook hashes: OK at each check.
- Named server `mlrec-p3` started with `CLAUDE_CONFIG_DIR` set to the isolated dir; workspace `w1` (`p3-disposable`) created with `--env CLAUDE_CONFIG_DIR`.
- Agent `p3-agent` (Claude, pane `w1:p1`, terminal `term_65f5e96bb42e31`) started; first-run theme, security notes and trust dialogs were confirmed on screen (`run/03-*`, `04-*`, `05-*`, `06-*`).
- Session reference reported by the v11 hook: `79cab582-a6fc-4303-8424-469d60da4ac4` (`run/07-agent-ready.json`).
- Agent process `pid 3765958` environment: `CLAUDE_CONFIG_DIR` equals the isolated dir (`run/08-agent-proc.txt`).

Not captured: the server session-reference log lines (the read was denied), the unique-token turn (the prompt was denied twice), the restart, the relaunch command, recall, and cleanup.

Current state: named server `mlrec-p3` running; agent `p3-agent` idle with no token sent. Production files were not changed.
