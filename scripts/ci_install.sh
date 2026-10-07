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

echo "== a failed refresh keeps the wheelhouse and the install"
if PIP_INDEX_URL=http://127.0.0.1:9/simple PIP_RETRIES=0 PIP_TIMEOUT=2 \
    sh "$here/setup.sh" --no-service; then
    echo "setup.sh succeeded with an unreachable index"; exit 1
fi
ls "$root"/wheelhouse/nevoflux_muse-*.whl
"$HOME/.local/bin/nf" version
"$root/venv/bin/python" -c 'import nevoflux_muse'
test ! -e "$root/venv.old"

echo "== a held lock makes ensure.sh back off, and a dead holder's lock is cleared"
sh -c 'NF_SRC=$1; . "$1/install/lib.sh"; nf_lock && sleep 8' holder "$here" &
holder=$!
for _ in $(seq 50); do [ -s "$root/.lock/pid" ] && break; sleep 0.1; done
test "$(cut -d " " -f 1 < "$root/.lock/pid")" = "$holder"
before=$(wc -l < "$root/install.log")
ln -sf /nonexistent/python3 "$root/venv/bin/python"
sh "$root/ensure.sh"
test "$(wc -l < "$root/install.log")" = "$((before + 1))"
tail -n 1 "$root/install.log" | grep -q "holds the lock (pid $holder); skipping"
test "$(cut -d " " -f 1 < "$root/.lock/pid")" = "$holder"
if "$root/venv/bin/python" -c 'import nevoflux_muse' 2>/dev/null; then
    echo "ensure.sh touched the venv while the lock was held"; exit 1
fi
wait "$holder"
sh "$root/ensure.sh"
grep -q "clearing a stale lock" "$root/install.log"
"$root/venv/bin/python" -c 'import nevoflux_muse'
test ! -e "$root/.lock"

echo "== a lock from a previous boot is stale even when its pid is alive"
mkdir "$root/.lock"
printf '%s 00000000-0000-0000-0000-000000000000\n' "$$" > "$root/.lock/pid"
if [ ! -r /proc/sys/kernel/random/boot_id ]; then echo "no boot_id on this runner"; exit 1; fi
ln -sf /nonexistent/python3 "$root/venv/bin/python"
sh "$root/ensure.sh"
grep -q "clearing a stale lock (pid $$)" "$root/install.log"
"$root/venv/bin/python" -c 'import nevoflux_muse'
test ! -e "$root/.lock"

echo "== the uv fallback"
fresh_home home-uv
NF_TEST_FORCE_UV=1 sh "$here/setup.sh" --no-service
"$root/venv/bin/python" -c "import sys, nevoflux_muse
assert sys.base_prefix.startswith('$HOME/.local/share/uv/python'), sys.base_prefix"

echo "== a bad uv checksum is refused"
fresh_home home-bad
if NF_TEST_FORCE_UV=1 NF_TEST_CORRUPT_UV_DOWNLOAD=1 \
    sh "$here/setup.sh" --no-service; then
    echo "setup.sh accepted a bad uv checksum"; exit 1
fi
test ! -e "$HOME/.local/share/uv/bin/uv"

echo "OK"
