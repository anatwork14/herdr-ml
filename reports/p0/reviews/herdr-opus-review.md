# Adversarial review: `herdr` reconciliation core @ `1376f400`

I reviewed only the source included in the request; nothing was executed. I found no CRITICAL or HIGH defects. The core never deletes, renames or focuses anything, and every uncertain path I traced ends BLOCKED rather than retrying.

I found two MEDIUM defects and several LOW issues. I make no claim about scientific validity. The holds on official execution, queued execution and bootstrap remain appropriate.

## Known missing integrations (not new defects)

These are already documented in the module docstring and `HERDR_RECONCILIATION.md`:

- There is no live adapter. Server-incarnation identity, atomic per-operation identity pinning, and an authoritative, complete inventory are all unproven.
- The identity checks across multiple calls cannot detect a server swap within a single adapter call.
- Whether the `cwd` reported by real Herdr is static or follows the shell is unknown. If it follows the shell, then after the user runs `cd`, `_one` raises "different checkout" and the project stays BLOCKED until someone intervenes. That fails closed, but it would make the tool unusable, so it must be settled during adapter work.
- Label fidelity is unknown. If Herdr truncates or sanitizes labels, marker matching fails closed.
- Pane and watcher reconciliation is not implemented.

---

## MEDIUM-1: Losing or recreating the state file silently duplicates the managed layout

**Root cause.** When `path.exists()` is false, `reconcile` mints a fresh `uuid4` token. Ownership markers are scoped to that token, so the guard "Unrecorded workspace without matching intent" only looks at the new token's marker. A workspace and tabs created under an earlier token are invisible to it. Nothing searches for existing `[herdr-ml:workspace:*]` objects with the same label and checkout before creating new ones.

The docs say deleting state is unsafe, but the code does nothing to stop the consequence. State can go missing without anyone deliberately deleting it, for example:

- the state directory is on tmpfs or was cleaned up,
- the state path is configured differently,
- a home directory is migrated, or
- an operator follows the "investigate" advice and removes the file.

**Reproduction.**
```python
r1 = run_once()
self.path.unlink()
r2 = run_once()   # succeeds
assert len(api.ws) == 2            # duplicate workspace, old one orphaned
assert api.calls == ['workspace','tab','tab','tab'] * 2
```

**Fix.** On the fresh-state path, before the first `_write`, list the workspaces. Raise `ReconcileBlocked` if any workspace label matches `marker('workspace', <any token>, label)` (use a regex on the `[herdr-ml:workspace:<hex32>]` suffix), or if any marked workspace's canonical cwd equals the checkout.

**Acceptance test.** Run once, delete the state file, run again. Expect `ReconcileBlocked`, zero new create calls, and no state file left behind. Do not leave a half-initialized state file, so that the block is not silently converted into adoption on a later run.

## MEDIUM-2: `ProcessCrashTests` only works with the `fork` start method

**Root cause.** The `multiprocessing.Process` target and its arguments must be picklable under the `spawn` or `forkserver` start methods. Two things here are not:

- `SharedFake` is a class defined locally inside the test method, so it cannot be pickled.
- `m.reconcile` comes from a module loaded with `spec_from_file_location('herdr_reconcile', ...)` that is never inserted into `sys.modules`, so pickling it by reference fails.

`spawn` is the default on macOS. Python 3.14 changes the default on Linux to `forkserver`. Under either, the test errors with a `PicklingError` or `AttributeError` before any crash is simulated. Depending on the runner, that shows up as an error, or someone "fixes" it by skipping the test, and the only real crash-after-mutation coverage disappears.

Commit `24b703b` pins a development Python version. I could not see which one, so whether this already bites depends on that pin.

**Reproduction.** Call `multiprocessing.set_start_method('spawn', force=True)` before the test, or run it on Python 3.14 on Linux.

**Fix.** Pick one:
- Use `multiprocessing.get_context('fork')` explicitly and skip on platforms without `fork`, with the skip stated loudly.
- Move `SharedFake` to module level and register the loaded module in `sys.modules['herdr_reconcile']` before `exec_module`.

**Acceptance test.** Under each of `fork`, `forkserver` and `spawn`, the test either passes or is an explicit, reported skip. It must never error.

---

## LOW / hardening

1. **Lock and state path are not canonicalized.** `Path(state_path)` is used as given.
   - Two different spellings of the same file (a symlink, or a bind-mount alias) get different `.lock` files, so there is no mutual exclusion between them.
   - `os.replace` on a symlinked path replaces the symlink with a regular file, which splits the state in two.
   - The docs require a single canonical path, but the code does not enforce it. Resolving the parent directory, and refusing if the state path itself is a symlink, would enforce it.
   - *Test:* point a symlink at the state file, then show that the run refuses, or that both paths use one lock.

2. **Loaded state is not schema-validated.** Only `version`, `server`, `project`, `checkout` and `label` are checked. If `tabs` is not a dict, `pending` is the wrong shape, `token` is not 32-character hex, or `workspace_id` is not a string or `None`, the run fails with a raw `KeyError`, `AttributeError` or `TypeError` instead of `ReconcileBlocked`. The worst case: `pending.kind == 'tab'` with `key: None` adopts the tab and then crashes in `json.dump(sort_keys=True)`. This stays inside the declared trust boundary, but callers will treat these as generic errors.
   - *Test:* feed each malformed field and expect `ReconcileBlocked` with no create calls.

3. **Permissions are not enforced.**
   - `mkdir(mode=0o700, exist_ok=True)` does nothing to a directory that already exists.
   - Intermediate directories created by `parents=True` get the umask default.
   - The lock file is created as `0o666 & ~umask`.
   - Nothing verifies the directory's owner or mode, even though the docs make that directory the trust boundary.
   - Fix: check `st_uid == os.getuid()` and that there are no group/other write bits, or block.

4. **Pending tab intent is resolved before the workspace is verified.** `_complete_intent` lists `tabs(record['workspace_id'])` before confirming that this workspace ID still carries the marker and checkout. The later workspace check does block, but by then the adoption has already been persisted. This fails closed. The fix is to verify the workspace first.

5. **The intent is kept even when dispatch definitely did not happen.** If the second `_check_identity` in `_intent_then_create` fails, `create_*` was never called. The intent stays anyway, and the project is permanently BLOCKED until an operator intervenes. That is conservative and consistent with the docs. If it should be relaxed, only an in-process, pre-dispatch failure should clear the intent, and never after a crash.

6. **A managed tab moved to another workspace is recreated as a duplicate.** The inventory only covers the recorded workspace. If such moves are possible in Herdr, the adapter contract needs either a global tab inventory or a documented scope for tab IDs.

7. **Stale entries in the return value.** `tabs` from earlier configurations stay in `record['tabs']`, are returned to the caller, and are never re-verified. Filter the return value to the requested `tabs`.

8. **The `.git` check is weak.** It only checks that the path exists, and it runs outside the lock. This is cosmetic, given the documented scope.

## Test gaps

Beyond MEDIUM-1 and MEDIUM-2, there are no tests for:

- lock contention between two concurrent processes,
- a symlinked or aliased state path,
- malformed state fields (as opposed to unparseable JSON),
- an identity change between create and `_complete_intent`, followed by resuming under the original identity,
- a pending tab intent whose workspace has since disappeared (the adapter's error contract is unspecified),
- state rollback to an older valid snapshot. My trace says tabs block correctly here; the test should confirm it.

## Bottom line

There is no CRITICAL or HIGH defect in this commit. The intent, marker and adoption logic is sound for the threat model the docs describe.

MEDIUM-1 is the one real fail-open gap in the core: losing the state file causes silent duplication instead of a block. Fix it before any adapter work. MEDIUM-2 can quietly remove the main crash-safety test, depending on which Python version is pinned. Keep the holds in place.
