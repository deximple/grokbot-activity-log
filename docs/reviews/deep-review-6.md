# Deep review 6 — archive, report rendering, status, tail, export, alias registry

Scope: `gblog.py` 0.6.1 as supplied (`archive_file`, `verify_archives`, `render_report`, the `report` / `status` / `tail` / `export` / `archive` / `alias` branches of `_main`, the `Ledger` readers and the alias registry), checked against README, `SECURITY.md`, `docs/DESIGN.md`, `docs/agent-integration.md` and `docs/faq.md`.

Method: static read only. Nothing was executed, so every reproduction is traced through the code, not observed. All reproductions use a temporary root. No code was edited; the fixes are given as snippets.

## Summary

| ID | Sev | Area | Finding |
|---|---|---|---|
| D6-01 | High | archive | `append` redacts the generated `path` of an archive record. For a Bot named like a key prefix (`xai-…`, `sk-…`) every archive makes the chain FAIL permanently |
| D6-02 | High | archive / status / alias | A FIFO at an archive address, `state/<bot>.json` or `state/aliases.json` blocks `verify --all`, `anchor`, `status` and appends |
| D6-03 | Medium | readers | A hash-valid record with a non-string `bot` is dropped by `report`, `status`, `tail` and `export` while `verify` says OK |
| D6-04 | Medium | alias | `resolve()` is not idempotent: a name that sanitises to a registered slug is written to a new variant chain that readers then merge |
| D6-05 | Medium | alias | `alias add --slug X` is accepted when X shadows another Bot's display name; that Bot's writes are silently diverted |
| D6-06 | Low-Med | alias | `chains_for()` and `alias list` disagree with `resolve()`; `alias list` prints unregistered chains in random order |
| D6-07 | Low-Med | report | Housekeeping records are labelled by `action` (`other`, `file`), not by type; `tail` does it right |
| D6-08 | Low-Med | report | The `detail` of `task-start` / `task-end` (the one-line result) never appears in a report |
| D6-09 | Low-Med | report | The "likely closed as" hint fires inside one chain and across different titles |
| D6-10 | Low | report / status | `(no task)` bucket adopts a title and an outcome; `status` counts an id-less `task_start` as open |
| D6-11 | Low-Med | archive | `(N archived file(s) intact)` counts records that name no file |
| D6-12 | Low | archive | A refused reserved name leaves `archive/<name>/<day>/` behind and creates the root |
| D6-13 | Low | archive CLI | `--name` with `/` is cut; `--task` is dropped silently; `--file` plus `--stdin`; empty stdin |
| D6-14 | Low | status | Spooled events without an alert file print no `ALERT` line |
| D6-15 | Low | report CLI | `--date` is ignored with `--since`; the report file is truncated in place |
| D6-16 | Low | docs | Documentation drift (table) |
| D6-17 | — | simplification | Code that can be removed without changing behaviour |

## Findings

### D6-01 (High) — `append` redacts the archive `path` it was given

`Ledger.append` passes every top-level string through `redact()`, including the content address that `archive_file` has just computed:

```python
# archive_file
rel = os.path.join("archive", bot, day, digest + ".gz")
rec = ledger.append(bot, {"type": "archive", ..., "sha256": digest, ..., "path": rel_posix, ...})
# Ledger.append (the same loop is in _refuse_tail)
full[key] = redact(value) if isinstance(value, str) else value
```

The path contains the Bot name. Several vendor rules match a plain name:

- `\bxai-[A-Za-z0-9_-]{16,}\b` matches `xai-research-assistant`.
- `\bsk-[A-Za-z0-9_-]{16,}\b` matches `sk-customer-support-bot` and `team-sk-customer-support`.
- `\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}` matches `sk_test_environment`.

The file is stored under the real name, but the record points at `archive/[REDACTED]/…`. `verify` then reports `archived file missing`. The record cannot be corrected, so the chain fails forever.

Reproduction:

```python
import os, tempfile, gblog
root = tempfile.mkdtemp(prefix="gblog-d6-")
L = gblog.Ledger(root)
rec = gblog.archive_file(L, "xai-research-assistant", b"evidence\n", "e.txt", ts="2024-06-01T10:00:00Z")
print(rec["path"])                                  # archive/[REDACTED]/2024-06-01/<sha>.gz
print(os.listdir(os.path.join(root, "archive")))    # ['xai-research-assistant']
print(L.verify("xai-research-assistant")["problems"])
# ['archive/[REDACTED]/2024-06-01/<sha>.gz: archived file missing']
```

The CLI gives the same result: `printf 'x\n' | gblog archive --bot xai-research-assistant --stdin`, then `gblog verify --all` exits 1.

Fix, step 1: never redact a value that is a well-formed archive address. Use one helper in both loops (`append` and `_refuse_tail`):

```python
_ARCHIVE_PATH_RE = re.compile(r"archive/[A-Za-z0-9._-]+/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9a-f]{64}\.gz\Z")

def _stored_value(record: Dict, key: str, value):
    """What append stores for one field: strings are redacted, except the address of an archive record."""
    if not isinstance(value, str):
        return value
    if record.get("type") == "archive" and (
            (key == "path" and _ARCHIVE_PATH_RE.match(value)) or (key == "sha256" and _is_full_hash(value))):
        return value
    return redact(value)
```

- In `append`: `full[key] = _stored_value(record, key, value)`.
- In `_refuse_tail`: `safe_record[key] = _stored_value(record, key, value)`.
- Record bytes of existing ledgers do not change.

Fix, step 2 (heals ledgers already hit; no record is rewritten): in `verify_archives`, before the "missing" check, map a redacted Bot component back to the record's own chain:

```python
parts = norm.split("/")
if not os.path.exists(full) and len(parts) == 4 and parts[1] == "[REDACTED]":
    owner = r.get("bot")
    if isinstance(owner, str) and _chain_key(owner) == owner and redact("archive/%s/x" % owner) == "archive/[REDACTED]/x":
        full = os.path.join(ledger.root, "archive", owner, parts[2], parts[3])
```

### D6-02 (High) — a FIFO at a path gblog reads blocks the command

`SECURITY.md` counts "a single file … that stops `verify --all` or `anchor`" as a vulnerability. The round-3 fix covered `*.jsonl` entries only. Two readers in this area still open whatever is at the path:

```python
# verify_archives
if not os.path.exists(full): ...
found = _gunzip_sha256(full, limit)        # gzip.open() -> open() blocks on a FIFO
# _read_json  (state views in status, alert files, state/aliases.json, anchors)
with open(path, encoding="utf-8") as fh:   # blocks on a FIFO
```

Reproduction (POSIX; today each call ends in `TimeoutExpired`):

```python
rec = gblog.archive_file(L, "ops", b"evidence\n", "e.txt", ts="2024-06-01T10:00:00Z")
full = os.path.join(root, rec["path"]); os.remove(full); os.mkfifo(full)
subprocess.run([sys.executable, "gblog.py", "--root", root, "verify", "--all"], timeout=10)
```

- `anchor` hangs the same way while it holds `state/.anchors.lock`.
- `os.mkfifo(state/ops.json)` blocks `status`, and `verify --bot ops` under the shared lock.
- The same FIFO blocks `note --bot ops` inside `_append_head` while it holds the exclusive lock.
- `os.mkfifo(state/aliases.json)` blocks every command that resolves a name.

Fix:

```python
def _read_json(path):
    try:
        if not os.path.isfile(path):   # a FIFO, directory or device is never opened
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    ...
# verify_archives, after the "archived file missing" check
if not os.path.isfile(full):
    problems.append("%s: not a regular file" % rel)
    continue
```

Outside this review's area, the same guard is needed for the bare `open()` calls on `anchors/<bot>.history.jsonl` (`_anchor_snapshot`, `orphan_witnesses`).

### D6-03 (Medium) — a record with a non-string `bot` disappears from every reader

`_readable`, `tail_records`, the later-file branch of `records()` and `tune_history` all skip such a record:

```python
if "_corrupt" in rec or not isinstance(rec.get("ts", ""), str) or not isinstance(rec.get("bot", ""), str):
    skipped += 1
    continue
```

`verify` does not check the type of `bot`; it only warns about a mismatch. R-04 made attribution depend on the directory, so the stored `bot` no longer matters to readers.

Reproduction:

```python
a = L.append("ops", {"type": "note", "detail": "one", "ts": "2024-06-01T10:00:00Z"})
rec = {"v": 1, "seq": 2, "ts": "2024-06-01T10:00:01Z", "bot": ["ops"], "type": "action",
       "action": "shell", "target": "rm -rf /backup", "outcome": "ok", "prev": a["hash"]}
rec["hash"] = gblog.compute_hash(rec)
open(L.log_files("ops")[0], "a").write(json.dumps(rec, sort_keys=True) + "\n")
L.write_state("ops", {"seq": 2, "head": rec["hash"], "updated": rec["ts"]})
```

- `gblog verify --bot ops` exits 0 with one `!` line (`bot field '['ops']' does not match chain 'ops'`).
- `gblog report --date 2024-06-01 --stdout` shows `| Records | 1 |` and no `rm -rf`.
- `tail` and `export` print one record; `status` counts one.
- Readers print "skipped 1 unreadable line(s) … run gblog verify", and `verify` says OK.

Fix: drop the `bot` test in the four places. A ready predicate:

```python
def _reader_skips(rec: Dict) -> bool:
    """A line no reader can use: not a JSON object, or a ts that cannot be compared."""
    return "_corrupt" in rec or not isinstance(rec.get("ts", ""), str)
```

- The existing attribution (`dict(rec, bot=chain, bot_field=rec.get("bot"))`) handles the rest.
- `export` (`raw=True`) prints the record as stored; its sort key already uses `str(r.get("bot") or "")`.

### D6-04 (Medium) — `resolve()` is not idempotent

```python
cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
...
return cleaned, not data["bots"]
```

The sanitised form is never compared with the registered slugs. The chain directory it produces is later resolved to a slug by the case-insensitive pass.

Reproduction:

```bash
export GBLOG_ROOT="$(mktemp -d)"
gblog alias add --slug ops-bot --display "Ops Bot"
gblog note --bot "OPS BOT" --detail x
# warning: bot name 'OPS BOT' is not registered ... (logging under 'OPS-BOT')
ls "$GBLOG_ROOT/logs"       # OPS-BOT   (a new variant chain)
gblog status                # one row "Ops Bot(ops-bot)": readers merge it
```

Under `alias strict on` the same name is refused, although its sanitised form is a registered slug.

Fix, before the final `return` of `resolve()`:

```python
for slug in bots:               # the sanitised form is the chain key: a registered slug wins, as it does for readers
    if cleaned.lower() == slug:
        return slug, True
```

### D6-05 (Medium) — a new slug may shadow another Bot's display name

`_add_alias` refuses a display name that equals another slug. It does not refuse the reverse. The slug passes of `resolve()` run before the display pass.

Reproduction:

```bash
gblog alias add --slug chief --display Ops
gblog note --bot Ops --detail one     # chain chief
gblog alias add --slug ops            # accepted, no message
gblog note --bot Ops --detail two     # chain ops, no warning
gblog alias list                      # no "conflicts:" section
```

Fix, inside the first `for other, e in data["bots"].items()` loop of `_add_alias`:

```python
if slug not in data["bots"] and (e.get("display") or "").lower() == slug:
    _die("'%s' is the display name of %s; a slug with that name would shadow it" % (slug, other))
```

Add the same pair to `alias_conflicts()` for registries that already contain it:

```python
if other != slug and (bots[other].get("display") or "").lower() == slug:
    problems.append("slug '%s' shadows the display name of %s; '%s' resolves to the slug" % (slug, other, bots[other]["display"]))
```

### D6-06 (Low-Med) — `chains_for()` and `alias list` disagree with `resolve()`

```python
names = {slug} | set(self.aliases()["bots"].get(slug, {}).get("aliases", []))
existing = [c for c in self.valid_chains() if c in names or self.canonical(c) == slug]
```

- **`c in names` ignores the rule "the slug wins".** Use the registry of `test_slug_beats_alias_regardless_of_order` (`holder` lists alias `mmm`; `mmm` is a slug). `gblog report --bot holder --stdout` then prints a `## Shared Name(mmm)` section. `tail --bot holder` and `export --bot holder` return `mmm`'s records. `status --bot holder` prints nothing.
- **`alias list` builds "chains on disk" from `[slug] + aliases` only.** After `gblog note --bot Ops` (empty registry) and `gblog alias add --slug ops`, chain `Ops` is merged by every reader. `alias list` shows it neither under `ops` nor under "unregistered chains".
- **`unmapped` is built by iterating a `set`.** The order of "unregistered chains: …" changes between runs (hash randomisation).

Fix:

```python
def chains_for(self, bot):
    slug = self.canonical(bot)
    existing = [c for c in self.valid_chains() if self.canonical(c) == slug]
    return existing or [c for c in (_chain_key(slug),) if c]
```

```python
# alias list
owner = {c: ledger.canonical(c) for c in chains}
for slug, e in sorted(data["bots"].items()):
    listed = [c for c in [slug] + e.get("aliases", []) if owner.get(c) == slug]
    on_disk = listed + sorted(c for c in chains if owner[c] == slug and c not in listed)
    ...
unmapped = sorted(c for c in chains if owner[c] not in data["bots"])
```

Output is unchanged for registries without conflicts or case variants.

### D6-07 (Low-Med) — the report labels housekeeping records by `action`

```python
kind = r.get("action") or r.get("type")
```

`verify`, `sync`, `tune` and `break_ack` records carry `action: other`; `archive` carries `action: file`.

Reproduction: `gblog verify --all --record; gblog sync --no-push; gblog report --stdout`. The lines read `` `other` chain — 2 records verified`` and `` `other` git **PENDING** — …``. An archive reads `` `file` e.txt``. A seal reads `` `other` logs/kim-biseo/…:165``. `tail` prints the type for the same records.

Fix:

```python
kind = (r.get("action") if r.get("type") == "action" else None) or r.get("type") or r.get("action")
```

- Lines of `action`, `approval` and `note` records written by their own commands are unchanged.
- A `gblog log --type note` record changes from `other` to `note`.

### D6-08 (Low-Med) — `task-start` / `task-end` detail is never rendered

```python
if r.get("type") == "task_start":
    t["start"] = r.get("ts")
elif r.get("type") == "task_end":
    t["end"] = r.get("ts"); t["outcome"] = r.get("outcome"); t["end_chain"] = ...
else:
    t["actions"].append(r)
```

The skill tells Bots to put the result in `task-end --detail`. The report shows only start, end and outcome.

Reproduction: `gblog task-end --bot ops --task-id "$TID" --summary "3 invoices reconciled"`, then `gblog report --stdout | grep reconciled` prints nothing. `tail` and `export` show the text.

Fix (additive; a task without such text renders as before): after the `if/elif`, replace the `else` branch with

```python
if r.get("type") not in ("task_start", "task_end") or r.get("detail"):
    t["actions"].append(r)
```

With D6-07 the line reads `` - 09:30:00 `task_end` — 3 invoices reconciled``.

### D6-09 (Low-Med) — the "likely closed as" hint is broader than documented

README: an open task is flagged when its twin "(same title) was closed in another chain of the same Bot". The code accepts any later closed task whose `_norm_title` matches. `_norm_title` removes all digits and punctuation, and the chain is not compared.

Reproduction (one chain):

```python
L.append("ops", {"type": "task_start", "task_id": "t5", "task": "Reconcile invoices 5", "ts": "2024-06-01T09:00:00Z"})
L.append("ops", {"type": "task_start", "task_id": "t6", "task": "Reconcile invoices 6", "ts": "2024-06-01T10:00:00Z"})
L.append("ops", {"type": "task_end", "task_id": "t6", "outcome": "ok", "ts": "2024-06-01T10:30:00Z"})
```

The report says t5 is "**open** — likely closed as `t6` in chain `ops` (ok)". The abandoned task reads as handled.

Fix, in the twin search:

```python
key = " ".join(t["title"].lower().split())
...
if (oid != tid and o["end"] and o["title"] and " ".join(o["title"].lower().split()) == key
        and (o.get("end_chain") or o["chain"]) != t["chain"]
        and (o["start"] or o["end"]) >= (t["start"] or "")):
```

`test_report_status_tail_merge_and_detect_cross_chain_close` still passes (same title, other chain).

### D6-10 (Low) — `(no task)` bucket and id-less tasks

- **Title.** `gblog note --bot ops --detail a`, then `gblog log --bot ops --task Deploy --action shell --target ls`. The report files both under `### Deploy`.
- **Outcome.** A `task-end` without an id (gblog warns it "closes no task") gives the bucket `end …, outcome **ok**`.
- **`status`.** `gblog log --bot ops --type task_start --task x` makes `open_tasks` 1 (`open_tasks.add(None)`). An id-less `task-end` then sets it to 0, against its own warning.

Fix:

```python
# render_report
tid = r.get("task_id") or "(no task)"
untasked = tid == "(no task)"
...
if r.get("task") and not t["title"] and not untasked:
    t["title"] = r["task"]
if r.get("type") == "task_start" and not untasked: ...
elif r.get("type") == "task_end" and not untasked: ...
else: t["actions"].append(r)
# create the bucket with title "" when untasked

# status
tid = r.get("task_id")
if not tid:
    continue
```

Related, in the metric table: "Failed" and "Denied" count records of any type. One failed action plus its `task-end --outcome fail` shows `| Failed | 2 |`. Either count `type == "action"` only, or rename the rows.

### D6-11 (Low-Med) — "archived file(s) intact" counts claims without a file

```python
arch_recs = [rec for _rel, _lineno, rec in items if rec.get("type") == "archive"]
archives = len(arch_recs)
```

Reproduction: `gblog log --bot ops --type archive --target claim.pdf; gblog verify --bot ops` prints `OK ops 1 records head … (1 archived file(s) intact)`. The next line is `! … archive record without path and sha256: no archived file backs it`.

Fix:

```python
archives = sum(1 for rec in arch_recs if rec.get("path") or rec.get("sha256"))
```

Change DESIGN §7 (`archives`) to "archive records that name a file". Existing tests count only real archives.

### D6-12 (Low) — a refused reserved name still creates directories

`archive_file` stores the file before `append` runs `_check_reserved`. The cleanup removes the `.gz` but not the directories. `_makedirs` also creates a missing root.

Reproduction: with a root that does not exist, `gblog archive --bot aliases --file /etc/hostname` exits 2 and leaves `<root>/archive/aliases/<day>/`. README and DESIGN §10 say a refused name writes nothing.

Fix, at the top of `archive_file`:

```python
bot = safe_name(bot)
ledger._check_reserved(bot)   # before anything is stored
```

### D6-13 (Low) — `archive` argument edges

| Case | Today | Fix |
|---|---|---|
| `--name "notes 2026/11/16"` | `archive_file` applies `os.path.basename` again; target is `16` | In the CLI: `name = (args.name or default).replace("/", "-").replace("\\", "-")`; keep `basename` for library callers |
| `--task "Title"` | Accepted (from `_add_common`), not stored, no message | Pass `task=` through to the record, or warn as for `--source` |
| `--file F --stdin` | stdin wins silently | `archive: give --file PATH or --stdin, not both`, exit 2 |
| `--stdin` with empty input | Stores a 0-byte archive, exit 0 | stderr: `warning: archived content is empty (0 bytes)` |

### D6-14 (Low) — spooled events without an alert file are invisible in `status`

```python
a_ts, a_n = chain_alerts[c]
if a_ts:
    out.write("ALERT %s: ...")
```

`_orphan_alerts` already falls back to the first spool file's `ts`; the main rows do not.

Reproduction: break state, `gblog note --bot ops --detail x` (exit 4), delete `state/ops.alert.json`. `gblog status` prints no `ALERT`. `status --json` has `"alert": null, "spooled": 1`. `gblog spool list` shows the event.

Fix: move the fallback into `Ledger.alert_info` and delete the four lines in `_orphan_alerts`:

```python
files = self.spool_files(bot)
if ts is None and files:
    first = _read_json(files[0]) or {}
    ts = first.get("ts") if isinstance(first.get("ts"), str) else "unknown"
return ts, len(files)
```

### D6-15 (Low) — `report` CLI

- **`--date D --since S`.** `--since` wins and `--date` is ignored without a message. Fix in `_validate_args`: `raise _UsageError("report --date cannot be combined with --since")`.
- **`--since today|yesterday`.** Rejected, although `--date` accepts both. Accept the same two words.
- **In-place truncation.** The report file (and the tuning file) is opened with `O_TRUNC` on the final path. A failed write (`ENOSPC`) leaves the previous report empty. Fix with one helper for both call sites:

```python
def _write_markdown(path: str, text: str) -> None:
    if os.path.islink(path):   # keep today's refusal (exit 2) instead of replacing the link
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)
    _atomic_write(path, lambda fh: fh.write(text), fsync_dir=False, errors="backslashreplace")
```

This needs `import errno`.

### D6-16 (Low) — documentation drift

| File | Statement | Code |
|---|---|---|
| README (archive), `SECURITY.md` "Not covered", DESIGN §6 `scanned` | Non-UTF-8 content is stored unscanned with `scanned: false` | Only content with a NUL byte is. Other non-UTF-8 text is scanned with `surrogateescape` and carries no `scanned` key. CHANGELOG and FAQ 18 are right |
| DESIGN §4, CHANGELOG | A symlink at a written path is an error (exit 2) | True for day files, reports, tuning files. Archive, state and anchor files are replaced by `os.replace` (exit 0, never followed) |
| DESIGN §7 `archives` | "number of `archive` records" | The text line calls them "archived file(s) intact" (D6-11) |
| README alias section | `alias list` shows chains on disk and unregistered chains | Case variants and display-name chains are missing (D6-06) |
| README alias section | Lists what `alias add` refuses | A slug equal to another display name is accepted (D6-05) |
| README, DESIGN §2 | Records are attributed by chain directory | Not when `bot` is not a string (D6-03) |
| README | Reserved names "write nothing" | `archive` leaves directories (D6-12) |
| README (twin flag) | "closed in another chain", "same title" | Any chain, digits ignored (D6-09) |
| `docs/agent-integration.md` §3 | `archive` warns about `--source` | `--task` is dropped without a warning (D6-13) |
| README, FAQ | `export` dumps JSON lines | A hand-written line with `NaN` is exported as `NaN`, which strict parsers reject. Say so, or pass `allow_nan=False` and skip the record with the existing warning |

### D6-17 — simplifications that do not change behaviour

- **Reader predicate.** Four copies (`_readable`, `tail_records`, `records()` later branch, `tune_history`) become `_reader_skips(rec)` from D6-03.
- **Attribution.** `if rec.get("bot") != chain: rec = dict(rec, bot=chain, bot_field=rec.get("bot"))` appears five times. One helper `_attributed(rec, chain)`.
- **Sort key.** `(str(r.get("ts") or ""), str(r.get("bot") or ""), _seq_int(r.get("seq")))` appears in `records`, `tail_records` and the `tune` branch. One module-level `_record_order(r)`.
- **`tail` branch.** `if args.n <= 0: return 0` repeats the first line of `tail_records`. Delete the CLI copy.
- **`_add_alias`.** `variants + ([display] if display else [])` is built three times. Compute it once.
- **Imports.** `import gzip` and `import zlib` are repeated inside four functions. Import both at module level.
- **`alias` branch.** `data = ledger.aliases()` is read for every subcommand but used only by `list`. Move it there. `add` and `remove` read the registry themselves, so the "unreadable" warning still appears once.
- **Outside this area, noted only.** `Ledger._chain_lines` and the class `_ChainLines` (about 70 lines) have no caller; `verify` uses `_chain_snapshot`.

## Test cases to add

`tests/test_integrity.py`

- `test_archive_path_survives_key_shaped_bot_names` (D6-01): for `xai-research-assistant`, `sk-customer-support-bot` and `sk_test_environment`, `rec["path"]` equals the real address, `verify` is OK and `archives == 1`.
- `test_archive_target_is_still_redacted` (D6-01): name `token=abcdef123456.txt` gives a target with `[REDACTED]`; `path` is untouched.
- `test_spooled_archive_event_keeps_its_path` (D6-01): tail mismatch on an `xai-…` chain; the spool payload holds the real path and replay verifies.
- `test_redacted_archive_path_of_old_record_is_resolved` (D6-01 step 2): raw record with `archive/[REDACTED]/…`; `verify` is OK.
- `test_fifo_at_archive_address_fails_not_hangs` (D6-02): subprocess `verify --all` with `timeout=20` exits 1 with `not a regular file`; a second chain still prints `OK`. Skip without `os.mkfifo`.
- `test_bare_archive_record_is_not_counted_as_a_file` (D6-11): `log --type archive`; the OK line has no "archived file(s) intact", the `!` line is present, exit 0.

`tests/test_malformed_input.py`

- `test_fifo_state_and_alias_files_do_not_hang` (D6-02): FIFO as `state/ops.json`, then as `state/aliases.json`; `status`, `tail` and `verify --bot ops` return within the timeout.
- `test_non_string_bot_record_is_reported_by_directory` (D6-03): the record appears in `report`, `tail`, `export` and `status` (`today`/`seq`), and no "skipped" warning is printed.

`tests/test_cli_ux.py`

- `test_sanitised_name_resolves_to_registered_slug` (D6-04): `--bot "OPS BOT"` writes to `ops-bot` without a warning, also under strict mode; `resolve(resolve(x)[0]) == resolve(x)` for a table of names.
- `test_slug_cannot_shadow_another_display_name` (D6-05): exit 2 and unchanged registry; a hand-written registry with the pair appears under `conflicts:`.
- `test_chains_for_follows_resolution_in_conflicting_registry` (D6-06): `report --bot holder`, `tail --bot holder` and `export --bot holder` contain none of `mmm`'s records.
- `test_alias_list_shows_case_variant_chains_and_sorted_unregistered` (D6-06): chain `Ops` is listed under `ops`; three unregistered chains print in sorted order (run in a subprocess with two `PYTHONHASHSEED` values).
- `test_report_names_housekeeping_records_by_type` (D6-07): `verify`, `sync`, `archive` and `tune` lines; action, approval and note lines are byte-identical to today.
- `test_report_shows_task_end_summary` (D6-08): the summary line is present; a task without detail renders as before.
- `test_open_task_hint_needs_same_title_and_other_chain` (D6-09): the same-chain case prints plain `outcome **open**`; the cross-chain case keeps the hint.
- `test_untasked_records_do_not_take_a_title_or_outcome` and `test_status_ignores_task_records_without_id` (D6-10).
- `test_archive_reserved_name_creates_nothing` (D6-12): the root does not exist afterwards.
- `test_archive_name_with_slash`, `test_archive_file_and_stdin_together`, `test_archive_task_flag_is_stored_or_announced`, `test_archive_empty_stdin_warns` (D6-13).
- `test_report_date_with_since_is_usage_error`, `test_report_since_today_and_yesterday`, `test_failed_report_write_keeps_previous_file` (D6-15; mock `os.fsync` to raise `ENOSPC`).

`tests/test_append_safety.py`

- `test_status_alert_from_spool_without_alert_file` (D6-14): an `ALERT` line and a non-null `alert` in `status --json`.

`tests/test_security_regressions.py`

- Extend `test_report_is_not_written_through_a_symlink`: assert exit 2 after the D6-15 change, so the refusal is kept on purpose.