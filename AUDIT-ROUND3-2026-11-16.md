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
---

# Appendix — round-3 audit at effort=max (claude-fable-5-2, org gju21, 11:05 KST)


Scope: `gblog.py` (reports itself as 0.6.0), `bin/gblog`, `install.sh`, `.github/workflows/ci.yml`, all `*.md`, `tests/*.py`, as they are in the tree after the round-2 and security-review work.

Method: static read only. Nothing was executed. Every finding is derived from the code path quoted as evidence. Performance figures are complexity estimates from the code, not measurements. "270 tests pass" was taken as given; that total includes six tests marked `@unittest.expectedFailure`.

### 1. Executive summary

- **Six security-review fixes are documented as shipped but are not in the code.** `CHANGELOG.md`, `SECURITY.md` and `docs/DESIGN.md` describe them as done. Their regression tests in `tests/test_security_regressions.py` carry `@unittest.expectedFailure`, so the suite is green. The six are: archive address checks (B:L4), `sync -m` redaction and the credential warning (B:L9), non-text browser rows (A:L-2), concurrent `spool replay` (B:L11), the duplicate-key warning (A:L-5), and the torn-line rollback (A:L-3). The last is a test defect more than a code defect.
- **`verify` computes symlink findings and throws them away.** `odd_files, link_files = self._file_findings(bot)` is never read. `SECURITY.md` ("Auditing a clone of the backup") promises that `--strict` fails on a symlink. It does not, and no test covers it.
- **The new catch-alls made most "never a traceback" assertions unable to fail.** `main()` turns every `Exception` into `error: internal error …` with exit 1, and `_verify_guarded` turns every exception inside `verify` into a `FAIL` entry. A test that expects exit 1 plus `FAIL` now passes when `verify` crashes.
- **R-15 (dead code) was not done, although the CHANGELOG says it was.** `_exit2`, `_assert_tail_matches`, `_BREAK_KEYS` and `_Lock._held` are all still present. The fixes added more dead code: `_GuardSkip`, `_COORD_KEYS`, `chain_hashes`, `irregular_logs` beside `_file_findings`, and `_touch_private`.
- **`__version__` is still `0.6.0`.** The code implements what the docs call 0.6.1. `SECURITY.md` tells operators that 0.6.0 is unsafe, and `gblog --version` cannot tell them which one they run.
- **Large ledgers: the chain walk is linear, but several commands read far more than they need.**
  - `tail -n 1` loads the whole ledger.
  - `tune` loads the whole history and has a quadratic matcher and one quadratic-to-cubic expression.
  - Archived files are re-hashed three times per daily routine run.
  - `sync` re-hashes every evidence file and force-adds every file by name.
- **The hardened shell hook now logs the skill's own commands.** Any `gblog …` command that contains `$` (for example `--bot "$BOT"`) is recorded as an unreported shell action. That lowers shell coverage and produces false `logging` proposals.
- **Machine-readable output can be invalid JSON.** `status --json`, `verify --json` and `tune --json` write `ensure_ascii=False` text through a `backslashreplace` stream. On an ASCII stdout, `ü` becomes `\xfc`, which is not a JSON escape.
- **The rest of round 2 and the security review reads as implemented.** I found no change to record bytes or the hash rule. Section 2 lists what I checked.

### 2. Checked and found consistent with the docs

- State witness on an empty or missing chain; `orphan_witnesses` including day rollups; `missing_witnesses` for `verify --bot`.
- Reserved names in `append`; the invalid-directory FAIL; attribution by directory; `bot_field`.
- One verify snapshot under the shared lock for log lines, state and both anchor files; `--contains` from `own_hashes`.
- `--replace-remote`; the recorded origin change; host-based GitHub classification; `ssh -G` alias resolution; fail-closed for unmapped GitHub URLs; `--hostname github.com` on `gh`.
- `_sync_evidence` / `_sync_drift`; hooks disabled; `.gitignore` recreated.
- `state_adopted` on the adopting record; future-tail warnings in `append`, `verify` and `status`; `report --date` using `clock_regressed`.
- `O_NOFOLLOW` on day files, state, anchors, reports, snapshots, archives and lock files.
- Fixture pin by hash; the temp-copy condition kept; `seal` guard under the bot lock.
- `install.sh`: absolute root, legacy rc lines, `~/.bash_profile`, read-only HOME, `--proto-redir`.

### 3. Prioritized findings

| ID | Sev | Area | Finding | Work items |
|---|---|---|---|---|
| T-01 | High | code vs docs | Six documented fixes are absent; their tests are `expectedFailure` | W-02 … W-07, W-23 |
| T-02 | High | correctness | Symlink findings computed and dropped in `verify` | W-01 |
| T-03 | High | tests | Catch-alls make crash assertions vacuous | W-08, W-22 |
| T-04 | Medium | code health | R-15 not done; new dead code; DESIGN §5 contradicts the lock code | W-19, W-27 |
| T-05 | Medium | release | `__version__` 0.6.0 while behaviour is 0.6.1 | W-20 |
| T-06 | Medium | docs | Documented behaviour with no code and no test | W-06, W-18, W-27 |
| T-07 | Medium | regression | Shell hook logs every `gblog` command containing `$` | W-16 |
| T-08 | Medium | correctness | A browser row with an absurd visit time pins the capture marker in the future | W-04 |
| T-09 | Medium | correctness | `--json` output invalid on a non-UTF-8 stdout | W-09 |
| T-10 | Medium | performance | `tail` reads the entire ledger | W-10 |
| T-11 | Medium | performance | `tune` and `coverage`: whole history in memory; quadratic and worse loops | W-11 |
| T-12 | Medium | performance | Archives re-hashed 3× per daily run; gzip read unbounded | W-02, W-12, W-28 |
| T-13 | Medium | performance | `sync`: force-add of every file in batches; full re-hash each run | W-03 |
| T-14 | Low-Med | correctness | Residual `bots()` callers: `alias list` exits 2 with `logs/---`; `sync` writes state for invalid directories | W-14 |
| T-15 | Low-Med | design | `state_adopted` warnings never clear; up to 21 `!` lines per chain forever | W-15 |
| T-16 | Low-Med | regression | Glued-name redaction rule also redacts counters such as `max_tokens=100000` | W-17 |
| T-17 | Low | performance | Append path: directory listing plus a stat per day file; four fsyncs | W-13 |
| T-18 | Low | performance | `verify`, `status`, `report`: redundant scans, reads and retained text | W-13 |
| T-19 | Low | correctness | `status --json` has no orphan alerts; `anchor` prints witness text unescaped; `git` output matched in English | W-03, W-14 |
| T-20 | Low | code health | Three exit-2 mechanisms; mixed prefixes; internal error shares exit 1 with FAIL | W-19, W-27 |
| T-21 | Low | code health | `_main` ≈ 480 lines, `verify` ≈ 230, `analyze` ≈ 200, `sync` ≈ 130 | W-21 |
| T-22 | Low | tests | Stale `pending:` / `deviation:` comments; vacuous and weak tests; duplicated helpers | W-22, W-24 |
| T-23 | Low | tests | Slow and timing-sensitive tests | W-26 |
| T-24 | Low | CI | Nothing guards `expectedFailure`, version drift or JSON on ASCII stdout | W-23, W-30 |

#### T-01 — Documented fixes that are not in the code

| Review id | Docs say | Code | Test (marked `expectedFailure`) |
|---|---|---|---|
| B:L4 | `verify` reports an `archive` record without `path` or 64-hex `sha256`, and a file not named `<sha256>.gz`. `archive` re-hashes an existing file before deduplicating. | `verify_archives`: `if r.get("type") != "archive" or not r.get("path"): continue`. `archive_file`: `existed = os.path.exists(full)` with no content check. A path-less record is still counted in `(N archived file(s) intact)`. | `test_archive_records_are_checked_one_by_one` |
| B:L9 | The `sync -m` message is redacted. A `--remote` URL with credentials prints a warning. | `commit_msg = message or …` goes to `git commit -m` unredacted. No warning exists. | `test_secrets_do_not_reach_git_unnoticed` |
| A:L-2 | Non-text URL or title and out-of-range visit time no longer crash. Marker saved per history file. Copy opened read-only. | A `bytes` title reaches `append` and raises `RecordError` (exit 2). `save_markers()` runs once after all files, so earlier rows are re-imported on every run. `sqlite3.connect(copy)` is a normal open. | `test_browser_capture_survives_a_non_text_title` |
| B:L11 | Two `spool replay` runs cannot append an event twice. | `spool_replay` takes no lock across check, append and move. `append(_guard=…)` exists but `spool_replay` does not use it. | `test_concurrent_spool_replay_appends_each_event_once` |
| A:L-5 | A line with duplicate keys is a `!` warning in default mode. | `_layout_findings` looks at raw text only when `strict`. | `test_duplicate_keys_warn_in_default_mode` |
| A:L-3 | A write that fails part-way is rolled back. | `_append_line` rolls back only bytes counted in `written`. The test's mock writes half and raises inside one `os.write` call, so `written` stays 0. A real kernel returns a short count first, which the code handles. | `test_failed_append_leaves_no_torn_line` |

#### T-02 — Symlink findings dropped

- Evidence, `Ledger.verify`: `odd_files, link_files = self._file_findings(bot)` and `irregular = self.irregular_logs(bot)`. Only `irregular` is used afterwards.
- CHANGELOG ("New `!` warnings … a symlink among the chain's files"), DESIGN §7 and `SECURITY.md` all describe a warning that `--strict` turns into a problem.
- Result: `gblog --root <clone> verify --all --strict` passes on a tree whose `logs/<bot>` or `state/<bot>.json` is a symlink.
- `_file_findings` also recomputes the "not a regular file" problems that `irregular_logs` produces. That is two directory scans for one answer.

#### T-03 — Crash assertions that cannot fail

- `main()`: `except Exception … return 1` unless `GBLOG_DEBUG` is set. The test helpers' own `except Exception: traceback.print_exc()` branch is now unreachable.
- `_verify_guarded` converts any exception into `{"ok": False, "problems": ["verify aborted: …"]}`. It ignores `GBLOG_DEBUG`.
- Consequence: `assertNotIn("Traceback", err)` (well over 100 occurrences) is always true. `assert_fail_not_crash` in `tests/test_malformed_input.py` expects exit 1 and `FAIL`, which is exactly what a crash inside `verify` now prints.
- Direct library calls (`self.ledger.verify(bot)`) still detect a crash. CLI-only checks do not.

#### T-04 — Dead code and duplicated helpers

| Item | Evidence | Action |
|---|---|---|
| `_exit2` | Same body as `_die`; used in `_check_reserved`, `_add_alias`, `capture_files` | Keep `_die` |
| `_assert_tail_matches` | No caller | Delete |
| `_BREAK_KEYS`, `_COORD_KEYS` | Never referenced; `seal_break` has a local `coord_keys` | Delete `_BREAK_KEYS`; use `_COORD_KEYS` in `seal_break` |
| `_Lock._held` | No code path nests an exclusive lock; DESIGN §5 says it does not exist | Delete |
| `_GuardSkip` | Defined, never raised or caught | Delete |
| `chain_hashes` | No caller in `gblog.py` or in the tests shown | Delete, or mark as public API |
| `irregular_logs` | Duplicates half of `_file_findings` | Fold (W-01) |
| `_touch_private` | `_aliases_lock` calls it, then `_Lock` creates the file `0o600` anyway | Delete |
| `read_anchor` | Same body as `_read_json` | Call `_read_json` |
| `_atomic_json` | Builds its own `path.<pid>.tmp` | Use `_tmp_name` |
| `log` branch | `args.source if args.source != "self" else None` | Use `_source_field` |
| `capture files` | Path check in `_main` and in `capture_files` | Keep the function's |
| `chain_records` | Stale comment ("would fall back to every file"); local `_key` duplicates `_seq_int` | Fix |
| Misc | `# -- iteration` above `# -- aliases`; local `import tempfile` in `capture_browser`; `env_ok` | Tidy |
| `_GITHUB_REMOTE_RE` | Used only by tests; decisions use `github_remote()` | Keep; retarget the tests (W-24) |
| `_tail_head` | Tests only; docstring says "used if state is missing" | Fix the docstring |

DESIGN §5 also disagrees with the lock code:

- The file is `state/.anchors.lock`, not `state/anchors.lock`.
- It is held for the whole `anchor()` run, including every chain's shared lock. It is not a leaf.

#### T-07 — Shell hook logs the skill's own commands

- Evidence, `SHELL_HOOK`: the first `case` arm is `*";"*|*"&"*|*"|"*|*"\$"*|*'`'*|*"<"*|*">"*|*$'\n'*) ;;`. A command that matches it is logged.
- History keeps the typed text, so `gblog log --bot "$BOT" --task-id "$TID" …` contains `$` and is logged as a `source: auto` shell action.
- `coverage` finds no self-report for it. Shell coverage drops, and `tune` proposes a `logging` fix.
- `TID=$(gblog task-start …)` is logged too. `_shell_name` drops the first token as an env assignment and returns `task-start`.
- A plain `$VAR` expansion cannot carry a second command. `$(`, backticks, `;`, `&`, `|`, `<`, `>` and newlines can.

#### T-08 — Browser marker pinned by one bad row

- Evidence, `capture_browser`: `newest = max(newest, visit_time)` runs for every row before any validation. Later runs then query `WHERE visit_time > ?` with that value.
- One row with `visit_time = 9e18` (corrupt, or planted) moves the marker past every real visit. Capture then reports `0 new visit(s)` forever.
- The same value makes `fromtimestamp` raise. `OverflowError` or `ValueError` becomes `error: internal error` (exit 1); `OSError` becomes exit 2.

#### T-09 — Invalid JSON on a non-UTF-8 stdout

- `status --json`, `verify --json` and `tune --json` call `json.dumps(…, ensure_ascii=False)`.
- `_tolerant_streams()` sets `errors="backslashreplace"`. On an ASCII stream, `Büro` is written as `B\xfcro`, and an astral character as `\U0001f600`. Neither is a JSON escape.
- `export` already handles this with an `ensure_ascii=True` fallback.
- The restricted-env CI job runs exactly this configuration but checks only text commands and `export`.

#### Performance on a large ledger (T-10 … T-13, T-17, T-18)

N is the number of records in a chain, D its day files, C the number of chains, A the archived bytes.

| Command | Cost today | Cause | Target |
|---|---|---|---|
| `append` | O(D) stats + 4 fsyncs | `log_files()` lists the directory and stats every `*.jsonl`, once or twice; the directory is fsynced even when the day file already existed | newest file only; 3 fsyncs |
| `verify` (walk) | O(N) time; raw text of every line kept | `_chain_snapshot` keeps `text` for all lines, used only by `--strict`; three directory scans; `_witness_present` parses the whole `ledger` chain | text only when needed; one scan |
| `verify` (archives) | O(A) per call, 3× per daily run | routine step 1 (`--contains`), step 2 (`--all`), then `anchor`; `anchor` has no skip flag | once per run |
| `status` | O(30 days of records) | state read three times per chain; `today()` evaluated per record; `aliases()` re-reads and re-validates the registry on every name resolution | one read each |
| `report --date D` | O(day D) + all later files | the `clock_regressed` scan JSON-parses every line of every later day file | byte pre-filter |
| `tail -n k` | O(all records, all chains) | `ledger.records(…)[-args.n:]` | newest files first |
| `tune` | all history in memory; O(P·N) | `ledger.records(bot)` with no `since`; `compute_metric` filters the full list per proposal; effects for every change ever applied | window plus `tune` records |
| `tune` rule H | O(T²·S) | `r.get("task_id") in {k for k, t in tasks.items() if t in stale}` is rebuilt for every `task_start` | one set |
| `coverage` | O(auto × self) | inner loop calls `_ts_epoch` (`strptime`) and `norm_target` per pair | bucket by key |
| `sync` | O(files) git calls + O(bytes) hashing | `git add -f` for every evidence file in batches of 200; `_sync_drift` hashes every log, history and archive file | untracked only |
| `export` | all records in memory | `records(raw=True)` then sort | acceptable; note only |

At 50k records per chain and ten chains, `tail -n 1` and `tune` each parse about half a million records. On a small cloud computer that is seconds of CPU and a large resident set for a command the docs tell Bots to run after every exit 2.

#### T-06, T-20 — Documentation drift

| File | Statement | Code |
|---|---|---|
| CHANGELOG | "Dead code and duplicate helpers were removed (R-15)" | Not removed (T-04) |
| CHANGELOG, DESIGN §7 | `!` warning for a `ts` that is not in normalised form | No such check |
| CHANGELOG "Added" | `capture files --record-baseline` | Flag does not exist |
| CHANGELOG, README | A new file-capture baseline is said "on the output line" | Output line unchanged; stderr only, and nothing at all for a first baseline |
| CHANGELOG | Browser capture "saves its marker per history file and opens the copied database read-only" | Neither |
| CHANGELOG, DESIGN §3/§5 | `state/anchors.lock`, a leaf lock around the rollup merge | `state/.anchors.lock`, held for the whole run |
| DESIGN §5 rule 3 | `_Lock` "keeps no re-entrancy state" | `_held` exists |
| DESIGN §7 table | Day-rollup witness prints `chain was anchored on <ts> but …` | Prints `chain was anchored (day rollup <date>) but …` |
| DESIGN §3, §6 | State holds head, `last_sync` and capture markers | `update_state` also stores a `state_adopted` object, and `verify` warns about it |
| DESIGN §4 | Text fields are the eleven listed in §6 | `_TEXT_FIELDS` also has `capture`, `profile`, `chain` |
| DESIGN §12 | "Per-append cost stays O(1)" | O(day files) |
| README, DESIGN §11 | Exit 1 is integrity FAIL or not pushed | Also every unexpected internal error |
| SECURITY, README | See T-01 and T-02 | |
| `tests/test_cli_ux.py` | "deviation: … Only `add_alias` does" | Fixed by R-02; comment is stale |
| `tests/test_security_regressions.py` | "`pending: <id>` … reproduces an attack that still works" | About 35 passing tests still carry a `# pending:` comment |

#### T-22, T-23 — Test weaknesses

| Test | Problem |
|---|---|
| `test_state_tmp_symlink_is_not_followed` | Plants `state/ops.json.tmp`. The code writes `ops.json.<pid>.tmp`, so the link is never touched and the test passes with or without `O_NOFOLLOW`. |
| `test_install_restricts_redirect_protocols` | Searches `install.sh` for the string `--proto-redir`, which also appears in comments. |
| `test_nan_in_a_log_line_is_flagged` | `assertTrue(res["warnings"] or not strict["ok"])` accepts either outcome and any text. |
| `test_sync_does_not_silently_retarget_origin` | Never asserts the exit code or the message of the refused run. |
| `test_deeply_nested_anchor_file_does_not_crash_verify` | Asserts no traceback (vacuous, T-03) and one `OK` line; not the exit code or the `ops` warning. |
| `test_non_string_text_fields_do_not_crash_readers` | `assertIn(rc, (0, 1))` for `verify`. |
| `RemoteRegexTest.test_regex_rejects_lookalike_hosts`, `test_remote_visibility_parses_urls` | Exercise `_GITHUB_REMOTE_RE`, which no decision uses any more. |
| `test_exit_code_table` | Calls `skipTest` at the end when git is missing, so the 0/1/2/4 assertions are reported as skipped. |
| `test_concurrent_anchor_runs_keep_both_rollup_entries` | Always waits the full `thread.join(5)`. |
| `test_many_pem_begin_markers_do_not_stall_redaction` | Wall-clock limit of 5 s including interpreter start; can flake on a loaded runner. |
| `LockingTest` (two tests) | Four spawned processes × 50 fsynced appends, and three writers plus an anchor loop; the slowest tests in the suite. |
| `test_full_flow`, `test_clamped_records_still_appear_in_todays_report` | Write "now", then read "today"; can straddle UTC midnight. |
| Helpers | Eight copies of `run()`, three of `stamp()`, two of `_AfterRelease`. |

### 4. Fix work items

Rules for every item:

- Standard library only, Python 3.8+.
- Record bytes and the hash rule do not change. A healthy ledger prints the same `gblog verify --all` bytes.
- Existing tests pass unmodified unless the item says otherwise.
- New output is additive: stderr lines, new `!` lines, new JSON keys.

#### Group G1 — `gblog.py`

**W-01 Report symlinks in `verify` (T-02).**
- In `Ledger.verify`, use both results of `_file_findings`:
  - `odd_files` replaces `irregular`: `problems.extend(odd_files)` in every mode. The text is the same (`<rel>: not a regular file`).
  - `link_files`: `warnings.extend(link_files)` by default, `problems.extend(link_files)` with `strict`. The text stays `<rel>: symbolic link (gblog does not write through one)`.
- Delete `Ledger.irregular_logs`.
- Compat: no change without symlinks. A setup that symlinks `archive/<bot>` gets one new `!` line (and a FAIL under `--strict`), as documented.
- Tests (`tests/test_integrity.py`, skipped where symlinks are unsupported):
  - `test_symlinked_day_file_warns_and_strict_fails`: exact warning text, exit 0; with `--strict`, exit 1 and the same text as a `-` line.
  - `test_symlinked_state_file_warns`.
  - `test_directory_named_jsonl_fails_in_every_mode`.

**W-02 Archive records are checked one by one; reads are bounded (T-01 B:L4, T-12).**
- `verify_archives`, for each record with `type == "archive"`:
  - `path` missing, empty or not a string: problem `archive record seq <n>: missing path`. Replaces the silent `continue`.
  - Existing path-validity check (`invalid archive path`): unchanged, and still first for a present path.
  - `sha256` not 64 lowercase hex: problem `<rel>: archive record has no valid sha256`.
  - Basename differs from `<sha256>.gz`: problem `<rel>: file name is not <sha256>.gz`.
  - When `bytes` is a non-negative int, stop reading after `bytes + 1` uncompressed bytes and report the existing `content hash mismatch` text. A gzip bomb can then no longer stall `verify --all`.
- `archive_file`: when the target file exists, gunzip and hash it, reading at most `len(data) + 1` bytes.
  - On a match, deduplicate as today.
  - On a mismatch or read error, rewrite it through the temp file and treat it as created by this call (`_dedup` false).
- Compat: every path gblog has ever written is `archive/<bot>/<date>/<sha256>.gz`.
- Tests: remove the marker from `test_archive_records_are_checked_one_by_one` and assert the three texts. Add `test_gzip_bomb_is_bounded` (`tests/test_integrity.py`): a member that expands to 50 MB behind a record with `bytes: 10` yields a mismatch, and a patched `GzipFile.read` shows that under 2 MB were read.

**W-03 `sync`: redaction, credential warning, locale, cost (T-01 B:L9, T-13, T-14, T-19).**
- `commit_msg = redact(message) if message else "gblog sync <ts>"`.
- When `--remote` parses as `http(s)` with both a user name and a password, print `warning: the remote URL contains credentials; git stores it in .git/config as written (use a credential helper)` on stderr. The URL is still set.
- `_git` and the `ls-tree` call in `_sync_drift` run with `LC_ALL=C` and `LANGUAGE=C` added to the inherited environment. `"nothing to commit"` is matched against git output.
- Stamp `last_sync` on `ledger.valid_chains()`, not `ledger.bots()`.
- Force-add only what needs it: after `git add -A`, read `git ls-files -z -- logs anchors archive state spool .gitignore` and pass to `git add -f` only the `forced` paths that are not in that list.
- `_sync_drift` is unchanged in this item.
- Tests:
  - Remove the marker from `test_secrets_do_not_reach_git_unnoticed`.
  - `test_sync_force_add_only_for_untracked` (`tests/test_capture_sync.py`): wrap `gblog._git`. On a second sync with no ignore rule, no `add -f` call is made. With a nested `.gitignore`, exactly the hidden file is passed.
  - `test_sync_does_not_write_state_for_invalid_dir`: `logs/my bot/` exists; after `sync --no-push` there is no `state/my bot.json`.
  - `test_sync_nothing_to_commit_under_foreign_locale`: with `LANG=de_DE.UTF-8` in the environment, the calls recorded by a `_git` wrapper carry `LC_ALL=C`.

**W-04 Browser capture survives odd rows and cannot be pinned (T-01 A:L-2, T-08).**
- Per row:
  - Skip a row whose URL is not `str`.
  - Use `""` for a title that is not `str`.
  - Compute `observed` inside `try/except (OverflowError, OSError, ValueError)`. On failure, skip the row.
  - Advance `newest` only for rows whose time converted and is not more than 86400 s after `time.time()`.
- Count skipped rows per history and print one stderr line: `notice: skipped <n> unusable row(s) in <path>`.
- Call `save_markers()` after each history file whose marker changed, not once at the end.
- Leave `sqlite3.connect(copy)` as it is; W-27 removes the read-only claim.
- Tests:
  - Remove the marker from `test_browser_capture_survives_a_non_text_title`.
  - `test_far_future_visit_does_not_pin_marker` (`tests/test_capture_sync.py`): one row dated year 9999, then a normal visit added later, which the next run captures.
  - `test_marker_saved_per_history`: the second of two histories raises in `append` (mock); the first history's marker is on disk.

**W-05 One `spool replay` per chain at a time (T-01 B:L11).**
- `spool_replay` holds `state/.replay-<bot>.lock` (exclusive, created `0o600`) for the whole call.
  - The leading dot cannot collide with a chain name, and the file matches the `state/*.lock` ignore rule.
  - Lock order: this lock first, then bot locks one at a time through `append`. Nothing takes it while holding a bot lock.
- Tolerate `FileNotFoundError` on the move to `replayed/`.
- Tests: remove the marker from `test_concurrent_spool_replay_appends_each_event_once`, and shorten its `thread.join(5)` to `join(1)`.

**W-06 Duplicate keys warn by default; timestamp form is a strict check (T-01 A:L-5, T-06).**
- `_chain_snapshot` parses with an `object_pairs_hook` that builds the same dict and records, in a per-line flag, whether any object had a repeated key. The tuple gains a fifth element `dup`. `_parse_line` is unchanged for all other callers.
- `_layout_findings`:
  - Not strict: `dup` adds the warning `<rel>:<n>: duplicate key in line`.
  - Strict: the existing `non-canonical line` problem covers it; add no second line.
  - Strict only: a `ts` for which `_is_norm_ts` is false adds the problem `<rel>:<n>: ts is not in normalised form`.
- No default-mode `ts` warning. Versions before 0.6 stored `--ts` as given, so a default warning could change the output of an existing ledger. W-27 corrects the docs.
- Tests: remove the marker from `test_duplicate_keys_warn_in_default_mode` and assert the exact text. Add `test_non_normalised_ts_strict_only` (`tests/test_integrity.py`): a raw hash-valid record with `ts` `2026-03-01 10:00:05` gives OK with no `!` line by default, and FAIL with the text under `--strict`.

**W-07 Roll back by file size (T-01 A:L-3).**
- `_append_line`, in the `except BaseException` handler: read `end = os.fstat(fd).st_size`. If `size < end <= size + len(data)`, call `os.ftruncate(fd, size)`. Drop the `written` condition.
- This relies on the caller holding the chain's lock, or `.anchors.lock` for history files. That is already true for every caller; say so in the docstring.
- Tests: remove the marker from `test_failed_append_leaves_no_torn_line`. Add `test_short_write_then_enospc_is_rolled_back` (`tests/test_append_safety.py`): the first `os.write` returns half the bytes, the second raises `ENOSPC`, and the file equals its earlier bytes.

**W-08 `GBLOG_DEBUG` reaches the per-chain guard (T-03).**
- `_verify_guarded`: when `os.environ.get("GBLOG_DEBUG")` is set, re-raise instead of building the FAIL entry.
- No change without the variable.
- Tests: W-22 uses it. Add `test_debug_env_reraises_from_verify_guard` (`tests/test_security_regressions.py`).

**W-09 One JSON writer (T-09).**
- Add `_write_json(out, obj, **dumps_kwargs)`:
  - Serialise with `ensure_ascii=False`.
  - Try to encode with `getattr(out, "encoding", None) or "utf-8"`.
  - On `UnicodeError` or `LookupError`, serialise again with `ensure_ascii=True`.
  - Write the text plus `"\n"`.
- Use it for `status --json`, `verify --json`, `tune --json` and `coverage --json`, with their current `indent` and `default` arguments. Give `export` the same helper per line.
- Compat: bytes are identical on a UTF-8 stream.
- Tests (`tests/test_malformed_input.py`, subprocess with `PYTHONIOENCODING=ascii`): `test_json_outputs_are_valid_on_ascii_stdout`. With a display name `Büro Chef` and a `detail` containing `☕` and an astral character, `json.loads` succeeds on the output of `status --json`, `verify --all --json`, `tune --json --stdout` and `coverage --json`.

**W-10 `tail` reads newest files first (T-10).**
- Add `Ledger.last_records(bot, n)`:
  - For each chain (`chains_for(bot)` or `valid_chains()`), walk `log_files` in reverse and collect readable records until that chain has at least `n`.
  - Apply the same attribution and `_text_fields` treatment as `records()`.
  - Merge, sort with the `records()` key, and return the last `n`.
  - Accumulate skipped-line counts per chain and emit one notice per chain.
- `tail` calls it.
- Compat: output is identical for any chain whose timestamps are monotonic in file order, which is every chain that verifies.
- Tests (`tests/test_cli_ux.py`):
  - `test_tail_reads_only_newest_files`: three chains with three day files each; wrap `Ledger.iter_records` and assert that `tail -n 1` opened one file per chain.
  - `test_tail_matches_full_read`: for n in 1, 5 and 1000, the output equals lines rendered from `records()[-n:]`.

**W-11 `tune` and `coverage` scale with the window (T-11).**
- Rule H: compute `stale_ids = {tid for tid, t in tasks.items() if …}` once, and compute the one-day cutoff string once.
- `coverage()`:
  - Precompute `(epoch, norm_target)` for every record once.
  - Index self-reports by `(bot, action, norm_target)`, keeping list order.
  - For each auto record, scan only its key's list for the first unused entry within the window.
  - The result must be identical to today's.
- `analyze`: bucket all records by `bot` once and pass the bot's list to `compute_metric`. Its signature is unchanged.
- CLI `tune`:
  - Load `records(bot, since=<start>)`, where `<start>` is the earlier of the window start and 90 days ago.
  - Add `tune` records from older day files, found with a byte pre-filter (`b'"tune"' in raw`) before JSON parsing.
  - Decisions (`applied`, `rejected`) therefore still come from any age.
- Behaviour change: "Effect of applied changes" and `revert` proposals cover changes applied in the last 90 days only. Add this to the CHANGELOG.
- Tests (`tests/test_tune_loop.py`):
  - `test_coverage_matches_reference`: seeded random records; compare with a brute-force copy of the old loop kept in the test.
  - `test_stale_task_rule_is_linear`: 400 open tasks; wrap `dict.__eq__` through a counting subclass, or assert the proposal's evidence list instead of timing.
  - `test_old_applied_decision_still_listed_as_applied`: a `tune` record 200 days old.
  - `test_effect_table_limited_to_90_days`.

**W-12 `anchor --skip-archives` (T-12).**
- Add the flag to the `anchor` subparser, with the same help text as on `verify`. `_main` already copies `skip_archives` into `ledger.verify_skip_archives`, which `_anchor` uses through `_verify_guarded`.
- Anchor files and output lines do not change.
- Tests (`tests/test_integrity.py`): `test_anchor_skip_archives`. With a damaged archive, `anchor` exits 1 and `anchor --skip-archives` exits 0. `verify_archives` is called with `check=False`, asserted by a wrapper.

**W-13 Constant-factor costs in `append`, `verify`, `status`, `report` (T-17, T-18).**
- `_tail_record`: use `os.scandir`. Sort the `*.jsonl` names in reverse and stat candidates only until the first regular, non-empty file. `log_files()` keeps its contract for every other caller.
- `_append_line`: fsync the directory only when the file was created by this call (`size == 0` before the write).
- `_chain_snapshot(bot, keep_text)`: keep the decoded text only when `strict` is true. Otherwise store `None`.
- `_witness_present`: skip lines without `b'"jay_approval_id"'` before parsing.
- `records(date=…)`: in the later-files scan, skip lines without `b'"clock_regressed"'` before parsing.
- `status`:
  - Take `updated` from the `_state_view` already read.
  - Call `alert_info` once per chain.
  - Compute `today()` once.
- `aliases()`: memoise the parsed registry on the `Ledger` instance when `ledger.cache_aliases` is true. `_main` sets it; the library default stays false. `_write_aliases` clears the memo.
- Compat: no output change.
- Tests:
  - `test_append_does_not_stat_old_day_files` (`tests/test_append_safety.py`): 40 day files; count `os.stat` and `DirEntry.stat` calls through a wrapper and assert a bound.
  - `test_append_fsyncs_directory_only_for_new_file`.
  - `test_status_reads_each_state_once`.
  - `test_alias_cache_sees_alias_remove` (the output of `alias remove` still shows the new resolution).

**W-14 Remaining `bots()` callers and small output gaps (T-14, T-19).**
- `alias list`: build `chains` from `valid_chains()`. `logs/---` no longer makes it exit 2.
- `status --json`: add a top-level key `orphan_alerts`, a list of `{"bot", "alert", "spooled"}` for chains known only from an alert file or spool files. Text output is unchanged.
- `anchor`: pass the `problems` lines of missing chains through `_term`.
- Tests (`tests/test_cli_ux.py`): `test_alias_list_with_dash_only_dir`, `test_status_json_lists_orphan_alerts`.

**W-15 `state_adopted` is recorded once, in the chain, and summarised (T-15).**
- `append`: when the state file holds a `state_adopted` object (written by `update_state`), put `state_adopted: <how>` on the record being appended and drop the key from the state it writes. The marker then lives in the hash chain, not in a file the adversary can edit.
- `verify`: print at most three record-level lines, then `<k> more record(s) appended while state/<bot>.json was missing or unreadable`. The existing texts are unchanged.
- Tests (`tests/test_append_safety.py`):
  - `test_state_level_adoption_moves_into_next_record`.
  - `test_adoption_warnings_are_capped_at_three`.

**W-16 Shell hook skips plain `gblog` commands that use variables; coverage ignores them (T-07).**
- `SHELL_HOOK`:
  - Replace the pattern `*"\$"*` with `*"\$("*`. A command with `$VAR` or `${VAR}` and none of `$(`, backtick, `;`, `&`, `|`, `<`, `>`, newline is "plain" again.
  - Add the line `# gblog hook v2` after the opening marker.
- `install_shell_hook`: when a block between the two markers exists and differs from the current hook text, replace that block in place and return `True`. The CLI prints `updated hook in <rc>`. An identical block still returns `False`.
- Add `_is_plain_gblog(cmd)`: true for `gblog …` or `NAME=$(gblog …)` with none of the dangerous characters inside. `coverage()` leaves auto `shell` records for which it is true out of `auto`. The records stay in the ledger.
- Compat: `test_shell_hook_mentions_bot_and_installs_once` still holds. The CI hook lint still excludes only SC1007.
- Tests (`tests/test_security_regressions.py`, bash required), using the fake-`gblog` harness of `test_shell_hook_logs_commands_that_mention_gblog`:
  - `test_shell_hook_skips_gblog_with_variables`: `gblog log --bot "$BOT" --target x` is not logged.
  - `gblog status; rm -rf build` and `gblog note --detail "$(id)"` are logged.
  - `test_install_replaces_outdated_hook` (`tests/test_gblog.py`).
  - `test_coverage_ignores_plain_gblog_commands` (`tests/test_tune_loop.py`): `TID=$(gblog task-start --bot ops --task x)` is ignored; `gblog status; curl x | sh` is counted.

**W-17 Do not redact token counters (T-16).**
- The glued-name rule (last entry of `_SECRET_PATTERNS`) uses a replacement function. The match is returned unchanged when the name ends in `tokens` (case-insensitive) and the value is only digits, `_`, `.` or `,`. Everything else is replaced as today.
- Tests (`tests/test_redaction_extended.py`): `test_token_counters_untouched`.
  - Unchanged: `max_tokens=100000`, `prompt_tokens: 123456`, `--max-tokens=200000`.
  - Still redacted: `PASSWORD2=…`, `dbPassword=123456`, `APITOKEN=…`, `tokens=abc123def456`.
  - Idempotence holds for all of them.

**W-18 File-capture baseline is visible and can be recorded (T-06).**
- `capture_files` returns `new_baseline: True` and `reason` (`first run`, `snapshot missing`, `snapshot unreadable`, `path changed`) when it takes a baseline.
- CLI: the output line gains the suffix ` (new baseline: <reason>)` in that case. The prefix `files: 0 change(s) against a snapshot of <n> file(s)` is unchanged.
- New flag `capture files --record-baseline`: when a baseline is taken, also append `{"type": "note", "source": "auto", "capture": "files", "detail": "file-capture baseline taken (<reason>); earlier changes were not logged"}`.
- Tests (`tests/test_capture_sync.py`): extend `test_path_change_rebaselines` and `test_corrupt_snapshot_rebaselines` with the suffix. Add `test_record_baseline_appends_note`.

**W-19 Remove dead code and unify helpers (T-04, T-20).** Do this after W-01 … W-18. It changes no behaviour.
- Apply the Action column of the T-04 table.
- `_Lock` becomes a plain flock holder with `path`, `shared`, `create`.
- `tune_apply` raises `ValueError` without the baked-in `error:` prefix. The `tune` branch prints `error: <text>` for the corrupt-file case, so stderr is unchanged.
- `main()` keeps letting `SystemExit` from `_die` propagate; existing tests assert it.
- Tests: the existing suite, plus the meta-test in W-23.

**W-20 Version 0.6.1 (T-05).**
- Set `__version__ = "0.6.1"`.
- Tests: `tests/test_scripts.py` reads the constant and needs no change. W-23 adds the changelog check.

**W-21 Split `_main` (T-21).** Optional; last.
- One function per command (`_cmd_verify(args, ledger, out)` and so on) and a dispatch dict. `_main` keeps parsing, validation and ledger setup.
- In `Ledger.verify`, extract `_collect_acks`, `_walk_chain` and `_state_findings` as private methods.
- No output or exit-code change. Do this only with the suite green and W-22 in place.

#### Group G2 — `tests/*.py` (do not touch `tests/fixtures/` data)

**W-22 Shared helpers; crashes fail tests again (T-03, T-22).**
- Add `tests/_util.py` with:
  - `run(argv, root, debug=True)`: sets `GBLOG_DEBUG=1` for the call when `debug` is true, folds `SystemExit` into the code, and renders any other exception as a traceback with exit 1.
  - `run_cli(argv, root, encoding="utf-8", extra_env=None)`: also sets `GBLOG_DEBUG=1` unless `extra_env` says otherwise.
  - `stamp`, `_AfterRelease`, `GIT_ENV`.
- Use them in `test_append_safety.py`, `test_integrity.py`, `test_malformed_input.py`, `test_cli_ux.py`, `test_capture_sync.py`, `test_tune_loop.py` and `test_security_regressions.py`.
- `tests/test_gblog.py` and `tests/test_chain_repair.py` keep their own `run()`, which lets `SystemExit` propagate on purpose.
- Tests that need the production behaviour pass `debug=False`:
  - `test_unexpected_error_in_one_chain_does_not_hide_the_others`;
  - `test_top_level_handlers`;
  - a new `test_internal_error_is_one_line` that asserts the `error: internal error (…)` line and exit 1.
- Requires W-08.

**W-23 Remove the markers; add guards (T-01, T-24).**
- Remove `@unittest.expectedFailure` from the six tests as W-02 … W-07 land.
- Delete every `# pending:` comment above a passing test, the stale `deviation:` comment in `tests/test_cli_ux.py`, and the matching sentences in both module docstrings.
- Add `tests/test_meta.py`:
  - `test_no_expected_failures`: no file under `tests/` contains the decorator. Build the search string from two parts so the test does not match itself.
  - `test_version_matches_changelog`: the first `## [x.y.z]` heading of `CHANGELOG.md` equals `gblog.__version__`.
  - `test_no_unreferenced_private_names`: with `ast`, collect module-level names and `Ledger` attributes that start with `_`. Each must be referenced elsewhere in `gblog.py` or in a file under `tests/`. Keep an explicit allow-list for `_tail_head` and `_GITHUB_REMOTE_RE`.

**W-24 Tests that can fail (T-22).**
- `test_state_tmp_symlink_is_not_followed`: plant the link at `gblog._tmp_name(state_path)`. Assert that `append` raises `OSError` and that the victim file is unchanged.
- `test_install_restricts_redirect_protocols`: delete it; CI's stub-curl step is the real check. Or move the stub-curl check into `tests/test_scripts.py` as `test_https_source_passes_proto_redir`.
- `test_nan_in_a_log_line_is_flagged`: assert the exact warning `…: non-finite number (NaN or Infinity) in record` by default, and the same text in `problems` with `strict=True`.
- `test_sync_does_not_silently_retarget_origin`: assert exit 2 and `pass --replace-remote` on the second run. Add a third run with `--replace-remote` that exits 0 and writes a `sync` record with target `git remote`.
- `test_deeply_nested_anchor_file_does_not_crash_verify`: assert exit 0 and the line `! anchors/ops.json is unreadable: anchored heads cannot be checked`.
- `test_non_string_text_fields_do_not_crash_readers`: assert `rc == 0` for `verify` and one `field '<name>' is not a string` warning per odd field.
- `RemoteRegexTest` and `test_remote_visibility_parses_urls`: assert on `gblog.github_remote(url)` returning `(True, "deximple", "grokledger")` or `(False, None, None)`. Keep one regex assertion for import compatibility.
- `test_exit_code_table`: move the exit-3 part into its own test with `skipUnless(GIT, …)`.

**W-25 Tests named in G1.** Add each test listed under W-01 … W-18 to the file named there, using fixed timestamps and `tests/_util.py`.

**W-26 Slow and timing-sensitive tests (T-23).**
- `test_concurrent_anchor_runs_keep_both_rollup_entries`: `thread.join(1)`.
- `test_many_pem_begin_markers_do_not_stall_redaction`: replace the wall-clock limit with a step count. Call `gblog._redact_pem` on 40,000 markers with `_PEM_END_RE` replaced by a wrapper that counts `search` calls, and assert at most 2. Keep one end-to-end call of `redact` without a time limit.
- `LockingTest`: 3 writers × 25 appends in the first test and 2 × 20 in the anchor variant. The assertions are unchanged apart from the expected counts.
- Midnight: freeze `gblog.now_iso` (as `StatusCountsTest` does) in `test_full_flow` and `test_clamped_records_still_appear_in_todays_report`.
- Optional: a `FastFsync` mixin in `tests/_util.py` that patches `gblog.os.fsync` to a no-op, for classes whose subject is not durability. Do not use it in `test_append_safety.py`.

#### Group G3 — `*.md`

**W-27 Make the docs match the code (T-04, T-06, T-20).** Apply after the G1 item each row depends on.

| File | Change |
|---|---|
| `CHANGELOG.md` 0.6.1 | Remove "opens the copied database read-only". |
| | Reword the `ts` warning as "`--strict` reports a `ts` that is not in normalised form" (W-06). |
| | Keep the R-15 line only once W-19 has landed. |
| | Name the lock `state/.anchors.lock`. |
| | Add: `anchor --skip-archives`; `status --json` key `orphan_alerts`; the 90-day limit of the effect table; hook v2 and in-place hook update; the baseline suffix; token counters. |
| `docs/DESIGN.md` §3, §5 | `state/.anchors.lock` and `state/.replay-<bot>.lock`. State that `.anchors.lock` is held for the whole `anchor` run and is taken before bot locks, never inside one. Remove the "leaf" wording for it. Keep rule 3 only after W-19. |
| §4, §6 | List `capture`, `profile` and `chain` with the text fields. Describe the state-level `state_adopted` object and its move into the next record (W-15). |
| §7 | Day-rollup problem text `chain was anchored (day rollup <date>) but logs/<bot> is missing`. `ts` form under `--strict` only. Duplicate-key warning text. Symlink texts (W-01). The three archive problem texts (W-02). |
| §11 | Exit 1 also covers an unexpected internal error; say that `GBLOG_DEBUG=1` re-raises, including inside `verify --all`. |
| §12 | Replace "Per-append cost stays O(1)" with "does not grow with the number of records; one directory listing per append". |
| `SECURITY.md` | No text change is needed once W-01 … W-03 land. Until then the affected sentences are wrong: symlink under `--strict`, archive address, `sync -m`, credential warning. |
| `README.md` | Capture section: the baseline suffix and `--record-baseline`. |
| | Upgrade step 6: "run `gblog capture shell-hook --bot <slug> --install` again; it replaces an older hook block". |
| | Exit table row 1: add "or an unexpected internal error (`error: internal error …`)". |

**W-28 Routine prompts use `--skip-archives` where a full check follows (T-12).**
- `docs/routine-daily-audit.md`:
  - Step 1: `gblog verify --bot <slug> --skip-archives --contains <head>`.
  - Step 2: `gblog verify --all --record` unchanged (the full check), then `gblog anchor --skip-archives`.
  - Add one line to "Why": archived files are re-hashed once per run, in step 2.
- `docs/routine-weekly-tune.md`: no change.
- Requires W-12.

**W-29 Cost table in `docs/DESIGN.md`.** Add a short section "Cost on a large ledger" with the complexity of `append`, `verify`, `status`, `report`, `tail`, `tune` and `sync` after W-10 … W-13. Later changes can then be checked against it.

#### Group G4 — `install.sh`, `bin/gblog`, `.github`

**W-30 `.github/workflows/ci.yml` (T-24).**
- Matrix: add `ubuntu-latest` / `"3.15"` with `allow-prereleases: true` on `setup-python` and the `-W error::DeprecationWarning` flag.
- Restricted-env job, in the ASCII CLI step:
  - `./bin/gblog status --json | python -c 'import json,sys; json.load(sys.stdin)'`;
  - the same for `verify --all --json`.
  - Requires W-09.
- Smoke step:
  - `./bin/gblog anchor --skip-archives` (W-12);
  - `./bin/gblog tail -n 1 | grep -c . ` must print `1`.
- `scripts` job: a step that fails when `grep -rn 'expectedFailure' tests/` finds a decorator. This is the same guard as `tests/test_meta.py`, visible in the job list.
- The 15-minute timeouts stay.

**W-31 `install.sh` and `bin/gblog`.**
- `install.sh`: install through a temporary name in `$PREFIX` followed by `mv -f`, so a concurrently started `gblog` never sees a half-written file. Print the `--version` output as today.
- `install.sh`: after installing, if any handled rc file contains `# >>> gblog shell hook >>>` without `# gblog hook v2`, print `note: <rc> has an older gblog shell hook; run: gblog capture shell-hook --bot <slug> --install`. Do not edit the hook from `install.sh`. Requires W-16.
- Tests (`tests/test_scripts.py`): `test_install_notes_outdated_hook`, and a rerun that stays byte-identical for the rc files.
- `bin/gblog`: no change.

### 5. Suggested order

1. W-08 and W-22. Tests must be able to see a crash before anything else changes.
2. W-01 … W-07 with W-23. This closes the gap between the security docs and the code.
3. W-09, W-14, W-16. These are the user-visible regressions.
4. W-10 … W-13 and W-28 (performance), each with its counting test.
5. W-15, W-17, W-18.
6. W-19 and W-20 once the suite is green, then W-24 … W-26.
7. W-27 and W-29 after the code they describe. W-30 and W-31 at any time after W-09, W-12 and W-16.
8. W-21 last, or not at all.

### 6. Not recommended

- **A default-mode warning for non-normalised `ts`.** Ledgers written before 0.6 may contain such values, and the warning would change the output of a healthy ledger. Keep it under `--strict` (W-06).
- **Caching the alias registry in the library by file mtime.** Tests and operators rewrite the file within one timestamp tick. Cache per CLI run only (W-13).
- **A `GBLOG_NO_FSYNC` switch in `gblog.py` to speed up tests.** It would be a durability bypass in production code. Patch `os.fsync` in tests instead (W-26).
- **Skipping the committed-content check for unchanged archive files.** It would cut `sync` I/O, but the check exists for the case where the index lies. Reduce the `git add -f` calls (W-03) and leave the hashing.
- **Escaping `[`, `]`, `!` and backticks in `_md`.** It would stop Markdown link and image injection from record text, but it changes how existing single-line values render (I-15 promised it would not). The "untrusted data" footer and the skill rule remain the control.
- **Turning the internal-error exit code into 2.** For `verify` and `anchor`, exit 1 is the fail-closed answer. Document it (W-27).
- **Converting `SystemExit` from `_die` into a return code in `main()`.** Existing tests assert the exception. Unchanged from round 2.