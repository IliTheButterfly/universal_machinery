"""Tests for user-defined-type (IEC §2.3.3) reader coverage.

The PLCopen XML reader populates ``Program.user_types`` from the
``<dataTypes>`` block and resolves ``<derived name=>`` references
into ``NamedType``.  This file exercises every UDT variant +
``NamedType`` resolution end-to-end via round-trip through the
emitter.
"""
from datetime import datetime, timezone

import pytest

from universal_machinery.builders import (
    alias_type, array_type, enum_type, named_type, prog, program,
    struct_type, subrange_type, var, var_in, var_inout, var_out,
)
from universal_machinery.emitters.plcopen_xml import emit_xml
from universal_machinery.il import (
    AliasType, ArrayType, EnumType, NamedType, StructType, SubrangeType,
    TagType,
)
from universal_machinery.parsers.plcopen_xml import (
    PlcopenParseError, parse_plcopen_xml,
)


_FIXED_TIME = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


def _round_trip(p):
    return parse_plcopen_xml(emit_xml(p, time_now=_FIXED_TIME))


# -----------------------------------------------------------------------------
# Round-trip of each UDT variant
# -----------------------------------------------------------------------------


def test_struct_type_round_trips():
    p = program(user_types=[
        struct_type("Axis", [
            var("pos",   TagType.REAL, comment="metres"),
            var("vel",   TagType.REAL),
            var("flags", TagType.WORD),
        ], comment="motion-axis state"),
    ])
    ut = _round_trip(p).user_types[0]
    assert isinstance(ut, StructType)
    assert ut.name == "Axis"
    assert ut.comment == "motion-axis state"
    assert [m.name for m in ut.members] == ["pos", "vel", "flags"]
    assert ut.members[0].data_type is TagType.REAL
    assert ut.members[0].comment == "metres"


def test_array_type_single_dim_round_trips():
    p = program(user_types=[
        array_type("Buffer8", element_type=TagType.INT, bounds=[(0, 7)]),
    ])
    ut = _round_trip(p).user_types[0]
    assert isinstance(ut, ArrayType)
    assert ut.bounds == ((0, 7),)
    assert ut.element_type is TagType.INT


def test_array_type_multi_dim_round_trips():
    p = program(user_types=[
        array_type("Matrix3x3", element_type=TagType.REAL,
                    bounds=[(0, 2), (0, 2)]),
    ])
    ut = _round_trip(p).user_types[0]
    assert ut.bounds == ((0, 2), (0, 2))


def test_enum_type_round_trips():
    p = program(user_types=[
        enum_type("Color", ["RED", "GREEN", "BLUE", "WHITE"]),
    ])
    ut = _round_trip(p).user_types[0]
    assert isinstance(ut, EnumType)
    assert ut.values == ("RED", "GREEN", "BLUE", "WHITE")


def test_subrange_type_signed_round_trips():
    p = program(user_types=[
        subrange_type("SmallSigned", base=TagType.INT,
                       lower=-100, upper=100),
    ])
    ut = _round_trip(p).user_types[0]
    assert isinstance(ut, SubrangeType)
    assert ut.base is TagType.INT
    assert ut.lower == -100
    assert ut.upper == 100


def test_subrange_type_unsigned_round_trips():
    p = program(user_types=[
        subrange_type("Percent", base=TagType.UINT, lower=0, upper=100),
    ])
    ut = _round_trip(p).user_types[0]
    assert ut.base is TagType.UINT
    assert ut.lower == 0
    assert ut.upper == 100


def test_alias_type_of_elementary_round_trips():
    p = program(user_types=[
        alias_type("Distance", base=TagType.REAL,
                    comment="metres"),
    ])
    ut = _round_trip(p).user_types[0]
    assert isinstance(ut, AliasType)
    assert ut.base is TagType.REAL
    assert ut.comment == "metres"


def test_alias_type_of_named_type_round_trips():
    """Alias-of-UDT: ``Velocity`` is an alias for ``Distance``."""
    p = program(user_types=[
        alias_type("Distance", base=TagType.REAL),
        alias_type("Velocity", base=named_type("Distance")),
    ])
    p2 = _round_trip(p)
    velocity = next(ut for ut in p2.user_types if ut.name == "Velocity")
    assert isinstance(velocity, AliasType)
    assert isinstance(velocity.base, NamedType)
    assert velocity.base.name == "Distance"


# -----------------------------------------------------------------------------
# NamedType references inside POU variable interfaces
# -----------------------------------------------------------------------------


def test_pou_input_with_derived_type_round_trips():
    """``<variable name="a"><type><derived name="Axis"/></type></...>``
    -> ``Var(data_type=NamedType("Axis"))``."""
    p = program(
        user_types=[struct_type("Axis", [var("pos", TagType.REAL)])],
        subroutines=[prog("Main", main=True,
                            inputs=[var_in("a", named_type("Axis"))])],
    )
    sub = _round_trip(p).find_subroutine("Main")
    assert isinstance(sub.inputs[0].data_type, NamedType)
    assert sub.inputs[0].data_type.name == "Axis"


def test_pou_with_all_three_directions_resolving_to_udts():
    p = program(
        user_types=[
            struct_type("Axis", [var("pos", TagType.REAL)]),
            array_type("Buffer", element_type=TagType.INT, bounds=[(0, 9)]),
            enum_type("Mode", ["IDLE", "RUN"]),
        ],
        subroutines=[prog("Main", main=True,
                            inputs=[var_in("axis", named_type("Axis"))],
                            outputs=[var_out("buf", named_type("Buffer"))],
                            in_outs=[var_inout("mode", named_type("Mode"))])],
    )
    sub = _round_trip(p).find_subroutine("Main")
    assert sub.inputs[0].data_type == NamedType("Axis")
    assert sub.outputs[0].data_type == NamedType("Buffer")
    assert sub.in_outs[0].data_type == NamedType("Mode")


# -----------------------------------------------------------------------------
# Struct member referencing another UDT
# -----------------------------------------------------------------------------


def test_struct_member_can_reference_another_named_type():
    """A STRUCT can have a member typed as another UDT."""
    p = program(user_types=[
        enum_type("Mode", ["IDLE", "RUN"]),
        struct_type("System", [
            var("mode", named_type("Mode")),
            var("speed", TagType.INT),
        ]),
    ])
    p2 = _round_trip(p)
    system = next(ut for ut in p2.user_types if ut.name == "System")
    assert isinstance(system, StructType)
    assert system.members[0].data_type == NamedType("Mode")


# -----------------------------------------------------------------------------
# Comments survive the round-trip
# -----------------------------------------------------------------------------


def test_udt_comment_preserved():
    p = program(user_types=[
        struct_type("Axis",
                     [var("pos", TagType.REAL)],
                     comment="motion-control axis state"),
    ])
    ut = _round_trip(p).user_types[0]
    assert ut.comment == "motion-control axis state"


# -----------------------------------------------------------------------------
# Error cases
# -----------------------------------------------------------------------------


def test_derived_type_missing_name_raises():
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="X"/>
  <types><dataTypes/><pous>
    <pou name="P" pouType="program">
      <interface>
        <localVars>
          <variable name="x"><type><derived/></type></variable>
        </localVars>
      </interface>
    </pou>
  </pous></types>
</project>'''
    with pytest.raises(PlcopenParseError, match="<derived> missing required name"):
        parse_plcopen_xml(xml)


def test_dataType_missing_baseType_raises():
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="X"/>
  <types><dataTypes>
    <dataType name="Empty"/>
  </dataTypes><pous/></types>
</project>'''
    with pytest.raises(PlcopenParseError, match="missing required child <baseType>"):
        parse_plcopen_xml(xml)


def test_array_dimension_non_integer_bounds_raises():
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="X"/>
  <types><dataTypes>
    <dataType name="Bad">
      <baseType><array>
        <dimension lower="x" upper="9"/>
        <baseType><INT/></baseType>
      </array></baseType>
    </dataType>
  </dataTypes><pous/></types>
</project>'''
    with pytest.raises(PlcopenParseError, match="non-integer bounds"):
        parse_plcopen_xml(xml)


def test_enum_value_missing_name_raises():
    xml = '''<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="X"/>
  <types><dataTypes>
    <dataType name="BadEnum">
      <baseType><enum><values>
        <value/>
      </values></enum></baseType>
    </dataType>
  </dataTypes><pous/></types>
</project>'''
    with pytest.raises(PlcopenParseError, match="<value> missing required name"):
        parse_plcopen_xml(xml)
