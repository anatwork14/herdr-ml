"""Phase 5: experiment recovery model and deterministic fault injection.

Real disposable CPU processes only (tests/support/process_tree.py), in private
temp directories. Every process a test starts carries the test's marker in its
argv; tearDown terminates any survivor by its RECORDED identity (pid plus start
time, through a pidfd) and then asserts `pgrep -f <marker>` is empty. Nothing
is signalled by name or pattern, and no process outside the test is touched.
No queue daemon, GPU, cgroup or containment backend is used. Containment stays
UNAVAILABLE, so release is always refused.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
for path in (str(SCRIPTS), str(ROOT / 'tests')):
    if path not in sys.path:
        sys.path.insert(0, path)

import containment_backend  # noqa: E402
import run_ownership  # noqa: E402
import run_recovery  # noqa: E402
import run_registry  # noqa: E402
from support import process_tree  # noqa: E402

RegistryError = run_ownership.RegistryError
DIGEST = 'a' * 64
QUEUE = 'd' * 64
REPEATS = 3

# Injected boundaries, in pipeline order, with the run-local state the crash leaves.
BOUNDARIES = (
    ('after_intent', 'queue_intent:after_commit', None),
    ('after_bind', 'queue_bind_task:after_commit', None),
    ('after_claim', 'claim:after_commit', 'CLAIMED'),
    ('after_child_start', 'descendants:after_commit', 'RUNNING'),
    ('after_completion_record', 'finish:after_commit', 'UNKNOWN'),
    ('before_release', 'release:begin', 'UNKNOWN'),
)

DRIVER = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    sys.path.insert(0, {scripts!r})
    sys.path.insert(0, {tests!r})
    import run_registry as registry
    import run_recovery as recovery
    from support import process_tree

    CRASH = {point!r}
    HOME = Path({home!r})
    FOLDER = Path({folder!r})
    EXTERNAL = Path({external!r})
    WORK = Path({work!r})
    SCRIPT = Path({script!r})
    MARKER = {marker!r}
    DIGEST = {digest!r}
    QUEUE = {queue!r}

    def hook(point):
        if point == CRASH:
            sys.stdout.write('CRASHED ' + point + '\\n')
            sys.stdout.flush()
            os._exit(77)

    def submit(task):
        with EXTERNAL.open('a') as handle:
            handle.write(task + '\\n')
            handle.flush()
            os.fsync(handle.fileno())

    intent = registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME, _fault=hook)
    submit('task-1')
    registry.queue_bind_task(FOLDER, intent, 'task-1', _store_home=HOME, _fault=hook)
    fence = registry.queue_claim(FOLDER, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A', _store_home=HOME)
    token = registry.claim(FOLDER, 'run-1', 'project-a', DIGEST, 'QUEUED', queue_instance=QUEUE,
                           queue_task_id='task-1', queue_worker_id='worker-A', queue_fence=fence,
                           _store_home=HOME, _fault=hook)
    registry.advance(FOLDER, token, 'CLAIMED', 'DISPATCHING')
    leader = process_tree.start_tree(SCRIPT, WORK, MARKER)
    registry.record_child(FOLDER, token, leader.pid)
    registry.advance(FOLDER, token, 'DISPATCHING', 'RUNNING')
    recovery.record_descendants(FOLDER, token, process_tree.published_identities(WORK), _fault=hook)
    code = leader.wait(timeout=30)
    registry.finish(FOLDER, token, {{'state': 'UNKNOWN', 'leader_state': 'COMPLETED', 'exit_code': code,
                                    'containment': 'UNKNOWN'}}, _fault=hook)
    result = recovery.release_run(FOLDER, token, containment=recovery.production_containment(), _fault=hook)
    print('NOT_REACHED' if CRASH else 'RELEASE ' + json.dumps(result['reasons']))
''')


class RecoveryBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.marker = process_tree.new_marker()
        self.script = process_tree.write_tree_script(self.root)
        self.workdirs = []
        self.handles = []
        self.recorded = []
        self.cases = 0

    def tearDown(self):
        # Terminate only by identities this test's processes published about themselves.
        survivors = list(self.recorded)
        for work in self.workdirs:
            survivors.extend(process_tree.published_identities(work, wait=False))
        if survivors:
            run_recovery.terminate_recorded(survivors, grace_s=0.1, kill_wait_s=5.0)
        for handle in self.handles:
            if handle.poll() is None:
                handle.kill()
            handle.wait(timeout=10)
        self.assertEqual(process_tree.wait_no_marked(self.marker), [],
                         'processes started by this test are still running')

    def new_case(self):
        self.cases += 1
        base = self.root / ('case-%d' % self.cases)
        home = base / 'home'
        home.mkdir(parents=True, mode=0o700)
        folder = base / '.runtime' / 'project-a' / 'runs' / 'run-1'
        folder.mkdir(parents=True)
        work = base / 'tree'
        work.mkdir()
        self.workdirs.append(work)
        return {'home': home, 'folder': folder, 'work': work, 'external': base / 'submissions.log'}

    def run_driver(self, case, point):
        code = DRIVER.format(scripts=str(SCRIPTS), tests=str(ROOT / 'tests'), point=point,
                             home=str(case['home']), folder=str(case['folder']),
                             external=str(case['external']), work=str(case['work']),
                             script=str(self.script), marker=self.marker, digest=DIGEST, queue=QUEUE)
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=90)
        return proc

    def submissions(self, case):
        path = case['external']
        return path.read_text().splitlines() if path.exists() else []

    def local_run(self, folder):
        """A SUBMITTED-path claim owned by this (live) test process, advanced to DISPATCHING."""
        token = run_registry.claim(folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        run_registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        return token

    def start_recorded_tree(self, case, token):
        leader = process_tree.start_tree(self.script, case['work'], self.marker)
        self.handles.append(leader)
        run_registry.record_child(case['folder'], token, leader.pid)
        run_registry.advance(case['folder'], token, 'DISPATCHING', 'RUNNING')
        identities = process_tree.published_identities(case['work'])
        run_recovery.record_descendants(case['folder'], token, identities)
        self.assertEqual(leader.wait(timeout=30), 0)
        return identities


class InjectedCrashRecoveryTests(RecoveryBase):
    """Item 6: crash at each named boundary, run recovery, assert an allowed end state, x3."""

    def check_boundary(self, label, point, crashed_state):
        for iteration in range(REPEATS):
            with self.subTest(boundary=label, iteration=iteration):
                case = self.new_case()
                proc = self.run_driver(case, point)
                self.assertEqual(proc.returncode, 77, proc.stdout + proc.stderr)
                self.assertIn('CRASHED ' + point, proc.stdout)
                self.assertNotIn('NOT_REACHED', proc.stdout)
                before = run_registry.inspect(case['folder'])
                self.assertEqual(before['state'] if before else None, crashed_state)
                submitted = self.submissions(case)

                report = run_recovery.recover(case['folder'], _store_home=case['home'], grace_s=0.3)
                again = run_recovery.recover(case['folder'], _store_home=case['home'], grace_s=0.3)

                queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
                local = run_registry.inspect(case['folder'])
                self.assertFalse(report['completed'])
                self.assertEqual(queue['phase'], 'AMBIGUOUS')
                self.assertEqual(again['queue_phase'], 'AMBIGUOUS')
                if crashed_state is None:
                    self.assertIsNone(local)
                    self.assertEqual(queue['ambiguous_from'], 'INTENT' if label == 'after_intent' else 'BOUND')
                    self.assertIsNone(report['termination'])
                else:
                    self.assertEqual(report['owner'], 'GONE')
                    self.assertEqual(local['state'], 'RECOVERY_FENCED')
                    self.assertEqual(local['identity'], 'CURRENT')
                    self.assertEqual(local['fence'], 2)
                    self.assertEqual(again['local_state'], 'RECOVERY_FENCED')
                    self.assertEqual(queue['ambiguous_from'], 'CLAIMED')
                    self.assertFalse(report['release']['released'])
                    self.assertIn('CONTAINMENT_UNAVAILABLE', report['release']['reasons'])
                    if crashed_state == 'CLAIMED':
                        self.assertEqual(report['termination']['entries'], [])
                        self.assertIsNone(local['child'])
                    else:
                        self.assertTrue(report['termination']['all_gone'], report['termination'])
                        self.assertEqual(report['release']['reasons'], ['CONTAINMENT_UNAVAILABLE'])
                        self.assertEqual(len(report['termination']['entries']), 3)  # leader == parent, child, grandchild
                    events = [e['kind'] for e in run_recovery.inspect_recovery(case['folder'])['events']]
                    self.assertIn('RECOVERY_FENCED', events)
                    self.assertIn('RELEASE_REFUSED', events)
                    self.assertNotIn('RELEASE_GATE_PASSED', events)
                    if local['result'] is not None:
                        self.assertEqual(local['result']['state'], 'UNKNOWN')
                # No resubmission, no relaunch, no reclaim.
                self.assertEqual(self.submissions(case), submitted)
                self.assertLessEqual(len(submitted), 1)
                with self.assertRaises(RegistryError):
                    run_registry.queue_intent(case['folder'], 'run-1', DIGEST, QUEUE, _store_home=case['home'])
                with self.assertRaises(RegistryError):
                    run_registry.queue_claim(case['folder'], 'run-1', DIGEST, QUEUE, 'task-1', 'worker-B',
                                             _store_home=case['home'])
                for initial in ('SUBMITTED', 'QUEUED'):
                    with self.assertRaises(RegistryError):
                        run_registry.claim(case['folder'], 'run-1', 'project-a', DIGEST, initial,
                                           queue_instance=QUEUE if initial == 'QUEUED' else None,
                                           queue_task_id='task-1' if initial == 'QUEUED' else None,
                                           queue_worker_id='worker-A' if initial == 'QUEUED' else None,
                                           queue_fence=1 if initial == 'QUEUED' else None,
                                           _store_home=case['home'])
                self.assertTrue((case['folder'] / 'execution.claim').is_dir())
                self.assertEqual(process_tree.wait_no_marked(self.marker), [])

    def test_crash_after_intent(self):
        self.check_boundary(*BOUNDARIES[0])

    def test_crash_after_bind(self):
        self.check_boundary(*BOUNDARIES[1])

    def test_crash_after_claim(self):
        self.check_boundary(*BOUNDARIES[2])

    def test_crash_after_child_start(self):
        self.check_boundary(*BOUNDARIES[3])

    def test_crash_after_completion_record(self):
        self.check_boundary(*BOUNDARIES[4])

    def test_crash_before_release(self):
        self.check_boundary(*BOUNDARIES[5])

    def test_uninterrupted_worker_release_is_refused_and_recovery_cleans_up(self):
        case = self.new_case()
        proc = self.run_driver(case, None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        reasons = json.loads(proc.stdout.split('RELEASE ', 1)[1])
        # The orphaned child and grandchild are still alive when the worker asks for release.
        self.assertEqual(reasons, ['DESCENDANT_NOT_VERIFIED_GONE', 'CONTAINMENT_UNAVAILABLE'])
        self.assertEqual(run_registry.inspect(case['folder'])['state'], 'UNKNOWN')
        report = run_recovery.recover(case['folder'], _store_home=case['home'], grace_s=0.3)
        self.assertEqual(report['local_state'], 'RECOVERY_FENCED')
        self.assertTrue(report['termination']['all_gone'])
        self.assertEqual(report['release']['reasons'], ['CONTAINMENT_UNAVAILABLE'])
        self.assertEqual(self.submissions(case), ['task-1'])


class OrphanAndInterruptedLaunchTests(RecoveryBase):
    """Items 1 and 2."""

    def test_orphaned_claim_is_fenced_never_reset_to_ready(self):
        case = self.new_case()
        self.assertEqual(self.run_driver(case, 'claim:after_commit').returncode, 77)
        report = run_recovery.recover(case['folder'], _store_home=case['home'])
        local = run_registry.inspect(case['folder'])
        self.assertEqual(local['state'], 'RECOVERY_FENCED')
        self.assertNotIn(local['state'], ('CLAIMED', 'SUBMITTED', 'QUEUED', 'COMPLETED'))
        self.assertTrue((case['folder'] / 'execution.claim').is_dir())
        self.assertEqual(report['queue_phase'], 'AMBIGUOUS')
        states = [e['state'] for e in local['events']]
        self.assertEqual(states, ['CLAIMED', 'RECOVERY_FENCED'])

    def test_claim_with_live_owner_is_not_fenced_or_signalled(self):
        case = self.new_case()
        token = run_registry.claim(case['folder'], 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        report = run_recovery.recover(case['folder'], queued=False)
        self.assertEqual(report['owner'], 'LIVE')
        self.assertEqual(report['local_state'], 'CLAIMED')
        self.assertIsNone(report['termination'])
        run_registry.advance(case['folder'], token, 'CLAIMED', 'DISPATCHING')  # authority kept
        kinds = [e['kind'] for e in run_recovery.inspect_recovery(case['folder'])['events']]
        self.assertEqual(kinds, ['RECOVERY_DEFERRED'])

    def test_interrupted_launch_intent_and_bound_are_ambiguous_not_relaunched(self):
        for point, origin, submitted in (('queue_intent:after_commit', 'INTENT', []),
                                         ('queue_bind_task:after_commit', 'BOUND', ['task-1'])):
            with self.subTest(point=point):
                case = self.new_case()
                self.assertEqual(self.run_driver(case, point).returncode, 77)
                self.assertEqual(run_recovery.recover(case['folder'], _store_home=case['home'])['queue_phase'],
                                 'AMBIGUOUS')
                queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
                self.assertEqual(queue['ambiguous_from'], origin)
                self.assertIsNone(run_registry.inspect(case['folder']))
                self.assertTrue((case['folder'] / 'execution.claim').is_dir())  # fenced locally too
                self.assertEqual(self.submissions(case), submitted)
                self.assertEqual(process_tree.published_identities(case['work'], wait=False), [])
                with self.assertRaises(RegistryError):
                    run_registry.queue_resolve_ambiguous(case['folder'], None, _store_home=case['home'])


class StaleWorkerTests(RecoveryBase):
    """Item 3: a superseded fence or attempt can write neither completion nor release."""

    def assert_inert(self, folder, token, home):
        calls = (
            lambda: run_registry.advance(folder, token, 'CLAIMED', 'DISPATCHING'),
            lambda: run_registry.advance(folder, token, 'DISPATCHING', 'RUNNING'),
            lambda: run_registry.record_child(folder, token, os.getpid()),
            lambda: run_registry.finish(folder, token, {'state': 'UNKNOWN'}),
            lambda: run_registry.verified_finish(folder, token, None, {'state': 'COMPLETED'}),
            lambda: run_registry.supersede(folder, token, None),
            lambda: run_recovery.record_descendants(folder, token, [run_recovery.read_identity(os.getpid())]),
            lambda: run_recovery.release_run(folder, token, containment=run_recovery.production_containment()),
        )
        for index, call in enumerate(calls):
            with self.subTest(call=index, token=token):
                with self.assertRaises(RegistryError):
                    call()

    def test_stale_superseded_and_foreign_tokens_cannot_complete_or_release(self):
        for point in ('claim:after_commit', 'descendants:after_commit', 'finish:after_commit'):
            case = self.new_case()
            self.assertEqual(self.run_driver(case, point).returncode, 77)
            before = run_registry.inspect(case['folder'])
            stale = run_ownership.encode_token(before['attempt_id'], before['fence'])
            run_recovery.recover(case['folder'], _store_home=case['home'], grace_s=0.3)
            after = run_registry.inspect(case['folder'])
            successor = run_ownership.encode_token(after['attempt_id'], after['fence'])
            foreign = run_ownership.encode_token(run_ownership.new_attempt_id(), after['fence'])
            snapshot = (after['state'], after['fence'], after['result'], len(after['events']))
            for token in (stale, successor, foreign):
                self.assert_inert(case['folder'], token, case['home'])
            final = run_registry.inspect(case['folder'])
            self.assertEqual((final['state'], final['fence'], final['result'], len(final['events'])), snapshot)
            kinds = [e['kind'] for e in run_recovery.inspect_recovery(case['folder'])['events']]
            self.assertNotIn('RELEASE_GATE_PASSED', kinds)

    def test_recovery_fence_refuses_when_owner_changed_since_observation(self):
        case = self.new_case()
        run_registry.claim(case['folder'], 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        entry = run_registry.inspect(case['folder'])
        with self.assertRaises(RegistryError):
            run_registry.recovery_fence(case['folder'], (entry['owner_pid'] + 1, entry['owner_boot'],
                                                         entry['owner_start']), 'test')
        self.assertEqual(run_registry.inspect(case['folder'])['state'], 'CLAIMED')


    def test_interrupted_recovery_fence_commit_leaves_the_prior_row(self):
        case = self.new_case()
        run_registry.claim(case['folder'], 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        entry = run_registry.inspect(case['folder'])

        def hook(point):
            if point == 'recovery_fence:before_commit':
                raise RuntimeError('injected')

        with self.assertRaises(RuntimeError):
            run_registry.recovery_fence(case['folder'], (entry['owner_pid'], entry['owner_boot'],
                                                         entry['owner_start']), 'test', _fault=hook)
        after = run_registry.inspect(case['folder'])
        self.assertEqual((after['state'], after['fence'], after['identity']), ('CLAIMED', 1, 'CURRENT'))


class ContainmentBlocksReleaseTests(RecoveryBase):
    """Item 4: containment UNKNOWN or UNAVAILABLE refuses release even when every descendant is gone."""

    def gone_identity(self):
        proc = subprocess.Popen([sys.executable, '-I', '-c', 'import sys; sys.stdin.read()', self.marker],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        self.handles.append(proc)
        identity = run_recovery.read_identity(proc.pid, role='leader')
        proc.stdin.close()
        proc.wait(timeout=10)
        self.assertEqual(run_recovery.observe(identity)[0], run_recovery.GONE)
        return identity

    def test_unknown_unavailable_and_forged_containment_refuse_release(self):
        identity = self.gone_identity()

        class Forged:
            status = 'VERIFIED'
            proof = object()

        for containment, expected in ((None, 'CONTAINMENT_UNKNOWN'),
                                      (run_recovery.production_containment(), 'CONTAINMENT_UNAVAILABLE'),
                                      (Forged(), 'CONTAINMENT_UNKNOWN'),
                                      ({'status': 'VERIFIED'}, 'CONTAINMENT_UNKNOWN')):
            with self.subTest(containment=containment):
                reasons, _ = run_recovery.release_gate([identity], containment)
                self.assertEqual(reasons, [expected])
        with self.assertRaises(ValueError):
            containment_backend.ContainmentResult(status=containment_backend.ContainmentStatus.VERIFIED,
                                                  reason='forged', proof=object())
        self.assertEqual(run_recovery.release_gate([], None)[0],
                         ['NO_RECORDED_DESCENDANTS', 'CONTAINMENT_UNKNOWN'])

    def test_recovery_records_unavailable_containment_and_never_completes(self):
        case = self.new_case()
        self.assertEqual(self.run_driver(case, 'finish:after_commit').returncode, 77)
        run_recovery.recover(case['folder'], _store_home=case['home'], grace_s=0.3)
        refused = [e for e in run_recovery.inspect_recovery(case['folder'])['events']
                   if e['kind'] == 'RELEASE_REFUSED']
        self.assertEqual(refused[-1]['detail']['reasons'], ['CONTAINMENT_UNAVAILABLE'])
        local = run_registry.inspect(case['folder'])
        self.assertNotIn(local['state'], run_ownership.VERIFIED_OUTCOMES)
        self.assertEqual(local['result']['state'], 'UNKNOWN')


class VerifiedTerminationTests(RecoveryBase):
    """Item 5: real parent/child/grandchild; release needs every recorded descendant gone."""

    def test_release_refused_until_every_recorded_descendant_is_verified_gone(self):
        case = self.new_case()
        token = self.local_run(case['folder'])
        identities = self.start_recorded_tree(case, token)
        self.recorded.extend(identities)
        roles = {i['role']: i for i in identities}
        self.assertEqual(run_recovery.observe(roles['parent'])[0] in run_recovery.GONE_STATES, True)
        self.assertEqual(run_recovery.observe(roles['child'])[0], run_recovery.ALIVE)
        self.assertEqual(run_recovery.observe(roles['grandchild'])[0], run_recovery.ALIVE)
        run_registry.finish(case['folder'], token, {'state': 'UNKNOWN', 'leader_state': 'COMPLETED'})
        containment = run_recovery.production_containment()

        first = run_recovery.release_run(case['folder'], token, containment=containment)
        self.assertFalse(first['released'])
        self.assertIn('DESCENDANT_NOT_VERIFIED_GONE', first['reasons'])

        # SIGTERM only: the grandchild ignores it and cannot be verified gone, so release is refused.
        partial = run_recovery.terminate_recorded(run_recovery.recorded_identities(case['folder']),
                                                  grace_s=0.5, escalate=False)
        finals = {e['identity']['role']: e['final'] for e in partial['entries']}
        self.assertFalse(partial['all_gone'])
        self.assertIn(finals['child'], run_recovery.GONE_STATES)
        self.assertEqual(finals['grandchild'], run_recovery.ALIVE)
        second = run_recovery.release_run(case['folder'], token, containment=containment)
        self.assertFalse(second['released'])
        self.assertEqual(second['reasons'], ['DESCENDANT_NOT_VERIFIED_GONE', 'CONTAINMENT_UNAVAILABLE'])

        # Escalation by recorded identity: SIGKILL reaches the grandchild, then every descendant is gone.
        full = run_recovery.terminate_recorded(run_recovery.recorded_identities(case['folder']), grace_s=0.3)
        self.assertTrue(full['all_gone'], full)
        grandchild = [e for e in full['entries'] if e['identity']['role'] == 'grandchild'][0]
        self.assertIn(('SIGKILL', 'SIGNALLED'), grandchild['signals'])
        third = run_recovery.release_run(case['folder'], token, containment=containment)
        self.assertEqual(third['reasons'], ['CONTAINMENT_UNAVAILABLE'])
        self.assertFalse(third['released'])
        self.assertEqual(run_registry.inspect(case['folder'])['state'], 'UNKNOWN')
        self.assertEqual(process_tree.wait_no_marked(self.marker), [])

    def test_pid_reuse_is_detected_and_a_mismatched_identity_is_never_signalled(self):
        proc = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)', self.marker],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.handles.append(proc)
        real = run_recovery.read_identity(proc.pid)
        self.recorded.append(real)
        reused = dict(real, start_ticks=str(int(real['start_ticks']) + 1))
        self.assertEqual(run_recovery.observe(reused)[0], run_recovery.GONE)
        report = run_recovery.terminate_recorded([reused], grace_s=0.1)
        self.assertEqual(report['entries'][0]['signals'], [])
        self.assertTrue(report['all_gone'])
        self.assertIsNone(proc.poll())  # the live process with a different identity was not touched
        self.assertEqual(run_recovery.observe(real)[0], run_recovery.ALIVE)
        self.assertTrue(run_recovery.terminate_recorded([real], grace_s=1.0)['all_gone'])

    def test_identity_mismatch_after_pidfd_open_is_never_signalled(self):
        proc = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)', self.marker],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.handles.append(proc)
        real = run_recovery.read_identity(proc.pid)
        self.recorded.append(real)
        opened, sent = [], []
        real_observe, real_open = run_recovery.observe, os.pidfd_open

        def pidfd_open(pid, *args):
            opened.append(pid)
            return real_open(pid, *args)

        def observe(identity):
            # Correct identity before pidfd_open; the PID is "reused" (start time differs) after it.
            if opened:
                return run_recovery.GONE, 'PID reused: start time differs from the recorded identity'
            return real_observe(identity)

        def send(fd, sig, *args):
            sent.append(sig)
            raise AssertionError('no signal may be sent after the identity re-check fails')

        with mock.patch.object(run_recovery, 'observe', observe), \
                mock.patch.object(run_recovery.os, 'pidfd_open', pidfd_open), \
                mock.patch.object(run_recovery.signal, 'pidfd_send_signal', send):
            report = run_recovery.terminate_recorded([real], grace_s=0.1)
        self.assertEqual(opened, [proc.pid])  # the re-check after pidfd_open was reached
        self.assertEqual(sent, [])
        self.assertEqual(report['entries'][0]['signals'], [('SIGTERM', run_recovery.GONE)])
        self.assertIsNone(proc.poll())
        self.assertEqual(run_recovery.observe(real)[0], run_recovery.ALIVE)

    def test_unrecorded_process_is_not_signalled(self):
        case = self.new_case()
        token = self.local_run(case['folder'])
        identities = self.start_recorded_tree(case, token)
        self.recorded.extend(identities)
        bystander = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)', self.marker],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.handles.append(bystander)
        self.recorded.append(run_recovery.read_identity(bystander.pid))
        report = run_recovery.terminate_recorded(run_recovery.recorded_identities(case['folder']), grace_s=0.3)
        self.assertTrue(report['all_gone'])
        self.assertIsNone(bystander.poll())

    def test_malformed_or_foreign_namespace_identity_is_unverified_and_blocks_release(self):
        identity = run_recovery.read_identity(os.getpid())
        for bad in (dict(identity, start_ticks=None), dict(identity, pid_namespace='pid:[1]'),
                    {'pid': os.getpid()}):
            with self.subTest(bad=bad):
                self.assertEqual(run_recovery.observe(bad)[0], run_recovery.UNVERIFIED)
                self.assertIn('DESCENDANT_NOT_VERIFIED_GONE', run_recovery.release_gate([bad], None)[0])
                report = run_recovery.terminate_recorded([bad], grace_s=0.1)
                self.assertFalse(report['all_gone'])
                self.assertEqual(report['entries'][0]['signals'], [])

    def test_descendants_cannot_be_recorded_outside_dispatching_or_running(self):
        case = self.new_case()
        token = run_registry.claim(case['folder'], 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        with self.assertRaises(RegistryError):
            run_recovery.record_descendants(case['folder'], token, [run_recovery.read_identity(os.getpid())])
        with self.assertRaises(RegistryError):
            run_recovery.record_descendants(case['folder'], token, [{'pid': os.getpid()}])


SUBMITTER = textwrap.dedent('''
    import sys
    sys.path.insert(0, {scripts!r})
    import run_registry as registry
    intent = registry.queue_intent({folder!r}, 'run-1', {digest!r}, {queue!r}, _store_home={home!r})
    registry.queue_bind_task({folder!r}, intent, 'task-1', _store_home={home!r})
    print('BOUND', flush=True)
    sys.stdin.read()  # stays a live submitter until the test closes stdin
''')

# Registers itself as a submitter. hold_s > 0: hold the queue write lock that long before committing,
# then stay alive. hold_s == 0: try once with a short busy timeout and report whether it was locked out.
LATE_SUBMITTER = textwrap.dedent('''
    import sqlite3, sys, time
    sys.path.insert(0, {scripts!r})
    import run_registry as registry
    db = registry._queue_connect({folder!r}, {home!r})
    db.execute('PRAGMA busy_timeout=300')
    try:
        db.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as exc:
        print('LOCKED_OUT ' + str(exc), flush=True)
        sys.exit(0)
    registry._record_submitter(db, 'run-1', 'bind')
    print('LOCKED', flush=True)
    time.sleep({hold_s!r})
    db.execute('COMMIT')
    print('COMMITTED', flush=True)
    sys.stdin.read()
''')


class LaunchQuiescenceTests(RecoveryBase):
    """Fix 1: interrupted-launch fencing only when no submitter or worker of the run is active.

    Lock: the queue store write lock (BEGIN IMMEDIATE on queue-registry.sqlite3). The
    check and the fence/marker write run in one such transaction, and queue_intent and
    queue_bind_task record their submitter under the same lock.
    """

    def queue_db(self, case):
        return case['home'] / '.local' / 'state' / 'herdr-ml' / 'queue-registry.sqlite3'

    def spawn(self, source, case, **extra):
        code = source.format(scripts=str(SCRIPTS), folder=str(case['folder']), home=str(case['home']),
                             digest=DIGEST, queue=QUEUE, **extra)
        proc = subprocess.Popen([sys.executable, '-c', code, self.marker], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        self.handles.append(proc)
        self.addCleanup(proc.stdout.close)
        return proc

    def stop(self, proc):
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=30), 0)

    def bound_case(self, live):
        case = self.new_case()
        submitter = self.spawn(SUBMITTER, case)
        self.assertEqual(submitter.stdout.readline().strip(), 'BOUND')
        if not live:
            self.stop(submitter)
        self.assertEqual(run_registry.inspect_queue(case['folder'], _store_home=case['home'])['phase'], 'BOUND')
        return case, submitter

    def test_live_submitter_blocks_fence_and_leaves_bytes_unchanged(self):
        case, submitter = self.bound_case(live=True)
        before = self.queue_db(case).read_bytes()
        listing = sorted(p.name for p in case['folder'].iterdir())
        with self.assertRaises(run_registry.RecoveryDeferred) as raised:
            run_registry.queue_fence_unlaunched(case['folder'], _store_home=case['home'])
        self.assertIn('RECOVERY_DEFERRED', str(raised.exception))
        self.assertIn('submitter', str(raised.exception))
        report = run_recovery.recover(case['folder'], _store_home=case['home'])
        self.assertIn('RECOVERY_DEFERRED', report['queue_deferred'])
        self.assertEqual(report['queue_phase'], 'BOUND')
        self.assertEqual(self.queue_db(case).read_bytes(), before)
        self.assertEqual(sorted(p.name for p in case['folder'].iterdir()), listing)
        self.assertFalse((case['folder'] / 'execution.claim').exists())
        self.stop(submitter)

    def test_dead_submitter_lets_fence_proceed(self):
        case, _ = self.bound_case(live=False)
        self.assertEqual(run_registry.queue_fence_unlaunched(case['folder'], _store_home=case['home']),
                         'AMBIGUOUS')
        queue = run_registry.inspect_queue(case['folder'], _store_home=case['home'])
        self.assertEqual((queue['phase'], queue['ambiguous_from']), ('AMBIGUOUS', 'BOUND'))
        report = run_recovery.recover(case['folder'], _store_home=case['home'])
        self.assertIsNone(report['queue_deferred'])
        self.assertEqual(report['queue_phase'], 'AMBIGUOUS')
        self.assertTrue((case['folder'] / 'execution.claim').is_dir())

    def test_submitter_holding_the_queue_lock_is_seen_after_it_commits(self):
        case, _ = self.bound_case(live=False)
        late = self.spawn(LATE_SUBMITTER, case, hold_s=0.5)
        self.assertEqual(late.stdout.readline().strip(), 'LOCKED')
        # The fence blocks on the queue write lock, then its check sees the committed live submitter.
        with self.assertRaises(run_registry.RecoveryDeferred):
            run_registry.queue_fence_unlaunched(case['folder'], _store_home=case['home'])
        self.assertEqual(late.stdout.readline().strip(), 'COMMITTED')
        self.assertEqual(run_registry.inspect_queue(case['folder'], _store_home=case['home'])['phase'], 'BOUND')
        self.stop(late)

    def test_submitter_cannot_appear_between_check_and_write(self):
        case, _ = self.bound_case(live=False)
        outcomes = []

        def check_then_race(db, folder, run_id, origin, claimant):
            reason = run_registry._launch_quiescence(db, folder, run_id, origin, claimant)
            late = self.spawn(LATE_SUBMITTER, case, hold_s=0)
            outcomes.append(late.stdout.readline().strip())
            late.stdin.close()
            late.wait(timeout=30)
            return reason

        phase, marker = run_registry.queue_fence_launch(case['folder'], _store_home=case['home'],
                                                        _quiescence=check_then_race)
        self.assertEqual((phase, marker), ('AMBIGUOUS', True))
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].startswith('LOCKED_OUT'), outcomes)
        with self.assertRaises(RegistryError):
            run_registry.queue_claim(case['folder'], 'run-1', DIGEST, QUEUE, 'task-1', 'worker-B',
                                     _store_home=case['home'])


class DescendantLineageTests(RecoveryBase):
    """Fix 2: only the leader, its session or its parent chain can be recorded as descendants."""

    def test_outsider_cannot_be_recorded_and_is_never_signalled(self):
        case = self.new_case()
        token = self.local_run(case['folder'])
        leader = process_tree.start_tree(self.script, case['work'], self.marker)
        self.handles.append(leader)
        run_registry.record_child(case['folder'], token, leader.pid)
        run_registry.advance(case['folder'], token, 'DISPATCHING', 'RUNNING')
        identities = process_tree.published_identities(case['work'])
        self.recorded.extend(identities)
        outsiders = []
        for new_session in (True, False):
            proc = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)', self.marker],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    start_new_session=new_session)
            self.handles.append(proc)
            identity = run_recovery.read_identity(proc.pid, role='outsider')
            self.recorded.append(identity)
            outsiders.append((proc, identity))
        for _, outsider in outsiders:
            for batch in ([outsider], identities + [outsider]):
                with self.subTest(pid=outsider['pid'], size=len(batch)):
                    with self.assertRaises(RegistryError) as raised:
                        run_recovery.record_descendants(case['folder'], token, batch)
                    self.assertIn('lineage', str(raised.exception))
        self.assertEqual(run_recovery.inspect_recovery(case['folder'])['descendants'], [])
        run_recovery.record_descendants(case['folder'], token, identities)  # the real tree is accepted
        self.assertEqual(leader.wait(timeout=30), 0)
        recorded_pids = {i['pid'] for i in run_recovery.recorded_identities(case['folder'])}
        for _, outsider in outsiders:
            self.assertNotIn(outsider['pid'], recorded_pids)
        # Recovery after the owner is fenced terminates the recorded tree only.
        entry = run_registry.inspect(case['folder'])
        run_registry.recovery_fence(case['folder'], (entry['owner_pid'], entry['owner_boot'],
                                                     entry['owner_start']), 'test: owner treated as gone')
        report = run_recovery.recover(case['folder'], queued=False, grace_s=0.3)
        self.assertTrue(report['termination']['all_gone'])
        signalled = {e['identity']['pid'] for e in report['termination']['entries']}
        for proc, outsider in outsiders:
            self.assertNotIn(outsider['pid'], signalled)
            self.assertIsNone(proc.poll())
            self.assertEqual(run_recovery.observe(outsider)[0], run_recovery.ALIVE)


class SafetyInvariantTests(unittest.TestCase):
    def test_production_guards_are_unchanged(self):
        import queue_adapter
        self.assertIs(queue_adapter.PRODUCTION_DISPATCH_ENABLED, False)
        self.assertIsInstance(containment_backend.get_backend(), containment_backend.UnavailableContainmentBackend)
        self.assertEqual(run_recovery.containment_status(run_recovery.production_containment()), 'UNAVAILABLE')
        source = (SCRIPTS / 'run_recovery.py').read_text()
        for forbidden in ('containment_sim', 'cgroup.kill', 'killpg', 'pkill', 'os.kill(', '_VERIFIED_ISSUERS'):
            self.assertNotIn(forbidden, source.split('"""', 2)[2])

    def test_new_crash_points_are_named(self):
        for point in ('queue_bind_task:after_commit', 'finish:after_commit', 'descendants:after_commit',
                      'release:begin', 'recovery_fence:before_commit'):
            self.assertIn(point, run_ownership.CRASH_POINTS)


if __name__ == '__main__':
    unittest.main()
