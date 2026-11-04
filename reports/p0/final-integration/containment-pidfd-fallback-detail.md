# Independent GPT fallback re-review: pidfd CPU test

Reviewed exact commit `a0a11d4006c07b60790cd03b54d7fb36482fa8e4` in a fresh detached worktree. This is not an Opus review or a containment certification. No CRITICAL/HIGH issue was found in the revised test's ownership/cleanup path.

The earlier PID reuse concern is addressed: the leader opens a pidfd for its own direct child while that child remains unreaped and transfers the descriptor by SCM_RIGHTS. The controller never signals the numeric grandchild PID. The earlier zombie false positive is addressed: after waiting for the leader to die, the controller sends a byte on a separate socket inherited only by the detached child and requires it echoed; signal-0 existence alone is not used as the survival assertion. Cleanup uses the pinned pidfd and polls for process exit. The leader's numeric process group is kept pinned by WNOWAIT until the group signal, and a nondefault inherited SIGCHLD causes an early test skip.

Focused test command: `python3 -m unittest discover -s tests -p test_containment_probe.py -v`. All three tests passed on Python 3.10.21 and 3.12.14. Twenty additional isolated 3.10 subprocess repetitions passed. An explicit SIGCHLD=SIG_IGN run skipped the demonstration before spawning, as intended.

The test shows only that a setsid child can outlive an original process group on the test host. It does not prove a cgroup supervisor, crash recovery, process-tree containment, GPU slot release, or scientific correctness. pidfd features unavailable on another host cause a documented skip, not a PASS for containment. GPU, queue and official holds remain necessary.
