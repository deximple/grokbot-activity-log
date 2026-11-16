#!/usr/bin/env python3
"""Benchmark for a large ledger (round-3 audit, W-20). Not a test.

``unittest discover`` does not collect this file. It makes no assertions; it prints one line per
command so that a change to gblog.py can be measured before and after::

    python tests/bench_large_ledger.py                  # 50 000 records, takes a while
    python tests/bench_large_ledger.py --records 5000   # a quick look

The ledger is written straight to a temporary directory (no fsync, no gblog code on the write
path): 30 older day files of padding, then ``--records`` records spread over the 30 days before
today. Every fifth record is an automatically captured shell action, so ``coverage`` and ``tune``
have something to match. The timed commands run in-process; the appends at the end go through
``Ledger.append`` and are real, fsynced appends.
"""
import argparse
import datetime as _dt
import io
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

BOT = "bench"
RECENT_DAYS = 30
PADDING_RECORDS = 20  # records in each padding day file


def _body(index):
    """Fields of record number ``index`` (0-based): tasks of five records each, 40 recurring titles."""
    task, step = divmod(index, 5)
    tid = "t%07d" % task
    kind = task % 40
    if step == 0:
        return {"type": "task_start", "task_id": tid, "task": "Bench task %d" % kind}
    if step == 1:
        failed = task % 17 == 0
        body = {"type": "action", "task_id": tid, "action": "shell", "target": "tool%d --run" % kind,
                "outcome": "fail" if failed else "ok"}
        if failed:
            body["detail"] = "exit 1"
        return body
    if step == 2:
        if task % 25 == 0:  # no automatic counterpart for this task's shell action
            return {"type": "note", "task_id": tid, "detail": "nothing was captured for this task"}
        return {"type": "action", "task_id": tid, "action": "shell", "target": "tool%d --run" % kind,
                "outcome": "ok", "source": "auto"}
    if step == 3:
        return {"type": "action", "task_id": tid, "action": "browser",
                "target": "https://site%d.example/page" % kind, "outcome": "ok", "detail": "opened"}
    return {"type": "task_end", "task_id": tid, "outcome": "ok", "detail": "done"}


def _plan(records, padding_days):
    """[(first second of a UTC day, number of records)], oldest day first, ending yesterday."""
    today = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    days = []
    for back in range(RECENT_DAYS + padding_days, RECENT_DAYS, -1):
        days.append((today - _dt.timedelta(days=back), PADDING_RECORDS))
    per_day, extra = divmod(records, RECENT_DAYS)
    for i, back in enumerate(range(RECENT_DAYS, 0, -1)):
        days.append((today - _dt.timedelta(days=back), per_day + (1 if i < extra else 0)))
    return days


def build(root, records, padding_days):
    """Write the chain and its state file. Returns (number of records, number of day files)."""
    bot_dir = os.path.join(root, "logs", BOT)
    os.makedirs(bot_dir)
    os.makedirs(os.path.join(root, "state"))
    prev, seq, last_ts, files = gblog.GENESIS, 0, None, 0
    for day, count in _plan(records, padding_days):
        if count <= 0:
            continue
        step = max(1, 86000 // count)
        lines = []
        for i in range(count):
            when = day + _dt.timedelta(seconds=min(i * step, 86399))
            last_ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")
            rec = {"v": gblog.SCHEMA_VERSION, "seq": seq + 1, "ts": last_ts, "bot": BOT}
            rec.update(_body(seq))
            rec["prev"] = prev
            rec["hash"] = gblog.compute_hash(rec)
            prev = rec["hash"]
            seq += 1
            lines.append(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        with open(os.path.join(bot_dir, day.strftime("%Y-%m-%d") + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        files += 1
    with open(os.path.join(root, "state", BOT + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"seq": seq, "head": prev, "updated": last_ts}, fh, indent=2, sort_keys=True)
    return seq, files


def _cli(root, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = gblog.main(["--root", root] + list(argv))
        except SystemExit as exc:
            rc = exc.code
    return "exit %s, %d characters of output" % (rc, len(out.getvalue()))


def _appends(root, count):
    ledger = gblog.Ledger(root)
    with redirect_stderr(io.StringIO()):
        for i in range(count):
            ledger.append(BOT, {"type": "note", "detail": "bench append %d" % (i + 1)})
    return "%d fsynced appends" % count


def _timed(label, fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    elapsed = time.perf_counter() - start
    print("%-26s %10.3f s   %s" % (label, elapsed, result))
    sys.stdout.flush()
    return elapsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--records", type=int, default=50000, help="records in the recent 30 days (default 50000)")
    parser.add_argument("--padding-days", type=int, default=30, help="older day files of padding (default 30)")
    parser.add_argument("--appends", type=int, default=100, help="appends timed at the end (default 100)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary ledger and print its path")
    args = parser.parse_args(argv)

    root = tempfile.mkdtemp(prefix="gblog-bench-")
    try:
        start = time.perf_counter()
        total, files = build(root, max(0, args.records), max(0, args.padding_days))
        print("gblog %s: %d records in %d day files, written in %.3f s" % (
            gblog.__version__, total, files, time.perf_counter() - start))
        sys.stdout.flush()
        _timed("verify --all", _cli, root, ["verify", "--all"])
        _timed("status", _cli, root, ["status"])
        _timed("report --date yesterday", _cli, root, ["report", "--date", "yesterday"])
        _timed("tail -n 1", _cli, root, ["tail", "-n", "1"])
        _timed("coverage", _cli, root, ["coverage"])
        _timed("tune --stdout", _cli, root, ["tune", "--stdout"])
        elapsed = _timed("%d appends" % args.appends, _appends, root, max(0, args.appends))
        if args.appends > 0:
            print("%-26s %10.3f ms" % ("  per append", 1000.0 * elapsed / args.appends))
    finally:
        if args.keep:
            print("ledger kept at %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
