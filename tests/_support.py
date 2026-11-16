"""Shared helpers for the test modules (round-3 audit, W-20).

``unittest discover`` does not collect this file (its name does not start with ``test``).
Test modules import it with::

    sys.path.insert(0, HERE)
    from _support import run

One implementation of the in-process CLI runner replaces the copies the modules used to carry.
The modules differ in what they expect of an exception, so ``run`` and ``run_raw`` take three
switches; each module binds the ones its tests depend on.
"""
import contextlib
import datetime as _dt
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import gblog  # noqa: E402

GBLOG_PY = os.path.join(REPO, "gblog.py")
DAY = "2024-06-01"

# git never prompts, never reads the user's or the system's configuration, and only uses local transports.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "file",
}


def run_raw(argv, fold_exit=True, catch=True, catch_interrupt=False):
    """Run ``gblog.main(argv)`` in-process and return ``(rc, stdout, stderr)``.

    * ``fold_exit`` (default): a ``SystemExit`` becomes the return code. With ``fold_exit=False``
      ``SystemExit`` and every other exception propagate to the caller.
    * ``catch`` (default): any other exception is rendered like the real CLI would show it
      (a traceback on stderr, exit 1), so a test can assert that no traceback was printed.
    * ``catch_interrupt``: a ``KeyboardInterrupt`` that escapes ``main`` is rendered the same way.
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = gblog.main(list(argv))
        except SystemExit as exc:
            if not fold_exit:
                raise
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                err.write("%s\n" % code)
                rc = 1
        except KeyboardInterrupt:
            if not (fold_exit and catch and catch_interrupt):
                raise
            err.write("Traceback (most recent call last):\nKeyboardInterrupt\n")
            rc = 1
        except Exception:
            if not (fold_exit and catch):
                raise
            traceback.print_exc(file=err)
            rc = 1
    return rc, out.getvalue(), err.getvalue()


def run(argv, root, **options):
    """``run_raw`` with ``--root <root>`` in front of ``argv``."""
    return run_raw(["--root", root] + list(argv), **options)


def run_cli(argv, root, encoding="utf-8", extra_env=None, timeout=120):
    """Run the real CLI in a fresh process whose stdout and stderr use ``encoding``.

    Needed where the stream encoding or the environment (``TMPDIR``, ...) is what is under test.
    Returns ``(rc, stdout, stderr)`` as text."""
    env = dict(os.environ)
    for key in ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT", "PYTHONUTF8"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = encoding
    env.update(extra_env or {})
    proc = subprocess.run([sys.executable, GBLOG_PY, "--root", root] + list(argv), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def stamp(n, day=DAY):
    """A fixed timestamp: second ``n`` after 10:00:00 UTC on ``day``."""
    return "%sT10:%02d:%02dZ" % (day, n // 60, n % 60)


def deep_nesting(default):
    """Nesting depth guaranteed to make ``json.loads`` raise ``RecursionError``.

    Before Python 3.14 the JSON scanner stops at a fixed recursion limit (at most 10 000 levels), so
    20 000 levels always raise RecursionError. From 3.14 the scanner uses the C stack and can parse
    50 000 nested objects without raising; probing once finds a depth past the current limit."""
    if sys.version_info < (3, 14):
        return 20000
    depth = max(default, 50000)
    while depth <= 8_000_000:
        try:
            json.loads('{"a":' * depth + "1" + "}" * depth)
        except RecursionError:
            return depth
        depth *= 2
    return depth


# --------------------------------------------------------------------------- strict runner (review K6-01)
# main() turns an unexpected exception into "error: internal error" (exit 1) and verify/anchor turn it into
# an "aborted" FAIL entry (exit 1 as well): a test that only expects exit 1 would pass on a crash.
_CRASH_MARKS = (("Traceback", "err"), ("error: internal error", "err"),
                ("verify aborted", "out"), ("anchor aborted", "out"))


def run_strict(argv, root, internal_ok=False, **options):
    """``run``, except that a swallowed crash raises AssertionError unless ``internal_ok`` is set."""
    rc, out, err = run(argv, root, **options)
    if not internal_ok:
        for text, where in _CRASH_MARKS:
            if text in (err if where == "err" else out):
                raise AssertionError("gblog %s: unexpected %r\n%s\n%s"
                                     % (" ".join(str(a) for a in argv), text, out[-2000:], err[-2000:]))
    return rc, out, err


# --------------------------------------------------------------------------- environment (K6-05, K6-06)
_ENV_SCRUB = ("GBLOG_TASK", "GBLOG_ROOT", "GBLOG_DEBUG", "GBLOG_BOT", "GBLOG_BROWSER_HISTORY")
_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
              "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


def _scrubbed(key):
    return key in _GIT_SCRUB or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def isolate_env(test):
    """Patch os.environ for one test: GIT_ENV applied; gblog and repository-pointing git variables removed."""
    patcher = mock.patch.dict(os.environ, GIT_ENV)
    patcher.start()
    test.addCleanup(patcher.stop)
    for key in list(os.environ):
        if key in _ENV_SCRUB or _scrubbed(key):
            del os.environ[key]


def git_env():
    """The environment for a git child of a test: never the repository of whoever runs the suite."""
    env = {k: v for k, v in os.environ.items() if not _scrubbed(k)}
    for key, value in GIT_ENV.items():
        env.setdefault(key, value)
    return env


def git(*args, check=True):
    """Run git for a test and return its stdout (the one helper the test modules share)."""
    return subprocess.run(["git"] + list(args), check=check, capture_output=True, encoding="utf-8",
                          errors="replace", env=git_env()).stdout


# --------------------------------------------------------------------------- clock (K6-07)
@contextlib.contextmanager
def frozen_clock(ts="2024-06-03T12:00:00Z"):
    """Freeze gblog's wall clock: ``now_iso()`` and ``_utcnow()`` both answer ``ts``."""
    when = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    with mock.patch.object(gblog, "now_iso", lambda: ts), mock.patch.object(gblog, "_utcnow", lambda: when):
        yield


def freeze_clock(test, ts="2024-06-03T12:00:00Z"):
    """``frozen_clock`` for the rest of one test."""
    clock = frozen_clock(ts)
    clock.__enter__()
    test.addCleanup(clock.__exit__, None, None, None)


# --------------------------------------------------------------------------- shared builders (round-4 G2-05)
def write_raw_chain(root, bot, extras, day=DAY, state=True):
    """Hash-valid lines written straight to logs/<bot>/<day>.jsonl, bypassing gblog.

    ``extras`` holds one dict per record; its keys override the default fields before the hash is
    computed. With ``state`` the state file names the last record."""
    d = os.path.join(root, "logs", bot)
    os.makedirs(d, exist_ok=True)
    prev, recs = gblog.GENESIS, []
    with open(os.path.join(d, day + ".jsonl"), "w", encoding="utf-8") as fh:
        for i, extra in enumerate(extras, 1):
            rec = {"v": 1, "seq": i, "ts": stamp(i, day), "bot": bot, "type": "note", "detail": "raw %d" % i, "prev": prev}
            rec.update(extra)
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            recs.append(rec)
    if state:
        os.makedirs(os.path.join(root, "state"), exist_ok=True)
        with open(os.path.join(root, "state", bot + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"seq": recs[-1]["seq"], "head": prev, "updated": recs[-1]["ts"]}, fh)
    return recs


def webkit_time(epoch):
    """Epoch seconds as a Chrome visit_time (microseconds since 1601)."""
    return (int(epoch) + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000


def add_visit(path, url, title, when):
    """Add one visit to a Chrome-style History database; ``when`` is the raw visit_time."""
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title))
        conn.execute("INSERT INTO visits(url, visit_time) VALUES (?, ?)", (cur.lastrowid, when))
        conn.commit()
    finally:
        conn.close()


def chrome_history(directory, rows, ago=True):
    """A Chrome-style History database under ``directory``/Default. ``rows`` is [(url, title, when)].

    With ``ago`` (default) ``when`` is seconds before now; otherwise it is the raw visit_time."""
    prof = os.path.join(directory, "Default")
    os.makedirs(prof)
    path = os.path.join(prof, "History")
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                           "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
        conn.commit()
    finally:
        conn.close()
    now = int((time.time() + gblog._WEBKIT_EPOCH_OFFSET) * 1_000_000)
    for url, title, when in rows:
        add_visit(path, url, title, now - when * 1_000_000 if ago else when)
    return path


class FakeBody:
    """Stand-in for the object urlopen() returns."""

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data


class AfterRelease:
    """Wraps a lock context manager and runs a callback right after the lock has been released."""

    def __init__(self, inner, callback):
        self.inner = inner
        self.callback = callback

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        result = self.inner.__exit__(*exc)
        self.callback()
        return result


@contextlib.contextmanager
def after_release(action):
    """Run ``action`` once, right after the first shared bot lock (the snapshot lock of verify) is released."""
    fired, real = [], gblog.Ledger.lock

    def hooked(ledger, bot, *args, **kwargs):
        inner = real(ledger, bot, *args, **kwargs)
        shared = kwargs.get("shared", args[0] if args else False)
        if shared and not fired:
            fired.append(True)
            return AfterRelease(inner, action)
        return inner

    with mock.patch.object(gblog.Ledger, "lock", hooked):
        yield fired


def build_legacy_ledger(root, bot="ops", browser_marker=None):
    """A ledger shaped like the output of gblog 0.2 to 0.5 (K3-08, round-4 G2-04). Returns its records.

    Free-form ``ts`` values (a space separator, a UTC offset), a state file with the single integer
    browser marker of 0.2, an alias entry without ``aliases`` (and a registry without ``strict``),
    and a refused event under the old spool name ``<stamp>-<8 hex>.json`` beside its alert file."""
    recs = write_raw_chain(root, bot, [{"ts": "2024-06-01 09:00:00"}, {"ts": "2024-06-01T10:00:00+09:00"}],
                           day="2024-06-01")
    state_path = os.path.join(root, "state", bot + ".json")
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    if browser_marker is not None:
        state["capture_browser_last"] = browser_marker
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    with open(os.path.join(root, "state", "aliases.json"), "w", encoding="utf-8") as fh:
        json.dump({"bots": {bot: {"display": "Legacy Bot"}}}, fh)
    payload = {"v": 1, "kind": "tail_mismatch", "ts": "2024-06-01T12:00:00Z", "bot": bot,
               "record": {"type": "note", "detail": "refused by 0.5"}}
    os.makedirs(os.path.join(root, "spool", bot))
    for path in (os.path.join(root, "spool", bot, "20240601T120000Z-deadbeef.json"),
                 os.path.join(root, "state", bot + ".alert.json")):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    return recs


# --------------------------------------------------------------------------- crash points (K6-03)
_CRASH_POINTS = ("open", "write", "fsync", "replace", "ftruncate", "mkdir", "remove")


def crash_at(root, scenario, at, half_write=False):
    """Run ``scenario(Ledger(root))`` in a forked child that dies at its ``at``-th patched os call.

    Returns the child's exit code: 77 when it died there, 0 when the scenario finished first, 3 when
    the scenario raised. With ``half_write`` a dying ``os.write`` first writes half of its data.
    ``os._exit`` keeps the page cache: this models a killed process, not a power cut. POSIX only."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # 3.12+: fork while other threads may exist
        pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
            real, calls = {name: getattr(os, name) for name in _CRASH_POINTS}, [0]

            def wrap(name):
                def inner(*args, **kwargs):
                    calls[0] += 1
                    if calls[0] == at:
                        if half_write and name == "write":
                            data = bytes(args[1])
                            real["write"](args[0], data[:max(1, len(data) // 2)])
                        os._exit(77)
                    return real[name](*args, **kwargs)
                return inner

            for name in _CRASH_POINTS:
                setattr(os, name, wrap(name))
            scenario(gblog.Ledger(root))
        except BaseException:
            code = 3
        finally:
            os._exit(code)
    return os.WEXITSTATUS(os.waitpid(pid, 0)[1])


# --------------------------------------------------------------------------- base class for new test modules
class LedgerCase(unittest.TestCase):
    """A temporary ledger root (not created yet) with a scrubbed environment (K6-06).

    gblog is process-safe, not thread-safe (temporary names are per pid): use threads in a test only
    when every write of both sides happens under the same flock; otherwise use processes."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="gblog-test-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "ledger")
        self.ledger = gblog.Ledger(self.root)
        isolate_env(self)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def quiet(self, fn, *args, **kwargs):
        """Call a library function with its stderr notices swallowed."""
        with redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def quiet_err(self, fn, *args, **kwargs):
        """Call a library function; returns ``(result, what it wrote to stderr)``."""
        err = io.StringIO()
        with redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, err.getvalue()

    def seed(self, bot="ops", n=2, day=DAY):
        return [self.quiet(self.ledger.append, bot, {"type": "note", "detail": "%s record %d" % (bot, i + 1),
                                                     "ts": stamp(i, day)}) for i in range(n)]

    def chain(self, bot="ops"):
        return [rec for _path, _lineno, rec in self.ledger.iter_records(bot)]

    def log_bytes(self, bot="ops"):
        data = b""
        for path in self.ledger.log_files(bot):
            with open(path, "rb") as fh:
                data += fh.read()
        return data
