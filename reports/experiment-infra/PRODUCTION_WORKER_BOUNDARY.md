# Production worker execution boundary (P7.2)

Status: OFFLINE_TESTED. Not a security certification. Production execution remains
refused: no containment backend is verified, so no launch can happen on a production
host. `researchctl.py` is unchanged and its guards are unchanged.

Module: `scripts/execution_boundary.py`. Tests: `tests/test_execution_boundary.py`.

## Reachability

- Not imported by any other script. A test greps every `scripts/*.py` for the name.
- No command-line flag, environment variable or config file selects it. The module has
  no argument parser and reads no process settings. Its only inputs are the arguments of
  `launch()`.
- The approval gate, commit and containment backend are passed programmatically. Production
  defaults are fail-closed.

## Check order

Checks run in this order. Each refusal raises `BoundaryRefusal(code, check)`. A refused
request does not change the run row, does not consume an approval and does not spawn a
process. Nothing advances the run row before check 5 succeeds.

| # | Reason code | What is checked |
|---|---|---|
| 1 | `IDENTITY_MISMATCH` | Read-only read of `registry.sqlite3`. The request `run_id` and `attempt_id` must equal the row. The run folder must be named after `run_id`. A missing or unreadable registry is also refused here. |
| 2 | `FENCE_STALE` | The row must recompute as CURRENT (`run_ownership.classify_row`). The presented token must decode, and must equal the durable `owner`, `attempt_id` and `fence`. The row state must be `CLAIMED`. |
| 3 | `QUEUE_NOT_ENABLED` | Any run with `queue_task_id` set is refused. Queue identity is not evaluated, because the queued path is disabled (QUEUE_READY is BLOCKED). |
| 4 | `MANIFEST_MISMATCH` | `execution_manifest.verify_before_execution` with the digest taken from the registry row, not from the request. This covers the manifest digest, run and attempt binding, source commit and file digests, input digests, interpreter and runtime. The command `cwd` must stay inside the verified source root. |
| 4 (see 7) | `OUTPUT_DESTINATION_INVALID` | Output failures raised inside check 4's manifest call (symlink, escape, existing target) are reported with code `OUTPUT_DESTINATION_INVALID` and check number 7. |
| 5 | `APPROVAL_UNAVAILABLE` | `approval_gate(run_id, attempt_id, manifest_digest)` must return `ApprovalDecision.GRANTED`. Exceptions and any other value refuse. The default is `fail_closed_approval_gate`, which always returns `UNAVAILABLE`. This check does not consume anything. |
| 6 | `CONTAINMENT_UNAVAILABLE` | `containment.establish(argv, run_id=...)` must return a result that `containment_backend.is_verified_for_execution` accepts. Exceptions refuse. The default is `containment_backend.get_backend()`, which is UNAVAILABLE in production. |
| 7 | `OUTPUT_DESTINATION_INVALID` | The output path must lie inside the run folder. No existing component may be a symlink or non-directory. The run folder and the nearest existing directories must be owned by the effective uid and must not be group or world writable. The target must not exist. |
| 8 (post) | `LAUNCH_FAILED` | Spawn failed after all checks passed. The row is finished as UNKNOWN. |

After check 7, `commit_approval` runs once. It is the only consumption point. The default
`fail_closed_approval_commit` returns False, so there is no launch. Any exception or any
value other than `True` refuses with `APPROVAL_UNAVAILABLE` and no process starts.

### Launch sequence (only after all checks and the commit)

1. Row `CLAIMED` to `DISPATCHING` (`run_registry.advance`, CAS on owner, attempt and fence).
2. `subprocess.Popen` of the manifest's `command.argv`, with `cwd` inside the verified source
   root, a minimal environment, stdin closed, `start_new_session=True`.
3. `run_registry.record_child`, then `DISPATCHING` to `RUNNING`.
4. Wait with `timeout_s`. On timeout, `SIGKILL` to the process group this boundary created.
5. Write stdout to the output path with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600. This is the
   first write to the output, and it happens after the launch.
6. `run_registry.finish` with state `UNKNOWN`. Containment is not verified on this path, so the
   result is never `COMPLETED`.

## Interfaces other workers must provide

**W3 (approval owner)** supplies two callables, both passed to `launch()`:

- `approval_gate(run_id, attempt_id, manifest_digest) -> ApprovalDecision`. Non-consuming.
  It answers whether a run-specific grant exists for this manifest digest.
- `approval_commit(run_id, attempt_id, manifest_digest) -> bool`. The single-use claim. It
  must return `True` only if it atomically consumed the grant. It runs only after checks 1
  to 7 pass and immediately before spawn. A repeat call must return False.

**W2 (containment)** must supply a backend whose `establish(argv, *, run_id)` returns a
`ContainmentResult` that `is_verified_for_execution` accepts. That needs a registered issuer
in `containment_backend._VERIFIED_ISSUERS`, which is currently empty. W2 must also make the
spawn happen inside that containment. The current `_spawn_and_collect` launches the
process directly. That is acceptable only because production refuses at check 6. Before any
production use, the spawn must go through the verified containment.

**Registry** (`run_registry.py`, unchanged): the row must be `CLAIMED` with an owner token
that matches the request.

## Verified (offline, CPU, this host)

- 25 tests in `tests/test_execution_boundary.py`. Each one has a 120 s SIGALRM watchdog. They
  start only processes that the test created and kill those process groups in cleanup.
- Direct invocation with production defaults launches nothing (marker file absent, output
  absent, `pgrep -f` on the per-test marker empty). The run stays `CLAIMED`.
- Each check refuses with its own code: identity (mismatched run, mismatched attempt, missing
  registry), stale fence (fence bumped, forged token), queued run, mutated manifest bytes,
  mutated source file, mutated dataset input, authorizer fault, authorizer DENIED, unverified
  containment, symlinked output target, symlinked output directory, group-writable output
  directory, pre-existing output, missing or failed commit.
- Refusals at checks 5 to 7 leave the run in `CLAIMED`, and the commit callable is not called
  for check 6 or 7 refusals.
- Positive path with a test-only containment double and a test-only issuer patched into
  `containment_backend._VERIFIED_ISSUERS`. Checks pass, the commit is called once, the child
  runs, the output is written with mode 0600 after launch, and the row ends `UNKNOWN`. A
  second launch with the same token is refused as `FENCE_STALE`.
- The module does not import `tests/support` or the containment simulator. It is not imported
  by any other script.
- Full suite: 478 tests before the commit split, and the commit split was checked with the
  boundary tests. P0 test files: no assert or `def test_` line changed.

## Not verified (explicit)

- No production containment exists. Check 6 always refuses on a production host, so the
  launch path has never run under real containment. Escape from the process tree is not
  tested.
- The approval gate and commit are test doubles. No real single-use claim, replay protection
  or revocation is verified here.
- Check 4 reads the source and inputs and then spawns. A file replaced between the digest
  check and the spawn is not detected (time-of-check to time-of-use). The manifest module's own
  documentation states this limit.
- Owner-only permission checks use `st_uid == geteuid()` and mode bits only. ACLs, extended
  attributes, mount options and other users with `CAP_DAC_OVERRIDE` are not considered.
- Queue identity (trusted queue instance) is not evaluated, because queued runs are refused.
- `fail_closed_approval_gate` returns UNAVAILABLE for every case, including DENIED, so the
  reason code does not distinguish a denial from an unavailable authorizer.
- Descendant processes that outlive the group leader and escape the process group are not
  observed. The outcome is always `UNKNOWN`.
- Run on Linux only, one host, one Python (3.12). No multi-user or concurrent-launch test.
