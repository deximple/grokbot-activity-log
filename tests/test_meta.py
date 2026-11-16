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
