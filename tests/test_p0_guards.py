"""Unsafe operational modes must not silently proceed while P0 is incomplete."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('researchctl_guard', Path(__file__).resolve().parents[1] / 'scripts/researchctl.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class GuardTests(unittest.TestCase):
    def test_official_and_queued_are_blocked(self):
        for mode, queue in [('official', None), ('diagnostic', 'gpu0')]:
            with self.subTest(mode=mode, queue=queue):
                with self.assertRaises(m.ResearchError):
                    m.require_supported_execution(mode, queue)

    def test_local_diagnostic_allowed(self):
        m.require_supported_execution('diagnostic', None)

    def test_explicit_local_queue_is_blocked_but_ledger_local_is_allowed(self):
        with self.assertRaises(m.ResearchError):
            m.require_supported_execution('diagnostic', 'local')
        m.require_supported_execution('diagnostic', 'local', submission=True)
        with self.assertRaises(m.ResearchError):
            m.require_supported_execution('diagnostic', None, submission=True)
