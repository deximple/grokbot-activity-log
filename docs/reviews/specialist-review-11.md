# Specialist review K11 — 2026-11-16

Area: code structure and maintainability of `gblog.py` (dead code, duplicated helpers, long functions, naming, refactor plan).

I read the current tree; I did not run it. Every "reproduce" below was derived from the code and still has to be confirmed by running it. Line counts are approximate (marked ≈). "No caller" means a text search of `gblog.py` and `tests/` as given to me.

`gblog.py` works, but its structure has started to produce defects. I found:

- one user-visible contradiction (K11-01);
- one integrity-relevant structural hazard in `verify` (K11-02);
- about 300 lines of test-only or dead code in the installed file (K11-03, K11-04, K11-09);
- several facts written 3 to 14 times, three of which have already drifted apart (K11-03, K11-06, K11-10).

Section 3 is a refactor plan in small commits. Each commit has a checkpoint that proves output, exit codes and on-disk bytes did not change. The file stays a single stdlib-only script. Round-4 items (G1-01 … G1-12) are not repeated.

## 1. Priorities

| Id | Priority | Finding in one line |
|---|---|---|
| K11-01 | P1 | Two `--hours` guards contradict each other, the documents and one test |
| K11-02 | P1 | The chain walk of `verify` runs as a side effect of the layout checker |
| K11-03 | P1 | `_verify_reference` and `_chain_snapshot` (≈270 lines, test-only) ship in the tool and have drifted |
| K11-04 | P1 | The sync verification cache is dead code with a comment that says it is in use |
| K11-05 | P1 | Two redaction rules are swapped in at import time by matching regex source prefixes |
| K11-06 | P2 | `_append_head` returns half its result through a list; the refusal path redacts under the lock |
| K11-07 | P2 | `_main` is one ≈550-line `if` chain; it hides a date bug and several duplicate renderings |
| K11-08 | P2 | Two ways to run git; the raw one skips the hardening flags |
| K11-09 | P2 | Dead and test-only members, unreachable branches |
| K11-10 | P2 | "Group records by task" is implemented five times with three different answers |
| K11-11 | P2 | Reader predicates, sort keys and raw-scan loops are copied four to five times |
| K11-12 | P2 | Ledger layout constants are written two to six times |
| K11-13 | P2 | Small helpers that exist once and are re-implemented elsewhere |
| K11-14 | P3 | Long functions and their extraction targets |
| K11-15 | P3 | Hidden inputs: option attributes on `Ledger`, result dict built in four places |
| K11-16 | P3 | Three ways to report a usage error; library code exits the process |
| K11-17 | P3 | Naming: one name with two meanings, two names for one thing |
| K11-18 | P3 | Imports, module layout, stale docstrings |
| K11-19 | P3 | Three clock sources; only one can be frozen in tests |
| K11-20 | P3 | `--root` is added through private argparse attributes; `_add_common` gives flags to commands that ignore them |
| K11-21 | P3 | "Binary content" is decided by two predicates that disagree by byte offset |

## 2. Findings

### K11-01 (P1) — Two `--hours` guards contradict each other

**Where.** `_validate_args`:

```python
    if args.cmd == "capture" and args.what == "browser" and args.hours < 0:
        raise _UsageError("--hours must be 0 or more")
    if args.cmd == "capture" and args.what == "browser" and args.hours < 1:
        raise _UsageError("--hours must be 1 or more (a window of 0 hours imports nothing)")
```

**What.** The first message says 0 is allowed; the second guard refuses 0. The documents and tests disagree as well:

- README, FAQ 28, `docs/DESIGN.md` §11 and the CHANGELOG say only a negative value exits 2. The CHANGELOG even records "refused when it is negative, not when it is below 1".
- `capture_browser` clamps with `hours = max(0, hours)`.
- `BrowserWindowTest.test_hours_zero_is_a_usage_error` pins exit 2 for 0. Its comment still calls that a bug in the opposite direction.
- `CliLayerTest.test_capture_browser_negative_hours_is_usage_error` pins the text `--hours must be 0 or more`.

**Reproduce.**

```
gblog capture browser --bot ops --hours=-1   # error: --hours must be 0 or more          (exit 2)
gblog capture browser --bot ops --hours=0    # error: --hours must be 1 or more (...)    (exit 2)
```

**Fix.** Keep one guard. This needs a maintainer decision; I recommend option A, because four documents and a CHANGELOG correction describe it as deliberate.

- **A (follow the documents).** Delete the second guard. `--hours 0` exits 0, prints `browser: 0 new visit(s) from 1 history file(s)` and stores the marker.
- **B (follow the test).** Keep only `args.hours < 1` with one message. Correct README, FAQ 28, DESIGN §11, the two CHANGELOG lines and the negative-hours test.

**Tests.**

- Option A: replace `test_hours_zero_is_a_usage_error` with `test_hours_zero_imports_nothing` (exit 0, the output line above, `capture_browser_last` has an entry for the history file, a second run adds nothing).
- Either option: one test asserting that `--hours=-1` and `--hours=0` never print two different lower bounds.

### K11-02 (P1) — The chain walk runs as a side effect of the layout checker

**Where.** `Ledger.verify`, `_layout_findings`, `Ledger._snapshot_lines`.

```python
        layout_warnings, noncanonical = _layout_findings(bot, feed(), log_names, strict, dup_lines, own)
        if not stopped and n_items > seg_start:
            close(seg_first, prev_item, seg_start, n_items - 1)
```

**What.** `feed()` is a generator that performs the hash walk, collects break_acks, archive records and `state_adopted` markers, and then yields the line. The only consumer is `_layout_findings`, a helper for warnings. The integrity result therefore depends on that helper iterating to the end. Two more couplings are implicit:

- `_snapshot_lines` must add `(rel, lineno)` to `dup_lines` before it yields the line, because `_layout_findings` looks the current line up in that set.
- `_layout_findings` copies `dup_lines` when it is not a `set` (`dup_lines = set(dup_lines)`). The lazy protocol only works because the caller passes a real `set`.

This is a hazard, not a present bug: today the loop in `_layout_findings` has no early exit.

**Reproduce.** This shows the dependency, not a defect in the shipped code path:

```python
ledger.append("ops", ...)  # three records, then remove state/ops.json
with mock.patch.object(gblog, "_layout_findings", return_value=([], [])):
    res = ledger.verify("ops")
# res["ok"] is True, res["records"] == 0, res["head"] == "genesis", no warning
```

A future `break` or exception inside the layout loop has the same effect on a chain whose state file is missing.

**Fix.** Drive the loop in `verify` and make the three consumers objects with `feed()` and `finish()`. The duplicate-key flag travels with the line.

```python
walk, scan = _ChainWalk(), _VerifyScan(bot, own, needed, skip_archives, pinned_bot, temp_copy)
layout = _LayoutCheck(bot, log_names, strict, own)
for i, (rel, lineno, rec, text, dup) in enumerate(self._snapshot_lines(extents, strict, snap_problems)):
    scan.feed(i, rel, lineno, rec)
    walk.feed(i, rel, lineno, rec)
    layout.feed(rel, lineno, rec, text, dup)
layout_warnings, noncanonical = layout.finish()
```

- `_ChainWalk` holds what are now 11 `nonlocal` names in the closures `close`, `stop`, `step` and `feed`.
- Keep `_layout_findings(...)` as a thin wrapper over `_LayoutCheck` until K11-03 is done; the reference implementation calls it.

**Tests.**

- `test_chain_walk_does_not_depend_on_the_layout_checker`: the reproduction above, asserting `records == 3`. It fails today and passes after the fix.
- The existing differential tests (`StreamingVerifyTest._same`) and the 300-mutation fuzz are the checkpoint. Run both before K11-03 removes the oracle.

### K11-03 (P1) — The in-file oracle ships with the tool and has drifted

**Where.** `Ledger._verify_reference` (≈225 lines) and `Ledger._chain_snapshot` (≈35 lines).

```python
    # The whole-list implementation that ``verify`` replaced (K4-02). gblog does not call it; it is kept as the
    # oracle of the differential tests in tests/test_k3_k4.py.
    def _verify_reference(self, bot: str, strict: Optional[bool] = None, skip_archives: Optional[bool] = None) -> Dict:
```

**What.** Both are test-only, yet `install.sh` installs them as part of `~/.local/bin/gblog`. Every message of `verify` exists twice. The copies have already diverged; the reference never received the K3-03 change:

```python
        layout_warnings, noncanonical = _layout_findings(bot, snap_lines, log_names, strict, dup_lines)   # no `own`
        ...
                          if isinstance(rec.get("hash"), str) and rec.get("bot") == bot}                  # not own()
```

**Reproduce.** Use the setup of `CaseVariantChainTest` (chain directory `Ops`, records with `bot: "ops"`, `os.path.samefile` patched to `True`):

```python
dict(ledger.verify("Ops")) == dict(ledger._verify_reference("Ops"))   # False: the reference adds two
                                                                      # "bot field 'ops' does not match chain 'Ops'" warnings
```

**Fix.** Do this after K11-02; the oracle is the checkpoint for that refactor.

1. Move both functions to `tests/_reference.py` as `verify_reference(ledger, bot, ...)` and `chain_snapshot(ledger, bot, ...)`.
2. Pass `own=ledger._own_name(bot)` to the layout check and use it for `own_hashes`.
3. Update the callers:
   - `tests/test_k3_k4.py`: `_same` and `test_verify_does_not_keep_the_parsed_chain`;
   - `tests/test_round4.py`: `test_one_behind_state_gets_a_hint`;
   - `tests/test_round3_followups.py`: `test_snapshot_keeps_text_only_on_request`.
4. Delete the two methods and the sentence about `_verify_reference` in the `verify` docstring.

**Tests.**

- `_same` also runs on the case-variant chain, so the oracle covers K3-03.
- `DeadCodeRemovalTest.test_removed_names_are_gone` gains `Ledger._verify_reference` and `Ledger._chain_snapshot`.

### K11-04 (P1) — The sync cache is dead code that claims to be in use

**Where.** `_SYNC_CACHE`, `_write_sync_cache`, the `cache_path` parameter of `_sync_drift`.

```python
_SYNC_CACHE = "gblog-sync-cache.json"  # lives under .git/: never committed and not part of the ledger tree
...
    # ``cache_path`` (sync passes .git/gblog-sync-cache.json) maps path -> {blob, size, mtime_ns} for the
```

`sync` calls `_sync_drift(root, checked)`; its own comment explains why there must be no cache.

**What.**

- The constant has no reader.
- `_write_sync_cache` and the cache branch run only from `test_sync_cache_does_not_hide_a_changed_file`.
- The comment in `_sync_drift` states the opposite of what `sync` does.
- CHANGELOG (D3-05) and `SECURITY.md` say a cache under `.git/` could be written by whoever the check is meant to catch. Code that is one argument away from re-enabling it should not stay.

**Reproduce.** `grep -n "cache" gblog.py` shows the constant, the helper and the branch. `grep -n "_sync_drift(" gblog.py` shows the single call without a cache path.

**Fix.**

1. Delete `_SYNC_CACHE` and `_write_sync_cache`.
2. Reduce the signature to `_sync_drift(root, checked)`.
3. In the loop keep the size rules and replace the cache test with `ok = _git_blob_id(full, size, len(sha)) == sha`.

**Tests.**

- Rewrite `test_sync_cache_does_not_hide_a_changed_file` as `test_sync_drift_rehashes_every_file`. Keep the same-size rewrite and the assertion `spy.call_count == len(checked)`; drop the cache file.
- `test_sync_keeps_no_verification_cache` stays.
- Add `_write_sync_cache` and `_SYNC_CACHE` to `test_removed_names_are_gone`.

### K11-05 (P1) — Redaction rules are replaced at import time by regex-source prefix

**Where.** Module level, after `_LinearRule`:

```python
for _i, (_pat, _repl) in enumerate(_SECRET_PATTERNS):
    _src = getattr(_pat, "pattern", "")
    if _src.startswith(r"\beyJ"):
        _SECRET_PATTERNS[_i] = (_LinearRule(_redact_jwt, (_pat, _repl)), None)
    elif _src.startswith(r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-]){0,8}"):
        _SECRET_PATTERNS[_i] = (_LinearRule(_redact_env_names, (_pat, _repl)), None)
```

**What.**

- The quadratic-time fix (K4-04) is applied only if two regex literals still start with these exact characters. An edit to either literal skips the swap silently and the slow expression runs again. Only one test (`LinearRedactionTest._rules`) would notice; nothing fails at import.
- The comment on `_SECRET_PATTERNS` describes the two expressions as live rules.
- The loop leaves `_i`, `_pat`, `_repl` and `_src` as module globals.
- Redaction is spread over three places: the list near the top, the linear rules under the "utils" banner, and `_redact_pem` / `redact` about 500 lines later.
- The secret-word alternation is written out in six rules, in two different orders. Four more rules carry subsets. `_ENV_KW` is a seventh copy. Finding R-12 was exactly a drift between such copies.

**Reproduce.** Change `\beyJ` to `(?:\b)eyJ` in the JWT entry. `python -c "import gblog"` succeeds, and `gblog.redact("eyJaaaaaaaaaa-" * 50000)` no longer returns promptly.

**Fix.**

1. Move the K4-04 block (`_ENV_*`, `_JWT_*`, `_redact_env_names`, `_redact_jwt`, `_LinearRule`) above the list. This is a pure move.
2. Name the two former pairs `_JWT_FORMER` and `_ENV_FORMER`. Write the list entries explicitly as `(_LinearRule(_redact_jwt, _JWT_FORMER), None)` and `(_LinearRule(_redact_env_names, _ENV_FORMER), None)`.
3. Delete the loop.
4. Build the alternations from named constants (`_KW_ALL`, `_KW_JSON`, `_KW_FLAG`, …) so that every composed pattern source is character-identical to today's literal.
5. Put `_redact_pem` and `redact` directly after the list.

**Tests.**

- `test_pattern_sources_are_unchanged`: a list in the test module with the sha256 of each of the 27 sources (`pat.pattern`, or `pat.former[0].pattern` for a linear rule), in order.
- `test_exactly_two_linear_rules_at_fixed_positions`: indexes and function names.
- The existing differential tests against `former` stay.

### K11-06 (P2) — `_append_head` out-parameter; refusal path redacts under the lock

**Where.** `Ledger._append_head`, `Ledger.append`, `Ledger._refuse_tail`.

```python
        if seen is not None:
            seen.append(raw)  # handed to append(), which updates and writes it within the same lock hold
            seen.append(path)  # the tail's day file: append() derives the timestamp floor from it (K3-01)
```

```python
            tail_ts = _ts_floor(tail, seen[1] if len(seen) > 1 else None)
            prior = seen[0] if seen else None
            state = seen[0] if seen else self._load_state(bot)
```

**What.**

- The function returns a 5-tuple and appends two unrelated values to a caller-supplied list. The list is annotated `List[Optional[Dict]]`, but its second item is a path.
- Both appends happen before any `return`, so the three fallbacks above are unreachable.
- `_tail_ok` reads the result by index: `self._append_head(safe_name(bot))[3]`.
- `append` redacts before it takes the lock (K4-04). `_refuse_tail` redacts the raw record a second time, inside the lock, with its own copy of the reserved-key tuple:

```python
            if key in ("v", "seq", "bot", "prev", "hash", "_corrupt") or value is None or value == "":
                continue
            safe_record[key] = _stored_value(record, key, value)
```

**Reproduce.** Take `LinearRedactionTest.test_append_redacts_before_it_takes_the_lock` and break state first (`write_state("ops", {"seq": 99, "head": "cd" * 32})`). The probe then records `False`: `redact` runs while the bot lock is held.

**Fix.**

- Return a `NamedTuple`: `_Head(seq, head, tail, tail_path, state, observed, recovered)`. `_tail_ok` returns `self._append_head(bot).observed is None`. Remove `seen` and the three fallbacks.
- Define `_RESERVED_KEYS = ("v", "seq", "bot", "prev", "hash", "_corrupt")` once.
- Give `_refuse_tail` the already redacted fields: `dict(stored, ts=record.get("ts"))` when a `ts` was given. Its payload is written with `sort_keys`, so the spool bytes stay the same.
- The second `_refuse_tail` call in `append` (non-regular day file) gets the same argument.

**Tests.**

- `test_refused_append_does_not_redact_under_the_lock` (the reproduction, expecting every probe to be `True`).
- `test_spool_payload_is_unchanged`: refuse one event with a secret, a `ts` and an archive `path`; compare the spool JSON with a literal.
- The order pinned by `test_roll_forward_is_persisted_before_the_new_line` must stay green.

### K11-07 (P2) — `_main` is one ≈550-line `if` chain

**Where.** `_main`, with 21 `if args.cmd == …` branches and a final `return 1` that only nested subcommands without a match would reach.

**What.** Four concrete problems follow from the shape.

1. **A date can change between two lines of one run.** The tune branch calls `today()` three times and renders the same Markdown twice:

   ```python
               out.write(render_tune(analysis, "Tuning proposals %s" % today(), labels))
           ...
               path = os.path.join(ledger.tuning_dir, today() + (("-" + (_chain_key(bot) or "bot")) if bot else "") + ".md")
               _write_markdown(path, render_tune(analysis, "Tuning proposals %s" % today(), labels))
   ```

2. **The anchor branch re-implements `_verify_text`.**

   ```python
                   out.write("FAIL  %-20s %6d records\n" % (_term(args.bot), 0))
                   out.write("      - no chain on disk for '%s'\n" % _term(args.bot))
   ```

   `_verify_text(args.bot, _verify_missing(args.bot, "no chain on disk for '%s'" % args.bot))` gives the same bytes. The `ANCHOR FAIL …` format string is also written twice.

3. **The sync branch detects a refusal by key and by string prefix.**

   ```python
           if res.get("refused") or (res["error"] and res["error"].startswith("refused:")):
               return 3
   ```

   `sync` sets the key whenever it writes that prefix, so the second half is redundant. It is also fragile: a push error is the last 400 characters of git's stderr, and a tail that happens to start with `refused:` would be reported as exit 3 instead of 1.

4. **Command classes are repeated as literal tuples.** "Logging commands", "readers that warn about skipped lines" and "readers that warn about an unknown bot" appear as three hand-maintained tuples at the top. There are also defensive `getattr(args, "json", False)`-style reads inside branches where argparse always defines the attribute.

**Reproduce (item 1).**

```python
days = iter(["2026-11-16", "2026-11-17", "2026-11-17"])
with mock.patch.object(gblog, "today", side_effect=lambda ts=None: next(days)):
    run(["tune"], root)
# stdout title: "Tuning proposals 2026-11-16"; file: tuning/2026-11-17.md
```

**Fix.**

- Move each branch verbatim into `_cmd_<name>(ledger, args, out) -> int`. `alias`, `capture`, `state` and `spool` dispatch on `args.what` through a second small table.
- `_main` shrinks to: parse, validate, build the ledger, apply three per-command flags, call `_COMMANDS[args.cmd]`.
- Hold the flags in the table: `_Command(handler, logs=False, reader=False)`.
- Tune: compute `day = today()` and `title` once. Render once when both stdout and the file need the text.
- Anchor: use `_verify_text` and one `_anchor_fail_line(bot, where, heads)`.
- Sync: `return 3 if res.get("refused") else …`.

**Tests.**

- `test_tune_uses_one_day_per_run` (the reproduction; file name day equals both titles).
- `test_every_subcommand_has_a_handler`: the `choices` of the subparsers action equal `set(gblog._COMMANDS)`.
- `test_push_error_that_starts_with_refused_is_exit_1`: patch `gblog._git` so `push` fails with stderr `"refused: by remote\n"` on a remote that passes the visibility check; expect exit 1.
- The golden transcript of section 3 guards the verbatim moves.

### K11-08 (P2) — Two ways to run git

**Where.** `_git` / `_git_call` versus the direct calls in `_sync_ignored` and `_sync_drift`:

```python
        res = subprocess.run(["git", "-C", root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
                              "logs", "anchors", "archive", "state", "spool", ".gitignore"], capture_output=True,
                             env=_git_env())
```

**What.**

- `_git` adds `-c core.hooksPath=<devnull> -c core.autocrlf=false` and runs under umask 077. The two raw calls exist only because they need bytes (`-z`), and they skip all of that. Any future hardening flag has to be added in three places.
- The "list of config values" comprehension is written four times: `_local_origin`, `current` and `old_push` in `sync`, and the `remote get-url --push --all` read.

One note outside my area, not verified: `core.fsmonitor` can name a program that index-reading git commands run, and `core.hooksPath` does not cover it. If the sync reviewer wants `-c core.fsmonitor=false`, a single wrapper is where it belongs.

**Reproduce.** `grep -n 'subprocess.run(\["git"' gblog.py` prints three call sites; only `_git_call` has the `-c` flags.

**Fix.**

- `_git(root, *args, check=True, binary=False)`. With `binary=True` it passes no `encoding` and returns bytes.
- Add `_git_lines(root, *args) -> List[str]` for the four comprehensions.
- Fold `_git_call` back into `_git` with `try/finally` around the umask.

**Tests.**

- `test_every_git_call_is_hardened`: patch `gblog.subprocess.run` with a recorder that delegates, run `sync --no-push` twice; every argv that starts with `git` contains `core.hooksPath=`.
- `GitLocaleTest` and the tests that patch `gblog._git` stay green.

### K11-09 (P2) — Dead and test-only members, unreachable branches

| Name | Callers in `gblog.py` | Callers in `tests/` | Action |
|---|---|---|---|
| `Ledger.missing_witnesses` | none; docstring says "Used by `verify --bot NAME`", which now uses `lost_chains_of` | none found | delete |
| `Ledger.chain_hashes` | none | none found | delete; its `own_only` rule (`rec.get("bot") == bot`) no longer matches what `--contains` accepts (K3-03) |
| `_SYNC_CACHE`, `_write_sync_cache`, `cache_path` | see K11-04 | one test | delete |
| `Ledger._verify_reference`, `Ledger._chain_snapshot` | see K11-03 | four tests | move to tests |
| `_LinearRule.pattern` | never read | none | delete the attribute |
| `Ledger._tail_head` | none (docstring says so) | many | move to `tests/_support.py` as `tail_head(ledger, bot)` |
| `Ledger.save_aliases` | none | `test_gblog.py` | keep as library API or move; decide and say so in the docstring |
| `install_shell_hook` | none (CLI calls `_install_shell_hook`) | three files | keep one public function that returns the status string; tests compare with `"present"` |
| `_GITHUB_REMOTE_RE` | none | two test classes | port the tests to `github_remote`, then delete |
| `Ledger._clamped_candidates` | one-line wrapper around `_scan`, one caller | none | inline |

Unreachable branches:

- `verify.step` (and the same lines in the reference). `_fields_ok(rec)` has already required a real `int`, so the `except` cannot run:

  ```python
              try:
                  seq_i = int(rec["seq"])
              except (KeyError, TypeError, ValueError):
  ```

- `_describe_join`: `except (KeyError, TypeError, ValueError): seq_before, seq_after = -1, -1`. Both arguments have passed `_fields_ok` at every call site.
- `append`: the three `seen` fallbacks (K11-06).
- `_main`, sync: the `startswith("refused:")` half (K11-07).

**Reproduce.** `grep -n "missing_witnesses\|chain_hashes" gblog.py tests/*.py` prints only the two definitions.

**Fix.** Delete or move as listed, one commit per row.

**Tests.** Extend `test_removed_names_are_gone`. For the unreachable branches the fuzz suite and the golden transcript are the proof.

### K11-10 (P2) — Task grouping exists five times and gives three answers

**Where.**

- `_tasks_of` (tune);
- the `tasks` dict in `render_report`;
- the `open_tasks` loop in the status branch of `_main`;
- `_task_context`;
- `_task_status`.

**What.** Each one decides on its own what opens and closes a task. Earlier fixes (D5-13, D6-10: "a task record without an id") had to be applied in several of them. They still disagree when a task id is reused after its `task-end`:

| View | Rule | Result for start, end, start |
|---|---|---|
| `status` | last event in `ts` order wins | open |
| `tune` (`_tasks_of`) | any `task_end` closes | closed |
| `report` | `start` overwritten by the later record, `end` kept | `start <t3>, end <t2>, outcome **ok**` (start after end) |
| `_task_status` | newest record wins | open |

K1-06 prints a warning that "reports will merge both runs". It does not say that `status` and `tune` will then count differently.

**Reproduce.**

```
gblog task-start --bot ops --task-id daily --task "run 1"
gblog task-end   --bot ops --task-id daily
gblog task-start --bot ops --task-id daily --task "run 2"
gblog status --json            # open_tasks: 1
gblog tune --json --stdout     # metrics.ops.open: 0
gblog report --stdout          # one section: start later than end, outcome ok
```

**Fix.** Two steps.

1. **Pure refactor.** Add one function `_task_events(records) -> Dict[str, List[Tuple[str, str, Dict]]]` (task id → ordered `(ts, kind, record)`). Build three small views on top of it; each keeps today's rule.
2. **Decision.** Choose one rule (I suggest "last event wins") and apply it to `tune` and `report`. This changes output for reused ids only and belongs in the CHANGELOG.

**Tests.**

- Step 1: table test of `_task_events` on hand-built records (no id, reused id, end without start, two chains).
- Step 2: `test_reused_task_id_is_open_everywhere`. The reproduction, asserting `open_tasks == 1`, `metrics["ops"]["open"] == 1` and `outcome **open**` in the report.

### K11-11 (P2) — Reader predicates, sort keys and raw scans are copied

**Where and what.**

- **"Is this a readable record"** is written five times with small differences:
  - `_readable` and `tail_records`: `"_corrupt" in rec or not isinstance(rec.get("ts", ""), str)`;
  - the later-file scan in `records` and `tune_history`: two separate `if` statements each;
  - `_task_context`: `not isinstance(rec.get("ts"), str)`, without the default.
- **The sort key** `(str(r.get("ts") or ""), str(r.get("bot") or ""), _seq_int(r.get("seq")))` is written in `tail_records._order`, in `records` and in the tune branch of `_main`. `chain_records` has a fourth form without the bot.
- **The raw scan** "open regular, for each line, test a marker, parse" exists as `Ledger._scan`. It is written out again in `_task_context`, `_task_status` and `_ReplayedIds.refresh`.
- `tail_records` bypasses `_readable` and therefore counts skipped lines itself.
- `_parse_line` and `_parse_line_dups` share a body except for the hook.

**Reproduce.** `grep -n '"_corrupt" in rec' gblog.py` and `grep -n '_seq_int(r.get("seq"))' gblog.py`.

**Fix.**

- Add module functions `_readable_record(rec) -> bool` and `_record_order(rec) -> tuple`.
- `tail_records` calls `_readable` per file.
- `_task_context` uses `Ledger._scan([path], marker)` inside its existing `try`.
- `_parse_line(raw)` and `_parse_line_dups(raw)` share `_loads_line(raw, hook)`.

**Tests.** Existing tests cover the behaviour (`test_skipped_lines_are_announced`, `test_tail_matches_full_read_across_day_files`, `test_non_string_bot_record_is_reported_by_directory`). Add `test_record_order_is_total` for `None`, list and bool values in `ts`, `bot` and `seq`.

### K11-12 (P2) — Ledger layout constants are written two to six times

| Fact | Copies |
|---|---|
| The seven ledger directories | tuple in `Ledger.init`; tuple in `Ledger.ensure`; `_LEDGER_TOPS`; `add_paths` in `sync`; pathspecs in `_sync_ignored` (five of them) and `_sync_drift` (three) |
| Create `.gitignore` if absent | `Ledger.init` (`os.path.exists`, raises on failure) and `_sync_evidence` (`os.path.lexists`, ignores failure) |
| Chain-name sanitising regex | `safe_name`, `_chain_key`, inline in `Ledger.resolve` |
| "Not a chain's state file" | `is_reserved`, and inline in `orphan_witnesses` (`name == "aliases" or name.startswith("capture-files-") or name.endswith(".alert") or ".alert.resolved-" in name`) |
| Reserved record keys | `append` and `_refuse_tail` (K11-06) |
| "A name selects nothing valid" | `invalid_chain_dirs` and `valid_chains` both compute `_chain_key(d) != d` |

**Reproduce.** `grep -n '"logs", ' gblog.py` and `grep -n 'A-Za-z0-9._-\]+", "-"' gblog.py`.

**Fix.**

- `_LEDGER_TOPS` becomes the single source. `Ledger.__init__` derives the `*_dir` attributes and a `self._dirs` tuple from it. `sync` builds `add_paths` and the pathspecs from it and from two named subsets (`_EVIDENCE_TOPS`, `_SYNCED_TOPS`).
- Add `Ledger._ensure_gitignore(strict: bool)`, called from both places with today's two failure modes.
- Add `_CHAIN_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")`. `safe_name(name)` becomes `_chain_key(name)` plus `_die`. `resolve` calls `_chain_key(raw)`.
- Add `_is_side_file_stem(name)`, used by `orphan_witnesses`. Keep its current, narrower rule: unlike `is_reserved` it does not exclude `.lock` and `.tmp` stems, and a legacy chain `ops.tmp` must still be reported.

**Tests.**

- `test_init_creates_exactly_the_ledger_tops`.
- `test_gitignore_is_created_by_init_and_by_sync` (remove it between the two).
- `test_side_file_stems`: table of `aliases`, `capture-files-x`, `x.alert`, `x.alert.resolved-1`, `ops.tmp`, `ops.lock`, `ops`.

### K11-13 (P2) — Helpers that exist once and are re-implemented elsewhere

| Idiom | Copies | Note |
|---|---|---|
| `os.path.relpath(p, self.root).replace(os.sep, "/")` | ≈14 | Four CLI messages use `relpath` without the replace (`wrote …` in report and tune, two `spool replay:` lines); they would print backslashes on Windows |
| `except BaseException: try: os.close(fd) …; raise` | 3 | `_open_regular`, `_open_private`, `_open_lock` |
| `now_iso().replace("-", "").replace(":", "")` | 2 | `_refuse_tail`, `spool_replay` |
| UTC timestamp formatting | 5 | `parse_ts`, `_ts_floor.at`, `_mtime_iso`, `capture_browser`, `_days_ago(…, "%Y-%m-%dT%H:%M:%SZ")` |
| `ALERT tail mismatch: %s\nspool: %s\nalert: %s\n` | 3 | `main`, `_record_verify`, `sync._bookkeep` (with ` on <bot>`) |
| `ALERT %s: tail mismatch since %s (%d spooled)` | 2 | both in the status branch |
| Heads named by an anchor entry | 4 + helper | `_anchor_entry_heads` exists. `_rollup_heads` repeats it exactly. `_anchor_heads`, `_anchor_checks` and the history line in `_anchor` are variants |
| One-line text of an exception | 2 | `_verify_guarded` (200 characters, `…`) and `main` (300 characters) |
| Count, list the first N, then "N more" | ≈10 | seven counters in `_layout_findings`; `bare`, `adopted` and links in `verify` |
| `how if how in ("missing", "unreadable") else …` | 3 | twice in `verify`, once in `append` (with a different default) |
| `if is_link and len(links) < _MAX_LAYOUT_FINDINGS: links.append(…)` | 3 | inside `_file_findings`, next to a local `link()` helper that does the same |

**Fix.**

- `Ledger.rel(path)`, plus one `_stamp()` and one `_fmt_ts(datetime)`.
- `_fdopen_checked(fd, check, mode, **kw)` for the three openers.
- `_alert_lines(exc, bot=None)` and `_status_alert_line(bot, since, n)`.
- `_rollup_heads` calls `_anchor_entry_heads`. Leave `_anchor_checks` alone: it must also report a head that is not a string.
- `_exc_text(exc, limit)`.
- A small collector for the capped lists:

  ```python
  class _Capped:
      def __init__(self, limit, more): self.items, self.n, self.limit, self.more = [], 0, limit, more
      def add(self, text):
          self.n += 1
          if self.n <= self.limit: self.items.append(text)
      def overflow(self): return [self.more % (self.n - self.limit)] if self.n > self.limit else []
  ```

  `_layout_findings` appends every finding to one shared `warnings` list in line order. The overflow lines follow in a fixed order: misfiled, duplicated, mistyped, non-finite, mismatched; then for `--strict` reformatted, odd `ts`. The collectors must share the output list and keep that order.

**Tests.**

- `test_layout_overflow_lines_keep_their_order`: a raw chain with 25 lines that each trigger four findings; compare `res["warnings"]` with a literal list. Write it before the change.
- For the four CLI messages, compare with `ledger.rel(path)`.

### K11-14 (P3) — Long functions

| Function | ≈ lines | Extraction that keeps behaviour |
|---|---|---|
| `_main` | 550 | K11-07 |
| `Ledger.verify` | 290 | K11-02; then `_state_findings(...)`, `_adoption_warnings(...)`, `_assemble_result(...)` |
| `analyze` | 270 | one generator per rule A–J yielding `add(...)` arguments; `analyze` keeps the loop and the order |
| `sync` | 215 | `_sync_set_remote`, `_sync_stage`, `_sync_commit`, `_sync_gate`, `_sync_push`, `_sync_stamp`, passing one result dict |
| `render_report` | 150 | `_report_tasks(recs, context)` returns ordered task objects; `_report_task_lines(task)` renders one |
| `capture_browser` | 150 | `_history_rows(path, since)` (copy, query, close) and `_import_rows(...)` |
| `capture_files` | 140 | `_scan_tree`, `_load_snapshot` (returns previous, reason, carried), `_diff_snapshot` |
| `Ledger._anchor` | 125 | lift `anchor_one` to a method; `_merge_rollup(day_doc, chains)` |
| `coverage` | 115 | `_index_self_reports`, `_match_auto`, `_drop_shared` |
| `Ledger.append` | 110 | `_choose_ts(explicit_ts, tail_ts, bot)` returns `(ts, regressed, wall)` |
| `_layout_findings` | 95 | K11-13 collector |

For `analyze`, the order of `add` calls is part of the output. The final sort is stable on `(category, -count)` and `add` drops a duplicate id, so the first call wins. A rule generator must be consumed in today's order: per bot A, B/C, D, E, F, G, H; then I; then J.

**Tests.** Add a golden test for `tune --json --stdout` on a seeded ledger with a frozen clock (section 3, step 0). `tests/test_tune_loop.py` covers the rules but not the order of ties.

### K11-15 (P3) — Hidden inputs

**Where.**

```python
    verify_want_hashes: Optional[List[str]] = None
    ...
        ledger.verify_want_hashes = wanted  # verify keeps only these hashes (and the anchored heads)
        ledger.verify_want_hashes = []  # anchor looks no hash up: verify need not keep them
    ...
        warned = self.__dict__.setdefault("_warned_chain_dirs", set())
```

**What.**

- `verify_strict`, `verify_skip_archives`, `verify_want_hashes`, `cache_aliases`, `warn_skipped` and `_outer_lock` are set from outside and change what methods return. `verify_want_hashes` changes the content of `res.hashes` without appearing in any signature.
- `_warned_chain_dirs` is created by writing into `__dict__`. "Warn once" has a second mechanism in the module global `_ALIASES_WARNED`, which `main` clears.
- `_VerifyResult` is a dict with two extra attributes. `_verify_guarded` returns a plain dict instead, so `_main` reads `getattr(res, "own_hashes", getattr(res, "hashes", ()))`.
- The 15-key result dict is built in `verify`, `_verify_reference` and `_verify_guarded`; `_verify_missing` builds a 7-key variant.

**Reproduce.**

```python
L = gblog.Ledger(root); L.verify_want_hashes = []
L.verify("ops").hashes        # set(): a later caller of the same object gets no hashes
```

**Fix.**

- `verify(bot, strict=None, skip_archives=None, want_hashes=None)`. The class attributes stay as defaults for one release, so tests that set them keep working.
- `_verify_result(bot, **over)` builds every result, including the aborted and the missing one. `_verify_guarded` returns a `_VerifyResult`.
- Initialise `self._warned_chain_dirs = set()` in `__init__`.

**Tests.** `test_verify_result_keys_are_the_same_everywhere`: the key sets of a normal, an aborted and a missing entry match the documented table in `docs/DESIGN.md` §7.

### K11-16 (P3) — Three ways to report a usage error

**What.**

- `_die(message)` writes `error: …` and raises `SystemExit(2)`. It is called from library code: `safe_name`, `Ledger._check_reserved`, `Ledger._add_alias`, `Ledger.resolve`, `capture_files`. `Ledger.append("///", …)` therefore exits the interpreter of a library caller; tests assert `SystemExit`.
- `_UsageError` is raised and turned into `error: …` by `main`.
- Direct `sys.stderr.write(...); return 2` in `_main`, with four prefixes (`seal:`, `archive:`, `state rebuild:`, `spool replay:`) and one message without a prefix (`no proposals yet; …`).

The texts are documented (`docs/agent-integration.md` §5) and pinned by tests, so they must not change. The structure can.

**Fix.** Keep every byte.

- `class UsageExit(SystemExit)` with `.message`. `_die` raises it after writing, so `assertRaises(SystemExit)` still holds. `main` needs no change.
- `_refuse(prefix, text) -> int` in the CLI layer for the direct writes. `prefix=None` covers the unprefixed line.
- Rule for new code: library raises typed exceptions, CLI prints.

**Tests.** The existing exit-code tests. Add `test_die_raises_usage_exit_with_message`.

### K11-17 (P3) — Naming

| Problem | Where | Fix |
|---|---|---|
| `canonical` means two things | module `canonical(record)` (hash input bytes) and `Ledger.canonical(chain)` (alias slug); `canonicalize(records)` is the second meaning | rename the module function `canonical_bytes`; keep `canonical = canonical_bytes` as an alias |
| `keep` has two types | `_open_private(…, keep: Optional[str])` is a path; `_atomic_write(…, keep: bool)` is a flag | `mode_from=` and `keep_mode=` |
| "bot" versus "chain" | `verify(bot)`, `append(bot)`, `log_files(bot)` take a chain directory; `records(bot)` takes a Bot and merges chains; `bots()` returns directory names including invalid ones | parameter `chain` where a directory is meant; `bots()` → `chain_dirs()` with an alias |
| `pending` has three meanings in `capture_files` | a dict of snapshot entries, a list in the snapshot file, a count in the result | `partial_snapshot`, `left_over`, result key unchanged |
| Names re-typed inside `analyze` | `titles` is a list in rule A and a dict in rule D; `days` is a set in rule D and a float in section J | disappears with the per-rule functions (K11-14) |
| Shadowing in `render_report` | `chains` (list per bot, then set per task); `head` (a Markdown heading, elsewhere a chain head) | `chain_names`, `twin_chains`, `heading` |
| Parameter reused as a flag | `sync(…, push, …)` sets `push = False` in three places | local `do_push` |
| `known` in `Ledger.resolve` | returns `(cleaned, not data["bots"])`; `True` means "no warning needed", not "registered" | rename the second element `quiet` in the docstring and the unpacking sites |
| Mixed privacy of exceptions | `_UsageError` and `_GuardSkip` are private; the other six are public | keep; document the rule next to the classes |
| `hist_problem` holds a list | `_anchor_checks` unpacks the fourth element of the snapshot | `file_problems` |

**Tests.** Renames are covered by the suite and the "seam" test of section 3 (step 0). No new behaviour.

### K11-18 (P3) — Imports, module layout, stale docstrings

**Imports.**

- `_sync_evidence` runs `import stat as _stat` although `stat` is imported at the top.
- `from bisect import insort` sits inside the per-bot loop of `render_report`.
- `urllib.parse` is imported inside eight functions; `gzip` in two; `zlib` in two; `time` in two.
- Lazy imports are justified for `sqlite3`, `shutil`, `glob`, `urllib.request` and `unicodedata` (start-up cost of every `gblog log`). Move the others to the top. Tests patch `urllib.request.urlopen` by module path, which keeps working either way.

**Layout.**

- The banner `# --- utils` comes after the redaction table; redaction is in three places (K11-05).
- `_DAY_FILE_RE`, `_MAX_LAYOUT_FINDINGS` and `_MAX_ADOPTED_LINES` are defined about 2,000 lines after their first use (`_ts_floor`, `Ledger.verify`).
- Proposed order, one file: constants and exceptions; time; JSON and record helpers; redaction; filesystem primitives; `Ledger` (layout, locks, aliases, readers, append, verify, anchors, recovery); report; archive; remote and sync; capture; coverage; tune; CLI.
- The alias registry (15 methods, ≈230 lines, own cache and lock) can become `_AliasRegistry`, with `Ledger` delegating the public methods.

**Stale text.**

- Module docstring: `spool/<bot>/<ts>.json` (now `<stamp>-<nnnn>-<id>.json`); no mention of `state/.anchors.lock`, `state/capture-files-<bot>.json`, `tuning/proposals.json`.
- `_anchor_snapshot` documents a 3-tuple and returns five values.
- `_append_head` documents a 5-tuple and also fills `seen`.
- `Ledger.missing_witnesses`: "Used by `verify --bot NAME`".
- `_sync_drift`: "sync passes .git/gblog-sync-cache.json".
- `_layout_findings`: "tuples of `Ledger._chain_snapshot`".
- `_SECRET_PATTERNS`: "(pattern, replacement) pairs", while two entries are `(_LinearRule, None)`.
- `Ledger.append`: the docstring is the history of the kim-biseo incident, not the contract (arguments, exceptions, what is written in which order).

**Fix.** A reorder is a pure move; check it with the AST comparison of section 3. Correct the docstrings in the commits that touch the functions.

**Tests.** A meta-test in `tests/test_meta.py`: function-local imports are limited to an allow-list (`sqlite3`, `shutil`, `glob`, `urllib.request`, `urllib.error`, `unicodedata`).

### K11-19 (P3) — Three clock sources

**What.**

- `now_iso()` is what tests freeze (`mock.patch.object(gblog, "now_iso", …)`).
- `_utcnow()` and `_days_ago()` read `_dt.datetime.now` directly. They are used by `status`, `tune`, `coverage` and `_task_status`.
- `time.time()` is a third source, used by capture and `_mtime_iso`.

Consequences in the tests: `StatusCountsTest` notes that the 30-day window "is computed from the real clock", and `tests/test_fuzz_verify.py` replaces the whole `gblog._dt` namespace to freeze it.

**Reproduce.** With `now_iso` patched to `2024-06-03T12:00:00Z`, `gblog status` still computes `month` from the real date and reads none of the 2024 day files.

**Fix.** One seam `_now() -> datetime` (aware, UTC). `now_iso`, `_utcnow` and `_days_ago` derive from it; capture uses `_now().timestamp()`. Tests that patch `now_iso` keep their present meaning, because `now_iso` stays a separate function. New tests patch `_now`.

**Tests.** `test_one_clock`: patch `gblog._now`; `now_iso()`, `today()`, `_days_ago(0)` and the cutoff of `capture_browser` all follow it.

### K11-20 (P3) — Parser construction

**What.**

- `_add_root_everywhere` uses `parser._actions`, `argparse._SubParsersAction` and `child._option_string_actions`. These are private; CI runs on 3.8 to 3.14 and nothing guarantees them.
- `_add_common` gives `--task`, `--source` and `--ts` to all six logging commands. `archive` then needs two runtime warnings ("`--task` is not stored…", "`--source` is not stored…"). The `tune` parser defines `--summary` and `--detail` as two arguments with one `dest`, while `_add_common` uses one argument with two option strings.

**Fix.**

- Create every subparser through one helper `_sub(subparsers, name, **kw)` that records the parser in a list. After all arguments are defined, add `--root` to each recorded parser with the public `add_argument`. Help order is unchanged because `--root` is still added last.
- Leave `_add_common` as it is; removing the two flags from `archive` would turn a warning into exit 2. Record the reason in a comment. Give `tune` the two-option form.

**Tests.** `test_root_after_subcommand` already covers every depth. Add `test_help_text_is_unchanged` for three subcommands (sha256 of `--help` output per Python minor version, or a comparison with the pre-change parser in the same test run).

### K11-21 (P3) — Two predicates for "binary content"

**Where.** `archive_file`:

```python
    as_text = _is_text(data)
    unscanned = bool(do_redact) and not as_text and b"\x00" in data
```

`_is_text` looks for a NUL byte in the first 4096 bytes only; the second test looks at the whole content.

**What.** Classification depends on the offset of a NUL byte. Valid UTF-8 with a NUL in the first 4 KiB is stored unscanned. The same content with the NUL at byte 5000 is scanned. `test_archive_roundtrip_dedup_redaction_and_verify` pins the first case (`b"\x00\x01\x02token=…"` is stored verbatim). The documents say "not UTF-8 and holds a NUL byte", which describes neither case exactly.

**Reproduce.**

```python
gblog.archive_file(L, "ops", b"\x00 token=abcdef123456\n", "a.txt")               # scanned: False, secret stored
gblog.archive_file(L, "ops", b"x" * 5000 + b"\x00 token=abcdef123456\n", "b.txt")  # redacted
```

**Fix.** One function `_archive_mode(data) -> "text" | "lossy" | "binary"` with the rule written once, and a table test. Whether the rule becomes "NUL anywhere" or stays "NUL in the first 4 KiB" is a behaviour decision for the maintainer and the security reviewer. The refactor should keep today's result and only make it explicit.

**Tests.** `test_archive_mode_table`: NUL at 0, 4095, 4096; valid and invalid UTF-8; empty content.

## 3. Refactor plan

Rules for every step:

- One concern per commit; no step mixes a move with a change.
- `gblog.py` stays one stdlib-only file for Python 3.8+.
- No change to stdout, stderr, exit codes, record bytes, state, anchor, spool or snapshot bytes, unless the step is marked **decision**.
- Names that tests patch are seams and keep their name and call sites: `now_iso`, `_dt`, `remote_visibility`, `_git`, `_sync_drift`, `_ssh_alias_target`, `github_remote`, `_git_blob_id`, `_task_status`, `_warn_task`, `_title_key`, `_parse_line_dups`, `redact`, `Ledger.write_state`, `Ledger._append_line`, `Ledger.append`, `Ledger.verify`, `Ledger.lock`, `Ledger._atomic_json`, `Ledger._tail_ok`, `Ledger._is_temp_copy`, `Ledger._spool_replayed`, `Ledger.iter_records`, `Ledger.tail_records`.

### Checkpoints

| Id | Checkpoint | Proves |
|---|---|---|
| C1 | `python -m unittest discover -s tests` on 3.8 and 3.12, plus `-W error::DeprecationWarning` on the newest | nothing pinned by a test changed |
| C2 | Golden CLI transcript (new, step 0) | stdout, stderr, exit code and on-disk bytes of ≈40 commands are identical |
| C3 | Differential `verify` versus the reference, and the 300-mutation fuzz | the verify refactor is exact |
| C4 | Pattern-source hashes and the `former` oracles | redaction is exact |
| C5 | AST comparison for pure moves (below) | a reorder changed order only |
| C6 | Ratchet in `tests/test_meta.py` (function length, local imports, handler table) | structure does not regress |
| C7 | `python tests/bench_large_ledger.py --records 50000` before and after | no cost regression in append and verify (compared by hand, not asserted) |

C2 in detail, `tests/test_golden_cli.py`:

- Freeze `gblog.now_iso` and `gblog._dt` (as `tests/test_fuzz_verify.py` does); patch `gblog.uuid.uuid4` with a counter.
- Run a fixed script: init, a task with every logging command, archive, alias add/list, verify (text, `--json`, `--strict`), anchor, status (text, `--json`), report, tail, export, coverage, tune (`--json`, apply, reject), a refused append, `spool list` and `spool replay`, `state rebuild`, three usage errors.
- Record per command `[exit code, stdout, stderr]` with the root replaced by `<ROOT>`, and at the end `{relative path: sha256}` for everything except `.git` and `*.lock`.
- Compare with `tests/golden_cli.json`. The file is generated once, at the commit before the plan starts, with `GBLOG_GOLDEN_UPDATE=1`. It is regenerated only in a **decision** commit, and then the diff of the JSON is the review artefact.

C5, run by hand on a move-only commit:

```bash
python - <<'PY'
import ast, subprocess
def body(src): return sorted(ast.dump(n) for n in ast.parse(src).body)
old = subprocess.check_output(["git", "show", "HEAD~1:gblog.py"]).decode()
assert body(old) == body(open("gblog.py").read()), "not a pure move"
PY
```

### Steps

| # | Commit | Finding | Checkpoints |
|---|---|---|---|
| 0a | Add the golden transcript test and its JSON | – | C1 |
| 0b | Add the golden `tune --json` test with tied proposal counts | K11-14 | C1 |
| 0c | Add the seam test (the names above exist) and the ratchet with today's numbers | – | C1 |
| 0d | Add `test_layout_overflow_lines_keep_their_order` | K11-13 | C1 |
| 1 | **Decision:** one `--hours` guard; align documents and the two tests | K11-01 | C1; C2 regenerated only if the script contains the case |
| 2 | Delete the sync cache | K11-04 | C1, C2 |
| 3 | Delete `missing_witnesses`, `chain_hashes`, `_LinearRule.pattern`; inline `_clamped_candidates` | K11-09 | C1, C2 |
| 4 | Remove the unreachable branches in `verify.step`, `_describe_join` and the sync exit code | K11-09, K11-07 | C1, C2, C3 |
| 5 | `_append_head` returns a `NamedTuple`; `_RESERVED_KEYS`; `_refuse_tail` takes redacted fields | K11-06 | C1, C2, new lock-probe test |
| 6 | Small helpers, one commit each: `Ledger.rel`, `_stamp`, `_fmt_ts`, `_fdopen_checked`, alert lines, `_exc_text`, `_CHAIN_KEY_RE`, `_LEDGER_TOPS` as source, `_ensure_gitignore`, `_is_side_file_stem`, `_readable_record`, `_record_order` | K11-11, K11-12, K11-13 | C1, C2 after each |
| 7 | `_git(binary=…)`, `_git_lines` | K11-08 | C1, C2, hardening test |
| 8 | Redaction: move the K4-04 block (C5), explicit list entries, named alternations, `redact` next to the list (C5) | K11-05 | C1, C4 |
| 9 | `verify`: explicit loop, `_ChainWalk`, `_VerifyScan`, `_LayoutCheck`, `_Capped` | K11-02, K11-13 | C1, C2, C3, C7 |
| 10 | Move `_verify_reference` and `_chain_snapshot` to `tests/_reference.py`; pass `own` | K11-03 | C1, C3 |
| 11 | `_verify_result` factory; `want_hashes=` keyword | K11-15 | C1, C2, C3 |
| 12 | `_main` → `_cmd_*`, one command (or one nested group) per commit, moved verbatim; then the table and flags | K11-07 | C1, C2 after each |
| 13 | Tune day computed once | K11-07 | C1; C2 unchanged (frozen clock) |
| 14 | `sync` phases | K11-14 | C1, C2 |
| 15 | `_task_events` and three views with today's rules | K11-10 | C1, C2, tune golden |
| 16 | **Decision:** one rule for reused task ids | K11-10 | C1; C2 regenerated, diff reviewed |
| 17 | `analyze` rule generators; `render_report`, `capture_*`, `coverage`, `_anchor` extractions | K11-14 | C1, C2, tune golden |
| 18 | Imports to the top (except the allow-list); module reorder; `_AliasRegistry` | K11-18 | C1, C2, C5 for the reorder |
| 19 | Renames with aliases; `UsageExit`; `_now` | K11-16, K11-17, K11-19 | C1, C2 |
| 20 | Parser helper for `--root`; `tune` option form | K11-20 | C1, help-text test |
| 21 | Lower the ratchet limits to the new numbers; remove aliases that no test uses | – | C1, C6 |

Order constraints:

- Step 9 must come before step 10; the oracle is the proof for the verify refactor.
- Step 5 comes before step 12, so the dispatch move does not carry the `seen` protocol into new functions.
- Steps 1 and 16 are the only ones that change behaviour. Each needs a CHANGELOG line.
- Step 12 is the largest. If a branch move changes the golden transcript, revert that one commit; do not fix forward.

## 4. Open decisions and limits of this review

Decisions for the maintainer:

- K11-01: is `--hours 0` accepted (documents) or refused (one test)?
- K11-10: does a reused task id reopen the task in `report` and `tune` as it does in `status`?
- K11-09: are `Ledger.save_aliases` and `install_shell_hook` public library API?
- K11-21: should a NUL byte count anywhere or only in the first 4 KiB?

Not checked:

- Nothing was executed. Each reproduction and each "unreachable" claim was derived by reading and should be confirmed by running it once.
- "No caller" is a text search of the files I was given. An external script that imports `gblog` could still use `chain_hashes` or `_GITHUB_REMOTE_RE`.
- I did not review the structure of the tests themselves beyond the seams they depend on, nor `install.sh` and the example scripts.
- The `core.fsmonitor` note in K11-08 is outside this area and unverified; it is passed on to the sync reviewer.