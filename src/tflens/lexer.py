"""A small HCL2 lexer.

Used for two things:
  * locating blocks/attributes (file:line) in source text (see ``locate``), and
  * tokenizing expression strings produced by python-hcl2 (see ``expr``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_OPS3 = ("...",)
_OPS2 = ("==", "!=", "<=", ">=", "&&", "||", "=>")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_NUM_RE = re.compile(r"\d+(\.\d+)?([eE][+-]?\d+)?")
_HEREDOC_RE = re.compile(r"<<(-?)([A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?\n")


@dataclass(frozen=True)
class Tok:
    kind: str  # ident | num | str | heredoc | op | nl | eof
    value: str
    line: int
    end_line: int


class LexError(ValueError):
    pass


def scan_string(text: str, i: int) -> int:
    """``i`` points just after an opening quote; return index just after the closing quote."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if text.startswith("$${", i) or text.startswith("%%{", i):
            i += 3
            continue
        if text.startswith("${", i) or text.startswith("%{", i):
            i = scan_interp(text, i + 2)
            continue
        i += 1
    raise LexError("unterminated string")


def scan_interp(text: str, i: int) -> int:
    """``i`` points just after ``${``; return index just after the matching ``}``."""
    n = len(text)
    depth = 1
    while i < n:
        c = text[i]
        if c == '"':
            i = scan_string(text, i + 1)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise LexError("unterminated interpolation")


def lex(text: str) -> list[Tok]:
    toks: list[Tok] = []
    i = 0
    n = len(text)
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            toks.append(Tok("nl", "\n", line, line))
            line += 1
            i += 1
            continue
        if c in " \t\r﻿":
            i += 1
            continue
        if c == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            line += text.count("\n", i, j)
            i = j
            continue
        if c == '"':
            j = scan_string(text, i + 1)
            nls = text.count("\n", i, j)
            toks.append(Tok("str", text[i + 1 : j - 1], line, line + nls))
            line += nls
            i = j
            continue
        if c == "<":
            m = _HEREDOC_RE.match(text, i)
            if m:
                strip, marker = m.group(1) == "-", m.group(2)
                body_start = m.end()
                pos = body_start
                body_lines: list[str] = []
                end = n
                while pos < n:
                    nl = text.find("\n", pos)
                    raw = text[pos:] if nl == -1 else text[pos:nl]
                    nxt = n if nl == -1 else nl + 1
                    if raw.strip() == marker:
                        end = pos + len(raw)
                        break
                    body_lines.append(raw.rstrip("\r"))
                    pos = nxt
                content = heredoc_content(body_lines, strip)
                nls = text.count("\n", i, end)
                toks.append(Tok("heredoc", content, line, line + nls))
                line += nls
                i = end
                continue
        if c.isdigit():
            m = _NUM_RE.match(text, i)
            assert m
            toks.append(Tok("num", m.group(0), line, line))
            i = m.end()
            continue
        if c.isalpha() or c == "_":
            m = _IDENT_RE.match(text, i)
            assert m
            toks.append(Tok("ident", m.group(0), line, line))
            i = m.end()
            continue
        for op in _OPS3 + _OPS2:
            if text.startswith(op, i):
                toks.append(Tok("op", op, line, line))
                i += len(op)
                break
        else:
            toks.append(Tok("op", c, line, line))
            i += 1
    toks.append(Tok("eof", "", line, line))
    return toks


def heredoc_content(lines: list[str], strip: bool) -> str:
    if strip:
        indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
        cut = min(indents) if indents else 0
        lines = [ln[cut:] for ln in lines]
    return "".join(ln + "\n" for ln in lines)
