# Preliminary independent GPT artifact manifest re-review

Reviewed immutable commit `a852cb3f0b2bd0fa3395a02e1cf6d43b5c71489f` in a fresh detached worktree, comparing with `c1c3d89`. This is a fallback review, not Opus approval. Scope: the artifact manifest `O_PATH` open and focused tests; no GPU or live service used. No HIGH or CRITICAL issue identified within this standalone diagnostic scope.

The manifest is initially opened with `O_PATH | O_NOFOLLOW`, fstat-checked as a regular file, and reopened by pinned `/proc/self/fd` handle for bounded read; identity is checked before and after. This avoids a content open of a final-component device/FIFO and avoids a pathname replacement between validation and read. I injected a temporary path swap from a valid regular manifest to a FIFO immediately before the procfd reopen: `read_manifest` returned the original pinned manifest and did not hang; the path afterward referred to the FIFO.

Focused suite: 13/13 passed under Python 3.10.21 and 13/13 under 3.12.14. Existing tests reject FIFO, symlink to valid regular manifest, duplicate keys and nonstandard JSON, and exercise bounded traversal.

LOW test gap: `test_manifest_device_rejected_without_reading` only checks for an error, so the old implementation, which opened `/dev/null` for reading before fstat, would also pass. Spy on `os.open` and assert `O_PATH` is used and no read-open is attempted for a nonregular pinned object; add a deterministic path-swap regression. These do not establish an immutable snapshot: root/manifest ancestors and procfs are trusted, files may change during or after scanning, and approval, provenance, dataset leakage, scientific correctness and actual execution binding remain unverified.
