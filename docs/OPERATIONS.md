# Operator runbook

**P0 safety hold:** workspace bootstrap, queued runs, and official runs are blocked pending reconciliation, process containment, authenticated approval, provenance binding, and independent validation. Do not use this starter for GPU workloads. Local CPU diagnostics only. The commands below describe intended future operation where applicable.

## Prerequisites and setup
Linux server (headless), SSH, Python 3.10.21 (the pinned development runtime; 3.10+ supported), git, jq, bash, Herdr TUI. Optional: Pueue/pueued, uv, MLflow, nvidia-smi, nvtop, btop. Most tools can use user space; NVIDIA driver and Docker GPU runtime may need administrator installation.

1. Inspect the host and agent dependencies: python3 scripts/researchctl.py doctor.
2. Install Herdr via its published install documentation (review script before executing).
3. Start Herdr interactively once, then detach. For supervised installations consider its headless server command and a user service.
4. Edit config/projects.json so project directories are existing Git checkouts; do not use the bootstrap as a cloning tool.
5. Run: RESEARCH_ROOT=$HOME/research bash scripts/bootstrap-herdr.sh
6. Install and authenticate agent CLIs separately; only then run herdr integration install codex/claude/hermes and check herdr integration status.
7. Find an *idle shell* pane with herdr pane list, then run herdr agent start <name> --kind codex --pane <id>.
8. Optionally initialize the one global Pueue GPU queue via scripts/setup-gpu-queue.sh.
9. Optionally install and run MLflow bound to 127.0.0.1; use SSH local port forwarding to view.
10. Test a CPU diagnostic run and inspect metadata/logs before running any GPU workloads.

## Example data flow

~~~bash
python3 scripts/researchctl.py gate-status --project fl-iot
python3 scripts/researchctl.py init-run --project fl-iot --mode diagnostic --dataset-version test --split-hash test --seed 123 -- python3 -c 'print("ok")'
python3 scripts/researchctl.py status --project fl-iot
~~~

Queued execution is blocked. Before enabling it, capture a fingerprint with `python scripts/runtime_identity.py` and compare the actual daemon-launched worker using `python scripts/runtime_identity.py --expect <fingerprint.json>`. A fingerprint alone cannot prove which interpreter a daemon uses: verify the worker inside the real daemon and preserve its output. Do not treat matching version strings as sufficient. The fingerprint covers the executable and installed package versions, not all package contents or scientific dependencies. Use fully specified argv.

## Host monitoring and troubleshooting
- herdr status server, herdr workspace list, herdr pane list, herdr agent list
- pueue status, pueue log, pueue group
- nvidia-smi, btop, df -h, du -sh $HOME/research/.runtime
- Research ledger: python3 scripts/researchctl.py status --project <project-id>
- Localhost MLflow: ssh -L 5000:127.0.0.1:5000 USER@SERVER

## Incident and recovery
- Mark interrupted/outdated runs as requiring human intervention; never call an interrupted run COMPLETE.
- On reboot confirm Herdr, queue and MLflow services are running; require operator decision before rerunning failed jobs.
- Restore artifacts from backup before investigating and preserve logs for scientific auditing.
- Stop an accidental GPU job through Pueue or the process supervisor, not by deleting the run folder.

## Retention and backups
Keep manifests and final metric summaries long term. Rotate raw logs by age/size after archiving; retain negative results. Exclude raw datasets and model weights from Git. Back up Pueue settings, MLflow metadata, run manifests and irreplaceable dataset split manifests with checksums. Test restore at intervals.

## Optional MLflow (manual service; no public binding)

~~~bash
uv tool install mlflow
mkdir -p "$HOME/research-services/mlflow/artifacts"
mlflow server --host 127.0.0.1 --port 5000 \
  --backend-store-uri "sqlite:///$HOME/research-services/mlflow/mlflow.db" \
  --artifacts-destination "file://$HOME/research-services/mlflow/artifacts"
~~~

Use a service manager and persistent storage for production; SQLite is a single-user starting point. Do not expose ports externally.
