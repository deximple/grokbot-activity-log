# Daily audit routine

Ask the Bot that owns the ledger (or a dedicated `ledger` Bot) to create this routine.
Routine runs are not interactive: approvals from a routine expire after ~10 minutes, so
pushing to the remote must be pre-approved (Auto-review *Allow automatically* rule for
`gblog sync` in `/workspace/activity-log`) or the routine will only commit locally.

## Prompt (English)

```
Every day at 23:50 (my time zone), run the ledger audit:
1. `gblog verify --all --record`, then `gblog anchor` even if verify exited 1.
   Anchor is per chain: OK chains resume from their own previous anchor; a FAIL chain is stored with its break descriptor and both segment heads. Do not replace this with a whole-ledger skip.
   `PASS (N approved break(s))` exits 0 and means a discontinuity that was already sealed; report the count. It is not a clean chain.
   If any chain prints FAIL, or `state/*.alert.json` exists, report the exact lines. The refused event is under `spool/`. Do not describe the ledger as verified. Do not modify log files. Do not run `gblog seal`. Do not quarantine a day file or rehash records.
   Continue to the sync step below so anchors, spool, and alert files are backed up. The summary must name every FAIL chain.
2. For every Bot on this account, export today's conversation transcript (tool calls included) to `transcripts/<slug>/<date>.jsonl`
   and run `gblog archive --bot ledger --kind transcript --file <that file> --detail "<slug> transcript"`. Then `gblog report` for today and `gblog status`.
3. `gblog sync -m "daily ledger sync"` to commit and push the ledger to the configured remote.
   If the push fails, say so explicitly with the error; never retry by changing history.
4. Post in this conversation: the metric table from the report, the list of tasks still open,
   any FAIL/DENIED/fail outcomes, and the sync result (commit id, pushed or not).
Do not contact anyone, do not change routines, do not delete anything.
If the ledger directory is missing, report "ledger missing" instead of creating an empty one.
```

## Weekly cross-check (manual, 5 minutes)

Self-reported logs can miss things. Once a week:

1. Open a Bot's conversation and pick two tasks from the last 7 days.
2. Compare the transcript (tool activity, computer use, files, approval cards) with
   `gblog report --since <date> --bot <bot>`.
3. Anything in the transcript that is missing from the ledger is a logging gap:
   add the missing rule to the Bot description or skill, and ask the Bot to add a
   `gblog note` describing the gap (never a back-dated action).
