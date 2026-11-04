"""Test-only construction helpers for approval tests.

Nothing in scripts/ imports this module. Anchors are loaded by the production loader
(approval.load_trust_anchor) with explicit uid allowlists; verifiers are supplied through
the public HandoffVerifier protocol. A private base directory is created inside the
checkout (git-ignored), because the production loader refuses world-writable ancestors
such as /tmp. The repo_root passed to the loader is a stand-in path, so the loader's
location check is the real one and is simply pointed away from the temp base.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import approval as ap  # noqa: E402

TEST_BASE = REPO / '.test-tmp'
STAND_IN_REPO = Path('/nonexistent-approval-test-repo')
_EUID = os.geteuid()
ADMIN_UIDS = tuple(sorted({_EUID, 0}))
WORKER_UIDS = (65534 if _EUID != 65534 else 65533,)
PRODUCTION_UIDS = {'admin_uids': ADMIN_UIDS, 'worker_uids': WORKER_UIDS}


def make_temp_dir():
    """Return (TemporaryDirectory-like object with cleanup(), realpath) under the private base."""
    TEST_BASE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(TEST_BASE, 0o700)
    path = tempfile.mkdtemp(prefix='ap-', dir=str(TEST_BASE))
    os.chmod(path, 0o700)

    class _Temp(object):
        name = path

        def cleanup(self):
            shutil.rmtree(path, ignore_errors=True)
    return _Temp(), Path(os.path.realpath(path))


def loader_options(run_dir, **extra):
    """Keyword arguments every production loader and store constructor needs in tests."""
    options = {'repo_root': STAND_IN_REPO, 'run_dir': run_dir}
    options.update(PRODUCTION_UIDS)
    options.update(extra)
    return options


class FixedAcceptHandoffVerifier(ap.HandoffVerifier):
    """Handoff verifier that exercises the binding checks without cryptography.

    Implements the public HandoffVerifier protocol; accepts one fixed value for listed keys.
    """
    algorithm = ap.HANDOFF_ALGORITHM

    def __init__(self, key_ids):
        self._key_ids = frozenset(key_ids)

    def verify(self, key_id, domain, message, value_hex):
        return key_id in self._key_ids and domain == ap.DOMAIN_HANDOFF and value_hex == 'test-accept'
