# CORE_RECOVERY_READY closure plan (draft, not executed)

Status: **proposal for approval.** Nothing in this document has been run. No agent, Herdr test server or production pane is touched by drafting it.

Starting SHA: `2dfcbb3c7600d4d99a6b9c8b6e5f6414fb386b00` (PR #2 branch `fix/p0-reliability-remediation`, draft, not merged).

Current state: `CORE_RECOVERY_READY = PENDING_INDEPENDENT_REVIEW`. `Manual Core release = BLOCKED`. Four live gates are PASS subject to their recorded limits (`reports/reviews/2026-11-03-manual-core-live-integration.md`).

## 0. Standing rules for every run

- Every Herdr command carries `--session <name>`. Test runs use only `mlrec-close-r1` and `mlrec-close-r2`. The default session is read-only (`workspace list`, `tab list`, `agent list`, `session list`, `pane list`, `pane get`).
- A wrapper script refuses to run any `herdr` command without `--session`, other than the read-only default-session list above. The wrapper's refusal is part of the evidence.
- No `pane read`, `pane send-*`, `pane run`, `agent *` or `terminal *` against any production pane (`w1`, `w3`, `w5`, `w6` in the default session). Production panes are inspected only through metadata (`pane list`, `pane get`).
- Do not read or copy anything under `/mnt/ssd/genos2/.claude`, any credentials file, or any production pane's scrollback. Do not retry a read that a permission classifier has denied.
- Production hooks, `settings.json`, credentials and the default server are not changed or restarted.
- Bootstrap, official execution, queues and GPU work stay disabled.
- Permission denials stop the operation. Do not split or reroute the denied command.

## 1. Part A: verified cleanup (run R1, one agent)

Goal: show that the agent exits through the documented `/exit` before any server stop, and that the process exit is not confused with a server-induced kill.

Preconditions: named session `mlrec-close-r1` does not exist. Isolated v11 config as used in `integration-upgrade/final/` (authenticated). Fixture cwd is a fresh directory under the scratchpad, not the project tree.

Sequence:

1. Baseline read-only inventories (default session): `workspace list`, `tab list`, `agent list`, `session list`, `pane list --workspace w1|w3|w5|w6`, each saved with timestamp and exit code. Save to `baseline/`.
2. Start the named server: `herdr --session mlrec-close-r1 server` (background). Save stdout, stderr, exit code.
3. Create a disposable workspace, `workspace create --cwd <fixture> --label close-r1 --no-focus`. Save output.
4. Start the agent: `agent start close-agent --kind claude --pane <pane> --timeout 90000` with the isolated `CLAUDE_CONFIG_DIR`. If the trust dialog blocks it, use `pane send-keys` with `down` and `enter` on that named session's pane only, saving the screen read before each key. Save all outputs.
5. Send one harmless prompt (`agent prompt close-agent "Reply with only OK." --wait --timeout 120000`). Save outputs.
6. **Capture the pre-exit identity** (before `/exit`):
   - `agent get close-agent` (pid, state, session id, terminal id), saved.
   - `pane process-info --pane <pane>` saved.
   - From `/proc/<pid>`: `stat` (pid, pgid, session id, field 22 start time), `cmdline`, and the list of child processes. Saved as text, with environment variable names only, no values.
   - `herdr session list --json` for the named session `running: true`.
7. Send the exit through the documented interface, with each exit code saved to its own file:
   - `herdr --session mlrec-close-r1 pane send-text <pane> "/exit"` → `08-send-text.{json,err,exit}`
   - `herdr --session mlrec-close-r1 pane send-keys <pane> enter` → `09-send-enter.{json,err,exit}`
8. Poll the original pid's identity, not the name. Each second for up to 60 s, check whether `/proc/<pid>` still exists with the same start time. Record each poll. Result is `exited_before_stop: true|false` and the elapsed time.
9. Capture the agent's lifecycle state after exit: `agent get close-agent` (expected: agent gone or no longer `claude`), `pane get <pane>`, `pane process-info --pane <pane>`. Save all.
10. Only if step 8 shows `exited_before_stop: true`, continue. If it is false, the run records a FAIL for clean exit and goes to the stop step with that failure written down. It does not count as a clean exit.
11. `herdr --session mlrec-close-r1 server stop` → exit code saved. Then poll the same pid identity for the post-stop state. This is recorded as server-induced termination if the process was still alive at step 10.
12. Delete the session: `herdr session delete mlrec-close-r1 --json`. Save.
13. Post inventories, the same read-only set as step 1, saved to `post/`. Compare stable fields and list ephemeral fields separately.
14. `sha256sum -c` on the two hash-verified production files, against the pre-test hash file, saved to `post/prod-hashes.txt`.

**Pass criteria for Part A:**
- Step 6 identity captured for the agent pid.
- Steps 7 send exit codes saved, both 0 (or the actual codes, recorded).
- Step 8 shows `exited_before_stop: true` within the timeout.
- Step 9 shows the agent is not running.
- Stable inventory fields identical to baseline, and the default session still running.

**Estimated agent starts:** 1. Maximum 2 if the first start is refused at the trust dialog and retried, which is what happened in cycle 1 of the recovery work. A third start is a stopping condition.

## 2. Part B: restoration repeat (run R2, one agent), optional

Needed only if the closure contract requires more than the existing single restore case. See section 4.

Sequence: same as the final cycle (`integration-upgrade/final/`), in session `mlrec-close-r2`:
1. Baseline, server start, workspace, agent start.
2. One token-establishment prompt; token saved before any prompt, length asserted > 0.
3. Pre-stop capture: agent pid, pgid, start time, `agent get`, `pane get`, `session.json` field names only.
4. `server stop` (exit saved), confirm the pid is gone.
5. `herdr --session mlrec-close-r2 server` with no agent arguments; save outputs.
6. Post-restart `agent get`, `pane get`, restored process argv and ancestry, and the server log restore lines.
7. One marker-only recall prompt; check the token appears after the marker in the reply, and that the prompt text itself contains no token.
8. Part A's exit and cleanup sequence to finish, with the same verified exit.

**Estimated agent starts:** 1 (maximum 2, as above).

## 3. Part C: production isolation rules

- Stable fields compared: workspace IDs and labels, tab IDs, agent names/panes/kinds/sessions, session list, pane IDs for `w1`, `w3`, `w5`, `w6`.
- Ephemeral fields reported separately and never used as a PASS/FAIL input: `focused`, `scroll.*`, `revision`, `seq` counters, `agent_status`, completion counters.
- Known open item: default `w1:p1` `scroll.max_offset_from_bottom` 1235 → 1239 in `integration-upgrade/final/`. This plan does not assume a cause. The closure decision is in section 4, item 3.
- Metadata-only reads of production panes. The denied scrollback read is not retried.

## 4. Decisions for approval

1. **Run R2 (restoration repeat):** needed or not? Recommendation: run it. The only restore case so far is one run on v11. A second clean-restart run would show repeatability on the same integration, though not general behavior.
2. **Contract for CORE_RECOVERY_READY.** Recommendation: keep the contract as written in the gate report (a) detach/reattach of agent output, (b) restoration after a clean named-server restart, (c) process survival across detach. Items (a) and (c) already have one case each on v9, and (b) on v11 (final cycle). Do not add crash recovery or the sidebar to this gate without a separate decision.
3. **Unexplained production scroll change.** Options: (i) accept it as an unexplained transient field change and record it, which allows closure; (ii) keep the gate open until a cause is found. Recommendation: (i), with the change stated in the report. The final cycle's `/exit` sends went to the named session's `w1:p1`, not the default session's `w1:p1`; the IDs overlap because both sessions use `w1`. No recorded command sent input to the default `w1:p1`, but the final cycle did not record a pane-level check to prove it. The reviewers found no evidence of test interference. Owner decision.
4. **Attach-window output (gap in (a)/(c)).** Not testable without a detach window with observed output. Recommendation: include an observer on the test agent's own pane during a detach in R1 or R2, so a single run closes this gap. This adds no agent start.

## 5. Expected evidence files

```
reports/herdr-live/recovery-closure/
  PLAN.md                     this file
  r1-cleanup/
    baseline/                 read-only inventories, default session
    run/                      server, workspace, agent start, prompt, exit sends, polls, stop, delete
    pre-exit-identity.txt     agent pid, pgid, start time, state, child list (no env values)
    exit-polls.txt            per-second identity checks
    post/                     post inventories, prod hash check
    RESULT.md
  r2-restore/                 same layout, if approved
  review/                     reviewer brief, reviews, commit SHA reviewed
```

Every `herdr` command's argv, timestamp, stdout, stderr and exit code is saved as one file, the same way as in `integration-upgrade/final/run/`.

## 6. PASS / FAIL / PENDING criteria

| Item | PASS | FAIL | PENDING |
|---|---|---|---|
| A. Exit before stop | exit sends saved; process identity gone before stop | process still alive at stop, or sends not saved | not run |
| A. Stop and delete | exit 0 for stop and delete; default still running | either step fails | not run |
| B. Stable production inventory | identical to baseline | any stable field differs | not run |
| B. Ephemeral changes | reported; not a gate input | none | none |
| (a) detach/reattach with output | observer frames + pty detach saved, output seen during detach | no output observed during detach | not run |
| (b) clean restart restoration | session restored with same id, no operator `--resume` in restart command, argv and ancestry saved | any element missing | only one case available |
| (c) survival across detach | prompt answered while detached, saved | no answer | not run |
| CORE_RECOVERY_READY | all of (a), (b), (c) PASS in independent review | any FAIL or a review HIGH finding | otherwise |

## 7. Untestable or deferred requirements

- **Launch attribution.** The Herdr server log does not record argv or the sender of the resume. The gate can state only that the restore was observed. Testing it would need a Herdr logging change, which is out of scope.
- **Crash recovery.** A server kill is destructive to the named server. It can be run in a disposable session, but it is not part of this contract. Deferred unless approved.
- **Sidebar TUI.** Not testable from the CLI. Deferred.
- **Up-to-date production integration.** The production hook is v9. Upgrading it is prohibited by this plan's rules. Restoration on the production default session is not tested and must not be.

## 8. Stopping conditions

Stop the whole cycle and report if any of these happens:

- Any stable production field differs from baseline, or the default session stops running.
- Any Herdr command is run without `--session`, other than the read-only default list.
- Any input is sent to, or pane content read from, a production pane.
- A third `agent start` is needed in a single run.
- A permission denial. The denied operation is stopped and not rerouted.
- Any credential material or file under `.claude` or a credentials path would be read or copied.
- The agent pid identity cannot be determined before `/exit`.
- The named server or session is not the one created by the run.

## 9. Review

- Obtain an independent review at the immutable commit that contains the cycle's evidence. The reviewer is given the evidence paths and this plan, not the author's conclusions.
- Resolve every confirmed HIGH or CRITICAL finding, and re-review the affected evidence.
- CORE_RECOVERY_READY becomes PASS only if the review confirms the criteria in section 6, and no other gate changes.
- Manual Core release stays BLOCKED until CORE_RECOVERY_READY is PASS and the release decision is made by the owner.
