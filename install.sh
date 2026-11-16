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
#                  a relative path is made absolute before it is used or persisted
#   GBLOG_REPO     base URL to fetch gblog.py from (default: GitHub raw at $GBLOG_REF)
#                  an https:// URL is never redirected to another scheme
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
  # An https source stays https: without --proto-redir, `curl -L` would follow a redirect
  # to plain http (or another scheme). Any other source (file://, a local mirror) is
  # fetched exactly as given.
  case "$REPO_RAW" in
    [Hh][Tt][Tt][Pp][Ss]://*) curl -fsSL --proto-redir '=https' "$REPO_RAW/gblog.py" -o "$tmp" ;;
    *) curl -fsSL "$REPO_RAW/gblog.py" -o "$tmp" ;;
  esac
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
# A relative GBLOG_ROOT would later resolve against whatever directory a shell happens to
# be in, so it is made absolute before it is used or persisted. Missing directories are
# created private (0700), like the ones gblog creates itself. CDPATH is cleared so that
# `cd` cannot land in a same-named directory somewhere else.
(umask 077 && mkdir -p -- "$GBLOG_ROOT")
GBLOG_ROOT="$(CDPATH='' cd -- "$GBLOG_ROOT" && pwd)"
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
failed=0  # set when a startup file could not be written (for example a read-only HOME)

# Lines written without the "# gblog ..." markers (by an older install.sh, or by hand)
# count too: an `export PATH=` line that already names $PREFIX and an `export GBLOG_ROOT=`
# line are never duplicated. Each directory is recognised as written out, as `printf %q`
# writes it, and (below $HOME) in its literal $HOME/... spelling.
path_forms=("$PREFIX" "$(printf %q "$PREFIX")")
root_forms=("$GBLOG_ROOT" "$(printf %q "$GBLOG_ROOT")")
case "$PREFIX" in
  "$HOME"/*) path_forms+=("\$HOME/${PREFIX#"$HOME"/}" "\${HOME}/${PREFIX#"$HOME"/}") ;;
esac
case "$GBLOG_ROOT" in
  "$HOME"/*) root_forms+=("\$HOME/${GBLOG_ROOT#"$HOME"/}" "\${HOME}/${GBLOG_ROOT#"$HOME"/}") ;;
esac

rc_has_prefix_on_path() {  # rc_has_prefix_on_path FILE: does an `export PATH=` line in FILE already name $PREFIX?
  local line form
  [ -f "$1" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      "export PATH="*)
        for form in "${path_forms[@]}"; do
          case "$line" in
            *"$form"*) return 0 ;;
          esac
        done
        ;;
    esac
  done < "$1"
  return 1
}

legacy_root=""
rc_root_value() {  # rc_root_value FILE: value of the last `export GBLOG_ROOT=` line of FILE -> $legacy_root; fails if none
  local line found=1
  legacy_root=""
  [ -f "$1" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      "export GBLOG_ROOT="*)
        legacy_root="${line#"export GBLOG_ROOT="}"
        legacy_root="${legacy_root%"${legacy_root##*[![:space:]]}"}"
        found=0
        ;;
    esac
  done < "$1"
  return "$found"
}

is_this_root() {  # is_this_root VALUE: is VALUE (as written in an rc file) this install's ledger root?
  local value="$1" form
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  for form in "${root_forms[@]}"; do
    if [ "$value" = "$form" ]; then
      return 0
    fi
  done
  return 1
}

persist() {  # persist FILE: append the missing blocks to FILE and say what was done
  local rc="$1" what="" verb="modified"
  [ -e "$rc" ] || verb="created"
  if [ "$need_path" = 1 ] && ! grep -qF '# gblog PATH' "$rc" 2>/dev/null && ! rc_has_prefix_on_path "$rc"; then
    if printf '\n# gblog PATH\n%s\n' "$path_line" >> "$rc"; then
      what="PATH"
    else
      failed=1
      echo "warning: could not write $rc" >&2
      return 0
    fi
  fi
  if grep -qF '# gblog ROOT' "$rc" 2>/dev/null; then
    if ! grep -qxF -- "$root_line" "$rc"; then
      echo "note: $rc already has a '# gblog ROOT' block with a different value; left unchanged" >&2
    fi
  elif rc_root_value "$rc"; then
    # an unmarked line: the same root needs nothing, a different one is the user's to resolve
    if ! is_this_root "$legacy_root"; then
      echo "warning: $rc already has 'export GBLOG_ROOT=$legacy_root' but this install uses $GBLOG_ROOT; that line was left unchanged and no GBLOG_ROOT block was added" >&2
    fi
  elif printf '\n# gblog ROOT\n%s\n' "$root_line" >> "$rc"; then
    what="${what:+$what, }GBLOG_ROOT"
  else
    failed=1
    echo "warning: could not write $rc" >&2
  fi
  if [ -n "$what" ]; then
    echo "$verb $rc ($what)"
    modified=1
  fi
  return 0
}

# ~/.bash_profile is included because bash login shells read it instead of ~/.profile.
found=0
for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile"; do
  if [ -f "$rc" ]; then
    found=1
    persist "$rc"
  fi
done
if [ "$found" = 0 ]; then
  persist "$HOME/.profile"  # no rc file exists yet: create ~/.profile
fi

if [ "$modified" = 0 ] && [ "$failed" = 1 ]; then
  # A read-only HOME or rc file: say so instead of claiming everything is already configured.
  echo "no shell startup file could be written (see the warnings above); nothing was persisted"
  echo "for this shell: $root_line"
elif [ "$modified" = 0 ]; then
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
