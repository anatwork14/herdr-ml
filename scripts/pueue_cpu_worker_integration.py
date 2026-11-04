#!/usr/bin/env python3
"""Run one CPU-only queue-protocol task against a fresh isolated Pueue 4 daemon.

This opt-in harness never contacts the user's default Pueue socket. It creates
private XDG directories and only shuts down the foreground daemon it started.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def run(command, env, *, timeout=15):
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('%s failed (%s): %s' %
                           (Path(command[0]).name, result.returncode,
                            result.stderr.strip()[-1000:]))
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pueue', required=True, type=Path)
    parser.add_argument('--pueued', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    pueue = args.pueue.resolve(strict=True)
    pueued = args.pueued.resolve(strict=True)
    repo = Path(__file__).resolve().parents[1]
    registry_path = repo / 'scripts' / 'run_registry.py'

    with tempfile.TemporaryDirectory(prefix='pueue-isolated-') as temp:
        root = Path(temp)
        os.chmod(root, 0o700)
        config, data, runtime = (root / 'config', root / 'data', root / 'runtime')
        for path in (config, data, runtime):
            path.mkdir(mode=0o700)
        env = {
            'PATH': str(pueue.parent) + os.pathsep + '/usr/bin:/bin',
            'XDG_CONFIG_HOME': str(config),
            'XDG_DATA_HOME': str(data),
            'XDG_RUNTIME_DIR': str(runtime),
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'SHELL': '/bin/sh',
        }
        daemon = subprocess.Popen([str(pueued)], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutdown_sent = False
        shutdown_code = None
        summary = None
        try:
            for _ in range(100):
                if daemon.poll() is not None:
                    raise RuntimeError('isolated Pueue daemon exited during startup')
                probe = subprocess.run([str(pueue), 'status'], env=env,
                                       capture_output=True, text=True, timeout=2)
                if probe.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('isolated Pueue daemon did not become ready')

            run_id = 'cpu-pueue-integration'
            digest = 'c' * 64
            cert = data / 'pueue' / 'certs' / 'daemon.cert'
            if not cert.is_file():
                raise RuntimeError('isolated Pueue daemon certificate is missing')
            queue_instance = hashlib.sha256(cert.read_bytes()).hexdigest()
            run_folder = root / '.runtime' / 'demo' / 'runs' / run_id
            run_folder.mkdir(parents=True, mode=0o700)
            import importlib.util
            spec = importlib.util.spec_from_file_location('queue_test_registry', registry_path)
            registry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(registry)
            account = registry.pwd.getpwuid(os.geteuid())
            isolated_account = registry.pwd.struct_passwd((account.pw_name, account.pw_passwd,
                account.pw_uid, account.pw_gid, account.pw_gecos, str(root), account.pw_shell))
            registry.pwd.getpwuid = lambda uid: isolated_account
            intent = registry.queue_intent(run_folder, run_id, digest, queue_instance)

            task_id_file = root / 'task-id'
            result_file = root / 'worker-result.json'
            worker_script = root / 'worker.py'
            worker_script.write_text('''import importlib.util, json, os, pwd, sys
from pathlib import Path
registry_file, task_id_file, folder, run_id, digest, queue_instance, result_file = sys.argv[1:]
spec = importlib.util.spec_from_file_location("worker_registry", registry_file)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
task_id = Path(task_id_file).read_text().strip()
account = pwd.getpwuid(os.geteuid())
isolated_account = pwd.struct_passwd((account.pw_name, account.pw_passwd,
    account.pw_uid, account.pw_gid, account.pw_gecos,
    str(Path(task_id_file).parent), account.pw_shell))
pwd.getpwuid = lambda uid: isolated_account
worker_id = os.environ.get("PUEUE_WORKER_ID")
if worker_id is None:
    raise RuntimeError("Pueue did not provide PUEUE_WORKER_ID")
fence = registry.queue_claim(Path(folder), run_id, digest, queue_instance, task_id, worker_id)
owner = registry.claim(Path(folder), run_id, "demo", digest, "QUEUED",
                       queue_instance=queue_instance, queue_task_id=task_id,
                       queue_worker_id=worker_id, queue_fence=fence)
registry.advance(Path(folder), owner, "CLAIMED", "DISPATCHING")
registry.advance(Path(folder), owner, "DISPATCHING", "RUNNING")
registry.finish(Path(folder), owner, {"state": "UNKNOWN", "reason": "CPU protocol harness only"})
Path(result_file).write_text(json.dumps({"task_id": task_id, "worker_id": worker_id,
                                         "fence": fence, "queue_task_id":
                                         registry.inspect(Path(folder))["queue_task_id"]}))
''', encoding='utf-8')
            command = [str(Path(args.python).resolve()), str(worker_script),
                       str(registry_path), str(task_id_file), str(run_folder),
                       run_id, digest, queue_instance, str(result_file)]
            task_output = run([str(pueue), 'add', '--stashed', '--print-task-id', '--escape', '--',
                               *command], env)
            if not re.fullmatch(r'[0-9]+', task_output):
                registry.queue_mark_ambiguous(run_folder, intent,
                                              'Pueue returned an unparseable task ID')
                raise RuntimeError('Pueue add response was ambiguous; task was not retried')
            task_id = task_output
            task_id_file.write_text(task_id + '\n', encoding='ascii')
            registry.queue_bind_task(run_folder, intent, task_id)

            status = run([str(pueue), 'status'], env)
            if task_id not in status or 'Stashed' not in status:
                raise RuntimeError('task was not observably stashed before enqueue')
            run([str(pueue), 'enqueue', task_id], env)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not result_file.exists():
                if daemon.poll() is not None:
                    raise RuntimeError('isolated daemon exited while task ran')
                time.sleep(0.1)
            if not result_file.is_file():
                raise RuntimeError('CPU worker did not finish before timeout')
            result = json.loads(result_file.read_text(encoding='utf-8'))
            if result['task_id'] != task_id or result['queue_task_id'] != task_id:
                raise RuntimeError('worker task binding did not match Pueue task ID')
            record = registry.inspect_queue(run_folder)
            if (record['phase'] != 'CLAIMED' or record['task_id'] != task_id or
                    record['worker_id'] != result['worker_id']):
                raise RuntimeError('shared registry does not show the bound worker claim')
            summary = {'pueue_task_id': task_id,
                       'pueue_worker_index': result['worker_id'],
                       'fence_created': bool(result['fence']),
                       'task_finished': True,
                       'isolated_xdg': True}
        finally:
            if daemon.poll() is None:
                try:
                    stopped = subprocess.run([str(pueue), 'shutdown'], env=env,
                                             capture_output=True, text=True,
                                             timeout=5, check=False)
                    shutdown_code = stopped.returncode
                    shutdown_sent = shutdown_code == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                daemon.wait(timeout=5 if shutdown_sent else 1)
            except subprocess.TimeoutExpired:
                daemon.terminate()
                try:
                    daemon.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=3)
        if summary is not None:
            if not shutdown_sent or daemon.returncode != 0:
                raise RuntimeError('isolated daemon did not stop cleanly through its own socket')
            summary.update({'shutdown_command_exit': shutdown_code,
                            'daemon_exit': daemon.returncode,
                            'daemon_stopped': daemon.poll() is not None,
                            'sanitized_environment': sorted(env)})
            print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    main()
