"""Adversarial CPU tests for signed provenance envelopes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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
        outside = self.root / 'outside'
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(p.ProvenanceError, 'cannot safely read artifact'): self.verify()
        target.unlink()
        del self.manifest['artifacts']['test_split']
        self.write_signed()
        with self.assertRaisesRegex(p.ProvenanceError, 'missing or unsupported artifact roles'): self.verify()

    def test_intermediate_symlink_rejected_even_with_correct_target(self):
        target = self.bundle / self.artifacts['test_split']['path']
        directory = self.root / 'outside_dir'
        directory.mkdir()
        (directory / 'split').write_bytes(target.read_bytes())
        (self.bundle / 'alias').symlink_to(directory, target_is_directory=True)
        self.manifest['artifacts']['test_split']['path'] = 'alias/split'
        self.write_signed()
        with self.assertRaisesRegex(p.ProvenanceError, 'cannot safely read artifact'): self.verify()

    def test_signed_path_traversal_and_duplicate_keys_rejected(self):
        outside = self.root / 'outside'
        outside.write_bytes((self.bundle / self.artifacts['test_split']['path']).read_bytes())
        self.manifest['artifacts']['test_split']['path'] = '../outside'
        self.write_signed()
        with self.assertRaisesRegex(p.ProvenanceError, 'artifact path must be relative and normalized'): self.verify()
        self.manifest['artifacts']['test_split']['path'] = 'test_split.dat'
        canonical = json.dumps(self.manifest, sort_keys=True, separators=(',', ':'))
        self.sign_bytes(('{"project":"demo",' + canonical[1:]).encode())
        with self.assertRaisesRegex(p.ProvenanceError, 'duplicate JSON key'): self.verify()

    def test_manifest_symlink_rejected_with_otherwise_valid_signature(self):
        self.assertEqual(self.verify()['project'], 'demo')
        actual = self.bundle / 'real-manifest.json'
        self.path.rename(actual)
        self.path.symlink_to(actual)
        with self.assertRaisesRegex(p.ProvenanceError, 'unable to verify signed manifest'): self.verify()

    def test_manifest_fifo_rejected_without_blocking(self):
        self.path.unlink()
        os.mkfifo(self.path)
        code = ("import importlib.util,sys; "
                "spec=importlib.util.spec_from_file_location('p',sys.argv[1]); "
                "p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p); "
                "p.verify_bundle(sys.argv[2],sys.argv[3],sys.argv[4],'reviewer')")
        try:
            result = subprocess.run([sys.executable, '-c', code, str(MODULE), str(self.path),
                str(self.path) + '.sig', str(self.allowed)], capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail('FIFO manifest blocked verification')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('manifest must be a regular file', result.stderr)

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

    def test_valid_bundle_and_clean_checkout_cannot_bypass_official_hold(self):
        research_root = self.root / 'research'
        checkout = research_root / 'demo'
        gates_dir = checkout / '.research'
        gates_dir.mkdir(parents=True)
        (gates_dir / 'evidence.md').write_text('Independent review fixture\n')
        gates = {name: {'status': 'PASS', 'reviewer': 'reviewer',
                        'reviewed_at': '2026-11-02', 'evidence': ['.research/evidence.md']}
                 for name in ('DATA_READY', 'BASELINE_READY', 'GPU_READY')}
        (gates_dir / 'gates.json').write_text(json.dumps({'schema_version': 1, 'gates': gates}))
        subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
        subprocess.run(['git', '-C', str(checkout), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-q', '-m', 'fixture'], check=True)
        sha = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain'], text=True), '')
        marker = self.root / 'payload-executed'
        payload = [sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)]
        self.manifest['source_git_sha'] = sha
        self.manifest['argv'] = payload
        self.write_signed()
        # The current CLI does not consume this bundle; verifying it here must
        # never be mistaken for execution-time provenance binding.
        self.assertEqual(self.verify()['source_git_sha'], sha)
        config = self.root / 'projects.json'
        config.write_text(json.dumps({'version': 1, 'projects': [{'id': 'demo', 'directory': 'demo'}]}))
        environment = dict(os.environ, RESEARCH_ROOT=str(research_root), HERDR_ML_CONFIG=str(config))
        result = subprocess.run([sys.executable, str(MODULE.parent / 'researchctl.py'),
            'init-run', '--project', 'demo', '--mode', 'official', '--dataset-version', 'fixture',
            '--split-hash', 'fixture', '--seed', '42', '--approve-official', '--', *payload],
            env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Official execution BLOCKED', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((research_root / '.runtime' / 'demo' / 'runs').exists())
