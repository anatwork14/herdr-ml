# Minimal operator handoff

1. **Containment:** The research user has no sudo. A site administrator should provide a user-owned delegated cgroup v2 service/scope on the actual worker host, with a recursively verifiable termination/empty mechanism (such as supported `cgroup.kill` and `cgroup.events`), or a site-supported equivalent with the same guarantees. A reachable user service manager is needed if systemd user units are the chosen mechanism. Do not grant broad write access to arbitrary systemd-owned cgroups. The current host uses a hybrid hierarchy; its observed cgroup does not expose `cgroup.kill`, so the administrator must confirm actual kernel and service capabilities rather than assume the interface exists.
2. **Herdr:** Start the integration worker inside a genuine Herdr-managed pane (`HERDR_ENV=1` supplied by Herdr) and a disposable isolated session. The current agent cannot control or inspect a Herdr session from outside it. Even there, bootstrap must stay blocked until the server identity, creation-bound project identity and an atomic parent/ownership guard are established from the actual installed API and verified under fault injection.

From the actual proposed worker environment, run:

```sh
python3.10 scripts/containment_probe.py
bash scripts/containment-provision-plan.sh
cat /proc/self/cgroup
systemctl --user is-system-running
```

The probe deliberately remains BLOCKED until an independently verified backend exists; its writable flags are hints only. In the authorized Herdr pane, consult the installed Herdr skill and `herdr --help`, then inspect the relevant API schema in an isolated session. Do not launch GPU jobs. The [systemd delegation guidance](https://systemd.io/CGROUP_DELEGATION/) and [kernel cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html) describe the interfaces; neither a user bus nor delegation alone is proof of safe execution.

These are external prerequisites, not the complete P0 fix. Worker-bound immutable provenance and run-specific approval, queue task binding/recovery and independently verified release checks also require implementation before any hold can be lifted.
