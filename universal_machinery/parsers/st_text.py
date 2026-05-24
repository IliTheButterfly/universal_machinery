"""IEC 61131-3 §3 Structured Text source-text parser.

Reads ST source text and produces a structured AST in
``universal_machinery.il.st`` shape (the same dataclasses the ST
emitter renders from).  Together with the emitter this closes a
lossless round-trip for ST-authored programs:

    ST source -> parse_st_body(src) -> list[Statement]
                                            |
                                            v
                              il -> emit_st_body(stmts) -> ST source

The parser is hand-rolled (no third-party dependency) so the
project stays self-contained.  Scope covers what the ST emitter
produces verbatim plus a few common dialect quirks.

Public API
----------

``parse_st_body(src: str) -> list[Statement]``
    Parse a top-level statement list (e.g. a POU body's
    contents).  Empty / whitespace-only input returns ``[]``.

``parse_st_expression(src: str) -> Expression``
    Parse a single expression -- handy for unit tests and for
    embedding ST literals inside other tools.

Both raise :class:`StParseError` for syntactic errors, with the
offending token's 1-indexed line and column attached.

Grammar (informal, IEC §3 simplified)
-------------------------------------

::

    statement_list := statement+

    statement := assignment ';'
               | function_call ';'
               | if_stmt
               | case_stmt
               | for_stmt
               | while_stmt
               | repeat_stmt
               | jump ';'
               | label

    assignment := variable ':=' expression
    variable   := name ('.' name | '[' expression (',' expression)* ']')*

    expression := or_expr
    or_expr    := xor_expr ('OR' xor_expr)*
    xor_expr   := and_expr ('XOR' and_expr)*
    and_expr   := eq_expr ('AND' eq_expr)*
    eq_expr    := cmp_expr (('=' | '<>') cmp_expr)*
    cmp_expr   := add_expr (('<' | '>' | '<=' | '>=') add_expr)*
    add_expr   := mul_expr (('+' | '-') mul_expr)*
    mul_expr   := exp_expr (('*' | '/' | 'MOD') exp_expr)*
    exp_expr   := unary_expr ('**' unary_expr)?
    unary_expr := ('NOT' | '-')? primary

    primary    := literal
                | variable_or_call
                | '(' expression ')'
    variable_or_call := name ( '(' arg_list ')' | '.' name | '[' ... ] )*
    arg_list   := (name ':=' expression | expression) (',' ...)*

    if_stmt    := 'IF' expression 'THEN' statement_list
                  ('ELSIF' expression 'THEN' statement_list)*
                  ('ELSE' statement_list)?
                  'END_IF' ';'?
    case_stmt  := 'CASE' expression 'OF'
                  (case_label_list ':' statement_list)+
                  ('ELSE' statement_list)?
                  'END_CASE' ';'?
    for_stmt   := 'FOR' name ':=' expression 'TO' expression
                  ('BY' expression)?
                  'DO' statement_list 'END_FOR' ';'?
    while_stmt := 'WHILE' expression 'DO' statement_list 'END_WHILE' ';'?
    repeat     := 'REPEAT' statement_list 'UNTIL' expression 'END_REPEAT' ';'?
    jump       := 'RETURN' | 'EXIT' | 'CONTINUE' | 'GOTO' name
    label      := name ':'   (not followed by '=' -- assignment uses ':=')

Comments
~~~~~~~~

``(* ... *)`` block comments are recognised and discarded.  IEC
also defines ``// ... \n`` line comments in 3rd edition;
recognised and discarded too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from ..il import (
    Address, Assignment, BinaryExpr, BinaryOp, CaseClause, CaseStatement,
    ContinueStatement, ExitStatement, Expression, FieldAccess, ForStatement,
    FunctionCallExpr, FunctionCallStatement, GotoStatement, IfStatement,
    IndexAccess, LabelStatement, Literal, RepeatStatement, ReturnStatement,
    Statement, TagRef, UnaryExpr, UnaryOp, VarRef, WhileStatement,
)


# -----------------------------------------------------------------------------
# Error
# -----------------------------------------------------------------------------


from ..exceptions import UniversalMachineryError


class StParseError(UniversalMachineryError):
    """Raised when ST source can't be parsed.

    Carries the 1-indexed ``line``/``column`` where the parser
    stopped and the offending token's text.  Use ``str(exc)`` for
    a one-line diagnostic suitable for CLI output.
    """

    def __init__(self, message: str, line: int = 0, column: int = 0,
                 lexeme: str = "") -> None:
        loc = f" at line {line}, column {column}" if line else ""
        super().__init__(f"{message}{loc}"
                          + (f" near {lexeme!r}" if lexeme else ""))
        self.message = message
        self.line = line
        self.column = column
        self.lexeme = lexeme


# -----------------------------------------------------------------------------
# Tokens
# -----------------------------------------------------------------------------


class TokKind(Enum):
    """Lexer token kinds.  Each is a stable enum value used by the
    parser's dispatch tables."""
    # Structural
    IDENT      = "IDENT"
    INT        = "INT"
    REAL       = "REAL"
    STRING     = "STRING"
    TYPED_LIT  = "TYPED_LIT"    # e.g. T#100ms, DT#2026-05-20-..., 16#FF
    DIRECT_REP = "DIRECT_REP"   # IEC §2.4.1.1 ``%IX0.0``, ``%QW1``, ``%MD2.3``
    # Punctuation
    LPAREN     = "("
    RPAREN     = ")"
    LBRACK     = "["
    RBRACK     = "]"
    COMMA      = ","
    SEMI       = ";"
    COLON      = ":"
    DOT        = "."
    ASSIGN     = ":="
    OUTPUT_BIND = "=>"
    # Operators
    PLUS       = "+"
    MINUS      = "-"
    STAR       = "*"
    SLASH      = "/"
    POW        = "**"
    LT         = "<"
    GT         = ">"
    LE         = "<="
    GE         = ">="
    EQ         = "="
    NE         = "<>"
    # Keywords -- separate kind for fast dispatch
    KW_IF      = "IF"
    KW_THEN    = "THEN"
    KW_ELSIF   = "ELSIF"
    KW_ELSE    = "ELSE"
    KW_END_IF  = "END_IF"
    KW_CASE    = "CASE"
    KW_OF      = "OF"
    KW_END_CASE = "END_CASE"
    KW_FOR     = "FOR"
    KW_TO      = "TO"
    KW_BY      = "BY"
    KW_DO      = "DO"
    KW_END_FOR = "END_FOR"
    KW_WHILE   = "WHILE"
    KW_END_WHILE = "END_WHILE"
    KW_REPEAT  = "REPEAT"
    KW_UNTIL   = "UNTIL"
    KW_END_REPEAT = "END_REPEAT"
    KW_RETURN  = "RETURN"
    KW_EXIT    = "EXIT"
    KW_CONTINUE = "CONTINUE"
    KW_GOTO    = "GOTO"
    KW_AND     = "AND"
    KW_OR      = "OR"
    KW_XOR     = "XOR"
    KW_NOT     = "NOT"
    KW_MOD     = "MOD"
    KW_TRUE    = "TRUE"
    KW_FALSE   = "FALSE"
    # End-of-input sentinel
    EOF        = "<EOF>"


_KEYWORDS: dict[str, TokKind] = {
    "IF":         TokKind.KW_IF,
    "THEN":       TokKind.KW_THEN,
    "ELSIF":      TokKind.KW_ELSIF,
    "ELSE":       TokKind.KW_ELSE,
    "END_IF":     TokKind.KW_END_IF,
    "CASE":       TokKind.KW_CASE,
    "OF":         TokKind.KW_OF,
    "END_CASE":   TokKind.KW_END_CASE,
    "FOR":        TokKind.KW_FOR,
    "TO":         TokKind.KW_TO,
    "BY":         TokKind.KW_BY,
    "DO":         TokKind.KW_DO,
    "END_FOR":    TokKind.KW_END_FOR,
    "WHILE":      TokKind.KW_WHILE,
    "END_WHILE":  TokKind.KW_END_WHILE,
    "REPEAT":     TokKind.KW_REPEAT,
    "UNTIL":      TokKind.KW_UNTIL,
    "END_REPEAT": TokKind.KW_END_REPEAT,
    "RETURN":     TokKind.KW_RETURN,
    "EXIT":       TokKind.KW_EXIT,
    "CONTINUE":   TokKind.KW_CONTINUE,
    "GOTO":       TokKind.KW_GOTO,
    "AND":        TokKind.KW_AND,
    "OR":         TokKind.KW_OR,
    "XOR":        TokKind.KW_XOR,
    "NOT":        TokKind.KW_NOT,
    "MOD":        TokKind.KW_MOD,
    "TRUE":       TokKind.KW_TRUE,
    "FALSE":      TokKind.KW_FALSE,
}


@dataclass(frozen=True)
class Token:
    kind: TokKind
    lexeme: str
    line: int
    column: int


# -----------------------------------------------------------------------------
# Lexer
# -----------------------------------------------------------------------------


_IDENT_RE   = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REAL_RE    = re.compile(r"\d+\.\d+(?:[eE][+-]?\d+)?")
_INT_RE     = re.compile(r"\d+")
_TYPED_RE   = re.compile(r"[A-Za-z_][A-Za-z0-9_]*#[A-Za-z0-9_:.\-+]+")
_BASED_RE   = re.compile(r"\d+#[0-9A-Fa-f_]+")    # 16#FF, 2#1010, etc.
_BLOCK_CMT  = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_LINE_CMT   = re.compile(r"//[^\n]*")


def _tokenize(src: str) -> list[Token]:
    """Turn raw source into a list of ``Token``s.

    Block comments ``(* ... *)`` and line comments ``// ...``
    are stripped (replaced with whitespace so column numbers stay
    consistent).  ``\\r\\n`` is normalised to ``\\n``.
    """
    src = src.replace("\r\n", "\n").replace("\r", "\n")

    # Strip comments, replacing each with the equivalent number of
    # blanks so source positions stay accurate.
    def _blank_replace(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))
    src = _BLOCK_CMT.sub(_blank_replace, src)
    src = _LINE_CMT.sub(_blank_replace, src)

    tokens: list[Token] = []
    i = 0
    n = len(src)
    line = 1
    line_start = 0     # byte offset of the most recent newline + 1

    def _col(pos: int) -> int:
        return pos - line_start + 1

    while i < n:
        c = src[i]
        # Whitespace
        if c == "\n":
            line += 1
            line_start = i + 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue

        # Multi-char punctuation first
        if src.startswith(":=", i):
            tokens.append(Token(TokKind.ASSIGN, ":=", line, _col(i))); i += 2
            continue
        if src.startswith("=>", i):
            tokens.append(Token(TokKind.OUTPUT_BIND, "=>", line, _col(i)))
            i += 2
            continue
        if src.startswith("<=", i):
            tokens.append(Token(TokKind.LE, "<=", line, _col(i))); i += 2
            continue
        if src.startswith(">=", i):
            tokens.append(Token(TokKind.GE, ">=", line, _col(i))); i += 2
            continue
        if src.startswith("<>", i):
            tokens.append(Token(TokKind.NE, "<>", line, _col(i))); i += 2
            continue
        if src.startswith("**", i):
            tokens.append(Token(TokKind.POW, "**", line, _col(i))); i += 2
            continue

        # Single-char punctuation
        single = {
            "(": TokKind.LPAREN, ")": TokKind.RPAREN,
            "[": TokKind.LBRACK, "]": TokKind.RBRACK,
            ",": TokKind.COMMA,  ";": TokKind.SEMI,
            ":": TokKind.COLON,  ".": TokKind.DOT,
            "+": TokKind.PLUS,   "-": TokKind.MINUS,
            "*": TokKind.STAR,   "/": TokKind.SLASH,
            "<": TokKind.LT,     ">": TokKind.GT,
            "=": TokKind.EQ,
        }
        if c in single:
            tokens.append(Token(single[c], c, line, _col(i))); i += 1
            continue

        # IEC §2.4.1.1 direct representation: ``%I*`` / ``%Q*`` /
        # ``%M*``.  Grammar: ``%`` + memory class letter (I/Q/M) +
        # optional size letter (X/B/W/D/L) + dotted decimal indices.
        # Consume as a single ``DIRECT_REP`` token so the
        # var-declaration parser can handle ``name AT %IX0.0 : BOOL;``
        # without dancing through three separate tokens.
        if c == "%":
            start = i
            i += 1
            # Memory class letter.
            if i < n and src[i] in "IQMiqm":
                i += 1
                # Optional size letter.
                if i < n and src[i] in "XBWDLxbwdl":
                    i += 1
                # Dotted decimal indices.
                while i < n and (src[i].isdigit() or src[i] == "."):
                    i += 1
                tokens.append(Token(TokKind.DIRECT_REP,
                                     src[start:i], line, _col(start)))
                continue
            # Bare ``%`` with nothing recognisable after it.
            raise StParseError(
                "unexpected '%' (expected IEC direct rep like "
                "%IX0.0 / %QW1 / %MD2.3)",
                line=line, column=_col(start),
            )

        # String literal: single or double quote
        if c in "'\"":
            quote = c
            start = i
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\n":
                    line += 1
                    line_start = i + 1
                i += 1
            if i >= n:
                raise StParseError("unterminated string literal",
                                    line=line, column=_col(start))
            i += 1  # closing quote
            tokens.append(Token(TokKind.STRING, src[start:i],
                                 line, _col(start)))
            continue

        # Typed literal (T#..., DT#..., 16#FF, BOOL#TRUE, ...).
        # The typed-literal form covers both identifier-prefixed
        # (T#100ms) and based-int (16#FF) variants.
        m = _BASED_RE.match(src, i)
        if m:
            tokens.append(Token(TokKind.TYPED_LIT, m.group(0),
                                 line, _col(i)))
            i = m.end()
            continue
        m = _TYPED_RE.match(src, i)
        if m:
            tokens.append(Token(TokKind.TYPED_LIT, m.group(0),
                                 line, _col(i)))
            i = m.end()
            continue

        # Number: try real first
        m = _REAL_RE.match(src, i)
        if m:
            tokens.append(Token(TokKind.REAL, m.group(0), line, _col(i)))
            i = m.end()
            continue
        m = _INT_RE.match(src, i)
        if m:
            tokens.append(Token(TokKind.INT, m.group(0), line, _col(i)))
            i = m.end()
            continue

        # Identifier / keyword.  Case-sensitive: IEC requires
        # uppercase keywords, but real-world tools sometimes
        # accept mixed case; we honour uppercase-only for
        # keyword matching.
        m = _IDENT_RE.match(src, i)
        if m:
            text = m.group(0)
            kind = _KEYWORDS.get(text, TokKind.IDENT)
            tokens.append(Token(kind, text, line, _col(i)))
            i = m.end()
            continue

        raise StParseError(f"unexpected character {c!r}",
                            line=line, column=_col(i))

    tokens.append(Token(TokKind.EOF, "", line, _col(i)))
    return tokens


# -----------------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------------


#: Lookup: TokKind for a binary operator -> (precedence, BinaryOp).
#: Higher precedence binds tighter.
_BINOP_PREC: dict[TokKind, tuple[int, BinaryOp]] = {
    TokKind.KW_OR:  (1, BinaryOp.OR),
    TokKind.KW_XOR: (2, BinaryOp.XOR),
    TokKind.KW_AND: (3, BinaryOp.AND),
    TokKind.EQ:     (4, BinaryOp.EQ),
    TokKind.NE:     (4, BinaryOp.NE),
    TokKind.LT:     (5, BinaryOp.LT),
    TokKind.GT:     (5, BinaryOp.GT),
    TokKind.LE:     (5, BinaryOp.LE),
    TokKind.GE:     (5, BinaryOp.GE),
    TokKind.PLUS:   (6, BinaryOp.ADD),
    TokKind.MINUS:  (6, BinaryOp.SUB),
    TokKind.STAR:   (7, BinaryOp.MUL),
    TokKind.SLASH:  (7, BinaryOp.DIV),
    TokKind.KW_MOD: (7, BinaryOp.MOD),
    TokKind.POW:    (8, BinaryOp.EXP),
}


class _Parser:
    """Token-stream consumer.  One instance per parse() call;
    threaded through helper methods via ``self.pos``."""

    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # ----- low-level helpers -----

    def _peek(self, off: int = 0) -> Token:
        idx = self.pos + off
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind is not TokKind.EOF:
            self.pos += 1
        return tok

    def _consume(self, kind: TokKind, msg: str = "") -> Token:
        tok = self._peek()
        if tok.kind is not kind:
            raise StParseError(
                msg or f"expected {kind.value}, got {tok.kind.value}",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        return self._advance()

    def _match(self, *kinds: TokKind) -> bool:
        return self._peek().kind in kinds

    # ----- top-level -----

    def parse_body(self) -> list[Statement]:
        stmts: list[Statement] = []
        while not self._match(TokKind.EOF):
            stmts.append(self._parse_statement())
        return stmts

    def parse_one_expression(self) -> Expression:
        expr = self._parse_expression()
        if not self._match(TokKind.EOF):
            tok = self._peek()
            raise StParseError(
                "trailing tokens after expression",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        return expr

    # ----- statements -----

    def _parse_statement(self) -> Statement:
        tok = self._peek()
        kind = tok.kind
        if kind is TokKind.KW_IF:       return self._parse_if()
        if kind is TokKind.KW_CASE:     return self._parse_case()
        if kind is TokKind.KW_FOR:      return self._parse_for()
        if kind is TokKind.KW_WHILE:    return self._parse_while()
        if kind is TokKind.KW_REPEAT:   return self._parse_repeat()
        if kind is TokKind.KW_RETURN:
            self._advance()
            self._consume_optional_semi()
            return ReturnStatement()
        if kind is TokKind.KW_EXIT:
            self._advance()
            self._consume_optional_semi()
            return ExitStatement()
        if kind is TokKind.KW_CONTINUE:
            self._advance()
            self._consume_optional_semi()
            return ContinueStatement()
        if kind is TokKind.KW_GOTO:
            self._advance()
            name = self._consume(TokKind.IDENT,
                                   "expected label name after GOTO").lexeme
            self._consume_optional_semi()
            return GotoStatement(label=name)
        if kind is TokKind.IDENT:
            # Could be: label `name:`, assignment `lvalue := ...`,
            # or a function-call statement `name(args);`.
            if (self._peek(1).kind is TokKind.COLON
                    and self._peek(2).kind is not TokKind.EQ):
                # `name : <type>` declarations aren't ST statements
                # -- they're var blocks -- but a bare ``name:`` *is*
                # a label.  IEC requires a label to precede a
                # statement; we accept stand-alone labels.
                name_tok = self._advance()
                self._consume(TokKind.COLON)
                return LabelStatement(name=name_tok.lexeme)
            return self._parse_assignment_or_call_statement()
        raise StParseError(
            f"unexpected token {tok.kind.value} ({tok.lexeme!r})",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )

    def _consume_optional_semi(self) -> None:
        if self._match(TokKind.SEMI):
            self._advance()

    def _parse_assignment_or_call_statement(self) -> Statement:
        # Parse a left-hand-side expression.  If followed by `:=`,
        # it's an assignment; if followed by `;` after a paren
        # expression, the LHS itself was the function-call
        # statement.
        lhs = self._parse_postfix(self._parse_primary())
        if self._match(TokKind.ASSIGN):
            self._advance()
            rhs = self._parse_expression()
            self._consume(TokKind.SEMI, "expected ';' after assignment")
            return Assignment(target=lhs, value=rhs)
        # Call statement: the LHS must be a FunctionCallExpr
        if isinstance(lhs, FunctionCallExpr):
            self._consume(TokKind.SEMI,
                            "expected ';' after function-call statement")
            return FunctionCallStatement(call=lhs)
        tok = self._peek()
        raise StParseError(
            "statement expected ':=' or '(...);'",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )

    def _parse_block_until(self, *terminators: TokKind) -> list[Statement]:
        """Statement list up to (but not consuming) one of
        ``terminators``."""
        stmts: list[Statement] = []
        while not self._match(*terminators, TokKind.EOF):
            stmts.append(self._parse_statement())
        return stmts

    def _parse_if(self) -> IfStatement:
        self._consume(TokKind.KW_IF)
        branches: list[tuple[Expression, tuple[Statement, ...]]] = []
        cond = self._parse_expression()
        self._consume(TokKind.KW_THEN, "expected THEN after IF condition")
        body = self._parse_block_until(
            TokKind.KW_ELSIF, TokKind.KW_ELSE, TokKind.KW_END_IF,
        )
        branches.append((cond, tuple(body)))
        while self._match(TokKind.KW_ELSIF):
            self._advance()
            c = self._parse_expression()
            self._consume(TokKind.KW_THEN, "expected THEN after ELSIF")
            b = self._parse_block_until(
                TokKind.KW_ELSIF, TokKind.KW_ELSE, TokKind.KW_END_IF,
            )
            branches.append((c, tuple(b)))
        else_branch: Optional[tuple[Statement, ...]] = None
        if self._match(TokKind.KW_ELSE):
            self._advance()
            else_branch = tuple(self._parse_block_until(TokKind.KW_END_IF))
        self._consume(TokKind.KW_END_IF, "expected END_IF")
        self._consume_optional_semi()
        return IfStatement(branches=tuple(branches), else_branch=else_branch)

    def _parse_case(self) -> CaseStatement:
        self._consume(TokKind.KW_CASE)
        selector = self._parse_expression()
        self._consume(TokKind.KW_OF, "expected OF after CASE selector")
        clauses: list[CaseClause] = []
        else_branch: Optional[tuple[Statement, ...]] = None
        while not self._match(TokKind.KW_ELSE, TokKind.KW_END_CASE,
                              TokKind.EOF):
            labels: list[Expression] = [self._parse_expression()]
            while self._match(TokKind.COMMA):
                self._advance()
                labels.append(self._parse_expression())
            self._consume(TokKind.COLON,
                            "expected ':' after CASE label list")
            # A clause body runs until either:
            #   - ELSE / END_CASE (the case-level terminators), OR
            #   - the next clause starts (label-list followed by ':')
            # We parse statements one at a time and bail out on the
            # next-clause lookahead.
            body: list[Statement] = []
            while not self._match(TokKind.KW_ELSE, TokKind.KW_END_CASE,
                                    TokKind.EOF):
                if self._is_case_clause_start():
                    break
                body.append(self._parse_statement())
            clauses.append(CaseClause(labels=tuple(labels),
                                        body=tuple(body)))
        if self._match(TokKind.KW_ELSE):
            self._advance()
            else_branch = tuple(self._parse_block_until(TokKind.KW_END_CASE))
        self._consume(TokKind.KW_END_CASE, "expected END_CASE")
        self._consume_optional_semi()
        return CaseStatement(selector=selector, clauses=tuple(clauses),
                              else_branch=else_branch)

    def _is_case_clause_start(self) -> bool:
        """Look ahead for `<label_list>:` to spot the start of a
        new CASE clause inside a block walker."""
        if self._match(TokKind.KW_ELSE, TokKind.KW_END_CASE, TokKind.EOF):
            return False
        # Scan forward over ``label (, label)*`` and check for ':'
        # without being ':='.  We don't want to consume tokens here,
        # so simulate.
        save = self.pos
        try:
            while True:
                tok = self._peek()
                if tok.kind in (TokKind.IDENT, TokKind.INT, TokKind.REAL,
                                  TokKind.STRING, TokKind.TYPED_LIT,
                                  TokKind.KW_TRUE, TokKind.KW_FALSE,
                                  TokKind.MINUS):
                    self.pos += 1
                else:
                    return False
                if self._match(TokKind.COMMA):
                    self.pos += 1
                    continue
                if (self._match(TokKind.COLON)
                        and self._peek(1).kind is not TokKind.EQ):
                    return True
                return False
        finally:
            self.pos = save

    def _parse_for(self) -> ForStatement:
        self._consume(TokKind.KW_FOR)
        name_tok = self._consume(TokKind.IDENT,
                                   "expected loop index after FOR")
        self._consume(TokKind.ASSIGN, "expected ':=' after FOR index")
        start = self._parse_expression()
        self._consume(TokKind.KW_TO, "expected TO in FOR")
        end = self._parse_expression()
        step: Optional[Expression] = None
        if self._match(TokKind.KW_BY):
            self._advance()
            step = self._parse_expression()
        self._consume(TokKind.KW_DO, "expected DO in FOR")
        body = tuple(self._parse_block_until(TokKind.KW_END_FOR))
        self._consume(TokKind.KW_END_FOR, "expected END_FOR")
        self._consume_optional_semi()
        return ForStatement(index_var=name_tok.lexeme,
                              start=start, end=end, body=body, step=step)

    def _parse_while(self) -> WhileStatement:
        self._consume(TokKind.KW_WHILE)
        cond = self._parse_expression()
        self._consume(TokKind.KW_DO, "expected DO in WHILE")
        body = tuple(self._parse_block_until(TokKind.KW_END_WHILE))
        self._consume(TokKind.KW_END_WHILE, "expected END_WHILE")
        self._consume_optional_semi()
        return WhileStatement(condition=cond, body=body)

    def _parse_repeat(self) -> RepeatStatement:
        self._consume(TokKind.KW_REPEAT)
        body = tuple(self._parse_block_until(TokKind.KW_UNTIL))
        self._consume(TokKind.KW_UNTIL, "expected UNTIL in REPEAT")
        until = self._parse_expression()
        self._consume(TokKind.KW_END_REPEAT, "expected END_REPEAT")
        self._consume_optional_semi()
        return RepeatStatement(body=body, until=until)

    # ----- expressions -----

    def _parse_expression(self) -> Expression:
        return self._parse_binary(min_prec=1)

    def _parse_binary(self, min_prec: int) -> Expression:
        left = self._parse_unary()
        while True:
            tok = self._peek()
            info = _BINOP_PREC.get(tok.kind)
            if info is None:
                break
            prec, op = info
            if prec < min_prec:
                break
            self._advance()
            # All these operators are left-associative except ** which
            # IEC defines as right-associative.  Bump the recursion's
            # min_prec to ``prec + 1`` for left-assoc, ``prec`` for
            # right-assoc.
            next_min = prec if op is BinaryOp.EXP else prec + 1
            right = self._parse_binary(next_min)
            left = BinaryExpr(op=op, lhs=left, rhs=right)
        return left

    def _parse_unary(self) -> Expression:
        if self._match(TokKind.KW_NOT):
            self._advance()
            return UnaryExpr(op=UnaryOp.NOT, operand=self._parse_unary())
        if self._match(TokKind.MINUS):
            self._advance()
            operand = self._parse_unary()
            # Fold ``-<int_or_real_literal>`` into a single negative
            # literal so the round-trip is AST-equal to what the
            # builder produces (``lit(-1)`` yields ``Literal("-1")``,
            # not ``UnaryExpr(NEG, Literal("1"))``).
            if (isinstance(operand, Literal)
                    and operand.kind in ("int", "real")
                    and not operand.value.startswith("-")):
                return Literal(value="-" + operand.value, kind=operand.kind)
            return UnaryExpr(op=UnaryOp.NEG, operand=operand)
        return self._parse_postfix(self._parse_primary())

    def _parse_primary(self) -> Expression:
        tok = self._peek()
        kind = tok.kind
        if kind is TokKind.INT:
            self._advance()
            return Literal(value=tok.lexeme, kind="int")
        if kind is TokKind.REAL:
            self._advance()
            return Literal(value=tok.lexeme, kind="real")
        if kind is TokKind.STRING:
            self._advance()
            return Literal(value=tok.lexeme, kind="string")
        if kind is TokKind.TYPED_LIT:
            self._advance()
            return Literal(value=tok.lexeme, kind="typed")
        if kind is TokKind.KW_TRUE:
            self._advance()
            return Literal(value="TRUE", kind="bool")
        if kind is TokKind.KW_FALSE:
            self._advance()
            return Literal(value="FALSE", kind="bool")
        if kind is TokKind.LPAREN:
            self._advance()
            inner = self._parse_expression()
            self._consume(TokKind.RPAREN, "expected ')'")
            return inner
        if kind is TokKind.IDENT:
            self._advance()
            return VarRef(ref=TagRef(name=tok.lexeme))
        raise StParseError(
            f"unexpected token {tok.kind.value} in expression",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )

    def _parse_postfix(self, base: Expression) -> Expression:
        """Walk over ``.field`` / ``[indices]`` / ``(args)`` after a
        primary; produces the right Expression tree."""
        while True:
            tok = self._peek()
            if tok.kind is TokKind.DOT:
                self._advance()
                name = self._consume(TokKind.IDENT,
                                       "expected field name after '.'").lexeme
                base = FieldAccess(base=base, field=name)
            elif tok.kind is TokKind.LBRACK:
                self._advance()
                indices = [self._parse_expression()]
                while self._match(TokKind.COMMA):
                    self._advance()
                    indices.append(self._parse_expression())
                self._consume(TokKind.RBRACK, "expected ']'")
                base = IndexAccess(base=base, indices=tuple(indices))
            elif tok.kind is TokKind.LPAREN:
                # Function call -- only valid when the base is a
                # bare name reference (VarRef -> TagRef).  We
                # capture the name and produce a FunctionCallExpr.
                if not (isinstance(base, VarRef)
                          and isinstance(base.ref, TagRef)):
                    return base
                self._advance()
                positional: list[Expression] = []
                named: list[tuple[str, Expression]] = []
                if not self._match(TokKind.RPAREN):
                    self._parse_call_arg(positional, named)
                    while self._match(TokKind.COMMA):
                        self._advance()
                        self._parse_call_arg(positional, named)
                self._consume(TokKind.RPAREN,
                                "expected ')' to close call args")
                base = FunctionCallExpr(
                    name=base.ref.name,
                    positional=tuple(positional),
                    named=tuple(named),
                )
            else:
                return base

    def _parse_call_arg(self,
                        positional: list[Expression],
                        named: list[tuple[str, Expression]]) -> None:
        """One argument inside a function-call paren list.

        Two shapes:
          - positional: ``expression``
          - named:      ``name := expression`` (IEC input binding) or
                        ``name => expression`` (output binding; we
                        treat both as ``named`` since the IL
                        ``FunctionCallExpr`` stores them in the same
                        slot).
        """
        # Lookahead: if we see IDENT followed by ASSIGN or
        # OUTPUT_BIND, it's a named arg.  Otherwise positional.
        if (self._peek().kind is TokKind.IDENT
                and self._peek(1).kind in (TokKind.ASSIGN,
                                              TokKind.OUTPUT_BIND)):
            name_tok = self._advance()
            self._advance()           # := or =>
            value = self._parse_expression()
            named.append((name_tok.lexeme, value))
        else:
            positional.append(self._parse_expression())


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------


def parse_st_body(src: str) -> list[Statement]:
    """Parse ST source text into a list of ``Statement``s.

    Empty / whitespace-only / comment-only input returns ``[]``.
    Raises :class:`StParseError` on the first syntactic problem.
    """
    tokens = _tokenize(src)
    return _Parser(tokens).parse_body()


def parse_st_expression(src: str) -> Expression:
    """Parse a single ST expression.

    Useful for unit tests and for embedding ST literals inside
    other tools.  Raises :class:`StParseError` if anything follows
    the expression.
    """
    tokens = _tokenize(src)
    return _Parser(tokens).parse_one_expression()


# -----------------------------------------------------------------------------
# Full-program parser (v6)
# -----------------------------------------------------------------------------
#
# Closes the read(.st) gap that ``openplc_backend`` / ``rusty_backend``
# previously raised ``NotImplementedError`` for.  Round-trip:
#
#     IL Program -> emit_program(p) -> ST source
#                                          |
#                                          v
#     IL Program <- parse_program(src) <- ST source
#
# Scope (v6):
#   - PROGRAM / FUNCTION / FUNCTION_BLOCK POUs
#   - All 7 IEC §2.4.3 VAR_* directions (VAR_INPUT / VAR_OUTPUT /
#     VAR_IN_OUT / VAR / VAR_EXTERNAL / VAR_TEMP / VAR_GLOBAL)
#   - Variable declarations: ``name : TYPE;``, optional ``:= init``,
#     optional ``AT %IX0.0`` direct-rep address, optional trailing
#     ``(* comment *)``.  Elementary types AND NamedType (FB instance
#     / UDT reference) both supported.
#   - IEC §2.3.3 TYPE ... END_TYPE blocks: STRUCT / ARRAY / ENUM /
#     SUBRANGE / ALIAS variants.
#   - IEC §2.7 CONFIGURATION / RESOURCE / TASK with VAR_GLOBAL,
#     VAR_ACCESS, VAR_CONFIG, PROGRAM-WITH bindings, and TASK specs.
#   - IEC 3rd-edition OOP: METHOD inside FUNCTION_BLOCK (PUBLIC /
#     PRIVATE / PROTECTED / INTERNAL access, ABSTRACT / OVERRIDE,
#     optional return type), INTERFACE blocks, EXTENDS / IMPLEMENTS
#     / ABSTRACT modifiers on FUNCTION_BLOCK.
#   - IEC §6.7 SFC text: INITIAL_STEP / STEP / END_STEP +
#     TRANSITION FROM ... TO ... ASSIGN ...; END_TRANSITION +
#     ACTION / END_ACTION inline bodies; conditions lower to
#     ContactNO / ContactNC / ParallelGroup ops.
#   - Body: every shape ``parse_body`` already covers (for non-SFC POUs)
#
# Out of scope (still raise StParseError):
#   - CLASS / class-level OOP (3rd-edition CLASS as a top-level POU
#     keyword; FUNCTION_BLOCK + OOP modifiers handle the common
#     OOP shape already)
#   - LD body parsing (no IEC ST equivalent; round-trip LD via XML)
#   - Vendor-style AT clauses (``name AT X001 : BOOL;``) -- these
#     emit as trailing ``(* AT X001 *)`` comments, not inline AT


# Names of the elementary IEC types we recognise verbatim.  Maps
# the uppercase IEC name to ``TagType`` (when it's elementary) or
# to ``None`` (then we treat the name as a NamedType reference to
# a user-defined type / FB type).  Imported lazily at parse time
# to avoid a circular import with ``il`` (which the emitter side
# uses).
def _elementary_typemap():
    from .. import il
    return {t.name: t for t in il.TagType}


# Direction strings <-> VarDirection.  All seven IEC §2.4.3 VAR_*
# variants supported as of v2.  Each maps to a ``VarDirection``
# enum name on the Subroutine; the parser routes them to the
# right field (``inputs`` / ``outputs`` / ``local_vars`` / ...).
_VAR_DIRECTION_KEYWORDS = {
    "VAR_INPUT":    "INPUT",
    "VAR_OUTPUT":   "OUTPUT",
    "VAR_IN_OUT":   "IN_OUT",
    "VAR":          "LOCAL",
    "VAR_EXTERNAL": "EXTERNAL",
    "VAR_TEMP":     "TEMP",
    "VAR_GLOBAL":   "LOCAL",  # global block stores LOCAL-direction
                              # Vars; the *block* type signals the
                              # POU-scope-global routing, not the
                              # per-Var direction enum.
}


_POU_KEYWORDS = {"PROGRAM", "FUNCTION_BLOCK", "FUNCTION"}


def _peek_identifier_keyword(parser: _Parser) -> Optional[str]:
    """Return the *uppercase* lexeme of the next token IFF it's a
    bare identifier, else ``None``.  PROGRAM / VAR / END_VAR etc.
    aren't first-class TokKinds; we match them by uppercased
    lexeme since IEC ST is case-insensitive for keywords.
    """
    tok = parser._peek()
    if tok.kind is not TokKind.IDENT:
        return None
    return tok.lexeme.upper()


def _consume_dotted_path(parser: _Parser) -> str:
    """Consume ``ident (. ident)*`` and return the joined dotted
    string.  Used for VAR_ACCESS / VAR_CONFIG instance paths and
    the ``alias`` half of an access-var declaration.
    """
    parts = [parser._consume(
        TokKind.IDENT, "expected identifier in dotted path"
    ).lexeme]
    while parser._peek().kind is TokKind.DOT:
        parser._advance()
        parts.append(parser._consume(
            TokKind.IDENT, "expected identifier after '.' in dotted path"
        ).lexeme)
    return ".".join(parts)


def _parse_access_var_decl(parser: _Parser):
    """One ``alias : instance_path : type [READ_ONLY|READ_WRITE];``."""
    from ..il.configuration import AccessVar
    alias_tok = parser._consume(
        TokKind.IDENT, "expected access-var alias"
    )
    parser._consume(
        TokKind.COLON, "expected ':' after access-var alias"
    )
    instance_path = _consume_dotted_path(parser)
    parser._consume(
        TokKind.COLON, "expected ':' after access-var instance path"
    )
    data_type = _parse_data_type_after_colon_already_consumed(parser)
    # Optional direction keyword.
    direction = "READ_WRITE"
    kw = _peek_identifier_keyword(parser)
    if kw in ("READ_ONLY", "READ_WRITE"):
        direction = kw
        parser._advance()
    parser._consume(
        TokKind.SEMI, "expected ';' after access-var declaration"
    )
    return AccessVar(
        alias=alias_tok.lexeme,
        instance_path=instance_path,
        data_type=data_type,
        direction=direction,
    )


def _parse_config_var_decl(parser: _Parser):
    """One ``instance_path : type := init_value;`` line."""
    from ..il.configuration import ConfigVar
    instance_path = _consume_dotted_path(parser)
    parser._consume(
        TokKind.COLON, "expected ':' after config-var instance path"
    )
    data_type = _parse_data_type_after_colon_already_consumed(parser)
    initial_value = ""
    if parser._peek().kind is TokKind.ASSIGN:
        parser._advance()
        # Slurp until SEMI (same shape as regular var init).
        start = parser.pos
        while True:
            tok = parser._peek()
            if tok.kind is TokKind.EOF:
                raise StParseError(
                    "unterminated config-var initial value",
                    line=tok.line, column=tok.column, lexeme=tok.lexeme,
                )
            if tok.kind is TokKind.SEMI:
                break
            parser._advance()
        initial_value = " ".join(
            t.lexeme for t in parser.tokens[start:parser.pos]
        )
    parser._consume(
        TokKind.SEMI, "expected ';' after config-var declaration"
    )
    return ConfigVar(
        instance_path=instance_path,
        data_type=data_type,
        initial_value=initial_value,
    )


def _parse_data_type_after_colon_already_consumed(parser: _Parser):
    """Like ``_parse_data_type`` but assumes the ``:`` has been
    consumed already (used inside VAR_ACCESS / VAR_CONFIG
    declarations where two ``:`` appear)."""
    from ..il import NamedType
    type_tok = parser._consume(
        TokKind.IDENT, "expected a type name"
    )
    elementary = _elementary_typemap()
    name = type_tok.lexeme.upper()
    return elementary[name] if name in elementary else NamedType(
        type_tok.lexeme
    )


def _parse_task_spec(parser: _Parser):
    """One ``TASK name(attr := val, ...);`` declaration inside a
    RESOURCE block."""
    from ..il.configuration import TaskSpec
    name_tok = parser._consume(
        TokKind.IDENT, "expected task name after TASK keyword"
    )
    parser._consume(
        TokKind.LPAREN, "expected '(' after task name"
    )
    attrs: dict[str, str] = {}
    while True:
        if parser._peek().kind is TokKind.RPAREN:
            break
        key_tok = parser._consume(
            TokKind.IDENT, "expected attribute name in TASK(...)"
        )
        parser._consume(
            TokKind.ASSIGN, "expected ':=' in TASK attribute"
        )
        # Slurp the value tokens until comma or RPAREN.  Some
        # attrs are typed literals (``T#100ms``), some are plain
        # integers (``PRIORITY``), some are identifiers (event /
        # interrupt names).
        start = parser.pos
        depth = 0
        while True:
            tok = parser._peek()
            if depth == 0 and tok.kind in (
                TokKind.COMMA, TokKind.RPAREN
            ):
                break
            if tok.kind is TokKind.EOF:
                raise StParseError(
                    "unterminated TASK attribute value",
                    line=tok.line, column=tok.column,
                    lexeme=tok.lexeme,
                )
            if tok.kind is TokKind.LPAREN:
                depth += 1
            elif tok.kind is TokKind.RPAREN:
                depth -= 1
            parser._advance()
        attrs[key_tok.lexeme.upper()] = " ".join(
            t.lexeme for t in parser.tokens[start:parser.pos]
        )
        if parser._peek().kind is TokKind.COMMA:
            parser._advance()
            continue
        break
    parser._consume(TokKind.RPAREN, "expected ')' after TASK attributes")
    parser._consume(TokKind.SEMI, "expected ';' after TASK declaration")

    priority = int(attrs.get("PRIORITY", "1"))
    interval = attrs.get("INTERVAL")
    single = attrs.get("SINGLE")
    interrupt = attrs.get("INTERRUPT")
    return TaskSpec(
        name=name_tok.lexeme,
        priority=priority,
        interval=interval,
        single=single,
        interrupt=interrupt,
    )


def _parse_pou_instance_decl(parser: _Parser):
    """One ``PROGRAM <inst> [WITH <task>] : <type>;`` line inside
    a RESOURCE block.  Distinct from a top-level POU declaration
    (which has VAR blocks + body) -- here it's just a binding."""
    from ..il.configuration import PouInstance
    # ``PROGRAM`` already consumed by caller (it dispatched on
    # the keyword).
    inst_name = parser._consume(
        TokKind.IDENT, "expected POU-instance name after PROGRAM"
    )
    task = None
    kw = _peek_identifier_keyword(parser)
    if kw == "WITH":
        parser._advance()
        task = parser._consume(
            TokKind.IDENT, "expected task name after WITH"
        ).lexeme
    parser._consume(
        TokKind.COLON, "expected ':' before POU type name"
    )
    type_tok = parser._consume(
        TokKind.IDENT, "expected POU type name"
    )
    parser._consume(
        TokKind.SEMI, "expected ';' after POU-instance binding"
    )
    return PouInstance(
        name=inst_name.lexeme,
        type_name=type_tok.lexeme,
        task=task,
    )


def _parse_resource_block(parser: _Parser):
    """One ``RESOURCE name ON PLC ... END_RESOURCE`` block."""
    from ..il.configuration import Resource
    parser._advance()  # consume RESOURCE
    name_tok = parser._consume(
        TokKind.IDENT, "expected resource name after RESOURCE"
    )
    # ``ON PLC`` -- two bare identifiers.  IEC allows other
    # resource-type names but ``PLC`` is the conventional one.
    on_kw = _peek_identifier_keyword(parser)
    if on_kw != "ON":
        tok = parser._peek()
        raise StParseError(
            f"expected 'ON' after resource name; got {tok.lexeme!r}",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )
    parser._advance()  # consume ON
    parser._consume(
        TokKind.IDENT, "expected resource type (e.g. PLC) after ON"
    )

    global_vars: list = []
    tasks: list = []
    pou_instances: list = []
    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == "END_RESOURCE":
            parser._advance()
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                "unterminated RESOURCE (missing END_RESOURCE)",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        if kw == "VAR_GLOBAL":
            _, items = _parse_var_block(parser)
            global_vars.extend(items)
        elif kw == "TASK":
            parser._advance()  # consume TASK
            tasks.append(_parse_task_spec(parser))
        elif kw == "PROGRAM":
            parser._advance()  # consume PROGRAM (binding, not POU)
            pou_instances.append(_parse_pou_instance_decl(parser))
        else:
            tok = parser._peek()
            raise StParseError(
                f"unexpected token inside RESOURCE: {tok.lexeme!r}.  "
                f"Expected VAR_GLOBAL / TASK / PROGRAM / END_RESOURCE.",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
    return Resource(
        name=name_tok.lexeme,
        tasks=tasks,
        pou_instances=pou_instances,
        global_vars=global_vars,
    )


def _parse_configuration_block(parser: _Parser):
    """One ``CONFIGURATION name ... END_CONFIGURATION`` block."""
    from ..il.configuration import Configuration
    parser._advance()  # consume CONFIGURATION
    name_tok = parser._consume(
        TokKind.IDENT, "expected configuration name"
    )

    global_vars: list = []
    access_vars: list = []
    config_vars: list = []
    resources: list = []
    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == "END_CONFIGURATION":
            parser._advance()
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                "unterminated CONFIGURATION (missing END_CONFIGURATION)",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        if kw == "VAR_GLOBAL":
            _, items = _parse_var_block(parser)
            global_vars.extend(items)
        elif kw == "VAR_ACCESS":
            parser._advance()  # consume VAR_ACCESS
            while True:
                inner_kw = _peek_identifier_keyword(parser)
                if inner_kw == "END_VAR":
                    parser._advance()
                    break
                if parser._peek().kind is TokKind.EOF:
                    tok = parser._peek()
                    raise StParseError(
                        "unterminated VAR_ACCESS block",
                        line=tok.line, column=tok.column,
                        lexeme=tok.lexeme,
                    )
                access_vars.append(_parse_access_var_decl(parser))
        elif kw == "VAR_CONFIG":
            parser._advance()  # consume VAR_CONFIG
            while True:
                inner_kw = _peek_identifier_keyword(parser)
                if inner_kw == "END_VAR":
                    parser._advance()
                    break
                if parser._peek().kind is TokKind.EOF:
                    tok = parser._peek()
                    raise StParseError(
                        "unterminated VAR_CONFIG block",
                        line=tok.line, column=tok.column,
                        lexeme=tok.lexeme,
                    )
                config_vars.append(_parse_config_var_decl(parser))
        elif kw == "RESOURCE":
            resources.append(_parse_resource_block(parser))
        else:
            tok = parser._peek()
            raise StParseError(
                f"unexpected token inside CONFIGURATION: "
                f"{tok.lexeme!r}.  Expected VAR_GLOBAL / "
                f"VAR_ACCESS / VAR_CONFIG / RESOURCE / "
                f"END_CONFIGURATION.",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
    return Configuration(
        name=name_tok.lexeme,
        resources=resources,
        global_vars=global_vars,
        access_vars=access_vars,
        config_vars=config_vars,
    )


def _parse_signed_int(parser: _Parser) -> int:
    """Consume an optionally-signed integer literal.

    Used for ``ARRAY`` bounds and ``SUBRANGE`` ranges where IEC
    allows negative lower bounds (``SmallInt : INT (-100..100);``).
    """
    sign = 1
    if parser._peek().kind is TokKind.MINUS:
        parser._advance()
        sign = -1
    tok = parser._consume(
        TokKind.INT, "expected an integer bound"
    )
    return sign * int(tok.lexeme)


def _consume_range_dots(parser: _Parser):
    """Consume the ``..`` separator (two ``.`` tokens) between
    range bounds.  Surface a clear error if only one ``.`` is
    present."""
    parser._consume(TokKind.DOT, "expected '..' in range")
    parser._consume(TokKind.DOT, "expected '..' in range")


def _parse_type_block(parser: _Parser) -> list:
    """One ``TYPE ... END_TYPE`` block.  Returns the list of
    UDTs declared inside it.

    Each entry inside the block is one of:

      Name : STRUCT field : type; ... END_STRUCT;
      Name : ARRAY [lo..hi (, lo..hi)*] OF type;
      Name : (V1, V2, ...);
      Name : Base (lo..hi);            -- SUBRANGE
      Name : Base;                     -- ALIAS

    A single TYPE block can declare multiple UDTs (separated by
    further ``Name : ...;`` entries before the closing
    ``END_TYPE``).  The emitter currently writes one UDT per
    block; multiple-UDT input is accepted for robustness.
    """
    from ..il import NamedType
    from ..il.types import (
        AliasType, ArrayType, EnumType, StructType, SubrangeType,
    )
    from ..il.ast import Var, VarDirection

    # Consume the ``TYPE`` keyword (caller has peeked it).
    parser._advance()

    udts: list = []
    elementary = _elementary_typemap()

    while True:
        # Closing ``END_TYPE``?
        kw = _peek_identifier_keyword(parser)
        if kw == "END_TYPE":
            parser._advance()
            return udts
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                "unterminated TYPE block (missing END_TYPE)",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )

        # One UDT declaration: Name : <body> ;
        name_tok = parser._consume(
            TokKind.IDENT, "expected UDT name inside TYPE block"
        )
        parser._consume(TokKind.COLON, "expected ':' after UDT name")

        nxt = _peek_identifier_keyword(parser)
        if nxt == "STRUCT":
            parser._advance()  # consume STRUCT
            members: list = []
            while True:
                kw = _peek_identifier_keyword(parser)
                if kw == "END_STRUCT":
                    parser._advance()
                    break
                if parser._peek().kind is TokKind.EOF:
                    tok = parser._peek()
                    raise StParseError(
                        "unterminated STRUCT (missing END_STRUCT)",
                        line=tok.line, column=tok.column,
                        lexeme=tok.lexeme,
                    )
                # STRUCT members reuse the regular var-decl
                # grammar minus the AT clause (no IEC support
                # for field-level direct rep inside STRUCT).
                members.append(_parse_var_decl(parser, "LOCAL"))
            udts.append(StructType(
                name=name_tok.lexeme,
                members=tuple(members),
            ))

        elif nxt == "ARRAY":
            parser._advance()  # consume ARRAY
            parser._consume(
                TokKind.LBRACK, "expected '[' after ARRAY"
            )
            bounds: list = []
            while True:
                lo = _parse_signed_int(parser)
                _consume_range_dots(parser)
                hi = _parse_signed_int(parser)
                bounds.append((lo, hi))
                if parser._peek().kind is TokKind.COMMA:
                    parser._advance()
                    continue
                break
            parser._consume(
                TokKind.RBRACK, "expected ']' to close ARRAY bounds"
            )
            # ``OF`` is its own TokKind (KW_OF, used in CASE
            # statements too) -- match directly, not via the
            # identifier-keyword helper.
            parser._consume(
                TokKind.KW_OF, "expected 'OF' after ARRAY bounds"
            )
            elem_tok = parser._consume(
                TokKind.IDENT, "expected element type after OF"
            )
            elem_name = elem_tok.lexeme.upper()
            element_type = (
                elementary[elem_name]
                if elem_name in elementary
                else NamedType(elem_tok.lexeme)
            )
            udts.append(ArrayType(
                name=name_tok.lexeme,
                element_type=element_type,
                bounds=tuple(bounds),
            ))

        elif parser._peek().kind is TokKind.LPAREN:
            # ENUM: ``(V1, V2, V3)``
            parser._advance()  # consume LPAREN
            values: list = []
            while True:
                v_tok = parser._consume(
                    TokKind.IDENT, "expected enum value name"
                )
                values.append(v_tok.lexeme)
                if parser._peek().kind is TokKind.COMMA:
                    parser._advance()
                    continue
                break
            parser._consume(
                TokKind.RPAREN, "expected ')' to close enum list"
            )
            udts.append(EnumType(
                name=name_tok.lexeme,
                values=tuple(values),
            ))

        else:
            # Base type identifier; followed by either:
            #   (lo..hi)  -> SubrangeType
            #   nothing   -> AliasType
            base_tok = parser._consume(
                TokKind.IDENT,
                "expected base type name in UDT body"
            )
            base_name = base_tok.lexeme.upper()
            base = (
                elementary[base_name]
                if base_name in elementary
                else NamedType(base_tok.lexeme)
            )
            if parser._peek().kind is TokKind.LPAREN:
                parser._advance()  # consume LPAREN
                lo = _parse_signed_int(parser)
                _consume_range_dots(parser)
                hi = _parse_signed_int(parser)
                parser._consume(
                    TokKind.RPAREN,
                    "expected ')' to close subrange bounds"
                )
                udts.append(SubrangeType(
                    name=name_tok.lexeme,
                    base=base,
                    lower=lo,
                    upper=hi,
                ))
            else:
                udts.append(AliasType(
                    name=name_tok.lexeme,
                    base=base,
                ))

        parser._consume(
            TokKind.SEMI, "expected ';' after UDT declaration"
        )


def _parse_data_type(parser: _Parser):
    """``: TYPE`` -- consume ``COLON`` then an identifier and
    return the resolved ``DataType``.

    Elementary type names (``BOOL`` / ``INT`` / ...) resolve to
    ``TagType``; anything else becomes a ``NamedType`` reference
    (FB instance, UDT, etc.).  TYPE-block declarations *are*
    parsed (since v3) and populate ``Program.user_types``, but
    this helper only resolves the *reference* -- the NamedType
    carries the name only and the resolver pass walks
    ``Program.user_types`` to attach the definition where
    needed.
    """
    from ..il.types import NamedType
    type_tok = parser._consume(
        TokKind.IDENT, "expected a type name after ':'"
    )
    elementary = _elementary_typemap()
    name = type_tok.lexeme.upper()
    if name in elementary:
        return elementary[name]
    # Preserve original casing for NamedType so emit_program can
    # round-trip the name verbatim.
    return NamedType(type_tok.lexeme)


def _parse_var_decl(parser: _Parser, direction: str):
    """One ``name [AT %loc] : TYPE [:= init] ; [(* comment *)]`` line."""
    from ..il.ast import Address, Var, VarDirection
    name_tok = parser._consume(
        TokKind.IDENT,
        "expected a variable name inside VAR block"
    )

    # Optional IEC §2.4.1.1 AT clause: ``name AT %IX0.0 : BOOL;``.
    # Only IEC direct rep (``%``-prefixed) is supported here;
    # vendor-style addresses (CLICK ``X001``, ``Y002``) appear in
    # the ST emit as ``(* AT ... *)`` trailing comments, not
    # inline AT clauses, so they don't reach this code path.
    address = None
    if (parser._peek().kind is TokKind.IDENT
            and parser._peek().lexeme.upper() == "AT"):
        parser._advance()  # consume AT
        addr_tok = parser._consume(
            TokKind.DIRECT_REP,
            "expected an IEC direct-rep address (%IX0.0 / %QW1 / "
            "%MD2.3) after AT keyword"
        )
        address = Address(addr_tok.lexeme)

    parser._consume(TokKind.COLON, "expected ':' after variable name")
    data_type = _parse_data_type(parser)

    initial_value = ""
    if parser._match(TokKind.ASSIGN):
        parser._advance()
        # We treat the initial value as a verbatim text snippet --
        # the IL's ``Var.initial_value`` is a string today, and
        # forcing it through ``parse_st_expression`` would change
        # the shape (and lose source fidelity for e.g. T# literals).
        # Slurp until the terminating semicolon.
        start = parser.pos
        depth = 0
        while True:
            tok = parser._peek()
            if tok.kind is TokKind.EOF:
                raise StParseError(
                    "unterminated initial value", line=tok.line,
                    column=tok.column, lexeme=tok.lexeme,
                )
            if depth == 0 and tok.kind is TokKind.SEMI:
                break
            if tok.kind is TokKind.LPAREN:
                depth += 1
            elif tok.kind is TokKind.RPAREN:
                depth -= 1
            parser._advance()
        init_tokens = parser.tokens[start:parser.pos]
        # Rebuild the source slice from the tokens' lexemes; loses
        # original whitespace but preserves the value's textual
        # shape (Literal-stringified for emit round-trip).
        initial_value = " ".join(t.lexeme for t in init_tokens)

    parser._consume(TokKind.SEMI, "expected ';' after var declaration")

    return Var(
        name=name_tok.lexeme,
        data_type=data_type,
        direction=VarDirection[direction],
        initial_value=initial_value,
        address=address,
    )


def _parse_var_block(parser: _Parser) -> tuple[str, list]:
    """One ``VAR* ... END_VAR`` block.

    Returns ``(block_keyword, list_of_Var)`` where ``block_keyword``
    is the original ``VAR_INPUT`` / ``VAR_OUTPUT`` / ``VAR_IN_OUT``
    / ``VAR`` / ``VAR_EXTERNAL`` / ``VAR_TEMP`` / ``VAR_GLOBAL``
    string.  Caller routes the list to the right Subroutine
    field; the per-``Var`` direction enum is filled in by
    ``_parse_var_decl`` from the
    ``_VAR_DIRECTION_KEYWORDS`` table.
    """
    block_kw = _peek_identifier_keyword(parser)
    if block_kw not in _VAR_DIRECTION_KEYWORDS:
        tok = parser._peek()
        raise StParseError(
            f"expected VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / VAR / "
            f"VAR_EXTERNAL / VAR_TEMP / VAR_GLOBAL; "
            f"got {tok.lexeme!r}",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )
    direction = _VAR_DIRECTION_KEYWORDS[block_kw]
    parser._advance()  # consume VAR*
    vars_: list = []
    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == "END_VAR":
            parser._advance()
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                "unterminated VAR block (missing END_VAR)",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        vars_.append(_parse_var_decl(parser, direction))
    return block_kw, vars_


_ACCESS_SPECS = {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL"}


def _parse_method(parser: _Parser):
    """One ``METHOD ... END_METHOD`` block inside an FB or
    INTERFACE.  Layout::

        METHOD [PUBLIC|PRIVATE|PROTECTED|INTERNAL]
               [ABSTRACT|OVERRIDE]
               <name>
               [: <return_type>]
            <VAR_* blocks>
            <body>          (omitted for ABSTRACT methods inside
                              interfaces)
        END_METHOD
    """
    from ..il.ast import Var, VarDirection
    from ..il.oop import AccessSpec, Method

    # Caller has already peeked ``METHOD``; consume it.
    parser._advance()

    access = AccessSpec.PUBLIC
    abstract = False
    override = False

    # Optional access specifier.
    kw = _peek_identifier_keyword(parser)
    if kw in _ACCESS_SPECS:
        access = AccessSpec[kw]
        parser._advance()
        kw = _peek_identifier_keyword(parser)

    # Optional ABSTRACT / OVERRIDE qualifier (mutually exclusive
    # by IEC, but accept either ordering).
    if kw == "ABSTRACT":
        abstract = True
        parser._advance()
        kw = _peek_identifier_keyword(parser)
    elif kw == "OVERRIDE":
        override = True
        parser._advance()
        kw = _peek_identifier_keyword(parser)

    name_tok = parser._consume(
        TokKind.IDENT, "expected method name"
    )

    # Optional return type.
    return_type = None
    if parser._peek().kind is TokKind.COLON:
        parser._advance()
        return_type = _parse_data_type_after_colon_already_consumed(parser)

    inputs: list = []
    outputs: list = []
    in_outs: list = []
    local_vars: list = []
    _routing = {
        "VAR_INPUT":  inputs,
        "VAR_OUTPUT": outputs,
        "VAR_IN_OUT": in_outs,
        "VAR":        local_vars,
    }
    while True:
        nxt = _peek_identifier_keyword(parser)
        if nxt in _routing:
            block_kw, items = _parse_var_block(parser)
            _routing[block_kw].extend(items)
        else:
            break

    body_stmts: list = []
    while True:
        nxt = _peek_identifier_keyword(parser)
        if nxt == "END_METHOD":
            parser._advance()
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                "unterminated METHOD (missing END_METHOD)",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        body_stmts.append(parser._parse_statement())

    return Method(
        name=name_tok.lexeme,
        access=access,
        abstract=abstract,
        override=override,
        inputs=inputs,
        outputs=outputs,
        in_outs=in_outs,
        local_vars=local_vars,
        return_type=return_type,
        st_body=body_stmts if body_stmts else None,
    )


def _parse_interface_block(parser: _Parser):
    """One ``INTERFACE Name [EXTENDS Parent[, Other]*] ...
    END_INTERFACE`` block.  Body is a list of METHOD signatures
    (no body)."""
    from ..il.oop import Interface

    parser._advance()  # consume INTERFACE
    name_tok = parser._consume(
        TokKind.IDENT, "expected interface name"
    )

    methods: list = []
    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == "END_INTERFACE":
            parser._advance()
            break
        if kw == "METHOD":
            methods.append(_parse_method(parser))
            continue
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                "unterminated INTERFACE (missing END_INTERFACE)",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        tok = parser._peek()
        raise StParseError(
            f"unexpected token inside INTERFACE: {tok.lexeme!r}.  "
            f"Expected METHOD or END_INTERFACE.",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )

    return Interface(
        name=name_tok.lexeme,
        methods=methods,
    )


_SFC_BODY_KEYWORDS = {"INITIAL_STEP", "STEP", "TRANSITION", "ACTION"}


def _expression_to_ld_ops(expr):
    """Lower an ST ``Expression`` AST to a tuple of IL LD ops.

    Mirrors the helper in ``parsers.plcopen_xml`` so SFC transition
    conditions round-trip via the same lowering rules: ``VarRef``
    -> ``ContactNO``, ``NOT VarRef`` -> ``ContactNC``, ``AND`` ->
    concatenation, ``OR`` -> ``ParallelGroup``.  Returns ``None``
    when the expression doesn't fit the LD subset -- caller falls
    back to a single ``ContactNO`` carrying the raw text."""
    from ..il.ops import ContactNO, ContactNC, ParallelGroup
    from ..il.ast import Address

    def _operand(addr_expr):
        if isinstance(addr_expr, VarRef):
            ref = addr_expr.ref
            if isinstance(ref, (Address, TagRef)):
                return ref
        return None

    if isinstance(expr, VarRef):
        op_addr = _operand(expr)
        if op_addr is None:
            return None
        return (ContactNO(op_addr),)

    if isinstance(expr, UnaryExpr) and expr.op is UnaryOp.NOT:
        inner = _expression_to_ld_ops(expr.operand)
        if inner is None:
            return None
        if len(inner) == 1 and isinstance(inner[0], ContactNO):
            return (ContactNC(inner[0].address),)
        if len(inner) == 1 and isinstance(inner[0], ContactNC):
            return (ContactNO(inner[0].address),)
        return None

    if isinstance(expr, BinaryExpr):
        if expr.op is BinaryOp.AND:
            lhs_ops = _expression_to_ld_ops(expr.lhs)
            rhs_ops = _expression_to_ld_ops(expr.rhs)
            if lhs_ops is None or rhs_ops is None:
                return None
            return lhs_ops + rhs_ops
        if expr.op is BinaryOp.OR:
            lhs_ops = _expression_to_ld_ops(expr.lhs)
            rhs_ops = _expression_to_ld_ops(expr.rhs)
            if lhs_ops is None or rhs_ops is None:
                return None

            def _branches(ops):
                if (len(ops) == 1
                        and isinstance(ops[0], ParallelGroup)):
                    return list(ops[0].branches)
                return [tuple(ops)]
            branches = tuple(_branches(lhs_ops) + _branches(rhs_ops))
            return (ParallelGroup(branches=branches),)

    return None


def _parse_time_ms(parser: _Parser) -> int:
    """Parse a ``T#<int>ms`` typed literal and return the
    milliseconds value.  The SFC emit path always renders action
    times in ``ms``; this parser is intentionally narrow."""
    tok = parser._consume(
        TokKind.TYPED_LIT, "expected T#<int>ms time literal"
    )
    text = tok.lexeme
    if not text.upper().startswith("T#"):
        raise StParseError(
            f"expected T# time literal, got {text!r}",
            line=tok.line, column=tok.column, lexeme=text,
        )
    body = text[2:]
    if body.lower().endswith("ms"):
        num = body[:-2]
    else:
        raise StParseError(
            f"unsupported time literal {text!r}; only T#<int>ms is "
            "accepted by the SFC text parser",
            line=tok.line, column=tok.column, lexeme=text,
        )
    try:
        return int(num)
    except ValueError as e:
        raise StParseError(
            f"non-integer time value in {text!r}",
            line=tok.line, column=tok.column, lexeme=text,
        ) from e


def _parse_sfc_step_list(parser: _Parser) -> tuple[str, ...]:
    """Parse either ``Name`` or ``(Name1, Name2, ...)`` -- used for
    transition ``FROM`` / ``TO`` operands (simultaneous div / conv).
    """
    if parser._peek().kind is TokKind.LPAREN:
        parser._advance()
        names = [parser._consume(
            TokKind.IDENT, "expected step name in step list"
        ).lexeme]
        while parser._peek().kind is TokKind.COMMA:
            parser._advance()
            names.append(parser._consume(
                TokKind.IDENT, "expected step name after ',' in step list"
            ).lexeme)
        parser._consume(TokKind.RPAREN, "expected ')' closing step list")
        return tuple(names)
    return (parser._consume(
        TokKind.IDENT, "expected step name"
    ).lexeme,)


def _parse_sfc_step_action(parser: _Parser):
    """One ``actname(qualifier[, T#time]);`` line inside a STEP."""
    from ..il.sfc import Action
    target_tok = parser._consume(
        TokKind.IDENT, "expected action target identifier"
    )
    parser._consume(TokKind.LPAREN, "expected '(' after action target")
    qual_tok = parser._consume(
        TokKind.IDENT, "expected action qualifier (N / R / S / L / ...)"
    )
    time_ms = None
    if parser._peek().kind is TokKind.COMMA:
        parser._advance()
        time_ms = _parse_time_ms(parser)
    parser._consume(TokKind.RPAREN, "expected ')' closing action call")
    parser._consume(TokKind.SEMI, "expected ';' after action call")
    return Action(
        qualifier=qual_tok.lexeme.upper(),
        target=target_tok.lexeme,
        time_ms=time_ms,
    )


def _parse_sfc_step(parser: _Parser, initial: bool):
    """One ``[INITIAL_]STEP Name: ... END_STEP`` block."""
    from ..il.sfc import Step
    parser._advance()  # consume STEP / INITIAL_STEP
    name_tok = parser._consume(
        TokKind.IDENT, "expected step name"
    )
    parser._consume(TokKind.COLON, "expected ':' after step name")
    actions: list = []
    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == "END_STEP":
            parser._advance()
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                f"unterminated STEP {name_tok.lexeme!r} (missing END_STEP)",
                line=tok.line, column=tok.column,
                lexeme=tok.lexeme,
            )
        actions.append(_parse_sfc_step_action(parser))
    return Step(
        name=name_tok.lexeme,
        initial=initial,
        actions=tuple(actions),
    )


def _parse_sfc_transition(parser: _Parser):
    """One ``TRANSITION FROM <from> TO <to> := <expr>;
    END_TRANSITION`` block."""
    from ..il.sfc import Transition
    from ..il.ops import ContactNO

    parser._advance()  # consume TRANSITION
    kw_tok = parser._peek()
    if kw_tok.lexeme.upper() != "FROM":
        raise StParseError(
            "expected FROM after TRANSITION",
            line=kw_tok.line, column=kw_tok.column,
            lexeme=kw_tok.lexeme,
        )
    parser._advance()
    from_steps = _parse_sfc_step_list(parser)

    # ``TO`` is a first-class keyword (TokKind.KW_TO) since it's
    # also used in FOR loops; match it by token kind, not by
    # ``_peek_identifier_keyword`` (which only sees IDENT tokens).
    kw_tok = parser._peek()
    if kw_tok.kind is not TokKind.KW_TO:
        raise StParseError(
            "expected TO in TRANSITION",
            line=kw_tok.line, column=kw_tok.column,
            lexeme=kw_tok.lexeme,
        )
    parser._advance()
    to_steps = _parse_sfc_step_list(parser)

    parser._consume(
        TokKind.ASSIGN, "expected ':=' before transition condition"
    )
    expr = parser._parse_expression()
    parser._consume(TokKind.SEMI, "expected ';' after transition condition")

    kw_tok = parser._peek()
    if _peek_identifier_keyword(parser) != "END_TRANSITION":
        raise StParseError(
            "expected END_TRANSITION",
            line=kw_tok.line, column=kw_tok.column,
            lexeme=kw_tok.lexeme,
        )
    parser._advance()

    # Lower expression to LD-style ops where possible; fall back to
    # a single ContactNO carrying the literal/expression text.  TRUE
    # collapses to an empty condition (matches the emit-side
    # ``_fmt_gate`` convention).
    condition_ops: tuple = ()
    if isinstance(expr, Literal) and expr.value.upper() == "TRUE":
        condition_ops = ()
    else:
        lowered = _expression_to_ld_ops(expr)
        if lowered is not None:
            condition_ops = lowered
        else:
            # Fall back: derive a textual placeholder from the
            # rebuilt expression source.  For a single VarRef of
            # unresolvable type this still falls through to a raw
            # contact.
            if isinstance(expr, VarRef):
                condition_ops = (ContactNO(expr.ref),)
            else:
                # Best-effort: render the expression via the emitter
                # to keep the raw text available.
                from ..emitters.st import emit_expression
                condition_ops = (
                    ContactNO(TagRef(name=emit_expression(expr))),
                )

    return Transition(
        from_steps=from_steps,
        to_steps=to_steps,
        condition=condition_ops,
    )


def _parse_sfc_action_block(parser: _Parser):
    """One ``ACTION Name: <body> END_ACTION`` block.  Returns
    ``(name, body_stmts)`` so the caller can attach the body to the
    matching step action's ``inline_body``."""
    parser._advance()  # consume ACTION
    name_tok = parser._consume(
        TokKind.IDENT, "expected action block name"
    )
    parser._consume(TokKind.COLON, "expected ':' after action name")
    body_stmts: list = []
    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == "END_ACTION":
            parser._advance()
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                f"unterminated ACTION {name_tok.lexeme!r} "
                "(missing END_ACTION)",
                line=tok.line, column=tok.column,
                lexeme=tok.lexeme,
            )
        body_stmts.append(parser._parse_statement())
    return name_tok.lexeme, tuple(body_stmts)


def _parse_sfc_body(parser: _Parser, end_kw: str):
    """Parse a POU body as an IEC §6.7 SFC text network.

    Loops until the enclosing POU's ``END_<kind>`` keyword.
    After collecting steps / transitions / action-blocks, any
    step action whose ``target`` matches an action-block name
    gets that block's statements moved onto ``inline_body``
    (and ``target`` cleared) -- this reverses the emitter's
    synthesised-name handling so inline-bodied actions round-
    trip cleanly."""
    from dataclasses import replace
    from ..il.sfc import SfcNetwork

    steps: list = []
    transitions: list = []
    action_bodies: dict = {}

    while True:
        kw = _peek_identifier_keyword(parser)
        if kw == end_kw:
            break
        if parser._peek().kind is TokKind.EOF:
            tok = parser._peek()
            raise StParseError(
                f"unterminated SFC body (missing {end_kw})",
                line=tok.line, column=tok.column,
                lexeme=tok.lexeme,
            )
        if kw == "INITIAL_STEP":
            steps.append(_parse_sfc_step(parser, initial=True))
        elif kw == "STEP":
            steps.append(_parse_sfc_step(parser, initial=False))
        elif kw == "TRANSITION":
            transitions.append(_parse_sfc_transition(parser))
        elif kw == "ACTION":
            name, body = _parse_sfc_action_block(parser)
            action_bodies[name] = body
        else:
            tok = parser._peek()
            raise StParseError(
                f"unexpected token {tok.lexeme!r} in SFC body; "
                "expected STEP / INITIAL_STEP / TRANSITION / "
                f"ACTION / {end_kw}",
                line=tok.line, column=tok.column,
                lexeme=tok.lexeme,
            )

    # Attach inline-action bodies back onto the matching step actions.
    if action_bodies:
        new_steps: list = []
        for step in steps:
            new_actions = []
            for action in step.actions:
                if (isinstance(action.target, str)
                        and action.target in action_bodies):
                    new_actions.append(replace(
                        action,
                        target="",
                        inline_body=action_bodies[action.target],
                    ))
                else:
                    new_actions.append(action)
            new_steps.append(replace(step, actions=tuple(new_actions)))
        steps = new_steps

    return SfcNetwork(steps=steps, transitions=transitions)


def _parse_pou(parser: _Parser):
    """One ``PROGRAM`` / ``FUNCTION`` / ``FUNCTION_BLOCK`` declaration.

    The interpretive scope at this point in the input is the bare
    POU keyword (``PROGRAM`` / ``FUNCTION`` / ``FUNCTION_BLOCK``)
    followed by name, optional return type for FUNCTION, optional
    VAR blocks, the body, and the matching ``END_*`` keyword.

    FUNCTION_BLOCK additionally accepts:
      - optional ``ABSTRACT`` qualifier between the kind keyword
        and the name (``FUNCTION_BLOCK ABSTRACT Name``)
      - optional ``EXTENDS Parent`` for single inheritance
      - optional ``IMPLEMENTS I1, I2, ...`` for interface impl
      - METHOD blocks inside the body (parsed into
        ``Subroutine.methods``)

    Returns the constructed ``Subroutine``.
    """
    from ..il.ast import PouKind, Subroutine

    kw = _peek_identifier_keyword(parser)
    if kw not in _POU_KEYWORDS:
        tok = parser._peek()
        raise StParseError(
            f"expected POU keyword (PROGRAM / FUNCTION / "
            f"FUNCTION_BLOCK); got {tok.lexeme!r}",
            line=tok.line, column=tok.column, lexeme=tok.lexeme,
        )
    parser._advance()  # consume the POU kind keyword
    pou_kind = {
        "PROGRAM":        PouKind.PROGRAM,
        "FUNCTION":       PouKind.FUNCTION,
        "FUNCTION_BLOCK": PouKind.FUNCTION_BLOCK,
    }[kw]
    end_kw = {
        "PROGRAM":        "END_PROGRAM",
        "FUNCTION":       "END_FUNCTION",
        "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
    }[kw]

    # ``FUNCTION_BLOCK ABSTRACT Name`` -- ABSTRACT before the name.
    abstract = False
    if (kw == "FUNCTION_BLOCK"
            and _peek_identifier_keyword(parser) == "ABSTRACT"):
        abstract = True
        parser._advance()

    name_tok = parser._consume(
        TokKind.IDENT, "expected POU name after declaration keyword"
    )
    name = name_tok.lexeme

    # FUNCTION has a required ``: ReturnType`` after the name.
    return_type = None
    if kw == "FUNCTION":
        parser._consume(TokKind.COLON, "expected ':' for FUNCTION return type")
        return_type = _parse_data_type(parser)

    # ``FUNCTION_BLOCK Name EXTENDS Parent`` (single inheritance).
    extends = None
    if (kw == "FUNCTION_BLOCK"
            and _peek_identifier_keyword(parser) == "EXTENDS"):
        parser._advance()
        ext_tok = parser._consume(
            TokKind.IDENT, "expected parent FB name after EXTENDS"
        )
        extends = ext_tok.lexeme

    # ``FUNCTION_BLOCK Name IMPLEMENTS I1, I2, ...`` (multi).
    implements: list = []
    if (kw == "FUNCTION_BLOCK"
            and _peek_identifier_keyword(parser) == "IMPLEMENTS"):
        parser._advance()
        while True:
            impl_tok = parser._consume(
                TokKind.IDENT,
                "expected interface name after IMPLEMENTS"
            )
            implements.append(impl_tok.lexeme)
            if parser._peek().kind is TokKind.COMMA:
                parser._advance()
                continue
            break

    inputs: list = []
    outputs: list = []
    in_outs: list = []
    local_vars: list = []
    external_vars: list = []
    temp_vars: list = []
    global_vars: list = []

    # Zero or more VAR* blocks.  All seven IEC §2.4.3 VAR_*
    # variants are accepted as of v2 -- each routes to the right
    # Subroutine field by *block keyword*, not by per-Var
    # direction (because VAR_GLOBAL stores LOCAL-direction Vars
    # but routes to ``global_vars``).
    _block_routing = {
        "VAR_INPUT":    inputs,
        "VAR_OUTPUT":   outputs,
        "VAR_IN_OUT":   in_outs,
        "VAR":          local_vars,
        "VAR_EXTERNAL": external_vars,
        "VAR_TEMP":     temp_vars,
        "VAR_GLOBAL":   global_vars,
    }
    while True:
        nxt = _peek_identifier_keyword(parser)
        if nxt in _VAR_DIRECTION_KEYWORDS:
            block_kw, items = _parse_var_block(parser)
            _block_routing[block_kw].extend(items)
        else:
            break

    # Optional METHOD blocks (FUNCTION_BLOCK only per IEC 3rd ed.,
    # but we don't gate -- if a user puts METHOD inside PROGRAM
    # we'll parse it; validation flags it later).
    methods: list = []
    while _peek_identifier_keyword(parser) == "METHOD":
        methods.append(_parse_method(parser))

    # Body: peek the first keyword to decide whether this is an
    # SFC body (INITIAL_STEP / STEP open the network) or an ordinary
    # ST statement body.  Empty body: just consume END_<kind>.
    sfc_net = None
    body_stmts: list = []
    first_kw = _peek_identifier_keyword(parser)
    if first_kw in {"INITIAL_STEP", "STEP"}:
        sfc_net = _parse_sfc_body(parser, end_kw)
        # _parse_sfc_body stops at end_kw without consuming it
        parser._advance()
    else:
        while True:
            nxt = _peek_identifier_keyword(parser)
            if nxt == end_kw:
                parser._advance()
                break
            if parser._peek().kind is TokKind.EOF:
                tok = parser._peek()
                raise StParseError(
                    f"unterminated POU (missing {end_kw})",
                    line=tok.line, column=tok.column, lexeme=tok.lexeme,
                )
            body_stmts.append(parser._parse_statement())

    return Subroutine(
        name=name,
        kind=pou_kind,
        main=(pou_kind is PouKind.PROGRAM),
        inputs=inputs,
        outputs=outputs,
        in_outs=in_outs,
        local_vars=local_vars,
        external_vars=external_vars,
        temp_vars=temp_vars,
        global_vars=global_vars,
        return_type=return_type,
        st_body=body_stmts if body_stmts else None,
        sfc=sfc_net,
        methods=methods,
        extends=extends,
        implements=implements,
        abstract=abstract,
    )


def parse_program(src: str):
    """Parse ST source text into an IL ``Program``.

    Closes the read(.st) gap for ``openplc_backend`` /
    ``rusty_backend``.  Scope (v6):

      - PROGRAM / FUNCTION / FUNCTION_BLOCK POUs
      - All seven IEC §2.4.3 VAR_* directions: VAR_INPUT /
        VAR_OUTPUT / VAR_IN_OUT / VAR / VAR_EXTERNAL /
        VAR_TEMP / VAR_GLOBAL
      - IEC §2.4.1.1 AT clauses on variables:
        ``name AT %IX0.0 : BOOL;``
      - IEC §2.3.3 TYPE ... END_TYPE blocks: STRUCT / ARRAY /
        ENUM / SUBRANGE / ALIAS variants.  Multiple UDTs per
        block accepted; ``Program.user_types`` populated.
      - IEC §2.7 CONFIGURATION / RESOURCE / TASK blocks with
        VAR_GLOBAL, VAR_ACCESS, VAR_CONFIG, PROGRAM instances,
        and TASK specs (priority/interval/single); populated
        into ``Program.configurations``.
      - IEC 3rd-edition OOP: METHOD inside FUNCTION_BLOCK
        (with access specs PUBLIC/PRIVATE/PROTECTED/INTERNAL,
        ABSTRACT, OVERRIDE, optional return type, VAR blocks
        and body), INTERFACE blocks, EXTENDS (single
        inheritance), IMPLEMENTS (multiple), ABSTRACT
        FUNCTION_BLOCK.  Populated into ``Subroutine.methods``
        / ``.extends`` / ``.implements`` / ``.abstract`` and
        ``Program.interfaces``.
      - IEC §6.7 SFC text representation: INITIAL_STEP /
        STEP / END_STEP (with ``action_target(qualifier
        [, T#<ms>ms]);`` action calls), TRANSITION FROM ...
        TO ... := <expr>; END_TRANSITION (single or
        ``(a, b)`` step lists for simultaneous div / conv),
        ACTION ... END_ACTION (inline-body action blocks
        re-attached to the corresponding step action's
        ``inline_body``).  Populated into ``Subroutine.sfc``.
        Transition conditions lower to ``ContactNO`` /
        ``ContactNC`` / ``ParallelGroup`` via the same AND /
        OR / NOT subset the PLCopen XML reader recognises;
        non-LD shapes fall back to a textual placeholder.
      - Body via the existing statement parser (for non-SFC POUs)
      - Multiple POUs in one source

    Not yet supported (raise ``StParseError``):

      - CLASS / EXTENDS at the class level (3rd-edition OOP
        beyond FUNCTION_BLOCK)
      - macroStep / jumpStep (lossless via PLCopen XML
        ``<macroStep>`` / ``<jumpStep>``; in ST text they
        appear as documenting comments rather than first-class
        constructs, so the parser treats them as plain steps)
      - Vendor-style AT clauses (``name AT X001 : BOOL;`` --
        these appear as trailing ``(* AT X001 *)`` comments on
        the emit side, not inline AT, so this scope is honest)

    Each remaining gap is a follow-up slice; the StParseError
    message points to which scope item you've hit.
    """
    from ..il.ast import Program

    tokens = _tokenize(src)
    parser = _Parser(tokens)

    subroutines: list = []
    user_types: list = []
    configurations: list = []
    interfaces: list = []
    while not parser._match(TokKind.EOF):
        kw = _peek_identifier_keyword(parser)
        if kw in _POU_KEYWORDS:
            subroutines.append(_parse_pou(parser))
        elif kw == "TYPE":
            user_types.extend(_parse_type_block(parser))
        elif kw == "CONFIGURATION":
            configurations.append(_parse_configuration_block(parser))
        elif kw == "INTERFACE":
            interfaces.append(_parse_interface_block(parser))
        else:
            tok = parser._peek()
            raise StParseError(
                f"expected POU keyword at program scope; got "
                f"{tok.lexeme!r}",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )

    return Program(
        subroutines=subroutines,
        user_types=user_types,
        configurations=configurations,
        interfaces=interfaces,
    )
