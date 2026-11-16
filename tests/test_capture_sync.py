"""Capture and sync behaviour (audit I-11, I-12, I-16, I-17; test item I-28).

No network and no real browser profile is ever touched:
  * every git remote that is actually pushed to is a local bare repository;
  * git runs with GIT_TERMINAL_PROMPT=0, an empty global/system config and
    GIT_ALLOW_PROTOCOL=file, so a push to any https/ssh URL fails locally;
  * ``gblog.remote_visibility`` is always replaced by a recording fake;
  * Chrome and Firefox history databases are built with sqlite3 and passed explicitly.
"""
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
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

GIT = shutil.which("git")
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "file",
}


def run(argv, root):
    """Run the CLI in-process. A SystemExit is folded into the return code."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = gblog.main(["--root", root] + argv)
        except SystemExit as exc:
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                err.write("%s\n" % code)
                rc = 1
    return rc, out.getvalue(), err.getvalue()


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _utc(epoch):
    return gblog._dt.datetime.fromtimestamp(epoch, gblog._dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gblog-capsync-"))
        self.root = os.path.join(self.tmp, "ledger")
        self.ledger = gblog.Ledger(self.root)
        self.ledger.init()
        self._saved_env = {k: os.environ.get(k) for k in GIT_ENV}
        os.environ.update(GIT_ENV)
        self._real_visibility = gblog.remote_visibility
        self.vis_calls = []
        self.vis_answer = lambda url: {"public": False, "reason": "test"}

        def fake(remote, *args, **kwargs):
            self.vis_calls.append(remote)
            return dict(self.vis_answer(remote))

        gblog.remote_visibility = fake

    def tearDown(self):
        gblog.remote_visibility = self._real_visibility
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _break_state(self, bot):
        """Put state far ahead of the log so the next append is refused (never a roll-forward)."""
        state = dict(self.ledger.read_state(bot))
        state["seq"] = int(state.get("seq", 0)) + 98
        state["head"] = "cd" * 32
        self.ledger.write_state(bot, state)

    def _git(self, *args):
        return subprocess.run(["git"] + list(args), check=True, capture_output=True, text=True).stdout

    def _bare(self, name="origin.git"):
        path = os.path.join(self.tmp, name)
        self._git("init", "--bare", "-q", path)
        return path

    def _commits(self, repo):
        res = subprocess.run(["git", "-C", repo, "rev-list", "--all", "--count"], capture_output=True, text=True)
        return int(res.stdout.strip() or 0) if res.returncode == 0 else 0


# --------------------------------------------------------------------------- I-11
class VerifyRecordTest(_Base):
    def test_verify_record_continues_after_tail_mismatch(self):
        self.ledger.append("aaa", {"type": "note", "detail": "first chain", "ts": "2026-01-01T10:00:00Z"})
        self.ledger.append("zzz", {"type": "note", "detail": "second chain", "ts": "2026-01-01T10:00:00Z"})
        self._break_state("aaa")
        log = self.ledger.log_files("aaa")[0]
        before = _read(log)
        rc, out, err = run(["verify", "--all", "--record"], self.root)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("ALERT tail mismatch", out + err)
        self.assertIn("verify record not written", out + err)
        # the chain after the refused one was still verified and got its verify record
        zzz_lines = [line for line in out.splitlines() if "zzz" in line]
        self.assertTrue(zzz_lines and zzz_lines[0].startswith("OK"), out)
        last = self.ledger.records("zzz")[-1]
        self.assertEqual((last["type"], last["outcome"]), ("verify", "ok"))
        # the refused chain was not written to; the event is in the spool
        self.assertEqual(_read(log), before)
        spooled = os.listdir(os.path.join(self.root, "spool", "aaa"))
        self.assertTrue(any(name.endswith(".json") for name in spooled), spooled)


@unittest.skipUnless(GIT, "git not installed")
class SyncTest(_Base):
    def setUp(self):
        super().setUp()
        self.ledger.append("ops", {"type": "note", "detail": "something to back up", "ts": "2026-01-01T10:00:00Z"})

    def test_sync_commits_spool_when_system_chain_mismatched(self):
        bare = self._bare()
        self.ledger.append("system", {"type": "note", "detail": "earlier sync", "ts": "2026-01-01T10:00:00Z"})
        self._break_state("system")
        rc, out, err = run(["sync", "--remote", bare, "-m", "daily ledger sync"], self.root)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        tracked = self._git("-C", self.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertTrue(any(p.startswith("spool/system/") for p in tracked), tracked)
        self.assertIn("state/system.alert.json", tracked)
        self.assertTrue(any(p.startswith("logs/ops/") for p in tracked), tracked)
        self.assertNotIn("(not pushed)", out)
        self.assertEqual(self._commits(bare), 1)

    def test_sync_without_root_readme(self):
        os.remove(os.path.join(self.root, "README.md"))
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("commit", out)
        self.assertEqual(self._commits(self.root), 1)
        self.assertEqual(self.vis_calls, [])  # --no-push never asks about visibility

    def test_sync_commits_gitignore_and_untracks_locks(self):
        # an older backup repository that already tracks a lock file and a capture snapshot (R-14)
        lock = os.path.join(self.root, "state", "ops.lock")
        snap = os.path.join(self.root, "state", "capture-files-ops.json")
        self.assertTrue(os.path.exists(lock))
        _write(snap, json.dumps({"path": "/workspace", "taken": "2026-01-01T10:00:00Z", "files": {}}))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".gitignore")))
        self._git("init", "-q", self.root)
        self._git("-C", self.root, "add", "-f", "--", "state/ops.lock", "state/capture-files-ops.json")
        self._git("-C", self.root, "-c", "user.name=old", "-c", "user.email=old@localhost", "commit", "-q", "-m", "0.5 backup")
        tracked = self._git("-C", self.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertIn("state/ops.lock", tracked)
        self.assertIn("state/capture-files-ops.json", tracked)
        for attempt in (1, 2):
            rc, out, err = run(["sync", "--no-push"], self.root)
            self.assertEqual(rc, 0, out + err)
            tracked = self._git("-C", self.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
            self.assertIn(".gitignore", tracked, attempt)
            self.assertFalse([p for p in tracked if p.endswith(".lock")], tracked)
            self.assertFalse([p for p in tracked if p.startswith("state/capture-files-")], tracked)
            self.assertTrue(any(p.startswith("logs/ops/") for p in tracked), tracked)
            self.assertIn("state/ops.json", tracked)
            # untracked, not deleted
            self.assertTrue(os.path.exists(lock))
            self.assertTrue(os.path.exists(snap))
        self.assertEqual(self.vis_calls, [])

    # ----------------------------------------------------------------------- I-12
    def test_unknown_visibility_github_refused(self):
        url = "https://github.com/someone/maybe-private.git"
        self.vis_answer = lambda u: {"public": None, "reason": "api.github.com HTTP 403"}
        rc, out, err = run(["sync", "--remote", url], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("refused", out + err)
        self.assertIn("could not confirm", out + err)
        self.assertTrue(self.vis_calls)
        # Either override lifts the refusal. The push itself cannot leave this machine
        # (GIT_ALLOW_PROTOCOL=file), so it may fail, but not with a usage error or a refusal.
        for flag in ("--allow-unknown-visibility", "--allow-public"):
            rc, out, err = run(["sync", flag], self.root)
            self.assertNotIn(rc, (2, 3, 4), "%s: %s%s" % (flag, out, err))
            self.assertNotIn("refused:", out + err, flag)

    def test_unknown_visibility_other_host_warns(self):
        bare = self._bare()
        self.vis_answer = lambda u: {"public": None, "reason": "not a github.com remote; visibility unknown"}
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: could not confirm", err)
        self.assertNotIn("(not pushed)", out)
        self.assertIn("pushed", out)
        self.assertTrue(self.vis_calls)
        self.assertEqual(self._commits(bare), 1)

    def test_pushurl_is_checked(self):
        bare = self._bare()
        rc, out, err = run(["sync", "--no-push", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self._git("-C", self.root, "remote", "set-url", "--push", "origin", "https://github.com/someone/public-repo.git")
        self.vis_answer = lambda u: {"public": "github.com" in u, "reason": "test"}
        del self.vis_calls[:]
        rc, out, err = run(["sync"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("PUBLIC", out + err)
        self.assertTrue(any(c.endswith("github.com/someone/public-repo.git") for c in self.vis_calls), self.vis_calls)
        self.assertEqual(self._commits(bare), 0)
        self.assertEqual(self.ledger.records("system")[-1]["outcome"], "denied")

    def test_error_output_redacts_userinfo(self):
        secret = "s3cr3t" + "Passw0rd"
        url = "https://alice:%s@github.com/someone/public-repo.git" % secret
        self.vis_answer = lambda u: {"public": True, "reason": "test"}
        rc, out, err = run(["sync", "--remote", url], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("refused", out + err)
        self.assertIn("PUBLIC", out + err)
        self.assertNotIn(secret, out + err)
        for dirpath, _dirs, files in os.walk(os.path.join(self.root, "logs")):
            for name in files:
                self.assertNotIn(secret.encode("utf-8"), _read(os.path.join(dirpath, name)), name)


class RemoteRegexTest(unittest.TestCase):
    ACCEPT = (
        "git@github.com:deximple/grokledger.git",
        "https://github.com/deximple/grokledger",
        "https://github.com/deximple/grokledger.git/",
        "https://www.github.com/deximple/grokledger.git",
        "ssh://git@github.com/deximple/grokledger.git",
        "https://user:tok@github.com/deximple/grokledger",
        "github.com/deximple/grokledger",
    )
    REJECT = (
        "https://evilgithub.com/o/r",
        "https://evilgithub.com/o/r.git",
        "git@notgithub.com:o/r.git",
        "https://github.com.evil.example/o/r",
        "https://example.com/github.com/o/r",
        "/srv/git/github.com/o/r.git",
    )

    def test_regex_rejects_lookalike_hosts(self):
        for url in self.ACCEPT:
            m = gblog._GITHUB_REMOTE_RE.search(url)
            self.assertIsNotNone(m, url)
            self.assertEqual((m.group(1), m.group(2)), ("deximple", "grokledger"), url)
        for url in self.REJECT:
            self.assertIsNone(gblog._GITHUB_REMOTE_RE.search(url), url)

    def test_lookalike_host_is_unknown_without_any_lookup(self):
        import urllib.request

        def boom(*args, **kwargs):
            raise AssertionError("remote_visibility tried gh or the network for a non-GitHub URL")

        orig_run, orig_open = subprocess.run, urllib.request.urlopen
        subprocess.run = boom
        urllib.request.urlopen = boom
        try:
            for url in self.REJECT:
                self.assertIsNone(gblog.remote_visibility(url)["public"], url)
        finally:
            subprocess.run = orig_run
            urllib.request.urlopen = orig_open


# --------------------------------------------------------------------------- I-16
def _webkit(epoch):
    return (int(epoch) + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000


class BrowserCaptureTest(_Base):
    def _chrome(self, name, visits):
        """visits: [(url, title, epoch seconds or None)]"""
        prof = os.path.join(self.tmp, name, "Default")
        os.makedirs(prof)
        path = os.path.join(prof, "History")
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        conn.commit()
        conn.close()
        for url, title, when in visits:
            self._add_chrome_visit(path, url, title, when)
        return path

    def _add_chrome_visit(self, path, url, title, when):
        conn = sqlite3.connect(path)
        cur = conn.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title))
        conn.execute("INSERT INTO visits(url, visit_time) VALUES (?, ?)", (cur.lastrowid, None if when is None else _webkit(when)))
        conn.commit()
        conn.close()

    @unittest.skipUnless(hasattr(time, "tzset"), "time.tzset not available")
    def test_cutoff_independent_of_TZ(self):
        base = int(time.time())
        recent = base - 20 * 3600
        hist = self._chrome("chrome-tz", [
            ("https://old.example/thirty-hours", "old", base - 30 * 3600),
            ("https://recent.example/twenty-hours", "recent", recent),
        ])
        saved = os.environ.get("TZ")
        try:
            for i, tz in enumerate(("UTC0", "PST8", "KST-9")):
                os.environ["TZ"] = tz
                time.tzset()
                bot = "tz%d" % i
                res = gblog.capture_browser(self.ledger, bot, [hist], hours=24)
                self.assertEqual(res["added"], 1, (tz, res))
                rec = self.ledger.records(bot)[-1]
                self.assertEqual(rec["target"], "https://recent.example/twenty-hours", tz)
                self.assertEqual(rec["observed"], _utc(recent), tz)
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def test_marker_beats_hours_window(self):
        base = int(time.time())
        hist = self._chrome("chrome-marker", [("https://first.example/a", "A", base - 50 * 3600)])
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=72)
        self.assertEqual(res["added"], 1, res)
        # a visit made after the last capture but older than --hours (the routine missed a day)
        self._add_chrome_visit(hist, "https://second.example/b", "B", base - 30 * 3600)
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual(res["added"], 1, res)
        self.assertEqual(self.ledger.records("ops")[-1]["target"], "https://second.example/b")
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual(res["added"], 0, res)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_firefox_places_fixture(self):
        base = int(time.time())
        prof = os.path.join(self.tmp, "firefox", "abcd1234.default-release")
        os.makedirs(prof)
        path = os.path.join(prof, "places.sqlite")
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER);")
        conn.execute("INSERT INTO moz_places VALUES (1, 'https://example.org/docs/?session=abc#top', 'Docs')")
        conn.execute("INSERT INTO moz_places VALUES (2, 'about:preferences', 'Settings')")
        conn.execute("INSERT INTO moz_places VALUES (3, 'https://null.example/x', 'no visit time')")
        conn.execute("INSERT INTO moz_historyvisits VALUES (1, 1, ?)", ((base - 600) * 1_000_000,))
        conn.execute("INSERT INTO moz_historyvisits VALUES (2, 2, ?)", ((base - 300) * 1_000_000,))
        conn.execute("INSERT INTO moz_historyvisits VALUES (3, 3, NULL)")
        conn.commit()
        conn.close()
        res = gblog.capture_browser(self.ledger, "ops", [path], hours=24)
        self.assertEqual(res["added"], 1, res)
        self.assertEqual(res["failed"], [])
        recs = self.ledger.records("ops")
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["target"], "https://example.org/docs/")
        self.assertEqual((rec["action"], rec["source"], rec["capture"]), ("browser", "auto", "browser"))
        self.assertEqual(rec["profile"], "abcd1234.default-release")
        self.assertEqual(rec["observed"], _utc(base - 600))
        markers = self.ledger.read_state("ops")["capture_browser_last"]
        self.assertIsInstance(markers, dict)
        self.assertIn(path, markers)
        res = gblog.capture_browser(self.ledger, "ops", [path], hours=24)
        self.assertEqual(res["added"], 0, res)

    def test_unreadable_history_reported(self):
        base = int(time.time())
        good = self._chrome("chrome-good", [("https://ok.example/page", "ok", base - 60)])
        bad = os.path.join(self.tmp, "chrome-bad", "Default", "History")
        _write(bad, "this is not a sqlite database\n" * 40)
        missing = os.path.join(self.tmp, "chrome-missing", "Default", "History")
        res = gblog.capture_browser(self.ledger, "ops", [bad, good, missing], hours=24)
        self.assertEqual(res["added"], 1, res)
        self.assertEqual(len(res["failed"]), 2, res)
        self.assertTrue(any(bad in str(f) for f in res["failed"]), res)
        self.assertTrue(any(missing in str(f) for f in res["failed"]), res)
        self.assertFalse(any(good in str(f) for f in res["failed"]), res)

        rc, out, err = run(["capture", "browser", "--bot", "cli", "--history", bad, "--history", good], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("1 new visit(s)", out)
        self.assertIn("1 unreadable", out)
        rc, out, err = run(["capture", "browser", "--bot", "cli2", "--history", good], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("unreadable", out)
        rc, out, err = run(["capture", "browser", "--bot", "cli3", "--history", bad], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)


# --------------------------------------------------------------------------- I-17
class FileCaptureTest(_Base):
    def _capture(self, bot, path, ledger=None):
        err = io.StringIO()
        with redirect_stderr(err):
            res = gblog.capture_files(ledger or self.ledger, bot, path)
        return res, err.getvalue()

    def _snap(self, bot="ops"):
        return os.path.join(self.root, "state", "capture-files-%s.json" % bot)

    def _file_records(self, ledger=None, bot="ops"):
        return [r for r in (ledger or self.ledger).records(bot) if r.get("capture") == "files"]

    def test_path_change_rebaselines(self):
        a = os.path.join(self.tmp, "work-a")
        b = os.path.join(self.tmp, "work-b")
        _write(os.path.join(a, "a1.txt"), "1")
        _write(os.path.join(a, "a2.txt"), "2")
        _write(os.path.join(b, "b1.txt"), "3")
        res, _ = self._capture("ops", a)
        self.assertEqual(res["changes"], 0)
        rc, out, err = run(["capture", "files", "--bot", "ops", "--path", b], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("files: 0 change(s)"), out)
        self.assertTrue(err.strip(), "expected a re-baseline notice on stderr")
        self.assertEqual(self.ledger.records("ops"), [])  # no mass deleted/created records
        _write(os.path.join(b, "b2.txt"), "4")
        res, _ = self._capture("ops", b)
        self.assertEqual(res["changes"], 1)
        self.assertEqual([(r["target"], r["detail"]) for r in self._file_records()], [("b2.txt", "created")])

    def test_missing_path_does_not_log_deletions(self):
        work = os.path.join(self.tmp, "work")
        for name in ("a.txt", "b.txt", "c.txt"):
            _write(os.path.join(work, name), name)
        res, _ = self._capture("ops", work)
        self.assertEqual(res["changes"], 0)
        before = _read(self._snap())
        shutil.rmtree(work)
        rc, out, err = run(["capture", "files", "--bot", "ops", "--path", work], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.ledger.records("ops"), [])
        self.assertEqual(_read(self._snap()), before)
        # a path that never existed does not create a snapshot either
        rc, out, err = run(["capture", "files", "--bot", "fresh", "--path", os.path.join(self.tmp, "never-there")], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(os.path.exists(self._snap("fresh")))
        self.assertEqual(self.ledger.records("fresh"), [])

    def test_snapshot_kept_when_append_refused(self):
        self.ledger.append("ops", {"type": "note", "detail": "seed", "ts": "2026-01-01T10:00:00Z"})
        work = os.path.join(self.tmp, "work")
        _write(os.path.join(work, "a.txt"), "1")
        res, _ = self._capture("ops", work)
        self.assertEqual(res["changes"], 0)
        _write(os.path.join(work, "a.txt"), "22")
        _write(os.path.join(work, "b.txt"), "x")
        good = dict(self.ledger.read_state("ops"))
        self._break_state("ops")
        before = _read(self._snap())
        rc, out, err = run(["capture", "files", "--bot", "ops", "--path", work], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertEqual(_read(self._snap()), before)  # the unlogged changes are still pending
        self.assertEqual(self._file_records(), [])
        self.ledger.write_state("ops", good)
        res, _ = self._capture("ops", work)
        self.assertEqual(res["changes"], 2)
        self.assertEqual(sorted((r["target"], r["detail"]) for r in self._file_records()),
                         [("a.txt", "modified"), ("b.txt", "created")])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_sibling_dir_with_root_prefix_is_scanned(self):
        parent = os.path.join(self.tmp, "ws")
        ledger = gblog.Ledger(os.path.join(parent, "activity-log"))
        ledger.init()
        sibling = os.path.join(parent, "activity-log-old")
        _write(os.path.join(sibling, "keep.txt"), "k")
        res, _ = self._capture("ops", parent, ledger)
        self.assertEqual(res["changes"], 0)
        _write(os.path.join(sibling, "new.txt"), "n")
        res, _ = self._capture("ops", parent, ledger)
        targets = [r["target"] for r in self._file_records(ledger)]
        self.assertIn(os.path.join("activity-log-old", "new.txt"), targets)
        self.assertEqual(res["changes"], 1, targets)
        # the ledger's own files are still excluded
        self.assertFalse(any(t.startswith("activity-log" + os.sep) for t in targets), targets)

    def test_corrupt_snapshot_rebaselines(self):
        work = os.path.join(self.tmp, "work")
        _write(os.path.join(work, "a.txt"), "1")
        res, _ = self._capture("ops", work)
        self.assertEqual(res["changes"], 0)
        _write(self._snap(), "{not json")
        _write(os.path.join(work, "b.txt"), "2")
        rc, out, err = run(["capture", "files", "--bot", "ops", "--path", work], self.root)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("files: 0 change(s)"), out)
        self.assertTrue(err.strip(), "expected a re-baseline notice on stderr")
        self.assertEqual(self.ledger.records("ops"), [])
        with open(self._snap(), encoding="utf-8") as fh:
            snap = json.load(fh)
        self.assertEqual(set(snap["files"]), {"a.txt", "b.txt"})
        _write(os.path.join(work, "c.txt"), "3")
        res, _ = self._capture("ops", work)
        self.assertEqual(res["changes"], 1)
        self.assertEqual([(r["target"], r["detail"]) for r in self._file_records()], [("c.txt", "created")])


if __name__ == "__main__":
    unittest.main()
