# Follow-up review: commit `7c1450f149eb` (`scripts/provenance.py`, tests, docs)

**Scope and method.** This is a static review of the source, tests and docs as pasted. I ran read-only `git show` and `git grep` commands to cross-check the pasted files against the commit, but their output didn't reach me. So I can't confirm the pasted text is byte-identical to the blobs at `7c1450f`, and I haven't checked callers of `verify_bundle` elsewhere in the tree. **No tests were run.** Every "would pass / would fail" claim below comes from reading the code.

**Bottom line:** I found no CRITICAL or HIGH defects and no remaining actionable MEDIUM issues. All three prior MEDIUM findings are fixed, and their tests now fail for the right reason. LOW items L2 is fixed; L1, L3, L4, L5 and L6 remain open, and I found a few new LOW items. Nothing here should lift the holds on official execution, queued execution or bootstrap.

## Status of prior findings

| ID | Status | Evidence |
|---|---|---|
| M1 symlink test | **Fixed** | `test_symlink_escape_and_missing_required_role_rejected` now writes identical bytes to `outside`, so following the link would produce a matching hash. It asserts `'cannot safely read artifact'`. |
| M1 intermediate symlink | **Fixed** | New `test_intermediate_symlink_rejected_even_with_correct_target`. The `alias/split` content matches the signed hash, so the test fails only if `O_NOFOLLOW` on directory components is removed. |
| M1 `..` traversal | **Fixed** | `outside` has identical bytes, and the test asserts the `'relative and normalized'` message. If the normalization check were removed, the hash would match and verification would succeed, so the test would fail. |
| M1 duplicate keys | **Fixed** | `'{"project":"demo",' + canonical[1:]` leaves an otherwise-valid manifest under last-wins parsing. The test asserts `'duplicate JSON key'`. |
| M2 namespace pinning | **Fixed** | `test_wrong_namespace_signature_and_oversize_rejected` signs with `other-purpose`. The positive tests sign with the literal `'herdr-ml-official-v1'`, not `p.NAMESPACE`, so a change to the constant breaks the happy path. The "signature over a different manifest" case is covered by `test_changed_..._or_manifest_rejected`, which rewrites `demo` to `evil` and keeps the old `.sig`. |
| M3 manifest FIFO / symlink | **Fixed** | `verify_bundle` opens the manifest with `O_RDONLY\|O_NOFOLLOW\|O_NONBLOCK` and checks `S_ISREG` through `fstat`. The `ProvenanceError` is not in the `except` tuple, so its message reaches the caller. `test_manifest_fifo_rejected_without_blocking` uses a subprocess with a 2-second timeout and checks the message. `test_manifest_symlink_rejected_with_otherwise_valid_signature` covers the symlink case. |
| L1 exceptions outside `ProvenanceError` | **Open** | `json.loads` can still raise a plain `ValueError` (int-digit limit, Python ≥3.11) or a `RecursionError` (deep nesting). The `except` tuple catches only `JSONDecodeError`. This happens only after signature verification and still fails closed. Fix: add `ValueError` and `RecursionError` to the `except` tuple. |
| L2 unknown top-level keys | **Fixed** | The code now requires the key set to equal the six v1 fields exactly. The `extra` subtest would fail if that check were removed. |
| L3 string and seed constraints | **Open** | `project` and `argv` still accept control characters and lone surrogates; `argv` only rejects `\x00`. `seed` has no range limit. |
| L4 doc precision | **Open** | Details under L4 below. |
| L5 no revocation / no timestamp | **Open** | Not documented. |
| L6 `ssh-keygen` from PATH, inherited environment | **Open** | Documented as a trust requirement, but not hardened. |
| Integration: identical split hashes | **Fixed (optional guard added)** | `verify_bundle` rejects identical split digests. `test_identical_train_validation_test_content_rejected` is effective: if the check were removed, the signed manifest would verify and the test would fail. |
| Integrations: replay, project/SHA binding, TOCTOU after return, source↔SHA, environment binding | **Open (by design)** | Disclosed in the docs and consistent with the holds. |

**L4 details.** The docs say "symlinks in any path component are rejected", but ancestors of the bundle directory are still followed. If the bundle directory itself is a symlink, the manifest opens (its `O_NOFOLLOW` applies only to the final component), but `_hash_relative`'s root `os.open(root, …O_NOFOLLOW)` raises `ELOOP`. That fails closed, but the reported error is `'cannot safely read artifact'`, which is misleading. "Paths are distinct" still means distinct strings only.

## New findings (all LOW)

- **N1. Two negative subtests would still pass if their control were removed.** This is the same pattern as old M1, but low severity because the schema checks make each control redundant today.
  - **NaN/Infinity seed:** if `parse_constant` were dropped, `float('nan')` would still fail with `'invalid seed'`.
    - Fix: put the constant somewhere the schema doesn't reject it, or assert `'nonstandard JSON constant'` with `assertRaisesRegex`.
  - **UTF-16 manifest:** a lenient decode such as `errors='replace'` would still fail in JSON parsing.
    - Fix: sign valid UTF-8 JSON containing an invalid byte inside a string value, e.g. `"project":"de\xffmo"`, and assert the specific failure.
- **N2. No test for non-regular or racing artifacts.**
  - Nothing exercises a FIFO or device artifact. The `O_NONBLOCK` and `S_ISREG` handling in `_hash_relative` is correct, but if `O_NONBLOCK` were dropped, a FIFO artifact would hang verification and no test would notice.
    - Fix: add a FIFO-artifact test in a subprocess with a timeout, mirroring the manifest FIFO test.
  - The `'artifact changed during verification'` path is also untested. That is acceptable, since it needs fault injection.
- **N3. The bundle root is re-resolved by path for each artifact.** `_hash_relative` reopens `manifest_path.parent` on every call, and the manifest was opened by path too.
  - Each artifact is still hash-bound, so integrity holds.
  - But someone with write access to the parent of the bundle could swap the bundle directory partway through, so different artifacts could come from different directory trees.
  - This is within the documented point-in-time limitation. Opening the root once as a dirfd and using it for the manifest and every artifact would close the gap cheaply.
- **N4. The signature file is read by `ssh-keygen` by path, with no `O_NOFOLLOW` or regular-file check.** A FIFO `.sig` blocks until the 15-second timeout, which then raises `ProvenanceError` (bounded and fail-closed). If the signature can come from an untrusted bundle, read it the same way as the manifest and pass it through a temporary file or `/dev/stdin`.
- **N5. Minor ordering.** The identical-split check runs only after every artifact has been hashed. It needs only the signed digests, so running it before hashing would avoid hashing large datasets just to reject the bundle.

## Recommended next steps (optional, in order)

1. Add `ValueError` and `RecursionError` to the `except` tuple (L1), with one signed test for a 5000-digit seed.
2. Tighten the N1 tests so each fails only if its own control is removed.
3. Add the FIFO-artifact test (N2).
4. Open the bundle root once as a dirfd (N3), and fix the L4 wording to "symlinks at or below the bundle directory are rejected; ancestors are trusted."
