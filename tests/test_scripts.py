"""Shell-level checks (audit I-31): the embedded bash hook, install.sh and the bin/gblog launcher.

Everything runs against temporary HOME / prefix / ledger directories. The piped-install
test fetches gblog.py through a file:// URL of this checkout, so no network is used.
"""
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import gblog  # noqa: E402

BASH = shutil.which("bash")
PYTHON3 = shutil.which("python3")
CURL = shutil.which("curl")
INSTALL = os.path.join(REPO, "install.sh")
LAUNCHER = os.path.join(REPO, "bin", "gblog")
VERSION_LINE = "gblog " + gblog.__version__
_PLAIN = re.compile(r"^[A-Za-z0-9_./-]+$")  # paths that `printf %q` leaves untouched


def _read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


@unittest.skipUnless(BASH, "bash not installed")
class ScriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gblog-scripts-"))
        self.home = os.path.join(self.tmp, "home")
        self.prefix = os.path.join(self.tmp, "prefix", "bin")
        self.root = os.path.join(self.tmp, "ledger")
        os.makedirs(self.home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, **extra):
        env = dict(os.environ)
        for key in ("GBLOG_REPO", "GBLOG_REF", "GBLOG_SHA256", "GBLOG_BOT", "GBLOG_TASK", "ZDOTDIR", "ENV", "BASH_ENV"):
            env.pop(key, None)
        # SHELL is pinned: install.sh also writes the login shell's own startup file (zsh, bash on macOS)
        env.update({"HOME": self.home, "GBLOG_PREFIX": self.prefix, "GBLOG_ROOT": self.root, "SHELL": "/bin/sh"})
        env.update(extra)
        return env

    def _sh(self, argv, **kwargs):
        kwargs.setdefault("env", self._env())
        kwargs.setdefault("cwd", self.tmp)
        kwargs.setdefault("timeout", 180)
        if "stdin" not in kwargs:
            kwargs["stdin"] = subprocess.DEVNULL
        return subprocess.run(argv, capture_output=True, text=True, **kwargs)

    def _assert_installed_version(self):
        installed = os.path.join(self.prefix, "gblog")
        self.assertTrue(os.path.isfile(installed), installed)
        self.assertTrue(os.access(installed, os.X_OK), installed)
        res = self._sh([installed, "--version"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), VERSION_LINE)
        return installed

    def test_shell_hook_is_valid_bash(self):
        hook = gblog.shell_hook("ci")
        self.assertIn("GBLOG_BOT:-ci", hook)
        path = os.path.join(self.tmp, "hook.sh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(hook)
        res = self._sh([BASH, "-n", path])
        self.assertEqual(res.returncode, 0, res.stderr)
        # sourcing it in a plain non-interactive shell must not fail either
        res = self._sh([BASH, "--noprofile", "--norc", "-c", 'source "$1"', "_", path])
        self.assertEqual(res.returncode, 0, res.stderr)

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_from_checkout_into_temp_home(self):
        bashrc = os.path.join(self.home, ".bashrc")
        with open(bashrc, "w", encoding="utf-8") as fh:
            fh.write("# existing line\n")
        res = self._sh([BASH, INSTALL])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self._assert_installed_version()
        self.assertEqual(_read(os.path.join(self.prefix, "gblog"), "rb"), _read(os.path.join(REPO, "gblog.py"), "rb"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "logs")))
        text = _read(bashrc)
        self.assertIn("# existing line", text)
        self.assertIn("gblog PATH", text)
        self.assertIn("gblog ROOT", text)
        self.assertIn("GBLOG_ROOT", text)
        if _PLAIN.match(self.tmp):
            self.assertIn(self.prefix, text)
            self.assertIn(self.root, text)
        # a rerun is idempotent
        res = self._sh([BASH, INSTALL])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = _read(bashrc)
        self.assertEqual(text.count("gblog PATH"), 1, text)
        self.assertEqual(text.count("gblog ROOT"), 1, text)
        self._assert_installed_version()

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_bin_launcher_through_symlink(self):
        dir_a = os.path.join(self.tmp, "a")
        dir_b = os.path.join(self.tmp, "b")
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        absolute = os.path.join(dir_a, "gblog")
        relative = os.path.join(dir_b, "gb")
        try:
            os.symlink(LAUNCHER, absolute)                       # absolute link to the launcher
            os.symlink(os.path.join("..", "a", "gblog"), relative)  # relative link to that link
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks not supported here")
        for link in (absolute, relative):
            res = self._sh([BASH, link, "--version"])
            self.assertEqual(res.returncode, 0, "%s: %s" % (link, res.stderr))
            self.assertEqual(res.stdout.strip(), VERSION_LINE, link)

    @unittest.skipUnless(PYTHON3 and CURL, "python3 and curl are required")
    def test_install_piped_ignores_cwd_gblog_py(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(cwd)
        with open(os.path.join(cwd, "gblog.py"), "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\nprint('decoy gblog 0.0.0')\n")
        env = self._env(GBLOG_REPO=pathlib.Path(REPO).as_uri())
        with open(INSTALL, "rb") as script:
            res = subprocess.run([BASH], stdin=script, cwd=cwd, env=env, capture_output=True, timeout=180)
        out = res.stdout.decode("utf-8", "replace")
        err = res.stderr.decode("utf-8", "replace")
        if res.returncode != 0 and ("not supported" in err.lower() or "unsupported protocol" in err.lower()):
            self.skipTest("this curl cannot read file:// URLs")
        self.assertEqual(res.returncode, 0, out + err)
        installed = self._assert_installed_version()
        data = _read(installed, "rb")
        self.assertNotIn(b"decoy", data)
        self.assertEqual(data, _read(os.path.join(REPO, "gblog.py"), "rb"))
        self.assertNotIn("decoy", out)
        self.assertNotIn("local checkout", out)

    # ----------------------------------------------------------------------- R-26 (install.sh)
    def _install(self, **extra):
        return self._sh([BASH, INSTALL], env=self._env(**extra))

    def _rc(self, name):
        return os.path.join(self.home, name)

    def _write_rc(self, name, text):
        with open(self._rc(name), "w", encoding="utf-8") as fh:
            fh.write(text)
        return self._rc(name)

    def _assert_rerun_is_a_noop(self, path, **extra):
        before = _read(path, "rb")
        res = self._install(**extra)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_read(path, "rb"), before)

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_relative_root_is_persisted_absolute(self):
        res = self._install(GBLOG_ROOT="rel-ledger")  # relative to the working directory, self.tmp
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        absolute = os.path.join(self.tmp, "rel-ledger")
        self.assertTrue(os.path.isdir(os.path.join(absolute, "logs")))
        self.assertIn("ledger root: " + absolute, res.stdout)
        lines = _read(self._rc(".profile")).splitlines()
        self.assertNotIn("export GBLOG_ROOT=rel-ledger", lines)
        self.assertEqual(len([line for line in lines if line.startswith("export GBLOG_ROOT=")]), 1, lines)
        if _PLAIN.match(self.tmp):
            self.assertIn("export GBLOG_ROOT=" + absolute, lines)
        self._assert_rerun_is_a_noop(self._rc(".profile"), GBLOG_ROOT="rel-ledger")

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_without_rc_file_creates_profile(self):
        res = self._install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        profile = self._rc(".profile")
        self.assertTrue(os.path.isfile(profile))
        text = _read(profile)
        self.assertEqual(text.count("# gblog PATH"), 1, text)
        self.assertEqual(text.count("# gblog ROOT"), 1, text)
        self.assertIn("export GBLOG_ROOT=", text)
        self.assertIn(profile, res.stdout)  # the script says which file it touched
        for name in (".bashrc", ".zshrc", ".bash_profile"):
            self.assertFalse(os.path.exists(self._rc(name)), name)
        self._assert_rerun_is_a_noop(profile)
        self._assert_installed_version()

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_handles_zshrc(self):
        zshrc = self._write_rc(".zshrc", "# zsh settings\n")
        res = self._install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = _read(zshrc)
        self.assertIn("# zsh settings", text)
        self.assertEqual(text.count("# gblog PATH"), 1, text)
        self.assertEqual(text.count("# gblog ROOT"), 1, text)
        self.assertFalse(os.path.exists(self._rc(".profile")))  # an rc file already existed
        self._assert_rerun_is_a_noop(zshrc)

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_handles_bash_profile(self):
        # bash login shells read ~/.bash_profile instead of ~/.profile
        bash_profile = self._write_rc(".bash_profile", "# login shell settings\n")
        res = self._install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = _read(bash_profile)
        self.assertIn("# login shell settings", text)
        self.assertEqual(text.count("# gblog PATH"), 1, text)
        self.assertEqual(text.count("# gblog ROOT"), 1, text)
        self.assertIn("export GBLOG_ROOT=", text)
        self._assert_rerun_is_a_noop(bash_profile)

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_respects_legacy_unmarked_lines(self):
        if not _PLAIN.match(self.tmp):
            self.skipTest("the temporary path needs shell quoting")
        legacy = "# added by an older install.sh\nexport PATH=\"%s:$PATH\"\nexport GBLOG_ROOT=%s\n" % (self.prefix, self.root)
        bashrc = self._write_rc(".bashrc", legacy)
        res = self._install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_read(bashrc), legacy)  # both lines are already there: no second block
        self._assert_installed_version()
        self._assert_rerun_is_a_noop(bashrc)

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_install_warns_about_a_different_unmarked_root(self):
        if not _PLAIN.match(self.tmp):
            self.skipTest("the temporary path needs shell quoting")
        legacy = "export PATH=\"%s:$PATH\"\nexport GBLOG_ROOT=/somewhere/else/ledger\n" % self.prefix
        bashrc = self._write_rc(".bashrc", legacy)
        res = self._install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_read(bashrc), legacy)  # left alone
        said = res.stdout + res.stderr
        self.assertIn("warning:", said)
        self.assertIn("/somewhere/else/ledger", said)
        self.assertIn(self.root, said)
        self._assert_installed_version()

    @unittest.skipUnless(PYTHON3 and (shutil.which("sha256sum") or shutil.which("shasum")),
                         "python3 and sha256sum or shasum are required")
    def test_install_checksum_is_enforced(self):
        good = hashlib.sha256(_read(os.path.join(REPO, "gblog.py"), "rb")).hexdigest()
        res = self._install(GBLOG_SHA256="0" * 64)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("sha256 mismatch", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "gblog")))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "logs")))
        for name in (".bashrc", ".zshrc", ".profile", ".bash_profile"):
            self.assertFalse(os.path.exists(self._rc(name)), name)
        res = self._install(GBLOG_SHA256=good.upper())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("sha256 verified: " + good, res.stdout)
        self._assert_installed_version()


if __name__ == "__main__":
    unittest.main()
