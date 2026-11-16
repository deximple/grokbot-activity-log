"""Tests for the round-4 audit of 2026-11-16 (work items G1-01 .. G1-12 in gblog.py).

Each test names the item it covers. Everything runs in temporary directories. No network is used:
git remotes are local paths or bare repositories and the visibility lookup is replaced by a fake.
"""
import datetime as _dt
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

sys.path.insert(0, HERE)
from _support import DAY, GIT_ENV, run, stamp  # noqa: E402

GIT = shutil.which("git")
BASH = shutil.which("bash")

FAKE_GBLOG = r'''#!/bin/sh
printf '%s\n' "$*" >> "$GBLOG_FAKE_LOG"
bot=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--bot" ]; then bot="$a"; fi
  prev="$a"
done
if [ "$bot" = "ops" ]; then exit 0; fi
if [ -n "${GBLOG_FAKE_ERR:-}" ]; then printf '%s\n' "$GBLOG_FAKE_ERR" >&2; fi
exit 2
'''


def git(*args, check=True):
    return subprocess.run(["git"] + list(args), check=check, capture_output=True, text=True).stdout


def ago(days, seconds=0):
    when = gblog._utcnow() - _dt.timedelta(days=days) + _dt.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-r4-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        self.ledger.init()
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT"):
            os.environ.pop(key, None)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def quiet(self, fn, *args, **kwargs):
        with redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def seed(self, bot="ops", n=2):
        return [self.quiet(self.ledger.append, bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1),
                                                     "ts": stamp(i)}) for i in range(n)]

    def chain(self, bot="ops"):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


# --------------------------------------------------------------------------- G1-01, G1-05, G1-07, G1-10 c
@unittest.skipUnless(GIT, "git not installed")
class SyncGuardTest(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(gblog, "remote_visibility", return_value={"public": False, "reason": "test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _origin(self):
        return git("-C", self.root, "config", "--local", "--get", "remote.origin.url", check=False).strip()

    def test_foreign_repository_is_refused_and_left_alone(self):
        # G1-01 a
        repo = os.path.join(self.base, "project")
        self.write(os.path.join(repo, "src", "main.py"), "print('hi')\n")
        git("init", "-q", repo)
        git("-C", repo, "add", "src/main.py")
        git("-C", repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "init")
        listing, status = sorted(os.listdir(repo)), git("-C", repo, "status", "--porcelain")
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is a git repository that is not a gblog ledger (it tracks src/main.py)", err)
        self.assertEqual(sorted(os.listdir(repo)), listing)
        self.assertEqual(git("-C", repo, "status", "--porcelain"), status)
        self.assertEqual(git("-C", repo, "rev-list", "--all", "--count").strip(), "1")
        # the documented opt-in
        git("-C", repo, "config", "gblog.ledger", "true")
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 0, out + err)

    def test_ledger_repositories_gain_the_marker(self):
        # G1-01 a: a repository gblog creates, and an older ledger repository that tracks only ledger paths
        self.seed("ops", 1)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(git("-C", self.root, "config", "--local", "--get", "gblog.ledger").strip(), "true")
        old = os.path.join(self.base, "old-ledger")
        gblog.Ledger(old).init()
        self.quiet(gblog.Ledger(old).append, "ops", {"type": "note", "detail": "x", "ts": stamp(0)})
        git("init", "-q", old)
        git("-C", old, "add", "-f", "--", "state/ops.json", "README.md")
        git("-C", old, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "0.5 backup")
        rc, out, err = run(["sync", "--no-push"], old)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(git("-C", old, "config", "--local", "--get", "gblog.ledger").strip(), "true")

    def test_stamp_loop_is_isolated(self):
        # G1-05 a: a directory at state/zzz.json must not turn a finished sync into exit 2
        self.seed("ops", 1)
        os.makedirs(self.path("logs", "zzz"))
        os.mkdir(self.path("state", "zzz.json"))
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"\Acommit [0-9a-f]+ \(not pushed\)\n\Z")
        self.assertIn("warning: zzz: last_sync not written (", err)
        self.assertIn("last_sync", json.loads(self.read(self.path("state", "ops.json"))))
        self.assertIn("last_sync", json.loads(self.read(self.path("state", "system.json"))))

    def test_healthy_push_prints_exactly_one_line(self):
        # G1-05: the stdout line of a healthy run is unchanged
        self.seed("ops", 1)
        bare = os.path.join(self.base, "origin.git")
        git("init", "--bare", "-q", bare)
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"\Acommit [0-9a-f]+ pushed\n\Z")
        self.assertNotIn("last_sync not written", err)

    def test_origin_replacement_is_undone_when_it_cannot_be_recorded(self):
        # G1-05 b
        self.seed("ops", 1)
        first, second = os.path.join(self.base, "a.git"), os.path.join(self.base, "b.git")
        rc, out, err = run(["sync", "--no-push", "--remote", first], self.root)
        self.assertEqual(rc, 0, out + err)
        self.ledger.write_state("system", {"seq": 99, "head": "cd" * 32})
        commits = git("-C", self.root, "rev-list", "--all", "--count").strip()
        rc, out, err = run(["sync", "--no-push", "--remote", second, "--replace-remote"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertEqual(self._origin(), first)
        self.assertEqual(git("-C", self.root, "rev-list", "--all", "--count").strip(), commits)
        spooled = [f for f in os.listdir(self.path("spool", "system")) if f.endswith(".json")]
        self.assertEqual(len(spooled), 1, spooled)

    def test_remote_refuses_a_first_origin_beside_a_legacy_remotes_file(self):
        # G1-07
        self.seed("ops", 1)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.write(self.path(".git", "remotes", "origin"), "URL: /somewhere/else.git\nPush: HEAD:refs/heads/main\n")
        rc, out, err = run(["sync", "--no-push", "--remote", os.path.join(self.base, "new.git")], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(".git/remotes/origin", err)
        self.assertEqual(self._origin(), "")
        self.assertEqual([r for r in self.chain("system") if r.get("target") == "git remote"], [])

    def test_drift_message_names_a_non_regular_evidence_entry(self):
        # G1-10 c
        self.seed("ops", 1)
        os.mkdir(self.path("logs", "ops", "zz.jsonl"))
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("logs/ops/zz.jsonl is not a regular file; not pushed", out)
        self.assertNotIn("ignore rule", out)


# --------------------------------------------------------------------------- G1-01 b
class TuneDecisionTest(unittest.TestCase):
    KEY = "fail:connector:crm.sync"

    def _recs(self, **tune):
        recs = [{"v": 1, "seq": i + 1, "bot": "ops", "type": "action", "action": "connector", "target": "crm.sync",
                 "outcome": "fail", "detail": "500", "ts": ago(d)} for i, d in enumerate((6, 5, 4))]
        pid = gblog._proposal_id("ops", "skill", self.KEY)
        recs.append(dict({"v": 1, "seq": 4, "bot": "ops", "type": "tune", "action": "other", "target": pid,
                          "outcome": "skipped", "ts": ago(1)}, **tune))
        return pid, recs

    def _status(self, **tune):
        pid, recs = self._recs(**tune)
        return {p["id"]: p["status"] for p in gblog.analyze(recs, 28, None)["proposals"]}[pid]

    def test_log_type_tune_cannot_forge_a_decision(self):
        self.assertEqual(self._status(), "new")
        self.assertEqual(self._status(detail="rejected"), "new")
        self.assertEqual(self._status(proposal={}), "new")
        self.assertEqual(self._status(proposal={"title": "t", "category": "skill", "key": self.KEY}), "rejected")


# --------------------------------------------------------------------------- G1-04
class EntryKindTest(Base):
    def test_entry_kind(self):
        target = os.path.join(self.base, "x")
        self.assertEqual(gblog._entry_kind(target), "missing")
        self.write(target, "x")
        self.assertEqual(gblog._entry_kind(target), "regular")
        folder = os.path.join(self.base, "d")
        os.mkdir(folder)
        self.assertEqual(gblog._entry_kind(folder), "directory")
        self.assertTrue(gblog._real_dir(folder))
        self.assertFalse(gblog._real_dir(target))
        try:
            os.symlink(os.path.join(self.base, "nowhere"), os.path.join(self.base, "dangling"))
            os.symlink(folder, os.path.join(self.base, "dirlink"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not supported here")
        self.assertEqual(gblog._entry_kind(os.path.join(self.base, "dangling")), "other")
        self.assertEqual(gblog._entry_kind(os.path.join(self.base, "dirlink")), "directory")
        self.assertFalse(gblog._real_dir(os.path.join(self.base, "dirlink")))


# --------------------------------------------------------------------------- G1-06
class TaskStatusTest(Base):
    def test_end_in_a_variant_chain_closes_the_task(self):
        with redirect_stderr(io.StringIO()):
            self.ledger.add_alias("chief-of-staff", "Chief of Staff", ["cos", "cos-primary"])
            self.ledger.append("cos", {"type": "task_start", "task_id": "a1", "task": "T"})
            self.assertEqual(gblog._task_status(self.ledger, "chief-of-staff", "a1"), "open")
            self.ledger.append("cos-primary", {"type": "task_end", "task_id": "a1", "outcome": "ok"})
        self.assertEqual(gblog._task_status(self.ledger, "chief-of-staff", "a1"), "closed")
        self.assertEqual(gblog._task_status(self.ledger, "chief-of-staff", "zz"), "unknown")

    def test_task_end_twice_warns_already_closed(self):
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "T"], self.root)
        self.assertEqual(rc, 0, err)
        tid = tid.strip()
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", tid], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("warning", err)
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", tid], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: task '%s' is already closed; recorded anyway" % tid, err)
        self.assertRegex(out, r"\A[0-9a-f]{12} #3 task_end ok\n\Z")
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", "ghost\x1b[2J"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("has no task-start in the last 30 days of ops", err)
        self.assertNotIn("\x1b", err)

    def test_scan_is_bounded(self):
        with redirect_stderr(io.StringIO()):
            for i in range(20):
                self.ledger.append("ops", {"type": "note", "detail": "padding %d" % i})
        self.assertEqual(gblog._task_status(self.ledger, "ops", "nosuch"), "unknown")
        with mock.patch.object(gblog, "_TASK_SCAN_BYTES", 200):
            self.assertEqual(gblog._task_status(self.ledger, "ops", "nosuch"), "unsure")
            rc, out, err = run(["note", "--bot", "ops", "--task-id", "nosuch", "--detail", "x"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("warning: task", err)
            rc, out, err = run(["task-start", "--bot", "ops", "--task-id", "fresh", "--task", "T"], self.root)
            self.assertEqual((rc, out), (0, "fresh\n"))
            self.assertNotIn("already used", err)

    def test_broken_diagnostic_never_changes_a_write(self):
        with mock.patch.object(gblog, "_task_status", side_effect=RuntimeError("boom")):
            rc, out, err = run(["note", "--bot", "ops", "--task-id", "t1", "--detail", "x"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertRegex(out, r"\A[0-9a-f]{12} #1 note\n\Z")
            rc, out, err = run(["task-end", "--bot", "ops", "--task-id", "t1"], self.root)
            self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.chain()), 2)

    def test_archive_prints_its_result_before_the_warning(self):
        src = self.write(os.path.join(self.base, "e.txt"), "evidence\n")
        order = []
        real = gblog._warn_task

        def spy(*args, **kwargs):
            order.append(len(self.chain()))
            return real(*args, **kwargs)

        with mock.patch.object(gblog, "_warn_task", spy):
            rc, out, err = run(["archive", "--bot", "ops", "--file", src, "--task-id", "typo1"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"\A[0-9a-f]{12} #1 archive archive/ops/")
        self.assertEqual(order, [1])
        self.assertIn("warning: task 'typo1' has no task-start", err)


# --------------------------------------------------------------------------- G1-08, G1-10 e
class AnchorWitnessTest(Base):
    def test_headless_entry_does_not_replace_a_rollup_witness(self):
        recs = self.seed("ops", 2)
        head = recs[-1]["hash"]
        with mock.patch.object(gblog, "now_iso", return_value="2024-06-02T12:00:00Z"):
            self.quiet(self.ledger.anchor)
            for path in self.ledger.log_files("ops"):
                open(path, "w").close()
            os.remove(self.path("state", "ops.json"))
            rollup = self.quiet(self.ledger.anchor)
        self.assertIs(rollup["chains"][0]["ok"], False)  # this run still reports the FAIL
        with open(self.path("anchors", "days", "2024-06-02.json"), encoding="utf-8") as fh:
            entry = {c["bot"]: c for c in json.load(fh)["chains"]}["ops"]
        self.assertIs(entry["ok"], True)
        self.assertEqual(entry["head"], head)
        self.assertEqual([(e["ok"], e["head"]) for e in entry["later"]], [(False, None)])
        os.remove(self.path("anchors", "ops.json"))
        os.remove(self.path("anchors", "ops.history.jsonl"))
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertTrue(any(p.startswith("anchors/days/2024-06-02.json: anchored head %s" % head[:12])
                            for p in res["problems"]), res["problems"])

    def test_history_is_written_before_the_anchor_file(self):
        self.seed("ops", 2)
        self.quiet(self.ledger.anchor)
        before = self.read(self.path("anchors", "ops.json"))
        self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "three", "ts": stamp(5)})
        real = gblog.Ledger._append_line

        def failing(ledger, path, line):
            if path.endswith(".history.jsonl"):
                raise OSError(errno.ENOSPC, "No space left on device", path)
            return real(ledger, path, line)

        with mock.patch.object(gblog.Ledger, "_append_line", failing):
            rollup = self.quiet(self.ledger.anchor)
        self.assertEqual(rollup["chains"][0]["problems"], ["anchor aborted: OSError"])
        self.assertEqual(self.read(self.path("anchors", "ops.json")), before)

    def test_fail_anchor_is_capped_but_keeps_every_head(self):
        n = 260
        d = self.path("logs", "ops")
        os.makedirs(d)
        prev, last = gblog.GENESIS, None
        with open(os.path.join(d, DAY + ".jsonl"), "w", encoding="utf-8") as fh:
            for i in range(1, n + 1):
                rec = {"v": 1, "seq": i, "ts": stamp(i), "bot": "ops", "type": "note", "detail": "r%d" % i,
                       "prev": prev if i == 1 else "%064x" % i}
                rec["hash"] = gblog.compute_hash(rec)
                prev = last = rec["hash"]
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self.ledger.write_state("ops", {"seq": n, "head": last})
        res = self.ledger.verify("ops")
        self.assertEqual((len(res["problems"]), len(res["segments"])), (n - 1, n))
        self.quiet(self.ledger.anchor)
        with open(self.path("anchors", "ops.json"), encoding="utf-8") as fh:
            entry = json.load(fh)
        self.assertEqual((len(entry["problems"]), len(entry["segments"])), (200, 200))
        self.assertEqual(entry["truncated"], {"problems": n - 1 - 200, "segments": n - 200})
        self.assertEqual(len(entry["segment_heads"]), n)
        self.assertEqual(entry["segment_heads"][-1], last)
        after = self.ledger.verify("ops")
        self.assertEqual(len(after["problems"]), n - 1)  # every anchored head is still found in the chain
        self.assertFalse([p for p in after["problems"] if "no longer in the chain" in p])


# --------------------------------------------------------------------------- G1-09
@unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is not available")
class NonBlockingReaderTest(Base):
    def test_readers_refuse_a_fifo_on_the_open_descriptor(self):
        fifo = os.path.join(self.base, "pipe")
        try:
            os.mkfifo(fifo)
        except OSError:
            self.skipTest("FIFOs are not supported here")
        # the entry "became" a FIFO after an isfile() check
        with mock.patch.object(gblog.os.path, "isfile", return_value=True):
            self.assertFalse(gblog._is_sqlite_file(fifo))
            self.assertIsNone(gblog._git_blob_id(fifo, 4, 40))
            with self.assertRaises(OSError):
                gblog._gunzip_sha256(fifo)
            with self.assertRaises(OSError):
                gblog._install_shell_hook("ops", fifo)
            with self.assertRaises(OSError):
                list(gblog.Ledger._scan([fifo], b"x"))
            self.assertFalse(gblog._archive_intact(fifo, "ab" * 32))

    def test_scan_and_gunzip_still_read_regular_files(self):
        recs = self.seed("ops", 2)
        hits = list(gblog.Ledger._scan(self.ledger.log_files("ops"), b"record 2"))
        self.assertEqual([rec["hash"] for _path, rec in hits], [recs[1]["hash"]])
        rec = self.quiet(gblog.archive_file, self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(5))
        self.assertEqual(gblog._gunzip_sha256(self.path(*rec["path"].split("/"))), rec["sha256"])
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- G1-10 b, d, f
class SmallDefectTest(Base):
    HINT = "state/ops.json is one record behind (interrupted append); the next append repairs it"

    def test_one_behind_state_gets_a_hint(self):
        recs = self.seed("ops", 3)
        self.ledger.write_state("ops", {"seq": 2, "head": recs[1]["hash"]})
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])  # the verdict stays FAIL
        self.assertEqual(res["warnings"], [self.HINT])
        self.assertEqual(dict(res), dict(self.ledger._verify_reference("ops")))
        self.ledger.write_state("ops", {"seq": 1, "head": recs[0]["hash"]})  # two behind: no hint
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(res["warnings"], [])
        self.ledger.write_state("ops", {"seq": 7, "head": recs[1]["hash"]})  # the right link, the wrong seq
        self.assertEqual(self.ledger.verify("ops")["warnings"], [])

    def test_verify_bot_reports_an_unlistable_witness_directory(self):
        self.seed("aaa", 1)
        self.quiet(self.ledger.anchor)
        rc, out, err = run(["verify", "--bot", "aaa"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("witness files", out)
        real = os.listdir

        def listdir(path="."):
            if os.path.basename(str(path)) == "days":
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real(path)

        with mock.patch.object(gblog.os, "listdir", listdir):
            rc, out, err = run(["verify", "--bot", "aaa"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^OK\s+aaa\s")
        self.assertRegex(out, r"(?m)^FAIL\s+\(witness files\)\s+0 records")
        self.assertIn("      - anchors/days cannot be listed (PermissionError): deleted chains cannot be reported",
                      out.splitlines())

    def test_replay_does_not_spool_an_event_twice(self):
        self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("ops", {"type": "note", "detail": "refused"})
        pending = self.ledger.spool_files("ops")
        self.assertEqual(len(pending), 1)
        # the tail "changes" between the check and the append: the check passes, the append is refused
        with mock.patch.object(gblog.Ledger, "_tail_ok", return_value=True):
            res = self.quiet(self.ledger.spool_replay, "ops")
        self.assertIs(res["refused"], True)
        self.assertEqual(res["replayed"], [])
        self.assertEqual(self.ledger.spool_files("ops"), pending)
        self.assertEqual(len(self.chain()), 2)


# --------------------------------------------------------------------------- G1-11
@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class HookFallbackClassTest(Base):
    def _calls(self, **extra):
        bindir = os.path.join(self.base, "bin")
        fake = self.write(os.path.join(bindir, "gblog"), FAKE_GBLOG)
        os.chmod(fake, 0o755)
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        log = os.path.join(self.base, "fake.log")
        if os.path.exists(log):
            os.remove(log)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), GBLOG_FAKE_LOG=log,
                   HOOK=hook, CMD="ls -la /tmp", HISTFILE=os.devnull, GBLOG_BOT="Ops2")
        for key in ("HISTCONTROL", "HISTIGNORE", "PROMPT_COMMAND", "GBLOG_TASK", "GBLOG_FAKE_ERR"):
            env.pop(key, None)
        env.update(extra)
        proc = subprocess.run([BASH, "--noprofile", "--norc", "-c", 'source "$HOOK"; history -s "$CMD"; __gblog_hook'],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")  # the captured error text is never printed at the prompt
        return self.read(log).splitlines() if os.path.exists(log) else []

    def test_environment_error_does_not_log_the_command_twice(self):
        calls = self._calls(GBLOG_FAKE_ERR="error: No space left on device: /x/state/Ops2.json")
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--bot Ops2", calls[0])

    def test_refused_name_still_falls_back(self):
        for text in ("error: bot name 'Ops2' is not registered in state/aliases.json (logging under 'Ops2')",
                     "error: bot name 'Ops2' is reserved"):
            calls = self._calls(GBLOG_FAKE_ERR=text)
            self.assertEqual(len(calls), 2, calls)
            self.assertIn("--bot ops", calls[1])
            self.assertIn("--detail GBLOG_BOT=Ops2 was refused", calls[1])

    def test_hook_is_valid_bash(self):
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        proc = subprocess.run([BASH, "-n", hook], capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# --------------------------------------------------------------------------- G1-12
class SharedHelperTest(Base):
    def test_names_of_and_attributed(self):
        with redirect_stderr(io.StringIO()):
            self.ledger.add_alias("chief-of-staff", "Chief of Staff", ["cos", "ChiefOfStaff"])
        self.assertEqual(self.ledger.names_of("cos"), ["cos", "chief-of-staff", "ChiefOfStaff"])
        self.assertEqual(self.ledger.names_of(" stranger "), ["stranger"])
        rec = {"bot": "ops", "seq": 1}
        self.assertIs(gblog.Ledger._attributed(rec, "ops"), rec)
        self.assertEqual(gblog.Ledger._attributed(rec, "web"), {"bot": "web", "bot_field": "ops", "seq": 1})
        self.assertEqual(self.ledger.witness_dir_problems(), [])


if __name__ == "__main__":
    unittest.main()
