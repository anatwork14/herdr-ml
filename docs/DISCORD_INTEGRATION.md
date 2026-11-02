# Optional Discord control plane (P3 design, not deployed)

Provide a notification/command gateway without opening Herdr control sockets to the Internet. Suggested Discord channels:
- #research-control — all projects, global alerts, incident escalation and owner approvals.
- #fl-iot — workspace status and research run summaries.
- #starlight-tempo — workspace status and benchmark summaries.
- #research-audit — independent reviews and gate decisions.

**Begin read-only:** /status, /runs, /agents, /gpu, /blockers, /last-review. Replies should include freshness timestamps and source run IDs. Never pretend the bot has direct GPU reservation or review authority.

Sensitive commands (/start, /stop, /merge, /delete, /approve) require strong identity binding, explicit authorization, idempotency keys, confirmation and audit logs. Do not shell-interpolate Discord text. Use a typed allowlist with project ACLs; no arbitrary remote command execution via chat.

Runtime architecture: bot process -> local authenticated broker -> Herdr CLI (status only) / run ledger (status only) / Pueue (status only); optional owner-approved action worker later. Limit output, redact secrets, retry with backoff, fail safe if provider is unavailable. Store bot tokens in secret manager or private env files, never repository files.

This file describes a future integration. No Discord bot, webhook or live credential is configured by this starter.
