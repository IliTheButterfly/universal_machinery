"""Type-checking tests for SFC transition conditions (IEC §2.6 / §6.5).

The validator now runs the same IEC §6.5 compatibility checks on
each ``Transition.condition`` tuple that it runs on rung ops --
contacts must be BOOL, compare operands must share a bucket.

Two new error codes:

  - ``sfc-contact-not-bool``      : ContactNO / ContactNC target
                                     isn't BOOL.
  - ``sfc-compare-type-mismatch`` : Compare operands cross IEC
                                     §6.5 buckets.

``ParallelGroup`` (OR branches inside a transition) recurses
into each branch so the deepest contact still gets checked.
"""
import pytest

from universal_machinery.builders import prog, program, tag_decl
from universal_machinery.il import (
    SfcNetwork, Step, TagRef, TagType, Transition,
)
from universal_machinery.il.ops import (
    Compare, ContactNC, ContactNO, ParallelGroup,
)
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


def _make_program(transitions, *, tags=()):
    """Helper: a one-POU Program with the given transitions and tags."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True), Step(name="B")],
        transitions=list(transitions),
    )
    return program(
        tags=list(tags),
        subroutines=[prog("Main", main=True, sfc=net)],
    )


# -----------------------------------------------------------------------------
# Clean SFC conditions (no errors)
# -----------------------------------------------------------------------------


def test_clean_single_bool_contact_passes():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNO(TagRef("start_btn")),))],
        tags=[tag_decl("start_btn", TagType.BOOL)],
    )
    assert _codes(p) == []


def test_clean_no_nc_chain_passes():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNO(TagRef("start")),
                                               ContactNC(TagRef("estop"))))],
        tags=[tag_decl("start", TagType.BOOL),
               tag_decl("estop", TagType.BOOL)],
    )
    assert _codes(p) == []


def test_clean_compare_with_matching_types():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(Compare(op=">",
                                                       lhs=TagRef("speed"),
                                                       rhs=TagRef("setpoint")),))],
        tags=[tag_decl("speed", TagType.INT),
               tag_decl("setpoint", TagType.INT)],
    )
    assert _codes(p) == []


def test_clean_compare_against_literal_skipped():
    """Comparing a tag against a literal value doesn't raise --
    literal type is unknown and the check skips."""
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(Compare(op=">",
                                                       lhs=TagRef("speed"),
                                                       rhs="100"),))],
        tags=[tag_decl("speed", TagType.INT)],
    )
    assert "sfc-compare-type-mismatch" not in _codes(p)


def test_clean_empty_condition_passes():
    """Unconditional transition (empty condition tuple) is fine."""
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=())],
    )
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Non-BOOL contact target
# -----------------------------------------------------------------------------


def test_int_contact_target_raises():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNO(TagRef("count")),))],
        tags=[tag_decl("count", TagType.INT)],
    )
    assert "sfc-contact-not-bool" in _codes(p)


def test_real_contact_target_raises():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNC(TagRef("temp")),))],
        tags=[tag_decl("temp", TagType.REAL)],
    )
    assert "sfc-contact-not-bool" in _codes(p)


def test_word_contact_target_raises():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNO(TagRef("flags")),))],
        tags=[tag_decl("flags", TagType.WORD)],
    )
    assert "sfc-contact-not-bool" in _codes(p)


def test_unresolved_contact_doesnt_raise_type_error():
    """Unresolved names are caught by the structural pass with
    ``unresolved-tagref``; the SFC type checker stays silent."""
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNO(TagRef("missing")),))],
    )
    codes = _codes(p)
    assert "sfc-contact-not-bool" not in codes
    # The structural pass complains separately
    assert "unresolved-tagref" in codes


# -----------------------------------------------------------------------------
# Cross-bucket compare
# -----------------------------------------------------------------------------


def test_bool_int_compare_raises():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(Compare(op=">",
                                                       lhs=TagRef("flag"),
                                                       rhs=TagRef("count")),))],
        tags=[tag_decl("flag", TagType.BOOL),
               tag_decl("count", TagType.INT)],
    )
    assert "sfc-compare-type-mismatch" in _codes(p)


def test_string_int_compare_raises():
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(Compare(op="=",
                                                       lhs=TagRef("name"),
                                                       rhs=TagRef("count")),))],
        tags=[tag_decl("name", TagType.STRING),
               tag_decl("count", TagType.INT)],
    )
    assert "sfc-compare-type-mismatch" in _codes(p)


def test_real_lreal_compare_passes():
    """REAL ↔ LREAL share a bucket -- the compare doesn't raise."""
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(Compare(op=">",
                                                       lhs=TagRef("r32"),
                                                       rhs=TagRef("r64")),))],
        tags=[tag_decl("r32", TagType.REAL),
               tag_decl("r64", TagType.LREAL)],
    )
    assert "sfc-compare-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# ParallelGroup recurses into branches
# -----------------------------------------------------------------------------


def test_parallel_group_with_bad_branch_flagged():
    """Each branch of a transition's ParallelGroup gets walked;
    a non-BOOL contact in any branch raises."""
    pg = ParallelGroup(branches=(
        (ContactNO(TagRef("manual")),),
        (ContactNO(TagRef("count")),),   # INT contact -- raises
    ))
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(pg,))],
        tags=[tag_decl("manual", TagType.BOOL),
               tag_decl("count", TagType.INT)],
    )
    assert "sfc-contact-not-bool" in _codes(p)


def test_parallel_group_with_all_bool_passes():
    pg = ParallelGroup(branches=(
        (ContactNO(TagRef("a")),),
        (ContactNO(TagRef("b")),),
        (ContactNC(TagRef("c")),),
    ))
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(pg,))],
        tags=[tag_decl("a", TagType.BOOL),
               tag_decl("b", TagType.BOOL),
               tag_decl("c", TagType.BOOL)],
    )
    assert "sfc-contact-not-bool" not in _codes(p)


# -----------------------------------------------------------------------------
# Location annotation
# -----------------------------------------------------------------------------


def test_error_location_names_transition_endpoints():
    """Error location strings carry the transition's from/to
    step names so users can quickly find the offending edge."""
    p = _make_program(
        transitions=[Transition(from_steps=("A",), to_steps=("B",),
                                  condition=(ContactNO(TagRef("count")),))],
        tags=[tag_decl("count", TagType.INT)],
    )
    errs = validate(p)
    contact_err = next(e for e in errs if e.code == "sfc-contact-not-bool")
    assert "A -> B" in contact_err.location
    assert "Main" in contact_err.location


# -----------------------------------------------------------------------------
# Multiple transitions in one body
# -----------------------------------------------------------------------------


def test_multiple_transitions_each_checked_independently():
    """Bad condition in only the second transition still raises."""
    net = SfcNetwork(
        steps=[Step(name="A", initial=True),
                Step(name="B"), Step(name="C")],
        transitions=[
            Transition(from_steps=("A",), to_steps=("B",),
                        condition=(ContactNO(TagRef("ok")),)),
            Transition(from_steps=("B",), to_steps=("C",),
                        condition=(ContactNO(TagRef("count")),)),  # INT
        ],
    )
    p = program(
        tags=[tag_decl("ok", TagType.BOOL),
               tag_decl("count", TagType.INT)],
        subroutines=[prog("Main", main=True, sfc=net)],
    )
    errs = validate(p)
    bad_errs = [e for e in errs if e.code == "sfc-contact-not-bool"]
    assert len(bad_errs) == 1
    assert "B -> C" in bad_errs[0].location
