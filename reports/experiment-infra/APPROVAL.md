# W4 run-specific execution approval

Status: **OFFLINE_TESTED** for the verifier (`scripts/approval.py`) using test-only trust anchors. **APPROVAL_READY is BLOCKED.** No independently controlled trust anchor exists in this repository, and this worktree cannot provision one. Nothing here authorizes official execution, and `scripts/researchctl.py` does not consume these approvals.

## Model

An approval authorizes exactly one attempt of one manifest on one worker. It is checked at the execution boundary (the launcher), not only at submission. The approval binds:

| Field | Meaning |
|---|---|
| `approval_id` | Single-use identifier; the consumption claim is keyed by it |
| `anchor_id` | Trust anchor that must match the anchor loaded by the verifier |
| `run_id`, `attempt_id` | Exact run and attempt. An approval for attempt 1 is rejected for attempt 2 |
| `manifest_sha256` | Approved manifest digest (see `PROVENANCE.md`) |
| `resource_class` | Authorized resource class; must equal the manifest's |
| `scope` | `{project, operation}`; must equal the manifest's |
| `output_destination_sha256` | SHA-256 of the canonical authorized `output` object |
| `issued_at`, `expires_at` | UTC timestamps (`YYYY-MM-DDTHH:MM:SSZ`), window at most 24 hours |
| `signature` | `{algorithm, signer_id, value}`; the MAC covers the canonical body without `signature` and a domain tag `herdr-ml-approval-v1\n` |

Canonical serialization and strict parsing are the same as for manifests. Approval bytes must be canonical.

## Principals and trust anchor

`load_trust_anchor(anchor_dir, repo_root, run_dir, test_only)` reads `anchor.json` (canonical, `kind: herdr-ml-trust-anchor`, `algorithm: HMAC-SHA256`) with:

- `anchor_id`: label for this authority.
- `workers`: principals that may consume approvals.
- `signers`: map from signer id to a key file in the same directory.
- `revoker`: a listed signer that signs revocation lists.

No principal may be both a worker and a signer. The loader refuses that combination, and the verifier refuses a self-issued approval.

Key material: one 32-byte or longer key file per signer, mode `0600` (no group or other bits), not a symlink, not owned by the verifying uid, and kept outside the repository, outside every run directory and outside any writable location of a worker.

## Trust-anchor requirement (to be provisioned by an operator)

Required before APPROVAL_READY can be considered:

1. **Location.** An absolute directory provisioned by an operator outside this repository, outside every run directory, and outside any path the worker uid can write. The module default is `/etc/herdr-ml/trust/execution-approval`, which is a constant. There is no environment override, so a worker cannot redirect verification.
2. **Ownership.** Provisioned by a trust-administration uid that is not the worker uid. Every ancestor directory is owned by a uid other than the verifying uid and has no group or other write bit. The production loader enforces this (`TRUST_ANCHOR_INVALID` on failure).
3. **Separation.** The verifier runs in the trusted launcher. The worker process runs under a uid that cannot read the anchor directory or key files, is not root, and lacks `CAP_DAC_OVERRIDE`. The loader cannot prove the last condition from inside the process. It is an operating-environment requirement.
4. **State.** The consumption directory and the revocation list are operated by the launcher or trust administrator. The worker cannot create, delete or edit consumption claims or revocation lists.
5. **Test isolation.** `test_only=True` exists only for the test suite. It requires the `TEST-ONLY-` label, a directory under the system temp directory, and skips ownership checks. Production code must never pass `test_only=True`. The label check rejects a production-labelled anchor loaded in test mode, and the reverse.

## Verification sequence

`verify_approval(approval_bytes, anchor, revocation_bytes, expected, store, now)` runs in this order. The first failure raises its code:

1. Strict parse, canonical bytes, schema, validity window (`APPROVAL_MALFORMED`, `APPROVAL_NONCANONICAL`).
2. `anchor_id` equals the anchor (`APPROVAL_UNKNOWN_ANCHOR`).
3. Signer equals `expected.worker_id` (`APPROVAL_SELF_ISSUED`). This check runs before signature verification, so a self-issued approval is rejected even when its MAC is valid.
4. Signer is provisioned (`APPROVAL_SIGNER_UNAUTHORIZED`). `expected.worker_id` is an authorized worker (`APPROVAL_WORKER_UNAUTHORIZED`).
5. Signature over the domain-separated canonical body (`APPROVAL_SIGNATURE_INVALID`).
6. `now` is within `[issued_at, expires_at)` (`APPROVAL_NOT_YET_VALID`, `APPROVAL_EXPIRED`).
7. Revocation list is present (`REVOCATION_LIST_MISSING`), canonical, signed by the revoker over the domain-separated body (`REVOCATION_MALFORMED`, `REVOCATION_UNAUTHENTICATED`), and does not list the approval id or the manifest digest (`APPROVAL_REVOKED`). A future-dated list is refused.
8. Run, attempt, manifest digest, resource class, scope and output destination equal the expected values (`APPROVAL_RUN_MISMATCH`, `APPROVAL_ATTEMPT_MISMATCH`, `APPROVAL_MANIFEST_MISMATCH`, `APPROVAL_RESOURCE_MISMATCH`, `APPROVAL_SCOPE_MISMATCH`, `APPROVAL_DESTINATION_MISMATCH`).
9. Consumption claim: `O_CREAT|O_EXCL` creation of `<approval_id>.consumed` in the consumption store (`APPROVAL_CONSUMED`). This is the last step, so a failed check never burns an approval.

On success the function returns an authorization record: approval digest, approval id, anchor, signer, worker, run, attempt and manifest digest.

## Revocation semantics

A revocation list is signed by the revoker and lists approval ids and manifest digests. It takes effect for any approval that names one of them, regardless of its validity window. A missing, unauthenticated, malformed, or future-dated list fails closed. Revocations are not monotonic in this version: there is no sequence number and no freshness bound. An older, validly signed list presented in place of a newer one would fail to revoke an approval that the newer list revoked, and the verifier cannot detect that. A sequence number and a minimum-sequence store are needed before revocation can be relied upon.

## Signature scheme and its limits

The stdlib provides HMAC-SHA256 only. The verifier interface is `SignatureVerifier` with `has_signer` and `verify`. `HmacSha256Verifier` holds signer keys. `UnprovisionedAsymmetricVerifier` is a placeholder that fails closed with `TRUST_ANCHOR_UNPROVISIONED`. An external, audited Ed25519 (or similar) verifier that holds only public keys can replace the HMAC implementation without changing the sequence above.

Limits of HMAC:

- The verifier holds the signer keys. It can sign anything. It must therefore run only in the trusted launcher, and its key files must not be readable by the worker.
- A valid MAC shows that the holder of the signer's key produced the approval. It does not show independent review or scientific adequacy.
- The worker cannot verify an approval itself. The handoff from launcher to worker (for example, passing the manifest digest that was authorized) needs an authenticated channel that is not yet defined.

## Gaps that remain open

- No provisioned anchor. APPROVAL_READY stays BLOCKED until an operator provisions a production anchor, the worker identity separation is confirmed in the environment, and an external verifier or equivalent review is accepted.
- Revocation freshness and sequencing are not defined (see above).
- Consumption durability relies on the filesystem and the launcher. The claim is written before execution, but a crash between claim and execution burns the approval, which fails closed.
- The worker-to-launcher handoff is undefined.
- Approval authorizes dispatch. It does not restrict what the process reads or writes; that depends on the execution boundary (W1 ownership fencing and W2 containment).
- Keys are not rotated, and no key-revocation path exists besides removing a signer from the anchor.

## Integration (deferred)

`scripts/researchctl.py` has no call to this module. The intended future point is `execute()`, after the existing gate and claim checks and before dispatch, with the consumption store and revocation list supplied by the launcher. Official execution must remain blocked by `require_supported_execution` until that integration is reviewed and a trust anchor is provisioned.

## Tests

`tests/test_approval.py` (38 tests): binding failures (attempt replay with non-consumption on failure, run, resource, scope, output destination, manifest digest, unknown anchor, unauthorized worker, unprovisioned signer); signature tampering and key mismatch; self-issued approval with a valid MAC; malformed, truncated and noncanonical approvals; expiry, not-yet-valid and overlong windows; revoked approval, revoked manifest, missing, forged and tampered revocation lists; single-use consumption and store placement; anchor provisioning (unprovisioned, relative, production versus test mode, label, location, key permissions, short key, key symlink, worker-owned key, writable or worker-owned ancestors, worker-also-signer, revoker not listed, asymmetric placeholder).
