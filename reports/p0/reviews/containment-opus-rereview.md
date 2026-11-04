# Follow-up review: commit `7c1450f149ebf97644e581354379f934adea914a`

**Scope.** I reviewed the supplied text of:
- `scripts/researchctl.py`
- `scripts/run_registry.py`
- `tests/test_p0_ownership.py`
- `tests/test_p0_guards.py`

**Limits:**
- I did not run any tests. Every statement about behaviour comes from reading the code.
- I did not compare the supplied text against the Git objects for this commit.
- `docs/OPERATIONS.md` and the bootstrap scripts were not supplied this time. The bootstrap hold is not assessed.
- Scientific validity and live integrations (Pueue, Herdr) are not assessed.

**Summary.**
- **No CRITICAL or HIGH defects remain** in the supplied code.
- All three holds are intact: official, queued, and the legacy-queued-record hold.
- Prior HIGH-1, MEDIUM-2 and LOW-4 are **resolved**. HIGH-2 and MEDIUM-1 are **partially resolved**.
- **Open:** one actionable MEDIUM (a residual of HIGH-2) and several LOW items.

---

## Status of prior findings

| ID | Finding | Status | Evidence |
|---|---|---|---|
| HIGH-1 | `"local"` sentinel bypasses queued hold | **Resolved** | See below |
| HIGH-2 | SIGHUP/SIGQUIT/SIGTSTP orphan workload | **Partially resolved**. SIGHUP fixed; SIGQUIT is open (now MEDIUM-A) | See below |
| MEDIUM-1 | Normal leader exit leaves group alive | **Resolved**, with residual LOW-D | See below |
| MEDIUM-2 | Exceptions after spawn abandon child | **Resolved** | See below |
| LOW-1 | pgid reuse race | **Resolved**, assuming default SIGCHLD disposition (see LOW-E) | See below |
| LOW-2 | Signal window before handlers installed | **Open** | See below |
| LOW-3 | Official BLOCKED after claim strands `CLAIMED` | **Open** (still unreachable) | See below |
| LOW-4 | Launch failure `finish` from `DISPATCHING` | **Resolved** | See below |
| LOW-5 | Test hygiene | **Partially resolved** | See below |

**HIGH-1 evidence:**
- `require_supported_execution(..., submission=False)` now raises for any `queue is not None`. That includes `"local"` and `""`, which `if args.queue` would have treated as false.
- In `cmd_init_run` this runs before `folder.mkdir` and before any `pueue` call, so `--queue local` with `gpu_group: "local"` exits 2 without spawning anything.
- `execute` also refuses any `status.json` state other than `SUBMITTED`. This rejects legacy `QUEUED` records whose `queue` is `"local"`.
- `test_legacy_queued_local_record_cannot_execute` asserts that no execution happens and no `execution.claim` is created.

**HIGH-2 evidence:** `old_handlers` now traps `SIGTERM`, `SIGINT` and `SIGHUP`, and `test_sighup_stops_stubborn_descendant` covers SIGHUP. SIGQUIT is still not trapped, and the `PR_SET_PDEATHSIG` backstop was not added.

**MEDIUM-1 evidence:**
- The loop uses `os.waitid(P_PID, pid, WEXITED|WNOHANG|WNOWAIT)` to detect leader exit without reaping it.
- `stop_and_reap()` sends `killpg(SIGKILL)` while the zombie leader still pins the pgid, and only then calls `child.wait()`.
- `test_normal_leader_exit_stops_same_group_descendant` covers this.

**MEDIUM-2 evidence:**
- `except OSError` with a live child calls `stop_and_reap()` before re-raising.
- A new `except BaseException` catches `RegistryError` and `sqlite3.Error` from `advance` and does the same.
- An outer `finally: stop_and_reap()` acts as a backstop.
- `test_post_spawn_status_failure_kills_group_and_stays_uncertain` covers the `atomic_json` path and asserts that the registry stays `RUNNING` with a null result.

**LOW-1 evidence:** `signal_group` is gated on `group_owned`, which is cleared before `child.wait()`. No group signal can be sent after the leader is reaped.

**LOW-2 evidence:** Handlers are still installed after `advance(CLAIMED→DISPATCHING)`.

**LOW-3 evidence:** The official-mode branch after `claim` still writes `BLOCKED` to `status.json` only. It remains unreachable because `require_supported_execution(..., submission=True)` rejects non-diagnostic mode first.

**LOW-4 evidence:** `run_registry.finish` accepts `state IN ('RUNNING','DISPATCHING')`, so both the pre-spawn interrupt path and the `Popen` `OSError` path (code 127) can finish.

**LOW-5 evidence:** Some items are fixed and some remain. The details are in the Tests section.

---

## MEDIUM-A (open; residual of HIGH-2): SIGQUIT orphans a running workload

**Root cause.**
- SIGQUIT is not in the trapped set `(SIGTERM, SIGINT, SIGHUP)`. Its default action terminates the supervisor and dumps core.
- The child runs with `start_new_session=True`, so it is not in the terminal's foreground process group. Ctrl-\ therefore reaches only the supervisor.
- The process group keeps running with no supervisor. The registry stays `RUNNING` and `execution.claim` persists.
- Accounting fails closed: `reconcile` reports UNKNOWN. But nothing stops the workload.

**Why MEDIUM, not HIGH.** It takes a deliberate keystroke or an explicit signal. A pane or SSH hangup, the likely case, is now handled.

**Repro (code reading).**
1. Run `init-run ... -- sh -c 'sleep 300'` in a terminal.
2. Press Ctrl-\, or run `kill -QUIT <supervisor pid>`.
3. `sleep` survives and the registry stays `RUNNING`.

**Fix.**
- Add `signal.SIGQUIT` to the trapped tuple.
- Decide explicitly on `SIGTSTP`. A stopped supervisor cannot propagate a later interrupt to the group. Ignoring it (`SIG_IGN`) is the simplest choice.
- Optional backstop: set `PR_SET_PDEATHSIG(SIGKILL)` in a `preexec_fn`. This covers the direct child only.

**Acceptance test.** Parameterise `_cancel_stubborn_descendant` with `signal.SIGQUIT` and assert:
- the grandchild is gone within 5 s;
- the registry state is `INTERRUPTED`.

---

## LOW (new or residual)

**LOW-A: Defense in depth for the queued hold.**
- `run_registry.claim` still accepts `initial_state == 'QUEUED'`, so the queued hold is enforced only in `researchctl.execute`.
- The local/queued distinction for a record depends on `status.json`. That file is a projection and is not covered by `submission.sha256`.
- Within the stated same-user trust model this is not exploitable.

**Fix:**
- Have `claim` reject `QUEUED` while the hold is active.
- Record an explicit `"dispatch": "local"` field in the hashed submission.

**LOW-B: Signal window and stranded claim (prior LOW-2, still open).**
- Under the Herdr/SSH model, a SIGHUP between `claim` and handler installation kills the supervisor. The run is left in `CLAIMED`/`DISPATCHING` with `execution.claim` present.
- No child exists, so this is safe, but it needs manual reconciliation.

**Fix:** install the handlers before `claim`. A signal that arrives before spawn already maps to `INTERRUPTED` via the existing `if interrupted:` branch.

**LOW-C: Unvalidated `argv` from the manifest.**
- In `execute`, a non-list, empty or non-string `argv` makes `Popen` raise `TypeError`/`IndexError`. That is pre-spawn, so it goes through the `BaseException` branch.
- `main` does not catch either type, so the user gets a traceback, exit code 1, and a registry stuck in `DISPATCHING`.
- It is reachable only through a manifest whose hash was rewritten.

**Fix:** before `claim`, validate that `argv` is a non-empty list of `str` and raise `ResearchError` otherwise.

**LOW-D: Survivor cleanup is silent (residual of MEDIUM-1).**
- When the leader exits by itself, any surviving same-group workers are SIGKILLed with no grace period.
- The result is still recorded as a plain `COMPLETED`/`FAILED`, with no field showing that cleanup killed anything. Outputs those workers were writing may be truncated while the result looks clean.

**Fix:**
- Before the SIGKILL, check `/proc/*/stat` for other members of the pgid.
- Record `"descendants_killed": true|false`.
- Optionally send SIGTERM and allow a short grace period first.

**LOW-E: The `WNOWAIT` pinning assumes SIGCHLD is not ignored.**
- If the supervisor inherits `SIGCHLD = SIG_IGN`, the kernel auto-reaps the child, so the zombie never pins the pgid.
- `waitid` then raises `ChildProcessError`, and `stop_and_reap()` sends `killpg(SIGKILL)` to a pgid that is no longer pinned.
- This is unlikely from an interactive shell, and the queued path is blocked.

**Fix:** call `signal.signal(signal.SIGCHLD, signal.SIG_DFL)` at the top of `execute`.

**LOW-F: `cmd_status` aborts on one corrupt registry.**
- `run_registry.inspect` can raise `sqlite3.DatabaseError`, which the per-folder `except (OSError, json.JSONDecodeError)` does not catch.
- One bad `registry.sqlite3` aborts the whole listing.

**Fix:** catch `sqlite3.Error` per folder.

---

## Tests (findings from reading; nothing executed)

**Added and adequate by reading:**
- SIGHUP cancellation
- Normal leader exit kills same-group descendants
- `atomic_json` failure after spawn kills the group and leaves the registry uncertain
- The explicit-`local` guard
- The legacy-`QUEUED` refusal

**Still open:**
- **Cancellation tests assert on `status.json`** (a projection), not on `run_registry.inspect(folder)['state']`.
- **`test_twenty_workers_have_one_owner` still writes to `os.environ` globally** without restoring it. The newer tests correctly use `patch.dict`.
- **The ownership test's `worker` doesn't catch `RegistryError` or `sqlite3.Error`.** Those can escape `advance` and `finish` unwrapped, and the test then fails as `queue.Empty` instead of with a clear message.

**Missing acceptance tests:**
1. SIGQUIT cancellation (MEDIUM-A).
2. `run_registry.advance` raising on `DISPATCHING→RUNNING` after spawn. The `BaseException` branch currently has no test.
3. An interrupt delivered before spawn. This should produce `INTERRUPTED` with a `finish` from `DISPATCHING`, and no child.
4. End-to-end `init-run --queue local` with `gpu_group: "local"` and a fake `pueue` on `PATH` that records calls. Assert exit code 2 and no call. Today only the guard function is unit-tested.

---

## Known missing integrations (unchanged; not new defects)

- **Containment.** Descendants can escape `killpg` via `setsid` or `setpgid` (for example `bash -m`, or job-control shells). Supervisor SIGKILL or OOM cannot be handled. Real containment needs a cgroup/systemd scope or a PID namespace.
- **"Diagnostic = CPU" is not enforced.** There is no `CUDA_VISIBLE_DEVICES=""` or `HIP_VISIBLE_DEVICES=""` in the child environment.
- **`submission.sha256` detects corruption only; it is not authentication.**
  - There is a small TOCTOU gap: `load_json` and `sha_file` each read the file separately.
  - Provenance is not implemented.

---

## Holds status

| Hold | Status | Evidence |
|---|---|---|
| Official execution | **Holds** | `require_supported_execution` raises for `mode != "diagnostic"` in both `cmd_init_run` and `execute` |
| Queued execution | **Holds** | Any non-`None` `--queue` is rejected before folder creation or `pueue`. `execute` rejects `queue != "local"` and any state other than `SUBMITTED`. Legacy `QUEUED`/`"local"` records are refused before claim |
| Bootstrap | **Not assessed** | Source not supplied |

**Recommendation:**
- Fix MEDIUM-A (trap SIGQUIT, decide on SIGTSTP) and add its test.
- Add missing tests 2–4, and move the cancellation tests to assert on registry state.
- Before any hold is lifted, address LOW-A, LOW-B and LOW-3.
