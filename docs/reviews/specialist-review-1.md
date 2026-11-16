# Specialist review K1 — 2026-11-16

**Area:** cross-command consistency in `gblog.py` 0.6.1: `--bot` selection, timestamps and UTC days, `--json` and stdout formats, exit codes, task-id / `$GBLOG_TASK` fallback, alias strict mode.

**Method:** code reading of the current tree only. Nothing was executed, so every reproduction below is derived from the code paths quoted and should be run before a fix lands. The fix snippets are sketches, not tested patches. No code was edited.

## 0. Summary

| Id | Prio | Finding |
|---|---|---|
| K1-01 | High | "Open task" means three different things. `report --date` shows a task that crosses UTC midnight as open on day D and untitled on D+1, while `status` says closed. No command lists open task ids. |
| K1-02 | High | Docs and CHANGELOG disagree with `gblog.py` in both directions: exit codes, JSON keys and output lines are documented but absent, and items listed as "known open" are already fixed. |
| K1-03 | Medium | A mistyped `--bot` is exit 1 on `verify`/`anchor`, exit 2 on `spool replay`/`state rebuild`, and silent exit 0 on six readers. `status` prints `(no records yet)`; `report` and `tune` write a file. |
| K1-04 | Medium | `anchor --bot X` exits 0 where `verify --bot X` exits 1 (deleted chain whose witness survives). |
| K1-05 | Medium | `status --bot B` shows alerts of every chain of B; `spool list --bot B` looks at one chain and says "no spooled events". |
| K1-06 | Medium | A stale or mistyped task id is diagnosed only by `task-end`. `log`, `note`, `approval` and `archive` attach silently; `task-start --task-id` accepts a duplicate. |
| K1-07 | Medium | Under `alias strict on`, the shell hook loses every event for a drifted `GBLOG_BOT`: exit 2 is swallowed and nothing is spooled. |
| K1-08 | Low-Med | `verify --record` and `tune --apply` bypass the name resolver. The daily routine appends to every legacy variant chain, which contradicts the README. |
| K1-09 | Low-Med | On case-insensitive filesystems, unregistered `Ops` and `ops` share one directory with mixed `bot` fields, which leads to a false `--contains` miss. |
| K1-10 | Low | `tune` uses two different window starts in one run (start of UTC day for coverage, to the second for the analysis). |
| K1-11 | Low | `--outcome` accepts synonyms; `--decision` and `--action` do not. `log --type approval` writes no `decision`. |
| K1-12 | Low | Format gaps: `tail` cannot identify a record; the merged `status --json` row mixes scopes; `alias resolve` has no machine-readable verdict. |
| K1-13 | Low | Small items: `verify --all --bot`, `--since today`, `archive --task`, `"strict": "off"`, `_ts_epoch`, `CLOCK` source. |

### How `--bot` is resolved today

| Command | Resolver | Chains selected | Unknown name |
|---|---|---|---|
| `task-start`, `log`, `approval`, `note`, `task-end`, `archive`, `capture *` | `_resolve_bot` (strict, reserved) | slug chain only | warning, or exit 2 in strict mode |
| `sync --bot` | `_resolve_bot`, except the literal `system` | slug chain | same |
| `verify --bot` | `chains_on_disk` plus exact-name witness check | every chain of the Bot on disk | FAIL, exit 1 |
| `anchor --bot` | `chains_on_disk` | same, **no witness check** (K1-04) | FAIL, exit 1 |
| `report`, `export`, `tail`, `coverage`, `tune --bot` | `canonical` → `chains_for` | every valid chain that resolves to the slug | **empty output, exit 0** (K1-03) |
| `status --bot` | `canonical` on group keys; orphan alerts by exact-name set | same | `(no records yet)`, exit 0 |
| `spool list`, `spool replay`, `state rebuild` | `chain_name` | exact directory, else **slug chain only** (K1-05) | list: exit 0; replay and rebuild: exit 2 |
| `seal` | `safe_name` | literal `kim-biseo` | exit 2 |
| `tune --apply` / `--reject` | none (bot taken from `proposals.json`) | that chain | no strict check (K1-08) |
| `verify --record` | none | every verified chain directory | no strict check (K1-08) |

Write commands and readers agree. The mismatches are in the recovery commands, in `anchor`, and in what an unknown name does.

---

## K1-01 (High) — Three definitions of "open task"; `report --date` is wrong across UTC midnight

**Where:** `render_report`, the `report` and `status` branches of `_main`.

```python
# render_report: only the records of the requested day are known
elif tid != "(no task)":
    twin = None
    ...
    else:
        meta.append("outcome **open**")
```

```python
# _main, status: a count only
rows.append({..., "open_tasks": len(open_tasks), ...})
```

- `report --date D` decides "open" from day D's records alone.
- `status` decides over 30 days and exposes only a count.
- `tune` decides over its window.

UTC midnight is 09:00 in Seoul, so a task that spans it is normal for a non-UTC user. Three things follow:

- The report for D says `outcome **open**` forever, also when it is regenerated the next day with `report --date yesterday`.
- The report for D+1 shows the same task without a title. The title is only on the `task_start` record, so the heading falls back to the id.
- `docs/routine-daily-audit.md` step 7 asks for "the list of tasks still open", and the skill says to close a stale task found in `gblog status`. No command prints an open task's id.

**Reproduction**

```bash
export GBLOG_ROOT=$(mktemp -d)
gblog task-start --bot ops --task-id n1 --task "Night job" --ts 2026-01-05T23:59:00Z
gblog log  --bot ops --task-id n1 --action shell --target make --ts 2026-01-06T00:00:30Z
gblog task-end --bot ops --task-id n1 --outcome ok --ts 2026-01-06T00:01:00Z
gblog report --date 2026-01-05 --stdout   # ### Night job  `n1`  /  _start 2026-01-05T23:59:00Z, outcome **open**_
gblog report --date 2026-01-06 --stdout   # ### n1  `n1`        /  _end 2026-01-06T00:01:00Z, outcome **ok**_
```

`status --json` reports `open_tasks: 0` for the same ledger (when run within 30 days of those dates).

**Fix**

1. Add a helper that finds, by raw scan, the counterpart record of tasks that cross the day. Call it only in `--date` mode, after `canonicalize`.

```python
def _task_context(ledger, bot, recs, date):
    """{(slug, task_id): {"start", "title", "end", "outcome"}} for tasks of one UTC day that were
    opened before it (30 days back) or closed after it. Lines are parsed only when they carry the marker."""
    seen = {}
    for r in recs:
        if r.get("task_id"):
            f = seen.setdefault((r.get("bot"), r["task_id"]), [False, False])
            f[0] = f[0] or r.get("type") == "task_start"
            f[1] = f[1] or r.get("type") == "task_end"
    want_end = {k for k, f in seen.items() if not f[1]}
    want_start = {k for k, f in seen.items() if not f[0]}
    ctx = {}
    if not (want_end or want_start) or not _is_date(date):
        return ctx
    floor = (_dt.datetime.strptime(date, "%Y-%m-%d") - _dt.timedelta(days=30)).strftime("%Y-%m-%d")
    for chain in (ledger.chains_for(bot) if bot else ledger.valid_chains()):
        slug = ledger.canonical(chain)
        for path in ledger.log_files(chain):
            day = os.path.basename(path)[:10]
            if day > date and want_end:
                marker, kind, want = b'"task_end"', "task_end", want_end
            elif floor <= day < date and want_start:
                marker, kind, want = b'"task_start"', "task_start", want_start
            else:
                continue
            with open(path, "rb") as fh:
                for raw in fh:
                    if marker not in raw:
                        continue
                    rec = _text_fields(_parse_line(raw))
                    key = (slug, rec.get("task_id"))
                    if rec.get("type") != kind or key not in want:
                        continue
                    slot = ctx.setdefault(key, {})
                    if kind == "task_end":
                        slot.setdefault("end", rec.get("ts"))          # the first end after the day
                        slot.setdefault("outcome", rec.get("outcome"))
                    else:
                        slot["start"], slot["title"] = rec.get("ts"), rec.get("task") or ""  # the latest start before it
    return ctx
```

2. Give `render_report` an optional fourth parameter `context=None`, so existing callers and output stay the same. Per task, with `extra = (context or {}).get((bot, tid), {})`:
   - no start on the day and `extra` has one: use its `start` and `title`;
   - no end on the day and `extra` has one: print `outcome **<outcome>** (closed <ts>, after this day)` instead of `outcome **open**`. This branch goes before the twin logic.
3. Add the key `open_task_list` to each `status --json` row: `[{"task_id", "task", "started", "chain"}]`, oldest first. Keep `open_tasks` as its length. Point the skill and the routine prompt at this key.

**Tests**

- `test_report_task_crossing_utc_midnight`: seed as above with `days_ago`. Day D contains `outcome **ok** (closed` and no `**open**`. Day D+1 has the heading `### Night job  \`n1\`` and `start <D ts>`.
- `test_report_open_task_stays_open`: with no `task_end` anywhere, the output is unchanged.
- `test_render_report_without_context_unchanged`: the existing `ReportRenderingTest` expectations hold with three arguments.
- `test_status_json_open_task_list`: two open tasks and one closed; ids, titles and order match; `len(open_task_list) == open_tasks`.

---

## K1-02 (High) — Docs and CHANGELOG disagree with the code in both directions

The CHANGELOG says every deep-review finding rated Medium or higher is fixed in this branch. Only the DR2, DR5 and DR6 items are in `gblog.py` (they are the ones `tests/test_deep_reviews.py` covers). Several DR1, DR3 and DR4 items are documented as shipped but are absent. Conversely, the "Known open findings" section lists DR2, DR5 and DR6 items that are implemented and tested.

Agents branch on exit codes and JSON keys, so this is a contract problem. The rows below are the ones I checked that fall in the K1 area.

| Documented (where) | Code today | Verdict |
|---|---|---|
| `capture browser --hours` below 1 exits 2 (README, FAQ 28, DESIGN §11, CHANGELOG D4-03) | `_validate_args`: `args.hours < 0` → `--hours must be 0 or more`; pinned by `test_capture_browser_negative_hours_is_usage_error` | fix docs: "below 0"; `--hours 0` means "start from now" |
| `coverage --json` key `elsewhere` (CHANGELOG "Added", README, FAQ 30, agent guide §10) | `coverage()` returns `rows, unmatched, self_only, auto, matched, pct` | not implemented |
| break descriptors carry `missing_count`, with empty `missing_seqs` above 10 000 (DESIGN §7, CHANGELOG D1-02) | `_describe_join`: `list(range(seq_before + 1, seq_after))`; `tests/test_fuzz_verify.py` says so in a comment | not implemented |
| FAIL entry `anchor aborted: <class>` (DESIGN §2 and §11) | only `verify aborted: …` from `_verify_guarded` | not implemented |
| `sync`: `<dir> is a symbolic link: …; not pushed`, exit 1 (README, SECURITY, DESIGN §11 and §12, FAQ 25) | no such check in `sync()` | not implemented |
| `sync` without `origin`: nothing is pushed (README, SECURITY, FAQ 26) | warns `no origin configured`, then runs `git push … origin` | exit 1 either way; behaviour differs |
| `capture files`: one snapshot per `--path`; file records carry `observed` (README, DESIGN §3 and §6) | one snapshot; `(new baseline: path changed)` is pinned by `test_new_baseline_reason_is_on_the_output_line`; no `observed` | not implemented |
| tune: `auto` decisions do not count as approvals (README table, FAQ 31) | `analyze`: `r.get("decision") in ("allowed", "auto")` | not implemented |
| tune: failures count the Bot's own reports only (CHANGELOG D4-06) | `fails` includes `source: auto` records | not implemented |
| `seal` with a tail mismatch on `kim-biseo` exits 4 and spools the ack (`docs/chain-repair-live-apply.md` "Known limit", CHANGELOG known-open D1-11/D2-08) | `seal_break` preflights the sealed chain → exit 2, nothing spooled (`test_seal_refuses_when_own_chain_has_tail_mismatch`) | docs stale |
| Known open: D5-04, D5-05, D5-06, D5-07, D5-12, D2-06, D2-09, D6-06 … D6-10 | implemented and tested in `tests/test_deep_reviews.py` | docs stale |
| DESIGN §4 "Two known limits": first interrupted append leaves `state_adopted` | genesis state is written before the first line (`test_first_append_interrupted_rolls_forward_without_marker`) | docs stale |

**Fix**

- For each "not implemented" row, either land the code or move the entry to "Known open findings" and correct README, FAQ, DESIGN and SECURITY. Until then 0.6.1 must not be tagged.
- Move the stale known-open ids into the fixed list and delete the stale "Known limit" paragraphs.
- Add `tests/test_doc_contract.py` so the drift cannot return unnoticed:
  - every `--flag` in backticks next to a `gblog <cmd>` in README and `docs/*.md` is accepted by `build_parser()`;
  - every key in the DESIGN §7 "`verify --json` entries" table appears in a real `verify --json` entry (healthy, broken and synthetic), or is listed in a small allow-list of conditional keys;
  - every `coverage --json` and `status --json` key named in the docs exists in real output;
  - every exit-2 message quoted in agent guide §5 is produced by its command.

---

## K1-03 (Medium) — A mistyped `--bot`: exit 1, exit 2, or silent exit 0

**Where:** the `report`, `status`, `tail`, `export`, `coverage` and `tune` branches of `_main`.

```python
bot = ledger.canonical(args.bot) if args.bot else None   # report, tune
recs = ledger.records(bot, date=date)                    # chains_for() -> [slug] -> no files -> []
...
if not rows:                                             # status
    out.write("(no records yet)\n")
```

**Reproduction**

```bash
gblog note --bot ops --detail x
gblog verify --bot opps        # FAIL … no chain on disk for 'opps'      exit 1
gblog spool replay --bot opps  # spool replay: no chain, spool or alert  exit 2
gblog status --bot opps        # root: …\n(no records yet)               exit 0
gblog report --bot opps        # "_No records._", writes reports/<date>-opps.md, exit 0
gblog tune --bot opps          # writes tuning/<date>-opps.md            exit 0
gblog export --bot opps; gblog tail --bot opps; gblog coverage --bot opps   # nothing, exit 0
```

A routine that posts `report --bot <typo>` reports "no activity" for a Bot that was busy. `(no records yet)` is false for a ledger that has records.

**Fix** (stdout and exit codes unchanged; one stderr line)

```python
def _warn_unknown_bot(ledger: "Ledger", name: Optional[str]) -> None:
    """Readers: say so when --bot selects nothing on disk (verify/anchor report the same name as FAIL)."""
    if name and not ledger.chains_on_disk(name):
        sys.stderr.write("warning: no chain on disk for '%s'; nothing to show (see `gblog alias list`)\n" % _term(name))
```

Call it once in each of the six branches, before any output. For `tune`, call it only in generate mode. In `status`, keep the existing line; the warning corrects it.

**Tests**

- `test_readers_warn_about_unknown_bot`: for each of the six commands, exit 0, stdout byte-identical to today, stderr contains the line once.
- `test_no_warning_for_display_name_or_alias`: with `seed_variants()`, `--bot "Chief of Staff"` and `--bot cos` produce no warning.
- `test_no_warning_without_bot`.

---

## K1-04 (Medium) — `anchor --bot X` passes where `verify --bot X` fails

**Where:** the `anchor` branch of `_main`, and `Ledger._anchor`.

```python
# _main, verify: the exact name is checked for a surviving witness
lost = ledger.missing_witnesses(exact) if exact not in bots else []
# _main, anchor: no such check
bots = ledger.chains_on_disk(args.bot)
# _anchor: the missing-chain report runs only without --bot
if bots is None:
    lost = self.missing_chains()
```

**Reproduction** (the setup of `test_verify_bot_fails_for_a_deleted_chain_that_an_alias_resolves_elsewhere`)

```bash
# chains ops and ops-new, anchored; alias add --slug ops-new --alias ops; rm -r logs/ops state/ops.json
gblog verify --bot ops   # OK ops-new … / FAIL ops 0 records … logs/ops is missing    exit 1
gblog anchor --bot ops   # ANCHOR OK ops-new …                                        exit 0
```

**Fix:** one helper for both commands. It also closes the known-open D1-07 (a deleted variant chain of a Bot named by slug).

```python
def lost_chains_of(self, name: str, present: Iterable[str] = ()) -> Dict[str, List[str]]:
    """Missing chains (logs/<chain> gone, witness left) that ``name`` selects: the exact name and
    every chain that resolves to the same slug. Chains in ``present`` are verified normally and are left out."""
    lost = self.missing_chains()
    if not lost:
        return {}
    exact, skip = (name or "").strip(), set(present)
    slug = self.canonical(exact)
    return {c: p for c, p in lost.items()
            if c not in skip and _chain_key(c) == c and (c == exact or self.canonical(c) == slug)}
```

- `verify --bot`: replace the `missing_witnesses(exact)` logic with `missing.update(ledger.lost_chains_of(args.bot, bots))`. The `not bots` branch keeps its `no chain on disk` entry and extends it with the exact name's lines, as today.
- `anchor --bot`: after `ledger.anchor(bots)`, render each lost chain like the `rollup["missing"]` entries (`ANCHOR FAIL <chain> break - heads` plus `      - <problem>`) and return 1.
- `present` keeps `test_logs_dir_deleted_state_remains_fails` at exactly one FAIL block.

**Tests**

- `test_anchor_bot_fails_for_deleted_chain_with_witness`: scenario above; exit 1, `ANCHOR OK ops-new`, `ANCHOR FAIL ops`, `but logs/ops is missing`; `anchors/ops.json` untouched.
- `test_verify_bot_slug_reports_deleted_variant_chain`: `seed_variants()`, anchor, delete `logs/cos` and `state/cos.json`; `verify --bot chief-of-staff` exits 1 with one `FAIL cos` block.
- Regression: `test_verify_bot_fails_for_a_deleted_chain_that_an_alias_resolves_elsewhere` and `test_logs_dir_deleted_state_remains_fails` stay green.

---

## K1-05 (Medium) — `status --bot` and `spool list --bot` disagree about one Bot's alerts

**Where:** the `spool` branch of `_main`, and `Ledger.chain_name`.

```python
chains = [ledger.chain_name(args.bot)] if args.bot else ledger.spool_bots()   # list
chain = ledger.chain_name(args.bot)                                           # replay, state rebuild
```

`chain_name` returns the exact directory or the slug: one chain. `status --bot` prints an `ALERT` line for every chain of the Bot. Variant chains do get refusals, because `verify --all --record` appends to each chain directory (K1-08).

**Reproduction**

```bash
# seed_variants(): chains cos, ChiefOfStaff, cos-primary, chief-of-staff; then break state/cos.json
gblog verify --all --record                    # exit 1; verify record for cos is spooled
gblog status --bot chief-of-staff              # ALERT cos: tail mismatch since … (1 spooled)
gblog spool list --bot chief-of-staff          # stderr "no spooled events", exit 0
gblog spool replay --bot "Chief of Staff"      # "replayed 0 spooled event(s) for chief-of-staff", exit 0
```

The skill tells the Bot to run `gblog spool list --bot "$BOT"` after an alert, so the Bot reports "nothing spooled" while `status` shows an alert.

**Fix**

```python
def spool_chains_for(self, name: str) -> List[str]:
    """Chains whose spool `spool list --bot` shows: the exact chain first, then every chain or spool directory of the Bot."""
    first = self.chain_name(name)
    slug = self.canonical(name)
    rest = [c for c in sorted(set(self.chains_for(name)) | set(self.spool_bots()))
            if c != first and _chain_key(c) == c and self.canonical(c) == slug]
    return [first] + rest
```

- `spool list --bot`: `chains = ledger.spool_chains_for(args.bot)`. The line format already carries the chain column.
- `spool replay --bot` and `state rebuild --bot` stay exact-chain (repair is addressed per chain on purpose). After the summary line, print a hint on stderr when sibling chains still have pending files: `note: <n> spooled event(s) are pending in chain(s) cos; replay each with gblog spool replay --bot <chain>`. Exit codes do not change.

**Tests**

- `test_spool_list_bot_covers_variant_chains`: scenario above; `spool list --bot chief-of-staff` and `--bot "Chief of Staff"` print the `cos` line; `--bot ops` prints nothing.
- `test_spool_replay_hints_at_sibling_chains`: replay by slug exits 0 with the note on stderr, and `spool/cos/*.json` is untouched; replay with `--bot cos` drains it.
- Regression: `test_spool_list_and_replay_clears_alert` stays green.

---

## K1-06 (Medium) — Only `task-end` diagnoses a stale or mistyped task id

**Where:** the `task-end` branch of `_main` is the only caller of `_task_start_seen`.

```python
elif not _task_start_seen(ledger, args.bot, args.task_id, args.ts):
    sys.stderr.write("warning: task-end for task '%s' has no task-start in the last 30 days of %s; recorded anyway\n" ...)
...
"task_id": args.task_id or os.environ.get("GBLOG_TASK"),        # log: no check
tid = args.task_id or uuid.uuid4().hex[:10]                     # task-start: no duplicate check
```

**Reproduction**

```bash
TID=$(gblog task-start --bot ops --task A) && export GBLOG_TASK="$TID"
gblog task-end --bot ops --outcome ok                  # A is closed; the export is still there
gblog log --bot ops --action shell --target "rm -rf build"    # exit 0, silent: filed under the closed task A
gblog note --bot ops --task-id typo123 --detail x             # exit 0, silent
gblog report --stdout     # "### typo123  `typo123`" with "_outcome **open**_"; status says open_tasks 0
gblog task-start --bot ops --task-id daily --task "run 1"; gblog task-start --bot ops --task-id daily --task "run 2"   # silent
```

Results:

- Records land under a closed task.
- The report invents an open task that `status` does not count.
- A fixed `--task-id` reused by a routine merges two runs into one task in `report`, `status` and `tune` durations.

**Fix**

```python
def _task_status(ledger, bot, task_id, ts=None, days=30):
    """'open', 'closed' or 'unknown' for a task id in the Bot's chains. Newest day file first; a line is
    parsed only when it contains the id as JSON text."""
    needle = json.dumps(task_id, ensure_ascii=False).encode("utf-8", "backslashreplace")
    now = _utcnow().replace(tzinfo=None)
    try:
        base = min(now, _dt.datetime.strptime(str(ts or "")[:10], "%Y-%m-%d"))
    except ValueError:
        base = now
    since = (base - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    files = sorted(((os.path.basename(p)[:10], p) for c in ledger.chains_for(bot) for p in ledger.log_files(c)
                    if os.path.basename(p)[:10] >= since), reverse=True)
    try:
        for _day, path in files:
            last = None
            with open(path, "rb") as fh:
                for raw in fh:
                    if needle in raw:
                        rec = _parse_line(raw)
                        if rec.get("task_id") == task_id and rec.get("type") in ("task_start", "task_end"):
                            last = "open" if rec["type"] == "task_start" else "closed"
            if last:
                return last
    except OSError:
        return "open"          # unreadable history: stay quiet, as _task_start_seen does
    return "unknown"
```

Use it as follows. All of these are stderr warnings; exit codes and stdout do not change.

- `log`, `approval`, `note`, `archive`, after a successful append, when a task id is in effect and `--source` is not `auto` (this keeps the shell hook cheap and silent):
  - `unknown` → `warning: task '<id>' has no task-start in the last 30 days of <bot>; recorded anyway (check --task-id / $GBLOG_TASK)`
  - `closed` → `warning: task '<id>' is already closed; recorded anyway (unset GBLOG_TASK or open a new task)`
- `task-start` with an explicit `--task-id`, checked before the append: a status other than `unknown` → `warning: task id '<id>' was already used by <bot> in the last 30 days; reports will merge both runs`.
- `task-start` without `--task` → `warning: task-start without --task: the task has no title`.
- `task-end`: unchanged, including its text.

Cost: one raw scan of today's day file per logging command in the usual case. Add that line to DESIGN §13, and add the new stderr lines to agent guide §6.

**Tests**

- `test_log_warns_for_closed_task_from_env`: start, export, end, `log` → exit 0, record written with that id, stderr has `is already closed`.
- `test_note_warns_for_unknown_task_id`; `test_no_warning_inside_open_task`: `log`, `note`, `approval` and `archive` between start and end are silent.
- `test_hook_records_are_not_checked`: `log --source auto` with an unknown id is silent.
- `test_task_start_duplicate_id_warns` and `test_task_start_without_title_warns`; stdout is still the id alone.
- Regression: `test_env_task_fallback_note_approval_task_end` and `test_task_end_without_id_warns` stay green.

---

## K1-07 (Medium) — Strict mode silently drops shell-hook evidence

**Where:** `_resolve_bot` and `SHELL_HOOK`.

```python
if data.get("strict"):
    _die(msg)                       # exit 2, nothing written, nothing spooled
```

```sh
gblog log --bot "${GBLOG_BOT:-%s}" ... --source auto >/dev/null 2>&1 || true
```

A self-reporting Bot sees the exit 2. The hook discards it. Unlike a tail mismatch, a strict refusal leaves no spool file and no alert, so `status`, `spool list` and `coverage` show nothing.

**Reproduction**

```bash
gblog alias add --slug ops --display Ops && gblog alias strict on
gblog capture shell-hook --bot ops --install
export GBLOG_BOT=Ops2          # a drifted name in one interactive shell
# every command typed in that shell: `gblog log --bot Ops2 …` exits 2 and is discarded
gblog status; gblog spool list; gblog coverage     # no trace
```

Without strict mode the same drift is logged under `Ops2` with a warning. Strict mode therefore turns a naming mistake into unreported loss of automatic evidence.

**Fix:** hook v3. A refusal of the name in `GBLOG_BOT` (exit 2) falls back to the Bot the hook was installed for, which passed `_resolve_bot` at install time.

```sh
  gblog log --bot "${GBLOG_BOT:-%(bot)s}" --task-id "${GBLOG_TASK:-}" --action shell --target="$cmd" \
    --outcome "$outcome" --source auto >/dev/null 2>&1 || {
    if [ "$?" -eq 2 ] && [ -n "${GBLOG_BOT:-}" ] && [ "${GBLOG_BOT}" != "%(bot)s" ]; then
      gblog log --bot "%(bot)s" --task-id "${GBLOG_TASK:-}" --action shell --target="$cmd" \
        --outcome "$outcome" --source auto --detail "GBLOG_BOT=${GBLOG_BOT} was refused" >/dev/null 2>&1 || true
    fi
  }
```

- `shell_hook()` formats with `{"bot": safe_name(bot)}`. The marker line becomes `# gblog hook v3`.
- `_install_shell_hook` already replaces an older block in place.
- Only exit 2 triggers the fallback. A tail mismatch (4) must stay on its own chain.
- `test_hook_is_marked_v2` has to be updated to v3; `test_shell_hook_mentions_bot_and_installs_once` asserts `GBLOG_BOT:-ops`, which still holds.
- CI shellchecks the hook, so run `shellcheck -s bash -e SC1007` on the new text.

**Tests**

- Extend `test_shell_hook_skips_only_plain_gblog_commands` with a fake `gblog` that exits 2 when `--bot` is not `ops`. With `GBLOG_BOT=Ops2`, the log shows two calls, the second with `--bot ops` and `--detail GBLOG_BOT=Ops2 was refused`.
- With the fake exiting 4: exactly one call.
- With `GBLOG_BOT` unset: one call.
- `test_install_replaces_v2_hook`: `_install_shell_hook` returns `updated`.

---

## K1-08 (Low-Med) — `verify --record` and `tune --apply` bypass the resolver

**Where:** the `verify` branch of `_main` and `tune_apply`.

```python
rc |= _record_verify(ledger, None if as_json else out, b, res, {"type": "verify", ...})   # b = chain directory
return ledger.append(p["bot"], {"type": "tune", ...})                                      # bot from proposals.json
```

Three contradictions follow:

- README ("One Bot, one name") says old chains stay byte-for-byte as they were. The daily routine runs `verify --all --record`, which appends one `verify` record per day to every legacy variant chain. `status` then counts them under `today`, and they can raise new alerts on retired chains (see K1-05).
- Under `alias strict on`, `note --bot lee` is refused for an unregistered chain `lee`, but `verify --record` and `tune --apply` still write to `logs/lee`.
- If aliases changed after the proposals were generated, `tune --apply` writes to a chain that no longer resolves to itself (known open as D4-20).

**Reproduction**

```bash
# seed_variants()
gblog verify --bot "Chief of Staff" --record      # four chains each gain a verify record
gblog alias strict on; gblog note --bot ops2 --detail x     # exit 2 (unregistered)
gblog verify --all --record                        # still appends to every unregistered chain
```

**Fix**

- Minimal: correct the README sentence to say that logging commands never write to a variant chain, and that `verify --record`, `state rebuild` and `spool replay` address a chain directory and do append to it. State in agent guide §7 that strict mode governs the logging commands only.
- `tune_apply`: write to `ledger.canonical(p["bot"])`, so a decision follows the Bot after an `alias add`. Keep the proposal id in `target`.
- Optional, needs the operator's sign-off because it changes where new records land: `verify --record` writes one record per chain into the canonical chain with `detail` prefixed `<chain>: `. Variant chains then really stay frozen.

**Tests**

- `test_tune_apply_follows_alias_added_after_generation`.
- A doc-contract test for the corrected README sentence (K1-02).
- If the optional part is taken: `test_verify_record_leaves_variant_chains_untouched`, comparing the variant chains' heads before and after.

---

## K1-09 (Low-Med) — Case-insensitive filesystems: `Ops` and `ops` share a directory

**Where:** `Ledger.write_name`, `Ledger.append`, `Ledger.chains_on_disk`.

With an empty registry, or for an unregistered name, `resolve("Ops")` returns `Ops`. On macOS or Windows `logs/Ops` is the existing `logs/ops`:

- `append` writes records with `"bot": "Ops"` into the directory that `os.listdir` reports as `ops`.
- The lock and state file coincide, so the chain stays hash-valid.
- `verify --bot ops` then warns `bot field 'Ops' does not match chain 'ops'` (a FAIL with `--strict`).
- `own_hashes` leaves those records out. The daily rollback check `verify --bot ops --contains <head>` exits 1 with `expected hash … not found in chain` whenever the posted head was written as `Ops`. That is a false ROLLBACK report.
- `verify --bot Ops` verifies the same files under a second name.

On Linux these are two chains, as documented.

**Reproduction** (macOS, no aliases)

```bash
gblog note --bot ops --detail a; H=$(gblog note --bot Ops --detail b | cut -c1-12)
gblog verify --bot ops                                   # OK … ! …:2: bot field 'Ops' does not match chain 'ops'
gblog verify --bot ops --contains "<full hash of record 2>"   # FAIL expected hash … not found in chain
```

**Fix:** adopt the spelling that is on disk.

```python
def _disk_name(self, bot: str) -> str:
    """The chain name as the directory under logs/ is spelled. Differs from ``bot`` only where the
    filesystem ignores letter case (macOS, Windows): there, logs/Ops *is* logs/ops."""
    if not os.path.isdir(os.path.join(self.logs_dir, bot)):
        return bot
    try:
        names = os.listdir(self.logs_dir)
    except OSError:
        return bot
    if bot in names:
        return bot
    low = bot.lower()
    return next((n for n in names if n.lower() == low), bot)
```

- Call it at the end of `write_name()` and inside `chains_on_disk()`, so `verify --bot Ops` reports the chain as `ops`.
- On a case-sensitive filesystem `isdir` is false for the other spelling and nothing changes.
- Cost: one listing of `logs/` per append.
- Existing mixed chains keep their warnings; note that in the FAQ.

**Tests**

- `test_write_name_adopts_on_disk_case`: skip unless the temporary directory is case-insensitive (create `A`, test `os.path.exists("a")`). The macOS CI leg runs it. Assert both records carry `bot: "ops"`, there are no warnings, and `--contains` passes for both hashes.
- `test_disk_name_unit`: patch `os.path.isdir` and `os.listdir` so the mapping is tested on Linux.
- `test_case_sensitive_fs_unchanged`: on Linux, `Ops` and `ops` remain two chains.

---

## K1-10 (Low) — `tune` uses two window starts in one run

**Where:** the `tune` branch of `_main` and `analyze`.

```python
since = _days_ago(args.days)                                   # "YYYY-MM-DD": start of that UTC day
cov = coverage([r for r in recs if r.get("ts", "") >= since])
analysis = analyze(recs, args.days, cov, effects_since=start)
# analyze():
since = _days_ago(window_days, "%Y-%m-%dT%H:%M:%SZ")           # exactly N×24 h ago
```

The coverage rows and the evidence of a `coverage:<kind>` proposal come from up to 24 hours more than the metrics. The proposal's baseline is computed by `compute_metric(..., since=<exact ts>)`, so its title can say "50.0% of 4 observed" while "Metric to watch" shows another value. `coverage --days N` has the same start-of-day rounding (N days plus the current partial day); that is fine alone but should be stated in its help.

**Reproduction:** run `gblog tune --days 1 --json --stdout` at 15:00Z with three unmatched auto shell records at 10:00Z yesterday. The `coverage:shell` proposal exists (count 3), while `metrics` and `since` describe a window that starts at 15:00Z yesterday and contains none of them.

**Fix:** compute the window once and hand it to both.

- `analyze(records, window_days, cov=None, effects_since=None, since=None)`: use the given `since` when present.
- In `_main`: `since_ts = _days_ago(args.days, "%Y-%m-%dT%H:%M:%SZ")`; `cov = coverage([r for r in recs if r.get("ts", "") >= since_ts])`; pass `since=since_ts`. Loading keeps using the date.
- `coverage --days` help: "from 00:00 UTC of the day N days ago".

**Tests**

- `test_tune_coverage_uses_the_analysis_window`: with `now_iso`/`_utcnow` patched, auto records 30 hours old produce no coverage proposal at `--days 1`; records 20 hours old do, and its `baseline` equals the percentage in the title.
- Regression: `TuneWindowTest` unchanged.

---

## K1-11 (Low) — `--outcome` forgives; `--decision` and `--action` do not

**Where:** `_build_parser`.

```python
s.add_argument("--outcome", default="ok", type=_norm_outcome, choices=OUTCOMES)
s.add_argument("--decision", required=True, choices=("allowed", "denied", "pending", "expired", "auto"))
s.add_argument("--action", default="other", choices=ACTION_KINDS, ...)
```

**Reproduction**

```bash
gblog approval --bot ops --target "send mail" --decision approved   # exit 2: invalid choice 'approved'
gblog log --bot ops --action computer-use --target x                # exit 2: invalid choice
gblog log --bot ops --type approval --target x --outcome denied     # record has no `decision`
```

In the last case the report counts the record under "Denied" (`outcome`), but `tune`'s `denied` metric (`decision`) does not.

**Fix** (additive; canonical values are stored and printed)

```python
_DECISION_SYNONYMS = {"approved": "allowed", "approve": "allowed", "allow": "allowed", "granted": "allowed", "yes": "allowed",
                      "rejected": "denied", "declined": "denied", "deny": "denied", "refused": "denied", "no": "denied",
                      "timeout": "expired", "timed-out": "expired", "automatic": "auto"}
def _norm_decision(value): v = str(value).strip().lower(); return _DECISION_SYNONYMS.get(v, v)
def _norm_action(value): return str(value).strip().lower().replace("-", "_")
```

- `--decision type=_norm_decision`, `--action type=_norm_action`.
- In the `log` branch, when `args.type == "approval"`, add `decision` derived from the outcome (`ok` → `allowed`, `denied` → `denied`, `pending` → `pending`, `skipped` → `expired`).
- Document the synonyms beside the `--outcome` list.

**Tests**

- `test_decision_synonyms`: each synonym is stored canonically and stdout shows the canonical word; `--decision banana` is still exit 2.
- `test_action_accepts_hyphen`.
- `test_log_type_approval_sets_decision`.

---

## K1-12 (Low) — Format gaps between commands that should cross-reference

1. **`tail` cannot identify a record.** Write commands print `<hash12> #<seq> <type>`. `tail` prints no hash, and with merged chains prints the Bot label plus a per-chain `#seq`. `tail --bot cos -n 6` after `seed_variants()` shows `Chief of Staff(chief-of-staff) #1` three times for three different records. The documented check after an exit 2 ("is my record the last line?") therefore rests on free text.
   - Fix: add `tail --json`, one JSON object per line: the record as stored plus `"chain": <directory>`, written with `_write_json`. In text mode, append `[<chain>]` after the label only when the chain differs from the slug.
   - Tests: `test_tail_json_lines` (parseable, `hash` and `chain` present, same order as text); `test_tail_marks_variant_chain` (`…(chief-of-staff)[cos] #2`); a single-chain line is byte-identical to today.
2. **The merged `status --json` row mixes scopes.** `seq` is the sum over chains, `updated` the maximum, and `head` only the slug chain's state head (or `-`). The reply footer built from it pairs a Bot-wide count with one chain's head.
   - Fix: add the key `heads: {chain: head12}`; leave `head` as it is.
   - Test: `test_status_json_heads` (one entry per chain, equal to the state heads).
3. **`alias resolve` has no machine-readable verdict.** It prints `slug  (unregistered)` on stdout with exit 0, so `SLUG=$(gblog alias resolve "$BOT")` captures the suffix.
   - Fix: add `alias resolve --check`, which prints the slug alone and exits 1 when unregistered. Default output stays unchanged.
   - Test: `test_alias_resolve_check`.
4. **`coverage --json` omits what the text shows.** There is no `unmatched` list and no window metadata, and it always escapes to ASCII while the other `--json` outputs do not.
   - Fix: add `unmatched_count`, `days`, `window_minutes`; drop the forced `ensure_ascii=True` (`_write_json` already escapes when the stream needs it).
   - Test: `test_coverage_json_metadata`.
5. **stderr prefixes for exit 2 vary** (`error:`, `seal:`, `archive:`, `state rebuild:`, `spool replay:`, `git failed:`, or none for `no proposals yet`). The docs quote the texts, so do not rename them. Add one sentence to agent guide §5: recognise exit 2 by the code, not by an `error:` prefix. CI's `grep '^error: '` only covers the `OSError` path.

---

## K1-13 (Low) — Small inconsistencies

| # | Where | Today | Fix | Test |
|---|---|---|---|---|
| a | `verify` branch: `if args.all or not args.bot` | `verify --all --bot X` ignores `--bot` without a word | stderr `notice: --all given; --bot X is ignored` | exit code and stdout equal to `verify --all` |
| b | `_validate_args` | `--date` accepts `today`/`yesterday`; `--since` (`report`, `export`) does not | accept the same two words for `--since` | `report --since yesterday --stdout` exits 0 |
| c | `archive` branch | `--source` warns "not stored"; `--task` is dropped silently | same warning for `--task` | stderr line; record unchanged |
| d | `_read_aliases` | a hand-edited `"strict": "off"` is truthy, so strict is on | `data["strict"] = data.get("strict") is True` | `alias list` prints `strict: off`; an unknown name is accepted |
| e | `_ts_epoch` | slices `ts[:19]`; a pre-0.6 `±HH:MM` offset is ignored, and a space-separated form yields 0.0, which makes all such records "simultaneous" in `coverage` | try `parse_ts(ts)` first, fall back to the slice | unit test with both forms |
| f | `status` `CLOCK` line and `future_tail` | read from `state.updated`; with a missing or unreadable state file there is no `CLOCK` line, although `verify` warns from the log tail | fall back to `_tail_record(c)` when `updated` is not a string | state removed and tail future-dated → `CLOCK` printed |
| g | `_orphan_alerts(only=…)` | matches the orphan chain against an exact-name set; a case variant (`Ops` for `--bot ops`) is dropped although readers resolve it | match `_chain_key(c) == c and ledger.canonical(c) == slug` as well | orphan alert `Ops` appears for `--bot ops` |
| h | `verify --bot`, readers | an unregistered name without ASCII letters exits 2 with "register it first", also for read commands, and prints no JSON under `--json` | readers and `verify` catch that case and treat it as "no chain on disk" | `verify --bot "Ωμέγα" --json` exits 1 with one synthetic entry |

---

## Suggested order

1. **K1-02** first. It decides which of the other documents are true, and the doc-contract test protects everything after it.
2. **K1-01, K1-03, K1-04, K1-05**: reader and recovery consistency. All are additive: stderr lines, one JSON key, and stricter exit codes only where `verify` already fails.
3. **K1-06, K1-07**: task-id and strict-mode evidence. K1-07 bumps the hook to v3; add "run `capture shell-hook --install` again" to the upgrade notes.
4. **K1-08 to K1-13** as capacity allows. The optional part of K1-08 needs the operator's decision.

None of the proposed fixes changes the record format or the hash rule, or stdout that an existing test pins. The intended exit-code changes are in K1-04 (`anchor --bot` and `verify --bot <slug>` fail where a witness of a deleted chain survives) and in K1-13(h) (2 → 1 for a non-ASCII unregistered name).