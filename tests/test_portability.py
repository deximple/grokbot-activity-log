"""Portability tests for the shell side of gblog (specialist review K7, 2026-11-16).

Covered: install.sh (startup lines every shell reads back, K7-03; the login shell's own file and
ZDOTDIR, K7-04; no usable HOME, K7-07; atomic replacement of the installed file, K7-11; spellings
of existing lines, K7-13), bin/gblog (an interpreter older than 3.8, K7-16), the example scripts
(K7-14) and .gitattributes (K7-18).

Everything runs against temporary HOME, prefix and ledger directories; no network is used. A test
is skipped where the platform lacks what it needs (bash, a second shell, non-ASCII file names).
"""
import io
import os
import re
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
PYTHON3 = shutil.which("python3")
GBLOG_PY = os.path.join(REPO, "gblog.py")
INSTALL = os.path.join(REPO, "install.sh")
LAUNCHER = os.path.join(REPO, "bin", "gblog")
AUDIT = os.path.join(REPO, "examples", "routine-daily-audit.sh")
TASK = os.path.join(REPO, "examples", "agent-task.sh")
SCRIPTS = (INSTALL, LAUNCHER, AUDIT, TASK)
VERSION_LINE = "gblog " + gblog.__version__
_PLAIN = re.compile(r"^[A-Za-z0-9_./-]+$")
PIPE, DEVNULL = subprocess.PIPE, subprocess.DEVNULL
READ_BACK = '. "$HOME/$1" && printf "%s\\n%s" "$GBLOG_ROOT" "${PATH%%:*}"'


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


@unittest.skipUnless(BASH and os.name == "posix", "bash on POSIX is required")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gblog-port-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.home)
        os.makedirs(self.cwd)
        self.prefix = os.path.join(self.tmp, "prefix", "bin")
        self.root = os.path.join(self.tmp, "ledger")

    def env(self, **extra):
        """The caller's environment without gblog settings; ``None`` removes a variable."""
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("GBLOG_") or key in ("ZDOTDIR", "ENV", "BASH_ENV", "CDPATH"):
                del env[key]
        env.update({"HOME": self.home, "SHELL": "/bin/sh", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def sh(self, argv, env=None, timeout=180):
        proc = subprocess.run(argv, cwd=self.cwd, env=env or self.env(), stdin=DEVNULL, stdout=PIPE, stderr=PIPE,
                              timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def install(self, prefix=None, root=None, **extra):
        return self.sh([BASH, INSTALL], self.env(GBLOG_PREFIX=prefix or self.prefix, GBLOG_ROOT=root or self.root, **extra))

    def odd_dir(self):
        """A directory whose name has a space, a quote, a dollar sign and (where possible) a non-ASCII letter."""
        for name in ("od d \u00e9'$x", "od d '$x"):
            path = os.path.join(self.tmp, name)
            try:
                os.makedirs(path)
                os.fsencode(path)
                return path
            except (OSError, UnicodeError):
                continue
        self.skipTest("this file system does not take such a directory name")

    def markers(self, path):
        data = _read(path)
        return data.count(b"# gblog PATH"), data.count(b"# gblog ROOT")

    def assert_version(self, binary):
        rc, out, err = self.sh([binary, "--version"])
        self.assertEqual((rc, out.strip()), (0, VERSION_LINE), err)


# --------------------------------------------------------------------------- install.sh
@unittest.skipUnless(PYTHON3, "python3 not on PATH")
class InstallQuotingTest(Base):
    def test_startup_lines_are_read_back_by_every_shell(self):
        # K7-03: the old `printf %q` output ($'...') is not POSIX; dash read a wrong GBLOG_ROOT from it
        odd = self.odd_dir()
        prefix, root = os.path.join(odd, "bin"), os.path.join(odd, "ledger")
        rc, out, err = self.install(prefix, root)
        self.assertEqual(rc, 0, out + err)
        profile = os.path.join(self.home, ".profile")
        before = _read(profile)
        self.assertNotIn(b"=$'", before)
        self.assert_version(os.path.join(prefix, "gblog"))
        self.assertTrue(os.path.isdir(os.path.join(root, "logs")))
        want = os.fsencode(root) + b"\n" + os.fsencode(prefix)
        shells = [name for name in ("sh", "dash", "bash", "zsh", "ksh") if shutil.which(name)]
        self.assertTrue(shells)
        for name in shells:
            with self.subTest(shell=name):
                proc = subprocess.run([name, "-c", READ_BACK, "_", ".profile"], cwd=self.cwd, stdin=DEVNULL,
                                      stdout=PIPE, stderr=PIPE, timeout=60,
                                      env={"HOME": self.home, "PATH": os.environ.get("PATH", os.defpath)})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, want)
        # a rerun in another locale writes nothing and does not call the same directory "a different value"
        for locale in ("C.UTF-8", "en_US.UTF-8", "C"):
            with self.subTest(rerun=locale):
                rc, out, err = self.install(prefix, root, LC_ALL=locale)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(_read(profile), before)
                self.assertNotIn("different value", err)

    def test_plain_paths_are_written_bare(self):
        # lines for ordinary paths stay byte-identical to what earlier versions wrote
        if not _PLAIN.match(self.tmp):
            self.skipTest("the temporary path needs shell quoting")
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        lines = _read(os.path.join(self.home, ".profile")).decode("ascii").splitlines()
        self.assertIn("export GBLOG_ROOT=" + self.root, lines)
        self.assertIn('export PATH=%s:"$PATH"' % self.prefix, lines)

    def _percent_q(self, text):
        return subprocess.run([BASH, "-c", 'printf %q "$1"', "_", text], env=self.env(), stdout=PIPE, check=True).stdout

    def test_lines_written_by_an_older_installer_are_recognised(self):
        # K7-03: the old spelling of an odd path must not be duplicated or reported as another root
        odd = self.odd_dir()
        prefix, root = os.path.join(odd, "bin"), os.path.join(odd, "ledger")
        path_line = b"export PATH=" + self._percent_q(prefix) + b':"$PATH"\n'
        root_line = b"export GBLOG_ROOT=" + self._percent_q(root) + b"\n"
        for marked in (True, False):
            with self.subTest(marked=marked):
                home = os.path.join(self.tmp, "home-%d" % marked)
                os.makedirs(home)
                text = (b"\n# gblog PATH\n" + path_line + b"\n# gblog ROOT\n" + root_line) if marked else path_line + root_line
                bashrc = _write(os.path.join(home, ".bashrc"), text)
                rc, out, err = self.install(prefix, root, HOME=home)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(_read(bashrc), text)
                self.assertNotIn("different value", err)
                self.assertNotIn("warning:", err)
                self.assertEqual(os.listdir(home), [".bashrc"])

    def test_home_relative_spellings_and_trailing_text_are_recognised(self):
        # K7-13: ~/..., $HOME/..., ${HOME}/..., a trailing comment, `VAR=...; export VAR`
        cases = [
            'export PATH=~/.local/bin:$PATH\nexport GBLOG_ROOT=~/ledger\n',
            'export PATH="$HOME/.local/bin:$PATH"\nexport GBLOG_ROOT="$HOME/ledger"  # the ledger\n',
            'PATH=${HOME}/.local/bin:$PATH; export PATH\nGBLOG_ROOT=$HOME/ledger; export GBLOG_ROOT\n',
        ]
        if "'" not in self.tmp:
            cases.append("export PATH=~/.local/bin:$PATH\n  export GBLOG_ROOT='%s/home-3/ledger'\n" % self.tmp)
        for i, text in enumerate(cases):
            with self.subTest(case=text):
                home = os.path.join(self.tmp, "home-%d" % i)
                os.makedirs(home)
                bashrc = _write(os.path.join(home, ".bashrc"), text)
                rc, out, err = self.install(os.path.join(home, ".local", "bin"), os.path.join(home, "ledger"), HOME=home)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(_read(bashrc), text.encode("utf-8"))
                self.assertNotIn("warning:", err)
                self.assertNotIn("note:", err)
                self.assertFalse(os.path.exists(os.path.join(home, ".profile")))
                self.assertIn("already configured", out)

    def test_a_different_root_is_still_reported(self):
        text = "export GBLOG_ROOT=~/somewhere-else  # old\n"
        bashrc = _write(os.path.join(self.home, ".bashrc"), text)
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warning:", err)
        self.assertIn("somewhere-else", err)
        self.assertEqual(_read(bashrc).count(b"GBLOG_ROOT="), 1)

    def test_plain_sh_is_told_to_use_bash(self):
        # K7-13: `sh install.sh` on dash used to die with "Illegal option -o pipefail"
        for name in ("dash", "sh"):
            shell = shutil.which(name)
            if shell and not subprocess.run([shell, "-c", 'printf %s "${BASH_VERSION:-}"'], stdin=DEVNULL,
                                            stdout=PIPE, stderr=DEVNULL).stdout:
                break
        else:
            self.skipTest("every sh here is bash")
        rc, out, err = self.sh([shell, INSTALL], self.env(GBLOG_PREFIX=self.prefix, GBLOG_ROOT=self.root))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("install.sh needs bash", err)
        self.assertFalse(os.path.exists(self.prefix))
        self.assertEqual(os.listdir(self.home), [])


@unittest.skipUnless(PYTHON3, "python3 not on PATH")
class InstallLoginShellTest(Base):
    def test_zsh_login_shell_gets_zshrc(self):
        # K7-04: a fresh macOS account has zsh and no rc file; zsh never reads ~/.profile
        rc, out, err = self.install(SHELL="/bin/zsh")
        self.assertEqual(rc, 0, out + err)
        zshrc = os.path.join(self.home, ".zshrc")
        self.assertEqual(os.listdir(self.home), [".zshrc"])
        self.assertEqual(self.markers(zshrc), (1, 1))
        before = _read(zshrc)
        rc, out, err = self.install(SHELL="/bin/zsh")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(_read(zshrc), before)
        self.assertEqual(os.listdir(self.home), [".zshrc"])
        if shutil.which("zsh"):
            proc = subprocess.run(["zsh", "-c", READ_BACK, "_", ".zshrc"], cwd=self.cwd, stdin=DEVNULL, stdout=PIPE,
                                  stderr=PIPE, timeout=60, env={"HOME": self.home, "PATH": os.environ.get("PATH", os.defpath)})
            self.assertEqual(proc.stdout, os.fsencode(self.root) + b"\n" + os.fsencode(self.prefix), proc.stderr)

    def test_zdotdir_is_honoured(self):
        zdot = os.path.join(self.tmp, "zdot")
        os.makedirs(zdot)
        rc, out, err = self.install(SHELL="/usr/bin/zsh", ZDOTDIR=zdot)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.markers(os.path.join(zdot, ".zshrc")), (1, 1))
        self.assertEqual(os.listdir(self.home), [])

    def test_zsh_with_only_a_profile_gets_both_files(self):
        _write(os.path.join(self.home, ".profile"), "# mine\n")
        rc, out, err = self.install(SHELL="/bin/zsh")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(os.listdir(self.home)), [".profile", ".zshrc"])
        for name in (".profile", ".zshrc"):
            self.assertEqual(self.markers(os.path.join(self.home, name)), (1, 1), name)

    def test_existing_zshrc_is_not_duplicated(self):
        zshrc = _write(os.path.join(self.home, ".zshrc"), "# zsh settings\n")
        for _ in range(2):
            rc, out, err = self.install(SHELL="/bin/zsh")
            self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.markers(zshrc), (1, 1))
        self.assertEqual(os.listdir(self.home), [".zshrc"])

    def test_bash_login_shell_file(self):
        # Terminal on macOS starts login shells: bash reads ~/.bash_profile there. Elsewhere ~/.profile is created.
        rc, out, err = self.install(SHELL="/bin/bash")
        self.assertEqual(rc, 0, out + err)
        expected = ".bash_profile" if sys.platform == "darwin" else ".profile"
        self.assertEqual(os.listdir(self.home), [expected])
        self.assertEqual(self.markers(os.path.join(self.home, expected)), (1, 1))


@unittest.skipUnless(PYTHON3, "python3 not on PATH")
class InstallEnvironmentTest(Base):
    def test_empty_home_installs_without_touching_startup_files(self):
        # K7-07: used to stop half-way (or to install under "/")
        rc, out, err = self.install(HOME="")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("no shell startup file could be written", out)
        self.assertNotIn("already configured", out)
        self.assertNotIn("unbound variable", err)
        self.assert_version(os.path.join(self.prefix, "gblog"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "logs")))
        self.assertEqual(os.listdir(self.cwd), [])
        self.assertEqual(os.listdir(self.home), [])

    def test_empty_home_needs_prefix_and_root(self):
        for missing in ("GBLOG_PREFIX", "GBLOG_ROOT"):
            with self.subTest(missing=missing):
                settings = {"GBLOG_PREFIX": self.prefix, "GBLOG_ROOT": self.root, "HOME": ""}
                settings[missing] = None
                rc, out, err = self.sh([BASH, INSTALL], self.env(**settings))
                self.assertEqual(rc, 1, out + err)
                self.assertIn("HOME is unset or empty", err)
                self.assertNotIn("unbound variable", err)
                self.assertFalse(os.path.exists(self.prefix))
                self.assertFalse(os.path.exists(self.root))
                self.assertEqual(os.listdir(self.cwd), [])

    def test_tilde_in_prefix_and_root_is_expanded(self):
        # a value set by cron or systemd reaches the script unexpanded
        rc, out, err = self.sh([BASH, INSTALL], self.env(GBLOG_PREFIX="~/bin", GBLOG_ROOT="~/ledger"))
        self.assertEqual(rc, 0, out + err)
        self.assert_version(os.path.join(self.home, "bin", "gblog"))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "ledger", "logs")))
        self.assertEqual(os.listdir(self.cwd), [])

    def test_upgrade_never_exposes_a_half_written_binary(self):
        # K7-11: other Bots call gblog while install.sh replaces it
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        binary = os.path.join(self.prefix, "gblog")
        stop = os.path.join(self.tmp, "stop")
        loop = subprocess.Popen([BASH, "-c", 'while [ ! -e "$1" ]; do "$2" --version 2>&1 || echo "FAILED $?"; done',
                                 "_", stop, binary], cwd=self.cwd, env=self.env(), stdin=DEVNULL, stdout=PIPE,
                                stderr=subprocess.STDOUT)
        try:
            for _ in range(4):
                rc, out, err = self.install()
                self.assertEqual(rc, 0, out + err)
        finally:
            _write(stop, b"")
            try:
                seen, _ = loop.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                loop.kill()
                seen, _ = loop.communicate()
        lines = seen.decode("utf-8", "replace").splitlines()
        self.assertTrue(lines)
        self.assertEqual([line for line in lines if line != VERSION_LINE], [])
        self.assertEqual(os.listdir(self.prefix), ["gblog"])  # no temporary file is left behind
        self.assertTrue(os.access(binary, os.X_OK))


# --------------------------------------------------------------------------- bin/gblog
class LauncherTest(Base):
    def test_old_python3_gets_one_plain_line(self):
        # K7-16: RHEL 8's python3 is 3.6, where gblog.py is a SyntaxError
        stub = os.path.join(self.tmp, "stub")
        os.makedirs(stub)
        fake = _write(os.path.join(stub, "python3"),
                      "#!/bin/sh\ncase \"$2\" in\n  *'< (3, 8)'*) exit 1 ;;\n  *) echo 3.6 ;;\nesac\n")
        os.chmod(fake, 0o755)
        env = self.env(PATH=stub + os.pathsep + os.environ.get("PATH", os.defpath))
        rc, out, err = self.sh([BASH, LAUNCHER, "--version"], env)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(out, "")
        self.assertEqual(err, "gblog needs Python 3.8 or newer (python3 is 3.6)\n")

    @unittest.skipUnless(PYTHON3, "python3 not on PATH")
    def test_launcher_runs_from_a_directory_with_a_space(self):
        odd = self.odd_dir()
        link = os.path.join(odd, "gb log")
        try:
            os.symlink(LAUNCHER, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not supported here")
        for argv in ([BASH, LAUNCHER, "--version"], [BASH, link, "--version"]):
            rc, out, err = self.sh(argv, self.env(CDPATH="/usr"))
            self.assertEqual((rc, out.strip()), (0, VERSION_LINE), err)


# --------------------------------------------------------------------------- examples/*.sh
class ExamplesTest(Base):
    def setUp(self):
        super().setUp()
        self.gblog_bin = _write(os.path.join(self.tmp, "gblog-bin"),
                                "#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(GBLOG_PY)))
        os.chmod(self.gblog_bin, 0o755)

    def seed(self, root, n=2):
        ledger = gblog.Ledger(root)
        with redirect_stderr(io.StringIO()):
            for i in range(n):
                ledger.append("ops", {"type": "note", "detail": "record %d" % (i + 1), "ts": "2024-06-01T10:00:%02dZ" % i})
        return ledger

    def audit(self, **extra):
        return self.sh([BASH, AUDIT], self.env(GBLOG_BIN=self.gblog_bin, **extra))

    def test_audit_expands_a_tilde_root(self):
        # K7-14: GBLOG_ROOT=~/... from cron used to end in "ledger missing" although gblog finds the ledger
        self.seed(os.path.join(self.home, "ledger"))
        rc, out, err = self.audit(GBLOG_ROOT="~/ledger")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RESULT: ok", out)
        self.assertIn("ops OK ", out)
        self.assertEqual(os.listdir(self.cwd), [])

    def test_audit_with_spaces_and_glob_characters_in_the_root(self):
        root = os.path.join(self.tmp, "led[g]er * x")
        ledger = self.seed(root)
        rc, out, err = self.audit(GBLOG_ROOT=root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RESULT: ok", out)
        head = [rec for _p, _n, rec in ledger.iter_records("ops")][-1]["hash"]
        self.assertIn("ops OK %s" % head, out.splitlines())
        # a pending alert is still found under such a root
        with redirect_stderr(io.StringIO()):
            ledger.write_state("ops", {"seq": 99, "head": "cd" * 32})
        rc, out, err = self.audit(GBLOG_ROOT=root)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("RESULT: ALERT", out)
        self.assertTrue(os.path.isfile(os.path.join(root, "state", "ops.alert.json")))

    def test_agent_task_logs_only_plain_ascii_program_names(self):
        # K7-14: "only a plain name is logged" must not depend on the locale (bracket ranges in bash 3.2)
        if sys.getfilesystemencoding().lower().replace("-", "") != "utf8":
            self.skipTest("a non-ASCII argument cannot be passed here")
        root = os.path.join(self.tmp, "ledger")
        locales = ("C", "C.UTF-8", "en_US.UTF-8")
        for locale in locales:
            rc, out, err = self.sh([BASH, TASK, "--bot", "ops", "--", "caf\u00e9-tool", "--secret-arg"],
                                   self.env(GBLOG_BIN=self.gblog_bin, GBLOG_ROOT=root, LC_ALL=locale))
            self.assertEqual(rc, 127, out + err)
        rc, out, err = self.sh([BASH, TASK, "--bot", "ops", "--", "no-such_tool.sh+x"],
                               self.env(GBLOG_BIN=self.gblog_bin, GBLOG_ROOT=root))
        self.assertEqual(rc, 127, out + err)
        recs = [rec for _p, _n, rec in gblog.Ledger(root).iter_records("ops")]
        self.assertEqual([r["target"] for r in recs if r["type"] == "action"],
                         ["command"] * len(locales) + ["no-such_tool.sh+x"])
        self.assertEqual([r["task"] for r in recs if r["type"] == "task_start"],
                         ["Run command"] * len(locales) + ["Run no-such_tool.sh+x"])
        with open(gblog.Ledger(root).log_files("ops")[0], "rb") as fh:
            data = fh.read()
        self.assertNotIn(b"caf", data)
        self.assertNotIn(b"secret-arg", data)


# --------------------------------------------------------------------------- repository files
class RepoFilesTest(Base):
    # constructs that bash 3.2 (macOS /bin/bash) does not have
    BASH4 = re.compile(rb"declare -A|\bmapfile\b|\breadarray\b|\bcoproc\b|globstar|\|&|&>>"
                       rb"|\$\{[A-Za-z_][A-Za-z0-9_]*(\^|,)")

    def test_scripts_parse_and_have_lf_line_endings(self):
        for path in SCRIPTS:
            with self.subTest(script=os.path.basename(path)):
                data = _read(path)
                self.assertNotIn(b"\r", data)
                self.assertTrue(data.startswith(b"#!/usr/bin/env bash\n"))
                rc, out, err = self.sh([BASH, "-n", path])
                self.assertEqual(rc, 0, out + err)

    def test_scripts_avoid_bash_4_constructs(self):
        for path in SCRIPTS:
            with self.subTest(script=os.path.basename(path)):
                self.assertIsNone(self.BASH4.search(_read(path)))

    def test_startup_lines_are_not_built_with_printf_q(self):
        # `printf %q` may only be used to recognise lines an older installer wrote
        text = _read(INSTALL).decode("utf-8")
        for name in ("path_line", "root_line"):
            line = [l for l in text.splitlines() if l.startswith(name + "=")]
            self.assertEqual(len(line), 1, name)
            self.assertNotIn("%q", line[0])
            self.assertIn("$(sq ", line[0])

    def test_gitattributes_keeps_scripts_and_fixtures_byte_exact(self):
        # K7-18: CRLF conversion would break the bash scripts and the byte-compared fixtures
        path = os.path.join(REPO, ".gitattributes")
        self.assertTrue(os.path.isfile(path))
        rules = {}
        for line in _read(path).decode("utf-8").splitlines():
            parts = line.split()
            if parts and not parts[0].startswith("#"):
                rules[parts[0]] = parts[1:]
        self.assertIn("eol=lf", rules.get("*.sh", []))
        self.assertIn("eol=lf", rules.get("bin/*", []))
        self.assertEqual(rules.get("*.jsonl"), ["-text"])


if __name__ == "__main__":
    unittest.main()
