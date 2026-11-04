# Experiment containment contract (Phase 2, W2)

Status: **OFFLINE_TESTED** for the proof model, release gate and simulator. **CONTAINMENT_READY: BLOCKED.** No production containment boundary exists on this host, and the simulator is test-only. It is not containment evidence and cannot be selected by any production path.

This contract complements `docs/CONTAINMENT.md` and `docs/SECURITY.md`. It does not relax either document. Host prerequisites are in `reports/experiment-infra/containment/OPERATOR_REQUIREMENTS.md`.

## Components

| Component | Location | Role |
|---|---|---|
| Proof model and release gate | `scripts/containment_contract.py` | MAC-bound `ContainmentProof`; `verify_for_release` is the only production entry point |
| Production selector | `scripts/containment_backend.py` | Always `UNAVAILABLE`; `release()` always refuses |
| Simulator (test-only) | `tests/support/containment_sim.py` | In-memory and real synthetic-child boundary model, controller state machine, test verifier `sim_verify` |
| Tests | `tests/test_containment_contract.py`, `tests/test_containment_sim.py`, `tests/test_containment_backend.py` | Enforcement evidence for the requirements below |

## Proof model

A `ContainmentProof` carries `run_id`, `attempt_id`, `boundary_id`, `verifier_id`, `observed_at_ns`, `nonce`, `epoch`, `occupancy`, `verdict` (`EMPTY`, `OCCUPIED`, `UNKNOWN`, `BREACHED`), `simulated` and `mac`. Release requires all of the following:

1. The `verifier_id` is in a trust-anchor mapping. The production mapping is empty.
2. The MAC, HMAC-SHA256 over every other field, verifies with that anchor's key.
3. `simulated` is false, unless the caller explicitly allows simulation. Only test code does.
4. `run_id`, `attempt_id`, `boundary_id`, `nonce` and `epoch` equal the values the supervisor issued in its challenge.
5. The observation is fresh: at most 30 s old, and at most 1 s in the future.
6. `verdict == EMPTY` and `occupancy == 0`.

Dataclass construction, JSON, dictionaries, process identity observations and simulated proofs all fail at least one check on this host.

**Known limitation.** The sealing function lives in the same module as the checker. A process that can run arbitrary code in the worker's address space can call it, so the MAC protects against accidental and naive forgery, not against code execution inside the verifier's trust domain. Real authority requires a verifier running as a separate identity whose key the worker cannot read. Nothing like that is provisioned here.

## Requirements

For each requirement: **Enforced** means a test fails if the property is broken. **Modelled** means the simulator tests the accounting or state logic, but the real kernel mechanism is absent. **Documented** means only this contract states it.

### R1. Boundary established before user code runs
- **Enforcement:** the production `establish` returns `UNAVAILABLE` and never launches anything. No execution path exists.
- **Tests:** `test_production_selector_is_unavailable`; `test_establish_is_unavailable_and_release_refused_for_every_proof`; `test_unavailable_host_capability_fails_closed`.
- **Status:** Enforced for refusal. Placement-before-exec is **Documented** only. The simulator spawns members directly and cannot demonstrate that ordering.
- **Out of scope:** a real placement mechanism on any host.

### R2. Boundary bound to the specific run and attempt
- **Enforcement:** every proof carries `run_id`, `attempt_id` and `boundary_id`, which must match the expectation exactly. The controller's journal records the same identifiers.
- **Tests:** `test_mismatched_run_attempt_boundary_refused`; `test_mismatched_attempt_cannot_reuse_another_attempts_proof`; `test_stale_and_mismatched_run_attempt_refused_at_backend`.
- **Status:** Enforced for proofs. Binding of a real kernel object to a run is **Documented** only.

### R3. Ordinary descendants cannot escape the claimed boundary (threat model: same-user, non-privileged, no kernel exploit)
- **Enforcement:** the boundary counts members by tracked handle, not by process group, session or parent. An escape or reparent is recorded as a breach. A breached attempt cannot complete or release, even when the escaped member has since exited.
- **Tests:** `test_escape_attempt_is_counted_as_breach_not_success`; `test_escaped_detached_descendant_is_still_occupancy`; `test_detached_descendant_survives_original_process_group` (existing probe test).
- **Status:** **Modelled.** Accounting is tested. Kernel-enforced confinement is absent, so the claim is **Documented** only.
- **Out of scope:** kernel escape, a root attacker, a process that can write another cgroup, and a process that has already left the boundary before it was observed.

### R4. Recursive occupancy tracked
- **Enforcement:** the boundary tracks every member it spawned or was told about, including forks during cancellation. Verification and completion re-scan the boundary. Occupancy is never assumed from an earlier scan.
- **Tests:** `test_fork_during_cancellation_is_terminated_and_rechecked`; `test_fork_during_cancellation_real_children_are_reaped_and_rechecked`; `test_occupancy_is_rechecked_at_completion_not_assumed_from_proof`; `test_parent_exit_with_descendants_keeps_occupancy_and_refuses_release`.
- **Status:** **Modelled.** A real implementation must enumerate descendants through a kernel mechanism, such as `cgroup.procs` plus `cgroup.events` `populated`, which is not available for this worker today.

### R5. Authorized whole-workload termination
- **Enforcement:** termination signals only handles the registry created. It sends SIGTERM first, escalates to SIGKILL after a grace period, re-scans for forks, and reports partial or failed cleanup. It never signals by PID, name or pattern.
- **Tests:** `test_sigterm_ignoring_members_escalate_to_sigkill_and_complete`; `test_sigterm_ignoring_descendants_escalate_only_to_spawned_members`; `test_partial_termination_refuses_completion_and_release`; `test_failed_cleanup_leaves_member_and_refuses_release`; `test_partial_cleanup_real_refuses_then_full_cleanup_releases`; `test_signals_never_reach_non_members`.
- **Status:** Escalation and partial-cleanup behaviour are **Modelled**. Who may authorize termination, and whether the authority check is independent of the worker, is **Documented** only.

### R6. Boundary verified empty before resources are released
- **Enforcement:** `Controller.release` requires a completed, verified empty proof and then re-observes occupancy. Any member still alive refuses release.
- **Tests:** `test_release_is_refused_without_a_verified_proof`; `test_real_simulated_run_proof_refused_by_production_verifier`; `test_stale_proof_from_before_termination_cannot_release`; `test_worker_crash_real_leaves_unknown_state`.
- **Status:** Enforced in the model. The production gate refuses all proofs.

### R7. Completion and release refused while verification is incomplete
- **Enforcement:** `complete()` refuses with no proof, after an interrupted verification, after an observation fault, after any escape, or when the completion re-check sees a live member. A supervisor restart rebuilds state as `UNKNOWN` from the durable journal, with no proof, so it cannot release.
- **Tests:** `test_interrupted_verification_declares_no_completion`; `test_interrupted_verification_declares_no_completion_even_with_older_proof`; `test_recovered_supervisor_cannot_release_without_fresh_verification`; `test_worker_crash_mid_run_stays_unknown_and_unreleased`; `test_parent_exit_with_descendants_refuses_completion_and_release`.
- **Status:** Enforced in the model.

### R8. Caller-constructed or stale proofs rejected
- **Enforcement:** see the proof model. Only a MAC from a trusted anchor, with the exact nonce and epoch, passes.
- **Tests:** `test_direct_dataclass_construction_never_verifies`; `test_tampered_proofs_refused`; `test_untrusted_verifier_and_wrong_key_refused`; `test_non_proof_inputs_never_confer_authority`; `test_process_identity_match_is_observation_not_containment`; `test_stale_replayed_and_unsolicited_proofs_refused`; `test_replayed_proof_from_earlier_epoch_is_rejected`; `test_invalid_tampered_proof_cannot_release`.
- **Status:** Enforced.

## Simulator isolation

- The simulator is under `tests/support/`. No file under `scripts/` names it, its key, or its verifier. A test scans every file in `scripts/` for these names.
- No file under `scripts/` calls `check_proof`, `seal` or `mac_for`, or passes an `anchors=` argument. A test enforces this by AST scan.
- The production verifier refuses simulated proofs, and the production selector never returns the simulator.
- Every synthetic child is started by `SpawnRegistry` as a `subprocess.Popen`, reaped in `tearDown`, and checked against `/proc` markers. Tests assert none remain.

## Out of scope

- Kernel escape, container breakout, and any exploit of the kernel or systemd.
- A root attacker, or an attacker who can read the verifier's key or code.
- Cross-host execution, network-isolated workloads, and remote termination.
- Out-of-band CUDA or GPU jobs started outside the boundary. These remain outside cooperative scheduling.
- PID reuse and process identity as a containment proof. Identity is an observation only.
- Scientific validity of any workload.

## Verification performed for this phase

- Full CPU suite under Python 3.12.14 with no real cgroup, sudo, GPU or research workload. Results and exit codes are in `reports/experiment-infra/w2-logs/`.
- The read-only host observation in `w2-logs/host-observation-readonly.log` confirms `BLOCKED` with exit 2.
- No independent review has been performed. This document does not claim one.
