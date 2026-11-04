# Independent GPT fallback review: Herdr cwd and guard

Reviewed immutable `a3db23c3a1d17c897a87a3dd46d47f31d2e41179`. The reviewer did not author the Herdr changes. Relative and `~` workspace/tab cwd values are rejected, and 26 mock Herdr tests passed on Python 3.10 and 3.12. No new CRITICAL/HIGH issue was found within the documented contract requiring a genuinely server-enforced parent guard.

A dishonest adapter that asserts `atomic_parent_guard_verified=True` while providing a no-op guard still permits the original race; `herdr-dishonest-guard-a3-repro.py` demonstrates this. A boolean is not proof of the contract. No live adapter, authoritative server identity, or actual server guard was verified. Bootstrap remains BLOCKED. This is not Opus approval.
