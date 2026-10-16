---
name: activity-log
description: Record every consequential action in the tamper-evident gblog ledger (task start, each action, approvals, task end), then verify and report. Use on every task.
---

# Activity log (gblog)

You keep a complete, append-only record of what you do. The record lives on this
computer under `$GBLOG_ROOT` (default `/workspace/activity-log`) and is written
with the `gblog` command. It is hash-chained, so a missing or edited line is
detectable later. Logging is **not optional** and must never be skipped to save
time. If `gblog` is missing, run `install.sh` from the repo first (see Recovery).

## When to use

Every task, including routine runs, test runs, and work handed to you by another Bot.
Also use it when the user asks "what did you do?" — answer from the ledger, not from memory.

## Required inputs

- `BOT`: your own Bot name. Use the registered slug (`gblog alias list`); if your name is not there, ask the user
  to run `gblog alias add --slug <ascii-slug> --display <your name> --alias <variants>` once. Never invent a new variant.
- The task title in the user's words.

## Sequence of work

1. **Open the task** before doing anything else:
   ```bash
   TID=$(gblog task-start --bot "$BOT" --task "<task title as given>")
   export GBLOG_TASK="$TID"   # lets the shell hook attach auto-captured commands to this task
   ```
   Keep `$TID` for the rest of the task. If the task spans several turns, record
   the id in your reply so you can pick it up again (`--task-id "$TID"`).

2. **Log each significant action immediately after it happens**, not in a batch at the end:

   | What happened | Command |
   |---|---|
   | Visited or acted on a web page | `gblog log --bot "$BOT" --task-id "$TID" --action browser --target "https://host/path" --detail "what you did there"` |
   | Ran a shell command | `gblog log ... --action shell --target "<the command>" --outcome ok\|fail` |
   | Called a connector / MCP tool | `gblog log ... --action connector --target "<connector>.<tool>" --outcome ok\|fail` |
   | Created, edited, uploaded or downloaded a file | `gblog log ... --action file --target "<path or name>"` |
   | Sent any message (email, Slack, chat, X post) | `gblog log ... --action message --target "<channel/recipient>" --detail "<subject or first line>"` |
   | Used the desktop / clicked through an app | `gblog log ... --action computer_use --target "<app or site>" --detail "<goal>"` |
   | Delegated to a subagent, Cloud Agent or another Bot | `gblog log ... --action delegation --target "<who>"` |
   | Saved or changed memory, a skill, or a routine | `gblog log ... --action memory\|routine --target "<name>"` |
   | Something failed or was blocked | add `--outcome fail` (or `denied`) and the error in `--detail` |

   Rules for `--target`: URLs as `scheme://host/path` only (no query strings);
   shell commands verbatim (gblog scrubs known secret shapes, but never paste
   passwords or one-time codes); recipients as names or roles, not full addresses
   if the user prefers.

3. **Log every approval request** the moment it is decided:
   ```bash
   gblog approval --bot "$BOT" --task-id "$TID" --target "<proposed action>" --decision allowed|denied|pending|expired|auto
   ```

4. **Archive evidence, not just claims.** Any file you produced or read that matters (a transcript you
   exported, a message body you sent, a report you generated) goes in content-addressed storage:
   ```bash
   gblog archive --bot "$BOT" --task-id "$TID" --kind transcript|message|artifact --file <path>
   ```
   Secrets are masked before the file is hashed; never pass `--no-redact` for anything that may contain credentials.

5. **Record decisions and reasoning** that a reader would not be able to infer from the actions:
   ```bash
   gblog note --bot "$BOT" --task-id "$TID" --detail "chose vendor B because A's quote expired"
   ```

6. **Close the task**:
   ```bash
   gblog task-end --bot "$BOT" --task-id "$TID" --outcome ok|fail|skipped --detail "<one-line result>"
   ```

7. **End your reply with a ledger footer** so the user can cross-check the transcript:
   `Ledger: <N> records, task <TID>, head <first 12 chars of last hash>` (take N and the head from `gblog status --json`).

## Validate

- `gblog verify --bot "$BOT"` must print `OK`. If it prints `FAIL`, stop, tell the
  user exactly which line is reported, and do not "fix" the log files by hand.
- `gblog status` shows `open` tasks; an open task older than today means a
  `task-end` was missed — close it with `--outcome skipped --detail "closed late"`.

## What requires approval

- Pushing the ledger to a remote (`gblog sync`) when the user has not pre-approved the remote.
- Any change to files under `logs/` other than through `gblog` (never do this; ask the user instead).
- Deleting or rotating the ledger.

## Recovery

If `gblog` is not on PATH (computer was reset or updated):
```bash
git clone https://github.com/deximple/grokbot-activity-log /workspace/grokbot-activity-log 2>/dev/null || git -C /workspace/grokbot-activity-log pull
bash /workspace/grokbot-activity-log/install.sh
```
Log the recovery itself: `gblog log --bot "$BOT" --action shell --target "install.sh" --detail "reinstalled gblog after reset"`.

## Automatic evidence (do this once per computer)

Your self-report is checked against evidence collected without you. Install it once and never disable it:
```bash
gblog capture shell-hook --bot "$BOT" --install      # every shell command -> ledger (source=auto)
gblog capture browser --bot "$BOT"                    # new Chrome visits -> ledger; run from the daily routine
gblog capture files --bot "$BOT" --path /workspace    # changed files -> ledger; run from the daily routine
gblog coverage                                        # % of observed actions you also reported
```
A coverage below 80% for an action kind means you are forgetting that kind; the `bot-tune` skill turns that into a rule change.

## Limits you must disclose

The ledger is self-reported by you. If you realise you did something without logging it,
add a `note` saying so rather than back-dating an action. Do not describe the ledger to the
user as an audit log; describe it as a complete activity record that is verified for tampering.
