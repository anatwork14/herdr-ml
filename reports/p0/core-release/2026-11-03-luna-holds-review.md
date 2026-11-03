# Independent fallback review of held paths

Reviewer: GPT-6 Luna, independent worktree `/tmp/herdr-pr2-review-52ca`.
Reviewed immutable commit: `52ca202a25fa0c469ee858ff2fffeadd4a237b1d`.
This is not an Opus review and is not a complete security or operational review.

The reviewer found no confirmed HIGH or CRITICAL bypass within this limited scope:

- `scripts/bootstrap-herdr.sh:5-7` exits 2 before mutation; running the script returned BLOCKED and exit 2. The reconciler is not a live adapter.
- `scripts/researchctl.py:211-216,237,307-312` blocks official and queued paths and rechecks at execution; direct gate calls rejected official and queued inputs and allowed diagnostic/local at the gate.
- `scripts/run_registry.py:240-265,295-327` checks binding and worker/fence fields, but is not connected to production queue dispatch. The review does not certify crash recovery or queue identity.
- `scripts/provenance.py:2-7,109-113` describes point-in-time verification, not execution authorization; no path was found that lets it bypass the official hold.

The reviewer observed that diagnostic commands execute with the invoking user's authority and that the registry assumes a trusted single-host user; neither is a security boundary. No containment backend is registered. No live Herdr, GPU, full-suite, production queue, or scientific validation was performed. These limitations remain release blockers.
