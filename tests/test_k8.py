"""Tests for specialist review K8 (operator UX of failures): messages, remedies, exit paths and JSON shapes.

Each test names the finding it covers. Everything runs in temporary directories; no network is used
(git remotes are local bare repositories and the visibility lookup is replaced by a fake).
"""
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
FIXTURE = os.path.join(HERE, "fixtures", "kim-biseo-break")
MISSING = "ab" * 32


def git(*args):
    return subprocess.run(["git"] + list(args), check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-k8-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.fresh = os.path.join(self.base, "fresh")  # never created by setUp
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

    def tamper(self, bot="ops"):
        path = self.ledger.log_files(bot)[0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.write(path, text.replace("%s record 1" % bot, "%s record X" % bot))

    def failing_state_write(self):
        real, calls = gblog.Ledger.write_state, [0]

        def flaky(ledger, bot, state):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), ledger._state_path(bot))
            return real(ledger, bot, state)

        return mock.patch.object(gblog.Ledger, "write_state", flaky)


# --------------------------------------------------------------------------- K8-01
class TailMismatchCauseTest(Base):
    def refuse(self, bot, record=None):
        with self.assertRaises(gblog.TailMismatchError) as ctx, redirect_stderr(io.StringIO()):
            self.ledger.append(bot, record or {"type": "note", "detail": "refused"})
        exc = ctx.exception
        with open(exc.alert_path, encoding="utf-8") as fh:
            alert = json.load(fh)
        self.assertEqual(alert["cause"], exc.observed["cause"])
        self.assertEqual(alert["reason"], "file tail does not match state head/hash/seq; append refused")
        self.assertIn("; cause: ", str(exc))
        return exc.observed["cause"], str(exc)

    def test_each_cause_is_named(self):
        L = self.ledger
        self.seed("ahead", 2)
        L.write_state("ahead", {"seq": 99, "head": "cd" * 32})
        self.assertEqual(self.refuse("ahead")[0], "state_ahead")
        self.seed("behind", 3)
        L.write_state("behind", {"seq": 1, "head": MISSING})
        cause, text = self.refuse("behind")
        self.assertEqual(cause, "state_behind")
        self.assertIn("state is 2 record(s) behind the log", text)
        self.assertIn("gblog state rebuild --bot behind", text)
        self.seed("rewritten", 2)
        L.write_state("rewritten", {"seq": 2, "head": MISSING})
        self.assertEqual(self.refuse("rewritten")[0], "tail_rewritten")
        self.seed("torn", 2)
        with open(L.log_files("torn")[0], "ab") as fh:
            fh.write(b"{not json\n")
        cause, text = self.refuse("torn")
        self.assertEqual(cause, "tail_corrupt")
        self.assertIn("is not a valid record", text)
        self.seed("gone", 2)
        for path in L.log_files("gone"):
            os.remove(path)
        self.assertEqual(self.refuse("gone")[0], "logs_missing")
        self.seed("day", 1)
        os.mkdir(self.path("logs", "day", "2024-06-02.jsonl"))
        cause, text = self.refuse("day", {"type": "note", "detail": "x", "ts": "2024-06-02T10:00:00Z"})
        self.assertEqual(cause, "day_file_not_regular")
        self.assertIn("agree", text)

    def test_directory_at_state_path_is_named_not_called_genesis(self):
        self.seed("ops", 1)
        os.remove(self.path("state", "ops.json"))
        os.mkdir(self.path("state", "ops.json"))
        rc, out, err = run(["note", "--bot", "ops", "--detail", "b"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(len(err.splitlines()), 3, err)
        self.assertIn("cause: state/ops.json is a directory", err)
        self.assertIn("state -/directory", err)
        self.assertNotIn("genesis", err)
        with open(self.path("state", "ops.alert.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cause"], "state_not_regular")
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["bots"][0]["alert_cause"], "state_not_regular")
        self.assertIs(doc["ledger_exists"], True)

    def test_error_json_line_is_opt_in(self):
        # K8-10 (3)
        self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        with mock.patch.dict(os.environ, {"GBLOG_ERRORS": "json"}):
            rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.root)
        self.assertEqual(rc, 4, out + err)
        last = err.splitlines()[-1]
        self.assertTrue(last.startswith("gblog-error: "), err)
        doc = json.loads(last[len("gblog-error: "):])
        self.assertEqual((doc["exit"], doc["kind"], doc["bot"], doc["cause"]), (4, "tail_mismatch", "ops", "state_ahead"))


# --------------------------------------------------------------------------- K8-02
class SealMessageTest(Base):
    def setUp(self):
        super().setUp()
        self.prod = os.path.join(self.base, "prod")
        os.makedirs(self.prod)
        for name in ("logs", "state"):
            shutil.copytree(os.path.join(FIXTURE, name), os.path.join(self.prod, name))

    def test_second_seal_says_already_sealed(self):
        ledger = gblog.Ledger(self.prod)
        desc = ledger.verify("kim-biseo")["breaks"][0]
        self.quiet(ledger.seal_break, "kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
        with self.assertRaises(gblog.SealError) as ctx:
            ledger.seal_break("kim-biseo", desc, "TEST-APPROVAL-0001", copy_fixture=True)
        text = str(ctx.exception)
        self.assertIn("a break_ack is already present", text)
        self.assertIn("already sealed, jay_approval_id=TEST-APPROVAL-0001", text)
        self.assertNotIn("does not match", text)

    def test_mismatch_names_what_was_found(self):
        rc, out, err = run(["seal", "--bot", "kim-biseo", "--jay-approval-id", "JAY-2026-11-16-0042"], self.prod)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("on-disk", err)
        self.assertIn("1 break(s) found; first at logs/kim-biseo/2026-11-02.jsonl:165 seq 1919->1921", err)

    def test_empty_root_says_no_chain(self):
        rc, out, err = run(["seal", "--bot", "kim-biseo", "--jay-approval-id", "JAY-2026-11-16-0042"], self.fresh)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("on-disk", err)
        self.assertIn("no chain on disk for kim-biseo", err)
        self.assertFalse(os.path.exists(self.fresh))


# --------------------------------------------------------------------------- K8-03
class ContainsMissTest(Base):
    def setUp(self):
        super().setUp()
        self.seed("cos", 1)
        self.seed("cos-primary", 1)
        self.quiet(self.ledger.add_alias, "chief-of-staff", "Chief of Staff", ["cos", "cos-primary"])

    def test_miss_is_reported_on_the_searched_chain(self):
        rc, out, err = run(["verify", "--bot", "cos", "--contains", MISSING], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^FAIL\s+cos\s")
        self.assertRegex(out, r"(?m)^OK\s+cos-primary\s")
        self.assertIn("      - expected hash abababababab not found in chain", out.splitlines())
        self.assertIn("hint: %s was looked for in chain(s) cos;" % MISSING, err)

    def test_json_carries_the_full_hash_and_the_chains(self):
        rc, out, err = run(["verify", "--bot", "cos", "--contains", MISSING, "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        by = {e["bot"]: e for e in json.loads(out)}
        self.assertEqual((by["cos"]["contains_missing"], by["cos"]["contains_searched"]), ([MISSING], ["cos"]))
        self.assertIs(by["cos"]["ok"], False)
        self.assertIs(by["cos-primary"]["ok"], True)
        self.assertNotIn("contains_missing", by["cos-primary"])
        rc, out, err = run(["verify", "--bot", "chief-of-staff", "--contains", MISSING, "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        by = {e["bot"]: e for e in json.loads(out)}
        self.assertEqual(by["cos-primary"]["contains_searched"], ["cos", "cos-primary"])
        self.assertIs(by["cos"]["ok"], True)


# --------------------------------------------------------------------------- K8-04, K8-06, K8-07, K8-17 (sync)
@unittest.skipUnless(GIT, "git not installed")
class SyncMessageTest(Base):
    def setUp(self):
        super().setUp()
        self.answer = {"public": False, "reason": "test"}
        patcher = mock.patch.object(gblog, "remote_visibility", side_effect=lambda *a, **k: dict(self.answer))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seed("ops", 1)

    def bare(self, name="origin.git"):
        repo = os.path.join(self.base, name)
        git("init", "--bare", "-q", repo)
        return repo

    def test_rejected_push_does_not_relay_git_pull(self):
        bare = self.bare()
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        branch = git("-C", self.root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        other = os.path.join(self.base, "other")
        git("clone", "-q", "-b", branch, bare, other)
        git("-C", other, "-c", "user.name=x", "-c", "user.email=x@localhost", "commit", "-q", "--allow-empty", "-m", "other")
        git("-C", other, "push", "-q", "origin", "HEAD:" + branch)
        self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "later", "ts": stamp(5)})
        rc, out, err = run(["sync"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("(not pushed)", out)
        self.assertIn("push error: push rejected: origin/%s holds commits" % branch, out)
        self.assertIn("Do not pull", out)
        for word in ("git pull", "integrate"):
            self.assertNotIn(word, out + err)

    def test_other_push_failures_pass_through(self):
        bare = self.bare()
        real = gblog._git

        def failing(root, *args, **kwargs):
            if args and args[0] == "push":
                raise subprocess.CalledProcessError(128, ["git", "push"], output="",
                                                    stderr="fatal: Authentication failed for 'origin'")
            return real(root, *args, **kwargs)

        with mock.patch.object(gblog, "_git", failing):
            rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("push error: fatal: Authentication failed", out)
        self.assertNotIn("push rejected", out)

    def test_refusals_name_operator_decisions(self):
        # K8-06
        self.answer = {"public": True, "reason": "test"}
        rc, out, err = run(["sync", "--remote", "https://github.com/someone/public-repo.git"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("PUBLIC", out)
        self.assertIn("operator decision: --allow-public", out)
        self.assertNotIn("Use --allow-public", out)
        rc, out, err = run(["sync", "--no-push", "--remote", self.bare("elsewhere.git")], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: origin is already", err)
        self.assertIn("operator decision: --replace-remote", err)

    def test_unknown_visibility_hint_fits_the_reason(self):
        # K8-07
        self.answer = {"public": None, "reason": "github.com URL that cannot be mapped to a repository"}
        rc, out, err = run(["sync", "--remote", "https://gist.github.com/0123456789abcdef.git"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("hint: this URL form cannot be checked", err)
        self.assertNotIn("retry later", err)
        self.answer = {"public": None, "reason": "api.github.com HTTP 403"}
        rc, out, err = run(["sync"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("rate limit", err)
        self.assertIn("operator decision: --allow-unknown-visibility", err)

    def test_git_failure_names_the_subcommand_and_other_host_warning_names_the_url(self):
        # K8-17
        bare = self.bare()
        self.answer = {"public": None, "reason": "not a github.com remote; visibility unknown"}
        rc, out, err = run(["sync", "--remote", bare], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: could not confirm %s is private: it is not on GitHub" % bare, err)
        self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "later", "ts": stamp(5)})
        with mock.patch.object(gblog, "_sync_drift", return_value=["logs/ops/%s.jsonl" % DAY]):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("\n  error: committed content differs from disk", out)
        self.assertNotIn("push error", out)
        self.quiet(self.ledger.append, "ops", {"type": "note", "detail": "again", "ts": stamp(6)})
        real = gblog._git

        def failing(root, *args, **kwargs):
            if "commit" in args:
                raise subprocess.CalledProcessError(1, ["git", "-c", "a=b", "-C", root] + list(args), output="", stderr="boom")
            return real(root, *args, **kwargs)

        with mock.patch.object(gblog, "_git", failing):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("git failed (git commit, exit 1): boom", err)

    def test_missing_git_binary_is_named(self):
        empty = os.path.join(self.base, "empty-bin")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            rc, out, err = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: git was not found on PATH", err)


# --------------------------------------------------------------------------- K8-05, K8-09, K8-10
class WrittenOrNotTest(Base):
    def test_failed_state_write_says_the_record_is_in_the_log(self):
        self.seed("ops", 1)
        with self.failing_state_write():
            rc, out, err = run(["note", "--bot", "ops", "--detail", "two"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: " + os.strerror(errno.ENOSPC), err)
        self.assertIn("notice: the record IS in the log (ops #2 %s)" % self.chain()[-1]["hash"][:12], err)
        # K8-09: the same head-mismatch line, now with the benign cause named
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("      - state/ops.json: head does not match last record (state tampered or log truncated)",
                      out.splitlines())
        self.assertIn("hint: ops: an append was interrupted", err)
        self.assertIn("gblog state rebuild --bot ops", err)
        rc, out, err = run(["verify", "--bot", "ops", "--json"], self.root)
        entry = json.loads(out)[0]
        self.assertEqual(entry["kind"], "chain")
        self.assertTrue(entry["hints"] and all(isinstance(h, str) for h in entry["hints"]))
        self.assertNotIn("hint:", err)
        self.assertEqual(dict(self.ledger.verify("ops")), dict(self.ledger._verify_reference("ops")))

    def test_failed_fsync_says_the_record_may_be_in_the_log(self):
        self.seed("ops", 1)
        with mock.patch.object(gblog.os, "fsync", side_effect=OSError(errno.EIO, "Input/output error")):
            rc, out, err = run(["note", "--bot", "ops", "--detail", "two"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("notice: the record may be in the log; check `gblog tail --bot ops -n 1` before you retry", err)

    def test_truncated_log_gets_the_restore_hint_and_healthy_chains_none(self):
        self.seed("ops", 3)
        self.seed("fine", 1)
        path = self.ledger.log_files("ops")[0]
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.write(path, "".join(line + "\n" for line in lines[:-1]))
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("restore logs/ops/ from backup (state rebuild refuses)", err)
        self.assertNotIn("interrupted", err)
        rc, out, err = run(["verify", "--bot", "fine"], self.root)
        self.assertEqual((rc, err), (0, ""))
        rc, out, err = run(["verify", "--bot", "fine", "--json"], self.root)
        self.assertNotIn("hints", json.loads(out)[0])

    def test_verify_json_kinds(self):
        # K8-10
        self.seed("ops", 1)
        self.seed("gone", 1)
        self.quiet(self.ledger.anchor)
        shutil.rmtree(self.path("logs", "gone"))
        os.remove(self.path("state", "gone.json"))
        os.makedirs(self.path("logs", "my bot"))
        rc, out, err = run(["verify", "--all", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual({e["bot"]: e["kind"] for e in json.loads(out)},
                         {"ops": "chain", "gone": "missing_chain", "my bot": "invalid_dir"})
        rc, out, err = run(["verify", "--bot", "typo", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual([(e["bot"], e["kind"]) for e in json.loads(out)], [("typo", "unknown_name")])
        rc, out, err = run(["verify", "--all", "--contains", MISSING, "--json"], self.root)
        self.assertEqual((rc, out), (2, ""))  # a usage error leaves stdout empty


# --------------------------------------------------------------------------- K8-06, K8-16
class NameMessageTest(Base):
    def test_strict_refusal_does_not_claim_to_log(self):
        self.quiet(self.ledger.add_alias, "ops", "Ops", [])
        self.quiet(self.ledger.set_alias_strict, True)
        rc, out, err = run(["note", "--bot", "stranger", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: bot name 'stranger' is not registered in state/aliases.json; nothing was logged", err)
        self.assertNotIn("logging under", err)
        self.assertFalse(os.path.exists(self.path("logs", "stranger")))
        self.quiet(self.ledger.set_alias_strict, False)
        rc, out, err = run(["note", "--bot", "stranger", "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning: bot name 'stranger' is not registered in state/aliases.json (logging under 'stranger');", err)
        self.assertIn("ask the operator", err)

    def test_unregistered_non_ascii_name_on_read_commands(self):
        name = "\uae40\ube44\uc11c"
        self.seed("ops", 1)
        rc, out, err = run(["verify", "--bot", name], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("no chain on disk for '%s'" % name, out)
        rc, out, err = run(["anchor", "--bot", name], self.root)
        self.assertEqual(rc, 1, out + err)
        for argv in (["status"], ["tail"], ["export"], ["spool", "list"]):
            rc, out, err = run(argv + ["--bot", name], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("warning: no chain on disk for '%s'; nothing to show" % name, err)
        rc, out, err = run(["note", "--bot", name, "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("has no ASCII letters or digits", err)
        rc, out, err = run(["alias", "add", "--slug", "kim", "--display", name], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["note", "--bot", name, "--detail", "x"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["verify", "--bot", name], self.root)
        self.assertEqual(rc, 0, out + err)


# --------------------------------------------------------------------------- K8-08
class AnchorOutputTest(Base):
    def test_fail_line_is_followed_by_its_reasons(self):
        self.seed("ops", 2)
        self.tamper("ops")
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^ANCHOR FAIL ops\s")
        self.assertIn("      - logs/ops/%s.jsonl:1: hash mismatch (record edited)" % DAY, out.splitlines())

    def test_aborted_chain_names_the_file(self):
        self.seed("aaa", 1)
        self.seed("zzz", 1)
        os.mkdir(self.path("anchors", "aaa.history.jsonl"))
        rc, out, err = run(["anchor"], self.root)
        self.assertEqual(rc, 1, out + err)
        lines = out.splitlines()
        self.assertIn("      - anchor aborted: IsADirectoryError", lines)
        self.assertIn("      - anchors/aaa.history.jsonl: %s" % os.strerror(errno.EISDIR), lines)
        self.assertRegex(out, r"(?m)^ANCHOR OK\s+zzz\s")

    def test_unknown_bot_has_an_anchor_line(self):
        self.seed("ops", 1)
        rc, out, err = run(["anchor", "--bot", "nosuch"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(out.splitlines()[0].startswith("ANCHOR FAIL nosuch"), out)
        self.assertRegex(out, r"(?m)^FAIL\s+nosuch\s+0 records")
        self.assertEqual(os.listdir(self.path("anchors")), [])

    def test_anchor_json(self):
        recs = self.seed("ops", 2)
        rc, out, err = run(["anchor", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        doc = json.loads(out)
        self.assertEqual([(c["bot"], c["head"]) for c in doc["chains"]], [("ops", recs[-1]["hash"])])
        self.assertEqual(doc["missing"], [])
        self.tamper("ops")
        rc, out, err = run(["anchor", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIs(json.loads(out)["chains"][0]["ok"], False)
        rc, out, err = run(["anchor", "--bot", "nosuch", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(json.loads(out)["missing"][0]["bot"], "nosuch")


# --------------------------------------------------------------------------- K8-11
class MissingLedgerTest(Base):
    WARNING = "warning: no ledger at %s (logs/ does not exist)"

    def test_read_commands_say_that_the_ledger_is_missing(self):
        for argv in (["verify", "--all"], ["status"], ["tail"], ["report", "--stdout"]):
            rc, out, err = run(argv, self.fresh)
            self.assertEqual(rc, 0, out + err)
            self.assertIn(self.WARNING % self.fresh, err)
        rc, out, err = run(["status", "--json"], self.fresh)
        self.assertIs(json.loads(out)["ledger_exists"], False)
        self.assertFalse(os.path.exists(self.fresh))
        rc, out, err = run(["status"], self.root)
        self.assertNotIn("no ledger at", err)

    def test_require_ledger_refuses_to_create_one(self):
        with mock.patch.dict(os.environ, {"GBLOG_REQUIRE_LEDGER": "1"}):
            for argv in (["verify", "--all"], ["status"], ["anchor"], ["note", "--bot", "ops", "--detail", "x"]):
                rc, out, err = run(argv, self.fresh)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error: no ledger at %s; run `gblog init`" % self.fresh, err)
            self.assertFalse(os.path.exists(self.fresh))
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                gblog.Ledger(self.fresh).append("ops", {"type": "note", "detail": "library path"})
            self.assertFalse(os.path.exists(self.fresh))
            rc, out, err = run(["init"], self.fresh)
            self.assertEqual(rc, 0, out + err)
            rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.fresh)
            self.assertEqual(rc, 0, out + err)


# --------------------------------------------------------------------------- K8-12
class SpoolMessageTest(Base):
    def plant(self, name, text):
        return self.write(self.path("spool", "ops", name), text)

    def test_refused_event_is_not_called_unreadable(self):
        self.seed("ops", 1)
        planted = self.plant("20240601T110000Z-0000-deadbeef.json", json.dumps(
            {"v": 1, "kind": "tail_mismatch", "ts": "2024-06-01T11:00:00Z", "bot": "ops",
             "record": {"type": "note", "task_id": ["x"], "detail": "planted"}}))
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("event refused (record field 'task_id' must be a string", err)
        self.assertNotIn("is unreadable", err)
        self.assertIn('note: to dismiss a spool file without replaying it, see README "Upgrading", item 3', err)
        self.assertTrue(os.path.isfile(planted))
        os.remove(planted)
        self.plant("20240601T110000Z-0000-00000000.json", "{not json")
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is unreadable; left in place", err)

    def test_rebuild_names_the_next_step_and_refusal_names_the_remedy(self):
        recs = self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": recs[-1]["hash"]})
        with self.assertRaises(gblog.TailMismatchError) as ctx:
            self.ledger.append("ops", {"type": "note", "detail": "refused"})
        self.assertEqual(ctx.exception.observed["cause"], "state_seq_mismatch")
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("left in spool/ops; operator: gblog verify --bot ops, then gblog state rebuild --bot ops", err)
        rc, out, err = run(["state", "rebuild", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rebuilt ops seq 3", out)
        self.assertIn("note: 1 spooled event(s) are pending for ops; next: gblog spool replay --bot ops", err)
        rc, out, err = run(["spool", "replay", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_spool_list_warns_about_a_name_that_selects_nothing(self):
        self.seed("ops", 1)
        rc, out, err = run(["spool", "list", "--bot", "typo"], self.root)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("warning: no chain on disk for 'typo'; nothing to show", err)
        rc, out, err = run(["spool", "list", "--bot", "ops"], self.root)
        self.assertNotIn("no chain on disk", err)


# --------------------------------------------------------------------------- K8-13, K8-14
class SymlinkAndHookMessageTest(Base):
    TEXT = "is a symbolic link; gblog does not write through one"

    def symlink(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not supported here")

    def test_symlinked_report_is_named(self):
        self.seed("ops", 1)
        victim = os.path.join(self.base, "victim.md")
        self.symlink(victim, self.path("reports", DAY + ".md"))
        rc, out, err = run(["report", "--date", DAY], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(self.TEXT, err)
        self.assertNotIn("Too many levels", err)
        self.assertFalse(os.path.exists(victim))

    @unittest.skipUnless(getattr(os, "O_NOFOLLOW", 0), "O_NOFOLLOW is not available here")
    def test_symlinked_day_file_and_lock_file_are_named(self):
        victim = self.write(os.path.join(self.base, "victim.log"), "")
        os.makedirs(self.path("logs", "ops"))
        self.symlink(victim, self.path("logs", "ops", gblog.today() + ".jsonl"))
        rc, out, err = run(["note", "--bot", "ops", "--detail", "through the link"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(self.TEXT, err)
        self.assertIn("notice: nothing was written to the log and nothing was spooled", err)
        self.assertNotIn("Too many levels", err)
        self.assertEqual(os.path.getsize(victim), 0)
        self.symlink(victim, self.path("state", "web.lock"))
        rc, out, err = run(["note", "--bot", "web", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("web.lock " + self.TEXT, err)

    def test_hook_that_could_not_be_replaced_is_reported(self):
        rc_file = os.path.join(self.base, "bashrc")
        broken = "# >>> gblog shell hook >>>\n__gblog_hook() { :; }\nalias ll='ls -l'\n"  # no end marker
        self.write(rc_file, broken)
        argv = ["capture", "shell-hook", "--bot", "ops", "--install", "--rc", rc_file]
        rc, out, err = run(argv, self.root)
        self.assertEqual((rc, out), (0, "hook already present in %s\n" % rc_file))
        self.assertIn("warning: %s holds a gblog hook block that is not the current hook" % rc_file, err)
        with open(rc_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), broken)
        self.write(rc_file, "# >>> gblog shell hook >>>\n__gblog_hook() { :; }\n# <<< gblog shell hook <<<\n")
        rc, out, err = run(argv, self.root)
        self.assertEqual((rc, out), (0, "updated hook in %s\n" % rc_file))
        self.assertNotIn("warning", err)
        rc, out, err = run(argv, self.root)
        self.assertEqual((rc, out), (0, "hook already present in %s\n" % rc_file))
        self.assertNotIn("warning", err)


if __name__ == "__main__":
    unittest.main()
