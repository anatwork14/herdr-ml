"""Crash-aware Herdr layout reconciliation core.

This module deliberately has no live Herdr adapter or command-line entry point.
The bootstrap safety hold remains in place until the actual server identity and
CLI data contracts are established and integration-tested in an authorized pane.

Adapter contract (a future adapter MUST obtain these values from the same server):
  identity() -> nonempty stable server/session incarnation identifier
  creation_cwd_verified() -> True only when adapter can establish immutable,
      server-provided creation-bound cwd for all workspaces and tabs; live pane
      cwd, caller assertions and unverifiable inferred paths are insufficient
  workspaces() -> iterable of {id, label, cwd}
  tabs(workspace_id) -> iterable of {id, label, cwd}
  create_workspace(label, cwd) -> None
  create_tab(workspace_id, label, cwd) -> None

A create may have taken effect even when it throws. The durable intent carries a
unique marker, allowing only a single verifiable match to be adopted. If an
uncertain create has no observable match, automatic retry is forbidden.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import uuid


class ReconcileBlocked(Exception):
    """The next mutation cannot be made safely without operator investigation."""


def marker(prefix, token, name):
    return f"{name} [herdr-ml:{prefix}:{token}]"


def _canonical(path):
    return str(Path(path).expanduser().resolve(strict=True))


def _write(path, state):
    """Persist record and directory entry before issuing an external mutation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.state-',
                                         encoding='utf-8', delete=False) as temp:
            temp_path = temp.name
            os.fchmod(temp.fileno(), 0o600)
            json.dump(state, temp, sort_keys=True, indent=2)
            temp.write('\n')
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
        temp_path = None
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp_path is not None:
            os.unlink(temp_path)


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _one(objects, label, cwd):
    matches = [obj for obj in objects if obj.get('label') == label]
    if len(matches) > 1:
        raise ReconcileBlocked('Multiple objects have the same ownership marker')
    if not matches:
        return None
    obj = matches[0]
    if not isinstance(obj.get('id'), str) or not obj['id']:
        raise ReconcileBlocked('Object has no stable ID')
    try:
        actual = _canonical(obj['cwd'])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ReconcileBlocked('Object has no verifiable checkout') from exc
    if actual != cwd:
        raise ReconcileBlocked('Object marker exists at a different checkout')
    return obj['id']


def _check_identity(adapter, record):
    if adapter.identity() != record["server"]:
        raise ReconcileBlocked("Server/session identity changed during reconciliation")


def _reject_untracked_workspace(objects, label, checkout):
    # State may be lost while the corresponding live workspace survives. Never
    # create a new marker until the authoritative inventory proves there is no
    # workspace, including a legacy unmarked one, for this checkout, or a
    # managed workspace for this label. Conservative blocking also
    # covers an unavailable cwd, since ownership cannot then be disproved.
    pattern = re.compile(r'^(.*) \[herdr-ml:workspace:[0-9a-f]{32}\]$')
    for obj in objects:
        object_label = obj.get('label')
        match = pattern.fullmatch(object_label) if isinstance(object_label, str) else None
        try:
            existing_checkout = _canonical(obj['cwd'])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ReconcileBlocked(
                f"Workspace id={obj.get('id')!r} label={object_label!r} "
                'has unverifiable checkout; operator reconciliation required'
            ) from exc
        if existing_checkout == checkout or (match and match.group(1) == label):
            raise ReconcileBlocked('Workspace exists but local ownership state is missing')


def _complete_intent(adapter, record, state_path):
    """Resolve a persisted intent without ever blindly repeating a mutation."""
    _check_identity(adapter, record)
    pending = record['pending']
    if pending['kind'] == 'workspace':
        objects = list(adapter.workspaces())
    elif pending['kind'] == 'tab':
        objects = list(adapter.tabs(record['workspace_id']))
    else:
        raise ReconcileBlocked('Unknown pending operation')
    _check_identity(adapter, record)
    found = _one(objects, pending['label'], record['checkout'])
    if found is None:
        raise ReconcileBlocked('Ambiguous operation: no matching object; do not retry automatically')
    if pending['kind'] == 'workspace':
        record['workspace_id'] = found
    else:
        record['tabs'][pending['key']] = found
    record['pending'] = None
    _write(state_path, record)


def _intent_then_create(adapter, record, path, kind, label, key=None):
    _check_identity(adapter, record)
    record['pending'] = {'kind': kind, 'label': label, 'key': key}
    _write(path, record)
    # Exceptions, timeouts, process death, and malformed responses all leave the
    # durable intent intact. A later invocation must discover the outcome.
    _check_identity(adapter, record)
    if kind == 'workspace':
        adapter.create_workspace(label, record['checkout'])
    else:
        adapter.create_tab(record['workspace_id'], label, record['checkout'])
    _complete_intent(adapter, record, path)


def reconcile(adapter, state_path, project_id, label, checkout, tabs):
    """Reconcile one project. Caller supplies only authenticated adapter data.

    A failed identity, changed checkout, duplicate marker, or unknown mutation
    outcome is BLOCKED; this routine never deletes, renames, or focuses anything.
    """
    path = Path(state_path)
    if not isinstance(project_id, str) or not project_id or not isinstance(label, str) or not label:
        raise ValueError('Project identity and label are required')
    if not isinstance(tabs, (list, tuple)) or not tabs or any(not isinstance(t, str) or not t for t in tabs) or len(set(tabs)) != len(tabs):
        raise ValueError('Tabs must be a nonempty list of unique names')
    checkout = _canonical(checkout)
    if not (Path(checkout) / '.git').exists():
        raise ReconcileBlocked('Checkout is not a Git worktree')
    with _locked(path):
        try:
            cwd_verified = adapter.creation_cwd_verified()
        except (AttributeError, NotImplementedError) as exc:
            raise ReconcileBlocked('Creation-bound cwd capability unavailable') from exc
        if cwd_verified is not True:
            raise ReconcileBlocked('Creation-bound cwd capability unverified')
        identity = adapter.identity()
        if not isinstance(identity, str) or not identity:
            raise ReconcileBlocked('Server/session incarnation identity is unavailable')
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise ReconcileBlocked('State cannot be read') from exc
            if not isinstance(record, dict) or record.get('version') != 1:
                raise ReconcileBlocked('Unknown state format')
            if (record.get('server') != identity or record.get('project') != project_id or
                    record.get('checkout') != checkout or record.get('label') != label):
                raise ReconcileBlocked('Stored project, checkout, label, or server identity changed')
        else:
            existing_workspaces = list(adapter.workspaces())
            if adapter.identity() != identity:
                raise ReconcileBlocked('Server/session identity changed during discovery')
            _reject_untracked_workspace(existing_workspaces, label, checkout)
            token = uuid.uuid4().hex
            record = {'version': 1, 'server': identity, 'project': project_id,
                      'label': label, 'checkout': checkout, 'token': token,
                      'workspace_id': None, 'tabs': {}, 'pending': None}
            _write(path, record)
        if record.get('pending'):
            _complete_intent(adapter, record, path)
        _check_identity(adapter, record)
        workspace_label = marker('workspace', record['token'], label)
        current_workspaces = list(adapter.workspaces())
        _check_identity(adapter, record)
        observed = _one(current_workspaces, workspace_label, checkout)
        if record['workspace_id'] is None:
            _reject_untracked_workspace(current_workspaces, label, checkout)
            if observed:
                raise ReconcileBlocked('Unrecorded workspace without matching intent')
            _intent_then_create(adapter, record, path, 'workspace', workspace_label)
        elif observed != record['workspace_id']:
            raise ReconcileBlocked('Workspace missing, moved, or ID reused')
        for tab_name in tabs:
            _check_identity(adapter, record)
            # Marker uses a digest rather than raw arbitrary input for stable uniqueness.
            tab_token = hashlib.sha256(tab_name.encode('utf-8')).hexdigest()[:16]
            tab_label = marker('tab', record['token'] + '-' + tab_token, tab_name)
            current_tabs = list(adapter.tabs(record['workspace_id']))
            _check_identity(adapter, record)
            observed = _one(current_tabs, tab_label, checkout)
            old = record['tabs'].get(tab_name)
            if old is not None and any(obj.get('id') == old and obj.get('label') != tab_label for obj in current_tabs):
                raise ReconcileBlocked('Tab ID reused for an unrelated object')
            if observed:
                if old is None or old != observed:
                    raise ReconcileBlocked('Unrecorded or replaced tab marker')
            else:
                _intent_then_create(adapter, record, path, 'tab', tab_label, tab_name)
        _check_identity(adapter, record)
        return {'workspace_id': record['workspace_id'], 'tabs': dict(record['tabs'])}
