# Delivery roadmap and acceptance criteria

## P0 — Workspace foundation (repository starter)
- [x] Server-only Herdr TUI architecture and project manifest
- [x] Workspace/tab bootstrap with existing-checkout safety
- [x] Agent role and research policy documents
- [x] Local diagnostics and unit-testable run ledger
- [ ] Real Herdr integration test on target server
- [ ] Authenticated Codex/Claude/Hermes agent tests on target server

Exit: bootstrap can be run twice without adding duplicate workspaces; terminal panes and agent starts proven on target host.

## P1 — ML execution
- [ ] Cooperative one-GPU queue bootstrap and runner submit support (queue setup has an isolated CPU test; submission remains blocked pending containment and integration)
- [x] Run IDs, metadata, persistent stdout/stderr, basic gate enforcement
- [ ] Validate real Pueue queue lifecycle and reboot behavior
- [ ] GPU smoke test on approved hardware
- [ ] MLflow integration; compare metrics and artifacts
- [ ] Checkpoint/resume per training engine
- [ ] GPU broker respecting memory/thermal pressure

Exit: two projects submit sequential heavy GPU runs to same queue, capture complete evidence and resume after deliberate interruption.

## P2 — Scientific governance
- [ ] Stage-gate schema and verified official mode (official execution remains blocked; signed-envelope verification is offline only)
- [ ] Independent reviewer identity and approval audit
- [ ] DVC/immutable dataset catalog and leakage audit adapter
- [ ] FL-specific DATA_READY certification
- [ ] Statistical comparison report generation
- [ ] SOTA baseline catalogs, OOD definitions and preregistration

Exit: every publication-grade run links to immutable data and approved protocol/evidence; no self-certification.

## P3 — Research automation and operations
- [ ] Optional Herdr plugins (run launcher, GPU monitor, review, experiment browser)
- [ ] Prometheus/Grafana exporters with quotas
- [ ] Discord bot (one channel/project + global control) with authenticated read-only status first
- [ ] Event alerts, approval UI, durable task DAG and project-scoped agent memory
- [ ] Backups/restores, retention and reboot supervision
- [ ] Scheduler fairness, preemption only with checkpoint awareness

Exit: operator can check per-project status remotely and recover failures safely without relying on a live SSH session.

## Non-goals for P0
Web dashboard, Kubernetes, auto-merging, arbitrary model downloading, auto-training, perfect scientific certification or kernel-enforced GPU scheduling.
