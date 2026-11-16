# Deep review 5 — CLI layer (_main, argparse, exit codes, error messages, stdout/stderr discipline, --json outputs)

Scope: `main`, `_main`, `build_parser` / `_build_parser` / `_add_root_everywhere`, `_validate_args`, `_resolve_bot`, `_verify_text`, `_record_verify`, `_write_json`, `_orphan_alerts`, and the library entry points they call directly, in the current `gblog.py` (reports itself as 0.6.1).

Method: static read only. Nothing was executed. Every reproduction below is derived from the code path quoted, uses only `mktemp -d` directories, and should be run once to confirm before fixing.

Checked and found consistent:

- `--root` before and after the subcommand (the `SUPPRESS` default keeps the global value).
- `_write_json` on an ASCII stdout.
- One result list for `verify` text and `--json`.
- `sync` exit precedence (3 before 4 before 1).
- The `$GBLOG_TASK` fallback, `tail -n 0`, and the `tune --apply/--reject` exclusive group.
- `SystemExit` from `_die` still propagates, as existing tests require.

## Summary

| ID | Sev | Finding |
|---|---|---|
| D5-01 | Medium | Reserved bot names are refused only inside `append`; `capture browser`, `capture files`, `archive` and `sync` have side effects first. `capture browser --bot ops.alert` exits 0 and creates a false `ALERT ops`. |
| D5-02 | Medium | `capture browser --hours` is not validated; a negative value pins the per-history marker in the future and blinds browser capture. |
| D5-03 | Medium | A closed stdout pipe turns any exit code (verify FAIL 1, sync refusal 3) into 0. |
| D5-04 | Low-Med | `verify --all --record` stops at the first chain whose record cannot be written (`OSError`); later chains are never verified, and `--json` prints nothing. |
| D5-05 | Low-Med | `--date` / `--since` are checked by shape only; `report --date 2024-13-45` exits 0 with an empty report. |
| D5-06 | Low-Med | `spool replay --bot <typo>` prints `replayed 0 spooled event(s) for <typo>` and exits 0. |
| D5-07 | Low | `tune --bot B` overwrites the all-Bots `tuning/<date>.md`. |
| D5-08 | Low | Raw control characters still reach the terminal in the `ALERT tail mismatch` lines, `error:` lines, the `ANCHOR FAIL` line and git error text. |
| D5-09 | Low | `verify` prints `(N archived file(s) intact)` for `archive` records that name no file. |
| D5-10 | Low | `alias`: unstable order of "unregistered chains", `internal error` after a successful `alias add`, and "(no aliases…)" for an unreadable registry. |
| D5-11 | Low | `verify --bot` / `anchor --bot` with a non-ASCII unregistered name exit 2 instead of `FAIL … no chain on disk` (exit 1). |
| D5-12 | Low | `~` in `--root=~/x` or `GBLOG_ROOT='~/x'` is not expanded; the ledger lands in `<cwd>/~/x`. |
| D5-13 | Low | `status`: no ALERT for pending spool without an alert file; a `task_start` without id counts as open forever; an unknown `--bot` prints "(no records yet)". |
| D5-14 | Low | `capture browser --bot <typo>` leaves `state/<typo>.json`; `verify --bot <typo>` then prints `OK … 0 records  head genesis`. |
| D5-15 | Low | Flag combinations and empty values that are silently ignored; two inaccurate messages. |
| D5-16 | Docs | CHANGELOG "Known open findings", README, SECURITY and DESIGN describe behaviour the code no longer has. |
| D5-17 | Cleanup | Simplifications that remove code without changing behaviour. |

## Findings

### D5-01 (Medium) — Reserved names are checked too late for four commands

**Where.** `Ledger.append` is the only caller of `_check_reserved` on the write path:

```python
        bot = safe_name(bot)
        self._check_reserved(bot)  # a new chain may not use a name that collides with gblog's own files under state/
```

`capture_browser` writes state through `update_state` without an append:

```python
        if newest != markers.get(path):
            markers[path] = newest
            changed = True
            save_markers(final=False)     # -> ledger.update_state(bot, {"capture_browser_last": keep})
```

`update_state` has no reserved-name check. For a name with no chain it writes `{"seq": 0, "head": "genesis", "capture_browser_last": {...}}` to `state/<bot>.json`. On the first run for a history file, `markers.get(path)` is `None`, so the write happens even with zero visits.

**What happens.**

- `capture browser --bot ops.alert` exits 0 and creates `state/ops.alert.json`. `alert_info("ops")` finds it, so `status` prints `ALERT ops: tail mismatch since unknown (0 spooled)`. `examples/routine-daily-audit.sh` then exits 4. `capture browser` is on the Auto-review allow-list.
- `capture browser --bot aliases` adds a `capture_browser_last` key to the alias registry, or creates the file. When a visit is imported, the append is refused (exit 2), but the `except BaseException` branch calls `save_markers` first, so the registry is still rewritten.
- `capture files --bot <reserved>` takes a first baseline (exit 0, `state/capture-files-<bot>.json` written); every later run with a change exits 2.
- `archive --bot <reserved>` stores the `.gz`, is refused, removes the file, and leaves `archive/<bot>/<day>/` behind. On a missing root this creates the root.
- `sync --bot <reserved> --remote NEW --replace-remote` runs `git remote set-url` before `_bookkeep` → `append` → `_die`. It exits 2 with origin already changed and no `git remote` record. SECURITY.md promises every origin change is recorded.

**Reproduction.**

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger
gblog note --bot ops --detail x
python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT); CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER);"); c.commit()' $T/History
gblog capture browser --bot ops.alert --history $T/History; echo "exit $?"   # 0 new visit(s) …; exit 0
gblog status | grep ALERT        # ALERT ops: tail mismatch since unknown (0 spooled)

git init --bare -q $T/a.git; git init --bare -q $T/b.git
gblog sync --no-push --remote $T/a.git
gblog sync --no-push --bot ops.tmp --remote $T/b.git --replace-remote; echo "exit $?"   # error: … reserved; exit 2
git -C $GBLOG_ROOT remote get-url origin              # $T/b.git
gblog export --bot system | grep -c '"git remote"'    # 1: the second change was never recorded
```

**Fix.** One helper, used by every writer before its first side effect:

```python
    def write_name(self, bot: str) -> str:
        """Sanitised chain name for a writer. A reserved name for a new chain is refused (exit 2) before anything is touched."""
        bot = safe_name(bot)
        self._check_reserved(bot)
        return bot
```

- `append`: `bot = self.write_name(bot)` replaces the two lines above.
- `update_state`: `bot = self.write_name(bot)` as its first line. Legacy reserved chains have `logs/<bot>` and still pass.
- `archive_file`, `capture_browser`, `capture_files`: `bot = ledger.write_name(bot)` instead of `safe_name(bot)`.
- `sync()`: `bot = ledger.write_name(bot)` before `ledger.ensure()`.
- `_main`, after `_resolve_bot(...)` for the write commands: `ledger.write_name(args.bot)`. This also covers `capture shell-hook --install`.

### D5-02 (Medium) — `capture browser --hours` can pin the marker in the future

**Where.** `_build_parser`: `s.add_argument("--hours", type=int, default=24)` with no entry in `_validate_args`. In `capture_browser`:

```python
    cutoff_epoch = time.time() - hours * 3600
    ...
            since = _marker_int(marker) if marker else from_epoch(cutoff_epoch)
```

**What happens.** With `--hours -1000000` on the first import of a history file, `since` is about 114 years ahead. No row matches, `newest == since` is saved as the marker, and every later run starts from that marker, because a marker wins over `--hours`. Each run prints `browser: 0 new visit(s) from 1 history file(s)` and exits 0. W-04 stopped a bad row from pinning the marker; the argument still can. The command is allow-listed.

**Reproduction.**

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger; gblog init
python3 - $T/History <<'PY'
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
c.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT); CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER);")
c.execute("INSERT INTO urls VALUES (1,'https://a.example/x','A')")
c.execute("INSERT INTO visits VALUES (1,1,?)", (int((time.time() + 11644473600 - 60) * 1000000),))
c.commit()
PY
gblog capture browser --bot ops --history $T/History --hours -1000000   # 0 new visit(s), exit 0
gblog capture browser --bot ops --history $T/History                    # 0 new visit(s) again, and on every later run
```

**Fix.**

`_validate_args`:

```python
    if args.cmd == "capture" and args.what == "browser" and args.hours < 0:
        raise _UsageError("--hours must be 0 or more")
    if args.cmd == "coverage" and args.window < 0:
        raise _UsageError("--window must be 0 or more")
```

`coverage --window -5` currently prints 0% coverage for every row, so it gets the same guard.

`capture_browser`, so that the library path and an already pinned ledger recover:

```python
    hours = max(0, hours)
    ...
        if to_epoch(since) > time.time() + 86400:   # same horizon as for rows
            sys.stderr.write("notice: ignoring browser-history marker dated in the future for %s in state/%s.json\n"
                             % (_term(_clean_text(path)), bot))
            since = from_epoch(cutoff_epoch)
```

### D5-03 (Medium) — A closed stdout pipe replaces the exit code with 0

**Where.** `main()`:

```python
        rc = _main(argv)
        try:
            sys.stdout.flush()
        except (AttributeError, ValueError):
            pass
        return rc
    ...
    except BrokenPipeError:
        ...
        return 0
```

**What happens.** When stdout is a pipe, Python buffers it, so the output of a normal `verify` sits in the buffer until that `flush()`. If the reader is gone, the flush raises `BrokenPipeError` and the handler returns 0, although `_main` had already returned 1 (verify FAIL), 3 or 4 (`sync`), or 4 (`spool replay`). DESIGN §11 documents "a closed output pipe" as exit 0; that was written for `export | head`, not for commands whose exit code is the verdict.

**Reproduction** (a pipe with no reader, so it does not depend on timing):

```python
import os, subprocess, sys
# root: a temp ledger whose chain "ops" has an edited line, so verify must exit 1
r, w = os.pipe(); os.close(r)
p = subprocess.run([sys.executable, "gblog.py", "--root", root, "verify", "--bot", "ops"], stdout=w, stderr=subprocess.DEVNULL)
os.close(w); print(p.returncode)      # 0
```

**Fix.** Keep the code `_main` already computed:

```python
    rc = 0
    try:
        rc = _main(argv)
        ...
        return rc
    ...
    except BrokenPipeError:
        ...
        return rc        # 0 when the pipe closed mid-run (as documented), the real code when the run had finished
```

Residual case: output above the 8 KiB buffer can hit `EPIPE` mid-run in `verify` or `anchor`. For those two branches, write through a small wrapper that swallows `BrokenPipeError` and stops writing, so the loop finishes and `rc` is returned.

### D5-04 (Low-Medium) — `verify --record` is aborted by an environment error

**Where.** `_record_verify` catches only `TailMismatchError`:

```python
    try:
        ledger.append(bot, record)
        return 0
    except TailMismatchError as exc:
```

**What happens.** An `OSError` from the append (read-only chain directory, `ENOSPC`) leaves the `for b in bots` loop and ends in `main` as `error: …`, exit 2. The remaining chains are not verified. With `--json`, `finish()` is never reached and stdout is empty. This contradicts "one chain cannot hide the others" for the command the daily routine runs.

**Reproduction** (not as root):

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger
gblog note --bot aaa --detail x; gblog note --bot zzz --detail y
chmod 0444 $GBLOG_ROOT/logs/aaa/*.jsonl
gblog verify --all --record; echo "exit $?"   # OK aaa …, then "error: Permission denied: …", exit 2; zzz is never printed
gblog verify --all --record --json            # prints nothing
```

**Fix.** Treat it like the tail-mismatch case: record it, say it, continue.

```python
    except OSError as exc:
        text = exc.strerror or exc.__class__.__name__
        res["record_error"] = "verify record not written (%s)" % text
        if out is not None:
            out.write("      - verify record not written (%s)\n" % _term(text))
        sys.stderr.write("error: %s%s\n" % (text, ": %s" % exc.filename if exc.filename else ""))
        return 1
```

In the caller, replace `rc |= _record_verify(...)` with `rc = max(rc, _record_verify(...))`. The `|=` only works while every value is 0 or 1.

### D5-05 (Low-Medium) — Dates are validated by shape, not as dates

**Where.** `_validate_args` uses `_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")` for `report --date`, `report --since` and `export --since`.

**Reproduction.**

```bash
gblog report --date 2024-13-45 --stdout; echo "exit $?"   # "# Activity report 2024-13-45" / "_No records._", exit 0
gblog export --since 2024-02-31; echo "exit $?"           # exit 0
```

Without `--stdout`, the first command also writes `reports/2024-13-45.md`. A swapped day and month in a routine prompt produces a plausible empty report.

**Fix.**

```python
def _is_date(value: object) -> bool:
    try:
        return bool(_DATE_RE.match(value)) and bool(_dt.datetime.strptime(value, "%Y-%m-%d"))
    except (TypeError, ValueError):
        return False
```

Use it in the three checks. The messages stay `--date must be YYYY-MM-DD, today or yesterday` and `--since must be YYYY-MM-DD`.

### D5-06 (Low-Medium) — `spool replay` for a name that does not exist reports success

**Where.** `_main`, `spool replay`: `chain = ledger.chain_name(args.bot)` followed by `ledger.spool_replay(chain)`. With no spool files and no alert, the result is empty.

**Reproduction.** `gblog spool replay --bot typo; echo $?` prints `replayed 0 spooled event(s) for typo` and exits 0. `verify --bot typo` exits 1 and `state rebuild --bot typo` exits 2. An operator who mistypes the chain during recovery sees a success line while the real alert stays.

**Fix.** Before `spool_replay`:

```python
            if (chain not in ledger.valid_chains() and chain not in ledger.spool_bots()
                    and not os.path.exists(os.path.join(ledger.state_dir, chain + ".alert.json"))):
                sys.stderr.write("spool replay: no chain, spool or alert on disk for '%s'\n" % _term(args.bot))
                return 2
```

The README "dismiss a stale alert" procedure still works, because an alert file exists there.

### D5-07 (Low) — `tune --bot B` overwrites the all-Bots tuning file

**Where.** `_main`, `tune`: `path = os.path.join(ledger.tuning_dir, today() + ".md")`. `report` appends `-<bot>` to the name of a scoped run; `tune` does not. I-18 made `proposals.json` merge by id, but the Markdown file was missed.

**Reproduction.** `gblog tune` then `gblog tune --bot ops`: `tuning/<date>.md` now holds only the `ops` proposals.

**Fix.**

```python
            day = today()
            name = day + ("-" + (_chain_key(bot) or "bot") if bot else "")
            path = os.path.join(ledger.tuning_dir, name + ".md")
```

Update the module docstring to `tuning/YYYY-MM-DD[-<bot>].md`. Render the Markdown once and reuse it for stdout and the file (see D5-17).

### D5-08 (Low) — Control characters still reach the terminal on four paths

README says terminal output "never contains raw control characters from records". These paths are not covered:

| Path | Raw text |
|---|---|
| `main()`: `"ALERT tail mismatch: %s\nspool: %s\nalert: %s\n" % (exc, …)`; the same line in `sync._bookkeep` and `_record_verify` | `observed["seq"]` and `observed["hash"]` come from the last log line as written. A planted tail with `"seq": "x\nALERT resolved"` forges a stderr line that Bots are told to report verbatim. |
| `main()`: `error: <strerror>: <filename>` and `error: internal error (…)` | A file name with ESC; whitespace is collapsed but ESC is kept. |
| `_main` anchor: `where = "%s:%s seq %s->%s" % (br.get("file"), …)` | `br["file"]` is a `*.jsonl` file name in the chain directory. W-11 escaped only the missing-chain lines. |
| `sync`: `"\n  push error: " + res["error"]` and `git failed: …` | git and remote stderr, multi-line, cut at 400 characters. |

**Reproduction** (anchor): in a temp ledger, create `logs/ops/zz$'\x1b[2J'.jsonl` holding one hash-valid record whose `prev` is wrong, then run `gblog anchor`. The `ANCHOR FAIL` line contains a raw ESC.

**Fix.**

- Wrap `str(exc)` in `_term(...)` in the three ALERT writers.
- Wrap `detail` and `text` in `_term(...)` in the two `error:` writers.
- Use `_term(where)` in the anchor line.
- For git text: `_term(" | ".join(line.strip() for line in text.splitlines() if line.strip()))`.

Text without control characters prints as before.

### D5-09 (Low) — "(N archived file(s) intact)" counts records that name no file

**Where.** `Ledger.verify`: `archives = len(arch_recs)`. `_verify_text` prints `(%d archived file(s) intact)` from it.

**Reproduction.**

```bash
gblog log --bot ops --type archive --target claim
gblog verify --bot ops
# OK    ops   1 records  head …  (1 archived file(s) intact)
#       ! logs/ops/….jsonl:1: archive record without path and sha256: no archived file backs it
```

The two lines contradict each other. The round-3 audit noted this and it is still present.

**Fix.** Add an additive result key and use it for the text. The `archives` key keeps its documented meaning.

```python
            res_archive_files = sum(1 for r in arch_recs if r.get("path") or r.get("sha256"))
            ...
            "archive_files": res_archive_files,
```

`_verify_text`: `n = res.get("archive_files", res.get("archives"))`, used for the condition and the count. Chains written by `gblog archive` print the same bytes.

### D5-10 (Low) — `alias` output

1. **Unstable order.** `alias list` builds `chains = set(ledger.valid_chains())`, then `unmapped = [c for c in chains if …]` and `", ".join(unmapped)`. With two or more unregistered chains, the order changes from run to run (string hash randomisation). Fix: iterate `sorted(chains)`.
2. **Crash after a successful write.** `alias add` prints `_term(entry["display"])`. `_read_aliases` accepts an entry without `display`. With a hand-written `{"bots": {"ops": {"aliases": []}}}`, `gblog alias add --slug ops --alias old` writes the registry and then ends with `error: internal error (KeyError: 'display')`, exit 1. Fix: `entry.get("display") or slug`. Use the same in `alias list`, where `"display": null` prints `None`.
3. **Unreadable registry.** `alias list` prints `(no aliases; every name is its own chain)` on stdout. Fix: when `data.get("unreadable")`, print `(state/aliases.json is unreadable; no names are resolved)` instead; exit code unchanged.
4. **Missing variants.** "chains on disk" lists only exact names (`[slug] + aliases`). A case variant that `resolve` maps to the slug appears neither there nor under "unregistered chains". Fix: `on_disk = [c for c in sorted(chains) if ledger.canonical(c) == slug]`.

### D5-11 (Low) — Non-ASCII unregistered name on `verify` / `anchor` exits 2

**Where.** `verify` and `anchor` call `ledger.chains_on_disk(args.bot)` → `resolve` → `_die("'…' has no ASCII letters or digits; register it first: gblog alias add …")`.

**Reproduction.** `gblog verify --bot 김비서; echo $?` gives exit 2 with advice for write commands, and `--json` prints nothing. README: "A name with no chain on disk prints `FAIL … no chain on disk for '<name>'` and exits 1."

**Fix.** Split the registry passes of `resolve` into `Ledger.registered(name) -> Optional[str]`, which never exits. In both branches:

```python
            unknown = ledger.registered(args.bot) is None and not _chain_key(args.bot)
            bots = [] if unknown else ledger.chains_on_disk(args.bot)
```

The existing `if not bots:` block then emits the FAIL entry, in text and JSON. The other readers (`status`, `tail`, `export`, `report`, `coverage`, `tune`) may keep exit 2; add that case to DESIGN §11.

### D5-12 (Low) — `~` is not expanded for the ledger root

**Where.** `Ledger.__init__`: `self.root = os.path.abspath(root or default_root())`. `--rc` and `capture files --path` call `expanduser`; the root does not.

**Reproduction.** `cd "$(mktemp -d)" && GBLOG_ROOT='~/gblog-tilde' gblog note --bot ops --detail x` creates `./~/gblog-tilde/`. Shells do not expand `--root=~/x`, and cron and systemd do not expand a tilde in an environment line. The result is a different ledger per working directory.

**Fix.** `self.root = os.path.abspath(os.path.expanduser(root or default_root()))`.

### D5-13 (Low) — `status` gaps

1. **Spool without an alert file.** The ALERT loop prints only `if a_ts:`. `_orphan_alerts` already handles "spool files but no alert file" for chains without a log directory; a chain with a log directory gets no line, although JSON shows `"spooled": N`. Fix: mirror `_orphan_alerts`. When `not a_ts and a_n`, take `ts` from the first spool file and print the line.
2. **`task_start` without an id.** `open_tasks.add(r.get("task_id"))`: `gblog log --type task_start` without an id adds `None`, which only a `task-end` without an id removes, the case the CLI warns "closes no task". Fix: skip records without `task_id` in both branches.
3. **Unknown filter.** `status --bot nosuch` prints `(no records yet)`. Fix: when `args.bot` is set and no row matched, print `(no chain on disk for '<name>')`.

### D5-14 (Low) — A capture-only state file makes `verify --bot <typo>` print `OK … 0 records`

**Where.** `chains_on_disk` counts a chain when `state/<c>.json` exists. `capture browser --bot typo` creates exactly that, with a genesis head (see D5-01).

**Reproduction.** After the first block of D5-01 with `--bot typo`: `gblog verify --bot typo` prints `OK    typo    0 records  head genesis` and exits 0. `gblog anchor --bot typo` writes `anchors/typo.json`. README: "It is never `OK … 0 records`."

**Fix.** In `chains_on_disk`, a state file whose readable head is genesis is not a chain:

```python
        def on_disk(c):
            if os.path.isdir(os.path.join(self.logs_dir, c)):
                return True
            if not os.path.exists(self._state_path(c)):
                return False
            st = self._load_state(c)
            return st is None or st.get("head", GENESIS) != GENESIS   # unreadable state still counts
```

An emptied chain whose state names a head still fails, as R-01 requires.

### D5-15 (Low) — Silently ignored input and two inaccurate messages

| Invocation | Today | Fix (stderr only, exit codes unchanged) |
|---|---|---|
| `verify --all --bot X` | `--bot` ignored; a typo exits 0 | `notice: --bot is ignored with --all` |
| `report --date D --since S` | `--date` ignored | `notice: --date is ignored with --since` |
| `archive --stdin --file F` | `--file` ignored | `notice: --file is ignored with --stdin` |
| `archive --task T` | dropped (only `--source` warns) | `warning: --task is not stored on archive records` |
| `tune --apply ""` | runs the full report and writes files | `raise _UsageError("--apply/--reject need a proposal id")` when the flag is present but empty (`is not None`) |
| `note` without `--detail` | empty note | `warning: note without --detail` |

Messages:

- `_resolve_bot` in strict mode prints `error: bot name 'x' is not registered … (logging under 'x'); …`, although nothing is logged. Drop the parenthesis when `strict`.
- `spool replay` prints `… is unreadable; left in place` also for an event that `append` refused (`RecordError`). Use `… cannot be replayed; left in place` for that case.

### D5-16 (Docs) — Documents that contradict the current CLI

| File | Statement | Code |
|---|---|---|
| CHANGELOG "Known open findings" | Version still `0.6.0`; no `ts` form check; no `--record-baseline`; hook logs `$VAR` commands; `sync` stamps invalid directories; `alias list` exits 2 with `logs/---`; clamped records in two reports; dead code present | All eight are in the code (`__version__ = "0.6.1"`, strict `ts is not in normalised form`, `--record-baseline`, hook v2, `valid_chains()`, …). The same file's "Behaviour changes" already describes hook v2. Remove the block and date the release. |
| SECURITY "Supported versions" | "a checkout that contains these fixes still prints `gblog 0.6.0`" | Prints `gblog 0.6.1`. |
| README shell-hook paragraph | "A hook installed from this tree also logs a `gblog` command that contains a shell variable" | The hook skips it (`*"\$("*`). |
| README capture paragraph | Stdout "still starts with `files: 0 change(s)`, so read stderr"; no `--record-baseline` | Stdout carries ` (new baseline: <reason>)`; the flag exists. `docs/faq.md` Q29 is correct. |
| README "Upgrading" table and "Spell flags out" | No row for `capture files --r` | Ambiguous between `--root` and `--record-baseline`. |
| README exit table, row 1 | No `capture browser` or `verify --record` cases | DESIGN §11 and agent-integration list both. |
| DESIGN §5 rule 3 | `_Lock` "still carries … (`_held`)" | Removed. |
| DESIGN §7 | "The written form of `ts` is not checked in any mode yet" | Checked under `--strict`. |
| DESIGN §7 JSON schema | No `aborted` key; no schema for `status --json` | `_verify_guarded` sets `"aborted": true`; `status --json` has `orphan_alerts` and `future_tail`. |
| `routine-daily-audit.md` "Why" | One bullet says `anchor` re-hashes archives ("two full passes"); the next says it runs with `--skip-archives` | The prompt uses `anchor --skip-archives`. Delete the first bullet. Align agent-integration §9 step 2 and `examples/routine-daily-audit.sh`, which both use plain `anchor`. |
| `--help` texts | `verify --strict`, `capture browser`, `capture shell-hook --install` | Incomplete: symlinks, field types and `ts` form are also strict; Firefox is also read; an old hook is replaced. |

### D5-17 (Cleanup) — Simplifications without behaviour change

1. **`$GBLOG_TASK` fallback, three copies.** Put `log` and `archive` into the tuple at the top of `_main` (`("approval", "note", "task-end", "log", "archive")`) and drop the two inline `or os.environ.get("GBLOG_TASK")`.
2. **`getattr` on attributes argparse always sets.** `getattr(args, "json", False)` in `verify`, `allow_unknown_visibility`, `replace_remote`, `record_baseline`, and `getattr(args, "bot", None)` three times in `status`. Use plain attribute access.
3. **`tune`.** `render_tune(...)` is called twice and `today()` three times, so title and file name can differ across midnight. Compute `day` and `text` once. Replace the two `add_argument` calls for `--summary` and `--detail` with `add_argument("--detail", "--summary", dest="detail", …)`, as `_add_common` does.
4. **Missing-chain merge, three copies.** It lives in `_main` (verify), `Ledger._anchor` and `missing_witnesses`. Add `Ledger.missing_chains() -> Dict[str, List[str]]` and use it in all three. Give `_verify_missing(name, problems)` a list so the caller no longer overwrites `entry["problems"]`.
5. **`verify` loop.** `getattr(res, "own_hashes", getattr(res, "hashes", ()))` becomes `getattr(res, "own_hashes", ())`. `args.bot.strip()` is computed up to three times per chain although `exact` exists.
6. **`status`.** The CLOCK loop recomputes `_future_ts(updated)` although `r["future_tail"]` holds the result.
7. **`_add_root_everywhere`.** It walks private argparse attributes (`_actions`, `_SubParsersAction`, `_option_string_actions`). A parent parser passed as `parents=[root_parent]` to each `add_parser` uses public API only and removes the function and the `build_parser` / `_build_parser` split.
8. **Unreachable code.** The final `return 1` of `_main` (every subparser group is `required=True`).
9. **Outside the CLI layer, seen while tracing.**
   - `class _ChainLines` and `Ledger._chain_lines` (about 70 lines) have no caller; `verify` uses `_chain_snapshot`.
   - The `.gitignore` body is a literal in `init()` and in `_sync_evidence()`.
   - The "`--ts … is earlier than the last record`" text exists in `append` and in `archive_file`.

## Test cases to add

`tests/test_cli_ux.py`, unless another file is named.

| Finding | Test | Asserts |
|---|---|---|
| D5-01 | `test_capture_browser_reserved_name_writes_nothing` (`test_capture_sync.py`) | For `ops.alert`, `aliases`, `capture-files-ops` with an empty-table history: exit 2, `reserved` in stderr, no new file under `state/`, `status` prints no `ALERT`, an existing registry is byte-identical. |
| D5-01 | `test_archive_reserved_name_leaves_no_directory` | `archive --bot aliases --stdin` exits 2; `archive/aliases` does not exist; a missing root stays missing. |
| D5-01 | `test_capture_files_reserved_name_refused_on_first_run` | Exit 2, no `state/capture-files-<bot>.json`. |
| D5-01 | `test_sync_reserved_bot_does_not_touch_origin` (`test_capture_sync.py`, git) | `sync --bot ops.tmp --remote B --replace-remote` exits 2, origin is still A, no `.git` on a fresh ledger. |
| D5-01 | `test_update_state_refuses_reserved_new_chain` (`test_append_safety.py`) | `SystemExit(2)`; a legacy chain with `logs/<name>` still updates. |
| D5-02 | `test_capture_browser_negative_hours_is_usage_error` | Exit 2, `--hours must be 0 or more`, no state file. |
| D5-02 | `test_future_marker_is_ignored_with_notice` | A marker 10 years ahead in state; the next run imports a recent visit and prints the notice. |
| D5-02 | `test_coverage_negative_window_is_usage_error` | Exit 2. |
| D5-03 | `test_exit_code_survives_closed_stdout` (`test_malformed_input.py`, POSIX, subprocess with a reader-less pipe) | `verify` on a tampered chain returns 1; `export` on a healthy ledger returns 0; no `Traceback`, no "Exception ignored". |
| D5-04 | `test_verify_record_oserror_does_not_hide_other_chains` (skip as root) | First chain's day file `0444`: both chains printed, exit 1, `record_error` in `--json`, `error:` on stderr. |
| D5-04 | `test_verify_record_exit_code_is_never_3` | Tail mismatch on one chain and FAIL on another give rc 1. |
| D5-05 | `test_impossible_dates_are_usage_errors` | `report --date 2024-13-45`, `--date 2024-02-30`, `report --since 2024-00-10`, `export --since 2024-02-31`: exit 2, no file under `reports/`. |
| D5-06 | `test_spool_replay_unknown_bot_exit_2` | Exit 2 with the message. Add `test_spool_replay_existing_chain_without_spool_exit_0` for an existing chain, which still prints `replayed 0 …`. |
| D5-07 | `test_tune_bot_run_keeps_all_bots_file` (`test_tune_loop.py`) | After `tune` and `tune --bot ops`: `tuning/<day>.md` is unchanged and `tuning/<day>-ops.md` exists. |
| D5-08 | `test_alert_line_has_no_escapes` (`test_security_regressions.py`) | Planted tail line with ESC and newline in `seq` and `hash`; `note` exits 4; stderr has exactly three lines and no `\x1b`. |
| D5-08 | `test_anchor_fail_line_has_no_escapes` | Stray `*.jsonl` name with ESC causing a break; no `\x1b` in stdout. |
| D5-08 | `test_sync_error_text_is_one_clean_line` | Mocked push failure with multi-line stderr containing ESC. |
| D5-09 | `test_bare_archive_record_is_not_counted_as_intact` (`test_integrity.py`) | After `log --type archive`: no `archived file(s) intact` on the OK line, the `!` line present, JSON has `archives: 1` and `archive_files: 0`. A real archive still prints `(1 archived file(s) intact)`. |
| D5-10 | `test_alias_list_unregistered_chains_sorted` | Three unregistered chains; output is sorted, also under several `PYTHONHASHSEED` values (subprocess). |
| D5-10 | `test_alias_add_entry_without_display` | Hand-written entry without `display`: exit 0, `display=<slug>`, no `internal error`. |
| D5-10 | `test_alias_list_unreadable_registry_says_so` | Stdout does not contain `no aliases`. |
| D5-10 | `test_alias_list_shows_case_variant_chain` | A chain `Cos` with alias `cos` is listed under its Bot. |
| D5-11 | `test_verify_non_ascii_unregistered_name_is_fail_not_usage` | `verify --bot 김비서` and `anchor --bot 김비서`: exit 1, `no chain on disk`. `--json` has one synthetic entry. The registered display name still resolves. |
| D5-12 | `test_root_tilde_is_expanded` | With `HOME` patched: `GBLOG_ROOT='~/x'` and `--root=~/x` resolve under `HOME`; nothing named `~` in the working directory. |
| D5-13 | `test_status_alert_for_spool_without_alert_file` | Delete `state/ops.alert.json`, keep the spool: `ALERT ops:` is printed. |
| D5-13 | `test_task_start_without_id_is_not_an_open_task` | `log --type task_start` without id gives `open_tasks == 0`. |
| D5-13 | `test_status_unknown_bot_message` | Text does not say `(no records yet)` when other chains exist. |
| D5-14 | `test_capture_only_state_is_not_a_chain` | After `capture browser --bot typo`: `verify --bot typo` exits 1 with `no chain on disk`; `anchor --bot typo` writes no anchor. An emptied chain with a non-genesis state still FAILs with the head-mismatch text. |
| D5-15 | `test_ignored_flag_combinations_are_announced` | The four notices and warnings; exit codes unchanged. |
| D5-15 | `test_tune_apply_empty_id_is_usage_error` | Exit 2, nothing under `tuning/`. |
| D5-15 | `test_strict_refusal_message_does_not_claim_logging` | No `logging under` in the strict error. |
| D5-16 | `test_changelog_has_no_stale_open_findings` (`test_meta.py`) | No "still prints `gblog 0.6.0`" in `CHANGELOG.md` or `SECURITY.md` when `gblog.__version__ != "0.6.0"`. |
| D5-16 | `test_readme_lists_every_ambiguous_abbreviation` (`test_meta.py`) | Each ambiguous case asserted in `test_abbreviations` appears in the README table. |
| D5-17 | `test_cli_output_is_unchanged_by_refactor` (named in W-16, never written) | Fixed-timestamp ledger and a table of commands with expected stdout written out. Add it before any cleanup. |