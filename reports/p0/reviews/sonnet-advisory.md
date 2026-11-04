# P0 advisory review: PR 2 ledger and `scripts/provenance.py`

This is advisory only and is not independent approval. I reviewed the pasted ledger, module and README. I did not run code or tests, and I did not inspect the repo or any Herdr session.

## Verdict
Every hold (H1 to H4, the official-execution hold and the queue hold) stays in place. Nothing in the ledger is evidence for lifting any of them. Its "Verification" column lists planned checks, not results.

## Critical findings

| # | Finding | Why it matters | Required evidence or action |
|---|---|---|---|
| C1 | **Dependency deadlock.** H1 needs H2 and Pueue. H3 needs immutable execution. H4 needs a real Herdr session. Pueue is absent, the cgroup is read-only to the user, the user bus is unavailable and there is no Herdr session. | Further code work cannot close H1 to H4. The blockers are environmental and need operator action. | Make explicit asks of the operator (see Priorities). Do not substitute weaker fallbacks (pgid or `setsid` kills, a spoofed `HERDR_ENV`, mock-only results) and then count them as closing a hold. |
| C2 | **Verification is not bound to execution.** `verify_bundle` is point-in-time. Pueue's socket accepts any command from any process of the same user, so enqueue-time verification authorizes nothing. | An artifact can be swapped between verify and launch. A job can bypass verification entirely. | Verify in the launch wrapper, from an immutable snapshot (content-addressed, read-only, fd-passed or mounted), and run from that snapshot. Hash what is actually read. |
| C3 | **No replay, freshness, revocation or run binding.** The manifest has no nonce, run ID, expiry, host or slot binding. `ssh-keygen -Y verify` is called without a revocation list. | A previously signed manifest can be replayed for a new run, slot or owner. A compromised key cannot be revoked. | Add a signed run ID, validity window and fencing token tied to H1 ownership, plus a revoked-keys file. Keep a consumed-ID ledger. |
| C4 | **`review_evidence` is only a hashed file, signed by the same owner.** | It is self-attestation. Nothing separates the author, the signer and the reviewer. | Require a distinct reviewer principal and signature over the review evidence. Enforce reviewer ≠ author ≠ operator in policy. |
| C5 | **Trust-anchor provisioning is undefined.** The allowed-signers file, the principal and `ssh-keygen` come from the caller's PATH and environment. | An attacker who controls any of these can make verification succeed. | Specify provisioning. Check ownership and mode of the policy file (not group- or world-writable). Use an absolute `ssh-keygen` path and a scrubbed environment. |

## High findings

- **Source is not bound to the claimed Git SHA.** Add a test that the `source` artifact's tree hash equals `source_git_sha`'s tree.
- **Dataset and split semantics are unchecked.** `dataset_manifest` is never cross-checked against `dataset_content`. Identical digests across `train_split`, `validation_split` and `test_split` are not rejected, so identical train and test files would pass. A cheap technical check is to reject any duplicate digests among the roles. Leakage and near-duplicate analysis is scientific review, not code.
- **The review is not bound to a candidate SHA.** `reports/p0/` is untracked, so evidence is not tied to a commit. The previous Opus review timed out, and a retry on a moving HEAD would review a different artifact. Freeze the SHA, commit or hash the reports, and re-request against that SHA.
- **Reviewer independence is unclear.** A review by a model in the authoring pipeline is not independent. This review isn't either.
- **GPU slot release has no test.** H2 acceptance is "CPU process-tree tests" only. Orphaned CUDA contexts and GPU memory are not covered. Add a GPU release test only after the hold review and explicit user authorization.

## Medium and low findings in the verifier

- Python can import from the cwd or `sys.path`, so a planted `json.py` or `hashlib.py` in a bundle directory runs inside the verifier. Require `python -I` and keep bundle directories off `sys.path`.
- `json.loads(bytes)` auto-detects UTF-16 and UTF-32 and accepts `NaN` and `Infinity`. Decode strictly as UTF-8 and pass `parse_constant` to reject non-finite constants.
- Unknown top-level keys are accepted. Use an exact allowed set so nothing signed gets silently ignored.
- `cwd`, environment variables, interpreter fingerprint, device/slot and output paths are not bound. `argv[0]` may be PATH-resolved.
- The change-detection check relies on mtime and ctime, which are weak. Compare bytes hashed to `st_size`, and prefer immutability (a snapshot or read-only mount) over detection.
- Add `O_NOCTTY` when opening artifacts.
- The manifest is opened without `O_NOFOLLOW`, and `manifest_path.parent` is taken from the unresolved path.
- The return value is only the parsed dict. Return an immutable result with the manifest SHA-256, signer key fingerprint, principal and verified digests. Avoid names or types that read as authorization.
- The ledger has no commit/test-output links per row, no abort or rollback plan and no owner dates. The base `d4ff2ac` is behind HEAD `eff54bb`, so check the merge-base against main `18ca3ef`.

## Missing acceptance tests

**Verifier (offline)**
- Wrong namespace, including a valid signature made for a different namespace.
- Wrong principal, wrong key and a one-byte manifest tamper.
- Truncated or garbage signature.
- Policy with `valid-before` or an expired key.
- A revoked key.
- Nested duplicate JSON keys.
- `seed=true`.
- UTF-16 manifest.
- `NaN`.
- Extra top-level key.
- Oversized manifest.
- Path cases: `..`, `//`, backslash, NUL, trailing `/`, absolute path.
- Symlink at the final component and at an intermediate component.
- FIFO and device files.
- Hardlink aliasing.
- A file mutated during hashing (a threaded race).
- `ssh-keygen` missing, and timeout.
- Identical digests across the three split roles.

**Negative gate (the most important test)**
- A fully valid signed bundle must still be refused by the official-execution path while the hold is in place. CI should fail if that path ever launches.

**Execution binding (future)**
- Swap an artifact after verify and before launch: the launch must fail.
- Replay a consumed run ID.
- A stale fencing token is rejected.
- A job enqueued directly in Pueue without a manifest is refused by the wrapper.

**H1, H2, H4 and the interpreter**
- Crash at every ownership boundary, with idempotent recovery.
- A process-tree kill that reaches double-forked and `setsid` children, verified from the cgroup.
- Herdr reconcile after a crash, with idempotence and a nonce-based server identity check, first mocked and then on an isolated real Herdr.
- A daemon handshake that rejects an interpreter fingerprint mismatch.

## Technical authorization vs scientific certification

- **Technical authorization** is the most this code can ever assert. It means a key in operator-controlled policy signed these exact bytes, and the named files hashed to the declared digests at one moment. A future execution layer would additionally bind it to an actual run.
- **Scientific certification** means the protocol, splits, leakage controls, baseline and results are valid. No code can establish this. It requires a human reviewer with a separate identity and a recorded review.
- Use distinct status names (for example `signature_verified`, `artifacts_hash_matched`, `execution_bound`). Code should never set a `certified` or `approved` value. That should come only from a signed human review record.

## Priorities

1. **Keep all holds** and add the negative-gate CI test.
2. **Ask the operator for the environment.** Pueue and `pueued` need installing in an isolated location. H2 needs a delegated cgroup subtree or a working user systemd bus (`loginctl enable-linger`, a valid `XDG_RUNTIME_DIR`). With hybrid cgroups on kernel 5.4 this may need cgroup v2 at boot or a root-owned helper; confirm with the admin. The Herdr session must be launched by the human owner.
3. **Freeze the candidate SHA**, commit or hash `reports/p0/`, and obtain a genuinely independent review against that SHA.
4. **Write the execution-binding design** (snapshot, launch-time verify, run ID, fencing token) before more verifier features. Fix C3 to C5 as part of it.
5. **Harden the verifier** with the medium findings and land the offline test matrix.
6. **Separate the scientific review track.** Use a named reviewer and checklist (protocol, leakage, baseline). Do not let engineering sign-off stand in for it.

Lifting any hold would first need the corresponding evidence: test outputs bound to the exact SHA, real (not mocked) containment, queue and Herdr results, and the independent review report. Those do not exist yet.
