# grokbot-activity-log

**A complete, tamper-evident activity record for [Grok Bot](https://docs.x.ai/grok-bot/overview) — on plans that have no Action Recording.**

[한국어 README](README.ko.md) · MIT licensed · Python 3.8+ standard library only · one file

Grok Bot's official logging (Action Recording, OpenTelemetry export, audit logs) is
[Enterprise-only](https://docs.x.ai/grok-bot/security). On SuperGrok / Cursor individual
plans the only record of what a Bot did is the in-app transcript, which cannot be exported.
This project gives each Bot a ledger it must write to as it works, makes that ledger hard to
tamper with, and pushes it to a repo you own every day.

```
$ gblog task-start --bot ops --task "Reconcile invoices"
1a5248e239
$ gblog log --bot ops --task-id 1a5248e239 --action browser --target https://mail.google.com/inbox --detail "opened inbox"
3866d105a1d7 #2 action
$ gblog approval --bot ops --task-id 1a5248e239 --target "send email to vendor" --decision denied
7853cc548280 #4 approval denied
$ gblog task-end --bot ops --task-id 1a5248e239 --outcome ok
$ gblog verify --all
OK    ops                       5 records  head 3c4241c25eb5
```

## What you get

| Piece | Where | Purpose |
|---|---|---|
| `gblog.py` | repo root | CLI: `task-start`, `log`, `approval`, `note`, `task-end`, `verify`, `report`, `status`, `tail`, `sync`, `export`, **`capture`, `coverage`, `tune`, `archive`, `alias`** |
| Skill: activity-log | `skills/activity-log/SKILL.md` | Tells the Bot exactly when and how to log; save it as a private skill |
| Skill: bot-tune | `skills/bot-tune/SKILL.md` | Weekly loop that turns the ledger into approved edits to Bot descriptions, skills, routines, Auto-review rules and memory — and measures the effect |
| Description rule | `docs/bot-description.md` | Paste into each Bot's description so logging stays on across sessions |
| Daily audit routine | `docs/routine-daily-audit.md` | verify → report → commit/push → post summary, every day |
| Weekly tune routine | `docs/routine-weekly-tune.md` | capture → coverage → tune → ask → apply → record, every week |
| Marketplace kit | `docs/marketplace.md` | Ready-to-paste listing, operating instructions, skills/routines/memories for a shareable "Ledger" Bot |
| Bot template | `docs/bot-template.md` | A shareable "Ledger" Bot for x.ai/bot, and a comparison with existing note-taking Bots |
| Tests + CI | `tests/`, `.github/workflows/ci.yml` | 20 unit tests incl. tamper detection, coverage matching and the tune loop; Python 3.8/3.10/3.12; shellcheck |

## How it works

- **One chain per Bot.** Records go to `logs/<bot>/YYYY-MM-DD.jsonl`. Each record has `seq`,
  `prev` (hash of the previous record) and `hash` (SHA-256 of its canonical JSON).
  `gblog verify` walks the chain and reports the first edited, deleted, duplicated or
  truncated line. The chain head is also kept in `state/<bot>.json`, so silently deleting
  the tail of a log is detected too.
- **Parallel Bots are safe.** All Bots share one computer; `gblog` takes an `flock` per Bot
  so concurrent writes never interleave.
- **Secrets are scrubbed before they touch disk**: PEM blocks, GitHub/Slack/AWS/OpenAI/xAI/Google
  key shapes, JWTs, `Bearer …`, `password=…`, `?token=…` query parameters and `user:pass@` URLs.
  (Pattern matching cannot catch everything — Bots are also told never to paste credentials.)
- **Human-readable output.** `gblog report` renders a Markdown day report grouped by Bot and
  task with FAIL/DENIED markers; `gblog status` shows chain heads, open tasks and sync state.
- **Backups you control — and private by policy.** `gblog sync` commits `logs/`, `reports/`, `state/`, `tuning/`, `archive/`
  into a git repo at the ledger root and pushes to the remote you configured. Nothing leaves the computer unless
  you set a remote, and `sync` **refuses to push to a public GitHub repository** (checked via `gh` or the public API;
  override with `--allow-public`, unknown hosts only warn). Each sync is itself logged (under bot `system` unless `--bot` is given).
- **Zero dependencies.** Grok Bot's docs warn that manually installed packages are replaceable;
  `gblog` is a single stdlib script and `install.sh` reinstalls it in seconds.

### Evidence the Bot cannot forget: `capture` and `coverage`

Self-reporting is only as good as the Bot's discipline, so v0.2 also collects evidence *without* it:

```bash
gblog capture shell-hook --bot ops --install   # bash PROMPT_COMMAND hook: every command -> ledger, source=auto
gblog capture browser --bot ops                 # new visits from the computer's Chrome History (host/path only)
gblog capture files --bot ops --path /workspace # created/modified/deleted files since the last snapshot
gblog coverage                                  # % of observed actions the Bot also reported, per Bot and kind
```

Auto records carry `source: "auto"` and are matched to self-reports by Bot, kind, normalised target
(host / command name / file name) and a ±30 min window. Low coverage for a kind is a concrete, measurable
logging gap — and the input for tuning.

### Tuning the Bots from their own ledger: `tune`

Model weights can't be fine-tuned on an individual plan; everything else about a Bot can. `gblog tune`
reads the last 28 days and writes `tuning/<date>.md` with evidence-backed proposals:

| Signal in the ledger | Proposal |
|---|---|
| same action/target failing ≥ 2× | `skill` — add a step or fallback |
| same approval denied ≥ 2× / approved ≥ 3× with no denial | `approval` — Always-deny + description rule / Allow-automatically rule |
| same task started manually on ≥ 3 days | `routine` — with the typical start hour |
| same 3-step action sequence in ≥ 3 tasks | `skill` — write it down as a skill |
| tasks > 2× median duration | `description` — checkpoints, ask early |
| notes containing lessons ("always/never/다음부터…") | `memory` |
| coverage < 80% for a kind, tasks never closed | `logging` |
| an applied change whose metric got worse after 7 days | `revert` |

Each proposal has an id, evidence record refs, and a metric with its baseline. The Bot drafts the exact edit,
the user approves, and the change is recorded: `gblog tune --apply <id> --detail "..."` (or `--reject`).
Next week's report compares the metric before/after each applied change (`too early` / `improving` /
`not helping`), closing the loop. The `bot-tune` skill and weekly routine run this end to end.

### Evidence files, not just claims: `archive`

A ledger that only stores *claims* ("I read the transcript") is weak. `gblog archive` stores the thing
itself — a conversation transcript, a message body, a produced file — content-addressed under
`archive/<bot>/<date>/<sha256>.gz`, and chains its hash:

```bash
gblog archive --bot ledger --kind transcript --file transcripts/kim-biseo/2026-10-16.jsonl --task-id "$TID"
cat body.txt | gblog archive --bot ops --kind message --stdin --name "reply to vendor"
```

Text is redacted *before* hashing (so the ledger never points at a file that still holds a secret; add
`--no-redact` to store verbatim), identical content is stored once, and `gblog verify` now re-hashes every
archived file: a modified or missing archive fails verification exactly like an edited log line.
Grok Bot on an individual plan can read the other Bots' conversations on the same account, so the daily
routine can archive each Bot's transcript and make the ledger a real record, not a diary.

### One Bot, one name: `alias`

Bots drift into name variants (`kim`, `kim-primary`, `KimBisunim`, …) and Korean display names are not valid
chain keys. `alias` fixes both without rewriting any existing chain:

```bash
gblog alias add --slug kim-biseo --display 김비서님 --alias kim --alias kim-primary --alias KimBisunim
gblog alias list            # slugs, display names, variants, chains on disk, unregistered chains
gblog alias strict on       # refuse unknown names instead of warning
```

- The canonical key is a single ASCII slug; `report`, `status`, `tail`, `coverage` and `tune` merge all
  variant chains under it and print `김비서님(kim-biseo)`.
- New records written under any variant or the display name go to the slug's chain; old chains stay
  byte-for-byte as they were (and still verify).
- Unknown names log normally but warn on stderr (or are refused with `strict on`).
- The report flags an open task whose twin (same title) was closed in another chain of the same Bot:
  `outcome **open** — likely closed as `a2` in chain `kim-primary` (ok)`.

### Record schema (v1)

```json
{"v":1,"seq":2,"ts":"2026-10-16T09:56:14Z","bot":"ops","type":"action","task_id":"1a5248e239",
 "action":"browser","target":"https://mail.google.com/inbox","outcome":"ok","detail":"opened inbox",
 "prev":"851cc7d8…","hash":"3866d105…"}
```

`type` ∈ `task_start | action | approval | task_end | note | sync | verify | tune | archive` · optional `source` ∈ `self | auto | routine` ·
`action` ∈ `shell | browser | connector | file | message | computer_use | delegation | memory | routine | other` ·
`outcome` ∈ `ok | fail | denied | pending | skipped | n/a`. Timestamps are UTC.

## Setup on a Grok Bot computer (5 minutes)

1. Tell a Bot:
   > Clone https://github.com/deximple/grokbot-activity-log into /workspace and run `install.sh`. Then run `gblog status`.
2. Save the skill: paste `skills/activity-log/SKILL.md` and ask the Bot to *"save this as a private skill called activity-log"*.
3. For every Bot you want logged, add the snippet from `docs/bot-description.md` to its description (choose a short `<BOT_NAME>`).
4. (Recommended) Create a **private** repo for backups and tell the Bot:
   > Run `gblog sync --remote <git-url> --no-push` once, then sign in to GitHub in the computer's browser / `gh auth login` when asked.
   Use a fine-grained token scoped to that one repo and enter it through the secure secret request, never in chat.
5. Create the daily audit routine from `docs/routine-daily-audit.md` and the weekly tune routine from `docs/routine-weekly-tune.md`; save `skills/bot-tune/SKILL.md` as a skill too.
6. Add an Auto-review *Allow automatically* rule for `gblog` commands in `/workspace/activity-log` so routine runs don't stall on approvals.

Run it anywhere else (laptop, CI, another agent) with `GBLOG_ROOT=/path ./bin/gblog …`.

## Limitations — read this

- **The ledger is self-reported.** `gblog` proves the log was not altered after it was written;
  it cannot prove the Bot logged everything. Mitigations built in: a mandatory `task-start`/`task-end`
  pair per task, a ledger footer in every reply, the weekly transcript cross-check in
  `docs/routine-daily-audit.md`, and the rule to add a `note` (never a back-dated action) when a gap is found.
- Auto Review does not review memory writes; routine approvals expire after ~10 minutes, so
  pre-approve `gblog sync` or it only commits locally.
- If you are on **Enterprise**, turn on Action Recording + OpenTelemetry Export + conversation
  content export instead — that is the ground truth. Keep `gblog` for the *why* (notes, decisions).

## Upgrading on a Grok Bot computer

```bash
git -C /workspace/grokbot-activity-log pull && bash /workspace/grokbot-activity-log/install.sh && gblog --version
gblog verify --all                  # chains are format-compatible; nothing to migrate
gblog alias add --slug <slug> --display <name> --alias <old-variant> ...   # once per Bot that has name variants
```

## Development

```bash
python -m unittest discover -s tests -v
GBLOG_ROOT=/tmp/x ./bin/gblog init && ./bin/gblog status
```

## License

MIT — see [LICENSE](LICENSE).
