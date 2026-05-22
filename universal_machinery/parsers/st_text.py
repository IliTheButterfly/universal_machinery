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
