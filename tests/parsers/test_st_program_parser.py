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


def test_configuration_block_minimal_round_trips():
    """v4 parses CONFIGURATION blocks.  Minimal: just the
    name + no resources / vars."""
    src = """\
PROGRAM Main
END_PROGRAM

CONFIGURATION Plant
END_CONFIGURATION
"""
    p_back = parse_program(src)
    assert len(p_back.configurations) == 1
    assert p_back.configurations[0].name == "Plant"


def test_configuration_with_resource_task_pou_instance_round_trips():
    """A representative CONFIGURATION: VAR_GLOBAL / VAR_ACCESS /
    VAR_CONFIG / RESOURCE (with its own globals + tasks + POU
    instances) all round-trip via the parser."""
    from universal_machinery.builders import (
        access_var, config_var, configuration, pou_instance,
        resource, task_spec,
    )
    from universal_machinery.il.ast import Var, VarDirection
    p = program(
        subroutines=[prog("Main", main=False, st_body=[])],
        configurations=[
            configuration("Plant",
                access_vars=[access_var(
                    alias="LAMP_HMI",
                    instance_path="PLC1.MainInst.lamp",
                    type_=TagType.BOOL,
                    direction="READ_WRITE",
                )],
                config_vars=[config_var(
                    instance_path="PLC1.MainInst.lamp",
                    type_=TagType.BOOL,
                    initial="FALSE",
                )],
                resources=[resource("PLC1",
                    tasks=[task_spec(
                        "Fast", interval="T#100ms", priority=1,
                    )],
                    pou_instances=[pou_instance(
                        "MainInst", type_name="Main", task="Fast",
                    )],
                    global_vars=[Var(name="G",
                                       data_type=TagType.INT,
                                       direction=VarDirection.LOCAL)],
                )],
                global_vars=[Var(name="CFG_G",
                                   data_type=TagType.INT,
                                   direction=VarDirection.LOCAL)],
            ),
        ],
    )
    p_back = parse_program(emit_program(p))
    cfg = p_back.configurations[0]
    assert cfg.name == "Plant"
    assert [v.name for v in cfg.global_vars] == ["CFG_G"]
    assert len(cfg.access_vars) == 1
    av = cfg.access_vars[0]
    assert av.alias == "LAMP_HMI"
    assert av.instance_path == "PLC1.MainInst.lamp"
    assert av.direction == "READ_WRITE"
    assert len(cfg.config_vars) == 1
    cv = cfg.config_vars[0]
    assert cv.instance_path == "PLC1.MainInst.lamp"
    assert cv.initial_value == "FALSE"
    r = cfg.resources[0]
    assert r.name == "PLC1"
    assert [t.name for t in r.tasks] == ["Fast"]
    assert r.tasks[0].interval == "T#100ms"
    assert r.tasks[0].priority == 1
    pi = r.pou_instances[0]
    assert pi.name == "MainInst"
    assert pi.type_name == "Main"
    assert pi.task == "Fast"
    assert [v.name for v in r.global_vars] == ["G"]


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


def test_interface_block_round_trips():
    """v5 parses INTERFACE blocks -- methods land on
    ``Program.interfaces`` and every method is abstract per
    IEC 3rd-edition rules."""
    from universal_machinery.il.oop import AccessSpec, Interface, Method
    from universal_machinery.il.ast import Var, VarDirection
    p = program(
        subroutines=[],
        interfaces=[
            Interface(
                name="IMotor",
                methods=[
                    Method(
                        name="Start",
                        abstract=True,
                        access=AccessSpec.PUBLIC,
                        inputs=[Var(name="speed",
                                       data_type=TagType.INT,
                                       direction=VarDirection.INPUT)],
                    ),
                    Method(
                        name="Stop",
                        abstract=True,
                        access=AccessSpec.PUBLIC,
                    ),
                ],
            ),
        ],
    )
    p_back = parse_program(emit_program(p))
    assert [i.name for i in p_back.interfaces] == ["IMotor"]
    methods = p_back.interfaces[0].methods
    assert [m.name for m in methods] == ["Start", "Stop"]
    assert all(m.abstract for m in methods)
    assert [v.name for v in methods[0].inputs] == ["speed"]


def test_function_block_with_methods_round_trips():
    """v5 parses METHOD blocks inside FUNCTION_BLOCK -- methods
    land on ``Subroutine.methods`` with access spec, abstract
    flag, optional return type and body."""
    from universal_machinery.il.oop import AccessSpec, Method
    from universal_machinery.il.ast import Subroutine, Var, VarDirection

    fb_sub = Subroutine(
        name="Counter",
        kind=PouKind.FUNCTION_BLOCK,
        local_vars=[Var(name="n", data_type=TagType.INT,
                          direction=VarDirection.LOCAL)],
        methods=[
            Method(
                name="Inc",
                access=AccessSpec.PUBLIC,
                st_body=[assign("n", "1")],
            ),
            Method(
                name="Get",
                access=AccessSpec.PROTECTED,
                return_type=TagType.INT,
                st_body=[assign("Get", "n")],
            ),
        ],
    )
    p = program(subroutines=[fb_sub])
    p_back = parse_program(emit_program(p))
    s = p_back.subroutines[0]
    assert s.name == "Counter"
    assert s.kind is PouKind.FUNCTION_BLOCK
    assert [m.name for m in s.methods] == ["Inc", "Get"]
    assert s.methods[0].access is AccessSpec.PUBLIC
    assert s.methods[1].access is AccessSpec.PROTECTED
    assert s.methods[1].return_type is TagType.INT


def test_function_block_extends_implements_round_trips():
    """v5 parses FB EXTENDS / IMPLEMENTS / ABSTRACT modifiers --
    populates ``Subroutine.extends`` / ``.implements`` /
    ``.abstract``."""
    from universal_machinery.il.oop import AccessSpec, Interface, Method
    from universal_machinery.il.ast import Subroutine, Var, VarDirection

    base = Subroutine(
        name="BaseMotor",
        kind=PouKind.FUNCTION_BLOCK,
        abstract=True,
        local_vars=[Var(name="rpm", data_type=TagType.INT,
                          direction=VarDirection.LOCAL)],
        methods=[
            Method(name="Cycle",
                     abstract=True,
                     access=AccessSpec.PROTECTED,
                     return_type=TagType.INT),
        ],
    )
    derived = Subroutine(
        name="DcMotor",
        kind=PouKind.FUNCTION_BLOCK,
        extends="BaseMotor",
        implements=["IMotor"],
        inputs=[Var(name="enable", data_type=TagType.BOOL,
                      direction=VarDirection.INPUT)],
    )
    p = program(
        subroutines=[base, derived],
        interfaces=[Interface(name="IMotor", methods=[
            Method(name="Start", abstract=True,
                     access=AccessSpec.PUBLIC),
        ])],
    )
    p_back = parse_program(emit_program(p))
    by_name = {s.name: s for s in p_back.subroutines}
    assert by_name["BaseMotor"].abstract is True
    assert by_name["BaseMotor"].extends is None
    assert by_name["BaseMotor"].implements == []
    assert by_name["DcMotor"].abstract is False
    assert by_name["DcMotor"].extends == "BaseMotor"
    assert by_name["DcMotor"].implements == ["IMotor"]


def test_method_with_override_round_trips():
    """v5 parses METHOD OVERRIDE on a derived FB."""
    from universal_machinery.il.oop import AccessSpec, Method
    from universal_machinery.il.ast import Subroutine

    derived = Subroutine(
        name="DcMotor",
        kind=PouKind.FUNCTION_BLOCK,
        extends="BaseMotor",
        methods=[
            Method(
                name="Cycle",
                access=AccessSpec.PUBLIC,
                override=True,
                return_type=TagType.INT,
                st_body=[assign("Cycle", "0")],
            ),
        ],
    )
    p_back = parse_program(emit_program(program(subroutines=[derived])))
    m = p_back.subroutines[0].methods[0]
    assert m.name == "Cycle"
    assert m.override is True
    assert m.return_type is TagType.INT


def test_sfc_body_minimal_round_trips():
    """v6 parses IEC §6.7 SFC text bodies -- POUs whose body
    opens with INITIAL_STEP / STEP route to ``Subroutine.sfc``
    instead of ``Subroutine.st_body``."""
    from universal_machinery.il.sfc import (
        Action, SfcNetwork, Step, Transition,
    )
    from universal_machinery.il.ops import ContactNO
    from universal_machinery.il import TagRef
    sfc = SfcNetwork(
        steps=[
            Step("Init", initial=True,
                  actions=(Action(qualifier="N", target="run_pou"),)),
            Step("Done"),
        ],
        transitions=[
            Transition(from_steps=("Init",), to_steps=("Done",),
                         condition=(ContactNO(TagRef(name="finished")),)),
        ],
    )
    p = program(subroutines=[prog("Seq", main=False, sfc=sfc)])
    p_back = parse_program(emit_program(p))
    sub = p_back.subroutines[0]
    assert sub.sfc is not None
    assert sub.st_body is None
    assert [s.name for s in sub.sfc.steps] == ["Init", "Done"]
    assert [s.name for s in sub.sfc.steps if s.initial] == ["Init"]
    assert sub.sfc.steps[0].actions[0].qualifier == "N"
    assert sub.sfc.steps[0].actions[0].target == "run_pou"
    assert len(sub.sfc.transitions) == 1
    t = sub.sfc.transitions[0]
    assert t.from_steps == ("Init",)
    assert t.to_steps == ("Done",)
    assert isinstance(t.condition[0], ContactNO)


def test_sfc_body_time_qualified_action_round_trips():
    """``L`` / ``D`` / ``SD`` etc. carry a ``T#<int>ms`` time
    literal; v6 parses it back into ``Action.time_ms``."""
    from universal_machinery.il.sfc import (
        Action, SfcNetwork, Step, Transition,
    )
    from universal_machinery.il.ops import ContactNO
    from universal_machinery.il import TagRef
    sfc = SfcNetwork(
        steps=[
            Step("S1", initial=True,
                  actions=(Action(qualifier="L", target="dwell",
                                       time_ms=750),)),
            Step("S2"),
        ],
        transitions=[
            Transition(from_steps=("S1",), to_steps=("S2",),
                         condition=(ContactNO(TagRef(name="go")),)),
        ],
    )
    p_back = parse_program(
        emit_program(program(subroutines=[prog("T", main=False, sfc=sfc)]))
    )
    a = p_back.subroutines[0].sfc.steps[0].actions[0]
    assert a.qualifier == "L"
    assert a.target == "dwell"
    assert a.time_ms == 750


def test_sfc_body_inline_action_round_trips():
    """v6 reverses the emitter's synthesised ACTION block: action
    targets that match an ``ACTION ... END_ACTION`` block get
    their body moved onto ``Action.inline_body`` with the target
    cleared."""
    from universal_machinery.il.sfc import (
        Action, SfcNetwork, Step, Transition,
    )
    from universal_machinery.il.ops import ContactNO
    from universal_machinery.il import TagRef
    sfc = SfcNetwork(
        steps=[
            Step("Init", initial=True, actions=(
                Action(qualifier="N", inline_body=(assign("y", "TRUE"),)),
            )),
            Step("Done"),
        ],
        transitions=[
            Transition(from_steps=("Init",), to_steps=("Done",),
                         condition=(ContactNO(TagRef(name="ready")),)),
        ],
    )
    p_back = parse_program(
        emit_program(program(subroutines=[prog("S", main=False, sfc=sfc)]))
    )
    a = p_back.subroutines[0].sfc.steps[0].actions[0]
    assert a.target == ""
    assert len(a.inline_body) == 1
    # body is a single Assignment of y := TRUE
    stmt = a.inline_body[0]
    assert stmt.__class__.__name__ == "Assignment"


def test_sfc_body_condition_with_not_and_or_round_trips():
    """v6 lowers the boolean AND / OR / NOT subset of the
    transition condition expression to ``ContactNO`` /
    ``ContactNC`` / ``ParallelGroup`` IL ops."""
    from universal_machinery.il.sfc import (
        SfcNetwork, Step, Transition,
    )
    from universal_machinery.il.ops import (
        ContactNO, ContactNC, ParallelGroup,
    )
    from universal_machinery.il import TagRef
    sfc = SfcNetwork(
        steps=[Step("A", initial=True), Step("B"), Step("C")],
        transitions=[
            Transition(from_steps=("A",), to_steps=("B",),
                         condition=(ContactNC(TagRef(name="halt")),
                                       ContactNO(TagRef(name="ok")))),
            Transition(from_steps=("B",), to_steps=("C",),
                         condition=(ParallelGroup(branches=(
                             (ContactNO(TagRef(name="x")),),
                             (ContactNO(TagRef(name="y")),),
                         )),)),
        ],
    )
    p_back = parse_program(
        emit_program(program(subroutines=[prog("Q", main=False, sfc=sfc)]))
    )
    sfc_back = p_back.subroutines[0].sfc
    t1, t2 = sfc_back.transitions
    assert isinstance(t1.condition[0], ContactNC)
    assert isinstance(t1.condition[1], ContactNO)
    assert isinstance(t2.condition[0], ParallelGroup)


def test_sfc_body_simultaneous_divergence_round_trips():
    """Multi-step ``FROM (a, b) TO (c, d)`` transitions round-trip
    via parenthesised step lists."""
    from universal_machinery.il.sfc import (
        SfcNetwork, Step, Transition,
    )
    from universal_machinery.il.ops import ContactNO
    from universal_machinery.il import TagRef
    sfc = SfcNetwork(
        steps=[
            Step("Start", initial=True),
            Step("Left"), Step("Right"), Step("Join"),
        ],
        transitions=[
            Transition(from_steps=("Start",),
                         to_steps=("Left", "Right"),
                         condition=(ContactNO(TagRef(name="split")),)),
            Transition(from_steps=("Left", "Right"),
                         to_steps=("Join",),
                         condition=(ContactNO(TagRef(name="merge")),)),
        ],
    )
    p_back = parse_program(
        emit_program(program(subroutines=[prog("P", main=False, sfc=sfc)]))
    )
    transitions = p_back.subroutines[0].sfc.transitions
    assert transitions[0].from_steps == ("Start",)
    assert transitions[0].to_steps == ("Left", "Right")
    assert transitions[1].from_steps == ("Left", "Right")
    assert transitions[1].to_steps == ("Join",)


def test_class_at_top_level_still_out_of_scope():
    """v5 accepts FB-level OOP but class-level CLASS / EXTENDS
    are still out of scope -- they should raise StParseError."""
    with pytest.raises(StParseError, match="expected POU keyword"):
        parse_program("CLASS Foo\nEND_CLASS\n")


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
