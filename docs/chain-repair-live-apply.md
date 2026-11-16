# LIVE-APPLY checklist — kim-biseo chain repair

**Do not run this until Jay says to apply it.** Merging the draft PR is not that approval.
`PENDING-JAY` is not an approval id; `gblog seal` rejects it. It also rejects `pending`, `tbd`, `todo`,
`placeholder` and `<id>` in any letter case, and any id shorter than 4 characters (exit 2).

This checklist changes the audit meaning of one live chain. It does not quarantine a file and it does not rehash history. Do not hand-edit `logs/` or `state/`. Do not claim the live ledger was repaired until the commands below have been run on it and the verify line matches.

N1 found no seq 1920 anywhere searched, so the live path is this seal, not inserting a recovered line.

## What verify honours (gblog 0.6.0 and later)

`gblog verify` itself now honours only the pinned ack. Before 0.6.0 the pin was enforced only by `gblog seal`; verify accepted any single self-consistent `break_ack`.

- A `break_ack` approves a discontinuity only when it is on the `kim-biseo` chain **and** its descriptor equals `KIM_BISEO_BREAK` in every field: `logs/kim-biseo/2026-11-02.jsonl`, lines 164/165, seq 1919→1921, missing `{1920}`, and the three full hashes.
- Any other `break_ack` (on another bot, at other coordinates, covering a longer gap, with different hashes, or appended by hand) is itself a problem: `<file>:<line>: break_ack is not the pinned kim-biseo 2026-11-02 approval`. That chain is FAIL.
- There is therefore no way to approve a break by appending an ack outside this checklist, and this checklist cannot be reused for another chain or another gap.
- Test-only exception: on a ledger root under the system temporary directory (compared after `realpath`), an ack whose descriptor equals the committed fabricated fixture (the six pinned coordinates and the three fixture hashes, `KIM_BISEO_FIXTURE_BREAK`) is accepted. Verify then marks the result `fixture_ack` and prints `! fixture break_ack honoured: this root is a temporary test copy`. This never applies to the live root. Do not move or copy the live ledger under a temporary directory to run this procedure.
- The temporary directory is whatever `TMPDIR` says. From 0.6.1 that no longer widens the rule: the fixture hashes are compiled in, so an ack with any other hashes is FAIL wherever the root is. If `TMPDIR` on the ledger host points at or above the ledger root, unset it before you start, so that a fixture ack can never be honoured there.

## Not part of this procedure

`gblog state rebuild` and `gblog spool replay` are **not** part of this procedure. Do not run them here.

- `state rebuild` rewrites `state/<bot>.json` from the log tail when state is missing, reset or behind. It refuses a chain whose walk has any problem other than the state-head mismatch, so it refuses kim-biseo before the seal, and it cannot close the 1919→1921 gap.
- `spool replay` appends events that an earlier append refused. It does not approve anything.
- If an append is refused during this checklist (exit 4, `ALERT tail mismatch`), stop and report. See "Alert" below.

## What the code showed (not a proof of the historical crash)

`append` takes the bot lock, writes the new line with `O_APPEND`, fsyncs the file and its directory, then atomically replaces `state/<bot>.json`. A crash inside that path cannot leave state ahead of the file. If the state file exists, older code trusted it and did not compare it to the log tail. State gets ahead when the jsonl is replaced or truncated **outside** the lock; the next append then uses a `prev` that is not in the file and a seq that skips. That matches 1919 → 1921 with orphan prev `268a3867…`. `sync` and browser capture rewrote state outside the lock (that race moves the head backward). They now re-read state under the same lock. The tail check refuses the forward case.

A crash inside `append` can leave state exactly one record **behind** the file. From 0.6.0 the next append rolls that case forward on its own (`notice: recovered state/<bot>.json after an interrupted append`). That is the opposite direction from the kim-biseo gap and does not explain it.

## After Jay says to apply

1. Install this gblog on the ledger host (`install.sh`). Do not copy `tests/fixtures/kim-biseo-break/` onto the live tree. That fixture is fabricated and its hashes are not the live ones.
2. Read-only: confirm there is still no seq 1920 record whose hash equals `268a3867a13c180b76a4a9d16d544e32647cf0695a7d15d7c6c0ee9f74d0df01`. If one appears, stop and insert that line instead of sealing.
3. Pre-step for the witness: `gblog verify --bot ledger` must print `OK`, and `state/ledger.alert.json` must not exist. `seal` preflights the `ledger` chain tail before it appends anything, because the witness note goes there; with a tail mismatch on `ledger` it refuses with `refusing: ledger witness chain has a tail mismatch` and writes nothing.
   If the `ledger` chain does not exist yet, verify prints `FAIL … no chain on disk for 'ledger'`. That one result is acceptable: the witness note will be the chain's first record. Any other FAIL, or any `!` line, on `ledger`: stop and report.
4. `gblog verify --bot kim-biseo` — expect FAIL at `logs/kim-biseo/2026-11-02.jsonl:165`, prev `268a3867a13c`, expected `11a6e8d43ca0`.
5. `gblog anchor` — OK chains get anchors resumed from their previous anchor. kim-biseo stays FAIL with the break descriptor and both segment heads. Do not skip the other chains.
6. Jay writes down the real approval id.
7. Once: `gblog seal --bot kim-biseo --jay-approval-id <that id>`.
   The command refuses (exit 2, nothing appended and nothing spooled) if the on-disk line, seq, or full hashes differ from `KIM_BISEO_BREAK`, if the id is a placeholder, if a `break_ack` is already present, or if `kim-biseo` itself or the `ledger` witness chain has a tail mismatch. It only appends. It does not rewrite 164/165.
8. `gblog verify --bot kim-biseo` — expect exit 0 and `PASS (1 approved break)`, with no `!` line under it (see "Witness warning"). `gblog verify --all` must not print FAIL. A new break is still FAIL, and so is any second or different `break_ack`.
9. `gblog verify --bot kim-biseo --json` must show `"fixture_ack": false`. If it shows `true` (the text output then also has the line `! fixture break_ack honoured: this root is a temporary test copy`), verify honoured the fabricated test-fixture ack because the root resolves under the temporary directory. That is not the live seal: stop and report.
10. `gblog anchor` again. kim-biseo is now PASS with `approved_breaks` 1 and the same id. Other chains still resume from their own anchors.
11. Leave the witness note on the `ledger` bot chain. `gblog tail --bot ledger -n 1` shows it: a `note` with target `kim-biseo` and detail `break_ack witness logs/kim-biseo/2026-11-02.jsonl seq 1919->1921 missing [1920]`. Do not delete `spool/` or `state/*.alert.json` if an append was refused; report those and stop.

## Witness warning

After the seal, verify cross-checks the witness. If the pinned ack is honoured and a `ledger` chain exists that has no note carrying the same `break` descriptor and the same `jay_approval_id`, verify prints this line under the kim-biseo line:

```
      ! witness note missing on ledger chain
```

- It is a warning, not a FAIL. The line above it still says `PASS (1 approved break)` and the exit code is still 0.
- It means the ack is on the kim-biseo chain but its witness is not on the `ledger` chain: the witness append did not happen, or the `ledger` chain was replaced or truncated afterwards.
- Do not run `seal` again (it refuses: a `break_ack` is already present). Do not hand-append a note. Report the line verbatim to Jay and stop.
- The warning is only raised when a `ledger` chain exists. If `logs/ledger` is absent after the seal, no warning is printed and the witness is also missing; step 11 catches that.
- Other `!` lines (for example `state/kim-biseo.json missing or unreadable: …` or `state/kim-biseo.json was reset to genesis`) are separate warnings. Report them verbatim too; they are not expected in this procedure.

## Alert

Tail mismatch: CLI exit **4**, stderr `ALERT tail mismatch`, event JSON in `spool/<bot>/`, latest copy in `state/<bot>.alert.json`. gblog does not send mail or chat. Page on that exit code and file. Do not edit the log to make the next append succeed.

`seal` preflights both chains. If `kim-biseo` itself has a tail mismatch, step 7 exits 2 with `refusing: kim-biseo has a tail mismatch (state does not match the log tail); nothing was sealed`, and nothing is spooled. Report and wait. A `break_ack` found in `spool/kim-biseo/` (left by an older version) is never appended by `gblog spool replay`; do not move or edit it.

`gblog spool list` shows the refused events and is read-only. Within this procedure do not go further than that: no `gblog spool replay`, no `gblog state rebuild`. Report the alert and wait for the operator.
