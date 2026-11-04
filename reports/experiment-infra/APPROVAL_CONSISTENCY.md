# Approval consumption consistency and launch handoff (P7.3)

Status: **OFFLINE_TESTED**. Code: `scripts/approval.py`. Tests: `tests/test_approval_consistency.py` (36 tests, each with a 60 second timeout) and the extended `tests/test_approval.py`. **APPROVAL_READY stays BLOCKED.** No production trust anchor, handoff verifier or key exists, and this work does not certify itself.

## 1. The interface

```
consume_for_attempt(run_id, attempt_id, approval, manifest_digest, now, *, ctx, _fault=None) -> record
```

- `approval`: canonical approval bytes. `manifest_digest`: the approved manifest SHA-256. `now`: timezone-aware UTC time at the boundary.
- `ctx`: `AttemptContext(store, anchor, revocation_state, revocations, worker_id, resource_class, scope, output)`, built by the trusted launcher. `revocations` is a callable returning the current revocation list bytes, so the boundary sees revocations issued during authorization.
- Returns the grant record on success. Otherwise raises `VerificationError` with one reason code. A refusal writes no claim. The only write that can happen first is a revocation-sequence advance, which is monotone.
- W1 calls it last, after all of its own checks pass.

The function re-runs every approval check at `now` (signature, worker, validity window, revocation, run, attempt, manifest, resource, scope, destination), then records the consumption. `verify_approval` uses the same `_authorize` path, so existing callers keep their behavior.

## 2. Protocol states

Two claim files per consumption, both `0600`, created with `O_EXCL`, fsynced, with a directory fsync after each.

| File | Key | States |
|---|---|---|
| Attempt claim | `(run_id, attempt_id)` | `consumed_not_launched` -> `launched`; `consumed_not_launched` -> `fenced` (recovery only) |
| Approval claim | `(approval_id, run_id, attempt_id)` | `consumed` (written once, never changed) |

- `consumed_not_launched` means the approval is consumed for this attempt and the launch is not yet recorded. Consume refuses it for reuse (`APPROVAL_ATTEMPT_UNLAUNCHED`). Only `fence_unlaunched()` moves it on.
- `fenced` is terminal. Consume refuses it (`APPROVAL_ATTEMPT_FENCED`). A new attempt needs a new approval and a new `attempt_id`.
- `launched` is terminal. It cannot be launched again or fenced.
- Attempt-state changes are atomic: a temp file is staged, fsynced, renamed over the claim, and the directory fsynced. A crash leaves the old state or the new one, never a mix.

**Write order.** The attempt claim is written first, then the approval claim, then the launch is recorded. The original gap was the reverse order: a crash after the approval claim left an approval burned with no attempt claim, so a different approval could later claim the attempt. Now a crash after the attempt claim blocks every other approval for that attempt, and the leftover is fenced, not reused.

**Pre-checks.** Before any write, `claim` reads both files. A malformed, unsafe or mis-keyed claim refuses (`CONSUMPTION_STORE_INVALID`) and nothing is written. Pre-checks are not authoritative on their own. The `O_EXCL` create decides races.

## 3. Crash table

Named fault points (`CRASH_POINTS`). A hook raises at the named point to simulate process death. A separate test uses `os._exit` after `claim:after_attempt_claim` in a forked child to check durability across processes.

| Crash point | Persistent state at the crash | Same approval, next consume | Different approval, next consume | After recovery fence |
|---|---|---|---|---|
| `claim:before_attempt_claim` | nothing | consumes normally | consumes normally | n/a |
| `claim:attempt_opened` | attempt file empty | `CONSUMPTION_STORE_INVALID` | `CONSUMPTION_STORE_INVALID` | manual review: the file is unparseable |
| `claim:after_attempt_claim` | attempt `consumed_not_launched`, no approval claim | `APPROVAL_ATTEMPT_UNLAUNCHED` | `APPROVAL_ATTEMPT_ALREADY_CLAIMED` | same approval `APPROVAL_ATTEMPT_FENCED`; other `APPROVAL_ATTEMPT_ALREADY_CLAIMED` |
| `claim:approval_opened` | attempt good, approval file empty | `CONSUMPTION_STORE_INVALID` | `APPROVAL_ATTEMPT_ALREADY_CLAIMED` | manual review |
| `claim:after_approval_claim` | both claims, `consumed_not_launched` | `APPROVAL_CONSUMED` | `APPROVAL_ATTEMPT_ALREADY_CLAIMED` | `APPROVAL_CONSUMED` (approval claim exists) |
| `launch:before_replace` | `consumed_not_launched`; orphan temp file, ignored | `APPROVAL_CONSUMED` | `APPROVAL_ATTEMPT_ALREADY_CLAIMED` | fence works, then `APPROVAL_CONSUMED` |
| `launch:after_replace` | `launched` | `APPROVAL_CONSUMED`; `mark_launched` refused | refused | fence refused (`launched`) |
| `fence:before_replace` | `consumed_not_launched`; fence not applied | `APPROVAL_CONSUMED` | refused | re-running fence applies it |
| `fence:after_replace` | `fenced` | `APPROVAL_CONSUMED` | refused | re-running fence is a no-op |

Empty or corrupt claims fail closed. They are not re-used and not repaired automatically. Operator review is required for them.

The revocation-sequence advance is a separate persistent write. It can land before any crash point above. It is monotone, so a repeat is harmless.

## 4. Concurrency

Six forked processes race for one attempt with six distinct approvals, and six with one approval. Exactly one returns success. The others refuse with `APPROVAL_ATTEMPT_ALREADY_CLAIMED`, or fail closed with `CONSUMPTION_STORE_INVALID` if they read the winner's file before its bytes are written. No child reports an unexpected exception.

## 5. Expiry, revocation, corruption, permissions

- Expiry: `now >= expires_at` refuses with `APPROVAL_EXPIRED` and writes nothing. One second before expiry still succeeds.
- Revocation during authorization: a list issued after the launcher started, and before the boundary, refuses with `APPROVAL_REVOKED`. A revoked manifest digest refuses the same way. No claim is written.
- Corruption: a truncated claim, a claim with an unknown state, a claim with the wrong kind, and a claim renamed to another key all refuse, and the store bytes are unchanged afterwards.
- Permissions: a group-readable claim refuses, and a store directory with mode `0770` or `0777` refuses at open. The bytes are unchanged.

## 6. Launch handoff format

Canonical JSON (sorted keys, no whitespace, ASCII). Signature covers `DOMAIN_HANDOFF` (`herdr-ml-launch-handoff-v1\n`) followed by the canonical body without `signature`.

```
{
  "schema_version": 1,
  "kind": "herdr-ml-launch-handoff",
  "anchor_id": "<id>",
  "run_id": "<id>", "attempt_id": "<id>", "worker_id": "<id>",
  "manifest_sha256": "<64 hex>",
  "consumption": {"approval_sha256": "<64 hex>", "grant_sha256": "<64 hex>"},
  "issued_at": "YYYY-MM-DDTHH:MM:SSZ", "expires_at": "YYYY-MM-DDTHH:MM:SSZ",   (window at most 24 h)
  "signature": {"algorithm": "Ed25519", "signer_id": "<key id>", "value": "<hex>"}
}
```

`grant_sha256` is the SHA-256 of the canonical grant record the store wrote at consumption. `handoff_body()` builds the unsigned body for the launcher's signing service.

`verify_handoff(bytes, verifier, *, store, now, expected)` runs in this order:
1. The verifier is an asymmetric `HandoffVerifier` (`HANDOFF_VERIFIER_UNPROVISIONED` otherwise).
2. Strict parse, canonical bytes, keys, schema, identifiers, digests, window (`HANDOFF_MALFORMED`, `HANDOFF_NONCANONICAL`).
3. Algorithm equals the verifier's (`HANDOFF_SIGNATURE_INVALID`).
4. Signature verifies over the domain and canonical body (`HANDOFF_SIGNATURE_INVALID`).
5. `anchor_id`, `worker_id`, `run_id`, `attempt_id`, `manifest_sha256` equal the expected values (`HANDOFF_BINDING_MISMATCH`).
6. `now` is within the window (`HANDOFF_NOT_YET_VALID`, `HANDOFF_EXPIRED`).
7. The store's current attempt claim exists (`HANDOFF_NOT_CONSUMED`), is not fenced (`HANDOFF_FENCED`), is `consumed_not_launched` (`HANDOFF_ALREADY_LAUNCHED`), and its approval and grant digests match (`HANDOFF_CONSUMPTION_MISMATCH`).

The default verifier is `UnprovisionedHandoffVerifier`. It refuses every handoff. `select_handoff_verifier(test_only=False, injected=...)` refuses a test-only verifier in production mode (`HANDOFF_VERIFIER_NOT_SELECTABLE`). The HMAC verifier is not a `HandoffVerifier`, so it cannot authorize a handoff.

## 7. What the verifier can and cannot prove

Can prove, given a valid signature from the handoff key and the admin-owned store:
- the attempt was consumed for exactly this run, attempt, worker, manifest and approval;
- the consumption has not been fenced or launched at the check time;
- the handoff has not expired, and the bytes are the canonical bytes that were signed.

Cannot prove:
- that the launcher or its signing service behaved honestly. A signature shows who signed, not that the request was right;
- that an administrator did not rewrite the store. Ownership and mode checks make this harder but do not rule it out;
- that a job did not start after a crash. The rule is conservative: an unrecorded launch is fenced, so the attempt is never re-used. Whether the process ran is unknown;
- freshness beyond `expires_at`, or the identity of the caller that reads the handoff;
- anything at all about handoff authenticity in this build. No asymmetric verifier is provisioned, so every handoff is refused.

The worker must be able to read the store (to call `read_attempt`) but must not be able to write it, and must not hold any signing or verification key. That is an operating-environment requirement and is not enforced by this code.

## 8. The asymmetric requirement

- The handoff is signed by a key held only by the launcher's signing service (or hardware token), never by the worker. The worker has no signing key in its uid, environment, repository or run directory.
- The verifier holds only public keys. They come from an admin-owned anchor, administered independently of the signer. Under `ownership` rules, the worker uid cannot write it.
- The algorithm is Ed25519 (or an equivalent asymmetric scheme) through an external, audited implementation. The stdlib has no Ed25519.
- Shared HMAC material is not independent authorization. A worker or launcher holding the same secret can forge handoffs. This code refuses HMAC for handoffs by type and by algorithm label.
- Environment variables are never a source of keys, trust anchors or authorization. The module reads none.
- Test-only verifiers carry a `TEST-ONLY-` label and only accept a fixed test value. Production mode refuses them.

### openssl check (this host)

`/usr/bin/openssl` is present at an absolute path, owned by root (OpenSSL 1.1.1f). `openssl genpkey -algorithm ed25519` works, but `openssl pkeyutl -sign` and `-verify` fail with "operation not supported for this keytype", and `-rawin` is not an option in this build. Shelling out to openssl would also add a new trusted executable to the verifier. Neither route is usable here, so the implementation stays fail-closed. An operator can later provision an external Ed25519 verifier that implements `HandoffVerifier` and holds public keys only.

## 9. Open items

- No production anchor, handoff verifier or signing key is provisioned. APPROVAL_READY stays BLOCKED.
- Empty or corrupt claims need an operator procedure (review, then fence or quarantine). This code does not repair them.
- Orphan `.tmp` state files from a crash at `launch:before_replace` or `fence:before_replace` are left in place. They are ignored by the store and can be removed by an operator.
- Integration into `scripts/researchctl.py` is not done. The launcher-side call site is `consume_for_attempt`, after the W1 checks.
- The worker-side reader of `verify_handoff` and the real signing service are not implemented.
