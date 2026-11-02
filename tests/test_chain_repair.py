"""Copy-fixture tests for the kim-biseo chain repair. Never points at a live ledger."""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "kim_biseo_fixture",
    os.path.join(HERE, "fixtures", "kim-biseo-break", "build_fixture.py"),
)
_FIXTURE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE)

COMMITTED = os.path.join(HERE, "fixtures", "kim-biseo-break")


def run(argv, root):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = gblog.main(["--root", root] + argv)
    return rc, out.getvalue(), err.getvalue()


def _copy_fixture(dest):
    for name in ("logs", "state"):
        shutil.copytree(os.path.join(COMMITTED, name), os.path.join(dest, name))


class ChainRepairTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gblog-chain-repair-")
        _copy_fixture(self.root)
        self.ledger = gblog.Ledger(self.root)
        self.break_file = os.path.join(self.root, "logs", "kim-biseo", "2026-11-02.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_pinned_incident_coordinates(self):
        br = gblog.KIM_BISEO_BREAK
        self.assertEqual(br["bot"], "kim-biseo")
        self.assertEqual(br["file"], "logs/kim-biseo/2026-11-02.jsonl")
        self.assertEqual((br["line_before"], br["line_after"]), (164, 165))
        self.assertEqual((br["seq_before"], br["seq_after"], br["missing_seqs"]), (1919, 1921, [1920]))
        self.assertEqual(br["hash_before"], "11a6e8d43ca0155f418be8bee452330dd76177249cd51dd5383ba9c46ad3a23e")
        self.assertEqual(br["orphan_prev"], "268a3867a13c180b76a4a9d16d544e32647cf0695a7d15d7c6c0ee9f74d0df01")
        self.assertEqual(br["hash_after"], "e8519bbde0e32dd8bb449d8fe61308540f1fc18d6cd9e899461e28f01c59a78e")

    def test_committed_fixture_matches_builder_and_is_not_the_live_hashes(self):
        fresh = tempfile.mkdtemp(prefix="gblog-fixture-rebuild-")
        try:
            built = _FIXTURE.build(fresh)
            for rel in (
                "logs/kim-biseo/2026-11-01.jsonl",
                "logs/kim-biseo/2026-11-02.jsonl",
                "state/kim-biseo.json",
            ):
                with open(os.path.join(COMMITTED, rel), "rb") as fh:
                    committed = fh.read()
                with open(os.path.join(fresh, rel), "rb") as fh:
                    rebuilt = fh.read()
                self.assertEqual(committed, rebuilt, rel)
            with open(os.path.join(COMMITTED, "expected-break.json"), encoding="utf-8") as fh:
                expected = json.load(fh)
            self.assertTrue(expected["fabricated"])
            self.assertFalse(expected["live_ledger"])
            self.assertEqual(expected["break"], built)
            live = gblog.KIM_BISEO_BREAK
            self.assertNotEqual(built["hash_before"], live["hash_before"])
            self.assertNotEqual(built["hash_after"], live["hash_after"])
            self.assertNotEqual(built["orphan_prev"], live["orphan_prev"])
        finally:
            shutil.rmtree(fresh, ignore_errors=True)

    def test_verify_fails_then_seal_passes_then_second_break_fails(self):
        before = _snapshot(self.root)
        rc, out, _ = run(["verify", "--bot", "kim-biseo"], self.root)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", out)
        self.assertNotIn("PASS(승인 단절", out)
        self.assertIn("broken chain", out)

        res = self.ledger.verify("kim-biseo")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["breaks"]), 1, res)
        self.assertEqual(len(res["segments"]), 2, res["segments"])
        observed = res["breaks"][0]
        self.assertEqual(observed["file"], "logs/kim-biseo/2026-11-02.jsonl")
        self.assertEqual((observed["line_before"], observed["line_after"]), (164, 165))
        self.assertEqual((observed["seq_before"], observed["seq_after"]), (1919, 1921))
        self.assertEqual(observed["missing_seqs"], [1920])
        self.assertEqual(res["segments"][0]["head"], observed["hash_before"])
        self.assertNotEqual(res["segments"][0]["head"], res["segments"][1]["head"])
        self.assertEqual(len(res["segments"][0]["head"]), 64)
        self.assertEqual(len(res["segments"][1]["head"]), 64)
        # both segments were walked, not stopped at the first gap
        self.assertGreater(res["records"], 1921)

        with self.assertRaises(gblog.SealError):
            self.ledger.seal_break("kim-biseo", observed, "PENDING-JAY", copy_fixture=False)
        other = {
            "file": "logs/kim-biseo/2026-11-02.jsonl",
            "line_before": 10, "line_after": 11,
            "seq_before": 10, "seq_after": 12,
            "hash_before": "11" * 32, "orphan_prev": "22" * 32, "hash_after": "33" * 32,
            "missing_seqs": [11],
        }
        with self.assertRaises(gblog.SealError) as ctx:
            self.ledger.seal_break("kim-biseo", other, "PENDING-JAY", copy_fixture=True)
        self.assertIn("coordinates", str(ctx.exception))
        rc, _out, err = run(["seal", "--bot", "kim-biseo", "--jay-approval-id", "PENDING-JAY"], self.root)
        self.assertEqual(rc, 2, err)
        self.assertIn("placeholder", err)
        rc, _out, err = run(["seal", "--bot", "kim-biseo", "--jay-approval-id", "JAY-NOT-THIS-COPY"], self.root)
        self.assertEqual(rc, 2, err)
        self.assertIn("on-disk", err)
        self.assertEqual(_snapshot(self.root), before)
        rc, out, _ = run(["verify", "--bot", "kim-biseo"], self.root)
        self.assertEqual(rc, 1)
        self.assertNotIn("PASS(승인 단절", out)

        ack = self.ledger.seal_break("kim-biseo", observed, "PENDING-JAY", copy_fixture=True)
        self.assertEqual(ack["type"], "break_ack")
        self.assertEqual(ack["jay_approval_id"], "PENDING-JAY")
        self.assertEqual(ack["break"]["missing_seqs"], [1920])
        after = _snapshot(self.root)
        for rel, data in before.items():
            self.assertTrue(after[rel].startswith(data), rel)
        witness = [r for r in self.ledger.records("ledger") if r.get("type") == "note"]
        self.assertEqual(witness[-1]["jay_approval_id"], "PENDING-JAY")
        self.assertEqual(witness[-1]["break"]["seq_before"], 1919)

        res = self.ledger.verify("kim-biseo")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["approved_breaks"], 1)
        self.assertEqual(res["breaks"], [])
        self.assertEqual(len(res["segments"]), 2)
        self.assertEqual(res["jay_approval_id"], "PENDING-JAY")
        rc, out, _ = run(["verify", "--bot", "kim-biseo"], self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS(승인 단절 1건)", out)
        self.assertIn("approved_discontinuities=1", out)
        rc, out, _ = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS(승인 단절 1건)", out)
        self.assertIn("ledger", out)

        with self.assertRaises(gblog.SealError):
            self.ledger.seal_break("kim-biseo", observed, "PENDING-JAY", copy_fixture=True)

        _splice_second_break(self.break_file)
        res = self.ledger.verify("kim-biseo")
        self.assertFalse(res["ok"], res)
        self.assertTrue(any("broken chain" in p for p in res["problems"]))
        rc, out, _ = run(["verify", "--bot", "kim-biseo"], self.root)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", out)
        self.assertNotIn("PASS(승인 단절", out)

    def test_wrong_ack_does_not_pass(self):
        res = self.ledger.verify("kim-biseo")
        bogus = dict(res["breaks"][0])
        bogus["hash_before"] = "ab" * 32
        self.ledger.append("kim-biseo", {
            "type": "break_ack",
            "jay_approval_id": "PENDING-JAY",
            "break": bogus,
            "detail": "wrong coordinates",
        })
        res = self.ledger.verify("kim-biseo")
        self.assertFalse(res["ok"], res)
        self.assertTrue(any("does not match" in p or "broken chain" in p for p in res["problems"]))
        rc, out, _ = run(["verify", "--bot", "kim-biseo"], self.root)
        self.assertEqual(rc, 1)
        self.assertNotIn("PASS(승인 단절", out)

    def test_anchor_ok_chain_resumes_and_fail_chain_keeps_both_heads(self):
        self.ledger.append("ops", {"type": "note", "detail": "ok chain", "ts": "2026-11-02T12:00:00Z"})
        first = self.ledger.read_state("ops")["head"]
        rollup = self.ledger.anchor()
        self.assertFalse(rollup["whole_ledger_skip"])
        self.assertEqual(rollup["mode"], "per-chain")
        by = {c["bot"]: c for c in rollup["chains"]}
        self.assertIn("ops", by)
        self.assertIn("kim-biseo", by)
        self.assertTrue(by["ops"]["ok"])
        self.assertIsNone(by["ops"]["resumed_from"])
        self.assertFalse(by["kim-biseo"]["ok"])
        self.assertEqual(len(by["kim-biseo"]["segment_heads"]), 2)
        br = by["kim-biseo"]["break"]
        self.assertEqual((br["seq_before"], br["seq_after"], br["missing_seqs"]), (1919, 1921, [1920]))
        self.assertEqual(by["kim-biseo"]["segment_heads"][0], br["hash_before"])
        self.assertEqual(by["kim-biseo"]["segments"][1]["seq_start"], 1921)
        rc, out, _ = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out)
        self.assertIn("ANCHOR OK", out)
        self.assertIn("ANCHOR FAIL", out)
        self.assertIn("seq 1919->1921", out)
        second = self.ledger.read_anchor("ops")
        self.assertEqual(second["resumed_from"], first)
        self.assertTrue(second["ok"])
        day = os.path.join(self.root, "anchors", "days")
        self.assertTrue(any(name.endswith(".json") for name in os.listdir(day)))

    def test_tail_mismatch_refuses_spools_and_alerts(self):
        self.ledger.append("ops", {"type": "note", "detail": "one", "ts": "2026-11-02T12:00:00Z"})
        self.ledger.append("ops", {"type": "note", "detail": "two", "ts": "2026-11-02T12:01:00Z"})
        path = self.ledger.log_files("ops")[0]
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        state = self.ledger.read_state("ops")
        state["seq"] = 99
        state["head"] = "cd" * 32
        self.ledger.write_state("ops", state)
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            self.ledger.append("ops", {"type": "note", "detail": "should spool"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertTrue(os.path.isfile(ctx.exception.spool_path))
        self.assertTrue(os.path.isfile(ctx.exception.alert_path))
        with open(ctx.exception.spool_path, encoding="utf-8") as fh:
            spool = json.load(fh)
        self.assertEqual(spool["kind"], "tail_mismatch")
        self.assertEqual(spool["record"]["detail"], "should spool")
        self.assertEqual(spool["expected_seq"], 99)
        with open(ctx.exception.alert_path, encoding="utf-8") as fh:
            alert = json.load(fh)
        self.assertEqual(alert["spool"], spool["spool"])
        rc, _out, err = run(["note", "--bot", "ops", "--detail", "cli spool"], self.root)
        self.assertEqual(rc, 4, err)
        self.assertIn("ALERT tail mismatch", err)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        # restoring the matching state lets append proceed; the refused events stayed in the spool
        good = self.ledger._tail_head("ops")
        self.ledger.write_state("ops", {"seq": good[0], "head": good[1]})
        rec = self.ledger.append("ops", {"type": "note", "detail": "after repair"})
        self.assertEqual(rec["seq"], good[0] + 1)
        self.assertEqual(rec["prev"], good[1])
        self.assertTrue(self.ledger.verify("ops")["ok"])


def _snapshot(root):
    found = {}
    logs = os.path.join(root, "logs")
    for dirpath, _dirs, files in os.walk(logs):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                found[os.path.relpath(path, root)] = fh.read()
    return found


def _splice_second_break(path):
    """Change one segment-2 record's prev and recompute only that record's hash."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    rec = json.loads(lines[166])  # line 167, seq 1923, still inside segment 2
    self_seq = rec["seq"]
    if self_seq != 1923:
        raise AssertionError("fixture layout drifted: line 167 seq %s" % self_seq)
    rec["prev"] = "ef" * 32
    rec["hash"] = gblog.compute_hash(rec)
    lines[166] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    unittest.main()
