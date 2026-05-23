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
# Full-program parser (v1)
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
# Scope (v1):
#   - PROGRAM / FUNCTION / FUNCTION_BLOCK POUs
#   - VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / VAR (local) blocks
#   - Variable declarations: ``name : TYPE;``, optional ``:= init``,
#     optional trailing ``(* comment *)`` -- elementary types AND
#     NamedType (FB instance / UDT reference) both supported
#   - Body: every shape ``parse_body`` already covers
#
# Out of scope (v1; raise StParseError):
#   - VAR_EXTERNAL / VAR_TEMP / VAR_GLOBAL
#   - AT clauses (``name AT %IX0.0 : BOOL;``)
#   - TYPE ... END_TYPE blocks (UDTs)
#   - CONFIGURATION / RESOURCE / TASK
#   - METHOD / INTERFACE / EXTENDS / IMPLEMENTS / ABSTRACT
#   - SFC text representation
#   - LD body parsing (no IEC ST equivalent; round-trip LD via XML)


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
    (FB instance, UDT, etc.).  TYPE-block declarations aren't
    parsed in v1, so the NamedType won't have its definition
    attached -- callers that need the definition must pre-populate
    ``Program.user_types`` and/or rely on a separate parser pass.
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


def _parse_pou(parser: _Parser):
    """One ``PROGRAM`` / ``FUNCTION`` / ``FUNCTION_BLOCK`` declaration.

    The interpretive scope at this point in the input is the bare
    POU keyword (``PROGRAM`` / ``FUNCTION`` / ``FUNCTION_BLOCK``)
    followed by name, optional return type for FUNCTION, optional
    VAR blocks, the body, and the matching ``END_*`` keyword.

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

    name_tok = parser._consume(
        TokKind.IDENT, "expected POU name after declaration keyword"
    )
    name = name_tok.lexeme

    # FUNCTION has a required ``: ReturnType`` after the name.
    return_type = None
    if kw == "FUNCTION":
        parser._consume(TokKind.COLON, "expected ':' for FUNCTION return type")
        return_type = _parse_data_type(parser)

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

    # Body: every statement until ``END_<kind>``.
    body_stmts: list = []
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
        st_body=body_stmts,
    )


def parse_program(src: str):
    """Parse ST source text into an IL ``Program``.

    Closes the read(.st) gap for ``openplc_backend`` /
    ``rusty_backend``.  Scope (v3):

      - PROGRAM / FUNCTION / FUNCTION_BLOCK POUs
      - All seven IEC §2.4.3 VAR_* directions: VAR_INPUT /
        VAR_OUTPUT / VAR_IN_OUT / VAR / VAR_EXTERNAL /
        VAR_TEMP / VAR_GLOBAL
      - IEC §2.4.1.1 AT clauses on variables:
        ``name AT %IX0.0 : BOOL;``
      - IEC §2.3.3 TYPE ... END_TYPE blocks: STRUCT / ARRAY /
        ENUM / SUBRANGE / ALIAS variants.  Multiple UDTs per
        block accepted; ``Program.user_types`` populated.
      - Body via the existing statement parser
      - Multiple POUs in one source

    Not yet supported (raise ``StParseError``):

      - CONFIGURATION / RESOURCE / TASK
      - METHOD / INTERFACE / EXTENDS / IMPLEMENTS / ABSTRACT
      - SFC text representation
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
    while not parser._match(TokKind.EOF):
        kw = _peek_identifier_keyword(parser)
        if kw in _POU_KEYWORDS:
            subroutines.append(_parse_pou(parser))
        elif kw == "TYPE":
            user_types.extend(_parse_type_block(parser))
        elif kw == "CONFIGURATION":
            tok = parser._peek()
            raise StParseError(
                "CONFIGURATION ... END_CONFIGURATION block parsing "
                "is not yet supported by parse_program (v1).",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        elif kw == "INTERFACE":
            tok = parser._peek()
            raise StParseError(
                "INTERFACE ... END_INTERFACE block parsing is not "
                "yet supported by parse_program (v1).",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )
        else:
            tok = parser._peek()
            raise StParseError(
                f"expected POU keyword at program scope; got "
                f"{tok.lexeme!r}",
                line=tok.line, column=tok.column, lexeme=tok.lexeme,
            )

    return Program(subroutines=subroutines, user_types=user_types)
