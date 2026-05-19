"""Tests for the first-class ST AST (IEC 61131-3 §3).

Covers four layers:

  - AST construction (dataclass / enum shapes, ``is_lvalue``,
    ``walk_expressions``)
  - Builder DSL (smart coercion, expression composition, statement
    helpers)
  - ST emission (expression precedence + parenthesisation,
    statement rendering, POU body integration)
  - JSON round-trip
  - Validation (body-kind mutex, lvalue check, FOR index_var
    declared, OOP method st_body)
"""
import pytest

from universal_machinery.builders import (
    abstract_method, add_e, and_e, assign, call_stmt, case_, case_clause,
    coil, continue_st, eq_e, exit_st, fb, fcall_expr, field_, for_, ge_e,
    goto, gt_e, if_, index_, interface, label_st, le_e, lit, lt_e, method,
    mul_e, ne_e, neg, not_e, or_e, prog, program, rung, ret_st, repeat_,
    sub_e, tag, tag_decl, var, var_in, var_out, var_ref, while_, xor_e,
)
from universal_machinery.emitters.st import (
    emit_pou, emit_program, emit_st_body, emit_statement,
)
from universal_machinery.il import (
    Assignment, BinaryExpr, BinaryOp, CaseClause, CaseStatement,
    ContinueStatement, ExitStatement, FieldAccess, ForStatement,
    FunctionCallExpr, FunctionCallStatement, GotoStatement, IfStatement,
    IndexAccess, LabelStatement, Literal, RepeatStatement, ReturnStatement,
    TagRef, TagType, UnaryExpr, UnaryOp, VarRef, WhileStatement, is_lvalue,
    walk_expressions,
)
from universal_machinery.serialisation import from_json, to_json
from universal_machinery.validation import is_valid, validate


# -----------------------------------------------------------------------------
# AST construction
# -----------------------------------------------------------------------------


def test_literal_kind_default_is_raw():
    lit_ = Literal("123")
    assert lit_.kind == "raw"


def test_binary_op_iec_symbols():
    assert BinaryOp.EQ.value == "="       # IEC uses '=' (not '==')
    assert BinaryOp.NE.value == "<>"      # IEC uses '<>' (not '!=')
    assert BinaryOp.MOD.value == "MOD"
    assert BinaryOp.AND.value == "AND"


def test_unary_op_values():
    assert UnaryOp.NEG.value == "-"
    assert UnaryOp.NOT.value == "NOT"


def test_is_lvalue_simple():
    assert is_lvalue(VarRef(TagRef("x")))
    assert is_lvalue(FieldAccess(VarRef(TagRef("o")), "f"))
    assert is_lvalue(IndexAccess(VarRef(TagRef("a")),
                                  (Literal("0", kind="int"),)))


def test_is_lvalue_rejects_literal_and_binop():
    assert not is_lvalue(Literal("0", kind="int"))
    assert not is_lvalue(BinaryExpr(BinaryOp.ADD,
                                     Literal("1", kind="int"),
                                     Literal("2", kind="int")))
    assert not is_lvalue(FunctionCallExpr(name="f"))


def test_walk_expressions_finds_nested_refs():
    body = [
        assign("count", add_e("count", 1)),
        if_(((gt_e("count", 10)), [assign("done", lit(True))])),
    ]
    seen = []
    for s in body:
        for e in walk_expressions(s):
            if isinstance(e, VarRef) and isinstance(e.ref, TagRef):
                seen.append(e.ref.name)
    assert "count" in seen
    assert "done" in seen


# -----------------------------------------------------------------------------
# Builder DSL: smart coercion
# -----------------------------------------------------------------------------


def test_assign_string_target_coerces_to_varref():
    a = assign("counter", 0)
    assert isinstance(a.target, VarRef)
    assert isinstance(a.target.ref, TagRef)
    assert a.target.ref.name == "counter"


def test_assign_int_literal():
    a = assign("counter", 42)
    assert a.value == Literal("42", kind="int")


def test_assign_bool_literal():
    a = assign("done", True)
    assert a.value == Literal("TRUE", kind="bool")
    b = assign("done", False)
    assert b.value == Literal("FALSE", kind="bool")


def test_assign_address_string_coerces_to_address():
    a = assign("Y001", True)
    assert isinstance(a.target, VarRef)
    # 'Y001' matches the address pattern (uppercase + digits)
    from universal_machinery.il import Address
    assert isinstance(a.target.ref, Address)


def test_lit_forces_string_literal():
    e = lit("'hello'", kind="string")
    assert e.value == "'hello'"
    assert e.kind == "string"


def test_field_and_index_compose():
    e = field_(index_("buffer", 3), "value")
    assert isinstance(e, FieldAccess)
    assert e.field == "value"
    assert isinstance(e.base, IndexAccess)
    assert e.base.indices[0] == Literal("3", kind="int")


def test_neg_and_not_unary_builders():
    assert neg(5) == UnaryExpr(UnaryOp.NEG, Literal("5", kind="int"))
    assert not_e("flag") == UnaryExpr(UnaryOp.NOT, VarRef(TagRef("flag")))


def test_all_binop_builders_construct_correct_op():
    cases = [
        (add_e, BinaryOp.ADD), (sub_e, BinaryOp.SUB),
        (mul_e, BinaryOp.MUL), (eq_e, BinaryOp.EQ),
        (ne_e, BinaryOp.NE), (lt_e, BinaryOp.LT),
        (le_e, BinaryOp.LE), (gt_e, BinaryOp.GT),
        (ge_e, BinaryOp.GE), (and_e, BinaryOp.AND),
        (or_e, BinaryOp.OR), (xor_e, BinaryOp.XOR),
    ]
    for fn, op in cases:
        e = fn("a", "b")
        assert e.op is op


def test_fcall_expr_with_positional_and_named():
    e = fcall_expr("LIMIT", 0, "x", hi=100)
    assert e.name == "LIMIT"
    assert len(e.positional) == 2
    assert e.named == (("hi", Literal("100", kind="int")),)


# -----------------------------------------------------------------------------
# Builder DSL: control flow
# -----------------------------------------------------------------------------


def test_if_with_elsif_and_else():
    s = if_(
        (gt_e("x", 10), [assign("y", 1)]),
        (gt_e("x", 5),  [assign("y", 2)]),
        else_=[assign("y", 0)],
    )
    assert isinstance(s, IfStatement)
    assert len(s.branches) == 2
    assert s.else_branch is not None
    assert len(s.else_branch) == 1


def test_case_with_clauses_and_else():
    s = case_(
        "mode",
        case_clause([lit(0)], [assign("state", "idle")]),
        case_clause([lit(1), lit(2)], [assign("state", "active")]),
        else_=[assign("state", "error")],
    )
    assert isinstance(s, CaseStatement)
    assert len(s.clauses) == 2
    assert len(s.clauses[1].labels) == 2


def test_while_for_repeat_shapes():
    w = while_(lt_e("i", 10), [assign("i", add_e("i", 1))])
    assert isinstance(w, WhileStatement)
    r = repeat_([assign("i", sub_e("i", 1))], gt_e("i", 0))
    assert isinstance(r, RepeatStatement)
    f = for_("i", 1, 10, [assign("sum", add_e("sum", "i"))])
    assert isinstance(f, ForStatement)
    assert f.index_var == "i"
    assert f.step is None
    f_step = for_("i", 1, 10, [], step=2)
    assert f_step.step == Literal("2", kind="int")


def test_call_stmt_and_jumps():
    cs = call_stmt("DoIt", "x", out="y")
    assert isinstance(cs, FunctionCallStatement)
    assert cs.call.name == "DoIt"
    assert isinstance(ret_st(), ReturnStatement)
    assert isinstance(exit_st(), ExitStatement)
    assert isinstance(continue_st(), ContinueStatement)


# -----------------------------------------------------------------------------
# ST emission
# -----------------------------------------------------------------------------


def test_emit_assignment_simple():
    lines = emit_statement(assign("x", 5), level=0)
    assert lines == ["x := 5;"]


def test_emit_expression_precedence_parenthesises_lower():
    # x + y * z  -- no parens needed; * binds tighter than +
    s = assign("r", add_e("x", mul_e("y", "z")))
    assert emit_statement(s, level=0) == ["r := x + y * z;"]

    # (x + y) * z  -- parens required around the +
    s2 = assign("r", mul_e(add_e("x", "y"), "z"))
    assert emit_statement(s2, level=0) == ["r := (x + y) * z;"]


def test_emit_unary_not_renders_with_space():
    s = assign("a", not_e("flag"))
    assert emit_statement(s, level=0) == ["a := NOT flag;"]


def test_emit_unary_neg_no_space():
    s = assign("a", neg("x"))
    assert emit_statement(s, level=0) == ["a := -x;"]


def test_emit_if_elsif_else():
    s = if_(
        (gt_e("x", 10), [assign("y", 1)]),
        (gt_e("x", 0),  [assign("y", 2)]),
        else_=[assign("y", 0)],
    )
    out = emit_statement(s, level=0)
    assert out == [
        "IF x > 10 THEN",
        "    y := 1;",
        "ELSIF x > 0 THEN",
        "    y := 2;",
        "ELSE",
        "    y := 0;",
        "END_IF;",
    ]


def test_emit_case_with_else():
    s = case_(
        "mode",
        case_clause([lit(0)], [assign("state", lit("'idle'", kind="string"))]),
        case_clause([lit(1)], [assign("state", lit("'run'",  kind="string"))]),
        else_=[assign("state", lit("'error'", kind="string"))],
    )
    out = emit_statement(s, level=0)
    assert out[0] == "CASE mode OF"
    assert "    0:" in out
    assert "    1:" in out
    assert "ELSE" in out
    assert out[-1] == "END_CASE;"


def test_emit_while():
    s = while_(lt_e("i", 10), [assign("i", add_e("i", 1))])
    out = emit_statement(s, level=0)
    assert out == [
        "WHILE i < 10 DO",
        "    i := i + 1;",
        "END_WHILE;",
    ]


def test_emit_repeat():
    s = repeat_([assign("i", sub_e("i", 1))], le_e("i", 0))
    out = emit_statement(s, level=0)
    assert out[0] == "REPEAT"
    assert "UNTIL i <= 0 END_REPEAT;" in out


def test_emit_for_with_and_without_step():
    s = for_("i", 1, 10, [assign("sum", add_e("sum", "i"))])
    out = emit_statement(s, level=0)
    assert out[0] == "FOR i := 1 TO 10 DO"
    assert out[-1] == "END_FOR;"

    s2 = for_("i", 0, 100, [assign("sum", add_e("sum", "i"))], step=2)
    out2 = emit_statement(s2, level=0)
    assert out2[0] == "FOR i := 0 TO 100 BY 2 DO"


def test_emit_jumps():
    assert emit_statement(ret_st(), level=0) == ["RETURN;"]
    assert emit_statement(exit_st(), level=0) == ["EXIT;"]
    assert emit_statement(continue_st(), level=0) == ["CONTINUE;"]


def test_emit_call_stmt_with_named_args():
    s = call_stmt("DoIt", in_=5, out="result")
    out = emit_statement(s, level=0)
    assert out == ["DoIt(in_ := 5, out := result);"]


def test_emit_field_and_index_access():
    s = assign(field_("axis", "position"), index_("buffer", "i"))
    out = emit_statement(s, level=0)
    assert out == ["axis.position := buffer[i];"]


def test_emit_program_with_st_body():
    p = program(subroutines=[
        prog("Main", main=True, st_body=[
            assign("count", 0),
            for_("i", 1, 10, [assign("count", add_e("count", "i"))]),
        ], local_vars=[var("i", TagType.INT)]),
    ])
    txt = emit_program(p)
    assert "PROGRAM Main" in txt
    assert "count := 0;" in txt
    assert "FOR i := 1 TO 10 DO" in txt
    assert "END_FOR;" in txt
    assert "END_PROGRAM" in txt


def test_st_body_takes_precedence_over_rungs_if_both_present():
    # The emitter renders st_body when present, regardless of rungs.
    p_st = prog("Main", st_body=[assign("x", 1)])
    p_ld = prog("Main", rungs=[rung(coil("Y001"))])
    txt_st = emit_pou(p_st)
    txt_ld = emit_pou(p_ld)
    assert "x := 1;" in txt_st
    assert "Y001 :=" in txt_ld


def test_method_with_st_body_emits_st():
    m = method("Compute",
               inputs=[var_in("x", TagType.INT)],
               st_body=[assign("y", mul_e("x", "x"))],
               return_type=TagType.INT)
    sub = fb("Squarer", methods=[m])
    txt = emit_pou(sub)
    assert "METHOD PUBLIC Compute : INT" in txt
    assert "y := x * x;" in txt


# -----------------------------------------------------------------------------
# JSON round-trip
# -----------------------------------------------------------------------------


def test_st_body_round_trips_through_json():
    body = [
        assign("count", lit(0)),
        for_("i", lit(1), lit(10),
             [assign("count", add_e("count", "i"))]),
        if_((gt_e("count", 50), [ret_st()]),
            else_=[assign("done", True)]),
    ]
    p = program(subroutines=[
        prog("Main", main=True, st_body=body,
             local_vars=[var("i", TagType.INT),
                         var("count", TagType.INT),
                         var("done", TagType.BOOL)]),
    ])
    js = to_json(p)
    p2 = from_json(js)
    assert p2.subroutines[0].st_body is not None
    # Round-tripped statement count matches
    assert len(p2.subroutines[0].st_body) == len(body)
    # Re-emit identical ST text
    assert emit_program(p) == emit_program(p2)


def test_case_statement_round_trips():
    body = [
        case_("mode",
              case_clause([lit(0)], [assign("y", lit(0))]),
              case_clause([lit(1), lit(2)], [assign("y", lit(1))]),
              else_=[assign("y", lit(-1))]),
    ]
    p = program(subroutines=[prog("Main", st_body=body)])
    js = to_json(p)
    p2 = from_json(js)
    case_stmt = p2.subroutines[0].st_body[0]
    assert isinstance(case_stmt, CaseStatement)
    assert len(case_stmt.clauses) == 2
    assert len(case_stmt.clauses[1].labels) == 2


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def test_clean_st_program_validates():
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("i", TagType.INT), var("sum", TagType.INT)],
             st_body=[
                 assign("sum", lit(0)),
                 for_("i", lit(1), lit(10),
                      [assign("sum", add_e("sum", "i"))]),
             ]),
    ])
    assert is_valid(p), validate(p)


def test_multiple_body_kinds_flagged():
    # A POU with both rungs and st_body is ambiguous.
    bad = prog("Main",
               rungs=[rung(coil("Y001"))],
               st_body=[assign("x", lit(1))])
    p = program(subroutines=[bad])
    codes = [e.code for e in validate(p)]
    assert "multiple-body-kinds" in codes


def test_bad_assignment_target_flagged():
    # Assigning to a literal is not an lvalue.
    bad_assignment = Assignment(target=Literal("5", kind="int"),
                                value=Literal("0", kind="int"))
    p = program(subroutines=[
        prog("Main", st_body=[bad_assignment]),
    ])
    codes = [e.code for e in validate(p)]
    assert "bad-assignment-target" in codes


def test_for_index_undeclared_flagged():
    p = program(subroutines=[
        prog("Main",
             # 'k' is NOT declared
             st_body=[for_("k", lit(0), lit(5), [])]),
    ])
    codes = [e.code for e in validate(p)]
    assert "for-index-undeclared" in codes


def test_for_index_declared_passes():
    p = program(subroutines=[
        prog("Main",
             local_vars=[var("k", TagType.INT)],
             st_body=[for_("k", lit(0), lit(5), [])]),
    ])
    codes = [e.code for e in validate(p)]
    assert "for-index-undeclared" not in codes


def test_method_st_body_validation_runs():
    # Bad assignment target inside a Method's st_body should fire.
    bad = Assignment(target=Literal("0", kind="int"),
                     value=Literal("1", kind="int"))
    p = program(subroutines=[
        fb("Owner",
           methods=[method("Bad", st_body=[bad])]),
    ])
    codes = [e.code for e in validate(p)]
    assert "bad-assignment-target" in codes


def test_method_for_index_uses_method_locals():
    # The FOR's index var is declared as a method-local; the
    # enclosing FB doesn't have it.  Should still validate cleanly.
    p = program(subroutines=[
        fb("Owner",
           methods=[method("Compute",
                           local_vars=[var("j", TagType.INT)],
                           st_body=[for_("j", lit(0), lit(3), [])])]),
    ])
    codes = [e.code for e in validate(p)]
    assert "for-index-undeclared" not in codes


# -----------------------------------------------------------------------------
# Goto / Label statements (IEC §3.3.2.5)
# -----------------------------------------------------------------------------


def test_goto_and_label_builder_construction():
    g = goto("END")
    assert isinstance(g, GotoStatement)
    assert g.label == "END"
    l = label_st("START")
    assert isinstance(l, LabelStatement)
    assert l.name == "START"


def test_emit_goto_statement():
    assert emit_statement(goto("END"), level=0) == ["GOTO END;"]


def test_emit_label_statement():
    assert emit_statement(label_st("START"), level=0) == ["START:"]


def test_goto_label_round_trips_through_json():
    p = program(subroutines=[
        prog("Main", main=True, st_body=[
            label_st("LOOP"),
            assign("x", lit(0)),
            goto("LOOP"),
        ]),
    ])
    p2 = from_json(to_json(p))
    body = p2.subroutines[0].st_body
    assert isinstance(body[0], LabelStatement)
    assert body[0].name == "LOOP"
    assert isinstance(body[2], GotoStatement)
    assert body[2].label == "LOOP"


def test_unresolved_goto_flagged():
    p = program(subroutines=[
        prog("Main", st_body=[goto("MISSING")]),
    ])
    codes = [e.code for e in validate(p)]
    assert "st-unresolved-goto" in codes


def test_resolved_goto_passes_validation():
    p = program(subroutines=[
        prog("Main", st_body=[
            label_st("HERE"),
            goto("HERE"),
        ]),
    ])
    codes = [e.code for e in validate(p)]
    assert "st-unresolved-goto" not in codes
    assert "st-duplicate-label" not in codes


def test_duplicate_label_flagged():
    p = program(subroutines=[
        prog("Main", st_body=[
            label_st("HERE"),
            assign("x", lit(0)),
            label_st("HERE"),  # duplicate
        ]),
    ])
    codes = [e.code for e in validate(p)]
    assert "st-duplicate-label" in codes


def test_goto_targets_label_inside_if_branch_resolves():
    """Label declared inside a nested ``IfStatement`` branch is
    visible to GOTOs in the same body."""
    p = program(subroutines=[
        prog("Main", st_body=[
            if_((eq_e("x", lit(1)), [label_st("MID")])),
            goto("MID"),
        ]),
    ])
    codes = [e.code for e in validate(p)]
    assert "st-unresolved-goto" not in codes


def test_method_st_body_goto_validation():
    p = program(subroutines=[
        fb("Owner",
           methods=[method("Bad",
                           st_body=[goto("NOWHERE")])]),
    ])
    codes = [e.code for e in validate(p)]
    assert "st-unresolved-goto" in codes


def test_emit_program_with_goto_label_in_body():
    p = program(subroutines=[
        prog("Main", main=True, st_body=[
            label_st("LOOP"),
            assign("count", add_e("count", lit(1))),
            if_((lt_e("count", lit(10)), [goto("LOOP")])),
        ]),
    ])
    txt = emit_program(p)
    assert "LOOP:" in txt
    assert "GOTO LOOP;" in txt
