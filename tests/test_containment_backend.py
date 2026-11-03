"""Contract tests for fail-closed containment backend selection."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'containment_backend.py'
spec = importlib.util.spec_from_file_location('containment_backend', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ContainmentBackendTests(unittest.TestCase):
    def test_default_backend_is_unavailable(self):
        result = m.UnavailableContainmentBackend().establish(['true'], run_id='test')
        self.assertEqual(result.status, m.ContainmentStatus.UNAVAILABLE)
        self.assertIsNone(result.proof)

    def test_verified_state_requires_evidence(self):
        with self.assertRaises(ValueError):
            m.ContainmentResult(m.ContainmentStatus.VERIFIED, 'claimed')

    def test_unavailable_state_rejects_asserted_evidence(self):
        with self.assertRaises(ValueError):
            m.ContainmentResult(m.ContainmentStatus.UNAVAILABLE, 'unknown', object())

    def test_test_backend_can_model_verified_contract(self):
        class FakeBackend:
            def establish(self, command, *, run_id):
                class FakeProof:
                    pass
                proof = FakeProof()
                proof.run_id = run_id
                return m.ContainmentResult(
                    m.ContainmentStatus.VERIFIED,
                    'test-only simulated proof',
                    proof,
                )

        result = FakeBackend().establish(['true'], run_id='test')
        self.assertEqual(result.status, m.ContainmentStatus.VERIFIED)
        self.assertEqual(result.proof.run_id, 'test')
