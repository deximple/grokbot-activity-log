#!/usr/bin/env bash
# agent-task.sh - run one command as one logged gblog task.
#
#   agent-task.sh --bot ops --task "Rebuild the search index" -- ./rebuild-index.sh --full
#
# What is written to the ledger:
#   task_start   the title
#   action       the program NAME only (never its arguments), outcome ok/fail from the
#                exit status, detail "exit N after Ns"
#   task_end     the same outcome, and a one-line summary
#
# What is never written: the command's arguments, its environment, its input and its output.
# The text you pass yourself (--task, --target, --summary) goes through gblog's redaction,
# which is best effort; do not put credentials there.
#
# The command gets GBLOG_TASK=<task id>, so gblog calls it makes itself attach to the task.
# Its stdin, stdout and stderr are untouched; this wrapper writes only to stderr.
#
# Exit status:
#   the command's own status, when all three records were written;
#   125 when the wrapper could not do its job: bad usage, gblog not found, or a gblog call
#       failed. If task-start failed, the command was NOT run. If a later call failed,
#       the command did run; its status is on stderr.
set -u

prog="agent-task"

usage() {
  cat <<'EOF'
usage: agent-task.sh --bot BOT [options] -- COMMAND [ARG...]

Runs COMMAND as one gblog task: task-start, one action record, task-end.

options:
  --bot BOT        Bot name (slug, display name or alias). Default: $GBLOG_BOT
  --task TITLE     task title. Default: "Run <program name>"
  --action KIND    shell (default), browser, connector, file, message, computer_use,
                   delegation, memory, routine or other
  --target TEXT    what the action record names. Default: the program name, never
                   its arguments
  --summary TEXT   text for task-end. Default: "<program name>: exit N after Ns"
  --routine        this run was started by a routine (task-start --source routine)
  -h, --help       show this help

environment:
  GBLOG_BIN        gblog executable (default: PATH, ~/.local/bin/gblog, ../bin/gblog)
  GBLOG_ROOT       ledger root, read by gblog itself

exit status: COMMAND's own status; 125 if the wrapper or a gblog call failed.
EOF
}

die() {
  printf '%s: %s\n' "$prog" "$*" >&2
  exit 125
}

bot="${GBLOG_BOT:-}"
title=""
action="shell"
target=""
summary=""
routine=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bot|--task|--action|--target|--summary)
      [ "$#" -ge 2 ] || die "$1 needs a value (see --help)"
      case "$1" in
        --bot) bot="$2" ;;
        --task) title="$2" ;;
        --action) action="$2" ;;
        --target) target="$2" ;;
        --summary) summary="$2" ;;
      esac
      shift 2
      ;;
    --routine)
      routine=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      # The argument is not echoed: it may be part of a command line that holds a secret.
      die "unexpected argument before --; options come first and the command follows -- (see --help)"
      ;;
  esac
done

[ "$#" -gt 0 ] || die "no command given; put it after -- (see --help)"
[ -n "$bot" ] || die "--bot is required (or set GBLOG_BOT)"
case "$action" in
  shell|browser|connector|file|message|computer_use|delegation|memory|routine|other) ;;
  *) die "--action must be one of: shell browser connector file message computer_use delegation memory routine other" ;;
esac

# The only part of the command line that is ever logged: the program's base name, and
# only when it looks like a plain name. Anything else is logged as the word "command".
name="${1##*/}"
case "$name" in
  ""|*[!A-Za-z0-9._+-]*) name="command" ;;
esac
[ -n "$target" ] || target="$name"
[ -n "$title" ] || title="Run $name"

home="${HOME:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || here=""
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
  die "gblog not found; run install.sh or set GBLOG_BIN to its absolute path. The command was NOT run"
fi

# Every value is passed as --flag=value, so a value that starts with "-" is never read as a flag.
start_args=(task-start "--bot=$bot" "--task=$title")
if [ "$routine" = 1 ]; then
  start_args+=(--source=routine)
fi
tid="$("$gblog" "${start_args[@]}")"
rc=$?
if [ "$rc" -ne 0 ] || [ -z "$tid" ]; then
  die "gblog task-start exited $rc; the command was NOT run"
fi

# Ctrl-C reaches the command as well (same process group). The trap only keeps this
# wrapper alive long enough to record how the command ended.
trap ':' INT
SECONDS=0
GBLOG_TASK="$tid" "$@"
status=$?
elapsed="$SECONDS"
trap - INT

if [ "$status" -eq 0 ]; then
  outcome="ok"
else
  outcome="fail"
fi
detail="exit $status after ${elapsed}s"
[ -n "$summary" ] || summary="$name: $detail"

failed=""
"$gblog" log "--bot=$bot" "--task-id=$tid" "--action=$action" "--target=$target" \
  "--outcome=$outcome" "--detail=$detail" >/dev/null
rc=$?
[ "$rc" -eq 0 ] || failed="log exited $rc"

# task-end is attempted even when log was refused: a refused event is kept in the spool.
"$gblog" task-end "--bot=$bot" "--task-id=$tid" "--outcome=$outcome" "--summary=$summary" >/dev/null
rc=$?
[ "$rc" -eq 0 ] || failed="${failed:+$failed, }task-end exited $rc"

if [ -n "$failed" ]; then
  printf '%s: the command exited %s, but logging failed (gblog %s); read the gblog lines above before retrying\n' \
    "$prog" "$status" "$failed" >&2
  exit 125
fi
printf '%s: task %s closed, outcome %s (exit %s)\n' "$prog" "$tid" "$outcome" "$status" >&2
exit "$status"
