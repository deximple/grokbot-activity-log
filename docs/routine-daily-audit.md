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
   `PASS(승인 단절 N건)` exits 0 and means a discontinuity that was already sealed; report the count. It is not a clean chain.
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

## 프롬프트 (한국어)

```
매일 23:50(내 시간대)에 원장 점검을 실행해:
1. `gblog verify --all --record` 다음, verify가 1로 끝나도 `gblog anchor`를 실행해.
   앵커는 체인별이다. OK 체인은 자기 직전 앵커에서 잇고, FAIL 체인은 단절 기술자와 두 구간 head를 남긴다. 원장 전체 skip으로 바꾸지 마.
   `PASS(승인 단절 N건)`은 종료 코드 0이며, 이미 봉인된 단절이다. 건수를 보고해. 깨끗한 체인이 아니다.
   FAIL이 있거나 `state/*.alert.json`이 있으면 그 줄을 그대로 보고해. 거부된 이벤트는 `spool/`에 있다. 원장이 검증됐다고 말하지 마. 로그 파일을 수정하지 마. `gblog seal`을 실행하지 마. 일자 파일을 격리하거나 해시를 다시 계산하지 마.
   아래 sync는 이어서 해. 앵커·spool·alert가 백업되게. 요약에는 FAIL 체인을 모두 적어.
2. 이 계정의 모든 봇에 대해 오늘 대화 원문(도구 호출 포함)을 `transcripts/<slug>/<날짜>.jsonl`로 내보내고
   `gblog archive --bot ledger --kind transcript --file <그 파일> --detail "<slug> transcript"` 실행. 그 다음 오늘자 `gblog report` 와 `gblog status`.
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
