# Operator requirements — verified containment for experiment workloads

Purpose: the minimum host provisioning needed before CONTAINMENT_READY can move beyond BLOCKED. Nothing here is a request to run workloads. It describes what an administrator would provide, how the worker would verify it, and what evidence a production verifier would need. The behavioural contract is in `reports/experiment-infra/CONTAINMENT.md`.

## Observed host (2026-11-04, read-only)

Re-observed with read-only commands. Raw output is in `reports/experiment-infra/w2-logs/host-observation-readonly.log`.

- Kernel `5.4.0-216-generic`. systemd `245.4-4ubuntu3.24`.
- `cgroup2` is listed in `/proc/filesystems`. The process is in a hybrid layout. `/proc/self/cgroup` contains `0::/system.slice/teleport.service`, and v1 controllers are mounted separately.
- The v2 directory `/sys/fs/cgroup/unified/system.slice/teleport.service` is owned by `root:root`, mode `755`. It is a system service's boundary, not a worker-owned subtree. Do not use it or change it.
- Its control files are `cgroup.procs` (`rw-r--r--`, root-owned), `cgroup.events` (`r--r--r--`, kernel-maintained, reports `populated` and `frozen`), and `cgroup.freeze` (root-owned). There is no `cgroup.kill`. The file was added upstream in Linux 5.14, and this kernel predates it.
- `/run/user/5011/bus` does not exist. The user systemd bus is unreachable.
- `scripts/containment_probe.py` reports `BLOCKED` and exits 2 on this host, as designed.

## Minimum capability

The worker needs a boundary it controls, with the following properties. The implementation must check each one on the delegated host. The current host provides none of them.

Either of the two options below is acceptable.

1. **Direct cgroup v2 delegation.** A cgroup v2 subtree on the unified hierarchy, owned by the worker UID and not shared with any other service. Within it the worker can create and remove child cgroups, write to child `cgroup.procs`, read child `cgroup.events` and `cgroup.procs`, and write child `cgroup.freeze`.
2. **Systemd delegation.** A site-supported service or scope with `Delegate=yes` that the worker starts through a reachable user manager (`systemctl --user`, with the bus at `/run/user/<uid>/bus`). Delegation applies to services and scopes, not slices. Persistence across logout needs `loginctl enable-linger` from an administrator. It is not required for a single session.

Either option must satisfy these conditions:

- The delegated subtree contains only experiment workloads. It must not include production agent or service slices.
- The worker holds no sudo, root, or write access to system-owned cgroups, and no new capability is granted to the worker beyond the subtree.
- Emptiness is established from `cgroup.events` (`populated 0`) and an empty `cgroup.procs`, read after termination. `cgroup.kill` is preferred when present, but it is not assumed.
- Termination must handle forks during the operation. One acceptable method is: write `1` to `cgroup.freeze`, enumerate `cgroup.procs`, send SIGKILL to each enumerated PID while it is still listed in that cgroup, write `0` to `cgroup.freeze`, then re-enumerate until empty within a bounded time. This method must be demonstrated on the delegated kernel before it is relied on.

Placement must happen before user code runs. A process placed after it has started, or after it has forked, does not satisfy the boundary.

## Least-privilege boundary

- No change to production Claude hooks, credentials, Herdr server state, or system-owned cgroups.
- No privilege escalation by the worker at any step of provisioning or verification.
- The delegation covers one named subtree. Its path is recorded in the delegation record.

## Verification procedure (run by the operator, as the worker user, without sudo)

1. Record the host identity: `uname -r`, `systemctl --version`, `cat /proc/sys/kernel/random/boot_id`, `cat /proc/self/cgroup`, and the owner and mode of the delegated subtree.
2. Confirm the subtree is owned by the worker UID and that `test -w <subtree>/cgroup.procs` succeeds for a child cgroup, not for the subtree root.
3. Create a disposable child, `<subtree>/exp-verify-<uuid>`. Confirm `cgroup.events` reads `populated 0`.
4. Start a synthetic tree inside the child. It has a parent that exits, a child that calls `setsid` and sleeps, and a grandchild that ignores SIGTERM. Place every process before it runs user code. Confirm that `cgroup.procs` lists every descendant and that `cgroup.events` reads `populated 1`.
5. Attempt an escape. A child process writes its own PID into a cgroup outside the subtree, for example the system service's `cgroup.procs`. The write must fail with `EACCES` or `EPERM`. Any success is a containment failure, and verification stops.
6. Terminate the whole tree with the freeze-and-kill method above. Confirm `cgroup.events` reads `populated 0` and `cgroup.procs` is empty within the bound (suggested: 10 s).
7. Remove the child with `rmdir`. Confirm that no process listed in the child's `cgroup.procs` survives, and that no process outside the subtree carries the synthetic tree's marker.
8. Record every command, its stdout and stderr, and its exit code, with start and finish times.

## Expected process-tree behaviour

- Descendants that double-fork, `setsid`, or change process group remain in the boundary.
- A process that escapes the boundary is a containment failure. It is never recorded as success.
- A SIGTERM-ignoring descendant is terminated by the escalation, and only members enumerated from the boundary are signalled.
- A process forked during termination is found by the re-enumeration and terminated.

## Evidence format required for a production verifier

Evidence is a directory with these parts:

- `manifest.json`, with these fields:
  - `schema_version`
  - `host`: `hostname`, `boot_id`, `kernel`, `systemd_version`
  - `worker_uid`
  - `delegation`: `mechanism` (`cgroup2-direct` or `systemd-service`), `administrator`, `record_ref` (the delegation record), and `subtree_path`
  - `steps`: a list of `{command, exit_code, started_utc, finished_utc, stdout_sha256}`
  - `result`: `populated_after_termination`, `procs_after_termination`, `escape_write_result`, and `forked_during_termination_terminated`
- Transcript files whose hashes appear in `manifest.json`.
- A signature over the bundle, made with a key held outside the worker's reach. This is a requirement to verify on the delegated host. It is not a present property: the current W2 verifier seals proofs with an in-process key, which same-user worker code can read.

Per run attempt, the production verifier also needs a fresh challenge. It issues a nonce and epoch for the attempt, and the verifier emits a `ContainmentProof` for exactly that run, attempt and boundary. The proof is checked by `verify_for_release` in `scripts/containment_contract.py`. Its trust anchor must be provisioned by the operator, separately from the worker, in the same way as the trust-anchor requirement in the W4 APPROVAL.md (not verified from this worktree). The HMAC seal in `containment_contract.py` is not authority against a same-user worker and does not satisfy this requirement. The current production anchor set is empty and must stay empty until this evidence has been independently reviewed.

## Current status

Not provided. The probe reports BLOCKED with exit 2. The simulator and contract are OFFLINE_TESTED only. The production backend returns UNAVAILABLE and refuses every release. CONTAINMENT_READY remains BLOCKED. No review of this document has been performed.
