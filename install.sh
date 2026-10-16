#!/usr/bin/env bash
# Install gblog on this machine (meant for a Grok Bot cloud computer, works anywhere with python3).
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/grokbot-activity-log/main/install.sh | bash
#   # or from a checkout:
#   ./install.sh
#
# Env overrides:
#   GBLOG_PREFIX   where to put the launcher      (default: ~/.local/bin)
#   GBLOG_ROOT     where the ledger lives         (default: /workspace/activity-log if writable, else ~/activity-log)
#   GBLOG_REPO     git URL to fetch gblog.py from (default: GitHub main)
set -euo pipefail

PREFIX="${GBLOG_PREFIX:-$HOME/.local/bin}"
REPO_RAW="${GBLOG_REPO:-https://raw.githubusercontent.com/OWNER/grokbot-activity-log/main}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
mkdir -p "$PREFIX"

if [ -n "$HERE" ] && [ -f "$HERE/gblog.py" ]; then
  install -m 0755 "$HERE/gblog.py" "$PREFIX/gblog"
  echo "installed $PREFIX/gblog from local checkout"
else
  tmp="$(mktemp)"
  curl -fsSL "$REPO_RAW/gblog.py" -o "$tmp"
  python3 -m py_compile "$tmp"
  install -m 0755 "$tmp" "$PREFIX/gblog"
  rm -f "$tmp"
  echo "installed $PREFIX/gblog from $REPO_RAW"
fi

if [ -z "${GBLOG_ROOT:-}" ]; then
  if [ -d /workspace ] && [ -w /workspace ]; then GBLOG_ROOT=/workspace/activity-log; else GBLOG_ROOT="$HOME/activity-log"; fi
fi
export GBLOG_ROOT
"$PREFIX/gblog" init

case ":$PATH:" in
  *":$PREFIX:"*) ;;
  *)
    for rc in "$HOME/.bashrc" "$HOME/.profile"; do
      if [ -f "$rc" ] && ! grep -q 'gblog PATH' "$rc"; then
        printf '\n# gblog PATH\nexport PATH="%s:$PATH"\nexport GBLOG_ROOT="%s"\n' "$PREFIX" "$GBLOG_ROOT" >> "$rc"
      fi
    done
    echo "added $PREFIX to PATH in ~/.bashrc / ~/.profile (open a new shell or: export PATH=\"$PREFIX:\$PATH\")"
    ;;
esac

echo "ledger root: $GBLOG_ROOT"
"$PREFIX/gblog" --version
