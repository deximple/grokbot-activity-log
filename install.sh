#!/usr/bin/env bash
# Install gblog on this machine (meant for a Grok Bot cloud computer, works anywhere with python3 >= 3.8).
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/grokbot-activity-log/main/install.sh | bash
#   # pinned (recommended for unattended installs):
#   curl -fsSL https://raw.githubusercontent.com/<owner>/grokbot-activity-log/<ref>/install.sh \
#     | GBLOG_REF=<ref> GBLOG_SHA256=<sha256 of gblog.py at that ref> bash
#   # or from a checkout:
#   bash ./install.sh
#
# Env overrides:
#   GBLOG_PREFIX   where to put the launcher      (default: ~/.local/bin)
#   GBLOG_ROOT     where the ledger lives         (default: /workspace/activity-log if writable, else ~/activity-log)
#                  a relative path is made absolute before it is used or persisted
#   GBLOG_REPO     base URL to fetch gblog.py from (default: GitHub raw at $GBLOG_REF)
#                  an https:// URL is never redirected to another scheme
#   GBLOG_REF      branch, tag or commit used to build the default URL (default: main; ignored when GBLOG_REPO is set)
#   GBLOG_SHA256   expected sha256 of gblog.py; the install aborts on mismatch and installs nothing
#
# Without a home directory (HOME unset or empty) both GBLOG_PREFIX and GBLOG_ROOT are required and no
# shell startup file is written.

# `sh install.sh` (dash) would die on the next line with "Illegal option -o pipefail".
if [ -z "${BASH_VERSION:-}" ]; then
  echo "install.sh needs bash: run 'bash install.sh'" >&2
  exit 1
fi
set -euo pipefail

tilde='~'

# A HOME that was not in the environment is one bash made up itself (from the passwd database, or "/"
# without an entry). It is treated like an unset one: nothing is written under a guessed directory.
case "$(declare -p HOME 2>/dev/null || true)" in
  "declare -"*x*" HOME="*) ;;
  *) HOME="" ;;
esac
if [ -z "$HOME" ] && { [ -z "${GBLOG_PREFIX:-}" ] || [ -z "${GBLOG_ROOT:-}" ]; }; then
  echo "HOME is unset or empty: set both GBLOG_PREFIX and GBLOG_ROOT; nothing installed" >&2
  exit 1
fi

expand_home() {  # expand_home VALUE: VALUE with a leading ~, $HOME or ${HOME} replaced by the home directory
  local v="$1"
  if [ -n "$HOME" ]; then
    case "$v" in
      "$tilde" | "\$HOME" | "\${HOME}") v="$HOME" ;;
      "$tilde"/*) v="$HOME/${v#??}" ;;
      "\$HOME"/*) v="$HOME/${v#??????}" ;;
      "\${HOME}"/*) v="$HOME/${v#????????}" ;;
    esac
  fi
  printf '%s' "$v"
}

sq() {  # sq STRING: STRING as one word that sh, dash, bash 3.2+, zsh and ksh all read the same way
  # The characters are listed one by one: a range such as A-Z depends on the locale in bash 3.2.
  local s="$1" out="" q="'"
  case "$s" in
    "" | *[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./+:@%,-]*) ;;
    *)
      printf '%s' "$s"
      return 0
      ;;
  esac
  while [ "${s#*"$q"}" != "$s" ]; do
    out="$out${s%%"$q"*}$q\\$q$q"
    s="${s#*"$q"}"
  done
  printf '%s%s%s%s' "$q" "$out" "$s" "$q"
}

unquote() {  # unquote WORD: the value a shell assigns for WORD (one level of '...', "...", $'...' or backslashes)
  local v="$1" q="'" esc
  case "$v" in
    "\$'"*"'")
      v="${v#??}"
      v="${v%?}"
      esc="\\'"
      v=${v//"$esc"/$q}
      v="$(printf '%b' "$v")"
      ;;
    "'"*"'")
      v="${v#?}"
      v="${v%?}"
      esc="'\\''"
      v=${v//"$esc"/$q}
      ;;
    '"'*'"')
      v="${v#?}"
      v="${v%?}"
      ;;
    *) v="${v//\\/}" ;;
  esac
  printf '%s' "$v"
}

# A value set by cron or systemd is not expanded by a shell, so "~/x" is expanded here.
PREFIX="$(expand_home "${GBLOG_PREFIX:-$HOME/.local/bin}")"
REF="${GBLOG_REF:-main}"
REPO_RAW="${GBLOG_REPO:-https://raw.githubusercontent.com/deximple/grokbot-activity-log/$REF}"
REPO_RAW="${REPO_RAW%/}"

# This is a checkout only when the script was started from a file (not piped into bash,
# where BASH_SOURCE[0] is empty) and that file sits next to gblog.py. A stray ./gblog.py
# in the current directory is never trusted.
HERE=""
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ]; then
  # CDPATH is cleared so that a relative path cannot resolve to a same-named directory elsewhere.
  HERE="$(CDPATH='' cd -- "$(dirname "$SELF")" 2>/dev/null && pwd)" || HERE=""
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
if ! pyver="$(python3 -V 2>&1)"; then
  # On macOS without the Command Line Tools /usr/bin/python3 is a stub that only offers to install them.
  if [ "$(uname -s 2>/dev/null || true)" = Darwin ]; then
    echo "python3 did not start: install the Command Line Tools (xcode-select --install) or another Python 3" >&2
  else
    echo "python3 did not start: $pyver" >&2
  fi
  exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
  echo "python 3.8 or newer is required (found: $pyver)" >&2
  exit 1
}
mkdir -p -- "$PREFIX"
PREFIX="$(CDPATH='' cd -- "$PREFIX" && pwd)"

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
new=""
trap 'rm -f "$tmp" ${new:+"$new"}' EXIT

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
# Other Bots may be calling gblog right now. `install` unlinks or truncates its target before it
# copies; a rename shows callers the old file or the new one, never half of one.
new="$PREFIX/.gblog.new.$$"
install -m 0755 "$tmp" "$new"
mv -f "$new" "$PREFIX/gblog"
new=""
echo "installed $PREFIX/gblog from $origin"

if [ -z "${GBLOG_ROOT:-}" ]; then
  if [ -d /workspace ] && [ -w /workspace ]; then GBLOG_ROOT=/workspace/activity-log; else GBLOG_ROOT="$HOME/activity-log"; fi
fi
GBLOG_ROOT="$(expand_home "$GBLOG_ROOT")"
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
# The lines use POSIX single-quote syntax (never `printf %q`, whose $'...' form dash does
# not read); a plain path is written bare, exactly as earlier versions wrote it.
case ":$PATH:" in
  *":$PREFIX:"*) need_path=0 ;;
  *) need_path=1 ;;
esac
path_line="export PATH=$(sq "$PREFIX"):\"\$PATH\""
root_line="export GBLOG_ROOT=$(sq "$GBLOG_ROOT")"
modified=0
failed=0  # set when a startup file could not be written (for example a read-only HOME)

# Lines written without the "# gblog ..." markers (by an older install.sh, or by hand)
# count too: a PATH line that already names $PREFIX and a GBLOG_ROOT line are never
# duplicated. $PREFIX is recognised as written out, as this script quotes it, as an older
# install.sh wrote it (`printf %q`), and (below $HOME) in its $HOME/..., ${HOME}/... and ~/... spellings.
path_forms=("$PREFIX" "$(sq "$PREFIX")" "$(printf %q "$PREFIX")")
if [ -n "$HOME" ]; then
  case "$PREFIX" in
    "$HOME"/*)
      rel="${PREFIX#"$HOME"/}"
      path_forms+=("\$HOME/$rel" "\${HOME}/$rel" "$tilde/$rel")
      ;;
  esac
fi

rc_has_prefix_on_path() {  # rc_has_prefix_on_path FILE: does a PATH line in FILE already name $PREFIX?
  local line form
  [ -f "$1" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      "export PATH="* | "PATH="*)
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
rc_root_value() {  # rc_root_value FILE: rest of the last `[export ]GBLOG_ROOT=` line of FILE -> $legacy_root; fails if none
  local line found=1
  legacy_root=""
  [ -f "$1" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      "export GBLOG_ROOT="*) line="${line#"export GBLOG_ROOT="}" ;;
      "GBLOG_ROOT="*) line="${line#"GBLOG_ROOT="}" ;;
      *) continue ;;
    esac
    legacy_root="${line%"${line##*[![:space:]]}"}"
    found=0
  done < "$1"
  return "$found"
}

is_this_root() {  # is_this_root VALUE: is VALUE (as written in an rc file) this install's ledger root?
  # Tried as written, without a trailing comment, and without a trailing `; export GBLOG_ROOT`.
  local cand
  for cand in "$1" "${1%%[[:space:]]#*}" "${1%%;*}"; do
    cand="${cand%"${cand##*[![:space:]]}"}"
    cand="$(expand_home "$(unquote "$cand")")"
    if [ "$cand" = "$GBLOG_ROOT" ]; then
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
      # another spelling of the same directory (written in another locale, or by `printf %q`) is not a different value
      if ! { rc_root_value "$rc" && is_this_root "$legacy_root"; }; then
        echo "note: $rc already has a '# gblog ROOT' block with a different value; left unchanged" >&2
      fi
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

found=0
if [ -z "$HOME" ]; then
  failed=1
  echo "warning: HOME is unset or empty; no shell startup file was looked at" >&2
else
  # ~/.bash_profile is included because bash login shells read it instead of ~/.profile.
  # zsh reads $ZDOTDIR/.zshrc when ZDOTDIR is set.
  zrc="${ZDOTDIR:-$HOME}/.zshrc"
  for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$zrc" "$HOME/.profile"; do
    if [ -f "$rc" ]; then
      found=1
      persist "$rc"
    fi
  done
  # The login shell's own file must have the blocks: zsh (the macOS default) never reads
  # ~/.profile, and Terminal on macOS starts login shells, where bash skips ~/.bashrc.
  login_shell="${SHELL:-}"
  case "${login_shell##*/}" in
    zsh)
      if [ ! -f "$zrc" ]; then
        persist "$zrc"
      fi
      ;;
    bash)
      if [ "$(uname -s 2>/dev/null || true)" = Darwin ] && [ ! -f "$HOME/.bash_profile" ] && [ ! -f "$HOME/.profile" ]; then
        persist "$HOME/.bash_profile"
      fi
      ;;
  esac
  if [ "$found" = 0 ] && [ "$modified" = 0 ] && [ "$failed" = 0 ]; then
    persist "$HOME/.profile"  # no rc file exists yet: create ~/.profile
  fi
fi

if [ "$modified" = 0 ] && [ "$failed" = 1 ]; then
  # A read-only or missing HOME: say so instead of claiming everything is already configured.
  echo "no shell startup file could be written (see the warnings above); nothing was persisted"
  echo "for this shell: $root_line"
elif [ "$modified" = 0 ]; then
  echo "no shell startup file was modified (already configured)"
else
  echo "open a new shell to pick up the change"
fi
if [ "$need_path" = 1 ]; then
  echo "for this shell: $path_line"
fi

echo "ledger root: $GBLOG_ROOT"
echo "absolute path: $PREFIX/gblog"
echo "(non-interactive shells do not read rc files; call the absolute path there)"
"$PREFIX/gblog" --version
