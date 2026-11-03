# Preliminary independent GPT process identity review

Reviewed Git SHA `6ec6a501c48aceb9fe8c6feeb8bb07545df79e69` in a detached worktree. Opus quota was unavailable; this is a fallback review, not Opus approval. Scope: `scripts/process_identity.py`, `scripts/researchctl.py` reconciliation path and `scripts/run_registry.py`. No live services or GPU workloads were used.

## MEDIUM — unrecognized registry state exits successfully

- Location: `scripts/researchctl.py:191-203`, `cmd_reconcile`.
- Root cause: known active/terminal states are mapped to UNKNOWN, but any other SQLite value is returned verbatim and produces exit 0. SQLite schema does not constrain `run.state`, so damaged or unsupported state can reach this path. There is no current automatic resource-release or reclaim behavior, but exit 0 can mislead external automation interpreting a successful reconcile as a safe outcome.
- Reproduced with a temporary genuine registry: call `run_registry.claim(folder, 'run-1', 'p', '0'*64, 'SUBMITTED')`, then execute `UPDATE run SET state='CORRUPTED'` with sqlite3. With `select_project` directed to the temporary root, `cmd_reconcile` prints state CORRUPTED and returns 0. No other run or service is touched.
- Required remediation: while no verified containment/release path exists, return UNKNOWN with nonzero exit for every unknown, malformed, or unsupported registry state. Ensure future transition and schema changes are explicit and do not equate command success with permission to retry.
- Acceptance: use real SQLite to inject unexpected values and prove UNKNOWN/exit 2, no mutation, no claim removal, no retry or resource release.

## MEDIUM — PID namespace not bound to observation

`process_identity.observe_process` compares hostname, boot UUID, PID and start ticks, but does not record or compare Linux PID namespace identity. Two processes in distinct PID namespaces on one kernel can share boot ID and hostname; numeric PIDs and start ticks may coincide. The existing wording correctly calls observations snapshots and expressly prohibits using them for reclaim, and current code does not reclaim or kill based on them. Before such observations drive decisions, record and compare PID namespace inode and relevant host/mount context, or return UNKNOWN when identity cannot be proven. Test simulated same tuple across differing namespaces and prove no automatic release or reclaim.

## LOW — read-only command can mutate registry

`run_registry.inspect` invokes `_connect`, which executes CREATE TABLE IF NOT EXISTS and pragmas; reconciliation described as read-only can acquire writes or migrate an old SQLite schema. Prefer a true SQLite read-only connection, tolerate legacy schema without mutation, and test file bytes/metadata and read-only directory handling.

## Verification

- `python3.10 -m unittest discover -s tests -p test_process_identity.py -v`: 5/5 pass, including live `/proc/self` match and fake-proc PID reuse/boot changes.
- Same process-identity suite on Python 3.12: 5/5 pass.
- `python3.10 -m unittest discover -s tests -p test_researchctl.py -v`: 10/10 pass.
- Observed normal recognized registry states map to UNKNOWN, no path deletes execution claims or invokes a resource-release operation, and missing/permission-denied `/proc` reads are never treated as proof of process death. These tests and static inspection do not establish cgroup containment, GPU release, authenticated identity, safe reclaim, or scientific validity.
