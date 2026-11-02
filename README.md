# Herdr ML — single-server AI/ML Research OS

A **server-only**, SSH-first research workspace layer built around **Herdr TUI**. One research project = one Herdr workspace; each workspace has agents, data, experiments, runs, logs, GPU, reviews, Git, research, and operations tabs.

> **Status:** implementation starter / integration pending. This repository **does not** claim that Herdr, a GPU, agent CLIs, MLflow, or Pueue are installed or running on your machine. No remote server was accessed.

## Architecture

- **Herdr TUI** — persistent terminal workspaces, tabs, panes and interactive agent management.
- **Codex / Claude Code / Hermes** — independently configurable agent runtimes; model nicknames are **roles**, not Herdr kinds.
- **Global Pueue queue** — serialized single-GPU heavy jobs; CPU tasks can run in parallel.
- **MLflow** (optional) — model metrics, parameters, artifacts and experiment comparison.
- **Python stdlib run ledger** — immutable run IDs, metadata, stdout/stderr logs, exit status and gate enforcement.
- **Git worktrees + independent reviews** — isolated worker branches and evidence-based scientific gates.
- **Optional future integration** — DVC, Grafana/Prometheus, Discord notification bridge, self-hosted plugins.

## Quick start

Prerequisites: Linux server, SSH, Python 3.10+, git, jq and **Herdr TUI**. For GPU: working NVIDIA driver/CUDA runtime. Install Herdr from the [official site](https://herdr.dev/tui/docs/install/), review the install script first, and start or attach once by running Herdr.

~~~bash
git clone https://github.com/anatwork14/herdr-ml.git
cd herdr-ml
python3 -m unittest discover -s tests -v
python3 scripts/researchctl.py doctor
~~~

**Configure projects before bootstrapping.** Edit [config/projects.json](config/projects.json). It contains placeholder checkout locations; the bootstrap will **not clone or overwrite research repositories**.

~~~bash
# In a second SSH shell, after Herdr server is running:
RESEARCH_ROOT="$HOME/research" bash scripts/bootstrap-herdr.sh
herdr

# Optional shared queue (Pueue/pueued must already be installed/running):
bash scripts/setup-gpu-queue.sh

# Offline diagnostic sample without GPU:
python3 scripts/researchctl.py init-run --project fl-iot --mode diagnostic \
  --dataset-version sandbox --split-hash test-split --seed 7 \
  -- python3 -c 'print("diagnostic only")'
~~~

The diagnostic command **creates and executes** a local CPU process unless you supply --queue; do not substitute training commands without approval. Run files are stored outside Git at RESEARCH_ROOT/.runtime/<project>/runs/.

## Official runs vs diagnostic runs

- Diagnostic runs are explicitly unvalidated and allowed for pipeline debugging.
- Official runs require DATA_READY, BASELINE_READY and GPU_READY evidence in the project gate manifest. The runner fails closed if any required gate is missing or invalid.
- An automatically produced technical record is **not** independent scientific certification.

Useful commands:

~~~bash
python3 scripts/researchctl.py gate-status --project fl-iot
python3 scripts/researchctl.py status --project fl-iot
python3 scripts/researchctl.py doctor
~~~

See [Operations](docs/OPERATIONS.md), [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md), [Roadmap](docs/ROADMAP.md), and [Research policy](RESEARCH_POLICY.md).

## Design constraints

Server-first, user-space friendly, no Kubernetes, no externally exposed management ports, and no automatic training/merges. All expensive or destructive work is an explicit operator action. No AI CLI, Docker daemon or GPU is a runtime dependency for documentation, tests, and diagnostic CPU execution.

Herdr API references: [CLI](https://herdr.dev/tui/docs/cli-reference/) · [Agent automation](https://herdr.dev/tui/docs/agent-automation/). Exact runtime behavior is version-dependent; scripts must be tested on your target Herdr version.
