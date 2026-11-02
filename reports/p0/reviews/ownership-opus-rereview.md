# Follow-up review of commit `7c1450f149ebf97644e581354379f934adea914a`

**Scope.** I reviewed only the source and tests included in your message. I did not compare them against the Git object, and I ran no tests. Anything I say a test does comes from reading it. Live integrations (Herdr, Pueue) and scientific correctness are out of scope. The holds on official runs, queued runs and bootstrap are assumed to stay in place.

## Verdict

**No CRITICAL or HIGH defects found.** Reading the code, I found no path to double execution and no path to a false `COMPLETED`:

- **Exactly one claimant.** The `execution.claim` `mkdir` fence decides who wins between new and legacy workers. The SQLite row check is a second barrier behind it.
- **`COMPLETED` is narrow.** It is recorded only when the leader exited 0 and no signal was observed.
- **Faults after launch kill the workload.** A fault after `Popen` now kills and reaps the process group, and leaves the registry non-terminal.

**Two actionable MEDIUM issues remain.** Both are leftovers of the previous MEDIUM-1 and MEDIUM-3. I also list several LOW items and test gaps.

---

## Status of previous findings

| Prior finding | Status | Evidence in 7c1450f |
|---|---|---|
| **MEDIUM-1** SIGHUP orphans the workload; no handle on the child | **Partially resolved** | SIGHUP is now in the handled set (`execute`, `old_handlers = {… SIGHUP}`), and `test_sighup_stops_stubborn_descendant` covers it. Still missing: SIGQUIT handling, recorded child pid/pgid/start time, and `PR_SET_PDEATHSIG`. See **MEDIUM-A**. |
| **MEDIUM-2** Exception after `Popen` abandons a live child | **Resolved** | Both `except OSError` (when `child is not None`) and `except BaseException` call `stop_and_reap()` and re-raise. `finally` also calls `stop_and_reap()`. The registry stays `RUNNING`/`DISPATCHING`, which meets the prior acceptance criterion ("never `COMPLETED`"). `test_post_spawn_status_failure_kills_group_and_stays_uncertain` covers the `atomic_json` fault. The `advance()` sqlite fault is not tested; see Tests. |
| **MEDIUM-3** Terminal state committed while group members still run | **Containment resolved; record-keeping not** | The leader exit is observed with `waitid(… WNOWAIT)`, then `stop_and_reap()` always sends `killpg(SIGKILL)` before reaping. `test_normal_leader_exit_stops_same_group_descendant` covers it. The result still does not record that survivors were killed. See **MEDIUM-B**. |
| **MEDIUM (conditional)** No mutual exclusion with legacy `mkdir` claimers | **Resolved** | `claim()` creates `execution.claim` (0700) before the transaction and fsyncs the directory. `MixedVersionTests` races a legacy `mkdir` against `claim()` 10 times. A new LOW item follows from this: a fence can now exist without a registry row (LOW-1). |
| LOW-1 A signal before `Popen` still launches | **Resolved (narrow residual)** | `if interrupted: code = 128 + sig` skips the launch. A signal between that check and `Popen` still launches, but the post-launch `if interrupted: signal_group(SIGTERM)` then kills it. |
| LOW-2 Signals before handlers are installed leave a stuck claim | **Open** | Handlers are installed after `claim()`, `advance(CLAIMED→DISPATCHING)` and the projection write. A default-action SIGTERM, SIGINT or SIGHUP there leaves `CLAIMED`/`DISPATCHING` permanently, with nothing launched. This fails closed. |
| LOW-3 Official `BLOCKED` path never closes the claim | **Open (unreachable)** | Unchanged. `require_supported_execution` rejects `official` before `claim()`. This must be fixed before the official hold is lifted. |
| LOW-4 `finish` from `DISPATCHING` accepts `COMPLETED` | **Open (unreachable)** | `finish()` still allows any terminal state from `DISPATCHING`. |
| LOW-5 A corrupt registry breaks `cmd_status` for every run | **Open** | The per-run `except` catches only `(OSError, JSONDecodeError)`. A `sqlite3.DatabaseError` reaches `main` and returns 2 for the whole listing. `inspect()` still runs DDL and can roll back a hot journal. |
| LOW-6 pgid-reuse race in the interrupt path | **Resolved** | `signal_group` only signals while `group_owned` is true. `stop_and_reap` clears `group_owned` before `child.wait()`. The wait loop uses `WNOWAIT`, so the zombie leader pins the pgid. |
| LOW-7 Test hygiene | **Partially open** | `OwnershipTests.test_twenty_workers_have_one_owner` still sets `os.environ[...]` globally, and `worker` still catches only `ResearchError`/`OSError`. The new tests use `patch.dict` or `env=`. |

---

## Remaining MEDIUM

### MEDIUM-A: If the controller dies without running its handler, the workload is orphaned and the registry cannot identify it

**Root cause.** The child is started with `start_new_session=True`, so it does not share the controller's fate.

- If the controller dies in a way that bypasses its handlers, the group keeps running with no supervisor. Examples are SIGKILL, the OOM killer, SIGQUIT (Ctrl-\ is not handled), or an interpreter crash.
- The `run` row stores only the controller's identity (`owner_pid`, `owner_boot`, `owner_start`), never the child's.
- `reconcile` therefore reports `UNKNOWN` "may own live descendants", with nothing an operator or tool can check that claim against.

Liveness and reclaim are intentionally absent, but this is a separate gap: without the child's identity, an `UNKNOWN` state can never be resolved with evidence.

**Reproduction (by reading).** Use the setup of `_cancel_stubborn_descendant`, but call `proc.kill()` instead of sending a handled signal.
- The child and grandchild survive.
- `inspect()` shows `RUNNING` with no child pid or pgid.

**Fix.**
1. Record `child_pid`, `child_pgid` (equal to `child_pid`), `boot_id`, and the child's `/proc/<pid>/stat` field 22 start time in the same transaction as `DISPATCHING→RUNNING`.
   - Pass these through `advance()`, or add a dedicated `set_running(folder, token, child_identity)`.
   - Use a new `CREATE TABLE IF NOT EXISTS child(...)` table rather than new `run` columns. Existing `registry.sqlite3` files would not gain new columns through the current `CREATE TABLE IF NOT EXISTS run` DDL.
2. In `reconcile`, report whether that pid with that start time is still alive, and list any `/proc/*/stat` entries whose pgrp equals `child_pgid`. This is evidence only, not reclaim.
3. Add SIGQUIT to the handled set.
4. Optionally set `PR_SET_PDEATHSIG(SIGKILL)` on the direct child, re-checking `getppid()` after the call to close the race. This does not cover grandchildren.

**Residual.** A SIGKILL that lands between `Popen` and the RUNNING commit still leaves `DISPATCHING` with no identity. This is inherent, and `reconcile` already treats `DISPATCHING` as `UNKNOWN`.

**Acceptance tests.**
- After SIGKILL to the controller, `inspect()` exposes the child's pgid and start time, and `reconcile` reports that the group is alive.
- After the test kills the group, `reconcile` reports that it is gone.
- SIGQUIT to the controller results in `INTERRUPTED` with no survivors.

### MEDIUM-B: `COMPLETED` is recorded even when the controller had to SIGKILL surviving descendants

**Root cause.** On a normal leader exit, `stop_and_reap()` unconditionally sends `killpg(SIGKILL)`, which is correct for containment. However:

- The result written to the registry and `result.json` contains only `exit_code` and the log hashes.
- If a background writer, data loader or checkpoint flusher was still running in the group, the controller truncated it.
- The run is then recorded as `COMPLETED exit=0`, with no indication that the controller cut the work short.
- `test_normal_leader_exit_stops_same_group_descendant` checks that the grandchild dies, but not what was recorded.

This is not a false completion by the leader's contract, but the terminal record is misleading. Runs are diagnostic only, which limits the impact.

**Fix.**
1. Before the SIGKILL in the normal-exit path, while the zombie leader still pins the pgid, scan `/proc/*/stat` for pgrp equal to `child.pid` and pid not equal to `child.pid`.
2. Record the result as `descendants_terminated: <count or pids>`.
3. Decide on a policy and test it. Either downgrade to `FAILED` with `detail="descendants_killed"`, or keep `COMPLETED` but make the flag mandatory in the result schema.

**Acceptance test.** Rerun the existing normal-exit test, and assert that `inspect()['result']` records the terminated descendant. Also assert the chosen terminal state.

---

## New / remaining LOW

1. **A fence can exist without a registry row.** In `claim()`, an exception after the `mkdir` and before `COMMIT` leaves `execution.claim` with no `run` row. Examples are `_connect` failing on a lock timeout, ENOSPC, or a corrupt database, or the directory `fsync` failing.
   - This fails closed: retries hit `EEXIST`, and `reconcile` reports `UNKNOWN`.
   - However, `cmd_status` falls back to `status.json` and shows `SUBMITTED` indefinitely.
   - The fence is also empty, so it cannot be told apart from a genuine legacy claim. The prior review suggested writing the token into it.
   - Fix:
     - Write `token`, pid, boot_id and time into a file inside the fence before opening the database.
     - Have `status` and `reconcile` show `FENCED_NO_RECORD` when the fence exists but no row does.
2. **SIGHUP now overrides `nohup`.** If the controller was started with SIGHUP ignored (`nohup researchctl init-run …`), `execute` replaces that disposition with `terminate`. Closing the terminal then kills a run the operator explicitly detached.
   - This fails safe, and the state is recorded truthfully as `INTERRUPTED`, but it is surprising.
   - Fix: if `signal.getsignal(SIGHUP) is SIG_IGN`, leave SIGHUP ignored.
3. **The exit code can contradict the recorded state, and output can fail after a terminal hang-up.**
   - A signal that arrives after `finish(COMPLETED)` commits makes `execute` return `128 + sig`, while the registry says `COMPLETED`.
   - After a real tty hang-up, the final `print(..., flush=True)` raises `EIO`. `main`'s own `print(..., file=sys.stderr)` raises again, so the process exits 1 with no message.
   - In both cases the registry and projections are already correct; only the exit status is wrong.
   - Fix:
     - Compute the return code from the committed `end_state`.
     - Wrap the final print in `try/except OSError`.
4. **An inherited `SIGCHLD=SIG_IGN` breaks supervision.** If a parent leaves SIGCHLD ignored, the kernel reaps the leader automatically.
   - `waitid` then raises `ChildProcessError`, an `OSError`, so the code takes the `child is not None` branch.
   - That branch sends `killpg` to a pgid that is no longer pinned, so a pgid-reuse race becomes possible.
   - `Popen.wait` maps `ECHILD` to 0. The registry stays `RUNNING`, so this still fails closed.
   - Fix: set SIGCHLD to `SIG_DFL` before `Popen`.
5. **Unchanged from the prior review:** LOW-2 (handler install order), LOW-3 (official `BLOCKED` path), LOW-4 (`finish` from `DISPATCHING`), LOW-5 (corrupt registry in `status`). Details are in the table above.
6. **An optional evidence event on post-launch faults.** When `stop_and_reap()` runs because of an exception, a best-effort `events` insert would help an operator during reconcile, for example `detail='controller_error: <exc>; group SIGKILLed'`. It should not change state.

---

## Test gaps (no tests were executed)

1. **Registry fault after `Popen` is untested.**
   - Patch `run_registry.advance` to raise `sqlite3.OperationalError` on `('DISPATCHING','RUNNING')`.
   - Assert the child is dead and `inspect()['state'] == 'DISPATCHING'`.
   - Assert `main` returns 2.
2. **A signal before `Popen` is untested.**
   - Deliver SIGTERM after the handlers are installed and before `Popen`.
   - Assert that nothing launched and the registry is `INTERRUPTED`.
3. **The SIGHUP and SIGTERM tests check only the `status.json` projection.** Also assert `run_registry.inspect(folder)['state'] == 'INTERRUPTED'` and that a result is present.
4. **Controller SIGKILL and SIGQUIT are untested.** Add these with MEDIUM-A.
5. **The registry row check is no longer exercised on its own.** Every reclaim test is now stopped by the `mkdir` fence first. Add a test that removes `execution.claim` after a committed claim and asserts that `claim()` still raises. This covers an operator following a legacy runbook.
6. **A crash between the fence and the `COMMIT` is untested.** Inject a failure into `_connect`, then assert the fence remains, there is no row, retries fail, and `reconcile` returns `UNKNOWN`.
7. **Hygiene in `test_p0_ownership.py`:**
   - Switch `test_twenty_workers_have_one_owner` to `patch.dict(os.environ, …)`.
   - Have `worker` catch `Exception` and report `repr(exc)`, so sqlite errors do not surface as `queue.Empty`.
