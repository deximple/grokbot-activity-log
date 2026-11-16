# Changelog

All notable changes to `gblog` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The on-disk record format (schema `v: 1`, canonical JSON, SHA-256 hash rule) has not changed in
any release listed here. A ledger written by any version still verifies with the latest one.

Entries for 0.1 to 0.5 are retroactive. They were reconstructed from the code and the README, and
their release dates were not recorded.

## [0.6.1] - Unreleased

Implements the round-2 audit of 2026-11-16. The item ids in parentheses (R-01 … R-25) refer to that
audit. The record format and the hash rule are unchanged. A healthy ledger prints the same
`gblog verify --all` output as with 0.6.0, apart from the new `!` warning lines listed below (for
example for a symlinked `archive/<bot>` directory or a `gblog log --type archive` record).

This release also implements the findings of two adversarial security reviews of that tree. Their
ids are written `A:…` (review A) and `B:…` (review B). Findings that R-01 … R-15 had already closed
are not repeated below: `A:H-1` and `B:H1` (R-01), `A:H-3` and `B:M6` (R-09), `A:M-5` (R-08),
`B:L7` (R-06), and the crash paths of `A:M-3`, `B:M1` and `B:L1` covered by R-05, R-06 and R-10.

A few entries carry an id of the form `W-…`. They come from the round-3 audit of the same day.
Entries with an id of the form `D1-…` to `D6-…` come from the six deep subsystem reviews of this
tree (verify and anchors; append and recovery; sync and remotes; capture, coverage and tune; the
CLI layer; archive, readers and aliases).

### Deep subsystem reviews (D1 … D6)

The round-3 items that this section used to list as open are in the code: the version string, the
`--strict` check of the `ts` form, `capture files --record-baseline`, hook v2, `sync` and invalid
chain directories, `alias list` / `anchor` output, clamped records in one daily report, and the
dead-code removal. Every deep-review finding rated Medium or higher is fixed in this branch:

- **Side files that are not regular files are never opened (D1-01, D6-02).** A FIFO, a device or a
  directory (also behind a symlink) at `state/<bot>.json`, `state/aliases.json`, an anchor file, a
  day rollup or an archive address used to hang or exhaust `verify --all`, `anchor`, `status` and
  `append`. A state, anchor, anchor-history or archive file of that kind is now a `verify` problem;
  elsewhere such a file counts as unreadable.
- **A large `seq` gap is described by its size (D1-02).** A break of more than 10 000 records has an
  empty `missing_seqs` and the additive key `missing_count`. `verify` used to build the whole list.
- **`anchor` isolates each chain completely (D1-03).** A failing pre-check or write for one chain is
  the FAIL entry `anchor aborted: <class>`; the other chains and the day rollup are still written.
- **A chain directory that cannot be listed is FAIL (D1-04)**: `logs/<bot>: cannot be listed
  (<class>)`. Without a state file it used to verify as `OK … 0 records`.
- **Day rollups are checked for containment (D1-05).** The heads of the newest
  `anchors/days/<date>.json` entry of a chain must still be in the chain, so a truncation is
  detected when only a rollup survives.
- **A state file with a wrong `seq` or a non-text `head` can be repaired (D1-06).** `verify` warns
  `state/<bot>.json: seq <n> does not match the last record (seq <m>); appends are refused until
  gblog state rebuild`. `append` treats a `head` that is not a string like an unreadable state file
  (it adopts the tail, with the `state_adopted` marker). `state rebuild` repairs both cases.
- **Two failed state writes in a row no longer brick a chain (D2-01).** A roll-forward is written
  to state before the new line.
- **Reserved bot names are refused before any side effect (D2-02, D5-01)**, also by
  `capture browser`, `capture files`, `archive` and `sync --bot`. `capture browser --bot ops.alert`
  used to exit 0 and create a false `ALERT ops`.
- **Spooled events replay in the order they were refused (D2-03).** Spool files are named
  `<stamp>-<nnnn>-<id>.json`; older names still replay.
- **Public-remote guard (D3-01, D3-02, D3-03).** A GitHub URL with a bracketed or percent-encoded
  host, a `<transport>::` prefix or one that cannot be parsed counts as unconfirmed visibility
  (exit 3). A wiki (`…/repo.wiki.git`) is checked as its repository. Without an `origin` in the
  ledger repository itself nothing is pushed (exit 1); `sync` used to warn and let git resolve the
  name elsewhere.
- **`sync` robustness (D3-04, D3-06, D3-07).** An ignore rule that names a whole ledger directory
  no longer aborts the run; undecodable bytes in git output no longer end in `internal error` after
  a successful push; commits and pushes are never signed.
- **`sync` re-hashes on every run (D3-05).** The cache of already verified files (in this tree
  only, never released) is removed.
- **A symlinked ledger directory is named, not blamed on git (D3-08).** git stores the link, not
  the files behind it. `sync` now says `<dir> is a symbolic link: …; not pushed` and exits 1. 0.6.0
  pushed such a ledger without the files behind the link. Use a mount instead.
- **Capture (D4-01 … D4-05, D5-02).**
  - file records carry `observed` (the file's mtime; `observed_from` on `deleted`), so `file`
    coverage works with a weekly capture run;
  - the shell hook logs nothing at the first prompt of a new interactive shell. Run
    `gblog capture shell-hook --bot <slug> --install` again to get the new hook text;
  - `capture browser --hours` below 1 exits 2, and a marker dated more than a day ahead is ignored
    with a notice;
  - `capture files` keeps one snapshot per `--path` (`state/capture-files-<bot>.<tag>.json` for an
    additional path), and changes left over behind `--limit` are logged first on the next run.
- **Coverage and tune (D4-06 … D4-10).** Failures and the `fail` metric count the Bot's own reports
  only. `auto` decisions are not approvals. Applying a `revert` proposal reopens the original one.
  A captured record that another Bot reported is left out of a Bot's count (`coverage` key
  `elsewhere`). `--days` selects the captured records; an older self-report can still match one.
  Baselines stored before this change may include auto records.
- **A closed stdout pipe no longer turns a finished command's exit code into 0 (D5-03).**
- **The `path` of an archive record is never redacted (D6-01).** For a Bot named like a key prefix
  (`xai-…`, `sk-…`) every archive used to make the chain FAIL for good. `verify` resolves a path
  that was already stored as `archive/[REDACTED]/…` to the chain's own directory.
- **Readers no longer drop a record whose `bot` field is not a string (D6-03).**
- **Aliases (D6-04, D6-05).** A name whose sanitised form is a registered slug resolves to it.
  `alias add` refuses a new slug that equals another Bot's display name.

### Known open findings

Rated below Medium by the deep reviews and **not** in the code yet. 0.6.1 stays "Unreleased" until
they are decided.

- **verify / anchor.** `verify --bot <Bot>` does not report a deleted variant chain whose witness
  survives; `verify --all` does (D1-07). A renamed day file passes `--strict` (D1-08). After a
  pre-0.6 tail with a UTC offset, `append` can write a record that fails `verify` (D1-09).
  `anchor` still replaces `anchors/<bot>.json` with a 0-record entry when day files were emptied
  (D1-10, D3-13). A record key `_corrupt` bricks its chain (D1-12). `(N archived file(s) intact)`
  counts `archive` records that name no file (D1-13; `tests/PENDING.txt`).
- **seal.** `seal` does not preflight the sealed chain's own tail: a mismatch spools a `break_ack`
  that `spool replay` never appends. An ack without its witness cannot be completed (D1-11, D2-08).
- **append / recovery.** The `ALERT` line shows the latest refusal, and a spool without an alert
  file prints none (D2-06). Capture events refused during an alert are imported again after
  recovery (D2-07, D4-17). An interrupted first append leaves a `state_adopted` marker (D2-09).
  `verify` reports FAIL, without a hint, while state is one record behind (D2-10). Directory fsync
  (D2-11), `state rebuild` without its note (D2-12), a removed lock file (D2-13), side files taken
  for chains by `verify --bot` (D2-14, D4-11).
- **sync.** `GIT_DIR` and `GIT_CONFIG_COUNT` are inherited (D3-09). `origin` can change before its
  record is written (D3-10). Multi-URL origins and `pushurl` (D3-11). A token used as URL user name
  (D3-12). `NAME=value` redaction stops at `&` or a quote (D3-14).
- **capture / tune.** `--history` spelling and non-database files (D4-12), a symlinked `--path`
  (D4-15), an excluded file logged as `deleted` (D4-16), ports dropped from browser targets
  (D4-19), `$?` after the hook and operator commands at a prompt (D4-14, D4-21), task bookkeeping
  commands counted as unreported (D4-18), proposal caps, decisions after `alias add` and changes
  older than 90 days (D4-13, D4-20, D4-22).
- **CLI and readers.** `verify --record` stops at an `OSError` (D5-04). Dates are checked by shape
  only (D5-05). `spool replay --bot <typo>` exits 0 (D5-06). `tune --bot` overwrites the all-Bots
  tuning file (D5-07). Raw control characters in `ALERT tail mismatch`, `error:`, `ANCHOR FAIL` and
  git error lines (D5-08). `~` in the root is not expanded (D5-12). `chains_for` and `alias list`
  versus `resolve` (D6-06). Report labels, task detail, the twin hint and the `(no task)` bucket
  (D6-07 … D6-10). `archive` argument edges (D6-13). `report --date` with `--since` (D6-15).

Future work (design changes, not planned for 0.6.1):

- `seal` rerun with the same approval id adds only a missing witness note. This changes the live
  procedure and needs the operator's sign-off.
- Capture events raise the alert without being spooled.
- A genesis state file written before a chain's first line.
- Records bound to their day file, as a `--strict` check.
- `sync` with a scrubbed git environment and a lock of its own (`state/.sync.lock`).

### Behaviour changes

Read these before upgrading a routine or a script that branches on output or exit codes.

- **An emptied or deleted chain no longer verifies while a witness survives (R-01).**
  - `verify` applies the state-head check even when the walk found no records. A chain whose day
    files were emptied while `state/<bot>.json` names a head is now FAIL. It used to print
    `OK … 0 records  head genesis`.
  - `verify --all` prints `FAIL  <bot>  0 records` for a name without a `logs/<bot>` directory whose
    `state/<bot>.json` still holds a head, or whose `anchors/<bot>.history.jsonl` still lists one.
    0.6.0 looked at `anchors/<bot>.json` only.
  - To retire a chain on purpose, see "Upgrading" in the README.
- **Reserved bot names are refused on append (R-02).** `gblog note --bot aliases …` exits 2 and
  writes nothing. 0.6.0 refused these names only in `alias add`, so the command above overwrote the
  alias registry.
- **Invalid chain directories (R-04).** A directory under `logs/` whose name is not a valid chain
  key (`logs/my bot`, `logs/ops.`, `logs/---`) is FAIL in `verify --all`. Readers skip it with a
  warning on stderr. It used to be read by reports without being verified, and `logs/---` made
  `status` and `verify --all` exit 2.
- **Records are attributed by chain directory (R-04).** `report`, `status`, `tail`, `coverage` and
  `tune` list a record under the chain it is stored in, even when its `bot` field names another
  Bot. `export` still prints records as stored.
- **An unreadable `state/<bot>.json` is treated like a missing one (R-07).** `append` adopts a
  self-consistent tail and prints `notice: state/<bot>.json was unreadable; adopted the log tail
  (seq <n>)`. It used to refuse with exit 4. A corrupt tail is still refused.
- **`verify --contains` with `--all` exits 2 (R-08).** It used to search every chain.
- **The fixture `break_ack` is pinned by hash (R-09).** On a temporary copy, an ack at the pinned
  coordinates with any other hashes is now FAIL. `Ledger.seal_break(copy_fixture=True)` refuses any
  descriptor other than the committed fixture.
- **An unreadable `state/aliases.json` is reported (R-10).** gblog warns on stderr, resolves no
  names and treats strict mode as off until the file is restored. `alias add`, `alias remove` and
  `alias strict` refuse to overwrite it (exit 2). It used to be treated as an empty registry
  without a message.
- `alias add` compares case-insensitively when it decides whether `--move` is needed (R-10).
- `state rebuild` on a chain whose state already matches the tail prints
  `state/<bot>.json already matches the log tail; nothing to do` and writes nothing. It used to
  rewrite state and append a note. The `rebuilt …` line now shows the seq and head that state holds
  after the note (R-13).
- `archive --no-redact` records carry `scanned: false` (R-13).
- `archive --ts` earlier than the Bot's last record stores no file. It used to leave the `.gz`
  behind (R-13).
- `sync` commits `<root>/.gitignore` and removes `state/*.lock` and `state/capture-files-*.json`
  from the git index. The files stay on disk (R-14).
- `status` can print two new kinds of line after the table: `CLOCK <chain>: last record dated <ts>
  (future)`, and `ALERT` lines for chains that have an alert or spool files but no log directory
  (R-03, R-13).
- New stderr lines (stdout is unchanged):
  - `warning: last record of <bot> is dated <ts> (future); this record is stamped with it` (R-03);
  - `warning: skipped <n> unreadable line(s) in logs/<chain>; run gblog verify` (R-06);
  - `warning: ignoring logs/<dir> (invalid chain directory name); run gblog verify --all` (R-04);
  - `notice: state/<bot>.json was missing; adopted the log tail (seq <n>)` (R-07).
- Tuning Markdown has an empty line before `### already applied` (R-11).
- `tune` reads records from the start of the `--days` window or from 90 days ago, whichever is
  earlier, plus the `tune` records of any age. Decisions (`applied`, `rejected`) therefore still
  count however old they are. The "Effect of applied changes" table and `revert` proposals cover
  only changes applied since that start (the last 90 days with the default `--days 28`) (W-11).
- **`sync --remote` no longer replaces an existing `origin` (B:H3).** When `origin` already points
  somewhere else, `gblog sync --remote <url>` exits 2 and changes nothing. Pass `--replace-remote`
  to move the backup on purpose. Setting the first `origin`, or repeating the current URL, works as
  before. Every change of `origin` is written to the ledger as a `sync` record with the old and the
  new URL (credentials removed). In 0.6.0 any `gblog` caller could silently and permanently re-point
  the backup to another host.
- **`sync --re` is ambiguous and exits 2.** The new flag `--replace-remote` shares that prefix with
  `--remote`. `--rem` still works. `--r` on `sync` now also matches `--replace-remote`.
- **More GitHub URLs are checked by the public-remote guard (A:H-2, B:H2).** A remote is classified
  by its parsed host, not by a pattern over the URL text.
  - `github.com`, `www.github.com`, `ssh.github.com` and any other `*.github.com` host count as
    GitHub, on any port and with or without a trailing dot. An ssh host alias that `ssh -G`
    resolves to such a host counts too. 0.6.0 pushed to `https://github.com:443/o/r.git` or
    `ssh://git@ssh.github.com:443/o/r.git` after only a warning; these are now refused with exit 3
    when the repository is public or its visibility cannot be confirmed.
  - A GitHub URL that cannot be mapped to one repository (a gist, an unexpected path) counts as
    unconfirmed visibility and is refused with exit 3.
  - A URL that merely contains `github.com` in its path (`https://evil.example/x//github.com/o/r`)
    is "another host". 0.6.0 looked up `o/r` on GitHub and could record the push as going to a
    private repository.
- **`sync` checks what it commits (B:M4).** gblog's own files are added even when a git ignore rule
  would hide them, git hooks do not run, and after the commit the log files, the anchor-history
  files and the archived files are compared with the committed content. When they differ (a clean filter, an
  index flag) `sync` reports an error, pushes nothing and exits 1. `sync` also creates
  `<root>/.gitignore` when it is missing.
- **`verify --contains` is chain-exact (B:M3).** A hash counts only when it belongs to a record
  whose `bot` field equals the chain directory it is stored in. When `--bot` names a chain
  directory, only that chain is searched. `verify --bot X` also FAILs when X itself left a witness
  but `logs/X` is gone, even if an alias resolves the name to other chains. In 0.6.0 a copy of the
  chain under another name, reached through `alias add`, satisfied the daily rollback check.
- **A day rollup is a witness of a deleted chain (B:M2).** `verify --all` FAILs for a name that an
  `anchors/days/*.json` rollup lists with more than zero records while `logs/<name>` is missing.
  The README ("Upgrading") has the extra step for a chain that was retired on purpose.
- **`anchor` no longer erases a witness (A:H-1).** For a chain directory without log files and
  without a state file, `anchor` used to replace `anchors/<bot>.json` with a `records: 0` entry. An
  existing anchor that recorded more than zero records is now left in place.
- **`append` refuses values it used to store (A:M-2, B:L10).** A record whose text field (`type`,
  `task_id`, `task`, `action`, `target`, `outcome`, `detail`, `source`, `decision`, `observed`,
  `kind`, …) is not a string, or that contains `NaN` or `Infinity`, is refused before anything is
  written (exit 2). `spool replay` leaves such a spooled event in place and lists it as skipped.
  Records already on disk are unaffected.
- **Symlinks are not followed on write (A:M-4, B:L8).** Day files, state, anchor, report, tuning,
  snapshot, archive and lock files are opened without following a symlink at the final path
  component, and temporary files get per-process names (`<path>.<pid>.tmp`). A planted symlink at a
  day file, an anchor history, a lock file, a report or a tuning file makes the command exit 2. A
  symlink at a file that gblog replaces atomically (state, anchor, rollup, snapshot, archive) is
  replaced by a regular file. Nothing is written through either.
- **A `*.jsonl` entry that is not a regular file is FAIL (B:L2).** A FIFO, directory or device in a
  chain directory is reported by `verify` and never opened. It used to hang `append` (while holding
  the lock) and `verify --all`.
- **Archive records are checked more strictly (B:L4).** `verify` FAILs an `archive` record that has
  a `sha256` but no `path`, a `path` but no 64-hex `sha256`, or a path whose file name is not
  `<sha256>.gz`. A record that names neither (as `gblog log --type archive` writes) is a `!`
  warning, and a problem with `--strict`: it is a claim that no stored file backs. Two records that
  name the same path with different hashes are both checked. `archive` re-hashes an existing file
  before it treats new content as "already stored", and rewrites it when it does not match.
- `alias add` refuses a display name or variant that contains control characters or line breaks
  (exit 2) (B:M7).
- Bot names containing `.alert.resolved-` are reserved for new chains (A:L-5).
- `spool replay` never appends a spooled event of type `break_ack`; the file is left in place and
  listed as skipped (A:L-5).
- `capture files` (A:L-2, B:L6):
  - `--limit` below 1 exits 2;
  - a run with more changes than `--limit` no longer forgets the changes it did not list;
  - a new baseline is announced with a `notice:` line on stderr: for an unreadable snapshot, a
    changed `--path`, and a snapshot that is missing although the chain already holds
    file-capture records. The stdout line keeps its `files: <n> change(s) …` prefix and ends with
    ` (new baseline: <reason>)`, and with `, <n> pending` when changes were left over.
    `--record-baseline` also appends an `auto` note (W-07).
- The shell hook skips only a plain `gblog …` command. A command line that merely contains the word
  `gblog`, or chains it with `;`, `&`, `|`, a redirection or a command substitution (`$( … )`,
  backticks), is logged. A plain command may use variables (`--bot "$BOT"`). A command that starts
  with `-` is logged instead of being dropped. The hook text is marked `# gblog hook v2`. An upgrade
  does not touch a hook that is already installed: run `gblog capture shell-hook --bot <slug>
  --install` again, which replaces an older hook block in place; see "Upgrading" in the README
  (A:L-2, B:L5, W-16).
- `report --date D` also lists records whose `clock_regressed` value (the real wall-clock time)
  falls on day D, so activity written after a future-dated record stays in the daily report
  (A:M-6, B:L3).
- A record that was appended while `state/<bot>.json` was missing or unreadable carries the new
  field `state_adopted`, and `verify` warns about every such record. In 0.6.0 the "state missing"
  warning disappeared with the first append (B:M8).
  `sync` and `capture browser` append no record when they re-create state from the log tail. They
  write the key `state_adopted` (`how`, `seq`, `ts`) into `state/<bot>.json` instead, and `verify`
  warns about that key too. The next `append` to the chain moves that marker onto its record and
  drops the key from state (W-15). The record field is permanent.
  Report the `!` line once with its cause (`docs/DESIGN.md`, section 4).
- Terminal output (`tail`, `status`, `spool list`, `alias list`, `verify` problem and warning lines)
  no longer contains raw control characters taken from records, file names or display names. Text
  without control characters prints as before (A:L-1, B:M7).
- New `!` warnings in `verify`: a non-string text field, a non-standard number, a line with
  duplicate keys, a symlink among the chain's files, an `archive` record that names neither a
  `path` nor a `sha256`, and a `state_adopted` marker (on a record or in state). `--strict` turns
  all but the last into problems (A:M-2, A:M-4, A:L-5, B:L4, B:M7, B:M8, B:L10). A line that is
  only reformatted is still silent without `--strict`.
- An unexpected internal error is printed as one `error:` line with a non-zero exit code. Set
  `GBLOG_DEBUG=1` to see the traceback (A:M-3).

### Security

- **State is a witness for an empty or missing chain (R-01).** See above. This restores the
  guarantee stated in `SECURITY.md`.
- **Future-dated tail is signalled (R-03).** `verify` warns
  `last record is dated <ts>, in the future: new records are stamped with that time (see
  clock_regressed)`. The clamp and the exit codes do not change.
- **Unverified directories no longer reach reports (R-04).**
- **`TMPDIR` can no longer widen the fixture rule (R-09).** `KIM_BISEO_FIXTURE_BREAK` pins the three
  fixture hashes. A fixture ack that is honoured prints
  `! fixture break_ack honoured: this root is a temporary test copy`.
- **Redaction (R-12).** The JSON-key rules also cover keys ending in `pwd` and `access_key`
  (`{"db_pwd": "…"}`, `{"SecretAccessKey": "…"}`), as the documentation already said. No pattern was
  removed.
- **Private files (R-14).** Lock files are created `0o600`; `state/` and `spool/<bot>/replayed/`
  are created `0o700`.
- **Threat model (B, A:M-4).** `SECURITY.md` and `docs/DESIGN.md` now name two more adversary
  positions: a prompt-injected Bot that can run only auto-approved `gblog` commands ("CLI-only"),
  and the author of a backup tree that an auditor clones onto another computer. The README
  allow-list for Auto-review is narrower: `sync --remote`, `--replace-remote`, `archive --no-redact`,
  `archive --file` outside the transcript directory and `capture shell-hook --install` are never
  auto-allowed.
- **Backups cannot be redirected or blinded through `gblog` alone (B:H3, B:M4).** See "Behaviour
  changes": `--replace-remote`, the recorded `origin` change, force-added files, disabled hooks and
  the committed-content check.
- **Public-remote guard (A:H-2, B:H2, A:L-5).** Besides the host parsing above: the `gh` lookup
  always names github.com, so `GH_HOST` cannot redirect it, and an unexpected answer from the API
  (a non-object body, a broken response) counts as unconfirmed visibility.
- **One chain cannot hide the others (A:M-3, B:M1).** An unexpected error while one chain is
  verified or anchored becomes a FAIL entry for that chain. `verify --all` and `anchor` continue
  with the remaining chains.
- **Redaction (A:M-1, B:M5, A:L-4, A:L-5, B:L9).** Added, with no pattern removed:
  - names with a secret word glued in or in camelCase (`dbPassword=…`, `accessToken: …`,
    `secretKey=…`, `PASSWORD2=…`, `APITOKEN=…`, `userpwd=…`). A counter is left as written: a name
    that ends in `tokens` with a value of only digits, `_`, `.` or `,` (`max_tokens=100000`,
    `prompt_tokens: 123456`) (W-17);
  - secret keys in a Python `dict` repr (`'password': '…'`);
  - prefixed flags (`--db-password …`, `--client-secret …`, `--auth-token=…`);
  - a PEM private-key block that was cut off before its `END` line. The PEM rule no longer takes
    quadratic time on input with many `BEGIN` markers;
  - the `sync -m` commit message;
  - git error text and captured page titles are redacted before they are shortened, so a cut can
    no longer leave part of a secret behind;
  - archived text that is UTF-8 apart from stray invalid bytes is scanned, and those bytes are
    stored unchanged. Content with a NUL byte is still stored unscanned (`scanned: false`).
- **Untrusted text (A:L-1, B:M7).** Reports and tuning files also neutralise display names, chain
  names and control characters (including ESC), not only `detail` and `target`.
- `sync --remote` with a user name and password in an http(s) URL prints a warning: git stores the
  URL in `.git/config` as written (B:L9).
- `install.sh` no longer follows a redirect from an `https://` download URL to another scheme
  (A:L-5).

### Added

- `status --json` rows gain `future_tail` (R-03).
- `verify --json` has an entry for every FAIL, including a name with no chain on disk and a chain
  whose logs are missing (`"missing": true`) (R-08).
- `tune` records store `count` in the `proposal` snapshot (R-11).
- In-memory record key `bot_field` (R-04).
- `sync --replace-remote` (B:H3).
- `anchor --skip-archives` (W-12), `capture files --record-baseline` (W-07), and the key
  `orphan_alerts` in `status --json` (W-14).
- Additive keys from the deep reviews: `missing_count` in break descriptors (D1-02), `elsewhere`
  in `coverage --json` (D4-09), and the record fields `observed` / `observed_from` on file-capture
  records (D4-01).
- Record field `state_adopted` (`missing` or `unreadable`), and the key `state_adopted` in
  `state/<bot>.json` (B:M8).
- `GBLOG_DEBUG` environment variable (A:M-3).
- `state/.anchors.lock`, held for the whole run of `gblog anchor`, so that two concurrent runs
  cannot lose each other's day-rollup entries (A:L-3, B:L8). The leading dot keeps it apart from
  the lock file of a Bot named `anchors`.
- `SECURITY.md`: "Auditing a clone of the backup" (A:M-4).

### Changed

- `verify` takes one snapshot of the log, state and anchor files under the shared lock and uses one
  parser for every check, including `--strict` and `--contains` (R-05).
- `verify --json` is rendered from the same result list as the text output (R-08).
- `alias add`, `alias remove` and `alias strict` read, modify and write the registry in one hold of
  `aliases.lock` (R-10).
- A rejected tuning proposal stays hidden for 28 days unless its evidence reaches twice the count
  stored at decision time. Evidence ageing out of the window no longer brings it back early (R-11).
- `task-end` looks for the matching `task-start` newest day first and stops at the first hit (R-13).
- Atomic JSON writes use a per-process temporary file name (R-14).
- State, alias-registry, proposal, snapshot and archive writes use per-process temporary file names
  (`<path>.<pid>.tmp`) (A:L-3, B:L8).
- `capture browser` keeps one marker per history file. When an append fails part-way through an
  import, the markers of the rows already imported are saved first, so the next run does not
  import them again (A:L-2).

### Fixed

- A concurrent `append` followed by `anchor` no longer makes a running `verify` report
  `anchored head … is no longer in the chain` (R-05).
- `verify --bot <unknown> --json` printed `[]` with exit 1 (R-08).
- Crash paths (R-05, R-06, R-10). Each of these used to end in a traceback:
  - a deeply nested JSON line in a log file;
  - an archive with a corrupted gzip stream (now `<path>: unreadable (…)`);
  - wrong-typed `seq`, `head`, `updated` or `last_sync` in a state file (`status`);
  - a `state/aliases.json` that is not an object;
  - records with a lone surrogate, or a non-ASCII display name, written to an ASCII stream;
  - non-string `detail`, `target`, `task` or `task_id` in a hash-valid record;
  - an unusable browser-capture marker;
  - a `proposals.json` entry without `bot`.
- Readers no longer skip unreadable lines without saying so (R-06).
- Concurrency (A:L-3, B:L8, B:L11):
  - two concurrent `seal` runs could both append a `break_ack`, a permanent FAIL. `seal` now checks
    again under the bot lock immediately before the append;
  - two concurrent `anchor` runs could lose each other's day-rollup entries;
  - two concurrent `spool replay` runs could append the same spooled event twice;
  - a write that fails part-way (for example `ENOSPC`) is rolled back to the size the file had
    before the call, so it leaves no torn anchor-history line or log tail behind.
- More crash paths (A:M-3, A:L-2, A:L-5). Each of these used to end in a traceback:
  - a deeply nested `anchors/<bot>.json` or day rollup;
  - a browser-history row with a non-text URL or title, or an out-of-range visit time. The same
    rows were then imported again on every later run;
  - an unexpected response body or a broken HTTP response during the visibility check.

### Documentation

- `docs/DESIGN.md`, README, `SECURITY.md`, both skills, both routine prompts and the template docs
  were brought in line with the code (R-22 … R-25). In particular: exit 2 no longer claims that
  nothing was written, the Auto-review rule is an allow-list, and browser and file capture run in
  the weekly tune routine only.
- Threat model, guarantees, non-guarantees, redaction lists and the private-remote policy were
  updated for the security-review fixes above (`SECURITY.md`, `docs/DESIGN.md`, README).
- `docs/DESIGN.md` describes `state/.anchors.lock` as it is held, quotes the `verify` problem and
  warning texts, and has a new section "Cost on a large ledger" (W-27, W-29).
- The daily audit prompt re-hashes archived files once per run: the rollback check and `anchor`
  use `--skip-archives`, and `verify --all --record` stays the full check (W-28).
- Round-3 documentation pass (W-22, W-23). Claims that the code does not meet yet were moved to
  "Known open findings" above. Corrected: the lock name `state/.anchors.lock` and what it covers,
  the two `state_adopted` markers and their permanence, the archive-record rules, the day-rollup
  problem text and the `verify` warning list. Step 1 of the daily routine now uses
  `--skip-archives`.
- Deep-review documentation pass (D1-14, D2-16, D3-15, D4-24, D5-16, D6-16). Removed: the stale
  "Known open findings", and contradictory text about `state_adopted` permanence, archive
  scanning, symlinks at written paths, the closed-pipe exit code, the `sync` comparison and the
  `capture files` output. The weekly routine passes `--hours 168` and `--exclude transcripts`.

## [0.6.0] - 2026-11-16

Implements the 2026-11-16 audit. The item ids in parentheses (I-01 … I-42) refer to that audit.

### Behaviour changes

Read these before upgrading a routine or a script that branches on output or exit codes.

- **`verify` of an unknown bot now exits 1.** `gblog verify --bot <name>` for a name with no chain on
  disk prints `FAIL  <name>  0 records` and `- no chain on disk for '<name>'`. It used to print
  `OK … 0 records  head genesis` and exit 0. `--record` no longer creates a junk chain for such a
  name. `anchor --bot <name>` behaves the same way and writes no `anchors/<name>.json` (I-06).
- **Usage errors now exit 2.** Every `error: …` message (unregistered name under `alias strict on`,
  alias conflicts, an empty bot name, a reserved bot name, an invalid date or `--ts`, an unreadable
  `--file`, a corrupt side file) is written to stderr and exits 2. These used to exit 1, the same
  code as a verify FAIL. `OSError` exits 2 with a one-line message, Ctrl-C exits 130, and a closed
  pipe (`gblog export | head`) exits 0, all without a traceback (I-07).
- **An unknown-visibility GitHub push is refused.** When `sync` cannot confirm that a github.com
  remote is private (rate limit, network error, timeout) it refuses the push with exit 3. It used to
  warn and push. Override with `--allow-unknown-visibility` (or `--allow-public`). Non-GitHub hosts
  still only warn (I-12).
- **An explicit past `--ts` is refused.** A `--ts` earlier than the last record of that Bot exits 2
  and writes nothing. A `--ts` that is not a timestamp, or is more than 300 s in the future, also
  exits 2 (I-04).
- A `break_ack` that is not the pinned kim-biseo 2026-11-02 approval now makes `verify` FAIL. It used
  to be accepted on any chain (I-01).
- `verify` FAILs when a head recorded in `anchors/<bot>.json` or `anchors/<bot>.history.jsonl` is no
  longer in the chain. `verify --all` FAILs for an anchored chain whose `logs/<bot>` directory is
  missing (I-02).
- `verify --bot`, `anchor --bot`, `export --bot` and `sync --bot` now resolve slugs, display names
  and aliases like the write commands do. `verify --bot "Chief of Staff"` checks every variant chain
  of that Bot (I-06).
- `alias add` refuses a variant that is already another Bot's alias unless `--move` is given. It used
  to move the alias silently (I-19).
- New chains cannot use the reserved bot names: `aliases`, anything starting with `capture-files-`,
  anything ending in `.alert`, `.lock` or `.tmp`. Existing chains with such names keep working (I-19).
  Correction: in 0.6.0 only `alias add` enforced this. `append` enforces it from 0.6.1.
- `seal` rejects more placeholder approval ids, case-insensitively: `pending-jay`, `pending`, `tbd`,
  `todo`, `placeholder`, `<id>`, and any id shorter than 4 characters (I-01).
- `sync` exits 4 when one of its own bookkeeping appends was refused by the tail check. It still
  commits and pushes first (I-11).
- `capture files` with a non-existent `--path` exits 2 and logs nothing. It used to log every known
  file as deleted (I-17).
- `capture browser` resumes from its per-history marker even when that is older than `--hours`
  (I-16).
- `tune --apply X --reject Y` is a usage error (exit 2). It used to record X as rejected (I-13).
- `tail -n 0` prints nothing. It used to print the whole ledger (I-13).
- Some prefix abbreviations of flags are now ambiguous and exit 2. Spell the flag out (I-12, I-13):
  - `--s` on `log` (`--source` / `--summary`), on `archive` (was `--stdin`) and on `tune` (was
    `--stdout`);
  - `--r` on `verify` (was `--record`), `sync` (was `--remote`), `tune` (was `--reject`) and
    `capture shell-hook` (was `--rc`), because every subcommand now accepts `--root`;
  - `--a`, `--al` and `--allow` on `sync` (`--allow-public` / `--allow-unknown-visibility`).
- `gblog init` writes `.gitignore`, but `state/*.lock` and `state/capture-files-*.json` files that
  an existing backup repository already tracks stay tracked until they are removed from the index.
  The README ("Upgrading") has the command; 0.6.1 `sync` does it (I-22).
- `status` prints an `ALERT` line for every `state/<bot>.alert.json`, including stale files left by
  0.4 or 0.5. The README ("Upgrading") says how to dismiss one without replaying it (I-05).
- A chain that was retired by removing `logs/<bot>` while `anchors/<bot>.json` remains makes
  `verify --all` FAIL until its anchor files are removed. The README ("Upgrading") has the
  procedure (I-02).
- New ledger files are created `0o600` and new directories `0o700`. Existing files are not changed
  (I-22).
- Report, tune and coverage Markdown collapses newlines and tabs in ledger-derived text to one
  space, escapes `|`, and truncates at 500 characters. Single-line values without `|` render exactly
  as before (I-15).

### Security

- **Forged `break_ack` no longer passes (I-01).** `verify` honours an ack only on the `kim-biseo`
  chain, and only when its descriptor equals the pinned incident, or when the ledger root is a
  temporary copy and the six coordinates equal the pinned ones. Any other ack is reported as
  `break_ack is not the pinned kim-biseo 2026-11-02 approval`. `_is_temp_copy` resolves symlinks.
  `seal` preflights the `ledger` witness chain and refuses before writing anything if that chain has
  a tail mismatch. `verify` warns `witness note missing on ledger chain` when it applies.
- **Rollback, truncation and deletion detection (I-02).** Anchor containment check; append-only
  `anchors/<bot>.history.jsonl`; missing-chain check in `verify --all`; `verify --contains HASH`;
  warnings when state is missing, unreadable or reset to genesis.
- **`--ts` can no longer escape the ledger root (I-04).** Timestamps are parsed and normalised
  before they are used in a file name, in `append` and in `archive`.
- **Redaction covers more shapes (I-08).** Added, with no pattern removed:
  - env-style names (`DB_PASSWORD=…`, `AWS_SECRET_ACCESS_KEY=…`), including quoted values;
  - JSON keys (`"password": "…"`), also inside an escaped JSON string;
  - CLI flags (`--password value`, `--token=…`);
  - `Authorization: Basic …`;
  - Stripe (`sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`), GitLab (`glpat-`), npm (`npm_`), Google
    OAuth (`ya29.`), Slack app tokens (`xapp-`) and Slack webhook URLs.
- **Public-remote guard (I-12).** The GitHub host match is anchored, so `evilgithub.com` is not
  treated as GitHub. Every push URL of `origin` is checked, including `pushurl`. Unknown visibility
  on github.com fails closed. Error text is passed through redaction before it is printed or logged.
- **Untrusted text in reports (I-15).** Reports and tuning files neutralise newlines and pipes, and
  their footers say that text taken from records is untrusted data.
- **Archive (I-20).** `verify` rejects an archive record whose `path` is absolute, contains `..` or
  does not start with `archive/`. Binary or non-UTF-8 content stored without scanning is marked
  `scanned: false` and a warning is printed.

### Added

- `gblog state rebuild --bot B`: rebuild `state/<bot>.json` from the log tail when state is missing,
  unreadable, reset to genesis or behind the log. Refused when state is ahead of the log (I-05).
- `gblog spool list [--bot B]` and `gblog spool replay --bot B`: list and re-append events that a
  tail mismatch refused. A fully replayed spool resolves `state/<bot>.alert.json` (I-05).
- `status`: JSON rows gain `alert` and `spooled`. Text output adds
  `ALERT <bot>: tail mismatch since <ts> (<n> spooled)` lines. `status --bot NAME` filters (I-05,
  I-13).
- `verify` flags (I-02, I-13, I-20, I-21):
  - `--contains HASH` (repeatable, needs `--bot`);
  - `--strict`;
  - `--skip-archives`;
  - `--json`.
- `verify` prints warnings as `      ! <text>` lines. Results gain the keys `warnings` and
  `fixture_ack` (I-01, I-02, I-21).
- `--summary` as an alias of `--detail` on every logging command and on `tune` (I-13).
- `--source` on `task-start`, `note`, `approval` and `task-end`. Routine runs should pass
  `--source routine` (I-13).
- `approval`, `note` and `task-end` fall back to `$GBLOG_TASK` when `--task-id` is absent (I-13).
- `--outcome` accepts synonyms (I-13):
  - `success`, `succeeded`, `done`, `complete`, `completed`, `pass`, `passed` → `ok`;
  - `failed`, `failure`, `error` → `fail`;
  - `cancelled`, `canceled`, `skip` → `skipped`;
  - `blocked` → `denied`.
- `--root` is accepted after the subcommand as well as before it (I-13).
- `report --date today|yesterday` (UTC days) (I-04).
- `sync --allow-unknown-visibility` (I-12).
- `alias add --move` (I-19).
- New record fields: `clock_regressed`, `spooled_at`, `spool_id`, `original_ts`, `scanned` (I-04,
  I-05, I-20).
- `gblog init` writes `<root>/.gitignore` (`state/*.lock`, `*.tmp`, `state/capture-files-*.json`) if
  absent (I-22).
- `CHANGELOG.md`, `SECURITY.md`, `docs/DESIGN.md`.

### Changed

- **Interrupted append rolls forward (I-05).** If state is exactly one record behind a
  self-consistent tail, the next append adopts the tail and prints
  `notice: recovered state/<bot>.json after an interrupted append`. Every other mismatch is refused
  as before.
- **Clock regression is clamped (I-04).** When the wall clock is earlier than the last record and no
  `--ts` was given, the record takes the tail timestamp and carries `clock_regressed` with the real
  wall-clock value. The chain no longer breaks.
- `verify` takes a shared lock while it snapshots the log, state and anchors, so a concurrent append
  no longer causes a spurious FAIL (I-10).
- `verify --record` and `sync` continue after a refused bookkeeping append instead of aborting.
  `sync` omits a missing root `README.md` from `git add` and reports `detached HEAD; pass --branch`
  (I-11).
- `sync` and `capture browser` update state through one locked helper that never writes a genesis
  state over a non-empty chain (I-09).
- `status`: `today` counts each record once (UTC date), `--chains` is truly per chain, and open tasks
  are computed over 30 days instead of 7 (I-14).
- `task-end` without a task id, or with an id that has no `task_start` in the last 30 days, still
  records but warns on stderr (I-13).
- `capture browser` (I-16):
  - the cutoff no longer depends on the local time zone;
  - unreadable history files are reported (`, <k> unreadable`);
  - the command exits 1 only when every history failed;
  - rows with a NULL time are skipped.
- `capture files` (I-17):
  - the snapshot is written atomically, and only after all appends succeed;
  - a changed `--path` or an unreadable snapshot starts a new baseline;
  - the ledger directory is excluded by path, not by string prefix.
- `tune` (I-18):
  - applied proposals move to `### already applied`;
  - rejected proposals are hidden for 28 days unless their count at least doubles;
  - `tune --bot B` merges into `proposals.json` instead of overwriting it;
  - routine-started tasks are no longer proposed as routines.
- Alias registry (I-19):
  - a slug always beats an alias, whatever the file order;
  - `aliases.json` is written under a lock with fsync;
  - `alias strict on` with an empty registry prints a note.
- `archive` writes deterministic gzip files and fsyncs them before chaining the hash. A missing or
  unreadable `--file` exits 2 with a message (I-20).
- `anchor --bot X` merges into the day rollup instead of replacing it. A FAIL chain without a
  descriptor prints `break -` (I-21).
- An implicit `init` prints `notice: initialized new ledger at <root>` on stderr (I-22).
- Deprecated `datetime.utcnow()` / `utcfromtimestamp()` calls are replaced (I-16).
- `_fsync_dir` no longer raises on Windows (I-22).
- `install.sh` (I-41):
  - treats a directory as a checkout only when it really is one;
  - supports `GBLOG_REF` and `GBLOG_SHA256`;
  - checks for Python 3.8 or newer;
  - persists `GBLOG_ROOT` independently of `PATH`;
  - handles `~/.zshrc`;
  - prints which files it modified.
- `bin/gblog` works through a symlink (I-42).
- CI adds newer Python versions, a macOS job, a `scripts` job and least-privilege permissions (I-40).

### Fixed

- `report --date D` no longer includes the entire history of Bots that have no log file for D (I-03).
- One malformed line (a non-object JSON value, an invalid UTF-8 byte, wrong field types) no longer
  crashes `verify`, `report`, `status` or `append`. It is treated as a corrupt record: FAIL in
  `verify`, skipped by readers, refused and spooled by `append` (I-07).
- An argument containing an un-encodable character is logged with the character escaped instead of
  crashing and losing the event (I-07).
- A non-dict `state/<bot>.json` is treated as unreadable instead of crashing (I-07).
- A log file whose last line has no trailing newline no longer gets the next record glued onto that
  line (I-22).
- Finding the tail record no longer scans the whole day file on every append (I-22).
- Tests no longer use the network or the developer's browser history, and no longer call the
  deprecated `utcnow()`. Tests of relative windows (tune, the future `--ts` bound, browser capture)
  still read the wall clock (I-30).

## [0.5.0]

### Added

- `alias remove --slug <slug>`: drop a registry entry. Chains on disk are untouched.
- `alias list` prints registry conflicts left by older versions, with the fix.

### Changed

- `alias add` refuses a variant or display name that is already another Bot's slug or display name,
  and a slug that is already another Bot's alias.

## [0.4.0]

### Added

- `seal`: append one `break_ack` for the pinned kim-biseo 2026-11-02 discontinuity
  (`KIM_BISEO_BREAK`), with a witness note on the `ledger` chain. Verify reports a sealed chain as
  `PASS (N approved break(s))`.
- `anchor`: per-chain anchors in `anchors/<bot>.json` and a day rollup in `anchors/days/<date>.json`.
  It never skips the whole ledger.
- Tail check: an append is refused when the log tail does not match `state/<bot>.json`. The event is
  saved under `spool/<bot>/`, `state/<bot>.alert.json` is replaced, and the CLI exits 4 with
  `ALERT tail mismatch`.
- `docs/chain-repair-live-apply.md` and the fabricated fixture under `tests/fixtures/kim-biseo-break/`.

### Changed

- Appends write the line with `O_APPEND`, fsync the file and its directory, and only then replace
  state atomically.
- `sync` and browser capture re-read state under the bot lock before writing it.
- `verify` walks every segment of a broken chain and reports break descriptors and segment heads.
- `sync` also commits `anchors/` and `spool/`.

## [0.3.0]

### Added

- `alias`: one canonical ASCII slug per Bot, a display name and historical variants
  (`add`, `list`, `strict`, `resolve`). `report`, `status`, `tail`, `coverage` and `tune` merge
  variant chains without rewriting them.
- The report flags an open task whose twin was closed in another chain of the same Bot.
- `archive`: content-addressed evidence files under `archive/<bot>/<date>/<sha256>.gz`. Text is
  redacted before hashing, and `verify` re-hashes archived files.
- Private sync: `sync` refuses to push to a public GitHub repository (`--allow-public` overrides).

### Changed

- Browser capture keeps one marker per history file. 0.2 kept a single marker.

## [0.2.0]

### Added

- `capture shell-hook`, `capture browser`, `capture files`: evidence collected without the Bot's
  cooperation, written with `source: "auto"`.
- `coverage`: share of automatically observed actions that the Bot also reported.
- `tune`: evidence-backed improvement proposals, `--apply` / `--reject`, and the
  "Effect of applied changes" table.
- `log --source` and the `$GBLOG_TASK` fallback for `log`.
- The `bot-tune` skill and the weekly tune routine.

## [0.1.0]

### Added

- Initial release:
  - hash-chained per-Bot JSONL ledger with an `flock` per Bot;
  - secret redaction;
  - `init`, `task-start`, `log`, `approval`, `note`, `task-end`, `verify`, `report`, `status`,
    `tail`, `sync`, `export`;
  - `install.sh`, the `activity-log` skill and the daily audit routine.
