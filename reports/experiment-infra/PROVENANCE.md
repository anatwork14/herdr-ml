# W4 execution manifest and worker-side provenance binding

Status: **OFFLINE_TESTED** for the manifest and its verifier (`scripts/execution_manifest.py`). This document does not claim PROVENANCE_READY. The manifest is not connected to `scripts/researchctl.py`, and official execution stays blocked there.

## Purpose

The existing `scripts/provenance.py` verifies a signed envelope and artifact hashes at one point in time. Nothing binds the launched process to that evidence. This module defines the canonical execution manifest, a closed set of identity fields, and the checks a worker runs immediately before execution. The manifest records what a run is allowed to use. The verifier checks that the bytes and environment on hand match the manifest at that moment. Neither one proves that the process later reads only these bytes.

## Identity fields (schema version 1)

`kind` is `herdr-ml-execution-manifest`. Top-level fields are exactly:

| Field | Content | Identity role |
|---|---|---|
| `run_id`, `attempt_id` | Identifiers (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`) | Binds the manifest to one attempt; approvals bind the same pair |
| `project`, `operation`, `resource_class` | ID; `operation` is `diagnostic-execution` or `official-execution` | Scope and authorized resource class |
| `source` | `commit` (full object id, informational), `worktree_clean: true`, `files` (sorted `{path, sha256, size}` of every declared source file), `files_sha256` | Source identity. The commit alone is rejected because the file list is required |
| `interpreter` | Resolved absolute `path`, SHA-256 of the image, Python version, implementation | Executable identity |
| `runtime` | Implementation, version, sorted `[name, version]` distributions, `packages_sha256` | Dependency identity |
| `command` | `argv` (`argv[0]` must equal `interpreter.path`), relative `cwd` | Executable and arguments |
| `config` | `{path, sha256, size}` | Configuration digest |
| `inputs` | Sorted roles with `{path, sha256, size}`. Required roles: `dataset_content`, `dataset_manifest`, `split_manifest`. Optional: `client_manifest` and other IDs (not `config`) | Dataset and input digests |
| `splits` | `split_manifest_sha256`, `client_manifest_sha256` (or null); must agree with `inputs` | Split and federated client manifest digests |
| `seeds` | `primary` integer and `named` map of integers | Deterministic seeds |
| `nondeterminism` | Sorted list of declared sources (may be empty) | Declared nondeterminism; an empty list is a claim of determinism |
| `output` | `root` ID and relative `path` under the run directory | Authorized output destination |

Canonical serialization: sorted keys, `separators=(',', ':')`, ASCII, no floats (rejected on parse and on encode), integers limited to ±(2^53−1), no duplicate keys, no NaN/Infinity, and UTF-8 only. A manifest digest is `SHA-256(canonical bytes)`. Parsing requires the presented bytes to equal the canonical form, so a manifest "in another serialization" fails even when its JSON value is equal.

## Creation

`build_manifest(...)` runs in the launcher with the interpreter it records:

1. Requires a clean checkout, using the git policy below. `git status --porcelain=v1 -z --untracked-files=all --ignored=matching --ignore-submodules=all` must be empty, so untracked and ignored files both block. Submodules are refused before this query, so the ignore flag never hides submodule content. The source root must be the repository top level, and HEAD must be a commit.
2. Hashes each declared source file, each input and the config. Each file is read once. Symlinks, non-regular files and paths with symlink components are rejected.
3. Records the resolved running interpreter and SHA-256 of `/proc/self/exe`. It also records the package list of the running interpreter.
4. Validates the result against the schema before returning it.

## Worker-side verification

`verify_before_execution(manifest_bytes, expected_sha256, run_id, attempt_id, worker_id, source_root, run_dir, seal_dir)` runs immediately before execution and performs these checks in order. Each failure raises one reason code and stops:

1. `expected_sha256` equals SHA-256 of the presented bytes (`MANIFEST_DIGEST_MISMATCH`).
2. Strict parse, canonical bytes, and schema (`MANIFEST_MALFORMED`, `MANIFEST_NONCANONICAL`, `MANIFEST_SCHEMA`).
3. `run_id` and `attempt_id` match the values the worker was given (`RUN_ID_MISMATCH`, `ATTEMPT_ID_MISMATCH`).
4. The running interpreter path and `/proc/self/exe` SHA-256 match the manifest (`INTERPRETER_PATH_MISMATCH`, `INTERPRETER_DIGEST_MISMATCH`). If procfs is unavailable, the check fails closed (`INTERPRETER_UNVERIFIABLE`).
5. The current package list and version digest match (`RUNTIME_DRIFT`).
6. HEAD equals `source.commit` (`SOURCE_COMMIT_MISMATCH`). Each declared source file hashes to its recorded digest (`SOURCE_MUTATED`, `SOURCE_FILE_MISSING`). The checkout is clean (`SOURCE_DIRTY`).
7. Each input is copied to a private sealed file while the same bytes are hashed (hash-then-consume). The digest and size must match. Mismatches use specific codes: `DATASET_DIGEST_MISMATCH` for `dataset_content`, `SPLIT_MANIFEST_MISMATCH` for split or client manifests, and `INPUT_DIGEST_MISMATCH` otherwise. Missing files give `INPUT_MISSING`. Unsafe paths, including symlinks and FIFOs, give `INPUT_UNSAFE_PATH`. A file that changes during the read gives `INPUT_CHANGED_DURING_READ`.
8. The output destination resolves under `run_dir` and does not already exist (`OUTPUT_DESTINATION_INVALID`).

The returned plan contains the manifest digest, run and attempt identity, worker id, effective runtime (the running image digest and runtime fields), the inputs actually used, the sealed copy paths, and the output path. Execution must use the sealed copies, not the original paths.

## Git policy

Every git call runs with the following controls:

- A fresh environment built from scratch: no inherited `GIT_*` variables, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL` set to the null device, and `HOME` and `XDG_CONFIG_HOME` pointing at an empty temporary directory.
- Command-line overrides: `core.fsmonitor=false`, `core.attributesFile` set to the null device, `core.hooksPath` set to the null device, and `core.untrackedCache=false`.
- Refusal, not filtering: before any status query, the repository's merged configuration is listed. Any key beginning `filter.` or `diff.` makes the source unverifiable (`SOURCE_UNVERIFIABLE`). Git therefore never runs a clean or textconv driver from repository configuration.

- Submodule refusal: any gitlink (mode `160000`), any `.gitmodules`, or a non-empty `.git/modules` makes the source unverifiable (`SOURCE_UNVERIFIABLE`). Plain `git status` in the superproject runs a submodule's clean filter even when the superproject configuration is clean, so submodules are never examined. Test `test_submodule_filter_is_refused_and_never_runs` builds that layout and checks the marker.

Git attributes naming a filter with no configured driver are inert. Git applies no filter, and such a repository is not refused by this rule. The policy is the refusal of configured drivers, not hashing without git filters. Test `test_repo_local_clean_filter_is_refused_and_never_runs` checks that a modified tracked file in a repository with a configured clean filter reports `SOURCE_UNVERIFIABLE`, and that the marker the filter would create does not exist afterward. Plain `git status` does run that filter, which was confirmed separately. Test `test_repo_local_diff_driver_is_refused` covers the diff rule. Test `test_ignored_file_makes_checkout_dirty` shows that the ignored-file check is active.

## Result record

`build_result_record(plan, result_path, exit_status, signal_number, containment)` writes canonical bytes containing: manifest digest, worker id, run and attempt id, effective runtime, inputs used, result artifact SHA-256 and size (`RESULT_ARTIFACT_MISSING` if absent), termination, containment, and `status`.

Containment is `UNKNOWN` by default. Overall `status` is `UNKNOWN` unless containment is `VERIFIED`, which then yields `COMPLETED` or `FAILED` from the exit status. A `VERIFIED` input must carry the attempt's own `run_id` and `attempt_id`, or it fails with `CONTAINMENT_MISMATCH`. This phase does not verify the W2 evidence; the caller must supply it only after W2 has verified it.

## What this does not prove

- Source identity is the declared file list plus a clean checkout at HEAD. A file the job reads that was not declared is not covered. The declared file set is a claim until a sealed snapshot or access tracing enforces it.
- Checks happen at one moment. Changes after the check are not detected. The execution boundary must use the sealed copies and must enforce immutability (W1 and W2 deliverables).
- The interpreter check compares the running image with the manifest. Executing the same inode through a pinned descriptor is not implemented here.
- Runtime identity covers distribution names and versions only. It does not cover native libraries, CUDA, environment variables, locale, or hardware. Declared seeds and nondeterminism are recorded but not enforced.
- Output destination checks cover the path only. They do not restrict what the process writes.
- Containment is `UNKNOWN` until W2 supplies verified evidence. The `verifier_id` and `evidence_sha256` fields are unauthenticated in this module.
- A correct manifest does not establish scientific validity, leakage freedom, dataset license, or model quality.

## Integration points (not connected in this phase)

- `scripts/researchctl.py`, `cmd_init_run`: official mode currently raises through `require_supported_execution`. A future change may create the manifest at submission and bind the approval digest. It must keep official execution blocked until the trust anchor exists.
- `scripts/researchctl.py`, `execute`: a future change calls `verify_before_execution` and `verify_approval` (with consumption) after the existing gate and claim checks and before dispatch. W1 ownership fencing and W2 containment evidence are inputs to that call.
- `containment` in `build_result_record` is the W2 interface. W1 ownership fencing has no interface in this module.

Until those integrations are reviewed and an external trust anchor is provisioned (see `APPROVAL.md`), official execution stays blocked and this module is an offline checker only.

## Tests

`tests/test_execution_manifest.py` (46 tests). Submodule cases: `test_submodule_filter_is_refused_and_never_runs`, `test_tracked_gitmodules_file_is_refused`, `test_populated_git_modules_directory_is_refused`. Review regressions include repo-local clean-filter refusal with a marker check, diff-driver refusal, ignored-file dirtiness, a read that grows during hashing (`INPUT_CHANGED_DURING_READ`), symlinks in parent directories of source and input paths, surrogate code points, and a failed seal that removes only files it created. Earlier cases: canonical determinism; commit-only rejection; floats, NaN, truncation, duplicate keys, non-canonical and digest-over-other-serialization rejection; source mutation, untracked files, new commit, missing file, dirty checkout, non-top-level root, symlink in source path; dataset replacement, split and client substitution, missing and corrupted input, symlink and FIFO input, sealed-copy integrity, private seal directory; interpreter digest and path replacement; in-process and cross-process dependency drift; run, attempt, worker identity; output escape and pre-existing destination; result record with UNKNOWN containment, missing artifact and containment mismatch.
