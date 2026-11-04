# QUEUE: identity binding, durable intent and private Pueue adapter

Phase 3, worker W3 (P3.1, P3.2, P3.3). Branch `feat/exp-w3-queue` from `feat/exp-w1-ownership` at `ef673ac`.
Nothing here authorizes official execution, production queue dispatch or GPU work. `researchctl` still refuses queued execution (guard unchanged, and it does not import the adapter).

## 1. What changed

| File | Change |
|---|---|
| `scripts/queue_adapter.py` (new) | `PrivatePueue` (pinned, private daemon only), `submit`, `release`, `claim_attempt`, `bound_identity`, `reconcile`. `PRODUCTION_DISPATCH_ENABLED = False`. |
| `scripts/run_registry.py` | Adds `queue_claim_idempotent`. Existing functions and `queue_claim` are unchanged. |
| `scripts/pueue_cpu_worker_integration.py` | Rewritten on the adapter. Project `cpu-integration` matches the folder `.runtime/cpu-integration/runs/<run_id>`. The worker calls `record_child` before RUNNING. The socket is relative, so long temp paths stay under the unix socket length limit (the old harness failed with `SUN_LEN` under long `TMPDIR`). Same CLI (`--pueue --pueued --python`). |
| `tests/test_queue_adapter.py` (new) | 10 offline tests and 11 private-daemon tests. |
| `reports/experiment-infra/w3-logs/` | Logs, each with the command and exit code. |

## 2. Behaviour

**Queue identity (P3.1).** The queue instance is the sha256 of the private daemon's `data/certs/daemon.cert`. It stays the same when the same state directory is restarted, and it changes if the state is replaced. The queue row binds (instance, task ID) to run_id, project and manifest. A claim then issues attempt and fence, and the run-local row records task, worker, attempt and fence under the identity hash. `bound_identity` returns the binding only when both stores agree and the local row is `CURRENT`. The Pueue task label `herdr-ml:<run_id>:<intent_token>` ties the daemon task to the intent.

**Durable intent (P3.2).** `submit` commits INTENT before it calls `pueue add --stashed`. A test observes the store at the moment of the `add` call and sees INTENT.

**No replay after uncertainty.** A timeout, an OS error, a non-zero exit, an unparseable task ID or a change of daemon identity fences the intent as AMBIGUOUS (origin INTENT) and raises `AmbiguousSubmission`. There is no retry, and the registry refuses any later submit for the same run_id.

**Idempotent claim.** When the same claimant process repeats the claim with identical fields, it gets the same attempt, fence and token, and nothing is written to either store. Another worker, another process, a field mismatch, or an INTENT, AMBIGUOUS or RETRYABLE row is refused.

**Reconciliation.** `reconcile` runs `queue_recover` (INTENT becomes AMBIGUOUS, and a CLAIMED row without a local record becomes AMBIGUOUS) and then only reads `pueue status --json`. It reports `instance_matches`, `tasks_for_intent` (matched by label), `bound_task_present` and `daemon_task_count`. It never adds, enqueues, removes or resolves anything. Resolving AMBIGUOUS still needs a `VerifiedProof`, and no verifier is registered.

**Isolation (P3.3).** Binaries are accepted only by explicit path and only when the sha256 is pinned. There is no PATH lookup. The adapter requires a 0700 root. It writes its own config, data and runtime directories and a relative socket `s.sock`. The client and the daemon run with a sanitized environment (`PUEUE_CONFIG_PATH`, `HOME` and `XDG_*` inside the root, and `-c <config>`). `stop` and `kill` act only on the daemon this object started.

## 3. Evidence

Pinned binaries verified: `pueue` `c1b10d7e…23fcb51` and `pueued` `5afeff6a…60390` (full hashes in `w3-logs/harness.log`).

| Log | Result |
|---|---|
| `w3-logs/full-suite.log` | Ran 230 (209 baseline + 21 new), OK, exit 0 |
| `w3-logs/queue-adapter.log` | Ran 21, OK, exit 0 (11 against a live private daemon) |
| `w3-logs/queue-adapter-no-binary.log` | `HERDR_PUEUE_BIN_DIR=/nonexistent`: OK (skipped=11), each skip prints `NOT_TESTED: pinned Pueue 4.0.4 unavailable …; no system pueue fallback`. A checksum-mismatch fake binary also skips with a `NOT_TESTED … checksum mismatch` message. |
| `w3-logs/harness.log` | Harness exit 0: task 0 claimed, `attempt_bound: true`, `claim_replayed_without_write: true`, local state UNKNOWN, daemon shutdown exit 0 |

Private-daemon tests (tests/test_queue_adapter.py, `PrivateDaemonTests`):

| Test | Shows |
|---|---|
| `test_private_daemon_is_isolated` | Socket, config and XDG paths are inside the private root. Clean shutdown removes the socket. |
| `test_intent_is_durable_before_submission` | INTENT is committed before `add`. The task is stashed and labelled. |
| `test_end_to_end_worker_claim_binds_task_to_attempt` | A Pueue worker claims twice idempotently. The binding matches. A resubmit is refused. The daemon holds 1 task. |
| `test_failing_daemon_not_running_fences_ambiguous_and_never_retries` | Daemon down: AMBIGUOUS, no retry, and 0 tasks after restart. |
| `test_failing_daemon_stalled_times_out_and_is_fenced` | Daemon sent SIGSTOP: timeout, AMBIGUOUS, and reconcile adds no task. |
| `test_lost_response_after_daemon_accepted_is_ambiguous_and_reconcile_does_not_resubmit` | The daemon accepted the task but the response was lost: AMBIGUOUS. Reconcile (run twice) finds exactly 1 labelled task and leaves the row AMBIGUOUS. Release and claim are refused. |
| `test_restart_after_crash_before_submission_fences_intent_without_resubmitting` | Daemon killed after INTENT: AMBIGUOUS and 0 tasks. |
| `test_restart_after_binding_reads_daemon_state_and_does_not_resubmit` | Daemon killed after BOUND: same instance after restart, task present, count 1. The worker then runs on that same task. |
| `test_restart_after_claim_keeps_claim_and_binding` | CLAIMED and the binding are unchanged after a daemon crash and restart. |
| `test_replaced_daemon_state_is_a_different_queue_instance` | A different state directory gives a different instance. Release is refused and no task is matched. |
| `test_harness_script_passes_against_private_daemon` | Fixed harness, exit 0. |

P0 protected tests: `git diff ef673ac -- tests/test_registry_p0.py tests/test_queue_protocol.py` is empty.

## 4. Gate status

- OFFLINE_TESTED: `queue_claim_idempotent` refusal matrix, `claim_attempt` and `bound_identity` agreement, binary pinning and no PATH fallback, owner-only root, researchctl queued path still BLOCKED.
- ISOLATED_CPU_VERIFIED (pinned Pueue 4.0.4, private daemon, CPU only, this host): durable intent before `add`, queue identity bound to run and attempt, idempotent worker claim inside a real Pueue task, AMBIGUOUS fencing for daemon down, stalled and lost-response cases, and restart reconciliation without resubmission (after INTENT, BOUND and CLAIMED).
- BLOCKED: production dispatch, researchctl queued execution, AMBIGUOUS resolution (no lookup verifier), containment, GPU.

## 5. Open risks

1. The queue instance is an on-disk certificate hash. It does not authenticate the socket peer. A process with write access to the private root could swap state.
2. The lost-response case is simulated by replacing the client's stdout after a real `add`. A response lost on the wire was not produced.
3. Reconcile's matching by label is an observation, not a proof. The intent token is visible in daemon state.
4. A worker learns its task ID from the BOUND queue row, not from Pueue. Pueue 4 exposes only `PUEUE_WORKER_ID` to tasks.
5. A BOUND row whose task is missing from the daemon is reported (`bound_task_present: false`) but has no registry transition.
6. `reports/p0/core-release/pueue-escaped-path-check.py` (historical) copies only two scripts and does not work with the current module layout. It was left unchanged.
7. Tests rely on SIGSTOP and timing (2 s client timeout). They are CPU-only and have not been run on another host.

## 6. Repairs (three LOW findings)

- A, check-to-use gap: `PrivatePueue` verifies each caller-supplied binary, copies it into `<root>/bin` (0700) via a temp file and rename, re-verifies the copy's sha256, and executes only the copy. The caller's path is never executed. Test: `test_only_the_verified_private_copy_is_executed` swaps the originals after construction and checks argv[0] and `/proc/<pid>/exe` are the private copies. Mutation (execute original path again) makes it fail.
- B, raw exceptions in reconcile: `subprocess.TimeoutExpired` and `OSError` from the daemon are reported as `daemon_error` with no state transition (tests: `ReconcileErrorTests`).
- C, enqueue timeout unrecorded: `release` durably records `ENQUEUE_ATTEMPTED` before `pueue enqueue`. A timeout or OS error records `ENQUEUE_UNKNOWN` and fences the row AMBIGUOUS (`ambiguous_from=BOUND`) through `_mark_ambiguous`; restart recovery fences an attempt with no recorded outcome the same way. No retry. AMBIGUOUS from BOUND cannot be resolved by queue evidence. Tests: `EnqueueTimeoutTests`.
- Unchanged: the queue identity still does not authenticate the socket peer; production dispatch stays disabled; the researchctl queued path stays BLOCKED.
