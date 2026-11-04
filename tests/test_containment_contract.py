"""Containment contract: proof model, release gate, production refusal, adversarial lifecycles.

Test-only simulator objects are used only inside tests. The production verifier
and production backend must refuse all of them. Real synthetic children are
reaped by ReapingTestCase.tearDown, which also asserts none remain.
"""
import ast
import dataclasses
import inspect
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import containment_sim as sim  # noqa: E402  (also puts scripts/ on sys.path)

contract = sim.contract
import containment_backend as backend  # noqa: E402
import process_identity  # noqa: E402

NOW = 1_700_000_000_000_000_000
PROD_KEY = b'p' * 32  # a key no production verifier holds on this host


class FixedClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def make_verifier(clock=None):
    return sim.make_test_verifier(clock=clock if clock is not None else FixedClock())


def challenge(verifier, **overrides):
    fields = dict(run_id='run-1', attempt_id='attempt-1', boundary_id='b-1', epoch=3)
    fields.update(overrides)
    return verifier.issue_challenge(**fields)


def sealed(exp, *, key=sim.SIM_KEY, verifier_id=sim.SIM_VERIFIER_ID, simulated=True, observed=NOW,
           occupancy=0, verdict=contract.VERDICT_EMPTY, seal_it=True, **overrides):
    fields = dict(run_id=exp.run_id, attempt_id=exp.attempt_id, boundary_id=exp.boundary_id,
                  verifier_id=verifier_id, observed_at_ns=observed, nonce=exp.nonce, epoch=exp.epoch,
                  occupancy=occupancy, verdict=verdict, simulated=simulated, mac='')
    fields.update(overrides)
    unsigned = contract.ContainmentProof(**fields)
    return contract.seal(unsigned, key) if seal_it else unsigned


def derived(proof):
    """An Expectation built from a proof's own fields. The production path must refuse it."""
    return contract.Expectation(run_id=proof.run_id, attempt_id=proof.attempt_id,
                                boundary_id=proof.boundary_id, nonce=proof.nonce, epoch=proof.epoch)


class ProofModelTests(sim.ReapingTestCase):
    def test_positive_control_test_verifier_accepts_once_and_production_refuses(self):
        v = make_verifier()
        exp = challenge(v)
        proof_ = sealed(exp)
        self.assertTrue(v.preview(proof_, exp).allowed)
        self.assertTrue(v.verify_for_release(proof_, exp).allowed)
        self.assertFalse(contract.verify_for_release(proof_, exp).allowed)

    def test_direct_dataclass_construction_never_verifies(self):
        v = make_verifier()
        exp = challenge(v)
        for mac in ('', '0' * 64, 'deadbeef'):
            with self.subTest(mac=mac):
                forged = contract.ContainmentProof(
                    run_id='run-1', attempt_id='attempt-1', boundary_id='b-1', verifier_id=sim.SIM_VERIFIER_ID,
                    observed_at_ns=NOW, nonce=exp.nonce, epoch=exp.epoch, occupancy=0, verdict='EMPTY',
                    simulated=True, mac=mac)
                self.assertFalse(v.preview(forged, exp).allowed)
                self.assertFalse(contract.verify_for_release(forged, exp).allowed)

    def test_production_verifier_has_no_anchors_and_refuses_sealed_proofs(self):
        production = contract._PRODUCTION_VERIFIER
        self.assertEqual(getattr(production, '_Verifier__anchors'), {})
        for key, verifier_id in ((PROD_KEY, 'prod-verifier'), (sim.SIM_KEY, sim.SIM_VERIFIER_ID)):
            with self.subTest(verifier=verifier_id):
                sealed_proof = sealed(derived(sealed(challenge(make_verifier()))), key=key,
                                      verifier_id=verifier_id, simulated=False)
                decision = contract.verify_for_release(sealed_proof, derived(sealed_proof))
                self.assertFalse(decision.allowed)
                self.assertIn('not trusted', decision.reason)

    def test_mismatched_run_attempt_boundary_refused(self):
        v = make_verifier()
        exp = challenge(v)
        for overrides in ({'run_id': 'run-2'}, {'attempt_id': 'attempt-2'}, {'boundary_id': 'b-2'}):
            with self.subTest(proof=overrides):
                self.assertFalse(v.preview(sealed(exp, **overrides), exp).allowed)
        other = challenge(v, run_id='run-2', boundary_id='b-9')
        self.assertFalse(v.preview(sealed(exp), other).allowed)

    def test_stale_and_future_proofs_refused_by_verifier_clock(self):
        v = make_verifier(FixedClock(NOW))
        exp = challenge(v)
        self.assertFalse(v.preview(sealed(exp, observed=NOW - contract.MAX_PROOF_AGE_NS - 1), exp).allowed)
        self.assertTrue(v.preview(sealed(exp, observed=NOW - contract.MAX_PROOF_AGE_NS), exp).allowed)
        self.assertFalse(v.preview(sealed(exp, observed=NOW + contract.MAX_CLOCK_SKEW_NS + 1), exp).allowed)

    def test_nonempty_or_unknown_verdicts_refused_even_with_valid_mac(self):
        v = make_verifier()
        exp = challenge(v)
        cases = [
            dict(occupancy=1, verdict=contract.VERDICT_EMPTY),
            dict(occupancy=1, verdict=contract.VERDICT_OCCUPIED),
            dict(occupancy=0, verdict=contract.VERDICT_OCCUPIED),
            dict(occupancy=0, verdict=contract.VERDICT_UNKNOWN),
            dict(occupancy=0, verdict=contract.VERDICT_BREACHED),
            dict(occupancy=0, verdict='PROBABLY_FINE'),
            dict(occupancy=-1, verdict=contract.VERDICT_EMPTY),
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertFalse(v.preview(sealed(exp, **overrides), exp).allowed)

    def test_tampered_proofs_refused(self):
        v = make_verifier()
        exp = challenge(v)
        original = sealed(exp, occupancy=1, verdict=contract.VERDICT_OCCUPIED)
        tampered = [
            dataclasses.replace(original, occupancy=0, verdict=contract.VERDICT_EMPTY),
            dataclasses.replace(original, run_id='run-2'),
            dataclasses.replace(original, simulated=False),
            dataclasses.replace(original, observed_at_ns=NOW + 1),
            dataclasses.replace(original, epoch=4),
        ]
        for forged in tampered:
            with self.subTest(forged=forged):
                self.assertFalse(v.preview(forged, exp).allowed)
        self.assertFalse(v.preview(sealed(exp, occupancy=1, verdict=contract.VERDICT_OCCUPIED, seal_it=False),
                                   exp).allowed)

    def test_untrusted_verifier_and_wrong_key_refused(self):
        v = make_verifier()
        exp = challenge(v)
        self.assertFalse(v.preview(sealed(exp, verifier_id='someone-else'), exp).allowed)
        self.assertFalse(v.preview(sealed(exp, key=PROD_KEY), exp).allowed)
        self.assertFalse(v.preview(sealed(exp, verifier_id=None, seal_it=False), exp).allowed)

    def test_non_proof_inputs_never_confer_authority(self):
        v = make_verifier()
        exp = challenge(v)
        as_dict = dataclasses.asdict(sealed(exp))
        observation = process_identity.observe_process({'pid': os.getpid()}, socket.gethostname())
        inputs = [None, as_dict, json.dumps(as_dict), 'VERIFIED', observation, sim.SIM_KEY, True]
        for value in inputs:
            with self.subTest(value=type(value).__name__):
                self.assertFalse(v.preview(value, exp).allowed)
                self.assertFalse(contract.verify_for_release(value, exp).allowed)
        self.assertFalse(v.preview(sealed(exp), {'run_id': 'run-1'}).allowed)

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
        self.assertFalse(contract.verify_for_release(observed, derived(sealed(challenge(make_verifier())))).allowed)

    def test_key_material_checked_and_no_public_checker_remains(self):
        with self.assertRaises(ValueError):
            contract.mac_for(sealed(challenge(make_verifier()), seal_it=False), b'short')
        self.assertFalse(hasattr(contract, 'check_proof'))


class MalformedInputTests(sim.ReapingTestCase):
    """MEDIUM-1: exact types, bounded values, and no exception escaping as acceptance."""

    def test_str_subclass_rebinding_cannot_match_another_run(self):
        class Rebound(str):
            def __eq__(self, other):
                return True

            __hash__ = str.__hash__

        v = make_verifier()
        exp_b = challenge(v, run_id='run-B')
        proof_a = sealed(exp_b, run_id='run-A')  # sealed for run-A, challenge is for run-B
        rewrapped = dataclasses.replace(proof_a, run_id=Rebound('run-A'))
        decision = v.preview(rewrapped, exp_b)
        self.assertFalse(decision.allowed)
        self.assertIn('malformed', decision.reason)
        rewrapped_exp = dataclasses.replace(exp_b, run_id=Rebound('run-B'))
        self.assertFalse(v.preview(proof_a, rewrapped_exp).allowed)

    def test_bool_epoch_does_not_match_integer_epoch(self):
        v = make_verifier()
        exp = challenge(v, epoch=1)
        proof_ = sealed(exp)
        self.assertTrue(v.preview(proof_, exp).allowed)
        self.assertFalse(v.preview(proof_, dataclasses.replace(exp, epoch=True)).allowed)
        self.assertFalse(v.preview(sealed(exp, epoch=True), exp).allowed)

    def test_unhashable_verdict_returns_refusal_not_exception(self):
        v = make_verifier()
        exp = challenge(v)
        decision = v.preview(sealed(exp, verdict=['EMPTY']), exp)
        self.assertFalse(decision.allowed)

    def test_empty_nonce_refused_on_both_sides(self):
        v = make_verifier()
        exp = challenge(v)
        self.assertFalse(v.preview(sealed(exp, nonce=''), exp).allowed)
        self.assertFalse(v.preview(sealed(exp), dataclasses.replace(exp, nonce='')).allowed)

    def test_out_of_bounds_counters_refused(self):
        v = make_verifier()
        exp = challenge(v)
        for overrides in ({'epoch': -1}, {'epoch': 2**70}, {'occupancy': -1},
                          {'occupancy': 2**40}, {'observed_at_ns': -5}):
            with self.subTest(overrides=overrides):
                self.assertFalse(v.preview(sealed(exp, **overrides), exp).allowed)

    def test_exception_inside_verifier_becomes_refusal(self):
        def broken_clock():
            raise OSError('clock unavailable')

        v = make_verifier(broken_clock)
        exp = challenge(v)
        decision = v.verify_for_release(sealed(exp), exp)
        self.assertFalse(decision.allowed)
        self.assertIn('failed closed', decision.reason)
        v = make_verifier(lambda: 'not-an-int')
        self.assertFalse(v.preview(sealed(exp), exp).allowed)


class ChallengeAndReplayTests(sim.ReapingTestCase):
    """MEDIUM-2: verifier-owned clock, single-use nonces, supervisor-issued challenges."""

    def test_production_entry_point_takes_no_caller_time_or_anchor(self):
        self.assertEqual(list(inspect.signature(contract.verify_for_release).parameters), ['proof', 'expectation'])

    def test_release_consumes_nonce_and_refuses_second_use(self):
        v = make_verifier()
        exp = challenge(v)
        proof_ = sealed(exp)
        self.assertTrue(v.verify_for_release(proof_, exp).allowed)
        second = v.verify_for_release(proof_, exp)
        self.assertFalse(second.allowed)
        self.assertIn('consumed', second.reason)
        self.assertFalse(v.preview(proof_, exp).allowed)

    def test_concurrent_release_consumes_the_nonce_exactly_once(self):
        v = make_verifier()
        exp = challenge(v)
        proof_ = sealed(exp)
        barrier = threading.Barrier(16)
        results = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            decision = v.verify_for_release(proof_, exp)
            with lock:
                results.append(decision.allowed)

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(results), 16)

    def test_new_challenge_supersedes_earlier_one_for_the_same_boundary(self):
        v = make_verifier()
        old = challenge(v)
        old_proof = sealed(old)
        new = challenge(v)
        self.assertNotEqual(old.nonce, new.nonce)
        decision = v.preview(old_proof, old)
        self.assertFalse(decision.allowed)
        self.assertIn('superseded', decision.reason)
        self.assertTrue(v.preview(sealed(new), new).allowed)

    def test_expectation_built_from_proof_fields_is_refused_by_production_path(self):
        v = make_verifier()
        forged = sealed(challenge(make_verifier()), run_id='run-1', nonce='a' * 32, epoch=1)
        built_from_proof = derived(forged)
        production = contract.verify_for_release(forged, built_from_proof)
        self.assertFalse(production.allowed)
        # Same attack against a verifier that trusts the key: refused because the
        # challenge was never issued by it.
        test_side = v.verify_for_release(sealed(built_from_proof), built_from_proof)
        self.assertFalse(test_side.allowed)
        self.assertIn('not issued', test_side.reason)


class ProductionIsolationTests(sim.ReapingTestCase):
    """LOW-4 / R1: the production release function is bound to its verifier object.

    Rebinding the module global _PRODUCTION_VERIFIER does not change outcomes.
    Editing private verifier fields in-process is NOT prevented and is out of the
    threat model. The documented limitation is asserted below.
    """

    def test_rebinding_production_verifier_global_does_not_change_release(self):
        permissive = make_verifier(FixedClock(NOW))  # trusts the simulator key
        exp = challenge(permissive)
        proof_ = sealed(exp, simulated=False)
        self.assertTrue(permissive.preview(proof_, exp).allowed)  # the proof is otherwise valid
        with mock.patch.object(contract, '_PRODUCTION_VERIFIER', permissive):
            decision = contract.verify_for_release(proof_, exp)
        self.assertFalse(decision.allowed)
        self.assertIn('not trusted', decision.reason)

    def test_limitation_text_is_documented(self):
        doc = (ROOT / 'reports' / 'experiment-infra' / 'CONTAINMENT.md').read_text(encoding='utf-8')
        self.assertIn('In-process tampering', doc)
        self.assertIn('rebinding module globals', doc)
        self.assertIn('outside the threat model', doc)


class ConstructorValidationTests(sim.ReapingTestCase):
    """R3: a Verifier rejects malformed policy arguments at construction."""

    def build(self, **overrides):
        fields = dict(anchors={}, clock=FixedClock(), allow_simulated=False)
        fields.update(overrides)
        return contract.Verifier(**fields)

    def test_allow_simulated_must_be_a_real_bool(self):
        for value in ('False', 0, 1, None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.build(allow_simulated=value)

    def test_max_age_must_be_positive_int_not_bool_and_bounded(self):
        with self.assertRaises(ValueError):
            self.build(max_age_ns=-1)
        with self.assertRaises(TypeError):
            self.build(max_age_ns=True)
        with self.assertRaises(ValueError):
            self.build(max_age_ns=0)
        with self.assertRaises(ValueError):
            self.build(max_age_ns=contract.MAX_WINDOW_NS + 1)
        with self.assertRaises(TypeError):
            self.build(max_age_ns=1.5)
        self.assertIsNotNone(self.build(max_age_ns=contract.MAX_WINDOW_NS))

    def test_max_skew_must_be_non_negative_int_not_bool_and_bounded(self):
        with self.assertRaises(ValueError):
            self.build(max_skew_ns=-1)
        with self.assertRaises(TypeError):
            self.build(max_skew_ns=False)
        self.assertIsNotNone(self.build(max_skew_ns=0))


class ProductionBackendTests(sim.ReapingTestCase):
    def test_selector_returns_unavailable_and_never_the_simulator(self):
        selected = backend.get_backend()
        self.assertIsInstance(selected, backend.UnavailableContainmentBackend)
        self.assertEqual(type(selected).__module__, 'containment_backend')
        self.assertIsNone(getattr(selected, '_verifier'))
        self.assertNotIn('sim', backend.get_backend.__code__.co_names)
        for name, value in vars(backend).items():
            module_file = getattr(value, '__file__', '') or ''
            self.assertNotIn('support', module_file, 'backend module imports test support: %s' % name)

    def test_establish_is_unavailable_and_release_refused_for_every_proof(self):
        result = backend.get_backend().establish(['true'], run_id='run-1')
        self.assertEqual(result.status, backend.ContainmentStatus.UNAVAILABLE)
        v = make_verifier()
        exp = challenge(v)
        b = backend.get_backend()
        candidates = [(None, exp), (sealed(exp), exp),
                      (sealed(exp, key=PROD_KEY, verifier_id='prod-verifier', simulated=False), exp)]
        for candidate, expectation_ in candidates:
            with self.subTest(candidate=type(candidate).__name__):
                decision = b.release(candidate, expectation_)
                self.assertFalse(decision.allowed)
                self.assertIn('UNAVAILABLE', decision.reason)

    def test_stale_and_mismatched_run_attempt_refused_at_backend(self):
        v = make_verifier(FixedClock(NOW))
        exp = challenge(v)
        backend_under_test = backend.UnavailableContainmentBackend(verifier=v)
        stale = sealed(exp, observed=NOW - contract.MAX_PROOF_AGE_NS - 1)
        self.assertFalse(backend_under_test.release(stale, exp).allowed)
        mismatched = sealed(exp, attempt_id='attempt-2')
        decision = backend_under_test.release(mismatched, exp)
        self.assertFalse(decision.allowed)
        self.assertIn('attempt', decision.reason)

    def test_test_anchor_in_verifier_still_refuses_unverified_boundary(self):
        """LOW-6b: a well-formed, test-verified proof must not release an unestablished boundary."""
        v = make_verifier()
        backend_under_test = backend.UnavailableContainmentBackend(verifier=v)
        occupied_boundary = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        occupied_boundary.spawn_leader()
        occupied = sim.Controller(occupied_boundary, verifier=v)
        occupied_proof = occupied.verify()
        self.assertEqual(occupied_proof.verdict, contract.VERDICT_OCCUPIED)
        self.assertFalse(backend_under_test.release(occupied_proof, occupied.expectation).allowed)

        empty_boundary = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        empty_boundary.spawn_leader()
        empty = sim.Controller(empty_boundary, verifier=v)
        empty.terminate(budget=1.0)
        empty_proof = empty.verify()
        self.assertTrue(v.preview(empty_proof, empty.expectation).allowed)
        decision = backend_under_test.release(empty_proof, empty.expectation)
        self.assertFalse(decision.allowed)
        self.assertIn('no established boundary', decision.reason)

    def test_real_simulated_run_proof_refused_by_production_verifier(self):
        boundary = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        leader = boundary.spawn_leader()
        boundary.add_descendant(leader)
        controller = sim.Controller(boundary)
        controller.terminate(budget=1.0)
        issued = controller.verify()
        self.assertTrue(issued.simulated)
        self.assertTrue(controller.complete().allowed)
        self.assertFalse(contract.verify_for_release(issued, controller.expectation).allowed)
        self.assertFalse(backend.get_backend().release(issued, controller.expectation).allowed)
        self.assertTrue(controller.release(issued).allowed)  # only the test verifier path

    def test_scripts_never_reference_simulator_or_its_key(self):
        needles = ('containment_sim', 'make_test_verifier', 'SIM_KEY', 'SIM_VERIFIER', 'sim_verify')
        for path in sorted(SCRIPTS.rglob('*')):
            if not path.is_file() or path.suffix == '.pyc':
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            for needle in needles:
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_scripts_cannot_seal_check_or_build_verifiers(self):
        forbidden_calls = {'check_proof', 'seal', 'mac_for', 'issue_challenge', 'preview', 'Verifier'}
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
        v = make_verifier()
        first = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        first.spawn_leader()
        c1 = sim.Controller(first, verifier=v)
        c1.terminate(budget=1.0)
        proof_one = c1.verify()
        second = sim.SimBoundary(None, 'run-1', 'attempt-2', real=False)
        second.spawn_leader()
        c2 = sim.Controller(second, verifier=v)
        c2.terminate(budget=1.0)
        c2.verify()
        self.assertTrue(c2.complete().allowed)
        self.assertFalse(c2.release(proof_one).allowed)
        self.assertFalse(v.preview(proof_one, c2.expectation).allowed)

    def test_replayed_proof_from_earlier_epoch_is_rejected(self):
        v = make_verifier()
        b = sim.SimBoundary(None, 'run-1', 'attempt-1', real=False)
        b.spawn_leader()
        c = sim.Controller(b, verifier=v)
        c.terminate(budget=1.0)
        first = c.verify()
        first_exp = c.expectation
        c.verify()
        second_exp = c.expectation
        self.assertNotEqual(first.nonce, second_exp.nonce)
        self.assertFalse(v.preview(first, second_exp).allowed)
        self.assertFalse(v.preview(first, first_exp).allowed)  # superseded by the newer challenge
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
        self.assertFalse(c.verifier.verify_for_release(forged, c.expectation).allowed)
        self.assertNotEqual(c.state, 'RELEASED')
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
        self.assertFalse(backend.get_backend().release(None, challenge(make_verifier())).allowed)

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
