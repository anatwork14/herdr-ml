"""Best-effort, read-only process identity observations on local Linux procfs.

A match describes only the observed PID at the moment of inspection. A missing
PID is merely not visible; neither observation proves descendant containment,
workload termination, resource release, or authority to retry a run.
"""
import socket
from pathlib import Path


def observe_process(identity, expected_host, *, proc_root=Path('/proc'), current_host=None):
    """Compare a recorded PID/start-time/boot identity without sending signals."""
    result = {'observation': 'UNKNOWN', 'reason': 'Incomplete process identity'}
    if not isinstance(identity, dict):
        return result
    pid, boot, start = identity.get('pid'), identity.get('boot'), identity.get('start_ticks')
    if (type(pid) is not int or pid <= 0 or not isinstance(boot, str) or not boot
            or not isinstance(start, str) or not start.isascii() or not start.isdigit()):
        return result
    if not isinstance(expected_host, str) or not expected_host:
        return result
    if current_host is None:
        current_host = socket.gethostname()
    if current_host != expected_host:
        return {'observation': 'UNKNOWN', 'reason': 'Recorded host differs from local host'}
    proc_root = Path(proc_root)
    try:
        current_boot = (proc_root / 'sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        if not current_boot:
            return {'observation': 'UNKNOWN', 'reason': 'Local boot identity unavailable'}
        if current_boot != boot:
            return {'observation': 'IDENTITY_MISMATCH', 'reason': 'Boot identity differs'}
        try:
            stat = (proc_root / str(pid) / 'stat').read_text(encoding='ascii')
        except FileNotFoundError:
            return {'observation': 'NOT_VISIBLE', 'reason': 'PID not visible in local procfs'}
        end = stat.rfind(')')
        if end < 0:
            raise ValueError('missing comm delimiter')
        fields = stat[end + 2:].split()
        state, current_start = fields[0], fields[19]
        if not current_start.isascii() or not current_start.isdigit() or len(state) != 1:
            raise ValueError('invalid process stat')
        # Avoid a mistaken match if the procfs mount changed during inspection.
        if (proc_root / 'sys/kernel/random/boot_id').read_text(encoding='ascii').strip() != current_boot:
            return {'observation': 'UNKNOWN', 'reason': 'Local boot identity changed during inspection'}
        if current_start != start:
            return {'observation': 'IDENTITY_MISMATCH', 'reason': 'PID start time differs'}
        observation = 'MATCH_ZOMBIE' if state in ('Z', 'X') else 'MATCH'
        return {'observation': observation, 'reason': 'Recorded PID and start time match current procfs',
                'process_state': state}
    except (OSError, UnicodeError, ValueError, IndexError):
        return {'observation': 'UNKNOWN', 'reason': 'Cannot safely inspect local procfs'}
