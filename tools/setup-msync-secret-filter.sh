#!/usr/bin/env bash
#
# setup-msync-secret-filter.sh — one-time, local-only git filter that keeps
# Pandora credentials out of commits.
#
#   ./tools/setup-msync-secret-filter.sh [--uninstall] [--status]
#
# This makes it safe to type your real Pandora credentials directly into
# config.py: it installs a git "clean" filter (see tools/msync-secrets-clean.py)
# plus a LOCAL .git/info/attributes rule, so every time config.py is staged the
# PANDORA_USERNAME / PANDORA_PASSWORD lines are scrubbed first. Nothing is
# committed, shared, or pushed, and other clones of the repo are unaffected.
#
# After it completes, edit config.py (server machine):
#
#   PANDORA_PASSWORD = "your-password"
#
# and restart the service — the server reads your credentials from the file,
# while `git add config.py` / `git diff --cached` never show them.
#
# Caveat: `git checkout -- config.py` (or `git stash`) restores the SCRUBBED
# version from git, wiping your local credentials — re-type them afterwards.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLEANER="$ROOT/tools/msync-secrets-clean.py"
FILTER="msync-secrets"
INFO_ATTR="$ROOT/.git/info/attributes"
RULE="config.py filter=$FILTER"
CMD_CLEAN="python3 $CLEANER"
CMD_SMUDGE="cat"

die() { echo "error: $*" >&2; exit 1; }

help_text() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

install_filter() {
    [ -f "$CLEANER" ] || die "missing $CLEANER"
    git config "filter.$FILTER.clean" "$CMD_CLEAN"
    git config "filter.$FILTER.smudge" "$CMD_SMUDGE"
    mkdir -p "$ROOT/.git/info"
    if ! grep -qF "$RULE" "$INFO_ATTR" 2>/dev/null; then
        echo "$RULE" >> "$INFO_ATTR"
    fi
    echo "Installed secret-clean filter (this clone only; nothing committed)."
    echo
    echo "Now type your real Pandora credentials into config.py and restart:"
    echo '    PANDORA_USERNAME = "you@example.com"'
    echo '    PANDORA_PASSWORD = "your-password"'
    echo "git add/git commit scrub them automatically."
}

uninstall_filter() {
    git config --unset "filter.$FILTER.clean" 2>/dev/null || true
    git config --unset "filter.$FILTER.smudge" 2>/dev/null || true
    if [ -f "$INFO_ATTR" ]; then
        grep -vF "$RULE" "$INFO_ATTR" > "$INFO_ATTR.tmp" || true
        mv "$INFO_ATTR.tmp" "$INFO_ATTR"
    fi
    echo "Removed secret-clean filter (this clone only)."
    echo "Warning: credentials in config.py are no longer scrubbed on commit."
}

status_filter() {
    if grep -qF "$RULE" "$INFO_ATTR" 2>/dev/null && \
       git config "filter.$FILTER.clean" >/dev/null 2>&1; then
        echo "secret-clean filter is INSTALLED for this clone."
    else
        echo "secret-clean filter is NOT installed — run:"
        echo "    $0"
    fi
}

case "${1:-}" in
    "" ) install_filter ;;
    --uninstall ) uninstall_filter ;;
    --status ) status_filter ;;
    --help|-h ) help_text ;;
    * ) die "unknown option: $1 (try --help)" ;;
esac