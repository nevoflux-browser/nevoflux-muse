#!/bin/sh
# The watchdog (distribution design §6.2): heal the venv if the system Python moved,
# then make sure the bridge runs. Register it to run every 5 minutes:
#   sh "$HOME/.local/share/nevoflux-muse/ensure.sh"
# It never deletes secrets; on failure it only logs to install.log.
set -eu
root="${NF_INSTALL_ROOT:-$HOME/.local/share/nevoflux-muse}"
NF_SRC=$(cat "$root/src_path" 2>/dev/null || printf '%s' "$root/src")
[ -d "$NF_SRC" ] || exit 0  # uninstalled: nothing to watch
# shellcheck source=install/lib.sh
. "$NF_SRC/install/lib.sh"

if ! nf_lock; then
    nf_log "another setup.sh/ensure.sh holds the lock (pid ${NF_LOCK_HOLDER:-unknown}); skipping"
    exit 0
fi
trap nf_unlock EXIT
trap 'nf_unlock; exit 130' INT
trap 'nf_unlock; exit 143' TERM
trap 'nf_unlock; exit 129' HUP

if ! nf_healthy; then
    nf_log "the venv does not work (did the system Python change?); rebuilding"
    : > "$NF_ROOT/pip.out"
    py=$(nf_python) || { nf_log "no Python to rebuild the venv with"; exit 1; }
    if ! nf_build_venv "$py" 0; then
        nf_log "rebuilding the venv failed; see $NF_ROOT/pip.out"
        exit 1
    fi
    if [ "$NF_ONLINE" = 1 ]; then
        nf_log "online recovery (the wheelhouse did not fit this Python)"
    else
        nf_log "offline recovery (from the wheelhouse)"
    fi
fi

if out=$("$NF_ROOT/venv/bin/nf" ensure --json); then
    nf_log "nf ensure: $out"
    exit 0
fi
nf_log "nf ensure failed: $out"
exit 1
