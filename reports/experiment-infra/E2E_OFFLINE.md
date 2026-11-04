# E2E_OFFLINE: offline end-to-end CPU validation (Phase 6)

Branch `feat/exp-phase6-e2e` from `feat/exp-phase5-recovery` at `8552182`.

**No operational PASS is claimed.** Everything here is OFFLINE_TESTED or ISOLATED_CPU_VERIFIED on this host. **EXPERIMENT_RECOVERY_READY, CONTAINMENT_READY, APPROVAL_READY and QUEUE_READY all stay BLOCKED.** The production containment backend is still `UNAVAILABLE`. No production trust anchor exists. `PRODUCTION_DISPATCH_ENABLED` is still `False`. The researchctl queued path is still blocked, and its guard is unchanged.

The only change under `scripts/` is in `approval.py`: test-only anchors and state must now live under a `test_root` the test passes explicitly, instead of under `tempfile.gettempdir()` (which follows `TMPDIR`); production mode refuses a `test_root`. The work adds `tests/support/e2e_harness.py` (test-only harness and worker) and `tests/test_e2e_offline.py` (20 tests, each with a SIGALRM timeout). The W2 simulator is imported only by the harness's simulator gate, and only inside the worker process. A test scans `scripts/` and confirms nothing there names `e2e_harness`, `containment_sim` or `tests/support`.

## 1. Harness

The test process does SUBMIT (the submitter and approver role) and RECOVER. A separate worker process runs `python e2e_harness.py <config> <marker>` and does every stage in between. When the worker exits, whether normally, by refusal, or by `os._exit(77)` at a named crash point, its owner identity is gone and recovery can act. Everything lives in a private `TemporaryDirectory` (mode 0700):

- a throwaway git repository holding the job;
- the inputs;
- a TEST-ONLY HMAC anchor, its revocation state and consumption store;
- the run folder `.runtime/project-e2e/runs/run-1`;
- a private queue-registry home.

There is no queue daemon. "External submission" appends one line to `submissions.log`, so any resubmission can be counted.

| Stage | Actor | Code path used | Result (simulator gate + test anchor) | Result (production gate) |
|---|---|---|---|---|
| SUBMIT | test | `execution_manifest.build_manifest` from a clean repo; `approval.issue_approval` and `issue_revocation` (TEST-ONLY anchor) | PASS | PASS |
| INTENT | worker | `queue_intent`, one external submission, `queue_bind_task` | PASS | PASS |
| CLAIM | worker | `queue_claim`, `claim(..., 'QUEUED', ...)` | PASS (fence 1) | PASS (fence 1) |
| CONTAINMENT_CHECK | worker | production: `get_backend().establish` + `is_verified_for_execution`; simulator: `SimBoundary` / `Controller.verify` + test verifier `preview` | PASS, labelled `SIMULATED_VERIFIED`, `OFFLINE_TESTED`, `operational: false` | **REFUSED `CONTAINMENT_UNAVAILABLE`** |
| VALIDATION | worker | `verify_before_execution` (sealed input copies), then `verify_approval` (consumed last) | PASS | not reached |
| EXECUTE | worker | `advance` to DISPATCHING, `Popen` of the manifest argv (`python -I job.py <marker>`) on the sealed dataset copy, `record_child`, RUNNING, `record_descendants` | PASS, exit 0 | not reached |
| ARTIFACT_SEAL | worker | `build_result_record` (containment UNKNOWN); canonical seal binding the manifest digest and the artifact sha256 and size; `finish()` records `output_sha256`, `result_record_sha256` and `seal_sha256` | PASS | not reached |
| (release) | worker | simulator `Controller.release` (test verifier); production `release_run` | simulator release allowed (test-only); production release REFUSED `CONTAINMENT_UNAVAILABLE` | not reached |
| RECOVER | test | `run_recovery.recover` | `RECOVERY_FENCED`, queue `AMBIGUOUS` (CLAIMED), not completed | `RECOVERY_FENCED`, queue `AMBIGUOUS` (CLAIMED), release refused `NO_RECORDED_DESCENDANTS`, `CONTAINMENT_UNAVAILABLE` |

The run never reaches `COMPLETED`, `FAILED` or `INTERRUPTED`. `verified_finish` needs a `VerifiedProof`, and no verifier is registered.

## 2. Production predicate refusal under the harness worker (item 1)

**Scope.** What is tested here is the production predicate, not a production worker. The worker is the **test harness worker** in `tests/support/e2e_harness.py` (`ProductionContainmentGate.check`). It calls the production selector `containment_backend.get_backend().establish` and the production predicate `containment_backend.is_verified_for_execution`, and the predicate always refuses because `_VERIFIED_ISSUERS` is empty. **No production worker in `scripts/` has a containment gate yet.** The fail-closed property of a real queued or worker path is therefore **NOT_TESTED**, and stays NOT_TESTED until such a gate exists in `scripts/` and is tested.

`test_production_containment_refuses_before_execute` gives the harness worker a valid TEST-ONLY approval, so the predicate is the only barrier. Observed:

- The worker exits 3 and prints `REFUSED CONTAINMENT_CHECK CONTAINMENT_UNAVAILABLE`. The stage log is SUBMIT, INTENT, CLAIM PASS, then CONTAINMENT_CHECK REFUSED with `code: CONTAINMENT_UNAVAILABLE`, and the backend reason is `No production containment backend is configured`.
- Nothing was executed:
  - no job process was spawned;
  - `child_identity` and `result` are empty;
  - the output file, the output directory, the seal, the result record and the sealed-input directory do not exist;
  - the approval consumption store is empty, so the approval was not used.
- State before recovery: local `CLAIMED`; queue `CLAIMED`, task `task-1`, fence 1; one submission.
- After recovery:
  - local state is `RECOVERY_FENCED`, and the queue is `AMBIGUOUS` (CLAIMED);
  - release is refused with `NO_RECORDED_DESCENDANTS`, `CONTAINMENT_UNAVAILABLE`;
  - a new intent, a queue claim, and both SUBMITTED and QUEUED local claims are all refused;
  - there is still exactly one submission.

Mutations:

- Making the harness gate pass when the backend is UNAVAILABLE makes the harness worker run the job (`0 != 3 : DONE`). Both `test_production_containment_refuses_before_execute` and `test_production_gate_and_dispatch_guards_stay_closed` fail.
- Making `containment_backend.is_verified_for_execution` return True for UNAVAILABLE fails the same 2 tests.
- After restoring either one, all 3 tests in the class pass.

## 3. Approval (item 3)

- No anchor provisioned: the worker loads the production default `/etc/herdr-ml/trust/execution-approval` in production ownership mode. It is refused at VALIDATION with `TRUST_ANCHOR_UNPROVISIONED`, and no job is spawned (`test_without_trust_anchor_validation_refuses_and_nothing_executes`). A direct load of the default path gives the same code.
- TEST-ONLY anchor in a temp directory: the approval verifies once, and a second use gives `APPROVAL_CONSUMED`.
- Production code cannot set that anchor:
  - The same temp anchor loaded in production mode (`test_only` defaults to False, with or without uid allowlists) is refused with `TRUST_ANCHOR_INVALID`.
  - `test_only=True` requires an explicit absolute `test_root` (not `/`) and refuses a path outside it. `TMPDIR` and `tempfile.gettempdir()` are not consulted: `test_test_only_anchor_outside_explicit_root_is_refused_even_when_tmpdir_points_there` points `TMPDIR` at an unlisted directory and the anchor, revocation state and consumption store there are all refused. Restoring the `gettempdir()` default makes that test fail.
  - `DEFAULT_TRUST_ANCHOR_DIR` is a constant, and an AST check finds no `environ` or `getenv` read in `approval.py`.
  - An AST scan of every `scripts/*.py` finds no `test_only=True`, and no call to `load_trust_anchor` or `provision_revocation_state` outside `approval.py`. `researchctl.py` does not import `approval`.

## 4. Artifact seal (item 4)

`test_seal_matches_recorded_output_and_tampering_fails` checks that three values are equal: the seal's `artifact.sha256`, the registry-recorded `output_sha256`, and sha256 of the output file. The result record's digest also matches `result_record_sha256` recorded by `finish()`.

Tampering is detected as follows:

| Change | Result |
|---|---|
| Changed bytes or an appended byte in the output | `ARTIFACT_DIGEST_MISMATCH` |
| Output deleted | `ARTIFACT_MISSING` |
| Restored output | Verifies again |
| Rewritten seal | `SEAL_DIGEST_MISMATCH` (the seal digest is recorded in the registry) |
| Seal presented for another manifest | `SEAL_BINDING_MISMATCH` |

## 5. Injected crash boundaries (item 5)

Each case runs the simulator gate with the test anchor, crashes the worker with `os._exit(77)` at an existing named hook, runs `recover` twice, and asserts all of the following:

- the allowed end state;
- `completed` is false;
- the submission count is unchanged and at most 1;
- intent, queue claim and local claims are refused;
- the approval-claim count is unchanged;
- every recorded job is gone and `pgrep -f <marker>` is empty right after recovery, before teardown.

| Boundary | Hook | Left by crash | After recovery | Passes |
|---|---|---|---|---|
| after intent | `queue_intent:after_commit` | queue INTENT, no local row, 0 submissions | queue AMBIGUOUS (INTENT), no local row | 3/3 |
| after bind | `queue_bind_task:after_commit` | queue BOUND, 1 submission | queue AMBIGUOUS (BOUND), no local row | 3/3 |
| after queue claim | `queue_claim:after_commit` | queue CLAIMED, no local row | queue AMBIGUOUS (CLAIMED), no local row | 3/3 |
| after local claim | `claim:after_commit` | local CLAIMED | RECOVERY_FENCED fence 2; release refused `NO_RECORDED_DESCENDANTS`, `CONTAINMENT_UNAVAILABLE` | 3/3 |
| after containment and validation, before dispatch | `execute:claimed` | local CLAIMED, approval consumed | same as above; approval stays consumed | 3/3 |
| job running | `descendants:after_commit` | local RUNNING, job ALIVE | RECOVERY_FENCED; job terminated by recorded identity and verified gone; release refused `CONTAINMENT_UNAVAILABLE` | 3/3 |
| job exited, before seal | `execute:leader_exited` | local RUNNING, output present, no seal | RECOVERY_FENCED; release refused `CONTAINMENT_UNAVAILABLE` | 3/3 |
| seal recorded | `finish:after_commit` | local UNKNOWN, seal present | RECOVERY_FENCED; seal still verifies against the registry record | 3/3 |
| before release | `release:begin` | local UNKNOWN, seal present | same | 3/3 |

Total: 27/27 (9 boundaries × 3). Every local-row case ends with the queue `AMBIGUOUS` (CLAIMED). The counts were printed by the suite (`E2E_BOUNDARY_PASSES`, with `HERDR_E2E_REPORT=1`).

## 6. Process accounting (item 6)

Each test records the identity (pid, boot id, start ticks, pid namespace) of two kinds of process:

- the worker, read from `/proc` immediately after `Popen` and before it is waited on;
- each job, published by the worker to `spawned.jsonl` before the job is allowed to run. The worker launches the job through a start-gate shim that blocks on an inherited pipe; the worker records the job's pid and start ticks, then writes the release byte, and the shim execs the manifest argv (same pid and start time). If the worker dies before the release, the pipe's write end closes, the shim reads EOF and exits 125 without running the job. `test_worker_killed_between_popen_and_record_leaves_no_orphan` crashes the worker at the harness-only point `execute:spawned_unrecorded` (after `Popen`, before the record) and asserts no job identity was recorded, no output exists and no marked process remains, before and after teardown.

`tearDown` asserts that no recorded identity is still alive. Any survivor is terminated by recorded identity only (pidfd, through `terminate_recorded`) and then reported as a failure. Teardown also asserts that `pgrep -f <marker>` is empty. After the full suite, `pgrep -af 'herdr-e2e-tes[t]-'` and `pgrep -af 'herdr-recovery-tes[t]-'` both exit 1.

## 7. Status per item

| Item | Status |
|---|---|
| 1a. Production predicate `is_verified_for_execution` refuses (`CONTAINMENT_UNAVAILABLE`) when called by the test harness worker; nothing executes | OFFLINE_TESTED (harness worker in `tests/support/`, not a production worker) |
| 1b. Production worker containment gate in `scripts/` (fail-closed property of a real queued or worker path) | BLOCKED: no such gate exists in `scripts/`; NOT_TESTED |
| 2. Simulator path reaches EXECUTE and ARTIFACT_SEAL | OFFLINE_TESTED only (simulator gate is not containment; never operational) |
| 3. Approval: unprovisioned refuses, TEST-ONLY anchor verifies, production cannot select it | OFFLINE_TESTED |
| 4. Seal matches recorded output hash; tampering detected | OFFLINE_TESTED |
| 5. Crash at 9 boundaries × 3, allowed terminal state, no resubmission, no leftovers | ISOLATED_CPU_VERIFIED (real crashed worker and job processes) |
| 6. Every started process recorded by pid and start time before the job is released; none remain, including after a worker death between `Popen` and the record | ISOLATED_CPU_VERIFIED |
| Live containment, production approval anchor, production queue dispatch | BLOCKED |
| EXPERIMENT_RECOVERY_READY, CONTAINMENT_READY, APPROVAL_READY, QUEUE_READY | BLOCKED |

## 8. Limits

- The simulator boundary has no members and does not contain the job. Its pass shows only that a verified gate lets the chain proceed.
- The worker plays both launcher and worker. It loads the TEST-ONLY anchor keys itself, which a production deployment must not do (see APPROVAL.md).
- The manifest and approval attempt label (`attempt-1`) is declared at SUBMIT. It is separate from the registry's fencing attempt id. The two are tied together only by the manifest digest, which is stored in the queue row and the local row.
- The seal is canonical JSON whose sha256 is recorded in the run registry. It is unsigned, so anyone who can write both the registry and the seal can forge it.
- There is no Pueue daemon. External submission is a local log line.
- Only the worker and job are tracked by identity. `git` helper processes are synchronous and waited on, and they are not identity-recorded.
- Tested on this host only: Python 3.12, kernel 5.4, single pid namespace, no cgroup.

## 9. Verification

- Full suite: `/mnt/ssd/genos2/.local/bin/python3.12 -m unittest discover -s tests` gave `Ran 455 tests`, `OK`, exit 0.
- `git diff 8552182 -- tests/test_registry_p0.py tests/test_queue_protocol.py` is empty (also empty against `cc3270e`).
