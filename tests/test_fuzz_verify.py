"""Deterministic fuzz tests for ``Ledger.verify`` and the read-only CLI commands.

Three small valid chains are written once with ``Ledger.append``. ``random.Random(20261116)``
then derives 300 single mutations from them: a flipped byte, dropped / duplicated / swapped /
inserted lines, truncation at a random offset, invalid UTF-8, JSON of the wrong shape or type,
NaN, duplicate keys, CRLF endings, edits to ``state/<bot>.json``, a deleted state file and
renamed files. Every mutation is a pure function on an in-memory ``{relative path: bytes}``
tree, so the same seed always gives the same 300 trees.

The oracle does not look at what a mutator intended. It compares the *canonical record list*
of the mutated chain with the one of the untouched chain: every non-blank line of every
``logs/<bot>/*.jsonl`` file, in file-name order, as canonical JSON text (or one marker for a
line that is not a UTF-8 JSON object). A byte-identical or whitespace-only change gives the
same list.

* The list differs -> ``verify`` must report FAIL (the untouched state file is the witness).
* The list is the same but the state file names another head -> FAIL (head mismatch).
* Otherwise the chain must still verify.

``verify`` must never raise, in the default mode and with ``strict=True``. A sample of 30
mutations is also run through the CLI (``verify --all``, ``status``, ``tail``,
``report --stdout``, ``export``): no traceback, no "internal error", a documented exit code.

Everything runs in temporary directories; no network and no live ledger is touched.
"""
import copy
import datetime as _dt
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import traceback
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gblog  # noqa: E402

SEED = 20261116
MUTATIONS = 300
CLI_SAMPLE = 30
DAYS = ("2024-06-01", "2024-06-02", "2024-06-03")

HEAD_MISMATCH = "state/%s.json: head does not match last record (state tampered or log truncated)"
STATE_MISSING = "state/%s.json missing or unreadable: tail truncation cannot be detected from state"
STATE_RESET = "state/%s.json was reset to genesis"

# README "CLI conventions and exit codes": 0 ok, 1 integrity FAIL, 2 usage/environment, 3 policy, 4 tail mismatch, 130 Ctrl-C.
_DOCUMENTED_EXIT_CODES = (0, 1, 2, 3, 4, 130)
_CLI_COMMANDS = (
    ["verify", "--all"],
    ["status"],
    ["tail"],
    ["report", "--stdout"],
    ["report", "--since", DAYS[0], "--stdout"],
    ["export"],
)


class _FrozenDatetime(_dt.datetime):
    """``datetime`` whose ``now()`` is noon of the last template day.

    ``gblog status`` reads the last 30 days of each chain. With the clock frozen here it reads the
    2024 day files of the templates (and therefore the mutated lines) on every run."""

    @classmethod
    def now(cls, tz=None):
        return cls(2024, 6, 3, 12, 0, 0, tzinfo=tz)


_FROZEN_DT = types.SimpleNamespace(datetime=_FrozenDatetime, timezone=_dt.timezone, timedelta=_dt.timedelta)


def _run(argv, root):
    """Run the CLI in-process. An uncaught exception is rendered like the real CLI would (traceback, exit 1)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = gblog.main(["--root", root] + list(argv))
        except SystemExit as exc:
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                err.write("%s\n" % code)
                rc = 1
        except Exception:  # only on a regression
            traceback.print_exc(file=err)
            rc = 1
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- byte helpers and the oracle
def _split(data):
    """The lines of a log file without their newline (the file ends with one)."""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _join(lines):
    return b"".join(line + b"\n" for line in lines)


def _dump(rec):
    """One record as gblog writes it (without the newline)."""
    return json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _dump_state(state):
    return json.dumps(state, indent=2, sort_keys=True).encode("utf-8")


_CORRUPT = "<not a UTF-8 JSON object>"


def _canon(raw):
    """Canonical text of one stripped log line, or the corrupt marker."""
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return _CORRUPT
    if not isinstance(rec, dict):
        return _CORRUPT
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain_view(tree, bot):
    """Canonical record list of ``logs/<bot>/*.jsonl`` in file-name order; blank lines do not count."""
    folder = "logs/%s/" % bot
    names = sorted(rel for rel in tree
                   if rel.startswith(folder) and rel.endswith(".jsonl") and "/" not in rel[len(folder):])
    view = []
    for rel in names:
        for raw in tree[rel].split(b"\n"):
            raw = raw.strip()
            if raw:
                view.append(_canon(raw))
    return view


def _state_of(tree, bot):
    """``state/<bot>.json`` as a dict; None when it is missing, unparsable or not an object."""
    data = tree.get("state/%s.json" % bot)
    if data is None:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _materialise(root, *trees):
    """Write in-memory trees below ``root`` (plain writes; nothing here needs to be durable)."""
    for sub in ("logs", "state"):
        os.makedirs(os.path.join(root, sub))
    for tree in trees:
        for rel, data in tree.items():
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)


# --------------------------------------------------------------------------- templates
class _Chain(object):
    """One untouched chain: its files as bytes plus what the mutators and the oracle need to know."""

    def __init__(self, bot, tree):
        self.bot = bot
        self.tree = tree
        self.state_rel = "state/%s.json" % bot
        self.logs = sorted(rel for rel in tree if rel.startswith("logs/"))
        self.records = []
        self.where = []  # record index -> (file, line index)
        for rel in self.logs:
            for at, line in enumerate(_split(tree[rel])):
                self.records.append(json.loads(line.decode("utf-8")))
                self.where.append((rel, at))
        self.head = self.records[-1]["hash"]
        self.view = _chain_view(tree, bot)


def _read_tree(root, bot):
    tree = {}
    folder = os.path.join(root, "logs", bot)
    for name in sorted(os.listdir(folder)):
        if name.endswith(".jsonl"):
            with open(os.path.join(folder, name), "rb") as fh:
                tree["logs/%s/%s" % (bot, name)] = fh.read()
    with open(os.path.join(root, "state", bot + ".json"), "rb") as fh:
        tree["state/%s.json" % bot] = fh.read()
    return tree


def _build_templates(root):
    """Three small valid chains, written by gblog itself with fixed timestamps."""
    ledger = gblog.Ledger(root)
    ledger.init()
    minute = {}

    def add(bot, day, **fields):
        n = minute.get((bot, day), 0)
        minute[(bot, day)] = n + 1
        fields["ts"] = "%sT10:%02d:00Z" % (day, n)
        ledger.append(bot, fields)

    # The templates only have to exist, not to survive a power cut: skip the fsync calls.
    with mock.patch.object(os, "fsync", lambda fd: None):
        for day in DAYS[:2]:  # alpha: 12 records in two day files
            tid = "a" + day[-2:]
            add("alpha", day, type="task_start", task_id=tid, task="Reconcile invoices")
            add("alpha", day, type="action", task_id=tid, action="shell", target="ls -la reports", outcome="ok")
            add("alpha", day, type="action", task_id=tid, action="browser", target="https://example.org/inbox",
                outcome="ok", detail="opened the inbox")
            add("alpha", day, type="approval", task_id=tid, target="send email to vendor", decision="denied",
                outcome="denied")
            add("alpha", day, type="note", task_id=tid, detail="vendor B chosen, quote A expired")
            add("alpha", day, type="task_end", task_id=tid, outcome="ok", detail="report sent")
        day = DAYS[2]  # bravo: 5 records in one day file
        add("bravo", day, type="task_start", task_id="b03", task="Weekly notes")
        add("bravo", day, type="action", task_id="b03", action="file", target="notes/todo.txt", outcome="ok",
            detail="created")
        add("bravo", day, type="action", task_id="b03", action="connector", target="calendar.create",
            outcome="fail", detail="timeout after 30 s")
        add("bravo", day, type="note", task_id="b03", detail="will retry tomorrow")
        add("bravo", day, type="task_end", task_id="b03", outcome="fail", detail="calendar entry missing")
        for day in DAYS:  # kilo: 9 records in three day files, with multi-byte text
            tid = "k" + day[-2:]
            add("kilo", day, type="task_start", task_id=tid, task="B\u00fcro aufr\u00e4umen")
            add("kilo", day, type="action", task_id=tid, action="browser", target="https://example.org/caf\u00e9",
                outcome="ok", detail="caf\u00e9 \u2615 besucht")
            add("kilo", day, type="note", task_id=tid, detail="\u65e5\u672c\u8a9e\u306e\u30e1\u30e2")
    return [_Chain(bot, _read_tree(root, bot)) for bot in ("alpha", "bravo", "kilo")]


# --------------------------------------------------------------------------- mutators
# Each mutator changes ``tree`` (a private copy of one chain's files) in place and returns a short
# description. All randomness comes from the ``rng`` it is handed.
_ODD_VALUES = ([], {}, 123.5, True, None, "x", ["a", 1], {"k": "v"}, 0, -1, "")
_RETYPE_FIELDS = ("seq", "ts", "prev", "hash", "bot", "type", "detail", "v", "task_id", "target")
_BAD_UTF8 = (b"\xff", b"\xfe\xff", b"\xc3\x28", b"\xed\xa0\x80", b"\xf8\x88\x80\x80\x80", b"\xff\xfe\xfa")
_SHAPES = (
    b"[]", b"{}", b"123", b"-0.5", b'"x"', b"null", b"true", b"NaN", b"[NaN]",
    b'{"seq": "1"}',
    b'[{"seq": 1}]',
    b'{"hash": 5, "prev": [], "seq": {}, "ts": 7}',
    b'{"ts": "2024-06-01T10:00:00Z", "bot": 5}',
    b'{"bot": "someone-else", "ts": "x", "type": "task_end"}',
    b'{"v": 1, "seq": 1, "ts": null, "bot": null, "type": null, "prev": null, "hash": null}',
    b'{"type": ["note"], "detail": {"k": [1, 2]}, "task_id": 7, "seq": 1.5}',
    b'{"type": "break_ack", "jay_approval_id": "x", "break": {}}',
    b"{not json", b'{"seq": 1,}', b"}{",
)


def _edit_line(rng, tree, chain, change):
    """Replace one record line by ``change(copy of the record, line bytes)``; returns where."""
    idx = rng.randrange(len(chain.records))
    rel, at = chain.where[idx]
    lines = _split(tree[rel])
    lines[at] = change(copy.deepcopy(chain.records[idx]), lines[at])
    tree[rel] = _join(lines)
    return "%s line %d" % (rel, at + 1)


def _m_flip_byte(rng, tree, chain):
    rel = rng.choice(chain.logs)
    data = bytearray(tree[rel])
    at = rng.randrange(len(data))
    data[at] ^= rng.randrange(1, 256)
    tree[rel] = bytes(data)
    return "%s byte %d" % (rel, at)


def _m_drop_line(rng, tree, chain):
    rel = rng.choice(chain.logs)
    lines = _split(tree[rel])
    at = rng.randrange(len(lines))
    del lines[at]
    tree[rel] = _join(lines)
    return "%s line %d" % (rel, at + 1)


def _m_duplicate_line(rng, tree, chain):
    rel = rng.choice(chain.logs)
    lines = _split(tree[rel])
    src = rng.randrange(len(lines))
    at = rng.randrange(len(lines) + 1)
    lines.insert(at, lines[src])
    tree[rel] = _join(lines)
    return "%s line %d copied to line %d" % (rel, src + 1, at + 1)


def _m_swap_lines(rng, tree, chain):
    rel = rng.choice(chain.logs)
    lines = _split(tree[rel])
    a, b = rng.sample(range(len(lines)), 2)
    lines[a], lines[b] = lines[b], lines[a]
    tree[rel] = _join(lines)
    return "%s lines %d and %d" % (rel, a + 1, b + 1)


def _m_insert_shape(rng, tree, chain):
    rel = rng.choice(chain.logs)
    lines = _split(tree[rel])
    at = rng.randrange(len(lines) + 1)
    shape = rng.choice(_SHAPES)
    lines.insert(at, shape)
    tree[rel] = _join(lines)
    return "%s line %d: %r" % (rel, at + 1, shape)


def _m_truncate(rng, tree, chain):
    rel = rng.choice(chain.logs)
    at = rng.randrange(len(tree[rel]))
    tree[rel] = tree[rel][:at]
    return "%s cut at offset %d" % (rel, at)


def _m_invalid_utf8(rng, tree, chain):
    rel = rng.choice(chain.logs)
    data = tree[rel]
    bad = rng.choice(_BAD_UTF8)
    if rng.random() < 0.3:
        lines = _split(data)
        at = rng.randrange(len(lines) + 1)
        lines.insert(at, bad)
        tree[rel] = _join(lines)
        return "%s line %d: %r" % (rel, at + 1, bad)
    at = rng.randrange(len(data) + 1)
    tree[rel] = data[:at] + bad + data[at:]
    return "%s offset %d: %r" % (rel, at, bad)


def _m_retype_keep_hash(rng, tree, chain):
    field = rng.choice(_RETYPE_FIELDS)
    value = copy.deepcopy(rng.choice(_ODD_VALUES))

    def change(rec, _line):
        rec[field] = value
        return _dump(rec)

    return "%s: %s = %s, stored hash kept" % (_edit_line(rng, tree, chain, change), field, json.dumps(value))


def _m_retype_rehash(rng, tree, chain):
    # The odd values stay small on purpose: after a hash-valid seq jump verify lists every missing
    # seq (``_describe_join`` builds ``list(range(...))``), so a huge forged seq is not fuzzed here.
    field = rng.choice([f for f in _RETYPE_FIELDS if f != "hash"])
    value = copy.deepcopy(rng.choice(_ODD_VALUES))

    def change(rec, _line):
        rec[field] = value
        rec["hash"] = gblog.compute_hash(rec)
        return _dump(rec)

    return "%s: %s = %s, hash recomputed" % (_edit_line(rng, tree, chain, change), field, json.dumps(value))


def _m_nan(rng, tree, chain):
    variant = rng.choice(("v-nan", "seq-inf", "extra-field", "seq-nan", "nested"))
    number = rng.choice((float("nan"), float("inf"), float("-inf")))

    def change(rec, _line):
        if variant == "v-nan":
            rec["v"] = float("nan")  # stored hash kept
        elif variant == "seq-inf":
            rec["seq"] = float("inf")  # stored hash kept
        else:
            if variant == "extra-field":
                rec["score"] = number
            elif variant == "seq-nan":
                rec["seq"] = float("nan")
            else:
                rec["detail"] = {"scores": [1, number]}
            rec["hash"] = gblog.compute_hash(rec)
        return _dump(rec)

    return "%s: %s" % (_edit_line(rng, tree, chain, change), variant)


def _m_dup_key_override(rng, tree, chain):
    """A second copy of a key at the end of the line: the last one wins, so the record changes."""
    chosen = []

    def change(rec, line):
        key, value = rng.choice((
            ("seq", rec["seq"] + 100), ("detail", "overridden"), ("hash", "0" * 64), ("prev", "f" * 64),
            ("bot", "someone-else"), ("ts", "1999-01-01T00:00:00Z"),
        ))
        chosen.append(key)
        return line[:-1] + (", %s: %s}" % (json.dumps(key), json.dumps(value))).encode("utf-8")

    return "%s: second %s" % (_edit_line(rng, tree, chain, change), chosen and chosen[0])


def _m_dup_key_shadow(rng, tree, chain):
    """A junk copy of a key at the start of the line: the real one still wins, so nothing changes."""
    chosen = []

    def change(_rec, line):
        key = rng.choice(("seq", "hash", "prev", "ts", "bot", "v", "type"))
        junk = rng.choice(_ODD_VALUES)
        chosen.append(key)
        return ("{%s: %s, " % (json.dumps(key), json.dumps(junk))).encode("utf-8") + line[1:]

    return "%s: shadowed %s" % (_edit_line(rng, tree, chain, change), chosen and chosen[0])


def _m_line_endings(rng, tree, chain):
    rel = rng.choice(chain.logs)
    data = tree[rel]
    variant = rng.choice(("crlf", "crlf-some", "lf-cr", "cr-only", "bom-crlf"))
    if variant == "crlf":
        data = data.replace(b"\n", b"\r\n")
    elif variant == "crlf-some":
        data = b"".join(line + (b"\r\n" if rng.random() < 0.5 else b"\n") for line in _split(data))
    elif variant == "lf-cr":
        data = data.replace(b"\n", b"\n\r")
    elif variant == "cr-only":
        data = data.replace(b"\n", b"\r")  # gblog splits on LF only: the whole file becomes one line
    else:
        data = b"\xef\xbb\xbf" + data.replace(b"\n", b"\r\n")  # a BOM in front of the first record
    tree[rel] = data
    return "%s %s" % (rel, variant)


def _m_whitespace(rng, tree, chain):
    rel = rng.choice(chain.logs)
    data = tree[rel]
    lines = _split(data)
    variant = rng.choice(("blank-lines", "trailing", "leading", "no-final-newline", "extra-newlines"))
    if variant == "blank-lines":
        for _ in range(rng.randrange(1, 4)):
            lines.insert(rng.randrange(len(lines) + 1), rng.choice((b"", b"   ", b"\t", b" \t ")))
        data = _join(lines)
    elif variant == "trailing":
        data = _join([line + rng.choice((b"", b" ", b"\t", b"  \t")) for line in lines])
    elif variant == "leading":
        data = _join([rng.choice((b"", b" ", b"\t")) + line for line in lines])
    elif variant == "no-final-newline":
        data = data[:-1]
    else:
        data = b"\n\n" + data + b"\n \n\t\n"
    tree[rel] = data
    return "%s %s" % (rel, variant)


def _m_reformat(rng, tree, chain):
    rel = rng.choice(chain.logs)
    variant = rng.choice(("compact", "reversed-keys", "ascii-escapes"))
    out = []
    for line in _split(tree[rel]):
        rec = json.loads(line.decode("utf-8"))
        if variant == "compact":
            text = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        elif variant == "reversed-keys":
            text = json.dumps(dict(reversed(list(rec.items()))), ensure_ascii=False)
        else:
            text = json.dumps(rec, ensure_ascii=True, sort_keys=True)
        out.append(text.encode("utf-8"))
    tree[rel] = _join(out)
    return "%s %s" % (rel, variant)


def _forge(chain, seq, prev, ts, **extra):
    """A hash-valid record that gblog never wrote."""
    rec = {"v": gblog.SCHEMA_VERSION, "seq": seq, "ts": ts, "bot": chain.bot, "type": "note",
           "detail": "forged record", "prev": prev}
    rec.update(extra)
    rec["hash"] = gblog.compute_hash(rec)
    return _dump(rec)


def _m_append_forged(rng, tree, chain):
    variant = rng.choice(("next", "backdated", "ack", "bad-ack", "middle", "restart"))
    idx = len(chain.records) - 1
    last = chain.records[idx]
    if variant == "next":
        line = _forge(chain, last["seq"] + 1, last["hash"], last["ts"])
    elif variant == "backdated":
        line = _forge(chain, last["seq"] + 1, last["hash"], "2000-01-01T00:00:00Z")
    elif variant == "ack":
        descriptor = {"file": chain.logs[-1], "line_before": 2, "line_after": 3, "seq_before": 2, "seq_after": 4,
                      "hash_before": "aa" * 32, "orphan_prev": "bb" * 32, "hash_after": "cc" * 32,
                      "missing_seqs": [3]}
        line = _forge(chain, last["seq"] + 1, last["hash"], last["ts"], **{
            "type": "break_ack", "jay_approval_id": "NOBODY-APPROVED-1", "break": descriptor})
    elif variant == "bad-ack":
        line = _forge(chain, last["seq"] + 1, last["hash"], last["ts"], **{
            "type": "break_ack", "jay_approval_id": "NOBODY-APPROVED-1", "break": "nope"})
    elif variant == "middle":
        idx = rng.randrange(len(chain.records) - 1)
        after = chain.records[idx]
        line = _forge(chain, after["seq"] + 1, after["hash"], after["ts"])
    else:
        line = _forge(chain, 1, gblog.GENESIS, last["ts"])
    rel, at = chain.where[idx]
    lines = _split(tree[rel])
    lines.insert(at + 1, line)
    tree[rel] = _join(lines)
    return "%s after %s line %d" % (variant, rel, at + 1)


def _m_rename_log(rng, tree, chain):
    variant = rng.choice(("bak", "upper-case", "first-to-last", "last-to-first", "keep-first", "keep-last",
                          "directory"))
    folder = "logs/%s/" % chain.bot
    first, last = chain.logs[0], chain.logs[-1]
    if variant == "bak":
        rel = rng.choice(chain.logs)
        tree[rel + ".bak"] = tree.pop(rel)
    elif variant == "upper-case":
        rel = rng.choice(chain.logs)
        tree[rel[:-len(".jsonl")] + ".JSONL"] = tree.pop(rel)
    elif variant == "first-to-last":
        tree[folder + "zz-moved.jsonl"] = tree.pop(first)
    elif variant == "last-to-first":
        tree[folder + "0000-moved.jsonl"] = tree.pop(last)
    elif variant == "keep-first":
        tree[folder + "2000-01-01.jsonl"] = tree.pop(first)
    elif variant == "keep-last":
        tree[folder + "2999-12-31.jsonl"] = tree.pop(last)
    else:
        for rel in chain.logs:
            tree["logs/%s-moved/%s" % (chain.bot, rel.rsplit("/", 1)[-1])] = tree.pop(rel)
    return variant


def _m_state_delete(rng, tree, chain):
    if rng.random() < 0.5:
        del tree[chain.state_rel]
        return "state file deleted"
    tree[chain.state_rel + ".bak"] = tree.pop(chain.state_rel)
    return "state file renamed to .bak"


def _m_state_head(rng, tree, chain):
    state = json.loads(tree[chain.state_rel].decode("utf-8"))
    variant = rng.choice(("other", "earlier", "genesis", "number", "null", "list", "missing", "upper-case",
                          "short", "empty"))
    if variant == "other":
        state["head"] = "%064x" % rng.getrandbits(256)
    elif variant == "earlier":
        rec = chain.records[rng.randrange(len(chain.records) - 1)]
        state.update({"seq": rec["seq"], "head": rec["hash"], "updated": rec["ts"]})
    elif variant == "genesis":
        state.update({"seq": 0, "head": gblog.GENESIS})
    elif variant == "number":
        state["head"] = 7
    elif variant == "null":
        state["head"] = None
    elif variant == "list":
        state["head"] = [state["head"]]
    elif variant == "missing":
        del state["head"]
    elif variant == "upper-case":
        state["head"] = state["head"].upper()
    elif variant == "short":
        state["head"] = state["head"][:12]
    else:
        state["head"] = ""
    tree[chain.state_rel] = _dump_state(state)
    return "head: %s" % variant


def _m_state_garbage(rng, tree, chain):
    data = tree[chain.state_rel]
    state = json.loads(data.decode("utf-8"))
    bogus = ("%064x" % rng.getrandbits(256)).encode("ascii")
    variant = rng.choice(("raw", "truncate", "flip", "seq", "crlf", "compact", "extra-keys", "dup-key-first",
                          "dup-key-last"))
    if variant == "raw":
        data = rng.choice((b"{not json", b"[]", b'"x"', b"null", b"123", b"", b"\xff\xfe", b"{}", b"NaN"))
    elif variant == "truncate":
        data = data[:rng.randrange(len(data))]
    elif variant == "flip":
        at = rng.randrange(len(data))
        data = data[:at] + bytes([data[at] ^ rng.randrange(1, 256)]) + data[at + 1:]
    elif variant == "seq":
        state["seq"] = copy.deepcopy(rng.choice((99, -1, 0, "x", None, float("nan"), [1])))
        data = _dump_state(state)
    elif variant == "crlf":
        data = data.replace(b"\n", b"\r\n")
    elif variant == "compact":
        data = json.dumps(state, separators=(",", ":")).encode("utf-8")
    elif variant == "extra-keys":
        state["last_sync"] = copy.deepcopy(rng.choice((
            "yesterday", {"ts": 1, "commit": "abc1234"},
            {"ts": "2024-06-03T11:00:00Z", "commit": "abc1234", "pushed": "maybe"})))
        state["updated"] = copy.deepcopy(rng.choice((5, ["x"], None, "not a time")))
        state["state_adopted"] = {"how": "missing", "seq": 1}
        data = _dump_state(state)
    elif variant == "dup-key-first":
        data = b'{"head": "' + bogus + b'", ' + data.lstrip()[1:]  # the real head comes later and wins
    else:
        data = data.rstrip()[:-1] + b', "head": "' + bogus + b'"}'  # the bogus head comes last and wins
    tree[chain.state_rel] = data
    return "state: %s" % variant


_MUTATORS = (
    ("flip_byte", _m_flip_byte),
    ("drop_line", _m_drop_line),
    ("duplicate_line", _m_duplicate_line),
    ("swap_lines", _m_swap_lines),
    ("insert_shape", _m_insert_shape),
    ("truncate", _m_truncate),
    ("invalid_utf8", _m_invalid_utf8),
    ("retype_keep_hash", _m_retype_keep_hash),
    ("retype_rehash", _m_retype_rehash),
    ("nan", _m_nan),
    ("dup_key_override", _m_dup_key_override),
    ("dup_key_shadow", _m_dup_key_shadow),
    ("line_endings", _m_line_endings),
    ("whitespace", _m_whitespace),
    ("reformat", _m_reformat),
    ("append_forged", _m_append_forged),
    ("rename_log", _m_rename_log),
    ("state_delete", _m_state_delete),
    ("state_head", _m_state_head),
    ("state_garbage", _m_state_garbage),
)
# Kinds whose every instance changes the canonical record list, and kinds that never do (and leave
# the state head alone). The other kinds can go either way; the oracle decides case by case.
_ALWAYS_CHANGES = frozenset(("drop_line", "duplicate_line", "swap_lines", "insert_shape", "invalid_utf8",
                             "retype_keep_hash", "retype_rehash", "nan", "dup_key_override", "append_forged"))
_NEVER_FAILS = frozenset(("dup_key_shadow", "whitespace", "reformat", "state_delete"))


class _Case(object):
    """One mutated tree together with what the oracle expects of it."""

    def __init__(self, index, kind, detail, chain, tree):
        self.index = index
        self.kind = kind
        self.detail = detail
        self.chain = chain
        self.tree = tree
        self.label = "#%03d %s (%s): %s" % (index, kind, chain.bot, detail)
        self.chain_changed = _chain_view(tree, chain.bot) != chain.view
        state = _state_of(tree, chain.bot)
        # What verify reads: the head of a readable state object (genesis when the key is absent).
        self.state_head = state.get("head", gblog.GENESIS) if state is not None else None
        self.state_conflict = isinstance(self.state_head, str) and self.state_head not in (gblog.GENESIS, chain.head)
        self.must_fail = self.chain_changed or self.state_conflict


def _generate_cases(chains):
    rng = random.Random(SEED)
    cases = []
    for index in range(MUTATIONS):
        kind, mutate = _MUTATORS[index % len(_MUTATORS)]
        chain = rng.choice(chains)
        tree = dict(chain.tree)
        detail = mutate(rng, tree, chain)
        cases.append(_Case(index, kind, detail, chain, tree))
    return cases


# --------------------------------------------------------------------------- tests
class FuzzVerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="gblog-fuzz-")
        try:
            cls.chains = _build_templates(os.path.join(cls.base, "template"))
            cls.cases = _generate_cases(cls.chains)
        except Exception:
            shutil.rmtree(cls.base, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_templates_are_valid_chains(self):
        self.assertEqual([(c.bot, len(c.logs), len(c.records)) for c in self.chains],
                         [("alpha", 2, 12), ("bravo", 1, 5), ("kilo", 3, 9)])
        for chain in self.chains:
            with self.subTest(bot=chain.bot):
                root = os.path.join(self.base, "clean-" + chain.bot)
                _materialise(root, chain.tree)
                ledger = gblog.Ledger(root)
                for strict in (False, True):
                    res = ledger.verify(chain.bot, strict=strict)
                    self.assertTrue(res["ok"], res["problems"])
                    self.assertEqual(res["problems"], [])
                    self.assertEqual(res["warnings"], [])
                    self.assertEqual(res["records"], len(chain.records))
                    self.assertEqual(res["head"], chain.head)
                self.assertEqual(len(chain.view), len(chain.records))
                self.assertNotIn(_CORRUPT, chain.view)

    def test_generation_is_deterministic(self):
        again = _generate_cases(self.chains)
        self.assertEqual([(c.kind, c.detail, c.tree) for c in again],
                         [(c.kind, c.detail, c.tree) for c in self.cases])

    def test_mutation_mix(self):
        self.assertEqual(len(self.cases), MUTATIONS)
        by_kind = {}
        for case in self.cases:
            by_kind.setdefault(case.kind, []).append(case)
        self.assertEqual(sorted(by_kind), sorted(kind for kind, _mutate in _MUTATORS))
        for kind, cases in sorted(by_kind.items()):
            self.assertEqual(len(cases), MUTATIONS // len(_MUTATORS), kind)
            for case in cases:
                if kind in _ALWAYS_CHANGES:
                    self.assertTrue(case.chain_changed, case.label)
                if kind in _NEVER_FAILS:
                    self.assertFalse(case.must_fail, case.label)
        self.assertGreaterEqual(sum(1 for c in self.cases if c.must_fail), 150)
        self.assertGreaterEqual(sum(1 for c in self.cases if not c.must_fail), 60)
        self.assertEqual({c.chain.bot for c in self.cases}, {"alpha", "bravo", "kilo"})

    def test_verify_never_raises_and_every_content_change_fails(self):
        for case in self.cases:
            with self.subTest(case=case.label):
                chain = case.chain
                bot = chain.bot
                root = os.path.join(self.base, "verify-%03d" % case.index)
                _materialise(root, case.tree)
                ledger = gblog.Ledger(root)
                try:
                    res = ledger.verify(bot)
                    strict = ledger.verify(bot, strict=True)
                except Exception:
                    self.fail("Ledger.verify raised:\n" + traceback.format_exc())
                for result in (res, strict):
                    self.assertIsInstance(result["ok"], bool)
                    self.assertEqual(result["ok"], not result["problems"])
                    self.assertTrue(all(isinstance(text, str) for text in result["problems"] + result["warnings"]))
                if strict["ok"]:  # strict only ever adds problems
                    self.assertTrue(res["ok"], res["problems"])
                if case.must_fail:
                    self.assertFalse(res["ok"], "a changed chain (or a contradicting state head) verified OK")
                    self.assertFalse(strict["ok"])
                    if not case.chain_changed:
                        # the log is untouched: the only finding is the head that state names
                        self.assertEqual(res["state_problem"], HEAD_MISMATCH % bot)
                        self.assertIn(HEAD_MISMATCH % bot, res["problems"])
                        self.assertEqual(res["records"], len(chain.records))
                else:
                    self.assertTrue(res["ok"], res["problems"])
                    self.assertEqual(res["records"], len(chain.records))
                    self.assertEqual(res["head"], chain.head)
                    self.assertIsNone(res["state_problem"])
                    if not isinstance(case.state_head, str):
                        self.assertIn(STATE_MISSING % bot, res["warnings"])
                    elif case.state_head == gblog.GENESIS:
                        self.assertIn(STATE_RESET % bot, res["warnings"])
                    else:
                        self.assertNotIn(STATE_MISSING % bot, res["warnings"])
                        self.assertNotIn(STATE_RESET % bot, res["warnings"])

    def test_cli_sample_never_prints_a_traceback(self):
        # (11 * k) mod 300 for k < 30: thirty different cases that cover every mutator at least once.
        picks = [self.cases[(k * 11) % len(self.cases)] for k in range(CLI_SAMPLE)]
        self.assertEqual(len({case.index for case in picks}), CLI_SAMPLE)
        self.assertEqual({case.kind for case in picks}, {kind for kind, _mutate in _MUTATORS})
        with mock.patch.object(gblog, "_dt", _FROZEN_DT):
            for case in picks:
                with self.subTest(case=case.label):
                    bot = case.chain.bot
                    others = [chain for chain in self.chains if chain is not case.chain]
                    root = os.path.join(self.base, "cli-%03d" % case.index)
                    # the mutated chain sits beside the two healthy ones, as in a real ledger
                    _materialise(root, case.tree, *[chain.tree for chain in others])
                    for argv in _CLI_COMMANDS:
                        rc, out, err = _run(argv, root)
                        said = "gblog %s -> exit %s\n%s\n%s" % (" ".join(argv), rc, out[-2000:], err[-2000:])
                        self.assertNotIn("Traceback", out + err, said)
                        self.assertNotIn("internal error", err, said)
                        self.assertIn(rc, _DOCUMENTED_EXIT_CODES, said)
                        if argv[0] == "verify":
                            self.assertEqual(rc, 1 if case.must_fail else 0, said)
                            self.assertNotIn("verify aborted", out, said)
                            status = "FAIL" if case.must_fail else "OK"
                            self.assertRegex(out, r"(?m)^%s\s+%s\s" % (status, re.escape(bot)), said)
                            if case.must_fail:
                                self.assertNotRegex(out, r"(?m)^OK\s+%s\s" % re.escape(bot), said)
                            for other in others:  # one damaged chain never hides the healthy ones
                                self.assertRegex(out, r"(?m)^OK\s+%s\s" % re.escape(other.bot), said)
                            continue
                        self.assertEqual(rc, 0, said)
                        if argv[0] == "status":
                            self.assertTrue(out.startswith("root: "), said)
                        elif argv[0] == "report":
                            self.assertTrue(out.startswith("# Activity report"), said)
                        elif argv[0] == "export":
                            rows = [line for line in out.split("\n") if line]
                            self.assertTrue(rows, said)
                            for line in rows:
                                self.assertIsInstance(json.loads(line), dict, said)


if __name__ == "__main__":
    unittest.main()
