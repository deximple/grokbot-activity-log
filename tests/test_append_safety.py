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


def _anchorer(root, stop_path):
    """Child process body: anchor the chain over and over while the writers run (importable for spawn)."""
    ledger = gblog.Ledger(root)
    deadline = time.time() + 240
    while not os.path.exists(stop_path) and time.time() < deadline:
        ledger.anchor(["ops"])
        time.sleep(0.005)


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
        # the printed seq and head are what state holds after the note (R-13)
        self.assertIn("rebuilt ops seq %d head %s" % (st["seq"], st["head"][:12]), out)
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
        self.assertIn("rebuilt ops seq %d head %s" % (st["seq"], st["head"][:12]), out)
        self.assertEqual(st.get("last_sync"), last_sync)

        # state behind, pointing at a record that exists
        self.ledger.write_state("ops", {"seq": 2, "head": recs[1]["hash"]})
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        st = self.raw_state()
        self.assertEqual(st["seq"], 6)
        self.assertIn("rebuilt ops seq %d head %s" % (st["seq"], st["head"][:12]), out)
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

    def test_parallel_appends_anchor_and_verify(self):
        """Writers, a process that anchors in a loop, and a verifier: no spurious 'anchored head' FAIL (R-05)."""
        self.ledger.init()
        self.ledger.append("ops", {"type": "note", "detail": "seed"})
        stop = os.path.join(self.base, "stop-anchoring")
        ctx = multiprocessing.get_context("spawn")
        writers = [ctx.Process(target=_writer, args=(self.root, "a%d" % n, 40)) for n in range(3)]
        anchorer = ctx.Process(target=_anchorer, args=(self.root, stop))
        procs = writers + [anchorer]
        for p in procs:
            p.start()
        failures = []
        rounds = 0
        deadline = time.time() + 300
        try:
            while any(p.is_alive() for p in writers) and time.time() < deadline:
                res = self.ledger.verify("ops")
                rounds += 1
                if not res["ok"]:
                    failures.append(res["problems"])
                time.sleep(0.01)
            for p in writers:
                p.join(timeout=60)
        finally:
            with open(stop, "w", encoding="utf-8"):
                pass
            anchorer.join(timeout=60)
            for p in procs:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=10)
        self.assertEqual([p.exitcode for p in procs], [0, 0, 0, 0])
        self.assertGreater(rounds, 0)
        self.assertEqual(failures, [])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 121)
        hashes = {rec.get("hash") for _p, _n, rec in self.ledger.iter_records("ops")}
        hist = os.path.join(self.root, "anchors", "ops.history.jsonl")
        self.assertTrue(os.path.isfile(hist))
        with open(hist, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(entries)
        for entry in entries:
            for head in entry["heads"]:
                self.assertIn(head, hashes)

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

        # R-14: lock files, anchors, spool and alert files are private as well
        self.ledger.anchor(["ops"])
        self.seed(2, "spooly")
        self.break_state("spooly")
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("spooly", {"type": "note", "detail": "refused", "ts": stamp(5)})
        pending = self.pending_spool("spooly")
        self.assertEqual(len(pending), 1)
        state_dir = os.path.join(self.root, "state")
        anchors = os.path.join(self.root, "anchors")
        days = os.path.join(anchors, "days")
        spool = os.path.join(self.root, "spool", "spooly")
        private = [
            os.path.join(state_dir, "ops.lock"),
            os.path.join(state_dir, "spooly.lock"),
            os.path.join(state_dir, "spooly.alert.json"),
            anchors,
            days,
            os.path.join(anchors, "ops.json"),
            os.path.join(anchors, "ops.history.jsonl"),
            os.path.join(self.root, "spool"),
            spool,
            os.path.join(spool, pending[0]),
        ] + [os.path.join(days, name) for name in os.listdir(days)]
        for path in private:
            self.assertTrue(os.path.exists(path), path)
            self.assertEqual(mode(path) & 0o077, 0, "%s is %o" % (path, mode(path)))
        # ... and so is what a replay leaves behind
        seq, head = self.ledger._tail_head("spooly")
        self.ledger.write_state("spooly", {"seq": seq, "head": head})
        res = self.ledger.spool_replay("spooly")
        self.assertEqual(len(res["replayed"]), 1)
        replayed = os.path.join(spool, "replayed")
        resolved = [os.path.join(state_dir, f) for f in os.listdir(state_dir) if f.startswith("spooly.alert.resolved-")]
        self.assertTrue(resolved)
        for path in [replayed, os.path.join(replayed, pending[0])] + resolved:
            self.assertTrue(os.path.exists(path), path)
            self.assertEqual(mode(path) & 0o077, 0, "%s is %o" % (path, mode(path)))

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


# --------------------------------------------------------------------------- round 2 (R-03, R-07, R-13)
FUTURE = "2099-01-01T00:00:00Z"


class FutureTailTest(SafetyBase):
    def test_future_tail_warns_in_verify_and_status(self):
        self.seed(2, "calm")
        self.ledger.append("skew", {"type": "note", "detail": "one minute ahead", "ts": "2024-06-01T12:01:00Z"})
        self.ledger.append("ops", {"type": "note", "detail": "written while the clock said 2099", "ts": FUTURE})
        warning = "last record is dated %s, in the future: new records are stamped with that time (see clock_regressed)" % FUTURE
        with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T12:00:00Z"):
            res = self.ledger.verify("ops")
            self.assertTrue(res["ok"], res["problems"])  # a warning, never a FAIL
            self.assertIn(warning, res["warnings"])
            for bot in ("calm", "skew"):  # the past, and a skew inside the 300 s tolerance
                self.assertFalse([w for w in self.ledger.verify(bot)["warnings"] if "in the future" in w], bot)
            rc, out, err = run(["verify", "--all"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("      ! " + warning, out)
            self.assertEqual(out.count("in the future"), 1, out)

            rc, out, err = run(["status", "--json"], self.root)
            self.assertEqual(rc, 0, out + err)
            rows = {r["bot"]: r for r in json.loads(out)["bots"]}
            self.assertEqual(rows["ops"]["future_tail"], FUTURE)
            self.assertIsNone(rows["calm"]["future_tail"])
            self.assertIsNone(rows["skew"]["future_tail"])
            rc, out, err = run(["status"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("CLOCK ops: last record dated %s (future)" % FUTURE, out)
            self.assertNotIn("CLOCK calm", out)
            self.assertNotIn("CLOCK skew", out)

    def test_clamp_warns_when_tail_is_future(self):
        self.ledger.append("ops", {"type": "note", "detail": "future-dated", "ts": FUTURE})
        warning = "warning: last record of ops is dated %s (future); this record is stamped with it" % FUTURE
        err = io.StringIO()
        with mock.patch.object(gblog, "now_iso", return_value="2026-03-01T09:00:00Z"), redirect_stderr(err):
            rec = self.ledger.append("ops", {"type": "note", "detail": "after the future record"})
        self.assertEqual(rec["ts"], FUTURE)  # the clamp rule itself is unchanged
        self.assertEqual(rec["clock_regressed"], "2026-03-01T09:00:00Z")
        self.assertIn(warning, err.getvalue())
        # the same through the CLI; the exit code does not change
        with mock.patch.object(gblog, "now_iso", return_value="2026-03-01T09:00:05Z"):
            rc, out, cli_err = run(["note", "--bot", "ops", "--detail", "cli after the future record"], self.root)
        self.assertEqual(rc, 0, out + cli_err)
        self.assertIn(warning, cli_err)
        self.assertEqual([os.path.basename(p) for p in self.ledger.log_files("ops")], ["2099-01-01.jsonl"])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

        # a small step back (inside the 300 s tolerance) is clamped quietly
        self.ledger.append("small", {"type": "note", "detail": "before the step", "ts": "2026-03-01T10:00:00Z"})
        err = io.StringIO()
        with mock.patch.object(gblog, "now_iso", return_value="2026-03-01T09:58:00Z"), redirect_stderr(err):
            rec = self.ledger.append("small", {"type": "note", "detail": "two minutes back"})
        self.assertEqual(rec["ts"], "2026-03-01T10:00:00Z")
        self.assertEqual(rec["clock_regressed"], "2026-03-01T09:58:00Z")
        self.assertNotIn("warning: last record of small", err.getvalue())


class UnreadableStateTest(SafetyBase):
    def test_unreadable_state_adopts_tail_like_missing(self):
        recs = self.seed(2)
        last, n = recs[-1], 2
        cases = (("missing", None, "missing"), ("unparsable", "{not json", "unreadable"), ("not an object", "[1, 2]", "unreadable"))
        for label, content, word in cases:
            with self.subTest(state=label):
                if content is None:
                    os.remove(self.state_path())
                else:
                    with open(self.state_path(), "w", encoding="utf-8") as fh:
                        fh.write(content)
                err = io.StringIO()
                with redirect_stderr(err):
                    rec = self.ledger.append("ops", {"type": "note", "detail": "after %s state" % label, "ts": stamp(n)})
                self.assertEqual((rec["seq"], rec["prev"]), (n + 1, last["hash"]))
                self.assertIn("notice: state/ops.json was %s; adopted the log tail (seq %d)" % (word, n), err.getvalue())
                st = self.raw_state()
                self.assertEqual((st["seq"], st["head"]), (n + 1, rec["hash"]))
                last, n = rec, n + 1

        # the CLI: exit 0 with the notice, never exit 4
        with open(self.state_path(), "w", encoding="utf-8") as fh:
            fh.write('"x"')
        rc, out, err = run(["note", "--bot", "ops", "--detail", "cli after unreadable state"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: state/ops.json was unreadable; adopted the log tail (seq %d)" % n, err)
        self.assertEqual(self.pending_spool(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "ops.alert.json")))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], n + 1)

        # sync / capture go through update_state: same rule, same notice
        synced = self.seed(2, "synced")
        with open(self.state_path("synced"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        last_sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234", "pushed": False}
        err = io.StringIO()
        with redirect_stderr(err):
            wrote = self.ledger.update_state("synced", {"last_sync": last_sync})
        self.assertTrue(wrote)
        st = self.raw_state("synced")
        self.assertEqual((st["seq"], st["head"]), (2, synced[1]["hash"]))
        self.assertEqual(st["last_sync"], last_sync)
        self.assertIn("notice: state/synced.json was unreadable; adopted the log tail (seq 2)", err.getvalue())

        # an unreadable state over a corrupt tail is still refused
        self.seed(2, "torn")
        with open(self.ledger.log_files("torn")[-1], "ab") as fh:
            fh.write(b'{"torn": \n')
        with open(self.state_path("torn"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        before = self.log_bytes("torn")
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("torn", {"type": "note", "detail": "must be spooled", "ts": stamp(9)})
        self.assertEqual(self.log_bytes("torn"), before)


class RecoveryPolishTest(SafetyBase):
    def _archive_files(self):
        found = []
        for dirpath, _dirs, files in os.walk(os.path.join(self.root, "archive")):
            found.extend(os.path.join(dirpath, f) for f in files)
        return sorted(found)

    def test_archive_past_ts_leaves_no_file(self):
        self.ledger.append("ops", {"type": "note", "detail": "first", "ts": "2024-06-02T10:00:00Z"})
        before = self.log_bytes()
        with self.assertRaises(gblog.TimestampError):
            gblog.archive_file(self.ledger, "ops", b"late evidence\n", "late.txt", ts="2024-06-01T10:00:00Z")
        self.assertEqual(self._archive_files(), [])
        self.assertEqual(self.log_bytes(), before)

        src = os.path.join(self.work, "late.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("late evidence from the CLI\n")
        rc, out, err = run(["archive", "--bot", "ops", "--file", src, "--ts", "2024-06-01T10:00:00Z"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("earlier than the last record", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self._archive_files(), [])
        self.assertEqual(self.log_bytes(), before)

        # an append that fails for another reason does not leave an unreferenced file behind either
        boom = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(gblog.Ledger, "append", side_effect=boom):
            with self.assertRaises(OSError):
                gblog.archive_file(self.ledger, "ops", b"never chained\n", "x.txt", ts="2024-06-02T10:00:01Z")
        self.assertEqual(self._archive_files(), [])

        # a tail mismatch keeps the file: the spooled event still refers to it
        self.seed(2, "held")
        self.break_state("held")
        with self.assertRaises(gblog.TailMismatchError):
            gblog.archive_file(self.ledger, "held", b"spooled evidence\n", "held.txt")
        kept = self._archive_files()
        self.assertEqual(len(kept), 1, kept)
        self.assertTrue(kept[0].endswith(".gz"), kept)

        # a timestamp that is not earlier than the tail still archives normally
        rec = gblog.archive_file(self.ledger, "ops", b"on time\n", "ok.txt", ts="2024-06-02T10:00:00Z")
        self.assertTrue(os.path.isfile(os.path.join(self.root, rec["path"])))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 1)

    def test_state_rebuild_noop_on_healthy_chain(self):
        self.seed(3)
        with open(self.state_path(), "rb") as fh:
            state_before = fh.read()
        log_before = self.log_bytes()
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("state/ops.json already matches the log tail; nothing to do", out + err)
        self.assertNotIn("rebuilt", out)
        self.assertEqual(self.log_bytes(), log_before)  # no note appended
        with open(self.state_path(), "rb") as fh:
            self.assertEqual(fh.read(), state_before)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    def test_status_alert_without_logs_dir(self):
        self.seed(2)
        self.seed(1, "keeper")
        self.break_state()
        rc, _out, _err = run(["note", "--bot", "ops", "--detail", "refused"], self.root)
        self.assertEqual(rc, 4)
        shutil.rmtree(os.path.join(self.root, "logs", "ops"))

        def check(spooled):
            rc, out, err = run(["status"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("Traceback", err)
            self.assertIn("ALERT ops: tail mismatch since", out)
            self.assertIn("(%d spooled)" % spooled, out)
            self.assertIn("keeper", out)

        check(1)  # the chain directory is gone; alert, spool and state remain
        os.remove(self.state_path())
        check(1)  # known only from the alert file and the spool
        shutil.rmtree(os.path.join(self.root, "spool", "ops"))
        check(0)  # known only from the alert file


if __name__ == "__main__":
    unittest.main()
