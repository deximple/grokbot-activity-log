# Specialist review K13 — 2026-11-16

**Area.** Performance and scale: 100 000-record chains, 10 000 archive files, many Bots; hotspots in verify, sync, coverage, tune and the task-status check; memory use; optimisations that keep output identical; benchmarks to add.

**Method.** Read-only review of the current tree (`gblog.py`, `bin/gblog`, `install.sh`, `tests/bench_large_ledger.py`). **No code was run.** Every figure below is an estimate derived from the code (CPython 3.8–3.12, one ~3 GHz core, warm page cache) and is marked as such. Each finding gives a way to measure it.

**Not repeated here.** K4-02 … K4-14 and the open K4 items in the CHANGELOG: line-size limits, `export`/`status`/`report`/`tail` parsing more than needed, `tune` metric recomputation, uncapped `problems`, the three walks of the daily routine, archive rotation, and the no-cache decision for `sync`.

**Symbols.** N records per chain, D day files per chain, C chain directories, R registered Bots, V aliases per Bot, Y files under `anchors/days/`, F evidence files, k records of one task.

| Id | Severity | One line |
|---|---|---|
| K13-01 | High | Every CLI call recompiles `gblog.py`; fixed process cost is likely 30–100× the append itself, and the benchmark cannot see it |
| K13-02 | High (long-lived or many-Bot ledgers) | `verify --bot`, `verify --all` and `anchor` parse every day rollup; the daily routine does this once per chain |
| K13-03 | Medium | `log_files()` and `bots()` issue one `stat` per entry; readers call them per chain and several times per command |
| K13-04 | Medium | `coverage`: the "reported by another Bot" pass is O(unmatched × self-reports); `_ts_epoch` uses `strptime` per record |
| K13-05 | Medium | Archive re-hash and the sync content check are single-threaded although zlib and hashlib release the GIL |
| K13-06 | Medium/Low | Name resolution is O(R·V) per call and O(C·R·V) per `chains_for()`, several times per command |
| K13-07 | Low/Medium | `state rebuild` holds the whole parsed chain in memory under the exclusive lock |
| K13-08 | Low | Task-status check parses every line that carries the task id: O(k) per command, O(k²) per task |
| K13-09 | Low (measure first) | Redaction of large archives runs ~27 full regex passes; a literal pre-filter skips most of them |
| K13-10 | Low | Smaller items: fsync fan-out per chain, anchor history parsed twice, unbounded `verify --record` detail, label per `tail` line |
| K13-11 | — | Benchmarks and complexity-guard tests to add |

---

## K13-01 — Every invocation recompiles `gblog.py` (High)

**Where.** `bin/gblog` (last line), `install.sh` (installs `gblog.py` as `$PREFIX/gblog`), `gblog.py` module level and `build_parser()`.

```bash
# bin/gblog
exec python3 "$repo/gblog.py" "$@"
```
```bash
# install.sh
install -m 0755 "$tmp" "$new"
mv -f "$new" "$PREFIX/gblog"        # executed through its shebang
```

**Problem.** In both launch paths `gblog.py` runs as `__main__`. Python never caches bytecode for `__main__`, so the whole multi-thousand-line source is parsed and compiled on every call. Each call then also:

- compiles about 35 regular expressions at module level (`_SECRET_PATTERNS`, `_PEM_*`, `_TS_RE`, …);
- imports `argparse`, `subprocess`, `tempfile`, `hashlib`, `uuid`;
- builds about 25 sub-parsers (`_build_parser` plus `_add_root_everywhere`);
- from a checkout, starts one extra interpreter for the version probe in `bin/gblog`.

Estimate per call: 15–25 ms interpreter start, 30–100 ms compile, 10–20 ms module body, ~10 ms stdlib imports, 3–6 ms parser. That is roughly **70–150 ms**, against 1–3 ms plus fsync for the append itself (DESIGN §13).

Consequences:

- A 100 000-record chain costs about 2–4 CPU-hours of process overhead to write.
- The shell hook adds this latency to every interactive prompt; when gblog refuses the name in `GBLOG_BOT` and the hook falls back, it adds it twice.
- `tests/bench_large_ledger.py` times `Ledger.append` and `gblog.main` in-process, so the dominant cost of the write path is invisible to it.

**Reproduction.**

```bash
export GBLOG_ROOT="$(mktemp -d)"; ./bin/gblog init
time (for i in $(seq 200); do ./bin/gblog note --bot bench --detail "n $i" >/dev/null; done)
python3 - <<'PY'
import time
t = time.perf_counter(); src = open("gblog.py", encoding="utf-8").read()
code = compile(src, "gblog.py", "exec"); c = time.perf_counter() - t
t = time.perf_counter(); ns = {"__name__": "gblog_probe"}; exec(code, ns); e = time.perf_counter() - t
t = time.perf_counter(); ns["build_parser"](); p = time.perf_counter() - t
print("compile %.1f ms, module body %.1f ms, build_parser %.1f ms" % (c * 1e3, e * 1e3, p * 1e3))
PY
python3 -X importtime -c 'import gblog' 2>&1 | tail -n 5
```

**Fix.**

1. **Checkout launcher: import instead of run.** Python then keeps `__pycache__/gblog.cpython-3x.pyc` (already ignored by `.gitignore`). Replace the last line of `bin/gblog`:

   ```bash
   # Imported, not run as a script: Python caches the bytecode in <checkout>/__pycache__.
   # sys.path[0] ('' under -c) is replaced by the checkout, exactly what a script run puts there,
   # so nothing is ever imported from the current directory.
   exec python3 -c 'import sys; sys.path[0] = sys.argv.pop(1); import gblog; sys.exit(gblog.main())' "$repo" "$@"
   ```

   Output is identical:
   - `prog="gblog"` is explicit in the parser.
   - `main(None)` reads `sys.argv[1:]`, which is correct after the `pop`.
   - Exit codes go through the same `sys.exit(main())`.
   - A stale `.pyc` is rejected by the source mtime and size check.
   - A read-only checkout or `PYTHONDONTWRITEBYTECODE` simply behaves as today.

   The only visible difference is one extra `File "<string>"` frame in a `GBLOG_DEBUG=1` traceback.

2. **Installed copy: opt-in two-file layout.** The default must stay byte-identical to `gblog.py`, because CI runs `cmp "$SRC/gblog.py" "$T/bin/gblog"` and the README promises one file. Add `GBLOG_LAYOUT=module` to `install.sh`:
   - install the module as `${GBLOG_LIBDIR:-$PREFIX/.gblog}/gblog.py`;
   - write a small stub as `$PREFIX/gblog` with the same atomic `mv`:

   ```python
   #!/usr/bin/env python3
   import sys
   if sys.version_info < (3, 8):
       sys.stderr.write("gblog needs Python 3.8 or newer (python3 is %d.%d)\n" % sys.version_info[:2]); sys.exit(2)
   sys.path[0] = "@LIBDIR@"          # replaces the script directory
   import gblog
   sys.exit(gblog.main())
   ```

   Whether this becomes the default is your decision; it changes the "one file" statement.

3. **Stop growing the compiled source with test oracles.** `Ledger._verify_reference` (about 250 lines) and the two `former` regexes exist only for differential tests. They are compiled or built on every call. Move them to `tests/_support.py` when the tests are next touched. New oracles for the fixes below should live in the tests from the start.

4. Leave the version probe in `bin/gblog` as it is. Folding it into the interpreter would lose the "python3 did not start" message. Measure it and decide separately.

**Tests.**
- `test_scripts.py::test_launcher_uses_bytecode_cache`: copy `gblog.py` and `bin/gblog` to a temp dir, run `--version` twice; assert the output equals `gblog <version>` and `__pycache__/gblog.*.pyc` exists.
- `test_launcher_ignores_cwd_modules`: run from a cwd containing `json.py` and `gblog.py` that raise `SystemExit("decoy")`; assert a normal `--version`.
- `test_launcher_output_identical`: for `--version`, `verify --bot nosuch`, an argparse error and `note --ts yesterday`, assert that `(rc, stdout, stderr)` of `python3 gblog.py …` and `bin/gblog …` are equal.
- `test_install_module_layout` (opt-in): the stub runs, the module is cached, and the default layout still passes `cmp`.
- CI: the existing "Launcher works through a symlink" and "ignores CDPATH" steps must pass unchanged.

---

## K13-02 — Witness scan parses every day rollup, once per chain in the daily routine (High for long-lived or many-Bot ledgers)

**Where.** `Ledger.orphan_witnesses`, `anchored_missing`, `missing_chains`, `lost_chains_of`, `_rollup_heads`; callers `verify --bot`, `verify --all`, `anchor`, `anchor --bot`.

```python
# orphan_witnesses(): every rollup, every entry, one isdir() per entry
for fname in self._witness_listing(days):
    ...
    data = _read_json(os.path.join(days, fname))
    for c in entries if isinstance(entries, list) else []:
        ...
        if (isinstance(name, str) and name and _chain_key(name) == name
                and _is_int(n) and n > 0 and gone(name)):      # gone() = os.path.isdir, not memoised
            rolled[name] = fname[:-5]
```
```python
# lost_chains_of(): one absent name is enough to run the full scan
gone = [n for n in self.names_of(name) if n and n not in have and _chain_key(n) == n and not os.path.isdir(...)]
lost = self.missing_chains() if gone else {}
```
```python
# anchored_missing(): parses every anchors/<bot>.json before it asks whether logs/<bot> exists
data = self.read_anchor(bot)
...
if _is_int(n) and n > 0 and not os.path.isdir(os.path.join(self.logs_dir, bot)):
```

**Problem.**

- `missing_chains()` costs Y file reads and JSON parses, each of size O(C), plus C·Y `isdir` calls and `_chain_key` regex calls, plus C anchor-file parses. The rollup directory grows by one file per day for ever.
- `verify --bot X` runs that full scan whenever any name of the Bot has no `logs/` directory. That is the normal case once a Bot is registered: a slug that never got its own chain, or an alias kept for convenience.
- Step 1 of the daily routine calls `verify --bot <chain> --contains …` once per posted chain. Daily cost is therefore about (C + 2) scans, which is O(C²·Y).
- `_rollup_heads` runs per chain **under the shared bot lock**. In `verify --all` it re-lists `anchors/days` and re-parses the newest rollup (size O(C)) for every chain: O(C²) per run.

Estimate: with C = 50 and Y = 1 000 (three years), one scan is about 0.3 s, so about 15 s per day in step 1 alone. With C = 200 one scan is about 1.2 s, so about 4 minutes per day.

**Reproduction.**

```python
import contextlib, io, os, tempfile, time, gblog
root = tempfile.mkdtemp(); L = gblog.Ledger(root)
with contextlib.redirect_stderr(io.StringIO()):
    for i in range(60): L.append("bot%02d" % i, {"type": "note", "detail": "x"})
    L.add_alias("chief", "Chief", ["bot00"])            # logs/chief does not exist
    L.anchor()
days = os.path.join(root, "anchors", "days"); doc = open(os.path.join(days, os.listdir(days)[0])).read()
for n in range(1000):
    d = (gblog._dt.date(2020, 1, 1) + gblog._dt.timedelta(days=n)).isoformat()
    open(os.path.join(days, d + ".json"), "w").write(doc)
calls, real = [], gblog._read_json
gblog._read_json = lambda p, *a, **k: (calls.append(p), real(p, *a, **k))[1]
t = time.perf_counter(); rc = gblog.main(["--root", root, "verify", "--bot", "bot00", "--skip-archives"])
print(rc, "%.2f s" % (time.perf_counter() - t), sum(os.sep + "days" + os.sep in p for p in calls), "rollup parses")
```

Expected today: about 1 000 rollup parses for one healthy `verify --bot`. After the fix: 1 (the `_rollup_heads` read).

**Fix (all identical-output).**

1. **Targeted lookup for `--bot`.** Give `anchored_missing`, `orphan_witnesses` and `missing_chains` an optional `only: Optional[Set[str]]`, and have `lost_chains_of` pass `set(gone)`.
   - State, history and anchor files: `continue` for names not in `only` before any read.
   - Rollups: read the bytes with `_open_regular` and the same 16 MiB cap, and skip the parse unless the file can name a wanted chain:

     ```python
     needles = [b'"' + n.encode("ascii") + b'"' for n in only]      # chain keys are [A-Za-z0-9._-]
     if b"\\" not in raw and not any(nd in raw for nd in needles):
         continue                                                   # no escape can hide a name, none is spelled out
     data = <the same decode + json.loads + isinstance(dict) as _read_json>
     ```

   Results per name are independent of other names, so the returned lines are unchanged. Keep the directory listings as they are, so the `(witness files)` behaviour does not change.

2. **Memoise `gone(name)`** in a dict inside `orphan_witnesses`: C `isdir` calls instead of C·Y.

3. **`anchored_missing`: test `os.path.isdir(logs/<bot>)` before `read_anchor(bot)`.** A healthy ledger then parses no anchor file.

4. **`--all` path: pre-filter rollups without a full parse.** Extract the candidate names with `rb'"bot"[ \t\r\n]*:[ \t\r\n]*"([^"\\]*)"'`. Skip the file when it contains no backslash and none of the candidates is gone. Otherwise parse as today. The estimated gain is 2–4× per file.

5. **`_rollup_heads`: per-process memo of parsed rollups** keyed by `(path, st_ino, st_mtime_ns, st_size)`. `_atomic_json` always creates a new inode, so one `os.stat` under the lock validates the entry. `verify --all` then parses the newest rollup once instead of C times.

**Tests** (`tests/test_k13.py`).
- `test_verify_bot_reads_one_rollup`: the reproduction above with 40 rollups and `_read_json` (or the new byte reader) wrapped; assert at most one rollup parse.
- `test_targeted_witness_scan_matches_full_scan`: a matrix of ledgers covering:
  - a gone chain named only in an old rollup;
  - the same with compact JSON (`"bot":"x"`);
  - the same with an escaped name (`"b\u006ft0"`) or escaped key;
  - a rollup that is not UTF-8;
  - a rollup larger than 16 MiB;
  - a FIFO in place of a rollup;
  - a state witness, a history witness and an anchor witness.

  Assert `lost_chains_of(name)` equals `{n: missing_chains()[n]}` computed by a copy of today's functions kept in the test.
- `test_anchored_missing_parses_no_anchor_of_present_chains`: wrap `read_anchor`; assert zero calls on a healthy ledger and identical results with one chain deleted.
- `test_rollup_memo_sees_a_rewritten_rollup`: verify chain A, run `anchor --bot B` (rewrites today's rollup), verify chain B in the same process; assert the new head is checked.
- Existing: `RollupWitnessTest`, `test_verify_all_survives_unlistable_witness_directory`, `test_anchor_bot_fails_for_deleted_chain_with_witness` must pass unchanged.

---

## K13-03 — `log_files()` and `bots()` stat every entry; readers call them per chain and several times per command (Medium)

**Where.** `Ledger.log_files`, `Ledger.bots`. Callers include `records`, `chain_records`, `tail_records`, `_task_context`, `_task_status`, `tune_history`, `_ReplayedIds.refresh`, `sync` and `_anchor`.

```python
paths = (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl"))
return sorted(p for p in paths if os.path.isfile(p))            # one stat per day file, then callers filter by name
```
```python
return sorted(d for d in os.listdir(self.logs_dir) if os.path.isdir(os.path.join(self.logs_dir, d)))
```

**Problem.** Every caller pays D `stat` calls per chain and then throws most paths away:

- `status` reads only the last 30 days but stats C·D files.
- `report --date` does it twice (`records`, then `_task_context`).
- `tail -n 1` stats C·D files.
- **Every logging command with a task id** stats D files per variant chain through `_warn_task` → `_task_status`, and C directories through `chains_for` → `bots()`. DESIGN §13 says "older day files are not opened"; they are still stat-ed.
- `spool replay` calls `log_files()` once per event through `_ReplayedIds.refresh`: S·D stats.

Estimate for C = 50 and D = 1 100: about 55 000 stats (about 0.1–0.15 s) per `status` or `tail`, about 0.3 s per `report --date`. For C = 200, four times that.

**Reproduction.** Build 50 chains × 1 100 one-line day files with the bench writer. Then run `strace -c -e trace=stat,newfstatat,statx ./bin/gblog status`, or wrap `os.path.isfile` and count calls during `gblog.main(["status"])`. Expected today: at least C·D calls.

**Fix.** Use `os.scandir`. `DirEntry.is_file()` and `is_dir()` answer from `d_type` without a syscall for regular entries and follow symlinks exactly as `os.path.isfile` and `isdir` do. Filter by name before the type test.

```python
def log_files(self, bot: str, since: Optional[str] = None) -> List[str]:
    d = os.path.join(self.logs_dir, bot)
    try:
        it = os.scandir(d)
    except (FileNotFoundError, NotADirectoryError):
        return []                                   # what `not os.path.isdir(d)` returned
    names = []
    with it:
        for entry in it:
            name = entry.name
            if not name.endswith(".jsonl") or (since is not None and name[:10] < since):
                continue
            try:
                regular = entry.is_file()           # follows symlinks, like os.path.isfile
            except OSError:
                regular = False                     # os.path.isfile swallows these too
            if regular:
                names.append(name)
    return sorted(os.path.join(d, n) for n in names)
```

Other errors from `scandir` (for example `PermissionError`) propagate as `os.listdir` did. `bots()` gets the same shape with `entry.is_dir()`. Pass `since=` from `chain_records`, `records(since=…)`, `_task_status` and `_task_context`; the caller-side filters stay, so results cannot change.

**Tests.**
- `test_log_files_matches_listdir_semantics`: a chain directory holding a regular file, an empty file, a directory named `x.jsonl`, a FIFO, a symlink to a regular file, a dangling symlink and a `.jsonl.bak`. Assert equality with a copy of today's expression, and the same for `since=`.
- `test_status_does_not_stat_old_day_files`: 400 day files; wrap `os.stat` and `os.path.isfile`; assert that calls on paths below `logs/` are O(selected files).
- `test_log_files_propagates_permission_error` (POSIX, skipped as root).
- Re-run `TailLookupTest`, `ReaderGapTest`, `test_disk_name_unit` and the `gblog.os.listdir` patches in `test_round4` and `test_specialist`; none of them patch the calls this change replaces.

---

## K13-04 — `coverage`: quadratic "elsewhere" pass; `strptime` per record (Medium)

**Where.** `coverage()`, `_ts_epoch()`.

```python
others = shared.get((a.get("action"), norm_target(a.get("action"), a.get("target"))), ())
lo_t, hi_t = span(a)
if any(b != a.get("bot") and lo_t <= t <= hi_t for t, b in others):     # scans every self-report of that target
```
```python
return _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc).timestamp()
```

**Problem.**

1. For every unmatched browser or file capture, the pass walks all self-reports with the same normalised target, over all Bots and the whole loaded range. `tune` loads at least 90 days; `coverage` up to `--days` + 90. For a browser record the target is the host. A single-Bot ledger can never produce a hit, yet it pays U × S. With 8 000 unmatched `github.com` visits and 10 000 self-reports that is 80 M tuple tests, about 5–8 s.
2. `_ts_epoch` runs `datetime.strptime` (about 5–9 µs, pure Python) once per self-report and two to four times per captured record (`span()` is called again in the second pass). At 100 000 loaded records that is about 1 s, a large share of `coverage` and `tune` after parsing.
3. Smaller: in the main loop, used entries inside the window are skipped linearly. This is only visible with tens of same-target reports per minute.

**Reproduction.**

```python
import time, gblog
D = "2026-01-%02dT%02d:%02d:00Z"
selfs = [{"bot": "ops", "type": "action", "action": "browser", "target": "https://github.com/a/%d" % i,
          "ts": D % (1 + i // 1440, i // 60 % 24, i % 60)} for i in range(20000)]
autos = [{"bot": "ops", "type": "action", "action": "browser", "source": "auto", "capture": "browser",
          "target": "https://github.com/b/%d" % i, "ts": "2026-02-10T00:00:00Z",
          "observed": "2025-06-01T00:00:00Z"} for i in range(20000)]
t = time.perf_counter(); cov = gblog.coverage(selfs + autos)
print(cov["auto"], cov["matched"], cov["elsewhere"], "%.1f s" % (time.perf_counter() - t))   # 400 M tuple tests today
```

**Fix.**

1. **Elsewhere pass.** While building `shared`, also keep `shared_bots[key]` (a set; these values are hashable because they were already part of an `index` key) and `shared_ordered[key]` (times non-decreasing, as `in_order` does). Then:

   ```python
   bots_here = shared_bots.get(key, ())
   if not bots_here or (len(bots_here) == 1 and a.get("bot") in bots_here):
       hit = False                                        # only this Bot ever reported that target
   elif shared_ordered[key]:
       lo = bisect_left(shared_times[key], lo_t); hi = bisect_right(shared_times[key], hi_t)
       hit = any(b != a.get("bot") for b in shared_botlist[key][lo:hi])
   else:
       hit = any(b != a.get("bot") and lo_t <= t <= hi_t for t, b in others)   # today's scan
   ```

   The predicate is the same; only the candidate range shrinks. Keep `(lo_t, hi_t)` from the first pass on the record's slot rather than calling `span()` again.

2. **`_ts_epoch` fast path** for the shape gblog writes, with a per-day cache. Anything else falls through to today's code, so the returned floats are identical.

   ```python
   _TS_FIXED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\Z")
   _DAY_SECONDS: Dict[str, Optional[int]] = {}

   head = ts[:19]
   if _TS_FIXED_RE.match(head):
       day = head[:10]
       base = _DAY_SECONDS.get(day, _DAY_SECONDS)             # the dict itself means "not cached"
       if base is _DAY_SECONDS:
           try:
               base = (_dt.date(int(day[:4]), int(day[5:7]), int(day[8:])).toordinal() - 719163) * 86400
           except ValueError:
               base = None                                    # not a calendar day: the slow path answers 0.0
           if len(_DAY_SECONDS) > 4096:
               _DAY_SECONDS.clear()
           _DAY_SECONDS[day] = base
       h, m, s = int(head[11:13]), int(head[14:16]), int(head[17:19])
       if base is not None and h < 24 and m < 60 and s < 60:
           return float(base + h * 3600 + m * 60 + s)
   # ... existing strptime code, unchanged
   ```

3. Optional, for ordered lists only: a "next unused" skip pointer with path compression per index list, starting at `bisect_left(times, lo_t)`. Leave the unordered path as it is.

**Tests.**
- `test_ts_epoch_fast_path_equals_strptime`: 5 000 random valid timestamps, plus:
  - `0000-01-01T00:00:00Z`, `2024-02-30T…`, `…T24:00:00Z`, `…T10:60:00Z`, `…T10:00:60Z`;
  - offsets, a space instead of `T`, `"2024-06-01T10:00:00Z\n## x"`;
  - non-ASCII digits, `""`, `None`, `5`.

  Assert equality with a copy of today's function.
- `test_elsewhere_matches_reference`: 3 000 random records over 3 Bots, mixing `capture` values, `observed` and `observed_from`. Assert that `rows`, `unmatched`, `elsewhere`, `auto`, `matched` and `self_only` equal a reference that includes today's second pass. Extend `_reference_coverage` in `test_mutation_gaps.py`.
- `test_single_bot_capture_is_not_quadratic`: run the reproduction in a child process with a 20 s timeout, in the style of the PEM guard.
- Existing: `CoverageRulesTest`, `CoverageDeepTest`, `test_coverage_ignores_plain_gblog_commands`.

---

## K13-05 — Archive re-hash and sync content check use one core (Medium)

K4-05 and K4-14 recorded the O(bytes) cost and the two rejected shortcuts (rotation needs sign-off; no cache). This item is the part that needs neither: the same full check on all cores.

**Where.** `verify_archives` (loop over `_gunzip_sha256`) and `_sync_drift` (loop over `_git_blob_id`).

```python
found = _gunzip_sha256(full, limit)            # one file at a time; inflate and SHA-256 both release the GIL
```
```python
ok = cache.get(rel) == entry or _git_blob_id(full, size, len(sha)) == sha    # one file at a time; SHA-1 releases the GIL
```

**Problem.** With 10 000 archive files, or about 55 000 after three years of daily transcripts for 50 Bots, `verify --all --record` and `sync` each read every byte once per day, sequentially. zlib inflate, hashlib updates larger than 2 KiB and file reads all run without the GIL, so threads scale. Estimate for 10 000 files × 50 KB: about 3 s sequential in `verify` and about 1.5 s in `sync` with a warm cache; much more with a cold cache.

**Reproduction.** Use bench scenario `--archives 10000 --archive-bytes 50000` (K13-11). Then run `time gblog verify --all`, `time gblog verify --all --skip-archives` and `time gblog sync --no-push`. The difference between the first two is the archive cost.

**Fix.**

- `verify_archives`: keep the loop for the cheap, ordered checks. Where the loop reaches `_gunzip_sha256`, store a job `(slot, rel, full, limit, sha)` in place of a problem string. After the loop, run the jobs through `concurrent.futures.ThreadPoolExecutor(max_workers=jobs).map(...)`. The worker catches `(OSError, EOFError, zlib.error)` and returns the same problem text. Emit the problems in slot order. Any other exception propagates as today.
- `_sync_drift`: compute the stat and size rules sequentially, hash the candidates with the same pool, and build `drift` in `sorted(checked)` order.
- `jobs = min(8, os.cpu_count() or 1)`. `GBLOG_JOBS` overrides it, and `1` means inline with no pool. Import `concurrent.futures` lazily and only above about 64 jobs, so small ledgers and K13-01 are unaffected.

Output is identical because the results are collected by position. The expected gain is close to the core count for large files and 2–3× on 4 cores for many small ones. On a 2-vCPU host the gain is at most 2×.

**Tests.**
- `test_parallel_archive_check_matches_sequential`: 300 archive records covering intact, missing, corrupt deflate body, wrong hash, oversized versus `bytes`, FIFO, symlink, duplicate `(path, sha)`, same path with another sha, and no `bytes`. Assert that `verify()` results are equal for `GBLOG_JOBS=1` and `GBLOG_JOBS=4`, including the order of `problems`.
- `test_parallel_drift_matches_sequential`: the `RemoteChangeAndDriftTest` cases under both settings.
- `test_worker_exception_still_aborts_only_that_chain`: `_gunzip_sha256` raising `RuntimeError` for one file gives `verify aborted: RuntimeError` for that chain; the others stay OK.
- `test_jobs_1_creates_no_pool`: patch `concurrent.futures.ThreadPoolExecutor` to raise.
- Existing `test_oversized_archive_is_a_mismatch_without_being_read_to_the_end` patches `gzip.GzipFile.read`; run it with both settings.

---

## K13-06 — Name resolution is O(R·V) per call and runs once per chain, several times per command (Medium/Low)

**Where.** `Ledger.resolve`, `chains_for`, `canonical`, `label`, `groups`; the `tail` printing loop.

```python
for slug, e in bots.items():
    if low in [str(a).lower() for a in e.get("aliases", [])]:      # a new list per entry, per call
        return slug, True
```
```python
existing = [c for c in self.valid_chains() if self.canonical(c) == slug]     # one resolve() per chain directory
```

**Problem.** `resolve()` makes five passes over the registry. `chains_for()` calls it once per chain directory and re-lists `logs/` each time. A single `report --bot X` runs `chains_for` three times (`_warn_unknown_bot`, `records`, `_task_context`); a logging command runs it once in `_warn_task`. `tail` calls `ledger.label()` per printed record.

Estimate: with R = 200 Bots, V = 3 and 600 chains, one `chains_for` is about 40 ms, so about 0.1 s is added to every `report` and `status`, and 40 ms to every `log` with a task id. With R ≤ 20 it is noise.

**Reproduction.** Write a registry of 300 Bots × 4 aliases and 1 200 empty chain directories. Then `python3 -m timeit -n 5 -s 'import gblog; L = gblog.Ledger(ROOT); L.cache_aliases = True' 'L.chains_for("bot150")'`.

**Fix.** Build a lookup index once per registry read and store it beside the cached registry. With `cache_aliases` off, build it per call; that costs what one resolve costs today. Use `setdefault` in file order so "first entry wins" is preserved:

```python
idx = {"display": {}, "alias": {}, "alias_low": {}}
for slug, e in bots.items():
    if isinstance(e.get("display"), str):
        idx["display"].setdefault(e["display"], slug)
    for a in e.get("aliases", []):
        idx["alias"].setdefault(a, slug)
        idx["alias_low"].setdefault(str(a).lower(), slug)
```

`resolve()` then becomes the same precedence chain with O(1) steps:

1. `raw in bots`
2. `low in bots`
3. `idx["display"].get(raw)`
4. `idx["alias"].get(raw)`
5. `idx["alias_low"].get(low)`
6. sanitise, then `cleaned.lower() in bots`

Drop the index wherever `_aliases_cached` is reset. In `tail`, cache labels in a dict keyed by `r["bot"]`.

**Tests.**
- `test_resolve_index_matches_reference`: 300 random registries, including the conflict shapes from `alias_conflicts()`: slug equal to another Bot's alias, shared display names, display equal to an alias, case variants. Use random probe names. Assert equality with a copy of today's `resolve`.
- `test_alias_add_then_resolve_in_one_process`: `add_alias`, `remove_alias` and `set_alias_strict` followed by `resolve` with `cache_aliases=True`; assert fresh results.
- Existing: `AliasRegistryTest`, `ReaderAliasTest`, `test_slug_beats_alias_regardless_of_order`.

---

## K13-07 — `state rebuild` holds the parsed chain in memory under the exclusive lock (Low/Medium)

**Where.** `Ledger.rebuild_state`, `_chain_items`; also `seal_break` and the CLI `seal` / `state rebuild` paths.

```python
with self.lock(bot):
    items = self._chain_items(bot)                 # every record of the chain, parsed, in one list
    ...
    tail = items[-1][2]
    ...
    known = any(rec.get("hash") == old_head for _rel, _lineno, rec in items)
```

**Problem.** K4-02 removed the whole-chain list from `verify`. `rebuild_state` still builds one, while holding the exclusive lock, so every writer of that chain waits. At 2.0–2.4 KB per parsed record that is about 220 MB for N = 100 000 and about 1.1 GB for N = 500 000. Only the tail record and one membership test are needed.

Also, neither `seal` nor `state rebuild` sets `verify_want_hashes`, so the `verify()` they call keeps two sets of N hashes (tens of MB at N = 500 000). `seal_break` parses the chain three times, once inside the lock; it runs once per ledger lifetime and is not worth changing.

**Reproduction.** Run `python tests/bench_large_ledger.py --records 500000 --keep`, delete `state/bench.json`, then run `gblog state rebuild --bot bench` under `/usr/bin/time -v` (or `tracemalloc` around `Ledger.rebuild_state`). Peak RSS is proportional to N.

**Fix.**

```python
with self.lock(bot):
    _path, tail = self._tail_record(bot)                     # same record as items[-1][2]
    if tail is None:
        raise StateRebuildError("refusing: no records for %s; nothing to rebuild from" % bot)
    ...                                                      # unchanged checks on `tail`
    if old_head != GENESIS:
        needle = old_head.encode("utf-8", "backslashreplace")
        known = any(rec.get("hash") == old_head
                    for _p, rec in self._scan(self.log_files(bot), needle))   # raw scan; parses only lines that hold the hash
```

`_last_line` plus `_parse_line` returns what the last element of `_chain_items` held, because both skip blank lines. A record whose `hash` equals `old_head` carries those 64 hex characters literally in its line, so the scan finds exactly the lines the list search did. In the CLI, set `ledger.verify_want_hashes = []` before `seal` and `state rebuild`.

**Tests.**
- `test_rebuild_state_does_not_materialise_the_chain`: patch `Ledger._chain_items` to raise `AssertionError` and run every branch: state missing, unparsable, non-string head, genesis, behind (`known`), right head with wrong seq, ahead (refused), no-op, and "log changed while checked".
- `test_rebuild_results_unchanged`: for each branch, compare the printed line, the note detail and the state file with the values the current tests assert (`RecoveryTest`, `StateRepairTest`, `test_state_seq_mismatch_warns_and_can_be_rebuilt`).
- Bench: peak RSS of `state rebuild` for 50 000 and 500 000 records (K13-11).

---

## K13-08 — Task-status check parses every line that carries the task id (Low)

**Where.** `_task_status`, called through `_warn_task` / `_task_state` by `log`, `approval`, `note`, `archive`, `task-end` and `task-start --task-id`.

```python
for raw in fh:
    budget -= len(raw)
    if needle in raw:
        rec = _parse_line(raw)                                  # every action, note and approval of the task
        if rec.get("task_id") == stored and rec.get("type") in ("task_start", "task_end"):
            last = (...)
```

**Problem.** Each command reads today's day file from the start; once the id is found, the 8 MiB budget no longer applies. It also JSON-parses every earlier record of the same task. That is O(k) parses per command and O(k²) per task. With the usual k ≤ 500 this is under 2 ms. For a long-lived task id (a stale exported `GBLOG_TASK`, or a routine that logs thousands of actions under one id), k = 20 000 costs about 60 ms per command. A day file pinned by a future-dated tail grows without bound and is re-read by every command.

**Reproduction.** Write one `task_start` followed by 20 000 `action` records with the same `task_id` into today's file (bench-style raw writer). Wrap `gblog._parse_line` with a counter and call `gblog._task_status(ledger, "ops", tid)`. Today: 20 001 parses.

**Fix (identical output).** Parse a line only when it can decide the answer:

```python
if needle in raw and (b"task_start" in raw or b"task_end" in raw or b"\\u" in raw):
    rec = _parse_line(raw)
```

The `\u` clause keeps hand-written lines that spell the type with an escape; `_ReplayedIds.refresh` uses the same guard.

**Not proposed as identical.** Reading today's file backwards and stopping at the newest `task_start` / `task_end` would make the common case O(distance to that record). It would also turn some of today's `unsure` answers into `open` or `closed`, namely when the deciding record lies beyond the first 8 MiB. That prints warnings that are not printed today, so it needs your decision.

**Tests.**
- `test_task_status_parses_only_start_and_end_lines`: the reproduction; assert at most 2 parses and the same result.
- `test_task_status_matrix_unchanged`: open, closed, unknown, closed in a variant chain, id only as a prefix of another id, `\u`-escaped type line, budget exhaustion with `_TASK_SCAN_BYTES` patched small. Compare with a copy of today's function.
- Existing: `TaskStatusTest`, `TaskIdDiagnosticsTest`.

---

## K13-09 — Redaction runs ~27 regex passes over every byte of an archive (Low; measure first)

**Where.** `redact()`, `_SECRET_PATTERNS`, called by `archive_file` and by `append` for every string field.

```python
out = _redact_pem(text)
for pat, repl in _SECRET_PATTERNS:
    out = pat.sub(repl, out)
```

**Problem.** About 16 of the rules start with `\b`, a look-behind or a cased literal under `(?i)`. For these, `sre` has no literal or charset prefix and tries a match at every position. K4-04 removed the quadratic cases; the constant remains. Estimate: about 1 MB/s end to end. A 5 MB transcript then costs about 5 s of CPU in `gblog archive`, and the daily routine archives one transcript per Bot. This estimate is the least certain in this review; measure before acting.

**Reproduction.** `python3 -m timeit -n 1 -r 3 -s 'import gblog; t = open("sample-transcript.jsonl", encoding="utf-8").read() * 20' 'gblog.redact(t)'` and divide the size by the time. Bench scenario `--redact-mb` (K13-11) reports MB/s.

**Fix (exact, additive).** Give each rule a necessary literal condition and skip `pat.sub` when it cannot hold. `str.__contains__` runs at memory speed.

- **Case-sensitive vendor rules.** A match must contain the literal:

  | Rule | Literal(s) |
  |---|---|
  | GitHub tokens | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` |
  | GitHub fine-grained | `github_pat_` |
  | Slack | `xox` |
  | AWS | `AKIA` |
  | `sk-…` / `xai-…` | `sk-` / `xai-` |
  | Google | `AIza`, `ya29.` |
  | Stripe | `k_live_`, `k_test_` |
  | GitLab / npm | `glpat-` / `npm_` |
  | Slack app token | `xapp-` |
  | URL userinfo | `://` |

- **Keyword rules** (the six whose keyword set is a subset of `_ENV_KW`): evaluate `_ENV_KW_ANY_RE.search(text)` once per `redact()` call and skip all of them when it is `None`. This is the test `_redact_env_names` already performs, with the same engine and flags, so it is exact.

- **Other `(?i)` literals** (`bearer`, `authorization`, `hooks.slack.com/services/`): pre-filter only when `text.isascii()` (O(1) in CPython), using `text.lower()`. For non-ASCII text run the rule as today. `re.IGNORECASE` lets `İ`, `ı`, `ſ` and the Kelvin sign match ASCII letters, which `lower()` does not reproduce.

**Tests.**
- `test_redact_prefilter_matches_plain_loop`: the 12 000-sample generator of `LinearRedactionTest`, with the alphabet extended by every literal above and by `İ ı ſ K`. Assert equality with a test-local loop that applies every rule unconditionally.
- `test_redact_every_documented_shape_still_redacted`: re-run `ExtendedRedactionTest`, `RedactionGapTest` and `RedactionTest` unchanged.
- Bench line `redact MB/s` before and after.

---

## K13-10 — Smaller items (Low)

1. **fsync fan-out per chain.** `sync` ends with `update_state(b, {"last_sync": …})` for every chain: one temp-file fsync and one `state/` directory fsync each. `anchor` does three fsyncs per chain. With C = 1 000 that is 2 000–3 000 fsyncs per run. Fix with identical on-disk result: let `write_state` and `_atomic_json` take `fsync_dir=False` inside these two loops, and fsync `state/` and `anchors/` once after the loop.

2. **Anchor history parsed twice per verify.** `_anchor_heads(snapshot)` and `_anchor_checks(...)` each run `_parse_line` over every line of `anchors/<bot>.history.jsonl`. Parse once in `_anchor_snapshot` (after the lock is released) and pass the entries on. This matters only when `anchor` runs much more often than daily.

3. **`verify --record` detail is unbounded.** `detail = "; ".join(res["problems"])` writes every problem of a FAIL chain into a new record on every run. A chain with thousands of breaks adds a line of hundreds of KB per day; that feeds K4-03 and K4-12. Capping it (first 20 problems plus `"; N more"`) changes new records only, but it is a behaviour change and needs your sign-off.

4. **`coverage` reads its window twice.** When an `observed` day lies before `--days`, the CLI calls `ledger.records()` again from the older floor and re-parses the window. An `until=` parameter would let it read only the older part and concatenate; all older records sort before the window, so the result is the same list.

5. **Listing memo.** After K13-03 and K13-06, a per-command memo of `valid_chains()` is not worth its invalidation risk: `append` creates chain directories mid-command and `_warn_task` must see them. Do not add one.

---

## K13-11 — Benchmarks and guards to add

**Extend `tests/bench_large_ledger.py`** (still not collected by unittest). The lines for timed gblog commands must keep the `… exit <rc>, …` form that the CI step greps.

| Flag | Scenario | Reports |
|---|---|---|
| `--cli-appends N` | spawn `bin/gblog note` N times, and `python3 gblog.py note` N times, on the generated ledger | ms per call for each launcher (K13-01) |
| `--chains C --aliases V` | C chain directories, a registry with V aliases per Bot, some slugs without a directory | `status`, `tail -n 1`, `report --date yesterday`, `verify --bot <chain>`, `verify --all`, `anchor --skip-archives` (K13-02, K13-03, K13-06) |
| `--rollups Y` | copy the day rollup to Y dated files | the `verify --bot` and `verify --all` lines again (K13-02) |
| `--day-files D` | spread padding over D day files | the reader lines again (K13-03) |
| `--archives A --archive-bytes B` | A archive records with real `.gz` files, written without fsync | `verify --all` with and without `--skip-archives`; `sync --no-push` when git is present; both with `GBLOG_JOBS=1` and the default (K13-05) |
| `--long-task K` | one task with K action records in today's file | one `note --task-id` call in-process (K13-08) |
| `--capture-heavy` | 20 000 self-reports and 20 000 unmatched browser captures for one host | `coverage`, `tune --stdout` (K13-04) |
| `--redact-mb M` | M MiB of synthetic transcript text | `redact` MB/s (K13-09) |
| `--rss` | run each timed command in a child via `python -c "import resource, sys, gblog; rc = gblog.main(sys.argv[1:]); sys.stderr.write('maxrss %d\n' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss); sys.exit(rc)"` | peak RSS per command, including `state rebuild` (K13-07) |

**CI.** Extend the existing benchmark step with one small run, for example `--records 500 --appends 5 --chains 8 --aliases 2 --rollups 20 --archives 20 --long-task 200 --cli-appends 3`. Timings are not checked; exit codes are.

**Deterministic complexity guards** (unit tests, counting calls rather than time, in the style of `test_many_open_tasks_do_not_rescan_every_task`):

- rollup parses per `verify --bot` at most 1 (K13-02);
- `os.stat` / `os.path.isfile` calls below `logs/` during `status` are O(selected files) (K13-03);
- `_parse_line` calls in `_task_status` for a 2 000-record task at most 2 (K13-08);
- `_chain_items` not called by `rebuild_state` (K13-07);
- `read_anchor` not called by `anchored_missing` on a healthy ledger (K13-02).

**Documentation.** DESIGN §13 should gain:

- one row for the process cost per invocation (K13-01);
- a corrected `append` row: "older day files are not opened" should also say whether they are stat-ed (K13-03);
- the Y-dependence of `verify --bot` (K13-02).

---

## Looked at and not reported

- **`Ledger.verify` walk.** One parse and one hash per record, no retained records, lock held only for the snapshot. The remaining cost is the per-record constant. `object_pairs_hook` in `_parse_line_dups` adds some; it is needed for the duplicate-key warning.
- **`_tail_record`, `_last_line`, `_append_line`.** O(1) in N; the `sorted()` over D names is negligible.
- **`render_report`** (after K4-08) and **`spool_replay`** (after K4-07; it benefits further from K13-03).
- **Replacing `gzip.GzipFile` with `zlib.decompressobj(wbits=31)`.** It would save about 5–10 % per archive but changes the exception class in `unreadable (<class>)` problem lines (`error` instead of `BadGzipFile`). Not identical; do not do it.
- **A regex look-ahead to thin out `_redact_env_names` candidates.** It would bring back quadratic backtracking on long `_`/`-` chains, which K4-04 removed.
- **A persisted index of rollup names.** It would be faster than K13-02 fix 4, but it is a cache beside the evidence, which the project has declined for `sync`. Not proposed.

## Suggested order

1. K13-11 bench flags `--cli-appends`, `--chains`, `--rollups`, `--rss`. This turns the estimates above into numbers.
2. K13-01 fix 1 (one line in `bin/gblog`) and K13-02 fixes 1–3. Largest gain, smallest change.
3. K13-03 and K13-04. Local, with differential tests.
4. K13-07, K13-08, K13-06.
5. K13-05 and K13-09 after measurement; K13-01 fix 2 and K13-10 item 3 after your decision.