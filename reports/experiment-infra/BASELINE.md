# Experiment Infrastructure — Phase 0 baseline

Date: 2026-11-04. Host: Ubuntu 20.04.6, kernel 5.4.0-216-generic, systemd 245.

## Repository state

- Reviewed PR #2 head: `d3742661073ec126c84bd30c1e6ecc6bbcb94ab4` (branch `fix/p0-reliability-remediation`, draft, not merged).
- Development branch: `feat/experiment-infrastructure-p1`, created from that head in worktree `/mnt/ssd/genos2/workspace/herdr-ml-expinfra`.
- Working tree at baseline: clean (the baseline log below is the only new file, committed with this document).
- Main checkout `/mnt/ssd/genos2/workspace/herdr-ml-p0` is unchanged and remains on PR #2's branch.

## Interpreters

| Interpreter | Path | Version | Status |
|---|---|---|---|
| Python 3.12 | `/mnt/ssd/genos2/.local/bin/python3.12` | 3.12.14 | used for baseline |
| Python 3.11 | not installed on host | — | NOT TESTED |
| Python 3.10 | not installed on host | — | NOT TESTED |
| System python3 | `/usr/bin/python3` | 3.8.10 | not used as a supported baseline |

Only Python 3.12 is available to test with. Python 3.10 and 3.11 remain untested on this host.

## Test suite

Command (documented in README.md): `python3 -m unittest discover -s tests -v`, run with the 3.12 interpreter above.

- Exit code: 0.
- Result: `Ran 120 tests … OK`.
- Raw log: `reports/experiment-infra/baseline-raw/py312-unittest.log`.

The log also contains expected error-path messages from tests (for example "Official run requires --approve-official"); these are test output, not failures.

## Existing execution gates

`researchctl.py gate-status` requires `--project`; it was not run without one, to avoid guessing a project name. The documented holds in `reports/reviews/2026-11-03-manual-core-continuation.md` remain in force: RUN_OWNERSHIP, CONTAINMENT, QUEUE, PROVENANCE, APPROVAL and EXPERIMENT_RECOVERY are BLOCKED. Official execution, queue dispatch, and GPU work are disabled.

## Queue preflight

- `pueue` and `pueued` are not on `PATH`.
- A pinned Pueue 4.0.4 pair exists at `/mnt/ssd/genos2/workspace/herdr-ml-pueue-test/bin/` (`pueue-x86_64-unknown-linux-musl`, sha256 `c1b10d7e…23fcb51`; `pueued-x86_64-unknown-linux-musl`, sha256 `5afeff6a…60390`). Both report version 4.0.4. These are the binaries used by the existing isolated CPU harness and are the candidates for Phase 3.
- No production Pueue daemon was queried or touched.

## Containment preflight (read-only)

`scripts/containment_probe.py` (exit 2, verdict BLOCKED), saved as `baseline-raw/containment-probe.json`.

- Current cgroup v2 boundary: `/sys/fs/cgroup/unified/system.slice/teleport.service`, owned by root, not writable by this user.
- `cgroup.kill`: absent at the boundary.
- `cgroup.procs`: not writable.
- User systemd bus: not reachable (`XDG_RUNTIME_DIR=/run/user/5011` has no bus socket; `systemctl --user` fails).
- Hybrid hierarchy: a cgroup2 mount at `/sys/fs/cgroup/unified`, and cgroup v1 controllers elsewhere.
- Kernel 5.4 and systemd 245 do not provide `cgroup.kill`.

Consistent with the previous CONTAINMENT blocker. No privilege was used. No cgroup was modified.

## Doctor

`researchctl.py doctor` reports `herdr` at `/mnt/ssd/genos2/.local/bin/herdr`, the herdr server reachable, `uv`, `nvidia-smi`, `docker` present, and `pueue`/`pueued`/`mlflow` not on `PATH` (optional). The GPU presence is not used as an execution gate.

## Baseline conclusion

- The existing 120 CPU tests pass on Python 3.12.14 at the reviewed head.
- No Experiment Infrastructure gate is PASS. Phase 0 claims no gate PASS.
- CONTAINMENT is BLOCKED by the host, not by code.
