"""IEC 61131-3 §3 Structured Text AST.

A first-class statement / expression model so POUs can be authored
in ST directly, alongside the existing ``rungs`` (LD/IL) and
``sfc`` (grafcet) body kinds.

Before this module, the only authored body form was LD ``Rung``;
the ST *emitter* translated rungs into ST text but couldn't represent
a program that started life as ST.  Real PLC code in the field is a
mix -- motion control, math-heavy loops, recipe handling, and most
3rd-edition method bodies are authored in ST rather than LD.  Without
an ST AST, we couldn't:

  - Round-trip ST source through the IL (read PLCopen XML ``<ST>``
    bodies, modify the AST, write them back).
  - Author programs in ST via the builder DSL.
  - Lower ST → LD or ST → another vendor's ST dialect without
    going through textual rewriting.

Grammar (IEC §3, simplified)
---------------------------

::

    statement_list := statement+
    statement := assignment ';'
               | function_call ';'
               | selection
               | iteration
               | jump ';'

    assignment := variable ':=' expression
    variable   := name ('.' name | '[' expression (',' expression)* ']')*

    expression := literal
                | variable
                | unary_op expression
                | expression binary_op expression
                | function_call

    selection  := if_stmt | case_stmt
    if_stmt    := 'IF' expression 'THEN' statement_list
                  ('ELSIF' expression 'THEN' statement_list)*
                  ('ELSE' statement_list)?
                  'END_IF'
    case_stmt  := 'CASE' expression 'OF'
                  (case_label_list ':' statement_list)+
                  ('ELSE' statement_list)?
                  'END_CASE'

    iteration  := for_stmt | while_stmt | repeat_stmt
    for_stmt   := 'FOR' name ':=' expression 'TO' expression
                  ('BY' expression)? 'DO' statement_list 'END_FOR'
    while_stmt := 'WHILE' expression 'DO' statement_list 'END_WHILE'
    repeat     := 'REPEAT' statement_list 'UNTIL' expression 'END_REPEAT'

    jump       := 'RETURN' | 'EXIT' | 'CONTINUE'

Modelling choices
-----------------

* **Frozen dataclasses.**  Expressions and statements are
  immutable; nested children are tuples (for hashability).  Matches
  the rest of ``il`` and round-trips cleanly through JSON.

* **Expression / Statement split.**  Side-effect-free constructs
  (literal, variable read, binary op, function call as expression)
  vs. side-effecting (assignment, control flow, function call as
  statement).  IEC ST allows a function call to appear as either;
  ``FunctionCallExpr`` is the value-producing form,
  ``FunctionCallStatement`` is the side-effect form.

* **Variable reference reuses Address / TagRef.**  The ST AST sits
  on top of the existing tag system: ``VarRef`` wraps an
  ``Address`` or ``TagRef`` so resolver passes can rewrite ST
  bodies the same way they rewrite rung bodies.  Field and index
  access stack on top of a base ``VarRef``.

* **No semantic typing in the AST itself.**  The AST records what
  was written; a separate type-checker pass (out of scope here)
  walks the tree against ``Var.data_type`` declarations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from .ast import Address, TagRef


# -----------------------------------------------------------------------------
# Expressions
# -----------------------------------------------------------------------------


class UnaryOp(Enum):
    """IEC §3.3.1 unary operators."""
    NEG = "-"        # arithmetic negation
    NOT = "NOT"      # logical / bitwise complement


class BinaryOp(Enum):
    """IEC §3.3.1 binary operators, in precedence order.

    Precedence is documented in IEC §3.3.1 Table 55; the emitter
    is responsible for parenthesisation when rendering.
    """
    EXP   = "**"     # exponent
    MUL   = "*"
    DIV   = "/"
    MOD   = "MOD"
    ADD   = "+"
    SUB   = "-"
    LT    = "<"
    GT    = ">"
    LE    = "<="
    GE    = ">="
    EQ    = "="      # IEC uses '=' for equality (not '==')
    NE    = "<>"     # IEC uses '<>' for inequality
    AND   = "AND"
    XOR   = "XOR"
    OR    = "OR"


@dataclass(frozen=True)
class Literal:
    """A literal value in an expression.

    ``value`` is kept verbatim as a string so the IEC formatting
    (``T#100ms``, ``REAL#3.14``, ``16#FF``, ``'hello'``) round-trips
    losslessly.  ``kind`` is a short tag for downstream consumers
    that want to discriminate without re-parsing: ``"int"``,
    ``"real"``, ``"bool"``, ``"string"``, ``"time"``, ``"typed"``
    (for ``TYPE#value`` forms), ``"raw"`` (catch-all).
    """
    value: str
    kind: str = "raw"


@dataclass(frozen=True)
class VarRef:
    """A simple variable reference (no field or index access).

    ``ref`` is either an ``Address`` (resolved location) or a
    ``TagRef`` (symbolic name); both pass through the existing
    tag resolver.  ST bodies typically use ``TagRef`` because
    they're authored in terms of declared names.
    """
    ref: "Address | TagRef"


@dataclass(frozen=True)
class FieldAccess:
    """Struct field access: ``base.field`` (recursive)."""
    base: "Expression"
    field: str


@dataclass(frozen=True)
class IndexAccess:
    """Array indexing: ``base[i, j, ...]``.

    Multi-dimensional indexing carries one expression per axis;
    single-dim ``a[i]`` has ``indices=(Expression,)``.
    """
    base: "Expression"
    indices: tuple["Expression", ...]


@dataclass(frozen=True)
class UnaryExpr:
    """Unary operation: ``-x``, ``NOT b``."""
    op: UnaryOp
    operand: "Expression"


@dataclass(frozen=True)
class BinaryExpr:
    """Binary operation: ``a + b``, ``x AND y``."""
    op: BinaryOp
    lhs: "Expression"
    rhs: "Expression"


@dataclass(frozen=True)
class FunctionCallExpr:
    """Function or FB-method call used as an expression (returns a value).

    ``name`` is the function or method identifier.  ``positional``
    is a tuple of expressions passed positionally; ``named`` is a
    tuple of (formal-parameter-name, expression) pairs for IEC's
    ``CALL(IN := value)`` form.

    IEC ST allows both forms in the same call; positional come
    before named in the rendered output.  FB instance methods
    render as ``Instance.MethodName(...)`` -- represent that with
    a ``FieldAccess`` chain in the ``name`` slot if needed.
    """
    name: str
    positional: tuple["Expression", ...] = ()
    named: tuple[tuple[str, "Expression"], ...] = ()


#: Union of every expression node.  ``Expression`` is the type
#: name used in field annotations; runtime checks compare against
#: this tuple of concrete classes.
Expression = Union[
    Literal, VarRef, FieldAccess, IndexAccess,
    UnaryExpr, BinaryExpr, FunctionCallExpr,
]


# -----------------------------------------------------------------------------
# Statements
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    """``target := value;`` -- assign a value to a writable variable.

    ``target`` must be a writable expression: ``VarRef``,
    ``FieldAccess``, or ``IndexAccess``.  Other expression kinds
    (literal, binary op, call) are not valid lvalues -- the
    validator enforces this.
    """
    target: Expression
    value: Expression


@dataclass(frozen=True)
class IfStatement:
    """``IF c THEN ... ELSIF c2 THEN ... ELSE ... END_IF``.

    ``branches`` is a tuple of ``(condition, statement_list)``
    pairs.  The first element is the ``IF`` branch; subsequent
    elements are ``ELSIF`` branches.  ``else_branch`` is the
    optional ``ELSE`` body.
    """
    branches: tuple[tuple[Expression, tuple["Statement", ...]], ...]
    else_branch: Optional[tuple["Statement", ...]] = None


@dataclass(frozen=True)
class CaseClause:
    """One ``label_list : statement_list`` clause inside CASE.

    ``labels`` is a tuple of expressions; each is either a literal
    value, a range literal (``Literal("1..5", kind="range")``), or
    an enum reference.  The emitter renders ``label1, label2: ...``.
    """
    labels: tuple[Expression, ...]
    body: tuple["Statement", ...]


@dataclass(frozen=True)
class CaseStatement:
    """``CASE selector OF (clause)+ [ELSE ...] END_CASE``."""
    selector: Expression
    clauses: tuple[CaseClause, ...]
    else_branch: Optional[tuple["Statement", ...]] = None


@dataclass(frozen=True)
class WhileStatement:
    """``WHILE cond DO body END_WHILE``."""
    condition: Expression
    body: tuple["Statement", ...]


@dataclass(frozen=True)
class RepeatStatement:
    """``REPEAT body UNTIL cond END_REPEAT``."""
    body: tuple["Statement", ...]
    until: Expression


@dataclass(frozen=True)
class ForStatement:
    """``FOR var := start TO end [BY step] DO body END_FOR``.

    ``index_var`` is the loop-variable name (must be declared in
    the enclosing POU's locals).  ``step`` defaults to ``None``,
    rendered as omitting the ``BY`` clause (which IEC treats as
    ``BY 1``).
    """
    index_var: str
    start: Expression
    end: Expression
    body: tuple["Statement", ...]
    step: Optional[Expression] = None


@dataclass(frozen=True)
class ReturnStatement:
    """``RETURN;`` -- exit the enclosing POU early."""


@dataclass(frozen=True)
class ExitStatement:
    """``EXIT;`` -- break out of the innermost loop."""


@dataclass(frozen=True)
class ContinueStatement:
    """``CONTINUE;`` -- skip to the next iteration of the innermost loop.

    Added in IEC 3rd edition.  Older compilers may not accept it;
    the conformance doc tracks the version requirement.
    """


@dataclass(frozen=True)
class FunctionCallStatement:
    """A function or FB call used for side effects (no return value
    captured): ``DoIt(in := x, out => y);``.

    ``call`` is the inner ``FunctionCallExpr``; wrapping it lets us
    distinguish "call used as value" from "call used as statement"
    in the AST (same syntax, different semantics).
    """
    call: FunctionCallExpr


@dataclass(frozen=True)
class CommentStatement:
    """A free-form comment line, rendered as ``(* text *)``.

    Used by lowering passes to inject markers / pragmas that don't
    map onto any other ST construct (e.g. an FBD jump that
    couldn't be translated, a vendor-specific hint).  The text is
    inserted verbatim between ``(* `` and `` *)`` -- callers are
    responsible for keeping any embedded ``*)`` out of the body.
    """
    text: str


#: Union of every statement node.
Statement = Union[
    Assignment, IfStatement, CaseStatement,
    WhileStatement, RepeatStatement, ForStatement,
    ReturnStatement, ExitStatement, ContinueStatement,
    FunctionCallStatement, CommentStatement,
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def is_lvalue(expr: Expression) -> bool:
    """True iff ``expr`` is a writable target for ``Assignment``.

    Only ``VarRef`` / ``FieldAccess`` / ``IndexAccess`` (and chains
    thereof) name memory locations; everything else (literals, math
    expressions, function-call results) is not assignable.
    """
    if isinstance(expr, VarRef):
        return True
    if isinstance(expr, FieldAccess):
        return is_lvalue(expr.base)
    if isinstance(expr, IndexAccess):
        return is_lvalue(expr.base)
    return False


def walk_expressions(stmt: Statement):
    """Yield every Expression appearing in ``stmt`` (and nested
    statements), in left-to-right order.

    Used by the resolver pass to rewrite ``TagRef`` references and
    by the validation pass to collect referenced names.
    """
    if isinstance(stmt, Assignment):
        yield from _walk_expr(stmt.target)
        yield from _walk_expr(stmt.value)
    elif isinstance(stmt, IfStatement):
        for cond, body in stmt.branches:
            yield from _walk_expr(cond)
            for s in body:
                yield from walk_expressions(s)
        if stmt.else_branch is not None:
            for s in stmt.else_branch:
                yield from walk_expressions(s)
    elif isinstance(stmt, CaseStatement):
        yield from _walk_expr(stmt.selector)
        for clause in stmt.clauses:
            for lbl in clause.labels:
                yield from _walk_expr(lbl)
            for s in clause.body:
                yield from walk_expressions(s)
        if stmt.else_branch is not None:
            for s in stmt.else_branch:
                yield from walk_expressions(s)
    elif isinstance(stmt, WhileStatement):
        yield from _walk_expr(stmt.condition)
        for s in stmt.body:
            yield from walk_expressions(s)
    elif isinstance(stmt, RepeatStatement):
        for s in stmt.body:
            yield from walk_expressions(s)
        yield from _walk_expr(stmt.until)
    elif isinstance(stmt, ForStatement):
        yield from _walk_expr(stmt.start)
        yield from _walk_expr(stmt.end)
        if stmt.step is not None:
            yield from _walk_expr(stmt.step)
        for s in stmt.body:
            yield from walk_expressions(s)
    elif isinstance(stmt, FunctionCallStatement):
        yield from _walk_expr(stmt.call)
    # ReturnStatement / ExitStatement / ContinueStatement carry no
    # expressions -- nothing to yield.


def _walk_expr(expr: Expression):
    """Yield ``expr`` plus every nested sub-expression."""
    yield expr
    if isinstance(expr, (FieldAccess, IndexAccess)):
        yield from _walk_expr(expr.base)
        if isinstance(expr, IndexAccess):
            for ix in expr.indices:
                yield from _walk_expr(ix)
    elif isinstance(expr, UnaryExpr):
        yield from _walk_expr(expr.operand)
    elif isinstance(expr, BinaryExpr):
        yield from _walk_expr(expr.lhs)
        yield from _walk_expr(expr.rhs)
    elif isinstance(expr, FunctionCallExpr):
        for p in expr.positional:
            yield from _walk_expr(p)
        for _, v in expr.named:
            yield from _walk_expr(v)
    # Literal / VarRef are leaves.
