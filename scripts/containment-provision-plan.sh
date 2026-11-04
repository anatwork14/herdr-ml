#!/usr/bin/env bash
# Prints a reviewable plan; never changes host configuration or invokes sudo.
set -euo pipefail
cat <<'PLAN'
Containment provisioning plan (informational; this script makes no changes)

1. As the research user, run:
     python3 scripts/containment_probe.py
   Exit 2 / BLOCKED is expected until an independently verified supervisor exists.

2. Ask the server administrator to provide a user systemd manager with a
   reachable user bus and a delegated, user-owned cgroup v2 service/scope subtree
   in the same host and mount namespace as the actual worker. The administrator
   can inspect current session state with:
     loginctl show-user <research-user> -p Linger -p State -p RuntimePath
   If persistence without login is required, the administrator may choose:
     loginctl enable-linger <research-user>
   This changes host state and is intentionally not executed here.

3. The administrator must select a supported unified cgroup v2 setup and a
   supervisor with a race-safe recursive kill mechanism; on current kernels
   check non-root cgroups for cgroup.kill and cgroup.events. The existing host
   may need a kernel/systemd configuration change or a suitable supervisor.
   This plan does not prescribe a boot change or reboot. Do not chown arbitrary
   systemd-managed paths or use a shared production service for experiments.

4. After provisioning, run the preflight from inside the actual delegated unit
   and conduct isolated CPU-only tests: detached descendants, graceful stop and
   escalation, supervisor SIGKILL, restart reconciliation, PID reuse, recursive
   empty verification, and non-release while any owned task remains. Verify the
   cgroup cannot be escaped by the workload under the chosen trust boundary.

5. Independent review must establish run/slot ownership, task-ID fencing,
   per-run resource accounting and safe release. A writable path or a working
   user bus alone is not proof. The implementation does not include this
   supervisor; the GPU, official-run and queued-run holds remain in effect.

References:
  https://systemd.io/CGROUP_DELEGATION/
  https://docs.kernel.org/admin-guide/cgroup-v2.html
PLAN
