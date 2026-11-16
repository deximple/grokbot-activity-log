"""Extended redaction shapes (audit F-08 / I-08, test item I-27).

Every secret-looking literal is assembled from pieces so that no scanner mistakes
this file for a leak. Nothing here touches the network or the wall clock.
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

PW = "hunter2" + "hunter2"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
STRIPE_LIVE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
STRIPE_TEST = "sk_" + "test_" + "51Hh1aBcDeFgHiJkLmNoPqRsT"
STRIPE_RK = "rk_" + "live_" + "9aZ8bY7cX6dW5eV4fU3gT2hS"
GITLAB = "glpat-" + "AbCdEfGhIjKlMnOpQrSt"
NPM = "npm_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GOOGLE_OAUTH = "ya29." + "a0AfH6SMBx1y2z3A4b5C6d7E8f9G0h1I2j3K4l5M6n"
SLACK_APP = "xapp-" + "1-A0123456789-1234567890123-" + "0123456789abcdef0123456789abcdef"
SLACK_HOOK_TAIL = "abcdEFGH1234ijklMNOP5678"
SLACK_HOOK = "https://hooks.slack.com/" + "services/" + "T00000000/B00000000/" + SLACK_HOOK_TAIL
BASIC = "dXNlcjpwYXNzd29yZA=="
BASIC2 = "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="

# (text, secret that must disappear, fragments that must survive)
ENV_CASES = [
    ("DB_PASSWORD=%s ./deploy.sh" % PW, PW, ("DB_PASSWORD", "./deploy.sh")),
    ("export AWS_SECRET_ACCESS_KEY=%s" % AWS_SECRET, AWS_SECRET, ("AWS_SECRET_ACCESS_KEY",)),
    ("GITHUB_TOKEN: abcdefgh12345678", "abcdefgh12345678", ("GITHUB_TOKEN",)),
    ("MYSQL_PWD=sup3rs3cret1 mysqldump shop", "sup3rs3cret1", ("MYSQL_PWD", "mysqldump shop")),
    ("my_api_key = abcd1234efgh5678", "abcd1234efgh5678", ("my_api_key",)),
    ("client_secret: zYxWvUtSrQ987654", "zYxWvUtSrQ987654", ("client_secret",)),
    ("STRIPE_PRIVATE_KEY=abcdef0123456789", "abcdef0123456789", ("STRIPE_PRIVATE_KEY",)),
]
QUOTED_CASES = [
    ('password="my dog spot" next', "my dog spot", ("password", "next")),
    ("SMTP_PASSWORD='two words' sendmail", "two words", ("SMTP_PASSWORD", "sendmail")),
]
JSON_CASES = [
    ('{"user": "bob", "password": "%s"}' % PW, PW, ('"user": "bob"', '"password"')),
    ('{"db_password":"p@ss w0rd!"}', "p@ss w0rd!", ('"db_password"',)),
    ('{"access_token": "abc.def-ghi_123456"}', "abc.def-ghi_123456", ('"access_token"',)),
    ('{"apiKey": "abcdef123456"}', "abcdef123456", ('"apiKey"',)),
    ('{"client_secret": "ab\\"cd-efgh-1234", "n": 1}', "cd-efgh-1234", ('"client_secret"', '"n": 1')),
    ('{"role":"user","text":"hi"}\n{"env":{"API_TOKEN":"tok_0123456789abcdef"}}', "tok_0123456789abcdef",
     ('"role":"user"', '"API_TOKEN"')),
]
FLAG_CASES = [
    ("mysql -u root --password %s shop" % PW, PW, ("mysql -u root", "--password", "shop")),
    ("tool --token=abcdef123456 run", "abcdef123456", ("tool", "run")),
    ('cli --api-key "my key value" go', "my key value", ("cli", "go")),
    ("deploy --secret 'top secret' now", "top secret", ("deploy", "now")),
    ("su --passwd=abc1 root", "abc1", ("su", "root")),
]
BASIC_CASES = [
    ("curl -H 'Authorization: Basic %s' https://example.com/x" % BASIC, BASIC, ("Authorization", "https://example.com/x")),
    ("authorization=basic %s" % BASIC2, BASIC2, ()),
]
VENDOR_CASES = [
    ("charge with %s today" % STRIPE_LIVE, STRIPE_LIVE[8:], ("charge with", "today")),
    ("charge with %s today" % STRIPE_TEST, STRIPE_TEST[8:], ("charge with", "today")),
    ("restricted %s here" % STRIPE_RK, STRIPE_RK[8:], ("restricted", "here")),
    ("git clone with %s done" % GITLAB, GITLAB[6:], ("git clone with", "done")),
    ("publish using %s ok" % NPM, NPM[4:], ("publish using", "ok")),
    ("oauth %s expired" % GOOGLE_OAUTH, GOOGLE_OAUTH[5:], ("oauth", "expired")),
    ("socket mode %s connected" % SLACK_APP, SLACK_APP[-32:], ("socket mode", "connected")),
    ("POST %s sent" % SLACK_HOOK, SLACK_HOOK_TAIL, ("POST", "sent")),
]
ALL_CASES = ENV_CASES + QUOTED_CASES + JSON_CASES + FLAG_CASES + BASIC_CASES + VENDOR_CASES


class ExtendedRedactionTest(unittest.TestCase):
    def assertRedacted(self, text, secret, keep=()):
        out = gblog.redact(text)
        self.assertNotIn(secret, out, "secret survived in %r -> %r" % (text, out))
        self.assertIn("[REDACTED]", out, text)
        for fragment in keep:
            self.assertIn(fragment, out, "context %r lost in %r -> %r" % (fragment, text, out))
        return out

    def _all(self, cases):
        for text, secret, keep in cases:
            with self.subTest(text=text):
                self.assertRedacted(text, secret, keep)

    def test_env_style_assignments(self):
        self._all(ENV_CASES)

    def test_quoted_values_with_spaces(self):
        self._all(QUOTED_CASES)
        self.assertNotIn("dog", gblog.redact('password="my dog spot" next'))

    def test_json_keys(self):
        self._all(JSON_CASES)

    def test_json_inside_json_string(self):
        inner = json.dumps({"password": PW, "user": "bob"})
        outer = json.dumps({"role": "tool", "text": inner})
        self.assertIn('\\"password\\"', outer)  # the shape under test: backslash-escaped quotes
        out = self.assertRedacted(outer, PW, ('"role": "tool"', "bob"))
        self.assertIn("password", out)

    def test_json_keys_pwd_and_access_key(self):
        # R-12: the JSON-key rules use the same word list as the documentation (pwd, access_key)
        short = "s3cretPwd" + "99"
        cases = [
            ('{"SecretAccessKey": "%s", "n": 1}' % AWS_SECRET, AWS_SECRET, ('"SecretAccessKey"', '"n": 1')),
            ('{"db_pwd": "%s", "user": "bob"}' % short, short, ('"db_pwd"', '"user": "bob"')),
            ('{"aws_access_key":"%s"}' % AWS_SECRET, AWS_SECRET, ('"aws_access_key"',)),
            ('{"access-key": "two words here", "n": 2}', "two words here", ('"access-key"', '"n": 2')),
        ]
        self._all(cases)
        for text, _secret, _keep in cases:
            with self.subTest(idempotent=text):
                once = gblog.redact(text)
                self.assertEqual(gblog.redact(once), once)
        # the same keys inside a JSON string (escaped quotes), as tool output appears in a transcript
        inner = json.dumps({"SecretAccessKey": AWS_SECRET, "db_pwd": short, "user": "bob"})
        outer = json.dumps({"role": "tool", "text": inner})
        self.assertIn('\\"SecretAccessKey\\"', outer)
        self.assertIn('\\"db_pwd\\"', outer)
        out = gblog.redact(outer)
        for secret in (AWS_SECRET, short):
            self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)
        self.assertIn('"role": "tool"', out)
        self.assertIn("SecretAccessKey", out)
        self.assertIn("db_pwd", out)
        self.assertIn("bob", out)
        self.assertEqual(gblog.redact(out), out)

    def test_cli_flags(self):
        self._all(FLAG_CASES)

    def test_basic_auth(self):
        self._all(BASIC_CASES)

    def test_stripe_keys(self):
        self._all(VENDOR_CASES[0:3])

    def test_gitlab_token(self):
        self._all(VENDOR_CASES[3:4])

    def test_npm_token(self):
        self._all(VENDOR_CASES[4:5])

    def test_google_oauth_token(self):
        self._all(VENDOR_CASES[5:6])

    def test_slack_app_token(self):
        self._all(VENDOR_CASES[6:7])

    def test_slack_webhook_url(self):
        self._all(VENDOR_CASES[7:8])

    def test_idempotent(self):
        samples = [c[0] for c in ALL_CASES] + [
            json.dumps({"text": json.dumps({"password": PW})}),
            "Authorization: Bearer abcdef123456 x",
            "https://mail.google.com/inbox?token=abc&x=1&api_key=zzz",
            "https://user:pa55word@h/x",
            "token count was 12",
            "git status",
        ]
        for text in samples:
            with self.subTest(text=text):
                once = gblog.redact(text)
                self.assertEqual(gblog.redact(once), once)

    def test_existing_expectations_unchanged(self):
        self.assertEqual(gblog.redact("Authorization: Bearer abcdef123456 x"), "Authorization: Bearer [REDACTED] x")
        self.assertEqual(
            gblog.redact("https://mail.google.com/inbox?token=abc&x=1&api_key=zzz"),
            "https://mail.google.com/inbox?token=[REDACTED]&x=1&api_key=[REDACTED]",
        )
        self.assertEqual(gblog.redact("https://user:pa55word@h/x"), "https://[REDACTED]@h/x")
        self.assertEqual(gblog.redact("token=abcdef123456"), "token=[REDACTED]")
        self.assertIn("[REDACTED]", gblog.redact("ghp_" + "a" * 36))
        self.assertIn("[REDACTED]", gblog.redact("AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertIn("[REDACTED]", gblog.redact("eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "abcdefghijklmnop"))
        self.assertIsNone(gblog.redact(None))
        self.assertEqual(gblog.redact(""), "")

    def test_prose_untouched(self):
        for text in (
            "token count was 12",
            "author=Jane Doe",
            "basic authentication failed",
            "the secret to success",
            "git status",
            "open https://example.com/a/b",
            "the key is under the mat",
        ):
            with self.subTest(text=text):
                self.assertEqual(gblog.redact(text), text)


class RedactionOnDiskTest(unittest.TestCase):
    """The new shapes must be scrubbed on the two paths that matter most: shell targets and archived transcripts."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gblog-redact-")
        self.ledger = gblog.Ledger(self.root)
        self.ledger.init()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_append_scrubs_env_flag_and_json_shapes(self):
        second = "s3condS3cret99"
        third = "abcdef0123456789"
        rec = self.ledger.append("ops", {
            "type": "action", "action": "shell", "outcome": "ok", "source": "auto",
            "target": "DB_PASSWORD=%s ./deploy.sh --password %s" % (PW, second),
            "detail": json.dumps({"api_key": third}),
            "ts": "2026-01-05T09:00:00Z",
        })
        self.assertIn("[REDACTED]", rec["target"])
        self.assertIn("./deploy.sh", rec["target"])
        with open(self.ledger.log_files("ops")[0], "rb") as fh:
            raw = fh.read()
        for secret in (PW, second, third):
            self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertTrue(self.ledger.verify("ops")["ok"])

    def test_archive_scrubs_jsonl_transcript(self):
        data = ('{"role":"user","text":"hi"}\n'
                '{"tool":"env","output":{"DB_PASSWORD":"%s","note":"x"}}\n' % PW).encode("utf-8")
        rec = gblog.archive_file(self.ledger, "ops", data, "t.jsonl", "transcript", ts="2026-01-05T09:00:00Z")
        self.assertTrue(rec["redacted"])
        with gzip.open(os.path.join(self.root, rec["path"]), "rb") as fh:
            stored = fh.read()
        self.assertNotIn(PW.encode("utf-8"), stored)
        self.assertIn(b"[REDACTED]", stored)
        self.assertIn(b'"note":"x"', stored)
        self.assertTrue(self.ledger.verify("ops")["ok"])


if __name__ == "__main__":
    unittest.main()
