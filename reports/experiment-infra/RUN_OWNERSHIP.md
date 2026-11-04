# RUN_OWNERSHIP: execution identity, fencing and cross-database recovery

Phase 1 (P1.2), worker W1. Gate status: **OFFLINE_TESTED** for the offline protocol tests in this document only (full suite: 185 tests OK, see section 10). Live queue integration, live containment and cross-host operation remain **BLOCKED**. Nothing here authorizes official execution, queue dispatch or GPU work.

Implementation: `scripts/run_ownership.py` (vocabulary and proof shapes), `scripts/run_registry.py` (SQLite transactions), `scripts/researchctl.py` (test-only hook at named boundaries). Tests: `tests/test_run_ownership.py`, `tests/test_run_ownership_faults.py`.

## 1. Authorities and non-claims

| Store | Authority | Notes |
|---|---|---|
| Run-local `registry.sqlite3` (one per run folder) | Execution ownership and outcome | Single `run` row; authoritative for owner, attempt, fence and state. |
| Shared queue index `queue-registry.sqlite3` (per account) | Queue binding and queue-side claim | Separate SQLite file. It cannot commit atomically with the run-local file. |
| `status.json`, `result.json`, logs | Projections | Never consulted for ownership decisions. |
| Process identity (PID, boot, start ticks) | Observation only | A match is not containment proof or authority. A mismatch or absent PID is never treated as termination or permission to reclaim. |

A successful leader exit is not completion. Whole-workload termination is a **required proof input**: a `VerifiedProof` of kind `WHOLE_WORKLOAD_TERMINATED`, minted only by `issue_proof()` after the verifier registered for its issuer accepts raw evidence. `_VERIFIED_ISSUERS` in `scripts/run_ownership.py` is empty, so no production path can obtain a proof, and production code cannot reach `COMPLETED`, `FAILED`, `INTERRUPTED` or `FENCED` today. Plain dicts, hand-built objects, unregistered issuers and verifiers that return anything other than `True` are refused. This is an in-process guard against accidental or naive forgery. It is not a security boundary against code running in the same process, and this document does not claim more than that. In-process forgery (`object.__new__`, reading `_ISSUE_KEY`, mutating `_VERIFIED_ISSUERS`) is out of scope and is not defended against.

## 2. Execution identity

Canonical record (`ExecutionIdentity`, `version: 2`), serialized with sorted keys and hashed as `identity_sha256`:

| Field | Source | Validation |
|---|---|---|
| `run_id` | Run folder name | Name pattern `[A-Za-z0-9][A-Za-z0-9._-]{0,254}` |
| `project` | Run folder project component (queue); claim argument (local) | Name pattern |
| `attempt_id` | Issued by owner store (local) or by the queue claim (queued) | 32 lowercase hex, uuid4 |
| `manifest_sha256` | Submission manifest digest | 64 lowercase hex. W4 will place its manifest digest here. Placeholders are rejected, so an unbound run cannot be claimed. |
| `worker_id` | Local: `local-<pid>`; queued: worker identity from the queue | Name pattern |
| `task_id` | Queue response; absent for local runs | Opaque, at most 256 characters, no NUL |
| `fence` | Owner store | Positive integer. Local first claim is 1. A queued claim takes `fence+1` on the queue row. A supersession increments it. |
| `identity_version`, `identity_sha256` | Owner store | A row is `CURRENT` only if the stored hash recomputes exactly. |

Token: `v2.<attempt_id>.<fence>`. Every mutating call decodes it first. A malformed token raises `Stale owner or malformed fencing token`.

Stale-token rule: `record_child`, `advance`, `finish`, `verified_finish` and `supersede` each run in one `BEGIN IMMEDIATE` transaction. The transaction reads the single `run` row, requires owner, attempt and fence to equal the token, and requires `classify_row == CURRENT` (the identity hash recomputes). Only then does it compare-and-set the state. A replayed, superseded, forged or tampered token matches no current row and is refused. Mutators never create `registry.sqlite3` or `execution.claim`: a refusal on an empty folder leaves no file.

Queue calls are keyed by intent token, run folder and phase (`INTENT`, `BOUND`, `CLAIMED`). Before a claim exists there is no fence, so these calls cannot use one.

Project scope:
- A queue scope is the run folder's project component. An explicit `project=` must agree with it.
- A `run_id` already bound to a different project is refused (`another project`).
- A task ID is unique per queue instance (`UNIQUE(queue_instance, task_id)`).

## 3. Run-local state machine

| From | To | Actor | Preconditions | Audit record (same transaction) |
|---|---|---|---|---|
| (none, PREPARED) | CLAIMED | Worker process | `SUBMITTED` status; `execution.claim` marker created (`mkdir`); no run row. For queued runs also: queue row `CLAIMED` with identical identity fields and the same claimant process. | `events`: `CLAIMED` with attempt, fence and identity hash |
| CLAIMED | DISPATCHING | Owner token | Token, attempt, fence and state match | `events` |
| DISPATCHING | RUNNING | Owner token | Same, plus a recorded child identity. **RUNNING without a recorded child is refused** (enforced in `advance`). | `events`, `child_identity` |
| DISPATCHING or RUNNING | UNKNOWN | Owner token | Leader outcome observed. Whole-workload termination **not** verified. This is the ambiguous or unknown outcome. | `events`, `result_json` |
| DISPATCHING or RUNNING | COMPLETED / FAILED / INTERRUPTED | Owner token plus containment | `VerifiedProof` `WHOLE_WORKLOAD_TERMINATED` bound to run, attempt and fence (section 1). No verifier is registered, so unreachable in this phase. UNKNOWN never reaches COMPLETED. | `events`, `result_json` |
| CLAIMED, DISPATCHING, RUNNING or UNKNOWN | FENCED | Owner store plus containment | Proof as above. Fence increments and the owner token changes to `v2.<attempt>.<fence+1>`. FENCED is terminal for every mutator: that derivable successor token is inert and is tested as such. A stale holder can no longer act. No new claim is possible for this `run_id`. | `events` with previous and new fence |
| CLAIMED, DISPATCHING, RUNNING | CANCEL_REQUESTED | Owner | **Specified, not durable in this phase.** The signal handler sets an in-memory flag only. | none yet |
| any unfinished | RELEASED | Containment | **Specified, not implemented.** Resource release is not verified. | none yet |

Legacy rows (no v2 identity, or a hash that does not recompute) are history only. They load as `UNKNOWN` with `legacy_state` preserved and `identity = LEGACY_FENCED`. Every mutator refuses them with `RegistryError`, never a raw `sqlite3` error, and `claim` against an existing row is always refused. No legacy row reaches `COMPLETED`, including one with injected owner, attempt and fence values.

## 4. Queue index state machine

| From | To | Actor | Preconditions | Audit record |
|---|---|---|---|---|
| (none) | INTENT | Submitter | Valid identifiers and digest; folder project matches; `run_id` unbound or bound to the same project | `queue_events` INTENT with intent token. **Committed before any external submission.** |
| INTENT | BOUND | Submitter | Intent token current; task ID unbound in this queue instance | `queue_events` BOUND with task ID |
| INTENT | AMBIGUOUS (origin INTENT) | Submitter on lost or uncertain response; restart recovery | Phase is INTENT | `queue_events` AMBIGUOUS with origin |
| BOUND | CLAIMED | Worker | Run, digest, queue instance, task ID and phase match. Issues attempt and fence `+1`. Records claimant `pid:boot:start`. | `queue_events` CLAIMED |
| CLAIMED | AMBIGUOUS (origin CLAIMED) | Restart recovery; or the claimant when the local step fails after verification | No matching `CURRENT` local record | `queue_events` AMBIGUOUS with origin and reason |
| AMBIGUOUS (origin INTENT) | BOUND | Reconciler | `QUEUE_LOOKUP_TASK` evidence bound to intent token, queue instance and task ID | `queue_events` with evidence |
| AMBIGUOUS (origin INTENT) | RETRYABLE | Reconciler | `QUEUE_LOOKUP_NO_TASK` evidence bound to intent token and queue instance. `RETRYABLE` never reopens the same `run_id`; a new run identity is required. | `queue_events` with evidence |
| AMBIGUOUS (origin CLAIMED) | none in this phase | Containment | Requires containment-verified termination of the local attempt. Queue evidence is refused. | n/a |

Lookup evidence must be a `VerifiedProof` (section 1). No production verifier is registered and the lookup that would produce evidence is not implemented. `QUEUE_LOOKUP_TASK` takes its task ID only from the verified proof, and a plain dict carrying a task ID is refused.

## 5. Cross-database protocol (queued runs)

Order of durable writes for one queued run:

1. `queue_intent`: commit INTENT in the queue index. Only then perform the external submission.
2. External submission returns a task ID. `queue_bind_task`: commit BOUND.
3. `queue_claim`: commit CLAIMED. Issues attempt and fence, and records the claimant process.
4. `claim` (local): take `BEGIN IMMEDIATE` on the queue index and verify the CLAIMED row against every presented field and against this process's claimant. Then create the marker, `BEGIN IMMEDIATE` on the local registry, insert the run row, and commit it. Then commit the queue transaction. A failure after verification marks the queue row AMBIGUOUS (origin CLAIMED) before the error propagates.
5. The child is launched only after step 4 returns.

Restart recovery (`queue_recover`), run before any worker may act:
- INTENT becomes AMBIGUOUS (origin INTENT).
- CLAIMED with no matching `CURRENT` local record becomes AMBIGUOUS (origin CLAIMED).
- CLAIMED with a matching local record is left unchanged.
- Recovery never resubmits, retries, launches or reclaims.

Why this is conservative: a crash between two commits looks the same as a crash just before the second commit, so the only safe state is fenced. Claimant identity means a restarted or different process cannot complete step 4 for a claim it did not make, so the claim cannot resume before recovery runs. The claimant check is an observation of this process, not authority. The queue lock is held across the local commit, so recovery cannot fence the row in between.

Failure to find a worker's own record is never a reason to retry an uncertain submission.

## 6. Crash-boundary table

Hooks are passed explicitly by tests (`_fault=`). They are not environment variables or CLI flags. Unknown hook names raise. Each fault test runs a synthetic driver in a child process that calls `os._exit(77)` at the named point, so no `finally` or rollback runs in Python. SQLite recovers from its journal on the next open.

| Boundary | Hook point | Test | Recovered state |
|---|---|---|---|
| Before intent is durable | `queue_intent:before_commit` | `test_crash_before_intent_is_durable_leaves_nothing_to_resume` | No row; no submission; nothing to resume |
| After intent, before submission | `queue_intent:after_commit` | `test_crash_after_intent_before_submission_is_fenced_ambiguous` | INTENT, then AMBIGUOUS after recovery; bind and claim refused; no submission |
| After submission, before task ID recorded | `queue_bind_task:before_commit` | `test_crash_after_submission_before_task_id_recorded_needs_recorded_evidence` | AMBIGUOUS; resolved only by task evidence; no second submission |
| After global queue claim, before local claim | `queue_claim:after_commit` | `test_crash_after_global_claim_before_local_claim_is_refused_then_fenced` | CLAIMED with no local record; another claimant refused; AMBIGUOUS after recovery |
| During local claim, before commit (queued) | `claim:before_commit` | `test_crash_during_local_claim_before_commit_is_fenced_not_retried` | Queue CLAIMED; no local row; AMBIGUOUS after recovery; claim refused |
| During local claim, before commit (local run) | `claim:before_commit` | `test_crash_during_local_claim_for_local_run_leaves_marker_and_no_row` | Marker without row; every further claim refused |
| After local claim, before launch (queued) | `claim:after_commit` | `test_crash_after_local_claim_before_launch_stays_claimed_and_fenced` | Queue and local CLAIMED; no child; claim refused |
| After local claim, before launch (researchctl) | `execute:claimed` | `test_crash_after_local_claim_before_launch_never_spawns_child` | CLAIMED; no child; no rerun |
| During execution | `execute:child_running` | `test_crash_during_execution_leaves_running_and_never_completes` | RUNNING; child identity recorded; no result; no rerun |
| After leader exit, before containment verification | `execute:leader_exited` | `test_crash_after_leader_exit_before_verification_is_not_completed` | RUNNING; no result written; no rerun |
| During resource release | `execute:before_release` | `test_crash_during_resource_release_records_unknown_not_completed` | UNKNOWN with leader outcome only; no `result.json`; no rerun |
| Interrupted commit, advance | `advance:before_commit` | `test_interrupted_commit_leaves_prior_consistent_state` (in-process) | Prior state (DISPATCHING) kept |
| Interrupted commit, finish | `finish:before_commit` | `test_interrupted_finish_commit_keeps_running_and_writes_no_result` (in-process) | RUNNING kept; no result written |

Test names are in `tests/test_run_ownership_faults.py` and `tests/test_run_ownership.py`.
The in-process claim-failure branch (queued claim, exception raised at `claim:before_commit`, queue row fenced AMBIGUOUS with origin CLAIMED) is tested by `test_claim_failure_after_queue_verification_fences_the_queue_row_in_process`.

## 7. Legacy, mixed and corrupt stores

- Legacy run row: loads as `UNKNOWN` (`LEGACY_FENCED`) and is never claimable. Test: `test_legacy_rows_load_as_fenced_unknown_and_are_never_claimable`.
- Tampered identity hash: `LEGACY_FENCED`. Test: `test_identity_columns_with_tampered_hash_are_legacy_fenced`.
- Mixed legacy and new records: each classified independently. Test: `test_mixed_legacy_and_new_records_are_each_classified_correctly`.
- Legacy queue index (no `claimant` column): open is refused. The index is not migrated automatically. Test: `test_legacy_queue_index_schema_fails_closed`.
- Corrupt run or queue file: refused, and the bytes are unchanged. Tests: `test_corrupt_registry_fails_closed_and_is_not_reset`, `test_corrupt_queue_index_fails_closed_without_reset`.
- Run-local inspection (`inspect`) opens the registry read-only, so it never creates tables or migrates. Queue inspection (`inspect_queue`) uses the write-capable open path and may create the owner-only store.

## 8. Known deviations and open items (for reviewer decision)

1. **Closed (M2).** A local claim must match the run folder project on both the SUBMITTED and queued paths. A mismatch is refused before any marker or registry file is created. P0 input change: `tests/test_queue_protocol.py` claim calls use `project='project-a'` (4 calls, lines 127, 169, 172, 175; assertions unchanged). `tests/test_registry_p0.py` fixture changes were authorised by the owner and are in their own commit: setUp and CrashTests use the standard run-folder layout, and fixtures call `record_child` before RUNNING (L1). No assertion, expected value, test name or test set changed.
2. `CANCEL_REQUESTED` is not durable. The signal handler sets an in-memory flag only.
3. `RELEASED` and resource-release verification are not implemented.
4. `researchctl` still refuses queued execution (guard unchanged). `queue_*` functions are not called by `researchctl`.
5. Queue lookup evidence (`QUEUE_LOOKUP_*`) has no producer.
6. Test-only parameters `_fault` and `_store_home` exist on the registry and queue functions, including `claim`. The public signature of `claim` has no queue store parameter. Production callers pass `None`. `_store_home` changes where the queue index lives, so it must never be wired to user input.

## 9. Not covered

- Live Pueue or any queue daemon; live containment (no backend registered); GPU work.
- Cross-host, cross-user and NFS semantics. Only one local POSIX filesystem is assumed.
- Power loss beyond SQLite `synchronous=FULL` and directory fsync on this host.
- Malicious local user with write access to the queue store (the store is owner-only, mode 0600 under a 0700 directory, but not hardened against the owner).
- Manifest digest content (W4), provenance and approvals.
- Correctness of any experiment result.

## 10. Verification

Logs: `reports/experiment-infra/w1-logs/`. Each file records the exact command and exit code at the top.

- `full-suite.log`: full suite with the baseline 120 tests plus the new ownership tests.
- `ownership-unit.log`: `tests/test_run_ownership.py`.
- `ownership-faults.log`: `tests/test_run_ownership_faults.py`.

Concurrency tests: the local claim race uses 10 threads, the queued claim race uses 8 threads, and the P0 cross-process race uses 20 processes. Gate: OFFLINE_TESTED, backed by `w1-logs/full-suite.log` (185 tests OK). Live integration remains BLOCKED. The gate matrix files were not edited in this phase.
