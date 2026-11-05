"""Value types produced by the evaluator.

Evaluated Terraform values are plain Python values (str, int, float, bool, None,
list, dict) or one of the symbolic types below. Nothing is ever guessed: anything
that cannot be determined statically becomes ``Unknown`` with a reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, order=True)
class Location:
    file: str
    start_line: int
    end_line: int

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "start_line": self.start_line, "end_line": self.end_line}

    def __str__(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.file}:{self.start_line}"
        return f"{self.file}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class Ref:
    """A reference to an attribute of a resource or data source (graph node)."""

    target: str
    attr: str
    value: Any = field(default=None, compare=False, hash=False)
    has_value: bool = field(default=False, compare=False, hash=False)
    # instances the reference stands for (``aws_subnet.a[*].id``), when that is known
    count: int | None = field(default=None, compare=False, hash=False)

    def expr(self) -> str:
        return f"{self.target}.{self.attr}" if self.attr else self.target


@dataclass(frozen=True)
class Unknown:
    """A value that cannot be determined. ``after_apply``: it only exists once terraform has
    applied and the code shows where it comes from (a transformation of an ARN or id, a
    ``timestamp()``): that is a link, not an unknown, so it is never listed as one.
    ``origin``: the file:line where the chain ends (the ``variable`` block with no value)."""

    reason: str
    refs: tuple[Ref, ...] = ()
    after_apply: bool = field(default=False, compare=False, hash=False)
    origin: Location | None = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class Interp:
    """A string template whose parts are not all known literals."""

    parts: tuple[Any, ...]


@dataclass(frozen=True)
class JsonEncoded:
    """Result of ``jsonencode``/policy documents. Keeps structure for policy analysis."""

    value: Any
    location: Location | None = None
    statement_locations: tuple[Location | None, ...] = ()


def unknown_from(reason: str, *values: Any) -> Unknown:
    """An Unknown computed from ``values``. When something in them is really unknown its
    reason is kept (and its origin); when all they lack are after-apply values the code links
    to, the result is one too (``after_apply``)."""
    refs: list[Ref] = []
    blocking: Unknown | None = None
    deferred = False
    for v in values:
        refs.extend(collect_refs(v))
        found: list[tuple[str, Unknown]] = []
        _walk_unknowns(v, "", found, True)
        for _, u in found:
            if u.after_apply:
                deferred = True
            elif blocking is None:
                blocking = u
    if blocking is not None and blocking.reason not in reason:
        reason = f"{reason} ({blocking.reason})"
    return Unknown(
        reason,
        tuple(dict.fromkeys(refs)),
        after_apply=blocking is None and (deferred or bool(refs)),
        origin=blocking.origin if blocking is not None else None,
    )


def shows_link(value: Any) -> bool:
    """Whether a value that is not known statically only lacks what exists after apply: the
    code shows the link, so it is not an unknown."""
    if isinstance(value, Ref) and value.has_value:
        value = value.value
    if isinstance(value, Unknown):
        return value.after_apply
    return not collect_unknowns(value) and bool(collect_refs(value) or _has_deferred(value))


def _has_deferred(value: Any) -> bool:
    found: list[tuple[str, Unknown]] = []
    _walk_unknowns(value, "", found, True)
    return bool(found)


def collect_refs(value: Any) -> list[Ref]:
    out: list[Ref] = []
    _walk_refs(value, out)
    return out


def _walk_refs(value: Any, out: list[Ref]) -> None:
    if isinstance(value, Ref):
        out.append(value)
    elif isinstance(value, Unknown):
        out.extend(value.refs)
    elif isinstance(value, Interp):
        for p in value.parts:
            _walk_refs(p, out)
    elif isinstance(value, JsonEncoded):
        _walk_refs(value.value, out)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_refs(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_refs(v, out)


def collect_unknowns(value: Any, path: str = "") -> list[tuple[str, Unknown]]:
    """The real unknowns of a value (after-apply links are not unknowns)."""
    out: list[tuple[str, Unknown]] = []
    _walk_unknowns(value, path, out, False)
    return out


def _walk_unknowns(value: Any, path: str, out: list[tuple[str, Unknown]], deferred: bool) -> None:
    if isinstance(value, Unknown):
        if deferred or not value.after_apply:
            out.append((path, value))
    elif isinstance(value, Interp):
        for p in value.parts:
            _walk_unknowns(p, path, out, deferred)
    elif isinstance(value, JsonEncoded):
        _walk_unknowns(value.value, path, out, deferred)
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk_unknowns(v, f"{path}.{k}" if path else str(k), out, deferred)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk_unknowns(v, f"{path}[{i}]", out, deferred)


def unwrap(value: Any) -> Any:
    """Replace Refs that carry a resolved value with that value (shallow for scalars)."""
    if isinstance(value, Ref) and value.has_value:
        return unwrap(value.value)
    if isinstance(value, Interp):
        s = render(value)
        return value if s is None else s
    if isinstance(value, JsonEncoded):
        return value
    return value


def is_known(value: Any) -> bool:
    if isinstance(value, Unknown):
        return False
    if isinstance(value, Ref):
        return value.has_value and is_known(value.value)
    if isinstance(value, Interp):
        return all(is_known(p) for p in value.parts)
    if isinstance(value, JsonEncoded):
        return is_known(value.value)
    if isinstance(value, dict):
        return all(is_known(v) for v in value.values())
    if isinstance(value, list):
        return all(is_known(v) for v in value)
    return True


def scalar_str(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float, str)):
        return str(value)
    return None


def render(value: Any) -> str | None:
    """Render a value as a concrete string, or None if it is not fully known."""
    if isinstance(value, Ref):
        return render(value.value) if value.has_value else None
    if isinstance(value, Interp):
        out = []
        for p in value.parts:
            s = render(p)
            if s is None:
                return None
            out.append(s)
        return "".join(out)
    if isinstance(value, JsonEncoded):
        if not is_known(value.value):
            return None
        return json.dumps(to_plain(value.value), separators=(",", ":"))
    return scalar_str(value)


def display(value: Any) -> str:
    """Human-readable rendering; symbolic parts shown as ``${...}``."""
    if isinstance(value, Ref):
        if value.has_value and is_known(value.value):
            return display(value.value)
        return "${" + value.expr() + "}"
    if isinstance(value, Unknown):
        if value.after_apply:
            return _after_apply_text(value)
        return f"<unknown: {value.reason}>"
    if isinstance(value, Interp):
        return "".join(display(p) for p in value.parts)
    if isinstance(value, JsonEncoded):
        return "jsonencode(" + json.dumps(to_json(value.value), sort_keys=True) + ")"
    if isinstance(value, (list, dict)):
        return json.dumps(to_json(value), sort_keys=True)
    if value is None:
        return "null"
    s = scalar_str(value)
    return s if s is not None else repr(value)


def _after_apply_text(value: Unknown) -> str:
    """``${aws_x.y.arn}``: what an after-apply value is computed from."""
    refs = [f"${{{r.expr()}}}" for r in value.refs]
    return " ".join(refs) if refs else "(known after apply)"


def to_plain(value: Any) -> Any:
    """Fully-known value -> plain JSON-able Python value."""
    if isinstance(value, Ref):
        return to_plain(value.value)
    if isinstance(value, Interp):
        return render(value)
    if isinstance(value, JsonEncoded):
        return to_plain(value.value)
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    return value


def to_json(value: Any) -> Any:
    """Serialize any evaluated value for model.json (explicit symbolic markers)."""
    if isinstance(value, Ref):
        d: dict[str, Any] = {"$ref": value.target, "attr": value.attr}
        if value.has_value and is_known(value.value):
            d["value"] = to_json(value.value)
        return d
    if isinstance(value, Unknown):
        if value.after_apply:
            return {
                "$template": _after_apply_text(value),
                "refs": sorted({r.target for r in value.refs}),
            }
        return {"$unknown": value.reason}
    if isinstance(value, Interp):
        s = render(value)
        if s is not None:
            return s
        return {
            "$template": display(value),
            "refs": sorted({r.target for r in collect_refs(value)}),
        }
    if isinstance(value, JsonEncoded):
        return {"$json": to_json(value.value)}
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
