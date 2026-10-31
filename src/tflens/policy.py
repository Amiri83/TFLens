"""IAM policy document normalization (jsonencode, heredoc JSON, policy_document)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .values import Interp, JsonEncoded, Location, Ref, Unknown, render, unwrap

_PLACEHOLDER = "__TFLENS_SYM_{}__"
_PLACEHOLDER_RE = re.compile(r"__TFLENS_SYM_(\d+)__")


def decode_json_interp(value: Interp) -> Any | None:
    """Decode JSON text containing symbolic parts (heredoc policy with ``${...}``)."""
    text: list[str] = []
    syms: list[Any] = []
    for p in value.parts:
        if isinstance(p, str):
            text.append(p)
        else:
            text.append(_PLACEHOLDER.format(len(syms)))
            syms.append(p)
    try:
        data = json.loads("".join(text))
    except ValueError:
        return None
    return _restore(data, syms)


def _restore(v: Any, syms: list[Any]) -> Any:
    if isinstance(v, str):
        pieces = _PLACEHOLDER_RE.split(v)
        if len(pieces) == 1:
            return v
        parts: list[Any] = []
        for i, piece in enumerate(pieces):
            if i % 2:
                parts.append(syms[int(piece)])
            elif piece:
                parts.append(piece)
        if len(parts) == 1:
            return parts[0]
        return Interp(tuple(parts))
    if isinstance(v, dict):
        return {k: _restore(x, syms) for k, x in v.items()}
    if isinstance(v, list):
        return [_restore(x, syms) for x in v]
    return v


def _policy_tokens(text: str, first_line: int) -> list[tuple[str, str, int]]:
    """Tiny tokenizer for HCL object literals / JSON: strings, words, brackets, ``:``/``=``."""
    out: list[tuple[str, str, int]] = []
    i, n, line = 0, len(text), first_line
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c == '"':
            j, start = i + 1, line
            while j < n and text[j] != '"':
                if text[j] == "\n":
                    line += 1
                j += 2 if text[j] == "\\" else 1
            out.append(("str", text[i + 1 : j], start))
            i = j + 1
        elif c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            out.append(("word", text[i:j], line))
            i = j
        else:
            if c in "{}[]:=":
                out.append(("op", c, line))
            i += 1
    return out


def statement_spans(lines: list[str], first_line: int) -> list[tuple[int, int]]:
    """Line spans of each entry of the ``Statement`` list inside policy source text."""
    toks = _policy_tokens("\n".join(lines), first_line)
    for k in range(len(toks) - 2):
        kind, value, _ = toks[k]
        if kind not in ("str", "word") or value != "Statement":
            continue
        if toks[k + 1][:2] not in (("op", ":"), ("op", "=")):
            continue
        opener = toks[k + 2]
        if opener[:2] not in (("op", "["), ("op", "{")):
            continue
        spans: list[tuple[int, int]] = []
        single = opener[1] == "{"
        depth = 1 if single else 0
        start = opener[2]
        for kind2, v, line in toks[k + 3 :]:
            if kind2 != "op" or v in ":=":
                continue
            if v in "[{":
                depth += 1
                if v == "{" and depth == 1:
                    start = line
            else:
                if depth == 0:
                    return spans
                depth -= 1
                if v == "}" and depth == 0:
                    spans.append((start, line))
                    if single:
                        return spans
        return spans
    return []


@dataclass
class Statement:
    effect: str
    actions: list[str]
    not_actions: list[str]
    resources: list[Any]
    not_resources: list[Any]
    principals: dict[str, list[Any]]
    sid: str | None
    location: Location | None
    conditions: Any = None
    errors: list[str] = field(default_factory=list)


def as_list(v: Any) -> list[Any]:
    v = unwrap(v)
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _strs(v: Any) -> list[str]:
    out = []
    for x in as_list(v):
        s = render(x)
        out.append(s if s is not None else f"<unknown:{_describe(x)}>")
    return out


def _describe(v: Any) -> str:
    if isinstance(v, Unknown):
        return v.reason
    if isinstance(v, Ref):
        return v.expr()
    return type(v).__name__


def parse_policy(value: Any, fallback: Location | None) -> tuple[list[Statement], list[str]]:
    """Return (statements, errors). Errors describe why a policy is not analyzable."""
    locs: tuple[Location | None, ...] = ()
    base_loc = fallback
    v = value
    if isinstance(v, Ref):
        if not v.has_value:
            return [], [f"policy value is a reference to {v.expr()} that is not statically known"]
        v = v.value
    if isinstance(v, Unknown):
        return [], [f"policy not known statically: {v.reason}"]
    if isinstance(v, JsonEncoded):
        locs = v.statement_locations
        base_loc = v.location or fallback
        v = v.value
    if isinstance(v, Interp):
        decoded = decode_json_interp(v)
        if decoded is None:
            return [], ["policy text with interpolations is not valid JSON"]
        v = decoded
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [], ["policy string is not valid JSON"]
    if not isinstance(v, dict):
        return [], [f"policy is not a JSON object ({type(v).__name__})"]
    raw_statements = as_list(v.get("Statement"))
    out: list[Statement] = []
    for i, st in enumerate(raw_statements):
        st = unwrap(st)
        loc = locs[i] if i < len(locs) and locs[i] is not None else base_loc
        if not isinstance(st, dict):
            out.append(
                Statement("Allow", [], [], [], [], {}, None, loc, errors=["statement not known"])
            )
            continue
        principals: dict[str, list[Any]] = {}
        p = unwrap(st.get("Principal"))
        if p == "*":
            principals["*"] = ["*"]
        elif isinstance(p, dict):
            for k in sorted(p):
                principals[str(k)] = as_list(p[k])
        elif p is not None:
            principals["?"] = [p]
        effect = render(st.get("Effect")) or "Allow"
        out.append(
            Statement(
                effect=effect,
                actions=_strs(st.get("Action")),
                not_actions=_strs(st.get("NotAction")),
                resources=as_list(st.get("Resource")),
                not_resources=as_list(st.get("NotResource")),
                principals=principals,
                sid=render(st.get("Sid")),
                location=loc,
                conditions=st.get("Condition"),
            )
        )
    return out, []
