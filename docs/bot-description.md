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

Replace `<BOT_NAME>` with a short stable name (`ops`, `inbox-triage`, ...). One chain is kept per name.
