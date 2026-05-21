"""Tests for the SFC condition-text lowering pass in the PLCopen
XML reader.

PR #19 captured transition conditions as a single placeholder
``ContactNO(TagRef(text))``.  This slice routes the text through
the ST expression parser and lowers AND / NOT / OR over bare
variable references to structured IL LD ops.  Anything outside
that subset still falls back to the textual placeholder.

Coverage:
  - Direct unit tests on the ``_expression_to_ld_ops`` helper
    for each grammar shape.
  - Full SFC round-trip (emit -> parse) for the same shapes.
  - Fallback behavior for complex expressions.
"""
import pytest

from universal_machinery.builders import prog, program
from universal_machinery.emitters.plcopen_xml import emit_xml
from universal_machinery.il import (
    Address, BinaryOp, Literal, SfcNetwork, Step, TagRef, Transition,
)
from universal_machinery.il.ops import ContactNC, ContactNO, ParallelGroup
from universal_machinery.parsers import parse_st_expression
from universal_machinery.parsers.plcopen_xml import (
    _expression_to_ld_ops, parse_plcopen_xml,
)


# -----------------------------------------------------------------------------
# Unit tests on the lowering helper
# -----------------------------------------------------------------------------


def _lower(text: str):
    return _expression_to_ld_ops(parse_st_expression(text))


def test_bare_var_lowers_to_single_NO_contact():
    out = _lower("a")
    assert out == (ContactNO(TagRef("a")),)


def test_NOT_var_lowers_to_NC_contact():
    out = _lower("NOT flag")
    assert out == (ContactNC(TagRef("flag")),)


def test_AND_chain_concatenates_contacts():
    out = _lower("a AND b")
    assert out == (ContactNO(TagRef("a")), ContactNO(TagRef("b")))


def test_AND_with_NOT_produces_mixed_NO_NC_chain():
    out = _lower("a AND NOT b")
    assert out == (ContactNO(TagRef("a")), ContactNC(TagRef("b")))


def test_OR_produces_parallel_group():
    out = _lower("a OR b")
    assert len(out) == 1
    pg = out[0]
    assert isinstance(pg, ParallelGroup)
    assert pg.branches == (
        (ContactNO(TagRef("a")),),
        (ContactNO(TagRef("b")),),
    )


def test_three_way_OR_flattens_to_single_parallel_group():
    """``a OR b OR c`` should produce a single ParallelGroup with
    three branches, not nested groups."""
    out = _lower("a OR b OR c")
    assert len(out) == 1
    pg = out[0]
    assert isinstance(pg, ParallelGroup)
    assert len(pg.branches) == 3


def test_AND_OR_combined_with_parens():
    """``a AND (b OR c)`` -> contact a, then a parallel group of b OR c."""
    out = _lower("a AND (b OR c)")
    assert len(out) == 2
    assert out[0] == ContactNO(TagRef("a"))
    assert isinstance(out[1], ParallelGroup)
    assert out[1].branches == (
        (ContactNO(TagRef("b")),),
        (ContactNO(TagRef("c")),),
    )


def test_OR_of_AND_terms():
    """``(a AND b) OR c`` -> a parallel group with two branches,
    the first containing two contacts in series."""
    out = _lower("(a AND b) OR c")
    assert len(out) == 1
    pg = out[0]
    assert isinstance(pg, ParallelGroup)
    assert len(pg.branches) == 2
    assert pg.branches[0] == (
        ContactNO(TagRef("a")), ContactNO(TagRef("b")),
    )
    assert pg.branches[1] == (ContactNO(TagRef("c")),)


def test_double_negation_collapses_to_NO():
    """``NOT NOT a`` is logically identical to ``a``."""
    out = _lower("NOT NOT a")
    assert out == (ContactNO(TagRef("a")),)


# -----------------------------------------------------------------------------
# Fallback shapes -- helper returns None, reader uses textual placeholder
# -----------------------------------------------------------------------------


def test_literal_alone_doesnt_lower():
    """``TRUE`` / ``FALSE`` / numeric literals aren't valid
    boolean operands for the LD-op grammar."""
    assert _expression_to_ld_ops(Literal("TRUE", kind="bool")) is None
    assert _expression_to_ld_ops(Literal("42", kind="int")) is None


def test_comparison_expression_doesnt_lower():
    """``x > 5`` lives in IL.Compare, not in the boolean
    contact-grammar this helper handles."""
    out = _expression_to_ld_ops(parse_st_expression("x > 5"))
    assert out is None


def test_field_access_doesnt_lower():
    """``axis.ready`` could be a valid IEC condition but our
    contact model uses simple Address/TagRef operands; field
    access bails out."""
    out = _expression_to_ld_ops(parse_st_expression("axis.ready"))
    assert out is None


def test_function_call_doesnt_lower():
    out = _expression_to_ld_ops(parse_st_expression("MAX(a, b)"))
    assert out is None


def test_arithmetic_doesnt_lower():
    out = _expression_to_ld_ops(parse_st_expression("a + b"))
    assert out is None


# -----------------------------------------------------------------------------
# SFC round-trip via emit_xml -> parse_plcopen_xml
# -----------------------------------------------------------------------------


def _round_trip_condition(condition):
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B")],
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=condition)],
    )
    p = program(subroutines=[prog("Main", main=True, sfc=net)])
    xml = emit_xml(p)
    p2 = parse_plcopen_xml(xml)
    return p2.find_subroutine("Main").sfc.transitions[0].condition


def test_round_trip_single_NO_contact():
    cond = (ContactNO(TagRef("start_btn")),)
    assert _round_trip_condition(cond) == cond


def test_round_trip_NC_contact():
    cond = (ContactNC(TagRef("estop")),)
    assert _round_trip_condition(cond) == cond


def test_round_trip_AND_chain():
    cond = (
        ContactNO(TagRef("start")),
        ContactNC(TagRef("estop")),
        ContactNO(TagRef("ready")),
    )
    assert _round_trip_condition(cond) == cond


def test_round_trip_OR_branches():
    cond = (ParallelGroup(branches=(
        (ContactNO(TagRef("manual")),),
        (ContactNO(TagRef("auto")),),
    )),)
    assert _round_trip_condition(cond) == cond


def test_round_trip_complex_AND_OR_keeps_shape():
    """``a AND (b OR c)`` -- the emitter renders with parens, the
    parser reconstructs the same AST."""
    cond = (
        ContactNO(TagRef("a")),
        ParallelGroup(branches=(
            (ContactNO(TagRef("b")),),
            (ContactNO(TagRef("c")),),
        )),
    )
    assert _round_trip_condition(cond) == cond


def test_round_trip_empty_condition_stays_empty():
    """Unconditional transition -> empty condition tuple."""
    cond = ()
    assert _round_trip_condition(cond) == ()


def test_round_trip_fallback_for_unsupported_condition():
    """A Compare op renders as ``x > 5`` which the lowering
    helper rejects -- the reader falls back to a single TagRef
    placeholder.  The re-emission still works (gate formatter
    renders the TagRef name verbatim), so the second round-trip
    is stable even if not AST-equal to the original.
    """
    from universal_machinery.il.ops import Compare
    cond = (Compare(op=">", lhs=TagRef("x"), rhs="5"),)
    out = _round_trip_condition(cond)
    # Single ContactNO carrying the placeholder text
    assert len(out) == 1
    assert isinstance(out[0], ContactNO)
    # Second round-trip is stable
    out2 = _round_trip_condition(out)
    assert out == out2
