"""Tests for the specialist reviews K9 (regressions of the round-4 work) and K10 (crash safety).

Each test names the finding it covers. Everything runs in temporary directories. No network is used:
git remotes are local paths or bare repositories and the visibility lookup is replaced by a fake.
"""
import errno
import io
import json
import os
import shutil
import signal
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
from _support import DAY, GIT_ENV, run, stamp  # noqa: E402

GIT = shutil.which("git")
BASH = shutil.which("bash")
PYTHON3 = shutil.which("python3")

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

SIGNAL_CHILD = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import gblog
real = gblog.Ledger._append_line
def hooked(self, path, line):
    real(self, path, line)
    os.kill(os.getpid(), signal.SIGTERM)
gblog.Ledger._append_line = hooked
sys.exit(gblog.main(["--root", sys.argv[2], "note", "--bot", "ops", "--detail", "x"]))
'''


def git(*args, check=True):
    return subprocess.run(["git"] + list(args), check=check, capture_output=True, text=True).stdout


def commit(repo, message="init"):
    git("-C", repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", message)


def commits(repo):
    return int(git("-C", repo, "rev-list", "--all", "--count", check=False).strip() or 0)


def marker(repo):
    return git("-C", repo, "config", "--local", "--get", "gblog.ledger", check=False).strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-k9k10-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        self.ledger.init()
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT", "GBLOG_ERRORS", "GBLOG_REQUIRE_LEDGER"):
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

    def state_write_failing(self, *failing):
        real, count = gblog.Ledger.write_state, [0]

        def write_state(ledger, bot, state):
            count[0] += 1
            if count[0] in failing:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), ledger._state_path(bot))
            return real(ledger, bot, state)

        return mock.patch.object(gblog.Ledger, "write_state", write_state)


# --------------------------------------------------------------------------- K9-01
@unittest.skipUnless(GIT, "git not installed")
class LedgerRepoGuardTest(Base):
    def test_v05_backup_with_extra_tracked_paths_keeps_syncing(self):
        # the operator's existing backup: logs/, state/, ..., transcripts/, unified/, README.md, .gitignore
        self.seed("ops", 1)
        for rel in ("transcripts/ops/2024-06-01.jsonl", "unified/all.md", "LICENSE"):
            self.write(self.path(*rel.split("/")), "x\n")
        git("init", "-q", self.root)
        git("-C", self.root, "add", "-f", "--", "logs", "state/ops.json", "README.md", ".gitignore",
            "transcripts", "unified", "LICENSE")
        commit(self.root, "0.5 backup")
        self.assertEqual(marker(self.root), "")
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: this backup repository also tracks 3 path(s) gblog does not write (LICENSE, ...)", err)
        self.assertEqual(marker(self.root), "true")
        tracked = git("-C", self.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        for rel in ("LICENSE", "transcripts/ops/2024-06-01.jsonl", "unified/all.md", "logs/ops/%s.jsonl" % DAY):
            self.assertIn(rel, tracked)
        self.assertEqual(commits(self.root), 2)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("also tracks", err)

    def test_unborn_repo_in_a_ledger_with_other_directories_is_adopted(self):
        self.seed("ops", 1)
        self.write(self.path("transcripts", "ops", "t.jsonl"), "x\n")
        git("init", "-q", self.root)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(marker(self.root), "true")
        tracked = git("-C", self.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertFalse([p for p in tracked if p.startswith("transcripts/")], tracked)

    def _project(self, tracked_readme=True):
        repo = os.path.join(self.base, "project")
        os.makedirs(repo)
        git("init", "-q", repo)
        if tracked_readme:
            self.write(os.path.join(repo, "README.md"), "# proj\n")
            self.write(os.path.join(repo, ".gitignore"), "build/\n")
            git("-C", repo, "add", "README.md", ".gitignore")
            commit(repo)
        self.write(os.path.join(repo, "src", "main.py"), "print(1)\n")
        return repo

    def test_readme_only_project_is_refused(self):
        repo = self._project()
        listing = sorted(os.listdir(repo))
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("holds no ledger yet and contains other files (src)", err)
        self.assertEqual(sorted(os.listdir(repo)), listing)
        self.assertEqual(commits(repo), 1)
        self.assertEqual(marker(repo), "")

    def test_unborn_repo_with_untracked_sources_is_refused(self):
        repo = self._project(tracked_readme=False)
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(sorted(os.listdir(repo)), [".git", "src"])
        self.assertEqual(marker(repo), "")

    def test_empty_clone_is_adopted(self):
        repo = os.path.join(self.base, "empty")
        os.makedirs(repo)
        git("init", "-q", repo)
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(marker(repo), "true")
        self.assertTrue(os.path.isdir(os.path.join(repo, "logs")))

    def test_marker_accepts_git_booleans(self):
        repo = self._project()
        git("-C", repo, "add", "src/main.py")
        commit(repo, "code")
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is a git repository that is not a gblog ledger (it tracks src/main.py)", err)
        git("-C", repo, "config", "gblog.ledger", "yes")
        rc, out, err = run(["sync", "--no-push"], repo)
        self.assertEqual(rc, 0, out + err)


# --------------------------------------------------------------------------- K9-02, K9-04, K10-02, K10-06
@unittest.skipUnless(GIT, "git not installed")
class SyncOriginTest(Base):
    def setUp(self):
        super().setUp()
        self.vis = mock.patch.object(gblog, "remote_visibility", return_value={"public": False, "reason": "test"})
        self.vis_mock = self.vis.start()
        self.addCleanup(self.vis.stop)
        self.seed("ops", 1)
        self.first = os.path.join(self.base, "a.git")
        self.second = os.path.join(self.base, "b.git")

    def origin(self):
        return git("-C", self.root, "config", "--local", "--get", "remote.origin.url", check=False).strip()

    def remote_records(self):
        return [r for r in self.chain("system") if r.get("target") == "git remote"]

    def bare(self, name):
        repo = os.path.join(self.base, name)
        git("init", "--bare", "-q", repo)
        return repo

    def test_refused_replacement_is_spooled_as_not_applied(self):
        # K9-02 path 1
        rc, out, err = run(["sync", "--no-push", "--remote", self.first], self.root)
        self.assertEqual(rc, 0, out + err)
        self.ledger.write_state("system", {"seq": 99, "head": "cd" * 32})
        before = commits(self.root)
        rc, out, err = run(["sync", "--no-push", "--remote", self.second, "--replace-remote"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(self.origin(), self.first)
        self.assertEqual(commits(self.root), before)
        spooled = self.ledger.spool_files("system")
        self.assertEqual(len(spooled), 1)
        for path in (spooled[0], self.path("state", "system.alert.json")):
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)["record"]
            self.assertEqual(rec["outcome"], "denied")
            self.assertIn("NOT applied", rec["detail"])
        seq, head = self.ledger._tail_head("system")
        self.ledger.write_state("system", {"seq": seq, "head": head})
        res = self.quiet(self.ledger.spool_replay, "system")
        self.assertEqual(len(res["replayed"]), 1)
        lying = [r for r in self.remote_records() if r.get("outcome") == "ok" and "b.git" in str(r.get("detail"))]
        self.assertEqual(lying, [])
        self.assertEqual(self.remote_records()[-1]["outcome"], "denied")

    def test_origin_change_is_made_when_its_record_is_durable(self):
        # K9-02 path 2: the line is in the log, only the state replace failed
        rc, out, err = run(["sync", "--no-push", "--remote", self.first], self.root)
        self.assertEqual(rc, 0, out + err)
        with self.state_write_failing(1):
            rc, out, err = run(["sync", "--no-push", "--remote", self.second, "--replace-remote"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self.origin(), self.second)
        tail = self.chain("system")[-1]
        self.assertEqual((tail["target"], tail["outcome"]), ("git remote", "ok"))
        self.assertIn("b.git", tail["detail"])
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: recovered state/system.json after an interrupted append", err)

    def test_record_is_written_before_origin_changes(self):
        # K10-06
        events = []
        real_set, real_append = gblog._set_origin, gblog.Ledger.append

        def set_origin(*args, **kwargs):
            events.append("set")
            return real_set(*args, **kwargs)

        def append(ledger, bot, record, **kwargs):
            if record.get("target") == "git remote":
                events.append("record")
            return real_append(ledger, bot, record, **kwargs)

        with mock.patch.object(gblog, "_set_origin", set_origin), mock.patch.object(gblog.Ledger, "append", append):
            rc, out, err = run(["sync", "--no-push", "--remote", self.first], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(events, ["record", "set"])
        self.assertEqual(self.origin(), self.first)

    def test_failed_origin_change_is_recorded_as_fail(self):
        # K10-06
        boom = subprocess.CalledProcessError(1, ["git", "config"], output="", stderr="boom")
        with mock.patch.object(gblog, "_set_origin", side_effect=boom):
            rc, out, err = run(["sync", "--no-push", "--remote", self.first], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self.origin(), "")
        self.assertEqual([r["outcome"] for r in self.remote_records()], ["ok", "fail"])

    def test_global_pushurl_does_not_divert_a_local_origin(self):
        # K9-04
        legit, other = self.bare("legit.git"), self.bare("other.git")
        for key in ("pushurl", "url"):
            cfg = self.write(os.path.join(self.base, "gitconfig-" + key), '[remote "origin"]\n\t%s = %s\n' % (key, other))
            self.vis_mock.reset_mock()
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": cfg}):
                rc, out, err = run(["sync", "--remote", legit], self.root)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("(not pushed)", out)
            self.assertIn("origin is also defined outside the ledger repository", out)
            self.assertEqual((commits(other), commits(legit)), (0, 0))
            self.assertFalse(self.vis_mock.called)
        rc, out, err = run(["sync"], self.root)  # control: without the global section the push goes through
        self.assertEqual(rc, 0, out + err)
        self.assertGreater(commits(legit), 0)

    def test_commit_is_consistent_when_an_append_races_git_add(self):
        # K10-02
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "second", "ts": stamp(5)})
        real, fired = gblog._git, []

        def racing(root, *args, **kwargs):
            if not fired and args[:2] == ("add", "-A") and "--" not in args:
                fired.append(True)
                real(root, "add", "-A", "logs", check=False)          # the log is read ...
                self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "raced", "ts": stamp(6)})
                return real(root, *[a for a in args if a != "logs"], **kwargs)  # ... and state after the append
            return real(root, *args, **kwargs)

        with mock.patch.object(gblog, "_git", racing):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(fired)
        clone = os.path.join(self.base, "clone")
        git("clone", "-q", self.root, clone)
        res = gblog.Ledger(clone).verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)


# --------------------------------------------------------------------------- K9-03
@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class HookLastLineTest(Base):
    def _calls(self, err_text):
        bindir = os.path.join(self.base, "bin")
        fake = self.write(os.path.join(bindir, "gblog"), FAKE_GBLOG)
        os.chmod(fake, 0o755)
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        log = os.path.join(self.base, "fake.log")
        if os.path.exists(log):
            os.remove(log)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), GBLOG_FAKE_LOG=log,
                   HOOK=hook, CMD="ls -la /tmp", HISTFILE=os.devnull, GBLOG_BOT="Ops2", GBLOG_FAKE_ERR=err_text)
        for key in ("HISTCONTROL", "HISTIGNORE", "PROMPT_COMMAND", "GBLOG_TASK"):
            env.pop(key, None)
        proc = subprocess.run([BASH, "--noprofile", "--norc", "-c", 'source "$HOOK"; history -s "$CMD"; __gblog_hook'],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        return self.read(log).splitlines() if os.path.exists(log) else []

    def test_warning_plus_environment_error_logs_once(self):
        calls = self._calls("warning: bot name 'Ops2' is not registered in state/aliases.json (logging under 'Ops2'); x\n"
                            "error: No space left on device: /x/state/Ops2.json")
        self.assertEqual(len(calls), 1, calls)

    def test_refusal_after_registry_warning_still_falls_back(self):
        calls = self._calls("warning: state/aliases.json is unreadable; names are not resolved\n"
                            "error: bot name 'Ops2' is reserved (it collides with gblog's own files under state/)")
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("--bot ops", calls[1])
        self.assertIn("--detail GBLOG_BOT=Ops2 was refused", calls[1])
        calls = self._calls("error: 'Ops2' has no ASCII letters or digits; register it first")
        self.assertEqual(len(calls), 2, calls)

    def test_usage_error_does_not_fall_back(self):
        calls = self._calls("usage: gblog log [-h]\ngblog log: error: argument --outcome: invalid choice: 'x'")
        self.assertEqual(len(calls), 1, calls)

    def test_hook_is_valid_bash_and_marked(self):
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        proc = subprocess.run([BASH, "-n", hook], capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = gblog.shell_hook("ops").splitlines()
        self.assertEqual(lines[1:3], ["# gblog hook v2", "# gblog hook text 3"])


# --------------------------------------------------------------------------- K10-01
class DurableTailTest(Base):
    def _raw_line(self, prev, seq, n):
        rec = {"v": 1, "seq": seq, "ts": stamp(n), "bot": "ops", "type": "note", "detail": "raw", "prev": prev}
        rec["hash"] = gblog.compute_hash(rec)
        with open(self.ledger.log_files("ops")[0], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-specific")
    def test_roll_forward_fsyncs_the_tail_before_state(self):
        recs = self.seed("ops", 2)
        self._raw_line(recs[-1]["hash"], 3, 5)  # the disk after a kill between write and fsync
        log = self.ledger.log_files("ops")[0]
        state = os.path.abspath(self.path("state", "ops.json"))

        def ident(path):
            st = os.stat(path)
            return (st.st_dev, st.st_ino)

        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            st = os.fstat(fd)
            events.append(("fsync", (st.st_dev, st.st_ino)))
            return real_fsync(fd)

        def replace(src, dst, *args, **kwargs):
            events.append(("replace", os.path.abspath(dst)))
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(gblog.os, "fsync", fsync), mock.patch.object(gblog.os, "replace", replace):
            rec = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "four", "ts": stamp(6)})
        self.assertEqual(rec["seq"], 4)
        first_replace = events.index(("replace", state))
        self.assertIn(("fsync", ident(log)), events[:first_replace])
        self.assertIn(("fsync", ident(self.path("logs", "ops"))), events[:first_replace])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_failed_fsync_rolls_the_line_back(self):
        self.seed("ops", 1)
        log = self.ledger.log_files("ops")[0]
        size = os.path.getsize(log)
        state = self.raw_state()
        real, calls = os.fsync, [0]

        def fsync(fd):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError(errno.EIO, "Input/output error")
            return real(fd)

        with mock.patch.object(gblog.os, "fsync", fsync), self.assertRaises(OSError):
            self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual(os.path.getsize(log), size)
        self.assertEqual(self.raw_state(), state)
        err = io.StringIO()
        with redirect_stderr(err):
            rec = self.ledger.append("ops", {"type": "note", "detail": "three", "ts": stamp(2)})
        self.assertNotIn("recovered", err.getvalue())
        self.assertEqual(rec["seq"], 2)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 2)

    def test_update_state_and_rebuild_fsync_the_adopted_tail(self):
        recs = self.seed("ops", 2)
        log = self.ledger.log_files("ops")[0]
        os.remove(self.path("state", "ops.json"))
        with mock.patch.object(gblog, "_fsync_path", wraps=gblog._fsync_path) as spy:
            self.assertTrue(self.quiet(self.ledger.update_state, "ops", {"marker": 1}))
        self.assertEqual([c[0][0] for c in spy.call_args_list], [log])
        self.ledger.write_state("ops", {"seq": 1, "head": recs[0]["hash"]})
        with mock.patch.object(gblog, "_fsync_path", wraps=gblog._fsync_path) as spy:
            res = self.quiet(self.ledger.rebuild_state, "ops")
        self.assertFalse(res["noop"])
        self.assertIn(log, [c[0][0] for c in spy.call_args_list])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_healthy_append_needs_no_recovery_fsync(self):
        self.seed("ops", 1)
        with mock.patch.object(gblog, "_fsync_path", wraps=gblog._fsync_path) as spy:
            self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual(spy.call_count, 0)


# --------------------------------------------------------------------------- K10-03
def chrome_history(directory, count):
    prof = os.path.join(directory, "Default")
    os.makedirs(prof)
    path = os.path.join(prof, "History")
    now = int((time.time() + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        for i in range(1, count + 1):
            conn.execute("INSERT INTO urls VALUES (?, ?, ?)", (i, "https://site%d.example/page" % i, "T%d" % i))
            conn.execute("INSERT INTO visits VALUES (?, ?, ?)", (i, i, now - (count + 10 - i) * 1_000_000))
        conn.commit()
    finally:
        conn.close()
    return path


class CaptureProgressTest(Base):
    NAMES = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]

    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.base, "work")
        self.write(os.path.join(self.work, "keep.txt"), "1")
        self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        for name in self.NAMES:
            self.write(os.path.join(self.work, name), name)

    def targets(self):
        return sorted(r["target"] for r in self.chain() if r.get("capture") == "files" and r.get("type") == "action")

    def test_files_partial_failure_is_not_logged_twice(self):
        real, calls = gblog.Ledger.append, [0]

        def flaky(ledger, bot, record, **kwargs):
            calls[0] += 1
            if calls[0] == 3:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return real(ledger, bot, record, **kwargs)

        with mock.patch.object(gblog.Ledger, "append", flaky), self.assertRaises(OSError):
            self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        self.assertEqual(self.targets(), ["a.txt", "b.txt"])
        res = self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        self.assertEqual(res["changes"], 3)
        self.assertEqual(self.targets(), self.NAMES)
        res = self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        self.assertEqual(res["changes"], 0)

    def test_files_failing_append_that_wrote_its_line_counts_as_done(self):
        # state writes: genesis + record 1, record 2, record 3 (fails after its line is durable)
        with self.state_write_failing(4), self.assertRaises(OSError):
            self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        self.assertEqual(self.targets(), ["a.txt", "b.txt", "c.txt"])
        self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        self.assertEqual(self.targets(), self.NAMES)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_files_refusal_at_first_append_leaves_the_snapshot(self):
        snap = self.path("state", "capture-files-ops.json")
        before = self.read(snap)
        with mock.patch.object(gblog.Ledger, "append", side_effect=OSError(errno.ENOSPC, "full")), \
                self.assertRaises(OSError):
            self.quiet(gblog.capture_files, self.ledger, "ops", self.work)
        self.assertEqual(self.read(snap), before)

    def test_browser_marker_is_saved_in_batches(self):
        hist = chrome_history(os.path.join(self.base, "chrome"), 60)
        real, saves = gblog.Ledger.update_state, []

        def update_state(ledger, bot, fields):
            if "capture_browser_last" in fields:
                saves.append(dict(fields["capture_browser_last"]))
            return real(ledger, bot, fields)

        with mock.patch.object(gblog.Ledger, "update_state", update_state):
            res = self.quiet(gblog.capture_browser, self.ledger, "web", [hist], 24)
        self.assertEqual(res["added"], 60, res)
        self.assertGreaterEqual(len(saves), 3)
        markers = [s[hist] for s in saves]
        self.assertEqual(markers, sorted(markers))
        res = self.quiet(gblog.capture_browser, self.ledger, "web", [hist], 24)
        self.assertEqual(res["added"], 0, res)
        self.assertEqual(len(self.chain("web")), 60)


# --------------------------------------------------------------------------- K10-04
@unittest.skipUnless(os.name == "posix" and hasattr(signal, "pthread_sigmask"), "POSIX signals are required")
class SignalTest(Base):
    def test_sigterm_between_line_and_state_is_deferred_and_exits_143(self):
        if signal.getsignal(signal.SIGTERM) is signal.SIG_IGN:
            self.skipTest("SIGTERM is ignored in this environment")
        proc = subprocess.run([sys.executable, "-c", SIGNAL_CHILD, REPO, self.root], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 143, proc.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"Traceback", proc.stderr)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["records"], res["warnings"]), (1, []))
        self.assertEqual(self.raw_state()["head"], self.chain()[-1]["hash"])
        self.assertEqual([n for n in os.listdir(self.path("state")) if n.endswith(".tmp")], [])

    def test_handlers_are_restored_after_main(self):
        before = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGHUP")}
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        with self.assertRaises(SystemExit):
            gblog.main(["--no-such-flag"])
        after = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGHUP")}
        self.assertEqual(before, after)

    def test_mask_is_restored_when_the_state_write_fails(self):
        self.seed("ops", 1)
        before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        with self.state_write_failing(1), self.assertRaises(OSError):
            self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), before)
        self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "three", "ts": stamp(2)})
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), before)


# --------------------------------------------------------------------------- K10-05
@unittest.skipUnless(BASH and PYTHON3 and os.name == "posix", "bash and python3 on POSIX are required")
class InstallRenameTest(Base):
    def test_install_leaves_no_temporary_executable(self):
        home, prefix = os.path.join(self.base, "home"), os.path.join(self.base, "prefix", "bin")
        os.makedirs(home)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GBLOG_")}
        env.update({"HOME": home, "GBLOG_PREFIX": prefix, "GBLOG_ROOT": os.path.join(self.base, "inst-ledger")})
        for _ in range(2):  # the second run replaces an existing executable
            proc = subprocess.run([BASH, os.path.join(REPO, "install.sh")], cwd=self.base, env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("installed %s/gblog from local checkout" % prefix, proc.stdout)
            self.assertEqual(os.listdir(prefix), ["gblog"])
        with open(os.path.join(prefix, "gblog"), "rb") as a, open(os.path.join(REPO, "gblog.py"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(os.access(os.path.join(prefix, "gblog"), os.X_OK))


# --------------------------------------------------------------------------- K10-07
class CrashReportingTest(Base):
    HINT = ("logs/%s/%s.jsonl:%d: last line is incomplete and state/%s.json names the record before it "
            "(interrupted write): restore the line from backup; no acknowledged record is missing")

    def _tear(self, bot):
        with open(self.ledger.log_files(bot)[0], "ab") as fh:
            fh.write(b'{"bot": "ops", "detail": "half')

    def test_torn_tail_gets_a_hint(self):
        self.seed("ops", 3)
        self._tear("ops")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(res["problems"], ["logs/ops/%s.jsonl:4: not valid JSON" % DAY])
        self.assertEqual(res["warnings"], [self.HINT % ("ops", DAY, 4, "ops")])
        self.assertEqual(dict(res), dict(self.ledger._verify_reference("ops")))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      ! " + self.HINT % ("ops", DAY, 4, "ops"), out.splitlines())

    def test_corrupt_line_in_the_middle_or_other_state_head_gets_no_hint(self):
        self.seed("mid", 3)
        log = self.ledger.log_files("mid")[0]
        lines = self.read(log).splitlines()
        self.write(log, "".join(line + "\n" for line in [lines[0], "{not json"] + lines[1:]))
        res = self.ledger.verify("mid")
        self.assertFalse(res["ok"])
        self.assertEqual(res["warnings"], [])
        recs = self.seed("ops", 3)
        self._tear("ops")
        self.ledger.write_state("ops", {"seq": 2, "head": recs[1]["hash"]})
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(dict(res), dict(self.ledger._verify_reference("ops")))

    def test_torn_tail_in_an_older_day_file_gets_no_hint(self):
        self.seed("ops", 2)
        self._tear("ops")
        with open(self.ledger.log_files("ops")[0], "ab") as fh:
            fh.write(b"\n")
        self.write(self.path("logs", "ops", "2024-06-02.jsonl"), "")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(dict(res), dict(self.ledger._verify_reference("ops")))

    def test_recorded_verify_names_the_interrupted_append(self):
        self.seed("ops", 1)
        with self.state_write_failing(1), self.assertRaises(OSError):
            self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        rc, out, err = run(["verify", "--all", "--record"], self.root)
        self.assertEqual(rc, 1, out + err)
        last = self.chain()[-1]
        self.assertEqual((last["type"], last["outcome"]), ("verify", "fail"))
        self.assertEqual(last["detail"], "state one record behind (interrupted append); repaired by this record")
        rc, out, err = run(["verify", "--all", "--record"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.chain()[-1]["detail"], "3 records verified")

    def test_other_recorded_failures_keep_their_problem_text(self):
        self.seed("ops", 2)
        log = self.ledger.log_files("ops")[0]
        self.write(log, self.read(log).replace("ops record 1", "ops record X"))
        rc, out, err = run(["verify", "--all", "--record"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("hash mismatch (record edited)", out)


if __name__ == "__main__":
    unittest.main()
