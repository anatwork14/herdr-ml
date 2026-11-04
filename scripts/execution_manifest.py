#!/usr/bin/env python3
"""Canonical execution manifest and worker-side pre-execution verification.

OFFLINE_TESTED only. This module never launches a process, never observes or
contains descendants, and never authorizes execution. verify_before_execution()
is a point-in-time check; it must be paired with a run-specific approval
(scripts/approval.py) and an immutable execution boundary. Bytes changed after
they were hashed and read are outside what this module can prove.

A Git commit hash alone is never accepted as input identity: the manifest must
also list every source file used with its SHA-256, and the checkout must be
clean at that commit.
"""
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile

SCHEMA_VERSION = 1
KIND = 'herdr-ml-execution-manifest'
RESULT_KIND = 'herdr-ml-execution-result'
OPERATIONS = ('diagnostic-execution', 'official-execution')
REQUIRED_INPUT_ROLES = ('dataset_content', 'dataset_manifest', 'split_manifest')
RESERVED_INPUT_ROLES = ('config',)
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SOURCE_FILES = 10000
MAX_INPUTS = 32
MAX_PACKAGES = 4096
MAX_ARGV = 256
MAX_NAMED_SEEDS = 32
MAX_NONDETERMINISM = 16
MAX_INT = 2 ** 53 - 1
CHUNK = 1024 * 1024
PATH_BYTE_LIMIT = 4096
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
HEX64_RE = re.compile(r'[0-9a-f]{64}\Z')
GIT_OID_RE = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')

# Reason codes. Every failure raises VerificationError with exactly one code.
MANIFEST_TOO_LARGE = 'MANIFEST_TOO_LARGE'
MANIFEST_MALFORMED = 'MANIFEST_MALFORMED'
MANIFEST_NONCANONICAL = 'MANIFEST_NONCANONICAL'
MANIFEST_SCHEMA = 'MANIFEST_SCHEMA'
MANIFEST_DIGEST_MISMATCH = 'MANIFEST_DIGEST_MISMATCH'
RUN_ID_MISMATCH = 'RUN_ID_MISMATCH'
ATTEMPT_ID_MISMATCH = 'ATTEMPT_ID_MISMATCH'
WORKER_ID_INVALID = 'WORKER_ID_INVALID'
SOURCE_UNVERIFIABLE = 'SOURCE_UNVERIFIABLE'
SOURCE_COMMIT_MISMATCH = 'SOURCE_COMMIT_MISMATCH'
SOURCE_DIRTY = 'SOURCE_DIRTY'
SOURCE_FILE_MISSING = 'SOURCE_FILE_MISSING'
SOURCE_MUTATED = 'SOURCE_MUTATED'
INPUT_UNSAFE_PATH = 'INPUT_UNSAFE_PATH'
INPUT_MISSING = 'INPUT_MISSING'
INPUT_CHANGED_DURING_READ = 'INPUT_CHANGED_DURING_READ'
INPUT_DIGEST_MISMATCH = 'INPUT_DIGEST_MISMATCH'
DATASET_DIGEST_MISMATCH = 'DATASET_DIGEST_MISMATCH'
SPLIT_MANIFEST_MISMATCH = 'SPLIT_MANIFEST_MISMATCH'
INTERPRETER_PATH_MISMATCH = 'INTERPRETER_PATH_MISMATCH'
INTERPRETER_DIGEST_MISMATCH = 'INTERPRETER_DIGEST_MISMATCH'
INTERPRETER_UNVERIFIABLE = 'INTERPRETER_UNVERIFIABLE'
RUNTIME_DRIFT = 'RUNTIME_DRIFT'
OUTPUT_DESTINATION_INVALID = 'OUTPUT_DESTINATION_INVALID'
RESULT_ARTIFACT_MISSING = 'RESULT_ARTIFACT_MISSING'
CONTAINMENT_MISMATCH = 'CONTAINMENT_MISMATCH'
SEAL_DIR_UNSAFE = 'SEAL_DIR_UNSAFE'

TOP_KEYS = ('schema_version', 'kind', 'run_id', 'attempt_id', 'project', 'operation',
            'resource_class', 'source', 'interpreter', 'runtime', 'command', 'config',
            'inputs', 'splits', 'seeds', 'nondeterminism', 'output')


class VerificationError(Exception):
    """Fail-closed rejection carrying one reason code."""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__('%s: %s' % (code, detail) if detail else code)


# ---------------------------------------------------------------- canonical JSON

def _has_surrogate(value):
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _check_plain(value, depth=0):
    if depth > 32:
        raise VerificationError(MANIFEST_SCHEMA, 'nesting too deep')
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        # Lone surrogates would serialize to the same ASCII as a real code point.
        if _has_surrogate(value):
            raise VerificationError(MANIFEST_SCHEMA, 'surrogate code point')
        return
    if type(value) is int:
        if abs(value) > MAX_INT:
            raise VerificationError(MANIFEST_SCHEMA, 'integer out of range')
        return
    if isinstance(value, list):
        for item in value:
            _check_plain(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str or _has_surrogate(key):
                raise VerificationError(MANIFEST_SCHEMA, 'invalid key')
            _check_plain(item, depth + 1)
        return
    raise VerificationError(MANIFEST_SCHEMA, 'non-canonical type ' + type(value).__name__)


def canonical_bytes(value):
    """Deterministic serialization: sorted keys, no whitespace, ASCII, no floats."""
    _check_plain(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def digest_value(value):
    return sha256_hex(canonical_bytes(value))


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(MANIFEST_MALFORMED, 'duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(_name):
    raise VerificationError(MANIFEST_MALFORMED, 'nonstandard JSON constant')


def _reject_float(_text):
    raise VerificationError(MANIFEST_MALFORMED, 'floats are not allowed')


def parse_canonical(data, limit, malformed, noncanonical, too_large, schema=None):
    """Parse bytes strictly and require that they are exactly the canonical form.

    Errors from canonicalization (for example an out-of-range integer) map to
    `schema`; JSON syntax errors map to `malformed`.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) > limit:
        raise VerificationError(too_large, 'size limit')
    data = bytes(data)
    schema = schema or malformed
    try:
        value = json.loads(data.decode('utf-8', 'strict'), object_pairs_hook=_strict_pairs,
                           parse_constant=_reject_constant, parse_float=_reject_float)
        canonical = canonical_bytes(value)
    except VerificationError as exc:
        code = schema if exc.code == MANIFEST_SCHEMA else malformed
        raise VerificationError(code, exc.detail) from exc
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise VerificationError(malformed, 'invalid JSON') from exc
    if canonical != data:
        raise VerificationError(noncanonical, 'bytes differ from canonical serialization')
    return value


# ---------------------------------------------------------------- bounded file IO

def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_regular(path, *, follow=False):
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
    if not follow:
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _consume(path, *, missing, unsafe, dest=None, follow=False, created=None):
    """Read the file exactly once, hashing the bytes and optionally copying them.

    The digest covers the same bytes that are written to dest, so a sealed copy
    cannot differ from what was hashed. dest is removed on failure only if this
    call created it (O_EXCL); a pre-existing file is never touched. If created
    is a list, dest is appended to it when this call creates dest.
    """
    try:
        fd = _open_regular(path, follow=follow)
    except FileNotFoundError as exc:
        raise VerificationError(missing, 'not found') from exc
    except OSError as exc:
        raise VerificationError(unsafe, 'cannot open safely') from exc
    out = None
    made = False
    ok = False
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError(unsafe, 'not a regular file')
        if dest is not None:
            out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | getattr(os, 'O_CLOEXEC', 0), 0o600)
            made = True
            if created is not None:
                created.append(dest)
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(fd, CHUNK)
            if not block:
                break
            size += len(block)
            if size > before.st_size:
                raise VerificationError(INPUT_CHANGED_DURING_READ, 'grew during read')
            digest.update(block)
            if out is not None:
                _write_all(out, block)
        if size != before.st_size or _identity(os.fstat(fd)) != _identity(before):
            raise VerificationError(INPUT_CHANGED_DURING_READ, 'changed during read')
        ok = True
        return digest.hexdigest(), size
    except OSError as exc:
        raise VerificationError(unsafe, 'read failed') from exc
    finally:
        os.close(fd)
        if out is not None:
            os.close(out)
        if made and not ok:
            _discard(dest)


def read_bounded(path, limit, *, missing, unsafe):
    """Read a small regular file once with a hard size limit."""
    try:
        fd = _open_regular(path)
    except FileNotFoundError as exc:
        raise VerificationError(missing, 'not found') from exc
    except OSError as exc:
        raise VerificationError(unsafe, 'cannot open safely') from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError(unsafe, 'not a regular file')
        if before.st_size > limit:
            raise VerificationError(unsafe, 'too large')
        chunks = []
        total = 0
        while True:
            block = os.read(fd, min(CHUNK, limit + 1 - total))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise VerificationError(unsafe, 'too large')
            chunks.append(block)
        if total != before.st_size or _identity(os.fstat(fd)) != _identity(before):
            raise VerificationError(INPUT_CHANGED_DURING_READ, 'changed during read')
        return b''.join(chunks)
    except OSError as exc:
        raise VerificationError(unsafe, 'read failed') from exc
    finally:
        os.close(fd)


# ---------------------------------------------------------------- path and value checks

def _is_int(value):
    return type(value) is int and -MAX_INT <= value <= MAX_INT


def _is_sha(value):
    return isinstance(value, str) and HEX64_RE.match(value) is not None


def _is_id(value):
    return isinstance(value, str) and ID_RE.match(value) is not None


def _is_text(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and '\x00' not in value
            and not _has_surrogate(value))


def _is_arg(value):
    return (isinstance(value, str) and '\x00' not in value and not _has_surrogate(value)
            and len(value.encode('utf-8')) <= 4096)


def _is_relpath(value, allow_dot=False):
    if allow_dot and value == '.':
        return True
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or _has_surrogate(value)):
        return False
    if len(value.encode('utf-8')) > PATH_BYTE_LIMIT or value.startswith('/'):
        return False
    return all(part not in ('', '.', '..') for part in value.split('/'))


def _is_abspath(value):
    if (not isinstance(value, str) or not value.startswith('/') or '\\' in value
            or '\x00' in value or _has_surrogate(value)):
        return False
    if len(value.encode('utf-8')) > PATH_BYTE_LIMIT:
        return False
    return value != '/' and all(part not in ('', '.', '..') for part in value[1:].split('/'))


def _input_file(path):
    if not _is_abspath(path) or os.path.realpath(path) != path:
        raise VerificationError(INPUT_UNSAFE_PATH, 'input path must be absolute, normalized and symlink-free')
    return path


def _source_file(root, rel):
    if not _is_relpath(rel):
        raise VerificationError(SOURCE_UNVERIFIABLE, 'unsafe source path')
    full = os.path.join(root, rel)
    if os.path.realpath(full) != full:
        raise VerificationError(SOURCE_UNVERIFIABLE, 'symlink in source path')
    return full


# ---------------------------------------------------------------- manifest schema

def _schema(condition, detail):
    if not condition:
        raise VerificationError(MANIFEST_SCHEMA, detail)


def _keys(obj, keys, detail):
    _schema(isinstance(obj, dict) and set(obj) == set(keys), detail)


def _file_entry(entry, path_ok, detail):
    _keys(entry, ('path', 'sha256', 'size'), detail)
    _schema(path_ok(entry['path']), detail + ' path')
    _schema(_is_sha(entry['sha256']), detail + ' sha256')
    _schema(_is_int(entry['size']) and entry['size'] >= 0, detail + ' size')


def validate_manifest(m):
    _keys(m, TOP_KEYS, 'top-level fields')
    _schema(type(m['schema_version']) is int and m['schema_version'] == SCHEMA_VERSION
            and m['kind'] == KIND, 'schema version or kind')
    for key in ('run_id', 'attempt_id', 'project', 'resource_class'):
        _schema(_is_id(m[key]), key)
    _schema(m['operation'] in OPERATIONS, 'operation')

    source = m['source']
    _keys(source, ('commit', 'worktree_clean', 'files', 'files_sha256'), 'source fields')
    _schema(isinstance(source['commit'], str) and GIT_OID_RE.match(source['commit']) is not None,
            'source commit must be a full object id')
    _schema(source['worktree_clean'] is True, 'worktree_clean must be true')
    files = source['files']
    _schema(isinstance(files, list) and 1 <= len(files) <= MAX_SOURCE_FILES, 'source files')
    for entry in files:
        _file_entry(entry, _is_relpath, 'source file')
    paths = [entry['path'] for entry in files]
    _schema(paths == sorted(set(paths)), 'source files must be unique and sorted')
    _schema(source['files_sha256'] == digest_value(files), 'source files digest')

    interp = m['interpreter']
    _keys(interp, ('path', 'sha256', 'python_version', 'implementation'), 'interpreter fields')
    _schema(_is_abspath(interp['path']) and _is_sha(interp['sha256']), 'interpreter path or digest')
    _schema(_is_text(interp['python_version']) and _is_text(interp['implementation']), 'interpreter identity')

    runtime = m['runtime']
    _keys(runtime, ('implementation', 'python_version', 'packages', 'packages_sha256'), 'runtime fields')
    _schema(_is_text(runtime['implementation']) and _is_text(runtime['python_version']), 'runtime identity')
    packages = runtime['packages']
    _schema(isinstance(packages, list) and len(packages) <= MAX_PACKAGES
            and all(isinstance(p, list) and len(p) == 2 and all(_is_text(x) for x in p) for p in packages)
            and packages == sorted(packages), 'packages must be sorted [name, version] pairs')
    _schema(runtime['packages_sha256'] == digest_value(packages), 'packages digest')

    command = m['command']
    _keys(command, ('argv', 'cwd'), 'command fields')
    argv = command['argv']
    _schema(isinstance(argv, list) and 1 <= len(argv) <= MAX_ARGV and all(_is_arg(a) for a in argv), 'argv')
    _schema(argv[0] == interp['path'], 'argv[0] must be the recorded interpreter path')
    _schema(_is_relpath(command['cwd'], allow_dot=True), 'cwd')

    _file_entry(m['config'], _is_abspath, 'config')

    inputs = m['inputs']
    _schema(isinstance(inputs, list) and 1 <= len(inputs) <= MAX_INPUTS, 'inputs')
    for entry in inputs:
        _keys(entry, ('role', 'path', 'sha256', 'size'), 'input fields')
        _schema(_is_id(entry['role']) and entry['role'] not in RESERVED_INPUT_ROLES, 'input role')
        _file_entry({'path': entry['path'], 'sha256': entry['sha256'], 'size': entry['size']},
                    _is_abspath, 'input ' + entry['role'])
    roles = [entry['role'] for entry in inputs]
    _schema(roles == sorted(set(roles)), 'input roles must be unique and sorted')
    by_role = {entry['role']: entry for entry in inputs}
    for required in REQUIRED_INPUT_ROLES:
        _schema(required in by_role, 'missing input role ' + required)

    splits = m['splits']
    _keys(splits, ('split_manifest_sha256', 'client_manifest_sha256'), 'splits fields')
    _schema(_is_sha(splits['split_manifest_sha256']), 'split manifest digest')
    _schema(by_role['split_manifest']['sha256'] == splits['split_manifest_sha256'],
            'split manifest digest disagrees with inputs')
    client = splits['client_manifest_sha256']
    if client is None:
        _schema('client_manifest' not in by_role, 'client manifest input without digest')
    else:
        _schema(_is_sha(client) and 'client_manifest' in by_role
                and by_role['client_manifest']['sha256'] == client,
                'client manifest digest disagrees with inputs')

    seeds = m['seeds']
    _keys(seeds, ('primary', 'named'), 'seed fields')
    _schema(_is_int(seeds['primary']), 'primary seed')
    named = seeds['named']
    _schema(isinstance(named, dict) and len(named) <= MAX_NAMED_SEEDS
            and all(_is_id(k) and _is_int(v) for k, v in named.items()), 'named seeds')

    nondet = m['nondeterminism']
    _schema(isinstance(nondet, list) and len(nondet) <= MAX_NONDETERMINISM
            and all(_is_id(x) for x in nondet) and nondet == sorted(set(nondet)),
            'nondeterminism declarations')

    output = m['output']
    _keys(output, ('root', 'path'), 'output fields')
    _schema(_is_id(output['root']) and _is_relpath(output['path']), 'output destination')
    return m


def encode_manifest(manifest):
    validate_manifest(manifest)
    return canonical_bytes(manifest)


def parse_manifest(data):
    """Strictly parse, require canonical bytes and validate the schema."""
    manifest = parse_canonical(data, MAX_MANIFEST_BYTES, MANIFEST_MALFORMED,
                               MANIFEST_NONCANONICAL, MANIFEST_TOO_LARGE, MANIFEST_SCHEMA)
    return validate_manifest(manifest)


def manifest_digest(data):
    return sha256_hex(bytes(data))


# ---------------------------------------------------------------- runtime and interpreter

def _normalize_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def collect_runtime():
    """Dependency identity of the interpreter that is evaluating this code."""
    packages = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get('Name')
        if name and dist.version:
            packages.append([_normalize_name(str(name)), str(dist.version)])
    packages.sort()
    return {'implementation': platform.python_implementation(),
            'python_version': platform.python_version(),
            'packages': packages, 'packages_sha256': digest_value(packages)}


def running_image_digest():
    """SHA-256 of the image the current process is executing (procfs only)."""
    return _consume('/proc/self/exe', missing=INTERPRETER_UNVERIFIABLE,
                    unsafe=INTERPRETER_UNVERIFIABLE, follow=True)


def verify_interpreter(interp):
    if os.path.realpath(sys.executable) != interp['path']:
        raise VerificationError(INTERPRETER_PATH_MISMATCH, 'running interpreter path differs')
    digest, _size = running_image_digest()
    if digest != interp['sha256']:
        raise VerificationError(INTERPRETER_DIGEST_MISMATCH, 'running image digest differs')


def check_runtime(expected, actual):
    for key in ('implementation', 'python_version', 'packages', 'packages_sha256'):
        if expected[key] != actual[key]:
            raise VerificationError(RUNTIME_DRIFT, key)


# ---------------------------------------------------------------- source tree

# Isolation for every git call: no system, global or user configuration, an empty
# HOME, attribute and hook files disabled, and fsmonitor off. Repository-local
# filter.* and diff.* configuration is refused before any status query runs.
GIT_ISOLATION = ('-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=' + os.devnull,
                 '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.untrackedCache=false')


def _git_env(home):
    return {'PATH': os.environ.get('PATH', os.defpath), 'HOME': home, 'XDG_CONFIG_HOME': home,
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', 'LANG': 'C'}


def _git(root, *args):
    with tempfile.TemporaryDirectory(prefix='herdr-ml-git-home-') as home:
        try:
            result = subprocess.run(['git', *GIT_ISOLATION, '-C', root, *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=60, check=False,
                                    env=_git_env(home))
        except (OSError, subprocess.SubprocessError) as exc:
            raise VerificationError(SOURCE_UNVERIFIABLE, 'git unavailable') from exc
    if result.returncode != 0:
        raise VerificationError(SOURCE_UNVERIFIABLE, 'git query failed')
    return result.stdout


def _reject_repo_filters(root):
    for raw in _git(root, 'config', '--list', '--name-only', '-z').split(b'\x00'):
        name = raw.decode('utf-8', 'replace').lower()
        if name.startswith('filter.') or name.startswith('diff.'):
            raise VerificationError(SOURCE_UNVERIFIABLE,
                                    'repository-local filter or diff configuration is refused')


def _reject_submodules(root):
    """Refuse any submodule. Git would run submodule filters during status."""
    for entry in _git(root, 'ls-files', '-s', '-z').split(b'\x00'):
        if not entry:
            continue
        meta, _, path = entry.partition(b'\t')
        if meta.split(b' ')[0] == b'160000':
            raise VerificationError(SOURCE_UNVERIFIABLE, 'submodules (gitlinks) are refused')
        if path == b'.gitmodules':
            raise VerificationError(SOURCE_UNVERIFIABLE, '.gitmodules is refused')
    if os.path.lexists(os.path.join(root, '.gitmodules')):
        raise VerificationError(SOURCE_UNVERIFIABLE, '.gitmodules is refused')
    gitdir = _git(root, 'rev-parse', '--absolute-git-dir').decode('utf-8', 'strict').strip()
    modules = os.path.join(gitdir, 'modules')
    if os.path.lexists(modules) and (not os.path.isdir(modules) or os.listdir(modules)):
        raise VerificationError(SOURCE_UNVERIFIABLE, 'repository has submodule metadata')


def _git_state(root):
    """Return (HEAD object id, tree is clean including untracked and ignored files)."""
    _reject_repo_filters(root)
    _reject_submodules(root)
    try:
        top = _git(root, 'rev-parse', '--show-toplevel').decode('utf-8', 'strict').strip()
        head = _git(root, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}')
        head = head.decode('ascii', 'strict').strip()
    except UnicodeDecodeError as exc:
        raise VerificationError(SOURCE_UNVERIFIABLE, 'git output') from exc
    if os.path.realpath(top) != root:
        raise VerificationError(SOURCE_UNVERIFIABLE, 'source root must be the repository top level')
    if not GIT_OID_RE.match(head):
        raise VerificationError(SOURCE_UNVERIFIABLE, 'HEAD is not a commit')
    status = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching',
                   '--ignore-submodules=all')
    return head, status == b''


def build_source_files(root, relpaths):
    files = []
    for rel in sorted(set(relpaths)):
        sha, size = _consume(_source_file(root, rel), missing=SOURCE_FILE_MISSING,
                             unsafe=SOURCE_UNVERIFIABLE)
        files.append({'path': rel, 'sha256': sha, 'size': size})
    return files


def verify_source(source, source_root):
    root = os.path.realpath(source_root)
    head, clean = _git_state(root)
    if head != source['commit']:
        raise VerificationError(SOURCE_COMMIT_MISMATCH, 'HEAD differs from manifest commit')
    for entry in source['files']:
        sha, size = _consume(_source_file(root, entry['path']), missing=SOURCE_FILE_MISSING,
                             unsafe=SOURCE_UNVERIFIABLE)
        if sha != entry['sha256'] or size != entry['size']:
            raise VerificationError(SOURCE_MUTATED, entry['path'])
    if not clean:
        raise VerificationError(SOURCE_DIRTY, 'checkout has changes, untracked or ignored files')


# ---------------------------------------------------------------- inputs (hash-then-consume)

_INPUT_CODES = {'dataset_content': DATASET_DIGEST_MISMATCH,
                'split_manifest': SPLIT_MANIFEST_MISMATCH,
                'client_manifest': SPLIT_MANIFEST_MISMATCH}


def seal_inputs(manifest, seal_dir):
    """Copy each input to a private sealed file while hashing the same bytes.

    Execution must use the sealed copies, never the original paths. On failure this
    call removes only the sealed files it created itself.
    """
    try:
        info = os.lstat(seal_dir)
    except OSError as exc:
        raise VerificationError(SEAL_DIR_UNSAFE, 'seal directory missing') from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise VerificationError(SEAL_DIR_UNSAFE, 'seal directory must be a private directory')
    items = [('config', manifest['config'])]
    items.extend((entry['role'], entry) for entry in manifest['inputs'])
    used = []
    sealed = {}
    created_here = []
    try:
        for role, entry in items:
            dest = os.path.join(seal_dir, role + '.sealed')
            created = []
            sha, size = _consume(_input_file(entry['path']), missing=INPUT_MISSING,
                                 unsafe=INPUT_UNSAFE_PATH, dest=dest, created=created)
            created_here.extend(created)
            if (sha, size) != (entry['sha256'], entry['size']):
                raise VerificationError(_INPUT_CODES.get(role, INPUT_DIGEST_MISMATCH), role)
            sealed[role] = dest
            used.append({'role': role, 'path': entry['path'], 'sha256': sha, 'size': size})
    except BaseException:
        for path in created_here:
            _discard(path)
        raise
    return used, sealed


def _discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- manifest creation

def build_manifest(*, run_id, attempt_id, project, operation, resource_class, source_root,
                   source_files, args, cwd, config, inputs, primary_seed, named_seeds=None,
                   nondeterminism=(), output_root, output_path):
    """Create and validate a manifest from the running interpreter and a clean checkout.

    inputs maps role -> absolute path. The interpreter is the running one, and
    argv[0] is recorded as its resolved path.
    """
    root = os.path.realpath(source_root)
    head, clean = _git_state(root)
    if not clean:
        raise VerificationError(SOURCE_DIRTY, 'checkout must be clean before a manifest is created')
    files = build_source_files(root, source_files)
    interpreter_path = os.path.realpath(sys.executable)
    image_sha, _size = running_image_digest()
    runtime = collect_runtime()
    input_records = []
    for role in sorted(inputs):
        digest, size = _consume(_input_file(inputs[role]), missing=INPUT_MISSING,
                                unsafe=INPUT_UNSAFE_PATH)
        input_records.append({'role': role, 'path': inputs[role], 'sha256': digest, 'size': size})
    config_sha, config_size = _consume(_input_file(config), missing=INPUT_MISSING,
                                       unsafe=INPUT_UNSAFE_PATH)
    by_role = {entry['role']: entry for entry in input_records}
    client = by_role.get('client_manifest')
    manifest = {
        'schema_version': SCHEMA_VERSION, 'kind': KIND, 'run_id': run_id,
        'attempt_id': attempt_id, 'project': project, 'operation': operation,
        'resource_class': resource_class,
        'source': {'commit': head, 'worktree_clean': True, 'files': files,
                   'files_sha256': digest_value(files)},
        'interpreter': {'path': interpreter_path, 'sha256': image_sha,
                        'python_version': runtime['python_version'],
                        'implementation': runtime['implementation']},
        'runtime': runtime,
        'command': {'argv': [interpreter_path] + list(args), 'cwd': cwd},
        'config': {'path': config, 'sha256': config_sha, 'size': config_size},
        'inputs': input_records,
        'splits': {'split_manifest_sha256': by_role['split_manifest']['sha256'],
                   'client_manifest_sha256': client['sha256'] if client else None},
        'seeds': {'primary': primary_seed, 'named': dict(named_seeds or {})},
        'nondeterminism': sorted(set(nondeterminism)),
        'output': {'root': output_root, 'path': output_path},
    }
    return validate_manifest(manifest)


# ---------------------------------------------------------------- output and results

def resolve_output(output, run_dir):
    root = os.path.realpath(run_dir)
    target = os.path.join(root, output['root'], output['path'])
    parent = os.path.dirname(target)
    if not os.path.realpath(parent).startswith(root + os.sep) or os.path.lexists(target):
        raise VerificationError(OUTPUT_DESTINATION_INVALID, 'destination escapes run directory or exists')
    return target


def _consume_result(path):
    return _consume(path, missing=RESULT_ARTIFACT_MISSING, unsafe=RESULT_ARTIFACT_MISSING)


def verify_before_execution(manifest_bytes, *, expected_sha256, run_id, attempt_id, worker_id,
                            source_root, run_dir, seal_dir):
    """Worker-side checks immediately before execution. Returns a plan dict or raises."""
    if not _is_id(worker_id):
        raise VerificationError(WORKER_ID_INVALID)
    if not _is_sha(expected_sha256) or sha256_hex(bytes(manifest_bytes)) != expected_sha256:
        raise VerificationError(MANIFEST_DIGEST_MISMATCH, 'manifest bytes differ from approved digest')
    manifest = parse_manifest(manifest_bytes)
    if manifest['run_id'] != run_id:
        raise VerificationError(RUN_ID_MISMATCH)
    if manifest['attempt_id'] != attempt_id:
        raise VerificationError(ATTEMPT_ID_MISMATCH)
    verify_interpreter(manifest['interpreter'])
    check_runtime(manifest['runtime'], collect_runtime())
    verify_source(manifest['source'], source_root)
    used, sealed = seal_inputs(manifest, seal_dir)
    output_path = resolve_output(manifest['output'], run_dir)
    image_sha, _size = running_image_digest()
    return {'manifest_sha256': expected_sha256, 'run_id': run_id, 'attempt_id': attempt_id,
            'worker_id': worker_id, 'source_commit': manifest['source']['commit'],
            'effective_runtime': {'interpreter_sha256': image_sha,
                                  'implementation': manifest['runtime']['implementation'],
                                  'python_version': manifest['runtime']['python_version'],
                                  'packages_sha256': manifest['runtime']['packages_sha256']},
            'inputs_used': used, 'sealed_inputs': sealed, 'output_path': output_path}


def build_result_record(plan, *, result_path, exit_status, signal_number=None, containment=None):
    """Canonical result record. Containment is UNKNOWN unless a verified result is supplied."""
    artifact_sha, artifact_size = _consume_result(result_path)
    if containment is None or containment == 'UNKNOWN':
        contained = {'status': 'UNKNOWN', 'verifier_id': None, 'evidence_sha256': None}
    else:
        _keys(containment, ('status', 'verifier_id', 'evidence_sha256', 'run_id', 'attempt_id'),
              'containment evidence')
        if containment['status'] != 'VERIFIED':
            raise VerificationError(CONTAINMENT_MISMATCH, 'only VERIFIED evidence is accepted')
        if (containment['run_id'], containment['attempt_id']) != (plan['run_id'], plan['attempt_id']):
            raise VerificationError(CONTAINMENT_MISMATCH, 'evidence belongs to another attempt')
        if not _is_id(containment['verifier_id']) or not _is_sha(containment['evidence_sha256']):
            raise VerificationError(CONTAINMENT_MISMATCH, 'malformed evidence reference')
        contained = {'status': 'VERIFIED', 'verifier_id': containment['verifier_id'],
                     'evidence_sha256': containment['evidence_sha256']}
    if contained['status'] != 'VERIFIED':
        status = 'UNKNOWN'
    else:
        status = 'COMPLETED' if exit_status == 0 else 'FAILED'
    record = {'schema_version': SCHEMA_VERSION, 'kind': RESULT_KIND,
              'manifest_sha256': plan['manifest_sha256'], 'worker_id': plan['worker_id'],
              'run_id': plan['run_id'], 'attempt_id': plan['attempt_id'],
              'effective_runtime': plan['effective_runtime'], 'inputs_used': plan['inputs_used'],
              'result_artifact': {'sha256': artifact_sha, 'size': artifact_size},
              'termination': {'exit_status': exit_status, 'signal': signal_number},
              'containment': contained, 'status': status}
    data = canonical_bytes(record)
    return record, data, sha256_hex(data)


def main(argv=None):
    """Print the canonical digest of a manifest file (no verification beyond parsing)."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('manifest')
    args = parser.parse_args(argv)
    try:
        data = read_bounded(args.manifest, MAX_MANIFEST_BYTES, missing=MANIFEST_MALFORMED,
                            unsafe=MANIFEST_MALFORMED)
        parse_manifest(data)
    except VerificationError as exc:
        print('BLOCKED: %s' % exc, file=sys.stderr)
        return 2
    print(manifest_digest(data))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
