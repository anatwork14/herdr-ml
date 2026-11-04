"""Regression tests for review findings N1 and N2.

N1: every queue and inspection path turns a store corrupted after page 1 into
RegistryError REGISTRY_UNREADABLE and leaves the store bytes unchanged.
N2: a run-local store with some tables but no run table is refused before any
write; schema DDL runs only on a file with no user tables.
CPU-only; every file lives in a tempfile directory.
"""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('run_registry_repairs', SCRIPTS / 'run_registry.py')
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)

DIGEST = 'a' * 64
QUEUE = 'd' * 64
RUN = 'run-1'
PROJECT = 'project-a'
TASK = 'task-1'
WORKER = 'worker-1'


def corrupt_later_pages(path):
    """Overwrite every page after page 1 with 0xff, as in the run-local corrupt-later-pages test."""
    data = bytearray(path.read_bytes())
    page = int.from_bytes(data[16:18], 'big')
    page = 65536 if page == 1 else page
    if len(data) <= 2 * page:
        raise AssertionError('store needs pages beyond page 1 for this test')
    data[page:] = b'\xff' * (len(data) - page)
    path.write_bytes(bytes(data))
    return path.read_bytes()


class RepairBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.folder = self.root / '.runtime' / PROJECT / 'runs' / RUN
        self.folder.mkdir(parents=True)
        self.queue_path = self.home / '.local' / 'state' / 'herdr-ml' / 'queue-registry.sqlite3'


class QueueCorruptLaterPagesTests(RepairBase):
    """N1: queue mutators and readers surface REGISTRY_UNREADABLE, never raw sqlite3 errors."""

    def corrupt_claimed_queue(self):
        token = registry.queue_intent(self.folder, RUN, DIGEST, QUEUE, _store_home=self.home)
        registry.queue_bind_task(self.folder, token, TASK, _store_home=self.home)
        # Pad the store with more rows so it spans pages beyond page 1.
        for index in range(2, 40):
            other = self.root / '.runtime' / PROJECT / 'runs' / ('run-%d' % index)
            other.mkdir(parents=True)
            registry.queue_intent(other, 'run-%d' % index, DIGEST, QUEUE, _store_home=self.home)
        fence = registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, TASK, WORKER, _store_home=self.home)
        before = corrupt_later_pages(self.queue_path)
        return token, fence, before

    def assert_unreadable_and_unchanged(self, call, before):
        with self.assertRaisesRegex(registry.RegistryError, 'REGISTRY_UNREADABLE'):
            call()
        self.assertEqual(self.queue_path.read_bytes(), before)

    def test_queue_intent(self):
        _, _, before = self.corrupt_claimed_queue()
        other = self.root / '.runtime' / PROJECT / 'runs' / 'run-new'
        other.mkdir(parents=True)
        self.assert_unreadable_and_unchanged(
            lambda: registry.queue_intent(other, 'run-new', DIGEST, QUEUE, _store_home=self.home), before)

    def test_queue_bind_task(self):
        token, _, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.queue_bind_task(self.folder, token, 'task-2', _store_home=self.home), before)

    def test_queue_mark_ambiguous(self):
        token, _, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.queue_mark_ambiguous(self.folder, token, 'x', _store_home=self.home), before)

    def test_queue_claim(self):
        _, _, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.queue_claim(self.folder, RUN, DIGEST, QUEUE, TASK, WORKER,
                                         _store_home=self.home), before)

    def test_queue_recover(self):
        _, _, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.queue_recover(self.folder, _store_home=self.home), before)

    def test_queue_resolve_ambiguous(self):
        _, _, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.queue_resolve_ambiguous(self.folder, None, _store_home=self.home), before)

    def test_inspect_queue(self):
        _, _, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.inspect_queue(self.folder, _store_home=self.home), before)

    def test_queued_claim_uses_registry_unreadable_code(self):
        _, fence, before = self.corrupt_claimed_queue()
        self.assert_unreadable_and_unchanged(
            lambda: registry.claim(self.folder, RUN, PROJECT, DIGEST, 'QUEUED', queue_instance=QUEUE,
                                   queue_task_id=TASK, queue_worker_id=WORKER, queue_fence=fence,
                                   _store_home=self.home), before)


class InspectCorruptLaterPagesTests(RepairBase):
    def test_inspect(self):
        token = registry.claim(self.folder, RUN, PROJECT, DIGEST, 'SUBMITTED')
        registry.advance(self.folder, token, 'CLAIMED', 'DISPATCHING')
        path = self.folder / 'registry.sqlite3'
        before = corrupt_later_pages(path)
        with self.assertRaisesRegex(registry.RegistryError, 'REGISTRY_UNREADABLE'):
            registry.inspect(self.folder)
        self.assertEqual(path.read_bytes(), before)


class IncompleteSchemaTests(RepairBase):
    """N2: an events-only store is refused before any schema write."""

    def test_events_only_store_is_refused_and_bytes_unchanged(self):
        path = self.folder / 'registry.sqlite3'
        db = sqlite3.connect(str(path))
        try:
            db.execute('''CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        owner TEXT, state TEXT NOT NULL, detail TEXT)''')
            db.commit()
        finally:
            db.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(registry.RegistryError, 'REGISTRY_UNREADABLE'):
            registry.claim(self.folder, RUN, PROJECT, DIGEST, 'SUBMITTED')
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
