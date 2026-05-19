"""Tests for IEC 61131-3 §2.3.3 user-defined types in the IL.

Covers ``StructType``, ``ArrayType``, ``EnumType``, ``AliasType`` and
``NamedType`` references, plus the Program-level declaration table.
Builder DSL helpers (``struct_type``, ``array_type``, ``enum_type``,
``alias_type``, ``named_type``) are exercised here too -- they should
construct exactly the same dataclasses as direct construction.
"""
import pytest

from universal_machinery.il import (
    AliasType, ArrayType, EnumType, NamedType, Program, StructType,
    SubrangeType, TagType, Var, is_elementary, is_signed_subrange,
    is_user_type, type_name,
)
from universal_machinery.builders import (
    alias_type, array_type, enum_type, named_type, program, struct_type,
    subrange_type, var,
)


# -----------------------------------------------------------------------------
# StructType
# -----------------------------------------------------------------------------


def test_struct_type_construction():
    """STRUCT with two INT members."""
    point = struct_type("Point", members=[
        var("x", TagType.INT),
        var("y", TagType.INT),
    ])
    assert point == StructType(
        name="Point",
        members=(
            Var(name="x", data_type=TagType.INT),
            Var(name="y", data_type=TagType.INT),
        ),
    )


def test_struct_type_with_nested_named_type():
    """A struct can have another struct's name as a member type via
    ``NamedType`` -- resolved by name at lower time."""
    line = struct_type("Line", members=[
        var("start", named_type("Point")),
        var("end",   named_type("Point")),
    ])
    assert line.members[0].data_type == NamedType("Point")
    assert line.members[1].data_type == NamedType("Point")


def test_struct_type_members_are_frozen():
    """``StructType.members`` is a tuple -- can't be mutated post-construction."""
    point = struct_type("Point", members=[var("x", TagType.INT)])
    assert isinstance(point.members, tuple)
    # Frozen dataclass -- can't reassign fields
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.name = "Other"        # type: ignore[misc]


# -----------------------------------------------------------------------------
# ArrayType
# -----------------------------------------------------------------------------


def test_array_type_single_dimension():
    vec = array_type("Vector10", element_type=TagType.INT,
                     bounds=[(0, 9)])
    assert vec == ArrayType(
        name="Vector10",
        element_type=TagType.INT,
        bounds=((0, 9),),
    )


def test_array_type_multidimensional():
    """Matrix declared with multiple bounds tuples."""
    mat = array_type("Matrix3x3", element_type=TagType.REAL,
                     bounds=[(0, 2), (0, 2)])
    assert mat.bounds == ((0, 2), (0, 2))
    assert mat.element_type is TagType.REAL


def test_array_type_with_struct_element_type():
    """ARRAY OF STRUCT via NamedType."""
    points = array_type("PointArray", element_type=named_type("Point"),
                        bounds=[(1, 100)])
    assert points.element_type == NamedType("Point")
    assert points.bounds == ((1, 100),)


# -----------------------------------------------------------------------------
# EnumType
# -----------------------------------------------------------------------------


def test_enum_type_construction():
    color = enum_type("Color", values=["RED", "GREEN", "BLUE"])
    assert color == EnumType(
        name="Color",
        values=("RED", "GREEN", "BLUE"),
    )


def test_enum_values_are_ordered_tuple():
    """Values preserve declaration order (IEC numbers them implicitly
    starting at 0)."""
    state = enum_type("State", values=["IDLE", "RUNNING", "DONE"])
    assert state.values == ("IDLE", "RUNNING", "DONE")


# -----------------------------------------------------------------------------
# SubrangeType
# -----------------------------------------------------------------------------


def test_subrange_of_signed_integer():
    """``TYPE SmallInt : INT (-100..100); END_TYPE``"""
    t = subrange_type("SmallInt", TagType.INT, lower=-100, upper=100)
    assert t == SubrangeType(
        name="SmallInt", base=TagType.INT, lower=-100, upper=100,
    )
    assert is_signed_subrange(t) is True


def test_subrange_of_unsigned_integer():
    """``TYPE Percent : UINT (0..100); END_TYPE``"""
    t = subrange_type("Percent", TagType.UINT, lower=0, upper=100)
    assert t.lower == 0 and t.upper == 100
    assert is_signed_subrange(t) is False


def test_subrange_all_signed_int_widths_classified_signed():
    for base in (TagType.SINT, TagType.INT, TagType.DINT, TagType.LINT):
        t = subrange_type("S", base, lower=0, upper=1)
        assert is_signed_subrange(t) is True, f"{base} should be signed"


def test_subrange_all_unsigned_int_widths_classified_unsigned():
    for base in (TagType.USINT, TagType.UINT, TagType.UDINT, TagType.ULINT):
        t = subrange_type("U", base, lower=0, upper=1)
        assert is_signed_subrange(t) is False, f"{base} should be unsigned"


def test_subrange_is_a_user_type():
    t = subrange_type("X", TagType.INT, lower=0, upper=10)
    assert is_user_type(t) is True
    assert is_elementary(t) is False
    assert type_name(t) == "X"


def test_program_collects_subrange_types():
    t1 = subrange_type("SmallInt", TagType.INT, lower=-100, upper=100)
    t2 = subrange_type("Percent", TagType.UINT, lower=0, upper=100)
    p = program(user_types=[t1, t2])
    assert p.find_user_type("SmallInt") is t1
    assert p.find_user_type("Percent") is t2


# -----------------------------------------------------------------------------
# AliasType
# -----------------------------------------------------------------------------


def test_alias_of_elementary_type():
    """``TYPE Distance : INT; END_TYPE``"""
    distance = alias_type("Distance", base=TagType.INT)
    assert distance == AliasType(name="Distance", base=TagType.INT)


def test_alias_of_user_type():
    """Aliases can target other UDTs via NamedType."""
    big_point = alias_type("BigPoint", base=named_type("Point"))
    assert big_point.base == NamedType("Point")


# -----------------------------------------------------------------------------
# type_name / is_elementary / is_user_type helpers
# -----------------------------------------------------------------------------


def test_type_name_for_elementary():
    assert type_name(TagType.BOOL) == "BOOL"
    assert type_name(TagType.INT)  == "INT"
    assert type_name(TagType.REAL) == "REAL"


def test_type_name_for_user_types():
    assert type_name(NamedType("Point"))                  == "Point"
    assert type_name(struct_type("P", members=[]))        == "P"
    assert type_name(array_type("A", TagType.INT, [(0, 9)])) == "A"
    assert type_name(enum_type("E", values=["X"]))        == "E"
    assert type_name(alias_type("D", base=TagType.INT))   == "D"


def test_is_elementary_distinguishes_elementary_from_user():
    assert is_elementary(TagType.INT) is True
    assert is_elementary(NamedType("Point")) is False
    assert is_elementary(struct_type("P", members=[])) is False


def test_is_user_type_distinguishes_user_from_elementary():
    assert is_user_type(TagType.INT) is False
    assert is_user_type(NamedType("Point")) is True
    assert is_user_type(struct_type("P", members=[])) is True
    assert is_user_type(array_type("A", TagType.INT, [(0, 9)])) is True
    assert is_user_type(enum_type("E", values=["X"])) is True
    assert is_user_type(alias_type("D", base=TagType.INT)) is True


# -----------------------------------------------------------------------------
# Program-level declaration table
# -----------------------------------------------------------------------------


def test_program_user_types_empty_by_default():
    p = program()
    assert p.user_types == []


def test_program_collects_user_types():
    point = struct_type("Point", members=[
        var("x", TagType.INT),
        var("y", TagType.INT),
    ])
    color = enum_type("Color", values=["RED", "GREEN", "BLUE"])
    p = program(user_types=[point, color])
    assert p.user_types == [point, color]


def test_program_find_user_type_returns_declaration():
    point = struct_type("Point", members=[var("x", TagType.INT)])
    color = enum_type("Color", values=["A", "B"])
    p = program(user_types=[point, color])
    assert p.find_user_type("Point") is point
    assert p.find_user_type("Color") is color
    assert p.find_user_type("Ghost") is None


# -----------------------------------------------------------------------------
# UDTs used as Var types
# -----------------------------------------------------------------------------


def test_var_can_reference_named_user_type():
    """A POU local declared as a Point struct."""
    v = var("origin", named_type("Point"))
    assert v.data_type == NamedType("Point")


def test_var_can_reference_inline_user_type():
    """A POU local declared with an inline struct (rare; usually
    types are declared at program scope)."""
    inline_point = struct_type("Point", members=[var("x", TagType.INT)])
    v = var("origin", inline_point)
    assert v.data_type is inline_point


def test_struct_member_with_initial_value():
    """STRUCT members can carry initial values like other Vars."""
    config = struct_type("Config", members=[
        var("max_speed", TagType.INT, initial="1000"),
        var("enabled",   TagType.BOOL, initial="TRUE"),
    ])
    assert config.members[0].initial_value == "1000"
    assert config.members[1].initial_value == "TRUE"


# -----------------------------------------------------------------------------
# Realistic combination
# -----------------------------------------------------------------------------


def test_realistic_program_with_udts():
    """A program declaring multiple interrelated UDTs."""
    p = program(
        cpu_model="C2-01CPU",
        user_types=[
            enum_type("MachineState",
                      values=["IDLE", "STARTING", "RUNNING", "STOPPING"]),
            struct_type("AxisConfig", members=[
                var("max_velocity", TagType.REAL, initial="100.0"),
                var("max_accel",    TagType.REAL, initial="500.0"),
                var("home_offset",  TagType.INT,  initial="0"),
            ]),
            struct_type("MachineConfig", members=[
                var("axis_x", named_type("AxisConfig")),
                var("axis_y", named_type("AxisConfig")),
                var("axis_z", named_type("AxisConfig")),
                var("state",  named_type("MachineState")),
            ]),
            array_type("RecipeBuffer",
                       element_type=TagType.INT,
                       bounds=[(0, 99)]),
            alias_type("Distance", base=TagType.DINT),
        ],
    )
    # Every UDT is discoverable by name
    assert p.find_user_type("MachineState") is not None
    assert p.find_user_type("AxisConfig").members[0].name == "max_velocity"
    assert p.find_user_type("MachineConfig").members[0].data_type == NamedType("AxisConfig")
    assert p.find_user_type("RecipeBuffer").bounds == ((0, 99),)
    assert p.find_user_type("Distance").base is TagType.DINT
