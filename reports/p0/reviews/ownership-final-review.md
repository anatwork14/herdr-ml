# Adversarial review of commit `29b32a46ac333c72fdfc9afa8db4b7357772c822`

## Scope

- **What I reviewed.** Only the source and tests pasted in your message. I took your word that they came from `git show` at this commit; I did not check them against the Git object.
- **How.** I used no tools and ran no tests. Anything I say about runtime behaviour, including whether a test passes, comes from reading the code.
- **What this is not.** It is not a certification of scientific validity, of operational readiness, or of containment.
- **Assumed holds.** I assume the holds on official runs, queued runs and bootstrap stay in place.

## Verdict

**No CRITICAL or HIGH defects found in the code paths that can actually run.** That reachable path is local `diagnostic` execution started with `init-run` or `_execute`.

- **Double execution.** The `execution.claim` `mkdir` fence is acquired before the database is touched, so losers fail before reaching SQLite. The `run` row check is a second barrier behind it.
- **False completion.** This commit removes the possibility structurally. `finish()` rejects every state except `UNKNOWN`. `status` and `reconcile` show every registry state as `UNKNOWN`, and a leader exit of 0 returns process exit 2. The leader's own outcome is kept only as `leader_state`.
- **Faults after launch.** Both exception paths, and `finally`, kill and reap the group. The registry is left in `DISPATCHING` or `RUNNING`.

The remaining risk is mostly **containment that is honestly reported as unverified but not provided**. That is a prerequisite gap rather than a correctness bug in the ledger.

---

## Status of prior findings

| Prior finding | Status at 29b32a4 | Notes |
|---|---|---|
| **MEDIUM-A**: controller death orphans the workload; child has no identity | **Partially resolved** | SIGQUIT is now handled and tested. `record_child()` stores pid, boot_id and start ticks before `DISPATCHING→RUNNING`, in a new `child_identity` table. Using a new table also works for existing databases. **Still open:** a SIGKILL'd or OOM-killed controller still orphans the group (no `PR_SET_PDEATHSIG`, subreaper or cgroup). `reconcile` only dumps the identity and does no liveness or pgrp scan. No test reads `inspect()['child']`. |
| **MEDIUM-B**: `COMPLETED` recorded after SIGKILLing survivors | **Resolved by redesign** | The terminal state is always `UNKNOWN`, with `containment: "UNKNOWN"`. The record is no longer misleading. What's missing is evidence: no `descendants_terminated` count or pids, so an operator cannot tell whether the controller cut work short. Downgraded to LOW-6. |
| LOW-1: fence without a registry row | **Open** | `_connect()` failing after `mkdir` leaves an empty fence and no row. `status` falls back to `status.json` and shows `SUBMITTED` indefinitely. Retries fail closed. |
| LOW-2: signals before handlers are installed | **Open** | Handlers are installed after `claim()`, `advance()` and the projection write. In that window, SIGINT raises `KeyboardInterrupt`, which `main` does not catch, and SIGTERM, SIGHUP or SIGQUIT kill the controller. The run stays in `CLAIMED` or `DISPATCHING`. Fails closed. Calling `execute` from a thread other than the main thread does the same: `signal.signal` raises `ValueError` after the claim. |
| LOW-3: official `BLOCKED` path never closes the claim | **Open, unreachable, and now worse if reached** | `finish()` accepts only `UNKNOWN`, so the registry cannot record `BLOCKED` at all. The gate and Git checks also run **after** `claim()`. Both must be fixed before the official hold is lifted. |
| LOW-4: `finish` from `DISPATCHING` | **Neutralised** | It is still allowed, but only to `UNKNOWN`. It is used correctly for exec failures (127) and for interrupts before launch. |
| LOW-5: corrupt registry breaks `status` for every run | **Open, plus a new aspect** | `sqlite3.DatabaseError` is not caught per run. New in this commit: `inspect()` → `_connect()` now runs `CREATE TABLE IF NOT EXISTS child_identity`, which **writes** to older databases. A read-only `status` therefore needs write access and a write lock. On a read-only runtime directory it fails for the whole listing with "attempt to write a readonly database". |
| SIGHUP overrides `nohup` | **Open** | Fails safe, records `INTERRUPTED`, but surprises the operator. |
| Exit code vs state, and EIO on print | **Open, partly by design** | A leader exit of 0 now returns 2, which is the same code as every `main` error. Callers cannot tell "finished, containment unknown" from "error". A print after a hang-up can still raise EIO twice. |
| Inherited `SIGCHLD=SIG_IGN` | **Resolved** | `execute` rejects any SIGCHLD disposition other than the default before claiming anything, so no fence is left behind. |

---

## Remaining MEDIUM

### MEDIUM-1: The workload outlives an uncatchable controller death, and `reconcile` cannot assess it

**Root cause.**
- `start_new_session=True` detaches the group from the controller.
- Nothing ties the child's lifetime to the controller: no `PR_SET_PDEATHSIG`, no `PR_SET_CHILD_SUBREAPER`, no cgroup or systemd scope.
- The identity recorded by `record_child` is forensic only, and `cmd_reconcile` never compares it with `/proc`.

**Reproduction (by reading).**
1. Use the setup from `_cancel_stubborn_descendant`, but call `proc.kill()` instead of sending a handled signal.
2. The leader and the grandchild keep running.
3. `inspect()` shows `RUNNING` with `child={pid, boot, start_ticks}`.
4. `reconcile` prints `UNKNOWN` with no liveness information.

**Impact.**
- On a GPU host, an orphaned diagnostic run can hold devices indefinitely with no supervisor.
- Separately, a descendant that calls `setsid()` or `setpgid()` escapes even the handled-signal and normal-exit `killpg`. Process groups cannot express containment, and the code correctly records `containment: UNKNOWN`.

**Fix direction.** Do these before relying on any "no survivors" property:
- Run the workload in a cgroup v2 / systemd transient scope and kill the cgroup.
- Failing that, add `PR_SET_CHILD_SUBREAPER` on the controller plus `PDEATHSIG` on the child.
- In `reconcile`, compare `/proc/<pid>/stat` start ticks with the recorded identity, and list processes whose pgrp or sid equals the recorded pid. This is evidence only, not reclaim.

### MEDIUM-2: Log hashes are recorded as final while escaped descendants may still write to the logs

**Root cause.**
- `stdout.log` and `stderr.log` are inherited by every descendant.
- `sha_file()` runs right after `stop_and_reap()`.
- A descendant that left the process group (or survived a controller SIGKILL) still holds those file descriptors and can append after the hash is committed to the registry and to `result.json`.

**Impact.** The recorded `stdout_sha256` and `stderr_sha256` can later disagree with the files, and nothing marks them as provisional. `containment: UNKNOWN` sits next to them, but a consumer verifying log integrity would read a mismatch as tampering.

**Fix options.**
- Label the hashes `*_sha256_at_leader_exit`.
- Or, once real containment exists, hash only after the cgroup is confirmed empty.

---

## LOW

1. **Contradictory leader record when a signal races the normal exit.** If a signal arrives after `waitid` sees the leader exit but before `stop_and_reap()`, the result reads `leader_state: INTERRUPTED` with `exit_code: 0`. If it arrives after `finish()` commits, the process returns `128+sig` while the record says `leader_state: COMPLETED`. Either way the registry state is `UNKNOWN`, so nothing is falsely certified. **Fix:** capture `interrupted` at the moment the leader exit is observed, and derive the return code from the committed result.
2. **Exit code 2 is overloaded.** Success-with-unknown-containment shares exit 2 with every error. **Fix:** use a distinct code, for example 3, and document it.
3. **The fence is empty.** It cannot be told apart from a legacy claim. **Fix:** write the token, pid, boot_id and time into a file inside the fence before `_connect()`. Have `status` and `reconcile` show `FENCED_NO_RECORD` when the fence exists but no row does.
4. **Projections are not durable.** `atomic_json` does not fsync the file or the directory. `submission.sha256` is written with a plain, non-atomic `write_text`. After a crash this can cause a hash mismatch and refuse execution, which fails closed. The SQLite registry itself is durable: `synchronous=FULL`, and the directory is fsynced after the fence and after commit.
5. **`submission.sha256` is integrity, not authentication.** Anyone who can write the run folder can rewrite both `submission.json` and its hash, and then `_execute` any argv as `diagnostic`. This is acceptable on the stated "single trusted host" model, but it is a hard prerequisite for lifting the official hold (see the prerequisites section).
6. **No evidence that descendants were terminated** (left over from MEDIUM-B). This is a scan of `/proc/*/stat` for the pgrp before the SIGKILL, recorded as `descendants_terminated`.
7. **The Python version check cannot run on old interpreters.** `researchctl.py` uses parenthesized multi-item `with`. On Python 3.8 that is a `SyntaxError` at parse time, so the `__main__` version guard never executes.

---

## Test observations (from reading; nothing executed)

1. **`test_stale_token_cannot_change_or_complete` passes for the wrong reason.**
   - It calls `finish()` with `state: 'COMPLETED'`, which is now rejected by the state check before the token is ever compared.
   - The token check in `finish()` is therefore untested.
   - **Fix:** use `{'state': 'UNKNOWN'}` with the wrong token.
2. **`record_child` is never asserted.**
   - Assert in a cancellation or normal-exit test that `inspect(folder)['child']['pid']` matches the leader.
   - Assert that `start_ticks` is not `None`.
3. **`test_ignored_sigchld_is_rejected_without_execution` is weak.**
   - It only asserts that `ResearchError` was raised, and `execute('demo', 'unused')` would raise it anyway if the check moved below `select_project`. That could come from "Unknown project", depending on whatever `RESEARCH_ROOT` or `HERDR_ML_CONFIG` leaked from `test_twenty_workers_have_one_owner`.
   - **Fix:** build a valid `SUBMITTED` run, then assert on the message, that no marker was written, and that no `execution.claim` exists.
4. **Still missing:**
   - a registry `sqlite3.OperationalError` on `('DISPATCHING','RUNNING')`;
   - a signal before `Popen`;
   - a failure between the fence and `COMMIT`;
   - a registry-row check with the fence removed;
   - a controller SIGKILL;
   - `inspect()['state'] == 'UNKNOWN'` assertions in the signal tests, which currently check only the projections.
5. **Hygiene.**
   - `test_twenty_workers_have_one_owner` still mutates `os.environ` globally.
   - `worker` still catches only `ResearchError` and `OSError`.
6. **Unverified portability risk.** Python 3.14 changed the default multiprocessing start method on Linux to `forkserver`. The `multiprocessing.Process` tests rely on fork-like inheritance of the environment and module state, and could behave differently. Pin `multiprocessing.get_context('fork')` in the tests, or confirm behaviour on your interpreter.

---

## Standalone components vs live integrated execution

**What this code base demonstrates (reading-level confidence):**
- **`run_registry`:** single-claimant fence, guarded transitions, `UNKNOWN`-only terminal state, crash survival.
- **The local `diagnostic` controller:** signal handling for TERM, INT, HUP and QUIT, killing and reaping the same process group, a fail-closed rejection of non-default SIGCHLD.

All of this has been exercised (per the tests as written) on a CPU-only Linux host with trivial Python workloads.

**What is not demonstrated at all:**
- Herdr integration.
- Pueue queuing, which is blocked: `queue_task_id` is never populated, and environment and interpreter propagation are unvalidated.
- Official runs, which are blocked.
- Real GPU or ML workloads, including D-state processes, multi-process data loaders and NCCL children that `setsid`.
- Multi-host or shared-filesystem use. SQLite locking on NFS is out of model.
- Any reclaim or recovery procedure.

## Prerequisites before lifting any hold

1. **Real containment.** cgroup v2 / systemd scope with kill-on-exit, or at minimum a subreaper plus `PDEATHSIG`. Containment would then be verifiable, and the result could carry something other than `UNKNOWN`.
2. **Reconcile evidence and a documented manual reclaim runbook.** Liveness checked against the recorded child identity, plus `FENCED_NO_RECORD` handling.
3. **Official path.**
   - Move the gate and pinned-Git checks before `claim()`.
   - Give the registry a terminal `BLOCKED` transition from `CLAIMED`.
   - Replace `submission.sha256` with authenticated approval and immutable provenance.
4. **Queued path.**
   - Task-id binding.
   - Validation of the daemon's interpreter and environment.
   - A defined `QUEUED` claim flow (currently refused outright).
5. **Read-only `status`/`reconcile`.** No DDL in `inspect()`, and per-run handling of `sqlite3.Error`.
