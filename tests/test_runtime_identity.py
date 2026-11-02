import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'runtime_identity.py'

class RuntimeIdentityTests(unittest.TestCase):
    def run_identity(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, env=env)

    def test_matching_and_changed_interpreter_identity(self):
        snapshot = self.run_identity()
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        record = json.loads(snapshot.stdout)
        self.assertEqual(record['schema_version'], 2)
        self.assertEqual(len(record['executable_sha256']), 64)
        self.assertIsInstance(record['sys_path'], list)
        self.assertIsInstance(record['flags'], dict)
        self.assertIn('stdlib', record)
        self.assertIn('libpython', record)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'expected.json'
            path.write_text(snapshot.stdout)
            self.assertEqual(self.run_identity('--expect', str(path)).returncode, 0)
            for field in ('packages_sha256', 'executable_sha256', 'prefix'):
                with self.subTest(field=field):
                    modified = dict(record, **{field: 'invalid'})
                    path.write_text(json.dumps(modified))
                    result = self.run_identity('--expect', str(path))
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('BLOCKED: interpreter fingerprint mismatch', result.stderr)
                    self.assertEqual(result.stdout, '')
            path.write_text(snapshot.stdout)
            env = dict(os.environ, PYTHONPATH=str(Path(temp) / 'extra'))
            result = self.run_identity('--expect', str(path), env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn('BLOCKED: interpreter fingerprint mismatch', result.stderr)
            self.assertEqual(result.stdout, '')
            optimized = subprocess.run([sys.executable, '-O', str(SCRIPT), '--expect', str(path)],
                                       text=True, capture_output=True)
            self.assertEqual(optimized.returncode, 2)
            self.assertIn('BLOCKED: interpreter fingerprint mismatch', optimized.stderr)
            self.assertEqual(optimized.stdout, '')
            for data in ('{', '[]'):
                path.write_text(data)
                result = self.run_identity('--expect', str(path))
                self.assertEqual(result.returncode, 2)
                self.assertIn('BLOCKED:', result.stderr)
                self.assertEqual(result.stdout, '')
            result = self.run_identity('--expect', str(Path(temp) / 'missing'))
            self.assertEqual(result.returncode, 2)
            self.assertIn('BLOCKED:', result.stderr)
            self.assertEqual(result.stdout, '')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc check')
    def test_spoofed_argv0_is_blocked(self):
        result = subprocess.run(['bash', '-c', 'exec -a /bin/false "$1" "$2"', '_',
                                 sys.executable, str(SCRIPT)], text=True, capture_output=True,
                                env=dict(os.environ, PYTHONHOME=sys.base_prefix))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('BLOCKED:', result.stderr)
        self.assertEqual(result.stdout, '')
