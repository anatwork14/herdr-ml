import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'runtime_identity.py'

class RuntimeIdentityTests(unittest.TestCase):
    def test_matching_and_changed_interpreter_identity(self):
        snapshot = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, check=True)
        record = json.loads(snapshot.stdout)
        self.assertEqual(len(record['executable_sha256']), 64)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'expected.json'
            path.write_text(snapshot.stdout)
            self.assertEqual(subprocess.run([sys.executable, str(SCRIPT), '--expect', str(path)], capture_output=True).returncode, 0)
            record['python_version'] = 'invalid'
            path.write_text(json.dumps(record))
            self.assertNotEqual(subprocess.run([sys.executable, str(SCRIPT), '--expect', str(path)], capture_output=True).returncode, 0)
