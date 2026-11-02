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
    def test_two_workers_have_one_owner(self):
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
            manifest = {'project': 'demo', 'mode': 'diagnostic', 'argv': [sys.executable, '-c',
                'import pathlib,time; pathlib.Path(%r).open("a").write("run\\n"); time.sleep(.2)' % str(marker)]}
            m.atomic_json(folder / 'submission.json', manifest)
            (folder / 'submission.sha256').write_text(m.sha_file(folder / 'submission.json'))
            m.atomic_json(folder / 'status.json', {'state': 'QUEUED'})
            start = multiprocessing.Event()
            results = multiprocessing.Queue()
            processes = [multiprocessing.Process(target=worker, args=('demo', 'test', start, results)) for _ in range(2)]
            for process in processes: process.start()
            start.set()
            for process in processes: process.join(10)
            self.assertTrue(all(not p.is_alive() for p in processes))
            outcomes = [results.get(timeout=2), results.get(timeout=2)]
            self.assertEqual(sum(x[0] == 'ok' for x in outcomes), 1, outcomes)
            self.assertEqual(marker.read_text().splitlines(), ['run'])
