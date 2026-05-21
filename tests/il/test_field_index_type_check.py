"""Type-checking tests for struct member + array element access.

Previous slices covered scalar variable references in ST AST
bodies (PR #24).  This slice extends the type-resolver to walk
``FieldAccess`` / ``IndexAccess`` chains through
``Program.user_types``: a STRUCT member's type, an ARRAY's
element type, and chains thereof (``axes[0].pose.x``) all
resolve to the leaf type-name and participate in the same
IEC §6.5 compatibility checks.

No new error codes -- existing ones (``st-assignment-type-mismatch``,
``st-condition-not-bool``) now fire on the deeper expression
shapes too.
"""
import pytest

from universal_machinery.builders import (
    abs_, add_e, alias_type, array_type, assign, eq_e, field_, for_, gt_e,
    if_, index_, lit, named_type, prog, program, struct_type, var,
    var_in,
)
from universal_machinery.il import TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# Struct member assignment
# -----------------------------------------------------------------------------


def test_struct_member_assignment_with_matching_type_passes():
    p = program(
        user_types=[struct_type("Axis", [var("pos", TagType.REAL)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis"))],
            st_body=[assign(field_("a", "pos"), lit(3.14))],
        )],
    )
    assert _codes(p) == []


def test_struct_member_assignment_type_mismatch_raises():
    p = program(
        user_types=[struct_type("Axis", [var("flags", TagType.WORD)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis"))],
            st_body=[assign(field_("a", "flags"), lit(3.14))],
        )],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


def test_struct_member_int_to_real_raises():
    p = program(
        user_types=[struct_type("Axis", [var("pos", TagType.REAL)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis"))],
            st_body=[assign(field_("a", "pos"), lit(42))],
        )],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


def test_unknown_struct_member_silently_skipped():
    """``a.nonexistent := value`` -- structural validation
    doesn't cover unknown member references, so the type
    checker stays silent instead of raising a false positive."""
    p = program(
        user_types=[struct_type("Axis", [var("pos", TagType.REAL)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis"))],
            st_body=[assign(field_("a", "missing"), lit(3.14))],
        )],
    )
    assert "st-assignment-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Nested struct members
# -----------------------------------------------------------------------------


def test_nested_struct_member_resolves():
    """``robot.head.angle := 45.0`` -- two-deep struct chain."""
    p = program(
        user_types=[
            struct_type("Head", [var("angle", TagType.REAL)]),
            struct_type("Robot", [var("head", named_type("Head"))]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("r", named_type("Robot"))],
            st_body=[assign(field_(field_("r", "head"), "angle"),
                              lit(45.0))],
        )],
    )
    assert _codes(p) == []


def test_nested_struct_member_type_mismatch():
    p = program(
        user_types=[
            struct_type("Head", [var("angle", TagType.REAL)]),
            struct_type("Robot", [var("head", named_type("Head"))]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("r", named_type("Robot"))],
            st_body=[assign(field_(field_("r", "head"), "angle"),
                              lit(True))],   # BOOL → REAL
        )],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# Array element access
# -----------------------------------------------------------------------------


def test_array_element_assignment_with_matching_type_passes():
    p = program(
        user_types=[array_type("Bytes8",
                                  element_type=TagType.BYTE,
                                  bounds=[(0, 7)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("b", named_type("Bytes8"))],
            st_body=[assign(index_("b", lit(0)), lit(255))],
        )],
    )
    assert _codes(p) == []


def test_array_element_assignment_type_mismatch_raises():
    p = program(
        user_types=[array_type("Reals",
                                  element_type=TagType.REAL,
                                  bounds=[(0, 9)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("r", named_type("Reals"))],
            st_body=[assign(index_("r", lit(0)), lit(True))],   # BOOL → REAL
        )],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# Mixed field + index chains
# -----------------------------------------------------------------------------


def test_array_of_struct_field_resolves():
    """``axes[3].position := 1.5`` -- index into array, then
    field access on the element."""
    p = program(
        user_types=[
            struct_type("Axis", [var("position", TagType.REAL)]),
            array_type("Axes",
                         element_type=named_type("Axis"),
                         bounds=[(0, 5)]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("axes", named_type("Axes"))],
            st_body=[assign(field_(index_("axes", lit(3)), "position"),
                              lit(1.5))],
        )],
    )
    assert _codes(p) == []


def test_array_of_struct_field_mismatch():
    p = program(
        user_types=[
            struct_type("Axis", [var("position", TagType.REAL)]),
            array_type("Axes",
                         element_type=named_type("Axis"),
                         bounds=[(0, 5)]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("axes", named_type("Axes"))],
            st_body=[assign(field_(index_("axes", lit(3)), "position"),
                              lit(True))],
        )],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


def test_struct_field_that_is_array_then_index():
    """``axis.history[0] := 0`` -- field access reaches an
    ARRAY-typed member, then index into it."""
    p = program(
        user_types=[
            array_type("Samples",
                         element_type=TagType.INT,
                         bounds=[(0, 9)]),
            struct_type("Axis", [var("history", named_type("Samples"))]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis"))],
            st_body=[assign(index_(field_("a", "history"), lit(0)),
                              lit(42))],
        )],
    )
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Field-access expression as RHS (not lvalue)
# -----------------------------------------------------------------------------


def test_field_access_value_type_used_in_assignment_rhs():
    """``y := axis.position;`` -- the RHS infers as the member's
    type (REAL), which doesn't match an INT target."""
    p = program(
        user_types=[struct_type("Axis", [var("position", TagType.REAL)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis")),
                         var("y", TagType.INT)],
            st_body=[assign("y", field_("a", "position"))],
        )],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


def test_field_access_in_comparison_yields_bool():
    """``IF axis.position > 100.0 THEN``"""
    p = program(
        user_types=[struct_type("Axis", [var("position", TagType.REAL)])],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis")),
                         var("alarm", TagType.BOOL)],
            st_body=[if_((gt_e(field_("a", "position"), lit(100.0)),
                            [assign("alarm", lit(True))]))],
        )],
    )
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Alias chains in member types
# -----------------------------------------------------------------------------


def test_struct_member_typed_as_alias_resolves_to_base():
    """A struct field typed as ``Distance`` (alias of REAL) is
    REAL-compatible."""
    p = program(
        user_types=[
            alias_type("Distance", base=TagType.REAL),
            struct_type("Axis", [var("pos", named_type("Distance"))]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("a", named_type("Axis"))],
            st_body=[assign(field_("a", "pos"), lit(3.14))],
        )],
    )
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Unresolved cases stay silent
# -----------------------------------------------------------------------------


def test_field_access_on_non_struct_type_silently_skipped():
    """``count.something`` where ``count`` is INT, not a struct.
    The structural resolver returns None, type check skips."""
    p = program(
        subroutines=[prog("Main", main=True,
            local_vars=[var("count", TagType.INT),
                         var("flag", TagType.BOOL)],
            st_body=[assign("flag", field_("count", "anything"))],
        )],
    )
    # Should not raise type-mismatch; the field access type
    # resolves to None and the check skips
    assert "st-assignment-type-mismatch" not in _codes(p)


def test_index_access_on_non_array_silently_skipped():
    p = program(
        subroutines=[prog("Main", main=True,
            local_vars=[var("scalar", TagType.INT),
                         var("flag", TagType.BOOL)],
            st_body=[assign("flag", index_("scalar", lit(0)))],
        )],
    )
    assert "st-assignment-type-mismatch" not in _codes(p)
