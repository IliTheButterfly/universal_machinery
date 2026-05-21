"""Audits ``docs/CONFORMANCE_TEST_PLAN.md`` — every cited test
fixture pointer must resolve to a real file (and named test, when
the row uses the ``::test_name`` form).

Why this exists: the conformance plan rows declare ✅ / ⚠️ /
❌ status with a "Test fixtures" column that points at concrete
pytest fixtures.  When tests move, get renamed, or new
conformance rows are added without backing fixtures, the
pointers go stale silently -- and the doc starts lying about
coverage.  This test pins the doc honest by failing CI on
broken pointers.

Pointer forms recognised:
  - ``tests/foo.py``                       -- file-only
  - ``tests/foo.py::test_bar``             -- file + named test
  - ``tests/foo.py::TestClass::test_bar``  -- file + class.test

Multiple pointers per row are picked up via a regex pass over
the line; extra prose between pointers is ignored.
"""
from __future__ import annotations

import re
from pathlib import Path


# Project root (the directory containing ``docs/`` and ``tests/``):
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLAN_PATH = _REPO_ROOT / "docs" / "CONFORMANCE_TEST_PLAN.md"


#: Pointer pattern: ``tests/<path>.py`` optionally followed by
#: ``::TestClass::test_x`` or ``::test_x``.  Path segments are
#: identifier characters + ``-`` + ``/``.  The optional ``*``
#: suffix on a test name marks a wildcard ("any test starting
#: with this prefix") used by the doc to point at a family of
#: parametrised / similarly-named tests.
_POINTER_RE = re.compile(
    r"(tests/[A-Za-z0-9_/\-]+\.py)"          # file path
    r"(?:::([A-Za-z_][A-Za-z0-9_]*)(\*)?"     # optional class or test (+ *)
    r"(?:::([A-Za-z_][A-Za-z0-9_]*)(\*)?)?)?" # optional test under class
)


_BACKTICKS_RE = re.compile(r"`[^`]*`")


def _strip_backticks(line: str) -> str:
    """Drop inline-code spans so prose placeholders like
    ``tests/foo.py`` don't get treated as real pointers.  The
    actual conformance-row pointers appear in the table cells
    as bare text (no backticks)."""
    return _BACKTICKS_RE.sub("", line)


def _extract_pointers(text: str):
    """Yield ``(file_path, first_name, first_wild, second_name,
    second_wild)`` 5-tuples for every test-fixture pointer in
    ``text``.

    ``first_name`` is the class-or-test name after the first
    ``::``.  ``second_name`` is the test name under a class
    (when the pointer uses ``Class::test`` form).  The ``*``
    flags are booleans -- True iff the doc used a wildcard
    suffix on that name.

    Inline-code spans (backtick-quoted) are stripped first so
    prose placeholders ``tests/foo.py`` in narrative text don't
    register as real pointers.
    """
    for m in _POINTER_RE.finditer(_strip_backticks(text)):
        yield (
            m.group(1),
            m.group(2),
            bool(m.group(3)),
            m.group(4),
            bool(m.group(5)),
        )


def _plan_text() -> str:
    return _PLAN_PATH.read_text(encoding="utf-8")


def _table_lines(text: str):
    """Yield every Markdown-table row that lives in a section
    after ``## §`` -- i.e., the conformance-row tables, not the
    Markdown front matter or status-legend table."""
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = "§" in line or "Languages" in line or "Configuration" in line
            continue
        if in_section and line.startswith("|"):
            yield line


def test_every_pointer_in_plan_resolves_to_an_existing_file():
    """Every ``tests/foo.py`` pointer in the plan must resolve
    to a real file in the repo.  Catches renames and deletes."""
    seen: list[tuple[str, int]] = []  # (file, line_no)
    for ln, line in enumerate(_plan_text().splitlines(), start=1):
        for file_part, *_rest in _extract_pointers(line):
            seen.append((file_part, ln))
    assert seen, "no fixture pointers found -- regex broke?"
    missing: list[tuple[str, int]] = []
    for file_part, ln in seen:
        if not (_REPO_ROOT / file_part).exists():
            missing.append((file_part, ln))
    assert not missing, (
        f"Conformance plan cites missing test files:\n"
        + "\n".join(f"  line {ln}: {p}" for p, ln in missing)
    )


def test_named_test_pointers_in_plan_resolve_to_real_test_functions():
    """When a pointer uses ``tests/foo.py::test_bar`` (or the
    class-nested form), the named test must actually exist
    in the file.

    Wildcard form ``::test_prefix_*`` requires at least one
    ``def test_prefix_*...`` in the file.  Parametrised tests
    expand at runtime, so checking for the underlying function
    name is enough.
    """
    bad: list[str] = []
    for ln, line in enumerate(_plan_text().splitlines(), start=1):
        for file_part, name_a, wild_a, name_b, wild_b in _extract_pointers(line):
            if name_a is None:
                continue
            path = _REPO_ROOT / file_part
            if not path.exists():
                continue  # file-missing case caught by the other test
            src = path.read_text(encoding="utf-8")
            cls_name = name_a if name_b else None
            test_name = name_b if name_b else name_a
            test_wild = wild_b if name_b else wild_a
            if cls_name is not None:
                if not re.search(rf"^class\s+{re.escape(cls_name)}\b",
                                  src, re.M):
                    bad.append(
                        f"line {ln}: class {cls_name} not found in "
                        f"{file_part}"
                    )
                    continue
            if test_wild:
                # ``::test_prefix_*`` -- any test starting with
                # the prefix counts.
                if not re.search(
                        rf"^\s*def\s+{re.escape(test_name)}[A-Za-z0-9_]*\b",
                        src, re.M):
                    bad.append(
                        f"line {ln}: no test matching prefix "
                        f"{test_name}* found in {file_part}"
                    )
            else:
                if not re.search(rf"^\s*def\s+{re.escape(test_name)}\b",
                                  src, re.M):
                    bad.append(
                        f"line {ln}: test {test_name} not found in "
                        f"{file_part}"
                    )
    assert not bad, (
        "Conformance plan cites named tests that don't exist:\n"
        + "\n".join(f"  {x}" for x in bad)
    )


def test_status_snapshot_matches_plan_internal_total():
    """The plan's "current passing total is **N tests**" line
    and "status: **N / N passing**" line agree with each other.
    Pins that the two snapshot numbers can't drift apart inside
    the same document (the actual pytest count drift is a
    separate, more expensive check)."""
    text = _plan_text()
    counts = re.findall(r"\*\*(\d+)\s*(?:/\s*\d+\s*)?(?:tests?|passing)\*\*",
                         text)
    # Pull out each numeric snapshot
    numbers = []
    for raw in counts:
        try:
            numbers.append(int(raw))
        except ValueError:
            pass
    assert numbers, "expected at least one snapshot count in the plan"
    assert len(set(numbers)) == 1, (
        f"plan has inconsistent snapshot counts: {numbers}"
    )
