# Specialist review K2 — 2026-11-16

**Area:** filesystem adversary — malicious files, symlinks, hard links, FIFOs and directories inside the ledger; permissions; atomic-write crash windows; concurrent invocations on a shared root.
**Reviewed:** `gblog.py` as it is in this tree (`__version__ = "0.6.1"`), plus `install.sh` and the documents where they make filesystem claims.

**Nothing below was executed.** Every reproduction and every "today:" output was derived by tracing the code. Each reproduction is written so that one run in a temporary directory confirms or refutes it.

Not re-reported:
- findings the code really closes (the per-process `*.tmp` name, `O_NOFOLLOW` on final components, `isfile` in `_read_json`, FIFO day files, the gzip read bound, `.anchors.lock`);
- items under "Known open findings" in the CHANGELOG (directory fsync D2-11, removed lock file D2-13, `sync` lock);
- the deliberate non-feature "a symlinked ledger sub-directory is followed".

One class is reported even though the CHANGELOG lists it as fixed: several D1/D3 filesystem defences are described in `CHANGELOG.md`, `docs/DESIGN.md`, `SECURITY.md` and the FAQ but are absent from `gblog.py`. They are marked "documented, not in code" and collected in Appendix A.

## Summary

| Id | Severity | Finding |
|---|---|---|
| K2-01 | High | One non-regular side file or unlistable side directory aborts `anchor` and `verify --all` for every chain (exit 2) |
| K2-02 | High | A FIFO at `state/<bot>.lock` hangs `verify`, `verify --all` and `anchor`; a hard link makes `anchor` deadlock on itself |
| K2-03 | High | One small hash-valid line makes `verify --all` / `anchor` allocate gigabytes (`list(range(…))`, two sites) |
| K2-04 | Medium | A symlinked evidence file is committed as a link and `sync` prints `pushed`; `verify` gives no line for a symlinked archive file |
| K2-05 | Medium | In-place writes truncate before any check: a hard link clobbers a file outside the root, a FIFO hangs `report` / `tune` |
| K2-06 | Medium | `_last_line` is quadratic in the length of the last line, under the exclusive bot lock; readers have no size bound and a check-then-open race |
| K2-07 | Medium | `sync` copies 0o600 ledger files into `.git/objects` with the process umask |
| K2-08 | Medium | Non-regular state, anchor and history files are a warning or silent, not the documented FAIL; a directory at `state/<bot>.json` makes every append write its line and exit 2 |
| K2-09 | Low | `tuning/proposals.json` is read with a bare `open()` and updated without a lock |
| K2-10 | Low | A refusal that cannot be spooled, or a non-regular target day file, loses the event with exit 2 |
| K2-11 | Low | `capture browser --history` copies any path without a type or size check (`/dev/zero` fills the temp disk; the command is auto-allowed) |
| K2-12 | Low | The shell-hook update rewrites the rc file in place; a failed write leaves a damaged `~/.bashrc` |
| K2-13 | Low | Archive cleanup looks at the tail record only and can delete a file that a chained record references |
| K2-14 | Low | `capture files` / `capture browser` read-modify-write their snapshot and markers without a lock |

Suggested order: K2-01, K2-02 and K2-03 first (each is "one file stops `verify --all` or `anchor`", which `SECURITY.md` lists as a vulnerability). Then K2-05 and K2-06, because they introduce the two helpers the other fixes reuse.

---

## K2-01 — One side file or side directory aborts `anchor` and `verify --all` for all chains

**Severity:** High. Documented as fixed (D1-03: "`anchor` isolates each chain completely … `anchor aborted: <class>`"), not in code.

**Where.** `Ledger._anchor` guards only the verify call. The two writes per chain and the rollup are unguarded:

```python
            res = _verify_guarded(self, bot)  # one chain the verifier cannot process must not drop the others
            ...
            self._atomic_json(self._anchor_file(bot), entry)
            self._append_line(self._anchor_history_file(bot), json.dumps({
```

The `verify` command collects the missing-chain witnesses before it verifies anything, also unguarded:

```python
            bots = [b for b in ledger.bots() if b not in invalid]
            # a chain that was anchored and then removed must not vanish silently
            missing = ledger.missing_chains()
```

`anchored_missing()` and `orphan_witnesses()` call `os.listdir()` on `anchors/`, `state/` and `anchors/days/` with no `try`.

**Why it matters.** A directory, FIFO or symlink at `anchors/<first bot>.history.jsonl` raises `OSError` out of `_append_line`. Likewise a directory at `anchors/<bot>.json` (`os.replace` gives `EISDIR`). The whole `anchor` run ends with exit 2:
- no later chain is anchored and no day rollup is written;
- the CLI prints no `ANCHOR` line, because it prints only after `ledger.anchor()` returns.

A side directory that cannot be listed (`chmod 000 anchors/days`) ends `verify --all` with exit 2 before the first chain is walked. The daily routine then reads stale `anchors/<chain>.json` files for its "Chain heads" block.

**Reproduction A (anchor).**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"
./bin/gblog note --bot aaa --detail x && ./bin/gblog note --bot zzz --detail y
mkdir "$GBLOG_ROOT/anchors/aaa.history.jsonl"
./bin/gblog anchor; echo "exit $?"
# today: "error: Is a directory: …/anchors/aaa.history.jsonl", exit 2, no ANCHOR line;
#        anchors/zzz.json and anchors/days/ do not exist
```

**Reproduction B (verify --all; run as a non-root user).**

```bash
./bin/gblog --root "$GBLOG_ROOT" anchor --bot zzz      # creates anchors/days
chmod 000 "$GBLOG_ROOT/anchors/days"
./bin/gblog verify --all; echo "exit $?"
# today: "error: Permission denied: …/anchors/days", exit 2, no OK/FAIL line for any chain
```

**Fix.**

1. In `_anchor`, wrap the per-chain body. Leave `GBLOG_DEBUG` behaviour as in `_verify_guarded`.

```python
        for bot in names:
            try:
                entry = self._anchor_one(bot)          # the current loop body, returning the entry
            except Exception as exc:
                if os.environ.get("GBLOG_DEBUG"):
                    raise
                entry = {"bot": bot, "ok": False, "status": "FAIL", "records": 0, "head": None,
                         "problems": ["anchor aborted: %s" % exc.__class__.__name__], "break": None,
                         "segments": [], "segment_heads": [], "ts": now_iso()}
            chains.append(entry)
```

   `_anchor_one` must not write `anchors/<bot>.json` when it raises. That already holds: `_atomic_json` either replaces the file or leaves it.

2. Guard the tail of `_anchor`:
   - `lost = self.missing_chains()` in `try/except OSError`; on failure add `{"bot": "(witness files)", "ok": False, "problems": ["witness files cannot be listed (<class>)"]}` to `rollup["missing"]`.
   - Around `self._atomic_json(day, merged)` catch `OSError` and add the same kind of entry, so the CLI still prints every chain line and exits 1.

3. In the CLI `verify` branch, wrap `ledger.missing_chains()` the same way and emit `_verify_missing("(witness files)", "witness files cannot be listed (<class>): deleted chains cannot be reported")`. The exit code is then 1, after every chain has been reported.

4. In the `anchor` CLI branch, print the `problems` of a FAIL chain entry that has no `break`. Today only `rollup["missing"]` entries get their `- …` lines.

**Tests.**

- `test_anchor_isolates_a_non_regular_history_file`: seed `aaa` and `zzz`; `os.mkdir(anchors/aaa.history.jsonl)`; `anchor` returns 1; output matches `^ANCHOR FAIL aaa` and `^ANCHOR OK\s+zzz`; `anchors/zzz.json` and the day rollup exist, and the rollup lists both bots. Repeat with a symlink and (where `os.mkfifo` exists) a FIFO.
- `test_anchor_isolates_a_directory_at_the_anchor_file`: `os.mkdir(anchors/aaa.json)`; same assertions.
- `test_verify_all_survives_unlistable_witness_directory`: patch `gblog.os.listdir` to raise `PermissionError` for a path whose basename is `days`; `verify --all` returns 1; both chains have an `OK` line; one `FAIL  (witness files)` block names the directory. The same with `--json` gives a list with three entries.
- `test_debug_env_reraises_from_anchor_write`: with `GBLOG_DEBUG=1` the `IsADirectoryError` propagates.

---

## K2-02 — Lock files: a FIFO hangs every reader, a hard link deadlocks `anchor`

**Severity:** High. The hang happens in `verify --all`.

**Where.** `_open_lock` and the shared branch of `Ledger._Lock.__enter__`:

```python
    if mode == "r":
        fd = os.open(path, os.O_RDONLY | nofollow)
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    try:
        return os.fdopen(fd, "r" if mode == "r" else "r+")
    except Exception:
        os.close(fd)
        raise
```

```python
                for mode in (("a+", "r") if self.create else ("r",)):
                    try:
                        self.fh = _open_lock(self.path, mode)
                        break
                    except OSError:
                        continue
```

**Three defects.**

1. **FIFO at `state/<bot>.lock`.**
   - `"a+"`: `os.open(O_RDWR)` on a FIFO succeeds on Linux. `os.fdopen(fd, "r+")` then fails, because `BufferedRandom` needs a seekable stream (`io.UnsupportedOperation`, an `OSError`).
   - The reader loop swallows the error and tries `"r"`. `os.open(O_RDONLY)` on a FIFO with no writer blocks forever.
   - `verify --bot`, `verify --all`, and the verify inside `anchor` (which holds `.anchors.lock`) never return.
   - `README.md` and `SECURITY.md` promise that non-regular state, anchor and archive files are never opened. The lock file is the one side file left out.

2. **Double close.** When `os.fdopen` fails after it has built the raw `FileIO`, `io.open` has already closed the descriptor. The `os.close(fd)` in the handler raises `EBADF` and masks the real error. A writer sees `error: Bad file descriptor`. `_open_private` has the same pattern.

3. **Hard link between `.anchors.lock` and a bot lock.** `flock` is per open file description.
   - `anchor` holds `LOCK_EX` on `state/.anchors.lock`.
   - `verify` opens `state/<bot>.lock` and asks for `LOCK_SH`.
   - If both names are the same inode, the second request waits on the first: the process deadlocks on itself.
   - `O_NOFOLLOW` stops a symlink here, not a hard link. DESIGN section 5 ("no code path nests a lock on the same file") assumes distinct inodes.

Also: a FIFO, directory or symlink at `state/<bot>.lock` makes every append to that chain exit 2. The event is not spooled, and `verify` prints nothing about it.

**Reproduction 1 (FIFO).**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"
./bin/gblog note --bot ops --detail x
rm "$GBLOG_ROOT/state/ops.lock" && mkfifo "$GBLOG_ROOT/state/ops.lock"
timeout 10 ./bin/gblog verify --all; echo "exit $?"     # today: 124 (hung)
./bin/gblog note --bot ops --detail y; echo "exit $?"   # today: "error: Bad file descriptor", exit 2
```

**Reproduction 3 (hard link).**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"
./bin/gblog note --bot ops --detail x && ./bin/gblog anchor
ln -f "$GBLOG_ROOT/state/.anchors.lock" "$GBLOG_ROOT/state/ops.lock"
timeout 10 ./bin/gblog anchor; echo "exit $?"           # today: 124 (self-deadlock)
```

**Fix.**

```python
def _open_lock(path: str, mode: str = "a+"):
    """Open a lock file for flock(). Never blocks and never returns anything but a regular file."""
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if mode == "r":
        fd = os.open(path, os.O_RDONLY | flags)
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "lock file is not a regular file", path)
        return io.FileIO(fd, "r")          # raw handle; flock() only needs the descriptor
    except BaseException:
        try:
            os.close(fd)                   # FileIO(fd) does not close a descriptor it failed to adopt
        except OSError:
            pass
        raise
```

- `O_NONBLOCK` makes `O_RDONLY` on a FIFO return at once; the `fstat` check then rejects it. `flock` blocking is unaffected.
- **Hard link.** Do not refuse `st_nlink > 1`: `cp -al` and rsnapshot-style snapshots hard-link lock files legitimately. Detect only the nesting that deadlocks:
  - `Ledger.anchor()` stores `(st_dev, st_ino)` of the open `.anchors.lock` in an instance attribute and clears it in `finally`.
  - `Ledger.lock()` passes that pair to `_Lock`.
  - `_Lock.__enter__` compares it with `os.fstat` of its own descriptor before calling `flock`. On a match the reader proceeds without a lock and the writer raises `OSError(errno.EDEADLK, "lock file is a hard link of state/.anchors.lock", path)`.
  - No class-level table is needed, so the W-19 test (`_Lock` has no `_held`) still holds.
- **Visibility.** In `_file_findings`, add a problem when `state/<bot>.lock` exists (`lexists`) and is not a regular file: `state/<bot>.lock: not a regular file (appends to this chain exit 2)`.
- Apply the same "close, ignore `EBADF`" handler in `_open_private`.

**Tests.**

- `test_fifo_lock_file_does_not_hang_verify` (needs `os.mkfifo`): `run_cli(["verify", "--all"], root, timeout=60)` returns 1; a `- state/ops.lock: not a regular file` line is present; a second chain is `OK`.
- `test_fifo_lock_file_append_error_names_the_file`: `note` exits 2; stderr contains `state/ops.lock` and does not contain `Bad file descriptor`.
- `test_hardlinked_anchor_lock_does_not_deadlock`: link as above; `run_cli(["anchor"], root, timeout=60)` returns without a timeout.
- `test_hardlinked_snapshot_locks_still_work`: `os.link(state/ops.lock, base/snap.lock)` (`nlink` 2); `append` still succeeds.
- `test_lock_is_a_plain_holder` (existing) must keep passing: `lock.fh` is set while held and `None` afterwards.

---

## K2-03 — A small hash-valid line makes `verify --all` and `anchor` allocate gigabytes

**Severity:** High. Site 1 is D1-02 (documented as fixed, not in code). Site 2 is not covered by D1-02.

**Where.**

Site 1, `_describe_join`:

```python
    missing = list(range(seq_before + 1, seq_after)) if seq_after > seq_before else []
```

Site 2, `_break_shape_ok`, reached from `_parse_ack` for every hash-valid `break_ack` record, before the pin is checked:

```python
    if n["missing_seqs"] != list(range(n["seq_before"] + 1, n["seq_after"])):
        return False
```

**Why it matters.** The hash is unkeyed, so anyone who can write a line can write a hash-valid one. A `break_ack` whose `break` says `seq_before: 0, seq_after: 2000000000, missing_seqs: []` is about 500 bytes.
- `verify` builds a two-billion-element list to compare it with `[]`.
- With default overcommit the kernel kills the process. `stdout` on a pipe is block-buffered, so even the lines of chains already verified are lost.
- `_verify_guarded` cannot help: there is no exception, only `SIGKILL`.
- With a smaller gap (for example 5·10⁷) the run completes after about 2 GB, and `verify --json` and `anchors/<bot>.json` carry the whole list.

`tests/test_fuzz_verify.py` avoids this on purpose ("a huge forged seq is not fuzzed here"). The same line in a cloned backup hits the auditor who runs `verify --all --strict`.

**Reproduction.** Run under a memory limit so the machine survives.

```bash
R="$(mktemp -d)"; python3 - "$R" <<'PY'
import json, os, sys
sys.path.insert(0, "."); import gblog
d = os.path.join(sys.argv[1], "logs", "aaa"); os.makedirs(d)
rec = {"v": 1, "seq": 1, "ts": "2026-11-16T10:00:00Z", "bot": "aaa", "type": "break_ack", "jay_approval_id": "x",
       "break": {"file": "logs/aaa/2026-11-16.jsonl", "line_before": 1, "line_after": 2, "seq_before": 0,
                 "seq_after": 2000000000, "hash_before": "aa" * 32, "orphan_prev": "bb" * 32,
                 "hash_after": "cc" * 32, "missing_seqs": []}, "prev": "genesis"}
rec["hash"] = gblog.compute_hash(rec)
with open(os.path.join(d, "2026-11-16.jsonl"), "w") as fh:
    fh.write(json.dumps(rec, sort_keys=True) + "\n")
PY
( ulimit -v 2000000; ./bin/gblog --root "$R" verify --all; echo "exit $?" )
# today, with the limit: "FAIL aaa … - verify aborted: MemoryError"
# today, without the limit: memory grows until the OOM killer ends the process
```

Site 1 needs two ordinary hash-valid notes with `seq` 1 and `seq` 2000000000, the second with a correct `prev`.

**Fix.**

```python
_MAX_MISSING_LISTED = 10000

# _describe_join
    gap = seq_after - seq_before - 1 if seq_after > seq_before else 0
    missing = list(range(seq_before + 1, seq_after)) if 0 < gap <= _MAX_MISSING_LISTED else []
    desc = {..., "missing_seqs": missing}
    if gap > _MAX_MISSING_LISTED:
        desc["missing_count"] = gap          # additive key, as DESIGN section 7 already documents

# _break_shape_ok: compare without building anything larger than the list in the record
    listed, first = n["missing_seqs"], n["seq_before"] + 1
    if len(listed) != n["seq_after"] - first or any(v != first + i for i, v in enumerate(listed)):
        return False
```

The pinned descriptors (`missing_seqs: [1920]`) compare exactly as before. `_same_break` is unchanged, because `missing_count` is not a key of `_norm_break`.

**Tests.**

- `test_huge_seq_jump_is_described_by_its_size`: raw chain with `seq` 1 then 10**12; `verify` returns promptly, `ok` is False, `breaks[0]["missing_seqs"] == []`, `breaks[0]["missing_count"] == 10**12 - 2`. **Do not run this before the fix**; on the current code it tries the allocation.
- `test_small_gap_is_still_listed`: `seq` 1 → 5 gives `missing_seqs == [2, 3, 4]` and no `missing_count`.
- `test_break_ack_with_huge_gap_is_rejected_without_allocating`: the record from the reproduction; `verify` gives `malformed break_ack`; patch `builtins.range` to assert it is never called with a span above 10 000 from `_break_shape_ok`.
- Extend the fuzz mutators (`_m_retype_rehash`, `_m_append_forged`) with `seq` values around 10**12 once the fix is in.
- The existing chain-repair and fixture tests must pass unchanged.

---

## K2-04 — A symlinked evidence file is committed as a link and `sync` says `pushed`

**Severity:** Medium. `SECURITY.md` counts as a vulnerability "a way to make `sync` report `pushed` while a file gblog wrote under `logs/`, `anchors/` or `archive/` … differs in it".

**Where.** `_sync_evidence.take` drops everything that is not a regular file, so `_sync_drift` never sees it:

```python
        if not _stat.S_ISREG(st.st_mode):
            return
        forced.append(rel)
        if check:
            checked[rel] = (st.st_size, append_only)
```

`verify_archives` follows the link (`os.path.exists`, `os.path.isfile`, `gzip.open`). `_file_findings` looks at `archive/<bot>` and its day directories, not at the files inside them.

**Why it matters.**
- Replace `archive/ops/<day>/<sha>.gz` (or an older day file, or `anchors/<bot>.history.jsonl`) with a symlink to a copy outside the root.
- Local `verify` stays `OK`. For an archive file it prints no `!` line, even with `--strict`.
- `git add -A` stages the link (mode 120000), the committed-content check skips it, and `sync` prints `commit … pushed`.
- If the swap happens before the first sync of that file, the evidence never reaches the remote, while every local check and the sync line look healthy.
- A symlinked directory is caught today, by accident: its files are missing from `ls-tree`, so they show up as drift. A symlinked file is not.

**Reproduction.**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"; B="$(mktemp -d)/origin.git"; git init --bare -q "$B"
printf 'evidence\n' | ./bin/gblog archive --bot ops --stdin --name e.txt
F="$(find "$GBLOG_ROOT/archive" -name '*.gz')"; mv "$F" /tmp/k2-moved.gz; ln -s /tmp/k2-moved.gz "$F"
./bin/gblog verify --all --strict                 # today: OK … (1 archived file(s) intact), no "!" line
./bin/gblog sync --remote "$B"; echo "exit $?"    # today: "commit … pushed" (plus the other-host warning), exit 0
git -C "$GBLOG_ROOT" ls-tree -r HEAD -- archive   # today: mode 120000: the backup holds a link, not the evidence
```

**Fix.**

1. `_sync_evidence`: for the checked classes (`logs/*/*.jsonl`, `anchors/*.history.jsonl`, `archive/*/*/*.gz`) record a non-regular entry instead of dropping it. Keep the 2-tuple return, because tests unpack it:

```python
        if not _stat.S_ISREG(st.st_mode):
            if check:
                checked[rel] = (-1, append_only)      # not a regular file: can never match a committed blob
            return
```

2. `_sync_drift`: `if staged_size < 0: drift.append(rel); continue` at the top of the loop. In `sync`, when a drifted path is a symlink on disk, word the error `"<rel> is a symbolic link: git stores the link, not the file; not pushed"`. Otherwise keep the existing text. Exit code 1, as for any drift.

3. `Ledger.verify`: for each `archive` record whose `path` passed the path checks, add the existing layout finding `"<path>: symbolic link (gblog does not write through one)"` when `os.path.islink` is true. It is a warning by default and a problem with `--strict`, like the other link findings.

**Tests.**

- `test_symlinked_archive_file_is_not_reported_as_pushed`: as above with `remote_visibility` mocked private; `rc == 1`; `(not pushed)` and the path in the output; `commits(bare) == 0`.
- `test_symlinked_day_file_is_drift`: move an older day file out, link it back; `_sync_evidence` puts it in `checked` with size −1; `_sync_drift` returns it.
- `test_symlinked_archive_file_warns_and_strict_fails`: `verify` has the `!` line; `verify --strict` exits 1.
- `test_sync_drift_detects_rewritten_file` and `test_sync_cache_does_not_hide_a_changed_file` (existing) must pass unchanged.

---

## K2-05 — In-place writes truncate before any check: hard links and FIFOs at write paths

**Severity:** Medium. `SECURITY.md` counts "a path that writes outside the ledger root" as a vulnerability. `O_NOFOLLOW` covers symlinks only.

**Where.** `_open_private`, used for the atomic-write temp file and directly for reports, tuning Markdown, `README.md` and `.gitignore`:

```python
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), perm)
```

The `report` branch:

```python
            with _open_private(path, "w", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write(text)
```

**Three consequences.**

1. **Temp name without `O_EXCL`.**
   - `_tmp_name` is `<path>.<pid>.tmp`. A regular file already at that name is truncated and reused.
   - If it is a hard link to a file outside the root, that file is truncated and filled with the new state. `os.replace` then makes `state/<bot>.json` a second name of the outside inode.
   - The pid is guessable; a same-user adversary can pre-plant a range.
   - A FIFO at the temp name blocks the `O_WRONLY` open forever. In `append` that is after the line is durable, with the bot lock held.

2. **Reports and tuning files are truncated in place.**
   - A hard link at the predictable name `reports/<tomorrow>.md` or `tuning/<date>.md` sends the Markdown into the linked file.
   - A FIFO there hangs `gblog report` after it has printed to stdout. The daily routine then never reaches `sync`.
   - A crash mid-write leaves a truncated report.
   - With `cp -al` snapshots, a same-day rerun rewrites the snapshot's copy as well.

3. **Appended files.** Day files and anchor histories follow a hard link too. For day files the tail check limits this to an empty victim. An anchor history has no such check.

**Reproduction (temp name; Python, in-process so the pid is known).**

```python
import os, sys, tempfile
sys.path.insert(0, "."); import gblog
base = tempfile.mkdtemp(); L = gblog.Ledger(os.path.join(base, "ledger"))
L.append("ops", {"type": "note", "detail": "one"})
victim = os.path.join(base, "victim.txt"); open(victim, "w").write("precious\n")
os.link(victim, gblog._tmp_name(L._state_path("ops")))
L.append("ops", {"type": "note", "detail": "two"})
print(open(victim).read()[:40], os.stat(victim).st_nlink)   # today: '{\n  "head": "…' and 2
```

**Reproduction (report).**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"; ./bin/gblog note --bot ops --detail x
D="$(date -u +%F)"; echo precious > /tmp/k2-victim; ln /tmp/k2-victim "$GBLOG_ROOT/reports/$D.md"
./bin/gblog report >/dev/null; head -1 /tmp/k2-victim       # today: "# Activity report <date>"
rm "$GBLOG_ROOT/reports/$D.md"; mkfifo "$GBLOG_ROOT/reports/$D.md"
timeout 10 ./bin/gblog report >/dev/null; echo "exit $?"    # today: 124 (hung)
```

**Fix.**

1. `_open_private`: never pass `O_TRUNC`; check, then truncate.

```python
def _open_private(path, mode="w", keep=None, exclusive=False, **kwargs):
    ...
    flags = (os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | (os.O_EXCL if exclusive else 0))
    fd = os.open(path, flags, perm)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
        if st.st_nlink > 1:
            raise OSError(errno.EMLINK, "file has more than one hard link; not overwritten in place", path)
        os.ftruncate(fd, 0)
        return os.fdopen(fd, mode, **kwargs)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
```

   `O_NONBLOCK` turns the FIFO case into an immediate `ENXIO`.

2. `_atomic_write`: open the temp file with `exclusive=True`. On `FileExistsError`, `os.lstat(tmp)`:
   - a regular file with `st_nlink == 1` is a stale leftover of a killed process: unlink it and retry once;
   - anything else (symlink, hard link, FIFO, directory): fall into the existing cleanup, which unlinks the name, and re-raise.

   This keeps `test_planted_tmp_symlink_is_refused_not_followed` and `test_state_tmp_symlink_is_not_followed` green. The symlink case still raises `OSError` and leaves no temp name.

3. Reports and tuning Markdown: write through `_atomic_write(path, lambda fh: fh.write(text), errors="backslashreplace")`.
   - To keep the documented "a symlink there is exit 2", first `if os.path.islink(path): raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)`.
   - A hard link or FIFO at the path is then replaced by a regular file, like state and anchor files.

4. Day files and anchor histories: do **not** refuse `st_nlink > 1`. Hard-link snapshots of a ledger are legitimate, and refusing would stop logging. Add a strict-only finding in `_file_findings`: `"<rel>: has <n> hard links (the same bytes are writable under another name)"`. Strict-only for the same reason as the `ts` form check: a default `verify` must not start printing lines for existing snapshot setups.

**Tests.**

- `test_planted_tmp_hardlink_is_not_written_through`: the Python reproduction; afterwards the victim reads `precious\n`, its `st_nlink` is 1 and `state/ops.json` is a different inode. The append may raise `OSError` or succeed.
- `test_stale_tmp_file_is_replaced`: a regular file at `_tmp_name(state)` (`nlink` 1); `append` succeeds; no `*.tmp` is left.
- `test_fifo_at_tmp_name_does_not_hang` (needs `os.mkfifo`; run the append in a subprocess with a timeout): it returns.
- `test_report_does_not_write_through_a_hard_link`: the victim is unchanged; `reports/<day>.md` is a regular file with `nlink` 1 and holds the report.
- `test_report_fifo_does_not_hang`: `run_cli(["report", "--date", DAY], …, timeout=60)` returns 0 and the path is a regular file afterwards.
- `test_report_is_not_written_through_a_symlink` (existing) must still pass: exit 2, victim absent.
- `test_hardlinked_day_file_is_a_strict_only_finding`: default `verify` has no new line; `--strict` fails with the text above.

---

## K2-06 — `_last_line` is quadratic under the exclusive lock; readers have no bound and a check-then-open race

**Severity:** Medium (CPU hang of `append` while it holds the bot lock).

**Where.** `_last_line`:

```python
            buf = (fh.read(step) + buf).rstrip()
            cut = buf.rfind(b"\n")
```

Each 64 KiB step copies the whole buffer and searches all of it again. For a last line of length L the work is about L²/64 KiB.
- 64 MiB: about 60 GiB of memory traffic.
- 256 MiB: about 1 TiB.

These figures follow from the code; they are not measurements.

`_tail_record` calls this from `append`, `update_state`, `archive_file` and `_tail_ok`, mostly under `LOCK_EX`. A day file with one long unterminated last line (zeros compress well, so it also fits in a cloned backup) therefore stalls every writer of that chain for minutes to hours. DESIGN section 13 states that the append cost "does not grow"; that holds only for short lines.

**Two related gaps in the readers.**

- **Check, then open.** Every reader tests the type and then opens: `os.path.isfile` then `open` in `_read_json`, `log_files` then `open`, `_anchor_snapshot`, `orphan_witnesses`, `verify_archives` then `gzip.open`. Swapping the entry for a FIFO in between blocks the reader, in `verify` under the shared lock.
- **No size bound.** `_read_json` does `json.load(fh)` on whatever size the file has. A committed `state/x.json` that is a multi-gigabyte file is read whole. `MemoryError` is not in its `except` list.

**Reproduction.**

```python
import os, sys, tempfile, time
sys.path.insert(0, "."); import gblog
L = gblog.Ledger(os.path.join(tempfile.mkdtemp(), "ledger"))
L.append("ops", {"type": "note", "detail": "one", "ts": "2024-06-01T10:00:00Z"})
with open(L.log_files("ops")[0], "ab") as fh:
    fh.write(b"x" * (64 << 20))                       # one 64 MiB line, no newline
t = time.time(); L._tail_record("ops"); print("%.1f s" % (time.time() - t))   # today: tens of seconds; 4x the size, 16x the time
```

**Fix.**

1. One helper for every read of a ledger file:

```python
def _open_regular(path: str):
    """Open ``path`` for reading only if it is a regular file. Never blocks on a FIFO or device."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
        return os.fdopen(fd, "rb")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
```

   Use it in `_read_json` (wrapping in `io.TextIOWrapper(…, "utf-8")`), `iter_records`, `_chain_snapshot`, `_last_line`, `_anchor_snapshot`, `_witness_present`, `_clamped_candidates`, `tune_history`, `_spool_replayed`, `orphan_witnesses`, `_git_blob_id` and `_gunzip_sha256` (`gzip.GzipFile(fileobj=_open_regular(full))`). It follows symlinks as today. The type test now happens on the opened descriptor.

2. `_last_line`, linear:

```python
def _last_line(path: str, block: int = 65536) -> bytes:
    with _open_regular(path) as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        parts: List[bytes] = []                 # pieces of the last line, newest first
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step)
            if not parts:
                chunk = chunk.rstrip()          # trailing blank lines and white space
                if not chunk:
                    continue
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                parts.append(chunk[cut + 1:])
                break
            parts.append(chunk)
        return b"".join(reversed(parts)).strip()
```

   The result is the same for every input: last non-blank line, stripped. Each byte is copied and searched once.

3. `_read_json(path, limit=16 << 20)`: after `fstat`, treat `st_size > limit` as unreadable (`None`), and add `MemoryError` to the caught exceptions. Pass `limit=None` for the file-capture snapshot and for `tuning/proposals.json`, which can be large legitimately.

**Tests.**

- `test_last_line_matches_reference`: 200 random files (blank tails, CRLF, no final newline, lines longer than two blocks, `block=7`); compare with `data.rstrip().rsplit(b"\n", 1)[-1].strip()`.
- `test_long_last_line_is_linear`: an 8 MiB unterminated last line; count the `read` calls on the handle (129, give or take one) and assert the result parses as corrupt.
- `test_append_refuses_long_garbage_tail_quickly`: an append to the chain from the reproduction raises `TailMismatchError`.
- `test_oversized_state_file_counts_as_unreadable`: a 17 MiB `state/ops.json`; `verify` warns `missing or unreadable` and `_read_json` reads no more than the header.
- `test_reader_does_not_block_when_entry_becomes_a_fifo` (needs `os.mkfifo`): patch `os.path.isfile` to return True for a FIFO path; `_read_json` returns `None`.

---

## K2-07 — `sync` copies private ledger files into `.git/objects` with the process umask

**Severity:** Medium (confidentiality at rest on a multi-user machine; the 0o600 promise of I-22/R-14 stops at the working tree).

**Where.** `sync` and `_git`:

```python
    if not os.path.isdir(os.path.join(root, ".git")):
        _git(root, "init", "-q")
```

```python
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false", "-C", root, *args],
                          check=check, text=True, capture_output=True, env=_git_env())
```

**Why it matters.**
- With the usual umask 022, `git init` creates `.git` as 0o755 and every object as 0o444.
- Every record and every archived transcript that gblog created 0o600 is therefore readable by other local users through `.git/objects`, whenever the root itself is traversable.
- That is the case for a pre-0.6 ledger (the old 0o755 root is "left alone"), for a root the user created with `mkdir`, and for the restore path the FAQ recommends (question 16: `git clone` the backup creates a 0o755 root with 0o644 files).

**Reproduction.**

```bash
umask 022; mkdir -p /tmp/k2-led && export GBLOG_ROOT=/tmp/k2-led
./bin/gblog note --bot ops --detail "private" && ./bin/gblog sync --no-push
stat -c '%a %n' "$GBLOG_ROOT"/logs/ops/*.jsonl "$GBLOG_ROOT/.git"
find "$GBLOG_ROOT/.git/objects" -type f -perm /044 | head -3
# today: 600 for the day file, 755 for .git, and object files that are group/other readable
```

**Fix.**

1. Run every git child with a private umask (stdlib, Python 3.8, single-threaded CLI):

```python
def _run_git(argv, **kwargs):
    old = os.umask(0o077)
    try:
        return subprocess.run(argv, **kwargs)
    finally:
        os.umask(old)
```

   Use it in `_git`, `_sync_ignored` and `_sync_drift`.

2. After gblog itself ran `git init`, `os.chmod(os.path.join(root, ".git"), 0o700)`. Leave an existing `.git` alone, consistent with "existing permissions are left alone". Its new objects become private through the umask.

3. Docs:
   - FAQ 16: after a restore by clone, run `chmod -R go-rwx <root>`.
   - `SECURITY.md` ("Confidentiality at rest"): say that committed copies follow the same rule from this version on, and that objects written earlier keep their mode.

**Tests.**

- `test_git_objects_are_private` (POSIX, git): `os.umask(0o022)`; append; `sync --no-push`; walk `.git` and assert `mode & 0o077 == 0` for `.git` and for every entry under `.git/objects`.
- `test_existing_git_dir_mode_is_left_alone`: pre-create `.git` with `git init` under umask 022; after `sync`, `.git` is still 0o755 and the objects written by this sync are private.
- `test_umask_is_restored`: `os.umask` after `sync(...)` equals the value before, also when git raises `CalledProcessError`.

---

## K2-08 — Non-regular side files: documented FAIL, actual warning or silence

**Severity:** Medium. Documented, not in code (D1-01, D1-04).

**Where.** `Ledger.verify` and its helpers.

| Planted entry | Documented (README, DESIGN section 2 and 7, FAQ) | Current code |
|---|---|---|
| FIFO or directory at `state/<bot>.json` | FAIL `…: not a regular file` | `_read_json` returns `None`, so the warning `state/<bot>.json missing or unreadable`, exit 0. `tests/test_deep_reviews.py::test_fifo_state_and_alias_files_do_not_hang` pins that. |
| FIFO or directory at `anchors/<bot>.json` | FAIL | warning `anchors/<bot>.json is unreadable`, exit 0 |
| FIFO or directory at `anchors/<bot>.history.jsonl` | FAIL | `os.path.isfile(hist)` is False, `history = None`: **no line at all**. The history witness is dropped silently. |
| `logs/<bot>` that cannot be listed | FAIL `logs/<bot>: cannot be listed (<class>)` | `entries()` returns `[]`; with the state file gone the chain prints `OK … 0 records  head genesis` |

From `_file_findings`:

```python
            try:
                it = os.scandir(d)
            except OSError:
                return []
```

**A write-side effect of the first row.** With a directory at `state/<bot>.json`, every append:
- treats state as unreadable and adopts the tail;
- writes its line durably, with `state_adopted: "unreadable"`;
- then fails in `os.replace(tmp, <directory>)` and exits 2.

The chain grows and each record carries the marker, yet every command reports an error. `verify` shows only warnings.

**Reproduction.**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"
./bin/gblog note --bot ops --detail one && ./bin/gblog anchor
rm "$GBLOG_ROOT/anchors/ops.history.jsonl" && mkdir "$GBLOG_ROOT/anchors/ops.history.jsonl"
rm "$GBLOG_ROOT/state/ops.json" && mkdir "$GBLOG_ROOT/state/ops.json"
./bin/gblog verify --all; echo "exit $?"            # today: OK + "! state/ops.json missing or unreadable", exit 0; nothing about the history
./bin/gblog note --bot ops --detail two; echo "exit $?"   # today: "error: Is a directory: …/state/ops.json", exit 2
./bin/gblog tail --bot ops -n 1                     # today: the record "two" is there
chmod 000 "$GBLOG_ROOT/logs/ops"; rmdir "$GBLOG_ROOT/state/ops.json"; rm -f "$GBLOG_ROOT"/anchors/ops.json
rmdir "$GBLOG_ROOT/anchors/ops.history.jsonl"; rm -rf "$GBLOG_ROOT/anchors/days"
./bin/gblog verify --all                            # today (non-root): "OK    ops    0 records  head genesis"
```

**Fix.** In `_file_findings`, which already runs once per chain under the shared lock:

```python
        try:
            it = os.scandir(d)
        except FileNotFoundError:
            return []
        except OSError as exc:
            if d == bot_dir:
                problems.append("logs/%s: cannot be listed (%s)" % (bot, exc.__class__.__name__))
            return []
        ...
        for path in (self._state_path(bot), self._anchor_file(bot), self._anchor_history_file(bot)):
            link(path)
            if os.path.lexists(path) and not os.path.isfile(path):
                problems.append("%s: not a regular file" % rel(path))
```

- This makes the three rows above a FAIL, as documented.
- The state warning stays for a file that is regular but unparsable.
- Change the pinned test to expect exit 1 for the FIFO state case, and update its comment.
- In `append`, when `os.path.lexists(state_path)` is true and the entry is neither a regular file nor a symlink, refuse before writing the line (`_refuse_tail` with `observed = {"file": "state/<bot>.json", "corrupt": True, …}`). The event is then spooled and the chain does not grow one "error" at a time.

**Tests.**

- `test_non_regular_state_file_fails_verify`: FIFO and directory variants; exit 1; `- state/ops.json: not a regular file`.
- `test_non_regular_anchor_history_fails_verify`: directory at the history path; exit 1. Without the fix, the silence itself is the assertion that fails.
- `test_unlistable_chain_directory_fails`: patch `gblog.os.scandir` to raise `PermissionError` for `logs/ops`; `verify` gives FAIL with `logs/ops: cannot be listed (PermissionError)`, never `0 records  head genesis`.
- `test_directory_at_state_path_spools_instead_of_appending`: the append raises `TailMismatchError`; the log bytes are unchanged; one spool file exists.

---

## K2-09 — `tuning/proposals.json`: bare `open()`, no lock

**Severity:** Low.

**Where.** `save_proposals` and `tune_apply`:

```python
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
```

**Why it matters.**
- `gblog tune` (auto-allowed; the weekly routine) blocks forever on a FIFO at that path. It reads without bound through a symlink to `/dev/zero`.
- `tune --apply` does the same after `os.path.exists`.
- The merge is read-modify-write without a lock, so two concurrent runs (`tune` and `tune --bot B`) drop each other's ids. That is the property I-18 added the merge for.

**Reproduction.**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"; ./bin/gblog note --bot ops --detail x
mkfifo "$GBLOG_ROOT/tuning/proposals.json"
timeout 10 ./bin/gblog tune >/dev/null; echo "exit $?"    # today: 124
```

**Fix.**
- Read with `_read_json(path, limit=None)` in both places.
- In `save_proposals`, print the existing `was unreadable and has been replaced` notice when `os.path.lexists(path)` is true and the result is `None`.
- Wrap read-merge-write in `Ledger._Lock(os.path.join(ledger.state_dir, ".tuning.lock"))`: a leaf lock with a dot name, like `.anchors.lock`.
- In `tune_apply`, map a `None` result to `ProposalsCorruptError` when the path exists.

**Tests.**
- `test_tune_does_not_hang_on_fifo_proposals`: with a timeout; exit 0; the notice on stderr; the path is a regular file afterwards.
- `test_tune_apply_on_non_regular_proposals_is_exit_2`.
- `test_concurrent_tune_runs_keep_both_id_sets`: hook `_atomic_write` as `test_concurrent_anchor_runs_keep_both_rollup_entries` hooks `_atomic_json`.

---

## K2-10 — A refusal that cannot be spooled, or a non-regular target day file, loses the event

**Severity:** Low. The design rule is "losing an event is worse".

**Where.** `_refuse_tail` creates directories and writes two files before it raises:

```python
        _makedirs(self.state_dir)
        _makedirs(spool_dir)
        ...
        self._atomic_json(spool_path, payload)
        self._atomic_json(alert_path, payload)
        raise TailMismatchError(
```

**Two cases.**

1. A regular file at `spool/<bot>` makes `_makedirs` raise `FileExistsError`. The CLI exits 2, not 4. There is no spool file and no alert file, so `status` prints no `ALERT`. A Bot that put state ahead of the log can keep the refusals out of `status` this way. Only `verify` still shows the mismatch.

2. A FIFO, directory or device at the *target* day file (for example tomorrow's name, planted today) is skipped by `_tail_record`, so the tail check passes. `_append_line` then fails (`EISDIR`, or `EINVAL` from `fsync` on a FIFO) and the event is lost with exit 2 for the whole UTC day. The shell hook hides the exit code.

**Reproduction 1.**

```bash
export GBLOG_ROOT="$(mktemp -d)/ledger"; ./bin/gblog note --bot ops --detail one
python3 -c 'import json,sys; json.dump({"seq": 99, "head": "cd"*32}, open(sys.argv[1], "w"))' "$GBLOG_ROOT/state/ops.json"
: > "$GBLOG_ROOT/spool/ops"
./bin/gblog note --bot ops --detail lost; echo "exit $?"   # today: "error: File exists: …/spool/ops", exit 2
./bin/gblog status | grep -c ALERT                          # today: 0
```

**Fix.**

- `_refuse_tail`:
  - write the alert first;
  - wrap each of the two writes in `try/except OSError`;
  - collect the failures and always raise `TailMismatchError`, with the message extended by `"; spool not written (<class>)"` where that applies. The exit code is then 4 in every case.

- `append`: after `path` is computed and before `_append_line`:

```python
            if os.path.lexists(path) and not os.path.islink(path) and not os.path.isfile(path):
                self._refuse_tail(bot, record, seq, head,
                                  {"seq": None, "hash": None, "file": rel_of(path), "corrupt": True})
```

  A symlink keeps its documented exit 2; the existing test `test_day_file_symlink_is_not_followed` expects `OSError` there.

**Tests.**
- `test_refusal_survives_a_file_at_the_spool_directory`: exit 4; `state/ops.alert.json` exists; `status` has the `ALERT` line.
- `test_non_regular_target_day_file_spools_the_event`: directory at today's file name beside a valid older day file; the append raises `TailMismatchError`; one spool file holds the record. After removing the directory and repairing nothing else, `spool replay` appends it.

---

## K2-11 — `capture browser --history` copies any path without a type or size check

**Severity:** Low (availability; reachable by the CLI-only adversary, because `capture browser` is on the README allow-list).

**Where.** `capture_browser`:

```python
            copy = os.path.join(tmp, "History")
            shutil.copyfile(path, copy)  # the browser keeps the live DB locked; read a copy
```

**Why it matters.** `shutil.copyfile` refuses a FIFO but not a character device.
- `gblog capture browser --bot ops --history /dev/zero` copies until the temp filesystem is full. It then cleans up and reports "unreadable".
- While the disk is full, appends of every chain on that filesystem fail with `ENOSPC`. They exit 2 and are not spooled.
- A very large regular file has the same effect.

**Reproduction.** Use a small tmpfs so nothing else is affected.

```bash
mkdir /tmp/k2-small && sudo mount -t tmpfs -o size=16m tmpfs /tmp/k2-small
TMPDIR=/tmp/k2-small ./bin/gblog --root "$(mktemp -d)/ledger" capture browser --bot ops --history /dev/zero; echo "exit $?"
# today: fills the 16 MiB, then "warning: could not read browser history /dev/zero", exit 1
```

**Fix.** Before copying:

```python
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_HISTORY_BYTES:      # 2 GiB
                raise OSError(errno.EINVAL, "not a regular file or too large", path)
```

Do this inside the existing `try`, so the path lands in `failed`. Apply the same test to the `-wal` and `-journal` side files, and skip them when it fails.

**Tests.**
- `test_capture_browser_refuses_a_device`: `--history /dev/zero` (POSIX) with `shutil.copyfile` patched to fail the test if it is called; exit 1; `1 unreadable` is not printed, because every history failed; the warning names the path.
- `test_capture_browser_refuses_an_oversized_history`: patch `_MAX_HISTORY_BYTES` to 10 bytes.

---

## K2-12 — The shell-hook update rewrites the rc file in place

**Severity:** Low (operator command, but the file is the user's `~/.bashrc`).

**Where.** `_install_shell_hook`:

```python
    with open(rc_path, "r+b") as fh:  # written in place: a symlinked rc file and its mode are kept
        fh.write(existing[:start] + hook + existing[stop:])
        fh.truncate()
```

A write that fails part-way (`ENOSPC`, a kill) leaves the new prefix followed by the old tail: a startup file that is neither version. The README tells every upgrader to run this command.

**Fix.** Keep the "symlink and mode are kept" property by replacing the link's target atomically:

```python
    target = os.path.realpath(rc_path)
    tmp = _tmp_name(target)
    with _open_private(tmp, "wb", keep=target, exclusive=True) as fh:
        fh.write(existing[:start] + hook + existing[stop:])
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
```

Remove `tmp` on failure, as `_atomic_write` does. Simpler still: call `_atomic_write(target, …, mode="wb")`.

**Tests.**
- `test_hook_update_is_atomic`: patch `os.replace` to raise `OSError`; the rc file is byte-identical to before and no `*.tmp` is left beside it.
- `test_hook_update_through_symlink_keeps_the_link`: `rc` is a symlink to `real_rc`; after the update `os.path.islink(rc)` is still true, `real_rc` holds the new block, and its mode bits are unchanged.
- `test_install_replaces_outdated_hook` (existing) must pass unchanged.

---

## K2-13 — Archive cleanup can delete a file that a chained record references

**Severity:** Low (needs a failing append plus concurrency; the result is a permanent `archived file missing` FAIL).

**Where.** `archive_file`, failure path:

```python
                _tp, tail_after = ledger._tail_record(bot)
                chained = bool(tail_after) and tail_after.get("path") == rel_posix and tail_after.get("sha256") == digest
```

The check looks only at the tail, and it runs without the bot lock. Two interleavings delete a referenced file:

1. Process A's line is durable but its state write fails. Process B appends anything before A's cleanup runs. The tail is then B's record, so A removes the file its own chained record names.
2. Process B found the file "already stored" and is about to append. Process A (the creator) fails and removes the file. B then chains a record to a missing file.

**Reproduction (interleaving 1, deterministic with a hook).**

```python
# after seeding "ops": patch Ledger.write_state to raise ENOSPC once, and in the same patch
# append one note through a second Ledger object after raising (use a threading.Timer of 0 s
# plus join, or call it from the patched function's "finally").
# Then: gblog.archive_file(ledger, "ops", b"evidence\n", "e.txt") raises OSError,
# ledger.verify("ops")["problems"] == ["archive/ops/<day>/<sha>.gz: archived file missing"]
```

**Fix.**
- Do the cleanup under the bot lock (`with ledger.lock(bot):`). Decide by searching the chain's day files from `day` on for the digest bytes; this is a raw scan like `_spool_replayed` (`digest.encode() in raw`, then parse and compare `sha256`). Remove the file only when no record names it.
- For the dedup side, pass a `_guard` to `ledger.append`. Under the bot lock it re-checks `os.path.isfile(full)` and rewrites the file from `data` when it is gone.

**Tests.**
- `test_archive_cleanup_keeps_a_file_named_by_an_earlier_record`: interleaving 1; `verify` stays OK with `archives == 1`.
- `test_dedup_recreates_a_file_removed_before_the_append`: remove the file from inside a patched `_append_head`; the record is appended and the file exists and is intact.
- `test_archive_past_ts_leaves_no_file` and `test_archive_file_survives_a_failed_state_write` (existing) must pass unchanged.

---

## K2-14 — Capture snapshot and markers are read-modify-write without a lock

**Severity:** Low (duplicate `auto` records; they distort coverage).

**Where.**
- `capture_files` reads `state/capture-files-<bot>.json`, appends one record per change and then saves the snapshot.
- `capture_browser` reads `capture_browser_last` once (`ledger.read_state(bot)`), imports, and writes the whole marker dict back through `update_state`.

Two overlapping runs for the same Bot (a routine retry, two Bots sharing a slug) both see the old snapshot or markers and both log the same changes. In `capture_browser` the later `update_state` also overwrites markers the other run advanced.

**Fix.**
- `capture_files`: hold `Ledger._Lock(os.path.join(state_dir, ".capture-files-%s.lock" % bot))` from the snapshot read to the snapshot write.
  - The dot name cannot collide with a chain lock.
  - It is ignored by `state/*.lock` in `.gitignore`.
  - It is always taken before any bot lock, so it is an outer lock like `.anchors.lock`. Add a row to DESIGN section 5.
- `capture_browser`: take the same kind of lock (`.capture-browser-<bot>.lock`) around the whole run. In `save_markers`, merge into the markers re-read under the bot lock instead of writing the dict read at the start.

**Tests.**
- `test_concurrent_capture_files_logs_each_change_once`: start the second run from a hook in `Ledger.append`, in a thread, and join with a short timeout, as the anchor-rollup test does. Exactly one `created` record per file.
- `test_capture_browser_marker_update_merges`: change another history's marker in state between the read and the save; both markers survive.

---

## Appendix A — Filesystem claims in the documents that the code does not implement

Each row was checked against `gblog.py` in this tree. Either implement it or move it to "Known open findings".

| Claim (where) | Code today | Covered above |
|---|---|---|
| D1-01: a non-regular state, anchor or anchor-history file is a `verify` problem (CHANGELOG, README, DESIGN section 2 and 7, SECURITY, FAQ 2) | warning for state and anchor, silence for history | K2-08 |
| D1-02: a gap above 10 000 gets `missing_count` and an empty `missing_seqs` (CHANGELOG, DESIGN section 7) | `list(range(…))`; a second site in `_break_shape_ok` | K2-03 |
| D1-03: `anchor aborted: <class>` per chain; the rollup is still written (CHANGELOG, DESIGN section 2 and 11) | no per-chain guard around the writes | K2-01 |
| D1-04: `logs/<bot>: cannot be listed (<class>)` (CHANGELOG, DESIGN, FAQ 2) | an unlistable directory reads as an empty chain | K2-08 |
| D1-05: heads of the newest day rollup are checked for containment (CHANGELOG, DESIGN section 8, FAQ 12) | `_anchor_snapshot` and `_anchor_checks` read `anchors/<bot>.json` and the history only; a rollup is a witness only for a *deleted* chain | not expanded here: needs a rollup read in `_anchor_snapshot` |
| D3-05: "`sync` re-hashes on every run; the cache is removed" (CHANGELOG, DESIGN section 13, SECURITY) | `_SYNC_CACHE` / `.git/gblog-sync-cache.json` is still used, and `test_sync_cache_does_not_hide_a_changed_file` expects it. The cache file is writable by the adversary; an entry matching blob id, size and mtime skips the hash. | remove `cache_path` from the `sync` call, or correct the documents |
| D3-08: a symlinked ledger directory gives `<dir> is a symbolic link: …; not pushed` (CHANGELOG, README, SECURITY, FAQ 25) | no such check; the case ends as generic drift ("committed content differs from disk"), and a symlinked *file* is not caught at all | K2-04 |
| D4-05: one snapshot per `--path` (`capture-files-<bot>.<tag>.json`) (CHANGELOG, README, DESIGN section 3) | a single `snap_path`; another `--path` re-baselines the same file | outside this area; noted for the capture owner |

## Appendix B — Checked and found sound

- Symlink at a final write component (day file, history, lock, state temp name): refused or replaced, never written through.
- `_append_line` rollback: removes only bytes this call wrote, and only while they are still the end of the file.
- Roll-forward order: adopted tail to state, then line, then state. A second failed state write leaves state one behind again.
- `spool replay` idempotence: the guard runs under the bot lock; it detects a moved file or a `spool_id` already present in the chain.
- `alias` updates: read, change and write in one hold of `aliases.lock`; an unreadable registry is never overwritten.
- `_read_json` on a symlink to a device or FIFO: `isfile` is false, so it is never opened (apart from the race in K2-06).
- `verify` snapshot: log lines, state, anchor and history are read under one shared lock, and nothing is re-read after release except archives.
- Archive reads: bounded by the recorded `bytes`.
- `install.sh`: the ledger root is created under `umask 077`; a relative root is made absolute; `CDPATH` is cleared.