# Adversarial review of `c1c3d897eadd26ad0cc7374e17090b0c3fdcdc28`

This is a static review of the attached text only. I ran no tests, opened no files, and executed nothing. Nothing below shows that the tests pass or that any behavior works on a live system.

## Verdict

I found **no unconditional CRITICAL or HIGH bug** in the attached code. Most boundaries fail closed: they raise or return BLOCKED/UNKNOWN rather than proceeding. The serious risks are:

- **One conditional HIGH** in `process_identity.py`. It only matters if a consumer uses its result to authorize a retry, and no consumer is attached.
- **Several MEDIUM issues** where a guarantee is weaker than the docstrings or the names suggest.

## Unsupported integrations

None of the following has a working integration in this commit:

- **Herdr reconciliation:** `herdr_reconcile.py` has no adapter and no CLI. Every safety property depends on an adapter that does not exist yet. The gates `coordination_verified()`, `creation_cwd_verified()` and `atomic_parent_guard_verified()` only check what the adapter reports about itself. An adapter that returns `True` passes all three. Read them as the adapter's promises, not as verification.
- **Containment:** `containment_probe.py` always returns `BLOCKED` with exit code 2. It provides no containment.
- **Process liveness and termination:** `process_identity.py` only observes. Nothing attached consumes its result.
- **Artifact integrity:** `artifact_tree.py` produces point-in-time digests with no authentication and no binding to what a job actually reads, as its docstring says.

## Conditional HIGH

### P1. `process_identity.observe_process` can report `IDENTITY_MISMATCH` or `NOT_VISIBLE` for a process that is still running

This only matters if a consumer treats either result as "the original process is gone, safe to retry." In that case the job could run twice, for example two jobs on the same GPU.

**Cause 1: inputs are compared as strings without checking their format.**
- `boot` is never checked against the canonical lowercase UUID form. A recorded value with a trailing `\n`, uppercase letters or no dashes returns `IDENTITY_MISMATCH 'Boot identity differs'`.
- `start_ticks` is compared as a string, so `"0123"` and `"123"` count as different.
- Both cases should return `UNKNOWN`, not a mismatch. The fix is to require exact canonical forms (`^[0-9a-f]{8}-…$`, and no leading zeros unless the value is `"0"`) before comparing.

**Cause 2: the identity does not include the PID namespace.**
- Suppose the recorder ran in a container and the observer runs on the host (or the reverse), and both share the same hostname, as with a shared UTS namespace. The boot ID matches, but the PID refers to a different process in each namespace.
- The result is a false `IDENTITY_MISMATCH` or a false `NOT_VISIBLE`.
- `hidepid=` mounts produce a false `NOT_VISIBLE` in the same way.
- The fix is to record `/proc/self/ns/pid` (device and inode) at recording time and return `UNKNOWN` when it differs at observation time.

The docstring already says that `NOT_VISIBLE` is not proof that the process ended. `IDENTITY_MISMATCH` carries no such warning, and its name reads like a definite answer. Any consumer should treat everything other than `MATCH` or `MATCH_ZOMBIE` as non-authorizing.

## MEDIUM

### A1. `artifact_tree.snapshot` can loop forever on a growing file
The read loop `while True: os.read(...)` runs until it reaches end of file, and the identity check happens only after the loop. If a writer keeps appending at least as fast as the hash reads (for example a training log inside the tree), the loop never ends.

Fix: read at most `opened.st_size` bytes. Then do one more 1-byte read and require that it returns empty, and raise `'file changed during read'` otherwise.

### A2. Change detection depends on coarse timestamps
`_identity` compares size, mtime and ctime. Linux sets file timestamps at the kernel's coarse clock tick. A same-size rewrite inside one tick between the first and last `fstat` can therefore produce a digest of mixed old and new content without raising an error.

The docstring disclaims concurrent writers, so this is a documented limit rather than a code bug. Nothing that consumes the manifest should treat it as an integrity proof.

### R1. `herdr_reconcile._canonical` accepts relative and `~` paths from the adapter
`Path(cwd).expanduser().resolve(strict=True)` resolves a relative cwd such as `"."` against **the reconciler's own working directory**. It also expands `~user` locally.

- In `_reject_untracked_workspace` this produces a spurious block, which is conservative and acceptable.
- In `_one` and `_verify_parent_workspace` it can make a marker match pass the checkout comparison when it should fail.

Fix: require `os.path.isabs(cwd)` and reject a leading `~` before resolving.

### R2. The global lock has no timeout or liveness bound
`_locked` uses a blocking `flock`. If an adapter call hangs while the user-wide global lock is held, reconciliation stops for every project under that OS account. The adapter contract rules out timeout-only locks for the guard, but there is still no watchdog or operator-visible timeout around adapter calls.

## LOW and hardening

**`artifact_tree.main`**
- Uncaught exceptions such as `MemoryError` exit with code 1, which is the same code as "Artifact tree differs." Both codes are non-zero, so a gate that requires 0 still fails closed. A caller that branches on 1 versus 2 will still misclassify the crash. Wrap the body in a catch-all that returns 2.

**`artifact_tree.read_manifest`**
- It opens the path with `O_RDONLY` before checking the file type, so a device node is actually opened, and opening some devices has side effects. Opening with `O_PATH` first, as `snapshot` already does, avoids this.

**`artifact_tree.snapshot`**
- It crosses mount points without checking `st_dev`.
- It has no time or size bound on hashing large or sparse files, or files on FUSE or network filesystems.

**`herdr_reconcile` state file**
- It is parsed with plain `json.loads`: duplicate keys and NaN are accepted, and the schema is not validated. A malformed field raises `KeyError` or `AttributeError` rather than `ReconcileBlocked`. Every such path I traced raises before any mutation, so it fails closed, but the error is less clear.

**`herdr_reconcile` file handling**
- Neither the state file nor the lock file is checked for being a regular file owned by the current user. It is safe only if `state_path` lives in a private directory.
- `_write` fsyncs the parent directory, but not the grandparent when `mkdir(parents=True)` has just created directories. After a power loss the whole state directory could disappear. `_reject_untracked_workspace` then blocks correctly, so this affects only liveness.

**`herdr_reconcile` tab recreation**
- A tab with a recorded ID that is missing from `tabs(workspace_id)` is silently recreated, whereas a missing workspace blocks.
- If the adapter returns an incomplete listing, this creates duplicate markers. Those then block permanently in `_one`.
- Decide whether recreating tabs is intended and document it.

**`containment_probe._unescape`**
- It converts octal escapes to individual code points with `chr`, so non-ASCII mount paths are decoded incorrectly. This only affects a hint field, and the verdict stays `BLOCKED`.

## What I checked and found sound

### `artifact_tree.snapshot`
- **Symlink and FIFO swap races.** Every swap I traced raises an error: stat with `O_NOFOLLOW`, then pin the inode with `O_PATH`, check `S_ISREG` and identity, reopen through `/proc/self/fd`, and check identity again.
- **Directory checks.** Opening subdirectories with `O_DIRECTORY|O_NOFOLLOW` and comparing dev/inode works as intended.
- **Size budget.** The per-record budget of 32 bytes covers the extra indentation for nested entries. Any manifest that `snapshot` emits therefore fits within `read_manifest`'s limit.
- **Non-UTF-8 filenames.** Names decoded with surrogate escapes round-trip correctly through JSON with `ensure_ascii`.

### `artifact_tree` manifest parsing
- Duplicate keys, NaN, bool-typed integers, BOMs, oversized integers and deep nesting are all rejected, with exit code 2.

### `herdr_reconcile`
- **Crash recovery.** The intent is written and fsynced before every create. If the outcome is uncertain, recovery blocks and never retries.
- **Lost state file.** If the state file is lost while the workspace survives, the run blocks.
- **Marker labels.** Labels are injective: the suffix has a fixed length and the label includes the name.
- **Guard exceptions.** `_parent_guard` re-raises even when a buggy guard's `__exit__` suppresses an exception. I traced this through the logic of `contextlib.contextmanager`.
- **Server changes.** If the server's identity changes, every later run blocks until an operator resolves it.

### `containment_probe`
- It is read-only, always reports `BLOCKED`, and discards the output of `systemctl`.

## Test gaps in the attachment

- No tests are attached for `herdr_reconcile`, `process_identity` or `containment_probe`.
- No test covers concurrent modification, a growing file (A1), the `/proc/self/fd` reopen, or a non-canonical boot ID or start time (P1).

Nothing in this commit, and nothing in this review, establishes scientific validity or a working end-to-end integration.
