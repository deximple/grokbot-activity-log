# Specialist review K6 — 2026-11-16

**Area:** test suite quality — duplicated tests, clock/locale/umask/environment dependence, tests that assert too little, slow tests, and the highest-value missing tests for crash safety and concurrency.

**Method and limits.** I read `gblog.py`, `tests/`, `.github/workflows/ci.yml` and the documents. I ran nothing: every "no test fails if X is removed" claim comes from searching the test sources, and each finding names a mutation or command that confirms it in a minute. Run times and fsync costs are estimates, not measurements.

**Out of scope.** The round-4 G1/G2 items being worked on elsewhere (sync foreign-repo guard, tail `ts` bound, sync cache removal, side-file rule, task-status function, origin definition, rollup protection, hook fallback) are not reviewed here. Where a duplicate or a helper touches one of them, only the test-hygiene side is mentioned.

## Summary

| Id | Priority | Finding |
|---|---|---|
| K6-01 | High | A crash and an integrity FAIL both exit 1, and the suite's standard `assertNotIn("Traceback", err)` cannot fail in-process |
| K6-02 | High | Nine tests carry `bug:` / `pending:` comments but pass; two of them pin behaviour the documents contradict |
| K6-03 | High | No crash-point enumeration: only one of the four rows of the crash table is exercised, and only by a mock |
| K6-04 | High | The post-lock read window of the streaming `verify` has no test; a day file deleted in that window raises instead of reporting |
| K6-05 | High | Test git helpers inherit `GIT_DIR` / `GIT_INDEX_FILE`; the D3-09 scrub in `_git_env` has no test |
| K6-06 | Medium | Environment scrubbing differs per file; `GBLOG_DEBUG=1` in the caller's shell fails tests |
| K6-07 | Medium | Two UTC-midnight flakes; there is no shared frozen clock |
| K6-08 | Medium | Durability order is tested for `append` only (archive, anchor, refusal are not) |
| K6-09 | Medium | Concurrency gaps: `state rebuild` race branch, `update_state` on a torn tail, unlocked readers, fresh-root race, `fcntl is None` |
| K6-10 | Medium | Thread-based concurrency tests rely on `join(0.3)` / `join(5)`; one comment describes locking the code does not do |
| K6-11 | Medium | Every test pays for real `fsync`; one shared no-op removes most of it |
| K6-12 | Medium | About 60 interpreter starts can be replaced by in-process runs |
| K6-13 | Medium | Complexity tests either hang CI (no bound) or use a tight 5 s wall-clock bound |
| K6-14 | Low | About 20 exact or near-exact duplicate tests across the "review round" files |
| K6-15 | Low | Script checks are split between `ci.yml` and `test_scripts.py`; one test only greps `install.sh` |
| K6-16 | Low | The private-mode test checks a hand-written path list under one umask |
| K6-17 | Low | Skips are invisible; some properties are covered only by mocks although CI has the real platform |
| K6-18 | Low | Weak assertions (`assertNotEqual(rc, 3)`, `assertNotIn(rc, (2, 3, 4))`) |
| K6-19 | Low | `test_meta.py` gaps: ids without module, no duplicate-name check |
| K6-20 | Low | The fuzz corpus never checks that `append` and `verify` agree |

A consolidation plan follows the findings.

---

## K6-01 — A crash passes as an integrity FAIL (High)

**Where.** `tests/_support.py::run_raw`, `gblog.py::main`, `gblog.py::_verify_guarded`, and every CLI test that expects exit 1.

`main()` turns any unexpected exception into one line and exit 1:

```python
    except Exception as exc:  # last resort: malformed input must never end in a traceback
        ...
        sys.stderr.write("error: internal error (%s%s); set GBLOG_DEBUG=1 for a traceback\n" ...)
        return 1
```

`_verify_guarded` turns a crash inside one chain into a `FAIL … verify aborted: <class>` entry, also exit 1. Two consequences for the tests:

- `self.assertNotIn("Traceback", err)` is the suite's standard "did not crash" assertion. For an in-process run it cannot fail: `main()` never prints a traceback unless `GBLOG_DEBUG` is set.
- Any test that expects exit 1 and checks little else passes when the command crashes.

Only `tests/test_mutation_gaps.py` defends against this, with a private wrapper whose docstring states the problem ("a missing traceback alone proves nothing"). The other files use `_support.run` unchanged.

Examples that pass on a crash:

```python
# tests/test_integrity.py  RollbackDetectionTest.test_full_rewrite_after_anchor_fails
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out)

# tests/test_capture_sync.py  BrowserCaptureTest.test_unreadable_history_reported
        rc, out, err = run(["capture", "browser", "--bot", "cli3", "--history", bad], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
```

Others with the same shape:

- `test_integrity.py`: `test_truncate_after_anchor_fails` (last line), `test_archive_path_escape_is_problem`, `test_stray_jsonl_warns`;
- `test_cli_ux.py`: `ExitCodeTableTest.test_exit_code_table` (`verify --bot nosuchbot`).

**Reproduction.** Add `raise RuntimeError("x")` as the first line of `Ledger._anchor_checks`. Then run:

```
python -m unittest tests.test_integrity.RollbackDetectionTest.test_full_rewrite_after_anchor_fails
```

The CLI assertion at the end of that test still holds (exit 1). The test goes red only because it also calls `ledger.verify()` directly earlier. Tests that use the CLI alone stay green.

**Fix.** Make the strict behaviour the default in `_support.run` and delete the private copy in `test_mutation_gaps.py`:

```python
_CRASH_MARKS = (("Traceback", "err"), ("error: internal error", "err"),
                ("verify aborted", "out"), ("anchor aborted", "out"))

def run(argv, root, internal_ok=False, **options):
    rc, out, err = run_raw(["--root", root] + list(argv), **options)
    if not internal_ok:
        for text, where in _CRASH_MARKS:
            if text in (err if where == "err" else out):
                raise AssertionError("gblog %s: unexpected %r\n%s\n%s"
                                     % (" ".join(argv), text, out[-2000:], err[-2000:]))
    return rc, out, err
```

These tests expect the marks and need `internal_ok=True`:

- `test_security_regressions.ReaderRobustnessTest.test_unexpected_error_in_one_chain_does_not_hide_the_others`;
- `test_deep_reviews_b.ChainIsolationTest.test_anchor_write_failure_in_one_chain_does_not_stop_the_others`;
- `test_specialist.AnchorVerifyIsolationTest.test_anchor_isolates_a_non_regular_history_file` and `test_anchor_isolates_a_directory_at_the_anchor_file`;
- `test_mutation_gaps.InternalErrorGapTest` (it already calls the raw runner).

Any other test that turns red after the change is a real finding.

Apply the same check to `run_cli`. A real process prints the same `error: internal error` line.

**Test cases.**

1. `test_meta`: a self-test that `_support.run` raises `AssertionError` when `main` is patched to print `error: internal error (X)`.
2. After the change, the `raise RuntimeError` mutation above must fail at least one CLI-only test in `test_integrity.py`.

---

## K6-02 — Nine tests say "bug" or "pending" and pass (High)

**Where.** `tests/PENDING.txt` says "Nothing is pending", and no test carries `@unittest.expectedFailure`. These comments and docstrings say otherwise:

| File | Test | Comment | Current state |
|---|---|---|---|
| `test_mutation_gaps.py` | `ArchiveClaimGapTest.test_bare_archive_record_is_not_counted_as_intact` | `# bug: D-8 … verify counts it` | passes: `archives = sum(1 for r in arch_recs if r.get("path") or r.get("sha256"))` |
| same | `InternalErrorGapTest.test_debug_env_reraises_from_verify_all` | `# bug: D-6 … never reads the variable` | passes: `_verify_guarded` reads `GBLOG_DEBUG` |
| same | `DocumentedButAbsentTest` (5 tests) | class docstring: "Each test fails today." | all five features exist |
| `test_deep_reviews_b.py` | `SyncDeepTest.test_no_repo_origin_is_never_pushed` | `# pending: D3-03 remotes-file case` | passes (`_local_origin`) |
| same | `BrowserWindowTest.test_hours_zero_is_a_usage_error` | `# bug: D4-03 … --hours=0 is accepted` | passes: `--hours 0` exits 2 |

The module docstring of `test_mutation_gaps.py` also promises tests "marked `@unittest.expectedFailure` with a `bug: ...` comment"; there are none.

Two of these are more than stale comments. The test, the code and the documents disagree:

- **`--hours 0`.** `_validate_args` has two consecutive checks:

  ```python
  if args.cmd == "capture" and args.what == "browser" and args.hours < 0:
      raise _UsageError("--hours must be 0 or more")
  if args.cmd == "capture" and args.what == "browser" and args.hours < 1:
      raise _UsageError("--hours must be 1 or more (a window of 0 hours imports nothing)")
  ```

  The first message says 0 is allowed and the second refuses it. README and FAQ 28 say "a negative value exits 2". The CHANGELOG says "`--hours 0` imports nothing on a first run" and lists "refused when it is negative, not when it is below 1" as a documentation correction. The library clamps with `hours = max(0, hours)`. The test pins exit 2 for 0.

- **Bare archive records.** The code no longer counts them and the test asserts that. `docs/DESIGN.md` section 7 (`archives` row) and the CHANGELOG "Known open findings" (D1-13, with a pointer to `tests/PENDING.txt`) still say they are counted.

**Reproduction.** `grep -nE '^\s*# (bug|pending):' tests/test_*.py` lists nine comment lines. `tests/PENDING.txt` lists nothing.

**Fix.**

1. Decide `--hours 0` once. The documents and the library clamp point to accepting 0 (exit 0, "0 new visit(s)", marker written). Then remove the second check, or correct README, FAQ and CHANGELOG. Keep one test for both bounds with the exact messages; `BrowserWindowTest.test_negative_hours…` currently asserts only the prefix `"error: --hours must be"`.
2. Remove D1-13 from "Known open findings" and correct the `archives` row in DESIGN section 7.
3. Rewrite the nine comments as `# fixed: <id>` (the convention `test_security_regressions.py` uses) and correct the two docstrings.
4. Make the convention checkable in `tests/test_meta.py`: a `# bug:` or `# pending:` comment directly above a test, or above its decorators, requires `@unittest.expectedFailure`.

```python
_OPEN_RE = re.compile(r"^\s*#\s*(bug|pending):")

def commented_open(text):
    """Names of tests that have a '# bug:' / '# pending:' comment in the lines above their def."""
    found, flagged = set(), False
    for line in text.splitlines():
        s = line.strip()
        if _OPEN_RE.match(line):
            flagged = True
        elif s.startswith("def test") and flagged:
            found.add(s[4:s.index("(")]); flagged = False
        elif s and not s.startswith(("#", "@")):
            flagged = False
    return found
```

**Test cases.** `PendingListTest.test_open_comments_have_markers`: for every test file, `commented_open(source)` must be a subset of the tests marked `expectedFailure`. Add a sample-based self-test like the existing `_SAMPLE`.

---

## K6-03 — No crash-point enumeration for the append state machine (High)

**Where.** `docs/DESIGN.md` section 4, "Crash outcomes", has four rows. The tests cover:

| Crash point | Existing test | How |
|---|---|---|
| before the line | none | — |
| during the line (torn) | `ConcurrencyTest.test_failed_append_leaves_no_torn_line` | mocked `ENOSPC` on the anchor history. This is the rollback path, which a killed process never runs |
| between line and state | `RecoveryTest.test_state_one_behind_rolls_forward`, D2-01 and D2-09 tests | mocked `write_state` / `os.replace` raising |
| after state | implicit | — |

All of these raise an exception inside the process, so `finally` and `except BaseException` cleanup runs. `_append_line` truncates the torn bytes; `_atomic_write` removes the temp file. A killed process does neither. No test asserts the state a kill leaves behind: a torn last line, or a `state/<bot>.json.<pid>.tmp` left by another pid.

`test_stale_tmp_file_is_replaced` writes the leftover under the test's own pid. That models pid reuse only. A leftover from another pid is never replaced, because `_tmp_name` is per pid.

**Reproduction of the gap.** Change `_append_line` to write the line in two `os.write` calls, or move `self.write_state` before `self._append_line` in `append`. The second mutation makes state run ahead of the log on a crash, which is the kim-biseo shape. Only `AppendGapTest.test_append_fsync_order` notices it, as an order assertion. No test shows the resulting ledger refusing or verifying.

**Fix: one harness, enumerated automatically.** POSIX only (`os.fork`). The child patches the syscall wrappers and calls `os._exit` at the k-th call. The parent checks invariants on what is on disk. k runs from 1 until the scenario completes without reaching it, so no call count is hard-coded.

```python
# tests/_support.py
_POINTS = ("open", "write", "fsync", "replace", "ftruncate", "mkdir", "remove")

def crash_at(root, scenario, at, half_write=False):
    """Run scenario(Ledger(root)) in a forked child that dies at the at-th patched os call.
    Returns True when the child died there, False when the scenario finished first."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # 3.12+: fork with threads present
        pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
            real, calls = {n: getattr(os, n) for n in _POINTS}, [0]
            def wrap(name):
                def inner(*a, **k):
                    calls[0] += 1
                    if calls[0] == at:
                        if half_write and name == "write":
                            real["write"](a[0], bytes(a[1])[:max(1, len(a[1]) // 2)])
                        os._exit(77)
                    return real[name](*a, **k)
                return inner
            for n in _POINTS:
                setattr(os, n, wrap(n))
            scenario(gblog.Ledger(root))
        except BaseException:
            code = 3
        finally:
            os._exit(code)
    return os.WEXITSTATUS(os.waitpid(pid, 0)[1]) == 77
```

Each iteration starts from a `shutil.copytree` of a small pristine ledger, which takes milliseconds. A fork avoids about 250 interpreter starts.

**Scenarios and invariants** (new file `tests/test_crash_points.py`):

| Scenario | Invariants after a crash at any point k |
|---|---|
| S1 append, existing chain, same day | I1, I2, I4 |
| S2 append that opens a new day file | I1, I2, I4; an empty day file is ignored by `_tail_record` |
| S3 first record of a chain | I1, I2; never a `state_adopted` marker (D2-09) |
| S4 roll-forward append (state one behind at start) | I1, I2; a crash during the healing state write leaves state exactly one behind again (D2-01) |
| S5 `archive_file` | I1, I2; `verify` never reports `archived file missing`; an unreferenced `.gz` is harmless; a re-run prints `(already stored)` or stores once |
| S6 refused append (`_refuse_tail`) | the log bytes are unchanged; at least one of alert and spool exists whenever the crash came after the first of the two writes; no half-written JSON under `spool/` or `state/` |
| S7 `spool_replay` of 3 events | after a re-run to completion each event is in the chain exactly once, in file-name order; the alert is resolved |
| S8 `anchor(["ops"])` | `verify` stays OK; a re-run exits 0; the history file has only complete lines |
| S9 `add_alias` | the registry is the old or the new document, never partial; the next command resolves names |
| S10 `rebuild_state` | state is old or new; the note is appended at most once; a re-run ends in `already matches` |

- **I1.** `Ledger(root).verify(bot)` returns without raising. It is `ok`, or it has exactly one problem. That problem is either the state-head mismatch with state exactly one record behind (the known D2-10 limit; pin it), or, for `half_write=True`, `…: not valid JSON` on the last line.
- **I2.** The next `append`: for non-torn points it succeeds, `verify` is then `ok` with no warnings, and the crashed record appears 0 or 1 times, never twice. For torn points it raises `TailMismatchError`, the log bytes are unchanged and the event is spooled. This is the "not auto-repaired" row of DESIGN section 4.
- **I4.** No record and no state file carries `state_adopted`.
- **I5** (all scenarios). Leftover `*.tmp` files of the dead child do not change the output of `verify --all`, `status`, `spool list` or `anchor`.

Run each scenario twice: `half_write=False`, and `half_write=True` for the points that are `os.write`.

**What this harness cannot see.** `os._exit` keeps the page cache, so it models a killed process, not a power cut. Missing or misplaced `fsync` calls are the subject of K6-08.

---

## K6-04 — The post-lock window of the streaming `verify` is untested (High)

**Where.** `gblog.py::Ledger._snapshot_lines` (K4-02). `verify` takes file sizes under the shared lock and parses after releasing it:

```python
            with _open_regular(path) as fh:
                st = os.fstat(fh.fileno())
                if (st.st_dev, st.st_ino) != (dev, ino) or st.st_size < size:
                    # never caused by gblog: the file was swapped or cut by something else after the snapshot
                    problems.append("%s: log file was replaced or shortened while it was being verified" % rel)
                    continue
```

Three tests hook the lock release (`_AfterRelease` in `test_integrity.py`, `test_security_regressions.py`, `test_deep_reviews_b.py`). All of them append after the release and so cover only the `raw[:left]` bound. I found:

- no test for the `replaced or shortened` problem (the string appears in no test file);
- no test for a day file deleted in the window. `_open_regular` then raises `FileNotFoundError` out of `Ledger.verify`. `verify --all` reports `verify aborted: FileNotFoundError`. `seal` and `state rebuild` call `Ledger.verify` directly and end as `error: No such file or directory` (exit 2) with no chain named.

**Reproduction** (uses the hook pattern the suite already has):

```python
def after_release(self, action):
    fired, real = [], gblog.Ledger.lock
    def hooked(ledger, bot, *a, **k):
        inner = real(ledger, bot, *a, **k)
        shared = k.get("shared", a[0] if a else False)
        if shared and not fired:
            fired.append(1)
            return _AfterRelease(inner, action)
        return inner
    return mock.patch.object(gblog.Ledger, "lock", hooked)

def test_day_file_deleted_after_snapshot(self):
    self.seed("ops", 3)
    log = self.ledger.log_files("ops")[0]
    with self.after_release(lambda: os.remove(log)):
        res = self.ledger.verify("ops")          # today: raises FileNotFoundError
```

**Fix.**

1. In `_snapshot_lines`, catch `OSError` around `_open_regular` and append `"%s: log file disappeared or became unreadable while it was being verified (%s)" % (rel, exc.__class__.__name__)`. The chain is then FAIL with a line that names the file, in the library as well.
2. Move `_AfterRelease` and the `after_release` helper into `_support` (three copies today).

**Test cases** (`VerifySnapshotTest`):

| Action after the lock release | Expected |
|---|---|
| `os.remove(day file)` | `ok` False; the problem names the file; no exception |
| truncate the file to half its size | `ok` False; the `replaced or shortened` text |
| write a new file and `os.replace` it over the day file (new inode, same bytes) | `ok` False; `replaced or shortened`. The check is on the inode on purpose |
| append one record (existing tests) | `ok` True; `records` equals the snapshot count |
| create a new, later day file | `ok` True; `records` equals the snapshot count |

---

## K6-05 — Test git helpers inherit `GIT_DIR` / `GIT_INDEX_FILE`; D3-09 is untested (High)

**Where.** `tests/_support.py::GIT_ENV`, and the git helpers in `test_capture_sync.py` (`_Base._git`), `test_deep_reviews_b.py` (`git`), `test_mutation_gaps.py` (`git`), `test_security_regressions.py` (`git`), `test_specialist.py` (`_git`), `test_examples.py` (`_git`), `test_cli_ux.py` and `test_k3_k4.py` (inline `subprocess.run([GIT, …])`).

```python
# tests/_support.py
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "file"}

# tests/test_capture_sync.py
    def _git(self, *args):
        return subprocess.run(["git"] + list(args), check=True, capture_output=True, text=True).stdout
```

`GIT_ENV` adds variables and removes none. gblog scrubs its own git children (`_GIT_ENV_DROP`, D3-09), but the test helpers call git directly. git exports `GIT_DIR` and `GIT_INDEX_FILE` to hooks, so this applies when the suite runs from a pre-commit or pre-push hook, or under `git rebase -x "python -m unittest …"`. `git -C <tmp>` does not override `GIT_DIR`.

Then, by git's documented behaviour (not run):

- `SyncTest.test_sync_commits_gitignore_and_untracks_locks` runs `git init`, `add -f state/ops.lock …` and `commit -m "0.5 backup"`. With `GIT_DIR` inherited these act on the developer's repository and index: a stray commit on the current branch.
- `SyncGapTest.test_sync_detects_a_day_file_git_was_told_not_to_look_at` runs `update-index --assume-unchanged` against the developer's index.

Second half: the product-side scrub has no test. `GitLocaleTest.test_git_runs_in_the_c_locale` checks `LC_ALL`, `LANGUAGE` and `LANG` only. No test file contains `GIT_DIR`.

**Reproduction.**

```
# in a throw-away clone, never in a checkout you care about:
GIT_DIR="$PWD/.git" GIT_INDEX_FILE="$PWD/.git/index" \
  python -m unittest tests.test_capture_sync.SyncTest.test_sync_commits_gitignore_and_untracks_locks
git log -1 --format=%s      # expected by reading: "0.5 backup"
```

Mutation for the second half: set `_GIT_ENV_DROP = ()`. The whole suite stays green.

**Fix** (tests only; the product-side foreign-repository guard is G-work and not repeated here):

```python
# tests/_support.py
_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
              "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")

def isolate_git(test, home):
    """Patch os.environ for one test: GIT_ENV applied, repository-pointing variables removed,
    HOME and XDG_CONFIG_HOME inside the test's temp dir (git older than 2.32 ignores GIT_CONFIG_GLOBAL)."""
    patcher = mock.patch.dict(os.environ, dict(GIT_ENV, HOME=home, XDG_CONFIG_HOME=home))
    patcher.start(); test.addCleanup(patcher.stop)
    for key in list(os.environ):
        if key in _GIT_SCRUB or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del os.environ[key]

def git(*args, check=True):       # the one helper every test file uses
    return subprocess.run(["git"] + list(args), check=check, capture_output=True,
                          encoding="utf-8", errors="replace").stdout
```

The `HOME` override also closes a vacuous-pass hole. On git older than 2.32, `GIT_CONFIG_GLOBAL` is ignored. `SyncDeepTest.test_commit_signing_config_does_not_break_sync` and the excludes-file half of `test_sync_with_ignored_top_level_directory` then pass without their config file being read.

**Test cases.**

1. `GitEnvTest.test_git_children_do_not_inherit_repository_variables` (unit): patch `gblog.subprocess.run` as `GitLocaleTest` does. Set `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0`. Call `gblog._git(root, "status")`. Assert none of them is in `kwargs["env"]` and that `os.environ` still has them.
2. `SyncGitDirTest.test_sync_ignores_inherited_git_dir` (end to end, local): `decoy = bare("decoy.git")`. Run `sync --no-push` with `GIT_DIR=decoy` and `GIT_INDEX_FILE=<tmp>/decoy.index`. Assert one commit in `<root>/.git`, zero in `decoy`, and that `decoy.index` does not exist.
3. `test_meta`: no test file calls `subprocess.run(["git"` or `[GIT,` outside `_support` (AST or regex check), so the scrub cannot be bypassed again.

---

## K6-06 — Environment scrubbing differs per file (Medium)

**Where.** The base classes scrub different variables:

| File / base class | Removes |
|---|---|
| `test_deep_reviews*.py`, `test_mutation_gaps.py`, `test_specialist.py`, `test_k3_k4.py` | `GBLOG_TASK`, `GBLOG_ROOT`, `GBLOG_DEBUG`, `GBLOG_BOT` |
| `test_round3_followups.py` | `GBLOG_TASK`, `GBLOG_ROOT`, `GBLOG_DEBUG` |
| `test_security_regressions.py`, `test_cli_ux.py`, `test_append_safety.py`, `test_malformed_input.py` | `GBLOG_TASK`, `GBLOG_ROOT` |
| `test_gblog.py`, `test_integrity.py`, `test_chain_repair.py`, `test_tune_loop.py`, `test_fuzz_verify.py`, `test_capture_sync.py` | nothing |
| `_support.run_cli` | `GBLOG_TASK`, `GBLOG_ROOT`, `PYTHONUTF8` (not `GBLOG_DEBUG`) |

The project's own skill tells a Bot to `export GBLOG_TASK="$TID"`, and README setup step 1 has a Bot run the tools on that computer. A suite run from such a shell is the normal case.

**Reproduction.**

```
GBLOG_DEBUG=1 python -m unittest \
  tests.test_security_regressions.ReaderRobustnessTest.test_unexpected_error_in_one_chain_does_not_hide_the_others
```

`_verify_guarded` re-raises, `_support.run` renders the traceback, and `assertNotIn("Traceback", err)` fails.

With `GBLOG_TASK=abc` exported I found no failing test. Records written by `log`/`note` in the unscrubbed files silently carry the caller's task id, and `_warn_task` scans run.

`test_gblog.CaptureCoverageTuneTest.test_log_source_and_env_task` sets and then deletes `os.environ["GBLOG_TASK"]`. That removes the caller's value for the rest of the process instead of restoring it.

**Fix.** One function in `_support`, used by one shared base class (see the consolidation plan) and by `run_cli`:

```python
_SCRUB = ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT", "GBLOG_BROWSER_HISTORY",
          "GBLOG_PREFIX", "GBLOG_REPO", "GBLOG_REF", "GBLOG_SHA256", "PYTHONWARNINGS")
```

`PYTHONWARNINGS` is scrubbed for child processes only. The in-process `-W error::DeprecationWarning` of CI stays.

**Test cases.** `test_meta`: every `unittest.TestCase` subclass under `tests/` derives from `_support.LedgerCase`, or is on a short allow-list (`PendingListTest`, pure-function classes such as `RemoteFormTest`). CI: add `GBLOG_DEBUG: "1"` and `GBLOG_TASK: leak` to the env of the existing "non-UTC time zone, C locale" step, so a missing scrub fails there.

---

## K6-07 — Two UTC-midnight flakes; no shared frozen clock (Medium)

**Where.**

```python
# tests/test_gblog.py  CliTest.test_full_flow
        rc, tid, _ = run(["task-start", "--bot", "ops", "--task", "demo"], self.root)
        ...
        rc, out, _ = run(["report", "--bot", "ops", "--stdout"], self.root)
        self.assertIn("### demo", out)                      # report = today's UTC day file

# tests/test_gblog.py  AliasArchiveSyncTest.test_archive_roundtrip_dedup_redaction_and_verify
        rc, out, _ = run(["archive", "--bot", "ops", "--file", src], self.root)
        self.assertIn("already stored", out)                # same content, "once per Bot and day"
        self.assertEqual(len(os.listdir(os.path.dirname(full))), 1)
```

Both read the wall clock twice and need both readings on one UTC day. The product behaviour is correct (reports and archive addresses are per UTC day). Only the tests are fragile. The window is the test's own run time once per day, so failures are rare and cannot be reproduced on request.

The suite already has three private ways of freezing time:

- `StatusCountsTest.setUp` patches `gblog.now_iso`;
- `test_fuzz_verify._FROZEN_DT` replaces `gblog._dt`;
- several tests patch `now_iso` inline.

None of them freezes `_utcnow()`, so `_days_ago`, the 30-day window of `status`, `_task_status` and `tune` still read the real clock inside "frozen" tests.

The other clock-dependent tests I checked are written safely: `test_report_date_validation` (y1/y2), `test_examples.test_healthy_run…` (day_before/day_after), `ArchiveClaimGapTest._claim` (day taken from the file name).

**Reproduction** (deterministic):

```python
ticks = iter(["2024-06-01T23:59:59Z"] * 40)
def clock():                       # 40 readings on day 1, then day 2
    return next(ticks, "2024-06-02T00:00:01Z")
with mock.patch.object(gblog, "now_iso", clock):
    CliTest("test_full_flow").debug()       # AssertionError: '### demo' not found
```

**Fix.**

```python
# tests/_support.py
@contextlib.contextmanager
def frozen_clock(ts="2024-06-03T12:00:00Z"):
    """Freeze gblog's wall clock: now_iso() and _utcnow() both answer ts."""
    when = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    with mock.patch.object(gblog, "now_iso", lambda: ts), mock.patch.object(gblog, "_utcnow", lambda: when):
        yield
```

Use it in the two tests above, in `StatusCountsTest` (which then no longer needs its "at most a few seconds later" caveat) and in `test_fuzz_verify`, replacing `_FROZEN_DT`. `capture_browser` reads `time.time()`; its tests use windows of hours and are not affected.

**Test cases.** A `ClockBoundaryTest` with a stepping clock (as in the reproduction) around the per-day features: `report` (default date), `archive` de-duplication, the `tune` output file name, the `anchor` rollup file name. Each assertion states the intended per-day behaviour, so the boundary is tested deliberately.

---

## K6-08 — Durability order is tested for `append` only (Medium)

**Where.** `AppendGapTest.test_append_fsync_order` records `os.fsync` and `os.replace` and asserts the order for `append`. It is the only such test. These orderings are all stated in comments or documents and unpinned:

```python
# archive_file
        _atomic_write(full, _emit, mode="wb", keep=False, fsync_dir=False)
    # the file and its directory entry must be durable before the hash is chained
    _fsync_dir(os.path.dirname(full))
    ...
        rec = ledger.append(bot, {"type": "archive", ...
```

| Path | Required order | Mutation that survives today |
|---|---|---|
| `archive_file` | fsync(tmp .gz) → replace → fsync(archive day dir) → day-file write of the record | delete the `_fsync_dir(os.path.dirname(full))` line |
| `_atomic_json` (anchor, rollup, alert, spool) | fsync(tmp) → replace → fsync(dir) | `fsync_dir=False` in `_atomic_json` |
| `_refuse_tail` | alert file complete before the spool file ("the alert comes first because `status` reads it") | swap the tuple order |
| `_anchor` | `anchors/<bot>.json` before the history line | swap |
| `append`, first record | genesis state durable before the first line (D2-09) | covered by a mock; the order is not asserted |

The crash harness of K6-03 cannot detect these mutations, because a killed process keeps its page cache.

**Fix.** Lift the recorder out of `test_append_fsync_order` into `_support`:

```python
@contextlib.contextmanager
def fs_events(root):
    """Record ('fsync', relpath-or-inode), ('replace', relpath) and ('write', relpath) in call order."""
```

`fsync` entries are resolved after the fact by `(st_dev, st_ino)`, as the existing test does. `write` entries come from wrapping `gblog.Ledger._append_line`.

**Test cases** (`DurabilityOrderTest`):

1. `test_archive_is_durable_before_it_is_chained`: the events must contain, in order, `fsync(<sha>.gz inode)`, `replace(archive/ops/<day>/<sha>.gz)`, `fsync(archive/ops/<day>)`, `write(logs/ops/<day>.jsonl)`.
2. `test_anchor_files_are_replaced_atomically_and_synced`: for each of `anchors/ops.json` and `anchors/days/<d>.json`: fsync, replace, fsync(dir).
3. `test_refusal_writes_alert_before_spool`: `replace(state/ops.alert.json)` precedes `replace(spool/ops/…json)`, each with its fsyncs.
4. `test_first_record_writes_genesis_state_first`: `replace(state/x.json)` precedes `write(logs/x/…)`, and a second `replace(state/x.json)` follows.

---

## K6-09 — Concurrency gaps with deterministic tests available (Medium)

All five can be written with the hook patterns the suite already uses. None needs sleeps.

**(a) `state rebuild` racing an append.** `rebuild_state` verifies without the lock and re-checks under it:

```python
            if "_corrupt" in tail or not _fields_ok(tail) or tail["hash"] != res["head"]:
                raise StateRebuildError("refusing: the log changed while it was being checked; run the command again")
```

No test reaches this line (the message is in no test file). Test: remove the state file and wrap `Ledger.verify` so that another `Ledger` appends one record after the real verify returns. Expect `StateRebuildError` with this text and no `state rebuilt…` note in the chain. A second `rebuild` returns `noop` because the interloper adopted the tail. The CLI exits 2 with `state rebuild: refusing: the log changed…`.

**(b) `update_state` on a torn tail without state.**

```python
                    if not _self_consistent(rec):
                        sys.stderr.write("warning: state/%s.json is missing or unreadable and the log tail is not "
                                         "self-consistent; state not written\n" % bot)
                        return False
```

This is untested, yet it is the branch that keeps `sync` and `capture browser` from writing a genesis state over a damaged chain. Test: seed 2 records, append `b'{"torn": '` to the day file, remove the state file. Then:

- `update_state("ops", {"last_sync": …})` returns `False`, prints the warning, and no state file exists;
- `gblog sync --no-push` exits 0 and still writes no `state/ops.json`;
- `gblog note --bot ops …` exits 4. The chain was not restarted at seq 1.

Mutation: replace `return False` by a fall-through. No test fails today.

**(c) Readers that take no lock.** DESIGN section 5, rule 5: "`records()`, `report`, `status` and `tail` tolerate a half-written last line". `LockingTest.test_parallel_appends_and_verify` runs only `verify` beside the writers. Extend its polling loop at no extra process cost:

```python
                recs = self.ledger.records("ops")              # must not raise
                self.assertEqual([r["seq"] for r in recs], list(range(1, len(recs) + 1)))
                self.assertGreaterEqual(len(recs), seen); seen = len(recs)
                self.ledger.tail_records("ops", 5); self.ledger._state_view("ops")
```

**(d) First use of a root by several processes.** Every concurrency test calls `ledger.init()` first, so the race in `ensure()` → `init()` never runs: `_makedirs`, the README marker, `.gitignore`. Test: four spawned `_writer` processes on a root that does not exist. Assert all exit 0, the record count is 4×n, `verify` is ok, and `README.md` and `.gitignore` hold exactly the expected bytes (not doubled, not empty).

**(e) No `fcntl`.** The code promises "where `fcntl` is unavailable, gblog runs without locks". CI has no such platform, and the tests only skip on `gblog.fcntl is None`. A regression in `_Lock.__enter__`/`__exit__` or in the `anchor` `_outer_lock` handling would ship unseen. Test: `mock.patch.object(gblog, "fcntl", None)`, then one flow: `task-start`, `log`, `archive`, `task-end`, `verify --all`, `anchor`, a refusal followed by `spool replay`, `alias add`. Assert exit codes and that `verify` is ok.

---

## K6-10 — Thread-based concurrency tests depend on sleeps (Medium)

**Where.** `tests/test_security_regressions.py::ConcurrencyTest`.

```python
# test_concurrent_anchor_runs_keep_both_rollup_entries
                thread.join(0.3)  # with a rollup lock the other run is still waiting after this; without one it has finished

# test_concurrent_spool_replay_appends_each_event_once
                thread.join(5)  # with the bot lock held across the loop the other replay waits
```

Problems:

1. **The regression signal depends on timing.** With correct code both tests pass at any speed, so they do not flake red. If the `.anchors.lock` hold were removed, the first test goes red only when the other thread finishes within 0.3 s. That thread runs a verify and five fsyncs. On a slow runner it does not finish, the main thread writes first, the other merges, and the test passes.
2. **The second comment is wrong.** `spool_replay` does not hold the bot lock across its loop. The property comes from the `_still_pending` guard under the lock. With correct code the other thread is not blocked, so the test costs up to 5 s when that thread is slow, for nothing.
3. **Threads share one pid.** `_tmp_name` is per process (`<path>.<pid>.tmp`). Two threads that write the same target outside a common lock collide on the temp name: `O_EXCL` fails, and `_atomic_write` then removes the other thread's temp file as "a regular leftover of a killed process". The current thread tests avoid this, but the convention is written nowhere.

**Fix.**

- Probe the lock instead of sleeping. `test_k3_k4.py` already does this for the bot lock. Inside the hook that fires when the rollup is about to be written:

  ```python
  with open(os.path.join(self.root, "state", ".anchors.lock"), "a+") as fh:
      with self.assertRaises(OSError):                    # the run in progress must hold it
          gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_EX | gblog.fcntl.LOCK_NB)
  ```

  Then start the second run and join it after the first has finished. No timed `join` is needed, and the regression is caught on any machine.

- For the spool test, drop the thread. Call `gblog.Ledger(self.root).spool_replay("ops")` synchronously inside the hook. It is not blocked, so that is equivalent. Correct the comment to name the guard.
- Add to the `_support` docstring: "gblog is process-safe, not thread-safe (temp names are per pid). Use threads only when every write of both sides happens under the same flock; otherwise use processes."

**Test cases.** After the change, the mutation "remove `with lock:` in `Ledger.anchor`" must fail `test_concurrent_anchor_runs_keep_both_rollup_entries` on every run, and the mutation "drop `_guard=_still_pending`" must fail the spool test on every run.

---

## K6-11 — Every test pays for real `fsync` (Medium)

**Where.** The whole suite. `Ledger.append` issues 3 fsyncs (day file, state temp file, `state/`), 4 when it opens a day file and 2 more for the first record of a chain. `anchor` issues about 7 per chain. A typical `setUp` + `seed("ops", 2)` is about 9. The suite does this thousands of times.

`test_fuzz_verify.py` already states the principle:

```python
    # The templates only have to exist, not to survive a power cut: skip the fsync calls.
    with mock.patch.object(os, "fsync", lambda fd: None):
```

No assertion in the suite depends on data reaching the disk. Durability is asserted by call order (`test_append_fsync_order`, K6-08), which works equally well over a no-op.

**Cost (not measured).** Count first:

```python
# one-off: python -c "…" around unittest.main(module=None, argv=["x", "discover", "-s", "tests"])
n = [0]; real = os.fsync
os.fsync = lambda fd: (n.__setitem__(0, n[0] + 1), real(fd))[1]
```

On tmpfs the saving is nil. On a runner where one fsync costs 2–10 ms (typical for the macOS job), several thousand calls amount to tens of seconds.

**Fix.** In the shared base class (consolidation, phase 1):

```python
        if os.environ.get("GBLOG_TEST_FSYNC") != "1":
            p = mock.patch.object(os, "fsync", lambda fd: None); p.start(); self.addCleanup(p.stop)
```

- Tests that record fsync order wrap whatever `os.fsync` is current, so they are unaffected.
- Children of the spawn-based tests (`_writer`, `_anchorer`) patch it themselves at the top. 320 fsynced appends are the slowest part of `LockingTest`.
- Real-process tests (`run_cli`, examples, scripts) keep real fsync.
- CI: set `GBLOG_TEST_FSYNC: "1"` in one matrix entry (ubuntu, 3.12), so the full suite still runs once with the real call.

**Test cases.** None new. The existing order test and the K6-08 tests are the guard. Verify with `--durations 15` before and after.

---

## K6-12 — About 60 interpreter starts can become in-process runs (Medium)

Counts are by reading:

| Test | Processes | Needed? |
|---|---|---|
| `test_malformed_input.RemainingCrashPathsTest.test_non_dict_aliases_warns_not_crashes` | 18 | No. The stated reason is "the warning is printed once per process", but `main()` starts with `_ALIASES_WARNED.clear()`, so an in-process run prints it once per run as well. `test_security_regressions…test_aliases_type_confusion…` already tests the same thing in-process |
| `…test_surrogate_record_export_and_report` | 6 | The utf-8 half: no. The ascii half: see below |
| `…test_non_ascii_label_on_ascii_stdout` | 7 | See below |
| `test_round3_followups.JsonWriterTest.test_json_outputs_are_valid_on_ascii_stdout` | 4 | See below |
| `test_mutation_gaps.DocumentedButAbsentTest.test_json_outputs_are_valid_on_ascii_stdout` | 2 | Duplicate of the previous row; delete (K6-14) |
| `test_examples.py` | about 80 (each real audit run starts 5–8 interpreters, each `agent-task.sh` run 3) | Yes. The scripts are the subject |
| `test_scripts.py` | about 60 | Yes |
| FIFO/hang guards (`run_cli(..., timeout=60)`) in `test_deep_reviews*.py`, `test_specialist.py` | about 15 | Yes. A hang must not hang the suite. Two are duplicates (K6-14) |

An ASCII stdout does not need a process. `_tolerant_streams()` calls `reconfigure(errors="backslashreplace")`, which a `TextIOWrapper` over `BytesIO` supports. `JsonWriterTest.test_write_json_escapes…` already uses that at unit level. Add:

```python
# tests/_support.py
def run_encoded(argv, root, encoding="ascii"):
    """In-process CLI run whose stdout/stderr are real text streams of the given encoding."""
    out_b, err_b = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(out_b, encoding=encoding, errors="strict", write_through=True)
    err = io.TextIOWrapper(err_b, encoding=encoding, errors="strict", write_through=True)
    with redirect_stdout(out), redirect_stderr(err):
        rc = gblog.main(["--root", root] + list(argv))
    return rc, out_b.getvalue().decode(encoding), err_b.getvalue().decode(encoding)
```

A byte that is not ASCII then fails the decode, which is a stronger check than the current substring assertions.

Keep one real-process test per property, for example `status` on `PYTHONIOENCODING=ascii`, to cover the interpreter's own stream setup. The CI step "CLI with ASCII stdio and a read-only HOME" covers the same end to end.

**Net effect.** About 35 starts removed from `test_malformed_input.py` and 4–6 elsewhere, with no change in asserted behaviour.

---

## K6-13 — Complexity tests: one has no bound, one has a tight bound (Medium)

**Where.**

```python
# tests/test_k3_k4.py  LinearRedactionTest.test_redaction_is_linear_on_crafted_text
        # each of these took minutes with the former expressions
        chain = "token_" * 50000
        self.assertEqual(gblog._redact_env_names(chain), chain)
        self.assertEqual(gblog.redact(chain + "x"), chain + "x")

# tests/test_security_regressions.py  RedactionTest.test_many_pem_begin_markers_do_not_stall_redaction
            proc = subprocess.run([sys.executable, "-c", code, REPO], ..., timeout=5)
```

- The first has no bound. If a rule becomes quadratic again, including any other rule in `_SECRET_PATTERNS` that `redact()` applies to the 300 KB input, the test does not fail. The job runs into `timeout-minutes: 15` with no test name in the log.
- The second bounds interpreter start, import (about 25 regex compilations) and the work at 5 s. The guarded regression costs minutes, so 5 s adds flake risk on a loaded macOS runner and no discrimination.
- `SpoolReplayIndexTest` and `ReportTwinIndexTest` count calls instead of time. That is the robust form and should be the model.

**Fix.** One helper and one rule: count when a counter exists, otherwise use a generous process bound.

```python
# tests/_support.py
def assert_finishes(test, code, seconds=60):
    """Run code (a -c program; argv[1] is the repo) in a child; fail with a clear message on timeout."""
```

Use it for both tests with `seconds=60`. Split the K4-04 test in two: the cheap equality assertions stay in-process on small input (`"token_" * 200`), and the large input runs through `assert_finishes`. Add one line that feeds the same crafted inputs through full `redact()`, so the "linear" claim covers every rule and not only the two rewritten ones.

**Test cases.** Mutation: restore the former JWT expression (`_LinearRule.former`) as the active rule. The suite must fail within about 60 s with the test name, not hang.

---

## K6-14 — Duplicate tests (Low)

The files follow review rounds (`test_deep_reviews.py`, `…_b.py`, `test_mutation_gaps.py`, `test_round3_followups.py`, `test_security_regressions.py`, `test_specialist.py`, `test_k3_k4.py`), not subsystems. The same behaviour was therefore pinned once per round. "Merge" means: move the one extra assertion, then delete.

| Keep | Delete or merge | Note |
|---|---|---|
| `test_round3_followups.AnchorSkipArchivesTest.test_anchor_skip_archives` | `test_mutation_gaps.DocumentedButAbsentTest.test_anchor_skip_archives` | same steps |
| `test_round3_followups.ShellHookTest.test_install_replaces_outdated_hook` | `test_mutation_gaps…test_install_replaces_outdated_hook` | the kept test is byte-exact |
| `test_round3_followups.TokenCounterTest.test_token_counters_untouched` | `test_mutation_gaps…test_token_counters_untouched` | subset |
| `test_round3_followups.TuneWindowTest.test_tune_parses_recent_files_and_keeps_old_decisions` | `test_mutation_gaps…test_effect_table_limited_to_90_days` | subset |
| `test_round3_followups.JsonWriterTest.test_json_outputs_are_valid_on_ascii_stdout` | `test_mutation_gaps…test_json_outputs_are_valid_on_ascii_stdout` | subset; 2 processes |
| `test_mutation_gaps.InternalErrorGapTest.test_debug_env_reraises_from_verify_all` (CLI level) | merge `test_round3_followups.DebugGuardTest` into it | keep its "without the variable: FAIL entry" half |
| `test_specialist.HugeGapTest` (2 tests) | `test_deep_reviews_b.SeqGapTest` (2 tests) | merge `len(json.dumps(breaks)) < 2000` and the direct `_break_shape_ok` assertion |
| `test_specialist.AnchorVerifyIsolationTest.test_anchor_isolates_a_non_regular_history_file` | `test_deep_reviews_b.ChainIsolationTest.test_anchor_write_failure_in_one_chain_does_not_stop_the_others` | merge the rollup-content assertions |
| `test_deep_reviews_b.StateRepairTest.test_state_seq_mismatch_warns_and_rebuild_repairs` | `test_deep_reviews.AppendRecoveryTest.test_state_seq_mismatch_warns_and_can_be_rebuilt` | merge "a head that is not in the chain is still refused" |
| `test_deep_reviews.AppendRecoveryTest.test_non_string_state_head_is_adopted_like_unreadable` | `test_deep_reviews_b.StateRepairTest.test_non_string_state_head_is_adopted_and_can_be_rebuilt`, `test_integrity.StateWitnessTest.test_non_string_state_head_is_a_warning` | the kept test covers verify, append, rebuild and `update_state` |
| `test_deep_reviews.CliLayerTest.test_capture_browser_negative_hours_is_usage_error` | `test_deep_reviews_b.BrowserWindowTest.test_negative_hours_is_a_usage_error_and_writes_nothing` | merge "no state file written" |
| `test_deep_reviews.CliLayerTest.test_future_marker_is_ignored_with_notice` | `test_deep_reviews_b.BrowserWindowTest.test_future_marker_is_ignored_with_a_notice` | same asserts |
| `test_deep_reviews_b.SideFileTypeTest.test_fifo_archive_is_a_problem_not_a_hang` | `test_deep_reviews.ArchiveTest.test_fifo_at_archive_address_fails_not_hangs` | identical; 1 process |
| `test_mutation_gaps.AppendGapTest.test_planted_tmp_symlink_is_refused_not_followed` | `test_security_regressions.SymlinkTest.test_state_tmp_symlink_is_not_followed` | merge "state did not become a symlink" (already present) |
| `test_mutation_gaps.SpoolGapTest.test_planted_break_ack_stays_in_the_spool_and_is_reported` | `test_security_regressions.MiscHardeningTest.test_spool_replay_never_appends_a_break_ack` | superset |
| `test_integrity.VerifySnapshotTest.test_anchor_written_after_snapshot_does_not_fail_verify` | `test_security_regressions.ConcurrencyTest.test_verify_is_not_confused_by_an_anchor_written_after_its_snapshot` | same hook, same asserts |
| `test_redaction_extended.ExtendedRedactionTest.test_existing_expectations_unchanged` | `test_gblog.RedactionPatterns.test_bearer_without_colon_and_query_params` | the same six assertions |
| `test_mutation_gaps.VisibilityLookupTest.test_github_remote_classifies_by_parsed_host` | `test_capture_sync.RemoteRegexTest.test_regex_rejects_lookalike_hosts`, `test_gblog…test_remote_visibility_parses_urls` (regex half) | the old tests pin `_GITHUB_REMOTE_RE`, which no decision uses any more ("kept for callers that import it"). Keep one two-line import-compatibility test |
| `test_mutation_gaps.AliasOutputGapTest` (2 tests) | `test_security_regressions.OutputInjectionTest.test_alias_display_name_cannot_forge_a_heading` | the old test cannot tell refusal from neutralisation |
| `test_security_regressions.ReaderRobustnessTest.test_aliases_type_confusion_neither_crashes_nor_loses_events` | merge `test_malformed_input…test_non_dict_aliases_warns_not_crashes` into it | see K6-12 |

That is about 20 test methods and about 22 process starts removed, with no assertion lost if the "merge" notes are followed. The sync-cache tests (`test_sync_cache_does_not_hide_a_changed_file`, `test_sync_keeps_no_verification_cache`) belong to the G-work and are not judged here.

Helper duplication, by reading:

| Helper | Copies |
|---|---|
| base class with temp root and env patch | 12 (`Base` ×7, `IntegrityBase`, `SafetyBase`, `CliBase`, `MalformedBase`, `_Base`) |
| Chrome-history builder | 9 (three signatures: seconds ago, raw WebKit time, epoch) |
| hash-valid raw chain writer | 7 (`write_raw_chain` ×3, `raw_chain` ×2, `_legacy_chain`, `crafted`) |
| `git()` wrappers | 8 |
| bash hook harness with a fake `gblog` | 4 |
| `_AfterRelease` | 3 |
| `_Body` (urlopen stand-in) | 3 |
| git environment dict | 3 definitions besides `_support.GIT_ENV` |

---

## K6-15 — Script checks are split between `ci.yml` and `test_scripts.py` (Low)

**Where.** The `scripts` and `restricted-env` jobs of `.github/workflows/ci.yml` hold about 200 lines of bash assertions.

| Check | `ci.yml` | `tests/test_scripts.py` |
|---|---|---|
| launcher through symlinks | yes | `test_bin_launcher_through_symlink` |
| install into temp HOME, rerun idempotent | yes | `test_install_from_checkout_into_temp_home` |
| relative root, legacy rc lines, `.bash_profile`, different unmarked root | yes | four tests |
| piped install ignores `./gblog.py` | yes | `test_install_piped_ignores_cwd_gblog_py` |
| `GBLOG_SHA256` | yes | `test_install_checksum_is_enforced` |
| `CDPATH` for launcher and installer | yes | none |
| https source never redirected (stub curl) | yes | none; only the grep below |
| `install.sh` with a read-only HOME | yes | none |

The YAML-only checks run on ubuntu only: not on macOS, where BSD userland differs, not on the 3.8 image, and not locally. The one unit test for the redirect rule asserts too little:

```python
# tests/test_security_regressions.py  MiscHardeningTest.test_install_restricts_redirect_protocols
        self.assertIn("--proto-redir", self.read(os.path.join(REPO, "install.sh")))
```

A comment that contains the string satisfies it.

**Fix.** Port the three YAML-only checks to `test_scripts.py`:

- the stub-curl script, verbatim, as a fixture string;
- the `CDPATH=/usr` cases;
- the read-only HOME case, with `skipTest` when the directory is still writable (root).

Replace the grep test with the stub-curl test. Then reduce the YAML to the checks that need the workflow context: shellcheck, the smoke test through `./bin/gblog`, and the CHANGELOG/version check.

**Test cases.** `test_https_source_is_not_redirected_to_another_scheme`: the stub records its arguments. Assert `--proto-redir` and `=https` are present for an `https://` `GBLOG_REPO` and absent for `file://`, and that the installed bytes equal `gblog.py`. Add `test_install_ignores_cdpath` and `test_install_with_read_only_home`.

---

## K6-16 — The private-mode test checks a hand-written list under one umask (Low)

**Where.** `tests/test_append_safety.py::HygieneTest.test_new_files_are_private` sets `umask 0o022` and checks an explicit list: day file, state, lock files, anchors, spool, alert and replayed files. It does not check:

- `archive/<bot>/<day>/<sha>.gz` ("always a private (0o600) file", comment in `archive_file`);
- `reports/*.md` and `tuning/*.md`, `tuning/proposals.json`;
- `state/aliases.json`, `state/aliases.lock`, `state/.anchors.lock`;
- `state/capture-files-*.json`;
- `README.md`, `.gitignore`;
- the directories `archive/…` and `spool/<bot>/replayed`.

`SyncFilesystemTest.test_git_objects_are_private` walks `.git` properly. The ledger tree itself has no such walk.

**Reproduction of the gap.** In `archive_file` change `_atomic_write(full, _emit, mode="wb", keep=False, …)` to write through `open(full, "wb")`. With umask 022 the evidence file becomes 0644 and no test fails.

**Fix.** One workflow test that runs: `init`, `append`, `archive`, `anchor`, a refusal, `spool replay`, `report`, `tune`, `capture files`, `alias add`. Then walk the whole root:

```python
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    self.assertEqual(stat.S_IMODE(os.lstat(full).st_mode) & 0o077, 0, full)
```

Run it under `umask 0o000` and `0o022` (`subTest`), restoring with `addCleanup(os.umask, old)`. Keep the existing "an existing file keeps its mode" assertions.

I found no test that fails under an unusual umask. Mode assertions either mask with `& 0o077` or follow an explicit `chmod`/`fchmod`.

---

## K6-17 — Skips are invisible; some platform properties have only mock coverage (Low)

**Where.**

- About 25 tests are skip-guarded (`skipUnless(GIT, …)`, `hasattr(os, "mkfifo")`, `shutil.which("bash")`, `SHELLCHECK`, `self.skipTest("symlinks are not supported here")`, curl `file://`). A container without git or bash runs green with all sync, hook and script tests missing. CI prints no skip count and has no threshold.
- `SmallConsistencyTest.test_case_sensitive_filesystem_keeps_two_chains` skips on a case-insensitive file system. The opposite case, the actual K1-09 and K3-03 behaviour, is tested only with mocks (`_disk_name` with a patched `listdir`; `_own_name` with `os.path.samefile` patched to `True`). The CI matrix has a `macos-latest` entry whose default volume ignores case. Nothing runs the real thing there.

**Fix.**

1. CI: after the main "Unit tests" step on ubuntu, fail when the number of skips exceeds a pinned number.

   ```bash
   python -m unittest discover -s tests 2>&1 | tee "$RUNNER_TEMP/ut.log"
   skipped="$(grep -oE 'skipped=[0-9]+' "$RUNNER_TEMP/ut.log" | cut -d= -f2)"
   [ "${skipped:-0}" -le 2 ] || { echo "::error::${skipped} tests were skipped on Linux (expected at most 2)"; exit 1; }
   ```

2. A real-file-system test for case-insensitive volumes. It runs on the macOS job.

   ```python
   @unittest.skipUnless(_case_insensitive(tempfile.gettempdir()), "needs a file system that ignores letter case")
   def test_write_uses_the_spelling_of_the_existing_directory(self):
       self.seed("Ops", 1)
       rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.root)
       self.assertEqual(rc, 0, out + err)
       self.assertEqual([r["bot"] for r in self.chain("Ops")], ["Ops", "Ops"])
       res = self.ledger.verify("Ops")
       self.assertEqual((res["ok"], res["warnings"]), (True, []))
   ```

---

## K6-18 — Weak assertions (Low)

| Test | Assertion | Problem | Replace with |
|---|---|---|---|
| `test_gblog.AliasArchiveSyncTest.test_sync_refuses_public_remote` (end) | `self.assertNotEqual(rc, 3)` | the `--allow-public` push to the local bare repository may fail in any way | `assertEqual(rc, 0)`, `" pushed"` in `out`, commit count of the bare repository above 0 |
| `test_capture_sync.SyncTest.test_unknown_visibility_github_refused` (loop) | `assertNotIn(rc, (2, 3, 4))` | exit 1 for any reason passes; a real `git push` to an https URL is attempted and relies on `GIT_ALLOW_PROTOCOL` | delete the loop; `test_mutation_gaps…test_unknown_visibility_override_really_pushes` covers the override |
| `test_gblog.CaptureCoverageTuneTest.test_tune_proposals_apply_and_effect` (end) | `assertNotEqual(rc, 0)` for an unknown id | — | `assertEqual(rc, 2)` and the message |
| `test_security_regressions.RemoteGuardTest.test_sync_does_not_silently_retarget_origin` | return value of the second `sync` ignored | — | `assertEqual(rc, 2)` and `--replace-remote` in `err` |
| `test_security_regressions.FutureTailTest.test_clamped_records_still_appear_in_todays_report` | `rc` unchecked | — | `assertEqual(rc, 0)` |
| all CLI tests | `assertNotIn("Traceback", err)` | vacuous in-process (K6-01) | covered by the strict runner |

Related: no test asserts that the suite makes no network or `ssh` call. By reading, none does: every push target is a local path, and the lookups are patched. To make the property enforced, patch `socket.socket.connect` in the shared base to raise `AssertionError("network use in a test")`. Subprocess-based tests are not covered by this; they use local remotes only.

---

## K6-19 — `test_meta.py` gaps (Low)

1. **Ids without a module.** `PENDING.txt` entries are `Class.test_name`, and `marked_tests()` merges all files into one set. `Base` exists in seven files, and `FixturePinTest`, `RedactionTest`, `CaptureTest` and `ConcurrencyTest` exist in more than one. A marker on one of them cannot be told from its namesake, and a second marker with the same id would hide behind the first. Use `module.Class.test_name` (file stem) in the list and in `marked_in_source`.
2. **Silently shadowed tests.** Two `def test_x` in one class, or two classes of one name in one module, drop the earlier one from the run without notice. I found none by inspection, but nothing checks it. Add an AST check: within a class, test method names are unique; within a module, class names are unique.
3. **Comment markers** (K6-02): `# bug:` / `# pending:` requires `expectedFailure`.
4. **Helper bypass** (K6-05, K6-06): no direct `subprocess.run(["git"…` and no `unittest.TestCase` base outside the allow-list.

**Test cases.** Extend `_SAMPLE` with a duplicated method and a duplicated class, and assert the checker reports both.

---

## K6-20 — The fuzz corpus never checks that `append` and `verify` agree (Low)

**Where.** `tests/test_fuzz_verify.py` derives 300 mutated trees and checks `verify` (default and strict) and five read-only commands. It never calls `append` on a mutated tree. The two functions share the tail and state rules (`_self_consistent`, `_usable_head`) but implement them separately. Earlier rounds found disagreements exactly there (D2-04, D2-05).

**Fix.** One more test method over the same cases, at no generation cost. For each case, on a copy:

1. `before = verify(bot)`.
2. Try `append(bot, note)` with stderr captured. Classify: appended / refused (`TailMismatchError`) / error.
3. `after = verify(bot)`.

Properties:

- **P1 (no laundering).** If `before` is FAIL and the append succeeded, `after` is FAIL too. The one documented exception is the interrupted-append shape: state names the record just before a self-consistent tail. Recognise it explicitly, which the `state_head: earlier` mutator produces when it picks the second-to-last record, and assert the `recovered state/…` notice in that case.
- **P2.** If the append was refused, the log bytes of the chain are unchanged and exactly one spool file exists.
- **P3.** If `before` is OK with a state warning (missing, unreadable, reset to genesis) and the append succeeded, the new record carries `state_adopted` when state was missing or unreadable, and `after` warns about it.
- **P4.** `append` never raises anything other than `TailMismatchError`.

This also exercises `_tail_record` on every mutated tail (truncated, invalid UTF-8, CRLF, BOM), which only hand-written cases cover today.

---

## Consolidation plan

Goal: the same behaviours pinned, fewer copies, one base class, faster runs. No test id disappears without a row in the K6-14 table.

**Safety net (set up before any move).**

1. **Test-id inventory.** Save the sorted list from `python -m unittest discover -s tests -v 2>&1 | grep -E ' \.\.\. '`. After each phase the diff must equal the planned removals and renames.
2. **Executed-line set.** One-off, stdlib only: `python -m trace --count --missing --coverdir <dir> --module unittest discover -s tests`, before and after. The set of executed lines of `gblog.py` may only grow. Tracing is slow and blind to child processes; use it as a refactoring check, not in CI.
3. **Mutation spot list.** Each of these must be killed after every phase:
   - the K6-01 `raise` in `_anchor_checks`;
   - `_GIT_ENV_DROP = ()`;
   - `write_state` before `_append_line`;
   - the `_fsync_dir` line in `archive_file` removed;
   - `return False` removed in `update_state`;
   - the `with lock:` in `Ledger.anchor` removed;
   - `_guard=_still_pending` removed;
   - the former JWT rule restored;
   - the `tail["hash"] != res["head"]` re-check removed;
   - the `(st_dev, st_ino)` check in `_snapshot_lines` removed.

**Phase 0 — strengthen without moving (K6-01, -02, -05, -06, -19).** The strict `run`, the env and git scrub, the meta checks and the comment clean-up. Tests that turn red here are real findings.

**Phase 1 — one support module, same test ids.** Add to `tests/_support.py`:

- `LedgerCase(unittest.TestCase)`:
  - set-up: realpath'd temp base, `root`, `ledger`, env scrub, git isolation, optional no-op fsync, network guard;
  - helpers: `path`, `seed`, `chain`, `raw_state`, `write`/`read`, `log_bytes`, `break_state`, `restore_state`, `pending`, `quiet`, `snapshot`, and `fifo`/`symlink`/`hardlink` (each skips when unsupported);
- `write_raw_chain(root, bot, extras, day, state=True, bot_field=None)`;
- `chrome_history(dir, visits, time="ago" | "webkit" | "epoch")` and `firefox_history`;
- `git`, `bare_repo`, `commit_count`;
- `hook_harness(test, fake_script)` returning a `run_hook(cmd, flags=(), **env)` callable;
- `AfterRelease` and `after_release(test, action)`;
- `FakeHttpBody`;
- `frozen_clock`, `fs_events`, `run_encoded`, `assert_finishes`, `crash_at`.

Replace the per-file copies mechanically. The inventory diff must be empty.

**Phase 2 — remove duplicates.** Apply the K6-14 table, moving the "merge" assertions first. Inventory diff: about 20 ids removed, none added.

**Phase 3 — add the missing tests.** In value order: K6-03 (`test_crash_points.py`), K6-04, K6-09 (b) and (a), K6-08, K6-05 tests, K6-09 (c)–(e), K6-20, K6-16, K6-17.

**Phase 4 — regroup by subsystem (optional, last).** Only after phases 0–3 are merged, so that history stays readable.

| New file | Takes from |
|---|---|
| `test_append.py`, `test_recovery.py` | `test_append_safety.py`, D2 parts of `test_deep_reviews.py`, `AppendGapTest`, `SpoolGapTest`, `SpoolReplayIndexTest` |
| `test_verify.py`, `test_anchor.py` | `test_integrity.py`, D1 parts of `test_deep_reviews_b.py`, `IntegrityGapTest`, `StreamingVerifyTest`, `test_chain_repair.py` |
| `test_fs_adversary.py` | K2 parts of `test_specialist.py`, `SymlinkTest`, `SideFileTypeTest`, FIFO tests |
| `test_sync.py` | sync classes of `test_capture_sync.py`, `SyncDeepTest`, `SyncGapTest`, `RemoteGuardTest`, `SameRemoteTest` |
| `test_capture.py`, `test_tune.py`, `test_redaction.py` | the capture, tune/coverage and redaction classes of all round files |
| `test_cli.py`, `test_readers.py` | `test_cli_ux.py`, `ReaderGapTest`, `ReaderAliasTest`, report rendering |
| `test_concurrency.py`, `test_crash_points.py` | `LockingTest`, `ConcurrencyTest`, new tests |

Finding ids stay searchable: every moved test keeps its `# D2-01`-style comment.

Update the documents that name test files:

- CHANGELOG: "`tests/test_specialist.py` covers them" and "`tests/PENDING.txt`";
- `examples/README.md`: `tests/test_examples.py`;
- DESIGN section 13 and `ci.yml`: `tests/bench_large_ledger.py`.

`test_fuzz_verify.py`, `test_examples.py`, `test_scripts.py`, `test_meta.py`, `test_malformed_input.py` and `tests/fixtures/` keep their names.

**Expected result (estimate, not measured).** About 20 fewer tests, about 60 fewer interpreter starts, and real fsync confined to one CI entry. The crash table of DESIGN section 4 gets an executable counterpart, and a crash can no longer pass as an integrity FAIL anywhere in the suite.