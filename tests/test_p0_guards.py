"""Unsafe operational modes must not silently proceed while P0 is incomplete."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('researchctl_guard', Path(__file__).resolve().parents[1] / 'scripts/researchctl.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class GuardTests(unittest.TestCase):
    def test_official_and_queued_are_blocked(self):
        for mode, queue in [('official', None), ('diagnostic', 'gpu0')]:
            with self.subTest(mode=mode, queue=queue):
                with self.assertRaises(m.ResearchError):
                    m.require_supported_execution(mode, queue)

    def test_local_diagnostic_allowed(self):
        m.require_supported_execution('diagnostic', None)

    def test_explicit_local_queue_is_blocked_but_ledger_local_is_allowed(self):
        with self.assertRaises(m.ResearchError):
            m.require_supported_execution('diagnostic', 'local')
        m.require_supported_execution('diagnostic', 'local', submission=True)
        with self.assertRaises(m.ResearchError):
            m.require_supported_execution('diagnostic', None, submission=True)

    def test_legacy_queued_local_record_cannot_execute(self):
        import json
        import os
        import sys
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'demo').mkdir()
            config = root / 'config.json'
            config.write_text(json.dumps({'version':1,'projects':[{'id':'demo','directory':'demo'}]}))
            folder = root / '.runtime/demo/runs/legacy'
            folder.mkdir(parents=True)
            marker = root / 'executed'
            m.atomic_json(folder / 'submission.json', {'project':'demo','mode':'diagnostic','queue':'local',
                'argv':[sys.executable, '-c', 'import pathlib; pathlib.Path(%r).touch()' % str(marker)]})
            (folder / 'submission.sha256').write_text(m.sha_file(folder / 'submission.json'))
            m.atomic_json(folder / 'status.json', {'state':'QUEUED'})
            with patch.dict(os.environ, {'RESEARCH_ROOT':str(root),'HERDR_ML_CONFIG':str(config)}):
                self.assertEqual(m.main(['_execute','--project','demo','--run-id','legacy']), 2)
            self.assertFalse(marker.exists())
            self.assertFalse((folder / 'execution.claim').exists())
