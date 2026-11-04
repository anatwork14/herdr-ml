"""Regression tests for three LOW repairs to the private Pueue adapter.

A: only the verified private copy in <root>/bin is executed, never the caller's path.
B: reconcile reports status timeouts and OS errors as daemon_error, with no transition.
C: an enqueue that times out ends AMBIGUOUS (origin BOUND) and is never retried.
D: a non-zero (or signal-negative) enqueue exit after the daemon acted ends AMBIGUOUS, not BOUND.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_queue_adapter import (Base, DIGEST, PINNED_ERROR, PINNED_PUEUE,  # noqa: E402
                                PINNED_PUEUED, QUEUE, harness)
import queue_adapter as qa  # noqa: E402
import run_registry as registry  # noqa: E402


class StubDaemon:
    """Answers instance_id/status like a private daemon and records every client call."""

    def __init__(self, task='7', label=None, enqueue=None, status_error=None):
        self.calls = []
        self.task = task
        self.label = label
        self.enqueue = enqueue
        self.status_error = status_error

    def instance_id(self):
        return QUEUE

    def status(self):
        self.calls.append(('status',))
        if self.status_error is not None:
            raise self.status_error
        return {'tasks': {self.task: {'label': self.label, 'status': 'Stashed'}}}

    def client(self, *args, **kwargs):
        self.calls.append(args)
        if args and args[0] == 'enqueue' and self.enqueue is not None:
            raise self.enqueue
        return subprocess.CompletedProcess(list(args), 0, '', '')


class ReconcileErrorTests(Base):
    def assert_daemon_error_without_transition(self, error, text):
        folder = self.folder()
        token = self.bound(folder)
        before = self.events()
        daemon = StubDaemon(label=qa.task_label('run-1', token), status_error=error)
        report = qa.reconcile(daemon, folder, _store_home=self.home)
        self.assertEqual(report['phase'], 'BOUND')
        self.assertFalse(report['daemon_reachable'])
        self.assertFalse(report['resubmitted'])
        self.assertIn(text, report['daemon_error'])
        self.assertEqual(registry.inspect_queue(folder, _store_home=self.home)['phase'], 'BOUND')
        self.assertEqual(self.events(), before)

    def test_status_timeout_is_reported_as_daemon_error(self):
        self.assert_daemon_error_without_transition(subprocess.TimeoutExpired(['pueue'], 1), 'timed out')

    def test_status_os_error_is_reported_as_daemon_error(self):
        self.assert_daemon_error_without_transition(PermissionError('denied'), 'PermissionError')


class EnqueueTimeoutTests(Base):
    def test_timed_out_enqueue_ends_ambiguous_and_is_not_retried(self):
        folder = self.folder()
        token = self.bound(folder)
        daemon = StubDaemon(label=qa.task_label('run-1', token),
                            enqueue=subprocess.TimeoutExpired(['pueue', 'enqueue'], 1))
        with self.assertRaisesRegex(qa.AmbiguousSubmission, 'timed out'):
            qa.release(daemon, folder, _store_home=self.home)
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['phase'], row['ambiguous_from'], row['task_id']), ('AMBIGUOUS', 'BOUND', '7'))
        phases = [phase for (phase,) in self.events()]
        self.assertEqual(phases[-3:], ['ENQUEUE_ATTEMPTED', 'ENQUEUE_UNKNOWN', 'AMBIGUOUS'])
        with self.assertRaises(qa.QueueAdapterError):
            qa.release(daemon, folder, _store_home=self.home)
        report = qa.reconcile(daemon, folder, _store_home=self.home)
        self.assertEqual((report['phase'], report['resubmitted']), ('AMBIGUOUS', False))
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim_idempotent(folder, 'run-1', DIGEST, QUEUE, '7', 'worker-A',
                                            _store_home=self.home)
        self.assertEqual([c for c in daemon.calls if c[0] in ('enqueue', 'add')], [('enqueue', '7')])

    def test_crash_after_enqueue_attempt_is_fenced_by_recovery(self):
        folder = self.folder()
        token = self.bound(folder)
        registry.queue_enqueue_attempted(folder, token, _store_home=self.home)
        self.assertEqual(registry.queue_recover(folder, _store_home=self.home), 'AMBIGUOUS')
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual(row['ambiguous_from'], 'BOUND')

    def test_successful_enqueue_stays_bound_after_recovery(self):
        folder = self.folder()
        token = self.bound(folder)
        daemon = StubDaemon(label=qa.task_label('run-1', token))
        self.assertEqual(qa.release(daemon, folder, _store_home=self.home), '7')
        self.assertEqual(registry.queue_recover(folder, _store_home=self.home), 'BOUND')


@unittest.skipIf(PINNED_ERROR, 'NOT_TESTED: pinned Pueue 4.0.4 binaries unavailable (%s)' % PINNED_ERROR)
class EnqueueNonZeroExitTests(Base):
    """The real enqueue reaches the private daemon, then the client reports a non-zero exit."""

    def assert_nonzero_exit_is_ambiguous(self, code):
        daemon_root = self.root / 'daemon'
        daemon_root.mkdir(mode=0o700)
        daemon = qa.PrivatePueue(daemon_root, PINNED_PUEUE, PINNED_PUEUED)
        self.addCleanup(daemon.kill)
        daemon.start()
        folder = self.folder()
        command = harness.worker_command(sys.executable, folder, self.home, self.root / 'result.json')
        submitted = qa.submit(daemon, folder, 'run-1', DIGEST, command, _store_home=self.home)
        real_client = daemon.client
        enqueues = []

        def lost_response_client(*args, **kwargs):
            result = real_client(*args, **kwargs)
            if args and args[0] == 'enqueue':
                enqueues.append(args)
                self.assertEqual(result.returncode, 0)
                return subprocess.CompletedProcess(result.args, code, '', 'connection lost')
            return result

        daemon.client = lost_response_client
        with self.assertRaisesRegex(qa.AmbiguousSubmission, 'exit %d' % code):
            qa.release(daemon, folder, _store_home=self.home)
        self.assertNotIn('Stashed', daemon.status()['tasks'][submitted['task_id']]['status'])
        row = registry.inspect_queue(folder, _store_home=self.home)
        self.assertEqual((row['phase'], row['ambiguous_from'], row['task_id']),
                         ('AMBIGUOUS', 'BOUND', submitted['task_id']))
        phases = [phase for (phase,) in self.events()]
        self.assertEqual(phases[-3:], ['ENQUEUE_ATTEMPTED', 'ENQUEUE_UNKNOWN', 'AMBIGUOUS'])
        self.assertNotIn('ENQUEUE_REFUSED', phases)
        with self.assertRaises(qa.QueueAdapterError):
            qa.release(daemon, folder, _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_enqueue_attempted(folder, row['intent_token'], _store_home=self.home)
        self.assertEqual(enqueues, [('enqueue', submitted['task_id'])])
        self.assertEqual(len(daemon.status()['tasks']), 1)

    def test_nonzero_exit_after_real_enqueue_is_ambiguous_and_not_resubmitted(self):
        self.assert_nonzero_exit_is_ambiguous(1)

    def test_signal_negative_exit_after_real_enqueue_is_ambiguous_and_not_resubmitted(self):
        self.assert_nonzero_exit_is_ambiguous(-9)


@unittest.skipIf(PINNED_ERROR, 'NOT_TESTED: pinned Pueue 4.0.4 binaries unavailable (%s)' % PINNED_ERROR)
class PrivateBinaryCopyTests(Base):
    def test_only_the_verified_private_copy_is_executed(self):
        source = self.root / 'src'
        source.mkdir(mode=0o700)
        pueue, pueued = source / 'pueue', source / 'pueued'
        shutil.copyfile(PINNED_PUEUE, pueue)
        shutil.copyfile(PINNED_PUEUED, pueued)
        daemon_root = self.root / 'daemon'
        daemon_root.mkdir(mode=0o700)
        daemon = qa.PrivatePueue(daemon_root, pueue, pueued)
        self.addCleanup(daemon.kill)
        private_bin = daemon_root.resolve() / 'bin'
        self.assertEqual((daemon.pueue, daemon.pueued), (private_bin / 'pueue', private_bin / 'pueued'))
        self.assertEqual(os.stat(private_bin).st_mode & 0o777, 0o700)
        # Swap the originals after the check: the adapter must keep running its verified copies.
        for path in (pueue, pueued):
            path.write_text('#!/bin/sh\nexit 99\n')
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv[0])
            return subprocess.CompletedProcess(argv, 0, '{"tasks": {}}', '')

        with patch.object(qa.subprocess, 'run', side_effect=fake_run):
            daemon.client('status')
            daemon.status()
        self.assertEqual(seen, [str(private_bin / 'pueue')] * 2)
        self.assertEqual(qa.sha256_file(daemon.pueue), qa.PINNED_SHA256['pueue'])
        self.assertEqual(qa.sha256_file(daemon.pueued), qa.PINNED_SHA256['pueued'])
        daemon.start()
        self.assertEqual(os.readlink('/proc/%d/exe' % daemon.pid), str(private_bin / 'pueued'))
        self.assertEqual(daemon.stop(), (0, 0))


if __name__ == '__main__':
    unittest.main()
