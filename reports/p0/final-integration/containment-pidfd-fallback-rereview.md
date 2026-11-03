# Independent GPT fallback re-review: CPU pidfd test

Reviewed immutable commit `a0a11d4006c07b60790cd03b54d7fb36482fa8e4`. The reviewer did not author the containment test. No new CRITICAL/HIGH was identified in the CPU-only test scope.

The leader obtains a pidfd for its own unreaped direct child and passes the descriptor using SCM_RIGHTS; the test does not use a numeric grandchild PID for cleanup. A child-only socket echoes after leader death to distinguish an executing child from a zombie. Cleanup uses the pinned pidfd, and the leader process-group ID stays pinned using WNOWAIT. A nondefault SIGCHLD disposition skips before spawn.

The three focused tests passed on Python 3.10 and 3.12, and the escape/cleanup demonstration passed 20 additional Python 3.10 repetitions. An explicit SIGCHLD=SIG_IGN case skipped as intended. This is a preliminary GPT fallback review, not Opus review or proof of real containment or GPU slot safety.
