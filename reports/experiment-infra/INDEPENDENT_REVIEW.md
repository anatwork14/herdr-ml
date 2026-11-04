# P7 independent review record

Reviewer model for every review below: Opus 5.5 (`claude-opus-5-5`). Statuses use only OFFLINE_TESTED, ISOLATED_CPU_VERIFIED, NOT_TESTED, BLOCKED. Nothing here is a PASS or READY claim for any gate; all six gates are BLOCKED (`FINAL_READINESS.md`). Resolutions are described in `P7_INTEGRATION.md` and summarised in `P7_SECURITY_CLOSURE.md`.

## Summary

| Review | Head reviewed | Final verdict recorded | Independently re-reviewed after repair |
|---|---|---|---|
| (a) late Phase 5 and 6 repairs | `3e6ed54` | repaired at `30df5f6` | yes, by review (b) |
| (b) P7 integrated head | `30df5f6` | repaired at `c47df58` | yes, by review (c) |
| (c) repair head | `0ae5631` | NOT CLOSED: 4 items open | yes, by review (d) |
| (d) repair head | `57f79ef` | NOT CLOSED: 1 HIGH open | yes, by review (e) |
| (e) re-review | `e53987e` | NOT CLOSED: residuals listed below | partly: see (f) |
| (f) follow-up | `f3123b3` | not reviewed by an independent reviewer | no |

## (a) Late Phase 5 and 6 repairs, head 3e6ed54

| Finding | Severity | Resolution (see `P7_INTEGRATION.md`, "Late review findings") |
|---|---|---|
| `record_child` accepts any pid | MEDIUM-1 | Refused with `CHILD_NOT_OWN_CHILD` unless the pid is this process or a /proc child of it |
| `researchctl` spawns before recording | MEDIUM-2 | `start_gated`: pipe gate, record, then release; official and queued paths remain disabled |
| test anchor accepted with any root | LOW-1 | Superseded: mechanism removed in review (e) repair |
| gate released too early in the harness | LOW-2 | Released after record_child, advance and record_descendants |
| documentation of session match | LOW-3 | RECOVERY.md and docstring corrected |
| claimant namespaces | LOW-4 | Claimant carries pid_ns and mnt_ns; other namespace gives UNKNOWN, recovery defers |

## (b) P7 integrated head, 30df5f6

| Finding | Severity | Resolution |
|---|---|---|
| Item 4: a live owner was SIGTERMed after `recovery_fence` (reproduced) | MEDIUM | R1: `recovery_fence` observes owner liveness under the write lock; live or undeterminable owner refuses with `OWNER_LIVE` |
| Latent spawn-before-record in the boundary | MEDIUM | R2: gated spawn; record before release; failure kills the child group and refuses `LAUNCH_RECORD_FAILED` |
| Latent spawn outside containment | MEDIUM | R3: boundary calls `backend.spawn_gated`; production backend raises `ContainmentUnavailable` |
| Test-root residual | LOW | R4 (later superseded, see (c) and (e)) |
| Handoff mode | LOW | R5 (later superseded) |
| Dispatch flag unused | LOW | R6: `scripts/dispatch_guard.py` checks the flag at import and in the boundary and researchctl |

Repair commit: `c47df58`.

## (c) Repair head 0ae5631 (verdict NOT CLOSED)

Prior items closed except the four below.

| Item | Detail | Disposition |
|---|---|---|
| TMPDIR-controlled temp base | `_test_root` used `tempfile.gettempdir()` | Repaired at `57f79ef` (fixed `/tmp` base); the mechanism was later deleted |
| Test-mode guards reachable by any caller | guards were not a control | Led to (d) and (e) |
| Dispatch docstring conflict | flag location documented inconsistently | Flag defined in `dispatch_guard.py`; `queue_adapter` imports it |
| INFO: attribute-based mode guard | instance attribute could imitate a test verifier | Replaced by isinstance check, then removed with the mode |

## (d) Repair head 57f79ef (verdict NOT CLOSED)

| Finding | Severity | Resolution |
|---|---|---|
| `TEST_CAPABILITY` reachable from `scripts/`; attribute-based `test_only` trust | HIGH | Repaired at `e53987e`: all test-only acceptance removed from `scripts/approval.py` |

## (e) Re-review of e53987e (verdict NOT CLOSED)

Confirmed: test-only acceptance is gone from `scripts/`. Residuals recorded by the reviewer:

- Caller-supplied admin uids in the generic loader (`load_trust_anchor(admin_uids=...)`). The production entry point `load_production_trust_anchor` pins `PRODUCTION_ADMIN_UIDS = (0,)`; the generic loader remains a residual.
- The AST guard (`tests/test_approval_ast_guard.py`) detects only named patterns; it is incomplete by nature.

Test change: 17 test methods were removed. Each tested a mechanism that was deleted (`TestAnchorContextTests`, `TestRootGuardTests`, `FixedTempBaseTests`, `TestCapabilityTests`, `HandoffModeTests`, two test-only-verifier tests). The retained semantics keep their assertions. No assertion in `tests/test_registry_p0.py` or `tests/test_queue_protocol.py` changed.

## (f) f3123b3

`fix(p7): pin production admin uids and extend the approval source guard`, a LOW follow-up addressing the (e) residuals partly (production entry point pins root; guard extended). It was verified by the lead only: suite 592 tests OK on Python 3.12.14. **It was not separately re-reviewed by an independent reviewer.** The generic-loader and AST-guard residuals remain open.

## What was not independently re-reviewed

- Head `f3123b3` and its changes to `scripts/approval.py` and the AST guard.
- The whole integrated tree after `e53987e`.
- Any live behaviour: no review covered a live host; none was possible (no delegated containment, no trust anchor).
- The reviews did not cover Python 3.10 or 3.11 (NOT_TESTED).
