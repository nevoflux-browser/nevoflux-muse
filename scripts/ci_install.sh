#!/usr/bin/env bash
# CI only: install from this checkout into scratch homes and exercise the install paths
# (spec §7). Not for users.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)

fresh_home() {
    export HOME="$RUNNER_TEMP/$1"
    rm -rf "$HOME"
    mkdir -p "$HOME"
    export NF_INSTALL_ROOT="$HOME/.local/share/nevoflux-muse"
    export NF_HOME="$HOME/state"
    root=$NF_INSTALL_ROOT
}

echo "== dry run changes nothing"
fresh_home home
sh "$here/setup.sh" --dry-run --no-service
test ! -e "$root"

echo "== a foreign ~/.local/bin/nf stops setup"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\n' > "$HOME/.local/bin/nf"
if sh "$here/setup.sh" --no-service; then echo "setup.sh overwrote a foreign nf"; exit 1; fi
rm "$HOME/.local/bin/nf"

echo "== install"
sh "$here/setup.sh" --no-service
"$HOME/.local/bin/nf" version
test "$(cat "$root/src_path")" = "$here"
test -x "$root/ensure.sh"

echo "== the generated unit passes systemd-analyze"
"$root/venv/bin/python" -c 'from pathlib import Path
from nevoflux_muse import daemon
print(daemon.unit_text(Path("/tmp/nf-state")), end="")' > "$RUNNER_TEMP/nevoflux-muse.service"
systemd-analyze verify "$RUNNER_TEMP/nevoflux-muse.service"

echo "== ensure.sh before pairing"
sh "$root/ensure.sh"
grep -q '"reason": "not_ready"' "$root/install.log"

echo "== ensure.sh heals a broken venv offline"
ln -sf /nonexistent/python3 "$root/venv/bin/python"
sh "$root/ensure.sh"
"$root/venv/bin/python" -c 'import nevoflux_muse'
grep -q "offline recovery" "$root/install.log"

echo "== the uv fallback"
fresh_home home-uv
NF_TEST_FORCE_UV=1 sh "$here/setup.sh" --no-service
"$root/venv/bin/python" -c "import sys, nevoflux_muse
assert sys.base_prefix.startswith('$HOME/.local/share/uv/python'), sys.base_prefix"

echo "== a bad uv checksum is refused"
fresh_home home-bad
if NF_TEST_FORCE_UV=1 NF_TEST_UV_SHA256=$(printf '0%.0s' $(seq 64)) \
    sh "$here/setup.sh" --no-service; then
    echo "setup.sh accepted a bad uv checksum"; exit 1
fi
test ! -e "$HOME/.local/share/uv/bin/uv"

echo "OK"
