# Deep review 2 — append, locking, spool and alerts, crash windows, recovery and state rebuild

Scope: `gblog.py` as it is in the tree (`__version__ = "0.6.1"`). Functions read: `Ledger.append`, `_append_head`, `_append_line`, `_refuse_tail`, `write_state`, `update_state`, `_Lock` / `lock`, `rebuild_state`, `spool_files`, `spool_replay`, `alert_info`, `_orphan_alerts`, `seal_break` (crash windows only), `_fsync_dir`, `_makedirs`, `_atomic_write`, plus the `status`, `spool` and `state` CLI branches and the documents that describe them.

Method: static read only. Nothing was executed. Every reproduction is traced through the code, uses a temporary directory only, and is short enough to paste into a test. A fix that touches an existing test's expectations says so.

Common preamble for the reproductions (inside `tests/`, with `tests/` on `sys.path`):

```python
import errno, json, os, stat, tempfile, threading
from unittest import mock
import gblog
from _support import run, stamp          # run(argv, root) -> (rc, stdout, stderr)
root = tempfile.mkdtemp(prefix="gblog-d2-")
L = gblog.Ledger(root)
```

## Summary

| ID | Sev | Finding |
|---|---|---|
| D2-01 | Medium-High | A second failed state write after a roll-forward leaves state two records behind: every later append exits 4 |
| D2-02 | Medium | Reserved names are not enforced on the `update_state` path: `capture browser --bot ops.alert` forges an alert file |
| D2-03 | Medium | Events refused within one second are replayed in random order |
| D2-04 | Medium-Low | A state object with a non-string `head`: `verify` warns, `append` exits 4, `state rebuild` says "restore the log from backup" |
| D2-05 | Medium-Low | Right `head`, wrong `seq`: `verify` is silent and OK, `append` exits 4, `state rebuild` refuses |
| D2-06 | Medium-Low | The `ALERT` line comes from the alert file only: "since" is the latest refusal, and a spool without an alert file is invisible |
| D2-07 | Low-Medium | Capture events are spooled once per run and imported again after recovery |
| D2-08 | Low-Medium | `seal`: a tail mismatch on the sealed chain spools a `break_ack` that replay refuses forever; ack without witness cannot be completed |
| D2-09 | Low | An interrupted first append is not the "one behind" case: it leaves a permanent `state_adopted` marker |
| D2-10 | Low | `verify` reports FAIL in the one crash window that `append` treats as benign, with no hint |
| D2-11 | Low | `_fsync_dir`: `EINVAL` from a filesystem without directory fsync makes every append exit 2; a new chain directory's parent is never fsynced |
| D2-12 | Low | `state rebuild` replaces state before its note: if the note fails, the rebuild leaves no trace and a rerun is a no-op |
| D2-13 | Low | Removing a lock file while it is held gives two exclusive holders |
| D2-14 | Low | `chains_on_disk` counts gblog's side files: `verify --bot aliases` prints `OK … 0 records` |
| D2-15 | — | Simplifications: about 80 lines of dead or redundant code |
| D2-16 | — | Documentation that contradicts the code in this area |

## Findings

### D2-01 (Medium-High) — A second failed state write makes the benign window permanent

**Where.** `Ledger.append`, roll-forward branch. The adopted tail is only printed, not persisted, before the new line is written:

```python
if recovered == "interrupted":
    sys.stderr.write("notice: recovered state/%s.json after an interrupted append\n" % bot)
...
self._append_line(path, json.dumps(full, ...) + "\n")   # line N+2 is durable
...
self.write_state(bot, state)                             # fails again: state still names N
```

`_append_head` rolls forward only when `tail["seq"] == seq + 1`, so a state file two records behind is refused.

**Why it matters.** On a full disk, appending a short line to an existing day file often still succeeds (slack in the last block), while creating `state/<bot>.json.<pid>.tmp` fails. So:

- Append 1: line written, state write fails, exit 2.
- Append 2: rolls forward, line written, state write fails again, exit 2.
- Append 3: refused. The spool write also hits `ENOSPC`, so the command exits 2 and the event is lost.
- After space is freed, every append exits 4 until the operator runs `state rebuild`.

`docs/DESIGN.md` §4 says the one-behind state "is the only mismatch a crash inside `append` can produce". `test_enospc_in_write_state_recovers` covers one failure only.

**Reproduction.**

```python
L.append("ops", {"type": "note", "detail": "one"})
real, left = gblog.Ledger.write_state, [2]
def full_disk(self, bot, state):
    if left[0]:
        left[0] -= 1
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    return real(self, bot, state)
with mock.patch.object(gblog.Ledger, "write_state", full_disk):
    for text in ("two", "three"):
        try:
            L.append("ops", {"type": "note", "detail": text})
        except OSError:
            pass
# log: one, two, three; state: seq 1
L.append("ops", {"type": "note", "detail": "four"})   # today: TailMismatchError (CLI exit 4)
```

**Fix.** Persist the adopted tail before writing anything new. If that write fails too, nothing is appended and state is still exactly one behind. In `append`, directly after the `observed` check and before `_guard()`:

```python
if recovered == "interrupted":
    # Persist the adopted tail first. If this write fails as well, no new line has been
    # written and state is still exactly one record behind: the next append can retry.
    healed = dict(seen[0] or {})
    healed.update({"seq": seq, "head": head})
    if isinstance(tail.get("ts"), str):
        healed["updated"] = tail["ts"]
    self.write_state(bot, healed)
    seen[0] = healed
```

Keep the notice where it is. Doing this before the guard also repairs state when `spool replay`'s guard raises `_GuardSkip` on a rolled-forward tail; today state stays one behind in that case.

With the fix the reproduction ends with `one, two, four` (the `three` call fails cleanly, nothing written) and `verify` is OK.

---

### D2-02 (Medium) — Reserved names are only enforced in `append`

**Where.** `Ledger.update_state` has no name check, and `capture_browser` reaches it without appending:

```python
def update_state(self, bot: str, fields: Dict) -> bool:
    with self.lock(bot):
        existed = os.path.exists(self._state_path(bot))
        ...
        self.write_state(bot, state)
```

```python
if newest != markers.get(path):          # capture_browser, after the rows of one history
    markers[path] = newest
    changed = True
    save_markers(final=False)            # -> ledger.update_state(bot, {"capture_browser_last": ...})
```

The same call sits in the `except BaseException:` handler, so it also runs when `append` has just refused the reserved name.

**Effect.**

- `gblog capture browser --bot ops.alert` creates `state/ops.alert.json`, which is the alert file of chain `ops`. `status` then prints `ALERT ops: tail mismatch since unknown (0 spooled)`, and `examples/routine-daily-audit.sh` exits 4.
- `--bot aliases` writes chain-state keys into `state/aliases.json`.
- `--bot capture-files-ops` writes into another Bot's file-capture snapshot.
- `gblog archive --bot aliases --stdin` exits 2 but leaves `archive/aliases/<day>/` behind.

`capture browser` is on the README Auto-review allow-list, so the CLI-only adversary can do the first three. README: "`alias add` and every write command refuse them with exit 2 and write nothing."

**Reproduction.** `chrome_history()` is the helper in `tests/test_security_regressions.py`.

```python
L.append("ops", {"type": "note", "detail": "x", "ts": stamp(0)})
hist = chrome_history(os.path.join(root, "..", "chrome-d2"), [("chrome://newtab/", "New tab", 60)])
rc, out, err = run(["capture", "browser", "--bot", "ops.alert", "--history", hist], root)
# rc == 0, "browser: 0 new visit(s) from 1 history file(s)"
os.path.exists(os.path.join(root, "state", "ops.alert.json"))     # True
run(["status"], root)[1]   # ... "ALERT ops: tail mismatch since unknown (0 spooled)"
```

With an `https://` visit in the history the command exits 2 ("reserved") and the file is still written.

**Fix.** Two lines, both using the existing check (a legacy chain with such a name still passes because `logs/<name>` exists).

1. `_main`, where write commands resolve `--bot`:

```python
if getattr(args, "bot", None) and args.cmd in ("task-start", "log", "approval", "task-end", "note", "archive", "capture"):
    args.bot = _resolve_bot(ledger, args.bot)
    ledger._check_reserved(safe_name(args.bot))   # exit 2 before anything is created
```

2. `Ledger.update_state`, first statements (for library callers):

```python
bot = safe_name(bot)
self._check_reserved(bot)
```

---

### D2-03 (Medium) — Spool order within one second is random

**Where.** `_refuse_tail` names the file with a one-second stamp and a random suffix. `spool_files` sorts by name, and `spool_replay` appends in that order:

```python
stamp = now_iso().replace("-", "").replace(":", "") + "-" + uuid.uuid4().hex[:8]
spool_path = os.path.join(spool_dir, stamp + ".json")
```

The docs promise "in file-name (= time) order" (DESIGN §4) and "in order" (README).

**Effect.** A scripted `task-start; log; task-end` typically lands within one second. After replay the chain can hold `task_end` before `task_start`. `status` then counts the task as open forever, and `tune` computes a negative duration. `test_spool_list_and_replay_clears_alert` compares a set, not the order.

**Reproduction.**

```python
L.append("ops", {"type": "note", "detail": "seed", "ts": stamp(0)})
L.write_state("ops", {"seq": 99, "head": "cd" * 32})
class Fake:                                  # stand-in for a UUID
    def __init__(self, h): self.hex = h + "0" * 24
ids = iter(["ffffffff", "88888888", "00000000"])
with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T10:00:05Z"), \
        mock.patch.object(gblog.uuid, "uuid4", side_effect=lambda: Fake(next(ids))):
    for rec in ({"type": "task_start", "task_id": "t1", "task": "T"},
                {"type": "action", "task_id": "t1", "action": "shell", "target": "ls"},
                {"type": "task_end", "task_id": "t1", "outcome": "ok"}):
        try:
            L.append("ops", rec)
        except gblog.TailMismatchError:
            pass
seq, head = L._tail_head("ops"); L.write_state("ops", {"seq": seq, "head": head})
[r["type"] for r in L.spool_replay("ops")["replayed"]]
# today: ['task_end', 'action', 'task_start']; `gblog status --json` -> open_tasks 1
```

Without the mocks, 30 refused appends in a loop show the same thing.

**Fix.** Refusals of one chain are serialised by its lock, so a per-second counter is their order. In `_refuse_tail`, after `_makedirs(spool_dir)`:

```python
stamp = now_iso().replace("-", "").replace(":", "")
try:
    nth = sum(1 for f in os.listdir(spool_dir) if f.startswith(stamp + "-") and f.endswith(".json"))
except OSError:
    nth = 0
spool_path = os.path.join(spool_dir, "%s-%04d-%s.json" % (stamp, nth, uuid.uuid4().hex[:8]))
```

Old names still sort and replay as before. `_spool_scan_from` and `_spool_replayed` only use the stamp prefix and the `[A-Za-z0-9._-]+` shape.

---

### D2-04 (Medium-Low) — Non-string `head`: three commands, three answers

**Where.** `verify` treats a non-string head as unreadable:

```python
state_readable = isinstance(state_head, str)  # a non-string head is as good as no state file
```

`_append_head` and `rebuild_state` use `_state_head()`, which hands the value through:

```python
seq, head = _state_head(state)        # (2, 7) for {"seq": 2, "head": 7}
...
return seq, head, tail, observed, ""  # refused
```

```python
known = any(rec.get("hash") == old_head and rec.get("seq") == old_seq for ...)   # rebuild_state
if not known or old_seq > tail["seq"]:
    raise StateRebuildError("... is ahead of the log or names a record that is not in it; restore the log from backup")
```

DESIGN §4 says an unreadable state file "follows exactly the same rule as a missing one, in `append` and in `update_state`"; §7 says a non-string head "counts as unreadable".

**Reproduction.**

```python
for i in range(2):
    L.append("ops", {"type": "note", "detail": str(i), "ts": stamp(i)})
with open(os.path.join(root, "state", "ops.json"), "w") as fh:
    json.dump({"seq": 2, "head": 7, "last_sync": {"ts": "x", "commit": "y"}}, fh)
run(["verify", "--bot", "ops"], root)             # rc 0, "! state/ops.json missing or unreadable: ..."
run(["note", "--bot", "ops", "--detail", "n"], root)   # rc 4, ALERT tail mismatch
run(["state", "rebuild", "--bot", "ops"], root)   # rc 2, "... restore the log from backup"
```

The log is intact. The only way out today is deleting the state file by hand.

**Fix.** One predicate, used by the three state consumers of the append path:

```python
def _usable_head(state: object) -> bool:
    """True when a parsed state object can be compared with the log tail (the test verify applies)."""
    return isinstance(state, dict) and isinstance(state.get("head", GENESIS), str)
```

- `_append_head`: keep the raw object for the merge, decide on the usable one.

```python
raw = self._load_state(bot) if state_exists else None
if seen is not None:
    seen.append(raw)
state = raw if _usable_head(raw) else None
```

  The rest is unchanged: `state is None` with a self-consistent tail adopts it as `"unreadable"`, prints the notice and sets `state_adopted`. `append` then merges `seq`/`head` into `seen[0]`, so `last_sync` and capture markers survive.

- `update_state`:

```python
raw = self._load_state(bot)
state = raw if _usable_head(raw) else None
if state is None:
    state = dict(raw or {}, seq=0, head=GENESIS)
    ...   # existing tail-seeding code, using state.update({"seq": rec["seq"], "head": rec["hash"]})
```

- `rebuild_state`: `if not _usable_head(old): was = "seq - head %s" % ("unreadable" if exists else "missing")`, then rebuild (see D2-05 for the other branch).

---

### D2-05 (Medium-Low) — Right `head`, wrong `seq`

**Where.** `verify` compares the head only (`state_head not in (GENESIS, final_head)`). `_append_head` needs both (`tail["seq"] == seq and tail["hash"] == head`). `rebuild_state` looks for a record with that hash and that seq, finds none, and refuses with the "restore the log from backup" text quoted in D2-04.

**Reproduction.**

```python
for i in range(2):
    L.append("ops", {"type": "note", "detail": str(i), "ts": stamp(i)})
L.write_state("ops", {"seq": 99, "head": L.read_state("ops")["head"]})
run(["verify", "--bot", "ops"], root)                  # rc 0, no "!" line at all
run(["note", "--bot", "ops", "--detail", "n"], root)   # rc 4
run(["state", "rebuild", "--bot", "ops"], root)        # rc 2, "restore the log from backup"
```

**Fix.**

1. `rebuild_state`: identify the record by its hash (unique in a verified chain) and stop re-reading the chain. `res.hashes` is the set of the snapshot `verify` just walked:

```python
with self.lock(bot):
    _path, tail = self._tail_record(bot)
    if not _self_consistent(tail) or not _fields_ok(tail) or tail["hash"] != res["head"]:
        raise StateRebuildError("refusing: the log changed while it was being checked; run the command again")
    ...
    elif old_head != GENESIS and old_head not in res.hashes:
        raise StateRebuildError("refusing: state/%s.json (seq %s head %s) names a record that is not in the log; "
                                "restore the log from backup" % (bot, old.get("seq"), str(old_head)[:12]))
```

   State ahead of the log is still refused: its head is not in the chain. `Ledger._chain_items` loses its only caller (D2-15).

2. `verify`: an additive warning, so the two commands stop disagreeing in silence. After the state checks:

```python
last_seq = segments[-1]["seq_end"] if segments else None
if state_readable and items and state_head == final_head and isinstance(last_seq, int) \
        and _state_head(raw_state)[0] != last_seq:
    warnings.append("state/%s.json: seq %s does not match the last record (seq %d); appends are refused "
                    "until `gblog state rebuild` is run" % (bot, raw_state.get("seq"), last_seq))
```

   A healthy ledger prints nothing new. The fuzz test's `state_garbage`/`seq` cases only assert the absence of the two existing state warnings.

---

### D2-06 (Medium-Low) — The `ALERT` line reads the alert file only

**Where.** `Ledger.alert_info` and the `status` text loop:

```python
if os.path.exists(path):
    data = _read_json(path)
    ts = data.get("ts") if data and isinstance(data.get("ts"), str) else "unknown"
return ts, len(self.spool_files(bot))
```

```python
a_ts, a_n = chain_alerts[c]
if a_ts:
    out.write("ALERT %s: tail mismatch since %s (%d spooled)\n" % (c, _term(a_ts), a_n))
```

`_refuse_tail` replaces the alert file on every refusal. Two consequences:

- **"since" is the latest refusal.** With a shell hook that keeps spooling, it moves with every command. The start of the incident is the datum the operator needs.
- **A spool without an alert file is invisible** for a chain that still has `logs/<bot>`. This happens when the alert write failed after the spool write, the process died between the two, or someone removed the file. `_orphan_alerts` has a fallback to the first spool file, but it only runs for chains without a log directory. The example audit script only runs `spool list` when it saw an alert.

**Reproduction.**

```python
L.append("ops", {"type": "note", "detail": "seed", "ts": stamp(0)})
L.write_state("ops", {"seq": 99, "head": "cd" * 32})
for when in ("2024-06-01T10:00:00Z", "2024-06-01T12:00:00Z"):
    with mock.patch.object(gblog, "now_iso", return_value=when):
        try:
            L.append("ops", {"type": "note", "detail": "refused"})
        except gblog.TailMismatchError:
            pass
run(["status"], root)[1]    # "ALERT ops: tail mismatch since 2024-06-01T12:00:00Z (2 spooled)"
os.remove(os.path.join(root, "state", "ops.alert.json"))
run(["status"], root)[1]    # no ALERT line; --json: "alert": null, "spooled": 2
```

**Fix.** One definition of "since": the oldest pending refused event, else the alert file.

```python
def alert_info(self, bot: str) -> Tuple[Optional[str], int]:
    """(since, pending). ``since`` is the time of the oldest pending spooled event, else of
    state/<bot>.alert.json; None when there is neither."""
    files = self.spool_files(bot)
    alert = os.path.join(self.state_dir, bot + ".alert.json")
    since = None
    for path in files[:1] + ([alert] if os.path.exists(alert) else []):
        data = _read_json(path)
        ts = data.get("ts") if data else None
        if isinstance(ts, str):
            since = ts
            break
        since = "unknown"
    return since, len(files)
```

Then delete the three-line fallback in `_orphan_alerts` (`if not a_ts and a_n: first = _read_json(...)`). The existing alert tests keep passing: for a single refusal the spool file and the alert file carry the same `ts`.

---

### D2-07 (Low-Medium) — Capture events are spooled per run and imported again after recovery

**Where.** `capture_files` and `capture_browser` append through the normal path. A refusal spools the first pending change and aborts; the snapshot or marker deliberately does not advance.

```python
for kind, rel in changes[:limit]:
    ledger.append(bot, {"type": "action", "action": "file", "target": rel, ... "capture": "files"})
```

Each run during an alert spools the same first change again. After recovery, `spool replay` appends every copy, and the next capture run finds the change once more.

**Reproduction.**

```python
work = tempfile.mkdtemp(prefix="gblog-d2-work-")
open(os.path.join(work, "a.txt"), "w").close()
L.append("ops", {"type": "note", "detail": "seed", "ts": stamp(0)})
gblog.capture_files(L, "ops", work)                       # baseline
open(os.path.join(work, "b.txt"), "w").close()
good = dict(L.read_state("ops")); L.write_state("ops", {"seq": 99, "head": "cd" * 32})
for _ in range(3):
    run(["capture", "files", "--bot", "ops", "--path", work], root)     # rc 4 each time
L.write_state("ops", good)
run(["spool", "replay", "--bot", "ops"], root)            # three "b.txt created" records
run(["capture", "files", "--bot", "ops", "--path", work], root)         # a fourth
```

**Fix.** A capture event is re-derivable, so raise the alert but do not spool it.

- `append(self, bot, record, _guard=None, _spool=True)` passes `spool=_spool` to `_refuse_tail`.
- `_refuse_tail(..., spool=True)`:

```python
if spool:
    self._atomic_json(spool_path, payload)
else:
    payload["spool"] = None
    spool_path = None
self._atomic_json(alert_path, payload)
```

- `main()` prints `exc.spool_path or "- (not spooled: the capture finds it again on its next run)"`.
- The four `ledger.append(...)` calls in `capture_files` and `capture_browser` pass `_spool=False`.

`sync` and `verify --record` keep spooling; `test_sync_commits_spool_when_system_chain_mismatched` and `test_verify_record_continues_after_tail_mismatch` depend on it. `test_snapshot_kept_when_append_refused` still passes (exit 4, snapshot unchanged, no file records).

---

### D2-08 (Low-Medium) — `seal`: two holes around the ack

**Where.** `Ledger.seal_break` preflights only the witness chain:

```python
if bot != "ledger" and not self._tail_ok("ledger"):
    raise SealError("refusing: ledger witness chain has a tail mismatch")
ack = self.append(bot, {"type": "break_ack", ...}, _guard=_no_second_ack)
if bot != "ledger":
    self.append("ledger", {"type": "note", ...})
```

**(a) Tail mismatch on the sealed chain itself.** `append` spools the `break_ack`. `spool_replay` never replays that type (`rejected.append(path)`), so the file stays, `spool replay` exits 2 on every run, and the alert is never resolved.

```python
# root = a temporary copy of tests/fixtures/kim-biseo-break (logs/ and state/)
desc = L.verify("kim-biseo")["breaks"][0]
L.write_state("kim-biseo", {"seq": 99, "head": "cd" * 32})
try:
    L.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
except gblog.TailMismatchError:
    pass                                  # spool/kim-biseo/<stamp>.json holds a break_ack
seq, head = L._tail_head("kim-biseo"); L.write_state("kim-biseo", {"seq": seq, "head": head})
res = L.spool_replay("kim-biseo")        # rejected: 1, remaining: 1, alert_resolved: None
```

**(b) Ack written, witness not.** If the second `append` fails for any reason other than a tail mismatch (`ENOSPC`, a kill), `verify` warns `witness note missing on ledger chain` forever. A rerun refuses (`a break_ack is already present`), and `docs/chain-repair-live-apply.md` forbids a hand-written note.

```python
real, calls = gblog.Ledger.append, []
def second_fails(self, bot, record, **kw):
    calls.append(bot)
    if len(calls) == 2:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    return real(self, bot, record, **kw)
with mock.patch.object(gblog.Ledger, "append", second_fails):
    ...  # seal_break raises OSError; the ack is in the chain, no witness
L.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)   # SealError: already present
```

**Fix.**

(a) Preflight the chain's own tail before the witness preflight; nothing is spooled:

```python
if not self._tail_ok(bot):
    raise SealError("refusing: %s has a tail mismatch (state does not match the log tail); nothing was sealed" % bot)
```

(b) Let the same command finish an interrupted seal. Move the ack scan before the `found = self.verify(bot).get("breaks")` check (after a successful ack that list is empty) and replace the plain refusal:

```python
acks = [rec for _p, _n, rec in self.iter_records(bot) if rec.get("type") == "break_ack"]
if acks:
    same = (len(acks) == 1 and acks[0].get("jay_approval_id") == jay
            and isinstance(acks[0].get("break"), dict) and _same_break(acks[0]["break"], desc))
    if same and bot != "ledger" and self._witness_present(desc, jay) is not True:
        self._append_witness(bot, desc, jay)     # the existing "ledger" note append, moved into a helper
        return acks[0]
    raise SealError("refusing: a break_ack is already present")
```

`docs/chain-repair-live-apply.md` ("Witness warning") then needs one changed sentence: rerunning `seal` with the same approval id adds only the missing witness. This changes the live procedure, so it needs the operator's sign-off. (a) can go in without (b).

---

### D2-09 (Low) — An interrupted first append leaves a permanent marker

**Where.** `_append_head` can only roll forward when a state file exists. For the first record of a chain there is none yet:

```python
if state is None:
    ...
    return tail["seq"], tail["hash"], tail, None, ("unreadable" if state_exists else "missing")
```

So "line durable, state not written" on record 1 is handled as a lost state file. Record 2 gets `state_adopted: missing`, and `verify` prints a `!` line for it forever. DESIGN's crash table says "between 6 and 7 → roll-forward" without this exception.

**Reproduction.**

```python
real, first = gblog.Ledger.write_state, [True]
def fail_once(self, bot, state):
    if first[0]:
        first[0] = False
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    return real(self, bot, state)
with mock.patch.object(gblog.Ledger, "write_state", fail_once):
    try:
        L.append("fresh", {"type": "note", "detail": "one"})
    except OSError:
        pass
rec = L.append("fresh", {"type": "note", "detail": "two"})
rec["state_adopted"]            # "missing"; stderr: "notice: state/fresh.json was missing; adopted ..."
L.verify("fresh")["warnings"]   # ["...:2: appended while state/fresh.json was missing; ..."]
```

**Fix.** Write the genesis state before the first line. The window then is the ordinary roll-forward case (`tail.prev == genesis`, `tail.seq == 0 + 1`). In `append`, after the `observed` check:

```python
if tail is None and seen[0] is None:
    # First record of a chain: with a genesis state on disk, an interruption between the
    # line and the state file is "one behind", not "state missing".
    seen[0] = {"seq": 0, "head": GENESIS}
    self.write_state(bot, seen[0])
```

Cost: one extra state write per chain lifetime. A deleted state file is still "missing" and still gets its marker. If the pre-write is not wanted, correct the DESIGN table instead (D2-16).

---

### D2-10 (Low) — `verify` reports FAIL in the benign window, with no hint

**Where.** `Ledger.verify`:

```python
if not problems and state_readable and state_head not in (GENESIS, final_head):
    state_problem = "state/%s.json: head does not match last record (state tampered or log truncated)" % bot
```

After one interrupted append, state is one record behind a tail that links to it. `append` repairs this silently on its next call. `verify`, `anchor`, and an auditor's clone of a backup taken in that window report FAIL with a text that names tampering. The skill tells a Bot to stop on FAIL, so a Bot that verifies before its next append stops, and the chain is then never repaired without the operator.

**Reproduction.**

```python
L.append("ops", {"type": "note", "detail": "one", "ts": stamp(0)})
with mock.patch.object(gblog.Ledger, "write_state", side_effect=OSError(errno.ENOSPC, "full")):
    try:
        L.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
    except OSError:
        pass
run(["verify", "--bot", "ops"], root)   # rc 1, "- state/ops.json: head does not match last record (...)"
```

**Fix.** Keep the verdict and the text (tests compare both); add one warning that names the case. After `state_problem` is set:

```python
if state_problem and items:
    last = items[-1][2]
    if last.get("prev") == state_head and _state_head(raw_state)[0] == _seq_int(last.get("seq")) - 1:
        warnings.append("state/%s.json is exactly one record behind the log (an interrupted append): "
                        "the next append, or `gblog state rebuild`, repairs it" % bot)
```

Add the same sentence to DESIGN §4 "Crash outcomes" and to the skill's Validate section.

---

### D2-11 (Low) — Directory fsync: fatal where it is unsupported, missing where it is needed

**Where.** `_fsync_dir` and `_makedirs`:

```python
try:
    os.fsync(fd)
except OSError:
    if os.name != "nt":
        raise
```

```python
for d in reversed(missing):
    try:
        os.mkdir(d, 0o700)        # the parent directory is never fsynced
```

**(a)** On Linux, a filesystem whose directories have no fsync handler answers `EINVAL`; VirtualBox shared folders are the case I recall, and some FUSE and network mounts may behave the same (not checked here). `_atomic_write` calls `_fsync_dir` after `os.replace`, so the record and the state are written and the command still exits 2. Every append does this.

```python
real = os.fsync
def no_dir_fsync(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        raise OSError(errno.EINVAL, "Invalid argument")
    return real(fd)
with mock.patch.object(gblog.os, "fsync", no_dir_fsync):
    run(["note", "--bot", "ops", "--detail", "a"], root)[0]   # 2: line written, state not
    run(["note", "--bot", "ops", "--detail", "b"], root)[0]   # 2: line and state written,
                                                               #    record carries state_adopted: missing
```

**(b)** For a new chain, `logs/<bot>` is created and fsynced, but its entry in `logs/` is not. After a power loss the state file can survive while the chain directory does not. That is a loud FAIL (`state/<bot>.json records seq 1 … but logs/<bot> is missing`) and exit 4 on every append.

```python
synced = []
with mock.patch.object(gblog, "_fsync_dir", side_effect=lambda p: synced.append(p)):
    L.append("newbot", {"type": "note", "detail": "x"})
os.path.join(root, "logs") in synced     # False today
```

**Fix.**

```python
import errno
_NO_DIR_FSYNC = (errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS)

    try:
        os.fsync(fd)
    except OSError as exc:
        # A filesystem that cannot fsync a directory: nothing more can be done for durability.
        if os.name != "nt" and exc.errno not in _NO_DIR_FSYNC:
            raise
```

And at the end of `_makedirs`:

```python
for d in missing:
    _fsync_dir(os.path.dirname(d))     # make the new directory's own entry durable
```

`EIO` and `ENOSPC` still propagate.

---

### D2-12 (Low) — `state rebuild` can finish without its note

**Where.** `rebuild_state` replaces state under the lock, releases it, then appends the note:

```python
    self.write_state(bot, new)
note = self.append(bot, {"type": "note", "source": "auto",
                         "detail": "state rebuilt from log tail (was %s)" % was})
```

If the note append fails, state already matches the tail. A rerun prints `already matches the log tail; nothing to do`. When state had been missing, nothing records that: no note, no `state_adopted` marker, and the `verify` warning is gone.

**Reproduction.**

```python
for i in range(3):
    L.append("ops", {"type": "note", "detail": str(i), "ts": stamp(i)})
os.remove(os.path.join(root, "state", "ops.json"))
with mock.patch.object(gblog.Ledger, "_append_line", side_effect=OSError(errno.ENOSPC, "full")):
    run(["state", "rebuild", "--bot", "ops"], root)     # rc 2
run(["state", "rebuild", "--bot", "ops"], root)[1]      # "... already matches the log tail; nothing to do"
run(["verify", "--bot", "ops"], root)[1]                # OK, no "!" line
```

**Fix.** Put state back when the note could not be written, so the rebuild can be repeated and still leaves its note.

```python
old_bytes = None
if exists:
    with open(self._state_path(bot), "rb") as fh:
        old_bytes = fh.read()                # inside the lock, before write_state(bot, new)
...
try:
    note = self.append(bot, {...})
except BaseException:
    with self.lock(bot):
        if self._load_state(bot) == new:     # nobody else has written state since
            if old_bytes is None:
                os.remove(self._state_path(bot))
            else:
                _atomic_write(self._state_path(bot), lambda fh: fh.write(old_bytes), mode="wb")
    raise
```

If the note's line became durable and only the final state write failed, the restore still applies. The rerun then writes a second note; the first stays.

---

### D2-13 (Low) — A removed lock file gives two exclusive holders

**Where.** `Ledger._Lock.__enter__` locks whatever file is at the path when it opens it:

```python
self.fh = _open_lock(self.path, "a+")
...
fcntl.flock(self.fh.fileno(), fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)
```

`state/*.lock` is in the ledger's `.gitignore`, so `git clean -fdX` in the ledger root removes every lock file. The README's retirement procedure also runs `rm -f … state/B.lock`. A writer that starts after the removal creates and locks a new inode while the first writer is still inside its critical section. Two appends then link to the same `prev`, which is a permanent FAIL.

**Reproduction.**

```python
L.append("ops", {"type": "note", "detail": "x"})
path = os.path.join(root, "state", "ops.lock")
entered = threading.Event()
def second():
    with L.lock("ops"):
        entered.set()
with L.lock("ops"):
    os.remove(path)
    t = threading.Thread(target=second); t.start(); t.join(1)
    entered.is_set()        # True today: a second "exclusive" holder
t.join(10)
```

**Fix.** After acquiring, check that the locked file is still the one at the path; otherwise retry. Exclusive branch:

```python
while True:
    fh = _open_lock(self.path, "a+")
    if fcntl is None:
        break
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        current = os.stat(self.path)
    except OSError:
        current = None
    if current is not None and os.path.samestat(os.fstat(fh.fileno()), current):
        break
    fh.close()     # removed or replaced while we waited: lock the file that is there now
self.fh = fh
```

Apply the same check in the shared branch when a lock was obtained.

---

### D2-14 (Low) — Side files under `state/` count as chains

**Where.** `Ledger.chains_on_disk`:

```python
return [c for c in self.chains_for(name)
        if os.path.isdir(os.path.join(self.logs_dir, c)) or os.path.exists(self._state_path(c))]
```

`state/aliases.json`, `state/<bot>.alert.json` and `state/capture-files-<bot>.json` are "the state file" of the names `aliases`, `<bot>.alert` and `capture-files-<bot>`.

**Reproduction.**

```python
run(["alias", "add", "--slug", "ops", "--display", "Ops"], root)
run(["verify", "--bot", "aliases"], root)   # rc 0: "OK    aliases    0 records  head genesis"
run(["anchor", "--bot", "aliases"], root)   # rc 0: writes anchors/aliases.json, a history line, a rollup entry
```

With a pending alert for `ops`, `verify --bot ops.alert` also prints `OK … 0 records` and creates `state/ops.alert.lock`. README: a name with no chain on disk "is never `OK … 0 records`".

**Fix.**

```python
return [c for c in self.chains_for(name)
        if os.path.isdir(os.path.join(self.logs_dir, c))
        or (os.path.exists(self._state_path(c)) and not is_reserved(c))]
```

A legacy chain with a reserved name still counts through its `logs/` directory. If that directory is gone, `verify --bot` reports `no chain on disk`, and `verify --all` still reports the orphaned state file.

---

### D2-15 — Simplifications (no behaviour change)

| Item | Evidence | Action | Lines |
|---|---|---|---|
| `_ChainLines` and `Ledger._chain_lines` | No caller. `verify` uses `_chain_snapshot`, which parses under the shared lock. The docstrings claim `verify` reads bytes under the lock and parses afterwards. | Delete both. If the shorter lock hold is wanted, wire it in as a separate, measured change. | about −70 |
| `Ledger._chain_items` | Only caller is `rebuild_state`; unneeded after D2-05. Also removes a second full read under the exclusive lock. | Delete | −6 |
| `seen` out-parameter of `_append_head` | It always appends exactly one element. `prior = seen[0] if seen else None` and `state = seen[0] if seen else self._load_state(bot)` have fallbacks that cannot run. | Return the raw state as a sixth tuple element; `_tail_ok` keeps reading index 3. | −4 |
| `_orphan_alerts` fallback | Duplicates what `alert_info` does after D2-06. | Delete | −3 |
| `_refuse_tail`: `_makedirs(self.state_dir)` | `_atomic_json` creates the parent of every file it writes. | Delete (keep `_makedirs(spool_dir)`; D2-03 lists that directory) | −1 |
| Field filter in `_refuse_tail` and in `append` | Two loops with the same rule (`None`/`""` dropped, strings redacted); they differ only in whether `ts` is skipped. | One helper `_stored_fields(record, skip)` | −5 |

---

### D2-16 — Documentation that contradicts the code

| File | Statement | Code | Change |
|---|---|---|---|
| DESIGN §5 rule 3 | `_Lock` "still carries a per-thread table of exclusive holds (`_held`)" that makes a second request a no-op | `_held` is gone (`test_removed_names_are_gone`). A nested request on the same lock file blocks forever. | "`_Lock` keeps no re-entrancy state; a nested request deadlocks." |
| CHANGELOG "Known open findings" | `_exit2`, `_assert_tail_matches`, `_BREAK_KEYS`, `_Lock._held` "are still in `gblog.py`"; `--version` prints 0.6.0 | All removed; `__version__` is 0.6.1 | Remove both bullets; re-check the rest of that list against the code. |
| DESIGN §4 "Traces of a lost state file"; §6 state keys | The state key `state_adopted` "is kept by every later `append` and by `state rebuild`"; "Later writers keep the key" | `append` moves it onto its record and drops the key. The paragraph above in the same section says so. | Keep the "moves into the next record" wording; delete the "kept" sentences and the repeated `update_state` paragraph. |
| DESIGN §4 steps 5–7 | "State is re-read, merged …" | The state read at the top of the lock hold is reused | "The state read under the lock is merged …" |
| DESIGN §4 "Crash outcomes" | "between 6 and 7 → roll-forward"; "the only mismatch a crash inside `append` can produce" | First record: state missing, adoption marker (D2-09). Two failures in a row: two behind, refused (D2-01). `verify` FAILs in the window (D2-10). | Rewrite the row once D2-01, D2-09 and D2-10 are decided. |
| DESIGN §6 | `state_adopted` is `missing` or `unreadable` | `append` can also write `unknown` (state key with an unexpected `how`) | Add the third value. |
| README; FAQ 7 | `ALERT <bot>: tail mismatch since <ts>` | Latest refusal, not the first (D2-06) | No text change after D2-06. |
| README, alias section | Reserved names: "every write command refuse them with exit 2 and write nothing" | `capture` writes (D2-02) | No text change after D2-02. |
| `docs/chain-repair-live-apply.md` | Exit 4 during the checklist: "stop and report" | A spooled `break_ack` can never be replayed (D2-08 a) | After D2-08 a: "`seal` refuses (exit 2) when `kim-biseo` has a tail mismatch; nothing is spooled." |

## Test cases to add

`tests/test_append_safety.py`

1. `test_two_failed_state_writes_do_not_brick_the_chain` (D2-01): the reproduction; afterwards the next append returns, the chain is `one, two, four`, `verify` is OK, no spool file, no alert.
2. `test_roll_forward_is_persisted_before_the_new_line` (D2-01): wrap `write_state` and `_append_line`; in the recovery append the first call is `write_state` with the tail's seq and head.
3. `test_append_crash_matrix`: inject one failure at each point (before the line; short write then `ENOSPC`; file fsync; between line and state; state temp write; `os.replace`; state directory fsync). After each: no torn line, at most one copy of the record, the next plain append exits 0 and `verify` is OK. Run it for a chain's first record and for a later one (D2-09).
4. `test_first_append_interrupted_rolls_forward_without_marker` (D2-09): fail the second `write_state` call of a new chain; record 2 has no `state_adopted`; stderr has the "recovered" notice.
5. `test_non_string_state_head_is_adopted_like_unreadable` (D2-04): `note` exits 0 with the "was unreadable; adopted" notice, `last_sync` survives, `state rebuild` on the same state exits 0, `update_state` seeds from the tail.
6. `test_state_seq_mismatch_warns_and_can_be_rebuilt` (D2-05): exact warning text from `verify` (exit 0); `state rebuild` exits 0 and state seq equals the note's seq; state with a head that is not in the chain is still refused.
7. `test_verify_hints_at_interrupted_append` (D2-10): FAIL with the unchanged problem text plus the new `!` line; no hint when state is two behind or ahead.
8. `test_dir_fsync_einval_is_tolerated` and `test_dir_fsync_eio_still_fails` (D2-11 a).
9. `test_new_chain_directory_entry_is_fsynced` (D2-11 b): `logs/` is among the `_fsync_dir` paths for a new chain, and not for a second append.
10. `test_state_rebuild_failure_restores_previous_state` (D2-12): after the failed run the state file is missing again (or byte-identical to before), and a rerun appends the note.
11. `test_lock_survives_lock_file_removal` (D2-13): the second acquisition is still blocked after 0.3 s and proceeds after release.
12. `test_spool_replay_keeps_refusal_order_within_one_second` (D2-03): the mocked reproduction; replayed types are `task_start, action, task_end` and `status --json` shows `open_tasks` 0.
13. `test_spool_file_names_sort_in_refusal_order` (D2-03): 20 refusals under a fixed `now_iso`; `spool_files()` equals the creation order.
14. `test_replay_after_crash_and_later_appends_does_not_duplicate`: append a record with `spool_id` by hand, add two more records, leave the spool file, replay; no second copy, file moved. Named in round 3 (W-08) but not in the tree.
15. `test_replay_guard_skip_heals_state` (D2-01): line durable, state one behind, spool file still pending; after `spool replay` state names the tail and `verify` is OK without another append.
16. `test_alert_since_is_the_first_pending_refusal` (D2-06).
17. `test_pending_spool_without_alert_file_is_reported` (D2-06): `status` text has the `ALERT` line and `status --json` has a non-null `alert`.
18. `test_update_state_refuses_reserved_names` (D2-02): `SystemExit(2)`, no file under `state/`; a legacy chain directory with such a name still works.

`tests/test_capture_sync.py`

19. `test_capture_refuses_reserved_bot_names_and_writes_nothing` (D2-02): for `ops.alert`, `aliases`, `capture-files-ops`, with an http and a non-http history: exit 2, the tree is unchanged, `status` prints no `ALERT`.
20. `test_capture_refusal_is_not_spooled` (D2-07): exit 4, alert file present, `spool/<bot>` has no pending file, stderr names the reason.
21. `test_no_duplicate_file_records_after_an_alert_period` (D2-07): three refused runs, restore state, replay, capture; exactly one `b.txt created` record.

`tests/test_cli_ux.py`

22. `test_archive_reserved_name_leaves_no_directories` (D2-02).
23. `test_side_files_are_not_chains` (D2-14): `verify --bot aliases` and `verify --bot ops.alert` exit 1 with `no chain on disk`; `anchor --bot aliases` writes nothing; no new lock file.

`tests/test_integrity.py`

24. `test_seal_refuses_when_own_chain_has_tail_mismatch` (D2-08 a): `SealError`, logs byte-identical, no spool file, no alert.
25. `test_seal_completes_a_missing_witness` (D2-08 b): ack present, witness append failed; the rerun appends exactly one witness note and no second ack; a rerun with another approval id is still refused.

`tests/test_round3_followups.py` (`DeadCodeRemovalTest`)

26. Extend `test_removed_names_are_gone` with `_ChainLines`, `Ledger._chain_lines` and `Ledger._chain_items` (D2-15).

`tests/test_examples.py`

27. `test_audit_reports_spool_without_alert_file` (D2-06): refused event, alert file removed; `routine-daily-audit.sh` exits 4 and prints the `spool list` output.