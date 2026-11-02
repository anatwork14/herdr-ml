#!/usr/bin/env python3
"""Point-in-time diagnostic artifact tree digests, not execution authorization.

This tool never makes source or data immutable, authenticates a manifest, or
proves that a job actually reads these bytes. Concurrent writers and changes
between verification and use require a separate immutable execution boundary.
"""
import hashlib
import os
import re
from pathlib import Path
import stat


MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_ENUMERATED_NAME_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 128


class ArtifactError(Exception):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def snapshot(root, max_entries=100000):
    """Hash a directory without following child symlinks or opening devices."""
    if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
        raise ArtifactError('invalid entry limit')
    records = []
    enumerated = 0
    name_bytes = 0
    def walk(fd, prefix, depth=0):
        nonlocal enumerated, name_bytes
        if depth > MAX_DEPTH:
            raise ArtifactError('directory depth limit exceeded')
        before = os.fstat(fd)
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                enumerated += 1
                name_bytes += len(os.fsencode(entry.name))
                if enumerated > max_entries or name_bytes > MAX_ENUMERATED_NAME_BYTES:
                    raise ArtifactError('entry limit exceeded')
                names.append(entry.name)
        for name in sorted(names):
            if len(records) >= max_entries:
                raise ArtifactError('entry limit exceeded')
            rel = prefix + name
            if len(os.fsencode(rel)) > MAX_PATH_BYTES:
                raise ArtifactError('path length limit exceeded')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if (info.st_dev, info.st_ino) != (os.fstat(child).st_dev, os.fstat(child).st_ino):
                        raise ArtifactError('directory changed during scan')
                    records.append({'path': rel, 'type': 'directory'})
                    walk(child, rel + '/', depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                # O_PATH pins the inode without opening a swapped device/FIFO.
                # Reopen the pinned regular inode via procfs, then verify identity.
                if not hasattr(os, 'O_PATH'):
                    raise ArtifactError('Linux O_PATH support required')
                handle = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    pinned = os.fstat(handle)
                    if not stat.S_ISREG(pinned.st_mode) or _identity(info) != _identity(pinned):
                        raise ArtifactError('file changed before read')
                    child = os.open('/proc/self/fd/%d' % handle, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        opened = os.fstat(child)
                        if _identity(pinned) != _identity(opened):
                            raise ArtifactError('file changed before read')
                        digest = hashlib.sha256()
                        while True:
                            chunk = os.read(child, 1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                        if _identity(opened) != _identity(os.fstat(child)):
                            raise ArtifactError('file changed during read')
                        records.append({'path': rel, 'type': 'file', 'size': opened.st_size,
                                        'sha256': digest.hexdigest()})
                    finally:
                        os.close(child)
                finally:
                    os.close(handle)
            else:
                raise ArtifactError('symlink or non-regular artifact is not allowed')
        if _identity(before) != _identity(os.fstat(fd)):
            raise ArtifactError('directory changed during scan')
    fd = None
    try:
        fd = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        walk(fd, '')
        return {'schema_version': 1, 'entries': records}
    except OSError as exc:
        raise ArtifactError('cannot safely scan artifact tree') from exc
    finally:
        if fd is not None:
            os.close(fd)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'entries'}:
        raise ArtifactError('invalid manifest schema')
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or not isinstance(manifest['entries'], list) or len(manifest['entries']) > 100000:
        raise ArtifactError('invalid manifest schema')
    for entry in manifest['entries']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not entry['path']:
            raise ArtifactError('invalid entry')
        if entry.get('type') == 'directory':
            if set(entry) != {'path', 'type'}:
                raise ArtifactError('invalid directory entry')
        elif entry.get('type') == 'file':
            if (set(entry) != {'path', 'type', 'size', 'sha256'} or type(entry['size']) is not int
                    or entry['size'] < 0 or not isinstance(entry['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                raise ArtifactError('invalid file entry')
        else:
            raise ArtifactError('invalid entry type')


def verify(root, manifest):
    """Compare actual files to a trusted supplied manifest; does not verify trust."""
    validate_manifest(manifest)
    return snapshot(root) == manifest


def main():
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('--verify', type=Path, help='Compare against a previously saved JSON manifest')
    args = parser.parse_args()
    try:
        actual = snapshot(args.root)
        if args.verify:
            with args.verify.open('rb') as source:
                raw = source.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ArtifactError('manifest too large')
            expected = json.loads(raw.decode('utf-8'))
            validate_manifest(expected)
            if actual != expected:
                print('Artifact tree differs', file=sys.stderr)
                return 1
        print(json.dumps(actual, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except (ArtifactError, OSError, ValueError, RecursionError) as exc:
        print('BLOCKED: %s' % exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
