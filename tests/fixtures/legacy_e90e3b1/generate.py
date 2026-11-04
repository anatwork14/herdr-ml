"""Generate legacy ENQUEUE_REFUSED fixtures with the e90e3b1 code against a private pinned daemon.

Usage: python3 generate.py <e90e3b1 scripts dir> <out dir>
Runs only against a mkdtemp 0700 private root. Never touches the default Pueue config.
"""
import hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

SCRIPTS, OUT = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(SCRIPTS))
import queue_adapter as qa  # historical module (e90e3b1)
import run_registry as reg  # historical module (e90e3b1)

BIN = Path('/mnt/ssd/genos2/workspace/herdr-ml-pueue-test/bin')
PUEUE, PUEUED = BIN / 'pueue-x86_64-unknown-linux-musl', BIN / 'pueued-x86_64-unknown-linux-musl'
assert qa.sha256_file(PUEUED) == '5afeff6adbafb909e8d54e2caff158e6966c2adffa2c09e60fd631cc51b60390'
assert qa.sha256_file(PUEUE) == 'c1b10d7e4e62211075ddd0e1dc3e8cbfc5a43d662cb3be7402a28504e23fcb51'
DIGEST = 'a' * 64
BOGUS_TASK = '4294967'  # not a task in the daemon: the real client exits non-zero

base = Path(tempfile.mkdtemp(prefix='p7w2-'))
os.chmod(base, 0o700)
home = base / 'home'; home.mkdir(mode=0o700)
(base / 'daemon').mkdir(mode=0o700)
daemon = qa.PrivatePueue(base / 'daemon', PUEUE, PUEUED)
daemon.start()
real = daemon.client
try:
    def folder(run):
        f = base / '.runtime' / 'project-a' / 'runs' / run
        f.mkdir(parents=True, exist_ok=True)
        return f

    def refusing_once(bogus_calls):
        def client(*args, **kw):
            if args and args[0] == 'enqueue' and bogus_calls:
                bogus_calls.pop()
                args = ('enqueue', BOGUS_TASK)
            return real(*args, **kw)
        return client

    # run-1: one refused enqueue, nothing else (BOUND + ENQUEUE_REFUSED)
    f1 = folder('run-1')
    s1 = qa.submit(daemon, f1, 'run-1', DIGEST, ['/bin/true'], _store_home=home)
    daemon.client = refusing_once([1])
    try:
        qa.release(daemon, f1, _store_home=home); raise SystemExit('expected refusal')
    except qa.QueueAdapterError as exc:
        print('run-1 refusal:', exc)
    # run-2: refused, then the historical code re-released the same BOUND row, which enqueued
    f2 = folder('run-2')
    s2 = qa.submit(daemon, f2, 'run-2', DIGEST, ['/bin/true'], _store_home=home)
    daemon.client = refusing_once([1])
    try:
        qa.release(daemon, f2, _store_home=home); raise SystemExit('expected refusal')
    except qa.QueueAdapterError as exc:
        print('run-2 refusal:', exc)
    daemon.client = real
    print('run-2 re-release:', qa.release(daemon, f2, _store_home=home))
    # run-3: refused, then claimed by a worker on the same BOUND row
    f3 = folder('run-3')
    s3 = qa.submit(daemon, f3, 'run-3', DIGEST, ['/bin/true'], _store_home=home)
    daemon.client = refusing_once([1])
    try:
        qa.release(daemon, f3, _store_home=home); raise SystemExit('expected refusal')
    except qa.QueueAdapterError as exc:
        print('run-3 refusal:', exc)
    daemon.client = real
    claim = reg.queue_claim(f3, 'run-3', DIGEST, daemon.instance_id(), s3['task_id'], 'worker-1', _store_home=home)
    print('run-3 claim fence:', claim)
    status = real('status', '--json')
    task_states = {k: v.get('status') for k, v in json.loads(status.stdout)['tasks'].items()}
finally:
    daemon.stop()
    shutil.rmtree(daemon.root, ignore_errors=True)  # daemon state is not part of the fixture

db_src = home / '.local' / 'state' / 'herdr-ml' / 'queue-registry.sqlite3'
OUT.mkdir(parents=True, exist_ok=True)
shutil.copyfile(db_src, OUT / 'queue-registry.sqlite3')
os.chmod(OUT / 'queue-registry.sqlite3', 0o600)
db = sqlite3.connect(str(db_src))
db.row_factory = sqlite3.Row
dump = {
    'queue_binding': [dict(r) for r in db.execute('SELECT * FROM queue_binding ORDER BY run_id')],
    'queue_events': [dict(r) for r in db.execute('SELECT * FROM queue_events ORDER BY seq')],
    'daemon_task_states_at_generation': task_states,
}
db.close()
(OUT / 'rows.json').write_text(json.dumps(dump, indent=1, sort_keys=True) + '\n')
print(json.dumps(dump['queue_events'], indent=0)[:1500])
print('base', base)
