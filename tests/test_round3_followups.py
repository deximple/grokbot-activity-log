"""Follow-up tests for the round-3 audit items (W-…) that were finished in gblog.py after the first pass.

Covered here: bounded archive reads (W-02), git in the C locale (W-03), the skipped-row notice of
browser capture (W-04), GBLOG_DEBUG in the per-chain verify guard (W-08), JSON that stays valid on an
ASCII stdout (W-09), the tune window (W-11), ``anchor --skip-archives`` (W-12), the cheaper tail
lookup and snapshot (W-13), ``orphan_alerts`` in ``status --json`` (W-14), the state-level
``state_adopted`` marker moving into the chain (W-15), hook v2 and its in-place update (W-16),
token counters (W-17) and the dead-code removal (W-19).

Everything runs in temporary directories. No network and no git is used.
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

sys.path.insert(0, HERE)
from _support import DAY, run, run_cli, stamp  # noqa: E402

PW = "hunter2" + "hunter2"
HEX = "abcdef" + "0123456789"


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-r3-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        self.ledger.init()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG"):
            os.environ.pop(key, None)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def seed(self, bot="ops", n=2):
        return [self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1), "ts": stamp(i)})
                for i in range(n)]

    def chain(self, bot="ops"):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def raw_state(self, bot="ops"):
        with open(self.path("state", bot + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def quiet_append(self, bot, record):
        with redirect_stderr(io.StringIO()):
            return self.ledger.append(bot, record)


# --------------------------------------------------------------------------- W-02
class ArchiveBoundedReadTest(Base):
    def test_oversized_archive_is_a_mismatch_without_being_read_to_the_end(self):
        claimed = "ab" * 32
        rel = "archive/ops/%s/%s.gz" % (DAY, claimed)
        full = self.path(*rel.split("/"))
        os.makedirs(os.path.dirname(full))
        with gzip.open(full, "wb") as fh:
            fh.write(b"\0" * (8 << 20))  # compresses to a few KiB, expands to 8 MiB
        self.ledger.append("ops", {"type": "archive", "action": "file", "target": "bomb", "outcome": "ok",
                                   "kind": "other", "sha256": claimed, "bytes": 10, "path": rel, "ts": stamp(0)})
        sizes = []
        real = gzip.GzipFile.read

        def counting(this, size=-1):
            chunk = real(this, size)
            sizes.append(len(chunk))
            return chunk

        with mock.patch.object(gzip.GzipFile, "read", counting):
            res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertIn(rel + ": content hash mismatch (file modified after archiving)", res["problems"])
        self.assertTrue(sizes)
        self.assertLess(sum(sizes), 1 << 20)

    def test_genuine_archive_still_verifies_and_deduplicates(self):
        rec = gblog.archive_file(self.ledger, "ops", b"evidence line\n" * 1000, "e.txt", "artifact", ts=stamp(0))
        again = gblog.archive_file(self.ledger, "ops", b"evidence line\n" * 1000, "e.txt", "artifact", ts=stamp(1))
        self.assertIs(rec["_dedup"], False)
        self.assertIs(again["_dedup"], True)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 2)

    def test_oversized_file_at_the_content_address_is_rewritten(self):
        data = b"evidence\n"
        full = self.path("archive", "ops", DAY, hashlib.sha256(data).hexdigest() + ".gz")
        os.makedirs(os.path.dirname(full))
        with gzip.open(full, "wb") as fh:
            fh.write(b"\0" * (4 << 20))
        rec = gblog.archive_file(self.ledger, "ops", data, "e.txt", "artifact", ts=stamp(0))
        self.assertIs(rec["_dedup"], False)
        with gzip.open(full, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- W-03
class GitLocaleTest(Base):
    def test_git_runs_in_the_c_locale(self):
        calls = []

        def fake(argv, *args, **kwargs):
            calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        foreign = {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"}
        with mock.patch.dict(os.environ, foreign), mock.patch.object(gblog.subprocess, "run", side_effect=fake):
            gblog._git(self.root, "status", "--porcelain")
            self.assertEqual(os.environ["LC_ALL"], "de_DE.UTF-8")  # only the child's environment is changed
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[0], "git")
        self.assertEqual((kwargs["env"]["LC_ALL"], kwargs["env"]["LANGUAGE"]), ("C", "C"))
        self.assertEqual(kwargs["env"]["LANG"], "de_DE.UTF-8")


# --------------------------------------------------------------------------- W-04
def _webkit(epoch):
    return (int(epoch) + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000


def _add_visit(path, url, title, when):
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title))
        conn.execute("INSERT INTO visits(url, visit_time) VALUES (?, ?)", (cur.lastrowid, when))
        conn.commit()
    finally:
        conn.close()


def _chrome_history(directory, rows):
    """A Chrome-style History database. ``rows`` is [(url, title, visit time in WebKit microseconds)]."""
    prof = os.path.join(directory, "Default")
    os.makedirs(prof)
    path = os.path.join(prof, "History")
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        conn.commit()
    finally:
        conn.close()
    for row in rows:
        _add_visit(path, *row)
    return path


class BrowserUnusableRowsTest(Base):
    def _capture(self, hist):
        err = io.StringIO()
        with redirect_stderr(err):
            res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        return res, err.getvalue()

    def test_unusable_rows_are_announced_and_do_not_pin_the_marker(self):
        now = int(time.time())
        hist = _chrome_history(os.path.join(self.base, "chrome"), [
            ("https://a.example/first", "First", _webkit(now - 120)),
            ("https://far.example/future", "Ten years ahead", _webkit(now + 3650 * 86400)),
            ("https://far.example/absurd", "No calendar holds this", 9 * 10 ** 18),
        ])
        notice = "notice: skipped 2 unusable row(s) in %s\n" % hist
        res, err = self._capture(hist)
        self.assertEqual(res["added"], 1, res)
        self.assertIn(notice, err)
        self.assertEqual([r["target"] for r in self.chain()], ["https://a.example/first"])
        # a later, ordinary visit is still found: the future-dated rows did not move the marker past it
        _add_visit(hist, "https://b.example/second", "Second", _webkit(now - 30))
        res, err = self._capture(hist)
        self.assertEqual(res["added"], 1, res)
        self.assertIn(notice, err)
        self.assertEqual(self.chain()[-1]["target"], "https://b.example/second")

    def test_clean_history_prints_no_notice(self):
        now = int(time.time())
        hist = _chrome_history(os.path.join(self.base, "chrome"), [("https://a.example/first", "First", _webkit(now - 60)),
                                                                   ("chrome://newtab/", "New tab", _webkit(now - 30))])
        res, err = self._capture(hist)
        self.assertEqual(res["added"], 1, res)
        self.assertNotIn("unusable row", err)


# --------------------------------------------------------------------------- W-08
class DebugGuardTest(Base):
    def test_debug_env_reraises_from_verify_guard(self):
        self.seed("ops", 1)
        with mock.patch.object(gblog.Ledger, "verify", side_effect=RuntimeError("boom")):
            res = gblog._verify_guarded(self.ledger, "ops")
            self.assertIs(res["ok"], False)
            self.assertEqual(res["problems"], ["verify aborted: RuntimeError: boom"])
            with mock.patch.dict(os.environ, {"GBLOG_DEBUG": "1"}):
                with self.assertRaises(RuntimeError):
                    gblog._verify_guarded(self.ledger, "ops")


# --------------------------------------------------------------------------- W-09
class JsonWriterTest(Base):
    OBJ = {"name": "B\u00fcro \u2615 \U0001f600", "n": 1}

    def test_write_json_escapes_what_the_stream_cannot_encode(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="backslashreplace")
        gblog._write_json(stream, self.OBJ, indent=2)
        stream.flush()
        text = raw.getvalue().decode("ascii")
        self.assertEqual(json.loads(text), self.OBJ)
        self.assertNotIn("\\x", text)
        self.assertTrue(text.endswith("\n"))

    def test_write_json_is_unchanged_on_a_utf8_stream(self):
        out = io.StringIO()
        gblog._write_json(out, self.OBJ, indent=2)
        self.assertEqual(out.getvalue(), json.dumps(self.OBJ, indent=2, ensure_ascii=False) + "\n")
        out = io.StringIO()
        gblog._write_json(out, self.OBJ, sort_keys=True, ensure_ascii=True)
        self.assertEqual(out.getvalue(), json.dumps(self.OBJ, sort_keys=True) + "\n")
        # a lone surrogate cannot be written as UTF-8 either: it is escaped, not passed through
        out = io.StringIO()
        gblog._write_json(out, {"d": "lone \ud800"})
        self.assertEqual(out.getvalue(), '{"d": "lone \\ud800"}\n')

    def test_json_outputs_are_valid_on_ascii_stdout(self):
        with redirect_stderr(io.StringIO()):
            self.ledger.add_alias("chief", "B\u00fcro Chef", [])
            self.ledger.append("chief", {"type": "note", "detail": "caf\u00e9 \u2615 \U0001f600"})
        parsed = {}
        for argv in (["status", "--json"], ["verify", "--all", "--json"], ["tune", "--json", "--stdout"],
                     ["coverage", "--json"]):
            with self.subTest(argv=argv):
                rc, out, err = run_cli(argv, self.root, "ascii")
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
                out.encode("ascii")  # every character written to an ASCII stdout is ASCII
                parsed[argv[0]] = json.loads(out)
        self.assertEqual(parsed["status"]["bots"][0]["label"], "B\u00fcro Chef(chief)")
        self.assertEqual(parsed["status"]["orphan_alerts"], [])
        self.assertEqual([entry["bot"] for entry in parsed["verify"]], ["chief"])


# --------------------------------------------------------------------------- W-11
class TuneWindowTest(Base):
    KEY = "fail:connector:quickbooks.sync"

    def setUp(self):
        super().setUp()
        self.now = gblog._dt.datetime.now(gblog._dt.timezone.utc).replace(tzinfo=None)

    def ts(self, days_ago, hour=9, minute=0):
        day = self.now - gblog._dt.timedelta(days=days_ago)
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _snapshot(self, title):
        return {"title": title, "category": "skill", "key": self.KEY,
                "metric": {"kind": "fail", "bot": "ops", "action": "connector", "key": "quickbooks.sync"},
                "baseline": 3.0, "better": "lower", "window": {"since": self.ts(228), "until": self.ts(200)}, "count": 3}

    def _seed(self):
        L = self.ledger
        pid = gblog._proposal_id("ops", "skill", self.KEY)
        L.append("ops", {"type": "note", "detail": "ancient note", "ts": self.ts(201)})
        L.append("ops", {"type": "tune", "action": "other", "target": pid, "outcome": "ok", "detail": "old fix",
                         "proposal": self._snapshot("old change"), "ts": self.ts(200)})
        L.append("ops", {"type": "tune", "action": "other", "target": "feedbeef", "outcome": "ok", "detail": "recent fix",
                         "proposal": self._snapshot("recent change"), "ts": self.ts(50)})
        for d in (6, 5, 4):
            tid = "t%d" % d
            L.append("ops", {"type": "task_start", "task_id": tid, "task": "Reconcile invoices %d" % d, "ts": self.ts(d)})
            L.append("ops", {"type": "action", "task_id": tid, "action": "connector", "target": "quickbooks.sync",
                             "outcome": "fail", "detail": "401", "ts": self.ts(d, 9, 5)})
            L.append("ops", {"type": "task_end", "task_id": tid, "outcome": "fail", "ts": self.ts(d, 9, 30)})
        return pid

    def test_tune_parses_recent_files_and_keeps_old_decisions(self):
        pid = self._seed()
        parsed = []
        real = gblog.Ledger.iter_records

        def recording(ledger, bot, files=None):
            parsed.extend(os.path.basename(p) for p in (files or []))
            return real(ledger, bot, files)

        with mock.patch.object(gblog.Ledger, "iter_records", recording):
            rc, out, err = run(["tune", "--days", "28", "--json", "--stdout"], self.root)
        self.assertEqual(rc, 0, err)
        analysis = json.loads(out)
        # the day files of 200 days ago were scanned as raw lines only; the one of 50 days ago was parsed
        self.assertNotIn(self.ts(201)[:10] + ".jsonl", parsed)
        self.assertNotIn(self.ts(200)[:10] + ".jsonl", parsed)
        self.assertIn(self.ts(50)[:10] + ".jsonl", parsed)
        # the decision of 200 days ago still counts
        skill = [p for p in analysis["proposals"] if p["id"] == pid]
        self.assertEqual(len(skill), 1, [p["id"] for p in analysis["proposals"]])
        self.assertEqual(skill[0]["status"], "applied")
        self.assertEqual(skill[0]["count"], 3)
        # the effect table covers changes applied in the loaded range (90 days) only
        self.assertEqual([e["id"] for e in analysis["effects"]], ["feedbeef"])
        self.assertEqual(analysis["metrics"]["ops"]["tasks"], 3)

    def test_tune_history_returns_only_what_records_leaves_out(self):
        pid = self._seed()
        start = (self.now - gblog._dt.timedelta(days=90)).strftime("%Y-%m-%d")
        older = self.ledger.tune_history("ops", start)
        self.assertEqual([(r["type"], r["target"]) for r in older], [("tune", pid)])
        recent = self.ledger.records("ops", since=start)
        self.assertEqual(len(older) + len(recent) + 1, len(self.ledger.records("ops")))  # + the ancient note
        self.assertEqual(self.ledger.tune_history("ops", "1970-01-01"), [])

    def test_analyze_without_a_bound_keeps_every_effect(self):
        self._seed()
        recs = self.ledger.records("ops")
        full = gblog.analyze(recs, 28, gblog.coverage(recs))
        self.assertEqual(len(full["effects"]), 2)
        bounded = gblog.analyze(recs, 28, gblog.coverage(recs), effects_since=self.ts(90)[:10])
        self.assertEqual([e["id"] for e in bounded["effects"]], ["feedbeef"])
        self.assertEqual([p["id"] for p in bounded["proposals"] if p["category"] != "revert"],
                         [p["id"] for p in full["proposals"] if p["category"] != "revert"])


# --------------------------------------------------------------------------- W-12
class AnchorSkipArchivesTest(Base):
    def test_anchor_skip_archives(self):
        rec = gblog.archive_file(self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        with gzip.open(self.path(*rec["path"].split("/")), "wb") as fh:
            fh.write(b"something else")
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("ANCHOR FAIL", out)
        rc, out, err = run(["anchor", "--skip-archives"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ANCHOR OK", out)
        self.assertIn(rec["hash"][:12], out)
        # the full check still says what is wrong
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("content hash mismatch", out)


# --------------------------------------------------------------------------- W-13
class TailLookupTest(Base):
    DAYS = ("2024-06-01", "2024-06-02", "2024-06-03", "2024-06-04")

    def _seed_days(self):
        for day in self.DAYS:
            self.ledger.append("ops", {"type": "note", "detail": day, "ts": day + "T10:00:00Z"})
        return self.path("logs", "ops")

    def test_tail_record_looks_at_the_newest_file_only(self):
        chain_dir = self._seed_days()
        looked = []
        real = os.path.isfile

        def counting(path):
            if os.path.dirname(str(path)) == chain_dir:
                looked.append(os.path.basename(str(path)))
            return real(path)

        with mock.patch.object(gblog.os.path, "isfile", counting):
            path, rec = self.ledger._tail_record("ops")
        self.assertEqual(os.path.basename(path), "2024-06-04.jsonl")
        self.assertEqual(rec["detail"], "2024-06-04")
        self.assertEqual(looked, ["2024-06-04.jsonl"])
        self.assertEqual(self.ledger._tail_record("nosuch"), (None, None))

    def test_tail_record_skips_empty_and_non_regular_entries(self):
        chain_dir = self._seed_days()
        with open(os.path.join(chain_dir, "2024-06-05.jsonl"), "w", encoding="utf-8"):
            pass
        os.mkdir(os.path.join(chain_dir, "2024-06-06.jsonl"))
        path, rec = self.ledger._tail_record("ops")
        self.assertEqual(os.path.basename(path), "2024-06-04.jsonl")
        self.assertEqual(rec["seq"], 4)
        newer = self.ledger.append("ops", {"type": "note", "detail": "after", "ts": "2024-06-04T11:00:00Z"})
        self.assertEqual((newer["seq"], newer["prev"]), (5, rec["hash"]))

    def test_snapshot_keeps_text_only_on_request(self):
        self.seed("ops", 2)
        lines, names = self.ledger._chain_snapshot("ops", keep_text=False)
        self.assertEqual(names, ["logs/ops/%s.jsonl" % DAY])
        self.assertEqual([item[3] for item in lines], [None, None])
        self.assertEqual([item[2]["seq"] for item in lines], [1, 2])
        lines, _names = self.ledger._chain_snapshot("ops")
        self.assertTrue(all(isinstance(item[3], str) for item in lines))
        # strict still sees a reformatted line; the default mode stays quiet
        path = self.ledger.log_files("ops")[0]
        with open(path, encoding="utf-8") as fh:
            original = fh.read().splitlines()
        compact = json.dumps(json.loads(original[0]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join([compact] + original[1:]) + "\n")
        self.assertTrue(self.ledger.verify("ops")["ok"])
        strict = self.ledger.verify("ops", strict=True)
        self.assertFalse(strict["ok"])
        self.assertIn("logs/ops/%s.jsonl:1: non-canonical line (duplicate keys or reformatted)" % DAY, strict["problems"])


# --------------------------------------------------------------------------- W-14
class OrphanAlertsJsonTest(Base):
    def test_status_json_lists_orphan_alerts(self):
        self.seed("ops", 2)
        self.seed("keeper", 1)
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["orphan_alerts"], [])

        self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        rc, out, err = run(["note", "--bot", "ops", "--detail", "refused"], self.root)
        self.assertEqual(rc, 4, out + err)
        shutil.rmtree(self.path("logs", "ops"))
        os.remove(self.path("state", "ops.json"))

        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual([row["bot"] for row in data["bots"]], ["keeper"])
        self.assertEqual(len(data["orphan_alerts"]), 1, data["orphan_alerts"])
        orphan = data["orphan_alerts"][0]
        self.assertEqual((orphan["bot"], orphan["spooled"]), ("ops", 1))
        self.assertIsInstance(orphan["alert"], str)
        # the text output says the same thing, as before
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ALERT ops: tail mismatch since %s (1 spooled)" % orphan["alert"], out)
        # --bot filters both
        rc, out, err = run(["status", "--json", "--bot", "keeper"], self.root)
        self.assertEqual(json.loads(out)["orphan_alerts"], [])


# --------------------------------------------------------------------------- W-15
class StateAdoptedTest(Base):
    def test_state_level_adoption_moves_into_next_record(self):
        self.seed("ops", 2)
        os.remove(self.path("state", "ops.json"))
        last_sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234", "pushed": False}
        with redirect_stderr(io.StringIO()):
            self.assertTrue(self.ledger.update_state("ops", {"last_sync": last_sync}))
        self.assertEqual(self.raw_state()["state_adopted"]["how"], "missing")
        state_line = "state/ops.json was re-created from the log tail at seq 2 (it was missing)"
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertTrue([w for w in res["warnings"] if w.startswith(state_line)], res["warnings"])

        rec = self.ledger.append("ops", {"type": "note", "detail": "next", "ts": stamp(5)})
        self.assertEqual(rec["state_adopted"], "missing")
        state = self.raw_state()
        self.assertNotIn("state_adopted", state)
        self.assertEqual(state["last_sync"], last_sync)
        self.assertEqual((state["seq"], state["head"]), (3, rec["hash"]))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertIn("logs/ops/%s.jsonl:3: appended while state/ops.json was missing; truncation before seq 3 "
                      "cannot be ruled out from state" % DAY, res["warnings"])
        self.assertFalse([w for w in res["warnings"] if w.startswith(state_line)], res["warnings"])
        # the marker is written once
        after = self.ledger.append("ops", {"type": "note", "detail": "later", "ts": stamp(6)})
        self.assertNotIn("state_adopted", after)

    def test_adoption_warnings_are_capped_at_three(self):
        self.seed("ops", 1)
        for i in range(5):
            os.remove(self.path("state", "ops.json"))
            self.quiet_append("ops", {"type": "note", "detail": "adopted %d" % i, "ts": stamp(i + 1)})
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        listed = [w for w in res["warnings"] if "appended while state/ops.json was missing;" in w]
        self.assertEqual(len(listed), 3, res["warnings"])
        self.assertIn("2 more record(s) appended while state/ops.json was missing or unreadable", res["warnings"])


# --------------------------------------------------------------------------- W-16
class ShellHookTest(Base):
    OLD_BLOCK = "# >>> gblog shell hook >>>\n__gblog_hook() { :; }\n# <<< gblog shell hook <<<\n"

    def test_hook_is_marked_v2(self):
        lines = gblog.shell_hook("ops").splitlines()
        self.assertEqual(lines[0], "# >>> gblog shell hook >>>")
        self.assertEqual(lines[1], "# gblog hook v2")
        self.assertEqual(lines[-1], "# <<< gblog shell hook <<<")
        self.assertIn('*"\\$("*', gblog.shell_hook("ops"))

    def test_install_replaces_outdated_hook(self):
        rc = os.path.join(self.base, "bashrc")
        before, after = b"export A=1 # caf\xe9\n", b"alias ll='ls -l'\n"  # a byte that is not UTF-8 survives
        with open(rc, "wb") as fh:
            fh.write(before + self.OLD_BLOCK.encode("ascii") + after)
        self.assertEqual(gblog._install_shell_hook("ops", rc), "updated")
        with open(rc, "rb") as fh:
            self.assertEqual(fh.read(), before + gblog.shell_hook("ops").encode("utf-8") + after)
        self.assertEqual(gblog._install_shell_hook("ops", rc), "present")
        self.assertFalse(gblog.install_shell_hook("ops", rc))
        # another default Bot is a different block: replaced, still exactly one block
        self.assertTrue(gblog.install_shell_hook("other", rc))
        with open(rc, "rb") as fh:
            data = fh.read()
        self.assertEqual(data.count(b">>> gblog shell hook >>>"), 1)
        self.assertIn(b"GBLOG_BOT:-other", data)
        self.assertTrue(data.startswith(before) and data.endswith(after))

    def test_block_without_end_marker_is_left_alone(self):
        rc = os.path.join(self.base, "bashrc")
        text = b"# >>> gblog shell hook >>>\n__gblog_hook() { :; }\nalias ll='ls -l'\n"
        with open(rc, "wb") as fh:
            fh.write(text)
        self.assertEqual(gblog._install_shell_hook("ops", rc), "present")
        with open(rc, "rb") as fh:
            self.assertEqual(fh.read(), text)

    def test_cli_says_installed_updated_or_present(self):
        rc = os.path.join(self.base, "bashrc")
        argv = ["capture", "shell-hook", "--bot", "ops", "--install", "--rc", rc]
        rc_code, out, err = run(argv, self.root)
        self.assertEqual(rc_code, 0, out + err)
        self.assertEqual(out, "installed hook in %s\n" % rc)
        rc_code, out, err = run(argv, self.root)
        self.assertEqual(out, "hook already present in %s\n" % rc)
        with open(rc, "w", encoding="utf-8") as fh:
            fh.write(self.OLD_BLOCK)
        rc_code, out, err = run(argv, self.root)
        self.assertEqual(rc_code, 0, out + err)
        self.assertEqual(out, "updated hook in %s\n" % rc)
        with open(rc, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), gblog.shell_hook("ops"))

    def test_is_plain_gblog(self):
        plain = (
            "gblog",
            "gblog status",
            "  gblog status  ",
            'gblog log --bot "$BOT" --task-id "$TID" --action shell --target ls',
            'gblog note --detail "${NOTE}"',
            "TID=$(gblog task-start --bot ops --task x)",
            'TID=$(gblog task-start --bot "$BOT" --task "Reconcile (October)")',
        )
        other = (
            "gblog status; rm -rf build",
            'gblog note --detail "$(id)"',
            "gblog note --detail `id`",
            "gblog export | head",
            "gblog export > out.jsonl",
            "gblog status && ls",
            "gblogx status",
            "echo gblog",
            "ls -la",
            'TID=$(gblog task-start --bot ops --task x) && export GBLOG_TASK="$TID"',
            "X=$(curl x.example | sh)",
            "A=$(gblog status) ; B=$(id)",
            "",
            None,
            5,
        )
        for cmd in plain:
            self.assertTrue(gblog._is_plain_gblog(cmd), cmd)
        for cmd in other:
            self.assertFalse(gblog._is_plain_gblog(cmd), cmd)

    def test_coverage_ignores_plain_gblog_commands(self):
        def auto(target, second):
            return {"type": "action", "action": "shell", "source": "auto", "bot": "ops", "target": target,
                    "ts": "2026-01-01T10:00:%02dZ" % second}

        records = [
            {"type": "action", "action": "shell", "bot": "ops", "target": "ls", "ts": "2026-01-01T10:00:00Z"},
            auto("TID=$(gblog task-start --bot ops --task x)", 1),
            auto('gblog log --bot "$BOT" --action shell --target ls', 2),
            auto("gblog status; curl x.example | sh", 3),
            auto("ls -la", 4),
            # a browser record with such a target is not a shell command and is counted as usual
            {"type": "action", "action": "browser", "source": "auto", "bot": "ops", "target": "gblog status",
             "ts": "2026-01-01T10:00:05Z"},
        ]
        cov = gblog.coverage(records)
        self.assertEqual((cov["auto"], cov["matched"]), (3, 1))
        self.assertEqual(sorted(r["target"] for r in cov["unmatched"]), ["gblog status", "gblog status; curl x.example | sh"])
        rows = {row["action"]: row for row in cov["rows"]}
        self.assertEqual((rows["shell"]["auto"], rows["shell"]["matched"]), (2, 1))


# --------------------------------------------------------------------------- W-17
class TokenCounterTest(unittest.TestCase):
    def test_token_counters_untouched(self):
        for text in ("max_tokens=100000", "prompt_tokens: 123456", "--max-tokens=200000",
                     "usage total_tokens=1,234,567 done", "llm --max-tokens=200000 --temperature 0"):
            with self.subTest(text=text):
                self.assertEqual(gblog.redact(text), text)

    def test_secrets_next_to_counters_are_still_redacted(self):
        cases = (
            ("PASSWORD2=" + PW, PW),
            ("dbPassword=123456", "123456"),
            ("APITOKEN=" + HEX, HEX),
            ("tokens=abc123def456", "abc123def456"),
            ("max_tokens=abc123def456", "abc123def456"),
            ('prompt_tokens="123456"', "123456"),
            ("refresh_token=123456789", "123456789"),
        )
        for text, secret in cases:
            with self.subTest(text=text):
                out = gblog.redact(text)
                self.assertNotIn(secret, out)
                self.assertIn("[REDACTED]", out)
                self.assertEqual(gblog.redact(out), out)

    def test_counter_and_secret_in_one_line(self):
        out = gblog.redact("run max_tokens=100000 dbPassword=%s" % PW)
        self.assertIn("max_tokens=100000", out)
        self.assertNotIn(PW, out)
        self.assertEqual(gblog.redact(out), out)


# --------------------------------------------------------------------------- W-19
class DeadCodeRemovalTest(Base):
    def test_removed_names_are_gone(self):
        for name in ("_exit2", "_BREAK_KEYS", "_touch_private"):
            self.assertFalse(hasattr(gblog, name), name)
        self.assertFalse(hasattr(gblog.Ledger, "_assert_tail_matches"))
        self.assertFalse(hasattr(gblog.Ledger._Lock, "_held"))

    def test_lock_is_a_plain_holder(self):
        lock = self.ledger.lock("ops")
        with lock:
            self.assertIsNotNone(lock.fh)
        self.assertIsNone(lock.fh)
        with self.ledger.lock("ops"):  # free again after the release
            pass
        self.assertTrue(os.path.isfile(self.path("state", "ops.lock")))

    def test_read_anchor_matches_the_file(self):
        self.seed("ops", 2)
        self.ledger.anchor(["ops"])
        with open(self.path("anchors", "ops.json"), encoding="utf-8") as fh:
            self.assertEqual(self.ledger.read_anchor("ops"), json.load(fh))
        self.assertIsNone(self.ledger.read_anchor("nosuch"))
        with open(self.path("anchors", "bad.json"), "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        self.assertIsNone(self.ledger.read_anchor("bad"))

    def test_tune_apply_error_texts_are_unchanged(self):
        rc, out, err = run(["tune", "--apply", "abcd1234"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(err, "no proposals yet; run `gblog tune` first\n")
        proposals = self.path("tuning", "proposals.json")
        with open(proposals, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        rc, out, err = run(["tune", "--apply", "abcd1234"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(err, "error: tuning/proposals.json is corrupt\n")
        with self.assertRaises(gblog.ProposalsCorruptError) as ctx:
            gblog.tune_apply(self.ledger, "abcd1234", None)
        self.assertEqual(str(ctx.exception), "tuning/proposals.json is corrupt")
        with open(proposals, "w", encoding="utf-8") as fh:
            json.dump({"generated": "2024-06-01T10:00:00Z", "proposals": []}, fh)
        rc, out, err = run(["tune", "--apply", "abcd1234"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(err, "unknown proposal id abcd1234 (run `gblog tune` to list current ids)\n")

    def test_reserved_and_capture_refusals_keep_their_text(self):
        rc, out, err = run(["note", "--bot", "aliases", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: bot name 'aliases' is reserved", err)
        missing = os.path.join(self.base, "never-there")
        rc, out, err = run(["capture", "files", "--bot", "ops", "--path", missing], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: capture path %s does not exist or is not a directory" % missing, err)


if __name__ == "__main__":
    unittest.main()
