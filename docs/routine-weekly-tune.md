# Weekly tuning routine

Owner: the Bot that owns the ledger (`ledger`), or the Bot you trust to propose changes to the others.
Prerequisite: the Bot has the `bot-tune` and `activity-log` skills.

## Prompt (English)

```
Every Monday at 09:00 (my time zone), run the bot-tune skill:
1. `gblog verify --all` — stop and report if the exit code is non-zero (FAIL). `PASS (N approved break(s))` exits 0; report the count, do not seal, and do not edit logs.
2. `gblog capture browser` and `gblog capture files --path /workspace` for every Bot, then `gblog coverage`.
3. `gblog tune --days 28`. Read the report.
4. For each proposal, draft the exact edit (description sentence, skill step, routine prompt, Auto-review rule, memory entry).
5. Post one message here: metrics table, coverage, the drafted edits with their proposal ids and the metric to watch,
   and the "Effect of applied changes" table. Ask me which to apply. Do NOT change any Bot before I answer.
6. After my answer: apply the approved ones, run `gblog tune --apply <id> --detail "..."` for each, `gblog tune --reject <id> --detail "..."`
   for the rest, then `gblog sync -m "weekly tune"`.
If there are no proposals, say so in one line.
```

## The loop

```
ledger (facts) ──gblog tune──▶ proposals ──user approves──▶ edit description/skill/routine/rule/memory
     ▲                                                                   │
     └────────────── gblog tune --apply <id> (recorded, metric baseline) ◀┘
                     next week: "Effect of applied changes" → keep / revert
```
