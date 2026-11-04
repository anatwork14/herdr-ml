# Queue worker protocol and isolated Pueue evidence

## Protocol

The registry uses one owner-private SQLite index at the OS account's canonical
`~/.local/state/herdr-ml/queue-registry.sqlite3` path. The location comes from
the account database (`pwd.getpwuid`), not `HOME`, XDG variables, or per-project
configuration. The path components must be owner-controlled directories that
are not group/world writable; the database file is restricted to mode `0600`.

Queue rows are unique by run ID and bind task IDs by the composite
`(queue_instance, task_id)` key. The caller must supply a 64-character queue
instance fingerprint. The isolated harness computes it from the isolated
daemon's certificate. The library does not prove that an arbitrary supplied
fingerprint belongs to the live Pueue daemon. Production integration must
derive the fingerprint from the exact configured daemon/profile and define how
identity changes on restore/reset. No production queue adapter exists here.

An enqueue intent is durable before external submission. A returned task ID is
bound once. An uncertain enqueue response is marked `AMBIGUOUS`; the API will
not retry or bind it later. A task claim records a worker lane and a fencing
token. Pueue's `PUEUE_WORKER_ID` is a worker index, not a task ID or unique
invocation ID; the protocol stores it separately from the Pueue task ID.

The global queue index and each run's local execution registry are separate
SQLite databases. There is no cross-database atomic transaction. The ordering
is intentionally fail-closed: the shared row is committed as `CLAIMED` before
the local execution claim is created. A crash in that interval leaves no local
execution claim and a non-replayable global row. The CPU test simulates that
window and confirms that a repeated worker claim and a mismatched local fence
cannot proceed. There is no automatic recovery; investigate the queue and run
records before any operator action.

## Isolated real-Pueue run

Harness: `scripts/pueue_cpu_worker_integration.py`. It starts `pueued` in the
foreground with fresh private `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and
`XDG_RUNTIME_DIR` directories and a sanitized environment. It creates a stashed
CPU task, reads its ID from `pueue add --print-task-id`, binds it, enqueues it,
and runs the worker protocol against that task. Its `finally` block sends
`pueue shutdown` only through the isolated client environment and waits for
that exact foreground process. The harness does not set or modify `HOME`; it
sets only the three isolated XDG paths plus `PATH`, `SHELL`, `LANG`, and
`LC_ALL`. No default/production Pueue socket is queried.

Command:

```sh
/mnt/ssd/genos2/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10 \
  scripts/pueue_cpu_worker_integration.py \
  --pueue /mnt/ssd/genos2/workspace/herdr-ml-pueue-test/bin/pueue-x86_64-unknown-linux-musl \
  --pueued /mnt/ssd/genos2/workspace/herdr-ml-pueue-test/bin/pueued-x86_64-unknown-linux-musl \
  --python /mnt/ssd/genos2/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10
```

Observed output:

```json
{"daemon_exit": 0, "daemon_stopped": true, "fence_created": true, "isolated_xdg": true, "pueue_task_id": "0", "pueue_worker_index": "0", "sanitized_environment": ["LANG", "LC_ALL", "PATH", "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"], "shutdown_command_exit": 0, "task_finished": true}
```

The task ID and worker index happened to both be `0`; the worker still passed
each through the distinct protocol fields. Pueue was version 4.0.4. The test
binaries had SHA-256 `c1b10d7e4e62211075ddd0e1dc3e8cbfc5a43d662cb3be7402a28504e23fcb51`
(`pueue`) and `5afeff6adbafb909e8d54e2caff158e6966c2adffa2c09e60fd631cc51b60390`
(`pueued`). No vendor signature or supply-chain certification is claimed.

The complete CPU-only unit suite passed 116 tests under Python 3.10.21. These
tests include global task uniqueness within one queue instance, separate queue
instance namespaces, ambiguous submission fencing, competing workers, and the
crash window between the global and run-local databases. They also check
0600 file creation, symlink refusal, HOME environment independence, and
fail-closed behavior when directory fsync fails.

This evidence validates a CPU protocol harness only. It does not wire
`researchctl` to Pueue, prove the supplied instance fingerprint against a
production daemon, recover crashes, prove descendant containment, validate
GPU scheduling, or authorize official execution. Queue and official holds
remain in force.
