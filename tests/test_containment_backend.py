"""Contract tests for fail-closed containment backend selection."""
import importlib.util
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'containment_backend.py'
# containment_backend imports containment_contract by bare name, as researchctl does for its helpers.
sys.path.append(str(PATH.parent))
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

    def test_release_is_refused_without_a_verified_proof(self):
        import containment_contract as contract
        expectation = contract.Expectation(run_id='test', attempt_id='attempt-1', boundary_id='b', nonce='n', epoch=1)
        backend = m.get_backend()
        self.assertFalse(backend.release(None, expectation).allowed)
        self.assertIn('UNAVAILABLE', backend.release(None, expectation).reason)
        forged = contract.ContainmentProof(run_id='test', attempt_id='attempt-1', boundary_id='b',
                                           verifier_id='sim-test-verifier', observed_at_ns=0, nonce='n',
                                           epoch=1, occupancy=0, verdict='EMPTY', simulated=True, mac='0' * 64)
        self.assertFalse(backend.release(forged, expectation).allowed)

    def test_selector_module_does_not_import_test_support(self):
        source = PATH.read_text(encoding='utf-8')
        self.assertNotIn('containment_sim', source)
        self.assertNotIn('support', source)

