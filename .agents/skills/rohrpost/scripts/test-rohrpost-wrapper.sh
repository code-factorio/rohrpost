#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
template="$skill_dir/scripts/rohrpost.template"
temp_root="$(mktemp -d)"
trap 'rm -rf "$temp_root"' EXIT

bash -n "$template"
mkdir -p "$temp_root/src/.venv/bin" "$temp_root/caller"
printf '%s\n' '#!/usr/bin/env bash' 'printf "cwd=%s args=%s\\n" "$PWD" "$*"' \
    > "$temp_root/src/.venv/bin/rp"
chmod +x "$temp_root/src/.venv/bin/rp"

sed "s|__ROHRPOST_HOME__|$temp_root|" "$template" > "$temp_root/rohrpost"
chmod +x "$temp_root/rohrpost"

output="$(cd "$temp_root/caller" && "$temp_root/rohrpost" doctor --json)"
expected="cwd=$temp_root/caller args=doctor --json"
test "$output" = "$expected"

printf 'rohrpost wrapper checks passed\n'