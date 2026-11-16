"""Checks on the test suite itself (round-3 audit, W-17).

``tests/PENDING.txt`` lists every test that is marked ``@unittest.expectedFailure``, one
``Class.test_name`` per line (blank lines and lines starting with ``#`` are ignored; a missing file
means that nothing is pending). The test below parses the test sources and compares the two sets,
so a marker can neither be added nor be left behind after its fix without a visible change to that
list.
"""
import ast
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(HERE, "PENDING.txt")
_ENTRY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.test[A-Za-z0-9_]*\Z")

_SAMPLE = '''
import unittest
from unittest import expectedFailure


class Sample(unittest.TestCase):
    @unittest.expectedFailure
    def test_attribute_form(self):
        pass

    @expectedFailure
    def test_name_form(self):
        pass

    @unittest.skipUnless(True, "x")
    def test_other_decorator(self):
        pass

    def test_plain(self):
        pass

    @unittest.expectedFailure
    def helper_is_not_a_test(self):
        pass


@unittest.expectedFailure
class WholeClass(unittest.TestCase):
    def test_inside(self):
        pass
'''


def _is_marker(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr == "expectedFailure"
    return isinstance(node, ast.Name) and node.id == "expectedFailure"


def marked_in_source(text, filename="<tests>"):
    """``Class.test_name`` of every test in ``text`` that is decorated with ``expectedFailure``."""
    found = set()
    for cls in ast.parse(text, filename=filename).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        whole_class = any(_is_marker(d) for d in cls.decorator_list)
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or not item.name.startswith("test"):
                continue
            if whole_class or any(_is_marker(d) for d in item.decorator_list):
                found.add("%s.%s" % (cls.name, item.name))
    return found


_OPEN_RE = re.compile(r"^\s*#\s*(bug|pending):")


def commented_open(text):
    """Names of tests with a "bug:" or "pending:" comment in the lines directly above their def (K6-02)."""
    found, flagged = set(), False
    for line in text.splitlines():
        s = line.strip()
        if _OPEN_RE.match(line):
            flagged = True
        elif s.startswith("def test") and flagged:
            found.add(s[4:s.index("(")])
            flagged = False
        elif s and not s.startswith(("#", "@")):
            flagged = False
    return found


def duplicate_names(text, filename="<tests>"):
    """Top-level class names and ``Class.test`` names defined twice: the earlier one never runs (K6-19)."""
    found, classes = [], set()
    for cls in ast.parse(text, filename=filename).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        if cls.name in classes:
            found.append(cls.name)
        classes.add(cls.name)
        seen = set()
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                if item.name in seen:
                    found.append("%s.%s" % (cls.name, item.name))
                seen.add(item.name)
    return found


# "@@" stands for "#", so that this sample is not itself read as an open comment.
_OPEN_SAMPLE = '''
class S(unittest.TestCase):
    @@ bug: X-1. documented, missing
    @unittest.expectedFailure
    def test_marked(self):
        pass

    @@ pending: X-2

    def test_unmarked(self):
        pass

    @@ bug: X-3 belongs to the assignment below
    x = 1

    def test_plain(self):
        pass
'''.replace("@@", "#")

_DUP_SAMPLE = '''
class A(unittest.TestCase):
    def test_a(self):
        pass

    def test_a(self):
        pass

    def helper(self):
        pass

    def helper(self):
        pass


class B(unittest.TestCase):
    def test_a(self):
        pass


class A(unittest.TestCase):
    def test_b(self):
        pass
'''


def _test_sources():
    for name in sorted(os.listdir(HERE)):
        if name.startswith("test") and name.endswith(".py"):
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                yield name, fh.read()


def marked_tests():
    found = set()
    for name in sorted(os.listdir(HERE)):
        if not (name.startswith("test") and name.endswith(".py")):
            continue
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            found |= marked_in_source(fh.read(), name)
    return found


def listed_tests():
    """Entries of tests/PENDING.txt in file order (a missing file lists nothing)."""
    try:
        with open(PENDING, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


class PendingListTest(unittest.TestCase):
    def test_marker_detection(self):
        # the comparison below is only worth something if a marker is actually seen
        self.assertEqual(marked_in_source(_SAMPLE),
                         {"Sample.test_attribute_form", "Sample.test_name_form", "WholeClass.test_inside"})

    def test_open_comment_detection(self):
        self.assertEqual(commented_open(_OPEN_SAMPLE), {"test_marked", "test_unmarked"})
        self.assertEqual(duplicate_names(_DUP_SAMPLE), ["A.test_a", "A"])

    def test_open_comments_have_markers(self):
        # K6-02: a test that is commented as an open bug must be marked (and therefore listed)
        stale = []
        for name, text in _test_sources():
            marked = {entry.split(".", 1)[1] for entry in marked_in_source(text, name)}
            stale += ["%s: %s" % (name, test) for test in sorted(commented_open(text) - marked)]
        self.assertEqual(stale, [], "a 'bug:' / 'pending:' comment without @unittest.expectedFailure: "
                                    "mark the test, or rewrite the comment as 'fixed: <id>'")

    def test_no_shadowed_tests_or_classes(self):
        # K6-19: a second definition of the same name silently drops the first one from the run
        found = []
        for name, text in _test_sources():
            found += ["%s: %s" % (name, dup) for dup in duplicate_names(text, name)]
        self.assertEqual(found, [])

    def test_pending_list_is_well_formed(self):
        entries = listed_tests()
        self.assertEqual([e for e in entries if not _ENTRY_RE.match(e)], [], "expected one Class.test_name per line")
        self.assertEqual(len(entries), len(set(entries)), "duplicate entries in tests/PENDING.txt")

    def test_expected_failures_are_listed(self):
        marked, listed = marked_tests(), set(listed_tests())
        self.assertEqual(
            sorted(marked - listed), [],
            "marked @unittest.expectedFailure but not listed in tests/PENDING.txt")
        self.assertEqual(
            sorted(listed - marked), [],
            "listed in tests/PENDING.txt but no longer marked: remove the line together with the marker")


if __name__ == "__main__":
    unittest.main()
