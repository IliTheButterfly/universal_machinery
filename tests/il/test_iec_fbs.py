"""Tests for IEC 61131-3 §2.5.2.3.3 bistables / edge triggers and the
generic ``StdFunc`` op (§2.5.2 standard library).
"""
import pytest

from universal_machinery.il import (
    Address, FTrig, Program, RS, RTrig, Rung, SR, STD_FUNCTION_NAMES,
    StdFunc, Subroutine, TagRef,
)
from universal_machinery.il.ops import addresses_of, tags_of
from universal_machinery.builders import (
    abs_, acos, and_, asin, atan, cos, exp, f_trig, limit, ln, log, max_,
    min_, mux, mul, not_, or_, r_trig, rol, ror, rs, sel, shl, shr, sin, sqrt,
    sr, std_func, tan, tag, xor_,
)


# -----------------------------------------------------------------------------
# Edge triggers (RTrig / FTrig)
# -----------------------------------------------------------------------------


def test_rtrig_construction_and_addresses():
    op = r_trig("C100", "X001", "Y001")
    assert op == RTrig(
        state=Address("C100"),
        clk=Address("X001"),
        q=Address("Y001"),
    )
    addrs = {a.raw for a in addresses_of(op)}
    assert addrs == {"C100", "X001", "Y001"}


def test_ftrig_construction():
    op = f_trig("C101", "X002", "Y002")
    assert op == FTrig(
        state=Address("C101"),
        clk=Address("X002"),
        q=Address("Y002"),
    )


def test_rtrig_with_tagrefs():
    op = r_trig("debounce_state", "start_btn", "start_pulse")
    addrs = {a.raw for a in addresses_of(op)}
    assert addrs == set()                      # all are TagRefs
    assert tags_of(op) == {"debounce_state", "start_btn", "start_pulse"}


# -----------------------------------------------------------------------------
# Bistables (SR / RS)
# -----------------------------------------------------------------------------


def test_sr_set_dominant_construction():
    op = sr("Y010", "X001", "X002")
    assert op == SR(q1=Address("Y010"), s1=Address("X001"), r=Address("X002"))
    addrs = {a.raw for a in addresses_of(op)}
    assert addrs == {"Y010", "X001", "X002"}


def test_rs_reset_dominant_construction():
    op = rs("Y011", "X003", "X004")
    assert op == RS(q1=Address("Y011"), r1=Address("X003"), s=Address("X004"))


def test_sr_and_rs_distinguishable_by_class():
    """Set-dominant vs reset-dominant: structurally distinct types so
    backends dispatch on isinstance without checking a string field."""
    s = sr("Q1", "S", "R")
    r = rs("Q1", "R", "S")
    assert isinstance(s, SR)
    assert isinstance(r, RS)
    assert not isinstance(s, RS)
    assert not isinstance(r, SR)


# -----------------------------------------------------------------------------
# StdFunc: generic IEC stdlib function
# -----------------------------------------------------------------------------


def test_std_func_construction_with_named_fn():
    op = std_func("ABS", ["DS10"], "DS11")
    assert op == StdFunc(
        name="ABS",
        inputs=(Address("DS10"),),
        output=Address("DS11"),
    )


def test_std_func_addresses_collected():
    op = std_func("SEL", ["X001", "DS10", "DS20"], "DS30")
    addrs = {a.raw for a in addresses_of(op)}
    assert addrs == {"X001", "DS10", "DS20", "DS30"}


def test_std_func_tagref_inputs_collected_via_tags_of():
    op = std_func("LIMIT", ["min_speed", "speed_sp", "max_speed"], "speed_cmd")
    assert tags_of(op) == {"min_speed", "speed_sp", "max_speed", "speed_cmd"}
    assert addresses_of(op) == set()


def test_std_func_numeric_literal_input_passes_through():
    """Numeric literals stay as strings on Value-typed input fields."""
    op = std_func("LIMIT", [0, "speed_sp", 1000], "speed_cmd")
    assert op.inputs[0] == "0"
    assert op.inputs[1] == TagRef("speed_sp")
    assert op.inputs[2] == "1000"


# -----------------------------------------------------------------------------
# IEC standard-function registry
# -----------------------------------------------------------------------------


def test_known_iec_names_registered():
    """Sanity: common IEC stdlib names are in the registry so
    validation passes can flag unknown names early."""
    expected = {
        "ABS", "SQRT", "AND", "OR", "XOR", "NOT",
        "SEL", "MAX", "MIN", "LIMIT", "MUX",
        "SHL", "SHR", "ROR", "ROL",
        "INT_TO_REAL", "REAL_TO_INT", "INT_TO_STRING",
    }
    assert expected <= STD_FUNCTION_NAMES


# -----------------------------------------------------------------------------
# Builder DSL convenience helpers
# -----------------------------------------------------------------------------


def test_numerical_helpers_produce_named_std_func():
    assert abs_("DS10",  "DS11").name == "ABS"
    assert sqrt("DS10",  "DS11").name == "SQRT"
    assert ln("DS10",    "DS11").name == "LN"
    assert log("DS10",   "DS11").name == "LOG"
    assert exp("DS10",   "DS11").name == "EXP"
    assert sin("DS10",   "DS11").name == "SIN"
    assert cos("DS10",   "DS11").name == "COS"
    assert tan("DS10",   "DS11").name == "TAN"
    assert asin("DS10",  "DS11").name == "ASIN"
    assert acos("DS10",  "DS11").name == "ACOS"
    assert atan("DS10",  "DS11").name == "ATAN"


def test_bitwise_helpers_produce_named_std_func():
    assert and_("DS1", "DS2", "DS3").name == "AND"
    assert or_("DS1",  "DS2", "DS3").name == "OR"
    assert xor_("DS1", "DS2", "DS3").name == "XOR"
    assert not_("DS1", "DS2").name == "NOT"


def test_bit_string_helpers():
    assert shl("DS1", 4, "DS2").name == "SHL"
    assert shr("DS1", 4, "DS2").name == "SHR"
    assert ror("DS1", 1, "DS2").name == "ROR"
    assert rol("DS1", 1, "DS2").name == "ROL"


def test_selection_helpers():
    assert max_("DS1", "DS2", "DS3").name == "MAX"
    assert min_("DS1", "DS2", "DS3").name == "MIN"


def test_sel_takes_gate_plus_two_inputs():
    """IEC SEL: G, IN0, IN1 -> output := IN1 if G else IN0."""
    op = sel("X001", "DS10", "DS20", "DS30")
    assert op.name == "SEL"
    assert op.inputs == (Address("X001"), Address("DS10"), Address("DS20"))
    assert op.output == Address("DS30")


def test_limit_takes_lo_value_hi():
    op = limit(0, "speed_sp", 1000, "speed_cmd")
    assert op.name == "LIMIT"
    assert op.inputs == ("0", TagRef("speed_sp"), "1000")


def test_mux_takes_selector_plus_inputs():
    op = mux("DS0", "DS10", "DS11", "DS12", "DS13", output="DS20")
    assert op.name == "MUX"
    assert len(op.inputs) == 5            # selector + 4 inputs


# -----------------------------------------------------------------------------
# Bistables / triggers compose into rungs cleanly
# -----------------------------------------------------------------------------


def test_bistables_compose_into_rungs():
    """Verify these new ops slot into a Rung's ops list as any other."""
    from universal_machinery.builders import rung, no, coil
    r = rung(
        r_trig("edge_state", "start_btn", "start_pulse"),
        no("start_pulse"),
        sr("running", "start_pulse", "stop_btn"),
        coil("motor_run"),
    )
    assert len(r.ops) == 4
    assert isinstance(r.ops[0], RTrig)
    assert isinstance(r.ops[2], SR)


def test_referenced_addresses_walks_iec_ops_through_program():
    """Program.referenced_addresses includes addresses from the new
    IEC ops so the symbol-table walker stays complete."""
    from universal_machinery.builders import rung, prog, program
    p = program(subroutines=[
        prog("Main", main=True, rungs=[
            rung(r_trig("C100", "X001", "Y001")),
            rung(sr("Y010", "X002", "X003")),
            rung(std_func("ABS", ["DS10"], "DS11")),
        ]),
    ])
    addrs = {a.raw for a in p.referenced_addresses()}
    assert addrs == {
        "C100", "X001", "Y001",       # RTrig
        "Y010", "X002", "X003",       # SR
        "DS10", "DS11",               # StdFunc ABS
    }
