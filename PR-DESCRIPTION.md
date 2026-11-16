# Hardening pass 2026-11-16: crash safety, sync/push guards, failure UX, portability, 35→721 tests

## Summary
- Four rounds of audits: `AUDIT-2026-11-16.md` and `AUDIT-ROUND2..4-2026-11-16.md`.
- Fourteen specialist reviews and six deep reviews, in `docs/reviews/`.
- Everything implemented comes with tests. Highlights:
  - **Integrity and crash safety.** Appends are made durable in a crash-safe order, and an interrupted append is reported as interrupted ("one record behind; repaired by this record"), not as tampering. Day-rollup and witness entries are protected, FAIL anchors are bounded, one side-file rule applies everywhere, and SIGTERM/SIGHUP are handled.
  - **sync and push.**
    - `sync` refuses a git repo that is not a ledger. Genuine backups are recognised, and the repo is marked with `gblog.ledger = true`.
    - A failed origin replacement is undone (exit 4, no commit).
    - A legacy `.git/remotes/origin` file blocks setting a first origin.
    - A system or global `remote.origin.url` stops the push.
  - **Tasks and hooks.** There is one task-status function, with a bounded scan and an "already closed" warning. The hook fallback decides only on a refused bot name.
  - **Operator UX.** Error messages are actionable and name the remedy command. `--json` errors have a consistent shape, and there is an exit-code table.
  - **Portability.** The install replaces the executable atomically. `bin/gblog`, the CI matrix and `.gitattributes` are updated.
  - **Docs.** README, SECURITY, DESIGN, CHANGELOG, FAQ and agent-integration were corrected to match the code.

## Compatibility
- `verify --all` on a copy of the production ledger gives byte-identical output to v0.5. Existing flags and stdout formats are kept.
- Behaviour changes:
  - `sync` refuses a non-ledger git repo. Run `git config gblog.ledger true` to opt in.
  - `sync --replace-remote` exit 4 now means no commit was made. Some docs still say otherwise; see below.
  - In an empty `git init` directory, `gblog init` must run before `sync`.

## Tests
- `python3 -m unittest discover -s tests`: **721 tests, OK.** 2 are skipped (one environment-dependent case-insensitive-volume test, one lint self-test) and 2 are expected failures, listed in `tests/PENDING.txt`.
- shellcheck (`install.sh`, `bin/gblog`, `examples/*.sh`) and the workflow YAML check are clean.

## Unfinished / follow-ups
See `CHANGES-2026-11-16.md`, section "Unfinished / in-progress work at wrap-up", for full detail.
- **Security review S3** is kept outside the repo. It has 3 High findings, none implemented yet:
  - FAIL anchor runs launder the witnesses.
  - The pushed branch is not pinned.
  - "Private" is judged from the URL text, not the connection.
- **W9 partial:**
  - K9-05 upgrade notes. Docs still promise a commit on `--replace-remote` exit 4.
  - K10-07c (needs a decision).
  - The Low items K9-06..08 and K10-08..12.
- **K7 follow-ups in gblog.py:** AppleDouble/.DS_Store, case variants, F_FULLFSYNC, UTF-8 argv, NFC, old Python/git CI jobs.
- **Reviewed but not implemented:** K11 (refactor plan), K12 (privacy and redaction), K13 (performance), K14 (CLI help).
- **Z1 skipped items:** G1-03 test branch, G1-04 FIFO verdict, G1-05b first origin, G1-12 large refactor.
- **Tests:**
  - 2 expected failures: `capture browser --hours 0`, and a day file deleted after the verify snapshot.
  - 1 skipped lint self-test.
  - CHANGELOG still lists K3-01, K3-04, K4-02, K4-04 and K4-07 as open, though they are fixed.
- **Discarded or superseded, nothing lost:** V1 (→V1b), V2 (stale), V3 (18 hunks dropped), XT/XT2 (→XT3), Z2 (→Z2b), O1/AUDITB/AUDITB2/R2A/R2B (no output). The S1c deferred hunks were re-done in P1.
