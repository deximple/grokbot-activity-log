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
13. Cost on large ledgers

## 1. Threat model

**The adversary is the Bot, or anyone with write access to the ledger directory.**

- The Bot may be careless (forgets to log), confused (follows instructions embedded in a web page
  title), or actively hiding an action.
- It runs as the same operating-system user as `gblog`. It can read, edit, truncate, replace or
  delete every file under the ledger root: `logs/`, `state/`, `anchors/`, `spool/`, `archive/`.
- It can import `gblog` and call `Ledger.append()` directly, bypassing the CLI. Record types, flag
  validation and the future-timestamp bound are CLI conveniences, not security boundaries.
- It can set environment variables (`GBLOG_ROOT`, `TMPDIR`, `GBLOG_TASK`, `GH_HOST`, `HISTCONTROL`)
  and may be able to change the clock.
- It controls everything git reads inside the ledger repository: `.git/config`, hooks, ignore
  files, `.gitattributes` filters and index flags.
- It can plant symlinks, FIFOs and directories anywhere in the tree, and put any text (terminal
  escapes, Markdown, look-alike output lines) into record fields, display names and file names.

Two narrower positions are also in the model, because gblog can do more against them:

- **CLI-only.** A prompt-injected Bot whose shell commands need approval but whose `gblog` commands
  are approved automatically (README, setup step 6). Whatever `gblog` arguments alone can do, this
  adversary does without an approval card. gblog therefore refuses to re-point an existing `origin`
  without `--replace-remote`, records every `origin` change, classifies GitHub remotes by parsed
  host, and keeps `--contains` chain-exact (sections 7 and 11, `SECURITY.md`). The allow-list in
  the README is the other half of this boundary.
- **The author of a tree you audit.** An auditor who clones the backup repository runs gblog as a
  different user on another computer. There the tree is untrusted input: gblog does not follow a
  symlink at a path it writes, never opens a non-regular `*.jsonl` entry, and neutralises control
  characters in what it prints. `SECURITY.md` ("Auditing a clone of the backup") has the procedure.

**Outside the adversary's reach, by assumption:**

- the conversation transcript, where the daily routine posts chain heads;
- a git remote that rejects force-pushes and that the Bot's token cannot administer.

**Goal.** Tamper *evidence*, not tamper *prevention*. gblog cannot stop a same-user process from
changing a file. It makes the change show up in `gblog verify`.

The exception is confidentiality of the backup, where gblog does prevent: `sync` refuses a public
or unconfirmed GitHub remote and does not change where backups go unless it is told to. This holds
for `gblog sync` only, not for `git` used directly.

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
| An archived file modified, missing, addressed outside `archive/`, or stored under a name that is not `<sha256>.gz`; an `archive` record that has only one of `path` and a 64-hex `sha256` | re-hash, path check and address check. A record that names neither is a warning (section 7) |
| The tail truncated, the chain emptied, or the chain rewritten, **while a witness survives** | state head mismatch (checked even when the walk finds zero records); anchor containment, including the newest day rollup (section 8); `verify --contains` |
| A whole chain deleted while `anchors/<bot>.json`, `anchors/<bot>.history.jsonl`, a day-rollup entry (`anchors/days/*.json`) or `state/<bot>.json` survives | missing-chain check in `verify --all`, and in `verify --bot <name>` for that exact name |
| A `*.jsonl` entry under `logs/<bot>/` that is not a regular file (FIFO, directory, device) | regular-file check; the entry is never opened |
| An anchor-history or archive file that is not a regular file (also behind a symlink) | regular-file check; the file is never opened. A non-regular `state/<bot>.json`, `anchors/<bot>.json`, alias registry, alert file or day rollup is never opened either and is treated as unreadable (for state and the anchor file: a warning, not a FAIL) |
| A chain directory that cannot be listed | `logs/<bot>: cannot be listed (<class>)`; it is not taken for an empty chain |
| A directory under `logs/` whose name is not a valid chain key (`logs/my bot`, `logs/ops.`, `logs/---`) | invalid-directory check in `verify --all`. Its records are not verified, and readers ignore the directory |

`gblog verify` prints a warning (a `!` line, exit code unchanged) when it has lost a witness:
`state/<bot>.json` is missing, unreadable or reset to genesis. A warning means "truncation can no
longer be detected from state". It does not mean "nothing happened".

`gblog verify` also warns when the last record of a chain is dated more than 300 s in the future:
`last record is dated <ts>, in the future: new records are stamped with that time (see
clock_regressed)`. Because of the clamp in section 4 (step 4), one future-dated record pins every
later record to that timestamp and to that day file. `report --date D` therefore also lists records
whose `clock_regressed` value (the real wall-clock time) falls on day D, wherever they are filed.
The same condition is reported in two more places:

- `append` prints `warning: last record of <bot> is dated <tail ts> (future); this record is
  stamped with it` on stderr each time it clamps to such a tail;
- `status` prints `CLOCK <chain>: last record dated <ts> (future)` after the `ALERT` lines, and
  `status --json` rows carry `future_tail` (the timestamp, or `null`).

This is a warning, not a FAIL. The chain is intact, and refusing to append would stop logging.

**One chain cannot hide the others.** `verify --all` and `anchor` treat an unexpected error while
one chain is checked as a FAIL of that chain and continue with the next one. A poisoned line, file
or side file never turns into a whole-ledger skip. This covers `anchor`'s own pre-checks and
writes: a failure there is the FAIL entry `anchor aborted: <class>` for that chain. Not covered
yet: the witness scan that runs before the first chain (an `anchors/` or `anchors/days/` directory
that cannot be listed), the write of the day rollup, and a lock file that is not a regular file
(`SECURITY.md`, "Filesystem assumptions").

**The "state missing" warning survives an append.** An append that adopts the log tail writes a
fresh state file, which would end the warning above. The record that adopted the tail therefore
carries `state_adopted` (section 4), and `verify` warns about every such record.

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
- **Confidentiality against shell access.** The remote guard covers `gblog sync`. Whoever can run
  `git` or edit `.git/config` can push the tree anywhere.
- **A remote nobody checks.** `sync` verifies that its commit holds gblog's files as they are on
  disk. A later commit made with `git` directly, or a remote that is never cloned and verified, is
  outside what gblog can see.
- **Capture completeness.** The shell hook reads interactive bash history only. A deleted
  file-capture snapshot gives a new baseline; gblog announces it but the changes in between are
  lost.
- **Safe write commands on an untrusted tree.** Only the final path component is protected against
  symlinks (section 12). Audit a clone with read-only commands.

## 3. On-disk layout

```
logs/<bot>/YYYY-MM-DD.jsonl        one record per line, hash-chained per bot, UTC day files
state/<bot>.json                   chain head (seq, head, updated) + last_sync + capture markers
                                   + state_adopted (sections 4 and 6)
state/<bot>.lock                   flock target for that chain
state/<bot>.alert.json             latest tail-mismatch refusal (page on this file)
state/<bot>.alert.resolved-<stamp>.json   alert renamed after a complete spool replay
state/capture-files-<bot>.json     file-capture snapshot (not committed; see .gitignore); one more
                                   capture-files-<bot>.<tag>.json per additional --path
state/aliases.json, aliases.lock   alias registry and its lock
state/.anchors.lock                held for the whole run of `gblog anchor` (section 5)
spool/<bot>/<stamp>-<nnnn>-<id>.json  event preserved when an append was refused; <nnnn> counts the
                                   refusals of one second, so names sort in refusal order
                                   (files written before 0.6.1 have no counter)
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

"Unreadable" includes a state file that parses but is not a JSON object, an object whose `head` is
not a string, and a path that is not a regular file. In the first column, "present" means the file
exists and parses to an object with a string `head`.

A state file that names the right `head` with a wrong `seq` falls under "anything else": `append`
refuses. `verify` warns `state/<bot>.json: seq <n> does not match the last record (seq <m>);
appends are refused until gblog state rebuild`, and `state rebuild` repairs it.

A state file that exists but cannot be read follows exactly the same rule as a missing one, in
`append` and in `update_state`. When a tail is adopted this way, the command prints one of these
lines on stderr:

- `notice: state/<bot>.json was missing; adopted the log tail (seq <n>)`
- `notice: state/<bot>.json was unreadable; adopted the log tail (seq <n>)`

`verify` reports the same condition as a `!` warning.

The record written by that append carries `state_adopted` with the value `missing` or `unreadable`.
The field is part of the hashed body. `verify` warns about every record that has it, because a
truncation before that record cannot be ruled out from state. Without the field, the first append
after a truncation plus a deleted state file would erase the only sign of it; the daily routine
itself appends before it verifies.

**Roll-forward rule.** Step 6 makes the line durable before step 7 replaces state. A process killed
between the two, or an `ENOSPC` while writing the state file, leaves state exactly one record behind
a valid tail whose `prev` is the state head. That is the only mismatch a crash inside `append` can
produce, so it is the only one repaired automatically. The append adopts the tail, prints
`notice: recovered state/<bot>.json after an interrupted append` on stderr and continues. It writes
the adopted tail to state before the new line, so a second failed state write appends nothing and
leaves state exactly one record behind again. State
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
- Known limit (D1-09, K3-01): the tail is used as a bound only when its `ts` is in the normalised
  form. After a record written with a free-form `--ts` before 0.6 (`…+09:00`, a space in place of
  `T`), neither the refusal nor the clamp applies, and the day file is chosen from the new `ts`
  alone. `verify` compares `ts` as text and walks day files in name order, so such an append can
  fail `verify` for good or land in an earlier day file (README, "Upgrading from 0.5").
- The CLI additionally rejects a `--ts` more than 300 s in the future. The library does not, because
  tests use fixed dates.

### Steps 5–7 — record, line, state

- Reserved keys (`v`, `seq`, `bot`, `prev`, `hash`) supplied by the caller are ignored. `None` and
  `""` values are dropped.
- Top-level string values pass through `redact()`. Text that cannot be encoded as UTF-8 is replaced
  with backslash escapes instead of failing the append. The generated `path` and `sha256` of an
  `archive` record are stored as written: a Bot name shaped like a key prefix (`xai-…`, `sk-…`)
  used to turn the path into `archive/[REDACTED]/…`, a permanent FAIL. `verify` resolves such an
  older record to the chain's own archive directory.
- The text fields listed at the end of section 6 must be strings, and every number must be finite.
  A record that breaks either rule (a list as `task_id`, `NaN`) is refused before anything is
  written; the CLI exits 2. Records already on disk are not affected.
- The line goes to `logs/<bot>/<ts[:10]>.jsonl`. If that file is non-empty and does not end in a
  newline, a newline is written first, so two records can never share a line.
- No write follows a symlink at its final path component. Day files, anchor histories, lock files,
  reports and tuning files are opened with `O_NOFOLLOW`: a symlink there is an error (exit 2).
  State, anchor, rollup, snapshot and archive files are written to a per-process temporary file
  (`<path>.<pid>.tmp`, also opened with `O_NOFOLLOW`) that replaces the path, so a symlink there
  is replaced by a regular file.
- If the write of a line fails part-way (for example `ENOSPC`), the file is truncated back to the
  size it had before the call. That removes only bytes the failing call wrote. A process killed
  mid-write can still leave a torn line (see "Crash outcomes").
- The state object read under the lock (step 2) is merged with `{seq, head, updated}` and replaced atomically. Other keys
  (`last_sync`, `capture_browser_last`) are preserved, except a state-level `state_adopted` object,
  which moves onto the record (see "Other writers of state").

### Crash outcomes

| Crash point | What is on disk | Next append |
|---|---|---|
| before step 6 | nothing new | normal |
| during step 6 (torn line) | a partial last line | refused (corrupt tail). Not auto-repaired: restore the log from backup |
| between 6 and 7 | line durable, state one behind | roll-forward |
| after 7 | consistent | normal |

The first record of a chain is covered as well: a genesis state file is written before the first
line, so an interruption there is "one behind" and rolls forward without a `state_adopted` marker.
One known limit (CHANGELOG, "Known open findings"): while state is one record behind, `verify`
reports the state-head mismatch as FAIL until the next append repairs it.

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
  leaves the remaining files in place. A spooled event of type `break_ack`, or one whose values
  `append` refuses, is never replayed: the file stays in place and is listed as skipped (exit 2).
  Two `spool replay` runs for the same chain cannot append the same event twice.

Neither command rewrites, trims, quarantines or rehashes a log line.

### Other writers of state

`sync` (`last_sync`) and `capture browser` (history markers) go through `Ledger.update_state()`. It
takes the bot lock, and if the state file is missing or unreadable while logs exist it seeds `seq` /
`head` from a self-consistent tail (the same test and the same `notice:` line as `append`), or
writes nothing and warns. A genesis state is never written over a non-empty
chain.

No record is appended on that path, so the adoption marker cannot go into the chain at once.
`update_state` stores it in the state file instead, as
`state_adopted: {"how": "missing" | "unreadable", "seq": <n>, "ts": <when>}`. While the key is
there, `verify` warns `state/<bot>.json was re-created from the log tail at seq <n> (it was <how>):
truncation before that record cannot be ruled out from state`. The next `append` to the chain
writes `state_adopted: <how>` on its record and drops the key from the state file it writes. From
then on the marker is part of the hash chain, not of a file the adversary can edit, and `verify`
reports it as a record-level warning like any other `state_adopted` record.

`update_state` refuses a reserved name for a new chain, like `append` (section 10).

### Traces of a lost state file

A missing or unreadable `state/<bot>.json` can be re-created in three ways. Each leaves a
different trace:

| State re-created by | Trace | `verify` afterwards |
|---|---|---|
| `append` (any logging command) | field `state_adopted` (`missing` or `unreadable`) on the record that adopted the tail; part of the hashed body | `! <file>:<line>: appended while state/<bot>.json was <how>; truncation before seq <n> cannot be ruled out from state` |
| `sync`, `capture browser` (through `update_state`) | key `state_adopted: {how, seq, ts}` in `state/<bot>.json`, until the next `append` moves it onto its record (first row) | `! state/<bot>.json was re-created from the log tail at seq <n> (it was <how>): truncation before that record cannot be ruled out from state` |
| `gblog state rebuild` (operator) | a `note` record with `source: auto` and the detail `state rebuilt from log tail (was seq - head missing)` (or `unreadable`) | no `!` line |

The record-level `!` line is permanent. The state-level one lasts until the next append:

- The record field is part of the hash chain and cannot be removed.
- The state key is kept by `update_state`. The next `append` (also the note that `state rebuild`
  writes) moves it onto its record and drops the key. The record value is the key's `how`, or
  `unknown` when that is neither `missing` nor `unreadable`.

Each line means the same thing: at that point the state witness was gone, so state cannot show
whether the tail was cut just before. What to do with one:

- Check the chain once against a head kept off the computer
  (`gblog verify --bot <chain> --contains <hash>`).
- Report the line once with its cause.
- After that it is a known historical marker. No command dismisses it (section 12).

## 5. Locks and lock order

| Lock file | Mode | Held by |
|---|---|---|
| `state/<bot>.lock` | exclusive (`LOCK_EX`) | `append`, `update_state`, `state rebuild` |
| `state/<bot>.lock` | shared (`LOCK_SH`) | `verify`, while it snapshots the log lines (parsed record and raw text), `state/<bot>.json`, `anchors/<bot>.json`, the lines of `anchors/<bot>.history.jsonl` and the newest day-rollup entry that names the chain |
| `state/aliases.lock` | exclusive | `alias add`, `alias remove`, `alias strict`: the read, the change and the write of `state/aliases.json` happen in one hold |
| `state/.anchors.lock` | exclusive | `anchor`, for its whole run: the verify of every chain, the anchor files and the read-merge-write of `anchors/days/<date>.json` |

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
   `_Lock` keeps no re-entrancy state (the 0.6.0 table `_held` is gone). No code path nests a lock
   on the same file.
4. **`aliases.lock` is a leaf; `.anchors.lock` is an outer lock.** `aliases.lock` is taken alone
   and nothing else is acquired while it is held. `state/.anchors.lock` is not a leaf:
   - It is held for the whole `anchor` run, so `verify` takes each chain's shared bot lock while
     it is held.
   - It is taken before any bot lock and never while one is held. The order is always
     `.anchors.lock`, then one bot lock, so there is no cycle.
   - It exists so that two concurrent `gblog anchor` runs cannot interleave their anchor files or
     lose each other's day-rollup entries.
   - Its name starts with a dot because no chain name can (section 10). A Bot named `anchors`
     locks `state/anchors.lock`, which is a different file. Without the dot the two locks would be
     one file.
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
| `state_adopted` | `missing` or `unreadable` (`unknown` when a marker moved from the state file had no usable `how`): this append adopted the log tail because `state/<bot>.json` was absent or could not be read, or `update_state` had done so before it and left the marker in the state file (section 4). Written since 0.6.1. `verify` warns about such records: the first few one by one, then a count |

### Per type

**`task_start`** — `task_id`, `task`, optional `detail`, `source`.

**`action`** — `action`, `target`, `outcome`, optional `detail`, `task_id`, `source`. Automatically
captured actions also carry:

| Field | Meaning |
|---|---|
| `capture` | `browser` or `files` (shell-hook records have `source: auto` and no `capture`) |
| `observed` | browser: UTC time of the visit in the history database (`ts` is the import time). Files: modification time of a created or modified file; a `deleted` record carries `observed_from` (the time of the previous snapshot) instead |
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

**`sync`** — `target` `git` (`outcome` `pending` before the commit; `fail` when the committed
content differs from disk), `git push` (`denied`) or `git remote` (`ok`: `origin` was set or
changed), and `detail`.

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
| `path` | `archive/<bot>/<date>/<sha256>.gz`, relative to the ledger root; stored as written, never redacted |
| `redacted` | `true` if redaction changed the content |
| `scanned` | present and `false` whenever the content was stored without secret scanning: redaction was requested but the content is not UTF-8 text and holds a NUL byte, or `--no-redact` was given. Absent when the content was scanned (text with stray invalid bytes is scanned) |

**`break_ack`** — written only by `seal`.

| Field | Meaning |
|---|---|
| `target` | `<file>:<line_after>` |
| `jay_approval_id` | the operator's approval reference (not authentication) |
| `break` | object: `file`, `line_before`, `line_after`, `seq_before`, `seq_after`, `hash_before`, `orphan_prev`, `hash_after`, `missing_seqs` |

Not stored on disk:

- `chain`: added in memory when variant chains are merged under one slug.
- `bot_field`: readers attribute a record to the chain directory it is stored in. When the stored
  `bot` differs from that directory (also when it is not a string), the in-memory copy gets `bot` set to the directory name and
  `bot_field` set to the stored value. `gblog export` prints records as stored and never adds it.
  `verify` warns about such a record, and FAILs it with `--strict`.
- `_dedup`: returned by `archive_file`.

Readers other than `export` also replace a non-string value in a text field (`type`, `task_id`,
`task`, `action`, `target`, `outcome`, `detail`, `source`, `decision`, `observed`, `kind`, `capture`,
`profile`, `chain`) with its
JSON text in memory, so a hash-valid record with odd field types cannot crash a report.
Since 0.6.1 `append` refuses such a value (section 4), so only hand-written lines and older
records carry one. `verify` warns about them.

### Keys of `state/<bot>.json`

The state file is not a record, but `append`, `verify` and `status` read it.

| Key | Written by | Meaning |
|---|---|---|
| `seq`, `head`, `updated` | `append`, `state rebuild`, and `update_state` when it adopts the tail | position, hash and `ts` of the last record |
| `last_sync` | `sync` | `{ts, commit, pushed}` of the last sync |
| `capture_browser_last` | `capture browser` | one marker per history file path |
| `state_adopted` | `update_state` only | `{how, seq, ts}`: state was missing or unreadable (`how`) and was re-created from the log tail at `seq`. The next `append` moves it onto its record and drops the key; `verify` warns about it for as long as it is there (section 4) |

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
7. Archived files are re-hashed unless `--skip-archives` is given. An `archive` record is a
   problem when it has a `sha256` but no `path`, a `path` but no 64-hex `sha256`, or a path whose
   file name is not `<sha256>.gz`. A record that names neither (`gblog log --type archive` writes
   one) is a warning: it is a claim, and no stored file backs it. Two records that name the same
   path with different hashes are each checked. `--skip-archives` skips the problems of this
   step, not the warning.

The lines printed are `OK`, `PASS (N approved break(s))` or `FAIL`, followed by `      - <problem>`
and `      ! <warning>` lines. Warnings do not change the exit code. `PASS` is not a clean chain;
callers must report the count.

Only regular files take part in the walk. Any other `*.jsonl` entry in the chain directory (a FIFO,
a directory, a device) is a problem (`<path>: not a regular file`) and is never opened, so it can
hang neither `verify` nor an
`append` that holds the lock.

The side files are never opened either when they are not regular files (also behind a symlink).
For `anchors/<bot>.history.jsonl` and an archived file that is a problem (`…: not a regular file`).
For `state/<bot>.json` and `anchors/<bot>.json` it is the "unreadable" warning, and an alias
registry, alert file or day rollup of that kind is treated as unreadable too. A chain directory
that cannot be listed is the problem `logs/<bot>: cannot be listed (<class>)`, not an empty chain.

Problem and warning lines can quote ledger content: a `bot` field, a file name, an anchor `ts`.
Control characters in them are neutralised before printing, so a crafted value cannot erase or
forge a line on a terminal. `tail`, `status`, `spool list` and `alias list` follow the same rule.

Warnings:

- state missing, unreadable or reset to genesis;
- witness note missing on the `ledger` chain;
- the last record dated more than 300 s in the future (section 2);
- a fixture `break_ack` honoured on a temporary copy (section 9);
- a record whose `bot` differs from its chain directory;
- a `*.jsonl` file whose name is not `YYYY-MM-DD.jsonl`;
- a record whose `ts` date differs from the day file it is stored in
  (`record dated <date> is stored in day file <date>`: a renamed or merged day file);
- a record that carries `state_adopted`, or a state file that carries the key `state_adopted`
  (section 4). The record line is permanent; the state line lasts until the next append;
- a state file that names the last record's hash with another `seq` (section 4);
- a record with a non-string text field or a non-standard number (`NaN`, `Infinity`);
- a line with duplicate keys;
- an `archive` record that names neither a `path` nor a `sha256` (step 7);
- `anchors/<bot>.json` exists but cannot be read, so its heads cannot be checked;
- a symlink among the chain's files: `logs/<bot>` and every entry in it, `state/<bot>.json`,
  `anchors/<bot>.json`, `anchors/<bot>.history.jsonl`, `archive/<bot>` and its day directories.

The written form of `ts` is checked with `--strict` only (`ts is not in normalised form`), because
records written with a free-form `--ts` before 0.6 are valid.

Flags:

- `--strict` turns the layout warnings into problems (`bot` field, file name, a record in a day
  file of another date, field types,
  non-standard numbers, duplicate keys, symlinks, an `archive` record that names no file), and
  requires every raw line (without surrounding white space) to equal the
  canonical serialisation of its record, and every `ts` to be in the normalised form. This catches
  reformatted lines. The state warnings and the
  `state_adopted` warning stay warnings.
- `--contains HASH` (repeatable, with `--bot`) fails with `expected hash <12 hex> not found in chain`.
  It is checked against the hashes of the same snapshot. Only a record whose `bot` field equals its
  chain directory counts, and when `--bot` names a chain directory only that chain is searched. A
  copy of the chain placed under another name and reached through an alias does not satisfy the
  check. Together with `--all` it is a usage error
  (exit 2, `verify --contains cannot be combined with --all`).
- `--skip-archives` skips step 7 and prints `(archives not checked)`.
- `--json` prints the result objects as one JSON list (schema below).

`verify --bot` accepts a slug, display name or alias and verifies every chain of that Bot. A name
with no chain on disk is `FAIL … no chain on disk`. `verify --all` also fails for every anchored
chain whose `logs/<bot>` directory has disappeared. `verify --bot X` fails in the same way when X
itself left a witness but `logs/X` is gone, even if alias resolution finds other chains for the
name.

`verify --all` prints one `FAIL  <name>  0 records` block, and exits 1, for each of these. A name is
listed once even when several apply.

| Situation | Problem line |
|---|---|
| `anchors/<bot>.json` records more than zero records, `logs/<bot>` is gone | `chain was anchored on <ts> but logs/<bot> is missing` |
| `anchors/<bot>.history.jsonl` lists at least one head, `logs/<bot>` is gone | `chain was anchored (history, last <ts>) but logs/<bot> is missing` |
| a day rollup `anchors/days/<date>.json` lists the chain with more than zero records, `logs/<bot>` is gone | `chain was anchored (day rollup <date>) but logs/<bot> is missing` |
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
| `archives` | int | number of `archive` records in the chain (0 when the walk already had a problem). A record that names no file is counted too, so the text suffix `(N archived file(s) intact)` can overstate (CHANGELOG, "Known open findings") |
| `archives_checked` | bool | `false` when `--skip-archives` was given |
| `approved_breaks` | int | 0 or 1 |
| `approved` | list | descriptor of the approved break, if any |
| `jay_approval_id` | string or null | id stored on the honoured ack |
| `fixture_ack` | bool | `true` when the honoured ack is the fabricated test fixture (section 9). Always `false` for a live seal |
| `breaks` | list | descriptors of unapproved breaks: the keys of the `break` object in section 6, plus `file_before` when the break spans two day files. A gap of more than 10 000 records has an empty `missing_seqs` and the key `missing_count` |
| `segments` | list | one object per contiguous segment: `seq_start`, `seq_end`, `head`, `line_start`, `line_end`, `file_start`, `file_end`, `records`, `index_start`, `index_end` |
| `state_problem` | string or null | the state-head mismatch text when that problem is in `problems`, otherwise `null`. `state rebuild` uses it to tell this one repairable problem from every other |
| `record_error` | string | only with `--record`, and only when the `verify` record itself could not be appended (tail mismatch). `ok` is not changed by it, but the exit code is 1 |

A synthetic entry has exactly these keys: `bot`, `ok` (`false`), `records` (0), `head` (`null`),
`problems`, `warnings` (empty) and `missing` (`true`).

A chain whose verification stopped on an unexpected internal error also gets an entry, with
`ok: false`, `aborted: true` and one problem that names the error (`verify aborted: <class>[: <text>]`). The other
chains are verified as usual.

## 8. Anchor files and history

`gblog anchor` verifies each chain and writes three things. There is no whole-ledger skip: one FAIL
chain never drops the others. `anchor --skip-archives` runs that verification without re-hashing
archived files, as `verify --skip-archives` does; anchor files and output lines do not change.

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
instead of replacing the file. `gblog anchor` holds `state/.anchors.lock` for its whole run, which
covers the read, the merge and the write (section 5).

**Containment rule (checked by `verify`).** Every head named by `anchors/<bot>.json` (`head` when
`ok`, otherwise each of `segment_heads`) and every `heads` entry in the history file must still be
the `hash` of some record in the chain. So must the heads of the newest day-rollup entry
(`anchors/days/<date>.json`) that names a head for the chain, which keeps a truncation detectable
when only a rollup survives. If not, the problem is
`anchors/<bot>.json: anchored head <12 hex> (<ts>) is no longer in the chain (history rewritten or
tail truncated)`. A corrupt history line is itself a problem. Chains that were never anchored behave
as before.

**Missing chain (checked by `verify --all`).** Both anchor files count as witnesses of a chain whose
log directory is gone: `anchors/<bot>.json` with `records` above zero, and any history line with a
non-empty `heads` list. A day-rollup entry that lists the chain with `records` above zero counts as
well. Section 7 lists the problem lines. A chain that was removed on purpose keeps failing until
its anchor files, its day-rollup entries and its state file are removed too; the README
("Upgrading") gives the procedure.

`gblog anchor` does not destroy such a witness itself. For a chain directory with no log file and
no state file it used to replace `anchors/<bot>.json` with a `records: 0` entry. Since 0.6.1 an
existing anchor that recorded more than zero records is left in place, and the chain is still
reported as FAIL. The same holds for a chain whose day files were emptied while its state file
survives: a FAIL entry that names no head does not replace an anchor that recorded records.

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
- sit after the join. The two records of the join must share one day file; the ack may be in a
  later file;
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

A `break_ack` reaches a chain only through `seal`. `seal` checks again, under the bot lock and
immediately before the append, that the chain holds no `break_ack`; two concurrent runs can
therefore not write two acks, which would be a permanent FAIL. `spool replay` skips a spooled event
of type `break_ack`.

## 10. Reserved bot names

Per-chain files in `state/` are named after the chain, so some names would collide with other files
there:

| Reserved | Collides with |
|---|---|
| the name `aliases` | `state/aliases.json`, `state/aliases.lock` |
| prefix `capture-files-` | `state/capture-files-<bot>.json` (another bot's snapshot) |
| suffix `.alert` | `state/<bot>.alert.json` |
| any name containing `.alert.resolved-` | `state/<bot>.alert.resolved-<stamp>.json` |
| suffix `.lock` | `state/<bot>.lock` |
| suffix `.tmp` | `state/<bot>.json.tmp` |

`append` and `alias add` refuse these names (exit 2) unless `logs/<name>` already exists, so a
legacy chain with such a name keeps appending and verifying. In `append` the check runs right after
the name is sanitised, before the directory tree is created and before the lock is taken. A refused
name therefore writes nothing: `gblog note --bot aliases --detail x` exits 2 and creates neither
`logs/aliases/` nor a `state/aliases.json` that would overwrite the alias registry.

`update_state`, `archive`, `capture browser`, `capture files` and `sync --bot` run the same check
before their first side effect. `capture browser --bot ops.alert` used to create
`state/ops.alert.json`, a false alert for `ops`.

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
| 0 | Success | any write; `verify` with only `OK` / `PASS (N approved break(s))` lines (warnings do not change it); `sync --no-push`; an output pipe that closed while the command was still writing (`gblog export` piped into `head`). A command that had finished keeps its own exit code |
| 1 | Integrity FAIL, not pushed, or an unexpected internal error | `verify` / `anchor` with a FAIL chain; `verify --bot` or `anchor --bot` with no chain on disk; a chain whose logs are missing while its state file, anchor or anchor history survives; an invalid directory name under `logs/`; a `--contains` miss; `verify --record` whose record append was refused; `sync` that committed but did not push; `capture browser` with no readable history; an unexpected internal error (`error: internal error (…)`, see below) |
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

Cases added in 0.6.1 by the security reviews:

- Exit 1: `sync` found that the committed content of a file under `logs/`, `anchors/` or `archive/`
  differs from the file on disk. Nothing is pushed.
- Exit 2: `sync --remote` would change an existing `origin` without `--replace-remote`; a record
  value that `append` refuses (a non-string text field, a non-finite number); a write target that
  is a symlink; `capture files --limit` below 1; control characters in an alias display name or
  variant; a spooled event that `spool replay` will not replay.
- Exit 3: a GitHub URL that cannot be mapped to one repository.

Cases added by the deep reviews:

- Exit 1: `sync` with no `origin` in the ledger repository, or with a ledger directory that is a
  symbolic link (nothing is pushed); an `anchor aborted: <class>` entry; a chain directory that
  cannot be listed; an anchor-history or archive file that is not a regular file.
- Exit 2: a negative `capture browser --hours`; a reserved bot name on `capture`, `archive` or
  `sync --bot`, before anything is written.
- Exit 3: a GitHub URL with a bracketed or percent-encoded host or a `<transport>::` prefix, one
  that cannot be parsed, and the wiki of a public repository.

An unexpected internal error is printed as one line,
`error: internal error (<class>[: <text>]); set GBLOG_DEBUG=1 for a traceback`, and exits 1. It
shares that code with an integrity FAIL on purpose: for `verify` and `anchor`, exit 1 is the
fail-closed answer. In `verify --all` and `anchor` such an error is a FAIL of the chain it happened
in (`verify aborted: <class>[: <text>]`), not the end of the run. With `GBLOG_DEBUG=1` the
exception is raised with its traceback instead, also from inside `verify --all` and `anchor`.

## 12. Deliberate non-features

- **No generic `seal`, no `--force`, no "accept truncation" mode.** The single pinned incident is the
  design. Verify is as strict as seal.
- **No auto-repair that rewrites, trims, quarantines or rehashes log lines**, including a torn final
  line. Recovery is append-only (`state rebuild`, `spool replay`) or a restore from backup.
- **No signatures or HMAC with an on-box key.** The adversary has the same file access; use off-box
  anchors.
- **No change to the canonical JSON, hash algorithm or schema `v`.**
- **No third-party dependencies and no database backend.** The cost of an append does not grow
  with the number of records (one directory listing per append; see section 13), and the JSONL
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
- **No containment check for symlinked directories.** gblog refuses a symlink at the final
  component of a path it writes. It does not refuse a ledger sub-directory that is a symlink to
  another disk, because existing setups rely on that. Do not run write commands on an untrusted
  clone. `sync` cannot back such a directory up, because git stores the link and not the files: it
  says so and pushes nothing (exit 1). Use a mount instead of a link.
- **No future bound in the library.** `Ledger.append` still accepts an explicit future `ts`; tests
  and fixtures use fixed dates. The CLI bound, the warnings in section 2 and the `clock_regressed`
  rule of `report --date` cover the effect.
- **No guard against `git` used directly.** `--replace-remote`, the visibility check and the
  committed-content check protect `gblog sync`. A same-user process can still run `git push`.
- **No FAIL for an `archive` record that names no file.** `gblog log --type archive` writes one, a
  chain cannot be repaired afterwards, and `gblog log` is on the Auto-review allow-list. Such a
  record is a warning, and a problem only with `--strict`.
- **No command that dismisses a `state_adopted` warning.** The marker exists because an append
  erases the "state missing" evidence. The record-level marker is permanent (section 4).
- **No read-only open of the copied browser database.** `capture browser` reads a private copy
  that it deletes afterwards, and read-only mode on a copy with a `-wal` file can fail to open.

## 13. Cost on large ledgers

N is the number of records in a chain, D the number of its day files, C the number of chains and A
the total size of the archived files. The table says what each command reads, so that a later
change can be checked against it. The figures follow from the code; they are not measurements.

| Command | Reads | Cost |
|---|---|---|
| `append` (`task-start`, `log`, `note`, …) | one listing of `logs/<bot>/`; the last line of the newest day file, read backwards in 64 KiB blocks; `state/<bot>.json` | Does not grow with N. One directory listing of D entries per append; older day files are not opened. One fsync each for the day file, the state file and `state/`. `logs/<bot>/` is fsynced as well when the append creates the day file |
| `verify --bot B` | every line of every day file of the chain, once, under the shared lock; the state file and both anchor files; archived files after the lock is released | O(N) time and memory: each line is parsed and hashed once, and the parsed records and their hashes are kept until the result is returned. The raw text of the lines is kept only with `--strict`. Plus O(A) to decompress and re-hash the archives, unless `--skip-archives` is given. The `ledger` chain is searched for the witness note only when a pinned `break_ack` was honoured |
| `verify --all`, `anchor` | the above for each chain, one chain at a time | The sum over all chains. `anchor` verifies before it writes; `anchor --skip-archives` saves the O(A) part |
| `status` | one read of each chain's state file; the day files of the last 30 days; the alert file and the spool directory of each chain | O(records of the last 30 days). Does not grow with older history |
| `report --date D` | the day-D file of each chain; every later day file as raw lines | O(records of day D) parsed. Later files are scanned for `clock_regressed`, and only a line that contains that key is parsed |
| `report --since`, `export --since`, `coverage` | day files from the start date on | O(records since that date), held in memory and sorted. `coverage` compares an observed action only with the self-reports of the same Bot, action kind and normalised target |
| `tail -n k` | per chain, day files newest first, until k readable records were found | O(records of the newest day file) per chain: each day file that is looked at is parsed whole. One day file per chain for a small k |
| `tune --days W` | records from the start of the window or from 90 days ago, whichever is earlier; older day files as raw lines, of which only lines that contain `"tune"` are parsed | O(records in that range), held in memory. Older history costs a raw scan only |
| `sync` | git's own work; one `stat` of every log, anchor-history and archive file; the bytes of the files that changed since the last run | git cost plus O(number of evidence files) plus O(bytes of the changed files). A file whose committed blob id, size, modification time, change time and inode equal the entry that the last run stored in `.git/gblog-sync-cache.json` is not hashed again. `git add -f` is used only for evidence files that an ignore rule hides |
| `export` without `--since` | every record of the selected chains | O(N) memory: the records are loaded and sorted before they are printed |

The daily routine (`docs/routine-daily-audit.md`) re-hashes archived files once per run: step 2
(`gblog verify --all --record`) does it, and the rollback check in step 1 and the `gblog anchor`
call use `--skip-archives`.

`verify` also reads day rollups, newest first, until one names a head for the chain (at most 400
files for a chain that was never anchored). `verify --all` and `anchor` parse every day rollup once
per run for the missing-chain check.

### Estimates for 50 000 and 500 000 records

The figures below come from specialist review K4 (2026-11-16). They were derived from the code and
were **not measured**. Assumptions: CPython 3.8–3.12 on 64-bit Linux, one core of about 3 GHz, a
warm page cache, about 300 bytes per log line and 12 keys per record. A parsed record then takes
2.0–2.4 KB of memory (its key strings are not shared between records), and `verify` spends about
10 µs per record, 40–50 % of it while the shared lock is held. To measure on your own host, run
`python tests/bench_large_ledger.py --records 500000 --keep` and time the commands on the ledger
it keeps.

M30 is the number of records of the last 30 days, S the number of pending spool files and Y the
number of day rollups under `anchors/days/`.

| Command | Complexity | N = 50 000 | N = 500 000 |
|---|---|---|---|
| `append` | O(D) directory entries; 3 fsync calls | 1–3 ms plus fsync | the same |
| `verify --bot B` | O(N) time and O(N) memory; the shared lock is held for the read and the parse, and appends to the chain wait that long | about 0.5 s, 110 MB, lock held 0.25 s | about 5 s, 1.0–1.2 GB, lock held 2.5 s |
| `verify --all`, `anchor` | sum over the chains, plus O(A), plus O(Y) rollups | sum of the row above | sum of the row above |
| daily routine | three walks per chain and run (rollback check, `verify --all --record`, `anchor`) | about 1.5 s per chain | about 15 s per chain, three peaks of about 1 GB |
| `status` | O(M30) records parsed, kept and sorted | M30 = 50 000: about 0.4 s, 110 MB | does not grow with older history |
| `report --date D` | O(records of day D), plus a raw scan of the later day files | small | small for yesterday; a raw scan of O(N) for an old D |
| `report --since`, `coverage`, `tune` | O(records since the date), kept and sorted | about 2.2 KB per record | about 2.2 KB per record |
| `export` | O(N) per selected chain; all selected chains are loaded and sorted before the first line is printed | about 110 MB per chain | about 1.1 GB per chain |
| `tail -n k` | the newest day file of each chain, parsed whole | small | small |
| `tune` | additionally one pass over the Bot's loaded records per proposal and per applied change | seconds with the default 90-day load | minutes with `--days 36500` and about 200 proposals |
| `sync` | git, one `stat` per evidence file, a hash of the changed files | not estimated | not estimated |
| `spool replay` | S × (lines since the refusal day), under the exclusive lock | S = 5 000 over 20 000 lines: about 40 s | S = 5 000 without a usable bound: about 15 minutes |
| `archive` | the whole file in memory | peak about 4–5 times the file size | the same |

The scheduler script `examples/routine-daily-audit.sh` calls `gblog anchor` without
`--skip-archives`, so it re-hashes the archived files twice per run; the routine prompt does it
once.

### Inputs that cost more than their size

Each row describes the current code. The fixes are future work (CHANGELOG, "Known open findings").
The figures are derived, not measured.

| Input | Effect | Id |
|---|---|---|
| a last line of length L without a line break | the tail lookup of `append` copies and searches about L² / 64 KiB bytes while it holds the exclusive lock (tens of seconds for 64 MiB). The tail is then refused, so every later append pays again | K4-03 |
| a log line or side file of any size | it is read whole into memory: no reader has a size bound | K4-03 |
| text with a long run such as `token_token_…` without an assignment, or `eyJ…-` repeated | two redaction rules take quadratic time (seconds at about 12 KB of such text). `append` redacts while it holds the bot lock; `archive` redacts the content before it takes the lock | K4-04 |
| an `archive` record without a usable `bytes` value | the file is decompressed to its end (memory stays bounded; time does not) | K4-05 |
| a growing `archive/` | every `verify` without `--skip-archives` decompresses and hashes all of it | K4-05 |
| thousands of spooled events | `spool replay` scans the chain since the refusal day once per event, and each refusal lists the whole spool directory | K4-07 |
| many open tasks in one report | `report` compares every open task with every task of the Bot (minutes for 20 000 open tasks) | K4-08 |
| a chain with many breaks | `problems`, `breaks` and `segments` have no cap and are written into `anchors/<bot>.json` and the day rollup | K4-12 |

A jump in `seq` is bounded: a gap of more than 10 000 records is described by `missing_count`
(section 7), and a `break_ack` whose list of missing seqs does not have the length of its range is
rejected before a range is built.

Kept as they are for now:

- The chain walk of `verify` is not streamed. The parsed records of one chain stay in memory until
  the result is returned. A streamed walk that gives the same result is future work (K4-02).
- `sync` does not hash a file again when its cache says the file is unchanged. The cache is a
  local file under `.git/`, which the adversary can write. Whether it stays is an open decision
  (D3-05, K4-14).
