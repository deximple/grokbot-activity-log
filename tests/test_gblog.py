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
        text = open(path, encoding="utf-8").read().replace("git status", "git push --force")
        open(path, "w", encoding="utf-8").write(text)
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("hash mismatch", res["problems"][0])

    def test_deleted_line_is_detected(self):
        for i in range(3):
            self.ledger.append("ops", {"type": "note", "detail": str(i)})
        path = self._log_path("ops")
        lines = open(path, encoding="utf-8").read().splitlines()
        open(path, "w", encoding="utf-8").write("\n".join([lines[0], lines[2]]) + "\n")
        res = self.ledger.verify("ops")
        self.assertFalse(res["ok"])
        self.assertIn("broken chain", res["problems"][0])

    def test_truncated_tail_is_detected_via_state(self):
        for i in range(3):
            self.ledger.append("ops", {"type": "note", "detail": str(i)})
        path = self._log_path("ops")
        lines = open(path, encoding="utf-8").read().splitlines()
        open(path, "w", encoding="utf-8").write("\n".join(lines[:2]) + "\n")
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


if __name__ == "__main__":
    unittest.main()
