"""Tests for the three deep subsystem reviews of gblog 0.6.1.

* DR2: append, locking, spool and alerts, crash windows, recovery and state rebuild (ids D2-..)
* DR5: the CLI layer (ids D5-..)
* DR6: archive, report rendering, status, tail, export, alias registry (ids D6-..)

Each test names the finding it covers. Everything runs in temporary directories. No network is
used: git remotes are local bare repositories and browser histories are built with sqlite3.
"""
import errno
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
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import gblog  # noqa: E402

sys.path.insert(0, HERE)
from _support import DAY, GIT_ENV, run, run_cli, stamp  # noqa: E402

GIT = shutil.which("git")
GBLOG_PY = os.path.join(REPO, "gblog.py")
FIXTURE = os.path.join(HERE, "fixtures", "kim-biseo-break")
HEAD_MISMATCH = "state/%s.json: head does not match last record (state tampered or log truncated)"


def chrome_history(directory, visits):
    """A Chrome-style History database. ``visits`` is [(url, title, seconds ago)]."""
    prof = os.path.join(directory, "Default")
    os.makedirs(prof)
    path = os.path.join(prof, "History")
    now = int((time.time() + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        for i, (url, title, age) in enumerate(visits, 1):
            conn.execute("INSERT INTO urls VALUES (?, ?, ?)", (i, url, title))
            conn.execute("INSERT INTO visits VALUES (?, ?, ?)", (i, i, now - age * 1_000_000))
        conn.commit()
    finally:
        conn.close()
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-deep-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.fresh = os.path.join(self.base, "fresh")  # never created by setUp
        self.ledger = gblog.Ledger(self.root)
        self.ledger.init()
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT"):
            os.environ.pop(key, None)

    # -- helpers
    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def quiet(self, fn, *args, **kwargs):
        """Call a library function; returns ``(result, what it wrote to stderr)``."""
        err = io.StringIO()
        with redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, err.getvalue()

    def seed(self, bot="ops", n=2):
        with redirect_stderr(io.StringIO()):
            return [self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1), "ts": stamp(i)})
                    for i in range(n)]

    def chain(self, bot="ops"):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def raw_state(self, bot="ops"):
        with open(self.path("state", bot + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write_json(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def break_state(self, bot="ops"):
        self.ledger.write_state(bot, {"seq": 99, "head": "cd" * 32})

    def restore_state(self, bot="ops"):
        seq, head = self.ledger._tail_head(bot)
        self.ledger.write_state(bot, {"seq": seq, "head": head})

    def refuse(self, bot, record):
        """Append a record that must be refused; returns the TailMismatchError."""
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            self.ledger.append(bot, record)
        return ctx.exception

    def pending(self, bot="ops"):
        d = self.path("spool", bot)
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.endswith(".json"))

    def snapshot(self, root=None):
        """{relative path: bytes} of every file below the root, plus one entry per directory."""
        root = root or self.root
        found = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                found[os.path.relpath(os.path.join(dirpath, name), root) + os.sep] = None
            for name in filenames:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    found[os.path.relpath(full, root)] = fh.read()
        return found

    def state_write_failing(self, *failing):
        """Patch Ledger.write_state: the calls whose 1-based number is in ``failing`` raise ENOSPC."""
        real, count = gblog.Ledger.write_state, [0]

        def write_state(ledger, bot, state):
            count[0] += 1
            if count[0] in failing:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), ledger._state_path(bot))
            return real(ledger, bot, state)

        return mock.patch.object(gblog.Ledger, "write_state", write_state)

    def status_rows(self):
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        return {row["bot"]: row for row in json.loads(out)["bots"]}

    def tamper(self, bot, old, new):
        path = self.ledger.log_files(bot)[0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old, new))


# --------------------------------------------------------------------------- DR2: append and recovery
class AppendRecoveryTest(Base):
    def test_two_failed_state_writes_do_not_brick_the_chain(self):
        # D2-01: a second failed state write after a roll-forward must not leave state two records behind
        self.seed("ops", 1)
        with self.state_write_failing(1, 2), redirect_stderr(io.StringIO()):
            for text in ("two", "three"):
                with self.assertRaises(OSError):
                    self.ledger.append("ops", {"type": "note", "detail": text, "ts": stamp(5)})
        # "two" is durable (only its state write failed); "three" failed before anything was written
        self.assertEqual([r["detail"] for r in self.chain()], ["ops record 1", "two"])
        self.assertEqual(self.raw_state()["seq"], 1)
        rec, err = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "four", "ts": stamp(6)})
        self.assertIn("notice: recovered state/ops.json after an interrupted append", err)
        self.assertEqual(rec["seq"], 3)
        self.assertEqual([r["detail"] for r in self.chain()], ["ops record 1", "two", "four"])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(self.pending(), [])
        self.assertFalse(os.path.exists(self.path("state", "ops.alert.json")))

    def test_roll_forward_is_persisted_before_the_new_line(self):
        # D2-01: in the recovery append the adopted tail reaches state before the new line is written
        self.seed("ops", 1)
        with self.state_write_failing(1), self.assertRaises(OSError):
            self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        two = self.chain()[-1]
        events = []
        real_state, real_line = gblog.Ledger.write_state, gblog.Ledger._append_line

        def write_state(ledger, bot, state):
            events.append(("state", state.get("seq"), state.get("head")))
            return real_state(ledger, bot, state)

        def append_line(ledger, path, line):
            events.append(("line",))
            return real_line(ledger, path, line)

        with mock.patch.object(gblog.Ledger, "write_state", write_state), \
                mock.patch.object(gblog.Ledger, "_append_line", append_line), redirect_stderr(io.StringIO()):
            rec = self.ledger.append("ops", {"type": "note", "detail": "three", "ts": stamp(2)})
        self.assertEqual(events, [("state", 2, two["hash"]), ("line",), ("state", 3, rec["hash"])])
        self.assertEqual(self.raw_state()["updated"], stamp(2))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_first_append_interrupted_rolls_forward_without_marker(self):
        # D2-09: an interrupted first append is "one behind" as well, not "state missing"
        with self.state_write_failing(2), self.assertRaises(OSError), redirect_stderr(io.StringIO()):
            self.ledger.append("fresh", {"type": "note", "detail": "one", "ts": stamp(0)})
        self.assertEqual(len(self.chain("fresh")), 1)
        self.assertEqual(self.raw_state("fresh")["seq"], 0)
        rec, err = self.quiet(self.ledger.append, "fresh", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertIn("notice: recovered state/fresh.json after an interrupted append", err)
        self.assertNotIn("state_adopted", rec)
        self.assertEqual(rec["seq"], 2)
        res = self.ledger.verify("fresh")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [])

    def test_non_string_state_head_is_adopted_like_unreadable(self):
        # D2-04: verify, append, state rebuild and update_state agree on a state whose head is not a string
        self.seed("ops", 2)
        sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234"}

        def damage():
            self.write_json(self.path("state", "ops.json"), {"seq": 2, "head": 7, "last_sync": sync})

        damage()
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("state/ops.json missing or unreadable", out)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "n"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: state/ops.json was unreadable; adopted the log tail (seq 2)", err)
        state = self.raw_state()
        self.assertEqual((state["seq"], state["head"], state["last_sync"]), (3, self.chain()[-1]["hash"], sync))
        self.assertEqual(self.pending(), [])

        damage()
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rebuilt ops seq 4", out)
        self.assertEqual(self.raw_state()["last_sync"], sync)

        damage()
        wrote, err = self.quiet(self.ledger.update_state, "ops", {"marker": 1})
        self.assertIs(wrote, True)
        self.assertIn("notice: state/ops.json was unreadable; adopted the log tail (seq 4)", err)
        state = self.raw_state()
        self.assertEqual((state["seq"], state["head"]), (4, self.chain()[-1]["hash"]))
        self.assertEqual((state["last_sync"], state["marker"]), (sync, 1))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_state_seq_mismatch_warns_and_can_be_rebuilt(self):
        # D2-05: the right head with the wrong seq
        recs = self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": recs[-1]["hash"]})
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [
            "state/ops.json: seq 99 does not match the last record (seq 2); appends are refused until "
            "`gblog state rebuild` is run"])
        self.assertFalse(self.ledger._tail_ok("ops"))
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rebuilt ops seq 3", out)
        self.assertEqual(self.raw_state()["seq"], 3)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [])
        # a head that is not in the chain is still refused
        self.ledger.write_state("ops", {"seq": 2, "head": "ab" * 32})
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("restore the log from backup", err)

    def test_update_state_refuses_reserved_names(self):
        # D2-02 / D5-01: the state path of a reserved name is one of gblog's own files
        fresh = gblog.Ledger(self.fresh)
        for name in ("ops.alert", "aliases", "capture-files-ops", "ops.lock"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
                    fresh.update_state(name, {"capture_browser_last": {}})
                self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(os.path.exists(self.fresh))
        # a chain that already exists under such a name keeps working
        os.makedirs(self.path("logs", "legacy.tmp"))
        wrote, _err = self.quiet(self.ledger.update_state, "legacy.tmp", {"x": 1})
        self.assertIs(wrote, True)
        self.assertEqual(self.raw_state("legacy.tmp")["x"], 1)


# --------------------------------------------------------------------------- DR2: spool and alerts
class SpoolAlertTest(Base):
    TASK = ({"type": "task_start", "task_id": "t1", "task": "T"},
            {"type": "action", "task_id": "t1", "action": "shell", "target": "ls"},
            {"type": "task_end", "task_id": "t1", "outcome": "ok"})

    def test_spool_replay_keeps_refusal_order_within_one_second(self):
        # D2-03: three events refused within one second are replayed in the order they were refused
        self.seed("ops", 1)
        self.break_state()

        class FakeId:
            def __init__(self, text):
                self.hex = text + "0" * 24

        ids = iter(["ffffffff", "88888888", "00000000"])
        with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T10:00:05Z"), \
                mock.patch.object(gblog.uuid, "uuid4", side_effect=lambda: FakeId(next(ids))):
            for rec in self.TASK:
                self.refuse("ops", dict(rec))
        self.assertEqual(len(self.pending()), 3)
        self.restore_state()
        res, _err = self.quiet(self.ledger.spool_replay, "ops")
        self.assertEqual([r["type"] for r in res["replayed"]], ["task_start", "action", "task_end"])
        self.assertEqual(self.status_rows()["ops"]["open_tasks"], 0)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_spool_file_names_sort_in_refusal_order(self):
        # D2-03
        self.seed("ops", 1)
        self.break_state()
        created = []
        with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T10:00:05Z"):
            for i in range(12):
                created.append(self.refuse("ops", {"type": "note", "detail": "refused %d" % i}).spool_path)
        self.assertEqual(self.ledger.spool_files("ops"), created)

    def test_replay_guard_skip_heals_state(self):
        # D2-01: the replayed line is durable, state is one behind, the spool file is still pending
        self.seed("ops", 2)
        self.break_state()
        spool = self.refuse("ops", {"type": "note", "detail": "refused once"}).spool_path
        self.restore_state()
        with open(spool, encoding="utf-8") as fh:
            payload = json.load(fh)
        name = os.path.basename(spool)
        with self.state_write_failing(1), self.assertRaises(OSError):
            self.ledger.append("ops", dict(payload["record"], spooled_at=payload["ts"], spool_id=name[:-5]))
        self.assertEqual(self.raw_state()["seq"], 2)
        res, _err = self.quiet(self.ledger.spool_replay, "ops")
        self.assertEqual((res["replayed"], res["skipped"], res["refused"], res["remaining"]), ([], [], False, 0))
        self.assertEqual(self.pending(), [])
        self.assertTrue(os.path.isfile(self.path("spool", "ops", "replayed", name)))
        self.assertEqual([r.get("detail") for r in self.chain()].count("refused once"), 1)
        state = self.raw_state()
        self.assertEqual((state["seq"], state["head"]), (3, self.chain()[-1]["hash"]))
        self.assertTrue(res["alert_resolved"])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_alert_since_is_the_first_pending_refusal(self):
        # D2-06 / D5-13 / D6-14: "since" is the oldest pending event; a spool without an alert file is still an alert
        self.seed("ops", 1)
        self.break_state()
        for when in ("2024-06-01T10:00:00Z", "2024-06-01T12:00:00Z"):
            with mock.patch.object(gblog, "now_iso", return_value=when):
                self.refuse("ops", {"type": "note", "detail": "refused"})
        line = "ALERT ops: tail mismatch since 2024-06-01T10:00:00Z (2 spooled)"
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(line, out.splitlines())
        self.assertEqual(self.ledger.alert_info("ops"), ("2024-06-01T10:00:00Z", 2))

        os.remove(self.path("state", "ops.alert.json"))
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(line, out.splitlines())
        row = self.status_rows()["ops"]
        self.assertEqual((row["alert"], row["spooled"]), ("2024-06-01T10:00:00Z", 2))
        # no spool and no alert file: no alert
        shutil.rmtree(self.path("spool", "ops"))
        self.assertEqual(self.ledger.alert_info("ops"), (None, 0))
        rc, out, err = run(["status"], self.root)
        self.assertNotIn("ALERT", out)

    def test_spool_replay_unknown_bot_exit_2(self):
        # D5-06
        rc, out, err = run(["spool", "replay", "--bot", "typo"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("spool replay: no chain, spool or alert on disk for 'typo'", err)
        self.assertEqual(out, "")
        self.seed("ops", 1)
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("replayed 0 spooled event(s) for ops", out)
        # an alert file alone is enough (the documented way to dismiss a stale alert)
        self.write_json(self.path("state", "gone.alert.json"), {"ts": "2024-06-01T10:00:00Z"})
        rc, out, err = run(["spool", "replay", "--bot", "gone"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("alert resolved", out)

    def test_alert_line_has_no_escapes(self):
        # D5-08: the observed seq and hash come from the last log line as written
        recs = self.seed("ops", 2)
        planted = {"v": 1, "seq": "x\nALERT resolved", "ts": stamp(5), "bot": "ops", "type": "note",
                   "prev": recs[-1]["hash"], "hash": "\x1b[2Jdeadbeef"}
        with open(self.ledger.log_files("ops")[0], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(planted, sort_keys=True) + "\n")
        rc, out, err = run(["note", "--bot", "ops", "--detail", "n"], self.root)
        self.assertEqual(rc, 4, out + err)
        lines = err.splitlines()
        self.assertEqual(len(lines), 3, err)
        self.assertTrue(lines[0].startswith("ALERT tail mismatch: "), err)
        self.assertTrue(lines[1].startswith("spool: "), err)
        self.assertTrue(lines[2].startswith("alert: "), err)
        self.assertNotIn("\x1b", err)

    def test_seal_refuses_when_own_chain_has_tail_mismatch(self):
        # D2-08 (a): a spooled break_ack could never be replayed, so nothing is spooled
        prod = os.path.join(self.base, "prod")
        os.makedirs(prod)
        for name in ("logs", "state"):
            shutil.copytree(os.path.join(FIXTURE, name), os.path.join(prod, name))
        ledger = gblog.Ledger(prod)
        desc = ledger.verify("kim-biseo")["breaks"][0]
        ledger.write_state("kim-biseo", {"seq": 99, "head": "cd" * 32})
        before = self.snapshot(os.path.join(prod, "logs"))
        with self.assertRaises(gblog.SealError) as ctx:
            ledger.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
        self.assertIn("kim-biseo has a tail mismatch", str(ctx.exception))
        self.assertEqual(self.snapshot(os.path.join(prod, "logs")), before)
        self.assertFalse(os.path.exists(os.path.join(prod, "spool", "kim-biseo")))
        self.assertFalse(os.path.exists(os.path.join(prod, "state", "kim-biseo.alert.json")))


# --------------------------------------------------------------------------- reserved names (D2-02, D5-01, D6-12)
class ReservedNameTest(Base):
    def test_capture_refuses_reserved_bot_names_and_writes_nothing(self):
        self.seed("ops", 1)
        self.quiet(self.ledger.add_alias, "ops", "Ops", [])
        empty = chrome_history(os.path.join(self.base, "chrome-empty"), [("chrome://newtab/", "New tab", 60)])
        visit = chrome_history(os.path.join(self.base, "chrome-visit"), [("https://a.example/x", "A", 60)])
        before = self.snapshot()
        for name in ("ops.alert", "aliases", "capture-files-ops"):
            for hist in (empty, visit):
                with self.subTest(name=name, history=os.path.basename(os.path.dirname(os.path.dirname(hist)))):
                    rc, out, err = run(["capture", "browser", "--bot", name, "--history", hist], self.root)
                    self.assertEqual(rc, 2, out + err)
                    self.assertIn("reserved", err)
                    self.assertEqual(out, "")
        self.assertEqual(self.snapshot(), before)
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("ALERT", out)

    def test_capture_files_reserved_name_refused_on_first_run(self):
        work = os.path.join(self.base, "work")
        os.makedirs(work)
        with open(os.path.join(work, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("1")
        before = self.snapshot()
        rc, out, err = run(["capture", "files", "--bot", "ops.tmp", "--path", work], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("reserved", err)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            gblog.capture_files(self.ledger, "ops.tmp", work)
        self.assertEqual(self.snapshot(), before)

    def test_archive_reserved_name_creates_nothing(self):
        src = os.path.join(self.base, "evidence.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("evidence\n")
        rc, out, err = run(["archive", "--bot", "aliases", "--file", src], self.fresh)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("reserved", err)
        self.assertFalse(os.path.exists(self.fresh), "a refused name created the ledger root")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            gblog.archive_file(gblog.Ledger(self.fresh), "ops.tmp", b"x\n", "x.txt")
        self.assertFalse(os.path.exists(self.fresh))
        self.assertFalse(os.path.exists(self.path("archive", "aliases")))

    @unittest.skipUnless(GIT, "git not installed")
    def test_sync_reserved_bot_does_not_touch_origin(self):
        first, second = os.path.join(self.base, "a.git"), os.path.join(self.base, "b.git")
        for repo in (first, second):
            subprocess.run([GIT, "init", "--bare", "-q", repo], check=True, capture_output=True)
        self.seed("ops", 1)
        rc, out, err = run(["sync", "--no-push", "--remote", first], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["sync", "--no-push", "--bot", "ops.tmp", "--remote", second, "--replace-remote"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("reserved", err)
        origin = subprocess.run([GIT, "-C", self.root, "remote", "get-url", "origin"], check=True,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(origin, first)
        rc, out, err = run(["sync", "--no-push", "--bot", "ops.tmp"], self.fresh)
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(os.path.exists(self.fresh))

    def test_side_files_are_not_chains(self):
        # D2-14: state/aliases.json and state/<bot>.alert.json are not "the state file" of a chain
        self.seed("ops", 1)
        rc, out, err = run(["alias", "add", "--slug", "ops", "--display", "Ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.write_json(self.path("state", "ops.alert.json"), {"ts": "2024-06-01T10:00:00Z"})
        before = sorted(os.listdir(self.path("state")))
        for name in ("aliases", "ops.alert"):
            with self.subTest(name=name):
                rc, out, err = run(["verify", "--bot", name], self.root)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("no chain on disk for '%s'" % name, out)
                self.assertNotIn("head genesis", out)
        rc, out, err = run(["anchor", "--bot", "aliases"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(os.listdir(self.path("anchors")), [])
        self.assertEqual(sorted(os.listdir(self.path("state"))), before)

    def test_capture_only_state_is_not_a_chain(self):
        # D5-14: a state file that names no head (written by a capture for a mistyped name) is not a chain
        hist = chrome_history(os.path.join(self.base, "chrome"), [("chrome://newtab/", "New tab", 60)])
        rc, out, err = run(["capture", "browser", "--bot", "typo", "--history", hist], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isfile(self.path("state", "typo.json")))
        rc, out, err = run(["verify", "--bot", "typo"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("no chain on disk for 'typo'", out)
        rc, out, err = run(["anchor", "--bot", "typo"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(os.path.exists(self.path("anchors", "typo.json")))
        # a chain whose logs are gone while state names a head still fails as before
        self.seed("ops", 2)
        shutil.rmtree(self.path("logs", "ops"))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn(HEAD_MISMATCH % "ops", out)


# --------------------------------------------------------------------------- DR5: CLI layer
class CliLayerTest(Base):
    def test_capture_browser_negative_hours_is_usage_error(self):
        # D5-02
        hist = chrome_history(os.path.join(self.base, "chrome"), [("https://a.example/x", "A", 60)])
        rc, out, err = run(["capture", "browser", "--bot", "ops", "--history", hist, "--hours=-5"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: --hours must be 0 or more", err)
        self.assertFalse(os.path.exists(self.path("state", "ops.json")))
        rc, out, err = run(["capture", "browser", "--bot", "ops", "--history", hist], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("browser: 1 new visit(s)", out)

    def test_coverage_negative_window_is_usage_error(self):
        # D5-02
        rc, out, err = run(["coverage", "--window=-5"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: --window must be 0 or more", err)

    def test_future_marker_is_ignored_with_notice(self):
        # D5-02: a marker that is already pinned in the future no longer blinds browser capture
        hist = chrome_history(os.path.join(self.base, "chrome"), [("https://a.example/x", "A", 60)])
        future = int((time.time() + 10 * 365 * 86400 + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
        self.quiet(self.ledger.update_state, "ops", {"capture_browser_last": {hist: future}})
        res, err = self.quiet(gblog.capture_browser, self.ledger, "ops", [hist], 24)
        self.assertEqual(res["added"], 1, res)
        self.assertIn("notice: ignoring browser-history marker dated in the future", err)
        self.assertLess(self.raw_state()["capture_browser_last"][hist], future)
        res, err = self.quiet(gblog.capture_browser, self.ledger, "ops", [hist], 24)
        self.assertEqual(res["added"], 0, res)
        self.assertNotIn("dated in the future", err)
        # the library path clamps a negative window as well
        other = chrome_history(os.path.join(self.base, "chrome2"), [("https://b.example/y", "B", 60)])
        res, _err = self.quiet(gblog.capture_browser, self.ledger, "lib", [other], -1000000)
        marker = self.raw_state("lib")["capture_browser_last"][other]
        self.assertLess(marker, int((time.time() + 3600 + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000))

    @unittest.skipUnless(os.name == "posix", "pipe semantics are POSIX-specific")
    def test_exit_code_survives_closed_stdout(self):
        # D5-03: the verdict is not replaced by 0 when only the final flush hits a closed pipe
        self.seed("ops", 2)
        self.seed("fine", 1)
        self.tamper("ops", "ops record 1", "ops record X")

        def closed_stdout(argv):
            reader, writer = os.pipe()
            os.close(reader)
            try:
                proc = subprocess.run([sys.executable, GBLOG_PY, "--root", self.root] + argv,
                                      stdin=subprocess.DEVNULL, stdout=writer, stderr=subprocess.PIPE, timeout=120)
            finally:
                os.close(writer)
            return proc.returncode, proc.stderr.decode("utf-8", "replace")

        rc, err = closed_stdout(["verify", "--bot", "ops"])
        self.assertEqual(rc, 1, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Exception ignored", err)
        rc, err = closed_stdout(["export", "--bot", "fine"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)

    def test_verify_record_oserror_does_not_hide_other_chains(self):
        # D5-04
        self.seed("aaa", 1)
        self.seed("zzz", 1)
        real = gblog.Ledger.append

        def denied(ledger, bot, record, **kwargs):
            if bot == "aaa":
                raise PermissionError(errno.EACCES, "Permission denied", "/x/logs/aaa/day.jsonl")
            return real(ledger, bot, record, **kwargs)

        with mock.patch.object(gblog.Ledger, "append", denied):
            rc, out, err = run(["verify", "--all", "--record"], self.root)
            self.assertEqual(rc, 1, out + err)
            self.assertRegex(out, r"(?m)^OK\s+aaa\s")
            self.assertRegex(out, r"(?m)^OK\s+zzz\s")
            self.assertIn("      - verify record not written (Permission denied)", out.splitlines())
            self.assertIn("error: Permission denied: /x/logs/aaa/day.jsonl", err)
            rc, out, err = run(["verify", "--all", "--record", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        by = {entry["bot"]: entry for entry in json.loads(out)}
        self.assertEqual(sorted(by), ["aaa", "zzz"])
        self.assertEqual(by["aaa"]["record_error"], "verify record not written (Permission denied)")
        self.assertNotIn("record_error", by["zzz"])
        self.assertEqual(self.chain("zzz")[-1]["type"], "verify")

    def test_verify_record_exit_code_is_never_3(self):
        # D5-04: a refused record on one chain and a FAIL on another still give exit 1
        recs = self.seed("aaa", 2)
        self.ledger.write_state("aaa", {"seq": 99, "head": recs[-1]["hash"]})  # verify OK, append refused
        self.seed("bbb", 2)
        self.tamper("bbb", "bbb record 1", "bbb record X")
        rc, out, err = run(["verify", "--all", "--record"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^FAIL\s+bbb\s")
        self.assertIn("verify record not written", out)

    def test_impossible_dates_are_usage_errors(self):
        # D5-05
        self.seed("ops", 1)
        for argv in (["report", "--date", "2024-13-45", "--stdout"], ["report", "--date", "2024-02-30"],
                     ["report", "--since", "2024-00-10", "--stdout"], ["export", "--since", "2024-02-31"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("must be YYYY-MM-DD", err)
                self.assertEqual(out, "")
        self.assertEqual(os.listdir(self.path("reports")), [])
        rc, out, err = run(["report", "--date", "2024-02-29", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["export", "--since", DAY], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(out.splitlines()), 1)

    def test_tune_bot_run_keeps_all_bots_file(self):
        # D5-07
        with redirect_stderr(io.StringIO()):
            self.ledger.append("ops", {"type": "note", "detail": "one"})
            self.ledger.append("web", {"type": "note", "detail": "two"})
        rc, out, err = run(["tune"], self.root)
        self.assertEqual(rc, 0, out + err)
        first = [n for n in os.listdir(self.path("tuning")) if n.endswith(".md")]
        self.assertEqual(len(first), 1, first)
        with open(self.path("tuning", first[0]), "rb") as fh:
            before = fh.read()
        rc, out, err = run(["tune", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        with open(self.path("tuning", first[0]), "rb") as fh:
            self.assertEqual(fh.read(), before)
        scoped = [n for n in os.listdir(self.path("tuning")) if n.endswith("-ops.md")]
        self.assertEqual(len(scoped), 1, os.listdir(self.path("tuning")))

    @unittest.skipUnless(os.name == "posix", "a file name with an escape character")
    def test_anchor_fail_line_has_no_escapes(self):
        # D5-08: the break descriptor names a file as it is called on disk
        self.seed("ops", 1)
        rec = {"v": 1, "seq": 2, "ts": stamp(5), "bot": "ops", "type": "note", "detail": "stray", "prev": "ab" * 32}
        rec["hash"] = gblog.compute_hash(rec)
        stray = self.path("logs", "ops", "zz\x1b[2J.jsonl")
        try:
            with open(stray, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            self.skipTest("this filesystem does not take such a file name")
        self.ledger.write_state("ops", {"seq": 2, "head": rec["hash"]})
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("ANCHOR FAIL ops", out)
        self.assertIn("seq 1->2", out)
        self.assertNotIn("\x1b", out)

    def test_root_tilde_is_expanded(self):
        # D5-12
        home = os.path.join(self.base, "home")
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            self.assertEqual(gblog.Ledger("~/x").root, os.path.join(home, "x"))
            with mock.patch.dict(os.environ, {"GBLOG_ROOT": "~/y"}):
                self.assertEqual(gblog.Ledger().root, os.path.join(home, "y"))
        self.assertEqual(gblog.Ledger(self.root).root, self.root)

    def test_task_start_without_id_is_not_an_open_task(self):
        # D5-13 / D6-10
        rc, out, err = run(["log", "--bot", "ops", "--type", "task_start", "--task", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("task_id", self.chain()[-1])
        self.assertEqual(self.status_rows()["ops"]["open_tasks"], 0)
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "real"], self.root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status_rows()["ops"]["open_tasks"], 1)
        rc, out, err = run(["task-end", "--bot", "ops", "--outcome", "ok"], self.root)  # closes no task
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.status_rows()["ops"]["open_tasks"], 1)


# --------------------------------------------------------------------------- DR6: archive
class ArchiveTest(Base):
    KEY_SHAPED = ("xai-research-assistant", "sk-customer-support-bot", "sk_test_environment")

    def test_archive_path_survives_key_shaped_bot_names(self):
        # D6-01: the generated content address is not redacted
        for bot in self.KEY_SHAPED:
            with self.subTest(bot=bot):
                data = ("evidence for %s\n" % bot).encode("utf-8")
                rec, _err = self.quiet(gblog.archive_file, self.ledger, bot, data, "e.txt", "artifact", ts=stamp(0))
                self.assertEqual(rec["path"], "archive/%s/%s/%s.gz" % (bot, DAY, rec["sha256"]))
                self.assertTrue(os.path.isfile(self.path(*rec["path"].split("/"))))
                self.assertEqual(self.chain(bot)[-1]["path"], rec["path"])
                res = self.ledger.verify(bot)
                self.assertTrue(res["ok"], res["problems"])
                self.assertEqual((res["archives"], res["warnings"]), (1, []))
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_archive_target_is_still_redacted(self):
        # D6-01: only the address is exempt
        bot = self.KEY_SHAPED[0]
        rec, _err = self.quiet(gblog.archive_file, self.ledger, bot, b"x\n", "token=abcdef123456.txt", ts=stamp(0))
        self.assertIn("[REDACTED]", rec["target"])
        self.assertNotIn("abcdef123456", rec["target"])
        self.assertTrue(rec["path"].startswith("archive/%s/" % bot))
        # a path that is not a well-formed address is redacted like any other text
        out, _err = self.quiet(self.ledger.append, "ops", {"type": "archive", "path": "password=hunter2hunter2",
                                                            "ts": stamp(0)})
        self.assertNotIn("hunter2", out["path"])

    def test_spooled_archive_event_keeps_its_path(self):
        # D6-01: the same rule applies to the copy in the spool
        bot = self.KEY_SHAPED[0]
        self.seed(bot, 1)
        self.break_state(bot)
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            gblog.archive_file(self.ledger, bot, b"spooled evidence\n", "s.txt", "artifact")
        with open(ctx.exception.spool_path, encoding="utf-8") as fh:
            stored = json.load(fh)["record"]["path"]
        self.assertTrue(stored.startswith("archive/%s/" % bot), stored)
        self.assertTrue(os.path.isfile(self.path(*stored.split("/"))))
        self.restore_state(bot)
        res, _err = self.quiet(self.ledger.spool_replay, bot)
        self.assertEqual([r["path"] for r in res["replayed"]], [stored])
        check = self.ledger.verify(bot)
        self.assertTrue(check["ok"], check["problems"])
        self.assertEqual(check["archives"], 1)

    def test_redacted_archive_path_of_old_record_is_resolved(self):
        # D6-01, step 2: a record written by 0.6.1 names archive/[REDACTED]/...; the file is under the chain's name
        bot = self.KEY_SHAPED[0]
        rec, _err = self.quiet(gblog.archive_file, self.ledger, bot, b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        old_path = "archive/[REDACTED]/%s/%s.gz" % (DAY, rec["sha256"])
        self.assertEqual(gblog.redact(rec["path"]), old_path)  # what the older version stored
        self.quiet(self.ledger.append, bot, {"type": "archive", "action": "file", "target": "e.txt", "outcome": "ok",
                                             "kind": "artifact", "sha256": rec["sha256"], "bytes": rec["bytes"],
                                             "path": old_path, "ts": stamp(1)})
        self.assertEqual(self.chain(bot)[-1]["path"], old_path)
        res = self.ledger.verify(bot)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 2)
        # the same record in a chain whose name is not redacted finds nothing
        self.quiet(self.ledger.append, "ops", {"type": "archive", "action": "file", "target": "e.txt", "outcome": "ok",
                                               "sha256": rec["sha256"], "bytes": rec["bytes"], "path": old_path,
                                               "ts": stamp(1)})
        res = self.ledger.verify("ops")
        self.assertEqual(res["problems"], [old_path + ": archived file missing"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is not available")
    def test_fifo_at_archive_address_fails_not_hangs(self):
        # D6-02
        rec, _err = self.quiet(gblog.archive_file, self.ledger, "aaa", b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        self.seed("zzz", 1)
        full = self.path(*rec["path"].split("/"))
        os.remove(full)
        try:
            os.mkfifo(full)
        except OSError:
            self.skipTest("FIFOs are not supported here")
        rc, out, err = run_cli(["verify", "--all"], self.root, timeout=60)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - %s: not a regular file" % rec["path"], out.splitlines())
        self.assertRegex(out, r"(?m)^FAIL\s+aaa\s")
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is not available")
    def test_fifo_state_and_alias_files_do_not_hang(self):
        # D6-02
        self.seed("ops", 2)
        state = self.path("state", "ops.json")
        os.remove(state)
        try:
            os.mkfifo(state)
        except OSError:
            self.skipTest("FIFOs are not supported here")
        for argv in (["status"], ["tail"], ["verify", "--bot", "ops"]):
            with self.subTest(argv=argv, fifo="state"):
                rc, out, err = run_cli(argv, self.root, timeout=60)
                self.assertEqual(rc, 0, out + err)
        self.assertIn("state/ops.json missing or unreadable", out)
        os.remove(state)
        os.mkfifo(self.path("state", "aliases.json"))
        for argv in (["status"], ["tail"]):
            with self.subTest(argv=argv, fifo="aliases"):
                rc, out, err = run_cli(argv, self.root, timeout=60)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("state/aliases.json is unreadable", err)


# --------------------------------------------------------------------------- DR6: readers and the alias registry
class ReaderAliasTest(Base):
    def test_non_string_bot_record_is_reported_by_directory(self):
        # D6-03: verify says OK, so the record must not vanish from the readers
        first = self.seed("ops", 1)[0]
        rec = {"v": 1, "seq": 2, "ts": stamp(1), "bot": ["ops"], "type": "action", "action": "shell",
               "target": "rm -rf /backup", "outcome": "ok", "prev": first["hash"]}
        rec["hash"] = gblog.compute_hash(rec)
        with open(self.ledger.log_files("ops")[0], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self.ledger.write_state("ops", {"seq": 2, "head": rec["hash"], "updated": rec["ts"]})
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["report", "--date", DAY, "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("| Records | 2 |", out)
        self.assertIn("rm -rf /backup", out)
        self.assertNotIn("skipped", err)
        rc, out, err = run(["tail"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("rm -rf /backup", out)
        rc, out, err = run(["export"], self.root)
        self.assertEqual(rc, 0, out + err)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertEqual(rows[1], rec)  # export prints the record as stored
        got = self.ledger.records("ops")
        self.assertEqual((got[1]["bot"], got[1]["bot_field"]), ("ops", ["ops"]))
        self.assertEqual(self.status_rows()["ops"]["seq"], 2)

    def test_sanitised_name_resolves_to_registered_slug(self):
        # D6-04: resolve() is idempotent; no variant chain for a name that sanitises to a slug
        rc, out, err = run(["alias", "add", "--slug", "ops-bot", "--display", "Ops Bot"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["note", "--bot", "OPS BOT", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("not registered", err)
        self.assertEqual(os.listdir(self.path("logs")), ["ops-bot"])
        rc, out, err = run(["alias", "strict", "on"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["note", "--bot", "Ops  Bot!", "--detail", "y"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.listdir(self.path("logs")), ["ops-bot"])
        self.assertEqual(len(self.chain("ops-bot")), 2)
        for name in ("OPS BOT", "ops-bot", "Ops Bot", "Ops  Bot!", "stranger x"):
            once = self.ledger.resolve(name)[0]
            self.assertEqual(self.ledger.resolve(once)[0], once, name)
        self.assertEqual(self.ledger.resolve("stranger x"), ("stranger-x", False))

    def test_slug_cannot_shadow_another_display_name(self):
        # D6-05
        rc, out, err = run(["alias", "add", "--slug", "chief", "--display", "Ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        with open(self.ledger.aliases_path(), "rb") as fh:
            before = fh.read()
        rc, out, err = run(["alias", "add", "--slug", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("'ops' is the display name of chief", err)
        with open(self.ledger.aliases_path(), "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.ledger.resolve("Ops"), ("chief", True))
        # extending an existing Bot is not a new slug
        rc, out, err = run(["alias", "add", "--slug", "chief", "--alias", "cos"], self.root)
        self.assertEqual(rc, 0, out + err)
        # a registry that already holds the pair is listed under conflicts
        self.write_json(self.ledger.aliases_path(), {"strict": False, "bots": {
            "chief": {"display": "Ops", "aliases": []}, "ops": {"display": "ops", "aliases": []}}})
        rc, out, err = run(["alias", "list"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("conflicts:", out)
        self.assertIn("slug 'ops' shadows the display name of chief", out)

    def test_chains_for_follows_resolution_in_conflicting_registry(self):
        # D6-06: `holder` lists another Bot's slug as an alias; the slug wins for readers as it does for writers
        self.write_json(self.ledger.aliases_path(), {"strict": False, "bots": {
            "holder": {"display": "Holder", "aliases": ["mmm"]},
            "mmm": {"display": "Shared Name", "aliases": []}}})
        with redirect_stderr(io.StringIO()):
            self.ledger.append("mmm", {"type": "note", "detail": "mmm secret work", "ts": stamp(0)})
            self.ledger.append("holder", {"type": "note", "detail": "holder work", "ts": stamp(1)})
        self.assertEqual(self.ledger.chains_for("holder"), ["holder"])
        self.assertEqual(self.ledger.chains_for("mmm"), ["mmm"])
        for argv in (["report", "--bot", "holder", "--date", DAY, "--stdout"], ["tail", "--bot", "holder"],
                     ["export", "--bot", "holder"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("holder work", out)
                self.assertNotIn("mmm secret work", out)

    def test_alias_list_shows_case_variant_chains_and_sorted_unregistered(self):
        # D6-06 / D5-10
        with redirect_stderr(io.StringIO()):
            for name in ("zeta", "alpha", "mid", "Ops"):
                self.ledger.append(name, {"type": "note", "detail": "x", "ts": stamp(0)})
            self.ledger.add_alias("ops", "Ops Bot", [])
        rc, out, err = run(["alias", "list"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("unregistered chains: alpha, mid, zeta\n", out)
        on_disk = [line for line in out.splitlines() if "chains on disk:" in line]
        self.assertEqual(len(on_disk), 1, out)
        self.assertTrue(on_disk[0].endswith("chains on disk: Ops"), on_disk[0])

    def test_alias_add_entry_without_display(self):
        # D5-10: a hand-written entry without `display` no longer ends in "internal error" after the write
        self.write_json(self.ledger.aliases_path(), {"strict": False, "bots": {"ops": {"aliases": []}}})
        rc, out, err = run(["alias", "add", "--slug", "ops", "--alias", "old"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out, "ops: display=ops aliases=old\n")
        self.assertNotIn("internal error", err)
        self.assertEqual(self.ledger.resolve("old"), ("ops", True))
        rc, out, err = run(["alias", "list"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("None", out)


# --------------------------------------------------------------------------- DR6: report rendering
def _rec(n, **fields):
    rec = {"v": 1, "seq": n, "ts": "%sT09:00:%02dZ" % (DAY, n - 1), "bot": "ops"}
    rec.update(fields)
    return rec


class ReportRenderingTest(unittest.TestCase):
    def test_report_names_housekeeping_records_by_type(self):
        # D6-07
        text = gblog.render_report([
            _rec(1, type="verify", action="other", target="chain", outcome="ok", detail="2 records verified"),
            _rec(2, type="sync", action="other", target="git", outcome="pending", detail="1 changed path(s) before commit"),
            _rec(3, type="archive", action="file", target="e.txt", outcome="ok"),
            _rec(4, type="tune", action="other", target="abcd1234", outcome="ok", detail="applied"),
            _rec(5, type="action", action="shell", target="ls", outcome="ok", detail="listed"),
            _rec(6, type="approval", target="send mail", outcome="denied", decision="denied"),
            _rec(7, type="note", detail="why"),
        ], "r")
        lines = text.splitlines()
        self.assertIn("- 09:00:00 `verify` chain \u2014 2 records verified", lines)
        self.assertIn("- 09:00:01 `sync` git **PENDING** \u2014 1 changed path(s) before commit", lines)
        self.assertIn("- 09:00:02 `archive` e.txt", lines)
        self.assertIn("- 09:00:03 `tune` abcd1234 \u2014 applied", lines)
        # records written by their own commands render as before
        self.assertIn("- 09:00:04 `shell` ls \u2014 listed", lines)
        self.assertIn("- 09:00:05 `approval` send mail **DENIED**", lines)
        self.assertIn("- 09:00:06 `note` \u2014 why", lines)
        self.assertNotIn("`other`", text)
        self.assertNotIn("`file`", text)

    def test_report_shows_task_end_summary(self):
        # D6-08
        text = gblog.render_report([
            _rec(1, type="task_start", task_id="t1", task="Reconcile"),
            _rec(2, type="action", task_id="t1", action="shell", target="ls", outcome="ok"),
            _rec(3, type="task_end", task_id="t1", outcome="ok", detail="3 invoices reconciled"),
            _rec(4, type="task_start", task_id="t2", task="Quiet"),
            _rec(5, type="task_end", task_id="t2", outcome="ok"),
        ], "r")
        lines = text.splitlines()
        self.assertIn("- 09:00:02 `task_end` \u2014 3 invoices reconciled", lines)
        self.assertEqual([line for line in lines if "`task_start`" in line], [])
        self.assertEqual(len([line for line in lines if "`task_end`" in line]), 1)
        self.assertIn("### Quiet  `t2`", lines)
        self.assertIn("- 09:00:01 `shell` ls", lines)

    def test_open_task_hint_needs_same_title_and_other_chain(self):
        # D6-09
        same_chain = gblog.render_report([
            _rec(1, type="task_start", task_id="t5", task="Reconcile invoices 5"),
            _rec(2, type="task_start", task_id="t6", task="Reconcile invoices 6"),
            _rec(3, type="task_end", task_id="t6", outcome="ok"),
            _rec(4, type="task_start", task_id="t7", task="Reconcile invoices 5"),
            _rec(5, type="task_end", task_id="t7", outcome="ok"),
        ], "r")
        self.assertIn("outcome **open**", same_chain)
        self.assertNotIn("likely closed", same_chain)
        cross_chain = gblog.render_report([
            _rec(1, type="task_start", task_id="a1", task="Reconcile invoices", chain="cos"),
            _rec(2, type="task_start", task_id="a2", task="Reconcile  Invoices", chain="cos-primary"),
            _rec(3, type="task_end", task_id="a2", outcome="ok", chain="cos-primary"),
            _rec(4, type="task_start", task_id="a3", task="Reconcile invoices 2", chain="cos-primary"),
            _rec(5, type="task_end", task_id="a3", outcome="ok", chain="cos-primary"),
        ], "r")
        self.assertIn("outcome **open** \u2014 likely closed as `a2` in chain `cos-primary` (ok)", cross_chain)
        self.assertEqual(cross_chain.count("likely closed"), 1)


if __name__ == "__main__":
    unittest.main()
