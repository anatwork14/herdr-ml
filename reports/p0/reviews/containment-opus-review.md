# Containment review: commit `1376f400e5df7bd1ae030a185c961a8cb50c9b6d`

**Scope.** I reviewed `scripts/researchctl.py`, `tests/test_p0_ownership.py` and `docs/OPERATIONS.md` as supplied.

**Not reviewed:**
- `scripts/run_registry.py` (not included). Any claim about atomic claim and transition semantics depends on it.
- The bootstrap scripts (not included). I can't assess the bootstrap hold from this material.
- Scientific validity is not assessed or certified.

**Summary:** 4 new defects (2 HIGH, 2 MEDIUM) and several LOW items. The official hold holds in both `init-run` and `_execute`. The queued hold can be bypassed under one configuration.

---

## HIGH-1: `"local"` sentinel lets queued execution bypass the hold

**Root cause.** `"local"` is used both as the "not queued" marker and as a valid user-supplied queue name.
- `require_supported_execution` allows `queue == "local"`.
- `cmd_init_run` sends anything truthy in `args.queue` to Pueue.
- `submission["queue"]` stores `"local"` for both cases, so `_execute` can't tell them apart.

**Repro.**
1. Config: `{"id":"demo","directory":"demo","gpu_group":"local"}`.
2. Run `pueue group add local`.
3. Run `researchctl.py init-run --project demo --mode diagnostic ... --queue local --approve-cost -- python3 -c 'print(1)'`.

**Result:** the output shows `QUEUED`, Pueue runs `_execute`, and `require_supported_execution(..., "local")` passes. The queued path runs end to end despite the hold.

**Fix.**
- Block on `if args.queue:` unconditionally.
- Store a separate field, e.g. `"dispatch": "local"|"pueue"`.
- Have `_execute` reject any `dispatch` other than `local`.
- Reject `local` as a group name.

**Acceptance test.** With `gpu_group: "local"`, `init-run --queue local --approve-cost` exits 2 with "Queued execution BLOCKED". No `pueue` invocation happens (assert with a fake `pueue` on PATH that records calls).

---

## HIGH-2: SIGHUP (and SIGQUIT/SIGTSTP) orphans a running workload

**Root cause.** Only SIGTERM and SIGINT are trapped. The child uses `start_new_session=True`, so it has no controlling tty and never receives the hangup. If the Herdr pane, SSH session or terminal closes, the supervisor dies with the default action:
- The process group keeps running with nothing supervising it.
- The registry stays `RUNNING`.

`reconcile` correctly reports UNKNOWN, but nothing tries to stop the workload. This is the most likely failure in the documented operating model (Herdr panes over SSH).

**Repro.**
1. Start `init-run ... -- sh -c 'sleep 300'`.
2. Run `kill -HUP <supervisor pid>`.
3. Check `ps -o pid,pgid,sid,cmd -g <pgid>`: `sleep` is still alive, and `status.json`/registry show `RUNNING`.

**Fix.**
- Trap SIGHUP and SIGQUIT with the same `terminate` handler.
- Decide on SIGTSTP: ignore it, or forward it to the group.
- Independently, set `PR_SET_PDEATHSIG` in the child via `preexec_fn`/ctypes as a backstop for supervisor SIGKILL. It only covers the direct child (see Known missing).

**Acceptance test.** Same harness as `test_sigterm_stops_stubborn_descendant`, but send SIGHUP. Assert the grandchild is gone within 5 s and the registry state is `INTERRUPTED`.

---

## MEDIUM-1: Normal leader exit leaves the process group alive and records a terminal state

**Root cause.** The group is killed only `if interrupted`. When the leader exits on its own (code 0 or nonzero), surviving group members keep running: background `&` jobs, dataloader workers, `nohup` children that didn't call `setsid`. The run is still recorded as `COMPLETED` or `FAILED` with stdout/stderr hashes, while descendants may still be writing to the checkout and logs (the log hashes then don't match the final files).

**Repro.**
1. Run `init-run ... -- sh -c 'sleep 120 & exit 0'`.
2. The result is `COMPLETED exit=0`, and `pgrep -g <pgid>` still lists `sleep`.

**Fix.**
- On every exit path, detect leader exit without reaping it: `os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)`.
- Send `killpg` SIGTERM, wait a grace period, then SIGKILL, and only then reap the leader.
- Record whether survivors existed (e.g. `"descendants_killed": true`). Don't report a clean `COMPLETED` in that case without the flag, or downgrade the state.

Keeping the zombie leader unreaped also fixes LOW-1.

**Acceptance test.** Run `sh -c 'sleep 120 & exit 0'` and assert:
- No process with that pgid exists after `execute` returns.
- The result records the cleanup.

---

## MEDIUM-2: Exceptions after spawn abandon the live child

**Root cause.** Several failures after `Popen` succeeds propagate out of `execute` without touching the child:
- `except OSError: if child is not None: raise` re-raises without killing anything.
- `run_registry.advance(... "RUNNING")` raising `RegistryError` or `sqlite3.Error` (e.g. `database is locked`) is not caught locally.
- `atomic_json` failing (ENOSPC, EROFS) likewise.

In each case the `finally` block only restores signal handlers. The detached session keeps running, and `main` prints `ERROR` and returns 2.

Recording "uncertain" is correct accounting. Abandoning a process the supervisor can still reach is not.

**Repro (in-process).** Monkeypatch `run_registry.advance` to raise when `new == "RUNNING"`, then run `execute` with argv `sleep 60`. `execute` raises, and `sleep` is still alive.

**Fix.** Add an outer `except BaseException` (or a `finally` guarded by "child is alive and not cleanly reaped") that:
1. Kills the group (SIGTERM, grace period, SIGKILL).
2. Tries a best-effort registry note.
3. Re-raises.

Leave the registry state UNKNOWN/uncertain if it can't be written.

**Acceptance test.** With `advance` patched to fail, and separately with `atomic_json` patched to raise `OSError`, assert that `execute` raises and the child pgid is empty within 5 s.

---

## LOW

1. **pgid reuse race.** In the interrupt path, `killpg(child.pid, SIGKILL)` runs after `child.wait()` has reaped the leader. If the group is empty by then, the pid/pgid can be recycled, and SIGKILL could hit an unrelated group (needs a fast pid wrap plus a `setsid`, so it's unlikely). The `WNOWAIT` ordering in MEDIUM-1 removes this.
2. **Signal window before handlers are installed.** Handlers go in after `advance(CLAIMED→DISPATCHING)`. A signal before that kills the supervisor and leaves the run in `CLAIMED`/`DISPATCHING`. No child exists yet, so this is safe, but reconcile then reports "may own live descendants" when none can exist. Install handlers before `claim`.
3. **Latent official-mode leak.** The official-mode BLOCKED path after `claim` leaves the registry in `CLAIMED` permanently, and only `status.json` says BLOCKED. It is unreachable today because `require_supported_execution` runs first. Fix it before the official hold is lifted.
4. **Launch-failure transition depends on `run_registry`.** The `Popen` `OSError` path calls `finish` from `DISPATCHING`. If `run_registry.finish` only accepts `RUNNING`, a launch failure raises and strands the run. Please verify against `run_registry.py`.
5. **Tests.**
   - `CancellationTests` asserts on `status.json`, which the docs call a projection. It should assert on the registry state.
   - `OwnershipTests` mutates `os.environ` globally without restoring it.
   - The ownership test's `worker` doesn't catch `RegistryError`. If an exception escapes, `results.get` times out instead of reporting a clear failure.

---

## Known missing integrations (not new defects)

These are consistent with the documented holds:

- **No real containment.** Descendants can escape with `setsid`/double-fork. Supervisor SIGKILL or OOM can't be handled. A cgroup/systemd scope (`systemd-run --user --scope`) or PID namespace would be needed. This is why the queued hold must stay in place.
- **"CPU diagnostics only" is not enforced.** Local diagnostic argv can use GPUs. A cheap guard is to set `CUDA_VISIBLE_DEVICES=""` (and `HIP_VISIBLE_DEVICES=""`) in the child environment.
- **`submission.sha256` is not authentication.** It detects corruption only, since a same-user writer can rewrite both files. Provenance is acknowledged as not implemented.
- **The child inherits the supervisor's full environment.** By design this is not a sandbox, per the module docstring.

---

## Holds status

| Hold | Status |
|---|---|
| Official execution | Holds: blocked at `init-run` and re-checked in `_execute` |
| Queued execution | Bypassable under one config (HIGH-1) |
| Bootstrap | Not assessable; source not provided |

**Recommendation:** fix HIGH-1 and HIGH-2 before relying on the local diagnostic path, then add the five acceptance tests above.
