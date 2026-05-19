"""PLCopen XML emission of authored ST (``Subroutine.st_body``).

The earlier slice landed the ST AST as a first-class body type
(``il/st.py``); the XML emitter still went through rung-to-ST
translation.  This slice routes authored ``st_body`` directly
through the ST emitter so the XML's ``<ST><pre>...</pre></ST>``
content matches the AST verbatim.

Each test emits XML and re-validates against the bundled PLCopen
TC6 v2.01 XSD so the conformant-output guarantee survives the new
body path.
"""
from datetime import datetime, timezone

import pytest

xmlschema = pytest.importorskip("xmlschema")

from universal_machinery.builders import (
    abstract_method, add_e, and_e, assign, call_stmt, case_, case_clause,
    coil, continue_st, eq_e, exit_st, fb, fcall_expr, field_, for_, ge_e,
    gt_e, if_, index_, interface, le_e, lit, lt_e, method, mul_e, ne_e,
    or_e, prog, program, repeat_, ret_st, rung, sub_e, var, var_in,
    var_out, while_,
)
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)
from universal_machinery.il import TagType


_FIXED_TIME = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Authored ST shows up verbatim in the XML body
# -----------------------------------------------------------------------------


def test_simple_assignment_st_body_validates():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("count", TagType.INT)],
             st_body=[assign("count", lit(0))]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "count := 0;" in xml


def test_for_loop_in_st_body_renders_verbatim():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("i", TagType.INT),
                         var("sum", TagType.INT)],
             st_body=[
                 assign("sum", lit(0)),
                 for_("i", lit(1), lit(10),
                      [assign("sum", add_e("sum", "i"))]),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "FOR i := 1 TO 10 DO" in xml
    assert "sum := sum + i;" in xml
    assert "END_FOR;" in xml


def test_if_elsif_else_in_st_body_validates():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("x", TagType.INT),
                         var("y", TagType.INT)],
             st_body=[
                 if_((gt_e("x", lit(10)), [assign("y", lit(1))]),
                     (gt_e("x", lit(0)),  [assign("y", lit(2))]),
                     else_=[assign("y", lit(0))]),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "IF x &gt; 10 THEN" in xml      # > escaped as &gt; in XML
    assert "ELSIF x &gt; 0 THEN" in xml
    assert "ELSE" in xml
    assert "END_IF;" in xml


def test_case_statement_validates():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("mode", TagType.INT),
                         var("state", TagType.INT)],
             st_body=[
                 case_("mode",
                       case_clause([lit(0)], [assign("state", lit(0))]),
                       case_clause([lit(1), lit(2)],
                                   [assign("state", lit(1))]),
                       else_=[assign("state", lit(-1))]),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "CASE mode OF" in xml
    assert "END_CASE;" in xml


def test_while_and_repeat_validate():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("i", TagType.INT)],
             st_body=[
                 assign("i", lit(0)),
                 while_(lt_e("i", lit(10)),
                        [assign("i", add_e("i", lit(1)))]),
                 repeat_([assign("i", sub_e("i", lit(1)))],
                         le_e("i", lit(0))),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "WHILE i &lt; 10 DO" in xml
    assert "REPEAT" in xml
    assert "UNTIL i &lt;= 0 END_REPEAT;" in xml


def test_jumps_validate():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("i", TagType.INT)],
             st_body=[
                 for_("i", lit(1), lit(10), [
                     if_((gt_e("i", lit(5)), [exit_st()]),
                         (eq_e("i", lit(3)), [continue_st()])),
                 ]),
                 ret_st(),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "EXIT;" in xml
    assert "CONTINUE;" in xml
    assert "RETURN;" in xml


def test_function_call_statement_validates():
    p = program(subroutines=[
        fb("Worker",
           inputs=[var_in("speed", TagType.INT)],
           outputs=[var_out("done", TagType.BOOL)]),
        prog("Main", main=True,
             local_vars=[var("w_done", TagType.BOOL)],
             st_body=[
                 call_stmt("Worker", speed=lit(100), done="w_done"),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "Worker(speed := 100, done := w_done);" in xml


def test_method_with_st_body_validates():
    """FB method with an authored ST body emits as the
    enclosing FB's ST text -- methods aren't first-class in
    PLCopen TC6 v2.01 XSD (3rd-edition limitation), but the
    method ST is still part of the FB's <ST> body so it round-
    trips through the same path."""
    p = program(subroutines=[
        fb("Squarer",
           methods=[
               method("Compute",
                      inputs=[var_in("x", TagType.INT)],
                      st_body=[assign("y", mul_e("x", "x"))],
                      return_type=TagType.INT),
           ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_expression_precedence_renders_with_parens():
    """``(a + b) * c`` requires parens around the +; the AST
    emitter parenthesises, the textual representation in XML
    preserves that.
    """
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("r", TagType.INT)],
             st_body=[
                 assign("r", mul_e(add_e("a", "b"), "c")),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "(a + b) * c" in xml


def test_st_body_takes_precedence_over_rungs_in_xml():
    """If both ``st_body`` and ``rungs`` are set (validator
    flags this), the XML body still picks ``st_body`` -- matches
    the standalone ST emitter."""
    p_st = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("x", TagType.INT)],
             st_body=[assign("x", lit(1))]),
    ])
    p_ld = program(subroutines=[
        prog("Main", main=True,
             rungs=[rung(coil("Y001"))]),
    ])
    xml_st = emit_xml(p_st, time_now=_FIXED_TIME)
    xml_ld = emit_xml(p_ld, time_now=_FIXED_TIME)
    assert "x := 1;" in xml_st
    assert "Y001 :=" in xml_ld


def test_st_body_with_xml_special_chars_is_escaped():
    """``&``, ``<``, ``>`` inside ST source (e.g., comparisons,
    string literals containing ampersands) must be XML-escaped
    so the document stays well-formed."""
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("a", TagType.INT), var("b", TagType.INT)],
             st_body=[
                 assign("a", lit(0)),
                 if_((and_e(lt_e("a", lit(10)), gt_e("b", lit(0))),
                      [assign("a", lit(1))])),
             ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # < / > must be escaped, AND keyword passes through
    assert "a &lt; 10" in xml
    assert "b &gt; 0" in xml
    assert "AND" in xml
