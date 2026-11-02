#!/usr/bin/env python3
"""Build a fabricated kim-biseo copy with the 1919→1921 gap at lines 164/165.

This is not the live ledger. Record bodies are synthetic, so their hashes are not
the live hashes pinned in ``gblog.KIM_BISEO_BREAK``. The 2026-11-01 file exists
only so seq 1919 falls on line 164 of 2026-11-02.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)

import gblog  # noqa: E402

ORPHAN_PREV = hashlib.sha256(b"gblog fixture orphan prev; not a record body").hexdigest()
FIXTURE_DIR = HERE


def _stamp(day: int, second: int) -> str:
    base = gblog._dt.datetime(2026, 11, day, tzinfo=gblog._dt.timezone.utc)
    return (base + gblog._dt.timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(seq: int, prev: str, ts: str) -> dict:
    rec = {
        "v": gblog.SCHEMA_VERSION,
        "seq": seq,
        "ts": ts,
        "bot": "kim-biseo",
        "type": "note",
        "detail": "fixture seq %d" % seq,
        "prev": prev,
    }
    rec["hash"] = gblog.compute_hash(rec)
    return rec


def _line(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"


def build(root: str) -> dict:
    """Write logs/ and state/ under ``root``. Return the honest break descriptor."""
    bot_dir = os.path.join(root, "logs", "kim-biseo")
    os.makedirs(bot_dir, exist_ok=True)
    os.makedirs(os.path.join(root, "state"), exist_ok=True)

    prev = gblog.GENESIS
    day1 = []
    for seq in range(1, 1756):
        rec = _record(seq, prev, _stamp(1, seq))
        day1.append(_line(rec))
        prev = rec["hash"]
    day1_path = os.path.join(bot_dir, "2026-11-01.jsonl")
    with open(day1_path, "w", encoding="utf-8") as fh:
        fh.writelines(day1)

    day2 = []
    # lines 1..164 → seq 1756..1919
    for offset, seq in enumerate(range(1756, 1920), start=1):
        rec = _record(seq, prev, _stamp(2, offset))
        day2.append((seq, rec))
        prev = rec["hash"]
    left = day2[-1][1]
    if len(day2) != 164 or left["seq"] != 1919:
        raise RuntimeError("fixture padding drifted: line 164 is not seq 1919")

    right = _record(1921, ORPHAN_PREV, _stamp(2, 165))
    prev = right["hash"]
    tail = [right]
    for offset, seq in enumerate(range(1922, 1927), start=166):
        rec = _record(seq, prev, _stamp(2, offset))
        tail.append(rec)
        prev = rec["hash"]

    day2_path = os.path.join(bot_dir, "2026-11-02.jsonl")
    with open(day2_path, "w", encoding="utf-8") as fh:
        for _seq, rec in day2:
            fh.write(_line(rec))
        for rec in tail:
            fh.write(_line(rec))

    last = tail[-1]
    state = {"head": last["hash"], "seq": last["seq"], "updated": last["ts"]}
    with open(os.path.join(root, "state", "kim-biseo.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")

    descriptor = {
        "file": "logs/kim-biseo/2026-11-02.jsonl",
        "line_before": 164,
        "line_after": 165,
        "seq_before": 1919,
        "seq_after": 1921,
        "hash_before": left["hash"],
        "orphan_prev": ORPHAN_PREV,
        "hash_after": right["hash"],
        "missing_seqs": [1920],
    }
    return descriptor


def main() -> None:
    descriptor = build(FIXTURE_DIR)
    doc = {
        "fabricated": True,
        "live_ledger": False,
        "note": "Honest hashes of this fabricated copy. Not the live record bodies. gblog seal (no copy flag) must refuse this tree.",
        "orphan_prev_source": "sha256('gblog fixture orphan prev; not a record body')",
        "break": descriptor,
    }
    with open(os.path.join(FIXTURE_DIR, "expected-break.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
