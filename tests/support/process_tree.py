"""TEST-ONLY disposable CPU process tree for recovery tests. Never import from scripts/.

A tree is parent -> child -> grandchild, all `python -I tree.py <role> <workdir> <marker>`
in a private temp directory:

- parent: publishes its identity, starts the child, waits until the child has
  published, then exits 0 without waiting for it (the child is orphaned).
- child: starts the grandchild, waits for it to publish, publishes, sleeps.
- grandchild: ignores SIGTERM, publishes, sleeps.

Each process publishes {pid, boot, start_ticks, pid_namespace, role} read from its
own /proc/self files, so the recorded start time is the real one even if the PID
is later reused. Every process sleeps at most LIFETIME_S, so a leak is bounded.
The marker is only for the pgrep leak check; nothing is ever signalled by marker.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

LIFETIME_S = 120
PUBLISH_TIMEOUT_S = 15.0
ROLES = ('parent', 'child', 'grandchild')

TREE_SRC = r'''
import json, os, signal, subprocess, sys, time
role, work, marker = sys.argv[1], sys.argv[2], sys.argv[3]
LIFETIME = %(lifetime)d

def identity():
    stat = open('/proc/self/stat').read()
    start = stat[stat.rfind(')') + 2:].split()[19]
    boot = open('/proc/sys/kernel/random/boot_id').read().strip()
    return {'pid': os.getpid(), 'boot': boot, 'start_ticks': start,
            'pid_namespace': os.readlink('/proc/self/ns/pid'), 'role': role}

def publish():
    tmp = os.path.join(work, role + '.tmp')
    with open(tmp, 'w') as handle:
        json.dump(identity(), handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, os.path.join(work, role + '.json'))

def spawn(nxt):
    return subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), nxt, work, marker],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)

def wait_for(name):
    deadline = time.monotonic() + 10
    while not os.path.exists(os.path.join(work, name + '.json')):
        if time.monotonic() > deadline:
            os._exit(3)
        time.sleep(0.01)

if role == 'grandchild':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    publish()
    time.sleep(LIFETIME)
elif role == 'child':
    spawn('grandchild')
    wait_for('grandchild')
    publish()
    time.sleep(LIFETIME)
elif role == 'parent':
    publish()
    spawn('child')
    wait_for('child')
    os._exit(0)
''' % {'lifetime': LIFETIME_S}


def new_marker():
    return 'herdr-recovery-test-' + uuid.uuid4().hex


def write_tree_script(directory):
    path = Path(directory) / 'tree.py'
    path.write_text(TREE_SRC)
    return path


def tree_argv(script, workdir, marker):
    return [sys.executable, '-I', str(script), 'parent', str(workdir), marker]


def start_tree(script, workdir, marker):
    Path(workdir).mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(tree_argv(script, workdir, marker), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True, start_new_session=True)


def published_identities(workdir, *, wait=True, timeout=PUBLISH_TIMEOUT_S):
    """Identities each tree member published about itself. With wait, all three or an error."""
    deadline = time.monotonic() + timeout
    while True:
        found = []
        for role in ROLES:
            path = Path(workdir) / (role + '.json')
            if path.is_file():
                found.append(json.loads(path.read_text()))
        if not wait or len(found) == len(ROLES):
            return found
        if time.monotonic() > deadline:
            raise RuntimeError('process tree did not publish within %.0fs' % timeout)
        time.sleep(0.01)


def pgrep(marker):
    """PIDs whose command line carries this test's marker (pgrep -f). Empty list means none."""
    proc = subprocess.run(['pgrep', '-f', marker], capture_output=True, text=True, timeout=10)
    if proc.returncode not in (0, 1):
        raise RuntimeError('pgrep failed: %s' % proc.stderr)
    return [int(line) for line in proc.stdout.split()]


def wait_no_marked(marker, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        found = pgrep(marker)
        if not found or time.monotonic() > deadline:
            return found
        time.sleep(0.05)
