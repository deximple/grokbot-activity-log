# Specialist review K10 — 2026-11-16

**Area.** Concurrency and crash safety: interleaved appends from several processes, lock lifetime, partial writes, fsync ordering, rename atomicity, power loss between a record and its side files, and what `gblog verify` says about each resulting state.

**Tree.** `gblog.py` 0.6.1 (unreleased), with round-4 items G1-01 … G1-12 in the code. Those items are not repeated here.

**Method.** The code was read, not run. Reproductions are written as test recipes and have not been executed. Time windows are estimates.

**Severity.**

- High: a sequence of crashes alone, with no tampering, ends in a state that gblog reports as tampering or that needs a restore from backup.
- Medium: duplicated or unrecorded events, or a backup that does not verify.
- Low: hardening.

## Summary

| Id | Severity | Finding |
|---|---|---|
| K10-01 | High | Roll-forward and tail adoption make `state/<bot>.json` durable before the adopted log line is durable. A failed `fsync` leaves a line that the next append adopts. |
| K10-02 | High | `sync` commits a tree that is not a point-in-time snapshot. An append during `git add` gives a commit whose state is ahead of its log, so a clone fails `verify`. |
| K10-03 | Medium | An interrupted `capture files` or `capture browser` run re-imports what it already appended. The duplicates are permanent. |
| K10-04 | Medium | SIGTERM and SIGHUP skip every cleanup path. Ctrl-C can land between the durable line and the state replace. |
| K10-05 | Medium | `install.sh` replaces the running executable in place. A truncated copy can exit 0 without logging. |
| K10-06 | Medium | `sync --remote` changes `origin` before it writes the record that must document the change. |
| K10-07 | Medium | `verify` reports three crash states with tamper wording and no hint: torn tail, a recorded FAIL for a healed interrupted append, torn anchor-history line. |
| K10-08 | Low | Lock lifetime: `verify` reads up to 400 rollups under the shared lock, `state rebuild` and the `seal` guard parse the whole chain under the exclusive lock, and lock waits are unbounded and anonymous. |
| K10-09 | Low | Temporary names are unique per pid only. The "stale leftover" branch can remove a live writer's file, and stale files are never collected. |
| K10-10 | Low | Renames in `spool replay` and alert resolution are not made durable. The first hook install is not atomic. |
| K10-11 | Low | Refines D2-11: parents of newly created directories are never fsynced. Lists the two `verify` states this produces. |
| K10-12 | Low | Refines D2-13: a concrete check for a lock file replaced while it is held. |

## Crash points and what `verify` says today

| # | Interruption | On disk afterwards | `verify` | Next `append` | Assessment |
|---|---|---|---|---|---|
| 1 | Before the line is written | Nothing new | OK | Normal | Correct |
| 2 | Power loss during the line write | Partial or NUL-filled last line; state names the record before it | FAIL `not valid JSON`, no hint | Exit 4 | By design; hint missing (K10-07) |
| 3 | Kill after `os.write`, before `os.fsync` returns | Whole line in the page cache only; state one behind | FAIL head mismatch, plus `! … one record behind` | Rolls forward, fsyncs state first | K10-01 |
| 4 | Kill after the file fsync, before the directory fsync of a new day file | As 3; later appends see `size != 0` and never fsync the directory | As 3 | As 3 | K10-01 |
| 5 | Kill between the durable line and the state replace | State one behind | FAIL plus hint | Rolls forward | Correct |
| 6 | Power loss after a roll-forward or adoption wrote state, before the adopted line reached disk | State ahead of log | FAIL `state tampered or log truncated`; `state rebuild` refuses | Exit 4 until restore | K10-01 |
| 7 | Power loss after the first record of a new chain or archive directory | State without `logs/<bot>`; record without file | `… but logs/<bot> is missing`; `archived file missing` | Exit 4 | K10-11 |
| 8 | Kill or error during a capture run | Records without snapshot or marker | OK | – | Duplicates on the next run (K10-03) |
| 9 | Kill between a replayed append and the spool-file move | Record plus pending spool file | OK | Guard skips, file is moved | Correct |
| 10 | Power loss after `spool replay` | Moved files and the resolved alert can return | OK; `status` shows the ALERT again | – | K10-10 |
| 11 | Kill between the history line and `anchors/<bot>.json` | History newer than anchor | OK | – | Correct |
| 12 | Power loss during the history line write | Torn history line | FAIL `corrupt anchor history line`, permanent | – | K10-07 |
| 13 | Kill between `_set_origin` and its record | `origin` changed, no record | OK, silent | – | K10-06 |
| 14 | Kill between `break_ack` and the witness note | Ack only | PASS plus `! witness note missing` | – | Known (D1-11) |
| 15 | Kill between `state rebuild`'s state write and its note | No note | OK, silent | – | Known (D2-12) |
| 16 | Append while `sync` runs `git add` | Live ledger fine; commit torn | Live OK; clone FAIL | – | K10-02 |

---

## K10-01 (High) — State is made durable before the line it names

**Where.** `gblog.py`: `Ledger.append` (roll-forward branch), `Ledger._append_line`, `Ledger.update_state`, `Ledger.rebuild_state`.

```python
# Ledger.append
if recovered == "interrupted":
    healed = dict(seen[0] or {})
    healed.update({"seq": seq, "head": head})
    ...
    self.write_state(bot, healed)      # tmp fsync, rename, directory fsync
```

```python
# Ledger._append_line
            except BaseException:      # rollback covers the write loop only
                ...
                raise
            os.fsync(fd)               # a failure here leaves the whole line in the file
        finally:
            os.close(fd)
        if size == 0:                  # only a new or empty file gets its directory entry synced
            _fsync_dir(os.path.dirname(path))
```

**Problem.** The healthy path keeps the invariant "line durable, then state". The recovery paths do not. A tail can be in the file without being durable in three ways:

1. The appender was killed after `os.write` and before `os.fsync` returned. This is the longest step of an append.
2. `os.fsync` raised. EIO can do this, and so can ENOSPC or EDQUOT on file systems that report them at fsync time. The rollback does not cover the fsync, so the line stays.
3. The appender created a day file, fsynced it, and was killed before `_fsync_dir`. Every later append sees `size != 0` and skips the directory fsync, so the entry is never synced by gblog.

The next writer treats that tail as an interrupted append. It writes and fsyncs a state file that names the tail's hash. The tail itself is flushed only if the new record lands in the same file and the call gets that far. It is not flushed when:

- the guard skips (`spool replay` after a killed replay);
- an explicit past `--ts` raises `TimestampError` after the heal;
- the target day file is refused as non-regular;
- the new record goes to another day file (UTC midnight in between);
- the adoption happens in `update_state` (`sync`, `capture browser`) or in `rebuild_state`, which never fsync a log file.

A power loss before the kernel writes the page back (typically 5 to 30 seconds) then leaves state ahead of the log:

- `verify` prints `state/<bot>.json: head does not match last record (state tampered or log truncated)`.
- Every append exits 4.
- `state rebuild` answers `restore the log from backup`.

`docs/DESIGN.md` section 4 says "State *ahead* of the log cannot come from a crash inside `append`." With the current code it can.

**Reproduction.**

Part A, fsync failure adopted:

1. Seed one record in `ops`.
2. Patch `gblog.os.fsync` to raise `OSError(EIO)` on its first call, then append "two". The call raises.
3. The day file now holds "two" and state says seq 1.
4. Append "three". It prints `notice: recovered state/ops.json after an interrupted append` and chains onto "two", a line whose fsync failed.

Part B, ordering:

1. Seed two records.
2. Append a hash-valid third line straight to the day file, as `raw_line` in `tests/test_deep_reviews_b.py` does. This is the disk state after a kill between write and fsync.
3. Record `os.fsync` and `os.replace` events as `test_append_fsync_order` does, then run `ledger.append`.
4. Today the first three events are the state temp-file fsync, the replace onto `state/ops.json`, and the `state/` fsync. The day file is fsynced only afterwards.

**Fix.**

1. Add a helper used only in recovery paths:

   ```python
   def _fsync_path(path: str) -> None:
       """Make an existing file and its directory entry durable."""
       flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
       try:
           fd = os.open(path, os.O_RDWR | flags)      # Windows needs write access for fsync
       except OSError:
           fd = os.open(path, os.O_RDONLY | flags)
       try:
           os.fsync(fd)
       finally:
           os.close(fd)
       _fsync_dir(os.path.dirname(path))
   ```

2. In `append`, when `recovered` is non-empty, call `_fsync_path(seen[1])` (the tail's day file, already carried in `seen`). Do it before `write_state(healed)` for `"interrupted"`, and before `_append_line` for `"missing"` and `"unreadable"`.
3. In `update_state`, call it for the tail's path before the seeded state is written. In `rebuild_state`, call it for the last log file before `write_state(new)`.
4. In `_append_line`, move `os.fsync(fd)` into the guarded region. A failed fsync then takes the same rollback as a failed write. On Linux a second fsync is not a reliable retry after a failed one, so the line must not stay.
5. Leave the healthy path alone. `test_append_fsync_order` pins its event list.
6. In `docs/DESIGN.md` section 4, add a "kill before fsync" row to "Crash outcomes" and state the rule "a state file never names a line that was not fsynced first".

**Tests** (`tests/test_k10.py`, `DurableTailTest`).

- `test_roll_forward_fsyncs_the_tail_before_state`: part B. Assert the day file's `(st_dev, st_ino)` and `logs/ops` are fsynced before the first replace onto `state/ops.json`.
- `test_guard_skip_still_makes_the_tail_durable`: the setup of `test_replay_guard_skip_heals_state`. Assert the same order when `spool_replay` only moves the file.
- `test_update_state_and_rebuild_fsync_the_adopted_tail`: remove state, call `update_state`. Then set state behind and call `rebuild_state`. Assert the order in both.
- `test_failed_fsync_rolls_the_line_back`: part A. Assert the file size is unchanged, state is unchanged, the next append prints no `recovered` notice, and `verify` is OK with two records.
- `test_healthy_path_events_unchanged`: `test_append_fsync_order` passes unmodified.

---

## K10-02 (High) — A `sync` commit is not a snapshot; a clone can show "state tampered or log truncated"

**Where.** `gblog.py`, `sync()`:

```python
added = _git(root, "add", "-A", *add_paths, check=False)   # no chain lock is held
...
drift = _sync_drift(root, checked)                         # logs: "committed blob is a prefix of the file"
```

**Problem.** `git add` reads the files one after another. Tracked files are refreshed in index order, which is path order, so `logs/<bot>/<day>.jsonl` is read before `state/<bot>.json`. An append that lands between the two reads is staged as "log at N, state at N+1". The shell hook appends at every prompt.

`_sync_drift` accepts this, because the committed log blob is a prefix of the file on disk. It does not look at state at all. `sync` then prints `pushed`.

The live ledger is fine. The pushed commit is not:

```
$ git clone <backup> audit && gblog --root audit verify --all
FAIL  ops   41 records
      - state/ops.json: head does not match last record (state tampered or log truncated)
```

This is the verdict an auditor is told to treat as truncation (`SECURITY.md`, "Auditing a clone of the backup"). The opposite order happens for files staged in git's second pass: state at N, log at N+1, reported as FAIL plus the "one record behind" hint.

The window is the time git needs to hash everything that sorts between the day file and the state file. On a ledger with large day files or reports that is tens of milliseconds or more per run.

**Reproduction** (deterministic).

1. Seed `ops`. Run `sync --no-push` once.
2. Append one more record so the day file is modified.
3. Wrap `gblog._git`. On the `add -A` call, run the real `git add -A logs`, then `ledger.append("ops", …)`, then the real `add -A` for the remaining paths.
4. Run `sync --no-push`. It exits 0.
5. `git clone` the root into a temporary directory and run `Ledger(clone).verify("ops")`. Today `ok` is `False` with the state-head problem.

**Fix.** Keep the existing first pass. Add a second pass that stages one chain at a time while holding that chain's shared lock, so log and state are read in one quiet moment:

```python
for chain in ledger.valid_chains():
    rels = [p for p in ("logs/%s" % chain, "state/%s.json" % chain, "state/%s.alert.json" % chain,
                        "anchors/%s.json" % chain, "anchors/%s.history.jsonl" % chain,
                        "archive/%s" % chain, "spool/%s" % chain)
            if os.path.lexists(os.path.join(root, *p.split("/")))]
    if not rels:
        continue
    with ledger.lock(chain, shared=True):          # appends to this chain wait for one git call
        _git(root, "add", "-A", "--", *rels, check=False)
        mine = [rel for rel in forced if rel in rels or rel.startswith(tuple(r + "/" for r in rels))]
        if mine:
            _git(root, "add", "-f", "--", *mine, check=False)
```

- This keeps the rule "one bot lock at a time". `sync` holds no lock while it appends its own bookkeeping records.
- git re-hashes only files whose stat changed since pass one, so the extra cost is one short git call per chain.
- Add the `sync` row to the lock table in `docs/DESIGN.md` section 5.

**Tests** (`SyncSnapshotTest`).

- `test_commit_is_consistent_when_an_append_races_git_add`: the reproduction. Assert the clone verifies OK.
- `test_chain_lock_is_held_while_its_files_are_staged`: in the `_git` wrapper, whenever a second-pass call names `logs/ops`, try `flock(LOCK_EX | LOCK_NB)` on `state/ops.lock`. It must fail. The same probe on `state/zzz.lock` must succeed.
- `test_sync_output_and_exit_codes_unchanged`: the existing `SyncTest` and `SyncDeepTest` suites pass unmodified.

---

## K10-03 (Medium) — An interrupted capture run appends its records again

**Where.** `gblog.py`, `capture_files` and `capture_browser`:

```python
# capture_files
for kind, rel in changes[:limit]:
    ledger.append(bot, {...})          # no try/except; the snapshot is saved only after the loop
if len(changes) > limit:
    ...
    save_snapshot(pending, ...)
else:
    save_snapshot()
```

```python
# capture_browser: the marker of a history file is saved after its last row,
# or from `except BaseException` when an append raises
if newest != markers.get(path):
    markers[path] = newest
    changed = True
    save_markers(final=False)
```

**Problem.** Records and their progress file are not updated together.

- `capture files`: any exception part-way leaves k records in the chain and the old snapshot. The next run logs the same k changes again. Examples are ENOSPC on the 50th append, Ctrl-C, or a refusal after the first record.
- `capture browser`: exceptions are handled, a kill is not. The weekly routine imports a week of history (`--hours 168`) at two to three fsyncs per row, which is the kind of run a timeout terminates (K10-04). Each terminated run re-appends its whole prefix on the next attempt. If the import never fits the timeout, every attempt adds the same rows again.

The duplicates cannot be removed. `coverage` counts each as a separate observed action. One self-report matches one of them and the rest are "unreported", so coverage falls and `tune` proposes a `logging` fix for a gap that does not exist.

This is not D2-07 / D4-17, which is about refused events being imported again.

**Reproduction.**

1. Take a baseline with `capture_files`. Create `a.txt` … `e.txt`.
2. Patch `Ledger.append` to raise `OSError(ENOSPC)` on its third call. Run `capture_files`. It raises.
3. Run it again without the patch.
4. The chain holds seven file records. `a.txt` and `b.txt` are `created` twice.

**Fix.**

1. `capture_files`: count completed appends. Every 25 appends, and in an `except BaseException` handler, write the snapshot the code already knows how to write for the `--limit` case: `previous` advanced by the changes logged so far, with the rest in `pending`.
2. In the handler, check the tail record first. The failing append may have written its line (ENOSPC on the state write). If the tail has `capture == "files"` and the same `target` and `detail`, count it as done. `archive_file`'s cleanup uses the same test.
3. With zero completed appends, write nothing. `test_snapshot_kept_when_append_refused` pins that.
4. `capture_browser`: save `markers[path] = newest` every 25 appended rows. Save only when the next row's visit time is strictly greater than `newest`. The resume query is `visit_time > since`, so saving in the middle of a run of equal times would drop the rest of that run.
5. With K10-04, SIGTERM takes the exception path. Only SIGKILL and power loss can then duplicate, and at most 25 rows.

**Tests** (`CaptureProgressTest`).

- `test_files_partial_failure_is_not_logged_twice`: the reproduction. Assert each of the five targets appears exactly once after the second run.
- `test_files_failing_append_that_wrote_its_line_counts_as_done`: fail the state write of the third append. Assert no duplicate of the third change.
- `test_files_refusal_at_first_append_leaves_snapshot_bytes`: unchanged behaviour.
- `test_browser_marker_is_saved_in_batches`: 60 visits; record `update_state` calls; assert at least two saves before the end. Then simulate a kill by aborting after row 40 with an exception that the handler is patched to ignore. Assert the rerun adds at most 25 duplicates.
- `test_browser_batch_boundary_keeps_equal_visit_times`: three rows share one `visit_time` across a batch boundary. All three are imported exactly once.

---

## K10-04 (Medium) — SIGTERM skips every cleanup path; Ctrl-C can split line and state

**Where.** `gblog.py`, `main()`:

```python
    except KeyboardInterrupt:
        return 130
```

No handler exists for SIGTERM or SIGHUP. These are the signals that `timeout`, systemd, a closing terminal and a routine runner send.

**Problem.**

1. Several repairs run only when an exception unwinds the stack:
   - `_atomic_write` removes its temp file;
   - `_append_line` rolls a partial write back;
   - `capture_browser` saves the markers of rows already imported;
   - `sync` restores `origin` when its record cannot be written;
   - `archive_file` removes an unchained file.

   The default SIGTERM action ends the process without unwinding. Each kill leaves `<path>.<pid>.tmp` files and the duplicate or unrecorded states of K10-03 and K10-06.
2. SIGINT is delivered as `KeyboardInterrupt` at any bytecode boundary, including between `_append_line` and the final `write_state`. The result is state "one behind", which `verify` reports as FAIL until the next append. It is avoidable for a signal the process handles itself.

**Reproduction.**

1. Run `timeout -s TERM 2 gblog capture browser --bot ops --history <large History>`.
2. Run the same command again without the timeout.
3. The rows of the first two seconds are in the chain twice.

For point 2, patch `Ledger.write_state` to raise `KeyboardInterrupt` once. That is today's Ctrl-C at that point: exit 130 and state one behind.

**Fix.**

1. In `main()`, when running in the main thread, install handlers for SIGTERM and SIGHUP (where present) that raise `_Terminated(BaseException)` carrying the signal number. Restore the previous handlers in a `finally`; tests call `main()` in-process. Map `_Terminated` to exit `128 + signum`, so SIGTERM gives 143.
2. In `Ledger.append`, block SIGINT, SIGTERM and SIGHUP with `signal.pthread_sigmask(SIG_BLOCK, …)` from just before `_append_line` until after the final `write_state`. Restore the mask in a `finally`. Guard with `hasattr(signal, "pthread_sigmask")`. A pending signal is delivered a few milliseconds later, after line and state agree.
3. Document exit 143 beside 130 in the README exit-code table and in `docs/DESIGN.md` section 11.

**Tests** (`SignalTest`, POSIX only).

- `test_sigterm_runs_cleanup_and_exits_143`: a child Python process patches `gblog._atomic_write`'s `write_fn` step to send itself SIGTERM, then calls `gblog.main(["--root", root, "note", …])`. Assert exit 143, no `*.tmp` under `state/`, and `verify` exit 0.
- `test_signal_between_line_and_state_is_deferred`: the child patches `Ledger._append_line` to send itself SIGINT after the real call. Assert exit 130, state names the new record, and `verify` prints no `one record behind` line.
- `test_handlers_are_restored_after_main`: in-process; `signal.getsignal(SIGTERM)` is the same object before and after `gblog.main(["--version"])`.
- `test_mask_is_restored_when_the_state_write_fails`: use `state_write_failing(1)`. Assert `pthread_sigmask(SIG_BLOCK, [])` equals its value before the call.

---

## K10-05 (Medium) — `install.sh` replaces the running executable in place

**Where.** `install.sh`:

```sh
install -m 0755 "$tmp" "$PREFIX/gblog"
```

**Problem.** coreutils `install` removes the destination and writes the new file under the final name. Nothing is fsynced.

- **Concurrent start.** A `gblog` process that starts during an upgrade reads a truncated script. The shell hook starts one at every prompt.
  - If the cut falls inside a statement, Python prints a `SyntaxError` and exits 1. The hook hides it and the event is lost.
  - If the cut falls on a statement boundary, the module is valid Python that defines some functions and ends. The last line, `sys.exit(main())`, is missing. The process exits 0 and logs nothing.
- **Power loss.** A crash during or shortly after the copy can leave the truncated file permanently. Every later `gblog log` then "succeeds" silently until someone runs `gblog --version` and sees no output.

The README upgrade command (`git pull && bash install.sh`) runs while Bots work.

**Reproduction.**

1. `head -c 200000 gblog.py > "$PREFIX/gblog"`, then trim to the last complete top-level line.
2. `gblog note --bot ops --detail x; echo $?` prints `0`.
3. No record is written and nothing is printed.

**Fix.**

```sh
new="$PREFIX/.gblog.new.$$"
trap 'rm -f "$tmp" "$new"' EXIT
install -m 0755 "$tmp" "$new"
python3 -c 'import os, sys; fd = os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "$new" || true
mv -f "$new" "$PREFIX/gblog"        # rename: readers see the old file or the complete new one
```

The printed lines stay as they are. CI greps `installed <prefix>/gblog from local checkout`.

**Tests** (`tests/test_scripts.py`).

- `test_install_replaces_the_executable_by_rename`: put a stub `mv` first on `PATH` that records its arguments and then runs the real `mv`. Assert one call whose destination is `$PREFIX/gblog` and whose source is in the same directory. Assert no `.gblog.new.*` file remains.
- `test_install_keeps_the_old_executable_when_the_copy_fails`: pre-install a working version. Put a stub `install` first on `PATH` that exits 1. Assert `install.sh` fails and `"$PREFIX/gblog" --version` still prints the old version line.
- The existing `cmp` and rerun tests pass unmodified.

---

## K10-06 (Medium) — `origin` is changed before the record that documents the change

**Where.** `gblog.py`, `sync()`:

```python
if changed:
    ...
    _set_origin(root, bool(current), remote)        # side effect first
...
if changed:
    try:
        change = {"type": "sync", ..., "target": "git remote", "outcome": "ok", "detail": "origin %s -> %s" % ...}
        if current:
            ledger.append(bot, change)              # record second
        else:
            _bookkeep(change)
    except BaseException:
        _restore_origin(root, current, old_push)
        raise
```

**Problem.** `SECURITY.md` promises "Every change of `origin` is written to the ledger". The undo covers exceptions only. A SIGKILL, SIGTERM (K10-04) or power loss between `_set_origin` and the append leaves the backup re-pointed with no record. Nothing reports it afterwards; `verify` has no view of `.git/config`.

The order is the wrong way round for a write-ahead rule. Record first can at worst over-report. Change first can under-report.

**Reproduction.** Patch `Ledger.append` to call `os._exit(9)` when the record's `target` is `git remote`. Run `sync --no-push --remote B --replace-remote` in a child process. Afterwards `git config --local remote.origin.url` is `B` and no chain mentions it.

**Fix.**

1. Append the `git remote` record first, with the same text and outcome as today. Use `ledger.append` for a replacement and `_bookkeep` for a first origin.
2. Then call `_set_origin`.
3. If `_set_origin` raises, append a second record (`target: git remote`, `outcome: fail`, `detail: "origin change to <url> failed: <class>"`) and re-raise. No restore is needed, because nothing was changed.
4. A tail mismatch on the first append now raises before any change, so the undo path of G1-05 b disappears. Exit code 4 and the unchanged `origin` stay as `test_origin_replacement_is_undone_when_it_cannot_be_recorded` expects.

**Tests** (`OriginOrderTest`).

- `test_record_is_written_before_origin_changes`: wrap `_set_origin` and `Ledger.append` to log call order. Assert the `git remote` append precedes `_set_origin`.
- `test_failed_origin_change_is_recorded_as_fail`: `_set_origin` raises `CalledProcessError`. Assert `origin` is unchanged, the chain ends with records `ok` then `fail`, and the CLI exits 2.
- The existing `RemoteChangeAndDriftTest`, `SameRemoteTest` and `SyncGuardTest` pass. They assert one `ok` record per successful change.

---

## K10-07 (Medium) — Three crash states are reported with tamper wording and no hint

G1-10 b added a hint for "state one record behind". Three neighbouring states still read as tampering.

### (a) Torn last line

**Where.** `Ledger.verify`, `step()`:

```python
if "_corrupt" in rec:
    walk_problems.append("%s: not valid JSON" % where)
    return stop(i)
```

A power loss during the line write leaves a fragment as the last line. State still names the record before it, because state is written only after the line's fsync. `verify` prints the same line it prints for an edit in the middle of the chain. The state check is skipped because `problems` is not empty. The operator gets `not valid JSON` and exit 4 on every append, with nothing that says "this is the last line and state agrees with everything before it".

**Fix.**

1. In `step()`, remember the index of the first corrupt line and the hash of the record before it (`GENESIS` for line 1).
2. After the pass, check four conditions: the line is the last non-blank line, it is in the newest day file, state is readable, and `state.head` equals that hash.
3. If all hold, add the warning ``<file>:<n>: last line is incomplete and state/<bot>.json names the record before it (interrupted write): restore the line from backup; no acknowledged record is missing``.
4. Keep the verdict FAIL.
5. Mirror the change in `_verify_reference`. The differential tests compare both results.

### (b) `verify --record` records "state tampered or log truncated" for an interrupted append, then heals it

**Where.** `_main`, `verify` branch:

```python
detail = "; ".join(res["problems"]) or "%d records verified" % res["records"]
```

On a chain that is one record behind, `verify --all --record` (step 2 of the daily routine) prints FAIL plus the hint. It then appends a `verify` record whose detail is the bare problem text. That append rolls the state forward. A second run is clean. The only lasting trace is a chained record that says tampering or truncation.

**Fix.** When `res["state_problem"]` is the only problem and the one-behind warning is present, write the detail as `state one record behind (interrupted append); repaired by this record`. Outcome `fail` and exit 1 are unchanged.

### (c) Torn anchor-history line is a permanent FAIL

**Where.** `Ledger._anchor_checks`:

```python
if "_corrupt" in entry or not isinstance(heads, list) or ...:
    problems.append("anchors/%s.history.jsonl:%d: corrupt anchor history line" % (bot, lineno))
```

A power loss during the history append leaves a fragment. The next `anchor` run starts its line with `\n`, so the fragment stays in the middle of the file. `verify` fails for that chain on every run until the file is restored from the backup. gblog offers no path for that.

The FAIL buys no tamper evidence. History lines are not chained, and removing a whole line is silent today.

**Fix** (needs the maintainer's decision, like the other pinned side-file verdicts).

1. In `_anchor`, under `.anchors.lock`, look at the history file before appending. If its last line has no trailing newline and does not parse, truncate the file back to the last newline. Print `notice: removed an incomplete last line from anchors/<bot>.history.jsonl (interrupted write)`. This is the rollback `_append_line` already does for a write that fails while the process is alive.
2. In `verify`, report an unparsable last line without a trailing newline as the warning `…: incomplete last line (interrupted anchor run)`. It is a problem with `--strict`.
3. A corrupt line that ends in a newline stays a problem. `test_anchor_history_appended_not_rewritten` pins that.

**Tests** (`CrashReportingTest`).

- `test_torn_tail_gets_a_hint`: cut the last line in half. Assert FAIL, the `not valid JSON` problem and exactly the new warning. Assert the same result from `_verify_reference`.
- `test_corrupt_line_in_the_middle_gets_no_hint`, `test_torn_tail_with_other_state_head_gets_no_hint`.
- `test_recorded_verify_names_the_interrupted_append`: use `state_write_failing(1)`, then `verify --all --record`. Assert exit 1 and that the record's detail is the new text. Assert a second run exits 0.
- `test_anchor_drops_an_incomplete_history_tail`: append `b'{"ts": "2024'` to the history. Assert `verify` warns and stays OK without `--strict`. Run `anchor`. Assert the file is one valid line longer than before the fragment and `verify --strict` is OK.

---

## K10-08 (Low) — Lock lifetime

**Where and what.**

1. `Ledger.verify` holds the shared bot lock for more than the snapshot needs:

   ```python
   with self.lock(bot, shared=True):
       log_paths, odd_files, link_files = self._file_findings(bot)
       extents = self._snapshot_extents(log_paths)
       raw_state = self._load_state(bot)
       anchor_snapshot = self._anchor_snapshot(bot)   # whole history file + _rollup_heads(): up to 400 rollup files
   ```

   K4-02 moved the chain parse out of the lock; the witness read stayed in. For a chain that was never anchored, `_rollup_heads` parses up to 400 day rollups while every appender of the chain waits. The shell hook at a prompt is one of them.

   The lock is not needed for these files. An anchored head is the hash of a record that existed when the anchor was written. The chain is append-only. So a witness read before the lock is taken is always contained in the extents taken under it. A witness written after the read is simply not seen, which is the property the current comment argues for.

2. `Ledger.rebuild_state` parses the whole chain under the exclusive lock:

   ```python
   with self.lock(bot):
       items = self._chain_items(bot)
   ```

   It needs only the tail and one membership test.

3. The `seal_break` guard `_no_second_ack` iterates every record under the exclusive lock.

4. Every `flock` is a blocking call without a deadline, and the lock file is empty. A stopped or hung gblog process (Ctrl-Z during an fsync, a stalled disk) blocks every other writer of the chain indefinitely. No message says who holds the lock.

**Fix.**

1. Call `_anchor_snapshot(bot)` before `with self.lock(...)` in `verify`, and in `_verify_reference` to keep the two equal. Update the comment and `docs/DESIGN.md` section 5, rule 2.
2. In `rebuild_state`, use `_tail_record(bot)` under the lock and compare `tail["hash"]` with `res["head"]`. Decide "state names a record of the chain" from the verify pass. Set `verify_want_hashes = [old_head]` before the call and test `old_head in res.hashes`.
3. In the seal guard, scan with `self._scan(self.log_files(bot), b'"break_ack"')` and parse only lines that contain the marker.
4. Opt-in bound: when `GBLOG_LOCK_TIMEOUT=<seconds>` is set, acquire with `LOCK_NB` in a loop. On expiry raise `OSError(ETIMEDOUT, "timed out waiting for the lock", path)`, which is exit 2 with nothing written. After an exclusive acquire, write `pid argv[1] ts` into the lock file so the message can name the holder. Unset means today's blocking behaviour. A later hook version can export the variable.

**Tests** (`LockLifetimeTest`).

- `test_anchor_witnesses_are_read_outside_the_lock`: patch `Ledger._anchor_snapshot` to probe `flock(LOCK_EX | LOCK_NB)` on `state/ops.lock`. It must be acquirable.
- `test_anchor_written_after_snapshot_does_not_fail_verify` and `test_rollup_written_after_snapshot_does_not_fail_verify` pass unmodified.
- `test_rebuild_does_not_parse_the_chain_under_the_lock`: patch `Ledger._chain_items` to raise. `state rebuild` still repairs a behind state and still refuses a head that is not in the chain.
- `test_lock_timeout_is_opt_in`: hold the exclusive lock in the test. Run the CLI with `GBLOG_LOCK_TIMEOUT=1`. Assert exit 2 within 3 s, a message that names `state/ops.lock`, and an unchanged chain. Without the variable the child is still waiting after 1 s; release the lock and let it finish.

---

## K10-09 (Low) — Temporary names are per pid only; the stale-file branch can hit a live writer

**Where.** `gblog.py`, `_tmp_name` and `_atomic_write`:

```python
return "%s.%d.tmp" % (path, os.getpid())
...
except FileExistsError:
    st = os.lstat(tmp)
    if not (stat.S_ISREG(st.st_mode) and st.st_nlink == 1):
        raise
    os.remove(tmp)                      # "a regular leftover of a killed process"
    fh = _open_private(tmp, mode, keep=keep_path, exclusive=True, **kwargs)
```

**Problem.**

1. Two gblog processes can have the same pid at the same time when a ledger volume is shared by two PID namespaces (two containers on one host). For writers that hold no lock, the second process takes the first one's temp file for a leftover and removes it. Those writers are `tuning/proposals.json`, the capture snapshot, reports, tuning Markdown and archive files.
   - The first process then renames the second one's half-written file over the target and reports success. Its own content is lost.
   - The second process fails on its rename with `No such file or directory`.
2. Leftovers of killed processes are replaced only when the same pid writes the same path again. Otherwise `state/ops.json.<pid>.tmp` files accumulate indefinitely. Readers ignore them, so this is clutter, not corruption.

**Reproduction.** In one process, create `tuning/proposals.json.<pid>.tmp` with `O_EXCL` and keep it open. That is a live writer with the same pid. Call `save_proposals`. Today it removes the open file and completes.

**Fix.** Keep the name; tests pin it.

1. After creating the temp file, take `flock(LOCK_EX | LOCK_NB)` on it and hold it until the rename.
2. In the `FileExistsError` branch, open the existing regular file with `O_RDWR | O_NOFOLLOW | O_NONBLOCK` and try the same lock. If it is held, raise `OSError(EBUSY, "another gblog process is writing", tmp)`, which is exit 2 with nothing replaced. If it is free, the file is stale: remove it as today.
3. Add a sweep to `Ledger.init()` (also reached from `sync` through `ensure`): remove regular, single-link `*.tmp` files under the ledger directories that are older than one hour and not locked.

**Tests** (`TempNameTest`).

- `test_live_writer_with_the_same_temp_name_is_not_removed`: hold the lock on the temp file from the test. `_atomic_write` raises `OSError` with `EBUSY`. The temp file and the target are untouched.
- `test_stale_tmp_file_is_replaced` (existing) passes: an unlocked leftover is still replaced.
- `test_init_sweeps_old_unlocked_temp_files`: one file with an mtime two hours ago is removed. One fresh file and one locked file stay.
- `test_planted_tmp_symlink_is_refused_not_followed` and the hard-link and FIFO tests pass unmodified.

---

## K10-10 (Low) — Renames that are not made durable; a non-atomic first install of the hook

**Where.**

```python
# Ledger.spool_replay
os.replace(path, os.path.join(done_dir, os.path.basename(path)))   # no directory fsync
...
os.replace(alert_path, resolved)                                    # no directory fsync
```

```python
# _install_shell_hook, first install
with open(rc_path, "ab") as fh:
    fh.write((b"\n" if existing and not existing.endswith(b"\n") else b"") + hook)
```

**Problem.**

1. After a power loss the spool files can be back in `spool/<bot>/`, and `state/<bot>.alert.json` can be back under its old name. `status` then prints `ALERT <bot>: tail mismatch since …` for an incident the operator resolved. The chain is unaffected, and a second `spool replay` cleans up through the guard. The alert is the page, though, and it fires twice.
2. The first hook install appends to `~/.bashrc` in place. A crash or full disk part-way leaves a block without its end marker. Every later `--install` returns `present` for such a block, and the README sends the operator to remove it by hand. The update path already writes a temp file and renames (K2-12).

**Fix.**

1. After the loop in `spool_replay`, when at least one file was moved, call `_fsync_dir` on `spool/<bot>/replayed` and then on `spool/<bot>`. After the alert rename, call `_fsync_dir(self.state_dir)`.
2. For the first install, build `existing + separator + hook` and write it with `_atomic_write(os.path.realpath(rc_path), …, mode="wb")`, as the update path does. A missing rc file is created `0o600`, as `open(..., "ab")` plus the umask did not guarantee.

**Tests.**

- `test_replay_makes_its_renames_durable`: patch `gblog._fsync_dir` to record paths. Assert all three directories appear after a replay that resolves an alert. Assert none appear for a replay with nothing to do.
- `test_first_hook_install_is_atomic`: patch `gblog.os.replace` to raise `OSError(ENOSPC)`. The rc file's bytes are unchanged and no `*.tmp` remains. Without the patch the result is byte-identical to today's (`existing + hook`).

---

## K10-11 (Low, refines D2-11) — Parents of newly created directories are never fsynced

**Where.** `_makedirs` creates missing levels and returns nothing. Callers fsync at most the innermost directory:

```python
# Ledger._append_line
_makedirs(os.path.dirname(path))
...
if size == 0:
    _fsync_dir(os.path.dirname(path))          # logs/<bot>, but not logs/
```

```python
# archive_file
_makedirs(os.path.dirname(full))
...
_fsync_dir(os.path.dirname(full))              # archive/<bot>/<day>, but not archive/<bot> or archive/
```

**Problem.** D2-11 records "directory fsync" as open. The concrete consequences are two false alarms after a power loss, on file systems that do not commit a new directory together with a file fsync:

| First write into | Can vanish | Survives (fsynced) | `verify` afterwards |
|---|---|---|---|
| A new chain | `logs/<bot>/` | `state/<bot>.json` with seq 1 | `verify --all`: `state/<bot>.json records seq 1 head … but logs/<bot> is missing`. Appends exit 4. |
| A new Bot or day under `archive/` | `archive/<bot>/` or `archive/<bot>/<day>/` | The `archive` record | `<path>: archived file missing`, a permanent FAIL. |

Both read as deletion of evidence. On ext4 and XFS the single journal makes this unlikely in practice. The code does not ensure it.

**Fix.**

1. Let `_makedirs` return the directories it created, outermost first.
2. Add `_makedirs_durable(path)`, which calls `_fsync_dir(os.path.dirname(d))` for each created directory.
3. Use it in `_append_line`, `archive_file`, `_atomic_json` (anchors, `anchors/days`, spool) and `write_state`.
4. When nothing was created, no extra fsync happens, so the pinned event lists of `test_append_fsync_order` do not change.

**Tests.**

- `test_new_chain_directory_is_made_durable`: record `_fsync_dir` paths during the first append to a new chain. `logs/` (the parent of the new `logs/<bot>`) is among them, before the state replace.
- `test_new_archive_directories_are_made_durable`: `archive/` and `archive/<bot>` appear before the record is appended.
- `test_existing_directories_cost_no_extra_fsync`: a second append and a second archive on the same day record exactly today's paths.

---

## K10-12 (Low, refines D2-13) — A lock file replaced while it is held

**Where.** `Ledger._Lock.__enter__` opens the path and locks the inode it got. Nothing ties that inode to the path afterwards.

**Problem.** D2-13 records "a removed lock file" as open. Suppose `state/<bot>.lock` is unlinked or replaced while a writer holds it. The README's retire procedure does this, and so can a restore of `state/` from backup or a cleanup script. The next writer creates and locks a new inode. Both are then inside the critical section, and both link to the same `prev`. The result is a duplicated `seq`, which is a permanent FAIL.

**Fix.**

1. After `flock` returns, compare `os.fstat(fd)` with `os.lstat(path)`. If device or inode differ, or the path is gone, unlock, close and start again. Bound this at five rounds. This closes "replaced while waiting".
2. Expose `held_ok()` on the lock, which makes the same comparison. Call it in `Ledger.append` immediately before `_append_line`. On mismatch raise `OSError(ESTALE, "lock file was replaced while it was held; nothing was written", path)`, which is exit 2. This closes "replaced while held", except for the few milliseconds of the write itself.
3. In the README retire procedure, remove `state/B.lock` only after `gblog verify --all` has shown the chain is no longer written to. Alternatively, leave lock files in place; `.gitignore` already excludes them.

**Tests.**

- `test_lock_replaced_while_waiting_is_reacquired`: the holder thread unlinks the lock file and releases. The waiter must end up holding a lock on the file that is now at the path. Assert by probing with `LOCK_NB` from a third descriptor.
- `test_append_refuses_when_its_lock_file_was_replaced`: patch `Ledger._append_head` to unlink and re-create the lock file before returning. Assert `OSError`, an unchanged day file, and unchanged state.
- The existing `LockFileTest` and `LockingTest` suites pass unmodified.

---

## Checked and found sound

These were traced and need no change.

- **Healthy append order.** Line write, file fsync, directory fsync for a new day file, state temp fsync, rename, `state/` fsync. A crash at any single point leaves "nothing new" or "one behind". The exception is the pre-fsync kill of K10-01.
- **ENOSPC during the line write.** The rollback removes only this call's bytes, and only while they are still the end of the file. The glue newline for a tail without `\n` is removed with them.
- **Lock order.** No path holds two bot locks. `.anchors.lock` is taken before any bot lock and never while one is held. `aliases.lock` is a leaf. The hard-link guard (`avoid`) prevents the self-wait.
- **`verify` after K4-02.** File sizes and state are taken in one shared hold. Bytes appended later are cut off by the recorded extent. A replaced or shortened file is reported, not misread.
- **`spool replay`.** A kill between append and move is repaired by the guard, which runs under the bot lock. Two concurrent replays append each event once. A refusal during replay does not spool the event twice (G1-10 f).
- **Refusals.** Alert first, then spool, both through temp file, fsync and rename. The per-second counter is computed under the chain's lock.
- **State merging.** `append` reuses the state object it read in the same lock hold, so `last_sync` and capture markers written by `update_state` are never lost to an append.
- **Alias registry.** Read, change and write happen in one hold of `aliases.lock`. An unreadable registry is never overwritten.
- **`seal`.** The second-ack check runs under the bot lock immediately before the append.
- **`anchor`.** The history line is written before `anchors/<bot>.json`. The rollup read-merge-write is covered by `.anchors.lock`.

## Suggested order of work

1. K10-01 and K10-04 together. Both touch `append` and `_append_line`, and the signal mask removes most of the states K10-01 has to repair.
2. K10-02. It is one added staging pass with no output change.
3. K10-03 and K10-06. They are independent and each has a deterministic test.
4. K10-05. It changes `install.sh` only.
5. K10-07 (a) and (b). Then get the maintainer's decision on (c).
6. K10-08 … K10-12 as hardening, in any order.

Documentation that follows from these fixes:

- `docs/DESIGN.md` section 4: the "Crash outcomes" table and the sentence about state ahead of the log.
- `docs/DESIGN.md` section 5: the `sync` row in the lock table and rule 2.
- `docs/DESIGN.md` section 11 and the README exit-code table: exit 143.
- `SECURITY.md`, "Private-remote policy": the record is written before `origin` changes.
- `CHANGELOG.md`, "Known open findings": D2-11 and D2-13 move to fixed once K10-11 and K10-12 land.