"""PLCopen XML emission + reader for SFC bodies (IEC §2.6).

Both directions land in one slice so the round-trip is verified
in the same test file.  Each test:

  - Builds an SfcNetwork in IL
  - emit_xml + validate against the bundled PLCopen TC6 v2.01 XSD
  - parse_plcopen_xml back
  - asserts the parsed shape matches the source
"""
from datetime import datetime, timezone

import pytest

xmlschema = pytest.importorskip("xmlschema")

from universal_machinery.builders import prog, program
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)
from universal_machinery.il import (
    Address, SfcNetwork, Step, TagType, Transition,
)
from universal_machinery.il.ops import ContactNO, ContactNC
from universal_machinery.parsers.plcopen_xml import parse_plcopen_xml


_FIXED_TIME = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


def _round_trip(net: SfcNetwork) -> SfcNetwork:
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    p2 = parse_plcopen_xml(xml)
    return p2.find_subroutine("Main").sfc


# -----------------------------------------------------------------------------
# Emission shape + XSD validation
# -----------------------------------------------------------------------------


def test_empty_sfc_body_emits_self_closing_and_validates():
    p = program(subroutines=[prog("Main", main=True,
                                    sfc=SfcNetwork())])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<SFC" in xml


def test_sfc_body_picked_over_st_when_sfc_set():
    """When ``sub.sfc`` is set the XML emits ``<SFC>`` natively,
    not the previous ``<ST>`` marker comment."""
    net = SfcNetwork(steps=[Step(name="X", initial=True)])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<SFC>" in xml
    assert "<ST>" not in xml


def test_step_element_has_required_attrs_and_position():
    net = SfcNetwork(steps=[Step(name="Idle", initial=True)])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Required attrs per the XSD: localId + name; initialStep
    # only when true.
    assert '<step localId="0" name="Idle" initialStep="true">' in xml
    assert '<position x="' in xml


def test_transition_condition_renders_as_inline_st():
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B")],
        transitions=[Transition(
            from_steps=("A",), to_steps=("B",),
            condition=(ContactNO(Address("start_btn")),),
        )],
    )
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # The inline ST condition wraps the boolean expression
    assert "<inline " in xml
    assert "start_btn" in xml


def test_compound_condition_with_NC_contact_renders():
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B")],
        transitions=[Transition(
            from_steps=("A",), to_steps=("B",),
            condition=(ContactNO(Address("start")),
                        ContactNC(Address("estop"))),
        )],
    )
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "start AND NOT estop" in xml


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


def test_round_trip_single_step_with_initial_flag():
    net = SfcNetwork(steps=[Step(name="Idle", initial=True,
                                    comment="boot state")])
    out = _round_trip(net)
    assert [s.name for s in out.steps] == ["Idle"]
    assert out.steps[0].initial
    assert out.steps[0].comment == "boot state"


def test_round_trip_three_step_pipeline():
    net = SfcNetwork(
        steps=[Step(name="Idle", initial=True),
                Step(name="Running"),
                Step(name="Stopped")],
        transitions=[
            Transition(from_steps=("Idle",), to_steps=("Running",),
                        condition=(ContactNO(Address("start")),)),
            Transition(from_steps=("Running",), to_steps=("Stopped",),
                        condition=(ContactNO(Address("stop")),)),
            Transition(from_steps=("Stopped",), to_steps=("Idle",)),
        ],
    )
    out = _round_trip(net)
    assert [s.name for s in out.steps] == ["Idle", "Running", "Stopped"]
    assert [(t.from_steps, t.to_steps) for t in out.transitions] == [
        (("Idle",), ("Running",)),
        (("Running",), ("Stopped",)),
        (("Stopped",), ("Idle",)),
    ]


def test_round_trip_unconditional_transition_yields_empty_condition():
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B")],
        transitions=[Transition(from_steps=("A",), to_steps=("B",))],
    )
    out = _round_trip(net)
    assert out.transitions[0].condition == ()


def test_round_trip_multi_from_transition_simultaneous_convergence():
    """A transition with multiple from_steps -- e.g. two parallel
    branches converging into a single follow-up."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True),
                Step(name="B", initial=True),
                Step(name="Joined")],
        transitions=[Transition(from_steps=("A", "B"),
                                  to_steps=("Joined",))],
    )
    out = _round_trip(net)
    assert set(out.transitions[0].from_steps) == {"A", "B"}
    assert out.transitions[0].to_steps == ("Joined",)


def test_round_trip_multi_to_transition_simultaneous_divergence():
    """A transition with multiple to_steps -- parallel-branch
    activation."""
    net = SfcNetwork(
        steps=[Step(name="Start", initial=True),
                Step(name="A"), Step(name="B")],
        transitions=[Transition(from_steps=("Start",),
                                  to_steps=("A", "B"))],
    )
    out = _round_trip(net)
    assert out.transitions[0].from_steps == ("Start",)
    assert set(out.transitions[0].to_steps) == {"A", "B"}


def test_round_trip_preserves_initial_step_among_many():
    """Only the explicitly-initial steps survive with
    ``initial=True``; the rest stay ``False`` (the XSD attribute
    is optional with default false)."""
    net = SfcNetwork(steps=[
        Step(name="A"),
        Step(name="B", initial=True),
        Step(name="C"),
    ])
    out = _round_trip(net)
    initial = [s.name for s in out.steps if s.initial]
    assert initial == ["B"]


def test_round_trip_step_comment_survives():
    net = SfcNetwork(steps=[
        Step(name="X", initial=True, comment="a documented step"),
    ])
    out = _round_trip(net)
    assert out.steps[0].comment == "a documented step"


def test_named_reference_condition_round_trips_as_textual_name():
    """Hand-rolled XML with ``<condition><reference name="cond1"/>``
    should parse and re-emit cleanly."""
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <interface/>
      <body><SFC>
        <step localId="0" name="A" initialStep="true">
          <position x="0" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
          <connectionPointOut formalParameter="OUT"/>
        </step>
        <step localId="1" name="B">
          <position x="0" y="0"/>
          <connectionPointIn><connection refLocalId="2"/></connectionPointIn>
          <connectionPointOut formalParameter="OUT"/>
        </step>
        <transition localId="2">
          <position x="0" y="0"/>
          <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
          <connectionPointOut/>
          <condition><reference name="myGuard"/></condition>
        </transition>
      </SFC></body>
    </pou>
  </pous></types>
</project>'''
    p = parse_plcopen_xml(xml)
    sfc = p.find_subroutine("Main").sfc
    # Named-reference condition lands as a single-contact gate
    # whose tag name carries the reference identifier.
    cond_ops = sfc.transitions[0].condition
    assert len(cond_ops) == 1
    from universal_machinery.il import TagRef
    assert cond_ops[0].address == TagRef("myGuard")
