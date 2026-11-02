#!/usr/bin/env python3
"""Verify a signed provenance envelope; this is not an execution authorizer.

The caller must supply an independently administered allowed-signers file and
principal. Verification does not lock artifacts or prove scientific validity.
Official execution remains disabled until a trusted execution boundary binds
verified immutable artifacts to the actual process and environment.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

NAMESPACE = 'herdr-ml-official-v1'
REQUIRED_ARTIFACTS = frozenset(('source', 'environment', 'dataset_manifest',
    'dataset_content', 'train_split', 'validation_split', 'test_split',
    'config', 'protocol', 'baseline', 'review_evidence'))


class ProvenanceError(Exception):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError('duplicate JSON key')
        result[key] = value
    return result


def _hash_relative(root, name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ProvenanceError('invalid artifact path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ProvenanceError('artifact path must be relative and normalized')
    directory = None
    file_fd = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[:-1]:
            next_dir = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_dir
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProvenanceError('artifact must be a regular file')
        digest = hashlib.sha256()
        with os.fdopen(file_fd, 'rb') as stream:
            file_fd = None
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
            after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ProvenanceError('artifact changed during verification')
        return digest.hexdigest()
    except OSError as exc:
        raise ProvenanceError('cannot safely read artifact') from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def verify_bundle(manifest_path, signature_path, allowed_signers, principal):
    """Verify signed bytes and all named artifact bytes, returning parsed metadata.

    This is point-in-time checking, not a filesystem isolation or authorization
    boundary. Never use this as the sole precondition for launching a run.
    """
    if not isinstance(principal, str) or not principal or principal.startswith('-') or '\n' in principal:
        raise ProvenanceError('invalid trusted principal')
    try:
        manifest_path = Path(manifest_path)
        with manifest_path.open('rb') as handle:
            data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ProvenanceError('manifest too large')
        verified = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(allowed_signers),
            '-I', principal, '-n', NAMESPACE, '-s', str(signature_path)], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        if verified.returncode:
            raise ProvenanceError('signature verification failed')
        manifest = json.loads(data, object_pairs_hook=_object)
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError('unable to verify signed manifest') from exc
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ProvenanceError('invalid manifest schema')
    if not isinstance(manifest.get('project'), str) or not manifest['project'].strip():
        raise ProvenanceError('missing project')
    if not isinstance(manifest.get('source_git_sha'), str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', manifest['source_git_sha']):
        raise ProvenanceError('invalid Git object ID')
    argv = manifest.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and '\x00' not in arg for arg in argv) or not argv[0]:
        raise ProvenanceError('invalid command argv')
    if type(manifest.get('seed')) is not int:
        raise ProvenanceError('invalid seed')
    artifacts = manifest.get('artifacts')
    if not isinstance(artifacts, dict) or set(artifacts) != REQUIRED_ARTIFACTS:
        raise ProvenanceError('missing or unsupported artifact roles')
    paths = set()
    for role, entry in artifacts.items():
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}:
            raise ProvenanceError('invalid artifact record: ' + role)
        name, expected = entry['path'], entry['sha256']
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ProvenanceError('invalid artifact hash: ' + role)
        if not isinstance(name, str) or name in paths:
            raise ProvenanceError('duplicate or invalid artifact path')
        paths.add(name)
        if _hash_relative(manifest_path.parent, name) != expected:
            raise ProvenanceError('artifact digest mismatch: ' + role)
    return manifest
