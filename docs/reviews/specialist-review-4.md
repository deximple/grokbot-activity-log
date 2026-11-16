# Specialist review K4 — 2026-11-16

**Area.** Performance and operations on chains of 50 000 – 500 000 records: worst-case time and memory of `append`, `verify`, `anchor`, `status`, `report`, `tail`, `export`, `sync`, `capture`, `coverage`, `tune`; accidental quadratic loops; decompression and inflation bombs; bounded-memory changes; benchmark and test designs.

**Reviewed.** `gblog.py` (`__version__ = "0.6.1"`) as it is in this tree, plus `docs/DESIGN.md` §13, the routine prompts, `examples/` and `tests/bench_large_ledger.py`.

**Method and limits.**

- No code was edited and nothing was run. Every figure below is derived from the code and is an estimate. Section 5 gives the scripts and tests that turn the estimates into measurements.
- Assumptions for the estimates: CPython 3.8–3.12 on a 64-bit Linux host, one ~3 GHz core, warm page cache, ~300 bytes per log line, 12 keys per record.
- Fixes are given as code to apply; none of them changes the record format, the hash rule or the output for a healthy ledger. Where a fix changes output for a pathological input, the finding says so.

---

## 1. Sizing model and cost table (current code)

Symbols: **N** records in a chain, **D** day files, **C** chains, **M30** records of the last 30 days, **A** uncompressed bytes of all archived files, **S** pending spool files, **Y** day rollups under `anchors/days/`.

**Memory per parsed record.** `json.loads` shares key strings only inside one call, so every record carries its own 12 key strings.

| Part | Bytes (3.8–3.10) | Note |
|---|---|---|
| 12 key strings | ~700 | not shared between records |
| value strings (`hash`, `prev`, `ts`, …) | ~860 | |
| the `dict` | 640 | ~460 on 3.11+ |
| tuples in `snap_lines` and `items`, list slots, `lineno` | ~190 | `verify` only |
| **total** | **~2.0–2.4 KB** | |

**Time per record in `verify`:** ~10 µs (read, parse with the duplicate-key hook, re-serialise and SHA-256, layout checks). About 40–50 % of it runs while the shared lock is held.

| Command | What grows with the ledger | N = 50k | N = 500k | Finding |
|---|---|---|---|---|
| `append` | one listing of D names; 3 fsync | ~1–3 ms + fsync | same | K4-03, K4-04 (worst case only) |
| `verify --bot` | all N parsed and kept | ~0.5 s, ~110 MB, lock ~0.25 s | ~5 s, **~1.0–1.2 GB**, lock **~2.5 s** | K4-01, K4-02 |
| `verify --all`, `anchor` | sum over chains, plus A, plus Y rollups | | | K4-05, K4-06, K4-12 |
| daily routine | 3 walks per chain per run | ~1.5 s per chain | ~15 s per chain, 3 × 1 GB peaks | K4-06 |
| `status` | M30 parsed, kept, sorted twice | | M30 = 50k: ~0.4 s, ~110 MB | K4-10 |
| `report --date D` | day D, plus a raw scan of later files | small | small for yesterday; O(N) raw for an old D | – |
| `report --since`, `coverage`, `tune` | records since the date, kept and sorted | | 2.2 KB per record | K4-08, K4-09, K4-11 |
| `export` | every record of every chain, kept and sorted | ~110 MB per chain | ~1.1 GB per chain, all chains at once | K4-09 |
| `tail -n k` | newest day file of each chain, parsed whole | small | small | K4-13 |
| `sync` | git, plus stat of every evidence file, plus hash of changed files | | | K4-14 |
| `spool replay` | S × (lines since the refusal + S) | | minutes for S in the thousands | K4-07 |
| `archive` | whole file in memory, ~4–5 copies | | | K4-05 |
| redaction (`append`, `archive`) | linear, except two rules | | | K4-04 |

---

## 2. Findings

| Id | Severity | Title |
|---|---|---|
| K4-01 | High | A `seq` jump or a `break_ack` range allocates a list as long as the gap: one line ends `verify --all` and `anchor` |
| K4-02 | High | `verify` keeps every parsed record (~1 GB at 500k) and parses under the shared lock (appends stall for seconds) |
| K4-03 | Medium | `_last_line` is quadratic in the length of the last line; lines and side files are read without a size bound |
| K4-04 | Medium | Two redaction rules are quadratic on crafted text; `append` redacts while it holds the bot lock |
| K4-05 | Medium | Archive re-hash cost grows without bound; an undeclared size is read to the end; `archive` needs ~5× the file size |
| K4-06 | Medium | The daily routine walks every chain three times; the example script re-hashes archives twice |
| K4-07 | Medium | `spool replay` scans the chain once per replayed event |
| K4-08 | Medium | `render_report` searches every task for every open task |
| K4-09 | Low | `export`, `report --since` and `records()` build and sort the full record list |
| K4-10 | Low | `status` builds and sorts the 30-day window to compute two numbers |
| K4-11 | Low | `tune` recomputes each metric with a full pass over the Bot's records |
| K4-12 | Low | Unbounded `problems` / `breaks` / `segments` lists flow into anchors and rollups; every rollup is parsed on each `verify --all` |
| K4-13 | Low | Smaller items (table) |
| K4-14 | Info | Cost claims in the documentation that the code does not match |

---

### K4-01 — A `seq` jump allocates a list as long as the gap (High)

**Where.** `_describe_join`, `_break_shape_ok`, `_norm_break`.

```python
# _describe_join
missing = list(range(seq_before + 1, seq_after)) if seq_after > seq_before else []

# _break_shape_ok
if n["missing_seqs"] != list(range(n["seq_before"] + 1, n["seq_after"])):
    return False
```

**Why it matters.**

- The hash is unkeyed, so any writer can produce a hash-valid line with `seq` far ahead. A genuine corruption that drops a large range has the same effect.
- A gap of 3 × 10⁸ asks for ~2.4 GB of list plus ~9.6 GB of `int` objects. On a small host the kernel kills the process (exit 137) before Python can raise `MemoryError`, so `_verify_guarded` never gets to turn it into a FAIL entry. `verify --all`, `anchor`, `seal` and `state rebuild` all go through this path.
- The second trigger needs no gap in the chain at all: a `break_ack` record whose own descriptor says `seq_before: 1, seq_after: 10**15` reaches `_break_shape_ok` on any chain, before the pin is checked.
- For a FAIL chain the list is also written to `anchors/<bot>.json`, merged into the day rollup and printed by `verify --json`.
- CHANGELOG (D1-02) and DESIGN §7 describe a cap with `missing_count`. The code under review has no such cap, and `tests/test_fuzz_verify.py` says so in a comment ("a huge forged seq is not fuzzed here").

**Reproduction.** The helper is used by later reproductions too.

```python
# repro_helpers.py — run from the repository root
import json, os, tempfile, gblog

def raw_chain(root, bot, extras, day="2024-06-01"):
    d = os.path.join(root, "logs", bot)
    os.makedirs(d, exist_ok=True); os.makedirs(os.path.join(root, "state"), exist_ok=True)
    prev, recs = gblog.GENESIS, []
    with open(os.path.join(d, day + ".jsonl"), "w", encoding="utf-8") as fh:
        for i, extra in enumerate(extras, 1):
            rec = {"v": 1, "seq": i, "ts": "%sT10:%02d:%02dZ" % (day, i // 60 % 60, i % 60),
                   "bot": bot, "type": "note", "detail": "r%d" % i, "prev": prev}
            rec.update(extra); rec["hash"] = gblog.compute_hash(rec); prev = rec["hash"]
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"); recs.append(rec)
    with open(os.path.join(root, "state", bot + ".json"), "w") as fh:
        json.dump({"seq": recs[-1]["seq"], "head": prev, "updated": recs[-1]["ts"]}, fh)
    return recs
```

```python
from repro_helpers import *
root = tempfile.mkdtemp()
raw_chain(root, "ops", [{}, {}, {"seq": 10**15}])
gblog.Ledger(root).verify("ops")          # MemoryError (immediately; 10**15 cannot be allocated)

brk = {"file": "logs/ack/2024-06-01.jsonl", "line_before": 1, "line_after": 2, "seq_before": 1,
       "seq_after": 10**15, "hash_before": "aa" * 32, "orphan_prev": "bb" * 32,
       "hash_after": "cc" * 32, "missing_seqs": []}
raw_chain(root, "ack", [{}, {"type": "break_ack", "jay_approval_id": "x-0001", "break": brk}])
gblog.Ledger(root).verify("ack")          # MemoryError in _break_shape_ok
```

With `{"seq": 300_000_000}` instead, the allocation succeeds lazily and the process is killed on a host with less than ~12 GB free.

**Fix.**

```python
_MAX_MISSING_SEQS = 10000   # a larger gap is described by its size, not by a list


def _gap(seq_before: int, seq_after: int) -> Tuple[List[int], int]:
    """``(missing seqs, size of the gap)``. The list is empty when the gap is above the cap."""
    size = seq_after - seq_before - 1
    if size <= 0:
        return [], 0
    if size > _MAX_MISSING_SEQS:
        return [], size
    return list(range(seq_before + 1, seq_after)), size
```

- `_describe_join`: `missing, size = _gap(seq_before, seq_after)`; after building `desc`, add `desc["missing_count"] = size` only when `size > _MAX_MISSING_SEQS`. Small gaps keep exactly today's keys.
- `_norm_break`: raise `ValueError("missing_seqs")` when `missing` is not a list **or** `len(missing) > _MAX_MISSING_SEQS`.
- `_break_shape_ok`: replace the comparison with

  ```python
  expected, size = _gap(n["seq_before"], n["seq_after"])
  if size > _MAX_MISSING_SEQS or n["missing_seqs"] != expected:
      return False
  ```

The pinned break has a gap of 1, so no honoured ack is affected. A descriptor above the cap can never equal a valid ack.

**Tests.**

1. `test_seq_gap_is_described_by_size`: the first reproduction. Expect `ok` false, the problem `logs/ops/2024-06-01.jsonl:3: seq 1000000000000000, expected 3 (missing or duplicated line)`, `breaks[0]["missing_seqs"] == []` and `breaks[0]["missing_count"] == 10**15 - 3`.
2. `test_small_gap_keeps_its_list`: `seq` 5 after 2 gives `missing_seqs == [3, 4]` and no `missing_count` key.
3. `test_break_ack_with_huge_range_is_malformed`: the second reproduction. Expect the problem `…:2: malformed break_ack` and a return in well under a second.
4. `test_seq_gap_does_not_hide_other_chains`: add a healthy chain `zzz`; `verify --all` exits 1, prints `OK    zzz`, and contains no `verify aborted`.
5. `test_anchor_entry_stays_small`: after `anchor`, `anchors/ops.json` is smaller than 4 KiB.
6. Fuzz: remove the "odd values stay small" restriction in `_m_retype_rehash` and add `10**15` and `2**62` to `_ODD_VALUES` for `seq`.

---

### K4-02 — `verify` keeps every parsed record and parses under the shared lock (High)

**Where.** `Ledger.verify`, `Ledger._chain_snapshot`.

```python
with self.lock(bot, shared=True):
    log_paths, odd_files, link_files = self._file_findings(bot)
    snap_lines, log_names = self._chain_snapshot(bot, dup_lines, log_paths, keep_text=strict)
    ...
items = [(rel, lineno, rec) for rel, lineno, rec, _text in snap_lines]
...
hashes = {rec.get("hash") for _rel, _lineno, rec in items if isinstance(rec.get("hash"), str)}
```

**Why it matters.**

- **Memory.** `snap_lines` and `items` hold all N parsed records until the function returns: ~2.0–2.4 KB per record, so ~1.0–1.2 GB at 500k. Two hash sets add ~30–60 MB. `--strict` adds the raw text (~170 MB).
- **Lock.** Reading and parsing happen inside the shared lock. An append needs `LOCK_EX` and waits: ~2.5 s at 500k with a warm cache, longer when 150 MB must come from disk. The shell hook runs `gblog log` synchronously in `PROMPT_COMMAND`, so an interactive prompt freezes for that time.
- The daily routine does this three times per chain (K4-06).
- DESIGN §13 keeps the non-streamed walk on purpose "because the order and the text of the problems on a broken chain depend on seeing both sides of every join". The walk only ever looks at the previous item (`_describe_join(items[i - 1], items[i])`) and at one ack. That does not need the whole list.

**Reproduction.**

```bash
python tests/bench_large_ledger.py --records 500000 --keep     # prints the ledger path
/usr/bin/time -v python gblog.py --root <path> verify --bot bench   # "Maximum resident set size"
```

Lock stall: run the probe from section 5 (`--probe-lock`) while `verify` runs.

**Fix, stage 1 (small, low risk, ~30 % less memory).**

- In `_parse_line_dups.pairs`, share the key strings: `obj = {sys.intern(k): v for k, v in items}`. This saves ~700 bytes per record for ~1 µs per record.
- Do not build `items`; iterate `snap_lines` and ignore the fourth element.
- Build `hashes` / `own_hashes` only when a caller asks (see stage 2, `want_hashes`).

**Fix, stage 2 (bounded memory, lock held for O(D) stat calls).**

*Snapshot by extent.* Log files are append-only: `_append_line` never rewrites earlier bytes, and its roll-back only removes bytes its own failing call wrote, under `LOCK_EX`. While the shared lock is held no writer is active, so the first `size` bytes of every day file are a consistent snapshot that can be read after the lock is released.

```python
def _snapshot_extents(self, bot: str):
    """Under the shared bot lock: every regular log file with the size and identity it has now."""
    paths, odd, links = self._file_findings(bot)
    extents = []
    for path in paths:
        st = os.stat(path)
        extents.append((path, st.st_size, st.st_dev, st.st_ino))
    return extents, odd, links


def _snapshot_lines(self, extents, keep_text: bool, dup_lines: set):
    """Yield ``(rel, lineno, record, text)`` for the bytes the snapshot covers. Constant memory."""
    for path, size, dev, ino in extents:
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (st.st_dev, st.st_ino) != (dev, ino) or st.st_size < size:
                raise _SnapshotChanged(rel)      # replaced or cut by something that is not gblog
            left, lineno = size, 0
            while left > 0:
                raw = fh.readline(left)
                if not raw:
                    break
                left -= len(raw)
                lineno += 1
                raw = raw.strip()
                if not raw:
                    continue
                text = None
                if keep_text:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        text = None
                rec, dup = _parse_line_dups(raw)
                if dup:
                    dup_lines.add((rel, lineno))
                yield rel, lineno, rec, text
```

`_SnapshotChanged` becomes the problem `<rel>: log file was replaced or shortened while it was being verified` (FAIL). gblog itself can never cause it.

*One pass, small state.* Replace the two loops over `items` with one pass over `_snapshot_lines` that keeps:

| State | Replaces |
|---|---|
| a running index `i`, and the previous item `(rel, lineno, rec)` | `items[i - 1]` in `_describe_join` |
| `expected_prev`, `expected_seq`, `last_ts`, `count`, the first item of the open segment | the walk variables and `close()` |
| `ack_problems`, `acks` (index, break, id, fixture flag) | pass 1 |
| `walk_problems`, `breaks`, and a list of *pending* joins `(i, same_file, desc, position)` | the `ack_ok` decision |
| a `stopped` flag | the `break` statements of the walk |
| `seen_needed`: hashes that are in `needed` | the full `hashes` set |
| first 3 + count of `state_adopted` records; `archive` records reduced to `seq`, `bot`, `path`, `sha256`, `bytes`; first 20 + count of bare archive records | `adopted_recs`, `arch_recs`, `bare` |
| the `_layout_findings` counters, fed one line at a time | the second loop over `snap_lines` |

Rules that keep the result identical:

1. **Acks are decided at the end.** At a join the current code approves it when the single eligible ack exists, lies after the join (`sole["index"] > i`), the two records share a file and the descriptors match. The walk continues the same way whether or not the join is approved (`seg_start = i`, `expected_prev = rec["prev"]`, `expected_seq = seq_i`). So record every join as pending; after the pass, approve the first pending join that meets the four conditions and delete its problem line and its `breaks` entry. Relative order of the remaining problems is unchanged.
2. **After the walk stops, keep reading.** Pass 1, `hashes`, `adopted_recs`, `bare` and the layout findings cover all lines today, also those after a `not valid JSON` stop. In `stopped` mode skip only the walk step.
3. **Problem order.** Emit `ack_problems`, then `walk_problems`, then the three ack summary lines, then state, anchors, archives, `odd_files`, strict layout: the order of today.
4. **`needed` is known before the pass.** The anchor file and the history lines are read under the lock. Collect their heads first, then test membership during the pass, and hand `seen_needed` to `_anchor_checks` in place of `hashes`.
5. **`--contains`.** Add `verify(..., want_hashes=None)`. The CLI passes the `--contains` list (possibly empty); `res.hashes` / `res.own_hashes` then hold only the wanted hashes that were seen. With `None` (library callers) the full sets are built as today.

Expected result: peak memory independent of N (a few MB plus the archive record list), shared lock held for D `stat` calls, run time slightly lower than today.

**Tests.**

1. `test_verify_streaming_matches_reference` (differential). Copy today's `Ledger.verify` body into `tests/_verify_reference.py`. For every tree of the fuzz corpus (300 mutations, strict and default), the kim-biseo fixture (unsealed, sealed, wrong ack, second break after the seal), and the ledgers built in `test_integrity.py`, assert that the reference and the new function return equal dicts, key by key, and that `set(wanted) & reference.hashes == new.hashes` for a list of wanted hashes.
2. `test_verify_peak_memory_is_bounded`: build 20 000 records with the bench builder, run `ledger.verify` under `tracemalloc`, assert a peak below 10 MB. Today's code needs ~45 MB.
3. `test_verify_does_not_parse_under_the_lock`: patch `gblog._parse_line_dups` with a wrapper that tries `flock(LOCK_EX | LOCK_NB)` on a second descriptor of `state/<bot>.lock` at its first call and records the outcome. Assert the lock was free.
4. `test_append_during_verify_is_not_seen`: patch `Ledger.lock` as `VerifySnapshotTest` does and append after the release. The result must report the old record count and head.
5. `test_replaced_day_file_fails_closed`: replace a day file between snapshot and read (same hook). Expect the new problem line and `ok` false.

---

### K4-03 — `_last_line` is quadratic; lines and side files have no size bound (Medium)

**Where.** `_last_line`, `iter_records`, `_chain_snapshot`, `_read_json`, `_anchor_snapshot`.

```python
# _last_line
buf = (fh.read(step) + buf).rstrip()
cut = buf.rfind(b"\n")
```

**Why it matters.**

- Each iteration copies the whole buffer and scans it again. A last line of length L costs L² / 64 KiB bytes of memory traffic: ~64 GiB for a 64 MiB line (tens of seconds), 16 times that for 256 MiB.
- `append` calls it while holding `LOCK_EX`. The tail is then refused as corrupt, so **every** later append repeats the work, and every writer of the chain (including the shell hook) waits.
- All line readers use `for raw in fh`, which reads a whole line into memory whatever its length. `_read_json` loads a whole side file. `_anchor_snapshot` keeps every history line.
- This is the inflation path for the "tree you audit" adversary: a 4 GiB one-line day file or `state/x.json` of repeated bytes costs a few MB in a git pack. `gblog verify --all --strict` on the clone then asks for 4 GiB or more.

**Reproduction.**

```python
from repro_helpers import *
import time
root = tempfile.mkdtemp(); raw_chain(root, "ops", [{}, {}])
with open(os.path.join(root, "logs", "ops", "2024-06-01.jsonl"), "ab") as fh:
    fh.write(b"x" * (64 << 20))                    # 64 MiB, no newline
t = time.perf_counter(); gblog.Ledger(root)._tail_record("ops"); print(time.perf_counter() - t)
```

**Fix 1 — linear `_last_line` with a cap.**

```python
_MAX_LINE_BYTES = 16 * 1024 * 1024   # longest log line gblog reads or writes


def _last_line(path: str, block: int = 65536, limit: int = _MAX_LINE_BYTES) -> Optional[bytes]:
    """Last non-blank line of a file (stripped); b"" if there is none; None if it is longer than ``limit``.

    Linear in the length of that line: each block is looked at once."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        parts: List[bytes] = []          # pieces of the last line, last piece first
        held = 0
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step)
            if not parts:
                chunk = chunk.rstrip()   # blank lines and white space after the last record
                if not chunk:
                    continue
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                parts.append(chunk[cut + 1:])
                break
            parts.append(chunk)
            held += len(chunk)
            if held > limit:
                return None
        return b"".join(reversed(parts)).strip()
```

In `_tail_record`: `if last is None: return path, {"_corrupt": True}`. Returning a truncated tail instead would be wrong: a crafted line could make its last 16 MiB a self-consistent record that `verify` never sees as one.

**Fix 2 — bounded line reader for every reader.**

```python
def _iter_lines(fh, limit: int = _MAX_LINE_BYTES) -> Iterator[Tuple[int, Optional[bytes]]]:
    """``(lineno, line)`` for each line of a binary file; ``None`` in place of a line longer than ``limit``."""
    lineno = 0
    while True:
        raw = fh.readline(limit + 1)
        if not raw:
            return
        lineno += 1
        if len(raw) > limit and not raw.endswith(b"\n"):
            while True:                  # skip the rest of the line in bounded pieces
                more = fh.readline(1 << 20)
                if not more or more.endswith(b"\n"):
                    break
            yield lineno, None
            continue
        yield lineno, raw
```

- `iter_records`, `_chain_snapshot` (or `_snapshot_lines` from K4-02), `_clamped_candidates`, `tune_history`, `_witness_present`, `_spool_replayed`: use `_iter_lines`; treat `None` as `{"_corrupt": True}`.
- `verify`: report `None` as `<file>:<n>: line longer than 16 MiB (not read)` instead of `not valid JSON`.
- `append`: after building the line, `if len(data) > _MAX_LINE_BYTES: raise RecordError("record is larger than 16 MiB")`, so gblog can never write a line it would not read.

**Fix 3 — size caps for side files.**

- `_read_json(path, limit=64 << 20)`: return `None` when `os.path.getsize(path) > limit`. Pass `limit=None` for the file-capture snapshot and the sync cache; both are local files that are never cloned and can legitimately be large.
- `_anchor_snapshot`: when the history file is larger than 64 MiB, add the problem `anchors/<bot>.history.jsonl: larger than 64 MiB (not read)`; otherwise read it with `_iter_lines`.

**Compatibility note.** The longest line the CLI can produce is below 1 MiB (argument limits). Only a direct `Ledger.append` call could have written a line above 16 MiB; such a ledger would change from OK to FAIL. If that risk is not acceptable, raise the constant; do not remove it.

**Tests.**

1. `test_last_line_equivalence`: for 2 000 random byte strings (lines, blank lines, trailing white space across the 64 KiB boundary, no final newline) compare the new function with a copy of the old one, with `block=7` and `block=65536`.
2. `test_last_line_is_linear`: wrap the file object and count bytes read. For a 4 MiB last line the count is below 5 MiB.
3. `test_overlong_tail_is_refused`: a 17 MiB last line (temporarily patch `_MAX_LINE_BYTES` to 64 KiB to keep the test small). `note` exits 4 and the log is unchanged.
4. `test_overlong_line_is_a_verify_problem_not_a_read`: `tracemalloc` peak below 4× the patched cap while `verify` reports the new problem line; the healthy chain beside it is `OK`.
5. `test_append_refuses_overlong_record`: `RecordError`, nothing written.
6. `test_oversized_state_file_is_unreadable`: a sparse 65 MiB `state/ops.json`; `status` exits 0, `verify` prints the "missing or unreadable" warning, and the peak stays small.

---

### K4-04 — Two redaction rules are quadratic; `append` redacts under the bot lock (Medium)

**Where.** `_SECRET_PATTERNS` (the env-style rule and the JWT rule), `redact`, `Ledger.append`.

```python
# env-style names (audit I-08)
r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-]){0,8}(?:token|secret|…)(?:[_-][A-Za-z0-9]+)*)\s*[:=]\s*(?:…)"
# JWT
r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
```

**Why it matters.**

- **Env rule.** The prefix is bounded (`{0,8}`), the suffix `(?:[_-][A-Za-z0-9]+)*` is not, and a match may start after every `_` or `-`. On `token_token_…_token` (k groups, no `=` behind it) each of the k start positions tries up to 9 prefix lengths, and each try runs the suffix to the end of the chain and back. That is ~9·k²/2 group iterations.
  - k = 2 000 (12 KB): seconds.
  - k = 20 000 (120 KB, one shell argument): minutes.
  - A 1 MiB line in an archived transcript: hours.
- **JWT rule.** `\b` holds after every `-`, and the first class contains `-`. On `eyJaaaaaaaaaa-` repeated, each start scans to the end of the run and back: n²/14 simple steps. Roughly seconds at 128 KiB, minutes at 1 MiB, hours at 16 MiB.
- The other rules were checked one by one: their scans either succeed, are bounded, or cannot overlap between start positions. The glued-name rule is bounded on both sides.
- `append` calls `_stored_value` (which calls `redact`) inside `with self.lock(bot):`. A slow redaction holds the chain's exclusive lock.
- Reach: page text that lands in a transcript (the daily routine archives transcripts), and any caller of `gblog log --detail`. The CLI-only adversary of `SECURITY.md` can stall any Bot's chain with one auto-approved command.

**Reproduction.**

```python
import time, gblog
for k in (500, 1000, 2000, 4000):
    s = "token_" * k
    t = time.perf_counter(); gblog.redact(s); print(k, round(time.perf_counter() - t, 2))   # ×4 per doubling
s = "eyJaaaaaaaaaa-" * 20000
t = time.perf_counter(); gblog.redact(s); print(round(time.perf_counter() - t, 2))
```

**Fix 1 — redact before the lock (exact).** In `append`, before `self.ensure()`:

```python
stored = {key: _stored_value(record, key, value) for key, value in record.items()
          if key not in ("v", "seq", "ts", "bot", "prev", "hash") and value is not None and value != ""}
```

Inside the lock, replace the `for key, value in record.items()` loop with `full.update(stored)`. The line is written with `sort_keys=True`, so the result is byte-identical.

**Fix 2 — linear env rule with the same result.** Three facts make an exact rewrite possible:

- A match can only start at the beginning of a group of a chain `G([_-]G)*`.
- Its name always ends at the end of that chain: what follows a shorter name is `_`, `-` or a letter, never `\s*[:=]`.
- Whether the assignment and value follow depends only on the text after the chain.

So decide once per chain.

```python
_ENV_KW = r"(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|access[_-]?key|private[_-]?key)"
_ENV_CHAIN_RE = re.compile(r"(?i)[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*")
_ENV_GROUP_RE = re.compile(r"(?i)[A-Za-z0-9]+")
_ENV_KW_RE = re.compile(r"(?i)" + _ENV_KW + r"(?![A-Za-z0-9])")
_ENV_TAIL_RE = re.compile(r"(?i)\s*[:=]\s*(?:\"[^\"\n]{1,}\"|'[^'\n]{1,}'|[^\s'\"&]{6,})")


def _redact_env_names(text: str) -> str:
    """The env-style rule (audit I-08) in one pass: same matches and same output as the former regular expression."""
    out: List[str] = []
    pos = 0
    for chain in _ENV_CHAIN_RE.finditer(text):
        start, end = chain.span()
        if start < pos:
            continue                                  # inside a value that was just redacted
        tail = _ENV_TAIL_RE.match(text, end)
        if tail is None:
            continue                                  # no assignment follows: nothing in this chain can match
        starts = [g.start() for g in _ENV_GROUP_RE.finditer(text, start, end)]
        first = next((i for i, g in enumerate(starts) if _ENV_KW_RE.match(text, g, end)), None)
        if first is None:
            continue
        name_start = starts[max(0, first - 8)]        # leftmost start with the keyword at most 8 groups ahead
        out.append(text[pos:name_start])
        out.append(text[name_start:end])
        out.append("=[REDACTED]")
        pos = tail.end()
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)
```

**Fix 3 — linear JWT rule.** Same technique: the first part of a JWT ends exactly where its run of `[A-Za-z0-9_-]` ends, so the remainder is checked once per run.

```python
_JWT_RUN_RE = re.compile(r"[A-Za-z0-9_-]+")
_JWT_HEAD_RE = re.compile(r"\beyJ")
_JWT_REST_RE = re.compile(r"\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")


def _redact_jwt(text: str) -> str:
    if "eyJ" not in text:
        return text
    out: List[str] = []
    pos = 0
    for run in _JWT_RUN_RE.finditer(text):
        start, end = run.span()
        if start < pos:
            continue
        rest = _JWT_REST_RE.match(text, end)
        if rest is None:
            continue
        at = text.find("eyJ", start, end)
        while at >= 0 and not (end - at - 3 >= 10 and _JWT_HEAD_RE.match(text, at)):
            at = text.find("eyJ", at + 1, end)
        if at < 0:
            continue
        out.append(text[pos:at])
        out.append("[REDACTED]")
        pos = rest.end()
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)
```

**Wiring.** Keep the list positions (rule order matters) with a small adapter, so `redact()` itself does not change:

```python
class _LinearRule:
    """A rule of _SECRET_PATTERNS implemented as a function; ``sub`` has the signature redact() calls."""
    def __init__(self, fn):
        self._fn = fn

    def sub(self, _repl, text):
        return self._fn(text)
```

Replace the two `(re.compile(...), repl)` entries by `(_LinearRule(_redact_jwt), None)` and `(_LinearRule(_redact_env_names), None)`.

**Tests.**

1. `test_env_rule_matches_former_regex` (differential). Keep the old compiled pattern in the test. Generate 200 000 strings with `random.Random(8)` from the tokens `token pwd api key apikey secret _ - = : space " ' & a Zz9 . \n` (1–40 tokens each). Assert `OLD.sub(r"\1=[REDACTED]", s) == gblog._redact_env_names(s)`. Add the fixed cases `a__b_token=secretvalue`, `x.token=secretvalue`, `token=ab pwd=longvalue1`, `token=pwd=longvalue1`, `token_ = value12`, and a name with 12 groups before the keyword.
2. `test_jwt_rule_matches_former_regex`: same scheme with the tokens `eyJ a×10 - _ . space é x`.
3. `test_redaction_is_linear_on_keyword_chains`: in a subprocess with a 5 s timeout (the pattern of `test_many_pem_begin_markers_do_not_stall_redaction`), `redact("token_" * 200000)` and `redact("eyJaaaaaaaaaa-" * 200000)`.
4. `test_append_redacts_outside_the_lock`: patch `gblog.redact` with a wrapper that probes `state/ops.lock` with `LOCK_EX | LOCK_NB` on a second descriptor; assert it was free.
5. All tests in `test_redaction_extended.py` and `RedactionTest` stay green unchanged.

---

### K4-05 — Archive cost: unbounded re-hash growth, undeclared size, `archive` memory (Medium)

**Where.** `verify_archives`, `_gunzip_sha256`, `archive_file`, CLI `archive`.

```python
size = r.get("bytes")
limit = size if _is_int(size) and size >= 0 else None      # None: read to the end
```

**Why it matters.**

- **Growth.** Every `verify` without `--skip-archives` decompresses and hashes all A bytes. A only grows: 5 Bots × 2 MB of transcript per day is ~3.6 GB per year. At ~250–400 MB/s that is ~10–15 s per year of history on every run, and ten times that with larger transcripts. After the K4-02 and K4-06 fixes this is the dominant term of the daily routine.
- **Undeclared or huge `bytes`.** Memory is bounded (1 MiB chunks), CPU is not. A record without a usable `bytes`, or with a very large one, makes `verify` read whatever the file expands to. A 10 MB gzip of zeros expands to 10 GB and compresses to a few KB inside a git pack. This cannot be refused: the record's `sha256` is the hash of that content, so only a full read can say OK. It can be made visible and schedulable.
- **`archive` memory.** The CLI reads the whole file, decodes it, redacts it, encodes it again and compresses it: ~4–5 times the file size at peak. A 1 GiB transcript needs ~5 GiB.

**Reproduction.**

```bash
python - <<'PY'
import gzip, hashlib, os, tempfile, gblog
root = tempfile.mkdtemp(); L = gblog.Ledger(root); L.init()
h = hashlib.sha256()
os.makedirs(os.path.join(root, "archive", "ops", "2024-06-01"))
tmp = os.path.join(root, "bomb.gz")
with gzip.open(tmp, "wb") as fh:
    for _ in range(4096):                       # 4 GiB of zeros, ~4 MB on disk
        fh.write(b"\0" * (1 << 20)); h.update(b"\0" * (1 << 20))
rel = "archive/ops/2024-06-01/%s.gz" % h.hexdigest(); os.replace(tmp, os.path.join(root, rel))
L.append("ops", {"type": "archive", "action": "file", "target": "x", "outcome": "ok",
                 "sha256": h.hexdigest(), "path": rel, "ts": "2024-06-01T10:00:00Z"})   # no "bytes"
print(root)
PY
time python gblog.py --root <root> verify --bot ops        # OK after reading 4 GiB
```

**Fix.**

1. **Make the cost visible.** In `Ledger.verify`, sum the declared `bytes` of the chain's archive records and count the bytes actually read. Add the additive result keys `archive_bytes_declared` and `archive_bytes_read`. Print them only with `--json`.
2. **Rotation (new, opt-in flag).** `verify --archive-rotation K` and `anchor --archive-rotation K`:
   - for every archive record still check the path rules, the address rule and that a regular file exists (as today);
   - re-hash a file when its date directory is today or yesterday (UTC), or when `date.toordinal() % K == today.toordinal() % K`;
   - end the line with `(M of N archived file(s) re-hashed, rotation K)`.

   A deleted file is found on every run; a modified old file within K days. The default (no flag) stays a full re-hash. This is a trade the operator must accept in writing, because a rotated run is weaker than a full one.
3. **Auditor guidance** (`SECURITY.md`, "Auditing a clone"): first `verify --all --strict --skip-archives`, then read `archive_bytes_declared` from `--json`, then decide on the full run.
4. **`archive --no-redact` and binary content.** Add an internal `archive_stream(ledger, bot, fileobj, …)` that hashes and compresses in 1 MiB chunks into `<address dir>/<pid>.tmp` and renames to `<sha256>.gz` when done. Use it when `--no-redact` is given. Text that must be scanned still needs the whole text, because rules span lines; document the ~5× peak in the README.

**Tests.**

1. `test_rotation_checks_every_file_within_k_days`: 10 archives over 10 date directories, `now_iso` patched day by day, K = 5. Each path is opened by `_gunzip_sha256` (wrapped with `mock.patch.object(..., wraps=...)`) at least once in any 5 consecutive days.
2. `test_rotation_still_detects_a_missing_file_every_day`.
3. `test_default_verify_rehashes_everything`: without the flag, `_gunzip_sha256` is called once per distinct address.
4. `test_json_reports_archive_bytes`.
5. `test_no_redact_archive_streams`: `tracemalloc` peak below 8 MiB for a 64 MiB input; stored content hashes to the recorded `sha256`.

---

### K4-06 — The daily routine walks every chain three times (Medium)

**Where.** `docs/routine-daily-audit.md` steps 1–2, `examples/routine-daily-audit.sh`, CLI `verify` / `anchor`.

**Why it matters.**

- Step 1 runs `verify --bot <chain> --skip-archives --contains <head>` per chain, step 2 runs `verify --all --record`, then `anchor --skip-archives`. That is three full walks per chain: ~15 s and three ~1 GB peaks per 500k-record chain today.
- `examples/routine-daily-audit.sh` calls plain `gblog anchor`, so the scheduler version also re-hashes all archives twice per night.
- Incremental verification from an on-box checkpoint is **not** a sound way to save work: the checkpoint is as writable as the log. The saving has to come from doing one walk and using its result three times.

**Fix.**

1. **Example script (one line).** `run anchor anchor --skip-archives`.
2. **`verify --all --record --anchor`.** After the `verify` record for a chain has been appended, the anchor entry can be derived without a second walk when the chain is OK or PASS and `rec["prev"] == res["head"]`. Then the chain is exactly the verified snapshot plus one record gblog just wrote, so `records = res["records"] + 1` and `head = rec["hash"]`; the other fields come from `res`. In every other case (FAIL chain, another writer got in between, the record was refused) call the existing per-chain anchor path. Hold `state/.anchors.lock` for the run, as `anchor` does.
3. **`verify --all --expect-heads FILE`.** FILE holds `<chain> <64-hex>` lines. For each line the hash must be in that chain's `own_hashes`, with the problem text `--contains` prints today. This is chain-exact, so the reason `--contains` was barred from `--all` (R-08) does not apply.
4. Routine prompt: one command, `gblog verify --all --record --anchor --expect-heads <file>`; keep the old three commands as the fallback for 0.6.0.

**Tests.**

1. `test_anchor_from_verify_matches_separate_anchor`: on two copies of one ledger run the combined command and the two separate commands with `now_iso` frozen; `anchors/*.json`, the history lines and the rollup are equal.
2. `test_combined_run_walks_each_chain_once`: wrap `Ledger.verify` and count calls per chain: 1 for OK chains, 2 for a FAIL chain.
3. `test_interleaved_append_forces_a_second_walk`: append from the lock hook between verify and record; the anchor head is the true tail.
4. `test_expect_heads_miss_is_exit_1` and `test_expect_heads_is_chain_exact` (the decoy-chain setup of `test_contains_is_not_satisfied_by_a_decoy_chain`).
5. `tests/test_examples.py`: the stub log shows `anchor --skip-archives`.

---

### K4-07 — `spool replay` scans the chain once per replayed event (Medium)

**Where.** `Ledger.spool_replay` (`_still_pending`), `Ledger._spool_replayed`.

```python
def _still_pending(...):
    if not os.path.exists(path) or self._spool_replayed(bot, spool_id, since_day):
        raise _GuardSkip()
```

**Why it matters.**

- The guard runs inside `append`, under `LOCK_EX`, for every event. Each call lists the chain directory and reads every line of every day file since the refusal day.
- With R lines in that range and S events the total is S·R + S²/2 line visits: ~40 s for S = 5 000, R = 20 000.
- When the event has no usable bound (`_spool_scan_from` returns `None`) R is the whole chain: ~15 minutes for S = 5 000 on a 500k chain, with the lock held ~0.2 s per event.
- A chain in tail mismatch with the shell hook installed spools every command, so S in the thousands is the normal shape of this incident.

**Reproduction.**

```python
import gblog, io, time
from contextlib import redirect_stderr
L = gblog.Ledger("<bench ledger from --keep>")
st = dict(L.read_state("bench")); L.write_state("bench", {"seq": 10**9, "head": "cd" * 32})
with redirect_stderr(io.StringIO()):
    for i in range(2000):
        try: L.append("bench", {"type": "note", "detail": "refused %d" % i})
        except gblog.TailMismatchError: pass
L.write_state("bench", st)
t = time.perf_counter(); L.spool_replay("bench"); print(time.perf_counter() - t)
```

**Fix.** Keep one index per `spool_replay` run and read only bytes appended since the last look. The guard still runs under the lock, so the bytes it reads are a consistent prefix, and a concurrent replay is still seen.

```python
class _ReplayedIds:
    """Spool ids already present in a chain: ``{id: newest day file that holds it}``.

    ``refresh`` reads only what was appended since the previous call. Call it under the bot lock."""

    def __init__(self, ledger: "Ledger", bot: str, since_day: Optional[str]):
        self.ledger, self.bot, self.since_day = ledger, bot, since_day
        self.days: Dict[str, str] = {}
        self.offsets: Dict[str, int] = {}

    def refresh(self) -> None:
        for path in self.ledger.log_files(self.bot):
            name = os.path.basename(path)
            if self.since_day and _DAY_FILE_RE.match(name) and name[:10] < self.since_day:
                continue
            done = self.offsets.get(path, 0)
            size = os.path.getsize(path)
            if size < done:
                done = 0                         # replaced outside gblog: read it again
            if size == done:
                continue
            with open(path, "rb") as fh:
                fh.seek(done)
                for raw in fh:
                    if b"spool_id" in raw or b"\\u" in raw:
                        sid = _parse_line(raw).get("spool_id")
                        if isinstance(sid, str) and name[:10] > self.days.get(sid, ""):
                            self.days[sid] = name[:10]
                self.offsets[path] = fh.tell()

    def replayed(self, spool_id: str, since_day: Optional[str]) -> bool:
        self.refresh()
        day = self.days.get(spool_id)
        return day is not None and (since_day is None or day >= since_day)
```

In `spool_replay`:

- Before the loop, compute each pending event's bound with `_spool_scan_from` and create `index = _ReplayedIds(self, bot, bound)`, where `bound` is the minimum of the per-event bounds, or `None` when any event has none.
- In the guard: `if not os.path.exists(path) or index.replayed(spool_id, since_day): raise _GuardSkip()`.

The per-event `since_day` test keeps today's rule exactly: an id found only in a file older than that event's bound does not count. The byte pre-filter (`spool_id` or a `\u` escape) is a superset of today's filter.

In the same area, `_refuse_tail` lists the whole spool directory on every refusal (`nth = sum(1 for f in os.listdir(spool_dir) …)`), which is O(S²) over an incident. Read the previous `state/<bot>.alert.json` instead (it is replaced on every refusal, under the lock): when its `ts` gives the same stamp, use its counter plus one; fall back to the listing only when the alert file is missing or unreadable. Store the counter as an additive key `nth` in the payload.

**Tests.**

1. `test_replay_reads_each_log_line_once`: with the line-counting helper from section 5, 200 spooled events on a day file of 2 000 lines read fewer than 5 000 log lines in total (today: more than 400 000).
2. `test_replay_after_crash_between_append_and_move` and `test_concurrent_spool_replay_appends_each_event_once`: unchanged and green.
3. `test_id_in_an_older_file_does_not_count`: plant a record with the event's `spool_id` in a day file older than its bound; the event is replayed.
4. `test_refusals_do_not_list_the_spool`: patch `os.listdir` to count calls on the spool directory during 50 refusals; at most 1. `test_spool_file_names_sort_in_refusal_order` stays green.

---

### K4-08 — `render_report` searches every task for every open task (Medium)

**Where.** `render_report`, the "twin" search.

```python
for oid in order:
    o = tasks[oid]
    if (oid != tid and o["end"] and o["title"] and " ".join(str(o["title"]).lower().split()) == key
            and (o.get("end_chain") or o["chain"]) != t["chain"] ...
```

**Why it matters.**

- For each task without an outcome the loop visits every task of the Bot and normalises the title of every closed one again. With T tasks and T_open open ones this is T_open × T.
- A Bot that does not close tasks is a failure mode `tune` has its own proposal for, so T_open ≈ T is realistic. `report --since <a year ago>` with 20 000 open and 20 000 closed tasks is ~4 × 10⁸ inner steps: several minutes.
- The daily report (hundreds of tasks) is unaffected.

**Reproduction.**

```python
import gblog, time
recs = [{"v": 1, "seq": i, "ts": "2024-06-01T10:00:00Z", "bot": "ops", "type": "task_start",
         "task_id": "t%d" % i, "task": "Task %d" % (i % 50)} for i in range(1, 20001)]
recs += [{"v": 1, "seq": 20000 + i, "ts": "2024-06-01T11:00:00Z", "bot": "ops", "type": "task_end",
          "task_id": "t%d" % i, "outcome": "ok"} for i in range(1, 10001)]
t = time.perf_counter(); gblog.render_report(recs, "r"); print(time.perf_counter() - t)
```

**Fix.** Index the closed tasks once per Bot, in `order`, and skip the scan when no other chain closed a task of that title.

```python
def _title_key(title: object) -> str:
    return " ".join(str(title).lower().split())

# after the loop that fills ``tasks`` and ``order``, before ``for tid in order``:
closed: Dict[str, List[str]] = {}          # title key -> closed task ids, in report order
closed_chains: Dict[str, set] = {}         # title key -> chains that closed such a task
for oid in order:
    o = tasks[oid]
    if o["end"] and o["title"]:
        k = _title_key(o["title"])
        closed.setdefault(k, []).append(oid)
        closed_chains.setdefault(k, set()).add(o.get("end_chain") or o["chain"])

# in the open-task branch:
key = _title_key(t["title"])
if closed_chains.get(key, set()) - {t["chain"]}:
    for oid in closed[key]:
        o = tasks[oid]
        if (oid != tid and (o.get("end_chain") or o["chain"]) != t["chain"]
                and (o["start"] or o["end"]) >= (t["start"] or "")):
            twin = (oid, o)
            break
```

The candidates are visited in the same order with the same conditions, so the first hit is the same task.

**Tests.**

1. `test_open_task_hint_needs_same_title_and_other_chain` (existing): unchanged.
2. `test_twin_search_matches_reference`: 300 random task sets (titles from 5 values with case and spacing variants, 3 chains, random open or closed). The rendered report equals the output of a copy of today's function.
3. `test_report_with_many_open_tasks_is_fast`: the reproduction with 20 000 + 10 000 records. Patch `gblog._title_key` with a counting wrapper and assert at most 40 000 calls. As a second guard, run it in a subprocess with a 20 s timeout.

---

### K4-09 — `export`, `report --since` and `records()` build and sort the full list (Low)

**Where.** `Ledger.records`, `Ledger.chain_records`, CLI `export`.

**Why it matters.** `gblog export` loads every record of every selected chain (~2.2 KB each), sorts, then prints. Ten chains of 500k records need ~11 GB. `gblog export | head` waits for all of it.

**Fix (exact output, ~10× less memory).** Sort an index, not the records.

1. For each selected chain and file, read lines with their byte offset. Parse each line once and keep only `(ts, str(bot), seq_int, chain_no, file_no, offset, length)`, applying the rules of `_readable` and the `since` filter. Intern the `bot` string.
2. Sort the index with the key `(ts, bot, seq)`: the same key and the same stability as `records()`.
3. For each entry, seek, read, parse again and write with `_write_json(out, rec, sort_keys=True)`.

The second parse costs CPU (about +60 % for `export`) and saves memory: ~250 bytes per record instead of ~2.2 KB. Call `_note_skipped` per chain as today.

`report --since`, `coverage` and `tune` need the records for their analysis. For those, reduce what is kept: `coverage` needs only `action` records with six fields, so filter in the reader before the list is built.

**Tests.**

1. `test_export_stream_equals_records`: on a ledger with alias chains, a clamped record, a corrupt line and a chain whose timestamps go backwards, the output is byte-equal to `"".join(dump(r) for r in ledger.records(raw=True))`.
2. `test_export_peak_memory`: 20 000 records, `tracemalloc` peak below 12 MB (today ~45 MB).
3. `test_skipped_lines_are_announced` (existing): unchanged.

---

### K4-10 — `status` builds and sorts the 30-day window to compute two numbers (Low)

**Where.** CLI `status`.

```python
recent = [r for c in chains for r in ledger.chain_records(c, since=month)]
...
for r in sorted(recent, key=lambda r: str(r.get("ts") or "")):
```

**Why it matters.** Skill and integration guide tell a Bot to run `gblog status --json --bot <slug>` for the footer of **every reply**. Each call parses M30 records, keeps them, sorts them per chain and again per group. With M30 = 50 000 that is ~0.4 s and ~110 MB per reply.

**Fix.** Keep only the task events and count `today` while reading.

```python
events = []                                   # (ts, type, task_id) of task records only
today_n = 0
for c in chains:
    part = []
    for r in ledger.iter_chain_records(c, since=month):     # generator form of chain_records, file order
        clock = r.get("clock_regressed")
        if str((clock if isinstance(clock, str) else r.get("ts")) or "").startswith(today_day):
            today_n += 1
        if r.get("task_id") and r.get("type") in ("task_start", "task_end"):
            part.append((str(r.get("ts") or ""), _seq_int(r.get("seq")), r["type"], r["task_id"]))
    part.sort(key=lambda e: e[:2])            # the order chain_records gives
    events.extend(part)
events.sort(key=lambda e: e[0])               # stable, as the current sorted(recent, key=ts)
open_tasks = set()
for _ts, _seq, kind, tid in events:
    (open_tasks.add if kind == "task_start" else open_tasks.discard)(tid)
```

`iter_chain_records` is `chain_records` without the list and the sort; it yields what `_readable` yields after the `since` filter, so the skipped-line warning is unchanged. A stable sort of the task records alone gives the same relative order as sorting all records and filtering, so the open-task set is the same.

**Tests.**

1. `StatusCountsTest` (existing, three tests): unchanged.
2. `test_status_matches_reference_on_unsorted_chain`: a chain whose timestamps go backwards and a group of three alias chains; `today` and `open_tasks` equal the values of a copy of today's loop.
3. `test_status_keeps_only_task_events`: 20 000 records in the window, 100 tasks; `tracemalloc` peak below 6 MB.

---

### K4-11 — `tune` recomputes each metric with a full pass (Low)

**Where.** `analyze` (`add`, section J), `compute_metric`.

```python
baseline = compute_metric(metric_records(metric), metric, since=since) if metric else None
...
after = compute_metric(metric_records(metric), metric, since=r.get("ts"))
```

**Why it matters.** Each proposal (P) and each applied change (E) filters all n records of the Bot again; `slow` and `open_tasks` rebuild the task table; `coverage` reruns `coverage()`. The total is (P + E) × n. With the default 90-day load this is a few seconds; with `--days 36500` on a 500k chain and 200 proposals it is minutes.

**Fix.** Build one index per Bot in `analyze` and answer metrics with `bisect`:

- `fail`: `{(action, norm_target): sorted ts list}`;
- `denied`, `approvals`: `{norm_title: sorted ts list}`;
- `manual_tasks`: `{norm_title: sorted ts list}` of non-routine `task_start` records;
- `slow`, `open_tasks`: one task table per Bot;
- `coverage`: one `coverage()` result per `(bot, since)`.

`count(since, until) = bisect_left(ts, until) - bisect_left(ts, since)`, which is the half-open interval `compute_metric` uses. `slow` and `open_tasks` depend on `since` and need one filtered pass per distinct `since` value; memoise by `since`. Keep `compute_metric` as the public function and as the test oracle.

**Tests.**

1. `test_metric_index_matches_compute_metric`: 500 random metrics over a random record list (four kinds, random `since` and `until`); the index answers equal `compute_metric`.
2. `test_analyze_calls_no_full_pass_per_proposal`: wrap `compute_metric` with a counter; `analyze` on 200 failing targets calls it 0 times.
3. `TuneLoopTest`, `TuneRulesGapTest`, `TuneWindowTest` (existing): unchanged.

---

### K4-12 — Unbounded result lists flow into anchors and rollups (Low)

**Where.** `Ledger.verify` (walk), `Ledger._anchor`, `Ledger.orphan_witnesses`.

**Why it matters.**

- The walk adds one problem, one `breaks` entry and one `segments` entry per join, without a cap. The layout findings are capped at 20; these are not.
- A chain with 100 000 joins (every second line removed) gives ~30 MB of `segments`. `_anchor` writes that into `anchors/<bot>.json`, appends all segment heads to the history, and merges the entry into the day rollup, every day.
- `orphan_witnesses` parses **every** day rollup on each `verify --all` and `anchor`. For a healthy ledger that is Y small files (~2 000 after five years, ~0.1 s). With a bloated entry it becomes Y × 30 MB.

**Fix.**

- In `_anchor`, for a FAIL entry keep `problems[:50]`, `segments[:50]`, and the first 50 and last 50 `segment_heads`, with the additive keys `problems_total`, `segments_total`. The history line gets the same capped head list.
- In `_verify_text`, print the first 200 problem lines and then `      - <n> more problem(s)`. `--json` stays complete.
- In `orphan_witnesses`, skip a rollup larger than the side-file cap (K4-03).

A chain with more than 50 unapproved joins is FAIL in every case; the cap only limits what is stored about it.

**Tests.**

1. `test_anchor_entry_of_a_shredded_chain_is_capped`: 2 000 records, every second line removed; `anchors/<bot>.json` is below 64 KiB and `segments_total` is 1 000.
2. `test_verify_still_fails_after_capped_anchor`: containment check and exit code unchanged.
3. `test_verify_text_is_capped_json_is_not`.

---

### K4-13 — Smaller items (Low)

| # | Where | Observation | Change |
|---|---|---|---|
| a | `rebuild_state` | After a full `verify` it parses the whole chain again with `_chain_items` under `LOCK_EX`, only to read the tail and to test whether the state head is in the chain. | Use `_tail_record(bot)` for the tail; when `tail["hash"] == res["head"]`, test `old_head in res.hashes`. |
| b | `seal_break` | Parses the whole chain twice more for `break_ack`, once under the lock. | Pre-filter lines with `b"break_ack" in raw or b"\\u" in raw` before parsing. |
| c | `_task_start_seen` | For an id without a `task_start` it parses 30 days of every chain of the Bot (~1 s at 150k records). | Parse only lines where `b"task_start" in raw or b"\\u" in raw`. |
| d | `tail_records` | Parses the whole newest day file for `tail -n 1`. | Read the file backwards in blocks until n lines are found (the technique of `_last_line`). |
| e | `capture_files` | `json.dump(doc, fh)` uses the pure-Python encoder: seconds for 200k files. The `earlier` check parses a whole chain once. | `fh.write(json.dumps(doc))`; pre-filter lines with `b"capture"`. |
| f | `capture_browser` | `fetchall()` holds every row; the whole history database is copied once per Bot per run. | Iterate the cursor before the temporary copy is removed; in the weekly routine pass the same `--history` copy to all Bots. |
| g | `_anchor` | Appends a history line on every run, also when nothing changed. | Skip the append when the last line has the same `ok`, `records` and `heads`. |
| h | module import | ~27 patterns are compiled on every start, also for `status` and `verify`. | Compile `_SECRET_PATTERNS` on first use of `redact()`. Saves ~10 ms per invocation. |
| i | `Ledger.append` design | 3 fsync per record (6 for the first of a chain). Capture runs pay this per record. | Keep. A batch API would break the "state is at most one record behind" rule that roll-forward depends on. `test_append_fsync_order` already pins the sequence. |
| j | shell hook | Each prompt waits for one interpreter start plus one append. | Document the latency; the K4-02 fix removes the multi-second stalls. |

Each change gets one equivalence test against a copy of today's function and one counting test (lines parsed or bytes read).

---

### K4-14 — Cost claims in the documentation that the code does not match (Info)

| Claim | Where it is made | Code under review |
|---|---|---|
| A gap above 10 000 records has an empty `missing_seqs` and `missing_count` | CHANGELOG D1-02, DESIGN §7 | Not implemented (K4-01). |
| `sync` re-hashes on every run; the cache was removed | CHANGELOG D3-05, DESIGN §13, SECURITY | `sync` passes `.git/gblog-sync-cache.json` to `_sync_drift`, and `test_sync_cache_does_not_hide_a_changed_file` depends on it. Decide which is intended; the §13 row for `sync` is wrong either way. |
| `verify` reads the newest day rollup that names the chain | CHANGELOG D1-05, DESIGN §8, §13 | `_anchor_checks` reads no rollup. Only `orphan_witnesses` reads rollups, and it reads all of them. |
| File-capture records carry `observed` | CHANGELOG D4-01, DESIGN §6 | `capture_files` writes no `observed`. All records of one run share one `ts`, so `file` coverage matches on the run time. |
| `tail -n k` costs O(C × k) | DESIGN §13 | It parses the whole newest day file of each chain (K4-13 d). |
| `verify`: "the raw text of the lines is kept only with `--strict`" | DESIGN §13 | True. The table omits that the parsed records (~2 KB each) are kept in every mode and that parsing happens under the lock (K4-02). |

After the fixes, add two columns to the §13 table: peak memory, and time under the bot lock.

---

## 3. Benchmark design

Extend `tests/bench_large_ledger.py` (still not collected by `unittest discover`).

**Options.**

| Option | Purpose |
|---|---|
| `--records 500000` | already works; document 50k / 200k / 500k as the three reference sizes |
| `--chains C` | C chains of `--records` each, for `verify --all`, `export`, `sync` |
| `--archives MB` | adds archive records and `.gz` files totalling that many MB |
| `--child` | run each timed command in a child process and report its peak RSS |
| `--probe-lock` | measure the longest append latency while `verify` runs |
| `--scenario NAME` | one of the adversarial fixtures below |
| `--json` | one JSON object per measurement, for comparing two runs |

**Peak RSS per command.** `RUSAGE_CHILDREN` is a running maximum, so each child reports its own figure:

```python
CHILD = ("import resource, sys, gblog\n"
         "rc = gblog.main(sys.argv[1:])\n"
         "sys.stderr.write('\\nMAXRSS_KB %d\\n' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n"
         "sys.exit(rc)\n")

def child(root, argv):
    t = time.perf_counter()
    p = subprocess.run([sys.executable, "-c", CHILD, "--root", root] + argv, cwd=REPO,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rss = int(p.stderr.decode().rsplit("MAXRSS_KB", 1)[1].split()[0])   # KiB on Linux, bytes on macOS
    return time.perf_counter() - t, rss, p.returncode
```

**Lock probe.**

```python
def probe_lock(root, bot, seconds):
    """Append every 20 ms for ``seconds``; return the longest time one append took."""
    ledger, worst, end = gblog.Ledger(root), 0.0, time.time() + seconds
    while time.time() < end:
        t = time.perf_counter()
        ledger.append(bot, {"type": "note", "detail": "probe"})
        worst = max(worst, time.perf_counter() - t)
        time.sleep(0.02)
    return worst
```

Run it in a thread started just before the `verify` child. Expected: ~2.5 s today at 500k, a few ms after K4-02.

**Scenarios.**

| Name | Fixture | Finding |
|---|---|---|
| `seqgap` | third record with `seq` 3 × 10⁸ (run under `ulimit -v 2000000`) | K4-01 |
| `longline` | 64 MiB last line without newline; time one `note` | K4-03 |
| `chains` | `redact("token_" * k)` for k = 1k, 2k, 4k, 8k; print the growth factor per doubling | K4-04 |
| `gzipbomb` | the K4-05 reproduction; time `verify` with and without `--skip-archives` | K4-05 |
| `spool` | 2 000 refused events on the bench chain; time `spool replay` | K4-07 |
| `opentasks` | 20 000 open and 10 000 closed tasks; time `report --since` | K4-08 |

**Budgets to record in the PR that applies the fixes** (500k records, one chain, warm cache): `verify` peak RSS below 100 MB; longest append during `verify` below 50 ms; `status --bot` below 30 MB; `export` below 250 MB; each scenario finishes in under 10 s.

---

## 4. Test design for CI

Wall-clock assertions are unreliable on shared runners. Use counters and `tracemalloc`; use a subprocess timeout only as a coarse second guard, with a factor of at least 20 between the expected time and the limit.

**Line-counting helper** (add to `tests/_support.py`):

```python
import builtins
from contextlib import contextmanager
from unittest import mock

@contextmanager
def count_log_lines(root):
    """Count the lines gblog reads from files under <root>/logs. Yields a dict with the key "lines"."""
    seen = {"lines": 0}
    logs = os.path.join(os.path.abspath(root), "logs") + os.sep

    class Counting:
        def __init__(self, fh): self._fh = fh
        def __iter__(self): return self
        def __next__(self):
            line = next(self._fh); seen["lines"] += 1; return line
        def readline(self, *a):
            line = self._fh.readline(*a); seen["lines"] += bool(line); return line
        def __enter__(self): self._fh.__enter__(); return self
        def __exit__(self, *exc): return self._fh.__exit__(*exc)
        def __getattr__(self, name): return getattr(self._fh, name)

    def counting_open(path, mode="r", *args, **kwargs):
        fh = builtins.open(path, mode, *args, **kwargs)
        if "b" in mode and "r" in mode and os.path.abspath(str(path)).startswith(logs):
            return Counting(fh)
        return fh

    with mock.patch.object(gblog, "open", counting_open, create=True):
        yield seen
```

**New test module `tests/test_scale.py`** (tests named in the findings):

| Test | Asserts | Finding |
|---|---|---|
| `test_seq_gap_is_described_by_size` and four more | results, not time | K4-01 |
| `test_verify_streaming_matches_reference` | dict equality on the fuzz corpus and the fixture | K4-02 |
| `test_verify_peak_memory_is_bounded` | `tracemalloc` peak below 10 MB at 20k records | K4-02 |
| `test_verify_does_not_parse_under_the_lock` | lock probe free at first parse | K4-02 |
| `test_last_line_equivalence`, `test_last_line_is_linear` | equality; bytes read | K4-03 |
| `test_env_rule_matches_former_regex`, `test_jwt_rule_matches_former_regex` | 200k differential cases each | K4-04 |
| `test_redaction_is_linear_on_keyword_chains` | subprocess, 5 s timeout | K4-04 |
| `test_replay_reads_each_log_line_once` | `count_log_lines` below 5 000 | K4-07 |
| `test_report_with_many_open_tasks_is_fast` | call counter | K4-08 |
| `test_export_stream_equals_records`, `test_export_peak_memory` | byte equality; peak | K4-09 |
| `test_status_keeps_only_task_events` | peak below 6 MB | K4-10 |
| `test_metric_index_matches_compute_metric` | equality | K4-11 |

**Rules for these tests.**

- Build large chains with the raw writer of the benchmark (`build`), never through `Ledger.append`; 20 000 fsynced appends would dominate the test.
- Start `tracemalloc` immediately before the call under test and stop it immediately after, so fixture building is not counted.
- Every "reference" function lives in the test tree as a frozen copy of the 0.6.1 code, with a header comment naming the commit it was copied from.
- Add the huge-`seq` values to the fuzz mutators once K4-01 is fixed, so the fuzz corpus covers the path.

---

## 5. Suggested order of work

1. **K4-01** (three small edits, removes a one-line kill of `verify --all` and `anchor`).
2. **K4-03 fix 1** and **K4-04 fix 1** (linear `_last_line`, redaction outside the lock): small, exact, and they remove the two ways to stall a chain's lock.
3. **K4-06 item 1** (one flag in the example script) and **K4-08** (exact, local).
4. **K4-02 stage 1**, then **stage 2** behind the differential test. This is the change that makes 500k-record chains comfortable on a small host.
5. **K4-07**, **K4-04 fixes 2–3**, **K4-03 fixes 2–3**.
6. **K4-06 items 2–4** and **K4-05** (new flags; need the operator's decision on rotation).
7. **K4-09 … K4-13**, then update DESIGN §13 from benchmark output (K4-14).

Decisions needed from the maintainer before work starts:

- the value of `_MAX_LINE_BYTES` and whether an existing over-long line may turn a chain FAIL (K4-03);
- whether `verify --archive-rotation` is acceptable as an opt-in, given that it trades detection latency for run time (K4-05);
- whether the sync cache stays or goes, since code and documentation disagree (K4-14).