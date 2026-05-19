"""Tests for the builder DSL (``universal_machinery.builders``).

The DSL wraps the raw IL dataclasses with short, IEC-flavoured helpers
and smart string coercion (CLICK-style addresses -> Address, other
strings -> TagRef).  The IL produced is structurally identical to
what raw dataclass construction would yield.
"""
import pytest

from universal_machinery.il import (
    Address, DataBlock, PouKind, Program, Rung, Subroutine, Tag, TagRef,
    TagType, Var, VarDirection,
)
from universal_machinery.il.ops import (
    BinaryMath, Call, Compare, ContactNC, ContactNO, End, Jump, Label,
    Move, OutCoil, OutReset, OutSet, ParallelGroup, Return, TON,
)
from universal_machinery.builders import (
    add, call, coil, ctu, data_block, div, end, eq, fb, fedge, fn, ge, gt,
    jump, label_, le, loc, lt, mod, move, mul, nc, ne, no, parallel, prog,
    program, redge, reset_, ret, rung, set_, sub, subroutine, tag, tag_decl,
    ton, var, var_in, var_inout, var_out,
)


# -----------------------------------------------------------------------------
# String coercion
# -----------------------------------------------------------------------------


def test_address_pattern_strings_become_addresses():
    """Strings matching ``[A-Z]+\\d+`` coerce to Address."""
    r = rung(no("X001"), nc("DS9000"), coil("Y005"))
    assert r.ops == [
        ContactNO(Address("X001")),
        ContactNC(Address("DS9000")),
        OutCoil(Address("Y005")),
    ]


def test_non_address_strings_become_tagrefs():
    """Strings that don't match the address pattern coerce to TagRef."""
    r = rung(no("start_btn"), coil("running_lamp"))
    assert r.ops == [
        ContactNO(TagRef("start_btn")),
        OutCoil(TagRef("running_lamp")),
    ]


def test_explicit_tag_helper_overrides_smart_classifier():
    """User has a custom symbolic tag named 'X001' (legal but rare).
    ``tag("X001")`` forces TagRef construction."""
    r = rung(no(tag("X001")))
    assert r.ops == [ContactNO(TagRef("X001"))]


def test_explicit_loc_helper_forces_address():
    """Symmetric: ``loc(...)`` forces Address even for non-pattern-matching
    strings.  Useful for non-standard vendor address formats."""
    addr = loc("Custom.0")
    assert addr == Address("Custom.0")


def test_iec_direct_representation_recognised_as_address():
    """IEC §2.4.1.1 direct-representation syntax (``%I0.0``, ``%QB5``,
    ``%MW10``) coerces to ``Address``, not ``TagRef``."""
    r = rung(no("%I0.0"),                 # input bit, default X size
             nc("%IX0.7"),                # explicit X size
             coil("%Q5.7"))               # output bit
    assert r.ops == [
        ContactNO(Address("%I0.0")),
        ContactNC(Address("%IX0.7")),
        OutCoil(Address("%Q5.7")),
    ]


def test_iec_direct_rep_byte_word_dword_lword_sizes():
    """All five size prefixes (X/B/W/D/L) over all three location
    families (%I, %Q, %M) coerce to Address."""
    cases = [
        "%IX0.0", "%IB0", "%IW0", "%ID0", "%IL0",
        "%QX0.0", "%QB0", "%QW0", "%QD0", "%QL0",
        "%MX0",   "%MB0", "%MW0", "%MD0", "%ML0",
    ]
    for s in cases:
        assert loc(s) == Address(s), f"{s!r} should coerce to Address"


def test_iec_direct_rep_hierarchical_address():
    """IEC allows dot-separated hierarchical indices for nested I/O
    modules: ``%I0.0.0`` (slot 0, channel 0, bit 0)."""
    assert loc("%I0.0.0") == Address("%I0.0.0")
    assert loc("%QW2.4.1") == Address("%QW2.4.1")


def test_iec_direct_rep_in_value_position():
    """``Compare(lhs=%I0.0, rhs=...)`` -- direct-rep as a Value
    operand, not just a Loc."""
    op = eq("%I0.0", "%I0.1")
    assert op.lhs == Address("%I0.0")
    assert op.rhs == Address("%I0.1")


def test_invalid_iec_prefix_becomes_tagref():
    """Only %I / %Q / %M are valid IEC location prefixes.  Other
    ``%``-prefixed strings should classify as ``TagRef`` so a typo
    doesn't silently become a fake Address."""
    op = no("%Z0.0")        # Z isn't a valid IEC location prefix
    assert op == ContactNO(TagRef("%Z0.0"))


def test_iec_direct_rep_requires_index():
    """``%I`` alone -- no index -- is not a valid direct-rep address;
    classify as TagRef."""
    op = no("%I")
    assert op == ContactNO(TagRef("%I"))


def test_pre_built_address_and_tagref_pass_through():
    """Already-built Address / TagRef pass through unchanged."""
    a = Address("DS500")
    t = TagRef("speed")
    r = rung(no(a), coil(t))
    assert r.ops == [ContactNO(a), OutCoil(t)]


# -----------------------------------------------------------------------------
# Value coercion (Address | TagRef | str literal | numeric)
# -----------------------------------------------------------------------------


def test_compare_with_numeric_literal():
    """Numeric literals (int / float) become string operands."""
    c = gt("speed", 100)
    assert c == Compare(op=">", lhs=TagRef("speed"), rhs="100")


def test_compare_with_float_literal():
    c = lt("temperature", 1.5)
    assert c == Compare(op="<", lhs=TagRef("temperature"), rhs="1.5")


def test_move_with_numeric_literal_source():
    m = move(42, "result")
    assert m == Move(src="42", dst=TagRef("result"))


# -----------------------------------------------------------------------------
# Contact / coil shortcuts
# -----------------------------------------------------------------------------


def test_all_contact_shortcuts():
    r = rung(no("X1"), nc("X2"), redge("X3"), fedge("X4"))
    assert isinstance(r.ops[0], ContactNO)
    assert isinstance(r.ops[1], ContactNC)
    assert r.ops[2].address == Address("X3")
    assert r.ops[3].address == Address("X4")


def test_all_coil_shortcuts():
    r = rung(coil("Y1"), set_("Y2"), reset_("Y3"))
    assert isinstance(r.ops[0], OutCoil)
    assert isinstance(r.ops[1], OutSet)
    assert isinstance(r.ops[2], OutReset)


# -----------------------------------------------------------------------------
# Timers
# -----------------------------------------------------------------------------


def test_timer_shortcuts():
    t = ton("T0", 1000, done_bit="C100")
    assert t == TON(
        address=Address("T0"), preset_ms=1000,
        done_bit=Address("C100"),
    )


# -----------------------------------------------------------------------------
# Compare shortcuts
# -----------------------------------------------------------------------------


def test_compare_helpers_cover_all_operators():
    assert eq("a", "b").op == "=="
    assert ne("a", "b").op == "!="
    assert lt("a", "b").op == "<"
    assert le("a", "b").op == "<="
    assert gt("a", "b").op == ">"
    assert ge("a", "b").op == ">="


# -----------------------------------------------------------------------------
# Math / move
# -----------------------------------------------------------------------------


def test_math_helpers_cover_operators():
    assert add("a", "b", "c").op == "+"
    assert sub("a", "b", "c").op == "-"
    assert mul("a", "b", "c").op == "*"
    assert div("a", "b", "c").op == "/"
    assert mod("a", "b", "c").op == "%"


def test_move_helper():
    m = move("DS10", "DS20")
    assert m == Move(src=Address("DS10"), dst=Address("DS20"))


# -----------------------------------------------------------------------------
# Control flow
# -----------------------------------------------------------------------------


def test_call_bare():
    c = call("Sub1")
    assert c == Call(target="Sub1")


def test_call_parameterized():
    c = call("Avg",
             inputs=[("a", "DS10"), ("b", 5)],
             return_to="DS20")
    assert c == Call(
        target="Avg",
        inputs=(("a", Address("DS10")), ("b", "5")),
        return_to=Address("DS20"),
    )


def test_call_function_block():
    c = call("PID",
             instance="DB100",
             inputs=[("sp", "DS50"), ("pv", "DS51")],
             outputs=[("out", "DS52")])
    assert c == Call(
        target="PID",
        instance=Address("DB100"),
        inputs=(("sp", Address("DS50")), ("pv", Address("DS51"))),
        outputs=(("out", Address("DS52")),),
    )


def test_control_flow_misc():
    assert ret() == Return()
    assert end() == End()
    assert jump("loop_top") == Jump(label="loop_top")
    assert label_("loop_top") == Label(name="loop_top")


# -----------------------------------------------------------------------------
# Parallel topology
# -----------------------------------------------------------------------------


def test_parallel_with_two_branches():
    pg = parallel([no("X1")], [no("X2"), nc("X3")])
    assert pg == ParallelGroup(branches=(
        (ContactNO(Address("X1")),),
        (ContactNO(Address("X2")), ContactNC(Address("X3"))),
    ))


# -----------------------------------------------------------------------------
# Variable / Tag declarations
# -----------------------------------------------------------------------------


def test_var_default_direction_is_local():
    v = var("scratch", TagType.INT)
    assert v == Var(name="scratch", data_type=TagType.INT,
                    direction=VarDirection.LOCAL)


def test_var_direction_shortcuts():
    assert var_in("a", TagType.INT).direction == VarDirection.INPUT
    assert var_out("r", TagType.INT).direction == VarDirection.OUTPUT
    assert var_inout("v", TagType.INT).direction == VarDirection.IN_OUT


def test_tag_decl_dynamic():
    """Tag declared without ``locked`` is dynamic (allocator chooses)."""
    t = tag_decl("speed", TagType.INT, "motor RPM")
    assert t == Tag(name="speed", data_type=TagType.INT,
                    description="motor RPM", address=None)


def test_tag_decl_static_with_locked_address():
    """Setting ``locked`` pins the tag to a physical address."""
    t = tag_decl("estop", TagType.BOOL, "E-stop input", locked="X101")
    assert t.address == Address("X101")


def test_data_block_helper():
    db = data_block(
        name="PID_loop1",
        members=[var("integral", TagType.REAL)],
        base_address="DB100",
        fb_template="PID",
    )
    assert db.base_address == Address("DB100")
    assert db.fb_template == "PID"
    assert db.members[0].name == "integral"


# -----------------------------------------------------------------------------
# POU shortcuts
# -----------------------------------------------------------------------------


def test_subroutine_creates_subroutine_kind():
    s = subroutine("Legacy", rungs=[rung(coil("Y001"))])
    assert s.kind == PouKind.SUBROUTINE
    assert len(s.rungs) == 1


def test_prog_creates_program_kind_and_supports_main_flag():
    p = prog("Main", main=True)
    assert p.kind == PouKind.PROGRAM
    assert p.main is True


def test_fn_creates_function_with_return_type():
    f = fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("result", TagType.INT)],
           return_type=TagType.INT)
    assert f.kind == PouKind.FUNCTION
    assert f.return_type == TagType.INT
    assert [v.name for v in f.inputs] == ["a", "b"]


def test_fb_creates_function_block():
    f = fb("PID",
           inputs=[var_in("sp", TagType.REAL), var_in("pv", TagType.REAL)],
           outputs=[var_out("out", TagType.REAL)])
    assert f.kind == PouKind.FUNCTION_BLOCK


# -----------------------------------------------------------------------------
# Top-level Program builder
# -----------------------------------------------------------------------------


def test_program_helper_keys_tags_by_name():
    p = program(
        subroutines=[prog("Main", main=True)],
        tags=[
            tag_decl("speed", TagType.INT, "motor RPM"),
            tag_decl("estop", TagType.BOOL, "E-stop", locked="X101"),
        ],
    )
    assert set(p.tags.keys()) == {"speed", "estop"}
    assert p.tags["estop"].address == Address("X101")
    assert p.find_subroutine("Main") is not None


def test_program_helper_supports_data_blocks():
    p = program(
        data_blocks=[
            data_block("Globals", members=[var("counter", TagType.DINT)]),
        ],
    )
    assert p.find_data_block("Globals") is not None


# -----------------------------------------------------------------------------
# Ergonomics: a real-shape program
# -----------------------------------------------------------------------------


def test_full_program_authored_via_dsl():
    """A complete miniature program written end-to-end with the DSL --
    proof of the verbosity reduction."""
    p = program(
        cpu_model="C2-01CPU",
        tags=[
            tag_decl("start_btn", TagType.BOOL, "operator start button",
                     locked="X101"),
            tag_decl("running",   TagType.BOOL, "machine running indicator"),
            tag_decl("speed_sp",  TagType.INT,  "speed setpoint"),
            tag_decl("speed_pv",  TagType.INT,  "speed process value"),
        ],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no("start_btn"), set_("running")),
                rung(
                    no("running"),
                    call("ClampSpeed",
                         inputs=[("sp", "speed_sp")],
                         return_to="speed_pv"),
                ),
                rung(end()),
            ]),
            fn("ClampSpeed",
               inputs=[var_in("sp", TagType.INT)],
               outputs=[var_out("clamped", TagType.INT)],
               return_type=TagType.INT,
               rungs=[
                   rung(gt(tag("sp"), 1000),
                        move(1000, tag("clamped"))),
                   rung(le(tag("sp"), 1000),
                        move(tag("sp"), tag("clamped"))),
                   rung(ret()),
               ]),
        ],
    )

    # Structural assertions on the resulting Program
    assert p.cpu_model == "C2-01CPU"
    assert p.tags["start_btn"].address == Address("X101")
    main = p.main_subroutine()
    assert main is not None and main.name == "Main"

    # Main's third rung is the call -- inputs reference the speed_sp tag
    call_rung = main.rungs[1]
    call_op = call_rung.ops[1]
    assert isinstance(call_op, Call)
    assert call_op.target == "ClampSpeed"
    assert call_op.inputs == (("sp", TagRef("speed_sp")),)
    assert call_op.return_to == TagRef("speed_pv")

    # ClampSpeed body uses TagRefs (sp / clamped) for the formal params
    clamp = p.find_subroutine("ClampSpeed")
    assert clamp.kind == PouKind.FUNCTION
    first_rung = clamp.rungs[0]
    cmp_op = first_rung.ops[0]
    assert isinstance(cmp_op, Compare)
    assert cmp_op.lhs == TagRef("sp")
    assert cmp_op.rhs == "1000"


def test_dsl_output_passes_through_full_lowering_pipeline():
    """A DSL-authored Program lowers cleanly through every pass."""
    from universal_machinery.lowering.click_calling import (
        lower_calls, lower_pou_bodies, lower_scheduled_calls,
        prepend_trampoline_to_main,
    )

    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(call("Average",
                          inputs=[("a", "DS10"), ("b", "DS11")],
                          return_to="DS20")),
            ]),
            fn("Average",
               inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
               outputs=[var_out("result", TagType.INT)],
               return_type=TagType.INT,
               rungs=[
                   rung(add(tag("a"), tag("b"), tag("result"))),
                   rung(ret()),
               ]),
        ],
    )

    p, alloc = lower_pou_bodies(p)
    p, _ = lower_calls(p, alloc=alloc)
    p, _ = lower_scheduled_calls(p, alloc=alloc)
    final = prepend_trampoline_to_main(p, alloc)

    # Average's body has tag("a")/tag("b")/tag("result") substituted to
    # the allocated arg/ret slots.
    avg = final.find_subroutine("Average")
    add_op = avg.rungs[0].ops[0]
    assert isinstance(add_op, BinaryMath)
    assert add_op.lhs == Address("DS9000")     # arg slot for "a"
    assert add_op.rhs == Address("DS9001")     # arg slot for "b"
    assert add_op.dst == Address("DS9100")     # ret slot for "result"
