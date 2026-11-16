"""CLI behaviour for agents and operators (audit item I-26).

Covers the cases named in the audit for:

  I-03  ``report --date`` must not fall back to a Bot's whole history
  I-06  one resolver for ``--bot`` (verify, anchor, export)
  I-13  agent ergonomics (--summary, $GBLOG_TASK, --outcome synonyms, --source, status --bot,
        --root after the subcommand, tail -n 0, tune --apply/--reject, verify --json)
  I-14  status counts (today, --chains, open tasks over 30 days)
  I-19  alias registry (slug beats alias, --move, reserved names)
  I-20  archive CLI (unreadable --file, ``scanned: false``)
  I-26  the exit-code table (0, 1, 2, 3, 4)

Every test is written to the audit text. Where gblog.py does not do what the audit says, the
test is marked ``@unittest.expectedFailure`` with a ``deviation:`` comment. (The one
deviation found in the first pass, reserved names on append, was fixed by round-2 R-02.)

Everything runs in temporary directories. No network is used: the one ``sync`` call has
``remote_visibility`` mocked and is refused before any push.
"""
import datetime as _dt
import gzip
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

GIT = shutil.which("git")
sys.path.insert(0, HERE)
from _support import GIT_ENV, run_raw, stamp  # noqa: E402,F401  (shared helpers, round-3 W-20)
from _support import run_strict as run  # noqa: E402  (K6-01: a swallowed crash fails the test)
DAY = "2024-06-01"
# every chain of the Bot registered by CliBase.seed_variants()
VARIANTS = ("ChiefOfStaff", "chief-of-staff", "cos", "cos-primary")


def days_ago(days, hour=9, minute=0):
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    when = (now - _dt.timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def chain_statuses(out):
    """{chain: "OK" | "FAIL"} from `gblog verify` text output."""
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("OK", "FAIL"):
            found[parts[1]] = parts[0]
    return found


class CliBase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="gblog-cli-ux-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")  # created by the first write, not here
        self.work = os.path.join(self.base, "work")
        os.makedirs(self.work)
        self.ledger = gblog.Ledger(self.root)
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT"):
            os.environ.pop(key, None)

    # -- helpers
    def chain(self, bot):
        """Records of exactly one chain directory, in file order (no alias merging)."""
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def last(self, bot="ops"):
        return self.chain(bot)[-1]

    def tree(self):
        found = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                found.append(os.path.relpath(os.path.join(dirpath, name), self.root))
        return sorted(found)

    def status_rows(self, *extra):
        rc, out, err = run(["status", "--json"] + list(extra), self.root)
        self.assertEqual(rc, 0, out + err)
        return {row["bot"]: row for row in json.loads(out)["bots"]}

    def seed_variants(self):
        """One Bot with four chains on disk (slug + three historical variants) and one unrelated Bot."""
        L = self.ledger
        L.append("cos", {"type": "note", "detail": "cos one", "ts": stamp(0)})
        L.append("cos", {"type": "note", "detail": "cos two", "ts": stamp(1)})
        L.append("ChiefOfStaff", {"type": "note", "detail": "camel one", "ts": stamp(2)})
        L.append("cos-primary", {"type": "note", "detail": "primary one", "ts": stamp(3)})
        L.append("cos-primary", {"type": "note", "detail": "primary two", "ts": stamp(4)})
        L.append("chief-of-staff", {"type": "note", "detail": "slug one", "ts": stamp(5)})
        L.append("ops", {"type": "note", "detail": "other bot", "ts": stamp(6)})
        L.add_alias("chief-of-staff", "Chief of Staff", ["cos", "ChiefOfStaff", "cos-primary"])

    def tamper(self, bot, old, new):
        path = self.ledger.log_files(bot)[0]
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old, new))


# --------------------------------------------------------------------------- I-03
class ReportDateTest(CliBase):
    def _seed(self):
        L = self.ledger
        # idle: history on two earlier days, nothing on 2024-06-03
        L.append("idle", {"type": "task_start", "task_id": "i1", "task": "Idle bot old task", "ts": "2024-06-01T09:00:00Z"})
        L.append("idle", {"type": "action", "task_id": "i1", "action": "shell", "target": "idle-old-command",
                          "outcome": "fail", "detail": "exit 1", "ts": "2024-06-01T09:01:00Z"})
        L.append("idle", {"type": "task_end", "task_id": "i1", "outcome": "fail", "ts": "2024-06-02T09:00:00Z"})
        # busy: one record the day before, three on the day
        L.append("busy", {"type": "note", "detail": "busy day before", "ts": "2024-06-02T23:59:59Z"})
        L.append("busy", {"type": "task_start", "task_id": "b1", "task": "Busy bot task", "ts": "2024-06-03T09:00:00Z"})
        L.append("busy", {"type": "action", "task_id": "b1", "action": "shell", "target": "busy-command",
                          "outcome": "ok", "ts": "2024-06-03T09:01:00Z"})
        L.append("busy", {"type": "task_end", "task_id": "b1", "outcome": "ok", "ts": "2024-06-03T09:02:00Z"})

    def test_report_date_excludes_idle_bot_history(self):
        self._seed()
        L = self.ledger
        # the library: an explicit empty file list means "no files", not "all files"
        self.assertEqual(list(L.iter_records("idle", [])), [])
        self.assertEqual(len(list(L.iter_records("idle"))), 3)
        self.assertEqual(L.records("idle", date="2024-06-03"), [])
        self.assertEqual([r["bot"] for r in L.records(date="2024-06-03")], ["busy", "busy", "busy"])

        rc, out, err = run(["report", "--date", "2024-06-03", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("| Bots | 1 |", out)
        self.assertIn("| Records | 3 |", out)
        self.assertIn("| Failed | 0 |", out)
        self.assertIn("## busy", out)
        self.assertIn("### Busy bot task", out)
        self.assertNotIn("## idle", out)
        self.assertNotIn("Idle bot old task", out)
        self.assertNotIn("idle-old-command", out)
        self.assertNotIn("busy day before", out)

        # asking for the idle Bot on that day gives an empty report, not its history
        rc, out, err = run(["report", "--bot", "idle", "--date", "2024-06-03", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("_No records._", out)
        self.assertNotIn("Idle bot old task", out)

        # a day on which both Bots wrote still shows both
        rc, out, err = run(["report", "--date", "2024-06-02", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("| Bots | 2 |", out)
        self.assertIn("| Records | 2 |", out)

        # the written report file is the same text
        rc, out, err = run(["report", "--date", "2024-06-03"], self.root)
        self.assertEqual(rc, 0, out + err)
        with open(os.path.join(self.root, "reports", "2024-06-03.md"), encoding="utf-8") as fh:
            written = fh.read()
        self.assertEqual(written, out)
        self.assertNotIn("Idle bot old task", written)

    def test_report_date_with_no_records_anywhere(self):
        # a ledger that does not exist yet: an empty report, and --stdout creates nothing
        rc, out, err = run(["report", "--date", "2024-06-03", "--stdout"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("# Activity report 2024-06-03", out)
        self.assertIn("_No records._", out)
        self.assertNotIn("| Records |", out)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.root))

        # records exist, but none on the requested day
        self._seed()
        for extra in ([], ["--bot", "busy"], ["--bot", "idle"]):
            with self.subTest(extra=extra):
                rc, out, err = run(["report", "--date", "2024-06-05", "--stdout"] + extra, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("_No records._", out)
                self.assertNotIn("| Records |", out)
                self.assertNotIn("Busy bot task", out)
                self.assertNotIn("Idle bot old task", out)
        rc, out, err = run(["report", "--date", "2024-06-05"], self.root)
        self.assertEqual(rc, 0, out + err)
        with open(os.path.join(self.root, "reports", "2024-06-05.md"), encoding="utf-8") as fh:
            self.assertIn("_No records._", fh.read())


# --------------------------------------------------------------------------- I-06
class BotResolverTest(CliBase):
    def test_verify_unknown_bot_fails_and_creates_nothing(self):
        # on a ledger that does not exist at all
        for argv in (["verify", "--bot", "nosuchbot"], ["verify", "--bot", "nosuchbot", "--record"]):
            with self.subTest(argv=argv, ledger="missing"):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 1, out + err)
                self.assertRegex(out, r"(?m)^FAIL\s+nosuchbot\s+0 records")
                self.assertIn("- no chain on disk for 'nosuchbot'", out)
                self.assertNotIn("head genesis", out)
                self.assertFalse(out.startswith("OK"), out)
                self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.root))

        # on a real ledger: a typo, an unregistered display name; with and without --record
        self.ledger.append("ops", {"type": "note", "detail": "real chain", "ts": stamp(0)})
        before = self.tree()
        for name in ("nosuchbot", "opps", "Ops Team"):
            for extra in ([], ["--record"]):
                with self.subTest(name=name, extra=extra):
                    rc, out, err = run(["verify", "--bot", name] + extra, self.root)
                    self.assertEqual(rc, 1, out + err)
                    self.assertRegex(out, r"(?m)^FAIL\s+%s\s+0 records" % re.escape(name))
                    self.assertIn("- no chain on disk for '%s'" % name, out)
                    self.assertEqual(chain_statuses(out).get("ops"), None)
        # no junk chain, no state file, no lock file, no verify record
        self.assertEqual(self.tree(), before)
        self.assertEqual(len(self.chain("ops")), 1)
        # an exact chain name behaves as before
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(chain_statuses(out), {"ops": "OK"})

    def test_verify_display_name_checks_all_variant_chains(self):
        self.seed_variants()
        want = {c: "OK" for c in VARIANTS}
        for name in ("Chief of Staff", "chief-of-staff", "cos", "ChiefOfStaff", "cos-primary", "COS"):
            with self.subTest(name=name):
                rc, out, err = run(["verify", "--bot", name], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(chain_statuses(out), want)
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(chain_statuses(out), {"ops": "OK"})

        # an edit in one legacy variant chain is found through the display name
        self.tamper("cos-primary", "primary one", "primary 0ne")
        want["cos-primary"] = "FAIL"
        for name in ("Chief of Staff", "chief-of-staff", "cos"):
            with self.subTest(name=name, tampered=True):
                rc, out, err = run(["verify", "--bot", name], self.root)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(chain_statuses(out), want)
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_export_display_name(self):
        self.seed_variants()
        rc, base, err = run(["export", "--bot", "chief-of-staff"], self.root)
        self.assertEqual(rc, 0, err)
        recs = [json.loads(line) for line in base.splitlines() if line.strip()]
        self.assertEqual(len(recs), 6)
        self.assertEqual({r["bot"] for r in recs}, set(VARIANTS))
        self.assertNotIn("other bot", base)
        for name in ("Chief of Staff", "cos", "ChiefOfStaff", "cos-primary"):
            with self.subTest(name=name):
                rc, out, err = run(["export", "--bot", name], self.root)
                self.assertEqual(rc, 0, err)
                self.assertEqual(out, base)
        rc, out, err = run(["export"], self.root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out.splitlines()), 7)
        rc, out, err = run(["export", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, err)
        self.assertEqual([json.loads(line)["detail"] for line in out.splitlines()], ["other bot"])
        # an unknown name exports nothing and creates nothing
        rc, out, err = run(["export", "--bot", "nosuchbot"], self.root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "nosuchbot")))

    def test_anchor_unknown_bot_writes_no_file(self):
        # a ledger that does not exist is not created by a typo
        rc, out, err = run(["anchor", "--bot", "nosuchbot"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(os.path.exists(self.root))

        self.ledger.append("ops", {"type": "note", "detail": "real chain", "ts": stamp(0)})
        anchors = os.path.join(self.root, "anchors")
        rc, out, err = run(["anchor", "--bot", "nosuchbot"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertRegex(out, r"(?m)^FAIL\s+nosuchbot\s+0 records")
        self.assertIn("- no chain on disk for 'nosuchbot'", out)
        self.assertNotIn("Traceback", err)
        # no anchors/<typo>.json, no history file, no day rollup
        self.assertEqual(os.listdir(anchors), [])

        rc, out, err = run(["anchor", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ANCHOR OK", out)
        self.assertTrue(os.path.isfile(os.path.join(anchors, "ops.json")))
        rc, out, err = run(["anchor", "--bot", "nosuchbot"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(sorted(n for n in os.listdir(anchors) if "nosuchbot" in n), [])
        days = os.path.join(anchors, "days")
        for name in os.listdir(days):
            with open(os.path.join(days, name), encoding="utf-8") as fh:
                self.assertEqual([c["bot"] for c in json.load(fh)["chains"]], ["ops"])

    def test_anchor_display_name_anchors_every_variant_chain(self):
        self.seed_variants()
        rc, out, err = run(["anchor", "--bot", "Chief of Staff"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("ANCHOR OK")), len(VARIANTS))
        anchors = os.path.join(self.root, "anchors")
        for chain in VARIANTS:
            self.assertTrue(os.path.isfile(os.path.join(anchors, chain + ".json")), chain)
        self.assertFalse(os.path.exists(os.path.join(anchors, "ops.json")))
        self.assertIn("chief-of-staff.json", os.listdir(anchors))
        self.assertNotIn("Chief-of-Staff.json", os.listdir(anchors))


# --------------------------------------------------------------------------- I-13
class AgentErgonomicsTest(CliBase):
    def test_task_end_accepts_summary(self):
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "demo", "--summary", "kickoff"], self.root)
        self.assertEqual(rc, 0, err)
        tid = tid.strip()
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", tid, "--outcome", "ok", "--summary", "report sent"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.endswith("task_end ok\n"), out)
        self.assertNotIn("warning", err)
        recs = self.chain("ops")
        self.assertEqual(recs[0]["detail"], "kickoff")
        self.assertEqual((recs[-1]["type"], recs[-1]["task_id"], recs[-1]["detail"]), ("task_end", tid, "report sent"))
        self.assertEqual(self.status_rows()["ops"]["open_tasks"], 0)

        # --summary is the same field as --detail on every logging command
        for argv, detail in (
            (["log", "--bot", "ops", "--action", "shell", "--target", "ls", "--summary", "ran it"], "ran it"),
            (["note", "--bot", "ops", "--summary", "why"], "why"),
            (["approval", "--bot", "ops", "--target", "send mail", "--decision", "allowed", "--summary", "asked"], "asked"),
            (["note", "--bot", "ops", "--detail", "classic flag"], "classic flag"),
        ):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.last("ops")["detail"], detail)
        # ... and on tune: the flag is accepted, the refusal is about the missing proposals
        rc, out, err = run(["tune", "--apply", "abcd1234", "--summary", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertNotIn("unrecognized arguments", err)
        self.assertIn("no proposals yet", err)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_env_task_fallback_note_approval_task_end(self):
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "env task"], self.root)
        self.assertEqual(rc, 0, err)
        tid = tid.strip()
        with mock.patch.dict(os.environ, {"GBLOG_TASK": tid}):
            rc, out, err = run(["note", "--bot", "ops", "--detail", "from env"], self.root)
            self.assertEqual(rc, 0, out + err)
            rc, out, err = run(["approval", "--bot", "ops", "--target", "send mail", "--decision", "allowed"], self.root)
            self.assertEqual(rc, 0, out + err)
            rc, out, err = run(["task-end", "--bot", "ops", "--outcome", "ok"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("warning", err)
        recs = self.chain("ops")[1:]
        self.assertEqual([r["type"] for r in recs], ["note", "approval", "task_end"])
        self.assertEqual([r.get("task_id") for r in recs], [tid, tid, tid])
        self.assertEqual(recs[1]["decision"], "allowed")
        self.assertEqual(self.status_rows()["ops"]["open_tasks"], 0)

        # an explicit --task-id beats the environment
        with mock.patch.dict(os.environ, {"GBLOG_TASK": "envtask"}):
            rc, out, err = run(["note", "--bot", "ops", "--task-id", "explicit1", "--detail", "explicit"], self.root)
            self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.last("ops")["task_id"], "explicit1")
        # neither flag nor environment: no task id is invented
        rc, out, err = run(["note", "--bot", "ops", "--detail", "no task"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("task_id", self.last("ops"))

    def test_task_end_without_id_warns(self):
        rc, out, err = run(["task-end", "--bot", "ops", "--outcome", "ok", "--detail", "orphan"], self.root)
        self.assertEqual(rc, 0, out + err)  # still recorded
        self.assertIn("warning: task-end without --task-id and $GBLOG_TASK unset closes no task", err)
        rec = self.last("ops")
        self.assertEqual((rec["type"], rec["detail"]), ("task_end", "orphan"))
        self.assertNotIn("task_id", rec)

        # an id with no task_start in the last 30 days: recorded, with a warning
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", "ghost123", "--outcome", "ok"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning", err)
        self.assertIn("ghost123", err)
        self.assertIn("no task-start", err)
        self.assertEqual(self.last("ops")["task_id"], "ghost123")

        # a proper pair is silent
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "paired"], self.root)
        self.assertEqual(rc, 0, err)
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", tid.strip(), "--outcome", "ok"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("warning", err)
        self.assertEqual(len(self.chain("ops")), 4)

    def test_outcome_synonyms(self):
        synonyms = {
            "success": "ok", "succeeded": "ok", "done": "ok", "complete": "ok", "completed": "ok",
            "pass": "ok", "passed": "ok",
            "failed": "fail", "failure": "fail", "error": "fail",
            "cancelled": "skipped", "canceled": "skipped", "skip": "skipped",
            "blocked": "denied",
        }
        expected = []
        for word in sorted(synonyms):
            rc, out, err = run(["log", "--bot", "ops", "--action", "shell", "--target", "cmd-" + word, "--outcome", word], self.root)
            self.assertEqual(rc, 0, "%s: %s%s" % (word, out, err))
            expected.append(("cmd-" + word, synonyms[word]))
        for value in ("ok", "fail", "denied", "pending", "skipped", "n/a"):
            rc, out, err = run(["log", "--bot", "ops", "--action", "shell", "--target", "canon-" + value, "--outcome", value], self.root)
            self.assertEqual(rc, 0, "%s: %s%s" % (value, out, err))
            expected.append(("canon-" + value, value))
        self.assertEqual([(r["target"], r["outcome"]) for r in self.chain("ops")], expected)

        # task-end takes the same synonyms and prints the canonical value
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", "t1", "--outcome", "done"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.endswith("task_end ok\n"), out)
        self.assertEqual(self.last("ops")["outcome"], "ok")

        # anything else is still a usage error and writes nothing
        count = len(self.chain("ops"))
        rc, out, err = run(["log", "--bot", "ops", "--action", "shell", "--target", "x", "--outcome", "banana"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("invalid choice", err)
        self.assertEqual(len(self.chain("ops")), count)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_source_routine_on_task_start(self):
        rc, tid, err = run(["task-start", "--bot", "ops", "--task", "Daily ledger audit", "--source", "routine"], self.root)
        self.assertEqual(rc, 0, err)
        tid = tid.strip()
        rec = self.last("ops")
        self.assertEqual((rec["type"], rec["task"], rec.get("source")), ("task_start", "Daily ledger audit", "routine"))

        for argv, kind, source in (
            (["note", "--bot", "ops", "--task-id", tid, "--detail", "n", "--source", "routine"], "note", "routine"),
            (["approval", "--bot", "ops", "--task-id", tid, "--target", "push", "--decision", "auto", "--source", "auto"], "approval", "auto"),
            (["task-end", "--bot", "ops", "--task-id", tid, "--outcome", "ok", "--source", "routine"], "task_end", "routine"),
        ):
            with self.subTest(kind=kind):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                rec = self.last("ops")
                self.assertEqual((rec["type"], rec.get("source")), (kind, source))

        # `self` is the default and is not stored; so is no flag at all
        for extra in (["--source", "self"], []):
            with self.subTest(extra=extra):
                rc, out, err = run(["task-start", "--bot", "ops", "--task", "Manual task"] + extra, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("source", self.last("ops"))

        count = len(self.chain("ops"))
        rc, out, err = run(["task-start", "--bot", "ops", "--task", "x", "--source", "bogus"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(len(self.chain("ops")), count)

    def test_status_bot_filter(self):
        L = self.ledger
        L.append("alpha", {"type": "note", "detail": "a1", "ts": stamp(0)})
        L.append("alpha", {"type": "note", "detail": "a2", "ts": stamp(1)})
        L.append("bravo", {"type": "note", "detail": "b1", "ts": stamp(2)})
        self.assertEqual(sorted(self.status_rows()), ["alpha", "bravo"])
        rows = self.status_rows("--bot", "alpha")
        self.assertEqual(sorted(rows), ["alpha"])
        self.assertEqual(rows["alpha"]["seq"], 2)

        rc, out, err = run(["status", "--bot", "bravo"], self.root)
        self.assertEqual(rc, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[1].split()[0], "bot")
        names = [line.split()[0] for line in lines[2:] if line.strip() and not line.startswith("ALERT")]
        self.assertEqual(names, ["bravo"])

        # an unknown name filters everything out; it is not an error
        self.assertEqual(self.status_rows("--bot", "nosuchbot"), {})

        # the filter resolves display names and aliases like the write commands do
        L.add_alias("alpha-team", "Alpha Team", ["alpha"])
        for name in ("Alpha Team", "alpha-team", "alpha"):
            with self.subTest(name=name):
                rows = self.status_rows("--bot", name)
                self.assertEqual(sorted(rows), ["alpha-team"])
                self.assertEqual(rows["alpha-team"]["chains"], ["alpha"])
                self.assertEqual(sorted(self.status_rows("--chains", "--bot", name)), ["alpha"])

    def test_root_after_subcommand(self):
        decoy = os.path.join(self.base, "decoy")
        absolute = os.path.abspath(self.root)
        with mock.patch.dict(os.environ, {"GBLOG_ROOT": decoy}):
            rc, out, err = run_raw(["note", "--bot", "ops", "--detail", "root after", "--root", self.root])
            self.assertEqual(rc, 0, out + err)

            rc, out, err = run_raw(["status", "--json", "--root", self.root])
            self.assertEqual(rc, 0, out + err)
            after = json.loads(out)
            self.assertEqual(after["root"], absolute)
            self.assertEqual([row["bot"] for row in after["bots"]], ["ops"])
            # ... the same answer as the classic position
            rc, out, err = run_raw(["--root", self.root, "status", "--json"])
            self.assertEqual(rc, 0, out + err)
            before = json.loads(out)
            self.assertEqual(before["root"], absolute)
            self.assertEqual([row["bot"] for row in before["bots"]], ["ops"])

            rc, out, err = run_raw(["verify", "--bot", "ops", "--root", self.root])
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(chain_statuses(out), {"ops": "OK"})
            rc, out, err = run_raw(["tail", "-n", "1", "--root", self.root])
            self.assertEqual(rc, 0, out + err)
            self.assertIn("root after", out)
            # nested subcommands accept it too
            rc, out, err = run_raw(["alias", "resolve", "ops", "--root", self.root])
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(out, "ops\n")
            rc, out, err = run_raw(["spool", "list", "--root", self.root])
            self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(decoy), "the environment root was used although --root was given")
        self.assertEqual(len(self.chain("ops")), 1)

    def test_tail_n_zero(self):
        for i, text in enumerate(("one", "two", "three")):
            self.ledger.append("ops", {"type": "note", "detail": text, "ts": stamp(i)})
        for argv in (["tail", "-n", "0"], ["tail", "--bot", "ops", "-n", "0"], ["tail", "-n", "-1"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(out, "")
        rc, out, err = run(["tail", "-n", "2"], self.root)
        self.assertEqual(rc, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("two", lines[0])
        self.assertIn("three", lines[1])
        rc, out, err = run(["tail"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(out.splitlines()), 3)

    def test_tune_apply_reject_exclusive(self):
        L = self.ledger
        for d in (6, 5, 4):
            tid = "t%d" % d
            L.append("ops", {"type": "task_start", "task_id": tid, "task": "Reconcile invoices %d" % d, "ts": days_ago(d)})
            L.append("ops", {"type": "action", "task_id": tid, "action": "connector", "target": "quickbooks.sync",
                             "outcome": "fail", "detail": "401", "ts": days_ago(d, 9, 5)})
            L.append("ops", {"type": "task_end", "task_id": tid, "outcome": "fail", "ts": days_ago(d, 9, 30)})
        rc, out, err = run(["tune", "--days", "28"], self.root)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.root, "tuning", "proposals.json"), encoding="utf-8") as fh:
            proposals = json.load(fh)["proposals"]
        skill = [p for p in proposals if p["category"] == "skill"][0]["id"]
        routine = [p for p in proposals if p["category"] == "routine"][0]["id"]
        self.assertNotEqual(skill, routine)

        before = len(self.chain("ops"))
        for argv in (["tune", "--apply", skill, "--reject", routine, "--detail", "both"],
                     ["tune", "--reject", skill, "--apply", routine]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("not allowed with", err)
                self.assertNotIn("Traceback", err)
        # neither id was recorded, in particular not "X as rejected"
        self.assertEqual(len(self.chain("ops")), before)
        self.assertFalse(any(r.get("type") == "tune" for r in self.chain("ops")))

        # each flag on its own still works
        rc, out, err = run(["tune", "--apply", skill, "--detail", "added re-auth step"], self.root)
        self.assertEqual(rc, 0, out + err)
        rec = self.last("ops")
        self.assertEqual((rec["type"], rec["target"], rec["outcome"]), ("tune", skill, "ok"))
        rc, out, err = run(["tune", "--reject", routine, "--summary", "declined"], self.root)
        self.assertEqual(rc, 0, out + err)
        rec = self.last("ops")
        self.assertEqual((rec["type"], rec["target"], rec["outcome"], rec["detail"]), ("tune", routine, "skipped", "declined"))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_verify_json(self):
        L = self.ledger
        L.append("alpha", {"type": "note", "detail": "alpha one", "ts": stamp(0)})
        L.append("alpha", {"type": "note", "detail": "alpha two", "ts": stamp(1)})
        L.append("bravo", {"type": "note", "detail": "bravo one", "ts": stamp(2)})

        rc, out, err = run(["verify", "--all", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)  # the whole of stdout is one JSON document
        self.assertIsInstance(data, list)
        self.assertEqual([d["bot"] for d in data], ["alpha", "bravo"])
        for d in data:
            self.assertTrue({"bot", "ok", "records", "head", "problems", "warnings"} <= set(d), sorted(d))
            self.assertIs(d["ok"], True)
            self.assertEqual(d["problems"], [])
            self.assertEqual(d["warnings"], [])
            self.assertEqual(d["head"], L.read_state(d["bot"])["head"])
        self.assertEqual([d["records"] for d in data], [2, 1])

        rc, out, err = run(["verify", "--bot", "alpha", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([d["bot"] for d in json.loads(out)], ["alpha"])

        # exit codes are the same as in text mode
        self.tamper("bravo", "bravo one", "bravo 0ne")
        rc_text, _out, _err = run(["verify", "--all"], self.root)
        rc, out, err = run(["verify", "--all", "--json"], self.root)
        self.assertEqual((rc, rc_text), (1, 1), out + err)
        by = {d["bot"]: d for d in json.loads(out)}
        self.assertIs(by["alpha"]["ok"], True)
        self.assertIs(by["bravo"]["ok"], False)
        self.assertTrue(any("hash mismatch" in p for p in by["bravo"]["problems"]), by["bravo"]["problems"])

        # a --contains miss is part of the JSON result
        rc, out, err = run(["verify", "--bot", "alpha", "--json", "--contains", "ab" * 32], self.root)
        self.assertEqual(rc, 1, out + err)
        entry = json.loads(out)[0]
        self.assertIs(entry["ok"], False)
        self.assertIn("expected hash abababababab not found in chain", entry["problems"])

        # an unknown bot is still exit 1 and still a JSON list
        rc, out, err = run(["verify", "--bot", "nosuchbot", "--json"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        entries = json.loads(out)
        self.assertIsInstance(entries, list)
        self.assertEqual(len(entries), 1, entries)
        entry = entries[0]
        self.assertEqual(entry["bot"], "nosuchbot")
        self.assertIs(entry["ok"], False)
        self.assertEqual(entry["records"], 0)
        self.assertIsNone(entry["head"])
        self.assertIs(entry["missing"], True)
        self.assertEqual(entry["problems"], ["no chain on disk for 'nosuchbot'"])
        self.assertEqual(entry["warnings"], [])


# --------------------------------------------------------------------------- N-13 / R-16 (round-3 W-18)
import subprocess  # noqa: E402  (only the sync abbreviation test needs it)


class AbbreviationTest(CliBase):
    """Prefix abbreviations of flags: the ambiguous ones exit 2 and write nothing; the documented short forms still work."""

    def test_abbreviations(self):
        elsewhere = os.path.join(self.base, "elsewhere")
        cases = [
            ["verify", "--bot", "ops", "--r"],                                   # --record / --root
            ["sync", "--no-push", "--r", elsewhere],                             # --remote / --replace-remote / --root
            ["sync", "--no-push", "--re", elsewhere],                            # --remote / --replace-remote
            ["sync", "--no-push", "--a"],                                        # --allow-public / --allow-unknown-visibility
            ["sync", "--no-push", "--al"],
            ["sync", "--no-push", "--allow"],
            ["tune", "--r", "abcd1234"],                                         # --reject / --root
            ["tune", "--s"],                                                     # --stdout / --summary
            ["capture", "shell-hook", "--bot", "ops", "--r", elsewhere],         # --rc / --root
            ["archive", "--bot", "ops", "--s"],                                  # --stdin / --source / --summary
            ["log", "--bot", "ops", "--action", "shell", "--target", "ls", "--s", "auto"],  # --source / --summary
        ]
        rc, helptext, err = run(["capture", "files", "--help"], self.root)
        self.assertEqual(rc, 0, helptext + err)
        if "--record-baseline" in helptext:
            # once `capture files` has --record-baseline, --r no longer means --root there
            cases.append(["capture", "files", "--bot", "ops", "--path", self.work, "--r", elsewhere])
        for argv in cases:
            with self.subTest(ambiguous=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("ambiguous option", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "")
        self.assertFalse(os.path.exists(self.root), "an ambiguous flag must not create the ledger")
        self.assertFalse(os.path.exists(elsewhere))

        # the documented short forms still parse as the flag they abbreviate
        self.ledger.append("ops", {"type": "note", "detail": "one", "ts": stamp(0)})
        rc, out, err = run(["verify", "--bot", "ops", "--rec"], self.root)  # --record
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((self.last("ops")["type"], self.last("ops")["target"]), ("verify", "chain"))

        rc, out, err = run(["log", "--bot", "ops", "--action", "shell", "--target", "ls", "--so", "auto"], self.root)
        self.assertEqual(rc, 0, out + err)  # --source
        self.assertEqual((self.last("ops")["type"], self.last("ops").get("source")), ("action", "auto"))

        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(b"evidence from stdin\n"))):
            rc, out, err = run(["archive", "--bot", "ops", "--std", "--name", "piped"], self.root)  # --stdin
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((self.last("ops")["type"], self.last("ops")["target"]), ("archive", "piped"))

        rc, out, err = run(["tune", "--days", "28", "--std"], self.root)  # --stdout
        self.assertEqual(rc, 0, out + err)
        self.assertIn("# Tuning proposals", out)
        self.assertEqual(os.listdir(os.path.join(self.root, "tuning")), [])  # print only: nothing was written

        rc, out, err = run(["tune", "--rej", "abcd1234", "--detail", "declined"], self.root)  # --reject
        self.assertEqual(rc, 2, out + err)  # parsed; the refusal is about the missing proposals
        self.assertNotIn("ambiguous option", err)
        self.assertNotIn("unrecognized arguments", err)
        self.assertIn("no proposals yet", err)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])

    @unittest.skipUnless(GIT, "git not installed")
    def test_abbreviations_sync_forms_still_accepted(self):
        self.ledger.append("ops", {"type": "note", "detail": "something to back up", "ts": stamp(0)})
        bare = os.path.join(self.base, "backup.git")
        subprocess.run([GIT, "init", "--bare", "-q", bare], check=True, capture_output=True)

        rc, out, err = run(["sync", "--no-push", "--rem", bare], self.root)  # --remote
        self.assertEqual(rc, 0, out + err)
        origin = subprocess.run([GIT, "-C", self.root, "remote", "get-url", "origin"], check=True,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(origin, bare)

        # --allow-p is --allow-public: a remote reported public is pushed to (a local bare repository, no network)
        with mock.patch.object(gblog, "remote_visibility", return_value={"public": True, "reason": "test"}) as vis:
            rc, out, err = run(["sync", "--allow-p"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(vis.called)
        self.assertNotIn("refused", out + err)
        self.assertNotIn("(not pushed)", out)
        self.assertIn(" pushed", out)
        pushed = subprocess.run([GIT, "--git-dir", bare, "rev-list", "--all", "--count"], check=True,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(pushed, "2")


# --------------------------------------------------------------------------- I-14
class StatusCountsTest(CliBase):
    def setUp(self):
        super().setUp()
        # Freeze gblog's idea of "now" (appends and today()) so the `today` column cannot
        # straddle UTC midnight. status computes its 30-day window from the real clock,
        # which is at most a few seconds later.
        self.now = gblog.now_iso()
        frozen = mock.patch.object(gblog, "now_iso", return_value=self.now)
        frozen.start()
        self.addCleanup(frozen.stop)

    def ago(self, days, minutes=0):
        base = _dt.datetime.strptime(self.now, "%Y-%m-%dT%H:%M:%SZ")
        return (base - _dt.timedelta(days=days) + _dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _seed_alias_chains(self):
        L = self.ledger
        L.append("cos", {"type": "note", "detail": "old cos", "ts": self.ago(3)})
        L.append("cos", {"type": "task_start", "task_id": "open1", "task": "Still open"})
        L.append("cos", {"type": "note", "detail": "cos today"})
        L.append("ChiefOfStaff", {"type": "note", "detail": "camel today"})
        L.append("cos-primary", {"type": "note", "detail": "old primary", "ts": self.ago(3)})
        L.append("cos-primary", {"type": "note", "detail": "primary today"})
        L.append("ops", {"type": "note", "detail": "ops today"})
        L.add_alias("chief-of-staff", "Chief of Staff", ["cos", "ChiefOfStaff", "cos-primary"])

    def test_status_today_not_multiplied_by_alias_chains(self):
        self._seed_alias_chains()
        rows = self.status_rows()
        self.assertEqual(sorted(rows), ["chief-of-staff", "ops"])
        merged = rows["chief-of-staff"]
        self.assertEqual(merged["chains"], ["ChiefOfStaff", "cos", "cos-primary"])
        self.assertEqual(merged["seq"], 6)
        self.assertEqual(merged["today"], 4)  # 2 + 1 + 1, each record once (not 12)
        self.assertEqual(merged["open_tasks"], 1)
        self.assertEqual((rows["ops"]["seq"], rows["ops"]["today"], rows["ops"]["open_tasks"]), (1, 1, 0))

        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        # label, chains, seq, today, open
        self.assertRegex(out, r"(?m)^Chief of Staff\(chief-of-staff\)\s+3\s+6\s+4\s+1\s")

    def test_status_chains_is_per_chain(self):
        self._seed_alias_chains()
        rows = self.status_rows("--chains")
        self.assertEqual(sorted(rows), ["ChiefOfStaff", "cos", "cos-primary", "ops"])
        expected = {  # chain: (seq, today, open tasks)
            "cos": (3, 2, 1),
            "ChiefOfStaff": (1, 1, 0),
            "cos-primary": (2, 1, 0),
            "ops": (1, 1, 0),
        }
        for chain, want in expected.items():
            with self.subTest(chain=chain):
                row = rows[chain]
                self.assertEqual((row["seq"], row["today"], row["open_tasks"]), want)
                self.assertEqual(row["chains"], [chain])
                self.assertEqual(row["label"], chain)
        # the per-chain numbers add up to the merged row
        merged = self.status_rows()["chief-of-staff"]
        variants = ("cos", "ChiefOfStaff", "cos-primary")
        self.assertEqual(sum(rows[c]["today"] for c in variants), merged["today"])
        self.assertEqual(sum(rows[c]["seq"] for c in variants), merged["seq"])

    def test_open_task_older_than_7_days_still_open(self):
        L = self.ledger
        L.append("ops", {"type": "task_start", "task_id": "t25", "task": "Long running", "ts": self.ago(25)})
        L.append("ops", {"type": "task_start", "task_id": "t10", "task": "Forgotten", "ts": self.ago(10)})
        L.append("ops", {"type": "task_start", "task_id": "t10b", "task": "Closed the same day", "ts": self.ago(10, 1)})
        L.append("ops", {"type": "task_end", "task_id": "t10b", "outcome": "ok", "ts": self.ago(10, 2)})
        L.append("ops", {"type": "task_start", "task_id": "t8", "task": "Closed a week later", "ts": self.ago(8)})
        L.append("ops", {"type": "task_end", "task_id": "t8", "outcome": "ok", "ts": self.ago(1)})
        row = self.status_rows()["ops"]
        self.assertEqual(row["open_tasks"], 2)  # t25 and t10: both older than 7 days, both still open
        self.assertEqual(row["today"], 0)
        self.assertEqual(row["seq"], 6)

        # closing the forgotten one late is still possible, without a warning, and is counted
        rc, out, err = run(["task-end", "--bot", "ops", "--task-id", "t10", "--outcome", "skipped", "--detail", "closed late"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("warning", err)
        row = self.status_rows()["ops"]
        self.assertEqual(row["open_tasks"], 1)
        self.assertEqual(row["today"], 1)


# --------------------------------------------------------------------------- I-19
class AliasRegistryTest(CliBase):
    RESERVED_NAMES = ("aliases", "Aliases", "capture-files-ops", "ops.alert", "ops.lock", "ops.tmp")

    def _write_registry(self, pairs):
        """Write state/aliases.json by hand so the entry order in the file is exactly ``pairs``."""
        self.ledger.init()
        with open(self.ledger.aliases_path(), "w", encoding="utf-8") as fh:
            json.dump({"strict": False, "bots": dict(pairs)}, fh)

    def test_slug_beats_alias_regardless_of_order(self):
        # a registry left by an older version: `holder` claims another Bot's slug and display name as aliases
        holder = ("holder", {"display": "Holder", "aliases": ["mmm", "Shared Name"]})
        slug = ("mmm", {"display": "Shared Name", "aliases": []})
        for label, pairs in (("alias holder first", (holder, slug)), ("slug first", (slug, holder))):
            with self.subTest(order=label):
                self._write_registry(pairs)
                self.assertEqual(list(self.ledger.aliases()["bots"]), [p[0] for p in pairs])
                self.assertEqual(self.ledger.resolve("mmm"), ("mmm", True))
                self.assertEqual(self.ledger.resolve("MMM"), ("mmm", True))
                # a display name beats an alias as well
                self.assertEqual(self.ledger.resolve("Shared Name"), ("mmm", True))
                self.assertEqual(self.ledger.resolve("Holder"), ("holder", True))
                self.assertEqual(self.ledger.resolve("holder"), ("holder", True))
                self.assertEqual(self.ledger.label("mmm"), "Shared Name(mmm)")
                rc, out, err = run(["alias", "resolve", "mmm"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(out, "mmm\n")
                rc, out, err = run(["alias", "list"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("slug 'mmm' is also an alias of holder", out)

        # writes follow the same rule: they land in the slug's own chain
        for name in ("mmm", "Shared Name"):
            rc, out, err = run(["note", "--bot", name, "--detail", "to the slug"], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("warning", err)
        self.assertEqual([r["bot"] for r in self.chain("mmm")], ["mmm", "mmm"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "holder")))

    def test_alias_steal_requires_move(self):
        rc, out, err = run(["alias", "add", "--slug", "one", "--display", "Bot One", "--alias", "legacy"], self.root)
        self.assertEqual(rc, 0, out + err)
        before = self.ledger.aliases()

        rc, out, err = run(["alias", "add", "--slug", "two", "--alias", "legacy"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: 'legacy' is an alias of one; use --move", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.ledger.aliases(), before)  # nothing moved, `two` was not created
        self.assertEqual(self.ledger.resolve("legacy"), ("one", True))

        rc, out, err = run(["alias", "add", "--slug", "two", "--alias", "legacy", "--move"], self.root)
        self.assertEqual(rc, 0, out + err)
        bots = self.ledger.aliases()["bots"]
        self.assertEqual(bots["two"]["aliases"], ["legacy"])
        self.assertNotIn("legacy", bots["one"]["aliases"])
        self.assertEqual(bots["one"]["display"], "Bot One")
        self.assertEqual(self.ledger.resolve("legacy"), ("two", True))

        # re-adding a Bot's own alias is not a steal
        rc, out, err = run(["alias", "add", "--slug", "two", "--alias", "legacy"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.ledger.aliases()["bots"]["two"]["aliases"], ["legacy"])

        # the check ignores letter case, like name resolution does (round-2 R-10): `Legacy` is two's `legacy`
        before = self.ledger.aliases()
        self.assertEqual(self.ledger.resolve("Legacy"), ("two", True))
        rc, out, err = run(["alias", "add", "--slug", "three", "--alias", "Legacy"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: 'Legacy' is an alias of two; use --move", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.ledger.aliases(), before)  # nothing moved, `three` was not created

        rc, out, err = run(["alias", "add", "--slug", "three", "--alias", "Legacy", "--move"], self.root)
        self.assertEqual(rc, 0, out + err)
        bots = self.ledger.aliases()["bots"]
        self.assertEqual(bots["three"]["aliases"], ["Legacy"])
        self.assertEqual(bots["two"]["aliases"], [])  # the case variant was taken away from the other Bot
        for name in ("legacy", "Legacy", "LEGACY"):
            self.assertEqual(self.ledger.resolve(name), ("three", True), name)

    def test_strict_with_empty_registry_prints_note(self):
        rc, out, err = run(["alias", "strict", "on"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("strict on", out)
        self.assertIn("note: strict has no effect until a Bot is registered", err)
        rc, out, err = run(["note", "--bot", "anyone", "--detail", "still logged"], self.root)
        self.assertEqual(rc, 0, out + err)

        rc, out, err = run(["alias", "add", "--slug", "known-bot"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["alias", "strict", "on"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("note:", err)
        rc, out, err = run(["note", "--bot", "stranger", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("not registered", err)
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "stranger")))

    def test_reserved_bot_names_refused_for_new_chains(self):
        # the registry path: `alias add` never registers a name that collides with gblog's own state files
        for name in self.RESERVED_NAMES:
            with self.subTest(name=name):
                rc, out, err = run(["alias", "add", "--slug", name, "--display", "Some Bot"], self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error:", err)
                self.assertIn("reserved", err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn(name.lower(), self.ledger.aliases()["bots"])
        self.assertFalse(os.path.exists(self.root), "a refused name must not create the ledger")
        # near misses are ordinary names
        for name in ("alias", "capture-files", "alert", "ops-alert", "tmp", "lock"):
            with self.subTest(name=name):
                rc, out, err = run(["alias", "add", "--slug", name], self.root)
                self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(self.ledger.aliases()["bots"]), ["alert", "alias", "capture-files", "lock", "ops-alert", "tmp"])

    # fixed: round-2 R-02 (`append` refuses a reserved name for a new chain, like `alias add` does)
    def test_reserved_bot_names_refused_on_append(self):
        for name in ("capture-files-ops", "ops.alert", "ops.lock", "ops.tmp", "aliases"):
            rc, out, err = run(["note", "--bot", name, "--detail", "must be refused"], self.root)
            self.assertEqual(rc, 2, "%s: exit %s %s%s" % (name, rc, out, err))
            self.assertIn("reserved", err, name)
            self.assertNotIn("Traceback", err, name)
            self.assertFalse(os.path.isdir(os.path.join(self.root, "logs", name)), name)
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stderr(io.StringIO()):
                    self.ledger.append(name, {"type": "note", "detail": "library path"})
            self.assertEqual(ctx.exception.code, 2, name)
            self.assertFalse(os.path.isdir(os.path.join(self.root, "logs", name)), name)

    def _legacy_chain(self, name):
        """A one-record chain written straight to disk, as a pre-0.6 ledger would contain it."""
        os.makedirs(os.path.join(self.root, "logs", name))
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        rec = {"v": 1, "seq": 1, "ts": "2024-06-01T10:00:00Z", "bot": name, "type": "note",
               "detail": "written before 0.6", "prev": gblog.GENESIS}
        rec["hash"] = gblog.compute_hash(rec)
        with open(os.path.join(self.root, "logs", name, "2024-06-01.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        with open(os.path.join(self.root, "state", name + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"seq": 1, "head": rec["hash"], "updated": rec["ts"]}, fh)
        return rec

    def test_existing_reserved_chain_still_appends(self):
        for n, name in enumerate(("legacy.tmp", "capture-files-legacy"), 1):
            with self.subTest(name=name):
                first = self._legacy_chain(name)
                rec = self.ledger.append(name, {"type": "note", "detail": "still works", "ts": "2024-06-01T10:01:00Z"})
                self.assertEqual((rec["seq"], rec["prev"], rec["bot"]), (2, first["hash"], name))
                rc, out, err = run(["note", "--bot", name, "--detail", "from the cli"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("reserved", err)
                rc, out, err = run(["verify", "--bot", name], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(chain_statuses(out), {name: "OK"})
                res = self.ledger.verify(name)
                self.assertTrue(res["ok"], res["problems"])
                self.assertEqual(res["records"], 3)
                # the existing chain can also be registered
                rc, out, err = run(["alias", "add", "--slug", name, "--display", "Legacy Bot %d" % n], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(name, self.ledger.aliases()["bots"])
                rc, out, err = run(["note", "--bot", "Legacy Bot %d" % n, "--detail", "through the display name"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(len(self.chain(name)), 4)


# --------------------------------------------------------------------------- I-20 (CLI side)
class ArchiveCliTest(CliBase):
    def _file(self, name, data):
        path = os.path.join(self.work, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _stored(self):
        found = []
        for dirpath, _dirs, files in os.walk(os.path.join(self.root, "archive")):
            found.extend(os.path.join(dirpath, f) for f in files)
        return found

    def test_archive_missing_file_exit_2(self):
        missing = os.path.join(self.work, "nope.txt")
        for path in (missing, self.work):  # a missing file, and a directory
            with self.subTest(path=path):
                rc, out, err = run(["archive", "--bot", "ops", "--file", path], self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("archive: cannot read %s" % path, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "")
        rc, out, err = run(["archive", "--bot", "ops"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("archive: give --file PATH or --stdin", err)
        # nothing was chained and nothing was stored
        self.assertEqual(self.ledger.log_files("ops"), [])
        self.assertEqual(self._stored(), [])

        # a readable file still works
        ok = self._file("ok.txt", b"evidence line\n")
        rc, out, err = run(["archive", "--bot", "ops", "--file", ok, "--kind", "artifact"], self.root)
        self.assertEqual(rc, 0, out + err)
        rec = self.last("ops")
        self.assertEqual((rec["type"], rec["target"], rec["kind"]), ("archive", "ok.txt", "artifact"))
        self.assertEqual(len(self._stored()), 1)

    def test_archive_binary_marks_unscanned(self):
        warning = "warning: binary/non-UTF-8 content stored without secret scanning"
        blob = b"\x00\x01\x02\xff\xfe binary screenshot bytes"
        rc, out, err = run(["archive", "--bot", "ops", "--file", self._file("shot.bin", blob), "--kind", "artifact"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(warning, err)
        rec = self.last("ops")
        self.assertIs(rec["scanned"], False)
        self.assertIs(rec["redacted"], False)
        self.assertEqual((rec["target"], rec["bytes"]), ("shot.bin", len(blob)))
        with gzip.open(os.path.join(self.root, rec["path"]), "rb") as fh:
            self.assertEqual(fh.read(), blob)

        # UTF-16 text is not scanned either, and says so
        utf16 = "meeting notes, nothing to see\n".encode("utf-16")
        rc, out, err = run(["archive", "--bot", "ops", "--file", self._file("export.txt", utf16)], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(warning, err)
        self.assertIs(self.last("ops")["scanned"], False)

        # scanned text carries no `scanned` key at all
        rc, out, err = run(["archive", "--bot", "ops", "--file", self._file("plain.txt", b"plain evidence line\n")], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("stored without secret scanning", err)
        rec = self.last("ops")
        self.assertNotIn("scanned", rec)
        self.assertIs(rec["redacted"], False)

        # with --no-redact no scan was requested: no warning, but the record says so (round-2 R-13)
        other = b"\x00\x09 other binary"
        rc, out, err = run(["archive", "--bot", "ops", "--file", self._file("raw.bin", other), "--no-redact"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("stored without secret scanning", err)
        self.assertIs(self.last("ops").get("scanned"), False)

        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["archives"], 4)


# --------------------------------------------------------------------------- I-26
class ExitCodeTableTest(CliBase):
    def test_exit_code_table(self):
        # 0 - success
        rc, out, err = run(["note", "--bot", "ops", "--detail", "fine"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)

        # 2 - usage or environment error; nothing is written
        missing = os.path.join(self.work, "nope.txt")
        for argv in (
            ["note", "--bot", "ops", "--detail", "x", "--ts", "yesterday"],
            ["note", "--bot", "ops", "--no-such-flag"],
            ["note", "--bot", "///", "--detail", "x"],
            ["report", "--date", "2024-6-1", "--stdout"],
            ["archive", "--bot", "ops", "--file", missing],
        ):
            with self.subTest(code=2, argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 2, out + err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(len(self.chain("ops")), 1)

        # 1 - integrity FAIL
        rc, out, err = run(["verify", "--bot", "nosuchbot"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.ledger.append("edited", {"type": "note", "detail": "original text", "ts": stamp(0)})
        self.ledger.append("edited", {"type": "note", "detail": "second line", "ts": stamp(1)})
        self.tamper("edited", "original text", "tampered text")
        for argv in (["verify", "--bot", "edited"], ["verify", "--all"]):
            with self.subTest(code=1, argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 1, out + err)
                self.assertIn("FAIL", out)
                self.assertNotIn("Traceback", err)

        # 4 - tail mismatch: the append is refused and the event is spooled
        self.ledger.append("stuck", {"type": "note", "detail": "one", "ts": stamp(0)})
        self.ledger.write_state("stuck", {"seq": 99, "head": "cd" * 32})
        rc, out, err = run(["note", "--bot", "stuck", "--detail", "refused"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertEqual(len(self.chain("stuck")), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "spool", "stuck"))), 1)

    @unittest.skipUnless(GIT, "git not installed")
    def test_exit_code_3_sync_refusal(self):
        # 3 - policy refusal: sync will not push to a public GitHub repository.
        # remote_visibility is mocked and the push is refused before git contacts anything.
        self.ledger.append("ops", {"type": "note", "detail": "something to back up", "ts": stamp(0)})
        with mock.patch.object(gblog, "remote_visibility", return_value={"public": True, "reason": "test"}) as vis:
            rc, out, err = run(["sync", "--remote", "https://github.com/someone/public-repo.git"], self.root)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("refused", out + err)
        self.assertIn("PUBLIC", out + err)
        self.assertTrue(vis.called)
        self.assertEqual(self.last("system")["outcome"], "denied")


if __name__ == "__main__":
    unittest.main()
