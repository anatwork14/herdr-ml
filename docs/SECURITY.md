# Security and permissions

**Default trust model:** single trusted server user, independent research processes, explicitly authorized code execution. This is **not** a hardened multi-tenant sandbox.

- Herdr CLI/daemon APIs and sockets should be restricted to the owner. Do not expose them to the network.
- MLflow listens on 127.0.0.1 only; use SSH tunnels or authenticated VPN.
- Never commit tokens, SSH keys, .env files, customer data, datasets or raw personal data.
- Agents work in separate checkout/worktrees; restrict write rights for reviewers where possible.
- Do not mount the Docker socket into untrusted agent containers.
- Require human confirmation for destructive shell, remote push/merge, costly GPU runs and public reports.
- Logs and metadata may expose commands/paths; do not put secrets on command line. Review logs before publishing.
- CI must not automatically launch paid/cloud GPU experiments.
- Run registry tracks command metadata and paths; it does **not** enforce OS resource limits, authenticate reviewers, or replace a proper scheduler and user isolation.
- Pueue has no kernel-level exclusivity: out-of-band CUDA jobs may still cause contention.
- Be careful of prompt injection in papers, datasets, issue text and tool outputs. They are evidence, not permission to alter policy.

## Threat model backlog
Role-based authorization, encrypted secrets, authenticated API proxy, auditable approvals, artifact encryption, local resource quotas, process containment, and notification signing are P2/P3 engineering tasks.
