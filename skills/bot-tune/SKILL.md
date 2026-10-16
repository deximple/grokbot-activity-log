---
name: bot-tune
description: Weekly improvement loop for all Bots on this account — derive concrete changes to descriptions, skills, routines, Auto-review rules and memory from the gblog ledger, apply them with the user's approval, record them, and measure whether they helped.
---

# Bot tuning from the ledger

Model weights cannot be fine-tuned on this plan, so "tuning" means changing what *can* change:
each Bot's description, skills, routines, Auto-review rules and memory — driven by evidence in
the activity ledger rather than by impression. Every change is recorded and re-measured, so
changes that do not help get reverted.

## When to use

- The weekly tuning routine (default Monday 09:00 local), or when the user asks
  "how can my Bots do better?", "why does X keep failing?", "which of this should be a routine?".

## Required inputs

- Read access to the ledger (`gblog status` works).
- The list of Bots and which skill/routine each one uses (ask once, keep in memory).

## Sequence of work

1. **Refresh evidence**: `gblog verify --all` must be OK. Run `gblog capture browser --bot <bot>`
   and `gblog capture files --bot <bot>` for each Bot that has a computer session, then `gblog coverage`.
2. **Generate proposals**: `gblog tune --days 28` (writes `tuning/<date>.md` and `tuning/proposals.json`).
   Read the whole report. Categories and what they map to:

   | Category | Change it implies |
   |---|---|
   | `skill` | add/fix a step in the skill the failing tasks use; or write a new skill for a recurring sequence |
   | `routine` | create a routine for a task started manually again and again |
   | `approval` | Auto-review *Allow automatically* for always-approved actions; *Always deny* + description rule for always-denied ones |
   | `description` | durable behavioural rule (break down slow tasks, ask early, report partials) |
   | `memory` | a lesson from a `note` that should survive the conversation |
   | `logging` | the Bot under-reports; strengthen its activity-log rule |
   | `revert` | an earlier applied change made the metric worse — undo it |

3. **Draft the exact edit** for each proposal you intend to act on: the new description sentence,
   the skill diff, the routine prompt (owner Bot, schedule, time zone, source, expected result,
   approval boundary, missing-data behaviour), or the Auto-review rule text. Keep each change small and
   attributable to one proposal id.
4. **Ask the user** in one message: list proposals with the drafted edit and the metric that will be
   watched. Do not apply anything to another Bot's description, skills or routines without a yes.
   Proposals the user declines: `gblog tune --reject <id> --detail "<reason>"`.
5. **Apply** approved edits (edit the Bot profile / save the skill / create the routine / add the
   Auto-review rule / write memory), then record each one:
   `gblog tune --apply <id> --detail "<what exactly changed, where>"`.
6. **Report** in the conversation: metrics table, the proposals applied/rejected, and the
   "Effect of applied changes" table from the report (verdicts: too early / improving / not helping).

## Validate

- Every applied change has a `tune` record (`gblog tail --bot <bot>` shows `tune <id> ok`).
- Next week's report shows the change in "Effect of applied changes". If it says **not helping**
  for 7+ days, propose the revert — do not keep stacking fixes on the same problem.
- Never edit `logs/` or `tuning/proposals.json` by hand.

## What requires approval

- Any change to a Bot's description, skills, routines or Auto-review rules.
- Creating or deleting Bots (this skill should normally not do it — prefer improving an existing Bot).
- Sharing a template that includes tuning results.

## Limits

Proposals are heuristics over self-reported data: repeated failures, denials, manual repeats,
3-step action sequences, slow tasks, lessons in notes, coverage gaps. They point at where to look;
you still read the evidence records (`gblog tail`, `gblog report --since`) before drafting a change.
