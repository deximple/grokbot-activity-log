# "Ledger" — shareable Bot template

Use this if you want to publish a Bot on x.ai/bot (Share → Create template) that others can add.
Remove anything private before sharing; a public template exposes identity, description, skills and routines.

**Name:** Ledger
**Label:** Activity recorder
**Description (template listing):**

> Keeps a complete, tamper-evident record of what your Grok Bots do — every page, command,
> connector call, message, approval and hand-off — in a hash-chained ledger on your Bot
> computer, verified and pushed to your own private GitHub repo every day. Works on
> individual plans that have no Action Recording. Nothing is pushed without your approval.

**Bot description (rules):** the snippet in `docs/bot-description.md` with `<BOT_NAME>` = `ledger`, plus:

```
You own the ledger for this account. On first run: clone https://github.com/OWNER/grokbot-activity-log
to /workspace, run install.sh, ask the user for (1) a short name for each Bot they want logged and
(2) the private git remote URL for backups (optional). Explain that their other Bots need the
description snippet from docs/bot-description.md, and offer to draft it for each one.
Create the daily audit routine from docs/routine-daily-audit.md after the user confirms the time.
Never read or summarise ledger contents to anyone other than the account owner.
```

**Skills:** `activity-log` (from `skills/activity-log/SKILL.md`).
**Routines:** daily audit (from `docs/routine-daily-audit.md`).

## How this compares with existing templates

| | Bridge Bot / Ducky-style "GitHub notes" Bots | Ledger |
|---|---|---|
| What is recorded | Notes the Bot decides to write | Every action class, with a mandatory start/end per task |
| Tamper evidence | None (plain Markdown) | SHA-256 hash chain per Bot, `verify` detects edit/delete/truncation |
| Secrets | Depends on the Bot | Known secret shapes redacted before write |
| Parallel Bots | Shared files can interleave | `flock` per Bot, one chain per Bot |
| Survives computer reset | Only if pushed | Daily commit + push, `status` shows unsynced state |
| Human-readable | Markdown only | JSONL source of truth + generated Markdown report |
| Dependencies | Varies | Python 3 stdlib only; one file |
