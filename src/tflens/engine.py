"""Module instantiation and lazy, memoized, cycle-safe evaluation.

Resolves variables (defaults, tfvars, module inputs), locals, data sources
(``aws_iam_policy_document`` is evaluated; others become references), local
modules (inputs in, outputs back into the caller) and resource attributes.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ciscan import ci_var_inputs, ci_workspace_use
from .evaluate import Ops, apply_ops, evaluate
from .expr import Call, ObjectExpr
from .loader import Attr, Block, ModuleSource, RemoteModule, RepoSource
from .values import (
    JsonEncoded,
    Location,
    Ref,
    Unknown,
    collect_unknowns,
    display,
    is_known,
    render,
    unwrap,
)

# Provider-documented identities: these computed attributes equal a configured argument.
ATTR_ALIASES: dict[tuple[str, str], str] = {
    ("aws_s3_bucket", "id"): "bucket",
    ("aws_dynamodb_table", "id"): "name",
    ("aws_iam_role", "id"): "name",
    ("aws_iam_role", "unique_name"): "name",
    ("aws_iam_instance_profile", "id"): "name",
    ("aws_iam_user", "id"): "name",
    ("aws_lambda_function", "id"): "function_name",
    ("aws_sns_topic_subscription", "id"): "arn",
}

_CIRCULAR = object()
# terraform_remote_state config settings that identify a state (compared with the stacks'
# backend settings); at least one of the path-like ones must match.
_STATE_KEYS = (
    "bucket",
    "key",
    "prefix",
    "path",
    "storage_account_name",
    "container_name",
    "organization",
    "address",
)
_STATE_PATH_KEYS = frozenset({"key", "prefix", "path", "address"})


@dataclass
class VarInfo:
    name: str
    value: Any
    source: str
    location: Location
    declared_default: bool
    description: str | None = None


class AttrScope:
    def __init__(self, inst: ModuleInstance, location: Location | None, self_id: str | None = None):
        self.inst = inst
        self.location = location
        self.self_id = self_id

    def resolve(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        return self.inst.resolve(name, ops, self)

    def read_file(self, path: str) -> str | None:
        return self.inst.engine.read_file(path, self.inst.root_dir)


@dataclass
class ModuleInstance:
    engine: Engine
    src: ModuleSource
    address: str
    prefix: str
    root_dir: str
    parent: ModuleInstance | None = None
    call: Block | None = None
    children: dict[str, ModuleInstance | None] = field(default_factory=dict)
    child_status: dict[str, str] = field(default_factory=dict)
    child_source: dict[str, Any] = field(default_factory=dict)
    child_remote: dict[str, RemoteModule] = field(default_factory=dict)
    var_info: dict[str, VarInfo] = field(default_factory=dict)
    _memo: dict[tuple[Any, ...], Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- identity
    @property
    def module_id(self) -> str | None:
        return f"{self.prefix}{self.address}" if self.address else None

    def node_id(self, rtype: str, name: str, data: bool = False) -> str:
        local = f"data.{rtype}.{name}" if data else f"{rtype}.{name}"
        return f"{self.prefix}{self.address + '.' if self.address else ''}{local}"

    # ---------------------------------------------------------------- memo
    def memo(self, key: tuple[Any, ...], fn: Callable[[], Any]) -> Any:
        if key in self._memo:
            v = self._memo[key]
            if v is _CIRCULAR:
                self.engine.cycle_hits += 1
                return Unknown(f"circular reference while evaluating {'.'.join(map(str, key))}")
            return v
        self._memo[key] = _CIRCULAR
        v = fn()
        self._memo[key] = v
        return v

    def eval_attr(
        self, attr: Attr, self_id: str | None = None, bindings: dict[str, Any] | None = None
    ) -> Any:
        return evaluate(attr.expr, AttrScope(self, attr.loc, self_id), bindings)

    def call_id(self, name: str) -> str:
        """The id of a module call of this module (``<root>::module.a.module.b``)."""
        return f"{self.prefix}{self.address + '.' if self.address else ''}module.{name}"

    # ---------------------------------------------------------------- resolution
    def resolve(self, name: str, ops: Ops, scope: AttrScope) -> tuple[Any, Ops]:
        first = ops[0][1] if ops and ops[0][0] == "attr" else None
        if name == "var":
            if first is None:
                return Unknown("invalid variable reference"), []
            return self.var(first), ops[1:]
        if name == "local":
            if first is None:
                return Unknown("invalid local reference"), []
            return self.local(first), ops[1:]
        if name == "module":
            if first is None:
                return Unknown("invalid module reference"), []
            # module.a[0].out / module.a["k"].out / module.a[*].out: one module's outputs
            # (a node stands for all its instances)
            i = 1
            while i < len(ops) and ops[i][0] in ("index", "splat"):
                i += 1
            if i < len(ops) and ops[i][0] == "attr":
                return self.module_output(first, ops[i][1]), ops[i + 1 :]
            return self.module_outputs(first), ops[1:]
        if name == "data":
            if first is None or len(ops) < 2 or ops[1][0] != "attr":
                return Unknown("invalid data source reference"), []
            return self.data_value(first, ops[1][1], ops[2:]), []
        if name == "path":
            if first == "module":
                return self.src.dir, ops[1:]
            if first in ("root", "cwd"):  # terraform runs from the root module's directory
                return self.root_dir, ops[1:]
            return Unknown(f"path.{first} depends on the working directory"), []
        if name == "terraform":
            return self.engine.workspace(self.root_dir, scope.location), []
        if name == "count":
            only = self.only_instance(scope.self_id) or self.first_instance(scope.self_id)
            if only is not None and first == "index":
                return only[0], ops[1:]
            return Unknown("count.index differs per resource instance"), []
        if name == "each":
            only = self.only_instance(scope.self_id) or self.first_instance(scope.self_id)
            if only is not None and first in ("key", "value"):
                return only[0 if first == "key" else 1], ops[1:]
            return Unknown("each.key/each.value differ per resource instance"), []
        if name == "self":
            return Unknown("self.* is only known after apply", after_apply=True), []
        if first is not None and (name, first) in self.src.resources:
            return self.resource_ref(name, first, ops[1:]), []
        return Unknown(f"reference to undeclared object {name}{'.' + first if first else ''}"), []

    def var(self, name: str) -> Any:
        return self.memo(("var", name), lambda: self._var(name))

    def _var(self, name: str) -> Any:
        block = self.src.variables.get(name)
        if block is None:
            return Unknown(f"reference to undeclared variable var.{name}")
        desc_attr = block.attrs.get("description")
        desc = render(self.eval_attr(desc_attr)) if desc_attr else None
        has_default = "default" in block.attrs
        if self.call is not None:
            caller = self.parent
            assert caller is not None
            inp = self.call.attrs.get(name)
            if inp is not None:
                # each.* / count.index of the call: the instance its node stands for
                value = caller.eval_attr(inp, caller.call_id(self.call.labels[0]))
                # nullable = false: an explicit null input takes the default
                if unwrap(value) is not None or has_default is False or _nullable(block):
                    value = _apply_type(value, block)
                    self.var_info[name] = VarInfo(
                        name, value, f"module input ({self.call.loc})", inp.loc, has_default, desc
                    )
                    return value
        else:
            tv = self.engine.root_var(self.root_dir, name)
            if tv is not None:
                value, src_label, loc = tv
                value = _apply_type(value, block)
                self.var_info[name] = VarInfo(name, value, src_label, loc, has_default, desc)
                return value
        if has_default:
            attr = block.attrs["default"]
            value = _apply_type(self.eval_attr(attr), block)
            self.var_info[name] = VarInfo(name, value, "default", attr.loc, True, desc)
            return value
        if self.call is None:
            found = self.engine.ci_var(name)
            if found is not None:
                raw, ci_loc = found
                if raw is not None:
                    self.var_info[name] = VarInfo(name, raw, str(ci_loc), ci_loc, False, desc)
                    return raw
                value = Unknown(
                    f"variable var.{name} is set at run time by {ci_loc} (not in the repository)",
                    origin=block.loc,
                )
                self.var_info[name] = VarInfo(name, value, "unset", block.loc, False, desc)
                return value
        where = "module call" if self.call is not None else "tfvars/--var-file"
        value = Unknown(
            f"variable var.{name} has no default and no value in {where}", origin=block.loc
        )
        self.var_info[name] = VarInfo(name, value, "unset", block.loc, False, desc)
        return value

    def local(self, name: str) -> Any:
        attr = self.src.locals.get(name)
        if attr is None:
            return Unknown(f"reference to undeclared local.{name}")
        return self.memo(("local", name), lambda: self.eval_attr(attr))

    def output(self, name: str) -> Any:
        block = self.src.outputs.get(name)
        if block is None or "value" not in block.attrs:
            return Unknown(f"module {self.address or '(root)'} has no output {name!r}")
        return self.memo(("output", name), lambda: self.eval_attr(block.attrs["value"]))

    def module_output(self, mname: str, out: str) -> Any:
        if mname not in self.src.module_calls:
            return Unknown(f"reference to undeclared module.{mname}")
        child = self.children.get(mname)
        if child is None:
            # a module whose code is not here (reported once, on the call): the reference
            # to its output is still a link the code shows, not one unknown per consumer
            return Ref(self.call_id(mname), out)
        return child.output(out)

    def module_outputs(self, mname: str) -> Any:
        """``module.a`` as a whole: its outputs, per instance when it has count / for_each
        (``values(module.a)[*].arn``): each stands for the instance the node shows."""
        if mname not in self.src.module_calls:
            return Unknown(f"reference to undeclared module.{mname}")
        child = self.children.get(mname)
        if child is None:
            return Ref(self.call_id(mname), "")
        outputs = {name: child.output(name) for name in sorted(child.src.outputs)}
        call = self.src.module_calls[mname]
        if "for_each" in call.attrs:
            each = self._each_value(call.attrs["for_each"])
            if isinstance(each, dict):
                return {str(k): outputs for k in each}
            if isinstance(each, list):
                return {str(unwrap(k)): outputs for k in each}
        elif "count" in call.attrs:
            n = self.instances_of(call)[0]
            if n is not None:
                return [outputs] * n
        return outputs

    def resource_attr(self, rtype: str, name: str, attr: str) -> tuple[bool, Any]:
        """Configured value of a resource argument: (configured?, value)."""
        block = self.src.resources.get((rtype, name))
        if block is None:
            return False, None
        attr = ATTR_ALIASES.get((rtype, attr), attr)
        node_id = self.node_id(rtype, name)
        return self._block_attr(block, ("rattr", rtype, name, attr), attr, node_id)

    def data_attr(self, dtype: str, name: str, attr: str) -> tuple[bool, Any]:
        block = self.src.data.get((dtype, name))
        if block is None:
            return False, None
        node_id = self.node_id(dtype, name, data=True)
        return self._block_attr(block, ("dattr", dtype, name, attr), attr, node_id)

    def _block_attr(
        self, block: Block, key: tuple[Any, ...], attr: str, node_id: str
    ) -> tuple[bool, Any]:
        if attr in block.attrs:
            return True, self.memo(key, lambda: self.eval_attr(block.attrs[attr], node_id))
        if attr in block.blocks:
            return True, self.memo(
                key, lambda: [self.eval_block(b, node_id) for b in block.blocks[attr]]
            )
        if attr in block.dynamic:
            return True, self.memo(key, lambda: self.eval_dynamic(block, attr, node_id))
        return False, None

    def eval_block(
        self,
        block: Block,
        self_id: str | None = None,
        bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in block.attrs:
            out[name] = self.eval_attr(block.attrs[name], self_id, bindings)
        for name, children in block.blocks.items():
            out[name] = [self.eval_block(b, self_id, bindings) for b in children]
        for name in block.dynamic:
            value = self.eval_dynamic(block, name, self_id, bindings)
            current = out.get(name)
            if isinstance(current, list) and isinstance(value, list):
                out[name] = current + value
            else:
                out.setdefault(name, value)
        return out

    def expand_dynamic(
        self,
        block: Block,
        name: str,
        self_id: str | None = None,
        bindings: dict[str, Any] | None = None,
    ) -> tuple[list[tuple[Block, dict[str, Any]]] | None, Any]:
        """The ``content`` of a ``dynamic`` block with the bindings of each element:
        ``(items, for_each value)``; ``items`` is None when ``for_each`` is not known."""
        dyn = block.dynamic_blocks.get(name)
        if dyn is None or "for_each" not in dyn.attrs or not dyn.blocks.get("content"):
            return None, None
        value = unwrap(self.eval_attr(dyn.attrs["for_each"], self_id, bindings))
        if isinstance(value, dict):
            pairs = sorted(value.items(), key=lambda kv: str(kv[0]))
        elif isinstance(value, list):
            pairs = list(enumerate(value))
        elif value is None:
            pairs = []
        else:
            return None, value
        iterator = name
        if "iterator" in dyn.attrs:
            iterator = str(dyn.attrs["iterator"].raw).removeprefix("${").removesuffix("}").strip()
        content = dyn.blocks["content"][0]
        return [
            (content, {**(bindings or {}), iterator: {"key": k, "value": v}}) for k, v in pairs
        ], value

    def eval_dynamic(
        self,
        block: Block,
        name: str,
        self_id: str | None = None,
        bindings: dict[str, Any] | None = None,
    ) -> Any:
        """A ``dynamic`` block's values: one per element of its ``for_each``, like the
        blocks it generates; an Unknown (with why) when ``for_each`` is not known."""
        items, value = self.expand_dynamic(block, name, self_id, bindings)
        if items is None:
            return unknown_dynamic(name, value)
        return [self.eval_block(c, self_id, b) for c, b in items]

    def resource_ref(self, rtype: str, name: str, ops: Ops) -> Any:
        node_id = self.node_id(rtype, name)
        indexed = False
        while ops and ops[0][0] in ("index", "splat"):
            indexed = indexed or ops[0][0] == "index"
            ops = ops[1:]
        count = None if indexed else self._ref_count(self.src.resources[(rtype, name)])
        if not ops:
            return Ref(node_id, "", count=count)
        attr = ops[0][1]
        configured, value = self.resource_attr(rtype, name, attr)
        ref = Ref(node_id, attr, value, configured and is_known(value), count)
        if configured and not is_known(value):
            ref = Ref(node_id, attr, value, False, count)
        return apply_ops(ref, ops[1:])

    def _ref_count(self, block: Block) -> int | None:
        """Instances a whole-resource reference stands for (None: not one with count /
        for_each, or not known)."""
        if "count" not in block.attrs and "for_each" not in block.attrs:
            return None
        return self.instances_of(block)[0]

    def data_value(self, dtype: str, name: str, ops: Ops) -> Any:
        if (dtype, name) not in self.src.data:
            return Unknown(f"reference to undeclared data.{dtype}.{name}")
        while ops and ops[0][0] in ("index", "splat"):
            ops = ops[1:]
        node_id = self.node_id(dtype, name, data=True)
        if not ops:
            return Ref(node_id, "")
        attr = ops[0][1]
        if dtype == "aws_iam_policy_document" and attr in ("json", "minified_json"):
            return apply_ops(self.policy_document(name), ops[1:])
        if dtype == "terraform_remote_state" and attr == "outputs":
            return self.remote_output(name, ops[1:])
        configured, value = self.data_attr(dtype, name, attr)
        return apply_ops(Ref(node_id, attr, value, configured and is_known(value)), ops[1:])

    def remote_state(self, name: str) -> dict[str, Any]:
        """Where ``data.terraform_remote_state.<name>`` reads from: its backend, config and
        the stack (root) of this repository whose state that is (:meth:`Engine.match_state`)."""
        return self.memo(("remote_state", name), lambda: self._remote_state(name))

    def _remote_state(self, name: str) -> dict[str, Any]:
        block = self.src.data[("terraform_remote_state", name)]
        node_id = self.node_id("terraform_remote_state", name, data=True)
        backend = self.eval_attr(block.attrs["backend"]) if "backend" in block.attrs else None
        config: dict[str, Any] = {}
        if "config" in block.attrs:
            value = unwrap(self.eval_attr(block.attrs["config"], node_id))
            if isinstance(value, dict):
                config = {str(k): render(v) for k, v in value.items()}
        btype = render(backend) or "local"
        return {
            "backend": btype,
            "config": {k: config[k] for k in sorted(config)},
            **self.engine.match_state(self, btype, config),
        }

    def remote_output(self, name: str, ops: Ops) -> Any:
        """``data.terraform_remote_state.<name>.outputs.<x>``: the output of the stack it
        points at when that stack is analyzed here (so references cross stacks), else a
        reference to the remote state (an external link, never an unknown value)."""
        node_id = self.node_id("terraform_remote_state", name, data=True)
        fallback = apply_ops(Ref(node_id, "outputs"), ops)
        output = None
        if ops and ops[0][0] == "attr":
            output = ops[0][1]
        elif ops and ops[0][0] == "index":  # outputs["vpc_id"]
            output = render(ops[0][1])
        if output is None:
            return fallback
        link = self.remote_state(name)
        stack, env = link.get("stack"), link.get("env")
        if stack is None:
            return fallback
        reader_env = self.engine.root_envs.get(self.root_dir)
        if reader_env and env and env != reader_env:
            return fallback  # the producing stack has no state for this environment
        applied = self.engine.root_envs.get(stack)
        target = self.engine.root_instance(stack)
        if target is None or (applied and env and applied != env):
            # not analyzed here with this environment's tfvars: the producing stack's own
            # code, with its files for this environment, gives what the code fixes; an id
            # only known after that stack's apply stays a link to its remote state
            producer = self.engine.producer(stack, env)
            if producer is None or output not in producer.src.outputs:
                return fallback
            value = producer.output(output)
            return apply_ops(value, ops[1:]) if is_known(value) else fallback
        if output not in target.src.outputs:
            return fallback
        value = target.output(output)
        if isinstance(unwrap(value), Unknown):
            return fallback
        return apply_ops(value, ops[1:])

    def policy_document(self, name: str) -> Any:
        return self.memo(("policydoc", name), lambda: self._policy_document(name))

    def _policy_document(self, name: str) -> Any:
        block = self.src.data[("aws_iam_policy_document", name)]
        node_id = self.node_id("aws_iam_policy_document", name, data=True)
        statements: list[Any] = []
        locs: list[Location | None] = []
        for key in ("source_policy_documents", "source_json"):
            if key in block.attrs:
                src = self.eval_attr(block.attrs[key], node_id)
                for doc in src if isinstance(src, list) else [src]:
                    if isinstance(doc, JsonEncoded) and isinstance(doc.value, dict):
                        sts = doc.value.get("Statement", [])
                        statements.extend(sts)
                        locs.extend(
                            doc.statement_locations
                            if len(doc.statement_locations) == len(sts)
                            else [doc.location] * len(sts)
                        )
                    else:
                        statements.append(Unknown(f"{key} entry is not a known policy document"))
                        locs.append(block.attrs[key].loc)
        if "statement" in block.dynamic:
            items, _ = self.expand_dynamic(block, "statement", node_id)
            if items is None:
                statements.append(Unknown("dynamic statement block is not expanded statically"))
                locs.append(block.loc)
            else:
                for content, bindings in items:
                    statements.append(_iam_statement(self.eval_block(content, node_id, bindings)))
                    locs.append(content.loc)
        for st_block in block.blocks.get("statement", []):
            st = self.eval_block(st_block, node_id)
            statements.append(_iam_statement(st))
            locs.append(st_block.loc)
        doc = {"Version": "2012-10-17", "Statement": statements}
        return JsonEncoded(doc, block.loc, tuple(locs))

    # ---------------------------------------------------------------- instances
    def instances_of(self, block: Block) -> tuple[int | None, str]:
        """How many instances a resource, data source or module call creates in this
        module: ``(n, "")``, or ``(None, reason)`` when its count / for_each is unknown."""
        out = self.memo(("instances", block.loc), lambda: self._instances_of(block))
        return out if isinstance(out, tuple) else (None, f"count is not known: {out.reason}")

    def _instances_of(self, block: Block) -> tuple[int | None, str]:
        if "count" in block.attrs:
            return _instance_count("count", self.eval_attr(block.attrs["count"]))
        attr = block.attrs.get("for_each")
        if attr is not None:
            each = self._each_value(attr)
            return _instance_count("for_each", self.eval_attr(attr) if each is None else each)
        return 1, ""

    def _each_value(self, attr: Attr) -> Any:
        """A ``for_each``'s collection, also when it is another resource with a known
        ``for_each`` (``for_each = aws_x.y``: the same keys). None when not known."""
        value = unwrap(self.eval_attr(attr))
        if isinstance(value, Ref) and not value.attr:
            source = self.blocks_by_id().get(value.target)
            if source is not None and "for_each" in source.attrs:
                return self._each_value(source.attrs["for_each"])
        return value if isinstance(value, (dict, list)) else None

    def multiplicity(self) -> tuple[int | None, str]:
        """How many times this module instance is created (the product of the counts of
        the module calls leading to it): 0 means none of its blocks is created."""
        if self.call is None or self.parent is None:
            return 1, ""
        return self.memo(("multiplicity",), self._multiplicity)

    def _multiplicity(self) -> tuple[int | None, str]:
        assert self.call is not None and self.parent is not None
        outer, outer_reason = self.parent.multiplicity()
        n, reason = self.parent.instances_of(self.call)
        if outer == 0 or n == 0:
            return 0, ""
        if outer is None or n is None:
            return None, outer_reason if outer is None else f"module.{self.call.labels[0]} {reason}"
        return outer * n, ""

    def blocks_by_id(self) -> dict[str, Block]:
        if "_blocks" not in self._memo:
            blocks = {self.node_id(t, n): b for (t, n), b in self.src.resources.items()}
            blocks.update({self.node_id(t, n, True): b for (t, n), b in self.src.data.items()})
            blocks.update({self.call_id(n): b for n, b in self.src.module_calls.items()})
            self._memo["_blocks"] = blocks
        return self._memo["_blocks"]

    def only_instance(self, self_id: str | None) -> tuple[Any, Any] | None:
        """``(count.index | each.key, each.value)`` of a block that creates exactly one
        instance (None otherwise): those values are then known."""
        block = self.blocks_by_id().get(self_id) if self_id else None
        if block is None:
            return None
        if "count" in block.attrs:
            return (0, None) if self.instances_of(block)[0] == 1 else None
        attr = block.attrs.get("for_each")
        value = self._each_value(attr) if attr is not None else None
        if isinstance(value, dict) and len(value) == 1:
            ((key, item),) = value.items()
            return key, item
        if isinstance(value, list) and len(value) == 1 and isinstance(unwrap(value[0]), str):
            return unwrap(value[0]), unwrap(value[0])
        return None

    def first_instance(self, self_id: str | None) -> tuple[Any, Any] | None:
        """``(count.index | each.key, each.value)`` of the first instance of a block whose
        count / for_each is known (``[0]``, or the first key in terraform's lexical
        order): a node stands for all its instances and shows the first one's values
        (:meth:`instance_shown`). None when the instances are not known."""
        block = self.blocks_by_id().get(self_id) if self_id else None
        if block is None:
            return None
        if "count" in block.attrs:
            n = self.instances_of(block)[0]
            return (0, None) if n else None
        attr = block.attrs.get("for_each")
        value = self._each_value(attr) if attr is not None else None
        if isinstance(value, dict) and value:
            key = min(value, key=str)
            return key, value[key]
        if isinstance(value, list) and value:
            keys = [unwrap(v) for v in value]
            if all(isinstance(k, str) for k in keys):
                return min(keys), min(keys)
        return None

    def instance_shown(self, block: Block, self_id: str) -> str | None:
        """``count.index = 0`` / ``each.key = "a"``: the instance whose values a node with
        several instances shows (None when it has one, or they are not known)."""
        if self.only_instance(self_id) is not None:
            return None
        first = self.first_instance(self_id)
        if first is None:
            return None
        if "count" in block.attrs:
            return "count.index = 0"
        return f"each.key = {display(first[0])!s}"

    def all_vars(self) -> list[VarInfo]:
        for name in sorted(self.src.variables):
            self.var(name)
        return [self.var_info[n] for n in sorted(self.var_info)]


def unknown_dynamic(name: str, value: Any = None) -> Unknown:
    """A ``dynamic`` block whose ``for_each`` is not known."""
    base = f"dynamic block {name!r} is not expanded statically"
    if isinstance(value, Unknown):
        return Unknown(f"{base} ({value.reason})", value.refs, value.after_apply, value.origin)
    if isinstance(value, Ref):
        return Unknown(base, (value,), after_apply=True)
    return Unknown(base)


def _nullable(block: Block) -> bool:
    attr = block.attrs.get("nullable")
    return attr is None or getattr(attr.expr, "value", True) is not False


def _apply_type(value: Any, block: Block) -> Any:
    """A variable's value with its type's ``optional(type, default)`` attributes filled in,
    as terraform does; values that are not plain objects / collections are left alone."""
    attr = block.attrs.get("type")
    return value if attr is None else _coerce(value, attr.expr)


def _coerce(value: Any, type_expr: Any) -> Any:
    if not isinstance(type_expr, Call) or not type_expr.args:
        return value
    name = type_expr.name
    arg = type_expr.args[0]
    if name == "optional":
        return _coerce(value, arg)
    if name == "object" and isinstance(arg, ObjectExpr) and isinstance(value, dict):
        out = dict(value)
        for key_expr, attr_type in arg.items:
            key = getattr(key_expr, "value", None)
            if not isinstance(key, str):
                continue
            if (
                out.get(key) is None
                and isinstance(attr_type, Call)
                and (attr_type.name == "optional")
            ):
                out[key] = _optional_default(attr_type)
            elif key in out:
                out[key] = _coerce(out[key], attr_type)
        return out
    if name == "map" and isinstance(value, dict):
        return {k: _coerce(v, arg) for k, v in value.items()}
    if name in ("list", "set") and isinstance(value, list):
        return [_coerce(v, arg) for v in value]
    return value


def _optional_default(opt: Call) -> Any:
    """``optional(type, default)``: the default (null without one), of that type."""
    if len(opt.args) < 2:
        return None
    default = evaluate(opt.args[1], _NoRefScope())
    return _coerce(default, opt.args[0])


def _instance_count(meta: str, value: Any) -> tuple[int | None, str]:
    """``count`` / ``for_each`` value -> number of instances, or (None, why not known)."""
    v = unwrap(value)
    if isinstance(v, Unknown):
        return None, f"{meta} is not known: {v.reason}"
    if meta == "count":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return max(int(v), 0), ""
        if isinstance(v, Ref):
            return None, f"count = ${{{v.expr()}}} is only known after apply"
        return None, f"count = {display(v)} is not a number"
    if isinstance(v, (dict, list)):
        return len(v), ""
    if isinstance(v, Ref):
        if v.count is not None:
            return v.count, ""
        return None, f"for_each = ${{{v.expr()}}} is only known after apply"
    return None, f"for_each = {display(v)} is not a map or set"


def _iam_statement(st: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "sid" in st:
        out["Sid"] = st["sid"]
    out["Effect"] = st.get("effect", "Allow")
    for src, dst in (
        ("actions", "Action"),
        ("not_actions", "NotAction"),
        ("resources", "Resource"),
        ("not_resources", "NotResource"),
    ):
        if src in st:
            out[dst] = st[src]
    for src, dst in (("principals", "Principal"), ("not_principals", "NotPrincipal")):
        blocks = st.get(src)
        if not isinstance(blocks, list) or not blocks:
            continue
        principal: dict[str, Any] = {}
        for p in blocks:
            ptype = render(p.get("type")) or "?"
            ids = p.get("identifiers", [])
            principal.setdefault(ptype, [])
            principal[ptype].extend(ids if isinstance(ids, list) else [ids])
        if list(principal) == ["*"]:
            out[dst] = "*"
        else:
            out[dst] = principal
    conds = st.get("condition")
    if isinstance(conds, list) and conds:
        cond: dict[str, Any] = {}
        for c in conds:
            test = render(c.get("test")) or "?"
            var = render(c.get("variable")) or "?"
            cond.setdefault(test, {})[var] = c.get("values")
        out["Condition"] = cond
    return out


class Engine:
    def __init__(
        self,
        repo: RepoSource,
        var_files: list[str] | None = None,
        cli_vars: dict[str, str] | None = None,
        scope: str | None = None,
        root_var_files: dict[str, list[str]] | None = None,
        exclude_roots: set[str] | None = None,
    ) -> None:
        self.repo = repo
        self.var_files = var_files or []
        self.cli_vars = cli_vars or {}
        # only roots at or below this directory (an environment's directory)
        self.scope = None if scope in (None, "", ".") else scope
        # per stack: the tfvars of the environment selected for that root (applied after
        # ``var_files``), and the roots of the stacks' other environments (not analyzed)
        self.root_var_files = root_var_files or {}
        self.exclude_roots = exclude_roots or set()
        # the state files of the repository's stacks: [{root, env, type, settings}]
        # (detect.state_index), matched by terraform_remote_state data sources
        self.state_index: list[dict[str, Any]] = []
        # root -> the environment applied to it (its tfvars), for state matching
        self.root_envs: dict[str, str] = {}
        self.instances: list[ModuleInstance] = []
        self.roots: list[str] = []
        # root directory -> directory of the module instantiated as that root (they
        # differ for a Terragrunt unit whose terraform.source is a local module)
        self.root_modules: dict[str, str] = {}
        self.units: dict[str, Any] = {}  # root directory -> terragrunt.Unit
        self._root_vars: dict[str, dict[str, tuple[Any, str, Location]]] = {}
        # (root, env) -> that stack evaluated with that environment's files, to read its
        # outputs when it is not analyzed here (shared between the engines it creates)
        self._producers: dict[tuple[str, str], ModuleInstance | None] = {}
        self._ci_vars: dict[str, tuple[str | None, Location]] | None = None
        # how many reads met a value still being evaluated (settle: re-evaluate those)
        self.cycle_hits = 0

    # ------------------------------------------------------------ discovery
    def referenced_dirs(self) -> set[str]:
        """Directories called as local modules (by a module block or a Terragrunt unit)."""
        referenced: set[str] = set()
        for mod in self.repo.modules.values():
            for call in mod.module_calls.values():
                src = _literal_source(call)
                if src is not None and _is_local(src):
                    referenced.add(posixpath.normpath(posixpath.join(mod.dir, src)))
        for unit in self.repo.units:
            if unit.module_dir and unit.module_dir != unit.dir:
                referenced.add(unit.module_dir)
        return referenced

    def discover_roots(self) -> list[str]:
        referenced = self.referenced_dirs()
        remote = self.repo.remote_dirs
        self.root_modules = {
            d: d for d in self.repo.modules if d not in referenced and d not in remote
        }
        self.units = {}
        for unit in self.repo.units:
            self.units[unit.dir] = unit
            if unit.module_dir:
                self.root_modules[unit.dir] = unit.module_dir
        return [r for r in sorted(self.root_modules) if self.selected(r)]

    def selected(self, root: str) -> bool:
        """Whether a root is analyzed: in the scope and not another environment's root."""
        if self.scope and not in_scope(root, self.scope):
            return False
        return root not in self.exclude_roots

    def root_instance(self, root: str | None) -> ModuleInstance | None:
        """The instance of an analyzed root module (None if it is not analyzed here)."""
        if root is None:
            return None
        return next((i for i in self.instances if i.parent is None and i.root_dir == root), None)

    def match_state(
        self, inst: ModuleInstance, btype: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """The stack whose state a ``terraform_remote_state`` (backend ``btype``, ``config``
        with rendered values, None where not known) reads: ``{stack, env, status, reason}``.
        A state is matched when the backend type and every identifying setting both sides
        know (bucket, key, prefix, path, ...) are equal; ``local`` paths are relative to
        the root reading them. Of several states that match, the one of the reader's
        environment wins (the same ``key`` in one bucket per environment). A stack whose
        backend leaves its key to ``terraform init`` is matched by the directory the key
        names, when exactly one stack lives there."""
        if btype == "local":
            path = config.get("path") or "terraform.tfstate"
            config = {"path": posixpath.normpath(posixpath.join(inst.root_dir, path))}
        missing = [k for k in _STATE_KEYS if k in config and config[k] is None]
        has_path = any(k in _STATE_PATH_KEYS and config.get(k) is not None for k in _STATE_KEYS)
        if missing and not has_path:
            return {
                "stack": None,
                "env": None,
                "status": "unresolved",
                "reason": f"config {', '.join(missing)} not known statically",
            }
        known = {k: v for k, v in config.items() if v is not None}
        matches = []
        partial = []
        for entry in self.state_index:
            if entry["type"] != btype or entry["root"] == inst.root_dir:
                continue
            settings = entry["settings"]
            if btype == "local":
                path = settings.get("path") or "terraform.tfstate"
                settings = {"path": posixpath.normpath(posixpath.join(entry["root"], path))}
            shared = [k for k in _STATE_KEYS if k in known and settings.get(k) is not None]
            if not any(k in _STATE_PATH_KEYS for k in shared):
                if all(str(known[k]) == str(settings[k]) for k in shared):
                    partial.append(entry)
                continue
            if all(str(known[k]) == str(settings[k]) for k in shared):
                matches.append(entry)
        matches = matches or _named_by_key(known, partial)
        if matches:
            reader = self.root_envs.get(inst.root_dir)
            entry = next(
                (m for m in matches if reader and m.get("env") == reader),
                next((m for m in matches if not m.get("env")), matches[0]),
            )
            return {
                "stack": entry["root"],
                "env": entry.get("env"),
                "status": "linked",
                "reason": f"{btype} state of {entry['root']}"
                + (f" ({entry['env']})" if entry.get("env") else ""),
            }
        if missing:
            return {
                "stack": None,
                "env": None,
                "status": "unresolved",
                "reason": f"config {', '.join(missing)} not known statically",
            }
        return {
            "stack": None,
            "env": None,
            "status": "external",
            "reason": "no stack of this repository writes this state",
        }

    def producer(self, root: str, env: str | None) -> ModuleInstance | None:
        """The stack ``root`` evaluated with its files for ``env``, to read its outputs when
        it is not analyzed here (its blocks are not added to the graph). None when the
        repository has no state of that stack for that environment."""
        key = (root, env or "")
        if key in self._producers:
            return self._producers[key]
        entry = next(
            (e for e in self.state_index if e["root"] == root and (e.get("env") or "") == key[1]),
            None,
        )
        self._producers[key] = None
        if entry is None:
            return None
        child = Engine(self.repo, root_var_files={root: list(entry.get("var_files") or [])})
        child.state_index = self.state_index
        child._producers = self._producers
        child.root_envs = {root: env} if env else {}
        child.discover_roots()
        src = self.repo.modules.get(child.root_modules.get(root, root))
        if src is None:
            return None
        inst = child._instantiate(src, "", f"{root}::", root, None, None, ())
        self._producers[key] = inst
        return inst

    # ------------------------------------------------------------ CI inputs
    def ci_var(self, name: str) -> tuple[str | None, Location] | None:
        """A variable a CI file / script on disk sets (``-var``, ``TF_VAR_*``): its value
        when literal, None when it is computed at run time, and the line that sets it."""
        if self._ci_vars is None:
            self._ci_vars = ci_var_inputs(self.repo)
        return self._ci_vars.get(name)

    def workspace(self, root_dir: str, location: Location | None) -> Any:
        """``terraform.workspace``: what the repository's CI selects (the environment), or
        ``default`` when it has CI files that never select one."""
        use = ci_workspace_use(self.repo)
        if use == "selects":
            env = self.root_envs.get(root_dir)
            if env:
                return env
        elif use == "never":
            return "default"
        return Unknown("terraform.workspace is selected at runtime", origin=location)

    # ------------------------------------------------------------ stable values
    def settle(self) -> None:
        """Repeat until stable. Evaluation is lazy and memoized, so one pass follows every
        chain to its end; only a read of a value still being evaluated (a cycle, guarded
        by ``try()`` or real) can leave a stale unknown behind. Those are forgotten and
        evaluated again, as long as that resolves more of them."""
        previous: int | None = None
        for _ in range(MAX_SETTLE_ROUNDS):
            self.cycle_hits = 0
            open_values = sum(_evaluate_all(i) for i in self.instances)
            if not self.cycle_hits or (previous is not None and open_values >= previous):
                return
            previous = open_values
            for inst in self.instances:
                inst._memo = {k: v for k, v in inst._memo.items() if not _is_open(v)}

    def _remote(self, root_dir: str, address: str) -> tuple[RemoteModule | None, str]:
        """Materialized directory of a remote module call, from the root's modules.json."""
        manifest = self.repo.remote.get(root_dir)
        if manifest is None:
            return None, "remote modules are not fetched (no .terraform/modules/modules.json)"
        key = ".".join(address.split(".")[1::2])  # module.a.module.b -> a.b
        entry = manifest.get(key)
        if entry is None:
            path = f"{'' if root_dir == '.' else root_dir + '/'}.terraform/modules/modules.json"
            return None, f"no entry {key!r} in {path} (re-run terraform init)"
        if entry.status != "ok":
            return None, entry.status
        return entry, ""

    def run(self) -> list[ModuleInstance]:
        self.roots = self.discover_roots()
        multi = len(self.roots) > 1
        for root in self.roots:
            prefix = f"{root}::" if multi else ""
            src = self.repo.modules[self.root_modules[root]]
            self._instantiate(src, "", prefix, root, None, None, ())
        self.settle()
        return self.instances

    def _instantiate(
        self,
        src: ModuleSource,
        address: str,
        prefix: str,
        root_dir: str,
        parent: ModuleInstance | None,
        call: Block | None,
        chain: tuple[str, ...],
    ) -> ModuleInstance:
        inst = ModuleInstance(self, src, address, prefix, root_dir, parent, call)
        self.instances.append(inst)
        for name in sorted(src.module_calls):
            block = src.module_calls[name]
            source = _literal_source(block)
            inst.child_source[name] = source
            child_addr = f"{address}.module.{name}" if address else f"module.{name}"
            if source is None:
                inst.children[name] = None
                inst.child_status[name] = f"module.{name} source is missing or not a literal string"
                continue
            if not _is_local(source):
                remote, reason = self._remote(root_dir, child_addr)
                if remote is None:
                    inst.children[name] = None
                    inst.child_status[name] = (
                        f"module.{name} uses non-local source {source!r}; {reason}"
                    )
                    continue
                inst.child_remote[name] = remote
                child_dir = remote.dir
            else:
                child_dir = posixpath.normpath(posixpath.join(src.dir, source))
            child_src = self.repo.modules.get(child_dir)
            if child_src is None:
                inst.children[name] = None
                inst.child_status[name] = (
                    f"module.{name} local source {child_dir!r} has no .tf files in the repository"
                )
                continue
            if child_dir in chain or len(chain) > 32:
                inst.children[name] = None
                inst.child_status[name] = f"module.{name} is recursive ({child_dir})"
                continue
            inst.children[name] = self._instantiate(
                child_src, child_addr, prefix, root_dir, inst, block, chain + (src.dir,)
            )
        return inst

    # ------------------------------------------------------------ root vars
    def root_var(self, root_dir: str, name: str) -> tuple[Any, str, Location] | None:
        if root_dir not in self._root_vars:
            self._root_vars[root_dir] = self._load_root_vars(root_dir)
        return self._root_vars[root_dir].get(name)

    def _load_root_vars(self, root_dir: str) -> dict[str, tuple[Any, str, Location]]:
        out: dict[str, tuple[Any, str, Location]] = {}
        prefix = "" if root_dir == "." else root_dir + "/"
        # terraform's order: terraform.tfvars, terraform.tfvars.json, *.auto.tfvars(.json)
        auto = [
            f
            for f in (f"{prefix}terraform.tfvars", f"{prefix}terraform.tfvars.json")
            if f in self.repo.tfvars
        ]
        auto += sorted(
            f
            for f in self.repo.tfvars
            if f.startswith(prefix)
            and "/" not in f[len(prefix) :]
            and f.endswith((".auto.tfvars", ".auto.tfvars.json"))
        )
        chosen = self.var_files + self.root_var_files.get(root_dir, [])
        files = auto + [f for f in chosen if f in self.repo.tfvars]
        empty = _EmptyScope(self)
        unit = self.units.get(root_dir)
        if unit is not None:
            # Terragrunt passes inputs as TF_VAR_* variables: the lowest precedence.
            unit_scope = _UnitScope(self, unit)
            for name, attr in unit.inputs.items():
                unit_scope.location = attr.loc
                label = f"terragrunt inputs ({attr.loc.file})"
                out[name] = (evaluate(attr.expr, unit_scope), label, attr.loc)
        for f in files:
            for name, attr in self.repo.tfvars[f].items():
                empty.location = attr.loc
                out[name] = (evaluate(attr.expr, empty), f, attr.loc)
        for name, raw in self.cli_vars.items():
            out[name] = (raw, "--var", Location("<cli>", 0, 0))
        return out

    def read_file(self, path: str, root_dir: str) -> str | None:
        """Read a repository file (path.module values are repo-relative; plain
        relative paths are relative to the root module). Never reads outside the repo."""
        root = self.repo.root
        candidates = [Path(path)] if path.startswith("/") else [root / path, root / root_dir / path]
        for cand in candidates:
            try:
                resolved = cand.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                try:
                    return resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return None
        return None


def in_scope(root: str, scope: str) -> bool:
    return root == scope or root.startswith(scope.rstrip("/") + "/")


class _EmptyScope:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.location: Location | None = None

    def resolve(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        return Unknown(f"references are not allowed in tfvars ({name})", origin=self.location), []

    def read_file(self, path: str) -> str | None:
        return None


class _UnitScope:
    """A Terragrunt unit's ``inputs``: its locals (and its parents') and the outputs of its
    ``dependency`` blocks, read from the stack they point at in this repository."""

    def __init__(self, engine: Engine, unit: Any) -> None:
        self.engine = engine
        self.unit = unit
        self.location: Location | None = None
        self._locals: dict[str, Attr] = {}
        self._dependencies: dict[str, Block] = {}
        for cfg in [*unit.parents, unit.config]:
            self._locals.update(cfg.locals)
            self._dependencies.update(cfg.dependencies)
        self._resolving: set[str] = set()

    def resolve(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        first = ops[0][1] if ops and ops[0][0] == "attr" else None
        if name == "local" and first in self._locals and first not in self._resolving:
            self._resolving.add(first)
            value = evaluate(self._locals[first].expr, self)
            self._resolving.discard(first)
            return value, ops[1:]
        if name == "dependency" and first in self._dependencies:
            return self._dependency(first, ops[1:])
        reason = f"{name}.* in terragrunt inputs is only known when terragrunt runs"
        return Unknown(reason, origin=self.location), []

    def _dependency(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        """``dependency.<name>.outputs.<x>``: that stack's output; a link to it when the
        value only exists after its apply (never an unknown: the code shows the link)."""
        block = self._dependencies[name]
        attr = block.attrs.get("config_path")
        path = getattr(attr.expr, "value", None) if attr is not None else None
        if not isinstance(path, str):
            return Unknown(f"dependency.{name}: config_path is not a literal path"), []
        root = posixpath.normpath(posixpath.join(self.unit.dir, path))
        link = Ref(f"{root}::dependency.{name}", "")
        if len(ops) < 2 or ops[0] != ("attr", "outputs") or ops[1][0] != "attr":
            return apply_ops(link, ops), []
        output = ops[1][1]
        env = self.engine.root_envs.get(self.unit.dir)
        producer = self.engine.producer(root, env) or self.engine.producer(root, None)
        if producer is None or output not in producer.src.outputs:
            return apply_ops(link, ops), []
        value = producer.output(output)
        return (value, ops[2:]) if is_known(value) else (apply_ops(link, ops), [])

    def read_file(self, path: str) -> str | None:
        return None


MAX_SETTLE_ROUNDS = 8


def _named_by_key(config: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The states among ``candidates`` (no key of their own) of the one stack whose directory
    a remote state's key names: ``network/terraform.tfstate``, ``stacks/network/...``."""
    path = config.get("key") or config.get("prefix")
    if not isinstance(path, str) or not path:
        return []
    named = posixpath.dirname(path) if path.endswith(".tfstate") else path.rstrip("/")
    if not named:
        return []
    parts = named.split("/")
    roots = {e["root"] for e in candidates}
    hits = {r for r in roots if r == named or r.split("/")[-1] in parts}
    return [e for e in candidates if e["root"] in hits] if len(hits) == 1 else []


def _evaluate_all(inst: ModuleInstance) -> int:
    """Evaluate a module instance's variables, locals and outputs: how many are not known."""
    open_values = 0
    for name in sorted(inst.src.variables):
        open_values += _is_open(inst.var(name))
    for name in sorted(inst.src.locals):
        open_values += _is_open(inst.local(name))
    for name in sorted(inst.src.outputs):
        open_values += _is_open(inst.output(name))
    return open_values


def _is_open(value: Any) -> bool:
    """A memoized value that may resolve on another pass."""
    if isinstance(value, tuple):  # instances_of: (None, why not known)
        return bool(value) and value[0] is None
    if value is _CIRCULAR:
        return True
    if isinstance(value, dict) and value.get("status") == "unresolved":
        return True
    return bool(collect_unknowns(value))


def _literal_source(block: Block) -> str | None:
    attr = block.attrs.get("source")
    if attr is None:
        return None
    from .evaluate import evaluate as _ev

    value = _ev(attr.expr, _NoRefScope())
    return value if isinstance(value, str) else None


def _is_local(source: str) -> bool:
    return source.startswith("./") or source.startswith("../")


class _NoRefScope:
    location: Location | None = None

    def resolve(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        return Unknown("module source must be a literal"), []

    def read_file(self, path: str) -> str | None:
        return None
