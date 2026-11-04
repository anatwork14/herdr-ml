#!/usr/bin/env python3
"""Run one CPU-only queue-protocol task against a fresh PRIVATE Pueue 4.0.4 daemon.

Opt-in harness. It never contacts the user's default Pueue socket or config: the
daemon, its socket, state and config live in a new 0700 temporary directory, the
binaries are accepted only by explicit path and pinned sha256, and only the daemon
this harness started is shut down. The queue index lives under an isolated
account home inside the same directory (test-only `_store_home`).

Worker mode (`--worker`) is what the Pueue task runs: it takes the bound task ID
from the queue row, claims idempotently (twice, to show the repeat writes
nothing), records its child, and finishes UNKNOWN. Whole-workload termination is
not verified, so it never reaches COMPLETED.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_adapter  # noqa: E402
import run_registry  # noqa: E402

PROJECT = 'cpu-integration'
RUN_ID = 'cpu-pueue-integration'
DIGEST = 'c' * 64


def run_folder(root, project=PROJECT, run_id=RUN_ID):
    """Standard layout <root>/.runtime/<project>/runs/<run_id>; the project is the folder component."""
    folder = Path(root) / '.runtime' / project / 'runs' / run_id
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    return folder


def worker_command(python, folder, store_home, result_file):
    return [str(Path(python).resolve()), str(Path(__file__).resolve()), '--worker',
            '--folder', str(folder), '--store-home', str(store_home), '--result', str(result_file)]


def worker(folder, store_home, result_file):
    folder = Path(folder)
    project = folder.parents[1].name
    row = run_registry.inspect_queue(folder, _store_home=store_home)
    worker_id = os.environ.get('PUEUE_WORKER_ID')
    if row is None or row['phase'] != 'BOUND' or worker_id is None:
        Path(result_file).write_text(json.dumps({'refused': True,
                                                 'phase': None if row is None else row['phase']}))
        return 3
    worker_name = 'pueue-worker-%s' % worker_id
    args = (folder, row['run_id'], project, row['manifest_sha256'], row['queue_instance'],
            row['task_id'], worker_name)
    first = queue_adapter.claim_attempt(*args, _store_home=store_home)
    second = queue_adapter.claim_attempt(*args, _store_home=store_home)
    token = first['token']
    run_registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
    child = subprocess.Popen(['/bin/sleep', '0.2'])
    run_registry.record_child(folder, token, child.pid)
    run_registry.advance(folder, token, 'DISPATCHING', 'RUNNING')
    exit_code = child.wait()
    run_registry.finish(folder, token, {'state': 'UNKNOWN', 'leader_exit': exit_code,
                                        'reason': 'CPU protocol harness only; termination not verified'})
    binding = queue_adapter.bound_identity(folder, _store_home=store_home)
    Path(result_file).write_text(json.dumps({
        'task_id': row['task_id'], 'worker_id': worker_name, 'first': first, 'second': second,
        'binding': binding, 'state': run_registry.inspect(folder)['state']}, sort_keys=True))
    return 0


def wait_for(path, daemon, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).is_file() and Path(path).stat().st_size:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        if not daemon.running():
            raise RuntimeError('private daemon exited while the task ran')
        time.sleep(0.1)
    raise RuntimeError('CPU worker did not finish before timeout')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pueue', type=Path)
    parser.add_argument('--pueued', type=Path)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--folder')
    parser.add_argument('--store-home')
    parser.add_argument('--result')
    args = parser.parse_args()
    if args.worker:
        return worker(args.folder, args.store_home, args.result)
    if args.pueue is None or args.pueued is None:
        parser.error('--pueue and --pueued are required (no PATH lookup)')

    with tempfile.TemporaryDirectory(prefix='pueue-private-') as temp:
        root = Path(temp)
        os.chmod(root, 0o700)
        home = root / 'home'
        home.mkdir(mode=0o700)
        daemon_root = root / 'daemon'
        daemon_root.mkdir(mode=0o700)
        daemon = queue_adapter.PrivatePueue(daemon_root, args.pueue, args.pueued)
        daemon.start()
        summary = None
        try:
            folder = run_folder(root)
            result_file = root / 'worker-result.json'
            submitted = queue_adapter.submit(daemon, folder, RUN_ID, DIGEST,
                                             worker_command(args.python, folder, home, result_file),
                                             _store_home=home)
            queue_adapter.release(daemon, folder, _store_home=home)
            result = wait_for(result_file, daemon)
            if result.get('refused') or result['task_id'] != submitted['task_id']:
                raise RuntimeError('worker task binding did not match Pueue task ID: %r' % result)
            if (result['first']['attempt_id'] != result['second']['attempt_id'] or
                    not result['second']['replayed']):
                raise RuntimeError('repeated claim was not idempotent')
            record = run_registry.inspect_queue(folder, _store_home=home)
            if record['phase'] != 'CLAIMED' or record['task_id'] != submitted['task_id']:
                raise RuntimeError('queue index does not show the bound worker claim')
            summary = {'pueue_task_id': submitted['task_id'], 'worker_id': result['worker_id'],
                       'attempt_bound': result['binding']['attempt_id'] == record['attempt_id'],
                       'fence': record['fence'],
                       'claim_replayed_without_write': result['second']['replayed'],
                       'local_state': result['state'], 'private_daemon': True}
        finally:
            shutdown_code, daemon_exit = daemon.stop()
        if summary is not None:
            if shutdown_code != 0 or daemon_exit != 0:
                raise RuntimeError('private daemon did not stop cleanly through its own socket')
            summary.update({'shutdown_command_exit': shutdown_code, 'daemon_exit': daemon_exit,
                            'sanitized_environment': sorted(daemon.env())})
            print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
