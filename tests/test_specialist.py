"""Tests for the two specialist reviews of 2026-11-16.

* K1: cross-command consistency (``--bot`` selection, task ids, report days, strict mode, the shell hook)
* K2: filesystem adversary (FIFOs, hard links, symlinks and directories planted in the ledger; permissions)

Each test names the finding it covers. Everything runs in temporary directories. No network is used:
git remotes are local bare repositories and the visibility lookup is replaced by a fake. Tests that
would hang if a fix regressed run the CLI in a child process with a timeout.
"""
import errno
import io
import json
import os
import random
import re
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
from _support import DAY, GIT_ENV, git as _git_cmd, run_cli, stamp, write_raw_chain  # noqa: E402
from _support import run_strict as run  # noqa: E402  (K6-01: a swallowed crash fails the test)

GIT = shutil.which("git")
BASH = shutil.which("bash")
LINK_TEXT = "%s: symbolic link (gblog does not write through one)"

FAKE_GBLOG = r'''#!/bin/sh
printf '%s\n' "$*" >> "$GBLOG_FAKE_LOG"
bot=""; prev=""
for a in "$@"; do
  if [ "$prev" = "--bot" ]; then bot="$a"; fi
  prev="$a"
done
if [ "$bot" = "ops" ]; then exit 0; fi
exit "${GBLOG_FAKE_RC:-2}"
'''


# write_raw_chain is tests/_support.write_raw_chain (round-4 G2-05).


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-spec-"))
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
        err = io.StringIO()
        with redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, err.getvalue()

    def seed(self, bot="ops", n=2, day=DAY):
        with redirect_stderr(io.StringIO()):
            return [self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1), "ts": stamp(i, day)})
                    for i in range(n)]

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

    def log_bytes(self, bot="ops"):
        data = b""
        for path in self.ledger.log_files(bot):
            with open(path, "rb") as fh:
                data += fh.read()
        return data

    def break_state(self, bot="ops"):
        self.ledger.write_state(bot, {"seq": 99, "head": "cd" * 32})

    def pending(self, bot="ops"):
        d = self.path("spool", bot)
        return sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []

    def fifo(self, path):
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo is not available")
        try:
            os.mkfifo(path)
        except OSError:
            self.skipTest("FIFOs are not supported here")

    def symlink(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not supported here")

    def hardlink(self, target, link):
        try:
            os.link(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("hard links are not supported here")


# --------------------------------------------------------------------------- K1-01
class ReportAcrossMidnightTest(Base):
    def _night_job(self, close=True):
        L = self.ledger
        L.append("ops", {"type": "task_start", "task_id": "n1", "task": "Night job", "ts": "2024-06-01T23:59:00Z"})
        L.append("ops", {"type": "action", "task_id": "n1", "action": "shell", "target": "make", "outcome": "ok",
                         "ts": "2024-06-02T00:00:30Z"})
        if close:
            L.append("ops", {"type": "task_end", "task_id": "n1", "outcome": "ok", "ts": "2024-06-02T00:01:00Z"})

    def test_report_task_crossing_utc_midnight(self):
        self._night_job()
        rc, out, err = run(["report", "--date", "2024-06-01", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("outcome **ok** (closed 2024-06-02T00:01:00Z, after this day)", out)
        self.assertNotIn("**open**", out)
        rc, out, err = run(["report", "--date", "2024-06-02", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("### Night job  `n1`", out.splitlines())
        self.assertIn("_start 2024-06-01T23:59:00Z, end 2024-06-02T00:01:00Z, outcome **ok**_", out.splitlines())

    def test_report_open_task_stays_open(self):
        self._night_job(close=False)
        rc, out, err = run(["report", "--date", "2024-06-01", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("_start 2024-06-01T23:59:00Z, outcome **open**_", out.splitlines())
        rc, out, err = run(["report", "--date", "2024-06-02", "--stdout"], self.root)
        self.assertIn("### Night job  `n1`", out.splitlines())
        self.assertIn("outcome **open**", out)

    def test_render_report_without_context_is_unchanged(self):
        recs = [{"v": 1, "seq": 1, "ts": DAY + "T09:00:00Z", "bot": "ops", "type": "task_start", "task_id": "t1", "task": "T"}]
        self.assertEqual(gblog.render_report(recs, "r"), gblog.render_report(recs, "r", None, None))
        self.assertIn("outcome **open**", gblog.render_report(recs, "r"))

    def test_status_json_open_task_list(self):
        with redirect_stderr(io.StringIO()):
            self.ledger.append("ops", {"type": "task_start", "task_id": "a1", "task": "First"})
            self.ledger.append("ops", {"type": "task_start", "task_id": "a2", "task": "Second"})
            self.ledger.append("ops", {"type": "task_start", "task_id": "a3", "task": "Third"})
            self.ledger.append("ops", {"type": "task_end", "task_id": "a3", "outcome": "ok"})
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        row = json.loads(out)["bots"][0]
        self.assertEqual(row["open_tasks"], 2)
        self.assertEqual(len(row["open_task_list"]), row["open_tasks"])
        self.assertEqual({(t["task_id"], t["task"], t["chain"]) for t in row["open_task_list"]},
                         {("a1", "First", "ops"), ("a2", "Second", "ops")})
        self.assertTrue(all(isinstance(t["started"], str) for t in row["open_task_list"]))


# --------------------------------------------------------------------------- K1-02 .. K1-05
class BotSelectionTest(Base):
    def test_coverage_json_has_documented_keys(self):
        # K1-02: a key the documents name must exist in real output
        self.seed("ops", 1)
        rc, out, err = run(["coverage", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue({"rows", "auto", "matched", "pct", "self_only", "elsewhere"} <= set(json.loads(out)))

    def test_readers_warn_about_unknown_bot(self):
        # K1-03
        self.seed("ops", 1)
        line = "warning: no chain on disk for 'opps'; nothing to show"
        for argv in (["report", "--stdout"], ["status"], ["tail"], ["export"], ["coverage"], ["tune", "--stdout"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv + ["--bot", "opps"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(err.count(line), 1, err)
                rc, out, err = run(argv + ["--bot", "ops"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("no chain on disk", err)
                rc, out, err = run(argv, self.root)
                self.assertNotIn("no chain on disk", err)
        self.assertFalse(os.path.exists(self.path("logs", "opps")))

    def test_no_warning_for_display_name_or_alias(self):
        # K1-03
        self.seed("cos", 1)
        self.quiet(self.ledger.add_alias, "chief-of-staff", "Chief of Staff", ["cos"])
        for name in ("Chief of Staff", "cos", "chief-of-staff"):
            rc, out, err = run(["tail", "--bot", name], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("no chain on disk", err)
            self.assertIn("cos record 1", out)

    def test_anchor_bot_fails_for_deleted_chain_with_witness(self):
        # K1-04: `verify --bot ops` fails here; `anchor --bot ops` used to exit 0
        self.seed("ops", 2)
        self.seed("ops-new", 1)
        self.quiet(self.ledger.anchor)
        self.quiet(self.ledger.add_alias, "ops-new", "Ops", ["ops"])
        shutil.rmtree(self.path("logs", "ops"))
        os.remove(self.path("state", "ops.json"))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        rc, out, err = run(["anchor", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+ops-new\s")
        self.assertRegex(out, r"(?m)^ANCHOR FAIL ops\s")
        self.assertIn("but logs/ops is missing", out)
        with open(self.path("anchors", "ops.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["records"], 2)  # the witness is left as it was
        rc, out, err = run(["anchor", "--bot", "ops-new"], self.root)  # still names the lost variant
        self.assertEqual(rc, 1, out + err)

    def _refusal_in_variant_chain(self):
        self.seed("cos", 1)
        self.seed("chief-of-staff", 1)
        self.quiet(self.ledger.add_alias, "chief-of-staff", "Chief of Staff", ["cos"])
        self.break_state("cos")
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("cos", {"type": "note", "detail": "refused in cos"})

    def test_spool_list_bot_covers_variant_chains(self):
        # K1-05
        self._refusal_in_variant_chain()
        rc, out, err = run(["status", "--bot", "chief-of-staff"], self.root)
        self.assertIn("ALERT cos: tail mismatch since", out)
        for name in ("chief-of-staff", "Chief of Staff", "cos"):
            with self.subTest(name=name):
                rc, out, err = run(["spool", "list", "--bot", name], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("refused in cos", out)
                self.assertEqual(len(out.splitlines()), 1)
        self.seed("ops", 1)
        rc, out, err = run(["spool", "list", "--bot", "ops"], self.root)
        self.assertEqual((rc, out), (0, ""))

    def test_spool_replay_hints_at_sibling_chains(self):
        # K1-05: replay stays per chain; the slug run says where the pending events are
        self._refusal_in_variant_chain()
        rc, out, err = run(["spool", "replay", "--bot", "chief-of-staff"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("replayed 0 spooled event(s) for chief-of-staff", out)
        self.assertIn("note: spooled event(s) are pending in chain(s) cos", err)
        self.assertEqual(len(self.pending("cos")), 1)
        seq, head = self.ledger._tail_head("cos")
        self.ledger.write_state("cos", {"seq": seq, "head": head})
        rc, out, err = run(["spool", "replay", "--bot", "cos"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("pending in chain", err)
        self.assertEqual(self.pending("cos"), [])


# --------------------------------------------------------------------------- K1-06
class TaskIdDiagnosticsTest(Base):
    LOG = ["log", "--bot", "ops", "--action", "shell", "--target", "ls"]

    def test_log_warns_for_closed_task_from_env(self):
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "A"], self.root)
        self.assertEqual(rc, 0, err)
        tid = tid.strip()
        with mock.patch.dict(os.environ, {"GBLOG_TASK": tid}):
            for argv in (self.LOG, ["note", "--bot", "ops", "--detail", "n"],
                         ["approval", "--bot", "ops", "--target", "send", "--decision", "allowed"]):
                rc, out, err = run(argv, self.root)  # inside the open task: silent
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("warning: task", err)
            rc, out, err = run(["task-end", "--bot", "ops", "--outcome", "ok"], self.root)
            self.assertEqual(rc, 0, out + err)
            rc, out, err = run(self.LOG, self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("warning: task '%s' is already closed; recorded anyway" % tid, err)
            self.assertRegex(out, r"^[0-9a-f]{12} #\d+ action\n$")
        self.assertEqual(self.chain()[-1]["task_id"], tid)  # recorded anyway

    def test_unknown_task_id_warns_except_for_hook_records(self):
        self.seed("ops", 1)
        rc, out, err = run(["note", "--bot", "ops", "--task-id", "typo123", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: task 'typo123' has no task-start in the last 30 days of ops; recorded anyway", err)
        rc, out, err = run(self.LOG + ["--task-id", "typo123", "--source", "auto"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("warning: task", err)
        rc, out, err = run(self.LOG, self.root)  # no task id at all: nothing to check
        self.assertNotIn("warning: task", err)

    def test_task_start_duplicate_id_and_missing_title_warn(self):
        rc, out, err = run(["task-start", "--bot", "ops", "--task-id", "daily", "--task", "run 1"], self.root)
        self.assertEqual((rc, out), (0, "daily\n"))
        self.assertNotIn("already used", err)
        rc, out, err = run(["task-start", "--bot", "ops", "--task-id", "daily", "--task", "run 2"], self.root)
        self.assertEqual((rc, out), (0, "daily\n"))
        self.assertIn("warning: task id 'daily' was already used by ops in the last 30 days", err)
        rc, out, err = run(["task-start", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: task-start without --task: the task has no title", err)
        self.assertRegex(out, r"^[0-9a-f]{10}\n$")

    def test_task_status_unit(self):
        self.assertEqual(gblog._task_status(self.ledger, "ops", "t1"), "unknown")
        with redirect_stderr(io.StringIO()):
            self.ledger.append("ops", {"type": "task_start", "task_id": "t1", "task": "T"})
            self.assertEqual(gblog._task_status(self.ledger, "ops", "t1"), "open")
            self.ledger.append("ops", {"type": "task_end", "task_id": "t1", "outcome": "ok"})
        self.assertEqual(gblog._task_status(self.ledger, "ops", "t1"), "closed")
        self.assertEqual(gblog._task_status(self.ledger, "ops", "t"), "unknown")  # a prefix is not the id


# --------------------------------------------------------------------------- K1-07
@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class HookFallbackTest(Base):
    def _calls(self, **extra):
        bindir = os.path.join(self.base, "bin")
        fake = self.write(os.path.join(bindir, "gblog"), FAKE_GBLOG)
        os.chmod(fake, 0o755)
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        log = os.path.join(self.base, "fake.log")
        if os.path.exists(log):
            os.remove(log)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), GBLOG_FAKE_LOG=log,
                   HOOK=hook, CMD="ls -la /tmp", HISTFILE=os.devnull)
        for key in ("HISTCONTROL", "HISTIGNORE", "PROMPT_COMMAND", "GBLOG_BOT", "GBLOG_TASK", "GBLOG_FAKE_RC"):
            env.pop(key, None)
        env.update(extra)
        proc = subprocess.run([BASH, "--noprofile", "--norc", "-c", 'source "$HOOK"; history -s "$CMD"; __gblog_hook'],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.read(log).splitlines() if os.path.exists(log) else []

    def test_refused_bot_name_falls_back_to_the_installed_bot(self):
        calls = self._calls(GBLOG_BOT="Ops2")
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("--bot Ops2", calls[0])
        self.assertIn("--bot ops", calls[1])
        self.assertIn("--target=ls -la /tmp", calls[1])
        self.assertIn("--source auto", calls[1])
        self.assertIn("--detail GBLOG_BOT=Ops2 was refused", calls[1])

    def test_only_exit_2_triggers_the_fallback(self):
        self.assertEqual(len(self._calls(GBLOG_BOT="Ops2", GBLOG_FAKE_RC="4")), 1)  # a tail mismatch stays on its chain
        self.assertEqual(len(self._calls()), 1)                                      # GBLOG_BOT unset
        self.assertEqual(len(self._calls(GBLOG_BOT="ops")), 1)                       # the installed Bot itself

    def test_hook_is_still_valid_bash(self):
        hook = self.write(os.path.join(self.base, "hook.sh"), gblog.shell_hook("ops"))
        proc = subprocess.run([BASH, "-n", hook], capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(gblog.shell_hook("ops").splitlines()[1], "# gblog hook v2")


# --------------------------------------------------------------------------- K1-08, K1-09, K1-11, K1-13
class SmallConsistencyTest(Base):
    def test_tune_apply_follows_alias_added_after_generation(self):
        # K1-08
        self.write(self.path("tuning", "proposals.json"), json.dumps({"generated": "2024-06-01T10:00:00Z", "proposals": [
            {"id": "abcd1234", "bot": "old", "title": "t", "category": "skill", "key": "k"}]}))
        self.quiet(self.ledger.add_alias, "new", "New Bot", ["old"])
        rc, out, err = run(["tune", "--apply", "abcd1234"], self.root)
        self.assertEqual(rc, 0, out + err)
        last = self.chain("new")[-1]
        self.assertEqual((last["type"], last["target"], last["bot"]), ("tune", "abcd1234", "new"))
        self.assertFalse(os.path.exists(self.path("logs", "old")))

    def test_disk_name_unit(self):
        # K1-09: the mapping a case-insensitive filesystem needs, tested with a patched view of the disk
        with mock.patch.object(gblog.os.path, "isdir", return_value=True), \
                mock.patch.object(gblog.os, "listdir", return_value=["ops", "web"]):
            self.assertEqual(self.ledger._disk_name("Ops"), "ops")
            self.assertEqual(self.ledger._disk_name("ops"), "ops")
            self.assertEqual(self.ledger._disk_name("web2"), "web2")
        self.assertEqual(self.ledger._disk_name("Ops"), "Ops")  # no such directory: the name is what it is
        self.assertEqual(self.ledger._disk_name("123"), "123")

    def test_case_sensitive_filesystem_keeps_two_chains(self):
        probe = os.path.join(self.base, "CaseProbe")
        os.mkdir(probe)
        if os.path.exists(os.path.join(self.base, "caseprobe")):
            self.skipTest("this filesystem ignores letter case")
        self.seed("ops", 1)
        self.seed("Ops", 1)
        self.assertEqual(sorted(self.ledger.bots()), ["Ops", "ops"])
        self.assertEqual([r["bot"] for r in self.chain("Ops")], ["Ops"])

    def test_decision_synonyms_and_action_hyphen(self):
        # K1-11
        rc, out, err = run(["approval", "--bot", "ops", "--target", "send mail", "--decision", "approved"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.endswith("approval allowed\n"), out)
        self.assertEqual((self.chain()[-1]["decision"], self.chain()[-1]["outcome"]), ("allowed", "ok"))
        rc, out, err = run(["approval", "--bot", "ops", "--target", "send mail", "--decision", "Rejected"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((self.chain()[-1]["decision"], self.chain()[-1]["outcome"]), ("denied", "denied"))
        rc, out, err = run(["approval", "--bot", "ops", "--target", "x", "--decision", "banana"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("invalid choice", err)
        rc, out, err = run(["log", "--bot", "ops", "--action", "computer-use", "--target", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.chain()[-1]["action"], "computer_use")
        self.assertEqual(len(self.chain()), 3)

    def test_small_items(self):
        # K1-13 (a), (b), (c), (d), (e)
        self.seed("ops", 1)
        rc, out, err = run(["verify", "--all", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: --all given; --bot ops is ignored", err)
        rc, out, err = run(["report", "--since", "yesterday", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"^# Activity report since \d{4}-\d{2}-\d{2}\n")
        rc, out, err = run(["export", "--since", "today"], self.root)
        self.assertEqual((rc, out), (0, ""))
        rc, out, err = run(["report", "--since", "nope", "--stdout"], self.root)
        self.assertEqual(rc, 2, out + err)
        src = self.write(os.path.join(self.base, "e.txt"), "evidence\n")
        rc, out, err = run(["archive", "--bot", "ops", "--file", src, "--task", "T"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: --task is not stored on archive records", err)
        self.assertNotIn("task", self.chain()[-1])
        self.write(self.ledger.aliases_path(), json.dumps({"strict": "off", "bots": {"ops": {"display": "Ops", "aliases": []}}}))
        rc, out, err = run(["note", "--bot", "stranger", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)  # "off" is not "on": warned, not refused
        self.assertIn("not registered", err)
        rc, out, err = run(["alias", "list"], self.root)
        self.assertIn("strict: off", out)
        want = gblog._ts_epoch("2024-06-01T10:00:00Z")
        self.assertEqual(gblog._ts_epoch("2024-06-01T19:00:00+09:00"), want)
        self.assertEqual(gblog._ts_epoch("2024-06-01 10:00:00"), want)
        self.assertEqual(gblog._ts_epoch("2024-06-01T10:00:00Z\n## x"), want)
        self.assertEqual(gblog._ts_epoch("garbage"), 0.0)


# --------------------------------------------------------------------------- K2-01
class AnchorVerifyIsolationTest(Base):
    def setUp(self):
        super().setUp()
        self.seed("aaa", 1)
        self.seed("zzz", 1)

    def _rollup(self):
        days = self.path("anchors", "days")
        with open(os.path.join(days, sorted(os.listdir(days))[0]), encoding="utf-8") as fh:
            return json.load(fh)

    def test_anchor_isolates_a_non_regular_history_file(self):
        os.mkdir(self.path("anchors", "aaa.history.jsonl"))
        rc, out, err = run(["anchor"], self.root, internal_ok=True)  # "anchor aborted" is the expected entry
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR FAIL aaa\s")
        self.assertIn("      - anchor aborted: IsADirectoryError", out.splitlines())
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")
        self.assertTrue(os.path.isfile(self.path("anchors", "zzz.json")))
        self.assertEqual(sorted(c["bot"] for c in self._rollup()["chains"]), ["aaa", "zzz"])

    def test_anchor_isolates_a_directory_at_the_anchor_file(self):
        os.mkdir(self.path("anchors", "aaa.json"))
        rc, out, err = run(["anchor"], self.root, internal_ok=True)  # "anchor aborted" is the expected entry
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR FAIL aaa\s")
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")
        self.assertTrue(os.path.isfile(self.path("anchors", "zzz.json")))

    def _unlistable_days(self):
        real = os.listdir

        def listdir(path="."):
            if os.path.basename(str(path)) == "days":
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real(path)

        return mock.patch.object(gblog.os, "listdir", listdir)

    def test_verify_all_survives_unlistable_witness_directory(self):
        self.quiet(self.ledger.anchor)
        with self._unlistable_days():
            rc, out, err = run(["verify", "--all"], self.root)
            self.assertEqual(rc, 1, out + err)
            self.assertRegex(out, r"(?m)^OK\s+aaa\s")
            self.assertRegex(out, r"(?m)^OK\s+zzz\s")
            self.assertRegex(out, r"(?m)^FAIL\s+\(witness files\)\s+0 records")
            self.assertIn("      - anchors/days cannot be listed (PermissionError): deleted chains cannot be reported",
                          out.splitlines())
            rc, out, err = run(["verify", "--all", "--json"], self.root)
            self.assertEqual(rc, 1, out + err)
            self.assertEqual(sorted(e["bot"] for e in json.loads(out)), ["(witness files)", "aaa", "zzz"])
            rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+aaa\s")
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")
        self.assertIn("anchors/days cannot be listed (PermissionError)", out)

    def test_anchor_reports_a_rollup_that_cannot_be_written(self):
        real = gblog.Ledger._atomic_json

        def atomic_json(ledger, path, obj):
            if os.path.basename(os.path.dirname(path)) == "days":
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real(ledger, path, obj)

        with mock.patch.object(gblog.Ledger, "_atomic_json", atomic_json):
            rc, out, err = run(["anchor"], self.root)
            self.assertEqual(rc, 1, out + err)
            self.assertRegex(out, r"(?m)^ANCHOR OK\s+aaa\s")
            self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")
            self.assertRegex(out, r"(?m)^      - anchors/days/\d{4}-\d{2}-\d{2}\.json: not written \(PermissionError\)$")
            with mock.patch.dict(os.environ, {"GBLOG_DEBUG": "1"}):
                with self.assertRaises(PermissionError):
                    self.ledger.anchor()


# --------------------------------------------------------------------------- K2-02
class LockFileTest(Base):
    def test_fifo_lock_file_does_not_hang_verify(self):
        self.seed("ops", 1)
        self.seed("zzz", 1)
        lock = self.path("state", "ops.lock")
        os.remove(lock)
        self.fifo(lock)
        rc, out, err = run_cli(["verify", "--all"], self.root, timeout=60)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - state/ops.lock: not a regular file (appends to this chain exit 2)", out.splitlines())
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")
        rc, out, err = run_cli(["anchor"], self.root, timeout=60)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")

    def test_fifo_lock_file_append_error_names_the_file(self):
        self.seed("ops", 1)
        lock = self.path("state", "ops.lock")
        os.remove(lock)
        self.fifo(lock)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "y"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("lock file is not a regular file", err)
        self.assertIn("ops.lock", err)
        self.assertNotIn("Bad file descriptor", err)
        self.assertEqual(len(self.chain()), 1)

    @unittest.skipIf(gblog.fcntl is None, "flock is not available on this platform")
    def test_hardlinked_anchor_lock_does_not_deadlock(self):
        self.seed("ops", 1)
        self.quiet(self.ledger.anchor)
        lock = self.path("state", "ops.lock")
        os.remove(lock)
        self.hardlink(self.path("state", ".anchors.lock"), lock)
        rc, out, err = run_cli(["anchor"], self.root, timeout=60)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+ops\s")
        rec, _err = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "still appends", "ts": stamp(5)})
        self.assertEqual(rec["seq"], 2)

    def test_lock_helper_refuses_non_regular_files(self):
        with self.assertRaises(OSError):
            gblog._open_lock(self.path("state"))  # a directory
        fifo = os.path.join(self.base, "pipe.lock")
        self.fifo(fifo)
        for mode in ("a+", "r"):
            with self.assertRaises(OSError) as ctx:
                gblog._open_lock(fifo, mode)
            self.assertNotEqual(ctx.exception.errno, errno.EBADF)


# --------------------------------------------------------------------------- K2-03 (already in the code; pinned here)
class HugeGapTest(Base):
    def test_huge_seq_jump_is_described_by_its_size(self):
        write_raw_chain(self.root, "aaa", [{}, {"seq": 10 ** 12}])
        res = self.ledger.verify("aaa")
        self.assertFalse(res["ok"])
        self.assertEqual(res["breaks"][0]["missing_seqs"], [])
        self.assertEqual(res["breaks"][0]["missing_count"], 10 ** 12 - 2)
        write_raw_chain(self.root, "bbb", [{}, {"seq": 5}])
        small = self.ledger.verify("bbb")["breaks"][0]
        self.assertEqual(small["missing_seqs"], [2, 3, 4])
        self.assertNotIn("missing_count", small)

    def test_break_ack_with_huge_gap_is_rejected_without_allocating(self):
        brk = {"file": "logs/aaa/%s.jsonl" % DAY, "line_before": 1, "line_after": 2, "seq_before": 0,
               "seq_after": 2000000000, "hash_before": "aa" * 32, "orphan_prev": "bb" * 32,
               "hash_after": "cc" * 32, "missing_seqs": []}
        write_raw_chain(self.root, "aaa", [{"type": "break_ack", "jay_approval_id": "x", "break": brk}])
        self.seed("zzz", 1)
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - logs/aaa/%s.jsonl:1: malformed break_ack" % DAY, out.splitlines())
        self.assertNotIn("verify aborted", out)
        self.assertRegex(out, r"(?m)^OK\s+zzz\s")


# --------------------------------------------------------------------------- K2-04, K2-07, sync cache
@unittest.skipUnless(GIT, "git not installed")
class SyncFilesystemTest(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(gblog, "remote_visibility", return_value={"public": False, "reason": "test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _git(self, *args):
        return _git_cmd(*args)  # never the repository of whoever runs the suite (K6-05)

    def _commits(self, repo):
        res = subprocess.run(["git", "-C", repo, "rev-list", "--all", "--count"], capture_output=True, text=True)
        return int(res.stdout.strip() or 0) if res.returncode == 0 else 0

    def test_symlinked_archive_file_is_not_reported_as_pushed(self):
        bare = os.path.join(self.base, "origin.git")
        self._git("init", "--bare", "-q", bare)
        rec, _err = self.quiet(gblog.archive_file, self.ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(0))
        full = self.path(*rec["path"].split("/"))
        moved = os.path.join(self.base, "moved.gz")
        os.rename(full, moved)
        self.symlink(moved, full)
        text = LINK_TEXT % rec["path"]
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["warnings"], [text])
        self.assertIn(text, self.ledger.verify("ops", strict=True)["problems"])
        self.assertEqual(self.ledger.verify("ops", skip_archives=True)["warnings"], [])
        _forced, checked = gblog._sync_evidence(self.ledger)
        self.assertEqual(checked[rec["path"]][0], -1)
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("(not pushed)", out)
        self.assertIn("%s is a symbolic link: git stores the link, not the file; not pushed" % rec["path"], out)
        self.assertEqual(self._commits(bare), 0)

    def test_symlinked_day_file_is_drift(self):
        self.seed("ops", 1)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        day = "logs/ops/%s.jsonl" % DAY
        full = self.path(*day.split("/"))
        moved = os.path.join(self.base, "moved.jsonl")
        os.rename(full, moved)
        self.symlink(moved, full)
        _forced, checked = gblog._sync_evidence(self.ledger)
        self.assertEqual(checked[day][0], -1)
        self.assertIn(day, gblog._sync_drift(self.root, checked))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_git_objects_are_private(self):
        # K2-07
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.seed("ops", 1)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        loose = []
        git_dir = self.path(".git")
        for dirpath, dirnames, filenames in os.walk(git_dir):
            for name in [""] + dirnames + filenames:
                full = os.path.join(dirpath, name) if name else dirpath
                if not os.path.islink(full) and os.stat(full).st_mode & 0o077:
                    loose.append(os.path.relpath(full, self.root))
        self.assertEqual(loose, [])
        self.assertEqual(os.umask(0o022), 0o022)  # the caller's umask is restored after every git call

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_existing_git_dir_mode_is_left_alone(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self._git("init", "-q", self.root)
        mode = os.stat(self.path(".git")).st_mode & 0o777
        self.seed("ops", 1)
        rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.stat(self.path(".git")).st_mode & 0o777, mode)

    def test_sync_keeps_no_verification_cache(self):
        self.seed("ops", 1)
        for _ in range(2):
            with mock.patch.object(gblog, "_git_blob_id", wraps=gblog._git_blob_id) as spy:
                rc, out, err = run(["sync", "--no-push"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertGreater(spy.call_count, 0)  # hashed on every run
        self.assertFalse(os.path.exists(self.path(".git", "gblog-sync-cache.json")))


# --------------------------------------------------------------------------- K2-05
class InPlaceWriteTest(Base):
    def test_planted_tmp_hard_link_is_not_written_through(self):
        self.seed("ops", 1)
        victim = self.write(os.path.join(self.base, "victim.txt"), "precious\n")
        state = self.ledger._state_path("ops")
        self.hardlink(victim, gblog._tmp_name(state))
        with redirect_stderr(io.StringIO()):
            try:
                self.ledger.append("ops", {"type": "note", "detail": "after the plant", "ts": stamp(5)})
            except OSError:
                pass  # refusing this one write is fine
            self.assertEqual(self.read(victim), "precious\n")
            self.assertEqual(os.stat(victim).st_nlink, 1)
            self.assertFalse(os.path.lexists(gblog._tmp_name(state)))
            self.ledger.append("ops", {"type": "note", "detail": "next", "ts": stamp(6)})
        self.assertNotEqual(os.stat(state).st_ino, os.stat(victim).st_ino)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    def test_stale_tmp_file_is_replaced(self):
        self.seed("ops", 1)
        self.write(gblog._tmp_name(self.ledger._state_path("ops")), "left by a killed process")
        rec, _err = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertEqual(rec["seq"], 2)
        self.assertEqual([n for n in os.listdir(self.path("state")) if n.endswith(".tmp")], [])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_fifo_at_tmp_name_is_refused_not_opened(self):
        self.seed("ops", 1)
        tmp = gblog._tmp_name(self.ledger._state_path("ops"))
        self.fifo(tmp)
        with self.assertRaises(OSError), redirect_stderr(io.StringIO()):
            self.ledger.append("ops", {"type": "note", "detail": "two", "ts": stamp(1)})
        self.assertFalse(os.path.lexists(tmp))

    def test_open_private_refuses_hard_links_and_fifos(self):
        victim = self.write(os.path.join(self.base, "victim.txt"), "precious\n")
        link = os.path.join(self.base, "link.txt")
        self.hardlink(victim, link)
        with self.assertRaises(OSError):
            gblog._open_private(link, "w", encoding="utf-8")
        self.assertEqual(self.read(victim), "precious\n")
        with gblog._open_private(os.path.join(self.base, "plain.txt"), "w", encoding="utf-8") as fh:
            fh.write("ok")
        self.assertEqual(self.read(os.path.join(self.base, "plain.txt")), "ok")
        with gblog._open_private(os.path.join(self.base, "plain.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")  # an existing regular file is still overwritten from the start
        self.assertEqual(self.read(os.path.join(self.base, "plain.txt")), "x")

    def test_report_does_not_write_through_a_hard_link(self):
        self.seed("ops", 1)
        victim = self.write(os.path.join(self.base, "victim.txt"), "precious\n")
        report = self.path("reports", DAY + ".md")
        self.hardlink(victim, report)
        rc, out, err = run(["report", "--date", DAY], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read(victim), "precious\n")
        self.assertEqual(self.read(report), out)
        self.assertEqual(os.stat(report).st_nlink, 1)
        self.assertEqual(os.listdir(self.path("reports")), [DAY + ".md"])

    def test_report_and_tune_fifo_do_not_hang(self):
        self.seed("ops", 1)
        report = self.path("reports", DAY + ".md")
        self.fifo(report)
        rc, out, err = run_cli(["report", "--date", DAY], self.root, timeout=60)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isfile(report))
        self.assertIn("# Activity report %s" % DAY, self.read(report))

    def test_symlinked_report_is_still_exit_2(self):
        self.seed("ops", 1)
        victim = os.path.join(self.base, "victim.md")
        self.symlink(victim, self.path("reports", DAY + ".md"))
        rc, out, err = run(["report", "--date", DAY], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(os.path.exists(victim))


# --------------------------------------------------------------------------- K2-06
class ReaderBoundsTest(Base):
    def test_last_line_matches_reference(self):
        rng = random.Random(7)
        path = os.path.join(self.base, "lines.bin")
        for _ in range(150):
            lines = [bytes(rng.choice(b"ab {}\t") for _ in range(rng.randrange(0, 40)))
                     for _ in range(rng.randrange(0, 6))]
            data = rng.choice([b"\n", b"\r\n"]).join(lines) + rng.choice([b"", b"\n", b"\n\n  \n", b" \t", b"\r\n"])
            with open(path, "wb") as fh:
                fh.write(data)
            want = data.rstrip().rsplit(b"\n", 1)[-1].strip()
            for block in (7, 65536):
                self.assertEqual(gblog._last_line(path, block=block), want, data)

    def test_long_garbage_tail_is_read_once_and_refused(self):
        self.seed("ops", 1)
        log = self.ledger.log_files("ops")[0]
        with open(log, "ab") as fh:
            fh.write(b"x" * (2 << 20))  # one long line without a newline
        self.assertEqual(len(gblog._last_line(log, block=4096)), 2 << 20)
        _path, tail = self.ledger._tail_record("ops")
        self.assertEqual(tail, {"_corrupt": True})
        before = os.path.getsize(log)
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("ops", {"type": "note", "detail": "refused", "ts": stamp(5)})
        self.assertEqual(os.path.getsize(log), before)

    def test_read_json_bounds_and_types(self):
        path = self.write(os.path.join(self.base, "side.json"), json.dumps({"seq": 1, "head": "genesis"}))
        self.assertEqual(gblog._read_json(path), {"seq": 1, "head": "genesis"})
        self.assertIsNone(gblog._read_json(path, limit=5))  # larger than the bound: counts as unreadable
        self.assertEqual(gblog._read_json(path, limit=None)["seq"], 1)
        self.assertIsNone(gblog._read_json(os.path.join(self.base, "missing.json")))
        self.assertIsNone(gblog._read_json(self.base))  # a directory
        with self.assertRaises(OSError):
            gblog._open_regular(self.base)
        fifo = os.path.join(self.base, "pipe.json")
        self.fifo(fifo)
        with mock.patch.object(gblog.os.path, "isfile", return_value=True):  # the entry "became" a FIFO after a check
            self.assertIsNone(gblog._read_json(fifo))
        with self.assertRaises(OSError):
            gblog._open_regular(fifo)


# --------------------------------------------------------------------------- K2-08, K2-10
class SideFileTest(Base):
    def test_directory_at_state_path_fails_verify_and_spools_instead_of_appending(self):
        self.seed("ops", 2)
        os.remove(self.path("state", "ops.json"))
        os.mkdir(self.path("state", "ops.json"))
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("state/ops.json: not a regular file (a directory; appends are refused)", res["problems"])
        before = self.log_bytes()
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("ops", {"type": "note", "detail": "kept in the spool", "ts": stamp(5)})
        self.assertEqual(self.log_bytes(), before)  # the chain does not grow one "error" at a time
        self.assertEqual(len(self.pending()), 1)
        rc, out, err = run(["note", "--bot", "ops", "--detail", "cli"], self.root)
        self.assertEqual(rc, 4, out + err)
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        os.rmdir(self.path("state", "ops.json"))
        rec, _err = self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "after the repair", "ts": stamp(6)})
        self.assertEqual(rec["seq"], 3)

    def test_non_regular_anchor_file_fails_verify(self):
        self.seed("ops", 1)
        os.mkdir(self.path("anchors", "ops.json"))
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertEqual(res["problems"], ["anchors/ops.json: not a regular file"])
        self.assertEqual(res["warnings"], [])
        os.mkdir(self.path("anchors", "ops.history.jsonl"))
        self.assertIn("anchors/ops.history.jsonl: not a regular file", self.ledger.verify("ops")["problems"])

    def test_refusal_survives_a_file_at_the_spool_directory(self):
        # K2-10: still exit 4, and the alert that `status` reads is written
        self.seed("ops", 1)
        self.break_state()
        self.write(self.path("spool", "ops"), "")
        rc, out, err = run(["note", "--bot", "ops", "--detail", "lost"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertIn("spool not written", err)
        self.assertTrue(os.path.isfile(self.path("state", "ops.alert.json")))
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ALERT ops: tail mismatch since", out)

    def test_non_regular_target_day_file_spools_the_event(self):
        # K2-10
        self.seed("ops", 1)
        planted = self.path("logs", "ops", "2024-06-02.jsonl")
        os.mkdir(planted)
        with self.assertRaises(gblog.TailMismatchError):
            self.ledger.append("ops", {"type": "note", "detail": "for tomorrow", "ts": "2024-06-02T10:00:00Z"})
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(self.chain()), 1)
        os.rmdir(planted)
        res, _err = self.quiet(self.ledger.spool_replay, "ops")
        self.assertEqual([r["detail"] for r in res["replayed"]], ["for tomorrow"])
        self.assertEqual(res["replayed"][0]["original_ts"], "2024-06-02T10:00:00Z")
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- K2-09, K2-11, K2-12
class MiscFilesystemTest(Base):
    def test_tune_does_not_hang_on_fifo_proposals(self):
        self.seed("ops", 1)
        path = self.path("tuning", "proposals.json")
        self.fifo(path)
        rc, out, err = run(["tune", "--apply", "abcd1234"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: tuning/proposals.json is corrupt", err)
        rc, out, err = run_cli(["tune"], self.root, timeout=60)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("notice: tuning/proposals.json was unreadable and has been replaced", err)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as fh:
            self.assertIn("proposals", json.load(fh))

    def test_missing_proposals_file_prints_no_notice(self):
        self.seed("ops", 1)
        rc, out, err = run(["tune"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("was unreadable", err)

    def test_non_regular_wal_side_file_is_not_copied(self):
        # K2-11
        prof = os.path.join(self.base, "chrome", "Default")
        os.makedirs(prof)
        hist = os.path.join(prof, "History")
        when = int((time.time() - 60 + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
        conn = sqlite3.connect(hist)
        try:
            conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                               "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
            conn.execute("INSERT INTO urls VALUES (1, 'https://a.example/x', 'A')")
            conn.execute("INSERT INTO visits VALUES (1, 1, ?)", (when,))
            conn.commit()
        finally:
            conn.close()
        os.mkdir(hist + "-wal")
        res, _err = self.quiet(gblog.capture_browser, self.ledger, "ops", [hist], 24)
        self.assertEqual((res["added"], res["failed"]), (1, []))

    OLD_BLOCK = b"# >>> gblog shell hook >>>\n__gblog_hook() { :; }\n# <<< gblog shell hook <<<\n"

    def test_hook_update_is_atomic(self):
        # K2-12
        rc_file = os.path.join(self.base, "bashrc")
        before = b"export A=1\n" + self.OLD_BLOCK + b"alias ll='ls -l'\n"
        with open(rc_file, "wb") as fh:
            fh.write(before)
        with mock.patch.object(gblog.os, "replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                gblog._install_shell_hook("ops", rc_file)
        with open(rc_file, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual([n for n in os.listdir(self.base) if n.endswith(".tmp")], [])
        self.assertEqual(gblog._install_shell_hook("ops", rc_file), "updated")
        with open(rc_file, "rb") as fh:
            self.assertEqual(fh.read(), b"export A=1\n" + gblog.shell_hook("ops").encode("utf-8") + b"alias ll='ls -l'\n")

    def test_hook_update_through_symlink_keeps_the_link(self):
        real_rc = os.path.join(self.base, "real_bashrc")
        with open(real_rc, "wb") as fh:
            fh.write(self.OLD_BLOCK)
        os.chmod(real_rc, 0o640)
        rc_file = os.path.join(self.base, "bashrc")
        self.symlink(real_rc, rc_file)
        self.assertEqual(gblog._install_shell_hook("ops", rc_file), "updated")
        self.assertTrue(os.path.islink(rc_file))
        with open(real_rc, "rb") as fh:
            self.assertEqual(fh.read(), gblog.shell_hook("ops").encode("utf-8"))
        if os.name == "posix":
            self.assertEqual(os.stat(real_rc).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
