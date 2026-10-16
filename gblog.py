#!/usr/bin/env python3
"""gblog - tamper-evident activity ledger for Grok Bot (and any other agent).

Single-file, standard-library only. Designed to run on a Grok Bot cloud
computer where manually installed packages may disappear after a reset.

Layout under GBLOG_ROOT (default: /workspace/activity-log):

    logs/<bot>/YYYY-MM-DD.jsonl   one JSON record per line, hash-chained per bot
    state/<bot>.json              chain head (seq, hash) + sync marker
    state/<bot>.lock              flock() so parallel Bots never interleave writes
    reports/YYYY-MM-DD[-<bot>].md rendered daily reports

Every record carries ``prev`` (hash of the previous record of that bot, or
"genesis") and ``hash`` (sha256 over the canonical JSON of the record without
``hash``).  ``gblog verify`` walks the chain and flags any missing, edited or
re-ordered line.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

try:  # POSIX only; on other platforms we degrade to no locking.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

__version__ = "0.1.0"
SCHEMA_VERSION = 1
GENESIS = "genesis"

RECORD_TYPES = ("task_start", "action", "approval", "task_end", "note", "sync", "verify")
ACTION_KINDS = (
    "shell",
    "browser",
    "connector",
    "file",
    "message",
    "computer_use",
    "delegation",
    "memory",
    "routine",
    "other",
)
OUTCOMES = ("ok", "fail", "denied", "pending", "skipped", "n/a")

# (pattern, replacement) pairs mirroring the kinds of secrets Cursor/Grok Bot scrub before export.
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bxai-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED]"),  # JWT
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(token|api[_-]?key|secret|password|passwd|pwd)\b\s*[:=]\s*['\"]?[^\s'\"&]{6,}"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|access_token|refresh_token|api[_-]?key|apikey|key|secret|password|pwd|sig|signature|auth|code)=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),  # userinfo in URLs
]


# --------------------------------------------------------------------------- utils
def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today(ts: Optional[str] = None) -> str:
    return (ts or now_iso())[:10]


def default_root() -> str:
    env = os.environ.get("GBLOG_ROOT")
    if env:
        return env
    if os.path.isdir("/workspace") and os.access("/workspace", os.W_OK):
        return "/workspace/activity-log"
    return os.path.join(os.path.expanduser("~"), "activity-log")


def redact(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def canonical(record: Dict) -> bytes:
    body = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_hash(record: Dict) -> str:
    return hashlib.sha256(canonical(record)).hexdigest()


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not cleaned:
        raise SystemExit("error: bot name must contain letters or digits")
    return cleaned


# --------------------------------------------------------------------------- store
class Ledger:
    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(root or default_root())
        self.logs_dir = os.path.join(self.root, "logs")
        self.state_dir = os.path.join(self.root, "state")
        self.reports_dir = os.path.join(self.root, "reports")

    # -- filesystem helpers
    def init(self) -> None:
        for d in (self.logs_dir, self.state_dir, self.reports_dir):
            os.makedirs(d, exist_ok=True)
        marker = os.path.join(self.root, "README.md")
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(
                    "# activity-log\n\nAppend-only, hash-chained activity ledger written by `gblog`.\n"
                    "Do not edit files under `logs/` by hand; run `gblog verify --all` to check integrity.\n"
                )

    def ensure(self) -> None:
        if not os.path.isdir(self.logs_dir):
            self.init()

    def bots(self) -> List[str]:
        if not os.path.isdir(self.logs_dir):
            return []
        return sorted(d for d in os.listdir(self.logs_dir) if os.path.isdir(os.path.join(self.logs_dir, d)))

    def log_files(self, bot: str) -> List[str]:
        d = os.path.join(self.logs_dir, bot)
        if not os.path.isdir(d):
            return []
        return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl"))

    def _state_path(self, bot: str) -> str:
        return os.path.join(self.state_dir, bot + ".json")

    def read_state(self, bot: str) -> Dict:
        try:
            with open(self._state_path(bot), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"seq": 0, "head": GENESIS}

    def write_state(self, bot: str, state: Dict) -> None:
        tmp = self._state_path(bot) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, self._state_path(bot))

    class _Lock:
        def __init__(self, path: str):
            self.path = path
            self.fh = None

        def __enter__(self):
            self.fh = open(self.path, "a+")
            if fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            if self.fh is not None:
                if fcntl is not None:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()

    def lock(self, bot: str) -> "Ledger._Lock":
        os.makedirs(self.state_dir, exist_ok=True)
        return Ledger._Lock(os.path.join(self.state_dir, bot + ".lock"))

    # -- iteration
    def iter_records(self, bot: str, files: Optional[Iterable[str]] = None) -> Iterator[Tuple[str, int, Dict]]:
        for path in files or self.log_files(bot):
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        rec = {"_corrupt": line}
                    yield path, lineno, rec

    def _tail_head(self, bot: str) -> Tuple[int, str]:
        """Recover (seq, hash) from the last log line; used if state is missing."""
        files = self.log_files(bot)
        for path in reversed(files):
            last = None
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last = line
            if last:
                try:
                    rec = json.loads(last)
                    return int(rec.get("seq", 0)), rec.get("hash", GENESIS)
                except ValueError:
                    break
        return 0, GENESIS

    # -- append
    def append(self, bot: str, record: Dict) -> Dict:
        bot = safe_name(bot)
        self.ensure()
        with self.lock(bot):
            state = self.read_state(bot)
            if state.get("head", GENESIS) == GENESIS and not self.log_files(bot):
                seq, head = 0, GENESIS
            elif not os.path.exists(self._state_path(bot)):
                seq, head = self._tail_head(bot)
            else:
                seq, head = int(state.get("seq", 0)), state.get("head", GENESIS)
            ts = record.get("ts") or now_iso()
            full = {"v": SCHEMA_VERSION, "seq": seq + 1, "ts": ts, "bot": bot}
            for key, value in record.items():
                if key in ("v", "seq", "bot", "prev", "hash"):
                    continue
                if value is None or value == "":
                    continue
                full[key] = redact(value) if isinstance(value, str) else value
            full["prev"] = head
            full["hash"] = compute_hash(full)
            day_dir = os.path.join(self.logs_dir, bot)
            os.makedirs(day_dir, exist_ok=True)
            path = os.path.join(day_dir, today(ts) + ".jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(full, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            state.update({"seq": full["seq"], "head": full["hash"], "updated": ts})
            self.write_state(bot, state)
            return full

    # -- verify
    def verify(self, bot: str) -> Dict:
        expected_prev = GENESIS
        expected_seq = 1
        count = 0
        problems: List[str] = []
        last_ts = ""
        for path, lineno, rec in self.iter_records(bot):
            where = "%s:%d" % (os.path.relpath(path, self.root), lineno)
            if "_corrupt" in rec:
                problems.append("%s: not valid JSON" % where)
                break
            if rec.get("hash") != compute_hash(rec):
                problems.append("%s: hash mismatch (record edited)" % where)
                break
            if rec.get("prev") != expected_prev:
                problems.append("%s: broken chain (prev=%s expected=%s)" % (where, str(rec.get("prev"))[:12], expected_prev[:12]))
                break
            if rec.get("seq") != expected_seq:
                problems.append("%s: seq %s, expected %d (missing or duplicated line)" % (where, rec.get("seq"), expected_seq))
                break
            if rec.get("ts", "") < last_ts:
                problems.append("%s: timestamp goes backwards" % where)
                break
            last_ts = rec.get("ts", "")
            expected_prev = rec["hash"]
            expected_seq += 1
            count += 1
        state = self.read_state(bot)
        if not problems and count and state.get("head") not in (GENESIS, expected_prev):
            problems.append("state/%s.json: head does not match last record (state tampered or log truncated)" % bot)
        return {"bot": bot, "records": count, "head": expected_prev, "ok": not problems, "problems": problems}

    # -- query
    def records(self, bot: Optional[str] = None, date: Optional[str] = None, since: Optional[str] = None) -> List[Dict]:
        out: List[Dict] = []
        for b in ([bot] if bot else self.bots()):
            files = self.log_files(b)
            if date:
                files = [f for f in files if os.path.basename(f).startswith(date)]
            elif since:
                files = [f for f in files if os.path.basename(f)[:10] >= since]
            for _, _, rec in self.iter_records(b, files):
                if "_corrupt" in rec:
                    continue
                if since and rec.get("ts", "") < since:
                    continue
                out.append(rec)
        out.sort(key=lambda r: (r.get("ts", ""), r.get("bot", ""), r.get("seq", 0)))
        return out


# --------------------------------------------------------------------------- report
def render_report(records: List[Dict], title: str) -> str:
    lines = ["# %s" % title, ""]
    if not records:
        lines.append("_No records._")
        return "\n".join(lines) + "\n"
    by_bot: Dict[str, List[Dict]] = {}
    for r in records:
        by_bot.setdefault(r.get("bot", "?"), []).append(r)
    total_actions = sum(1 for r in records if r.get("type") == "action")
    denied = [r for r in records if r.get("outcome") == "denied"]
    failed = [r for r in records if r.get("outcome") == "fail"]
    approvals = [r for r in records if r.get("type") == "approval"]
    lines += [
        "| Metric | Value |",
        "|---|---|",
        "| Bots | %d |" % len(by_bot),
        "| Records | %d |" % len(records),
        "| Actions | %d |" % total_actions,
        "| Approval events | %d |" % len(approvals),
        "| Failed | %d |" % len(failed),
        "| Denied | %d |" % len(denied),
        "| First / last | %s / %s |" % (records[0].get("ts"), records[-1].get("ts")),
        "",
    ]
    for bot, recs in sorted(by_bot.items()):
        lines += ["## %s" % bot, ""]
        tasks: Dict[str, Dict] = {}
        order: List[str] = []
        for r in recs:
            tid = r.get("task_id") or "(no task)"
            if tid not in tasks:
                tasks[tid] = {"title": r.get("task") or "", "actions": [], "start": None, "end": None, "outcome": None}
                order.append(tid)
            t = tasks[tid]
            if r.get("task") and not t["title"]:
                t["title"] = r["task"]
            if r.get("type") == "task_start":
                t["start"] = r.get("ts")
            elif r.get("type") == "task_end":
                t["end"] = r.get("ts")
                t["outcome"] = r.get("outcome")
            else:
                t["actions"].append(r)
        for tid in order:
            t = tasks[tid]
            head = "### %s" % (t["title"] or tid)
            if tid != "(no task)":
                head += "  `%s`" % tid
            lines.append(head)
            meta = []
            if t["start"]:
                meta.append("start %s" % t["start"])
            if t["end"]:
                meta.append("end %s" % t["end"])
            if t["outcome"]:
                meta.append("outcome **%s**" % t["outcome"])
            elif tid != "(no task)":
                meta.append("outcome **open**")
            if meta:
                lines.append("_%s_" % ", ".join(meta))
            lines.append("")
            for r in t["actions"]:
                kind = r.get("action") or r.get("type")
                mark = {"fail": "FAIL", "denied": "DENIED", "pending": "PENDING"}.get(r.get("outcome", ""), "")
                parts = ["%s" % r.get("ts", "")[11:19], "`%s`" % kind]
                if r.get("target"):
                    parts.append(r["target"])
                if mark:
                    parts.append("**%s**" % mark)
                if r.get("detail"):
                    parts.append("— " + r["detail"])
                lines.append("- " + " ".join(parts))
            lines.append("")
    lines += ["---", "_Generated by gblog %s. Records are self-reported by the Bot; cross-check against the in-app transcript._" % __version__]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- git sync
def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], check=check, text=True, capture_output=True)


def sync(ledger: Ledger, bot: str, remote: Optional[str], branch: Optional[str], push: bool, message: Optional[str]) -> Dict:
    root = ledger.root
    ledger.ensure()
    if not os.path.isdir(os.path.join(root, ".git")):
        _git(root, "init", "-q")
    if remote:
        remotes = _git(root, "remote", check=False).stdout.split()
        if "origin" in remotes:
            _git(root, "remote", "set-url", "origin", remote)
        else:
            _git(root, "remote", "add", "origin", remote)
    status = _git(root, "status", "--porcelain").stdout
    pending = [l for l in status.splitlines() if l.strip()]
    ledger.append(bot, {"type": "sync", "action": "other", "target": "git", "outcome": "pending", "detail": "%d changed path(s) before commit" % len(pending)})
    _git(root, "add", "-A", "logs", "reports", "state", "README.md")
    commit_msg = message or "gblog sync %s" % now_iso()
    env_ok = True
    try:
        _git(root, "-c", "user.name=gblog", "-c", "user.email=gblog@localhost", "commit", "-q", "-m", commit_msg)
    except subprocess.CalledProcessError as exc:
        if "nothing to commit" not in (exc.stdout + exc.stderr):
            raise
        env_ok = False
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    result = {"commit": sha, "committed": env_ok, "pushed": False, "error": None}
    if push:
        cur_branch = branch or _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        try:
            _git(root, "push", "-u", "origin", "HEAD:%s" % cur_branch)
            result["pushed"] = True
        except subprocess.CalledProcessError as exc:
            result["error"] = (exc.stderr or exc.stdout).strip()[-400:]
    for b in ledger.bots():
        st = ledger.read_state(b)
        st["last_sync"] = {"ts": now_iso(), "commit": sha, "pushed": result["pushed"]}
        ledger.write_state(b, st)
    return result


# --------------------------------------------------------------------------- CLI
def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bot", required=True, help="Bot name (one chain per bot)")
    p.add_argument("--task-id", help="task id from `task-start`")
    p.add_argument("--task", help="task title")
    p.add_argument("--detail", help="free text (secrets are redacted)")
    p.add_argument("--ts", help="override timestamp (ISO 8601 UTC); default now")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gblog", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="ledger root (default: $GBLOG_ROOT, /workspace/activity-log, or ~/activity-log)")
    p.add_argument("--version", action="version", version="gblog " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the ledger directories")

    s = sub.add_parser("task-start", help="open a task; prints its task id")
    _add_common(s)

    s = sub.add_parser("log", help="append one action record")
    _add_common(s)
    s.add_argument("--type", default="action", choices=RECORD_TYPES)
    s.add_argument("--action", default="other", choices=ACTION_KINDS, help="kind of action")
    s.add_argument("--target", help="what was acted on: host/path, command, connector.tool, file, recipient")
    s.add_argument("--outcome", default="ok", choices=OUTCOMES)

    s = sub.add_parser("approval", help="record an approval request and its decision")
    _add_common(s)
    s.add_argument("--target", required=True, help="the proposed action")
    s.add_argument("--decision", required=True, choices=("allowed", "denied", "pending", "expired", "auto"))

    s = sub.add_parser("task-end", help="close a task")
    _add_common(s)
    s.add_argument("--outcome", default="ok", choices=OUTCOMES)

    s = sub.add_parser("note", help="free-form note (why/decision context)")
    _add_common(s)

    s = sub.add_parser("verify", help="check hash chains")
    s.add_argument("--bot")
    s.add_argument("--all", action="store_true")
    s.add_argument("--record", action="store_true", help="append a verify record with the result")

    s = sub.add_parser("report", help="render a Markdown report")
    s.add_argument("--bot")
    s.add_argument("--date", help="YYYY-MM-DD (UTC); default today")
    s.add_argument("--since", help="YYYY-MM-DD; report everything from this date")
    s.add_argument("--stdout", action="store_true", help="print only, do not write reports/")

    s = sub.add_parser("status", help="per-bot summary")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("tail", help="show recent records")
    s.add_argument("--bot")
    s.add_argument("-n", type=int, default=20)

    s = sub.add_parser("sync", help="commit (and push) the ledger with git")
    s.add_argument("--bot", default="system", help="bot that performs the sync")
    s.add_argument("--remote", help="git remote URL to set as origin")
    s.add_argument("--branch", help="branch to push (default: current)")
    s.add_argument("--no-push", action="store_true")
    s.add_argument("-m", "--message")

    s = sub.add_parser("export", help="dump records as JSON lines")
    s.add_argument("--bot")
    s.add_argument("--since")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = Ledger(args.root)
    out = sys.stdout

    if args.cmd == "init":
        ledger.init()
        out.write("initialized %s\n" % ledger.root)
        return 0

    if args.cmd == "task-start":
        tid = args.task_id or uuid.uuid4().hex[:10]
        ledger.append(args.bot, {"type": "task_start", "task_id": tid, "task": args.task, "detail": args.detail, "ts": args.ts})
        out.write(tid + "\n")
        return 0

    if args.cmd == "log":
        rec = ledger.append(args.bot, {"type": args.type, "task_id": args.task_id, "task": args.task, "action": args.action,
                                       "target": args.target, "outcome": args.outcome, "detail": args.detail, "ts": args.ts})
        out.write("%s #%d %s\n" % (rec["hash"][:12], rec["seq"], rec["type"]))
        return 0

    if args.cmd == "approval":
        outcome = {"allowed": "ok", "auto": "ok", "denied": "denied", "pending": "pending", "expired": "skipped"}[args.decision]
        rec = ledger.append(args.bot, {"type": "approval", "task_id": args.task_id, "task": args.task, "target": args.target,
                                       "outcome": outcome, "decision": args.decision, "detail": args.detail, "ts": args.ts})
        out.write("%s #%d approval %s\n" % (rec["hash"][:12], rec["seq"], args.decision))
        return 0

    if args.cmd == "task-end":
        rec = ledger.append(args.bot, {"type": "task_end", "task_id": args.task_id, "task": args.task, "outcome": args.outcome,
                                       "detail": args.detail, "ts": args.ts})
        out.write("%s #%d task_end %s\n" % (rec["hash"][:12], rec["seq"], args.outcome))
        return 0

    if args.cmd == "note":
        rec = ledger.append(args.bot, {"type": "note", "task_id": args.task_id, "task": args.task, "detail": args.detail, "ts": args.ts})
        out.write("%s #%d note\n" % (rec["hash"][:12], rec["seq"]))
        return 0

    if args.cmd == "verify":
        bots = ledger.bots() if (args.all or not args.bot) else [safe_name(args.bot)]
        if not bots:
            out.write("no bots logged yet under %s\n" % ledger.root)
            return 0
        rc = 0
        for b in bots:
            res = ledger.verify(b)
            if res["ok"]:
                out.write("OK    %-20s %6d records  head %s\n" % (b, res["records"], res["head"][:12]))
            else:
                rc = 1
                out.write("FAIL  %-20s %6d records verified before first problem\n" % (b, res["records"]))
                for prob in res["problems"]:
                    out.write("      - %s\n" % prob)
            if args.record:
                ledger.append(b, {"type": "verify", "action": "other", "target": "chain", "outcome": "ok" if res["ok"] else "fail",
                                  "detail": "; ".join(res["problems"]) or "%d records verified" % res["records"]})
        return rc

    if args.cmd == "report":
        bot = safe_name(args.bot) if args.bot else None
        if args.since:
            recs = ledger.records(bot, since=args.since)
            title = "Activity report since %s" % args.since
            fname = "since-%s" % args.since
        else:
            date = args.date or today()
            recs = ledger.records(bot, date=date)
            title = "Activity report %s" % date
            fname = date
        if bot:
            title += " — %s" % bot
            fname += "-" + bot
        text = render_report(recs, title)
        out.write(text)
        if not args.stdout:
            ledger.ensure()
            path = os.path.join(ledger.reports_dir, fname + ".md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            sys.stderr.write("wrote %s\n" % os.path.relpath(path, ledger.root))
        return 0

    if args.cmd == "status":
        rows = []
        for b in ledger.bots():
            st = ledger.read_state(b)
            today_recs = ledger.records(b, date=today())
            open_tasks = set()
            for r in ledger.records(b, since=(_dt.date.today() - _dt.timedelta(days=7)).isoformat()):
                if r.get("type") == "task_start":
                    open_tasks.add(r.get("task_id"))
                elif r.get("type") == "task_end":
                    open_tasks.discard(r.get("task_id"))
            rows.append({"bot": b, "seq": st.get("seq", 0), "head": st.get("head", GENESIS)[:12], "updated": st.get("updated"),
                         "today": len(today_recs), "open_tasks": len(open_tasks), "last_sync": st.get("last_sync")})
        if args.json:
            out.write(json.dumps({"root": ledger.root, "bots": rows}, indent=2) + "\n")
            return 0
        out.write("root: %s\n" % ledger.root)
        if not rows:
            out.write("(no records yet)\n")
            return 0
        out.write("%-20s %6s %8s %5s %20s  %s\n" % ("bot", "seq", "today", "open", "updated", "last sync"))
        for r in rows:
            ls = r["last_sync"]
            ls_txt = "%s (%s%s)" % (ls["ts"], ls["commit"], "" if ls["pushed"] else ", not pushed") if ls else "never"
            out.write("%-20s %6d %8d %5d %20s  %s\n" % (r["bot"], r["seq"], r["today"], r["open_tasks"], r["updated"] or "-", ls_txt))
        return 0

    if args.cmd == "tail":
        recs = ledger.records(safe_name(args.bot) if args.bot else None)[-args.n:]
        for r in recs:
            bits = [r.get("ts", ""), r.get("bot", ""), "#%s" % r.get("seq"), r.get("type", "")]
            if r.get("action") and r.get("type") == "action":
                bits.append(r["action"])
            if r.get("target"):
                bits.append(r["target"])
            if r.get("outcome") and r["outcome"] != "ok":
                bits.append("[%s]" % r["outcome"])
            if r.get("detail"):
                bits.append("- " + r["detail"])
            out.write(" ".join(str(b) for b in bits) + "\n")
        return 0

    if args.cmd == "sync":
        try:
            res = sync(ledger, safe_name(args.bot), args.remote, args.branch, not args.no_push, args.message)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write("git failed: %s\n" % (exc.stderr or exc.stdout).strip())
            return 2
        out.write("commit %s%s%s\n" % (res["commit"], " pushed" if res["pushed"] else " (not pushed)", "" if not res["error"] else "\n  push error: " + res["error"]))
        return 0 if (res["pushed"] or args.no_push) else 1

    if args.cmd == "export":
        for r in ledger.records(safe_name(args.bot) if args.bot else None, since=args.since):
            out.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
