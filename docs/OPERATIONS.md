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

Queued execution is blocked. For diagnostic comparison, from the intended working directory run `/absolute/path/to/python3 -I /absolute/path/to/herdr-ml/scripts/runtime_identity.py > fingerprint.json`, using the exact interpreter and flags intended for the worker, and inspect the recorded executable and prefix before trusting the baseline. Compare with `/absolute/path/to/python3 -I /absolute/path/to/herdr-ml/scripts/runtime_identity.py --expect fingerprint.json` under the intended worker environment and working directory. Substitute the worker's actual flags for `-I` if different. The record includes filesystem paths, so treat it as potentially sensitive. It does not directly emit environment variables, but `sys_path` can contain PYTHONPATH entries.

This standalone fingerprint is not bound to any actual run and is not a security attestation or tamper-evident record. It includes interpreter flags, import search paths, build details and, on Linux, detected mapped libpython files; an empty libpython list only means none were detected. It does not prove daemon/job identity, capture package contents, all startup hooks, arbitrary native libraries, or the effects of environment variables. A matching fingerprint does not authorize queue execution or scientific claims.

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

## Run registry and recovery (CPU diagnostics only)

Each new run uses a per-run SQLite registry with a one-time owner token and an event history. SQLite state and committed result are authoritative; the JSON result and status are compatibility projections and can be stale after interruption. `python3 scripts/researchctl.py reconcile --project <id> --run-id <id>` is read-only. It reports UNKNOWN for claims or processes whose completion is not proven; repeat inspection does not reclaim a run. Do not delete registry files or legacy `execution.claim` directories to retry. Preserve them and investigate the original supervisor and all descendants, then submit a new run only after an operator verifies that the old workload is no longer running. Automatic reclaim and queue task-ID reconciliation are not implemented. This protocol is for a trusted single host and supported local filesystems with working SQLite locking and fsync, not a distributed lock or an authentication boundary. Queued and official execution remain blocked.

Without verified process containment, the runner records `UNKNOWN` for the overall run after any leader exit, including an exit code of zero, failure or interruption. `result.json` and the registry preserve the separate leader outcome and exit code; neither proves that detached descendants stopped or a GPU slot is safe. `UNKNOWN` is never permission to retry. Diagnostic commands whose leader exits zero return status 2 to signal the unresolved containment state. The registry records child PID, Linux boot ID, and process start ticks when available for investigation only; those values do not establish containment. The runner refuses a non-default inherited SIGCHLD disposition so that its unreaped process-group leader cannot be silently reaped by another handler.

When containment is UNKNOWN, `stdout_sha256_after_leader_exit` and `stderr_sha256_after_leader_exit` are hashes observed after the leader was reaped, not final artifact hashes. A detached descendant may continue writing during or after hashing, so these values do not prove an immutable log snapshot. Preserve logs and perform a new final hash only after containment is independently verified.

Older `stdout_sha256` and `stderr_sha256` fields in legacy records are also nonfinal and must not be treated as verified final log hashes. Hashing a log while an uncontained writer is active may read a mixture of file contents; `log_hash_observed_at` is recorded after both hash calculations and does not identify a point-in-time snapshot.
