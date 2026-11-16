"""Tests for test review K6 and the open G2 items of the round-4 audit (2026-11-16).

* K6-01: the strict runner; K6-03: crash points of ``append``; K6-04: the window after verify's snapshot
  lock; K6-05: git children and repository variables; K6-07: UTC-day boundaries; K6-08: durability order;
  K6-09: races and platforms without ``fcntl``; K6-16: private modes of everything gblog creates.
* G2-03: ``append`` opens no day file but the newest; G2-04: a ledger shaped like the output of 0.5;
  G2-07: a file system that ignores letter case.

Everything runs in temporary directories. No network is used: the only git remotes are local.
"""
import io
import json
import os
import shutil
import stat
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
from _support import DAY, LedgerCase, after_release, build_legacy_ledger, chrome_history  # noqa: E402
from _support import crash_at, frozen_clock, git, stamp, webkit_time  # noqa: E402
from _support import run_strict as run  # noqa: E402

GIT = shutil.which("git")
HEAD_MISMATCH = "state/%s.json: head does not match last record (state tampered or log truncated)"
REPLACED = "logs/ops/%s.jsonl: log file was replaced or shortened while it was being verified" % DAY


def _ignores_case(directory):
    """True when ``directory`` is on a file system that ignores letter case (macOS and Windows defaults)."""
    probe = tempfile.mkdtemp(prefix="GbCase", dir=directory)
    try:
        return os.path.isdir(os.path.join(os.path.dirname(probe), os.path.basename(probe).swapcase()))
    finally:
        os.rmdir(probe)


# --------------------------------------------------------------------------- K6-01
class StrictRunnerTest(LedgerCase):
    def test_run_strict_rejects_a_swallowed_crash(self):
        def crashed(argv):
            sys.stderr.write("error: internal error (X); set GBLOG_DEBUG=1 for a traceback\n")
            return 1

        with mock.patch.object(gblog, "main", crashed):
            with self.assertRaises(AssertionError):
                run(["status"], self.root)
            rc, out, err = run(["status"], self.root, internal_ok=True)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("error: internal error (X)", err)

    def test_run_strict_rejects_an_aborted_chain(self):
        self.seed("ops", 1)
        with mock.patch.object(gblog.Ledger, "verify", side_effect=RuntimeError("boom")):
            with self.assertRaises(AssertionError):
                run(["verify", "--all"], self.root)  # exit 1, like an integrity FAIL
            rc, out, err = run(["verify", "--all"], self.root, internal_ok=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("verify aborted: RuntimeError: boom", out)
        rc, out, err = run(["verify", "--all"], self.root)  # a healthy run passes the strict runner
        self.assertEqual(rc, 0, out + err)


# --------------------------------------------------------------------------- K6-05
class GitEnvTest(LedgerCase):
    POINTERS = {"GIT_DIR": "/x/.git", "GIT_WORK_TREE": "/x", "GIT_INDEX_FILE": "/x/.git/index",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/x/hooks"}

    def test_git_children_do_not_inherit_repository_variables(self):
        # D3-09: the ledger repository is the one at the ledger root, with the configuration read from files
        calls = []

        def fake(argv, *args, **kwargs):
            calls.append(kwargs)
            return gblog.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.dict(os.environ, self.POINTERS), mock.patch.object(gblog.subprocess, "run", side_effect=fake):
            gblog._git(self.root, "status", "--porcelain")
            for key, value in self.POINTERS.items():
                self.assertEqual(os.environ[key], value)  # only the child's environment is changed
        self.assertEqual(len(calls), 1)
        self.assertEqual([key for key in self.POINTERS if key in calls[0]["env"]], [])

    @unittest.skipUnless(GIT, "git not installed")
    def test_sync_ignores_an_inherited_git_dir(self):
        # what a pre-commit hook or `git rebase -x` exports must not point sync at another repository
        self.seed("ops", 1)
        decoy = os.path.join(self.base, "decoy.git")
        git("init", "--bare", "-q", decoy)
        index = os.path.join(self.base, "decoy.index")
        with mock.patch.dict(os.environ, {"GIT_DIR": decoy, "GIT_INDEX_FILE": index}):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(git("-C", self.root, "rev-list", "--all", "--count").strip(), "1")
        self.assertEqual(git("--git-dir", decoy, "rev-list", "--all", "--count").strip(), "0")
        self.assertFalse(os.path.exists(index))


# --------------------------------------------------------------------------- G2-03
class AppendReadsTest(LedgerCase):
    def test_append_opens_no_day_file_but_the_newest(self):
        # DESIGN section 13: the cost of an append does not grow with the number of day files
        for day in ("2024-06-01", "2024-06-02", "2024-06-03"):
            self.quiet(self.ledger.append, "ops", {"type": "note", "detail": day, "ts": day + "T10:00:00Z"})
        opened, real = [], gblog._open_regular

        def spy(path):
            opened.append(os.path.abspath(path))
            return real(path)

        with mock.patch.object(gblog, "_open_regular", spy):
            rec = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "x", "ts": "2024-06-03T11:00:00Z"})
        self.assertEqual(rec["seq"], 4)
        logs = [p for p in opened if p.startswith(self.path("logs") + os.sep)]
        self.assertEqual(logs, [self.path("logs", "ops", "2024-06-03.jsonl")])
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- G2-04
class LegacyLedgerTest(LedgerCase):
    TS_LINES = ["logs/ops/2024-06-01.jsonl:%d: ts is not in normalised form" % n for n in (1, 2)]

    def test_a_ledger_shaped_like_0_5_keeps_working(self):
        # K3-08: free-form ts, the single integer browser marker, an alias entry without `aliases`,
        # and a spool file under the old name
        now = int(time.time())
        recs = build_legacy_ledger(self.root, browser_marker=webkit_time(now - 3600))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["head"], res["warnings"]), (2, recs[-1]["hash"], []))
        self.assertEqual(self.ledger.verify("ops", strict=True)["problems"], self.TS_LINES)

        # append: the tail carries a UTC offset; the new record still sorts after it
        rec = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "after the upgrade"})
        self.assertEqual((rec["seq"], rec["prev"]), (3, recs[-1]["hash"]))
        self.assertNotIn("clock_regressed", rec)

        # capture browser: the 0.2 marker is honoured (the older visit is not imported) and replaced
        hist = chrome_history(os.path.join(self.base, "chrome"), [("https://old.example/a", "Old", 7200),
                                                                  ("https://new.example/b", "New", 60)])
        rc, out, err = run(["capture", "browser", "--bot", "ops", "--history", hist], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("browser: 1 new visit(s) from 1 history file(s)", out)
        self.assertEqual(self.chain()[-1]["target"], "https://new.example/b")
        with open(self.path("state", "ops.json"), encoding="utf-8") as fh:
            markers = json.load(fh)["capture_browser_last"]
        self.assertEqual(list(markers), [hist])

        rc, out, err = run(["alias", "list"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("strict: off", out)
        self.assertIn("Legacy Bot", out)
        self.assertIn("chains on disk: ops", out)

        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("replayed 1 spooled event(s) for ops; alert resolved", out)
        last = self.chain()[-1]
        self.assertEqual((last["detail"], last["spool_id"], last["spooled_at"]),
                         ("refused by 0.5", "20240601T120000Z-deadbeef", "2024-06-01T12:00:00Z"))
        self.assertFalse(os.path.exists(self.path("state", "ops.alert.json")))

        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["warnings"]), (5, []))
        self.assertEqual(self.ledger.verify("ops", strict=True)["problems"], self.TS_LINES)


# --------------------------------------------------------------------------- G2-07
@unittest.skipUnless(_ignores_case(tempfile.gettempdir()), "needs a file system that ignores letter case")
class CaseInsensitiveVolumeTest(LedgerCase):
    def test_write_uses_the_spelling_of_the_existing_directory(self):
        # K1-09, K3-03 on a real volume (the macOS CI job); elsewhere only mocks cover _disk_name
        self.seed("Ops", 1)
        rec = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "second spelling"})
        self.assertEqual(rec["bot"], "Ops")
        self.assertEqual([r["bot"] for r in self.chain("Ops")], ["Ops", "Ops"])
        res = self.ledger.verify("Ops")
        self.assertEqual((res["ok"], res["warnings"]), (True, []))
        rc, out, err = run(["verify", "--bot", "ops", "--contains", rec["hash"]], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("bot field", out)
        self.assertNotIn("      ! ", out)


# --------------------------------------------------------------------------- K6-04
class SnapshotWindowTest(LedgerCase):
    """verify takes file sizes under the shared lock and parses after releasing it (K4-02)."""

    def setUp(self):
        super().setUp()
        self.recs = self.seed("ops", 3)
        self.log = self.ledger.log_files("ops")[0]

    def _verify_after(self, action):
        with after_release(action) as fired:
            res = self.ledger.verify("ops")
        self.assertEqual(fired, [True], "the hook did not run: verify no longer takes Ledger.lock(bot, shared=True)")
        return res

    def test_day_file_cut_after_snapshot_is_a_problem(self):
        def cut():
            with open(self.log, "r+b") as fh:
                fh.truncate(os.path.getsize(self.log) // 2)

        res = self._verify_after(cut)
        self.assertFalse(res["ok"], res)
        self.assertIn(REPLACED, res["problems"])

    def test_day_file_swapped_after_snapshot_is_a_problem(self):
        def swap():  # the same bytes under a new inode: the check is on the file, not on its content
            with open(self.log, "rb") as fh:
                data = fh.read()
            with open(self.log + ".new", "wb") as fh:
                fh.write(data)
            os.replace(self.log + ".new", self.log)

        res = self._verify_after(swap)
        self.assertFalse(res["ok"], res)
        self.assertIn(REPLACED, res["problems"])

    def test_later_day_file_is_not_part_of_the_snapshot(self):
        def later():
            self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "next day", "ts": stamp(0, "2024-06-02")})

        res = self._verify_after(later)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["head"]), (3, self.recs[-1]["hash"]))
        self.assertEqual(self.ledger.verify("ops")["records"], 4)

    # bug: K6-04. A day file removed between the snapshot and the parse raises FileNotFoundError out of
    # Ledger.verify (seal and state rebuild then exit 2 without naming a chain) where a problem line belongs.
    @unittest.expectedFailure
    def test_day_file_deleted_after_snapshot_is_a_problem(self):
        res = self._verify_after(lambda: os.remove(self.log))
        self.assertFalse(res["ok"], res)
        self.assertTrue(any("logs/ops/%s.jsonl" % DAY in p for p in res["problems"]), res["problems"])


# --------------------------------------------------------------------------- K6-07
class ClockBoundaryTest(LedgerCase):
    """Per-UTC-day behaviour, tested at the boundary on purpose (and never by accident at midnight)."""

    def test_archive_dedup_is_per_utc_day(self):
        data = b"the same evidence\n"
        with frozen_clock("2024-06-01T23:59:59Z"):
            first = self.quiet(gblog.archive_file, self.ledger, "ops", data, "e.txt")
            again = self.quiet(gblog.archive_file, self.ledger, "ops", data, "e.txt")
        with frozen_clock("2024-06-02T00:00:01Z"):
            next_day = self.quiet(gblog.archive_file, self.ledger, "ops", data, "e.txt")
        self.assertEqual((first["_dedup"], again["_dedup"], next_day["_dedup"]), (False, True, False))
        self.assertEqual(first["path"], again["path"])
        self.assertIn("/2024-06-01/", first["path"])
        self.assertIn("/2024-06-02/", next_day["path"])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 3)

    def test_report_default_day_is_the_current_utc_day(self):
        with frozen_clock("2024-06-01T23:59:59Z"):
            rc, out, err = run(["task-start", "--bot", "ops", "--task", "Late task"], self.root)
            self.assertEqual(rc, 0, out + err)
        with frozen_clock("2024-06-02T00:00:01Z"):
            rc, out, err = run(["report", "--stdout"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(out.startswith("# Activity report 2024-06-02\n"), out[:60])
            self.assertIn("_No records._", out)
            rc, out, err = run(["report", "--date", "yesterday", "--stdout"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(out.startswith("# Activity report 2024-06-01\n"), out[:60])
            self.assertIn("### Late task", out)


# --------------------------------------------------------------------------- K6-09 (a), (b), (e)
class RaceAndPlatformTest(LedgerCase):
    def test_state_rebuild_refuses_when_the_log_changes_under_it(self):
        self.seed("ops", 2)
        os.remove(self.path("state", "ops.json"))
        real, fired = gblog.Ledger.verify, []

        def racing(ledger, bot, *args, **kwargs):
            res = real(ledger, bot, *args, **kwargs)
            if not fired:  # another process appends between the unlocked walk and the locked re-check
                fired.append(True)
                with redirect_stderr(io.StringIO()):
                    gblog.Ledger(self.root).append("ops", {"type": "note", "detail": "interloper", "ts": stamp(5)})
            return res

        with mock.patch.object(gblog.Ledger, "verify", racing):
            with self.assertRaises(gblog.StateRebuildError) as ctx:
                self.ledger.rebuild_state("ops")
        self.assertEqual(str(ctx.exception),
                         "refusing: the log changed while it was being checked; run the command again")
        details = [r["detail"] for r in self.chain()]
        self.assertEqual(details[-1], "interloper")
        self.assertFalse([d for d in details if d.startswith("state rebuilt")])
        res = self.ledger.rebuild_state("ops")  # the interloper adopted the tail: nothing is left to repair
        self.assertIs(res["noop"], True)
        self.assertEqual(len(self.chain()), 3)

    def test_update_state_writes_nothing_over_a_torn_tail(self):
        # what keeps sync and capture browser from writing a genesis state over a damaged chain
        self.seed("ops", 2)
        with open(self.ledger.log_files("ops")[0], "ab") as fh:
            fh.write(b'{"torn": \n')
        os.remove(self.path("state", "ops.json"))
        before = self.log_bytes()
        wrote, err = self.quiet_err(self.ledger.update_state, "ops", {"last_sync": {"ts": "x", "commit": "y"}})
        self.assertIs(wrote, False)
        self.assertIn("warning: state/ops.json is missing or unreadable and the log tail is not "
                      "self-consistent; state not written", err)
        self.assertFalse(os.path.exists(self.path("state", "ops.json")))
        rc, out, err = run(["note", "--bot", "ops", "--detail", "must not restart the chain"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(self.log_bytes(), before)
        self.assertFalse(os.path.exists(self.path("state", "ops.json")))

    def test_cli_flow_without_fcntl(self):
        # "where fcntl is unavailable, gblog runs without locks": no CI platform lacks it, so it is patched away
        src = os.path.join(self.base, "evidence.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("evidence\n")
        with mock.patch.object(gblog, "fcntl", None):
            rc, tid, err = run(["task-start", "--bot", "ops", "--task", "No locks"], self.root)
            self.assertEqual(rc, 0, err)
            tid = tid.strip()
            for argv in (["log", "--bot", "ops", "--task-id", tid, "--action", "shell", "--target", "ls"],
                         ["archive", "--bot", "ops", "--task-id", tid, "--file", src],
                         ["task-end", "--bot", "ops", "--task-id", tid],
                         ["alias", "add", "--slug", "ops", "--display", "Ops"],
                         ["verify", "--all"], ["anchor"], ["status"]):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, "%s: %s%s" % (argv, out, err))
            self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
            rc, out, err = run(["note", "--bot", "ops", "--detail", "refused"], self.root)
            self.assertEqual(rc, 4, out + err)
        self.assertEqual([r["type"] for r in self.chain()], ["task_start", "action", "archive", "task_end"])


# --------------------------------------------------------------------------- K6-08
class DurabilityOrderTest(LedgerCase):
    def _record(self):
        """Patches that record ("fsync", inode), ("replace", path) and ("line", path) in call order."""
        events = []
        real_fsync, real_replace, real_line = os.fsync, os.replace, gblog.Ledger._append_line

        def fsync(fd):
            st = os.fstat(fd)
            events.append(("fsync", (st.st_dev, st.st_ino)))
            return real_fsync(fd)

        def replace(src, dst, *args, **kwargs):
            events.append(("replace", os.path.abspath(dst)))
            return real_replace(src, dst, *args, **kwargs)

        def line(ledger, path, text):
            events.append(("line", os.path.abspath(path)))
            return real_line(ledger, path, text)

        patches = (mock.patch.object(gblog.os, "fsync", fsync), mock.patch.object(gblog.os, "replace", replace),
                   mock.patch.object(gblog.Ledger, "_append_line", line))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return events

    @staticmethod
    def _ident(path):
        st = os.stat(path)
        return (st.st_dev, st.st_ino)

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-specific")
    def test_archive_is_durable_before_it_is_chained(self):
        events = self._record()
        rec = self.quiet(gblog.archive_file, self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        full = self.path(*rec["path"].split("/"))
        order = [events.index(("fsync", self._ident(full))),                   # the temporary file that became the .gz
                 events.index(("replace", full)),
                 events.index(("fsync", self._ident(os.path.dirname(full)))),  # its directory entry
                 events.index(("line", self.path("logs", "ops", DAY + ".jsonl")))]
        self.assertEqual(order, sorted(order), events)

    def test_refusal_writes_the_alert_before_the_spool_file(self):
        # `status` reads the alert: it must exist even when the spool write is the one that fails
        self.seed("ops", 1)
        self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        events = self._record()
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            self.ledger.append("ops", {"type": "note", "detail": "refused"})
        alert = events.index(("replace", os.path.abspath(ctx.exception.alert_path)))
        spool = events.index(("replace", os.path.abspath(ctx.exception.spool_path)))
        self.assertLess(alert, spool, events)
        self.assertFalse([e for e in events if e[0] == "line"])  # nothing was written under logs/

    def test_first_record_writes_a_genesis_state_before_the_line(self):
        # D2-09: an interrupted first append is "one behind", never "state missing"
        events = self._record()
        self.quiet(self.ledger.append, "fresh", {"type": "note", "detail": "one", "ts": stamp(0)})
        state = ("replace", self.path("state", "fresh.json"))
        marks = [e for e in events if e == state or e[0] == "line"]
        self.assertEqual(marks, [state, ("line", self.path("logs", "fresh", DAY + ".jsonl")), state])


# --------------------------------------------------------------------------- K6-03
@unittest.skipUnless(hasattr(os, "fork"), "os.fork is not available")
class CrashPointTest(LedgerCase):
    """The crash table of DESIGN section 4, enumerated: the appending process is killed at every os call.

    I1: verify never raises; it is OK, or its one problem is the state-head mismatch (state one record
        behind), or, after a torn write, ``not valid JSON`` on the last line.
    I2: the next append succeeds and the chain verifies without warnings, with the crashed record in it at
        most once; after a torn line it is refused instead and the log is left as it is.
    I4: no record carries ``state_adopted``."""

    def _check(self, root, bot, label):
        ledger = gblog.Ledger(root)
        _path, tail = ledger._tail_record(bot)
        torn = tail is not None and "_corrupt" in tail
        res = ledger.verify(bot)
        files = ledger.log_files(bot)
        before = [open(p, "rb").read() for p in files]
        if torn:
            self.assertEqual(len(res["problems"]), 1, label)
            self.assertIn("not valid JSON", res["problems"][0], label)
            with self.assertRaises(gblog.TailMismatchError), redirect_stderr(io.StringIO()):
                ledger.append(bot, {"type": "note", "detail": "next", "ts": stamp(6)})
            self.assertEqual([open(p, "rb").read() for p in files], before, label)
            return
        self.assertIn(res["problems"], ([], [HEAD_MISMATCH % bot]), label)
        with redirect_stderr(io.StringIO()):
            ledger.append(bot, {"type": "note", "detail": "next", "ts": stamp(6)})
        res = ledger.verify(bot)
        self.assertEqual((res["ok"], res["problems"], res["warnings"]), (True, [], []), label)
        recs = [rec for _p, _n, rec in ledger.iter_records(bot)]
        details = [rec.get("detail") for rec in recs]
        self.assertLessEqual(details.count("crashed"), 1, label)
        self.assertEqual(details[-1], "next", label)
        self.assertEqual([rec["seq"] for rec in recs], list(range(1, len(recs) + 1)), label)
        self.assertFalse([rec for rec in recs if "state_adopted" in rec], label)

    def _enumerate(self, bot, half_write):
        pristine = os.path.join(self.base, "pristine")
        if not os.path.isdir(pristine):
            ledger = gblog.Ledger(pristine)
            with redirect_stderr(io.StringIO()):
                ledger.init()
                for i in range(2):
                    ledger.append("ops", {"type": "note", "detail": "seed %d" % i, "ts": stamp(i)})

        def scenario(ledger):
            ledger.append(bot, {"type": "note", "detail": "crashed", "ts": stamp(5)})

        died = 0
        for at in range(1, 100):
            root = os.path.join(self.base, "case-%s-%d-%d" % (bot, half_write, at))
            shutil.copytree(pristine, root)
            code = crash_at(root, scenario, at, half_write)
            if code == 0:
                break
            self.assertEqual(code, 77, "the scenario raised at point %d instead of running" % at)
            died += 1
            self._check(root, bot, "%s, crash at os call %d, half_write=%s" % (bot, at, half_write))
            shutil.rmtree(root, True)
        else:
            self.fail("the scenario never completed")
        self.assertGreaterEqual(died, 6, "append made fewer os calls than the state machine needs")

    def test_append_to_an_existing_chain(self):
        self._enumerate("ops", False)

    def test_append_to_an_existing_chain_with_a_torn_write(self):
        self._enumerate("ops", True)

    def test_first_record_of_a_chain(self):
        self._enumerate("fresh", False)

    def test_first_record_of_a_chain_with_a_torn_write(self):
        self._enumerate("fresh", True)


# --------------------------------------------------------------------------- K6-16
@unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
class PrivateModeTest(LedgerCase):
    def _workflow(self, root, work, src):
        for argv in (["init"], ["note", "--bot", "ops", "--detail", "x"],
                     ["archive", "--bot", "ops", "--file", src], ["anchor"], ["report"], ["tune"],
                     ["alias", "add", "--slug", "ops", "--display", "Ops"],
                     ["capture", "files", "--bot", "ops", "--path", work]):
            rc, out, err = run(argv, root)
            self.assertEqual(rc, 0, "%s: %s%s" % (argv, out, err))
        with redirect_stderr(io.StringIO()):
            gblog.Ledger(root).write_state("ops", {"seq": 99, "head": "cd" * 32})
        rc, out, err = run(["note", "--bot", "ops", "--detail", "refused"], root)  # alert and spool files
        self.assertEqual(rc, 4, out + err)

    def test_everything_gblog_creates_is_private_under_any_umask(self):
        work = os.path.join(self.base, "work")
        os.makedirs(work)
        src = os.path.join(work, "evidence.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("evidence\n")
        for mask in (0o022, 0o000):
            with self.subTest(umask=oct(mask)):
                root = os.path.join(self.base, "ledger-%o" % mask)
                old = os.umask(mask)
                try:
                    self._workflow(root, work, src)
                finally:
                    os.umask(old)
                loose = [root] if stat.S_IMODE(os.lstat(root).st_mode) & 0o077 else []
                seen = 0
                for dirpath, dirnames, filenames in os.walk(root):
                    for name in dirnames + filenames:
                        full = os.path.join(dirpath, name)
                        seen += 1
                        if not os.path.islink(full) and stat.S_IMODE(os.lstat(full).st_mode) & 0o077:
                            loose.append(os.path.relpath(full, root))
                self.assertEqual(loose, [])
                self.assertGreater(seen, 20)
                for rel in (("reports",), ("tuning", "proposals.json"), ("state", "aliases.json"),
                            ("state", ".anchors.lock"), ("state", "capture-files-ops.json"),
                            ("state", "ops.alert.json"), ("README.md",), (".gitignore",)):
                    self.assertTrue(os.path.exists(os.path.join(root, *rel)), rel)


if __name__ == "__main__":
    unittest.main()
