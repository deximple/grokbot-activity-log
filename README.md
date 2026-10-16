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
| `gblog.py` | repo root | CLI: `task-start`, `log`, `approval`, `note`, `task-end`, `verify`, `report`, `status`, `tail`, `sync`, `export` |
| Skill | `skills/activity-log/SKILL.md` | Tells the Bot exactly when and how to log; save it as a private skill |
| Description rule | `docs/bot-description.md` | Paste into each Bot's description so logging stays on across sessions |
| Daily audit routine | `docs/routine-daily-audit.md` | verify → report → commit/push → post summary, every day |
| Bot template | `docs/bot-template.md` | A shareable "Ledger" Bot for x.ai/bot, and a comparison with existing note-taking Bots |
| Tests + CI | `tests/`, `.github/workflows/ci.yml` | 13 unit tests incl. tamper detection; Python 3.8/3.10/3.12; shellcheck |

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
- **Backups you control.** `gblog sync` commits `logs/`, `reports/`, `state/` into a git repo at
  the ledger root and pushes to the remote you configured. Nothing leaves the computer unless
  you set a remote.
- **Zero dependencies.** Grok Bot's docs warn that manually installed packages are replaceable;
  `gblog` is a single stdlib script and `install.sh` reinstalls it in seconds.

### Record schema (v1)

```json
{"v":1,"seq":2,"ts":"2026-10-16T09:56:14Z","bot":"ops","type":"action","task_id":"1a5248e239",
 "action":"browser","target":"https://mail.google.com/inbox","outcome":"ok","detail":"opened inbox",
 "prev":"851cc7d8…","hash":"3866d105…"}
```

`type` ∈ `task_start | action | approval | task_end | note | sync | verify` ·
`action` ∈ `shell | browser | connector | file | message | computer_use | delegation | memory | routine | other` ·
`outcome` ∈ `ok | fail | denied | pending | skipped | n/a`. Timestamps are UTC.

## Setup on a Grok Bot computer (5 minutes)

1. Tell a Bot:
   > Clone https://github.com/OWNER/grokbot-activity-log into /workspace and run `install.sh`. Then run `gblog status`.
2. Save the skill: paste `skills/activity-log/SKILL.md` and ask the Bot to *"save this as a private skill called activity-log"*.
3. For every Bot you want logged, add the snippet from `docs/bot-description.md` to its description (choose a short `<BOT_NAME>`).
4. (Recommended) Create a **private** repo for backups and tell the Bot:
   > Run `gblog sync --remote <git-url> --no-push` once, then sign in to GitHub in the computer's browser / `gh auth login` when asked.
   Use a fine-grained token scoped to that one repo and enter it through the secure secret request, never in chat.
5. Create the daily audit routine from `docs/routine-daily-audit.md`.
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

## Development

```bash
python -m unittest discover -s tests -v
GBLOG_ROOT=/tmp/x ./bin/gblog init && ./bin/gblog status
```

## License

MIT — see [LICENSE](LICENSE).
