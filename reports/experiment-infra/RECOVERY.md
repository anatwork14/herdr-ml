# RECOVERY: fenced orphan and interrupted-launch recovery, verified CPU termination

Phase 5. Branch `feat/exp-phase5-recovery` from `feat/exp-integration` at `345e5b8`.

**EXPERIMENT_RECOVERY_READY: BLOCKED.** No live containment exists on this host (the production backend is `UNAVAILABLE` and no verifier is registered), so release is always refused and no run can be completed by recovery. Nothing here authorizes official execution, queue dispatch or GPU work. `PRODUCTION_DISPATCH_ENABLED` stays `False`, and the researchctl queued path stays blocked (guard unchanged). The W2 simulator is not used by this phase.

Implementation: `scripts/run_recovery.py` (new), `scripts/run_registry.py` (`recovery_fence`, `queue_fence_unlaunched`, two after-commit hooks), `scripts/run_ownership.py` (state `RECOVERY_FENCED`, five crash-point names). Tests: `tests/test_recovery.py`, helper `tests/support/process_tree.py`.

## 1. Stores and authority

| Store | Content | Writer |
|---|---|---|
| `registry.sqlite3` (run-local) | Run row, leader child identity | Owner token; `recovery_fence` (owner store, CAS on observed owner) |
| `recovery.sqlite3` (run-local, new, mode 0600) | `descendants` (pid, boot id, start ticks, pid namespace, role, attempt, fence), `recovery_events` | `record_descendants` holds the registry write lock and verifies the current owner token while it commits, so a fenced worker cannot add a descendant after recovery |
| `queue-registry.sqlite3` | Queue binding | Unchanged protocol, plus `queue_fence_unlaunched` |

## 2. State transitions per scenario

`recover(folder)` runs in this order: observe owner, fence the local row, terminate recorded descendants, evaluate release, then queue recovery. It never resubmits, re-launches, reclaims or completes.

| Scenario | Before | Owner observation | After recovery |
|---|---|---|---|
| Orphaned claim (item 1) | local `CLAIMED`, no child | GONE (`NOT_VISIBLE`, start time differs, zombie, or boot differs) | local `RECOVERY_FENCED`, fence+1; queue `CLAIMED` to `AMBIGUOUS` (origin CLAIMED); marker kept; release refused (`NO_RECORDED_DESCENDANTS`, `CONTAINMENT_UNAVAILABLE`) |
| Claim with live owner | local `CLAIMED` | LIVE (pid and start time match) | unchanged; event `RECOVERY_DEFERRED`; nothing signalled |
| Owner not observable | any recoverable | UNKNOWN (host or namespace differs, procfs unreadable) | unchanged; event `RECOVERY_DEFERRED` |
| Interrupted launch after intent (item 2) | queue `INTENT`, no local row | n/a | queue `AMBIGUOUS` (origin INTENT); `execution.claim` marker created (`LAUNCH_FENCED`), so no local or queued claim can start the run |
| Interrupted launch after bind (item 2) | queue `BOUND`, no local row | n/a | queue `AMBIGUOUS` (origin BOUND, new `queue_fence_unlaunched`); marker created; no resolution path |
| Crash with child running | local `RUNNING` | GONE | `RECOVERY_FENCED`; recorded descendants terminated and verified gone; release refused (`CONTAINMENT_UNAVAILABLE`); queue `AMBIGUOUS` (origin CLAIMED) |
| Crash after leader outcome recorded | local `UNKNOWN` | GONE | same as above; `result_json` keeps `state: UNKNOWN` |
| Stale worker (item 3) | any | n/a | stale token, successor token `v2.<attempt>.<fence+1>` and a foreign attempt are all refused by `advance`, `record_child`, `finish`, `verified_finish`, `supersede`, `record_descendants`, `release_run`; the row and event count are unchanged |
| Containment unknown or unavailable (item 4) | any | n/a | `release_gate` adds `CONTAINMENT_UNKNOWN` or `CONTAINMENT_UNAVAILABLE`; recovery records `RELEASE_REFUSED`; the run state never enters `COMPLETED`, `FAILED` or `INTERRUPTED` |

`RECOVERY_FENCED` is terminal for every mutator: each mutator requires a state that it lacks. It is distinct from `FENCED`, which still requires a `VerifiedProof` of whole-workload termination. `recovery_fence` refuses when the row's owner pid, boot or start time differs from the one recovery observed, so a row that changed after observation is never fenced by mistake.

Release (`release_run`, owner token, state `UNKNOWN` only) and the recovery path both use `release_gate(identities, containment)`. Every refusal reason is recorded:

1. `NO_RECORDED_DESCENDANTS`: nothing was recorded, so nothing can be verified.
2. `DESCENDANT_NOT_VERIFIED_GONE`: some recorded identity is `ALIVE` or `UNVERIFIED`.
3. `CONTAINMENT_<status>`: `containment_backend.is_verified_for_execution` is false. It is always false here, because `_VERIFIED_ISSUERS` is empty and `ContainmentResult(VERIFIED)` cannot be constructed.

Release never changes the run state. The allowed branch (`RELEASE_GATE_PASSED`) cannot be reached on this host.

## 3. Tested crash boundaries

Each case runs a synthetic queued worker in a child process (intent, external submission, bind, queue claim, local claim, dispatch, real process tree, record child, RUNNING, record descendants, leader exit, finish, release). The hook calls `os._exit(77)` at the named point, so no `finally` or rollback runs. The test then runs `recover` twice and asserts the allowed end state. Each boundary runs 3 times (`REPEATS = 3`, each in a fresh temp dir).

| Boundary | Hook | State left by crash | Allowed state after recovery | Runs |
|---|---|---|---|---|
| after intent | `queue_intent:after_commit` | queue INTENT | queue AMBIGUOUS (INTENT), no local row, 0 submissions | 3/3 |
| after bind | `queue_bind_task:after_commit` (new) | queue BOUND | queue AMBIGUOUS (BOUND), no local row, 1 submission | 3/3 |
| after claim | `claim:after_commit` | local CLAIMED | RECOVERY_FENCED fence 2, queue AMBIGUOUS (CLAIMED), no child | 3/3 |
| after child start | `descendants:after_commit` (new) | local RUNNING, tree alive | RECOVERY_FENCED, 3 identities terminated and verified gone, release refused | 3/3 |
| after completion record | `finish:after_commit` (new) | local UNKNOWN, descendants alive | same | 3/3 |
| before release | `release:begin` (new) | local UNKNOWN, descendants alive | same | 3/3 |

Every case also asserts: no new submission, `queue_intent` for the same run refused, `queue_claim` refused, local `SUBMITTED` and `QUEUED` claims refused, a second `recover` gives the same states, and `pgrep -f <marker>` is empty. A control run with no crash shows that the worker's own release is refused with `DESCENDANT_NOT_VERIFIED_GONE` and `CONTAINMENT_UNAVAILABLE` while the orphaned child and grandchild are alive. `recovery_fence:before_commit` (new) is tested in-process: an interrupted fence commit leaves the prior row.

## 4. Process termination method and limits

Test workload (`tests/support/process_tree.py`), in a private temp directory: a parent that exits 0 after its child is ready, a child that sleeps, and a grandchild that ignores SIGTERM. Each process publishes its own identity from `/proc/self/stat` (start ticks, field 22), the boot id and its pid namespace. Each sleeps at most 120 s, so any leak is bounded.

Method (`terminate_recorded`):
1. `observe(identity)` reads `/proc/<pid>/stat`. A missing PID is `GONE`. A different start time is `GONE` (PID reused; that process is never signalled). A different boot id is `GONE`. State Z or X is `ZOMBIE`, which counts as gone. A malformed identity, a different pid namespace or unreadable procfs is `UNVERIFIED`.
2. To signal, `os.pidfd_open(pid)` comes first, then the identity is re-checked, then `signal.pidfd_send_signal`. If the process dies and the PID is reused after the check, the send fails with ESRCH and does not reach the new process.
3. SIGTERM goes to every `ALIVE` identity. After the grace period, SIGKILL goes to any identity still alive (unless `escalate=False`). Then every identity is observed again. `all_gone` is true only if each one is `GONE` or `ZOMBIE`.

Result (ISOLATED_CPU_VERIFIED, this host, Python 3.12, kernel 5.4): with SIGTERM only, the child is gone and the grandchild stays `ALIVE`, so release is refused with `DESCENDANT_NOT_VERIFIED_GONE`. After escalation the grandchild receives SIGKILL by its recorded identity, every descendant is verified gone, and release is still refused, but only for `CONTAINMENT_UNAVAILABLE`. A live process whose recorded start time is off by one tick is reported `GONE` and receives no signal. An unrecorded process is never signalled.

Limits:
- No cgroup is used. There is no `cgroup.kill` and no `cgroup.procs` enumeration. Only recorded identities are terminated. A descendant that was never recorded (for example, one forked after the last record, or one that detached before it) cannot be found. A crash between spawning the leader and `record_child`/`record_descendants` leaves processes that recovery cannot see.
- Identity is pid plus start time plus boot id. This is an observation, not containment proof.
- Escape from a cgroup cannot be tested on this host: **BLOCKED** (no delegated cgroup; see CONTAINMENT.md and OPERATOR_REQUIREMENTS.md).
- Zombies count as gone. Orphans are reaped by the system's reaper, not by recovery.
- Single host and a single pid namespace only.

## 5. Status per item

| Item | Status |
|---|---|
| 1. Orphaned claims to RECOVERY_FENCED, never reset to ready | OFFLINE_TESTED (real crashed worker processes) |
| 2. Interrupted launches (INTENT, BOUND) to AMBIGUOUS plus local marker, never re-launched | OFFLINE_TESTED |
| 3. Stale fence or attempt cannot write completion or release | OFFLINE_TESTED |
| 4. UNKNOWN or UNAVAILABLE containment blocks release; recovery records it and never completes | OFFLINE_TESTED |
| 5. Verified whole-workload termination (recorded identities) before release | ISOLATED_CPU_VERIFIED (real parent, child and SIGTERM-ignoring grandchild) |
| 6. Deterministic crash injection at 6 boundaries, 3 runs each | ISOLATED_CPU_VERIFIED |
| Cgroup-level containment, escape detection, kernel-enumerated occupancy | BLOCKED |
| EXPERIMENT_RECOVERY_READY | BLOCKED (no live containment) |

## 6. Residual risks

1. The local `SUBMITTED` claim path does not read the queue index. Recovery closes this for an interrupted launch by creating the `execution.claim` marker. Otherwise, researchctl's status check (a queued run is never `SUBMITTED`) is still what separates the two paths.
2. Quiescence is now enforced in code (was documentation only). `queue_intent` and `queue_bind_task` record the submitter's identity (pid, boot, start time, namespaces, host) in `queue_submitters`, under the queue write lock. Before `queue_fence_unlaunched`, `queue_fence_launch` (also creates the `execution.claim` marker) or `queue_recover(require_quiescent=True)` (used by `recover`) writes a fence, `_launch_quiescence` checks every recorded submitter (INTENT/BOUND origin), the queue claimant and any local owner. If any is live, or its liveness cannot be determined (including a row with no submitter record), the call raises `RecoveryDeferred` (`RECOVERY_DEFERRED`) and writes nothing; `recover` reports it in `queue_deferred`. Lock: the check and the write run in one `BEGIN IMMEDIATE` transaction on `queue-registry.sqlite3`, the same lock under which submitters and claimants are recorded. The claimant record now carries its pid and mount namespaces (`pid:boot:start|pid_ns|mnt_ns`), and an observer in a different pid namespace sees UNKNOWN and defers instead of reading a live claimant as GONE. Legacy three-field claimants still assume the same pid namespace.
5. Descendant lineage is verified before recording. `record_descendants` accepts an identity only if it is the recorded leader, or its current `/proc` session id equals the leader's (a session match counts only when the leader leads its own session, that is, the leader's pid equals its session id), or its parent chain reaches the leader (pid and start time). The leader must still be observable (running or an unreaped zombie), so descendants must be recorded before the leader is reaped. One outsider refuses the whole call and nothing is recorded, so recovery never signals it.
3. `recovery.sqlite3` and `registry.sqlite3` are separate files. Lock ordering (registry first) keeps descendant recording and fencing serial. A crash between the two commits leaves descendants recorded under a row that is still current, which is safe.
4. Owner liveness uses `process_identity.observe_process`. An owner that cannot be observed keeps its authority, and recovery does nothing until an operator decides.

## 7. Verification

Command: `/mnt/ssd/genos2/.local/bin/python3.12 -m unittest discover -s tests` (results in section 8). Mutation: deleting the `DESCENDANT_NOT_VERIFIED_GONE` refusal in `release_gate` makes `test_release_refused_until_every_recorded_descendant_is_verified_gone`, `test_uninterrupted_worker_release_is_refused_and_recovery_cleans_up` and `test_malformed_or_foreign_namespace_identity_is_unverified_and_blocks_release` fail (5 failures, including subtests, out of 22). Restoring it makes all 22 pass. `git diff 345e5b8 -- tests/test_registry_p0.py tests/test_queue_protocol.py` is empty.

## 8. Results

- Full suite: Ran 432, OK, exit 0. That is 404 baseline tests plus 28 in `tests/test_recovery.py` (6 added for quiescence, lineage and the pidfd re-check).
- Mutation: making `_launch_quiescence` always report quiescent fails `test_live_submitter_blocks_fence_and_leaves_bytes_unchanged` and `test_submitter_holding_the_queue_lock_is_seen_after_it_commits`; restoring it passes all 28.
- The PID-reuse re-check after `pidfd_open` is exercised directly: `observe` is patched to match before `pidfd_open` and mismatch after; no signal is sent and the process stays alive.
- After the suite, `pgrep -af 'herdr-recovery-tes[t]-'` and `pgrep -af 'tree\.py (parent|child|grandchild)'` both exit 1 (no processes).
- Each test's `tearDown` terminates any survivor by its recorded identity only, then asserts that `pgrep -f <marker>` is empty. The marker is unique to that test.
