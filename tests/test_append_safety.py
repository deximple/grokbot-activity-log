"""Append-path safety tests for the post-audit ledger (audit items I-04, I-05, I-09, I-10, I-22).

Timestamp validation and monotonicity, interrupted-append recovery, state rebuild,
spool / alert tooling, state writes that must not brick a chain, reader locking and
file hygiene. Everything runs in temporary directories; no network is used.
"""
import datetime as _dt
import errno
import io
import json
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

TS_ERROR = "error: --ts must look like 2026-11-16T09:30:00Z"


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


def stamp(n, day="2024-06-01"):
    return "%sT10:%02d:%02dZ" % (day, n // 60, n % 60)


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _writer(root, tag, count):
    """Child process body for the parallel-append test (must be importable for the spawn context)."""
    ledger = gblog.Ledger(root)
    for i in range(count):
        ledger.append("ops", {"type": "note", "detail": "%s-%03d" % (tag, i)})


class SafetyBase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="gblog-append-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.work = os.path.join(self.base, "work")
        os.makedirs(self.work)
        self.ledger = gblog.Ledger(self.root)
        env = mock.patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GBLOG_TASK", None)
        os.environ.pop("GBLOG_ROOT", None)

    def seed(self, n=2, bot="ops"):
        return [self.ledger.append(bot, {"type": "note", "detail": "seed %d" % (i + 1), "ts": stamp(i)}) for i in range(n)]

    def state_path(self, bot="ops"):
        return os.path.join(self.root, "state", bot + ".json")

    def raw_state(self, bot="ops"):
        with open(self.state_path(bot), encoding="utf-8") as fh:
            return json.load(fh)

    def log_bytes(self, bot="ops"):
        out = b""
        for path in self.ledger.log_files(bot):
            with open(path, "rb") as fh:
                out += fh.read()
        return out

    def drop_last_line(self, bot="ops"):
        path = self.ledger.log_files(bot)[-1]
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines[:-1]))

    def break_state(self, bot="ops"):
        self.ledger.write_state(bot, {"seq": 99, "head": "cd" * 32})

    def pending_spool(self, bot="ops"):
        d = os.path.join(self.root, "spool", bot)
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.endswith(".json"))

    def status_row(self, bot="ops"):
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        return {r["bot"]: r for r in json.loads(out)["bots"]}[bot]


# --------------------------------------------------------------------------- I-04
class TimestampTest(SafetyBase):
    def test_ts_traversal_rejected(self):
        stray = "/tmp/x.jsonl"
        stray_existed = os.path.exists(stray)
        try:
            for bad in ("/tmp/x", "../../../a", "yesterday"):
                with self.subTest(ts=bad):
                    rc, out, err = run(["note", "--bot", "ops", "--detail", "x", "--ts", bad], self.root)
                    self.assertEqual(rc, 2, out + err)
                    self.assertIn(TS_ERROR, err)
                    self.assertNotIn("Traceback", err)
                    with self.assertRaises(ValueError):
                        gblog.parse_ts(bad)
                    with self.assertRaises(ValueError):
                        self.ledger.append("ops", {"type": "note", "detail": "x", "ts": bad})
            created = (not stray_existed) and os.path.exists(stray)
        finally:
            if not stray_existed and os.path.exists(stray):
                os.remove(stray)
        self.assertFalse(created, "an absolute --ts escaped the ledger root")
        # nothing was written anywhere
        self.assertEqual(self.ledger.log_files("ops"), [])
        self.assertFalse(os.path.exists(self.state_path()))
        self.assertFalse(os.path.exists(os.path.join(self.base, "a.jsonl")))

    def test_ts_offset_normalised(self):
        good = {
            "2026-03-01T09:30:00Z": "2026-03-01T09:30:00Z",
            "2026-03-01T09:30:00": "2026-03-01T09:30:00Z",
            "2026-03-01 09:30:00": "2026-03-01T09:30:00Z",
            "2026-03-01T09:30:00.123456Z": "2026-03-01T09:30:00Z",
            "2026-03-01T18:30:00+09:00": "2026-03-01T09:30:00Z",
            "2026-03-01T18:30:00+0900": "2026-03-01T09:30:00Z",
            "2026-03-01T04:30:00-05:00": "2026-03-01T09:30:00Z",
            "2026-03-01T03:00:00+09:00": "2026-02-28T18:00:00Z",
        }
        for raw, want in good.items():
            with self.subTest(ts=raw):
                self.assertEqual(gblog.parse_ts(raw), want)
        for bad in ("2026-02-30T00:00:00Z", "2026-03-01", "2026-03-01T25:00:00Z", "20260301T093000Z",
                    "2026-03-01T09:30:00Zjunk", "2026-03-01T09:30Z"):
            with self.subTest(ts=bad):
                with self.assertRaises(ValueError):
                    gblog.parse_ts(bad)

        # the normalised UTC value is what gets chained, and it picks the day file
        rc, out, err = run(["note", "--bot", "ops", "--detail", "offset", "--ts", "2024-06-01T03:00:00+09:00"], self.root)
        self.assertEqual(rc, 0, out + err)
        rec = self.ledger.records("ops")[-1]
        self.assertEqual(rec["ts"], "2024-05-31T18:00:00Z")
        self.assertEqual([os.path.basename(p) for p in self.ledger.log_files("ops")], ["2024-05-31.jsonl"])
        rc, out, err = run(["note", "--bot", "ops", "--detail", "space", "--ts", "2024-06-01 09:30:00"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.ledger.records("ops")[-1]["ts"], "2024-06-01T09:30:00Z")
        rec = self.ledger.append("ops", {"type": "note", "detail": "library", "ts": "2024-06-02T08:00:00+02:00"})
        self.assertEqual(rec["ts"], "2024-06-02T06:00:00Z")
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_explicit_past_ts_refused_chain_intact(self):
        self.ledger.append("ops", {"type": "note", "detail": "first", "ts": "2024-06-02T10:00:00Z"})
        before = self.log_bytes()
        self.assertTrue(issubclass(gblog.TimestampError, ValueError))
        with self.assertRaises(gblog.TimestampError):
            self.ledger.append("ops", {"type": "note", "detail": "late", "ts": "2024-06-01T10:00:00Z"})
        rc, out, err = run(["note", "--bot", "ops", "--detail", "late", "--ts", "2024-06-01T10:00:00Z"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is earlier than the last record of ops", err)
        self.assertIn("2024-06-02T10:00:00Z", err)
        self.assertIn("omit --ts", err)
        self.assertNotIn("Traceback", err)
        # the chain is byte-identical, no earlier day file appeared, and this is not a tail mismatch
        self.assertEqual(self.log_bytes(), before)
        self.assertEqual([os.path.basename(p) for p in self.ledger.log_files("ops")], ["2024-06-02.jsonl"])
        self.assertEqual(self.raw_state()["seq"], 1)
        self.assertEqual(self.pending_spool(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "ops.alert.json")))
        # an equal timestamp is not "earlier"; and plain appends keep working
        rec = self.ledger.append("ops", {"type": "note", "detail": "same second", "ts": "2024-06-02T10:00:00Z"})
        self.assertEqual(rec["seq"], 2)
        rec = self.ledger.append("ops", {"type": "note", "detail": "now"})
        self.assertEqual(rec["seq"], 3)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])

    def test_future_ts_refused_by_cli(self):
        for ts in (_iso(_utc_now() + _dt.timedelta(hours=1)), "2099-01-01T00:00:00Z"):
            with self.subTest(ts=ts):
                rc, out, err = run(["note", "--bot", "ops", "--detail", "future", "--ts", ts], self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.ledger.log_files("ops"), [])
        # a small clock skew is tolerated
        rc, out, err = run(["note", "--bot", "ops", "--detail", "skew", "--ts", _iso(_utc_now() + _dt.timedelta(seconds=60))], self.root)
        self.assertEqual(rc, 0, out + err)
        # the library itself has no future bound (fixtures and tests use fixed dates)
        rec = self.ledger.append("lib", {"type": "note", "detail": "fixed date", "ts": "2099-01-01T00:00:00Z"})
        self.assertEqual(rec["ts"], "2099-01-01T00:00:00Z")

    def test_clock_regression_clamped_and_flagged(self):
        self.ledger.append("ops", {"type": "note", "detail": "before the step", "ts": "2026-03-01T10:00:00Z"})
        with mock.patch.object(gblog, "now_iso", return_value="2026-03-01T09:00:00Z"):
            rec = self.ledger.append("ops", {"type": "note", "detail": "clock stepped back"})
        self.assertEqual(rec["ts"], "2026-03-01T10:00:00Z")
        self.assertEqual(rec["clock_regressed"], "2026-03-01T09:00:00Z")
        with mock.patch.object(gblog, "now_iso", return_value="2026-03-01T11:00:00Z"):
            rec = self.ledger.append("ops", {"type": "note", "detail": "clock fine again"})
        self.assertEqual(rec["ts"], "2026-03-01T11:00:00Z")
        self.assertNotIn("clock_regressed", rec)
        # a step back across midnight must not open an earlier day file
        with mock.patch.object(gblog, "now_iso", return_value="2026-02-28T23:59:59Z"):
            rec = self.ledger.append("ops", {"type": "note", "detail": "stepped back a day"})
        self.assertEqual(rec["ts"], "2026-03-01T11:00:00Z")
        self.assertEqual(rec["clock_regressed"], "2026-02-28T23:59:59Z")
        self.assertEqual([os.path.basename(p) for p in self.ledger.log_files("ops")], ["2026-03-01.jsonl"])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 4)

    def test_archive_ts_traversal_rejected(self):
        for bad in ("../../../a", "yesterday"):
            with self.subTest(ts=bad):
                with self.assertRaises(ValueError):
                    gblog.archive_file(self.ledger, "ops", b"evidence", "f.txt", ts=bad)
        self.assertFalse(os.path.exists(os.path.join(self.base, "a")))
        stored = []
        for dirpath, _dirs, files in os.walk(os.path.join(self.root, "archive")):
            stored.extend(os.path.join(dirpath, f) for f in files)
        self.assertEqual(stored, [])
        self.assertEqual(self.ledger.log_files("ops"), [])
        src = os.path.join(self.work, "f.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("evidence\n")
        rc, out, err = run(["archive", "--bot", "ops", "--file", src, "--ts", "../../../a"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(TS_ERROR, err)
        self.assertEqual(self.ledger.log_files("ops"), [])
        # a valid --ts still files the archive under its UTC day
        rc, out, err = run(["archive", "--bot", "ops", "--file", src, "--ts", "2024-06-01T10:00:00Z"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("archive/ops/2024-06-01/", self.ledger.records("ops")[-1]["path"])

    def test_report_date_validation(self):
        self.ledger.append("ops", {"type": "task_start", "task_id": "t1", "task": "Dated task", "ts": stamp(0)})
        self.ledger.append("ops", {"type": "task_end", "task_id": "t1", "outcome": "ok", "ts": stamp(1)})
        bad = (
            ["report", "--date", "2024-6-1", "--stdout"],
            ["report", "--date", "../x", "--stdout"],
            ["report", "--date", "../x"],
            ["report", "--date", "tomorrow", "--stdout"],
            ["report", "--since", "nope", "--stdout"],
            ["export", "--since", "20240601"],
            ["coverage", "--days", "0"],
            # `coverage --days N` above 36500 is clamped with a notice (exit 0) for backward
            # compatibility with an existing tested invocation; tune rejects it.
            ["tune", "--days", "36501", "--stdout"],
            ["tune", "--days", "0", "--stdout"],
            ["tune", "--days", "-3", "--stdout"],
        )
        for argv in bad:
            with self.subTest(argv=argv):
                rc, out, err = run(list(argv), self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.md")))
        good = (
            ["report", "--date", "2024-06-01", "--stdout"],
            ["report", "--date", "today", "--stdout"],
            ["report", "--since", "2024-06-01", "--stdout"],
            ["export", "--since", "2024-06-01"],
            ["coverage", "--days", "36500"],
            ["tune", "--days", "28", "--stdout"],
        )
        for argv in good:
            with self.subTest(argv=argv):
                rc, out, err = run(list(argv), self.root)
                self.assertEqual(rc, 0, out + err)
        rc, out, _ = run(["report", "--date", "2024-06-01", "--stdout"], self.root)
        self.assertIn("### Dated task", out)
        y1 = (_utc_now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        rc, out, err = run(["report", "--date", "yesterday", "--stdout"], self.root)
        y2 = (_utc_now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"Activity report \d{4}-\d{2}-\d{2}")
        self.assertTrue(any("Activity report %s" % d in out for d in (y1, y2)), out[:80])


# --------------------------------------------------------------------------- I-05
class RecoveryTest(SafetyBase):
    def _flaky_write_state(self):
        """write_state fails once with ENOSPC (after the log line is already durable), then works."""
        orig = gblog.Ledger.write_state
        calls = {"n": 0}

        def flaky(ledger, bot, state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.path.join(ledger.state_dir, bot + ".json"))
            return orig(ledger, bot, state)

        return mock.patch.object(gblog.Ledger, "write_state", flaky)

    def test_state_one_behind_rolls_forward(self):
        self.ledger.append("ops", {"type": "note", "detail": "one", "ts": stamp(0)})
        with self._flaky_write_state():
            with self.assertRaises(OSError):
                self.ledger.append("ops", {"type": "note", "detail": "two (line durable, state not replaced)", "ts": stamp(1)})
        lines = self.log_bytes().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.ledger.read_state("ops")["seq"], 1)
        err = io.StringIO()
        with redirect_stderr(err):
            third = self.ledger.append("ops", {"type": "note", "detail": "three", "ts": stamp(2)})
        self.assertIn("notice: recovered state/ops.json after an interrupted append", err.getvalue())
        self.assertEqual(third["seq"], 3)
        self.assertEqual(third["prev"], json.loads(lines[1])["hash"])
        st = self.raw_state()
        self.assertEqual((st["seq"], st["head"]), (3, third["hash"]))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)
        self.assertEqual(self.pending_spool(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "ops.alert.json")))

    def test_enospc_in_write_state_recovers(self):
        rc, out, err = run(["note", "--bot", "ops", "--detail", "one"], self.root)
        self.assertEqual(rc, 0, out + err)
        with self._flaky_write_state():
            rc, out, err = run(["note", "--bot", "ops", "--detail", "two"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error:", err)
        self.assertIn(os.strerror(errno.ENOSPC), err)
        self.assertNotIn("Traceback", err)
        # the next append is not a tail mismatch (exit 4): the durable line is adopted
        rc, out, err = run(["note", "--bot", "ops", "--detail", "three"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: recovered state/ops.json", err)
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        details = [rec["detail"] for _p, _n, rec in self.ledger.iter_records("ops")]
        self.assertEqual(details, ["one", "two", "three"])
        self.assertIsNone(self.status_row().get("alert"))

    def _assert_refused(self, bot):
        before = self.log_bytes(bot)
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            self.ledger.append(bot, {"type": "note", "detail": "must be spooled", "ts": stamp(30)})
        self.assertEqual(self.log_bytes(bot), before)
        self.assertTrue(os.path.isfile(ctx.exception.spool_path))
        self.assertTrue(os.path.isfile(ctx.exception.alert_path))

    def test_state_ahead_still_refused(self):
        # state far ahead with a head that is not in the file
        self.seed(2, "ahead")
        self.break_state("ahead")
        self._assert_refused("ahead")
        # the log lost its last line: state is one ahead of the file (the kim-biseo shape)
        self.seed(3, "trunc")
        self.drop_last_line("trunc")
        self._assert_refused("trunc")
        # state one behind by seq, but its head is not the tail's prev: not an interrupted append
        self.seed(2, "wronghead")
        self.ledger.write_state("wronghead", {"seq": 1, "head": "ab" * 32})
        self._assert_refused("wronghead")
        # state two behind: only a single durable-but-unrecorded line may be rolled forward
        recs = self.seed(3, "twobehind")
        self.ledger.write_state("twobehind", {"seq": 1, "head": recs[0]["hash"]})
        self._assert_refused("twobehind")

    def test_state_rebuild_missing_and_genesis(self):
        recs = self.seed(3)
        os.remove(self.state_path())
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rebuilt ops seq", out)
        items = [rec for _p, _n, rec in self.ledger.iter_records("ops")]
        self.assertEqual(len(items), 4)
        note = items[-1]
        self.assertEqual((note["type"], note.get("source"), note["prev"]), ("note", "auto", recs[-1]["hash"]))
        self.assertTrue(note["detail"].startswith("state rebuilt from log tail"), note["detail"])
        st = self.raw_state()
        self.assertEqual((st["seq"], st["head"]), (4, note["hash"]))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("      ! ", out)

        # state reset to genesis: rebuilt, other keys preserved
        last_sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234", "pushed": False}
        self.ledger.write_state("ops", {"seq": 0, "head": gblog.GENESIS, "last_sync": last_sync})
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        st = self.raw_state()
        self.assertEqual(st["seq"], 5)
        self.assertEqual(st.get("last_sync"), last_sync)

        # state behind, pointing at a record that exists
        self.ledger.write_state("ops", {"seq": 2, "head": recs[1]["hash"]})
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.raw_state()["seq"], 6)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        rec = self.ledger.append("ops", {"type": "note", "detail": "after rebuild"})
        self.assertEqual(rec["seq"], 7)

    def test_state_rebuild_refuses_truncation(self):
        self.seed(3)
        self.drop_last_line()
        with open(self.state_path(), "rb") as fh:
            state_before = fh.read()
        log_before = self.log_bytes()
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("restore the log from backup", err)
        self.assertNotIn("Traceback", err)
        with open(self.state_path(), "rb") as fh:
            self.assertEqual(fh.read(), state_before)
        self.assertEqual(self.log_bytes(), log_before)

        # a head that is nowhere in the file is state ahead of the log as well
        self.break_state()
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("restore the log from backup", err)
        self.assertEqual(self.raw_state()["seq"], 99)
        self.assertEqual(self.log_bytes(), log_before)

        # a chain that does not walk cleanly is never rebuilt, even with state gone
        self.seed(3, "bad")
        path = self.ledger.log_files("bad")[0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace("seed 2", "seed X")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.remove(self.state_path("bad"))
        bad_before = self.log_bytes("bad")
        rc, out, err = run(["state", "rebuild", "--bot", "bad"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(os.path.exists(self.state_path("bad")))
        self.assertEqual(self.log_bytes("bad"), bad_before)

    def test_spool_list_and_replay_clears_alert(self):
        self.seed(2)
        self.break_state()
        rc, _out, _err = run(["note", "--bot", "ops", "--detail", "first refused"], self.root)
        self.assertEqual(rc, 4)
        rc, _out, _err = run(["log", "--bot", "ops", "--action", "shell", "--target", "ls -la", "--detail", "second refused"], self.root)
        self.assertEqual(rc, 4)
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("ops", {"type": "note", "detail": "third refused", "ts": "2024-06-01T12:00:00Z"})
        wanted = {"first refused", "second refused", "third refused"}
        pending = self.pending_spool()
        self.assertEqual(len(pending), 3)
        alert = os.path.join(self.root, "state", "ops.alert.json")
        self.assertTrue(os.path.isfile(alert))
        log_before = self.log_bytes()

        for argv in (["spool", "list"], ["spool", "list", "--bot", "ops"]):
            rc, out, err = run(argv, self.root)
            self.assertEqual(rc, 0, out + err)
            hits = [line for line in out.splitlines() if "refused" in line]
            self.assertEqual(len(hits), 3, out)
            for text in wanted:
                self.assertIn(text, out)
            self.assertTrue(all("ops" in line for line in hits), out)
        rc, out, err = run(["spool", "list", "--bot", "other"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("refused", out)

        # replay while the tail still mismatches: refused again, nothing is consumed
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertNotIn("Traceback", err)
        still = self.pending_spool()
        for name in pending:
            self.assertIn(name, still)
        self.assertTrue(os.path.isfile(alert))
        self.assertEqual(self.log_bytes(), log_before)

        # the operator restores a state that matches the tail; replay then drains the spool
        seq, head = self.ledger._tail_head("ops")
        self.ledger.write_state("ops", {"seq": seq, "head": head})
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.pending_spool(), [])
        replayed_dir = os.path.join(self.root, "spool", "ops", "replayed")
        moved = os.listdir(replayed_dir)
        for name in pending:
            self.assertIn(name, moved)
        self.assertFalse(os.path.exists(alert))
        resolved = [f for f in os.listdir(os.path.join(self.root, "state"))
                    if f.startswith("ops.alert.resolved-") and f.endswith(".json")]
        self.assertTrue(resolved)

        replayed = [rec for _p, _n, rec in self.ledger.iter_records("ops") if rec.get("spool_id")]
        self.assertEqual({r["detail"] for r in replayed}, wanted)
        self.assertTrue(all(r.get("spooled_at") for r in replayed))
        third = [r for r in replayed if r["detail"] == "third refused"][0]
        self.assertEqual(third["original_ts"], "2024-06-01T12:00:00Z")
        self.assertNotEqual(third["ts"], "2024-06-01T12:00:00Z")
        second = [r for r in replayed if r["detail"] == "second refused"][0]
        self.assertEqual((second["type"], second["action"], second["target"]), ("action", "shell", "ls -la"))
        self.assertTrue(self.log_bytes().startswith(log_before))  # append-only
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, _ = run(["spool", "list"], self.root)
        self.assertNotIn("refused", out)
        row = self.status_row()
        self.assertIsNone(row["alert"])
        self.assertEqual(row["spooled"], 0)

    def test_status_reports_alert(self):
        self.seed(2)
        row = self.status_row()
        self.assertIsNone(row["alert"])
        self.assertEqual(row["spooled"], 0)
        rc, out, _ = run(["status"], self.root)
        self.assertEqual(rc, 0)
        self.assertNotIn("ALERT", out)

        self.break_state()
        rc, _out, _err = run(["note", "--bot", "ops", "--detail", "refused"], self.root)
        self.assertEqual(rc, 4)
        row = self.status_row()
        self.assertTrue(row["alert"])
        self.assertIsInstance(row["alert"], str)
        self.assertEqual(row["spooled"], 1)
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ALERT ops: tail mismatch since", out)
        self.assertIn("(1 spooled)", out)


# --------------------------------------------------------------------------- I-09
class StateWriteTest(SafetyBase):
    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_sync_does_not_brick_chain_without_state(self):
        recs = self.seed(2)
        os.remove(self.state_path())
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        st = self.raw_state()
        # never a genesis state over a non-empty chain: seq/head are seeded from the tail
        self.assertEqual((st["seq"], st["head"]), (2, recs[1]["hash"]))
        self.assertIn("last_sync", st)
        rec = self.ledger.append("ops", {"type": "note", "detail": "after sync"})
        self.assertEqual((rec["seq"], rec["prev"]), (3, recs[1]["hash"]))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "ops.alert.json")))

    def _chrome_history(self, visits):
        prof = os.path.join(self.base, "chrome", "Default")
        os.makedirs(prof)
        hist = os.path.join(prof, "History")
        now_webkit = int((time.time() + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
        conn = sqlite3.connect(hist)
        try:
            conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                               "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
            for i, (url, age_seconds) in enumerate(visits, 1):
                conn.execute("INSERT INTO urls VALUES (?, ?, ?)", (i, url, "title %d" % i))
                conn.execute("INSERT INTO visits VALUES (?, ?, ?)", (i, i, now_webkit - age_seconds * 1_000_000))
            conn.commit()
        finally:
            conn.close()
        return hist

    def test_capture_marker_write_without_state(self):
        recs = self.seed(2)
        os.remove(self.state_path())
        # one recent non-http visit: nothing is appended, but the per-history marker advances
        hist = self._chrome_history([("chrome://newtab/", 60)])
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual(res["added"], 0)
        self.assertFalse(res.get("failed"))
        st = self.raw_state()
        self.assertEqual((st["seq"], st["head"]), (2, recs[1]["hash"]))
        self.assertIsInstance(st.get("capture_browser_last"), dict)
        rec = self.ledger.append("ops", {"type": "note", "detail": "after capture"})
        self.assertEqual((rec["seq"], rec["prev"]), (3, recs[1]["hash"]))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])


# --------------------------------------------------------------------------- I-10
@unittest.skipIf(gblog.fcntl is None, "flock is not available on this platform")
class LockingTest(SafetyBase):
    def test_parallel_appends_and_verify(self):
        self.ledger.init()
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_writer, args=(self.root, "w%d" % n, 50)) for n in range(4)]
        for p in procs:
            p.start()
        failures = []
        rounds = 0
        deadline = time.time() + 300
        try:
            while any(p.is_alive() for p in procs) and time.time() < deadline:
                res = self.ledger.verify("ops")
                rounds += 1
                if not res["ok"]:
                    failures.append(res["problems"])
                time.sleep(0.01)
            for p in procs:
                p.join(timeout=60)
        finally:
            for p in procs:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=10)
        self.assertEqual([p.exitcode for p in procs], [0, 0, 0, 0])
        self.assertGreater(rounds, 0)
        self.assertEqual(failures, [])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 200)
        self.assertEqual(self.ledger.read_state("ops")["seq"], 200)
        details = sorted(rec["detail"] for _p, _n, rec in self.ledger.iter_records("ops"))
        self.assertEqual(details, sorted("w%d-%03d" % (n, i) for n in range(4) for i in range(50)))

    def _assert_lock_free(self, bot="ops"):
        path = os.path.join(self.root, "state", bot + ".lock")
        with open(path, "a+") as fh:
            try:
                gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_EX | gblog.fcntl.LOCK_NB)
            except OSError:
                self.fail("verify returned while still holding state/%s.lock" % bot)
            gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_UN)

    def test_verify_releases_lock(self):
        self.seed(2)
        gblog.archive_file(self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(5))
        self.assertTrue(self.ledger.verify("ops")["ok"])
        self._assert_lock_free()
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out + err)
        self._assert_lock_free()
        # seal_break calls verify and then append: an append straight after verify must not block
        rec = self.ledger.append("ops", {"type": "note", "detail": "after verify", "ts": stamp(6)})
        self.assertEqual(rec["seq"], 4)
        # the FAIL path releases it as well
        path = self.ledger.log_files("ops")[0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace("seed 1", "seed X")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertFalse(self.ledger.verify("ops")["ok"])
        self._assert_lock_free()

    def test_verify_waits_for_writer_and_shares_with_readers(self):
        self.seed(2)
        lock_path = os.path.join(self.root, "state", "ops.lock")
        result = {}

        def target():
            result["res"] = self.ledger.verify("ops")

        # a writer holds the exclusive lock: verify must wait for it
        with open(lock_path, "a+") as fh:
            gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_EX)
            try:
                t = threading.Thread(target=target, daemon=True)
                t.start()
                t.join(0.5)
                blocked = t.is_alive()
            finally:
                gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_UN)
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertTrue(blocked, "verify did not wait for the writer's exclusive lock")
        self.assertTrue(result["res"]["ok"], result["res"])

        # another reader holds a shared lock: verify must not wait
        result.clear()
        with open(lock_path, "a+") as fh:
            gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_SH)
            try:
                t = threading.Thread(target=target, daemon=True)
                t.start()
                t.join(10)
                finished = not t.is_alive()
            finally:
                gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_UN)
        t.join(30)
        self.assertTrue(finished, "verify blocked on another reader's shared lock")
        self.assertTrue(result["res"]["ok"], result["res"])


# --------------------------------------------------------------------------- I-22
class HygieneTest(SafetyBase):
    def test_append_after_tail_without_newline(self):
        recs = self.seed(2)
        path = self.ledger.log_files("ops")[0]
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.endswith(b"\n"))
        with open(path, "wb") as fh:
            fh.write(data[:-1])  # an editor or a manual restore dropped the final newline
        rec = self.ledger.append("ops", {"type": "note", "detail": "three", "ts": stamp(2)})
        self.assertEqual((rec["seq"], rec["prev"]), (3, recs[1]["hash"]))
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(data[:-1]))  # earlier bytes are never rewritten
        self.assertTrue(raw.endswith(b"\n"))
        lines = [line for line in raw.split(b"\n") if line.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIsInstance(json.loads(line.decode("utf-8")), dict)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_new_files_are_private(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.ledger.append("ops", {"type": "note", "detail": "one", "ts": stamp(0)})
        log = self.ledger.log_files("ops")[0]
        bot_dir = os.path.dirname(log)

        def mode(path):
            return stat.S_IMODE(os.stat(path).st_mode)

        for path in (log, self.state_path()):
            self.assertEqual(mode(path) & 0o077, 0, "%s is %o" % (path, mode(path)))
        for path in (bot_dir, os.path.join(self.root, "logs"), os.path.join(self.root, "state")):
            self.assertEqual(mode(path) & 0o077, 0, "%s is %o" % (path, mode(path)))
        # files and directories that already exist are left as they are
        os.chmod(log, 0o644)
        os.chmod(bot_dir, 0o755)
        self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual(mode(log), 0o644)
        self.assertEqual(mode(bot_dir), 0o755)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_implicit_init_notice(self):
        rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: initialized new ledger at %s" % self.ledger.root, err)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "y"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("initialized new ledger", err)
        # an explicit `gblog init` is not an implicit one
        other = os.path.join(self.base, "explicit")
        rc, out, err = run(["init"], other)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("initialized", out)
        self.assertNotIn("notice: initialized new ledger", err)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], other)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("initialized new ledger", err)

    def test_gitignore_written_once(self):
        rc, out, err = run(["init"], self.root)
        self.assertEqual(rc, 0, out + err)
        path = os.path.join(self.root, ".gitignore")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as fh:
            lines = [line.strip() for line in fh.read().splitlines()]
        for pattern in ("state/*.lock", "*.tmp", "state/capture-files-*.json"):
            self.assertIn(pattern, lines)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("my-own-line\n")
        with open(path, "rb") as fh:
            before = fh.read()
        rc, out, err = run(["init"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
