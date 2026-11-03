"""CPU-only queue identity, enqueue ambiguity, and worker fencing tests."""
import importlib.util
import multiprocessing
import tempfile
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts' / 'run_registry.py'
spec = importlib.util.spec_from_file_location('queue_registry', path)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)

RUN = 'run-1'
DIGEST = 'a' * 64


def claim_worker(folder, start, results, task='task-1', worker='worker-1'):
    start.wait()
    try:
        results.put(('won', registry.queue_claim(Path(folder), RUN, DIGEST, task, worker)))
    except registry.RegistryError:
        results.put(('lost', None))


class QueueProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def test_durable_intent_task_binding_and_worker_fence(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST)
        self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'INTENT')
        registry.queue_bind_task(self.folder, token, 'task-42')
        before = registry.inspect_queue(self.folder)
        self.assertEqual(before['phase'], 'BOUND')
        fence = registry.queue_claim(self.folder, RUN, DIGEST, 'task-42', 'worker-A')
        self.assertTrue(fence)
        claimed = registry.inspect_queue(self.folder)
        self.assertEqual(claimed['worker_id'], 'worker-A')
        self.assertEqual(claimed['fence'], fence)
        self.assertEqual(claimed['task_id'], 'task-42')

    def test_duplicate_intent_cannot_retry_external_enqueue(self):
        registry.queue_intent(self.folder, RUN, DIGEST)
        with self.assertRaisesRegex(registry.RegistryError, 'never retry'):
            registry.queue_intent(self.folder, RUN, DIGEST)

    def test_ambiguous_enqueue_is_permanently_fenced(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST)
        registry.queue_mark_ambiguous(self.folder, token, 'client timed out after write')
        with self.assertRaises(registry.RegistryError):
            registry.queue_bind_task(self.folder, token, 'possibly-created-task')
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(self.folder, RUN, DIGEST, 'possibly-created-task', 'worker-A')
        self.assertEqual(registry.inspect_queue(self.folder)['phase'], 'AMBIGUOUS')

    def test_wrong_run_digest_task_and_worker_replay_fail_closed(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST)
        registry.queue_bind_task(self.folder, token, 'task-42')
        for run, digest, task in [('other', DIGEST, 'task-42'),
                                  (RUN, 'b' * 64, 'task-42'),
                                  (RUN, DIGEST, 'task-other')]:
            with self.subTest(run=run, digest=digest, task=task):
                with self.assertRaises(registry.RegistryError):
                    registry.queue_claim(self.folder, run, digest, task, 'worker-A')
        registry.queue_claim(self.folder, RUN, DIGEST, 'task-42', 'worker-A')
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(self.folder, RUN, DIGEST, 'task-42', 'worker-B')

    def test_competing_workers_get_one_fence(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST)
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
        token = registry.queue_intent(self.folder, RUN, DIGEST)
        registry.queue_bind_task(self.folder, token, 'task-42')
        fence = registry.queue_claim(self.folder, RUN, DIGEST, 'task-42', 'worker-A')
        with self.assertRaisesRegex(registry.RegistryError, 'requires bound task'):
            registry.claim(self.folder, RUN, 'project', DIGEST, 'QUEUED')
        with self.assertRaisesRegex(registry.RegistryError, 'mismatch'):
            registry.claim(self.folder, RUN, 'project', DIGEST, 'QUEUED',
                           queue_task_id='task-42', queue_worker_id='worker-B', queue_fence=fence)
        owner = registry.claim(self.folder, RUN, 'project', DIGEST, 'QUEUED',
                               queue_task_id='task-42', queue_worker_id='worker-A', queue_fence=fence)
        self.assertTrue(owner)
        self.assertEqual(registry.inspect(self.folder)['queue_task_id'], 'task-42')


if __name__ == '__main__':
    unittest.main()
