# P0 integration status — PR #2

Implementation SHA: `ae2b02eb930e56cb245d6afa93138f32cd50869c`. A later commit may contain only this report and review evidence; check PR head for its exact SHA. Original reviewer report remains unchanged, SHA-256 `ab8e6853b0c1ddb026580a6423bb43737db2e693753becba4e948ad4dc603d09`.

| Component | Verdict | Evidence and remaining requirement |
|---|---|---|
| Code correctness | BLOCKED | 105 CPU-only tests pass on Python 3.10.21 and 3.12.14. Opus reviewed `c1c3d89` and its artifact delta, and Luna/fallback reviewers checked subsequent scoped fixes; a complete independent Opus review of this implementation SHA is unavailable due to provider quota. The safety holds remain. |
| Real Herdr functionality | BLOCKED | Mock reconciliation covers partial operations, parent guards and competing state paths. No live adapter or server-atomic guard has been verified. The caller is outside Herdr and the installed skill requires stopping before session control. Run the worker inside a genuine Herdr-managed pane and use a dedicated isolated session to discover and verify the actual API and ownership contract. |
| CPU experiment execution | BLOCKED | Local diagnostic commands run, but arbitrary argv is trusted-user code and CPU-only execution is not enforced. Without verified descendant containment, every overall run state is UNKNOWN even when its leader succeeds; the result is not a complete experiment record. |
| GPU process containment | BLOCKED | A real isolated CPU test demonstrates that a detached child can escape process-group killing. The current user's cgroup v2 membership is not writable, lacks `cgroup.kill`, and the user systemd bus is unavailable. A host operator must provide a properly delegated cgroup v2 service/scope or an equivalent site-supported containment boundary; see `scripts/containment-provision-plan.sh` and `docs/CONTAINMENT.md`. Do not change system settings or lift the hold based on a writable path alone. |
| Queue and recovery | BLOCKED | Earlier isolated Pueue CPU tests exercised sequential tasks and daemon restart; the researchctl queue path remains blocked and run ID/task ID/worker identity binding, safe crash recovery and containment-verified slot release are not implemented. A real integrated CPU lifecycle test is required after containment is available. |
| Provenance and approvals | BLOCKED | Signed envelopes and diagnostic tree inventories verify named bytes at a point in time. They do not bind a process to immutable source, data, splits, environment and evidence, or enforce an authenticated run-bound approval and independent reviewer policy. Provision operator-controlled trust anchors and implement/test the execution boundary before official use. No scientific correctness, OOD validity or model quality is certified. |
| Independent review | BLOCKED | Opus 5.5 produced static reviews of `c1c3d89` and `e1e62f1` delta, identifying a conditional identity issue and other fixes. A later full delta request was rejected by the provider's five-hour usage limit. GPT-6 Luna and other independent fallback reviews checked scoped subsequent changes; they are not Opus approval or full-system certification. Requests, failures and reports are in `reports/p0/final-integration/`. |
| Overall server readiness | BLOCKED | The above integration, trust, containment and review prerequisites are unresolved. PR #2 remains draft; official, queue and bootstrap holds are unchanged. No GPU workload was launched. |

## Verification

```sh
/mnt/ssd/genos2/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10 -m unittest discover -s tests -v
/mnt/ssd/genos2/.local/bin/python3.12 -m unittest discover -s tests -v
bash -n scripts/*.sh
python3.10 scripts/containment_probe.py
bash scripts/containment-provision-plan.sh
```

At the implementation SHA above, the first two commands passed 105 CPU-only tests; logs are `reports/p0/final-integration/python310-final.log` and `python312-final.log`. The containment probe exits 2 with BLOCKED by design and performed no cgroup mutation. A recheck on 2026-11-03 again found the current cgroup v2 path non-writable, `cgroup.kill` absent and no reachable user systemd bus. The plan script prints a reviewable handoff and executes no privilege escalation. The documented cgroup interfaces follow [systemd delegation guidance](https://systemd.io/CGROUP_DELEGATION/) and the [kernel cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html); actual site capabilities must be verified inside the worker's delegated unit.

PID and mount-namespace observations are advisory snapshots and never authorize kill, reclaim, retry or slot release. Artifact inventories reject unsafe file types and apply resource bounds, but are not atomic multi-file snapshots. The Herdr mock requires an adapter-asserted atomic parent guard; independent tests show that a dishonest no-op guard still permits the historical race. No real server support for that guard is claimed.
