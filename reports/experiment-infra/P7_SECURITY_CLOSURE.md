# P7 security closure

Branch `feat/p7-integration`, head `f3123b3`. No gate is PASS or READY; all six are BLOCKED. Component statuses use only OFFLINE_TESTED, ISOLATED_CPU_VERIFIED, NOT_TESTED, BLOCKED. Review record: `INDEPENDENT_REVIEW.md`.

The repository documents define P7.1 to P7.4 by name (`QUEUE_LEGACY_MIGRATION.md`, `PRODUCTION_WORKER_BOUNDARY.md`, `APPROVAL_CONSISTENCY.md`). P7.5 and P7.6 are not defined in any repository document; the rows below are the lead's grouping and are labelled as such.

## Tasks

| Task | Status | Closed | Remains | Tests |
|---|---|---|---|---|
| P7.1 Legacy `ENQUEUE_REFUSED` fence | OFFLINE_TESTED | Refused rows from `e90e3b1` are fenced, never released or resubmitted; one-transaction idempotent migration; damaged or truncated stores stay blocked | Migration has not been run against any production store (NOT_TESTED live) | `test_refused_bound_row_cannot_be_released_and_is_fenced`, `test_first_run_fences_every_refused_bound_row_in_one_transaction`, `test_second_run_changes_no_bytes`, `test_interrupted_migration_commits_no_fence_and_no_marker`, `test_truncated_store_is_refused_as_unreadable_and_never_repaired`, `test_no_environment_variable_lifts_the_fence` |
| P7.2 Production worker execution boundary | OFFLINE_TESTED | Ordered fail-closed checks; no spawn without approval commit; spawn only via `backend.spawn_gated`; production backend refuses | No production worker containment gate exists in `scripts/`; the e2e gate is in the harness. Production backend returns UNAVAILABLE by design | `test_default_approval_refuses_before_containment_and_launches_nothing`, `test_production_containment_refuses_and_launches_nothing`, `test_missing_or_failed_commit_refuses_at_check5_without_launch`, `test_production_backend_cannot_spawn_even_if_checks_1_to_5_pass`, `test_boundary_source_has_no_direct_spawn`, `test_record_failure_kills_child_and_leaves_no_output`, `test_child_is_blocked_until_identity_recorded` |
| P7.3 Approval consumption consistency and handoff | OFFLINE_TESTED | Non-consuming `verify_for_attempt`, single-use `consume_for_attempt`, `mark_launched`; test-only acceptance removed from `scripts/approval.py`; production entry pins admin uid root | No trust anchor provisioned (production load fails closed `TRUST_ANCHOR_UNPROVISIONED`); stdlib verifier is HMAC, no Ed25519 verifier; generic loader accepts caller-supplied admin uids | `test_gate_grants_and_writes_nothing_bytes_unchanged`, `test_gate_refuses_wrong_manifest_revoked_and_already_consumed`, `test_full_launch_consumes_once_then_marks_launched`, `test_boundary_never_spawns_when_commit_fails`, `ProductionOnlySurfaceTests`, `tests/test_approval_ast_guard.py` (`test_no_named_test_only_pattern_in_any_script`, `test_real_scripts_tree_is_clean_under_the_extended_checks`) |
| P7.4 Daemon identity | OFFLINE_TESTED (callback and identity matrix); ISOLATED_CPU_VERIFIED for private-daemon behaviour as recorded in `QUEUE.md` | SO_PEERCRED probe on the daemon socket, binary hash, start ticks and socket inode checked; replaced or restarted daemon refused | The probe connection is not the connection that carries requests, so there is no per-request peer authentication. Queue identity is a cert hash | `test_socket_replacement_by_another_private_daemon_is_refused`, `test_daemon_restart_invalidates_callbacks_issued_by_the_old_process`, `test_identity_is_never_read_from_the_environment` |
| P7.5 Integration (lead grouping) | OFFLINE_TESTED | W1, W2, W3 merged with a full suite after each; adapter `BoundaryApprovalAdapter`; late review findings MEDIUM-1, MEDIUM-2, LOW-1 to LOW-4 closed | Not a live integration; researchctl official and queued execution stay disabled | `tests/test_p7_integration.py` (21 tests), `RecordChildTests.test_parent_pid_is_refused_writes_nothing_and_recovery_never_signals_it`, `GatedLaunchTests.test_failed_record_never_releases_the_job`, `ClaimantNamespaceTests.*` |
| P7.6 Review and repair loop (lead grouping) | OFFLINE_TESTED | Repairs R1 to R6 and rounds 2 and 3; dispatch flag centralised | Reviews (c), (d), (e) end NOT CLOSED; `f3123b3` has no independent re-review | `OwnerLivenessFenceTests.test_live_owner_is_refused_by_recovery_fence_and_never_signalled`, `DispatchDisabledTests.test_constant_is_false`, `DispatchDisabledTests.test_boundary_and_researchctl_refuse_when_flag_is_true` |

Facts verified in code at `f3123b3`: `PRODUCTION_DISPATCH_ENABLED = False` in `scripts/dispatch_guard.py`; `PRODUCTION_ADMIN_UIDS = (0,)` in `scripts/approval.py`; `_VERIFIED_ISSUERS` is empty in `scripts/run_ownership.py` (dict) and `scripts/containment_backend.py` (frozenset); `get_backend()` returns `UnavailableContainmentBackend`.

## Review findings and resolution

Full detail in `INDEPENDENT_REVIEW.md`.

| Review | Findings | Resolution state |
|---|---|---|
| (a) `3e6ed54` | MEDIUM-1, MEDIUM-2, LOW-1 to LOW-4 | Closed in `30df5f6` (LOW-1 later superseded) |
| (b) `30df5f6` | live owner SIGTERMed after `recovery_fence` (reproduced); latent spawn-before-record; latent spawn outside containment; LOW test root, handoff mode, dispatch flag | Repaired at `c47df58` |
| (c) `0ae5631` | TMPDIR temp base; test guards reachable by any caller; dispatch docstring; INFO attribute guard | NOT CLOSED at that head; carried to (d) |
| (d) `57f79ef` | HIGH: `TEST_CAPABILITY` reachable from `scripts/`; attribute-based `test_only` trust | Repaired at `e53987e` |
| (e) `e53987e` | Caller-supplied admin uids; AST guard incomplete | NOT CLOSED: residual; production entry pins root at `f3123b3` |
| (f) `f3123b3` | none recorded | Lead-verified only (592 OK, Python 3.12.14); not independently re-reviewed |

## Tests

592 tests OK on Python 3.12.14. Python 3.10 and 3.11 are NOT_TESTED.
