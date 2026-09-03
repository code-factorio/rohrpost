# Install Rohrpost locally

Use this playbook only after the Rohrpost skill reports that
`scripts/rohrpost` is missing or its installation is incomplete.

On Windows — PowerShell, cmd, or Git Bash — follow
[`playbooks/windows.md`](windows.md) instead of the steps below: it installs
uv, materialises the `.ps1`, `.cmd`, and Git Bash wrappers, and validates them
with the Windows equivalents of the checks here.

## Permission

Ask the user whether to install Rohrpost locally. Explain the two routes and
which one you will take: the **native binary** (download one release file, no
runtime needed) or the **Python reference** (clone the repository, obtain
Python 3.14 through `uv`, create an isolated environment, install
dependencies). Both routes end by creating the local `scripts/rohrpost`
wrapper. Stop if the user declines.

Prefer the native route: it is what the wrapper checks first, it needs no
toolchain, and it produces byte-identical output. Fall back to the Python
route when no release exists for the platform or when the user asks for the
source checkout.

## Location

Use `ROHRPOST_HOME` when it is already set. Otherwise use:

```bash
export ROHRPOST_HOME="${XDG_CACHE_HOME:-$HOME/.cache}/rohrpost"
```

Keep this variable set for the current shell. The generated wrapper also embeds
the resolved path, so later calls remain usable when the environment variable
is not restored.

## Provision: native binary

Releases at `https://github.com/code-factorio/rohrpost/releases` carry one
binary per platform (`rp-linux-x86_64`, `rp-linux-aarch64`,
`rp-macos-universal`, plus the Windows `.exe` files) and a `SHA256SUMS`
manifest. Require `curl` (and `sha256sum` or `shasum`):

```bash
mkdir -p "$ROHRPOST_HOME/bin"
asset="rp-$(uname -s | tr A-Z a-z)-$(uname -m | sed 's/arm64/aarch64/')"   # macOS: rp-macos-universal
base="https://github.com/code-factorio/rohrpost/releases/latest/download"
curl -fsSL "$base/$asset" -o "$ROHRPOST_HOME/bin/rp"
curl -fsSL "$base/SHA256SUMS" -o "$ROHRPOST_HOME/bin/SHA256SUMS"
(cd "$ROHRPOST_HOME/bin" && grep " $asset\$" SHA256SUMS | sed "s|$asset|rp|" | sha256sum -c -)
chmod +x "$ROHRPOST_HOME/bin/rp"
"$ROHRPOST_HOME/bin/rp" --version
```

Record the release tag you installed. If a checksum does not match, delete
the download, stop and report it. With the binary in place, skip to
"Materialize the wrapper": the wrapper runs `$ROHRPOST_HOME/bin/rp` before it
looks for a source checkout.

## Provision: Python reference

The canonical source is `https://github.com/code-factorio/rohrpost.git`.
Require `git`, `curl`, and `uv`. If `uv` is unavailable, install it using the
official method appropriate for the current Bash environment, then refresh the
shell so `uv` is on `PATH`. Do not use a system Python as a substitute for the
project Python.

Clone the source if it is not already present:

```bash
mkdir -p "$ROHRPOST_HOME"
git clone https://github.com/code-factorio/rohrpost.git "$ROHRPOST_HOME/src"
```

Resolve `main` to a concrete commit and keep that hash for this installation:

```bash
cd "$ROHRPOST_HOME/src"
git fetch origin main
rohrpost_revision="$(git rev-parse origin/main)"
git checkout --detach "$rohrpost_revision"
uv python install 3.14
uv sync
```

If Git is unavailable but network access is available, download the `main`
tarball with `curl`, unpack it into `$ROHRPOST_HOME/src`, and record the archive
URL plus the resolved commit from its contents when available. If neither Git
nor the tarball route is available, stop and report the missing prerequisite.

## Materialize the wrapper

From the repository containing this skill, replace the placeholder in
`scripts/rohrpost.template` with the shell-quoted value of `$ROHRPOST_HOME`,
write the result to `scripts/rohrpost`, and make it executable. Do not
hand-edit the generated wrapper.

Validate the generated file before using it:

```bash
bash -n scripts/rohrpost
test -x scripts/rohrpost
```

Finally, verify the installation from the caller's repository:

```bash
scripts/rohrpost doctor --json
```

If any prerequisite, checkout, environment, executable, or validation step
fails, stop and report the exact failure. The wrapper is intentionally a
validator and launcher; it never repairs an installation itself.