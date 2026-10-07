#!/bin/sh
# Uninstall (distribution design §7.3): a wrapper for `nf uninstall`, which revokes the
# sign-in on the server before it removes anything.
set -eu
root="${NF_INSTALL_ROOT:-$HOME/.local/share/nevoflux-muse}"
NF_SRC=$(cat "$root/src_path" 2>/dev/null || printf '%s' "$root/src")
if [ -d "$NF_SRC" ]; then
    # shellcheck source=install/lib.sh
    . "$NF_SRC/install/lib.sh"
    if ! nf_lock_wait; then
        echo "uninstall.sh: another setup.sh or ensure.sh is running; try again shortly" >&2
        exit 1
    fi
    if ! nf_healthy && py=$(nf_python); then
        : > "$NF_ROOT/pip.out"
        nf_build_venv "$py" 0 || true
    fi
    # exec replaces the shell, so release the lock first.
    nf_unlock
    trap - EXIT INT TERM HUP
fi
if [ -x "$root/venv/bin/nf" ]; then
    exec "$root/venv/bin/nf" uninstall "$@"
fi
cat >&2 <<EOF
uninstall.sh: no working nevoflux-muse under $root.
The sign-in must be revoked before anything is deleted, and that needs a working
install: run setup.sh again, then nf uninstall.
EOF
exit 1
