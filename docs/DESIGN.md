# gblog design

This document describes how `gblog` 0.6 behaves, including the 0.6.1 fixes, and why. The README is the user guide and
[`SECURITY.md`](../SECURITY.md) is the policy. This is the reference for people who change the code
or audit a ledger.

Contents:

1. Threat model
2. Guarantees and non-guarantees
3. On-disk layout
4. Append state machine
5. Locks and lock order
6. Record field reference
7. Verification
8. Anchor files and history
9. The verify-time `break_ack` pin
10. Reserved bot names
11. Exit codes
12. Deliberate non-features

## 1. Threat model

**The adversary is the Bot, or anyone with write access to the ledger directory.**

- The Bot may be careless (forgets to log), confused (follows instructions embedded in a web page
  title), or actively hiding an action.
- It runs as the same operating-system user as `gblog`. It can read, edit, truncate, replace or
  delete every file under the ledger root: `logs/`, `state/`, `anchors/`, `spool/`, `archive/`.
- It can import `gblog` and call `Ledger.append()` directly, bypassing the CLI. Record types, flag
  validation and the future-timestamp bound are CLI conveniences, not security boundaries.
- It can set environment variables (`GBLOG_ROOT`, `TMPDIR`, `GBLOG_TASK`) and may be able to change
  the clock.

**Outside the adversary's reach, by assumption:**

- the conversation transcript, where the daily routine posts chain heads;
- a git remote that rejects force-pushes and that the Bot's token cannot administer.

**Goal.** Tamper *evidence*, not tamper *prevention*. gblog cannot stop a same-user process from
changing a file. It makes the change show up in `gblog verify`.

## 2. Guarantees and non-guarantees

### Guaranteed

`gblog verify` reports FAIL (exit 1) for:

| Change | Detected by |
|---|---|
| A record edited in place | `hash` no longer equals SHA-256 of the canonical JSON |
| A line deleted, inserted, duplicated or re-ordered | `prev` / `seq` link broken |
| A timestamp earlier than its predecessor | monotonic `ts` rule |
| A line that is not a JSON object, not UTF-8, or has wrong field types | corrupt-record rule |
| A `break_ack` that is not the pinned incident (section 9) | verify-time pin |
| An archived file modified, missing, or addressed outside `archive/` | re-hash and path check |
| The tail truncated, the chain emptied, or the chain rewritten, **while a witness survives** | state head mismatch (checked even when the walk finds zero records); anchor containment (section 8); `verify --contains` |
| A whole chain deleted while `anchors/<bot>.json`, `anchors/<bot>.history.jsonl` or `state/<bot>.json` survives | missing-chain check in `verify --all` |
| A directory under `logs/` whose name is not a valid chain key (`logs/my bot`, `logs/ops.`, `logs/---`) | invalid-directory check in `verify --all`. Its records are not verified, and readers ignore the directory |

`gblog verify` prints a warning (a `!` line, exit code unchanged) when it has lost a witness:
`state/<bot>.json` is missing, unreadable or reset to genesis. A warning means "truncation can no
longer be detected from state". It does not mean "nothing happened".

`gblog verify` also warns when the last record of a chain is dated more than 300 s in the future:
`last record is dated <ts>, in the future: new records are stamped with that time (see
clock_regressed)`. Because of the clamp in section 4 (step 4), one future-dated record pins every
later record to that timestamp and to that day file, so daily reports stop showing the chain. The
same condition is reported in two more places:

- `append` prints `warning: last record of <bot> is dated <tail ts> (future); this record is
  stamped with it` on stderr each time it clamps to such a tail;
- `status` prints `CLOCK <chain>: last record dated <ts> (future)` after the `ALERT` lines, and
  `status --json` rows carry `future_tail` (the timestamp, or `null`).

This is a warning, not a FAIL. The chain is intact, and refusing to append would stop logging.

### Not guaranteed

- **Completeness.** Records are self-reported. `coverage` measures the gap against automatically
  captured evidence, but that evidence has blind spots of its own: the shell hook is interactive
  bash only, and on a shared computer browser and file activity cannot be attributed to one Bot.
- **Authorship.** There are no signatures. Any writer can append a hash-valid record under any Bot
  name, including `verify`, `sync`, `tune` and `note` records. A `verify ok` record is a claim.
  Rerun `gblog verify` to know.
- **A full rewrite with no witness.** Someone who rewrites the log, `state/`, every file under
  `anchors/` and the remote, when nobody kept a head elsewhere, produces a ledger that verifies.
  Off-box anchors exist to close this.
- **Timestamps.** `ts` is the writer's clock or an explicit `--ts`. It is ordered, not attested.
- **Redaction.** Pattern matching only. See `SECURITY.md` for covered and uncovered shapes.
- **Approval authenticity.** `jay_approval_id` is a recorded reference, not authentication.
- **Temporary-copy ledgers.** A ledger whose root is under the system temporary directory is treated
  as a test copy for the purposes of section 9: the committed, fabricated fixture ack is honoured
  there (with a `!` warning). Never keep a production ledger there.

## 3. On-disk layout

```
logs/<bot>/YYYY-MM-DD.jsonl        one record per line, hash-chained per bot, UTC day files
state/<bot>.json                   chain head (seq, head, updated) + last_sync + capture markers
state/<bot>.lock                   flock target for that chain
state/<bot>.alert.json             latest tail-mismatch refusal (page on this file)
state/<bot>.alert.resolved-<stamp>.json   alert renamed after a complete spool replay
state/capture-files-<bot>.json     file-capture snapshot (not committed; see .gitignore)
state/aliases.json, aliases.lock   alias registry and its lock
spool/<bot>/<stamp>-<id>.json      event preserved when an append was refused
spool/<bot>/replayed/              spool files after a successful replay
anchors/<bot>.json                 latest anchor of that chain
anchors/<bot>.history.jsonl        append-only anchor history
anchors/days/YYYY-MM-DD.json       per-day rollup of all chain anchors
archive/<bot>/<date>/<sha256>.gz   content-addressed evidence files
reports/, tuning/                  rendered Markdown (derived data, not evidence)
```

**Hash rule (schema `v: 1`, frozen).** `hash` is the SHA-256 hex digest of the record without its
`hash` key, serialised with `sort_keys=True`, separators `(",", ":")`, `ensure_ascii=False`, encoded
as UTF-8. The line on disk is `json.dumps(record, ensure_ascii=False, sort_keys=True)` plus `"\n"`.
`prev` is the previous record's `hash`, or the string `genesis` for the first record.

New files are created `0o600` and new directories `0o700`. Existing permissions are left alone.

## 4. Append state machine

`Ledger.append(bot, record)` is the only code path that writes under `logs/`. It never rewrites
earlier bytes.

```
           +--------------------------------------------------------------+
           | 0. name check (safe_name, reserved names), ensure() the tree |
           +--------------------------------------------------------------+
                                       |
                         1. LOCK_EX on state/<bot>.lock
                                       |
                         2. decide the expected (seq, head)
                                       |
          +----------------+-----------+------------+------------------+
          |                |                        |                  |
     tail == state   tail is one ahead        any other mismatch   no state file
          |          of state (roll-forward)        |                  |
          |          adopt tail, notice             |           adopt a consistent
          |                |                        |           tail, or genesis
          |                |                 3. REFUSE: spool +        |
          +--------+-------+                    alert, raise           |
                   |                            (CLI exit 4)           |
                   +---------------------------+-----------------------+
                                               |
                         4. timestamp rule (normalise, monotonic)
                                               |
                         5. build record: seq+1, prev=head, redact, hash
                                               |
                         6. LINE: O_APPEND write, fsync file, fsync directory
                                               |
                         7. STATE: tmp file, fsync, os.replace, fsync directory
                                               |
                         8. unlock
```

### Step 2 — expected head and the tail check

"Tail" is the last non-empty line of the newest day file. It is found by reading backwards in
64 KiB blocks, so the cost per append does not grow with the file. A tail is *self-consistent* when
it is a UTF-8 JSON object whose `hash` equals the hash of its body.

| State file | Log files | Tail versus state | Result |
|---|---|---|---|
| missing or unreadable | none | – | start at `(0, genesis)` |
| missing or unreadable | present | tail self-consistent | adopt the tail's `(seq, hash)` |
| missing or unreadable | present | tail corrupt | **refuse** |
| present, head `genesis` | none | – | start at `(0, genesis)` |
| present | none | state is not genesis | **refuse** |
| present | present | `tail.seq == state.seq` and `tail.hash == state.head` | proceed |
| present | present | tail self-consistent, `tail.prev == state.head`, `tail.seq == state.seq + 1` | **roll forward** |
| present | present | anything else (state ahead, state far behind, tail corrupt) | **refuse** |

"Unreadable" includes a state file that parses but is not a JSON object. In the first column,
"present" means the file exists and parses to an object.

A state file that exists but cannot be read follows exactly the same rule as a missing one, in
`append` and in `update_state`. When a tail is adopted this way, the command prints one of these
lines on stderr:

- `notice: state/<bot>.json was missing; adopted the log tail (seq <n>)`
- `notice: state/<bot>.json was unreadable; adopted the log tail (seq <n>)`

`verify` reports the same condition as a `!` warning.

**Roll-forward rule.** Step 6 makes the line durable before step 7 replaces state. A process killed
between the two, or an `ENOSPC` while writing the state file, leaves state exactly one record behind
a valid tail whose `prev` is the state head. That is the only mismatch a crash inside `append` can
produce, so it is the only one repaired automatically. The append adopts the tail, prints
`notice: recovered state/<bot>.json after an interrupted append` on stderr and continues. State
*ahead* of the log cannot come from a crash inside `append`. It means the log was replaced or
truncated outside the lock, and it is always refused.

### Step 3 — refusal

A refusal writes nothing under `logs/` and does not touch `state/<bot>.json`. It:

1. writes the event (already redacted) to `spool/<bot>/<UTC stamp>-<8 hex>.json` with
   `kind: "tail_mismatch"`, the expected seq and head, and the observed tail;
2. replaces `state/<bot>.alert.json` with the same payload;
3. raises `TailMismatchError`. The CLI prints `ALERT tail mismatch: …` on stderr and exits 4.

gblog sends no mail or chat. The exit code and the alert file are the page. `gblog status` repeats
the alert as `ALERT <bot>: tail mismatch since <ts> (<n> spooled)`.

### Step 4 — timestamp rule

- An explicit `ts` is parsed strictly (`YYYY-MM-DD[T ]HH:MM:SS[.frac][Z|±HH[:]MM]`, no zone means
  UTC) and normalised to `YYYY-MM-DDTHH:MM:SSZ`. Anything else is rejected before a file name is
  derived from it.
- An explicit `ts` earlier than the tail's `ts` raises `TimestampError`. The CLI exits 2, nothing is
  written, and the message tells the caller to omit `--ts` and put the original time in `--detail`.
- An implicit `now` earlier than the tail's `ts` (the clock stepped backwards) is clamped to the
  tail's `ts`, and the record gains `clock_regressed` holding the real wall-clock value. The chain
  stays valid and the anomaly is visible. When the tail is more than 300 s ahead of the wall clock,
  the cause is more likely a future-dated record than a clock step. `append` then also prints
  `warning: last record of <bot> is dated <tail ts> (future); this record is stamped with it` on
  stderr. The clamp and the exit code are the same in both cases.
- `archive` checks an explicit `ts` against the tail before it stores the file, so a refused
  timestamp leaves no `.gz` behind.
- The CLI additionally rejects a `--ts` more than 300 s in the future. The library does not, because
  tests use fixed dates.

### Steps 5–7 — record, line, state

- Reserved keys (`v`, `seq`, `bot`, `prev`, `hash`) supplied by the caller are ignored. `None` and
  `""` values are dropped.
- Top-level string values pass through `redact()`. Text that cannot be encoded as UTF-8 is replaced
  with backslash escapes instead of failing the append.
- The line goes to `logs/<bot>/<ts[:10]>.jsonl`. If that file is non-empty and does not end in a
  newline, a newline is written first, so two records can never share a line.
- State is re-read, merged with `{seq, head, updated}` and replaced atomically. Other keys
  (`last_sync`, `capture_browser_last`) are preserved.

### Crash outcomes

| Crash point | What is on disk | Next append |
|---|---|---|
| before step 6 | nothing new | normal |
| during step 6 (torn line) | a partial last line | refused (corrupt tail). Not auto-repaired: restore the log from backup |
| between 6 and 7 | line durable, state one behind | roll-forward |
| after 7 | consistent | normal |

### Recovery tooling (append-only)

- **`gblog state rebuild --bot B`** rewrites `state/<bot>.json` from the tail, under the lock, only
  when both of these hold:
  - the chain walk has no problem other than the state-head mismatch;
  - state is missing, unreadable or genesis, **or** its `(seq, head)` equals an existing record at
    or before the tail (state is behind).

  It preserves other state keys, appends a `note` (`source: auto`,
  `state rebuilt from log tail (was seq A head B)`) and prints `rebuilt <bot> seq N head <12 hex>`.
  N and the head are the values state holds after that note.
  When state already equals the tail, the command prints
  `state/<bot>.json already matches the log tail; nothing to do`, exits 0 and writes nothing.
  State ahead of the log is always refused with `restore the log from backup`.
- **`gblog spool list [--bot B]`** shows pending refused events.
- **`gblog spool replay --bot B`** appends each spooled `record` in file-name order with
  `spooled_at` (when it was refused) and `spool_id`. A `ts` the record carried is moved to
  `original_ts`, so the replay never back-dates. Each replayed file moves to
  `spool/<bot>/replayed/`. When none remain, the alert file is renamed to
  `state/<bot>.alert.resolved-<stamp>.json`. If an append is refused again, the command exits 4 and
  leaves the remaining files in place.

Neither command rewrites, trims, quarantines or rehashes a log line.

### Other writers of state

`sync` (`last_sync`) and `capture browser` (history markers) go through `Ledger.update_state()`. It
takes the bot lock, and if the state file is missing or unreadable while logs exist it seeds `seq` /
`head` from a self-consistent tail (the same test and the same `notice:` line as `append`), or
writes nothing and warns. A genesis state is never written over a non-empty
chain.

## 5. Locks and lock order

| Lock file | Mode | Held by |
|---|---|---|
| `state/<bot>.lock` | exclusive (`LOCK_EX`) | `append`, `update_state`, `state rebuild` |
| `state/<bot>.lock` | shared (`LOCK_SH`) | `verify`, while it snapshots the log lines (parsed record and raw text), `state/<bot>.json`, `anchors/<bot>.json` and the lines of `anchors/<bot>.history.jsonl` |
| `state/aliases.lock` | exclusive | `alias add`, `alias remove`, `alias strict`: the read, the change and the write of `state/aliases.json` happen in one hold |

Rules:

1. **One bot lock at a time.** No code path holds the locks of two chains simultaneously. Multi-chain
   operations take and release them one after another, so there is no lock-order cycle. Example:
   `seal` verifies `kim-biseo`, preflights the `ledger` tail, appends the ack to `kim-biseo`, then
   appends the witness to `ledger`. `sync` stamps `last_sync` on every chain the same way.
2. **Readers release before slow work and before returning.** `verify` drops its shared lock before
   it re-hashes archives and before it returns. Callers such as `seal_break` and
   `verify --record` call `append` right after `verify`, so `verify` must never return holding the
   lock. Everything `verify` judges is computed from the one snapshot taken under the lock: the
   chain walk, the state check, anchor containment, the layout findings (`bot` field, file names,
   non-canonical lines) and `--contains`. No file of the chain is read again after the lock is
   released; only archived files are. A concurrent `append` followed by `anchor` therefore cannot
   make a running `verify` report a head that is "no longer in the chain".
3. **No re-entry.** `flock` is per open file description. Code that already holds a bot lock must
   not call the public `append` for the same bot, because the second acquisition would deadlock.
   `_Lock` is a plain exclusive-or-shared flock holder and keeps no re-entrancy state.
4. **`aliases.lock` is a leaf.** It is taken alone and nothing else is acquired while it is held.
5. **Readers other than `verify` do not lock.** `records()`, `report`, `status` and `tail` tolerate
   a half-written last line by treating it as corrupt and skipping it.
6. Where `fcntl` is unavailable, gblog runs without locks. Concurrent writers are then unsupported.

## 6. Record field reference

### Common to every record

| Field | Type | Meaning |
|---|---|---|
| `v` | int | schema version, always `1` |
| `seq` | int | 1-based position in the chain |
| `ts` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `bot` | string | chain name (the directory under `logs/`) |
| `type` | string | see below |
| `prev` | string | `hash` of the previous record, or `genesis` |
| `hash` | string | 64 hex, see the hash rule |

### Optional on most types

| Field | Meaning |
|---|---|
| `task_id` | id printed by `task-start`, or taken from `$GBLOG_TASK` |
| `task` | task title |
| `detail` | free text (`--detail` / `--summary`), redacted |
| `source` | absent means self-reported; `auto` means captured without the Bot; `routine` means written by a routine run |
| `action` | `shell`, `browser`, `connector`, `file`, `message`, `computer_use`, `delegation`, `memory`, `routine`, `other` |
| `target` | what was acted on |
| `outcome` | `ok`, `fail`, `denied`, `pending`, `skipped`, `n/a` |

### Fields added by the append path itself

| Field | When |
|---|---|
| `clock_regressed` | the wall clock was earlier than the tail and `ts` was clamped; holds the actual wall-clock time |
| `spooled_at` | the record was replayed from the spool; time of the original refusal |
| `spool_id` | name of the spool file it was replayed from |
| `original_ts` | the `ts` the refused record carried, if any |

### Per type

**`task_start`** — `task_id`, `task`, optional `detail`, `source`.

**`action`** — `action`, `target`, `outcome`, optional `detail`, `task_id`, `source`. Automatically
captured actions also carry:

| Field | Meaning |
|---|---|
| `capture` | `browser` or `files` (shell-hook records have `source: auto` and no `capture`) |
| `observed` | browser only: UTC time of the visit in the history database (`ts` is the import time) |
| `profile` | browser only: name of the profile directory the history came from |

For browser records `target` is `scheme://host/path` and `detail` is the page title (first 120
characters). For file records `target` is the path relative to the scanned directory and `detail` is
`created`, `modified` or `deleted`.

**`approval`** — `target` (the proposed action), `decision` ∈ `allowed | denied | pending | expired |
auto`, and `outcome` derived from it (`allowed`/`auto` → `ok`, `denied` → `denied`, `pending` →
`pending`, `expired` → `skipped`).

**`task_end`** — `task_id`, `outcome`, optional `detail`.

**`note`** — `detail`. Special notes written by gblog itself:

- file-capture overflow (`source: auto`, `capture: files`);
- state rebuild (`source: auto`);
- the seal witness on the `ledger` chain: `target` (the sealed bot), `break` (same descriptor as the
  ack) and `jay_approval_id`.

**`sync`** — `target` `git` or `git push`, `outcome` `pending` or `denied`, `detail`.

**`verify`** — written by `verify --record`: `target` `chain`, `outcome` `ok` or `fail`, `detail`
with the problems or the record count.

**`tune`** — `target` (the proposal id), `outcome` `ok` (applied) or `skipped` (rejected), `detail`,
and:

| Field | Meaning |
|---|---|
| `proposal` | object: `title`, `category`, `key`, `metric`, `baseline`, `better`, `window`, `count` — a snapshot of the proposal, so its effect can be measured later. `count` is the number of evidence records at decision time (written since 0.6.1). A rejected proposal stays hidden for 28 days unless its evidence reaches twice that `count`. Older `tune` records have no `count`; for them the evidence still inside the window is counted instead |

**`archive`**

| Field | Meaning |
|---|---|
| `target` | file name as given (basename) |
| `kind` | `transcript`, `message`, `artifact`, `other` |
| `sha256` | SHA-256 of the stored (redacted, uncompressed) content |
| `bytes` | length of the stored content |
| `path` | `archive/<bot>/<date>/<sha256>.gz`, relative to the ledger root |
| `redacted` | `true` if redaction changed the content |
| `scanned` | present and `false` whenever the content was stored without secret scanning: redaction was requested but the content was binary or not UTF-8, or `--no-redact` was given. Absent when the content was scanned |

**`break_ack`** — written only by `seal`.

| Field | Meaning |
|---|---|
| `target` | `<file>:<line_after>` |
| `jay_approval_id` | the operator's approval reference (not authentication) |
| `break` | object: `file`, `line_before`, `line_after`, `seq_before`, `seq_after`, `hash_before`, `orphan_prev`, `hash_after`, `missing_seqs` |

Not stored on disk:

- `chain`: added in memory when variant chains are merged under one slug.
- `bot_field`: readers attribute a record to the chain directory it is stored in. When the stored
  `bot` differs from that directory, the in-memory copy gets `bot` set to the directory name and
  `bot_field` set to the stored value. `gblog export` prints records as stored and never adds it.
  `verify` warns about such a record, and FAILs it with `--strict`.
- `_dedup`: returned by `archive_file`.

Readers other than `export` also replace a non-string value in a text field (`type`, `task_id`,
`task`, `action`, `target`, `outcome`, `detail`, `source`, `decision`, `observed`, `kind`) with its
JSON text in memory, so a hash-valid record with odd field types cannot crash a report.

## 7. Verification

`verify` takes one snapshot under the shared bot lock (section 5): every log line with its raw
text, the state file, `anchors/<bot>.json` and the lines of `anchors/<bot>.history.jsonl`. All
checks below, including the layout findings and `--contains`, use that snapshot and one parser.

`verify` walks one chain in file-name order:

1. Each line must be a UTF-8 JSON object with a string `hash`, `prev` and `ts` and an integer `seq`.
   Otherwise it is a problem and the walk stops.
2. `hash` must equal the hash of the body.
3. `prev` and `seq` must continue from the previous record. A break either matches the single
   eligible `break_ack` (section 9) or is a problem. The walk continues into the next segment, so
   every break and both segment heads are reported.
4. `ts` must not go backwards.
5. The state head, if present and not genesis, must equal the final head. This also applies when
   the walk found no records: a state file that names a head while the chain is empty is a FAIL,
   not `OK … 0 records`. A state file whose `head` is not a string counts as unreadable (warning).
6. Anchor containment (section 8).
7. Archived files are re-hashed unless `--skip-archives` is given.

The lines printed are `OK`, `PASS (N approved break(s))` or `FAIL`, followed by `      - <problem>`
and `      ! <warning>` lines. Warnings do not change the exit code. `PASS` is not a clean chain;
callers must report the count.

Warnings:

- state missing, unreadable or reset to genesis;
- witness note missing on the `ledger` chain;
- the last record dated more than 300 s in the future (section 2);
- a fixture `break_ack` honoured on a temporary copy (section 9);
- a record whose `bot` differs from its chain directory;
- a `*.jsonl` file whose name is not `YYYY-MM-DD.jsonl`.

Flags:

- `--strict` turns the `bot`-field and file-name warnings into problems, and requires every raw line to equal the
  canonical serialisation of its record. This catches duplicate keys and reformatted lines.
- `--contains HASH` (repeatable, with `--bot`) fails with `expected hash <12 hex> not found in chain`.
  It is checked against the hashes of the same snapshot. Together with `--all` it is a usage error
  (exit 2, `verify --contains cannot be combined with --all`).
- `--skip-archives` skips step 7 and prints `(archives not checked)`.
- `--json` prints the result objects as one JSON list (schema below).

`verify --bot` accepts a slug, display name or alias and verifies every chain of that Bot. A name
with no chain on disk is `FAIL … no chain on disk`. `verify --all` also fails for every anchored
chain whose `logs/<bot>` directory has disappeared.

`verify --all` prints one `FAIL  <name>  0 records` block, and exits 1, for each of these. A name is
listed once even when several apply.

| Situation | Problem line |
|---|---|
| `anchors/<bot>.json` records more than zero records, `logs/<bot>` is gone | `chain was anchored on <ts> but logs/<bot> is missing` |
| `anchors/<bot>.history.jsonl` lists at least one head, `logs/<bot>` is gone | `chain was anchored (history, last <ts>) but logs/<bot> is missing` |
| `state/<bot>.json` holds a non-genesis head, `logs/<bot>` is gone | `state/<bot>.json records seq <n> head <12 hex> but logs/<bot> is missing` |
| a directory under `logs/` whose name is not in sanitised form (section 10) | `logs/<dir>: directory name is not a valid chain key; its records are not verified` |

The state check skips gblog's own side files (`aliases`, `capture-files-*`, `*.alert`,
`*.alert.resolved-*`). An invalid directory name is never passed to the chain walk.

### `verify --json` entries

`gblog verify --json` prints one JSON list. Text and JSON output are rendered from the same list of
result objects, so every chain block of the text output has exactly one entry, including a FAIL for
a name that has no chain. Exit codes are the same as without `--json`.

| Key | Type | Meaning |
|---|---|---|
| `bot` | string | chain directory name. On a synthetic entry: the name that was asked for, or the name of the missing or invalid chain |
| `ok` | bool | `false` is FAIL. `true` covers `OK` and `PASS (N approved break(s))`; read `approved_breaks` to tell them apart |
| `records` | int | records walked |
| `head` | string or null | hash of the last record of the last segment; `genesis` for an empty chain; `null` on a synthetic entry |
| `problems` | list of strings | the `-` lines. A `--contains` miss is added to the last chain's entry |
| `warnings` | list of strings | the `!` lines |
| `missing` | bool | present and `true` only on a synthetic entry (no chain on disk for the name, logs missing while a witness survives, invalid directory). Treat an absent key as `false` |
| `archives` | int | number of `archive` records in the chain (0 when the walk already had a problem) |
| `archives_checked` | bool | `false` when `--skip-archives` was given |
| `approved_breaks` | int | 0 or 1 |
| `approved` | list | descriptor of the approved break, if any |
| `jay_approval_id` | string or null | id stored on the honoured ack |
| `fixture_ack` | bool | `true` when the honoured ack is the fabricated test fixture (section 9). Always `false` for a live seal |
| `breaks` | list | descriptors of unapproved breaks: the keys of the `break` object in section 6, plus `file_before` when the break spans two day files |
| `segments` | list | one object per contiguous segment: `seq_start`, `seq_end`, `head`, `line_start`, `line_end`, `file_start`, `file_end`, `records`, `index_start`, `index_end` |
| `state_problem` | string or null | the state-head mismatch text when that problem is in `problems`, otherwise `null`. `state rebuild` uses it to tell this one repairable problem from every other |
| `record_error` | string | only with `--record`, and only when the `verify` record itself could not be appended (tail mismatch). `ok` is not changed by it, but the exit code is 1 |

A synthetic entry has exactly these keys: `bot`, `ok` (`false`), `records` (0), `head` (`null`),
`problems`, `warnings` (empty) and `missing` (`true`).

## 8. Anchor files and history

`gblog anchor` verifies each chain and writes three things. There is no whole-ledger skip: one FAIL
chain never drops the others.

**`anchors/<bot>.json`** — the latest entry.

- OK or PASS chain: `bot`, `ok: true`, `status` (`OK` or `PASS (N approved break(s))`), `records`,
  `head`, `resumed_from` (the previous anchor's head), `approved_breaks`, `approved`,
  `jay_approval_id`, `ts`.
- FAIL chain: `bot`, `ok: false`, `status: FAIL`, `records`, `head` (last segment head), `problems`,
  `break` (first descriptor, or null), `segments`, `segment_heads`, `ts`.

**`anchors/<bot>.history.jsonl`** — one line appended per run and never rewritten:
`{"ts", "ok", "heads": [...], "records"}`. Only the latest anchor survives in the `.json` file; the
history keeps every earlier head.

**`anchors/days/<date>.json`** — `{"mode": "per-chain", "whole_ledger_skip": false, "ts", "chains":
[...]}`. Running `anchor --bot X` later the same day merges X's entry into the existing rollup
instead of replacing the file.

**Containment rule (checked by `verify`).** Every head named by `anchors/<bot>.json` (`head` when
`ok`, otherwise each of `segment_heads`) and every `heads` entry in the history file must still be
the `hash` of some record in the chain. If not, the problem is
`anchors/<bot>.json: anchored head <12 hex> (<ts>) is no longer in the chain (history rewritten or
tail truncated)`. A corrupt history line is itself a problem. Chains that were never anchored behave
as before.

**Missing chain (checked by `verify --all`).** Both anchor files count as witnesses of a chain whose
log directory is gone: `anchors/<bot>.json` with `records` above zero, and any history line with a
non-empty `heads` list. Section 7 lists the problem lines. A chain that was removed on purpose keeps
failing until its anchor files and its state file are removed too; the README ("Upgrading") gives
the procedure.

**Off-box anchors.** Anchor files live beside the log, so an adversary who rewrites the log can
rewrite them too. The files only raise the cost. The witnesses that count are outside the computer:

- the daily routine posts every chain's 64-hex head in the conversation, and the next day runs
  `gblog verify --bot <slug> --contains <yesterday's head>`;
- `gblog sync` pushes `anchors/` to a remote that refuses force-pushes.

## 9. The verify-time `break_ack` pin

Exactly one historical discontinuity may be approved: `kim-biseo`,
`logs/kim-biseo/2026-11-02.jsonl` lines 164/165, seq 1919 → 1921, missing `{1920}`, with the three
full hashes compiled into `KIM_BISEO_BREAK`.

Until 0.6 only `seal` enforced this; `verify` accepted any single self-consistent ack. Since 0.6
`verify` applies the same rule. A `break_ack` record is **eligible** only if the chain is
`kim-biseo` and one of these holds:

- its descriptor equals `KIM_BISEO_BREAK` exactly (all nine keys, full hashes); or
- its descriptor equals `KIM_BISEO_FIXTURE_BREAK` exactly (the pinned coordinates plus the three
  hashes of the fabricated test fixture, the same values as
  `tests/fixtures/kim-biseo-break/expected-break.json`) **and** the ledger root is a temporary copy
  (its real path is under the real path of the system temporary directory). The result then
  carries `fixture_ack: true`, and `verify` adds the warning
  `fixture break_ack honoured: this root is a temporary test copy`.

**`TMPDIR` no longer matters.** In 0.6.0 the fixture rule compared only the six coordinate keys
(`file`, `line_before`, `line_after`, `seq_before`, `seq_after`, `missing_seqs`) on a temporary
copy. `tempfile.gettempdir()` honours `TMPDIR`, so `TMPDIR=/workspace` made a live root count as
"temporary", and an ack at the pinned coordinates with any three hashes became eligible. Since
0.6.1 both eligible descriptors are compiled in with full hashes:

- Whatever `TMPDIR` says, an ack with other hashes is never eligible.
- The fixture hashes belong to synthetic record bodies, so they cannot describe a join in a real
  chain.
- The temporary-copy condition is kept on purpose: the fixture must never be honoured on a live
  root.
- When the fixture ack is honoured, the `!` line shows it in the text output as well as in `--json`.
- `Ledger.seal_break(copy_fixture=True)` applies the same rule and refuses any other descriptor
  with `refusing: copy fixture hashes do not match the committed fixture`.

Every other ack, on any chain, adds the problem
`<file>:<line>: break_ack is not the pinned kim-biseo 2026-11-02 approval` and is ignored when
joins are evaluated. The break it claimed to approve still FAILs.

An eligible ack must also:

- be the only ack in the chain;
- sit after the join, in the same file;
- describe the on-disk join exactly (seqs, lines, the hash before, the orphan `prev`, the hash
  after);
- lie inside a verified segment.

It approves one link. A later break is FAIL.

**Witness.** `seal` appends the same descriptor and approval id as a `note` on the `ledger` chain.
Before writing anything, `seal` preflights the `ledger` chain's tail and refuses with
`refusing: ledger witness chain has a tail mismatch` if it would not accept an append. This prevents
an ack without a witness. `verify` warns `witness note missing on ledger chain` when a pinned
(non-fixture) ack is used, a `ledger` chain exists, and no such note is found.

`jay_approval_id` is stored on both records. It is a reference to an approval made elsewhere. The
CLI rejects obvious placeholders, but the id is not authenticated.

## 10. Reserved bot names

Per-chain files in `state/` are named after the chain, so some names would collide with other files
there:

| Reserved | Collides with |
|---|---|
| the name `aliases` | `state/aliases.json`, `state/aliases.lock` |
| prefix `capture-files-` | `state/capture-files-<bot>.json` (another bot's snapshot) |
| suffix `.alert` | `state/<bot>.alert.json` |
| suffix `.lock` | `state/<bot>.lock` |
| suffix `.tmp` | `state/<bot>.json.tmp` |

`append` and `alias add` refuse these names (exit 2) unless `logs/<name>` already exists, so a
legacy chain with such a name keeps appending and verifying. In `append` the check runs right after
the name is sanitised, before the directory tree is created and before the lock is taken. A refused
name therefore writes nothing: `gblog note --bot aliases --detail x` exits 2 and creates neither
`logs/aliases/` nor a `state/aliases.json` that would overwrite the alias registry.

Two names are conventional, not reserved: `ledger` (the seal witness chain and the usual owner of
the daily routine) and `system` (the default `--bot` of `sync`).

Chain names are otherwise sanitised to `[A-Za-z0-9._-]`. Display names with spaces or non-ASCII
characters must be registered with `gblog alias add`.

gblog only ever creates chain directories whose name is already in sanitised form. A directory
under `logs/` whose name is not (it contains a space, starts or ends with `-` or `.`, or has no
letter or digit at all) is not a chain:

- `verify --all` FAILs it (section 7) and never walks it under a different name;
- `report`, `status`, `tail`, `coverage`, `tune` and `export` skip it and print
  `warning: ignoring logs/<dir> (invalid chain directory name); run gblog verify --all` on stderr;
- one such directory no longer makes `status` or `verify --all` exit 2 for the whole ledger.

## 11. Exit codes

| Code | Meaning | Examples |
|---|---|---|
| 0 | Success | any write; `verify` with only `OK` / `PASS (N approved break(s))` lines (warnings do not change it); `sync --no-push`; a closed output pipe |
| 1 | Integrity FAIL, or not pushed | `verify` / `anchor` with a FAIL chain; `verify --bot` or `anchor --bot` with no chain on disk; a chain whose logs are missing while its state file, anchor or anchor history survives; an invalid directory name under `logs/`; a `--contains` miss; `verify --record` whose record append was refused; `sync` that committed but did not push; `capture browser` with no readable history |
| 2 | Usage or environment error (see below for what may have been written) | unknown flag or value; invalid, past or far-future `--ts`; an invalid date; `verify --contains` with `--all`; unregistered name under `alias strict on`; reserved name; alias conflict; unreadable `--file`; corrupt side file; an unreadable `state/aliases.json` that an `alias` command would overwrite; `git` failure; `seal` refusal; `state rebuild` refusal; unknown proposal id; `OSError` such as a full disk |
| 3 | Policy refusal | `sync` would push to a public GitHub remote, or to one whose visibility cannot be confirmed |
| 4 | Tail mismatch | an append was refused and the event is in `spool/<bot>/`; `sync` after committing, when one of its own records was refused; `spool replay` refused again |
| 130 | Interrupted | Ctrl-C |

Exit 2 covers two situations, and only the first guarantees that nothing was written:

- **Usage error.** An argparse message, or `error: --…`, a reserved or unregistered name, an alias
  conflict, a `seal:` or `state rebuild:` refusal. Nothing was written.
- **Environment error.** `error: <strerror>[: <path>]`. The failing step may come after the log
  line is durable. Example: `ENOSPC` while `state/<bot>.json` is replaced leaves the record in the
  log, and the next append rolls forward (section 4). Check `gblog tail --bot <bot> -n 1` before
  retrying, or the record is written twice.
- Two commands exit 2 after partial work by design. `sync` with a `git` failure has already written
  its `sync pending` record. `spool replay` exits 2 when it had to skip an unreadable spool file,
  after appending every readable one; it lists what it replayed.

When several apply to `sync`, 2 and 3 take precedence over 4. No code path prints a traceback for
malformed ledger input; that would be a bug.

## 12. Deliberate non-features

- **No generic `seal`, no `--force`, no "accept truncation" mode.** The single pinned incident is the
  design. Verify is as strict as seal.
- **No auto-repair that rewrites, trims, quarantines or rehashes log lines**, including a torn final
  line. Recovery is append-only (`state rebuild`, `spool replay`) or a restore from backup.
- **No signatures or HMAC with an on-box key.** The adversary has the same file access; use off-box
  anchors.
- **No change to the canonical JSON, hash algorithm or schema `v`.**
- **No third-party dependencies and no database backend.** Per-append cost stays O(1) and the JSONL
  files stay human-readable.
- **No per-Bot attribution of shared-computer browser or file activity.** It is not knowable from
  the evidence.
- **`log --type` still accepts housekeeping types** (`verify`, `sync`, `tune`, `archive`) for
  backward compatibility. Treat such records as claims.
- **No refusal to append when the tail is future-dated.** That would stop logging. gblog keeps the
  clamp and warns in `append`, `verify` and `status`.
- **No failed write when `state/aliases.json` is unreadable.** Losing an event is worse than
  logging it under an unresolved name. gblog warns, resolves nothing and refuses only to overwrite
  the registry.
- **No `allow_abbrev=False`, and `--root` stays on every subcommand.** The abbreviations that became
  ambiguous are documented in the README instead.
- **No fixture ack outside a temporary copy.** Pinning the fixture by hash did not remove the
  temporary-copy condition (section 9).
