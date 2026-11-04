# Experiment containment contract (Phase 2, W2)

Status: **OFFLINE_TESTED** for the proof model, release gate and simulator. **CONTAINMENT_READY: BLOCKED.** No production containment boundary exists on this host. The simulator is test-only, is not containment evidence, and cannot be selected by any production path.

This contract complements `docs/CONTAINMENT.md` and `docs/SECURITY.md`. It does not relax either. Host prerequisites are in `reports/experiment-infra/containment/OPERATOR_REQUIREMENTS.md`.

## Components

| Component | Location | Role |
|---|---|---|
| Verifier and proof model | `scripts/containment_contract.py` | `Verifier` owns trust anchors, clock, outstanding challenges and consumed nonces. `verify_for_release` is the only production entry point. |
| Production selector | `scripts/containment_backend.py` | Always `UNAVAILABLE`. `release()` always refuses. |
| Simulator (test-only) | `tests/support/containment_sim.py` | In-memory and real synthetic-child boundary, controller state machine, `make_test_verifier()` |
| Tests | `tests/test_containment_contract.py`, `tests/test_containment_sim.py`, `tests/test_containment_backend.py` | Evidence for the requirements below |

## Trust boundary (read first)

The HMAC seal uses a key held in the same process as the checker. **This is not authority against a same-user worker.** Any code running in the worker process can read the key, call the sealing function, or replace the verifier. In-process code is outside the threat model.

Real authority requires an externally held verifier key: a verifier running under a separate identity, with a key the worker cannot read. That is a condition this phase does not establish. This matches the trust-anchor requirement in the W4 `APPROVAL.md` (referenced from branch `feat/exp-w4-provenance`; not verified from this worktree). Nothing like that is provisioned here. Asymmetric signatures are not implemented, because the standard library does not provide them and this phase does not invent cryptography.

The production trust-anchor set is empty, so the production verifier refuses every proof on this host.

## Proof model

A `ContainmentProof` carries `run_id`, `attempt_id`, `boundary_id`, `verifier_id`, `observed_at_ns`, `nonce`, `epoch`, `occupancy`, `verdict` (`EMPTY`, `OCCUPIED`, `UNKNOWN`, `BREACHED`), `simulated` and `mac`.

A `Verifier` object holds, privately: its trust anchors (copied at construction), its clock, its maximum age and skew, its outstanding challenges and its consumed-nonce set. Constructor arguments are validated: `allow_simulated` must be a bool, and the age and skew windows must be integers (not bool) within bounds. The production release function is a closure over its verifier object, so rebinding the module global `_PRODUCTION_VERIFIER` does not change outcomes. **In-process tampering (rebinding module globals, editing private verifier fields) is outside the threat model** and is not prevented.

Release requires all of the following. Each is a refusal if it fails.

1. The proof's `verifier_id` is in the verifier's anchors.
2. The MAC, HMAC-SHA256 over every other field, verifies with that anchor's key.
3. `simulated` is false, unless the verifier was built to accept simulated proofs. Only `make_test_verifier()` does.
4. `run_id`, `attempt_id`, `boundary_id`, `nonce` and `epoch` equal the challenge.
5. The challenge was issued by this verifier, has not been superseded by a newer challenge for the same boundary, and has not been consumed.
6. The observation is fresh by the verifier's clock: at most 30 s old, and at most 1 s in the future.
7. `verdict == EMPTY` and `occupancy == 0`.

On the release path the nonce is consumed atomically with the check, so a second use is refused. Completion uses a non-consuming preview. Every exception inside the verifier is converted to a refusal.

## Requirements

Each requirement is marked as **Enforced** (a test fails if the property is broken), **Modelled** (the simulator tests accounting or state logic, but no kernel mechanism is consulted), or **Documented** (this contract states it only).

### R1. Boundary established before user code runs
- **Enforcement:** the production `establish` returns `UNAVAILABLE` and launches nothing.
- **Tests:** `test_production_selector_is_unavailable`; `test_establish_is_unavailable_and_release_refused_for_every_proof`; `test_unavailable_host_capability_fails_closed`.
- **Status:** refusal is **Enforced**. Placement before exec is **Documented** only. The simulator spawns members directly and cannot show that ordering.

### R2. Boundary bound to the specific run and attempt
- **Enforcement:** proofs must match the challenge's `run_id`, `attempt_id` and `boundary_id`. The controller's journal records the same identifiers.
- **Tests:** `test_mismatched_run_attempt_boundary_refused`; `test_mismatched_attempt_cannot_reuse_another_attempts_proof`; `test_stale_and_mismatched_run_attempt_refused_at_backend`.
- **Status:** **Enforced** for proofs. Binding a real kernel object to a run is **Documented** only.

### R3. Ordinary descendants cannot escape the claimed boundary
- **Enforcement:** none against a real escape. The simulator cannot observe OS-level reparenting, process-group change or cgroup writes. `attempt_escape()` is **self-reported**: the test harness declares it. A declared escape is counted as a breach, and a breached attempt cannot complete or release.
- **Tests (self-reported only):** `test_self_reported_escape_is_breach_not_success`; `test_self_reported_escape_keeps_detached_member_counted`; `test_breach_recorded_after_verification_blocks_completion`. `test_detached_descendant_survives_original_process_group` (existing probe test) shows only that a detached CPU child survives process-group kill. It does not show confinement.
- **Status:** **Documented.** Kernel-enforced confinement is absent, and no test here detects a real escape.
- **Out of scope:** kernel escape, a root attacker, and any process that leaves the boundary before it is observed.

### R4. Recursive occupancy tracked
- **Enforcement:** the boundary counts members registered by the harness, including forks it declares during cancellation. Verification, completion and release each re-observe occupancy. A member adopted after completion blocks release.
- **Tests:** `test_fork_during_cancellation_is_terminated_and_rechecked`; `test_fork_during_cancellation_real_children_are_reaped_and_rechecked`; `test_occupancy_is_rechecked_at_completion_not_assumed_from_proof`; `test_member_adopted_after_completion_blocks_release`; `test_parent_exit_with_descendants_keeps_occupancy_and_refuses_release`.
- **Status:** **Modelled, over harness-registered members only.** The model cannot see an unregistered fork. Real recursive enumeration (for example `cgroup.procs` with `cgroup.events` `populated`) is **Documented** and is not available to this worker.

### R5. Authorized whole-workload termination
- **Enforcement:** termination signals only handles that the registry created. It sends SIGTERM first, escalates to SIGKILL after a grace period, re-scans for members, and reports partial or failed cleanup. It never signals by PID, name or pattern.
- **Tests:** `test_sigterm_ignoring_members_escalate_to_sigkill_and_complete`; `test_sigterm_ignoring_descendants_escalate_only_to_spawned_members`; `test_partial_termination_refuses_completion_and_release`; `test_failed_cleanup_leaves_member_and_refuses_release`; `test_partial_cleanup_real_refuses_then_full_cleanup_releases`; `test_signals_never_reach_non_members`.
- **Status:** escalation and partial-cleanup behaviour are **Modelled**. Who may authorize termination is **Documented** only.

### R6. Boundary verified empty before resources are released
- **Enforcement:** `Controller.release` requires state `COMPLETED`, a preview-valid proof, and a fresh occupancy observation of zero. Only then is the nonce consumed and the boundary closed.
- **Tests:** `test_release_refused_from_any_state_other_than_completed`; `test_member_adopted_after_completion_blocks_release`; `test_release_is_refused_without_a_verified_proof`; `test_stale_proof_from_before_termination_cannot_release`; `test_worker_crash_real_leaves_unknown_state`.
- **Status:** **Enforced in the model.** The production gate refuses every proof.

### R7. Completion and release refused while verification is incomplete
- **Enforcement:** `complete()` refuses with no proof, after an interrupted verification, after an observation fault, after any declared breach, and when the completion re-check sees a live member. A supervisor restart rebuilds state as `UNKNOWN` from the durable journal, with no proof, so it cannot release.
- **Tests:** `test_interrupted_verification_declares_no_completion`; `test_interrupted_verification_declares_no_completion_even_with_older_proof`; `test_breach_recorded_after_verification_blocks_completion`; `test_recovered_supervisor_cannot_release_without_fresh_verification`; `test_worker_crash_mid_run_stays_unknown_and_unreleased`; `test_parent_exit_with_descendants_refuses_completion_and_release`.
- **Status:** **Enforced in the model.**

### R8. Caller-constructed or stale proofs rejected
- **Enforced:**
  1. Exact types: every text field is `str` and every counter is `int`, not `bool`. Values are bounded and non-empty where required (`test_str_subclass_rebinding_cannot_match_another_run`, `test_bool_epoch_does_not_match_integer_epoch`, `test_unhashable_verdict_returns_refusal_not_exception`, `test_empty_nonce_refused_on_both_sides`, `test_out_of_bounds_counters_refused`).
  2. The MAC must verify under an anchor held by the verifier object (`test_direct_dataclass_construction_never_verifies`, `test_tampered_proofs_refused`, `test_untrusted_verifier_and_wrong_key_refused`). **Caveat:** same-process code can seal, as stated above.
  3. Exact `run_id`, `attempt_id`, `boundary_id`, `nonce` and `epoch` match (`test_mismatched_run_attempt_boundary_refused`).
  4. The challenge must have been issued by the verifier. An expectation built from the proof's own fields is refused, as is a superseded challenge (`test_expectation_built_from_proof_fields_is_refused_by_production_path`, `test_new_challenge_supersedes_earlier_one_for_the_same_boundary`).
  5. Release consumes the nonce atomically. A second use is refused, including under 16-way concurrency (`test_release_consumes_nonce_and_refuses_second_use`, `test_concurrent_release_consumes_the_nonce_exactly_once`).
  6. Freshness uses the verifier's own clock. The production entry point takes no caller time (`test_stale_and_future_proofs_refused_by_verifier_clock`, `test_production_entry_point_takes_no_caller_time_or_anchor`).
  7. Rebinding the module global `_PRODUCTION_VERIFIER` does not change the production outcome (`test_rebinding_production_verifier_global_does_not_change_release`). Editing private verifier fields in-process is not prevented; it is outside the threat model (`test_limitation_text_is_documented` checks this text is present). Invalid constructor arguments are rejected (`test_allow_simulated_must_be_a_real_bool`, `test_max_age_must_be_positive_int_not_bool_and_bounded`).
  8. Simulated proofs are refused unless the verifier allows them (`test_real_simulated_run_proof_refused_by_production_verifier`).
  9. Process identity and non-`ContainmentProof` inputs are refused (`test_non_proof_inputs_never_confer_authority`, `test_process_identity_match_is_observation_not_containment`).
  10. Any exception inside the verifier is a refusal (`test_exception_inside_verifier_becomes_refusal`).
  11. A stale or replayed proof from an earlier epoch is refused (`test_replayed_proof_from_earlier_epoch_is_rejected`, `test_stale_proof_from_before_termination_cannot_release`).
- **Not enforced:**
  - Forgery by same-process code (see Trust boundary).
  - Replay across a verifier restart. The consumed-nonce set is in memory. A production verifier must persist consumed nonces durably before it can be relied on. Until then, a restart forgets them. Outstanding challenges are also lost on restart, which fails closed.
  - Clock tampering, and any other in-process tampering with module globals or private verifier fields.
- **Status:** enforced for the in-process checks listed above. Not authority against a same-user worker.

## Simulator isolation

- The simulator is under `tests/support/`. No file under `scripts/` names it, its key, its verifier factory or its verifier identity. A test scans every file in `scripts/` for these names.
- No file under `scripts/` calls `check_proof`, `seal`, `mac_for`, `issue_challenge` or `preview`, or constructs a `Verifier`. An AST test enforces this.
- The production verifier refuses simulated proofs, and the production selector never returns the simulator.
- Every synthetic child is started by `SpawnRegistry` as a `subprocess.Popen`, with a bounded startup wait, reaped in `tearDown`, and checked against `/proc` markers. Tests assert none remain.

## Out of scope

- Kernel escape, container breakout, and any exploit of the kernel or systemd.
- A root attacker, or any same-user code that can read the verifier's key or memory.
- Cross-host execution, network-isolated workloads, and remote termination.
- Out-of-band CUDA or GPU jobs started outside the boundary.
- PID reuse, and process identity as a containment proof. Identity is an observation only.
- Scientific validity of any workload.

## Verification performed for this phase

- Full CPU suite under Python 3.12.14, with no real cgroup, sudo, GPU or research workload. Logs and exit codes are in `reports/experiment-infra/w2-logs/`.
- The read-only host observation in `w2-logs/host-observation-readonly.log` confirms BLOCKED with exit 2.
- No independent review of this revision has been performed by W2. The Opus review of the prior revision (6319e1c) reported no CRITICAL or HIGH findings. Its MEDIUM and LOW findings are addressed in this revision as described above.
