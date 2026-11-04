"""Crash-boundary fault injection with real process death.

Each scenario runs a synthetic driver in a child Python process. The driver calls
the production function with a test-only hook (passed as a parameter, never an
environment variable or CLI flag). At the named boundary the hook prints a marker
and calls os._exit(77): no finally blocks, no rollback in Python, no cleanup.
The parent then inspects what survived and checks that it is fenced or ambiguous,
never completed or retried.

Only synthetic argv children started by these tests run, and they are short.
Files live under tempfile directories. No queue daemon, GPU or workload is used.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry = _load('run_registry_faults', 'run_registry.py')
ownership = registry.run_ownership
researchctl = _load('researchctl_faults', 'researchctl.py')

REGISTRY_FILE = str(SCRIPTS / 'run_registry.py')
RESEARCHCTL_FILE = str(SCRIPTS / 'researchctl.py')
DIGEST = 'a' * 64
QUEUE = 'd' * 64
NOW = '2026-11-04T00:00:00Z'

PREAMBLE = textwrap.dedent('''
    import importlib.util, os, sys
    from pathlib import Path

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    registry = _load('run_registry', {registry!r})
    CRASH = {point!r}
    HOME = Path({home!r})
    FOLDER = Path({folder!r})
    EXTERNAL = Path({external!r})
    DIGEST = {digest!r}
    QUEUE = {queue!r}

    def hook(point):
        if point == CRASH:
            sys.stdout.write('CRASHED ' + point + '\\n')
            sys.stdout.flush()
            os._exit(77)

    def submit(task):
        """Stand-in for the external queue accepting a task; records it durably."""
        with EXTERNAL.open('a') as handle:
            handle.write(task + '\\n')
            handle.flush()
            os.fsync(handle.fileno())
''')


class FaultBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.folder = self.root / '.runtime' / 'project-a' / 'runs' / 'run-1'
        self.folder.mkdir(parents=True)
        self.external = self.root / 'external-submissions.log'

    def driver(self, body, point, folder=None):
        code = PREAMBLE.format(registry=REGISTRY_FILE, point=point, home=str(self.home),
                               folder=str(folder or self.folder), external=str(self.external),
                               digest=DIGEST, queue=QUEUE) + textwrap.dedent(body)
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 77, proc.stdout + proc.stderr)
        self.assertIn('CRASHED ' + point, proc.stdout)
        self.assertNotIn('NOT_REACHED', proc.stdout)

    def submissions(self):
        if not self.external.exists():
            return []
        return self.external.read_text().splitlines()

    def queue(self, folder=None):
        return registry.inspect_queue(folder or self.folder, _store_home=self.home)

    def queued_local_claim(self, fence, attempt=None):
        return registry.claim(self.folder, 'run-1', 'project-a', DIGEST, 'QUEUED',
                              queue_instance=QUEUE, queue_task_id='task-1', queue_worker_id='worker-A',
                              queue_fence=fence, queue_attempt_id=attempt, queue_store_home=self.home)


class QueueBoundaryFaults(FaultBase):
    def test_crash_before_intent_is_durable_leaves_nothing_to_resume(self):
        self.driver('''
            registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME, _fault=hook)
            print('NOT_REACHED')
        ''', 'queue_intent:before_commit')
        self.assertIsNone(self.queue())
        self.assertIsNone(registry.queue_recover(self.folder, _store_home=self.home))
        self.assertEqual(self.submissions(), [])
        self.assertIsNone(registry.inspect(self.folder))

    def test_crash_after_intent_before_submission_is_fenced_ambiguous(self):
        self.driver('''
            registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME, _fault=hook)
            print('NOT_REACHED')
        ''', 'queue_intent:after_commit')
        self.assertEqual(self.queue()['phase'], 'INTENT')
        self.assertEqual(registry.queue_recover(self.folder, _store_home=self.home), 'AMBIGUOUS')
        self.assertEqual(self.submissions(), [])
        with self.assertRaises(registry.RegistryError):
            registry.queue_bind_task(self.folder, self.queue()['intent_token'], 'task-1', _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_resolve_ambiguous(self.folder, None, _store_home=self.home)
        self.assertEqual(self.queue()['phase'], 'AMBIGUOUS')

    def test_crash_after_submission_before_task_id_recorded_needs_recorded_evidence(self):
        self.driver('''
            token = registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME)
            submit('task-1')
            registry.queue_bind_task(FOLDER, token, 'task-1', _store_home=HOME, _fault=hook)
            print('NOT_REACHED')
        ''', 'queue_bind_task:before_commit')
        self.assertEqual(self.submissions(), ['task-1'])
        self.assertEqual(self.queue()['phase'], 'INTENT')
        self.assertEqual(registry.queue_recover(self.folder, _store_home=self.home), 'AMBIGUOUS')
        token = self.queue()['intent_token']
        with self.assertRaises(registry.RegistryError):
            registry.queue_resolve_ambiguous(self.folder, None, _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(self.folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A', _store_home=self.home)
        self.assertEqual(self.queue()['phase'], 'AMBIGUOUS')
        self.assertEqual(self.submissions(), ['task-1'])
        with patch.dict(ownership._VERIFIED_ISSUERS, {'synthetic-test-issuer': lambda k, b, raw: True}):
            proof = ownership.issue_proof('synthetic-test-issuer', ownership.QUEUE_LOOKUP_TASK,
                                          {'intent_token': token, 'queue_instance': QUEUE, 'task_id': 'task-1'},
                                          None)
        self.assertEqual(registry.queue_resolve_ambiguous(self.folder, proof, _store_home=self.home), 'BOUND')
        self.assertEqual(self.submissions(), ['task-1'])

    def test_crash_after_global_claim_before_local_claim_is_refused_then_fenced(self):
        self.driver('''
            token = registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME)
            submit('task-1')
            registry.queue_bind_task(FOLDER, token, 'task-1', _store_home=HOME)
            registry.queue_claim(FOLDER, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                 _store_home=HOME, _fault=hook)
            print('NOT_REACHED')
        ''', 'queue_claim:after_commit')
        row = self.queue()
        self.assertEqual((row['phase'], row['worker_id']), ('CLAIMED', 'worker-A'))
        self.assertIsNone(registry.inspect(self.folder))
        with self.assertRaisesRegex(registry.RegistryError, 'another claimant'):
            self.queued_local_claim(row['fence'], row['attempt_id'])
        self.assertFalse((self.folder / 'execution.claim').exists())
        self.assertEqual(registry.queue_recover(self.folder, _store_home=self.home), 'AMBIGUOUS')
        with self.assertRaises(registry.RegistryError):
            self.queued_local_claim(row['fence'], row['attempt_id'])
        self.assertIsNone(registry.inspect(self.folder))
        self.assertEqual(self.submissions(), ['task-1'])

    def test_crash_during_local_claim_before_commit_is_fenced_not_retried(self):
        self.driver('''
            token = registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME)
            registry.queue_bind_task(FOLDER, token, 'task-1', _store_home=HOME)
            fence = registry.queue_claim(FOLDER, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A', _store_home=HOME)
            registry.claim(FOLDER, 'run-1', 'project-a', DIGEST, 'QUEUED', queue_instance=QUEUE,
                           queue_task_id='task-1', queue_worker_id='worker-A', queue_fence=fence,
                           queue_store_home=HOME, _fault=hook)
            print('NOT_REACHED')
        ''', 'claim:before_commit')
        self.assertIsNone(registry.inspect(self.folder))
        self.assertTrue((self.folder / 'execution.claim').exists())
        self.assertEqual(registry.queue_recover(self.folder, _store_home=self.home), 'AMBIGUOUS')
        row = self.queue()
        with self.assertRaises(registry.RegistryError):
            self.queued_local_claim(row['fence'], row['attempt_id'])
        self.assertIsNone(registry.inspect(self.folder))

    def test_crash_after_local_claim_before_launch_stays_claimed_and_fenced(self):
        self.driver('''
            token = registry.queue_intent(FOLDER, 'run-1', DIGEST, QUEUE, _store_home=HOME)
            registry.queue_bind_task(FOLDER, token, 'task-1', _store_home=HOME)
            fence = registry.queue_claim(FOLDER, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A', _store_home=HOME)
            registry.claim(FOLDER, 'run-1', 'project-a', DIGEST, 'QUEUED', queue_instance=QUEUE,
                           queue_task_id='task-1', queue_worker_id='worker-A', queue_fence=fence,
                           queue_store_home=HOME, _fault=hook)
            print('NOT_REACHED')
        ''', 'claim:after_commit')
        self.assertEqual(registry.queue_recover(self.folder, _store_home=self.home), 'CLAIMED')
        row = registry.inspect(self.folder)
        self.assertEqual((row['state'], row['identity'], row['child']), ('CLAIMED', 'CURRENT', None))
        with self.assertRaises(registry.RegistryError):
            self.queued_local_claim(row['fence'], row['attempt_id'])
        self.assertEqual(registry.inspect(self.folder)['state'], 'CLAIMED')

    def test_crash_during_local_claim_for_local_run_leaves_marker_and_no_row(self):
        self.driver('''
            registry.claim(FOLDER, 'run-1', 'project-a', DIGEST, 'SUBMITTED', _fault=hook)
            print('NOT_REACHED')
        ''', 'claim:before_commit')
        self.assertIsNone(registry.inspect(self.folder))
        with self.assertRaises(registry.RegistryError):
            registry.claim(self.folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        self.assertIsNone(registry.inspect(self.folder))


class ExecutionBoundaryFaults(FaultBase):
    """Crash points inside researchctl.execute, using a synthetic leader argv."""

    RUN_ID = 'run-exec'

    def setUp(self):
        super().setUp()
        self.research = self.root / 'research'
        (self.research / 'demo').mkdir(parents=True)
        config = self.root / 'projects.json'
        config.write_text(json.dumps({'version': 1, 'projects': [{'id': 'demo', 'directory': 'demo'}]}))
        self.config = config
        self.env = patch.dict(os.environ, {'RESEARCH_ROOT': str(self.research),
                                           'HERDR_ML_CONFIG': str(config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.run_folder = self.research / '.runtime' / 'demo' / 'runs' / self.RUN_ID
        self.run_folder.mkdir(parents=True)
        self.marker = self.root / 'executions.log'
        child = ('import pathlib,time; p=pathlib.Path(%r); p.open("a").write("ran\\n"); time.sleep(1.0)'
                 % str(self.marker))
        manifest = {'schema_version': 1, 'run_id': self.RUN_ID, 'project': 'demo', 'mode': 'diagnostic',
                    'argv': [sys.executable, '-c', child], 'queue': 'local', 'git': None}
        manifest_path = self.run_folder / 'submission.json'
        researchctl.atomic_json(manifest_path, manifest)
        (self.run_folder / 'submission.sha256').write_text(researchctl.sha_file(manifest_path) + '\n')
        researchctl.atomic_json(self.run_folder / 'status.json', {'state': 'SUBMITTED'})

    def driver_execute(self, point):
        body = textwrap.dedent('''
            import importlib.util
            spec = importlib.util.spec_from_file_location('researchctl_child', %r)
            rc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(rc)
            rc.execute('demo', %r, _fault=hook)
            print('NOT_REACHED')
        ''' % (RESEARCHCTL_FILE, self.RUN_ID))
        code = PREAMBLE.format(registry=REGISTRY_FILE, point=point, home=str(self.home),
                               folder=str(self.run_folder), external=str(self.external),
                               digest=DIGEST, queue=QUEUE) + body
        env = dict(os.environ, RESEARCH_ROOT=str(self.research), HERDR_ML_CONFIG=str(self.config))
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 77, proc.stdout + proc.stderr)
        self.assertIn('CRASHED ' + point, proc.stdout)
        self.assertNotIn('NOT_REACHED', proc.stdout)

    def wait_for_child_gone(self, identity):
        pid = identity['pid']
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            stat = Path('/proc/%d/stat' % pid)
            if not stat.exists() or stat.read_text().split()[2] == 'Z':
                return
            time.sleep(0.05)
        self.fail('child from crashed driver did not finish')

    def executions(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def assert_not_rerun(self, expected_runs):
        code = researchctl.main(['_execute', '--project', 'demo', '--run-id', self.RUN_ID])
        self.assertEqual(code, 2)
        self.assertEqual(len(self.executions()), expected_runs)

    def test_crash_after_local_claim_before_launch_never_spawns_child(self):
        self.driver_execute('execute:claimed')
        row = registry.inspect(self.run_folder)
        self.assertEqual((row['state'], row['identity'], row['child']), ('CLAIMED', 'CURRENT', None))
        self.assert_not_rerun(expected_runs=0)
        self.assertEqual(registry.inspect(self.run_folder)['state'], 'CLAIMED')
        self.assertEqual(self.executions(), [])

    def test_crash_during_execution_leaves_running_and_never_completes(self):
        self.driver_execute('execute:child_running')
        row = registry.inspect(self.run_folder)
        self.assertEqual(row['state'], 'RUNNING')
        self.assertIsNotNone(row['child'])
        self.wait_for_child_gone(row['child'])
        self.assertEqual(self.executions(), ['ran'])
        self.assert_not_rerun(expected_runs=1)
        after = registry.inspect(self.run_folder)
        self.assertEqual((after['state'], after['result']), ('RUNNING', None))
        self.assertFalse((self.run_folder / 'result.json').exists())

    def test_crash_after_leader_exit_before_verification_is_not_completed(self):
        self.driver_execute('execute:leader_exited')
        row = registry.inspect(self.run_folder)
        self.assertEqual(row['state'], 'RUNNING')
        self.assertIsNone(row['result'])
        self.assertFalse((self.run_folder / 'result.json').exists())
        self.assert_not_rerun(expected_runs=1)
        self.assertEqual(registry.inspect(self.run_folder)['state'], 'RUNNING')

    def test_crash_during_resource_release_records_unknown_not_completed(self):
        self.driver_execute('execute:before_release')
        row = registry.inspect(self.run_folder)
        self.assertEqual(row['state'], 'UNKNOWN')
        self.assertEqual(row['result']['leader_state'], 'COMPLETED')
        self.assertFalse((self.run_folder / 'result.json').exists())
        self.assert_not_rerun(expected_runs=1)
        self.assertEqual(researchctl.main(['status', '--project', 'demo']), 0)
        self.assertEqual(registry.inspect(self.run_folder)['state'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
