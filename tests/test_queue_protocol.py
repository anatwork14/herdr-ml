"""CPU-only queue identity, enqueue ambiguity, and worker fencing tests."""
import importlib.util
import multiprocessing
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts' / 'run_registry.py'
spec = importlib.util.spec_from_file_location('queue_registry', path)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)

RUN = 'run-1'
DIGEST = 'a' * 64
QUEUE = 'd' * 64
OTHER_QUEUE = 'e' * 64


def claim_worker(folder, start, results, task='task-1', worker='worker-1'):
    start.wait()
    try:
        results.put(('won', registry.queue_claim(Path(folder), RUN, DIGEST, QUEUE, task, worker)))
    except registry.RegistryError:
        results.put(('lost', None))


class QueueProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / 'home'
        home.mkdir(mode=0o700)
        account = registry.pwd.getpwuid(os.geteuid())
        fake_account = registry.pwd.struct_passwd((account.pw_name, account.pw_passwd,
            account.pw_uid, account.pw_gid, account.pw_gecos, str(home), account.pw_shell))
        original_getpwuid = registry.pwd.getpwuid
        registry.pwd.getpwuid = lambda uid: fake_account
        self.addCleanup(setattr, registry.pwd, 'getpwuid', original_getpwuid)
        runtime = Path(temp.name) / '.runtime'
        self.folder = runtime / 'project-a' / 'runs' / 'run-1'
        self.folder.mkdir(parents=True)

    def test_durable_intent_task_binding_and_worker_fence(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'INTENT')
        registry.queue_bind_task(self.folder, token, 'task-42')
        before = registry.inspect_queue(self.folder)
        self.assertEqual(before['phase'], 'BOUND')
        fence = registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, 'task-42', 'worker-A')
        self.assertTrue(fence)
        claimed = registry.inspect_queue(self.folder)
        self.assertEqual(claimed['worker_id'], 'worker-A')
        self.assertEqual(claimed['fence'], fence)
        self.assertEqual(claimed['task_id'], 'task-42')

    def test_duplicate_intent_cannot_retry_external_enqueue(self):
        registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        with self.assertRaisesRegex(registry.RegistryError, 'never retry'):
            registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)

    def test_store_is_mode_0600_and_ignores_home_environment(self):
        registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        store = Path(registry.pwd.getpwuid(os.geteuid()).pw_dir) / '.local/state/herdr-ml/queue-registry.sqlite3'
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)
        with patch.dict(os.environ, {'HOME': '/tmp/untrusted-home'}):
            self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'INTENT')

    def test_store_symlink_is_not_followed(self):
        home = Path(registry.pwd.getpwuid(os.geteuid()).pw_dir)
        directory = home / '.local/state/herdr-ml'
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        store = directory / 'queue-registry.sqlite3'
        outside = home / 'outside.sqlite3'
        outside.write_bytes(b'untouched')
        store.symlink_to(outside)
        with self.assertRaises(OSError):
            registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        self.assertEqual(outside.read_bytes(), b'untouched')

    def test_directory_fsync_failure_prevents_intent_return(self):
        with patch.object(registry.os, 'fsync', side_effect=OSError('injected fsync failure')):
            with self.assertRaises(registry.RegistryError):
                registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        self.assertIsNone(registry.inspect_queue(self.folder))

    def test_task_id_unique_in_same_queue_and_namespaced_across_daemons(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        registry.queue_bind_task(self.folder, token, 'task-global')
        other = self.folder.parents[2] / 'project-b' / 'runs' / 'run-2'
        other.mkdir(parents=True)
        token2 = registry.queue_intent(other, 'run-2', 'b' * 64, QUEUE)
        with self.assertRaisesRegex(registry.RegistryError, 'already bound'):
            registry.queue_bind_task(other, token2, 'task-global')
        self.assertEqual(registry.inspect_queue(other)['phase'], 'INTENT')
        other3 = self.folder.parents[2] / 'project-c' / 'runs' / 'run-3'
        other3.mkdir(parents=True)
        other_queue = registry.queue_intent(other3, 'run-3', 'c' * 64, OTHER_QUEUE)
        registry.queue_bind_task(other3, other_queue, 'task-global')

    def test_ambiguous_enqueue_is_permanently_fenced(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        registry.queue_mark_ambiguous(self.folder, token, 'client timed out after write')
        with self.assertRaises(registry.RegistryError):
            registry.queue_bind_task(self.folder, token, 'possibly-created-task')
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, 'possibly-created-task', 'worker-A')
        self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'AMBIGUOUS')

    def test_crash_between_global_fence_and_run_claim_stays_unretryable(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        registry.queue_bind_task(self.folder, token, 'task-crash-window')
        fence = registry.queue_claim(self.folder, RUN, DIGEST, QUEUE,
                                     'task-crash-window', 'worker-A')
        # Simulates process death after the global commit, before the run-local
        # execution.claim marker and SQLite row are created.
        self.assertFalse((self.folder / 'execution.claim').exists())
        self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'CLAIMED')
        self.assertIsNone(registry.inspect(self.folder))
        with self.assertRaisesRegex(registry.RegistryError, 'stale replay'):
            registry.queue_claim(self.folder, RUN, DIGEST, QUEUE,
                                 'task-crash-window', 'worker-A')
        # A caller without the persisted fence cannot proceed or refresh it.
        with self.assertRaisesRegex(registry.RegistryError, 'mismatch'):
            registry.claim(self.folder, RUN, 'project-a', DIGEST, 'QUEUED',
                           queue_instance=QUEUE, queue_task_id='task-crash-window',
                           queue_worker_id='worker-A', queue_fence='wrong-fence')
        self.assertEqual(registry.inspect_queue(self.folder)['fence'], fence)
        self.assertFalse((self.folder / 'execution.claim').exists())

    def test_wrong_run_digest_task_and_worker_replay_fail_closed(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        registry.queue_bind_task(self.folder, token, 'task-42')
        for run, digest, task in [('other', DIGEST, 'task-42'),
                                  (RUN, 'b' * 64, 'task-42'),
                                  (RUN, DIGEST, 'task-other')]:
            with self.subTest(run=run, digest=digest, task=task):
                with self.assertRaises(registry.RegistryError):
                    registry.queue_claim(self.folder, run, digest, QUEUE, task, 'worker-A')
        registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, 'task-42', 'worker-A')
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, 'task-42', 'worker-B')

    def test_competing_workers_get_one_fence(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        registry.queue_bind_task(self.folder, token, 'task-42')
        start = multiprocessing.Event()
        results = multiprocessing.Queue()
        workers = [multiprocessing.Process(target=claim_worker,
                    args=(str(self.folder), start, results, 'task-42', 'worker-%d' % i))
                   for i in range(12)]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(10)
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))
        outcomes = [results.get(timeout=2) for _ in workers]
        self.assertEqual(sum(outcome[0] == 'won' for outcome in outcomes), 1)
        self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'CLAIMED')

    def test_execution_claim_requires_same_queue_fence(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE)
        registry.queue_bind_task(self.folder, token, 'task-42')
        fence = registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, 'task-42', 'worker-A')
        with self.assertRaisesRegex(registry.RegistryError, 'requires queue instance'):
            registry.claim(self.folder, RUN, 'project-a', DIGEST, 'QUEUED')
        with self.assertRaisesRegex(registry.RegistryError, 'mismatch'):
            registry.claim(self.folder, RUN, 'project-a', DIGEST, 'QUEUED',
                           queue_instance=QUEUE, queue_task_id='task-42',
                           queue_worker_id='worker-B', queue_fence=fence)
        owner = registry.claim(self.folder, RUN, 'project-a', DIGEST, 'QUEUED',
                               queue_instance=QUEUE, queue_task_id='task-42',
                               queue_worker_id='worker-A', queue_fence=fence)
        self.assertTrue(owner)
        self.assertEqual(registry.inspect(self.folder)['queue_task_id'], 'task-42')


if __name__ == '__main__':
    unittest.main()
