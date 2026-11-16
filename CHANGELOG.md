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
`gblog verify --all` output as with 0.6.0.

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

### Added

- `status --json` rows gain `future_tail` (R-03).
- `verify --json` has an entry for every FAIL, including a name with no chain on disk and a chain
  whose logs are missing (`"missing": true`) (R-08).
- `tune` records store `count` in the `proposal` snapshot (R-11).
- In-memory record key `bot_field` (R-04).

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
- Dead code and duplicate helpers were removed; behaviour is unchanged (R-15).

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

### Documentation

- `docs/DESIGN.md`, README, `SECURITY.md`, both skills, both routine prompts and the template docs
  were brought in line with the code (R-22 … R-25). In particular: exit 2 no longer claims that
  nothing was written, the Auto-review rule is an allow-list, and browser and file capture run in
  the weekly tune routine only.

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
