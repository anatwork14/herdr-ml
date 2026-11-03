# Preliminary independent GPT artifact inventory re-review

Reviewed immutable commit `f4a50fca4da35015f45b17a771618211b7a298ed` in a new detached worktree. Opus quota was unavailable. This is a fallback review of the diagnostic inventory, not execution authorization or a scientific/data-correctness certification. No GPU or live service was used. No CRITICAL/HIGH issue was confirmed in this standalone, non-authorizing scope.

## MEDIUM — generated manifest may exceed its own verify limit

- Location: `scripts/artifact_tree.py:30-54, 145-154`.
- Root cause: the global enumeration budget counts each basename once but `records` accumulates the full repeated path for every entry; no serialized output budget is applied before returning or printing. The verify path independently rejects manifest inputs over 16 MiB. Deep common prefixes can therefore produce valid output too large for the same CLI to verify, while accumulating substantially more memory than the name budget suggests.
- Reproduced with a temporary directory: create 14 nested directories each with a 245-character component, then 4,900 empty files in the deepest directory. `snapshot(root)` succeeded with 4,914 entries; `json.dumps(..., ensure_ascii=True, sort_keys=True, indent=2)` measured 17,681,358 bytes versus `MAX_MANIFEST_BYTES == 16,777,216`.
- Remediation: enforce a global cumulative expanded-path and serialized-output budget while accumulating records (including escaping/JSON overhead), and bound both library and CLI output; or explicitly support streaming/bounded manifests and a compatible verify limit.
- Acceptance: the above tree is either refused early with a bounded-memory error or emits a manifest that the same CLI can verify; include deep/wide and escaped Unicode paths, exact limit boundaries, and account for retained list/record memory.

## MEDIUM — verification of a FIFO manifest blocks indefinitely

- Location: `scripts/artifact_tree.py:148-149`.
- Root cause: ordinary `Path.open('rb')` blocks on a FIFO before size-limited `read`, and follows a final symlink. This is a diagnostic denial of service, not a demonstrated privilege escalation.
- Reproduced: create an empty temporary tree and `os.mkfifo(manifest)`, then `subprocess.run([python, 'scripts/artifact_tree.py', tree, '--verify', manifest], timeout=1)`: `TimeoutExpired`.
- Remediation: safely open the manifest with no-follow, nonblocking flags, verify it is a regular file via fstat, and only then bounded-read. Maintain documented trusted root-ancestor boundary.
- Acceptance: FIFO and symlink manifests both return BLOCKED within a short timeout, with a valid normal file still verifying.

## MEDIUM — duplicate JSON keys are silently accepted

- Location: `scripts/artifact_tree.py:152`.
- Root cause: default `json.loads` applies last-key-wins, allowing conflicting encodings to be treated as a single schema.
- Reproduced: inventory a one-file tree, write `{\"schema_version\":0,\"schema_version\":1,\"entries\":<correct entries>}`, and invoke `--verify`; the command exits 0.
- Remediation: strict UTF-8, duplicate-key rejection at all nesting depths, rejection of nonstandard JSON constants and exact schema validation; don't represent a successful hash comparison as signature or authorization.
- Acceptance: conflicting duplicate keys at top level and in entries and nonstandard constants return BLOCKED; deterministic valid output still round-trips.

## Verification and boundaries

- Focused `test_artifact_tree.py`: 8/8 pass on Python 3.10.21 and 3.12.14. The above adversarial scenarios were not covered by that suite.
- The new global enumeration counter is incremented before sorting, and the depth and individual path guards bound the corresponding traversal paths; this does not cap aggregate expanded output or prove immutable data access.
- `O_NOFOLLOW`, fd-relative enumeration and `O_PATH` for regular child entries reduce symlink/device substitution exposure; the tool explicitly trusts root ancestors and is not a filesystem isolation boundary.
- Official, queue and GPU holds remain necessary. No independent scientific validation, actual model execution or immutable execution binding is claimed.
