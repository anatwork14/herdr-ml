# Independent reviews — recovery evidence (verbatim)

Saved verbatim from the reviewer hand-backs in the integration session. Reviewers were read-only and given evidence paths, not the author's conclusions. Each review is the reviewer's own report.

---

## 1. Final-cycle review at 4322889 (`reports/herdr-live/integration-upgrade/final/`)

Independent review of reports/herdr-live/integration-upgrade/final/ at commit 4322889. Read-only. I did not read /mnt/ssd/genos2/.claude, so I could not re-run the production hash check; I checked only that after/prod-hashes.txt has sha256sum -c format and that the baseline hash file exists. Commit touches only final/ files.

Findings:

1. HIGH. RESULT.md:29 and :33. The claim "no command in this cycle targeted that pane's scroll" is unsupported. The default session also has a w1:p1 (baseline/panes-w1.json: a live antigravity pane, agent_status "working", terminal term_65f481531047e1). The only default-session change is max_offset_from_bottom 1235 to 1239 in that pane (verified by diff). The /exit sends (run/29, run/30) were at 23:59:40 and their outputs are empty. No command with the --session flag is recorded for them, so the target session is unknown. Fix: record the exact /exit commands with --session. Check the default w1:p1 scrollback for a stray "/exit". If this cannot be resolved, state the change as unexplained and open, not as "no command targeted".

2. MEDIUM. RESULT.md:29, :31, :42; after/isolated-config-process-count.txt. The 0-process count was taken at 00:00:54, after the server stop (run/32 at 23:59:49). Server shutdown kills pane children (run/22: "pane session terminated ... signal=Kill"). So the count cannot show that /exit worked. run/29, run/30 and run/31 are empty, so there is no pre-stop exit evidence. The count method is not recorded anywhere. Fix: capture the process scan (with the command) after /exit and before the stop, save the send exit codes, and reword line 29.

3. MEDIUM. RESULT.md:3. "Production /mnt/ssd/genos2/.claude was not read" is contradicted by after/prod-hashes.txt, which hashes two files under that path. Also, the check covers only two files, so "not changed" holds only for those two. Fix: "not linked or modified; two files hash-verified (after/prod-hashes.txt)".

4. LOW. RESULT.md:15. run/08-prompt-token-length.txt says 25. The token MLFINAL_0bf5c0c9885e638b is 24 characters; run/token.txt is 25 bytes including the newline. Fix: state 24 characters, or say the value is a byte count including the newline.

5. LOW. RESULT.md:25. "the token appears after the marker in the reply" is misleading. The marker is in the prompt (run/26), and the reply line (run/28 line 17) contains the token. Token count 2 is correct (run/28 lines 8 and 17). Fix: "the reply contains the token; the token occurs twice in run/28 (original prompt echo and reply)".

6. LOW. Startup and trust dialog. run/03-agent-start.err returns agent_not_ready, but the server log shows agent.start completed ok (run/22, 16:58:20). The trust-dialog keystrokes (run/04, run/05) are not mentioned in RESULT. Fix: add one line.

7. LOW. RESULT.md:41. "hooks identical to P1" rests only on run/00-note.txt. No diff or hash is in the directory. Fix: add the diff or hash output.

8. LOW. RESULT.md:31. Says run/31 "used a cwd pattern", but the file is empty, so no output was recorded. Fix: say that no output was saved.

Verified as supported: the restore chain. run/13 has session 2d592efa in the pre-stop state. run/15 and run/16 show a clean stop with exit 0 and the process gone. run/17 shows the restart command with no agent arguments. run/19 shows restart_needed no. run/22 and run/23 show "session restore evaluated ok, workspaces=1", a pane spawn of pid 356269, and the agent changed with pgid 356542. run/21 shows argv "claude --resume 2d592efa", parent 356269 and grandparent 356210, which matches the server pid in run/22. run/20 and run/24 show the same session id and a new terminal. run/26 contains 0 token occurrences. run/27 exit 0. The 17-24 chain supports "restored after clean stop, no operator --resume in the restart command". The mechanism is correctly labelled inferred. Secret scan: no credential patterns, no .credentials or config-path references; the only token is the MLFINAL test string, as allowed. Inventory: baseline and after match for every file except the w1:p1 scroll field (finding 1).

Recommended gate verdict: PENDING. The one-case restore chain (runs 13 to 28) is internally consistent and supports the narrow claim "clean stop then restore with the same session, no operator --resume in the restart command". It does not establish the restore mechanism, which the RESULT already says. Crash recovery and sidebar were not tested. Still open: the /exit cleanup has no pre-stop evidence, the post-stop count cannot distinguish /exit from the server stop, and a live production pane in the default session changed during the window with its /exit target session unrecorded. Finding 1 needs resolution, or an explicit statement that it is unexplained, before this could be read as a clean production-isolation result. Fix findings 1 to 3 and re-issue.

---

## 2. Final-cycle re-review at 24bd1d1

Independent re-review of reports/herdr-live/integration-upgrade/final/RESULT.md at 24bd1d1 (read-only). Evidence files read: run/token.txt, run/08-*, run/00-note.txt, run/03, run/04, run/10, run/21, run/22, run/25, run/26, run/28, run/29-31 (empty), run/09 and run/27 exit files, run/14-16, run/32, after/prod-hashes.txt, after/isolated-config-process-count.txt.

1. Default w1:p1 scroll (HIGH): RESOLVED as wording. L10 and L32 now list the 1235 to 1239 change as open and unexplained, and no longer claim the pane was untargeted. Caveat: L32 cites a "last 200 lines" read that has no evidence file in final/, so that claim is uncited. The change itself remains unexplained.

2. Cleanup before stop (MEDIUM): RESOLVED. L30 says the exit before stop is not verified, and L31 says the post-stop count of 0 cannot show /exit worked. run/29, run/30 and run/31 are empty, and no exit codes were saved (L30 says so).

3. Production wording (MEDIUM): PARTIALLY RESOLVED. The "unread" wording is gone. L35 cites two hash-verified files (after/prod-hashes.txt: both OK). But L3 still says the production `.claude` was "not linked or modified", which only two files support.

4. Token length (LOW): RESOLVED. run/token.txt is 25 bytes, 24 without the newline. L16 states this correctly.

5. Recall wording (LOW): RESOLVED. run/26 contains only the RECALLFINAL marker, with 0 MLFINAL occurrences (L25). run/28 contains the token twice, once in the echoed prompt and once in the reply `● MLFINAL_…` (L26).

6. Startup/trust (LOW): RESOLVED. run/03 shows agent_not_ready, run/04 shows the "Yes, I trust this folder" selection, and run/22 shows agent.start ok at 16:58:20. The "down then enter" keystrokes rest on the empty run/04-trust-down.json and run/05 files, so they are the orchestrator's account only.

7. Hooks-identical (LOW): RESOLVED. L44 labels the claim as resting only on run/00-note.txt, and no diff or hash is saved.

8. run/31 empty (LOW): RESOLVED. L30 says the file is empty and records no check output.

Gate: CORE_RECOVERY_READY should stay PENDING. The restore is observed in one case (run/22, run/21, run/24). Launch attribution is unproven, the default w1:p1 change is unexplained, and the isolated agent's exit before stop is unverified.

Remaining overstatements:
- L7 says restoration is "demonstrated". L40 says the mechanism is not proven, so "observed (one case)" is the honest wording.
- L3 "not linked or modified" (see item 3).
- L32 "last 200 lines" claim has no cited evidence.

---

## 3. Consolidated run review at 2e17f9d (`reports/herdr-live/recovery-closure/r2/`)

Independent review of r2 at 2e17f9d (read-only; no repo files changed). Base path: reports/herdr-live/recovery-closure/r2/.

FINDINGS

MEDIUM-1 RESULT.md:35 and :27. The answer-frame method is wrong. 20-observer-answer-check.txt flags has_40 True on the prompt-echo frames (21.898, 24.399-24.466) and False on the answer frames 23.930-23.967, where decoded text shows "...3940 DETACHED-WINDOW-DONE" (19-observer-timeline.txt seq 15-16). Answer rendering starts at 23.930, not 21.9. The marker is present through 24.600 (seq 23), not 24.4. The conclusion survives: 18-pty-attach-2.bin contains 40, the marker and "Baked", and 43-recall-transcript.txt lines 17-56 show the full 1-40 plus marker. Fix: cite decoded frames with corrected times, drop the has_40 identification, and cite 43 for the number check.

MEDIUM-2 RESULT.md:59, 51, 50. "Verified exit" and "the original pid was gone at the first check" overstate the evidence. PLAN section 1 step 8 requires per-second polls for up to 60 s. 50-exit-polls.txt has one line, GONE_OR_REUSED, which does not separate gone from reused. It was taken at 02:46:47.558, 3.35 s after the send at 02:46:44.207 (run/47). 51 says elapsed=0s, which is unexplained. The corroboration exists but is not cited: 52 (agent_not_found) and 54 (pane foreground is bash pid 2094749). Fix: say "one identity poll, GONE_OR_REUSED, plus post-exit pane state" or add the per-second polls.

MEDIUM-3 RESULT.md:70. "The owner accepted it as unexplained transient metadata" has no record in the repo. PLAN.md:84 (section 4 item 3) lists this as a pending owner decision, and PLAN.md:77 says the closure decision is there. Fix: cite the decision or write "pending owner decision".

MEDIUM-4 RESULT.md:69. "not changed by this run's commands" is unsupported. No command log or --session wrapper evidence exists in r2 (PLAN section 0 says the wrapper's refusals are evidence). The cause is also unrecorded. Fix: delete the causal clause or save the command log.

MEDIUM-5 RESULT.md:45, run/38, run/31. "CLAUDE_CONFIG_DIR matches the isolated config (count 1, names/values only)." 38 contains only "env CLAUDE_CONFIG_DIR matches: 1", with no pid and no expected value. Nothing shows the first agent (pid 2089091) used the isolated config; PLAN section 1 step 6 env-name capture is absent from r2. 31 has a placeholder "<iso>". Fix: save env names and the per-pid comparison target; delete "values".

LOW
- RESULT.md:3. "Not linked or modified": the hash check covers two files only, and "not linked" is unverified. "Default server not restarted" rests on running:true only (sessions.json). Reword.
- RESULT.md:42. run/29 does not name pid 2089091. proc-poll.txt shows it GONE from 02:45:56.935; cite that.
- run/37 is a scan filtered to argv starting "claude --resume", so it cannot rule out another restored process. Cite run/46, which lists only pid 2095038 pre-exit.
- RESULT.md:9. Verdict (a) "reattach of agent output" reads as streaming. 18 .bin is a viewport snapshot; line 81 says so, so align the verdict.
- run/token.txt has no timestamp. "Before any prompt" rests on mtime 02:44:53.2 against the send at 02:44:56.5. 10-token-prompt-sent-at.txt holds two unlabeled timestamps (send 56.506, return 58.914).
- run/03 says "blocked during startup", not the trust dialog. Linking it to the dialog is inference (04 shows the dialog).
- RESULT.md:12 "documented pane interface" and the pty_attach_once.py and observer harness are not saved in run/. Cite or save them.

VERIFIED WITHOUT FINDING
One start (only run/03 among run/ files); trust down/enter (05, 06, 07). Token is 20 characters; it appears in no prompt except 10-token-prompt-text.txt (14 and 41 contain 0). Restart has no agent args (31); session id matches (21 vs 35). Recall: marker 41 has 0 token occurrences; reply at line 63 follows marker at line 60. Stable inventory: I recomputed flat-JSON diffs for all eight files. Only focused flags and panes-w1 pane[0] (w1:p1) scroll 397 to 405 differ, matching comparison.txt exactly. Prod hashes OK before and after. Isolated count 0. Session deleted. run/39 server-log lines agree with 37 (parent 2094749, pgid 2095038). Credentials: no hits for sk-, api key, password, bearer, oauth, PRIVATE KEY or email patterns in r2.

VERDICT: PENDING
The three contract items meet PLAN section 6 on the evidence. (a) Output was generated in the attach-free window (decoded frames 15-23 at 23.93-24.60, reattach capture, transcript). (b) Restore kept the session id with no agent args, argv and ancestry were saved, and recall after the marker succeeded. (c) The process was alive and the prompt was answered while detached. No CRITICAL or HIGH finding. I still recommend PENDING because the RESULT states several things more strongly than its evidence (MEDIUM-1 to -4), and PLAN section 3 makes the unrecorded owner decision on the scroll change a closure input. After those corrections and the recorded decision, no new run is needed and the gate can move to PASS.

---

## 4. Consolidated run re-review at 5cd49b4

Independent re-review of r2/RESULT.md at 5cd49b4 (read-only; no files changed, no Herdr commands run, .claude and credentials not read).

VERDICT on criteria (a), (b), (c): all three are supported by the cited files. Verdict for those criteria: PASS.
- (a) Detach at 17.313 (run/13-pty-attach-1.stdout, exit 0). Prompt sent 21.523 while detached. Answer frames 15 to 23 at 23.930 to 24.600 (run/19, frame 23 DONE flag True). Reattach at 28.579, exit 0, and run/18-pty-attach-2.bin contains DETACHED-WINDOW-DONE.
- (b) Same session 75971b1d (run/21, run/35). run/31 restart is `herdr --session mlrec-close-r2 server` with no agent arguments. run/39 shows the restore log and restored pgid 2095038 (argv `claude --resume` comes from herdr's restore, which is accepted under the criterion).
- (c) proc-poll.txt shows the pid 2089091 start time 475608787 unchanged 07.8 to 55.9 s. Prompt returned idle at 24.229 (run/15, run/16). run/17 shows 1 to 40 and the marker.

Checks requested:
1. Frame times 23.930 to 24.600, seq 15 to 23: correct in run/19. Unverified part: the "...3940" text. No cited file shows decoded text. run/20 (has_40) shows False at seq 15, 17 and 23, and contains_DONE_line False for every frame, which conflicts with run/19. run/20 is not cited in RESULT.md but remains in run/ unexplained. LOW.
2. run/43: the 1 to 40 sequence and the marker are present, but "1" is on line 16, not 17. Range should be 16 to 56. LOW.
3. run/50 is one poll, GONE_OR_REUSED at 02:46:47.558, matching the text. run/51 "elapsed=0s" has no script in the repo to show why. LOW.
4. run/46 contains pid 2095038. Confirmed.
5. run/38 contains no pid. The pid 2095038 label comes only from RESULT.md. LOW.

Previous findings:
- MEDIUM-1 (frame times, has_40): resolved in RESULT text. Frame-level checks are no longer used for the numbers.
- MEDIUM-2 (exit described as verified): resolved. Text now says GONE_OR_REUSED.
- MEDIUM-3 (owner acceptance of the scroll change): still open. It is acknowledged as conversation-only, not in the repo. MEDIUM for production isolation.
- MEDIUM-4 (focus causality): resolved, cause stated as unrecorded.
- MEDIUM-5 (env check scope): partly resolved (see item 5).
- LOW items: mostly resolved. The pid 2089091 absence cite (run/29) names no pid. The "done_marker_in_bin True" field is not in run/18 stdout, though the .bin confirms the marker.

Remaining findings:
- MEDIUM (new): PLAN.md step 8 requires per-second identity polling for up to 60 s. Step 10 says that if exited_before_stop is not true, clean exit records FAIL. The run did one poll and then stopped the server. Under the plan's rule, clean exit is not met. This does not affect (a) to (c), but it blocks an unqualified READY.
- MEDIUM: the production scroll change (397 to 405, run/comparison) is unattributed. It is acceptable only with the owner's acceptance recorded in the repo.

CORE_RECOVERY_READY: PASS for criteria (a), (b) and (c). I would not stamp overall READY until the clean-exit gap is either fixed or explicitly waived in the repo, and the scroll-change acceptance is recorded. The RESULT's own PENDING recommendation is reasonable on those two points.
