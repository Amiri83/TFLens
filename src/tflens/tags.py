"""Tags: what each resource is tagged with, and how each stack tags its resources.

Detection is generic, from the code only: a provider's ``default_tags { tags = ... }``
(``default_labels`` for providers that call them labels), the resource's own ``tags`` /
``labels`` argument, whatever produces them (a tagging module's output, a ``merge()`` of
locals, a variable passed down through module calls) evaluated like any other value. The
provider configuration of a resource is found as terraform does: its ``provider``
meta-argument, else the provider named by its type's prefix, followed up the module
calls through their ``providers`` maps. A tag whose value is not known statically is
listed with ``UNKNOWN``, never dropped.
"""

from __future__ import annotations

import re
from typing import Any

from .engine import ModuleInstance
from .expr import normalize_key
from .loader import Block
from .values import collect_unknowns, display, render, unwrap

TAG_ATTRS = ("tags", "labels")
UNKNOWN = "UNKNOWN"
_TAGGY = re.compile(r"(tag|label)", re.I)
_PROVIDER_REF = re.compile(r"^\$?\{?\s*([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_-]+))?\s*\}?$")
_MODULE_OUT = re.compile(r"\bmodule\.([A-Za-z_][\w-]*)\.([A-Za-z_][\w-]*)")
_LOCAL_REF = re.compile(r"\blocal\.([A-Za-z_][\w-]*)")


def _provider_ref(raw: Any) -> tuple[str, str | None] | None:
    """``aws.west`` / ``${aws.west}`` -> ``("aws", "west")``."""
    if not isinstance(raw, str):
        return None
    m = _PROVIDER_REF.match(raw.strip())
    return (m.group(1), m.group(2)) if m else None


def _alias(inst: ModuleInstance, provider: Block) -> str | None:
    attr = provider.attrs.get("alias")
    return render(inst.eval_attr(attr)) if attr is not None else None


def provider_config(
    inst: ModuleInstance, block: Block, rtype: str
) -> tuple[ModuleInstance, Block] | None:
    """The ``provider`` block a resource uses, with the module instance it is in."""
    ref = _provider_ref(block.attrs["provider"].raw) if "provider" in block.attrs else None
    name, alias = ref if ref else (rtype.split("_", 1)[0], None)
    i: ModuleInstance | None = inst
    while i is not None:
        for p in i.src.providers:
            if p.labels and p.labels[0] == name and _alias(i, p) == alias:
                return i, p
        call = i.call
        if call is None or i.parent is None:
            return None
        mapping = call.attrs.get("providers")
        key = name if alias is None else f"{name}.{alias}"
        mapped = None
        if mapping is not None and isinstance(mapping.raw, dict):
            for k, v in mapping.raw.items():
                if str(normalize_key(k)).strip('"') == key:
                    mapped = _provider_ref(v)
                    break
        if mapped is not None:
            name, alias = mapped
        elif alias is not None:
            return None  # an aliased provider must be passed explicitly
        i = i.parent
    return None


def default_tags(inst: ModuleInstance, provider: Block) -> tuple[Any, Any] | None:
    """``(value, location)`` of a provider's default tags (None if it sets none)."""
    for b in provider.blocks.get("default_tags", []):
        attr = b.attrs.get("tags")
        if attr is not None:
            return inst.eval_attr(attr), attr.loc
    attr = provider.attrs.get("default_labels")
    if attr is not None:
        return inst.eval_attr(attr), attr.loc
    return None


def _text(value: Any) -> str:
    """A tag value: the string, ``${...}`` for one only known after apply (another
    stack's output, a resource attribute), UNKNOWN for one not known statically."""
    s = render(value)
    if s is not None:
        return s
    return UNKNOWN if collect_unknowns(value) else display(value)


def _add(out: dict[str, Any], value: Any, source: str, loc: Any) -> None:
    v = unwrap(value)
    if isinstance(v, dict):
        for k in sorted(v):
            out["tags"][str(k)] = _text(v[k])
            out["sources"][str(k)] = source
            if render(v[k]) is None:
                out["unknown"].append({"key": str(k), "reason": _reason(v[k]), "source": source})
    elif v is not None:
        out["unknown"].append({"key": "*", "reason": _reason(v), "source": source})
    out["locations"][source] = loc.to_dict()


def _reason(value: Any) -> str:
    unknown = collect_unknowns(value)
    if unknown:
        return unknown[0][1].reason
    return f"{display(value)} is only known after apply"


def resource_tags(
    inst: ModuleInstance, block: Block, rtype: str, node_id: str, inherits: bool
) -> dict[str, Any] | None:
    """The tags a resource gets: its provider's default tags (when ``inherits``: a
    resource, not a data source or relationship) overridden by its own ``tags``.
    ``{tags: {key: value}, sources: {key: default_tags | resource}, unknown: [...],
    locations: {source: loc}, provider}``; None when it has none."""
    out: dict[str, Any] = {"tags": {}, "sources": {}, "unknown": [], "locations": {}}
    if inherits:
        found = provider_config(inst, block, rtype)
        if found is not None:
            pinst, provider = found
            defaults = default_tags(pinst, provider)
            if defaults is not None:
                alias = _alias(pinst, provider)
                out["provider"] = provider.labels[0] + (f".{alias}" if alias else "")
                _add(out, defaults[0], "default_tags", defaults[1])
    for name in TAG_ATTRS:
        attr = block.attrs.get(name)
        if attr is not None:
            _add(out, inst.eval_attr(attr, node_id), "resource", attr.loc)
            break
    if not out["tags"] and not out["unknown"]:
        return None
    out["tags"] = {k: out["tags"][k] for k in sorted(out["tags"])}
    out["sources"] = {k: out["sources"][k] for k in sorted(out["sources"])}
    return out


def tagging(insts: list[ModuleInstance]) -> dict[str, Any]:
    """How a stack tags: providers' default tags, tagging modules (a module whose output
    feeds tags) and ``merge()`` locals building tags, each with where it is."""
    defaults: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    seen_modules: set[tuple[str, str]] = set()
    for inst in insts:
        tag_exprs: list[str] = []
        for p in inst.src.providers:
            found = default_tags(inst, p)
            if found is None:
                continue
            value = unwrap(found[0])
            alias = _alias(inst, p)
            defaults.append(
                {
                    "provider": (p.labels[0] if p.labels else "?") + (f".{alias}" if alias else ""),
                    "module": inst.module_id or inst.root_dir,
                    "keys": sorted(map(str, value)) if isinstance(value, dict) else [],
                    "location": found[1].to_dict(),
                }
            )
            for b in p.blocks.get("default_tags", []):
                tag_exprs += [str(a.raw) for a in b.attrs.values()]
            if "default_labels" in p.attrs:
                tag_exprs.append(str(p.attrs["default_labels"].raw))
        for block in inst.src.resources.values():
            tag_exprs += [str(block.attrs[a].raw) for a in TAG_ATTRS if a in block.attrs]
        for call in inst.src.module_calls.values():
            tag_exprs += [str(a.raw) for n, a in call.attrs.items() if _TAGGY.search(n)]
        # locals building tags: tag-named, or read where tags are set (followed through
        # the locals they read)
        wanted = {n for n in inst.src.locals if _TAGGY.search(n)}
        for text in tag_exprs:
            wanted |= set(_LOCAL_REF.findall(text))
        todo = list(wanted)
        while todo:
            name = todo.pop()
            attr = inst.src.locals.get(name)
            if attr is None:
                continue
            for ref in _LOCAL_REF.findall(str(attr.raw)):
                if ref not in wanted:
                    wanted.add(ref)
                    todo.append(ref)
        for name in sorted(wanted):
            attr = inst.src.locals.get(name)
            if attr is None:
                continue
            raw = str(attr.raw)
            tag_exprs.append(raw)
            if "merge(" in raw:
                merges.append(
                    {
                        "name": f"local.{name}",
                        "module": inst.module_id or inst.root_dir,
                        "location": attr.loc.to_dict(),
                    }
                )
        for text in tag_exprs:
            for mname, output in _MODULE_OUT.findall(text):
                call = inst.src.module_calls.get(mname)
                key = (inst.module_id or inst.root_dir, mname)
                if call is None or key in seen_modules:
                    continue
                seen_modules.add(key)
                child = inst.children.get(mname)
                modules.append(
                    {
                        "address": (
                            f"{inst.address}.module.{mname}" if inst.address else f"module.{mname}"
                        ),
                        "output": output,
                        "dir": child.src.dir if child is not None else None,
                        "source": inst.child_source.get(mname),
                        "location": call.loc.to_dict(),
                    }
                )
    return {"default_tags": defaults, "tag_modules": modules, "merge_locals": merges}
