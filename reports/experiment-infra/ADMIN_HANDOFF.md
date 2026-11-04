# Administrator handoff

This document describes only. It contains no key material and no commands that change production state. Items are split into (A) containment capabilities and (B) the independent approval authority.

## A. Containment capabilities (host administrator)

| # | Item | Needed for restricted host test | Needed for production |
|---|---|---|---|
| A1 | A delegated cgroup v2 subtree for the worker user, or a site-supported systemd unit that gives an equivalent boundary | yes | yes |
| A2 | `cgroup.kill` available on that subtree, or a method to verify emptiness (kernel-enumerated `cgroup.procs` empty) | yes | yes |
| A3 | A verification transcript produced by running the verification as the worker user (not as root), showing the boundary, the kill and the emptiness check | yes | yes |
| A4 | A signed delegation record from an administrator other than the worker | yes | yes |

Host facts at time of writing: Linux 5.4.0-216; no delegated cgroup v2 boundary, no `cgroup.kill` and no user systemd bus were found (`CONTAINMENT.md`, `containment/OPERATOR_REQUIREMENTS.md`). The transcript and record must be new; none exists.

## B. Independent approval authority

| # | Item | Restricted host test | Production |
|---|---|---|---|
| B1 | An Ed25519 public key administered outside the worker. HMAC is not acceptable: the worker side would hold the signing key | yes | yes |
| B2 | A root-owned anchor directory at the fixed location the production loader reads, owned by uid 0 (`PRODUCTION_ADMIN_UIDS`), not group- or other-writable, with every parent directory up to the root passing the same owner and mode checks, and key files not group- or other-accessible | yes | yes |
| B3 | Key custody: the private key held by an authority separate from the worker and from the host administrator who deploys the anchor, offline or in an HSM | yes | yes |
| B4 | Rotation procedure: a new anchor identifier, overlap window, revocation-state sequence that only moves forward | no (a single test key may be used) | yes |
| B5 | Revocation procedure: a revocation list advanced by the authority, sequence rollback refused by the verifier | no | yes |

The worker uids must be disjoint from the admin uids; the loader refuses overlap.

## Also required in code (not administrator items)

A production containment backend that is implemented (it returns UNAVAILABLE by design today), a worker containment gate in `scripts/` (none exists), and an Ed25519 verifier implementation. Until then no restricted host test should be scheduled.

## Prerequisite summary

- Restricted host test: A1 to A4, B1 to B3, plus the code items above.
- Production: all of A and B, plus an independent re-review of the final head.
