"""Contract tests for fail-closed containment backend selection."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'containment_backend.py'
spec = importlib.util.spec_from_file_location('containment_backend', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ContainmentBackendTests(unittest.TestCase):
    def test_production_selector_is_unavailable(self):
        result = m.get_backend().establish(['true'], run_id='test')
        self.assertEqual(result.status, m.ContainmentStatus.UNAVAILABLE)
        self.assertIsNone(result.proof)
        self.assertFalse(m.is_verified_for_execution(result))

    def test_caller_cannot_create_verified_result_with_arbitrary_proof(self):
        class FakeProof:
            run_id = 'test'
            issuer = object()

        with self.assertRaises(ValueError):
            m.ContainmentResult(m.ContainmentStatus.VERIFIED, 'claimed', FakeProof())

    def test_unavailable_rejects_asserted_proof(self):
        with self.assertRaises(ValueError):
            m.ContainmentResult(m.ContainmentStatus.UNAVAILABLE, 'unknown', object())

    def test_untrusted_objects_never_authorize_execution(self):
        self.assertFalse(m.is_verified_for_execution({'status': 'VERIFIED'}))
        class ForgedResult:
            status = m.ContainmentStatus.VERIFIED
            proof = object()
        self.assertFalse(m.is_verified_for_execution(ForgedResult()))
