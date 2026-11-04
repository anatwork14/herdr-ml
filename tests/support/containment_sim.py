"""TEST-ONLY containment boundary simulator. Never import this from scripts/.

This is not a production backend and its output is not containment evidence.
It models a boundary as a member table with parent links, escape records and
an injectable failure surface. In real mode each member is a synthetic child
that this module starts with subprocess.Popen. Signals are sent only through
Popen handles held by the SpawnRegistry that created them, and members are
addressed by synthetic IDs, never by PID, name or pattern.

Proofs issued here are marked simulated=True and sealed with a per-process test
key. Only a Verifier built by make_test_verifier() accepts them. The production
verifier in scripts/containment_contract.py refuses them by design.

Escape is SELF-REPORTED. attempt_escape() is a declaration by the test harness.
The simulator does not observe any reparenting, cgroup write or namespace change.
Occupancy counts only members the harness registered through this module.
"""
import os
import secrets
import select
import signal
import subprocess
import sys
import time
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.append(str(_SCRIPTS))
import containment_contract as contract  # noqa: E402

SIM_VERIFIER_ID = 'sim-test-verifier'
SIM_KEY = secrets.token_bytes(32)  # generated per test process, never written to disk
READY_TIMEOUT_S = 10.0


def make_test_verifier(clock=time.time_ns) -> contract.Verifier:
    """Test-only verifier. It trusts the simulator key and accepts simulated proofs."""
    return contract.Verifier(anchors={SIM_VERIFIER_ID: SIM_KEY}, clock=clock, allow_simulated=True)


SIM_VERIFIER = make_test_verifier()

# Synthetic child: installs its SIGTERM disposition, reports ready on stdout,
# then sleeps. The trailing marker argument makes every child identifiable in
# /proc/<pid>/cmdline for the leak check. The marker is never used to signal.
CHILD_SRC = r'''
import signal, sys, time
mode, seconds = sys.argv[1], float(sys.argv[2])
if 'ignore-term' in mode:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.stdout.write('ready\n')
sys.stdout.flush()
time.sleep(seconds)
'''


class ObservationError(RuntimeError):
    """The boundary could not be observed; any verdict must be UNKNOWN."""


class SpawnRegistry:
    """The only place this module creates OS processes. Keeps every Popen handle."""

    def __init__(self):
        self.marker = 'sim-child-' + uuid.uuid4().hex
        self.procs: List[subprocess.Popen] = []

    def spawn(self, mode: str, *, seconds: float = 60.0, detach: bool = False) -> subprocess.Popen:
        if mode not in ('sleep', 'ignore-term'):
            raise ValueError('unknown synthetic child mode')
        argv = [sys.executable, '-I', '-c', CHILD_SRC, mode, str(seconds), self.marker]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=detach, close_fds=True)
        self.procs.append(proc)
        try:
            readable, _, _ = select.select([proc.stdout], [], [], READY_TIMEOUT_S)
            ready = proc.stdout.readline() if readable else b''
        finally:
            proc.stdout.close()
        if ready != b'ready\n':
            proc.kill()
            proc.wait(timeout=5)
            raise RuntimeError('synthetic child did not report ready within %.0fs' % READY_TIMEOUT_S)
        return proc

    def reap_all(self, timeout: float = 5.0) -> List[int]:
        """SIGKILL and reap every Popen child this registry created; return unreaped PIDs."""
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
        leaked = []
        for proc in self.procs:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                leaked.append(proc.pid)
        return leaked


def marked_processes(marker: str) -> List[int]:
    """Read-only /proc observation of any live process carrying the marker."""
    found = []
    needle = marker.encode('ascii')
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = Path(entry.path, 'cmdline').read_bytes()
        except OSError:
            continue
        if needle in cmdline:
            found.append(int(entry.name))
    return found


@dataclass
class Member:
    member_id: str
    parent: Optional[str]
    proc: Optional[subprocess.Popen] = None
    ignore_sigterm: bool = False
    detached: bool = False
    escaped: bool = False
    forked_during_cancel: bool = False
    term_sent_at: Optional[float] = None
    kill_attempted: bool = False
    exited: bool = False


class SimBoundary:
    """OS-level boundary model. Persists across supervisor (Controller) restarts."""

    def __init__(self, registry: Optional[SpawnRegistry], run_id: str, attempt_id: str, *,
                 real: bool = True, journal: Optional[list] = None):
        if real and registry is None:
            raise ValueError('real mode requires a SpawnRegistry')
        self.real = real
        self.registry = registry
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.boundary_id = 'sim-' + uuid.uuid4().hex
        self.members: Dict[str, Member] = {}
        self.leader_id: Optional[str] = None
        self.epoch = 0
        self.breaches: List[str] = []
        self.failing_ids = set()        # signal delivery fails for these members
        self.observation_fault = False  # procfs-equivalent observation unavailable
        self.cancelling = False
        self.signal_log: List[tuple] = []
        self.on_signal = None           # test hook, called after each delivered signal
        self.journal = journal if journal is not None else []
        self.released = False
        self.last_known_alive = 0
        self._counter = 0

    # --- membership -------------------------------------------------------

    def _member(self, member_id: str) -> Member:
        if member_id not in self.members:
            raise KeyError('unknown boundary member')
        return self.members[member_id]

    def _start(self, parent: Optional[str], ignore_sigterm: bool, detached: bool, seconds: float) -> str:
        self._counter += 1
        member_id = 'm%d' % self._counter
        proc = None
        if self.real:
            mode = 'ignore-term' if ignore_sigterm else 'sleep'
            proc = self.registry.spawn(mode, seconds=seconds, detach=detached)
        self.members[member_id] = Member(member_id, parent, proc, ignore_sigterm, detached)
        return member_id

    def spawn_leader(self, *, seconds: float = 60.0, ignore_sigterm: bool = False) -> str:
        if self.leader_id is not None:
            raise RuntimeError('boundary already has a leader')
        self.leader_id = self._start(None, ignore_sigterm, False, seconds)
        return self.leader_id

    def add_descendant(self, parent_id: str, *, ignore_sigterm: bool = False, detached: bool = False,
                       seconds: float = 60.0) -> str:
        if self._member(parent_id).exited:
            raise RuntimeError('an exited parent cannot fork')
        return self._start(parent_id, ignore_sigterm, detached, seconds)

    def fork_during_cancel(self, parent_id: str, *, ignore_sigterm: bool = False) -> str:
        if not self.cancelling:
            raise RuntimeError('fork_during_cancel requires an active cancellation')
        member_id = self.add_descendant(parent_id, ignore_sigterm=ignore_sigterm)
        self.members[member_id].forked_during_cancel = True
        return member_id

    def adopt_orphan(self) -> str:
        """Register a harness-started member with no parent, after any completion.

        Models a process that becomes visible to the boundary in the window between
        completion and release. It is registered by the harness, not discovered.
        """
        return self._start(None, False, False, 60.0)

    def attempt_escape(self, member_id: str) -> None:
        """Record a SELF-REPORTED reparent or detach attempt. It is never a success.

        The harness declares this. Nothing in the simulator observes the OS.
        """
        member = self._member(member_id)
        member.escaped = True
        member.parent = None
        self.breaches.append(member_id)
        self.journal.append({'event': 'ESCAPE_ATTEMPT', 'member': member_id,
                             'run_id': self.run_id, 'attempt_id': self.attempt_id})

    def crash_leader(self) -> None:
        """Simulate the run leader (worker) dying mid-run; descendants are untouched."""
        member = self._member(self.leader_id)
        if member.proc is not None:
            member.proc.kill()
            member.proc.wait(timeout=5)
        member.exited = True

    # --- observation ------------------------------------------------------

    def _refresh(self) -> None:
        for member in self.members.values():
            if not member.exited and member.proc is not None and member.proc.poll() is not None:
                member.exited = True

    def alive_ids(self) -> List[str]:
        if self.observation_fault:
            raise ObservationError('boundary observation unavailable')
        self._refresh()
        alive = [mid for mid, m in self.members.items() if not m.exited]
        self.last_known_alive = len(alive)
        return alive

    def occupancy(self) -> int:
        return len(self.alive_ids())

    # --- signalling (only to members this boundary spawned) ----------------

    def signal(self, member_id: str, sig: int) -> bool:
        member = self._member(member_id)
        if member_id in self.failing_ids:
            self.signal_log.append((member_id, signal.Signals(sig).name, 'FAILED'))
            raise OSError('injected cleanup failure for member')
        if member.exited:
            return True
        if self.real:
            if member.proc is None:
                raise RuntimeError('real boundary member has no spawned handle')
            if member.proc.poll() is None:
                member.proc.send_signal(sig)
        elif sig == signal.SIGKILL or (sig == signal.SIGTERM and not member.ignore_sigterm):
            member.exited = True
        self.signal_log.append((member_id, signal.Signals(sig).name, 'SENT'))
        if self.on_signal is not None:
            self.on_signal(self, member_id, sig)
        return True

    def close(self) -> None:
        self.released = True


@dataclass(frozen=True)
class TerminationReport:
    signals_sent: int
    alive_after: Optional[int]

    @property
    def complete(self) -> bool:
        return self.alive_after == 0


class Controller:
    """Supervisor state machine. Its in-memory state is lost on crash; the boundary journal survives."""

    def __init__(self, boundary: SimBoundary, *, state: str = 'RUNNING',
                 verifier: Optional[contract.Verifier] = None):
        self.boundary = boundary
        self.state = state
        self.verifier = verifier if verifier is not None else SIM_VERIFIER
        self.expectation: Optional[contract.Expectation] = None
        self.last_proof: Optional[contract.ContainmentProof] = None

    @classmethod
    def recover(cls, boundary: SimBoundary, *, verifier: Optional[contract.Verifier] = None) -> 'Controller':
        """Rebuild supervisor state from the durable journal. Never yields a proof or release."""
        last = boundary.journal[-1]['event'] if boundary.journal else None
        state = 'RELEASED' if last == 'RELEASED' else 'UNKNOWN'
        boundary.journal.append({'event': 'RECOVERED', 'state': state,
                                 'run_id': boundary.run_id, 'attempt_id': boundary.attempt_id})
        return cls(boundary, state=state, verifier=verifier)

    def _log(self, event: str, **fields) -> None:
        entry = {'event': event, 'run_id': self.boundary.run_id, 'attempt_id': self.boundary.attempt_id}
        entry.update(fields)
        self.boundary.journal.append(entry)

    def _invalidate(self, reason: str) -> None:
        self.state = 'UNKNOWN'
        self.last_proof = None
        self.expectation = None
        self._log('INVALIDATED', reason=reason)

    def observe_leader(self) -> str:
        """Leader exit is not proof. Any unplanned leader exit makes the run UNKNOWN."""
        leader = self.boundary.members[self.boundary.leader_id]
        self.boundary._refresh()
        if leader.exited and self.state in ('RUNNING', 'TERMINATING'):
            self._invalidate('leader exited before containment was verified')
        return self.state

    def terminate(self, *, grace: float = 0.3, budget: float = 5.0,
                  max_signals: Optional[int] = None) -> TerminationReport:
        if self.state == 'RELEASED':
            raise RuntimeError('boundary already released')
        boundary = self.boundary
        self.state = 'TERMINATING'
        self.last_proof = None
        self.expectation = None
        boundary.cancelling = True
        self._log('TERMINATE_BEGIN')
        deadline = time.monotonic() + budget
        sent = 0
        alive_after: Optional[int] = None
        while True:
            if max_signals is not None and sent >= max_signals:
                break
            try:
                alive = boundary.alive_ids()
            except ObservationError:
                self._log('OBSERVATION_FAILED', phase='terminate')
                break
            if not alive or time.monotonic() > deadline:
                break
            now = time.monotonic()
            for member_id in alive:
                if max_signals is not None and sent >= max_signals:
                    break
                member = boundary.members[member_id]
                if member.term_sent_at is None:
                    member.term_sent_at = now
                    try:
                        boundary.signal(member_id, signal.SIGTERM)
                        sent += 1
                    except OSError:
                        self._log('SIGNAL_FAILED', member=member_id, sig='SIGTERM')
                elif now - member.term_sent_at >= grace and not member.kill_attempted:
                    member.kill_attempted = True
                    try:
                        boundary.signal(member_id, signal.SIGKILL)
                        sent += 1
                    except OSError:
                        self._log('SIGNAL_FAILED', member=member_id, sig='SIGKILL')
            time.sleep(0.01)
        try:
            alive_after = len(boundary.alive_ids())
        except ObservationError:
            alive_after = None
        self._log('TERMINATE_END', signals=sent, alive_after=alive_after)
        return TerminationReport(sent, alive_after)

    def verify(self, *, interrupt: bool = False) -> Optional[contract.ContainmentProof]:
        boundary = self.boundary
        boundary.epoch += 1
        self.last_proof = None
        self.expectation = None
        if interrupt:
            self.state = 'UNKNOWN'
            self._log('VERIFY_INTERRUPTED', epoch=boundary.epoch)
            return None
        # The challenge comes from the verifier, never from the simulator itself.
        challenge = self.verifier.issue_challenge(run_id=boundary.run_id, attempt_id=boundary.attempt_id,
                                                  boundary_id=boundary.boundary_id, epoch=boundary.epoch)
        try:
            occupancy = boundary.occupancy()
            if boundary.breaches:
                verdict = contract.VERDICT_BREACHED
            else:
                verdict = contract.VERDICT_EMPTY if occupancy == 0 else contract.VERDICT_OCCUPIED
        except ObservationError:
            occupancy = boundary.last_known_alive
            verdict = contract.VERDICT_UNKNOWN
        proof = contract.seal(contract.ContainmentProof(
            run_id=boundary.run_id, attempt_id=boundary.attempt_id, boundary_id=boundary.boundary_id,
            verifier_id=SIM_VERIFIER_ID, observed_at_ns=self.verifier.now_ns(), nonce=challenge.nonce,
            epoch=challenge.epoch, occupancy=occupancy, verdict=verdict, simulated=True), SIM_KEY)
        self.expectation = challenge
        self.last_proof = proof
        self.state = 'PROOF_ISSUED'
        self._log('VERIFIED', verdict=verdict, occupancy=occupancy, epoch=boundary.epoch)
        return proof

    def complete(self) -> contract.Decision:
        if self.state == 'RELEASED':
            return contract.Decision(False, 'already released')
        decision = self._completion_decision()
        if decision.allowed:
            self.state = 'COMPLETED'
        self._log('COMPLETE' if decision.allowed else 'COMPLETE_REFUSED', reason=decision.reason)
        return decision

    def _completion_decision(self) -> contract.Decision:
        if self.boundary.breaches:
            return contract.Decision(False, 'self-reported escape recorded: boundary breached')
        if self.last_proof is None or self.expectation is None:
            return contract.Decision(False, 'no verification proof: verification incomplete or interrupted')
        check = self.verifier.preview(self.last_proof, self.expectation)
        if not check.allowed:
            return check
        try:
            occupancy = self.boundary.occupancy()
        except ObservationError:
            return contract.Decision(False, 'observation failed at completion re-check')
        if occupancy:
            return contract.Decision(False, '%d descendant(s) observed at completion re-check' % occupancy)
        return contract.Decision(True, 'completed: verified empty and re-checked')

    def release(self, proof: Optional[contract.ContainmentProof] = None) -> contract.Decision:
        if self.state != 'COMPLETED':
            return contract.Decision(False, 'release refused: boundary not completed')
        if proof is None:
            return contract.Decision(False, 'release refused: no proof presented')
        if proof != self.last_proof or self.expectation is None:
            return contract.Decision(False, 'release refused: proof is not the completed verification')
        if self.boundary.breaches:
            return contract.Decision(False, 'release refused: self-reported escape recorded')
        check = self.verifier.preview(proof, self.expectation)
        if not check.allowed:
            return check
        # Re-observe immediately before release. Occupancy is never taken from the earlier check.
        try:
            occupancy = self.boundary.occupancy()
        except ObservationError:
            return contract.Decision(False, 'release refused: observation failed')
        if occupancy:
            return contract.Decision(False, 'release refused: %d descendant(s) remain' % occupancy)
        consumed = self.verifier.verify_for_release(proof, self.expectation)
        if not consumed.allowed:
            return consumed
        self.boundary.close()
        self.state = 'RELEASED'
        self._log('RELEASED')
        return contract.Decision(True, 'released: boundary observed empty and verified')


class ReapingTestCase(unittest.TestCase):
    """Every synthetic child a test creates is reaped in tearDown, which asserts none remain."""

    def setUp(self):
        self.registry = SpawnRegistry()

    def tearDown(self):
        leaked = self.registry.reap_all(timeout=5)
        self.assertEqual(leaked, [], 'synthetic children not reaped: %r' % leaked)
        self.assertEqual(marked_processes(self.registry.marker), [],
                         'marked synthetic processes still visible in /proc after reap')
