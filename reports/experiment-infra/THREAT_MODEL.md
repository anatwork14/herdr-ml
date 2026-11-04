# Threat model (P7)

Scope: `feat/p7-integration` at `f3123b3`. Status words: OFFLINE_TESTED, ISOLATED_CPU_VERIFIED, NOT_TESTED, BLOCKED. All six gates are BLOCKED.

| Threat | In or out | Notes |
|---|---|---|
| Same-user code added to `scripts/` | OUT | Deterrence only: `tests/test_approval_ast_guard.py` detects named patterns; it is incomplete and is not a security boundary |
| In-process forgery of Python objects (`object.__new__`, module globals, private fields, `_VERIFIED_ISSUERS`, same-process keys) | OUT | |
| Same-user processes signalling recorded descendants | IN for recorded identities | Identity via pidfd and start-time identity (OFFLINE_TESTED; CPU termination ISOLATED_CPU_VERIFIED) |
| Same-user processes: unrecorded descendants | OUT | Not found by recovery |
| Cgroup escape | OUT | Not verified; no cgroup boundary exists on this host (BLOCKED) |
| Daemon substitution | PARTLY IN | `PrivatePueue.peer_identity()` reads SO_PEERCRED on a probe connection and checks pid, executable hash, start ticks and socket inode. The carrying connection is not bound, so per-request authentication is OUT |
| Host-administrator compromise | OUT | |
| Queue cert-hash | identity only | A hash of the daemon certificate. It is not authentication and not peer authentication |
| TMPDIR and temp base | fixed `/tmp` only | Test-root mechanisms were deleted in the e53987e repair; TMPDIR is not consulted by approval code |
| Caller-supplied admin uids in the generic loader (`load_trust_anchor`) | residual | `load_production_trust_anchor` pins `PRODUCTION_ADMIN_UIDS = (0,)` and takes no admin or directory parameter. No trust anchor is provisioned, so the production path fails closed `TRUST_ANCHOR_UNPROVISIONED` |
| Approval signatures | limited | The stdlib verifier is HMAC-SHA256 and holds signer keys, so it is not an independent authority. An Ed25519 public-key verifier is not provisioned |

No claim of peer authentication is made where the transport cannot provide it.
