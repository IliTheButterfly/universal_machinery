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
    Action, Address, SfcNetwork, Step, TagType, Transition,
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


# -----------------------------------------------------------------------------
# Action blocks (IEC §2.6.4.4 / PLCopen <actionBlock>)
# -----------------------------------------------------------------------------


def test_step_without_actions_emits_no_connection_point_out_action():
    """``<connectionPointOutAction>`` only appears on steps that
    have at least one Action attached -- empty-action steps stay
    minimal (per the XSD it's ``minOccurs=0``)."""
    net = SfcNetwork(steps=[Step(name="Idle", initial=True)])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "connectionPointOutAction" not in xml
    assert "<actionBlock" not in xml


def test_step_with_actions_emits_connection_point_out_action():
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="N", target=Address("Q0.0")),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<connectionPointOutAction formalParameter="OUT_ACTION"/>' in xml
    assert "<actionBlock " in xml
    assert 'qualifier="N"' in xml
    assert '<reference name="Q0.0"/>' in xml


def test_action_block_wires_back_to_step_via_OUT_ACTION():
    """The actionBlock's ``<connectionPointIn>`` points at the
    step's localId and tags ``formalParameter="OUT_ACTION"``."""
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="S", target=Address("MotorOn")),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # step is localId="0" since it's the only step
    assert ('<connection refLocalId="0" formalParameter="OUT_ACTION"/>'
            in xml)


def test_action_block_with_duration_emits_iec_time_literal():
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="L", target=Address("Lamp"), time_ms=2500),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'qualifier="L"' in xml
    assert 'duration="T#2500ms"' in xml


@pytest.mark.parametrize("qualifier", [
    "N", "R", "S", "L", "D", "P", "P0", "P1", "DS", "DL", "SD", "SL",
])
def test_all_valid_qualifiers_validate_against_xsd(qualifier):
    """Every IEC §2.6.4.4 qualifier the schema accepts should
    survive emission + XSD validation."""
    time_ms = 100 if qualifier in {"L", "D", "DS", "DL", "SD", "SL"} else None
    net = SfcNetwork(steps=[Step(name="S", initial=True, actions=(
        Action(qualifier=qualifier, target=Address("X"), time_ms=time_ms),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert f'qualifier="{qualifier}"' in xml


def test_unknown_qualifier_falls_back_to_N():
    """An out-of-spec qualifier in IL doesn't break the document --
    the emitter substitutes ``N`` so the XSD stays happy."""
    net = SfcNetwork(steps=[Step(name="S", initial=True, actions=(
        Action(qualifier="BOGUS", target=Address("X")),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'qualifier="N"' in xml
    assert 'qualifier="BOGUS"' not in xml


def test_pou_name_target_emits_reference_unchanged():
    """An ``Action.target`` that's a plain string (e.g. a POU
    name to invoke) is rendered verbatim in ``<reference name=>``."""
    net = SfcNetwork(steps=[Step(name="S", initial=True, actions=(
        Action(qualifier="N", target="DoTheThing"),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<reference name="DoTheThing"/>' in xml


def test_multiple_actions_per_step_share_one_action_block():
    """Multiple ``Action`` entries on a single ``Step`` lower
    into one ``<actionBlock>`` with multiple ``<action>``
    children (one per IL Action)."""
    net = SfcNetwork(steps=[Step(name="S", initial=True, actions=(
        Action(qualifier="N", target=Address("A")),
        Action(qualifier="S", target=Address("B")),
        Action(qualifier="R", target=Address("C")),
    ))])
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert xml.count("<actionBlock ") == 1
    assert xml.count("<action ") == 3


# -----------------------------------------------------------------------------
# Action-block round-trip
# -----------------------------------------------------------------------------


def test_round_trip_single_action_per_step():
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="N", target=Address("Q0.0")),
    ))])
    out = _round_trip(net)
    assert len(out.steps[0].actions) == 1
    a = out.steps[0].actions[0]
    assert a.qualifier == "N"
    # Address objects in IL round-trip back as strings (the
    # PLCopen ``<reference name=>`` body has no way to distinguish
    # Address from POU name); the IL ``target`` field is typed
    # ``Address | str`` to allow both forms.
    assert str(a.target) == "Q0.0"
    assert a.time_ms is None


def test_round_trip_action_with_duration_preserves_ms():
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="L", target=Address("Lamp"), time_ms=750),
    ))])
    out = _round_trip(net)
    assert out.steps[0].actions[0].qualifier == "L"
    assert out.steps[0].actions[0].time_ms == 750


def test_round_trip_action_with_comment_preserves_text():
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="N", target=Address("Q0.0"),
                comment="turn on the motor"),
    ))])
    out = _round_trip(net)
    assert out.steps[0].actions[0].comment == "turn on the motor"


def test_round_trip_multiple_actions_preserves_order():
    net = SfcNetwork(steps=[Step(name="Run", initial=True, actions=(
        Action(qualifier="N", target=Address("A")),
        Action(qualifier="S", target=Address("B")),
        Action(qualifier="R", target=Address("C")),
    ))])
    out = _round_trip(net)
    quals = [a.qualifier for a in out.steps[0].actions]
    assert quals == ["N", "S", "R"]


def test_round_trip_only_attaches_actions_to_owning_step():
    """In a multi-step network, each Step's actions stay attached
    to the right Step after round-trip (action-blocks resolve via
    the back-pointing localId, not by position)."""
    net = SfcNetwork(
        steps=[
            Step(name="A", initial=True, actions=(
                Action(qualifier="N", target=Address("oA")),
            )),
            Step(name="B", actions=(
                Action(qualifier="S", target=Address("oB1")),
                Action(qualifier="L", target=Address("oB2"), time_ms=200),
            )),
            Step(name="C"),
        ],
        transitions=[
            Transition(from_steps=("A",), to_steps=("B",)),
            Transition(from_steps=("B",), to_steps=("C",)),
        ],
    )
    out = _round_trip(net)
    by_name = {s.name: s for s in out.steps}
    assert [a.qualifier for a in by_name["A"].actions] == ["N"]
    assert [a.qualifier for a in by_name["B"].actions] == ["S", "L"]
    assert by_name["C"].actions == ()


# -----------------------------------------------------------------------------
# Reader-only: documents with hand-rolled actionBlock shapes
# -----------------------------------------------------------------------------


_HAND_ROLLED_ACTION_BLOCK = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body>
        <SFC>
          <step localId="0" name="S0" initialStep="true">
            <position x="0" y="0"/>
            <connectionPointOut formalParameter="OUT"/>
            <connectionPointOutAction formalParameter="OUT_ACTION"/>
          </step>
          <actionBlock localId="1">
            <position x="100" y="0"/>
            <connectionPointIn>
              <connection refLocalId="0" formalParameter="OUT_ACTION"/>
            </connectionPointIn>
            <action localId="2" qualifier="D" duration="T#3s">
              <relPosition x="0" y="0"/>
              <reference name="Heater"/>
            </action>
          </actionBlock>
        </SFC>
      </body>
    </pou>
  </pous></types>
</project>'''


def test_reader_parses_hand_rolled_action_block_with_seconds_duration():
    p = parse_plcopen_xml(_HAND_ROLLED_ACTION_BLOCK)
    s0 = p.find_subroutine("Main").sfc.steps[0]
    assert len(s0.actions) == 1
    a = s0.actions[0]
    assert a.qualifier == "D"
    assert a.time_ms == 3000      # T#3s -> 3000 ms
    assert str(a.target) == "Heater"


def test_reader_tolerates_unknown_qualifier_by_falling_back_to_N():
    bad = _HAND_ROLLED_ACTION_BLOCK.replace('qualifier="D"',
                                                 'qualifier="WHAT"')
    p = parse_plcopen_xml(bad)
    a = p.find_subroutine("Main").sfc.steps[0].actions[0]
    assert a.qualifier == "N"


def test_reader_drops_inline_action_body_until_we_model_them():
    """``<action><inline>...</inline></action>`` is XSD-valid but
    we don't yet model inline action bodies on the IL side -- the
    reader silently drops them so the rest of the document still
    parses."""
    inline_xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body>
        <SFC>
          <step localId="0" name="S0" initialStep="true">
            <position x="0" y="0"/>
            <connectionPointOut formalParameter="OUT"/>
            <connectionPointOutAction formalParameter="OUT_ACTION"/>
          </step>
          <actionBlock localId="1">
            <position x="100" y="0"/>
            <connectionPointIn>
              <connection refLocalId="0" formalParameter="OUT_ACTION"/>
            </connectionPointIn>
            <action localId="2" qualifier="N">
              <relPosition x="0" y="0"/>
              <inline name="inner"><ST><xhtml:pre xmlns:xhtml="http://www.w3.org/1999/xhtml">x := 1;</xhtml:pre></ST></inline>
            </action>
            <action localId="3" qualifier="S">
              <relPosition x="0" y="20"/>
              <reference name="Lamp"/>
            </action>
          </actionBlock>
        </SFC>
      </body>
    </pou>
  </pous></types>
</project>'''
    p = parse_plcopen_xml(inline_xml)
    actions = p.find_subroutine("Main").sfc.steps[0].actions
    # Only the <reference>-based action survives; the inline one
    # is dropped pending a future slice that models inline bodies.
    assert len(actions) == 1
    assert actions[0].qualifier == "S"
    assert str(actions[0].target) == "Lamp"


@pytest.mark.parametrize("raw,expected_ms", [
    ("T#500ms", 500),
    ("T#1s", 1000),
    ("T#1s500ms", 1500),
    ("T#2m", 120_000),
    ("T#1h", 3_600_000),
    ("TIME#250ms", 250),
])
def test_iec_time_literal_parses_into_ms(raw, expected_ms):
    """The reader's duration parser recognises the canonical IEC
    TIME shapes used by emitters."""
    from universal_machinery.parsers.plcopen_xml import _parse_duration_ms
    assert _parse_duration_ms(raw) == expected_ms


def test_unknown_time_literal_round_trips_as_None():
    """A duration we can't parse drops to ``None`` rather than
    raising -- conformance over strictness on the read path."""
    from universal_machinery.parsers.plcopen_xml import _parse_duration_ms
    assert _parse_duration_ms("not-a-duration") is None


# -----------------------------------------------------------------------------
# Divergence / convergence markers (IEC §2.6.3 / PLCopen
# <simultaneousDivergence> / <simultaneousConvergence> /
# <selectionDivergence> / <selectionConvergence>)
# -----------------------------------------------------------------------------


def _emit(net: SfcNetwork) -> str:
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    return xml


def test_linear_flow_emits_no_marker_elements():
    """A plain three-step pipeline doesn't need any divergence /
    convergence markers -- each transition has exactly one source
    and one destination."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B"), Step(name="C")],
        transitions=[
            Transition(from_steps=("A",), to_steps=("B",)),
            Transition(from_steps=("B",), to_steps=("C",)),
        ],
    )
    xml = _emit(net)
    for marker in ("simultaneousDivergence", "simultaneousConvergence",
                    "selectionDivergence", "selectionConvergence"):
        assert marker not in xml


# ---- simultaneous divergence ------------------------------------------------


def test_simultaneous_divergence_emits_marker_with_per_branch_pins():
    """One transition with multi to_steps lowers to a
    ``<simultaneousDivergence>`` marker between the transition
    and its destination steps; each destination references the
    marker via ``OUT<i>`` formalParameter."""
    net = SfcNetwork(
        steps=[Step(name="Start", initial=True),
                Step(name="A"), Step(name="B")],
        transitions=[Transition(from_steps=("Start",),
                                  to_steps=("A", "B"))],
    )
    xml = _emit(net)
    assert "<simultaneousDivergence " in xml
    assert 'formalParameter="OUT0"' in xml
    assert 'formalParameter="OUT1"' in xml


def test_simultaneous_divergence_round_trips_multi_to():
    net = SfcNetwork(
        steps=[Step(name="Start", initial=True),
                Step(name="A"), Step(name="B")],
        transitions=[Transition(from_steps=("Start",),
                                  to_steps=("A", "B"))],
    )
    out = _round_trip(net)
    assert out.transitions[0].from_steps == ("Start",)
    assert set(out.transitions[0].to_steps) == {"A", "B"}


def test_simultaneous_divergence_three_way_fanout_round_trips():
    net = SfcNetwork(
        steps=[Step(name="Start", initial=True),
                Step(name="A"), Step(name="B"), Step(name="C")],
        transitions=[Transition(from_steps=("Start",),
                                  to_steps=("A", "B", "C"))],
    )
    out = _round_trip(net)
    assert set(out.transitions[0].to_steps) == {"A", "B", "C"}


# ---- simultaneous convergence ----------------------------------------------


def test_simultaneous_convergence_emits_marker_with_multi_inputs():
    """One transition with multi from_steps lowers to a
    ``<simultaneousConvergence>`` marker; each from_step's
    outgoing wire lands at one of the marker's inputs."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True),
                Step(name="B", initial=True),
                Step(name="Joined")],
        transitions=[Transition(from_steps=("A", "B"),
                                  to_steps=("Joined",))],
    )
    xml = _emit(net)
    assert "<simultaneousConvergence " in xml
    # Two <connectionPointIn> elements (one per source) is the
    # distinctive convergence shape per the XSD.
    assert xml.count("<connectionPointIn>") >= 2


def test_simultaneous_convergence_round_trips_multi_from():
    net = SfcNetwork(
        steps=[Step(name="A", initial=True),
                Step(name="B", initial=True),
                Step(name="J")],
        transitions=[Transition(from_steps=("A", "B"),
                                  to_steps=("J",))],
    )
    out = _round_trip(net)
    assert set(out.transitions[0].from_steps) == {"A", "B"}
    assert out.transitions[0].to_steps == ("J",)


# ---- selection divergence ---------------------------------------------------


def test_selection_divergence_emits_marker_for_step_with_multiple_outgoing():
    """A step that's the only ``from_step`` of multiple
    transitions lowers to a ``<selectionDivergence>`` marker; each
    branch transition references the marker via ``OUT<i>``."""
    net = SfcNetwork(
        steps=[Step(name="Decide", initial=True),
                Step(name="Path1"), Step(name="Path2")],
        transitions=[
            Transition(from_steps=("Decide",), to_steps=("Path1",)),
            Transition(from_steps=("Decide",), to_steps=("Path2",)),
        ],
    )
    xml = _emit(net)
    assert "<selectionDivergence " in xml
    assert 'formalParameter="OUT0"' in xml
    assert 'formalParameter="OUT1"' in xml


def test_selection_divergence_round_trips_branch_targets():
    net = SfcNetwork(
        steps=[Step(name="Decide", initial=True),
                Step(name="P1"), Step(name="P2"), Step(name="P3")],
        transitions=[
            Transition(from_steps=("Decide",), to_steps=("P1",)),
            Transition(from_steps=("Decide",), to_steps=("P2",)),
            Transition(from_steps=("Decide",), to_steps=("P3",)),
        ],
    )
    out = _round_trip(net)
    branch_targets = {t.to_steps[0] for t in out.transitions}
    assert branch_targets == {"P1", "P2", "P3"}
    for t in out.transitions:
        assert t.from_steps == ("Decide",)


# ---- selection convergence --------------------------------------------------


def test_selection_convergence_emits_marker_for_step_with_multiple_incoming():
    """A step that's the only ``to_step`` of multiple transitions
    lowers to a ``<selectionConvergence>`` marker; the step itself
    just references the marker."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True),
                Step(name="B", initial=True),
                Step(name="J")],
        transitions=[
            Transition(from_steps=("A",), to_steps=("J",)),
            Transition(from_steps=("B",), to_steps=("J",)),
        ],
    )
    xml = _emit(net)
    assert "<selectionConvergence " in xml


def test_selection_convergence_round_trips_join_sources():
    net = SfcNetwork(
        steps=[Step(name="A", initial=True),
                Step(name="B", initial=True),
                Step(name="C", initial=True),
                Step(name="J")],
        transitions=[
            Transition(from_steps=("A",), to_steps=("J",)),
            Transition(from_steps=("B",), to_steps=("J",)),
            Transition(from_steps=("C",), to_steps=("J",)),
        ],
    )
    out = _round_trip(net)
    join = [t for t in out.transitions if t.to_steps == ("J",)]
    assert {t.from_steps[0] for t in join} == {"A", "B", "C"}


# ---- combined / interleaved patterns ----------------------------------------


def test_selection_div_then_selection_conv_round_trip_full_diamond():
    """Diamond: Decide branches to P1 / P2 (selection div), both
    re-converge at Done (selection conv).  All four marker shapes
    coexist in one body."""
    net = SfcNetwork(
        steps=[Step(name="Decide", initial=True),
                Step(name="P1"), Step(name="P2"),
                Step(name="Done")],
        transitions=[
            Transition(from_steps=("Decide",), to_steps=("P1",)),
            Transition(from_steps=("Decide",), to_steps=("P2",)),
            Transition(from_steps=("P1",), to_steps=("Done",)),
            Transition(from_steps=("P2",), to_steps=("Done",)),
        ],
    )
    xml = _emit(net)
    assert "<selectionDivergence " in xml
    assert "<selectionConvergence " in xml
    out = _round_trip(net)
    out_targets = {(t.from_steps, t.to_steps) for t in out.transitions}
    assert out_targets == {
        (("Decide",), ("P1",)),
        (("Decide",), ("P2",)),
        (("P1",), ("Done",)),
        (("P2",), ("Done",)),
    }


def test_simultaneous_div_then_conv_round_trip_parallel_pair():
    """Fork-join: Start forks to A and B (sim div), both join at
    End (sim conv via multi-from transition)."""
    net = SfcNetwork(
        steps=[Step(name="Start", initial=True),
                Step(name="A"), Step(name="B"),
                Step(name="End")],
        transitions=[
            Transition(from_steps=("Start",), to_steps=("A", "B")),
            Transition(from_steps=("A", "B"), to_steps=("End",)),
        ],
    )
    xml = _emit(net)
    assert "<simultaneousDivergence " in xml
    assert "<simultaneousConvergence " in xml
    out = _round_trip(net)
    fork = next(t for t in out.transitions if t.from_steps == ("Start",))
    join = next(t for t in out.transitions if t.to_steps == ("End",))
    assert set(fork.to_steps) == {"A", "B"}
    assert set(join.from_steps) == {"A", "B"}


def test_marker_emission_keeps_existing_step_and_transition_localIds():
    """Markers get fresh localIds after the steps + transitions
    block; existing localId allocation order (steps 0..N-1, then
    transitions N..N+M-1) stays unchanged."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B"), Step(name="C")],
        transitions=[Transition(from_steps=("A",), to_steps=("B", "C"))],
    )
    xml = _emit(net)
    assert '<step localId="0" name="A" initialStep="true">' in xml
    assert '<step localId="1" name="B">' in xml
    assert '<step localId="2" name="C">' in xml
    assert '<transition localId="3">' in xml
    # Marker is the next available localId (4).
    assert '<simultaneousDivergence localId="4">' in xml


# ---- markers + actions on the same body -------------------------------------


def test_action_block_localIds_skip_over_marker_ids():
    """When both markers and action blocks are present, the
    action-block localIds get allocated after the markers so all
    IDs stay unique within the body."""
    net = SfcNetwork(
        steps=[Step(name="Start", initial=True,
                      actions=(Action(qualifier="N",
                                         target=Address("Q0.0")),)),
                Step(name="A"), Step(name="B")],
        transitions=[Transition(from_steps=("Start",),
                                  to_steps=("A", "B"))],
    )
    xml = _emit(net)
    # Steps 0..2, transition 3, sim_div 4, action block 5, action 6
    assert '<actionBlock localId="5">' in xml
    assert '<action localId="6" qualifier="N">' in xml


# ---- reader-only: hand-rolled marker shapes ---------------------------------


_HAND_ROLLED_SIM_DIV = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body>
        <SFC>
          <step localId="0" name="Start" initialStep="true">
            <position x="0" y="0"/>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <step localId="1" name="A">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="4" formalParameter="OUT0"/>
            </connectionPointIn>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <step localId="2" name="B">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="4" formalParameter="OUT1"/>
            </connectionPointIn>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <transition localId="3">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="0"/>
            </connectionPointIn>
            <connectionPointOut/>
          </transition>
          <simultaneousDivergence localId="4">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="3"/>
            </connectionPointIn>
            <connectionPointOut formalParameter="OUT0"/>
            <connectionPointOut formalParameter="OUT1"/>
          </simultaneousDivergence>
        </SFC>
      </body>
    </pou>
  </pous></types>
</project>'''


def test_reader_dissolves_hand_rolled_simultaneous_divergence():
    p = parse_plcopen_xml(_HAND_ROLLED_SIM_DIV)
    sfc = p.find_subroutine("Main").sfc
    assert {t.from_steps[0] for t in sfc.transitions} == {"Start"}
    assert set(sfc.transitions[0].to_steps) == {"A", "B"}


_HAND_ROLLED_SEL_CONV = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body>
        <SFC>
          <step localId="0" name="A" initialStep="true">
            <position x="0" y="0"/>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <step localId="1" name="B" initialStep="true">
            <position x="0" y="0"/>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <step localId="2" name="J">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="5"/>
            </connectionPointIn>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <transition localId="3">
            <position x="0" y="0"/>
            <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
            <connectionPointOut/>
          </transition>
          <transition localId="4">
            <position x="0" y="0"/>
            <connectionPointIn><connection refLocalId="1"/></connectionPointIn>
            <connectionPointOut/>
          </transition>
          <selectionConvergence localId="5">
            <position x="0" y="0"/>
            <connectionPointIn><connection refLocalId="3"/></connectionPointIn>
            <connectionPointIn><connection refLocalId="4"/></connectionPointIn>
            <connectionPointOut/>
          </selectionConvergence>
        </SFC>
      </body>
    </pou>
  </pous></types>
</project>'''


def test_reader_dissolves_hand_rolled_selection_convergence():
    """Two transitions feeding a selectionConvergence both end up
    targeting the downstream step in IL ``to_steps``."""
    p = parse_plcopen_xml(_HAND_ROLLED_SEL_CONV)
    sfc = p.find_subroutine("Main").sfc
    j_targets = [t for t in sfc.transitions if t.to_steps == ("J",)]
    assert len(j_targets) == 2
    assert {t.from_steps[0] for t in j_targets} == {"A", "B"}


def test_reader_traces_through_chained_markers():
    """A document where a simultaneousDivergence feeds another
    marker before reaching the step.  The reader should
    transitively follow the chain."""
    chained = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="Main" pouType="program">
      <body>
        <SFC>
          <step localId="0" name="Start" initialStep="true">
            <position x="0" y="0"/>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <step localId="1" name="A">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="3" formalParameter="OUT0"/>
            </connectionPointIn>
            <connectionPointOut formalParameter="OUT"/>
          </step>
          <transition localId="2">
            <position x="0" y="0"/>
            <connectionPointIn><connection refLocalId="0"/></connectionPointIn>
            <connectionPointOut/>
          </transition>
          <simultaneousDivergence localId="3">
            <position x="0" y="0"/>
            <connectionPointIn>
              <connection refLocalId="2"/>
            </connectionPointIn>
            <connectionPointOut formalParameter="OUT0"/>
          </simultaneousDivergence>
        </SFC>
      </body>
    </pou>
  </pous></types>
</project>'''
    p = parse_plcopen_xml(chained)
    sfc = p.find_subroutine("Main").sfc
    # The single transition should have from_steps=Start and to_steps=A
    assert sfc.transitions[0].from_steps == ("Start",)
    assert sfc.transitions[0].to_steps == ("A",)
