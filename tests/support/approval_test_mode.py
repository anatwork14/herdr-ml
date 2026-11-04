"""The only place that obtains the approval test-mode capability.

approval._grant_test_capability() returns the private token only to callers inside
tests/support. Tests pass TEST_MODE as mode= (verify_handoff) or test_capability=
(verify_approval, AttemptContext). The string 'test' is refused.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import approval as _ap  # noqa: E402

TEST_MODE = _ap._grant_test_capability()
