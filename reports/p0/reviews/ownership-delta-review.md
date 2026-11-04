# Follow-up review: `c52b2eb` vs `29b32a4`

**Scope:** static reading of the supplied diff and the Git-blob sources of `scripts/researchctl.py` and `scripts/run_registry.py`. Nothing was executed and no tests were run.

## Verdict

**No new critical or high defects.** The commit does what it says. The UNKNOWN containment labeling and the preliminary log-hash labels are truthful on every path I traced. Two medium items and a few low items are below.

## 1. UNKNOWN containment remains truthful

I traced every exit path of `execute()` and the two read paths.

| Path | Registry | `result.json` / `status.json` | Reported by `status` / `reconcile` |
|---|---|---|---|
| Normal leader exit (0 or non-zero) | `UNKNOWN` via `finish` | `UNKNOWN`, `containment: UNKNOWN` | UNKNOWN |
| Interrupted before `Popen` (code `128+sig`) | `UNKNOWN` | `UNKNOWN` | UNKNOWN |
| `Popen` OSError (code 127) | `UNKNOWN` | `UNKNOWN` (conservative, still truthful) | UNKNOWN |
| `record_child` or `advance` raises after spawn | stays `DISPATCHING` | status `DISPATCHING`; no result | Mapped to UNKNOWN |
| Official gate `BLOCKED` after claim | stays `CLAIMED` | status `BLOCKED` | `status` prefers the registry, so `CLAIMED` maps to UNKNOWN |
| `sha_file` raises after reap (see M2) | stays `RUNNING` | status `RUNNING`; no result | Mapped to UNKNOWN |

- `finish()` still rejects any terminal state other than `UNKNOWN`.
- `leader_state: "COMPLETED"` is scoped to the leader, so it is not a whole-workload claim.
- The return-2-on-leader-success behavior is unchanged.

## 2. Preliminary log-hash labels are truthful

- **Rename:** `stdout_sha256` / `stderr_sha256` become `*_after_leader_exit`. The names now match the semantics, and the old keys are absent in new records (test asserts this).
- **Timing:** dict literals evaluate left to right. So `leader_completed_at` is taken before hashing and `log_hash_observed_at` after both hashes finish. The ordering is honest.
  - Small nuance: `leader_completed_at` is really "after reap", and `log_hash_observed_at` marks when hashing ended, not when it began.
- **Parent writes flushed:** the parent's own file handles are closed (the `with` exits) before hashing, so parent-side buffered writes such as the OSError text are flushed.
- **Docs:** the OPERATIONS.md paragraph correctly says the values are not final and not an immutable snapshot, and that a re-hash is needed after containment is verified.
- **Fencing test:** the `test_registry_p0.py` change is a real fix. The old test passed `state: COMPLETED`, which failed on `Invalid terminal state` before the owner check ever ran, so fencing was never exercised. With `UNKNOWN` plus `assertRaisesRegex('Stale owner')`, the owner-fencing branch is now actually covered.

## Findings

### M1 (Medium): legacy records still carry unlabeled hashes

Results written before this commit carry `stdout_sha256` / `stderr_sha256` with the same post-leader-exit semantics but no qualifying label.

- `reconcile` prints `registry.result` verbatim, so operators will see these keys on old runs.
- OPERATIONS.md only documents the new keys.

**Fix:** add one sentence to the docs saying legacy `stdout_sha256` / `stderr_sha256` fields have identical non-final semantics. Optionally, have `reconcile` annotate them.

### M2 (Medium, pre-existing): hash failure leaves an incomplete record

If `sha_file` raises after the leader has been reaped (for example, a descendant deleted or replaced a log), then:

- `finish()` and the `result.json` write are skipped;
- the registry stays `RUNNING`;
- the leader exit code is lost from the authoritative record.

Truthfulness is preserved, since this is reported as UNKNOWN, but forensic data is dropped.

**Fix:** wrap each hash, record `null` plus an error string, and still call `finish()`.

### L1: hash-limitation wording could be stronger

A descendant shares the file description (opened `"wb"`, no `O_APPEND`). It can seek, truncate, or write concurrently, so a hash may match no on-disk state that ever existed (a torn read), not just an earlier one. Consider saying "may not correspond to any single point-in-time content."

### L2: `log_hash_observed_at` is undocumented

The new field is not mentioned in OPERATIONS.md.

### L3: fingerprint `sys_path[0]` will differ from the worker

With the absolute script path, `sys_path[0]` becomes `herdr-ml/scripts`, which will differ from any worker or workload invocation.

- This is harmless while the fingerprint is non-attesting.
- But `--expect` comparisons are only meaningful if the same script path is used both times. Worth one clause in the docs.
- I could not confirm from the supplied blobs that `scripts/runtime_identity.py` exists at that path. It was presumably added in `29b32a4`.

### L4: test gaps

`test_researchctl.py` checks key presence only. It does not check:

- that `log_hash_observed_at` is present;
- that the hash values equal `sha256` of the actual log files;
- that the registry `result_json` matches `result.json`.

### L5: possible downstream consumers of the old keys

Any tooling that reads `result.json["stdout_sha256"]` will silently get nothing. Nothing in the supplied sources reads them, but grep any external consumers before merge.

## Recommended before merge

M1 (a one-line docs fix). M2 can be tracked separately since it predates this commit.
