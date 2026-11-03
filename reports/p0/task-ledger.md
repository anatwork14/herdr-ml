# PR 2 P0 task ledger

Base `d4ff2ac5159f5e94a1717fdb2deb8a328ba7f4b0`; main `18ca3ef4600791394f32981c0f738b9b6f76f575`. The original review and prior progress report remain unchanged. This ledger records in-progress work, not approval.

| Task/finding | Owner | Dependencies | Verification | Current result |
|---|---|---|---|---|
| H1 transactional ownership, fencing, audit, crash recovery | ownership worker | containment and Pueue for safe recovery and queue binding | 20 worker test; crash boundaries; registry tests | IN PROGRESS; hold remains |
| H2 process containment and slot release | orchestrator | delegated cgroup or equivalent user supervisor; ownership protocol | inspect cgroups and user bus; CPU process-tree tests | BLOCKED: hybrid cgroup mount root-owned/read-only to user; systemd user bus unavailable in current context; queue hold remains |
| H3 source/data/split/evidence and authenticated approval | provenance worker | trust anchors, immutable execution, independent scientific review | artifact mutation/signature tests; independent review | IN PROGRESS; official hold remains |
| H4 Herdr reconciliation | Herdr worker | real authorized Herdr session and reliable server identity | mocked crash/idempotence tests, then isolated real Herdr | IN PROGRESS; actual session BLOCKED outside Herdr; bootstrap hold remains |
| Python interpreter consistency | orchestrator | pinned runtime and actual daemon | interpreter identity checks and CI; daemon handshake | IN PROGRESS; Pueue absent and daemon consistency BLOCKED |
| Pueue cross-project queue and service restart | orchestrator | Pueue installed, isolated daemon, safe containment | CPU-only queue integration and restart | BLOCKED: pueue and pueued absent |
| Independent review | orchestrator | immutable candidate SHA, available independent reviewer | persist focused requests/logs and review/fix/review | IN PROGRESS; previous Opus timed out; probing runtime and retrying |
| Final readiness and PR update | orchestrator | preceding tasks and review | tests, exact SHA, review report | IN PROGRESS; PR remains draft |

Environment observations: `HERDR_ENV` unset; Herdr skill forbids inspecting or controlling a session. `mount` shows hybrid cgroups; `/sys/fs/cgroup/unified` is root-owned and not writable by this user. `systemctl --user is-system-running` failed to connect to bus both with default environment and `XDG_RUNTIME_DIR=/run/user/5011`. Pueue binaries not on PATH. No GPU workloads have been launched.

## 2026-11-02 execution and review

- The requested Claude aliases were checked, not inferred: `opus`, `sonnet`, and `haiku` returned model IDs `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` respectively (`model-probes.json`). Implementation workers use the available team runtime, and the independent focused reviews use actual Opus 5.5.
- Implementation commits integrated: runtime fingerprint `24b703b`, provenance `eff54bb`, Herdr mock core `93f84d1`, transactional ownership `1376f40`; stricter provenance `db5dc14`. The original reports are preserved.
- CPU integration at `1376f400e5df7bd1ae030a185c961a8cb50c9b6d`: 34 tests pass on Python 3.10.21 and 3.12.14; includes 20 workers, crash simulations, signed artifacts, and mocked Herdr. These tests do not establish real containment or scientific correctness.
- Independently requested four Opus reviews at `1376f400e5df7bd1ae030a185c961a8cb50c9b6d`, with inputs and reports in `reviews/`. The containment review found two HIGH defects: queue-name `local` bypass and SIGHUP orphaning. Both are assigned to ownership worker for repair/retest/re-review. Other reviews identified medium process, Herdr state-loss and provenance availability/test gaps, also assigned for repair. Review findings are evidence, not blanket approval.
- Pueue 4.0.4 Linux binaries downloaded to a separate XDG-isolated user directory; `setup-gpu-queue.sh` configured one group. Two harmless project-labeled CPU tasks ran sequentially and completed records survived a clean daemon restart. A daemon-launched CPU task verified Python 3.10.21 fingerprint. See `pueue-cpu-evidence.json`. This is not a researchctl queued integration, crash restart, cgroup, GPU or production service test. The isolated daemon was shut down afterward.
- Sonnet 5.5 advisory is recorded under `reviews/`; it correctly identifies the absence of execution-bound provenance and trusted independent authorization. Its earlier statement that Pueue is unavailable was superseded by the isolated binary test above, not by a system installation.

### Review finding disposition

| Finding | Owner | Dependency | Verification | Result |
|---|---|---|---|---|
| Opus containment HIGH-1: `--queue local` bypass, including legacy records | ownership worker | guard at submission and worker | `python3 -m unittest discover -s tests -p test_p0_guards.py -v` | Repaired in `6381655`, `7c1450f`; Opus re-review at `7c1450f` says resolved. |
| Opus containment HIGH-2: SIGHUP orphans child | ownership worker | process-group supervision | `python3 -m unittest discover -s tests -p test_p0_ownership.py -v` | SIGHUP repaired in `6381655`; Opus re-review says partial due SIGQUIT and uncatchable death. |
| Opus ownership/containment MEDIUM: SIGQUIT, missing child identity, misleading completion | ownership worker | safe identity and verified containment | adversarial process tests; focused Opus re-review | IN PROGRESS; uncatchable death / detached process containment remains BLOCKED on host support. |
| Opus ownership MEDIUM: post-spawn errors abandon child; same-group survivors after leader exit; mixed legacy claim race | ownership worker | process-group and mixed-version fencing | ownership and registry tests | Repaired in `6381655`; Opus re-review says resolved except truthful survivor reporting, in progress. |
| Opus Herdr MEDIUM: state loss duplicates layout; non-fork crash test | Herdr worker | authoritative inventory; mock adapter | `python3 -m unittest discover -s tests -p test_herdr_reconcile.py -v` | Repaired in `26bd57a`; Opus re-review says fixed. Actual adapter/session still BLOCKED. |
| Opus Herdr MEDIUM: unrelated unverifiable managed workspace blocks onboarding | Herdr worker | identity cannot safely be ruled out without a resolvable path | same Herdr tests, including error details | Deliberately remains fail-closed; `95f7b08` identifies workspace and documents operator reconciliation. |
| Opus provenance MEDIUM: FIFO/symlink manifest and false-positive attack tests | provenance worker | safe file handling and valid adversarial fixtures | `python3 -m unittest discover -s tests -p test_provenance.py -v` | Repaired in `181c966`; Opus re-review at `7c1450f` says fixed. |
| Sonnet advisory: JSON strictness, signatures and exact duplicate splits | provenance worker | valid signed fixtures | same provenance tests | Repaired in `db5dc14`; duplicate hashes do not prove scientifically disjoint splits. |
| Sonnet advisory: valid evidence must not bypass official hold | provenance worker | valid signature and clean Git fixture | same provenance tests | Negative end-to-end test added in `adf7cf2`; official hold remains. |
| Opus runtime HIGH: probe can match different effective runtimes | provenance worker | fingerprint hardening; immutable environment and execution boundary | runtime identity tests; focused Opus re-review | IN PROGRESS; package contents/actual job environment remain unproven, queue hold stays. |
| Opus runtime HIGH: probe not bound to task/run | orchestrator | actual worker wrapper, run-token binding, containment and daemon | actual daemon-launched job handshake, registry evidence | BLOCKED; synthetic daemon probe is not a handshake with researchctl's queued run. |
| Other Opus LOW findings | orchestrator | see individual review reports | per-finding checks in `reviews/*-opus-rereview.md` | OPEN unless explicitly recorded as fixed; no approval implied. |

### Integrated candidate `29b32a4`

- Second Opus pass at `7c1450f149ebf97644e581354379f934adea914a` found no CRITICAL/HIGH in ownership, process-group control, Herdr mock core or provenance verifier in the supplied scope; it did not execute tests or inspect live services. SIGQUIT, child forensic identity and unverified leader completion were subsequently addressed in `328047a`: all outcomes retain `UNKNOWN` containment, and successful leader exits return 2 until whole-workload completion can be verified.
- The focused runtime Opus review at `95f7b08` identified two HIGH limitations before use as a queue gate: the fingerprint omits parts of the effective execution environment, and it is not bound to a particular job. Diagnostic hardening landed in `29b32a4`; the latter remains BLOCKED, as does proof of complete environment identity. Re-review is pending.
- A mismatch in an integration test's expected guard message caused 1/52 failures in both Python versions. The guard itself blocked the run; `1c788a3` aligned the assertion and retained the no-launch/no-record assertions. The integrated `29b32a4` suite subsequently passed 53 tests on both Python 3.10.21 and 3.12.14; logs are `python310-tests.log` and `python312-tests.log`.
- Pueue CPU test also covered SIGKILL of an isolated daemon and a restart reporting `Killed`; this does not prove the child tree is dead or that the researchctl registry can recover. A new standalone daemon-worker fingerprint check passed for `29b32a4`, with the same scope limitation.
- Three focused final Opus requests (ownership, runtime and integrated holds) target the immutable implementation SHA from `29b32a4` and are pending. Review input is exported with `git show`, and no review model is given tools or access to execute code. Results must not be conflated with tests or scientific certification.
- `actions/python-versions` official `versions-manifest.json`, queried via GitHub API, lists Python 3.10.21 and three Linux x64 artifacts. This checks availability, not workflow execution or artifact integrity.

### Integration-review repairs

- Opus integration review at `29b32a4` identified two HIGH-if-integrated Herdr defects: legacy unmarked workspace duplication and unverified cwd semantics. `9bbc4e5` blocks existing checkout collisions and requires an adapter to verify creation-bound cwd; bootstrap remains blocked.
- Opus also found a manifest parse/hash time-of-check race; `04cf77be37dd52fb3b8bf97fc66763e57554f53c` reads, hashes and parses the same bytes. Its test mutates the file after the read and proves the parsed argv remains bound to the hashed bytes.
- The latest integrated candidate `04cf77be37dd52fb3b8bf97fc66763e57554f53c` passes 59 tests on Python 3.10.21 and 3.12.14; logs are preserved. Two focused final Opus re-reviews are pending against this exact SHA.

### Latest independent fallback cycle

- The two focused Opus 5.5 re-review attempts for `04cf77be37dd52fb3b8bf97fc66763e57554f53c` were rejected by the provider's five-hour limit, reset stated as 21:30 Asia/Ho_Chi_Minh. A clean retry for manifest review received the same rejection. Exact requests and sanitized failure records are in `reviews/`; no Opus approval is inferred.
- An independent GPT reviewer checked the manifest and ownership code at that SHA and found no new CRITICAL/HIGH on the reachable local path; ten ownership and seven registry tests passed. See `reviews/fallback-manifest-review.md`.
- Another independent GPT reviewer found a HIGH-if-integrated same-checkout race across different Herdr state paths. The orchestrator independently ran `python3.10 reports/p0/reviews/herdr-duplicate-repro.py` and observed two created mock workspaces. The bootstrap hold prevents live impact. Repair and re-review are in progress; see `reviews/fallback-herdr-review.md`.
- GitHub Actions offline validation passed for PR head `04cf77be37dd52fb3b8bf97fc66763e57554f53c` (`https://github.com/anatwork14/herdr-ml/actions/runs/41454848823/job/125308821740`).

- `5cfd9ed2de6ff362cbb753d112e125b60d2a3880` adds a fixed per-OS-user global coordination lock and capability contract for same-filesystem Herdr clients. Mock concurrency tests pass; cross-user/host/server-side coordination remains unproven and bootstrap stays blocked. Independent fallback re-review of the new commit is pending. Full suite: 63 passed on Python 3.10.21 and 3.12.14.
- Independent GPT fallback re-review of `5cfd9ed2de6ff362cbb753d112e125b60d2a3880` found no new CRITICAL/HIGH within the stated cooperating same-user/shared-filesystem contract. A two-process mock test with distinct state files and project IDs produced one workspace and one blocked caller; the orchestrator reproduced it. See `reviews/fallback-herdr-rereview.md` and `reviews/herdr-concurrency-recheck.py`. The adapter's ability to verify the contract and any cross-host safety remain BLOCKED.

## 2026-11-03 continuation: no-sudo integration

- Continued from PR head `d2daca72d5be50e4de39d39d3172480090c18f2e`; original report remains unchanged. All existing official/queue/bootstrap holds were retained.
- Added a read-only containment preflight and a non-mutating executable administrator handoff. Rechecking the actual current session found no writable delegated v2 cgroup, no `cgroup.kill`, and no reachable user systemd bus. The CPU-only detached-process test establishes that process groups alone are insufficient; the test's initial PID reuse/zombie weaknesses were independently found and repaired with an inherited pidfd and live socket handshake.
- Added point-in-time artifact-tree content inventory with strict input, regular-file pinning and globally bounded traversal/output. Independent reviews found and drove fixes for boolean schema confusion, deep/wide memory exhaustion, FIFO/device opening, duplicate keys, oversized output and growing-file reads. This is diagnostic only; no immutable job binding or approval is claimed.
- Added PID/boot/start/namespace observations to reconciliation; corrupt or unverified records always remain UNKNOWN and no observation grants reclaim. An Opus 5.5 static review at `c1c3d89` found a conditional process-identity issue and relative-cwd/growing-file weaknesses; subsequent fixes were independently checked within their scopes. See `final-integration/retry-opus-review.md`.
- Opus delta review after these repairs was rejected by the provider usage limit, reset stated as 16:50 Asia/Ho_Chi_Minh. The request and error are preserved; Luna reviewers independently checked scoped follow-ups and did not purport to replace Opus approval. A full latest-SHA independent Opus review remains BLOCKED.
- Current implementation `ae2b02eb930e56cb245d6afa93138f32cd50869c`: 105 CPU-only tests pass on Python 3.10.21 and 3.12.14, with logs in `final-integration/`. See `../reviews/2026-11-03-p0-integration.md` for the eight requested statuses and minimal prerequisites.
