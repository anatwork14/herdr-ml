"""CPU-only integrity checks for diagnostic artifact tree manifests."""
import importlib.util
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('artifact_tree', pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'artifact_tree.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ArtifactTreeTests(unittest.TestCase):
    def test_changes_additions_and_removals_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'data').mkdir()
            (root / 'data' / 'x').write_bytes(b'first')
            record = m.snapshot(root)
            self.assertTrue(m.verify(root, record))
            (root / 'data' / 'x').write_bytes(b'other')
            self.assertFalse(m.verify(root, record))
            (root / 'data' / 'x').write_bytes(b'first')
            (root / 'other').write_bytes(b'new')
            self.assertFalse(m.verify(root, record))
            (root / 'other').unlink()
            (root / 'data' / 'x').unlink()
            self.assertFalse(m.verify(root, record))

    def test_symlink_and_fifo_fail_closed(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'link').symlink_to('/etc/passwd')
            with self.assertRaises(m.ArtifactError): m.snapshot(root)
            (root / 'link').unlink()
            os.mkfifo(root / 'pipe')
            with self.assertRaises(m.ArtifactError): m.snapshot(root)

    def test_manifest_is_deterministic_and_detects_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'z').write_bytes(b'z')
            (root / 'a').mkdir()
            first = m.snapshot(root)
            self.assertEqual(first, m.snapshot(root))
            (root / 'a').rmdir()
            self.assertFalse(m.verify(root, first))

    def test_command_verifies_actual_files(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'data'
            root.mkdir()
            (root / 'item').write_text('original')
            script = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'artifact_tree.py'
            first = subprocess.run([sys.executable, str(script), str(root)], capture_output=True, text=True, check=True)
            manifest = pathlib.Path(tmp) / 'manifest.json'
            manifest.write_text(first.stdout)
            self.assertEqual(subprocess.run([sys.executable, str(script), str(root), '--verify', str(manifest)], capture_output=True).returncode, 0)
            (root / 'item').write_text('changed')
            self.assertEqual(subprocess.run([sys.executable, str(script), str(root), '--verify', str(manifest)], capture_output=True).returncode, 1)

    def test_boolean_integer_fields_are_rejected(self):
        import copy
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'data'
            root.mkdir()
            (root / 'item').write_bytes(b'x')
            original = m.snapshot(root)
            script = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'artifact_tree.py'
            for key in ('schema_version', 'size'):
                altered = copy.deepcopy(original)
                if key == 'schema_version': altered[key] = True
                else: altered['entries'][0][key] = True
                with self.assertRaises(m.ArtifactError): m.verify(root, altered)
                path = pathlib.Path(tmp) / 'manifest.json'
                path.write_text(json.dumps(altered))
                result = subprocess.run([sys.executable, str(script), str(root), '--verify', str(path)], capture_output=True)
                self.assertEqual(result.returncode, 2)

    def test_entry_limit_applies_before_sorting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ('a', 'b', 'c'):
                (root / name).write_bytes(b'')
            with self.assertRaisesRegex(m.ArtifactError, 'entry limit'):
                m.snapshot(root, max_entries=2)
