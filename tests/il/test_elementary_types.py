"""Tests for the five elementary types added in this slice:
LWORD / DATE / DT / TOD / WSTRING.

Prior to this work the IL ``TagType`` enum was missing these
five members even though they're declared in IEC §6.4.  In
particular the conformance doc claimed §6.4 coverage was
complete -- the row called out
``BYTE / WORD / DWORD / LWORD`` and ``TIME / DATE / TOD / DT``
as ✅ but LWORD / DATE / TOD / DT / WSTRING didn't exist.

This file verifies:

  - All five new types are reachable from the builder DSL.
  - Each one round-trips through PLCopen XML and survives
    XSD validation against the bundled TC6 v2.01 schema.
  - PLCopen's lowercase ``<string/>`` / ``<wstring/>`` shape is
    emitted instead of the previous (XSD-invalid) ``<STRING/>``.
  - The type-check buckets recognise the new types so
    cross-bucket use (e.g. STRING ↔ INT) raises sensibly.
"""
from datetime import datetime, timezone

import pytest

xmlschema = pytest.importorskip("xmlschema")

from universal_machinery.builders import (
    assign, lit, move, prog, program, tag_decl, var,
)
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)
from universal_machinery.il import TagType
from universal_machinery.parsers.plcopen_xml import parse_plcopen_xml
from universal_machinery.validation import validate


_FIXED_TIME = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Enum membership
# -----------------------------------------------------------------------------


def test_new_types_are_in_TagType():
    """All five additions should be accessible by name."""
    for name in ("LWORD", "DATE", "TOD", "DT", "WSTRING"):
        assert hasattr(TagType, name), f"TagType.{name} missing"


def test_new_types_have_iec_values():
    """``TagType.NAME.value`` matches the IEC short-form keyword
    (used both by ST emission and PLCopen XML)."""
    assert TagType.LWORD.value == "LWORD"
    assert TagType.DATE.value == "DATE"
    assert TagType.TOD.value == "TOD"
    assert TagType.DT.value == "DT"
    assert TagType.WSTRING.value == "WSTRING"


# -----------------------------------------------------------------------------
# PLCopen XML round-trip + XSD validation
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("tag_type", [
    TagType.LWORD, TagType.DATE, TagType.TOD, TagType.DT, TagType.WSTRING,
])
def test_new_type_round_trips_through_xml(tag_type):
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", tag_type)])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    out = parse_plcopen_xml(xml).find_subroutine("Main")
    assert out.local_vars[0].data_type is tag_type


def test_STRING_was_previously_XSD_invalid_and_now_isnt():
    """Regression: prior emitter emitted ``<STRING/>`` which
    fails XSD validation (the schema uses lowercase
    ``<string/>``).  This test pins the fix in place."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("greeting", TagType.STRING)])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    # No raise == fixed
    validate_plcopen_xml(xml)
    assert "<string/>" in xml
    assert "<STRING/>" not in xml


def test_WSTRING_emits_lowercase_per_xsd():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("msg", TagType.WSTRING)])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<wstring/>" in xml


# -----------------------------------------------------------------------------
# Reader recognises both lowercase and uppercase forms
# -----------------------------------------------------------------------------


_HAND_ROLLED = """<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <interface>
        <localVars>
          <variable name="a"><type><string/></type></variable>
          <variable name="b"><type><wstring/></type></variable>
        </localVars>
      </interface>
    </pou>
  </pous></types>
</project>"""


def test_reader_handles_lowercase_string_and_wstring():
    p = parse_plcopen_xml(_HAND_ROLLED)
    sub = p.find_subroutine("Main")
    assert sub.local_vars[0].data_type is TagType.STRING
    assert sub.local_vars[1].data_type is TagType.WSTRING


# -----------------------------------------------------------------------------
# Type-checker buckets honour the new types
# -----------------------------------------------------------------------------


def test_lword_compatible_with_other_bit_strings():
    """LWORD lives in the bit-string bucket alongside BYTE /
    WORD / DWORD.  Moving between them shouldn't raise."""
    p = program(
        tags=[tag_decl("hi", TagType.LWORD), tag_decl("lo", TagType.DWORD)],
        subroutines=[prog("Main", main=True, rungs=[
            __import__("universal_machinery.builders",
                          fromlist=["rung"]).rung(
                move("hi", "lo")),
        ])],
    )
    codes = [e.code for e in validate(p)]
    assert "move-type-mismatch" not in codes


def test_date_dt_tod_share_time_bucket():
    """DATE / TOD / DT share TIME's bucket so cross-assignment
    doesn't raise (explicit conversion functions exist for the
    rare case where users want type-correctness)."""
    p = program(
        tags=[tag_decl("d", TagType.DATE), tag_decl("dt", TagType.DT)],
        subroutines=[prog("Main", main=True, rungs=[
            __import__("universal_machinery.builders",
                          fromlist=["rung"]).rung(
                move("d", "dt")),
        ])],
    )
    codes = [e.code for e in validate(p)]
    assert "move-type-mismatch" not in codes


def test_wstring_in_string_bucket():
    p = program(
        tags=[tag_decl("a", TagType.STRING), tag_decl("b", TagType.WSTRING)],
        subroutines=[prog("Main", main=True, rungs=[
            __import__("universal_machinery.builders",
                          fromlist=["rung"]).rung(
                move("a", "b")),
        ])],
    )
    codes = [e.code for e in validate(p)]
    assert "move-type-mismatch" not in codes


def test_string_to_bool_still_raises_across_buckets():
    """The new types in their own buckets still raise when
    crossed with unrelated types -- BOOL ↔ STRING for instance."""
    p = program(
        tags=[tag_decl("text", TagType.STRING),
               tag_decl("flag", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            __import__("universal_machinery.builders",
                          fromlist=["rung"]).rung(
                move("text", "flag")),
        ])],
    )
    codes = [e.code for e in validate(p)]
    assert "move-type-mismatch" in codes


# -----------------------------------------------------------------------------
# ST emission of the new types
# -----------------------------------------------------------------------------


def test_st_emission_uses_iec_keyword_for_each_new_type():
    """``emit_program`` renders variable declarations using
    ``TagType.value`` -- LWORD / DATE / TOD / DT / WSTRING all
    emit with their IEC keyword."""
    from universal_machinery.emitters.st import emit_program
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[
            var("a", TagType.LWORD),
            var("b", TagType.DATE),
            var("c", TagType.TOD),
            var("d", TagType.DT),
            var("e", TagType.WSTRING),
        ])])
    text = emit_program(p)
    assert "a : LWORD;" in text
    assert "b : DATE;" in text
    assert "c : TOD;" in text
    assert "d : DT;" in text
    assert "e : WSTRING;" in text
