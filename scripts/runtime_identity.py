#!/usr/bin/env python3
"""Compare diagnostic interpreter fingerprints; this is not an attestation.

Use the same absolute interpreter, flags, environment and working directory for
CLI and worker. A standalone match is not evidence of any particular run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import sysconfig


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def identity():
    import importlib.metadata
    if not sys.executable:
        raise ValueError('interpreter executable unavailable')
    executable = Path(sys.executable).resolve(strict=True)
    if sys.platform.startswith('linux'):
        proc_executable = os.readlink('/proc/self/exe')
        if proc_executable.endswith(' (deleted)') or Path(proc_executable).resolve(strict=True) != executable:
            raise ValueError('sys.executable differs from running image')
    libpython = []
    if sys.platform.startswith('linux'):
        mappings = set()
        with open('/proc/self/maps', encoding='utf-8') as maps:
            for line in maps:
                parts = line.split(None, 5)
                if len(parts) < 6:
                    continue
                path = parts[5].strip()
                if re.match(r'^libpython[0-9].*\.so(?:\.[0-9]+)*$', Path(path.removesuffix(' (deleted)')).name):
                    if path.endswith(' (deleted)'):
                        raise ValueError('mapped libpython is deleted')
                    mappings.add(str(Path(path).resolve(strict=True)))
        libpython = [{'path': path, 'sha256': sha_file(path)} for path in sorted(mappings)]
    packages = sorted((str(d.metadata.get('Name') or ''), str(d.version or ''))
                      for d in importlib.metadata.distributions())
    packages_json = json.dumps(packages, separators=(',', ':'), ensure_ascii=True)
    flag_names = ('debug', 'inspect', 'interactive', 'optimize', 'dont_write_bytecode',
                  'no_user_site', 'no_site', 'ignore_environment', 'verbose', 'bytes_warning',
                  'quiet', 'hash_randomization', 'isolated', 'dev_mode', 'utf8_mode',
                  'warn_default_encoding', 'safe_path', 'int_max_str_digits')
    build = {'version_sha256': hashlib.sha256(sys.version.encode()).hexdigest(),
             'ldlibrary': sysconfig.get_config_var('LDLIBRARY'),
             'py_enable_shared': sysconfig.get_config_var('Py_ENABLE_SHARED')}
    return {'schema_version': 2, 'executable': str(executable), 'executable_sha256': sha_file(executable),
            'python_version': platform.python_version(), 'implementation': platform.python_implementation(),
            'prefix': str(Path(sys.prefix).resolve()), 'base_prefix': str(Path(sys.base_prefix).resolve()),
            'cwd': str(Path.cwd()), 'sys_path': list(sys.path),
            'flags': {name: getattr(sys.flags, name) for name in flag_names if hasattr(sys.flags, name)},
            'stdlib': sysconfig.get_path('stdlib'), 'build': build, 'libpython': libpython,
            'startup_modules_loaded': {name: name in sys.modules for name in ('sitecustomize', 'usercustomize')},
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
            if not isinstance(expected, dict) or actual != expected:
                print('BLOCKED: interpreter fingerprint mismatch', file=sys.stderr)
                return 2
        print(json.dumps(actual, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print('BLOCKED: cannot fingerprint runtime: {}'.format(type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
