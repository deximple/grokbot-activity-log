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
- a path that writes outside the ledger root;
- a way to make `sync` push to a public repository without an override flag;
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
state file survives, and does not refuse reserved bot names on append. Both are fixed in 0.6.1 (see
[`CHANGELOG.md`](CHANGELOG.md)).

## Threat model (summary)

The full version is in [`docs/DESIGN.md`](docs/DESIGN.md).

**Adversary.** The Bot itself (careless, prompt-injected or deliberately hiding something), or anyone
else with write access to the ledger directory. They run as the same user as `gblog`. They can edit
or delete any file under the ledger root, call `gblog.Ledger.append()` directly, set environment
variables and change the clock.

**Not assumed.** The adversary cannot rewrite what is already outside the computer: the conversation
transcript where the daily routine posts chain heads, and a git remote that rejects force-pushes.

**Guaranteed, as tamper evidence (not prevention).** `gblog verify` reports FAIL for:

- an edited record;
- a deleted, inserted, re-ordered or duplicated line;
- a forged `break_ack`;
- a modified or missing archived file;
- a truncated tail, an emptied chain or a rewritten chain, as long as at least one witness survives:
  `state/<bot>.json`, an anchor or anchor-history entry, or a head you pass to
  `verify --contains`;
- a deleted chain whose state file, anchor or anchor-history file survives (`verify --all`);
- a directory under `logs/` whose name is not a valid chain key (`verify --all`). Its records are
  never treated as verified and are left out of reports.

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

There is deliberately no HMAC or signature with a key stored on the same computer, because the
adversary would hold that key too. Use off-box anchors instead (see "Keep the remote append-only").

## Redaction limits

Redaction is pattern matching applied to the top-level string fields of a record and to archived
text. It is a safety net. The primary control is that Bots are told never to paste credentials.

**Covered shapes:**

- PEM private-key blocks;
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
  `AWS_SECRET_ACCESS_KEY: …`;
- JSON keys ending in one of those words (`"password": "…"`), also inside an escaped JSON string;
- CLI flags `--password`, `--passwd`, `--token`, `--secret`, `--api-key` followed by a value;
- URL query parameters such as `?token=`, `access_token=`, `api_key=`, `key=`, `secret=`, `sig=`,
  `signature=`, `auth=`, `code=`;
- `user:pass@` userinfo in URLs.

**Not covered — these are stored as written:**

- a secret with no recognisable name or shape: `mysql -phunter2`, `echo hunter2 | login`,
  "the password is hunter2";
- names without a keyword (`CREDENTIAL=…`, `AUTH=…`, `PASS=…`) and cookies or session ids
  (`Cookie: sessionid=…`);
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
| any other host | gblog cannot check. It prints a warning on stderr and pushes. Making that remote private is your responsibility. |

Notes:

- The GitHub host match is strict (`github.com` or `www.github.com`). Look-alike hosts are treated
  as "any other host", never as a confirmed-private GitHub repository.
- The check reads the host name as it is written in the URL. An ssh host alias that points at
  github.com (a `Host` entry in `~/.ssh/config`, used as `git@gh-backup:owner/repo.git`) is treated
  as another host and is not checked: gblog warns and pushes. Use the literal `github.com` host in
  the backup remote so that the visibility check applies.
- `--no-push` never contacts the network.
- The local commit is made even when the push is refused, so anchors, spool and alert files are
  still captured.
- Visibility is checked at push time only. gblog cannot notice a repository being made public later.
- Refusal and error text passes through redaction before it is printed or logged, so userinfo in a
  remote URL is not echoed.
- Both override flags are deliberate, per-invocation decisions. Do not put them in a routine prompt
  or in an Auto-review *Allow automatically* rule.
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
