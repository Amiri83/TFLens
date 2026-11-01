"""Deterministic expression evaluator.

Scopes (variables, locals, modules, resources) are provided by the engine through
the ``Scope`` protocol. Any value that cannot be resolved statically evaluates to
``Unknown`` with a reason; references to resources become ``Ref`` values so that
relationships survive through variables, locals and module outputs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any, Protocol

from .expr import (
    Binary,
    Call,
    Cond,
    Directive,
    ForExpr,
    Invalid,
    Literal,
    ObjectExpr,
    Postfix,
    Template,
    TupleExpr,
    Unary,
    Var,
)
from .values import (
    Interp,
    JsonEncoded,
    Location,
    Ref,
    Unknown,
    collect_refs,
    is_known,
    render,
    scalar_str,
    to_plain,
    unknown_from,
    unwrap,
)

Ops = list[tuple[str, Any]]


class Scope(Protocol):
    location: Location | None

    def resolve(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        """Resolve a root name; return the value and the ops not yet applied."""
        ...

    def read_file(self, path: str) -> str | None: ...


def evaluate(node: Any, scope: Scope, bindings: dict[str, Any] | None = None) -> Any:
    return _Evaluator(scope).eval(node, bindings or {})


class _Evaluator:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def eval(self, node: Any, b: dict[str, Any]) -> Any:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Template):
            return self.template(node, b)
        if isinstance(node, TupleExpr):
            return [self.eval(x, b) for x in node.items]
        if isinstance(node, ObjectExpr):
            out: dict[str, Any] = {}
            for k, v in node.items:
                key = unwrap(self.eval(k, b))
                ks = scalar_str(key)
                if ks is None:
                    return unknown_from("object key is not known statically", key)
                out[ks] = self.eval(v, b)
            return out
        if isinstance(node, Var):
            return self.traverse(node.name, [], b)
        if isinstance(node, Postfix):
            ops = [(k, self.eval(v, b) if k == "index" else v) for k, v in node.ops]
            if isinstance(node.base, Var):
                return self.traverse(node.base.name, ops, b)
            return apply_ops(self.eval(node.base, b), ops)
        if isinstance(node, Call):
            return self.call(node, b)
        if isinstance(node, Unary):
            return self.unary(node, b)
        if isinstance(node, Binary):
            return self.binary(node, b)
        if isinstance(node, Cond):
            c = unwrap(self.eval(node.cond, b))
            if isinstance(c, bool):
                return self.eval(node.then if c else node.other, b)
            a = self.eval(node.then, b)
            o = self.eval(node.other, b)
            if not isinstance(c, Unknown) and c is not None:
                return unknown_from("conditional on non-boolean value", c, a, o)
            reason = c.reason if isinstance(c, Unknown) else "condition not known"
            return unknown_from(f"conditional depends on unknown value ({reason})", c, a, o)
        if isinstance(node, ForExpr):
            return self.for_expr(node, b)
        if isinstance(node, Invalid):
            return Unknown(f"unparseable expression: {node.text!r} ({node.error})")
        if isinstance(node, Directive):
            return Unknown("template directive (%{...}) not evaluated")
        return Unknown(f"unsupported expression node {type(node).__name__}")

    def traverse(self, name: str, ops: Ops, b: dict[str, Any]) -> Any:
        if name in b:
            return apply_ops(b[name], ops)
        value, rest = self.scope.resolve(name, ops)
        return apply_ops(value, rest)

    def template(self, node: Template, b: dict[str, Any]) -> Any:
        parts: list[Any] = []
        for p in node.parts:
            v = p if isinstance(p, str) else self.eval(p, b)
            if isinstance(p, Directive):
                v = Unknown("template directive (%{...}) not evaluated")
            if isinstance(v, (Ref, Interp, Unknown)):
                parts.extend(v.parts if isinstance(v, Interp) else [v])
                continue
            s = scalar_str(v)
            if s is None:
                if isinstance(v, JsonEncoded):
                    rs = render(v)
                    parts.append(
                        rs if rs is not None else unknown_from("jsonencode of unknown value", v)
                    )
                    continue
                parts.append(unknown_from("non-string value interpolated into template", v))
            else:
                parts.append(s)
        if all(isinstance(p, str) for p in parts):
            return "".join(parts)
        merged: list[Any] = []
        for p in parts:
            if isinstance(p, str) and merged and isinstance(merged[-1], str):
                merged[-1] += p
            else:
                merged.append(p)
        return Interp(tuple(merged))

    def unary(self, node: Unary, b: dict[str, Any]) -> Any:
        v = unwrap(self.eval(node.operand, b))
        if node.op == "!" and isinstance(v, bool):
            return not v
        if node.op == "-" and isinstance(v, (int, float)) and not isinstance(v, bool):
            return -v
        return unknown_from(f"operator {node.op} on non-literal value", v)

    def binary(self, node: Binary, b: dict[str, Any]) -> Any:
        left = unwrap(self.eval(node.left, b))
        right = unwrap(self.eval(node.right, b))
        op = node.op
        if op in ("&&", "||"):
            if isinstance(left, bool) and isinstance(right, bool):
                return (left and right) if op == "&&" else (left or right)
            if op == "&&" and (left is False or right is False):
                return False
            if op == "||" and (left is True or right is True):
                return True
            return unknown_from(f"operator {op} on unknown value", left, right)
        if not (is_known(left) and is_known(right)):
            return unknown_from(f"operator {op} on unknown value", left, right)
        lp, rp = to_plain(left), to_plain(right)
        try:
            if op == "==":
                return lp == rp
            if op == "!=":
                return lp != rp
            lp, rp = _num(lp), _num(rp)
            if op == "+":
                return lp + rp
            if op == "-":
                return lp - rp
            if op == "*":
                return lp * rp
            if op == "/":
                return lp / rp
            if op == "%":
                return lp % rp
            if op == "<":
                return lp < rp
            if op == ">":
                return lp > rp
            if op == "<=":
                return lp <= rp
            if op == ">=":
                return lp >= rp
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            return Unknown(f"operator {op} failed: {exc}")
        return Unknown(f"unsupported operator {op}")

    def for_expr(self, node: ForExpr, b: dict[str, Any]) -> Any:
        coll = unwrap(self.eval(node.coll, b))
        if isinstance(coll, dict):
            pairs = list(coll.items())
        elif isinstance(coll, list):
            pairs = list(enumerate(coll))
        else:
            return unknown_from("for expression over a collection that is not known", coll)
        list_out: list[Any] = []
        obj_out: dict[str, Any] = {}
        for k, v in pairs:
            nb = dict(b)
            nb[node.val_var] = v
            if node.key_var is not None:
                nb[node.key_var] = k
            if node.cond is not None:
                c = unwrap(self.eval(node.cond, nb))
                if not isinstance(c, bool):
                    return unknown_from("for expression filter not known", c)
                if not c:
                    continue
            val = self.eval(node.val_expr, nb)
            if node.is_object:
                key = scalar_str(unwrap(self.eval(node.key_expr, nb)))
                if key is None:
                    return unknown_from("for expression key not known", val)
                if node.grouping:
                    obj_out.setdefault(key, []).append(val)
                else:
                    obj_out[key] = val
            else:
                list_out.append(val)
        return obj_out if node.is_object else list_out

    def call(self, node: Call, b: dict[str, Any]) -> Any:
        args = [self.eval(a, b) for a in node.args]
        if node.expand and args:
            last = unwrap(args[-1])
            if not isinstance(last, list):
                return unknown_from(f"{node.name}(...) expansion of unknown list", last)
            args = args[:-1] + last
        fn = _FUNCTIONS.get(node.name)
        if node.name == "jsonencode":
            return JsonEncoded(args[0] if args else None, self.scope.location)
        if node.name in ("file", "templatefile") and args:
            return self._file(node.name, args)
        if fn is None:
            return unknown_from(f"function {node.name}() is not evaluated statically", *args)
        try:
            return fn(*args)
        except _NeedsUnknown as exc:
            return unknown_from(f"{node.name}(): {exc}", *args)
        except (TypeError, ValueError, KeyError, IndexError, AttributeError) as exc:
            return unknown_from(f"{node.name}() could not be evaluated: {exc}", *args)

    def _file(self, name: str, args: list[Any]) -> Any:
        path = render(args[0])
        if path is None:
            return unknown_from(f"{name}() path not known", *args)
        content = self.scope.read_file(path)
        if content is None:
            return Unknown(f"{name}(): file {path!r} not found in repository")
        if name == "file":
            return content
        return Unknown(f"templatefile({path!r}) is not rendered statically")


def _num(v: Any) -> Any:
    if isinstance(v, bool):
        raise TypeError("boolean in arithmetic")
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        f = float(v)
        return int(f) if f.is_integer() else f
    raise TypeError(f"not a number: {v!r}")


def apply_ops(value: Any, ops: Ops) -> Any:
    for idx, (kind, arg) in enumerate(ops):
        if isinstance(value, Unknown):
            return value
        if kind == "splat":
            rest = ops[idx + 1 :]
            if isinstance(value, Ref):
                return apply_ops(value, rest)
            base = unwrap(value)
            if base is None:
                return []
            items = base if isinstance(base, list) else [base]
            return [apply_ops(item, rest) for item in items]
        if isinstance(value, Ref):
            value = _narrow_ref(value, kind, arg)
            continue
        base = unwrap(value)
        if kind == "attr":
            if isinstance(base, dict):
                if arg not in base:
                    return Unknown(f"attribute {arg!r} not found")
                value = base[arg]
            else:
                return unknown_from(f"cannot read attribute {arg!r} of non-object value", base)
        elif kind == "index":
            key = unwrap(arg)
            if not is_known(key):
                return unknown_from("index is not known statically", key, base)
            if (
                isinstance(base, list)
                and isinstance(key, (int, float))
                and not isinstance(key, bool)
            ):
                i = int(key)
                if not 0 <= i < len(base):
                    return Unknown(f"index {i} out of range")
                value = base[i]
            elif isinstance(base, dict):
                ks = scalar_str(key)
                if ks not in base:
                    return Unknown(f"key {ks!r} not found")
                value = base[ks]
            else:
                return unknown_from("cannot index value", base)
    return value


def _narrow_ref(ref: Ref, kind: str, arg: Any) -> Ref:
    if kind == "attr":
        attr = f"{ref.attr}.{arg}" if ref.attr else arg
        sub, has = None, False
        if ref.has_value and isinstance(unwrap(ref.value), dict) and arg in unwrap(ref.value):
            sub, has = unwrap(ref.value)[arg], True
        return Ref(ref.target, attr, sub, has)
    key = unwrap(arg)
    ks = scalar_str(key)
    attr = f"{ref.attr}[{ks if ks is not None else '?'}]"
    sub, has = None, False
    base = unwrap(ref.value) if ref.has_value else None
    if isinstance(base, list) and isinstance(key, int) and 0 <= key < len(base):
        sub, has = base[key], True
    elif isinstance(base, dict) and ks in base:
        sub, has = base[ks], True
    return Ref(ref.target, attr, sub, has)


# ------------------------------------------------------------------- functions


class _NeedsUnknown(Exception):
    pass


def _known(v: Any) -> Any:
    v = unwrap(v)
    if not is_known(v):
        raise _NeedsUnknown("argument not known statically")
    return to_plain(v)


def _str(v: Any) -> str:
    s = render(v)
    if s is None:
        raise _NeedsUnknown("string argument not known statically")
    return s


def _list(v: Any) -> list[Any]:
    v = unwrap(v)
    if isinstance(v, list):
        return v
    if v is None:
        return []
    raise _NeedsUnknown("list argument not known statically")


def _dict(v: Any) -> dict[str, Any]:
    v = unwrap(v)
    if isinstance(v, dict):
        return v
    raise _NeedsUnknown("map argument not known statically")


def _join(sep: Any, *lists: Any) -> Any:
    s = _str(sep)
    items: list[Any] = []
    for lst in lists:
        # aws_subnet.x[*].id: the instances of one resource, joined
        items.extend([lst] if isinstance(unwrap(lst), Ref) else _list(lst))
    parts: list[Any] = []
    for i, item in enumerate(items):
        if i:
            parts.append(s)
        item = unwrap(item)
        sv = scalar_str(item)
        if isinstance(item, Interp):
            parts.extend(item.parts)
        else:
            parts.append(sv if sv is not None else item)
    if all(isinstance(p, str) for p in parts):
        return "".join(parts)
    if any(not isinstance(p, (str, Ref, Unknown)) for p in parts):
        raise _NeedsUnknown("join of non-string values")
    return Interp(tuple(parts))


def _format(fmt: Any, *args: Any) -> Any:
    f = _str(fmt)
    parts: list[Any] = []
    buf = ""
    ai = 0
    i = 0
    while i < len(f):
        c = f[i]
        if c == "%" and i + 1 < len(f):
            spec = f[i + 1]
            if spec == "%":
                buf += "%"
                i += 2
                continue
            if spec in "sdvqf":
                if ai >= len(args):
                    raise ValueError("not enough format arguments")
                a = unwrap(args[ai])
                ai += 1
                sv = scalar_str(a)
                if spec == "q" and sv is not None:
                    sv = json.dumps(sv)
                if sv is not None:
                    buf += sv
                elif isinstance(a, (Ref, Unknown, Interp)):
                    if buf:
                        parts.append(buf)
                        buf = ""
                    parts.extend(a.parts if isinstance(a, Interp) else [a])
                else:
                    raise _NeedsUnknown("format argument not a string")
                i += 2
                continue
            raise _NeedsUnknown(f"format verb %{spec} not supported")
        buf += c
        i += 1
    if buf:
        parts.append(buf)
    if all(isinstance(p, str) for p in parts):
        return "".join(parts)
    return Interp(tuple(parts))


def _merge(*maps: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in maps:
        if unwrap(m) is None:
            continue
        out.update(_dict(m))
    return out


def _concat(*lists: Any) -> list[Any]:
    out: list[Any] = []
    for lst in lists:
        out.extend(_list(lst))
    return out


def _flatten(v: Any) -> list[Any]:
    out: list[Any] = []
    for item in _list(v):
        u = unwrap(item)
        if isinstance(u, list):
            out.extend(_flatten(u))
        elif isinstance(u, Unknown):
            raise _NeedsUnknown("flatten of unknown element")
        else:
            out.append(item)
    return out


def _distinct(v: Any) -> list[Any]:
    out: list[Any] = []
    seen: list[Any] = []
    for item in _list(v):
        key = json.dumps(to_plain(item), sort_keys=True) if is_known(item) else repr(item)
        if key not in seen:
            seen.append(key)
            out.append(item)
    return out


def _lookup(m: Any, key: Any, *default: Any) -> Any:
    d = _dict(m)
    k = _str(key)
    if k in d:
        return d[k]
    if default:
        return default[0]
    raise _NeedsUnknown(f"key {k!r} not in map")


def _element(lst: Any, idx: Any) -> Any:
    base = unwrap(lst)
    if isinstance(base, Ref):
        # element(aws_subnet.x[*].id, count.index): one instance of the same resource
        i = unwrap(idx)
        return _narrow_ref(base, "index", i) if is_known(i) else base
    items = _list(lst)
    i = int(_known(idx))
    if not items:
        raise ValueError("element() of empty list")
    return items[i % len(items)]


def _coalesce(*args: Any) -> Any:
    for a in args:
        u = unwrap(a)
        if isinstance(u, Unknown) or (isinstance(u, Ref) and not u.has_value):
            raise _NeedsUnknown("coalesce over unknown value")
        if u is not None and u != "":
            return a
    raise ValueError("no non-null arguments")


def _try(*args: Any) -> Any:
    for a in args:
        if not isinstance(a, Unknown):
            return a
    raise _NeedsUnknown("all try() alternatives unknown")


def _jsondecode(v: Any) -> Any:
    u = unwrap(v)
    if isinstance(u, JsonEncoded):
        return u.value
    if isinstance(u, Interp):
        from .policy import decode_json_interp

        decoded = decode_json_interp(u)
        if decoded is not None:
            return decoded
    return json.loads(_str(u))


def _length(v: Any) -> int:
    u = unwrap(v)
    if isinstance(u, (list, dict)):
        return len(u)
    return len(_str(u))


def _tostring(v: Any) -> Any:
    u = unwrap(v)
    if isinstance(u, (Ref, Interp)):
        return u
    s = scalar_str(u)
    if s is None:
        raise _NeedsUnknown("tostring of non-scalar")
    return s


def _tonumber(v: Any) -> Any:
    return _num(_known(v))


def _replace(s: Any, old: Any, new: Any) -> str:
    o = _str(old)
    if o.startswith("/") and o.endswith("/") and len(o) > 1:
        raise _NeedsUnknown("regex replace not evaluated")
    return _str(s).replace(o, _str(new))


def _keys(m: Any) -> list[str]:
    return sorted(_dict(m).keys())


def _values(m: Any) -> list[Any]:
    d = _dict(m)
    return [d[k] for k in sorted(d)]


def _contains(lst: Any, v: Any) -> bool:
    target = _known(v)
    return any(_known(x) == target for x in _list(lst))


def _zipmap(ks: Any, vs: Any) -> dict[str, Any]:
    return {_str(k): v for k, v in zip(_list(ks), _list(vs), strict=True)}


def _range(*args: Any) -> list[int]:
    nums = [int(_known(a)) for a in args]
    return list(range(*nums))


def _compact(v: Any) -> list[Any]:
    out = []
    for item in _list(v):
        u = unwrap(item)
        if isinstance(u, Unknown):
            raise _NeedsUnknown("compact over unknown element")
        if u is not None and u != "":
            out.append(item)
    return out


def _substr(s: Any, off: Any, length: Any) -> str:
    text = _str(s)
    o, n = int(_known(off)), int(_known(length))
    return text[o:] if n < 0 else text[o : o + n]


def _passthrough(v: Any) -> Any:
    return v


def _toset(v: Any) -> list[Any]:
    return _distinct(v)


def _can(v: Any) -> bool:
    if isinstance(v, Unknown):
        raise _NeedsUnknown("can() of unknown value")
    return True


def _one(v: Any) -> Any:
    items = _list(v)
    if len(items) > 1:
        raise ValueError("one() with more than one element")
    return items[0] if items else None


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "jsondecode": _jsondecode,
    "concat": _concat,
    "merge": _merge,
    "format": _format,
    "join": _join,
    "split": lambda sep, s: _str(s).split(_str(sep)),
    "lower": lambda s: _str(s).lower(),
    "upper": lambda s: _str(s).upper(),
    "title": lambda s: _str(s).title(),
    "trimspace": lambda s: _str(s).strip(),
    "trimprefix": lambda s, p: _str(s).removeprefix(_str(p)),
    "trimsuffix": lambda s, p: _str(s).removesuffix(_str(p)),
    "replace": _replace,
    "substr": _substr,
    "tostring": _tostring,
    "tonumber": _tonumber,
    "tobool": lambda v: bool(_known(v)),
    "tolist": lambda v: list(_list(v)),
    "toset": _toset,
    "tomap": lambda v: dict(_dict(v)),
    "lookup": _lookup,
    "element": _element,
    "length": _length,
    "coalesce": _coalesce,
    "coalescelist": lambda *ls: next((x for x in ls if _list(x)), []),
    "compact": _compact,
    "distinct": _distinct,
    "flatten": _flatten,
    "keys": _keys,
    "values": _values,
    "contains": _contains,
    "zipmap": _zipmap,
    "range": _range,
    "try": _try,
    "can": _can,
    "one": _one,
    "sensitive": _passthrough,
    "nonsensitive": _passthrough,
    "max": lambda *a: max(_num(_known(x)) for x in a),
    "min": lambda *a: min(_num(_known(x)) for x in a),
    "abs": lambda v: abs(_num(_known(v))),
    "ceil": lambda v: math.ceil(_num(_known(v))),
    "floor": lambda v: math.floor(_num(_known(v))),
    "slice": lambda lst, a, b: _list(lst)[int(_known(a)) : int(_known(b))],
    "sort": lambda lst: sorted(_str(x) for x in _list(lst)),
    "reverse": lambda lst: list(reversed(_list(lst))),
    "startswith": lambda s, p: _str(s).startswith(_str(p)),
    "endswith": lambda s, p: _str(s).endswith(_str(p)),
}


def refs_in(value: Any) -> list[Ref]:
    return collect_refs(value)
