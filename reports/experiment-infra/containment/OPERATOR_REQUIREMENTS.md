# Operator requirements — verified containment for experiment workloads

Purpose: the minimum host provisioning needed before CONTAINMENT_READY can move beyond BLOCKED. Nothing here is requested as an approval to run workloads. It describes what an administrator would need to provide and how it would be verified.

## Observed host (2026-11-04, read-only)

- Ubuntu 20.04.6, kernel 5.4.0-216, systemd 245.
- Hybrid cgroup hierarchy: cgroup2 at `/sys/fs/cgroup/unified`, v1 controllers elsewhere.
- Current process boundary: `/sys/fs/cgroup/unified/system.slice/teleport.service`, owned by root, not writable by the worker user.
- `cgroup.kill` is not available on this kernel/systemd combination.
- User systemd bus is not reachable for the worker user.

## Minimum capability

Either of:

1. A cgroup v2 subtree delegated to the worker user, with `cgroup.procs`, `cgroup.controllers` and `cgroup.events` writable within it, and a boundary created per run attempt.
2. A site-supported systemd service or scope template, started by the worker user through a reachable user bus, that places workloads in a dedicated unit and reports unit membership.

`cgroup.kill` is preferred but not required if the implementation can verify emptiness by reading `cgroup.procs` until empty.

## Least-privilege boundary

- The worker user must not gain sudo, root, or write access to system-owned cgroups.
- Delegation must cover only the experiment subtree, not production agent or service slices.
- No change to production Claude hooks, credentials, or Herdr server state.

## Verification procedure (to be run by the operator)

1. Create a disposable child cgroup under the delegated subtree.
2. Start a synthetic process tree (parent exits, child sleeps, grandchild ignores SIGTERM) inside it.
3. Confirm membership via `cgroup.procs` lists every descendant.
4. Terminate the entire tree through the delegated mechanism; confirm `cgroup.procs` becomes empty within a bounded time.
5. Remove the child cgroup; confirm no leftover processes outside the subtree.
6. Record the commands, outputs, and exit codes.

## Expected process-tree behavior

- Descendants that double-fork or setsid must remain in the boundary.
- A process that escapes the boundary (for example by writing to another cgroup) is treated as containment failure, not as success.

## Evidence needed to enable a production backend

- The verification transcript above, run on the actual worker host.
- The delegation record (who delegated what, and the exact subtree path).
- Confirmation that the verification ran as the worker user without privilege escalation.
- A signed or otherwise independently recorded statement of the delegation, from an administrator other than the worker.

## Current status

Not provided. CONTAINMENT_READY remains BLOCKED. The repository's containment backend continues to return UNAVAILABLE.
