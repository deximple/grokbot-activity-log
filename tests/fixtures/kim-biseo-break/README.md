# Fabricated kim-biseo break (not the live ledger)

This tree is a **copy built for tests**. It is not `activity-log` and it is not the private grokledger.

`logs/kim-biseo/2026-11-01.jsonl` is padding so that `logs/kim-biseo/2026-11-02.jsonl` **line 164 is seq 1919** and **line 165 is seq 1921** (missing `{1920}`). Record bodies are synthetic notes. Their `hash` values are the real SHA-256 of those lines. They are **not** the live hashes pinned in `gblog.KIM_BISEO_BREAK`.

`gblog seal` (the operator command) must refuse this tree. Tests seal it only through `Ledger.seal_break(..., copy_fixture=True)` on a temporary copy, which still requires the on-disk bytes to match the descriptor.

Regenerate with:

```bash
python3 tests/fixtures/kim-biseo-break/build_fixture.py
```
