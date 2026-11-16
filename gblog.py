#!/usr/bin/env python3
"""gblog - tamper-evident activity ledger for Grok Bot (and any other agent).

Single-file, standard-library only. Designed to run on a Grok Bot cloud
computer where manually installed packages may disappear after a reset.

Layout under GBLOG_ROOT (default: /workspace/activity-log):

    logs/<bot>/YYYY-MM-DD.jsonl   one JSON record per line, hash-chained per bot
    state/<bot>.json              chain head (seq, hash) + sync marker
    state/<bot>.lock              flock() so parallel Bots never interleave writes
    state/<bot>.alert.json        latest append refusal (tail did not match state)
    spool/<bot>/<ts>.json         event preserved when an append is refused
    anchors/<bot>.json            latest per-chain anchor (not a whole-ledger skip)
    anchors/days/YYYY-MM-DD.json  that day's per-chain anchor rollup
    anchors/<bot>.history.jsonl   append-only list of every anchored head (`gblog verify` checks they still exist)
    spool/<bot>/replayed/         spool files already re-appended by `gblog spool replay`
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
import tempfile
import uuid
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

try:  # POSIX only; on other platforms we degrade to no locking.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

__version__ = "0.6.0"
SCHEMA_VERSION = 1
GENESIS = "genesis"

# N1 found no seq 1920. The operator CLI seals only this discontinuity.
# A 12-hex prefix is not enough; verify compares the full hashes to the bytes.
KIM_BISEO_BREAK = {
    "bot": "kim-biseo",
    "file": "logs/kim-biseo/2026-11-02.jsonl",
    "line_before": 164,
    "line_after": 165,
    "seq_before": 1919,
    "seq_after": 1921,
    "hash_before": "11a6e8d43ca0155f418be8bee452330dd76177249cd51dd5383ba9c46ad3a23e",
    "orphan_prev": "268a3867a13c180b76a4a9d16d544e32647cf0695a7d15d7c6c0ee9f74d0df01",
    "hash_after": "e8519bbde0e32dd8bb449d8fe61308540f1fc18d6cd9e899461e28f01c59a78e",
    "missing_seqs": [1920],
}
_BREAK_KEYS = (
    "file", "line_before", "line_after", "seq_before", "seq_after",
    "hash_before", "orphan_prev", "hash_after", "missing_seqs",
)
# The six coordinates a temp-copy fixture ack must share with the pinned incident (hashes may differ).
_COORD_KEYS = ("file", "line_before", "line_after", "seq_before", "seq_after", "missing_seqs")
# Values the seal CLI never accepts as Jay's approval id (compared case-insensitively).
_PLACEHOLDER_IDS = ("pending-jay", "pending", "tbd", "todo", "placeholder", "<id>")


class TailMismatchError(Exception):
    """Append refused because the log tail does not match state. The event is in the spool."""

    def __init__(self, message: str, spool_path: str, alert_path: str, expected: Dict, observed: Dict):
        super().__init__(message)
        self.spool_path = spool_path
        self.alert_path = alert_path
        self.expected = expected
        self.observed = observed


class SealError(Exception):
    """Seal refused. No log line is rewritten."""


class TimestampError(ValueError):
    """Append refused because of its timestamp. Nothing was written."""


class StateRebuildError(Exception):
    """State rebuild refused. Nothing was written."""


class _UsageError(Exception):
    """Bad CLI value. main() prints ``error: ...`` and returns 2."""

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


TS_HINT = "2026-11-16T09:30:00Z"
_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?"
    r"(?:Z|([+-])([0-9]{2}):?([0-9]{2}))?\Z"
)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def parse_ts(value: object) -> str:
    """Strictly parse ``YYYY-MM-DD[T ]HH:MM:SS[.frac][Z|±HH[:]MM]`` and return ``YYYY-MM-DDTHH:MM:SSZ`` (UTC).

    No zone means UTC. Anything else raises ValueError, so a timestamp can never become a path."""
    m = _TS_RE.match(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError("timestamp must look like %s" % TS_HINT)
    try:
        when = _dt.datetime(*(int(m.group(i)) for i in range(1, 7)))
        if m.group(7):
            hours, minutes = int(m.group(8)), int(m.group(9))
            if hours > 23 or minutes > 59:
                raise ValueError("offset")
            delta = _dt.timedelta(hours=hours, minutes=minutes)
            when = when - delta if m.group(7) == "+" else when + delta
    except (ValueError, OverflowError):
        raise ValueError("timestamp must look like %s" % TS_HINT)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (when.year, when.month, when.day, when.hour, when.minute, when.second)


def _is_norm_ts(value: object) -> bool:
    try:
        return isinstance(value, str) and parse_ts(value) == value
    except ValueError:
        return False


def _die(message: str) -> None:
    """Usage/environment error: message on stderr, exit code 2 (callers still see SystemExit)."""
    sys.stderr.write("error: %s\n" % message)
    raise SystemExit(2)


def _parse_line(raw: bytes) -> Dict:
    """One log line as a dict. Bad UTF-8, bad JSON or a non-object is ``{"_corrupt": True}``."""
    try:
        text = raw.strip().decode("utf-8") if isinstance(raw, bytes) else str(raw).strip()
        rec = json.loads(text)
    except (ValueError, RecursionError):
        return {"_corrupt": True}
    return rec if isinstance(rec, dict) else {"_corrupt": True}


def _read_json(path: str) -> Optional[Dict]:
    """A JSON object from ``path``; None when missing, unparsable or not an object."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _safe_hash(record: Dict) -> Optional[str]:
    """compute_hash(), except that a record that cannot be encoded never matches."""
    try:
        return compute_hash(record)
    except (UnicodeEncodeError, RecursionError):
        return None


def _fields_ok(rec: Dict, need_ts: bool = True) -> bool:
    seq = rec.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return False
    if not isinstance(rec.get("hash"), str) or not isinstance(rec.get("prev"), str):
        return False
    return isinstance(rec.get("ts"), str) or not need_ts


def _seq_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def _clean_text(value):
    """Recursively replace text that cannot be UTF-8 encoded (lone surrogates from argv) with backslash escapes."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {_clean_text(k): _clean_text(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_text(v) for v in value]
    return value


def _state_head(state: Optional[Dict]) -> Tuple[int, object]:
    if state is None:
        return 0, GENESIS
    try:
        seq = int(state.get("seq", 0))
    except (TypeError, ValueError, OverflowError):
        seq = -1
    return seq, state.get("head", GENESIS)


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
        _die("bot name must contain letters or digits")
    return cleaned


def _is_full_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _norm_break(br: Dict) -> Dict:
    missing = br.get("missing_seqs")
    if not isinstance(missing, list):
        raise ValueError("missing_seqs")
    return {
        "file": br.get("file"),
        "line_before": int(br["line_before"]),
        "line_after": int(br["line_after"]),
        "seq_before": int(br["seq_before"]),
        "seq_after": int(br["seq_after"]),
        "hash_before": br.get("hash_before"),
        "orphan_prev": br.get("orphan_prev"),
        "hash_after": br.get("hash_after"),
        "missing_seqs": [int(x) for x in missing],
    }


def _break_shape_ok(br: Dict) -> bool:
    """One exact forward gap: consecutive lines, full hashes, listed missing seqs. Not a wildcard."""
    try:
        n = _norm_break(br)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if n["line_after"] != n["line_before"] + 1 or n["seq_after"] <= n["seq_before"]:
        return False
    if n["missing_seqs"] != list(range(n["seq_before"] + 1, n["seq_after"])):
        return False
    if n["orphan_prev"] == n["hash_before"]:
        return False
    if not all(_is_full_hash(n[k]) for k in ("hash_before", "orphan_prev", "hash_after")):
        return False
    if not isinstance(n["file"], str):
        return False
    parts = str(n["file"]).split("/")
    if not parts or ".." in parts or not n["file"].startswith("logs/") or not n["file"].endswith(".jsonl"):
        return False
    return True


def _same_break(a: Dict, b: Dict) -> bool:
    try:
        na, nb = _norm_break(a), _norm_break(b)
    except (KeyError, TypeError, ValueError):
        return False
    return _break_shape_ok(na) and na == nb


def _describe_join(prev_item: Tuple, cur_item: Tuple) -> Dict:
    prev_rel, prev_line, prev_rec = prev_item
    rel, line, rec = cur_item
    try:
        seq_before = int(prev_rec["seq"])
        seq_after = int(rec["seq"])
    except (KeyError, TypeError, ValueError):
        seq_before, seq_after = -1, -1
    missing = list(range(seq_before + 1, seq_after)) if seq_after > seq_before else []
    desc = {
        "file": rel,
        "line_before": prev_line,
        "line_after": line,
        "seq_before": seq_before,
        "seq_after": seq_after,
        "hash_before": prev_rec.get("hash"),
        "orphan_prev": rec.get("prev"),
        "hash_after": rec.get("hash"),
        "missing_seqs": missing,
    }
    if prev_rel != rel:
        desc["file_before"] = prev_rel
    return desc


def _parse_ack(rec: Dict) -> Optional[Dict]:
    if rec.get("type") != "break_ack":
        return None
    jay = rec.get("jay_approval_id")
    if not isinstance(jay, str) or not jay.strip():
        return None
    br = rec.get("break")
    if not isinstance(br, dict) or not _break_shape_ok(br):
        return None
    return _norm_break(br)


def pass_label(approved: int) -> str:
    return "PASS (%d approved break%s)" % (approved, "" if approved == 1 else "s")


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- store
class Ledger:
    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(root or default_root())
        self.logs_dir = os.path.join(self.root, "logs")
        self.state_dir = os.path.join(self.root, "state")
        self.reports_dir = os.path.join(self.root, "reports")
        self.tuning_dir = os.path.join(self.root, "tuning")
        self.archive_dir = os.path.join(self.root, "archive")
        self.anchors_dir = os.path.join(self.root, "anchors")
        self.spool_dir = os.path.join(self.root, "spool")

    # -- filesystem helpers
    def init(self) -> None:
        for d in (self.logs_dir, self.state_dir, self.reports_dir, self.tuning_dir, self.archive_dir, self.anchors_dir, self.spool_dir):
            os.makedirs(d, exist_ok=True)
        marker = os.path.join(self.root, "README.md")
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(
                    "# activity-log\n\nAppend-only, hash-chained activity ledger written by `gblog`.\n"
                    "Do not edit files under `logs/` by hand; run `gblog verify --all` to check integrity.\n"
                )

    def ensure(self) -> None:
        if not all(os.path.isdir(d) for d in (self.logs_dir, self.state_dir, self.reports_dir, self.tuning_dir, self.archive_dir, self.anchors_dir, self.spool_dir)):
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

    def _load_state(self, bot: str) -> Optional[Dict]:
        """state/<bot>.json as a dict, or None when it is missing, unparsable or not an object."""
        return _read_json(self._state_path(bot))

    def read_state(self, bot: str) -> Dict:
        data = self._load_state(bot)
        return data if data is not None else {"seq": 0, "head": GENESIS}

    def write_state(self, bot: str, state: Dict) -> None:
        """Atomically replace state/<bot>.json. Caller must already have fsynced the new log line."""
        os.makedirs(self.state_dir, exist_ok=True)
        path = self._state_path(bot)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(self.state_dir)

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
        display = display.strip() if display else None
        variants = [v.strip() for v in variants if v and v.strip()]
        for other, e in data["bots"].items():
            if other == slug:
                continue
            if slug in [a.lower() for a in e.get("aliases", [])]:
                _die("'%s' is already an alias of %s; add it there or remove it first" % (slug, other))
            for name in variants + ([display] if display else []):
                if name.lower() == other:
                    _die("'%s' is the slug of another Bot; run `gblog alias remove --slug %s` first"
                                     % (name, other))
                if name == e.get("display"):
                    _die("'%s' is already the display name of %s" % (name, other))
            if display and display in e.get("aliases", []):
                _die("display name '%s' is already an alias of %s" % (display, other))
        entry = data["bots"].setdefault(slug, {"display": slug, "aliases": []})
        if display:
            entry["display"] = display
        for v in variants:
            if v and v != slug and v not in entry["aliases"]:
                entry["aliases"].append(v)
        for other, e in data["bots"].items():
            if other != slug:
                e["aliases"] = [a for a in e.get("aliases", []) if a not in entry["aliases"]]
        self.save_aliases(data)
        return entry

    def remove_alias(self, slug: str) -> Dict:
        """Drop one registry entry. Chains on disk are never touched."""
        slug = safe_name(slug).lower()
        data = self.aliases()
        if slug not in data["bots"]:
            _die("'%s' is not a registered slug" % slug)
        entry = data["bots"].pop(slug)
        self.save_aliases(data)
        return entry

    def alias_conflicts(self) -> List[str]:
        """Registry entries that make name resolution depend on file order (left by gblog < 0.5)."""
        bots = self.aliases()["bots"]
        problems = []
        for slug in sorted(bots):
            for other in sorted(bots):
                if other != slug and slug in [a.lower() for a in bots[other].get("aliases", [])]:
                    problems.append("slug '%s' is also an alias of %s; the slug wins, so that alias is ignored "
                                    "(fix: gblog alias remove --slug %s)" % (slug, other, slug))
        by_display: Dict[str, List[str]] = {}
        for slug, e in bots.items():
            if e.get("display") and e.get("display") != slug:
                by_display.setdefault(e["display"], []).append(slug)
        for display, slugs in sorted(by_display.items()):
            if len(slugs) > 1:
                problems.append("display name '%s' is used by %s; it resolves to whichever is listed first"
                                % (display, ", ".join(sorted(slugs))))
            for other in sorted(bots):
                if other not in slugs and display in bots[other].get("aliases", []):
                    problems.append("display name '%s' of %s is also an alias of %s" % (display, ", ".join(slugs), other))
        return problems

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
            _die("'%s' has no ASCII letters or digits; register it first: "
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

    def chains_on_disk(self, name: str) -> List[str]:
        """chains_for(name), limited to chains that have a logs/<chain> directory or a state file."""
        return [c for c in self.chains_for(name)
                if os.path.isdir(os.path.join(self.logs_dir, c)) or os.path.exists(self._state_path(c))]

    def chain_name(self, name: str) -> str:
        """Exact chain when ``name`` is a chain (or spool) directory, else the canonical slug.

        Repair commands need to address a legacy variant chain that an alias would otherwise merge away."""
        raw = (name or "").strip()
        if raw in self.bots() or raw in self.spool_bots():
            return raw
        return self.canonical(raw)

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
        # An explicit empty list means "no files", not "all files" (F-03).
        paths = self.log_files(bot) if files is None else list(files)
        for path in paths:
            with open(path, "rb") as fh:
                for lineno, raw in enumerate(fh, 1):
                    if not raw.strip():
                        continue
                    yield path, lineno, _parse_line(raw)

    def _tail_record(self, bot: str) -> Tuple[Optional[str], Optional[Dict]]:
        """Last non-empty log line as (path, record). Corrupt JSON is {"_corrupt": True}."""
        for path in reversed(self.log_files(bot)):
            last = None
            with open(path, "rb") as fh:
                for line in fh:
                    if line.strip():
                        last = line
            if not last:
                continue
            return path, _parse_line(last)
        return None, None

    def _tail_head(self, bot: str) -> Tuple[int, str]:
        """Recover (seq, hash) from the last log line; used if state is missing."""
        _path, rec = self._tail_record(bot)
        if not rec or rec.get("_corrupt"):
            return 0, GENESIS
        try:
            return int(rec.get("seq", 0)), rec.get("hash", GENESIS)
        except (TypeError, ValueError):
            return 0, GENESIS

    def _atomic_json(self, path: str, obj: Dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path))

    def _append_line(self, path: str, line: str) -> None:
        """O_APPEND + fsync of the file and its directory. Never rewrites earlier bytes."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = line.encode("utf-8")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            view = data
            while view:
                wrote = os.write(fd, view)
                if wrote <= 0:
                    raise OSError("short write appending %s" % path)
                view = view[wrote:]
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(os.path.dirname(path))

    def _refuse_tail(self, bot: str, record: Dict, expected_seq: int, expected_head: str, observed: Dict) -> None:
        """Keep the event, raise, and do not touch the chain. Alert is the spool plus state/<bot>.alert.json.

        gblog does not send mail or chat. Operators page on CLI exit 4, the stderr line
        ``ALERT tail mismatch``, and a new ``state/<bot>.alert.json``. The daily routine
        should treat that file as a page. Do not delete the spool or edit the log to retry.
        """
        spool_dir = os.path.join(self.spool_dir, bot)
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(spool_dir, exist_ok=True)
        stamp = now_iso().replace("-", "").replace(":", "") + "-" + uuid.uuid4().hex[:8]
        spool_path = os.path.join(spool_dir, stamp + ".json")
        alert_path = os.path.join(self.state_dir, bot + ".alert.json")
        safe_record: Dict = {}
        for key, value in record.items():
            if key in ("v", "seq", "bot", "prev", "hash") or value is None or value == "":
                continue
            safe_record[key] = redact(value) if isinstance(value, str) else value
        payload = {
            "v": 1,
            "kind": "tail_mismatch",
            "ts": now_iso(),
            "bot": bot,
            "reason": "file tail does not match state head/hash/seq; append refused",
            "expected_seq": expected_seq,
            "expected_head": expected_head,
            "observed": observed,
            "record": safe_record,
            "spool": os.path.relpath(spool_path, self.root).replace(os.sep, "/"),
            "alert": os.path.relpath(alert_path, self.root).replace(os.sep, "/"),
        }
        payload = _clean_text(payload)
        self._atomic_json(spool_path, payload)
        self._atomic_json(alert_path, payload)
        raise TailMismatchError(
            "bot %s tail seq/hash %s/%s != state %s/%s" % (
                bot, observed.get("seq"), str(observed.get("hash") or "")[:12], expected_seq, str(expected_head)[:12],
            ),
            spool_path, alert_path,
            {"seq": expected_seq, "head": expected_head}, observed,
        )

    def _assert_tail_matches(self, bot: str, seq: int, head: str, record: Dict) -> None:
        path, rec = self._tail_record(bot)
        if path is None:
            if int(seq) == 0 and head == GENESIS:
                return
            self._refuse_tail(bot, record, seq, head, {"seq": None, "hash": None, "file": None, "corrupt": False})
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        corrupt = bool(rec.get("_corrupt")) or rec.get("hash") != _safe_hash(rec) or not _fields_ok(rec, need_ts=False)
        observed = {"seq": rec.get("seq"), "hash": rec.get("hash"), "file": rel, "corrupt": corrupt}
        if corrupt or rec["seq"] != _state_head({"seq": seq})[0] or rec.get("hash") != head:
            self._refuse_tail(bot, record, seq, head, observed)

    def _append_head(self, bot: str) -> Tuple[int, object, Optional[Dict], Optional[Dict], bool]:
        """Decide what the next append links to. Reads only; never spools.

        Returns ``(seq, head, tail_record, observed, recovered)``. ``observed`` is None when the
        append may proceed; otherwise it is the mismatch description for ``_refuse_tail`` and
        ``(seq, head)`` are the expected values. ``recovered`` is True for the one roll-forward
        case: state parses, the tail is self-consistent, ``tail.prev == state.head`` and
        ``tail.seq == state.seq + 1`` (the previous append died between the fsynced line and
        the state replace). Every other mismatch is refused.
        """
        path, tail = self._tail_record(bot)
        state_exists = os.path.exists(self._state_path(bot))
        state = self._load_state(bot) if state_exists else None
        seq, head = _state_head(state)
        if path is None or tail is None:
            if not state_exists or (head == GENESIS and (seq == 0 or not self.log_files(bot))):
                return 0, GENESIS, None, None, False
            return seq, head, None, {"seq": None, "hash": None, "file": None, "corrupt": False}, False
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        corrupt = "_corrupt" in tail or tail.get("hash") != _safe_hash(tail) or not _fields_ok(tail, need_ts=False)
        observed = {"seq": tail.get("seq"), "hash": tail.get("hash"), "file": rel, "corrupt": corrupt}
        if not state_exists:
            if corrupt:
                return 0, GENESIS, tail, observed, False
            return tail["seq"], tail["hash"], tail, None, False
        if not corrupt and tail["seq"] == seq and tail["hash"] == head:
            return seq, head, tail, None, False
        if state is not None and not corrupt and tail["prev"] == head and tail["seq"] == seq + 1:
            return tail["seq"], tail["hash"], tail, None, True
        return seq, head, tail, observed, False

    def _tail_ok(self, bot: str) -> bool:
        """Non-spooling preflight: would the next append to ``bot`` pass the tail check?"""
        return self._append_head(safe_name(bot))[3] is None

    # -- append
    def append(self, bot: str, record: Dict) -> Dict:
        """Append one record under the bot lock.

        The current code writes the line and fsyncs it before replacing state, so a crash
        inside append cannot leave state ahead of the file. State can still be ahead if the
        jsonl is replaced or truncated outside the lock afterwards: the next append used to
        trust state whenever the state file existed. That is the shape of the kim-biseo gap
        (file ends at 1919, next prev is a hash that is not in the file, seq jumps to 1921).
        Tail check refuses that append. ``sync`` and browser capture used to rewrite state
        outside the lock; those writes now re-read state under the same lock (a stale rewrite
        moves the head backward, which this check also refuses).
        """
        bot = safe_name(bot)
        record = _clean_text(record)
        explicit_ts = None
        if record.get("ts"):
            try:
                explicit_ts = parse_ts(record["ts"])
            except ValueError:
                raise TimestampError("--ts must look like %s" % TS_HINT)
        self.ensure()
        with self.lock(bot):
            seq, head, tail, observed, recovered = self._append_head(bot)
            if observed is not None:
                self._refuse_tail(bot, record, seq, head, observed)
            if recovered:
                sys.stderr.write("notice: recovered state/%s.json after an interrupted append\n" % bot)
            # Timestamps never go backwards inside a chain (verify would FAIL forever otherwise).
            tail_ts = tail.get("ts") if tail else None
            if not _is_norm_ts(tail_ts):
                tail_ts = None
            wall = now_iso()
            regressed = False
            if explicit_ts is not None:
                if tail_ts and explicit_ts < tail_ts:
                    raise TimestampError(
                        "--ts %s is earlier than the last record of %s (%s); omit --ts and put the original time in --detail"
                        % (explicit_ts, bot, tail_ts))
                ts = explicit_ts
            else:
                ts = wall
                if tail_ts and wall < tail_ts:
                    ts, regressed = tail_ts, True
            full = {"v": SCHEMA_VERSION, "seq": seq + 1, "ts": ts, "bot": bot}
            for key, value in record.items():
                if key in ("v", "seq", "ts", "bot", "prev", "hash"):
                    continue
                if value is None or value == "":
                    continue
                full[key] = redact(value) if isinstance(value, str) else value
            if regressed:
                full["clock_regressed"] = wall
            full["prev"] = head
            full["hash"] = compute_hash(full)
            path = os.path.join(self.logs_dir, bot, today(ts) + ".jsonl")
            self._append_line(path, json.dumps(full, ensure_ascii=False, sort_keys=True) + "\n")
            state = self.read_state(bot)
            state.update({"seq": full["seq"], "head": full["hash"], "updated": ts})
            self.write_state(bot, state)
            return full

    def _chain_items(self, bot: str) -> List[Tuple[str, int, Dict]]:
        items = []
        for path, lineno, rec in self.iter_records(bot):
            rel = os.path.relpath(path, self.root).replace(os.sep, "/")
            items.append((rel, lineno, rec))
        return items

    # -- verify
    def verify(self, bot: str) -> Dict:
        """Walk every segment. One break_ack may approve one exact link; any other break FAILs.

        A passing sealed chain is ok with approved_breaks >= 1. Callers print
        ``PASS (N approved break(s))`` and must not treat that as a chain with no gap.
        """
        bot = safe_name(bot)
        items = self._chain_items(bot)
        problems: List[str] = []
        warnings: List[str] = []
        acks = []
        pinned_bot = bot == KIM_BISEO_BREAK["bot"]
        for i, (rel, lineno, rec) in enumerate(items):
            if rec.get("type") != "break_ack" or "_corrupt" in rec:
                continue
            if rec.get("hash") != _safe_hash(rec):
                continue
            parsed = _parse_ack(rec)
            if parsed is None:
                problems.append("%s:%d: malformed break_ack" % (rel, lineno))
                continue
            # Verify is as strict as seal: only the pinned incident, or its coordinates on a temp-copy fixture.
            pinned = pinned_bot and _same_break(parsed, KIM_BISEO_BREAK)
            fixture = (not pinned and pinned_bot
                       and all(parsed[k] == KIM_BISEO_BREAK[k] for k in _COORD_KEYS)
                       and self._is_temp_copy())
            if not (pinned or fixture):
                problems.append("%s:%d: break_ack is not the pinned kim-biseo 2026-11-02 approval" % (rel, lineno))
                continue
            acks.append({"index": i, "break": parsed, "jay_approval_id": str(rec.get("jay_approval_id")).strip(),
                         "fixture": bool(fixture)})

        sole = acks[0] if len(acks) == 1 else None
        used = False
        segments: List[Dict] = []
        breaks: List[Dict] = []
        approved: List[Dict] = []

        def close(start: int, end: int) -> None:
            if end < start:
                return
            rel_s, line_s, rec_s = items[start]
            rel_e, line_e, rec_e = items[end]
            segments.append({
                "index_start": start,
                "index_end": end,
                "seq_start": rec_s.get("seq"),
                "seq_end": rec_e.get("seq"),
                "head": rec_e.get("hash"),
                "line_start": line_s,
                "line_end": line_e,
                "file_start": rel_s,
                "file_end": rel_e,
                "records": end - start + 1,
            })

        expected_prev = GENESIS
        expected_seq = 1
        last_ts = ""
        seg_start = 0
        count = 0
        i = 0
        n = len(items)
        while i < n:
            rel, lineno, rec = items[i]
            where = "%s:%d" % (rel, lineno)
            if "_corrupt" in rec:
                problems.append("%s: not valid JSON" % where)
                break
            if rec.get("hash") != _safe_hash(rec):
                problems.append("%s: hash mismatch (record edited)" % where)
                break
            if not _fields_ok(rec):
                problems.append("%s: malformed record fields" % where)
                break
            try:
                seq_i = int(rec["seq"])
            except (KeyError, TypeError, ValueError):
                problems.append("%s: seq %s, expected %d (missing or duplicated line)" % (where, rec.get("seq"), expected_seq))
                break
            prev_ok = rec.get("prev") == expected_prev
            seq_ok = seq_i == expected_seq
            if not (prev_ok and seq_ok):
                if i == 0:
                    if not prev_ok:
                        problems.append("%s: broken chain (prev=%s expected=%s)" % (where, str(rec.get("prev"))[:12], str(expected_prev)[:12]))
                    else:
                        problems.append("%s: seq %s, expected %d (missing or duplicated line)" % (where, rec.get("seq"), expected_seq))
                    break
                desc = _describe_join(items[i - 1], items[i])
                prev_rel = items[i - 1][0]
                ack_ok = (
                    sole is not None and not used and sole["index"] > i and prev_rel == rel
                    and _same_break(sole["break"], desc)
                )
                if ack_ok:
                    close(seg_start, i - 1)
                    approved.append(sole["break"])
                    used = True
                    seg_start = i
                    expected_prev = rec.get("prev")
                    expected_seq = seq_i
                    continue
                if not prev_ok:
                    problems.append("%s: broken chain (prev=%s expected=%s)" % (where, str(rec.get("prev"))[:12], str(expected_prev)[:12]))
                else:
                    problems.append("%s: seq %s, expected %d (missing or duplicated line)" % (where, rec.get("seq"), expected_seq))
                breaks.append(desc)
                close(seg_start, i - 1)
                seg_start = i
                expected_prev = rec.get("prev")
                expected_seq = seq_i
                continue
            ts = rec.get("ts", "") or ""
            if ts < last_ts:
                problems.append("%s: timestamp goes backwards" % where)
                break
            if ts:
                last_ts = ts
            expected_prev = rec["hash"]
            expected_seq = seq_i + 1
            count += 1
            i += 1
        if i > seg_start:
            close(seg_start, i - 1)

        if len(acks) > 1:
            problems.append("multiple break_ack records; only one approved discontinuity is allowed")
        if sole is not None and not used:
            problems.append("break_ack does not match the discontinuity at the pinned coordinates")
        if sole is not None and used and not any(s["index_start"] <= sole["index"] <= s["index_end"] for s in segments):
            problems.append("break_ack is outside a verified segment")

        final_head = segments[-1]["head"] if segments else GENESIS
        raw_state = self._load_state(bot)
        state = raw_state if raw_state is not None else {"seq": 0, "head": GENESIS}
        state_problem = None
        if not problems and count and state.get("head") not in (GENESIS, final_head):
            state_problem = "state/%s.json: head does not match last record (state tampered or log truncated)" % bot
            problems.append(state_problem)
        if items:
            if raw_state is None:
                warnings.append("state/%s.json missing or unreadable: tail truncation cannot be detected from state" % bot)
            elif state.get("head", GENESIS) == GENESIS:
                warnings.append("state/%s.json was reset to genesis" % bot)
        # Every head that was ever anchored must still be a record of this chain.
        hashes = {rec.get("hash") for _rel, _lineno, rec in items if isinstance(rec.get("hash"), str)}
        anchor_problems, anchor_warnings = self._anchor_checks(bot, hashes)
        problems.extend(anchor_problems)
        warnings.extend(anchor_warnings)
        fixture_ack = bool(used and sole is not None and sole.get("fixture"))
        if used and sole is not None and not fixture_ack \
                and self._witness_present(sole["break"], sole["jay_approval_id"]) is False:
            warnings.append("witness note missing on ledger chain")
        archives = 0
        if not problems:
            arch_recs = [rec for _rel, _lineno, rec in items if rec.get("type") == "archive"]
            archives = len(arch_recs)
            problems.extend(verify_archives(self, arch_recs))
        return {
            "bot": bot,
            "records": count,
            "head": final_head,
            "ok": not problems,
            "problems": problems,
            "archives": archives,
            "approved_breaks": len(approved),
            "approved": approved,
            "breaks": breaks,
            "segments": segments,
            "jay_approval_id": sole["jay_approval_id"] if used and sole is not None else None,
            "warnings": warnings,
            "fixture_ack": fixture_ack,
            "state_problem": state_problem,
        }

    def _anchor_history_file(self, bot: str) -> str:
        return os.path.join(self.anchors_dir, bot + ".history.jsonl")

    def _anchor_checks(self, bot: str, hashes: set) -> Tuple[List[str], List[str]]:
        """Containment of anchored heads: ``(problems, warnings)`` for anchors/<bot>.json and its history."""
        problems: List[str] = []
        warnings: List[str] = []
        reported = set()

        def check(where: str, heads: Iterable, ts: object) -> None:
            for h in heads:
                if h is None or h == GENESIS:
                    continue
                key = str(h)
                if (isinstance(h, str) and h in hashes) or key in reported:
                    continue
                reported.add(key)
                problems.append("%s: anchored head %s (%s) is no longer in the chain (history rewritten or tail truncated)"
                                % (where, key[:12], ts))

        if os.path.exists(self._anchor_file(bot)):
            data = self.read_anchor(bot)
            if data is None:
                warnings.append("anchors/%s.json is unreadable: anchored heads cannot be checked" % bot)
            else:
                heads = [data.get("head")] if data.get("ok") else data.get("segment_heads")
                check("anchors/%s.json" % bot, heads if isinstance(heads, list) else [], data.get("ts"))
        hist = self._anchor_history_file(bot)
        if os.path.exists(hist):
            with open(hist, "rb") as fh:
                for lineno, raw in enumerate(fh, 1):
                    if not raw.strip():
                        continue
                    entry = _parse_line(raw)
                    heads = entry.get("heads")
                    if "_corrupt" in entry or not isinstance(heads, list) or not all(isinstance(h, str) for h in heads):
                        problems.append("anchors/%s.history.jsonl:%d: corrupt anchor history line" % (bot, lineno))
                        continue
                    check("anchors/%s.history.jsonl:%d" % (bot, lineno), heads, entry.get("ts"))
        return problems, warnings

    def _witness_present(self, brk: Dict, jay: str) -> Optional[bool]:
        """True/False when a ``ledger`` chain exists and does/does not hold the witness note; None without one."""
        if not self.log_files("ledger"):
            return None
        for _path, _lineno, rec in self.iter_records("ledger"):
            if rec.get("type") != "note" or rec.get("jay_approval_id") != jay:
                continue
            other = rec.get("break")
            try:
                if isinstance(other, dict) and _same_break(other, brk):
                    return True
            except (AttributeError, OverflowError):
                continue
        return False

    def chain_hashes(self, bot: str) -> set:
        """Every record hash present in one chain (used by ``verify --contains``)."""
        return {rec.get("hash") for _path, _lineno, rec in self.iter_records(safe_name(bot))
                if isinstance(rec.get("hash"), str)}

    def anchored_missing(self) -> List[Tuple[str, str]]:
        """(bot, anchor ts) for every anchors/<bot>.json with records > 0 whose logs/<bot> directory is gone."""
        found: List[Tuple[str, str]] = []
        if not os.path.isdir(self.anchors_dir):
            return found
        for name in sorted(os.listdir(self.anchors_dir)):
            if not name.endswith(".json") or not os.path.isfile(os.path.join(self.anchors_dir, name)):
                continue
            bot = name[:-5]
            data = self.read_anchor(bot)
            if not data:
                continue
            n = data.get("records")
            if isinstance(n, int) and not isinstance(n, bool) and n > 0 \
                    and not os.path.isdir(os.path.join(self.logs_dir, bot)):
                found.append((bot, str(data.get("ts"))))
        return found

    def seal_break(self, bot: str, descriptor: Dict, jay_approval_id: str, copy_fixture: bool = False) -> Dict:
        """Append one break_ack for an exact on-disk gap. Does not rewrite, quarantine, or rehash.

        The CLI passes the pinned kim-biseo incident and ``copy_fixture=False``.
        Tests of a fabricated copy pass ``copy_fixture=True`` only under a temporary directory.
        The file, lines, seqs, and missing seq must still be the kim-biseo 2026-11-02 coordinates;
        only the three hashes may differ, and they must match the bytes. Not a generic ack.
        """
        jay = str(jay_approval_id or "").strip()
        if not jay:
            raise SealError("refusing: jay_approval_id is required")
        bot = safe_name(bot)
        if bot != KIM_BISEO_BREAK["bot"]:
            raise SealError("refusing: only kim-biseo 2026-11-02 can be sealed")
        try:
            desc = _norm_break(descriptor)
        except (KeyError, TypeError, ValueError):
            raise SealError("refusing: descriptor is not a single exact discontinuity")
        if not _break_shape_ok(desc):
            raise SealError("refusing: descriptor is not a single exact discontinuity")
        if not copy_fixture and not _same_break(desc, KIM_BISEO_BREAK):
            raise SealError("refusing: only the pinned kim-biseo 2026-11-02 discontinuity can be sealed")
        if copy_fixture:
            if not self._is_temp_copy():
                raise SealError("refusing: copy-fixture seal is only allowed under a temporary directory")
            coord_keys = ("file", "line_before", "line_after", "seq_before", "seq_after", "missing_seqs")
            if {k: desc[k] for k in coord_keys} != {k: KIM_BISEO_BREAK[k] for k in coord_keys}:
                raise SealError("refusing: copy fixture must use the kim-biseo 2026-11-02 coordinates")
        found = self.verify(bot).get("breaks") or []
        if len(found) != 1 or not _same_break(found[0], desc):
            raise SealError("refusing: on-disk chain does not match the pinned discontinuity")
        if any(rec.get("type") == "break_ack" for _p, _n, rec in self.iter_records(bot)):
            raise SealError("refusing: a break_ack is already present")
        # The witness goes on the ledger chain right after the ack; refuse now rather than leave an ack without it.
        if bot != "ledger" and not self._tail_ok("ledger"):
            raise SealError("refusing: ledger witness chain has a tail mismatch")
        ack = self.append(bot, {
            "type": "break_ack",
            "action": "other",
            "target": "%s:%d" % (desc["file"], desc["line_after"]),
            "outcome": "ok",
            "detail": "approved discontinuity seq %s->%s missing %s" % (desc["seq_before"], desc["seq_after"], desc["missing_seqs"]),
            "jay_approval_id": jay,
            "break": desc,
        })
        if bot != "ledger":
            self.append("ledger", {
                "type": "note",
                "action": "other",
                "target": bot,
                "outcome": "ok",
                "detail": "break_ack witness %s seq %s->%s missing %s" % (desc["file"], desc["seq_before"], desc["seq_after"], desc["missing_seqs"]),
                "jay_approval_id": jay,
                "break": desc,
            })
        return ack

    def _is_temp_copy(self) -> bool:
        root = os.path.realpath(os.path.abspath(self.root))
        tmp = os.path.realpath(os.path.abspath(tempfile.gettempdir()))
        try:
            return os.path.commonpath([root, tmp]) == tmp and root != tmp
        except ValueError:
            return False

    # -- recovery (append-only: state rebuild, spool replay; never rewrites a log line)
    def rebuild_state(self, bot: str) -> Dict:
        """Rewrite state/<bot>.json from the log tail, then append a note saying so.

        Allowed only when the chain walk has no problem other than the state-head mismatch and
        state is missing, unreadable, genesis, or names an existing record at or before the tail.
        State ahead of the log is always refused. The walk runs first; the decision and the state
        write then happen under the bot lock after re-checking that the tail is the walked head.
        """
        bot = safe_name(bot)
        if not self.log_files(bot):
            raise StateRebuildError("refusing: no log files for %s; nothing to rebuild from" % bot)
        res = self.verify(bot)
        other = [p for p in res["problems"] if p != res.get("state_problem")]
        if other:
            raise StateRebuildError("refusing: chain does not verify (%s)" % other[0])
        with self.lock(bot):
            items = self._chain_items(bot)
            if not items:
                raise StateRebuildError("refusing: no records for %s; nothing to rebuild from" % bot)
            tail = items[-1][2]
            if "_corrupt" in tail or not _fields_ok(tail) or tail["hash"] != res["head"]:
                raise StateRebuildError("refusing: the log changed while it was being checked; run the command again")
            exists = os.path.exists(self._state_path(bot))
            old = self._load_state(bot)
            old_seq, old_head = _state_head(old)
            if old is None:
                was = "seq - head %s" % ("unreadable" if exists else "missing")
            else:
                was = "seq %s head %s" % (old.get("seq"), str(old_head)[:12])
                if old_head != GENESIS:
                    known = any(rec.get("hash") == old_head and rec.get("seq") == old_seq for _rel, _lineno, rec in items)
                    if not known or old_seq > tail["seq"]:
                        raise StateRebuildError(
                            "refusing: state/%s.json (seq %s head %s) is ahead of the log or names a record that is "
                            "not in it; restore the log from backup" % (bot, old.get("seq"), str(old_head)[:12]))
            new = dict(old or {})
            new.update({"seq": tail["seq"], "head": tail["hash"], "updated": tail["ts"]})
            self.write_state(bot, new)
        note = self.append(bot, {"type": "note", "source": "auto",
                                 "detail": "state rebuilt from log tail (was %s)" % was})
        return {"bot": bot, "seq": tail["seq"], "head": tail["hash"], "note": note}

    def spool_bots(self) -> List[str]:
        if not os.path.isdir(self.spool_dir):
            return []
        return sorted(d for d in os.listdir(self.spool_dir) if os.path.isdir(os.path.join(self.spool_dir, d)))

    def spool_files(self, bot: str) -> List[str]:
        """Pending spool files of one chain in file-name (= time) order. ``replayed/`` is not pending."""
        d = os.path.join(self.spool_dir, bot)
        if not os.path.isdir(d):
            return []
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if f.endswith(".json") and os.path.isfile(os.path.join(d, f)))

    def alert_info(self, bot: str) -> Tuple[Optional[str], int]:
        """(ts of state/<bot>.alert.json or None, number of pending spool files)."""
        path = os.path.join(self.state_dir, bot + ".alert.json")
        ts = None
        if os.path.exists(path):
            data = _read_json(path)
            ts = data.get("ts") if data and isinstance(data.get("ts"), str) else "unknown"
        return ts, len(self.spool_files(bot))

    def spool_replay(self, bot: str) -> Dict:
        """Append every pending spooled event of ``bot`` in file-name order; move each file to ``replayed/``.

        Stops (``refused``) as soon as the tail no longer matches state, without spooling the event
        a second time. The alert file is renamed to ``.alert.resolved-<stamp>.json`` when nothing remains.
        """
        bot = safe_name(bot)
        replayed: List[Dict] = []
        skipped: List[str] = []
        refused = False
        for path in self.spool_files(bot):
            payload = _read_json(path)
            rec = payload.get("record") if payload else None
            if not isinstance(rec, dict):
                skipped.append(path)
                continue
            spool_id = os.path.basename(path)[:-5]
            done_dir = os.path.join(os.path.dirname(path), "replayed")
            _tail_path, tail = self._tail_record(bot)
            if not (tail and tail.get("spool_id") == spool_id):  # else: appended earlier, crashed before the move
                if not self._tail_ok(bot):
                    refused = True
                    break
                rec = dict(rec)
                if "ts" in rec:
                    rec["original_ts"] = rec.pop("ts")
                rec["spooled_at"] = payload.get("ts")
                rec["spool_id"] = spool_id
                replayed.append(self.append(bot, rec))
            os.makedirs(done_dir, exist_ok=True)
            os.replace(path, os.path.join(done_dir, os.path.basename(path)))
        remaining = self.spool_files(bot)
        resolved = None
        alert_path = os.path.join(self.state_dir, bot + ".alert.json")
        if not remaining and not refused and os.path.exists(alert_path) and self._tail_ok(bot):
            stamp = now_iso().replace("-", "").replace(":", "")
            resolved = os.path.join(self.state_dir, "%s.alert.resolved-%s.json" % (bot, stamp))
            os.replace(alert_path, resolved)
        return {"bot": bot, "replayed": replayed, "skipped": skipped, "refused": refused,
                "remaining": len(remaining), "alert_resolved": resolved}

    def _anchor_file(self, bot: str) -> str:
        return os.path.join(self.anchors_dir, bot + ".json")

    def read_anchor(self, bot: str) -> Optional[Dict]:
        try:
            with open(self._anchor_file(bot), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def anchor(self, bots: Optional[Iterable[str]] = None) -> Dict:
        """Anchor each chain. OK chains resume from their previous anchor. A FAIL chain records the break and both segment heads.

        There is no whole-ledger skip. One FAIL chain does not drop the others.
        """
        self.ensure()
        names = [safe_name(b) for b in bots] if bots is not None else self.bots()
        chains = []
        for bot in names:
            if not self.log_files(bot) and not os.path.exists(self._state_path(bot)):
                entry = {
                    "bot": bot, "ok": False, "status": "FAIL", "records": 0, "head": None,
                    "problems": ["no chain for %s" % bot], "break": None, "segments": [],
                    "segment_heads": [], "ts": now_iso(),
                }
                self._atomic_json(self._anchor_file(bot), entry)
                chains.append(entry)
                continue
            res = self.verify(bot)
            prev = self.read_anchor(bot)
            segments = res.get("segments") or []
            heads = [s.get("head") for s in segments if s.get("head")]
            resumed = None
            if prev and prev.get("ok"):
                resumed = prev.get("head")
            elif prev:
                prev_heads = prev.get("segment_heads") or []
                resumed = prev_heads[-1] if prev_heads else prev.get("head")
            if res["ok"]:
                entry = {
                    "bot": bot,
                    "ok": True,
                    "status": pass_label(res["approved_breaks"]) if res.get("approved_breaks") else "OK",
                    "records": res["records"],
                    "head": res["head"],
                    "resumed_from": resumed,
                    "approved_breaks": res.get("approved_breaks") or 0,
                    "approved": res.get("approved") or [],
                    "jay_approval_id": res.get("jay_approval_id"),
                    "ts": now_iso(),
                }
            else:
                entry = {
                    "bot": bot,
                    "ok": False,
                    "status": "FAIL",
                    "records": res["records"],
                    "head": heads[-1] if heads else None,
                    "problems": res["problems"],
                    "break": (res.get("breaks") or [None])[0],
                    "segments": [{k: s.get(k) for k in ("seq_start", "seq_end", "head", "line_start", "line_end", "file_start", "file_end", "records")} for s in segments],
                    "segment_heads": heads,
                    "ts": now_iso(),
                }
            self._atomic_json(self._anchor_file(bot), entry)
            # Append-only history: anchors/<bot>.json keeps only the latest anchor, this keeps every head.
            self._append_line(self._anchor_history_file(bot), json.dumps({
                "ts": entry["ts"],
                "ok": entry["ok"],
                "records": entry["records"],
                "heads": [h for h in ([entry["head"]] if entry["ok"] else heads) if isinstance(h, str) and h != GENESIS],
            }, sort_keys=True) + "\n")
            chains.append(entry)
        rollup = {"mode": "per-chain", "whole_ledger_skip": False, "ts": now_iso(), "chains": chains}
        day = os.path.join(self.anchors_dir, "days", today() + ".json")
        self._atomic_json(day, rollup)
        return rollup

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
                if "_corrupt" in rec or not isinstance(rec.get("ts", ""), str) or not isinstance(rec.get("bot", ""), str):
                    continue  # corrupt or malformed lines are reported by verify, never by a traceback here
                if since and rec.get("ts", "") < since:
                    continue
                out.append(rec)
        out.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("bot") or ""), _seq_int(r.get("seq"))))
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
    if ts:
        ts = parse_ts(ts)  # ValueError before anything is written; the day below becomes a directory name
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
    _git(root, "add", "-A", "logs", "reports", "state", "tuning", "archive", "anchors", "spool", "README.md")
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
        with ledger.lock(b):
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
        with ledger.lock(bot):
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
        try:
            host = (urlsplit(t if "://" in t else "//" + t).hostname or t).lower()
        except ValueError:
            host = t.lower()
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
    try:
        with open(path, encoding="utf-8") as fh:
            proposals = json.load(fh).get("proposals", [])
        match = [p for p in proposals if p["id"] == pid]
    except (ValueError, AttributeError, TypeError, KeyError):
        raise ValueError("error: %s is corrupt" % os.path.relpath(path, ledger.root).replace(os.sep, "/"))
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
    s.add_argument("--contains", action="append", default=[], metavar="HASH",
                   help="full 64-hex record hash that must still be in the chain (repeatable; requires --bot)")

    s = sub.add_parser("seal", help="seal the pinned kim-biseo 2026-11-02 break only (refuses any other coordinates)")
    s.add_argument("--bot", required=True)
    s.add_argument("--jay-approval-id", required=True, help="Jay's approval id. A placeholder is not live authorization")

    s = sub.add_parser("anchor", help="per-chain anchor; OK chains resume, a FAIL chain records the break and both segment heads")
    s.add_argument("--bot", help="one chain (still not a whole-ledger skip)")

    s = sub.add_parser("report", help="render a Markdown report")
    s.add_argument("--bot")
    s.add_argument("--date", help="YYYY-MM-DD (UTC), or `today` / `yesterday`; default today")
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
    s = als.add_parser("add", help="register or extend a Bot: --slug chief-of-staff --display 'Chief of Staff' --alias cos --alias ChiefOfStaff")
    s.add_argument("--slug", required=True)
    s.add_argument("--display")
    s.add_argument("--alias", action="append", default=[], help="historical chain/name variant (repeatable)")
    s = als.add_parser("remove", help="drop a registry entry (chains on disk are untouched)")
    s.add_argument("--slug", required=True)
    als.add_parser("list", help="show slugs, display names, variants, chains on disk, and registry conflicts")
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
    st = sub.add_parser("state", help="repair state/<bot>.json from the log (never rewrites a log line)")
    sts = st.add_subparsers(dest="what", required=True)
    s = sts.add_parser("rebuild", help="rewrite state from the log tail when the chain verifies; refuses if state is ahead of the log")
    s.add_argument("--bot", required=True)

    sp = sub.add_parser("spool", help="events kept when an append was refused (tail mismatch)")
    sps = sp.add_subparsers(dest="what", required=True)
    s = sps.add_parser("list", help="one line per pending spooled event")
    s.add_argument("--bot")
    s = sps.add_parser("replay", help="append the pending spooled events of one Bot in order and clear its alert")
    s.add_argument("--bot", required=True)
    return p


def _resolve_bot(ledger: Ledger, name: str) -> str:
    slug, known = ledger.resolve(name)
    if not known:
        data = ledger.aliases()
        msg = "bot name '%s' is not registered in state/aliases.json (logging under '%s'); register it with: gblog alias add --slug <slug> --alias '%s'" % (name, slug, name)
        if data.get("strict"):
            _die(msg)
        sys.stderr.write("warning: " + msg + "\n")
    return slug


def _validate_args(args: argparse.Namespace) -> None:
    """Normalise and bound-check --ts, --date, --since and --days before anything is written."""
    ts = getattr(args, "ts", None)
    if ts:
        try:
            args.ts = parse_ts(ts)
        except ValueError:
            raise _UsageError("--ts must look like %s" % TS_HINT)
        if _ts_epoch(args.ts) - _ts_epoch(now_iso()) > 300:
            raise _UsageError("--ts %s is more than 300 seconds in the future" % args.ts)
    if args.cmd == "report" and args.date:
        if args.date == "today":
            args.date = today()
        elif args.date == "yesterday":
            args.date = (_dt.datetime.strptime(today(), "%Y-%m-%d") - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif not _DATE_RE.match(args.date):
            raise _UsageError("--date must be YYYY-MM-DD, today or yesterday")
    if args.cmd in ("report", "export") and args.since and not _DATE_RE.match(args.since):
        raise _UsageError("--since must be YYYY-MM-DD")
    if args.cmd in ("coverage", "tune"):
        if args.days < 1:
            raise _UsageError("--days must be between 1 and 36500")
        if args.days > 36500:
            if args.cmd == "tune":
                raise _UsageError("--days must be between 1 and 36500")
            # `coverage --days 100000` is an existing, tested invocation that must keep exiting 0.
            sys.stderr.write("notice: --days %d is above the maximum; using 36500\n" % args.days)
            args.days = 36500


def main(argv: Optional[List[str]] = None) -> int:
    try:
        rc = _main(argv)
        try:
            sys.stdout.flush()
        except (AttributeError, ValueError):
            pass
        return rc
    except TailMismatchError as exc:
        sys.stderr.write("ALERT tail mismatch: %s\nspool: %s\nalert: %s\n" % (exc, exc.spool_path, exc.alert_path))
        return 4
    except (_UsageError, TimestampError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except BrokenPipeError:
        try:  # keep the interpreter from complaining again when it flushes stdout at exit
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 0
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        detail = exc.strerror or str(exc) or exc.__class__.__name__
        if exc.filename:
            detail = "%s: %s" % (detail, exc.filename)
        sys.stderr.write("error: %s\n" % detail)
        return 2


def _main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_args(args)
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
        wanted = [h.strip().lower() for h in (args.contains or [])]
        if wanted and not args.bot:
            raise _UsageError("verify --contains requires --bot")
        if any(not _is_full_hash(h) for h in wanted):
            raise _UsageError("verify --contains needs a full 64-hex record hash")
        gone: List[Tuple[str, str]] = []
        if args.all or not args.bot:
            bots = ledger.bots()
            gone = ledger.anchored_missing()  # a chain that was anchored and then removed must not vanish silently
            if not bots and not gone:
                out.write("no bots logged yet under %s\n" % ledger.root)
                return 0
        else:
            bots = ledger.chains_on_disk(args.bot)  # slug, display name or alias -> every chain of that Bot
            if not bots:
                out.write("FAIL  %-20s %6d records\n" % (args.bot, 0))
                out.write("      - no chain on disk for '%s'\n" % args.bot)
                return 1
        not_found: List[str] = []
        if wanted:
            present = set()
            for b in bots:
                present |= ledger.chain_hashes(b)
            not_found = [h for h in wanted if h not in present]
        rc = 0
        for b in bots:
            res = ledger.verify(b)
            arch = "  (%d archived file(s) intact)" % res["archives"] if res.get("archives") else ""
            if not_found and b == bots[-1]:
                res["problems"].extend("expected hash %s not found in chain" % h[:12] for h in not_found)
                res["ok"] = False
            if res["ok"] and res.get("approved_breaks"):
                out.write("%s  %-20s %6d records  head %s  approved_discontinuities=%d%s\n" % (
                    pass_label(res["approved_breaks"]), b, res["records"], res["head"][:12], res["approved_breaks"], arch))
            elif res["ok"]:
                out.write("OK    %-20s %6d records  head %s%s\n" % (b, res["records"], res["head"][:12], arch))
            else:
                rc = 1
                out.write("FAIL  %-20s %6d records\n" % (b, res["records"]))
                for prob in res["problems"]:
                    out.write("      - %s\n" % prob)
            for warn in res.get("warnings") or []:
                out.write("      ! %s\n" % warn)
            if args.record:
                detail = "; ".join(res["problems"]) or "%d records verified" % res["records"]
                if res["ok"] and res.get("approved_breaks"):
                    detail = "%s; %s" % (pass_label(res["approved_breaks"]), detail)
                ledger.append(b, {"type": "verify", "action": "other", "target": "chain", "outcome": "ok" if res["ok"] else "fail",
                                  "detail": detail})
        for name, anchored_ts in gone:
            rc = 1
            out.write("FAIL  %-20s %6d records\n" % (name, 0))
            out.write("      - chain was anchored on %s but logs/%s is missing\n" % (anchored_ts, name))
        return rc

    if args.cmd == "seal":
        jay_id = str(args.jay_approval_id).strip()
        if jay_id.lower() in _PLACEHOLDER_IDS or len(jay_id) < 4:
            sys.stderr.write("seal: refusing placeholder jay_approval_id %s; pass Jay's real approval id\n" % (jay_id or "(empty)"))
            return 2
        try:
            rec = ledger.seal_break(args.bot, KIM_BISEO_BREAK, args.jay_approval_id, copy_fixture=False)
        except SealError as exc:
            sys.stderr.write("seal: %s\n" % exc)
            return 2
        out.write("sealed %s #%d jay_approval_id=%s\n" % (rec["hash"][:12], rec["seq"], args.jay_approval_id))
        return 0

    if args.cmd == "anchor":
        bots = None
        if args.bot:
            bots = ledger.chains_on_disk(args.bot)
            if not bots:  # a typo must not create anchors/<typo>.json
                out.write("FAIL  %-20s %6d records\n" % (args.bot, 0))
                out.write("      - no chain on disk for '%s'\n" % args.bot)
                return 1
        rollup = ledger.anchor(bots)
        for c in rollup["chains"]:
            if c["ok"]:
                extra = (" " + c["status"]) if c.get("approved_breaks") else ""
                resumed = c.get("resumed_from") or "-"
                out.write("ANCHOR %-4s %-20s head %s resumed_from %s%s\n" % (
                    "PASS" if c.get("approved_breaks") else "OK", c["bot"], str(c.get("head") or "")[:12], str(resumed)[:12], extra))
            else:
                br = c.get("break") or {}
                heads = " ".join(str(h)[:12] for h in (c.get("segment_heads") or []))
                out.write("ANCHOR FAIL %-20s break %s:%s seq %s->%s heads %s\n" % (
                    c["bot"], br.get("file"), br.get("line_after"), br.get("seq_before"), br.get("seq_after"), heads))
        return 0 if all(c["ok"] for c in rollup["chains"]) else 1

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
        for r in rows:
            alerts = [ledger.alert_info(c) for c in r["chains"]]
            stamps = sorted(a_ts for a_ts, _n in alerts if a_ts)
            r["alert"] = stamps[0] if stamps else None
            r["spooled"] = sum(a_n for _ts, a_n in alerts)
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
        for r in rows:
            for c in r["chains"]:
                a_ts, a_n = ledger.alert_info(c)
                if a_ts:
                    out.write("ALERT %s: tail mismatch since %s (%d spooled)\n" % (c, a_ts, a_n))
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
        # Same resolver as the write commands. The default housekeeping chain "system" resolves quietly,
        # so strict alias mode cannot block backups.
        args.bot = ledger.canonical(args.bot) if args.bot == "system" else _resolve_bot(ledger, args.bot)
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
        for r in ledger.records(ledger.canonical(args.bot) if args.bot else None, since=args.since):
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
            conflicts = ledger.alias_conflicts()
            if conflicts:
                out.write("conflicts:\n" + "".join("  - %s\n" % c for c in conflicts))
            return 0
        if args.what == "remove":
            slug = safe_name(args.slug).lower()
            entry = ledger.remove_alias(slug)
            out.write("removed %s (display=%s); chains on disk untouched; '%s' now resolves to %s\n"
                      % (slug, entry.get("display", slug), slug, ledger.canonical(slug)))
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

    if args.cmd == "state":
        if args.what == "rebuild":
            chain = ledger.chain_name(args.bot)
            try:
                res = ledger.rebuild_state(chain)
            except StateRebuildError as exc:
                sys.stderr.write("state rebuild: %s\n" % exc)
                return 2
            out.write("rebuilt %s seq %d head %s\n" % (chain, res["seq"], res["head"][:12]))
            return 0

    if args.cmd == "spool":
        if args.what == "list":
            chains = [ledger.chain_name(args.bot)] if args.bot else ledger.spool_bots()
            shown = 0
            for chain in chains:
                for path in ledger.spool_files(chain):
                    payload = _read_json(path) or {}
                    rec = payload.get("record") if isinstance(payload.get("record"), dict) else {}
                    detail = " ".join(str(rec.get("detail") or rec.get("target") or "").split())[:60]
                    out.write("%s  %-20s %-10s %s\n" % (payload.get("ts") or "?", chain,
                                                        rec.get("type") or ("?" if payload else "corrupt"), detail))
                    shown += 1
            if not shown:
                sys.stderr.write("no spooled events\n")
            return 0
        if args.what == "replay":
            chain = ledger.chain_name(args.bot)
            res = ledger.spool_replay(chain)
            for rec in res["replayed"]:
                out.write("%s #%d %s replayed from spool %s\n" % (rec["hash"][:12], rec["seq"], rec.get("type", ""), rec.get("spool_id")))
            for path in res["skipped"]:
                sys.stderr.write("spool replay: %s is unreadable; left in place\n" % os.path.relpath(path, ledger.root))
            if res["refused"]:
                sys.stderr.write("ALERT tail mismatch: %s still does not match state; %d spooled event(s) left in spool/%s\n"
                                 % (chain, res["remaining"], chain))
                return 4
            out.write("replayed %d spooled event(s) for %s%s\n" % (
                len(res["replayed"]), chain, "; alert resolved" if res["alert_resolved"] else ""))
            return 2 if res["skipped"] else 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
