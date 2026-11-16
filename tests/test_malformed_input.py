"""Malformed-input tests for the post-audit CLI (audit item I-07).

Raw bytes are written straight into ``logs/<bot>/<date>.jsonl`` (and ``state/``) of a
temporary ledger. Whatever is in there, the CLI must answer with an exit code - 1 for
``verify``, 4 for a refused append, 2 for usage / environment errors - and never with
a Python traceback.
"""
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import gblog  # noqa: E402

GBLOG_PY = os.path.join(REPO, "gblog.py")
DAY = "2024-06-01"


sys.path.insert(0, HERE)
from _support import deep_nesting, run_cli, stamp  # noqa: E402,F401  (shared helpers, round-3 W-20)
from _support import run as _support_run  # noqa: E402


def run(argv, root):
    """Run the CLI in-process. An uncaught exception or Ctrl-C is rendered like the real CLI would (traceback, exit 1)."""
    return _support_run(argv, root, catch_interrupt=True)


class MalformedBase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="gblog-malformed-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GBLOG_TASK", None)
        os.environ.pop("GBLOG_ROOT", None)

    def seed(self, bot="ops", n=2):
        return [self.ledger.append(bot, {"type": "note", "detail": "valid %d" % (i + 1), "ts": stamp(i)}) for i in range(n)]

    def log(self, bot="ops"):
        return os.path.join(self.root, "logs", bot, DAY + ".jsonl")

    def state_path(self, bot="ops"):
        return os.path.join(self.root, "state", bot + ".json")

    def log_bytes(self, bot="ops"):
        with open(self.log(bot), "rb") as fh:
            return fh.read()

    def insert_raw(self, bot, raw, index):
        path = self.log(bot)
        with open(path, "rb") as fh:
            lines = fh.read().split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        lines.insert(index, raw)
        with open(path, "wb") as fh:
            fh.write(b"\n".join(lines) + b"\n")

    def append_raw(self, bot, raw):
        with open(self.log(bot), "ab") as fh:
            fh.write(raw + b"\n")

    def crafted(self, bot, prev_hash, **over):
        """A hash-valid record (the hash really is the sha256 of the body) with odd field types."""
        rec = {"v": 1, "seq": 3, "ts": stamp(2), "bot": bot, "type": "note", "detail": "crafted", "prev": prev_hash}
        rec.update(over)
        rec["hash"] = gblog.compute_hash(rec)
        return json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def assert_fail_not_crash(self, bot, line=None):
        res = self.ledger.verify(bot)  # must return, not raise
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["problems"])
        if line is not None:
            where = "logs/%s/%s.jsonl:%d" % (bot, DAY, line)
            self.assertTrue(any(where in p for p in res["problems"]), res["problems"])
        rc, out, err = run(["verify", "--bot", bot], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL", out)
        self.assertNotIn("Traceback", err)
        return res

    def spooled(self, bot):
        d = os.path.join(self.root, "spool", bot)
        if not os.path.isdir(d):
            return []
        return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json"))


class MalformedLogTest(MalformedBase):
    def test_non_object_lines_fail_not_crash(self):
        n = 0
        for raw in (b"[]", b"123", b'"x"', b"null"):
            for where, index in (("first", 0), ("middle", 1), ("last", 2)):
                bot = "c%d" % n
                n += 1
                with self.subTest(raw=raw, where=where):
                    self.seed(bot)
                    self.insert_raw(bot, raw, index)
                    self.assert_fail_not_crash(bot, line=index + 1)
                    # readers skip the line instead of crashing on it
                    self.assertEqual(len(self.ledger.records(bot)), 2)
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("FAIL")), n)

    def test_invalid_utf8_fails_not_crash(self):
        cases = (
            ("garbage line", lambda bot: self.append_raw(bot, b"\xff\xfe\xfa")),
            ("latin-1 inside JSON", lambda bot: self.append_raw(bot, b'{"detail": "caf\xe9"}')),
            ("byte flipped in a valid line", lambda bot: self._flip(bot)),
        )
        for i, (label, damage) in enumerate(cases):
            bot = "u%d" % i
            with self.subTest(case=label):
                self.seed(bot)
                damage(bot)
                self.assert_fail_not_crash(bot)
                self.assertLessEqual(len(self.ledger.records(bot)), 2)  # must not raise

        # a JSON-escaped lone surrogate parses, but cannot be re-encoded for hashing: a hash mismatch, not a crash
        bot = "surrogate"
        recs = self.seed(bot)
        line = json.dumps({"v": 1, "seq": 3, "ts": stamp(2), "bot": bot, "type": "note", "detail": "\ud800",
                           "prev": recs[1]["hash"], "hash": "00" * 32}, sort_keys=True)  # ensure_ascii keeps it \ud800
        self.assertIn("\\ud800", line)
        self.append_raw(bot, line.encode("ascii"))
        self.assert_fail_not_crash(bot, line=3)

    def _flip(self, bot):
        data = self.log_bytes(bot)
        self.assertIn(b"valid 2", data)
        with open(self.log(bot), "wb") as fh:
            fh.write(data.replace(b"valid 2", b"valid \xff"))

    def test_bad_field_types_fail_not_crash(self):
        cases = (
            ("ts-int", {"ts": 123}),
            ("ts-list", {"ts": ["2024-06-01"]}),
            ("ts-null", {"ts": None}),
            ("seq-str", {"seq": "three"}),
            ("seq-digits", {"seq": "3"}),
            ("seq-bool", {"seq": True}),
            ("seq-float", {"seq": 3.0}),
            ("seq-null", {"seq": None}),
            ("prev-int", {"prev": 7}),
        )
        for label, over in cases:
            bot = "f-" + label
            with self.subTest(case=label):
                recs = self.seed(bot)
                self.append_raw(bot, self.crafted(bot, recs[1]["hash"], **over))
                res = self.assert_fail_not_crash(bot)
                self.assertTrue(any("malformed record fields" in p for p in res["problems"]), res["problems"])
                self.ledger.records(bot)  # sorting / filtering must not raise on these types

        # such a tail is never appended to: the event is spooled and the CLI exits 4
        bot = "f-seq-str"
        before = self.log_bytes(bot)
        rc, out, err = run(["note", "--bot", bot, "--detail", "keep me"], self.root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("ALERT tail mismatch", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.log_bytes(bot), before)
        self.assertEqual(len(self.spooled(bot)), 1)

    def test_report_status_survive_corrupt_line(self):
        tid = "t1"
        self.ledger.append("ops", {"type": "task_start", "task_id": tid, "task": "Survivor task", "ts": stamp(0)})
        self.ledger.append("ops", {"type": "action", "task_id": tid, "action": "shell", "target": "ls", "outcome": "ok", "ts": stamp(1)})
        self.ledger.append("ops", {"type": "task_end", "task_id": tid, "outcome": "ok", "ts": stamp(2)})
        for raw in (b"[]", b"123", b"null", b'"x"', b"{not json", b"\xff\xfe garbage", b'{"detail": "caf\xe9"}'):
            self.append_raw("ops", raw)
        self.assertEqual(len(self.ledger.records("ops")), 3)
        self.assertEqual(len(self.ledger.records("ops", date=DAY)), 3)
        outputs = {}
        commands = (
            ["report", "--date", DAY, "--stdout"],
            ["report", "--since", DAY, "--stdout"],
            ["report", "--bot", "ops", "--date", DAY, "--stdout"],
            ["status"],
            ["status", "--json"],
            ["status", "--chains"],
            ["tail"],
            ["tail", "--bot", "ops", "-n", "2"],
            ["export"],
            ["coverage", "--days", "36500"],
            ["tune", "--days", "28", "--stdout"],
        )
        for argv in commands:
            with self.subTest(argv=argv):
                rc, out, err = run(list(argv), self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("Traceback", err)
                outputs[" ".join(argv)] = out
        self.assertIn("### Survivor task", outputs["report --date %s --stdout" % DAY])
        self.assertIn("| Records | 3 |", outputs["report --date %s --stdout" % DAY])
        exported = [json.loads(line) for line in outputs["export"].splitlines() if line.strip()]
        self.assertEqual([r["seq"] for r in exported], [1, 2, 3])
        data = json.loads(outputs["status --json"])
        self.assertEqual([r["bot"] for r in data["bots"]], ["ops"])
        # verify still says FAIL about the same file
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)

    def test_append_refuses_and_spools_on_non_object_tail(self):
        n = 0
        for raw in (b"[]", b"123", b"null", b'"x"', b"\xff\xfe"):
            for drop_state in (False, True):
                bot = "t%d" % n
                n += 1
                with self.subTest(raw=raw, drop_state=drop_state):
                    self.seed(bot)
                    self.append_raw(bot, raw)
                    if drop_state:
                        os.remove(self.state_path(bot))
                    before = self.log_bytes(bot)
                    rc, out, err = run(["note", "--bot", bot, "--detail", "keep me %s" % bot], self.root)
                    self.assertEqual(rc, 4, out + err)
                    self.assertIn("ALERT tail mismatch", err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.log_bytes(bot), before)
                    files = self.spooled(bot)
                    self.assertEqual(len(files), 1)
                    with open(files[0], encoding="utf-8") as fh:
                        payload = json.load(fh)
                    self.assertEqual(payload["kind"], "tail_mismatch")
                    self.assertEqual(payload["record"]["detail"], "keep me %s" % bot)
                    self.assertTrue(os.path.isfile(os.path.join(self.root, "state", bot + ".alert.json")))
                    # the library raises the same typed refusal, not AttributeError / UnicodeDecodeError
                    with self.assertRaises(gblog.TailMismatchError):
                        self.ledger.append(bot, {"type": "note", "detail": "library path", "ts": stamp(40)})


class MalformedArgsAndStateTest(MalformedBase):
    def test_surrogate_argument_is_logged(self):
        bad = "bad \udcff byte"
        rec = self.ledger.append("ops", {"type": "note", "detail": bad, "extra": {"k": ["nested \udc80"]}, "ts": stamp(0)})
        self.assertEqual(rec["detail"], "bad \\udcff byte")
        json.dumps(rec, ensure_ascii=False).encode("utf-8")  # everything that was stored is encodable
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(self.ledger.records("ops")[-1]["detail"], "bad \\udcff byte")

        if os.name != "posix":
            return
        # the real thing: undecodable argv bytes arrive as surrogates (this is what the shell hook hides with `|| true`)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env.pop("GBLOG_TASK", None)
        proc = subprocess.run([sys.executable, GBLOG_PY, "--root", self.root, "note", "--bot", "ops", "--detail", b"caf\xff argv"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn("Traceback", err)
        last = [r for _p, _n, r in self.ledger.iter_records("ops")][-1]
        self.assertEqual(last["seq"], 2)
        self.assertEqual(last["detail"], "caf\\udcff argv")
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])

    def test_non_dict_state(self):
        for i, content in enumerate(("[]", '"x"', "123", "null")):
            bot = "s%d" % i
            with self.subTest(state=content):
                self.seed(bot)
                with open(self.state_path(bot), "w", encoding="utf-8") as fh:
                    fh.write(content)
                self.assertIsInstance(self.ledger.read_state(bot), dict)
                rc, out, err = run(["verify", "--bot", bot], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("Traceback", err)
                self.assertIn("state/%s.json missing or unreadable" % bot, out)
                for argv in (["status"], ["status", "--json"], ["tail", "--bot", bot]):
                    rc, out, err = run(argv, self.root)
                    self.assertEqual(rc, 0, out + err)
                    self.assertNotIn("Traceback", err)
                # an unreadable state is treated like a missing one: the append adopts the log tail (R-07)
                before = self.log_bytes(bot)
                rc, out, err = run(["note", "--bot", bot, "--detail", "after bad state"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("notice: state/%s.json was unreadable; adopted the log tail (seq 2)" % bot, err)
                self.assertNotIn("Traceback", err)
                self.assertTrue(self.log_bytes(bot).startswith(before))
                self.assertEqual(sum(1 for _item in self.ledger.iter_records(bot)), 3)
                self.assertEqual(self.spooled(bot), [])
                # the supported repair works on an unreadable state
                with open(self.state_path(bot), "w", encoding="utf-8") as fh:
                    fh.write(content)
                rc, out, err = run(["state", "rebuild", "--bot", bot], self.root)
                self.assertEqual(rc, 0, out + err)
                rc, out, err = run(["note", "--bot", bot, "--detail", "after rebuild"], self.root)
                self.assertEqual(rc, 0, out + err)
                rc, out, err = run(["verify", "--bot", bot], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIsInstance(self.ledger.read_state(bot).get("seq"), int)

    def test_error_exit_code_is_2(self):
        # library callers still see SystemExit, now with code 2 and the message on stderr
        err = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(err):
                gblog.safe_name("///")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("error: bot name must contain letters or digits", err.getvalue())

        rc, out, err = run(["note", "--bot", "///", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

        rc, out, err = run(["alias", "remove", "--slug", "nosuch"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: 'nosuch' is not a registered slug", err)

        rc, out, err = run(["alias", "add", "--slug", "known-bot", "--display", "Known Bot"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["alias", "strict", "on"], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["log", "--bot", "stranger", "--action", "shell", "--target", "ls"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("not registered", err)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "logs", "stranger")))

        # a corrupt JSON side file is an error message, not a traceback
        os.makedirs(os.path.join(self.root, "tuning"), exist_ok=True)
        with open(os.path.join(self.root, "tuning", "proposals.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        rc, out, err = run(["tune", "--apply", "abcd1234"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is corrupt", err)
        self.assertNotIn("Traceback", err)

        # an environment error (the ledger root is a regular file)
        not_a_dir = os.path.join(self.base, "root-is-a-file")
        with open(not_a_dir, "w", encoding="utf-8") as fh:
            fh.write("x")
        rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], not_a_dir)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_top_level_handlers(self):
        self.seed("ops")
        boom = OSError(errno.ENOSPC, "No space left on device", "/x/y/full.jsonl")
        with mock.patch.object(gblog.Ledger, "append", side_effect=boom):
            rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error: No space left on device", err)
        self.assertIn("/x/y/full.jsonl", err)
        self.assertNotIn("Traceback", err)

        with mock.patch.object(gblog.Ledger, "append", side_effect=KeyboardInterrupt):
            rc, out, err = run(["note", "--bot", "ops", "--detail", "x"], self.root)
        self.assertEqual(rc, 130, out + err)
        self.assertNotIn("Traceback", err)

    @unittest.skipUnless(os.name == "posix", "pipe semantics are POSIX-specific")
    def test_broken_pipe_exits_quietly(self):
        # about 1 MB of export output, far more than any pipe buffer; built raw so no fsync is involved
        bot_dir = os.path.join(self.root, "logs", "big")
        os.makedirs(bot_dir)
        os.makedirs(os.path.join(self.root, "state"))
        prev = gblog.GENESIS
        lines = []
        ts = ""
        for seq in range(1, 3001):
            ts = "%sT%02d:%02d:%02dZ" % (DAY, seq // 3600, (seq // 60) % 60, seq % 60)
            rec = {"v": 1, "seq": seq, "ts": ts, "bot": "big", "type": "note", "detail": ("padding " * 25) + str(seq), "prev": prev}
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            lines.append(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        with open(os.path.join(bot_dir, DAY + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(self.root, "state", "big.json"), "w", encoding="utf-8") as fh:
            json.dump({"seq": 3000, "head": prev, "updated": ts}, fh)

        proc = subprocess.Popen([sys.executable, GBLOG_PY, "--root", self.root, "export"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            first = proc.stdout.readline()
            proc.stdout.close()  # what `gblog export | head -1` does
            err = proc.stderr.read().decode("utf-8", "replace")
            rc = proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
        self.assertEqual(json.loads(first.decode("utf-8"))["seq"], 1)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)


# --------------------------------------------------------------------------- round 2 (R-05, R-06, R-10)
# run_cli(argv, root, encoding) runs the real CLI in a fresh process with a real stdout of the given
# encoding; it comes from tests/_support.py (imported at the top of this module).


ALIASES_WARNING = ("warning: state/aliases.json is unreadable; names are not resolved and strict mode is off "
                   "until it is restored")
TEXT_FIELDS = ("type", "task_id", "task", "action", "target", "outcome", "detail", "source", "decision", "observed", "kind")


class RemainingCrashPathsTest(MalformedBase):
    def craft_chain(self, bot, prev_hash, overrides, first_seq=3):
        """Append hash-valid records with odd field types straight to the log and move state to the last one."""
        last = None
        for i, over in enumerate(overrides):
            fields = {"seq": first_seq + i, "ts": stamp(first_seq + i)}
            fields.update(over)
            raw = self.crafted(bot, prev_hash, **fields)
            self.append_raw(bot, raw)
            last = json.loads(raw.decode("utf-8"))
            prev_hash = last["hash"]
        self.ledger.write_state(bot, {"seq": last["seq"], "head": last["hash"], "updated": last["ts"]})
        return last

    def test_deeply_nested_line_fails_not_crash(self):
        depth = deep_nesting(50000)
        cases = (
            ("arrays", b"[" * depth + b"]" * depth),
            ("objects", b'{"a":' * depth + b"1" + b"}" * depth),
        )
        for i, (label, raw) in enumerate(cases):
            bot = "deep%d" % i
            with self.subTest(case=label):
                self.seed(bot)
                self.append_raw(bot, raw)
                self.assert_fail_not_crash(bot, line=3)
                rc, out, err = run(["verify", "--bot", bot, "--strict"], self.root)
                self.assertEqual(rc, 1, out[:2000] + err[-2000:])
                self.assertNotIn("Traceback", err)
                self.assertEqual(len(self.ledger.records(bot)), 2)  # readers skip the line
                rc, out, err = run(["report", "--date", DAY, "--stdout"], self.root)
                self.assertEqual(rc, 0, err[-2000:])
                self.assertNotIn("Traceback", err)
                rc, out, err = run(["note", "--bot", bot, "--detail", "keep me"], self.root)
                self.assertEqual(rc, 4, out + err[-2000:])
                self.assertNotIn("Traceback", err)
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out[:2000] + err[-2000:])
        self.assertNotIn("Traceback", err)

    def test_corrupt_gzip_stream_fails_not_crash(self):
        rec = gblog.archive_file(self.ledger, "ops", b"evidence that will be damaged\n", "e.txt", "artifact", ts=stamp(0))
        full = os.path.join(self.root, rec["path"])
        self.assertTrue(self.ledger.verify("ops")["ok"])
        # a valid gzip header followed by bytes that are not a deflate stream (reserved block type): zlib.error on read
        with open(full, "wb") as fh:
            fh.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + b"\xff" * 64)
        res = self.ledger.verify("ops")  # must return, not raise
        self.assertFalse(res["ok"], res)
        self.assertTrue(any(p.startswith(rec["path"] + ": unreadable (") for p in res["problems"]), res["problems"])
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL", out)
        self.assertIn(rec["path"] + ": unreadable (", out)
        self.assertNotIn("Traceback", err)
        rc, out, err = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        rc, out, err = run(["verify", "--bot", "ops", "--skip-archives"], self.root)
        self.assertEqual(rc, 0, out + err)

    def test_status_survives_wrong_typed_state_fields(self):
        good_sync = {"ts": "2024-06-01T11:00:00Z", "commit": "abc1234", "pushed": False}
        cases = (
            {"seq": "three", "head": 7, "updated": 5, "last_sync": "yesterday"},
            {"seq": None, "head": None, "updated": ["x"], "last_sync": {"ts": 1, "commit": "abc1234"}},
            {"seq": [1], "head": {"a": 1}, "updated": {"b": 2}, "last_sync": [1, 2]},
            {"seq": {"n": 1}, "head": 12, "updated": 0, "last_sync": {"ts": "2024-06-01T11:00:00Z"}},
        )
        for i, state in enumerate(cases):
            bot = "w%d" % i
            self.seed(bot)
            with open(self.state_path(bot), "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        self.seed("fine")
        self.assertTrue(self.ledger.update_state("fine", {"last_sync": good_sync}))
        for argv in (["status"], ["status", "--chains"], ["status", "--bot", "w0"], ["status", "--json", "--chains"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv, self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("Traceback", err)
        rc, out, err = run(["status", "--json"], self.root)
        self.assertEqual(rc, 0, out + err)
        rows = {r["bot"]: r for r in json.loads(out)["bots"]}
        for i in range(len(cases)):
            row = rows["w%d" % i]
            self.assertEqual(row["seq"], 0, row)
            self.assertEqual(row["head"], "genesis", row)
            self.assertIsNone(row["updated"], row)
            self.assertIsNone(row["last_sync"], row)
        fine = rows["fine"]
        self.assertEqual(fine["seq"], 2)
        self.assertEqual(len(fine["head"]), 12)
        self.assertEqual(fine["last_sync"], good_sync)

    def test_surrogate_record_export_and_report(self):
        recs = self.seed("ops")
        line = json.dumps({"v": 1, "seq": 3, "ts": stamp(2), "bot": "ops", "type": "note", "detail": "lone \ud800 surrogate",
                           "prev": recs[1]["hash"], "hash": "00" * 32}, sort_keys=True)  # ensure_ascii keeps it \ud800
        self.assertIn("\\ud800", line)
        self.append_raw("ops", line.encode("ascii"))
        self.assertEqual(len(self.ledger.records("ops")), 3)
        # a real stdout: the character that cannot be encoded is escaped, the command does not die
        for encoding in ("utf-8", "ascii"):
            for argv in (["export"], ["tail"], ["report", "--date", DAY, "--stdout"]):
                with self.subTest(encoding=encoding, argv=argv):
                    rc, out, err = run_cli(argv, self.root, encoding)
                    self.assertEqual(rc, 0, err)
                    self.assertNotIn("Traceback", err)
                    self.assertIn("\\ud800", out)
                    self.assertIn("valid 1", out)
        # in-process, without --stdout: the report file itself must be writable
        rc, out, err = run(["report", "--date", DAY], self.root)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        with open(os.path.join(self.root, "reports", DAY + ".md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("lone \\ud800 surrogate", text)
        self.assertIn("valid 1", text)

    def test_non_ascii_label_on_ascii_stdout(self):
        with redirect_stderr(io.StringIO()):
            self.ledger.add_alias("chief", "B\u00fcro Chef", [])
            self.ledger.append("chief", {"type": "note", "detail": "caf\u00e9 visit", "ts": stamp(0)})
        for argv in (["status"], ["tail"], ["report", "--date", DAY, "--stdout"], ["alias", "list"], ["export"]):
            with self.subTest(argv=argv):
                rc, out, err = run_cli(argv, self.root, "ascii")
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
        rc, out, err = run_cli(["status"], self.root, "ascii")
        self.assertIn("B\\xfcro Chef(chief)", out)
        rc, out, err = run_cli(["export"], self.root, "ascii")
        self.assertIn("caf\\u00e9 visit", out)  # export stays valid JSON: ensure_ascii escaping

    def test_non_string_text_fields_do_not_crash_readers(self):
        recs = self.seed("ops")
        odd = (
            {"type": "task_start", "task": 123, "task_id": ["t", 1]},
            {"type": "action", "action": "shell", "target": {"cmd": "ls"}, "detail": ["a", "b"], "outcome": "ok",
             "task_id": ["t", 1]},
            {"type": "action", "action": 9, "target": ["x"], "outcome": 1, "source": 3, "observed": 5, "detail": 7},
            {"type": "approval", "target": 5, "decision": ["denied"], "outcome": "denied", "detail": {"k": "v"}},
            {"type": "note", "detail": {"k": "v"}, "task_id": 77},
            {"type": "task_end", "task_id": ["t", 1], "outcome": ["ok"]},
            {"type": ["note"], "detail": "typed oddly", "kind": 4},
        )
        self.craft_chain("ops", recs[1]["hash"], odd)

        # readers hand out text for the text fields (the JSON text of whatever was stored)
        got = self.ledger.records("ops")
        self.assertEqual(len(got), 2 + len(odd))
        for rec in got:
            for field in TEXT_FIELDS:
                if field in rec:
                    self.assertIsInstance(rec[field], str, "%s of #%s" % (field, rec.get("seq")))
        by_seq = {r["seq"]: r for r in got}
        self.assertEqual(json.loads(by_seq[3]["task"]), 123)
        self.assertEqual(json.loads(by_seq[4]["detail"]), ["a", "b"])
        self.assertEqual(json.loads(by_seq[4]["target"]), {"cmd": "ls"})
        self.assertEqual(json.loads(by_seq[7]["task_id"]), 77)
        self.assertEqual(by_seq[1]["detail"], "valid 1")  # ordinary records are untouched

        commands = (
            ["report", "--date", DAY, "--stdout"],
            ["report", "--since", DAY, "--stdout"],
            ["report", "--bot", "ops", "--date", DAY],
            ["tail"],
            ["tail", "--bot", "ops", "-n", "3"],
            ["export"],
            ["status"],
            ["status", "--json"],
            ["coverage", "--days", "36500"],
            ["tune", "--days", "36500", "--stdout"],
            ["tune", "--days", "36500", "--json", "--stdout"],
        )
        outputs = {}
        for argv in commands:
            with self.subTest(argv=argv):
                rc, out, err = run(list(argv), self.root)
                self.assertEqual(rc, 0, out[:2000] + err)
                self.assertNotIn("Traceback", err)
                outputs[" ".join(argv)] = out
        rc, out, err = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 0, out + err)  # odd field types are a warning, never a FAIL of a hash-valid chain
        self.assertNotIn("Traceback", err)
        self.assertIn("      ! logs/ops/%s.jsonl:3: field 'task' is not a string" % DAY, out)
        self.assertRegex(out, r"(?m)^      ! logs/ops/[0-9-]+\.jsonl:\d+: field '\w+' is not a string$")
        # export stays a faithful dump of what is on disk
        rows = {r["seq"]: r for r in (json.loads(line) for line in outputs.get("export", "").splitlines() if line.strip())}
        self.assertEqual(rows[4]["detail"], ["a", "b"])
        self.assertEqual(rows[4]["target"], {"cmd": "ls"})
        self.assertEqual(rows[3]["task"], 123)

    def test_skipped_lines_are_announced(self):
        def build(root):
            ledger = gblog.Ledger(root)
            with redirect_stderr(io.StringIO()):
                ledger.append("ops", {"type": "task_start", "task_id": "t1", "task": "Announced task", "ts": stamp(0)})
                ledger.append("ops", {"type": "action", "task_id": "t1", "action": "shell", "target": "ls",
                                      "outcome": "ok", "ts": stamp(1)})
                ledger.append("ops", {"type": "task_end", "task_id": "t1", "outcome": "ok", "ts": stamp(2)})

        build(self.root)
        clean = os.path.join(self.base, "clean")
        build(clean)
        for raw in (b"{not json", b"[]"):
            self.append_raw("ops", raw)
        notice = "warning: skipped 2 unreadable line(s) in logs/ops; run gblog verify"
        for argv in (["report", "--date", DAY, "--stdout"], ["export"], ["tail"]):
            with self.subTest(argv=argv):
                rc, out, err = run(list(argv), self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertIn(notice, err)
                rc, clean_out, clean_err = run(list(argv), clean)
                self.assertEqual(rc, 0, clean_out + clean_err)
                self.assertEqual(out, clean_out)  # stdout is unchanged; only stderr speaks up
                self.assertNotIn("unreadable line", clean_err)
        pattern = r"warning: skipped \d+ unreadable line\(s\) in logs/%s; run gblog verify"
        for argv in (["coverage", "--days", "36500"], ["tune", "--days", "36500", "--stdout"]):
            with self.subTest(argv=argv):
                rc, out, err = run(list(argv), self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertRegex(err, pattern % "ops")
        # status only reads the last 30 days, so it needs a damaged file from today
        with redirect_stderr(io.StringIO()):
            self.ledger.append("fresh", {"type": "note", "detail": "written just now"})
        with open(self.ledger.log_files("fresh")[-1], "ab") as fh:
            fh.write(b"{not json\n")
        rc, out, err = run(["status"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(err, pattern % "fresh")
        self.assertNotIn("Traceback", err)

    def test_non_dict_aliases_warns_not_crashes(self):
        self.seed("ops")
        path = os.path.join(self.root, "state", "aliases.json")
        for content in ("[]", '"x"', '{"bots": [], "strict": true}'):
            with self.subTest(aliases=content):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                count = sum(1 for _item in self.ledger.iter_records("ops"))
                # a write still goes through (no event is lost) and the warning is printed once per process
                rc, out, err = run_cli(["note", "--bot", "ops", "--detail", "still logged"], self.root)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(err.count(ALIASES_WARNING), 1, err)
                self.assertEqual(sum(1 for _item in self.ledger.iter_records("ops")), count + 1)
                for argv in (["status"], ["report", "--date", DAY, "--stdout"], ["verify", "--bot", "ops"]):
                    rc, out, err = run_cli(argv, self.root)
                    self.assertEqual(rc, 0, "%s: %s%s" % (argv, out, err))
                    self.assertNotIn("Traceback", err)
                # the damaged registry is never overwritten
                for argv in (["alias", "add", "--slug", "new-bot", "--display", "New Bot"], ["alias", "strict", "on"]):
                    rc, out, err = run_cli(argv, self.root)
                    self.assertEqual(rc, 2, "%s: %s%s" % (argv, out, err))
                    self.assertNotIn("Traceback", err)
                    with open(path, encoding="utf-8") as fh:
                        self.assertEqual(fh.read(), content)


if __name__ == "__main__":
    unittest.main()
