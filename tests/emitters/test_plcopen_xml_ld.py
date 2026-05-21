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
    OutCoil, OutReset, OutSet,
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
    """A rung containing a non-LD op (math) keeps going through
    ST text emission."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(add(tag("a"), tag("b"), tag("r"))),
    ])])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Not LD body
    assert "<LD>" not in xml
    # ST text body present
    assert "r := a + b;" in xml


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
