# gblog design

This document describes how `gblog` 0.6 behaves and why. The README is the user guide and
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
| The tail truncated, or the chain rewritten, **while a witness survives** | state head mismatch; anchor containment (section 8); `verify --contains` |
| A whole chain deleted while `anchors/<bot>.json` survives | missing-chain check in `verify --all` |

`gblog verify` prints a warning (a `!` line, exit code unchanged) when it has lost a witness:
`state/<bot>.json` is missing, unreadable or reset to genesis. A warning means "truncation can no
longer be detected from state". It does not mean "nothing happened".

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
  as a test copy for the purposes of section 9. Never keep a production ledger there.

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

"Unreadable" includes a state file that parses but is not a JSON object.

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
  stays valid and the anomaly is visible.
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
takes the bot lock, and if the state file is missing while logs exist it seeds `seq` / `head` from a
self-consistent tail, or writes nothing and warns. A genesis state is never written over a non-empty
chain.

## 5. Locks and lock order

| Lock file | Mode | Held by |
|---|---|---|
| `state/<bot>.lock` | exclusive (`LOCK_EX`) | `append`, `update_state`, `state rebuild` |
| `state/<bot>.lock` | shared (`LOCK_SH`) | `verify`, while it snapshots log items, state and anchor files |
| `state/aliases.lock` | exclusive | `save_aliases` |

Rules:

1. **One bot lock at a time.** No code path holds the locks of two chains simultaneously. Multi-chain
   operations take and release them one after another, so there is no lock-order cycle. Example:
   `seal` verifies `kim-biseo`, preflights the `ledger` tail, appends the ack to `kim-biseo`, then
   appends the witness to `ledger`. `sync` stamps `last_sync` on every chain the same way.
2. **Readers release before slow work and before returning.** `verify` drops its shared lock before
   it re-hashes archives and before it returns. Callers such as `seal_break` and
   `verify --record` call `append` right after `verify`, so `verify` must never return holding the
   lock.
3. **No re-entry.** `flock` is per open file description. Code that already holds a bot lock must
   not call the public `append` for the same bot, because the second acquisition would deadlock.
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
| `proposal` | object: `title`, `category`, `key`, `metric`, `baseline`, `better`, `window` — a snapshot of the proposal, so its effect can be measured later |

**`archive`**

| Field | Meaning |
|---|---|
| `target` | file name as given (basename) |
| `kind` | `transcript`, `message`, `artifact`, `other` |
| `sha256` | SHA-256 of the stored (redacted, uncompressed) content |
| `bytes` | length of the stored content |
| `path` | `archive/<bot>/<date>/<sha256>.gz`, relative to the ledger root |
| `redacted` | `true` if redaction changed the content |
| `scanned` | present and `false` only when redaction was requested but the content was binary or not UTF-8, so it was stored unscanned |

**`break_ack`** — written only by `seal`.

| Field | Meaning |
|---|---|
| `target` | `<file>:<line_after>` |
| `jay_approval_id` | the operator's approval reference (not authentication) |
| `break` | object: `file`, `line_before`, `line_after`, `seq_before`, `seq_after`, `hash_before`, `orphan_prev`, `hash_after`, `missing_seqs` |

Not stored on disk: `chain` (added in memory when variant chains are merged under one slug) and
`_dedup` (returned by `archive_file`).

## 7. Verification

`verify` walks one chain in file-name order:

1. Each line must be a UTF-8 JSON object with a string `hash`, `prev` and `ts` and an integer `seq`.
   Otherwise it is a problem and the walk stops.
2. `hash` must equal the hash of the body.
3. `prev` and `seq` must continue from the previous record. A break either matches the single
   eligible `break_ack` (section 9) or is a problem. The walk continues into the next segment, so
   every break and both segment heads are reported.
4. `ts` must not go backwards.
5. The state head, if present and not genesis, must equal the final head.
6. Anchor containment (section 8).
7. Archived files are re-hashed unless `--skip-archives` is given.

The lines printed are `OK`, `PASS (N approved break(s))` or `FAIL`, followed by `      - <problem>`
and `      ! <warning>` lines. Warnings do not change the exit code. `PASS` is not a clean chain;
callers must report the count.

Warnings: state missing, unreadable or reset to genesis; witness note missing on the `ledger` chain;
a record whose `bot` differs from its chain directory; a `*.jsonl` file whose name is not
`YYYY-MM-DD.jsonl`.

Flags:

- `--strict` turns the last two warnings into problems, and requires every raw line to equal the
  canonical serialisation of its record. This catches duplicate keys and reformatted lines.
- `--contains HASH` (repeatable, with `--bot`) fails with `expected hash <12 hex> not found in chain`.
- `--skip-archives` skips step 7 and prints `(archives not checked)`.
- `--json` prints the result objects, including `warnings` and `fixture_ack`.

`verify --bot` accepts a slug, display name or alias and verifies every chain of that Bot. A name
with no chain on disk is `FAIL … no chain on disk`. `verify --all` also fails for every anchored
chain whose `logs/<bot>` directory has disappeared.

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
- the ledger root is a temporary copy (its real path is under the real path of the system temporary
  directory) **and** the six coordinate keys (`file`, `line_before`, `line_after`, `seq_before`,
  `seq_after`, `missing_seqs`) equal the pinned ones. This is the fabricated test fixture, whose
  hashes necessarily differ. The result then carries `fixture_ack: true`.

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
legacy chain with such a name keeps appending and verifying.

Two names are conventional, not reserved: `ledger` (the seal witness chain and the usual owner of
the daily routine) and `system` (the default `--bot` of `sync`).

Chain names are otherwise sanitised to `[A-Za-z0-9._-]`. Display names with spaces or non-ASCII
characters must be registered with `gblog alias add`.

## 11. Exit codes

| Code | Meaning | Examples |
|---|---|---|
| 0 | Success | any write; `verify` with only `OK` / `PASS (N approved break(s))` lines (warnings do not change it); `sync --no-push`; a closed output pipe |
| 1 | Integrity FAIL, or not pushed | `verify` / `anchor` with a FAIL chain; `verify --bot` or `anchor --bot` with no chain on disk; an anchored chain whose logs are missing; a `--contains` miss; `verify --record` whose record append was refused; `sync` that committed but did not push; `capture browser` with no readable history |
| 2 | Usage or environment error; nothing was written | unknown flag or value; invalid, past or far-future `--ts`; an invalid date; unregistered name under `alias strict on`; reserved name; alias conflict; unreadable `--file`; corrupt side file; `git` failure; `seal` refusal; `state rebuild` refusal; unknown proposal id |
| 3 | Policy refusal | `sync` would push to a public GitHub remote, or to one whose visibility cannot be confirmed |
| 4 | Tail mismatch | an append was refused and the event is in `spool/<bot>/`; `sync` after committing, when one of its own records was refused; `spool replay` refused again |
| 130 | Interrupted | Ctrl-C |

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
