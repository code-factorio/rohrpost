#!/usr/bin/env bash
set -euo pipefail

# Self-test for the bash wrapper template. Each candidate layout the wrapper
# may find in the install's .venv is driven end to end, including the Windows
# ones (Scripts/rp, Scripts/rp.exe), so running this script under Git Bash on
# Windows exercises the same resolution path. The PowerShell and cmd wrappers
# are validated by tests/test_windows_wrapper_templates.py and, on a real
# machine, by playbooks/windows.md; this script does not cover them.

skill_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
template="$skill_dir/scripts/rohrpost.template"
temp_root="$(mktemp -d)"
trap 'rm -rf "$temp_root"' EXIT

bash -n "$template"

check_layout() {
    local layout="$1"
    rm -rf "$temp_root/src/.venv"
    mkdir -p "$temp_root/src/.venv/$(dirname -- "$layout")" "$temp_root/caller"
    printf '%s\n' '#!/usr/bin/env bash' 'printf "cwd=%s args=%s\\n" "$PWD" "$*"' \
        > "$temp_root/src/.venv/$layout"
    chmod +x "$temp_root/src/.venv/$layout"

    sed "s|__ROHRPOST_HOME__|$temp_root|" "$template" > "$temp_root/rohrpost"
    chmod +x "$temp_root/rohrpost"

    local output
    output="$(cd "$temp_root/caller" && "$temp_root/rohrpost" doctor --json)"
    test "$output" = "cwd=$temp_root/caller args=doctor --json"
}

for layout in bin/rp Scripts/rp Scripts/rp.exe; do
    check_layout "$layout"
done

# A native rp under <home>/bin wins over any source checkout and needs none.
check_native() {
    rm -rf "$temp_root/src" "$temp_root/bin"
    mkdir -p "$temp_root/bin" "$temp_root/caller"
    printf '%s\n' '#!/usr/bin/env bash' 'printf "native cwd=%s args=%s\\n" "$PWD" "$*"' \
        > "$temp_root/bin/rp"
    chmod +x "$temp_root/bin/rp"
    sed "s|__ROHRPOST_HOME__|$temp_root|" "$template" > "$temp_root/rohrpost"
    chmod +x "$temp_root/rohrpost"
    local output
    output="$(cd "$temp_root/caller" && "$temp_root/rohrpost" doctor --json)"
    test "$output" = "native cwd=$temp_root/caller args=doctor --json"
}
check_native

printf 'rohrpost wrapper checks passed\n'
