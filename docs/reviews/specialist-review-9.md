# Specialist review K9 — 2026-11-16

**Area:** regressions from today's G1-01 … G1-12 work in `gblog.py` (sync guard, origin handling, task status, day rollup, side files, shell hook): new bugs, behaviour changes for ledgers that came from 0.5, and missing upgrade notes.

**Method:** I read the current `gblog.py`, tests and documents. Nothing was executed, so every reproduction below is derived from the code and should be run before a fix is merged. No code was edited. Round-4 items are not repeated; each finding concerns what the G1 implementation itself introduced or left open.

| Id | Priority | Area | One line |
|---|---|---|---|
| K9-01 | High | `sync` / `_require_ledger_repo` | An existing backup repository that tracks one extra file stops syncing (exit 2); a new project repository that tracks only `README.md`/`.gitignore` is adopted as a ledger |
| K9-02 | High | `sync` origin change (G1-05 b) | A replacement that is undone leaves its `origin A -> B` record in the spool, or durably in the log; exit 4 no longer means "the commit was made" |
| K9-03 | Medium | shell hook (G1-11) | The fallback pattern also matches the non-strict *warning*, so an environment error still logs the command twice; a refusal behind another stderr line is handled only by accident |
| K9-04 | Medium | `sync` push gate (G1-07) | `_local_origin()` tests that a local origin exists, not that the push URLs come from it; a global `remote.origin.pushurl` diverts the push |
| K9-05 | Medium | documents | No G1 change is in CHANGELOG or README "Upgrading"; several K3/K4 notes describe hazards the code has closed; `--hours 0` contradicts four documents |
| K9-06 | Low | `_task_status` (G1-06) | Id matching misses escaped spellings; a stray `*.jsonl` file decides first |
| K9-07 | Low | `spool_replay` (G1-10 f) | The alert file still names the deleted duplicate spool file and holds the transformed record |
| K9-08 | Low | day rollup (G1-08) | An unreadable rollup is overwritten, witnesses included; `missing` is dropped by `anchor --bot`; the kept entry hides today's FAIL from rollup readers |

---

## K9-01 (High) — `_require_ledger_repo` refuses real backups and accepts some foreign repositories

**Where:** `gblog.py`, `_require_ledger_repo()` and its call at the top of `sync()`.

```python
    marked = _git(root, "config", "--local", "--get", "gblog.ledger", check=False)
    if marked.returncode == 0 and marked.stdout.strip().lower() == "true":
        return
    ...
    for path in listed.stdout.split("\0"):
        if not path or path in ("README.md", ".gitignore"):
            continue
        if path.partition("/")[0] not in _LEDGER_TOPS:
            raise _UsageError(...)
    _git(root, "config", "--local", "gblog.ledger", "true", check=False)
```

### Problem A: an existing backup with one extra tracked file exits 2 on every run

- **Trigger.** Any tracked path outside `logs reports state tuning archive anchors spool README.md .gitignore`. Realistic cases:
  - a `LICENSE` from "initialise this repository" on GitHub that was merged into the backup;
  - a `.gitattributes` (the README's own upgrade notes mention LFS and filter setups);
  - a `README` without `.md`;
  - a `.github/` directory.
- **Effect.** The refusal comes before the commit, so no local commit is made either. Until today such a ledger synced normally, because `sync` only ever staged its own paths.
- **Why it matters.** The daily routine and `examples/routine-daily-audit.sh` report "usage or environment error" and backups stop. The fix is a raw `git config` command, which a Bot must not run.
- **Documentation.** Neither the refusal nor the new `gblog.ledger` key in `.git/config` appears in CHANGELOG, README "Upgrading" or SECURITY.

Reproduction (derived):

```bash
R=$(mktemp -d); export GBLOG_ROOT=$R
gblog note --bot ops --detail x
git -C $R init -q; echo MIT > $R/LICENSE
git -C $R add -f LICENSE logs state README.md
git -C $R -c user.name=u -c user.email=u@x commit -qm "0.5 backup"
gblog sync --no-push
# error: … is a git repository that is not a gblog ledger (it tracks LICENSE); nothing was changed. …   (exit 2)
```

### Problem B: the guard adopts repositories it was written to refuse

`README.md` and `.gitignore` are always allowed, and an empty index passes. Two common repository shapes are therefore adopted and marked:

- a project whose only commit is `README.md` + `.gitignore` (the usual first commit on GitHub), with any amount of untracked source;
- a repository after `git init` with nothing committed yet.

`sync` then creates the ledger tree there. It stages the project's own `README.md` and `.gitignore` (`add_paths.append("README.md")` when the file exists), commits with hooks disabled, and pushes the project's branch if its origin is private or not on GitHub. This is the outcome G1-01 describes as the thing to prevent.

```bash
P=$(mktemp -d); git -C $P init -q; echo "# proj" > $P/README.md; echo build/ > $P/.gitignore
git -C $P add .; git -C $P -c user.name=u -c user.email=u@x commit -qm init
mkdir $P/src; echo 'print(1)' > $P/src/main.py          # untracked project code
gblog --root $P sync --no-push                          # exit 0: logs/ state/ … created, "gblog sync" commit made
git -C $P config --get gblog.ledger                     # true
```

### Problem C: the marker is compared as text

`git config gblog.ledger yes` (or `on`, `1`) is a valid git boolean but is not accepted. An operator who writes it that way still gets the refusal.

### Fix

Decide from what the repository shows, in this order:

1. Read the marker as a git boolean (`--bool`).
2. Treat a repository as a previous gblog backup when it tracks a day file **and** the state file of the same chain. Such a repository keeps syncing; gblog prints one notice that names the extra paths and sets the marker.
3. A repository that tracks nothing of a ledger is adopted only when its work tree holds nothing foreign.

```python
_DAY_PATH_RE = re.compile(r"logs/([A-Za-z0-9._-]+)/[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl\Z")
_ROOT_FILES = ("README.md", ".gitignore")

def _require_ledger_repo(root: str) -> None:
    marked = _git(root, "config", "--local", "--bool", "--get", "gblog.ledger", check=False)
    if marked.returncode == 0 and marked.stdout.strip() == "true":
        return
    listed = _git(root, "ls-files", "-z", check=False)
    if listed.returncode != 0:
        return
    tracked = {p for p in listed.stdout.split("\0") if p}
    foreign = sorted(p for p in tracked if p not in _ROOT_FILES and p.partition("/")[0] not in _LEDGER_TOPS)
    chains = {m.group(1) for m in map(_DAY_PATH_RE.match, tracked) if m}
    was_backup = any("state/%s.json" % c in tracked for c in chains)   # gblog sync committed here before
    if foreign and not was_backup:
        raise _UsageError("%s is a git repository that is not a gblog ledger (it tracks %s); nothing was changed. "
                          "If it is your ledger, run: git -C <root> config gblog.ledger true" % (root, _term(foreign[0])))
    if not any(p.partition("/")[0] in _LEDGER_TOPS for p in tracked):
        try:
            extra = sorted(n for n in os.listdir(root)
                           if n != ".git" and n not in _ROOT_FILES and n not in _LEDGER_TOPS)
        except OSError:
            extra = []
        if extra:
            raise _UsageError("%s is a git repository that holds no ledger yet and contains other files (%s); nothing "
                              "was changed. If it is your ledger, run: git -C <root> config gblog.ledger true"
                              % (root, _term(extra[0])))
    if foreign:
        sys.stderr.write("notice: this backup repository also tracks %d path(s) gblog does not write (%s%s); "
                         "gblog commits only its own files\n"
                         % (len(foreign), _term(foreign[0]), ", ..." if len(foreign) > 1 else ""))
    _git(root, "config", "--local", "gblog.ledger", "true", check=False)
```

Add the upgrade note in K9-05.

### Tests (`tests/test_round4.py`, `SyncGuardTest`)

- `test_old_backup_with_extra_tracked_file_keeps_syncing`
  - Setup: commit `logs/ops/<day>.jsonl`, `state/ops.json`, `README.md`, `LICENSE`.
  - Expect: `sync --no-push` exits 0; stderr has the `notice:` line naming `LICENSE`; `gblog.ledger` is `true`; `LICENSE` is unchanged in `HEAD`.
- `test_readme_only_project_is_refused`
  - Setup: tracked `README.md` + `.gitignore`, untracked `src/main.py`.
  - Expect: exit 2; `os.listdir` unchanged; one commit; no `gblog.ledger` key.
- `test_unborn_repo_with_untracked_sources_is_refused`
  - Setup: `git init`, untracked `src/`.
  - Expect: exit 2; nothing created.
- `test_empty_clone_is_adopted`
  - Setup: `git init` in an empty directory, or one that holds only an initialised ledger.
  - Expect: exit 0; marker set.
- `test_marker_accepts_git_booleans`
  - Setup: foreign repository with `git config gblog.ledger yes`.
  - Expect: exit 0.
- Keep `test_foreign_repository_is_refused_and_left_alone` and `test_ledger_repositories_gain_the_marker` unchanged.

---

## K9-02 (High) — an undone origin replacement leaves a record that says it happened

**Where:** `gblog.py`, `sync()`, the `if changed:` block (G1-05 b).

```python
        if changed:
            try:
                change = {"type": "sync", ..., "target": "git remote", "outcome": "ok",
                          "detail": "origin %s -> %s" % (...)}
                if current:
                    ledger.append(bot, change)      # strict: a tail mismatch propagates
                else:
                    _bookkeep(change)
            except BaseException:
                _restore_origin(root, current, old_push)
                raise
```

The intent is that every change of `origin` is in the chain, or the change is undone. Two paths leave the ledger saying the opposite of what happened.

### Path 1: the spooled copy survives the undo

1. The sync Bot's chain has a tail mismatch. `ledger.append` calls `_refuse_tail`, which writes `spool/<bot>/…json` and the alert with the record `origin A -> B, outcome ok`.
2. The exception handler restores `origin` to A. The CLI exits 4.
3. The operator repairs state and runs `gblog spool replay --bot system`, as the documents tell them to.
4. The chain now holds a hash-chained `sync` record `origin A -> B` with outcome `ok`. `origin` is still A.

A reader of the ledger, such as an auditor checking where backups went, is told the backup moved.

```bash
gblog sync --no-push --remote $A
python3 - <<'PY'   # break state of the sync bot
import gblog, os; L = gblog.Ledger(os.environ["GBLOG_ROOT"]); L.write_state("system", {"seq": 99, "head": "cd"*32})
PY
gblog sync --no-push --remote $B --replace-remote     # exit 4, origin restored to $A
# operator: gblog state rebuild --bot system … ; gblog spool replay --bot system
gblog tail --bot system -n 1                          # "… sync git remote - origin $A -> $B"   (false)
```

### Path 2: the line is durable, then the change is undone

1. `ledger.append` writes and fsyncs the line.
2. The state replace fails (`ENOSPC`, `EACCES`). This is the documented "environment error may already have written the record" case.
3. `except BaseException` restores `origin`.
4. The next append rolls state forward and adopts the record. Again the chain says B and the configuration says A.

### Contract change

README, `docs/agent-integration.md` ("With exit 4, `sync` still made its commit"), `docs/DESIGN.md` §11 and SECURITY ("The local commit is made even when the push is refused") all promise a commit on exit 4. On this path there is none.

### Fix

1. **Preflight a replacement** with the non-spooling check. Touch nothing when the chain cannot take the record, and spool a truthful event.
2. **On a refusal after the change**, rewrite the spooled and alert copies so a later replay records that the change was *not* applied.
3. **Do not undo** when the record line is already the chain tail.

```python
        if changed and current and not ledger._tail_ok(bot):
            # origin is not touched; the refused event says so (TailMismatchError -> exit 4)
            ledger.append(bot, {"type": "sync", "action": "other", "target": "git remote", "outcome": "denied",
                                "detail": "origin %s -> %s NOT applied: the record could not be written"
                                          % (_safe_url(current[0]), _safe_url(remote))})
            raise _UsageError("origin was not changed (the %s chain was repaired meanwhile); run the command again" % bot)
        ...
        if changed:
            change = {...}                                        # as today
            try:
                if current:
                    ledger.append(bot, change)
                else:
                    _bookkeep(change)
            except TailMismatchError as exc:                      # raced after the preflight
                _restore_origin(root, current, old_push)
                for path in (exc.spool_path, exc.alert_path):     # never leave "origin A -> B, ok" for a replay
                    doc = _read_json(path)
                    rec = doc.get("record") if doc else None
                    if isinstance(rec, dict):
                        rec.update(outcome="denied", detail="%s NOT applied (record refused, origin restored)"
                                                            % rec.get("detail"))
                        try:
                            ledger._atomic_json(path, doc)
                        except OSError:
                            pass
                raise
            except BaseException:
                try:
                    _p, tail = ledger._tail_record(bot)
                except OSError:
                    tail = None
                durable = bool(tail) and tail.get("type") == "sync" and tail.get("target") == "git remote" \
                    and tail.get("detail") == redact(change["detail"])
                if not durable:                                   # the chain does not hold the change: undo it
                    _restore_origin(root, current, old_push)
                raise
```

4. **Documents.** In each place that promises a commit on exit 4, add: "except when `--replace-remote` could not be recorded: then nothing is changed, nothing is committed, and the refused event says `NOT applied`".

### Tests (`SyncGuardTest`)

- Extend `test_origin_replacement_is_undone_when_it_cannot_be_recorded`.
  - Expect: the one spooled file has `record["outcome"] == "denied"` and `NOT applied` in its detail.
  - Then restore state, run `spool_replay("system")`, and expect that no record of the chain has `target == "git remote"`, `outcome == "ok"` and detail `origin <first> -> <second>`.
- `test_origin_change_survives_when_its_record_is_durable`
  - Setup: patch `Ledger.write_state` so the first call during the replacement raises `ENOSPC`.
  - Expect: exit 2; `origin == second`; the tail record of `system` is the `git remote` record; a second `sync --no-push` exits 0 with the roll-forward notice.
- `test_first_origin_with_tail_mismatch_is_unchanged`
  - Existing behaviour: `_bookkeep` path, origin set, commit made, exit 4.

---

## K9-03 (Medium) — the hook fallback still double-logs on an environment error

**Where:** `gblog.py`, `SHELL_HOOK` (G1-11).

```sh
  if [ "$st" -eq 2 ] && [ -n "${GBLOG_BOT:-}" ] && [ "${GBLOG_BOT:-}" != "%(bot)s" ]; then
    case "$err" in
      ""|*"bot name"*|*"no ASCII letters"*)
        gblog log --bot "%(bot)s" ... --detail "GBLOG_BOT=${GBLOG_BOT} was refused" ...
```

### Problem A: the substring also matches a warning

`*"bot name"*` is tested against the whole captured stderr. When a registry exists and strict mode is off, an unregistered `GBLOG_BOT` makes every call print this line with exit 0:

```
warning: bot name 'Ops2' is not registered in state/aliases.json (logging under 'Ops2'); …
```

If that call then ends in an environment error, stderr is the warning followed by `error: No space left on device: …`, and the exit code is 2. The record line may already be durable, since the state replace is the usual failing step. The pattern matches the warning, the fallback runs, and the command is recorded a second time under the installed Bot. G1-11's own comment names this as the case it excludes. `tests/test_round4.py::HookFallbackClassTest` covers only an `error:` line on its own.

Reproduction with the test harness's fake `gblog`:

```bash
GBLOG_BOT=Ops2 GBLOG_FAKE_ERR="warning: bot name 'Ops2' is not registered in state/aliases.json (logging under 'Ops2'); register it with: …
error: No space left on device: /x/state/Ops2.json"
# -> two calls are logged; expected one
```

### Problem B: a refusal behind another line matches only by accident

When `state/aliases.json` is unreadable, a reserved `GBLOG_BOT` produces two lines: the registry warning, then `error: bot name 'ops.tmp' is reserved …`. The substring happens to match today. Any tightening to an anchored pattern must not lose this case.

### Problem C: two different hook texts carry the same marker

The K1-07 text and the G1-11 text both say `# gblog hook v2`. An operator cannot tell from `~/.bashrc` whether the reinstall in README item 6 was done after G1-11. That item describes the K1-07 change only.

### Fix

`_die()` writes the refusal as the last stderr line before exit, so decide on that line with a start anchor:

```sh
  if [ "$st" -eq 2 ] && [ -n "${GBLOG_BOT:-}" ] && [ "${GBLOG_BOT:-}" != "%(bot)s" ]; then
    err="${err##*$'\n'}"   # the refusal is the last line gblog wrote
    case "$err" in
      ""|"error: bot name "*|"error: '"*"has no ASCII letters or digits"*)
        ...
```

For Problem C, add a second comment line under the marker, for example `# gblog hook text 3`. The tests that pin `lines[1] == "# gblog hook v2"` are unaffected. Mention this line in README "Upgrading" item 6 as the thing to look for.

### Tests (`HookFallbackClassTest`)

- `test_warning_plus_environment_error_logs_once`: the two-line `GBLOG_FAKE_ERR` above gives one call.
- `test_refusal_after_registry_warning_still_falls_back`: `"warning: state/aliases.json is unreadable; …\nerror: bot name 'Ops2' is reserved (…)"` gives two calls.
- `test_usage_error_does_not_fall_back`: `"usage: gblog log …\ngblog log: error: argument --outcome: invalid choice"` gives one call.
- Keep `test_hook_is_valid_bash`; CI's `shellcheck -s bash -e SC1007` step must still pass.

---

## K9-04 (Medium) — the push gate checks that a local origin exists, not where the push goes

**Where:** `gblog.py`, `sync()`, push section (G1-07).

```python
        urls: List[str] = []
        if _local_origin(root):
            urls = [u.strip() for u in _git(root, "remote", "get-url", "--push", "--all", "origin", ...)]
        ...
                _git(root, "push", "--no-verify", "--no-signed", "-u", "origin", "HEAD:%s" % cur_branch)
```

The comment and SECURITY say an origin from the global configuration is never checked and never pushed to. That holds only when there is no local origin at all. Once `.git/config` has `remote.origin.url`, both `git remote get-url --push --all origin` and `git push origin` read every configuration scope:

- a global or system `remote.origin.url` is an additional push URL;
- a global or system `remote.origin.pushurl` replaces the local URL for pushes.

The visibility check runs on the foreign URL, and for a non-GitHub host it only warns. The result line is `commit <id> pushed`, and the repository named in `.git/config` receives nothing.

**Severity.** Setting this up needs write access to `~/.gitconfig`, which the "CLI-only" adversary does not have. So this is a wrong guarantee and an accident path, for example a stray `[remote "origin"]` section in a shared global configuration. It is not a new attack.

Reproduction (the same pattern as `SyncDeepTest.global_config`):

```bash
printf '[remote "origin"]\n\tpushurl = %s\n' "$T/other.git" > $T/gitconfig
GIT_CONFIG_GLOBAL=$T/gitconfig gblog sync --remote "$T/legit.git"
# "commit … pushed"; other.git has the commit, legit.git has none
```

### Fix

Fail closed when `origin` has a URL outside the repository's own configuration:

```python
def _foreign_origin(root: str) -> List[str]:
    """URLs that git would add to origin from the system or global configuration."""
    found: List[str] = []
    for scope in ("--system", "--global"):
        for key in ("remote.origin.url", "remote.origin.pushurl"):
            res = _git(root, "config", scope, "--get-all", key, check=False)
            found += [u.strip() for u in res.stdout.splitlines() if u.strip()]
    return found
```

In `sync()`, before the visibility checks:

```python
        foreign = _foreign_origin(root) if _local_origin(root) else []
        if foreign:
            result["error"] = ("origin is also defined outside the ledger repository (system or global git "
                               "configuration: %s); not pushed" % _safe_url(foreign[0]))
            push = False
```

This exits 1 after the commit, like the other "not pushed" cases. `url.<base>.insteadOf` rewrites are not affected.

### Tests (`SyncDeepTest`)

- `test_global_pushurl_does_not_divert_a_local_origin`
  - Setup: as in the reproduction.
  - Expect: exit 1; `(not pushed)`; `commits(other) == 0`; `self.vis_calls == []`.
- `test_global_url_is_not_added_to_a_local_origin`
  - Setup: global `url = other`.
  - Expect: the same.
- Control: with `GIT_CONFIG_GLOBAL=os.devnull` (the tests' default) the existing push tests stay green.

---

## K9-05 (Medium) — upgrade notes: G1 changes are missing, K3/K4 notes are stale

### A. G1 behaviour changes with no entry in CHANGELOG or README "Upgrading"

`CHANGELOG.md` has no `G1-…` id at all. Each row below is visible to a routine or script:

| Change (code) | What a user or script sees | Where to document |
|---|---|---|
| `_require_ledger_repo` (G1-01 a) | `sync` exits 2 for a repository that tracks non-ledger files; `sync` writes `gblog.ledger = true` into `.git/config` | CHANGELOG "Behaviour changes"; README "Upgrading from 0.5" exit-code table and a new item; SECURITY "Private-remote policy" |
| `analyze` decisions (G1-01 b) | a `tune` record without a `proposal` snapshot no longer hides or "applies" a proposal | CHANGELOG; `docs/DESIGN.md` §6 `tune` |
| stamp loop (G1-05 a) | new stderr line `warning: <chain>: last_sync not written (<class>)`; exit code unchanged | `docs/agent-integration.md` §6 |
| strict origin record (G1-05 b) | `sync --remote X --replace-remote` exits 4 **without** a commit when the sync Bot's chain has a tail mismatch (see K9-02) | README exit table; DESIGN §11; agent-integration §5; SECURITY notes |
| `_task_status` (G1-06) | `task-end` prints a third warning, `task '<id>' is already closed; recorded anyway …`; all task warnings are suppressed after 8 MiB of scanning | agent-integration §3.4 and §6 (the "already closed" row lists only `log`, `approval`, `note`, `archive`); DESIGN §13 first table; FAQ 35 |
| legacy remote file (G1-07) | `sync --remote` exits 2 while `.git/remotes/origin` or `.git/branches/origin` exists; such a file is never pushed to | README item 5; SECURITY table row "no `origin` configured" |
| anchor order and rollup (G1-08) | the history line is written before `anchors/<bot>.json`; a same-day rollup entry with a head is kept and gains `later` (at most 10) | DESIGN §8 (file descriptions) |
| verify hint (G1-10 b) | new `!` line `state/<bot>.json is one record behind (interrupted append); the next append repairs it` beside the FAIL | DESIGN §4 "Crash outcomes" (still says "without a hint"); §7 warning list; CHANGELOG known-open list (D2-10 is listed as open) |
| drift text (G1-10 c) | new `sync` error `<file> is not a regular file; not pushed` | FAQ 25 |
| `verify --bot` (G1-10 d) | can print `FAIL  (witness files)` and exit 1; `--json` gains that entry | DESIGN §7 "Entries that are not chains" (names `verify --all` only); routine step 1 |
| anchor cap (G1-10 e) | a FAIL anchor stores at most 200 `problems` and `segments`, plus `truncated: {problems, segments}` | DESIGN §8; §13 table row K4-12 |
| hook text (G1-11) | the fallback runs only for a refused name; the text changed again with the same `v2` marker | README item 6; CHANGELOG K1-07 entry ("exit 2: unregistered …, reserved" is now narrower) |

Suggested README text, as a new item 8 under "After upgrading from 0.5 or earlier":

> **8. Backup repository marker.** `gblog sync` now refuses (exit 2, nothing changed) a git repository at the ledger root that it cannot recognise as a ledger, and marks one it recognises with `gblog.ledger = true` in `.git/config`. A backup repository that gblog synced before is recognised. If `sync` prints `… is a git repository that is not a gblog ledger (it tracks <path>)` for your ledger, the operator runs `git -C "$GBLOG_ROOT" config gblog.ledger true` once.

### B. Notes that describe hazards the code no longer has

`tests/test_k3_k4.py` shows code fixes for K3 and K4, but the documents still call them open. For someone upgrading from 0.5 this matters as much as a missing note, because operators are told to work around problems that are gone.

- **README, "Known limits of this upgrade", first bullet (local-time `--ts` tail).**
  - It tells the operator not to write to the chain for up to 9 hours.
  - `append` now derives its bound from the tail's `ts` text and day file (`_ts_floor`, `LegacyTimestampTest`), so the first append is clamped and verifies.
  - Replace the bullet with one sentence saying the record is stamped with the tail time and carries `clock_regressed`.
  - Remove the matching "Known limit (D1-09, K3-01)" bullet in DESIGN §4 step 4 and the CHANGELOG known-open entry.
- **README item 5 and CHANGELOG ("`--remote` with another spelling … exits 2", K3-04).**
  - `_same_remote` accepts `…/r` for `…/r.git` and https for ssh on GitHub.
  - Only a different repository exits 2.
- **README, "File systems that ignore letter case"; SECURITY, "Case-sensitive names".**
  - `verify` now accepts a case-variant `bot` field that names the same directory (`_own_name`, K3-03).
  - The "keep their warning, and `--contains` does not count them" sentences are no longer true on such a file system.
- **DESIGN §13 and CHANGELOG "Future work".**
  - "The chain walk of `verify` is not streamed" and the estimate of about 1 GB at 500 000 records describe `_verify_reference`. `verify` now streams and parses after the lock is released.
  - Table rows K4-04 (quadratic redaction; `append` redacts under the lock), K4-07 (`spool replay` scans once per event) and K4-08 (`report` compares every open task with every task) are fixed in the code.
- **CHANGELOG, "K3 and K4 changed documentation only".**
  - This is no longer true.

### C. `capture browser --hours 0`

`_validate_args` refuses it:

```python
    if args.cmd == "capture" and args.what == "browser" and args.hours < 1:
        raise _UsageError("--hours must be 1 or more (a window of 0 hours imports nothing)")
```

Four places say only a negative value exits 2:

- README ("a negative value exits 2");
- FAQ 28;
- DESIGN §11 ("a negative `capture browser --hours`");
- the CHANGELOG correction "refused when it is negative, not when it is below 1", plus "`--hours 0` imports nothing on a first run".

`tests/test_deep_reviews_b.py::test_hours_zero_is_a_usage_error` pins the code. Fix the four documents, and delete that test's stale `# bug: D4-03` comment.

### Test for the documents

Add to `tests/test_meta.py`:

- `test_changelog_mentions_every_user_visible_stderr_line`
  - For a fixed list of new message stems (`is a git repository that is not a gblog ledger`, `last_sync not written`, `is one record behind`, `defines a legacy remote named origin`, `is not a regular file; not pushed`), assert each appears in `CHANGELOG.md` and in at least one of `README.md` and `docs/*.md`.
  - This keeps the next round from shipping messages without notes.

---

## K9-06 (Low) — `_task_status` misses ids in escaped spellings and lets a stray file decide

**Where:** `gblog.py`, `_task_status()` (G1-06).

```python
    stored = redact(task_id) or task_id
    needle = json.dumps(stored, ensure_ascii=False).encode("utf-8", "backslashreplace")
    ...
                day = os.path.basename(p)[:10]
                if day >= since:
                    by_day.setdefault(day, []).append(p)
    ...
        for day in sorted(by_day, reverse=True):
```

1. **`_clean_text` is skipped.**
   - `append` stores `_clean_text(value)` and then redacts it. For an id with an undecodable argv byte, the stored id contains a literal backslash escape, and the line on disk holds it JSON-escaped (`\\udcff`).
   - The needle is built from the uncleaned id, so it never matches.
   - Every `log`, `note` and `task-end` for that task prints `has no task-start in the last 30 days` although the task is open.
2. **Only the `ensure_ascii=False` spelling is searched.**
   - A line that stores a non-ASCII id as `\uXXXX` is hash-valid and silent in a default `verify`, but is not found. Such lines come from a restore through another JSON tool or a hand-normalised file.
   - `_ReplayedIds.refresh` handles the same situation with `b"\\u" in raw`.
3. **A stray file sorts first.**
   - A `*.jsonl` file that is not a day file, such as `zz-backup.jsonl` (a `verify` warning only), has the "day" `zz-backup.`.
   - It passes `day >= since`, sorts before every date in reverse order, and so decides the status. A backup copy that holds an old `task_start` for the id makes a closed task look open.

### Fix

```python
    stored = redact(_clean_text(task_id)) or task_id
    needles = {json.dumps(stored, ensure_ascii=False).encode("utf-8", "backslashreplace"),
               json.dumps(stored).encode("ascii")}
    ...
                name = os.path.basename(p)
                if not _DAY_FILE_RE.match(name):
                    continue                      # not a day file: verify reports it; it never decides a task's state
                day = name[:10]
    ...
                        if any(n in raw for n in needles):
```

Also update DESIGN §13 (first table, `append` row) and FAQ 35. Both say "every day file of the last 30 days for an unknown id". The scan now stops after 8 MiB (`_TASK_SCAN_BYTES`) and then stays quiet.

### Tests (`TaskStatusTest`)

- `test_surrogate_task_id_is_found`: `append("ops", {"type": "task_start", "task_id": "caf\udcff", …})`, then `_task_status(ledger, "ops", "caf\udcff") == "open"`.
- `test_ascii_escaped_line_is_found`: write a hash-valid `task_start` line with `ensure_ascii=True` and a non-ASCII id; expect `open`.
- `test_stray_jsonl_does_not_decide`: chain with start and end today, plus `logs/ops/zz-backup.jsonl` holding only the start line; expect `closed`.

---

## K9-07 (Low) — after a refused replay the alert file names a spool file that was deleted

**Where:** `gblog.py`, `Ledger.spool_replay()` (G1-10 f).

```python
            except TailMismatchError as exc:
                try:
                    if os.path.abspath(exc.spool_path) != os.path.abspath(path):
                        os.remove(exc.spool_path)
                except OSError:
                    pass
                refused = True
                break
```

`_refuse_tail` writes the alert first, then the spool file. The handler removes the duplicate spool file but leaves `state/<bot>.alert.json` as written by that refusal:

- its `spool` key names the file that was just removed;
- its `record` is the transformed replay record, with `spooled_at`, `spool_id` and `original_ts`, not the original event;
- its `ts` is the time of the replay attempt.

`status` is unaffected, because `alert_info` reads the oldest pending spool file. An operator who follows the alert file, as the documents recommend, is sent to a missing path.

### Fix

Restore the alert from the still-pending original after removing the duplicate:

```python
                doc = _read_json(exc.alert_path)
                if doc is not None and payload is not None:
                    doc.update(ts=payload.get("ts", doc.get("ts")), record=payload.get("record"),
                               spool=os.path.relpath(path, self.root).replace(os.sep, "/"))
                    try:
                        self._atomic_json(exc.alert_path, doc)
                    except OSError:
                        pass
```

### Test (`SmallDefectTest`)

Extend `test_replay_does_not_spool_an_event_twice`. After the refused replay, load `state/ops.alert.json` and assert:

- the file named by `spool` exists;
- it equals the pending spool file;
- `"spool_id" not in alert["record"]`.

---

## K9-08 (Low) — day-rollup merge: three gaps around the new witness rule

**Where:** `gblog.py`, `Ledger._anchor()`, the merge block (G1-08).

```python
        merged = dict(rollup)
        day_doc = _read_json(day)  # None when missing, unparsable or not an object
        earlier = day_doc.get("chains") if day_doc is not None else None
        if isinstance(earlier, list):
            ...
        try:
            self._atomic_json(day, merged)
```

1. **An unreadable rollup is replaced, not kept.**
   - `_read_json` returns `None` for a missing file, and also for an unparsable one, one above 16 MiB (K2-06), or one whose `chains` is not a list.
   - In each of the last three cases the existing file is overwritten with this run's chains only.
   - A later `anchor --bot X` on the same day then removes every other chain's entry, which is the witness G1-08 was written to protect. `verify --all` loses the "anchored (day rollup …)" evidence for a chain deleted that day.
2. **`missing` is lost.**
   - A full run stores `missing` (deleted chains, `(witness files)`) in the rollup.
   - A later `anchor --bot X` builds `merged` from a rollup that has no such key, so the key disappears from the file.
3. **The kept entry hides today's verdict.**
   - When a head-less FAIL is appended to `later`, the top-level entry for that chain still says `ok: true`.
   - `rollup["chains"]` returned to the CLI is correct, but anything that reads `anchors/days/<date>.json` sees OK for a chain whose latest run failed.
   - `later` is not documented (K9-05).

### Fix

```python
        day_doc = _read_json(day)
        if day_doc is None and os.path.lexists(day):
            # unreadable: never overwrite what may be the only witness; keep it beside the new file
            aside = "%s.unreadable-%s.json" % (day[:-5], now_iso().replace("-", "").replace(":", ""))
            try:
                os.replace(day, aside)          # the name does not match _DATE_RE: readers ignore it, sync still backs it up
            except OSError:
                pass
            rollup.setdefault("missing", []).append({"bot": "(day rollup)", "ok": False, "problems": [
                "anchors/days/%s: unreadable; kept as %s" % (os.path.basename(day), os.path.basename(aside))]})
        earlier = day_doc.get("chains") if day_doc is not None else None
        ...
        if bots is not None and day_doc is not None and isinstance(day_doc.get("missing"), list) \
                and "missing" not in merged:
            merged["missing"] = day_doc["missing"]          # a --bot run does not re-scan for deleted chains
        ...
                    kept.append(dict(c, later=(later + [new])[-10:], latest_ok=False))   # readers: the newest run failed
```

Document `later` and `latest_ok` in DESIGN §8.

### Tests (`AnchorWitnessTest`)

- `test_unreadable_rollup_is_kept_aside`
  - Setup: anchor `aaa` and `zzz`; write `{not json` over today's rollup; run `anchor(["aaa"])`.
  - Expect: a file `…unreadable-….json` exists with the garbage; the returned rollup has a `(day rollup)` entry; the CLI exits 1.
- `test_bot_run_keeps_missing`
  - Setup: full run with a deleted chain, so `missing` is present; then `anchor(["keeper"])`.
  - Expect: the rollup file still has `missing`.
- Extend `test_headless_entry_does_not_replace_a_rollup_witness`: `entry["latest_ok"] is False`.

---

## Checked, no finding

These G1 paths were read for the same class of problem and look consistent with their tests:

- `_entry_kind` / `_real_dir` (G1-04), including a dangling link and a symlinked directory at `state/<bot>.json`.
- `_open_regular` in `_scan`, `_gunzip_sha256`, `_git_blob_id`, `_is_sqlite_file` and `_install_shell_hook` (G1-09). `O_NONBLOCK` has no effect on regular files, and the missing-attribute fallbacks cover Windows.
- The stamp loop in `sync` (G1-05 a). Every failure `update_state` can raise for a valid chain directory is an `OSError`; `write_name` cannot exit for a directory that exists.
- `_one_behind` (G1-10 b) in both `verify` and `_verify_reference`.
- The anchor cap (G1-10 e). `segment_heads` and the history line stay complete, so containment is unaffected.
- `names_of`, `_attributed` and the message constants (G1-12).
- `examples/routine-daily-audit.sh`. `print_heads` skips the `(witness files)` and `(day rollup)` lines, and prints `(no head)` for an `anchor aborted` chain even though the history file's last line is then stale.

One adjacent observation, not from G1: `_snapshot_extents` takes `(st_dev, st_ino)` from `os.stat(path)`, and `_snapshot_lines` compares them with `os.fstat` on the opened file. On a platform where those two calls disagree, every chain would report `log file was replaced or shortened while it was being verified`. I believe some Windows builds of Python 3.12 report different `st_dev` values for path and handle, but I did not confirm this, and Windows is untested in CI. Taking both values from `os.fstat` on a descriptor opened under the lock would remove the dependency.