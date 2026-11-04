"""P7.4: queue daemon identity on the private pinned Pueue 4.0.4 daemon.

What is checked: the socket peer (SO_PEERCRED pid, /proc exe hash, start ticks, socket inode)
against the process this code started, and every callback field against the CLAIMED queue row
(expected queue instance, task ID, worker index, run attempt, ownership fence).

What is NOT claimed: the pueue client's own connections are not bound to the probed peer, and
the queue instance is the certificate on disk. See reports/experiment-infra/QUEUE.md.
Skipped with NOT_TESTED when the pinned binaries are unavailable. Never a production daemon.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_queue_adapter import (DIGEST, PINNED_ERROR, PINNED_PUEUE, PINNED_PUEUED,  # noqa: E402
                                QUEUE)
import queue_adapter as qa  # noqa: E402
import run_registry as registry  # noqa: E402
import pueue_cpu_worker_integration as harness  # noqa: E402

SKIP = 'NOT_TESTED: pinned Pueue 4.0.4 binaries unavailable (%s)' % PINNED_ERROR


@unittest.skipIf(PINNED_ERROR, SKIP)
class DaemonIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w2id-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        os.chmod(self.root, 0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.folder = harness.run_folder(self.root / 'a', harness.PROJECT)
        self.daemon_a = self.new_daemon('a')
        self.daemon_a.start()
        self.instance_a = self.daemon_a.instance_id()
        self.submitted = qa.submit(self.daemon_a, self.folder, 'run-1', DIGEST, ['/bin/true'],
                                   _store_home=self.home)
        qa.release(self.daemon_a, self.folder, _store_home=self.home)
        qa.claim_attempt(self.folder, 'run-1', harness.PROJECT, DIGEST, self.instance_a,
                         self.submitted['task_id'], 'worker-1', _store_home=self.home)
        self.callback = qa.issue_callback(self.daemon_a, self.folder, _store_home=self.home)

    def new_daemon(self, name):
        root = self.root / ('daemon-' + name)
        root.mkdir(mode=0o700)
        daemon = qa.PrivatePueue(root, PINNED_PUEUE, PINNED_PUEUED, timeout=5.0)
        self.addCleanup(daemon.kill)
        return daemon

    def store_bytes(self):
        return (self.home / '.local' / 'state' / 'herdr-ml' / 'queue-registry.sqlite3').read_bytes()

    def verify(self, callback, daemon=None):
        return qa.verify_callback(daemon or self.daemon_a, self.folder, callback, _store_home=self.home)

    def test_genuine_callback_is_verified_against_every_expected_field(self):
        before = self.store_bytes()
        identity = self.verify(self.callback)
        self.assertEqual(identity['expected_queue_instance'], self.instance_a)
        self.assertEqual(identity['connected_queue_instance'], self.instance_a)
        self.assertEqual(identity['connected_peer']['pid'], self.daemon_a.pid)
        self.assertEqual(identity['connected_peer']['exe_sha256'],
                         qa.PINNED_SHA256['pueued'])
        self.assertEqual((identity['task_id'], identity['worker_id'], identity['attempt_id'], identity['fence']),
                         (self.submitted['task_id'], 'worker-1', self.callback['attempt_id'], 1))
        self.assertEqual(self.store_bytes(), before, 'verification never writes')

    def test_stale_or_foreign_fields_are_refused_and_write_nothing(self):
        before = self.store_bytes()
        cases = {
            'fence': dict(self.callback, fence=self.callback['fence'] - 1),
            'attempt_id': dict(self.callback, attempt_id='0' * 32),
            'task_id': dict(self.callback, task_id='9999'),
            'worker_id': dict(self.callback, worker_id='worker-2'),
            'queue_instance': dict(self.callback, queue_instance='e' * 64),
        }
        for field, callback in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(qa.QueueAdapterError, field):
                self.verify(callback)
        with self.assertRaises(qa.QueueAdapterError):
            self.verify('not a record')
        self.assertEqual(self.store_bytes(), before)

    def test_socket_replacement_by_another_private_daemon_is_refused(self):
        daemon_b = self.new_daemon('b')
        daemon_b.start()
        sock_a, sock_b = self.daemon_a.socket_path(), daemon_b.socket_path()
        backup = self.root / 'a-socket.bak'
        os.rename(sock_a, backup)
        try:
            os.rename(sock_b, sock_a)  # the path now leads to daemon B's listener
            with self.assertRaisesRegex(qa.QueueAdapterError, 'pid|socket'):
                self.verify(self.callback)
            with self.assertRaisesRegex(qa.QueueAdapterError, 'pid'):
                self.daemon_a.verify_peer()
        finally:
            if sock_a.exists():
                os.rename(sock_a, sock_b)
            os.rename(backup, sock_a)
        self.assertEqual(self.verify(self.callback)['connected_peer']['pid'], self.daemon_a.pid)

    def test_daemon_restart_invalidates_callbacks_issued_by_the_old_process(self):
        old_pid = self.daemon_a.pid
        self.daemon_a.stop()
        self.daemon_a.start()  # same state directory: same certificate, new process
        self.assertNotEqual(self.daemon_a.pid, old_pid)
        self.assertEqual(self.daemon_a.instance_id(), self.instance_a)
        with self.assertRaisesRegex(qa.QueueAdapterError, 'another daemon process'):
            self.verify(self.callback)
        fresh = qa.issue_callback(self.daemon_a, self.folder, _store_home=self.home)
        self.assertEqual(self.verify(fresh)['connected_peer']['pid'], self.daemon_a.pid)

    def test_mismatched_daemon_state_is_refused_even_when_the_peer_issued_the_callback(self):
        daemon_c = self.new_daemon('c')  # its own state directory, hence a different certificate
        daemon_c.start()
        self.assertNotEqual(daemon_c.instance_id(), self.instance_a)
        callback_from_c = qa.issue_callback(daemon_c, self.folder, _store_home=self.home)
        self.assertEqual(callback_from_c['queue_instance'], self.instance_a)  # row, not the daemon
        with self.assertRaisesRegex(qa.QueueAdapterError, 'queue instance differs'):
            self.verify(callback_from_c, daemon=daemon_c)

    def test_identity_is_never_read_from_the_environment(self):
        before = self.store_bytes()
        os.environ['PUEUE_CONFIG_PATH'] = '/nonexistent/other-daemon.yml'
        try:
            self.assertEqual(self.verify(self.callback)['connected_peer']['pid'], self.daemon_a.pid)
        finally:
            del os.environ['PUEUE_CONFIG_PATH']
        self.assertEqual(self.store_bytes(), before)

    def test_callback_requires_a_claimed_row(self):
        other = harness.run_folder(self.root / 'b', harness.PROJECT, 'run-2')
        with self.assertRaisesRegex(qa.QueueAdapterError, 'claimed'):
            qa.issue_callback(self.daemon_a, other, _store_home=self.home)


if __name__ == '__main__':
    unittest.main()
