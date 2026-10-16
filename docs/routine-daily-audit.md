# Daily audit routine

Ask the Bot that owns the ledger (or a dedicated `ledger` Bot) to create this routine.
Routine runs are not interactive: approvals from a routine expire after ~10 minutes, so
pushing to the remote must be pre-approved (Auto-review *Allow automatically* rule for
`gblog sync` in `/workspace/activity-log`) or the routine will only commit locally.

## Prompt (English)

```
Every day at 23:50 (my time zone), run the ledger audit:
1. `gblog verify --all --record` — if anything prints FAIL, report the exact lines and stop; do not modify log files.
2. `gblog report` for today and `gblog status`.
3. `gblog sync -m "daily ledger sync"` to commit and push the ledger to the configured remote.
   If the push fails, say so explicitly with the error; never retry by changing history.
4. Post in this conversation: the metric table from the report, the list of tasks still open,
   any FAIL/DENIED/fail outcomes, and the sync result (commit id, pushed or not).
Do not contact anyone, do not change routines, do not delete anything.
If the ledger directory is missing, report "ledger missing" instead of creating an empty one.
```

## 프롬프트 (한국어)

```
매일 23:50(내 시간대)에 원장 점검을 실행해:
1. `gblog verify --all --record` — FAIL이 하나라도 있으면 해당 줄을 그대로 보고하고 중단해. 로그 파일을 수정하지 마.
2. 오늘자 `gblog report` 와 `gblog status` 실행.
3. `gblog sync -m "daily ledger sync"` 로 원장을 커밋·푸시해. 푸시가 실패하면 오류를 그대로 알려주고, 히스토리를 바꿔서 재시도하지 마.
4. 이 대화에 보고: 리포트의 지표 표, 아직 열려 있는 작업 목록, FAIL/DENIED/fail 결과, 동기화 결과(커밋 id, 푸시 여부).
누구에게도 연락하지 말고, 루틴을 바꾸지 말고, 아무것도 삭제하지 마.
원장 디렉터리가 없으면 빈 원장을 만들지 말고 "ledger missing"이라고 보고해.
```

## Weekly cross-check (manual, 5 minutes)

Self-reported logs can miss things. Once a week:

1. Open a Bot's conversation and pick two tasks from the last 7 days.
2. Compare the transcript (tool activity, computer use, files, approval cards) with
   `gblog report --since <date> --bot <bot>`.
3. Anything in the transcript that is missing from the ledger is a logging gap:
   add the missing rule to the Bot description or skill, and ask the Bot to add a
   `gblog note` describing the gap (never a back-dated action).
