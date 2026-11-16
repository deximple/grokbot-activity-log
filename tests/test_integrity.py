"""Integrity tests for the post-audit ledger (audit items I-01, I-02, I-20, I-21).

Covers the verify-time break_ack pin, rollback / truncation / deletion detection
(state warnings, anchor containment, anchor history, ``--contains``), archive path
checks and the strict verify mode.

Every chain is built in a temporary directory with explicit, fixed timestamps, so
no case depends on the wall clock. Nothing here touches a live ledger or the network.
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "kim-biseo-break")
PIN_MSG = "break_ack is not the pinned kim-biseo 2026-11-02 approval"
STATE_MISSING = "state/%s.json missing or unreadable: tail truncation cannot be detected from state"
STATE_RESET = "state/%s.json was reset to genesis"


def run(argv, root):
    """Run the CLI in-process. An uncaught exception is rendered like the real CLI would (traceback, exit 1)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = gblog.main(["--root", root] + list(argv))
        except SystemExit as exc:
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                err.write("%s\n" % code)
                rc = 1
        except Exception:  # pragma: no cover - only on a regression
            traceback.print_exc(file=err)
            rc = 1
    return rc, out.getvalue(), err.getvalue()


def stamp(n, day="2026-03-01"):
    return "%sT10:%02d:%02dZ" % (day, n // 60, n % 60)


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


def _snapshot_logs(root):
    found = {}
    for dirpath, _dirs, files in os.walk(os.path.join(root, "logs")):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                found[os.path.relpath(path, root)] = fh.read()
    return found


class IntegrityBase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="gblog-integrity-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)

    def chain(self, bot, n, day="2026-03-01", start=0):
        recs = []
        for i in range(start, start + n):
            recs.append(self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1), "ts": stamp(i, day)}))
        return recs

    def log_path(self, bot, day="2026-03-01"):
        return os.path.join(self.root, "logs", bot, day + ".jsonl")

    def state_path(self, bot):
        return os.path.join(self.root, "state", bot + ".json")

    def drop_last_line(self, bot, day="2026-03-01"):
        path = self.log_path(bot, day)
        _write(path, _lines(path)[:-1])

    def copy_fixture(self):
        for name in ("logs", "state"):
            shutil.copytree(os.path.join(FIXTURE, name), os.path.join(self.root, name))


# --------------------------------------------------------------------------- I-01
class BreakAckPinTest(IntegrityBase):
    def _forge_ack(self, bot, desc, day="2026-03-01"):
        return self.ledger.append(bot, {
            "type": "break_ack", "action": "other", "outcome": "ok",
            "jay_approval_id": "FORGED-APPROVAL-1", "break": desc,
            "detail": "forged acknowledgement", "ts": stamp(30, day),
        })

    def _assert_forged_ack_fails(self, bot):
        res = self.ledger.verify(bot)
        self.assertFalse(res["ok"], res)
        self.assertEqual(res.get("approved_breaks"), 0, res)
        self.assertTrue(any(PIN_MSG in p for p in res["problems"]), res["problems"])
        rc, out, err = run(["verify", "--bot", bot], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL", out)
        self.assertNotIn("approved break", out)
        self.assertNotIn("PASS", out)
        return res

    def test_forged_ack_other_bot_fails(self):
        self.chain("ops", 5)
        path = self.log_path("ops")
        lines = _lines(path)
        _write(path, lines[:2] + lines[3:])  # delete seq 3; the tail still matches state
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["breaks"]), 1, res)
        desc = res["breaks"][0]
        self.assertEqual((desc["seq_before"], desc["seq_after"], desc["missing_seqs"]), (2, 4, [3]))
        self._forge_ack("ops", desc)
        res = self._assert_forged_ack_fails("ops")
        # the forged ack is line 5 of the day file (4 surviving records + the ack)
        self.assertIn("logs/ops/2026-03-01.jsonl:5: " + PIN_MSG, res["problems"])

    def test_forged_ack_multi_record_gap_fails(self):
        # the pinned bot and the pinned day file, but not the pinned lines / seqs
        day = "2026-11-02"
        self.chain("kim-biseo", 8, day=day)
        path = self.log_path("kim-biseo", day)
        lines = _lines(path)
        _write(path, lines[:2] + lines[5:])  # delete seq 3..5
        res = self.ledger.verify("kim-biseo")
        self.assertEqual(len(res["breaks"]), 1, res)
        desc = res["breaks"][0]
        self.assertEqual(desc["file"], gblog.KIM_BISEO_BREAK["file"])
        self.assertEqual((desc["seq_before"], desc["seq_after"], desc["missing_seqs"]), (2, 6, [3, 4, 5]))
        self._forge_ack("kim-biseo", desc, day=day)
        self._assert_forged_ack_fails("kim-biseo")

    def _rewrite_prefix(self, bot, upto, day):
        path = self.log_path(bot, day)
        lines = _lines(path)
        prev = gblog.GENESIS
        for i in range(upto):
            rec = json.loads(lines[i])
            rec["detail"] = "rewritten history %d" % (i + 1)
            rec["prev"] = prev
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            lines[i] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        _write(path, lines)

    def test_forged_ack_rewritten_prefix_fails(self):
        for bot, day in (("ops", "2026-03-01"), ("kim-biseo", "2026-11-02")):
            with self.subTest(bot=bot):
                self.chain(bot, 6, day=day)
                self._rewrite_prefix(bot, 3, day)
                res = self.ledger.verify(bot)
                self.assertFalse(res["ok"])
                self.assertEqual(len(res["breaks"]), 1, res)
                desc = res["breaks"][0]
                # consecutive seqs, only the prev link is wrong: nothing is "missing"
                self.assertEqual((desc["seq_before"], desc["seq_after"], desc["missing_seqs"]), (3, 4, []))
                self._forge_ack(bot, desc, day=day)
                self._assert_forged_ack_fails(bot)

    def test_fixture_ack_still_passes_in_temp_copy(self):
        self.copy_fixture()
        res = self.ledger.verify("kim-biseo")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["breaks"]), 1, res)
        desc = res["breaks"][0]
        self.ledger.append("kim-biseo", {
            "type": "break_ack", "action": "other", "outcome": "ok",
            "jay_approval_id": "TEST-APPROVAL-0001", "break": desc,
            "detail": "fixture seal", "ts": "2026-11-03T00:00:00Z",
        })
        res = self.ledger.verify("kim-biseo")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["approved_breaks"], 1)
        self.assertIs(res.get("fixture_ack"), True)
        self.assertEqual(res["jay_approval_id"], "TEST-APPROVAL-0001")
        self.assertNotIn("witness note missing on ledger chain", res.get("warnings") or [])
        rc, out, err = run(["verify", "--bot", "kim-biseo"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("PASS (1 approved break)", out)
        # the same fabricated ack is not honoured outside a temporary copy: its hashes are not the pinned ones
        with mock.patch.object(gblog.Ledger, "_is_temp_copy", return_value=False):
            res = self.ledger.verify("kim-biseo")
        self.assertFalse(res["ok"], res)
        self.assertTrue(any(PIN_MSG in p for p in res["problems"]), res["problems"])

    def test_seal_refuses_when_ledger_tail_mismatch(self):
        self.copy_fixture()
        self.ledger.append("ledger", {"type": "note", "detail": "witness chain one", "ts": "2026-11-02T12:00:00Z"})
        self.ledger.append("ledger", {"type": "note", "detail": "witness chain two", "ts": "2026-11-02T12:01:00Z"})
        self.ledger.write_state("ledger", {"seq": 99, "head": "cd" * 32})
        desc = self.ledger.verify("kim-biseo")["breaks"][0]
        before = _snapshot_logs(self.root)
        with self.assertRaises(gblog.SealError) as ctx:
            self.ledger.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
        self.assertIn("ledger witness chain has a tail mismatch", str(ctx.exception))
        # nothing was appended anywhere, and the preflight did not spool or alert
        self.assertEqual(_snapshot_logs(self.root), before)
        self.assertFalse(any(r.get("type") == "break_ack" for r in self.ledger.records("kim-biseo")))
        spool = os.path.join(self.root, "spool", "ledger")
        self.assertFalse(os.path.isdir(spool) and os.listdir(spool))
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "ledger.alert.json")))
        # once the witness chain is healthy again the same seal goes through, ack and witness together
        seq, head = self.ledger._tail_head("ledger")
        self.ledger.write_state("ledger", {"seq": seq, "head": head})
        ack = self.ledger.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
        self.assertEqual(ack["type"], "break_ack")
        witness = [r for r in self.ledger.records("ledger") if r.get("jay_approval_id") == "TEST-APPROVAL-0001"]
        self.assertEqual(len(witness), 1)
        self.assertTrue(self.ledger.verify("kim-biseo")["ok"])

    def test_seal_cli_rejects_placeholders(self):
        for value in ("PENDING-JAY", "pending-jay", "Pending", "TBD", "todo", "PlaceHolder", "<id>", "<ID>", "abc", "j1"):
            with self.subTest(value=value):
                rc, out, err = run(["seal", "--bot", "kim-biseo", "--jay-approval-id", value], self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("seal:", err)
                self.assertIn("refusing", err)
                # rejected as a placeholder, before the chain on disk is even looked at
                self.assertNotIn("on-disk", err)
                self.assertNotIn("Traceback", err)
        # a real-looking id gets past the placeholder check and is refused for the right reason
        rc, out, err = run(["seal", "--bot", "kim-biseo", "--jay-approval-id", "JAY-2026-11-16-0042"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("on-disk", err)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "logs", "kim-biseo")))


# --------------------------------------------------------------------------- I-02
class RollbackDetectionTest(IntegrityBase):
    def _assert_anchor_problem(self, res, head):
        self.assertFalse(res["ok"], res)
        hits = [p for p in res["problems"] if head[:12] in p and "no longer in the chain" in p]
        self.assertTrue(hits, res["problems"])
        return hits

    def test_truncate_and_delete_state_warns(self):
        recs = self.chain("ops", 3)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res.get("warnings"), [])
        self.assertIs(res.get("fixture_ack"), False)
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0)
        self.assertNotIn("      ! ", out)

        self.drop_last_line("ops")
        # while state survives, the truncation is still a FAIL
        self.assertFalse(self.ledger.verify("ops")["ok"])

        state = self.state_path("ops")
        for label, content in (("missing", None), ("unparsable", "{not json"), ("non-dict", "[1, 2]")):
            with self.subTest(state=label):
                if content is None:
                    if os.path.exists(state):
                        os.remove(state)
                else:
                    with open(state, "w", encoding="utf-8") as fh:
                        fh.write(content)
                res = self.ledger.verify("ops")
                self.assertTrue(res["ok"], res)  # a warning, not a FAIL
                self.assertEqual(res["records"], 2)
                self.assertEqual(res["head"], recs[1]["hash"])
                self.assertIn(STATE_MISSING % "ops", res["warnings"])
                rc, out, err = run(["verify", "--bot", "ops"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(out.startswith("OK"), out)
                self.assertIn("      ! " + STATE_MISSING % "ops", out)
                self.assertNotIn("Traceback", err)

        self.ledger.write_state("ops", {"seq": 0, "head": gblog.GENESIS})
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res)
        self.assertIn(STATE_RESET % "ops", res["warnings"])
        rc, out, _ = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn("      ! " + STATE_RESET % "ops", out)

    def test_truncate_after_anchor_fails(self):
        recs = self.chain("ops", 3)
        rollup = self.ledger.anchor()
        self.assertTrue(rollup["chains"][0]["ok"], rollup)
        self.assertTrue(self.ledger.verify("ops")["ok"])
        head = recs[-1]["hash"]

        self.drop_last_line("ops")
        os.remove(self.state_path("ops"))
        hits = self._assert_anchor_problem(self.ledger.verify("ops"), head)
        self.assertTrue(any(p.startswith("anchors/ops.json: anchored head %s" % head[:12]) for p in hits), hits)
        self.assertTrue(any("is no longer in the chain (history rewritten or tail truncated)" in p for p in hits), hits)
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL", out)

        # rewriting state to agree with the truncated tail does not help
        self.ledger.write_state("ops", {"seq": 2, "head": recs[1]["hash"]})
        self._assert_anchor_problem(self.ledger.verify("ops"), head)

        # nor does deleting the latest anchor: the append-only history still names the lost head
        os.remove(os.path.join(self.root, "anchors", "ops.json"))
        self._assert_anchor_problem(self.ledger.verify("ops"), head)
        rc, out, _ = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out)

    def test_full_rewrite_after_anchor_fails(self):
        recs = self.chain("ops", 3)
        self.ledger.anchor()
        shutil.rmtree(os.path.join(self.root, "logs", "ops"))
        os.remove(self.state_path("ops"))
        for i in range(3):
            self.ledger.append("ops", {"type": "note", "detail": "rewritten %d" % (i + 1), "ts": stamp(i)})
        # the new chain is self-consistent and agrees with its state; only the anchor can tell
        res = self.ledger.verify("ops")
        self._assert_anchor_problem(res, recs[-1]["hash"])
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out)

    def test_deleted_chain_reported_by_verify_all(self):
        self.chain("ops", 3)
        self.chain("keeper", 2)
        self.ledger.anchor()
        rc, out, _ = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out)
        shutil.rmtree(os.path.join(self.root, "logs", "ops"))
        os.remove(self.state_path("ops"))
        for argv in (["verify", "--all"], ["verify"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 1, out + err)
                self.assertRegex(out, r"(?m)^FAIL\s+ops\s+0 records")
                self.assertIn("chain was anchored on", out)
                self.assertIn("but logs/ops is missing", out)
                self.assertRegex(out, r"(?m)^OK\s+keeper\s")

    def test_contains_flag(self):
        recs = self.chain("ops", 3)
        rc, out, err = run(["verify", "--bot", "ops", "--contains", recs[1]["hash"]], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["verify", "--bot", "ops", "--contains", recs[0]["hash"], "--contains", recs[2]["hash"]], self.root)
        self.assertEqual(rc, 0, out + err)
        missing = "ab" * 32
        rc, out, err = run(["verify", "--bot", "ops", "--contains", recs[0]["hash"], "--contains", missing], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("expected hash %s not found in chain" % missing[:12], out + err)

        # the off-box head is what exposes a rollback that state can no longer show
        self.drop_last_line("ops")
        os.remove(self.state_path("ops"))
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out)
        rc, out, err = run(["verify", "--bot", "ops", "--contains", recs[2]["hash"]], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("expected hash %s not found in chain" % recs[2]["hash"][:12], out + err)

        # usage: needs --bot and a full 64-hex hash
        for argv in (["verify", "--all", "--contains", recs[0]["hash"]],
                     ["verify", "--contains", recs[0]["hash"]],
                     ["verify", "--bot", "ops", "--contains", "abc123"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertNotIn("Traceback", err)

    def test_anchor_history_appended_not_rewritten(self):
        recs = self.chain("ops", 3)
        self.ledger.anchor()
        hist = os.path.join(self.root, "anchors", "ops.history.jsonl")
        self.assertTrue(os.path.isfile(hist))
        with open(hist, "rb") as fh:
            first_bytes = fh.read()
        entries = [json.loads(line) for line in first_bytes.decode("utf-8").splitlines() if line.strip()]
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0]["ok"], True)
        self.assertEqual(entries[0]["heads"], [recs[-1]["hash"]])
        self.assertEqual(entries[0]["records"], 3)
        self.assertIn("ts", entries[0])

        more = self.chain("ops", 2, start=3)
        self.ledger.anchor()
        with open(hist, "rb") as fh:
            second_bytes = fh.read()
        self.assertTrue(second_bytes.startswith(first_bytes))
        entries = [json.loads(line) for line in second_bytes.decode("utf-8").splitlines() if line.strip()]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["heads"], [more[-1]["hash"]])
        self.assertEqual(entries[1]["records"], 5)
        # an old anchored head that is still in the (longer) chain is fine
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])

        rc, out, err = run(["anchor", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        with open(hist, "rb") as fh:
            third_bytes = fh.read()
        self.assertTrue(third_bytes.startswith(second_bytes))
        self.assertEqual(len([l for l in third_bytes.splitlines() if l.strip()]), 3)

        with open(hist, "ab") as fh:
            fh.write(b"{not json\n")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertTrue(any("history" in p for p in res["problems"]), res["problems"])


# --------------------------------------------------------------------------- I-20 (verify side)
class ArchiveIntegrityTest(IntegrityBase):
    def test_archive_path_escape_is_problem(self):
        data = b"outside evidence\n"
        digest = hashlib.sha256(data).hexdigest()
        outside = os.path.join(self.base, "outside.gz")
        with gzip.open(outside, "wb") as fh:
            fh.write(data)
        for bad in ("../outside.gz", outside, "archive/../../outside.gz", "logs/ops/x.gz"):
            with self.subTest(path=bad):
                problems = gblog.verify_archives(self.ledger, [{"type": "archive", "path": bad, "sha256": digest}])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("invalid archive path", problems[0])

        # end to end: a chained record that points outside archive/ at a file with the right hash
        self.ledger.append("ops", {"type": "archive", "action": "file", "target": "outside", "outcome": "ok",
                                   "kind": "other", "sha256": digest, "bytes": len(data),
                                   "path": "../outside.gz", "ts": stamp(0)})
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertTrue(any("invalid archive path" in p for p in res["problems"]), res["problems"])
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out)

        # a normal archive record is unaffected
        rec = gblog.archive_file(self.ledger, "clean", b"inside evidence\n", "e.txt", "artifact", ts=stamp(0))
        self.assertTrue(rec["path"].startswith("archive/"))
        res = self.ledger.verify("clean")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 1)

    def test_skip_archives(self):
        rec = gblog.archive_file(self.ledger, "ops", b"hello evidence\n", "e.txt", "artifact", ts=stamp(0))
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn("intact", out)
        full = os.path.join(self.root, rec["path"])
        with gzip.open(full, "wb") as fh:
            fh.write(b"something else")
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out)
        self.assertIn("content hash mismatch", out)
        for argv in (["verify", "--bot", "ops", "--skip-archives"], ["verify", "--all", "--skip-archives"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("(archives not checked)", out)
                self.assertNotIn("intact", out)


# --------------------------------------------------------------------------- I-21
class AnchorAndStrictTest(IntegrityBase):
    def _rollup(self):
        days = os.path.join(self.root, "anchors", "days")
        names = sorted(n for n in os.listdir(days) if n.endswith(".json"))
        self.assertEqual(len(names), 1, names)
        with open(os.path.join(days, names[0]), encoding="utf-8") as fh:
            return json.load(fh)

    def test_anchor_single_bot_keeps_rollup(self):
        self.chain("alpha", 2)
        self.chain("bravo", 2)
        with mock.patch.object(gblog, "now_iso", return_value="2026-03-05T12:00:00Z"):
            rc, out, err = run(["anchor"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(sorted(c["bot"] for c in self._rollup()["chains"]), ["alpha", "bravo"])
            newer = self.ledger.append("alpha", {"type": "note", "detail": "alpha record 3", "ts": stamp(10)})
            rc, out, err = run(["anchor", "--bot", "alpha"], self.root)
            self.assertEqual(rc, 0, out + err)
            rollup = self._rollup()
        by = {c["bot"]: c for c in rollup["chains"]}
        self.assertEqual(sorted(by), ["alpha", "bravo"])
        self.assertEqual(len(rollup["chains"]), 2)
        self.assertEqual(by["alpha"]["head"], newer["hash"])  # refreshed
        self.assertEqual(by["alpha"]["records"], 3)
        self.assertEqual(by["bravo"]["records"], 2)           # kept
        self.assertFalse(rollup["whole_ledger_skip"])

    def test_anchor_fail_line_without_descriptor(self):
        self.chain("ops", 2)
        path = self.log_path("ops")
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace("ops record 1", "ops record X")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("ANCHOR FAIL", out)
        self.assertIn("break -", out)
        self.assertNotIn("None", out)

    def test_bot_field_mismatch_warns_and_strict_fails(self):
        self.chain("alpha", 2)
        shutil.copytree(os.path.join(self.root, "logs", "alpha"), os.path.join(self.root, "logs", "bravo"))
        shutil.copyfile(self.state_path("alpha"), self.state_path("bravo"))
        res = self.ledger.verify("bravo")
        self.assertTrue(res["ok"], res["problems"])
        self.assertTrue(res.get("warnings"), res)
        self.assertTrue(any("bot" in w for w in res["warnings"]), res["warnings"])
        rc, out, err = run(["verify", "--bot", "bravo"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("      ! ", out)
        rc, out, err = run(["verify", "--bot", "bravo", "--strict"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL", out)
        # the genuine chain is clean in both modes
        self.assertEqual(self.ledger.verify("alpha").get("warnings"), [])
        rc, out, err = run(["verify", "--bot", "alpha", "--strict"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_duplicate_key_line_strict_fails(self):
        self.chain("ops", 2)
        path = self.log_path("ops")
        original = _lines(path)
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 0, out + err)

        # a duplicate key whose first value is shadowed: parses to the same record, hash still matches
        dup = '{"detail": "shadow", ' + original[0][1:]
        self.assertEqual(json.loads(dup), json.loads(original[0]))
        compact = json.dumps(json.loads(original[0]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertNotEqual(compact, original[0])
        for label, line in (("duplicate key", dup), ("reformatted", compact)):
            with self.subTest(case=label):
                _write(path, [line] + original[1:])
                rc, out, err = run(["verify", "--bot", "ops"], self.root)
                self.assertEqual(rc, 0, out + err)  # the default mode is unchanged
                rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("non-canonical line (duplicate keys or reformatted)", out)
        _write(path, original)
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_stray_jsonl_warns(self):
        self.chain("ops", 2)
        stray = os.path.join(self.root, "logs", "ops", "zz-backup.jsonl")
        with open(stray, "w", encoding="utf-8"):
            pass
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 2)
        self.assertTrue(any("zz-backup.jsonl" in w for w in res.get("warnings") or []), res)
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("      ! ", out)
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 1, out + err)
        os.remove(stray)
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 0, out + err)


if __name__ == "__main__":
    unittest.main()
