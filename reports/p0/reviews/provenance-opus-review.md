# Provenance review of commit `1376f400e5df` (`scripts/provenance.py`, its tests, and docs)

**Bottom line:** I found no CRITICAL or HIGH defects. Within the narrow claim it makes, the verifier holds up. It verifies the exact bytes it parses, it parses only after the signature check, it rejects duplicate keys, it requires an exact set of artifact roles, and it hashes artifacts through `O_NOFOLLOW` directory traversal from a file descriptor.

I found three MEDIUM defects (two in tests, one in code) and several LOW items. I had no tools, so these reproductions are reasoned from the source, not executed.

## MEDIUM

### M1. Three adversarial tests would still pass if their protection were removed

**Root cause:** each negative test fails for a reason other than the one it claims to test.

| Test | What it claims | Why it passes anyway |
|---|---|---|
| `test_symlink_escape_and_missing_required_role_rejected` | Symlinks are rejected | The symlink target `self.root / 'outside'` does not exist. If `O_NOFOLLOW` were dropped, `open` would follow the link, get `ENOENT`, and raise `ProvenanceError`. |
| `test_signed_path_traversal_and_duplicate_keys_rejected` (traversal part) | `..` is rejected | `../outside` also does not exist. If the normalization check were removed, `os.open('..', O_DIRECTORY\|O_NOFOLLOW, dir_fd=…)` succeeds, then `outside` gives `ENOENT`, which still raises. |
| Same test (duplicate-key part) | Duplicate keys are rejected | `{"schema_version":1,"schema_version":1}` has no `project` key. Without the `_object` hook, it fails on `missing project` instead. |

None of the three tests checks which error was raised, so the duplicate-key, `..`, and symlink controls are effectively untested against regression. Intermediate-directory symlinks are not tested at all.

**Reproduction:** delete `os.O_NOFOLLOW` from the final `os.open`, delete the `'..'` check, or drop `object_pairs_hook`. The suite stays green each time.

**Acceptance tests.** Each attack must be constructed so it would succeed if the control were missing:

```python
def test_symlink_to_identical_bytes_rejected(self):
    role = 'test_split'
    target = self.bundle / self.artifacts[role]['path']
    outside = self.root / 'outside'
    outside.write_bytes(target.read_bytes())   # hash would match if followed
    target.unlink(); target.symlink_to(outside)
    with self.assertRaisesRegex(p.ProvenanceError, 'cannot safely read'): self.verify()

def test_symlinked_intermediate_directory_rejected(self):
    real = self.root / 'real'; real.mkdir()
    (real / 'x.dat').write_bytes(b'fixture test_split')
    (self.bundle / 'sub').symlink_to(real, target_is_directory=True)
    self.manifest['artifacts']['test_split'] = {'path': 'sub/x.dat',
        'sha256': hashlib.sha256(b'fixture test_split').hexdigest()}
    self.write_signed()
    with self.assertRaisesRegex(p.ProvenanceError, 'cannot safely read'): self.verify()

def test_dotdot_to_identical_bytes_rejected(self):
    (self.root / 'outside').write_bytes(b'fixture test_split')
    self.manifest['artifacts']['test_split']['path'] = '../outside'
    self.write_signed()
    with self.assertRaisesRegex(p.ProvenanceError, 'relative and normalized'): self.verify()

def test_duplicate_key_last_wins_would_pass_rejected(self):
    body = json.dumps(self.manifest, sort_keys=True, separators=(',', ':'))
    self.path.write_text(body[:-1] + ',"project":"evil"}\n')  # otherwise-valid manifest
    Path(str(self.path) + '.sig').unlink()
    subprocess.run(['ssh-keygen','-Y','sign','-q','-n',p.NAMESPACE,'-f',str(self.key),str(self.path)], check=True)
    with self.assertRaisesRegex(p.ProvenanceError, 'duplicate JSON key'): self.verify()
```

### M2. No test pins the signature namespace

**Root cause:** every test signs with the correct namespace. If `NAMESPACE` changed, or the `-n` argument were built wrongly, nothing would catch it. That breaks the cross-protocol separation the docs rely on.

**Acceptance test:** sign the manifest with `-n other-namespace` and assert `ProvenanceError`. Also add a test where the `.sig` file comes from a different but valid manifest, and assert rejection.

### M3. The manifest is opened with weaker rules than the artifacts, so a FIFO hangs the verifier

**Root cause:** `manifest_path.open('rb')` follows symlinks, does not check `S_ISREG`, and has no timeout. Only the `ssh-keygen` call has the 15-second timeout. If an untrusted bundle makes `manifest.json` a FIFO, `open()` blocks forever waiting for a writer. If it is a symlink to a device, the 1 MiB cap limits the read, but the open itself can still have side effects.

Integrity is not affected, because the bytes are signature-checked. This is an availability problem and an inconsistency: the docs say symlinks are rejected, but the manifest itself may be a symlink.

**Reproduction:** `os.mkfifo(bundle/'manifest.json')`, then `verify_bundle(...)` never returns.

**Fix:** open the manifest with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, check `fstat` for a regular file, then read. Ideally open the bundle root once as a dirfd and use it for both the manifest and the artifacts.

**Acceptance test:** create a FIFO manifest and a symlinked manifest. Run `verify_bundle` in a subprocess with a timeout and assert that it returns `ProvenanceError` quickly.

## LOW

- **L1. Some errors after verification escape as non-`ProvenanceError` exceptions.**
  - A signed manifest with a seed over 4300 digits raises a plain `ValueError` from Python ≥3.11's int-digit limit. That is not a `JSONDecodeError`, so the `except` clause misses it.
  - About 1 MB of nested `[` raises `RecursionError`.
  - Both happen only after signature verification, so the attacker must hold a signing key, and both still fail closed. They do break the exception contract.
  - Test: sign `{"seed": 1e5000-digit int...}` and assert `ProvenanceError`.
- **L2. Unknown top-level keys are accepted and returned.** A signed `{"official": true, "env": {...}}` passes through. Any downstream reader of those keys would treat them as verified schema fields. Fix by rejecting keys outside the v1 set, just as `artifacts` entries already are.
- **L3. Unrestricted string content.** `project` and `argv` allow control characters and lone surrogates (`\ud800`); a lone surrogate in `argv` makes `subprocess` fail later. `seed` has no range limit, while NumPy's legacy seeding needs `0 ≤ seed < 2**32`.
- **L4. Documentation imprecision.**
  - "Symlinks in any path component are rejected" is true only below the root. Ancestors of the root, and the manifest file itself, may be symlinks.
  - "Paths are distinct" means distinct strings only. Hard links and case-insensitive filesystems can alias the same file. Integrity is unaffected because the checks are content hashes.
- **L5. No key revocation.** There is no `-r` revocation list, and SSHSIG signatures carry no timestamp, so `valid-before` is checked against the time of verification, not the time of signing. The only way to revoke a key is to edit `allowed_signers`. Worth documenting.
- **L6. `ssh-keygen` is resolved from PATH and inherits the full environment.** This is documented as a trust requirement. Using an absolute path and a minimal `env` would be cheap hardening.

## Known missing integrations (not new defects)

These are consistent with the stated holds, and the docs already disclose them:

- Replay and cross-project acceptance. `verify_bundle` takes no expected project, Git SHA, run ID, nonce, or expiry, so any validly signed bundle passes. A caller that only checks "no exception was raised" accepts a bundle from another project. When execution binding is designed, add `expected_project` and `expected_source_git_sha` parameters.
- There is a point-in-time time-of-check/time-of-use gap after the function returns. No file descriptors or snapshot are handed back.
- `source` is not bound to `source_git_sha`, `argv[0]` is resolved through PATH, and the environment is not bound to the launched process.
- Identical hashes across `train_split`, `validation_split`, and `test_split` are accepted. That is a scientific-validity question, which I am not certifying. A cheap structural check for distinct digests would be a reasonable optional guard.

The holds on official execution, queued execution, and bootstrap remain warranted. Nothing in this commit should lift them.
