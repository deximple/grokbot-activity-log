"""Tests for the gaps named in the test-quality review of 2026-11-16 (mutations M-01 .. M-30).

Each test carries the id of the review item it closes (N-..) or of the defect it documents (D-..).
Every test passes on the current gblog.py, except the ones marked ``@unittest.expectedFailure``
with a ``bug: ...`` comment: those document behaviour that the documentation promises and the
code does not have. They are listed in ``tests/PENDING.txt`` (``tests/test_meta.py`` checks that).

Everything runs in temporary directories. No network is used: git remotes are local bare
repositories, git runs with GIT_ALLOW_PROTOCOL=file, and the visibility lookup, ``gh``, ``ssh``
and ``urlopen`` are replaced by fakes.
"""
import datetime as _dt
import errno
import gzip
import io
import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import gblog  # noqa: E402

sys.path.insert(0, HERE)
from _support import GIT_ENV, deep_nesting, run_cli, run_raw, stamp  # noqa: E402
from _support import run as _support_run  # noqa: E402

GIT = shutil.which("git")
BASH = shutil.which("bash")
DAY = "2024-06-01"
LINK_TEXT = "%s: symbolic link (gblog does not write through one)"
INTERNAL_LINE = "error: internal error (RuntimeError: boom second line); set GBLOG_DEBUG=1 for a traceback\n"


def run(argv, root, internal_ok=False):
    """Run the CLI in-process. Unlike ``_support.run`` this also fails on a swallowed crash:
    ``main()`` turns an unexpected exception into ``error: internal error`` and ``verify --all``
    turns it into ``verify aborted``, so a missing traceback alone proves nothing."""
    rc, out, err = _support_run(argv, root)
    if not internal_ok:
        for text, where in (("Traceback", err), ("internal error", err), ("verify aborted", out)):
            if text in where:
                raise AssertionError("gblog %s: unexpected %r\n%s\n%s" % (" ".join(argv), text, out[-2000:], err[-2000:]))
    return rc, out, err


def write_raw_chain(root, bot, extras, day=DAY, state=True):
    """Hash-valid lines written straight to logs/<bot>/<day>.jsonl, bypassing gblog.

    ``extras`` holds one dict per record; its keys override the default fields before the hash is
    computed. With ``state`` the state file names the last record."""
    d = os.path.join(root, "logs", bot)
    os.makedirs(d, exist_ok=True)
    prev, recs = gblog.GENESIS, []
    with open(os.path.join(d, day + ".jsonl"), "w", encoding="utf-8") as fh:
        for i, extra in enumerate(extras, 1):
            rec = {"v": 1, "seq": i, "ts": stamp(i, day), "bot": bot, "type": "note", "detail": "raw %d" % i, "prev": prev}
            rec.update(extra)
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            recs.append(rec)
    if state:
        os.makedirs(os.path.join(root, "state"), exist_ok=True)
        with open(os.path.join(root, "state", bot + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"seq": len(recs), "head": prev, "updated": recs[-1]["ts"]}, fh)
    return recs


def ago(days, seconds=0):
    """A UTC timestamp ``days`` days (minus ``seconds``) before now."""
    when = gblog._utcnow() - _dt.timedelta(days=days) + _dt.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args, check=True):
    return subprocess.run(["git"] + list(args), check=check, capture_output=True, text=True).stdout


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _rewrite(path, data):
    """Overwrite ``path`` and move its mtime forward, as a later edit of the file would."""
    st = os.stat(path)
    with open(path, "wb") as fh:
        fh.write(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def _webkit(epoch):
    return (int(epoch) + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000


def add_visit(path, url, title, when):
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title))
        conn.execute("INSERT INTO visits(url, visit_time) VALUES (?, ?)", (cur.lastrowid, when))
        conn.commit()
    finally:
        conn.close()


def chrome_history(directory, visits):
    """A Chrome-style History database. ``visits`` is [(url, title, raw visit_time)]."""
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
    for url, title, when in visits:
        add_visit(path, url, title, when)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-gaps-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
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

    def seed(self, bot="ops", n=2, day=DAY):
        return [self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1), "ts": stamp(i, day)})
                for i in range(n)]

    def chain(self, bot):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def raw_state(self, bot="ops"):
        with open(self.path("state", bot + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def log_bytes(self, bot="ops"):
        return b"".join(_read_bytes(p) for p in self.ledger.log_files(bot))

    def break_state(self, bot="ops"):
        self.ledger.write_state(bot, {"seq": 99, "head": "cd" * 32})

    def restore_state(self, bot="ops"):
        seq, head = self.ledger._tail_head(bot)
        self.ledger.write_state(bot, {"seq": seq, "head": head})

    def pending_spool(self, bot="ops"):
        d = self.path("spool", bot)
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.endswith(".json"))

    def symlink(self, target, link, is_dir=False):
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not supported here")

    def quiet(self, fn, *args, **kwargs):
        """Call a library function; returns ``(result, what it wrote to stderr)``."""
        err = io.StringIO()
        with redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, err.getvalue()

    def flaky_write_state(self):
        """write_state fails once with ENOSPC (after the log line is already durable), then works."""
        orig = gblog.Ledger.write_state
        calls = {"n": 0}

        def flaky(ledger, bot, state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.path.join(ledger.state_dir, bot + ".json"))
            return orig(ledger, bot, state)

        return mock.patch.object(gblog.Ledger, "write_state", flaky)

    def bare(self, name):
        repo = os.path.join(self.base, name)
        git("init", "--bare", "-q", repo)
        return repo

    def commits(self, repo):
        res = subprocess.run(["git", "-C", repo, "rev-list", "--all", "--count"], capture_output=True, text=True)
        return int(res.stdout.strip() or 0) if res.returncode == 0 else 0


# --------------------------------------------------------------------------- N-01, N-09, N-11, N-28
class IntegrityGapTest(Base):
    def test_timestamp_going_backwards_fails(self):
        # N-01: the monotonic-ts rule on a chain whose links, hashes and state are all intact
        write_raw_chain(self.root, "ops", [{"ts": DAY + "T10:00:10Z"}, {"ts": DAY + "T10:00:05Z"},
                                           {"ts": DAY + "T10:00:20Z"}])
        text = "logs/ops/%s.jsonl:2: timestamp goes backwards" % DAY
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(res["problems"], [text])
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - " + text, out.splitlines())
        # control: equal timestamps are not "backwards"
        write_raw_chain(self.root, "same", [{"ts": DAY + "T10:00:10Z"}] * 3)
        res = self.ledger.verify("same")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["warnings"]), (3, []))

    def test_symlinked_day_file_warns_and_strict_fails(self):
        # N-09
        self.seed("ops", 2, day="2026-03-01")
        log = self.ledger.log_files("ops")[0]
        moved = self.path("moved.jsonl")
        os.rename(log, moved)
        self.symlink(moved, log)
        text = LINK_TEXT % "logs/ops/2026-03-01.jsonl"
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 2)
        self.assertEqual(res["warnings"], [text])
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("      ! " + text, out.splitlines())
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - " + text, out.splitlines())

    def test_symlinked_state_anchor_and_archive_dir_warn(self):
        # N-09
        gblog.archive_file(self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts="2026-03-01T10:00:00Z")
        self.ledger.append("ops", {"type": "note", "detail": "two", "ts": "2026-03-01T10:00:01Z"})
        self.ledger.anchor(["ops"])
        moves = (
            (self.path("state", "ops.json"), os.path.join(self.base, "state-moved.json"), False),
            (self.path("anchors", "ops.json"), os.path.join(self.base, "anchor-moved.json"), False),
            (self.path("archive", "ops"), os.path.join(self.base, "archive-moved"), True),
        )
        for link, target, is_dir in moves:
            os.rename(link, target)
            self.symlink(target, link, is_dir)
        texts = [LINK_TEXT % rel for rel in ("state/ops.json", "anchors/ops.json", "archive/ops")]
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["archives"]), (2, 1))
        for text in texts:
            self.assertIn(text, res["warnings"])
        strict = self.ledger.verify("ops", strict=True)
        self.assertFalse(strict["ok"])
        for text in texts:
            self.assertIn(text, strict["problems"])

    def test_directory_named_jsonl_fails_in_every_mode(self):
        # N-09
        self.seed("ops", 1)
        os.mkdir(self.path("logs", "ops", "zz.jsonl"))
        text = "logs/ops/zz.jsonl: not a regular file"
        for strict in (False, True):
            res = self.ledger.verify("ops", strict=strict)
            self.assertFalse(res["ok"], strict)
            self.assertIn(text, res["problems"])

    def test_non_normalised_ts_is_a_strict_only_finding(self):
        # N-28
        day = "2026-03-01"
        write_raw_chain(self.root, "ops", [{"ts": day + "T10:00:01+00:00"}, {"ts": day + "T10:00:02+00:00"}], day=day)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["warnings"]), (2, []))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("      ! ", out)
        texts = ["logs/ops/%s.jsonl:%d: ts is not in normalised form" % (day, n) for n in (1, 2)]
        strict = self.ledger.verify("ops", strict=True)
        self.assertFalse(strict["ok"])
        self.assertEqual(strict["problems"], texts)
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - " + texts[0], out.splitlines())

    def test_verify_bot_fails_for_a_deleted_chain_that_an_alias_resolves_elsewhere(self):
        # N-11: the chains an alias resolves a name to cannot stand in for the named chain itself
        self.seed("ops", 3)
        self.seed("ops-new", 2)
        self.ledger.anchor()
        self.ledger.add_alias("ops-new", "Ops", ["ops"])
        shutil.rmtree(self.path("logs", "ops"))
        os.remove(self.path("state", "ops.json"))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^OK\s+ops-new\s")
        self.assertEqual(len(re.findall(r"(?m)^FAIL\s+ops\s+0 records", out)), 1, out)
        self.assertIn("but logs/ops is missing", out)
        rc, out, err = run(["verify", "--bot", "ops", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        by = {entry["bot"]: entry for entry in json.loads(out)}
        self.assertEqual(sorted(by), ["ops", "ops-new"])
        self.assertIs(by["ops-new"]["ok"], True)
        self.assertIs(by["ops"]["ok"], False)
        self.assertIs(by["ops"]["missing"], True)

    def test_anchor_reports_a_missing_chain_and_keeps_its_witness(self):
        # the rollup["missing"] exit code of `anchor`
        self.seed("ops", 2)
        self.seed("keeper", 1)
        self.ledger.anchor()
        shutil.rmtree(self.path("logs", "ops"))
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+keeper\s")
        self.assertRegex(out, r"(?m)^ANCHOR FAIL ops\s")
        self.assertIn("but logs/ops is missing", out)
        with open(self.path("anchors", "ops.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["records"], 2)

    def test_unreadable_anchor_file_is_a_warning_not_a_fail(self):
        # section 3: a crash inside verify would print FAIL ops; the chain must stay OK with the warning
        self.seed("ops", 1)
        self.seed("zzz", 1)
        with open(self.path("anchors", "ops.json"), "wb") as fh:
            fh.write(b"[" * deep_nesting(200000))
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out[:2000] + err[-2000:])
        self.assertRegex(out, r"(?m)^OK\s+ops\s")
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")
        self.assertIn("      ! anchors/ops.json is unreadable: anchored heads cannot be checked", out.splitlines())


# --------------------------------------------------------------------------- N-02, N-04, N-05, N-12, N-29
@unittest.skipUnless(GIT, "git not installed")
class SyncGapTest(Base):
    DAY_FILE = "logs/ops/2026-01-01.jsonl"

    def setUp(self):
        super().setUp()
        self.vis_calls = []
        self.vis_answer = lambda url: {"public": False, "reason": "test"}

        def fake(remote, *args, **kwargs):
            self.vis_calls.append(remote)
            return dict(self.vis_answer(remote))

        patcher = mock.patch.object(gblog, "remote_visibility", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger.append("ops", {"type": "note", "detail": "something to back up", "ts": "2026-01-01T10:00:00Z"})

    def _later_note(self):
        self.ledger.append("ops", {"type": "note", "detail": "written after the first backup",
                                   "ts": "2026-01-01T10:00:05Z"})

    def _first_sync(self, name="origin.git"):
        bare = self.bare(name)
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.commits(bare), 1)
        return bare

    def test_sync_with_drift_exits_1_and_does_not_push(self):
        # N-02: what sync does once the committed-content check reports a file
        bare = self._first_sync()
        self._later_note()
        asked = len(self.vis_calls)
        with mock.patch.object(gblog, "_sync_drift", return_value=[self.DAY_FILE]) as drift:
            rc, out, err = run(["sync"], self.root)
            self.assertTrue(drift.called)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("(not pushed)", out)
            self.assertIn("committed content differs from disk for %s" % self.DAY_FILE, out)
            self.assertEqual(self.commits(bare), 1)
            self.assertEqual(len(self.vis_calls), asked)  # nothing is pushed, so nobody is asked
            last = self.chain("system")[-1]
            self.assertEqual((last["type"], last["outcome"]), ("sync", "fail"))
            # never a success, even when no push was asked for
            rc, out, err = run(["sync", "--no-push"], self.root)
            self.assertEqual(rc, 1, out + err)

    def test_sync_detects_a_day_file_git_was_told_not_to_look_at(self):
        # N-02: an index flag keeps the new record out of the commit while git reports success
        bare = self._first_sync()
        git("-C", self.root, "update-index", "--assume-unchanged", self.DAY_FILE)
        self._later_note()
        rc, out, err = run(["sync"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("(not pushed)", out)
        self.assertIn("committed content differs from disk for %s" % self.DAY_FILE, out)
        self.assertEqual(self.commits(bare), 1)

    def test_sync_cache_does_not_hide_a_changed_file(self):
        # N-02: the cache branch of _sync_drift
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        _forced, checked = gblog._sync_evidence(self.ledger)
        self.assertIn(self.DAY_FILE, checked)
        cache = os.path.join(self.base, "sync-cache.json")
        self.assertEqual(gblog._sync_drift(self.root, checked, cache), [])
        self.assertTrue(os.path.isfile(cache))
        full = self.path(*self.DAY_FILE.split("/"))
        original = _read_bytes(full)
        self.assertIn(b"something to back up", original)
        _rewrite(full, original.replace(b"something to back up", b"SOMETHING TO BACK UP"))
        self.assertEqual(os.path.getsize(full), len(original))
        self.assertEqual(gblog._sync_drift(self.root, checked, cache), [self.DAY_FILE])
        _rewrite(full, original)
        self.assertEqual(gblog._sync_drift(self.root, checked, cache), [])
        # nothing changed since the last run: no file is hashed again
        with mock.patch.object(gblog, "_git_blob_id", wraps=gblog._git_blob_id) as spy:
            self.assertEqual(gblog._sync_drift(self.root, checked, cache), [])
        self.assertEqual(spy.call_count, 0)
        # without a cache every checked file is hashed
        with mock.patch.object(gblog, "_git_blob_id", wraps=gblog._git_blob_id) as spy:
            self.assertEqual(gblog._sync_drift(self.root, checked), [])
        self.assertEqual(spy.call_count, len(checked))

    def test_allow_unknown_visibility_does_not_override_public(self):
        # N-04
        self.vis_answer = lambda url: {"public": True, "reason": "test"}
        rc, out, err = run(["sync", "--remote", "https://github.com/someone/public-repo.git",
                            "--allow-unknown-visibility"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("PUBLIC", out + err)
        self.assertEqual(self.chain("system")[-1]["outcome"], "denied")

    def test_every_push_url_is_checked(self):
        # N-04: two push URLs, only the second one is public
        bare = self.bare("origin.git")
        rc, out, err = run(["sync", "--no-push", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        public = "https://github.com/someone/public-repo.git"
        git("-C", self.root, "remote", "set-url", "--push", "origin", bare)
        git("-C", self.root, "remote", "set-url", "--add", "--push", "origin", public)
        self.vis_answer = lambda url: {"public": "github.com" in url, "reason": "test"}
        rc, out, err = run(["sync"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("PUBLIC", out + err)
        self.assertEqual(sorted(self.vis_calls), sorted([bare, public]))
        self.assertEqual(self.commits(bare), 0)

    def test_sync_checks_an_ssh_alias_as_the_github_repository(self):
        # N-05: sync asks about the repository the alias points at, and names the URL as written
        alias = "git@gh-backup:o/r.git"
        rc, out, err = run(["sync", "--no-push", "--remote", alias], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.vis_calls, [])
        self.vis_answer = lambda url: {"public": None, "reason": "api.github.com HTTP 403"}
        mapping = {alias: "https://github.com/o/r.git"}
        with mock.patch.object(gblog, "_ssh_alias_target", side_effect=lambda url: mapping.get(url, url)):
            rc, out, err = run(["sync"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertEqual(self.vis_calls, ["https://github.com/o/r.git"])
        self.assertIn("refused: could not confirm %s is private" % alias, out)

    def test_unknown_visibility_override_really_pushes(self):
        # section 3: with a local repository classified as GitHub, the override is seen to push
        bare = self.bare("maybe-private.git")
        self.vis_answer = lambda url: {"public": None, "reason": "api.github.com HTTP 403"}
        with mock.patch.object(gblog, "github_remote", return_value=(True, "o", "r")):
            rc, out, err = run(["sync", "--remote", bare], self.root)
            self.assertEqual(rc, 3, out + err)
            self.assertIn("refused: could not confirm", out)
            self.assertEqual(self.commits(bare), 0)
            rc, out, err = run(["sync", "--allow-unknown-visibility"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(" pushed", out)
        self.assertNotIn("(not pushed)", out)
        self.assertGreater(self.commits(bare), 0)
        self.assertEqual(self.commits(bare), self.commits(self.root))

    @unittest.skipUnless(os.name == "posix", "executable hook scripts are POSIX-specific")
    def test_sync_runs_no_git_hooks(self):
        # N-12
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        marker = os.path.join(self.base, "hook-ran")
        hooks = self.path(".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        for name, code in (("pre-commit", 1), ("post-commit", 0), ("pre-push", 1)):
            path = self.write(os.path.join(hooks, name),
                              "#!/bin/sh\necho %s >> %s\nexit %d\n" % (name, shlex.quote(marker), code))
            os.chmod(path, 0o755)
        self._later_note()
        bare = self.bare("origin.git")
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(" pushed", out)
        self.assertNotIn("(not pushed)", out)
        self.assertFalse(os.path.exists(marker), "a git hook of the ledger repository was run")
        self.assertEqual(self.commits(bare), 2)
        self.assertEqual(self.commits(bare), self.commits(self.root))

    def test_sync_does_not_stamp_invalid_chain_dirs(self):
        # N-29
        write_raw_chain(self.root, "my bot", [{}], state=False)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: ignoring logs/my bot", err)
        for name in ("my bot.json", "my bot.lock", "my-bot.json", "my-bot.lock"):
            self.assertFalse(os.path.exists(self.path("state", name)), name)
        self.assertIn("last_sync", self.raw_state("ops"))

    def test_remote_password_reaches_neither_the_output_nor_the_ledger(self):
        # section 3 (RemoteGuardTest): exit code and the password itself
        rc, out, err = run(["sync", "--no-push", "-m", "nightly",
                            "--remote", "https://deploy:tok3nValue9@host.example/r.git"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("tok3nValue9", out + err)
        self.assertIn("warning: the --remote URL contains a user name and password", err)
        for top in ("logs", "state", "spool"):
            for dirpath, _dirs, files in os.walk(self.path(top)):
                for name in files:
                    self.assertNotIn(b"tok3nValue9", _read_bytes(os.path.join(dirpath, name)), name)
        change = [r for r in self.chain("system") if r.get("target") == "git remote"]
        self.assertEqual(len(change), 1)
        self.assertIn("https://[REDACTED]@host.example/r.git", change[0]["detail"])


# --------------------------------------------------------------------------- N-03
class _Body:
    """Stand-in for the object urlopen() returns."""

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data


class VisibilityLookupTest(unittest.TestCase):
    URL = "https://github.com/someone/repo"

    def _api(self, answer):
        seen = []

        def urlopen(req, *args, **kwargs):
            seen.append(req.full_url)
            if isinstance(answer, BaseException):
                raise answer
            return _Body(answer)

        with mock.patch.object(gblog.subprocess, "run", side_effect=OSError("gh is not installed")), \
                mock.patch("urllib.request.urlopen", urlopen):
            result = gblog.remote_visibility(self.URL)
        self.assertEqual(seen, ["https://api.github.com/repos/someone/repo"])
        return result

    def _http_error(self, code):
        return urllib.error.HTTPError("https://api.github.com/repos/someone/repo", code, "status %d" % code, {}, None)

    def test_api_answers_map_to_public_private_unknown(self):
        self.assertEqual(self._api(b'{"private": false}'), {"public": True, "reason": "api.github.com"})
        self.assertEqual(self._api(b'{"private": true}'), {"public": False, "reason": "api.github.com"})
        self.assertEqual(self._api(self._http_error(404)),
                         {"public": False, "reason": "api.github.com 404 (private or nonexistent)"})
        self.assertEqual(self._api(self._http_error(403)), {"public": None, "reason": "api.github.com HTTP 403"})

    def test_gh_answer_false_means_public(self):
        for stdout, public in (("false\n", True), ("true\n", False)):
            calls = []

            def fake_gh(argv, *args, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            with mock.patch.object(gblog.subprocess, "run", side_effect=fake_gh), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("the API must not be asked")) as api:
                result = gblog.remote_visibility(self.URL)
            self.assertEqual(result, {"public": public, "reason": "gh api"})
            self.assertFalse(api.called)
            self.assertEqual(len(calls), 1)
            self.assertIn("repos/someone/repo", calls[0])

    def test_github_remote_classifies_by_parsed_host(self):
        # section 3: decisions use github_remote(), not the old pattern over the URL text
        for url in ("git@github.com:deximple/grokledger.git", "https://github.com/deximple/grokledger",
                    "https://github.com/deximple/grokledger.git/", "https://www.github.com/deximple/grokledger.git",
                    "ssh://git@github.com/deximple/grokledger.git", "https://user:tok@github.com/deximple/grokledger",
                    "github.com/deximple/grokledger", "https://github.com:443/deximple/grokledger.git",
                    "ssh://git@ssh.github.com:443/deximple/grokledger.git", "https://github.com./deximple/grokledger"):
            self.assertEqual(gblog.github_remote(url), (True, "deximple", "grokledger"), url)
        for url in ("https://evilgithub.com/o/r", "git@notgithub.com:o/r.git", "https://github.com.evil.example/o/r",
                    "https://example.com/github.com/o/r", "/srv/git/github.com/o/r.git",
                    "https://evil.example/x//github.com/o/r"):
            self.assertEqual(gblog.github_remote(url), (False, None, None), url)
        # on GitHub, but not a repository the API can be asked about: visibility unknown
        self.assertEqual(gblog.github_remote("https://gist.github.com/0123456789abcdef.git"), (True, None, None))

    def test_safe_url_drops_a_token_without_a_password(self):
        self.assertEqual(gblog._safe_url("https://Zq7tokenonly99@host.example/r.git"),
                         "https://[REDACTED]@host.example/r.git")
        self.assertEqual(gblog._safe_url("https://deploy:pa55word9@host.example/r.git"),
                         "https://[REDACTED]@host.example/r.git")
        self.assertEqual(gblog._safe_url("git@host.example:o/r.git"), "git@host.example:o/r.git")


# --------------------------------------------------------------------------- N-06, N-15, N-17
class SpoolGapTest(Base):
    def test_spooled_event_is_redacted(self):
        # N-06: spool and alert files are pushed by sync, so they must not hold what a record would not
        token = "Zq7xT0k" + "9LmN4pR2"
        password = "hunter2" + "Hunter2x"
        self.seed("ops", 2)
        self.break_state("ops")
        rc, out, err = run(["log", "--bot", "ops", "--action", "shell", "--target", "deploy --token=" + token,
                            "--detail", "DB_PASSWORD=" + password], self.root)
        self.assertEqual(rc, 4, out + err)
        pending = self.pending_spool("ops")
        self.assertEqual(len(pending), 1)
        for path in (self.path("spool", "ops", pending[0]), self.path("state", "ops.alert.json")):
            data = _read_bytes(path)
            self.assertNotIn(token.encode("ascii"), data, path)
            self.assertNotIn(password.encode("ascii"), data, path)
            self.assertIn(b"[REDACTED]", data, path)
        self.assertNotIn(token, out + err)
        self.assertNotIn(password, out + err)
        self.restore_state("ops")
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        for data in (_read_bytes(self.path("spool", "ops", "replayed", pending[0])), self.log_bytes("ops")):
            self.assertNotIn(token.encode("ascii"), data)
            self.assertNotIn(password.encode("ascii"), data)
        last = self.chain("ops")[-1]
        self.assertEqual((last["type"], last["action"], last["spool_id"]), ("action", "shell", pending[0][:-5]))
        self.assertIn("[REDACTED]", last["target"])

    def test_caller_supplied_chain_fields_are_ignored(self):
        # N-15: v, seq, bot, prev and hash always come from the chain, also on a replayed event
        forged = {"v": 7, "seq": 99, "bot": "other", "prev": "ab" * 32, "hash": "cd" * 32}
        first = self.seed("ops", 1)[0]
        rec = self.ledger.append("ops", dict(forged, type="note", detail="x", ts=stamp(1)))
        self.assertEqual((rec["v"], rec["seq"], rec["bot"], rec["prev"]), (1, 2, "ops", first["hash"]))
        self.assertEqual(rec["hash"], gblog.compute_hash(rec))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [])
        payload = {"v": 1, "kind": "tail_mismatch", "ts": "2024-06-01T11:00:00Z", "bot": "ops",
                   "record": dict(forged, type="note", detail="planted")}
        self.write(self.path("spool", "ops", "20240601T110000Z-deadbeef.json"), json.dumps(payload))
        res = self.ledger.spool_replay("ops")
        self.assertEqual(len(res["replayed"]), 1, res)
        new = res["replayed"][0]
        self.assertEqual((new["v"], new["seq"], new["bot"], new["prev"]), (1, 3, "ops", rec["hash"]))
        self.assertEqual(new["hash"], gblog.compute_hash(new))
        self.assertEqual((new["detail"], new["spool_id"]), ("planted", "20240601T110000Z-deadbeef"))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    def test_replay_after_a_crash_between_append_and_move_does_not_duplicate(self):
        # N-17: the record is in the chain, other records followed, the spool file was never moved
        self.seed("ops", 2)
        self.break_state("ops")
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            self.ledger.append("ops", {"type": "note", "detail": "refused once"})
        spool = ctx.exception.spool_path
        self.restore_state("ops")
        with open(spool, encoding="utf-8") as fh:
            payload = json.load(fh)
        name = os.path.basename(spool)
        self.ledger.append("ops", dict(payload["record"], spooled_at=payload["ts"], spool_id=name[:-5]))
        for n in (1, 2):
            self.ledger.append("ops", {"type": "note", "detail": "later %d" % n})
        res = self.ledger.spool_replay("ops")
        self.assertEqual((res["replayed"], res["skipped"], res["refused"], res["remaining"]), ([], [], False, 0))
        self.assertEqual(self.pending_spool("ops"), [])
        self.assertTrue(os.path.isfile(self.path("spool", "ops", "replayed", name)))
        self.assertEqual([r.get("detail") for r in self.chain("ops")].count("refused once"), 1)
        self.assertTrue(res["alert_resolved"])
        self.assertFalse(os.path.exists(self.path("state", "ops.alert.json")))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 5)

    def test_planted_break_ack_stays_in_the_spool_and_is_reported(self):
        # section 3 (MiscHardeningTest): the file stays, `rejected` lists it, the CLI exits 2
        self.seed("ops", 2)
        brk = {k: v for k, v in gblog.KIM_BISEO_BREAK.items() if k != "bot"}
        payload = {"v": 1, "kind": "tail_mismatch", "ts": "2024-06-01T11:00:00Z", "bot": "ops",
                   "record": {"type": "break_ack", "jay_approval_id": "NOT-JAY-0001", "detail": "planted", "break": brk}}
        planted = self.write(self.path("spool", "ops", "20240601T110000Z-deadbeef.json"), json.dumps(payload))
        res = self.ledger.spool_replay("ops")
        self.assertEqual((res["rejected"], res["replayed"], res["remaining"]), ([planted], [], 1))
        self.assertTrue(os.path.isfile(planted))
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("holds a break_ack", err)
        self.assertIn("replayed 0 spooled event(s) for ops", out)
        self.assertTrue(os.path.isfile(planted))
        self.assertNotIn("break_ack", [r.get("type") for r in self.chain("ops")])

    def test_unreadable_spool_file_is_exit_2_and_left_in_place(self):
        self.seed("ops", 1)
        planted = self.write(self.path("spool", "ops", "20240601T110000Z-00000000.json"), "{not json")
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is unreadable; left in place", err)
        self.assertIn("replayed 0 spooled event(s) for ops", out)
        self.assertTrue(os.path.isfile(planted))
        self.assertEqual(len(self.chain("ops")), 1)


# --------------------------------------------------------------------------- N-08, N-14, N-16, N-30
class AppendGapTest(Base):
    def test_state_recreated_without_an_append_leaves_a_marker(self):
        # N-08: sync / capture re-create state through update_state; the marker lives in state itself
        self.seed("ops", 3)
        log = self.ledger.log_files("ops")[-1]
        self.write(log, "".join(line + "\n" for line in self.read(log).splitlines()[:-1]))
        os.remove(self.path("state", "ops.json"))
        last_sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234", "pushed": False}
        wrote, err = self.quiet(self.ledger.update_state, "ops", {"last_sync": last_sync})
        self.assertIs(wrote, True)
        self.assertIn("notice: state/ops.json was missing; adopted the log tail (seq 2)", err)
        state = self.raw_state("ops")
        marker = state["state_adopted"]
        self.assertEqual((marker["how"], marker["seq"]), ("missing", 2))
        self.assertTrue(gblog._is_norm_ts(marker["ts"]), marker)
        self.assertEqual((state["seq"], state["last_sync"]), (2, last_sync))
        text = ("state/ops.json was re-created from the log tail at seq 2 (it was %s): truncation before that record "
                "cannot be ruled out from state")
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [text % "missing"])
        # Since round-3 W-15 the next append moves the marker into the chain (recorded once, hash-covered):
        # the warning now names that record instead of state, and state no longer carries the key.
        self.ledger.append("ops", {"type": "note", "detail": "after the adoption"})
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)
        self.assertEqual(len(res["warnings"]), 1, res["warnings"])
        self.assertIn("state/ops.json was missing", res["warnings"][0])
        self.assertIn("cannot be ruled out from state", res["warnings"][0])
        self.assertEqual(len([r for r in self.chain("ops") if "state_adopted" in r]), 1)
        # an unparsable state file is "unreadable"
        self.seed("torn", 2)
        self.write(self.path("state", "torn.json"), "{not json")
        wrote, err = self.quiet(self.ledger.update_state, "torn", {"last_sync": last_sync})
        self.assertIs(wrote, True)
        marker = self.raw_state("torn")["state_adopted"]
        self.assertEqual((marker["how"], marker["seq"]), ("unreadable", 2))
        res = self.ledger.verify("torn")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [text.replace("ops", "torn") % "unreadable"])

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-specific")
    def test_append_fsync_order(self):
        # N-14: the line is durable before state is replaced; the chain directory is fsynced only for a new day file
        self.seed("ops", 1)
        log = self.ledger.log_files("ops")[0]
        state = self.ledger._state_path("ops")
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            st = os.fstat(fd)
            events.append(("fsync", (st.st_dev, st.st_ino), stat.S_ISREG(st.st_mode)))
            return real_fsync(fd)

        def replace(src, dst, *args, **kwargs):
            events.append(("replace", os.path.abspath(dst), None))
            return real_replace(src, dst, *args, **kwargs)

        def ident(path):
            st = os.stat(path)
            return (st.st_dev, st.st_ino)

        with mock.patch.object(gblog.os, "fsync", fsync), mock.patch.object(gblog.os, "replace", replace):
            self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual([e[0] for e in events], ["fsync", "fsync", "replace", "fsync"])
        self.assertEqual(events[0][1], ident(log))                    # 1. the day file
        self.assertTrue(events[1][2])                                 # 2. a regular file ...
        self.assertNotEqual(events[1][1], ident(log))                 # ... that is not the day file:
        self.assertEqual(events[1][1], ident(state))                  # the temp file that became state
        self.assertEqual(events[2][1], os.path.abspath(state))        # 3. replaced onto state/ops.json
        self.assertEqual(events[3][1], ident(self.path("state")))     # 4. the state directory
        self.assertNotIn(ident(self.path("logs", "ops")), [e[1] for e in events])

        del events[:]
        with mock.patch.object(gblog.os, "fsync", fsync), mock.patch.object(gblog.os, "replace", replace):
            self.ledger.append("ops", {"type": "note", "detail": "next day", "ts": stamp(0, "2024-06-02")})
        new_log = self.ledger.log_files("ops")[-1]
        self.assertNotEqual(new_log, log)
        self.assertEqual([e[0] for e in events], ["fsync", "fsync", "fsync", "replace", "fsync"])
        self.assertEqual(events[0][1], ident(new_log))
        self.assertEqual(events[1][1], ident(self.path("logs", "ops")))
        self.assertEqual([e[1] for e in events].count(ident(self.path("logs", "ops"))), 1)
        self.assertEqual(events[4][1], ident(self.path("state")))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_archive_file_survives_a_failed_state_write(self):
        # N-16: the log line became durable, so the chain references the file and it must stay
        self.seed("ops", 1)
        with self.flaky_write_state():
            with self.assertRaises(OSError):
                gblog.archive_file(self.ledger, "ops", b"evidence that is chained\n", "e.txt", "artifact", ts=stamp(5))
        stored = []
        for dirpath, _dirs, files in os.walk(self.path("archive")):
            stored.extend(os.path.join(dirpath, f) for f in files)
        self.assertEqual(len(stored), 1, stored)
        last = self.chain("ops")[-1]
        self.assertEqual((last["type"], last["seq"]), ("archive", 2))
        self.assertEqual(self.path(*last["path"].split("/")), stored[0])
        self.assertEqual(self.raw_state("ops")["seq"], 1)
        _rec, err = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "next", "ts": stamp(6)})
        self.assertIn("notice: recovered state/ops.json after an interrupted append", err)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["archives"]), (3, 1))

    def test_failed_state_write_leaves_no_temp_file(self):
        # N-30
        self.seed("ops", 1)
        real = os.replace
        calls = []

        def flaky(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) == 1:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), dst)
            return real(src, dst, *args, **kwargs)

        with mock.patch.object(gblog.os, "replace", flaky):
            with self.assertRaises(OSError):
                self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual(len(calls), 1)
        self.assertEqual([n for n in os.listdir(self.path("state")) if n.endswith(".tmp")], [])
        self.assertEqual(len(self.chain("ops")), 2)  # the line was durable before state was touched
        self.assertEqual(self.raw_state("ops")["seq"], 1)
        rec, err = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "three", "ts": stamp(2)})
        self.assertIn("notice: recovered state/ops.json after an interrupted append", err)
        self.assertEqual(rec["seq"], 3)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_tmp_name_is_per_process(self):
        # N-30
        self.assertEqual(gblog._tmp_name("/x/y.json"), "/x/y.json.%d.tmp" % os.getpid())

    @unittest.skipUnless(getattr(os, "O_NOFOLLOW", 0), "O_NOFOLLOW is not available here")
    def test_planted_tmp_symlink_is_refused_not_followed(self):
        # N-30: the link sits at the name the code really opens (state/ops.json.<pid>.tmp)
        self.seed("ops", 1)
        victim = self.write(os.path.join(self.base, "victim.txt"), "precious\n")
        state = self.ledger._state_path("ops")
        self.symlink(victim, gblog._tmp_name(state))
        with self.assertRaises(OSError):
            self.ledger.append("ops", {"type": "note", "detail": "after the plant", "ts": stamp(5)})
        self.assertEqual(self.read(victim), "precious\n")
        self.assertFalse(os.path.islink(state))
        self.assertFalse(os.path.lexists(gblog._tmp_name(state)))

    def test_roll_forward_needs_the_next_seq_as_well_as_the_link(self):
        # the `tail.seq == state.seq + 1` half of the roll-forward rule
        recs = self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 5, "head": recs[0]["hash"]})  # the head is the tail's prev, the seq is not
        before = self.log_bytes("ops")
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("ops", {"type": "note", "detail": "must be spooled", "ts": stamp(9)})
        self.assertEqual(self.log_bytes("ops"), before)
        self.assertEqual(len(self.pending_spool("ops")), 1)

    def test_future_bound_is_300_seconds(self):
        # the upper side of both 300 s bounds
        now = "2024-06-01T10:00:00Z"
        self.assertFalse(gblog._future_ts("2024-06-01T10:05:00Z", now))
        self.assertTrue(gblog._future_ts("2024-06-01T10:05:01Z", now))
        self.assertFalse(gblog._future_ts("2024-06-01T10:05:01+00:00", now))  # not a normalised timestamp
        base = _dt.datetime.now(_dt.timezone.utc)
        near = (base + _dt.timedelta(seconds=250)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = (base + _dt.timedelta(seconds=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rc, out, err = run(["note", "--bot", "far", "--detail", "x", "--ts", far], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is more than 300 seconds in the future", err)
        self.assertEqual(self.ledger.log_files("far"), [])
        rc, out, err = run(["note", "--bot", "near", "--detail", "x", "--ts", near], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.chain("near")[-1]["ts"], near)


# --------------------------------------------------------------------------- N-07
def _tail(n):
    return ("A1b2C3d4E5f6G7h8I9j0" * 3)[:n]


class RedactionGapTest(Base):
    def test_every_documented_vendor_shape(self):
        # N-07: every token is assembled from pieces, so this file holds no secret-looking literal
        shapes = [("github" + "_pat_", 22)]
        shapes += [(prefix + "_", 24) for prefix in ("gho", "ghu", "ghs", "ghr", "ghp")]
        shapes += [("xox%s-" % kind, 12) for kind in "abprs"]
        shapes += [("sk" + "-", 20), ("xai" + "-", 20), ("AI" + "za", 35)]
        for prefix, n in shapes:
            with self.subTest(prefix=prefix):
                token = prefix + _tail(n)
                out = gblog.redact("use %s now" % token)
                self.assertEqual(out, "use [REDACTED] now")
                self.assertNotIn(_tail(n), out)
                self.assertEqual(gblog.redact(out), out)

    def test_every_documented_query_parameter(self):
        # N-07
        for name in ("token", "access_token", "refresh_token", "api_key", "apikey", "key", "secret", "password",
                     "pwd", "sig", "signature", "auth", "code"):
            with self.subTest(parameter=name):
                out = gblog.redact("https://h.example/p?%s=VALUE123&x=1" % name)
                self.assertEqual(out, "https://h.example/p?%s=[REDACTED]&x=1" % name)
                self.assertEqual(gblog.redact(out), out)
        # a parameter that is not on the list is left alone
        self.assertEqual(gblog.redact("https://h.example/p?page=VALUE123&x=1"), "https://h.example/p?page=VALUE123&x=1")

    def test_archive_with_invalid_bytes_is_scanned_and_keeps_those_bytes(self):
        # section 3 (RedactionTest): the stray bytes are stored unchanged
        secret = b"abcdef" + b"0123456789"
        data = b"user said: token=" + secret + b" please\n\xff\xfe trailing bytes\n"
        rec, err = self.quiet(gblog.archive_file, self.ledger, "ops", data, "export.txt", "transcript", ts=stamp(0))
        self.assertNotIn("stored without secret scanning", err)
        with gzip.open(self.path(*rec["path"].split("/")), "rb") as fh:
            stored = fh.read()
        self.assertNotIn(secret, stored)
        self.assertIn(b"token=[REDACTED] please\n", stored)
        self.assertIn(b"\xff\xfe trailing bytes\n", stored)
        self.assertIs(rec["redacted"], True)
        self.assertNotIn("scanned", rec)
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- N-18, N-19
class TuneRulesGapTest(unittest.TestCase):
    """analyze() on hand-built record dicts: no ledger, no fsync."""

    TARGET = "post weekly summary"

    def _approval(self, bot, seq, decision):
        return {"v": 1, "seq": seq, "bot": bot, "ts": ago(5, seq), "type": "approval", "target": self.TARGET,
                "decision": decision, "outcome": "ok" if decision == "allowed" else "denied"}

    def test_allow_rule_needs_three_approvals_and_no_denial(self):
        # N-18
        cases = (
            ("a", ["allowed"] * 3),
            ("b", ["allowed"] * 3 + ["denied"]),
            ("c", ["allowed"] * 2),
            ("d", ["allowed", "denied", "allowed", "denied", "allowed"]),
            ("e", ["allowed", "denied", "allowed", "denied"]),
        )
        recs = []
        for bot, decisions in cases:
            recs += [self._approval(bot, i + 1, decision) for i, decision in enumerate(decisions)]
        by = {}
        for p in gblog.analyze(recs, 28, None)["proposals"]:
            by.setdefault(p["bot"], []).append(p)
        self.assertEqual([(p["category"], p["key"]) for p in by.get("a", [])], [("approval", "allow:" + self.TARGET)])
        allow = by["a"][0]
        self.assertIn("approved 3 times without a single denial", allow["title"])
        self.assertEqual((allow["count"], allow["baseline"], allow["metric"]["kind"]), (3, 3.0, "approvals"))
        for bot in ("b", "c", "d"):  # one denial is enough to keep the action behind an approval
            self.assertEqual(by.get(bot, []), [], bot)
        # as many denials as approvals: the opposite proposal
        self.assertEqual([(p["category"], p["key"]) for p in by.get("e", [])], [("approval", "deny:" + self.TARGET)])
        self.assertIn("was denied 2 of 4 times", by["e"][0]["title"])

    def _tune(self, bot, pid, days):
        return {"v": 1, "seq": 1, "bot": bot, "ts": ago(days), "type": "tune", "action": "other", "target": pid,
                "outcome": "ok", "detail": "applied",
                "proposal": {"title": "Fix for %s" % bot, "category": "skill", "key": "fail:connector:quickbooks.sync",
                             "metric": {"kind": "fail", "bot": bot, "action": "connector", "key": "quickbooks.sync"},
                             "baseline": 14.0, "better": "lower",
                             "window": {"since": ago(days + 28), "until": ago(days)}}}

    def _fail(self, bot, seq, days):
        return {"v": 1, "seq": seq, "bot": bot, "ts": ago(days), "type": "action", "action": "connector",
                "target": "quickbooks.sync", "outcome": "fail", "detail": "401"}

    def test_effect_verdicts_and_revert(self):
        # N-19: 0.5 failures a day before; 0.8 a day after for `worse`, 0.1 for `better`
        recs = [self._tune("worse", "effworse1", 10)]
        recs += [self._fail("worse", i + 2, days) for i, days in enumerate(range(9, 1, -1))]
        recs += [self._tune("better", "effbetter", 10), self._fail("better", 2, 5)]
        recs += [self._tune("fresh", "efffresh1", 2)]
        analysis = gblog.analyze(recs, 28, None)
        effects = {e["id"]: e for e in analysis["effects"]}
        self.assertEqual(sorted(effects), ["effbetter", "efffresh1", "effworse1"])
        self.assertEqual((effects["effworse1"]["after"], effects["effbetter"]["after"]), (8.0, 1.0))
        self.assertIs(effects["effworse1"]["improved"], False)
        self.assertIs(effects["effbetter"]["improved"], True)
        reverts = [(p["bot"], p["key"]) for p in analysis["proposals"] if p["category"] == "revert"]
        self.assertEqual(reverts, [("worse", "revert:effworse1")])
        md = gblog.render_tune(analysis, "T")
        rows = {line.split("|")[1].strip(): line for line in md.splitlines() if line.startswith("| eff")}
        self.assertIn("**not helping**", rows["effworse1"])
        self.assertTrue(rows["effbetter"].rstrip().endswith("| improving |"), rows["effbetter"])
        self.assertNotIn("not helping", rows["effbetter"])
        self.assertIn("too early (", rows["efffresh1"])


# --------------------------------------------------------------------------- N-20
def _reference_coverage(records, window_minutes=30):
    """The plain nested loop that coverage() must agree with."""
    auto = [r for r in records if r.get("type") == "action" and r.get("source") == "auto"]
    selfs = [r for r in records if r.get("type") == "action" and r.get("source") != "auto"]
    window = window_minutes * 60
    used, matched, unmatched = set(), 0, []
    for a in auto:
        t_a = gblog._ts_epoch(a.get("observed") or a.get("ts"))
        want = gblog.norm_target(a.get("action"), a.get("target"))
        hit = None
        for i, s in enumerate(selfs):
            if i in used or s.get("bot") != a.get("bot") or s.get("action") != a.get("action"):
                continue
            if not want or gblog.norm_target(s.get("action"), s.get("target")) != want:
                continue
            if abs(gblog._ts_epoch(s.get("ts")) - t_a) <= window:
                hit = i
                break
        if hit is None:
            unmatched.append(a)
        else:
            used.add(hit)
            matched += 1
    return len(auto), matched, unmatched


def _action(kind, target, ts, auto=False, bot="ops", **extra):
    rec = {"bot": bot, "type": "action", "action": kind, "target": target, "ts": ts}
    if auto:
        rec["source"] = "auto"
    rec.update(extra)
    return rec


class CoverageRulesTest(unittest.TestCase):
    D = "2026-01-01T"

    def test_window_is_minutes(self):
        recs = [_action("shell", "git status", self.D + "10:00:00Z"),
                _action("shell", "git push", self.D + "10:20:00Z", auto=True)]
        self.assertEqual(gblog.coverage(recs)["matched"], 1)
        self.assertEqual(gblog.coverage(recs, window_minutes=30)["matched"], 1)
        self.assertEqual(gblog.coverage(recs, window_minutes=20)["matched"], 1)  # the bound is inclusive
        cov = gblog.coverage(recs, window_minutes=10)
        self.assertEqual((cov["auto"], cov["matched"], cov["pct"]), (1, 0, 0.0))

    def test_one_report_covers_one_observation(self):
        recs = [_action("shell", "git status", self.D + "10:00:00Z"),
                _action("shell", "git push", self.D + "10:20:00Z", auto=True),
                _action("shell", "git log", self.D + "10:25:00Z", auto=True)]
        cov = gblog.coverage(recs)
        self.assertEqual((cov["auto"], cov["matched"], cov["pct"]), (2, 1, 50.0))
        self.assertEqual([r["target"] for r in cov["unmatched"]], ["git log"])
        self.assertEqual(cov["self_only"], 0)

    def test_observed_time_is_used(self):
        report = _action("browser", "https://example.org/", self.D + "10:00:00Z")
        imported_late = _action("browser", "https://example.org/x", self.D + "18:00:00Z", auto=True,
                                observed=self.D + "10:02:00Z")
        self.assertEqual(gblog.coverage([report, imported_late])["matched"], 1)
        observed_early = _action("browser", "https://example.org/x", self.D + "10:01:00Z", auto=True,
                                 observed=self.D + "03:00:00Z")
        self.assertEqual(gblog.coverage([report, observed_early])["matched"], 0)

    def test_other_bot_or_kind_never_matches(self):
        auto = _action("shell", "git push", self.D + "10:01:00Z", auto=True)
        self.assertEqual(gblog.coverage([_action("shell", "git status", self.D + "10:00:00Z", bot="other"), auto])["matched"], 0)
        self.assertEqual(gblog.coverage([_action("file", "git", self.D + "10:00:00Z"), auto])["matched"], 0)

    def test_matches_reference_implementation(self):
        rng = random.Random(1)
        targets = {
            "shell": ["git status", "git push", "ls -la", "make test", "python run.py"],
            "browser": ["https://github.com/a/b", "https://www.github.com/c", "https://example.org/x",
                        "https://docs.example.org/"],
            "file": ["a.txt", "dir/a.txt", "b.md"],
        }
        auto_only = {"shell": "rm -rf build", "browser": "https://never-reported.example/", "file": "unseen.bin"}
        start = _dt.datetime(2026, 1, 1, 8, 0, 0)
        recs = []
        for i in range(900):
            auto = i % 3 == 0  # 300 observed, 600 reported
            kind = rng.choice(sorted(targets))
            target = auto_only[kind] if auto and rng.randrange(10) == 0 else rng.choice(targets[kind])
            ts = (start + _dt.timedelta(seconds=rng.randrange(6 * 3600))).strftime("%Y-%m-%dT%H:%M:%SZ")
            recs.append(_action(kind, target, ts, auto=auto, bot=rng.choice(["a", "b"])))
        for window in (30, 5, 1):
            with self.subTest(window=window):
                want_auto, want_matched, want_unmatched = _reference_coverage(recs, window)
                cov = gblog.coverage(recs, window)
                self.assertEqual((cov["auto"], cov["matched"]), (want_auto, want_matched))
                self.assertEqual(cov["unmatched"], want_unmatched)
                self.assertEqual(sum(row["auto"] for row in cov["rows"]), want_auto)
                self.assertEqual(sum(row["matched"] for row in cov["rows"]), want_matched)
        want_auto, want_matched, _unmatched = _reference_coverage(recs, 30)
        self.assertEqual(want_auto, 300)
        self.assertTrue(0 < want_matched < want_auto, want_matched)


# --------------------------------------------------------------------------- N-21, N-25, N-26
class CaptureGapTest(Base):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.base, "work")
        self.write(os.path.join(self.work, "keep.txt"), "1")

    def _capture(self, *extra, **kwargs):
        rc, out, err = run(["capture", "files", "--bot", "ops", "--path", kwargs.get("path", self.work)] + list(extra),
                           self.root)
        self.assertEqual(rc, 0, out + err)
        return out, err

    def _snap(self):
        return self.path("state", "capture-files-ops.json")

    def test_future_and_out_of_range_visits_do_not_pin_the_marker(self):
        # N-21: one absurd visit time must not move the marker past every real visit
        base = int(time.time())
        hist = chrome_history(os.path.join(self.base, "chrome"), [
            ("https://a.example/first", "First", _webkit(base - 600)),
            ("https://future.example/later", "Future", _webkit(base + 10 * 365 * 86400)),
            ("https://broken.example/never", "Broken", 9 * 10 ** 18),
        ])
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual((res["added"], res["failed"]), (1, []))
        self.assertEqual(self.ledger.read_state("ops")["capture_browser_last"], {hist: _webkit(base - 600)})
        add_visit(hist, "https://c.example/third", "Third", _webkit(base - 60))
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual((res["added"], res["failed"]), (1, []))
        self.assertEqual([r["target"] for r in self.chain("ops")], ["https://a.example/first", "https://c.example/third"])
        self.assertEqual(self.ledger.read_state("ops")["capture_browser_last"], {hist: _webkit(base - 60)})
        res = gblog.capture_browser(self.ledger, "ops", [hist], hours=24)
        self.assertEqual(res["added"], 0)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_non_text_title_is_imported_once_as_text(self):
        # section 3 (CaptureTest): exit codes and the second visit, not only "no traceback"
        base = int(time.time())
        hist = chrome_history(os.path.join(self.base, "chrome"), [
            ("https://a.example/first", "First", _webkit(base - 120)),
            ("https://b.example/second", b"\x00\xff blob title", _webkit(base - 60)),
        ])
        outputs = []
        for _ in range(2):
            rc, out, err = run(["capture", "browser", "--bot", "ops", "--history", hist], self.root)
            self.assertEqual(rc, 0, out + err)
            outputs.append(out)
        self.assertIn("browser: 2 new visit(s) from 1 history file(s)", outputs[0])
        self.assertIn("browser: 0 new visit(s) from 1 history file(s)", outputs[1])
        seconds = [r for r in self.chain("ops") if r.get("target") == "https://b.example/second"]
        self.assertEqual(len(seconds), 1)
        self.assertIsInstance(seconds[0].get("detail"), str)
        self.assertIn("blob title", seconds[0]["detail"])
        self.assertEqual(len(self.chain("ops")), 2)

    def test_new_baseline_reason_is_on_the_output_line(self):
        # N-26
        line = "files: %d change(s) against a snapshot of %d file(s)%s\n"
        out, err = self._capture()
        self.assertEqual(out, line % (0, 1, " (new baseline: first run)"))
        self.assertNotIn("notice:", err)
        out, _err = self._capture()
        self.assertEqual(out, line % (0, 1, ""))
        self.write(os.path.join(self.work, "b.txt"), "x")
        out, _err = self._capture()
        self.assertEqual(out, line % (1, 1, ""))
        os.remove(self._snap())
        out, err = self._capture()
        self.assertEqual(out, line % (0, 2, " (new baseline: snapshot missing)"))
        self.assertIn("notice: state/capture-files-ops.json is missing", err)
        self.write(self._snap(), "{not json")
        out, err = self._capture()
        self.assertEqual(out, line % (0, 2, " (new baseline: snapshot unreadable)"))
        self.assertIn("is unreadable; taking a new baseline", err)
        other = os.path.join(self.base, "other")
        for name in ("x.txt", "y.txt", "z.txt"):
            self.write(os.path.join(other, name), name)
        out, err = self._capture(path=other)
        self.assertEqual(out, line % (0, 3, " (new baseline: path changed)"))
        self.assertIn("notice: capture path changed from", err)
        # the only change that was ever logged is the one found against a valid snapshot
        logged = [(r["target"], r["detail"]) for r in self.chain("ops") if r.get("capture") == "files"]
        self.assertEqual(logged, [("b.txt", "created")])

    def test_record_baseline_appends_one_note(self):
        # N-26
        out, _err = self._capture("--record-baseline")
        self.assertEqual(out, "files: 0 change(s) against a snapshot of 1 file(s) (new baseline: first run)\n")
        recs = self.chain("ops")
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["type"], recs[0].get("source"), recs[0].get("capture"), recs[0]["detail"]),
                         ("note", "auto", "files", "file capture: new baseline of 1 file(s) (first run)"))
        out, _err = self._capture("--record-baseline")  # no new baseline, so no note
        self.assertEqual(out, "files: 0 change(s) against a snapshot of 1 file(s)\n")
        self.assertEqual(len(self.chain("ops")), 1)
        # without the flag a new baseline appends nothing
        rc, out, err = run(["capture", "files", "--bot", "plain", "--path", self.work], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.chain("plain"), [])

    def test_pending_count_is_on_the_output_line(self):
        # N-26
        self._capture()
        for i in range(5):
            self.write(os.path.join(self.work, "new%d.txt" % i), "n")
        out, _err = self._capture("--limit", "3")
        self.assertEqual(out, "files: 5 change(s) against a snapshot of 1 file(s), 2 pending\n")
        targets = [r["target"] for r in self.chain("ops") if r.get("type") == "action"]
        self.assertEqual(targets, ["new0.txt", "new1.txt", "new2.txt"])
        out, _err = self._capture()
        self.assertEqual(out, "files: 2 change(s) against a snapshot of 4 file(s)\n")
        targets = [r["target"] for r in self.chain("ops") if r.get("type") == "action"]
        self.assertEqual(targets, ["new%d.txt" % i for i in range(5)])

    def test_exclude_skips_the_named_directory(self):
        self._capture("--exclude", "skipme")
        self.write(os.path.join(self.work, "skipme", "x.txt"), "x")
        self.write(os.path.join(self.work, "y.txt"), "y")
        out, _err = self._capture("--exclude", "skipme")
        self.assertEqual(out, "files: 1 change(s) against a snapshot of 1 file(s)\n")
        self.assertEqual([r["target"] for r in self.chain("ops")], ["y.txt"])

    @unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
    def test_shell_hook_skips_only_plain_gblog_commands(self):
        # N-25: the skipped side of the hook's rule, and the commands that only look plain
        bindir = os.path.join(self.base, "bin")
        fake = self.write(os.path.join(bindir, "gblog"), "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$GBLOG_FAKE_LOG\"\n")
        os.chmod(fake, 0o755)
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        log = os.path.join(self.base, "fake.log")

        def logged(cmd):
            if os.path.exists(log):
                os.remove(log)
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), GBLOG_FAKE_LOG=log,
                       HOOK=hook, CMD=cmd, HISTFILE=os.devnull)
            for key in ("HISTCONTROL", "HISTIGNORE", "PROMPT_COMMAND", "GBLOG_BOT", "GBLOG_TASK"):
                env.pop(key, None)
            subprocess.run([BASH, "--noprofile", "--norc", "-c", 'source "$HOOK"; history -s "$CMD"; __gblog_hook'],
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            return self.read(log) if os.path.exists(log) else ""

        control = logged("ls -la /tmp")  # the harness works: an ordinary command is captured
        self.assertIn("--target=ls -la /tmp", control)
        self.assertIn("--bot ops", control)
        self.assertIn("--source auto", control)
        plain = ("gblog status", "gblog",
                 'gblog log --bot "$BOT" --task-id "$TID" --action shell --target ls',
                 'gblog note --detail "${X} done"')
        self.assertEqual([cmd for cmd in plain if logged(cmd) != ""], [])
        carrying = ('gblog note --detail "$(id)"', 'gblog note --detail "`id`"', "gblog status > /tmp/x",
                    "gblog export < /dev/null", "gblog status & sleep 1",
                    "TID=$(gblog task-start --bot ops --task x)", "gblogx status")
        self.assertEqual([cmd for cmd in carrying if cmd not in logged(cmd)], [])


# --------------------------------------------------------------------------- N-22, N-23, N-24
class ReaderGapTest(Base):
    def _seed_days(self):
        L = self.ledger
        L.append("idle", {"type": "task_start", "task_id": "i1", "task": "Idle bot old task", "ts": "2024-06-01T09:00:00Z"})
        L.append("idle", {"type": "action", "task_id": "i1", "action": "shell", "target": "idle-old-command",
                          "outcome": "fail", "ts": "2024-06-01T09:01:00Z"})
        L.append("idle", {"type": "task_end", "task_id": "i1", "outcome": "fail", "ts": "2024-06-02T09:00:00Z"})
        L.append("busy", {"type": "note", "detail": "busy day before", "ts": "2024-06-02T23:59:59Z"})
        L.append("busy", {"type": "task_start", "task_id": "b1", "task": "Busy bot task", "ts": "2024-06-03T09:00:00Z"})
        L.append("busy", {"type": "action", "task_id": "b1", "action": "shell", "target": "busy-command",
                          "outcome": "ok", "ts": "2024-06-03T09:01:00Z"})
        L.append("busy", {"type": "task_end", "task_id": "b1", "outcome": "ok", "ts": "2024-06-03T09:02:00Z"})

    def test_since_excludes_earlier_days(self):
        # N-22
        self._seed_days()
        rc, out, err = run(["report", "--since", "2024-06-03", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("| Bots | 1 |", out)
        self.assertIn("| Records | 3 |", out)
        self.assertIn("### Busy bot task", out)
        self.assertNotIn("Idle bot old task", out)
        self.assertNotIn("busy day before", out)
        rc, out, err = run(["export", "--since", "2024-06-02"], self.root)
        self.assertEqual(rc, 0, out + err)
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["ts"] >= "2024-06-02" for row in rows), [row["ts"] for row in rows])
        self.assertEqual(len(self.ledger.chain_records("busy", since="2024-06-03")), 3)
        self.assertEqual(len(self.ledger.chain_records("busy")), 4)
        self.assertEqual(len(self.ledger.records(since="2024-06-02")), 5)
        self.assertEqual(len(self.ledger.records("idle", since="2024-06-03")), 0)

    def test_open_task_older_than_30_days_is_not_counted(self):
        # N-22: status looks at the last 30 days only
        self.ledger.append("ops", {"type": "task_start", "task_id": "old", "task": "Ancient", "ts": ago(45)})
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        row = {r["bot"]: r for r in json.loads(out)["bots"]}["ops"]
        self.assertEqual((row["seq"], row["open_tasks"], row["today"]), (1, 0, 0))

    def test_tail_matches_full_read_across_day_files(self):
        # N-23
        L = self.ledger
        plan = (("alpha", "2024-06-01", 2), ("alpha", "2024-06-02", 2), ("bravo", "2024-06-02", 3),
                ("alpha", "2024-06-03", 1))
        for bot, day, count in plan:
            for i in range(count):
                L.append(bot, {"type": "note", "detail": "%s %s %d" % (bot, day, i), "ts": "%sT10:00:%02dZ" % (day, i)})
        self.assertEqual(len(L.log_files("alpha")), 3)

        def keys(records):
            return [(r["ts"], r["bot"], r["seq"]) for r in records]

        full = L.records()
        self.assertEqual(len(full), 8)
        for n in (1, 2, 4, 5, 100):
            with self.subTest(n=n):
                self.assertEqual(keys(L.tail_records(None, n)), keys(full[-n:]))
                self.assertEqual(keys(L.tail_records("alpha", n)), keys(L.records("alpha")[-n:]))
        self.assertEqual(L.tail_records(None, 0), [])
        rc, out, err = run(["tail", "--bot", "alpha", "-n", "4"], self.root)
        self.assertEqual(rc, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        for line, seq in zip(lines, (2, 3, 4, 5)):
            self.assertIn(" alpha #%d note " % seq, line)

    def test_clamped_record_is_reported_on_its_wall_clock_day_only(self):
        # N-24: a record clamped across midnight is listed once, on the day its wall clock showed
        self.ledger.append("ops", {"type": "note", "detail": "first minute of the second day", "ts": "2024-06-02T00:00:10Z"})
        with mock.patch.object(gblog, "now_iso", return_value="2024-06-01T23:59:50Z"):
            rec = self.ledger.append("ops", {"type": "task_start", "task_id": "c1", "task": "Clamped work"})
        self.assertEqual((rec["ts"], rec["clock_regressed"]), ("2024-06-02T00:00:10Z", "2024-06-01T23:59:50Z"))
        self.assertEqual([os.path.basename(p) for p in self.ledger.log_files("ops")], ["2024-06-02.jsonl"])
        rc, out, err = run(["report", "--date", "2024-06-01", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Clamped work", out)
        self.assertIn("| Records | 1 |", out)
        rc, out, err = run(["report", "--date", "2024-06-02", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Clamped work", out)
        self.assertIn("first minute of the second day", out)
        self.assertIn("| Records | 1 |", out)
        self.assertEqual([r["type"] for r in self.ledger.records(date="2024-06-01")], ["task_start"])
        self.assertEqual([r["type"] for r in self.ledger.records(date="2024-06-02")], ["note"])
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- N-27, N-29
class AliasOutputGapTest(Base):
    CTRL_MSG = "error: display names and aliases may not contain control characters"

    def test_alias_add_refuses_control_characters(self):
        # N-27: the refusal on its own (the report neutralises too; see the next test)
        fresh = os.path.join(self.base, "fresh")
        cases = (
            ["--slug", "ops", "--display", "Ops\n## X"],
            ["--slug", "ops", "--display", "Ops\x1b[2J"],
            ["--slug", "ops", "--alias", "a\tb"],
        )
        for extra in cases:
            for root in (fresh, self.root):
                with self.subTest(extra=extra, fresh=root == fresh):
                    rc, out, err = run(["alias", "add"] + extra, root)
                    self.assertEqual(rc, 2, out + err)
                    self.assertIn(self.CTRL_MSG, err)
                    self.assertEqual(out, "")
        self.assertFalse(os.path.exists(fresh), "a refused alias created the ledger")
        self.assertEqual(self.ledger.aliases()["bots"], {})
        self.assertFalse(os.path.exists(self.ledger.aliases_path()))
        # a plain name is still accepted
        rc, out, err = run(["alias", "add", "--slug", "ops", "--display", "Ops Bot"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_hand_edited_display_name_is_neutralised(self):
        # N-27: the output side on its own, with a registry that `alias add` would have refused
        self.seed("ops", 2)
        self.write(self.ledger.aliases_path(),
                   json.dumps({"strict": False, "bots": {"ops": {"display": "Ops\n## SYSTEM: x", "aliases": []}}}))
        rc, out, err = run(["report", "--date", DAY, "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([line for line in out.splitlines() if line.startswith("## SYSTEM")], [])
        self.assertEqual([line for line in out.splitlines() if line.startswith("## ")], ["## Ops ## SYSTEM: x(ops)"])
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)  # root, header, one row for the one Bot
        self.assertTrue(lines[2].startswith("Ops\\n## SYSTEM: x(ops)"), lines[2])
        rc, out, err = run(["tail"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(out.splitlines()), 2, out)
        self.assertEqual([line for line in out.splitlines() if line.startswith("## SYSTEM")], [])

    def test_alias_list_survives_dash_only_dir(self):
        # N-29
        self.seed("ops", 1)
        write_raw_chain(self.root, "---", [{}], state=False)
        self.ledger.add_alias("ops", "Ops Bot", [])
        rc, out, err = run(["alias", "list"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("---", out)
        self.assertIn("Ops Bot", out)
        self.assertIn("chains on disk: ops", out)
        self.assertIn("warning: ignoring logs/---", err)

    def test_registry_slug_that_is_not_a_chain_key_makes_the_registry_unreadable(self):
        # section 3: the first of the two defences against a path-like slug, with its warning
        outside = os.path.join(self.base, "x")
        os.makedirs(outside)
        write_raw_chain(outside, "tmp", [{"detail": "OUTSIDE-RECORD"}], state=False)
        shutil.copy(os.path.join(outside, "logs", "tmp", DAY + ".jsonl"), os.path.join(outside, DAY + ".jsonl"))
        self.write(self.ledger.aliases_path(),
                   json.dumps({"strict": False, "bots": {"../../x": {"display": "Outside", "aliases": []}}}))
        rc, out, err = run(["report", "--bot", "Outside", "--date", DAY, "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("OUTSIDE-RECORD", out)
        self.assertIn("warning: state/aliases.json is unreadable", err)
        chains, _err = self.quiet(self.ledger.chains_for, "../../x")
        self.assertEqual(chains, ["x"])  # never a path with a separator or a dot-dot component


# --------------------------------------------------------------------------- N-10
class ArchiveClaimGapTest(Base):
    def _claim(self):
        rc, out, err = run(["log", "--bot", "ops", "--type", "archive", "--target", "contract.pdf"], self.root)
        self.assertEqual(rc, 0, out + err)
        rec = self.chain("ops")[-1]
        self.assertEqual((rec["type"], rec["target"]), ("archive", "contract.pdf"))
        self.assertNotIn("path", rec)
        self.assertNotIn("sha256", rec)
        day = os.path.basename(self.ledger.log_files("ops")[0])[:-len(".jsonl")]
        return "logs/ops/%s.jsonl:1: archive record without path and sha256: no archived file backs it" % day

    def test_log_type_archive_is_a_warning_not_a_fail(self):
        text = self._claim()
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [text])
        for argv in (["verify", "--bot", "ops"], ["verify", "--bot", "ops", "--skip-archives"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(out.startswith("OK"), out)
                self.assertIn("      ! " + text, out.splitlines())
        rc, out, err = run(["verify", "--bot", "ops", "--strict"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(out.startswith("FAIL"), out)
        self.assertIn("      - " + text, out.splitlines())
        strict = self.ledger.verify("ops", strict=True)
        self.assertEqual(strict["problems"], [text])
        # the chain keeps working after the claim
        rc, out, err = run(["note", "--bot", "ops", "--detail", "still appending"], self.root)
        self.assertEqual(rc, 0, out + err)

    # bug: D-8. A bare `archive` record names no file ("a claim, not stored evidence"), yet verify counts
    # it in the "(N archived file(s) intact)" suffix of the OK line.
    def test_bare_archive_record_is_not_counted_as_intact(self):
        self._claim()
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("archived file(s) intact", out.splitlines()[0])


# --------------------------------------------------------------------------- N-13
class InternalErrorGapTest(Base):
    def test_internal_error_is_one_line_and_exit_1(self):
        with mock.patch.object(gblog.Ledger, "tail_records", side_effect=RuntimeError("boom\nsecond line")):
            rc, out, err = _support_run(["tail"], self.root)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, INTERNAL_LINE)

    def test_debug_env_reraises(self):
        with mock.patch.dict(os.environ, {"GBLOG_DEBUG": "1"}), \
                mock.patch.object(gblog.Ledger, "tail_records", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_raw(["--root", self.root, "tail"], catch=False)

    def test_error_in_one_chain_is_a_fail_entry_that_names_it(self):
        # what `verify --all` does with an unexpected error today: FAIL for that chain, the others go on
        self.seed("aaa", 1)
        self.seed("zzz", 1)
        real = gblog.Ledger.verify

        def flaky(ledger, bot, *args, **kwargs):
            if bot == "aaa":
                raise RuntimeError("unexpected bug")
            return real(ledger, bot, *args, **kwargs)

        with mock.patch.object(gblog.Ledger, "verify", flaky):
            rc, out, err = run(["verify", "--all"], self.root, internal_ok=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - verify aborted: RuntimeError: unexpected bug", out.splitlines())
        self.assertRegex(out, r"(?m)^FAIL\s+aaa\s+0 records")
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")
        self.assertEqual(err, "")

    # bug: D-6. docs/DESIGN.md section 11 says GBLOG_DEBUG=1 re-raises "also from inside verify --all and
    # anchor"; _verify_guarded never reads the variable and turns the exception into a FAIL entry.
    def test_debug_env_reraises_from_verify_all(self):
        self.seed("ops", 1)
        with mock.patch.dict(os.environ, {"GBLOG_DEBUG": "1"}), \
                mock.patch.object(gblog.Ledger, "verify", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_raw(["--root", self.root, "verify", "--all"], catch=False)


# --------------------------------------------------------------------------- section 5 (D-1 .. D-7)
class DocumentedButAbsentTest(Base):
    """Behaviour the documentation describes and the code does not have. Each test fails today."""

    # bug: D-1. The daily routine prompt, DESIGN sections 8/13 and the CHANGELOG use
    # `gblog anchor --skip-archives`; the anchor parser has no such flag and exits 2.
    def test_anchor_skip_archives(self):
        rec = gblog.archive_file(self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        with gzip.open(self.path(*rec["path"].split("/")), "wb") as fh:
            fh.write(b"something else")
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        rc, out, err = run(["anchor", "--skip-archives"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ANCHOR OK", out)

    # bug: D-2. README and CHANGELOG say that running `capture shell-hook --install` again replaces an
    # older hook block in place; install_shell_hook returns False whenever any block exists.
    def test_install_replaces_outdated_hook(self):
        rc_file = os.path.join(self.base, "bashrc")
        old = "# >>> gblog shell hook >>>\n# an older hook body\n# <<< gblog shell hook <<<\n"
        self.write(rc_file, "# my settings\n" + old)
        self.assertTrue(gblog.install_shell_hook("ops", rc_file))
        text = self.read(rc_file)
        self.assertEqual(text.count(">>> gblog shell hook >>>"), 1)
        self.assertIn("__gblog_hook", text)
        self.assertIn("# my settings", text)
        self.assertNotIn("an older hook body", text)

    # bug: D-3. The CHANGELOG says a counter such as `max_tokens=100000` is left as written; the
    # glued-name redaction rule turns it into `max_tokens=[REDACTED]`.
    def test_token_counters_untouched(self):
        for text in ("max_tokens=100000", "prompt_tokens: 123456", "--max-tokens=200000"):
            self.assertEqual(gblog.redact(text), text)

    # bug: D-4. CHANGELOG, DESIGN section 13 and FAQ 35 say the "Effect of applied changes" table covers
    # changes applied in the last 90 days (default --days 28); `tune` loads the whole history.
    def test_effect_table_limited_to_90_days(self):
        proposal = {"title": "Old change", "category": "skill", "key": "fail:connector:x.sync",
                    "metric": {"kind": "fail", "bot": "ops", "action": "connector", "key": "x.sync"},
                    "baseline": 3.0, "better": "lower", "window": {"since": ago(228), "until": ago(200)}}
        self.ledger.append("ops", {"type": "tune", "action": "other", "target": "old00001", "outcome": "ok",
                                   "detail": "applied long ago", "proposal": proposal, "ts": ago(200)})
        rc, out, err = run(["tune", "--days", "28", "--json", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["effects"], [])

    # bug: D-7. `status --json` and `tune --json` write ensure_ascii=False text through a backslashreplace
    # stream; on an ASCII stdout a non-ASCII display name becomes `B\xfcro`, which is not a JSON escape.
    def test_json_outputs_are_valid_on_ascii_stdout(self):
        self.quiet(self.ledger.add_alias, "chief", "B\u00fcro Chef", [])
        self.ledger.append("chief", {"type": "note", "detail": "caf\u00e9 visit"})
        for argv in (["status", "--json"], ["tune", "--json", "--stdout"]):
            rc, out, err = run_cli(argv, self.root, "ascii")
            self.assertEqual(rc, 0, err)
            json.loads(out)


if __name__ == "__main__":
    unittest.main()
