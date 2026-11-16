#!/usr/bin/env bash
# routine-daily-audit.sh - nightly ledger audit for a scheduler (cron, a systemd timer).
#
# It is not interactive and never asks a question. Steps, in this order:
#
#   1. gblog verify --all --record     (the one step that re-hashes archived files)
#   2. gblog anchor --skip-archives    (also after a FAIL: anchoring is per chain)
#      then a "Chain heads <UTC date>" block with every full 64-hex head
#   3. gblog report --date yesterday   (UTC day; written to reports/<date>.md)
#   4. gblog sync --remote=<url>       (only when GBLOG_AUDIT_REMOTE is set)
#   5. gblog status, and gblog spool list when an alert is pending
#
# Environment:
#   GBLOG_ROOT            ledger root (default: the same rule as gblog itself)
#   GBLOG_BIN             gblog executable (default: PATH, ~/.local/bin/gblog, ../bin/gblog)
#   GBLOG_AUDIT_REMOTE    git URL of the PRIVATE backup remote. Unset: no sync at all,
#                         nothing is committed and nothing leaves the computer.
#   GBLOG_AUDIT_NO_PUSH   1 = commit locally and set origin, but do not push
#   GBLOG_AUDIT_BOT       if set, the run is wrapped in a task of that Bot
#                         (task-start --source routine ... task-end)
#
# The script never passes --allow-public, --allow-unknown-visibility or --replace-remote,
# and never runs seal, spool replay or state rebuild. Those are the operator's decisions.
#
# Exit status (the gblog exit-code table; when several apply, the first in this list):
#   4  tail mismatch: a step exited 4, or state/<bot>.alert.json is pending
#   1  integrity FAIL: verify or anchor reported a FAIL chain
#   3  sync refused the push (public remote, or visibility not confirmed)
#   2  usage or environment error: ledger missing, gblog not found, a step exited 2
#      (or with an unexpected code)
#   1  sync committed but did not push
#   0  every step succeeded ("!" warnings and PASS lines do not change it)
set -u
set -o pipefail

say() { printf '%s\n' "$*"; }
warn() { printf 'daily-audit: %s\n' "$*" >&2; }

home="${HOME:-}"
remote="${GBLOG_AUDIT_REMOTE:-}"
no_push="${GBLOG_AUDIT_NO_PUSH:-0}"
audit_bot="${GBLOG_AUDIT_BOT:-}"

# -- find gblog. A scheduler's PATH usually lacks ~/.local/bin, so that path is tried too.
here="$(CDPATH='' cd -- "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || here=""
gblog="${GBLOG_BIN:-}"
if [ -z "$gblog" ]; then
  if command -v gblog >/dev/null 2>&1; then
    gblog="gblog"
  elif [ -x "$home/.local/bin/gblog" ]; then
    gblog="$home/.local/bin/gblog"
  elif [ -n "$here" ] && [ -x "$here/../bin/gblog" ]; then
    gblog="$here/../bin/gblog"
  fi
fi
if [ -z "$gblog" ] || ! command -v "$gblog" >/dev/null 2>&1; then
  warn "gblog not found; run install.sh or set GBLOG_BIN to its absolute path"
  exit 2
fi

# -- ledger root: the same default as gblog, exported so every step uses the same one
root="${GBLOG_ROOT:-}"
if [ -z "$root" ]; then
  if [ -d /workspace ] && [ -w /workspace ]; then
    root="/workspace/activity-log"
  else
    root="$home/activity-log"
  fi
fi
# A value set by cron or systemd is not expanded by a shell: "~/x" means $HOME/x here, as it does for gblog.
tilde='~'
if [ -n "$home" ]; then
  case "$root" in
    "$tilde") root="$home" ;;
    "$tilde"/*) root="$home/${root#??}" ;;
  esac
fi
export GBLOG_ROOT="$root"

# gblog creates missing directories on first use. A deleted ledger must not turn into an
# empty, healthy-looking one, so this check comes before any gblog command.
if [ ! -d "$root/logs" ]; then
  warn "ledger missing: $root/logs does not exist; no gblog command was run"
  exit 2
fi

# An unattended run must fail instead of waiting for a credential prompt.
export GIT_TERMINAL_PROMPT="${GIT_TERMINAL_PROMPT:-0}"

tmp="$(mktemp -d)" || {
  warn "mktemp failed"
  exit 2
}
trap 'rm -rf "$tmp"' EXIT

saw_alert=0
saw_fail=0
saw_refused=0
saw_error=0
saw_unpushed=0
codes=""
summary=()
rc=0

explain() {  # explain STEP CODE: what that exit code means for that step
  case "$1:$2" in
    *:0) say "ok" ;;
    verify:1|anchor:1) say "integrity FAIL" ;;
    sync:1) say "committed but NOT pushed" ;;
    sync:3) say "push refused: the remote is public or its visibility is unconfirmed" ;;
    *:2) say "usage or environment error" ;;
    *:4) say "tail mismatch: an append was refused, the event is in spool/" ;;
    *) say "unexpected exit code" ;;
  esac
}

note() {  # note STEP CODE: remember the result of one step
  case "$1:$2" in
    *:0) ;;
    verify:1|anchor:1) saw_fail=1 ;;
    sync:1) saw_unpushed=1 ;;
    sync:3) saw_refused=1 ;;
    *:4) saw_alert=1 ;;
    *) saw_error=1 ;;
  esac
  codes="${codes:+$codes }$1=$2"
  summary+=("$(printf '%-10s exit %-3s %s' "$1" "$2" "$(explain "$1" "$2")")")
}

run() {  # run STEP ARG...: one gblog command; its stdout is shown and kept in $tmp/STEP.out
  local step="$1"
  shift
  say ""
  say "== gblog $step"
  "$gblog" "$@" | tee "$tmp/$step.out"
  rc="${PIPESTATUS[0]}"
  note "$step" "$rc"
}

print_heads() {  # full heads of this anchor run; keep a copy of this block off the computer
  local tag status chain rest head hist found printed
  printed=0
  say ""
  say "Chain heads $(date -u +%Y-%m-%d)"
  while read -r tag status chain rest; do
    [ "$tag" = "ANCHOR" ] || continue
    case "$chain" in
      # listed one by one: a range such as A-Z depends on the locale in bash 3.2 (macOS)
      ""|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-]*) continue ;;
    esac
    found=0
    hist="$root/anchors/$chain.history.jsonl"
    if [ -f "$hist" ]; then
      # The last history line is this run's entry. A head is printed only when the anchor
      # line above shows its first 12 characters, so a stale line is never reported.
      while read -r head; do
        case "$rest" in
          *"${head:0:12}"*)
            say "$chain $status $head"
            found=1
            ;;
        esac
      done < <(tail -n 1 "$hist" | grep -oE '[0-9a-f]{64}')
    fi
    if [ "$found" = 0 ]; then
      say "$chain $status (no head)"
    fi
    printed=$((printed + 1))
  done < "$tmp/anchor.out"
  if [ "$printed" = 0 ]; then
    say "(no chains anchored)"
  fi
}

# -- optional: record the run itself as a routine task
tid=""
if [ -n "$audit_bot" ]; then
  say "== gblog task-start (--source routine)"
  tid="$("$gblog" task-start "--bot=$audit_bot" --source=routine "--task=Daily ledger audit")"
  rc=$?
  note task-start "$rc"
  if [ "$rc" -ne 0 ]; then
    tid=""
  fi
fi

# -- 1. verify. A FAIL does not stop the run: anchors, spool and alert files still need a backup.
run verify verify --all --record
warnings="$(grep -c '^      ! ' "$tmp/verify.out")"
sealed="$(grep -c '^PASS (' "$tmp/verify.out")"

# -- 2. anchor (per chain; one FAIL chain never drops the others). Step 1 has just re-hashed
#    every archived file, so anchor walks the chains without doing that a second time. A chain
#    whose only problem is a damaged archived file is therefore FAIL in step 1 and OK here;
#    the exit status of this script follows step 1.
run anchor anchor --skip-archives
print_heads

# -- 3. report for yesterday (UTC). The Markdown goes to reports/, not to this output.
say ""
say "== gblog report (--date yesterday, UTC; written to reports/)"
"$gblog" report --date yesterday >/dev/null
rc=$?
note report "$rc"

# -- 4. sync, only when a remote was configured
if [ -n "$remote" ]; then
  sync_args=(sync "--remote=$remote" -m "daily ledger sync")
  if [ "$no_push" = "1" ]; then
    sync_args+=(--no-push)
  fi
  run sync "${sync_args[@]}"
else
  say ""
  say "== sync skipped: GBLOG_AUDIT_REMOTE is not set"
fi

# A loop, not `compgen -G`: glob characters in $root itself must not be read as a pattern.
for alert in "$root"/state/*.alert.json; do
  if [ -e "$alert" ]; then
    saw_alert=1
  fi
done

if [ -n "$tid" ]; then
  outcome="ok"
  if [ "$saw_alert" = 1 ] || [ "$saw_fail" = 1 ] || [ "$saw_refused" = 1 ] \
    || [ "$saw_error" = 1 ] || [ "$saw_unpushed" = 1 ]; then
    outcome="fail"
  fi
  "$gblog" task-end "--bot=$audit_bot" "--task-id=$tid" "--outcome=$outcome" "--summary=$codes" >/dev/null
  rc=$?
  note task-end "$rc"
fi

# -- 5. status and pending alerts (both read-only)
run status status
if grep -Eq '^ALERT [^ ]+: tail mismatch since ' "$tmp/status.out"; then
  saw_alert=1
fi
if [ "$saw_alert" = 1 ]; then
  say ""
  say "== gblog spool list (read-only)"
  "$gblog" spool list
fi

say ""
say "== summary $(date -u +%Y-%m-%dT%H:%M:%SZ) ledger $root"
for line in "${summary[@]}"; do
  say "$line"
done
if [ "$warnings" != "0" ]; then
  say "verify printed $warnings warning line(s) starting with '!'; they do not change the exit code, read them above"
fi
if [ "$sealed" != "0" ]; then
  say "verify printed $sealed PASS line(s): a sealed discontinuity, not a clean chain"
fi

if [ "$saw_alert" = 1 ]; then
  say "RESULT: ALERT - tail mismatch. An append was refused and the event is under spool/."
  say "Do not edit logs or state. Replaying the spool or rebuilding state is the operator's decision."
  exit 4
fi
if [ "$saw_fail" = 1 ]; then
  say "RESULT: FAIL - at least one chain did not verify. Do not edit log files."
  exit 1
fi
if [ "$saw_refused" = 1 ]; then
  say "RESULT: REFUSED - gblog sync will not push to a public or unconfirmed remote. The local commit was made."
  exit 3
fi
if [ "$saw_error" = 1 ]; then
  say "RESULT: ERROR - a step ended with a usage or environment error; see its output above."
  exit 2
fi
if [ "$saw_unpushed" = 1 ]; then
  say "RESULT: NOT PUSHED - the ledger was committed locally but not pushed. Never fix this by rewriting history."
  exit 1
fi
say "RESULT: ok"
exit 0
