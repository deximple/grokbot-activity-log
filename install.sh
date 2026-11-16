#!/usr/bin/env bash
# Install gblog on this machine (meant for a Grok Bot cloud computer, works anywhere with python3 >= 3.8).
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/grokbot-activity-log/main/install.sh | bash
#   # pinned (recommended for unattended installs):
#   curl -fsSL https://raw.githubusercontent.com/<owner>/grokbot-activity-log/<ref>/install.sh \
#     | GBLOG_REF=<ref> GBLOG_SHA256=<sha256 of gblog.py at that ref> bash
#   # or from a checkout:
#   ./install.sh
#
# Env overrides:
#   GBLOG_PREFIX   where to put the launcher      (default: ~/.local/bin)
#   GBLOG_ROOT     where the ledger lives         (default: /workspace/activity-log if writable, else ~/activity-log)
#   GBLOG_REPO     base URL to fetch gblog.py from (default: GitHub raw at $GBLOG_REF)
#   GBLOG_REF      branch, tag or commit used to build the default URL (default: main; ignored when GBLOG_REPO is set)
#   GBLOG_SHA256   expected sha256 of gblog.py; the install aborts on mismatch and installs nothing
set -euo pipefail

PREFIX="${GBLOG_PREFIX:-$HOME/.local/bin}"
REF="${GBLOG_REF:-main}"
REPO_RAW="${GBLOG_REPO:-https://raw.githubusercontent.com/deximple/grokbot-activity-log/$REF}"
REPO_RAW="${REPO_RAW%/}"

# This is a checkout only when the script was started from a file (not piped into bash,
# where BASH_SOURCE[0] is empty) and that file sits next to gblog.py. A stray ./gblog.py
# in the current directory is never trusted.
HERE=""
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ]; then
  HERE="$(cd "$(dirname "$SELF")" 2>/dev/null && pwd)" || HERE=""
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
  echo "python 3.8 or newer is required (found: $(python3 -V 2>&1))" >&2
  exit 1
}
mkdir -p "$PREFIX"
PREFIX="$(cd "$PREFIX" && pwd)"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    return 1
  fi
}

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if [ -n "$HERE" ] && [ -f "$HERE/gblog.py" ] && [ -f "$HERE/install.sh" ]; then
  cp "$HERE/gblog.py" "$tmp"
  origin="local checkout"
else
  command -v curl >/dev/null 2>&1 || { echo "curl is required to download gblog.py" >&2; exit 1; }
  curl -fsSL "$REPO_RAW/gblog.py" -o "$tmp"
  origin="$REPO_RAW"
fi

if [ -n "${GBLOG_SHA256:-}" ]; then
  want="$(printf '%s' "$GBLOG_SHA256" | tr -d '[:space:]' | tr 'ABCDEF' 'abcdef')"
  got="$(sha256_of "$tmp")" || {
    echo "GBLOG_SHA256 is set but neither sha256sum nor shasum is available; nothing installed" >&2
    exit 1
  }
  if [ "$got" != "$want" ]; then
    echo "sha256 mismatch for gblog.py from $origin: expected $want, got $got; nothing installed" >&2
    exit 1
  fi
  echo "sha256 verified: $got"
fi

# Syntax check without leaving a .pyc behind (this is a sanity check, not an integrity check).
python3 -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$tmp" || {
  echo "gblog.py from $origin is not valid Python; nothing installed" >&2
  exit 1
}
install -m 0755 "$tmp" "$PREFIX/gblog"
echo "installed $PREFIX/gblog from $origin"

if [ -z "${GBLOG_ROOT:-}" ]; then
  if [ -d /workspace ] && [ -w /workspace ]; then GBLOG_ROOT=/workspace/activity-log; else GBLOG_ROOT="$HOME/activity-log"; fi
fi
export GBLOG_ROOT
"$PREFIX/gblog" init

# Persist PATH (only when PREFIX is not already on it) and GBLOG_ROOT (always) in the
# shell startup files. Each block has its own marker, so reruns never duplicate a block.
case ":$PATH:" in
  *":$PREFIX:"*) need_path=0 ;;
  *) need_path=1 ;;
esac
path_line="export PATH=$(printf %q "$PREFIX"):\"\$PATH\""
root_line="export GBLOG_ROOT=$(printf %q "$GBLOG_ROOT")"
modified=0

persist() {  # persist FILE: append the missing blocks to FILE and say what was done
  local rc="$1" what="" verb="modified"
  [ -e "$rc" ] || verb="created"
  if [ "$need_path" = 1 ] && ! grep -qF '# gblog PATH' "$rc" 2>/dev/null; then
    if printf '\n# gblog PATH\n%s\n' "$path_line" >> "$rc"; then
      what="PATH"
    else
      echo "warning: could not write $rc" >&2
      return 0
    fi
  fi
  if grep -qF '# gblog ROOT' "$rc" 2>/dev/null; then
    if ! grep -qxF -- "$root_line" "$rc"; then
      echo "note: $rc already has a '# gblog ROOT' block with a different value; left unchanged" >&2
    fi
  elif printf '\n# gblog ROOT\n%s\n' "$root_line" >> "$rc"; then
    what="${what:+$what, }GBLOG_ROOT"
  else
    echo "warning: could not write $rc" >&2
  fi
  if [ -n "$what" ]; then
    echo "$verb $rc ($what)"
    modified=1
  fi
  return 0
}

found=0
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
  if [ -f "$rc" ]; then
    found=1
    persist "$rc"
  fi
done
if [ "$found" = 0 ]; then
  persist "$HOME/.profile"  # no rc file exists yet: create ~/.profile
fi

if [ "$modified" = 0 ]; then
  echo "no shell startup file was modified (already configured)"
else
  echo "open a new shell to pick up the change"
fi
if [ "$need_path" = 1 ]; then
  echo "for this shell: export PATH=\"$PREFIX:\$PATH\""
fi

echo "ledger root: $GBLOG_ROOT"
echo "absolute path: $PREFIX/gblog"
echo "(non-interactive shells do not read rc files; call the absolute path there)"
"$PREFIX/gblog" --version
