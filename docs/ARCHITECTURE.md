# Server-only architecture

## Components and boundaries

SSH client -> Herdr TUI (control plane) -> project workspaces -> agent CLI panes / data panes / run and logs panes.

Separately managed persistent services:
1. Pueue daemon, global gpu0 queue, concurrency=1 across ALL projects.
2. Stdlib run ledger (scripts/researchctl.py) stored under RESEARCH_ROOT/.runtime/<project>/runs/<run-id>.
3. Optional MLflow tracking bound to 127.0.0.1, with durable metadata and artifact storage.
4. Optional future DVC/object store, Prometheus/Grafana exporter, Discord webhook bridge.
5. Git repositories stored separately from run records and datasets.

**Do not mistake Herdr pane state for an experiment state.** Agents may be idle while GPU processes run, and an agent may be working without any training. The ledger and external queue are the authoritative local job execution records. Queue visibility and experiment state should be reconciled rather than fabricated.

## Workspace layout

| Tab | Purpose |
| --- | --- |
| 00 Control | current objectives, task DAG, blockers, signoffs |
| 01 Agents | manager / workers / reviewers / handoffs |
| 02 Data | dataset provenance, quality, preprocessing and splits |
| 03 Experiments | experiment configurations, comparison and dashboards |
| 04 Runs | central queue and job status |
| 05 Logs | per-run stdout/stderr and operational events |
| 06 GPU | nvidia-smi, nvtop and resource budgets |
| 07 Review | independent code/scientific gates |
| 08 Git | git status, diff, worktrees and PRs |
| 09 Research | papers, hypotheses, novelty and ablations |
| 10 Operations | service status, backups, health and incident response |

Tabs are **real Herdr terminal tabs** but high-level descriptions are design guidance, not fully implemented graphical panels. A bootstrap creates tabs and starts only safe watchers.

## Agent topology

Owner -> Orchestrator -> Workers (implementation/data/experiments/literature). Independently: Reviewers (code/methodology/reproducibility/security) -> gate verdict -> orchestrator/owner. Configure actual models via each agent runtime; the roles file is not a runnable model adapter.

Worktrees are Herdr-managed or standard Git worktrees. A distinct review agent must not self-sign work authored by that same agent identity.

## Data and run lifecycle

DATA_READY -> BASELINE_READY -> GPU_READY -> official run -> REVIEW_READY -> release/publication claim. Missing prerequisites -> BLOCKED. Diagnostic runs bypass research claims, not safety or user approval.

A run: validate input -> write immutable submission manifest -> [optional queue] -> execute command under a process -> capture logs/result/checksums -> update state -> independent human/scientific review. Queue and run ledger never replace model-specific checkpointing.

## Failure modes

- SSH disconnect: Herdr pane state survives detach, not arbitrary machine outages.
- Machine restart: service supervisor + checkpoint recovery (not automatic rerun without operator approval).
- GPU OOM: failed run preserved; resource/batch size adjusted in a *new* run.
- Concurrent agents: separate worktrees and Git SHA provenance.
- Dataset leakage: gate fails closed, cannot silently declare OOD valid.
- Disk pressure: quotas, cleanup policy and backed-up metadata.
