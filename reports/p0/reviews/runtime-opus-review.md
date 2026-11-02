# Review: commit `95f7b08`, runtime and daemon fingerprinting

**Scope:** `scripts/runtime_identity.py`, `tests/test_runtime_identity.py`, `.github/workflows/ci.yml`, `.python-version`, and `docs/OPERATIONS.md`. I reviewed only the supplied source and ran nothing. I make no claim about whether the daemon, Pueue, or Herdr is ready.

**Verdict:** I found no confirmed critical defects. The script fails closed on the paths it handles, and the docs correctly say a match is not proof of execution. There are two high-severity gaps, and both matter before this fingerprint is used as a gate for queued execution. Neither is exploitable while the P0 hold blocks queued runs.

---

## High

### H1. The fingerprint can match while the runtime the code actually uses differs
`identity()` covers the executable bytes, `sys.prefix`/`sys.base_prefix`, and the (name, version) pairs from `importlib.metadata.distributions()`. Each of the following changes what code runs without changing the fingerprint:

- **`PYTHONPATH` or extra `sys.path` entries holding modules without dist-info.** For example, a bare `numpy/` directory that shadows the installed one. It has no metadata, so it is invisible to `distributions()`.
- **Code that runs at startup:** `sitecustomize`/`usercustomize`, `.pth` lines, `PYTHONSTARTUP`-style hooks, `LD_PRELOAD`, and `LD_LIBRARY_PATH`.
- **Shared-library builds.** When the interpreter is built with `--enable-shared` (actions/setup-python builds are; many pyenv and conda configurations can be), `executable_sha256` hashes only a small launcher. `libpython3.10.so` and the whole stdlib are not covered.
- **Interpreter flags** (`-I`, `-E`, `-s`, `-S`, `-O`, `-X`), which change import behaviour. Some show up indirectly through the package list, but most do not.
- **Package contents.** The docs disclose this one.

**Impact:** `--expect` returns 0 for two workers whose imported code differs materially. That defeats the stated purpose of comparing runtimes.

**Action:**
- Add these fields: `sys.path`, `sys.flags`, a hash of `sys.version` together with `sysconfig.get_config_var('LDLIBRARY'/'Py_ENABLE_SHARED')`, a hash of the resolved libpython (if shared), the stdlib directory, and the presence of `sitecustomize`/`usercustomize`.
- Fingerprint an allowlist of environment variables: `PYTHON*`, `LD_*`, `PATH`, `VIRTUAL_ENV`.
- Optionally hash the `RECORD` files of the installed distributions. This is cheap and catches most in-place edits.
- Bump `schema_version`.

### H2. The fingerprint process is not the job process, and nothing binds the two
The docs say to run `runtime_identity.py --expect` inside the daemon. What that measures is the environment of that one invocation. The job is a separate invocation with its own argv, flags, cwd, and possibly its own environment. The output also contains no run ID, task ID, nonce, or timestamp, so a preserved "match" cannot be tied to a specific run. This sits directly on one of the listed P0 blockers ("provenance binding").

**Action:**
- Have the worker wrapper (the supervisor that `exec`s the job) compute the fingerprint in-process immediately before `exec`, using the same interpreter and flags.
- Record the fingerprint in the run's SQLite registry along with the run ID and the owner token.
- Accept a `--nonce` or `--run-id` argument and echo it in the output, so preserved evidence can be bound to a run.
- Until that exists, the docs should state outright that a standalone match is not evidence for any particular run. The current wording comes close but stops short of that.

---

## Medium

### M1. `sys.executable` is not guaranteed to be the running image
On Linux, CPython 3.10's path calculation derives `sys.executable` from `argv[0]`, searching `PATH` when `argv[0]` has no slash. It does not use `/proc/self/exe`.

For example, `exec -a python3 /opt/other/python3.10 script.py` runs one binary while the script hashes whatever `python3` resolves to on `PATH`. A supervisor that sets `argv[0]` can trigger the same mismatch.

**Action:** On Linux, hash `os.readlink('/proc/self/exe')`. Block if that path differs from `Path(sys.executable).resolve()`, or if it ends in ` (deleted)`.

### M2. The test cannot tell a fail-closed block from a crash
`assertNotEqual(returncode, 0)` also passes on an unhandled traceback, which exits with 1. A regression that broke `--expect`, for instance by raising `TypeError`, would still pass.

**Action:** Assert `returncode == 2` and `'BLOCKED: interpreter fingerprint mismatch' in stderr`, and assert that stdout is empty on mismatch. Add cases for:
- a malformed JSON expect file
- a missing expect file
- a non-object JSON value
- changes to `packages_sha256`, `executable_sha256`, and `prefix`
- a `PYTHONPATH` difference, which will fail until H1 is fixed and serves as a regression marker

### M3. Some failure paths escape the `BLOCKED` contract
These cases exit with 1 and a traceback instead of 2 and a `BLOCKED` message. They still fail closed, but callers that check for exactly 2 or for the message will misclassify them:

- `TypeError` when `sys.executable` is `None` (embedded interpreters).
- `TypeError` from `sorted()` on broken distributions. A missing `Version` gives `None`, which cannot be compared with `str`. On 3.10, missing metadata can also raise inside `d.metadata`.
- `RecursionError` on a deeply nested expect file.

**Action:** Catch `Exception` around `identity()` and the comparison, map it to `BLOCKED` with exit 2, and normalise `None` to `''`.

### M4. The docs undermine the "same absolute interpreter" rule
The docstring says "use the same absolute interpreter." `OPERATIONS.md` says `python scripts/runtime_identity.py`, while every other command in the docs and CI uses `python3`. The capture step is trust-on-first-use: if `python` resolves to the wrong environment, every later comparison faithfully confirms the wrong baseline. The docs also never say to redirect the output to a file, or which cwd, flags, and environment to use.

**Action:** Document the exact command, e.g. `/abs/path/to/venv/bin/python3 -I scripts/runtime_identity.py > fingerprint.json`. Use whichever flags the worker will actually run with, because `-I` changes what gets measured. Also require operators to check the recorded `executable` and `prefix` by eye before trusting the baseline.

### M5. CI does not run on this branch's pushes
The push trigger is `main` and `feat/**`. Pushes to `fix/p0-reliability-remediation` are validated only if a PR is open.

**Action:** Add `fix/**` to the trigger, or trigger on all branches.

### M6. Actions are pinned by mutable tag
`actions/checkout@v4` and `actions/setup-python@v5` are pinned by tag. `contents: read` limits the damage, but pinning to full commit SHAs is the standard fix.

---

## Low / notes
- **No mismatch detail.** On mismatch the script prints no information about which keys differ. Emitting the differing key names (not the values) to stderr would help operators without leaking anything meaningful.
- **Self-attestation only.** Code in a compromised environment (via `.pth` or `sitecustomize`) runs before the script and can falsify the output. The docstring says "not a sandbox"; it should also say "not tamper-evident."
- **Shadowing.** `sys.path[0]` is `scripts/`, so a stray `scripts/json.py` or `scripts/hashlib.py` would shadow the stdlib module inside the fingerprinting tool itself. Keep that directory free of stdlib names, or check for it in a test.
- **Pinned version.** I could not verify from the source alone that `3.10.21` exists in the setup-python version manifest. If it doesn't, CI fails loudly rather than silently, so the risk is limited, but confirm it.
- **Import ordering.** The `importlib.metadata` import runs before the 3.10 version check. On Python 3.7 or older, the script dies with `ImportError` (exit 1) instead of printing the `BLOCKED` message. Moving the import inside `identity()` fixes this.

## Not assessed
I did not assess `researchctl.py`, the registry or reconcile code, the bootstrap scripts, the Pueue setup, or any actual daemon behaviour. None of them were supplied.
