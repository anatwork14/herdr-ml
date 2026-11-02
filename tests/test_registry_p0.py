"""CPU-only transactional registry ownership and recovery tests."""
import importlib.util
import multiprocessing
import tempfile
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts' / 'run_registry.py'
spec = importlib.util.spec_from_file_location('run_registry', path)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def attempt(path, ready, queue):
    ready.wait()
    try:
        token = registry.claim(Path(path), 'run', 'project', 'a' * 64, 'SUBMITTED')
        queue.put(('won', token))
    except registry.RegistryError:
        queue.put(('lost', None))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def claim(self):
        return registry.claim(self.folder, 'run', 'project', 'a' * 64, 'SUBMITTED')

    def test_twenty_workers_one_claim(self):
        start = multiprocessing.Event()
        queue = multiprocessing.Queue()
        workers = [multiprocessing.Process(target=attempt, args=(str(self.folder), start, queue)) for _ in range(20)]
        for worker in workers: worker.start()
        start.set()
        for worker in workers: worker.join(20)
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))
        results = [queue.get(timeout=3) for _ in workers]
        self.assertEqual(sum(item[0] == 'won' for item in results), 1)

    def test_stale_token_cannot_change_or_complete(self):
        token = self.claim()
        with self.assertRaises(registry.RegistryError):
            registry.advance(self.folder, 'incorrect', 'CLAIMED', 'DISPATCHING')
        registry.advance(self.folder, token, 'CLAIMED', 'DISPATCHING')
        with self.assertRaisesRegex(registry.RegistryError, 'Stale owner'):
            registry.finish(self.folder, 'incorrect', {'state': 'UNKNOWN', 'exit_code': 0})
        self.assertEqual(registry.inspect(self.folder)['state'], 'DISPATCHING')

    def test_abandoned_claim_is_never_reclaimed_on_retry(self):
        token = self.claim()
        self.assertTrue(token)
        for _ in range(2):
            with self.assertRaises(registry.RegistryError): self.claim()
            self.assertEqual(registry.inspect(self.folder)['state'], 'CLAIMED')

    def test_result_and_completion_are_atomic_and_terminal(self):
        token = self.claim()
        registry.advance(self.folder, token, 'CLAIMED', 'DISPATCHING')
        registry.advance(self.folder, token, 'DISPATCHING', 'RUNNING')
        result = {'state': 'UNKNOWN', 'leader_state': 'COMPLETED', 'exit_code': 0}
        registry.finish(self.folder, token, result)
        item = registry.inspect(self.folder)
        self.assertEqual(item['state'], 'UNKNOWN')
        self.assertEqual(item['result'], result)
        with self.assertRaises(registry.RegistryError): registry.finish(self.folder, token, result)

    def test_existing_legacy_claim_fails_closed(self):
        (self.folder / 'execution.claim').mkdir()
        with self.assertRaises(registry.RegistryError): self.claim()


def crash_at_phase(folder, phase, ready):
    token = registry.claim(Path(folder), 'run', 'project', 'a' * 64, 'SUBMITTED')
    if phase in ('DISPATCHING', 'RUNNING', 'UNCOMMITTED_RESULT'):
        registry.advance(Path(folder), token, 'CLAIMED', 'DISPATCHING')
    if phase in ('RUNNING', 'UNCOMMITTED_RESULT'):
        registry.advance(Path(folder), token, 'DISPATCHING', 'RUNNING')
    if phase == 'UNCOMMITTED_RESULT':
        db = registry._connect(Path(folder))
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE run SET state='COMPLETED', result_json='{}' WHERE id=1")
    ready.set()
    import time
    time.sleep(60)


class CrashTests(unittest.TestCase):
    def test_crashes_keep_claim_and_never_report_false_completion(self):
        import signal
        for phase in ('CLAIMED', 'DISPATCHING', 'RUNNING', 'UNCOMMITTED_RESULT'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                ready = multiprocessing.Event()
                proc = multiprocessing.Process(target=crash_at_phase, args=(tmp, phase, ready))
                proc.start()
                try:
                    self.assertTrue(ready.wait(10))
                    proc.kill()
                    proc.join(10)
                    self.assertFalse(proc.is_alive())
                    expected = 'RUNNING' if phase == 'UNCOMMITTED_RESULT' else phase
                    self.assertEqual(registry.inspect(folder)['state'], expected)
                    self.assertIsNone(registry.inspect(folder)['result'])
                    for _ in range(2):
                        with self.assertRaises(registry.RegistryError):
                            registry.claim(folder, 'run', 'project', 'a' * 64, 'SUBMITTED')
                finally:
                    if proc.is_alive():
                        proc.kill()
                        proc.join()


def legacy_claim(folder, start, results):
    start.wait()
    try:
        (Path(folder) / 'execution.claim').mkdir(mode=0o700)
        results.put('legacy')
    except FileExistsError:
        results.put('lost')


def new_claim(folder, start, results):
    start.wait()
    try:
        registry.claim(Path(folder), 'run', 'project', 'a' * 64, 'SUBMITTED')
        results.put('new')
    except registry.RegistryError:
        results.put('lost')


class MixedVersionTests(unittest.TestCase):
    def test_legacy_and_new_workers_cannot_both_claim(self):
        for _ in range(10):
            with tempfile.TemporaryDirectory() as tmp:
                start = multiprocessing.Event()
                results = multiprocessing.Queue()
                processes = [multiprocessing.Process(target=fn, args=(tmp, start, results))
                             for fn in (legacy_claim, new_claim)]
                for process in processes: process.start()
                start.set()
                for process in processes: process.join(10)
                self.assertTrue(all(process.exitcode == 0 for process in processes))
                self.assertEqual(sum(results.get(timeout=2) != 'lost' for _ in processes), 1)
