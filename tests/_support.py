"""Shared helpers for the test modules (round-3 audit, W-20).

``unittest discover`` does not collect this file (its name does not start with ``test``).
Test modules import it with::

    sys.path.insert(0, HERE)
    from _support import run

One implementation of the in-process CLI runner replaces the copies the modules used to carry.
The modules differ in what they expect of an exception, so ``run`` and ``run_raw`` take three
switches; each module binds the ones its tests depend on.
"""
import io
import os
import subprocess
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

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
    for key in ("GBLOG_TASK", "GBLOG_ROOT", "PYTHONUTF8"):
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
    """Nesting depth for the "deeply nested JSON" tests.

    Before Python 3.14 the JSON scanner stops at a fixed recursion limit (at most 10 000 levels), so
    20 000 levels always raise RecursionError and the tests need no more. From 3.14 the limit follows
    the free C stack, so the original, larger depth (``default``) is kept there."""
    return 20000 if sys.version_info < (3, 14) else default
