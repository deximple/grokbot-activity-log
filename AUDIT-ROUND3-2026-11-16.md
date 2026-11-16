<!-- Round-3 audit: claude-fable-5-2, effort=xhigh, batch, org gju20, 10:56 KST. Written before the round-4 commit that closed the 6 pending security gaps (T-01 items may already be fixed). -->
# Round-3 audit — 2026-11-16

Scope: `gblog.py` (reports itself as 0.6.0), `bin/gblog`, `install.sh`, `.github/workflows/ci.yml`, all `*.md`, `tests/*.py`, as they stand after round 1 (I-01 … I-42), round 2 (R-01 … R-27) and the security round (A:…, B:…).

Method: static read only. Nothing was executed and no timing was measured. Statements about what a test does today are traced through the code, not observed; performance findings are complexity arguments. W-20 adds a benchmark script so the performance items can be measured before and after.

## 1. Executive summary

- **Several fixes that the 0.6.1 CHANGELOG, `SECURITY.md` and `docs/DESIGN.md` describe as done are not in the code.** Six tests in `tests/test_security_regressions.py` still carry `@unittest.expectedFailure`, so the suite is green because those attacks still work (T-01).
  - Affected: `sync -m` redaction and the credential warning, browser capture with a non-text title, concurrent `spool replay`, torn-line rollback, the duplicate-key warning, and the archive-record checks.
- **`verify` computes the symlink findings and throws them away.** `odd_files, link_files = self._file_findings(bot)` is never used. The `!` warning for symlinks and the `--strict` failure that `SECURITY.md` ("Auditing a clone") relies on do not exist, and no test covers them (T-02).
- **`gblog --version` still prints 0.6.0** while the docs tell operators that the security fixes are "in 0.6.1" (T-03).
- **R-15 was not done**, although the CHANGELOG and DESIGN §5 say it was. `_exit2`, `_assert_tail_matches`, `_BREAK_KEYS` and `_Lock._held` are still there, and the security round added more dead code (T-05).
- **Regressions from the fixes:**
  - The new shell-hook skip rule logs every `gblog … "$TID"` command typed at a prompt, which lowers shell coverage (T-04).
  - `sync` writes state files for invalid chain directories (T-09).
  - A record clamped across midnight appears in two daily reports (T-14).
  - `sync --re` became ambiguous on argparse and is undocumented (T-17).
- **Large ledgers have several super-linear paths.**
  - `tail -n 1` parses the whole ledger (T-06).
  - Every name lookup re-reads `state/aliases.json` (T-06).
  - `coverage` is O(auto × self) with a `strptime` per pair, and one comprehension in `analyze` is cubic (T-07).
  - `sync` re-hashes every log and archive file on every run, and the daily routine fully verifies every chain three times (T-08).
- **Test gaps:** `test_abbreviations` (R-16) was never written, the `--move` case-variant test (R-10) is missing, there are nine copies of `run()`, and one test blocks for 5 s by construction (T-17, T-18).
- **Read and found present:** R-01 to R-14 (state witness, reserved names on append, future-tail signals, directory attribution, one-snapshot verify, unreadable-state rule, result-list `verify --json`, fixture pin by hash, alias registry lock, tune count), plus the remote guard by parsed host, `--replace-remote`, the committed-content check, and the `seal` guard under the lock.

## 2. Prioritized findings

| ID | Sev | Area | Finding | Evidence | Work item |
|---|---|---|---|---|---|
| T-01 | High | code / docs / tests | Six documented fixes are not implemented; their tests are `expectedFailure` | §3.1 | W-04 … W-09, W-17, W-22 |
| T-02 | High | `verify` | Symlink findings computed and discarded; no test | `verify`: `odd_files, link_files = self._file_findings(bot)`, never read again | W-02 |
| T-03 | Med-High | release | `__version__ = "0.6.0"`; docs say "fixed in 0.6.1"; CHANGELOG says "0.6.1 - Unreleased" | `gblog.py` top; `SECURITY.md` "Supported versions" | W-01, W-22, W-25 |
| T-04 | Medium | shell hook | Skip rule excludes any command containing `$`, `<` or `>`, so `gblog log --bot "$BOT" --task-id "$TID" …` is auto-logged as an unreported shell action | `SHELL_HOOK`: `*"\$"*` arm before `gblog\ *` | W-10 |
| T-05 | Medium | code health | R-15 not done; more dead and duplicated code added since | §3.2 | W-15 |
| T-06 | Medium | performance | `tail` loads and sorts the whole ledger; alias registry re-read and re-validated on every `resolve` / `label` / `canonical` | `tail`: `ledger.records(...)[-args.n:]`; `aliases()` has no cache | W-12 |
| T-07 | Medium | performance | `coverage()` nested loop with `_ts_epoch` and `norm_target` per pair; `analyze` rebuilds a set with dict-equality scans per record | §3.3 | W-13 |
| T-08 | Medium | performance | `verify` keeps raw text and two hash sets per chain; three directory scans; `sync` hashes every evidence file every run; daily routine verifies three times | §3.3 | W-14, W-23 |
| T-09 | Medium | `sync` / state | `sync` stamps `ledger.bots()` (raw), creating `state/my bot.json`; `update_state` writes an undocumented, permanent `state_adopted` object into state | `sync` last loop; `update_state` | W-05, W-22 |
| T-10 | Medium | docs | Drift beyond T-01: lock name, "leaf lock", `--record-baseline`, "output line", rollup problem text, warning list | §3.4 | W-22 |
| T-11 | Low-Med | `capture files` | Documented `--record-baseline` flag does not exist; a new baseline still prints `files: 0 change(s) …` on stdout | `_build_parser` "files"; `_main` | W-07 |
| T-12 | Low | robustness | `alias list` exits 2 when `logs/---` exists and a Bot is registered; `anchor` prints missing-chain problem lines without `_term` | `alias list`: `ledger.canonical(c)` over `ledger.bots()`; `anchor`: `"      - %s\n" % p` | W-11 |
| T-13 | Low | hygiene | Per-pid temp files are never removed on failure; the write-fsync-replace sequence exists six times | `write_state`, `_write_aliases`, `_atomic_json`, `save_snapshot`, `save_proposals`, `archive_file` | W-09 |
| T-14 | Low | `report` | A clamped record whose `ts` day and `clock_regressed` day differ is listed in both daily reports; `status` counts it once; every later day file is fully JSON-parsed for `report --date` | `records()` `later` branch | W-12 |
| T-15 | Low | code health | Four error styles (`_die`/`_exit2` exits from library code, `_UsageError` raised by `sync()`, prefixed `ValueError` text from `tune_apply`, ad-hoc `seal:` / `archive:` / `state rebuild:` lines) | §3.2 | W-16 |
| T-16 | Low | code health | Long functions: `_main` (about 430 lines), `Ledger.verify` (about 230), `analyze` (about 200), `sync` (about 120) | — | W-16 |
| T-17 | Medium | tests | `test_abbreviations` missing; `--re` on `sync` newly ambiguous; no case-variant `--move` test; weak assertions; stale `pending:` comments; nine `run()` copies | §3.5 | W-17 … W-21 |
| T-18 | Low | tests | One test waits 5 s by design; several deep-recursion and multi-process tests dominate run time; no duration report | §3.5 | W-20, W-25 |
| T-19 | Low | scripts | `bin/gblog` and `install.sh` run `cd` on relative paths without clearing `CDPATH` | `bin/gblog`: `cd -P "$(dirname "$src")"`; `install.sh`: `cd "$PREFIX"`, `cd "$(dirname "$SELF")"` | W-24 |
| T-20 | Low | CI | Nothing fails when a fix lands without removing its `expectedFailure`, or when docs name a version the binary does not report | `ci.yml` | W-25 |

## 3. Evidence

### 3.1 T-01 — documented as fixed, not implemented

| # | Claim (CHANGELOG 0.6.1 unless noted) | Code today | Test still `expectedFailure` |
|---|---|---|---|
| a | "the `sync -m` commit message" is redacted; `sync --remote` with user and password "prints a warning" (B:L9) | `commit_msg = message or …`, no `redact`; no warning anywhere in `sync` | `test_secrets_do_not_reach_git_unnoticed` |
| b | Browser rows with a non-text title or out-of-range time no longer crash or re-import; marker saved "per history file"; database opened "read-only" (A:L-2) | A `bytes` title reaches `append` and raises `RecordError` after earlier rows were appended. Markers are saved once, after the loop, so those rows are imported again on the next run. `sqlite3.connect(copy)` is read-write. `fromtimestamp` is unguarded. | `test_browser_capture_survives_a_non_text_title` |
| c | "two concurrent `spool replay` runs could append the same spooled event twice" fixed (B:L11); DESIGN §4 repeats it | `spool_replay` checks only whether the tail record has the same `spool_id`, outside the lock, and calls `append` without `_guard`. `_GuardSkip` is defined and never used. The final `os.replace` raises `FileNotFoundError` when another run moved the file. | `test_concurrent_spool_replay_appends_each_event_once` |
| d | A part-way write "is rolled back to the size the file had before the call" (A:L-3) | Rollback runs only `if written and st_size == size + written`. `written` is updated after `os.write` returns, so bytes written by a call that then raises are never removed. | `test_failed_append_leaves_no_torn_line` |
| e | New `!` warnings: "a line with duplicate keys", "a `ts` that is not in normalised form" (A:L-5) | `_layout_findings` runs the canonical comparison only `if strict`; there is no `ts`-form check in `verify` at all | `test_duplicate_keys_warn_in_default_mode` |
| f | `verify` reports an archive record without `path` or 64-hex `sha256` and a file name that is not `<sha256>.gz`; `archive` re-hashes an existing file before dedup (B:L4) | `verify_archives`: `if … not r.get("path"): continue`; no name or hash-shape check. `archive_file`: `existed = os.path.exists(full)`, no re-hash. | `test_archive_records_are_checked_one_by_one` |

Item f needs a design decision before it is implemented as written. `gblog log --type archive` is still accepted (README: "housekeeping types"), and such a record has no `path`. If that became a FAIL, any auto-allowed `gblog log` call could make a chain fail forever, and older ledgers may already hold such records. W-04 makes the path-less case a warning.

### 3.2 T-05 / T-15 — dead code, duplicates, error styles

Never done from round-2 R-15:

| Item | State |
|---|---|
| `_exit2` | Still defined, identical to `_die`; used in `_check_reserved`, `_add_alias`, `capture_files` |
| `_assert_tail_matches`, `_BREAK_KEYS` | No caller, no reference |
| `coord_keys` in `seal_break` | Duplicates `_COORD_KEYS` |
| `_Lock._held` | Still present. DESIGN §5 rule 3 says `_Lock` "keeps no re-entrancy state". No code path nests a lock on the same file. |
| `chain_records` | Stale comment ("it would fall back to every file"); local `_key` duplicates `_seq_int` |
| `# -- iteration` label, local `import tempfile` in `capture_browser`, `env_ok` in `sync` | Unchanged |
| `capture files` path check | Still in `_main` and in `capture_files` |
| `log` source expression | Still duplicates `_source_field` |

Added since:

| Item | Evidence |
|---|---|
| `_GuardSkip` | Defined, never raised or caught |
| `Ledger.chain_hashes` | No caller after R-05 |
| `irregular_logs` and `_file_findings` | Two scans for the same "not a regular file" list; half of `_file_findings` is discarded (T-02) |
| `_touch_private` | Redundant with `_open_lock`, which creates the file `0o600` |
| `save_markers(strict=True)` | Parameter never passed |
| `_tmp_name` | `_atomic_json` builds the same name inline |
| `read_anchor` | Same body as `_read_json` |
| Missing-chain merge | `anchored_missing` + `orphan_witnesses` merged by hand in `_main` (verify) and in `_anchor`; the "chain was anchored on … but logs/… is missing" text is formatted in three places |
| Side-file skip list | `orphan_witnesses` repeats the `RESERVED` knowledge (`aliases`, `capture-files-`, `.alert`, `.alert.resolved-`) |
| `.gitignore` body | Literal in `init()` and in `_sync_evidence()` |
| `--ts … is earlier than the last record` message | In `append` and in `archive_file` |
| `isinstance(x, int) and not isinstance(x, bool)` | About ten copies |
| `valid_chains` | Keeps its "warned" set in `self.__dict__.setdefault(...)` instead of `__init__` |

Error styles (T-15): `tune_apply` raises `ValueError("error: … is corrupt")` with the prefix inside the message, and `ValueError("unknown proposal id …")` without it. Both are printed as given, so one line has `error:` and the other does not. `sync()` raises the CLI-private `_UsageError`. `safe_name`, `resolve` and `capture_files` call `sys.exit` from library code; existing tests assert `SystemExit`, so that part must stay.

### 3.3 T-06 … T-08 — cost on a 5k–50k-record chain

| Command | Today | Why |
|---|---|---|
| `append` | O(day files) stats + 4 fsyncs | `log_files` lists and `isfile`s every day file (twice on the genesis branch). `_append_line` fsyncs the directory even when the file already existed. State is read twice. |
| `tail -n 1` | Full parse and sort of every chain | `ledger.records(bot)[-n:]`. The README and the skill tell Bots to run this before retrying after exit 2. |
| name lookup | One read and validation of `aliases.json` per call | `tail` calls `label()` per line (two reads each). `chains_for` calls `canonical` per chain. Every `task-end` goes through `chains_for`. |
| `status` | Parses 30 days of every chain; state read up to four times per chain | `chain_records(c, since=month)`. `read_state(c)` is called again for `future_tail` and for the `CLOCK` lines, although `_state_view` already holds `updated`. `alert_info` is called twice. |
| `report --date D` | Parses every day file after D for every chain | The `later` branch calls `_parse_line` on each line to look for `clock_regressed`. |
| `verify` | About 4 passes over the items; raw text kept for every line; 3 directory scans | `_chain_snapshot` stores `(rel, lineno, rec, text)`, and the text is only needed for the canonical comparison. `log_files`, `irregular_logs` and `_file_findings` each list the directory. The UTF-8 decode happens twice per line. Parsing runs under the shared lock. |
| `verify --all` | Retains two hash sets per chain until exit | `res.hashes` / `res.own_hashes` stay attached to every entry in `results`. |
| `coverage` | O(auto × self), `strptime` and `urlsplit` per pair | Inner loop calls `_ts_epoch(s.get("ts"))` and `norm_target(...)` for every candidate. |
| `tune` | As `coverage`, plus a cubic comprehension | `r.get("task_id") in {k for k, t in tasks.items() if t in stale}` rebuilds the set for every `task_start` record; `t in stale` compares task dicts by value. `compute_metric(kind="coverage")` runs `coverage` again per proposal. |
| `sync` | Hashes every log, anchor-history and archive file; one `git add -f` per 200 files | `_sync_drift` reads each checked file in full; `_sync_evidence` force-adds every file whether or not an ignore rule applies. |
| daily routine | Three full verifies per chain, archives included | Step 1 `verify --bot … --contains`, step 2 `verify --all --record`, then `anchor` (which verifies again). |

### 3.4 T-10 — documentation drift not covered by T-01

| File | Statement | Code |
|---|---|---|
| DESIGN §3, §5; CHANGELOG "Added" | `state/anchors.lock` | `state/.anchors.lock`. The dot is deliberate: a Bot named `anchors` would otherwise share the file. |
| DESIGN §5 rule 4 | `anchors.lock` is a leaf, held "around the read-merge-write" of the day rollup | Held for the whole `anchor` run; bot locks (shared, in `verify`) are taken while it is held. The order is always anchors → bot, so there is no cycle, but it is not a leaf. |
| DESIGN §5 rule 3; CHANGELOG "Changed" | `_Lock` keeps no re-entrancy state; dead code removed (R-15) | T-05 |
| DESIGN §7 table | Day-rollup witness prints `chain was anchored on <ts> but logs/<bot> is missing` | `chain was anchored (day rollup <date>) but logs/<name> is missing` |
| DESIGN §7 warnings; CHANGELOG | `!` for a symlink, duplicate keys, non-normalised `ts`; `--strict` turns them into problems | T-02, T-01 e |
| `SECURITY.md` "Auditing a clone" | "`--strict` also fails on a symlink among a chain's files" | T-02 |
| DESIGN §2, §7 step 7; `SECURITY.md` guarantees | Archive record address checks | T-01 f |
| DESIGN §3, §6 | State holds `seq`, `head`, `updated`, `last_sync`, capture markers | `update_state` also writes `state_adopted: {how, seq, ts}`. `append` and `state rebuild` preserve it, so `verify` warns about it forever. Not documented. |
| README capture section; CHANGELOG | New baseline "is said on the output line"; `capture files --record-baseline` | T-11 |
| CHANGELOG "Changed" | Browser marker per history file; copied database read-only; "uniquely named temporary files" | T-01 b; names are `<path>.<pid>.tmp` |
| README "Upgrading" table | `sync --r` ambiguous between `--remote`, `--root` | Also `--replace-remote`; `--re` was valid in 0.6.0 and is now ambiguous |
| README exit table | Exit 1 = integrity FAIL or not pushed | Also an unexpected internal error (`error: internal error …`) and the committed-content mismatch |
| README shell-hook paragraph | "Only a plain `gblog …` command is skipped" | A plain command with `$VAR`, `<` or `>` is logged (T-04) |

Three recovery paths for a missing state file leave three different traces: `append` writes a record field (permanent `!`), `sync` / `capture browser` write a state key (permanent `!`), and `state rebuild` writes a note (no `!`). None of the docs says the `!` lines are permanent or how an operator should treat them.

### 3.5 T-17 / T-18 — tests

Missing or wrong:

| Item | Detail |
|---|---|
| `test_abbreviations` (R-16) | Not in `tests/test_cli_ux.py`. Nothing checks the N-13 ambiguities or the forms the README says still work. |
| `test_alias_steal_requires_move` "with a case variant" (R-10) | The test uses `legacy` only; the case-insensitive comparison is untested. |
| `sync --replace-remote` | Only the refusal is tested, by end state. No assertion on exit 2 or the message; no test of the positive path or of the recorded `git remote` record. |
| `_ssh_alias_target` | No test. |
| `_sync_drift` | Only the nested-`.gitignore` case, which `git add -f` fixes before the drift check runs. The check itself is never seen to fire. |
| Shell hook | No test that a plain `gblog …` command is skipped. |
| `verify` symlink warning | No test (T-02). |

Weak assertions (would pass with the feature broken):

| Test | Problem |
|---|---|
| `test_nan_in_a_log_line_is_flagged` | `assertTrue(res["warnings"] or not strict["ok"])`: any warning satisfies it |
| `test_adoption_leaves_a_lasting_trace_for_verify` | `"state" in w` |
| `test_missing_snapshot_rebaseline_is_announced` | `"baseline" in (out + err).lower()` |
| `test_non_string_text_fields_do_not_crash_readers` | `assertIn(rc, (0, 1))` for `verify` |
| `test_sync_does_not_silently_retarget_origin` | Second `sync` result is not asserted |
| `test_verify_json` (unknown bot) | Asserts "is a list", not the entry |

Hygiene:

- About thirty tests in `tests/test_security_regressions.py` keep a `# pending: …` comment although they have no `expectedFailure` marker. The module docstring says a test without a marker is fixed.
- `tests/test_cli_ux.py` keeps a `# deviation:` comment above `test_reserved_bot_names_refused_on_append`, which now passes.
- There are nine copies of `run()` (seven test modules plus `_invoke` and `run_cli` variants) with different exception handling. `tests/test_gblog.py` and `tests/test_chain_repair.py` do not fold `SystemExit`.
- `test_exit_code_table` ends in `skipTest` when git is missing, so the 0/1/2/4 assertions are reported as skipped.

Slow or timing-dependent:

| Test | Cost or risk |
|---|---|
| `test_concurrent_anchor_runs_keep_both_rollup_entries` | `thread.join(5)` while the main thread holds `.anchors.lock`: always waits the full 5 s. The spool-replay test may gain the same wait, depending on how W-08 is implemented. |
| `test_deeply_nested_line_fails_not_crash`, `test_deep_json_line_does_not_stop_other_chains`, `test_deeply_nested_anchor_file_does_not_crash_verify` | 50 000 to 200 000 nesting levels, several CLI runs each |
| `test_parallel_appends_and_verify`, `test_parallel_appends_anchor_and_verify` | 320 appends from spawned processes, four fsyncs each, with a busy verify loop |
| `test_many_pem_begin_markers_do_not_stall_redaction` | Wall-clock limit of 5 s including interpreter start; the only pure timing assertion |
| `test_ts_traversal_rejected` | Probes the fixed path `/tmp/x.jsonl`; two parallel runs on one host can interfere |

## 4. Fix work items

Rules for every item:

- Standard library only, Python 3.8+.
- Record bytes, the hash rule and schema `v: 1` do not change.
- Existing CLI flags keep working. New stdout text is additive (a suffix or new lines); new diagnostics go to stderr.
- Every existing test passes unmodified unless the item says otherwise.
- A healthy ledger written only by gblog prints the same `verify --all` output before and after, except where an item lists a new `!` line.

### Group G1 — `gblog.py`

**W-01 Report version 0.6.1 (T-03).**
- Set `__version__ = "0.6.1"`.
- Effect: `gblog --version` and the report and tuning footers show 0.6.1. Nothing else changes.
- Tests: none new. `tests/test_scripts.py` reads `gblog.__version__`.

**W-02 Use the file findings in `verify` (T-02).**
- Replace `log_files` + `irregular_logs` + `_file_findings` inside the shared lock with one `os.scandir` pass per directory. It returns the regular `*.jsonl` paths (sorted), the irregular entries and the symlink findings.
- `problems` gets `<rel>: not a regular file` for each irregular entry (same text as today).
- The symlink lines `<rel>: symbolic link (gblog does not write through one)` join `layout_warnings`: `!` lines by default, problems with `--strict`. Paths checked: `logs/<bot>`, each entry in it, `state/<bot>.json`, `anchors/<bot>.json`, `anchors/<bot>.history.jsonl`, `archive/<bot>` and its entries. Cap at `_MAX_LAYOUT_FINDINGS`.
- Delete `irregular_logs`.
- Compat: a ledger whose `archive/<bot>` is a symlink gains one `!` line. Run `verify --all` on a copy of the production ledger before release. If the line appears, it is expected; keep the CHANGELOG entry.
- Tests (`tests/test_integrity.py`):
  - `test_symlinked_day_file_warns_and_strict_fails`: move a day file inside the root and symlink it back. Expect `ok` true, the exact warning, the `      ! ` line, and `--strict` exit 1 with the `      - ` line.
  - `test_symlinked_state_and_anchor_files_warn`.
  - `test_verify_lists_chain_directory_once` (mock `os.scandir`, count calls for `logs/<bot>`).

**W-03 Duplicate-key warning; strict `ts` form (T-01 e).**
- `_layout_findings`: for each readable line compare `json.dumps(rec, ensure_ascii=False, sort_keys=True)` with the stored text in both modes.
  - Strict: unchanged (`non-canonical line (duplicate keys or reformatted)`).
  - Default: on a mismatch only, re-parse that line with an `object_pairs_hook` that detects a repeated key at any depth. If found, warn `<rel>:<n>: duplicate keys in line`. A line that is only reformatted stays silent, as today. Cap at 20, then `<k> more line(s) with duplicate keys`.
- Strict only: `<rel>:<n>: ts is not in normalised form` when `ts` does not match `YYYY-MM-DDTHH:MM:SSZ`. It is not a default-mode warning, because records written with a free-form `--ts` before 0.6 would change the output of a healthy ledger.
- Tests:
  - Remove the marker from `test_duplicate_keys_warn_in_default_mode` and assert the exact text.
  - Add `test_reformatted_line_is_silent_in_default_mode`.
  - Add `test_strict_flags_non_normalised_ts` (raw hash-valid line with `2024-06-01T10:00:01+00:00`).
  - `test_duplicate_key_line_strict_fails` must pass unchanged.

**W-04 Archive record checks and dedup (T-01 f).**
- `verify_archives`, for each `type: archive` record:
  - No string `path`, or `sha256` not 64-hex: return it as a **warning**, `archive record #<seq>: no stored file (path or sha256 missing)`. `Ledger.verify` adds it to `warnings`, or to `problems` with `--strict`. Change the return value to `(problems, warnings)` and adapt the one caller in `Ledger.verify`. The direct call in `tests/test_integrity.py` (`test_archive_path_escape_is_problem`) must keep working: keep `verify_archives(ledger, records)` returning the problems list and add `verify_archive_findings` for the pair.
  - `path` valid but its file name is not `<sha256>.gz`: problem `<path>: archive path is not addressed by its sha256`.
  - Existing checks unchanged.
- `archive_file`: when the target exists (use `os.path.lexists`), gunzip and hash it. If it is a symlink, unreadable or does not hash to `digest`, rewrite it through the temp file and `os.replace`, and report `_dedup: False`.
- Reason for the warning: see §3.1; `gblog log --type archive` must not be able to make a chain FAIL forever.
- Tests: in `test_archive_records_are_checked_one_by_one`, change the path-less case to expect a warning by default and FAIL with `strict=True`, and remove the marker. Add `test_archive_name_must_be_its_sha256` and `test_log_type_archive_record_is_a_warning_not_a_fail` (CLI `log --type archive`, then `verify` exit 0 with the `!` line).

**W-05 `sync` (T-01 a, T-09, T-08).**
- `commit_msg = redact(message)` when `-m` is given.
- When `--remote` matches `^https?://[^/@\s]*:[^/@\s]*@` (case-insensitive), print on stderr: `warning: the remote URL contains a password; git stores it in .git/config as written. Use a credential helper instead.`
- Stamp `last_sync` on `ledger.valid_chains()`, not `ledger.bots()`.
- Force-add only what an ignore rule hides: run `git ls-files --others --ignored --exclude-standard -z -- logs anchors archive state spool .gitignore`, intersect with the evidence list, and pass that to `git add -f`. The committed-content check stays as the backstop.
- Tests:
  - Remove the marker from `test_secrets_do_not_reach_git_unnoticed`.
  - Add `test_sync_does_not_stamp_invalid_chain_dirs` (`logs/my bot` exists; no `state/my bot.json` and no `state/my-bot.json` after `sync --no-push`).
  - `test_sync_pushes_new_records_despite_nested_gitignore` must pass unchanged.

**W-06 `capture browser` (T-01 b).**
- Per row: skip a row whose URL is not `str`. A `bytes` title is decoded with `errors="replace"`; any other non-string title becomes `""`.
- A visit time that cannot be converted (`OverflowError`, `OSError`, `ValueError`) or is more than one day in the future is skipped **without** advancing the marker.
- Save the marker after each history file. If an append raises part-way through a file, save the marker at the last appended row's time, then re-raise.
- Do not add read-only opening (`mode=ro` on a WAL copy is fragile). Remove that claim in W-22.
- Tests:
  - Remove the marker from `test_browser_capture_survives_a_non_text_title`.
  - Add `test_out_of_range_visit_time_is_skipped_and_does_not_move_marker`.
  - Add `test_marker_saved_per_history_when_a_later_history_is_refused` (two histories; tail mismatch on the second chain write; rerun imports nothing from the first).

**W-07 `capture files` (T-11).**
- Add `--record-baseline`. When a new baseline is taken, also append `{"type": "note", "source": "auto", "capture": "files", "detail": "file capture: new baseline of <n> file(s) (<reason>)"}` with reason `first run`, `snapshot missing`, `snapshot unreadable` or `path changed`.
- `capture_files` returns `new_baseline: <reason or None>`.
- Stdout line: append ` (new baseline: <reason>)` on a new baseline and `, <k> pending` when changes exceed `--limit`. The prefix `files: <n> change(s) against a snapshot of <m> file(s)` is unchanged.
- Remove the duplicate path check from `_main`. The function's message must end with "nothing was logged and the snapshot is unchanged".
- Compat: `capture files --r` becomes ambiguous (`--root`, `--record-baseline`); document it in W-22.
- Tests: `test_record_baseline_appends_note`, `test_new_baseline_is_on_the_output_line`. `test_path_change_rebaselines` and `test_corrupt_snapshot_rebaselines` must pass unchanged (they assert `startswith("files: 0 change(s)")` and no records without the flag).

**W-08 `spool replay` appends each event once (T-01 c).**
- Pass a `_guard` to `append`. Under the bot lock it scans the chain's day files dated on or after the spool file's stamp date for a line containing the `spool_id` bytes, parses the hits, and raises `_GuardSkip` when a record with that `spool_id` exists.
- On `_GuardSkip`, treat the event as already replayed. Move the file with `FileNotFoundError` ignored.
- Drop the tail-only check; the guard covers the crash-before-move case too.
- Tests:
  - Remove the marker from `test_concurrent_spool_replay_appends_each_event_once`.
  - Add `test_replay_after_crash_and_later_appends_does_not_duplicate` (append with `spool_id` by hand, add two more records, leave the spool file, replay: no second copy, file moved).

**W-09 Size-based rollback and one atomic-write helper (T-01 d, T-13).**
- `_append_line`: on any exception after the first `os.write`, read `cur = os.fstat(fd).st_size`. If `size < cur <= size + len(data)`, call `os.ftruncate(fd, size)`. Earlier bytes are never touched.
- Add `_atomic_write(path, write_fn, mode="w", keep=True, fsync_dir=True)`: temp name from `_tmp_name`, `_open_private`, flush, fsync, `os.replace`, optional directory fsync; on any failure, unlink the temp file and re-raise.
- Use it in `write_state`, `_write_aliases`, `_atomic_json`, `save_snapshot`, `save_proposals` and `archive_file`.
- Tests:
  - Remove the marker from `test_failed_append_leaves_no_torn_line`.
  - Add `test_failed_state_write_leaves_no_temp_file` (mock `os.fsync` to raise `ENOSPC` once; no `*.tmp` under `state/`).
  - `test_enospc_in_write_state_recovers` must pass unchanged.

**W-10 Shell hook skip rule (T-04).**
- In `SHELL_HOOK`, replace the `*"\$"*` alternative with `*"\$("*`. Keep the backtick, `;`, `&`, `|`, `<`, `>` and newline alternatives.
- Result: `gblog log --bot "$BOT" --task-id "$TID" …` is skipped again; `gblog note --detail "$(cat x)"` is still logged.
- The CI lint (`bash -n`, shellcheck) must still pass. Installed hooks are not updated; README "Upgrading" item 6 already covers reinstalling.
- Tests: extend `test_shell_hook_logs_commands_that_mention_gblog` with skipped commands (`gblog status`, `gblog log --bot "$BOT" --task-id "$TID" --action shell --target ls`) and logged ones (`gblog note --detail "$(id)"`, ``gblog note --detail "`id`"``).

**W-11 Small robustness fixes (T-12).**
- `alias list`: build the chain set from `ledger.valid_chains()`.
- `anchor` CLI: pass the missing-chain problem lines and the bot names through `_term`.
- `chain_name`: use `valid_chains()` for the "is a chain directory" test.
- Tests: `test_alias_list_survives_dash_only_dir`, `test_anchor_missing_chain_lines_have_no_escapes` (anchor `ts` holding `\x1b[2J` in `anchors/<bot>.json`, `logs/<bot>` removed).

**W-12 Reader cost: registry cache, `tail`, `status`, `report` (T-06, T-14).**
- Registry cache:
  - Add `Ledger.cache_aliases = False`. When true, `_read_aliases` returns the first result for the life of the instance; `_write_aliases` clears it.
  - `_main` sets it to true. Library callers are unchanged.
- `tail`:
  - Add `Ledger.tail_records(bot, n)`. Per chain, read day files newest first and stop once `n` readable records are collected. Apply the same directory attribution, merge, sort with the existing key, return the last `n`.
  - Output is identical for every chain whose timestamps do not go backwards, which is every chain that passes `verify`.
  - The skipped-line warning covers only the files read.
- `status`:
  - Take `updated` from the `_state_view` already read. Call `alert_info` once per chain and reuse it for the JSON rows and the text lines.
- `report --date`:
  - In the `later` scan, skip any raw line that does not contain `b'"clock_regressed"'` before parsing.
  - A record in the day-D file whose `clock_regressed` is a normalised timestamp on another day is listed on that other day only. This makes `report` agree with `status`.
- Tests (`tests/test_cli_ux.py`):
  - `test_tail_reads_only_the_newest_files` (wrap `Ledger.iter_records`, assert the files opened).
  - `test_tail_matches_full_read` (several chains, an alias merge, `n` in 1, 2, 5, 100).
  - `test_registry_is_read_once_per_command` (count `_read_json` calls for `aliases.json` during `tail -n 50`).
  - `test_clamped_record_is_reported_on_its_wall_clock_day_only`.
  - `test_clamped_records_still_appear_in_todays_report` must pass unchanged.

**W-13 `coverage` and `analyze` (T-07).**
- `coverage`:
  - Precompute the epoch and normalised target of every self record once.
  - Index the self records by `(bot, action, normalised target)`, keeping record order.
  - For each auto record, scan only that list: skip used entries and entries outside the window, take the first hit. An empty normalised target never matches, as today.
  - Results are identical.
- `analyze`:
  - Compute the one-day cutoff once.
  - Build `stale_ids = {task_id …}` once and select evidence with `r.get("task_id") in stale_ids`.
- Tests (`tests/test_tune_loop.py`):
  - `test_coverage_matches_reference_implementation`: keep the old nested loop as a helper in the test file; compare on a seeded pseudo-random set of 300 auto and 600 self records with repeated targets.
  - `test_stale_task_evidence_unchanged`.

**W-14 `verify`, `append`, `sync` cost (T-08).** Do each bullet separately and measure with W-20.
- `verify`: under the shared lock read each log file's bytes only. After the lock is released, split, decode once, parse, and compute the canonical-comparison flag per line; do not keep the raw text. Results and text output are unchanged.
- `verify` CLI loop: after `--contains` has used them, set `res.hashes` and `res.own_hashes` to empty sets so `results` does not retain them.
- `append`: skip `_fsync_dir` in `_append_line` when the file already had data (`size > 0`). Read state once and pass it from `_append_head` to the final update.
- `sync`: keep `.git/gblog-sync-cache.json`, mapping path → `{blob, size, mtime_ns}` for files verified by the last run. Skip the hash when the HEAD blob id and the file's stat both match.
  - The file lives inside `.git`, so it is neither committed nor in the ledger tree.
  - It is forgeable by anyone with file access, which is the adversary `SECURITY.md` already excludes for `git`. The CLI-only adversary cannot write it.
- Tests:
  - `test_append_fsyncs_directory_only_for_a_new_day_file` (count `_fsync_dir` calls for `logs/<bot>`).
  - `test_sync_rehashes_only_changed_files` (wrap `_git_blob_id`).
  - `test_sync_cache_does_not_hide_a_changed_file` (rewrite a committed day file with same-length bytes and a new mtime; drift is reported).
  - `test_anchor_written_after_snapshot_does_not_fail_verify` and the parallel tests must pass unchanged.

**W-15 Remove dead code and duplicates (T-05).** Behaviour-neutral; do it after W-02 … W-14.
- Delete `_exit2` (use `_die`), `_assert_tail_matches`, `_BREAK_KEYS`, `_GuardSkip` if W-08 did not use it, `chain_hashes`, `_touch_private` and the `strict` parameter of `save_markers`.
- `seal_break` uses `_COORD_KEYS`. `_Lock` loses `_held` and the `threading` import.
- `read_anchor` returns `_read_json(self._anchor_file(bot))`. `_atomic_json` uses `_tmp_name` (or W-09's helper).
- Add `Ledger.missing_chains()` returning `{name: [problem lines]}`; use it in `_main` (verify), `_anchor` and `missing_witnesses`.
- Add `_is_side_file(name)` next to `is_reserved`, a `_GITIGNORE_BODY` constant, a `_past_ts_error(...)` helper and `_is_int(x)`.
- Initialise `_warned_chain_dirs` in `__init__`. Fix the stale comment and the local sort key in `chain_records`. Rename `env_ok` to `committed`. Remove the local `import tempfile`. Use `_source_field` in `log`.
- Keep `_tail_head` (tests use it) and `_GITHUB_REMOTE_RE` (tests import it); add a one-line comment to each.
- Tests: the existing suite.

**W-16 Split long functions; one error convention (T-15, T-16).** Last item; mechanical.
- `_main` becomes a dispatch over `_cmd_<name>(args, ledger, out)` functions, bodies moved verbatim.
- `Ledger.verify` is split into `_eligible_acks`, `_walk` and `_state_findings`. Do not change the order in which problems and warnings are added.
- `tune_apply` raises `_UsageError` subclasses with messages without the `error:` prefix. The CLI prints `error: <message>` for all three cases, so `unknown proposal id …` and `no proposals yet …` gain the prefix (stderr only).
- `sync()` raises a public `SyncRefused(ValueError)` instead of `_UsageError`; `main` maps it to exit 2 with the same text.
- Leave `SystemExit` from `_die` as it is (round 2, §6).
- Tests: `test_cli_output_is_unchanged_by_refactor` in `tests/test_cli_ux.py`: a fixed-timestamp ledger and a table of commands with expected stdout written out in the test.

### Group G2 — `tests/*.py` (do not touch `tests/fixtures/` data)

**W-17 Close the `expectedFailure` gap (T-01, T-17).**
- Remove each marker in the same change as its fix (W-03 … W-09).
- Delete every `# pending:` comment above a test that has no marker. Replace it with `# fixed: <id>`. Delete the `# deviation:` comment in `tests/test_cli_ux.py`.
- Add `tests/PENDING.txt` (one `Class.test_name` per line, empty when nothing is pending) and `tests/test_meta.py::test_expected_failures_are_listed`. The test parses the test sources with `ast`, collects every `@unittest.expectedFailure` test and asserts the set equals the file. A marker can then not be added or left behind silently.

**W-18 Add the missing round-2 tests (T-17).**
- `tests/test_cli_ux.py::test_abbreviations`:
  - Exit 2 for: `verify --r`, `sync --r`, `sync --re`, `sync --a`, `sync --al`, `sync --allow`, `tune --r`, `tune --s`, `capture shell-hook --r`, `archive --s`, `log --s`, and `capture files --r` after W-07.
  - Still accepted: `verify --rec`, `sync --rem`, `tune --rej`, `tune --std`, `archive --std`, `log --so`, `sync --allow-p` (visibility mocked, `--no-push` where needed).
- Extend `test_alias_steal_requires_move` with `--alias Legacy` against a registered `legacy`: refused without `--move`, moved with it.
- `tests/test_capture_sync.py`:
  - `test_replace_remote_changes_origin_and_records_it`: exit 0, origin changed, a `sync` record with target `git remote` and detail `origin <old> -> <new>`.
  - `test_remote_change_without_flag_is_usage_error`: exit 2, message contains `--replace-remote`, origin unchanged.
  - `test_ssh_alias_of_github_is_checked_as_github`: `subprocess.run` mocked to answer `ssh -G gh-backup` with `hostname github.com`; `_ssh_alias_target("git@gh-backup:o/r.git")` returns `https://github.com/o/r.git`. Also test the failure and non-GitHub cases returning the URL unchanged.
  - `test_sync_drift_detects_rewritten_file` (calls `_sync_drift` directly): commit a day file, overwrite it with different bytes of the same length → listed; append to it → not listed; an archive file changed → listed.

**W-19 Tighten weak assertions (T-17).**
- `test_nan_in_a_log_line_is_flagged`: assert the exact warning `…:2: non-finite number (NaN or Infinity) in record` and that strict fails with the same text.
- `test_adoption_leaves_a_lasting_trace_for_verify`: assert the exact `appended while state/ops.json was missing; …` warning.
- `test_missing_snapshot_rebaseline_is_announced`: assert the stdout suffix from W-07 and the stderr notice.
- `test_non_string_text_fields_do_not_crash_readers`: assert `verify` exit 0 and at least one `field '<name>' is not a string` warning.
- `test_verify_json`: assert the synthetic entry for the unknown bot (`ok` false, `records` 0, `head` null, `missing` true).
- `test_exit_code_table`: move the exit-3 part into `test_exit_code_3_sync_refusal` with `skipUnless(GIT)`.

**W-20 Shared helpers, slow tests, benchmark (T-17, T-18).**
- Add `tests/_support.py` with `run(argv, root)`, `run_raw(argv)`, `run_cli(argv, root, encoding="utf-8", extra_env=None)`, `stamp(n, day)` and `GIT_ENV`. Replace the local copies module by module. Keep each module's current exception behaviour where its tests depend on it: `tests/test_gblog.py` asserts `SystemExit`, so it gets `run(..., fold_exit=False)`.
- `test_concurrent_anchor_runs_keep_both_rollup_entries`: replace `thread.join(5)` with `thread.join(0.3)`. With the lock in place the other run is still blocked after 0.3 s, which is the point of the test; without it the run finishes within the join.
- Deep-nesting tests: lower the depth to 20 000. On a default CPython build any nesting this deep still makes `json.loads` raise `RecursionError`.
- `test_ts_traversal_rejected`: use a per-run name under `tempfile.gettempdir()` instead of `/tmp/x`.
- Add `tests/bench_large_ledger.py` (not collected by discovery). It writes a 50 000-record chain plus 30 day files of padding directly (no fsync), then times `verify`, `status`, `report --date yesterday`, `tail -n 1`, `coverage`, `tune --stdout` and 100 appends, and prints one line per command. No assertions.

**W-21 Resource hygiene in `tests/test_gblog.py`.** Replace the bare `open(...).read()` / `open(...).write(...)` calls with `with` blocks. No assertion changes.

### Group G3 — `*.md`

**W-22 Make the docs say what the code does (T-01, T-03, T-09, T-10).**
- `CHANGELOG.md`:
  - Give 0.6.1 a release date once W-01 … W-11 are merged.
  - Until an item's fix is merged, move it to a new "Known open findings" list at the top of 0.6.1. Do not leave it under "Security" or "Fixed". This applies to T-01 a–f.
  - Remove "opens the copied database read-only". Change "uniquely named temporary files" to "per-process temporary file names".
  - Change `state/anchors.lock` to `state/.anchors.lock`.
  - Keep "Dead code … removed (R-15)" only when W-15 is merged.
  - Add under "Behaviour changes": `sync --re` is ambiguous (`--remote`, `--replace-remote`); after W-07, `capture files --r` is ambiguous.
  - Reword the default-mode warning list to match W-02 / W-03 (symlink and duplicate keys warn; `ts` form is strict only) and W-04 (path-less archive record is a warning).
- `docs/DESIGN.md`:
  - §3 and §5: `state/.anchors.lock`. It is held for the whole `anchor` run, taken before any bot lock and never while one is held; it is not a leaf. Explain the dot (a Bot named `anchors`).
  - §3 and §6: add the state key `state_adopted: {how, seq, ts}` written by `update_state`. Say that both adoption markers are permanent and what the `!` line means.
  - §4: describe the three recovery traces (record field, state key, rebuild note) in one table.
  - §7: use the real day-rollup problem text. Align the warning list and the `--strict` list with W-02 / W-03 / W-04.
  - §12: add "No read-only open of the copied browser database".
- `SECURITY.md`: keep the "Auditing a clone" sentence only when W-02 is merged. Reword the archive guarantee per W-04 (name not addressed by hash → FAIL; record without a file → warning).
- `README.md`:
  - Exit table row 1: add "an unexpected internal error (`error: internal error …`)" and "`sync` found committed content that differs from disk".
  - Upgrading table: add `--replace-remote` to the `sync --r` row; add a `sync --re` row (shortest form `--rem`).
  - Shell-hook paragraph: state the rule after W-10 (skipped: a plain `gblog` command, variables allowed; logged: lists, pipes, redirections, command substitution).
  - Capture paragraph: describe the `(new baseline: …)` suffix and `--record-baseline` after W-07.
  - Add two sentences under "Recovery": adoption `!` lines are permanent, and report them once with their cause.

**W-23 Routine cost (T-08).**
- `docs/routine-daily-audit.md` step 1: use `gblog verify --bot <slug> --skip-archives --contains <head>`, and say that archives are re-hashed in step 2.
- Add to "Why the routine is shaped this way": `anchor` verifies every chain again, so step 2 is two full passes; this is expected.
- `skills/activity-log/SKILL.md`: no change to commands. Add after the hook install line: "Reinstall the hook after upgrading gblog (README, Upgrading, item 6)".

### Group G4 — `install.sh`, `bin/gblog`, `.github`

**W-24 Clear `CDPATH` before `cd` (T-19).**
- `bin/gblog`: use `CDPATH='' cd -P -- …` in both `cd` calls.
- `install.sh`: use `CDPATH='' cd -- …` for `HERE` and `PREFIX` (the ledger root already does this).
- Tests: `tests/test_scripts.py::test_launcher_ignores_cdpath` (from the repo root with `CDPATH=/usr` exported, `bash bin/gblog --version` prints the version line) and `test_install_ignores_cdpath` (relative `GBLOG_PREFIX=bin` with `CDPATH=/usr`; the launcher lands under the working directory).

**W-25 CI guards (T-03, T-18, T-20).**
- New step in the `test` job (3.12 entry): `grep -q "^## \[$(./bin/gblog --version | cut -d' ' -f2)\]" CHANGELOG.md`, with an error message naming both values.
- On the 3.12 entry run `python -m unittest discover -s tests --durations 15` (available from 3.12) so slow tests show up in the log.
- Smoke step additions, on a fresh root before the tamper block:
  - `printf 'evidence\n' | ./bin/gblog archive --bot ci --kind message --stdin --name smoke`, then `./bin/gblog verify --bot ci`;
  - `./bin/gblog sync --no-push`;
  - `./bin/gblog capture files --bot ci --path "$RUNNER_TEMP"` (must exit 0);
  - `./bin/gblog tail -n 1 --bot ci`.
- `scripts` job: add `( cd "$GITHUB_WORKSPACE" && CDPATH=/usr bin/gblog --version )`.
- The pending-list check is the unit test from W-17; no extra CI step is needed.

## 5. Suggested order

1. W-22 first, moving T-01 a–f to "Known open findings", so the published docs stop overclaiming while the fixes are written. W-17's `PENDING.txt` lands with it.
2. W-02, W-03, W-04 (verify), then W-05, W-06, W-08, W-09 (the remaining T-01 items), each with its marker removal.
3. W-10, W-07, W-11 (regressions and small fixes), with W-18 and W-19.
4. W-20 (benchmark and helpers), then W-12, W-13, W-14, measured.
5. W-15, then W-16, once the suite is green.
6. W-01 and the CHANGELOG date as the release step; W-23, W-24 and W-25 at any time.

## 6. Not recommended

- **Making a path-less `archive` record a FAIL.** `gblog log --type archive` writes one, there is no way to repair a chain afterwards, and the command is on the Auto-review allow-list.
- **Warning about non-normalised `ts` in default mode.** Records written with a free-form `--ts` before 0.6 would add `!` lines to a healthy ledger. Strict mode is the right place.
- **Renaming the lock to `state/anchors.lock` to match the docs.** A Bot named `anchors` would share the file, and `anchor` would wait on itself. Fix the docs.
- **A process-wide cache of `aliases.json` keyed by mtime.** Tests and operators rewrite the file within one timestamp tick. Cache per CLI invocation only (W-12).
- **A `GBLOG_NO_FSYNC` switch to speed up tests.** A durability switch in an audit tool will end up set in production. Patch `os.fsync` inside tests if needed.
- **A streaming rewrite of the chain walk.** The memory gain over W-14 is small next to the risk of changing the order or text of problems on a broken chain.
- **Opening the copied browser database with `mode=ro`.** The copy is private and deleted, and read-only mode on a WAL copy can fail to open. Drop the claim instead.
- **Dismissing `state_adopted` warnings automatically.** The marker exists because an append erases the "state missing" evidence. Document that it is permanent.