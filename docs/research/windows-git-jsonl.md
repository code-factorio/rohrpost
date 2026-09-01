# Git for Windows vs a committed JSONL event log

Research note for **What Git-for-Windows behaviours threaten the event log**
(`RP-c9tb4p`), a child of the wayfinder map **Cross-machine sync** (`RP-06ywvd`).

Rohrpost commits `.rohrpost/log.jsonl` as its event store and appends to it with
`os.write` (`src/rohrpost/store.py`), while readers go through
`open(..., "r")` plus `strip()`. On Windows, Git's line-ending machinery can rewrite
those bytes both in the working tree and on the way into a commit, and the sync merge
path hands the file to `git merge-file`/`merge=union`. This note records what the
primary sources say about each behaviour.

This is **evidence, not a recommendation.** The consuming decisions are separate tickets.

## Summary

- The Git for Windows installer writes `core.autocrlf` to the **system** config, defaulting
  to `true` ("Checkout Windows-style, commit Unix-style"); `input` and `false` are the two
  other radio choices, and upgrades replay the previous install's choice.
- With `autocrlf=true`, an LF blob checks out with CRLF line endings. Rohrpost's
  `open("r")`/`strip()` reads tolerate that (universal newlines). Its `os.write` appends do
  not: on Windows the fd is opened without `O_BINARY`, so the C runtime opens it in text
  mode and translates every appended `\n` to `\r\n` on disk.
- `.rohrpost/**/*.jsonl  text eol=lf` normalises CRLF to LF in the index on every checkin
  and checks the file out with the same bytes as the index, on any platform and regardless
  of `core.autocrlf`. A repo checked out on Linux and on Windows then holds byte-identical
  working-tree copies.
- The `union` merge driver does no line-ending conversion itself. It merges the blob
  contents, which are LF whenever the `text` attribute applies at checkin. Input
  normalisation happens only with `merge.renormalize`, which is off by default. Line
  comparison is byte-exact, so if CRLF blobs ever reach the merge, the same logical line
  contributed as LF and as CRLF counts as two lines and both survive.
- `git merge-file` is a builtin compiled into `git.exe`, so it is present in every Git for
  Windows flavour: full installer, PortableGit, and MinGit.
- `core.longpaths` is a Git-for-Windows-only config, **disabled by default**, enabling paths
  over the Windows 260-character `MAX_PATH` for builtin commands. Deep `.venv` trees are the
  classic way a clone crosses that threshold.

## What the installer sets `core.autocrlf` to

The installer (Inno Setup script maintained in
[build-extra/installer/install.iss](https://github.com/git-for-windows/build-extra/blob/main/installer/install.iss))
presents a dedicated "Configuring the line ending conversions" page with three radio
buttons, quoting the script's captions and descriptions:

- "Checkout Windows-style, commit Unix-style line endings": Git converts LF to CRLF on
  checkout and CRLF to LF on commit; `core.autocrlf` is set to `"true"`.
- "Checkout as-is, commit Unix-style line endings": no conversion on checkout, CRLF to LF on
  commit; `core.autocrlf` is set to `"input"`.
- "Checkout as-is, commit as-is": no conversion either way; `core.autocrlf` is set to
  `"false"`.

The selection logic defaults to the first option: `ReplayChoice('CRLF Option',
'CRLFAlways')` restores a previous install's choice, and the `else` branch checks
`RdbCRLF[GC_CRLFAlways]`, i.e. `true`. The chosen value is written via
`GitSystemConfigSet('core.autocrlf', Cmd)`, which lands in the system config, not the
repo config. ([install.iss](https://github.com/git-for-windows/build-extra/blob/main/installer/install.iss),
`GC_LFOnly`/`GC_CRLFAlways`/`GC_CRLFCommitAsIs` constants and the page-creation and
config-write blocks)

Upstream documents the two non-obvious mappings: `core.autocrlf=true` "is the same as
setting the `text` attribute to `auto` on all files and `core.eol` to `crlf`", and `input`
means "no output conversion is performed", i.e. no conversion on checkout, so the working
tree keeps LF.
([git-config: core.autocrlf](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coreautocrlf))

## What a CRLF working tree means for files committed with LF

With `autocrlf=true` (the installer default) a JSONL file whose blobs are LF checks out with
CRLF line endings: the `text=auto` behaviour converts on checkout for anything Git
classifies as text, and JSONL contains no NUL bytes, so it is text.
([gitattributes: text=auto](https://git-scm.com/docs/gitattributes#_text),
[text-and-binary detection](https://git-scm.com/docs/gitattributes#_checking-out_and_checking-in))

The `core.safecrlf` documentation states the working-tree consequence directly: "a text file
with `LF` ... could later be checked out with `core.eol=crlf`, in which case the resulting
file would contain `CRLF`, although the original file contained `LF`". It also states that a
mixed-ending file cannot be recreated by Git once converted, and that `safecrlf` reports
mixed-ending files.
([git-config: core.safecrlf](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coresafecrlf))

For rohrpost's own reads this is mostly harmless. `open(..., "r")` defaults to
`newline=None`, which enables universal newlines: lines may end in `'\n'`, `'\r'`, or
`'\r\n'` and are translated to `'\n'` before being returned.
([io.open, newline parameter](https://docs.python.org/3/library/io.html#newline-universal-newlines))
`str.strip()` with no argument removes whitespace, which includes `\r`.
([str.strip](https://docs.python.org/3/library/stdtypes.html#str.strip))
So `read_events_lenient` in `src/rohrpost/store.py` decodes a CRLF working tree fine.

The appends are the other story. `append_event` opens the log with
`os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)` and writes
`encode(event) + b"\n"`. Python documents that on Windows "adding `O_BINARY` is needed to
open files in binary mode"; without it the descriptor takes the C runtime's default mode,
which is text (`_O_TEXT`), and text mode replaces each line feed with a CRLF pair on output.
The interpreter does not change this default: `os_open_impl` adds only `O_NOINHERIT` on
Windows, the `pythoncore.vcxproj` build does not link `binmode.obj`, and `_fmode`'s initial
value is `_O_TEXT`.
([Python os.open note](https://docs.python.org/3/library/os.html#os.open),
[osmodule.c os_open_impl](https://github.com/python/cpython/blob/main/Modules/posixmodule.c),
[_fmode](https://learn.microsoft.com/en-us/cpp/c-runtime-library/fmode?view=msvc-170),
[text and binary mode file I/O](https://learn.microsoft.com/en-us/cpp/c-runtime-library/text-and-binary-mode-file-i-o?view=msvc-170),
[_write](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/write?view=msvc-170))

Two consequences follow for a Windows checkout:

- With `autocrlf=true`, appended lines match the rest of the working-tree file (all CRLF),
  and the text attribute or `autocrlf` converts everything back to LF at checkin, so blobs
  stay LF. The working tree is byte-wise different from the blob while it sits uncommitted.
- With `autocrlf=input` or `false`, the working-tree file is otherwise LF, so each `os.write`
  append introduces CRLF lines into an LF file: mixed endings in the working tree. At
  checkin, the `text` attribute still normalises those CRLFs to LF, but with `autocrlf=false`
  and no attribute the CRLF lines enter the blob and travel in history.
  ([gitattributes: text Set](https://git-scm.com/docs/gitattributes#_text))

The short-write rollback in `append_event` (`written != len(line)`) cannot catch any of
this: the CRT documents that the LF-to-CRLF replacement "doesn't affect the return value", so
`os.write` reports the untranslated byte count while writing more bytes to disk.
([_write](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/write?view=msvc-170))

## What `text eol=lf` guarantees

Setting the `text` attribute "enables end-of-line conversion ... When a matching file is
added to the index, the file's line endings are normalized to LF in the index", and this
"every time the file is checked in". The `eol` attribute sets the working-tree style; `eol=lf`
"uses the same line endings in the working directory as in the index when the file is checked
out". Specifying `eol` automatically sets `text` if `text` was left unspecified.
([gitattributes: text, eol](https://git-scm.com/docs/gitattributes#_eol))

The attribute overrides config: "If the `eol` attribute is unspecified for a file, its line
endings in the working directory are determined by the `core.autocrlf` or `core.eol`
configuration variable", and `core.eol` itself "is ignored if `core.autocrlf` is set to
`true` or `input`".
([gitattributes: eol](https://git-scm.com/docs/gitattributes#_eol),
[git-config: core.eol](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coreeol))

Together: with `.rohrpost/**/*.jsonl  text eol=lf`, checkin normalises whatever a Windows
writer produced to LF in the blob, and checkout reproduces the blob's bytes on any platform.
A clone checked out on Linux and one on Windows hold byte-identical working-tree copies of
the log, and every commit is byte-identical to what `append_event` wrote. The line endings
are consistent, "either all LF or all CRLF, but never mixed", in the wording the `safecrlf`
documentation uses for per-worktree consistency.
([git-config: core.safecrlf](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coresafecrlf))

## Does `merge=union` interact with CRLF?

The union driver does no conversion. In `merge-ll.c`, `ll_union_merge` sets the
`XDL_MERGE_FAVOR_UNION` variant and delegates to the xdiff merge; nothing in the driver
touches line endings. The documented behaviour is to "take lines from both versions, instead
of leaving conflict markers", with the warning that "this tends to leave the added lines in
the resulting file in random order".
([merge-ll.c](https://github.com/git/git/blob/master/merge-ll.c),
[gitattributes: union](https://git-scm.com/docs/gitattributes#_built-in_merge_drivers))

What the merge sees are blob contents, not working-tree files: `merge-ort` loads the three
versions with `read_mmblob` from the object database before calling `ll_merge`. With the
`text` attribute in force at checkin, those blobs are LF, so the union merge itself runs over
LF lines on both sides, and the merged result is written to the working tree through the
normal checkout conversion (the gitattributes effects section lists `git merge` among the
commands that copy repository content to working-tree files).
([merge-ort.c](https://github.com/git/git/blob/master/merge-ort.c),
[gitattributes: checking-out and checking-in](https://git-scm.com/docs/gitattributes#_checking-out_and_checking-in))

Input normalisation exists but is opt-in: `ll_merge` calls `normalize_file` on all three
inputs only when `opts->renormalize` is set, which is driven by `merge.renormalize`. That
config "can convert the data recorded in commits to a canonical form before performing a
merge to reduce unnecessary conflicts", for repositories where "earlier commits record text
files with CRLF line endings, but recent ones use LF".
([merge-ll.c, normalize_file](https://github.com/git/git/blob/master/merge-ll.c),
[git-config: merge.renormalize](https://git-scm.com/docs/git-config#Documentation/git-config.txt-mergerenormalize))

If CRLF blobs do reach the merge (a repo where the file was committed before any
normalisation applied, or committed with `autocrlf=false` and no attribute), line comparison
is byte-exact: `xdl_recmatch` returns equality only on identical bytes unless whitespace
flags are set, and the merge sets none by default (`xmp.xpp.flags = opts->xdl_opts`).
The same logical line contributed as LF by one side and as CRLF by the other therefore counts
as two different records, and union takes both: the merged blob carries mixed endings and
duplicated-looking lines.
([xutils.c, xdl_recmatch](https://github.com/git/git/blob/master/xdiff/xutils.c),
[merge-ll.c, ll_xdl_merge](https://github.com/git/git/blob/master/merge-ll.c))

One further degradation: if any of the three inputs contains a NUL byte in its first 8000
bytes, `buffer_is_binary` classifies it as binary and the xdiff merge is skipped. For the
union variant the fallback is `ll_binary_merge`, which for a non-virtual ancestor takes
`src1` (ours) and reports a binary conflict, so the other side's lines silently disappear
from the tentative result. A JSONL file with an embedded NUL would take that path.
([xdiff-interface.c, buffer_is_binary](https://github.com/git/git/blob/master/xdiff-interface.c),
[merge-ll.c, ll_binary_merge](https://github.com/git/git/blob/master/merge-ll.c))

## Is `git merge-file` available in typical Git-for-Windows installs?

`merge-file` is a builtin command: it is implemented in `builtin/merge-file.c` and
registered in git.c's command table as `{ "merge-file", cmd_merge_file, RUN_SETUP_GENTLY }`,
so it is compiled into `git.exe` and also works outside a repository.
([builtin/merge-file.c](https://github.com/git/git/blob/master/builtin/merge-file.c),
[git.c](https://github.com/git/git/blob/master/git.c))

Every Git for Windows distribution ships that `git.exe`:

- The full installer and PortableGit are the same distribution, packaged respectively as an
  installer and a portable archive ([gitforwindows.org](https://gitforwindows.org/)).
- MinGit is "an intentionally minimal, non-interactive distribution" that "bundles `git.exe`
  and supporting cast"; it "excludes localized messages, interactive commands, help
  documents, executables that are not called by `git.exe`, and the likes", plus Tcl/Tk and
  Perl. Nothing in that exclusion list removes builtins from `git.exe`, and MinGit's build
  script relocates the `libexec/git-core/*.exe` helpers into `bin/` rather than dropping
  them. ([gitforwindows.org/MinGit](https://gitforwindows.org/MinGit),
  [mingit/release.sh](https://github.com/git-for-windows/build-extra/blob/main/mingit/release.sh))

`git merge-file` also carries the `--union` option ("Include all lines from all files"),
which is what a sync path would use in place of the `merge=union` attribute when merging
outside a real merge.
([git-merge-file](https://git-scm.com/docs/git-merge-file))

## `core.longpaths` on Git for Windows

`core.longpaths` does not exist in upstream Git's config documentation; it is documented only
in the Git-for-Windows fork: "Enable long path (> 260) support for builtin commands in Git
for Windows. This is disabled by default, as long paths are not supported by Windows
Explorer, cmd.exe and the Git for Windows tool chain (msys, bash, tcl, perl...). Only enable
this if you know what you're doing and are prepared to live with a few quirks."
([upstream core.adoc, no longpaths](https://github.com/git/git/blob/master/Documentation/config/core.adoc),
[Git for Windows core.adoc](https://github.com/git-for-windows/git/blob/main/Documentation/config/core.adoc))

The 260 threshold is Windows' `MAX_PATH`: "the maximum length for a path is MAX_PATH, which
is defined as 260 characters", and Microsoft's own example of hitting it is cloning "a git
repo that has long file names into a folder that itself has a long name".
([Maximum path length limitation](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation))

Whether it matters for rohrpost depends on absolute path length: a `.venv` created inside a
clone nests `.venv/Lib/site-packages/<package>/...` under the clone root, and virtualenvs
place a full-path copy of the interpreter inside `.venv/Scripts/`, so the same repo can fit
under 260 characters at a short clone location and exceed it in a deeper user directory. The
limit is checked by the OS against the full path, which is why the same tree works in one
location and fails in another.
([Maximum path length limitation](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation))
