# Examples

Two small bash scripts that use the `gblog` CLI exactly as documented in the main
[README](../README.md). Both are standalone, need only bash and `gblog`, and are covered by
`tests/test_examples.py`.

| File | What it is |
|---|---|
| [`routine-daily-audit.sh`](routine-daily-audit.sh) | A nightly audit a scheduler can run: verify, anchor, report, optional sync |
| [`agent-task.sh`](agent-task.sh) | A wrapper that runs one command as one logged task |

Run them with `bash examples/<name>.sh …`, or `chmod +x` them first.

Both scripts find `gblog` in this order: `$GBLOG_BIN`, `gblog` on `PATH`, `~/.local/bin/gblog`
(where `install.sh` puts it), then `../bin/gblog` of this checkout. Schedulers do not read shell
startup files, so set `GBLOG_BIN` and `GBLOG_ROOT` explicitly there.

## `routine-daily-audit.sh`

The scheduler version of [`docs/routine-daily-audit.md`](../docs/routine-daily-audit.md). It is not
interactive.

```bash
# crontab: 00:10 UTC, so that yesterday's UTC day is complete
CRON_TZ=UTC
10 0 * * * GBLOG_ROOT=/workspace/activity-log GBLOG_AUDIT_REMOTE=git@github.com:you/private-ledger.git bash /workspace/grokbot-activity-log/examples/routine-daily-audit.sh >> "$HOME/gblog-audit.log" 2>&1
```

Steps, in order:

1. **Ledger check.** If `<root>/logs` does not exist, the script prints `ledger missing` and exits 2
   before running any `gblog` command. `gblog` would otherwise create an empty ledger.
2. **`gblog verify --all --record`.** A FAIL does not stop the run, because anchors, spool and alert
   files still need a backup.
3. **`gblog anchor`**, then a `Chain heads <UTC date>` block with one line per chain:
   `<chain> <OK|PASS|FAIL> <full 64-hex head>`. A FAIL chain gets one line per segment head.
4. **`gblog report --date yesterday`.** Days are UTC days. The Markdown is written to
   `reports/<date>.md`; it is not printed.
5. **`gblog sync --remote=<url> -m "daily ledger sync"`**, only when `GBLOG_AUDIT_REMOTE` is set.
   Without it nothing is committed and nothing leaves the computer.
6. **`gblog status`**, and `gblog spool list` when an alert is pending. Both are read-only.
7. A summary with every step's exit code and one `RESULT:` line.

### Environment

| Variable | Meaning |
|---|---|
| `GBLOG_ROOT` | Ledger root. Default: the same rule as `gblog` (`/workspace/activity-log` if `/workspace` is writable, else `~/activity-log`). |
| `GBLOG_BIN` | Path of the `gblog` executable. |
| `GBLOG_AUDIT_REMOTE` | Git URL of the **private** backup remote. Unset means no sync. |
| `GBLOG_AUDIT_NO_PUSH` | `1`: commit locally and set `origin`, but do not push. Useful for a first run. |
| `GBLOG_AUDIT_BOT` | If set, the run is recorded as a task of that Bot: `task-start --source routine --task "Daily ledger audit"` before step 2 and `task-end` after step 5. `--source routine` keeps `gblog tune` from proposing the run as a routine. |

### Exit status

The script uses the exit-code table of the main README. When several apply, it exits with the first
one in this list:

| Exit | Meaning |
|---|---|
| 4 | Tail mismatch: a step exited 4, or a `state/<bot>.alert.json` is pending. The refused events are under `spool/`. |
| 1 | Integrity FAIL: `verify` or `anchor` reported a FAIL chain. |
| 3 | `sync` refused the push: the remote is public, or its visibility could not be confirmed. The local commit was made. |
| 2 | Usage or environment error: ledger missing, `gblog` not found, or a step exited 2 or with an unexpected code. |
| 1 | `sync` committed but did not push. |
| 0 | Every step succeeded. `!` warning lines and `PASS (N approved break(s))` do not change it; the summary counts them. |

The summary lists each step's own exit code, so the two meanings of 1 can be told apart.

### What it deliberately does not do

- It never passes `--allow-public`, `--allow-unknown-visibility` or `--replace-remote`. If `origin`
  already points somewhere other than `GBLOG_AUDIT_REMOTE`, `sync` exits 2 and `origin` stays as it
  is. Moving the backup is a manual, one-time `gblog sync --remote <url> --replace-remote`.
- It never runs `gblog seal`, `gblog spool replay` or `gblog state rebuild`, and it never edits a
  file under the ledger.
- It does not export or archive Bot transcripts. Only a Bot can do that; see
  `docs/routine-daily-audit.md`.
- It does not run the rollback check for you. Keep the `Chain heads` block somewhere the ledger's
  writer cannot change (the scheduler's mail, a log host), and check yesterday's block by hand or
  from your own tooling:

  ```bash
  gblog verify --bot <chain> --contains <yesterday's 64-hex head>
  ```

  `expected hash … not found in chain` means the chain was rolled back or rewritten.

`GBLOG_AUDIT_REMOTE` is an operator setting. Set it in the scheduler's configuration; do not let a
Bot choose it. The script never prints the URL itself. Use a remote without credentials in the URL
(a credential helper or an ssh key), because git stores the URL in `.git/config` as written.

## `agent-task.sh`

Runs one command as one complete task: `task-start`, one `action` record, `task-end`.

```bash
bash examples/agent-task.sh --bot ops --task "Rebuild the search index" -- ./rebuild-index.sh --full
bash examples/agent-task.sh --bot ops --routine --task "Nightly export" --summary "export finished" -- ./export.sh
```

| Option | Meaning |
|---|---|
| `--bot BOT` | Bot name (slug, display name or alias). Default: `$GBLOG_BOT`. |
| `--task TITLE` | Task title. Default: `Run <program name>`. |
| `--action KIND` | `shell` (default) or another action kind accepted by `gblog log`. |
| `--target TEXT` | Text for the action record. Default: the program name. |
| `--summary TEXT` | Text for `task-end --summary`. Default: `<program name>: exit N after Ns`. |
| `--routine` | Adds `--source routine` to `task-start`. |

Options come first; the command follows `--`. To set environment variables for the command, use
`-- env NAME=value command …`.

### What is logged, and what is not

- **Logged:** the task title; the program's base name (for example `rebuild-index.sh`); the outcome
  (`ok` for exit status 0, `fail` otherwise); `exit N after Ns`; the summary.
- **Never logged:** the command's arguments, its environment, its input and its output. A program
  name that is not a plain name (it contains a space, `=` or similar) is logged as the word
  `command`.
- The text you pass yourself (`--task`, `--target`, `--summary`) goes through gblog's redaction.
  That is best-effort pattern matching, so do not put credentials there.

Arguments are passed to the command unchanged, and every value is handed to `gblog` as
`--flag=value`, so titles and targets that start with `-` or contain quotes, `$( … )` or `;` are
stored literally and never executed.

The command gets `GBLOG_TASK=<task id>` in its environment. Its stdin, stdout and stderr are
untouched; the wrapper prints one line on stderr when the task is closed.

### Exit status

| Exit | Meaning |
|---|---|
| the command's status | All three records were written. |
| 125 | The wrapper could not do its job: bad usage, `gblog` not found, or a `gblog` call failed. If `task-start` failed (for example exit 4, tail mismatch), **the command was not run**. If `log` or `task-end` failed, the command did run and its status is on stderr; read the `gblog` lines above it before retrying, so that nothing is logged twice. |

A command that itself exits 125 cannot be told apart from a wrapper failure by the exit code alone;
the stderr line can.

## Tests

```bash
python -m unittest discover -s tests -p 'test_examples.py' -v
```

The tests run both scripts against a temporary `GBLOG_ROOT`. They use no network: the only push
goes to a local bare repository, and the exit-code mapping is checked with a stand-in for `gblog`.
They are skipped when bash is missing; the `shellcheck` test is skipped when `shellcheck` is not
installed.
