# Follow-up adversarial review: `herdr` reconciliation core @ `7c1450f1`

**Scope:** I reviewed only the source, tests and docs included in the request (`scripts/herdr_reconcile.py`, `tests/test_herdr_reconcile.py`, `docs/HERDR_RECONCILIATION.md`).

- **Nothing was executed.** No tests were run and I did not run git commands.
- **Not checked against the commit.** I did not compare the included text with the repository object for `7c1450f`.
- **Other changes in this branch were not reviewed.** Several recent commit messages refer to queue holds, mixed-version claims and cancellation. Those files were not included.
- **No claims beyond the code.** I make no claim about live Herdr behaviour or scientific correctness.

**Verdict:** I found no CRITICAL or HIGH defects. Both prior MEDIUM findings are **fixed**. There is one new MEDIUM, which is an availability defect that fails closed. The prior LOW items are mostly still open. **The holds remain appropriate.**

---

## Status of prior findings

| ID | Finding | Status | Evidence |
|---|---|---|---|
| MEDIUM-1 | Lost state file silently duplicates the layout | **FIXED** | See below |
| MEDIUM-2 | `ProcessCrashTests` only works under `fork` | **FIXED** | See below |
| LOW-1 | State and lock path not canonicalized | OPEN | `path = Path(state_path)` is unchanged. There is no symlink refusal. `os.replace` still replaces a symlink with a regular file. A dangling symlink now hits the MEDIUM-1 guard, which partially mitigates this. |
| LOW-2 | Loaded state not schema-validated | OPEN | Only `version`, `server`, `project`, `checkout` and `label` are checked. A malformed `pending`, `tabs`, `token` or `workspace_id` still raises a raw `TypeError`, `KeyError` or `AttributeError`. A `pending` of kind tab with `key: None` still adopts the tab and then fails in `json.dump(sort_keys=True)`. |
| LOW-3 | Directory and lock permissions not enforced | OPEN | `mkdir(mode=0o700, exist_ok=True)` and `open('a')` are unchanged. There is no owner or mode check. |
| LOW-4 | Pending tab intent resolved before the workspace is verified | OPEN | `_complete_intent` still calls `adapter.tabs(record['workspace_id'])` and persists the adoption before the `observed != record['workspace_id']` check. This still fails closed. |
| LOW-5 | Intent kept when dispatch definitely did not happen | ACCEPTED (by design) | Consistent with the docs, which say crash-before-dispatch is BLOCKED. |
| LOW-6 | A managed tab moved to another workspace is recreated | OPEN | The inventory is still scoped to `record['workspace_id']`. |
| LOW-7 | Stale tab entries in the return value | OPEN | It still returns `dict(record['tabs'])`, which is not filtered to the requested `tabs`. |
| LOW-8 | Weak `.git` check, run outside the lock | OPEN (cosmetic) | Unchanged. |

### MEDIUM-1: fixed

- **Fresh-state path.** It now lists workspaces, re-checks identity, then calls `_reject_untracked_workspace`, all before the first `_write`. A block therefore leaves no state file behind, so it cannot later turn into adoption.
- **`_reject_untracked_workspace`.** It blocks on any `[herdr-ml:workspace:<hex32>]` workspace whose base label matches or whose canonical cwd matches.
- **Second call site.** The same check is now also run when `workspace_id is None`. This covers a forged or rolled-back state with a different `token`, and is tested by `test_initial_state_without_workspace_detects_other_managed_layout`.
- **Acceptance test.** `test_missing_state_does_not_create_duplicate` matches the requested test. It checks for `ReconcileBlocked`, an unchanged `calls` list, no state file, and exactly one workspace.
- **Rollback trace.** Rolling back to the very first snapshot (`workspace_id=None`, `pending=None`, same token) now blocks via `_reject_untracked_workspace`, because the workspace's own marker matches the regex with the same label.
- **Residual:** see LOW-N1 below.

### MEDIUM-2: fixed

- **Fork context.** `ctx = multiprocessing.get_context('fork')` is used for both `Process` and `Manager`. Under fork, the target and its arguments are not pickled, so the locally defined `SharedFake` and the module that is missing from `sys.modules` no longer matter.
- **Skip path.** There is an explicit `skipTest(...)` when `fork` is unavailable. This is reported under `-v`.
- **Platform residual (not a defect).** On macOS, `fork` is listed as available but is unsafe once certain system frameworks are loaded. Python 3.12+ also warns if the process is multi-threaded when it forks. That warning is harmless unless the runner uses `-W error`.

---

## New findings

### MEDIUM-3: Any stale managed workspace with an unresolvable cwd blocks all new-project bootstrap

**Where:** `_reject_untracked_workspace`, `scripts/herdr_reconcile.py`, the `try: existing_checkout = _canonical(obj['cwd'])` block.

**Root cause.** The function runs `_canonical(obj['cwd'])`, which calls `resolve(strict=True)`. It does this for **every** managed workspace in the inventory, before it checks whether that workspace's label matches. If any managed workspace belonging to *any* project has a cwd that no longer exists, the function raises `ReconcileBlocked('Managed workspace has unverifiable checkout')`. Examples include:

- a removed git worktree,
- an unmounted volume,
- a renamed directory.

This happens on:

- every fresh-state run, for every project, and
- every run where `workspace_id is None`.

**Impact.** This fails closed, so it is not a safety defect. However, removing an old worktree is routine, and doing so permanently blocks onboarding of unrelated projects. The error does not identify the offending workspace.

The likely operator workaround is to rename the stale workspace and strip its marker. That makes the workspace invisible to every future guard, so the workaround erodes the protection MEDIUM-1 added. That is why this is rated MEDIUM rather than LOW.

**Fix.**

1. If the base label equals `label`, block unconditionally. This is the current behaviour and is correct.
2. Otherwise, only an *unresolvable* cwd should block. Check it with a non-strict comparison: if the cwd as reported (after `normpath`, without `strict=True`) equals the checkout, block.
3. If the cwd cannot be resolved and the label differs, the workspace belongs to another project. Either ignore it, or block with a message that includes the workspace ID and label.
4. Whichever way it goes, include the workspace ID in the exception message.

Note that a cwd which no longer exists cannot be the current checkout, because `checkout` itself was canonicalized with `strict=True`. Ignoring the unresolvable, non-matching case does not open a duplication path for *this* checkout.

**Acceptance test.**

- Create a managed workspace for project A whose cwd is then deleted.
- A fresh run for project B (different label, existing checkout) succeeds.
- A fresh run for project A's label still blocks.

### LOW-N1: The state-loss guard depends on the marker surviving on the workspace label

If the managed workspace was renamed so that it no longer carries the marker, and the state is then lost, nothing detects the old layout. A new workspace and tabs are created, and the old tabs, which still carry markers, are orphaned.

The same applies if the project `label` is changed and the workspace's cwd has also drifted. This could happen if Herdr's cwd follows the shell, which is still unknown.

This needs a user rename and state loss together, so it is LOW. Optional hardening: during fresh-state discovery, also scan tabs for `[herdr-ml:tab:<hex32>-<hex16>]` markers. This requires a complete tab inventory from the adapter.

### LOW-N2: A relative cwd is resolved against the caller's working directory

`_canonical` calls `Path(...).resolve()`. If the adapter ever returns a relative `cwd`, the result depends on `os.getcwd()` at reconcile time. The docs already require canonical cwd from the adapter. Enforcing this cheaply in the core, by blocking when `not os.path.isabs(obj['cwd'])`, would turn a doc contract into a code check. This applies to both `_one` and `_reject_untracked_workspace`.

### LOW-N3: Non-dict inventory items raise raw errors

`obj.get(...)` in `_one`, `_reject_untracked_workspace` and the tab-reuse check raises `AttributeError` if the adapter yields non-dict items. This is within the adapter contract, but it should become `ReconcileBlocked` once an adapter exists.

### Test hygiene (LOW)

`process.join(10)` followed by `assertEqual(process.exitcode, 71)` leaves the child running if it hangs. Add `process.kill()` and `process.join()` in a `finally` block.

---

## Remaining test gaps

These are untested:

- The fresh-state guard's **checkout-match** branch: a managed workspace with a different label at the same checkout.
- The guard's **unverifiable-cwd** branch, including the MEDIUM-3 scenario.
- An identity change during fresh-state discovery (`'Server/session identity changed during discovery'`).
- Malformed state fields, as opposed to unparseable JSON (LOW-2).
- Symlinked or aliased state paths, and lock contention between two processes (LOW-1).
- Rollback to an intermediate snapshot, for example one where the workspace is recorded but no tabs are. My trace says it blocks via `'Unrecorded or replaced tab marker'`, but no test confirms this.
- A pending tab intent whose workspace has disappeared. The adapter's error contract for this is still unspecified.

## Documentation

`HERDR_RECONCILIATION.md` now describes the state-loss guard accurately ("same label or canonical checkout blocks"). It does not mention that *any* managed workspace with an unverifiable cwd also blocks. Update it alongside MEDIUM-3.

## Bottom line

- **Prior MEDIUMs.** MEDIUM-1 and MEDIUM-2 are fixed, and the fixes match the requested acceptance criteria.
- **Severity.** There is no CRITICAL or HIGH defect, and every path I traced fails closed.
- **New MEDIUM-3.** An availability bug that could push operators toward a workaround which weakens the state-loss guard. Fix it before adapter work.
- **Prior LOWs.** LOW-1 through LOW-4, LOW-6 and LOW-7 are still open. LOW-1 (path canonicalization) and LOW-2 (schema validation) are the ones worth fixing before a live adapter exists.
- **Holds.** Keep the holds on bootstrap, official execution and queued execution in place.
