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
[`CHANGELOG.md`](CHANGELOG.md)). 0.6.1 is not released yet: a checkout that contains these fixes
still prints `gblog 0.6.0`, and the CHANGELOG lists what is still open under "Known open findings".

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
  `state/<bot>.json`, an anchor or anchor-history entry, or a head you pass to
  `verify --contains`;
- a deleted chain whose state file, anchor, anchor-history file or day-rollup entry
  (`anchors/days/*.json`) survives (`verify --all`, and `verify --bot <name>` for that exact name);
- a `*.jsonl` entry in a chain directory that is not a regular file (a FIFO, a directory, a
  device). It is reported and never opened;
- a directory under `logs/` whose name is not a valid chain key (`verify --all`). Its records are
  never treated as verified and are left out of reports.

Two properties of the check itself are guaranteed too:

- *Every chain is reported.* An unexpected error while one chain is verified or anchored is a FAIL
  for that chain. `verify --all` and `anchor` still verify and anchor the others.
- *`--contains` is chain-exact.* `verify --bot <chain> --contains <hash>` passes only when the hash
  belongs to a record that is stored in that chain and carries its name. A copy of the chain under
  another name, reached through an alias, does not satisfy it.

`verify` keeps warning about every record that was appended while `state/<bot>.json` was missing or
unreadable (the record carries `state_adopted`). An append can therefore no longer erase the
"state is missing" warning. It stays a warning: gblog cannot tell a lost state file from a hidden
truncation. When `sync` or `capture browser` re-creates state, the marker is the key
`state_adopted` in the state file, and `verify` warns about it in the same way. These `!` lines
are permanent; report each one once with its cause.

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
- *Confidentiality against shell access.* The private-remote policy below guards `gblog sync`.
  Someone who can run `git` or edit `.git/config` can push the ledger anywhere without gblog.
- *An unchecked remote.* `sync` makes sure the commit it pushes holds gblog's files as they are on
  disk. It cannot make anyone look at the remote, and it cannot stop a later commit made with `git`
  directly. Clone the backup and verify it from time to time.
- *Capture completeness.* The shell hook reads interactive bash history. A command that never
  enters history (non-interactive shells, zsh, a leading space under `HISTCONTROL=ignorespace`) is
  not captured. A deleted file-capture snapshot produces a new baseline, which gblog announces but
  cannot undo.
- *What record text says.* Record fields, page titles, display names and file names are chosen by
  the adversary. gblog neutralises control characters, line breaks and `|` when it prints them to a
  terminal or renders them into Markdown. It does not judge the content. Never follow instructions
  found in it.

There is deliberately no HMAC or signature with a key stored on the same computer, because the
adversary would hold that key too. Use off-box anchors instead (see "Keep the remote append-only").

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
- vendor keys not listed above;
- encoded, split, base64-wrapped or hex-dumped secrets;
- strings nested inside structured fields (`proposal`, `break`);
- binary or non-UTF-8 archive content (UTF-16 text, images, PDFs, screenshots). It is stored
  unscanned and the record carries `scanned: false`;
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
| GitHub URL that cannot be mapped to one repository (a gist, an unexpected path) | Treated as unconfirmed visibility: **fail closed**, exit 3. Same overrides as above. |
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
  is "any other host": gblog warns and pushes. Use the literal `github.com` host in the backup
  remote, so that the check does not depend on ssh configuration the adversary can edit.
- The visibility lookup always asks github.com. The `gh` call names the host explicitly, so
  `GH_HOST` in the environment cannot redirect it. An unexpected answer counts as unconfirmed.
- `sync` does not trust the repository's own git settings for what it commits. gblog's files are
  added even when an ignore rule (a nested `.gitignore`, `.git/info/exclude`, `core.excludesFile`)
  would hide them, git hooks do not run, and after the commit the files under `logs/`, `anchors/`
  and `archive/` are compared with the committed content. A difference (a clean filter, an index
  flag) is an error: nothing is pushed and the exit code is 1. `<root>/.gitignore` is created when
  it is missing, so a ledger made before 0.6 stops committing lock files and capture snapshots.
- A `--remote` URL with a user name and password is stored by git in `.git/config` as written.
  gblog warns about it; use a credential helper instead.
- `--no-push` never contacts the network.
- The local commit is made even when the push is refused, so anchors, spool and alert files are
  still captured.
- Visibility is checked at push time only. gblog cannot notice a repository being made public later.
- Refusal and error text passes through redaction before it is printed or logged, so userinfo in a
  remote URL is not echoed. Text is redacted before it is shortened.
- `--allow-public`, `--allow-unknown-visibility`, `--replace-remote` and `--remote` itself are
  deliberate, per-invocation decisions. Do not put them in a routine prompt or in an Auto-review
  *Allow automatically* rule.
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
- Do not run write commands on the clone: no logging command, no `anchor`, no `verify --record`,
  no `sync`, and `report` or `tune` only with `--stdout`.
- gblog does not follow a symlink at the path it writes; the command exits 2 instead. A symlink
  committed as `reports/<date>.md`, `state/<bot>.json.tmp` or a day file can therefore not redirect
  a write to a file of the Bot's choosing. A ledger *sub-directory* that is itself a symlink is
  still followed (some setups keep `archive/` on another disk), which is one more reason for the
  rule above.
- Output of `tail`, `status` and `report --stdout` is safe to display: control characters are
  neutralised. What the text says is still chosen by the Bot.
