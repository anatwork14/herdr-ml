# Ownership review of commit `1376f400e5df7bd1ae030a185c961a8cb50c9b6d`

## Verdict

**No CRITICAL or HIGH defects found** in current-version ownership logic, and I found no double execution or false completion.

- **Single claim:** `BEGIN IMMEDIATE` plus a single `id=1` row gives one winner.
- **Stale owners:** a stale token cannot advance or finish the run, because every update is guarded by `owner=? AND state=?`.
- **No reclaim:** an abandoned claim is never reclaimed.
- **Atomic finish:** the terminal state and `result_json` commit in one transaction.
- **Truthful reporting:** `status` and `reconcile` prefer the registry over JSON projections.

I found **three MEDIUM defects**. All three are in the supervisor's handling of the live process. In each, the registry stays truthful, but a dispatched workload either keeps running unsupervised or keeps running after a terminal state is recorded. There is also one conditional MEDIUM about running alongside older code, plus several LOW items.

I am not certifying scientific validity.

### Known missing integrations (not counted as new defects)

- Official execution: authenticated approval and immutable provenance. Blocked by `require_supported_execution`.
- Queued execution: containment and daemon interpreter validation. Blocked.
- Bootstrap. On hold.
- Owner liveness, lease, and automatic reclaim. Intentionally absent.
- cgroup-level containment. Descendants that call `setsid` escape the process group.

---

## MEDIUM-1: SIGHUP (pane or terminal close) orphans the workload, and the registry has no handle to find it

**Root cause**
- `execute` handles only SIGTERM and SIGINT.
- The child is started with `start_new_session=True`, so it is in a different session from the controller and has no controlling terminal.
- When a Herdr pane closes, the controller gets SIGHUP and dies by default action. The child never receives a signal and keeps running, holding the GPU and writing logs.
- The registry is left at `RUNNING`, and `reconcile` correctly reports `UNKNOWN`.
- However, the registry stores `owner_pid`, `owner_boot` and `owner_start` for the **controller** only. It stores nothing about the child (pid, pgid, start time). No tool can identify or contain the orphan, so the `UNKNOWN` state can never be resolved with evidence.

**Reproduction.** Use the same setup as `test_sigterm_stops_stubborn_descendant`, but call `proc.send_signal(signal.SIGHUP)`.
- Result: the controller exits with -1, the child and grandchild stay alive, and `inspect()['state'] == 'RUNNING'`.

**Fix**
1. Add SIGHUP (and SIGQUIT) to the handled set.
2. In the `DISPATCHING→RUNNING` transaction, record `child_pid`, `child_pgid` and the child's `/proc/<pid>/stat` start time, so `reconcile` can report whether that group still exists.
3. Optionally, set `PR_SET_PDEATHSIG` on the direct child. This does not cover grandchildren, which is the known containment gap.

**Acceptance tests**
- After SIGHUP to the controller, all members of the child group are gone within 5 s, and the registry state is `INTERRUPTED`.
- After SIGKILL to the controller, `inspect()` exposes the child pgid and start time, and `reconcile` reports whether that group is alive.

## MEDIUM-2: An exception after `Popen` makes the controller exit and abandon a live child

**Root cause**
- Inside `with ... stdout, stderr`, anything that raises after `Popen` succeeds escapes the block with no kill and no wait. This includes:
  - `run_registry.advance(DISPATCHING→RUNNING)` raising `sqlite3.OperationalError`, for example from a full disk or a 30 s lock timeout;
  - `atomic_json(status RUNNING)` raising `OSError` (ENOSPC), which the `except OSError` branch deliberately re-raises;
  - `os.killpg` raising `PermissionError`.
- `main` catches the exception and returns 2. The child, in its own session, keeps running.
- The comment "A post-dispatch failure is uncertain" is correct about the **state**: it should not be recorded as a launch failure. But being uncertain about state does not justify abandoning supervision.

**Reproduction.** Patch `m.run_registry.advance` so that the `('DISPATCHING','RUNNING')` call raises `sqlite3.OperationalError`, and run `execute()` with argv `sleep 30`.
- Result: `execute` raises, `sleep` is still alive, and the registry stays at `DISPATCHING`.
- The same happens if you patch `atomic_json` to raise `OSError` when the payload state is `RUNNING`.

**Fix**
- Once `child` is set, wrap everything that follows in `try/except BaseException`.
- In the handler: `killpg` SIGTERM, wait, `killpg` SIGKILL, reap, then make a best-effort `finish(INTERRUPTED, detail='controller_error: …')`, and finally re-raise.
- If `finish` itself fails, leave the registry non-terminal. That is the truthful fail-closed outcome.

**Acceptance test.** For each injected fault point after `Popen`:
- the child group is dead when `execute` returns or raises;
- the registry is either `INTERRUPTED` with a recorded reason, or still non-terminal;
- it is never `COMPLETED`.

## MEDIUM-3: `COMPLETED` or `FAILED` is committed while process-group members are still running

**Root cause**
- The group-wide `killpg(SIGKILL)` runs only when `interrupted` is true.
- When the direct child exits normally, any background descendants still in its group keep running. Examples are a forked data loader or a `subprocess.Popen` without `wait`.
- `finish()` still commits a terminal state and the stdout/stderr hashes, while the work started under this claim keeps producing outputs and holding the GPU.
- The registry docstring itself says descendants are the reason claims are never reclaimed. The terminal path does not apply the same rule.

**Reproduction.** Run argv `python -c "import subprocess; subprocess.Popen(['sleep','60'])"`.
- Result: `COMPLETED exit=0`, and `sleep` is still alive in pgid `child.pid`.

**Fix**
1. Always signal the group after the leader exits.
2. To avoid a pgid-reuse race, observe the exit with `os.waitid(P_PID, pid, WEXITED|WNOWAIT)`, then call `killpg`, then reap.
3. Record in the result whether any survivors were killed, for example `descendants_terminated: true`. Alternatively, refuse `COMPLETED` when survivors existed.

**Acceptance test.** After `execute` returns, no process with pgid `child_pid` exists, and the result records whether any were killed. This covers groups only; `setsid` escapes remain the known containment gap.

## MEDIUM (conditional): No mutual exclusion with code that predates the registry

**Root cause**
- `claim` treats an existing `execution.claim` as a stop signal, but it never **creates** one.
- Older `researchctl` code, which uses `mkdir execution.claim`, ignores SQLite entirely.
- Between the registry `COMMIT` and the `DISPATCHING` projection write, `status.json` still says `SUBMITTED` or `QUEUED`. During that window, a legacy controller can pass its state check and run the same job.
- This matters here because several worktrees share one `RESEARCH_ROOT` (`herdr-ml-p0`, `herdr-ml-review`, and possibly `main`). It also matters for a legacy process that was already running when this code was upgraded.

**Fix.** Inside the `BEGIN IMMEDIATE` window, `os.mkdir(folder/'execution.claim')` before inserting the row, and write the token into it.
- Distinguish a fence created by the registry from a true legacy one, either through the row or through the marker's contents.

**Acceptance test**
- Run a legacy-style `mkdir` claimer concurrently with `claim()` across many iterations. Exactly one of them succeeds every time.
- After `claim()`, a legacy `mkdir` attempt fails with `EEXIST`.

---

## LOW / latent

1. **A signal before `Popen` still launches the workload.** If SIGTERM arrives during `DISPATCHING`, `interrupted` is set, but `Popen` still runs and the child gets SIGTERM only afterwards, so its side effects may already have happened.
   - Fix: check `interrupted` before `Popen` and finish as `INTERRUPTED` without launching.
2. **Signals before the handlers are installed leave a stuck claim.** SIGTERM or SIGINT arriving between `claim()` and handler installation kills the controller by default action, leaving `CLAIMED` or `DISPATCHING` permanently with nothing launched.
   - This fails closed, but it needs manual reconciliation.
   - Install the handlers before `claim()`.
3. **Official `BLOCKED` path never closes the claim** (currently unreachable because of the hold).
   - It writes `status.json` as `BLOCKED` but leaves the registry at `CLAIMED`. `status` would show `CLAIMED`, and `reconcile` would say "may own live descendants" even though nothing was dispatched.
   - `finish` cannot go from `CLAIMED` to a terminal state. Add a `CLAIMED→BLOCKED` transition before lifting the official hold.
4. **`finish` from `DISPATCHING` accepts `COMPLETED`.** This is unreachable today, but should be restricted to `FAILED` (launch failure) as hardening.
5. **One corrupt registry breaks `cmd_status` for every run.** `sqlite3.DatabaseError` from `inspect` is not caught inside the per-run loop, so a single corrupt `registry.sqlite3` makes the whole command fail.
   - Catch `sqlite3.Error` per run and report that run as `INVALID`.
   - Separately, `inspect` → `_connect` runs DDL and may roll back a hot journal. It is a read path with write side effects, which is acceptable on one trusted host but worth documenting.
6. **pgid-reuse race in the interrupt path.** `killpg(child.pid, SIGKILL)` runs after `child.wait()` has reaped the child. If the group is empty, the pgid can be reused. The window is tiny; the `WNOWAIT` ordering from MEDIUM-3 fixes it.
7. **Test hygiene**
   - `test_p0_ownership` mutates `os.environ` globally without restoring it.
   - `worker` catches only `ResearchError` and `OSError`, so a `sqlite3.Error` shows up as an opaque `queue.Empty` instead of a diagnostic.
   - None of the MEDIUM scenarios above are tested: SIGHUP, faults after `Popen`, lingering descendants after a normal exit, and the legacy-fence race.
