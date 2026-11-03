# Delta review: c1c3d89 → e1e62f1

This review covers only the diff you pasted. I ran no tools and made no live claims. Anything that depends on code outside the diff, such as `_identity`, `main()` or the existing symlink test, is marked as unverified.

## Verdict

I found **no HIGH or CRITICAL issues** in the new read path. The O_PATH pin plus `/proc/self/fd` reopen is the right construction for Linux 5.4, where `openat2` is not available. There is one conditional item you should check before merging, and several MEDIUM/LOW items.

## Manifest read and inode pinning: correct

- **No driver open on special files.** `O_PATH | O_NOFOLLOW` resolves the path without calling the driver's or FIFO's `open`. `fstat` on an O_PATH fd works on 5.4. Type and size are checked before any real open.
- **The reopen is pinned to the inode.** Opening `/proc/self/fd/N` follows the magic link to the already-pinned inode, not the original path. A rename or swap after the first `os.open` cannot change what gets read.
- **The file type cannot change.** Inode type is immutable, so the `S_ISREG` check holds through the reopen. Content can still grow, but the bounded `read(MAX+1)` handles that.
- **Omitting `O_NOFOLLOW` on the reopen is correct.** Adding it would make the magic-link open fail with ELOOP.
- **Permissions are re-checked on the reopen.** It cannot give more access than a normal `O_RDONLY` open.
- **No fd leaks.** The inner and outer `finally` blocks cover every failure point (`/proc` open, `fstat`, `fdopen`). The `fd = None` handoff to the file object is correct.

## Conditional: possible symlink-test regression (HIGH if it applies)

With `O_PATH`, `O_NOFOLLOW` no longer fails with ELOOP on a final symlink. Instead it returns an fd for the symlink itself, and `fstat` reports `S_IFLNK`. The symlink is still rejected, but the error is now `ArtifactError('manifest must be a regular file')` instead of an `OSError`/ELOOP.

If the symlink-defense test added in c1c3d89 asserts `OSError`, ELOOP, or a specific message, it will now fail, unless `ArtifactError` subclasses `OSError`. The security behavior is fine; check that test's assertion.

## MEDIUM

1. **Missing `/proc` produces the wrong error type.** In a chroot, minimal container or some sandboxes, the reopen raises `FileNotFoundError`/`PermissionError` rather than `ArtifactError`. It fails closed, but if `main()` only catches `ArtifactError`, users get a traceback. Wrap the reopen and re-raise as `ArtifactError('/proc required to reopen pinned manifest')`.
2. **The new test's skip guard is wrong.** It skips on whether `/dev/null` exists, but the code depends on `O_PATH`. On macOS/BSD, `/dev/null` exists, so the test runs and fails: it gets 'Linux O_PATH support required' instead of 'regular file', or `m.os.O_PATH` raises `AttributeError`. Use `skipUnless(hasattr(os, 'O_PATH'), ...)`. This is LOW if CI is Linux-only.
3. **The test does not cover the threat that motivated the change.** Opening `/dev/null` is harmless. The real risks are FIFOs (blocking or side effects) and real devices. Add a `os.mkfifo` test in a temp dir. It needs no `/dev`, and it actually exercises the "don't open it" property.
4. **The test's "not opened" assertion is narrow.** `call_count == 1` only proves no second `os.open`. A later regression to builtin `open()`, `io.open` or `os.openat` would still pass. That is acceptable as a regression guard, but the test name overstates it. Also assert `O_NOFOLLOW` in the flags.

## LOW / pre-existing (not regressions)

- **Parent directories are still followed.** `O_NOFOLLOW` only covers the final component. If the threat model includes swapping parent directories, you need a dirfd walk, since `RESOLVE_NO_SYMLINKS` requires 5.6+. That matches the existing behavior, but it is inconsistent with the "atomic parent guard" in ea7395c.
- **The "changed before read" check may never fire.** It can only trigger if `_identity` includes mutable fields (size, mtime_ns, ctime_ns). If it is only `(st_dev, st_ino)`, the check is a no-op and in-place modification during the read goes undetected. That limitation existed before this commit. Unverified, since `_identity` is not in the diff.
- **FUSE files can still block.** A regular file on FUSE can block on open or read. That is out of scope unless manifests may live on untrusted mounts.
- **No positive-path test for the reopen.** Existing read tests presumably exercise it. Confirm at least one test reads a valid regular file through the new path.

## Suggested before merge

1. Fix or confirm the symlink test assertion.
2. Map `OSError` from the `/proc` reopen to `ArtifactError`.
3. Change the skip guard to check for `O_PATH`.
4. Add a FIFO rejection test.
