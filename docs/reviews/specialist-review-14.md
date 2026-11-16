# Specialist review K14 — 2026-11-16

**Area.** CLI help and argument UX: accuracy and completeness of the argparse help for every subcommand and flag, flag naming, mutually exclusive options, defaults, examples in `--help`, and parity with the README command reference.

**Method.** I read `gblog.py` (`_add_common`, `_build_parser`, `_add_root_everywhere`, `_validate_args`, `_main`) against README, `docs/agent-integration.md`, `docs/faq.md` and both skills. Nothing was executed. Every reproduction below is derived from the code and should be run once before fixing.

**Compatibility rule used for the fixes.** No flag is removed or renamed. An exit code changes only where the command today does something other than what was asked (K14-01, K14-02). Everything else is help text, one stderr line, or an additive flag.

**Merge note.** All fixes are confined to `_add_common`, `_build_parser`, `_validate_args`, a few lines at the top of the `archive` and `tune` branches of `_main`, one line in `capture_files`, and one new test module. Land them after the K9/K10 work that is in progress in `gblog.py`.

## Summary

| Id | Severity | Finding |
|---|---|---|
| K14-01 | High | `archive --file F --stdin` ignores the file and archives stdin, often 0 bytes, with exit 0 |
| K14-02 | High | `tune --apply ""` runs the analysis instead of recording the decision; `--stdout`/`--json`/`--bot` are ignored with `--apply`/`--reject` |
| K14-03 | High/Medium | Operator-only flags can be abbreviated (`archive --no` is `--no-redact`), which defeats allow-lists written against the full names |
| K14-04 | Medium | An explicitly empty value (`--bot ""`, `--since ""`, `--remote ""`) is treated as "flag absent" without a word |
| K14-05 | Medium | `--task` given where `--task-id` was meant goes unnoticed on `log`/`approval`/`note`; `note` without `--detail` writes an empty record |
| K14-06 | Medium | Twelve help strings say something the code does not do |
| K14-07 | Medium | No subcommand describes itself or shows an example; 40 of 76 arguments have no help; no default is shown |
| K14-08 | Medium | `--bot` has six meanings; the help states one and gets another wrong |
| K14-09 | Medium | `--help` marks nothing as operator-only and lists no exit codes or environment variables |
| K14-10 | Low | Flags that have no effect in the given combination are accepted silently |
| K14-11 | Low | `capture files --exclude` takes exact names only; `capture shell-hook --rc ~/.zshrc` reports success for a hook zsh never runs |
| K14-12 | Low | Case and hyphen normalisation covers three of the seven choice flags |
| K14-13 | Low | Three usage messages contradict what is accepted |
| K14-14 | Low | Parity gaps between the parser and the documents |
| K14-15 | Low | Naming differences worth a help cross-reference, not a rename |
| K14-16 | n/a | One test module that pins all of the above |

---

## K14-01 — `archive --file F --stdin` archives stdin and ignores the file (High)

**Where.** `_build_parser` (archive) and the `archive` branch of `_main`.

```python
s.add_argument("--file", help="path to archive; use --stdin to read standard input")
s.add_argument("--stdin", action="store_true")
...
if args.stdin:
    data = sys.stdin.buffer.read()
    name = args.name or "stdin"
elif args.file:
```

**What happens.** The two sources are not exclusive and `--stdin` wins. In a non-interactive tool shell stdin is empty, so the ledger chains a 0-byte file named `stdin`. The Bot believes it archived `report.csv`.

**Reproduce.**

```bash
export GBLOG_ROOT=$(mktemp -d); printf 'evidence\n' > report.csv
gblog archive --bot ops --kind artifact --file report.csv --stdin < /dev/null; echo "exit $?"
# <hash> #1 archive archive/ops/<day>/e3b0c442….gz sha256=e3b0c44298fc 0 bytes
# exit 0
gblog tail -n 1        # … ops #1 archive stdin
```

**Fix.**

1. Put both flags in a mutually exclusive group that is not required. The "neither" case then keeps today's message `archive: give --file PATH or --stdin`, which `test_archive_missing_file_exit_2` pins.

   ```python
   src = s.add_mutually_exclusive_group()
   src.add_argument("--file", metavar="PATH", help="path of the file to archive")
   src.add_argument("--stdin", action="store_true",
                    help="read the content from standard input (recorded name: --name, default 'stdin')")
   ```

   Both given is then `gblog archive: error: argument --stdin: not allowed with argument --file`, exit 2, nothing written.
2. Warn about empty content. The record is still written.

   ```python
   if not data:
       sys.stderr.write("warning: the content to archive is empty (0 bytes)%s\n"
                        % ("; was standard input connected?" if args.stdin else ""))
   ```

This overlaps the open item "`archive` argument edges (D6-13)" in the CHANGELOG, which gives no detail. The CHANGELOG needs a "Behaviour changes" line because `--file X --stdin` moves from exit 0 to exit 2.

**Tests.**

- `--file F --stdin` and `--stdin --file F`: exit 2, `not allowed with argument` on stderr, stdout empty, ledger root not created.
- `--stdin` with empty input: exit 0, the warning, record has `bytes: 0`.
- `--file` on an empty file: the warning without the stdin hint.
- `archive --bot ops` still prints `archive: give --file PATH or --stdin`.
- `archive --std --name piped` (the documented abbreviation) still works.

---

## K14-02 — `tune` has two modes behind one flat flag set (High)

**Where.** `_build_parser` (tune) and the `tune` branch of `_main`.

```python
if args.cmd == "tune":
    if args.apply or args.reject:        # truthiness: --apply "" is "not given"
        rec = tune_apply(ledger, args.apply or args.reject, args.detail, reject=bool(args.reject))
```

**What happens.**

- **Empty id.** `gblog tune --apply "$PID" --detail "…"` with `PID` unset runs the full analysis. It prints the proposals, rewrites `tuning/<date>.md` and `tuning/proposals.json`, and exits 0. No `tune` record is written. The Bot reports "recorded".
- **`--stdout` is not a dry run.** The help says `--stdout  print only, do not write tuning/`. With `--apply ID` it is ignored and a record is appended to the ledger.
- **`--json` with `--apply`** prints the text line, not JSON.
- **`--bot B` with `--apply ID`** is ignored. The record goes to the proposal's Bot even when `B` is another Bot.
- **`--detail` without `--apply`/`--reject`** is dropped silently.

**Reproduce.**

```bash
unset PID
gblog tune --apply "$PID" --detail "added re-auth step"; echo "exit $?"   # Markdown report, exit 0
gblog tail -n 1                                                            # no tune record
gblog tune --apply <valid id> --stdout                                     # a tune record is appended
```

**Fix.** In `_validate_args`, before anything is written:

```python
if args.cmd == "tune":
    flag = next((f for f in ("apply", "reject") if getattr(args, f) is not None), None)
    if flag:
        if not getattr(args, flag).strip():
            raise _UsageError("--%s needs a proposal id (the value is empty; is the shell variable set?)" % flag)
        if args.stdout or args.json:
            raise _UsageError("--%s records a decision in the ledger; --stdout and --json belong to the "
                              "analysis and cannot be combined with it" % flag)
    elif args.detail:
        sys.stderr.write("notice: --detail is stored only with --apply or --reject; ignored\n")
```

- Use `is not None` in `_main` as well, including the `deciding` computation.
- Give `tune_apply` an optional `only_bot`. When `--bot` is given and `ledger.canonical(only_bot) != ledger.canonical(p["bot"])`, raise `ValueError("proposal %s belongs to %s, not to %s")`. The existing handler prints it and exits 2.
- Reword the help:
  - `--stdout`: "analysis only: print, do not write tuning/ (not a dry run for --apply/--reject)".
  - `--json`: "print the analysis as JSON (tuning/ is still written unless --stdout)".
  - `--apply`/`--reject`: add "one id per call; writes a tune record to the proposal's Bot chain".

No existing test combines a decision with `--stdout` or `--json`.

**Tests.**

- `--apply ""` and `--reject ""`: exit 2, nothing under `tuning/`, no record.
- `--apply ID --stdout` and `--reject ID --json`: exit 2, no record.
- `--apply ID --bot other`: exit 2, no record. `--apply ID --bot <its own Bot or alias>`: exit 0.
- `tune --detail x` alone: exit 0 and the notice.
- `--apply X --reject Y` still prints `not allowed with`.

---

## K14-03 — Operator-only flags can be abbreviated (High/Medium)

**Where.** `build_parser` (argparse default `allow_abbrev=True`); README setup step 6; `SECURITY.md` "CLI-only adversary".

**What happens.** The README says never to auto-allow `archive --no-redact`, `sync --remote`, `--replace-remote`, `--allow-*` or `capture shell-hook --install`. argparse accepts any unique prefix, so these spellings parse today:

| Command | Flag | Shortest accepted spelling |
|---|---|---|
| `archive` | `--no-redact` | `--no` |
| `sync` | `--replace-remote` | `--rep` |
| `sync` | `--remote` | `--rem` (documented as "still works") |
| `sync` | `--allow-public` | `--allow-p` (documented as "still works") |
| `sync` | `--allow-unknown-visibility` | `--allow-u` |
| `capture shell-hook` | `--install` | `--i` |
| `alias add` | `--move` | `--m` |
| `seal` | `--jay-approval-id` | `--j` |

An Auto-review rule or a grep written against the full names does not match the abbreviations. How strongly this bites depends on how Auto-review matches rule text, which I cannot determine from the repo. The README itself tells readers that `--rem` and `--allow-p` work.

**Reproduce.**

```bash
gblog archive --bot ops --file transcripts/x.jsonl --no     # stored verbatim; record carries scanned: false
gblog sync --rep --rem "$URL"                               # replaces origin
```

**Fix.** Variant A is compatible and can go in now. Variant B needs a maintainer decision.

- **A. Say what was understood.** After `parse_args`, compare each `--token` of argv with the option strings of the leaf parser. A token that is not an option string was accepted as a prefix.

  ```python
  def _abbreviations(parser, leaf, argv):
      known = set(parser._option_string_actions) | set(leaf._option_string_actions)
      for tok in argv:
          if tok == "--":
              break
          name = tok.split("=", 1)[0]
          if name.startswith("--") and name not in known:
              full = [o for o in leaf._option_string_actions if o.startswith(name)]
              if len(full) == 1:
                  yield name, full[0]
  ```

  Print `notice: --no was read as --no-redact` on stderr for each hit. Extend README step 6: "a rule must also cover abbreviated spellings: anything starting with `--rem`, `--rep`, `--allow`, `--no` (on `archive`) or `--i` (on `capture shell-hook`)".
- **B. Refuse abbreviations of operator-only flags** with exit 2 and `spell --no-redact out`. This contradicts the "still works" column for `--rem` and `--allow-p` and `AbbreviationTest.test_abbreviations_sync_forms_still_accepted`. A middle path is to refuse only the flags the table never promised: `--no-redact`, `--replace-remote`, `--allow-unknown-visibility`, `--install`, `--move`, `--jay-approval-id`.

**Adjacent observation, outside K14 (for the `sync` owner).** A refused push writes a `denied` sync record. A push that goes through because of `--allow-public` or `--allow-unknown-visibility` writes no record that the override was used; `sync()` goes straight from the visibility check to `git push`.

**Tests.**

- A: `archive … --no` exits 0 and stderr contains `--no was read as --no-redact`. `verify --rec` prints the notice for `--record`. A fully spelled command prints none.
- A: `--target=--force` is not reported.
- B, if chosen: `archive --no`, `sync --rep`, `capture shell-hook --i` exit 2 and write nothing; `--rem` and `--allow-p` behave as decided.

---

## K14-04 — An explicitly empty value is treated as "flag absent" (Medium)

**Where.** `_main` and `_validate_args` test truthiness (`if args.bot`, `if args.since`, `remote.strip() or None`).

**What happens.** An unset shell variable in a routine widens the scope with exit 0 and no stderr line. K1-03 added a warning for a `--bot` that selects nothing; the empty string takes the other branch (`if name and …`).

| Command line | What runs |
|---|---|
| `report --bot "" --stdout`, `status`/`tail`/`export`/`coverage`/`tune --bot ""` | every Bot |
| `anchor --bot ""` | every chain is anchored |
| `spool list --bot ""` | every chain |
| `report --since ""` | today's report |
| `export --since ""` | the whole ledger |
| `sync --remote "" --no-push` | commits, sets no origin, exit 0 |

Write commands already exit 2 for `--bot ""`. `--task-id ""` is a documented fallback to `$GBLOG_TASK` and must stay.

**Reproduce.** `unset BOT; gblog report --bot "$BOT" --stdout | head -3` shows all Bots.

**Fix.** A notice in `_validate_args`; exit code and stdout are unchanged:

```python
_READ_CMDS = ("verify", "anchor", "report", "status", "tail", "export", "coverage", "tune", "spool")
for name in ("bot", "since", "date", "remote", "branch"):
    value = getattr(args, name, None)
    if isinstance(value, str) and not value.strip() and (name != "bot" or args.cmd in _READ_CMDS):
        sys.stderr.write("notice: --%s was given an empty value and is ignored (is the shell variable set?)\n" % name)
```

Add the line to the stderr table in `docs/agent-integration.md` section 6. Making these exit 2 is a candidate for the next minor.

**Tests.**

- Each row of the table: exit code and stdout equal to the run without the flag, stderr has exactly one notice.
- `note --bot ""` is still exit 2 with `bot name must contain letters or digits` and no notice.
- `log --task-id ""` prints no notice.

---

## K14-05 — `--task` for `--task-id`, and empty records (Medium)

**Where.** `_add_common` and the `log`/`approval`/`note` branches of `_main`.

```python
p.add_argument("--task-id", help="task id from `task-start`")
p.add_argument("--task", help="task title")
```

**What happens.**

- `gblog log --bot ops --task "$TID" --action shell --target ls` stores the id as a title and no `task_id`. The report lists it under `(no task)`. `task-end` warns in this case; `log`, `approval` and `note` do not, because `_warn_task` only runs when an id is present.
- `gblog note --bot ops` writes a note with no text. `gblog log --bot ops` writes an action with no target and no detail. Both exit 0 without a warning. `task-start` without `--task` does warn (K1-06).

**Fix.** Warnings in `_main`, placed after the `$GBLOG_TASK` fallback; the record is written either way:

```python
if args.cmd in ("log", "approval", "note") and args.task and not (args.task_id or os.environ.get("GBLOG_TASK")):
    sys.stderr.write("warning: --task is the task title; without --task-id (or $GBLOG_TASK) this record "
                     "belongs to no task\n")
if args.cmd == "note" and not (args.detail or "").strip():
    sys.stderr.write("warning: note without --detail: the note is empty\n")
if args.cmd == "log" and args.type == "action" and not args.target and not args.detail:
    sys.stderr.write("warning: log without --target and --detail: the action names nothing\n")
```

Make the help differ per command: `task-start` gets "choose the task id yourself (default: 10 random hex characters, printed on stdout)"; the others get "task id printed by task-start (default: $GBLOG_TASK)" and "task title (normally given once, on task-start)".

**Tests.**

- `log --task abc` with no id and no env: warning. With `GBLOG_TASK` set: none. `log --task T --task-id X`: none.
- `note --bot ops`: exit 0 and warning. `note --detail x`: none.
- The shell-hook call shape (`--target=… --source auto --task-id ""`) prints none of the three.

---

## K14-06 — Help strings that say something the code does not do (Medium)

| # | Flag or command | Help today | What the code does | Proposed text |
|---|---|---|---|---|
| a | `anchor --bot` | "one chain (still not a whole-ledger skip)" | `chains_on_disk(args.bot)`: every chain of the Bot (`test_anchor_display_name_anchors_every_variant_chain`) | "only this Bot (slug, display name or alias): every chain of it" |
| b | `spool replay` | "…of one Bot…" | `chain_name(args.bot)`: one chain (K1-05) | "append the pending spooled events of one chain in order and clear its alert (operator only)" |
| c | `capture shell-hook` | "a bash hook that logs every command" | interactive bash only; skips plain `gblog …`, a repeated command, and the first prompt | "print (or --install) the hook that logs commands typed at an interactive bash prompt" |
| d | `shell-hook --install` | "append the hook to --rc instead of printing it" | also replaces an older hook block in place | "write the hook into --rc: append it, or replace an older gblog hook block (operator only)" |
| e | `capture browser`, `--history` | "Chrome/Chromium"; "(default: auto-detect)" | also Firefox `places.sqlite`, Brave, Edge, Vivaldi; `--history` replaces auto-detection and is repeatable | "import new visits from browser history (Chromium family and Firefox)"; "history database to read instead of auto-detection (repeatable)" |
| f | `verify --strict` | names 3 conditions | 10 conditions (README table, `_layout_findings`) | "also FAIL on every layout '!' warning (bot field, file names, a record in another day's file, symlinks, non-string or non-finite values, duplicate keys, an archive record that names no file) and on a line or ts that is not in canonical form" |
| g | `--ts` | "override timestamp (ISO 8601 UTC); default now" | offsets are accepted and converted; refused when earlier than the last record or more than 300 s ahead | "record timestamp such as 2026-11-16T09:30:00Z (an offset is converted to UTC); refused when earlier than the Bot's last record or more than 300 s ahead; default: now (operator only)" |
| h | `--detail`, `archive --no-redact` | "secrets are redacted" | best-effort pattern matching | "free text; known secret shapes are redacted (best effort); --summary is the same flag" |
| i | `report --since`, `export --since` | "YYYY-MM-DD; …" / none | `today` and `yesterday` are accepted (K1-13) | "YYYY-MM-DD, today or yesterday (UTC): everything from that day on" |
| j | `archive --name` | "(default: file basename)" | `stdin` with `--stdin` | "(default: the file's base name, or 'stdin')" |
| k | `state rebuild` | "rewrite state from the log tail…" | also appends one `note` record | add "; appends one auto note (operator only)" |
| l | `verify`, `--all` | "check hash chains"; no help | no flag means every chain | "check every chain (the default), or the chains of one Bot" |

Do not use `%` in any new help string; argparse formats help with `%`.

**Tests.** For each row, `assertIn` of a distinguishing phrase in the action's `help`. For (f), each of `symlink`, `duplicate`, `day` must appear.

---

## K14-07 — No descriptions, no examples, 40 arguments without help, no defaults (Medium)

**Where.** `_build_parser`. `sub.add_parser(name, help=…)` feeds only the parent's listing; none of the 32 subcommand parsers has `description` or `epilog`.

**What it looks like today.**

```
$ gblog tail --help
usage: gblog tail [-h] [--bot BOT] [-n N] [--root ROOT]

options:
  -h, --help   show this help message and exit
  --bot BOT
  -n N
  --root ROOT  ledger root (same as the global --root)
```

- By my count, 40 of the 76 command-specific arguments have no help string.
- No default is shown anywhere.
- The only example in the CLI is inside the `alias add` summary, which appears in `gblog alias --help` and not in `gblog alias add --help`.

**Fix.** One helper and explicit text. Do not use `ArgumentDefaultsHelpFormatter`; it would print `(default: None)` and `(default: False)`.

```python
def _command(sub, name, summary, details="", examples=()):
    text = summary[:1].upper() + summary[1:] + ("" if summary.endswith(".") else ".")
    if details:
        text += "\n\n" + details
    epilog = ("examples:\n" + "\n".join("  " + e for e in examples)) if examples else None
    return sub.add_parser(name, help=summary, description=text, epilog=epilog,
                          formatter_class=argparse.RawDescriptionHelpFormatter)
```

Proposed text for every argument that has none (rows marked † are already covered by K14-06):

| Command | Argument | Help |
|---|---|---|
| `log` | `--type` | "record type (default: action); for compatibility only: use task-start, approval, note, task-end and archive for those types" |
| `log`, `task-end` | `--outcome` | "result (default: ok); synonyms are stored canonically: success/done/passed -> ok, failed/error -> fail, cancelled/skip -> skipped, blocked -> denied" |
| `log` | `--action` (has help) | add "(default: other); letter case is ignored and - is read as _" |
| `approval` | `--decision` | "what was decided; approved/granted/yes -> allowed, rejected/declined/no -> denied, timeout -> expired, automatic -> auto" |
| `verify` | `--bot` | "only this Bot (slug, display name or alias): every chain of it" |
| `verify` | `--all` | † |
| `seal` | `--bot` | "must be kim-biseo, the one chain that can be sealed" |
| `report` | `--bot` | "only this Bot (slug, display name or alias)" |
| `status` | `--json` | "print JSON (root, bots, orphan_alerts) instead of the table" |
| `status` | `--chains` | "one row per chain directory instead of one row per Bot" |
| `tail` | `--bot` | "only this Bot (slug, display name or alias): every chain of it" |
| `tail` | `-n` | "number of records, counted over all selected chains (default: 20; 0 prints nothing)" |
| `sync` | `--no-push` | "commit only; never contacts the network" |
| `sync` | `-m/--message` | "commit message (default: 'gblog sync <time>'); redacted like a record field" |
| `archive` | `--stdin` | see K14-01 |
| `archive` | `--kind` | "what the content is (default: other)" |
| `alias add` | `--slug` | "canonical ASCII chain key; lower-cased; never changes afterwards" |
| `alias add` | `--display` | "name shown in reports and status; may contain spaces and non-ASCII characters" |
| `alias remove` | `--slug` | "registered slug to drop" |
| `alias strict` | `mode` | "on: refuse unregistered names (exit 2); off: warn and log under the sanitised name" |
| `alias resolve` | `name` | "slug, display name or alias" |
| `export` | `--bot` | "only this Bot (slug, display name or alias)" |
| `export` | `--since` | † |
| `capture shell-hook` | `--bot` | "default Bot of the hook; a shell overrides it with $GBLOG_BOT" |
| `capture shell-hook` | `--rc` | "bash startup file for --install (default: ~/.bashrc)" |
| `capture browser` | `--bot` | "Bot to log the visits under" |
| `capture browser` | `--hours` | "how far back the first import of a history file goes (default: 24, minimum 1); later runs resume from the per-file marker" |
| `capture files` | `--bot` | "Bot to log the changes under" |
| `capture files` | `--path` | "directory to scan (default: /workspace); each path keeps its own snapshot" |
| `capture files` | `--exclude` | see K14-11 |
| `capture files` | `--limit` | "log at most N changes per run (default: 200, minimum 1); the rest stay pending" |
| `coverage` | `--bot` | "only this Bot" |
| `coverage` | `--days` | "count actions captured in the last N days (default: 7)" |
| `coverage` | `--window` (has help) | add "(default: 30)" |
| `coverage` | `--json` | "print totals and rows as JSON instead of Markdown" |
| `tune` | `--bot` | "analyse only this Bot" |
| `tune` | `--days` | "analysis window in days (default: 28)" |
| `tune` | `--json` | see K14-02 |
| `state rebuild` | `--bot` | "chain to repair; an exact chain directory name wins over alias resolution" |
| `spool list` | `--bot` | "only this Bot: every chain of it" |
| `spool replay` | `--bot` | "chain whose spool is replayed; an exact chain or spool directory name wins over alias resolution" |

Also:

- Add "(default: system)" to `sync --bot` and "(default: ok)" to both `--outcome` flags.
- Set metavars where the dest is misleading: `--window MINUTES`, `--remote URL`, `--exclude NAME`, `--since DATE`, `--ts TIMESTAMP`, `--rc FILE`.
- On `tune`, declare `--detail`/`--summary` as one action, as `_add_common` does.

Examples, each written as a plain invocation that `parse_args` accepts (no `<placeholder with spaces>`):

```
task-start   TID=$(gblog task-start --bot ops --task "Reconcile invoices")
             gblog task-start --bot ledger --source routine --task "Daily ledger audit"
log          gblog log --bot ops --task-id "$TID" --action shell --target "git status" --outcome ok
             gblog log --bot ops --action connector --target quickbooks.sync --outcome fail --detail "401"
             gblog log --bot ops --action shell --target="--force" --detail="-1 rows changed"
approval     gblog approval --bot ops --task-id "$TID" --target "send email to vendor" --decision denied
note         gblog note --bot ops --task-id "$TID" --detail "chose vendor B because A's quote expired"
archive      gblog archive --bot ops --task-id "$TID" --kind artifact --file report.csv
             cat body.txt | gblog archive --bot ops --kind message --stdin --name "reply to vendor"
task-end     gblog task-end --bot ops --task-id "$TID" --outcome ok --summary "3 invoices reconciled"
verify       gblog verify --all
             gblog verify --bot ops --skip-archives --contains "$YESTERDAY_HEAD"
anchor       gblog anchor --skip-archives
report       gblog report --date yesterday
             gblog report --since 2026-11-01 --bot ops --stdout
status       gblog status --json --bot ops
tail         gblog tail --bot ops -n 1
sync         gblog sync --no-push
export       gblog export --bot ops --since yesterday
tune         gblog tune --days 28
             gblog tune --apply abcd1234 --detail "added a re-auth step to the skill"
capture      gblog capture browser --bot ops --hours 168
             gblog capture files --bot ops --path /workspace --exclude transcripts
alias add    gblog alias add --slug chief-of-staff --display "Chief of Staff" --alias cos
spool list   gblog spool list --bot ops
```

**Tests.** See K14-16, tests 1 to 4.

---

## K14-08 — `--bot` has six meanings (Medium)

| Commands | What `--bot` does in the code | Help today |
|---|---|---|
| `task-start`, `log`, `approval`, `note`, `task-end`, `archive`, `capture browser`, `capture files` | `_resolve_bot`: writes to the canonical slug's chain | "Bot name (one chain per bot)" / none |
| `verify`, `anchor`, `report`, `status`, `tail`, `export`, `coverage`, `tune` (analysis), `spool list` | filter: every chain of that Bot | none; `anchor`: "one chain" (wrong); `status`: correct |
| `state rebuild`, `spool replay` | `chain_name`: an exact chain or spool directory wins, else the slug | none |
| `seal` | must be the literal `kim-biseo`; no alias resolution | none |
| `sync` | the chain that receives sync's own records (default `system`); **not a filter** | "bot that performs the sync" |
| `capture shell-hook` | the default Bot written into the hook | none |

The `sync` row matters most. On nine other non-logging commands `--bot X` narrows the scope, so `gblog sync --bot ops` reads as "back up only ops". It commits everything and writes its records into `ops`.

**Fix.** The texts in K14-06 and K14-07, plus `sync --bot`: "chain that receives sync's own records (default: system); it does not limit what is committed".

**Tests.** `sync --bot` help contains `does not limit`. `seal --bot` help contains `kim-biseo`. `state rebuild` and `spool replay` help contain `exact`.

---

## K14-09 — `--help` marks nothing as operator-only and lists no exit codes or environment variables (Medium)

**What is missing.**

- README step 6 and `docs/agent-integration.md` section 12 list commands and flags a Bot must not run on its own: `seal`, `spool replay`, `state rebuild`, `alias add/remove/strict`, `sync --remote/--replace-remote/--allow-*`, `archive --no-redact`, `capture shell-hook --install`, `--ts`. No help string says so.
- The exit-code table (0/1/2/3/4/130) exists only in the README.
- `--help` names one environment variable, `$GBLOG_ROOT`. gblog also reads `GBLOG_TASK`, `GBLOG_BOT` (hook), `GBLOG_DEBUG`, `GBLOG_BROWSER_HISTORY`, `GBLOG_ERRORS` and `GBLOG_REQUIRE_LEDGER`. The last two appear in no document either.

**Fix.**

1. Append "(operator only)" to the help of each item above, and to the description of the four operator commands.
2. Give the top-level parser an epilog with `RawDescriptionHelpFormatter`:

```
commands by purpose:
  logging (a Bot runs these)   task-start, log, approval, note, archive, task-end
  reading                      status, tail, export, coverage, report, tune   (report and tune also write
                               reports/ and tuning/ unless --stdout), spool list, alias list, alias resolve
  integrity and backup         verify, anchor, sync
  evidence capture             capture browser, capture files, capture shell-hook
  operator only                seal, state rebuild, spool replay, alias add/remove/strict, and the flags
                               marked "(operator only)"

exit codes:
  0 ok   1 integrity FAIL, or not pushed   2 usage or environment error
  3 sync refused the push (policy)   4 tail mismatch: append refused, event spooled   130 interrupted

environment:
  GBLOG_ROOT             ledger root (see --root)
  GBLOG_TASK             task id used when --task-id is absent (log, approval, note, archive, task-end)
  GBLOG_BOT              Bot the shell hook logs under
  GBLOG_BROWSER_HISTORY  extra history globs for capture browser (colon-separated)
  GBLOG_REQUIRE_LEDGER   1: a missing ledger is an error and is never created implicitly
  GBLOG_ERRORS           json: one machine-readable "gblog-error: {...}" stderr line per failed run
  GBLOG_DEBUG            1: traceback for an internal error

Run `gblog COMMAND --help` for flags and examples.
```

**Tests.**

- For each operator-only (command, flag), the help contains `operator only`.
- Top-level `format_help()` contains every code in `(0, 1, 2, 3, 4, 130)`.
- Every `GBLOG_[A-Z_]+` name found in the source of `gblog.py` appears in the top-level help. This catches the next undocumented variable.

---

## K14-10 — Flags with no effect in the given combination are accepted silently (Low)

| Command line | What happens |
|---|---|
| `sync --replace-remote` (no `--remote`) | flag ignored |
| `sync --no-push --allow-public`, `--no-push --allow-unknown-visibility`, `--no-push --branch X` | flags ignored |
| `capture shell-hook --rc FILE` (no `--install`) | hook printed; `--rc` ignored |
| `report --date D --since S` | `--since` wins; this is the open item D6-15 |
| `archive --task T`, `archive --source S` | already warned, but both appear in `archive --help` as ordinary options |

`verify --all --bot X` already prints a notice (K1-13). The same policy fits here.

**Fix.** One `notice:` each in `_validate_args`; exit codes unchanged:

```
notice: --replace-remote has no effect without --remote
notice: --allow-public has no effect with --no-push          (likewise --allow-unknown-visibility, --branch)
notice: --rc is used only with --install; the hook was printed, not installed
notice: --since given; --date D is ignored
```

For `archive`, stop inheriting `--task` and `--source` from `_add_common`. Declare them with `help=argparse.SUPPRESS`, so they are still accepted and still warn but are no longer advertised.

**Tests.** One per row: same exit code and stdout as before, the notice on stderr. `archive --help` contains neither `--task ` nor `--source`. `archive --task T` still prints `--task is not stored on archive records`.

---

## K14-11 — `capture files --exclude` and `capture shell-hook --rc` (Low)

**`--exclude`.** `capture_files` matches exact base names at any depth:

```python
skip_names = set(exclude) | {".git", "__pycache__", "node_modules", ".cache", ".venv"}
```

`--exclude transcripts` works. `--exclude transcripts/` (tab completion), `--exclude /workspace/transcripts` and `--exclude "*.log"` match nothing and say nothing. The flag has no help.

Fix:

- Help: "file or directory NAME to skip at any depth (repeatable; an exact name, not a path or pattern)".
- Strip trailing separators from each value.
- If a value still contains `/`, `*` or `?`, print `notice: --exclude takes a file or directory name; '<value>' is a path or pattern and matches nothing`.

**`--rc`.** `gblog capture shell-hook --bot ops --install --rc ~/.zshrc` prints `installed hook in …/.zshrc` and exits 0. The hook relies on `PROMPT_COMMAND`, which zsh does not run, so nothing is ever captured.

Fix: when the base name of `--rc` starts with `.z`, print `warning: the hook works in interactive bash only; zsh does not run it, so nothing will be captured from <file>`. Still install.

**Tests.**

- `--exclude transcripts/` skips the directory.
- `--exclude "a/b"` and `--exclude "*.log"` print the notice and skip nothing.
- `--exclude transcripts` prints no notice.
- `--install --rc <tmp>/.zshrc` exits 0 with the warning. `--rc <tmp>/bashrc` prints none.

---

## K14-12 — Choice normalisation covers three of seven flags (Low)

`--action`, `--outcome` and `--decision` ignore letter case; `--action` also reads `-` as `_`. `--type`, `--source`, `--kind` and the `alias strict` mode do not.

- `gblog log --type task-start` is `invalid choice`, although the command is named `task-start`.
- `gblog archive --kind Transcript` and `gblog alias strict ON` are `invalid choice`.
- `examples/agent-task.sh --action computer-use` is refused by the wrapper's own `case` list, although gblog accepts it.

**Fix.** `type=_norm_action` on `--type`; a `_norm_word` (`str(v).strip().lower()`) on `--source`, `--kind` and `mode`. In `agent-task.sh`, lower-case the value and map `-` to `_` before the `case`.

**Tests.** `--type task-start` stores `task_start`. `--kind Transcript` stores `transcript`. `--source Routine` stores `routine`. `alias strict ON` prints `strict on`. `--type banana` is still exit 2.

---

## K14-13 — Usage messages that contradict what is accepted (Low)

1. **`--hours`.** `_validate_args` has two checks:

   ```python
   if … and args.hours < 0:
       raise _UsageError("--hours must be 0 or more")
   if … and args.hours < 1:
       raise _UsageError("--hours must be 1 or more (a window of 0 hours imports nothing)")
   ```

   `--hours=-5` is told that 0 is fine; `--hours=0` is then refused. Keep only the second check. `tests/test_deep_reviews.py::CliLayerTest::test_capture_browser_negative_hours_is_usage_error` pins the wrong text and must change to `--hours must be 1 or more`.
2. **`--since`.** The message is `--since must be YYYY-MM-DD`, although `today` and `yesterday` are accepted. Use `--since must be YYYY-MM-DD, today or yesterday`. Existing tests match the prefix only.
3. **Internal dest names.** `gblog` prints `the following arguments are required: cmd`; `gblog alias`, `capture`, `state` and `spool` print `…required: what`. Add `metavar="COMMAND"` to the top-level `add_subparsers` and `metavar="SUBCOMMAND"` to the four nested ones. The `dest` values stay.

**Tests.** `--hours=-5` and `--hours=0` give the same single message. `--since nope` mentions `today`. `run_raw([])` exits 2 and stderr contains `COMMAND` and not `: cmd`.

---

## K14-14 — Parity gaps between the parser and the documents (Low)

I checked every `gblog …` invocation in README, the docs and both skills against the parser. **No document names a flag that does not exist.** The gaps run the other way:

| Parser | Documents |
|---|---|
| `anchor --json` | not in README, `docs/DESIGN.md` section 8, `docs/agent-integration.md` or the CHANGELOG. The daily routine could read full heads from it instead of from `anchors/<chain>.json`. |
| `GBLOG_REQUIRE_LEDGER`, `GBLOG_ERRORS` | in no document (see K14-09) |
| `init` | missing from the CLI list in the README table "What you get" |
| `verify --record`, `--all`, `--bot` | not in the README `verify` flag table; `--record` is in the FAQ and the routine only |
| `alias resolve` | not in the README alias block; used in `docs/agent-integration.md` |
| `sync --branch` | appears only inside the error text `detached HEAD; pass --branch` |

The README abbreviation table matches the current parser row by row.

**Fix.** Add the missing rows. Test 5 in K14-16 keeps the parity in both directions.

---

## K14-15 — Naming differences worth a cross-reference, not a rename (Low)

| Topic | Today | Recommendation |
|---|---|---|
| Free text | `--detail`/`--summary` on logging commands and `tune`; `-m/--message` on `sync` | Help only: say on `--message` that it is the commit message, not a ledger detail. |
| Count | `tail -n` has no long form | Add `--lines` as a second option string of the same action. `tail` has no other `--l…` flag. |
| Output | `report`/`tune` write a file unless `--stdout`; `coverage` only prints, and `coverage --stdout` is exit 2 | Accept `--stdout` on `coverage` as a documented no-op. No `--s…` flag exists there. |
| Time range | `--date`, `--since` (report, export); `--days` (coverage, tune; defaults 7 and 28); `--hours` (capture browser); `--window` in minutes | Help only: state the unit and default on each. **Do not add `--days` to `report`**: it would make `--d` and `--da` ambiguous. |

Rule for every additive flag: run test 6 of K14-16 first.

---

## K14-16 — One test module: `tests/test_cli_help.py`

Shared helpers:

```python
def walk(parser, path=()):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            seen = set()
            for name, child in action.choices.items():
                if id(child) not in seen:
                    seen.add(id(child))
                    yield from walk(child, path + (name,))

def arguments(parser):
    skip = (argparse._HelpAction, argparse._VersionAction, argparse._SubParsersAction)
    return [a for a in parser._actions if not isinstance(a, skip)]
```

1. **Every command describes itself and renders.** For every `(path, p)`: `p.description` is non-empty and `p.format_help()` does not raise. Through the CLI, `gblog <path> --help` exits 0 with non-empty stdout and does not create the ledger root.
2. **Every argument has help.** `a.help` is non-empty and not `SUPPRESS` for every argument, except an allow-list of exactly `archive --task` and `archive --source`.
3. **Defaults in help are the real defaults.** For `tail -n` 20, `coverage --days` 7 and `--window` 30, `tune --days` 28, `capture browser --hours` 24, `capture files --path` /workspace and `--limit` 200, `shell-hook --rc` ~/.bashrc, `sync --bot` system, `archive --kind` other, `log --type` action, `--action` other, `--outcome` ok: `str(a.default)` equals the value and `"default: " + value` is in `a.help`.
4. **Examples parse.** Every epilog line that starts with two spaces and contains `gblog ` is cut at `gblog `, split with `shlex` (stopping at ` | ` and stripping a trailing `)`), and passed to `build_parser().parse_args`. At least 20 lines must be found.
5. **Document parity.**
   - For every `gblog ` occurrence in `README.md`, `SECURITY.md`, `docs/*.md` and `skills/*/SKILL.md`: take the text up to the next backtick or end of line, find the longest token prefix that is a parser path, and require every `--flag` token to be an option string of that parser or of the top level. Skip tokens containing `*`, `…` or `<`.
   - In the other direction, every option string of every parser appears somewhere in those files.
6. **README abbreviation table.** For each row, every "No longer accepted" form is a prefix of at least two option strings of that subcommand, and the "still works" form is a prefix of exactly one.
7. **Behaviour.** The cases listed under K14-01 to K14-05 and K14-10 to K14-13.

Existing tests to update: `test_capture_browser_negative_hours_is_usage_error` (K14-13). If K14-03 variant B is chosen, also `AbbreviationTest.test_abbreviations_sync_forms_still_accepted`.

---

## Checked and found correct

- `tune --apply` / `--reject` are a real mutually exclusive group.
- `--root` is accepted at every depth after the subcommand; `default=SUPPRESS` keeps the global value.
- `tail -n 0`, `tail -n -1`, `--hours -5` and `--target=--force` parse as documented.
- `verify --contains` without `--bot`, with `--all`, or with a short hash is exit 2 before any work.
- Record types, action kinds, outcomes, decisions, archive kinds and all synonym lists in README, the skill and `docs/agent-integration.md` match `RECORD_TYPES`, `ACTION_KINDS`, `OUTCOMES`, `ARCHIVE_KINDS`, `_OUTCOME_SYNONYMS` and `_DECISION_SYNONYMS`.
- No current help string contains `%`.

## New stderr lines and exit-code changes (for the CHANGELOG)

| Change | Kind |
|---|---|
| `archive --file F --stdin`: exit 0 to exit 2 | behaviour change (K14-01) |
| `tune --apply ""`/`--reject ""`; `--apply`/`--reject` with `--stdout`, `--json` or a foreign `--bot`: exit 0 to exit 2 | behaviour change (K14-02) |
| `warning: the content to archive is empty (0 bytes)…` | new stderr line |
| `notice: --X was read as --full-name` | new stderr line (K14-03, variant A) |
| `notice: --X was given an empty value and is ignored …` | new stderr line (K14-04) |
| `warning: --task is the task title; …`, `warning: note without --detail …`, `warning: log without --target and --detail …` | new stderr lines (K14-05) |
| `notice: … has no effect …`, `notice: --since given; --date D is ignored` | new stderr lines (K14-10) |
| `notice: --exclude takes a file or directory name; …`, `warning: the hook works in interactive bash only; …` | new stderr lines (K14-11) |
| `--hours must be 1 or more` for negative values; `--since must be YYYY-MM-DD, today or yesterday`; `COMMAND`/`SUBCOMMAND` in argparse errors | message text (K14-13) |

Add each line to the table in `docs/agent-integration.md` section 6.

## Suggested order

1. K14-01 and K14-02: small, and they stop wrong records.
2. K14-03 variant A, K14-04, K14-05, and K14-13 item 1 (the `--hours` message): notices and warnings only.
3. K14-06 to K14-09 in one commit with tests 1 to 4 of K14-16: help text only.
4. K14-10 to K14-15 and tests 5 and 6.
5. Maintainer decision on K14-03 variant B.