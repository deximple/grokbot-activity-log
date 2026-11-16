# Deep review 1 — verify, the hash chain walk, state witnesses, anchors, seal and break acks

Scope: `Ledger.verify` and its helpers, the append-side tail check where it meets verify, `rebuild_state`, `anchor` / `_anchor`, `seal_break`, the `verify` / `anchor` / `seal` CLI branches, and the docs that describe them.

Method: static read of the current tree. Nothing was executed, so every reproduction is traced through the code, not observed. All reproductions use a temporary root.

The reproductions share this preamble:

```python
import json, os, shutil, tempfile, gblog
root = tempfile.mkdtemp(prefix="gblog-d1-"); L = gblog.Ledger(root); L.init()
T = "2024-06-01T10:00:%02dZ"
def seed(bot, n):
    return [L.append(bot, {"type": "note", "detail": str(i), "ts": T % i}) for i in range(n)]
def raw_line(bot, seq, prev, **extra):            # a hash-valid line written around gblog
    rec = dict({"v": 1, "seq": seq, "ts": T % 50, "bot": bot, "type": "note", "prev": prev}, **extra)
    rec["hash"] = gblog.compute_hash(rec)
    with open(os.path.join(root, "logs", bot, "2024-06-01.jsonl"), "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return rec
```

## 1. Summary

| ID | Sev | Finding |
|---|---|---|
| D1-01 | High | A FIFO, a device, or a symlink to one at a state, anchor, rollup or archive path hangs or exhausts memory in `verify --all`, `anchor` and `append` |
| D1-02 | High | One record with a large `seq` gap makes `verify` build `list(range(...))` of that size |
| D1-03 | Medium | `anchor` isolates only the verify step per chain; a failing pre-check or write stops every later chain |
| D1-04 | Medium | A chain directory that cannot be listed is verified as an empty chain (`OK … 0 records` when no witness exists) |
| D1-05 | Medium | Day rollups are a witness for a deleted chain but not for a truncated or rewritten one |
| D1-06 | Medium | A state file that `verify` accepts while `append` exits 4 and `state rebuild` says "restore the log from backup" |
| D1-07 | Low-Med | `verify --bot <Bot>` does not report a deleted variant chain whose witness survives |
| D1-08 | Low-Med | Records are not bound to their day file; a renamed file passes `--strict` and drops out of `report --date` |
| D1-09 | Low | After a legacy tail (`--ts` with an offset), `append` can write a record that fails `verify` forever |
| D1-10 | Low | `anchor` still replaces the latest anchor with a 0-record entry when day files were emptied |
| D1-11 | Low | `seal` does not preflight its own chain; an ack without a witness cannot be repaired by any command |
| D1-12 | Low | A record with a `_corrupt` key bricks its chain |
| D1-13 | Low | `(N archived file(s) intact)` counts records that name no file |
| D1-14 | Docs | Docs describe verify, anchor and state behaviour that the code no longer has |
| D1-15 | Cleanup | About 120 lines of dead or unreachable code in this area |

D1-01, D1-02 and D1-03 each match the vulnerability class that `SECURITY.md` itself names: "a single file or record that stops `verify --all` or `anchor` from reporting the other chains".

## 2. Findings

### D1-01 (High) — Side files that are not regular files hang or exhaust `verify`, `anchor` and `append`

B:L2 closed this for `logs/<bot>/*.jsonl` only. Every other file that verify reads is opened without a type check:

```python
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
```
```python
# Ledger._anchor_snapshot (runs under the shared bot lock)
if os.path.exists(hist):
    history = []
    with open(hist, "rb") as fh:
        for lineno, raw in enumerate(fh, 1):
```

Paths affected:

- `state/<name>.json`, read by `_load_state` and by `orphan_witnesses`.
- `anchors/<bot>.json` and `anchors/<bot>.history.jsonl`, read by `_anchor_snapshot`.
- `anchors/*.history.jsonl` and `anchors/days/*.json`, read by `orphan_witnesses`.
- `archive/.../<sha>.gz`, where `verify_archives` checks `os.path.exists` and then calls `gzip.open`.

Git cannot store a FIFO, but it stores symlinks. A committed `state/ghost.json -> /dev/zero` therefore reaches the auditor of a clone, the position `SECURITY.md` puts in the threat model.

Reproduction:

```python
seed("ops", 2); seed("zzz", 1)
os.mkfifo(os.path.join(root, "state", "ghost.json"))
# $ timeout 10 gblog --root <root> verify --all ; echo $?
#   -> 124 and no output: orphan_witnesses() blocks before the first chain is printed
```

Variants:

- `os.symlink("/dev/zero", <root>/state/ghost.json)`: `json.load` reads until memory runs out (`MemoryError` under `ulimit -v`, the OOM killer otherwise). No chain is reported.
- A FIFO at `anchors/ops.history.jsonl` or `state/ops.json`: `verify` blocks while it holds the shared lock, so `gblog note --bot ops` blocks too.
- A FIFO at `state/ops.json`: `append` blocks in `_append_head` while it holds the exclusive lock.
- An archived `.gz` replaced by a FIFO: `verify --all` blocks in `_gunzip_sha256`.

Fix: add one helper and use it wherever a file under the ledger is read.

```python
import stat as _stat          # with the other imports

def _open_regular(path, mode="rb", **kwargs):
    """open() for reading that only ever reads a regular file.

    A FIFO would block, a device such as /dev/zero never ends, a directory cannot be read. All three
    (also behind a symlink) raise OSError. O_NONBLOCK keeps the open itself from waiting on a FIFO."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file: %s" % path)
        return os.fdopen(fd, mode, **kwargs)
    except BaseException:
        os.close(fd)
        raise
```

Call sites:

1. `_read_json`: `with _open_regular(path, "r", encoding="utf-8") as fh:`. The existing `except OSError` returns `None`, so a FIFO state file becomes "unreadable" in `verify`, `append` and `status`.
2. `_anchor_snapshot`: open the history with `_open_regular`. On `OSError`, return `history=None` plus the exception class. `_anchor_checks` then adds the problem `anchors/<bot>.history.jsonl: unreadable (<class>)` instead of letting the exception abort the chain.
3. `orphan_witnesses`: use `_open_regular` in the history loop. The `except OSError: continue` is already there.
4. `_gunzip_sha256`: `with _open_regular(full) as raw, gzip.GzipFile(fileobj=raw, mode="rb") as fh:`. `verify_archives` already turns `OSError` into `<rel>: unreadable (…)`. Change its `os.path.exists(full)` to `os.path.lexists(full)`.
5. `_file_findings`: for `state/<bot>.json`, `anchors/<bot>.json` and `anchors/<bot>.history.jsonl`, add `<rel>: not a regular file` to `problems` when `os.path.lexists(p) and not os.path.isfile(p)`.

Compat: a ledger made only of regular files prints the same bytes.

### D1-02 (High) — A large `seq` gap is materialised as a list

```python
# _describe_join
missing = list(range(seq_before + 1, seq_after)) if seq_after > seq_before else []
```
```python
# _break_shape_ok (called by _parse_ack for every hash-valid break_ack on any chain)
if n["missing_seqs"] != list(range(n["seq_before"] + 1, n["seq_after"])):
    return False
```

`tests/test_fuzz_verify.py` knows about the first one and avoids it ("a huge forged seq is not fuzzed here"). The second is reachable through `Ledger.append` alone, which the threat model grants the adversary.

Reproduction (run under `ulimit -v 2000000`; without a limit the process is killed):

```python
a, b = seed("ops", 2)
raw_line("ops", 10**10, b["hash"])
L.verify("ops")                       # _describe_join -> list(range(3, 10**10))

L.append("ack", {"type": "break_ack", "jay_approval_id": "x", "ts": T % 0, "break": {
    "file": "logs/ack/2024-06-01.jsonl", "line_before": 1, "line_after": 2,
    "seq_before": 1, "seq_after": 10**10, "missing_seqs": [],
    "hash_before": "a" * 64, "orphan_prev": "b" * 64, "hash_after": "c" * 64}})
L.verify("ack")                       # _break_shape_ok -> list(range(2, 10**10))
```

With `MemoryError`, `_verify_guarded` turns it into a FAIL for that chain. With the OOM killer, `verify --all` and `anchor` die and the remaining chains are never reported. A gap of 5·10⁷ already costs about 2 GB.

Fix:

```python
_MAX_MISSING_SEQS = 10000      # a larger gap is described by its size, not by a list

# _break_shape_ok: compare lengths first; the range is then as long as a list that came from one JSON line
gap = n["seq_after"] - n["seq_before"] - 1
if len(n["missing_seqs"]) != gap or n["missing_seqs"] != list(range(n["seq_before"] + 1, n["seq_after"])):
    return False

# _describe_join
gap = seq_after - seq_before - 1
missing = list(range(seq_before + 1, seq_after)) if 0 < gap <= _MAX_MISSING_SEQS else []
...
if gap > _MAX_MISSING_SEQS:
    desc["missing_count"] = gap          # additive key in `breaks` (verify --json, anchors/<bot>.json)
```

Compat: every descriptor with a gap of 10 000 or less is unchanged, including the pinned and the fixture break (gap 1). A descriptor with a larger gap can never equal the pin, so no ack decision changes.

### D1-03 (Medium) — `anchor` is isolated per chain only inside `_verify_guarded`

```python
# Ledger._anchor
for bot in names:
    if not self.log_files(bot) and not os.path.exists(self._state_path(bot)):   # os.listdir can raise
        ...
    res = _verify_guarded(self, bot)
    ...
    self._atomic_json(self._anchor_file(bot), entry)                             # can raise
    self._append_line(self._anchor_history_file(bot), ...)                       # can raise (O_NOFOLLOW, EISDIR)
```

The CHANGELOG says an unexpected error "while one chain is verified or anchored becomes a FAIL entry for that chain". That holds for the verify call only.

Reproduction:

```python
seed("aaa", 1); seed("zzz", 1)
os.makedirs(os.path.join(root, "anchors", "aaa.history.jsonl"))   # a directory (or a planted symlink) at the history path
# $ gblog --root <root> anchor
#   error: Is a directory: <root>/anchors/aaa.history.jsonl      (exit 2)
#   anchors/zzz.json and anchors/days/<today>.json are not written
```

The same happens with `chmod 000 logs/aaa`: `self.log_files(bot)` raises `PermissionError` before the guard is reached.

Fix: move the loop body into `_anchor_one(bot) -> entry` and guard it.

```python
for bot in names:
    try:
        entry = self._anchor_one(bot)
    except Exception as exc:
        if os.environ.get("GBLOG_DEBUG"):
            raise
        entry = {"bot": bot, "ok": False, "status": "FAIL", "records": 0, "head": None,
                 "problems": ["anchor aborted: %s" % exc.__class__.__name__], "break": None,
                 "segments": [], "segment_heads": [], "ts": now_iso()}
    chains.append(entry)
```

The CLI already prints `ANCHOR FAIL … break - heads` for such an entry and exits 1.

### D1-04 (Medium) — An unlistable chain directory is verified as an empty chain

```python
# Ledger._file_findings.entries
try:
    it = os.scandir(d)
except OSError:
    return []
...
except OSError:
    pass
```

`verify` now takes its file list from this scan (W-02). Before W-02, `log_files()` raised and the command failed. Now a `PermissionError`, `EIO` or stale NFS handle on `logs/<bot>` yields "no files".

Reproduction (POSIX, not root):

```python
seed("ops", 2); os.remove(os.path.join(root, "state", "ops.json"))
os.chmod(os.path.join(root, "logs", "ops"), 0)
r = L.verify("ops"); print(r["ok"], r["records"], r["problems"], r["warnings"])   # True 0 [] []
# $ gblog verify --all  ->  "OK    ops    0 records  head genesis", exit 0
```

With a state file present the result is FAIL, but with the wrong text: `head does not match last record (state tampered or log truncated)`.

Fix: only "does not exist" is benign for the chain directory.

```python
def entries(d, must_list=False):
    try:
        it = os.scandir(d)
    except FileNotFoundError:
        return []
    except OSError as exc:
        if must_list:
            problems.append("%s: cannot be listed (%s)" % (rel(d), exc.__class__.__name__))
        return []
    found = []
    try:
        for entry in it:
            ...
    except OSError as exc:
        if must_list:
            problems.append("%s: listing stopped early (%s)" % (rel(d), exc.__class__.__name__))
    finally:
        ...
    return sorted(found)
...
for name, is_link, is_file in entries(bot_dir, must_list=True):
```

The archive directory keeps `must_list=False`, because only symlink warnings come from it.

### D1-05 (Medium) — Day rollup heads are never checked for containment

`_anchor_checks` reads `anchors/<bot>.json` and `anchors/<bot>.history.jsonl`. `orphan_witnesses` reads `anchors/days/*.json`, but only for names whose `logs/<name>` is gone (B:M2). The README promises detection of a truncated or rewritten chain "while a witness survives: `state/<bot>.json`, an anchor under `anchors/`, …", and a day rollup is an anchor under `anchors/`.

Reproduction:

```python
recs = seed("ops", 3); L.anchor()
p = os.path.join(root, "logs", "ops", "2024-06-01.jsonl")
lines = open(p).read().splitlines(); open(p, "w").write("\n".join(lines[:2]) + "\n")
for f in ("state/ops.json", "anchors/ops.json", "anchors/ops.history.jsonl"):
    os.remove(os.path.join(root, f))
r = L.verify("ops"); print(r["ok"], r["problems"])     # True []
# anchors/days/<today>.json still lists ops with records 3 and head recs[2]["hash"]
```

The same holds for an emptied `logs/ops/` directory: `OK ops 0 records head genesis`.

Fix: read the newest rollup entry that names a head for the chain, inside the same lock hold, and check it like `anchors/<bot>.json`.

```python
def _rollup_heads(self, bot):
    """(date, heads, ts) from the newest day rollup that names a head for ``bot``; None when none does."""
    days = os.path.join(self.anchors_dir, "days")
    try:
        names = sorted((n for n in os.listdir(days) if n.endswith(".json") and _DATE_RE.match(n[:-5])),
                       reverse=True)
    except OSError:
        return None
    for name in names:
        data = _read_json(os.path.join(days, name))
        entries = data.get("chains") if data else None
        for c in entries if isinstance(entries, list) else []:
            if not isinstance(c, dict) or c.get("bot") != bot:
                continue
            heads = [c.get("head")] if c.get("ok") else c.get("segment_heads")
            heads = [h for h in heads if isinstance(h, str) and h != GENESIS] if isinstance(heads, list) else []
            if heads:                       # a 0-record or aborted entry never shadows an older one
                return name[:-5], heads, c.get("ts")
    return None
```

- `_anchor_snapshot` returns it as a fourth element. It must stay inside `with self.lock(bot, shared=True)`, for the same reason as R-05: no append can run while the lock is held, so any head a rollup names at read time is in the snapshot.
- `_anchor_checks` adds, after the history loop: `if rollup: check("anchors/days/%s.json" % rollup[0], rollup[1], rollup[2])`. The existing `reported` set keeps one problem line per head.

Cost: one small file per chain in the usual case, and all rollups for a chain that was never anchored. Add a line to `docs/DESIGN.md` §13.

If the code change is not wanted, the minimum is to change the README and `SECURITY.md` wording to "`anchors/<bot>.json` or its history".

### D1-06 (Medium) — A state file that `verify` accepts, `append` refuses and `state rebuild` cannot repair

```python
# Ledger.verify: only the head is looked at
if not problems and state_readable and state_head not in (GENESIS, final_head):
```
```python
# Ledger._append_head: seq and head must both match
if not corrupt and tail["seq"] == seq and tail["hash"] == head:
```
```python
# Ledger.rebuild_state
known = any(rec.get("hash") == old_head and rec.get("seq") == old_seq for _rel, _lineno, rec in items)
if not known or old_seq > tail["seq"]:
    raise StateRebuildError("... is ahead of the log or names a record that is not in it; restore the log from backup")
```

Reproduction:

```python
a, b = seed("ops", 2)
L.write_state("ops", {"seq": 99, "head": b["hash"]})     # right head, wrong seq
r = L.verify("ops"); print(r["ok"], r["warnings"])        # True []
# gblog note --bot ops --detail x    -> exit 4, ALERT tail mismatch
# gblog state rebuild --bot ops      -> exit 2, "... restore the log from backup"   (the log is intact)

L.write_state("ops", {"seq": 2, "head": 7})               # head is not text
# verify: OK + "! state/ops.json missing or unreadable ..."   (docs: such a head "counts as unreadable")
# note: exit 4.   state rebuild: the same refusal as above.
```

DESIGN §4 says an unreadable state follows "exactly the same rule as a missing one", and README says `state rebuild` works when state is unreadable. Neither holds for an object with a non-string head. No documented command repairs either case.

Fix, in three parts:

1. `verify`, after the state warnings. It must be a warning, not a problem, because `tests/test_fuzz_verify.py` asserts `ok` for a wrong `seq` with the right head.

```python
if (not problems and state_readable and segments and state_head == final_head
        and _state_head(raw_state)[0] != segments[-1]["seq_end"]):
    warnings.append("state/%s.json: seq %s does not match the last record (seq %s); appends are refused "
                    "until `gblog state rebuild`" % (bot, raw_state.get("seq"), segments[-1]["seq_end"]))
```

2. `_append_head`, right after `seen.append(state)`, so `append` still merges into the original object and keeps `last_sync` and the capture markers:

```python
if state is not None and not isinstance(state.get("head", GENESIS), str):
    state = None          # same rule as verify: a head that is not text is an unreadable state
```

   The existing branch then adopts a self-consistent tail with the `unreadable` notice and the `state_adopted` marker.

3. `rebuild_state`: decide by hash, and treat a non-string head as unreadable.

```python
old_seq, old_head = _state_head(old)
usable = old is not None and isinstance(old_head, str)
if usable and old_seq == tail["seq"] and old_head == tail["hash"]:
    return {...noop...}
if not usable:
    was = "seq - head %s" % ("unreadable" if exists else "missing")
else:
    was = "seq %s head %s" % (old.get("seq"), str(old_head)[:12])
    if old_head != GENESIS and not any(rec.get("hash") == old_head for _rel, _lineno, rec in items):
        raise StateRebuildError(...)          # unchanged text: the head is not in the log
```

   A head that is in the log is at or before the tail by construction, so the `seq` conditions add nothing.

### D1-07 (Low-Medium) — `verify --bot <Bot>` skips a deleted variant chain

```python
# _main, verify branch
bots = ledger.chains_on_disk(args.bot)
exact = args.bot.strip()
lost = ledger.missing_witnesses(exact) if exact not in bots else []
```

`chains_for` returns only chains with a `logs/` directory when at least one exists. A variant whose logs are gone is then dropped. Only the literal `--bot` text gets the missing-witness check.

Reproduction:

```python
seed("chief", 2); seed("cos", 3); L.add_alias("chief", "Chief", ["cos"])
shutil.rmtree(os.path.join(root, "logs", "cos"))          # state/cos.json survives
# gblog verify --bot chief   -> "OK    chief   2 records", exit 0
# gblog verify --bot Chief   -> the same
# gblog verify --bot cos     -> FAIL cos ... state/cos.json records seq 3 ... but logs/cos is missing
```

The skill tells a Bot that `gblog verify --bot "$BOT"` "checks every chain of that Bot". `verify --all` does catch this.

Fix: check every name of the Bot.

```python
exact = args.bot.strip()
slug = ledger.canonical(args.bot)
variants = ledger.aliases()["bots"].get(slug, {}).get("aliases", [])
lost_by_name = {}
for name in dict.fromkeys([exact, slug] + [a for a in variants if isinstance(a, str)]):
    if name not in bots:
        lines = ledger.missing_witnesses(name)
        if lines:
            lost_by_name[name] = lines
if not bots:
    entry = _verify_missing(args.bot, "no chain on disk for '%s'" % args.bot)
    for lines in lost_by_name.values():
        entry["problems"].extend(lines)
    emit(args.bot, entry)
    return finish(1)
missing.update(lost_by_name)
```

### D1-08 (Low-Medium) — Records are not bound to their day file

`verify` checks file names against `YYYY-MM-DD.jsonl`, but not that a record's `ts` belongs in the file it is in. `records(date=…)` and `chain_records(since=…)` select by file name.

Reproduction:

```python
seed("ops", 2)
d = os.path.join(root, "logs", "ops")
os.rename(os.path.join(d, "2024-06-01.jsonl"), os.path.join(d, "2024-05-20.jsonl"))
r = L.verify("ops", strict=True); print(r["ok"], r["warnings"])     # True []
print(len(L.records("ops", date="2024-06-01")))                      # 0
```

A rename that keeps the file order (into a date with no file of its own) therefore hides a day from `report --date` and from the `status` window, and `verify --strict` stays silent.

Every version of `append` names the file `ts[:10] + ".jsonl"`, also for clamped and replayed records. A ledger written by gblog therefore never trips this check.

Fix, in `_layout_findings` (warning by default, problem with `--strict`, capped like the others):

```python
day = rel.rsplit("/", 1)[-1]
ts_value = rec.get("ts")
if isinstance(ts_value, str) and _DAY_FILE_RE.match(day) and ts_value[:10] != day[:10]:
    misfiled += 1
    if misfiled <= _MAX_LAYOUT_FINDINGS:
        warnings.append("%s:%d: record dated %s is stored in day file %s" % (rel, lineno, ts_value[:10], day[:10]))
...
if misfiled > _MAX_LAYOUT_FINDINGS:
    warnings.append("%d more record(s) stored in a day file of another date" % (misfiled - _MAX_LAYOUT_FINDINGS))
```

### D1-09 (Low) — After a legacy tail, `append` can write a record that fails `verify` forever

```python
# Ledger.append
tail_ts = tail.get("ts") if tail else None
if not _is_norm_ts(tail_ts):
    tail_ts = None                      # no clamp, no bound
```
```python
# Ledger.verify
if ts < last_ts:                        # text comparison
    problems.append("%s: timestamp goes backwards" % where)
```

Before 0.6, `--ts` was stored as given. Two failure shapes follow when such a record is the tail:

- Tail `2026-11-16T18:30:00+09:00` (09:30Z). A record at 10:00Z compares lower as text, so `verify` reports `timestamp goes backwards`.
- Tail `2026-11-17T03:00:00+09:00`, stored in `2026-11-17.jsonl`. A record at `2026-11-16T18:30:00Z` goes to `2026-11-16.jsonl`, which sorts before the tail's file. The walk order breaks, and the next append exits 4.

Reproduction (first shape):

```python
from unittest import mock
os.makedirs(os.path.join(root, "logs", "old"))
rec = {"v": 1, "seq": 1, "ts": "2026-11-16T18:30:00+09:00", "bot": "old", "type": "note", "prev": "genesis"}
rec["hash"] = gblog.compute_hash(rec)
open(os.path.join(root, "logs", "old", "2026-11-16.jsonl"), "w").write(json.dumps(rec, sort_keys=True) + "\n")
L.write_state("old", {"seq": 1, "head": rec["hash"]})
with mock.patch.object(gblog, "now_iso", return_value="2026-11-16T10:00:00Z"):
    L.append("old", {"type": "note", "detail": "30 minutes later"})          # succeeds
print(L.verify("old")["problems"])     # ['logs/old/2026-11-16.jsonl:2: timestamp goes backwards']
```

Fix:

1. `verify`: compare by instant only where text order says "backwards". This can only remove a FAIL, and it is identical for normalised values.

```python
def _ts_backwards(ts, last_ts):
    if not ts < last_ts:
        return False
    try:
        return parse_ts(ts) < parse_ts(last_ts)
    except ValueError:
        return True
```

2. `append`: bound the new record by the parsed tail and by the day file that holds the tail. `_append_head` already knows that path and can hand it back through `seen`.

```python
try:
    floor = parse_ts(tail.get("ts") if tail else None)
except ValueError:
    floor = None
tail_day = os.path.basename(tail_file)[:10] if tail_file else ""
if _DATE_RE.match(tail_day) and (floor is None or floor[:10] < tail_day):
    floor = tail_day + "T00:00:00Z"      # never file a record before the day file that holds the tail
```

   Then use `floor` where `tail_ts` is used today (explicit `--ts` check, clamp, future warning). For a normalised tail in its own day file, `floor == tail_ts`, so nothing changes.

### D1-10 (Low) — `anchor` still overwrites the latest anchor with a 0-record entry

The A:H-1 guard sits only in the "no log file and no state file" branch. Every other FAIL path ends in:

```python
self._atomic_json(self._anchor_file(bot), entry)
```

Reproduction:

```python
recs = seed("ops", 3); L.anchor()
open(os.path.join(root, "logs", "ops", "2024-06-01.jsonl"), "w").close()     # emptied; state survives
L.anchor()
print(json.load(open(os.path.join(root, "anchors", "ops.json")))["records"])  # 0
```

The same happens when `_verify_guarded` returns an aborted result. The history line still holds the old head, so detection survives. But `anchored_missing()`, which reads `anchors/<bot>.json` only, has lost its witness, and today's rollup entry is replaced as well.

Fix: keep the previous anchor when the new entry names no head.

```python
prev_n = prev.get("records") if prev else None
keep = (not entry["ok"] and not heads
        and isinstance(prev_n, int) and not isinstance(prev_n, bool) and prev_n > 0)
if not keep:
    self._atomic_json(self._anchor_file(bot), entry)
```

The entry still goes into `chains`, so the output and exit code are unchanged.

### D1-11 (Low) — `seal` does not preflight its own chain, and an ack without a witness is final

```python
# Ledger.seal_break
if bot != "ledger" and not self._tail_ok("ledger"):
    raise SealError("refusing: ledger witness chain has a tail mismatch")
...
ack = self.append(bot, {... "type": "break_ack" ...}, _guard=_no_second_ack)   # the sealed chain is not preflighted
if bot != "ledger":
    self.append("ledger", {...witness...})
```

Reproduction on a copy of the fixture:

```python
fx = "tests/fixtures/kim-biseo-break"
for n in ("logs", "state"):
    shutil.copytree(os.path.join(fx, n), os.path.join(root, n), dirs_exist_ok=True)
desc = L.verify("kim-biseo")["breaks"][0]
L.write_state("kim-biseo", {"seq": 5000, "head": "cd" * 32})
try:
    L.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
except gblog.TailMismatchError as exc:
    print(exc.spool_path)                                  # a spooled break_ack
print(L.spool_replay("kim-biseo")["rejected"])             # the same file, forever
```

`spool replay` never appends a `break_ack`, so that spool file keeps the alert open until someone moves it by hand. The CLI exits 4 here, not 2.

Second gap: if the witness append fails after the ack was written, no command can add the witness. A rerun stops at `a break_ack is already present`, and the checklist forbids a hand-written note.

Fix:

```python
if not self._tail_ok(bot):
    raise SealError("refusing: %s has a tail mismatch; nothing was written" % bot)
```

For the witness, at the "already present" check: when the one ack on disk carries this descriptor and this `jay_approval_id`, and `self._witness_present(desc, jay) is False`, append only the witness note and return the existing ack. Any other existing ack still refuses.

### D1-12 (Low) — `_corrupt` is an in-band sentinel that `append` will store

`_parse_line` marks an unreadable line as `{"_corrupt": True}`. About a dozen call sites test `"_corrupt" in rec`. `append` copies caller keys except `v`, `seq`, `ts`, `bot`, `prev` and `hash`.

Reproduction:

```python
L.append("ops", {"type": "note", "detail": "x", "_corrupt": False})
print(L.verify("ops")["problems"])       # ['logs/ops/<today>.jsonl:1: not valid JSON']
L.append("ops", {"type": "note"})        # TailMismatchError; `state rebuild` refuses as well
```

This is reachable through the library and through a spool file's `record`, not through CLI flags.

Fix: add `"_corrupt"` to the ignored-key tuple in `append`, and to the same tuple in `_refuse_tail`, so it is never spooled either. This matches the documented rule that reserved keys supplied by the caller are ignored.

### D1-13 (Low) — "N archived file(s) intact" counts records that name no file

```python
arch_recs = [rec for _rel, _lineno, rec in items if rec.get("type") == "archive"]
archives = len(arch_recs)
```

Reproduction: run `gblog log --bot ops --type archive --target claim`, then `gblog verify --bot ops`. It prints:

```
OK    ops                       1 records  head …  (1 archived file(s) intact)
      ! logs/ops/<day>.jsonl:1: archive record without path and sha256: no archived file backs it
```

Fix: `archives = sum(1 for rec in arch_recs if rec.get("path") or rec.get("sha256"))`. Records that share one stored file keep counting as before, so only ledgers that already gained the new `!` line change. Update the `archives` row of the `verify --json` table in DESIGN §7.

### D1-14 (Docs) — Statements about this area that the code no longer matches

| File | Statement | Code |
|---|---|---|
| `CHANGELOG.md` "Known open findings"; DESIGN §7 | "`verify` does not check that `ts` is written in the normalised form, in any mode" | `_layout_findings` reports `ts is not in normalised form` under `--strict`. It is absent from the `--strict` rows in README, DESIGN §7 and FAQ 5, and I found no test for it. |
| `CHANGELOG.md` "Known open findings" | `anchor` prints missing-chain lines without neutralising control characters; `_exit2`, `_assert_tail_matches`, `_BREAK_KEYS`, `_Lock._held` still present; version prints 0.6.0 | All done in code (`_term`, names removed, `__version__ = "0.6.1"`). The other items in that list are implemented too. `SECURITY.md` "Supported versions" repeats the 0.6.0 claim. |
| DESIGN §5 rule 3 | `_Lock` "still carries a per-thread table of exclusive holds (`_held`)" | `_Lock` keeps no such table. |
| DESIGN §4 table and the text below it; §6 state keys; CHANGELOG; README "Lost state file"; `SECURITY.md` | The state-level `state_adopted` key "is kept by every later `append` and by `state rebuild`" | The next `append` moves it onto its record and drops the key (W-15). The same §4 also describes the move, so the section contradicts itself. The value `unknown` is undocumented. |
| `docs/routine-daily-audit.md` "Why"; `docs/agent-integration.md` §9 and §11.2; `examples/routine-daily-audit.sh` | "`gblog anchor` then verifies every chain again, archives included … two full passes" | The prompt runs `gblog anchor --skip-archives`. Two bullets in the same "Why" list contradict each other. |
| README, FAQ 5 | `--strict` fails on "any line that is not byte-for-byte the canonical serialisation" | The comparison uses the stripped line. Leading or trailing blanks, CRLF and blank lines pass. |
| DESIGN §9 | An eligible ack must "sit after the join, in the same file" | The code requires the two records of the join to share a file. The ack may be in any later file, as the live seal's will be. |
| DESIGN §13 | The last three bullets ("No FAIL for an `archive` record…", "No command that dismisses…", "No read-only open…") | They belong to §12. The sentence above them says "Two costs". |
| DESIGN §7 `verify --json` table | Key list | `aborted` (set by `_verify_guarded`) is missing. Add `missing_count` if D1-02 lands. |

### D1-15 (Cleanup) — Code that can go without changing behaviour

| Item | Evidence | Action |
|---|---|---|
| `class _ChainLines`, `Ledger._chain_lines` (about 75 lines) | No caller. `verify` uses `_chain_snapshot`. This is the unused half of W-14 and repeats `_parse_line_dups`. | Delete. DESIGN §13 already describes parsing under the lock. |
| `Ledger.chain_hashes` | No caller in `gblog.py` or the tests | Delete. |
| `_VerifyResult.hashes`, `res.hashes = hashes`, the `getattr(res, "hashes", ())` fallback | `--contains` reads `own_hashes` only | Keep `own_hashes`, drop `hashes`. |
| `try: seq_i = int(rec["seq"]) except …` in the walk | Unreachable after `_fields_ok(rec)` | `seq_i = rec["seq"]`. |
| `try/except` around `int()` in `_describe_join` | Both records passed `_fields_ok` | Use the values directly. |
| "broken chain" / "seq …, expected …" messages | Built twice in the walk (`i == 0` and the general case) | One local helper. |
| `_anchor_checks(..., snapshot=None)` fallback | Every caller passes the snapshot | Make it required. |
| `Ledger._chain_items` | One caller (`rebuild_state`) | `[(r, n, rec) for r, n, rec, _ in self._chain_snapshot(bot, keep_text=False)[0]]`. |
| Merge of `anchored_missing()` and `orphan_witnesses()` | Done by hand in the verify CLI branch, in `_anchor` and in `missing_witnesses`; the "chain was anchored on … but logs/… is missing" text exists three times | One `Ledger.missing_chains() -> {name: [lines]}`. D1-07 then needs one scan, not one per name. |
| `os.path.lexists(self._state_path(bot))` in the state warning | A stat after the lock is released, although the snapshot was just taken | Record "state file exists" in the snapshot. |

## 3. Checked and found sound

- The ack rules are enforced as documented: single ack, placed after the join, both join records in one file, descriptor equal to the on-disk join, ack inside a verified segment. The pin and the fixture are compared with full hashes after `_norm_break`.
- An unverified record cannot satisfy anchor containment or `--contains`. Any line that fails the walk makes the chain FAIL, and `own_hashes` requires `bot ==` chain directory.
- Reading `anchors/<bot>.json` and the history under the same shared lock as the log lines closes the R-05 race. The argument also covers the rollup read proposed in D1-05.
- `_last_line` and the walk split on LF and strip the same bytes, so append and verify agree on what the tail is.
- `rebuild_state` re-checks the tail under the exclusive lock against the head it verified.
- The roll-forward rule adopts exactly one self-consistent line whose `prev` is the state head.

## 4. Test cases to add

`tests/test_integrity.py`

- `test_fifo_side_files_do_not_hang_verify_all` (D1-01): FIFOs at `state/ghost.json`, `anchors/days/2020-01-01.json` and `anchors/ops.history.jsonl`. Run the CLI in a subprocess with a 30 s timeout. Expect exit 1, `FAIL ops` with `not a regular file` or `unreadable (`, and `OK zzz`. Skip without `os.mkfifo`.
- `test_symlink_to_device_is_not_read` (D1-01): `state/ghost.json -> /dev/zero`; `verify --all` returns within the timeout.
- `test_fifo_archive_is_a_problem_not_a_hang` (D1-01).
- `test_large_seq_gap_is_described_by_count` (D1-02): raw line with `seq = 3 + 5_000_000`; `breaks[0]["missing_seqs"] == []`, `missing_count` set, and `len(json.dumps(res["breaks"])) < 2000`.
- `test_break_ack_with_huge_gap_is_malformed_without_allocation` (D1-02): `_break_shape_ok` with `seq_after = 10**15` returns `False`; `verify` reports `malformed break_ack`.
- `test_anchor_write_failure_in_one_chain_does_not_stop_the_others` (D1-03): a directory at `anchors/aaa.history.jsonl`. Expect exit 1, `anchors/zzz.json` written, and a rollup listing both chains.
- `test_unreadable_chain_directory_fails_verify` (D1-04): `chmod 0`, no state file. Expect `ok` false and `logs/ops: cannot be listed (PermissionError)`. Skip as root; restore the mode in cleanup.
- `test_scandir_error_mid_listing_is_a_problem` (D1-04): mock `os.scandir` with an iterator that raises `OSError`.
- `test_truncate_after_anchor_detected_from_day_rollup_only` and `test_emptied_chain_dir_with_only_day_rollup_fails` (D1-05).
- `test_rollup_entry_without_heads_does_not_shadow_older_rollups` (D1-05).
- `test_rollup_written_after_snapshot_does_not_fail_verify` (D1-05): the `_AfterRelease` hook of the R-05 test with `anchors/<bot>.json` and the history removed.
- `test_renamed_day_file_warns_and_strict_fails` (D1-08): exact warning text, `ok` true by default, FAIL with `--strict`.
- `test_clamped_and_replayed_records_are_not_misfiled` (D1-08): after a clock clamp and a spool replay there is no such warning. Also assert the committed fixture has none.
- `test_strict_flags_non_normalised_ts` and `test_non_normalised_ts_is_silent_by_default` (D1-14): the existing strict check has no test.
- `test_anchor_does_not_replace_anchor_with_zero_record_entry` (D1-10): one case with an emptied day file, one with `Ledger.verify` mocked to raise.
- `test_seal_refuses_on_own_tail_mismatch_without_spooling` (D1-11): `SealError`, no spool file, no alert file.
- `test_seal_rerun_adds_missing_witness_only` (D1-11): the first witness append is mocked to raise `OSError`; the rerun adds one note and no second ack.
- `test_bare_archive_claim_is_not_counted_as_intact` (D1-13): no `(1 archived file(s) intact)` suffix. Two records sharing one stored file still give `archives == 2`.

`tests/test_append_safety.py`

- `test_fifo_state_file_does_not_hang_append` (D1-01): subprocess with timeout. Expect exit 0, the `unreadable` adoption notice, and a regular state file afterwards.
- `test_state_seq_mismatch_warns_and_rebuild_repairs` (D1-06): right head, `seq` 99. Expect the exact warning, `note` exit 4, `state rebuild` exit 0, then `note` exit 0.
- `test_non_string_state_head_is_adopted_like_unreadable` (D1-06): `note` exits 0 with the notice and `last_sync` is preserved. `state rebuild` records `was seq - head unreadable`.
- `test_legacy_offset_tail_append_still_verifies` and `test_legacy_tail_near_midnight_keeps_file_order` (D1-09).
- `test_backdated_normalised_ts_still_fails` (D1-09): the semantic comparison does not relax normalised values.
- `test_append_ignores_corrupt_sentinel_key` (D1-12): the stored record has no `_corrupt` key, and the chain verifies and accepts the next append. Add one case through `spool_replay`.

`tests/test_cli_ux.py`

- `test_verify_bot_reports_deleted_variant_chain` (D1-07): by slug and by display name. Expect exit 1 and a `FAIL cos` block. With `--json`, expect an entry with `missing: true`.
- `test_verify_display_name_checks_all_variant_chains` must pass unchanged.

`tests/test_fuzz_verify.py`

- Add a `huge_seq` mutator (hash-valid line, `seq` up to 10¹²) once D1-02 lands, and remove the comment that excludes it.
- Add FIFO or directory variants of `state/<bot>.json` to `_m_state_delete` once D1-01 lands, for the CLI sample only.

`tests/test_meta.py`

- `test_no_unreferenced_private_names` (D1-15): with `ast`, every module-level `_name` and every `Ledger._name` in `gblog.py` is referenced in `gblog.py` or under `tests/`. Keep an allow-list for `_tail_head` and `_GITHUB_REMOTE_RE`.