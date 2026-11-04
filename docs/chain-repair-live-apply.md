# LIVE-APPLY checklist — kim-biseo chain repair

**Do not run this until Jay says to apply it.** Merging the draft PR is not that approval.
`PENDING-JAY` is not an approval id; `gblog seal` rejects it.

This checklist changes the audit meaning of one live chain. It does not quarantine a file and it does not rehash history. Do not hand-edit `logs/` or `state/`. Do not claim the live ledger was repaired until the commands below have been run on it and the verify line matches.

N1 found no seq 1920 anywhere searched, so the live path is this seal, not inserting a recovered line.

## What the code showed (not a proof of the historical crash)

`append` takes the bot lock, writes the new line with `O_APPEND`, fsyncs the file and its directory, then atomically replaces `state/<bot>.json`. A crash inside that path cannot leave state ahead of the file. If the state file exists, older code trusted it and did not compare it to the log tail. State gets ahead when the jsonl is replaced or truncated **outside** the lock; the next append then uses a `prev` that is not in the file and a seq that skips. That matches 1919 → 1921 with orphan prev `268a3867…`. `sync` and browser capture rewrote state outside the lock (that race moves the head backward). They now re-read state under the same lock. The tail check refuses the forward case.

## After Jay says to apply

1. Install this gblog on the ledger host (`install.sh`). Do not copy `tests/fixtures/kim-biseo-break/` onto the live tree. That fixture is fabricated and its hashes are not the live ones.
2. Read-only: confirm there is still no seq 1920 record whose hash equals `268a3867a13c180b76a4a9d16d544e32647cf0695a7d15d7c6c0ee9f74d0df01`. If one appears, stop and insert that line instead of sealing.
3. `gblog verify --bot kim-biseo` — expect FAIL at `logs/kim-biseo/2026-11-02.jsonl:165`, prev `268a3867a13c`, expected `11a6e8d43ca0`.
4. `gblog anchor` — OK chains get anchors resumed from their previous anchor. kim-biseo stays FAIL with the break descriptor and both segment heads. Do not skip the other chains.
5. Jay writes down the real approval id.
6. Once: `gblog seal --bot kim-biseo --jay-approval-id <that id>`.
   The command refuses if the on-disk line, seq, or full hashes differ from `KIM_BISEO_BREAK`. It only appends. It does not rewrite 164/165.
7. `gblog verify --bot kim-biseo` — expect exit 0 and `PASS (1 approved break)`. `gblog verify --all` must not print FAIL. A new break is still FAIL.
8. `gblog anchor` again. kim-biseo is now PASS with `approved_breaks` 1 and the same id. Other chains still resume from their own anchors.
9. Leave the witness note on the `ledger` bot chain. Do not delete `spool/` or `state/*.alert.json` if an append was refused; report those and stop.

## Alert

Tail mismatch: CLI exit **4**, stderr `ALERT tail mismatch`, event JSON in `spool/<bot>/`, latest copy in `state/<bot>.alert.json`. gblog does not send mail or chat. Page on that exit code and file. Do not edit the log to make the next append succeed.
