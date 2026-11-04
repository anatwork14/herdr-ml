# Owner decision — limited Manual Core release (HERDR-ML PR #2)

Date: 2026-11-04. Decision: **APPROVED — limited manual-only Manual Core release.**

Reviewed PR #2 head: `88a37f3`. Branch `fix/p0-reliability-remediation`. PR #2 remains draft and unmerged.

## Gates

| Gate | Status |
|---|---|
| HERDR_WORKSPACE_READY | PASS (live) |
| MULTI_AGENT_READY | PASS (live) |
| TERMINAL_AND_LOGS_READY | PASS (live) |
| GIT_WORKTREE_READY | PASS (live) |
| CORE_RECOVERY_READY | PASS on criteria (a)–(c), with the deviations below |

Authoritative gate report: `reports/reviews/2026-11-03-manual-core-live-integration.md`.

## Criteria accepted for CORE_RECOVERY_READY

- (a) Agent-pane detach and reattach with observed output continuity: `reports/herdr-live/recovery-closure/r2/` (observer frames 23.93–24.60 s inside a detached window 17.31–28.58 s; reattach viewport shows the completed output).
- (b) Restoration of conversation state after a clean disposable named-server restart, without an operator-supplied `--resume` argument: same session `75971b1d-…` restored; recall after restart returned the token after the marker.
- (c) Agent process survival and response completion across detach: claude pid kept its identity 17.8–24.8 s; a prompt sent while detached was answered.

## Scope of this release

Manual Core, manual-only: a human-initiated Herdr workflow in disposable named sessions, with Herdr agent start, prompt, read, observer, terminal attach/detach, worktree and workspace operations, and a clean named-server restart with restoration of the session.

## Excluded capabilities

- Automatic bootstrap.
- Queued runs and queue dispatch.
- Official runs.
- GPU workloads.
- Upgrading production Claude hooks (production remains v9).
- Restarting the production Herdr server.
- Crash recovery (server kill, power loss): not tested.
- The Herdr sidebar TUI: not tested.
- Launch attribution: not proven; restore mechanism inferred from ancestry and the server log.
- Experiment-infrastructure holds (`RUN_OWNERSHIP`, `CONTAINMENT`, `QUEUE`, `PROVENANCE`, `APPROVAL`, `EXPERIMENT_RECOVERY`) remain BLOCKED per `reports/reviews/2026-11-03-manual-core-continuation.md`.

## Evidence SHAs

- Consolidated run evidence: `2e17f9d` (raw evidence and first RESULT).
- RESULT corrections after review: `5cd49b4`.
- Gate verdict and report update: `88a37f3`.
- Final-cycle evidence (prior run, same integration): `4322889`, `24bd1d1`, `2dfcbb3`.
- Closure plan: `14ebf7c`.

## Independent reviews

Saved verbatim in `reports/herdr-live/recovery-closure/review/independent-reviews.md`:

- Final-cycle review at `4322889`: 8 findings (1 HIGH, 4 MEDIUM, 3 LOW). Addressed in `24bd1d1` and `2dfcbb3`.
- Final-cycle re-review at `24bd1d1`: remaining wording items; addressed in `2dfcbb3`.
- Consolidated run review at `2e17f9d`: 5 MEDIUM and LOW items; addressed in `5cd49b4`.
- Consolidated run re-review at `5cd49b4`: criteria (a)–(c) PASS; one MEDIUM (exit-poll procedure) and one MEDIUM (scroll-change acceptance not in repo); the owner decision addresses both.

No CRITICAL or HIGH finding is open.

## Known deviations (recorded, accepted by the owner)

- The prescribed per-second post-exit polling (PLAN.md section 1, step 8) was not followed. A single identity poll of pid 2095038, 3.35 s after `/exit`, reported `GONE_OR_REUSED`; post-exit state (agent `agent_not_found`, pane foreground `bash`) corroborates it. The owner accepts this single poll as sufficient for this limited release. No repeat was run.
- Production `w1:p1` scroll changes (`1235→1239` in the final cycle, `397→405` in the consolidated run) and focus moves are unexplained transient metadata. The owner accepts them as such. Production state is **not** claimed to be byte-identical, and the cause is not proven.
- Stable production identifiers are identical before and after the consolidated run (`recovery-closure/r2/post/comparison.txt`).

## Supported versus unsupported

Supported by saved evidence, limited scope: the four live gates; agent-pane detach/reattach with output continuity; clean named-server restart with session restoration without `--resume` in the restart command, on the isolated v11 config; process survival across detach; marker recall after restart.

Unsupported or not claimed: automatic bootstrap; queued, official or GPU execution; production hook upgrade; production server restart; crash recovery; sidebar TUI; launch attribution; byte-identical production state; restore on the production v9 integration; general or multi-agent restore behavior.

## Authority

This decision is the owner's, given in the conversation of 2026-11-04. Manual Core release is approved only within the scope above. Any broader release requires a new decision.
