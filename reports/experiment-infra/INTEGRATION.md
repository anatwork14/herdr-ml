# Experiment Infrastructure: integration of W1, W2, W4, W3

Branch: `feat/exp-integration`.

Base: `feat/experiment-infrastructure-p1` (PR #3 head) at
`865c37525cd4fb81603526170dffd0c2d3c844a0`. The base branch was not modified.

## Merges

Each merge was a separate `git merge --no-ff` commit. Every source branch was
confirmed at its accepted SHA before merging.

| # | Branch | Accepted SHA | Merge commit | Conflicts | Ran | Exit |
|---|--------|--------------|--------------|-----------|-----|------|
| 1 | feat/exp-w1-ownership | ef673ace1f783b52693497ccc021663ed4589e32 | a94d9fa | none | 209 | 0 (OK) |
| 2 | feat/exp-w2-containment | ac9e2a50c7862bcfd865f36c4d450e6c5971aec7 | 63216ee | none | 274 | 0 (OK) |
| 3 | feat/exp-w4-provenance | f3414a2139e677541b4d3b826ad80f0fbab49343 | 9d24ba8 | none | 375 | 0 (OK) |
| 4 | feat/exp-w3-queue | 8c306d91063be179d0101929f162cfbd66c034fc | aaef2e2 | none | 404 | 0 (OK) |

Suite command after each merge:
`/mnt/ssd/genos2/.local/bin/python3.12 -m unittest discover -s tests`.

Final count: 404 tests, OK (expected at least 238).

## Conflicts

None. Git's `ort` strategy merged all four branches automatically.
`feat/exp-w3-queue` was built on top of `feat/exp-w1-ownership` (ef673ac is an
ancestor of 8c306d9). The W1 commits it carries were already merged in step 1.
No test, assertion or check was changed during integration.

## Protected P0 files

`git diff 865c375 HEAD -- tests/test_registry_p0.py tests/test_queue_protocol.py`
shows only the fixture changes from W1 (commit 3002ac0: 16 insertions and 7
deletions across the two files).

`git diff ef673ac HEAD` on the same two files is empty, so they match W1
byte for byte.

## Safety invariants after integration

- `scripts/queue_adapter.py`: `PRODUCTION_DISPATCH_ENABLED = False`.
- `scripts/researchctl.py`: `require_supported_execution` still refuses every
  queued (non-local) execution. That refusal is unchanged from the base.
- `scripts/containment_contract.py`: the production verifier is built with
  `anchors={}`, so the W2 production trust-anchor set is empty.
- `scripts/approval.py`: the W4 approval protocol fails closed with
  `TRUST_ANCHOR_UNPROVISIONED` when the external trust anchor has not been
  provisioned. Its tests pass in the integrated suite.
