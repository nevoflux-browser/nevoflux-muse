#!/bin/sh
# Install or upgrade nevoflux-muse into a self-managed venv (distribution design §5).
#   ~/.local/share/nevoflux-muse/src/setup.sh [--dry-run] [--no-service]
# Re-run it after checking out a newer tag to upgrade; it is idempotent.
set -eu
NF_SRC=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=install/lib.sh
. "$NF_SRC/install/lib.sh"

dry=0
service=
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry=1 ;;
        --no-service) service=--no-service ;;
        *) echo "setup.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done
bin="$HOME/.local/bin"

if [ "$dry" = 1 ]; then
    if py=$(nf_find_python); then
        how="the system Python $py"
    else
        how="uv $NF_UV_VERSION (Python $NF_UV_PYTHON), downloaded and checksum-verified"
    fi
    cat <<EOF
setup.sh would:
  use $how
  refresh $NF_ROOT/wheelhouse from $NF_SRC/requirements.lock
  build $NF_ROOT/venv and install nevoflux-muse from $NF_SRC
  link $bin/nf -> $NF_ROOT/venv/bin/nf
  copy ensure.sh to $NF_ROOT/ensure.sh
  record the installed version in $NF_ROOT/installed_tag
  run: nf setup $service
EOF
    exit 0
fi

waited=0
until nf_lock; do
    if [ "$waited" -ge 60 ]; then
        echo "setup.sh: another setup.sh or ensure.sh is running; try again shortly" >&2
        exit 1
    fi
    sleep 2
    waited=$((waited + 2))
done
trap nf_unlock EXIT
: > "$NF_ROOT/pip.out"
nf_log "setup.sh from $NF_SRC"

if [ -e "$bin/nf" ] || [ -L "$bin/nf" ]; then
    if [ "$(readlink "$bin/nf" 2>/dev/null || true)" != "$NF_ROOT/venv/bin/nf" ]; then
        nf_log "$bin/nf exists and is not ours; move it aside and run setup.sh again"
        exit 1
    fi
fi

py=$(nf_python) || { nf_log "no Python to build the venv with"; exit 1; }
if ! nf_build_venv "$py" 1; then
    nf_log "building the venv failed; see $NF_ROOT/pip.out"
    exit 1
fi
mkdir -p "$bin"
[ -L "$bin/nf" ] || ln -s "$NF_ROOT/venv/bin/nf" "$bin/nf"
cp "$NF_SRC/ensure.sh" "$NF_ROOT/ensure.sh"
chmod 0700 "$NF_ROOT/ensure.sh"
printf '%s\n' "$NF_SRC" > "$NF_ROOT/src_path"
git -C "$NF_SRC" describe --tags --always > "$NF_ROOT/installed_tag" 2>/dev/null \
    || echo unknown > "$NF_ROOT/installed_tag"

# shellcheck disable=SC2086 # $service is empty or one word
if out=$("$NF_ROOT/venv/bin/nf" setup $service --json); then
    nf_log "nf setup: $out"
    printf '%s\n' "$out"
else
    nf_log "nf setup failed: $out"
    exit 1
fi
cat <<EOF
Installed nevoflux-muse $(cat "$NF_ROOT/installed_tag") into $NF_ROOT.
Next (INSTALL.md): register the watchdog, which runs every 5 minutes:
  sh "$NF_ROOT/ensure.sh"
then pair:
  printf '%s\n' '<the block from /pair-agent>' | $bin/nf pair --from-stdin
EOF
