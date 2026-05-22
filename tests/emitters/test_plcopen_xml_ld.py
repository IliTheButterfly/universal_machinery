"""PLCopen XML emission + reader for native LD bodies (IEC §6.6).

Pure-LD rungs (contacts + coils only) now emit as native
``<body><LD>...</LD></body>`` with the canonical
leftPowerRail → contacts → coil → rightPowerRail chain, and
round-trip cleanly through the reader.

Mixed rungs that contain math / call / stdlib / parallel-group
ops still fall back to ST translation (a follow-up will route
those through ``<block>`` elements per the fbdObjects group).
"""
from datetime import datetime, timezone

import pytest

xmlschema = pytest.importorskip("xmlschema")

from universal_machinery.builders import (
    add, coil, fedge, nc, no, prog, program, redge, reset_, rung, set_,
    tag, tag_decl,
)
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)
from universal_machinery.il import Address, TagRef, TagType
from universal_machinery.il.ops import (
    ContactFallingEdge, ContactNC, ContactNO, ContactRisingEdge,
    OutCoil, OutReset, OutSet, ParallelGroup,
)
from universal_machinery.parsers.plcopen_xml import parse_plcopen_xml


_FIXED_TIME = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


def _round_trip(rungs):
    p = program(subroutines=[prog("Main", main=True, rungs=rungs)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    return parse_plcopen_xml(xml).find_subroutine("Main").rungs


# -----------------------------------------------------------------------------
# Emission shape + XSD validation
# -----------------------------------------------------------------------------


def test_pure_ld_rung_emits_native_LD_element():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X1"), coil("Y1")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<leftPowerRail" in xml
    assert "<contact " in xml
    assert "<coil " in xml
    assert "<rightPowerRail" in xml


def test_negated_contact_emits_negated_true():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(nc("estop"), coil("running")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'negated="true"' in xml


def test_set_coil_emits_storage_set():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("start"), set_("running")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'storage="set"' in xml


def test_reset_coil_emits_storage_reset():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("stop"), reset_("running")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'storage="reset"' in xml


def test_multiple_rungs_each_get_own_power_rails():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X1"), coil("Y1")),
        rung(no("X2"), coil("Y2")),
        rung(no("X3"), coil("Y3")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Three rungs -> three left rails + three right rails
    assert xml.count("<leftPowerRail") == 3
    assert xml.count("<rightPowerRail") == 3


def test_mixed_rung_falls_back_to_ST_translation():
    """``End`` is the only IL op without a native-LD shape --
    the IEC §6.6 "end of main program" semantics have no
    graphical XSD element.  Every other op (contacts, coils,
    parallel groups, Compare, Move, BinaryMath, StdFunc, Call,
    all FBs, Jump / Label / Return) now lowers to native LD."""
    from universal_machinery.builders import end
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(end()),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Not LD body
    assert "<LD>" not in xml


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


def test_round_trip_single_no_coil_rung():
    rungs = [rung(no("X1"), coil("Y1"))]
    out = _round_trip(rungs)
    assert len(out) == 1
    ops = out[0].ops
    assert len(ops) == 2
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], OutCoil)


def test_round_trip_no_then_nc_then_set_rung():
    rungs = [rung(no("start"), nc("estop"), set_("running"))]
    out = _round_trip(rungs)
    assert len(out) == 1
    ops = out[0].ops
    assert [type(op).__name__ for op in ops] == [
        "ContactNO", "ContactNC", "OutSet",
    ]


def test_round_trip_reset_coil():
    rungs = [rung(nc("idle"), reset_("latched"))]
    out = _round_trip(rungs)
    assert isinstance(out[0].ops[-1], OutReset)


def test_round_trip_address_vs_tagref_classification_survives():
    """CLICK-style ``X001`` round-trips as Address; symbolic
    names round-trip as TagRef."""
    rungs = [rung(no("X001"), no("running"), coil("Y001"))]
    out = _round_trip(rungs)
    ops = out[0].ops
    assert ops[0].address == Address("X001")
    assert ops[1].address == TagRef("running")
    assert ops[2].address == Address("Y001")


def test_round_trip_multiple_rungs_preserve_order():
    rungs = [
        rung(no("a"), coil("x")),
        rung(no("b"), set_("y")),
        rung(nc("c"), reset_("z")),
    ]
    out = _round_trip(rungs)
    assert len(out) == 3
    # Coil identity check across all three
    assert isinstance(out[0].ops[-1], OutCoil)
    assert isinstance(out[1].ops[-1], OutSet)
    assert isinstance(out[2].ops[-1], OutReset)


def test_empty_body_round_trips():
    p = program(subroutines=[prog("Main", main=True)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    out = parse_plcopen_xml(xml).find_subroutine("Main")
    assert out.rungs == []


# -----------------------------------------------------------------------------
# Body dispatch precedence
# -----------------------------------------------------------------------------


def test_ld_body_picked_over_st_when_rungs_pure_LD():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X1"), coil("Y1")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Main POU body uses <LD>, not <ST>.  (The synthetic
    # GlobalsHolder POU still uses <ST>, so we don't assert no <ST>
    # anywhere -- we check on the Main POU specifically by scoping
    # to the substring after Main's opening tag.)
    main_chunk = xml.split('pou name="Main"')[1].split("</pou>")[0]
    assert "<LD>" in main_chunk
    assert "<ST>" not in main_chunk


def test_ld_reader_recognises_negated_contact_and_coil():
    """Hand-rolled XML with negated contact + coil parses back as
    ContactNC + OutCoil (negated coil isn't currently in the IL,
    so the negated flag on coil is silently dropped -- which
    matches the emitter side)."""
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <interface/>
      <body><LD>
        <leftPowerRail localId="0"><position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/></leftPowerRail>
        <contact localId="1" negated="true">
          <position x="100" y="0"/>
          <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
          <connectionPointOut/>
          <variable>flag</variable></contact>
        <coil localId="2">
          <position x="200" y="0"/>
          <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
          <connectionPointOut/>
          <variable>out</variable></coil>
        <rightPowerRail localId="3">
          <position x="300" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    assert len(sub.rungs) == 1
    ops = sub.rungs[0].ops
    assert isinstance(ops[0], ContactNC)
    assert ops[0].address == TagRef("flag")
    assert isinstance(ops[1], OutCoil)


# -----------------------------------------------------------------------------
# Edge contacts (XSD ``edge="rising"`` / ``edge="falling"`` on
# ``<contact>``).  IL distinguishes via ``ContactRisingEdge`` /
# ``ContactFallingEdge``.
# -----------------------------------------------------------------------------


def test_rising_edge_contact_emits_edge_attribute():
    """``redge('clk')`` -> ``<contact edge="rising">`` per the
    XSD's ``edgeModifierType`` enumeration."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(redge("clk"), coil("out")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'edge="rising"' in xml
    assert 'edge="falling"' not in xml
    # The contact element shouldn't pick up a ``negated`` flag from
    # the edge -- ContactRisingEdge isn't a negated contact.
    contact_elem = xml.split("<contact ")[1].split(">")[0]
    assert 'negated="true"' not in contact_elem


def test_falling_edge_contact_emits_edge_attribute():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(fedge("clk"), coil("out")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'edge="falling"' in xml


def test_edge_contact_body_no_longer_falls_back_to_ST():
    """Rungs with edge contacts now stay in native ``<LD>`` rather
    than dropping to the ST-text fallback emit path."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(redge("X"), coil("Y")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    # The ST-text fallback wraps the body in an <ST> element with
    # the rung text inside; absence pins the native path.
    assert "<ST>" not in xml


def test_rising_edge_contact_round_trips():
    rungs = _round_trip([rung(redge("X001"), coil("Y001"))])
    assert isinstance(rungs[0].ops[0], ContactRisingEdge)
    assert rungs[0].ops[0].address == Address("X001")


def test_falling_edge_contact_round_trips():
    rungs = _round_trip([rung(fedge("X002"), coil("Y002"))])
    assert isinstance(rungs[0].ops[0], ContactFallingEdge)
    assert rungs[0].ops[0].address == Address("X002")


def test_mixed_edge_and_regular_contacts_in_one_body():
    """A POU body with a mix of NO / NC / rising / falling
    contacts should round-trip every contact kind correctly."""
    rungs = _round_trip([
        rung(no("A"), coil("oA")),
        rung(nc("B"), coil("oB")),
        rung(redge("C"), coil("oC")),
        rung(fedge("D"), coil("oD")),
    ])
    kinds = [type(r.ops[0]).__name__ for r in rungs]
    assert kinds == [
        "ContactNO", "ContactNC",
        "ContactRisingEdge", "ContactFallingEdge",
    ]


def test_edge_contact_chained_with_other_contacts():
    """A rung with multiple contacts (gate) followed by a coil
    keeps every contact's kind during round-trip."""
    rungs = _round_trip([
        rung(redge("clk"), no("enable"), coil("pulse_out")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], ContactRisingEdge)
    assert isinstance(ops[1], ContactNO)
    assert isinstance(ops[2], OutCoil)


# -----------------------------------------------------------------------------
# Reader-only: hand-rolled documents
# -----------------------------------------------------------------------------


def test_reader_parses_hand_rolled_edge_attribute():
    """A document with ``edge="rising"`` on a ``<contact>`` (no
    matter who emitted it) should lower into ``ContactRisingEdge``
    on read."""
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <contact localId="1" edge="rising">
          <position x="100" y="0"/>
          <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
          <connectionPointOut/>
          <variable>clk</variable>
        </contact>
        <coil localId="2">
          <position x="200" y="0"/>
          <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
          <connectionPointOut/>
          <variable>out</variable>
        </coil>
        <rightPowerRail localId="3">
          <position x="300" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    assert isinstance(sub.rungs[0].ops[0], ContactRisingEdge)


def test_reader_treats_missing_edge_attribute_as_plain_NO():
    """``edge="none"`` (the XSD default) should not promote a
    plain NO contact into an edge contact."""
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <contact localId="1" edge="none">
          <position x="100" y="0"/>
          <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
          <connectionPointOut/>
          <variable>flag</variable>
        </contact>
        <coil localId="2">
          <position x="200" y="0"/>
          <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
          <connectionPointOut/>
          <variable>out</variable>
        </coil>
        <rightPowerRail localId="3">
          <position x="300" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    assert isinstance(sub.rungs[0].ops[0], ContactNO)


# -----------------------------------------------------------------------------
# Negated edge contacts: ``<contact edge="rising" negated="true">`` /
# ``<contact edge="falling" negated="true">``.  Previously the
# reader stripped the negation (lossy case documented in PR #36);
# now ContactRisingEdge / ContactFallingEdge carry a ``negated``
# flag and round-trip the combination losslessly.
# -----------------------------------------------------------------------------


def test_negated_rising_edge_round_trips_via_dataclass_flag():
    """``ContactRisingEdge(addr, negated=True)`` -> XML contact
    with both ``negated="true"`` and ``edge="rising"``; reader
    recovers both flags into the same IL form."""
    from universal_machinery.il.ops import ContactRisingEdge
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(ContactRisingEdge(Address("clk"), negated=True), coil("out")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, ContactRisingEdge)
    assert op.negated is True


def test_negated_falling_edge_round_trips_via_dataclass_flag():
    from universal_machinery.il.ops import ContactFallingEdge
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(ContactFallingEdge(Address("clk"), negated=True), coil("out")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, ContactFallingEdge)
    assert op.negated is True


def test_negated_edge_contact_emit_carries_both_attrs():
    from universal_machinery.il.ops import ContactRisingEdge
    from universal_machinery.il import Address
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ContactRisingEdge(Address("clk"), negated=True), coil("out")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Both attrs must appear on the same <contact> element
    contact_elem = xml.split("<contact ")[1].split(">")[0]
    assert 'negated="true"' in contact_elem
    assert 'edge="rising"' in contact_elem


def test_plain_edge_contact_still_emits_without_negated_attribute():
    """Negative baseline: a non-negated edge contact (the
    common case) doesn't pick up a stray ``negated="true"``."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(redge("X"), coil("Y")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    contact_elem = xml.split("<contact ")[1].split(">")[0]
    assert 'negated="true"' not in contact_elem


def test_reader_recovers_negated_edge_from_hand_rolled_xml():
    """A document that carries ``negated="true" edge="rising"``
    on a ``<contact>`` element now round-trips into
    ``ContactRisingEdge(..., negated=True)`` rather than losing
    the negation."""
    from universal_machinery.il.ops import ContactRisingEdge
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <contact localId="1" negated="true" edge="rising">
          <position x="100" y="0"/>
          <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
          <connectionPointOut/>
          <variable>flag</variable>
        </contact>
        <coil localId="2">
          <position x="200" y="0"/>
          <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
          <connectionPointOut/>
          <variable>out</variable>
        </coil>
        <rightPowerRail localId="3">
          <position x="300" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    op = sub.rungs[0].ops[0]
    assert isinstance(op, ContactRisingEdge)
    assert op.negated is True


# -----------------------------------------------------------------------------
# Compare ops in LD (IEC §2.5.2.8): the comparison family lowers
# to ``<block typeName="GT|GE|EQ|LE|LT|NE">`` embedded in the LD
# body, with two ``<inVariable>`` operand sources.  The block's
# OUT pin replaces a contact's output in the rung gate chain.
# Previously any rung containing a Compare dropped to ST text.
# -----------------------------------------------------------------------------


def test_compare_rung_emits_native_LD_not_ST_fallback():
    """A rung with a Compare op stays in ``<LD>`` rather than
    dropping to the legacy ST-text fallback emit path."""
    from universal_machinery.il.ops import Compare
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(Compare(op=">", lhs="X001", rhs="50"), coil("Y001")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_compare_emits_block_typeName_and_in_variables():
    from universal_machinery.il.ops import Compare
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(Compare(op=">", lhs="X001", rhs="50"), coil("Y001")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="GT"' in xml
    # Two <inVariable> producers, one each for the lhs/rhs operands
    assert xml.count("<inVariable") == 2
    assert "<expression>X001</expression>" in xml
    assert "<expression>50</expression>" in xml


@pytest.mark.parametrize("symbol,block_name", [
    ("==", "EQ"),
    ("!=", "NE"),
    ("<",  "LT"),
    ("<=", "LE"),
    (">",  "GT"),
    (">=", "GE"),
])
def test_each_compare_op_maps_to_iec_block_typename(symbol, block_name):
    """All six IEC §2.5.2.8 comparison ops emit with the right
    ``<block typeName=...>``."""
    from universal_machinery.il.ops import Compare
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(Compare(op=symbol, lhs="a", rhs="b"), coil("Y")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert f'typeName="{block_name}"' in xml


def test_compare_round_trips_through_LD():
    """Emit a Compare-gated rung, parse it back, and verify the
    IL ``Compare`` op is recovered with its symbol and operands."""
    from universal_machinery.il.ops import Compare
    rungs = _round_trip([
        rung(Compare(op=">", lhs="X001", rhs="50"), coil("Y001")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], Compare)
    assert ops[0].op == ">"
    # lhs should round-trip as an Address (X001 is CLICK-style)
    assert isinstance(ops[0].lhs, Address)
    assert ops[0].lhs.raw == "X001"
    # rhs is a numeric literal, stays as raw text
    assert ops[0].rhs == "50"
    assert isinstance(ops[1], OutCoil)


@pytest.mark.parametrize("symbol", ["==", "!=", "<", "<=", ">", ">="])
def test_every_compare_op_round_trips(symbol):
    from universal_machinery.il.ops import Compare
    rungs = _round_trip([
        rung(Compare(op=symbol, lhs="a", rhs="b"), coil("Y")),
    ])
    assert isinstance(rungs[0].ops[0], Compare)
    assert rungs[0].ops[0].op == symbol


def test_compare_block_wires_EN_to_leftRail():
    """The Compare block consumes the rung's boolean signal via
    its ``EN`` input.  Pinning so the forward-walk LD reader can
    discover the block by traversing leftPowerRail's consumers."""
    from universal_machinery.il.ops import Compare
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(Compare(op=">", lhs="a", rhs="b"), coil("Y")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    # The block's EN formal parameter connection wires back to
    # leftPowerRail localId=0.
    import re
    block_m = re.search(r"<block [^>]*typeName=\"GT\".*?</block>",
                         xml, re.S)
    assert block_m is not None
    en_m = re.search(
        r'<variable formalParameter="EN">\s*'
        r'<connectionPointIn>\s*<connection refLocalId="0"/>',
        block_m.group(0),
    )
    assert en_m is not None


def test_reader_recovers_compare_from_hand_rolled_GT_block():
    """A document with a hand-rolled ``<block typeName="GT">`` in
    an LD body recovers as an IL Compare op even when the emit
    side wasn't ours."""
    from universal_machinery.il.ops import Compare
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <inVariable localId="1">
          <position x="100" y="0"/>
          <connectionPointOut/>
          <expression>a</expression>
        </inVariable>
        <inVariable localId="2">
          <position x="100" y="40"/>
          <connectionPointOut/>
          <expression>b</expression>
        </inVariable>
        <block localId="3" typeName="LT">
          <position x="200" y="0"/>
          <inputVariables>
            <variable formalParameter="EN">
              <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
            </variable>
            <variable formalParameter="IN1">
              <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
            </variable>
            <variable formalParameter="IN2">
              <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
            </variable>
          </inputVariables>
          <inOutVariables/>
          <outputVariables>
            <variable formalParameter="OUT"><connectionPointOut/></variable>
          </outputVariables>
        </block>
        <coil localId="4">
          <position x="300" y="0"/>
          <connectionPointIn><connection refLocalId="3"/></connectionPointIn>
          <connectionPointOut/>
          <variable>Y</variable>
        </coil>
        <rightPowerRail localId="5">
          <position x="400" y="0"/>
          <connectionPointIn><connection refLocalId="4"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    assert isinstance(sub.rungs[0].ops[0], Compare)
    assert sub.rungs[0].ops[0].op == "<"


# -----------------------------------------------------------------------------
# Move op in LD (IEC §2.5.2.1): lowers to ``<block typeName="MOVE">``
# with an ``<inVariable>`` source and ``<outVariable>`` destination.
# Previously a Move-containing rung dropped to ST-text fallback.
# -----------------------------------------------------------------------------


def test_move_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.il.ops import Move
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X001"), Move(src="42", dst="DS5")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_move_emits_block_typeName_MOVE_with_in_and_out_variables():
    from universal_machinery.il.ops import Move
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X001"), Move(src="42", dst="DS5")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="MOVE"' in xml
    assert "<inVariable" in xml
    assert "<outVariable" in xml
    assert "<expression>42</expression>" in xml
    assert "<expression>DS5</expression>" in xml


def test_move_round_trips_with_literal_src_and_address_dst():
    from universal_machinery.il.ops import Move
    rungs = _round_trip([
        rung(no("X001"), Move(src="42", dst="DS5")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], Move)
    assert ops[1].src == "42"
    assert isinstance(ops[1].dst, Address)
    assert ops[1].dst.raw == "DS5"


def test_move_round_trips_with_address_src_and_address_dst():
    from universal_machinery.il.ops import Move
    rungs = _round_trip([
        rung(Move(src="X001", dst="Y001")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], Move)
    assert isinstance(ops[0].src, Address)
    assert ops[0].src.raw == "X001"
    assert isinstance(ops[0].dst, Address)
    assert ops[0].dst.raw == "Y001"


def test_move_block_wires_EN_to_upstream_contact():
    """The Move block's EN input ties back to the upstream contact
    (not directly to leftRail) so the rung's boolean gate keeps
    flowing through it."""
    from universal_machinery.il.ops import Move
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("enable"), Move(src="src_v", dst="dst_v")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    # The block at the end has EN refLocalId pointing at the
    # contact (localId=1), not leftRail (localId=0).
    import re
    block_m = re.search(
        r"<block [^>]*typeName=\"MOVE\".*?</block>", xml, re.S,
    )
    assert block_m is not None
    en_m = re.search(
        r'<variable formalParameter="EN">\s*'
        r'<connectionPointIn>\s*<connection refLocalId="1"/>',
        block_m.group(0),
    )
    assert en_m is not None


def test_reader_recovers_move_from_hand_rolled_block():
    """A document with a hand-rolled ``<block typeName="MOVE">``
    + inVariable / outVariable recovers as an IL Move op."""
    from universal_machinery.il.ops import Move
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <inVariable localId="1">
          <position x="100" y="0"/>
          <connectionPointOut/>
          <expression>source_var</expression>
        </inVariable>
        <block localId="2" typeName="MOVE">
          <position x="200" y="0"/>
          <inputVariables>
            <variable formalParameter="EN">
              <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
            </variable>
            <variable formalParameter="IN">
              <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
            </variable>
          </inputVariables>
          <inOutVariables/>
          <outputVariables>
            <variable formalParameter="ENO"><connectionPointOut/></variable>
            <variable formalParameter="OUT"><connectionPointOut/></variable>
          </outputVariables>
        </block>
        <outVariable localId="3">
          <position x="300" y="0"/>
          <connectionPointIn>
            <connection refLocalId="2" formalParameter="OUT"/>
          </connectionPointIn>
          <expression>dest_var</expression>
        </outVariable>
        <rightPowerRail localId="4">
          <position x="400" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    ops = sub.rungs[0].ops
    assert isinstance(ops[0], Move)
    from universal_machinery.il import TagRef
    assert ops[0].src == TagRef("source_var")
    assert ops[0].dst == TagRef("dest_var")


def test_compare_and_move_in_same_rung_round_trip():
    """A rung mixing a Compare gate + a Move action -- both
    native LD block ops -- round-trips with both ops preserved."""
    from universal_machinery.il.ops import Compare, Move
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(Compare(op=">", lhs="speed", rhs="0"),
             Move(src="speed", dst="last_speed")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], Compare)
    assert ops[0].op == ">"
    assert isinstance(ops[1], Move)
    assert ops[1].dst == TagRef("last_speed")


# -----------------------------------------------------------------------------
# BinaryMath in LD (IEC §2.5.2.5): ``+``/``-``/``*``/``/``/``%``
# lower to ``<block typeName="ADD|SUB|MUL|DIV|MOD">`` with two
# ``<inVariable>`` operand sources and an ``<outVariable>``
# destination.  Previously any BinaryMath-containing rung
# dropped to ST-text fallback (``r := a + b;``).
# -----------------------------------------------------------------------------


def test_binary_math_rung_emits_native_LD_not_ST_fallback():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(add("DS1", "DS2", "DS3")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_binary_math_emits_block_typeName_ADD_and_three_operand_elements():
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(add("DS1", "DS2", "DS3")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="ADD"' in xml
    assert xml.count("<inVariable") == 2
    assert "<outVariable" in xml
    assert "<expression>DS1</expression>" in xml
    assert "<expression>DS2</expression>" in xml
    assert "<expression>DS3</expression>" in xml


@pytest.mark.parametrize("symbol,block_name", [
    ("+", "ADD"),
    ("-", "SUB"),
    ("*", "MUL"),
    ("/", "DIV"),
    ("%", "MOD"),
])
def test_each_binary_math_op_maps_to_iec_block_typename(symbol, block_name):
    from universal_machinery.il.ops import BinaryMath
    from universal_machinery.il import Address
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(BinaryMath(op=symbol, lhs=Address("DS1"),
                          rhs=Address("DS2"), dst=Address("DS3"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert f'typeName="{block_name}"' in xml


@pytest.mark.parametrize("symbol", ["+", "-", "*", "/", "%"])
def test_every_binary_math_op_round_trips(symbol):
    from universal_machinery.il.ops import BinaryMath
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(BinaryMath(op=symbol, lhs=Address("DS1"),
                          rhs=Address("DS2"), dst=Address("DS3"))),
    ])
    assert isinstance(rungs[0].ops[0], BinaryMath)
    assert rungs[0].ops[0].op == symbol


def test_binary_math_round_trips_operands_with_address_and_literal():
    from universal_machinery.il.ops import BinaryMath
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(BinaryMath(op="+", lhs=Address("DS1"),
                          rhs="100", dst=Address("DS2"))),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, BinaryMath)
    assert op.op == "+"
    assert op.lhs == Address("DS1")
    assert op.rhs == "100"
    assert op.dst == Address("DS2")


def test_binary_math_gated_by_contact_round_trips():
    """A rung with a contact gate followed by BinaryMath: the
    block's EN wires through the contact, ENO continues the
    gate so downstream chaining would still work."""
    from universal_machinery.il.ops import BinaryMath
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(no("enable"),
              BinaryMath(op="*", lhs=Address("speed"),
                          rhs="60", dst=Address("rpm"))),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], BinaryMath)
    assert ops[1].op == "*"


def test_reader_recovers_binary_math_from_hand_rolled_SUB_block():
    from universal_machinery.il.ops import BinaryMath
    from universal_machinery.il import Address
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <inVariable localId="1">
          <position x="100" y="0"/>
          <connectionPointOut/>
          <expression>DS10</expression>
        </inVariable>
        <inVariable localId="2">
          <position x="100" y="40"/>
          <connectionPointOut/>
          <expression>DS11</expression>
        </inVariable>
        <block localId="3" typeName="SUB">
          <position x="200" y="0"/>
          <inputVariables>
            <variable formalParameter="EN">
              <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
            </variable>
            <variable formalParameter="IN1">
              <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
            </variable>
            <variable formalParameter="IN2">
              <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
            </variable>
          </inputVariables>
          <inOutVariables/>
          <outputVariables>
            <variable formalParameter="ENO"><connectionPointOut/></variable>
            <variable formalParameter="OUT"><connectionPointOut/></variable>
          </outputVariables>
        </block>
        <outVariable localId="4">
          <position x="300" y="0"/>
          <connectionPointIn>
            <connection refLocalId="3" formalParameter="OUT"/>
          </connectionPointIn>
          <expression>DS12</expression>
        </outVariable>
        <rightPowerRail localId="5">
          <position x="400" y="0"/>
          <connectionPointIn><connection refLocalId="3"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    op = sub.rungs[0].ops[0]
    assert isinstance(op, BinaryMath)
    assert op.op == "-"
    assert op.lhs == Address("DS10")
    assert op.rhs == Address("DS11")
    assert op.dst == Address("DS12")


# -----------------------------------------------------------------------------
# ParallelGroup (OR branches inside a rung).  Previously any rung
# carrying a ParallelGroup dropped to ST-text fallback; native LD
# now emits the branches with multi-incoming wires at the join.
# -----------------------------------------------------------------------------


def _pg(*branches):
    return ParallelGroup(branches=tuple(tuple(b) for b in branches))


def test_parallel_group_rung_emits_native_LD_not_ST_fallback():
    """A rung with a ParallelGroup should now stay in
    ``<LD>...</LD>`` -- the ST-text fallback was the previous
    behaviour."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("A"),
             _pg([no("B")], [no("C")]),
             coil("D")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_parallel_group_emits_multi_incoming_wire_at_join():
    """The first op *after* a ParallelGroup carries every branch
    tail in its ``<connectionPointIn>``.  For
    ``A AND (B OR C) -> coil``, the coil's incoming has two
    ``<connection>`` children, one per branch tail."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("A"),
             _pg([no("B")], [no("C")]),
             coil("D")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Two contacts B & C, both children of A; the coil joins them.
    # The coil's connectionPointIn has 2 connections.
    import re
    coil_match = re.search(
        r'<coil[^>]*>.*?<connectionPointIn>(.*?)</connectionPointIn>',
        xml, re.S,
    )
    assert coil_match is not None
    assert coil_match.group(1).count("<connection ") == 2


def test_parallel_group_two_branch_round_trips():
    rungs = _round_trip([
        rung(no("A"),
             _pg([no("B")], [no("C")]),
             coil("D")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], ParallelGroup)
    assert len(ops[1].branches) == 2
    # Branch contents preserved (single ContactNO each)
    branch_contacts = [b[0] for b in ops[1].branches]
    assert all(isinstance(c, ContactNO) for c in branch_contacts)
    assert {c.address.name for c in branch_contacts} == {"B", "C"}
    assert isinstance(ops[2], OutCoil)


def test_parallel_group_three_branch_round_trips():
    rungs = _round_trip([
        rung(_pg([no("A")], [no("B")], [no("C")]),
             coil("out")),
    ])
    ops = rungs[0].ops
    pg = ops[0]
    assert isinstance(pg, ParallelGroup)
    assert len(pg.branches) == 3
    branch_names = {b[0].address.name for b in pg.branches}
    assert branch_names == {"A", "B", "C"}


def test_parallel_group_branch_with_multiple_contacts_round_trips():
    """A branch can carry more than one contact (a sub-AND chain
    inside an OR alternative).  ``A AND ((B AND C) OR D) -> out``."""
    rungs = _round_trip([
        rung(no("A"),
             _pg([no("B"), no("C")], [no("D")]),
             coil("out")),
    ])
    ops = rungs[0].ops
    assert isinstance(ops[1], ParallelGroup)
    branches = ops[1].branches
    # Find the 2-op branch
    by_len = {len(b): b for b in branches}
    assert set(by_len) == {1, 2}
    two_op = by_len[2]
    assert [c.address.name for c in two_op] == ["B", "C"]
    one_op = by_len[1]
    assert one_op[0].address.name == "D"


def test_parallel_group_with_mixed_contact_kinds_in_branches():
    """Branches can mix NO / NC contacts."""
    rungs = _round_trip([
        rung(_pg([no("a")], [nc("b")]),
             coil("out")),
    ])
    pg = rungs[0].ops[0]
    assert isinstance(pg, ParallelGroup)
    kinds = {type(b[0]).__name__ for b in pg.branches}
    assert kinds == {"ContactNO", "ContactNC"}


def test_parallel_group_xsd_validates():
    """All ParallelGroup shapes should XSD-validate (the XSD
    only requires multi-incoming at the join; nothing more)."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(_pg([no("a")], [no("b")], [no("c")]), coil("out")),
        rung(no("x"),
             _pg([no("y")], [no("z")]),
             coil("done")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_parallel_group_localIds_remain_unique():
    """Across multiple rungs containing ParallelGroups, every
    localId in the body should still be unique."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(_pg([no("a")], [no("b")]), coil("out1")),
        rung(_pg([no("c")], [no("d")]), coil("out2")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    import re
    ids = re.findall(r'localId="(\d+)"', xml)
    assert len(ids) == len(set(ids)), "duplicate localId found"


# ---- reader-only: hand-rolled documents -------------------------------------


def test_reader_recovers_parallel_group_from_hand_rolled_multi_incoming():
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body><LD>
        <leftPowerRail localId="0">
          <position x="0" y="0"/>
          <connectionPointOut formalParameter="OUT"/>
        </leftPowerRail>
        <contact localId="1">
          <position x="100" y="0"/>
          <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
          <connectionPointOut/>
          <variable>start</variable>
        </contact>
        <contact localId="2">
          <position x="200" y="0"/>
          <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
          <connectionPointOut/>
          <variable>p</variable>
        </contact>
        <contact localId="3">
          <position x="200" y="40"/>
          <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
          <connectionPointOut/>
          <variable>q</variable>
        </contact>
        <coil localId="4">
          <position x="300" y="0"/>
          <connectionPointIn>
            <connection refLocalId="2"/>
            <connection refLocalId="3"/>
          </connectionPointIn>
          <connectionPointOut/>
          <variable>done</variable>
        </coil>
        <rightPowerRail localId="5">
          <position x="400" y="0"/>
          <connectionPointIn><connection refLocalId="4"/></connectionPointIn>
        </rightPowerRail>
      </LD></body>
    </pou>
  </pous></types>
</project>'''
    sub = parse_plcopen_xml(xml).find_subroutine("Main")
    ops = sub.rungs[0].ops
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], ParallelGroup)
    branch_names = {b[0].address.name for b in ops[1].branches}
    assert branch_names == {"p", "q"}
    assert isinstance(ops[2], OutCoil)


# -----------------------------------------------------------------------------
# StdFunc in LD (IEC §2.5.2 standard-library function calls):
# every IEC stdlib function (ABS / SQRT / AND / OR / SEL / LIMIT /
# MUX / SHL / SHR / ROR / ROL / sin / cos / type-conversions / etc.)
# now lowers to a ``<block typeName=NAME>`` with variable IN /
# IN1..INn pin wiring + ``<outVariable>`` for the output.
# -----------------------------------------------------------------------------


def test_stdfunc_one_input_round_trips_via_IN_pin():
    """A single-input StdFunc (ABS) uses the unindexed ``IN``
    pin per IEC convention; round-trip preserves name + arg."""
    from universal_machinery.builders import abs_
    from universal_machinery.il.ops import StdFunc
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(abs_(tag("a"), tag("r"))),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, StdFunc)
    assert op.name == "ABS"
    assert op.inputs == (TagRef("a"),)
    assert op.output == TagRef("r")


def test_stdfunc_two_input_round_trips_via_IN1_IN2_pins():
    from universal_machinery.builders import and_
    from universal_machinery.il.ops import StdFunc
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(and_(tag("a"), tag("b"), tag("r"))),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, StdFunc)
    assert op.name == "AND"
    assert op.inputs == (TagRef("a"), TagRef("b"))


def test_stdfunc_three_input_round_trips_via_IN1_IN2_IN3():
    from universal_machinery.builders import sel
    from universal_machinery.il.ops import StdFunc
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(sel(tag("c"), tag("lo"), tag("hi"), tag("out"))),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, StdFunc)
    assert op.name == "SEL"
    assert op.inputs == (TagRef("c"), TagRef("lo"), TagRef("hi"))
    assert op.output == TagRef("out")


def test_stdfunc_emits_block_with_function_name_as_typeName():
    from universal_machinery.builders import abs_
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(abs_(tag("a"), tag("r"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="ABS"' in xml


def test_stdfunc_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.builders import abs_
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(abs_(tag("a"), tag("r"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_stdfunc_two_input_uses_IN1_IN2_pin_naming_in_xml():
    from universal_machinery.builders import and_
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(and_(tag("a"), tag("b"), tag("r"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    assert 'formalParameter="IN1"' in xml
    assert 'formalParameter="IN2"' in xml


def test_stdfunc_one_input_uses_IN_pin_naming_in_xml():
    from universal_machinery.builders import abs_
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(abs_(tag("a"), tag("r"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    # Single-input form uses "IN" (no index) per IEC convention
    assert 'formalParameter="IN"' in xml
    # And NOT IN1
    assert 'formalParameter="IN1"' not in xml


# -----------------------------------------------------------------------------
# Call op in LD: invokes a user POU (FUNCTION / FUNCTION_BLOCK /
# SUBROUTINE) via ``<block typeName=<target>>`` with named-parameter
# binding via formalParameter.  This closes the mixed LD+FBD-block
# series -- after this slice, only the stateful FB family
# (TON / TOF / CTU / RTrig / ...) still falls back to ST text.
# -----------------------------------------------------------------------------


def test_unparameterised_subroutine_call_round_trips():
    from universal_machinery.builders import call
    from universal_machinery.il.ops import Call
    rungs = _round_trip([rung(call("Sub1"))])
    op = rungs[0].ops[0]
    assert isinstance(op, Call)
    assert op.target == "Sub1"
    assert op.inputs == ()
    assert op.outputs == ()
    assert op.return_to is None


def test_function_call_with_return_to_round_trips():
    from universal_machinery.builders import call
    from universal_machinery.il.ops import Call
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(call("Average",
                    inputs=[("a", tag("x")), ("b", tag("y"))],
                    return_to=tag("avg"))),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, Call)
    assert op.target == "Average"
    assert op.inputs == (
        ("a", TagRef("x")), ("b", TagRef("y")),
    )
    assert op.return_to == TagRef("avg")


def test_function_block_call_with_outputs_and_instance_round_trips():
    from universal_machinery.builders import call
    from universal_machinery.il.ops import Call
    from universal_machinery.il import Address, TagRef
    rungs = _round_trip([
        rung(call("PID",
                    instance=Address("DB7"),
                    inputs=[("SP", tag("setpoint")),
                              ("PV", tag("process"))],
                    outputs=[("OUT", tag("output"))])),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, Call)
    assert op.target == "PID"
    assert op.instance == Address("DB7")
    assert op.inputs == (
        ("SP", TagRef("setpoint")),
        ("PV", TagRef("process")),
    )
    assert op.outputs == (("OUT", TagRef("output")),)


def test_call_block_uses_target_as_typeName():
    from universal_machinery.builders import call
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(call("Average",
                    inputs=[("a", tag("x"))],
                    return_to=tag("avg"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="Average"' in xml


def test_call_fb_emits_instanceName_attribute():
    from universal_machinery.builders import call
    from universal_machinery.il import Address
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(call("PID", instance=Address("DB7"),
                    outputs=[("OUT", tag("output"))])),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'instanceName="DB7"' in xml


def test_call_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.builders import call
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(call("OtherPou")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_function_return_pin_uses_target_name_as_formalParameter():
    """Per IEC convention the function's return-value pin
    carries the function's own name as its formalParameter."""
    from universal_machinery.builders import call
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(call("Average",
                    inputs=[("a", tag("x"))],
                    return_to=tag("avg"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    import re
    # The function-return outVariable's connection refs the
    # block with formalParameter="Average".
    assert re.search(
        r'<connection refLocalId="\d+" formalParameter="Average"/>',
        xml,
    ) is not None


# -----------------------------------------------------------------------------
# IEC §2.5.2.3.1 timer FBs (TON / TOF / TP) in LD: each lowers to
# ``<block typeName=TON|TOF|TP instanceName=<addr>>`` with IN <-
# rung gate, PT <- inVariable carrying the time literal, and
# Q + ET output pins each consumed (when set) by an outVariable.
# -----------------------------------------------------------------------------


def test_ton_round_trips_with_done_bit_and_accumulator():
    from universal_machinery.builders import ton
    from universal_machinery.il.ops import TON
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(ton("T1", 1000, accumulator="ET1", done_bit="Q1")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, TON)
    assert op.address == Address("T1")
    assert op.preset_ms == 1000
    assert op.accumulator == Address("ET1")
    assert op.done_bit == Address("Q1")


def test_tof_round_trips_with_done_bit_only():
    from universal_machinery.builders import tof
    from universal_machinery.il.ops import TOF
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(tof("T2", 500, done_bit="Q2")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, TOF)
    assert op.preset_ms == 500
    assert op.done_bit == Address("Q2")
    assert op.accumulator is None


def test_tp_round_trips_with_no_outputs_set():
    from universal_machinery.builders import tp
    from universal_machinery.il.ops import TP
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(tp("T3", 250)),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, TP)
    assert op.address == Address("T3")
    assert op.preset_ms == 250
    assert op.done_bit is None
    assert op.accumulator is None


def test_timer_block_emits_typeName_TON_with_instanceName():
    from universal_machinery.builders import ton
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ton("T1", 1000)),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="TON"' in xml
    assert 'instanceName="T1"' in xml


def test_timer_block_emits_PT_as_iec_time_literal():
    """The PT preset_ms is rendered as ``T#<ms>ms`` in the
    inVariable's expression text per IEC §2.5.2.10 convention."""
    from universal_machinery.builders import ton
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ton("T1", 1500)),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<expression>T#1500ms</expression>" in xml


def test_timer_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.builders import ton
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ton("T1", 100)),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_timer_round_trip_preserves_iec_time_through_multiple_durations():
    """A range of preset durations (ms / s / mixed) round-trips
    through the T# literal correctly via ``_parse_duration_ms``."""
    from universal_machinery.builders import ton
    from universal_machinery.il.ops import TON
    for ms in (10, 1500, 60_000, 3_661_000):
        rungs = _round_trip([rung(ton("T", ms))])
        op = rungs[0].ops[0]
        assert isinstance(op, TON)
        assert op.preset_ms == ms


# -----------------------------------------------------------------------------
# IEC §2.5.2.3.2 counter FBs (CTU / CTD / CTUD) in LD: each
# lowers to ``<block typeName=CTU|CTD|CTUD instanceName=<addr>>``
# with CU/CD/R/LD/PV inputs and Q/QU/QD/CV outputs as appropriate
# for the counter variant.
# -----------------------------------------------------------------------------


def test_ctu_round_trips_with_reset_and_outputs():
    from universal_machinery.builders import ctu
    from universal_machinery.il.ops import CTU
    from universal_machinery.il import Address, TagRef
    rungs = _round_trip([
        rung(ctu("C1", 10, reset="Rst1",
                  accumulator="CV1", done_bit="Q1")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, CTU)
    assert op.address == Address("C1")
    assert op.preset == 10
    # 'Rst1' coerces to TagRef (mixed-case word, not CLICK addr)
    assert op.reset == TagRef("Rst1")
    assert op.accumulator == Address("CV1")
    assert op.done_bit == Address("Q1")


def test_ctd_round_trips_with_load_and_outputs():
    from universal_machinery.builders import ctd
    from universal_machinery.il.ops import CTD
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(ctd("C2", 100, load="LD2",
                  accumulator="CV2", done_bit="Q2")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, CTD)
    assert op.preset == 100
    assert op.load == Address("LD2")
    assert op.done_bit == Address("Q2")


def test_ctud_round_trips_with_all_inputs_and_outputs():
    from universal_machinery.builders import ctud
    from universal_machinery.il.ops import CTUD
    from universal_machinery.il import Address
    rungs = _round_trip([
        rung(ctud("C3", 50,
                    cu_input="UP3", cd_input="DN3",
                    reset="R3", load="L3",
                    accumulator="CV3",
                    qu="QU3", qd="QD3")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, CTUD)
    assert op.preset == 50
    assert op.cu_input == Address("UP3")
    assert op.cd_input == Address("DN3")
    assert op.reset == Address("R3")
    assert op.load == Address("L3")
    assert op.accumulator == Address("CV3")
    assert op.qu == Address("QU3")
    assert op.qd == Address("QD3")


def test_counter_block_emits_typeName_and_instanceName():
    from universal_machinery.builders import ctu
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ctu("MyCounter", 5)),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="CTU"' in xml
    assert 'instanceName="MyCounter"' in xml


def test_counter_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.builders import ctu
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ctu("C1", 10)),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


def test_ctud_emits_separate_CU_CD_inVariable_sources():
    """CTUD takes both count-up (CU) and count-down (CD) as
    auxiliary bool inputs from inVariables -- not from the rung
    gate.  Pinning the shape so the LD reader's parallel-fork
    detector doesn't trip on multi-pin wiring."""
    from universal_machinery.builders import ctud
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(ctud("C3", 50, cu_input="up", cd_input="dn")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Both CU and CD have their own inVariable
    assert "<expression>up</expression>" in xml
    assert "<expression>dn</expression>" in xml


# -----------------------------------------------------------------------------
# IEC §2.5.2.3.3 bistables (SR / RS) + edge triggers (R_TRIG /
# F_TRIG) in LD: each lowers to ``<block typeName=...>`` with
# named bool inputs from auxiliary inVariables.  Bistables' Q1
# output is the instance name (one storage serves both roles).
# -----------------------------------------------------------------------------


def test_sr_round_trips_with_S1_R_and_Q1_output():
    from universal_machinery.builders import sr
    from universal_machinery.il.ops import SR
    from universal_machinery.il import Address
    rungs = _round_trip([rung(sr("Q1", "S1", "R1"))])
    op = rungs[0].ops[0]
    assert isinstance(op, SR)
    assert op.q1 == Address("Q1")
    assert op.s1 == Address("S1")
    assert op.r == Address("R1")


def test_rs_round_trips_with_R1_S_and_Q1_output():
    from universal_machinery.builders import rs
    from universal_machinery.il.ops import RS
    from universal_machinery.il import Address
    rungs = _round_trip([rung(rs("Q2", "R2", "S2"))])
    op = rungs[0].ops[0]
    assert isinstance(op, RS)
    assert op.q1 == Address("Q2")
    assert op.r1 == Address("R2")
    assert op.s == Address("S2")


def test_r_trig_round_trips_with_state_clk_and_q():
    from universal_machinery.builders import r_trig
    from universal_machinery.il.ops import RTrig
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(r_trig("PrevCLK", "CLK", "EdgeQ")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, RTrig)
    assert op.state == TagRef("PrevCLK")
    assert op.clk == TagRef("CLK")
    assert op.q == TagRef("EdgeQ")


def test_f_trig_round_trips_with_state_clk_and_q():
    from universal_machinery.builders import f_trig
    from universal_machinery.il.ops import FTrig
    from universal_machinery.il import TagRef
    rungs = _round_trip([
        rung(f_trig("PrevCLK", "CLK", "EdgeQ")),
    ])
    op = rungs[0].ops[0]
    assert isinstance(op, FTrig)


def test_sr_emits_block_with_instanceName_eq_q1():
    """Per IEC §2.5.2.3.3 the SR FB's Q1 storage IS its instance
    name -- the block's ``instanceName`` attribute carries Q1's
    address."""
    from universal_machinery.builders import sr
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(sr("MotorOn", "StartBtn", "StopBtn")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="SR"' in xml
    assert 'instanceName="MotorOn"' in xml


def test_r_trig_emits_block_with_instanceName_eq_state():
    from universal_machinery.builders import r_trig
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(r_trig("PrevState", "CLK", "Q")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="R_TRIG"' in xml
    assert 'instanceName="PrevState"' in xml


def test_bistable_or_edge_trigger_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.builders import sr, r_trig
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(sr("Q1", "S1", "R1")),
        rung(r_trig("Prev", "CLK", "Q")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml


# -----------------------------------------------------------------------------
# Control-flow ops in LD: Jump / Label / Return lower to dedicated
# ``<jump label=...>`` / ``<label label=...>`` / ``<return>`` XSD
# elements (commonObjects group).  ``End`` stays in ST fallback
# (no native XSD shape for "end of main program").
# -----------------------------------------------------------------------------


def test_jump_emits_native_jump_element():
    from universal_machinery.builders import jump
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X001"), jump("END_OF_SCAN")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<jump localId=' in xml
    assert 'label="END_OF_SCAN"' in xml


def test_label_emits_native_label_element():
    from universal_machinery.builders import label_
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(label_("MY_TARGET")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<label localId=' in xml
    assert 'label="MY_TARGET"' in xml


def test_return_emits_native_return_element():
    from universal_machinery.builders import ret
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("done_flag"), ret()),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<return localId=' in xml


def test_jump_round_trips_with_target_label():
    from universal_machinery.builders import jump
    from universal_machinery.il.ops import Jump
    rungs = _round_trip([rung(no("X001"), jump("TARGET"))])
    ops = rungs[0].ops
    assert isinstance(ops[0], ContactNO)
    assert isinstance(ops[1], Jump)
    assert ops[1].label == "TARGET"


def test_label_round_trips_with_name():
    from universal_machinery.builders import label_
    from universal_machinery.il.ops import Label
    rungs = _round_trip([rung(label_("TARGET"))])
    assert isinstance(rungs[0].ops[0], Label)
    assert rungs[0].ops[0].name == "TARGET"


def test_return_round_trips():
    from universal_machinery.builders import ret
    from universal_machinery.il.ops import Return
    rungs = _round_trip([rung(no("flag"), ret())])
    assert isinstance(rungs[0].ops[1], Return)


def test_jump_label_return_in_same_body_round_trips():
    """A POU with Jump (to a Label) + a Return early-exit
    round-trips with all three control-flow ops preserved.
    Note: Label rungs end up at the bottom of the rung list
    after round-trip (the reader's orphan-element pass picks
    them up after the leftRail forward walk)."""
    from universal_machinery.builders import jump, label_, ret
    from universal_machinery.il.ops import Jump, Label, Return
    rungs = _round_trip([
        rung(no("skip_flag"), jump("AFTER")),
        rung(no("X001"), coil("Y001")),
        rung(label_("AFTER")),
        rung(no("done_flag"), ret()),
    ])
    # Collect the op types per rung
    by_type = [type(op).__name__ for r in rungs for op in r.ops]
    assert "Jump" in by_type
    assert "Label" in by_type
    assert "Return" in by_type
    assert "OutCoil" in by_type
    # The Jump's target survives
    j = next(op for r in rungs for op in r.ops if isinstance(op, Jump))
    assert j.label == "AFTER"
    # The Label's name survives
    l = next(op for r in rungs for op in r.ops if isinstance(op, Label))
    assert l.name == "AFTER"


def test_jump_rung_emits_native_LD_not_ST_fallback():
    from universal_machinery.builders import jump
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(jump("TARGET")),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<LD>" in xml
    assert "<ST>" not in xml
