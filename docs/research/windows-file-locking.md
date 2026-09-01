# `msvcrt.locking` semantics for a blocking exclusive lock

Research note for **[win-1] msvcrt locking semantics** (`RP-acx7sx`), a child of the wayfinder
map **Windows support** (`RP-06ywvd`).

`store.file_lock` today takes an exclusive blocking `fcntl.flock` on `.rohrpost/.lock`
([store.py](../../src/rohrpost/store.py)). That call does not exist on Windows, so the decision
ticket (`RP-sdjcft`) needs to know what `msvcrt.locking` actually provides. This note pins each
semantic down to a primary source: the [Python 3.14 `msvcrt` docs](https://docs.python.org/3.14/library/msvcrt.html#msvcrt.locking),
CPython 3.14 source, Microsoft's CRT and Win32 documentation, and one Raymond Chen post that
Microsoft documents link to for append concurrency.

This is **evidence, not a recommendation.** The consuming decision is separate.

## Summary

- `msvcrt.locking` locks a **byte range**, from the fd's current position for `nbytes` bytes.
  The range may extend past end of file, so a 0-byte lock file can be locked; nothing requires
  the locked bytes to exist.
- The lock belongs to the **file handle**, not the process. A second handle in the same process
  can neither take an overlapping exclusive lock nor read or write the locked bytes through it.
  Unlike `flock`, the locks are enforced: `ReadFile`/`WriteFile` through another handle fail on
  a locked range.
- `LK_LOCK`/`LK_RLCK` retry once per second and give up after **10 attempts** (~10 s), raising
  `OSError` with errno `EDEADLOCK`. `LK_NBLCK`/`LK_NBRLCK` fail immediately. `LK_UNLCK` must
  name the exact previously locked range; adjacent regions are not merged.
- No `msvcrt` mode waits indefinitely. Unbounded blocking means calling `LK_NBLCK` (or
  `LK_LOCK`) in a loop; the kernel-level "wait until acquired" mode exists only in `LockFileEx`,
  which the `msvcrt` module does not expose. The GIL is released during `locking` calls.
- The OS **unlocks ranges when the holding process terminates**, but the docs give no timing
  guarantee and warn access may be denied until the OS gets around to it.
- Windows documents `O_APPEND` as "move the pointer to the end before every write", not as an
  atomic step. The Linux guarantee that a single `O_APPEND` write is atomic (spec §7) has no
  stated Windows equivalent; the documented Windows tool for concurrent appenders is a
  byte-range lock.

## The contract `store.file_lock` relies on today

The lock is `fcntl.flock(fh.fileno(), fcntl.LOCK_EX)` on `.rohrpost/.lock`, opened
`"a+"`, with an explicit unlock and close in `finally`. The docstring records the contract:
exclusive, blocking, held for the duration of the block, per open file description, and
callers must not nest two `file_lock` calls on the same dir
([store.py](../../src/rohrpost/store.py)).

[flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html) defines the terms: locks are
"associated with an open file description", so descriptors created by `dup` share the lock while
a second `open()` is "treated independently" and "may be denied by a lock that the calling
process has already placed"; the lock is released by `LOCK_UN` or "when all such file
descriptors have been closed". The lock is advisory only: "a process is free to ignore the use
of `flock()` and perform I/O on the file".

Appends rely on a second Linux behaviour, spec §7
([spec](../spec/ROHRPOST-SPEC.md)): one `write()` of one line under `O_APPEND`. Linux
documents `O_APPEND` as "[t]he modification of the file offset and the write operation are
performed as a single atomic step", with an NFS exception
([open(2)](https://man7.org/linux/man-pages/man2/open.2.html)).

## What does `msvcrt.locking` lock, and does the range have to exist?

`msvcrt.locking(fd, mode, nbytes)` locks the region "[extending] from the current file position
for `nbytes` bytes, and ... may continue beyond the end of the file"
([Python docs](https://docs.python.org/3.14/library/msvcrt.html#msvcrt.locking)). The CRT page
for the underlying `_locking` says the same: "All locking or unlocking begins at the current
position of the file pointer and proceeds for the next `nbytes` bytes. It's possible to lock
bytes past end of file" ([_locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)).

The region does not need to exist in the file. The Win32 layer underneath, `LockFile`, states:
"You can lock bytes that are beyond the end of the current file. This is useful to coordinate
adding records to the end of a file"
([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)).
Raymond Chen's append-concurrency recipe exploits exactly this, locking "a nonexistent byte well
beyond the anticipated maximum file size"
([The Old New Thing, 2015](https://devblogs.microsoft.com/oldnewthing/20151127-00/?p=92211)).
A 0-byte `.lock` file is lockable; a common choice is a 1-byte range at offset 0, which may lie
past end of file of an empty file.

Two constraints on the descriptor: `LockFile` "must have been created with the `GENERIC_READ`
or `GENERIC_WRITE` access right" ([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)),
and the `fd` passed to `msvcrt.locking` must be a C-runtime descriptor, since the function is
"based on file descriptor `fd` from the C runtime"
([Python docs](https://docs.python.org/3.14/library/msvcrt.html#msvcrt.locking)). A file object
from `open("a+")` satisfies both (see the `a+` section below).

## Handle or process? What happens with a second handle in the same process?

The lock attaches to the handle. "Locking a region of a file gives the threads of the locking
process exclusive access to the specified region using this file handle", and the same page is
explicit about re-entry: "If the locking process opens the file a second time, it cannot access
the specified region through this second handle until it unlocks the region"
([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)).
Acquisition conflicts too: "Exclusive locks cannot overlap an existing locked region of a file".
The conceptual doc extends this to all I/O: "Attempts to access a byte range that is locked by
another process always fail. If the locking process attempts to access a locked byte range
through a second file handle, the attempt fails"
([byte-range locking](https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files)).
`WriteFile` documents the write side: "If part of the file is locked by another process and the
write operation overlaps the locked portion, `WriteFile` fails"
([WriteFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile)).

So Windows range locks are enforced by the OS, not advisory. This differs from `flock` on two
axes ([flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)):

- **Who may conflict.** `flock` locks belong to the open file description. Descriptors from
  `dup`/`fork` share one lock; a separate `open()` gets an independent lock that may be denied
  by the caller's own lock. Windows locks belong to the handle, so any second handle in the
  same process is excluded, including for plain reads and writes of the locked bytes.
- **Advisory vs enforced.** `flock` "places advisory locks only"; a process can ignore it.
  Windows `ReadFile`/`WriteFile` fail outright on a locked range, through any handle other than
  the locking one.

For the specific hazard `file_lock` documents (nesting two calls on the same dir), the outcome
is failure on both platforms because `file_lock` opens a fresh descriptor per call: under
`flock` the nested call blocks on an independent open file description; under `msvcrt` the
nested call holds a second handle and hits the overlap rule, failing fast with `LK_NBLCK` or
after the retry budget with `LK_LOCK`.

One blind spot either way: "Locking a region of a file does not prevent reading or writing from
a mapped file view" ([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)),
and Windows byte-range locks "are ignored when using memory mapped files"
([byte-range locking](https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files)).

## What do `LK_LOCK`, `LK_NBLCK`, `LK_RLCK`, `LK_NBRLCK` and `LK_UNLCK` do?

The Python constants are the CRT `_LK_*` values, inserted directly from `<sys/locking.h>`
([msvcrtmodule.c](https://github.com/python/cpython/blob/3.14/PC/msvcrtmodule.c)), and
`msvcrt.locking` is a thin wrapper that releases the GIL around the call and raises `OSError`
from `errno` on failure
([msvcrtmodule.c, `msvcrt_locking_impl`](https://github.com/python/cpython/blob/3.14/PC/msvcrtmodule.c)).
The modes ([Python docs](https://docs.python.org/3.14/library/msvcrt.html#msvcrt.LK_LOCK),
[_locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)):

| Mode | Behaviour |
|---|---|
| `LK_LOCK`, `LK_RLCK` | Locks the bytes; if taken, "tries again after 1 second"; after 10 attempts returns an error. The CRT reports this as errno `EDEADLOCK`. `LK_RLCK` is "Same as `LK_LOCK`". CPython raises `OSError`. |
| `LK_NBLCK`, `LK_NBRLCK` | Locks the bytes or errors immediately. `LK_NBRLCK` is "Same as `LK_NBLCK`". |
| `LK_UNLCK` | "Unlocks the specified bytes, which must have been previously locked." |

Unlock requirements: the region being unlocked must match a previously locked one, and locking
does not merge adjacent regions, "if two locked regions are adjacent, each region must be
unlocked separately" ([_locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)).
Since both lock and unlock start at the current file position, the unlock call depends on the
fd position too. Other error codes: `EACCES` for a plain locking violation, `EBADF` for a bad
descriptor ([_locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)).
Non-overlapping regions may be locked simultaneously; overlapping exclusive locks fail.

## What does a blocking wait look like? Is `LK_LOCK`'s retry enough?

Documented facts that constrain the shape:

- A single `LK_LOCK` call is bounded: at most 10 tries, 1 second apart, then `OSError`
  ([Python docs](https://docs.python.org/3.14/library/msvcrt.html#msvcrt.LK_LOCK),
  [_locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)).
  The 1-second interval and the 10-try budget are fixed; neither is a parameter.
- No `msvcrt` mode waits indefinitely. The kernel does offer "a file lock request that will
  block until the lock is acquired", namely `LockFileEx` without `LOCKFILE_FAIL_IMMEDIATELY`
  ([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)),
  but `LockFileEx` is not among the functions `msvcrt` exposes
  ([Python docs function list](https://docs.python.org/3.14/library/msvcrt.html#file-operations),
  [msvcrtmodule.c](https://github.com/python/cpython/blob/3.14/PC/msvcrtmodule.c)).
- During the call the GIL is released
  ([msvcrtmodule.c](https://github.com/python/cpython/blob/3.14/PC/msvcrtmodule.c)), so a
  blocked or retrying `locking` call does not hold the GIL.

Within the stdlib, then, "blocking" can only mean repeated `locking` calls, each either
`LK_NBLCK` (caller controls the interval, catches `OSError`) or `LK_LOCK` (CRT paces the
retries at 1 s and fails after ~10 s). The docs define no call that blocks for an unbounded
time; anything longer needs a loop in Python code. For contrast, `fcntl.flock` without
`LOCK_NB` blocks with the wait handled by the kernel, and can be interrupted (`EINTR`),
which `file_lock` currently surfaces
([flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)).

## Are locks released when the holding process dies?

Yes, but without a timing guarantee. `LockFile`'s remarks: "If a process terminates with a
portion of a file locked or closes a file that has outstanding locks, the locks are unlocked by
the operating system. However, the time it takes for the operating system to unlock these locks
depends upon available system resources. Therefore, it is recommended that your process
explicitly unlock all files it has locked when it terminates. If this is not done, access to
these files may be denied if the operating system has not yet unlocked them"
([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)).
The CRT adds the hygiene rule that regions "should be locked only briefly and should be
unlocked before closing a file or exiting the program"
([_locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)).

On the Linux side, release on death follows from the descriptor rule: the lock "is released ...
when all such file descriptors have been closed", and process termination closes the
descriptors ([flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)). The Windows page
is the only one of the two that names residual risk: an unlock that has not happened yet can
deny access to a waiter.

## How does this interact with `lock.open("a+", encoding="utf-8")` and `O_APPEND` appends?

**The fd layer.** On CPython 3.14, `open("a+", encoding="utf-8")` builds a `FileIO` whose mode
`a` maps to `O_APPEND | O_CREAT` and `+` to `O_RDWR`; the fd is always opened with `O_BINARY`
when defined, plus `O_NOINHERIT` on Windows, via the CRT `_wopen`
([fileio.c](https://github.com/python/cpython/blob/3.14/Modules/_io/fileio.c)). `os.open` also
routes through `_wopen` on Windows
([posixmodule.c](https://github.com/python/cpython/blob/3.14/Modules/posixmodule.c)). So the
descriptor behind both `file_lock`'s `fh.fileno()` and `append_event`'s `os.open` is a CRT fd,
which is what `msvcrt.locking` requires. Encoding and newline translation happen above the fd
in `TextIOWrapper`; the forced `O_BINARY` means the CRT does no translation, so the position
`msvcrt.locking` uses is a plain byte offset.

**The append flag.** At the CRT level, `_O_APPEND` means "Moves the file pointer to the end of
the file before every write operation"
([_open](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen)).
That is a per-write seek performed by the CRT; the docs describe it as two steps, not one
atomic step, and nothing repositions the pointer at open time. Since `msvcrt.locking` locks
from the current position, the locked range depends on where the fd happens to be when the
call is made, a dependency `flock` does not have (it locks the whole file, not a range).

**Append atomicity.** The Linux guarantee the spec leans on has no stated Windows counterpart:

- Linux: "Before each write(2), the file offset is positioned at the end of the file ... The
  modification of the file offset and the write operation are performed as a single atomic
  step." NFS is the documented exception ([open(2)](https://man7.org/linux/man-pages/man2/open.2.html)).
- Windows: `FILE_APPEND_DATA` is defined as an access right, "the right to append data to the
  file", under which "write operations will not overwrite existing data"
  ([file access rights](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants)).
  `WriteFile` offers write-at-EOF via `Offset`/`OffsetHigh` `0xFFFFFFFF`, "functionally
  equivalent to ... `FILE_APPEND_DATA` access"
  ([WriteFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile)).
  Neither page claims the offset pick and the write are atomic against concurrent appenders.
  The only atomicity statement on the `WriteFile` page is sector-scoped: "Although a
  single-sector write is atomic, a multi-sector write is not guaranteed to be atomic unless you
  are using a transaction."
- Chen's treatment of concurrent appenders points the same way: a `FILE_APPEND_DATA`-only open
  means "the caller can write only to the end of the file, and any offset information provided
  in the write operation is ignored", and for multiple processes appending he reaches for
  `LockFile`, "precisely the job that `LockFile` was created to solve", because the
  set-pointer-then-write pattern races. His footnote adds that the `OVERLAPPED.Offset` is not
  updated with where the append actually landed
  ([The Old New Thing, 2015](https://devblogs.microsoft.com/oldnewthing/20151127-00/?p=92211)).

**Network file systems.** Windows `LockFile` is supported on SMB 3.0, including Transparent
Failover, CsvFS and ReFS ([LockFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile)).
The Linux `flock` emulation has its own quirks there: since Linux 5.5, `flock` over CIFS is
emulated with SMB byte-range locks on the whole file and "the locks are not advisory anymore",
and up to Linux 5.4 they were not propagated at all
([flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)).

## Source index

| Source | What it establishes |
|---|---|
| [Python 3.14 `msvcrt` docs](https://docs.python.org/3.14/library/msvcrt.html#msvcrt.locking) | `locking` API, region from current position, past-EOF, `LK_*` behaviours, 1 s / 10 attempts |
| [`PC/msvcrtmodule.c` (CPython 3.14)](https://github.com/python/cpython/blob/3.14/PC/msvcrtmodule.c) | Thin `_locking` wrapper, GIL released, `OSError` from `errno`, `LK_*` = CRT constants |
| [CRT `_locking`](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking) | Position dependence, past-EOF, `EDEADLOCK`/`EACCES`, unlock must match, unlock-before-exit advice |
| [Win32 `LockFile`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile) | Per-handle scope, second-handle exclusion, exclusive locks cannot overlap, release on termination, `LockFileEx` blocking, GENERIC_READ/WRITE, SMB support |
| [Byte-range locking (Win32 conceptual)](https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files) | Enforcement against all other handles, mapped-view blind spot, exclusive vs shared locks |
| [Win32 `WriteFile`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile) | Locked-range writes fail, write-at-EOF `0xFFFFFFFF` ≡ `FILE_APPEND_DATA`, single-sector atomicity statement |
| [File access rights constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants) | `FILE_APPEND_DATA` as an access right, no-overwrite |
| [CRT `_open`/`_wopen`](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen) | `_O_APPEND` = pointer to end before every write |
| [Raymond Chen, The Old New Thing (2015)](https://devblogs.microsoft.com/oldnewthing/20151127-00/?p=92211) | `FILE_APPEND_DATA` documented meaning, `LockFile` as the append-concurrency tool, past-EOF lock bytes |
| [CPython `Modules/_io/fileio.c` (3.14)](https://github.com/python/cpython/blob/3.14/Modules/_io/fileio.c) | `a` → `O_APPEND\|O_CREAT`, forced `O_BINARY`, `_wopen`, `O_NOINHERIT` on Windows |
| [CPython `Modules/posixmodule.c` (3.14)](https://github.com/python/cpython/blob/3.14/Modules/posixmodule.c) | `os.open` → `_wopen` on Windows |
| [flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html) | POSIX contract: advisory, per-OFD, dup shares, second open independent, release on close, CIFS/NFS behaviour |
| [open(2)](https://man7.org/linux/man-pages/man2/open.2.html) | `O_APPEND` offset update + write as a single atomic step, NFS exception |
