"""Tests for three deep subsystem reviews of gblog 0.6.1 (second file).

* DR1: verify, the chain walk, state witnesses and anchors (ids D1-..)
* DR3: sync, git handling and remote visibility (ids D3-..)
* DR4: capture, coverage and tune (ids D4-..)

Every finding rated Medium or higher has at least one test, written to the fixed behaviour. A test
marked ``@unittest.expectedFailure`` documents behaviour the reviews ask for and gblog.py does not
have yet; it is listed in ``tests/PENDING.txt``.

Everything runs in temporary directories. No network is used: git remotes are local bare
repositories, git runs with GIT_ALLOW_PROTOCOL=file, and the visibility lookup, ``gh`` and
``urlopen`` are replaced by fakes.
"""
import calendar
import datetime as _dt
import errno
import io
import json
import os
import shutil
import sqlite3
import stat
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
from _support import DAY, GIT_ENV, chrome_history, git, run_cli, stamp  # noqa: E402
from _support import AfterRelease as _AfterRelease, FakeBody as _Body  # noqa: E402  (round-4 G2-05)
from _support import run_strict as run  # noqa: E402  (K6-01: a swallowed crash fails the test)

GIT = shutil.which("git")
BASH = shutil.which("bash")
T0 = calendar.timegm((2024, 6, 1, 10, 0, 0))  # 2024-06-01T10:00:00Z
ROLLUP = {"mode": "per-chain", "whole_ledger_skip": False, "ts": "2024-06-01T12:00:00Z"}


# git(*args) is tests/_support.git: it never inherits GIT_DIR or GIT_INDEX_FILE (K6-05).


def commits(repo):
    res = subprocess.run(["git", "-C", repo, "rev-list", "--all", "--count"], capture_output=True, text=True)
    return int(res.stdout.strip() or 0) if res.returncode == 0 else 0


def ago(days, seconds=0):
    """A UTC timestamp ``days`` days (plus ``seconds``) before now."""
    when = gblog._utcnow() - _dt.timedelta(days=days) + _dt.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def rec(seq, bot="ops", **fields):
    out = {"v": 1, "seq": seq, "bot": bot}
    out.update(fields)
    return out


# chrome_history, _Body and _AfterRelease are shared helpers of tests/_support.py (round-4 G2-05).


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-deepb-"))
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

    def seed(self, bot="ops", n=2):
        return [self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1), "ts": stamp(i)})
                for i in range(n)]

    def chain(self, bot="ops"):
        return [r for _path, _lineno, r in self.ledger.iter_records(bot)]

    def raw_state(self, bot="ops"):
        with open(self.path("state", bot + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def quiet(self, fn, *args, **kwargs):
        """Call a library function; returns ``(result, what it wrote to stderr)``."""
        err = io.StringIO()
        with redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, err.getvalue()

    def raw_line(self, bot, seq, prev, **extra):
        """A hash-valid line appended straight to the day file, bypassing gblog."""
        line = dict({"v": 1, "seq": seq, "ts": stamp(50), "bot": bot, "type": "note", "prev": prev}, **extra)
        line["hash"] = gblog.compute_hash(line)
        with open(self.path("logs", bot, DAY + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        return line

    def fifo(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.mkfifo(path)
        except OSError:
            self.skipTest("FIFOs are not supported here")

    def truncate(self, keep, bot="ops"):
        path = self.ledger.log_files(bot)[0]
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines[:keep]))

    def drop_witnesses(self, bot="ops"):
        """Remove state, the latest anchor and the anchor history: only day rollups are left."""
        for parts in (("state", bot + ".json"), ("anchors", bot + ".json"), ("anchors", bot + ".history.jsonl")):
            if os.path.exists(self.path(*parts)):
                os.remove(self.path(*parts))


# --------------------------------------------------------------------------- D1-01
@unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is not available")
class SideFileTypeTest(Base):
    def test_fifo_side_files_do_not_hang_verify_all(self):
        self.seed("ops", 2)
        self.seed("zzz", 1)
        for parts in (("state", "ghost.json"), ("anchors", "days", "2020-01-01.json"), ("anchors", "ops.history.jsonl")):
            self.fifo(self.path(*parts))
        rc, out, err = run_cli(["verify", "--all"], self.root, timeout=60)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertRegex(out, r"(?m)^FAIL\s+ops\s")
        self.assertIn("      - anchors/ops.history.jsonl: not a regular file", out.splitlines())
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")
        self.assertNotIn("ghost", out)

    @unittest.skipUnless(os.path.exists("/dev/zero"), "/dev/zero is not available")
    def test_symlink_to_device_is_not_read(self):
        self.seed("ops", 2)
        self.seed("zzz", 1)
        try:
            os.symlink("/dev/zero", self.path("state", "ghost.json"))
            os.symlink("/dev/zero", self.path("anchors", "ops.json"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported here")
        rc, out, err = run_cli(["verify", "--all"], self.root, timeout=60)
        # Never read (no hang on /dev/zero). Since specialist review K2 a symlinked side file is a problem
        # for its chain, not just a warning; other chains are unaffected.
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertRegex(out, r"(?m)^FAIL\s+ops\s")
        self.assertIn("anchors/ops.json: not a regular file", out)
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")

    def test_fifo_archive_is_a_problem_not_a_hang(self):
        arch, _err = self.quiet(gblog.archive_file, self.ledger, "aaa", b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        self.seed("zzz", 1)
        full = self.path(*arch["path"].split("/"))
        os.remove(full)
        self.fifo(full)
        rc, out, err = run_cli(["verify", "--all"], self.root, timeout=60)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - %s: not a regular file" % arch["path"], out.splitlines())
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")

    def test_fifo_state_file_does_not_hang_append(self):
        self.seed("ops", 2)
        state = self.path("state", "ops.json")
        os.remove(state)
        self.fifo(state)
        rc, out, err = run_cli(["note", "--bot", "ops", "--detail", "after the fifo"], self.root, timeout=60)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: state/ops.json was unreadable; adopted the log tail (seq 2)", err)
        self.assertTrue(stat.S_ISREG(os.lstat(state).st_mode), "state/ops.json is still not a regular file")
        last = self.chain()[-1]
        self.assertEqual((last["seq"], last["detail"], last.get("state_adopted")), (3, "after the fifo", "unreadable"))
        self.assertEqual(self.raw_state()["head"], last["hash"])


# --------------------------------------------------------------------------- D1-02
class SeqGapTest(Base):
    def test_large_seq_gap_is_described_by_count(self):
        _first, second = self.seed("ops", 2)
        self.raw_line("ops", 3 + 5_000_000, second["hash"])
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["breaks"]), 1, res["problems"])
        brk = res["breaks"][0]
        self.assertEqual((brk["seq_before"], brk["seq_after"]), (2, 5_000_003))
        self.assertEqual(brk["missing_seqs"], [])
        self.assertEqual(brk["missing_count"], 5_000_000)
        self.assertLess(len(json.dumps(res["breaks"])), 2000)
        # a small gap is still listed seq by seq
        _first, second = self.seed("small", 2)
        self.raw_line("small", 6, second["hash"])
        brk = self.ledger.verify("small")["breaks"][0]
        self.assertEqual(brk["missing_seqs"], [3, 4, 5])
        self.assertNotIn("missing_count", brk)

    def test_break_ack_with_huge_gap_is_malformed_without_allocation(self):
        brk = {"file": "logs/ack/%s.jsonl" % DAY, "line_before": 1, "line_after": 2, "seq_before": 1,
               "seq_after": 10 ** 15, "missing_seqs": [], "hash_before": "a" * 64, "orphan_prev": "b" * 64,
               "hash_after": "c" * 64}
        self.assertFalse(gblog._break_shape_ok(brk))
        self.ledger.append("ack", {"type": "break_ack", "jay_approval_id": "x", "break": brk, "ts": stamp(0)})
        res = self.ledger.verify("ack")
        self.assertFalse(res["ok"])
        self.assertIn("logs/ack/%s.jsonl:1: malformed break_ack" % DAY, res["problems"])


# --------------------------------------------------------------------------- D1-03, D1-04
class ChainIsolationTest(Base):
    def test_anchor_write_failure_in_one_chain_does_not_stop_the_others(self):
        self.seed("aaa", 1)
        self.seed("zzz", 1)
        os.makedirs(self.path("anchors", "aaa.history.jsonl"))  # a directory where the history file belongs
        rc, out, err = run(["anchor"], self.root, internal_ok=True)  # "anchor aborted" is the expected entry
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertRegex(out, r"(?m)^ANCHOR FAIL aaa\s")
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")
        self.assertTrue(os.path.isfile(self.path("anchors", "zzz.json")))
        days = self.path("anchors", "days")
        names = [n for n in os.listdir(days) if n.endswith(".json")]
        self.assertEqual(len(names), 1, names)
        with open(os.path.join(days, names[0]), encoding="utf-8") as fh:
            by = {c["bot"]: c for c in json.load(fh)["chains"]}
        self.assertEqual(sorted(by), ["aaa", "zzz"])
        self.assertIs(by["zzz"]["ok"], True)
        self.assertIs(by["aaa"]["ok"], False)
        self.assertTrue(by["aaa"]["problems"][0].startswith("anchor aborted: "), by["aaa"]["problems"])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_unreadable_chain_directory_fails_verify(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can list any directory")
        self.seed("ops", 2)
        os.remove(self.path("state", "ops.json"))
        bot_dir = self.path("logs", "ops")
        os.chmod(bot_dir, 0)
        self.addCleanup(os.chmod, bot_dir, 0o700)
        try:
            os.listdir(bot_dir)
        except OSError:
            pass
        else:
            self.skipTest("a directory with mode 0 can still be listed here")
        text = "logs/ops: cannot be listed (PermissionError)"
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertIn(text, res["problems"])
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^FAIL\s+ops\s")
        self.assertIn("      - " + text, out.splitlines())
        self.assertNotIn("head genesis", out)

    def test_scandir_error_mid_listing_is_a_problem(self):
        self.seed("ops", 2)
        bot_dir = self.path("logs", "ops")
        real = os.scandir

        class Broken:
            def __iter__(self):
                return self

            def __next__(self):
                raise OSError(errno.EIO, "Input/output error")

            def close(self):
                pass

        def fake(path="."):
            return Broken() if os.path.abspath(str(path)) == bot_dir else real(path)

        with mock.patch.object(gblog.os, "scandir", fake):
            res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertIn("logs/ops: listing stopped early (OSError)", res["problems"])
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- D1-05
class RollupWitnessTest(Base):
    def _rollup_hits(self, res, head):
        return [p for p in res["problems"]
                if p.startswith("anchors/days/") and head[:12] in p and "is no longer in the chain" in p]

    def test_truncate_after_anchor_detected_from_day_rollup_only(self):
        recs = self.seed("ops", 3)
        self.ledger.anchor()
        self.truncate(2)
        self.drop_witnesses()
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertEqual(len(self._rollup_hits(res, recs[2]["hash"])), 1, res["problems"])
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^FAIL\s+ops\s")

    def test_emptied_chain_dir_with_only_day_rollup_fails(self):
        recs = self.seed("ops", 3)
        self.seed("keeper", 1)
        self.ledger.anchor()
        for path in self.ledger.log_files("ops"):
            os.remove(path)
        self.drop_witnesses()
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["records"], 0)
        self.assertEqual(len(self._rollup_hits(res, recs[2]["hash"])), 1, res["problems"])
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^FAIL\s+ops\s")
        self.assertRegex(out, r"(?m)^OK\s+keeper\s")

    def test_rollup_entry_without_heads_does_not_shadow_older_rollups(self):
        recs = self.seed("ops", 3)
        head = recs[2]["hash"]
        older = dict(ROLLUP, chains=[{"bot": "ops", "ok": True, "records": 3, "head": head, "ts": ROLLUP["ts"]}])
        newer = dict(ROLLUP, chains=[{"bot": "ops", "ok": False, "records": 0, "head": None, "segment_heads": [],
                                      "ts": "2024-06-02T12:00:00Z"}])
        self.write(self.path("anchors", "days", "2024-06-01.json"), json.dumps(older))
        self.write(self.path("anchors", "days", "2024-06-02.json"), json.dumps(newer))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])  # the rollup head is still in the chain
        self.truncate(2)
        os.remove(self.path("state", "ops.json"))
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"], res)
        self.assertTrue(any(p.startswith("anchors/days/2024-06-01.json: anchored head %s" % head[:12])
                            for p in res["problems"]), res["problems"])

    def test_rollup_written_after_snapshot_does_not_fail_verify(self):
        recs = self.seed("ops", 3)
        self.ledger.anchor()
        os.remove(self.path("anchors", "ops.json"))
        os.remove(self.path("anchors", "ops.history.jsonl"))
        fired = []
        real_lock = gblog.Ledger.lock

        def later():
            if fired:
                return
            fired.append(True)
            newer = self.ledger.append("ops", {"type": "note", "detail": "after the snapshot", "ts": stamp(9)})
            self.ledger.anchor(["ops"])  # rewrites today's rollup with the new head
            fired.append(newer["hash"])

        def hooked(ledger, bot, *args, **kwargs):
            inner = real_lock(ledger, bot, *args, **kwargs)
            shared = kwargs.get("shared", args[0] if args else False)
            return _AfterRelease(inner, later) if shared and not fired else inner

        with mock.patch.object(gblog.Ledger, "lock", hooked):
            res = self.ledger.verify("ops")
        self.assertEqual(len(fired), 2, "the hook did not run")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["head"]), (3, recs[-1]["hash"]))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["head"]), (4, fired[1]))


# --------------------------------------------------------------------------- D1-06
class StateRepairTest(Base):
    def test_state_seq_mismatch_warns_and_rebuild_repairs(self):
        recs = self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": recs[-1]["hash"]})
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], ["state/ops.json: seq 99 does not match the last record (seq 2); appends "
                                           "are refused until `gblog state rebuild` is run"])
        rc, out, err = run(["note", "--bot", "ops", "--detail", "refused"], self.root)
        self.assertEqual(rc, 4, out + err)
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rebuilt ops seq 3", out)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "accepted"], self.root)
        self.assertEqual(rc, 0, out + err)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["warnings"]), (4, []))

    def test_non_string_state_head_is_adopted_and_can_be_rebuilt(self):
        self.seed("ops", 2)
        sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234", "pushed": False}
        self.write(self.path("state", "ops.json"), json.dumps({"seq": 2, "head": 7, "last_sync": sync}))
        rc, out, err = run(["note", "--bot", "ops", "--detail", "n"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: state/ops.json was unreadable; adopted the log tail (seq 2)", err)
        self.assertEqual(self.raw_state()["last_sync"], sync)
        self.write(self.path("state", "ops.json"), json.dumps({"seq": 3, "head": ["x"], "last_sync": sync}))
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        last = self.chain()[-1]
        self.assertEqual(last["detail"], "state rebuilt from log tail (was seq - head unreadable)")
        state = self.raw_state()
        self.assertEqual((state["seq"], state["head"], state["last_sync"]), (4, last["hash"], sync))
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- D3-01, D3-02 (classification)
def _no_lookup():
    """Patches under which any gh call or API request fails the test."""
    return (mock.patch.object(gblog.subprocess, "run", side_effect=AssertionError("gh was called")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("the API was asked")))


class RemoteFormTest(unittest.TestCase):
    ODD_GITHUB = (
        "[github.com:22]:o/r.git",
        "git@[github.com]:o/r.git",
        "[git@github.com:22]:o/r.git",
        "https::https://github.com/o/r.git",
        "ssh://git@github%2Ecom/o/r.git",
        "https://\uff47\uff49\uff54\uff48\uff55\uff42.com/o/r",  # fullwidth letters
        "https://github\u3002com/o/r",                            # ideographic full stop
        "ssh://git@[github.com]/o/r.git",
    )

    def test_bracketed_and_helper_urls_are_github(self):
        gh, api = _no_lookup()
        with gh, api:
            for url in self.ODD_GITHUB:
                with self.subTest(url=url):
                    self.assertEqual(gblog.github_remote(url), (True, None, None))
                    answer = gblog.remote_visibility(url)
                    self.assertIsNone(answer["public"])
                    self.assertNotIn("not a github.com remote", answer["reason"])

    def test_decoded_authority_is_not_reported_private(self):
        url = "ssh://git@evil.example%2F@github.com/o/r.git"  # git decodes first: the host is evil.example
        self.assertEqual(gblog.github_remote(url), (False, None, None))
        gh, api = _no_lookup()
        with gh, api:
            self.assertIsNone(gblog.remote_visibility(url)["public"])

    def test_unparsable_url_fails_closed(self):
        self.assertEqual(gblog.github_remote("https://[github.com/o/r"), (True, None, None))

    def test_plain_forms_still_map(self):
        for url in ("git@github.com:deximple/grokledger.git", "https://github.com/deximple/grokledger",
                    "ssh://git@github.com/deximple/grokledger.git"):
            self.assertEqual(gblog.github_remote(url), (True, "deximple", "grokledger"), url)
        for url in ("https://evilgithub.com/o/r", "https://example.com/github.com/o/r", "[evil.example:22]:o/r.git"):
            self.assertEqual(gblog.github_remote(url), (False, None, None), url)

    def test_wiki_is_checked_as_its_repository(self):
        for url in ("https://github.com/octocat/Hello-World.wiki.git", "git@github.com:octocat/Hello-World.wiki.git"):
            self.assertEqual(gblog.github_remote(url), (True, "octocat", "Hello-World"), url)
        seen = []

        def urlopen(req, *args, **kwargs):
            seen.append(req.full_url)
            return _Body(b'{"private": false}')

        with mock.patch.object(gblog.subprocess, "run", side_effect=OSError("gh is not installed")), \
                mock.patch("urllib.request.urlopen", urlopen):
            answer = gblog.remote_visibility("git@github.com:octocat/Hello-World.wiki.git")
        self.assertEqual(seen, ["https://api.github.com/repos/octocat/Hello-World"])
        self.assertIs(answer["public"], True)


# --------------------------------------------------------------------------- D3-01 .. D3-08 (sync)
@unittest.skipUnless(GIT, "git not installed")
class SyncDeepTest(Base):
    DAY_FILE = "logs/ops/2026-01-01.jsonl"

    def setUp(self):
        super().setUp()
        self.real_visibility = gblog.remote_visibility
        self.vis_calls = []
        self.vis_answer = lambda url: {"public": False, "reason": "test"}

        def fake(remote, *args, **kwargs):
            self.vis_calls.append(remote)
            return dict(self.vis_answer(remote))

        patcher = mock.patch.object(gblog, "remote_visibility", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger.append("ops", {"type": "note", "detail": "something to back up", "ts": "2026-01-01T10:00:00Z"})

    def bare(self, name):
        repo = os.path.join(self.base, name)
        git("init", "--bare", "-q", repo)
        return repo

    def tracked(self, root=None):
        return git("-C", root or self.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()

    def global_config(self, name, text):
        """A git config file to use as GIT_CONFIG_GLOBAL for one command."""
        return {"GIT_CONFIG_GLOBAL": self.write(os.path.join(self.base, name), text)}

    def test_sync_refuses_odd_github_forms(self):
        # D3-01
        self.vis_answer = lambda url: {"public": None, "reason": "github.com URL that cannot be mapped to a repository"}
        rc, out, err = run(["sync", "--remote", "[github.com:22]:o/r.git"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("refused", out + err)
        self.assertEqual(self.chain("system")[-1]["outcome"], "denied")

    def test_sync_refuses_the_wiki_of_a_public_repository(self):
        # D3-02: the real lookup runs; gh is "not installed" and the API says the parent repository is public
        seen = []
        real_run = subprocess.run

        def no_gh(argv, *args, **kwargs):
            if argv and argv[0] in ("gh", "ssh"):
                raise OSError("%s is not installed" % argv[0])
            return real_run(argv, *args, **kwargs)

        def urlopen(req, *args, **kwargs):
            seen.append(req.full_url)
            return _Body(b'{"private": false}')

        with mock.patch.object(gblog, "remote_visibility", self.real_visibility), \
                mock.patch.object(gblog.subprocess, "run", no_gh), mock.patch("urllib.request.urlopen", urlopen):
            rc, out, err = run(["sync", "--remote", "https://github.com/octocat/Hello-World.wiki.git"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("PUBLIC", out + err)
        self.assertEqual(seen, ["https://api.github.com/repos/octocat/Hello-World"])

    # fixed: D3-03, including the remotes-file case (a legacy .git/remotes/origin file is never pushed to)
    def test_no_repo_origin_is_never_pushed(self):
        # D3-03: a remote named origin that git would find outside the ledger repository's own config
        for case in ("directory", "remotes-file", "global-config"):
            with self.subTest(case=case):
                root = os.path.join(self.base, "ledger-" + case)
                gblog.Ledger(root).append("ops", {"type": "note", "detail": "x", "ts": "2026-01-01T10:00:00Z"})
                target, env = self.bare("target-%s.git" % case), {}
                if case == "directory":
                    target = os.path.join(root, "origin")
                    git("init", "--bare", "-q", target)
                elif case == "remotes-file":
                    rc, out, err = run(["sync", "--no-push"], root)
                    self.assertEqual(rc, 0, out + err)
                    self.write(os.path.join(root, ".git", "remotes", "origin"),
                               "URL: %s\nPush: HEAD:refs/heads/main\n" % target)
                else:
                    env = self.global_config("origin.gitconfig", '[remote "origin"]\n\turl = %s\n' % target)
                del self.vis_calls[:]
                with mock.patch.dict(os.environ, env):
                    rc, out, err = run(["sync"], root)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("(not pushed)", out)
                self.assertIn("no origin configured in the ledger repository", out)
                self.assertEqual(commits(target), 0)
                self.assertEqual(self.vis_calls, [])

    def test_sync_with_ignored_top_level_directory(self):
        # D3-04
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        os.makedirs(self.path(".git", "info"), exist_ok=True)
        with open(self.path(".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write("logs/\n")
        self.ledger.append("web", {"type": "note", "detail": "hidden by an ignore rule", "ts": "2026-01-02T10:00:00Z"})
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("git failed", err)
        self.assertIn("logs/web/2026-01-02.jsonl", self.tracked())
        # the same for a directory named in a global excludes file
        excludes = self.write(os.path.join(self.base, "excludes"), "archive/\n")
        env = self.global_config("excludes.gitconfig", "[core]\n\texcludesFile = %s\n" % excludes)
        arch, _err = self.quiet(gblog.archive_file, self.ledger, "ops", b"evidence\n", "e.txt", "artifact",
                                ts="2026-01-03T10:00:00Z")
        with mock.patch.dict(os.environ, env):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(arch["path"], self.tracked())

    def test_same_size_rewrite_with_restored_mtime_is_drift(self):
        # D3-05: an index flag hides the change from git, and the old mtime is put back
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        git("-C", self.root, "update-index", "--assume-unchanged", self.DAY_FILE)
        full = self.path(*self.DAY_FILE.split("/"))
        st = os.stat(full)
        with open(full, "rb") as fh:
            data = fh.read()
        self.assertIn(b"something to back up", data)
        with open(full, "wb") as fh:
            fh.write(data.replace(b"something to back up", b"SOMETHING TO BACK UP"))
        os.utime(full, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual((os.path.getsize(full), os.stat(full).st_mtime_ns), (len(data), st.st_mtime_ns))
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("committed content differs from disk for %s" % self.DAY_FILE, out)

    @unittest.skipUnless(os.name == "posix", "executable hook scripts are POSIX-specific")
    def test_remote_hook_output_with_invalid_bytes(self):
        # D3-06
        bare = self.bare("o.git")
        hook = self.write(os.path.join(bare, "hooks", "post-receive"), '#!/bin/sh\nprintf "\\377 caf\\351\\n"\n')
        os.chmod(hook, 0o755)
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("internal error", err)
        self.assertIn(" pushed", out)
        self.assertNotIn("(not pushed)", out)
        self.assertEqual(commits(bare), 1)
        self.assertIs(self.raw_state("ops")["last_sync"]["pushed"], True)

    def test_commit_signing_config_does_not_break_sync(self):
        # D3-07
        env = self.global_config("sign.gitconfig", "[commit]\n\tgpgsign = true\n[push]\n\tgpgsign = false\n"
                                                   "[gpg]\n\tprogram = false\n")
        with mock.patch.dict(os.environ, env):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("git failed", err)
        self.assertEqual(commits(self.root), 1)

    def test_symlinked_archive_dir_is_named_in_the_error(self):
        # D3-08
        big = os.path.join(self.base, "bigdisk")
        os.rename(self.path("archive"), big)
        try:
            os.symlink(big, self.path("archive"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported here")
        self.quiet(gblog.archive_file, self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts="2026-01-01T10:00:01Z")
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 1)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("archive is a symbolic link", out)
        self.assertIn("not pushed", out)
        self.assertNotIn("ignore rule", out)


# --------------------------------------------------------------------------- D4-01, D4-04, D4-05
class FileCaptureDeepTest(Base):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.base, "work")
        self.put("keep.txt", "1")

    def put(self, name, text, mtime=None, top=None):
        path = self.write(os.path.join(top or self.work, name), text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def capture(self, top=None, **kwargs):
        res, _err = self.quiet(gblog.capture_files, self.ledger, "ops", top or self.work, **kwargs)
        return res

    def file_records(self):
        return [r for r in self.chain() if r.get("capture") == "files" and r.get("type") == "action"]

    def test_file_records_carry_mtime_as_observed(self):
        # D4-01
        self.assertEqual(self.capture()["new_baseline"], "first run")
        made = self.put("made.txt", "x", mtime=T0)
        self.put("future.txt", "y", mtime=time.time() + 2 * 86400)
        self.assertEqual(self.capture()["changes"], 2)
        by = {r["target"]: r for r in self.file_records()}
        self.assertEqual((by["made.txt"]["detail"], by["made.txt"]["observed"]), ("created", "2024-06-01T10:00:00Z"))
        self.assertEqual(by["future.txt"]["detail"], "created")
        self.assertNotIn("observed", by["future.txt"])  # an mtime in the future is not believed
        self.put("made.txt", "xx", mtime=T0 + 60)
        self.assertEqual(self.capture()["changes"], 1)
        last = self.file_records()[-1]
        self.assertEqual((last["target"], last["detail"], last["observed"]), ("made.txt", "modified", "2024-06-01T10:01:00Z"))
        os.remove(made)
        self.assertEqual(self.capture()["changes"], 1)
        last = self.file_records()[-1]
        self.assertEqual((last["target"], last["detail"]), ("made.txt", "deleted"))
        self.assertNotIn("observed", last)
        self.assertTrue(gblog._is_norm_ts(last.get("observed_from")), last)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_file_coverage_matches_report_made_at_mtime(self):
        # D4-01: the Bot reported the file when it wrote it; the capture run comes much later
        self.ledger.append("ops", {"type": "action", "action": "file", "target": "report.csv",
                                   "ts": "2024-06-01T10:00:00Z"})
        self.capture()
        self.put("report.csv", "x", mtime=T0)
        self.assertEqual(self.capture()["changes"], 1)
        cov = gblog.coverage(self.ledger.records("ops"))
        self.assertEqual(cov["rows"], [{"bot": "ops", "action": "file", "auto": 1, "matched": 1, "pct": 100.0}])

    def test_other_path_does_not_replace_the_snapshot(self):
        # D4-04
        other = os.path.join(self.base, "other")
        self.put("x.txt", "x", top=other)
        self.capture()
        self.put("payload.sh", "#!/bin/sh\n")
        res = self.capture(top=other)
        self.assertEqual((res["changes"], res["new_baseline"]), (0, "path changed"))
        res = self.capture()
        self.assertEqual((res["changes"], res["new_baseline"]), (1, None))
        self.assertEqual([(r["target"], r["detail"]) for r in self.file_records()], [("payload.sh", "created")])
        snaps = sorted(n for n in os.listdir(self.path("state")) if n.startswith("capture-files-ops"))
        self.assertEqual(len(snaps), 2, snaps)
        self.assertIn("capture-files-ops.json", snaps)
        # the other directory keeps a baseline of its own as well
        self.put("y.txt", "y", top=other)
        res = self.capture(top=other)
        self.assertEqual((res["changes"], res["new_baseline"]), (1, None))

    def test_pending_changes_are_logged_before_new_ones(self):
        # D4-05: three early paths change before every run; the later path must not starve behind --limit
        for i in range(3):
            self.put("a%d.log" % i, "0")
        self.capture()
        self.put("zz-payload.sh", "#!/bin/sh\n")
        pending = []
        for run_no in (1, 2):
            for i in range(3):
                self.put("a%d.log" % i, "x" * (run_no + 1))
            pending.append(self.capture(limit=3)["pending"])
        self.assertEqual(pending, [1, 1])
        targets = [r["target"] for r in self.file_records()]
        self.assertEqual(targets[:3], ["a0.log", "a1.log", "a2.log"])
        self.assertEqual(targets[3:], ["zz-payload.sh", "a0.log", "a1.log"])


# --------------------------------------------------------------------------- D4-03
class BrowserWindowTest(Base):
    def setUp(self):
        super().setUp()
        self.hist = chrome_history(os.path.join(self.base, "chrome"), [("https://a.example/one", "A", 60)])

    def test_negative_hours_is_refused_with_the_exact_message(self):
        # D4-03 (the larger negative-window test is test_deep_reviews.CliLayerTest, K6-14)
        argv = ["capture", "browser", "--bot", "ops", "--history", self.hist]
        rc, out, err = run(argv + ["--hours=-1"], self.root)
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(err, "error: --hours must be 0 or more\n")
        self.assertFalse(os.path.exists(self.path("state", "ops.json")))
        rc, out, err = run(argv, self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("browser: 1 new visit(s)", out)

    # bug: Z-12 (round-4 G2-01). README, FAQ 28 and the CHANGELOG say that only a negative --hours is
    # refused and that `--hours 0` imports nothing on a first run; _validate_args still exits 2 for 0.
    @unittest.expectedFailure
    def test_hours_zero_imports_nothing_on_a_first_run(self):
        argv = ["capture", "browser", "--bot", "ops", "--history", self.hist]
        rc, out, err = run(argv + ["--hours=0"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("browser: 0 new visit(s) from 1 history file(s)", out)
        # the first run left a marker: the default window does not reach back behind it
        rc, out, err = run(argv, self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("browser: 0 new visit(s) from 1 history file(s)", out)
        self.assertEqual(self.chain(), [])

    def test_future_marker_is_ignored_with_a_notice(self):
        future = int((time.time() + 10 * 365 * 86400 + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
        self.quiet(self.ledger.update_state, "ops", {"capture_browser_last": {self.hist: future}})
        res, err = self.quiet(gblog.capture_browser, self.ledger, "ops", [self.hist], 24)
        self.assertEqual(res["added"], 1, res)
        self.assertIn("notice: ignoring browser-history marker dated in the future", err)
        self.assertLess(self.raw_state()["capture_browser_last"][self.hist], future)


# --------------------------------------------------------------------------- D4-02
@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class ShellHookPrimingTest(Base):
    OLD = "rm -rf /srv/old-release"
    NEW = "ls -la /tmp"

    def hook(self, flags, script):
        """Run ``script`` in bash with the hook file in $HOOK; returns (process, what the fake gblog logged)."""
        bindir = os.path.join(self.base, "bin")
        fake = self.write(os.path.join(bindir, "gblog"), "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$GBLOG_FAKE_LOG\"\n")
        os.chmod(fake, 0o755)
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        log = os.path.join(self.base, "fake.log")
        if os.path.exists(log):
            os.remove(log)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), GBLOG_FAKE_LOG=log, HOOK=hook,
                   HISTFILE=os.devnull, OLD=self.OLD, NEW=self.NEW)
        for key in ("HISTCONTROL", "HISTIGNORE", "PROMPT_COMMAND", "GBLOG_BOT", "GBLOG_TASK"):
            env.pop(key, None)
        # a new session: an interactive bash must not reach for the terminal of whoever runs the tests
        proc = subprocess.run([BASH, "--noprofile", "--norc"] + flags + ["-c", script], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                              start_new_session=True)
        text = ""
        if os.path.exists(log):
            with open(log, encoding="utf-8") as fh:
                text = fh.read()
        return proc, text

    def test_hook_primes_on_first_interactive_prompt(self):
        first = 'source "$HOOK"; history -s "$OLD"; __gblog_hook'
        # not interactive (the harness the other hook tests use): the command is logged as before
        _proc, text = self.hook([], first)
        self.assertIn("--target=" + self.OLD, text)
        proc, _text = self.hook(["-i"], 'case "$-" in *i*) echo yes;; esac')
        if b"yes" not in proc.stdout:
            self.skipTest("bash -i -c does not run as an interactive shell here")
        # interactive: what history shows at the first prompt was typed in an earlier session
        _proc, text = self.hook(["-i"], first)
        self.assertEqual(text, "")
        _proc, text = self.hook(["-i"], first + '; history -s "$NEW"; __gblog_hook')
        self.assertIn("--target=" + self.NEW, text)
        self.assertNotIn(self.OLD, text)
        self.assertEqual(len(text.splitlines()), 1, text)


# --------------------------------------------------------------------------- D4-06, D4-07, D4-08
class TuneRulesDeepTest(unittest.TestCase):
    """analyze() on hand-built record dicts: no ledger, no fsync."""

    def analyze(self, recs):
        return gblog.analyze(recs, 28, gblog.coverage(recs))

    def test_hook_duplicate_of_a_reported_failure_counts_once(self):
        # D4-06
        recs = [rec(1, type="action", action="shell", target="make build", outcome="fail", detail="exit 2", ts=ago(2)),
                rec(2, type="action", action="shell", target="make build", outcome="fail", source="auto", ts=ago(2, 5))]
        a = self.analyze(recs)
        self.assertEqual([p["key"] for p in a["proposals"] if p["category"] == "skill"], [])
        self.assertEqual((a["metrics"]["ops"]["actions"], a["metrics"]["ops"]["failed_actions"]), (1, 1))
        metric = {"kind": "fail", "bot": "ops", "action": "shell", "key": "make"}
        self.assertEqual(gblog.compute_metric(recs, metric), 1.0)
        # control: two failures the Bot reported itself are a repeated failure
        recs.append(rec(3, type="action", action="shell", target="make build", outcome="fail", ts=ago(1)))
        a = self.analyze(recs)
        self.assertEqual([p["key"] for p in a["proposals"] if p["category"] == "skill"], ["fail:shell:make"])
        self.assertEqual(gblog.compute_metric(recs, metric), 2.0)

    def test_auto_failures_do_not_make_skill_proposals(self):
        # D4-06: commands that exit non-zero by design, seen by the hook only
        recs = [rec(i + 1, type="action", action="shell", target="grep -q needle haystack.txt", outcome="fail",
                    source="auto", ts=ago(3, i * 7200)) for i in range(3)]
        a = self.analyze(recs)
        self.assertEqual([p for p in a["proposals"] if p["category"] == "skill"], [])
        self.assertEqual(a["metrics"]["ops"]["failed_actions"], 0)

    def test_auto_decisions_do_not_propose_an_allow_rule(self):
        # D4-07
        recs = [rec(i + 1, type="approval", target="push the ledger", decision="auto", outcome="ok", ts=ago(5 - i))
                for i in range(4)]
        self.assertEqual([p for p in self.analyze(recs)["proposals"] if p["category"] == "approval"], [])

    def test_allow_rule_effect_improves_when_decisions_become_auto(self):
        # D4-07: the Allow rule works, so every later request is decided automatically
        target = "push the ledger"
        key = gblog._norm_title(target)
        pid = gblog._proposal_id("ops", "approval", "allow:" + key)
        applied = ago(11)
        recs = [rec(i + 1, type="approval", target=target, decision="allowed", outcome="ok", ts=ago(d))
                for i, d in enumerate((14, 13, 12))]
        recs.append(rec(4, type="tune", action="other", target=pid, outcome="ok", detail="added an Allow rule", ts=applied,
                        proposal={"title": "allow rule", "category": "approval", "key": "allow:" + key, "baseline": 3.0,
                                  "metric": {"kind": "approvals", "bot": "ops", "key": key}, "better": "lower",
                                  "window": {"since": ago(39), "until": applied}, "count": 3}))
        recs += [rec(5 + i, type="approval", target=target, decision="auto", outcome="ok", ts=ago(d))
                 for i, d in enumerate(range(10, 0, -1))]
        a = self.analyze(recs)
        self.assertEqual([e["id"] for e in a["effects"]], [pid])
        self.assertEqual(a["effects"][0]["after"], 0.0)
        self.assertIs(a["effects"][0]["improved"], True)
        self.assertEqual([p["title"] for p in a["proposals"] if p["category"] == "revert"], [])
        self.assertNotIn("not helping", gblog.render_tune(a, "T"))

    # -- D4-08
    KEY = "fail:connector:crm.sync"

    def _fail(self, seq, ts):
        return rec(seq, type="action", action="connector", target="crm.sync", outcome="fail", detail="500", ts=ts)

    def _tune(self, seq, ts, outcome="ok"):
        pid = gblog._proposal_id("ops", "skill", self.KEY)
        return rec(seq, type="tune", action="other", target=pid, outcome=outcome, detail="added a retry", ts=ts,
                   proposal={"title": "crm.sync", "category": "skill", "key": self.KEY, "baseline": 3.0,
                             "metric": {"kind": "fail", "bot": "ops", "action": "connector", "key": "crm.sync"},
                             "better": "lower", "window": {"since": ago(43), "until": ago(15)}, "count": 3})

    def _base(self):
        return [self._fail(i + 1, ago(d)) for i, d in enumerate((20, 19, 18))] + [self._tune(4, ago(15))]

    def test_applied_revert_reopens_the_original(self):
        pid = gblog._proposal_id("ops", "skill", self.KEY)
        recs = self._base() + [self._fail(5 + i, ago(d)) for i, d in enumerate(range(14, 0, -1))]
        a = self.analyze(recs)
        self.assertEqual([(e["id"], e["improved"]) for e in a["effects"]], [(pid, False)])
        reverts = [p for p in a["proposals"] if p["category"] == "revert"]
        self.assertEqual([p["key"] for p in reverts], ["revert:" + pid])
        revert = reverts[0]
        recs.append(rec(len(recs) + 1, type="tune", action="other", target=revert["id"], outcome="ok", detail="reverted",
                        ts=gblog.now_iso(), proposal={"title": revert["title"], "category": "revert", "key": revert["key"]}))
        a = self.analyze(recs)
        status = {p["id"]: (p["category"], p["status"]) for p in a["proposals"]}
        self.assertEqual(status.get(pid), ("skill", "new"))               # the problem is open again
        self.assertEqual(status.get(revert["id"]), ("revert", "applied"))  # the revert itself stays visible
        self.assertEqual(a["effects"], [])                                # nothing is measured for an undone change

    def test_only_the_standing_decision_is_measured(self):
        pid = gblog._proposal_id("ops", "skill", self.KEY)
        later = ago(10)
        # applied, then rejected: no effect row, and the proposal is "rejected"
        a = self.analyze(self._base() + [self._tune(5, later, outcome="skipped")])
        self.assertEqual(a["effects"], [])
        self.assertEqual({p["id"]: p["status"] for p in a["proposals"]}.get(pid), "rejected")
        # applied twice: one effect row, for the later decision
        a = self.analyze(self._base() + [self._tune(5, later)])
        self.assertEqual([(e["id"], e["applied"]) for e in a["effects"]], [(pid, later)])


# --------------------------------------------------------------------------- D4-09, D4-10
class CoverageDeepTest(Base):
    D = "2026-01-01T"

    def test_other_bots_report_takes_a_shared_capture_out_of_the_count(self):
        # D4-09: one visit on a shared computer, imported once per Bot
        visit = {"type": "action", "action": "browser", "target": "https://github.com/x/y", "source": "auto",
                 "capture": "browser", "observed": self.D + "10:01:00Z"}
        recs = [
            {"type": "action", "action": "browser", "bot": "alice", "target": "https://github.com/x/y",
             "ts": self.D + "10:00:00Z"},                                   # alice did it and said so
            {"type": "action", "action": "shell", "bot": "alice", "target": "git status", "ts": self.D + "10:00:30Z"},
            dict(visit, bot="alice", ts="2026-01-05T09:00:00Z"),
            dict(visit, bot="bob", ts="2026-01-05T09:00:05Z"),              # the same visit, imported for bob
            # a shell-hook record names its Bot: alice's report does not excuse bob
            {"type": "action", "action": "shell", "bot": "bob", "target": "git push", "source": "auto",
             "ts": self.D + "10:01:00Z"},
        ]
        cov = gblog.coverage(recs)
        rows = {(r["bot"], r["action"]): r for r in cov["rows"]}
        self.assertEqual(rows[("alice", "browser")]["pct"], 100.0)
        self.assertNotIn(("bob", "browser"), rows)
        self.assertEqual(cov["elsewhere"], 1)
        self.assertEqual((rows[("bob", "shell")]["auto"], rows[("bob", "shell")]["matched"]), (1, 0))
        self.assertEqual([(r["bot"], r["action"]) for r in cov["unmatched"]], [("bob", "shell")])
        self.assertEqual((cov["auto"], cov["matched"]), (2, 1))
        self.assertIn("reported by another Bot", gblog.render_coverage(cov, "t"))

    def test_coverage_window_keeps_older_self_reports(self):
        # D4-10: the visit was reported ten days ago and imported only today
        ten = gblog._utcnow() - _dt.timedelta(days=10)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        self.ledger.append("ops", {"type": "action", "action": "browser", "target": "https://github.com/x/y",
                                   "ts": ten.strftime(fmt)})
        self.ledger.append("ops", {"type": "action", "action": "browser", "target": "https://github.com/x/y",
                                   "source": "auto", "capture": "browser",
                                   "observed": (ten - _dt.timedelta(seconds=60)).strftime(fmt)})
        for days in ("7", "30"):
            with self.subTest(days=days):
                rc, out, err = run(["coverage", "--bot", "ops", "--days", days, "--json"], self.root)
                self.assertEqual(rc, 0, out + err)
                data = json.loads(out)
                self.assertEqual((data["auto"], data["matched"], data["pct"]), (1, 1, 100.0))
        # the window still decides which observed actions are counted
        cov = gblog.coverage(self.ledger.records("ops"), auto_since=gblog.now_iso()[:4] + "9")
        self.assertEqual(cov["auto"], 0)


if __name__ == "__main__":
    unittest.main()
