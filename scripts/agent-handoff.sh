#!/usr/bin/env bash
set -euo pipefail
cat <<'HANDOFF'
WORKER HANDOFF
- Project ID / Task ID:
- Agent identity, branch and separate worktree:
- Pinned commit SHA:
- Files changed, tests and exact outputs:
- Dataset/protocol changes:
- Run IDs, evidence and unresolved risks:

INDEPENDENT REVIEW
- Reviewer identity (not original author):
- Exact SHA reviewed:
- Code/methodology/leakage/reproducibility findings:
- Verdict: PASS / FAIL / BLOCKED
- Evidence / required fixes:

OWNER DECISION
- Proposed diff, cost and risk:
- Merge/GPU/data deletion/publication approval requested:
- Rollback plan:
HANDOFF
