#!/usr/bin/env python3
"""Report or compare the current interpreter environment; not a sandbox.

Use the same absolute interpreter for CLI and worker. A matching fingerprint is
necessary, but does not prove that a daemon actually executed a submitted job.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys


def identity():
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256()
    with executable.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    packages = sorted((d.metadata.get('Name', ''), d.version) for d in importlib.metadata.distributions())
    packages_json = json.dumps(packages, separators=(',', ':'), ensure_ascii=True)
    return {'schema_version': 1, 'executable': str(executable), 'executable_sha256': digest.hexdigest(),
            'python_version': platform.python_version(), 'implementation': platform.python_implementation(),
            'prefix': str(Path(sys.prefix).resolve()), 'base_prefix': str(Path(sys.base_prefix).resolve()),
            'packages_sha256': hashlib.sha256(packages_json.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', type=Path, help='Compare to a previously saved JSON fingerprint')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        print('BLOCKED: Python 3.10 or newer required', file=sys.stderr)
        return 2
    try:
        actual = identity()
        if args.expect:
            expected = json.loads(args.expect.read_text(encoding='utf-8'))
            if actual != expected:
                print('BLOCKED: interpreter fingerprint mismatch', file=sys.stderr)
                return 2
        print(json.dumps(actual, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print('BLOCKED: cannot fingerprint runtime: {}'.format(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
