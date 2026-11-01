"""Why a block is counted the way it is: the ``count`` / ``for_each`` on the way to it.

A resource is created ``count x (the counts of the module calls leading to it)`` times.
For every such ``count`` / ``for_each`` (outermost module call first, the block's own
last) this records the expression as written, the value it resolved to (or why it is
not known) and where each variable / local it reads comes from: a tfvars file, a
variable default, a module input (followed to the caller), a local, ``--var``, or unset.
A count that is not known is never assumed to be 1: callers show it as uncertain with
this explanation (``node["properties"]["count_why"]``).
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

from .detect import _source_text
from .engine import ModuleInstance
from .expr import Postfix, Var
from .loader import Attr, Block
from .values import display, is_known

MAX_DEPTH = 6
MAX_REFS = 8
MAX_VALUE = 80


def _refs(expr: Any) -> list[tuple[str, str]]:
    """``(var | local, name)`` read by an expression, in order, each once."""
    out: list[tuple[str, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, Postfix) and isinstance(x.base, Var) and x.base.name in ("var", "local"):
            if x.ops and x.ops[0][0] == "attr":
                out.append((x.base.name, x.ops[0][1]))
            for op, arg in x.ops[1:]:
                if op == "index":
                    walk(arg)
            return
        if is_dataclass(x) and not isinstance(x, type):
            for f in fields(x):
                walk(getattr(x, f.name))
        elif isinstance(x, (tuple, list)):
            for y in x:
                walk(y)

    walk(expr)
    return list(dict.fromkeys(out))[:MAX_REFS]


def _short(value: Any) -> str:
    text = display(value)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def expression(inst: ModuleInstance, attr: Attr) -> str:
    """An argument's expression as written (first line), else its evaluated rendering."""
    return _source_text(inst.engine.repo, attr) or _short(inst.eval_attr(attr))


def _origin(source: str) -> str:
    if source in ("default", "unset", "--var"):
        return source
    if source.startswith("module input"):
        return "module input"
    if source.startswith("terragrunt inputs"):
        return "terragrunt inputs"
    return "tfvars"


def sources(inst: ModuleInstance, expr: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Where the variables / locals an expression reads get their value, followed through
    module inputs and locals (``from``)."""
    out = []
    for kind, name in _refs(expr):
        entry: dict[str, Any] = {"ref": f"{kind}.{name}", "module": inst.module_id or "root"}
        if kind == "var":
            value = inst.var(name)
            info = inst.var_info.get(name)
            origin = _origin(info.source) if info else "unset"
            entry.update(value=_short(value), known=is_known(value), origin=origin)
            if info is not None:
                entry["location"] = info.location.to_dict()
                if origin == "tfvars":
                    entry["file"] = info.source
            if origin == "module input" and inst.call is not None and inst.parent is not None:
                inp = inst.call.attrs.get(name)
                if inp is not None and depth < MAX_DEPTH:
                    entry["expression"] = expression(inst.parent, inp)
                    entry["from"] = sources(inst.parent, inp.expr, depth + 1)
        else:
            attr = inst.src.locals.get(name)
            value = inst.local(name)
            entry.update(value=_short(value), known=is_known(value), origin="local")
            if attr is not None:
                entry["location"] = attr.loc.to_dict()
                if depth < MAX_DEPTH:
                    entry["expression"] = expression(inst, attr)
                    entry["from"] = sources(inst, attr.expr, depth + 1)
        out.append(entry)
    return out


def _loc_text(loc: dict[str, Any] | None) -> str:
    return f"{loc['file']}:{loc['start_line']}" if loc else "?"


def source_text(s: dict[str, Any]) -> str:
    """``var.enabled = true (module input main.tf:22: var.lambdas_enabled = true from
    env/dev.tfvars:2)``."""
    origin, loc = s["origin"], s.get("location")
    if origin == "unset":
        head = f"{s['ref']} is not set (no default, no value in tfvars)"
    elif not s["known"]:
        head = f"{s['ref']} is not known ({origin} {_loc_text(loc)})"
    elif origin == "tfvars":
        head = f"{s['ref']} = {s['value']} from {_loc_text(loc)}"
    elif origin == "default":
        head = f"{s['ref']} = {s['value']} (default, {_loc_text(loc)})"
    elif origin == "--var":
        head = f"{s['ref']} = {s['value']} (--var)"
    else:
        head = f"{s['ref']} = {s['value']} ({origin} {_loc_text(loc)})"
    inner = s.get("from") or []
    if inner:
        head += " ← " + "; ".join(source_text(x) for x in inner)
    return head


def step_text(step: dict[str, Any]) -> str:
    """``module.api.module.base: count = var.enabled ? 1 : 0 → 1 (var.enabled = ...)``."""
    n = step["instances"]
    head = f"{step['address']}: {step['meta']} = {step['expression']} → "
    head += str(n) if n is not None else "not known"
    srcs = "; ".join(source_text(s) for s in step["sources"])
    return head + (f" ({srcs})" if srcs else "")


def count_why(inst: ModuleInstance, block: Block | None, address: str) -> dict[str, Any] | None:
    """The ``count`` / ``for_each`` steps of a block (``block``, at ``address``) or of a
    module instance (``block`` None), outermost module call first; None when there is
    none (the block is created exactly once)."""
    chain: list[tuple[ModuleInstance, Block, str]] = []
    i = inst
    while i.call is not None and i.parent is not None:
        chain.append((i.parent, i.call, i.module_id or ""))
        i = i.parent
    chain.reverse()
    if block is not None:
        chain.append((inst, block, address))
    steps = []
    for owner, blk, addr in chain:
        for meta in ("count", "for_each"):
            attr = blk.attrs.get(meta)
            if attr is None:
                continue
            n, reason = owner.instances_of(blk)
            value = owner.eval_attr(attr)
            step: dict[str, Any] = {
                "address": addr,
                "meta": meta,
                "expression": expression(owner, attr),
                "value": _short(value) if is_known(value) else "not known",
                "instances": n,
                "location": attr.loc.to_dict(),
                "sources": sources(owner, attr.expr),
            }
            if n is None:
                step["reason"] = reason
            steps.append(step)
    if not steps:
        return None
    known = all(s["instances"] is not None for s in steps)
    return {
        "known": known,
        "steps": steps,
        "text": "; ".join(step_text(s) for s in steps),
    }
