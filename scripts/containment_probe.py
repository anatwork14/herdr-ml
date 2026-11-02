#!/usr/bin/env python3
"""Read-only containment preflight. A capability hint never enables GPU work."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), value)


def discover_v2(cgroup_text, mountinfo_text):
    """Find current v2 cgroup in a visible cgroup2 mount, never traverse above it."""
    membership = next((line[3:] for line in cgroup_text.splitlines() if line.startswith('0::/')), None)
    if membership is None:
        return None
    member = PurePosixPath(membership)
    if '..' in member.parts:
        return None
    matches = []
    for line in mountinfo_text.splitlines():
        left, sep, right = line.partition(' - ')
        if not sep or not right.startswith('cgroup2 '):
            continue
        fields = left.split()
        if len(fields) < 6:
            continue
        root = PurePosixPath(_unescape(fields[3]))
        mount = PurePosixPath(_unescape(fields[4]))
        try:
            suffix = member.relative_to(root)
        except ValueError:
            continue
        if not root.is_absolute() or not mount.is_absolute() or '..' in root.parts or '..' in mount.parts:
            continue
        matches.append((len(root.parts), str(mount / suffix)))
    return max(matches)[1] if matches else None


def probe():
    report = {'schema_version': 1, 'verdict': 'BLOCKED',
              'reason': 'No integrated, independently verified containment backend; GPU and queue holds remain.',
              'linux': sys.platform.startswith('linux'), 'current_cgroup_v2': None,
              'current_cgroup_writable': False, 'current_cgroup_procs_writable': False,
              'current_cgroup_kill_present': False, 'current_cgroup_kill_writable': False,
              'user_systemd_bus_reachable': False, 'observations': []}
    if not report['linux']:
        report['observations'].append('Linux cgroup inspection unavailable on this platform')
        return report
    try:
        cgroup = discover_v2(Path('/proc/self/cgroup').read_text(), Path('/proc/self/mountinfo').read_text())
        if cgroup:
            path = Path(cgroup)
            report['current_cgroup_v2'] = str(path)
            report['current_cgroup_writable'] = os.access(path, os.W_OK)
            report['current_cgroup_procs_writable'] = os.access(path / 'cgroup.procs', os.W_OK)
            report['current_cgroup_kill_present'] = (path / 'cgroup.kill').exists()
            report['current_cgroup_kill_writable'] = os.access(path / 'cgroup.kill', os.W_OK)
        else:
            report['observations'].append('Current cgroup v2 path is not visible')
    except (OSError, ValueError) as exc:
        report['observations'].append('Cannot inspect cgroup: ' + type(exc).__name__)
    systemctl = shutil.which('systemctl')
    if systemctl:
        try:
            # Discard output: the user manager may expose private environment values.
            result = subprocess.run([systemctl, '--user', 'show-environment'], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
            report['user_systemd_bus_reachable'] = result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            report['observations'].append('Cannot query user systemd: ' + type(exc).__name__)
    else:
        report['observations'].append('systemctl unavailable')
    report['observations'].append('Writability is only a hint, not evidence of delegation, isolation or safe release')
    return report


def main():
    print(json.dumps(probe(), indent=2, sort_keys=True))
    return 2  # A preflight cannot certify process-tree containment.


if __name__ == '__main__':
    sys.exit(main())
