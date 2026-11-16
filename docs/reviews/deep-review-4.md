# Deep review 4 — capture (shell hook, browser, files), coverage and tune

Scope: `SHELL_HOOK`, `_install_shell_hook`, `capture_browser`, `capture_files`, `_url_target`, `norm_target`, `_is_plain_gblog`, `coverage`, `compute_metric`, `analyze`, `render_tune`, `save_proposals`, `tune_apply`, the `capture` / `coverage` / `tune` branches of `_main`, and the documents that describe them.

Method: static read of the current tree. Nothing was executed. Every reproduction was traced through the code and uses temporary directories only; "today" lines are what the code implies, not observed output. The D4-02 reproduction also relies on bash loading the history file for `bash -i` with a piped stdin, so run that one first.

## 1. Summary

| ID | Sev | Area | Finding |
|---|---|---|---|
| D4-01 | High | files / coverage | File records carry no observation time, so `file` coverage is about 0% under the documented weekly cadence |
| D4-02 | Medium | shell hook | Every new interactive shell logs the last command of the previous session |
| D4-03 | Medium | browser | `--hours` below 1 stores a marker in the future; a future marker is trusted forever |
| D4-04 | Medium | files | `capture files --path <other>` replaces the Bot's only snapshot, with no ledger trace |
| D4-05 | Medium | files | Sustained noise in alphabetically earlier paths keeps a later change pending forever |
| D4-06 | Medium | tune | A failure that is both self-reported and hook-captured counts twice; auto failures drive `skill` proposals |
| D4-07 | Medium | tune | `auto` decisions count as approvals: a working Allow rule is judged "not helping" |
| D4-08 | Medium | tune | Applying a `revert` proposal leaves the original change "already applied" forever |
| D4-09 | Medium | coverage / tune | Shared-computer capture gives every idle Bot a guaranteed `logging` proposal |
| D4-10 | Medium | coverage | Auto records are windowed by import time but matched by `observed`: false gaps after a missed run |
| D4-11 | Low-Med | browser | `capture browser` creates state for a name with no chain; `verify --bot <typo>` then prints `OK … 0 records` |
| D4-12 | Low-Med | browser | Marker keyed by the literal `--history` spelling; any readable file is copied whole |
| D4-13 | Low-Med | tune | Per-Bot caps count decided proposals: one applied sequence blocks all others |
| D4-14 | Low | shell hook | The hook resets `$?` for later `PROMPT_COMMAND` entries; `sed` eats digits on continuation lines |
| D4-15 | Low | files | A symlinked `--path` scans the ledger itself, on every run |
| D4-16 | Low | files | A newly excluded file is logged as `deleted` |
| D4-17 | Low | files | A failure part-way through a run logs the earlier changes again on the next run |
| D4-18 | Low | coverage | The documented task commands count as unreported shell actions |
| D4-19 | Low | browser | `_url_target` drops the port and IPv6 brackets |
| D4-20 | Low | tune | Decisions and effect metrics are lost after `alias add` |
| D4-21 | Low | shell hook | Plain `gblog alias / seal / sync / state / spool / capture` leave no auto record |
| D4-22 | Low | tune | A measurable change applied more than 90 days ago is hidden forever without measurement |
| D4-23 | Low | misc | Six small edge cases |
| D4-24 | — | docs | Documentation that contradicts the code |

Read and found sound:

- per-history marker save and the partial-failure marker in `capture_browser`;
- redact-before-cut for titles;
- the `--limit` below 1 refusal;
- the bisect matching in `coverage` (same first-unused-in-order rule as a full scan);
- the stored-`count` rule for rejected proposals;
- the id merge in `save_proposals`;
- the byte pre-filter in `tune_history`;
- the in-place hook update.

## 2. Findings

### D4-01 (High) — file capture has no observation time, so `file` coverage cannot work

Function: `capture_files` writes

```python
ledger.append(bot, {"type": "action", "action": "file", "target": rel, "outcome": "ok", "detail": kind,
                    "source": "auto", "capture": "files"})
```

and `coverage` matches on `t_a = _ts_epoch(a.get("observed") or a.get("ts"))`.

- Browser records carry `observed` (the visit time). File records do not, so `t_a` is the time of the capture run.
- The skill and the weekly routine say file capture runs once a week. A self-report matches only if it was written within ±30 minutes of that run.
- Consequences:
  - `file` coverage is near 0% for every Bot.
  - Rule I then adds a `logging` proposal (`coverage:file`) every week once three changes were observed.
  - Applying it cannot move the metric, so after 7 days it is "not helping" and a `revert` proposal follows.

Reproduction:

```python
import os, tempfile, time, gblog
base = tempfile.mkdtemp(); work = os.path.join(base, "work"); os.makedirs(work)
L = gblog.Ledger(os.path.join(base, "ledger"))
gblog.capture_files(L, "ops", work)                              # baseline
then = time.time() - 7200
L.append("ops", {"type": "action", "action": "file", "target": "report.csv",
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(then))})   # the Bot's report, 2 h ago
p = os.path.join(work, "report.csv")
with open(p, "w") as fh: fh.write("x")
os.utime(p, (then, then))                                        # the file it wrote at that time
gblog.capture_files(L, "ops", work)                              # the capture run, now
print(gblog.coverage(L.records("ops"))["rows"])
# today:    [{'bot': 'ops', 'action': 'file', 'auto': 1, 'matched': 0, 'pct': 0.0}]
# expected: matched 1, pct 100.0
```

Fix:

1. In `capture_files`, add `observed` (the file's mtime, UTC) to `created` and `modified` records. Omit it when the mtime cannot be converted or lies in the future. `append` drops `None`, and `coverage` already prefers `observed`.

   ```python
   def _mtime_iso(meta):
       try:
           if meta[0] > time.time():
               return None
           return _dt.datetime.fromtimestamp(meta[0], _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
       except (TypeError, ValueError, OverflowError, OSError, IndexError):
           return None
   # in the append loop:
   "observed": None if kind == "deleted" else _mtime_iso(current[rel]),
   ```

2. For `deleted` records, store `observed_from` (the `taken` value of the previous snapshot). In `coverage`, when it is present, accept a self-report with `lo - window <= t <= t_a + window`, where `lo = _ts_epoch(a["observed_from"])`. A deletion happened somewhere between the two snapshots.
3. `docs/routine-weekly-tune.md`: use `gblog capture files --bot <slug> --path /workspace --exclude transcripts`. The daily routine writes `transcripts/<slug>/<date>.jsonl` under `/workspace`, and each of those is currently an unreported `file` action for every Bot.
4. `docs/DESIGN.md` §6: `observed` is no longer "browser only".

Record format: one optional field on new records. Existing ledgers verify unchanged.

### D4-02 (Medium) — each new interactive shell logs the previous session's last command

Function: `SHELL_HOOK`

```bash
__gblog_last=""
...
  cmd=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null | sed 's/^ *[0-9]* *//')
  if [ -z "$cmd" ] || [ "$cmd" = "$__gblog_last" ]; then return 0; fi
```

- The first prompt of an interactive shell runs `PROMPT_COMMAND` before anything is typed.
- By then bash has loaded `~/.bash_history`, so `history 1` is the last command of an earlier session.
- `__gblog_last` is empty, so that command is logged again: current timestamp, outcome `ok`, source `auto`.
- This happens for every new terminal, tmux pane or nested `bash`. The phantom record has no self-report nearby, so it lowers shell coverage.

Reproduction:

```bash
T=$(mktemp -d); mkdir -p "$T/home"
export GBLOG_ROOT="$T/ledger" PATH="$PWD/bin:$PATH"
printf 'rm -rf /srv/old-release\n' > "$T/home/.bash_history"     # typed in an earlier session
gblog capture shell-hook --bot ops --install --rc "$T/home/.bashrc"
HOME="$T/home" bash -i <<< 'exit' 2>/dev/null                    # a new interactive shell; nothing is typed
gblog tail --bot ops -n 1
# today:    <now> ops #1 action shell rm -rf /srv/old-release
# expected: no record
```

Fix (hook text; keep the `# gblog hook v2` line, because the installer replaces a block whose text differs):

```bash
__gblog_last=""
__gblog_primed=""
__gblog_hook() {
  local rc=$? cmd
  cmd=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null | sed '1s/^ *[0-9]*\** *//')
  if [ -z "$__gblog_primed" ]; then
    __gblog_primed=1
    # The first prompt of an interactive shell comes before any typed command:
    # what history shows then was typed in an earlier session.
    case "$-" in *i*) __gblog_last="$cmd"; return "$rc";; esac
  fi
```

- The test harness (`bash -c 'source …; history -s …; __gblog_hook'`) is not interactive, so the existing hook tests behave as before.
- Known cost: re-sourcing `~/.bashrc` in a running shell primes again, so the `. ~/.bashrc` command itself is not logged.

### D4-03 (Medium) — `capture browser --hours` below 1 pins the marker in the future

Function: `capture_browser`

```python
cutoff_epoch = time.time() - hours * 3600
...
since = _marker_int(marker) if marker else from_epoch(cutoff_epoch)
...
last_target, newest = None, since
...
if newest != markers.get(path):
    markers[path] = newest
```

- `--hours` is not validated. A negative value puts `cutoff_epoch` in the future.
- For a history file without a marker, that future value becomes the marker even when no row was read.
- Later runs query `visit_time > marker` and import nothing until that time passes.
- `capture browser` is on the README's Auto-review allow-list, so one auto-approved command can blind browser capture for a new history file.
- W-04 stopped a bad *row* from pinning the marker, but a stored future marker is still trusted. That includes markers pinned before 0.6.1.

Reproduction:

```python
import os, sqlite3, tempfile, time, gblog
base = tempfile.mkdtemp(); prof = os.path.join(base, "chrome", "Default"); os.makedirs(prof)
hist = os.path.join(prof, "History")
def visit(url, age):
    c = sqlite3.connect(hist)
    c.executescript("CREATE TABLE IF NOT EXISTS urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                    "CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
    cur = c.execute("INSERT INTO urls(url, title) VALUES (?, 't')", (url,))
    c.execute("INSERT INTO visits(url, visit_time) VALUES (?, ?)",
              (cur.lastrowid, int((time.time() - age + gblog._WEBKIT_EPOCH_OFFSET) * 1e6)))
    c.commit(); c.close()
L = gblog.Ledger(os.path.join(base, "ledger")); L.init()
visit("https://a.example/one", 60)
print(gblog.capture_browser(L, "ops", [hist], hours=-87600)["added"])   # 0; the marker is now 10 years ahead
visit("https://b.example/two", 5)
print(gblog.capture_browser(L, "ops", [hist], hours=24)["added"])       # today 0, for 10 years
# expected: the first call is refused (exit 2); the second imports 2
```

Fix:

1. At the top of `capture_browser`, before state is read:

   ```python
   if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
       _die("capture --hours must be 1 or more; nothing was imported and the markers are unchanged")
   ```

2. Do not believe a marker that is more than one day ahead (the same rule as for rows). After `since` is computed:

   ```python
   try:
       ahead = bool(marker) and to_epoch(since) > time.time() + 86400
   except OverflowError:
       ahead = True
   if ahead:
       sys.stderr.write("notice: the browser-history marker for %s is dated in the future; ignoring it\n"
                        % _term(_clean_text(path)))
       since = from_epoch(cutoff_epoch)
   ```

### D4-04 (Medium) — `capture files --path <other>` destroys the Bot's baseline

Function: `capture_files`

```python
snap_path = os.path.join(ledger.state_dir, "capture-files-%s.json" % bot)
...
if snap.get("path") not in (None, path):
    baseline_reason = "path changed"
```

- There is one snapshot per Bot. A run with another directory replaces it with a baseline of that directory.
- The next ordinary run sees "path changed" again and takes another baseline. Every change made since the last ordinary run is dropped.
- Without `--record-baseline` nothing reaches the ledger. The notice goes to the stderr of the command that caused it.
- `capture files` is on the allow-list with any `--path`, so this is reachable by `gblog` arguments alone.

Reproduction:

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger; mkdir -p $T/work $T/other; echo 1 > $T/work/a.txt
gblog capture files --bot ops --path $T/work       # baseline
echo payload > $T/work/payload.sh                  # the change that should be logged
gblog capture files --bot ops --path $T/other      # replaces the snapshot of $T/work
gblog capture files --bot ops --path $T/work       # "(new baseline: path changed)"
gblog export --bot ops | grep -c payload           # today 0; expected 1
```

Fix: keep one snapshot per directory, and never overwrite the snapshot of another path.

```python
legacy = os.path.join(ledger.state_dir, "capture-files-%s.json" % bot)
snap_path, moved_from = legacy, None
held = _read_json(legacy)
if held is not None and held.get("path") not in (None, path):
    # the Bot's snapshot belongs to another directory: leave it alone, one more file per extra path
    tag = hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    snap_path = os.path.join(ledger.state_dir, "capture-files-%s.%s.json" % (bot, tag))
    if not os.path.exists(snap_path):
        moved_from = held.get("path")
```

- When `moved_from` is set, print the existing `notice: capture path changed from … to …` line and use `baseline_reason = "path changed"`. The stdout suffix and the notice text stay as they are.
- The new file name starts with `capture-files-`, so `.gitignore`, `orphan_witnesses`, `_sync_evidence` and the reserved prefix already cover it.
- `test_path_change_rebaselines` passes unchanged: notice on stderr, 0 changes, then `b2.txt` created.
- README step 6: scope the allow rule to `gblog capture files --path /workspace`.

### D4-05 (Medium) — a change can stay pending forever behind `--limit`

Function: `capture_files`

```python
changes.sort(key=lambda c: c[1])
for kind, rel in changes[:limit]:
    ledger.append(...)
```

- Changes are logged in path order. Files that change before every run and sort early take all `limit` slots each time.
- A later path is counted in the overflow note but never named.
- A weekly run over `/workspace` easily exceeds 200 changes, so this does not need an adversary.
- The existing test covers a single burst only.

Reproduction:

```python
import os, tempfile, gblog
base = tempfile.mkdtemp(); work = os.path.join(base, "work"); os.makedirs(work)
L = gblog.Ledger(os.path.join(base, "ledger"))
def put(name, text):
    with open(os.path.join(work, name), "w") as fh: fh.write(text)
for i in range(3): put("a%d.log" % i, "0")
gblog.capture_files(L, "ops", work)
put("zz-payload.sh", "#!/bin/sh\n")
for run in range(1, 6):
    for i in range(3): put("a%d.log" % i, "x" * (run + 1))           # three files change before every run
    print(gblog.capture_files(L, "ops", work, limit=3)["pending"])   # 1 1 1 1 1
print(any(r.get("target") == "zz-payload.sh" for r in L.records("ops")))
# today False; expected True from the second run on
```

Fix: remember what was left over and log it first.

```python
carried = set(p for p in (snap.get("pending") or []) if isinstance(p, str))   # where the snapshot is loaded
...
changes.sort(key=lambda c: (c[1] not in carried, c[1]))
...
def save_snapshot(files=None, pending=()):
    doc = {"path": path, "taken": now_iso(), "files": current if files is None else files,
           "pending": sorted(pending)}
# overflow branch:
save_snapshot(pending_files, [rel for _kind, rel in changes[limit:]])
```

Every pending change is then named within `ceil(pending / limit)` runs. The snapshot is local scratch and is not committed, so the extra key has no compatibility cost.

### D4-06 (Medium) — hook-captured failures are counted as failures, and counted twice

Functions: `analyze` and `compute_metric`

```python
fails = [r for r in recs if r.get("type") == "action" and r.get("outcome") == "fail"]
...
"actions": sum(1 for r in recs if r.get("type") == "action" and r.get("source") != "auto"),
"failed_actions": len(fails),
```

- `actions` excludes auto records; `failed_actions`, rule A and the `fail` metric include them.
- One failed command that the Bot reports and the hook captures makes two records in the same `(action, target)` group. That reaches the rule A threshold of 2.
- Commands that exit non-zero by design (`grep`, `diff`, `test`) become "failed N times" `skill` proposals.
- The "Failed" column can exceed "Actions".

Reproduction:

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger
gblog log --bot ops --action shell --target "make build" --outcome fail --detail "exit 2"
gblog log --bot ops --action shell --target "make build" --outcome fail --source auto   # the hook's record
gblog tune --stdout | grep 'failed 2 times'
# today:    - **[…]** ops — `make` shell failed 2 times
# expected: no proposal (one failure)
```

Fix: count the Bot's own reports only. Auto evidence stays in `coverage`, where an unreported failure appears under "Observed but not reported".

```python
fails = [r for r in recs if r.get("type") == "action" and r.get("outcome") == "fail" and r.get("source") != "auto"]
# compute_metric, kind == "fail": add the same `and r.get("source") != "auto"`
```

Baselines stored in older `tune` records may include auto records, so their first effect row after the change can look better than it is. Say so in the CHANGELOG.

### D4-07 (Medium) — `auto` decisions count as approvals

Functions: `analyze` rule C and `compute_metric`

```python
ok = [r for r in ev if r.get("decision") in ("allowed", "auto")]
...
elif len(ok) >= 3 and not den:        # -> "Add an Auto-review Allow automatically rule"
...
if kind == "approvals":
    return float(sum(1 for r in recs if r.get("type") == "approval" and _norm_title(r.get("target")) == metric.get("key")))
```

- Three `--decision auto` records propose an Allow rule for an action that is already auto-allowed.
- After a real Allow rule is added, the Bot logs `--decision auto` for each request, as the skill tells it to. The `approvals` metric therefore never falls.
- A change that worked is reported as "**not helping**" after 7 days and gets a `revert` proposal.

Reproduction:

```python
import datetime as dt, os, tempfile, gblog
L = gblog.Ledger(os.path.join(tempfile.mkdtemp(), "ledger"))
now = dt.datetime.now(dt.timezone.utc)
ts = lambda d: (now - dt.timedelta(days=d)).replace(hour=9, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
for d in (14, 13, 12):
    L.append("ops", {"type": "approval", "target": "push the ledger", "decision": "allowed", "outcome": "ok", "ts": ts(d)})
key = gblog._norm_title("push the ledger")
L.append("ops", {"type": "tune", "target": gblog._proposal_id("ops", "approval", "allow:" + key), "outcome": "ok",
                 "detail": "added an Allow automatically rule", "ts": ts(11),
                 "proposal": {"title": "allow rule", "category": "approval", "key": "allow:" + key, "baseline": 3.0,
                              "metric": {"kind": "approvals", "bot": "ops", "key": key}, "better": "lower",
                              "window": {"since": ts(39), "until": ts(11)}, "count": 3}})
for d in range(10, 0, -1):            # the rule works: every later request is decided automatically
    L.append("ops", {"type": "approval", "target": "push the ledger", "decision": "auto", "outcome": "ok", "ts": ts(d)})
recs = L.records("ops")
a = gblog.analyze(recs, 28, gblog.coverage(recs))
print(a["effects"][0]["improved"], [p["title"] for p in a["proposals"] if p["category"] == "revert"])
# today:    False ['Change … did not help after 11 days']
# expected: True []
```

Fix:

```python
allowed = [r for r in ev if r.get("decision") == "allowed"]       # a person had to answer
if len(den) >= 2 and len(den) >= len(ok): ...                      # unchanged
elif len(allowed) >= 3 and not den:
    add(..., allowed, {"kind": "approvals", "bot": bot, "key": key}, "lower")
# compute_metric, kind == "approvals": add `and r.get("decision") != "auto"`
```

### D4-08 (Medium) — a reverted change stays "already applied"

Function: `analyze`

```python
decisions[str(r["target"])] = r                       # latest decision per proposal id
...
add(r.get("bot", "?"), "revert", "revert:" + str(r.get("target")), ...)
```

- `gblog tune --apply <revert id>` records a decision for the *revert* proposal only.
- The original proposal keeps its `ok` decision. It stays under "already applied", so the problem it addressed is never proposed again.
- Its effect row keeps saying "not helping" for 90 days.
- The same happens for apply-then-reject of one id. Two applies of one id give two effect rows.

Reproduction (continues the D4-07 pattern on a fresh ledger):

```python
for d in (20, 19, 18):
    L.append("ops", {"type": "action", "action": "connector", "target": "crm.sync", "outcome": "fail", "ts": ts(d)})
P = gblog._proposal_id("ops", "skill", "fail:connector:crm.sync")
L.append("ops", {"type": "tune", "target": P, "outcome": "ok", "detail": "added a retry", "ts": ts(15),
                 "proposal": {"title": "crm.sync", "category": "skill", "key": "fail:connector:crm.sync", "baseline": 3.0,
                              "metric": {"kind": "fail", "bot": "ops", "action": "connector", "key": "crm.sync"},
                              "better": "lower", "window": {"since": ts(43), "until": ts(15)}, "count": 3}})
for d in range(14, 0, -1):
    L.append("ops", {"type": "action", "action": "connector", "target": "crm.sync", "outcome": "fail", "ts": ts(d)})
recs = L.records("ops"); a = gblog.analyze(recs, 28, gblog.coverage(recs))
R = [p for p in a["proposals"] if p["category"] == "revert"][0]
L.append("ops", {"type": "tune", "target": R["id"], "outcome": "ok", "detail": "reverted",        # `tune --apply R`
                 "proposal": {"title": R["title"], "category": "revert", "key": R["key"]}})
recs = L.records("ops"); a = gblog.analyze(recs, 28, gblog.coverage(recs))
print([(p["category"], p["status"]) for p in a["proposals"] if p["id"] in (P, R["id"])], [e["id"] for e in a["effects"]])
# today:    [('skill', 'applied'), ('revert', 'applied')] [P]
# expected: [('skill', 'new'), ('revert', 'applied')] []
```

Fix, in `analyze` after the decisions loop:

```python
undone = {}                                   # original proposal id -> the applied revert of that change
for r in records:
    snap = r.get("proposal")
    if (r.get("type") == "tune" and r.get("outcome") == "ok" and isinstance(snap, dict)
            and snap.get("category") == "revert" and str(snap.get("key") or "").startswith("revert:")):
        undone[str(snap["key"])[7:]] = r
for pid, rev in undone.items():
    d = decisions.get(pid)
    if d is not None and d.get("outcome") == "ok" and _precedes(d, rev):
        del decisions[pid]                    # the change was undone: its proposal is open again
```

In section J, measure only the decision that currently stands:

```python
if decisions.get(str(r.get("target"))) is not r:
    continue                                  # superseded by a later decision, or reverted
```

`test_analyze_without_a_bound_keeps_every_effect` still gives two rows, because its two records have different targets.

### D4-09 (Medium) — every Bot that did not act gets a `logging` proposal

Function: `coverage` matches only within one Bot: `cand = (a.get("bot"), a.get("action"), want)`.

- The weekly routine runs `capture browser` and `capture files` "for every Bot". All Bots share one browser profile and one `/workspace`.
- Each visit therefore becomes one auto record per Bot. Only the Bot that made the visit has a self-report.
- The README states the attribution limit, but rule I still turns it into a `logging` proposal for every other Bot.

Reproduction:

```python
import gblog
visit = {"type": "action", "action": "browser", "target": "https://github.com/x/y", "source": "auto",
         "capture": "browser", "observed": "2026-01-01T10:01:00Z"}
recs = [{"type": "action", "action": "browser", "bot": "alice", "target": "https://github.com/x/y",
         "ts": "2026-01-01T10:00:00Z"},                           # alice did it and said so
        dict(visit, bot="alice", ts="2026-01-05T09:00:00Z"),
        dict(visit, bot="bob", ts="2026-01-05T09:00:05Z")]        # the same visit, imported for bob
print([(r["bot"], r["pct"]) for r in gblog.coverage(recs)["rows"]])
# today:    [('alice', 100.0), ('bob', 0.0)]
# expected: bob has no observed browser action of his own
```

Fix, in `coverage`: a captured record that another Bot reported is that Bot's activity, not an unreported action.

```python
shared = {}       # in the indexing loop, after times.append(...):
shared.setdefault((s.get("action"), s_target), []).append((times[-1], s.get("bot")))
...
kept, elsewhere = [], 0                                   # after the matching loop
for a in unmatched:
    t_a = _ts_epoch(a.get("observed") or a.get("ts"))
    want = norm_target(a.get("action"), a.get("target"))
    if a.get("capture") in ("browser", "files") and any(
            b != a.get("bot") and abs(t - t_a) <= window for t, b in shared.get((a.get("action"), want), ())):
        by_key[(a.get("bot", "?"), a.get("action") or "other")]["auto"] -= 1
        elsewhere += 1                                    # shell-hook records name their Bot and are left alone
    else:
        kept.append(a)
unmatched = kept
```

- Skip rows whose `auto` is now 0.
- Return the additive key `elsewhere`.
- Add one sentence to `render_coverage`: "N observed actions were reported by another Bot on this computer".

### D4-10 (Medium) — the coverage window cuts off the self-reports it needs

Functions: `_main` (`coverage`, `tune`) and `compute_metric`

```python
cov = coverage(ledger.canonicalize(ledger.records(..., since=since)), args.window)   # coverage
cov = coverage([r for r in recs if r.get("ts", "") >= since])                         # tune
```

- A browser record's `ts` is its import time. Its `observed` time can be much older after a missed run or a first import with a large `--hours`.
- The auto record is inside the window by `ts`. The self-report it would match is outside it.
- The same data gives 0% or 100% depending on `--days`.

Reproduction:

```python
import datetime as dt, os, tempfile, gblog
root = os.path.join(tempfile.mkdtemp(), "ledger"); L = gblog.Ledger(root)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
ten = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
L.append("ops", {"type": "action", "action": "browser", "target": "https://github.com/x/y", "ts": iso(ten)})
L.append("ops", {"type": "action", "action": "browser", "target": "https://github.com/x/y", "source": "auto",
                 "capture": "browser", "observed": iso(ten + dt.timedelta(minutes=1))})    # imported today
# gblog --root <root> coverage --bot ops --days 7    -> Overall: **0.0%** of 1 …
# gblog --root <root> coverage --bot ops --days 30   -> Overall: **100.0%** of 1 …
```

Fix: the window selects which *auto* records are counted. Self-reports may be older.

```python
def coverage(records, window_minutes=30, auto_since=None):
    auto = [r for r in records if ... and (not auto_since or str(r.get("ts") or "") >= auto_since)]
    # self_only: count only self-reports with ts >= auto_since

# _main, coverage:
recs = ledger.records(bot, since=since)
seen = [str(r["observed"])[:10] for r in recs if r.get("source") == "auto" and _is_norm_ts(r.get("observed"))]
if seen and min(seen) < since:
    recs = ledger.records(bot, since=min(seen))
cov = coverage(ledger.canonicalize(recs), args.window, auto_since=since)

# _main, tune (recs already reach back at least 90 days):
cov = coverage(recs, auto_since=since)
```

`compute_metric(kind="coverage")` gets the same treatment: pass the Bot's records up to `until` and `auto_since=since`.

### D4-11 (Low-Medium) — `capture browser` creates state for a chain that does not exist

Function: `capture_browser` → `save_markers` → `Ledger.update_state`

- The marker is saved even when no row was imported.
- For a name with no chain, `update_state` writes `state/<name>.json` with `seq 0, head genesis`.
- `chains_on_disk` then treats the name as a chain, which contradicts the README ("It is never `OK … 0 records`").
- The reserved-name check is in `append` only, so `--bot aliases` writes `state/aliases.json` through `update_state`.

Reproduction:

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger
python3 - "$T/History" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
c.commit()
PY
gblog capture browser --bot opps --history "$T/History"   # a typo of "ops": 0 new visit(s)
gblog verify --bot opps
# today:    OK    opps    0 records  head genesis   (exit 0)
# expected: FAIL … no chain on disk for 'opps'      (exit 1)
gblog capture browser --bot aliases --history "$T/History"; ls "$T/ledger/state"   # aliases.json was created
```

Fix:

1. `capture_browser`: call `ledger._check_reserved(bot)` right after `bot = safe_name(bot)`.
2. `Ledger.chains_on_disk`: a state file that only says "genesis" names no chain.

   ```python
   def on_disk(c):
       if os.path.isdir(os.path.join(self.logs_dir, c)):
           return True
       if not os.path.exists(self._state_path(c)):
           return False
       state = self._load_state(c)
       return state is None or state.get("head", GENESIS) != GENESIS
   return [c for c in self.chains_for(name) if on_disk(c)]
   ```

   The marker is still stored, so the first-import window is not lost. Only `verify --bot` and `anchor --bot` change. I found no test that relies on the old answer, but run the full suite, because `anchor --bot` shares this helper.

### D4-12 (Low-Medium) — `--history` spelling and non-database files

Function: `capture_browser`

```python
paths = history_paths or find_browser_histories()
...
marker = markers.get(path)
...
shutil.copyfile(path, copy)
```

(a) The marker key is the argument as typed. Auto-detected paths go through `realpath`; explicit ones do not. The same file under another spelling is imported again.

```bash
gblog capture browser --bot ops --history "$T/chrome/Default/History"             # 1 new visit(s)
( cd "$T/chrome" && gblog capture browser --bot ops --history Default/History )   # 1 new visit(s): the same visit again
```

(b) Any readable path is copied whole into a temporary directory before SQLite looks at it. `--history /dev/zero` copies until the disk is full; the copy is then removed. Other appends fail with `ENOSPC` meanwhile. The command is auto-allowed.

Fix:

```python
given = {os.path.realpath(os.path.expanduser(p)): p for p in (history_paths or [])}
paths = list(given) if history_paths else find_browser_histories()
...
marker = markers.get(path)
if marker is None and path in given:
    marker = markers.get(given[path])            # a marker stored under the older, literal spelling
...
try:                                             # before mkdtemp
    with open(path, "rb") as fh:
        is_db = fh.read(16) == b"SQLite format 3\x00"
except OSError:
    is_db = False
if not os.path.isfile(path) or not is_db:        # isfile is evaluated first: a FIFO is never opened
    failed.append(path)
    continue
```

Write the last condition as two statements so that `open` is not reached for a non-regular file.

### D4-13 (Low-Medium) — caps count proposals that are already decided

Function: `analyze`, rules E and G

```python
if any(p["bot"] == bot and p["category"] == "skill" and p["key"].startswith("seq:") for p in proposals):
    break
...
for r in lessons[-5:]:
```

- Rule E allows one sequence proposal per Bot. An applied one still occupies the slot.
- Once a sequence is written down as a skill it recurs more often, so it stays on top and no other sequence is ever proposed.
- Rule G considers the five newest lesson notes, decided or not. A sixth undecided note is never shown.

Reproduction: seven closed tasks, four with steps X (`shell git pull`, `shell make build`, `file dist/app.tar`) and three with steps Y (`browser https://crm.example/leads`, `connector sheets.append`, `message sales-channel`). `analyze` returns one `seq:` proposal (X). Append `{"type": "tune", "target": <its id>, "outcome": "ok"}` and analyse again. Today the `seq:` proposals are `[X (applied)]`; expected `[X (applied), Y (new)]`.

Fix:

```python
open_seq = lambda p: (p["bot"] == bot and p["category"] == "skill" and p["key"].startswith("seq:")
                      and p["status"] != "applied" and not p["hidden"])
if any(open_seq(p) for p in proposals):
    break
...
undecided = 0
for r in reversed(lessons):
    pid = _proposal_id(bot, "memory", "note:%s" % str(r.get("hash") or "")[:12])
    add(bot, "memory", ...)                      # unchanged arguments
    undecided += pid not in decisions
    if undecided >= 5:
        break
```

### D4-14 (Low) — the hook hides the exit status from later `PROMPT_COMMAND` entries, and `sed` edits continuation lines

Function: `SHELL_HOOK`

(a) The hook is prepended to `PROMPT_COMMAND` and always returns 0. A pre-existing entry that reads `$?` (a common coloured-prompt pattern) sees success after every command.

```bash
gblog capture shell-hook --bot ops > "$T/hook.sh"
bash --noprofile --norc -c 'source "$1"; PROMPT_COMMAND="__gblog_hook;echo \"next entry sees: \$?\""; (exit 7); eval "$PROMPT_COMMAND"' _ "$T/hook.sh"
# today: next entry sees: 0      expected: 7
```

Fix: replace all four `return 0` with `return "$rc"` (the early return, the plain-gblog arm, `command -v gblog … || return "$rc"`, and the last line).

(b) `sed 's/^ *[0-9]* *//'` runs on every line of a multi-line entry (here-documents, `lithist`). A body line `  7z x a.zip` is logged as `z x a.zip`. Fix: `sed '1s/^ *[0-9]*\** *//'`. That is first line only, and it also drops the `*` bash prints for a modified entry.

### D4-15 (Low) — a symlinked `--path` makes file capture log the ledger itself

Function: `capture_files`

```python
path = os.path.abspath(os.path.expanduser(path))
...
if _is_within(dirpath, ledger.root):
```

- Neither side is resolved. If `--path` reaches the workspace through a symlink, the ledger directory is not recognised.
- Every run then logs the files the previous run wrote (`logs/…jsonl`, `state/….json`, the snapshot itself), forever.
- The same spelling difference also triggers a "path changed" baseline.

Reproduction:

```bash
T=$(mktemp -d); mkdir -p $T/ws; ln -s $T/ws $T/link; echo 1 > $T/ws/a.txt
export GBLOG_ROOT=$T/ws/activity-log
gblog capture files --bot ops --path $T/link     # baseline
gblog capture files --bot ops --path $T/link     # today: 3 change(s), all under activity-log/; expected 0
gblog capture files --bot ops --path $T/link     # today: again changes under activity-log/, on every run
```

Fix:

```python
real_top, real_root = os.path.realpath(path), os.path.realpath(ledger.root)
...
if _is_within(os.path.join(real_top, os.path.relpath(dirpath, path)), real_root):
...
if snap.get("path") is not None and os.path.realpath(str(snap["path"])) != real_top:   # "path changed"
```

`os.walk` does not follow links below the top, so the join is the real path of each directory. The stored `path` and the record targets stay as they are.

### D4-16 (Low) — an excluded file is reported as deleted

Function: `capture_files`

```python
for rel in previous:
    if rel not in current:
        changes.append(("deleted", rel))
```

A file that is in the snapshot and is now skipped (a new `--exclude`, or an unreadable `stat`) is logged as `deleted` although it exists. Dropping the flag later logs it as `created`.

```bash
echo 1 > $T/work/keep.txt; gblog capture files --bot ops --path $T/work
gblog capture files --bot ops --path $T/work --exclude keep.txt
gblog tail --bot ops -n 1        # today: … action file keep.txt - deleted
```

Fix:

```python
for rel in previous:
    if rel not in current:
        if os.path.lexists(os.path.join(path, rel)):
            current[rel] = previous[rel]     # still on disk, only not looked at this time: keep what was known
        else:
            changes.append(("deleted", rel))
```

### D4-17 (Low) — a failure part-way through logs the earlier changes twice

Function: `capture_files` saves the snapshot only after the whole loop. If the third of four appends fails (`ENOSPC`, Ctrl-C, a routine timeout), two records are in the ledger and the snapshot is unchanged. The next run logs all four again. `capture_browser` already saves its progress in this case.

Reproduction: patch `gblog.Ledger.append` to raise `OSError(28, …)` on its third call, run `capture_files` with four new files, then run it again without the patch. `a.txt` and `b.txt` are in the chain twice.

Fix (it also removes the inline loop of the overflow branch):

```python
def advanced(done):                           # the snapshot once the first `done` changes are logged
    files = dict(previous)
    for kind, rel in changes[:done]:
        if kind == "deleted":
            files.pop(rel, None)
        else:
            files[rel] = current[rel]
    return files

done = 0
try:
    for kind, rel in changes[:limit]:
        ledger.append(...)
        done += 1
except BaseException:
    if done:
        try:
            save_snapshot(advanced(done))
        except OSError:
            pass
    raise
```

`advanced(len(changes))` equals `current`, so the normal path can call `save_snapshot(advanced(min(limit, len(changes))))` in both branches. `test_snapshot_kept_when_append_refused` is unaffected, because `done` is 0 there.

### D4-18 (Low) — the documented task commands lower shell coverage

Function: `coverage` leaves out only `_is_plain_gblog` targets. The hook logs these, and coverage counts them as unreported:

- `TID=$(gblog task-start …) && export GBLOG_TASK="$TID" && echo "task $TID"` (agent-integration §3.1);
- `export GBLOG_TASK="$TID"` and `unset GBLOG_TASK` (skill step 1, §3.4);
- `GBLOG_ROOT=/path ./bin/gblog status` and `"$HOME/.local/bin/gblog" …` (README, skill).

Separately, the test runs on the *redacted* target. `echo token=aaaaaa;id>/tmp/x` is stored as `echo token=[REDACTED]`, and `gblog note --detail token=aaaaaa;id>/tmp/x` is stored as a plain `gblog` call. Coverage then ignores the latter.

Reproduction:

```python
import gblog
cmds = ['TID=$(gblog task-start --bot ops --task x) && export GBLOG_TASK="$TID" && echo "task $TID"',
        'export GBLOG_TASK="$TID"', 'unset GBLOG_TASK', 'GBLOG_ROOT=/tmp/x ./bin/gblog status']
recs = [{"type": "action", "action": "shell", "bot": "ops", "source": "auto", "target": t,
         "ts": "2026-01-01T10:00:0%dZ" % i} for i, t in enumerate(cmds)]
print(gblog.coverage(recs)["auto"])      # today 4; expected 0
```

Fix: a second predicate used by `coverage` only. `_is_plain_gblog` and its test stay as they are, apart from one added line: `if "[REDACTED]" in cmd: return False`.

```python
_GBLOG_ENV_RE = re.compile(r"(?:export[ \t]+GBLOG_(?:TASK|BOT|ROOT)=\S*|unset[ \t]+GBLOG_(?:TASK|BOT))\Z")
_ECHO_RE = re.compile(r"echo(?:[ \t]|\Z)")

def _is_ledger_bookkeeping(cmd):
    """coverage only: an `&&` list of gblog calls and GBLOG_* exports or unsets (echo allowed beside them)."""
    if not isinstance(cmd, str) or "[REDACTED]" in cmd:
        return False
    real = 0
    for part in cmd.split("&&"):
        part = part.strip()
        inner = _GBLOG_ASSIGN_RE.match(part)
        if inner:
            part = inner.group(1).strip()
        if not part or _GBLOG_CARRIER_RE.search(part):
            return False
        if _shell_name(part) == "gblog" or _GBLOG_ENV_RE.match(part):
            real += 1
        elif not _ECHO_RE.match(part):
            return False
    return real > 0
```

In `coverage`, use `_is_ledger_bookkeeping(r.get("target"))`. It accepts everything `_is_plain_gblog` accepts.

SECURITY.md ("Capture completeness") should also say that redaction of an unquoted value runs to the next white space. A command glued to a secret-looking argument without spaces is replaced by `[REDACTED]` in the captured target.

### D4-19 (Low) — `_url_target` drops the port

Function: `_url_target`

```python
host = parts.hostname or parts.netloc
return "%s://%s%s" % (parts.scheme, host, parts.path or "/")
```

- `http://localhost:3000/` and `http://localhost:8080/` are both stored as `http://localhost/`.
- The consecutive-duplicate filter (`target != last_target`) then drops the second visit.
- `http://[::1]:3000/x` is stored as `http://::1/x`.

Reproduction: `gblog._url_target("http://localhost:8080/admin")` returns `'http://localhost/admin'`.

Fix:

```python
host = parts.hostname or parts.netloc
if ":" in host:
    host = "[%s]" % host
try:
    port = parts.port
except ValueError:
    return None
return "%s://%s%s%s" % (parts.scheme, host, "" if port is None else ":%d" % port, parts.path or "/")
```

- User info is still dropped. Coverage matching is unchanged, because `norm_target` uses the host name only.
- README and DESIGN: "scheme://host[:port]/path".

### D4-20 (Low) — decisions and effect metrics are lost after `alias add`

Function: `analyze`. A proposal id is `sha1(bot|category|key)`, and a stored metric names the Bot as it was called at decision time.

- After `gblog alias add --slug chief-of-staff --alias cos` (the README's upgrade step), regenerated proposals carry the slug and get new ids.
- A rejection made under `cos` is forgotten and the proposal is shown as new.
- Effect rows for older applied changes are computed over an empty bucket (`records_by_bot.get("cos")`) and read "improving".

Reproduction: three failures in chain `cos`; `gblog tune`; `gblog tune --reject <id>`; `gblog alias add --slug chief-of-staff --alias cos`; `gblog tune --stdout`. The same failure is listed with a new id and an Apply/Reject line.

Fix:

```python
by_triple = {}                                # in the decisions loop, next to decisions[...] = r
snap = r.get("proposal")
if isinstance(snap, dict) and isinstance(r.get("bot"), str):
    by_triple[(r["bot"], snap.get("category"), snap.get("key"))] = decisions[str(r["target"])]
...
decision = decisions.get(pid) or by_triple.get((bot, category, key))       # in add()
...
metric = dict(metric, bot=r.get("bot"))       # section J: the tune record lives on the proposal's chain
```

The CLI canonicalises `r["bot"]`, so both lookups follow the alias.

### D4-21 (Low) — operator commands typed at a prompt leave no auto record

Function: `SHELL_HOOK`, arm `gblog|gblog\ *|__gblog*) return 0;;`.

- The skip rule exists so that a Bot's own reports are not logged twice.
- It also skips `gblog alias add … --move`, `gblog seal`, `gblog sync --remote … --replace-remote`, `gblog state rebuild`, `gblog spool replay` and `gblog capture files --path …`.
- `alias` writes no ledger record at all, so a registry change made at a prompt is visible nowhere.
- Since W-16, `coverage` ignores plain `gblog` records, so logging these commands costs no coverage.

Reproduction: with the test harness of `test_shell_hook_logs_commands_that_mention_gblog`, `logged("gblog alias add --slug decoy --alias ops --move")` is empty.

Fix:

```bash
  case "$cmd" in
    *";"*|*"&"*|*"|"*|*"\$("*|*'`'*|*"<"*|*">"*|*$'\n'*) ;;
    gblog|__gblog*) return "$rc";;
    gblog\ alias*|gblog\ seal*|gblog\ sync*|gblog\ state*|gblog\ spool*|gblog\ capture*|gblog\ -*) ;;
    gblog\ *) return "$rc";;
  esac
```

- `gblog\ -*` covers a leading global option such as `--root`, where the subcommand is not in a fixed position.
- Update the README and FAQ 27 sentence to "a plain reporting or read-only `gblog` command is skipped".

### D4-22 (Low) — an old applied change is neither measured nor proposed again

Functions: `analyze` (`effects_since`) and `add`. Since W-11 a change applied before the loaded range has no effect row and no `revert` proposal. Its decision still counts, so the proposal sits under "already applied" for good, even while its evidence keeps accumulating.

Reproduction: the D4-08 ledger with the `tune` record at 100 days and failures on each of the last 10 days. `gblog tune --stdout` lists the failure under "already applied", and the effect table is empty.

Fix, in `add()`, for measurable proposals only (sequence, memory and revert proposals have no metric and stay applied):

```python
expired = bool(status == "applied" and metric and effects_since and str(status_ts or "") < effects_since)
# proposals.append({..., "expired": expired})
```

In `render_tune`:

- `applied = [p for p in … if p.get("status") == "applied" and not p.get("expired")]`.
- For an expired entry that is shown, add the line `Applied on <date>; listed again because the change is too old to be measured and its evidence is still in the window.`
- The JSON `status` stays `applied`.

### D4-23 (Low) — small edge cases

| # | Where | Problem | Fix |
|---|---|---|---|
| a | `_validate_args`, `coverage --window` | `--window=-5` matches nothing and prints 0% | `if args.cmd == "coverage" and args.window < 1: raise _UsageError("--window must be 1 or more")` |
| b | `analyze` J, coverage metric | `after is None` (no observation since the change) gives `improved = False`, then "not helping" and a revert | `improved = None if after is None or before is None or p.get("better") != "higher" else after > before` |
| c | `analyze` metrics | A task that started before the window and ended inside it counts in `closed` and `succeeded` but not in `tasks` ("Closed" > "Tasks") | Require `t["start"]` in both sums |
| d | `coverage`, auto loop | `norm_target` and `by_key.setdefault` are outside the `try` that protects the self-report loop; a raw record with a list `target` raises `AttributeError` (library callers only) | Wrap the per-record body in the same `except (AttributeError, TypeError): continue` |
| e | `coverage`, `self_recs` | An `archive` record (`action: "file"`, `target` = file name) is not a self-report, so an archived file still counts as unreported | `r.get("type") in ("action", "archive")` |
| f | `render_tune`, effect table | Coverage percentages are printed under "Before/day" and "After/day" | Name the columns "Before" and "After" and add `%` for coverage rows |

### D4-24 — documentation that contradicts the code

| File | Statement | Code |
|---|---|---|
| CHANGELOG, "Known open findings" | T-04/W-10: the hook logs `gblog log --bot "$BOT" …` | Hook v2 skips it (`*"\$("*`). The same file says so under "Behaviour changes". |
| CHANGELOG, "Known open findings" and the `capture files` bullet | No `--record-baseline`; stdout unchanged | The flag exists. Stdout ends with ` (new baseline: <reason>)` and `, <n> pending`. |
| README, capture section | "A hook installed from this tree also logs a `gblog` command that contains a shell variable" | It does not. |
| README, capture section | "The stdout line still starts with `files: 0 change(s)`, so read stderr" | True, but the suffix and `--record-baseline` are not mentioned. FAQ 29 has them. A first run prints the suffix and no notice. |
| README, "Upgrading" table | No row for `capture files` | `capture files --r` is ambiguous (`--root`, `--record-baseline`). The shortest working forms are `--rec` and `--roo`. |
| `docs/routine-weekly-tune.md` | "a weekly run misses nothing" | The first import of a history file is bounded by `--hours` (default 24). Use `gblog capture browser --bot <slug> --hours 168`; the marker wins afterwards. |
| README, tune table | "approved ≥ 3×", "failing ≥ 2×" | Both currently count auto records (D4-06, D4-07). |
| DESIGN §6 | `observed`: "browser only" | Becomes "browser: visit time; files: mtime" with D4-01. |
| SECURITY, "Capture completeness" | — | Add: file capture compares size and mtime only, so a file rewritten with the same size and a restored mtime is not seen. |

## 3. Simplifications that do not change behaviour

- **S-1** `from urllib.parse import urlsplit` once at module level. Five functions import it locally (`_remote_host_path`, `_ssh_alias_target`, `_has_url_password`, `_url_target`, `norm_target`).
- **S-2** `tune` branch: `render_tune(analysis, …)` is called twice with the same arguments (once to print, once for the file). Build the text once.
- **S-3** `capture_files`: the `advanced()` helper from D4-17 replaces the inline `pending` loop and the two `save_snapshot` branches.
- **S-4** `analyze`: `by_bot` is `records_by_bot` filtered by window and type. Build `records_by_bot` first and derive `by_bot` from it; this removes one pass.
- **S-5** `capture_browser`: the two "unusable marker" notices share one message. A small `_usable_marker(value, label)` returning an int or `None` removes one `try` block.

## 4. Test cases to add

`tests/test_capture_sync.py`
- `test_file_records_carry_mtime_as_observed`: `created` and `modified` have `observed`; `deleted` has `observed_from`; a future mtime gives no `observed`.
- `test_file_coverage_matches_report_made_at_mtime` (the D4-01 reproduction): 100%.
- `test_hours_below_one_is_a_usage_error`: exit 2, no state written.
- `test_future_marker_is_ignored_with_a_notice`.
- `test_other_path_does_not_replace_the_snapshot` (the D4-04 reproduction): `payload.sh` is logged by the third run.
- `test_pending_changes_are_logged_before_new_ones` (the D4-05 reproduction).
- `test_symlinked_path_does_not_scan_the_ledger`, `test_symlink_spelling_is_not_a_path_change`.
- `test_excluded_file_is_not_logged_as_deleted`: un-excluding later logs nothing.
- `test_partial_failure_does_not_log_twice`: mock the third append.
- `test_history_path_spelling_does_not_reimport`: relative, absolute and symlinked spelling.
- `test_non_sqlite_history_is_not_copied`: wrap `shutil.copyfile`; a text file and a directory are `failed` without a copy.
- `test_capture_browser_unknown_bot_does_not_make_a_chain`: `verify --bot` exits 1; reserved name exits 2 and writes nothing.
- `test_url_target_keeps_port_and_brackets`: two ports are two records.

`tests/test_security_regressions.py` (bash harness)
- `test_hook_primes_on_first_interactive_prompt`: run `bash -i` with a seeded `HISTFILE`; no record. The non-interactive harness still logs.
- `test_hook_returns_the_status_it_saw`: `(exit 7)` followed by a second `PROMPT_COMMAND` entry sees 7.
- `test_hook_keeps_digits_on_continuation_lines`.
- `test_hook_logs_operator_gblog_commands`: `gblog alias add …` and `gblog --root X seal …` are logged; `gblog log …` and `gblog status` are skipped.

`tests/test_tune_loop.py`
- `test_hook_duplicate_of_a_reported_failure_counts_once`; `test_auto_failures_do_not_make_skill_proposals`.
- `test_auto_decisions_do_not_propose_an_allow_rule`; `test_allow_rule_effect_improves_when_decisions_become_auto` (the D4-07 reproduction).
- `test_applied_revert_reopens_the_original` (the D4-08 reproduction); `test_apply_then_reject_has_no_effect_row`; `test_double_apply_has_one_effect_row`.
- `test_applied_sequence_does_not_block_the_next_one`; `test_sixth_undecided_lesson_is_proposed`.
- `test_decision_survives_alias_add`; `test_effect_uses_the_canonical_bot`.
- `test_old_applied_change_with_fresh_evidence_is_listed_again`: metric proposals only; a sequence proposal stays applied.
- `test_coverage_effect_without_new_observations_is_na`.
- `test_closed_never_exceeds_tasks`.

`tests/test_round3_followups.py` or a new `tests/test_coverage.py`
- `test_other_bots_report_takes_a_shared_capture_out_of_the_count` (the D4-09 reproduction): shell-hook records are unaffected; `elsewhere` is reported.
- `test_coverage_window_keeps_older_self_reports` (the D4-10 reproduction): `--days 7` and `--days 30` agree.
- `test_task_bookkeeping_commands_are_not_unreported` (the D4-18 reproduction); `test_redacted_target_is_never_plain`.
- `test_coverage_window_below_one_is_a_usage_error`.
- `test_archive_record_counts_as_a_file_report`.
- `test_coverage_survives_a_raw_auto_record`: list `target`.

`tests/test_cli_ux.py`
- Extend `test_abbreviations`: `capture browser --hours=0` exits 2; `capture files --rec` still means `--record-baseline`.

Suggested order:

1. D4-01, D4-06, D4-07, D4-09 and D4-10. They remove the false proposals the weekly routine produces today.
2. D4-02, D4-14 and D4-21, as one hook text change.
3. D4-03, D4-04, D4-05, D4-11 and D4-12.
4. The remaining tune and file items.
5. D4-24 after the code it describes.