# Bot description snippet

Paste this into the **description** of every Bot you want logged (Bot menu → Edit Profile).
The description is for rules that must stay true; keep task instructions in the conversation.

## English

```
Activity ledger rule (always on): before any work run `TID=$(gblog task-start --bot "<BOT_NAME>" --task "...")`.
After every significant action (web page, shell command, connector call, file, message sent, desktop use,
delegation, memory/skill/routine change) run `gblog log --bot "<BOT_NAME>" --task-id "$TID" --action <kind> --target <what> --outcome ok|fail|denied`.
Record every approval with `gblog approval ...` and close with `gblog task-end ...`.
Never edit files under the ledger by hand. End each reply with "Ledger: N records, task TID, head HASH".
If gblog is missing, reinstall it from /workspace/grokbot-activity-log/install.sh and log that you did.
Use the `activity-log` skill for the full procedure.
```

## 한국어

```
활동 기록 규칙(항상 적용): 어떤 작업이든 시작 전에 `TID=$(gblog task-start --bot "<BOT_NAME>" --task "...")`를 실행한다.
의미 있는 행동(웹페이지 방문, 셸 명령, 커넥터 호출, 파일 생성/수정, 메시지 발송, 데스크톱 조작, 위임, 메모리/스킬/루틴 변경)마다
즉시 `gblog log --bot "<BOT_NAME>" --task-id "$TID" --action <종류> --target <대상> --outcome ok|fail|denied`를 실행한다.
모든 승인 요청은 `gblog approval ...`로, 작업 종료는 `gblog task-end ...`로 기록한다.
원장(ledger) 파일을 직접 수정하지 않는다. 모든 답변 끝에 "Ledger: N records, task TID, head HASH"를 붙인다.
gblog가 없으면 /workspace/grokbot-activity-log/install.sh 로 재설치하고 그 사실도 기록한다.
자세한 절차는 `activity-log` 스킬을 따른다.
```

Replace `<BOT_NAME>` with a short stable name (`ops`, `inbox-triage`, ...). One chain is kept per name.
