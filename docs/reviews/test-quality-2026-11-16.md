# Test-quality review — 2026-11-16

Scope: `tests/*.py` as supplied, read against `gblog.py` 0.6.1.

Method: static trace only. Nothing was executed and no mutation tool was run. Every "uncaught" claim comes from following the mutated code path through each test that touches it. New tests marked **fails today** document a defect in the current code, not just a missing assertion.

## 1. Summary

- **One documented integrity guarantee has no test.** No test asserts `timestamp goes backwards`; the fuzz cases that produce one still FAIL through another check (M-01).
- **Backup and remote-policy integration is untested.** `_sync_drift`, `_ssh_alias_target` and the `gh` branch of `remote_visibility` are tested as functions. Their use inside `sync` and the API branch of the lookup are not (M-02 to M-05, M-12).
- **Six documented secret shapes have no test string.** `github_pat_`, `xox?-`, `sk-`, `xai-`, `AIza` and `gho_`/`ghu_`/`ghs_`/`ghr_` can be deleted from `_SECRET_PATTERNS` without a failure (M-07). Spool files, which `sync` pushes, are never checked for redaction (M-06).
- **About 150 `assertNotIn("Traceback", err)` assertions cannot fail.** `main()` turns every exception into `error: internal error …` with exit 1, and `_verify_guarded` turns it into a FAIL line. Only `tests/test_fuzz_verify.py` also rejects `internal error` and `verify aborted`.
- **Several fixes exist in the code with no test.** The CHANGELOG still lists most of them under "Known open findings": the strict `ts` form check, `--record-baseline`, the hook `$` rule, `valid_chains()` in `sync` and `alias list`, and the clamped-record de-duplication (M-24 to M-26, M-28, M-29).
- **Tracing turned up eight places where the docs promise behaviour the code does not have.** No test names any of them, so the suite is green (section 5).

## 2. The 30 most important uncaught mutations

Ordered by impact. "Closes" names the new test in section 4.

| # | Function | Mutation | Why no test catches it | Closes |
|---|---|---|---|---|
| M-01 | `Ledger.verify` | Delete the `if ts < last_ts:` block | No test asserts `timestamp goes backwards`. The fuzz "backdated" and "swap_lines" cases still FAIL through the state head or a broken link, and the fuzz oracle only asks for `not ok`. | N-01 |
| M-02 | `sync` | Delete the `if drift:` block (or set `drift = []`) | `_sync_drift` is called directly by one unit test. No test makes `sync` see a drift; the nested-`.gitignore` test is repaired by `git add -f` before the check runs. | N-02 |
| M-03 | `remote_visibility` | Invert `not body.get("private", False)`; map HTTP 404 to `None` | `urlopen` is only mocked with `[]`, `IncompleteRead` and `URLError`. No test feeds a real API body or an `HTTPError`. Only the `gh` branch has a value assertion. | N-03 |
| M-04 | `sync` | `if public and not (allow_public or allow_unknown)` | No test combines a public remote with `--allow-unknown-visibility`. | N-04 |
| M-05 | `sync` | `targets = {u: u for u in urls}` (alias not resolved) | `_ssh_alias_target` is tested as a function only. No `sync` test uses an ssh alias URL. | N-05 |
| M-06 | `Ledger._refuse_tail` | Copy record values into `safe_record` without `redact` | Every refused event in the tests has harmless text. No test reads a spool or alert file for a secret. | N-06 |
| M-07 | `_SECRET_PATTERNS` | Delete the `github_pat_`, `xox[abprs]-`, `sk-`, `xai-`, `AIza` rules; reduce `(?:ghp\|gho\|ghu\|ghs\|ghr)_` to `ghp_` | No test contains a string of these shapes. Stripe's `sk_live_` is a different rule. | N-07 |
| M-08 | `Ledger.update_state`, `Ledger.verify` | Do not write `state["state_adopted"]`; or drop the `seeded` warning | Tests of the `sync`/`capture` adoption path assert `seq`, `head`, `last_sync` and the stderr notice. None reads the key or the `re-created from the log tail` warning. | N-08 |
| M-09 | `Ledger.verify` | Drop `link_files` from `layout_warnings` | No test puts a symlink among a chain's files and then runs `verify`. `SymlinkTest` covers writes only. | N-09 |
| M-10 | `Ledger.verify` | Remove the `bare_archives` warning; or add it to `problems` in default mode | No test runs `log --type archive`, and no chain in the suite holds an `archive` record without `path` and `sha256`. Both directions pass. | N-10 |
| M-11 | `_main` (verify branch) | `lost = []` instead of `ledger.missing_witnesses(exact)` | Every "logs deleted" test uses a name that resolves to itself. No test has an alias that resolves the named chain elsewhere. | N-11 |
| M-12 | `_git` | Drop `-c core.hooksPath=<devnull>` (and `--no-verify`) | No test installs a git hook. | N-12 |
| M-13 | `main` | Catch-all `return 0`; or ignore `GBLOG_DEBUG` | No test raises an unexpected exception outside `verify`. No test sets `GBLOG_DEBUG`. | N-13 |
| M-14 | `Ledger._append_line`, `_atomic_write` | Delete `os.fsync(fd)`, `os.fsync(fh.fileno())` or the `_fsync_dir` calls | Nothing counts or orders fsync calls. The line-before-state order is pinned only through a mocked `write_state`. | N-14 |
| M-15 | `Ledger.append` | Stop skipping caller-supplied `v`, `seq`, `bot`, `prev`, `hash` | No test passes these keys. A planted spool file can carry them into `spool replay`. | N-15 |
| M-16 | `archive_file` | On an append failure always remove the stored file (ignore the `chained` check) | The only failure test mocks `Ledger.append`, so the log line never becomes durable. | N-16 |
| M-17 | `Ledger.spool_replay` | Guard checks only `not os.path.exists(path)`; or `_spool_scan_from` returns `max(days)` | The concurrency test moves the file before the guard runs. No test leaves a spool file behind after its record was appended. | N-17 |
| M-18 | `analyze` (approval rule) | `elif len(ok) >= 3:` (drop `and not den`) | The only approval evidence in the suite is three denials. An *Allow automatically* proposal is never generated or excluded by a test. | N-18 |
| M-19 | `analyze` (effects) | `improved = rate_after > rate_before`; or never add the `revert` proposal | The one effect assertion is `"too early" in out`, which is printed for either verdict. No applied change in the suite is 7 days old. | N-19 |
| M-20 | `coverage` | `window = window_minutes * 3600`; drop the `used` check; use `ts` instead of `observed` | The one test has no same-key pair outside the window, no key with two observations, and no `observed` that differs from `ts`. `--window` is never passed. | N-20 |
| M-21 | `capture_browser` | Delete `if epoch > horizon: continue` | No history fixture has a future-dated or out-of-range visit. One such row would then move the marker past every real visit. | N-21 |
| M-22 | `Ledger.records`, `chain_records` | Ignore `since` | `--since` and `--days` runs assert only exit 0. No test checks that an earlier record is excluded. | N-22 |
| M-23 | `Ledger.tail_records` | Break after the newest day file regardless of `have` | Every `tail` test uses a single day file or `-n 1`. | N-23 |
| M-24 | `Ledger.records(date=…)` | Delete the `continue` for a record clamped across midnight | The clamped-record test asserts only that the task appears in today's report. | N-24 |
| M-25 | `SHELL_HOOK` | Delete the `gblog\|gblog\ *\|__gblog*) return 0;;` arm; or go back to `*"\$"*` | The hook test checks only commands that must be logged. | N-25 |
| M-26 | `capture_files`, `_main` | Drop the `(new baseline: …)` suffix, the `, N pending` suffix or the `--record-baseline` note | Tests use `startswith("files: 0 change(s)")`. One accepts an empty suffix. The flag is never passed. | N-26 |
| M-27 | `Ledger._add_alias`, `render_report` | Accept control characters in display names; or drop `_md` around the label | The one test ignores the `alias add` result, so either single mutation passes. | N-27 |
| M-28 | `_layout_findings` | Delete the strict `ts`-form check; or run it in default mode | No test has a hash-valid record with a non-normalised `ts` in an otherwise healthy chain. | N-28 |
| M-29 | `sync`, `_main` (alias list) | `ledger.bots()` instead of `valid_chains()` | No `sync` or `alias list` test has an invalid directory under `logs/`. | N-29 |
| M-30 | `_atomic_write`, `_tmp_name` | Do not remove the temp file on failure; or return `path + ".tmp"` | No test fails a write inside `_atomic_write`. The symlink test plants a name the code never opens. | N-30 |

Also uncaught, lower priority:

- `_safe_url` reduced to `redact()`: a token-only `https://TOKEN@host/` URL is printed and logged.
- `_term` removed from `spool list`, `alias list` or the `anchor` missing-chain lines.
- `capture files --exclude` ignored.
- The 60-day drop in `save_proposals`.
- `spool replay` returning 0 after a skipped file.
- The `tail["seq"] == seq + 1` half of the roll-forward rule.
- The upper side of both 300 s bounds.
- The `rollup["missing"]` exit code of `anchor`.

## 3. Weak tests

| Test | Problem | Change |
|---|---|---|
| `test_security_regressions.py::SymlinkTest::test_state_tmp_symlink_is_not_followed` | Plants `state/ops.json.tmp`. The code opens `ops.json.<pid>.tmp`, so the link is never touched. Passes with `O_NOFOLLOW` removed. | Plant the link at `gblog._tmp_name(state_path)`; see N-30. |
| `test_security_regressions.py::MiscHardeningTest::test_install_restricts_redirect_protocols` | Searches `install.sh` for `--proto-redir`. The string is also in a comment, so the test passes with the flag deleted from the `curl` call. | Replace with a stub-`curl` test in `test_scripts.py` that records the arguments, as the CI step does. |
| Every `assertNotIn("Traceback", err)` on output of `_support.run` | `main()` catches every exception, so the string cannot appear unless `GBLOG_DEBUG` is set. | Let `run()` also fail on `internal error` in stderr and `verify aborted` in stdout unless the caller opts out. |
| `test_security_regressions.py::ReaderRobustnessTest::test_deeply_nested_anchor_file_does_not_crash_verify` | Asserts no traceback and `OK zzz`. If `read_anchor` stopped catching `RecursionError`, `_verify_guarded` would print `FAIL ops`, and the test would still pass. | Assert exit 0, `OK ops`, and `! anchors/ops.json is unreadable: anchored heads cannot be checked`. |
| `…::ReaderRobustnessTest::_assert_one_fail_does_not_hide_the_rest` | `FAIL aaa` is also what a crash inside `verify` prints. | Assert `verify aborted` is not in the output, and the expected problem text (`not valid JSON`, `unreadable (`). |
| `…::OutputInjectionTest::test_alias_display_name_cannot_forge_a_heading` | Ignores the `alias add` exit code. Passes if the refusal is removed (the report neutralises) and if the neutralisation is removed (the refusal holds). | Split into two tests; see N-27. |
| `…::ReaderRobustnessTest::test_alias_slug_keys_cannot_leave_the_logs_directory` | Two defences (`_read_aliases` key check, `_chain_key` fallback in `chains_for`). Either can be removed alone. | Also assert the `state/aliases.json is unreadable` warning, and test `chains_for("../../x")` directly. |
| `…::MiscHardeningTest::test_duplicate_keys_warn_in_default_mode` | `assertTrue(res["warnings"])` accepts any warning. | Assert `logs/ops/2024-06-01.jsonl:1: duplicate keys in line (only the last value of a repeated key counts)`. |
| `…::CaptureTest::test_missing_snapshot_rebaseline_is_announced` | Accepts an empty suffix or `(new baseline: snapshot missing)`. | Require the suffix; see N-26. |
| `…::CaptureTest::test_shell_hook_logs_commands_that_mention_gblog` | Only the "must be logged" side. | Add the skipped side; see N-25. |
| `…::CaptureTest::test_browser_capture_survives_a_non_text_title` | The traceback check is vacuous. Exit codes and the second visit are not asserted. | Assert both runs exit 0 and that `https://b.example/second` was imported once with a string `detail`. |
| `…::RemoteGuardTest::test_secrets_do_not_reach_git_unnoticed` | The exit code is not asserted. The password in the `--remote` URL is not searched for in stderr or the ledger. | Assert exit 0, and that `tok3nValue9` is in neither the output nor `logs/`. |
| `…::MiscHardeningTest::test_spool_replay_never_appends_a_break_ack` | Does not assert that the file stays in place, that `rejected` lists it, or the CLI exit code. | Assert all three (exit 2 through the CLI). |
| `…::RedactionTest::test_archive_with_one_invalid_byte_is_still_scanned` | Does not check that the invalid bytes are stored unchanged. | Assert `b"\xff\xfe trailing bytes"` is in the stored content. |
| `test_capture_sync.py::RemoteRegexTest::test_regex_rejects_lookalike_hosts`, `test_gblog.py::…::test_remote_visibility_parses_urls` | Exercise `_GITHUB_REMOTE_RE`, which no decision uses. | Assert on `gblog.github_remote(url)`: `(True, "deximple", "grokledger")` or `(False, None, None)`. |
| `test_capture_sync.py::SyncTest::test_unknown_visibility_github_refused` (override half) | `assertNotIn(rc, (2, 3, 4))`: the push cannot succeed, so the test never sees an override push anything. | Patch `gblog.github_remote` to classify a local bare path as GitHub. Assert exit 3 without the flag and one commit in the bare repository with it. |
| `test_capture_sync.py::…::test_sync_drift_detects_rewritten_file` | Calls `_sync_drift` without `cache_path`. The cache branch is never executed by a test that could fail. | Add the cache cases in N-02. |
| `test_gblog.py::…::test_coverage_matches_within_window` | The name promises a window. No pair is outside it. | See N-20. |
| `test_gblog.py::…::test_tune_proposals_apply_and_effect` | `"too early"` holds for any verdict. `assertNotEqual(rc, 0)` for an unknown id. | Assert exit 2 and the `unknown proposal id` text; verdicts in N-19. |
| `test_append_safety.py::HygieneTest::test_new_files_are_private` | Does not cover `archive/`, `reports/`, `tuning/`, `state/aliases.json` or the capture snapshot. | Extend the `private` list after one `archive`, `report`, `tune`, `alias add` and `capture files` run. |
| `test_append_safety.py::LockingTest::*`, `test_security_regressions.py::…::test_concurrent_alias_add_keeps_every_entry` | Probabilistic: they depend on a race actually happening. | Keep them. Add one deterministic hook test per lock, in the style of the `_tail_ok` hook used for `seal`. |
| `test_gblog.py::CliTest::test_full_flow`, `test_security_regressions.py::…::test_clamped_records_still_appear_in_todays_report`, `…::test_many_pem_begin_markers_do_not_stall_redaction` | The first two can straddle UTC midnight. The third is a 5 s wall-clock limit that includes interpreter start. | Freeze `gblog.now_iso` as `StatusCountsTest` does. Count `_PEM_END_RE.search` calls instead of timing. |

## 4. New test cases

`write_raw_chain` (now in `test_security_regressions.py`) should move to `tests/_support.py`; several cases below need it.

**N-01** `tests/test_integrity.py::ChainDirectoryTest::test_timestamp_going_backwards_fails`
1. `write_raw_chain(root, "ops", [{"ts": "2024-06-01T10:00:10Z"}, {"ts": "2024-06-01T10:00:05Z"}, {"ts": "2024-06-01T10:00:20Z"}])`.
2. Assert `ledger.verify("ops")["problems"] == ["logs/ops/2024-06-01.jsonl:2: timestamp goes backwards"]`.
3. Assert `verify --bot ops` exits 1 and prints that text as a `      - ` line.
4. Control: the same chain with three equal timestamps verifies OK.

**N-02** `tests/test_capture_sync.py::RemoteChangeAndDriftTest`
- `test_sync_with_drift_exits_1_and_does_not_push`:
  1. Sync once to a bare origin (exit 0, one commit there). Append a note.
  2. Patch `gblog._sync_drift` to return `["logs/ops/2026-01-01.jsonl"]` and run `sync`.
  3. Assert exit 1, `(not pushed)` and `committed content differs from disk for logs/ops/2026-01-01.jsonl` in stdout.
  4. Assert the bare repository still has one commit and `self.vis_calls` did not grow.
  5. Assert the last `system` record has `outcome == "fail"`.
  6. Repeat with `--no-push`: exit 1.
- `test_sync_detects_a_day_file_git_was_told_not_to_look_at`:
  1. After the first sync run `git update-index --assume-unchanged logs/ops/2026-01-01.jsonl`.
  2. Append a record with `ts` `2026-01-01T10:00:05Z` and run `sync`.
  3. Assert exit 1 and that the bare repository is unchanged.
- `test_sync_cache_does_not_hide_a_changed_file`:
  1. Call `_sync_drift(root, checked, cache)` once (returns `[]`, cache file written).
  2. `_rewrite` the day file with same-length bytes. The second call returns `[day]`.
  3. Restore the bytes with a new mtime: `[]`.
  4. Wrap `gblog._git_blob_id`: a third call with nothing changed hashes no file.

**N-03** `tests/test_capture_sync.py::VisibilityLookupTest::test_api_answers_map_to_public_private_unknown`

Patch `gblog.subprocess.run` to raise `OSError` and `urllib.request.urlopen` per case. Call `remote_visibility("https://github.com/someone/repo")`.

| `urlopen` answer | Expected result |
|---|---|
| body `{"private": false}` | `{"public": True, "reason": "api.github.com"}` |
| body `{"private": true}` | `public` is `False` |
| `HTTPError(url, 404, …)` | `public` is `False`, reason `api.github.com 404 (private or nonexistent)` |
| `HTTPError(url, 403, …)` | `public` is `None`, reason `api.github.com HTTP 403` |

- Assert the request URL is `https://api.github.com/repos/someone/repo`.
- Second test `test_gh_answer_false_means_public`: `gh` returns `false\n` with exit 0. Assert `public` is `True`, reason `gh api`, `urlopen` not called.

**N-04** `tests/test_capture_sync.py::SyncTest`
- `test_allow_unknown_visibility_does_not_override_public`:
  1. `vis_answer` public; run `sync --remote https://github.com/someone/public-repo.git --allow-unknown-visibility`.
  2. Assert exit 3, `PUBLIC` in the output, last `system` record `denied`.
- `test_every_push_url_is_checked`:
  1. Origin is a bare path. `git remote set-url --push origin <bare>`, then `git remote set-url --add --push origin https://github.com/someone/public-repo.git`.
  2. `vis_answer` is public only for the GitHub URL.
  3. Assert exit 3 and zero commits in the bare repository.

**N-05** `tests/test_capture_sync.py::SyncTest::test_sync_checks_an_ssh_alias_as_the_github_repository`
1. `sync --no-push --remote git@gh-backup:o/r.git` exits 0.
2. Patch `gblog._ssh_alias_target` to map that URL to `https://github.com/o/r.git`. `vis_answer` returns `{"public": None, "reason": "api.github.com HTTP 403"}`.
3. Run `sync`. Assert exit 3 and `self.vis_calls == ["https://github.com/o/r.git"]`.
4. Assert the output contains `refused: could not confirm git@gh-backup:o/r.git is private`.

**N-06** `tests/test_append_safety.py::RecoveryTest::test_spooled_event_is_redacted`
1. Seed two records and call `break_state()`.
2. Run `log --bot ops --action shell --target "deploy --token=<T>" --detail "DB_PASSWORD=<P>"` (secrets assembled from pieces). Assert exit 4.
3. Read the spool file and `state/ops.alert.json` as bytes: neither secret is present, `[REDACTED]` is.
4. Restore state and run `spool replay`. Assert the file under `replayed/` and the log contain neither secret.

**N-07** `tests/test_redaction_extended.py::ExtendedRedactionTest::test_every_documented_vendor_shape`
- One case each, built from pieces:
  - `github_pat_` plus 22 characters;
  - `gho_`, `ghu_`, `ghs_`, `ghr_` plus 24;
  - `xoxa-`, `xoxb-`, `xoxp-`, `xoxr-`, `xoxs-` plus 12;
  - `sk-` plus 20;
  - `xai-` plus 20;
  - `AIza` plus 35.
- For each, use `assertRedacted("use <token> now", <token tail>, ("use", "now"))` and assert idempotence.
- `test_every_documented_query_parameter`: for `token`, `access_token`, `refresh_token`, `api_key`, `apikey`, `key`, `secret`, `password`, `pwd`, `sig`, `signature`, `auth`, `code`, assert `redact("https://h.example/p?%s=VALUE123&x=1")` equals the same URL with `[REDACTED]` as the value and `&x=1` kept.

**N-08** `tests/test_append_safety.py::UnreadableStateTest::test_state_recreated_without_an_append_leaves_a_marker`
1. Seed three records, drop the last line, remove the state file.
2. `update_state("ops", {"last_sync": {...}})` returns `True`.
3. Assert `raw_state()["state_adopted"]` has `how == "missing"`, `seq == 2` and a normalised `ts`.
4. Assert `verify("ops")` is OK and its warnings contain exactly `state/ops.json was re-created from the log tail at seq 2 (it was missing): truncation before that record cannot be ruled out from state`, and not the "missing or unreadable" line.
5. Append one note. The same warning is still in `verify`.
6. Repeat with an unparsable state file: `how == "unreadable"`.

Step 5 pins what the code does (the key is kept); `docs/DESIGN.md` section 4 says both that and the opposite (D-5).

**N-09** `tests/test_integrity.py::AnchorAndStrictTest`
- `test_symlinked_day_file_warns_and_strict_fails`:
  1. Chain of two records. Move the day file to `<root>/moved.jsonl` and symlink it back (skip where unsupported).
  2. Assert `verify` is OK with 2 records and the warning `logs/ops/2026-03-01.jsonl: symbolic link (gblog does not write through one)`.
  3. Assert the CLI exits 0 with the `      ! ` line, and `--strict` exits 1 with the same text as a `      - ` line.
- `test_symlinked_state_anchor_and_archive_dir_warn`: the same for `state/ops.json`, `anchors/ops.json` (after `anchor`) and `archive/ops`; each gives `<rel>: symbolic link (…)`.
- `test_directory_named_jsonl_fails_in_every_mode`: `mkdir logs/ops/zz.jsonl`. Assert `problems` contains `logs/ops/zz.jsonl: not a regular file` with and without `strict`.

**N-10** `tests/test_cli_ux.py::ArchiveCliTest::test_log_type_archive_is_a_warning_not_a_fail`
1. `log --bot ops --type archive --target contract.pdf` exits 0.
2. `verify --bot ops` exits 0 and prints `      ! logs/ops/<day>.jsonl:1: archive record without path and sha256: no archived file backs it`.
3. `verify --bot ops --skip-archives` still prints the line.
4. `verify --bot ops --strict` exits 1 with the text as a `      - ` line.
5. **Fails today:** assert the OK line does not say `(1 archived file(s) intact)` (D-8).

**N-11** `tests/test_integrity.py::StateWitnessTest::test_verify_bot_fails_for_a_deleted_chain_that_an_alias_resolves_elsewhere`
1. Chains `ops` (3 records) and `ops-new` (2). Run `anchor()`. `add_alias("ops-new", "Ops", ["ops"])`.
2. Remove `logs/ops` and `state/ops.json`.
3. `verify --bot ops`: assert exit 1, an `OK    ops-new` line, exactly one `FAIL  ops … 0 records` block, and `but logs/ops is missing`.
4. With `--json`: the `ops` entry has `missing` true.

**N-12** `tests/test_capture_sync.py::SyncTest::test_sync_runs_no_git_hooks` (POSIX)
1. `sync --no-push` once.
2. Write executable `pre-commit`, `post-commit` and `pre-push` hooks under `.git/hooks`. Each appends to a marker file; `pre-commit` and `pre-push` exit 1.
3. Append a note and run `sync --remote <bare>`.
4. Assert exit 0, ` pushed` in stdout, the marker file does not exist, and the bare repository has the same commit count as the ledger repository.

**N-13** `tests/test_malformed_input.py::MalformedArgsAndStateTest`
- `test_internal_error_is_one_line_and_exit_1`: patch `Ledger.tail_records` with `side_effect=RuntimeError("boom\nsecond line")` and run `tail`. Assert exit 1, empty stdout, and stderr equal to `error: internal error (RuntimeError: boom second line); set GBLOG_DEBUG=1 for a traceback\n`.
- `test_debug_env_reraises`: with `GBLOG_DEBUG=1`, `run_raw([...], catch=False)` raises `RuntimeError`.
- `test_debug_env_reraises_from_verify_all`: patch `Ledger.verify` to raise; with `GBLOG_DEBUG=1`, `verify --all` raises. **Fails today** (D-6).

**N-14** `tests/test_append_safety.py::HygieneTest::test_append_fsync_order`
1. Seed one record.
2. Wrap `gblog.os.fsync` and `gblog.os.replace` so each call is recorded. For `fsync`, record `os.fstat(fd)`.
3. Append a second record to the same day file.
4. Assert the recorded order is: fsync of the day file (`samestat` with the log path), fsync of a regular file that is not the day file, `replace` onto `state/ops.json`, fsync of the `state/` directory.
5. Assert there was no fsync of `logs/ops/`.
6. Append a record with a `ts` on the next day. Assert one additional fsync of the `logs/ops/` directory.

**N-15** `tests/test_append_safety.py::HygieneTest::test_caller_supplied_chain_fields_are_ignored`
1. Seed one record.
2. Append `{"type": "note", "detail": "x", "v": 7, "seq": 99, "bot": "other", "prev": "ab"*32, "hash": "cd"*32}`.
3. Assert the result has `v == 1`, `seq == 2`, `bot == "ops"`, `prev` equal to the first hash, and `hash == compute_hash(rec)`.
4. Assert `verify` is OK with no warnings.
5. Plant a spool file whose `record` carries the same five keys. `spool_replay` appends a record with the correct chain fields; `verify` is OK.

**N-16** `tests/test_append_safety.py::RecoveryPolishTest::test_archive_file_survives_a_failed_state_write`
1. Seed one record.
2. Under `_flaky_write_state()` (move the helper to `SafetyBase`), `archive_file(…)` raises `OSError`.
3. Assert exactly one `.gz` exists under `archive/`, and the last log line is the `archive` record with that `path`.
4. The next append prints the `recovered state/ops.json` notice. `verify` is OK with `archives == 1`.

**N-17** `tests/test_append_safety.py::RecoveryTest::test_replay_after_a_crash_between_append_and_move_does_not_duplicate`
1. Refuse one event (`detail` "refused once") and restore state.
2. Simulate the crash: append the payload's `record` by hand with `spooled_at` and `spool_id`, and leave the spool file in place. Append two more notes.
3. `spool_replay("ops")`: assert `replayed == []`, no pending spool file, the file is under `replayed/`, the chain holds the detail exactly once, and the alert is resolved.

**N-18** `tests/test_tune_loop.py::TuneLoopTest::test_allow_rule_needs_three_approvals_and_no_denial`

Four Bots, same target `post weekly summary`, different approval records:

| Bot | Approvals | Expected |
|---|---|---|
| `a` | 3 allowed | one proposal with key `allow:post weekly summary`, title contains `approved 3 times without a single denial` |
| `b` | 3 allowed, 1 denied | no proposal in category `approval` |
| `c` | 2 allowed | none |
| `d` | 3 allowed, 2 denied | none |

**N-19** `tests/test_tune_loop.py::TuneLoopTest::test_effect_verdicts_and_revert`
1. For Bots `worse` and `better`, append a `tune` record at `ts(10)` with `outcome` `ok` and this `proposal` snapshot:
   - metric `{"kind": "fail", "bot": <bot>, "action": "connector", "key": "quickbooks.sync"}`;
   - `baseline` 14.0;
   - `better` `lower`;
   - a 28-day `window`.
2. Then append 8 failing `quickbooks.sync` actions for `worse` and 1 for `better`, on days 9 to 2.
3. Assert the Markdown effect row of `worse` contains `**not helping**`.
4. Assert a `revert` proposal with key `revert:<id>` exists for `worse`.
5. Assert the row of `better` contains `improving`, and it has no `revert` proposal.
6. A third change applied 2 days ago shows `too early (`.

**N-20** `tests/test_tune_loop.py::CoverageRulesTest` (pure function, hand-built record dicts)
- `test_window_is_minutes`: self `git status` at 10:00, auto `git push` at 10:20. `coverage(recs)["matched"] == 1`; `coverage(recs, window_minutes=10)["matched"] == 0`.
- `test_one_report_covers_one_observation`: add auto `git log` at 10:25. Assert `auto == 2`, `matched == 1`, one unmatched.
- `test_observed_time_is_used`: auto browser record with `ts` 18:00 and `observed` 10:02 matches a self report at 10:00. One with `ts` 10:01 and `observed` 03:00 does not.
- `test_matches_reference_implementation`: 300 auto and 600 self records from `random.Random(1)`. Compare `auto`, `matched` and the unmatched list with a brute-force nested loop kept in the test file.

**N-21** `tests/test_capture_sync.py::BrowserCaptureTest::test_future_and_out_of_range_visits_do_not_pin_the_marker`
1. History with visit A at `base - 600`, visit B at `base + 10 years`, and one raw `visit_time` of `9 * 10**18`.
2. Run 1: assert `added == 1`, and `capture_browser_last[path] == _webkit(base - 600)`.
3. Add visit C at `base - 60`. Run 2: assert `added == 1` and the last record's target is C.

**N-22** `tests/test_cli_ux.py::ReportDateTest::test_since_excludes_earlier_days` (uses `_seed()`)
- `report --since 2024-06-03 --stdout`: `| Records | 3 |`, no `Idle bot old task`, no `busy day before`.
- `export --since 2024-06-02`: 5 rows, every `ts >= "2024-06-02"`.
- `len(ledger.chain_records("busy", since="2024-06-03")) == 3`.
- In `StatusCountsTest`, add `test_open_task_older_than_30_days_is_not_counted`: a `task_start` at `ago(45)` gives `open_tasks == 0`.

**N-23** `tests/test_cli_ux.py::AgentErgonomicsTest::test_tail_matches_full_read_across_day_files`
1. `alpha`: 2 records on 06-01, 2 on 06-02, 1 on 06-03. `bravo`: 3 on 06-02.
2. For `n` in 1, 2, 4, 5, 100: the `(ts, bot, seq)` list of `tail_records(None, n)` equals that of `records()[-n:]`.
3. `tail --bot alpha -n 4` prints four lines with `#2` to `#5`.

**N-24** `tests/test_cli_ux.py::ReportDateTest::test_clamped_record_is_reported_on_its_wall_clock_day_only`
1. Append a note with `ts` `2024-06-02T00:00:10Z`.
2. With `gblog.now_iso` patched to `2024-06-01T23:59:50Z`, append a `task_start` "Clamped work".
3. Assert `report --date 2024-06-01 --stdout` contains `Clamped work`, and `report --date 2024-06-02 --stdout` does not.
4. Assert `len(records(date=…))` is 1 for each day.

**N-25** `tests/test_security_regressions.py::CaptureTest::test_shell_hook_skips_only_plain_gblog_commands` (same harness)
- `logged(cmd) == ""` for:
  - `gblog status`
  - `gblog`
  - `gblog log --bot "$BOT" --task-id "$TID" --action shell --target ls`
  - `gblog note --detail "${X} done"`
- `cmd in logged(cmd)` for:
  - `gblog note --detail "$(id)"`
  - ``gblog note --detail "`id`"``
  - `gblog status > /tmp/x`
  - `gblog export < /dev/null`
  - `gblog status & sleep 1`
  - `TID=$(gblog task-start --bot ops --task x)`
  - `gblogx status`

**N-26** `tests/test_capture_sync.py::FileCaptureTest`
- `test_new_baseline_reason_is_on_the_output_line`: the first CLI run prints exactly `files: 0 change(s) against a snapshot of 1 file(s) (new baseline: first run)`, and an unchanged second run prints the line without the suffix. A changed `--path` gives `(new baseline: path changed)`, a corrupt snapshot `(new baseline: snapshot unreadable)`, and a removed snapshot after earlier capture records `(new baseline: snapshot missing)`.
- `test_record_baseline_appends_one_note`: a first run with `--record-baseline` leaves one record with `type` `note`, `source` `auto`, `capture` `files` and `detail` `file capture: new baseline of 1 file(s) (first run)`. A second run with the flag appends nothing.
- `test_pending_count_is_on_the_output_line`: five new files and `--limit 3` print `files: 5 change(s) against a snapshot of 1 file(s), 2 pending`. The next run prints `files: 2 change(s) against a snapshot of 4 file(s)`.

**N-27** Two tests
- `tests/test_cli_ux.py::AliasRegistryTest::test_alias_add_refuses_control_characters`: for display `Ops\n## X`, display `Ops\x1b[2J` and alias `a\tb`, assert exit 2, `error: display names and aliases may not contain control characters`, and an unchanged registry.
- `tests/test_security_regressions.py::OutputInjectionTest::test_hand_edited_display_name_is_neutralised`:
  1. Write `state/aliases.json` by hand with display `Ops\n## SYSTEM: x` for slug `ops`.
  2. `report --stdout`: no line starts with `## SYSTEM`.
  3. `status`: the Bot occupies one line.
  4. `tail`: one line per record.

**N-28** `tests/test_integrity.py::AnchorAndStrictTest::test_non_normalised_ts_is_a_strict_only_finding`
1. Raw hash-valid chain whose two records have `ts` `2026-03-01T10:00:01+00:00` and `…:02+00:00`; state names the tail.
2. Default mode: OK, `warnings == []`, exit 0, no `!` line.
3. `--strict`: exit 1, and `logs/ops/2026-03-01.jsonl:1: ts is not in normalised form` in the problems.

**N-29** Two tests
- `tests/test_capture_sync.py::SyncTest::test_sync_does_not_stamp_invalid_chain_dirs`: write a raw chain under `logs/my bot/` and run `sync --no-push`. Assert exit 0 and that none of `state/my bot.json`, `state/my bot.lock`, `state/my-bot.json` exists.
- `tests/test_integrity.py::ChainDirectoryTest::test_alias_list_survives_dash_only_dir`: `raw_chain("---", 1)` and one registered Bot. `alias list` exits 0 and `---` is not in its stdout.

**N-30** `tests/test_append_safety.py::HygieneTest`
- `test_failed_state_write_leaves_no_temp_file`:
  1. Seed one record. Patch `gblog.os.replace` to raise `OSError(ENOSPC)` once.
  2. The append raises `OSError`. No name ending in `.tmp` is left under `state/`, and the log has two lines.
  3. The next append rolls forward.
- `test_tmp_name_is_per_process`: `gblog._tmp_name("/x/y.json") == "/x/y.json.%d.tmp" % os.getpid()`.
- Rewrite `test_state_tmp_symlink_is_not_followed`: plant the link at `gblog._tmp_name(state_path)`. Assert that `append` raises `OSError`, the victim still reads `precious\n`, and `state/ops.json` is not a link.

## 5. Defects and absent features found while tracing

None of these is caught, because no test names the behaviour. Each needs a code or documentation decision before its test can pass.

| # | Documented | Code today | Test to add |
|---|---|---|---|
| D-1 | `gblog anchor --skip-archives` (daily routine prompt step 2, DESIGN sections 8 and 13, CHANGELOG) | The `anchor` parser has only `--bot`; the command exits 2. The routine prompt as written fails at step 2. | `test_integrity.py::test_anchor_skip_archives`: with a damaged archive, `anchor` exits 1 and `anchor --skip-archives` exits 0 |
| D-2 | Re-running `capture shell-hook --install` replaces an older block and prints `updated hook in …`; the hook carries `# gblog hook v2` (README, CHANGELOG) | `install_shell_hook` returns `False` whenever a block exists. `SHELL_HOOK` has no `v2` line. | `test_gblog.py::test_install_replaces_outdated_hook` |
| D-3 | Counters such as `max_tokens=100000` are left as written (CHANGELOG) | The glued-name rule turns it into `max_tokens=[REDACTED]`. | `test_redaction_extended.py::test_token_counters_untouched` |
| D-4 | `tune` reads the `--days` window or 90 days, plus `tune` records of any age (CHANGELOG, DESIGN section 13, FAQ 35) | `_main` calls `ledger.records(bot)` with no `since`. | `test_tune_loop.py::test_effect_table_limited_to_90_days`, or correct the docs |
| D-5 | The next `append` moves a state-level `state_adopted` onto its record and drops the key (DESIGN section 4) | The key is kept; the record gets no marker. The same section also says "kept". | N-08 pins the code; fix the document |
| D-6 | `GBLOG_DEBUG=1` re-raises inside `verify --all` and `anchor` (DESIGN section 11) | `_verify_guarded` never reads the variable. | N-13, third test |
| D-7 | `--json` output is JSON | On an ASCII stdout `status --json` writes `B\xfcro`, which is not a JSON escape. `tune --json` has the same problem. | `test_malformed_input.py::test_json_outputs_are_valid_on_ascii_stdout`: `json.loads` of `status --json` and `tune --json --stdout` through `run_cli(…, "ascii")` |
| D-8 | A bare `archive` record "is a claim, not stored evidence" | It is counted in `(N archived file(s) intact)`. | N-10, step 5 |

Two further observations:

- No caller reaches `_ChainLines`, `Ledger._chain_lines`, `Ledger.chain_hashes`, `Ledger._assert_tail_matches` or `_BREAK_KEYS`, so no test can fail on them. `verify` uses `_chain_snapshot`, although its docstring says otherwise.
- The CHANGELOG "Known open findings" list is stale in the other direction for the version string, the `ts` form check, `--record-baseline`, the hook `$` rule, `sync` on invalid directories, `alias list`, and clamped records. All of these are in the code. N-24 to N-26, N-28 and N-29 are the tests that would have shown it.

## 6. Suggested order

1. N-01, N-02, N-03, N-04, N-05: integrity and remote policy, all pass today.
2. N-06, N-07: leak paths, all pass today.
3. The `_support.run` change from section 3, then N-13.
4. N-08 to N-12 and N-14 to N-17.
5. N-18 to N-30.
6. The section 5 tests, each together with its fix. List them in `tests/PENDING.txt` if they land first.