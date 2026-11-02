"""Adversarial CPU tests for signed provenance envelopes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'provenance.py'
if MODULE.exists():
    spec = importlib.util.spec_from_file_location('provenance', MODULE)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
else:
    p = None


class SignedProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.key = self.root / 'reviewer'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)], check=True)
        self.allowed = self.root / 'allowed_signers'
        self.allowed.write_text('reviewer ' + self.key.with_suffix('.pub').read_text())
        self.artifacts = {}
        for role in ('source', 'environment', 'dataset_manifest', 'dataset_content', 'train_split',
                     'validation_split', 'test_split', 'config', 'protocol', 'baseline', 'review_evidence'):
            f = self.bundle / (role + '.dat')
            f.write_text('fixture ' + role)
            self.artifacts[role] = {'path': f.name, 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()}
        self.manifest = {'schema_version': 1, 'project': 'demo', 'source_git_sha': 'a' * 40,
                         'argv': ['python', 'train.py'], 'seed': 42, 'artifacts': self.artifacts}
        self.path = self.bundle / 'manifest.json'
        self.write_signed()

    def write_signed(self):
        self.path.write_text(json.dumps(self.manifest, sort_keys=True, separators=(',', ':')) + '\n')
        signature = Path(str(self.path) + '.sig')
        if signature.exists(): signature.unlink()
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-n', 'herdr-ml-official-v1', '-f', str(self.key), str(self.path)], check=True)

    def verify(self):
        return p.verify_bundle(self.path, Path(str(self.path) + '.sig'), self.allowed, 'reviewer')

    def test_real_signature_and_all_artifact_hashes(self):
        self.assertEqual(self.verify()['project'], 'demo')

    def test_changed_dataset_split_evidence_or_manifest_rejected(self):
        for role in ('dataset_content', 'test_split', 'review_evidence'):
            with self.subTest(role=role):
                f = self.bundle / self.artifacts[role]['path']
                original = f.read_bytes()
                f.write_bytes(original + b'changed')
                with self.assertRaises(p.ProvenanceError): self.verify()
                f.write_bytes(original)
        self.path.write_text(self.path.read_text().replace('demo', 'evil'))
        with self.assertRaises(p.ProvenanceError): self.verify()

    def test_untrusted_principal_and_unsigned_changes_rejected(self):
        with self.assertRaises(p.ProvenanceError):
            p.verify_bundle(self.path, Path(str(self.path) + '.sig'), self.allowed, 'attacker')
        self.manifest['argv'].append('--changed')
        self.path.write_text(json.dumps(self.manifest))
        with self.assertRaises(p.ProvenanceError): self.verify()

    def test_symlink_escape_and_missing_required_role_rejected(self):
        target = self.bundle / self.artifacts['test_split']['path']
        target.unlink()
        target.symlink_to(self.root / 'outside')
        with self.assertRaises(p.ProvenanceError): self.verify()
        target.unlink()
        del self.manifest['artifacts']['test_split']
        self.write_signed()
        with self.assertRaises(p.ProvenanceError): self.verify()

    def test_signed_path_traversal_and_duplicate_keys_rejected(self):
        self.manifest['artifacts']['test_split']['path'] = '../outside'
        self.write_signed()
        with self.assertRaises(p.ProvenanceError): self.verify()
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        signature = Path(str(self.path) + '.sig')
        signature.unlink()
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-n', 'herdr-ml-official-v1', '-f', str(self.key), str(self.path)], check=True)
        with self.assertRaises(p.ProvenanceError): self.verify()

    def test_unsigned_environment_or_config_changes_rejected(self):
        for role in ('source', 'environment', 'dataset_manifest', 'train_split', 'validation_split', 'config', 'protocol', 'baseline'):
            with self.subTest(role=role):
                artifact = self.bundle / self.artifacts[role]['path']
                before = artifact.read_bytes()
                artifact.write_bytes(before + b' altered')
                with self.assertRaises(p.ProvenanceError): self.verify()
                artifact.write_bytes(before)

    def sign_bytes(self, data, namespace='herdr-ml-official-v1'):
        self.path.write_bytes(data)
        signature = Path(str(self.path) + '.sig')
        if signature.exists(): signature.unlink()
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-n', namespace, '-f', str(self.key), str(self.path)], check=True)

    def test_rejects_non_utf8_nonstandard_and_unknown_fields(self):
        canonical = json.dumps(self.manifest, sort_keys=True).encode()
        self.sign_bytes(canonical.decode().encode('utf-16'))
        with self.assertRaises(p.ProvenanceError): self.verify()
        self.manifest['extra'] = 123
        self.write_signed()
        with self.assertRaises(p.ProvenanceError): self.verify()
        self.manifest.pop('extra')
        self.manifest['seed'] = float('nan')
        self.write_signed()
        with self.assertRaises(p.ProvenanceError): self.verify()
        self.manifest['seed'] = float('inf')
        self.write_signed()
        with self.assertRaises(p.ProvenanceError): self.verify()

    def test_wrong_namespace_signature_and_oversize_rejected(self):
        data = self.path.read_bytes()
        self.sign_bytes(data, namespace='other-purpose')
        with self.assertRaises(p.ProvenanceError): self.verify()
        self.sign_bytes(data + b' ' * (1024 * 1024))
        with self.assertRaises(p.ProvenanceError): self.verify()
        self.sign_bytes(data)
        Path(str(self.path) + '.sig').write_text('invalid')
        with self.assertRaises(p.ProvenanceError): self.verify()

    def test_identical_train_validation_test_content_rejected(self):
        train = (self.bundle / self.artifacts['train_split']['path']).read_bytes()
        validation = self.bundle / self.artifacts['validation_split']['path']
        validation.write_bytes(train)
        self.manifest['artifacts']['validation_split']['sha256'] = hashlib.sha256(train).hexdigest()
        self.write_signed()
        with self.assertRaises(p.ProvenanceError): self.verify()
