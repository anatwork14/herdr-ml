# P0 readiness — PR #2

Implementation under review: `5cfd9ed2de6ff362cbb753d112e125b60d2a3880`. The final PR head may additionally contain evidence/documentation commits; see GitHub for its exact SHA. Original reviewer report SHA-256: `ab8e6853b0c1ddb026580a6423bb43737db2e693753becba4e948ad4dc603d09`.

| Status | Verdict | Evidence / missing prerequisite |
|---|---|---|
| RUN_OWNERSHIP_READY | BLOCKED | Transactional owner/fence and 20-worker CPU tests pass. Safe reclaim, queue task-ID binding and ambiguous-dispatch recovery need verified containment and integrated queue testing. No timeout automatically reclaims ownership. |
| PROCESS_CONTAINMENT_READY | BLOCKED | Same-process-group cancellation tests pass; detached children and SIGKILL remain uncontained. This host exposes a hybrid cgroup setup without a user-delegated writable cgroup v2 subtree and its user systemd bus was unavailable. Provide a properly delegated containment boundary and validate it with isolated CPU processes before any GPU slot release. |
| PROVENANCE_READY | BLOCKED | Real SSH signature and artifact-hash tests pass at a point in time. Actual source, data, splits, environment and evidence are not yet bound immutably to the launched job; supply immutable artifacts and a trusted execution boundary. |
| APPROVAL_READY | BLOCKED | Signed-envelope verification is not execution authorization. Independently administered signer policy, distinct reviewers, approval lifecycle and run binding are not implemented/provisioned. No scientific judgment or dataset/model correctness has been certified. |
| HERDR_BOOTSTRAP_READY | BLOCKED | Crash-aware reconciliation is mock-tested but has no verified real adapter, server-incarnation identity or pane reconciliation. This caller is outside Herdr and the Herdr skill prohibits controlling a session from here. An authorized Herdr-managed pane and isolated test session are required. |
| PYTHON_RUNTIME_READY | BLOCKED | Python 3.10.21 pinned for development/CI; tests pass on 3.10.21 and 3.12.14. A standalone fingerprint probe ran through isolated Pueue, but no handshake binds a fingerprint to a researchctl job, Herdr pane or production daemon. Fingerprints are not tamper-evident or package-content proofs. |
| PUEUE_READY | BLOCKED | Isolated Pueue 4.0.4 serialized two synthetic CPU tasks, retained completed states across clean restart, and reported a test task killed after daemon SIGKILL/restart. No researchctl queue integration, queue-ID binding, crash reconciliation or containment-safe release has been verified. |
| INDEPENDENT_REVIEW_READY | BLOCKED | Opus reviewed earlier immutable candidates, but the provider rejected the latest requests and retry due to a five-hour usage limit. Independent GPT fallback reviews found a Herdr concurrency defect; its repair passed an independent two-process mock retest within a same-user shared-filesystem contract. The final complete integrated implementation has not been reviewed by Opus, and the broader live/approval requirements remain unverified. Reports and limitations are in `reports/p0/reviews/`. |
| SERVER_READY | BLOCKED | No safe containment or authorized live Herdr integration and other required P0 guarantees are complete. PR remains draft and must not merge. |

Official execution, queued execution and automatic bootstrap remain blocked in code. Local diagnostic runs execute arbitrary trusted-user commands and do not enforce CPU-only behavior; without verified containment their overall state is `UNKNOWN`, including after a successful leader exit. Their log hashes are point-in-time observations, not proof that detached writers have stopped.

Reproduce CPU tests from the repository root using a supported interpreter:

```sh
python3.10 -m unittest discover -s tests -v
python3.12 -m unittest discover -s tests -v
bash -n scripts/*.sh
```

The recorded executions used Python 3.10.21 and 3.12.14 and passed 63 tests. See `reports/p0/task-ledger.md` for owners, findings, dependencies and commands; see `reports/p0/pueue-evidence-notes.md` for the isolated queue tests and limitations. No GPU workload was run.
