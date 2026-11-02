# Research and AI-agent operating policy

This policy applies independently to every Herdr ML project workspace.

## Human responsibility and scope
**Owner:** authorizes expensive GPU runs, pushes/merges, changing protected data, deletion, published claims and security-sensitive actions.
**Orchestrator:** owns task graph, budget and dependency handling; may not self-certify a scientific result.
**Workers:** isolated worktree/branch per change; report exact SHA, tests, evidence, unresolved limitations.
**Independent reviewers:** inspect under read-only defaults; should not be the original author or change results to manufacture a PASS.

## Required research gates
- **DATA_READY**: source and license, immutable hashes, train/validation/test provenance, split-before-fit, exact/near-duplicate analysis, leakage risk, OOD definition, protected units, reviewer evidence.
- **BASELINE_READY**: preregistered baseline list, tuning budgets, seeds, metrics, fairness controls, expected variance/confidence intervals, reproducible environment.
- **GPU_READY**: validated command, hardware and VRAM budget, checkpoint/resume policy, smoke test, queue and resource assignment.
- **REVIEW_READY**: auditable result artifacts, independent code and scientific review, limitations, negative results and statistical treatment.

An official run is blocked by any missing gate. Diagnostic runs are permitted but MUST be clearly labeled unvalidated and must not be used as publication-grade evidence. Gate entries in this repo are illustrative only; write independently reviewed project-local evidence before using the official mode. A JSON file is not a cryptographic attestation.

## Reproducibility
Every experiment captures: run ID, project, mode, Git SHA and dirty state, exact argv, dataset version, split manifest hash, seed, timestamp, Python/OS, relevant optional GPU metadata, stdout/stderr, return code and manifest checksum. Persist additional package lock, container digest, model, training hyperparameters, artifact checksums and reviewer IDs in the project experiment report.

## Worktree and merge discipline
Workers MUST not modify the same checkout concurrently; create independent Git worktrees. Reviewer should inspect immutable SHA, not moving branches. Don't claim PASS based only on an agent-authored review. Require explicit owner approval before push/merge or destructive file/data operations.

## Resource controls
Use the one global GPU queue for heavy jobs. CPU agents can operate concurrently subject to memory/CPU budget. Never launch expensive jobs from a TUI shortcut automatically. Pueue is cooperative: outside queue processes can still consume GPU.

## Safety
Do not commit datasets, keys, tokens, private papers or checkpoints. Logs must exclude credentials. No automatic key exfiltration, unattended sudo, host Docker socket mounting or externally published dashboard. Use localhost + SSH forwarding for MLflow. Protect destructive operations with confirmations; redact logs before sharing.

## Handoffs
Worker -> Reviewer: SHA, scope, touched files, diff, tests, raw logs, blockers.
Reviewer -> Orchestrator: PASS/FAIL/BLOCKED, explicit evidence paths, known gaps.
Orchestrator -> Owner: evidence, decision required, cost/risk, rollback plan.
