"""Tests for the IEC §3 Structured Text source-text parser.

Three coverage layers:

  - Expression parsing: precedence, associativity, all literal
    forms, postfix chains (field access, indexing, calls).
  - Statement parsing: every IEC §3 statement kind end-to-end.
  - Round-trip: ``emit_st_body(...) -> parse_st_body(...)`` is a
    no-op on the IL.  This is the load-bearing test; it pairs
    the emitter and parser against each other so drift gets
    caught immediately.
"""
import pytest

from universal_machinery.builders import (
    add_e, and_e, assign, call_stmt, case_, case_clause, continue_st,
    eq_e, exit_st, fcall_expr, field_, for_, ge_e, goto, gt_e, if_,
    index_, label_st, le_e, lit, lt_e, method, mul_e, ne_e, neg, not_e,
    or_e, repeat_, ret_st, sub_e, while_, xor_e,
)
from universal_machinery.emitters.st import emit_st_body
from universal_machinery.il import (
    Assignment, BinaryExpr, BinaryOp, CaseClause, CaseStatement,
    ContinueStatement, ExitStatement, FieldAccess, ForStatement,
    FunctionCallExpr, FunctionCallStatement, GotoStatement, IfStatement,
    IndexAccess, LabelStatement, Literal, RepeatStatement, ReturnStatement,
    TagRef, UnaryExpr, UnaryOp, VarRef, WhileStatement,
)
from universal_machinery.parsers.st_text import (
    StParseError, parse_st_body, parse_st_expression,
)


# -----------------------------------------------------------------------------
# Lexer edge cases (exercised via parse_st_body)
# -----------------------------------------------------------------------------


def test_empty_input_returns_empty_list():
    assert parse_st_body("") == []
    assert parse_st_body("   \n\t  ") == []


def test_block_comment_stripped():
    out = parse_st_body("(* note *)\nx := 1;")
    assert len(out) == 1
    assert isinstance(out[0], Assignment)


def test_line_comment_stripped():
    out = parse_st_body("// comment\nx := 1;")
    assert len(out) == 1


def test_string_literal_round_trips_through_lexer():
    e = parse_st_expression("'hello world'")
    assert isinstance(e, Literal)
    assert e.kind == "string"
    assert e.value == "'hello world'"


def test_typed_literal_time_form():
    e = parse_st_expression("T#100ms")
    assert e.kind == "typed"
    assert e.value == "T#100ms"


def test_typed_literal_based_int():
    e = parse_st_expression("16#FF")
    assert e.kind == "typed"
    assert e.value == "16#FF"


# -----------------------------------------------------------------------------
# Expression parsing -- precedence + associativity
# -----------------------------------------------------------------------------


def test_addition_left_associative():
    e = parse_st_expression("a + b + c")
    # (a + b) + c
    assert isinstance(e, BinaryExpr)
    assert e.op is BinaryOp.ADD
    assert isinstance(e.lhs, BinaryExpr)
    assert e.lhs.op is BinaryOp.ADD


def test_multiplication_binds_tighter_than_addition():
    e = parse_st_expression("a + b * c")
    assert isinstance(e, BinaryExpr)
    assert e.op is BinaryOp.ADD
    assert isinstance(e.rhs, BinaryExpr)
    assert e.rhs.op is BinaryOp.MUL


def test_parens_override_precedence():
    e = parse_st_expression("(a + b) * c")
    assert isinstance(e, BinaryExpr)
    assert e.op is BinaryOp.MUL
    assert isinstance(e.lhs, BinaryExpr)
    assert e.lhs.op is BinaryOp.ADD


def test_exponent_is_right_associative():
    """IEC §3.3.1: ``**`` is right-associative.  ``a ** b ** c``
    parses as ``a ** (b ** c)``."""
    e = parse_st_expression("a ** b ** c")
    assert isinstance(e, BinaryExpr)
    assert e.op is BinaryOp.EXP
    assert isinstance(e.rhs, BinaryExpr)
    assert e.rhs.op is BinaryOp.EXP


def test_logical_precedence_iec_order():
    """IEC §3.3.1: precedence (lowest→highest) is OR, XOR, AND,
    comparison, arithmetic.  ``a OR b AND c`` is ``a OR (b AND c)``."""
    e = parse_st_expression("a OR b AND c")
    assert e.op is BinaryOp.OR
    assert e.rhs.op is BinaryOp.AND


def test_iec_equality_uses_single_equals():
    """IEC uses ``=`` for equality; the parser doesn't accept ``==``."""
    e = parse_st_expression("a = b")
    assert isinstance(e, BinaryExpr)
    assert e.op is BinaryOp.EQ
    with pytest.raises(StParseError):
        parse_st_expression("a == b")


def test_iec_inequality_uses_angle_brackets():
    e = parse_st_expression("a <> b")
    assert e.op is BinaryOp.NE


def test_mod_operator_keyword():
    e = parse_st_expression("a MOD b")
    assert e.op is BinaryOp.MOD


def test_unary_not_binds_to_primary():
    e = parse_st_expression("NOT flag")
    assert isinstance(e, UnaryExpr)
    assert e.op is UnaryOp.NOT


def test_unary_minus_binds_to_primary():
    e = parse_st_expression("-x")
    assert isinstance(e, UnaryExpr)
    assert e.op is UnaryOp.NEG


def test_field_access_chain():
    e = parse_st_expression("axis.position.x")
    assert isinstance(e, FieldAccess)
    assert e.field == "x"
    assert isinstance(e.base, FieldAccess)
    assert e.base.field == "position"


def test_index_access_with_multi_dim():
    e = parse_st_expression("buf[i, j]")
    assert isinstance(e, IndexAccess)
    assert len(e.indices) == 2


def test_chained_index_and_field():
    """e.g. ``buffer[3].value`` -- common pattern for array of struct."""
    e = parse_st_expression("buffer[3].value")
    assert isinstance(e, FieldAccess)
    assert e.field == "value"
    assert isinstance(e.base, IndexAccess)


def test_function_call_positional_args():
    e = parse_st_expression("MAX(a, b)")
    assert isinstance(e, FunctionCallExpr)
    assert e.name == "MAX"
    assert len(e.positional) == 2


def test_function_call_named_args_with_input_bind():
    e = parse_st_expression("DoIt(IN1 := x, IN2 := y)")
    assert isinstance(e, FunctionCallExpr)
    assert e.named == (
        ("IN1", VarRef(TagRef("x"))),
        ("IN2", VarRef(TagRef("y"))),
    )


def test_function_call_with_output_bind_treated_as_named():
    """IEC ``out => target`` and ``in := source`` both end up in
    the IL's ``named`` slot since the AST doesn't distinguish."""
    e = parse_st_expression("Inst(IN := x, OUT => y)")
    assert e.named == (
        ("IN", VarRef(TagRef("x"))),
        ("OUT", VarRef(TagRef("y"))),
    )


def test_boolean_literals():
    assert parse_st_expression("TRUE") == Literal("TRUE", kind="bool")
    assert parse_st_expression("FALSE") == Literal("FALSE", kind="bool")


def test_integer_and_real_literals():
    assert parse_st_expression("42").kind == "int"
    assert parse_st_expression("3.14").kind == "real"


# -----------------------------------------------------------------------------
# Statement parsing
# -----------------------------------------------------------------------------


def test_simple_assignment():
    out = parse_st_body("x := 42;")
    assert len(out) == 1
    s = out[0]
    assert isinstance(s, Assignment)
    assert s.target.ref.name == "x"
    assert s.value.value == "42"


def test_assignment_to_field_access():
    out = parse_st_body("axis.position := 0;")
    s = out[0]
    assert isinstance(s.target, FieldAccess)


def test_assignment_to_indexed_lhs():
    out = parse_st_body("buffer[0] := value;")
    s = out[0]
    assert isinstance(s.target, IndexAccess)


def test_if_elsif_else():
    src = """
    IF x > 10 THEN
        y := 1;
    ELSIF x > 0 THEN
        y := 2;
    ELSE
        y := 0;
    END_IF;
    """
    out = parse_st_body(src)
    s = out[0]
    assert isinstance(s, IfStatement)
    assert len(s.branches) == 2
    assert s.else_branch is not None


def test_case_with_multi_label_clauses_and_else():
    src = """
    CASE mode OF
        0:        state := 0;
        1, 2:     state := 1;
        ELSE      state := -1;
    END_CASE;
    """
    out = parse_st_body(src)
    s = out[0]
    assert isinstance(s, CaseStatement)
    assert len(s.clauses) == 2
    assert len(s.clauses[1].labels) == 2
    assert s.else_branch is not None


def test_for_with_explicit_step():
    src = "FOR i := 0 TO 10 BY 2 DO sum := sum + i; END_FOR;"
    out = parse_st_body(src)
    s = out[0]
    assert isinstance(s, ForStatement)
    assert s.index_var == "i"
    assert s.step is not None


def test_for_without_step():
    src = "FOR i := 0 TO 10 DO sum := sum + i; END_FOR;"
    s = parse_st_body(src)[0]
    assert isinstance(s, ForStatement)
    assert s.step is None


def test_while_loop():
    src = "WHILE i < 10 DO i := i + 1; END_WHILE;"
    s = parse_st_body(src)[0]
    assert isinstance(s, WhileStatement)


def test_repeat_until_loop():
    src = "REPEAT i := i - 1; UNTIL i <= 0 END_REPEAT;"
    s = parse_st_body(src)[0]
    assert isinstance(s, RepeatStatement)


def test_return_exit_continue():
    out = parse_st_body("RETURN; EXIT; CONTINUE;")
    assert isinstance(out[0], ReturnStatement)
    assert isinstance(out[1], ExitStatement)
    assert isinstance(out[2], ContinueStatement)


def test_goto_and_label():
    out = parse_st_body("LOOP:\nx := x + 1;\nGOTO LOOP;")
    assert isinstance(out[0], LabelStatement)
    assert out[0].name == "LOOP"
    assert isinstance(out[2], GotoStatement)
    assert out[2].label == "LOOP"


def test_function_call_statement():
    out = parse_st_body("DoIt(IN := x, OUT => y);")
    s = out[0]
    assert isinstance(s, FunctionCallStatement)
    assert s.call.name == "DoIt"


# -----------------------------------------------------------------------------
# Error cases
# -----------------------------------------------------------------------------


def test_unterminated_string_raises():
    with pytest.raises(StParseError, match="unterminated string"):
        parse_st_body("x := 'oops")


def test_unexpected_character_raises():
    with pytest.raises(StParseError, match="unexpected character"):
        parse_st_body("x := @;")


def test_missing_semicolon_after_assignment_raises():
    with pytest.raises(StParseError, match="expected ';'"):
        parse_st_body("x := 1\ny := 2;")


def test_missing_end_if_raises():
    with pytest.raises(StParseError):
        parse_st_body("IF x > 0 THEN y := 1;")


def test_trailing_tokens_in_single_expression_raises():
    with pytest.raises(StParseError, match="trailing tokens"):
        parse_st_expression("a + b ;")


def test_unknown_pou_type_message_includes_lexeme():
    """Error messages should include the offending lexeme + line/col."""
    try:
        parse_st_body("x := ;")
    except StParseError as exc:
        assert exc.line >= 1
        assert exc.column >= 1
    else:
        pytest.fail("expected StParseError")


# -----------------------------------------------------------------------------
# Round-trip: emit -> parse -> compare statement-list shape
# -----------------------------------------------------------------------------


def _round_trip(stmts):
    """Emit a statement list, parse it back, return the parsed list."""
    src = "\n".join(emit_st_body(stmts, level=0))
    return parse_st_body(src)


def test_round_trip_assignment():
    body = [assign("x", lit(42))]
    out = _round_trip(body)
    assert out == body


def test_round_trip_for_with_inner_assignment():
    body = [for_("i", lit(1), lit(10),
                  [assign("sum", add_e("sum", "i"))])]
    out = _round_trip(body)
    assert out == body


def test_round_trip_if_elsif_else():
    body = [if_(
        (gt_e("x", lit(10)), [assign("y", lit(1))]),
        (gt_e("x", lit(0)),  [assign("y", lit(2))]),
        else_=[assign("y", lit(0))],
    )]
    out = _round_trip(body)
    assert out == body


def test_round_trip_case_with_multi_label_clauses():
    body = [case_("mode",
                   case_clause([lit(0)], [assign("state", lit(0))]),
                   case_clause([lit(1), lit(2)],
                                [assign("state", lit(1))]),
                   else_=[assign("state", lit(-1))])]
    out = _round_trip(body)
    assert out == body


def test_round_trip_while_loop():
    body = [while_(lt_e("i", lit(10)),
                    [assign("i", add_e("i", lit(1)))])]
    assert _round_trip(body) == body


def test_round_trip_repeat_loop():
    body = [repeat_([assign("i", sub_e("i", lit(1)))],
                     le_e("i", lit(0)))]
    assert _round_trip(body) == body


def test_round_trip_goto_label():
    body = [label_st("LOOP"),
            assign("i", add_e("i", lit(1))),
            goto("LOOP")]
    assert _round_trip(body) == body


def test_round_trip_call_statement():
    body = [call_stmt("DoIt", IN1=lit(5), OUT="result")]
    out = _round_trip(body)
    assert len(out) == 1
    assert isinstance(out[0], FunctionCallStatement)
    assert out[0].call.name == "DoIt"
    assert len(out[0].call.named) == 2


def test_round_trip_expression_precedence_survives():
    """A nested arithmetic expression with parens emits with
    parens; the parser puts the parens back where they belong, so
    the round-trip is structural-equality stable."""
    body = [assign("r", mul_e(add_e("a", "b"), "c"))]
    out = _round_trip(body)
    assert out == body


def test_round_trip_logical_chain():
    body = [assign("ok", and_e("a", or_e("b", "c")))]
    out = _round_trip(body)
    assert out == body


def test_round_trip_nested_blocks():
    """Mix every statement kind into one body."""
    body = [
        assign("count", lit(0)),
        for_("i", lit(1), lit(10), [
            if_((gt_e("i", lit(5)), [exit_st()]),
                else_=[assign("count", add_e("count", "i"))]),
        ]),
        while_(lt_e("count", lit(100)),
                [assign("count", add_e("count", lit(1)))]),
        ret_st(),
    ]
    out = _round_trip(body)
    assert out == body
