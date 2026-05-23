"""Tests for ``parsers.st_text.parse_program`` -- the full-program
ST parser that closes the ``read(.st)`` gap for the openplc /
rusty backends.

Each test follows the round-trip pattern::

    p_orig = program(...)              # build IL
    src    = emit_program(p_orig)      # emit ST
    p_back = parse_program(src)        # parse back
    # assert p_back captures the same essential shape

Equality on dataclasses works for the simple cases but the
emitter sometimes synthesises VAR_INPUT etc. names that the
parser preserves verbatim -- focus on the structural invariants
(POU count, kinds, var counts, body statement counts) rather
than full ``==``.
"""

from __future__ import annotations

import pytest

from universal_machinery.builders import (
    assign, case_, case_clause, coil, fb, fcall_expr, fn, for_, if_,
    no, prog, program, repeat_, rung, var, var_in, var_inout,
    var_out, while_,
)
from universal_machinery.emitters.st import emit_program
from universal_machinery.il import (
    NamedType, PouKind, Program, TagType, VarDirection,
)
from universal_machinery.parsers.st_text import (
    StParseError, parse_program,
)


# -----------------------------------------------------------------------------
# Headline round-trip: PROGRAM POU with a local var and an ST body
# -----------------------------------------------------------------------------


def test_minimal_program_round_trips():
    """The smallest meaningful program: one PROGRAM POU with a
    local var and a one-line ST assignment."""
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("x", TagType.INT)],
             st_body=[assign("x", 1)]),
    ])
    src = emit_program(p)
    p_back = parse_program(src)
    assert isinstance(p_back, Program)
    assert len(p_back.subroutines) == 1
    s = p_back.subroutines[0]
    assert s.name == "Main"
    assert s.kind is PouKind.PROGRAM
    assert s.main is True
    assert len(s.local_vars) == 1
    assert s.local_vars[0].name == "x"
    assert s.local_vars[0].data_type is TagType.INT
    assert s.local_vars[0].direction is VarDirection.LOCAL
    assert s.st_body is not None
    assert len(s.st_body) == 1


def test_program_with_all_four_var_directions_round_trips():
    """A PROGRAM with VAR_INPUT, VAR_OUTPUT, VAR_IN_OUT, VAR
    blocks all round-trip into the right Subroutine fields."""
    p = program(subroutines=[
        prog("Main", main=True,
             inputs=[var_in("a", TagType.INT)],
             outputs=[var_out("b", TagType.INT)],
             in_outs=[var_inout("c", TagType.INT)],
             local_vars=[var("d", TagType.BOOL)],
             st_body=[assign("b", "a")]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert [v.name for v in s.inputs] == ["a"]
    assert [v.name for v in s.outputs] == ["b"]
    assert [v.name for v in s.in_outs] == ["c"]
    assert [v.name for v in s.local_vars] == ["d"]
    assert s.local_vars[0].data_type is TagType.BOOL


# -----------------------------------------------------------------------------
# FUNCTION POU
# -----------------------------------------------------------------------------


def test_function_pou_with_return_type_round_trips():
    """``FUNCTION Name : ReturnType ... END_FUNCTION`` -- the
    return type appears after the name, before any VAR blocks."""
    p = program(subroutines=[
        fn("Doubled",
           return_type=TagType.INT,
           inputs=[var_in("x", TagType.INT)],
           st_body=[assign("Doubled", "x")]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert s.name == "Doubled"
    assert s.kind is PouKind.FUNCTION
    assert s.return_type is TagType.INT
    assert [v.name for v in s.inputs] == ["x"]


# -----------------------------------------------------------------------------
# FUNCTION_BLOCK POU + NamedType for instance / UDT references
# -----------------------------------------------------------------------------


def test_function_block_pou_round_trips():
    """``FUNCTION_BLOCK Name ... END_FUNCTION_BLOCK`` -- the
    keyword matches the IL's ``PouKind.FUNCTION_BLOCK``."""
    p = program(subroutines=[
        fb("Average",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("avg", TagType.INT)],
           st_body=[assign("avg", "a")]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert s.name == "Average"
    assert s.kind is PouKind.FUNCTION_BLOCK
    assert [v.name for v in s.inputs] == ["a", "b"]
    assert [v.name for v in s.outputs] == ["avg"]


def test_named_type_for_fb_instance_round_trips():
    """``t1 : TON;`` parses ``TON`` as a NamedType reference
    (not an elementary type), preserving the FB instance shape."""
    from universal_machinery.il.ast import Var
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[
                 var("trigger", TagType.BOOL),
                 Var(name="t1", data_type=NamedType("TON"),
                     direction=VarDirection.LOCAL),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    t1 = next(v for v in s.local_vars if v.name == "t1")
    assert isinstance(t1.data_type, NamedType)
    assert t1.data_type.name == "TON"


# -----------------------------------------------------------------------------
# ST control-flow bodies (statement parser is already battle-tested;
# pin that POU-level parsing routes them correctly)
# -----------------------------------------------------------------------------


def test_program_with_if_body_round_trips():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[
                 var("hot", TagType.BOOL),
                 var("zone", TagType.INT),
             ],
             st_body=[
                 if_(("hot", [assign("zone", 1)]),
                      else_=[assign("zone", 0)]),
             ]),
    ])
    p_back = parse_program(emit_program(p))
    assert len(p_back.subroutines[0].st_body or []) == 1


def test_program_with_for_loop_round_trips():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[
                 var("i", TagType.INT),
                 var("total", TagType.INT),
             ],
             st_body=[
                 for_("i", 0, 9, [assign("total", "total")]),
             ]),
    ])
    p_back = parse_program(emit_program(p))
    assert len(p_back.subroutines[0].st_body or []) == 1


def test_program_with_while_repeat_case_round_trip():
    """Three different control-flow constructs in one body --
    pins that each ends cleanly so the POU's END_PROGRAM is
    reached without confusing the parser."""
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[
                 var("flag", TagType.BOOL),
                 var("counter", TagType.INT),
                 var("mode", TagType.INT),
             ],
             st_body=[
                 while_("flag", [assign("counter", "counter")]),
                 repeat_([assign("counter", 5)], until="flag"),
                 case_("mode",
                       case_clause([0], [assign("counter", 100)]),
                       case_clause([1], [assign("counter", 200)]),
                       else_=[assign("counter", 999)]),
             ]),
    ])
    p_back = parse_program(emit_program(p))
    assert len(p_back.subroutines[0].st_body or []) == 3


# -----------------------------------------------------------------------------
# Multiple POUs in one source
# -----------------------------------------------------------------------------


def test_multiple_pous_in_one_source_round_trip():
    """``FUNCTION ... END_FUNCTION`` followed by ``PROGRAM ...
    END_PROGRAM`` -- both must show up in the parsed Program."""
    p = program(subroutines=[
        fn("Doubled",
           return_type=TagType.INT,
           inputs=[var_in("x", TagType.INT)],
           st_body=[assign("Doubled", "x")]),
        prog("Main", main=True,
             local_vars=[
                 var("a", TagType.INT),
                 var("r", TagType.INT),
             ],
             st_body=[assign("r", fcall_expr("Doubled", "a"))]),
    ])
    p_back = parse_program(emit_program(p))
    assert sorted(s.name for s in p_back.subroutines) == \
        ["Doubled", "Main"]
    # The function call inside the PROGRAM body should reach back
    # to the FUNCTION's name; parser just needs to preserve the
    # call expression's literal name.
    main = next(s for s in p_back.subroutines if s.name == "Main")
    # Body has one assignment with a FunctionCallExpr on the RHS.
    assert len(main.st_body) == 1


# -----------------------------------------------------------------------------
# Initial values
# -----------------------------------------------------------------------------


def test_var_with_initial_value_preserves_textual_init():
    """``count : INT := 10;`` -- the initial value comes through
    as the raw textual snippet (matches how the emitter writes it
    and how the IL stores it)."""
    from universal_machinery.il.ast import Var
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[
                 Var(name="count", data_type=TagType.INT,
                     direction=VarDirection.LOCAL,
                     initial_value="10"),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert s.local_vars[0].initial_value == "10"


# -----------------------------------------------------------------------------
# Out-of-scope shapes raise StParseError with a clear pointer
# -----------------------------------------------------------------------------


def test_at_clause_with_iec_direct_rep_round_trips():
    """v2 parses IEC §2.4.1.1 ``name AT %IX0.0 : BOOL;`` -- the
    ``%``-prefixed direct-rep token is tokenized as a
    ``DIRECT_REP`` and the parser builds an ``Address`` from
    it.  Round-trip pinned."""
    from universal_machinery.il.ast import Address, Var, VarDirection
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[
                 Var(name="in1", data_type=TagType.BOOL,
                     direction=VarDirection.LOCAL,
                     address=Address("%IX0.0")),
                 Var(name="out1", data_type=TagType.BOOL,
                     direction=VarDirection.LOCAL,
                     address=Address("%QX0.0")),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    addrs = {v.name: v.address for v in s.local_vars}
    assert addrs["in1"] == Address("%IX0.0")
    assert addrs["out1"] == Address("%QX0.0")


def test_at_clause_with_vendor_address_unchanged_no_AT_keyword():
    """Vendor-style addresses (CLICK ``X001`` etc.) aren't IEC
    direct rep -- the ST emitter renders them as trailing
    ``(* AT X001 *)`` comments, never as inline ``AT X001``.
    A literal inline ``AT X001`` doesn't appear in real emit
    output, so parser rejection of that hypothetical shape is
    fine and stays an error -- it would mean the user
    hand-authored a non-IEC AT clause."""
    src = """\
PROGRAM Main
VAR
    lamp AT X001 : BOOL;
END_VAR
END_PROGRAM
"""
    # Without ``%``, the tokenizer reads ``X001`` as an IDENT;
    # the parser expects DIRECT_REP after AT and raises.
    with pytest.raises(StParseError, match="DIRECT_REP|direct-rep"):
        parse_program(src)


def test_type_block_with_alias_round_trips():
    """v3 parses TYPE blocks.  A simple ALIAS round-trips into
    ``Program.user_types`` (no longer raises)."""
    from universal_machinery.il.types import AliasType
    src = """\
TYPE
    Speed : REAL;
END_TYPE

PROGRAM Main
END_PROGRAM
"""
    p_back = parse_program(src)
    assert len(p_back.user_types) == 1
    assert isinstance(p_back.user_types[0], AliasType)
    assert p_back.user_types[0].name == "Speed"


def test_type_block_with_all_five_variants_round_trips():
    """All five IEC §2.3.3 UDT variants -- STRUCT / ARRAY /
    ENUM / SUBRANGE / ALIAS -- emit and re-parse to equal
    dataclasses.  Pin via direct ``==`` (frozen dataclasses)."""
    from universal_machinery.builders import (
        alias_type, array_type, enum_type, struct_type, subrange_type,
    )
    from universal_machinery.il.ast import Var, VarDirection
    udts = [
        struct_type("Point", [
            Var(name="x", data_type=TagType.INT,
                direction=VarDirection.LOCAL),
            Var(name="y", data_type=TagType.INT,
                direction=VarDirection.LOCAL),
        ]),
        array_type("Vec10", TagType.INT, [(0, 9)]),
        array_type("Matrix3x3", TagType.REAL, [(0, 2), (0, 2)]),
        enum_type("Color", ["RED", "GREEN", "BLUE"]),
        subrange_type("Pct", TagType.INT, 0, 100),
        subrange_type("SignedRange", TagType.INT, -100, 100),
        alias_type("Speed", TagType.REAL),
    ]
    p = program(user_types=udts,
                  subroutines=[prog("Main", main=True, st_body=[])])
    p_back = parse_program(emit_program(p))
    assert len(p_back.user_types) == len(udts)
    for original, parsed in zip(udts, p_back.user_types):
        assert original == parsed, (
            f"{type(original).__name__} mismatch: "
            f"emit/parse round-trip lost information"
        )


def test_configuration_block_raises_with_clear_message():
    """v1 doesn't parse CONFIGURATION ... END_CONFIGURATION."""
    src = """\
PROGRAM Main
END_PROGRAM

CONFIGURATION Plant
END_CONFIGURATION
"""
    with pytest.raises(StParseError, match="CONFIGURATION.*not yet"):
        parse_program(src)


def test_var_external_block_round_trips():
    """v2 parses VAR_EXTERNAL blocks -- vars go into
    ``Subroutine.external_vars`` and carry
    ``VarDirection.EXTERNAL``."""
    from universal_machinery.il.ast import Var, VarDirection
    p = program(subroutines=[
        prog("Main", main=True,
             external_vars=[
                 Var(name="LED", data_type=TagType.BOOL,
                     direction=VarDirection.EXTERNAL),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert [v.name for v in s.external_vars] == ["LED"]
    assert s.external_vars[0].direction is VarDirection.EXTERNAL


def test_var_temp_block_round_trips():
    """v2 parses VAR_TEMP blocks."""
    from universal_machinery.il.ast import Var, VarDirection
    p = program(subroutines=[
        prog("Main", main=True,
             temp_vars=[
                 Var(name="tmp", data_type=TagType.REAL,
                     direction=VarDirection.TEMP),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert [v.name for v in s.temp_vars] == ["tmp"]
    assert s.temp_vars[0].direction is VarDirection.TEMP


def test_var_global_block_round_trips():
    """v2 parses POU-scope VAR_GLOBAL blocks (rare in practice
    but legal IEC -- emitted from
    ``Subroutine.global_vars``)."""
    from universal_machinery.il.ast import Var, VarDirection
    p = program(subroutines=[
        prog("Main", main=True,
             global_vars=[
                 Var(name="SETPOINT", data_type=TagType.INT,
                     direction=VarDirection.LOCAL),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert [v.name for v in s.global_vars] == ["SETPOINT"]


def test_full_seven_var_directions_round_trip():
    """All seven IEC §2.4.3 VAR_* blocks in one POU.  Pins that
    they all route to the right Subroutine field and don't
    interact (e.g. ``LED`` in VAR_EXTERNAL doesn't leak into
    ``local_vars``)."""
    from universal_machinery.il.ast import Var, VarDirection
    p = program(subroutines=[
        prog("Main", main=True,
             inputs=[var_in("a", TagType.INT)],
             outputs=[var_out("b", TagType.INT)],
             in_outs=[var_inout("c", TagType.INT)],
             local_vars=[var("d", TagType.BOOL)],
             external_vars=[
                 Var(name="LED", data_type=TagType.BOOL,
                     direction=VarDirection.EXTERNAL),
             ],
             temp_vars=[
                 Var(name="scratch", data_type=TagType.INT,
                     direction=VarDirection.TEMP),
             ],
             global_vars=[
                 Var(name="STATE", data_type=TagType.INT,
                     direction=VarDirection.LOCAL),
             ],
             st_body=[]),
    ])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert [v.name for v in s.inputs]         == ["a"]
    assert [v.name for v in s.outputs]        == ["b"]
    assert [v.name for v in s.in_outs]        == ["c"]
    assert [v.name for v in s.local_vars]     == ["d"]
    assert [v.name for v in s.external_vars]  == ["LED"]
    assert [v.name for v in s.temp_vars]      == ["scratch"]
    assert [v.name for v in s.global_vars]    == ["STATE"]


def test_empty_input_returns_empty_program():
    """Empty source / whitespace-only is valid -- returns a
    ``Program`` with no subroutines.  Symmetric with
    ``parse_st_body`` (which returns ``[]`` for empty input)."""
    p_back = parse_program("")
    assert isinstance(p_back, Program)
    assert p_back.subroutines == []


def test_garbage_at_top_level_raises():
    """Stray identifiers at program scope must error out cleanly."""
    with pytest.raises(StParseError, match="expected POU keyword"):
        parse_program("FoobarNonsense")
