"""Mock-only reconciliation tests; never contact a Herdr server."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('herdr_reconcile', Path(__file__).resolve().parents[1] / 'scripts' / 'herdr_reconcile.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeHerdr:
    def __init__(self):
        self.server = 'server-incarnation-1'
        self.ws = []
        self.ts = {}
        self.next_id = 0
        self.fail_after = None
        self.fail_before = False
        self.calls = []

    def identity(self): return self.server
    def creation_cwd_verified(self): return True
    def workspaces(self): return list(self.ws)
    def tabs(self, workspace): return list(self.ts[workspace])
    def _id(self):
        self.next_id += 1
        return str(self.next_id)
    def create_workspace(self, label, cwd):
        self.calls.append('workspace')
        if self.fail_before: raise TimeoutError('before')
        wid = self._id()
        self.ws.append({'id': wid, 'label': label, 'cwd': cwd})
        self.ts[wid] = []
        if self.fail_after == len(self.calls): raise TimeoutError('after')
    def create_tab(self, workspace, label, cwd):
        self.calls.append('tab')
        if self.fail_before: raise TimeoutError('before')
        self.ts[workspace].append({'id': self._id(), 'label': label, 'cwd': cwd})
        if self.fail_after == len(self.calls): raise TimeoutError('after')


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / 'checkout'
        (self.checkout / '.git').mkdir(parents=True)
        self.path = self.root / 'state' / 'project.json'
        self.api = FakeHerdr()
        self.names = ['Control', 'Runs', 'Logs']

    def run_once(self):
        return m.reconcile(self.api, self.path, 'project', 'Project', self.checkout, self.names)

    def test_first_repeat_and_deleted_tab(self):
        first = self.run_once()
        self.assertEqual(first, self.run_once())
        self.assertEqual(self.api.calls, ['workspace', 'tab', 'tab', 'tab'])
        wid = first['workspace_id']
        self.api.ts[wid] = [t for t in self.api.ts[wid] if t['id'] != first['tabs']['Runs']]
        second = self.run_once()
        self.assertNotEqual(first['tabs']['Runs'], second['tabs']['Runs'])
        self.assertEqual(len(self.api.ws), 1)
        self.assertEqual(len(self.api.ts[wid]), 3)

    def test_crash_after_each_remote_mutation_recovers_without_duplicate(self):
        for stage in range(1, 5):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as td:
                self.path = Path(td) / 'state.json'
                self.api = FakeHerdr()
                self.api.fail_after = stage
                with self.assertRaises(TimeoutError): self.run_once()
                self.api.fail_after = None
                result = self.run_once()
                self.assertEqual(len(self.api.ws), 1)
                self.assertEqual(len(self.api.ts[result['workspace_id']]), 3)
                self.assertEqual(len(self.api.calls), 4)

    def test_uncertain_absent_operation_is_never_retried(self):
        self.api.fail_before = True
        with self.assertRaises(TimeoutError): self.run_once()
        self.api.fail_before = False
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertEqual(self.api.calls, ['workspace'])

    def test_new_server_or_reused_id_blocks(self):
        result = self.run_once()
        self.api.server = 'server-incarnation-2'
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.api.server = 'server-incarnation-1'
        self.api.ws[0]['label'] = 'Unrelated workspace with reused ID'
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertEqual(self.api.ws[0]['id'], result['workspace_id'])

    def test_unverifiable_managed_workspace_identifies_operator_action(self):
        other_label = m.marker('workspace', 'a' * 32, 'other')
        self.api.ws.append({'id': 'w-stale', 'label': other_label,
                            'cwd': str(self.root / 'missing')})
        with self.assertRaises(m.ReconcileBlocked) as caught:
            self.run_once()
        message = str(caught.exception)
        self.assertIn('w-stale', message)
        self.assertIn(other_label, message)
        self.assertIn('operator reconciliation required', message)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.api.calls, [])

    def test_reused_tab_id_blocks(self):
        result = self.run_once()
        tabs = self.api.ts[result['workspace_id']]
        next(t for t in tabs if t['id'] == result['tabs']['Runs'])['label'] = 'user tab'
        before = len(self.api.calls)
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertEqual(len(self.api.calls), before)

    def test_missing_state_does_not_create_duplicate(self):
        self.run_once()
        self.path.unlink()
        before = list(self.api.calls)
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.api.calls, before)
        self.assertEqual(len(self.api.ws), 1)

    def test_unmarked_legacy_workspace_at_checkout_blocks(self):
        self.api.ws.append({'id': 'legacy', 'label': 'Project', 'cwd': str(self.checkout)})
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.api.calls, [])

    def test_creation_bound_cwd_capability_required(self):
        self.api.creation_cwd_verified = lambda: False
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.api.calls, [])

    def test_lazy_inventory_server_change_blocks_before_adoption(self):
        self.api.fail_after = 1
        with self.assertRaises(TimeoutError): self.run_once()
        self.api.fail_after = None
        def changed_listing():
            yield from self.api.ws
            self.api.server = 'different-server'
        self.api.workspaces = changed_listing
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertIsNotNone(json.loads(self.path.read_text())['pending'])
        self.assertEqual(self.api.calls, ['workspace'])

    def test_initial_state_without_workspace_detects_other_managed_layout(self):
        self.run_once()
        record = json.loads(self.path.read_text())
        record['workspace_id'] = None
        record['pending'] = None
        record['token'] = 'a' * 32
        self.path.write_text(json.dumps(record))
        before = list(self.api.calls)
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertEqual(self.api.calls, before)

    def test_duplicate_or_wrong_checkout_blocks(self):
        self.run_once()
        self.api.ws.append(dict(self.api.ws[0]))
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.api.ws.pop()
        self.api.ws[0]['cwd'] = str(self.root)
        with self.assertRaises(m.ReconcileBlocked): self.run_once()

    def test_incomplete_state_fails_without_mutation(self):
        self.path.parent.mkdir()
        self.path.write_text('{')
        with self.assertRaises(m.ReconcileBlocked): self.run_once()
        self.assertEqual(self.api.calls, [])

class ProcessCrashTests(unittest.TestCase):
    def test_kill_after_remote_mutation_then_resume(self):
        import multiprocessing
        import os

        class SharedFake:
            def __init__(self, shared, crash_at=None):
                self.shared = shared
                self.crash_at = crash_at
            def identity(self): return 'server-1'
            def creation_cwd_verified(self): return True
            def workspaces(self): return json.loads(self.shared['workspaces'])
            def tabs(self, wid): return json.loads(self.shared['tabs'])[wid]
            def _create(self, kind, label, cwd, wid=None):
                count = self.shared['count'] + 1
                self.shared['count'] = count
                item = {'id': str(count), 'label': label, 'cwd': cwd}
                workspaces = self.workspaces()
                tabs = json.loads(self.shared['tabs'])
                if kind == 'workspace':
                    workspaces.append(item)
                    tabs[item['id']] = []
                else:
                    tabs[wid].append(item)
                self.shared['workspaces'] = json.dumps(workspaces)
                self.shared['tabs'] = json.dumps(tabs)
                if count == self.crash_at:
                    os._exit(71)
            def create_workspace(self, label, cwd): self._create('workspace', label, cwd)
            def create_tab(self, wid, label, cwd): self._create('tab', label, cwd, wid)

        if 'fork' not in multiprocessing.get_all_start_methods():
            self.skipTest('abrupt-exit harness requires fork multiprocessing context')
        ctx = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as td, ctx.Manager() as manager:
            root = Path(td)
            checkout = root / 'repo'
            (checkout / '.git').mkdir(parents=True)
            for stage in range(1, 5):
                with self.subTest(stage=stage):
                    shared = manager.dict(workspaces='[]', tabs='{}', count=0)
                    state = root / ('state-%s.json' % stage)
                    args = (SharedFake(shared, stage), state, 'demo', 'Demo', checkout, ['Control', 'Runs', 'Logs'])
                    process = ctx.Process(target=m.reconcile, args=args)
                    process.start()
                    process.join(10)
                    self.assertEqual(process.exitcode, 71)
                    result = m.reconcile(SharedFake(shared), state, 'demo', 'Demo', checkout, ['Control', 'Runs', 'Logs'])
                    self.assertEqual(shared['count'], 4)
                    self.assertEqual(len(json.loads(shared['workspaces'])), 1)
                    self.assertEqual(len(json.loads(shared['tabs'])[result['workspace_id']]), 3)
