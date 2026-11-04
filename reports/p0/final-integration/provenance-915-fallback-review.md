# Preliminary independent GPT provenance pinning review

Reviewed immutable commit `91546b36fb59500500f5b4b0a9ea67d5fac97b13` in a fresh detached worktree. This is a fallback review, not Opus approval. Scope: `scripts/provenance.py`, particularly O_PATH pinning and provenance tests. No live service, GPU or protected dataset was used. No HIGH/CRITICAL finding confirmed in this standalone diagnostic scope.

The code opens each final manifest/artifact path with `O_PATH | O_NOFOLLOW`, verifies regular-file type, then opens the pinned procfd for reading and checks identity. A deterministic injected swap replaced the artifact pathname with a FIFO immediately before reopening `/proc/self/fd/<handle>`: the verifier returned the original regular-file hash and did not open or hang on the FIFO. The 15 provenance tests passed on Python 3.10.21 and 3.12.14. Existing tests spy on open flags for `/dev/null` and FIFO and verify that the final special-file path is opened only with O_PATH.

## MEDIUM: growing artifact can drive unbounded hashing

- Location: `scripts/provenance.py:90-93`, `_hash_relative`.
- Cause: the stream is read repeatedly until EOF without enforcing the initial pinned `st_size`. The `_pinned_regular` fstat comparison detects modification only after the consumer returns; a concurrent writer can keep extending the file and delay that check indefinitely.
- Reproduction: in an isolated temporary directory, start with a 65,536-byte regular file. Wrap the real `_pinned_regular` stream so that after each of its first six nonempty reads, 65,536 bytes are appended to that same file. `_hash_relative` reads 458,752 bytes before eventually raising `artifact changed during read`. A continual writer can extend the work further. The reproduction used bounded synthetic writes and no dataset.
- Remediation: cap reads to the initial pinned size plus one byte (and explicitly reject growth), then compare metadata and digest; define resource policy for legitimately large initial files.
- Acceptance: a controlled append-on-read test proves at most initial size plus one byte is consumed, returns BLOCKED promptly, and unchanged regular files of varied sizes retain correct hashes.

The manifest itself has a 1 MiB read cap, and device/FIFO paths fail before read-open. These checks do not prove immutable source or data, trustworthy signature policy, run-bound approval, atomic multi-file snapshot, or scientific validity; the official and queued execution holds remain required. Root ancestors, procfs and separately supplied trust policy are trusted under the stated design.
