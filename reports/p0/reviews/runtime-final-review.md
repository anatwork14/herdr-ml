# Review: commit `29b32a4`, hardened diagnostic interpreter fingerprint

**Method:** I read only the source pasted above. I used no tools, ran no tests, and did not check whether a CI run exists for this commit. Any outcome I describe below is inferred from reading CPython 3.10 and Linux behaviour, not observed. I make no claim about scientific validity, operational readiness, or the state of the daemon, Pueue, or Herdr.

**Scope:** `scripts/runtime_identity.py`, `tests/test_runtime_identity.py`, `docs/OPERATIONS.md`, `.python-version`, and `.github/workflows/ci.yml`. I did not see `researchctl.py` or any worker or supervisor code. So I cannot confirm whether the fingerprint is invoked anywhere in live execution. Everything below concerns the standalone component.

## Verdict

**Standalone component:** I found no confirmed CRITICAL or HIGH defects. Every failure path I traced ends in exit 2 with a `BLOCKED:` message, and I found no path that wrongly exits 0 on a mismatch the tool claims to detect. Most of the prior review's H1 and M1–M4 findings are addressed.

**Integrated execution:** One HIGH *prerequisite gap* remains. It is the prior H2: the fingerprint is not bound to any run. The docs now state this correctly, so it is a missing capability rather than a false claim. It must be closed before this tool can serve as an execution gate.

Several MEDIUM gaps also remain. In each one, two runtimes behave differently but produce the same fingerprint.

---

## HIGH: remaining prerequisite gap (integration, not a defect)

### P1. The fingerprint is not bound to the job process or to any run (prior H2, unchanged in code)
- **What exists:** no `--run-id` or `--nonce` argument, no output in the registry, and no in-process call from a worker wrapper before `exec`.
- **Structural consequence:** the record contains `sys_path` and `cwd`. `sys.path[0]` is always the `scripts/` directory of the fingerprinting tool. A job started as `python3 train.py` or `python3 -c ...` has a different `sys.path[0]`. So the tool can never measure the job's import path. It can only measure "the same tool run from the same place."
- **Status:** the docs now say "not bound to any actual run," which closes the overclaim. The capability itself is still one of the P0 blockers listed in the hold ("provenance binding"). It is safe only because queued and official execution are blocked.
- **Required before gating:** the supervisor computes the record in-process, using the job's interpreter and flags, immediately before `exec`. It then persists the record together with the run ID and owner token in the per-run SQLite registry.

---

## MEDIUM

### M1. Some behaviour-changing interpreter options are still not captured
`flags` covers `sys.flags`, but these are missing:
- `sys.warnoptions` (`-W`, `PYTHONWARNINGS`)
- `sys._xoptions` (`-X faulthandler`, `-X tracemalloc`, `-X pycache_prefix=...`, and others)
- `sys.pycache_prefix`

**Scenario (expected, not run):**
1. Capture a baseline without options.
2. Run `python3 -W error scripts/runtime_identity.py --expect fp.json`.
3. Expected result: exit 0, because the script itself raises no warnings. A job run with `-W error` turns warnings into exceptions, so it behaves differently from the baseline.

**Fix:** add `warnoptions`, `_xoptions`, and `pycache_prefix` to the record and bump the schema version.

### M2. `PYTHONHASHSEED` values cannot be told apart
`sys.flags.hash_randomization` is 0 only when `PYTHONHASHSEED=0`. A fixed seed such as `123` reports 1, the same as random seeding. Iteration order of str-keyed sets changes with the seed, which matters for reproducibility in this ML-research context.

**Scenario:**
1. Capture a baseline with `PYTHONHASHSEED` unset.
2. Compare with `PYTHONHASHSEED=123` set.
3. Expected result: a match.

**Fix:** record whether `PYTHONHASHSEED` is set and its value. The seed is not a secret.

### M3. Native libraries visible in `/proc/self/maps` are ignored unless they are libpython
The code already parses `/proc/self/maps` but keeps only `libpython*`. The following changes are directly observable in that file but are discarded:
- libraries loaded via `LD_PRELOAD`
- a `libcrypto`/`libssl` picked up through `LD_LIBRARY_PATH` by `_hashlib`
- a different `libffi`

**Scenario:** `LD_PRELOAD=/tmp/x.so python3 … --expect fp.json` is expected to exit 0.

**Fix:** record every file-backed mapping as a path plus hash, or at least every `*.so*` mapping. For this script the set of loaded extensions is deterministic, so the record stays stable.

### M4. Hashes are taken from the path, not from the running image (TOCTOU)
`sha_file(executable)` and `sha_file(libpython path)` reopen files by path after the `/proc` check.

- **Executable:** `ETXTBSY` prevents in-place writes. However, a `rename()` onto the path between the `/proc/self/exe` comparison and `sha_file` means the tool hashes the new file. The `(deleted)` check has already run by then.
- **libpython:** `ETXTBSY` does not protect shared libraries mapped by `ld.so`. An in-place overwrite (`cp new.so libpython3.10.so.1.0`) leaves no `(deleted)` marker, and the recorded hash describes bytes the process may not have loaded.
- **Fix:** hash `/proc/self/exe` directly; opening it reaches the running inode even after deletion. For libraries, compare `st_dev`/`st_ino` from `os.stat(path)` with the device and inode fields in the maps line, before and after hashing. Block on any difference.
- **Severity:** low-medium on a trusted single host, but it is cheap to fix.

### M5. A shared build with no detected libpython is accepted silently
If `build.py_enable_shared == 1` on Linux and `libpython == []`, the tool still exits 0. The docs say an empty list "only means none were detected." For a shared build, that is an internal inconsistency and should block. It cannot be explained away.

**Fix:** on Linux, `if py_enable_shared and not libpython: raise ValueError(...)`.

### M6. CI still does not run on pushes to this branch (prior M5, unaddressed)
The push trigger is still `main` and `feat/**`. Pushes to `fix/p0-reliability-remediation` are validated only through an open PR, so I cannot assume this commit's tests ran in CI.

### M7. Actions are still pinned by mutable tag (prior M6, unaddressed)
`actions/checkout@v4` and `actions/setup-python@v5` are not pinned to commit SHAs.

---

## LOW
- **L1. Script-directory shadowing (prior note, unaddressed).** `sys.path[0]` is `scripts/`, and Python 3.10 has no `-P` option. `importlib.metadata` lazily imports `email`, `csv`, `zipfile` and others. A stdlib-named file in `scripts/` would run inside the fingerprinting tool. Add a test that `scripts/` contains no names from `sys.stdlib_module_names`.
- **L2. Strict UTF-8 decoding of `/proc/self/maps`.** Any mapped file whose path is not valid UTF-8 raises `UnicodeDecodeError`, which blocks. This fails closed but is a spurious denial. Read the file in binary mode, or use `errors='surrogateescape'`.
- **L3. Doc accuracy: "It does not emit environment variable values."** `sys_path` contains the literal `PYTHONPATH` entries, and `prefix`/`executable` reflect the venv location. Reword it, for example: "does not emit environment variables directly; `sys_path` includes `PYTHONPATH` entries."
- **L4. The doc command uses a relative script path.** `scripts/runtime_identity.py` is relative, yet the docs say to run it "from the intended working directory." If the worker's working directory is not the repo root, the command fails. Use an absolute script path.
- **L5. Error output is uninformative.**
  - `BLOCKED: cannot fingerprint runtime: <TypeName>` does not distinguish a fingerprinting failure from an `--expect` read failure.
  - A mismatch does not name the differing keys (prior note, unaddressed).
  - As a result, `test_spoofed_argv0_is_blocked` passes on *any* `BLOCKED:` message, not specifically the `/proc/self/exe` mismatch. Emit a reason code and assert on it.
- **L6. argparse errors exit 2 without the `BLOCKED:` prefix.** Callers that key on the exit code alone will conflate "bad invocation" with "blocked." Both are non-zero, so this still fails closed.
- **L7. `str(d.metadata.get('Name') or '')` collapses distinct broken distributions to `('', version)`.** This is acceptable but lossy. Consider blocking on nameless distributions instead.
- **L8. The `3.10.21` pin is unverified.** I still cannot confirm from source that setup-python's manifest has this version. If it doesn't, CI fails loudly.

---

## Disposition of prior findings
| Prior | Status |
|---|---|
| H1 runtime coverage | **Partial.** Added: `sys.path`, `sys.flags`, build vars, mapped libpython, stdlib path, sitecustomize/usercustomize presence, schema v2. Missing: env allowlist, `-W`/`-X` (M1), hash-seed value (M2), non-libpython libraries (M3), `RECORD` hashes, stdlib and `.pth` contents (disclosed). |
| H2 run binding | **Docs only.** The capability is still absent (P1). |
| M1 argv0 spoofing | **Fixed** with a `/proc/self/exe` comparison. A TOCTOU remains (M4). |
| M2 test precision | **Largely fixed.** Exit 2, message, and empty-stdout assertions were added, along with malformed, missing, field, `PYTHONPATH`, and `-O` cases. The spoof test is still loose (L5). |
| M3 traceback paths | **Fixed** with `except Exception` and normalisation of `None` to `''`. |
| M4 docs / interpreter | **Largely fixed.** The relative script path remains (L4). |
| M5 CI trigger, M6 SHA pinning | **Unaddressed.** |
| Low: tamper-evidence wording, import ordering | **Fixed.** |
| Low: mismatch detail, shadowing | **Unaddressed.** |

## Not assessed
I did not assess:
- whether any supervisor or `researchctl` path calls this tool
- the registry, reconcile, or containment code referenced in `OPERATIONS.md`
- the bootstrap or Pueue scripts
- actual CI results
- runtime behaviour on any real host

The final paragraphs of `OPERATIONS.md` describe registry and containment behaviour whose source I was not given. I cannot confirm that they are accurate.
