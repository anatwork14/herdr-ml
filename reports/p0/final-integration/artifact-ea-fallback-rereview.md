# Preliminary independent GPT artifact inventory re-review

Reviewed exact integrated SHA `ea7395c8f8305800bd5f553f8cfc4e4ce13818a2` in a new detached worktree. Opus was unavailable; this is an independent fallback review, not Opus approval. Scope: `scripts/artifact_tree.py` and `tests/test_artifact_tree.py`; no live services, datasets or GPU were used. No HIGH or CRITICAL issue was found in this standalone diagnostic scope.

The three findings in `/tmp/herdr-artifact-f4-review.md` were retested independently:

- **Expanded-path output budget:** the prior reproduction (14 nested 245-character components and 4,900 empty files) now raises `manifest size limit exceeded`. `append_record` accounts for JSON escaping and per-record overhead before appending; global entry, aggregate enumerated-name, depth and per-path limits remain. This is a bounded in-memory diagnostic, not an immutable filesystem snapshot.
- **FIFO manifest:** a temporary FIFO used with `--verify` returns exit 2 promptly with `BLOCKED: manifest must be a regular file`, instead of hanging. The code uses `O_NOFOLLOW | O_NONBLOCK`, verifies the opened file with `fstat`, and bounds the read.
- **Duplicate keys:** an otherwise-valid empty-tree manifest containing conflicting `schema_version` keys returns exit 2 with `BLOCKED: duplicate manifest key`. The hook applies to nested objects as well.

A symlink to an otherwise-valid regular manifest also returned exit 2. Remaining LOW test gap: `test_fifo_symlink_and_duplicate_manifest_are_blocked` uses a symlink to `/dev/null`, which would still fail the regular-file check if `O_NOFOLLOW` were accidentally removed. Add a symlink-to-valid-regular-manifest test. `json.loads` still permits NaN/Infinity internally, although exact manifest schema/type validation prevents such values from passing the current CLI verifier; explicit `parse_constant` rejection would make its parser contract clearer.

Focused suite: 10/10 passed with Python 3.10.21 and 10/10 with Python 3.12.14. Original root ancestor paths remain trusted, the inventory can change during or after scanning, and file hashes do not establish authorization, scientific validity, immutable execution, dataset provenance or leakage prevention. Official and queue holds must remain until their separate requirements are satisfied.
