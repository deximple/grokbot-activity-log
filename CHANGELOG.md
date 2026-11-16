# Changelog

All notable changes to `gblog` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The on-disk record format (schema `v: 1`, canonical JSON, SHA-256 hash rule) has not changed in
any release listed here. A ledger written by any version still verifies with the latest one.

Entries for 0.1 to 0.5 are retroactive. They were reconstructed from the code and the README, and
their release dates were not recorded.

## [0.6.0] - Unreleased

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
- The prefix abbreviation `--s` on `log` is now ambiguous (`--source` / `--summary`). Spell the flag
  out (I-13).
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
- Tests no longer use the network, the developer's browser history or the wall clock (I-30).

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
