# Adversarial review of commit `29b32a4`: researchctl, run registry, provenance, Herdr reconcile

**Method:** I used no tools and ran no tests. This is static reading of the source you pasted. I did not check that the paste matches the Git blobs, and I did not confirm any runtime behaviour. Every scenario below comes from reading the code and is not reproduced. Nothing here certifies scientific validity or operational readiness.

## Scope: what is live and what is standalone

| Component | Status at this commit |
|---|---|
| `researchctl.py init-run --mode diagnostic` (local) → `execute` | **Live, reachable.** This is the only path that executes anything. |
| `researchctl.py` official mode, queued mode, Pueue dispatch | Blocked by `require_supported_execution`. The code after the block is dead. |
| `researchctl.py status / reconcile / gate-status / doctor` | Live, read-only in intent. |
| `run_registry.py` | Live, used by the diagnostic path. |
| `bootstrap-herdr.sh` | Blocked by an unconditional `exit 2`. Everything after it is dead. |
| `provenance.py` | **Standalone.** Nothing imports it, so no execution decision depends on it. |
| `herdr_reconcile.py` | **Standalone.** It has no adapter and no CLI. Its findings are latent until it is integrated. |

## Verdict

- **CRITICAL:** none found.
- **HIGH on the live path:** none confirmed. The invariants the code claims appear to hold on Linux:
  - exactly-once claim, via `mkdir` plus a SQLite `BEGIN IMMEDIATE`;
  - no process-group signal after the leader is reaped, because `WNOWAIT` keeps the leader as a zombie so its PID/PGID cannot be reused;
  - fail-closed blocks on official and queued execution;
  - `UNKNOWN` as the only terminal state.
- **HIGH if integrated:** two findings in `herdr_reconcile.py` (L1, L2 below).
- Several MEDIUM/LOW defects listed below.

---

## Live path findings (`researchctl.py`, `run_registry.py`)

### M1: Parsed argv is not bound to the hashed bytes (TOCTOU) — MEDIUM
**Root cause:** `execute` reads `submission.json` twice. `load_json()` parses one read, and `sha_file()` hashes a second read. The registry stores the digest of the second read, but `Popen` runs the argv from the first.
**Scenario:** Between the two reads, `submission.json` is replaced with a version whose hash matches `submission.sha256`. The run then executes argv A while the registry records the digest of manifest B.
**Context:** The `.sha256` file sits in the same writable directory, so this is a corruption check, not authentication. Anyone who can write the folder can already rewrite both files.
**Fix:** Read the bytes once, hash them, then parse those same bytes.

### M2: Log hashes are recorded while escaped descendants may still be writing — MEDIUM
**Root cause:** After the leader is reaped, `stdout_sha256` and `stderr_sha256` are computed immediately. A descendant that called `setsid()` survives the group `SIGKILL`, keeps the inherited log fds, and keeps appending.
**Scenario:** The workload spawns a detached logger and exits 0. `result.json` records hashes that no longer match the log files. The code marks `containment: UNKNOWN`, but the hashes still look final. Label them as a snapshot or omit them.

### M3: No platform guard; on non-Linux, a run is launched, killed, and wedged — MEDIUM (outside the declared platform)
**Root cause:** `os.waitid` does not exist on macOS. The `AttributeError` is raised after `Popen` and after the registry reaches `RUNNING`.
**Scenario:** On macOS, the claim succeeds and the workload starts. `except BaseException` then calls `stop_and_reap()` and SIGKILLs it. The registry stays `RUNNING` permanently, and `main` does not catch `AttributeError`, so the user sees a traceback.
**Fix:** Refuse at the start of `execute` unless running on Linux with `os.waitid` available.

### M4: Claims can be stranded with no operator resolution path — MEDIUM (gap)
Several points leave a run permanently claimed with no tooling to resolve it:
- A crash after `mkdir execution.claim`.
- A commit followed by a failed directory fsync. The claim is committed but no token is returned.
- A SIGINT before the handlers are installed. Handlers are installed after `advance(DISPATCHING)`, so the default handler raises `KeyboardInterrupt`, which `main` does not catch.

In all cases the registry is left at CLAIMED/DISPATCHING. `reconcile` only reports `UNKNOWN`. Failing closed is correct here, but the documented manual investigation procedure is missing.

### L1: `status` contradicts the claim marker — LOW
If a crash happens after `mkdir execution.claim` but before the registry row is committed, `inspect()` returns `None`. `cmd_status` then falls back to `status.json` and prints `SUBMITTED`, while `reconcile` reports `UNKNOWN`. `status` should treat an existing `execution.claim` as `UNKNOWN`.

### L2: One bad registry hides every run in `status` — LOW
The per-folder `except` in `cmd_status` catches `OSError` and `JSONDecodeError`, but not `sqlite3.Error`. A single corrupt registry, or one locked for more than 30 s, aborts the whole listing through `main`. Separately, `inspect()` goes through `_connect`, which issues DDL and pragmas, so it is not strictly read-only.

### L3: Unhandled `AttributeError` on non-object JSON — LOW
These all raise an uncaught `AttributeError` and a traceback. They fail closed, but noisily:
- a gates file that is a JSON list (`data.get`);
- a config file that is a list;
- non-dict project entries.

### L4: Durability is asymmetric — LOW
SQLite is durable (`synchronous=FULL`). The JSON projections are not:
- `atomic_json` never fsyncs the file or its directory.
- `submission.sha256` is written with a non-atomic `write_text`.
- The run folder's entry in `runs/` is never fsynced.

After a power loss, the forensic manifest can be empty, or the whole run folder can be missing, even though a workload was launched.

### L5: Exit code 2 is ambiguous — LOW
A successful leader returns 2 by design, since containment is unknown. But 2 is also the exit code for refusals, claim conflicts, and argparse errors. Automation cannot tell "ran, outcome unknown" apart from "never ran".

### L6: Diagnostic provenance is weak — LOW (documented limitation)
- Git state is captured at submission only, and `dirty` is a boolean.
- Nothing is re-captured at execution time.
- A `SUBMITTED` run that was never executed stays executable through `_execute` indefinitely, for example after `execute` refused on the SIGCHLD check. It would then run against whatever the checkout contains at that later time.

### Paths I checked that look correct
- **Signal races:** a signal before, during, or just after `Popen`; the `group_owned` toggle inside `stop_and_reap`.
- **Reap ordering:** `waitid(WNOWAIT)` keeps the zombie, the group is SIGKILLed, then `wait()` reaps.
- **Leader PGID:** `start_new_session` makes the leader a session leader, which cannot call `setpgid`, so its PGID stays pinned.
- **Child start-time parsing:** the `/proc/<pid>/stat` field index (starttime is index 19 after `)`).
- **Exactly-once:** `mkdir` together with the `id=1` row check.
- **Registry transitions:** the transition whitelist, and the `finish` guard on `RUNNING`/`DISPATCHING`.

---

## Standalone components

### `provenance.py` (not wired into anything)
- **P1 — MEDIUM:** The manifest and artifacts are not bound to the same directory instance. The manifest is opened by path, then each artifact re-opens `manifest_path.parent` by name. If the parent directory, or one of its ancestors (only the final component uses `O_NOFOLLOW`), is renamed or swapped between signature verification and hashing, artifacts are hashed from a different tree. **Fix:** open the parent directory fd once, `openat` the manifest relative to it, and reuse that fd for the artifacts.
- **P2 — LOW:** `os.open` runs before the `S_ISREG` check. With `O_NONBLOCK` this avoids hanging on a FIFO, but a device node inside the tree is still opened, which can have side effects. Check with `fstatat(AT_SYMLINK_NOFOLLOW)` first.
- **P3 — LOW:** Some failures on a validly signed manifest escape as non-`ProvenanceError` exceptions. Very large integer literals exceed `int_max_str_digits` and raise a plain `ValueError`, and deep nesting raises `RecursionError`. Exploiting this requires a signer.
- **Integration gap:** none of these is cross-checked against anything:
  - `project`;
  - `source_git_sha` against the `source` artifact;
  - `argv` against what actually runs.

  As the docstring says, verification is point-in-time and does not authorize execution.

### `herdr_reconcile.py` (no adapter)
- **L1 — HIGH if integrated: legacy workspaces are duplicated.**
  - **Root cause:** `_reject_untracked_workspace` only recognises labels carrying the `[herdr-ml:workspace:<32hex>]` marker. The old bootstrap created workspaces with plain labels and recorded them in `workspaces.tsv`. This module never reads that file.
  - **Scenario:** A user who ran the old bootstrap runs the new reconciler. It creates a second workspace for the same checkout.
  - **Fix:** Add a migration or adoption path, or block when any workspace's cwd equals the checkout.
- **L2 — HIGH if integrated: the meaning of `cwd` is unspecified.**
  - **Root cause:** If the adapter reports a pane's live cwd rather than the cwd it was created with, the checks break as soon as a user runs `cd`:
    - `_one` blocks with "different checkout".
    - `_reject_untracked_workspace` can wrongly attribute another project's checkout to this workspace and block that project.
  - **Fix:** The adapter contract must state which cwd it returns.
- **L3 — MEDIUM: missing tabs and missing workspaces are handled inconsistently.**
  - A missing workspace is BLOCKED. A recorded tab that is missing is silently recreated with the same deterministic label.
  - **Scenario:** `tabs()` returns a transient partial listing. A duplicate tab is created, and every later run blocks with "Multiple objects".
- **L4 — MEDIUM: a server restart has no recovery path.** Any change of identity blocks permanently. Deleting the state file does not help if Herdr restores the old workspaces, because `_reject_untracked_workspace` then blocks again. Routine reboots therefore need an adoption procedure that does not exist yet.
- **L5 — LOW: the identity check does not protect lazy listings.** The contract allows any iterable. In `_complete_intent`, `adapter.workspaces()` is followed by `_check_identity`; if it returns a generator, the real fetch happens during `_one`, after the second identity check, so the identity "sandwich" protects nothing. Materialise with `list()` before the post-check.
- **L6 — LOW:** A malformed state file (non-dict `tabs`, missing `token`) raises `AttributeError` or `KeyError` instead of `ReconcileBlocked`.

### `bootstrap-herdr.sh`
The hold is effective: `exit 2` runs before any side effect. The dead code below it still has the old unsafe pattern: no marker, a non-atomic TSV, and no intent journal. Delete it rather than relying on the hold staying in place.

---

## Remaining prerequisite gaps (not bugs at this commit, since they are blocked or documented)
1. **No real containment.** There is no cgroup, systemd scope, or other kill-all primitive, so descendants that call `setsid` can outlive the run while holding the GPU and log fds. There is also no wall-clock timeout.
2. **No authenticated approval or trusted execution boundary for official runs.** `provenance.py` is not bound to the process that actually executes.
3. **No safe queued execution.** The daemon's interpreter and environment are not validated, and there is no containment.
4. **No operator procedure** for resolving stranded claims or `UNKNOWN` runs.
5. **No live Herdr adapter.** Server identity, the cwd semantics, and migration from legacy workspaces are all undefined.
6. **Gate files are self-attested.** Evidence is checked for existence only, not content.
