#!/usr/bin/env python3
"""gblog - tamper-evident activity ledger for Grok Bot (and any other agent).

Single-file, standard-library only. Designed to run on a Grok Bot cloud
computer where manually installed packages may disappear after a reset.

Layout under GBLOG_ROOT (default: /workspace/activity-log):

    logs/<bot>/YYYY-MM-DD.jsonl   one JSON record per line, hash-chained per bot
    state/<bot>.json              chain head (seq, hash) + sync marker
    state/<bot>.lock              flock() so parallel Bots never interleave writes
    reports/YYYY-MM-DD[-<bot>].md rendered daily reports
    tuning/YYYY-MM-DD.md          improvement proposals derived from the ledger (`gblog tune`)
    archive/<bot>/<date>/<sha256>.gz  content-addressed evidence files (`gblog archive`), hashes chained
    state/aliases.json            canonical slug + display name + historical name variants per Bot

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

__version__ = "0.3.0"
SCHEMA_VERSION = 1
GENESIS = "genesis"

RECORD_TYPES = ("task_start", "action", "approval", "task_end", "note", "sync", "verify", "tune", "archive")
SOURCES = ("self", "auto", "routine")
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
        self.tuning_dir = os.path.join(self.root, "tuning")
        self.archive_dir = os.path.join(self.root, "archive")

    # -- filesystem helpers
    def init(self) -> None:
        for d in (self.logs_dir, self.state_dir, self.reports_dir, self.tuning_dir, self.archive_dir):
            os.makedirs(d, exist_ok=True)
        marker = os.path.join(self.root, "README.md")
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(
                    "# activity-log\n\nAppend-only, hash-chained activity ledger written by `gblog`.\n"
                    "Do not edit files under `logs/` by hand; run `gblog verify --all` to check integrity.\n"
                )

    def ensure(self) -> None:
        if not all(os.path.isdir(d) for d in (self.logs_dir, self.state_dir, self.reports_dir, self.tuning_dir, self.archive_dir)):
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
    # -- aliases (one canonical ASCII slug per Bot; display name + historical variants)
    def aliases_path(self) -> str:
        return os.path.join(self.state_dir, "aliases.json")

    def aliases(self) -> Dict:
        try:
            with open(self.aliases_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data.setdefault("bots", {})
        data.setdefault("strict", False)
        return data

    def save_aliases(self, data: Dict) -> None:
        self.ensure()
        tmp = self.aliases_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, self.aliases_path())

    def add_alias(self, slug: str, display: Optional[str], variants: Iterable[str]) -> Dict:
        slug = safe_name(slug).lower()
        data = self.aliases()
        entry = data["bots"].setdefault(slug, {"display": slug, "aliases": []})
        if display:
            entry["display"] = display.strip()
        for v in variants:
            v = v.strip()
            if v and v != slug and v not in entry["aliases"]:
                entry["aliases"].append(v)
        for other, e in data["bots"].items():
            if other != slug:
                e["aliases"] = [a for a in e.get("aliases", []) if a not in entry["aliases"]]
        self.save_aliases(data)
        return entry

    def resolve(self, name: str) -> Tuple[str, bool]:
        """Map any Bot name (slug, display name, historical variant) to its canonical slug.

        Returns (slug, known).  Unknown names fall back to safe_name(name) so old
        behaviour is unchanged when no aliases are configured."""
        data = self.aliases()
        raw = (name or "").strip()
        for slug, e in data["bots"].items():
            if raw == slug or raw.lower() == slug or raw == e.get("display") or raw in e.get("aliases", []) \
                    or raw.lower() in [a.lower() for a in e.get("aliases", [])]:
                return slug, True
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
        if not cleaned:
            raise SystemExit("error: '%s' has no ASCII letters or digits; register it first: "
                             "gblog alias add --slug <ascii-slug> --display '%s'" % (raw, raw))
        return cleaned, not data["bots"]

    def canonical(self, chain: str) -> str:
        slug, _ = self.resolve(chain)
        return slug

    def label(self, chain_or_slug: str) -> str:
        slug = self.canonical(chain_or_slug)
        display = self.aliases()["bots"].get(slug, {}).get("display")
        return "%s(%s)" % (display, slug) if display and display != slug else slug

    def chains_for(self, bot: str) -> List[str]:
        slug = self.canonical(bot)
        names = {slug} | set(self.aliases()["bots"].get(slug, {}).get("aliases", []))
        existing = [c for c in self.bots() if c in names or self.canonical(c) == slug]
        return existing or [slug]

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for chain in self.bots():
            out.setdefault(self.canonical(chain), []).append(chain)
        return dict(sorted(out.items()))

    def canonicalize(self, records: List[Dict]) -> List[Dict]:
        """Copies of records with ``bot`` replaced by the canonical slug (original chain kept in ``chain``)."""
        cache: Dict[str, str] = {}
        out = []
        for r in records:
            chain = r.get("bot", "?")
            if chain not in cache:
                cache[chain] = self.canonical(chain)
            if cache[chain] != chain:
                r = dict(r, bot=cache[chain], chain=chain)
            out.append(r)
        return out

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
        archives = 0
        if not problems:
            arch_recs = [r for _, _, r in self.iter_records(bot) if r.get("type") == "archive"]
            archives = len(arch_recs)
            problems.extend(verify_archives(self, arch_recs))
        return {"bot": bot, "records": count, "head": expected_prev, "ok": not problems, "problems": problems, "archives": archives}

    # -- query
    def records(self, bot: Optional[str] = None, date: Optional[str] = None, since: Optional[str] = None) -> List[Dict]:
        out: List[Dict] = []
        for b in (self.chains_for(bot) if bot else self.bots()):
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
def render_report(records: List[Dict], title: str, labels: Optional[Dict[str, str]] = None) -> str:
    labels = labels or {}
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
        chains = sorted({r.get("chain") or r.get("bot", "?") for r in recs})
        lines += ["## %s" % labels.get(bot, bot), ""]
        if len(chains) > 1:
            lines += ["_Chains merged by alias: %s_" % ", ".join("`%s`" % c for c in chains), ""]
        tasks: Dict[str, Dict] = {}
        order: List[str] = []
        for r in recs:
            tid = r.get("task_id") or "(no task)"
            if tid not in tasks:
                tasks[tid] = {"title": r.get("task") or "", "actions": [], "start": None, "end": None, "outcome": None,
                              "chain": r.get("chain") or r.get("bot")}
                order.append(tid)
            t = tasks[tid]
            if r.get("task") and not t["title"]:
                t["title"] = r["task"]
            if r.get("type") == "task_start":
                t["start"] = r.get("ts")
            elif r.get("type") == "task_end":
                t["end"] = r.get("ts")
                t["outcome"] = r.get("outcome")
                t["end_chain"] = r.get("chain") or r.get("bot")
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
                if t.get("end_chain") and t["chain"] and t["end_chain"] != t["chain"]:
                    meta.append("closed from chain `%s`" % t["end_chain"])
            elif tid != "(no task)":
                twin = None
                if t["title"]:
                    key = _norm_title(t["title"])
                    for oid in order:
                        o = tasks[oid]
                        if oid != tid and o["end"] and o["title"] and _norm_title(o["title"]) == key and (o["start"] or o["end"]) >= (t["start"] or ""):
                            twin = (oid, o)
                            break
                if twin:
                    meta.append("outcome **open** — likely closed as `%s` in chain `%s` (%s)" % (twin[0], twin[1].get("end_chain") or twin[1]["chain"], twin[1]["outcome"]))
                else:
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


# --------------------------------------------------------------------------- archive (content-addressed evidence files)
ARCHIVE_KINDS = ("transcript", "message", "artifact", "other")


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def archive_file(ledger: "Ledger", bot: str, data: bytes, name: str, kind: str = "other", task_id: Optional[str] = None,
                 detail: Optional[str] = None, do_redact: bool = True, ts: Optional[str] = None) -> Dict:
    """Store ``data`` as archive/<bot>/<date>/<sha256>.gz and chain its hash.

    Text content is redacted before hashing so the ledger never references a
    file that still contains a secret.  Identical content is stored once."""
    import gzip
    bot = safe_name(bot)
    redacted = False
    if do_redact and _is_text(data):
        text = data.decode("utf-8")
        clean = redact(text) or ""
        redacted = clean != text
        data = clean.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    day = today(ts)
    rel = os.path.join("archive", bot, day, digest + ".gz")
    full = os.path.join(ledger.root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    existed = os.path.exists(full)
    if not existed:
        tmp = full + ".tmp"
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(data)
        os.replace(tmp, full)
    rec = ledger.append(bot, {"type": "archive", "action": "file", "target": os.path.basename(name) or "stdin", "outcome": "ok",
                              "task_id": task_id, "kind": kind if kind in ARCHIVE_KINDS else "other", "sha256": digest,
                              "bytes": len(data), "path": rel.replace(os.sep, "/"), "redacted": redacted,
                              "detail": detail, "ts": ts})
    rec["_dedup"] = existed
    return rec


def verify_archives(ledger: "Ledger", records: Iterable[Dict]) -> List[str]:
    import gzip
    problems: List[str] = []
    seen = set()
    for r in records:
        if r.get("type") != "archive" or not r.get("path"):
            continue
        rel = r["path"]
        if rel in seen:
            continue
        seen.add(rel)
        full = os.path.join(ledger.root, rel)
        if not os.path.exists(full):
            problems.append("%s: archived file missing" % rel)
            continue
        h = hashlib.sha256()
        try:
            with gzip.open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except (OSError, EOFError) as exc:
            problems.append("%s: unreadable (%s)" % (rel, exc.__class__.__name__))
            continue
        if h.hexdigest() != r.get("sha256"):
            problems.append("%s: content hash mismatch (file modified after archiving)" % rel)
    return problems


# --------------------------------------------------------------------------- remote visibility (backups must be private)
_GITHUB_REMOTE_RE = re.compile(r"(?:github\.com[/:])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def remote_visibility(remote: str, timeout: float = 6.0) -> Dict:
    """Return {"public": True|False|None, "reason": str} for a git remote URL.

    GitHub remotes are checked with `gh` when available, else with the public
    REST API (a 200 without credentials means the repository is public)."""
    m = _GITHUB_REMOTE_RE.search(remote or "")
    if not m:
        return {"public": None, "reason": "not a github.com remote; visibility unknown"}
    owner, repo = m.group(1), m.group(2)
    try:
        res = subprocess.run(["gh", "api", "repos/%s/%s" % (owner, repo), "--jq", ".private"], text=True, capture_output=True, timeout=timeout)
        if res.returncode == 0 and res.stdout.strip() in ("true", "false"):
            return {"public": res.stdout.strip() == "false", "reason": "gh api"}
    except (OSError, subprocess.TimeoutExpired):
        pass
    import urllib.error
    import urllib.request
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (owner, repo),
                                 headers={"User-Agent": "gblog/" + __version__, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return {"public": not body.get("private", False), "reason": "api.github.com"}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"public": False, "reason": "api.github.com 404 (private or nonexistent)"}
        return {"public": None, "reason": "api.github.com HTTP %d" % exc.code}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"public": None, "reason": "network: %s" % exc.__class__.__name__}


# --------------------------------------------------------------------------- git sync
def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], check=check, text=True, capture_output=True)


def sync(ledger: Ledger, bot: str, remote: Optional[str], branch: Optional[str], push: bool, message: Optional[str],
         allow_public: bool = False) -> Dict:
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
    _git(root, "add", "-A", "logs", "reports", "state", "tuning", "archive", "README.md")
    commit_msg = message or "gblog sync %s" % now_iso()
    env_ok = True
    try:
        _git(root, "-c", "user.name=gblog", "-c", "user.email=gblog@localhost", "commit", "-q", "-m", commit_msg)
    except subprocess.CalledProcessError as exc:
        if "nothing to commit" not in (exc.stdout + exc.stderr):
            raise
        env_ok = False
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    result = {"commit": sha, "committed": env_ok, "pushed": False, "error": None, "visibility": None}
    if push:
        origin = _git(root, "remote", "get-url", "origin", check=False).stdout.strip()
        vis = remote_visibility(origin) if origin else {"public": None, "reason": "no origin configured"}
        result["visibility"] = vis
        if vis["public"] and not allow_public:
            result["error"] = "refused: remote %s is PUBLIC (%s); backups must be private. Use --allow-public to override." % (origin, vis["reason"])
            ledger.append(bot, {"type": "sync", "action": "other", "target": "git push", "outcome": "denied", "detail": result["error"][:300]})
            push = False
        elif vis["public"] is None:
            sys.stderr.write("warning: could not confirm the remote is private (%s)\n" % vis["reason"])
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


# --------------------------------------------------------------------------- capture (automatic evidence)
# Records with ``source: "auto"`` are collected without the Bot's cooperation
# (shell hook, browser history, file snapshots).  They are used by ``coverage``
# to measure how much of the Bot's real activity it reported itself.

SHELL_HOOK = r'''# >>> gblog shell hook >>>
# Logs every interactive shell command to the gblog ledger (source=auto).
# Set GBLOG_TASK=<task id> after `gblog task-start` so commands attach to the task.
__gblog_last=""
__gblog_hook() {
  local rc=$? cmd
  cmd=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null | sed 's/^ *[0-9]* *//')
  if [ -z "$cmd" ] || [ "$cmd" = "$__gblog_last" ]; then return 0; fi
  __gblog_last="$cmd"
  case "$cmd" in gblog*|*" gblog "*|__gblog*) return 0;; esac
  command -v gblog >/dev/null 2>&1 || return 0
  local outcome=ok
  [ "$rc" -eq 0 ] || outcome=fail
  gblog log --bot "${GBLOG_BOT:-%s}" --task-id "${GBLOG_TASK:-}" --action shell --target "$cmd" \
    --outcome "$outcome" --source auto >/dev/null 2>&1 || true
  return 0
}
case ";$PROMPT_COMMAND;" in *"__gblog_hook"*) ;; *) PROMPT_COMMAND="__gblog_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}";; esac
# <<< gblog shell hook <<<
'''

BROWSER_HISTORY_GLOBS = (
    # every profile ("Default", "Profile 1", ...) of every Chromium-family install; GBLOG_BROWSER_HISTORY adds more (colon-separated globs)
    "~/.config/google-chrome*/*/History",
    "~/.config/chromium*/*/History",
    "~/.config/BraveSoftware/*/*/History",
    "~/.config/microsoft-edge*/*/History",
    "~/.config/vivaldi*/*/History",
    "~/snap/chromium/common/chromium/*/History",
    "~/snap/brave/current/.config/BraveSoftware/*/*/History",
    "~/.var/app/*/config/*/*/History",
    "~/.cache/ms-playwright*/**/History",
    "~/.mozilla/firefox/*/places.sqlite",
    "~/Library/Application Support/Google/Chrome*/*/History",
    "~/Library/Application Support/Chromium/*/History",
    "~/Library/Application Support/BraveSoftware/*/*/History",
    "~/Library/Application Support/Microsoft Edge/*/History",
    "/home/*/.config/google-chrome*/*/History",
    "/home/*/.config/chromium*/*/History",
    "/root/.config/google-chrome*/*/History",
    "/root/.config/chromium*/*/History",
    "/workspace/.config/google-chrome*/*/History",
    "/workspace/.config/chromium*/*/History",
)
_WEBKIT_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def shell_hook(bot: str) -> str:
    return SHELL_HOOK % safe_name(bot)


def install_shell_hook(bot: str, rc_path: str) -> bool:
    rc_path = os.path.expanduser(rc_path)
    existing = ""
    if os.path.exists(rc_path):
        with open(rc_path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    if ">>> gblog shell hook >>>" in existing:
        return False
    with open(rc_path, "a", encoding="utf-8") as fh:
        fh.write(("\n" if existing and not existing.endswith("\n") else "") + shell_hook(bot))
    return True


def find_browser_histories() -> List[str]:
    import glob
    patterns = list(BROWSER_HISTORY_GLOBS) + [p for p in os.environ.get("GBLOG_BROWSER_HISTORY", "").split(":") if p]
    found: List[str] = []
    for pattern in patterns:
        try:
            found.extend(p for p in glob.glob(os.path.expanduser(pattern), recursive=True) if os.path.isfile(p) and os.access(p, os.R_OK))
        except OSError:
            continue
    return sorted(set(os.path.realpath(p) for p in found))


def _url_target(url: str) -> Optional[str]:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.hostname or parts.netloc
    return "%s://%s%s" % (parts.scheme, host, parts.path or "/")


def capture_browser(ledger: "Ledger", bot: str, history_paths: Optional[List[str]] = None, hours: int = 24) -> Dict:
    import shutil
    import sqlite3
    import tempfile
    bot = safe_name(bot)
    paths = history_paths or find_browser_histories()
    state = ledger.read_state(bot)
    markers = state.get("capture_browser_last", {})
    if not isinstance(markers, dict):  # v0.2 kept a single marker
        markers = {"*": int(markers)}
    cutoff_epoch = _dt.datetime.utcnow().timestamp() - hours * 3600
    added, changed = 0, False
    for path in paths:
        firefox = path.endswith("places.sqlite")
        # Chrome: microseconds since 1601; Firefox: microseconds since 1970
        to_epoch = (lambda v: v / 1_000_000) if firefox else (lambda v: v / 1_000_000 - _WEBKIT_EPOCH_OFFSET)
        from_epoch = (lambda e: int(e * 1_000_000)) if firefox else (lambda e: int((e + _WEBKIT_EPOCH_OFFSET) * 1_000_000))
        since = max(int(markers.get(path, markers.get("*", 0))), from_epoch(cutoff_epoch))
        tmp = tempfile.mkdtemp(prefix="gblog-hist-")
        try:
            copy = os.path.join(tmp, "History")
            shutil.copyfile(path, copy)  # the browser keeps the live DB locked; read a copy
            for suffix in ("-wal", "-journal"):
                if os.path.exists(path + suffix):
                    shutil.copyfile(path + suffix, copy + suffix)
            conn = sqlite3.connect(copy)
            try:
                if firefox:
                    rows = conn.execute("SELECT v.visit_date, p.url, p.title FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id "
                                        "WHERE v.visit_date > ? ORDER BY v.visit_date", (since,)).fetchall()
                else:
                    rows = conn.execute("SELECT v.visit_time, u.url, u.title FROM visits v JOIN urls u ON u.id = v.url "
                                        "WHERE v.visit_time > ? ORDER BY v.visit_time", (since,)).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        last_target, newest = None, since
        for visit_time, url, title in rows:
            newest = max(newest, int(visit_time))
            target = _url_target(url or "")
            if not target or target == last_target:
                continue
            last_target = target
            observed = _dt.datetime.utcfromtimestamp(to_epoch(visit_time)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ledger.append(bot, {"type": "action", "action": "browser", "target": target, "outcome": "ok",
                                "detail": (title or "")[:120], "source": "auto", "capture": "browser", "observed": observed,
                                "profile": os.path.basename(os.path.dirname(path))})
            added += 1
        if newest != markers.get(path):
            markers[path] = newest
            changed = True
    if changed:
        state = ledger.read_state(bot)
        state["capture_browser_last"] = {k: v for k, v in markers.items() if k != "*"}
        ledger.write_state(bot, state)
    return {"histories": paths, "added": added}


def capture_files(ledger: "Ledger", bot: str, path: str, exclude: Iterable[str] = (), limit: int = 200) -> Dict:
    bot = safe_name(bot)
    path = os.path.abspath(os.path.expanduser(path))
    skip_names = set(exclude) | {".git", "__pycache__", "node_modules", ".cache", ".venv"}
    snap_path = os.path.join(ledger.state_dir, "capture-files-%s.json" % bot)
    current: Dict[str, List[float]] = {}
    for dirpath, dirnames, filenames in os.walk(path):
        if os.path.abspath(dirpath).startswith(ledger.root):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip_names]
        for name in filenames:
            if name in skip_names:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            current[os.path.relpath(full, path)] = [round(st.st_mtime, 3), st.st_size]
    previous: Optional[Dict[str, List[float]]] = None
    if os.path.exists(snap_path):
        with open(snap_path, encoding="utf-8") as fh:
            previous = json.load(fh).get("files", {})
    ledger.ensure()
    with open(snap_path, "w", encoding="utf-8") as fh:
        json.dump({"path": path, "taken": now_iso(), "files": current}, fh)
    if previous is None:
        return {"baseline": len(current), "changes": 0}
    changes: List[Tuple[str, str]] = []
    for rel, meta in current.items():
        if rel not in previous:
            changes.append(("created", rel))
        elif previous[rel] != meta:
            changes.append(("modified", rel))
    for rel in previous:
        if rel not in current:
            changes.append(("deleted", rel))
    changes.sort(key=lambda c: c[1])
    for kind, rel in changes[:limit]:
        ledger.append(bot, {"type": "action", "action": "file", "target": rel, "outcome": "ok", "detail": kind,
                            "source": "auto", "capture": "files"})
    if len(changes) > limit:
        ledger.append(bot, {"type": "note", "detail": "%d more file changes not listed individually" % (len(changes) - limit),
                            "source": "auto", "capture": "files"})
    return {"baseline": len(previous), "changes": len(changes)}


# --------------------------------------------------------------------------- coverage (self-report vs. auto)
def _ts_epoch(ts: Optional[str]) -> float:
    if not ts:
        return 0.0
    try:
        return _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _shell_name(cmd: str) -> str:
    tokens = cmd.strip().split()
    while tokens and (("=" in tokens[0] and not tokens[0].startswith("-")) or tokens[0] in ("sudo", "env", "nohup", "time")):
        tokens.pop(0)
    return os.path.basename(tokens[0]) if tokens else ""


def norm_target(kind: Optional[str], target: Optional[str]) -> str:
    t = (target or "").strip()
    if kind == "browser":
        from urllib.parse import urlsplit
        host = (urlsplit(t if "://" in t else "//" + t).hostname or t).lower()
        return host[4:] if host.startswith("www.") else host
    if kind == "shell":
        return _shell_name(t)
    if kind == "file":
        return os.path.basename(t.rstrip("/"))
    return t.lower()


def coverage(records: List[Dict], window_minutes: int = 30) -> Dict:
    auto = [r for r in records if r.get("type") == "action" and r.get("source") == "auto"]
    self_recs = [r for r in records if r.get("type") == "action" and r.get("source") != "auto"]
    window = window_minutes * 60
    by_key: Dict[Tuple[str, str], Dict] = {}
    unmatched: List[Dict] = []
    used = set()
    for a in auto:
        key = (a.get("bot", "?"), a.get("action", "other"))
        slot = by_key.setdefault(key, {"auto": 0, "matched": 0})
        slot["auto"] += 1
        t_a = _ts_epoch(a.get("observed") or a.get("ts"))
        want = norm_target(a.get("action"), a.get("target"))
        hit = None
        for i, s in enumerate(self_recs):
            if i in used or s.get("bot") != a.get("bot") or s.get("action") != a.get("action"):
                continue
            if abs(_ts_epoch(s.get("ts")) - t_a) > window:
                continue
            if want and norm_target(s.get("action"), s.get("target")) == want:
                hit = i
                break
        if hit is None:
            unmatched.append(a)
        else:
            used.add(hit)
            slot["matched"] += 1
    rows = []
    for (bot, kind), slot in sorted(by_key.items()):
        rows.append({"bot": bot, "action": kind, "auto": slot["auto"], "matched": slot["matched"],
                     "pct": round(100.0 * slot["matched"] / slot["auto"], 1) if slot["auto"] else None})
    total_auto = sum(r["auto"] for r in rows)
    total_matched = sum(r["matched"] for r in rows)
    return {"rows": rows, "unmatched": unmatched, "self_only": len(self_recs) - len(used),
            "auto": total_auto, "matched": total_matched,
            "pct": round(100.0 * total_matched / total_auto, 1) if total_auto else None}


def render_coverage(cov: Dict, title: str) -> str:
    lines = ["# %s" % title, ""]
    if not cov["auto"]:
        lines.append("_No automatically captured records yet. Install the shell hook (`gblog capture shell-hook --install`) "
                     "and run `gblog capture browser` / `gblog capture files` from a routine._")
        return "\n".join(lines) + "\n"
    lines += ["Overall: **%s%%** of %d automatically observed actions were also reported by the Bot "
              "(%d self-reported actions had no automatic counterpart, which is expected for connectors/messages)." %
              (cov["pct"], cov["auto"], cov["self_only"]), "",
              "| Bot | Action | Observed | Reported | Coverage |", "|---|---|---|---|---|"]
    for r in cov["rows"]:
        lines.append("| %s | %s | %d | %d | %s%% |" % (r["bot"], r["action"], r["auto"], r["matched"], r["pct"]))
    if cov["unmatched"]:
        lines += ["", "## Observed but not reported (latest %d)" % min(len(cov["unmatched"]), 25), ""]
        for r in cov["unmatched"][-25:]:
            lines.append("- %s %s `%s` %s" % ((r.get("observed") or r.get("ts", ""))[:19], r.get("bot"), r.get("action"), r.get("target", "")))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- tune (improve the Bots from their own ledger)
TUNE_CATEGORIES = ("skill", "routine", "approval", "description", "memory", "logging", "revert")
_LESSON_RE = re.compile(r"(?i)\b(lesson|learned|next time|from now on|should (?:always|never)|always|never|remember)\b|교훈|다음부터|항상|절대|기억")
_NORM_TITLE_RE = re.compile(r"[0-9]+|[#\[\](){}:;,.!?\"'`]+|\b(?:on|for|the|a|an|of|to)\b", re.I)


def _norm_title(title: Optional[str]) -> str:
    return " ".join(_NORM_TITLE_RE.sub(" ", title or "").lower().split())


def _ref(r: Dict) -> str:
    return "%s#%s" % (r.get("ts", "")[:10], r.get("seq"))


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _proposal_id(bot: str, category: str, key: str) -> str:
    return hashlib.sha1(("%s|%s|%s" % (bot, category, key)).encode("utf-8")).hexdigest()[:8]


def _tasks_of(recs: List[Dict]) -> Dict[str, Dict]:
    tasks: Dict[str, Dict] = {}
    for r in recs:
        tid = r.get("task_id")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"title": "", "start": None, "end": None, "outcome": None, "actions": [], "approvals": []})
        if r.get("task") and not t["title"]:
            t["title"] = r["task"]
        if r.get("type") == "task_start":
            t["start"] = r.get("ts")
        elif r.get("type") == "task_end":
            t["end"], t["outcome"] = r.get("ts"), r.get("outcome")
        elif r.get("type") == "action":
            t["actions"].append(r)
        elif r.get("type") == "approval":
            t["approvals"].append(r)
    return tasks


def compute_metric(records: List[Dict], metric: Dict, since: Optional[str] = None, until: Optional[str] = None) -> Optional[float]:
    recs = [r for r in records if r.get("bot") == metric.get("bot")
            and (not since or r.get("ts", "") >= since) and (not until or r.get("ts", "") < until)]
    kind = metric.get("kind")
    if kind == "fail":
        return float(sum(1 for r in recs if r.get("type") == "action" and r.get("outcome") == "fail"
                         and r.get("action") == metric.get("action") and norm_target(r.get("action"), r.get("target")) == metric.get("key")))
    if kind == "denied":
        return float(sum(1 for r in recs if r.get("type") == "approval" and r.get("decision") == "denied"
                         and _norm_title(r.get("target")) == metric.get("key")))
    if kind == "approvals":
        return float(sum(1 for r in recs if r.get("type") == "approval" and _norm_title(r.get("target")) == metric.get("key")))
    if kind == "manual_tasks":
        return float(sum(1 for r in recs if r.get("type") == "task_start" and r.get("source") != "routine"
                         and _norm_title(r.get("task")) == metric.get("key")))
    if kind == "slow":
        tasks = _tasks_of(recs)
        durations = [(_ts_epoch(t["end"]) - _ts_epoch(t["start"])) / 60 for t in tasks.values() if t["start"] and t["end"]]
        threshold = metric.get("threshold_min") or 0
        return float(sum(1 for d in durations if d > threshold))
    if kind == "open_tasks":
        tasks = _tasks_of(recs)
        return float(sum(1 for t in tasks.values() if t["start"] and not t["end"]))
    if kind == "coverage":
        cov = coverage(recs)
        for row in cov["rows"]:
            if row["action"] == metric.get("action"):
                return row["pct"]
        return None
    return None


def analyze(records: List[Dict], window_days: int, cov: Optional[Dict] = None) -> Dict:
    now = now_iso()
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_bot: Dict[str, List[Dict]] = {}
    for r in records:
        if r.get("type") in ("sync", "verify") or r.get("ts", "") < since:
            continue
        by_bot.setdefault(r.get("bot", "?"), []).append(r)
    metrics: Dict[str, Dict] = {}
    proposals: List[Dict] = []

    def add(bot: str, category: str, key: str, title: str, suggestion: str, evidence: List[Dict], metric: Optional[Dict], better: str) -> None:
        pid = _proposal_id(bot, category, key)
        if any(p["id"] == pid for p in proposals):
            return
        baseline = compute_metric(records, metric, since=since) if metric else None
        proposals.append({"id": pid, "bot": bot, "category": category, "key": key, "title": title, "suggestion": suggestion,
                          "evidence": [_ref(r) for r in evidence[:8]], "count": len(evidence),
                          "metric": metric, "baseline": baseline, "better": better, "window": {"since": since, "until": now}})

    for bot, recs in sorted(by_bot.items()):
        tasks = _tasks_of(recs)
        durations = {tid: (_ts_epoch(t["end"]) - _ts_epoch(t["start"])) / 60 for tid, t in tasks.items() if t["start"] and t["end"]}
        fails = [r for r in recs if r.get("type") == "action" and r.get("outcome") == "fail"]
        denied = [r for r in recs if r.get("type") == "approval" and r.get("decision") == "denied"]
        m = {"tasks": sum(1 for t in tasks.values() if t["start"]),
             "closed": sum(1 for t in tasks.values() if t["end"]),
             "succeeded": sum(1 for t in tasks.values() if t["outcome"] == "ok"),
             "open": sum(1 for t in tasks.values() if t["start"] and not t["end"]),
             "actions": sum(1 for r in recs if r.get("type") == "action" and r.get("source") != "auto"),
             "auto_actions": sum(1 for r in recs if r.get("type") == "action" and r.get("source") == "auto"),
             "failed_actions": len(fails), "approvals": sum(1 for r in recs if r.get("type") == "approval"),
             "denied": len(denied), "median_minutes": round(_median(list(durations.values())), 1)}
        metrics[bot] = m

        # A. repeated failures -> skill fix
        groups: Dict[Tuple[str, str], List[Dict]] = {}
        for r in fails:
            groups.setdefault((r.get("action", "other"), norm_target(r.get("action"), r.get("target"))), []).append(r)
        for (kind, key), ev in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(ev) < 2 or not key:
                continue
            titles = sorted({tasks[r["task_id"]]["title"] for r in ev if r.get("task_id") in tasks and tasks[r["task_id"]]["title"]})[:3]
            add(bot, "skill", "fail:%s:%s" % (kind, key),
                "`%s` %s failed %d times" % (key, kind, len(ev)),
                "Add an explicit step or fallback for `%s` to the skill these tasks use%s. Last error: \"%s\". "
                "If the cause is environmental (login expired, tool missing), put the fix in the Bot's setup instructions instead."
                % (key, (" (%s)" % ", ".join(titles)) if titles else "", (ev[-1].get("detail") or "")[:140]),
                ev, {"kind": "fail", "bot": bot, "action": kind, "key": key}, "lower")

        # B/C. approvals -> auto-review rules
        appr: Dict[str, List[Dict]] = {}
        for r in recs:
            if r.get("type") == "approval":
                appr.setdefault(_norm_title(r.get("target")), []).append(r)
        for key, ev in sorted(appr.items(), key=lambda kv: -len(kv[1])):
            if not key:
                continue
            den = [r for r in ev if r.get("decision") == "denied"]
            ok = [r for r in ev if r.get("decision") in ("allowed", "auto")]
            if len(den) >= 2 and len(den) >= len(ok):
                add(bot, "approval", "deny:" + key, "\"%s\" was denied %d of %d times" % (ev[0].get("target"), len(den), len(ev)),
                    "Stop proposing it: add to the Bot description \"Do not %s; instead ...\" or an Auto-review *Always deny* rule, "
                    "so the Bot takes the alternative path without asking." % ev[0].get("target"),
                    den, {"kind": "denied", "bot": bot, "key": key}, "lower")
            elif len(ok) >= 3 and not den:
                add(bot, "approval", "allow:" + key, "\"%s\" was approved %d times without a single denial" % (ev[0].get("target"), len(ok)),
                    "Add an Auto-review *Allow automatically* rule for this action (scoped as narrowly as possible) to remove the wait.",
                    ok, {"kind": "approvals", "bot": bot, "key": key}, "lower")

        # D. repeated manual tasks -> routine
        titles: Dict[str, List[Dict]] = {}
        for r in recs:
            if r.get("type") == "task_start" and r.get("source") != "routine":
                titles.setdefault(_norm_title(r.get("task")), []).append(r)
        for key, ev in sorted(titles.items(), key=lambda kv: -len(kv[1])):
            days = {r.get("ts", "")[:10] for r in ev}
            if len(ev) >= 3 and len(days) >= 3 and key:
                hours = sorted(int(r.get("ts", "T00")[11:13] or 0) for r in ev)
                add(bot, "routine", "routine:" + key, "\"%s\" was started manually %d times on %d days" % (ev[0].get("task"), len(ev), len(days)),
                    "Turn it into a routine (typical start %02d:00 UTC). Routine prompt: the task title, the data source, the expected result, "
                    "what must not happen without approval, and what to report when the data is missing." % hours[len(hours) // 2],
                    ev, {"kind": "manual_tasks", "bot": bot, "key": key}, "lower")

        # E. repeated action sequences -> skill
        grams: Dict[Tuple, List[str]] = {}
        for tid, t in tasks.items():
            seq = [(a.get("action"), norm_target(a.get("action"), a.get("target"))) for a in t["actions"] if a.get("source") != "auto"]
            seen = set()
            for i in range(len(seq) - 2):
                g = tuple(seq[i:i + 3])
                if len({x for x in g}) < 2 or g in seen:
                    continue
                seen.add(g)
                grams.setdefault(g, []).append(tid)
        for g, tids in sorted(grams.items(), key=lambda kv: -len(kv[1])):
            if len(tids) < 3:
                continue
            key = " > ".join("%s:%s" % x for x in g)
            if any(p["bot"] == bot and p["category"] == "skill" and p["key"].startswith("seq:") for p in proposals):
                break
            add(bot, "skill", "seq:" + key, "The sequence %s recurs in %d tasks" % (" → ".join("`%s %s`" % x for x in g), len(tids)),
                "Write it down as a skill (name, when to use, the steps with exact commands/URLs, how to validate) so every run is identical "
                "and shorter. Tasks: %s." % ", ".join(tids[:5]),
                [tasks[t]["actions"][0] for t in tids if tasks[t]["actions"]], None, "info")

        # F. slow tasks -> break down
        if len(durations) >= 4:
            med = _median(list(durations.values()))
            threshold = max(2 * med, 15.0)
            slow = [tid for tid, d in durations.items() if d > threshold]
            if len(slow) >= 2:
                ev = [r for r in recs if r.get("type") == "task_end" and r.get("task_id") in slow]
                add(bot, "description", "slow", "%d tasks took more than %.0f min (median %.0f min)" % (len(slow), threshold, med),
                    "Add to the description: break long tasks into checkpoints, report partial results, and ask early when input is missing. "
                    "Slowest: %s." % ", ".join("%s (%.0f min)" % (tasks[t]["title"] or t, durations[t]) for t in sorted(slow, key=lambda x: -durations[x])[:3]),
                    ev, {"kind": "slow", "bot": bot, "threshold_min": round(threshold, 1)}, "lower")

        # G. lessons in notes -> memory / description
        lessons = [r for r in recs if r.get("type") == "note" and r.get("source") != "auto" and _LESSON_RE.search(r.get("detail") or "")]
        for r in lessons[-5:]:
            add(bot, "memory", "note:%s" % r.get("hash", "")[:12], "Lesson noted on %s" % r.get("ts", "")[:10],
                "Persist to Bot memory or description so it survives the conversation: \"%s\"" % (r.get("detail") or "")[:200], [r], None, "info")

        # H. tasks never closed -> logging discipline
        stale = [t for t in tasks.values() if t["start"] and not t["end"] and t["start"] < (_dt.datetime.utcnow() - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")]
        if len(stale) >= 2:
            add(bot, "logging", "open", "%d tasks were never closed with task-end" % len(stale),
                "Re-state in the description: every reply that finishes work ends with `gblog task-end`. Close the stale ones with "
                "`--outcome skipped --detail \"closed late\"`.", [r for r in recs if r.get("type") == "task_start" and r.get("task_id") in {k for k, t in tasks.items() if t in stale}],
                {"kind": "open_tasks", "bot": bot}, "lower")

    # I. coverage gaps -> logging rules
    if cov:
        for row in cov["rows"]:
            if row["auto"] >= 3 and row["pct"] is not None and row["pct"] < 80:
                add(row["bot"], "logging", "coverage:" + row["action"],
                    "%s reported only %s%% of %d observed `%s` actions" % (row["bot"], row["pct"], row["auto"], row["action"]),
                    "Strengthen the `%s` row of the activity-log skill for this Bot (give the exact command and say \"immediately after, not at the end\"); "
                    "set `GBLOG_TASK` after task-start so auto records attach to the task." % row["action"],
                    [r for r in cov["unmatched"] if r.get("bot") == row["bot"] and r.get("action") == row["action"]],
                    {"kind": "coverage", "bot": row["bot"], "action": row["action"]}, "higher")

    # J. effect of applied changes -> keep or revert
    effects: List[Dict] = []
    for r in records:
        if r.get("type") != "tune" or r.get("outcome") != "ok" or not isinstance(r.get("proposal"), dict):
            continue
        p = r["proposal"]
        metric = p.get("metric")
        if not metric:
            continue
        after = compute_metric(records, metric, since=r.get("ts"))
        days = max(1.0, (_ts_epoch(now) - _ts_epoch(r.get("ts"))) / 86400)
        before = p.get("baseline")
        window_days_before = max(1.0, (_ts_epoch(p.get("window", {}).get("until")) - _ts_epoch(p.get("window", {}).get("since"))) / 86400) if p.get("window") else float(window_days)
        if metric.get("kind") == "coverage":
            improved = (after is not None and before is not None and after > before) if p.get("better") == "higher" else None
            rate_before, rate_after = before, after
        else:
            rate_before = (before or 0.0) / window_days_before
            rate_after = (after or 0.0) / days
            improved = rate_after < rate_before
        effects.append({"id": r.get("target"), "bot": r.get("bot"), "title": p.get("title"), "applied": r.get("ts"), "days": round(days, 1),
                        "before": before, "after": after, "rate_before": round(rate_before or 0, 2), "rate_after": round(rate_after or 0, 2), "improved": improved})
        if days >= 7 and improved is False:
            add(r.get("bot", "?"), "revert", "revert:" + str(r.get("target")), "Change %s did not help after %.0f days" % (r.get("target"), days),
                "\"%s\" — metric went from %.2f to %.2f per day. Revert it or try a different fix. (%s)" % (p.get("title"), rate_before or 0, rate_after or 0, r.get("detail") or ""),
                [r], None, "info")
    order = {c: i for i, c in enumerate(TUNE_CATEGORIES)}
    proposals.sort(key=lambda p: (order.get(p["category"], 9), -p["count"]))
    return {"generated": now, "window_days": window_days, "since": since, "metrics": metrics, "proposals": proposals, "effects": effects,
            "coverage": {k: v for k, v in (cov or {}).items() if k != "unmatched"} if cov else None}


def render_tune(analysis: Dict, title: str, labels: Optional[Dict[str, str]] = None) -> str:
    labels = labels or {}
    lines = ["# %s" % title, "", "_Window: last %d days (since %s). Generated %s by gblog %s._" % (
        analysis["window_days"], analysis["since"][:10], analysis["generated"], __version__), ""]
    if not analysis["metrics"]:
        lines.append("_No records in the window._")
        return "\n".join(lines) + "\n"
    lines += ["## Bots", "", "| Bot | Tasks | Closed | OK | Open | Actions | Failed | Approvals | Denied | Median min |", "|---|---|---|---|---|---|---|---|---|---|"]
    for bot, m in sorted(analysis["metrics"].items()):
        lines.append("| %s | %d | %d | %d | %d | %d | %d | %d | %d | %s |" % (labels.get(bot, bot), m["tasks"], m["closed"], m["succeeded"], m["open"], m["actions"],
                                                                        m["failed_actions"], m["approvals"], m["denied"], m["median_minutes"]))
    if analysis.get("coverage") and analysis["coverage"].get("auto"):
        lines += ["", "Self-report coverage: **%s%%** of %d observed actions (see `gblog coverage`)." % (analysis["coverage"]["pct"], analysis["coverage"]["auto"])]
    lines += ["", "## Proposals (%d)" % len(analysis["proposals"]), ""]
    if not analysis["proposals"]:
        lines.append("_Nothing to change: no repeated failures, denials, manual repeats, slow tasks or coverage gaps in the window._")
    current = None
    for p in analysis["proposals"]:
        if p["category"] != current:
            current = p["category"]
            lines += ["### %s" % current, ""]
        lines.append("- **[%s]** %s — %s" % (p["id"], labels.get(p["bot"], p["bot"]), p["title"]))
        lines.append("  - %s" % p["suggestion"])
        if p["metric"] and p["baseline"] is not None:
            lines.append("  - Metric to watch: `%s` = %s (want %s)" % (p["metric"]["kind"], p["baseline"], p["better"]))
        lines.append("  - Evidence: %s" % ", ".join(p["evidence"]))
        lines.append("  - Apply: `gblog tune --apply %s --detail \"what you changed\"` · Reject: `gblog tune --reject %s --detail \"why\"`" % (p["id"], p["id"]))
    if analysis["effects"]:
        lines += ["", "## Effect of applied changes", "", "| Id | Bot | Change | Applied | Before/day | After/day | Verdict |", "|---|---|---|---|---|---|---|"]
        for e in analysis["effects"]:
            if e["improved"] is None:
                verdict = "n/a"
            elif e["days"] < 7:
                verdict = "too early (%s so far)" % ("better" if e["improved"] else "no change")
            else:
                verdict = "improving" if e["improved"] else "**not helping**"
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (e["id"], e["bot"], e["title"], e["applied"][:10], e["rate_before"], e["rate_after"], verdict))
    lines += ["", "---", "_Proposals are derived from the Bot's own ledger. Apply them through the Bot's description, skills, routines, "
              "Auto-review rules or memory — never by editing the ledger._"]
    return "\n".join(lines) + "\n"


def tune_apply(ledger: "Ledger", pid: str, detail: Optional[str], reject: bool = False) -> Dict:
    path = os.path.join(ledger.tuning_dir, "proposals.json")
    if not os.path.exists(path):
        raise ValueError("no proposals yet; run `gblog tune` first")
    with open(path, encoding="utf-8") as fh:
        proposals = json.load(fh).get("proposals", [])
    match = [p for p in proposals if p["id"] == pid]
    if not match:
        raise ValueError("unknown proposal id %s (run `gblog tune` to list current ids)" % pid)
    p = match[0]
    keep = {k: p.get(k) for k in ("title", "category", "key", "metric", "baseline", "better", "window")}
    return ledger.append(p["bot"], {"type": "tune", "action": "other", "target": pid, "outcome": "skipped" if reject else "ok",
                                    "detail": detail or ("rejected" if reject else "applied"), "proposal": keep})


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
    s.add_argument("--source", choices=SOURCES, help="self (default), auto (captured without the Bot), routine")

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

    s = sub.add_parser("status", help="per-bot summary (chains merged by alias; --chains for the raw per-chain view)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--chains", action="store_true")

    s = sub.add_parser("tail", help="show recent records")
    s.add_argument("--bot")
    s.add_argument("-n", type=int, default=20)

    s = sub.add_parser("sync", help="commit (and push) the ledger with git; refuses to push to a PUBLIC GitHub remote")
    s.add_argument("--bot", default="system", help="bot that performs the sync")
    s.add_argument("--remote", help="git remote URL to set as origin")
    s.add_argument("--branch", help="branch to push (default: current)")
    s.add_argument("--no-push", action="store_true")
    s.add_argument("--allow-public", action="store_true", help="push even if the remote repository is public")
    s.add_argument("-m", "--message")

    s = sub.add_parser("archive", help="store a file (transcript, message body, artifact) content-addressed and chain its sha256")
    _add_common(s)
    s.add_argument("--file", help="path to archive; use --stdin to read standard input")
    s.add_argument("--stdin", action="store_true")
    s.add_argument("--name", help="name recorded in the ledger (default: file basename)")
    s.add_argument("--kind", default="other", choices=ARCHIVE_KINDS)
    s.add_argument("--no-redact", action="store_true", help="store text verbatim (secrets are redacted by default)")

    al = sub.add_parser("alias", help="canonical Bot names: one ASCII slug per Bot, a display name, and historical variants")
    als = al.add_subparsers(dest="what", required=True)
    s = als.add_parser("add", help="register or extend a Bot: --slug kim-biseo --display 김비서님 --alias kim --alias KimBisunim")
    s.add_argument("--slug", required=True)
    s.add_argument("--display")
    s.add_argument("--alias", action="append", default=[], help="historical chain/name variant (repeatable)")
    als.add_parser("list", help="show slugs, display names, variants and which chains exist on disk")
    s = als.add_parser("strict", help="refuse (instead of warn about) names that are not registered")
    s.add_argument("mode", choices=("on", "off"))
    s = als.add_parser("resolve", help="print the canonical slug for a name")
    s.add_argument("name")

    s = sub.add_parser("export", help="dump records as JSON lines")
    s.add_argument("--bot")
    s.add_argument("--since")

    cap = sub.add_parser("capture", help="collect evidence automatically (shell hook, browser history, file changes)")
    caps = cap.add_subparsers(dest="what", required=True)
    s = caps.add_parser("shell-hook", help="print (or --install) a bash hook that logs every command")
    s.add_argument("--bot", required=True)
    s.add_argument("--install", action="store_true", help="append the hook to --rc instead of printing it")
    s.add_argument("--rc", default="~/.bashrc")
    s = caps.add_parser("browser", help="import new visits from Chrome/Chromium history")
    s.add_argument("--bot", required=True)
    s.add_argument("--history", action="append", help="path to a History sqlite file (default: auto-detect)")
    s.add_argument("--hours", type=int, default=24)
    s = caps.add_parser("files", help="log files created/modified/deleted since the last snapshot")
    s.add_argument("--bot", required=True)
    s.add_argument("--path", default="/workspace")
    s.add_argument("--exclude", action="append", default=[])
    s.add_argument("--limit", type=int, default=200)

    s = sub.add_parser("coverage", help="how much of the auto-captured activity the Bot reported itself")
    s.add_argument("--bot")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--window", type=int, default=30, help="match window in minutes")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("tune", help="derive improvement proposals for the Bots from the ledger")
    s.add_argument("--bot")
    s.add_argument("--days", type=int, default=28)
    s.add_argument("--json", action="store_true")
    s.add_argument("--stdout", action="store_true", help="print only, do not write tuning/")
    s.add_argument("--apply", metavar="ID", help="record that proposal ID was applied")
    s.add_argument("--reject", metavar="ID", help="record that proposal ID was rejected")
    s.add_argument("--detail", help="what was changed / why rejected")
    return p


def _resolve_bot(ledger: Ledger, name: str) -> str:
    slug, known = ledger.resolve(name)
    if not known:
        data = ledger.aliases()
        msg = "bot name '%s' is not registered in state/aliases.json (logging under '%s'); register it with: gblog alias add --slug <slug> --alias '%s'" % (name, slug, name)
        if data.get("strict"):
            raise SystemExit("error: " + msg)
        sys.stderr.write("warning: " + msg + "\n")
    return slug


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = Ledger(args.root)
    out = sys.stdout
    if getattr(args, "bot", None) and args.cmd in ("task-start", "log", "approval", "task-end", "note", "archive", "capture"):
        args.bot = _resolve_bot(ledger, args.bot)

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
        rec = ledger.append(args.bot, {"type": args.type, "task_id": args.task_id or os.environ.get("GBLOG_TASK"), "task": args.task,
                                       "action": args.action, "target": args.target, "outcome": args.outcome, "detail": args.detail,
                                       "source": args.source if args.source != "self" else None, "ts": args.ts})
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
                out.write("OK    %-20s %6d records  head %s%s\n" % (b, res["records"], res["head"][:12], "  (%d archived file(s) intact)" % res["archives"] if res.get("archives") else ""))
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
        bot = ledger.canonical(args.bot) if args.bot else None
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
            title += " — %s" % ledger.label(bot)
            fname += "-" + bot
        recs = ledger.canonicalize(recs)
        labels = {b: ledger.label(b) for b in {r.get("bot", "?") for r in recs}}
        text = render_report(recs, title, labels)
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
        groups = {c: [c] for c in ledger.bots()} if args.chains else ledger.groups()
        week = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
        for key, chains in groups.items():
            seq = sum(ledger.read_state(c).get("seq", 0) for c in chains)
            updated = max((ledger.read_state(c).get("updated") or "" for c in chains), default="")
            syncs = [ledger.read_state(c).get("last_sync") for c in chains if ledger.read_state(c).get("last_sync")]
            last_sync = max(syncs, key=lambda s: s.get("ts", "")) if syncs else None
            recent = [r for c in chains for r in ledger.records(c, since=week)]
            open_tasks = set()
            for r in sorted(recent, key=lambda r: r.get("ts", "")):
                if r.get("type") == "task_start":
                    open_tasks.add(r.get("task_id"))
                elif r.get("type") == "task_end":
                    open_tasks.discard(r.get("task_id"))
            today_n = sum(1 for r in recent if r.get("ts", "").startswith(today()))
            head = ledger.read_state(key).get("head", GENESIS)[:12] if key in chains else "-"
            rows.append({"bot": key, "label": ledger.label(key) if not args.chains else key, "chains": chains, "seq": seq, "head": head,
                         "updated": updated or None, "today": today_n, "open_tasks": len(open_tasks), "last_sync": last_sync})
        if args.json:
            out.write(json.dumps({"root": ledger.root, "bots": rows}, indent=2, ensure_ascii=False) + "\n")
            return 0
        out.write("root: %s\n" % ledger.root)
        if not rows:
            out.write("(no records yet)\n")
            return 0
        out.write("%-28s %6s %6s %8s %5s %20s  %s\n" % ("bot", "chains", "seq", "today", "open", "updated", "last sync"))
        for r in rows:
            ls = r["last_sync"]
            ls_txt = "%s (%s%s)" % (ls["ts"], ls["commit"], "" if ls["pushed"] else ", not pushed") if ls else "never"
            out.write("%-28s %6d %6d %8d %5d %20s  %s\n" % (r["label"], len(r["chains"]), r["seq"], r["today"], r["open_tasks"], r["updated"] or "-", ls_txt))
        return 0

    if args.cmd == "tail":
        recs = ledger.records(ledger.canonical(args.bot) if args.bot else None)[-args.n:]
        for r in recs:
            bits = [r.get("ts", ""), ledger.label(r.get("bot", "")), "#%s" % r.get("seq"), r.get("type", "")]
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
            res = sync(ledger, safe_name(args.bot), args.remote, args.branch, not args.no_push, args.message, args.allow_public)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write("git failed: %s\n" % (exc.stderr or exc.stdout).strip())
            return 2
        out.write("commit %s%s%s\n" % (res["commit"], " pushed" if res["pushed"] else " (not pushed)", "" if not res["error"] else "\n  push error: " + res["error"]))
        if res["error"] and res["error"].startswith("refused:"):
            return 3
        return 0 if (res["pushed"] or args.no_push) else 1

    if args.cmd == "export":
        for r in ledger.records(safe_name(args.bot) if args.bot else None, since=args.since):
            out.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        return 0

    if args.cmd == "archive":
        if args.stdin:
            data = sys.stdin.buffer.read()
            name = args.name or "stdin"
        elif args.file:
            with open(args.file, "rb") as fh:
                data = fh.read()
            name = args.name or os.path.basename(args.file)
        else:
            sys.stderr.write("archive: give --file PATH or --stdin\n")
            return 2
        rec = archive_file(ledger, args.bot, data, name, args.kind, args.task_id or os.environ.get("GBLOG_TASK"), args.detail,
                           do_redact=not args.no_redact, ts=args.ts)
        out.write("%s #%d archive %s sha256=%s %d bytes%s%s\n" % (rec["hash"][:12], rec["seq"], rec["path"], rec["sha256"][:12], rec["bytes"],
                                                                 " (redacted)" if rec.get("redacted") else "", " (already stored)" if rec.get("_dedup") else ""))
        return 0

    if args.cmd == "alias":
        data = ledger.aliases()
        if args.what == "add":
            entry = ledger.add_alias(args.slug, args.display, args.alias)
            out.write("%s: display=%s aliases=%s\n" % (safe_name(args.slug).lower(), entry["display"], ", ".join(entry["aliases"]) or "-"))
            return 0
        if args.what == "list":
            if not data["bots"]:
                out.write("(no aliases; every name is its own chain)%s\n" % (" strict" if data.get("strict") else ""))
                return 0
            chains = set(ledger.bots())
            out.write("strict: %s\n" % ("on" if data.get("strict") else "off"))
            for slug, e in sorted(data["bots"].items()):
                on_disk = [c for c in [slug] + e.get("aliases", []) if c in chains]
                out.write("%-20s %-16s aliases: %s\n%-20s %-16s chains on disk: %s\n" % (
                    slug, e.get("display", slug), ", ".join(e.get("aliases", [])) or "-", "", "", ", ".join(on_disk) or "-"))
            unmapped = [c for c in chains if c not in data["bots"] and ledger.canonical(c) == c]
            if unmapped:
                out.write("unregistered chains: %s\n" % ", ".join(unmapped))
            return 0
        if args.what == "strict":
            data["strict"] = args.mode == "on"
            ledger.save_aliases(data)
            out.write("strict %s\n" % args.mode)
            return 0
        if args.what == "resolve":
            slug, known = ledger.resolve(args.name)
            out.write("%s%s\n" % (slug, "" if known else "  (unregistered)"))
            return 0

    if args.cmd == "capture":
        if args.what == "shell-hook":
            if args.install:
                changed = install_shell_hook(args.bot, args.rc)
                out.write("%s %s\n" % ("installed hook in" if changed else "hook already present in", os.path.expanduser(args.rc)))
            else:
                out.write(shell_hook(args.bot))
            return 0
        if args.what == "browser":
            res = capture_browser(ledger, args.bot, args.history, args.hours)
            if not res["histories"]:
                sys.stderr.write("no browser history database found; pass --history PATH\n")
                return 1
            out.write("browser: %d new visit(s) from %d history file(s)\n" % (res["added"], len(res["histories"])))
            return 0
        if args.what == "files":
            res = capture_files(ledger, args.bot, args.path, args.exclude, args.limit)
            out.write("files: %d change(s) against a snapshot of %d file(s)\n" % (res["changes"], res["baseline"]))
            return 0

    if args.cmd == "coverage":
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
        cov = coverage(ledger.canonicalize(ledger.records(ledger.canonical(args.bot) if args.bot else None, since=since)), args.window)
        if args.json:
            out.write(json.dumps({k: v for k, v in cov.items() if k != "unmatched"}, indent=2) + "\n")
        else:
            out.write(render_coverage(cov, "Self-report coverage, last %d days" % args.days))
        return 0

    if args.cmd == "tune":
        if args.apply or args.reject:
            try:
                rec = tune_apply(ledger, args.apply or args.reject, args.detail, reject=bool(args.reject))
            except ValueError as exc:
                sys.stderr.write("%s\n" % exc)
                return 2
            out.write("%s #%d tune %s %s\n" % (rec["hash"][:12], rec["seq"], rec["target"], rec["outcome"]))
            return 0
        bot = ledger.canonical(args.bot) if args.bot else None
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
        recs = ledger.canonicalize(ledger.records(bot))
        cov = coverage([r for r in recs if r.get("ts", "") >= since])
        analysis = analyze(recs, args.days, cov)
        labels = {b: ledger.label(b) for b in analysis["metrics"]}
        if args.json:
            out.write(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n")
        else:
            out.write(render_tune(analysis, "Tuning proposals %s" % today(), labels))
        if not args.stdout:
            ledger.ensure()
            with open(os.path.join(ledger.tuning_dir, "proposals.json"), "w", encoding="utf-8") as fh:
                json.dump({"generated": analysis["generated"], "proposals": analysis["proposals"]}, fh, indent=2, ensure_ascii=False)
            path = os.path.join(ledger.tuning_dir, today() + ".md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(render_tune(analysis, "Tuning proposals %s" % today(), labels))
            sys.stderr.write("wrote %s\n" % os.path.relpath(path, ledger.root))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
