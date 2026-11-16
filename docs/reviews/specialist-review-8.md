# Specialist review K8 — 2026-11-16

**Area.** Operator UX of failures in `gblog` 0.6.1 (current tree): every stderr message and exit path, whether the message is accurate and actionable, whether it quotes the right remedy command, recovery playbooks per `verify` FAIL kind, and `--json` error shapes.

**Method.** Read `gblog.py`, the docs and the tests. Nothing was run; every reproduction and quoted output below is derived from the code.

**Out of scope.** The round-4 G1/G2 items another engineer is fixing (sync foreign-repo guard, tail ts bound, sync cache removal, side-file rule, task-status function, origin definition, rollup protection, hook fallback) are not repeated.

**Compatibility rule for every fix.** No existing stdout line, exit code or record field changes unless stated. Additions are new stderr lines, text appended to an existing stderr line, or additive JSON keys. Where a test pins the current text, the finding names the test.

## Summary

| Id | Sev | Finding |
|---|---|---|
| K8-01 | High | `ALERT tail mismatch` prints one sentence for six different causes, makes false claims about state in three of them, and names no remedy |
| K8-02 | High | `seal` on an already-sealed chain says "on-disk chain does not match the pinned discontinuity"; the live-apply checklist promises "a break_ack is already present" |
| K8-03 | High | A `verify --contains` miss (the ROLLBACK signal) is attached to the wrong chain, with a 12-hex hash only |
| K8-04 | High | `sync` relays git's "integrate the remote changes (git pull)" advice on a rejected push, the opposite of the SECURITY.md rule |
| K8-05 | High | Exit 2 "may have written the record": `append` knows the answer and the CLI does not print it |
| K8-06 | Med | Strict-mode refusal says "(logging under 'x')" although nothing is logged; several messages hand a Bot an operator-only override |
| K8-07 | Med | `sync` hint "retry later" is wrong for URLs that can never be mapped; no hint for rate limits |
| K8-08 | Med | `anchor` FAIL lines give no reason; `anchor aborted: <class>` names no file; no `anchor --json` |
| K8-09 | Med | `verify` FAIL and `!` lines carry no remedy; the one quoted command (`gblog state rebuild`) is incomplete; state-behind and log-truncated print the same line (D2-10) |
| K8-10 | Med | `verify --json`: `missing: true` covers four situations; no machine-readable kind; no error contract |
| K8-11 | Med | Missing ledger and empty ledger are indistinguishable on read commands (exit 0) |
| K8-12 | Med | `spool replay` calls a refused event "unreadable"; no dismiss pointer; `state rebuild` does not name the next step; `spool list --bot <typo>` is silent |
| K8-13 | Med | A planted symlink produces `error: Too many levels of symbolic links: <path>` |
| K8-14 | Med | `capture shell-hook --install` prints "hook already present" when an outdated block could not be replaced |
| K8-15 | Low | `capture browser --hours`: two contradictory messages; README, CHANGELOG, code and tests disagree |
| K8-16 | Low | An unregistered non-ASCII `--bot` on read commands exits 2 with a write-side remedy |
| K8-17 | Low | stderr grammar: nine line styles, bare lines, `git failed:` without the subcommand, `push error:` for non-push errors |
| K8-18 | Low | No per-FAIL playbook in the docs; three FAIL kinds have no documented end state |

---

## K8-01 (High) — `ALERT tail mismatch` is one sentence for six causes

**Where.** `Ledger._refuse_tail`, fed by `Ledger._append_head` and `Ledger.append`.

```python
payload = {... "reason": "file tail does not match state head/hash/seq; append refused", ...}
raise TailMismatchError(_term(("bot %s tail seq/hash %s/%s != state %s/%s" + ...) % (
    bot, observed.get("seq"), str(observed.get("hash") or "")[:12], expected_seq, str(expected_head)[:12])), ...)
```

**What the operator sees today.**

| Real cause | Line after `ALERT tail mismatch: ` | What is wrong with it |
|---|---|---|
| state ahead of or behind the log | `bot ops tail seq/hash 2/ab12… != state 99/cdcd…` | correct, but no remedy and no direction |
| torn or garbage last line, state present | `bot ops tail seq/hash None/ != state 2/ab12…` | does not say the last line is corrupt |
| torn last line, state missing | `bot ops tail seq/hash None/ != state 0/genesis` | claims a genesis state; there is no state file |
| log files gone, state names a head | `bot ops tail seq/hash None/ != state 2/ab12…` | same text as the torn line |
| directory at `state/ops.json` (K2-08) | `bot ops tail seq/hash None/ != state 0/genesis` | claims a genesis state; the cause is a directory |
| FIFO, directory or device at the target day file (K2-10) | `bot ops tail seq/hash None/ != state 2/ab12…` | claims a mismatch; tail and state agree |

Each cause has a different remedy in `docs/faq.md` question 8 (`state rebuild`, restore the day file, remove the entry). The alert file's `reason` is the same constant for all six, and `status` prints `tail mismatch since …` for all six.

**Reproduction.**

```bash
export GBLOG_ROOT=$(mktemp -d); gblog note --bot ops --detail a
rm "$GBLOG_ROOT/state/ops.json"; mkdir "$GBLOG_ROOT/state/ops.json"
gblog note --bot ops --detail b      # ALERT tail mismatch: bot ops tail seq/hash None/ != state 0/genesis   (exit 4)
mkdir -p "$GBLOG_ROOT/logs/web/$(date -u +%F).jsonl"
gblog note --bot web --detail c      # ALERT tail mismatch: bot web tail seq/hash None/ != state 0/genesis   (exit 4)
```

Two unrelated causes produce the same line, and neither is a tail mismatch.

**Fix.**

1. Classify in `_append_head` and `append` by adding a key `cause` to the `observed` dict. The return tuple stays as it is, and the key lands in the spool and alert JSON as an additive field.
   - `state_not_regular`, `logs_missing`, `tail_corrupt`, `day_file_not_regular`;
   - `state_ahead` when `state.seq > tail.seq`;
   - `tail_rewritten` when the seqs are equal and the hashes differ;
   - `state_behind` when `state.seq < tail.seq` and it is not the roll-forward case.
2. In `_refuse_tail`, keep the current sentence as a prefix and append `; cause: <text>` on the same line. `test_alert_line_has_no_escapes` pins exactly three stderr lines, so no new line is added. Also copy `cause` to the payload top level, beside the unchanged `reason`.

   | `cause` | Text appended (`<b>` is the chain, `<f>` is `observed.file`) |
   |---|---|
   | `state_behind` | `state is N record(s) behind the log; operator: gblog verify --bot <b>, then gblog state rebuild --bot <b>` |
   | `state_ahead` | `state is ahead of the log (records are missing at the end of <f>); restore logs/<b>/ from backup; state rebuild will refuse` |
   | `tail_rewritten` | `the last record of <f> is not the one state names; restore that day file from backup` |
   | `tail_corrupt` | `the last line of <f> is not a valid record; restore that day file from backup (gblog never trims a line)` |
   | `logs_missing` | `state names a head but logs/<b>/ holds no log file; restore logs/<b>/ from backup` |
   | `state_not_regular` | `state/<b>.json is a directory; remove it, then operator: gblog state rebuild --bot <b>` |
   | `day_file_not_regular` | `<f> is not a regular file; remove it (the log tail and state agree)` |

3. For the state-missing and state-directory cases, print `state -/missing` and `state -/directory` where the line now says `0/genesis`. No test pins that substring.
4. The `status` ALERT line is pinned by tests and stays. Add `alert_cause` to `status --json` rows, read from the alert file.

**Tests.**
- One per cause: assert exit 4, exactly three stderr lines, `cause: ` present, and `json.load(alert)["cause"]` equals the code.
- State-directory case: `"genesis" not in err`.
- Day-file case: `"agree" in err`.
- Existing `test_refusal_survives_a_file_at_the_spool_directory` (`spool not written`) still passes.
- `status --json` row has `alert_cause`.

## K8-02 (High) — `seal` on a sealed chain reports a mismatch

**Where.** `Ledger.seal_break`:

```python
found = self.verify(bot).get("breaks") or []
if len(found) != 1 or not _same_break(found[0], desc):
    raise SealError("refusing: on-disk chain does not match the pinned discontinuity")
if any(rec.get("type") == "break_ack" ...):
    raise SealError("refusing: a break_ack is already present")
```

After a successful seal, `verify` returns `breaks == []`, so the first test fires. The "already present" message is unreachable in the normal case.

`docs/chain-repair-live-apply.md` says "Do not run `seal` again (it refuses: a `break_ack` is already present)". An operator who reruns step 7 during the live procedure reads "on-disk chain does not match", which the same checklist treats as stop-and-report.

The same sentence also covers three other states: no `kim-biseo` chain under this root (wrong `--root`), a chain with no break, and a chain with other breaks.

**Reproduction.**

```python
import shutil, tempfile, gblog
root = tempfile.mkdtemp()
for d in ("logs", "state"):
    shutil.copytree("tests/fixtures/kim-biseo-break/" + d, root + "/" + d)
L = gblog.Ledger(root); desc = L.verify("kim-biseo")["breaks"][0]
L.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
L.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
# SealError: refusing: on-disk chain does not match the pinned discontinuity
```

**Fix.** Keep both sentences as prefixes and append the state in parentheses. Tests assert `"on-disk" in err` for the mismatch case and for the no-chain case.

```python
res = self.verify(bot); found = res.get("breaks") or []
if res.get("approved_breaks"):
    raise SealError("refusing: a break_ack is already present (%s is already sealed, jay_approval_id=%s); nothing was appended"
                    % (bot, res.get("jay_approval_id")))
base = "refusing: on-disk chain does not match the pinned discontinuity"
if not self.log_files(bot):
    raise SealError("%s (no chain on disk for %s under %s; check --root / $GBLOG_ROOT)" % (base, bot, self.root))
if not found:
    raise SealError("%s (%s has no discontinuity: nothing to seal)" % (base, bot))
if len(found) != 1 or not _same_break(found[0], desc):
    b = found[0]
    raise SealError("%s (%d break(s) found; first at %s:%s seq %s->%s)"
                    % (base, len(found), b.get("file"), b.get("line_after"), b.get("seq_before"), b.get("seq_after")))
```

In the CLI, print `"seal: %s\n" % _term(str(exc))`, because the approval id comes from ledger content.

**Tests.**
- Seal the fixture copy, seal again: the message contains `a break_ack is already present` and `already sealed`.
- CLI on an empty root: exit 2, `on-disk` and `no chain on disk for kim-biseo` in stderr, nothing created.
- Fixture with the hashes of another copy: `1 break(s) found; first at logs/kim-biseo/2026-11-02.jsonl:165 seq 1919->1921`.

## K8-03 (High) — a `--contains` miss is reported on the wrong chain

**Where.** `_main`, `verify` branch:

```python
if wanted and (args.bot.strip() not in bots or b == args.bot.strip()):
    present |= set(...)                      # only the named chain is searched
...
if not_found and b == bots[-1]:              # ...but the miss goes to the last chain
    res["problems"].extend("expected hash %s not found in chain" % h[:12] for h in not_found)
```

**Reproduction.**

```bash
gblog note --bot cos --detail x; gblog note --bot cos-primary --detail y
gblog alias add --slug chief-of-staff --alias cos --alias cos-primary
gblog verify --bot cos --contains "$(printf 'ab%.0s' $(seq 32))"
# OK    cos                  1 records  head …
# FAIL  cos-primary          1 records
#       - expected hash abababababab not found in chain
```

- The daily routine says "report it under the word ROLLBACK with the chain name and the full hash". The output names a chain that was never searched and prints 12 of 64 hex characters.
- `--json` puts the problem on the `cos-primary` entry and carries no full hash.

**Fix.**
1. Decide where the miss is reported: `decide_at = exact if exact in bots else bots[-1]`. Compute and attach `not_found` when `b == decide_at`. In the exact-chain case `present` is complete at that point.
2. Keep the problem string, since tests pin it. Add two keys to that entry: `contains_missing` (full 64-hex list) and `contains_searched` (the chain names searched).
3. In text mode add one stderr line per miss:
   `hint: <full hash> was looked for in chain(s) <list>; a miss means a rollback, a rewrite or a wrong chain name (docs/routine-daily-audit.md, step 1)`.

**Tests.**
- The reproduction: `FAIL` is on `cos`, `cos-primary` is `OK`, exit 1.
- JSON: the `cos` entry has `contains_missing == [h]` and `contains_searched == ["cos"]`; the `cos-primary` entry has `ok: true`.
- `--bot "chief-of-staff"` (not a chain directory): the miss is on the last chain as today, with `contains_searched` listing both chains.
- Existing `test_contains_flag` and `test_verify_json` pass unchanged (single chain).

## K8-04 (High) — `sync` relays git's "git pull" advice after a rejected push

**Where.** `sync()`:

```python
except subprocess.CalledProcessError as exc:
    result["error"] = _safe_url((exc.stderr or exc.stdout or "").strip())[-400:]
```

A non-fast-forward rejection ends with git's own hint: `hint: … You may want to first integrate the remote changes (e.g., 'git pull ...') before pushing again.` The last 400 characters are exactly that hint, printed after `push error:`.

`SECURITY.md` says the opposite: "never fix it by rewriting history. A non-fast-forward push is itself a signal of a rollback: stop and report." A Bot that follows the printed advice merges remote history into the ledger tree.

**Reproduction.**

```bash
git init --bare -q /tmp/o.git; export GBLOG_ROOT=$(mktemp -d)
gblog note --bot ops --detail a; gblog sync --remote /tmp/o.git
br=$(git -C "$GBLOG_ROOT" rev-parse --abbrev-ref HEAD)
git clone -q -b "$br" /tmp/o.git /tmp/other
git -C /tmp/other -c user.name=x -c user.email=x@x commit -q --allow-empty -m other
git -C /tmp/other push -q
gblog note --bot ops --detail b; gblog sync    # exit 1; "push error: … integrate the remote changes (e.g. 'git pull ...')"
```

**Fix.**
1. Push with `-c advice.pushUpdateRejected=false`. The option goes after `-C root`, which git accepts, so the hint block is never produced.
2. Classify the failure. `_git_env` already forces the C locale. If stderr contains `[rejected]`, `non-fast-forward` or `fetch first`, set:
   `result["error"] = "push rejected: origin/<branch> holds commits this ledger does not have (non-fast-forward). This can mean a rollback, or another writer of the remote. Do not pull, merge, force-push or rewrite history; stop and report."`
   and the additive key `result["rejected"] = True`. Exit stays 1.
3. For any other push failure keep the redacted git text, cut on a line boundary.

**Tests.**
- The reproduction: exit 1, `push rejected:` in stdout, neither `git pull` nor `integrate` in stdout or stderr, `(not pushed)` present.
- An authentication-style failure (mock `_git` raising with `fatal: Authentication failed`): the text is passed through as before.
- Existing `test_push_error_is_redacted_before_it_is_cut` still passes.

## K8-05 (High) — exit 2 does not say whether the record was written

**Where.** `main()`'s `OSError` handler prints `error: <strerror>[: <path>]` only. The README, the skill and the agent guide all tell the caller to run `gblog tail --bot B -n 1` before retrying, because "the failing step may come after the log line is durable".

`Ledger.append` knows which side of the line the failure was on:

```python
self._append_line(path, ...)       # failure here: unknown (a failed fsync can follow a complete write)
...
self.write_state(bot, state)       # failure here: the record IS in the log
```

The same gap exists for `error: internal error (…); set GBLOG_DEBUG=1 for a traceback`. That line invites a rerun of a command that may already have appended.

**Fix.** Tag the exception; no new exception type is needed.

```python
try: self._append_line(path, line)
except OSError as exc: exc.gblog_append = ("unknown", bot, full["seq"], full["hash"]); raise
...
try: self.write_state(bot, state)
except OSError as exc: exc.gblog_append = ("written", bot, full["seq"], full["hash"]); raise
```

In `main()`, after the `error:` line:
- `written`: `notice: the record IS in the log (<bot> #<seq> <hash12>); do not run the command again. state/<bot>.json is repaired by the next append`
- `unknown`: ``notice: the record may be in the log; check `gblog tail --bot <bot> -n 1` before you retry``

Untagged exceptions print as today. For the internal-error line, append ``; check `gblog tail -n 1` before rerunning a write command`` when `args.cmd` is a write command.

**Tests.**
- `test_enospc_in_write_state_recovers` pattern: exit 2, the `error:` line, plus `notice: the record IS in the log (ops #2 `.
- `os.fsync` failing inside `_append_line`: the `may be in the log` notice.
- `test_top_level_handlers` (mocked `append`): output unchanged.
- `archive` with a failing state write: the notice is present and the stored file is kept (existing test).

## K8-06 (Med) — strict-mode refusal claims the record is being logged

**Where.** `_resolve_bot`:

```python
msg = "bot name '%s' is not registered in state/aliases.json (logging under '%s'); register it with: gblog alias add --slug <slug> --alias '%s'" % (name, slug, name)
if data.get("strict"): _die(msg)
```

**Reproduction.** `gblog alias add --slug ops; gblog alias strict on; gblog note --bot stranger --detail x` prints `error: bot name 'stranger' is not registered in state/aliases.json (logging under 'stranger'); register it with: …` and exits 2. Nothing is logged.

Second problem: the remedy is addressed to whoever reads stderr, usually the Bot. `alias add` is operator-only (`docs/agent-integration.md` section 12). The same pattern appears in three more messages:

| Message | Operator-only action it suggests |
|---|---|
| `refused: remote … is PUBLIC …; backups must be private. Use --allow-public to override.` | `--allow-public` |
| `hint: retry later, or pass --allow-unknown-visibility if you know the repository is private` | `--allow-unknown-visibility` |
| `error: origin is already …; pass --replace-remote to send backups to … instead` | `--replace-remote` |

**Fix.**
- Strict: `bot name '<n>' is not registered in state/aliases.json; nothing was logged (alias strict is on). Use the registered slug (gblog alias list), or ask the operator to run: gblog alias add --slug <slug> --alias '<n>'`
- Non-strict: keep the text up to the semicolon, which the docs quote, then `; ask the operator to register it: gblog alias add …`.
- The three sync messages: keep every word tests look for (`refused`, `PUBLIC`, `origin is already`, `--replace-remote`) and replace the imperative with `operator decision: --allow-public` / `--allow-unknown-visibility` / `--replace-remote`.

**Tests.**
- Strict: `"logging under" not in err`, `"nothing was logged" in err`, exit 2, no chain directory created.
- Non-strict: the warning still starts with the documented prefix.
- sync: existing assertions pass; `"Use --allow-public" not in out`.

## K8-07 (Med) — `sync` says "retry later" for a refusal that never clears

**Where.** `sync()`:

```python
elif unknown_github and not (allow_public or allow_unknown):
    refusal = "refused: could not confirm %s is private (%s)" % (...)
    sys.stderr.write("hint: retry later, or pass --allow-unknown-visibility if you know the repository is private\n")
```

When the reason is `github.com URL that cannot be mapped to a repository` (a gist, `[github.com:22]:o/r.git`, a `<transport>::` prefix, an unparsable URL), a retry gives the same result every night. The remedy in FAQ 23 ("write it as `https://github.com/<owner>/<repo>`") is not printed.

**Reproduction.** `gblog sync --remote '[github.com:22]:o/r.git'` exits 3 with `hint: retry later, …`.

**Fix.** Choose the hint from `vis.get("reason", "")`:

| Reason | Hint |
|---|---|
| starts with `github.com URL that cannot be mapped` | `hint: this URL form cannot be checked; the operator rewrites origin as https://github.com/<owner>/<repo> (gblog sync --remote <url> --replace-remote)` |
| contains `HTTP 403` or `HTTP 429` | `` hint: the GitHub API refused the lookup (rate limit); sign in with `gh auth login` on this computer, or retry later `` |
| anything else | `hint: the lookup failed (<reason>); retry later` |

The override flag is mentioned only as "operator decision" (K8-06).

**Tests.** Fake `remote_visibility` returning each reason: exit 3 and the matching hint; no `retry later` in the first case.

## K8-08 (Med) — `anchor` FAIL lines say nothing about why

**Where.** `_main`, `anchor` branch:

```python
out.write("ANCHOR FAIL %-20s break %s heads %s\n" % (...))
for prob in c.get("problems") or []:
    if str(prob).startswith("anchor aborted"):
        out.write("      - %s\n" % _term(prob))
```

Four separate gaps:

1. An edited chain prints `ANCHOR FAIL ops   break - heads ab12…` and nothing else. The routine runs `anchor` "even if verify exited 1", and `examples/routine-daily-audit.sh` reduces it to `ops FAIL (no head)`.
2. `anchor aborted: IsADirectoryError` names the class, not the file. `exc.filename` and `exc.strerror` are dropped in `Ledger._anchor`. Every other OSError-derived problem line names its path.
3. `anchor --bot typo` prints verify-format lines (`FAIL  typo  0 records`), not `ANCHOR FAIL …`. A script that reads `ANCHOR` lines sees nothing; the example script prints `(no chains anchored)`.
4. There is no `anchor --json`. The routine needs full 64-hex heads, and both documented ways scrape files: a Python snippet over `anchors/*.json` in `docs/agent-integration.md` 11.2, and `tail -n 1 | grep -oE` over the history in the example script.

**Fix.**
1. Print every problem of a FAIL chain as `      - <problem>` lines, capped at 5 with `      - … N more (gblog verify --bot <chain>)`.
2. In `_anchor`, when `exc` is an `OSError` with a filename, append a second problem `"<relative path>: <strerror>"`. The first stays `anchor aborted: <class>`; tests pin that exact line and `problems[0]`.
3. For the unknown name, print `ANCHOR FAIL <name> break - heads` before the two existing lines.
4. Add `anchor --json`: print the rollup dict `Ledger.anchor()` already returns (`chains`, `missing`), with the same exit codes. Document that entries named in parentheses have no head.

**Tests.**
- Tampered chain: `      - logs/ops/…:1: hash mismatch (record edited)` appears under `ANCHOR FAIL ops`.
- Directory at `anchors/aaa.history.jsonl`: both `anchor aborted: IsADirectoryError` and `anchors/aaa.history.jsonl: Is a directory` are printed, and `zzz` is still anchored.
- `anchor --bot nosuch`: one line starts with `ANCHOR FAIL nosuch`; nothing is created.
- `anchor --json`: valid JSON; `chains[*].head` are 64 hex; exit 1 with a FAIL chain.

## K8-09 (Med) — `verify` lines carry no remedy; state-behind and truncation look the same

**Where.** `Ledger.verify` and `_verify_text`.

- `state/<b>.json: head does not match last record (state tampered or log truncated)` is printed in two very different cases:
  - state is one record behind after an interrupted append. This is benign, and the next append or `state rebuild` repairs it;
  - the log lost its tail. This needs a restore, and `state rebuild` refuses.

  This is the open item D2-10. It is not in the G1/G2 list, so a precise fix follows.
- With `verify --all --record`, the benign case also appends a permanent `verify fail` record, because the record append itself rolls state forward.
- Three sibling warnings quote remedies inconsistently:
  - `… appends are refused until `gblog state rebuild` is run` quotes a command that fails as written; `--bot` is required;
  - `state/<b>.json missing or unreadable: …` quotes nothing;
  - `state/<b>.json was reset to genesis` quotes nothing.

  All three have the same remedy and all three texts are pinned by tests and docs.

**Fix.** Add hints without touching pinned text or the dict that `_verify_reference` must equal.

1. In `verify`, read the state head before the walk and add it to `needed`. After the walk set a non-dict attribute, like `hashes`:
   - `res.state_relation = "one_behind"` when the state head is in `hashes` and equals the `prev` of the last record;
   - `"behind"` when it is in `hashes` otherwise;
   - `"not_in_chain"` when it is not;
   - `None` when there is no state problem.
2. Add a CLI-layer function `_fail_hints(bot, res) -> List[str]` that maps problem and warning classes to one line each.
3. Text mode prints the hints on stderr after the chain block as `hint: <chain>: <text>`. With `--json` they are the additive key `hints` on the entry. Update the "exactly these keys" sentence in `docs/DESIGN.md` section 7.

   | Trigger | Hint |
   |---|---|
   | state problem, `one_behind` | `` an append was interrupted (state is one record behind); the next append repairs it, or operator: `gblog state rebuild --bot <b>` `` |
   | state problem, `behind` | `` state names an earlier record of this chain; operator: `gblog state rebuild --bot <b>` `` |
   | state problem, `not_in_chain` | `state names a head that is not in the log: the end of the log was removed or rewritten; restore logs/<b>/ from backup (state rebuild refuses)` |
   | warning: seq mismatch, missing/unreadable, reset to genesis | `` operator: `gblog state rebuild --bot <b>` `` |
   | `no chain on disk for` | `` the name selects nothing: `gblog alias list` `` |
   | `anchored head … no longer in the chain`, `expected hash …` | `` treat as ROLLBACK: find the commit that still holds the head with `git -C <root> log --oneline -S<head> -- logs/<b>` `` |
   | `archived file missing`, `content hash mismatch`, `unreadable (` | `` restore the file from the backup: `git -C <root> log --oneline -- <path>` `` |
   | `not a regular file`, `cannot be listed` | `remove or repair that entry (ls -l shows what it is); gblog never opens it` |
   | `verify aborted` | `` rerun with GBLOG_DEBUG=1: `GBLOG_DEBUG=1 gblog verify --bot <b>` `` |

**Tests.**
- Interrupt an append (`state_write_failing(1)`), then `verify --bot ops`: exit 1, the pinned problem line, stderr hint contains `interrupted`.
- Drop the last log line: hint contains `restore logs/ops/ from backup`.
- `--json`: `entry["hints"]` is a list of strings; `dict(ledger.verify(b)) == dict(ledger._verify_reference(b))` still holds (existing `_same`).
- Healthy chain: no `hint:` on stderr and no `hints` key (or an empty list).

## K8-10 (Med) — `verify --json`: one flag for four situations, no error contract

**Where.** `_verify_missing` and its three callers. Every synthetic entry is `{"missing": true, "ok": false, "records": 0, "head": null, …}`. A consumer has to match English text to tell them apart:

| Situation | `bot` | First problem text |
|---|---|---|
| name typo | as given | `no chain on disk for '<n>'` |
| chain deleted, witness survives | chain | `… but logs/<n> is missing` |
| invalid directory | dir name | `… not a valid chain key …` |
| witness directory unlistable | `(witness files)` | `… cannot be listed …` |

The routine must treat row 1 as "wrong name" and row 2 as ROLLBACK. Row 4 is not a chain at all, yet it carries `missing: true`.

Second gap: there is no stated contract for `--json` when the command fails. Today stdout is either a complete document or empty for `verify`, `status`, `coverage` and `tune`. That is true from the code, but nothing documents it and nothing tests it.

**Fix.**
1. Add the additive key `kind` on synthetic entries: `unknown_name`, `missing_chain`, `invalid_dir`, `witness_files`. Add `kind: "chain"` on real entries at emit time, in the CLI layer. `aborted: true` stays as it is.
2. Document the contract in `docs/DESIGN.md` section 7: "with `--json`, stdout is one complete JSON document or empty; exit 2 always means empty stdout for `verify`".
3. Optional and opt-in: with `GBLOG_ERRORS=json`, `main()` writes one extra final stderr line:
   `gblog-error: {"exit": 4, "kind": "tail_mismatch", "bot": …, "cause": …, "spool": …, "alert": …}`
   Kinds are `usage`, `environment` (with `written` from K8-05), `tail_mismatch` and `internal`. Default output is unchanged.

**Tests.**
- `verify --all --json` with a typo'd `--bot`, a deleted anchored chain, `logs/my bot`, and a patched `os.listdir` for `anchors/days`: the four `kind` values.
- For each of the four `--json` commands with a usage error: `out == ""` and exit 2.
- `GBLOG_ERRORS=json gblog note …` with a broken state: the last stderr line parses after the `gblog-error: ` prefix and has `exit == 4`.

## K8-11 (Med) — a missing ledger looks like an empty one

**Where.** The `_main` read paths, and `Ledger.ensure` / `Ledger.anchor`.

- `gblog --root /typo verify --all` prints `no bots logged yet under /typo` and exits 0.
- `status` prints `(no records yet)` and exits 0; `verify --all --json` prints `[]`.
- A plain `gblog anchor` on a missing root creates a ledger (`ensure()`), writes an empty day rollup and exits 0.

For this reason the routine prompt needs a `test -d` step before any gblog command, and `examples/routine-daily-audit.sh` re-implements `default_root()` in bash. That duplicate can drift.

**Fix.**
1. Read commands (`verify`, `status`, `tail`, `export`, `coverage`, `spool list`, and `report` / `tune` with `--stdout`): when `logs/` does not exist, print on stderr
   `warning: no ledger at <root> (logs/ does not exist); check --root / $GBLOG_ROOT`.
   Exit code and stdout are unchanged. `status --json` gains `"ledger_exists": false`.
2. Opt-in guard `GBLOG_REQUIRE_LEDGER=1`:
   - `Ledger.ensure()` refuses to create a fresh tree: ``error: no ledger at <root>; run `gblog init` there or fix --root / $GBLOG_ROOT``, exit 2;
   - the read commands above exit 2 with the same line.

   The routine prompt and the example script can then export the variable and drop their own check.

**Tests.**
- Missing root: `verify --all` exit 0 with the warning; nothing created.
- With the env var: `verify --all`, `status`, `anchor` and `note` all exit 2 and create nothing.
- With the env var, `gblog init` followed by `note` works.
- Existing `test_report_date_with_no_records_anywhere` passes (it checks stdout and the absence of a traceback).

## K8-12 (Med) — spool recovery messages

**Where.** `Ledger.spool_replay` and the `spool` / `state` branches of `_main`.

1. **Wrong word.** `except RecordError: skipped.append(path)` puts an event that `append` refused by content into `skipped`. The CLI then prints `spool replay: <path> is unreadable; left in place`. The file is valid JSON; the operator opens it and finds nothing wrong.
2. **No way out is named.** An unreadable file, a refused event or a spooled `break_ack` makes `spool replay` exit 2 on every run, and the alert never resolves. The only dismiss procedure is the README's "Upgrading" item 3; the message does not point there.
3. **Refused again.** `ALERT tail mismatch: <c> still does not match state; N spooled event(s) left in spool/<c>` (exit 4) names no next command.
4. **Missing next step.** `gblog state rebuild --bot B` prints `rebuilt B seq N head H` and stops, even when spool files are pending. FAQ 8 step 3 (`spool replay`) is the next action.
5. **Silent typo.** `spool list --bot <typo>` prints `no spooled events` and exits 0, while `spool replay --bot <typo>` exits 2 (D5-06) and the read commands warn (K1-03).

**Fix.**
1. Collect `(path, str(exc))` in a new result key `invalid`. `skipped` keeps unreadable files only. CLI: `spool replay: <path>: event refused (<reason>); left in place`. Exit stays 2.
2. After any skipped, invalid or rejected line, print once: `note: to dismiss a spool file without replaying it, see README "Upgrading", item 3 (operator)`.
3. Append `; operator: gblog verify --bot <c>, then gblog state rebuild --bot <c> (docs/faq.md, question 8)` to the refused line.
4. After a rebuild, when `ledger.spool_files(chain)` is non-empty, print on stderr: `note: <n> spooled event(s) are pending for <chain>; next: gblog spool replay --bot <chain>`.
5. `spool list --bot X`: when no chain, spool directory or alert file exists for any chain of X, print the K1-03 warning (`no chain on disk for '<X>'; nothing to show …`). Exit stays 0.

**Tests.**
- Plant a spool file whose record has `task_id: ["x"]`: exit 2, `event refused (record field 'task_id' must be a string` in stderr, `"is unreadable" not in err`, file still in place.
- `{not json` file: still `is unreadable`.
- Rebuild with one pending spool file: stderr contains `next: gblog spool replay --bot ops`.
- `spool list --bot typo`: the warning, exit 0.

## K8-13 (Med) — planted symlinks surface as "Too many levels of symbolic links"

**Where.** `main()`'s `OSError` handler. The sources are `O_NOFOLLOW` opens in `_append_line`, `_open_lock` and `_open_private`, and the deliberate `raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)` in `_write_markdown`.

**Reproduction.** `ln -s /tmp/x "$GBLOG_ROOT/reports/$(date -u +%F).md"; gblog report` prints `error: Too many levels of symbolic links: …/reports/<date>.md` and exits 2. The same text appears for a symlinked day file, where the event is lost and not spooled, and for a symlinked lock file.

`verify` uses its own wording for the same condition: `symbolic link (gblog does not write through one)`.

**Fix.** In the handler:

```python
if exc.errno == errno.ELOOP and exc.filename and os.path.islink(exc.filename):
    detail = "%s is a symbolic link; gblog does not write through one (replace it with a regular file or remove it)" % exc.filename
```

For the day-file case add a second line: `notice: nothing was written and nothing was spooled`.

**Tests.** Symlinked report, day file and lock file: exit 2, `is a symbolic link; gblog does not write through one` in stderr, `"Too many levels" not in err`. The victim file is untouched (existing tests).

## K8-14 (Med) — "hook already present" when the outdated hook could not be replaced

**Where.** `_install_shell_hook`:

```python
if end_at < 0 or existing[start:at].strip() not in (b"", b"#"):
    return "present"
```

The CLI maps `present` to `hook already present in <rc>`, exit 0.

**Reproduction.** An rc file whose block has no end marker, or has text before the start marker on the same line. `gblog capture shell-hook --bot ops --install` prints `hook already present in ~/.bashrc`. The old hook stays, and it skips every command that mentions `gblog` (B:L5).

The README's "Upgrading" item 6 tells the operator to check for `# gblog hook v2` by hand; the command itself gives no sign.

**Fix.** Keep the return value, which a test pins. In the CLI branch, after `present`, read the rc file. If `shell_hook(bot).encode().rstrip(b"\n")` is not contained in it, print on stderr:

`warning: <rc> holds a gblog hook block that is not the current hook and could not be replaced (no end marker, or text before the start marker); remove the block by hand and run this command again`

Exit stays 0. Stdout is unchanged.

**Tests.**
- Block without end marker: the warning is on stderr and the file is unchanged.
- Current block: no warning.
- Old well-formed block: `updated hook in …`, no warning.

## K8-15 (Low) — `capture browser --hours` has two contradictory messages

**Where.** `_validate_args`:

```python
if ... args.hours < 0: raise _UsageError("--hours must be 0 or more")
if ... args.hours < 1: raise _UsageError("--hours must be 1 or more (a window of 0 hours imports nothing)")
```

`--hours=-5` is told that 0 is fine; `--hours=0` is then refused. The sources disagree:

| Source | Says |
|---|---|
| README | "a negative value exits 2" |
| CHANGELOG | "`--hours 0` imports nothing on a first run"; "refused when it is negative, not when it is below 1" |
| code | 0 exits 2 |
| `test_capture_browser_negative_hours_is_usage_error` | pins `--hours must be 0 or more` for -5 |
| `test_hours_zero_is_a_usage_error` | pins exit 2 for 0; its comment describes the opposite bug |

**Fix (needs one maintainer decision).** Preferred: one rule and one message, `--hours must be 1 or more (a window of 0 hours imports nothing)`. Then:
- change the one assertion in `tests/test_deep_reviews.py` to that text;
- correct the README sentence and the two CHANGELOG sentences;
- delete the stale comment in `tests/test_deep_reviews_b.py`.

The alternative is to accept 0 and drop the second check and `test_hours_zero_is_a_usage_error`. That keeps a silent no-op, so it is not recommended.

**Tests.** `-5` and `0` both exit 2 with the same line, and no state file is created. `1` works.

## K8-16 (Low) — unregistered non-ASCII `--bot` on read commands exits 2

**Where.** `Ledger.resolve`: `_die("'%s' has no ASCII letters or digits; register it first: gblog alias add …")`. It is reached from `chains_on_disk` and `canonical` in the `verify`, `anchor`, `report`, `status`, `tail`, `export`, `coverage`, `tune` and `spool list` paths.

**Reproduction.** With no alias registered for the name:

| Command | Exit | Output |
|---|---|---|
| `gblog verify --bot 김비서` | 2 | `error: '김비서' has no ASCII letters or digits; register it first: …` |
| `gblog verify --bot nosuch` | 1 | `FAIL … no chain on disk for 'nosuch'` |
| `gblog status --bot 김비서` | 2 | the same `error:` line |
| `gblog status --bot nosuch` | 0 | warning |

The same condition, "this name selects nothing", gives three exit codes, and the remedy shown is about writing.

**Fix.** Add `Ledger.resolvable(name) -> bool`: the registry passes of `resolve`, then "sanitised form is non-empty". In `_main`:
- `verify` / `anchor`: when not resolvable, take the existing "no chain on disk" FAIL path, exit 1. Guard the later `ledger.canonical(args.bot)` call in the same way.
- readers and `spool list`: print the K1-03 warning and produce empty output, exit 0.
- write commands: unchanged (exit 2 with the register message, reworded as in K8-06).

**Tests.**
- `verify --bot 김비서`: exit 1, `no chain on disk for`.
- `status --bot 김비서`: exit 0, the warning.
- `note --bot 김비서`: exit 2 as today.
- After `alias add --slug kim --display 김비서` all three resolve.

## K8-17 (Low) — stderr grammar

**Where.** Every `sys.stderr.write` in `gblog.py`. Current line styles:

| Style | Examples | Exit |
|---|---|---|
| `error: …` | `_die`, `_UsageError`, `OSError`, internal error | 2, or 1 for internal |
| `error: …` inside a run that continues | `_record_verify` OSError path | 1 |
| `<command>: …` | `seal:`, `state rebuild:`, `archive:`, `spool replay:` | 2 |
| `git failed: …` | sync | 2 |
| bare | `no proposals yet; …`, `unknown proposal id …` | 2 |
| bare | `no browser history database found; pass --history PATH` | 1 |
| bare | `no spooled events`, `wrote <path>` | 0 |
| `ALERT …`, `spool: …`, `alert: …` | tail mismatch | 4 |
| `warning:`, `notice:`, `note:`, `hint:` | four advisory prefixes; `note:` and `notice:` are used for the same purpose | – |

Concrete defects:
- **Bare exit-2 lines are invisible to `grep '^error: '`.** CI's restricted-env job uses exactly that grep. The texts are pinned by `test_tune_apply_error_texts_are_unchanged`.
- **`git failed: <git stderr>` does not name the git subcommand.** With empty git output it prints `git failed: ` and nothing else.
- **`push error:` labels every `result["error"]`.** That includes `committed content differs from disk …` after `--no-push`, where no push was attempted.
- **A missing `git` binary** prints `error: No such file or directory: 'git'`.
- **`warning: could not confirm the remote is private (not a github.com remote; visibility unknown)`** appears on every sync to a non-GitHub remote and does not name the URL.

**Fix.**
1. Document the grammar as a table in `docs/agent-integration.md` section 6. State that `note:` equals `notice:`, and list the bare lines as legacy.
2. Add a check to `tests/test_meta.py`: scan `gblog.py` with `ast` for string literals passed to `sys.stderr.write`. A literal must start with an allowed prefix or be in an explicit legacy list. New bare lines then fail CI.
3. `git failed (git <sub>, exit <n>): <text>`. The prefix `git failed` is kept, so existing greps still match.
4. In `_git_call`, turn `FileNotFoundError` into ``_UsageError("git was not found on PATH; `gblog sync` needs it")``.
5. Non-GitHub warning: `warning: <safe url> is not on GitHub, so gblog cannot check its visibility; keeping it private is the operator's responsibility`.
6. Drift after `--no-push`: print `  error: …` where it now says `  push error: …` only when `push` was never attempted. `docs/faq.md` quotes `push error:` for push failures, which stay.

**Tests.**
- The meta test.
- sync with a failing `git commit` (mock): stderr starts with `git failed (git commit, exit `.
- `PATH` without git: exit 2, `git was not found`.
- Local bare remote: the warning names the path.
- Drift with `--no-push`: `"push error" not in out`.

## K8-18 (Low) — no per-FAIL playbook; three kinds have no end state

**Where.** `docs/faq.md` question 2 lists FAIL texts with "what happened" only. Question 8 covers the tail mismatch. The README's "Upgrading" item 4 covers retired chains. Nothing tells the operator what to do for the other kinds. Three situations have no documented end state at all:

1. **Tail records lost and not in any backup.** State is ahead, `state rebuild` refuses, and every append exits 4 forever. The docs stop at "restore the log from backup".
2. **`anchors/<b>.history.jsonl:<n>: corrupt anchor history line`.** A process killed inside `anchor` can leave a torn line. Every later `verify` FAILs, the next `anchor` appends after it, and no gblog command repairs a witness file.
3. **Permanent record-level FAILs.** These are `timestamp goes backwards`, a second or foreign `break_ack`, and an `archive` record with a bad address. The chain FAILs in every daily run with no "known FAIL" state.

Two pitfalls also belong in the playbook:
- A safety copy of a damaged day file must not end in `.jsonl` inside `logs/<bot>/`; it would join the chain walk. Copy it outside `logs/`.
- `git pull` is never a remedy (K8-04).

**Fix (docs only).** Add the table below to `docs/faq.md` as a new question after question 2, and link it from `_fail_hints` (K8-09). For end states 1 and 3 the maintainers must decide and write down one sanctioned procedure. Suggested: freeze the chain in place, record `gblog note --bot ledger --detail "chain X frozen at head <64-hex>: <cause>"`, continue under a successor slug, and have the routine report the chain as `FAIL (known since <date>)`. For end state 2: restore the history file from the backup commit, then run `gblog anchor`.

**Tests.** A doc test in `tests/test_meta.py`: every problem-format string literal in `Ledger.verify`, `verify_archives`, `_anchor_checks`, `_file_findings` and the synthetic entries has a row in the FAQ table, matched on a stable fragment such as `hash mismatch (record edited)`.

---

## Appendix A — playbook per `verify` FAIL kind (proposed FAQ content)

`<root>` is the ledger root and `<b>` the chain. Every step is for the operator. A Bot reports the lines verbatim and stops.

| FAIL text (fragment) | Meaning | Operator steps | End state |
|---|---|---|---|
| `not valid JSON`, `hash mismatch (record edited)`, `malformed record fields`, `seq …, expected …`, `broken chain` | a line was damaged, edited, removed or inserted | 1. stop writing to `<b>`; 2. copy the day file outside `logs/`; 3. `git -C <root> log --oneline -- logs/<b>/<day>.jsonl`, then `git -C <root> checkout <commit> -- logs/<b>/<day>.jsonl`; 4. `gblog verify --bot <b>` | OK, or the state row below if records after `<commit>` are gone |
| `timestamp goes backwards` | a record is dated before its predecessor | no repair exists | frozen chain (K8-18) |
| `state/<b>.json: head does not match last record` | state and log disagree | read the hint (K8-09): interrupted or behind → `gblog state rebuild --bot <b>`; not in chain → restore `logs/<b>/` | OK after rebuild or restore; otherwise frozen |
| `anchored head … is no longer in the chain`, `expected hash … not found` | ROLLBACK or rewrite | `git -C <root> log --oneline -S<64-hex head> -- logs/<b>` finds the commit that still holds it; compare with the remote; restore the day files from that commit | OK after restore; otherwise report as ROLLBACK |
| `corrupt anchor history line` | torn or edited witness line | `git -C <root> checkout <commit> -- anchors/<b>.history.jsonl`, then `gblog anchor --bot <b>` | OK |
| `archived file missing`, `content hash mismatch`, `<path>: unreadable (` | evidence file removed or changed | `git -C <root> log --oneline -- <path>`, `git -C <root> checkout <commit> -- <path>`, `gblog verify --bot <b>` | OK |
| `invalid archive path`, `no 64-hex sha256`, `not addressed by its own sha256`, `no path` | a hand-written `archive` record | none | frozen chain |
| `<path>: not a regular file` (log entry, anchor file, history, archive, lock) | FIFO, directory, device or dangling link planted | `ls -l <path>`; remove it; `gblog verify --bot <b>` | OK |
| `state/<b>.json: not a regular file (a directory…)` | directory at the state path | `rmdir`, then `gblog state rebuild --bot <b>`, then `gblog spool replay --bot <b>` if events were spooled | OK |
| `logs/<b>: cannot be listed`, `(witness files)` … `cannot be listed` | permissions | fix the mode of that directory; rerun | OK |
| `chain was anchored … but logs/<b> is missing`, `state/<b>.json records … but logs/<b> is missing` | chain directory deleted | restore `logs/<b>/` from backup, or retire it (README "Upgrading", item 4) | OK |
| `directory name is not a valid chain key` | foreign directory under `logs/` | move it out of `logs/` | OK |
| `no chain on disk for '<n>'` | wrong name | `gblog alias list`; rerun with the slug | – |
| `break_ack is not the pinned …`, `multiple break_ack …`, `malformed break_ack`, `does not match the discontinuity`, `outside a verified segment` | forged or misplaced acknowledgement | none; stop and report | frozen chain |
| `log file was replaced or shortened while it was being verified` | something other than gblog writes in `logs/` | rerun; if it repeats, find the writer | – |
| `verify aborted: <class>` | bug or unreadable input | `GBLOG_DEBUG=1 gblog verify --bot <b>`; file an issue with a fabricated reproduction | – |

## Appendix B — messages that quote a remedy, and whether it is right

| Message (source) | Quoted remedy | Verdict |
|---|---|---|
| seq-mismatch warning (`verify`) | `` `gblog state rebuild` `` | incomplete: `--bot` is required (K8-09) |
| state missing / reset warnings (`verify`) | none | missing (K8-09) |
| `ALERT tail mismatch` (`_refuse_tail`) | none | missing; wrong claims (K8-01) |
| `spool replay` refused | none | missing (K8-12) |
| not registered (`_resolve_bot`) | `gblog alias add …` | operator-only; false "logging under" in strict mode (K8-06) |
| `refused: … PUBLIC … Use --allow-public to override.` | `--allow-public` | operator-only (K8-06) |
| `hint: retry later, or pass --allow-unknown-visibility …` | retry / override | wrong for unmappable URLs (K8-07) |
| `origin is already …; pass --replace-remote …` | `--replace-remote` | operator-only (K8-06) |
| `push error: <git text>` | git's `git pull` hint | contradicts SECURITY.md (K8-04) |
| `seal: refusing: on-disk chain does not match …` | none | wrong sentence for a sealed chain (K8-02) |
| `--ts … is earlier than the last record …; omit --ts and put the original time in --detail` | omit `--ts` | correct |
| `state/aliases.json is unreadable and will not be overwritten; restore it from backup or move it away …` | restore / move | correct |
| `no origin configured in the ledger repository; set one with `gblog sync --remote <url>`` | `sync --remote` | correct; add "operator" |
| `detached HEAD; pass --branch` | `--branch` | correct |
| `skipped N unreadable line(s) in logs/<c>; run gblog verify` | `gblog verify` | works; `--bot <c>` would be more precise |
| `no proposals yet; run `gblog tune` first` | `gblog tune` | correct; bare line (K8-17) |
| `error: tuning/proposals.json is corrupt` | none | the remedy is "run `gblog tune`, it replaces the file"; the text is pinned by `test_tune_apply_error_texts_are_unchanged` |

## Appendix C — suggested order of work

1. K8-02, K8-03, K8-04. Each is a small code change with no pinned text.
2. K8-01 and K8-05. Both touch `append`; land them together with their tests.
3. K8-09 and K8-10 (hints and `kind`), then the FAQ table of K8-18, which the hints link to.
4. K8-06, K8-07, K8-08, K8-12, K8-13, K8-14.
5. K8-11, K8-16, K8-17.
6. K8-15, after the maintainer decision.