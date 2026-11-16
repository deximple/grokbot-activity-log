<!-- Round-4 audit: claude-fable-5-2 effort=max (12:55 KST); appendix at effort=high (12:51 KST). -->
# Round-4 audit — 2026-11-16

Scope: the current tree (`gblog.py` 0.6.1 "Unreleased", tests, docs, `install.sh`, `bin/gblog`, `examples/`, CI). This is an audit only: no file was changed and nothing was executed. Every statement was derived by reading the sources and names the function or document it rests on.

## 1. Executive summary

The hash-chain core is sound. I found no new way to make `verify` print `OK`/`PASS` for an edited, truncated or re-ordered chain while a witness survives. The append state machine, the `break_ack` pin, the lock order and `--contains` exactness are consistent with each other and with the tests.

The remaining risk has moved from the core to the edges that many parallel edits touched today. The six that matter most:

1. **The documentation describes a tree that no longer exists (Z-01, Z-04).** The K1/K2 specialist fixes are in `gblog.py`, but the CHANGELOG says that pass "changed documentation only". "Known open findings", the `SECURITY.md` "Known limits" table and DESIGN §2/§7/§13 still list about 20 fixed items as open. They also describe a sync cache that `sync()` no longer uses, and claim a `.git/remotes/` protection that the code does not have.
2. **The "CLI-only adversary" boundary has unscoped arguments (Z-02).** `--root` is accepted by every auto-allowed command. `gblog sync --root <any git repo>` creates ledger directories there, commits with hooks disabled and pushes that repository's current branch. `tune --apply/--reject` is on the README allow-list, and `log --type tune` can forge decisions.
3. **A pre-0.6 free-form `ts` tail can still brick a chain on the first append after upgrade (Z-03).** This is D1-09/K3-01, the only open item that can make a healthy ledger FAIL for good. A small fix is specified below.
4. **Rules contradict each other between functions (Z-05, Z-08, Z-09, Z-10).** A FIFO at `state/<bot>.json` is a warning, a directory there is a FAIL, and any non-regular `anchors/<bot>.json` is a FAIL. "origin" is defined two ways inside `sync()`. The per-chain anchor is protected as a witness but its day-rollup entry is not. `_open_regular` is used on half of the read paths.
5. **`sync` can exit 2 after a successful push (Z-06).** The `last_sync` stamp loop at the end is not isolated per chain.
6. **Code size.** `gblog.py` is about 3 900 lines. `_main` (about 450 lines), `Ledger.verify` (about 250) and `sync` (about 170) carry most of the complexity. At least eight blocks are duplicated, the sync cache is dead code in production, and two functions answer "is this task open?" differently.

The test suite also carries stale intent (Z-12): a pending test asserts a claim the docs withdrew, and a class of passing tests is still labelled "fails today".

Recommended order: G3-01 and G3-02 (documentation truth and allow-list scope), then G1-01, G1-02 and G1-05, then the consistency items, then the simplifications.

## 2. Prioritized findings

| Id | Pri | Kind | One line |
|---|---|---|---|
| Z-01 | P1 | false docs | K1/K2 fixes are in the code; CHANGELOG, SECURITY and DESIGN still say "documentation only" and "open" |
| Z-02 | P1 | defect (policy and code) | `--root`, `tune --apply/--reject`, `log --type tune`, `--ts`, `--history` are unscoped for the CLI-only adversary |
| Z-03 | P1 | defect | free-form pre-0.6 tail `ts`: first append can FAIL the chain for good or land in an earlier day file |
| Z-04 | P1 | false docs, dead code | sync cache documented as active; `sync()` never passes it; cost tables wrong |
| Z-05 | P1 | contradiction | non-regular side files: three different verdicts, none matching the docs |
| Z-06 | P1 | defect | `sync` stamp loop not isolated; exit 2 after push; origin-change "undo" not true on tail mismatch |
| Z-07 | P2 | regression, duplication | task-id diagnostics: two implementations, a whole-day-file scan per CLI append, order bug |
| Z-08 | P2 | defect | "origin" read two ways; `.git/remotes/origin` is pushed to and bypasses `--replace-remote` |
| Z-09 | P2 | contradiction | day-rollup witness can be erased by a later headless entry; anchor written before history |
| Z-10 | P2 | contradiction | K2-06 half-applied: ten read sites still `open()` after `isfile()` |
| Z-11 | P2 | defect (small) | D1-13 count, D2-10 hint, misleading drift message, `(witness files)` lost with `--bot` |
| Z-12 | P2 | tests | pending test contradicts docs; "fails today" class passes; cache test covers dead code |
| Z-13 | P2 | defect | shell-hook fallback fires on any exit 2, so an environment error logs the command twice |
| Z-14 | P2 | false docs | message and format mismatches; new stderr warnings undocumented |
| Z-15 | P3 | complexity | `_main`, `verify`, `sync`; eight duplicated blocks; dead helpers |
| Z-16 | P3 | tests | helpers copied across modules; no legacy-ledger fixture; no case-insensitive test; no docs lint |
| Z-17 | P3 | scripts, CI | example audit re-hashes archives twice; examples not linted in `scripts` job; installer gaps |
| Z-18 | P3 | docs structure | the 0.6.1 CHANGELOG section corrects itself in four layers |
| Z-19 | P3 | defect (edge) | 16 MiB side-file bound plus unbounded anchor content; replay race double-spools |

### Z-01 (P1) Docs say "open"; code says "fixed"

`gblog.py` contains the specialist fixes, each tagged in comments:

- `_open_regular`, `_last_line` (K2-06); `_open_private` without `O_TRUNC` (K2-05); `_open_lock`, `_Lock.avoid` (K2-02)
- `_disk_name` (K1-09); `_refuse_tail` (K2-10); the directory test in `_append_head` (K2-08)
- `missing_chains` and `_witness_listing` (K2-01); `lost_chains_of` (K1-04); `spool_chains_for` (K1-05)
- `_task_context`, `open_task_list` (K1-01); `_task_status`, `_warn_task` (K1-06); hook fallback (K1-07)
- `_norm_decision`, `_norm_action` (K1-11); `_warn_unknown_bot` (K1-03)
- umask in `_git` (K2-07); size `-1` in `_sync_evidence` (K2-04); atomic `_install_shell_hook` (K2-12); `_read_json` in `save_proposals` (K2-09)

Documents that contradict this:

- CHANGELOG "Specialist reviews": "This pass changed **documentation only**".
- CHANGELOG "Known open findings" lists every item above as not in the code.
- CHANGELOG "Future work" lists the hook fallback (K1-07), which is done.
- `SECURITY.md` "Known limits (open; fixes are future work)": 13 of 17 rows are fixed. They are both lock rows, both K2-01 rows, the hard-link row, the FIFO-at-report row, the swapped-FIFO row, the directory-at-state row, both K2-10 rows, the `.git/objects` row, the cache row and the hook rewrite row.
- `SECURITY.md` "Not guaranteed": says `.git/objects` gets the process umask.
- DESIGN §2: "Not covered yet: the witness scan…, the write of the day rollup, and a lock file that is not a regular file".
- DESIGN §13: the "L² / 64 KiB" tail lookup row.
- README "Upgrading from 0.5": the letter-case limit says records carry `bot: "ops"` inside `Ops`. `_disk_name` now writes `Ops`.

Impact: an auditor relying on `SECURITY.md` plans around attacks that no longer work and is not told which do. Fix: G3-01, which contains a claim-to-truth table.

### Z-02 (P1) Unscoped arguments defeat the CLI-only boundary

`SECURITY.md` says gblog "refuses the argument-only attacks it knows about" and that the README allow-list is the other half. Neither half covers these:

1. **`--root` / `GBLOG_ROOT`.** `_add_root_everywhere` puts `--root` on every subcommand. For `gblog sync --root R`, where `R` is any existing git repository with an `origin`, `sync()` does the following:
   - `ledger.ensure()` creates `logs/ state/ reports/ tuning/ archive/ anchors/ spool/`, and `README.md` and `.gitignore` when absent, inside `R`.
   - It runs `git add -A logs reports state tuning archive anchors spool [README.md] [.gitignore]`. This stages the project's own files of those names, deletions included.
   - It runs `git rm --cached state/*.lock state/capture-files-*.json`.
   - It commits as `gblog` with `--no-verify --no-gpg-sign`, so the project's pre-commit hook and signing policy are bypassed.
   - It pushes `HEAD:<current branch>` with `--no-verify`. That publishes every unpushed local commit of that branch. The push is refused only when the remote is a public or unconfirmed GitHub repository; any other host only warns.

   The chain `archive --root R --file <allowed transcript path>` followed by `sync --root R` copies ledger evidence into a repository whose remote the operator never approved for backups.
2. **`tune --apply` / `--reject`.** `tune` is on the "Allow automatically" list without qualification. A rejected proposal is hidden for 28 days and an applied one moves to "already applied". An injected Bot can hide `logging`, `revert` or `approval: deny` proposals unseen.
3. **`log --type tune`.** `analyze()` builds `decisions` from any record with `type == "tune"`, an outcome of `ok` or `skipped`, and a `target`. `gblog log --type tune --target <id> --outcome skipped` is such a record and needs no `proposals.json`.
4. **`--ts` on a new chain name.** The past-`ts` refusal compares with the tail. A fresh name has none, so `gblog note --bot new-name --ts 2020-01-01T00:00:00Z` writes a back-dated chain. This is only a warning unless `alias strict on` is set.
5. **`capture browser --history PATH` and `--hours 0`.** `--history` imports any SQLite file with the two tables as `source: auto` evidence. `--hours 0` on the first import pins the marker to "now", so earlier visits are never imported.

Fix: G3-02 (scope the allow-list, list the flags in `SECURITY.md`) and G1-01 (code guards).

### Z-03 (P1) Free-form tail `ts` still bricks a chain (D1-09, K3-01)

`append()` drops a tail timestamp that is not in normalised form (`if not _is_norm_ts(tail_ts): tail_ts = None`). Then neither the past-`--ts` refusal nor the clamp applies, and the day file is chosen from the wall clock alone. `verify` compares `ts` as text and walks day files in name order.

After a pre-0.6 record with `ts` `2026-11-17T08:00:00+09:00`, an append at 23:30Z on the 16th is stamped `2026-11-16T23:30:00Z` and written to `2026-11-16.jsonl`. The next append sees a tail in the newest day file that does not match state. The result is exit 4 forever, or `timestamp goes backwards` forever.

The README documents a manual wait of up to the UTC offset. A code fix is small and needs no format change: G1-02.

### Z-04 (P1) The sync cache is documented as active and is dead code

`sync()` calls `_sync_drift(root, checked)` without `cache_path`. The comment above it says why ("a cache file under .git/ is writable by whoever the check is meant to catch"). `tests/test_specialist.py::test_sync_keeps_no_verification_cache` asserts it. D3-05 is therefore decided.

Still present in the code: `_SYNC_CACHE` (unused), `_write_sync_cache`, and the `cache_path` branch of `_sync_drift`. Only `test_mutation_gaps.py::test_sync_cache_does_not_hide_a_changed_file` reaches them.

Documents that say the cache is in use:

- CHANGELOG "sync content check (D3-05, open)", the "Documentation corrected" bullet "`sync` still uses its cache", and "Known open findings".
- `SECURITY.md`: the notes under "Private-remote policy" and the cache row of "Known limits".
- DESIGN §13: the `sync` row and "Kept as they are for now".
- FAQ 35: "A file that is unchanged since the last run is not hashed again".

The real cost of `sync` is now git plus a hash of every evidence file on every run, O(total bytes of `logs/`, anchor histories and `archive/`). Fix: G1-03 and G3-01.

### Z-05 (P1) Non-regular side files: three verdicts, none as documented

| Entry | `verify` today | `append` today | Docs (README, SECURITY, DESIGN §2/§7) |
|---|---|---|---|
| FIFO or device (also behind a symlink) at `state/<bot>.json` | `!` "missing or unreadable" (exit 0) | adopts the tail; `os.replace` silently removes the planted entry | warning |
| directory at `state/<bot>.json` | FAIL "not a regular file (a directory; appends are refused)" | refused and spooled (exit 4) | warning; "every append writes its line and exits 2" |
| any non-regular `anchors/<bot>.json` (FIFO, directory, device link, dangling link) | FAIL "anchors/<bot>.json: not a regular file" | n/a | warning |

Sources: `_anchor_snapshot`, `_file_findings`, `_append_head`. The tests are `test_symlink_to_device_is_not_read`, `test_non_regular_anchor_file_fails_verify` and `test_fifo_state_and_alias_files_do_not_hang`.

The same planted object therefore gives exit 0 under `state/` and exit 1 under `anchors/`. The state FIFO case also destroys the evidence of the plant on the next append; only the `state_adopted` marker remains. Fix: G1-04, a decision with a recommendation.

### Z-06 (P1) `sync` is not isolated after the push

At the end of `sync()`:

```python
for b in ledger.valid_chains():
    ...
    ledger.update_state(b, {"last_sync": dict(stamp)})
```

Nothing here is guarded. With a directory at `state/x.json` (K2-08), `write_state` raises `IsADirectoryError`. With a non-regular or symlinked `state/x.lock` (K2-02), `_open_lock` raises `OSError`. Either reaches `main()` as `error: …`, exit 2. This happens after the commit and after a successful push. The `commit … pushed` line is never printed, and the chains after `x` get no `last_sync`.

The daily routine then reports "usage or environment error" for a run that did back up. This contradicts the "one chain cannot hide the others" guarantee.

Second point, same function. The comment says: "Every change of origin is recorded. When the record cannot be written, the change is undone." But `_bookkeep` catches `TailMismatchError`. On a tail mismatch of the sync Bot's chain the origin change stays and is not in the chain. The record is only in `spool/`, and the run pushes to the new remote. `SECURITY.md` states the record is "written to the ledger". Fix: G1-05.

### Z-07 (P2) Task-id diagnostics: duplicated, costly, inconsistent

- **Two implementations.** `_task_start_seen` (used by `task-end`) parses every JSON line of up to 30 days through `iter_records` and compares the unredacted id. `_task_status` (used by `log`, `note`, `approval`, `archive`, `task-start`) scans raw lines and compares the redacted id. For an id that redaction changes, `task-end` always warns "no task-start".
- **`task-end` never says "already closed".** `log` does.
- **Raw id in one warning.** The `task-end` warning prints `args.task_id` raw. `_warn_task` uses `_term`.
- **Cost.** `log`, `note`, `approval` and `archive` now read the whole newest day file after the append. For a task opened N days ago they read N days of files on each call. For an unknown id they read 30 days. DESIGN §12/§13 and FAQ 35 still say an append "does not grow with the number of records". That holds for `Ledger.append`, but not for the CLI commands.
- **Order bug.** `_task_status` sorts `(day, path)` in reverse and lets the first file with a hit decide. For a task started in chain `cos` and ended the same day in `cos-primary`, the path `…/cos/…` sorts after `…/cos-primary/…`, so `cos` is read first and the answer is "open".
- **Output order.** `archive` calls `_warn_task` before it prints its result line; `log` calls it after. An unexpected exception in the check would lose the only line that tells an agent the record exists.

Fix: G1-06.

### Z-08 (P2) "origin" is read two ways

In `sync()`, `--remote` handling reads `git config --local --get-all remote.origin.url`. The push list reads `git remote get-url --push --all origin`. Based on my recollection of git's `remote.c` (not checked against the git source here), a legacy `.git/remotes/origin` file counts as configured in the repository. If so, two things follow:

- The push goes to that URL although no local config names it. This is the pending test `SyncDeepTest.test_no_repo_origin_is_never_pushed`. `SECURITY.md` says such a remote "is never used".
- `gblog sync --remote NEW` sees `current == []`. It treats NEW as the first origin and sets it without `--replace-remote`. The recorded change reads `origin - -> NEW`.

Fix: G1-07. Keep `get-url` for the effective, `insteadOf`-rewritten URLs, but gate it on the local config.

### Z-09 (P2) Witness protection differs between the anchor file and the day rollup

`_anchor.anchor_one` keeps an existing `anchors/<bot>.json` when the new entry is a FAIL that names no head ("never erase a witness"). The day rollup has no such rule. `kept` drops every earlier same-day entry for the bots of this run, so a headless FAIL entry replaces a same-day entry that named a head.

`_rollup_heads` then skips today's entry and falls back to older days. For a chain first anchored today, the rollup witness is gone.

Also, `anchors/<bot>.json` is written before the history append. When the history append fails, the chain gets an `anchor aborted` entry (0 records, no head) in the rollup while the anchor file already holds the new OK entry. Fix: G1-08.

### Z-10 (P2) K2-06 is applied on half of the read paths

`_open_regular` tests the file type on the opened descriptor and never blocks. These sites still do `isfile()` and then a plain open, or no check at all:

- `_witness_present`
- `_spool_replayed`
- `_clamped_candidates`
- `tune_history`
- the history read in `_anchor_snapshot`
- the history read in `orphan_witnesses`
- `_gunzip_sha256` (`gzip.open`)
- `_git_blob_id`
- `_is_sqlite_file`
- `_install_shell_hook` (reads the rc file with no type check)

`SECURITY.md` "Auditing a clone" says gblog "never opens a FIFO or a device at a path it reads". That holds only when nothing is swapped between the two calls. Fix: G1-09.

### Z-11 (P2) Small defects with known shape

- **D1-13.** `verify` sets `archives = len(arch_recs)`, which includes bare claims. The pending test `test_bare_archive_record_is_not_counted_as_intact` documents it. The fix is one line.
- **D2-10.** While state is exactly one record behind, `verify` prints the generic "state tampered or log truncated" FAIL although `append` would roll forward.
- **Drift message.** For a non-regular evidence entry that is not a symlink (a directory `logs/x/zz.jsonl`), `sync` prints "(git ignore rule, filter or index flag)". Only `drift[0]` is explained.
- **`(witness files)` with `--bot`.** With `verify --bot X`, an unlistable `anchors/` or `anchors/days/` is not reported. The pseudo-entry is never in `gone`, so "no witness" and "witness directory unreadable" look the same.

Fix: G1-10.

### Z-12 (P2) Tests that contradict docs or code

- `BrowserWindowTest.test_hours_zero_is_a_usage_error` is pending with the comment "README and CHANGELOG say … below 1 exits 2". Both documents were corrected to "negative". The CHANGELOG says "`--hours 0` imports nothing on a first run". The pending test now asserts withdrawn behaviour.
- `DocumentedButAbsentTest` has the docstring "Each test fails today" and five `# bug:` comments. None of the five tests is marked, and all describe code that exists. Four duplicate tests in `test_round3_followups.py`.
- `InternalErrorGapTest.test_debug_env_reraises_from_verify_all` carries a `# bug: D-6` comment for behaviour that `_verify_guarded` has.
- `SyncGapTest.test_sync_cache_does_not_hide_a_changed_file` tests the dead cache branch (Z-04).
- `tests/PENDING.txt` says "All entries below are in tests/test_mutation_gaps.py". Two of the three are in `test_deep_reviews_b.py`.

Fix: G2-01 and G2-02.

### Z-13 (P2) Hook fallback fires on any exit 2

The v2 hook retries under the installed Bot when the first `gblog log` exits 2 and `GBLOG_BOT` differs. Exit 2 also covers environment errors that may already have written the record, for example `ENOSPC` on the state replace. In that case the command is logged twice, once per chain. Fix: G1-11.

### Z-14 (P2) Message and format mismatches

- Spool file name: DESIGN §4 step 3 and agent-integration §8 say `<UTC stamp>-<8 hex>.json`. The code and DESIGN §3 say `<stamp>-<nnnn>-<8 hex>.json`.
- Refusal order: DESIGN §4 step 3 says spool first, then alert. `_refuse_tail` writes the alert first.
- The seq-mismatch warning: the code ends "…until `gblog state rebuild` is run". DESIGN §4 and the CHANGELOG quote it without the backticks and "is run".
- agent-integration §3: "`--task` on `archive` … prints no warning". The code prints `warning: --task is not stored on archive records`.
- agent-integration §3 lists `--decision` and `--action` as closed sets. `approved`, `rejected`, `computer-use` and others are accepted.
- agent-integration §6 and the skill do not list the new stderr lines:
  - `task '<id>' is already closed`
  - `task '<id>' has no task-start…` on `log`, `note`, `approval`, `archive`
  - `task id '<id>' was already used`
  - `task-start without --task`
  - `no chain on disk for '<name>'; nothing to show`
  - `--all given; --bot … is ignored`
  - the `spool replay` sibling note

  An agent that follows "report `warning:` lines verbatim" will meet lines no document explains.
- `verify --strict` help text names three of the eight strict findings.
- `capture shell-hook --install` help says "append"; it also replaces an older block.
- FAQ 2 and DESIGN §7 do not list the new problems `state/<bot>.lock: not a regular file…`, `state/<bot>.json: not a regular file (a directory…)`, `anchors/<bot>.json: not a regular file`, or the symlink warning for an archived file.

Fix: G3-03.

### Z-15 (P3) Complexity hotspots

- `_main`: one 450-line `if` chain; every command shares one local namespace.
- `Ledger.verify`: about 250 lines mixing the ack scan, the walk, state checks, adoption warnings, anchors, archives and layout.
- `sync`: about 170 lines with nested helpers.
- Duplicated blocks:
  1. the candidate, gone and lost logic in the CLI `verify` branch is a copy of `Ledger.lost_chains_of`;
  2. `_orphan_alerts` rebuilds the same slug-plus-variants set;
  3. "attribute by directory" (`dict(rec, bot=chain, bot_field=…)`) appears five times: `tail_records`, `chain_records`, `records` twice, `tune_history`;
  4. the pattern "raw scan, parse only lines containing a marker" appears six times: `_clamped_candidates`, `tune_history`, `_task_context`, `_task_status`, `_spool_replayed`, `_witness_present`;
  5. the text `"%s: symbolic link (gblog does not write through one)"` appears five times;
  6. the "state missing or unreadable" warning appears twice;
  7. the directory-at-state predicate appears in `_append_head` and `_file_findings`;
  8. `self.ensure()` runs in both `anchor` and `_anchor`.
- Dead or test-only code: the sync cache (Z-04), `_GITHUB_REMOTE_RE`, `Ledger._tail_head`, `Ledger.chain_hashes`, the `install_shell_hook` wrapper.

Fix: G1-12.

### Z-16 (P3) Test-suite gaps and duplication

- `chrome_history` exists in six variants.
- `write_raw_chain`, `_AfterRelease` and `_Body` exist in three variants each.
- `stamp` is redefined in `test_integrity.py`.
- No fixture is shaped like a 0.2 to 0.5 ledger (K3-08). The Z-03 fix needs one.
- `_disk_name` has only a mocked unit test, and the macOS job skips the one filesystem test.
- Nothing checks docs against code. Such a check would have caught Z-01, Z-04 and Z-14.

Fix: G2-03 to G2-06.

### Z-17 (P3) Scripts and CI

- `examples/routine-daily-audit.sh` runs `gblog anchor` without `--skip-archives`. Archives are hashed twice per night. DESIGN §13 already admits this.
- The `scripts` CI job runs `shellcheck install.sh bin/gblog` only. The example scripts are linted only when a unit-test runner happens to have shellcheck.
- `install.sh`: a `# gblog PATH` block with a different prefix is left without a note; the ROOT block gets one. The installer does not mention that an existing hook block without `# gblog hook v2` must be reinstalled (README "Upgrading" item 6).
- `tests/bench_large_ledger.py` is never executed, so it can rot unnoticed.

Fix: G4-01 to G4-04.

### Z-18 (P3) The 0.6.1 CHANGELOG section corrects itself in layers

The section contains R-items, then A/B items, then W-items, then D-items with "the corrections that the specialist reviews made", then K-items "documentation only" with "In the code already", "listed until now" and "Documentation corrected" (one of which is now wrong again). A reader cannot derive the behaviour of 0.6.1 without replaying the day. Fix: G3-05.

### Z-19 (P3) Edges

- `_read_json` treats a side file above 16 MiB as unreadable. `anchors/<bot>.json` of a FAIL chain has no cap on `problems` or `segments` (K4-12). A chain with very many breaks can produce an anchor that `read_anchor` then refuses. `verify` loses the containment check to a warning, and `anchor_one` (with `prev is None`) overwrites it.
- `spool_replay` checks `_tail_ok` and then calls `append`. If the tail changes between the two, `append` spools the event a second time, with `spool_id` and `spooled_at` inside the record. A later replay appends both copies.

Fix: G1-10 items (e) and (f).

## 3. Fix work items

Each item is self-contained: problem, change, acceptance. Unless stated otherwise, stdout of existing commands, exit codes and the on-disk record format do not change.

### G1 — `gblog.py`

**G1-01 Guard `sync` against a foreign repository; tighten tune decisions (Z-02).**

- (a) In `sync()`, before `ledger.ensure()`: when `<root>/.git` exists, read `git config --local --get gblog.ledger`.
  - If it is `true`, continue.
  - Otherwise list the tracked paths (`git ls-files -z`). If every path is `README.md`, `.gitignore` or under `logs/`, `reports/`, `state/`, `tuning/`, `archive/`, `anchors/`, `spool/`, set the marker and continue. This covers existing ledgers.
  - Otherwise raise `_UsageError("<root> is a git repository that is not a gblog ledger (it tracks <first foreign path>); nothing was changed")`.
  - When gblog runs `git init` itself, set the marker.
  - Nothing may be created before this check. Move `ensure()` and the first `_bookkeep` after it.
  - This refusal is new behaviour for a ledger repository that also tracks other files. It needs the operator's sign-off; offer `git -C <root> config gblog.ledger true` as the documented opt-in.
- (b) In `analyze()`, count a `tune` record as a decision only when it carries a `proposal` object. `tune_apply` always writes one; `gblog log --type tune` cannot. First check records written by `--apply`/`--reject` of 0.2 to 0.5. If any lack `proposal`, accept `detail in ("applied", "rejected")` or a non-empty `proposal` instead.
- Acceptance:
  - `sync --no-push --root <repo with src/main.py tracked>` exits 2 and leaves `git status` and the directory listing unchanged.
  - An existing ledger repository syncs and gains the marker.
  - `log --type tune --target <pid> --outcome skipped` no longer changes `status` or `hidden` of `<pid>` in `tune --json`.

**G1-02 Use a non-normalised tail `ts` as a bound (Z-03; D1-09, K3-01).**

- Add `_tail_bound(ts)`. Return `ts` when `_is_norm_ts(ts)`. Otherwise collect candidates:
  - `parse_ts(ts)` when it parses;
  - `ts[:10] + "T" + ts[11:19] + "Z"` when `ts` matches `\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}` and the result is normalised.

  Return the maximum, or `None` when there is none.
- The second candidate guarantees that the bound compares greater than or equal to the stored text, which is what `verify` compares (`"Z" > "+"`, `"T" > " "`). It also guarantees that the bound's date is not earlier than the tail's day-file name.
- In `append()` and in the pre-check of `archive_file()`, replace the "drop if not normalised" lines by this bound. Everything else stays: an explicit `--ts` below the bound is refused with `_past_ts_error`; an implicit clock below it is clamped with `clock_regressed`; the existing future-tail warning fires when applicable.
- When the tail `ts` is a string and no bound can be derived, print once per append: `warning: last record of <bot> has an unusable ts; this record may not verify`.
- Acceptance: on a chain whose tail is `2026-11-17T08:00:00+09:00`, with the clock mocked to `2026-11-16T23:30:00Z`:
  - `append` writes `ts == "2026-11-17T08:00:00Z"` with `clock_regressed`;
  - the record goes to `2026-11-17.jsonl`;
  - `verify` is OK; `--strict` reports only the old record;
  - a second append succeeds.

  The same holds for a tail written `2026-11-17 08:00:00`. Remove the manual-wait paragraph from the README (G3-01).

**G1-03 Remove the sync cache (Z-04).**

- Delete `_SYNC_CACHE`, `_write_sync_cache`, the `cache_path` parameter and its branch in `_sync_drift`. Keep the always-hash path.
- Acceptance: `grep -n "sync-cache\|cache_path" gblog.py` is empty; `test_sync_keeps_no_verification_cache` still passes. See G2-02 for the test that goes with it.

**G1-04 One rule for non-regular side files (Z-05).**

- Add one helper, `_entry_kind(path)`, returning `missing`, `regular` (also behind a symlink) or `other`. The type test must use `os.stat` with a fallback to `os.lstat` for a dangling link, so that nothing is opened.
- Recommended rule for `verify`: `other` at `state/<bot>.json`, `anchors/<bot>.json` or `anchors/<bot>.history.jsonl` is a problem, `<rel>: not a regular file`. For the directory-at-state case keep the existing longer text.
- `missing`, and `regular` with unparsable content, keep today's warnings.
- `append` and `update_state` keep today's behaviour: a directory is refused and spooled; a FIFO or device is adopted with `state_adopted` and replaced.
- This turns one case from exit 0 into exit 1: a FIFO or device at `state/<bot>.json` in `verify`. It is unreleased 0.6.1 behaviour, and `test_fifo_state_and_alias_files_do_not_hang` asserts exit 0, so it needs sign-off.
- Fallback without behaviour change: keep the code and make the docs state the three rows of the Z-05 table exactly.
- Use the helper in `_append_head` and `_file_findings` so the directory predicate exists once.
- Acceptance: one table-driven test with FIFO, directory, device symlink and dangling symlink at each of the three paths. The verdict must be identical per entry kind across paths.

**G1-05 Isolate the `sync` stamp loop; make the origin record rule true (Z-06).**

- (a) Wrap each `ledger.update_state(b, …)` in `try/except OSError`. Collect `"<chain>: last_sync not written (<class>)"` in `result["stamp_errors"]`. The CLI prints one `warning:` line per entry on stderr after the `commit …` line. The exit code stays what the commit and push earned.
- (b) In the `if changed:` block, do not use `_bookkeep`. Call `ledger.append` directly. On `TailMismatchError`, call `_restore_origin(...)` and re-raise. The CLI exits 4, the event is in the spool, and origin is unchanged. The comment and the `SECURITY.md` row then hold literally.
- Acceptance:
  - With a directory at `state/zzz.json` and a reachable local bare origin, `sync` prints `commit <id> pushed`, exits 0, warns about `zzz`, and `state/ops.json` has `last_sync`.
  - With a tail mismatch on `system`, `sync --remote NEW --replace-remote` exits 4 and `git remote get-url origin` is unchanged.

**G1-06 One task-status function (Z-07).**

- Delete `_task_start_seen`. `task-end` uses `_task_status`:
  - `unknown` gives the existing "no task-start" warning (text unchanged);
  - `closed` (a second `task-end`) gives the "already closed" warning.
- Print the id through `_term` in every warning.
- In `_task_status`, decide per day across all chains of the Bot: collect the last `(ts, kind)` over every file of the newest day that mentions the id, and take the greatest `ts`. This fixes the `cos` / `cos-primary` order bug.
- Bound the scan. Stop after 8 MiB of raw lines without a hit and return `open` (no warning). State the bound in the docstring.
- In the `archive` branch, call `_warn_task` after `out.write`. Wrap `_warn_task` in `try/except Exception: pass`; a diagnostic must never change the result of a write command.
- Acceptance:
  - A test for start in `cos`, end in `cos-primary` on the same day gives `closed`.
  - `task-end` twice warns "already closed".
  - A 50 MB newest day file without the id makes `log --task-id X` read at most 8 MiB. Check it with a counting wrapper around `_open_regular`.

**G1-07 One definition of "origin" (Z-08).**

- Add `_local_origin(root)` returning `(urls, pushurls)` from `git config --local --get-all remote.origin.url` and `…pushurl`. Use it for `current` and `old_push`.
- Before the push list is read, if both are empty, set `result["error"] = "no origin configured in the ledger repository; …"` and `push = False` without calling `git remote get-url`. Keep `get-url --push --all` for the effective URLs when a local origin exists; it applies `insteadOf` and `pushInsteadOf`, which the guard must see.
- In the `--remote` path, when `current` is empty but `.git/remotes/origin` or `.git/branches/origin` exists, raise `_UsageError` naming the file. Do not treat NEW as the first origin.
- Acceptance: the pending test `SyncDeepTest.test_no_repo_origin_is_never_pushed` passes for all three sub-cases. Remove its marker and its line in `tests/PENDING.txt`.

**G1-08 Protect the rollup entry like the anchor file (Z-09).**

- In `_anchor`, when merging today's rollup: if this run's entry for a bot is not `ok` and names no head, and the earlier same-day entry named at least one head or had `records > 0`, keep the earlier entry and attach the new one under the additive key `"later": [entry]`.
- In `anchor_one`, append the history line before writing `anchors/<bot>.json`. A failed history append then leaves the previous anchor file untouched.
- Acceptance: anchor a chain, empty its day files, delete state, anchor again on the same mocked day. The rollup still names the old head, and `verify` fails with the `anchors/days/<date>.json: anchored head …` problem.

**G1-09 Finish K2-06 (Z-10).**

- Replace open-after-`isfile` with `_open_regular` (catching `OSError` as "unreadable" where the caller did before) in:
  - `_witness_present`
  - `_spool_replayed`
  - `_clamped_candidates`
  - `tune_history`
  - both history reads
  - `_git_blob_id`
  - `_is_sqlite_file`
- In `_gunzip_sha256`, open with `_open_regular` and pass `fileobj=` to `gzip.GzipFile`.
- In `_install_shell_hook`, read the rc file through `_open_regular` (resolve the path first).
- Acceptance: a test that patches `os.path.isfile` to return `True` for a FIFO, the same technique as `test_read_json_bounds_and_types`. Each listed reader must return or raise `OSError` within a `run_cli` timeout.

**G1-10 Small defects (Z-11, Z-19).**

- (a) D1-13: in `verify`, set `archives = sum(1 for r in arch_recs if r.get("path") or r.get("sha256"))`. Remove the marker and the `PENDING.txt` line of `ArchiveClaimGapTest.test_bare_archive_record_is_not_counted_as_intact`.
- (b) D2-10: when the only problem is the state-head mismatch, and the last record's `prev` equals the state head, and its `seq` equals state `seq + 1`, append the warning `state/<bot>.json is one record behind (interrupted append); the next append repairs it`. The verdict stays FAIL.
- (c) Drift message: for `staged_size < 0` and not a link, use `"<rel> is not a regular file; not pushed"`. Append `" and N more"` as today.
- (d) `verify --bot`: when `missing_chains()` returns the `(witness files)` entry, emit it for `--bot` as well.
- (e) Cap what `anchor_one` stores: the first 200 `problems` and `segments` plus the additive key `"truncated": {"problems": n, "segments": m}`. `segment_heads` stays complete, because containment needs it.
- (f) `spool_replay`: catch `TailMismatchError` from `append`, remove the spool file that this refusal just created (`exc.spool_path`), set `refused = True` and break.
- Acceptance: one test per letter. For (e), a chain with 5 000 breaks gives an anchor file below 1 MiB and `verify` still checks every segment head.

**G1-11 Hook fallback only for a refused name (Z-13).**

- In `SHELL_HOOK`, before the fallback, test the name without writing: `gblog alias resolve "${GBLOG_BOT}"`.
- Fall back only when that command exits non-zero or prints `(unregistered)`. In strict mode the latter is the refusal.
- Keep the marker line `# gblog hook v2` and the block markers. The text change makes `--install` report `updated`, which is correct.
- Acceptance: extend `HookFallbackTest`. With a fake `gblog` that exits 2 for `log` but resolves the name, there is exactly one call. With `(unregistered)`, there are two.

**G1-12 Simplifications without behaviour change (Z-15).** Do each as its own commit with the suite green.

1. The CLI `verify` branch calls `ledger.lost_chains_of(args.bot, bots)`. `_orphan_alerts` takes its name set from a new `Ledger.names_of(name)` shared with `lost_chains_of`.
2. Add `Ledger._attributed(rec, chain)` (the five copies) and `Ledger._scan(paths, marker)` (yields parsed lines that contain `marker`, via `_open_regular`) for the six raw scans.
3. Use the constants `_LINK_TEXT` and `_STATE_UNREADABLE` for the repeated texts.
4. Split `_main` into `cmd_<name>(ledger, args, out)` functions and a dispatch dict. Keep the pre-dispatch block (resolver, `GBLOG_TASK`, warnings) in `_main`.
5. Split `Ledger.verify` into `_scan_acks`, `_walk`, `_state_findings` and `_assemble` over the one snapshot. The lock scope must stay exactly as it is.
6. Remove `ensure()` from `_anchor` (`anchor` calls it). Drop the `install_shell_hook` wrapper only if the tests are moved to `_install_shell_hook`. Keep `_GITHUB_REMOTE_RE`, `_tail_head` and `chain_hashes` (tests and library callers use them) but mark them "not used by gblog itself" in one place.

Acceptance: no test changes except imports; `gblog.py` shrinks by at least 250 lines.

### G2 — tests

**G2-01 Resolve the `--hours 0` pending test (Z-12).**

Delete `BrowserWindowTest.test_hours_zero_is_a_usage_error` and its two lines in `tests/PENDING.txt`. Add the positive test the docs now promise: `capture browser --hours=0` on a first import exits 0, prints `browser: 0 new visit(s)…`, and a second run with the default `--hours` imports nothing older than the first run's marker. If G3-02 decides to refuse `--hours 0`, invert this item instead.

**G2-02 Remove stale intent (Z-12).**

- In `test_mutation_gaps.py`:
  - Delete `DocumentedButAbsentTest`. Keep `test_effect_table_limited_to_90_days` by moving it to `TuneWindowTest` in `test_round3_followups.py`; the other four are duplicates there.
  - Drop the `# bug: D-6` comment on `test_debug_env_reraises_from_verify_all`.
  - Delete `test_sync_cache_does_not_hide_a_changed_file` together with G1-03. Keep its first half (a same-size rewrite is drift) as a cache-free test.
- Rewrite the header of `tests/PENDING.txt`: one comment per entry naming its file. After G1-07, G1-10 (a) and G2-01 the list is empty; keep the file with its two explanatory lines.

**G2-03 Tests for the new fixes.**

One test class per G1 item, as listed under each item's acceptance. Two additions:

- A test that `sync` on a healthy ledger still prints exactly `commit <id> pushed\n` on stdout. This guards the G1-05 output change.
- A test that `Ledger.append` itself reads no day file beyond the tail. Count `_open_regular` calls. This pins the DESIGN §13 claim to the library, where it is true.

**G2-04 Legacy-shaped ledger test (K3-08, needed by G1-02).**

Build in a temporary directory, with a small builder in `tests/_support.py` and no new fixture files, a chain as 0.5 wrote it:

- records with free-form `ts` (`+09:00`, space separator);
- a state file with the single integer `capture_browser_last`;
- an `aliases.json` entry without `aliases`;
- a spool file with the old name `<stamp>-<8 hex>.json`.

Assert that `verify` is OK, that `--strict` lists only the `ts` lines, and that append (after G1-02), `capture browser`, `alias list` and `spool replay` all work.

**G2-05 Shared helpers (Z-16).**

Move `chrome_history` (one signature: rows of `(url, title, when)` plus `ago=True/False`), `write_raw_chain`, `AfterRelease` and `FakeBody` into `tests/_support.py`. Import them in the six modules. Delete the local `stamp` in `test_integrity.py` in favour of `_support.stamp(n, day)`.

**G2-06 Docs-versus-code lint (new `tests/test_docs.py`).**

Standard library only; it reads the Markdown files. Four checks:

1. Every `gblog <subcommand> --flag` in a fenced or inline code span of `README.md`, `docs/*.md` and `skills/*/SKILL.md` parses with `gblog.build_parser()`. Unknown flags fail; placeholder values are substituted.
2. Every id in CHANGELOG "Known open findings" that appears in a `gblog.py` comment of the form `(K\d-\d+)` or `K\d-\d+:` is reported, unless it is listed in a small allow-dict of partly fixed ids with a reason.
3. Every quoted message in DESIGN §7 and FAQ 2 and 4 (text between backticks that starts with `state/`, `anchors/`, `logs/` or `warning:`) matches a string literal in `gblog.py` after `<placeholders>` are replaced by `%s`/`%d`.
4. The string `gblog-sync-cache` appears in no document.

**G2-07 Case-insensitive filesystem test.**

Add a test that runs only when the temporary directory ignores letter case (the inverse of the existing skip). After `seed("Ops")`, the call `append("ops", …)` writes `bot: "Ops"`, `verify --bot ops` is OK without a bot-field warning, and `--contains` finds the new head. The macOS CI job then covers `_disk_name` for real.

### G3 — docs

**G3-01 Make the status of every review item true (Z-01, Z-04).**

Apply this table to CHANGELOG ("Specialist reviews", "Deep subsystem reviews", "Known open findings", "Future work"), the `SECURITY.md` "Known limits" table and its notes, DESIGN §2/§5/§7/§13, README ("How it works", "Upgrading from 0.5"), FAQ 35 and agent-integration.

| Id | State in the code today | What the docs must say |
|---|---|---|
| K1-01 | fixed | `report --date` names tasks closed after or opened before the day; `status --json` has `open_task_list` |
| K1-03 | fixed (stderr warning, exit 0) | readers warn `no chain on disk for '<name>'` |
| K1-04 | fixed | `anchor --bot X` exits 1 for a deleted chain with a witness |
| K1-05 | fixed | `spool list --bot` covers every chain of the Bot; `spool replay` stays per chain and prints a note |
| K1-06 | fixed, see G1-06 | `log`, `note`, `approval`, `archive`, `task-start` warn about closed, unknown or reused ids |
| K1-07 | fixed, see G1-11 | the hook falls back to its installed Bot |
| K1-08 | half | `tune --apply` follows the resolver; `verify --record` still writes to variant chains |
| K1-09 | fixed for writes | on a case-insensitive file system the existing directory spelling is used |
| K1-11, K1-13 | fixed | `--decision` synonyms, `--action computer-use`, `--since today/yesterday`, the `--all` plus `--bot` notice |
| K2-01 | fixed | FAIL entries `(witness files)` and `(day rollup)` |
| K2-02 | fixed | a FIFO lock is a `verify` problem and no hang; a hard link to `.anchors.lock` is detected |
| K2-04 | fixed | a symlinked archived file is a `!` (a problem with `--strict`); `sync` reports drift and exit 1 |
| K2-05 | fixed | temporary files use `O_EXCL`; reports and tuning files are replaced atomically; a hard link is never written through |
| K2-06, K4-03 | half | the type is tested on the open descriptor (after G1-09: everywhere); the tail lookup is linear; side files above 16 MiB count as unreadable; a log line has no size bound |
| K2-07 | fixed for git calls made by gblog | objects are created with umask 077; existing objects and an existing `.git` keep their modes |
| K2-08 | fixed | the directory is a FAIL; the append is refused and spooled (exit 4) |
| K2-09 | half | a FIFO at `proposals.json` is handled; there is still no lock |
| K2-10 | fixed | exit 4 with the alert even when the spool cannot be written; a non-regular day-file target is spooled |
| K2-12 | fixed | the hook block is replaced atomically |
| D3-05 | decided | there is no cache; every run hashes every evidence file |
| D1-01 | changed | see G1-04; state the final rule in one table |
| D3-03 | open until G1-07 | remove the `.git/remotes/` claim from `SECURITY.md` until then |
| D1-09, K3-01 | open until G1-02 | keep the README workaround until then |

Still open, keep as listed: D1-11, D1-13 (until G1-10), D2-07/D4-17, D2-10 (until G1-10), D2-11 to D2-13, D3-10, D3-11, D3-14, D4-13 to D4-22, D5-08, D6-13, D6-15, K1-10, K1-12, K2-13, K2-14, K3-04 to K3-08, K4-02, K4-04 to K4-13.

Further edits in the same pass:

- Replace the CHANGELOG sentence "This pass changed documentation only" with a list of the code changes above.
- In DESIGN §13, change the `sync` row to "git, plus a hash of every evidence file: O(bytes of logs, anchor histories and archives)". Delete the cache paragraph under "Kept as they are" and the L² row.
- In DESIGN §13 and FAQ 35, add to the append row: "the CLI commands that carry a task id also scan the newest day file for that id (bounded, see G1-06)".

**G3-02 Scope the Auto-review allow-list (Z-02).**

- In README setup step 6, state that every rule must pin the ledger and exclude:
  - `--root` on any command, and a command line prefixed with `GBLOG_ROOT=`;
  - `--ts`;
  - `tune --apply` and `tune --reject`: allow `gblog tune` only with `--days`, `--bot`, `--stdout`, `--json`;
  - `capture browser --history`, and decide on `--hours 0`;
  - `sync --branch` and `sync --bot`;
  - `log --type` other than the default.
- Add the same list to "Never allow automatically", with the reason for `--root` in one sentence: `gblog sync --root <repo>` commits and pushes that repository.
- In `SECURITY.md`, add `--root` to the CLI-only paragraph, and after G1-01 add the row "root is a git repository that is not a ledger → exit 2".
- In agent-integration §12, add `--root`, `tune --apply/--reject` and `capture browser --history` to the operator-only table.
- In `docs/routine-weekly-tune.md`, say that step 6 (`tune --apply/--reject`) waits for the approval card.

**G3-03 Message and format corrections (Z-14).**

- DESIGN §4 step 3 and agent-integration §8: spool name `<stamp>-<nnnn>-<8 hex>.json`; order "alert first, then spool; a failure of either is named in the `ALERT` line".
- DESIGN §4 and CHANGELOG D1-06: quote the seq-mismatch warning exactly as the code prints it.
- agent-integration §3: `archive --task` prints a warning; list the `--decision` and `--action` synonyms.
- agent-integration §6 and `skills/activity-log/SKILL.md` "Common mistakes": add the seven new stderr lines from Z-14 with meaning and action. State that none of them changes stdout or the exit code.
- FAQ 2 and DESIGN §7: add the three new problem texts and the archived-file symlink warning.
- In `gblog.py` help strings (text only): complete the `--strict` list; change `--install` to "append the hook to --rc, or replace an older gblog block in it".

**G3-04 State the cost of the K1-06 check and the sync hash.** This is covered by the last bullet of G3-01. In addition, `docs/routine-daily-audit.md` "Why the routine is shaped this way" gets one sentence: `sync` hashes every archived file once per run; this replaces the "three times" arithmetic.

**G3-05 Restructure the 0.6.1 CHANGELOG section (Z-18).**

Rewrite it in the Keep-a-Changelog groups the file already uses: Behaviour changes, Security, Added, Changed, Fixed, Documentation, Known open findings. Each bullet states the final behaviour once and carries all ids in parentheses. Delete "In the code already", "listed until now", "Documentation corrected" and every "Correction:" sentence. Move the review history (which review found what) to a short `docs/reviews-2026-11-16.md` linked from the section header. No statement about behaviour may appear in two places.

**G3-06 README "Upgrading from 0.5".** After G1-02, replace the "last record with a local-time `--ts`" limit by: "the first append after such a record is stamped with that record's clock time as UTC and carries `clock_regressed`". After G2-07, replace the letter-case limit by the `_disk_name` behaviour, and keep the advice not to register a slug that differs only by case.

**G3-07 `SECURITY.md` "Private-remote policy".** State where the origin-change record goes on a tail mismatch (after G1-05: the change is undone, exit 4). Remove the cache note. After G1-07, keep the `.git/remotes/` sentence and add "`--remote` refuses to set a first origin while such a file exists".

**G3-08 Test comments as documentation.** The module docstrings of `test_security_regressions.py`, `test_mutation_gaps.py` and `test_cli_ux.py` describe the pending mechanism. After G2-02, state in each that nothing in the module is pending, and point to `tests/PENDING.txt` as the single list.

### G4 — scripts and CI

**G4-01 `examples/routine-daily-audit.sh` (Z-17).**

- Call `run anchor anchor --skip-archives`. The step-1 verify is the full check. Update the header comment and `examples/README.md` step 3.
- In `print_heads`, print lines whose chain field starts with `(` verbatim as `<name> FAIL (no head)`, so `(witness files)` and `(day rollup)` appear in the off-box block.
- Update the stand-in argument list in `tests/test_examples.py::test_exit_code_mapping` (`"anchor --skip-archives"`). Extend the tamper test to assert the `(witness files)` line with a patched listing.

**G4-02 CI: lint what ships.**

- In the `scripts` job, change the shellcheck step to `shellcheck install.sh bin/gblog examples/*.sh` and add `bash -n examples/*.sh`.
- Add a step `python tests/bench_large_ledger.py --records 2000 --padding-days 2 --appends 5` to the 3.12 Linux leg, so the benchmark cannot rot. It makes no assertions; it must exit 0.

**G4-03 CI smoke for the new guards.** After G1-01, add to "Smoke test CLI":

- create a scratch git repository with one tracked `src/x` file;
- run `./bin/gblog sync --no-push --root "$scratch"`;
- require exit 2 and an unchanged `git -C "$scratch" status --porcelain`.

After G1-05, add a block that plants `mkdir "$GBLOG_ROOT/state/zzz.json"` beside an empty `logs/zzz/` and requires `sync --no-push` to exit 0 with a `warning:` line.

**G4-04 `install.sh`.**

- In `persist`, when a `# gblog PATH` block exists and no `export PATH=` line of the file names the current prefix, print `note: <rc> already has a '# gblog PATH' block for another prefix; left unchanged`. This mirrors the ROOT note.
- After the rc loop, grep each handled file for `# >>> gblog shell hook >>>` without `# gblog hook v2`. When found, print `note: <rc> holds an older gblog shell hook; run: gblog capture shell-hook --bot <slug> --install`.
- Both are stdout or stderr notes only. Reruns stay byte-identical for the rc files; the existing `cmp` checks in CI must still pass.
- Add one CI assertion for each note in "Install handles a relative root, legacy rc lines and ~/.bash_profile".

**G4-05 Optional: a Windows smoke leg.** FAQ 34 says "partly, untested". A `windows-latest` step that runs only `python gblog.py --root %TEMP%\g init`, `note`, `verify --all` and `status` would turn "untested" into a checked minimum without porting the suite. Skip this if the project does not want to imply support.

## 4. Order and dependencies

1. G3-01 and G3-02: documentation only. They remove the false claims at once and close the policy gap without code.
2. G1-02 with G2-04; G1-05; G1-01 with G4-03 (needs sign-off for the refusal).
3. G1-04 (decision), G1-07, G1-08, G1-09, G1-10, G1-11 with their G2-03 tests. Then G2-01 and G2-02 empty `tests/PENDING.txt`.
4. G1-03, G1-06, G1-12, G2-05: simplifications, each behind a green suite.
5. G2-06, G2-07, G3-03 to G3-08, G4-01, G4-02, G4-04. The docs lint should land before the CHANGELOG rewrite so that the rewrite is checked by it.

## 5. Checked and found consistent

- `Ledger.append`: the name check comes before any side effect; genesis state is written before the first line; the roll-forward is persisted before the new line; the `state_adopted` marker moves from state onto the record; a failed write is rolled back by prefix comparison.
- `Ledger.verify`: one snapshot under the shared lock; the ack pin (`KIM_BISEO_BREAK` and the fixture, by hash); the state-head check on an empty walk; containment for anchor, history and rollup; `own_hashes` for `--contains`.
- Locks: `.anchors.lock` is always taken before any bot lock; `aliases.lock` is a leaf; no path nests two bot locks; `verify` releases its lock before archives are hashed.
- `_atomic_write` and `_open_private`: `O_EXCL` temporary files; stale regular leftovers are replaced; links and FIFOs are refused and unlinked; there is no in-place truncation.
- Remote classification: parsed host; helper, bracket, percent and IDNA forms are "odd" and fail closed; a wiki maps to its repository; the `gh` lookup is pinned to github.com.
- Python 3.8: no syntax or standard-library call newer than 3.8 was found (`str.isascii` is 3.7). The Windows fallbacks (`O_NOFOLLOW`, `O_NONBLOCK`, `fchmod`, `fcntl`) are all guarded with `getattr`/`hasattr`.
---
# Appendix — round-4 audit at effort=high

Scope: the 0.6.1 branch as given (`gblog.py`, tests, docs, `install.sh`, `bin/gblog`, `examples/`, CI). Nothing was edited or executed; every finding comes from reading the code against the docs and tests, and each one states its confidence. Record format, hash rule and existing CLI output are treated as frozen.

### 1. Executive summary

The cryptographic core is sound: I found no new way to make `verify` print `OK`/`PASS` for an edited, truncated or forged chain while a witness survives. What is wrong now comes mostly from several review rounds landing on the same day.

1. **The documentation describes an older tree (Z-01, Z-02, Z-10, Z-13).** The CHANGELOG says the K1–K4 pass "changed documentation only", but the code and `tests/test_specialist.py` contain most K1/K2 fixes. The CHANGELOG, SECURITY, DESIGN and README still list them as open, in the pessimistic direction. Four documents also say `sync` uses a verification cache; `sync()` no longer does.
2. **Four code defects are worth fixing before 0.6.1 is cut.**
   - Z-03 (known as D1-09/K3-01, still the only way ordinary use can make a chain FAIL for good): an append after a pre-0.6 tail whose `ts` is not normalised.
   - Z-04 (new, from K2-10): `spool replay` spools an event a second time when the append inside it is refused, and the event is later appended twice.
   - Z-05: a legacy `.git/remotes/origin` file is still pushed to, although SECURITY.md says it never is.
   - Z-06 (new, from K1-09): `_disk_name` diverts a new chain `ops` into an existing chain `OPS` on a case-sensitive filesystem.
3. **Parallel edits left duplicated and contradictory logic (Z-07 … Z-11, Z-14, Z-15).** Examples: two task-id scanners with different matching rules, the "lost chain" selection written twice, three different rules for a non-regular side file, and different keyword lists in sibling redaction rules.
4. **The test suite misdescribes itself in places (Z-12).** One `expectedFailure` asserts behaviour the docs have since withdrawn, and a class of passing tests is still labelled "fails today".

Suggested order: G1-01 … G1-04 (defects), G3-01 … G3-03 (false claims), then duplication and hygiene.

### 2. Findings

Severity: **H** = wrong result, lost or duplicated evidence, or a false security claim. **M** = wrong behaviour in a plausible situation, or a material false doc claim. **L** = inconsistency, hygiene or cost.

| Id | Sev | Area | One line | Work item |
|---|---|---|---|---|
| Z-01 | H | docs | K1/K2 fixes are in the code; docs say "documentation only" and list them as open | G3-01, G3-02 |
| Z-02 | H | docs + code | Docs say `sync` uses `.git/gblog-sync-cache.json`; it does not; dead cache code and a test remain | G1-05, G3-03 |
| Z-03 | H | append | Non-normalised tail `ts` can still brick a chain (D1-09/K3-01) | G1-01 |
| Z-04 | H | spool | `spool replay` re-spools and later duplicates an event refused inside the replay | G1-02 |
| Z-05 | M | sync | `.git/remotes/origin` is pushed to; SECURITY/CHANGELOG say never | G1-03 |
| Z-06 | M | names | `_disk_name` misfires on case-sensitive filesystems (`ops` → `OPS`) | G1-04 |
| Z-07 | M | sync | A failure while stamping `last_sync` after a successful push exits 2 and loses the result line | G1-06 |
| Z-08 | M | hook | The K1-07 fallback fires on every exit 2 and writes a duplicate record with a false detail | G1-07 |
| Z-09 | M | CLI | Two task-id scanners with different rules; `task-end` misses a double close; per-command scan cost | G1-08 |
| Z-10 | M | verify + docs | Non-regular side files follow three different rules; docs state a fourth | G3-04 (G1-09 optional) |
| Z-11 | M | CLI | Lost-chain selection and FAIL-entry dicts duplicated; synthetic `bot` names undocumented | G1-10, G3-05 |
| Z-12 | M | tests | Stale `expectedFailure`, "fails today" tests that pass, a test of dead code, a weak default runner | G2-01 … G2-03 |
| Z-13 | M | docs | `agent-integration.md` and DESIGN §4 contradict the code | G3-06 |
| Z-14 | L | readers | `_open_regular` is used in some readers; others still do `isfile()` then `open()` | G1-11 |
| Z-15 | L | redaction | Sibling rules use different keyword lists (`--pwd`, `--access-key` unprefixed are missed) | G1-12 |
| Z-16 | L | sync / alias | An `origin` change survives when its record is only spooled; alias changes leave no record | G1-13 |
| Z-17 | L | sync | `umask 077` also covers `git push` to a local-path remote, and is process-wide | G1-14 |
| Z-18 | L | hook | Atomic rc-file replace fails on bind-mounted rc files | G1-15 |
| Z-19 | L | messages | Two wordings for one condition; two warnings print raw control characters; docs misquote a warning | G1-16, G3-06 |
| Z-20 | L | docs | `bot-tune` skill and the weekly routine give different capture commands | G3-07 |
| Z-21 | L | docs | SECURITY and README/example disagree on `--remote` in routines | G3-07 |
| Z-22 | L | scripts/CI | Example script re-hashes archives twice; CI gaps | G4-01 … G4-04 |
| Z-23 | L | size | Complexity hotspots and repeated idioms | G1-17 |

#### Z-01 (H) — Docs say the K1–K4 pass was documentation only; the code contains the fixes

Confidence: high. Each row is backed by a function in `gblog.py` and a test in `tests/test_specialist.py`.

The CHANGELOG ("Specialist reviews", and "They changed documentation only" in the header) is false. As a result, "Known open findings", the "Known limits" table in SECURITY.md, DESIGN §2 and §13, and README "Known limits of this upgrade" list defects that are fixed. An operator reading them would add workarounds that are no longer needed, and a reporter is told that fixed behaviour "needs no new report".

| Listed as open | In the code (evidence) |
|---|---|
| K1-01 task crossing UTC midnight; no list of open task ids | `_task_context`; `open_task_list` in `status --json` |
| K1-03 mistyped `--bot` is silent | `_warn_unknown_bot` (still exit 0, now with a warning) |
| K1-04 `anchor --bot` exits 0 for a deleted chain | `Ledger.lost_chains_of`, used by the `anchor` branch |
| K1-05 `spool list --bot` looks at one chain | `spool_chains_for` |
| K1-06 only `task-end` diagnoses a bad task id | `_task_status` / `_warn_task` (see Z-09) |
| K1-07 hook drops events of a refused `GBLOG_BOT` | fallback block in `SHELL_HOOK` (see Z-08) |
| K1-08 `tune --apply` bypasses the resolver | `tune_apply` uses `ledger.canonical` (`verify --record` unchanged, by design) |
| K1-09/K3-03 case-insensitive filesystems | `_disk_name` (see Z-06) |
| K1-11 no synonyms for `--decision` / `--action` | `_norm_decision`, `_norm_action` |
| K2-01 unlistable `anchors/`, unwritable rollup → exit 2 | `_witness_listing`, `(witness files)` / `(day rollup)` entries |
| K2-02 FIFO or hard-linked lock file | `_open_lock`, `_Lock(avoid=…)` |
| K2-04 symlinked evidence file is "pushed" | `_sync_evidence` size `-1` → drift |
| K2-05 hard link / FIFO at an overwritten path | `_open_private` (`st_nlink`, `O_EXCL`), `_write_markdown` |
| K2-06/K4-03 quadratic tail lookup; FIFO swapped in | linear `_last_line`, `_open_regular`, 16 MiB bound in `_read_json` (log lines still unbounded) |
| K2-07 `.git/objects` readable | `umask 077` in `_git`, `.git` chmod 0700 |
| K2-08 directory at `state/<bot>.json` | refused and spooled in `_append_head`; problem in `_file_findings` |
| K2-09 FIFO at `proposals.json` | `_read_json(..., limit=None)` (the missing lock is still open) |
| K2-10 refusal that cannot be spooled; non-regular day file | `_refuse_tail` tolerates failures; day-file check in `append` |
| K2-12 hook update rewrites the rc file in place | `_atomic_write` in `_install_shell_hook` |
| D3-05 sync cache | removed from `sync()` (Z-02) |

Still open as far as I can tell, and should stay listed: D1-09/K3-01 (Z-03), D1-11, D1-13, D2-07/D4-17, D2-10 … D2-13, D3-10, D3-11, D3-14, D5-08 (partly), D6-13, D6-15, K1-10, K1-12, K2-09 (lock), K2-13, K2-14, K3-04 … K3-08, K4-02, K4-04 … K4-13.

#### Z-02 (H) — The sync verification cache is documented as in use; it is not

Confidence: high.

- `sync()` calls `_sync_drift(root, checked)` with no cache path. The comment there says why ("a cache file under .git/ is writable by whoever the check is meant to catch"). `SyncFilesystemTest.test_sync_keeps_no_verification_cache` asserts it.
- These still say a file "is not hashed again" when the cache matches:
  - CHANGELOG ("`sync` content check (D3-05, open)", and "Corrected: `sync` still uses its cache");
  - SECURITY.md (private-remote notes, and the "Known limits" row);
  - DESIGN §13 (the `sync` row and "Kept as they are");
  - FAQ 35.
- Dead code remains: `_SYNC_CACHE`, `_write_sync_cache`, and the `cache_path` branch of `_sync_drift` (about 40 lines). `SyncGapTest.test_sync_cache_does_not_hide_a_changed_file` keeps that branch alive.

The cost claim is also wrong in the cheap direction: every `sync` now hashes every log, anchor-history and archive file, so the cost is O(bytes of all evidence), not O(changed bytes).

#### Z-03 (H) — A non-normalised tail `ts` can still make a chain FAIL permanently

Confidence: high on the mechanism; it is acknowledged as D1-09/K3-01.

In `Ledger.append`, `if not _is_norm_ts(tail_ts): tail_ts = None` disables both the "explicit `--ts` is earlier" refusal and the clock clamp. `archive_file` carries a second copy of the same bound. `verify` compares `ts` as text and walks day files by name. After a pre-0.6 tail such as `2026-11-17T08:00:00+09:00`, a UTC "now" of `2026-11-16T23:30:00Z` produces a record that sorts before its predecessor (`timestamp goes backwards`, permanent) and lands in an earlier day file (every later append exits 4).

This is the highest-value remaining code defect because ordinary use can trigger it and it cannot be repaired afterwards. A fix that changes no stored byte exists; see G1-01.

#### Z-04 (H) — `spool replay` can spool an event again and later append it twice

Confidence: high by reading; no existing test covers it.

- `spool_replay` pre-checks `_tail_ok`, then calls `append(bot, rec, _guard=…)`. It catches `_GuardSkip` and `RecordError` only.
- `append` can still raise `TailMismatchError` after the pre-check. The deterministic case is the K2-10 branch, where a FIFO or directory sits at the target day file; a race is the other.
- In both cases `_refuse_tail` writes a new spool file whose record already carries `spooled_at` and `spool_id`. The exception leaves `spool_replay`, and the original spool file stays pending.
- On the next run the original is replayed. Then the copy is replayed under its own new `spool_id`, which the `_spool_replayed` guard cannot recognise. The same event is in the chain twice, plus once more for every failed `spool replay` run in between.

The docstring ("without spooling the event a second time") holds only when the pre-check catches the mismatch.

#### Z-05 (M) — `.git/remotes/origin` is still a push target

Confidence: medium-high. The pending test comment says the same; my reading of git is that `git remote get-url` accepts a remote defined by a legacy remotes file as "configured in the repository".

- SECURITY.md says a remote named `origin` found in "global configuration, `.git/remotes/`, a directory of that name" is never used. The CHANGELOG (D3-03) says the same.
- `sync()` decides "is there an origin?" from `git remote get-url --push --all origin`.
- The `--remote` branch of the same function uses a different definition: `git config --local --get-all remote.origin.url`. With only a remotes file present, `--remote X` treats `origin` as unset and silently adds one, while a plain `sync` pushes to the file's URL.
- `SyncDeepTest.test_no_repo_origin_is_never_pushed` is the pending test. It bundles three cases in one `expectedFailure`, so the two that work are not protected against regression.

#### Z-06 (M) — `_disk_name` diverts writes on case-sensitive filesystems

Confidence: high.

`_disk_name` detects a case-insensitive filesystem by `os.path.isdir(logs/<bot.swapcase()>)`. On a case-sensitive filesystem that test is also true when the exact case-swapped chain exists as a separate chain (`OPS` vs `ops`, `QA` vs `qa`).

- The first `gblog note --bot ops` is then written into `logs/OPS` with `bot: "OPS"`.
- `chains_for("ops")` falls back to `["OPS"]`, so `verify --bot ops` prints `OK OPS …` where it used to print `no chain on disk for 'ops'`.
- `agent-integration.md` §7 promises that "`Ops` and `ops` are two chains" without a registry.
- `SmallConsistencyTest.test_case_sensitive_filesystem_keeps_two_chains` uses `ops`/`Ops`, whose swap-case is `oPS`, so it misses the case.

#### Z-07 (M) — A failed `last_sync` stamp turns a successful push into exit 2

Confidence: high.

The loop at the end of `sync()` calls `ledger.update_state(b, …)` for every chain without isolation. Since K2-02 and K2-08, a FIFO at `state/<bot>.lock` or a directory at `state/<bot>.json` raises `OSError` there (before, it hung). The exception reaches `main()` after commit and push, so the operator sees `error: …` with exit 2 and never sees `commit <id> pushed`. Chains later in the alphabet get no stamp. The same applies to `_bookkeep` for an `OSError` on the sync Bot's own chain: one planted entry at `state/system.lock` blocks every backup.

This contradicts the principle stated in SECURITY.md that one damaged chain must not stop the others.

#### Z-08 (M) — The hook fallback fires on any exit 2

Confidence: high.

The K1-07 block in `SHELL_HOOK` retries under the installed Bot when the first `gblog log` exits 2 and `GBLOG_BOT` differs. Exit 2 is also the environment-error code, and the README warns that the record may already be written in that case. The hook then:

- logs the command a second time, in another Bot's chain;
- stores the detail `GBLOG_BOT=<x> was refused`, which is untrue in that case.

The hook discards stderr, so it cannot tell the cases apart today.

#### Z-09 (M) — Two task-id scanners that disagree

Confidence: high.

| | `_task_start_seen` (used by `task-end`) | `_task_status` (used by `log`, `note`, `approval`, `archive`, `task-start`) |
|---|---|---|
| Id compared | raw `--task-id` | `redact(task_id)`, as stored |
| Method | `iter_records`, parses every line | raw scan, parses lines that contain the id |
| Detects "already closed" | no | yes |
| Warning text | `task-end for task '<id>' has no task-start …` | `task '<id>' has no task-start … (check --task-id / $GBLOG_TASK)` |

Consequences:

- `task-end` is now the only logging command that cannot see a double close.
- For an id that redaction changes, `task-end` warns falsely.
- Every logging command with a task id reads today's day file in full, and up to 30 days of files for an unknown id. That is fine at normal volumes, but DESIGN §13 still states that `append` "does not grow with N" and costs "1–3 ms".

#### Z-10 (M) — Three rules for a non-regular side file, and the docs state a fourth

Confidence: high (tests pin each behaviour).

| Entry | `verify` today | `append` today |
|---|---|---|
| FIFO at `state/<bot>.json` | `!` warning ("missing or unreadable") | adopts the tail, replaces the FIFO |
| directory at `state/<bot>.json` | problem (FAIL) | refused, spooled, exit 4 |
| any non-regular `anchors/<bot>.json` | problem (FAIL) | – |
| non-regular `state/<bot>.lock` | problem (FAIL) | exit 2 |

README ("How it works"), DESIGN §2, §4 and §7, SECURITY.md ("Guaranteed") and the CHANGELOG all say a non-regular state file or `anchors/<bot>.json` "counts as unreadable (a `!` warning, not a FAIL)". DESIGN §4 also says a state path that is not a regular file is adopted like a missing one, which is no longer true for a directory. Deleting `anchors/<bot>.json` is silent while a FIFO there is a FAIL; that is defensible, but it should be written down.

#### Z-11 (M) — Duplicated lost-chain logic and FAIL-entry shapes; synthetic names undocumented

Confidence: high.

- The candidate computation (name as given, slug, each registered variant → `gone` → `missing_chains()`) exists twice: inline in the `verify` branch of `_main`, and as `Ledger.lost_chains_of` (written for `anchor --bot`). They are identical today and will drift.
- Four hand-written "failed chain" dicts exist:
  - `_verify_guarded` (16 keys copied from `Ledger.verify`'s result);
  - `_verify_missing`;
  - the "no chain" entry in `_anchor`;
  - the "anchor aborted" entry in `_anchor`.
- `verify --all --json` and `anchor` can now emit entries whose `bot` is `(witness files)` or `(day rollup)`, with `"missing": true` on the former. DESIGN §7 defines `bot` as a chain name and `missing` as "no chain on disk". A consumer that builds the "Chain heads" block from these entries gets a pseudo-chain.

#### Z-12 (M) — The test suite misdescribes itself

Confidence: high.

1. `BrowserWindowTest.test_hours_zero_is_a_usage_error` (`expectedFailure`, listed in `PENDING.txt`) asserts that `--hours=0` exits 2. README and CHANGELOG now say only a negative value exits 2 and that "`--hours 0` imports nothing on a first run". The pending test is waiting for behaviour the project has decided against.
2. `DocumentedButAbsentTest` in `test_mutation_gaps.py` is described as "Each test fails today", and every method carries a `# bug: D-n …` comment. None is marked `expectedFailure`, and all of them pass. `InternalErrorGapTest.test_debug_env_reraises_from_verify_all` has the same stale comment.
3. `SyncGapTest.test_sync_cache_does_not_hide_a_changed_file` exercises the dead cache branch (Z-02).
4. `_support.run` renders an escaped exception as a traceback, but `main()` converts every exception into `error: internal error` with exit 1. Assertions of the form `assertNotIn("Traceback", err)` therefore prove little. `test_mutation_gaps.py` works around this with a local stricter `run`.
5. `SyncDeepTest.test_no_repo_origin_is_never_pushed` hides two passing sub-cases behind one expected failure (Z-05).

#### Z-13 (M) — `agent-integration.md` and DESIGN §4 contradict the code

Confidence: high.

- §3: "`--task` on `archive` is not stored either, and prints no warning". The code prints `warning: --task is not stored on archive records`.
- §3: `--decision` "is one of …" with no synonyms, and `--action` with underscore only. The code accepts `approved`, `rejected` and others, and `computer-use`.
- §6 (stderr lines) lacks every line added today:
  - `warning: task '<id>' has no task-start …`;
  - `warning: task '<id>' is already closed …`;
  - `warning: task id '<id>' was already used …`;
  - `warning: task-start without --task …`;
  - `warning: no chain on disk for '<name>'; nothing to show …`;
  - `notice: --all given; --bot <x> is ignored`;
  - `note: spooled event(s) are pending in chain(s) …`.
- §8 and DESIGN §4 step 3 give the spool file name as `<UTC stamp>-<8 hex>.json`. The code and DESIGN §3 use `<stamp>-<nnnn>-<id>.json`. DESIGN step 3 also lists "spool, then alert"; the code writes the alert first.
- `status --json` rows have `open_task_list`; no document mentions it.
- FAQ 4, DESIGN §4 and the CHANGELOG quote the seq-mismatch warning as "…until gblog state rebuild". The code prints "…until `gblog state rebuild` is run".

#### Z-14 (L) — `_open_regular` is applied unevenly

These still use a type test followed by a plain `open()`:

- `_anchor_snapshot` (history);
- `orphan_witnesses` (history);
- `_witness_present`;
- `_clamped_candidates`;
- `tune_history`;
- `_spool_replayed`;
- `_gunzip_sha256` (through `gzip.open`);
- `_is_sqlite_file`.

`log_files()` filters with `isfile`, so only the swap race remains. SECURITY.md lists that race as a known limit for all readers, although half of them are already safe.

#### Z-15 (L) — Redaction rules disagree on the keyword list

- The env-style, JSON-key, dict-repr, prefixed-flag and glued rules use `token|secret|password|passwd|pwd|api_key|apikey|access_key|private_key`.
- The plain flag rule uses `password|passwd|token|secret|api-key`.
- The older assignment rule has its own shorter list.

So `--db-pwd X` and `--secret-access-key X` are redacted, while `--pwd X` and `--access-key X` are not. SECURITY.md lists only the five plain flags, so this is not a documented guarantee, but it is surprising.

#### Z-16 (L) — "Every change of `origin` is recorded" has an exception; alias changes are never recorded

- `_bookkeep` swallows `TailMismatchError`. If the sync Bot's chain has a tail mismatch, `origin` is changed and the record exists only in `spool/`. The `except BaseException: _restore_origin` path, and the docs, say the change is undone when it cannot be recorded.
- `--bot` lets the caller choose which chain receives the record.
- `alias add --move`, `alias remove` and `alias strict off` change where future records land and leave no ledger record. The shell hook skips them as plain `gblog` commands. The only trace is `state/aliases.json` in git history.

#### Z-17 (L) — `umask 077` is wider than its purpose

`_git` sets the process umask for every git call, including `git push`. For a local-path remote, the receiving side runs under that umask, so a group-shared bare backup repository receives objects only the owner can read. The umask change is also process-global, which matters for library callers with threads.

#### Z-18 (L) — Hook update and bind-mounted rc files

`_install_shell_hook` now replaces the rc file with `os.replace`. A single-file bind mount of `~/.bashrc`, common in containers, makes that fail (`EBUSY`), so the command exits 2 where the old in-place rewrite worked.

Also note: the hook text changed (K1-07) while the marker stayed `# gblog hook v2`. That is acceptable only because 0.6.1 is unreleased.

#### Z-19 (L) — Message inconsistencies

- The same condition has two wordings (Z-09).
- The `task-end` warning and the `_resolve_bot` warning print the user-supplied name without `_term`. Every newer warning neutralises control characters.
- `anchor` prints `      - <problem>` lines for "anchor aborted" and missing chains only. A chain that FAILs for another reason gets no reason line.
- `anchor --bot <unknown>` answers in `verify` format (`FAIL  <name>  0 records`), not `ANCHOR FAIL`.

#### Z-20, Z-21 (L) — Small doc contradictions

- `skills/bot-tune/SKILL.md` step 1 gives `gblog capture browser --bot <bot>` and `gblog capture files --bot <bot>`. `docs/routine-weekly-tune.md` and the CHANGELOG require `--hours 168` and `--exclude transcripts`.
- SECURITY.md says `--remote` itself must not appear in a routine. README "Upgrading" item 5 says a routine that passes the same `--remote` every run keeps working, and `examples/routine-daily-audit.sh` does exactly that.

#### Z-22 (L) — Scripts and CI

- `examples/routine-daily-audit.sh` runs `gblog anchor` without `--skip-archives`. The flag exists now; DESIGN §13 still documents the double re-hash as a known cost.
- CI runs shellcheck on `install.sh` and `bin/gblog` only. The example scripts are linted only if the test job's image happens to have shellcheck.
- `tests/bench_large_ledger.py` is byte-compiled but never run, and DESIGN §13 points readers at it.
- The CHANGELOG check interpolates the version into a regular expression, so the dots match any character.

#### Z-23 (L) — Size and complexity hotspots

`gblog.py` is roughly 3,900 lines. The functions most worth simplifying, largest first:

- `_main`: about 470 lines, one `if args.cmd == …` chain.
- `Ledger.verify`: about 230 lines.
- `analyze`: about 230 lines.
- `sync`: about 170 lines, six concerns in one function.
- `capture_files` and `capture_browser`.
- `_layout_findings`: seven hand-rolled "first 20, then N more" counters.

Repeated idioms:

- `os.path.relpath(x, self.root).replace(os.sep, "/")`, about twelve times.
- The literal "symbolic link (gblog does not write through one)", four times.
- The compact UTC stamp expression, twice.
- git is invoked three ways: `_git`, and bare `subprocess.run` in `_sync_ignored` and `_sync_drift`.
- Local imports (`gzip`, `zlib`, `urllib.parse`) are repeated in about nine functions, and `import stat as _stat` shadows the module-level import.
- The reserved-name rule is written out again inside `orphan_witnesses`.

Unused outside tests: `Ledger._tail_head`, `Ledger.chain_hashes`, `_GITHUB_REMOTE_RE`, `install_shell_hook`.

Fragile: `_add_root_everywhere` relies on private argparse attributes.

Stale: the `_anchor_snapshot` docstring describes a 3-tuple and the function returns five items; `_anchor_checks` unpacks a list of problems into a variable named `hist_problem`.

### 3. Fix work items

Common constraints for every item: stdlib only; Python 3.8+; no change to the record format, the hash rule or `tests/fixtures/`; existing stdout lines and exit codes stay unless the item says otherwise; new stderr lines are allowed. Each item can be done on its own.

#### G1 — `gblog.py`

**G1-01 (Z-03) One lower bound for the next timestamp, valid after a non-normalised tail.**
- Add `_tail_floor(tail) -> Optional[str]`:
  - return the tail `ts` when it is normalised;
  - otherwise return the larger of (a) `parse_ts(raw)` when it parses, and (b) the relabelled value `raw[:10] + "T" + raw[11:19] + "Z"` when that is a valid normalised timestamp;
  - return `None` when neither exists.
- Why (b) is safe: the relabelled value is text-greater than every raw form that shares its first 19 characters (`Z` sorts after `+`, `-`, `.`, end of string, and `T` after a space), and it has the same date as the tail's day file. Any `ts` at or above the floor therefore passes `verify`'s text comparison and lands in the same or a later day file.
- Use the helper in `Ledger.append`, replacing `if not _is_norm_ts(tail_ts): tail_ts = None`, and in the pre-check of `archive_file`, which duplicates the bound.
- The clamp, `clock_regressed`, the future-tail warning and `_past_ts_error` stay as they are.
- Acceptance: a chain whose only record has `ts` `2026-11-17T08:00:00+09:00`, with "now" mocked to `2026-11-16T23:30:00Z`:
  - the append is stamped `2026-11-17T08:00:00Z`, carries `clock_regressed`, and is written to `2026-11-17.jsonl`;
  - `verify` is OK, and `--strict` reports only the old line;
  - an explicit `--ts 2026-11-16T23:30:00Z` exits 2.
- Repeat the acceptance for a space separator, a fractional second and a negative offset.
- Then remove the D1-09/K3-01 limit from README, DESIGN §4 and the CHANGELOG.

**G1-02 (Z-04) `spool replay` must never create a second copy of an event.**
- Give `Ledger.append` a private keyword, for example `_spooled_from: Optional[str] = None`, and pass it to `_refuse_tail`. When it is set, `_refuse_tail` writes neither a spool file nor an alert, and raises `TailMismatchError` with `spool_path` set to the existing file.
- In `spool_replay`, pass the current path and catch `TailMismatchError`: set `refused = True` and `break`.
- The CLI path for `refused` already prints the right line and exits 4.
- Acceptance: one spooled event and a directory planted at today's day file. Running `spool replay` twice leaves exactly one pending file; after removing the directory, a third run appends the event once.

**G1-03 (Z-05) One definition of "origin is configured".**
- In the push branch of `sync()`, read `git config --local --get-all remote.origin.url` first; it is the test the `--remote` branch already uses. When it is empty, take the existing "no origin configured in the ledger repository" path.
- Only otherwise use `remote get-url --push --all origin` for the URL list. Keep it, because it applies `insteadOf`.
- Acceptance: the three sub-cases of the pending test, split into three tests, all pass without `expectedFailure`.

**G1-04 (Z-06) Make `_disk_name` exact.**
- Keep the swap-case probe as the cheap pre-filter.
- Return `bot` unless `logs/<bot>` is itself a directory and `os.path.samefile(logs/<bot>, logs/<other>)` is true. Only then list `logs/` for the stored spelling.
- Acceptance on a case-sensitive filesystem: with only `logs/OPS` present, `note --bot ops` creates `logs/ops` and `verify --bot ops` no longer verifies `OPS`. Keep `test_disk_name_unit` passing by patching `samefile` as well.

**G1-05 (Z-02) Remove the dead sync cache.**
- Delete `_SYNC_CACHE` and `_write_sync_cache`.
- Delete the `cache_path` parameter and the cache branch of `_sync_drift`.
- No behaviour change. Pair with G2-01 (remove the cache test) and G3-03.

**G1-06 (Z-07) Isolate per-chain bookkeeping in `sync`.**
- Wrap the `update_state` call of the stamp loop in `try/except OSError`.
- Collect failures in `result["stamp_errors"]` and print `warning: last_sync not recorded for <chain> (<class>)` on stderr.
- The exit code stays what the commit and push earned.
- Acceptance: a directory at `state/aaa.json` with a local bare remote; `sync` prints `commit … pushed`, exits 0, and stamps `zzz`.

**G1-07 (Z-08) Fall back only for a refused name.**
- In `SHELL_HOOK`, capture the first call's stderr: `err=$(gblog log … 2>&1 >/dev/null) || st=$?`.
- Run the fallback only when `st` is 2 and `err` matches `*"is not registered"*`, `*"is reserved"*` or `*"must contain letters or digits"*`.
- Keep shellcheck clean and keep the `# gblog hook v2` line.
- Acceptance: extend `HookFallbackTest` with a fake that exits 2 and prints `error: No space left on device`; there must be no second call.

**G1-08 (Z-09) One task-id scanner.**
- Delete `_task_start_seen`.
- `task-end` calls `_task_status` before its append:
  - `unknown` prints the existing `task-end for task …` text unchanged, because tests match it;
  - `closed` prints a new line: `warning: task '<id>' is already closed; recorded anyway`.
- In `_task_status`, parse a line only when it contains the id needle and one of `"task_start"` / `"task_end"`.
- Acceptance: a double `task-end` warns, and an id that redaction rewrites does not false-warn.

**G1-09 (Z-10, optional; needs a maintainer decision) One helper for side-file type.**
- Add `_side_kind(path)` returning `missing`, `regular`, `link` or `other`.
- Use it in `_file_findings`, `_anchor_snapshot` and `_append_head`.
- Do not change today's verdicts in this item; the purpose is to keep the three call sites from drifting. Changing the FIFO-at-state verdict would break existing tests and needs its own decision.

**G1-10 (Z-11) Remove the duplicated selection and entry shapes.**
- The `verify` branch calls `ledger.lost_chains_of(args.bot, bots)` in place of its inline copy.
- Add `_fail_entry(bot, problems, **extra)` and use it for the four FAIL dicts.
- Output must be byte-identical; the existing tests are the acceptance.

**G1-11 (Z-14) Use `_open_regular` in the remaining readers** listed under Z-14. For archives, open with `_open_regular` and pass the handle to `gzip.GzipFile(fileobj=…)`. Mechanical, with no output change.

**G1-12 (Z-15) One keyword source for redaction.**
- Build the alternations from one constant.
- Extend the plain flag rule to `pwd` and `access[_-]?key`.
- No pattern may be removed; every replacement stays idempotent.
- Acceptance: `test_redaction_extended.py` passes unchanged, plus new cases for `--pwd X` and `--access-key X`.

**G1-13 (Z-16) Keep the "recorded or undone" promise.**
- For the origin-change record only, treat `TailMismatchError` like any other failure: restore `origin` and re-raise, giving exit 4.
- Leave alias-change records as a documented gap (G3-05). Writing them changes where records land and needs operator sign-off.

**G1-14 (Z-17) Narrow the umask** to `init`, `add`, `commit` and `rm --cached`, for example through a keyword on `_git`. `push`, `remote` and `config` run with the caller's umask.

**G1-15 (Z-18) Rc-file fallback.** When the atomic replace in `_install_shell_hook` fails with `EBUSY`, `EXDEV` or `EPERM`, fall back to the previous in-place write and return `updated`.

**G1-16 (Z-19) Message hygiene.**
- Pass the name through `_term` in the `task-end` and `_resolve_bot` warnings.
- Print the first problem as a `      - …` line under every `ANCHOR FAIL`. This adds stdout lines under FAIL only; mention it in the CHANGELOG.

**G1-17 (Z-23) Mechanical simplification, with no behaviour change.**
- Add `Ledger._rel(path)`, a constant for the symlink text, and a `_capped(list, text)` helper for `_layout_findings`.
- Move `_main` to one `_cmd_<name>(ledger, args, out)` function per subcommand, in the same file.
- Split `sync()` into `_sync_set_remote`, `_sync_commit`, `_sync_check_push` and `_sync_stamp`.
- Fix the `_anchor_snapshot` docstring and the `hist_problem` name.
- Drop the shadowing `import stat as _stat`.
- Do this last, in its own commit.

#### G2 — tests

**G2-01 (Z-12) Make the pending list truthful.**
- Replace `test_hours_zero_is_a_usage_error` with a passing test: `--hours=0` exits 0 and imports nothing on a first run. Remove its `PENDING.txt` line.
- Split `test_no_repo_origin_is_never_pushed` into three tests. After G1-03 none is pending; before it, only the remotes-file case is.
- Delete `test_sync_cache_does_not_hide_a_changed_file` together with G1-05.

**G2-02 (Z-12) Rename and re-comment.**
- Rename `DocumentedButAbsentTest` to `DocumentedBehaviourTest`.
- Replace each `# bug: D-n …` comment with `# fixed: D-n`.
- Fix the class docstring and the comment above `test_debug_env_reraises_from_verify_all`.

**G2-03 (Z-12) A strict default runner.**
- Move the check from `test_mutation_gaps.run` into `_support.run` behind `internal_ok=False`: fail on `Traceback`, on `internal error` in stderr and on `verify aborted` in stdout.
- Opt out explicitly in the few tests that provoke those on purpose.

**G2-04 Regression tests for G1-01 … G1-08.** One class per item, each using the acceptance scenario in the item. None exists today.

**G2-05 A doc-drift guard.**
- For every `gblog <subcommand> … --flag` found in the fenced code blocks and inline code of `README.md`, `docs/*.md`, `skills/*/SKILL.md` and `examples/README.md`, assert that the flag is in that subcommand's parser from `build_parser()`.
- For every `warning:` / `notice:` line quoted in `agent-integration.md` §6, assert that its fixed prefix occurs in `gblog.py`.
- This would have caught D-1 and Z-13.

#### G3 — docs

**G3-01 (Z-01) CHANGELOG.**
- Replace the "Specialist reviews (K1 … K4)" text with a list of what is in the code, using the Z-01 table.
- Delete "They changed documentation only".
- Rebuild "Known open findings" from the still-open list under Z-01, re-checking each line against the code.
- Add the new behaviour under "Behaviour changes":
  - the reader warning for an unknown `--bot`;
  - the task-id warnings;
  - `anchor --bot` exiting 1 for a lost chain;
  - a directory at a state path giving exit 4 and a `verify` FAIL;
  - a non-regular `anchors/<bot>.json` being a FAIL;
  - the `(witness files)` and `(day rollup)` entries;
  - the `--decision` and `--action` synonyms;
  - `open_task_list`;
  - the hook fallback.

**G3-02 (Z-01) SECURITY.md, DESIGN, README.**
- SECURITY "Known limits" table: keep only rows that are still true. As far as I can tell these are an unbounded log line, the two-run races K2-09 and K2-14, and K2-13.
- Fix "A lock file is not covered yet" and the matching sentences in DESIGN §2.
- DESIGN §13: remove the K4-03 quadratic row and the FIFO-lock row.
- README "Known limits of this upgrade": narrow the letter-case item to what `_disk_name` still leaves open.

**G3-03 (Z-02) Sync cache.** Remove every statement that a file "is not hashed again" (CHANGELOG D3-05, two places in SECURITY, the DESIGN §13 row and "Kept as they are", FAQ 35). State the real cost: every evidence file is hashed on every run.

**G3-04 (Z-10) Side-file rules.** Put the four-row table from Z-10 into DESIGN §7. Correct the "counts as unreadable (a `!` warning)" sentences in README, DESIGN §2 and §4, SECURITY and the README upgrade table.

**G3-05 (Z-11, Z-16) DESIGN §7 and SECURITY.**
- Document the synthetic entries `(witness files)` and `(day rollup)`, and say that routines must skip names in parentheses when they build the "Chain heads" block.
- Under "Not guaranteed", state that alias-registry changes are not recorded in any chain.

**G3-06 (Z-13, Z-19) `agent-integration.md`.**
- §3: decision and action synonyms; the `--task` warning on `archive`.
- §6: add the seven new stderr lines.
- §8 and DESIGN §4 step 3: the spool file name, and the alert-then-spool order.
- Document `open_task_list`.
- Quote the seq-mismatch warning exactly in FAQ 4, DESIGN §4 and the CHANGELOG.
- DESIGN §13: add the per-command task-id scan to the `append` row.

**G3-07 (Z-20, Z-21).**
- Give the `bot-tune` skill the same capture commands as the weekly routine.
- In SECURITY.md, say that `--remote` with a fixed, operator-chosen URL in a scheduler is acceptable, and that `--replace-remote` and the `--allow-*` flags never are.

#### G4 — scripts and CI

**G4-01** `examples/routine-daily-audit.sh`: change `run anchor anchor` to `run anchor anchor --skip-archives`. Update the stub-call expectations in `tests/test_examples.py`, the header comment, `examples/README.md` step 3, and the DESIGN §13 sentence about the double re-hash.

**G4-02** CI `scripts` job: `shellcheck install.sh bin/gblog examples/*.sh`.

**G4-03** CI `test` job (3.12, ubuntu): `python tests/bench_large_ledger.py --records 500 --appends 5`, so the benchmark DESIGN §13 points at cannot rot.

**G4-04** CI CHANGELOG step: match the version as a fixed string, for example `grep -qF "## [${ver}]" CHANGELOG.md`.

### 4. What not to change

- The `break_ack` pin, the hash rule, canonical JSON, and the meaning of exit codes 0–4.
- The verdicts of the Z-10 table, unless a maintainer decides otherwise; tests pin them.
- `# gblog hook v2`; the README and tests reference it and 0.6.1 is unreleased.
- `tests/fixtures/`.