"""HCL expression AST and parser.

python-hcl2 returns expressions as strings (``"${aws_sqs_queue.q.arn}"``). This
module parses them into a small AST that ``evaluate`` can walk deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .lexer import LexError, Tok, heredoc_content, lex, scan_interp


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class Template:
    parts: tuple[Any, ...]  # str | expression node | Directive


@dataclass(frozen=True)
class Directive:
    text: str


@dataclass(frozen=True)
class TupleExpr:
    items: tuple[Any, ...]


@dataclass(frozen=True)
class ObjectExpr:
    items: tuple[tuple[Any, Any], ...]


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Postfix:
    base: Any
    ops: tuple[tuple[str, Any], ...]  # ("attr", name) | ("index", expr) | ("splat", None)


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Any, ...]
    expand: bool = False


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Any


@dataclass(frozen=True)
class Binary:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class Cond:
    cond: Any
    then: Any
    other: Any


@dataclass(frozen=True)
class ForExpr:
    key_var: str | None
    val_var: str
    coll: Any
    key_expr: Any
    val_expr: Any
    cond: Any
    is_object: bool
    grouping: bool


@dataclass(frozen=True)
class Invalid:
    """Expression that could not be parsed; evaluates to Unknown."""

    text: str
    error: str


# --------------------------------------------------------------------------- parser

_BIN_LEVELS: tuple[tuple[str, ...], ...] = (
    ("||",),
    ("&&",),
    ("==", "!="),
    ("<", ">", "<=", ">="),
    ("+", "-"),
    ("*", "/", "%"),
)


class _Parser:
    def __init__(self, toks: list[Tok]) -> None:
        self.t = [x for x in toks if x.kind != "nl"]
        self.i = 0

    def peek(self, k: int = 0) -> Tok:
        return self.t[min(self.i + k, len(self.t) - 1)]

    def next(self) -> Tok:
        tok = self.peek()
        self.i += 1
        return tok

    def is_op(self, value: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "op" and tok.value == value

    def is_ident(self, value: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "ident" and tok.value == value

    def expect_op(self, value: str) -> None:
        tok = self.next()
        if tok.kind != "op" or tok.value != value:
            raise ExprError(f"expected {value!r}, got {tok.value!r}")

    def parse(self) -> Any:
        e = self.expr()
        if self.peek().kind != "eof":
            raise ExprError(f"unexpected token {self.peek().value!r}")
        return e

    def expr(self) -> Any:
        c = self.binary(0)
        if self.is_op("?"):
            self.next()
            a = self.expr()
            self.expect_op(":")
            b = self.expr()
            return Cond(c, a, b)
        return c

    def binary(self, lvl: int) -> Any:
        if lvl == len(_BIN_LEVELS):
            return self.unary()
        left = self.binary(lvl + 1)
        while self.peek().kind == "op" and self.peek().value in _BIN_LEVELS[lvl]:
            op = self.next().value
            right = self.binary(lvl + 1)
            left = Binary(op, left, right)
        return left

    def unary(self) -> Any:
        if self.is_op("!") or self.is_op("-"):
            op = self.next().value
            return Unary(op, self.unary())
        return self.postfix()

    def postfix(self) -> Any:
        base = self.primary()
        ops: list[tuple[str, Any]] = []
        while True:
            if self.is_op("."):
                self.next()
                tok = self.next()
                if tok.kind == "ident":
                    ops.append(("attr", tok.value))
                elif tok.kind == "num":
                    ops.append(("index", Literal(int(float(tok.value)))))
                elif tok.kind == "op" and tok.value == "*":
                    ops.append(("splat", None))
                else:
                    raise ExprError(f"bad traversal after '.': {tok.value!r}")
            elif self.is_op("["):
                self.next()
                if self.is_op("*") and self.is_op("]", 1):
                    self.next()
                    self.next()
                    ops.append(("splat", None))
                else:
                    idx = self.expr()
                    self.expect_op("]")
                    ops.append(("index", idx))
            else:
                break
        return Postfix(base, tuple(ops)) if ops else base

    def primary(self) -> Any:
        tok = self.next()
        if tok.kind == "num":
            v = float(tok.value)
            return Literal(int(v) if v.is_integer() and "." not in tok.value else v)
        if tok.kind == "str":
            return parse_template(tok.value, escaped=True)
        if tok.kind == "heredoc":
            return parse_template(tok.value, escaped=False)
        if tok.kind == "ident":
            if tok.value == "true":
                return Literal(True)
            if tok.value == "false":
                return Literal(False)
            if tok.value == "null":
                return Literal(None)
            if self.is_op("("):
                return self.call(tok.value)
            return Var(tok.value)
        if tok.kind == "op":
            if tok.value == "(":
                e = self.expr()
                self.expect_op(")")
                return e
            if tok.value == "[":
                if self.is_ident("for"):
                    return self.for_expr("]")
                return self.tuple_expr()
            if tok.value == "{":
                if self.is_ident("for"):
                    return self.for_expr("}")
                return self.object_expr()
        raise ExprError(f"unexpected token {tok.value!r}")

    def call(self, name: str) -> Call:
        self.expect_op("(")
        args: list[Any] = []
        expand = False
        while not self.is_op(")"):
            args.append(self.expr())
            if self.is_op("..."):
                self.next()
                expand = True
            if self.is_op(","):
                self.next()
            elif not self.is_op(")"):
                raise ExprError("expected ',' or ')' in call")
        self.next()
        # provider-namespaced functions (provider::x::f) are not supported
        return Call(name, tuple(args), expand)

    def tuple_expr(self) -> TupleExpr:
        items: list[Any] = []
        while not self.is_op("]"):
            items.append(self.expr())
            if self.is_op(","):
                self.next()
            elif not self.is_op("]"):
                raise ExprError("expected ',' or ']' in tuple")
        self.next()
        return TupleExpr(tuple(items))

    def object_expr(self) -> ObjectExpr:
        items: list[tuple[Any, Any]] = []
        while not self.is_op("}"):
            if self.peek().kind == "ident" and (self.is_op("=", 1) or self.is_op(":", 1)):
                key: Any = Literal(self.next().value)
            else:
                key = self.expr()
            tok = self.next()
            if tok.kind != "op" or tok.value not in ("=", ":"):
                raise ExprError("expected '=' or ':' in object")
            items.append((key, self.expr()))
            if self.is_op(","):
                self.next()
        self.next()
        return ObjectExpr(tuple(items))

    def for_expr(self, closer: str) -> ForExpr:
        self.next()  # 'for'
        first = self.next().value
        second = None
        if self.is_op(","):
            self.next()
            second = self.next().value
        if not self.is_ident("in"):
            raise ExprError("expected 'in' in for expression")
        self.next()
        coll = self.expr()
        self.expect_op(":")
        is_object = closer == "}"
        key_expr = None
        val = self.expr()
        grouping = False
        if is_object:
            self.expect_op("=>")
            key_expr = val
            val = self.expr()
            if self.is_op("..."):
                self.next()
                grouping = True
        cond = None
        if self.is_ident("if"):
            self.next()
            cond = self.expr()
        self.expect_op(closer)
        key_var, val_var = (first, second) if second is not None else (None, first)
        return ForExpr(key_var, val_var, coll, key_expr, val, cond, is_object, grouping)


def parse_expression(text: str) -> Any:
    try:
        return _Parser(lex(text)).parse()
    except (ExprError, LexError, IndexError, ValueError) as exc:
        return Invalid(text, str(exc))


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def parse_template(raw: str, escaped: bool = True) -> Any:
    parts: list[Any] = []
    buf: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if escaped and c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "u" and i + 5 < n + 1:
                try:
                    buf.append(chr(int(raw[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            buf.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if raw.startswith("$${", i):
            buf.append("${")
            i += 3
            continue
        if raw.startswith("%%{", i):
            buf.append("%{")
            i += 3
            continue
        if raw.startswith("${", i) or raw.startswith("%{", i):
            try:
                end = scan_interp(raw, i + 2)
            except LexError:
                buf.append(raw[i:])
                break
            inner = raw[i + 2 : end - 1].strip()
            inner = inner.removeprefix("~").removesuffix("~").strip()
            if buf:
                parts.append("".join(buf))
                buf = []
            if raw[i] == "%":
                parts.append(Directive(inner))
            else:
                parts.append(parse_expression(inner))
            i = end
            continue
        buf.append(c)
        i += 1
    if buf:
        parts.append("".join(buf))
    if not parts:
        return Literal("")
    if len(parts) == 1 and isinstance(parts[0], str):
        return Literal(parts[0])
    return Template(tuple(parts))


# --------------------------------------------------------------- python-hcl2 values

_HEREDOC_RAW = re.compile(r"^<<(-?)([A-Za-z_][A-Za-z0-9_]*)\r?\n(.*?)\r?\n?[ \t]*\2\s*$", re.S)


def _whole_interp(s: str) -> bool:
    if not (s.startswith("${") and s.endswith("}")):
        return False
    try:
        return scan_interp(s, 2) == len(s)
    except LexError:
        return False


def raw_to_expr(value: Any) -> Any:
    """Convert a python-hcl2 value into an expression AST.

    Handles both python-hcl2 conventions: bare literal strings (``bucket-a``) and
    quoted literal strings (``"bucket-a"``); expressions are wrapped in ``${...}``.
    """
    if isinstance(value, str):
        s = value
        if _whole_interp(s):
            return parse_expression(s[2:-1])
        if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
            inner = s[1:-1]
            if not _HEREDOC_RAW.match(inner):
                return parse_template(inner, escaped=True)
            s = inner
        m = _HEREDOC_RAW.match(s)
        if m:
            lines = m.group(3).split("\n")
            return parse_template(heredoc_content(lines, m.group(1) == "-"), escaped=False)
        return parse_template(s, escaped=False)
    if isinstance(value, dict):
        items = []
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("__") and k.endswith("__"):
                continue
            items.append((Literal(normalize_key(k)), raw_to_expr(v)))
        return ObjectExpr(tuple(items))
    if isinstance(value, list):
        return TupleExpr(tuple(raw_to_expr(v) for v in value))
    return Literal(value)


def normalize_key(key: Any) -> Any:
    if isinstance(key, str):
        k = key
        if _whole_interp(k):
            k = k[2:-1]
        if len(k) >= 2 and k[0] == k[-1] == '"':
            k = k[1:-1]
        return k
    return key
