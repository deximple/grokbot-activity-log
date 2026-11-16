# Security policy

## Reporting a vulnerability

Report privately. Open a **private security advisory** on the repository: GitHub → *Security* →
*Advisories* → *Report a vulnerability* on `deximple/grokbot-activity-log`. Do not open a public
issue or pull request for a vulnerability.

Include:

- the gblog version (`gblog --version`), Python version and platform;
- the smallest sequence of commands or file edits that reproduces the problem on a **fresh temporary
  ledger** (`GBLOG_ROOT=$(mktemp -d)`);
- what you expected `gblog verify` (or the affected command) to report, and what it reported.

Never attach a real ledger, real transcripts or real credentials. A reproduction on fabricated data
is enough.

These count as vulnerabilities:

- any way to edit, delete, reorder or truncate records, or to forge an approval, that `gblog verify`
  reports as `OK` or `PASS` while a witness listed in the threat model below still exists;
- a path that writes outside the ledger root, including through a symlink planted at the path gblog
  is about to write;
- a way to make `sync` push to a public repository without an override flag;
- a way to change an existing backup remote (`origin`) through `gblog` arguments alone, without
  `--replace-remote`;
- a way to make `sync` report `pushed` while a file gblog wrote under `logs/`, `anchors/` or
  `archive/` is missing from the pushed commit or differs in it;
- a single file or record that stops `verify --all` or `anchor` from reporting the other chains;
- a secret shape listed as covered below that is stored unredacted.

The items under "Not guaranteed" and "Redaction limits" are known limits, not vulnerabilities.
The known limits under "Filesystem assumptions" are open findings that are already recorded; they
need no new report.
Improvements to them are welcome as ordinary issues.

## Supported versions

| Version | Supported |
|---|---|
| 0.6.x (latest release) | yes |
| earlier than 0.6 | no — upgrade |

Upgrading never needs a migration; the record format is unchanged. Versions before 0.6 accept a
forged `break_ack` and do not detect several truncation cases, so do not rely on their `verify`
output. Run the newest 0.6.x: 0.6.0 still reports an emptied chain as `OK … 0 records` while its
state file survives, and does not refuse reserved bot names on append. It also pushes to a GitHub
remote written with a port, as `ssh.github.com` or through an ssh host alias after only a warning,
and lets `gblog sync --remote` re-point an existing backup remote. All of these are fixed in 0.6.1 (see
[`CHANGELOG.md`](CHANGELOG.md), which also lists what remains open under "Known open findings").

## Threat model (summary)

The full version is in [`docs/DESIGN.md`](docs/DESIGN.md).

**Adversary.** The Bot itself (careless, prompt-injected or deliberately hiding something), or anyone
else with write access to the ledger directory. They run as the same user as `gblog`. They can edit
or delete any file under the ledger root, call `gblog.Ledger.append()` directly, set environment
variables and change the clock. That includes everything git reads in the ledger repository
(`.git/config`, hooks, ignore files, filters, index flags), symlinks and FIFOs planted in the tree,
and any text in record fields, display names and file names.

Two narrower positions are part of the model as well:

- **A CLI-only adversary.** A prompt-injected Bot whose shell commands need approval, but whose
  `gblog` commands are approved automatically. Whatever `gblog` arguments alone can do, it can do
  without anyone seeing an approval card. gblog refuses the argument-only attacks it knows about
  (see "Private-remote policy"). The Auto-review allow-list in the README (setup step 6) is the
  other half of that boundary: a blanket rule for every `gblog` command gives this adversary
  `sync --remote`, `archive --file <any readable path> --no-redact`, `alias` and `seal`.
- **The author of a tree you audit.** When you clone the backup repository to check it, you run
  gblog as yourself on files the Bot wrote. See "Auditing a clone of the backup".

**Not assumed.** The adversary cannot rewrite what is already outside the computer: the conversation
transcript where the daily routine posts chain heads, and a git remote that rejects force-pushes.

**Guaranteed, as tamper evidence (not prevention).** `gblog verify` reports FAIL for:

- an edited record;
- a deleted, inserted, re-ordered or duplicated line;
- a forged `break_ack`;
- a modified or missing archived file, and an `archive` record whose path is not addressed by its
  own hash (`archive/<bot>/<date>/<sha256>.gz`) or that carries only one of `path` and `sha256`.
  A record that names no file at all (`gblog log --type archive` writes one) is a `!` warning, and
  FAIL with `--strict`: it is a claim, not stored evidence;
- a truncated tail, an emptied chain or a rewritten chain, as long as at least one witness survives:
  `state/<bot>.json`, an anchor, anchor-history or day-rollup entry, or a head you pass to
  `verify --contains`;
- a deleted chain whose state file, anchor, anchor-history file or day-rollup entry
  (`anchors/days/*.json`) survives (`verify --all`, and `verify --bot <name>` for that exact name);
- a `*.jsonl` entry in a chain directory that is not a regular file (a FIFO, a directory, a
  device). It is reported and never opened. The same holds for `anchors/<bot>.history.jsonl`,
  `anchors/<bot>.json` and an archived file, also behind a symlink, for a `state/<bot>.lock`
  that is a FIFO, directory or device, and for a directory at `state/<bot>.json`. A FIFO or
  device at `state/<bot>.json`, and a non-regular day rollup, alert file or alias registry, is
  never opened either; it is treated as unreadable (for the state file that is a `!` warning,
  not a FAIL). `docs/DESIGN.md`, section 7, has the table;
- a chain directory that cannot be listed. It is not taken for an empty chain;
- a directory under `logs/` whose name is not a valid chain key (`verify --all`). Its records are
  never treated as verified and are left out of reports.

Two properties of the check itself are guaranteed too:

- *Every chain is reported.* An unexpected error while one chain is verified or anchored is a FAIL
  for that chain. `verify --all` and `anchor` still verify and anchor the others. A witness
  directory that cannot be listed, or a day rollup that cannot be written, is a FAIL entry of its
  own (`(witness files)`, `(day rollup)`), printed beside the chains. The known limits are listed
  under "Filesystem assumptions".
- *`--contains` is chain-exact.* `verify --bot <chain> --contains <hash>` passes only when the hash
  belongs to a record that is stored in that chain and carries its name. A copy of the chain under
  another name, reached through an alias, does not satisfy it.

`verify` keeps warning about every record that was appended while `state/<bot>.json` was missing or
unreadable (the record carries `state_adopted`). An append can therefore no longer erase the
"state is missing" warning. It stays a warning: gblog cannot tell a lost state file from a hidden
truncation. When `sync` or `capture browser` re-creates state, the marker is the key
`state_adopted` in the state file, and `verify` warns about it in the same way until the next
append moves the marker onto its record. The record-level `!` line is permanent; report each one
once with its cause.

**Not guaranteed.**

- *Completeness.* The ledger is self-reported. gblog cannot prove the Bot logged everything.
- *Authorship.* Records are not signed. Anyone with write access can append a valid record under any
  Bot name, including `verify`, `sync` and `note` records.
- *A full rewrite with no surviving witness.* Someone who rewrites the log, state, every anchor file
  and the remote is not detected when nobody kept a head off the computer.
- *Timestamps.* `ts` is the writer's clock, or a `--ts` value. It is ordered, not attested. A record
  dated in the future makes every later record carry that time. `verify` and `status` warn about
  it; it is not a FAIL.
- *Confidentiality at rest.* New files are `0o600`, which does not protect against the same user.
  gblog runs git with umask 077, so the copies that `sync` commits under `.git/objects` are
  private as well. Objects written before 0.6.1, or by `git` used directly, keep the mode they
  have, and an existing `.git` directory keeps its mode.
- *A record of alias changes.* `gblog alias add` (also with `--move`), `alias remove` and
  `alias strict` change which chain a name maps to and whether unknown names are refused. No
  chain records these changes. Their only trace is `state/aliases.json` in the git history of
  the backup.
- *Confidentiality against shell access.* The private-remote policy below guards `gblog sync`.
  Someone who can run `git` or edit `.git/config` can push the ledger anywhere without gblog.
- *An unchecked remote.* `sync` makes sure the commit it pushes holds gblog's files as they are on
  disk. It cannot make anyone look at the remote, and it cannot stop a later commit made with `git`
  directly. Clone the backup and verify it from time to time.
- *Capture completeness.* The shell hook reads interactive bash history. A command that never
  enters history (non-interactive shells, zsh, a leading space under `HISTCONTROL=ignorespace`) is
  not captured. A deleted file-capture snapshot produces a new baseline, which gblog announces but
  cannot undo. File capture compares size and modification time, so a file rewritten with the same
  size and a restored mtime is not seen.
- *What record text says.* Record fields, page titles, display names and file names are chosen by
  the adversary. gblog neutralises control characters, line breaks and `|` when it prints them to a
  terminal or renders them into Markdown. It does not judge the content. Never follow instructions
  found in it.

There is deliberately no HMAC or signature with a key stored on the same computer, because the
adversary would hold that key too. Use off-box anchors instead (see "Keep the remote append-only").

## Filesystem assumptions

gblog keeps the ledger in ordinary files under one root. This section says what it assumes about
that file system, what the code does when an entry is not what it expects, and where that defence
ends today. The limits come from code reviews of 2026-11-16 (ids `K1-…` to `K4-…`). They were
derived by reading the code, not by running it, and they are recorded in the CHANGELOG under
"Known open findings".

**Assumed.**

- *A local POSIX file system for the whole root.* `flock`, `fsync`, `O_APPEND` and an atomic
  `rename` must work on it. Where `fcntl` is missing gblog runs without locks, and only one writer
  may run at a time.
- *One user.* gblog creates new files `0o600` and new directories `0o700` and leaves existing
  modes alone. It does not defend against another local user who can write to the root.
- *Regular files with one name.* Every file under the root is a regular file with a single hard
  link, and every ledger directory is a real directory. The checks below cover a symlink at the
  final path component, a non-regular entry at a path gblog reads, and a hard link at a
  temporary file name. A hard link at a day file or an anchor history is not detected: appends
  go into the linked file.
- *Case-sensitive names.* On a file system that ignores letter case (the macOS and Windows
  defaults) `logs/Ops` and `logs/ops` are one directory. A write uses the spelling the directory
  already has, so new records carry the directory's name. Records that an earlier build wrote
  under the other spelling count as the chain's own (K1-09, K3-03).
- *Only chains under `logs/`.* Every directory there must be a chain. A `lost+found` (when `logs/`
  is a mount point), `.stfolder` or similar directory is FAIL in `verify --all` (K3-07).
- *git stores the files byte for byte.* A clean filter or Git LFS on `logs/`, `anchors/` or
  `archive/` makes the committed-content check of `sync` fail on every run (exit 1, nothing
  pushed). There is no override.

**What the code does.**

- It does not write through a symlink at the final path component. Day files, anchor histories
  and lock files are opened with `O_NOFOLLOW`, and a symlink at a report or tuning file is refused
  (exit 2). State, anchor, rollup, snapshot, archive, report and tuning files are written to
  `<path>.<pid>.tmp` and renamed over the path, so a hard link or FIFO planted there is replaced
  by a regular file and nothing is written through it.
- The temporary file is created exclusively (`O_EXCL`). A regular leftover of a killed process is
  replaced. Anything else at that name (a symlink, a hard link, a FIFO) is removed and that one
  write fails (exit 2); the next run succeeds.
- It does not open a non-regular entry at a path it reads. Log files and JSON side files are
  tested on the opened descriptor, so an entry swapped in after a check cannot block the reader.
  A `*.jsonl` entry, an anchor history, `anchors/<bot>.json` or an archived file of that kind is
  a `verify` problem, and so is a directory at `state/<bot>.json`. A FIFO or device at
  `state/<bot>.json`, and a day rollup, alert file or alias registry of that kind, counts as
  unreadable. A JSON side file larger than 16 MiB counts as unreadable too.
- Lock files are opened without blocking and must be regular files. A FIFO, directory or device
  at `state/<bot>.lock` is a `verify` problem; `verify` and `anchor` go on without that lock, and
  appends to that chain exit 2. While `anchor` holds `state/.anchors.lock`, a bot lock that is a
  hard link of it is not requested.
- A directory at `state/<bot>.json` cannot be replaced, so an append to that chain is refused
  (spooled, exit 4) before a line is written. A FIFO, directory or device at the target day file
  is refused in the same way. A refusal stays exit 4 when its spool file cannot be written; the
  alert file is written first and the message names what failed.
- A witness directory (`state/`, `anchors/`, `anchors/days/`) that cannot be listed, and a day
  rollup that cannot be written, are FAIL entries (`(witness files)`, `(day rollup)`) printed
  beside the chains. They do not end the run.
- `sync` treats a symlink in place of a day file, an anchor history or an archived file as a
  difference between commit and disk: nothing is pushed (exit 1). `verify` follows such a link
  and prints a `!` line for it (a problem with `--strict`).
- gblog runs git with umask 077 and makes a `.git` directory that it creates `0o700`.
- `capture shell-hook --install` replaces an older hook block by writing a temporary file and
  renaming it over the rc file. A symlinked rc file stays a link.
- It reads an archived file only up to one byte past the `bytes` its record declares.
- A write of a log line that fails part-way is rolled back to the size the file had before.
- A chain directory that cannot be listed is FAIL, and a failure while one chain is anchored is a
  FAIL entry for that chain (`anchor aborted: <class>`).

**Known limits (open; fixes are future work).**

| Entry on disk | What happens today | Id |
|---|---|---|
| a very long log line, or a very large day file | it is read whole into memory: no reader bounds a log line. The file-capture snapshot and `tuning/proposals.json` have no size bound either | K2-06, K4-03 |
| an anchor history, an archived file or a browser history file that is swapped for a FIFO between the type test and the open | the reader blocks: for these files the test and the open are still separate steps. The raw scans of log files (later day files in `report --date`, older `tune` records, the witness note, the spool-replay check) work the same way | K2-06, Z-14 |
| a hard link at a day file or an anchor history | appends go into the linked file, which can lie outside the root | K2-05 |
| a regular file at `spool/<bot>` | a refused append still exits 4 and writes the alert, but no spool file: the event is kept only in `state/<bot>.alert.json`, which the next refusal replaces | K2-10 |
| two overlapping `capture` or `tune` runs for one Bot | the snapshot, the browser markers and `tuning/proposals.json` are read, changed and written without a lock: duplicate `auto` records, lost proposal ids | K2-09, K2-14 |
| a failing `archive` that overlaps another append | the cleanup looks at the tail record only and can remove a file that a chained record names | K2-13 |

## Redaction limits

Redaction is pattern matching applied to the top-level string fields of a record and to archived
text. It is a safety net. The primary control is that Bots are told never to paste credentials.

**Covered shapes:**

- PEM private-key blocks, including a block that was cut off before its `END` line;
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`);
- Slack tokens (`xoxa-`, `xoxb-`, `xoxp-`, `xoxr-`, `xoxs-`, `xapp-`) and Slack webhook URLs;
- AWS access key ids (`AKIA…`);
- `sk-…` and `xai-…` API keys;
- Google keys (`AIza…`) and OAuth tokens (`ya29.…`);
- Stripe keys (`sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`);
- GitLab (`glpat-`) and npm (`npm_`) tokens;
- JWTs;
- `Bearer …` and `Authorization: Basic …`;
- assignments whose name contains `token`, `secret`, `password`, `passwd`, `pwd`, `api_key`,
  `apikey`, `access_key` or `private_key`, for example `password=…`, `DB_PASSWORD=…`,
  `AWS_SECRET_ACCESS_KEY: …`. The keyword may be glued to other letters or digits, or written in
  camelCase: `dbPassword=…`, `accessToken: …`, `secretKey=…`, `PASSWORD2=…`, `APITOKEN=…`;
- JSON keys ending in one of those words (`"password": "…"`), also inside an escaped JSON string,
  and the same keys in a Python `dict` repr (`'password': '…'`);
- CLI flags `--password`, `--passwd`, `--token`, `--secret`, `--api-key` followed by a value, also
  with a prefix (`--db-password …`, `--client-secret …`, `--auth-token=…`);
- the commit message given to `gblog sync -m`;
- URL query parameters such as `?token=`, `access_token=`, `api_key=`, `key=`, `secret=`, `sig=`,
  `signature=`, `auth=`, `code=`;
- `user:pass@` userinfo in URLs.

**Not covered — these are stored as written:**

- a secret with no recognisable name or shape: `mysql -phunter2`, `echo hunter2 | login`,
  "the password is hunter2";
- names without a keyword (`CREDENTIAL=…`, `AUTH=…`, `PASS=…`) and cookies or session ids
  (`Cookie: sessionid=…`);
- JSON values that are not strings (`"pin": 123456`), and short option forms such as
  `curl -u user:pass`;
- unquoted values shorter than 6 characters, and everything after the first space of an unquoted
  multi-word value;
- an unquoted value that contains `&` or a quote: the part after it is kept, and nothing is
  redacted when the part before it is shorter than 6 characters (`DB_PASSWORD=Sup3r&Secret!`);
- vendor keys not listed above;
- encoded, split, base64-wrapped or hex-dumped secrets;
- strings nested inside structured fields (`proposal`, `break`);
- archive content that is not UTF-8 and holds a NUL byte (UTF-16 text, images, PDFs,
  screenshots). It is stored unscanned and the record carries `scanned: false`. Text with stray
  invalid bytes is scanned;
- anything archived with `--no-redact` (the record carries `scanned: false`);
- page titles captured from browser history, which are stored in `detail` and may contain private
  information that is not a "secret" by shape.

If a secret reaches the ledger:

1. Rotate the secret first.
2. Do not edit the log line. That breaks the chain, and the value may already be in git history.
3. Add a `gblog note` describing the exposure.
4. Decide with the operator what to do about the backup repository.

## Private-remote policy

Backups must be private. `gblog sync` commits locally and then applies this rule to **every push URL
of `origin`**, including a separate `pushurl`:

| Remote | Result |
|---|---|
| github.com repository reported **public** | Push refused, exit 3, a `denied` sync record is written. Override: `--allow-public`. |
| github.com repository whose visibility **cannot be confirmed** (rate limit, network error, timeout) | **Fail closed**: push refused, exit 3, `refused: could not confirm <url> is private (<reason>)`. Override: `--allow-unknown-visibility` (or `--allow-public`). |
| github.com repository confirmed private | Push proceeds. An unauthenticated API 404 counts as private or nonexistent. |
| GitHub URL that cannot be mapped to one repository (a gist, an unexpected path, a bracketed or percent-encoded host, a `<transport>::` prefix, a URL that cannot be parsed) | Treated as unconfirmed visibility: **fail closed**, exit 3. Same overrides as above. |
| wiki of a GitHub repository (`…/repo.wiki.git`) | Checked as that repository: a wiki is as public as its repository. |
| no `origin` configured in the ledger repository itself | Nothing is pushed, exit 1. A remote named `origin` that git would find elsewhere (global configuration, `.git/remotes/`, a directory of that name) is never used. |
| any other host | gblog cannot check. It prints a warning on stderr and pushes. Making that remote private is your responsibility. |
| `--remote <url>` while `origin` already points somewhere else | Nothing is changed, exit 2. Override: `--replace-remote`. Every change of `origin` is written to the ledger as a `sync` record with the old and the new URL (credentials removed). |

Notes:

- A remote is a GitHub remote when its **parsed host** is `github.com`, `www.github.com`,
  `ssh.github.com` or any other `*.github.com` name. The port, the letter case and a trailing dot
  are ignored. `https://github.com:443/o/r.git`, `ssh://git@ssh.github.com:443/o/r.git` and
  `https://github.com./o/r` are checked like `git@github.com:o/r.git`. (0.6.0 matched the URL text
  and pushed to these forms after only a warning.)
- Look-alike hosts (`evilgithub.com`, `github.com.evil.example`) and URLs that merely contain
  `github.com` in their path (`https://evil.example/x//github.com/o/r`) are "any other host". They
  are never looked up as, or reported as, a confirmed-private GitHub repository.
- An ssh host alias (a `Host` entry in `~/.ssh/config`, used as `git@gh-backup:owner/repo.git`) is
  resolved with `ssh -G`, a local lookup that opens no connection. An alias whose `HostName` is a
  GitHub host is checked like the literal URL. When `ssh` is missing or the lookup fails, the alias
  is "any other host": gblog warns and pushes. Use an `https://github.com/<owner>/<repo>` URL for
  the backup remote, so that the check does not depend on ssh configuration the adversary can edit.
- The visibility lookup always asks github.com. The `gh` call names the host explicitly, so
  `GH_HOST` in the environment cannot redirect it. An unexpected answer counts as unconfirmed.
- `sync` does not trust the repository's own git settings for what it commits. gblog's files are
  added even when an ignore rule (a nested `.gitignore`, `.git/info/exclude`, `core.excludesFile`)
  would hide them (also a rule that names a whole directory such as `logs/`), git hooks do not
  run, commits and pushes are not signed, and after the commit the log files, the anchor-history
  files and the archived files are compared with the committed content. Every such file is hashed
  on every run: gblog keeps no cache of verified files, because a cache under `.git/` could be
  written by whoever the check is meant to catch. A difference (a clean filter, an index flag, a
  symlink in place of the file) is an error: nothing is
  pushed and the exit code is 1. `<root>/.gitignore` is created when it is missing, so a ledger
  made before 0.6 stops committing lock files and capture snapshots.
- A ledger directory that is a symbolic link cannot be backed up: git stores the link, not the
  files behind it. `sync` says so, pushes nothing and exits 1.
- A `--remote` URL with a user name and password is stored by git in `.git/config` as written.
  gblog warns about it; use a credential helper instead.
- `sync` refuses (exit 2, nothing changed) a git repository at the ledger root that it cannot
  recognise as a ledger, and marks a repository it recognises with `gblog.ledger = true` in
  `.git/config`. If `origin` is also defined in the system or global git configuration (`url` or
  `pushurl`), `sync` commits locally and refuses to push (exit 1).
- `--no-push` never contacts the network.
- The local commit is made even when the push is refused, so anchors, spool and alert files are
  still captured (except when `--replace-remote` could not be recorded because of a tail mismatch
  on the sync Bot's chain: then nothing is changed, nothing is committed, and the refused event
  says `NOT applied`, exit 4).
- Visibility is checked at push time only. gblog cannot notice a repository being made public later.
- Refusal and error text passes through redaction before it is printed or logged, so userinfo in a
  remote URL is not echoed. Text is redacted before it is shortened.
- `--allow-public`, `--allow-unknown-visibility` and `--replace-remote` are deliberate,
  per-invocation decisions. Never put them in a routine prompt, in a scheduler entry or in an
  Auto-review *Allow automatically* rule.
- `--remote` decides where backups go, so a Bot must not choose it: keep it out of routine
  prompts and Auto-review rules as well. A scheduler entry that the operator wrote and that
  passes the same fixed URL on every run is acceptable (`examples/routine-daily-audit.sh` does
  this with `GBLOG_AUDIT_REMOTE`). Repeating the current `origin`, also in another spelling of
  the same repository, changes nothing; any other URL exits 2 without `--replace-remote`.
- Use a fine-grained token scoped to the one backup repository, entered through the secure secret
  request and never in chat.

## `jay_approval_id` is a reference, not authentication

`gblog seal --jay-approval-id <id>` stores the id on the `break_ack` record and on the witness note
in the `ledger` chain. It is a **recorded reference** to an approval that happened elsewhere. It is
not a signature and not a credential. gblog cannot check who typed it, and anyone with write access
could type any string. Obvious placeholders (`PENDING-JAY`, `tbd`, `todo`, ids shorter than 4
characters, and similar) are rejected, but that is a typo guard, not authentication.

What limits abuse is the pin, not the id. At verification time gblog honours a `break_ack` only for
the single kim-biseo 2026-11-02 discontinuity whose file, lines, sequence numbers and three full
hashes are compiled into `KIM_BISEO_BREAK`. An ack for any other break, on any chain, makes `verify`
FAIL whatever id it carries. To audit the approval itself, compare the stored id with the operator's
own record of it.

The only other descriptor `verify` can honour is the fabricated test fixture, whose three hashes are
also compiled in (`KIM_BISEO_FIXTURE_BREAK`). It is honoured only when the ledger root is under the
system temporary directory, and `verify` then prints
`! fixture break_ack honoured: this root is a temporary test copy` and sets `fixture_ack: true`.
Setting `TMPDIR` cannot make any other ack eligible. On a live ledger that line, or
`"fixture_ack": true` in `verify --json`, is a finding to report.

A `break_ack` reaches a chain only through `seal`. `gblog spool replay` skips a spooled event of
that type, and `seal` checks again under the chain's lock that no ack is present, so two concurrent
runs cannot write two.

## Keep the remote append-only

The remote and the conversation transcript are the only witnesses an on-box adversary cannot
rewrite. To make them count:

- Turn on branch protection for the backup branch: **no force-push, no branch deletion**. Apply it
  to administrators too if the plan allows.
- Give the Bot's token the minimum it needs: contents read/write on that one repository, and no
  administration permission. Otherwise it could switch branch protection off.
- If `gblog sync` reports a rejected push, never "fix" it by rewriting history. A non-fast-forward
  push is itself a signal of a rollback: stop and report.
- Have the daily routine post every chain's full 64-hex head in the conversation after
  `gblog anchor`, and check the previous day's head with
  `gblog verify --bot <slug> --contains <head>`. A miss means the chain was rolled back or
  rewritten.
- Review who has write access to the backup repository as carefully as who has access to the
  computer.

## Auditing a clone of the backup

A clone of the backup repository is the Bot's files on your computer. Treat the tree as untrusted
input.

- Run read-only checks: `gblog --root <clone> verify --all --strict`, and
  `gblog --root <clone> verify --bot <chain> --contains <head>` with heads you kept elsewhere.
  `--strict` also fails on a symlink among a chain's files and on the other layout warnings.
- `verify` creates `state/<bot>.lock` for each chain it reads (an empty file, never through a
  symlink). Mount or `chmod` the clone read-only if nothing at all may be written; `verify` then
  runs without the lock.
- Do not run write commands on the clone: no logging command, no `anchor`, no `verify --record`,
  no `sync`, and `report` or `tune` only with `--stdout`.
- gblog does not follow a symlink at the path it writes. The command exits 2 (day files, anchor
  histories, lock files, reports, tuning files) or replaces the link with a regular file (state,
  anchor, snapshot and archive files). A symlink committed as `reports/<date>.md`,
  `state/<bot>.json` or a day file can therefore not redirect a write to a file of the Bot's
  choosing. A ledger *sub-directory* that is itself a symlink is still followed by the write
  commands (some setups keep `archive/` on another disk; `sync` refuses to push such a tree),
  which is one more reason for the rule above.
- gblog never opens a FIFO or a device at a path it reads (state, anchors, rollups, archives, the
  alias registry), also behind a committed symlink such as `state/x.json -> /dev/zero`.
- Output of `tail`, `status` and `report --stdout` is safe to display: control characters are
  neutralised. What the text says is still chosen by the Bot.
- Run `gblog --root <clone> verify --all --strict --skip-archives` first. gblog reads a log line
  whole, whatever its size (a JSON side file above 16 MiB counts as unreadable), and decompresses
  an archived file up to the `bytes` its
  record declares (to the end when the record declares none). A clone can hold a few megabytes
  that expand to gigabytes, so run the audit under a memory limit (`ulimit -v`) and decide on the
  full archive check afterwards.
