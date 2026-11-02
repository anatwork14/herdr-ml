"""Adversarial CPU tests for concurrent run ownership."""
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'researchctl.py'
spec = importlib.util.spec_from_file_location('researchctl_p0', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def worker(project, run_id, start, results):
    start.wait()
    try:
        results.put(('ok', m.execute(project, run_id)))
    except (m.ResearchError, OSError) as exc:
        results.put(('error', str(exc)))


class OwnershipTests(unittest.TestCase):
    def test_twenty_workers_have_one_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'demo'
            repo.mkdir()
            config = root / 'config.json'
            config.write_text(json.dumps({'version': 1, 'projects': [{'id': 'demo', 'directory': 'demo'}]}))
            os.environ['RESEARCH_ROOT'] = str(root)
            os.environ['HERDR_ML_CONFIG'] = str(config)
            folder = root / '.runtime/demo/runs/test'
            folder.mkdir(parents=True)
            marker = root / 'marker'
            manifest = {'project': 'demo', 'mode': 'diagnostic', 'queue': 'local', 'argv': [sys.executable, '-c',
                'import pathlib,time; pathlib.Path(%r).open("a").write("run\\n"); time.sleep(.2)' % str(marker)]}
            m.atomic_json(folder / 'submission.json', manifest)
            (folder / 'submission.sha256').write_text(m.sha_file(folder / 'submission.json'))
            m.atomic_json(folder / 'status.json', {'state': 'QUEUED'})
            start = multiprocessing.Event()
            results = multiprocessing.Queue()
            processes = [multiprocessing.Process(target=worker, args=('demo', 'test', start, results)) for _ in range(20)]
            for process in processes: process.start()
            start.set()
            for process in processes: process.join(10)
            self.assertTrue(all(not p.is_alive() for p in processes))
            outcomes = [results.get(timeout=2) for _ in processes]
            self.assertEqual(sum(x[0] == 'ok' for x in outcomes), 1, outcomes)
            self.assertEqual(marker.read_text().splitlines(), ['run'])

class CancellationTests(unittest.TestCase):
    def test_sigterm_stops_stubborn_descendant(self):
        import signal
        self._cancel_stubborn_descendant(signal.SIGTERM)

    def test_sighup_stops_stubborn_descendant(self):
        import signal
        self._cancel_stubborn_descendant(signal.SIGHUP)

    def _cancel_stubborn_descendant(self, sig):
        import signal
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'demo'
            (repo / '.git').mkdir(parents=True)
            config = root / 'config.json'
            config.write_text(json.dumps({'version': 1, 'projects': [{'id': 'demo', 'directory': 'demo'}]}))
            marker = root / 'pid'
            env = dict(os.environ, RESEARCH_ROOT=str(root), HERDR_ML_CONFIG=str(config))
            grandchild = 'import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)' % str(marker)
            program = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",%r]); time.sleep(60)' % grandchild
            proc = subprocess.Popen([sys.executable, str(MODULE), 'init-run', '--project', 'demo', '--mode', 'diagnostic', '--dataset-version', 'test', '--split-hash', 'test', '--seed', '1', '--', sys.executable, '-c', program], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                proc.send_signal(sig)
                proc.communicate(timeout=10)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    stat = Path('/proc/%d/stat' % pid)
                    if not stat.exists() or stat.read_text().split()[2] == 'Z':
                        break
                    time.sleep(.02)
                else:
                    self.fail('stubborn descendant remained running')
                folder, = (root / '.runtime/demo/runs').iterdir()
                self.assertEqual(json.loads((folder / 'status.json').read_text())['state'], 'INTERRUPTED')
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_normal_leader_exit_stops_same_group_descendant(self):
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'demo' / '.git').mkdir(parents=True)
            config = root / 'config.json'
            config.write_text(json.dumps({'version': 1, 'projects': [{'id':'demo','directory':'demo'}]}))
            marker = root / 'pid'
            grandchild = 'import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)' % str(marker)
            program = 'import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,"-c",%r]); p=pathlib.Path(%r); exec("while not p.exists(): time.sleep(.01)")' % (grandchild, str(marker))
            env = dict(os.environ, RESEARCH_ROOT=str(root), HERDR_ML_CONFIG=str(config))
            proc = subprocess.run([sys.executable,str(MODULE),'init-run','--project','demo','--mode','diagnostic','--dataset-version','test','--split-hash','test','--seed','1','--',sys.executable,'-c',program], env=env, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            pid = int(marker.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                stat = Path('/proc/%d/stat' % pid)
                if not stat.exists() or stat.read_text().split()[2] == 'Z':
                    break
                time.sleep(.02)
            else:
                self.fail('same-group descendant outlived leader')

    def test_post_spawn_status_failure_kills_group_and_stays_uncertain(self):
        from unittest.mock import patch
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'demo').mkdir()
            config = root / 'config.json'
            config.write_text(json.dumps({'version': 1, 'projects': [{'id':'demo','directory':'demo'}]}))
            folder = root / '.runtime/demo/runs/test'
            folder.mkdir(parents=True)
            marker = root / 'pid'
            script = 'import pathlib,os,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)' % str(marker)
            m.atomic_json(folder / 'submission.json', {'project':'demo','mode':'diagnostic','queue':'local','argv':[sys.executable,'-c',script]})
            (folder / 'submission.sha256').write_text(m.sha_file(folder / 'submission.json'))
            m.atomic_json(folder / 'status.json', {'state':'SUBMITTED'})
            original = m.atomic_json
            def fail_running(path, payload):
                if payload.get('state') == 'RUNNING':
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    if not marker.exists():
                        raise AssertionError('child did not start')
                    raise OSError('simulated disk error')
                return original(path, payload)
            with patch.dict(os.environ, {'RESEARCH_ROOT':str(root),'HERDR_ML_CONFIG':str(config)}):
                with patch.object(m, 'atomic_json', side_effect=fail_running):
                    with self.assertRaises(OSError): m.execute('demo','test')
            pid = int(marker.read_text())
            stat = Path('/proc/%d/stat' % pid)
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')
            self.assertEqual(m.run_registry.inspect(folder)['state'], 'RUNNING')
            self.assertIsNone(m.run_registry.inspect(folder)['result'])
