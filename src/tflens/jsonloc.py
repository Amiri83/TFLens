"""Terraform JSON syntax (``*.tf.json``, ``*.tfvars.json``) with exact line ranges.

The standard ``json`` module gives values but no positions, so this module parses JSON
itself into a tree annotated with line numbers, then converts it into the same shapes
the HCL path produces: python-hcl2-style data (blocks marked ``__is_block__``) and a
:class:`~tflens.locate.LocBlock` tree, so ``loader`` handles both syntaxes alike.

JSON does not say whether an object is a nested block or a map argument; objects are
treated as nested blocks except for well-known map arguments (``tags``, ...).
"""

from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .locate import LocBlock


class JsonLocError(ValueError):
    pass


@dataclass
class JNode:
    value: Any  # python value for scalars; None for objects / arrays
    start: int
    end: int
    items: list[tuple[str, int, JNode]] = field(default_factory=list)  # object: key, line, value
    elems: list[JNode] = field(default_factory=list)  # array
    kind: str = "scalar"  # scalar | object | array

    def plain(self, literal: bool = False) -> Any:
        """The value as python-hcl2 would give it. Strings are templates in ``.tf.json``
        (``${...}`` is evaluated); with ``literal`` (``.tfvars.json``) they are not."""
        if self.kind == "object":
            return {k: v.plain(literal) for k, _, v in self.items if k != "//"}
        if self.kind == "array":
            return [e.plain(literal) for e in self.elems]
        if isinstance(self.value, str):
            return _hcl_string(self.value, literal)
        return self.value


def _hcl_string(s: str, literal: bool) -> str:
    """A JSON string in the form ``expr.raw_to_expr`` reads: bare when unambiguous, else
    quoted with HCL escapes (``literal`` also escapes ``${`` / ``%{``)."""
    if literal and ("${" in s or "%{" in s):
        s = s.replace("${", "$${").replace("%{", "%%{")
    elif not (s.startswith('"') or s.startswith("<<")):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


_TOKEN = re.compile(
    r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r"|(?P<lit>true|false|null)|(?P<punct>[{}\[\]:,]))",
    re.S,
)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.newlines = [m.start() for m in re.finditer("\n", text)]
        self.tok: tuple[str, str, int, int] | None = None
        self._next()

    def line(self, pos: int) -> int:
        return bisect.bisect_left(self.newlines, pos) + 1

    def _next(self) -> None:
        m = _TOKEN.match(self.text, self.pos)
        if m is None:
            if self.text[self.pos :].strip():
                raise JsonLocError(f"unexpected character at line {self.line(self.pos)}")
            self.tok = None
            return
        kind = m.lastgroup or ""
        start = m.start(kind)
        self.tok = (kind, m.group(kind), self.line(start), self.line(m.end() - 1))
        self.pos = m.end()

    def expect(self, punct: str) -> int:
        if self.tok is None or self.tok[1] != punct:
            raise JsonLocError(f"expected {punct!r}")
        line = self.tok[2]
        self._next()
        return line

    def value(self) -> JNode:
        if self.tok is None:
            raise JsonLocError("unexpected end of input")
        kind, text, start, end = self.tok
        if kind == "punct" and text == "{":
            node = JNode(None, start, start, kind="object")
            self._next()
            while self.tok is not None and self.tok[1] != "}":
                if self.tok[0] != "str":
                    raise JsonLocError(f"expected a key at line {self.tok[2]}")
                key, key_line = json.loads(self.tok[1]), self.tok[2]
                self._next()
                self.expect(":")
                node.items.append((key, key_line, self.value()))
                if self.tok is not None and self.tok[1] == ",":
                    self._next()
            node.end = self.expect("}")
            return node
        if kind == "punct" and text == "[":
            node = JNode(None, start, start, kind="array")
            self._next()
            while self.tok is not None and self.tok[1] != "]":
                node.elems.append(self.value())
                if self.tok is not None and self.tok[1] == ",":
                    self._next()
            node.end = self.expect("]")
            return node
        if kind == "punct":
            raise JsonLocError(f"unexpected {text!r} at line {start}")
        self._next()
        return JNode(json.loads(text), start, end)


def parse(text: str) -> JNode:
    p = _Parser(text)
    node = p.value()
    if p.tok is not None:
        raise JsonLocError(f"trailing content at line {p.tok[2]}")
    return node


# -------------------------------------------------------------- Terraform JSON syntax

_LABELS = {
    "resource": 2,
    "data": 2,
    "module": 1,
    "variable": 1,
    "output": 1,
    "provider": 1,
    "locals": 0,
    "terraform": 0,
    "check": 1,
}
# Block types whose arguments are all plain attributes (objects are values, not blocks).
_ALL_ATTRS = {"module", "variable", "output", "locals"}
# Arguments that are maps (objects) rather than nested blocks.
MAP_ATTRS = frozenset(
    {
        "tags",
        "tags_all",
        "variables",
        "triggers",
        "labels",
        "environment_variables",
        "parameters",
        "required_providers",
    }
)


def tf_json(text: str) -> tuple[dict[str, Any], LocBlock]:
    """A ``.tf.json`` file as (python-hcl2-shaped data, location tree)."""
    root = parse(text)
    if root.kind != "object":
        raise JsonLocError("the top level is not an object")
    loc = LocBlock("<file>", (), 1, max(1, root.end))
    data: dict[str, list[Any]] = {}
    for btype, _, node in root.items:
        depth = _LABELS.get(btype)
        if depth is None:
            continue
        for labels, body in _bodies(node, depth):
            entry: Any = _body(btype, body, loc, labels)
            for label in reversed(labels):
                entry = {label: entry}
            data.setdefault(btype, []).append(entry)
    return data, loc


def _bodies(node: JNode, depth: int) -> list[tuple[tuple[str, ...], JNode]]:
    if depth == 0:
        if node.kind == "array":
            return [((), e) for e in node.elems if e.kind == "object"]
        return [((), node)] if node.kind == "object" else []
    out: list[tuple[tuple[str, ...], JNode]] = []
    if node.kind == "array":  # each label level may also be an array of objects
        for e in node.elems:
            out.extend(_bodies(e, depth))
        return out
    if node.kind != "object":
        return out
    for key, _, child in node.items:
        if key == "//":
            continue
        for labels, body in _bodies(child, depth - 1):
            out.append(((key,) + labels, body))
    return out


def _body(btype: str, body: JNode, parent: LocBlock, labels: tuple[str, ...]) -> dict[str, Any]:
    lb = LocBlock(btype, labels, body.start, body.end)
    parent.children.append(lb)
    out: dict[str, Any] = {"__is_block__": True}
    for key, line, value in body.items:
        if key == "//":
            continue
        if btype == "terraform" and key in ("backend", "cloud") and value.kind == "object":
            if key == "cloud":
                out[key] = [_nested(key, (), value, lb)]
            else:
                out[key] = [
                    {k: _nested(key, (k,), v, lb)} for k, _, v in value.items if v.kind == "object"
                ]
            continue
        if btype not in _ALL_ATTRS and key not in MAP_ATTRS and key != "dynamic":
            if value.kind == "object":
                out[key] = [_nested(key, (), value, lb)]
                continue
            if (
                value.kind == "array"
                and value.elems
                and all(e.kind == "object" for e in value.elems)
            ):
                out[key] = [_nested(key, (), e, lb) for e in value.elems]
                continue
        lb.attrs.setdefault(key, (line, value.end))
        out[key] = value.plain()
    return out


def _nested(btype: str, labels: tuple[str, ...], node: JNode, parent: LocBlock) -> dict[str, Any]:
    return _body(btype, node, parent, labels)


def tfvars_json(text: str) -> dict[str, tuple[Any, int, int]]:
    """A ``.tfvars.json`` file: variable -> (value, start line, end line)."""
    root = parse(text)
    if root.kind != "object":
        raise JsonLocError("the top level is not an object")
    return {k: (v.plain(literal=True), line, v.end) for k, line, v in root.items if k != "//"}
