"""Source locator: recovers file:line ranges for every block and attribute.

python-hcl2 gives us the values; this scanner gives us exact line ranges so every
block, nested block and attribute can carry evidence (file, start, end, snippet).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import Tok, lex

_OPEN = {"(", "[", "{"}
_CLOSE = {")", "]", "}"}


@dataclass
class LocBlock:
    type: str
    labels: tuple[str, ...]
    start: int
    end: int
    attrs: dict[str, tuple[int, int]] = field(default_factory=dict)
    children: list[LocBlock] = field(default_factory=list)

    def children_of(self, block_type: str) -> list[LocBlock]:
        return [c for c in self.children if c.type == block_type]


def scan(text: str) -> LocBlock:
    toks = lex(text)
    root = LocBlock("<file>", (), 1, max(1, text.count("\n") + 1))
    _parse_body(toks, 0, root)
    return root


def _parse_body(toks: list[Tok], i: int, block: LocBlock) -> int:
    """Parse body tokens into ``block``; return index of closing ``}`` (or eof)."""
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "eof":
            return i
        if t.kind == "nl":
            i += 1
            continue
        if t.kind == "op" and t.value == "}":
            return i
        if t.kind == "ident":
            nxt = toks[i + 1]
            if nxt.kind == "op" and nxt.value == "=":
                i, end_line = _skip_expression(toks, i + 2, t.line)
                block.attrs.setdefault(t.value, (t.line, end_line))
                continue
            j = i + 1
            labels: list[str] = []
            while toks[j].kind in ("str", "ident"):
                labels.append(toks[j].value)
                j += 1
            if toks[j].kind == "op" and toks[j].value == "{":
                child = LocBlock(t.value, tuple(labels), t.line, t.line)
                k = _parse_body(toks, j + 1, child)
                child.end = toks[k].line
                block.children.append(child)
                i = k + 1
                continue
        i += 1
    return i


def _skip_expression(toks: list[Tok], i: int, start_line: int) -> tuple[int, int]:
    depth = 0
    end_line = start_line
    while i < len(toks):
        t = toks[i]
        if t.kind == "eof":
            break
        if t.kind == "nl" and depth == 0:
            break
        if t.kind == "op" and t.value in _OPEN:
            depth += 1
        elif t.kind == "op" and t.value in _CLOSE:
            if depth == 0:
                break
            depth -= 1
        if t.kind != "nl":
            end_line = t.end_line
        i += 1
    return i, end_line
