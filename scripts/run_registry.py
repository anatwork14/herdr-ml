"""Transactional per-run ownership for a single trusted Linux host.

SQLite is authoritative; JSON files written by the runner are projections.
An active claim is never reclaimed here, even when its owner appears dead:
without verified containment, descendants may still be running.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import uuid


class RegistryError(Exception):
    pass


def _connect(folder):
    db = sqlite3.connect(str(Path(folder) / 'registry.sqlite3'), timeout=30, isolation_level=None)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('''CREATE TABLE IF NOT EXISTS run (
        id INTEGER PRIMARY KEY CHECK (id = 1), run_id TEXT NOT NULL,
        project TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
        state TEXT NOT NULL, owner TEXT NOT NULL, owner_pid INTEGER NOT NULL,
        owner_host TEXT NOT NULL, owner_boot TEXT, owner_start TEXT,
        queue_task_id TEXT, result_json TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        owner TEXT, state TEXT NOT NULL, detail TEXT)''')
    return db


def _identity():
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stat = Path('/proc/self/stat').read_text()
        start = stat[stat.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        boot, start = None, None
    return boot, start


def claim(folder, run_id, project, digest, initial_state):
    """Atomically create a run and claim it exactly once, never stealing owners."""
    if initial_state not in ('SUBMITTED', 'QUEUED'):
        raise RegistryError('Run is not eligible for execution')
    db = _connect(folder)
    token = uuid.uuid4().hex
    try:
        db.execute('BEGIN IMMEDIATE')
        if (Path(folder) / 'execution.claim').exists():
            raise RegistryError('Legacy execution claim requires manual reconciliation')
        if db.execute('SELECT 1 FROM run WHERE id=1').fetchone():
            raise RegistryError('Run already claimed; do not retry without reconciliation')
        boot, start = _identity()
        db.execute('INSERT INTO run VALUES (1,?,?,?,?,?,?,?,?,?,?,?)',
                   (run_id, project, digest, 'CLAIMED', token, os.getpid(),
                    socket.gethostname(), boot, start, None, None))
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)',
                   (token, 'CLAIMED', 'Atomic one-time owner acquisition'))
        db.execute('COMMIT')
        # Persist the new registry directory entry before any dispatch.
        directory = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return token
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def advance(folder, token, expected, target):
    if (expected, target) not in {('CLAIMED', 'DISPATCHING'), ('DISPATCHING', 'RUNNING')}:
        raise RegistryError('Illegal execution transition')
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute('UPDATE run SET state=? WHERE id=1 AND owner=? AND state=?',
                             (target, token, expected)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal execution state')
        db.execute('INSERT INTO events(owner,state) VALUES (?,?)', (token, target))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def finish(folder, token, result):
    state = result.get('state')
    if state not in ('COMPLETED', 'FAILED', 'INTERRUPTED'):
        raise RegistryError('Invalid terminal state')
    data = json.dumps(result, sort_keys=True)
    db = _connect(folder)
    try:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute("UPDATE run SET state=?, result_json=? WHERE id=1 AND owner=? AND state IN ('RUNNING','DISPATCHING')",
                             (state, data, token)).rowcount
        if updated != 1:
            raise RegistryError('Stale owner or illegal terminal state')
        db.execute('INSERT INTO events(owner,state,detail) VALUES (?,?,?)', (token, state, data))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def inspect(folder):
    path = Path(folder) / 'registry.sqlite3'
    if not path.is_file():
        return None
    db = _connect(folder)
    try:
        row = db.execute('SELECT run_id,project,manifest_sha256,state,owner_pid,owner_host,owner_boot,owner_start,queue_task_id,result_json FROM run WHERE id=1').fetchone()
        if row is None:
            return None
        fields = ('run_id','project','manifest_sha256','state','owner_pid','owner_host','owner_boot','owner_start','queue_task_id','result')
        result = dict(zip(fields, row))
        if result['result'] is not None:
            result['result'] = json.loads(result['result'])
        result['events'] = [dict(zip(('seq','at','state','detail'), item)) for item in db.execute('SELECT seq,at,state,detail FROM events ORDER BY seq')]
        return result
    finally:
        db.close()
