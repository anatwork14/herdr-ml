"""Containment contract: proof model, release gate, production refusal, adversarial lifecycles.

Test-only simulator objects are used only inside tests. The production verifier
and production backend must refuse all of them. Real synthetic children are
reaped by ReapingTestCase.tearDown, which also asserts none remain.
"""
import ast
import dataclasses
import json
import os
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import containment_sim as sim  # noqa: E402  (also puts scripts/ on sys.path)

contract = sim.contract
import containment_backend as backend  # noqa: E402
import process_identity  # noqa: E402

NOW = 1_700_000_000_000_000_000
PROD_KEY = b'p' * 32  # a key no production verifier holds on this host


def expectation(**overrides):
    fields = dict(run_id='run-1', attempt_id='attempt-1', boundary_id='b-1', nonce='n-1', epoch=3)
    fields.update(overrides)
    return contract.Expectation(**fields)


def proof(key=sim.SIM_KEY, *, verifier_id=sim.SIM_VERIFIER_ID, simulated=True, seal=True, **overrides):
    fields = dict(run_id='run-1', attempt_id='attempt-1', boundary_id='b-1', verifier_id=verifier_id,
                  observed_at_ns=NOW, nonce='n-1', epoch=3, occupancy=0,
                  verdict=contract.VERDICT_EMPTY, simulated=simulated, mac='')
    fields.update(overrides)
    unsigned = contract.ContainmentProof(**fields)
    return contract.seal(unsigned, key) if seal else unsigned


def sim_check(obj, exp=None, *, now=NOW):
    return contract.check_proof(obj, exp or expectation(), anchors=sim.SIM_ANCHORS, now_ns=now,
                                allow_simulated=True)


class ProofModelTests(sim.ReapingTestCase):
    def test_positive_control_sim_proof_accepted_only_by_test_verifier(self):
        good = proof()
        self.assertTrue(sim_check(good).allowed)
        self.assertTrue(sim.sim_verify(good, expectation(), now_ns=NOW).allowed)
        self.assertFalse(contract.check_proof(good, expectation(), anchors=sim.SIM_ANCHORS, now_ns=NOW).allowed)
        self.assertFalse(contract.verify_for_release(good, expectation(), now_ns=NOW).allowed)

    def test_direct_dataclass_construction_never_verifies(self):
        for mac in ('', '0' * 64, 'deadbeef'):
            with self.subTest(mac=mac):
                forged = contract.ContainmentProof(
                    run_id='run-1', attempt_id='attempt-1', boundary_id='b-1', verifier_id=sim.SIM_VERIFIER_ID,
                    observed_at_ns=NOW, nonce='n-1', epoch=3, occupancy=0, verdict='EMPTY',
                    simulated=True, mac=mac)
                self.assertFalse(sim_check(forged).allowed)
                self.assertFalse(contract.verify_for_release(forged, expectation(), now_ns=NOW).allowed)

    def test_production_anchor_set_is_empty_and_refuses_any_sealed_proof(self):
        self.assertEqual(dict(contract.PRODUCTION_TRUST_ANCHORS), {})
        for key, verifier in ((PROD_KEY, 'prod-verifier'), (sim.SIM_KEY, sim.SIM_VERIFIER_ID)):
            with self.subTest(verifier=verifier):
                sealed = proof(key, verifier_id=verifier, simulated=False)
                decision = contract.verify_for_release(sealed, expectation(), now_ns=NOW)
                self.assertFalse(decision.allowed)
                self.assertIn('not trusted', decision.reason)

    def test_mismatched_run_attempt_boundary_refused(self):
        # Each proof is validly sealed; only the binding to the run attempt differs.
        self.assertFalse(sim_check(proof(run_id='run-2'), expectation()).allowed)
        self.assertFalse(sim_check(proof(attempt_id='attempt-2'), expectation()).allowed)
        self.assertFalse(sim_check(proof(boundary_id='b-2'), expectation()).allowed)
        self.assertFalse(sim_check(proof(), expectation(run_id='run-2')).allowed)
        self.assertFalse(sim_check(proof(), expectation(attempt_id='attempt-2')).allowed)
        self.assertFalse(sim_check(proof(), expectation(boundary_id='b-2')).allowed)

    def test_stale_replayed_and_unsolicited_proofs_refused(self):
        self.assertFalse(sim_check(proof(), expectation(nonce='n-2')).allowed)
        self.assertFalse(sim_check(proof(), expectation(epoch=4)).allowed)
        self.assertFalse(sim_check(proof(observed_at_ns=NOW - contract.MAX_PROOF_AGE_NS - 1)).allowed)
        self.assertTrue(sim_check(proof(observed_at_ns=NOW - contract.MAX_PROOF_AGE_NS)).allowed)
        self.assertFalse(sim_check(proof(observed_at_ns=NOW + contract.MAX_CLOCK_SKEW_NS + 1)).allowed)

    def test_nonempty_or_unknown_verdicts_refused_even_with_valid_mac(self):
        cases = [
            dict(occupancy=1, verdict=contract.VERDICT_EMPTY),      # inconsistent EMPTY
            dict(occupancy=1, verdict=contract.VERDICT_OCCUPIED),
            dict(occupancy=0, verdict=contract.VERDICT_OCCUPIED),
            dict(occupancy=0, verdict=contract.VERDICT_UNKNOWN),
            dict(occupancy=0, verdict=contract.VERDICT_BREACHED),
            dict(occupancy=0, verdict='PROBABLY_FINE'),
            dict(occupancy=-1, verdict=contract.VERDICT_EMPTY),
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertFalse(sim_check(proof(**overrides)).allowed)

    def test_tampered_proofs_refused(self):
        sealed = proof(occupancy=1, verdict=contract.VERDICT_OCCUPIED)
        tampered = [
            dataclasses.replace(sealed, occupancy=0, verdict=contract.VERDICT_EMPTY),
            dataclasses.replace(sealed, run_id='run-1'),
            dataclasses.replace(sealed, simulated=False),
            dataclasses.replace(sealed, observed_at_ns=NOW + 1),
            dataclasses.replace(sealed, epoch=3),
        ]
        for forged in tampered:
            with self.subTest(forged=forged):
                self.assertFalse(sim_check(forged).allowed)
        self.assertFalse(sim_check(proof(occupancy=1, verdict=contract.VERDICT_OCCUPIED, seal=False)).allowed)

    def test_untrusted_verifier_and_wrong_key_refused(self):
        self.assertFalse(sim_check(proof(verifier_id='someone-else')).allowed)
        self.assertFalse(sim_check(proof(PROD_KEY)).allowed)
        self.assertFalse(sim_check(proof(verifier_id=None, seal=False)).allowed)

    def test_non_proof_inputs_never_confer_authority(self):
        as_dict = dataclasses.asdict(proof())
        observation = process_identity.observe_process({'pid': os.getpid()}, socket.gethostname())
        inputs = [None, as_dict, json.dumps(as_dict), 'VERIFIED', observation, sim.SIM_ANCHORS, True]
        for value in inputs:
            with self.subTest(value=type(value).__name__):
                self.assertFalse(sim_check(value).allowed)
                self.assertFalse(contract.verify_for_release(value, expectation(), now_ns=NOW).allowed)
        self.assertFalse(sim_check(proof(), {'run_id': 'run-1'}).allowed)

    def test_process_identity_match_is_observation_not_containment(self):
        if not Path('/proc/self/stat').is_file():
            self.skipTest('Linux procfs unavailable')
        text = Path('/proc/self/stat').read_text()
        identity = {
            'pid': os.getpid(),
            'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'start_ticks': text[text.rfind(')') + 2:].split()[19],
            'pid_namespace': os.readlink('/proc/self/ns/pid'),
            'mount_namespace': os.readlink('/proc/self/ns/mnt'),
        }
        observed = process_identity.observe_process(identity, socket.gethostname())
        self.assertEqual(observed['observation'], 'MATCH')
        self.assertFalse(contract.verify_for_release(observed, expectation(), now_ns=NOW).allowed)

    def test_key_material_and_expectation_types_are_checked(self):
        with self.assertRaises(ValueError):
            contract.mac_for(proof(seal=False), b'short')
        self.assertFalse(contract.check_proof(proof(), {'run_id': 'run-1'}, anchors=sim.SIM_ANCHORS,
                                              now_ns=NOW, allow_simulated=True).allowed)


class ProductionBackendTests(sim.ReapingTestCase):
    def test_selector_returns_unavailable_and_never_the_simulator(self):
        selected = backend.get_backend()
        self.assertIsInstance(selected, backend.UnavailableContainmentBackend)
        self.assertEqual(type(selected).__module__, 'containment_backend')
        self.assertNotIn('sim', backend.get_backend.__code__.co_names)
        for name, value in vars(backend).items():
            module_file = getattr(value, '__file__', '') or ''
            self.assertNotIn('support', module_file, 'backend module imports test support: %s' % name)

    def test_establish_is_unavailable_and_release_refused_for_every_proof(self):
        result = backend.get_backend().establish(['true'], run_id='run-1')
        self.assertEqual(result.status, backend.ContainmentStatus.UNAVAILABLE)
        b = backend.get_backend()
        candidates = [None, proof(), proof(PROD_KEY, verifier_id='prod-verifier', simulated=False)]
        for candidate in candidates:
            with self.subTest(candidate=type(candidate).__name__):
                decision = b.release(candidate, expectation(), now_ns=NOW)
                self.assertFalse(decision.allowed)
                self.assertIn('UNAVAILABLE', decision.reason)

    def test_stale_and_mismatched_run_attempt_refused_at_backend(self):
        b = backend.get_backend()
        self.assertFalse(b.release(proof(), expectation(attempt_id='attempt-2'), now_ns=NOW).allowed)
        self.assertFalse(b.release(proof(), expectation(nonce='old'), now_ns=NOW).allowed)
        self.assertFalse(b.release(proof(observed_at_ns=0), expectation(), now_ns=NOW).allowed)

    def test_real_simulated_run_proof_refused_by_production_verifier(self):
        boundary = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        leader = boundary.spawn_leader()
        boundary.add_descendant(leader)
        controller = sim.Controller(boundary)
        controller.terminate(budget=1.0)
        issued = controller.verify()
        self.assertTrue(issued.simulated)
        self.assertTrue(controller.complete().allowed)
        exp = contract.Expectation(run_id='run-1', attempt_id='attempt-1', boundary_id=boundary.boundary_id,
                                   nonce=controller.nonce, epoch=boundary.epoch)
        self.assertFalse(contract.verify_for_release(issued, exp).allowed)
        self.assertFalse(backend.get_backend().release(issued, exp).allowed)
        self.assertTrue(controller.release(issued).allowed)  # only the test verifier path

    def test_scripts_never_reference_simulator_or_its_key(self):
        needles = ('containment_sim', 'sim_verify', 'SIM_KEY', 'SIM_ANCHORS', 'SIM_VERIFIER_ID')
        for path in sorted(SCRIPTS.rglob('*')):
            if not path.is_file() or path.suffix in ('.pyc',):
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            for needle in needles:
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_scripts_use_only_the_production_entry_point_for_verification(self):
        forbidden_calls = {'check_proof', 'seal', 'mac_for'}
        for path in sorted(SCRIPTS.glob('*.py')):
            if path.name == 'containment_contract.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                    with self.subTest(path=path.name, call=name):
                        self.assertNotIn(name, forbidden_calls)
                        self.assertFalse(any(kw.arg == 'anchors' for kw in node.keywords))


class AdversarialLifecycleTests(sim.ReapingTestCase):
    def test_parent_exit_with_descendants_refuses_completion_and_release(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=0.2)
        b.add_descendant(leader, detached=True, seconds=60)
        b.members[leader].proc.wait(timeout=5)
        c = sim.Controller(b)
        self.assertEqual(c.observe_leader(), 'UNKNOWN')
        self.assertEqual(b.occupancy(), 1)
        issued = c.verify()
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(issued).allowed)
        c.terminate(grace=0.2, budget=5.0)
        self.assertEqual(b.occupancy(), 0)

    def test_stale_proof_from_before_termination_cannot_release(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        b.spawn_leader(seconds=60)
        c = sim.Controller(b)
        before = c.verify()
        c.terminate(grace=0.2, budget=5.0)
        fresh = c.verify()
        self.assertTrue(c.complete().allowed)
        self.assertFalse(c.release(before).allowed)
        self.assertTrue(c.release(fresh).allowed)

    def test_mismatched_attempt_cannot_reuse_another_attempts_proof(self):
        first = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        first.spawn_leader()
        c1 = sim.Controller(first)
        c1.terminate(budget=1.0)
        proof_one = c1.verify()
        second = sim.SimBoundary(None, 'run-1', 'attempt-2', real=False)
        second.spawn_leader()
        c2 = sim.Controller(second)
        c2.terminate(budget=1.0)
        c2.verify()
        self.assertTrue(c2.complete().allowed)
        self.assertFalse(c2.release(proof_one).allowed)
        exp2 = contract.Expectation(run_id='run-1', attempt_id='attempt-2', boundary_id=second.boundary_id,
                                    nonce=c2.nonce, epoch=second.epoch)
        self.assertFalse(sim.sim_verify(proof_one, exp2, now_ns=proof_one.observed_at_ns).allowed)

    def test_replayed_proof_from_earlier_epoch_is_rejected(self):
        b = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        first = c.verify()
        second = c.verify()
        self.assertNotEqual(first.nonce, second.nonce)
        stale_exp = contract.Expectation(run_id='run-1', attempt_id='attempt-1', boundary_id=b.boundary_id,
                                         nonce=first.nonce, epoch=first.epoch)
        self.assertFalse(sim.sim_verify(first, contract.Expectation(
            run_id='run-1', attempt_id='attempt-1', boundary_id=b.boundary_id,
            nonce=second.nonce, epoch=second.epoch), now_ns=first.observed_at_ns).allowed)
        self.assertTrue(sim.sim_verify(first, stale_exp, now_ns=first.observed_at_ns).allowed)
        self.assertFalse(c.release(first).allowed)
        self.assertTrue(c.complete().allowed)

    def test_invalid_tampered_proof_cannot_release(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60)
        b.add_descendant(leader, seconds=60)
        c = sim.Controller(b)
        c.terminate(grace=0.2, budget=5.0)
        valid = c.verify()
        self.assertTrue(c.complete().allowed)
        forged = dataclasses.replace(valid, mac='0' * 64)
        self.assertFalse(c.release(forged).allowed)
        self.assertFalse(c.state == 'RELEASED')
        self.assertTrue(c.release(valid).allowed)

    def test_partial_cleanup_refuses_release_with_real_children(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60, ignore_sigterm=True)
        b.add_descendant(leader, ignore_sigterm=True, seconds=60)
        c = sim.Controller(b)
        report = c.terminate(max_signals=1, budget=5.0)
        self.assertFalse(report.complete)
        proof = c.verify()
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)

    def test_unavailable_host_capability_fails_closed(self):
        self.assertEqual(backend.get_backend().establish(['true'], run_id='run-1').status,
                         backend.ContainmentStatus.UNAVAILABLE)
        # Without a verified boundary there is no completion or release path for the production code.
        self.assertFalse(backend.get_backend().release(None, expectation()).allowed)

    def test_interrupted_verification_declares_no_completion_even_with_older_proof(self):
        b = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        older = c.verify()
        self.assertIsNone(c.verify(interrupt=True))
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(older).allowed)
        self.assertEqual(c.state, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
