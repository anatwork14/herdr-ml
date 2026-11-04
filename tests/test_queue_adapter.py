"""W3 queue adapter: identity binding, durable intent, idempotent claim, no replay after uncertainty.

Two layers:
- Offline tests (always run): registry and adapter logic with synthetic queue identities.
- Private-daemon tests: a pinned Pueue 4.0.4 daemon started per test inside a 0700
  temporary directory with its own config and socket. They are skipped with a
  NOT_TESTED message when the pinned binaries are missing or fail the checksum.
  There is no fallback to a system `pueue`, and the default daemon is never used.
CPU only; nothing is dispatched to a production queue.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import queue_adapter as qa  # noqa: E402
import run_registry as registry  # noqa: E402
import pueue_cpu_worker_integration as harness  # noqa: E402

DIGEST = 'a' * 64
QUEUE = 'd' * 64
BIN_DIR = Path(os.environ.get('HERDR_PUEUE_BIN_DIR',
                              '/mnt/ssd/genos2/workspace/herdr-ml-pueue-test/bin'))
PUEUE = BIN_DIR / 'pueue-x86_64-unknown-linux-musl'
PUEUED = BIN_DIR / 'pueued-x86_64-unknown-linux-musl'


def _pinned_binaries():
    try:
        return qa.verify_binary(PUEUE, 'pueue'), qa.verify_binary(PUEUED, 'pueued'), None
    except qa.BinaryUnavailable as exc:
        return None, None, str(exc)


PINNED_PUEUE, PINNED_PUEUED, PINNED_ERROR = _pinned_binaries()


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w3q-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        os.chmod(self.root, 0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)

    def folder(self, project='project-a', run_id='run-1'):
        return harness.run_folder(self.root, project, run_id)

    def bound(self, folder, run_id='run-1', task='7'):
        token = registry.queue_intent(folder, run_id, DIGEST, QUEUE, _store_home=self.home)
        registry.queue_bind_task(folder, token, task, _store_home=self.home)
        return token

    def events(self, run_id='run-1'):
        import sqlite3
        db = sqlite3.connect(str(self.home / '.local' / 'state' / 'herdr-ml' / 'queue-registry.sqlite3'))
        try:
            return db.execute('SELECT phase FROM queue_events WHERE run_id=? ORDER BY seq',
                              (run_id,)).fetchall()
        finally:
            db.close()


class OfflineAdapterTests(Base):
    def test_missing_binary_is_refused_without_path_fallback(self):
        with self.assertRaises(qa.BinaryUnavailable):
            qa.verify_binary(self.root / 'absent', 'pueue')
        with self.assertRaisesRegex(qa.BinaryUnavailable, 'refusing to search PATH'):
            qa.verify_binary(None, 'pueue')
        source = (SCRIPTS / 'queue_adapter.py').read_text()
        self.assertNotIn('shutil.which', source)

    def test_checksum_mismatch_is_refused(self):
        fake = self.root / 'pueue'
        fake.write_text('#!/bin/sh\necho 0\n')
        fake.chmod(0o700)
        with self.assertRaisesRegex(qa.BinaryUnavailable, 'checksum mismatch'):
            qa.verify_binary(fake, 'pueue')

    def test_private_root_must_be_owner_only(self):
        loose = self.root / 'loose'
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        if PINNED_ERROR:
            with self.assertRaises(qa.BinaryUnavailable):
                qa.PrivatePueue(loose, PUEUE, PUEUED)
            return
        with self.assertRaisesRegex(qa.QueueAdapterError, '0700'):
            qa.PrivatePueue(loose, PUEUE, PUEUED)

    def test_submit_accepts_only_a_private_daemon(self):
        self.assertFalse(qa.PRODUCTION_DISPATCH_ENABLED)
        with self.assertRaisesRegex(qa.QueueAdapterError, 'production dispatch is disabled'):
            qa.submit(object(), self.folder(), 'run-1', DIGEST, ['true'], _store_home=self.home)
        self.assertIsNone(registry.inspect_queue(self.folder(), _store_home=self.home))

    def test_researchctl_queued_path_stays_blocked_and_does_not_use_adapter(self):
        spec = importlib.util.spec_from_file_location('researchctl_w3', SCRIPTS / 'researchctl.py')
        researchctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(researchctl)
        with self.assertRaisesRegex(researchctl.ResearchError, 'Queued execution BLOCKED'):
            researchctl.require_supported_execution('diagnostic', 'gpu0')
        with self.assertRaisesRegex(researchctl.ResearchError, 'Queued execution BLOCKED'):
            researchctl.require_supported_execution('diagnostic', 'gpu0', submission=True)
        self.assertNotIn('queue_adapter', (SCRIPTS / 'researchctl.py').read_text())

    def test_claim_is_idempotent_for_the_same_claimant_and_writes_nothing(self):
        folder = self.folder()
        self.bound(folder)
        attempt, fence, replayed = registry.queue_claim_idempotent(
            folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A', _store_home=self.home)
        self.assertFalse(replayed)
        before = self.events()
        again = registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                                _store_home=self.home)
        self.assertEqual(again, (attempt, fence, True))
        self.assertEqual(self.events(), before)
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['phase'], row['attempt_id'], row['fence']), ('CLAIMED', attempt, fence))

    def test_claim_is_never_stolen_by_another_worker_or_process(self):
        folder = self.folder()
        self.bound(folder)
        attempt, fence, _ = registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                                            _store_home=self.home)
        with self.assertRaisesRegex(registry.RegistryError, 'never stolen'):
            registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-B',
                                            _store_home=self.home)
        with patch.object(registry, '_this_claimant', return_value='1:boot:2'):
            with self.assertRaisesRegex(registry.RegistryError, 'never stolen'):
                registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                                _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '8', 'worker-A',
                                            _store_home=self.home)
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['attempt_id'], row['fence'], row['worker_id']), (attempt, fence, 'worker-A'))

    def test_intent_and_ambiguous_rows_cannot_be_claimed(self):
        folder = self.folder()
        token = registry.queue_intent(folder, 'run-1', DIGEST, QUEUE, _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                            _store_home=self.home)
        registry.queue_mark_ambiguous(folder, token, 'lost response', _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_bind_task(folder, token, '7', _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                            _store_home=self.home)
        self.assertEqual(registry.inspect_queue(folder, _store_home=self.home)['phase'], 'AMBIGUOUS')

    def test_claim_attempt_binds_queue_identity_to_run_and_attempt_idempotently(self):
        folder = self.folder()
        self.bound(folder)
        first = qa.claim_attempt(folder, 'run-1', 'project-a', DIGEST, QUEUE, '7', 'worker-A',
                                 _store_home=self.home)
        second = qa.claim_attempt(folder, 'run-1', 'project-a', DIGEST, QUEUE, '7', 'worker-A',
                                  _store_home=self.home)
        self.assertFalse(first['replayed'])
        self.assertTrue(second['replayed'])
        self.assertEqual((first['token'], first['attempt_id'], first['fence']),
                         (second['token'], second['attempt_id'], second['fence']))
        local = registry.inspect(folder)
        self.assertEqual([e['state'] for e in local['events']], ['CLAIMED'])
        binding = qa.bound_identity(folder, _store_home=self.home)
        self.assertEqual((binding['run_id'], binding['queue_instance'], binding['task_id'],
                          binding['attempt_id'], binding['fence']),
                         ('run-1', QUEUE, '7', first['attempt_id'], first['fence']))
        self.assertEqual(local['identity'], 'CURRENT')

    def test_bound_identity_refuses_disagreeing_stores(self):
        folder = self.folder()
        self.bound(folder)
        with self.assertRaisesRegex(qa.QueueAdapterError, 'No claimed'):
            qa.bound_identity(folder, _store_home=self.home)
        registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                        _store_home=self.home)
        with self.assertRaisesRegex(qa.QueueAdapterError, 'disagree'):
            qa.bound_identity(folder, _store_home=self.home)


@unittest.skipIf(PINNED_ERROR is not None, 'NOT_TESTED: pinned Pueue %s unavailable (%s); '
                 'no system pueue fallback' % (qa.PUEUE_VERSION, PINNED_ERROR))
class PrivateDaemonTests(Base):
    """Every test starts its own daemon under a fresh 0700 directory and stops only that daemon."""

    def setUp(self):
        super().setUp()
        self.daemon_root = self.root / 'daemon'
        self.daemon_root.mkdir(mode=0o700)
        self.daemon = self.new_daemon()

    def new_daemon(self, timeout=5.0):
        daemon = qa.PrivatePueue(self.daemon_root, PINNED_PUEUE, PINNED_PUEUED, timeout=timeout)
        self.addCleanup(daemon.kill)
        return daemon

    def command(self, folder, name='result.json'):
        return harness.worker_command(sys.executable, folder, self.home, self.root / name)

    def tasks(self, daemon=None):
        return (daemon or self.daemon).status()['tasks']

    def test_private_daemon_is_isolated(self):
        self.daemon.start()
        env = self.daemon.env()
        self.assertEqual(env['PUEUE_CONFIG_PATH'], str(self.daemon.config))
        self.assertTrue(self.daemon.socket_path().is_socket())
        self.assertIn(self.daemon_root.resolve(), self.daemon.socket_path().resolve().parents)
        for key, value in env.items():
            if key.startswith('XDG') or key == 'HOME':
                self.assertIn(self.daemon_root.resolve(), Path(value).parents)
        self.assertEqual(self.daemon.stop(), (0, 0))
        self.assertFalse(self.daemon.socket_path().exists())

    def test_intent_is_durable_before_submission(self):
        self.daemon.start()
        folder = self.folder()
        seen = []
        real_client = self.daemon.client

        def observing_client(*args, **kwargs):
            if args and args[0] == 'add':
                seen.append(registry.inspect_queue(folder, _store_home=self.home))
            return real_client(*args, **kwargs)

        self.daemon.client = observing_client
        result = qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder), _store_home=self.home)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['phase'], 'INTENT')
        self.assertEqual(seen[0]['intent_token'], result['intent_token'])
        self.assertEqual(seen[0]['queue_instance'], self.daemon.instance_id())
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['phase'], row['task_id']), ('BOUND', result['task_id']))
        task = self.tasks()[result['task_id']]
        self.assertEqual(task['label'], qa.task_label('run-1', result['intent_token']))
        self.assertIn('Stashed', task['status'])

    def test_end_to_end_worker_claim_binds_task_to_attempt(self):
        self.daemon.start()
        folder = self.folder()
        submitted = qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder),
                              _store_home=self.home)
        qa.release(self.daemon, folder, _store_home=self.home)
        result = harness.wait_for(self.root / 'result.json', self.daemon)
        self.assertEqual(result['task_id'], submitted['task_id'])
        self.assertEqual(result['first']['attempt_id'], result['second']['attempt_id'])
        self.assertTrue(result['second']['replayed'])
        self.assertEqual(result['state'], 'UNKNOWN')
        binding = qa.bound_identity(folder, _store_home=self.home)
        self.assertEqual((binding['task_id'], binding['queue_instance'], binding['attempt_id']),
                         (submitted['task_id'], self.daemon.instance_id(), result['first']['attempt_id']))
        with self.assertRaises(registry.RegistryError):
            qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder), _store_home=self.home)
        self.assertEqual(len(self.tasks()), 1)

    def test_failing_daemon_not_running_fences_ambiguous_and_never_retries(self):
        self.daemon.start()
        instance = self.daemon.instance_id()
        self.assertEqual(self.daemon.stop(), (0, 0))
        folder = self.folder()
        with self.assertRaises(qa.AmbiguousSubmission):
            qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder), _store_home=self.home)
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['phase'], row['ambiguous_from'], row['task_id'], row['queue_instance']),
                         ('AMBIGUOUS', 'INTENT', None, instance))
        with self.assertRaises(registry.RegistryError):
            qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder), _store_home=self.home)
        with self.assertRaises(qa.QueueAdapterError):
            qa.release(self.daemon, folder, _store_home=self.home)
        restarted = self.new_daemon()
        restarted.start()
        report = qa.reconcile(restarted, folder, _store_home=self.home)
        self.assertEqual(report['phase'], 'AMBIGUOUS')
        self.assertFalse(report['resubmitted'])
        self.assertEqual((report['daemon_task_count'], report['tasks_for_intent']), (0, []))
        self.assertEqual(self.tasks(restarted), {})

    def test_failing_daemon_stalled_times_out_and_is_fenced(self):
        self.daemon = self.new_daemon(timeout=2.0)
        self.daemon.start()
        folder = self.folder()
        os.kill(self.daemon.pid, signal.SIGSTOP)
        try:
            with self.assertRaisesRegex(qa.AmbiguousSubmission, 'timed out'):
                qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder), _store_home=self.home)
        finally:
            os.kill(self.daemon.pid, signal.SIGCONT)
        self.assertEqual(registry.inspect_queue(folder, _store_home=self.home)['phase'], 'AMBIGUOUS')
        time.sleep(0.3)
        count = len(self.tasks())
        report = qa.reconcile(self.daemon, folder, _store_home=self.home)
        self.assertEqual(report['phase'], 'AMBIGUOUS')
        self.assertEqual(len(self.tasks()), count)
        self.assertLessEqual(count, 1)

    def test_lost_response_after_daemon_accepted_is_ambiguous_and_reconcile_does_not_resubmit(self):
        self.daemon.start()
        folder = self.folder()
        real_client = self.daemon.client

        def lossy_client(*args, **kwargs):
            result = real_client(*args, **kwargs)
            if args and args[0] == 'add':
                return subprocess.CompletedProcess(result.args, 0, 'garbled\n', '')
            return result

        self.daemon.client = lossy_client
        with self.assertRaisesRegex(qa.AmbiguousSubmission, 'unparseable'):
            qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder), _store_home=self.home)
        self.daemon.client = real_client
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['phase'], row['task_id']), ('AMBIGUOUS', None))
        for _ in range(2):
            report = qa.reconcile(self.daemon, folder, _store_home=self.home)
            self.assertEqual(report['phase'], 'AMBIGUOUS')
            self.assertEqual(len(report['tasks_for_intent']), 1)
            self.assertIsNone(report['bound_task_present'])
            self.assertEqual(report['daemon_task_count'], 1)
        task_id = report['tasks_for_intent'][0]
        self.assertIn('Stashed', self.tasks()[task_id]['status'])
        with self.assertRaises(qa.QueueAdapterError):
            qa.release(self.daemon, folder, _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim_idempotent(folder, 'run-1', DIGEST, row['queue_instance'], task_id,
                                            'worker-A', _store_home=self.home)

    def test_restart_after_crash_before_submission_fences_intent_without_resubmitting(self):
        self.daemon.start()
        folder = self.folder()
        registry.queue_intent(folder, 'run-1', DIGEST, self.daemon.instance_id(), _store_home=self.home)
        self.daemon.kill()
        restarted = self.new_daemon()
        restarted.start()
        report = qa.reconcile(restarted, folder, _store_home=self.home)
        self.assertEqual((report['phase'], report['instance_matches'], report['daemon_task_count']),
                         ('AMBIGUOUS', True, 0))
        self.assertEqual(self.tasks(restarted), {})

    def test_restart_after_binding_reads_daemon_state_and_does_not_resubmit(self):
        self.daemon.start()
        folder = self.folder()
        submitted = qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder),
                              _store_home=self.home)
        self.daemon.kill()
        restarted = self.new_daemon()
        restarted.start()
        self.assertEqual(restarted.instance_id(), submitted['queue_instance'])
        for _ in range(2):
            report = qa.reconcile(restarted, folder, _store_home=self.home)
            self.assertEqual(report['phase'], 'BOUND')
            self.assertTrue(report['instance_matches'])
            self.assertTrue(report['bound_task_present'])
            self.assertEqual(report['tasks_for_intent'], [submitted['task_id']])
            self.assertEqual(report['daemon_task_count'], 1)
        qa.release(restarted, folder, _store_home=self.home)
        result = harness.wait_for(self.root / 'result.json', restarted)
        self.assertEqual(result['task_id'], submitted['task_id'])
        self.assertEqual(len(self.tasks(restarted)), 1)

    def test_restart_after_claim_keeps_claim_and_binding(self):
        self.daemon.start()
        folder = self.folder()
        submitted = qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder),
                              _store_home=self.home)
        qa.release(self.daemon, folder, _store_home=self.home)
        harness.wait_for(self.root / 'result.json', self.daemon)
        before = qa.bound_identity(folder, _store_home=self.home)
        self.daemon.kill()
        restarted = self.new_daemon()
        restarted.start()
        report = qa.reconcile(restarted, folder, _store_home=self.home)
        self.assertEqual((report['phase'], report['bound_task_present'], report['daemon_task_count']),
                         ('CLAIMED', True, 1))
        self.assertEqual(qa.bound_identity(folder, _store_home=self.home), before)
        self.assertEqual(before['task_id'], submitted['task_id'])

    def test_replaced_daemon_state_is_a_different_queue_instance(self):
        self.daemon.start()
        folder = self.folder()
        submitted = qa.submit(self.daemon, folder, 'run-1', DIGEST, self.command(folder),
                              _store_home=self.home)
        self.assertEqual(self.daemon.stop(), (0, 0))
        other_root = self.root / 'other-daemon'
        other_root.mkdir(mode=0o700)
        other = qa.PrivatePueue(other_root, PINNED_PUEUE, PINNED_PUEUED)
        self.addCleanup(other.kill)
        other.start()
        self.assertNotEqual(other.instance_id(), submitted['queue_instance'])
        with self.assertRaisesRegex(qa.QueueAdapterError, 'instance'):
            qa.release(other, folder, _store_home=self.home)
        report = qa.reconcile(other, folder, _store_home=self.home)
        self.assertEqual((report['phase'], report['instance_matches'], report['tasks_for_intent']),
                         ('BOUND', False, []))
        self.assertEqual(self.tasks(other), {})

    def test_harness_script_passes_against_private_daemon(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'pueue_cpu_worker_integration.py'),
                                 '--pueue', str(PINNED_PUEUE), '--pueued', str(PINNED_PUEUED),
                                 '--python', sys.executable], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        summary = json.loads(result.stdout)
        self.assertTrue(summary['attempt_bound'])
        self.assertTrue(summary['claim_replayed_without_write'])
        self.assertEqual((summary['local_state'], summary['daemon_exit']), ('UNKNOWN', 0))


if __name__ == '__main__':
    unittest.main()
