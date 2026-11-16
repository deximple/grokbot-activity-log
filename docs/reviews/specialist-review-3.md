# Specialist review K3 — 2026-11-16

**Area:** backward compatibility and migration: ledgers written by 0.2–0.5 and 0.6.0, optional new fields, old alias registries and anchors, missing state.
**Tree reviewed:** the current `gblog.py` (reports `0.6.1`), with the docs and tests around it.
**Method:** code reading only; nothing was executed. Each "today" result below is a trace of the current code, and the snippets are quoted from it. I had no access to the 0.2–0.5 sources, so claims about what those versions wrote come from the CHANGELOG, the legacy tests and the fixture builder; where that leaves doubt, the finding says so.

## Summary

| Id | Severity | Finding | Kind |
|---|---|---|---|
| K3-01 | **High** | A pre-0.6 tail with a free-form `ts` is ignored by the monotonic guard and by day-file selection. The first append after the upgrade can land in an earlier day file: permanent FAIL, and every later append exits 4. | new false FAIL, brick |
| K3-02 | **Medium** | The upgrade documentation describes behaviour this tree does not have (D1, D3, D4 items), and lists as open items the code has already fixed. | migration guidance |
| K3-03 | Medium (macOS, Windows) | On a case-insensitive file system, `alias add` (lower-cased slug) or name-case drift puts records with `bot: "ops"` into directory `Ops`. `verify --contains` then reports a rollback that did not happen. | new false FAIL |
| K3-04 | Medium | `sync --remote` with an equivalent spelling of the current origin (`…/r` vs `…/r.git`, https vs ssh for the same GitHub repository) exits 2 before the local commit. | new refusal |
| K3-05 | Low | One invalid entry in `state/aliases.json` disables the whole registry: every display name starts a new chain and strict mode is off. | silent chain split |
| K3-06 | Low | `verify_archives`: the informational `bytes` field became a hard read limit; a backslash `path` is checked for shape but opened unnormalised. | possible false FAIL |
| K3-07 | Low | Any directory under `logs/` that is not a chain (`lost+found` on a mount, sync-tool folders) is a permanent `verify --all` FAIL, even when it holds no log file. | new false FAIL |
| K3-08 | Low | No test uses a ledger shaped like 0.2–0.5 output. A new module is proposed. | test gap |

No silent bypass specific to legacy data was found beyond what the CHANGELOG already lists as open (D1-07, D1-10).

---

## K3-01 — High — A legacy tail with a non-normalised `ts` can brick the chain on the first append after the upgrade

This extends the open item D1-09 ("after a pre-0.6 tail with a UTC offset, `append` can write a record that fails `verify`", rated below Medium). Two consequences are not in that entry: the record can be filed in an earlier day file, which bricks the chain, and the explicit `--ts` and `archive` paths have the same hole.

### Where

`Ledger.append`:

```python
            tail_ts = tail.get("ts") if tail else None
            if not _is_norm_ts(tail_ts):
                tail_ts = None
            ...
            else:
                ts = wall
                if tail_ts and wall < tail_ts:
                    ts, regressed = tail_ts, True
            ...
            path = os.path.join(self.logs_dir, bot, today(ts) + ".jsonl")
```

`archive_file` has the same test (`if _is_norm_ts(_tail_ts) and ts < _tail_ts`).

Before 0.6 `--ts` was stored as written, and the day file was `ts[:10]`. `verify` compares `ts` as text (`if ts < last_ts`) and walks day files in name order. When the tail's `ts` is not in normalised form, `append` drops both protections:

- **Text order.** The new `ts` (wall clock, UTC) can sort below the tail's text.
- **File order.** The new record can go to a day file whose name sorts before the tail's file.

### Who is affected

Anyone whose last pre-upgrade record carries a local-time `--ts` (`…+09:00`, or a local time with no zone) east of UTC.

- **Same UTC day:** any append within the offset (9 hours for KST) after that record is a permanent `timestamp goes backwards`.
- **Local date ahead of the UTC date** (00:00–09:00 local for KST): the chain is bricked.

Neither state has a supported repair: recovery is append-only and `seal` is pinned.

### Reproduction (brick)

```python
raw_chain(root, "ops", ["2024-06-02T08:00:00+09:00"], "2024-06-02")   # a 0.5 record: 23:00Z on 06-01
with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T23:30:00Z"):
    ledger.append("ops", {"type": "note", "detail": "first record after the upgrade"})
```

Trace of today's code:

1. Tail and state match, so the append proceeds. `tail_ts` is `None`, `ts = wall`.
2. The record (seq 2) is written to a new file `2024-06-01.jsonl`, which sorts before the tail's `2024-06-02.jsonl`.
3. `verify` reads seq 2 first: `broken chain (prev=… expected=genesis)`. FAIL, permanently.
4. The next append: `_tail_record` takes the newest file name (`2024-06-02.jsonl`, seq 1), state says seq 2, and the roll-forward test fails. `TailMismatchError`, exit 4, on every append from then on.
5. `state rebuild` refuses with `chain does not verify`.

Same-day variant: tail `2024-06-01T23:30:00+09:00`, wall `2024-06-01T15:00:00Z`. The record goes to the same file and gives `logs/ops/2024-06-01.jsonl:2: timestamp goes backwards` forever.

### Fix

Do not change `verify`. Its text comparison is the frozen rule, and comparing instants would newly FAIL legacy chains that are text-ordered (a `-05:00` record followed by a UTC one). Instead, make the writer compute the bound the same way `verify` reads it.

```python
def _ts_floor(tail: Optional[Dict], tail_path: Optional[str]) -> Optional[str]:
    """Smallest normalised timestamp the next record of a chain may carry, or None.

    verify compares ``ts`` as text and walks day files in name order, so the bound is textual:
    the new ``ts`` must not sort below the tail's ``ts`` as written (a pre-0.6 tail can be
    ``2026-11-17T08:00:00+09:00`` or ``2026-11-17 08:00``), and the record must be filed in the
    tail's day file or a later one."""
    floor = None
    raw = tail.get("ts") if isinstance(tail, dict) else None
    if _is_norm_ts(raw):
        floor = raw
    elif isinstance(raw, str) and _is_date(raw[:10]):
        day = _dt.datetime.strptime(raw[:10], "%Y-%m-%d")

        def at(seconds: int) -> str:
            w = day + _dt.timedelta(seconds=seconds)
            return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (w.year, w.month, w.day, w.hour, w.minute, w.second)

        try:
            lo, hi = 0, 86400              # at(86400) is the next midnight: above any text with this date prefix
            while lo < hi:                 # normalised text order is time order, so the test is monotone
                mid = (lo + hi) // 2
                if at(mid) >= raw:
                    hi = mid
                else:
                    lo = mid + 1
            floor = at(lo)
        except OverflowError:
            floor = None
    name = os.path.basename(tail_path) if tail_path else ""
    if _DAY_FILE_RE.match(name) and _is_date(name[:10]):
        day_floor = name[:10] + "T00:00:00Z"
        if floor is None or day_floor > floor:
            floor = day_floor
    return floor
```

Wiring:

- `_append_head`: after `seen.append(raw)` add `seen.append(path)`. The list is already an out-parameter.
- `append`: replace the two `tail_ts` lines with `tail_ts = _ts_floor(tail, seen[1] if len(seen) > 1 else None)`. The rest (refusal of an explicit earlier `--ts`, clamp with `clock_regressed`, the future warning) works unchanged on the floor.
- `archive_file`: `_floor = _ts_floor(_tail, _tail_path)`, then `if _floor and ts < _floor: raise _past_ts_error(ts, bot, _floor)`.

The floor is minimal, so legacy spellings that already sort below every `T…Z` time of their day (`2024-06-01 23:30:00`, date-only) cause no clamping. A tail in a file that is not a day file gets no file bound; such chains were already refused by 0.4 and 0.5.

### Tests (new `tests/test_legacy_compat.py`; `raw_chain` and `Base` are in K3-08)

```python
class LegacyTimestampTest(Base):
    def _append(self, now, **fields):
        with mock.patch.object(gblog, "now_iso", return_value=now), redirect_stderr(io.StringIO()):
            return self.ledger.append("ops", dict({"type": "note", "detail": "after the upgrade"}, **fields))

    def test_offset_tail_in_a_later_day_file_does_not_brick_the_chain(self):
        raw_chain(self.root, "ops", ["2024-06-02T08:00:00+09:00"], "2024-06-02")
        rec = self._append("2024-06-01T23:30:00Z")
        self.assertEqual([os.path.basename(p) for p in self.ledger.log_files("ops")], ["2024-06-02.jsonl"])
        self.assertEqual((rec["ts"], rec["clock_regressed"]), ("2024-06-02T08:00:00Z", "2024-06-01T23:30:00Z"))
        self.assertTrue(self.ledger.verify("ops")["ok"])
        nxt = self._append("2024-06-02T09:00:00Z")          # today: TailMismatchError
        self.assertEqual((nxt["seq"], nxt["ts"]), (3, "2024-06-02T09:00:00Z"))
        self.assertNotIn("clock_regressed", nxt)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    def test_offset_tail_on_the_same_utc_day_keeps_verifying(self):
        raw_chain(self.root, "ops", ["2024-06-01T23:30:00+09:00"], "2024-06-01")
        rec = self._append("2024-06-01T15:00:00Z")
        self.assertEqual((rec["ts"], rec["clock_regressed"]), ("2024-06-01T23:30:00Z", "2024-06-01T15:00:00Z"))
        res = self.ledger.verify("ops")                    # today: timestamp goes backwards
        self.assertTrue(res["ok"], res["problems"])

    def test_legacy_spellings_that_sort_low_are_not_clamped(self):
        for n, raw in enumerate(("2024-06-01 23:30:00", "2024-06-01")):
            bot = "low%d" % n
            raw_chain(self.root, bot, [raw], "2024-06-01")
            with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T15:00:00Z"):
                rec = self.ledger.append(bot, {"type": "note", "detail": "x"})
            self.assertEqual(rec["ts"], "2024-06-01T15:00:00Z")
            self.assertNotIn("clock_regressed", rec)
            self.assertTrue(self.ledger.verify(bot)["ok"])

    def test_minute_precision_tail_gets_the_minimal_floor(self):
        raw_chain(self.root, "ops", ["2024-06-01T23:30Z"], "2024-06-01")
        rec = self._append("2024-06-01T23:30:10Z")
        self.assertEqual(rec["ts"], "2024-06-01T23:31:00Z")
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_explicit_ts_and_archive_use_the_same_bound(self):
        raw_chain(self.root, "ops", ["2024-06-01T23:30:00+09:00"], "2024-06-01")
        before = open(self.ledger.log_files("ops")[0], "rb").read()
        rc, out, err = run(["note", "--bot", "ops", "--detail", "x", "--ts", "2024-06-01T15:00:00Z"], self.root)
        self.assertEqual(rc, 2, out + err)                 # today: 0, and the chain FAILs from then on
        self.assertIn("is earlier than the last record of ops", err)
        with self.assertRaises(gblog.TimestampError):
            gblog.archive_file(self.ledger, "ops", b"x\n", "e.txt", ts="2024-06-01T15:00:00Z")
        self.assertEqual(open(self.ledger.log_files("ops")[0], "rb").read(), before)
        self.assertEqual([f for _d, _s, fs in os.walk(os.path.join(self.root, "archive")) for f in fs], [])
        self.assertTrue(self.ledger.verify("ops")["ok"])
```

Documentation to add: one sentence in README "Upgrading" and DESIGN section 4 step 4 that after a pre-0.6 record with a local-time `--ts`, the next records can be stamped with that wall-clock reading (`clock_regressed` holds the real time).

---

## K3-02 — Medium — The upgrade documentation and the code describe different trees

`CHANGELOG.md` says "Every deep-review finding rated Medium or higher is fixed in this branch". In this tree that holds for D2, D5 and D6 (`tests/test_deep_reviews.py` covers exactly those three reviews). The D1, D3 and D4 entries are documented in CHANGELOG, README and DESIGN, but the code does not have them. Operators rewrite routines and Auto-review rules from these texts.

### A. Documented as fixed, absent from the code (upgrade-relevant rows)

| Documented | What the code does |
|---|---|
| D1-02: a gap over 10 000 records gets empty `missing_seqs` plus `missing_count` | `_describe_join`: `missing = list(range(seq_before + 1, seq_after))`, no cap, no `missing_count`. The fuzz suite avoids a large forged `seq` for that reason. |
| D1-05: heads of the newest day rollup must still be in the chain | `_anchor_snapshot` reads `anchors/<bot>.json` and the history only. Rollups are used only for a missing `logs/<bot>` (`orphan_witnesses`). |
| D1-01, D1-04, D1-03: non-regular state/anchor file is a problem; `logs/<bot>: cannot be listed`; `anchor aborted: <class>` | None of the three texts exists. A FIFO at `state/<bot>.json` is the "missing or unreadable" warning, and `test_fifo_state_and_alias_files_do_not_hang` expects exit 0. |
| D3-03: no `origin` in the ledger repository means nothing is pushed | `sync` prints `warning: could not confirm the remote is private (no origin configured)` and runs `git push -u origin …`. |
| D3-05: the sync cache is removed | `_SYNC_CACHE` and `_write_sync_cache` exist; `sync` passes `.git/gblog-sync-cache.json`. |
| D3-08 and README Upgrading item 7: `sync` says `<dir> is a symbolic link … not pushed` | No such check. A symlinked `archive/` ends in the generic `committed content differs from disk … (git ignore rule, filter or index flag); not pushed`. The exit code is 1 as documented, but the text points the operator the wrong way. |
| D3-04: an ignore rule naming a whole ledger directory no longer aborts | `_git(root, "add", "-A", *add_paths)` runs with `check=True`, so git's "paths are ignored" error becomes `git failed`, exit 2. |
| D3-01, D3-02, D3-07: bracketed or percent-encoded host, `<transport>::`, wiki; unsigned commits | `github_remote` has no such cases; `commit` and `push` carry no `--no-gpg-sign`. |
| D4-01: file records carry `observed` / `observed_from` | `capture_files` appends neither. File coverage with a weekly run is still about 0 %, which feeds a `logging` proposal each week. |
| D4-03: `capture browser --hours` below 1 exits 2 | `_validate_args` rejects only `< 0` (`--hours must be 0 or more`). |
| D4-05: one snapshot per `--path` | One snapshot per Bot; a different path gives `(new baseline: path changed)`, as `test_new_baseline_reason_is_on_the_output_line` asserts. |
| D4-06 to D4-09: own reports only; `auto` is not an approval; `elsewhere` | `analyze` counts `decision in ("allowed", "auto")` and all `fail` actions; `coverage` has no `elsewhere` key. |
| D4-02: the hook logs nothing at the first prompt of a new shell | `SHELL_HOOK` starts with `__gblog_last=""` and logs `history 1` at the first prompt. |

### B. Listed under "Known open findings", already in the code

- D2-06, D2-08 (own-tail preflight in `seal`), D2-09, D2-14.
- D5-04, D5-05, D5-06, D5-07, D5-12.
- D6-06 to D6-10.

Stale texts that follow from this:

- `docs/chain-repair-live-apply.md`, "Known limit": the `break_ack` no longer goes to the spool; `seal` now exits 2.
- DESIGN section 4, "Two known limits", first item.
- CHANGELOG "Future work", "A genesis state file written before a chain's first line": `append` does this now.

### C. Upgrade notes that are missing

- **Git LFS or any clean filter on `archive/**` or `logs/**`.** The committed-content check compares blob bytes with the disk, so `sync` exits 1 on every run. This is by design, but README "Upgrading" does not mention it and there is no override. Add an item that says `sync` cannot be used with a filter on gblog's files.
- **`$GBLOG_TASK` fallback** for `note`, `approval` and `task-end` (added in 0.6.0). A stale export from the long-standing `export GBLOG_TASK="$TID"` advice now attaches records to an old task. One sentence under Upgrading: unset it after `task-end`.
- **`verify --bot <slug> --contains <head>` changes meaning** once the slug's own chain directory exists: only that chain is searched. A routine that passes a slug or display name with a head of a variant chain then reports a rollback. Say explicitly: pass the chain name exactly as posted.

### Fix

Decide row by row: implement, or move the entry to "Known open findings" and correct README and DESIGN. To stop this recurring, add a small guard test in the style of `tests/test_meta.py`:

```python
# tests/test_docs_claims.py
"""Sentinel strings for behaviour the upgrade notes promise. A row is removed only together with the doc text."""
import os, unittest
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMS = {                       # text in the docs -> text that must then exist in gblog.py
    "missing_count": '"missing_count"',
    "cannot be listed": "cannot be listed (",
    "anchor aborted": "anchor aborted: ",
    "is a symbolic link:": "is a symbolic link",
    "`elsewhere`": '"elsewhere"',
    "observed_from": '"observed_from"',
}

class DocsClaimsTest(unittest.TestCase):
    def test_documented_behaviour_exists_in_the_code(self):
        read = lambda *p: open(os.path.join(REPO, *p), encoding="utf-8").read()
        docs = read("CHANGELOG.md") + read("README.md") + read("docs", "DESIGN.md")
        code = read("gblog.py")
        missing = [doc for doc, marker in CLAIMS.items() if doc in docs and marker not in code]
        self.assertEqual(missing, [], "documented but not in gblog.py")
```

All six rows fail today.

---

## K3-03 — Medium on macOS and Windows — a case variant of the chain name breaks `--contains`

### Where

`Ledger.verify` and `_layout_findings` compare the `bot` field with the directory name as text:

```python
        res.own_hashes = {rec.get("hash") for _rel, _lineno, rec in items
                          if isinstance(rec.get("hash"), str) and rec.get("bot") == bot}
```
```python
        if rec.get("bot") != bot:
            mismatched += 1
```

On a case-insensitive file system `logs/ops` and `logs/Ops` are one directory, and `os.listdir` reports the name it was created with. `add_alias` lower-cases the slug. README "Upgrading" tells every user with name variants to run `gblog alias add --slug <slug> … --alias <old-variant>`.

### Reproduction (macOS default APFS; the CI macOS job)

```bash
gblog note --bot Ops --detail one            # directory logs/Ops, record bot "Ops"
gblog alias add --slug ops --display Ops --alias Ops
gblog note --bot Ops --detail two            # resolves to slug "ops"; appended to the same directory, record bot "ops"
gblog anchor                                 # anchors/Ops.json; the routine posts "Ops <head of record 2>"
gblog verify --bot Ops --contains <that head>
```

Today:

- Exit 1 with `expected hash … not found in chain`. The daily routine reports a ROLLBACK every day.
- `! …: bot field 'ops' does not match chain 'Ops'` for each new record.
- `--strict` FAILs.

0.6.0 passed the `--contains` call; the chain-exact rule (B:M3) is what made it fail. Linux is not affected: there the two names are two directories.

### Fix

Decide "own" by directory identity, restricted to case variants. On a case-sensitive file system this changes nothing, and a decoy copy under another name is still a different directory.

```python
    def _own_name(self, chain: str):
        """Predicate: does a record's ``bot`` value name the directory ``logs/<chain>``?
        Equal text, or a case variant that is the same directory (case-insensitive file systems)."""
        chain_dir = os.path.join(self.logs_dir, chain)
        seen: Dict[str, bool] = {chain: True}

        def own(value: object) -> bool:
            if not isinstance(value, str):
                return False
            if value not in seen:
                same = False
                if value.lower() == chain.lower() and _chain_key(value) == value:
                    try:
                        same = os.path.samefile(os.path.join(self.logs_dir, value), chain_dir)
                    except OSError:
                        same = False
                seen[value] = same
            return seen[value]

        return own
```

Use it in three places:

- `verify`: `own = self._own_name(bot)` for `res.own_hashes`.
- `_layout_findings`: pass `own` as a parameter and replace `rec.get("bot") != bot` with `not own(rec.get("bot"))`.
- Readers (`records`, `chain_records`, `tail_records`): use the same predicate before setting `bot_field`.

### Test

```python
def _case_insensitive(directory):
    probe = os.path.join(directory, "CaseProbe")
    os.mkdir(probe)
    try:
        return os.path.isdir(os.path.join(directory, "caseprobe"))
    finally:
        os.rmdir(probe)

class CaseVariantChainTest(Base):
    def test_case_variant_of_the_chain_name_is_the_same_chain(self):
        if not _case_insensitive(self.base):
            self.skipTest("needs a case-insensitive file system")
        with redirect_stderr(io.StringIO()):
            self.ledger.append("Ops", {"type": "note", "detail": "one", "ts": "2024-06-01T10:00:00Z"})
            self.ledger.add_alias("ops", "Ops", ["Ops"])
        rc, out, err = run(["note", "--bot", "Ops", "--detail", "two"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.listdir(os.path.join(self.root, "logs")), ["Ops"])
        head = self.ledger.read_state("Ops")["head"]
        res = self.ledger.verify("Ops")
        self.assertEqual((res["ok"], res["records"], res["warnings"]), (True, 2, []))   # today: one warning
        rc, out, err = run(["verify", "--bot", "Ops", "--contains", head], self.root)
        self.assertEqual(rc, 0, out + err)                                              # today: 1
        rc, out, err = run(["verify", "--bot", "Ops", "--strict"], self.root)
        self.assertEqual(rc, 0, out + err)                                              # today: 1
```

The decoy test `test_contains_is_not_satisfied_by_a_decoy_chain` must keep passing; it does, because `decoy` is not a case variant of `ops`.

---

## K3-04 — Medium — `sync --remote` treats another spelling of the same remote as a re-point

### Where

`sync()`:

```python
        if current and remote not in current and not replace_remote:
            raise _UsageError("origin is already %s; pass --replace-remote to send backups to %s instead"
                              % (_safe_url(current[0]), _safe_url(remote)))
```

The comparison is on text, and the error is raised before the `git add` and `commit`. Unlike the exit-3 refusal, not even the local commit is made.

README says "a routine that passes the same `--remote` on every run keeps working". That is true only for a byte-identical string. `examples/routine-daily-audit.sh` passes `--remote=$GBLOG_AUDIT_REMOTE` every night. If `origin` was set by hand or by an earlier clone as `git@github.com:you/ledger.git` and the variable holds `https://github.com/you/ledger`, or the same URL without `.git`, the run exits 2 every night. 0.6.0 ran `set-url`, which was harmless in this case.

### Fix

```python
def _same_remote(a: str, b: str) -> bool:
    """True when two spellings name the same repository: equal text; the same GitHub owner/repo over
    any transport; the same host, transport kind and path apart from a trailing ``/`` or ``.git``;
    or the same local directory."""
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return True
    ga, gb = github_remote(a), github_remote(b)
    if ga[0] or gb[0]:
        return bool(ga[0] and gb[0] and ga[1] and gb[1]
                    and (ga[1].lower(), ga[2].lower()) == (gb[1].lower(), gb[2].lower()))

    def norm(url: str):
        host, path, kind = _remote_host_path(url)
        if host is None:                                   # a local path or file:// URL
            local = path if kind == "file" else url
            return ("", os.path.realpath(os.path.expanduser(local or "")), "path")
        tail = (path or "").rstrip("/")
        return (host, tail[:-4] if tail.endswith(".git") else tail, kind)

    return norm(a) == norm(b)
```

In `sync()`:

```python
        same = any(_same_remote(remote, u) for u in current)
        if current and not same and not replace_remote:
            raise _UsageError(...)
        if same and remote not in current and not replace_remote:
            pass                                           # an equivalent spelling: .git/config stays as it is
        elif "origin" in remotes:
            _git(root, "remote", "set-url", "origin", remote)
        else:
            _git(root, "remote", "add", "origin", remote)
        ...
        if not same or (replace_remote and remote not in current):
            _bookkeep({... "target": "git remote" ...})
```

This is safe by construction: when the spelling is equivalent, `origin` is not touched, and the visibility check still runs on the stored push URLs.

### Test (add to `RemoteChangeAndDriftTest` in `tests/test_capture_sync.py`; no network, `--no-push`)

```python
    def test_equivalent_spelling_of_origin_is_not_a_change(self):
        url = "https://github.com/you/private-ledger.git"
        rc, out, err = run(["sync", "--no-push", "--remote", url], self.root)
        self.assertEqual(rc, 0, out + err)
        records, commits = len(self._remote_records()), self._commits(self.root)
        for same in ("https://github.com/you/private-ledger", "https://github.com/you/private-ledger/",
                     "git@github.com:you/private-ledger.git", "ssh://git@github.com/You/Private-Ledger.git"):
            with self.subTest(remote=same):
                rc, out, err = run(["sync", "--no-push", "--remote", same], self.root)
                self.assertEqual(rc, 0, out + err)                    # today: 2
                self.assertEqual(self._origin(), url)
                self.assertEqual(len(self._remote_records()), records)
        self.assertGreater(self._commits(self.root), commits)         # the nightly commit is made again
        rc, out, err = run(["sync", "--no-push", "--remote", "https://github.com/you/other.git"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self._origin(), url)

    def test_trailing_slash_on_a_local_remote_is_the_same_remote(self):
        bare = self._bare("first.git")
        self.assertEqual(run(["sync", "--no-push", "--remote", bare], self.root)[0], 0)
        rc, out, err = run(["sync", "--no-push", "--remote", bare + "/"], self.root)
        self.assertEqual(rc, 0, out + err)                            # today: 2
        self.assertEqual(self._origin(), bare)
```

---

## K3-05 — Low — one bad entry disables the whole alias registry

### Where

`Ledger._read_aliases`:

```python
        usable = isinstance(bots, dict) and all(k and _chain_key(k) == k for k in bots) and all(
            isinstance(e, dict) ... for e in bots.values())
        if not usable:
            return {"bots": {}, "strict": False, "unreadable": True}, True
```

The slug-key rule is new in 0.6.1, and any single odd entry now empties the registry. With an empty registry `resolve()` returns `known=True` for every name. So `--bot "Chief of Staff"` silently starts a new chain `Chief-of-Staff`, strict mode is off, and reports stop merging. The only signal is one stderr line, which the shell hook discards.

I could not confirm that 0.3–0.5 ever wrote such an entry; hand-edited registries are the likely source.

### Reproduction

```python
write_json(aliases_path, {"strict": True, "bots": {
    "chief-of-staff": {"display": "Chief of Staff", "aliases": ["cos"]},
    "broken": {"display": "Broken", "aliases": None}}})
run(["note", "--bot", "Chief of Staff", "--detail", "x"], root)   # today: rc 0, new directory logs/Chief-of-Staff
run(["note", "--bot", "stranger", "--detail", "x"], root)         # today: rc 0 although strict is on
```

### Fix

Validate per entry.

- Keep the valid entries and the stored `strict` value.
- Drop each invalid entry with a warning that names it, for example: `warning: state/aliases.json is unreadable in part: entry '<key>' is ignored; the registry is not rewritten until it is fixed`. The text must still contain `state/aliases.json is unreadable`, because existing tests assert that substring.
- Return a second flag, `partial`, so `_aliases_for_update` still refuses to overwrite the file.
- Whole-file failures (not an object, `bots` not an object) stay exactly as they are, with today's exact warning text.

### Test

```python
    def test_one_bad_registry_entry_does_not_disable_the_others(self):
        path = self.ledger.aliases_path()
        doc = {"strict": True, "bots": {"chief-of-staff": {"display": "Chief of Staff", "aliases": ["cos"]},
                                        "broken": {"display": "Broken", "aliases": None}}}
        self.ledger.init()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        before = open(path, "rb").read()
        rc, out, err = run(["note", "--bot", "Chief of Staff", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.listdir(os.path.join(self.root, "logs")), ["chief-of-staff"])  # today: Chief-of-Staff
        self.assertIn("state/aliases.json is unreadable", err)
        self.assertIn("'broken'", err)
        rc, out, err = run(["note", "--bot", "stranger", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)                                                 # strict still applies
        rc, out, err = run(["alias", "add", "--slug", "new-bot"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(open(path, "rb").read(), before)                                  # never overwritten
```

These existing tests keep their meaning: `test_aliases_type_confusion_neither_crashes_nor_loses_events`, `test_non_dict_aliases_warns_not_crashes`, `test_registry_slug_that_is_not_a_chain_key_makes_the_registry_unreadable`.

---

## K3-06 — Low — `verify_archives`: `bytes` as a hard limit, and backslash paths

### Where

```python
        full = os.path.join(ledger.root, rel)          # rel, not the normalised form checked just above
        ...
        size = r.get("bytes")
        limit = size if _is_int(size) and size >= 0 else None
        ...
            found = _gunzip_sha256(full, limit)        # None when the content is longer than `bytes`
```

- **`bytes`.** Until the bounded read (W-02), `bytes` was informational; DESIGN calls it "length of the stored content". Now any record whose `bytes` is smaller than the stored content is `content hash mismatch (file modified after archiving)`, even when the SHA-256 matches. I could not confirm that 0.3–0.5 recorded the post-redaction length. If any of them recorded the original length, every redacted archive whose content grew fails. Check the tagged sources before release.
- **Backslash path.** `norm = rel.replace("\\", "/")` is used for the checks, but the file is opened from `rel`. A record written on Windows by a version that stored `os.sep` is `archived file missing` on Linux or macOS.

### Fix

```python
        full = os.path.join(ledger.root, *norm.split("/"))
        ...
        cap = None if limit is None else max(4 * limit, limit + 65536)   # still stops a gzip bomb after 64 KiB of excess
        found = _gunzip_sha256(full, cap)
```

The digest alone decides. `test_oversized_archive_is_a_mismatch_without_being_read_to_the_end` still passes: with `bytes: 10` the read stops below 1 MiB. Optionally add a `!` warning when the digest matches but the length differs from `bytes`.

### Tests

```python
    def test_archive_with_a_smaller_bytes_field_still_verifies(self):
        data = b"evidence line\n" * 50
        digest = hashlib.sha256(data).hexdigest()
        rel = "archive/ops/2024-06-01/%s.gz" % digest
        full = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(full))
        with gzip.open(full, "wb") as fh:
            fh.write(data)
        self.ledger.append("ops", {"type": "archive", "action": "file", "target": "e.txt", "outcome": "ok",
                                   "kind": "artifact", "sha256": digest, "bytes": len(data) - 40, "path": rel,
                                   "ts": "2024-06-01T10:00:00Z"})
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])      # today: content hash mismatch

    @unittest.skipUnless(os.sep == "/", "POSIX path separator")
    def test_archive_path_written_with_backslashes_is_found(self):
        rec = gblog.archive_file(self.ledger, "win", b"evidence\n", "e.txt", "artifact", ts="2024-06-01T10:00:00Z")
        self.ledger.append("win", {"type": "archive", "action": "file", "target": "e.txt", "outcome": "ok",
                                   "kind": "artifact", "sha256": rec["sha256"], "bytes": rec["bytes"],
                                   "path": rec["path"].replace("/", "\\"), "ts": "2024-06-01T10:00:01Z"})
        res = self.ledger.verify("win")
        self.assertTrue(res["ok"], res["problems"])      # today: archived file missing
```

---

## K3-07 — Low — a non-chain directory under `logs/` is a permanent FAIL

### Where

`Ledger.invalid_chain_dirs` returns `[d for d in self.bots() if _chain_key(d) != d]`, and `verify --all` emits a FAIL for each.

The intent (R-04) is to stop smuggled records from being read. But the rule also hits directories that hold no log file at all:

- `lost+found`, when `logs/` is a mount point. README "Upgrading" item 7 recommends a mount.
- `.stfolder`, `.Trash-1000`, `.snapshots`.

0.6.0 printed `OK … 0 records` for a listable one. Now `verify --all` exits 1 every day, and every reader prints `warning: ignoring logs/<dir> …` on every command. Readers never read these directories, so the FAIL protects nothing when there is no `*.jsonl` inside.

### Fix (in the `verify --all` branch of `_main`)

```python
def _holds_logs(path: str) -> Optional[bool]:
    """True/False when the directory can be listed, None when it cannot."""
    try:
        return any(name.endswith(".jsonl") for name in os.listdir(path))
    except OSError:
        return None
```

- `True`: FAIL as today.
- `False`: no FAIL; one `notice: logs/<dir> is not a chain directory and holds no log file; ignored` on stderr.
- `None`: no FAIL only when the directory is not owned by the current user (`os.stat(path).st_uid != os.getuid()`, where `getuid` exists). The adversary in the threat model runs as the gblog user and cannot create such a directory. Otherwise FAIL as today.

### Test

```python
    def test_non_chain_directory_without_log_files_is_not_a_fail(self):
        self.seed("ops", 1)
        os.mkdir(os.path.join(self.root, "logs", ".stfolder"))
        os.makedirs(os.path.join(self.root, "logs", "my bot"))
        open(os.path.join(self.root, "logs", "my bot", "2024-06-01.jsonl"), "w").close()
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)                                   # because of "my bot" only
        self.assertRegex(out, r"(?m)^FAIL\s+my bot\s")
        self.assertNotRegex(out, r"(?m)^FAIL\s+\.stfolder\s")                # today: FAIL
        self.assertIn("logs/.stfolder", err)
        shutil.rmtree(os.path.join(self.root, "logs", "my bot"))
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out + err)                                   # today: 1
```

---

## K3-08 — Low — no test uses a ledger shaped like 0.2–0.5 output

Every test builds its ledger with the current `Ledger.append`. The CHANGELOG promise "a ledger written by any version still verifies with the latest one" is therefore untested, and K3-01 went unnoticed for that reason.

Proposed base for `tests/test_legacy_compat.py`. It builds legacy shapes by hand, so no fixture files change.

```python
"""Ledgers shaped like the ones gblog 0.2-0.5 wrote (review K3). No gblog code is on the write path."""
import gzip, hashlib, io, json, os, shutil, sys, tempfile, time, unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402
sys.path.insert(0, HERE)
from _support import run  # noqa: E402


def raw_chain(root, bot, stamps, day):
    """One hash-valid note per entry of ``stamps`` in logs/<bot>/<day>.jsonl; state names the last one."""
    d = os.path.join(root, "logs", bot)
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    prev, recs = gblog.GENESIS, []
    with open(os.path.join(d, day + ".jsonl"), "a", encoding="utf-8") as fh:
        for i, ts in enumerate(stamps, 1):
            rec = {"v": 1, "seq": i, "ts": ts, "bot": bot, "type": "note", "detail": "legacy %d" % i, "prev": prev}
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            recs.append(rec)
    with open(os.path.join(root, "state", bot + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"seq": len(recs), "head": prev, "updated": stamps[-1]}, fh)
    return recs


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-legacy-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        for key in ("GBLOG_TASK", "GBLOG_ROOT"):
            os.environ.pop(key, None)

    def seed(self, bot="ops", n=1):
        with redirect_stderr(io.StringIO()):
            return [self.ledger.append(bot, {"type": "note", "detail": "r%d" % i, "ts": "2024-06-01T10:00:%02dZ" % i})
                    for i in range(n)]
```

Shape tests that should pass on the current code and stay as regression guards:

```python
class LegacyShapeTest(Base):
    def test_free_form_ts_chain_verifies_and_only_strict_objects(self):
        raw_chain(self.root, "ops", ["2024-06-01 09:00:00", "2024-06-01T09:05:00+00:00"], "2024-06-01")
        res = self.ledger.verify("ops")
        self.assertEqual((res["ok"], res["records"], res["warnings"]), (True, 2, []))
        strict = self.ledger.verify("ops", strict=True)
        self.assertEqual(len(strict["problems"]), 2)
        self.assertTrue(all(p.endswith("ts is not in normalised form") for p in strict["problems"]))

    def test_single_int_browser_marker_of_0_2_is_migrated(self):
        import sqlite3
        prof = os.path.join(self.base, "chrome", "Default")
        os.makedirs(prof)
        hist = os.path.join(prof, "History")
        now = int((time.time() + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
        conn = sqlite3.connect(hist)
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        conn.execute("INSERT INTO urls VALUES (1, 'https://old.example/a', 'A')")
        conn.execute("INSERT INTO urls VALUES (2, 'https://new.example/b', 'B')")
        conn.execute("INSERT INTO visits VALUES (1, 1, ?)", (now - 1200 * 1_000_000,))
        conn.execute("INSERT INTO visits VALUES (2, 2, ?)", (now - 300 * 1_000_000,))
        conn.commit(); conn.close()
        recs = raw_chain(self.root, "ops", ["2024-06-01T09:00:00Z"], "2024-06-01")
        self.ledger.write_state("ops", {"seq": 1, "head": recs[0]["hash"],
                                        "capture_browser_last": now - 600 * 1_000_000})   # one marker, as 0.2 kept it
        with redirect_stderr(io.StringIO()):
            res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual(res["added"], 1)
        self.assertEqual(self.ledger.records("ops")[-1]["target"], "https://new.example/b")
        self.assertEqual(list(self.ledger.read_state("ops")["capture_browser_last"]), [hist])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_registry_entry_without_aliases_key_and_without_strict(self):
        self.ledger.init()
        with open(self.ledger.aliases_path(), "w", encoding="utf-8") as fh:
            json.dump({"bots": {"chief-of-staff": {"display": "Chief of Staff"}}}, fh)
        self.assertEqual(self.ledger.resolve("Chief of Staff"), ("chief-of-staff", True))
        rc, out, err = run(["note", "--bot", "Chief of Staff", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("unreadable", err)
        self.assertEqual(os.listdir(os.path.join(self.root, "logs")), ["chief-of-staff"])

    def test_anchor_file_without_history_still_witnesses_a_truncation(self):
        recs = raw_chain(self.root, "ops", ["2024-06-01T09:00:00Z", "2024-06-01T09:01:00Z"], "2024-06-01")
        os.makedirs(os.path.join(self.root, "anchors", "days"))
        with open(os.path.join(self.root, "anchors", "ops.json"), "w", encoding="utf-8") as fh:   # as 0.4 wrote it
            json.dump({"bot": "ops", "ok": True, "status": "OK", "records": 2, "head": recs[1]["hash"],
                       "resumed_from": None, "ts": "2024-06-01T10:00:00Z"}, fh)
        self.assertTrue(self.ledger.verify("ops")["ok"])
        self.assertEqual(self.ledger.anchor(["ops"])["chains"][0]["resumed_from"], recs[1]["hash"])
        log = self.ledger.log_files("ops")[0]
        first = open(log, encoding="utf-8").read().splitlines()[0]
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(first + "\n")
        os.remove(os.path.join(self.root, "state", "ops.json"))
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertTrue(any("no longer in the chain" in p for p in res["problems"]), res["problems"])
```

`LegacyTimestampTest` (K3-01) goes into the same module. The tests from K3-03, K3-05, K3-06 and K3-07 fit there too; the K3-04 tests belong in `tests/test_capture_sync.py`.

Two release-time checks that need the old sources, which I could not do:

1. Run 0.2, 0.3 and 0.5 from their tags. Write a small ledger with each, including `archive` with a redacted file, `alias add`, `anchor` and a refused append. Verify it with this tree, with and without `--strict`. This settles the `bytes` question in K3-06 and confirms the line serialisation that `--strict` compares against. The legacy test in `tests/test_gblog.py` replaces both `"detail":"a"` and `"detail": "a"`, which suggests the separators were not always certain.
2. Confirm that `add_alias` in 0.3–0.5 always sanitised and lower-cased slug keys. This decides how likely K3-05 is on untouched registries.

---

## Checked and found compatible

- **Anchors from 0.4/0.5.** Full 64-hex heads; `_anchor_checks` reads `head` and `segment_heads` as those versions wrote them. Junk `records: 0` anchors from `anchor --bot <typo>` are not treated as witnesses.
- **Old spool file names** (`<stamp>-<8 hex>.json`) replay. The stale-alert dismissal procedure in README Upgrading item 3 works against `spool_replay` as written.
- **Retired-chain procedure** (Upgrading item 4) matches `missing_chains()`: anchor file, history, state head, rollup `chains` entries. The `missing` key that `anchor` now adds to a rollup is not read back as a witness.
- **Archive paths.** For Bot names shaped like key prefixes, a record written before the Stripe/GitLab rules holds the true path, and one written by 0.6.0 holds the redacted path, which the D6-01 fallback resolves. Rules added later cannot change the redaction of a path.
- **Proposals.** `tune` records without `proposal.count` and `proposals.json` entries without per-entry `generated` are handled.
- **Reserved names.** Legacy chains under now-reserved names keep appending, capturing, archiving and syncing. A legacy chain directory ending in `.tmp` is covered by the ledger's own `*.tmp` ignore rule, but the forced `git add -f` path stages it.
- **Abbreviation table** in README Upgrading matches `_build_parser()` and `_add_root_everywhere()` row by row; I found no undocumented new ambiguity.
- **Mixed-version operation.** A 0.6.0 writer preserves the state-level `state_adopted` key, and the next current append moves it onto its record. The new spool names and the genesis state written before a first line do not disturb 0.6.0 readers.

## Suggested order

1. K3-01 before any release note calls 0.6.1 safe for ledgers with pre-0.6 `--ts` records.
2. K3-02: per row, implement or move back to "Known open findings".
3. K3-04 and K3-03: small, self-contained, with tests.
4. K3-05 to K3-08 as hardening, together with the two release-time checks.