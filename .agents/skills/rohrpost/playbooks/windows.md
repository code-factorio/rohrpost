# Install Rohrpost on Windows

Use this playbook on Windows — in PowerShell, cmd, or Git Bash — when the
Rohrpost skill reports that a wrapper is missing or its installation is
incomplete. It is the Windows branch of `install-local.md`: ask the user for
permission before installing anything and stop if the user declines. The
wrappers are validators and launchers; they never repair an installation.

## Location

Use `ROHRPOST_HOME` when it is already set. Otherwise set nothing: the `.ps1`
and `.cmd` wrappers default the install home to `%LOCALAPPDATA%\rohrpost` at
runtime. If you choose a custom location, keep `ROHRPOST_HOME` set for the
current shell. The Git Bash wrapper embeds the resolved path when it is
materialised, so later calls stay usable when the variable is not restored.

## Provision: native binary (preferred)

Releases at `https://github.com/code-factorio/rohrpost/releases` carry
`rp-windows-x86_64.exe` and `rp-windows-arm64.exe` plus a `SHA256SUMS`
manifest. No Python, no `uv`, no source checkout is needed:

```powershell
$rohrpostHome = if ($env:ROHRPOST_HOME) { $env:ROHRPOST_HOME } else { Join-Path $env:LOCALAPPDATA 'rohrpost' }
$bin = Join-Path $rohrpostHome 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x86_64' }
$asset = "rp-windows-$arch.exe"
$base = 'https://github.com/code-factorio/rohrpost/releases/latest/download'
Invoke-WebRequest "$base/$asset" -OutFile (Join-Path $bin 'rp.exe')
Invoke-WebRequest "$base/SHA256SUMS" -OutFile (Join-Path $bin 'SHA256SUMS')
$expected = (Select-String -Path (Join-Path $bin 'SHA256SUMS') -Pattern " $asset$").Line.Split(' ')[0]
$actual = (Get-FileHash (Join-Path $bin 'rp.exe') -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "checksum mismatch for $asset" }
& (Join-Path $bin 'rp.exe') --version
```

Every wrapper runs `<home>\bin\rp.exe` before it looks for a source
checkout, so continue at "Materialise the wrappers".

## Provision: Python reference

The canonical source is `https://github.com/code-factorio/rohrpost.git`.
Require `git` and `uv`. Do not use a system Python as a substitute for the
project Python — a bare `python` on Windows is often a Microsoft Store alias
that opens the store instead of an interpreter.

Install uv with the official installer (PowerShell):

```powershell
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
```

uv installs to `%USERPROFILE%\.local\bin`; open a new shell, or add it to the
current one:

```powershell
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
```

Clone the source if it is not already present. Keep the path short and outside
OneDrive-synced folders: deep `.venv` trees can hit the 260-character MAX_PATH
limit, and OneDrive breaks uv's hardlink-based installs.

```powershell
$rohrpostHome = if ($env:ROHRPOST_HOME) { $env:ROHRPOST_HOME } else { Join-Path $env:LOCALAPPDATA 'rohrpost' }
New-Item -ItemType Directory -Force -Path $rohrpostHome | Out-Null
git clone https://github.com/code-factorio/rohrpost.git (Join-Path $rohrpostHome 'src')
```

Resolve `main` to a concrete commit and keep that hash for this installation:

```powershell
Set-Location (Join-Path $rohrpostHome 'src')
git fetch origin main
$rohrpostRevision = git rev-parse origin/main
git checkout --detach $rohrpostRevision
uv python install 3.14
uv sync
```

After `uv sync` the console script is `.venv\Scripts\rp.exe` inside the
checkout; the venv needs no activation.

## Materialise the wrappers

From the repository containing this skill:

- `scripts/rohrpost.ps1.template` → copy to `scripts/rohrpost.ps1` (PowerShell)
- `scripts/rohrpost.cmd.template` → copy to `scripts/rohrpost.cmd` (cmd)
- `scripts/rohrpost.template` → `scripts/rohrpost` for Git Bash, with the
  `__ROHRPOST_HOME__` placeholder replaced by the shell-quoted POSIX form of
  the install home (e.g. `/c/Users/<you>/AppData/Local/rohrpost`); do this
  substitution from Git Bash exactly as `install-local.md` describes

```powershell
Copy-Item scripts/rohrpost.ps1.template scripts/rohrpost.ps1
Copy-Item scripts/rohrpost.cmd.template scripts/rohrpost.cmd
```

The `.ps1` and `.cmd` templates carry no placeholder — they resolve
`%LOCALAPPDATA%\rohrpost` at runtime and honour `ROHRPOST_HOME`. Do not
hand-edit any generated wrapper.

## Validate

Windows decides executability by file extension, not a mode bit, so the
`test -x` equivalent is a plain existence check, and the `bash -n` equivalent
is a parse run:

```powershell
Test-Path scripts/rohrpost.ps1 -PathType Leaf
Test-Path scripts/rohrpost.cmd -PathType Leaf

$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path scripts/rohrpost.ps1), [ref]$null, [ref]$errors) | Out-Null
if ($errors) { $errors; exit 1 }
```

cmd has no offline syntax check; the end-to-end call below is its validation.

Finally, verify the installation from the caller's repository — each shell
must reach the same install through its own wrapper:

```powershell
scripts\rohrpost.ps1 doctor --json
```

```bat
scripts\rohrpost.cmd doctor --json
```

```bash
scripts/rohrpost doctor --json
```

If any prerequisite, checkout, environment, executable, or validation step
fails, stop and report the exact failure.

## If PowerShell blocks the wrapper

If PowerShell reports that running scripts is disabled on the system, widen
the policy for the current user once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
