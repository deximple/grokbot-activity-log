"""Tests for the specialist reviews K3 (compatibility and migration) and K4 (cost on large ledgers).

Each test names the finding it covers. Everything runs in temporary directories; no network is used
(git remotes are local bare repositories or are never contacted: ``--no-push``).
"""
import io
import json
import os
import random
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
from _support import DAY, GIT_ENV, run_strict as run, stamp  # noqa: E402

GIT = shutil.which("git")
FIXTURE = os.path.join(HERE, "fixtures", "kim-biseo-break")


def raw_chain(root, bot, stamps, day, bot_field=None):
    """One hash-valid note per entry of ``stamps`` in logs/<bot>/<day>.jsonl, written without gblog; state names the last."""
    d = os.path.join(root, "logs", bot)
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    prev, recs = gblog.GENESIS, []
    with open(os.path.join(d, day + ".jsonl"), "w", encoding="utf-8") as fh:
        for i, ts in enumerate(stamps, 1):
            rec = {"v": 1, "seq": i, "ts": ts, "bot": bot_field or bot, "type": "note", "detail": "legacy %d" % i,
                   "prev": prev}
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            recs.append(rec)
    with open(os.path.join(root, "state", bot + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"seq": len(recs), "head": prev, "updated": stamps[-1]}, fh)
    return recs


class Base(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-k3k4-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT"):
            os.environ.pop(key, None)

    def quiet(self, fn, *args, **kwargs):
        with redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def seed(self, bot="ops", n=3):
        return [self.quiet(self.ledger.append, bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1),
                                                     "ts": stamp(i)}) for i in range(n)]

    def chain(self, bot="ops"):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]


# --------------------------------------------------------------------------- K3-01
class LegacyTimestampTest(Base):
    def _append(self, now, **fields):
        with mock.patch.object(gblog, "now_iso", return_value=now), redirect_stderr(io.StringIO()):
            return self.ledger.append("ops", dict({"type": "note", "detail": "after the upgrade"}, **fields))

    def _files(self):
        return [os.path.basename(p) for p in self.ledger.log_files("ops")]

    def test_offset_tail_in_a_later_day_file_does_not_brick_the_chain(self):
        raw_chain(self.root, "ops", ["2024-06-02T08:00:00+09:00"], "2024-06-02")
        rec = self._append("2024-06-01T23:30:00Z")
        self.assertEqual(self._files(), ["2024-06-02.jsonl"])
        self.assertEqual((rec["ts"], rec["clock_regressed"]), ("2024-06-02T08:00:00Z", "2024-06-01T23:30:00Z"))
        self.assertTrue(self.ledger.verify("ops")["ok"])
        nxt = self._append("2024-06-02T09:00:00Z")
        self.assertEqual((nxt["seq"], nxt["ts"]), (3, "2024-06-02T09:00:00Z"))
        self.assertNotIn("clock_regressed", nxt)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    def test_offset_tail_on_the_same_utc_day_keeps_verifying(self):
        raw_chain(self.root, "ops", ["2024-06-01T23:30:00+09:00"], "2024-06-01")
        rec = self._append("2024-06-01T15:00:00Z")
        self.assertEqual((rec["ts"], rec["clock_regressed"]), ("2024-06-01T23:30:00Z", "2024-06-01T15:00:00Z"))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])

    def test_legacy_spellings_that_sort_low_are_not_clamped(self):
        for raw in ("2024-06-01 23:30:00", "2024-06-01"):
            raw_chain(self.root, "ops", [raw], "2024-06-01")
            rec = self._append("2024-06-01T15:00:00Z")
            self.assertEqual(rec["ts"], "2024-06-01T15:00:00Z", raw)
            self.assertNotIn("clock_regressed", rec)
            self.assertTrue(self.ledger.verify("ops")["ok"], raw)

    def test_minute_precision_tail_gets_the_minimal_floor(self):
        raw_chain(self.root, "ops", ["2024-06-01T23:30Z"], "2024-06-01")
        rec = self._append("2024-06-01T23:30:10Z")
        self.assertEqual(rec["ts"], "2024-06-01T23:31:00Z")
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_explicit_ts_and_archive_use_the_same_bound(self):
        raw_chain(self.root, "ops", ["2024-06-01T23:30:00+09:00"], "2024-06-01")
        log = self.ledger.log_files("ops")[0]
        with open(log, "rb") as fh:
            before = fh.read()
        rc, out, err = run(["note", "--bot", "ops", "--detail", "x", "--ts", "2024-06-01T15:00:00Z"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("is earlier than the last record of ops", err)
        with self.assertRaises(gblog.TimestampError):
            gblog.archive_file(self.ledger, "ops", b"x\n", "e.txt", ts="2024-06-01T15:00:00Z")
        with open(log, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual([f for _d, _s, fs in os.walk(os.path.join(self.root, "archive")) for f in fs], [])
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_ts_floor_unit(self):
        floor = gblog._ts_floor
        self.assertEqual(floor({"ts": "2024-06-01T10:00:00Z"}, "/x/logs/ops/2024-06-01.jsonl"), "2024-06-01T10:00:00Z")
        self.assertEqual(floor({"ts": "2024-06-01T10:00:00Z"}, "/x/logs/ops/2024-06-03.jsonl"), "2024-06-03T00:00:00Z")
        self.assertEqual(floor({"ts": "2024-06-01T23:30:00+09:00"}, None), "2024-06-01T23:30:00Z")
        self.assertIsNone(floor({"ts": 5}, None))
        self.assertIsNone(floor({"ts": "garbage"}, "/x/logs/ops/zz-backup.jsonl"))
        self.assertIsNone(floor(None, None))


# --------------------------------------------------------------------------- K3-03
class CaseVariantChainTest(Base):
    def test_case_variant_bot_field_counts_when_it_is_the_same_directory(self):
        recs = raw_chain(self.root, "Ops", [stamp(0), stamp(1)], DAY, bot_field="ops")
        raw_chain(self.root, "alpha", [stamp(0)], DAY, bot_field="bravo")
        with mock.patch.object(os.path, "samefile", return_value=True):  # what a case-insensitive file system answers
            res = self.ledger.verify("Ops")
            self.assertTrue(res["ok"], res["problems"])
            self.assertEqual(res["warnings"], [])
            self.assertEqual(res.own_hashes, {r["hash"] for r in recs})
            self.assertTrue(self.ledger.verify("Ops", strict=True)["ok"])
            other = self.ledger.verify("alpha")  # another name is never a case variant
            self.assertEqual(other["warnings"],
                             ["logs/alpha/%s.jsonl:1: bot field 'bravo' does not match chain 'alpha'" % DAY])
            self.assertEqual(other.own_hashes, set())


# --------------------------------------------------------------------------- K3-04
class SameRemoteTest(Base):
    def test_same_remote_unit(self):
        same = gblog._same_remote
        url = "https://github.com/you/private-ledger.git"
        for other in ("https://github.com/you/private-ledger", "https://github.com/you/private-ledger/",
                      "git@github.com:you/private-ledger.git", "ssh://git@github.com/You/Private-Ledger.git"):
            self.assertTrue(same(url, other), other)
        for other in ("https://github.com/you/other.git", "https://github.com/you/private-ledger.wiki.git",
                      "https://evil.example/you/private-ledger.git", "https://gist.github.com/0123456789abcdef.git"):
            self.assertFalse(same(url, other), other)
        self.assertTrue(same("https://h.example/r.git", "https://h.example/r/"))
        self.assertFalse(same("https://h.example:8443/r", "https://h.example/r"))
        self.assertFalse(same("git@h.example:r.git", "ssh://git@h.example/r.git"))
        self.assertTrue(same(self.base, self.base + os.sep))
        self.assertFalse(same(self.base, os.path.join(self.base, "other")))

    def _origin(self):
        return subprocess.run([GIT, "-C", self.root, "remote", "get-url", "origin"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def _remote_records(self):
        return [r for r in self.chain("system") if r.get("target") == "git remote"]

    @unittest.skipUnless(GIT, "git not installed")
    def test_equivalent_spelling_of_origin_is_not_a_change(self):
        self.seed("ops", 1)
        url = "https://github.com/you/private-ledger.git"
        rc, out, err = run(["sync", "--no-push", "--remote", url], self.root)
        self.assertEqual(rc, 0, out + err)
        for same in ("https://github.com/you/private-ledger", "git@github.com:you/private-ledger.git"):
            rc, out, err = run(["sync", "--no-push", "--remote", same], self.root)
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(self._origin(), url)
        self.assertEqual(len(self._remote_records()), 1)
        rc, out, err = run(["sync", "--no-push", "--remote", "https://github.com/you/other.git"], self.root)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("--replace-remote", err)
        self.assertEqual(self._origin(), url)

    @unittest.skipUnless(GIT, "git not installed")
    def test_trailing_slash_on_a_local_remote_is_the_same_remote(self):
        self.seed("ops", 1)
        bare = os.path.join(self.base, "first.git")
        subprocess.run([GIT, "init", "--bare", "-q", bare], check=True, capture_output=True)
        self.assertEqual(run(["sync", "--no-push", "--remote", bare], self.root)[0], 0)
        rc, out, err = run(["sync", "--no-push", "--remote", bare + "/"], self.root)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._origin(), bare)


# --------------------------------------------------------------------------- K4-02
MUTATIONS = (
    ("healthy", lambda l: l),
    ("edited", lambda l: [l[0].replace("day one 0", "day one X")] + l[1:]),
    ("dropped", lambda l: l[:2] + l[3:]),
    ("two gaps", lambda l: [l[0]] + l[2:4] + l[5:]),
    ("swapped", lambda l: [l[1], l[0]] + l[2:]),
    ("garbage", lambda l: l[:3] + ["{not json"] + l[3:]),
    ("duplicated", lambda l: l[:2] + [l[1]] + l[2:]),
    ("cut", lambda l: l[:-1] + [l[-1][:40]]),
)


class StreamingVerifyTest(Base):
    def _same(self, ledger, bot, label=""):
        new = None
        for strict in (True, False):
            new = ledger.verify(bot, strict=strict)
            old = ledger._verify_reference(bot, strict=strict)
            self.assertEqual(dict(new), dict(old), "%s strict=%s" % (label, strict))
            self.assertEqual(new.hashes, old.hashes, label)
            self.assertEqual(new.own_hashes, old.own_hashes, label)
        return new

    def _template(self):
        root = os.path.join(self.base, "template")
        ledger = gblog.Ledger(root)
        with redirect_stderr(io.StringIO()):
            for i in range(6):
                ledger.append("ops", {"type": "note", "detail": "day one %d" % i, "ts": stamp(i)})
            gblog.archive_file(ledger, "ops", b"evidence\n", "e.txt", "artifact", ts=stamp(10))
            ledger.append("ops", {"type": "archive", "target": "claim.pdf", "ts": stamp(11)})
            for i in range(4):
                ledger.append("ops", {"type": "note", "detail": "day two %d" % i, "ts": stamp(i, "2024-06-02")})
            ledger.anchor(["ops"])
        return root

    def test_streaming_verify_matches_the_reference(self):
        template = self._template()
        cases = [(name, fn, None) for name, fn in MUTATIONS]
        cases += [("no state", MUTATIONS[0][1], "remove"), ("wrong state head", MUTATIONS[0][1], "wrong")]
        for n, (name, mutate, state) in enumerate(cases):
            root = os.path.join(self.base, "case%d" % n)
            shutil.copytree(template, root)
            log = os.path.join(root, "logs", "ops", DAY + ".jsonl")
            with open(log, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 8)
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("".join(line + "\n" for line in mutate(lines)))
            state_path = os.path.join(root, "state", "ops.json")
            if state == "remove":
                os.remove(state_path)
            elif state == "wrong":
                with open(state_path, "w", encoding="utf-8") as fh:
                    json.dump({"seq": 12, "head": "ab" * 32}, fh)
            res = self._same(gblog.Ledger(root), "ops", name)
            if name == "healthy":
                self.assertTrue(res["ok"], res["problems"])
                self.assertEqual((res["records"], res["archives"]), (12, 1))  # the bare claim is not counted
            elif name != "no state":
                self.assertFalse(res["ok"], name)

    def test_streaming_verify_matches_the_reference_on_the_sealed_fixture(self):
        for name in ("logs", "state"):
            shutil.copytree(os.path.join(FIXTURE, name), os.path.join(self.root, name))
        res = self._same(self.ledger, "kim-biseo", "unsealed")
        self.assertEqual(len(res["breaks"]), 1)
        self.quiet(self.ledger.seal_break, "kim-biseo", res["breaks"][0], "TEST-APPROVAL-0001", copy_fixture=True)
        res = self._same(self.ledger, "kim-biseo", "sealed")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual((res["approved_breaks"], res["breaks"], len(res["segments"])), (1, [], 2))
        self._same(self.ledger, "ledger", "witness chain")

    def test_verify_does_not_keep_the_parsed_chain(self):
        self.seed("ops", 3)
        with mock.patch.object(gblog.Ledger, "_chain_snapshot", side_effect=AssertionError("whole-list snapshot")):
            res = self.ledger.verify("ops", strict=True)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 3)

    @unittest.skipIf(gblog.fcntl is None, "flock is not available on this platform")
    def test_verify_parses_after_the_lock_is_released(self):
        self.seed("ops", 3)
        lock = os.path.join(self.root, "state", "ops.lock")
        real, free = gblog._parse_line_dups, []

        def probing(raw):
            if not free:
                with open(lock, "a+") as fh:
                    try:
                        gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_EX | gblog.fcntl.LOCK_NB)
                        gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_UN)
                        free.append(True)
                    except OSError:
                        free.append(False)
            return real(raw)

        with mock.patch.object(gblog, "_parse_line_dups", probing):
            res = self.ledger.verify("ops")
        self.assertEqual(free, [True])
        self.assertTrue(res["ok"], res["problems"])

    def test_wanted_hashes_limit_what_verify_keeps(self):
        recs = self.seed("ops", 3)
        self.assertEqual(len(self.ledger.verify("ops").hashes), 3)
        self.ledger.verify_want_hashes = [recs[1]["hash"], "ab" * 32]
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res.hashes, {recs[1]["hash"]})
        self.assertEqual(res.own_hashes, {recs[1]["hash"]})
        rc, out, err = run(["verify", "--bot", "ops", "--contains", recs[0]["hash"]], self.root)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run(["verify", "--bot", "ops", "--contains", "ab" * 32], self.root)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("expected hash abababababab not found in chain", out)


# --------------------------------------------------------------------------- K4-04
TOKENS = ("token", "pwd", "api", "key", "apikey", "secret", "TOKEN", "_", "-", "=", ":", " ", '"', "'", "&", "a",
          "Zz9", ".", "\n", "value12", "x1", "eyJ", "aaaaaaaaaa", "eyJhbGciOiJIUzI1", "\u00e9")
FIXED = (
    "a__b_token=secretvalue", "x.token=secretvalue", "token=ab pwd=longvalue1", "token=pwd=longvalue1",
    "token_ = value12", "g0_g1_g2_g3_g4_g5_g6_g7_g8_g9_g10_g11_token=secretvalue", 'password="my dog spot" next',
    "DB_PASSWORD=hunter2hunter2 ./deploy.sh", "api_key : 'two words' x", "token=\"abc\"def_pwd=longvalue1",
    "eyJaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc", "x eyJaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc- y",
    "_eyJaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc", "eyJeyJaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc\u00e9",
    "eyJaaaaa-eyJaaaaaaaaaa.bbbbbbbbbbbb.aaaaaaaaaaaa-bbb\u00e9 eyJaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc",
)


class LinearRedactionTest(Base):
    def _rules(self):
        rules = [pat for pat, _repl in gblog._SECRET_PATTERNS if isinstance(pat, gblog._LinearRule)]
        self.assertEqual(sorted(r.fn.__name__ for r in rules), ["_redact_env_names", "_redact_jwt"])
        return rules

    def _samples(self):
        rng = random.Random(8)
        for text in FIXED:
            yield text
        for _ in range(12000):
            yield "".join(rng.choice(TOKENS) for _ in range(rng.randrange(1, 30)))

    def test_linear_rules_match_the_former_expressions(self):
        rules = self._rules()
        for text in self._samples():
            for rule in rules:
                former, repl = rule.former
                self.assertEqual(rule.sub(None, text), former.sub(repl, text), repr(text))

    def test_redact_output_is_unchanged(self):
        def old_redact(text):
            out = gblog._redact_pem(text)
            for pat, repl in gblog._SECRET_PATTERNS:
                if isinstance(pat, gblog._LinearRule):
                    pat, repl = pat.former
                out = pat.sub(repl, out)
            return out

        rng = random.Random(9)
        for _ in range(3000):
            text = "".join(rng.choice(TOKENS) for _ in range(rng.randrange(1, 30)))
            self.assertEqual(gblog.redact(text), old_redact(text), repr(text))

    def test_redaction_is_linear_on_crafted_text(self):
        # each of these took minutes with the former expressions
        chain = "token_" * 50000
        self.assertEqual(gblog._redact_env_names(chain), chain)
        self.assertEqual(gblog.redact(chain + "x"), chain + "x")
        run_text = "eyJaaaaaaaaaa-" * 50000
        self.assertEqual(gblog._redact_jwt(run_text), run_text)
        self.assertTrue(gblog._redact_env_names(chain + "x=secretvalue").endswith("x=[REDACTED]"))

    @unittest.skipIf(gblog.fcntl is None, "flock is not available on this platform")
    def test_append_redacts_before_it_takes_the_lock(self):
        self.seed("ops", 1)
        lock = os.path.join(self.root, "state", "ops.lock")
        real, free = gblog.redact, []

        def probing(text):
            with open(lock, "a+") as fh:
                try:
                    gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_EX | gblog.fcntl.LOCK_NB)
                    gblog.fcntl.flock(fh.fileno(), gblog.fcntl.LOCK_UN)
                    free.append(True)
                except OSError:
                    free.append(False)
            return real(text)

        with mock.patch.object(gblog, "redact", probing):
            rec = self.ledger.append("ops", {"type": "note", "detail": "token=abcdef123456", "ts": stamp(5)})
        self.assertTrue(free and all(free), free)
        self.assertEqual(rec["detail"], "token=[REDACTED]")
        self.assertEqual(list(rec)[:6], ["v", "seq", "ts", "bot", "type", "detail"])
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- K4-07
class SpoolReplayIndexTest(Base):
    def test_replay_reads_the_chain_through_one_index(self):
        self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        for i in range(5):
            with self.assertRaises(gblog.TailMismatchError):
                self.ledger.append("ops", {"type": "note", "detail": "refused %d" % i})
        seq, head = self.ledger._tail_head("ops")
        self.ledger.write_state("ops", {"seq": seq, "head": head})
        with mock.patch.object(gblog.Ledger, "_spool_replayed", side_effect=AssertionError("one scan per event")):
            res = self.quiet(self.ledger.spool_replay, "ops")
            self.assertEqual([r["detail"] for r in res["replayed"]], ["refused %d" % i for i in range(5)])
            self.assertEqual((res["remaining"], res["refused"], res["skipped"]), (0, False, []))
            again = self.quiet(self.ledger.spool_replay, "ops")
        self.assertEqual(again["replayed"], [])
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 7)

    def test_event_that_is_already_in_the_chain_is_not_appended_again(self):
        self.seed("ops", 2)
        self.ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        spooled = []
        for i in range(2):
            with self.assertRaises(gblog.TailMismatchError) as ctx:
                self.ledger.append("ops", {"type": "note", "detail": "refused %d" % i})
            spooled.append(ctx.exception.spool_path)
        seq, head = self.ledger._tail_head("ops")
        self.ledger.write_state("ops", {"seq": seq, "head": head})
        with open(spooled[0], encoding="utf-8") as fh:
            payload = json.load(fh)
        # the first event was appended by a run that died before it moved the spool file
        self.quiet(self.ledger.append, "ops", dict(payload["record"], spooled_at=payload["ts"],
                                                   spool_id=os.path.basename(spooled[0])[:-5]))
        res = self.quiet(self.ledger.spool_replay, "ops")
        self.assertEqual([r["detail"] for r in res["replayed"]], ["refused 1"])
        details = [r.get("detail") for r in self.chain()]
        self.assertEqual((details.count("refused 0"), details.count("refused 1")), (1, 1))
        self.assertTrue(self.ledger.verify("ops")["ok"])


# --------------------------------------------------------------------------- K4-08
def _task(n, kind, tid, ts="2024-06-01T09:00:00Z", **fields):
    rec = {"v": 1, "seq": n, "ts": ts, "bot": "ops", "type": kind, "task_id": tid}
    rec.update(fields)
    return rec


class ReportTwinIndexTest(unittest.TestCase):
    def test_many_open_tasks_do_not_rescan_every_task(self):
        recs = [_task(i + 1, "task_start", "t%d" % i, task="Task %d" % (i % 20)) for i in range(2000)]
        recs += [_task(2001 + i, "task_end", "t%d" % i, outcome="ok") for i in range(1000)]
        real, calls = gblog._title_key, [0]

        def counting(title):
            calls[0] += 1
            return real(title)

        with mock.patch.object(gblog, "_title_key", counting):
            text = gblog.render_report(recs, "r")
        self.assertLessEqual(calls[0], 2100)  # once per closed task, once per open task
        self.assertEqual(text.count("outcome **open**"), 1000)
        self.assertNotIn("likely closed", text)

    def test_twin_in_another_chain_is_still_found(self):
        recs = [_task(1, "task_start", "a1", task="Reconcile invoices", chain="cos"),
                _task(2, "task_start", "a0", task="Something else", chain="cos-primary"),
                _task(3, "task_start", "a2", task="reconcile  INVOICES", chain="cos-primary"),
                _task(4, "task_end", "a2", outcome="ok", chain="cos-primary")]
        text = gblog.render_report(recs, "r")
        self.assertIn("outcome **open** \u2014 likely closed as `a2` in chain `cos-primary` (ok)", text)
        self.assertEqual(text.count("likely closed"), 1)

    def test_title_from_another_day_counts_only_for_later_tasks(self):
        # B has only its task-end on this day; its title and start come from the day before (the report context)
        open_a = _task(1, "task_start", "A", ts="2024-06-01T08:00:00Z", task="Night job", chain="c1")
        end_b = _task(2, "task_end", "B", ts="2024-06-01T09:00:00Z", outcome="ok", chain="c2")
        context = {("ops", "B"): {"start": "2024-06-01T08:30:00Z", "title": "Night job"}}
        after = gblog.render_report([end_b, open_a], "r", None, context)   # B is rendered first: A sees its title
        self.assertIn("likely closed as `B` in chain `c2` (ok)", after)
        before = gblog.render_report([open_a, end_b], "r", None, context)  # A is rendered first: B has no title yet
        self.assertNotIn("likely closed", before)
        self.assertIn("### Night job  `B`", before.splitlines())


if __name__ == "__main__":
    unittest.main()
