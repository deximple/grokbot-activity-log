# Weekly tuning routine

Owner: the Bot that owns the ledger (`ledger`), or the Bot you trust to propose changes to the others.
Prerequisite: the Bot has the `bot-tune` and `activity-log` skills.

## Prompt (English)

```
Every Monday at 09:00 (my time zone), run the bot-tune skill:
1. `gblog verify --all` — stop and report if anything is FAIL.
2. `gblog capture browser` and `gblog capture files --path /workspace` for every Bot, then `gblog coverage`.
3. `gblog tune --days 28`. Read the report.
4. For each proposal, draft the exact edit (description sentence, skill step, routine prompt, Auto-review rule, memory entry).
5. Post one message here: metrics table, coverage, the drafted edits with their proposal ids and the metric to watch,
   and the "Effect of applied changes" table. Ask me which to apply. Do NOT change any Bot before I answer.
6. After my answer: apply the approved ones, run `gblog tune --apply <id> --detail "..."` for each, `gblog tune --reject <id> --detail "..."`
   for the rest, then `gblog sync -m "weekly tune"`.
If there are no proposals, say so in one line.
```

## 프롬프트 (한국어)

```
매주 월요일 09:00(내 시간대)에 bot-tune 스킬을 실행해:
1. `gblog verify --all` — FAIL이 있으면 중단하고 보고.
2. 모든 봇에 대해 `gblog capture browser`, `gblog capture files --path /workspace` 실행 후 `gblog coverage`.
3. `gblog tune --days 28` 실행 후 리포트를 읽어.
4. 제안마다 정확한 수정안을 작성해 (description 문장, 스킬 단계, 루틴 프롬프트, Auto-review 규칙, 메모리 항목).
5. 여기에 메시지 하나로 보고: 지표 표, 커버리지, 제안 id별 수정안과 관찰할 지표, "Effect of applied changes" 표.
   어떤 걸 적용할지 나에게 물어. 내 답 전에는 어떤 봇도 바꾸지 마.
6. 내 답 후: 승인된 것을 적용하고 각각 `gblog tune --apply <id> --detail "..."`, 나머지는 `gblog tune --reject <id> --detail "..."`,
   마지막에 `gblog sync -m "weekly tune"`.
제안이 없으면 한 줄로 그렇게 말해.
```

## The loop

```
ledger (facts) ──gblog tune──▶ proposals ──user approves──▶ edit description/skill/routine/rule/memory
     ▲                                                                   │
     └────────────── gblog tune --apply <id> (recorded, metric baseline) ◀┘
                     next week: "Effect of applied changes" → keep / revert
```
