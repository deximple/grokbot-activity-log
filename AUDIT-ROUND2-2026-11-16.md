<!-- Round-2 audit: claude-fable-5-2, effort=max, batch, 10:14 KST. R-16 (test_cli_ux.py) was already created before this audit landed. -->
# Round-2 audit — 2026-11-16

Scope: `gblog.py` 0.6.0, `bin/gblog`, `install.sh`, `.github/workflows/ci.yml`, all `*.md`, `tests/*.py`, measured against work items I-01 … I-42 of the 2026-11-16 audit.

Method: static read only. Nothing was executed, so every finding is derived from the code path quoted as evidence. I did not re-count the 133 tests, and the 0.5 `install.sh` was not supplied. Repro sequences are what the code implies, not observed output.

## 1. Executive summary

- **37 of 42 items are implemented as specified.** Four are partial in code (I-02, I-05, I-07, I-10), one is half missing (I-19), and one deliverable is absent entirely (I-26, `tests/test_cli_ux.py`). The remaining gaps are documentation statements that contradict the code (I-34, I-35).
- **`tests/test_cli_ux.py` was never created.** About 25 specified tests are missing, so I-03, I-06, I-13, I-14, I-19 and the CLI half of I-20 have no unit coverage. This is why the next point went unnoticed.
- **Reserved bot names are not enforced on append.** `_check_reserved` is called only from `add_alias`. CHANGELOG and DESIGN say `append` refuses them.
- **A fully truncated chain still verifies OK while its state witness survives.** `verify` skips the state-head check when the walk counted zero records. This contradicts the guarantee in `SECURITY.md`.
- **The clock clamp (I-04) removed a loud failure and added no signal.** One future-dated record pins every later record to that timestamp and day file. `verify` stays OK with no warning, and daily reports stop showing that chain.
- **`verify` still has a race and a crash path.** Anchor files are read after the shared lock is released, so a concurrent `anchor` can cause a spurious FAIL. The strict/layout wrapper re-reads every file with a second parser that can raise `RecursionError`.
- **Chain directories with non-canonical names are read by reports but never verified.** One directory named `logs/---` makes `status` and `verify --all` exit 2.
- **Merge debris is visible.** There are two identical error helpers, one dead tail-check function, an unused re-entrant lock mechanism that DESIGN says does not exist, and a `verify --json` implementation that re-enters `_main` and drops some FAILs.
- **The production check proves little.** Byte-identical `verify --all` output exercises only the healthy path. None of the findings below changes that output on a healthy ledger.

## 2. Status of I-01 … I-42

| ID | Status | Evidence | Gap |
|---|---|---|---|
| I-01 | Implemented | `verify`: `pinned = pinned_bot and _same_break(parsed, KIM_BISEO_BREAK)`; `fixture = (… all(parsed[k] == KIM_BISEO_BREAK[k] for k in _COORD_KEYS) and self._is_temp_copy())`; `_tail_ok`; `_PLACEHOLDER_IDS` | Witness warning has no test. Fixture rule depends on `TMPDIR` and is invisible in text output (N-10). |
| I-02 | Partial | `_anchor_checks`, `anchored_missing`, `chain_hashes`, `_anchor_history_file`; state warnings under `if items:` | `if not problems and count and state.get("head") not in (GENESIS, final_head)` skips empty chains (N-01). Missing-chain check reads `anchors/<bot>.json` only. |
| I-03 | Implemented | `iter_records`: `paths = self.log_files(bot) if files is None else list(files)` | Both named tests are missing (N-03). |
| I-04 | Implemented | `parse_ts`, `TimestampError`, clamp in `append`, `_validate_args` | `coverage --days` above 36500 clamps instead of exiting 2 (forced by an existing test). No signal for a future-dated tail (N-04). |
| I-05 | Partial | `_append_head` roll-forward; `rebuild_state`; `spool_replay`; `alert_info` | An unreadable state file is refused although DESIGN says "adopt" (N-08). Polish gaps in N-18. |
| I-06 | Implemented | `chains_on_disk`; verify/anchor branches print `no chain on disk for '<name>'` | No unit tests; only the CI smoke step checks `nosuchbot`. |
| I-07 | Partial | `_parse_line`, `_fields_ok`, `_safe_hash`, `_clean_text`, handlers in `main()` | Ten crash paths remain (N-07). `_die` and `_exit2` are duplicates. |
| I-08 | Implemented | 11 patterns appended to `_SECRET_PATTERNS` | JSON-key rules omit `pwd` and `access_key`, which the docs list (N-15). |
| I-09 | Implemented | `update_state` seeds from the tail or writes nothing | Re-implements the JSON read and the self-consistency test. |
| I-10 | Partial | `_Lock(shared=True)`; `with self.lock(bot, shared=True): items = …; raw_state_snapshot = …` | `_anchor_checks` runs after the lock is released (N-06). |
| I-11 | Implemented | `_record_verify`; `_bookkeep` in `sync`; `detached HEAD; pass --branch` | — |
| I-12 | Implemented | Anchored `_GITHUB_REMOTE_RE`; `get-url --push --all`; `unknown_github` refusal; `_safe_url` | The flag travels through `ledger.allow_unknown_visibility`, not the `allow_unknown` parameter. |
| I-13 | Implemented | `--summary` alias, `_norm_outcome`, `_source_field`, `_add_root_everywhere`, exclusive group, `_VerifyJsonSink` | All ten tests are missing. `verify --json` drops some FAILs (N-09). New abbreviation breaks are undocumented (N-13). |
| I-14 | Implemented | `chain_records`; `month` window; `today_n` | No tests. Crashes on wrong-typed state fields (N-07). |
| I-15 | Implemented | `_md`, `_MD_BREAKS_RE`, both footers | `ts` and the record `bot` field are rendered raw (low). |
| I-16 | Implemented | `cutoff_epoch = time.time() - hours * 3600`; marker wins; `failed` list | — |
| I-17 | Implemented | `save_snapshot()` after the appends; `_is_within`; re-baseline notices | The path check exists twice (CLI and function). |
| I-18 | Implemented | `decisions`, `status`, `hidden`, `save_proposals` merge | "Count doubled" is approximated and decays (N-14). |
| I-19 | Partial | Multi-pass `resolve`; `--move`; `RESERVED`; `_check_reserved` | `append` never calls `_check_reserved` (N-02). The alias lock covers only the write (N-11). All four tests are missing. |
| I-20 | Implemented | `GzipFile(filename="", …, mtime=0)`; fsync; `scanned`; `invalid archive path`; `--skip-archives` | `zlib.error` is uncaught. CLI tests are missing. |
| I-21 | Implemented | Rollup merge by bot; `break -`; `_chain_layout_findings`; `--strict` | Built as a wrapper that reads every file a second time (N-06). |
| I-22 | Implemented | `_append_line` newline guard; `_last_line`; `_open_private`; `.gitignore`; notice | Lock files and two directories bypass the private-mode helpers (N-17). |
| I-23 | Implemented | `tests/test_integrity.py`, all named cases | See N-19. |
| I-24 | Implemented | `tests/test_append_safety.py` | See N-19. |
| I-25 | Implemented | `tests/test_malformed_input.py` | `test_non_dict_state` accepts two outcomes. |
| I-26 | **Missing** | No `tests/test_cli_ux.py` in the tree | N-03. |
| I-27 | Implemented | `tests/test_redaction_extended.py` | — |
| I-28 | Implemented | `tests/test_capture_sync.py` | — |
| I-29 | Implemented | `tests/test_tune_loop.py` | — |
| I-30 | Implemented | Bare-repo origin, `BROWSER_HISTORY_GLOBS = ()`, no `utcnow` | — |
| I-31 | Implemented | `tests/test_scripts.py` | zsh, `.profile` creation and checksum are covered only in CI. |
| I-32 | Implemented | `CHANGELOG.md` | Two overclaims (N-20). |
| I-33 | Implemented | README sections rewritten | Setup step 6 contradicts `SECURITY.md`. The exit 2 wording is wrong (N-12). |
| I-34 | Partial | "Common mistakes" section added | Still says "every shell command -> ledger". Says "Exit 2 means nothing was written". |
| I-35 | Partial | `docs/DESIGN.md` covers all requested sections | Four statements contradict the code (N-20). |
| I-36 | Implemented | Step 0, two reports, "Chain heads" block, `--contains` | No capture step, although the skill says capture runs daily. |
| I-37 | Implemented | "What verify honours", pre-step 3, "Witness warning" | Does not mention `TMPDIR`. |
| I-38 | Implemented | `SECURITY.md` | Guarantee about the state witness is broken by N-01. |
| I-39 | Implemented | `already applied`, 28 days, `--source routine` | — |
| I-40 | Implemented | Matrix, permissions, concurrency, `scripts` job | Hook lint excludes SC1007 (documented). No `timeout-minutes`. |
| I-41 | Implemented | `BASH_SOURCE` check, `GBLOG_REF`, `GBLOG_SHA256`, markers | Relative `GBLOG_ROOT` is persisted as given; legacy 0.5 blocks are not detected (N-21). |
| I-42 | Implemented | `while [ -L "$src" ]` loop with plain `readlink` | — |

## 3. Findings

### N-01 (High) — The state witness is ignored when the chain has no records

- Evidence, `Ledger.verify`: `if not problems and count and state.get("head") not in (GENESIS, final_head):`. With zero records `count` is 0, so the check is skipped. The missing-state warnings sit under `if items:` and are skipped too.
- Sequence: three appends, then empty the day file or delete `logs/ops` while `state/ops.json` stays. `gblog verify --bot ops` prints `OK    ops    0 records  head genesis` and exits 0.
- `append` on the same chain refuses with exit 4, so the two commands disagree.
- `verify --all` enumerates `logs/` only. A chain whose directory is gone but whose state file or `anchors/<bot>.history.jsonl` remains is not reported. `anchored_missing` reads `anchors/<bot>.json` only.
- `SECURITY.md` lists this as guaranteed: "a truncated tail … as long as at least one witness survives: `state/<bot>.json`".
- `test_deleted_chain_reported_by_verify_all` deletes the state file as well, so it cannot see the gap.

### N-02 (High) — Reserved names are not refused on append

- Evidence: `append` starts with `bot = safe_name(bot)` and `record = _clean_text(record)`. There is no `_check_reserved` call. The only caller is `add_alias`.
- `gblog note --bot aliases --detail x` creates `logs/aliases/` and `state/aliases.json`. The second overwrites the alias registry.
- CHANGELOG ("New chains cannot use the reserved bot names") and DESIGN §4 step 0 and §10 describe behaviour that does not exist.
- Most likely lost in a hand-merged `append` hunk; four items touch that function.

### N-03 (High) — `tests/test_cli_ux.py` is missing

No unit test exists for any of the following:

- `report --date` for an idle Bot (I-03).
- `verify`, `anchor` and `export` name resolution (I-06).
- `--summary`, `$GBLOG_TASK` fallback, outcome synonyms, `status --bot`, `--root` after the subcommand, `tail -n 0`, the exclusive group, `verify --json` (I-13).
- Status counts (I-14).
- Resolve order, `--move`, reserved names (I-19).
- `archive` with a missing file, and `scanned: false` (I-20).
- `test_exit_code_table`.

### N-04 (Medium-High) — A future-dated tail pins the chain silently

- Evidence, `append`: `if tail_ts and wall < tail_ts: ts, regressed = tail_ts, True`, then `path = os.path.join(self.logs_dir, bot, today(ts) + ".jsonl")`.
- One record dated 2099 (via `Ledger.append` with a fixed `ts`, a raw line, or a clock moved forward for one command) makes every later record carry the 2099 timestamp and land in `2099-…jsonl`.
- `records(date=…)` selects files by name, so `gblog report` never shows that chain again. `status` shows `today 0`.
- `verify` prints `OK` with no `!` line. In 0.5 the next record caused `timestamp goes backwards` (FAIL). The clamp is right for benign regressions, but detectability went from loud to none.
- The threat model in DESIGN §1 grants the adversary both capabilities ("call `Ledger.append()` directly", "may be able to change the clock").

### N-05 (Medium) — Non-canonical chain directories and the record `bot` field

- `verify --all` calls `ledger.verify(b)` for each directory, and `verify` does `bot = safe_name(bot)`:
  - `logs/my bot/` verifies the non-existent `my-bot` and prints `OK    my bot    0 records  head genesis`.
  - `logs/ops./` re-verifies `ops` and prints a second OK line.
- `records()` reads those directories by their raw names. Their records therefore reach `report`, `status`, `tune` and `export` unverified.
- `logs/---`: `safe_name` and `resolve` call `_die`. `verify --all` and `status` (through `groups()`) exit 2 for the whole ledger.
- Readers group by the record's `bot` field (`by_bot.setdefault(r.get("bot", "?"), …)`). A raw-written, hash-valid record in chain A with `bot: "B"` is reported under B. `verify` only warns unless `--strict` is given.

### N-06 (Medium) — Verify snapshot is incomplete, and the options wrapper re-reads everything

- `_anchor_checks(bot, hashes)` runs after the `with self.lock(bot, shared=True)` block. DESIGN §5 says the lock covers "log items, state and anchor files".
- Race: verifier snapshots N records; another process appends N+1; the routine runs `anchor`; the verifier then reads a history line naming head N+1 and reports `anchored head … is no longer in the chain`.
- `Ledger.verify = _with_verify_options(Ledger.verify)` runs `_chain_layout_findings` on every call. It opens every log file a second time, outside the lock, and parses with `except ValueError: continue`. A deeply nested JSON line raises `RecursionError`, which the walk handles through `_parse_line` but this second parser does not.
- `--contains` reads the chain a third time through `chain_hashes`.

### N-07 (Medium) — Remaining crash paths

DESIGN §11 says a traceback on malformed ledger input "would be a bug". These remain:

| Trigger | Code | Result |
|---|---|---|
| One line of deeply nested JSON (`[[[[…`) | `_chain_layout_findings`: `except ValueError` | `RecursionError` from every `verify` |
| Archive `.gz` with corrupted deflate data | `verify_archives`: `except (OSError, EOFError)` | `zlib.error` |
| State with a wrong-typed `seq`, `head`, `updated` or `last_sync` | `status`: `sum(ledger.read_state(c).get("seq", 0) …)`, `[:12]`, `s.get("ts", "")` | `TypeError` or `AttributeError` |
| `state/aliases.json` is `[]`, or `"bots"` is not an object | `aliases()`: `data.setdefault("bots", {})` | `AttributeError` in every command that resolves a name |
| Record containing a JSON-escaped lone surrogate | `export`, `tail`, `report` writing to a UTF-8 stream | `UnicodeEncodeError` |
| Non-ASCII display name on an ASCII stdout | any `out.write(label)` | `UnicodeEncodeError` |
| Hash-valid record with a non-string `detail`, `target`, `task` or `task_id` | `tail`: `"- " + r["detail"]`; `render_report`: `tid not in tasks`; `norm_target` | `TypeError` |
| `capture_browser_last` is a non-numeric string | `capture_browser`: `int(markers)` | `ValueError` |
| `proposals.json` entry without `bot`, or with a non-string `generated` | `tune_apply`: `p["bot"]`; `_ts_epoch(stamp)` | `KeyError` or `TypeError` |
| `logs/---` | N-05 | exit 2 for `status` and `verify --all` |

Related: readers skip corrupt lines silently. A report built from a damaged file looks complete unless someone also runs `verify`.

### N-08 (Medium) — Unreadable state: three commands, three behaviours

- `_append_head`: when the state file exists but does not parse, `state is None` and `(seq, head)` is `(0, genesis)`. Neither the equal branch nor the roll-forward branch (`state is not None and …`) applies, so the append is refused with exit 4.
- `update_state` (used by `sync` and `capture browser`) seeds from the tail and rewrites the same file without a message.
- `verify` warns and stays OK.
- DESIGN §4: "missing or unreadable | present | tail self-consistent → adopt the tail".
- `test_non_dict_state` asserts `assertIn(rc, (0, 4))`, so it passes either way.

### N-09 (Medium) — `verify --json` drops FAILs

- `_VerifyJsonSink` collects only return values of `ledger.verify`. The unknown-bot branch and the "anchored but logs missing" branch write text to the sink and return 1.
- Result: `gblog verify --bot typo --json` prints `[]` with exit 1.
- The implementation re-enters `_main(argv, _sink=sink)` and swaps `ledger.verify` for a closure. It works, but it is fragile.
- `verify --all --bot X --contains H` is accepted: `--all` wins and the hash is searched across every chain.

### N-10 (Medium) — The fixture-ack rule is environment-controlled and invisible

- `_is_temp_copy()` compares against `tempfile.gettempdir()`, which honours `TMPDIR`. `realpath` does not change that.
- With `TMPDIR=/workspace`, the live root qualifies. An ack at the six pinned coordinates with any three hashes is then eligible on `kim-biseo`.
- The text output is the same `PASS (1 approved break)` line. `fixture_ack` appears only in `--json`.
- The fabricated fixture is deterministic and its three hashes are committed in `expected-break.json`. Pinning them removes the environment dependence.
- No test exercises the pinned (non-fixture) branch or the witness warning. The only assertion is an `assertNotIn` on the fixture path, which passes if the feature is deleted.

### N-11 (Medium) — Alias registry

- Unparsable `aliases.json` is silently treated as empty (`except (OSError, ValueError): data = {}`). Strict mode turns off and merging stops, and new records go to variant chains without warning.
- `save_aliases` takes `aliases.lock` around the write only. `add_alias` reads before the lock, so two concurrent edits lose one. The write was already atomic, so the lock protects nothing.
- The `--move` check is case-sensitive (`v in e.get("aliases", [])`), while `resolve` has a case-insensitive alias pass.

### N-12 (Low-Medium) — "Exit 2 means nothing was written" is false

| Case | What was written |
|---|---|
| `ENOSPC` in `write_state` (shown by `test_enospc_in_write_state_recovers`) | The log line is durable; the next append rolls forward |
| `sync` with a `git` failure | The `sync pending` record |
| `spool replay` with one unreadable file | Every readable event was appended; `return 2 if res["skipped"] else 0` |
| `archive --ts <past>` | The `.gz` is stored before `append` raises `TimestampError` |

The skill tells Bots to fix the command and run it again after exit 2. In the first case that produces a duplicate record.

### N-13 (Low-Medium) — Undocumented compatibility breaks

| Change | Cause | Documented |
|---|---|---|
| `--r` is ambiguous on `verify` (`--record`), `sync` (`--remote`), `tune` (`--reject`), `capture shell-hook` (`--rc`) | `--root` added to every subparser | No |
| `--s` is ambiguous on `archive` (was `--stdin`) and `tune` (was `--stdout`) | `--source`, `--summary` | Only for `log` |
| `--a`, `--al`, `--allow` are ambiguous on `sync` | `--allow-unknown-visibility` | No |
| `state/*.lock` and `state/capture-files-*.json` already tracked in an existing backup repo stay tracked | `.gitignore` does not untrack | No |
| Stale `state/<bot>.alert.json` files from 0.4/0.5 now print `ALERT` in `status` after upgrade | I-05 | No; there is no documented way to dismiss without replaying |
| A retired chain with a surviving anchor makes `verify --all` FAIL permanently | I-02 | No |

Also worth stating: `main()` returns 2 for `_UsageError` but lets `SystemExit(2)` from `_die` propagate. Existing tests assert `SystemExit`, so this must stay.

### N-14 (Low) — Tune "count doubled" rule decays

- `prior = sum(1 for r in evidence if _precedes(r, decision))` counts only evidence still inside the window.
- Once the pre-rejection evidence ages out, `prior` is 0. Any two new events then satisfy `len(evidence) >= 2 * max(prior, 1)`, and the proposal returns well before 28 days.
- The snapshot stored by `tune_apply` has no `count`, so the true value at rejection time is unknown.

### N-15 (Low) — Redaction lists disagree

- Both JSON-key patterns use `token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key`.
- README and `SECURITY.md` say "JSON keys ending in those words", and the word list includes `pwd` and `access_key`.
- `{"SecretAccessKey": "…"}` (AWS STS output) and `{"db_pwd": "…"}` are stored as written.

### N-16 (Low) — Dead code and duplicated helpers

| Item | Evidence | Action |
|---|---|---|
| `_die` and `_exit2` | Identical bodies | Keep `_die` |
| `_assert_tail_matches` | No caller; `append` uses `_append_head` | Delete |
| `_BREAK_KEYS` | Never referenced | Delete |
| `coord_keys` tuple in `seal_break` | Duplicates `_COORD_KEYS` | Use the constant |
| `_Lock._held` re-entrancy | No code path nests a bot lock; DESIGN §5 says "No re-entry" | Delete |
| `_with_verify_options`, `Ledger.verify = …` | N-06 | Fold into the method |
| `_VerifyJsonSink`, `_main(_sink=)` | N-09 | Replace |
| `ledger.allow_unknown_visibility` | `sync(allow_unknown=None)` parameter is never passed | Pass the parameter |
| `capture files` path check | Present in `_main` and in `capture_files` | Keep the function's |
| Hand-rolled `json.load` blocks | `update_state`, `aliases`, `read_anchor`, `tune_apply`, `save_proposals` beside `_read_json` | Use `_read_json` |
| `log` source expression | Duplicates `_source_field` | Use the helper |
| `_tail_head` | Called only by tests | Keep; mark as a test helper |
| Comment in `chain_records` ("it would fall back to every file") | Stale after I-03 | Fix |
| Local sort key in `chain_records` | Duplicates `_seq_int` | Reuse |
| `# -- iteration` directly above `# -- aliases`; local `import tempfile`; `env_ok` | Stale label, shadowed import, misnomer | Tidy |

### N-17 (Low) — Hygiene gaps

- `_Lock.__enter__` uses `open(self.path, "a+")`, so `state/<bot>.lock` is created `0o644`.
- `lock()` and `spool_replay` call `os.makedirs`, not `_makedirs`. `state/` (when first created by `update_state`) and `spool/<bot>/replayed/` follow the umask.
- `sync` never adds `<root>/.gitignore`. `git status` always shows one untracked path, and a restored clone has no ignore file.
- `_atomic_json` uses a fixed `path + ".tmp"`. Anchor files are written without a lock, so two concurrent `anchor` runs can interleave in one temp file.

### N-18 (Low) — Recovery and archive polish

- `status` builds rows from `logs/` only. An alert for a chain whose log directory is gone is not shown.
- `state rebuild` prints the pre-note seq and head, while state is one record further. On a healthy chain it still rewrites state and appends a note.
- `archive --no-redact` leaves no marker: the record has `redacted: false` and no `scanned` key, the same as scanned clean text.
- `_task_start_seen` loads 30 days of every chain of the Bot on each `task-end`.

### N-19 — Test weaknesses

| Test | Why it would pass with the feature broken |
|---|---|
| (none) for reserved names | The feature is absent and the suite is green |
| `test_non_dict_state` | `assertIn(rc, (0, 4))` |
| `test_fixture_ack_still_passes_in_temp_copy` | Witness warning checked only with `assertNotIn` |
| `test_deleted_chain_reported_by_verify_all` | Deletes state too; never tests state as the surviving witness |
| `test_bot_field_mismatch_warns_and_strict_fails` | `any("bot" in w …)`; the strict branch asserts only rc 1 |
| `test_new_files_are_private` | Checks one log and one state file; the lock file would fail |
| `test_parallel_appends_and_verify` | Probabilistic for the lock; never runs `anchor` concurrently |
| `test_state_rebuild_missing_and_genesis` | Asserts only the prefix `rebuilt ops seq` |
| `test_rejected_hidden_then_returns` | Does not cover evidence ageing out |
| `test_archive_roundtrip…` (binary case) | Asserts `redacted` false, not `scanned` |
| CI smoke only | Sole check of `--summary` and of `verify --bot nosuchbot` |

Structural points:

- Six copies of `run()` exist with different exception handling.
- In-process `StringIO` capture cannot see output-encoding crashes.
- The two git tests in `tests/test_gblog.py` (`test_sync_commits_locally`, `test_sync_refuses_public_remote` up to its bare-repo block) run without config isolation.

### N-20 — Documentation drift

| File | Statement | Code |
|---|---|---|
| DESIGN §4 table | Unreadable state with a consistent tail → adopt | Refused (N-08) |
| DESIGN §4 step 0, §10; CHANGELOG | `append` refuses reserved names | Not implemented (N-02) |
| DESIGN §5 rule 3 | "No re-entry … would deadlock" | `_Lock._held` makes it a no-op |
| DESIGN §5 table | Shared lock covers anchor files | Read after release (N-06) |
| README step 6 | Auto-allow "`gblog` commands" | Covers `seal`, `spool replay`, `state rebuild`, `sync --allow-public`; `SECURITY.md` forbids exactly that |
| README exit table; SKILL | Exit 2: "Nothing was written" | N-12 |
| SKILL "Automatic evidence" | "every shell command -> ledger" | Interactive bash only |
| SKILL step 1 | No `--source routine` for routine runs | README says routine runs pass it |
| SKILL and daily routine | Capture "run from the daily routine" | The daily prompt has no capture step |
| `routine-weekly-tune.md` | No `task-start --source routine`, no `!` or exit 4 handling, no untrusted-text rule | Three weekly runs get proposed as a routine |
| `bot-template.md` | "`verify` detects edit/delete/truncation" | Truncation only while a witness survives |
| CHANGELOG | "Tests no longer use … the wall clock" | Tune, future-`--ts` and capture tests do |
| CHANGELOG / `SECURITY.md` | "0.6.0 - Unreleased" versus "0.6.x (latest release)" | Inconsistent |
| README alias section | No `--move`, no reserved names | In CHANGELOG only |

### N-21 — install and CI

- `install.sh` makes `PREFIX` absolute but not `GBLOG_ROOT`. `GBLOG_ROOT=ledger ./install.sh` persists `export GBLOG_ROOT=ledger`, which later resolves against any working directory.
- Idempotence relies on the 0.6 markers. If the 0.5 script wrote its lines under a different marker, an upgrade from a non-interactive shell appends a second PATH block and a second ROOT block. Check against the 0.5 script.
- `~/.bash_profile` is not handled. Bash login shells read it instead of `~/.profile`.
- CI jobs have no `timeout-minutes`; a hung `flock` test would run for hours.
- The smoke step exercises none of `state rebuild`, `spool list`, `verify --json`, `--strict` or reserved names.

## 4. Fix work items

Rules for every item:

- Standard library only, Python 3.8+.
- Record bytes and the hash rule do not change.
- Every existing test passes unmodified unless the item says otherwise.
- New output goes to stderr or to additive lines and keys.

### Group G1 — `gblog.py`

**R-01 Count the state witness on an empty or missing chain (N-01).**
- `Ledger.verify`:
  - Drop `count and` from the state check. Use `raw_state is not None`, a string `head`, and `head not in (GENESIS, final_head)`. The problem text is unchanged.
  - A non-string `head` counts as "missing or unreadable" (warning).
- Add `Ledger.orphan_witnesses()` returning `(bot, text)` pairs for names with no `logs/<name>` directory:
  - `state/<name>.json` is an object with a string non-genesis `head` and no `kind` key. Skip `aliases`, `capture-files-*`, `*.alert` and `*.alert.resolved-*`. Text: `state/<name>.json records seq <n> head <12 hex> but logs/<name> is missing`.
  - `anchors/<name>.history.jsonl` has at least one non-empty `heads` list. Text: `chain was anchored (history, last <ts>) but logs/<name> is missing`.
- `verify --all` prints one `FAIL  <bot>  0 records` block per name. Merge with `anchored_missing` so no bot appears twice. rc is 1.
- Compat: a healthy ledger prints the same bytes.
- Tests (R-17): `test_empty_chain_with_state_fails`, `test_logs_dir_deleted_state_remains_fails`, `test_history_only_missing_chain_reported`.

**R-02 Refuse reserved names in `append` (N-02).**
- Call `self._check_reserved(bot)` right after `bot = safe_name(bot)`.
- Message and exit 2 as in `add_alias`. A chain with an existing `logs/<name>` keeps appending.
- Tests (R-16): `test_reserved_bot_names_refused_for_new_chains`, `test_existing_reserved_chain_still_appends`.

**R-03 Signal a future-dated tail (N-04).**
- `verify`: if the last walked record has a normalised `ts` more than 300 s after `now_iso()`, add the warning `last record is dated <ts>, in the future: new records are stamped with that time (see clock_regressed)`.
- `append`: when clamping and the tail is more than 300 s ahead of the wall clock, write `warning: last record of <bot> is dated <tail ts> (future); this record is stamped with it` to stderr.
- `status`:
  - JSON rows gain `future_tail` (ts or null), taken from `state.updated`.
  - Text output adds `CLOCK <chain>: last record dated <ts> (future)` after the `ALERT` lines.
- The clamp rule and exit codes do not change.
- Tests (R-18): `test_future_tail_warns_in_verify_and_status`, `test_clamp_warns_when_tail_is_future`. Both patch `gblog.now_iso`.

**R-04 Chain directory validity and attribution by directory (N-05).**
- Add `Ledger.invalid_chain_dirs()`: directories under `logs/` whose name differs from its sanitised form. Compute without calling `_die`.
- `verify --all`: for each, print `FAIL  <dir>  0 records` and `- logs/<dir>: directory name is not a valid chain key; its records are not verified`. rc is 1. Never pass the name to `Ledger.verify`.
- `records()`, `groups()`, `chains_for()` and `status` skip these directories and write one stderr line: `warning: ignoring logs/<dir> (invalid chain directory name); run gblog verify --all`.
- `records()` and `chain_records()` gain `raw=False`:
  - When a record's `bot` differs from its chain directory, yield a copy with `bot` set to the directory and `bot_field` set to the original.
  - `export` passes `raw=True`.
- Compat: no change for valid ledgers.
- Tests (R-17): `test_invalid_chain_dir_fails_verify_all_and_is_not_reported`, `test_dash_only_dir_does_not_break_status`, `test_report_attributes_by_directory`.

**R-05 One snapshot and one parser in `verify` (N-06).**
- Inside the shared lock, read:
  - items together with the stripped raw text of each line;
  - state;
  - `anchors/<bot>.json`;
  - the lines of `anchors/<bot>.history.jsonl`.
- `_anchor_checks` works on those snapshots.
- Compute the layout findings (bot mismatch, stray file names, non-canonical lines) from the same items.
- Delete `_chain_layout_findings`, `_with_verify_options` and the `Ledger.verify = …` assignment.
- Signature: `verify(self, bot, strict=None, skip_archives=None)`. `None` means "use the instance attribute".
- `--contains` uses the hashes of the same snapshot.
- Compat: result keys and all text output are unchanged.
- Tests (R-17): `test_anchor_written_after_snapshot_does_not_fail_verify`. Tests (R-19): `test_deeply_nested_line_fails_not_crash`.

**R-06 Remove the remaining crash paths (N-07).**
- `main()`: call `reconfigure(errors="backslashreplace")` on stdout and stderr when the method exists. Report and tuning files are opened with `errors="backslashreplace"`.
- `verify_archives`: also catch `zlib.error`. The problem text stays `<rel>: unreadable (<class>)`.
- Add `Ledger._state_view(bot)` returning typed values. `status` reads each chain's state once through it:
  - `seq`: int, 0 if invalid;
  - `head`: str, `genesis` if invalid;
  - `updated`: str or None;
  - `last_sync`: None unless it is an object with string `ts` and `commit`.
- Readers (not `raw=True`): a non-string value in `type`, `task_id`, `task`, `action`, `target`, `outcome`, `detail`, `source`, `decision`, `observed` or `kind` is replaced by its JSON text.
- Renderers pass `ts` through `_md`.
- `capture_browser`: an unusable marker value is ignored with a stderr notice.
- `tune_apply`: an entry without `bot` reports `error: tuning/proposals.json is corrupt`.
- `_ts_epoch`: a non-string gives 0.0.
- `analyze`: guard the `int(...[11:13])` hour parse.
- Readers count skipped lines per chain. `report`, `export`, `tail`, `status`, `coverage` and `tune` print `warning: skipped <n> unreadable line(s) in logs/<chain>; run gblog verify` on stderr. Stdout is unchanged.
- Tests (R-19): `test_corrupt_gzip_stream_fails_not_crash`, `test_status_survives_wrong_typed_state_fields`, `test_surrogate_record_export_and_report` (subprocess for `export`; in-process `report` without `--stdout`), `test_non_string_text_fields_do_not_crash_readers`, `test_skipped_lines_are_announced`.

**R-07 One rule for an unreadable state file (N-08).**
- `_append_head`: treat "exists but does not parse" as "missing". Adopt a self-consistent tail; refuse a corrupt one.
- Print `notice: state/<bot>.json was missing; adopted the log tail (seq <n>)` or `… was unreadable; …` on stderr.
- `update_state` reuses the same self-consistency test (`_safe_hash` plus `_fields_ok`) and prints the same notice.
- Tests (R-19): tighten `test_non_dict_state` to `rc == 0` plus the notice. Tests (R-18): `test_unreadable_state_adopts_tail_like_missing`.

**R-08 Rebuild `verify --json` on a result list (N-09).**
- The `verify` branch builds `results`, a list of dicts. The unknown-bot and missing-chain cases become synthetic entries: `{"bot", "ok": false, "records": 0, "head": null, "problems": [...], "warnings": [], "missing": true}`.
- Text and JSON are both rendered from that list. Delete `_VerifyJsonSink` and the `_sink` parameter.
- `--contains` together with `--all` exits 2 with `verify --contains cannot be combined with --all`.
- Compat: text output is byte-identical.
- Tests (R-16): `test_verify_json`, `test_verify_json_unknown_bot_has_entry`. Tests (R-17): `test_verify_json_missing_chain_has_entry`, `test_contains_with_all_is_usage_error`.

**R-09 Pin the fixture ack by hash and make it visible (N-10).**
- Add `KIM_BISEO_FIXTURE_BREAK` with the pinned coordinates and the three hashes from `tests/fixtures/kim-biseo-break/expected-break.json`.
- `verify`: `fixture = pinned_bot and _same_break(parsed, KIM_BISEO_FIXTURE_BREAK) and self._is_temp_copy()`.
- When a fixture ack is honoured, add the warning `fixture break_ack honoured: this root is a temporary test copy`.
- `seal_break(copy_fixture=True)`: after the coordinate check, also require the fixture descriptor. Otherwise raise `SealError("refusing: copy fixture hashes do not match the committed fixture")`.
- Compat: the flows in `tests/test_chain_repair.py` and `tests/test_integrity.py` use exactly that fixture. The live pin is unchanged.
- Tests (R-17): `test_fixture_constant_matches_expected_break_json`, `test_fixture_coordinates_with_other_hashes_fail_even_in_temp`, `test_fixture_ack_prints_warning_line`.

**R-10 Alias registry robustness (N-11).**
- `aliases()`: if the file exists but is not an object, or `bots` is not an object:
  - warn once per process on stderr: `warning: state/aliases.json is unreadable; names are not resolved and strict mode is off until it is restored`;
  - return an empty registry flagged unreadable;
  - writes still proceed under `safe_name`, so no event is lost.
- `save_aliases` refuses to overwrite an unreadable registry (exit 2).
- `add_alias`, `remove_alias` and `alias strict` run read, modify and write inside one hold of `aliases.lock`.
- The `--move` check compares case-insensitively.
- Tests (R-19): `test_non_dict_aliases_warns_not_crashes`. Tests (R-16): `test_alias_steal_requires_move` with a case variant.

**R-11 Store the count at decision time (N-14).**
- `tune_apply` adds `count` to the `proposal` snapshot.
- `analyze`: when the decision carries an integer `count`, `hidden = recent and len(evidence) < 2 * count`. Otherwise keep the current formula.
- `render_tune`: emit an empty line before `### already applied`.
- Tests (R-20): `test_rejected_stays_hidden_when_old_evidence_ages_out`.

**R-12 Align JSON-key redaction with the documented word list (N-15).**
- Add `pwd` and `access[_-]?key` to both JSON-key patterns. Remove nothing.
- Tests (R-20): `{"SecretAccessKey": …}`, `{"db_pwd": …}`, and the escaped-quote variant. Idempotence and `test_existing_expectations_unchanged` must still hold.

**R-13 Recovery and archive polish (N-12, N-18).**
- `state rebuild`:
  - When state already equals the tail, print `state/<bot>.json already matches the log tail; nothing to do`, exit 0, write nothing.
  - Otherwise print the seq and head that state holds after the note.
- `status`: chains known only from `state/<bot>.alert.json` or `spool/<bot>/` get an `ALERT` line.
- `archive_file`:
  - With an explicit `ts` earlier than the tail, raise `TimestampError` before storing anything.
  - If `append` raises anything except `TailMismatchError` and the `.gz` was created by this call, remove it.
  - `--no-redact` sets `scanned: false`.
- `_task_start_seen`: scan day files newest first and stop at the first hit.
- Tests (R-18): `test_archive_past_ts_leaves_no_file`, `test_state_rebuild_noop_on_healthy_chain`, `test_status_alert_without_logs_dir`. Tests (R-16): `test_archive_no_redact_marks_unscanned`.

**R-14 Hygiene (N-17).**
- Create lock files with `os.open(…, 0o600)`.
- `lock()` and `spool_replay` use `_makedirs`.
- `_atomic_json` writes `path + ".<pid>.tmp"`.
- `sync`:
  - Add `.gitignore` to the pathspec when present.
  - After `git add`, run `git rm -r -q --cached --ignore-unmatch -- 'state/*.lock' 'state/capture-files-*.json'`.
  - Pass `allow_unknown=` as a parameter and delete the attribute.
- Tests (R-18): extend `test_new_files_are_private` to lock, spool, alert and anchor files. Tests (R-21): `test_sync_commits_gitignore_and_untracks_locks` in `tests/test_capture_sync.py`.

**R-15 Remove dead code and duplicate helpers (N-16).** Do this last; it changes no behaviour.
- Apply the Action column of the N-16 table.
- `_Lock` becomes a plain exclusive or shared flock holder.
- Keep `_tail_head`, with a comment that tests use it.
- Tests: the existing suite.

### Group G2 — `tests/*.py` (do not touch `tests/fixtures/` data)

**R-16 Create `tests/test_cli_ux.py` (I-26, N-03).** Use fixed timestamps and the `run()` pattern.
- I-03: `test_report_date_excludes_idle_bot_history`, `test_report_date_with_no_records_anywhere`.
- I-06: `test_verify_unknown_bot_fails_and_creates_nothing`, `test_verify_display_name_checks_all_variant_chains`, `test_export_display_name`, `test_anchor_unknown_bot_writes_no_file`.
- I-13: `test_task_end_accepts_summary`, `test_env_task_fallback_note_approval_task_end`, `test_task_end_without_id_warns`, `test_outcome_synonyms`, `test_source_routine_on_task_start`, `test_status_bot_filter`, `test_root_after_subcommand`, `test_tail_n_zero`, `test_tune_apply_reject_exclusive`, `test_verify_json`.
- I-14: `test_status_today_not_multiplied_by_alias_chains`, `test_status_chains_is_per_chain`, `test_open_task_older_than_7_days_still_open`.
- I-19: `test_slug_beats_alias_regardless_of_order`, `test_alias_steal_requires_move`, plus the two tests named in R-02.
- I-20: `test_archive_missing_file_exit_2`, `test_archive_binary_marks_unscanned`.
- `test_exit_code_table`: one command each for 0, 1, 2, 3 (mocked visibility) and 4.
- `test_abbreviations`: assert that each ambiguity listed in N-13 exits 2, and that `--rec`, `--rem`, `--std` and `--allow-p` still work.
- The tests named in R-08, R-10 and R-13 for this file.

**R-17 Extend `tests/test_integrity.py`.**
- The tests named in R-01, R-04, R-05, R-08 and R-09.
- Witness coverage: use `mock.patch.dict(gblog.KIM_BISEO_BREAK, <fixture descriptor>)` so the ack takes the pinned branch. Then:
  - `test_witness_warning_when_ledger_chain_lacks_note`;
  - `test_no_witness_warning_when_note_present`;
  - `test_no_witness_warning_without_ledger_chain`.
  
  Each asserts `fixture_ack is False`.
- Strengthen `test_bot_field_mismatch_warns_and_strict_fails` to assert the exact warning text and the strict problem text.

**R-18 Extend `tests/test_append_safety.py`.** Add the tests named in R-03, R-07, R-13 and R-14. Make `test_state_rebuild_missing_and_genesis` assert the printed seq equals the state seq.

**R-19 Extend `tests/test_malformed_input.py`.** Add the tests named in R-05, R-06, R-07 and R-10. Use a subprocess with `PYTHONIOENCODING=ascii` for the output-encoding cases.

**R-20 Extend `tests/test_tune_loop.py` and `tests/test_redaction_extended.py`** with the tests named in R-11 and R-12.

**R-21 Test hygiene.**
- Add the I-28 git environment (`GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`) in `setUp`/`tearDown` of `CliTest` and `AliasArchiveSyncTest` in `tests/test_gblog.py`. No assertion changes.
- Add `test_sync_commits_gitignore_and_untracks_locks` (R-14) and a concurrent-anchor variant of `test_parallel_appends_and_verify` to their files.
- Extend `tests/test_scripts.py` to mirror R-26: relative root, legacy rc, `.bash_profile`, `.zshrc`, no rc file, checksum mismatch.

### Group G3 — `*.md`

**R-22 `docs/DESIGN.md`.**
- Fix the four contradictions in N-20 so they match the code after R-02, R-05, R-07 and R-15.
- §9: describe the fixture pin (R-09) and state that `TMPDIR` no longer matters.
- Add a short schema for `verify --json` entries, including `missing`, `archives_checked`, `record_error` and `state_problem`.
- §2: add the future-tail warning (R-03) and the invalid-directory FAIL (R-04).
- §6: add the `scanned` rule for `--no-redact`, `bot_field` (in memory only) and `count` in the `proposal` snapshot.

**R-23 `README.md`, `SECURITY.md`, `CHANGELOG.md`.**
- README step 6: replace the blanket rule with an allow-list (`task-start`, `log`, `approval`, `note`, `task-end`, `archive`, `verify`, `anchor`, `report`, `status`, `tail`, `coverage`, `tune`, `capture`, `sync` without `--allow-*`). Name `seal`, `spool replay`, `state rebuild`, `alias` and both override flags as never auto-allowed.
- Exit table, row 2:
  - a usage error (argparse, `error: --…`) wrote nothing;
  - an environment error (`error: <strerror>`) may have written the record, so check `gblog tail -n 1` before retrying;
  - `spool replay` lists what it replayed.
- Upgrade section:
  - the abbreviation list from N-13;
  - `git rm --cached` for already-tracked lock and snapshot files (or "R-14 does it");
  - how to dismiss a stale alert: move the files to `spool/<bot>/replayed/`, add a `gblog note`, then run `gblog spool replay --bot B`;
  - how to retire a chain: remove its anchor files and add a note.
- Alias section: add `--move` and reserved names.
- CHANGELOG:
  - correct the wall-clock claim;
  - add the N-13 rows under "Behaviour changes";
  - add a 0.6.1 section for R-01 … R-15;
  - make the release status agree with `SECURITY.md`.
- `SECURITY.md`: add "an ssh host alias that points at github.com is treated as another host and is not checked".

**R-24 `skills/activity-log/SKILL.md`, `skills/bot-tune/SKILL.md`.**
- Replace "Exit 2 means nothing was written" with the wording from R-23.
- "Automatic evidence": say "interactive bash only" and "browser history (Chromium family and Firefox)".
- Step 1: add "in a routine run, add `--source routine`".
- State where capture runs (see R-25), and repeat the shared-computer attribution limit in one sentence.

**R-25 Routine and template docs.**
- `docs/routine-weekly-tune.md`: open the run with `gblog task-start --bot <slug> --source routine --task "Weekly tune"` and close it. Add the `!`-line rule, the exit 4 rule and the untrusted-text sentence.
- `docs/routine-daily-audit.md`: either add a capture step or change the skill. Pick one place.
- `docs/bot-description.md`: add "If a gblog command exits 4, stop and report."
- `docs/bot-template.md`: change the truncation claim to "while a witness survives".
- `docs/chain-repair-live-apply.md`: after step 8, add "`gblog verify --bot kim-biseo --json` must show `"fixture_ack": false`".

### Group G4 — `install.sh`, `bin/gblog`, `.github`

**R-26 `install.sh` (N-21).**
- Make `GBLOG_ROOT` absolute before `gblog init` (`mkdir -p`, then `cd … && pwd`).
- Legacy detection, after checking the 0.5 script:
  - skip the PATH block when the rc file already has an `export PATH=` line containing `$PREFIX`;
  - skip the ROOT block when it already has the identical `export GBLOG_ROOT=` line;
  - when a different unmarked `export GBLOG_ROOT=` line exists, print a `warning:` with both values and leave the file alone.
- Handle `~/.bash_profile` when it exists.
- Reruns stay idempotent.
- Tests: R-21.
- `bin/gblog`: no change.

**R-27 `.github/workflows/ci.yml`.**
- Add `timeout-minutes: 15` to both jobs.
- Smoke step additions:
  - `gblog note --bot aliases --detail x` must exit 2;
  - `gblog verify --all --json | python -c 'import json,sys; json.load(sys.stdin)'`;
  - `gblog verify --all --strict`;
  - `gblog status --bot ci --root "$GBLOG_ROOT"`;
  - `rm "$GBLOG_ROOT/state/ci.json" && gblog state rebuild --bot ci`;
  - `gblog spool list`.
- Run the unit tests once more on one matrix entry with `TZ=Asia/Seoul LC_ALL=C PYTHONIOENCODING=ascii`.
- Run the 3.13 and 3.14 entries with `python -W error::DeprecationWarning -m unittest …`.

## 5. Suggested order

1. R-02 and R-16 together. They close the item whose test file is missing.
2. R-01, R-03, R-04, R-09 (integrity), with R-17.
3. R-05, R-06, R-07, R-08, R-10 (robustness), with R-18 and R-19.
4. R-11 … R-14, with R-20 and R-21.
5. R-15 once the suite is green.
6. R-22 … R-25 after the code they describe; R-26 and R-27 at any time.

## 6. Not recommended

- **Converting `SystemExit` from `_die` into a return code in `main()`.** Existing tests assert `SystemExit`.
- **Removing `--root` from subparsers or setting `allow_abbrev=False` to restore abbreviations.** Document the ambiguities instead.
- **Refusing to append when the tail is future-dated.** That would stop logging. Warn (R-03) and keep the clamp.
- **Failing writes when `aliases.json` is unreadable.** Losing events is worse than logging under an unresolved name.
- **Dropping the temp-copy condition after R-09.** Keep both conditions; the fixture must never be honoured on a live root.