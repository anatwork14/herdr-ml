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

## Review repairs (R1-R6)

Applied on top of 30df5f6; each has a regression test in `tests/test_p7_repairs.py` that fails
without the fix. The fix commit SHA is recorded in the last line of this section.

| Id | Repair | Test |
|---|---|---|
| R1 MEDIUM | `recovery_fence` observes owner liveness (`process_identity.observe_process`) under the same `BEGIN IMMEDIATE` lock it writes under. A live or undeterminable owner refuses with `OwnerLive` (`OWNER_LIVE`) and writes nothing. `record_child` is unchanged (P0 tests still record `os.getpid()`). Three existing `tests/test_recovery.py` tests that fenced a live owner (this process) now patch the observation to model a gone owner; no assertion changed. | `OwnerLivenessFenceTests.test_live_owner_is_refused_by_recovery_fence_and_never_signalled` (child-process owner, `recover()` leaves it alive), `test_undeterminable_owner_is_refused` |
| R2 MEDIUM | The boundary starts the job blocked at a start gate, runs `record_child` and the RUNNING advance, and only then releases the gate. On any record failure it aborts the gate, kills the child's process group, reaps it and refuses with `LAUNCH_RECORD_FAILED`; no output is written. | `GatedSpawnTests.test_record_failure_kills_child_and_leaves_no_output`, `test_child_is_blocked_until_identity_recorded` |
| R3 MEDIUM | The boundary no longer spawns. It calls `backend.spawn_gated(...)`, a launcher the containment backend provides (`containment_backend.GatedChild`). The production backend raises `ContainmentUnavailable`, so the boundary refuses `CONTAINMENT_UNAVAILABLE`. The test double spawns only through `tests/support/gated_launcher.py`. | `GatedSpawnTests.test_production_backend_cannot_spawn_even_if_checks_1_to_5_pass`, `test_boundary_source_has_no_direct_spawn` |
| R4 LOW | `approval._test_root` additionally requires the root to be under `tempfile.gettempdir()` (and not the temp dir itself), not equal to or an ancestor of `$HOME`, and to hold no symlink or foreign-owned entry. This is a TEST-MODE GUARD ONLY, not a production control: production mode refuses `test_root` outright. Deviation: strict "empty" could not be required because every existing fixture places its anchor and state directories inside the root; the guard instead refuses planted symlinks and foreign-owned entries. | `TestRootGuardTests.test_home_directory_root_is_refused_and_fresh_temp_root_accepted`, `test_home_itself_and_its_ancestors_are_refused` |
| R5 LOW | `verify_handoff(..., mode='production')`. A `TestOnlyHandoffVerifier` is accepted only with `mode='test'`; production mode (the default) and unknown modes refuse with `HANDOFF_VERIFIER_NOT_SELECTABLE`. Existing handoff tests now pass `mode='test'`. | `HandoffModeTests.test_production_mode_refuses_test_only_verifier` |
| R6 LOW | New `scripts/dispatch_guard.py` raises if `queue_adapter.PRODUCTION_DISPATCH_ENABLED` is not False, at import and via `require_disabled()`. The boundary (`DISPATCH_ENABLED`, check 0) and `researchctl.require_supported_execution` call it. The flag stays False; researchctl does not name the adapter module. | `DispatchDisabledTests.test_constant_is_false`, `test_import_time_check_refuses_when_flag_is_true`, `test_boundary_and_researchctl_refuse_when_flag_is_true` |

Fix commit SHA: c47df58. Full suite at that commit (Python 3.12): Ran 575, OK, exit 0; marker pgrep empty; P0 diff vs 3e6ed54 has no assert or def test_ line.

## Verification

See the final section appended after the last run: full suite, markers, P0 diff.

Final head, Python 3.12.14, `python3.12 -m unittest discover -s tests`: Ran 563, OK, exit 0 (after the
three merges the suite was 542; 21 tests added in `tests/test_p7_integration.py`).
`pgrep` for the suite's markers (`herdr-e2e-test-`, `bnd-marker`, `gate-marker`) and for `pueued`: empty.
`git diff 3e6ed54 -- tests/test_registry_p0.py tests/test_queue_protocol.py`: empty (no assert or `def test_`
line changed). `PRODUCTION_DISPATCH_ENABLED` is unchanged and False.

## Repairs round 2 (R1-R4)

Tests are in `tests/test_p7_repairs.py` unless noted. Supersedes the R4/R5 rows above where they differ.

| Id | Repair | Test |
|---|---|---|
| R1 LOW | `approval._test_root` no longer reads `tempfile.gettempdir()` or TMPDIR. The root must be under the fixed base `/tmp`, or `/run/user/<uid>` when that exists. Any root under HOME, equal to HOME, or above HOME is refused. Docstrings corrected. | `FixedTempBaseTests.test_tmpdir_pointing_at_home_does_not_admit_a_home_subdirectory` |
| R2 INFO | The handoff mode guard uses `isinstance(verifier, TestOnlyHandoffVerifier)`, not an instance attribute. | `TestCapabilityTests.test_instance_attribute_cannot_make_a_production_verifier_look_like_a_test_verifier` |
| R3 MEDIUM | Private `approval.TEST_CAPABILITY` token, returned by `_grant_test_capability()` only to a caller inside `tests/support`. `tests/support/approval_test_mode.py` (`TEST_MODE`) is the only holder. `verify_handoff(mode=...)` accepts the token (identity) or `'production'`; `verify_approval` and `AttemptContext` take `test_capability=` and a test anchor or test state is refused without it. The string `'test'` is refused. The 11 `verify_handoff` calls in `tests/test_approval_consistency.py` and all approval test calls pass `TEST_MODE`; no assertion changed. | `test_string_test_mode_is_refused`, `test_test_anchor_without_the_capability_is_refused_by_verify_approval`, `test_capability_is_granted_only_to_tests_support`, `test_no_script_other_than_approval_names_the_capability` |
| R4 LOW | `PRODUCTION_DISPATCH_ENABLED` is defined in `scripts/dispatch_guard.py`; `queue_adapter` imports it from there. researchctl's import chain no longer reaches `queue_adapter`. `DispatchDisabledTests` now flips the flag in `dispatch_guard`; `test_queue_adapter` additionally checks `sys.modules` in a subprocess. | `OfflineAdapterTests.test_researchctl_queued_path_stays_blocked_and_does_not_use_adapter` (strengthened), `DispatchDisabledTests.*` |

Verification, round 2 (Python 3.12, `env -u TMPDIR python3.12 -m unittest discover -s tests`): Ran 581, OK, exit 0. Marker and `pueued` pgrep empty. P0 diff vs 3e6ed54 for `tests/test_registry_p0.py` and `tests/test_queue_protocol.py`: empty. Mutation: restoring the `tempfile.gettempdir()` check made `FixedTempBaseTests` fail; restored. `PRODUCTION_DISPATCH_ENABLED` is False.
