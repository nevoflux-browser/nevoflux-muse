# shellcheck shell=sh
# nevoflux-muse install library, sourced by setup.sh, ensure.sh and uninstall.sh.
# The caller sets NF_SRC (the repository checkout). Nothing here touches the state
# directory: secrets are the Python side's business.

NF_ROOT="${NF_INSTALL_ROOT:-$HOME/.local/share/nevoflux-muse}"
NF_UV_DIR="$HOME/.local/share/uv"
NF_LOG="$NF_ROOT/install.log"
NF_LOG_MAX=262144

# shellcheck source=install/uv.lock.sh
. "$NF_SRC/install/uv.lock.sh"

nf_log() {
    mkdir -p "$NF_ROOT"
    if [ -f "$NF_LOG" ] && [ "$(wc -c < "$NF_LOG")" -gt "$NF_LOG_MAX" ]; then
        : > "$NF_LOG"
    fi
    nf_line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
    printf '%s\n' "$nf_line" >> "$NF_LOG"
    printf '%s\n' "$nf_line" >&2
}

# A mkdir lock: atomic everywhere, unlike flock. The holder's pid is published by an atomic
# mv, so a contender never sees a half-made lock; a lock with no pid yet is held until it
# is a minute old. A live holder is never robbed. A dead holder's lock is cleared by an
# atomic rename, which only one contender wins.
nf_lock() {
    mkdir -p "$NF_ROOT" || return 1
    nf_try=0
    while [ "$nf_try" -lt 3 ]; do
        nf_try=$((nf_try + 1))
        if mkdir "$NF_ROOT/.lock" 2>/dev/null; then
            if printf '%s\n' "$$" > "$NF_ROOT/.lock/pid.tmp" \
                && mv "$NF_ROOT/.lock/pid.tmp" "$NF_ROOT/.lock/pid"; then
                return 0
            fi
            rm -rf "$NF_ROOT/.lock"
            return 1
        fi
        [ -d "$NF_ROOT/.lock" ] || continue
        nf_old=$(cat "$NF_ROOT/.lock/pid" 2>/dev/null || true)
        if [ -n "$nf_old" ]; then
            if kill -0 "$nf_old" 2>/dev/null; then
                return 1
            fi
        elif [ -z "$(find "$NF_ROOT/.lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
            return 1
        fi
        nf_stale="$NF_ROOT/.lock.stale.$$"
        rm -rf "$nf_stale"
        if mv "$NF_ROOT/.lock" "$nf_stale" 2>/dev/null; then
            nf_seen=$(cat "$nf_stale/pid" 2>/dev/null || true)
            if [ "$nf_seen" != "$nf_old" ]; then
                # Someone else's fresh lock: put it back.
                mv "$nf_stale" "$NF_ROOT/.lock" 2>/dev/null || rm -rf "$nf_stale"
                return 1
            fi
            nf_log "clearing a stale lock (pid ${nf_old:-unknown})"
            rm -rf "$nf_stale"
        fi
    done
    return 1
}

# Releases the lock only if this process holds it.
nf_unlock() {
    if [ "$(cat "$NF_ROOT/.lock/pid" 2>/dev/null || true)" = "$$" ]; then
        rm -rf "$NF_ROOT/.lock"
    fi
    return 0
}

# Waits up to a minute for the lock; on success releases it on exit and on signals.
nf_lock_wait() {
    nf_waited=0
    until nf_lock; do
        if [ "$nf_waited" -ge 60 ]; then
            return 1
        fi
        sleep 2
        nf_waited=$((nf_waited + 2))
    done
    trap nf_unlock EXIT
    trap 'nf_unlock; exit 130' INT
    trap 'nf_unlock; exit 143' TERM
    trap 'nf_unlock; exit 129' HUP
}

nf_find_python() {
    [ "${NF_TEST_FORCE_UV:-}" = 1 ] && return 1
    for nf_py in python3 python3.13 python3.12 python3.11 python3.10; do
        nf_path=$(command -v "$nf_py" 2>/dev/null) || continue
        if "$nf_path" -c 'import sys, ensurepip, venv; sys.exit(sys.version_info < (3, 10))' \
            2>/dev/null; then
            printf '%s\n' "$nf_path"
            return 0
        fi
    done
    return 1
}

nf_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        shasum -a 256 "$1" | cut -d ' ' -f 1
    fi
}

nf_fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        nf_log "neither curl nor wget is available to download uv"
        return 1
    fi
}

# The fallback: a pinned, checksum-verified uv, and the Python it installs into home.
nf_uv_python() {
    if [ "$(uname -s)" != Linux ]; then
        nf_log "the uv fallback is for Linux; install Python >= 3.10 instead"
        return 1
    fi
    case "$(uname -m)" in
        x86_64 | amd64) nf_arch=x86_64; nf_sum=$NF_UV_SHA256_X86_64 ;;
        aarch64 | arm64) nf_arch=aarch64; nf_sum=$NF_UV_SHA256_AARCH64 ;;
        *) nf_log "no pinned uv build for $(uname -m)"; return 1 ;;
    esac
    nf_uv="$NF_UV_DIR/bin/uv"
    if [ ! -x "$nf_uv" ] \
        || [ "$("$nf_uv" --version 2>/dev/null | cut -d ' ' -f 2)" != "$NF_UV_VERSION" ]; then
        nf_name="uv-$nf_arch-unknown-linux-musl"
        nf_tmp="$NF_UV_DIR/tmp"
        rm -rf "$nf_tmp"
        mkdir -p "$nf_tmp" "$NF_UV_DIR/bin" || return 1
        nf_log "downloading uv $NF_UV_VERSION ($nf_arch)"
        if ! nf_fetch \
            "https://github.com/astral-sh/uv/releases/download/$NF_UV_VERSION/$nf_name.tar.gz" \
            "$nf_tmp/$nf_name.tar.gz"; then
            rm -rf "$nf_tmp"
            return 1
        fi
        if [ "${NF_TEST_CORRUPT_UV_DOWNLOAD:-}" = 1 ]; then
            printf 'x' >> "$nf_tmp/$nf_name.tar.gz"  # test hook: can only make the check fail
        fi
        nf_got=$(nf_sha256 "$nf_tmp/$nf_name.tar.gz")
        if [ "$nf_got" != "$nf_sum" ]; then
            nf_log "the uv download failed its checksum (got $nf_got); refusing to use it"
            rm -rf "$nf_tmp"
            return 1
        fi
        if ! tar -xzf "$nf_tmp/$nf_name.tar.gz" -C "$nf_tmp" \
            || ! mv "$nf_tmp/$nf_name/uv" "$nf_uv" \
            || ! chmod 0755 "$nf_uv"; then
            nf_log "could not unpack uv"
            rm -rf "$nf_tmp"
            return 1
        fi
        rm -rf "$nf_tmp"
    fi
    UV_PYTHON_INSTALL_DIR="$NF_UV_DIR/python" "$nf_uv" python install "$NF_UV_PYTHON" >&2 \
        || return 1
    UV_PYTHON_INSTALL_DIR="$NF_UV_DIR/python" UV_PYTHON_PREFERENCE=only-managed \
        "$nf_uv" python find "$NF_UV_PYTHON"
}

nf_python() {
    if nf_found=$(nf_find_python); then
        nf_log "using the system Python $nf_found"
        printf '%s\n' "$nf_found"
        return 0
    fi
    nf_log "no usable system Python (>= 3.10 with venv); falling back to uv"
    nf_found=$(nf_uv_python) || return 1
    nf_log "using uv's Python $nf_found"
    printf '%s\n' "$nf_found"
}

# Every dependency wheel the lock names, plus a wheel of this project: a heal can then
# reinstall without the network. The build backend comes from install/build.lock (hashed),
# not from an unpinned download. The project wheel is built aside and only swapped in once
# everything succeeded, so a failed refresh never leaves the wheelhouse without one.
# $1: the new venv.
nf_refresh_wheelhouse() {
    nf_rv="$1"
    nf_wh="$NF_ROOT/wheelhouse"
    nf_proj="$NF_ROOT/wheelhouse.new-proj"
    mkdir -p "$nf_wh" || return 1
    rm -rf "$nf_proj"
    mkdir -p "$nf_proj" || return 1
    "$nf_rv/bin/python" -m pip install --disable-pip-version-check --require-hashes \
        -r "$NF_SRC/install/build.lock" >> "$NF_ROOT/pip.out" 2>&1 || return 1
    "$nf_rv/bin/python" -m pip download --disable-pip-version-check --require-hashes \
        --only-binary=:all: -r "$NF_SRC/requirements.lock" -d "$nf_wh" \
        >> "$NF_ROOT/pip.out" 2>&1 || return 1
    "$nf_rv/bin/python" -m pip wheel --disable-pip-version-check --no-build-isolation \
        --no-deps "$NF_SRC" -w "$nf_proj" >> "$NF_ROOT/pip.out" 2>&1 || return 1
    set -- "$nf_proj"/nevoflux_muse-*.whl
    [ -e "$1" ] || return 1
    rm -f "$nf_wh"/nevoflux_muse-*.whl || return 1
    mv "$1" "$nf_wh"/ || return 1
    rm -rf "$nf_proj"
    for nf_whl in "$nf_wh"/*.whl; do
        [ -e "$nf_whl" ] || continue
        nf_base=$(basename "$nf_whl")
        case "$nf_base" in nevoflux_muse-*) continue ;; esac
        nf_pkg=$(printf '%s' "$nf_base" | cut -d - -f 1 | tr '[:upper:]' '[:lower:]' | tr '_' '-')
        nf_ver=$(printf '%s' "$nf_base" | cut -d - -f 2)
        if ! grep -qi "^$nf_pkg==$nf_ver" "$NF_SRC/requirements.lock"; then
            rm -f "$nf_whl"
            nf_log "removed the stale wheel $nf_base"
        fi
    done
}

# Sets NF_ONLINE=1 when the wheelhouse was not enough and pip used the index.
# shellcheck disable=SC2034 # NF_ONLINE is read by ensure.sh
nf_install_deps() {
    nf_vpy="$1/bin/python"
    NF_ONLINE=0
    if ! "$nf_vpy" -m pip install --disable-pip-version-check --require-hashes --no-index \
        --find-links "$NF_ROOT/wheelhouse" -r "$NF_SRC/requirements.lock" \
        >> "$NF_ROOT/pip.out" 2>&1; then
        NF_ONLINE=1
        "$nf_vpy" -m pip install --disable-pip-version-check --require-hashes \
            --find-links "$NF_ROOT/wheelhouse" -r "$NF_SRC/requirements.lock" \
            >> "$NF_ROOT/pip.out" 2>&1 || return 1
    fi
    set -- "$NF_ROOT"/wheelhouse/nevoflux_muse-*.whl
    if [ -e "$1" ]; then
        "$nf_vpy" -m pip install --disable-pip-version-check --no-deps --no-index \
            --find-links "$NF_ROOT/wheelhouse" nevoflux-muse >> "$NF_ROOT/pip.out" 2>&1
    else
        nf_log "no project wheel in the wheelhouse; installing from $NF_SRC"
        "$nf_vpy" -m pip install --disable-pip-version-check --no-deps "$NF_SRC" \
            >> "$NF_ROOT/pip.out" 2>&1
    fi
}

# Builds the venv in place (console scripts hard-code its path, so it cannot be moved),
# with the old one parked aside and put back if anything fails.
# $1: the Python; $2: 1 to refresh the wheelhouse first (setup), 0 to use it as is (heal).
nf_build_venv() {
    nf_v="$NF_ROOT/venv"
    nf_old="$NF_ROOT/venv.old"
    rm -rf "$nf_old" || return 1
    if [ -e "$nf_v" ]; then
        mv "$nf_v" "$nf_old" || return 1
    fi
    if "$1" -m venv "$nf_v" >> "$NF_ROOT/pip.out" 2>&1 \
        && { [ "$2" != 1 ] || nf_refresh_wheelhouse "$nf_v"; } \
        && nf_install_deps "$nf_v"; then
        rm -rf "$nf_old"
        return 0
    fi
    rm -rf "$nf_v" || return 1
    if [ -e "$nf_old" ]; then
        mv "$nf_old" "$nf_v" || return 1
    fi
    return 1
}

nf_healthy() {
    "$NF_ROOT/venv/bin/python" -c 'import nevoflux_muse' >/dev/null 2>&1
}
