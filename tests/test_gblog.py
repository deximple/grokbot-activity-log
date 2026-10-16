import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402


def run(argv, root):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = gblog.main(["--root", root] + argv)
    return rc, out.getvalue(), err.getvalue()


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gblog-test-")
        self.ledger = gblog.Ledger(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_chain_links_and_verifies(self):
        a = self.ledger.append("ops", {"type": "task_start", "task": "t1", "task_id": "x1"})
        b = self.ledger.append("ops", {"type": "action", "action": "shell", "target": "ls", "outcome": "ok"})
        self.assertEqual(a["prev"], gblog.GENESIS)
        self.assertEqual(b["prev"], a["hash"])
        self.assertEqual((a["seq"], b["seq"]), (1, 2))
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["records"], 2)
        self.assertEqual(res["head"], b["hash"])

    def test_chains_are_per_bot(self):
        self.ledger.append("a", {"type": "note", "detail": "x"})
        self.ledger.append("b", {"type": "note", "detail": "y"})
        self.assertEqual(self.ledger.verify("a")["records"], 1)
        self.assertEqual(self.ledger.verify("b")["records"], 1)
        self.assertEqual(self.ledger.bots(), ["a", "b"])

    def _log_path(self, bot):
        return self.ledger.log_files(bot)[0]

    def test_edit_is_detected(self):
        self.ledger.append("ops", {"type": "action", "action": "shell", "target": "git status"})
        self.ledger.append("ops", {"type": "action", "action": "shell", "target": "ls"})
        path = self._log_path("ops")
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace("git status", "git push --force")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("hash mismatch", res["problems"][0])

    def test_deleted_line_is_detected(self):
        for i in range(3):
            self.ledger.append("ops", {"type": "note", "detail": str(i)})
        path = self._log_path("ops")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        open(path, "w", encoding="utf-8").write("\n".join([lines[0], lines[2]]) + "\n")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("broken chain", res["problems"][0])

    def test_truncated_tail_is_detected_via_state(self):
        for i in range(3):
            self.ledger.append("ops", {"type": "note", "detail": str(i)})
        path = self._log_path("ops")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines[:2]) + "\n")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("head does not match", res["problems"][0])

    def test_state_recovered_from_log_when_missing(self):
        a = self.ledger.append("ops", {"type": "note", "detail": "1"})
        os.remove(os.path.join(self.root, "state", "ops.json"))
        b = self.ledger.append("ops", {"type": "note", "detail": "2"})
        self.assertEqual(b["prev"], a["hash"])
        self.assertEqual(b["seq"], 2)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_redaction(self):
        rec = self.ledger.append("ops", {
            "type": "action", "action": "shell",
            "target": "curl -H 'Authorization: Bearer abcdef1234567890' https://user:pa55word@example.com/x",
            "detail": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123 and AKIAABCDEFGHIJKLMNOP and -----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        })
        blob = json.dumps(rec)
        for leaked in ("abcdef1234567890", "pa55word", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123", "AKIAABCDEFGHIJKLMNOP", "BEGIN PRIVATE KEY"):
            self.assertNotIn(leaked, blob)
        self.assertIn("[REDACTED]", rec["target"])

    def test_bot_name_sanitised(self):
        rec = self.ledger.append("Ops Bot/Alpha", {"type": "note", "detail": "x"})
        self.assertEqual(rec["bot"], "Ops-Bot-Alpha")
        with self.assertRaises(SystemExit):
            self.ledger.append("///", {"type": "note"})

    def test_day_rollover_keeps_single_chain(self):
        self.ledger.append("ops", {"type": "note", "detail": "d1", "ts": "2026-01-01T10:00:00Z"})
        self.ledger.append("ops", {"type": "note", "detail": "d2", "ts": "2026-01-02T10:00:00Z"})
        self.assertEqual(len(self.ledger.log_files("ops")), 2)
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["records"], 2)

    def test_report_groups_tasks(self):
        tid = "abc123"
        self.ledger.append("ops", {"type": "task_start", "task": "Pay rent", "task_id": tid, "ts": "2026-01-05T09:00:00Z"})
        self.ledger.append("ops", {"type": "action", "action": "browser", "target": "https://bank.example/login", "task_id": tid, "outcome": "ok", "ts": "2026-01-05T09:01:00Z"})
        self.ledger.append("ops", {"type": "approval", "target": "transfer 1000", "task_id": tid, "outcome": "denied", "decision": "denied", "ts": "2026-01-05T09:02:00Z"})
        self.ledger.append("ops", {"type": "task_end", "task_id": tid, "outcome": "fail", "ts": "2026-01-05T09:03:00Z"})
        text = gblog.render_report(self.ledger.records("ops", date="2026-01-05"), "r")
        self.assertIn("### Pay rent", text)
        self.assertIn("**DENIED**", text)
        self.assertIn("outcome **fail**", text)
        self.assertIn("| Denied | 1 |", text)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gblog-cli-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_full_flow(self):
        rc, out, _ = run(["init"], self.root)
        self.assertEqual(rc, 0)
        rc, tid, _ = run(["task-start", "--bot", "ops", "--task", "demo"], self.root)
        tid = tid.strip()
        self.assertEqual(rc, 0)
        self.assertEqual(len(tid), 10)
        rc, _, _ = run(["log", "--bot", "ops", "--task-id", tid, "--action", "shell", "--target", "echo hi"], self.root)
        self.assertEqual(rc, 0)
        rc, _, _ = run(["approval", "--bot", "ops", "--task-id", tid, "--target", "send mail", "--decision", "allowed"], self.root)
        self.assertEqual(rc, 0)
        rc, _, _ = run(["task-end", "--bot", "ops", "--task-id", tid, "--outcome", "ok"], self.root)
        self.assertEqual(rc, 0)
        rc, out, _ = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK"))
        rc, out, _ = run(["report", "--bot", "ops", "--stdout"], self.root)
        self.assertEqual(rc, 0)
        self.assertIn("### demo", out)
        rc, out, _ = run(["status", "--json"], self.root)
        data = json.loads(out)
        self.assertEqual(data["bots"][0]["seq"], 4)
        self.assertEqual(data["bots"][0]["open_tasks"], 0)
        rc, out, _ = run(["export", "--bot", "ops"], self.root)
        self.assertEqual(len(out.strip().splitlines()), 4)

    def test_verify_exit_code_on_tamper(self):
        run(["note", "--bot", "ops", "--detail", "a"], self.root)
        run(["note", "--bot", "ops", "--detail", "b"], self.root)
        path = gblog.Ledger(self.root).log_files("ops")[0]
        text = open(path, encoding="utf-8").read().replace('"detail":"a"', '"detail":"z"').replace('"detail": "a"', '"detail": "z"')
        open(path, "w", encoding="utf-8").write(text)
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_sync_commits_locally(self):
        run(["note", "--bot", "ops", "--detail", "a"], self.root)
        rc, out, _ = run(["sync", "--no-push"], self.root)
        self.assertEqual(rc, 0, out)
        self.assertIn("commit", out)
        log = subprocess.run(["git", "-C", self.root, "log", "--oneline"], capture_output=True, text=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 1)
        rc, out, _ = run(["verify", "--all"], self.root)
        self.assertEqual(rc, 0)
        rc, out, _ = run(["status"], self.root)
        self.assertIn("not pushed", out)


class RedactionPatterns(unittest.TestCase):
    def test_bearer_without_colon_and_query_params(self):
        self.assertEqual(gblog.redact("Authorization: Bearer abcdef123456 x"), "Authorization: Bearer [REDACTED] x")
        self.assertEqual(
            gblog.redact("https://mail.google.com/inbox?token=abc&x=1&api_key=zzz"),
            "https://mail.google.com/inbox?token=[REDACTED]&x=1&api_key=[REDACTED]",
        )
        self.assertEqual(gblog.redact("https://user:pa55word@h/x"), "https://[REDACTED]@h/x")
        self.assertIn("[REDACTED]", gblog.redact("ghp_" + "a" * 36))
        self.assertIn("[REDACTED]", gblog.redact("AKIAABCDEFGHIJKLMNOP"))
        self.assertIn("[REDACTED]", gblog.redact("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"))

    def test_plain_text_untouched(self):
        for s in ["git status", "open https://example.com/a/b", "token count was 12", "the key is under the mat"]:
            self.assertEqual(gblog.redact(s), s)


class CaptureCoverageTuneTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gblog-v02-")
        self.ledger = gblog.Ledger(self.root)
        self.work = tempfile.mkdtemp(prefix="gblog-work-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)

    def test_shell_hook_mentions_bot_and_installs_once(self):
        hook = gblog.shell_hook("ops")
        self.assertIn('GBLOG_BOT:-ops', hook)
        self.assertIn("--source auto", hook)
        rc = os.path.join(self.work, "bashrc")
        self.assertTrue(gblog.install_shell_hook("ops", rc))
        self.assertFalse(gblog.install_shell_hook("ops", rc))
        self.assertEqual(open(rc).read().count(">>> gblog shell hook >>>"), 1)

    def test_capture_files_diff(self):
        open(os.path.join(self.work, "a.txt"), "w").write("1")
        res = gblog.capture_files(self.ledger, "ops", self.work)
        self.assertEqual(res["changes"], 0)
        open(os.path.join(self.work, "a.txt"), "w").write("22")
        open(os.path.join(self.work, "b.txt"), "w").write("x")
        res = gblog.capture_files(self.ledger, "ops", self.work)
        self.assertEqual(res["changes"], 2)
        recs = self.ledger.records("ops")
        self.assertEqual(sorted((r["target"], r["detail"]) for r in recs), [("a.txt", "modified"), ("b.txt", "created")])
        self.assertTrue(all(r["source"] == "auto" and r["action"] == "file" for r in recs))
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_coverage_matches_within_window(self):
        L = self.ledger
        L.append("ops", {"type": "action", "action": "browser", "target": "https://www.github.com/x/y", "ts": "2026-01-01T10:00:00Z"})
        L.append("ops", {"type": "action", "action": "browser", "target": "https://github.com/x/y/pulls", "source": "auto", "ts": "2026-01-01T10:05:00Z"})
        L.append("ops", {"type": "action", "action": "browser", "target": "https://example.org/", "source": "auto", "ts": "2026-01-01T10:06:00Z"})
        L.append("ops", {"type": "action", "action": "shell", "target": "git status", "ts": "2026-01-01T10:07:00Z"})
        L.append("ops", {"type": "action", "action": "shell", "target": "sudo git -C /x status", "source": "auto", "ts": "2026-01-01T10:08:00Z"})
        L.append("ops", {"type": "action", "action": "shell", "target": "rm -rf build", "source": "auto", "ts": "2026-01-01T12:00:00Z"})
        cov = gblog.coverage(L.records("ops"))
        self.assertEqual((cov["auto"], cov["matched"], cov["pct"]), (4, 2, 50.0))
        self.assertEqual(sorted(r["target"] for r in cov["unmatched"]), ["https://example.org/", "rm -rf build"])
        self.assertIn("50.0%", gblog.render_coverage(cov, "t"))

    def test_tune_proposals_apply_and_effect(self):
        L = self.ledger
        now = gblog._dt.datetime.utcnow()
        def ts(days_ago, hour=9, minute=0):
            return (now - gblog._dt.timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        for d in (6, 5, 4):
            tid = "t%d" % d
            L.append("ops", {"type": "task_start", "task_id": tid, "task": "Reconcile invoices %d" % d, "ts": ts(d)})
            L.append("ops", {"type": "action", "task_id": tid, "action": "connector", "target": "quickbooks.sync", "outcome": "fail", "detail": "401", "ts": ts(d, 9, 5)})
            L.append("ops", {"type": "approval", "task_id": tid, "target": "send email to vendor", "decision": "denied", "outcome": "denied", "ts": ts(d, 9, 6)})
            L.append("ops", {"type": "task_end", "task_id": tid, "outcome": "fail", "ts": ts(d, 9, 30)})
        L.append("ops", {"type": "note", "detail": "Lesson: always export the CSV before syncing", "ts": ts(4, 10)})
        recs = L.records("ops")
        analysis = gblog.analyze(recs, 28, gblog.coverage(recs))
        cats = {p["category"]: p for p in analysis["proposals"]}
        self.assertIn("skill", cats)      # repeated quickbooks.sync failure
        self.assertIn("approval", cats)   # repeatedly denied
        self.assertIn("routine", cats)    # same task 3 days in a row
        self.assertIn("memory", cats)     # lesson in a note
        self.assertEqual(cats["skill"]["baseline"], 3.0)
        self.assertEqual(analysis["metrics"]["ops"]["tasks"], 3)
        text = gblog.render_tune(analysis, "T")
        self.assertIn("quickbooks.sync", text)
        self.assertIn("--apply %s" % cats["skill"]["id"], text)
        # CLI: write proposals, apply one, see the effect table
        rc, out, _ = run(["tune", "--days", "28"], self.root)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, "tuning", "proposals.json")))
        rc, out, _ = run(["tune", "--apply", cats["skill"]["id"], "--detail", "added re-auth step to skill"], self.root)
        self.assertEqual(rc, 0, out)
        tune_rec = L.records("ops")[-1]
        self.assertEqual((tune_rec["type"], tune_rec["outcome"], tune_rec["target"]), ("tune", "ok", cats["skill"]["id"]))
        self.assertEqual(tune_rec["proposal"]["metric"]["kind"], "fail")
        rc, out, _ = run(["tune", "--days", "28", "--stdout"], self.root)
        self.assertIn("Effect of applied changes", out)
        self.assertIn("too early", out)
        rc, out, _ = run(["tune", "--reject", cats["approval"]["id"], "--detail", "intended"], self.root)
        self.assertEqual(rc, 0)
        self.assertTrue(L.verify("ops")["ok"])
        rc, out, _ = run(["tune", "--apply", "nope"], self.root)
        self.assertNotEqual(rc, 0)

    def test_log_source_and_env_task(self):
        os.environ["GBLOG_TASK"] = "envtask"
        try:
            rc, out, _ = run(["log", "--bot", "ops", "--action", "shell", "--target", "ls", "--source", "auto"], self.root)
        finally:
            del os.environ["GBLOG_TASK"]
        self.assertEqual(rc, 0)
        rec = self.ledger.records("ops")[-1]
        self.assertEqual((rec["task_id"], rec["source"]), ("envtask", "auto"))


class AliasArchiveSyncTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gblog-v03-")
        self.ledger = gblog.Ledger(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed_variants(self):
        L = self.ledger
        L.append("kim", {"type": "task_start", "task_id": "a1", "task": "Reconcile invoices", "ts": "2026-02-01T09:00:00Z"})
        L.append("KimBisunim", {"type": "action", "action": "shell", "target": "ls", "ts": "2026-02-01T09:05:00Z"})
        L.append("kim-primary", {"type": "task_start", "task_id": "a2", "task": "Reconcile invoices", "ts": "2026-02-01T09:10:00Z"})
        L.append("kim-primary", {"type": "task_end", "task_id": "a2", "outcome": "ok", "ts": "2026-02-01T09:20:00Z"})

    def test_resolve_and_label_without_aliases_is_unchanged(self):
        self.assertEqual(self.ledger.resolve("ops"), ("ops", True))
        self.assertEqual(self.ledger.label("ops"), "ops")
        with self.assertRaises(SystemExit):
            self.ledger.resolve("김비서님")

    def test_alias_merges_chains_without_rewriting_them(self):
        self._seed_variants()
        heads_before = {c: self.ledger.read_state(c)["head"] for c in self.ledger.bots()}
        rc, out, _ = run(["alias", "add", "--slug", "kim-biseo", "--display", "김비서님", "--alias", "kim", "--alias", "KimBisunim",
                          "--alias", "kim-primary"], self.root)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ledger.resolve("KimBisunim"), ("kim-biseo", True))
        self.assertEqual(self.ledger.resolve("김비서님"), ("kim-biseo", True))
        self.assertEqual(self.ledger.resolve("kim-biseo"), ("kim-biseo", True))
        self.assertEqual(self.ledger.label("kim"), "김비서님(kim-biseo)")
        self.assertEqual(sorted(self.ledger.chains_for("kim-biseo")), ["KimBisunim", "kim", "kim-primary"])
        self.assertEqual(len(self.ledger.records("kim-biseo")), 4)
        # new writes go to the canonical slug; old chains are untouched and still verify
        rc, out, _ = run(["log", "--bot", "김비서님", "--action", "shell", "--target", "pwd"], self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self.ledger.records("kim-biseo")[-1]["bot"], "kim-biseo")
        self.assertEqual({c: self.ledger.read_state(c)["head"] for c in heads_before}, heads_before)
        for c in self.ledger.bots():
            self.assertTrue(self.ledger.verify(c)["ok"], c)
        # unknown names warn (and are refused in strict mode)
        rc, out, err = run(["log", "--bot", "lee", "--action", "shell", "--target", "ls"], self.root)
        self.assertEqual(rc, 0)
        self.assertIn("not registered", err)
        run(["alias", "strict", "on"], self.root)
        with self.assertRaises(SystemExit):
            run(["log", "--bot", "park", "--action", "shell", "--target", "ls"], self.root)
        rc, out, _ = run(["alias", "list"], self.root)
        self.assertIn("kim-biseo", out)
        self.assertIn("김비서님", out)
        self.assertIn("unregistered chains: lee", out)

    def test_report_status_tail_merge_and_detect_cross_chain_close(self):
        self._seed_variants()
        self.ledger.add_alias("kim-biseo", "김비서님", ["kim", "KimBisunim", "kim-primary"])
        rc, out, _ = run(["report", "--date", "2026-02-01", "--stdout"], self.root)
        self.assertEqual(rc, 0)
        self.assertIn("## 김비서님(kim-biseo)", out)
        self.assertIn("Chains merged by alias", out)
        self.assertIn("likely closed as `a2` in chain `kim-primary` (ok)", out)
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith("## ")), 1)
        rc, out, _ = run(["status"], self.root)
        self.assertIn("김비서님(kim-biseo)", out)
        self.assertNotIn("KimBisunim", out)
        rc, out, _ = run(["status", "--chains"], self.root)
        self.assertIn("KimBisunim", out)
        rc, out, _ = run(["tail", "-n", "1"], self.root)
        self.assertIn("김비서님(kim-biseo)", out)
        rc, out, _ = run(["coverage", "--bot", "kim", "--days", "100000"], self.root)
        self.assertEqual(rc, 0)

    def test_archive_roundtrip_dedup_redaction_and_verify(self):
        src = os.path.join(self.root, "transcript.jsonl")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write('{"role":"user","text":"token=abcdef123456 please"}\n{"role":"bot","text":"done"}\n')
        rc, out, _ = run(["archive", "--bot", "ops", "--file", src, "--kind", "transcript", "--task-id", "t1"], self.root)
        self.assertEqual(rc, 0, out)
        rec = self.ledger.records("ops")[-1]
        self.assertEqual((rec["type"], rec["kind"], rec["target"], rec["redacted"]), ("archive", "transcript", "transcript.jsonl", True))
        full = os.path.join(self.root, rec["path"])
        self.assertTrue(os.path.exists(full))
        import gzip
        stored = gzip.open(full, "rb").read()
        self.assertNotIn(b"abcdef123456", stored)
        self.assertIn(b"[REDACTED]", stored)
        self.assertEqual(rec["sha256"], gblog.hashlib.sha256(stored).hexdigest())
        res = self.ledger.verify("ops")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["archives"], 1)
        # same content again -> same path, "already stored"
        rc, out, _ = run(["archive", "--bot", "ops", "--file", src], self.root)
        self.assertIn("already stored", out)
        self.assertEqual(len(os.listdir(os.path.dirname(full))), 1)
        # tamper with the archived file -> verify fails, chain itself still fine
        with gzip.open(full, "wb") as fh:
            fh.write(b"something else")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("content hash mismatch", res["problems"][0])
        os.remove(full)
        self.assertIn("archived file missing", self.ledger.verify("ops")["problems"][0])
        rc, out, _ = run(["verify", "--bot", "ops"], self.root)
        self.assertEqual(rc, 1)
        # binary via stdin is stored verbatim
        rec = gblog.archive_file(self.ledger, "ops", b"\x00\x01\x02token=abcdef123456", "blob.bin", "artifact")
        self.assertFalse(rec["redacted"])
        self.assertEqual(rec["bytes"], 21)

    def test_sync_refuses_public_remote(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        self.ledger.append("ops", {"type": "note", "detail": "x"})
        calls = []
        orig = gblog.remote_visibility
        gblog.remote_visibility = lambda remote, timeout=6.0: calls.append(remote) or {"public": True, "reason": "test"}
        try:
            rc, out, err = run(["sync", "--remote", "https://github.com/someone/public-repo.git"], self.root)
        finally:
            gblog.remote_visibility = orig
        self.assertEqual(rc, 3, out + err)
        self.assertIn("PUBLIC", out)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("github.com/someone/public-repo.git"), calls)  # git may rewrite the URL via insteadOf
        self.assertEqual(self.ledger.records("system")[-1]["outcome"], "denied")
        # --no-push never contacts the network; --allow-public attempts the push
        gblog.remote_visibility = lambda remote, timeout=6.0: calls.append("again") or {"public": True, "reason": "test"}
        try:
            rc, out, err = run(["sync", "--no-push"], self.root)
            self.assertEqual(rc, 0, out + err)
            rc, out, err = run(["sync", "--allow-public"], self.root)
        finally:
            gblog.remote_visibility = orig
        self.assertNotEqual(rc, 3)

    def test_remote_visibility_parses_urls(self):
        for url in ("git@github.com:deximple/grokledger.git", "https://github.com/deximple/grokledger", "https://github.com/deximple/grokledger.git/"):
            m = gblog._GITHUB_REMOTE_RE.search(url)
            self.assertEqual((m.group(1), m.group(2)), ("deximple", "grokledger"), url)
        self.assertIsNone(gblog.remote_visibility("/srv/git/ledger.git")["public"])

    def test_capture_browser_from_chrome_history_fixture(self):
        import sqlite3
        prof = os.path.join(self.root, "google-chrome-beta", "Profile 2")
        os.makedirs(prof)
        hist = os.path.join(prof, "History")
        conn = sqlite3.connect(hist)
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        now_webkit = int((gblog._dt.datetime.utcnow().timestamp() + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
        conn.execute("INSERT INTO urls VALUES (1, 'https://mail.google.com/mail/u/0/?token=zzz#inbox', 'Inbox')")
        conn.execute("INSERT INTO urls VALUES (2, 'chrome://newtab/', 'New tab')")
        conn.execute("INSERT INTO visits VALUES (1, 1, ?)", (now_webkit - 60_000_000,))
        conn.execute("INSERT INTO visits VALUES (2, 2, ?)", (now_webkit - 30_000_000,))
        conn.execute("INSERT INTO visits VALUES (3, 1, ?)", (now_webkit - 1_000_000,))
        conn.commit(); conn.close()
        os.environ["GBLOG_BROWSER_HISTORY"] = os.path.join(self.root, "google-chrome*", "*", "History")
        try:
            found = gblog.find_browser_histories()
            self.assertIn(os.path.realpath(hist), found)
            res = gblog.capture_browser(self.ledger, "ops", None, hours=24)
            self.assertEqual(res["added"], 1)
            res = gblog.capture_browser(self.ledger, "ops", None, hours=24)
            self.assertEqual(res["added"], 0)  # marker per history file
        finally:
            del os.environ["GBLOG_BROWSER_HISTORY"]
        rec = self.ledger.records("ops")[-1]
        self.assertEqual(rec["target"], "https://mail.google.com/mail/u/0/")
        self.assertEqual(rec["profile"], "Profile 2")
        self.assertEqual(rec["source"], "auto")
        st = self.ledger.read_state("ops")
        self.assertIsInstance(st["capture_browser_last"], dict)


if __name__ == "__main__":
    unittest.main()
