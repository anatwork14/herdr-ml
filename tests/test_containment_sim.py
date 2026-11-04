"""Simulator mechanics: escalation, partial and failed cleanup, forks, crashes.

Test-only. Every synthetic child is a Popen handle created by ReapingTestCase's
registry and is reaped in tearDown. No cgroup, sudo or PID-based signal is used.

Escapes in these tests are SELF-REPORTED by the harness (attempt_escape). They
show that a declared escape is counted and blocks completion. They do not show
that the simulator detects a real OS escape, because it cannot.
"""
import signal
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import containment_sim as sim  # noqa: E402

contract = sim.contract


class InMemoryBoundaryTests(sim.ReapingTestCase):
    """Same state machine, no OS processes at all."""

    def boundary(self, **kwargs):
        return sim.SimBoundary(None, 'run-1', 'attempt-1', real=False, **kwargs)

    def test_sigterm_ignoring_members_escalate_to_sigkill_and_complete(self):
        b = self.boundary()
        leader = b.spawn_leader(ignore_sigterm=True)
        stubborn = b.add_descendant(leader, ignore_sigterm=True)
        plain = b.add_descendant(leader)
        c = sim.Controller(b)
        report = c.terminate(grace=0.0, budget=1.0)
        self.assertTrue(report.complete)
        kills = {mid for mid, sig, outcome in b.signal_log if sig == 'SIGKILL' and outcome == 'SENT'}
        self.assertEqual(kills, {leader, stubborn})
        self.assertNotIn(plain, kills)
        self.assertEqual(b.occupancy(), 0)
        self.assertTrue(c.verify().verdict == contract.VERDICT_EMPTY)
        self.assertTrue(c.complete().allowed)
        self.assertTrue(c.release(c.last_proof).allowed)

    def test_partial_termination_refuses_completion_and_release(self):
        b = self.boundary()
        leader = b.spawn_leader()
        b.add_descendant(leader)
        b.add_descendant(leader)
        c = sim.Controller(b)
        report = c.terminate(max_signals=1, budget=1.0)
        self.assertEqual(report.alive_after, 2)
        proof = c.verify()
        self.assertEqual(proof.verdict, contract.VERDICT_OCCUPIED)
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)

    def test_failed_cleanup_leaves_member_and_refuses_release(self):
        b = self.boundary()
        leader = b.spawn_leader()
        stuck = b.add_descendant(leader)
        b.failing_ids.add(stuck)
        c = sim.Controller(b)
        report = c.terminate(grace=0.0, budget=0.2)
        self.assertEqual(report.alive_after, 1)
        self.assertIn((stuck, 'SIGTERM', 'FAILED'), b.signal_log)
        proof = c.verify()
        self.assertEqual(proof.occupancy, 1)
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)

    def test_fork_during_cancellation_is_terminated_and_rechecked(self):
        b = self.boundary()
        leader = b.spawn_leader(ignore_sigterm=True)
        forked = []

        def fork_once(boundary, member_id, sig):
            if not forked:
                forked.append(boundary.fork_during_cancel(leader))

        b.on_signal = fork_once
        c = sim.Controller(b)
        report = c.terminate(grace=0.0, budget=1.0)
        self.assertTrue(report.complete)
        child = forked[0]
        self.assertTrue(b.members[child].forked_during_cancel)
        self.assertIn((child, 'SIGTERM', 'SENT'), b.signal_log)
        c.verify()
        self.assertTrue(c.complete().allowed)

    def test_occupancy_is_rechecked_at_completion_not_assumed_from_proof(self):
        b = self.boundary()
        leader = b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        proof = c.verify()
        self.assertEqual(proof.verdict, contract.VERDICT_EMPTY)
        b.observation_fault = True  # a re-check can no longer observe the boundary
        self.assertFalse(c.complete().allowed)
        b.observation_fault = False
        self.assertTrue(c.complete().allowed)

    def test_self_reported_escape_is_breach_not_success(self):
        b = self.boundary()
        leader = b.spawn_leader()
        escaped = b.add_descendant(leader)
        b.attempt_escape(escaped)
        self.assertIsNone(b.members[escaped].parent)
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        proof = c.verify()
        self.assertEqual(proof.verdict, contract.VERDICT_BREACHED)
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)

    def test_interrupted_verification_declares_no_completion(self):
        b = self.boundary()
        b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        self.assertIsNone(c.verify(interrupt=True))
        self.assertEqual(c.state, 'UNKNOWN')
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(None).allowed)

    def test_worker_crash_mid_run_stays_unknown_and_unreleased(self):
        b = self.boundary()
        leader = b.spawn_leader()
        b.add_descendant(leader)
        c = sim.Controller(b)
        self.assertEqual(c.observe_leader(), 'RUNNING')
        b.crash_leader()
        self.assertEqual(c.observe_leader(), 'UNKNOWN')
        proof = c.verify()
        self.assertEqual(proof.verdict, contract.VERDICT_OCCUPIED)
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)

    def test_recovered_supervisor_cannot_release_without_fresh_verification(self):
        b = self.boundary()
        b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        stale = c.verify()
        self.assertTrue(c.complete().allowed)
        # The supervisor process dies here; only the boundary journal survives.
        recovered = sim.Controller.recover(b)
        self.assertEqual(recovered.state, 'UNKNOWN')
        self.assertFalse(recovered.complete().allowed)
        self.assertFalse(recovered.release(stale).allowed)
        self.assertFalse(recovered.release(None).allowed)
        recovered.terminate(budget=1.0)
        fresh = recovered.verify()
        self.assertTrue(recovered.complete().allowed)
        self.assertTrue(recovered.release(fresh).allowed)
        self.assertEqual(sim.Controller.recover(b).state, 'RELEASED')

    def test_member_adopted_after_completion_blocks_release(self):
        """Mutation M1: removing the occupancy re-check in release() must fail this test."""
        b = self.boundary()
        b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        proof = c.verify()
        self.assertTrue(c.complete().allowed)
        b.adopt_orphan()  # a member becomes visible between completion and release
        decision = c.release(proof)
        self.assertFalse(decision.allowed)
        self.assertIn('descendant', decision.reason)
        self.assertFalse(b.released)
        self.assertEqual(c.state, 'COMPLETED')
        c.terminate(budget=1.0)
        fresh = c.verify()
        self.assertTrue(c.complete().allowed)
        self.assertTrue(c.release(fresh).allowed)

    def test_release_refused_from_any_state_other_than_completed(self):
        """Mutation M6: removing the state check in release() must fail this test."""
        b = self.boundary()
        b.spawn_leader()
        c = sim.Controller(b)
        self.assertFalse(c.release(None).allowed)  # RUNNING
        c.terminate(budget=1.0)
        proof = c.verify()
        self.assertEqual(c.state, 'PROOF_ISSUED')
        refused = c.release(proof)  # a valid, empty proof, but completion never happened
        self.assertFalse(refused.allowed)
        self.assertIn('not completed', refused.reason)
        self.assertIsNone(c.verify(interrupt=True))
        self.assertEqual(c.state, 'UNKNOWN')
        self.assertFalse(c.release(proof).allowed)
        self.assertFalse(b.released)

    def test_breach_recorded_after_verification_blocks_completion(self):
        """Mutation M4: removing the breach check from completion must fail this test."""
        b = self.boundary()
        leader = b.spawn_leader()
        c = sim.Controller(b)
        c.terminate(budget=1.0)
        proof = c.verify()
        self.assertEqual(proof.verdict, contract.VERDICT_EMPTY)
        b.attempt_escape(leader)  # self-reported; the member has already exited
        decision = c.complete()
        self.assertFalse(decision.allowed)
        self.assertIn('escape', decision.reason)
        self.assertFalse(c.release(proof).allowed)
        self.assertFalse(b.released)

    def test_hanging_child_startup_fails_the_test_instead_of_blocking(self):
        registry = self.registry
        with mock.patch.object(sim, 'CHILD_SRC', 'import time; time.sleep(30)'), \
                mock.patch.object(sim, 'READY_TIMEOUT_S', 0.5):
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                registry.spawn('sleep', seconds=30)
            self.assertLess(time.monotonic() - started, 10)

    def test_signals_never_reach_non_members(self):
        b = self.boundary()
        b.spawn_leader()
        with self.assertRaises(KeyError):
            b.signal('m999', signal.SIGKILL)
        self.assertEqual(b.signal_log, [])


class RealProcessBoundaryTests(sim.ReapingTestCase):
    """Real synthetic children, each started by this test's registry and reaped in tearDown."""

    def test_parent_exit_with_descendants_keeps_occupancy_and_refuses_release(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=0.2)
        b.add_descendant(leader, seconds=60)
        b.add_descendant(leader, detached=True, seconds=60)
        b.members[leader].proc.wait(timeout=5)
        c = sim.Controller(b)
        self.assertEqual(c.observe_leader(), 'UNKNOWN')
        self.assertEqual(b.occupancy(), 2)
        proof = c.verify()
        self.assertEqual(proof.occupancy, 2)
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)
        # Positive control: the same boundary can be released after real termination.
        self.assertTrue(c.terminate(grace=0.2, budget=5.0).complete)
        proof = c.verify()
        self.assertTrue(c.complete().allowed)
        self.assertTrue(c.release(proof).allowed)

    def test_sigterm_ignoring_descendants_escalate_only_to_spawned_members(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60)
        stubborn = [b.add_descendant(leader, ignore_sigterm=True, seconds=60) for _ in range(2)]
        # Spawned by this test, but deliberately outside the boundary.
        unrelated = self.registry.spawn('ignore-term', seconds=60)
        c = sim.Controller(b)
        report = c.terminate(grace=0.2, budget=5.0)
        self.assertTrue(report.complete)
        targets = {mid for mid, _, _ in b.signal_log}
        self.assertTrue(targets <= set(b.members))
        for mid in stubborn:
            self.assertIn((mid, 'SIGKILL', 'SENT'), b.signal_log)
        self.assertIsNone(unrelated.poll(), 'a non-member process was signalled')
        unrelated.kill()
        unrelated.wait(timeout=5)

    def test_self_reported_escape_keeps_detached_member_counted(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60)
        detached = b.add_descendant(leader, detached=True, seconds=60)
        b.attempt_escape(detached)  # self-reported; the OS state is not changed or observed
        self.assertEqual(b.occupancy(), 2)
        c = sim.Controller(b)
        c.terminate(grace=0.2, budget=5.0)
        proof = c.verify()
        self.assertEqual(proof.verdict, contract.VERDICT_BREACHED)
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)

    def test_fork_during_cancellation_real_children_are_reaped_and_rechecked(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60, ignore_sigterm=True)
        forked = []

        def fork_once(boundary, member_id, sig):
            if not forked:
                forked.append(boundary.fork_during_cancel(leader))

        b.on_signal = fork_once
        c = sim.Controller(b)
        report = c.terminate(grace=0.2, budget=5.0)
        self.assertTrue(report.complete)
        self.assertEqual(b.occupancy(), 0)
        self.assertIsNotNone(b.members[forked[0]].proc.poll())
        c.verify()
        self.assertTrue(c.complete().allowed)

    def test_partial_cleanup_real_refuses_then_full_cleanup_releases(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60)
        b.add_descendant(leader, seconds=60)
        c = sim.Controller(b)
        partial = c.terminate(max_signals=1, budget=5.0)
        # The second member was never signalled, so it is deterministically alive.
        self.assertFalse(partial.complete)
        self.assertGreaterEqual(partial.alive_after, 1)
        proof = c.verify()
        self.assertFalse(c.complete().allowed)
        self.assertFalse(c.release(proof).allowed)
        self.assertTrue(c.terminate(grace=0.2, budget=5.0).complete)
        proof = c.verify()
        self.assertTrue(c.complete().allowed)
        self.assertTrue(c.release(proof).allowed)

    def test_worker_crash_real_leaves_unknown_state(self):
        b = sim.SimBoundary(self.registry, 'run-1', 'attempt-1')
        leader = b.spawn_leader(seconds=60)
        b.add_descendant(leader, seconds=60)
        c = sim.Controller(b)
        b.crash_leader()
        self.assertEqual(c.observe_leader(), 'UNKNOWN')
        self.assertEqual(b.occupancy(), 1)
        self.assertFalse(c.release(c.verify()).allowed)
        self.assertFalse(c.complete().allowed)


if __name__ == '__main__':
    unittest.main()
