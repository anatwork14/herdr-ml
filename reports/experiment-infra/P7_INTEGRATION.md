# P7 integration

Branch `feat/p7-integration`, created from 3e6ed54. Nothing pushed; PR #2 and PR #3 untouched.
`PRODUCTION_DISPATCH_ENABLED` stays False; researchctl official and queued execution stay disabled.

## Merges (git merge --no-ff, full suite after each, Python 3.12)

| Step | Branch at | Merge SHA | Ran | Result |
|---|---|---|---|---|
| a | feat/p7-w2-queue 0aaaef2 | 8da86ff | 481 | OK |
| b | feat/p7-w3-approval 55b56b3 | ffd703d | 517 | OK |
| c | feat/p7-w1-boundary 1659d9c | 93af269 | 542 | OK |

The briefed counts for (b) 491 and (c) 480 are not the integrated totals; each count above is the suite on
the merged tree. No conflicts occurred.

## Interface adapter

- `approval.verify_for_attempt(...)`: non-consuming twin of `consume_for_attempt`. Both go through one
  shared `_checked_grant` (same validation and `_authorize` checks). The non-consuming path passes
  `write=False`, so `load_revocations` checks sequence rollback but never calls `RevocationState.advance`,
  and `ConsumptionStore.preflight` reads the claims and raises what `claim` would raise (approval
  consumed, attempt already claimed, unlaunched, fenced). It writes nothing.
- `approval.BoundaryApprovalAdapter(ctx, approval, decisions, clock)`:
  - `gate` calls `verify_for_attempt`; GRANTED on success, UNAVAILABLE on any failure.
  - `commit` calls `consume_for_attempt` and returns True only when it returned a grant record; any
    refusal, fault or non-record return gives False, so the boundary refuses at check 5 without spawning.
  - `launched` calls `mark_launched` (consumed_not_launched to launched).
  - `decisions` is the boundary's `ApprovalDecision` enum, passed in so no script imports the boundary
    (an existing W1 test forbids that).
- `execution_boundary.launch` gained an optional `approval_launched` callback, invoked after
  record_child and the RUNNING advance. Defaults are unchanged and fail closed
  (`fail_closed_approval_gate` returns UNAVAILABLE, `fail_closed_approval_commit` returns False).
- Tests (`tests/test_p7_integration.py`): `test_gate_grants_and_writes_nothing_bytes_unchanged`,
  `test_gate_rejects_rolled_back_revocation_sequence_without_writing`,
  `test_gate_refuses_wrong_manifest_revoked_and_already_consumed`,
  `test_boundary_never_spawns_when_commit_fails`, `test_boundary_never_spawns_when_commit_returns_non_success`,
  `test_commit_only_after_all_checks_and_gate_alone_never_consumes`,
  `test_full_launch_consumes_once_then_marks_launched`, `test_default_provider_stays_fail_closed`.

## Late review findings

| Finding | Repair | Tests |
|---|---|---|
| MEDIUM-1 record_child accepts any pid | `run_registry.record_child` refuses with `CHILD_NOT_OWN_CHILD` before any write unless the pid is this process or a /proc child whose ppid equals `os.getpid()`; a gone, reaped or unreadable pid is refused. This process itself stays allowed because existing P0 tests record `os.getpid()` and may not change. | `RecordChildTests.test_parent_pid_is_refused_writes_nothing_and_recovery_never_signals_it`, `test_unrelated_gone_and_invalid_pids_are_refused`, `test_own_child_is_accepted_and_reaped_child_is_refused` |
| MEDIUM-2 spawn before record | `researchctl.start_gated` runs the child behind a pipe gate shim (exec after release; EOF means exit 125), calls the record callback, then releases. The execute path uses it for record_child. The official and queued paths remain disabled. | `GatedLaunchTests.test_identity_is_recorded_before_the_gate_is_released`, `test_failed_record_never_releases_the_job`, `test_missing_executable_exits_127_after_release`, `test_execution_stays_disabled` |
| LOW-1 test anchor accepted with any root | `_test_root` requires an existing 0700 directory owned by the current uid; `verify_approval` and `consume_for_attempt`/`verify_for_attempt` refuse a test anchor unless the store and revocation state are also test-only (and the reverse). "Fresh" is not enforceable beyond owner and mode. | `TestAnchorContextTests.*` (3 tests) |
| LOW-2 gate released too early | Harness releases the gate after record_child, advance and record_descendants. Release happens at the post-commit hook `descendants:after_commit` (descendants already durable) so the existing crash-with-job-running boundary keeps its meaning; the harness then waits (bounded) for the output so that state is deterministic. | existing `test_e2e_offline` boundary tests (unchanged) |
| LOW-3 docs | RECOVERY.md and `record_descendants` docstring say a session match counts only when the leader leads its own session (code already did). | none (docs) |
| LOW-4 claimant namespaces | Claimant is now `pid:boot:start\|pid_ns\|mnt_ns`; `_claimant_liveness` uses `observe_process`, so another namespace yields UNKNOWN (recovery defers) instead of GONE. Legacy 3-field claimants keep old behaviour. | `ClaimantNamespaceTests.*` (3 tests) |

## Verification

See the final section appended after the last run: full suite, markers, P0 diff.

Final head, Python 3.12.14, `python3.12 -m unittest discover -s tests`: Ran 563, OK, exit 0 (after the
three merges the suite was 542; 21 tests added in `tests/test_p7_integration.py`).
`pgrep` for the suite's markers (`herdr-e2e-test-`, `bnd-marker`, `gate-marker`) and for `pueued`: empty.
`git diff 3e6ed54 -- tests/test_registry_p0.py tests/test_queue_protocol.py`: empty (no assert or `def test_`
line changed). `PRODUCTION_DISPATCH_ENABLED` is unchanged and False.
