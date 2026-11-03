"""Best-effort, read-only process identity observations on local Linux procfs.

A match describes only the observed PID at the moment of inspection. A missing
PID is merely not visible; neither observation proves descendant containment,
workload termination, resource release, or authority to retry a run.
"""
import os
import re
import socket
import uuid
from pathlib import Path


def _boot_valid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _ticks_valid(value):
    return (isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,19}', value) is not None
            and int(value) < 2**64)


def _namespace_valid(value, kind):
    return isinstance(value, str) and re.fullmatch(kind + r':\[(?:0|[1-9][0-9]*)\]', value) is not None


def observe_process(identity, expected_host, *, proc_root=Path('/proc'), current_host=None):
    """Compare a recorded PID/start-time/boot identity without sending signals."""
    result = {'observation': 'UNKNOWN', 'reason': 'Incomplete or malformed process identity'}
    if not isinstance(identity, dict):
        return result
    pid, boot, start = identity.get('pid'), identity.get('boot'), identity.get('start_ticks')
    pid_ns, mount_ns = identity.get('pid_namespace'), identity.get('mount_namespace')
    if (type(pid) is not int or pid <= 0 or not _boot_valid(boot) or not _ticks_valid(start)
            or not _namespace_valid(pid_ns, 'pid') or not _namespace_valid(mount_ns, 'mnt')):
        return result
    if not isinstance(expected_host, str) or not expected_host:
        return result
    if current_host is None:
        current_host = socket.gethostname()
    if current_host != expected_host:
        return {'observation': 'UNKNOWN', 'reason': 'Recorded host differs from local host'}
    proc_root = Path(proc_root)
    try:
        local_pid_ns = os.readlink(proc_root / 'self/ns/pid')
        local_mount_ns = os.readlink(proc_root / 'self/ns/mnt')
        if local_pid_ns != pid_ns or local_mount_ns != mount_ns:
            return {'observation': 'UNKNOWN', 'reason': 'Recorded namespaces differ from local procfs context'}
        current_boot = (proc_root / 'sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        if not _boot_valid(current_boot):
            return {'observation': 'UNKNOWN', 'reason': 'Local boot identity unavailable or malformed'}
        if current_boot != boot:
            return {'observation': 'IDENTITY_MISMATCH', 'reason': 'Boot identity differs'}
        try:
            stat = (proc_root / str(pid) / 'stat').read_text(encoding='ascii')
        except FileNotFoundError:
            return {'observation': 'NOT_VISIBLE', 'reason': 'PID not visible in local procfs'}
        end = stat.rfind(')')
        if end < 0 or not stat.startswith(str(pid) + ' ('):
            raise ValueError('invalid process stat header')
        fields = stat[end + 2:].split()
        state, current_start = fields[0], fields[19]
        if not _ticks_valid(current_start) or state not in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I', 'N'):
            raise ValueError('invalid process stat')
        # Both the inspected PID and inspector must still be in the recorded
        # namespaces. A changed or hidden target is ambiguous, not a mismatch.
        if (os.readlink(proc_root / str(pid) / 'ns/pid') != pid_ns
                or os.readlink(proc_root / str(pid) / 'ns/mnt') != mount_ns
                or os.readlink(proc_root / 'self/ns/pid') != local_pid_ns
                or os.readlink(proc_root / 'self/ns/mnt') != local_mount_ns
                or (proc_root / 'sys/kernel/random/boot_id').read_text(encoding='ascii').strip() != current_boot):
            return {'observation': 'UNKNOWN', 'reason': 'Namespace or boot identity changed during inspection'}
        if current_start != start:
            return {'observation': 'IDENTITY_MISMATCH', 'reason': 'PID start time differs'}
        observation = 'MATCH_ZOMBIE' if state in ('Z', 'X', 'x') else 'MATCH'
        return {'observation': observation, 'reason': 'Recorded PID and start time match current procfs',
                'process_state': state}
    except (OSError, UnicodeError, ValueError, IndexError):
        return {'observation': 'UNKNOWN', 'reason': 'Cannot safely inspect local procfs'}
