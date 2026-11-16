"""The example scripts under examples/ (routine-daily-audit.sh, agent-task.sh).

Both scripts run against a temporary GBLOG_ROOT with the gblog.py of this checkout. No network
is used: the only real push goes to a local bare repository, and the exit-code mapping is
checked with a stand-in for gblog that exits with a configured code.
"""
import datetime as _dt
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import gblog  # noqa: E402

BASH = shutil.which("bash")
GIT = shutil.which("git")
SHELLCHECK = shutil.which("shellcheck")
GBLOG_PY = os.path.join(REPO, "gblog.py")
AUDIT = os.path.join(REPO, "examples", "routine-daily-audit.sh")
TASK = os.path.join(REPO, "examples", "agent-task.sh")
PW = "hunter2" + "hunter2"

# A stand-in for gblog: it records its arguments and exits with the code configured for the subcommand.
STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
case "$1" in
  task-start) echo stubtask01; exit "${STUB_RC_TASK_START:-0}" ;;
  log) exit "${STUB_RC_LOG:-0}" ;;
  task-end) exit "${STUB_RC_TASK_END:-0}" ;;
  verify) exit "${STUB_RC_VERIFY:-0}" ;;
  anchor) exit "${STUB_RC_ANCHOR:-0}" ;;
  report) exit "${STUB_RC_REPORT:-0}" ;;
  sync) exit "${STUB_RC_SYNC:-0}" ;;
esac
exit 0
'''


def utc_yesterday():
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")


@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class ExamplesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gblog-examples-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "ledger")  # created by the first write, not here
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.home)
        os.makedirs(self.cwd)
        # the real CLI of this checkout, run with the interpreter that runs the tests
        self.gblog_bin = self.script("gblog", "#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(GBLOG_PY)))
        self.ledger = gblog.Ledger(self.root)

    # -- helpers
    def script(self, name, text):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)
        return path

    def stub(self):
        return self.script("stub-gblog", STUB)

    def env(self, **extra):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("GBLOG_") or key.startswith("STUB_"):
                del env[key]
        env.update({
            "HOME": self.home,
            "GBLOG_ROOT": self.root,
            "GBLOG_BIN": self.gblog_bin,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        })
        env.update(extra)
        return env

    def sh(self, argv, env=None):
        proc = subprocess.run(argv, cwd=self.cwd, env=env or self.env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def audit(self, **extra):
        return self.sh([BASH, AUDIT], self.env(**extra))

    def task(self, args, **extra):
        return self.sh([BASH, TASK] + list(args), self.env(**extra))

    def seed(self, bot="ops", n=2, day="2024-06-01"):
        with redirect_stderr(io.StringIO()):
            return [self.ledger.append(bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1),
                                             "ts": "%sT10:00:%02dZ" % (day, i)}) for i in range(n)]

    def chain(self, bot):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def break_state(self, bot="ops"):
        with redirect_stderr(io.StringIO()):
            self.ledger.write_state(bot, {"seq": 99, "head": "cd" * 32})

    def ledger_bytes(self):
        blob = b""
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    blob += fh.read()
        return blob

    def calls(self, log):
        if not os.path.exists(log):
            return []
        with open(log, encoding="utf-8") as fh:
            return fh.read().splitlines()


class ScriptLintTest(ExamplesBase):
    def test_bash_syntax(self):
        for path in (AUDIT, TASK):
            with self.subTest(script=os.path.basename(path)):
                rc, out, err = self.sh([BASH, "-n", path])
                self.assertEqual(rc, 0, out + err)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.readline().rstrip("\n"), "#!/usr/bin/env bash")

    @unittest.skipUnless(SHELLCHECK, "shellcheck not installed")
    def test_shellcheck_clean(self):
        rc, out, err = self.sh([SHELLCHECK, AUDIT, TASK])
        self.assertEqual(rc, 0, out + err)


# --------------------------------------------------------------------------- routine-daily-audit.sh
class DailyAuditTest(ExamplesBase):
    def test_missing_ledger_is_reported_and_not_created(self):
        rc, out, err = self.audit()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("ledger missing", err)
        self.assertFalse(os.path.exists(self.root), "the audit created an empty ledger")

    def test_healthy_run_verifies_anchors_and_reports(self):
        day_before = utc_yesterday()
        self.seed("ops", 2, day=day_before)
        rc, out, err = self.audit()
        day_after = utc_yesterday()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RESULT: ok", out)
        recs = self.chain("ops")
        self.assertEqual([r["type"] for r in recs], ["note", "note", "verify"])
        self.assertEqual(recs[-1]["outcome"], "ok")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "anchors", "ops.json")))
        # the full head is printed for an off-box copy
        lines = out.splitlines()
        self.assertTrue(any(line.startswith("Chain heads ") for line in lines), out)
        self.assertIn("ops OK %s" % recs[-1]["hash"], lines)
        # yesterday's report (UTC) was written, and not printed
        reports = sorted(os.listdir(os.path.join(self.root, "reports")))
        self.assertEqual(len(reports), 1, reports)
        self.assertIn(reports[0], (day_before + ".md", day_after + ".md"))
        if day_before == day_after:
            with open(os.path.join(self.root, "reports", reports[0]), encoding="utf-8") as fh:
                self.assertIn("ops record 1", fh.read())
        self.assertNotIn("ops record 1", out)
        # no remote configured: no sync, no git repository
        self.assertIn("sync skipped", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git")))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_tampered_chain_exits_1_and_other_chains_are_still_anchored(self):
        self.seed("ops", 2)
        self.seed("keeper", 1)
        path = self.ledger.log_files("ops")[0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("ops record 1", text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("ops record 1", "ops record X"))
        rc, out, err = self.audit()
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL", out)
        self.assertIn("integrity FAIL", out)
        self.assertIn("RESULT: FAIL", out)
        lines = out.splitlines()
        self.assertIn("ops FAIL (no head)", lines)
        self.assertIn("keeper OK %s" % self.chain("keeper")[-1]["hash"], lines)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "anchors", "keeper.json")))
        self.assertEqual(len(os.listdir(os.path.join(self.root, "reports"))), 1)

    def test_tail_mismatch_exits_4_and_lists_the_spool(self):
        self.seed("ops", 2)
        self.break_state("ops")
        rc, out, err = self.audit()
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertIn("ALERT ops: tail mismatch since", out)
        self.assertIn("gblog spool list", out)
        self.assertIn("RESULT: ALERT", out)
        spooled = [f for f in os.listdir(os.path.join(self.root, "spool", "ops")) if f.endswith(".json")]
        self.assertEqual(len(spooled), 1, spooled)
        self.assertEqual(len(self.chain("ops")), 2)  # the refused record is not in the log
        self.assertTrue(os.path.isfile(os.path.join(self.root, "anchors", "ops.json")))  # the run went on

    def test_audit_bot_wraps_the_run_in_a_routine_task(self):
        self.seed("ops", 1)
        rc, out, err = self.audit(GBLOG_AUDIT_BOT="ledger")
        self.assertEqual(rc, 0, out + err)
        recs = self.chain("ledger")
        self.assertEqual([r["type"] for r in recs], ["task_start", "verify", "task_end"])
        self.assertEqual((recs[0]["task"], recs[0].get("source")), ("Daily ledger audit", "routine"))
        self.assertEqual(recs[2]["task_id"], recs[0]["task_id"])
        self.assertEqual(recs[2]["outcome"], "ok")
        self.assertIn("verify=0", recs[2]["detail"])

    # -- sync against a local bare repository (no network)
    def _git(self, *args):
        return subprocess.run([GIT] + list(args), env=self.env(), check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.decode("utf-8", "replace")

    def _bare(self, name):
        path = os.path.join(self.tmp, name)
        self._git("init", "--bare", "-q", path)
        return path

    def _commits(self, repo):
        proc = subprocess.run([GIT, "-C", repo, "rev-list", "--all", "--count"], env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return int(proc.stdout.decode().strip() or 0) if proc.returncode == 0 else 0

    @unittest.skipUnless(GIT, "git not installed")
    def test_sync_goes_only_to_the_configured_remote(self):
        bare = self._bare("backup.git")
        self.seed("ops", 2)
        rc, out, err = self.audit(GBLOG_AUDIT_REMOTE=bare, GBLOG_AUDIT_NO_PUSH="1")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._commits(self.root), 1)
        self.assertEqual(self._commits(bare), 0)  # --no-push: committed locally only
        # the same remote on the next night: pushed
        rc, out, err = self.audit(GBLOG_AUDIT_REMOTE=bare)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(" pushed", out)
        self.assertEqual(self._commits(bare), self._commits(self.root))
        self.assertEqual(self._commits(bare), 2)
        self.assertEqual(self._git("-C", self.root, "remote", "get-url", "origin").strip(), bare)

    @unittest.skipUnless(GIT, "git not installed")
    def test_a_different_remote_is_an_error_and_origin_is_kept(self):
        first, second = self._bare("first.git"), self._bare("second.git")
        self.seed("ops", 1)
        rc, out, err = self.audit(GBLOG_AUDIT_REMOTE=first, GBLOG_AUDIT_NO_PUSH="1")
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.audit(GBLOG_AUDIT_REMOTE=second, GBLOG_AUDIT_NO_PUSH="1")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("origin is already", err)
        self.assertIn("RESULT: ERROR", out)
        self.assertEqual(self._git("-C", self.root, "remote", "get-url", "origin").strip(), first)
        self.assertEqual(self._commits(second), 0)

    # -- exit-code mapping, with a stand-in for gblog
    def test_exit_code_mapping(self):
        os.makedirs(os.path.join(self.root, "logs"))
        stub = self.stub()
        log = os.path.join(self.tmp, "stub.log")
        url = "https://deploy:s3cretTok3n@git.example.invalid/private-ledger.git"
        steps = ["verify --all --record", "anchor", "report --date yesterday",
                 "sync --remote=%s -m daily ledger sync" % url, "status"]
        cases = (  # verify, anchor, report, sync -> script
            (0, 0, 0, 0, 0),
            (1, 1, 0, 0, 1),
            (0, 0, 0, 1, 1),
            (0, 0, 0, 3, 3),
            (0, 0, 0, 2, 2),
            (0, 0, 0, 4, 4),
            (1, 1, 0, 3, 1),
            (1, 1, 0, 4, 4),
            (2, 0, 0, 0, 2),
            (0, 0, 2, 3, 3),
            (0, 0, 2, 1, 2),
            (0, 0, 7, 0, 2),
        )
        for verify, anchor, report, sync, expected in cases:
            with self.subTest(verify=verify, anchor=anchor, report=report, sync=sync):
                if os.path.exists(log):
                    os.remove(log)
                rc, out, err = self.audit(GBLOG_BIN=stub, STUB_LOG=log, GBLOG_AUDIT_REMOTE=url,
                                          STUB_RC_VERIFY=str(verify), STUB_RC_ANCHOR=str(anchor),
                                          STUB_RC_REPORT=str(report), STUB_RC_SYNC=str(sync))
                self.assertEqual(rc, expected, out + err)
                calls = self.calls(log)
                # every step runs whatever the earlier ones returned
                self.assertEqual(calls[:5], steps)
                self.assertEqual(calls[5:], ["spool list"] if expected == 4 else [])
                self.assertNotIn("s3cretTok3n", out + err)  # the script never prints the remote URL
                for flag in ("--allow", "--replace-remote"):
                    self.assertNotIn(flag, "\n".join(calls))
                self.assertIn("(no chains anchored)", out)

    def test_no_remote_means_no_sync_call(self):
        os.makedirs(os.path.join(self.root, "logs"))
        log = os.path.join(self.tmp, "stub.log")
        rc, out, err = self.audit(GBLOG_BIN=self.stub(), STUB_LOG=log)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.calls(log), ["verify --all --record", "anchor", "report --date yesterday", "status"])

    def test_missing_gblog_is_an_environment_error(self):
        os.makedirs(os.path.join(self.root, "logs"))
        rc, out, err = self.audit(GBLOG_BIN=os.path.join(self.tmp, "no-such-gblog"))
        self.assertEqual(rc, 2, out + err)
        self.assertIn("gblog not found", err)


# --------------------------------------------------------------------------- agent-task.sh
class AgentTaskTest(ExamplesBase):
    def test_success_is_one_closed_task(self):
        rc, out, err = self.task(["--bot", "ops", "--task", "Say hello", "--", "sh", "-c", "echo hello"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out, "hello\n")  # the wrapper adds nothing to the command's stdout
        recs = self.chain("ops")
        self.assertEqual([r["type"] for r in recs], ["task_start", "action", "task_end"])
        tid = recs[0]["task_id"]
        self.assertEqual([r.get("task_id") for r in recs], [tid, tid, tid])
        self.assertEqual(recs[0]["task"], "Say hello")
        self.assertNotIn("source", recs[0])
        self.assertEqual((recs[1]["action"], recs[1]["target"], recs[1]["outcome"]), ("shell", "sh", "ok"))
        self.assertTrue(recs[1]["detail"].startswith("exit 0 after "), recs[1]["detail"])
        self.assertEqual(recs[2]["outcome"], "ok")
        self.assertIn("exit 0", recs[2]["detail"])
        self.assertIn(tid, err)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_failure_status_is_logged_and_returned(self):
        rc, out, err = self.task(["--bot", "ops", "--", "sh", "-c", "exit 3"])
        self.assertEqual(rc, 3, out + err)
        recs = self.chain("ops")
        self.assertEqual([r["type"] for r in recs], ["task_start", "action", "task_end"])
        self.assertEqual(recs[0]["task"], "Run sh")
        self.assertEqual(recs[1]["outcome"], "fail")
        self.assertTrue(recs[1]["detail"].startswith("exit 3 after "), recs[1]["detail"])
        self.assertEqual(recs[2]["outcome"], "fail")
        # a command that does not exist is a failed task, not a wrapper error
        rc, out, err = self.task(["--bot", "ops", "--", "definitely-not-a-command-xyz"])
        self.assertEqual(rc, 127, out + err)
        recs = self.chain("ops")[3:]
        self.assertEqual([r["type"] for r in recs], ["task_start", "action", "task_end"])
        self.assertEqual((recs[1]["target"], recs[1]["outcome"]), ("definitely-not-a-command-xyz", "fail"))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_arguments_and_environment_are_never_logged(self):
        secret = "Zq9" + "TOPSECRET" + "x7"
        rc, out, err = self.task(["--bot", "ops", "--task", "Deploy DB_PASSWORD=" + PW, "--",
                                  "sh", "-c", 'printf "%s" "$1"', "sh", "--db-pass " + secret],
                                 SECRET_ENV=secret)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out, "--db-pass " + secret)  # the command still gets its argument unchanged
        blob = self.ledger_bytes()
        self.assertNotIn(secret.encode("utf-8"), blob)
        self.assertNotIn(PW.encode("utf-8"), blob)
        self.assertNotIn(secret, err)
        recs = self.chain("ops")
        self.assertIn("[REDACTED]", recs[0]["task"])
        self.assertEqual(recs[1]["target"], "sh")

        # a whole command line handed over as one word is not a program name: nothing of it is logged
        rc, out, err = self.task(["--bot", "ops", "--", "mysql -p" + secret])
        self.assertEqual(rc, 127, out + err)
        recs = self.chain("ops")[3:]
        self.assertEqual(recs[0]["task"], "Run command")
        self.assertEqual(recs[1]["target"], "command")
        self.assertNotIn(secret.encode("utf-8"), self.ledger_bytes())

    def test_quoting_is_safe(self):
        title = '-x "quoted" $(touch pwned) `touch pwned2`; rm -rf *'
        rc, out, err = self.task(["--bot", "ops", "--task", title, "--target", "--force",
                                  "--summary", "-1 rows; it's done", "--",
                                  "sh", "-c", 'printf "%s|" "$@"', "sh", "a b", 'c"d', "$HOME", "*"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out, 'a b|c"d|$HOME|*|')
        recs = self.chain("ops")
        self.assertEqual(recs[0]["task"], title)
        self.assertEqual(recs[1]["target"], "--force")
        self.assertEqual(recs[2]["detail"], "-1 rows; it's done")
        self.assertEqual(os.listdir(self.cwd), [])  # nothing in the title was executed

    def test_usage_errors_run_nothing(self):
        marker = ["sh", "-c", "touch ran"]
        cases = (
            ["--bot", "ops"] + marker,                                   # no --
            ["--bot", "ops", "--"],                                      # no command
            ["--"] + marker,                                             # no bot
            ["--bot", "ops", "--action", "bogus", "--"] + marker,        # unknown action kind
            ["--bot"],                                                   # option without a value
            ["--bot", "ops", "--no-such-option", "--"] + marker,
        )
        for args in cases:
            with self.subTest(args=args):
                rc, out, err = self.task(args)
                self.assertEqual(rc, 125, out + err)
                self.assertIn("agent-task:", err)
                self.assertEqual(out, "")
                self.assertEqual(os.listdir(self.cwd), [])
                self.assertFalse(os.path.exists(self.root))
        rc, out, err = self.task(["--help"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("usage:", out)

    def test_command_is_not_run_when_task_start_is_refused(self):
        self.seed("ops", 2)
        self.break_state("ops")
        rc, out, err = self.task(["--bot", "ops", "--", "sh", "-c", "touch ran"])
        self.assertEqual(rc, 125, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertIn("task-start exited 4", err)
        self.assertIn("NOT run", err)
        self.assertEqual(os.listdir(self.cwd), [])
        self.assertEqual(len(self.chain("ops")), 2)

    def test_routine_flag_env_bot_and_task_id_for_the_command(self):
        rc, out, err = self.task(["--routine", "--action", "other", "--", "sh", "-c", 'printf "%s" "$GBLOG_TASK"'],
                                 GBLOG_BOT="nightly")
        self.assertEqual(rc, 0, out + err)
        recs = self.chain("nightly")
        self.assertEqual([r["type"] for r in recs], ["task_start", "action", "task_end"])
        self.assertEqual(out, recs[0]["task_id"])
        self.assertEqual(recs[0].get("source"), "routine")
        self.assertEqual(recs[1]["action"], "other")
        self.assertNotIn("source", recs[1])

    def test_logging_failure_is_exit_125(self):
        stub = self.stub()
        log = os.path.join(self.tmp, "stub.log")
        cases = (
            ({}, "exit 0", 0),
            ({}, "exit 3", 3),
            ({"STUB_RC_LOG": "4"}, "exit 0", 125),
            ({"STUB_RC_TASK_END": "2"}, "exit 3", 125),
        )
        for extra, command, expected in cases:
            with self.subTest(extra=extra, command=command):
                if os.path.exists(log):
                    os.remove(log)
                rc, out, err = self.task(["--bot", "ops", "--", "sh", "-c", command],
                                         GBLOG_BIN=stub, STUB_LOG=log, **extra)
                self.assertEqual(rc, expected, out + err)
                # task-end is attempted even when log was refused
                self.assertEqual([line.split()[0] for line in self.calls(log)], ["task-start", "log", "task-end"])
                if expected == 125:
                    self.assertIn("logging failed", err)
                    self.assertIn("the command exited %s" % command.split()[1], err)
        # a refused task-start: nothing else is called and the command does not run
        os.remove(log)
        rc, out, err = self.task(["--bot", "ops", "--", "sh", "-c", "touch ran"],
                                 GBLOG_BIN=stub, STUB_LOG=log, STUB_RC_TASK_START="2")
        self.assertEqual(rc, 125, out + err)
        self.assertEqual([line.split()[0] for line in self.calls(log)], ["task-start"])
        self.assertEqual(os.listdir(self.cwd), [])


if __name__ == "__main__":
    unittest.main()
