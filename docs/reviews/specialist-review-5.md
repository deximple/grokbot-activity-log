# Specialist review K5 — 2026-11-16

**Area:** documentation accuracy. README, both `SKILL.md` files, `docs/*.md`, `SECURITY.md`, `CHANGELOG.md` and `examples/README.md` were compared with the current `gblog.py`.

**Method:** read-through of the code and the tests that pin it. Nothing was executed; the reproductions are the commands a maintainer should run, and the stated results follow from the quoted code. Doc lines are identified by file, section and quoted text; code lines by function and quoted statement.

**Not covered** (a separate engineer has the round-4 G1/G2 items): sync foreign-repo guard, tail `ts` bound, sync cache removal, side-file rule, task-status function, origin definition, rollup protection, hook fallback. The "Hand-over" section at the end lists the doc passages those items will invalidate.

## Summary

| Id | Prio | One line |
|---|---|---|
| K5-01 | High | `capture browser --hours 0` exits 2; six doc passages say only a negative value does |
| K5-02 | High | Documents say K3/K4 "changed documentation only"; `gblog.py` contains K3-03, K3-04, K4-02, K4-04, K4-07, K4-08 |
| K5-03 | High | `sync --remote <other spelling of origin>`: docs say exit 2, code exits 0 and keeps `origin` |
| K5-04 | High | DESIGN §5/§7/§13 and FAQ 35 describe the old whole-list `verify`; a new problem line is undocumented |
| K5-05 | Medium | `(N archived file(s) intact)` / `archives`: docs say bare claims are counted and a test is pending; neither is true |
| K5-06 | Medium | `examples/routine-daily-audit.sh` runs `anchor --skip-archives`; three documents say it does not |
| K5-07 | Medium | Routine prompts quote `gblog task-end --outcome ok`, which exits 2 (no `--bot`); the daily prompt never captures the task id |
| K5-08 | Low | README headline transcript cannot be reproduced (`#4`, missing `task_end` line, `5 records`) |
| K5-09 | Low | README "Development": `GBLOG_ROOT=/tmp/x … && ./bin/gblog status` runs `status` on the default ledger |
| K5-10 | Low | "Shortest form that still works" is not the shortest in 5 of 10 rows |
| K5-11 | Low | `record_error` is also set on `OSError`; DESIGN §7 says tail mismatch only |
| K5-12 | Low | `(archives not checked)` is printed only for chains that have archive records |
| K5-13 | Low | FAQ 28: `--history` replaces auto-detection, it does not "add" |
| K5-14 | Low | `gblog capture shell-hook --install` without `--bot` (marketplace.md, `render_coverage`) exits 2 |
| K5-15 | Low | DESIGN §7 problem list and §13/FAQ 35 `report --date` cost are incomplete |
| K5-16 | Low | Schema and stderr-list nits (`source: self`, `spool_id`, README claim about §6) |
| K5-17 | Low | Stale `--help` strings and module docstring |
| K5-18 | Low | SECURITY "read-only checks": `verify` creates `state/<bot>.lock` in the audited clone |

---

## K5-01 (High) — `capture browser --hours 0` exits 2; the documents say it is accepted

**Code** (`_validate_args`):

```python
if args.cmd == "capture" and args.what == "browser" and args.hours < 0:
    raise _UsageError("--hours must be 0 or more")
if args.cmd == "capture" and args.what == "browser" and args.hours < 1:
    raise _UsageError("--hours must be 1 or more (a window of 0 hours imports nothing)")
```

`tests/test_deep_reviews_b.py::BrowserWindowTest.test_hours_zero_is_a_usage_error` pins exit 2 for `--hours=0`. It is not an expected failure; `tests/PENDING.txt` is empty.

**Doc lines that say otherwise:**

| Doc line | Says |
|---|---|
| README, "What capture does not see": "(default 24; a negative value exits 2)" | 0 accepted |
| `docs/faq.md` Q28: "(default 24; a negative value exits 2)" | 0 accepted |
| CHANGELOG, Capture bullet: "a negative `capture browser --hours` exits 2 (`--hours 0` imports nothing on a first run)" | 0 accepted |
| CHANGELOG, "Documentation corrected": "`capture browser --hours` is refused when it is negative, not when it is below 1." | the opposite of the code |
| CHANGELOG, deep-review intro: "the corrections … made to … the `--hours` bound" | same |
| `docs/DESIGN.md` §11, deep-review cases: "Exit 2: a negative `capture browser --hours`" | 0 accepted |

Two secondary defects:

- The message for a negative value (`--hours must be 0 or more`) contradicts the next check, which refuses 0.
- The test comment says "bug: D4-03. README and CHANGELOG say … below 1 exits 2; --hours=0 is accepted". Both halves are now false: documents and code each moved and passed each other.

**Reproduction:**

```bash
export GBLOG_ROOT="$(mktemp -d)"
./bin/gblog capture browser --bot ops --history /nonexistent --hours 0;  echo "exit $?"
# error: --hours must be 1 or more (a window of 0 hours imports nothing)   exit 2
./bin/gblog capture browser --bot ops --history /nonexistent --hours=-5; echo "exit $?"
# error: --hours must be 0 or more                                          exit 2
```

**Fix** (documents follow the code, because a test pins the code):

- README and FAQ 28: "(default 24; a value below 1 exits 2)".
- CHANGELOG Capture bullet: "`capture browser --hours` below 1 exits 2 (the library function clamps a negative value to 0)".
- CHANGELOG: delete the "Documentation corrected" line about `--hours` and drop "and the `--hours` bound" from the intro.
- DESIGN §11: "Exit 2: `capture browser --hours` below 1".
- Follow-up code change for the engineer (not part of this review):
  - Collapse the two checks into one with the message `--hours must be 1 or more`.
  - Update the assertion `"error: --hours must be 0 or more"` in `tests/test_deep_reviews.py`.
  - Correct the stale test comment.

**Test cases:**

1. CLI: `--hours 0`, `--hours=-1` → rc 2, nothing under `state/`; `--hours 1` → not rc 2.
2. Doc contract: README, `docs/faq.md`, `docs/DESIGN.md` do not contain "a negative value exits 2" or "a negative `capture browser --hours`".
3. Library: `capture_browser(ledger, "ops", [hist], hours=0)` returns `added == 0` on a first run and does not raise.

---

## K5-02 (High) — documents say K3/K4 "changed documentation only"; six K3/K4 findings are fixed in the code

**Doc line** (CHANGELOG, "Specialist reviews (K1 … K4)"): "K3 and K4 changed documentation only." The "Known open findings" list, README "Known limits of this upgrade", SECURITY "Filesystem assumptions" and DESIGN §13 repeat the open state.

**Code:** each item below is in `gblog.py`, tagged with its id in a comment, and covered by `tests/test_k3_k4.py`.

| Id | Doc line that is now false | Code that contradicts it |
|---|---|---|
| K3-03 | README "Known limits": "records that an earlier 0.6 build already wrote there as `bot: "ops"` keep their `bot field 'ops' does not match chain 'Ops'` warning, and `verify --contains` does not count their hashes" | `Ledger._own_name`: a case variant counts when `os.path.samefile(variant, chain_dir)`; `verify` passes `own` to `_layout_findings` and fills `own_hashes` with `if b == bot or own(b)` |
| K3-03 | SECURITY "Case-sensitive names" (same claim) and CHANGELOG "Known open … CLI and readers" (same claim) | same |
| K3-04 | see K5-03 | `_same_remote` |
| K4-02 | see K5-04 | streaming `Ledger.verify` |
| K4-04 | CHANGELOG open: "Two redaction rules are quadratic on crafted text, and `append` redacts under the bot lock" | `_redact_env_names`, `_redact_jwt`, `_LinearRule`; in `append`: "Redaction happens before the lock is taken (K4-04)" |
| K4-04 | DESIGN §13 "Inputs that cost more than their size", row "`token_token_…` … quadratic time … `append` redacts while it holds the bot lock" | same; `test_redaction_is_linear_on_crafted_text`, `test_append_redacts_before_it_takes_the_lock` |
| K4-04 | DESIGN §4 diagram, step 5 "build record: seq+1, prev=head, redact, hash" drawn after step 1 (lock) | `stored = {…}` is computed before `with self.lock(bot)` |
| K4-07 | CHANGELOG open: "`spool replay` scans the chain once per event" | `_ReplayedIds.refresh` reads only bytes appended since the last call |
| K4-07 | DESIGN §13 estimates row "`spool replay` \| S × (lines since the refusal day) … about 15 minutes" and inputs row "scans the chain since the refusal day once per event" | same; `test_replay_reads_the_chain_through_one_index`. The second half of the inputs row ("each refusal lists the whole spool directory") is still true (`_refuse_tail`: `os.listdir(spool_dir)`) |
| K4-08 | DESIGN §13 inputs row: "`report` compares every open task with every task of the Bot (minutes for 20 000 open tasks)" | `render_report`: `closed` / `closed_chains` index by `_title_key`; `test_many_open_tasks_do_not_rescan_every_task` |
| K4-08 | CHANGELOG open: "`export`, `status`, `report` and `tail` keep or parse more than they need (K4-08 … K4-10, K4-13)" | K4-08 is closed; the range should start at K4-09 |

**Reproduction:** `python -m unittest tests.test_k3_k4 -v` passes; then `grep -n "documentation only" CHANGELOG.md`.

**Fix:**

- CHANGELOG, "Specialist reviews": replace the sentence with "K3-03, K3-04, K4-02, K4-04, K4-07 and K4-08 were then fixed in `gblog.py` as well; `tests/test_k3_k4.py` covers them. The other K3 and K4 findings changed documentation only."
- CHANGELOG, "Fixed in the code": add one bullet per id:
  - **Case variants on a case-insensitive file system (K3-03).** A record whose `bot` field is another letter-case spelling of the same directory is the chain's own: no `bot field` warning, and `--contains` counts it.
  - **Another spelling of `origin` (K3-04).** Text from K5-03.
  - **Streamed `verify` (K4-02).** Text from K5-04.
  - **Linear redaction (K4-04).** The env-style and JWT rules run in one pass with unchanged output; `append` redacts before it takes the bot lock.
  - **`spool replay` index (K4-07).** One index of the spool ids in the chain per run.
  - **Report twin lookup (K4-08).** Closed tasks are indexed by title.
- CHANGELOG, "Known open findings" and "Future work": remove the sentences quoted in the table, plus the future-work bullet "A streamed `verify` …".
- README "Known limits", bullet "File systems that ignore letter case": delete "What stays: …"; keep the first two sentences and the last one.
- SECURITY "Case-sensitive names": delete the last sentence.
- DESIGN §13: delete the three stale input rows; correct the `spool replay` row to "O(lines appended since the oldest pending refusal) once per run, plus one tail read per event".
- DESIGN §4: move "redact" out of step 5 into a step "0b. redact (before the lock)".

**Test cases** (doc contract; proposed new `tests/test_docs_contract.py`):

1. A table `FIXED = {"K3-03", "K3-04", "K4-02", "K4-04", "K4-07", "K4-08"}`: none of these ids occurs between the headings `### Known open findings` and `### Behaviour changes` of CHANGELOG.md.
2. Every id in `FIXED` occurs in `gblog.py` (the fix comments carry the ids) and in the CHANGELOG "Fixed in the code" list.
3. CHANGELOG does not contain "K3 and K4 changed documentation only".

---

## K5-03 (High) — `sync --remote` with another spelling of `origin`: documented exit 2, actual exit 0

**Doc lines:**

- README "After upgrading", item 5: "…keeps working, as long as the URL is written exactly as `origin` has it. Another spelling of the same repository (`…/r` for `…/r.git`, https for ssh) also exits 2, before the local commit".
- README "Known limits of this upgrade": "*Another spelling of the backup URL.* `sync --remote` compares the URL as text".
- CHANGELOG, "Documentation corrected": "`sync --remote` compares the URL as text (K3-04)"; "Known open … sync": "`--remote` with another spelling of the current `origin` exits 2 (K3-04)".
- SECURITY, private-remote notes: "Repeating the current `origin` changes nothing, and any other URL exits 2 without `--replace-remote`."

**Code** (`sync`):

```python
same = any(_same_remote(remote, u) for u in current)
if current and not same and not replace_remote:
    raise _UsageError("origin is already %s; pass --replace-remote to send backups to %s instead" ...)
# Another spelling of the current origin (…/r for …/r.git, https for ssh) is not a change: .git/config
# stays as it is and the run goes on (K3-04). --replace-remote stores the new spelling.
changed = remote not in current and not (same and not replace_remote)
```

`_same_remote` treats two URLs as the same remote when one of these holds:

- the text is equal;
- both are GitHub and name the same owner and repository (any transport, letter case ignored, wiki-ness equal);
- both are local paths to the same absolute directory;
- the text is equal apart from a trailing `/` or `.git`.

Another port, user or host is a different remote. On a non-GitHub host, https versus ssh is also different (`test_same_remote_unit`).

**Reproduction:**

```bash
export GBLOG_ROOT="$(mktemp -d)"; ./bin/gblog note --bot ops --detail x
./bin/gblog sync --no-push --remote https://github.com/you/private-ledger.git   # exit 0
./bin/gblog sync --no-push --remote git@github.com:you/private-ledger.git; echo "exit $?"
# README: 2.  Actual: 0.
git -C "$GBLOG_ROOT" remote get-url origin      # still https://github.com/you/private-ledger.git
```

**Fix:**

- README item 5, replace the two sentences with:
  "A routine that passes the same repository on every run keeps working. The URL may be spelled differently from the stored one when it names the same repository: for GitHub, the same owner and repository over https or ssh, with or without `.git`; for another host, the same text apart from a trailing `/` or `.git`; for a local path, the same directory. `origin` keeps the spelling it has, and nothing is recorded. With `--replace-remote` the new spelling is stored and recorded as a change. Any other URL exits 2, before the local commit."
- README "Known limits": delete the bullet "Another spelling of the backup URL".
- SECURITY: "Repeating the current `origin`, also in another spelling of the same repository, changes nothing; any other URL exits 2 without `--replace-remote`."
- CHANGELOG: handled in K5-02.
- `examples/README.md` ("If `origin` already points somewhere other than `GBLOG_AUDIT_REMOTE` …"): add "(another spelling of the same repository is accepted)".

Note for the G-item owner: if "origin definition" changes `_same_remote`, this text must be re-checked. The finding here is only that the documents describe the pre-K3-04 behaviour.

**Test cases:** the behaviour is covered by `SameRemoteTest`. Doc contract: README does not contain "compares the URL as text" or "written exactly as `origin` has it".

---

## K5-04 (High) — DESIGN and FAQ describe the old `verify`; the streamed walk and its new problem line are undocumented

**Code** (`Ledger.verify`):

```python
with self.lock(bot, shared=True):
    log_paths, odd_files, link_files = self._file_findings(bot)
    extents = self._snapshot_extents(log_paths)      # (path, size, st_dev, st_ino)
    raw_state = self._load_state(bot)
    anchor_snapshot = self._anchor_snapshot(bot)
...
layout_warnings, noncanonical = _layout_findings(bot, feed(), log_names, strict, dup_lines, own)
```

Lines are read by `_snapshot_lines` after the lock is released, up to the recorded size. No parsed record is kept. `_snapshot_lines` can add this problem:

```python
problems.append("%s: log file was replaced or shortened while it was being verified" % rel)
```

**Doc lines that are now false:**

| Doc line | Why false |
|---|---|
| DESIGN §5 lock table: "`verify`, while it snapshots the log lines (parsed record and raw text), `state/<bot>.json`, …" | under the lock only the file list, sizes, state and anchor files are taken |
| DESIGN §5 rule 2: "No file of the chain is read again after the lock is released; only archived files are." | every log file is read after the release, up to its snapshot size |
| DESIGN §7, first paragraph: "takes one snapshot under the shared bot lock: every log line with its raw text, …" | same |
| DESIGN §13, first table: "`verify --bot B` \| … once, under the shared lock … O(N) time and memory: … the parsed records and their hashes are kept until the result is returned" | memory no longer grows with N. What is kept is the next row |
| DESIGN §13 estimates: "110 MB, lock held 0.25 s", "1.0–1.2 GB, lock held 2.5 s", "three peaks of about 1 GB" | derived from the old implementation |
| DESIGN §13 "Kept as they are for now": "The chain walk of `verify` is not streamed…" | it is |
| `docs/faq.md` Q35: "Every record is read into memory and re-hashed" | not kept in memory |
| CHANGELOG "Changed" (R-05): "takes one snapshot of the log, state and anchor files under the shared lock" | historical; needs a pointer to K4-02 |

What is kept in memory now:

- one small dict per `archive` record (`arch_recs`), plus segments and breaks;
- the first 3 adoption findings and the first 20 layout findings;
- a set of record hashes. The CLI keeps only the `--contains` hashes and the anchored heads (`ledger.verify_want_hashes = wanted`, or `[]` for `anchor`). A library call with `verify_want_hashes = None` keeps every hash.

**Reproduction:** `python -m unittest tests.test_k3_k4.StreamingVerifyTest -v` (`test_verify_parses_after_the_lock_is_released`, `test_verify_does_not_keep_the_parsed_chain`).

**Fix** (replacement text):

- DESIGN §5 table cell: "`verify`, while it lists the chain's files and records the size of every log file, and reads `state/<bot>.json`, `anchors/<bot>.json`, the lines of `anchors/<bot>.history.jsonl` and the newest day-rollup entry that names the chain".
- DESIGN §5 rule 2, replace the paragraph body with:
  "`verify` drops its shared lock before it parses, before it re-hashes archives and before it returns. The snapshot taken under the lock is: the list of regular log files with the size each had, the state file and the anchor files. Log files are append-only and writers hold the exclusive lock, so the first *size* bytes of each file are that snapshot. They are read line by line after the lock is released; bytes appended later are not read. A file that was replaced or shortened in between (gblog never does that) is the problem `<file>: log file was replaced or shortened while it was being verified`. The walk, the state check, anchor containment, the layout findings and `--contains` all use this one snapshot, so a concurrent `append` followed by `anchor` still cannot make a running `verify` report a head that is "no longer in the chain"."
- DESIGN §7: the same first sentence, and add the new problem text to the list (see K5-15).
- DESIGN §13:
  - Row `verify --bot B`: "O(N) time. Memory does not grow with N for the CLI (one small entry per `archive` record; the `--contains` hashes and anchored heads). A library call without `verify_want_hashes` keeps one hash per record. The shared lock is held for one directory scan, one `stat` per day file and the side-file reads."
  - Estimates: delete the memory and lock-hold figures or mark them "0.6.0".
  - Delete the first "Kept as they are" bullet.
- FAQ 35: "Linear in the chain. Every record is read and re-hashed once; records are not kept in memory. Every archived file is decompressed and re-hashed…".

**Test cases:** behaviour is covered. Doc contract: `docs/DESIGN.md` does not contain "No file of the chain is read again after the lock is released" and does contain "replaced or shortened while it was being verified".

---

## K5-05 (Medium) — `(N archived file(s) intact)` and `archives`: documents say bare claims are counted and a test is pending

**Doc lines:**

- CHANGELOG "Known open … verify / anchor": "`(N archived file(s) intact)` counts `archive` records that name no file (D1-13; `tests/PENDING.txt`)."
- DESIGN §7, `verify --json` table, `archives`: "A record that names no file is counted too, so the text suffix `(N archived file(s) intact)` can overstate (CHANGELOG, "Known open findings")".

**Code** (`Ledger.verify`):

```python
# A bare claim (no path, no sha256) addresses no file and is not counted as archived evidence.
archives = sum(1 for r in arch_recs if r.get("path") or r.get("sha256"))
```

`tests/PENDING.txt` says "Nothing is pending". `ArchiveClaimGapTest.test_bare_archive_record_is_not_counted_as_intact` passes, although its comment still begins "bug: D-8".

**Reproduction:**

```bash
export GBLOG_ROOT="$(mktemp -d)"
./bin/gblog log --bot ops --type archive --target contract.pdf
./bin/gblog verify --bot ops
# OK    ops   1 records  head …            <- no "(1 archived file(s) intact)"
#       ! logs/ops/<day>.jsonl:1: archive record without path and sha256: no archived file backs it
./bin/gblog verify --bot ops --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["archives"])'   # 0
```

**Fix:**

- CHANGELOG: delete the sentence; add under "In the code, but listed under 'Known open findings' until now": "`(N archived file(s) intact)` and the `archives` key count only `archive` records that name a file (D1-13)."
- DESIGN §7 `archives` row: "number of `archive` records that name a `path` or a `sha256` (0 when the walk already had a problem). A record that names neither is not counted; it is the warning of step 7."
- Housekeeping, for the engineer: several test comments are stale.
  - `tests/test_mutation_gaps.py`: the comments "bug: D-1 … D-8", and the class docstring of `DocumentedButAbsentTest` ("Each test fails today"), which is false.
  - `tests/test_deep_reviews_b.py`: the comments "bug: D4-03" and "pending: D3-03".

**Test cases:** existing behaviour test. Doc contract: CHANGELOG does not mention `tests/PENDING.txt` in "Known open findings" while that file lists no test.

---

## K5-06 (Medium) — the example audit script runs `anchor --skip-archives`; three documents say it runs plain `anchor`

**Code** (`examples/routine-daily-audit.sh`): `run anchor anchor --skip-archives`. The script's own comment adds: "A chain whose only problem is a damaged archived file is therefore FAIL in step 1 and OK here; the exit status of this script follows step 1." `tests/test_examples.py` pins the call (`"anchor --skip-archives"`).

**Doc lines:**

| Doc line | Problem |
|---|---|
| `examples/README.md`, step 3: "**`gblog anchor`**, then a `Chain heads <UTC date>` block" | flag missing |
| DESIGN §13: "The scheduler script `examples/routine-daily-audit.sh` calls `gblog anchor` without `--skip-archives`, so it re-hashes the archived files twice per run" | false |
| CHANGELOG "Known open … Tests and routine": "`examples/routine-daily-audit.sh` re-hashes the archived files twice (K4-06)" | false |

A consequence the examples README does not state: the status column of the "Chain heads" block comes from `anchor`. A chain with a damaged archived file therefore shows `OK <head>` in that block while `RESULT: FAIL` and exit 1 come from `verify`.

**Reproduction:** `grep -n "anchor" examples/routine-daily-audit.sh`. Then damage an archived `.gz` and run the script: the block prints `ops OK <head>`, the result line is `RESULT: FAIL`.

**Fix:**

- `examples/README.md` step 3: "**`gblog anchor --skip-archives`** (step 2 has just re-hashed every archived file), then a `Chain heads <UTC date>` block … The status in that block is the anchor's: a chain whose only problem is a damaged archived file is `FAIL` in step 2 and `OK` here. The script's exit status follows step 2."
- DESIGN §13: delete the sentence.
- CHANGELOG: reduce the bullet to "The daily routine prompt walks every chain three times (K4-06)."

**Test cases:** doc contract: every `gblog <subcommand> …` step quoted in bold in `examples/README.md` "Steps, in order" equals, token for token, a call in the script (ignoring `--remote=<url>`).

---

## K5-07 (Medium) — routine prompts quote a `task-end` that exits 2; the daily prompt never keeps the task id

**Doc lines:**

- `docs/routine-daily-audit.md`, prompt step 0: "open this run's task with `gblog task-start --bot <your slug> --source routine --task "Daily ledger audit"`" (no `TID=$(…)`, no `export GBLOG_TASK`).
- Same prompt, step 7: "Close the task with `gblog task-end --outcome ok` (or `fail`) and a one-line `--detail`."
- `docs/routine-weekly-tune.md`: "close the task with `gblog task-end --outcome fail --detail "<why>"`".
- `docs/agent-integration.md` §9, daily table row 8: "`gblog task-end --outcome ok` or `--outcome fail`".

**Code:** `_add_common`: `p.add_argument("--bot", required=True, …)`. In the `task-end` branch: `if not args.task_id: sys.stderr.write("warning: task-end without --task-id and $GBLOG_TASK unset closes no task\n")`.

**Reproduction:**

```bash
./bin/gblog task-end --outcome ok; echo "exit $?"
# gblog task-end: error: the following arguments are required: --bot      exit 2
unset GBLOG_TASK; ./bin/gblog task-end --bot ledger --outcome ok
# warning: task-end without --task-id and $GBLOG_TASK unset closes no task   (exit 0, task stays open)
```

A Bot that follows the daily prompt literally either writes nothing (exit 2) or leaves one open task per day. `status` then shows a growing `open` count, and `tune` proposes "N tasks were never closed with task-end". The weekly prompt's steps 0 and 5 are spelled correctly; the daily one is not.

**Fix:**

- Daily prompt step 0: "… open this run's task: `TID=$(gblog task-start --bot <your slug> --source routine --task "Daily ledger audit")`, then `export GBLOG_TASK="$TID"`."
- Daily prompt step 7: "Close the task with `gblog task-end --bot <your slug> --task-id "$TID" --outcome ok` (or `--outcome fail`) and a one-line `--detail`."
- Weekly prompt: "`gblog task-end --bot <your slug> --task-id "$TID" --outcome fail --detail "<why>"`".
- agent-integration §9 row 8: "`gblog task-end --bot <slug> --task-id "$TID" --outcome ok` or `--outcome fail`".

**Test cases:** doc contract `test_routine_prompt_commands_parse`:

1. Extract every backticked string that starts with `gblog ` from the two prompt blocks.
2. Replace `<…>` placeholders and `"$TID"` with dummy values.
3. Call `gblog.build_parser().parse_args(shlex.split(cmd)[1:])` and assert that no `SystemExit` is raised.

Today this fails for `gblog task-end --outcome ok` and `gblog task-end --outcome fail --detail "<why>"`.

---

## K5-08 (Low) — README headline transcript cannot be reproduced

**Doc line** (README, top): after `task-start` and `log` (`#2`), the transcript shows `7853cc548280 #4 approval denied`, then `task-end` with no output, then `5 records`.

**Code:**

- Each command appends one record, so the approval is `#3`.
- The `task-end` branch always prints `out.write("%s #%d task_end %s\n" % …)`.
- Four commands give `4 records`, and the head equals the first field of the `task-end` line.

**Fix:**

```
$ gblog approval --bot ops --task-id 1a5248e239 --target "send email to vendor" --decision denied
7853cc548280 #3 approval denied
$ gblog task-end --bot ops --task-id 1a5248e239 --outcome ok
3c4241c25eb5 #4 task_end ok
$ gblog verify --all
OK    ops                       4 records  head 3c4241c25eb5
```

**Test case:**

1. Run the five commands on a fresh root.
2. Assert the `#n` numbers are 2, 3, 4.
3. Assert the last word of `verify`'s line equals the first word of the `task-end` line.
4. Assert the README block has the same shape (`#2 action`, `#3 approval denied`, `#4 task_end ok`, `4 records`).

---

## K5-09 (Low) — README "Development": `status` runs on the wrong ledger

**Doc line:** `GBLOG_ROOT=/tmp/x ./bin/gblog init && ./bin/gblog status`.

**Shell semantics and code:** the assignment applies to the first command only. `default_root()` then returns `/workspace/activity-log` or `~/activity-log` for `status`, so the first output line is `root: <default root>`, not `/tmp/x`.

**Fix:** `export GBLOG_ROOT="$(mktemp -d)"; ./bin/gblog init && ./bin/gblog status`.

**Test case:** doc contract: no fenced command line in README matches `^[A-Z_]+=\S+ .*&& \./bin/gblog`.

---

## K5-10 (Low) — "Shortest form that still works" is not the shortest in five rows

**Doc line:** README "After upgrading", item 1, table column "Shortest form that still works".

**Code** (`_build_parser`, argparse prefix matching):

| Row | README | Actually shortest | Why |
|---|---|---|---|
| `verify` `--r` | `--rec` | `--re` | only `--record` starts with `--re` |
| `tune` `--r` | `--rej` | `--re` | only `--reject` |
| `tune` `--s` | `--std` | `--st` | only `--stdout` |
| `capture files` `--r` | `--roo` | `--ro` | `--record-baseline` starts with `--re` |
| `archive` `--s` | `--std` | `--st` | only `--stdin` |

The listed forms do work (`AbbreviationTest` uses them), so only the header is wrong.

**Fix:** rename the column to "A form that still works (spell the flag out in scripts)". Do not shorten the values: `sync --re` already broke once in 0.6.1.

**Test case:** for each table row, parse `<command> … <form>` and assert it resolves to the first flag in "Now ambiguous between" (or to `--root` for `capture files`), and that the "No longer accepted" form raises `SystemExit(2)`.

---

## K5-11 (Low) — `record_error` is also set for an `OSError`

**Doc line** (DESIGN §7, `record_error`): "only with `--record`, and only when the `verify` record itself could not be appended (tail mismatch)".

**Code** (`_record_verify`): a second branch, `except OSError`, sets `res["record_error"] = "verify record not written (%s)" % text` and returns 1.

**Fix:** "… could not be appended: a tail mismatch (`tail mismatch: verify record not written (spool: <path>)`) or an environment error (`verify record not written (<system message>)`)."

**Test case:** existing `test_verify_record_oserror_does_not_hide_other_chains`. Doc contract: DESIGN contains both strings.

---

## K5-12 (Low) — `(archives not checked)` is not always printed

**Doc lines:**

- README: "Use `gblog verify --skip-archives` …; it prints `(archives not checked)`."
- DESIGN §7: "`--skip-archives` skips step 7 and prints `(archives not checked)`."
- FAQ Q5: "the line says `(archives not checked)`".

**Code** (`_verify_text`): the suffix is written only `if res.get("archives")`, that is, for an OK or PASS chain that has at least one archive record naming a file. `docs/routine-daily-audit.md` already says it correctly ("A chain that has archive records then prints …").

**Fix:** use the routine doc's wording in all three places: "a chain that has archive records then prints `(archives not checked)` in place of `(N archived file(s) intact)`".

**Test case:** `verify --skip-archives` on a chain of notes prints no parenthesis; on a chain with one `gblog archive` record it prints the suffix.

---

## K5-13 (Low) — FAQ 28: `--history` replaces auto-detection

**Doc line:** "It copies each history database it finds (…; add more with `--history PATH` or `GBLOG_BROWSER_HISTORY`)".

**Code:**

- `capture_browser`: `paths = list(given) if history_paths else find_browser_histories()`.
- `find_browser_histories`: `GBLOG_BROWSER_HISTORY` is split on `:` and each part is a glob added to the built-in list.

**Fix:** "…; `GBLOG_BROWSER_HISTORY` adds colon-separated glob patterns to that search, and `--history PATH` (repeatable) reads only the files you name".

**Test case:** with a fake `find_browser_histories` returning `[a]`, `capture_browser(…, [b])` reads only `b`.

---

## K5-14 (Low) — `capture shell-hook --install` is quoted without the required `--bot`

**Doc line** (`docs/marketplace.md`, operating instructions): "(gblog capture shell-hook --install; interactive bash only)".

**Code:**

- The `shell-hook` parser: `s.add_argument("--bot", required=True)`, so the command exits 2.
- `render_coverage` prints the same incomplete hint ("`gblog capture shell-hook --install`" and "`gblog capture browser` / `gblog capture files`"). All three need `--bot`.

**Fix:**

- marketplace.md: "`gblog capture shell-hook --bot <slug> --install`".
- For the engineer: add `--bot <slug>` to the three commands in the `render_coverage` text. No test asserts that sentence.

**Test case:** the K5-07 parser test, extended to `docs/marketplace.md`.

---

## K5-15 (Low) — DESIGN §7 problem list and the `report --date` cost rows are incomplete

**(a) Problem texts.** CHANGELOG W-27 says DESIGN "quotes the `verify` problem and warning texts". These texts are in `gblog.py` and in no document:

| Problem text | Code |
|---|---|
| `<file>: log file was replaced or shortened while it was being verified` | `_snapshot_lines` |
| `logs/<bot>: listing stopped early (<class>)` | `_file_findings.entries` |
| `anchors/<bot>.history.jsonl: unreadable (<class>)` | `_anchor_snapshot` |
| `anchors/<bot>.history.jsonl:<n>: corrupt anchor history line` | `_anchor_checks` |
| `multiple break_ack records; only one approved discontinuity is allowed` | `verify` |
| `break_ack does not match the discontinuity at the pinned coordinates` | `verify` |
| `break_ack is outside a verified segment` | `verify` |
| `<file>:<line>: malformed break_ack` | `verify` |

**Fix:** add a short "Other problem lines" list at the end of DESIGN §7 (the `break_ack` ones may sit in §9).

**(b) `report --date D` cost.**

- DESIGN §13, row `report --date D`: "the day-D file of each chain; every later day file as raw lines".
- FAQ 35: "Reads the day file for D. It also scans later day files…".
- **Code** (`_task_context`): when a task of day D has no `task_start` on day D, day files of up to 30 earlier days are also scanned as raw lines for `"task_start"`; later files are scanned for `"task_end"` as well as `clock_regressed`.

**Fix:** append to both rows: "…and, for tasks that cross midnight, raw scans of up to 30 earlier day files (`task_start`) and of the later day files (`task_end`)."

**Test case:** doc contract: every string literal passed to `problems.append(` in `Ledger.verify`, `_snapshot_lines`, `_file_findings`, `_anchor_snapshot` and `_anchor_checks` has its static prefix (text before the first `%`) somewhere in `docs/DESIGN.md` or `docs/faq.md`.

---

## K5-16 (Low) — schema and stderr-list nits

| Doc line | Code | Fix |
|---|---|---|
| README "Record schema": "optional `source` ∈ `self \| auto \| routine`" | `_source_field`: `return source if source and source != "self" else None`; the CLI never stores `self` | "optional `source` ∈ `auto \| routine` (absent means self-reported; `--source self` stores nothing)" |
| DESIGN §6: "`spool_id` \| name of the spool file it was replayed from" | `spool_id = os.path.basename(path)[:-5]` | "name of the spool file, without `.json`" |
| README "Upgrading from 0.5": "The new `notice:` and `warning:` lines on stderr are listed in `docs/agent-integration.md`, section 6." | §6 omits at least the lines below | either add them to §6 or change the README sentence to "The lines an agent must act on are listed in …" |

Lines missing from §6:

- `warning: --source is not stored on archive records`
- `notice: tuning/proposals.json was unreadable and has been replaced`
- `notice: --days <n> is above the maximum; using 36500`
- the three `capture files` baseline notices
- `notice: ignoring browser-history marker dated in the future …`
- `notice: skipped <n> unusable row(s) in <path>`
- `warning: could not read browser history <path>`
- `warning: the --remote URL contains …`
- `warning: could not confirm the remote is private (<reason>)`
- `hint: retry later, or pass --allow-unknown-visibility …`
- `warning: state/<bot>.json is missing or unreadable and the log tail is not self-consistent; state not written`
- `note: strict has no effect until a Bot is registered`

**Test case:** doc contract: every `sys.stderr.write("warning: …` / `"notice: …` / `"note: …` literal in `gblog.py` has its static prefix in `docs/agent-integration.md`, or is on an explicit allow-list in the test.

---

## K5-17 (Low) — `--help` strings and the module docstring lag behind the behaviour

| Text in `gblog.py` | Behaviour |
|---|---|
| `capture browser` help: "import new visits from Chrome/Chromium history" | Firefox `places.sqlite` is read too |
| `shell-hook --install` help: "append the hook to --rc instead of printing it" | an older block is replaced in place (`_install_shell_hook` → `updated`) |
| `verify --strict` help: three cases | README lists nine; the help should say "and the other layout warnings (see README)" |
| `--since` error: "--since must be YYYY-MM-DD" | `today` and `yesterday` are accepted |
| Module docstring: "spool/<bot>/<ts>.json" | files are `<stamp>-<nnnn>-<8 hex>.json` |
| `_sync_drift` docstring and `_SYNC_CACHE` | the documents say there is no cache; belongs to the G item "sync cache removal", listed only for completeness |

**Fix:** align the strings. No behaviour change.

**Test case:** `gblog capture browser --help` mentions "Firefox"; `gblog report --since nope --stdout` names `today` and `yesterday` in its error.

---

## K5-18 (Low) — SECURITY "read-only checks": `verify` writes lock files into the audited clone

**Doc line** (SECURITY, "Auditing a clone of the backup"): "Run read-only checks: `gblog --root <clone> verify --all --strict` …".

**Code:**

- `Ledger.lock(bot, shared=True)`: for a chain on disk it calls `_makedirs(self.state_dir)` and returns `_Lock(path, shared=True, create=on_disk)`.
- `_Lock.__enter__` tries `_open_lock(path, "a+")` first, which creates `state/<bot>.lock` (0o600).
- Lock files are never committed, so a fresh clone has none and `verify` creates one per chain.

This is safe: `O_NOFOLLOW`, regular file only, and the reader goes on without a lock when the tree is read-only. It is still a write into the tree. DESIGN §5 says so; SECURITY does not.

**Fix:** add to that section: "`verify` creates `state/<bot>.lock` for each chain it reads (an empty file, never through a symlink). Mount or `chmod` the clone read-only if nothing at all may be written; `verify` then runs without the lock."

**Test case:** copy a ledger without `state/*.lock`, run `verify --all`, assert the only new paths are `state/<bot>.lock`. With the tree made read-only (non-root), assert rc 0 and no new path.

---

## Hand-over to the G1/G2 owner (not reviewed here)

These passages are currently inaccurate or will become so when the G items land. They are listed so that the documentation is edited once.

| G item | Doc passages to re-check |
|---|---|
| tail `ts` bound (K3-01) | README "Known limits": "A last record with a local-time `--ts`" (the 9-hour waiting advice). DESIGN §4 step 4: "Known limit (D1-09, K3-01)". CHANGELOG "Known open … verify / anchor", first sentence. `_ts_floor` already exists and `LegacyTimestampTest` passes. |
| sync cache removal | `_SYNC_CACHE`, `_write_sync_cache`, the `cache_path` paragraph in `_sync_drift`; the documents already say "no cache". |
| side-file rule | DESIGN §7 table; README "How it works"; SECURITY "Guaranteed". |
| task-status function | DESIGN §13 `append` row; FAQ 35 "Append" row. |
| origin definition | SECURITY policy table row "no `origin` configured …"; FAQ 26; the K5-03 text above. |
| hook fallback | README item 6, last sentence; FAQ 27; agent-integration §10. |

## Proposed doc-contract tests (one new file, stdlib only)

`tests/test_docs_contract.py`, about 150 lines, no network. Each test reads the repository documents and either parses quoted commands with `gblog.build_parser()` or compares quoted strings with the `gblog.py` source.

1. `test_fenced_and_prompt_commands_parse` (K5-07, K5-14).
2. `test_abbreviation_table_rows` (K5-10).
3. `test_readme_transcript_is_reproducible` (K5-08).
4. `test_no_scoped_env_before_and_chain` (K5-09).
5. `test_fixed_ids_not_in_known_open` (K5-02, K5-05).
6. `test_banned_phrases`: a short list of sentences that must not reappear (K5-01, K5-03, K5-04, K5-06).
7. `test_problem_and_stderr_literals_are_documented` (K5-15, K5-16).
8. `test_examples_readme_steps_match_script` (K5-06).

## Spot checks that matched (no finding)

- Exit-code tables: README, DESIGN §11 and agent-integration §5 agree with `main` and `_main`. Checked: 0/1/2/3/4/130, the `sync` precedence 3 before 4, and the closed-pipe rule.
- Quoted `verify` warnings and problems in README "New `!` lines", FAQ Q2/Q4, DESIGN §4 and §7 match the format strings character for character.
- Spool file name `<stamp>-<nnnn>-<8 hex>.json`, `state/<bot>.alert.resolved-<stamp>.json`, `state/.anchors.lock`, `capture-files-<bot>.<tag>.json` and `tuning/<date>-<bot>.md` all match.
- `--outcome`, `--decision` and `--action` synonyms match in README, the activity-log skill and agent-integration §3.
- Reserved names, the resolve order in agent-integration §7, and every string in `docs/chain-repair-live-apply.md` match `seal_break`, `resolve` and `is_reserved`.
- `install.sh` behaviour quoted in README and agent-integration matches. One wording nit: it installs `gblog.py` itself as `$PREFIX/gblog`, not the `bin/gblog` launcher.