# W4 run-specific execution approval

Status: **OFFLINE_TESTED** for the verifier (`scripts/approval.py`) using test-only trust anchors. **APPROVAL_READY is BLOCKED.** No independently controlled trust anchor exists in this repository, and this worktree cannot provision one. Nothing here authorizes official execution, and `scripts/researchctl.py` does not call this module.

## Model

An approval authorizes one manifest digest for one worker, one run, one attempt, one resource class, one scope and one output destination, inside a validity window, unless revoked, and only once. It is checked at the execution boundary (the launcher), not only at submission. The approval binds:

| Field | Meaning |
|---|---|
| `approval_id` | Identifier; the consumption claim is keyed by it with `run_id` and `attempt_id` |
| `anchor_id` | Must equal the anchor loaded by the verifier |
| `worker_id` | The worker the approval names. The verifier requires it to equal the worker about to run (`APPROVAL_WORKER_MISMATCH` otherwise) |
| `run_id`, `attempt_id` | Exact run and attempt. An approval for attempt 1 is rejected for attempt 2 |
| `manifest_sha256` | Approved manifest digest (see `PROVENANCE.md`) |
| `resource_class` | Must equal the manifest's |
| `scope` | `{project, operation}`; must equal the manifest's |
| `output_destination_sha256` | SHA-256 of the canonical authorized `output` object |
| `issued_at`, `expires_at` | UTC timestamps (`YYYY-MM-DDTHH:MM:SSZ`); window at most 24 hours |
| `signature` | `{algorithm, signer_id, value}`; the MAC covers the canonical body without `signature`, with domain tag `herdr-ml-approval-v1\n` |

Approval bytes must be canonical. Parsing uses the same strict rules as manifests (no floats, duplicate keys, NaN or Infinity; surrogate code points rejected).

The anchor's `workers` list is an allowlist of principals that may consume approvals. It does not authorize a particular approval. The `worker_id` field in the signed body does that.

## Principals and trust anchor

`load_trust_anchor(anchor_dir, *, repo_root, run_dir, test_only, admin_uids, worker_uids)` reads `anchor.json` (canonical, `kind: herdr-ml-trust-anchor`, `algorithm: HMAC-SHA256`) containing:

- `anchor_id`: label for this authority.
- `workers`: allowlist of worker principals.
- `signers`: map from signer id to a key file in the same directory.
- `revoker`: a listed signer that signs revocation lists.

No principal may be both a worker and a signer; the loader refuses that combination.

## Trust-anchor requirement (to be provisioned by an operator)

1. **Location.** An absolute directory outside this repository, outside every run directory, and outside any path the worker uid can write. Path components must not be symlinks (`TRUST_ANCHOR_INVALID`). The module default is `/etc/herdr-ml/trust/execution-approval`, a constant. Nothing reads an environment override.
2. **Ownership, checked by the production loader.** Every directory from the anchor up to `/`, `anchor.json`, and every key file must be owned by a uid in the caller's `admin_uids` allowlist, and by no uid in `worker_uids`. The verifier's own uid is not special. Directories must not be group- or other-writable. Files (`anchor.json`, key files, state and claim files) must have no group or other permission bits (`0600`). Keys must be at least 32 bytes.
3. **Allowlists are inputs.** Production mode requires explicit, disjoint `admin_uids` and `worker_uids`. Omitting either is refused.
4. **Separation.** The verifier runs in the trusted launcher. The worker runs under a uid that cannot read the anchor directory or key files, is not in `admin_uids`, and lacks `CAP_DAC_OVERRIDE`. The loader cannot verify that last condition from inside the process; it is an operating-environment requirement.
5. **State.** The consumption directory and the revocation sequence state are owned by admin uids and not writable by workers. Provisioning the revocation sequence (`provision_revocation_state`) is an operator step; a missing state file fails closed (`REVOCATION_STATE_MISSING`).
6. **Test isolation.** `test_only=True` is for the test suite only. It requires the `TEST-ONLY-` label and a directory under the system temp directory, and it skips ownership checks (mode checks still apply). Production must never pass `test_only=True`.

### What this host can and cannot show

Production ownership mode needs administrative ownership of `/etc/herdr-ml` and its ancestors. This worktree is not root, and the brief forbids touching `/etc`. The production ownership rule is therefore tested only with patched `os.lstat` metadata: a worker-owned `0755` directory is refused, a root-owned directory passes when uid 0 is in `admin_uids`. For the default path with uid 0 as owner, every directory check passes and the refusal is the `anchor.json` file-type check (`must be a regular file`), because the simulated entry is a directory. Nothing is accepted on that path. With uid 1000 as owner, the default path is refused with an owner reason. No test ran against a real root-owned `/etc` tree. Production mode on this host has not been exercised end to end, and the default path has not been provisioned. APPROVAL_READY stays BLOCKED for that reason.

## Verification sequence

`verify_approval(approval_bytes, anchor, revocation_bytes, revocation_state, expected, store, now)` runs in this order and raises the first failure's code:

1. Strict parse, canonical bytes, schema, identifiers, digests, scope and validity window (`APPROVAL_MALFORMED`, `APPROVAL_NONCANONICAL`). Integers outside the safe range are `APPROVAL_MALFORMED`.
2. `anchor_id` equals the anchor (`APPROVAL_UNKNOWN_ANCHOR`).
3. Signer equals `expected.worker_id` (`APPROVAL_SELF_ISSUED`). This runs before signature verification, so a self-issued approval is rejected even with a valid MAC.
4. Signer is provisioned (`APPROVAL_SIGNER_UNAUTHORIZED`). `expected.worker_id` is on the workers allowlist (`APPROVAL_WORKER_UNAUTHORIZED`).
5. Signature over the domain-separated canonical body (`APPROVAL_SIGNATURE_INVALID`). Non-hex or non-ASCII signature values fail with this code.
6. The signed `worker_id` equals `expected.worker_id` (`APPROVAL_WORKER_MISMATCH`).
7. `now` is within `[issued_at, expires_at)` (`APPROVAL_NOT_YET_VALID`, `APPROVAL_EXPIRED`).
8. The revocation list is present (`REVOCATION_LIST_MISSING`), canonical (`REVOCATION_MALFORMED`), signed by the revoker (`REVOCATION_UNAUTHENTICATED`), and has a sequence no lower than the recorded one (`REVOCATION_ROLLBACK`). Only then is the recorded sequence advanced. The approval id or manifest digest must not be listed (`APPROVAL_REVOKED`).
9. Run, attempt, manifest digest, resource class, scope and output destination equal the expected values (`APPROVAL_RUN_MISMATCH`, `APPROVAL_ATTEMPT_MISMATCH`, `APPROVAL_MANIFEST_MISMATCH`, `APPROVAL_RESOURCE_MISMATCH`, `APPROVAL_SCOPE_MISMATCH`, `APPROVAL_DESTINATION_MISMATCH`).
10. Consumption, the last step (see below).

A failed check at steps 1 to 9 does not consume the approval. Step 8 can advance the sequence even if a later check fails. That is intended: the list was authentic.

## Consumption store

`ConsumptionStore(directory, ...)` must be an admin-owned, non-group-writable directory that is not inside the repository or a run directory. A missing directory is `CONSUMPTION_STORE_INVALID`, not a raw exception. A claim is two files created with `O_EXCL`, mode `0600`, and fsynced, followed by an fsync of the directory:

- approval claim keyed by `(approval_id, run_id, attempt_id)`: a second use of the same approval returns `APPROVAL_CONSUMED`;
- attempt claim keyed by `(run_id, attempt_id)`: a second approval for the same attempt returns `APPROVAL_ATTEMPT_ALREADY_CLAIMED`.

If a claim file already exists, it is checked for permissions and ownership first. A claim with open permissions or a non-admin owner returns `CONSUMPTION_STORE_INVALID`. Known gap: a crash between the approval claim and the attempt claim leaves the approval burned but the attempt unclaimed, so a fresh approval for the same attempt could pass. An operator must review leftover approval claims that have no matching attempt claim. Writing both claims in one atomic step is not implemented.

## Revocation semantics

A revocation list is signed by the revoker and carries a `sequence` integer. The verifier records the highest accepted sequence in an admin-owned state file, `revocation-sequence.json`. It refuses any list whose sequence is lower than the recorded one (`REVOCATION_ROLLBACK`). An unauthenticated list does not advance the recorded sequence. A missing state file fails closed (`REVOCATION_STATE_MISSING`). A state file recorded for a different anchor is refused first, before the list is parsed (`REVOCATION_STATE_ANCHOR_MISMATCH`).

Limits: the sequence check stops an older list from masking a newer one on this launcher. It does not stop an attacker who can roll back or replace the state file, which is why the state file must be admin-owned. Multiple launchers sharing one state file need external locking, which is not implemented. A future-dated list is refused. The verifier does not check wall-clock freshness of the list.

## Signature scheme and its limits

The stdlib provides HMAC-SHA256 only. The verifier interface is `SignatureVerifier` (`has_signer`, `verify`). `HmacSha256Verifier` holds signer keys. `UnprovisionedAsymmetricVerifier` is a placeholder that fails closed with `TRUST_ANCHOR_UNPROVISIONED`. An external, audited Ed25519 (or similar) verifier that holds only public keys can replace the HMAC implementation without changing the verification sequence.

Limits of HMAC:

- The verifier holds the signer keys, so it can sign anything. It must run only in the trusted launcher, and its key files must not be readable by the worker.
- A valid MAC shows that the holder of the signer's key produced the approval. It is not independent review or scientific adequacy.
- The worker cannot verify an approval itself. The handoff from launcher to worker (passing the authorized manifest digest) needs an authenticated channel that is not yet defined.

## Gaps that remain open

- No provisioned production anchor, so APPROVAL_READY stays BLOCKED.
- Production ownership checks are tested only with patched metadata on this host (see above).
- Multi-launcher state sharing and locking are not implemented.
- Crash between the two consumption claims (see Consumption store) can leave an attempt unclaimed.
- The worker-to-launcher handoff is undefined.
- Approval authorizes dispatch. It does not restrict what the process reads or writes; that depends on the execution boundary (W1 ownership fencing and W2 containment).
- Keys are not rotated, and there is no key-revocation path beyond removing a signer from the anchor.

## Integration (deferred)

`scripts/researchctl.py` has no call to this module. The intended future point is `execute()`, after the existing gate and claim checks and before dispatch, with the consumption store and revocation state supplied by the launcher. Official execution remains blocked by `require_supported_execution` until that integration is reviewed and a trust anchor is provisioned.

## Tests

`tests/test_approval.py` (54 tests). Binding: valid single use; attempt replay with no consumption on failure; approval for worker-1 refused for worker-2 (`APPROVAL_WORKER_MISMATCH`); second approval for the same attempt refused; expiry, not-yet-valid, run, resource, scope, output destination, manifest digest, unauthorized worker, unprovisioned signer, unknown anchor; non-string signer and out-of-range integers. Signatures: tampering, malformed and non-ASCII signature values, wrong key, self-issued approval with a valid MAC, truncation and non-canonical bytes, overlong window. Revocation: revoked approval and manifest, missing, forged and tampered lists, malformed list; older list cannot mask a newer one; rollback check before other use; unauthenticated list does not advance the sequence; missing state fails closed; state directory permissions. Consumption: missing directory reported as `CONSUMPTION_STORE_INVALID`; open directory mode refused; claims `0600` and fsynced (file and directory); planted claim with open mode refused; store inside repository or run directory refused. Anchor provisioning: unprovisioned, relative, production versus test mode, label, locations, key permissions, short key, key symlink, `anchor.json` with mode `0666` refused (and `0600` accepted), symlink in a parent directory, asymmetric placeholder. Ownership: worker-owned `0755` directory refused; key owned by a worker uid refused; explicit disjoint allowlists required; root-owned directory passes only when root is an admin uid; the production default path refuses a worker-owned tree with an owner reason and a root-owned tree with `must be a regular file` (patched metadata, exact reason asserted).
