# FAQ

Short answers, based on what `gblog.py` in this repository does. For the details behind an answer
see [`docs/DESIGN.md`](DESIGN.md), [`SECURITY.md`](../SECURITY.md) and the
[README](../README.md). Agents and prompt authors should start with
[`docs/agent-integration.md`](agent-integration.md).

## Verification

### 1. What does `OK` from `gblog verify` mean, and what does it not mean?

`OK` means every line of that chain parses, every record's `hash` equals the SHA-256 of its body,
the `prev` / `seq` links are unbroken, timestamps do not go backwards, the surviving witnesses
agree (state head, anchored heads), and archived files still hash to the recorded value.

It does not mean the Bot logged everything. The ledger is self-reported and records are not signed.

### 2. What does `FAIL` mean?

At least one problem was found. The command exits 1 and prints one `      - <problem>` line per
finding. Typical problems:

| Problem text | What happened |
|---|---|
| `hash mismatch (record edited)` | A line was changed after it was written. |
| `broken chain (prev=… expected=…)`, `seq N, expected M (missing or duplicated line)` | A line was deleted, inserted, duplicated or re-ordered. |
| `not valid JSON`, `malformed record fields` | A damaged or hand-written line. |
| `timestamp goes backwards` | A record is dated earlier than its predecessor. |
| `state/<bot>.json: head does not match last record (state tampered or log truncated)` | The log tail and the state file disagree. |
| `anchored head … is no longer in the chain (history rewritten or tail truncated)` | A head recorded by `gblog anchor` (latest anchor, history or newest day rollup) is gone. |
| `<path>: archived file missing`, `content hash mismatch (file modified after archiving)` | An evidence file was removed or changed. |
| `break_ack is not the pinned kim-biseo 2026-11-02 approval` | A forged or misplaced acknowledgement. |
| `no chain on disk for '<name>'` | The name is wrong (a typo, or an unregistered variant). |
| `chain was anchored … but logs/<bot> is missing`, `state/<bot>.json records seq … but logs/<bot> is missing` | A whole chain was deleted while a witness survived. |
| `logs/<dir>: directory name is not a valid chain key; its records are not verified` | A directory under `logs/` that gblog would never create. |
| `<path>: not a regular file`, `logs/<bot>: cannot be listed (…)`, `anchor aborted: …` | A FIFO, device or directory where gblog expects a file (it is never opened), or a chain directory gblog cannot read. |
| `<dir> cannot be listed (…): deleted chains cannot be reported`, under `FAIL  (witness files)` | `state/`, `anchors/` or `anchors/days/` could not be listed. `(witness files)` is not a chain and has no head. |

Report the lines verbatim. Do not edit log files, and do not describe the ledger as verified.

### 3. What does `PASS (1 approved break)` mean?

The chain has one discontinuity that was acknowledged with `gblog seal`. gblog honours exactly one
such acknowledgement: the pinned `kim-biseo` 2026-11-02 gap (seq 1919 → 1921), whose file, lines,
sequence numbers and three full hashes are compiled into `KIM_BISEO_BREAK`. The exit code is 0 and
the line ends with `approved_discontinuities=1`.

`PASS` is not a clean chain; report the count. No other chain can show `PASS`, and any further
break in that chain is `FAIL`.

A test fixture ack is honoured only on a ledger under the system temporary directory. `verify`
then prints `! fixture break_ack honoured: this root is a temporary test copy`.

### 4. What are the lines that start with `!`?

Warnings. They do not change the exit code. Examples:

- `state/<bot>.json missing or unreadable: tail truncation cannot be detected from state`
- `state/<bot>.json was reset to genesis`
- `last record is dated <ts>, in the future: …`
- `<file>:<line>: appended while state/<bot>.json was missing; …`
- `witness note missing on ledger chain`
- `bot field '<x>' does not match chain '<y>'`
- ``state/<bot>.json: seq <n> does not match the last record (seq <m>); appends are refused until
  `gblog state rebuild` is run`` (the operator runs that command, not the Bot)

A warning usually means a witness was lost, so report it verbatim even when the chain says `OK`.

### 5. When do I use `--strict`, `--skip-archives`, `--json`, `--record` and `--contains`?

| Flag | Use |
|---|---|
| `--strict` | Auditing. Also FAILs on a `bot` field that differs from the chain, a `*.jsonl` file that is not named `YYYY-MM-DD.jsonl`, a non-string text field, a non-finite number, a symlink among the chain's files, a `ts` that is not in the normalised form, and any line that is not what gblog writes (surrounding white space is ignored). |
| `--skip-archives` | A quick chain-only check. Archived files are not re-hashed, and the line of a chain that has archive records says `(archives not checked)`. |
| `--json` | Scripts. Prints one JSON list with an entry per chain block; the exit codes are unchanged. |
| `--record` | Appends a `verify` record with the result to each verified chain. |
| `--contains HASH` | Rollback check. Repeatable, needs `--bot`, needs a full 64-hex hash, and cannot be combined with `--all`. |

### 6. The ledger contains a `verify ok` record. Is that proof?

No. Anyone who can write to the ledger can append a hash-valid record of any type, and
`gblog log --type verify` is accepted. Such a record is a claim. Run `gblog verify` again.

## Tail mismatch and recovery

### 7. A command exited 4 with `ALERT tail mismatch`. What happened?

Before appending, gblog compares the last line of the chain's newest day file with
`state/<bot>.json`. They disagreed in a way that an interrupted append cannot explain, so the
append was refused:

- nothing was written to the log;
- the event is in `spool/<bot>/`;
- `state/<bot>.alert.json` holds the expected and the observed values.

gblog sends no mail or chat message. The exit code, the alert file and the `ALERT` line of
`gblog status` are the notification.

### 8. How does the operator recover from a tail mismatch?

Nothing here rewrites a log line. An agent must not run these steps on its own.

1. Look: `gblog spool list --bot B`, `gblog verify --bot B`, and `state/B.alert.json`
   (`expected_seq`, `expected_head`, `observed`).
2. Decide which side is wrong:

   | Situation | Fix |
   |---|---|
   | The chain verifies apart from `state/B.json: head does not match last record`, and state is behind the log or reset to genesis; or `verify` warns that the state `seq` does not match; or the state `head` is not text | `gblog state rebuild --bot B`. It rewrites state from the log tail and appends a `note` saying what state said before. |
   | `state rebuild` answers `… is ahead of the log or names a record that is not in it; restore the log from backup` | The log lost records. Restore the files under `logs/B/` from the backup. |
   | `verify` reports a damaged last line (`not valid JSON`, `hash mismatch`); the alert shows `"corrupt": true` | Restore the day file from the backup. gblog never trims a torn line. |

3. Replay the refused events: `gblog spool replay --bot B`. It prints one line per replayed
   record, moves the files to `spool/B/replayed/` and renames the alert to
   `state/B.alert.resolved-<stamp>.json`.

If `spool replay` exits 4, the tail still does not match; the remaining events stay in the spool.

### 9. Is `notice: recovered state/<bot>.json after an interrupted append` an alert?

No. An earlier append was interrupted after its log line was durable and before state was
replaced. The next append found state exactly one record behind a valid tail and rolled it
forward. Your record was written.

### 10. What about `notice: state/<bot>.json was missing; adopted the log tail (seq N)`?

The state file was gone (or unreadable) and gblog re-created it from the last log line. The append
succeeded, but the state witness was lost: a truncation before that record cannot be ruled out
from state. The record written by that append carries `state_adopted`, and every later `verify`
prints a `!` line for it. When `sync` or `capture browser` re-created the state file, the marker
first sits in the state file, and the next append moves it onto its record. Compare with a head
kept off the computer (question 12).

### 11. `gblog status` shows an `ALERT` for a problem that was fixed long ago. How do I clear it?

Upgrading does not clear old `state/<bot>.alert.json` files. Either replay the spooled events
(`gblog spool replay --bot B`) or dismiss them without replay. The README ("Upgrading", item 3)
has the exact procedure.

## Anchors

### 12. What does `gblog anchor` write, and why do I also need an external anchor?

`gblog anchor` verifies each chain and writes:

- `anchors/<chain>.json`: the latest anchor (status, record count, full head);
- `anchors/<chain>.history.jsonl`: one appended line per run;
- `anchors/days/<UTC date>.json`: a per-day rollup of all chains.

`verify` then FAILs when an anchored head is no longer in the chain (it checks the latest anchor,
the history and the newest day rollup). `verify --all` FAILs for a
chain whose `logs/<chain>` directory is gone while its anchor file, anchor history, day-rollup
entry or state file survives.

These files sit beside the logs, so whoever can rewrite a log can rewrite them too. An external
anchor is a head kept where the ledger's writer cannot change it:

- The daily routine posts every chain's full 64-hex head in the conversation. On the next run it
  checks `gblog verify --bot <chain> --contains <head>`.
- `gblog sync` pushes to a remote that refuses force-pushes.

Read the full head from `anchors/<chain>.json` (`head`, or `segment_heads` for a FAIL chain) or
from `gblog verify --json`. Command output and `gblog status --json` show only 12 characters.

### 13. Can a copy of the chain under another name satisfy `--contains`?

No. A hash counts only when its record's `bot` field names the chain it is stored in. When `--bot`
names a chain directory, only that chain is searched.

### 14. I removed a chain on purpose and `verify --all` keeps failing. Why?

That is what the missing-chain check is for. The chain's witnesses still exist: the anchor files,
the day-rollup entries and the state file. The simplest retirement is to leave the logs in place.
Otherwise follow "Retired chains" in the README ("Upgrading", item 4).

## Moving and restoring

### 15. How do I move a ledger to another directory or computer?

Copy the whole root (`logs/`, `state/`, `anchors/`, `archive/`, `spool/`, `reports/`, `tuning/`
and `.git/`), then point gblog at it with `--root` or `GBLOG_ROOT`. Paths stored in records are
relative to the root, so nothing has to be rewritten. Afterwards:

- Run `gblog verify --all`, and `gblog verify --bot <chain> --contains <head>` with heads you kept
  elsewhere.
- Update the `export GBLOG_ROOT=…` line in your shell startup files. `install.sh` leaves an
  existing line with a different value unchanged and says so.
- Browser-capture markers are keyed by the history file's path. On another computer the first
  `capture browser` run falls back to the `--hours` window.
- The file-capture snapshot records the scanned `--path`. A different path starts a new baseline.

### 16. Can I restore from the git backup?

Yes. Clone the backup repository to the new root and run `gblog verify --all`. Two kinds of files
are not committed, on purpose:

- `state/*.lock`, which are re-created as needed;
- `state/capture-files-*.json`: the next `capture files` run takes a new baseline and logs no
  changes for that run.

Missing empty directories are re-created by the next write command.

### 17. Can I keep a ledger under `/tmp`?

Do not. A root under the system temporary directory counts as a test copy. That is the only place
where the fabricated fixture acknowledgement is honoured (question 3).

## Privacy and redaction

### 18. What does `gblog archive` store, and who can read it?

The content is stored as `archive/<bot>/<UTC date>/<sha256>.gz`, and its hash, size and path are
chained in an `archive` record.

- The files are gzip-compressed, not encrypted. New files are created `0o600` and new directories
  `0o700`. That does not protect against the same user.
- `gblog sync` commits and pushes `archive/`. Whoever can read the backup remote can read the
  archives.
- Content is scanned for secrets before it is hashed, unless `--no-redact` is given.
- Content that is not UTF-8 text and contains a NUL byte (binaries, UTF-16 exports) is stored
  unscanned. gblog prints a warning, and the record carries `scanned: false`.
- Identical content is stored once per Bot and day.

### 19. Can I delete an archived file later?

Not without consequences. `verify` re-hashes every archived file, and a missing file is a FAIL
(`archived file missing`). Decide what to archive before you archive it. The copy in git history
also remains.

### 20. What does redaction cover, and what does it miss?

Redaction is pattern matching on the top-level string fields of a record and on archived text.

- Covered: known token shapes (GitHub, Slack, AWS key ids, Stripe, GitLab, npm, Google, JWTs),
  PEM private-key blocks, `Bearer …` and `Authorization: Basic …`, assignments and JSON keys whose
  name contains `token`, `secret`, `password`, `passwd`, `pwd`, `api_key`, `access_key` or
  `private_key`, flags such as `--password value`, secret query parameters, and `user:pass@` in
  URLs.
- Not covered:
  - secrets with no recognisable name or shape (`mysql -phunter2`, "the password is hunter2");
  - names without a keyword (`PASS=…`, cookies);
  - unquoted values shorter than 6 characters;
  - encoded or split secrets;
  - strings nested inside structured fields;
  - unscanned archive content;
  - private information that is not a "secret" by shape, such as page titles.

The full lists are in [`SECURITY.md`](../SECURITY.md). The primary control is that Bots never
paste credentials.

### 21. A secret reached the ledger. What now?

1. Rotate the secret.
2. Do not edit the log line: that breaks the chain, and the value may already be in git history.
3. Add a `gblog note` describing the exposure.
4. Decide with the operator what to do about the backup repository.

## Sync and remotes

### 22. Where does `gblog sync` send my data?

Nowhere until a remote is configured. `sync` commits `logs/`, `reports/`, `state/`, `tuning/`,
`archive/`, `anchors/` and `spool/` into a git repository at the ledger root and pushes to
`origin`.

- `gblog sync --no-push` commits only and never contacts the network.
- The only network use is the `git push` and, before it, the visibility lookup for GitHub remotes
  (`gh api`, or the public `api.github.com` endpoint).

### 23. Why did `sync` exit 3?

Policy refusal. Every push URL of `origin` is checked:

| Remote | Result |
|---|---|
| GitHub repository reported public | Refused. Override: `--allow-public`. |
| GitHub repository whose visibility could not be confirmed (rate limit, network error, a URL that maps to no repository) | Refused. Override: `--allow-unknown-visibility` or `--allow-public`. |
| GitHub repository confirmed private | Pushed. |
| Any other host | gblog cannot check; it warns on stderr and pushes. |

- GitHub is recognised by the parsed host: `github.com` and any `*.github.com`, on any port, and
  an ssh-config alias that `ssh -G` resolves to one.
- A GitHub URL with a bracketed or percent-encoded host or a `<transport>::` prefix maps to no
  repository and is refused. Write it as `https://github.com/<owner>/<repo>`.
- The wiki of a repository is checked as that repository.
- A push needs an `origin` in the ledger repository itself (question 26).
- The local commit is made even when the push is refused, and a `sync` record with outcome
  `denied` is written (when `--replace-remote` itself is refused with exit 4 because of a tail
  mismatch, nothing is changed and nothing is committed).
- The override flags are per-invocation operator decisions. Never put them in a routine prompt.

### 24. How do I set or change the backup remote?

`gblog sync --remote <url>` sets `origin` the first time. If `origin` already points elsewhere,
the command exits 2 and changes nothing. Add `--replace-remote` to move the backup on purpose.
Every change of `origin` is written to the ledger as a `sync` record with the old and the new URL.

- git stores the URL in `.git/config` as written, so use a credential helper instead of a password
  in the URL.
- Keep secrets out of `-m`.

### 25. `sync` printed `committed content differs from disk … not pushed`. Why?

After committing, `sync` compares the committed bytes of the log files, the anchor-history files
and the archived files with the files on disk. A git filter or an index flag can keep records out
of a commit while git reports success. In that case `sync` pushes nothing and exits 1. Inspect
the git configuration of the ledger repository.

A ledger directory that is a symbolic link gives a message of its own (`… is a symbolic link …;
not pushed`): git stores the link, not the files behind it. Use a mount instead. A symlink in
place of a single day file, anchor history or archived file is reported in the same way
(`<file> is a symbolic link: git stores the link, not the file; not pushed`).

### 26. `sync` says `(not pushed)` and exits 1. What are the usual causes?

- No `origin` is configured in the ledger repository itself. A remote of that name in the global
  git configuration is not used.
- The push was rejected or authentication failed. The error follows `push error:`.
- `detached HEAD; pass --branch`.

A rejected non-fast-forward push can be a sign of a rollback. Never fix it by rewriting history.

## Capture and coverage

### 27. What does the shell hook capture?

`gblog capture shell-hook --bot B --install` appends a `PROMPT_COMMAND` hook to `--rc` (default
`~/.bashrc`), once. Without `--install` it prints the hook. After each command typed at an
interactive bash prompt, the hook runs
`gblog log --action shell --target=<command> --outcome ok|fail --source auto`, with
`$GBLOG_TASK` as the task id and `${GBLOG_BOT:-B}` as the Bot. When gblog refuses the name in
`GBLOG_BOT` (exit 2: unregistered under `alias strict on`, reserved), the hook logs the command
under B, with the detail `GBLOG_BOT=<name> was refused`.

Not captured:

- non-interactive tool shells and zsh;
- commands that bash keeps out of history;
- a command identical to the one just before it;
- whatever history shows at the first prompt of a new interactive shell (it was typed in an
  earlier session);
- a plain `gblog …` call, with or without variables such as `"$BOT"`. A `gblog` command line that
  contains `;`, `&`, `|`, `$(`, a backtick, `<`, `>` or a line break is logged.

The hook discards the output of its own `gblog` call, so an alert caused by a hook record shows up
only in `gblog status`.

### 28. What does `capture browser` read and store?

It copies each history database it finds (Chromium-family `History` files and Firefox
`places.sqlite`; `GBLOG_BROWSER_HISTORY` adds colon-separated glob patterns to that search, and
`--history PATH`, repeatable, reads only the files you name) and imports visits
newer than its per-history marker. The first import of a history file is bounded by `--hours`
(default 24; a value below 1 exits 2). A marker dated more than a day in the future is ignored
with a notice.

Each record stores `scheme://host/path` without query string or fragment, the page title (first
120 characters), the visit time as `observed`, and the profile directory name. Only `http` and
`https` visits are imported.

The command exits 1 when it finds no history database or when every database was unreadable.

### 29. How does `capture files` decide what changed?

It walks `--path` (default `/workspace`) and compares each file's size and modification time with
the snapshot in `state/capture-files-<bot>.json`.

- The walk skips the ledger itself, `.git`, `__pycache__`, `node_modules`, `.cache`, `.venv` and
  the names given with `--exclude`.
- Each difference is logged as `created`, `modified` or `deleted`.
- The first run, a run after the snapshot was lost or became unreadable, and the first run with
  another `--path` (each path keeps a snapshot of its own) take a new baseline and log nothing.
  The output line then ends with
  ` (new baseline: <reason>)`, and for every reason except a first run gblog also says so on
  stderr. Add `--record-baseline` to append an `auto` note to the ledger as well.
- At most `--limit` changes (default 200, minimum 1) are logged per run. The rest stay pending,
  are logged first on the next run, and a note records how many.
- `created` and `modified` records carry the file's modification time as `observed`. Coverage
  matches on that time, not on the time of the capture run.

### 30. What does `gblog coverage` measure?

The share of automatically captured actions (`source: auto`) that the Bot also reported itself.
Two records match when they have the same Bot, the same action kind and the same normalised target
(host, command name or file name) and lie within `--window` minutes (default 30) of each other.
`--days` (default 7) sets the period.

On a shared computer, browser and file activity cannot be attributed to one Bot. A captured record
that another Bot reported is left out of this Bot's count. `--days` selects the captured records;
an older self-report can still match one.

## Tune loop

### 31. How does the tune loop work?

1. `gblog tune --days 28` analyses the ledger. It writes `tuning/<UTC date>.md` and merges the
   proposals into `tuning/proposals.json`. Add `--stdout` to print only.
2. Each proposal has an 8-character id, a category (`skill`, `routine`, `approval`, `description`,
   `memory`, `logging`, `revert`), evidence references and, where possible, a metric with its
   baseline.
3. The Bot drafts the exact edit and the user decides.
4. Record the decision with `gblog tune --apply <id> --detail "what changed"` or
   `gblog tune --reject <id> --detail "why"`. The two flags cannot be combined. The `tune` record
   goes to the proposal's Bot chain.
5. Later reports compare the metric before and after each applied change ("Effect of applied
   changes": `too early`, `improving`, `not helping`). After 7 days without improvement gblog
   proposes a `revert`.

Proposals are heuristics over self-reported data: repeated failures, repeated denials, tasks
started manually on three or more days, recurring 3-step sequences, slow tasks, lessons in notes,
tasks never closed, and coverage below 80%. Failures count the Bot's own reports only, a decision
recorded as `auto` is not an approval by a person, and applying a `revert` proposal reopens the
original one.

### 32. Why did a proposal disappear, or come back?

- An applied proposal moves to `### already applied`.
- A rejected proposal is hidden for 28 days. It returns earlier only when its evidence reaches
  twice the count stored at the time of the rejection.
- `gblog tune --json` always lists every proposal with its `status`.
- Ids that no run has regenerated for 60 days are dropped from `proposals.json`.

### 33. Why does tune propose a routine for something that already is a routine?

The routine's `task-start` lacks `--source routine`. Only tasks started without that flag count as
manual repeats. Fix the routine prompt; do not create a second routine.

## Platform and performance

### 34. Does gblog run on Windows?

Partly, and it is untested there: CI runs on Linux and macOS only.

- `bin/gblog`, `install.sh` and the shell hook are bash. Run `python gblog.py …` directly instead.
- File locking uses `fcntl.flock`. Where `fcntl` is missing, gblog runs without locks, so only one
  writer may run at a time.
- Directory fsync is skipped on Windows.
- Without `/workspace` the default root is `~/activity-log`.

### 35. How does gblog behave on a big ledger?

| Operation | Cost |
|---|---|
| Append (`log`, `note`, …) | Does not grow with the ledger. The tail is found by reading the newest day file backwards in 64 KiB blocks. Each append fsyncs the day file and the state file, and the chain directory when it creates a day file. A command that names a task id also reads day files of the Bot for that id, newest first: today's file for a task opened today, up to 30 days of files for an unknown id (at most 8 MiB of log lines). |
| `verify`, `anchor` | Linear in the chain. Every record is read and re-hashed once; records are not kept in memory. Every archived file is decompressed and re-hashed. `--skip-archives` skips that last part. |
| `report --date D` | Reads the day file for D. It also scans later day files for records whose real wall-clock day was D (`clock_regressed`) and, for tasks that cross midnight, makes raw scans of up to 30 earlier day files (`task_start`) and of the later day files (`task_end`). |
| `status` | Reads the last 30 days of each chain. |
| `tail` | Reads the newest day files of each chain until it has enough records. Use `--bot` to narrow it. |
| `tune` | Reads from the start of the `--days` window or from 90 days ago, whichever is earlier, plus the `tune` records of any age. Use `--bot` to narrow it. |
| `coverage`, `export` | Read from `--days` or `--since` onward. |
| `sync` | git cost, plus a comparison of the log, anchor-history and archive files with the commit. Every such file is hashed on every run; there is no cache of verified files. |

Logs are split into one file per UTC day, so there is nothing to rotate. Do not delete old day
files: the chain walk starts at the first record, and a missing prefix is a FAIL.

The reference for these costs is the table in [`docs/DESIGN.md`](DESIGN.md), section 13.

### 36. Can several Bots write at the same time?

Yes. Each append takes an exclusive `flock` on `state/<bot>.lock`, so writers of the same chain
never interleave, and writers of different chains do not wait for each other. `verify` takes a
shared lock while it reads its snapshot. See question 34 for platforms without `fcntl`.

### 37. Which time zone does gblog use?

UTC everywhere: record timestamps, day files, and `report --date today` or `yesterday`. Do not
pass `--ts` unless you must. gblog exits 2 and writes nothing when `--ts`:

- is not a full timestamp such as `2026-11-16T09:30:00Z`;
- is earlier than the Bot's last record;
- is more than 300 seconds in the future.

If the system clock steps backwards, the new record takes the previous record's timestamp and
carries the real time in `clock_regressed`.

### 38. What does gblog need to run?

Python 3.8 or newer and nothing else: `gblog.py` is a single standard-library script. `sync`
needs `git`, the visibility lookup uses `gh` when it is installed, and `install.sh` needs `bash`
(plus `curl` when it is piped rather than run from a checkout).
