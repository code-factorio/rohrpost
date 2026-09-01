# uv + CPython on native Windows: runtime facts

Research note for **uv + Python 3.14 on Windows runtime facts** (`RP-5t88jp`), a child of the
wayfinder map **Windows support** (`RP-06ywvd`).

Question: which runtime facts about uv and CPython on native Windows do the wrapper, install
playbook, and CI work depend on? Rohrpost defines one console script, `rp = "rohrpost.cli:main"` in the repo's `pyproject.toml`
under `[project.scripts]`, built with `uv_build`, so every claim below is about what happens to
*that* script on a Windows machine.

This is **evidence, not a recommendation.** The consuming decisions are separate tickets.

## Summary

- After `uv sync`, the console script lands as `.venv\Scripts\rp.exe`. It is a Win32 "trampoline"
  binary written by uv, not a `.bat`, not `rp-script.py`, and not an embedded Python: the `.exe`
  embeds a zip of the entry-point script plus the path to the venv's `python.exe`, and invoking
  it runs that interpreter over the embedded zip.
- PowerShell, cmd.exe, and Git Bash all run the same `.exe` by name; Git Bash (Cygwin-lineage
  runtime) additionally accepts the extensionless spelling `rp`, with a documented rule that a
  same-named shell script would win over the `.exe`.
- `uvx rohrpost` builds a disposable environment inside the uv cache, `%LOCALAPPDATA%\uv\cache`
  on Windows, and pins the resolved version for later invocations until the cache is pruned or
  refreshed. `uv tool install` instead puts the environment in `%APPDATA%\uv\data\tools` and
  copies executables to `%USERPROFILE%\.local\bin`.
- Windows' own folder semantics say `%LOCALAPPDATA%` is the per-user, non-roaming data home,
  `%APPDATA%` (Roaming) is for data that follows a user across machines, and Microsoft explicitly
  tells applications not to create files at the `%USERPROFILE%` root. pip and uv both put caches
  in `%LOCALAPPDATA%\<tool>\cache`.
- The gotchas with primary-source backing: `%LOCALAPPDATA%\Microsoft\WindowsApps` execution
  aliases that make bare `python` open the Microsoft Store, the 260-character MAX_PATH limit
  (opt-in fix per machine plus per app manifest), OneDrive breaking uv's hardlink-based installs
  into venvs under synced folders, and Microsoft Defender scanning overhead on dev workloads.

## `.venv` layout after `uv sync` and where `rp` lands

`uv sync` creates the project environment at `.venv` next to the `pyproject.toml`, overridable
with `UV_PROJECT_ENVIRONMENT`
([uv storage reference](https://docs.astral.sh/uv/reference/storage/#project-virtual-environments)).

The venv layout on Windows is the standard one: a `Scripts` subdirectory holding a copy (or
symlink) of the Python interpreter, and `Lib\site-packages` for packages
([venv module docs, 3.14](https://docs.python.org/3.14/library/venv.html#creating-virtual-environments):
"creates a `bin` (or `Scripts` on Windows) subdirectory... on Windows, this is
`Lib\site-packages`"). The "Using Python on Windows" chapter activates with
`<env>\Scripts\Activate`, confirming the same layout for real 3.14 environments
([Python 3.14 docs](https://docs.python.org/3.14/using/windows.html#using-python-on-windows)).

The `rp` console script declared in `[project.scripts]` therefore lands as `.venv\Scripts\rp.exe`.

### What the `.exe` actually is

uv does not reuse setuptools' `foo.exe` + `foo-script.py` pair, and it does not embed a Python
interpreter. Windows console-script shims written by uv are "trampolines": small prebuilt Rust
binaries, a fork of [posy's trampolines](https://github.com/njsmith/posy) with logic copied from
distlib's launchers
([uv-trampoline README](https://github.com/astral-sh/uv/tree/main/crates/uv-trampoline)). The
`.exe` carries PE resources holding the trampoline kind (script or Python launcher), the path to
`python.exe`, and a zip containing `__main__.py`. When invoked it runs
`python.exe path\to\<the .exe>`, and Python's zipimport executes the embedded `__main__.py` from
inside the `.exe` itself
([uv-trampoline README](https://github.com/astral-sh/uv/tree/main/crates/uv-trampoline#how-do-you-use-it)).
So there is exactly one shim file, `rp.exe`; no `rp-script.py` sidecar exists to fall out of
sync, and the shim binds to a specific interpreter path.

The same trampoline mechanism back the venv's `python.exe`: a commit changing trampoline
behaviour describes pointing it at venv interpreters versus installed interpreters like
`.local\bin\python3.10.exe`
([uv commit 4879934](https://github.com/astral-sh/uv/commit/4879934b20d9c8771adf907e61457f6650a080fc)).
The README's own smoke test runs `.venv\Scripts\black --version` on a Windows machine, which
exercises the exact layout `rp.exe` will have
([uv-trampoline README](https://github.com/astral-sh/uv/tree/main/crates/uv-trampoline#testing-the-trampolines)).

### Behaviour from PowerShell, cmd.exe, Git Bash

- PowerShell and cmd.exe execute `rp.exe` as an ordinary Win32 console binary found via `PATH`;
  no activation is required, since the interpreter path is embedded in the shim. CPython's venv
  docs state scripts installed in an environment "should be runnable without activating it"
  ([venv docs, 3.14](https://docs.python.org/3.14/library/venv.html#how-venvs-work)). Activation
  exists for shell convenience: `<venv>\Scripts\activate.bat` for cmd and
  `<venv>\Scripts\Activate.ps1` for PowerShell, the latter gated by PowerShell's execution
  policy, which may need `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`
  ([venv docs, 3.14](https://docs.python.org/3.14/library/venv.html#creating-virtual-environments)).
- Git Bash is Git for Windows' bundled bash
  ([gitforwindows.org](https://gitforwindows.org/)); its unixy tools are directly based on
  Cygwin ([MSYS2 docs](https://www.msys2.org/docs/what-is-msys2/#msys2-vs-cygwin)). For that
  runtime family, the Cygwin docs state: "Win32 executable filenames end with `.exe` but the
  `.exe` need not be included in the command, so that traditional UNIX names can be used", and
  `ls`/`stat` transparently resolve `filename` to `filename.exe`
  ([Cygwin UG, The .exe extension](https://cygwin.com/cygwin-ug-net/using-specialnames.html#pathnames-exe)).
  `rp` typed in Git Bash therefore resolves to `rp.exe`, provided the `Scripts` directory is on
  `PATH` (the Windows `%PATH%` is converted to POSIX form when the first Cygwin process starts
  ([Cygwin UG](https://cygwin.com/cygwin-ug-net/using.html))).
- One documented shell difference: if an extensionless `rp` shell script and `rp.exe` coexist in
  the same directory, the shell script has precedence for `rp`
  ([Cygwin UG, The .exe extension](https://cygwin.com/cygwin-ug-net/using-specialnames.html#pathnames-exe)).
  uv writes only the `.exe` for entry points it installs, and for `uv tool install` it copies
  executables into the bin directory rather than symlinking on Windows
  ([uv tools concept](https://docs.astral.sh/uv/concepts/tools/#tool-executables)), so the
  conflict is theoretical for uv-produced layouts.

Net: the same `rp.exe` serves all three shells; the differences are PATH spelling and the
PowerShell-only execution-policy gate on `Activate.ps1`.

## `uvx rohrpost`: resolution, cache location, first-run behaviour

`uvx` is an alias for `uv tool run`; the two are exactly equivalent. Running a tool with `uvx`
installs its dependencies into a temporary virtual environment isolated from the current project
([uv tools concept](https://docs.astral.sh/uv/concepts/tools/#the-uv-tool-interface)), nearly
equivalent to `uv run --no-project --with rohrpost -- rohrpost`
([uv tools concept](https://docs.astral.sh/uv/concepts/tools/#relationship-to-uv-run)).

- Cache location. That disposable environment is stored inside the uv cache directory, which on
  Windows is `%LOCALAPPDATA%\uv\cache` (or `uv\cache` within `FOLDERID_LocalAppData`)
  ([uv cache docs](https://docs.astral.sh/uv/concepts/cache/#cache-directory),
  [uv storage reference](https://docs.astral.sh/uv/reference/storage/#cache-directory)).
  If the environment is deleted by `uv cache clean`, uv recreates it automatically on the next
  run ([uv tools concept](https://docs.astral.sh/uv/concepts/tools/#tool-environments)).
- Version resolution. `uvx` resolves the latest available version on first invocation and then
  keeps using the cached version for the same request, until the cache is pruned or refreshed,
  an explicit version is requested (`rohrpost@x.y.z`, `rohrpost@latest`), or the tool has been
  installed with `uv tool install`, in which case the installed version wins
  ([uv tools concept](https://docs.astral.sh/uv/concepts/tools/#tool-versions)).
- First-run behaviour, concretely: a cache miss means resolution, download, and environment
  creation; later invocations reuse the cached environment, which the docs give as the reason it
  exists ("only cached to reduce the overhead of repeated invocations",
  [uv tools concept](https://docs.astral.sh/uv/concepts/tools/#tool-environments)). The uv docs
  publish no latency numbers for either case; measuring first-run vs warm-run cost on Windows
  would need a benchmark the docs do not provide.
- Contrast with `uv tool install rohrpost`: the environment goes to the `tools/` subdirectory of
  the persistent data directory, `%APPDATA%\uv\data\tools` on Windows, and the executables are
  copied to the executable directory, `%USERPROFILE%\.local\bin`, which must be on `PATH`; uv
  warns when it is not and offers `uv tool update-shell`
  ([uv storage reference](https://docs.astral.sh/uv/reference/storage/#tools),
  [executable directory](https://docs.astral.sh/uv/reference/storage/#executable-directory),
  [uv tools concept](https://docs.astral.sh/uv/concepts/tools/#tool-executables)).
- One performance caveat that holds for both paths: the cache directory needs to be on the same
  filesystem as the environments it feeds, or uv falls back from linking to slow copies
  ([uv cache docs](https://docs.astral.sh/uv/concepts/cache/#cache-directory)).

## Install-root convention: `%LOCALAPPDATA%` vs `%APPDATA%` vs `%USERPROFILE%`

Microsoft's own folder definitions, unchanged since the CSIDL era:

- `CSIDL_LOCAL_APPDATA` (`%USERPROFILE%\AppData\Local`) is "the file system directory that
  serves as a data repository for local (nonroaming) applications"
  ([CSIDL](https://learn.microsoft.com/en-us/windows/win32/shell/csidl),
  [KNOWNFOLDERID](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid) maps
  `FOLDERID_LocalAppData` there).
- `CSIDL_APPDATA` (`FOLDERID_RoamingAppData`, `%USERPROFILE%\AppData\Roaming`) is the
  application-data folder whose contents roam with the user in domain environments; related
  per-user folders are described as "it will roam with the user"
  ([CSIDL](https://learn.microsoft.com/en-us/windows/win32/shell/csidl),
  [KNOWNFOLDERID](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid)).
- The profile root itself is off limits: "Applications should not create files or folders at this
  level; they should put their data under the locations referred to by `CSIDL_APPDATA` or
  `CSIDL_LOCAL_APPDATA`"
  ([CSIDL](https://learn.microsoft.com/en-us/windows/win32/shell/csidl)).
- For per-user *programs*, the known folder is `FOLDERID_UserProgramFiles` =
  `%LOCALAPPDATA%\Programs`
  ([KNOWNFOLDERID](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid)); the
  Python install manager puts its global commands under `%LocalAppData%\Python\bin` by default
  ([Python 3.14 docs](https://docs.python.org/3.14/using/windows.html#installation)).

Tooling precedents line up with those definitions:

- uv cache: `%LOCALAPPDATA%\uv\cache`
  ([uv storage reference](https://docs.astral.sh/uv/reference/storage/#cache-directory)).
- pip cache: `%LocalAppData%\pip\Cache`
  ([pip docs](https://pip.pypa.io/en/stable/topics/caching/#default-paths)).
- uv persistent per-user data and config: `%APPDATA%\uv\data` and `%APPDATA%\uv` (roaming side),
  while disposable data sits in `%LOCALAPPDATA%`
  ([uv storage reference](https://docs.astral.sh/uv/reference/storage/#persistent-data-directory)).
- uv's own binaries and installed tool executables go to `%USERPROFILE%\.local\bin` on Windows,
  a Unix-style dotfile in the profile root, one level down rather than loose files at the root
  ([uv storage reference](https://docs.astral.sh/uv/reference/storage/#executable-directory),
  [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/#uninstallation)).

Reading: the platform convention puts non-roaming, machine-local data (caches, per-user program
binaries) under `%LOCALAPPDATA%`, settings that should follow a user across machines under
`%APPDATA%\Roaming`, and nothing at all directly in `%USERPROFILE%`. uv splits along exactly
that line: cache in Local, data/config in Roaming, executables in a PATH directory.

## Gotchas

### WindowsApps `python.exe` execution aliases

App execution aliases are "a special type of reparse point managed by Windows for MSIX packages",
stored in `%LOCALAPPDATA%\Microsoft\WindowsApps`
([Sysinternals: Microsoft Store](https://learn.microsoft.com/en-us/sysinternals/downloads/microsoft-store#app-execution-aliases));
an alias name must end in `.exe`, and when multiple apps register the same alias the last one
registered wins
([MS packaging extensions](https://learn.microsoft.com/en-us/windows/apps/desktop/modernize/desktop-to-uwp-extensions#start-your-application-in-different-ways)).
On machines with no real Python, `python.exe` and `python3.exe` in that directory are the
Store stubs. The Python 3.14 docs' troubleshooting table lists the symptoms: `python` gives
"command not found" or opens the Microsoft Store; the fix is the "Manage app execution aliases"
settings page and making sure `%UserProfile%\AppData\Local\Microsoft\WindowsApps` is on `PATH`,
noting that "the operating system includes this entry once by default, after other user paths"
([Python 3.14 docs](https://docs.python.org/3.14/using/windows.html#pymanager-troubleshoot)).
Practical consequences for a playbook: bare `python` on a fresh Windows box is not a reliable
interpreter, and the WindowsApps directory sits last among user PATH entries so real installs
shadow the aliases. Python 3.14 ships the Python Install Manager as the supported way to get
`python`/`py` commands
([Python 3.14 docs](https://docs.python.org/3.14/using/windows.html#python-install-manager)),
while uv installs its own managed CPython builds into its persistent data directory and puts
their executables in its executable directory
([uv storage reference](https://docs.astral.sh/uv/reference/storage/#python-versions),
[#python-executables](https://docs.astral.sh/uv/reference/storage/#python-executables)).

### MAX_PATH / long paths for deep `.venv` trees

The Windows API caps paths at 260 characters (MAX_PATH) unless an app opts in; extended-length
paths need the `\\?\` prefix and relative paths are always MAX_PATH-limited, and Microsoft gives
"cloning a git repo that has long file names into a folder that itself has a long name" as the
canonical way to hit the limit
([Maximum Path Length Limitation](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)).
Since Windows 10 1607 the cap is removable, but only per app: the registry value
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` must be `1` (or the
"Enable Win32 long paths" group policy set) *and* the application manifest must declare
`longPathAware` ([same Microsoft page](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation#enable-long-paths-in-windows-10-version-1607-and-later)).
Python's Windows docs carry the same instruction and note that after the registry change and a
reboot, `open()`, `os`, and most path functionality accept paths over 260 characters
([Python 3.14 docs, Removing the MAX_PATH limitation](https://docs.python.org/3.14/using/windows.html#removing-the-max-path-limitation)).
A `.venv` is exactly the deep-tree case: a real uv error shows the chain
`C:\Users\<user>\AppData\Local\uv\cache\archive-v0\ez_A-yAgnUYbpxYugeHkX\certifi\py.typed`, and
that is the *short* half of a link that ends in the project's `site-packages`
([uv#7906](https://github.com/astral-sh/uv/issues/7906)). Also relevant: the Windows CWD itself
is capped near MAX_PATH, so long Cygwin working directories silently diverge from the Windows
one ([Cygwin UG](https://cygwin.com/cygwin-ug-net/using.html#pathnames-win32-api)).

### OneDrive interference

Synced folders and uv's hardlink-based install strategy collide, documented in uv's own tracker:

- Installing into a venv inside OneDrive fails with "The cloud operation cannot be performed on
  a file with incompatible hardlinks. (os error 396)" when uv links from
  `%LOCALAPPDATA%\uv\cache` into the project; maintainers closed it as not planned, and affected
  users report that moving or cleaning the cache fixes it
  ([uv#7906](https://github.com/astral-sh/uv/issues/7906)).
- The reverse damage also happens: once a hardlink exists inside a OneDrive-managed venv,
  hardlinking into ordinary folders starts failing, and deleting the OneDrive venv fixed it for
  the reporter ([uv#9500](https://github.com/astral-sh/uv/issues/9500)).
- `uv python install` fails with os error 448 on machines using OneDrive Files On-Demand
  ([uv#19616](https://github.com/astral-sh/uv/issues/19616)).

Microsoft's documentation explains the friction: OneDrive "doesn't support syncing using symbolic
links or junction points" and skips temporary TMP files; Files On-Demand serves files as
placeholders that hydrate on open
([OneDrive restrictions and limitations](https://support.microsoft.com/en-us/onedrive/restrictions-and-limitations-in-onedrive-and-sharepoint)).
Known Folder Move redirects Documents, Desktop, and Pictures into the OneDrive sync root
([OneDrive restrictions and limitations](https://support.microsoft.com/en-us/onedrive/restrictions-and-limitations-in-onedrive-and-sharepoint),
[AutoSave doc](https://support.microsoft.com/en-us/office/collab-files/what-you-should-know-about-autosave)),
so a clone under Documents can be a synced folder without the user choosing anything. Defender's
docs add a performance angle: profiles redirected to OneDrive or network shares make scans
slower because scanning crosses a network hop
([Defender troubleshooting](https://learn.microsoft.com/en-us/defender-endpoint/troubleshoot-performance-issues)).

### Antivirus (Microsoft Defender) interference

Microsoft documents, on its own endpoint-security pages, that real-time protection scans affect
developer workloads:

- Launching an unsigned binary triggers a real-time protection scan, and Defender's
  troubleshooting page offers signing, file-hash indicators, or process+path exclusions as the
  remedies ([Defender troubleshooting](https://learn.microsoft.com/en-us/defender-endpoint/troubleshoot-performance-issues)).
  `rp.exe` trampolines and freshly downloaded wheels are exactly the unsigned-binary class this
  row describes.
- The Defender performance analyzer exists to find "files, file extensions, and processes that
  might be causing performance issues... during antivirus scans"
  ([Performance analyzer](https://learn.microsoft.com/en-us/defender-endpoint/tune-performance-defender-antivirus)).
- Windows offers Dev Drive, a filesystem mode that "reduces the performance impact of Microsoft
  Defender Antivirus scans" by deferring scans until after file-open completes
  ([Dev Drive performance mode](https://learn.microsoft.com/en-us/defender-endpoint/microsoft-defender-endpoint-antivirus-performance-mode)),
  which is Microsoft acknowledging the synchronous-scan cost that dev trees pay.

Worth noting for scoping: these pages establish that scan overhead on dev files is real and
measurable, and give the sanctioned knobs (exclusions, indicators, Dev Drive); they do not
single out Python tooling.
