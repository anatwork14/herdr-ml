"""Negative and positive tests for the canonical execution manifest (test-only fixtures).

All keys, repositories and files are created inside per-test temporary directories.
Nothing here launches an experiment, touches a run root or uses a real trust anchor.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import execution_manifest as em  # noqa: E402

GIT = ['git', '-c', 'user.name=test-only', '-c', 'user.email=test-only@example.invalid',
       '-c', 'commit.gpgsign=false']


def git(cwd, *args):
    subprocess.run(GIT + ['-C', str(cwd), *args], check=True, capture_output=True)


class ManifestFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp = Path(os.path.realpath(self.temp_dir.name))
        self.source = self.temp / 'source'
        self.data = self.temp / 'data'
        self.run_dir = self.temp / 'run'
        self.data.mkdir()
        self.run_dir.mkdir()
        self.source.mkdir()
        (self.source / 'lib').mkdir()
        (self.source / 'train.py').write_text('print("train")\n')
        (self.source / 'lib' / 'model.py').write_text('MODEL = 1\n')
        git(self.source, 'init', '-q')
        git(self.source, 'add', '-A')
        git(self.source, 'commit', '-q', '-m', 'test-only fixture')
        self.config = self.data / 'config.json'
        self.config.write_text('{"lr": 0.1}\n')
        self.dataset = self.data / 'dataset.bin'
        self.dataset.write_bytes(b'dataset-bytes-v1')
        self.dataset_manifest = self.data / 'dataset_manifest.json'
        self.dataset_manifest.write_text('{"files": 1}\n')
        self.split = self.data / 'split.json'
        self.split.write_text('{"train": [0], "test": [1]}\n')
        self.client = self.data / 'client.json'
        self.client.write_text('{"clients": ["a", "b"]}\n')
        self.spec = {
            'run_id': 'run-1', 'attempt_id': 'attempt-1', 'project': 'demo',
            'operation': 'diagnostic-execution', 'resource_class': 'cpu-diagnostic',
            'source_root': str(self.source), 'source_files': ['train.py', 'lib/model.py'],
            'args': ['-I', 'train.py'], 'cwd': '.', 'config': str(self.config),
            'inputs': {'dataset_content': str(self.dataset),
                       'dataset_manifest': str(self.dataset_manifest),
                       'split_manifest': str(self.split),
                       'client_manifest': str(self.client)},
            'primary_seed': 7, 'named_seeds': {'numpy': 7},
            'nondeterminism': [], 'output_root': 'run-output', 'output_path': 'result.json'}

    def build(self, **overrides):
        spec = dict(self.spec, **overrides)
        manifest = em.build_manifest(**spec)
        return manifest, em.encode_manifest(manifest)

    def seal_dir(self):
        return tempfile.mkdtemp(dir=str(self.temp))

    def verify(self, data, *, expected=None, attempt_id='attempt-1', run_id='run-1',
               worker_id='worker-1', source_root=None, seal=None):
        digest = expected if expected is not None else em.sha256_hex(data)
        return em.verify_before_execution(
            data, expected_sha256=digest, run_id=run_id, attempt_id=attempt_id,
            worker_id=worker_id, source_root=str(source_root or self.source),
            run_dir=str(self.run_dir), seal_dir=seal or self.seal_dir())

    def assertCode(self, code, func, *args, **kwargs):
        with self.assertRaises(em.VerificationError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.detail)
        return ctx.exception


class CanonicalManifestTests(ManifestFixture):
    def test_builds_deterministically_and_verifies_from_clean_checkout(self):
        _, first = self.build()
        _, second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(first, em.canonical_bytes(json.loads(first)))
        plan = self.verify(first)
        self.assertEqual(plan['manifest_sha256'], em.sha256_hex(first))
        self.assertEqual(plan['source_commit'], subprocess.run(
            GIT + ['-C', str(self.source), 'rev-parse', 'HEAD'], capture_output=True,
            text=True, check=True).stdout.strip())
        roles = sorted(item['role'] for item in plan['inputs_used'])
        self.assertEqual(roles, ['client_manifest', 'config', 'dataset_content',
                                 'dataset_manifest', 'split_manifest'])
        self.assertEqual(len(plan['effective_runtime']['interpreter_sha256']), 64)

    def test_commit_hash_alone_is_not_accepted_as_identity(self):
        manifest, _ = self.build()
        manifest['source'] = {'commit': manifest['source']['commit'], 'worktree_clean': True}
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)

    def test_source_must_be_declared_clean(self):
        manifest, _ = self.build()
        manifest['source']['worktree_clean'] = False
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)

    def test_floats_and_nonfinite_values_are_rejected(self):
        self.assertCode(em.MANIFEST_SCHEMA, em.canonical_bytes, {'lr': 0.1})
        self.assertCode(em.MANIFEST_SCHEMA, em.canonical_bytes, {'flag': (1, 2)})
        self.assertCode(em.MANIFEST_SCHEMA, em.canonical_bytes, {'big': 2 ** 60})
        _, data = self.build()
        self.assertCode(em.MANIFEST_MALFORMED, em.parse_manifest,
                        data.replace(b'"primary":7', b'"primary":7.0'))
        self.assertCode(em.MANIFEST_MALFORMED, em.parse_manifest,
                        data.replace(b'"primary":7', b'"primary":NaN'))

    def test_truncated_and_duplicate_key_manifests_fail_closed(self):
        _, data = self.build()
        self.assertCode(em.MANIFEST_MALFORMED, em.parse_manifest, data[:-7])
        self.assertCode(em.MANIFEST_MALFORMED, em.parse_manifest, b'')
        self.assertCode(em.MANIFEST_MALFORMED, em.parse_manifest, b'\xff\xfe{')
        self.assertCode(em.MANIFEST_MALFORMED, em.parse_manifest,
                        data.replace(b'{"', b'{"kind":"x","', 1))

    def test_non_canonical_manifest_bytes_are_rejected(self):
        manifest, data = self.build()
        pretty = json.dumps(manifest, indent=2, sort_keys=True).encode('ascii')
        self.assertCode(em.MANIFEST_NONCANONICAL, em.parse_manifest, pretty)
        self.assertCode(em.MANIFEST_NONCANONICAL, em.parse_manifest, data + b'\n')

    def test_digest_computed_over_a_different_serialization_is_rejected(self):
        manifest, data = self.build()
        pretty = json.dumps(manifest, indent=2, sort_keys=True).encode('ascii')
        self.assertCode(em.MANIFEST_DIGEST_MISMATCH, self.verify, data, expected=em.sha256_hex(pretty))
        self.assertCode(em.MANIFEST_DIGEST_MISMATCH, self.verify, pretty, expected=em.sha256_hex(data))
        # The digest matches the presented bytes, but the bytes are not canonical.
        self.assertCode(em.MANIFEST_NONCANONICAL, self.verify, pretty, expected=em.sha256_hex(pretty))

    def test_tampered_schema_fields_fail(self):
        manifest, _ = self.build()
        manifest['interpreter']['path'] = 'relative/python'
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)
        manifest, _ = self.build()
        manifest['command']['argv'][0] = '/usr/bin/env'
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)
        manifest, _ = self.build()
        manifest['output']['path'] = '../escape.json'
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)
        manifest, _ = self.build()
        manifest['runtime']['packages_sha256'] = '0' * 64
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)


class SourceBindingTests(ManifestFixture):
    def test_source_mutation_after_manifest_creation_is_rejected(self):
        _, data = self.build()
        (self.source / 'train.py').write_text('print("changed")\n')
        self.assertCode(em.SOURCE_MUTATED, self.verify, data)

    def test_untracked_file_makes_checkout_dirty(self):
        _, data = self.build()
        (self.source / 'notes.txt').write_text('extra\n')
        self.assertCode(em.SOURCE_DIRTY, self.verify, data)

    def test_new_commit_is_a_different_source(self):
        _, data = self.build()
        (self.source / 'train.py').write_text('print("v2")\n')
        git(self.source, 'commit', '-q', '-am', 'change')
        self.assertCode(em.SOURCE_COMMIT_MISMATCH, self.verify, data)

    def test_missing_source_file_is_rejected(self):
        _, data = self.build()
        (self.source / 'lib' / 'model.py').unlink()
        git(self.source, 'add', '-A')
        git(self.source, 'commit', '-q', '-m', 'drop model')
        self.assertCode(em.SOURCE_COMMIT_MISMATCH, self.verify, data)
        # Rebuilding against the new head still lacks a declared file.
        self.assertCode(em.SOURCE_FILE_MISSING, em.build_manifest, **self.spec)

    def test_builder_refuses_a_dirty_checkout(self):
        (self.source / 'train.py').write_text('print("dirty")\n')
        self.assertCode(em.SOURCE_DIRTY, self.build)

    def test_non_toplevel_source_root_is_unverifiable(self):
        _, data = self.build()
        (self.source / 'lib').mkdir(exist_ok=True)
        self.assertCode(em.SOURCE_UNVERIFIABLE, self.verify, data, source_root=self.source / 'lib')

    def test_symlink_in_source_path_is_rejected(self):
        _, data = self.build()
        target = self.temp / 'outside.py'
        target.write_text('print("outside")\n')
        (self.source / 'train.py').unlink()
        os.symlink(target, self.source / 'train.py')
        self.assertCode(em.SOURCE_UNVERIFIABLE, self.verify, data)


class InputBindingTests(ManifestFixture):
    def test_dataset_replacement_is_rejected(self):
        _, data = self.build()
        self.dataset.write_bytes(b'dataset-bytes-v2')
        self.assertCode(em.DATASET_DIGEST_MISMATCH, self.verify, data)

    def test_split_manifest_substitution_is_rejected(self):
        _, data = self.build()
        self.split.write_text('{"train": [1], "test": [0]}\n')
        self.assertCode(em.SPLIT_MANIFEST_MISMATCH, self.verify, data)

    def test_client_manifest_substitution_is_rejected(self):
        _, data = self.build()
        self.client.write_text('{"clients": ["a", "c"]}\n')
        self.assertCode(em.SPLIT_MANIFEST_MISMATCH, self.verify, data)

    def test_missing_input_is_rejected(self):
        _, data = self.build()
        self.dataset_manifest.unlink()
        self.assertCode(em.INPUT_MISSING, self.verify, data)

    def test_truncated_corrupted_config_is_rejected(self):
        _, data = self.build()
        self.config.write_bytes(self.config.read_bytes()[:5])
        self.assertCode(em.INPUT_DIGEST_MISMATCH, self.verify, data)

    def test_symlink_input_is_rejected(self):
        _, data = self.build()
        copy = self.temp / 'copy.bin'
        copy.write_bytes(self.dataset.read_bytes())
        self.dataset.unlink()
        os.symlink(copy, self.dataset)
        self.assertCode(em.INPUT_UNSAFE_PATH, self.verify, data)

    def test_fifo_input_is_rejected_without_blocking(self):
        _, data = self.build()
        self.dataset.unlink()
        os.mkfifo(self.dataset)
        self.assertCode(em.INPUT_UNSAFE_PATH, self.verify, data)

    def test_sealed_copy_is_what_was_hashed(self):
        _, data = self.build()
        expected = {entry['role']: entry['sha256'] for entry in em.parse_manifest(data)['inputs']}
        plan = self.verify(data)
        self.dataset.write_bytes(b'mutated after verification')
        sealed = Path(plan['sealed_inputs']['dataset_content'])
        self.assertEqual(em.sha256_hex(sealed.read_bytes()), expected['dataset_content'])
        self.assertNotEqual(em.sha256_hex(self.dataset.read_bytes()), expected['dataset_content'])

    def test_seal_directory_must_be_private(self):
        _, data = self.build()
        seal = self.seal_dir()
        os.chmod(seal, 0o755)
        self.assertCode(em.SEAL_DIR_UNSAFE, self.verify, data, seal=seal)


class InterpreterAndRuntimeTests(ManifestFixture):
    def test_interpreter_digest_replacement_is_rejected(self):
        manifest, _ = self.build()
        manifest['interpreter']['sha256'] = '1' * 64
        self.assertCode(em.INTERPRETER_DIGEST_MISMATCH, self.verify, em.encode_manifest(manifest))

    def test_interpreter_path_replacement_is_rejected(self):
        other = self.data / 'other-python'
        other.write_bytes(b'#!/bin/sh\n')
        manifest, _ = self.build()
        manifest['interpreter']['path'] = str(other)
        manifest['command']['argv'][0] = str(other)
        self.assertCode(em.INTERPRETER_PATH_MISMATCH, self.verify, em.encode_manifest(manifest))

    def test_runtime_drift_in_process_is_rejected(self):
        manifest, _ = self.build()
        packages = manifest['runtime']['packages'] + [['zz-fake-pkg', '1.0']]
        manifest['runtime']['packages'] = sorted(packages)
        manifest['runtime']['packages_sha256'] = em.digest_value(manifest['runtime']['packages'])
        self.assertCode(em.RUNTIME_DRIFT, self.verify, em.encode_manifest(manifest))

    def test_dependency_version_change_between_creation_and_worker_is_rejected(self):
        env_v1 = self.fake_env('1.0')
        env_v2 = self.fake_env('1.1')
        spec_path = self.temp / 'spec.json'
        spec_path.write_text(json.dumps(self.spec))
        manifest_path = self.temp / 'manifest.json'
        child_build = (
            'import json,sys; sys.path.insert(0, sys.argv[1]); import execution_manifest as em\n'
            'spec = json.load(open(sys.argv[2])); m = em.build_manifest(**spec)\n'
            'open(sys.argv[3], "wb").write(em.encode_manifest(m))\n')
        built = subprocess.run([sys.executable, '-c', child_build, str(SCRIPTS), str(spec_path),
                                str(manifest_path)], env=env_v1, capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        data = manifest_path.read_bytes()
        digest = em.sha256_hex(data)
        child_verify = (
            'import sys; sys.path.insert(0, sys.argv[1]); import execution_manifest as em\n'
            'data = open(sys.argv[2], "rb").read()\n'
            'try:\n'
            '    em.verify_before_execution(data, expected_sha256=sys.argv[3], run_id="run-1",\n'
            '        attempt_id="attempt-1", worker_id="worker-1", source_root=sys.argv[4],\n'
            '        run_dir=sys.argv[5], seal_dir=sys.argv[6])\n'
            '    print("PASSED")\n'
            'except em.VerificationError as exc:\n'
            '    print(exc.code)\n')
        seal_same = self.seal_dir()
        seal_drift = self.seal_dir()
        args = [str(SCRIPTS), str(manifest_path), digest, str(self.source), str(self.run_dir)]
        same = subprocess.run([sys.executable, '-c', child_verify, *args, seal_same],
                              env=env_v1, capture_output=True, text=True)
        self.assertEqual(same.stdout.strip(), 'PASSED', same.stderr)
        drift = subprocess.run([sys.executable, '-c', child_verify, *args, seal_drift],
                               env=env_v2, capture_output=True, text=True)
        self.assertEqual(drift.stdout.strip(), em.RUNTIME_DRIFT, drift.stderr)

    def fake_env(self, version):
        root = self.temp / ('pkgs-' + version)
        info = root / ('zz_fake_dep-%s.dist-info' % version)
        info.mkdir(parents=True)
        (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: zz-fake-dep\nVersion: %s\n' % version)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(root)
        return env


class WorkerIdentityAndOutputTests(ManifestFixture):
    def test_wrong_attempt_or_run_is_rejected(self):
        _, data = self.build()
        self.assertCode(em.ATTEMPT_ID_MISMATCH, self.verify, data, attempt_id='attempt-2')
        self.assertCode(em.RUN_ID_MISMATCH, self.verify, data, run_id='run-2')

    def test_invalid_worker_identity_is_rejected(self):
        _, data = self.build()
        self.assertCode(em.WORKER_ID_INVALID, self.verify, data, worker_id='../worker')

    def test_existing_or_escaping_output_destination_is_rejected(self):
        _, data = self.build()
        out_dir = self.run_dir / 'run-output'
        out_dir.mkdir()
        (out_dir / 'result.json').write_text('{}\n')
        self.assertCode(em.OUTPUT_DESTINATION_INVALID, self.verify, data)
        (out_dir / 'result.json').unlink()
        outside = self.temp / 'outside'
        outside.mkdir()
        out_dir.rmdir()
        os.symlink(outside, self.run_dir / 'run-output')
        self.assertCode(em.OUTPUT_DESTINATION_INVALID, self.verify, data)

    def test_result_record_is_unknown_without_verified_containment(self):
        _, data = self.build()
        plan = self.verify(data)
        out_dir = self.run_dir / 'run-output'
        out_dir.mkdir()
        result = out_dir / 'result.json'
        result.write_text('{"metric": 1}\n')
        record, raw, record_sha = em.build_result_record(plan, result_path=str(result), exit_status=0)
        self.assertEqual(record['status'], 'UNKNOWN')
        self.assertEqual(record['containment'], {'status': 'UNKNOWN', 'verifier_id': None,
                                                 'evidence_sha256': None})
        self.assertEqual(record['worker_id'], 'worker-1')
        self.assertEqual(record['manifest_sha256'], em.sha256_hex(data))
        self.assertEqual(record['result_artifact']['sha256'], em.sha256_hex(result.read_bytes()))
        self.assertEqual(len(record_sha), 64)
        self.assertEqual(raw, em.canonical_bytes(record))

    def test_missing_result_artifact_is_rejected(self):
        _, data = self.build()
        plan = self.verify(data)
        self.assertCode(em.RESULT_ARTIFACT_MISSING, em.build_result_record, plan,
                        result_path=str(self.run_dir / 'absent.json'), exit_status=0)

    def test_containment_evidence_must_match_attempt(self):
        _, data = self.build()
        plan = self.verify(data)
        out_dir = self.run_dir / 'run-output'
        out_dir.mkdir()
        result = out_dir / 'result.json'
        result.write_text('{}\n')
        evidence = {'status': 'VERIFIED', 'verifier_id': 'w2-verifier', 'evidence_sha256': '2' * 64,
                    'run_id': 'run-1', 'attempt_id': 'attempt-2'}
        self.assertCode(em.CONTAINMENT_MISMATCH, em.build_result_record, plan,
                        result_path=str(result), exit_status=0, containment=evidence)
        evidence['attempt_id'] = 'attempt-1'
        record, _, _ = em.build_result_record(plan, result_path=str(result), exit_status=0,
                                              containment=evidence)
        self.assertEqual(record['status'], 'COMPLETED')
        self.assertEqual(record['containment']['status'], 'VERIFIED')


class ReviewRegressionTests(ManifestFixture):
    """Regression tests for independent review findings H3, L1, L3 and the gap list."""

    def test_repo_local_clean_filter_is_refused_and_never_runs(self):
        marker = self.temp / 'filter-ran.marker'
        repo = self.temp / 'filter-repo'
        repo.mkdir()
        (repo / 'a.txt').write_text('one\n')
        git(repo, 'init', '-q')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', 'base')
        (repo / '.gitattributes').write_text('*.txt filter=marker\n')
        git(repo, 'add', '.gitattributes')
        git(repo, 'commit', '-q', '-m', 'attributes')
        git(repo, 'config', 'filter.marker.clean', 'touch %s && cat' % marker)
        (repo / 'a.txt').write_text('two\n')  # modified tracked file
        spec = dict(self.spec, source_root=str(repo), source_files=['a.txt'])
        self.assertCode(em.SOURCE_UNVERIFIABLE, em.build_manifest, **spec)
        self.assertFalse(marker.exists(), 'clean filter must not run')

    def test_repo_local_diff_driver_is_refused(self):
        repo = self.temp / 'diff-repo'
        repo.mkdir()
        (repo / 'a.txt').write_text('one\n')
        git(repo, 'init', '-q')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', 'base')
        git(repo, 'config', 'diff.evil.textconv', 'false')
        spec = dict(self.spec, source_root=str(repo), source_files=['a.txt'])
        self.assertCode(em.SOURCE_UNVERIFIABLE, em.build_manifest, **spec)

    def test_ignored_file_makes_checkout_dirty(self):
        (self.source / '.gitignore').write_text('cache.bin\n')
        git(self.source, 'add', '.gitignore')
        git(self.source, 'commit', '-q', '-m', 'ignore cache')
        (self.source / 'cache.bin').write_bytes(b'not part of the declared files')
        self.assertCode(em.SOURCE_DIRTY, self.build)

    def test_input_changed_during_read_is_detected(self):
        path = self.data / 'growing.bin'
        path.write_bytes(b'abc')
        real_read = os.read
        calls = {'n': 0}

        def growing_read(fd, count):
            data = real_read(fd, count)
            if calls['n'] == 0:
                with open(path, 'ab') as handle:
                    handle.write(b'def')
            calls['n'] += 1
            return data

        with mock.patch.object(em.os, 'read', growing_read):
            with self.assertRaises(em.VerificationError) as ctx:
                em._consume(str(path), missing=em.INPUT_MISSING, unsafe=em.INPUT_UNSAFE_PATH)
        self.assertEqual(ctx.exception.code, em.INPUT_CHANGED_DURING_READ)

    def test_symlink_in_parent_directory_of_source_is_rejected(self):
        real_dir = self.temp / 'real-lib'
        real_dir.mkdir()
        (real_dir / 'model.py').write_text('MODEL = 2\n')
        os.symlink(real_dir, self.source / 'link-lib')
        self.assertCode(em.SOURCE_UNVERIFIABLE, em._source_file, str(self.source), 'link-lib/model.py')

    def test_symlink_in_parent_directory_of_input_is_rejected(self):
        os.symlink(self.data, self.temp / 'link-data')
        spec = dict(self.spec, inputs=dict(self.spec['inputs'],
                                            dataset_content=str(self.temp / 'link-data' / 'dataset.bin')))
        self.assertCode(em.INPUT_UNSAFE_PATH, em.build_manifest, **spec)

    def test_surrogate_code_points_cannot_alias_real_characters(self):
        self.assertEqual(em.canonical_bytes({'x': '\U0001F600'}), b'{"x":"\\ud83d\\ude00"}')
        self.assertCode(em.MANIFEST_SCHEMA, em.canonical_bytes, {'x': '\ud83d\ude00'})
        manifest, data = self.build()
        manifest['inputs'][0]['path'] = manifest['inputs'][0]['path'] + '\ud83d'
        self.assertCode(em.MANIFEST_SCHEMA, em.encode_manifest, manifest)
        self.assertCode(em.MANIFEST_SCHEMA, em.parse_manifest, data.replace(b'"cwd":"."', b'"cwd":"\\ud83d"'))

    def test_pre_existing_sealed_file_survives_a_failed_seal(self):
        _, data = self.build()
        seal = self.seal_dir()
        precious = Path(seal) / 'config.sealed'
        precious.write_bytes(b'precious')
        self.assertCode(em.INPUT_UNSAFE_PATH, self.verify, data, seal=seal)
        self.assertEqual(precious.read_bytes(), b'precious')

    def test_failed_seal_removes_only_files_it_created(self):
        _, data = self.build()
        self.dataset.write_bytes(b'dataset-bytes-v2')
        seal = self.seal_dir()
        self.assertCode(em.DATASET_DIGEST_MISMATCH, self.verify, data, seal=seal)
        self.assertEqual(sorted(os.listdir(seal)), [])


if __name__ == '__main__':
    unittest.main()
