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

from .evaluate import Ops, apply_ops, evaluate
from .loader import Attr, Block, ModuleSource, RepoSource
from .values import JsonEncoded, Location, Ref, Unknown, is_known, render

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
                return Unknown(f"circular reference while evaluating {'.'.join(map(str, key))}")
            return v
        self._memo[key] = _CIRCULAR
        v = fn()
        self._memo[key] = v
        return v

    def eval_attr(self, attr: Attr, self_id: str | None = None) -> Any:
        return evaluate(attr.expr, AttrScope(self, attr.loc, self_id))

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
            if len(ops) > 1 and ops[1][0] == "attr":
                return self.module_output(first, ops[1][1]), ops[2:]
            return self.module_outputs(first), ops[1:]
        if name == "data":
            if first is None or len(ops) < 2 or ops[1][0] != "attr":
                return Unknown("invalid data source reference"), []
            return self.data_value(first, ops[1][1], ops[2:]), []
        if name == "path":
            if first == "module":
                return self.src.dir, ops[1:]
            if first == "root":
                return self.root_dir, ops[1:]
            return Unknown(f"path.{first} depends on the working directory"), []
        if name == "terraform":
            return Unknown("terraform.workspace is selected at runtime"), []
        if name == "count":
            return Unknown("count.index differs per resource instance"), []
        if name == "each":
            return Unknown("each.key/each.value differ per resource instance"), []
        if name == "self":
            return Unknown("self.* is only known after apply"), []
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
                value = caller.eval_attr(inp)
                self.var_info[name] = VarInfo(
                    name, value, f"module input ({self.call.loc})", inp.loc, has_default, desc
                )
                return value
        else:
            tv = self.engine.root_var(self.root_dir, name)
            if tv is not None:
                value, src_label, loc = tv
                self.var_info[name] = VarInfo(name, value, src_label, loc, has_default, desc)
                return value
        if has_default:
            attr = block.attrs["default"]
            value = self.eval_attr(attr)
            self.var_info[name] = VarInfo(name, value, "default", attr.loc, True, desc)
            return value
        where = "module call" if self.call is not None else "tfvars/--var-file"
        value = Unknown(f"variable var.{name} has no default and no value in {where}")
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
            return Unknown(self.child_status.get(mname, f"module.{mname} not analyzed"))
        return child.output(out)

    def module_outputs(self, mname: str) -> Any:
        child = self.children.get(mname)
        if child is None:
            return Unknown(self.child_status.get(mname, f"module.{mname} not analyzed"))
        return {name: child.output(name) for name in sorted(child.src.outputs)}

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
        return False, None

    def eval_block(self, block: Block, self_id: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in block.attrs:
            out[name] = self.eval_attr(block.attrs[name], self_id)
        for name, children in block.blocks.items():
            out[name] = [self.eval_block(b, self_id) for b in children]
        for name in block.dynamic:
            out.setdefault(name, Unknown(f"dynamic block {name!r} is not expanded statically"))
        return out

    def resource_ref(self, rtype: str, name: str, ops: Ops) -> Any:
        node_id = self.node_id(rtype, name)
        while ops and ops[0][0] in ("index", "splat"):
            ops = ops[1:]
        if not ops:
            return Ref(node_id, "")
        attr = ops[0][1]
        configured, value = self.resource_attr(rtype, name, attr)
        ref = Ref(node_id, attr, value, configured and is_known(value))
        if configured and not is_known(value):
            ref = Ref(node_id, attr, value, False)
        return apply_ops(ref, ops[1:])

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
        configured, value = self.data_attr(dtype, name, attr)
        return apply_ops(Ref(node_id, attr, value, configured and is_known(value)), ops[1:])

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
            statements.append(Unknown("dynamic statement block is not expanded statically"))
            locs.append(block.loc)
        for st_block in block.blocks.get("statement", []):
            st = self.eval_block(st_block, node_id)
            statements.append(_iam_statement(st))
            locs.append(st_block.loc)
        doc = {"Version": "2012-10-17", "Statement": statements}
        return JsonEncoded(doc, block.loc, tuple(locs))

    def all_vars(self) -> list[VarInfo]:
        for name in sorted(self.src.variables):
            self.var(name)
        return [self.var_info[n] for n in sorted(self.var_info)]


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
    ) -> None:
        self.repo = repo
        self.var_files = var_files or []
        self.cli_vars = cli_vars or {}
        self.instances: list[ModuleInstance] = []
        self.roots: list[str] = []
        self._root_vars: dict[str, dict[str, tuple[Any, str, Location]]] = {}

    # ------------------------------------------------------------ discovery
    def discover_roots(self) -> list[str]:
        referenced: set[str] = set()
        for mod in self.repo.modules.values():
            for call in mod.module_calls.values():
                src = _literal_source(call)
                if src is not None and _is_local(src):
                    referenced.add(posixpath.normpath(posixpath.join(mod.dir, src)))
        roots = sorted(d for d in self.repo.modules if d not in referenced)
        return roots

    def run(self) -> list[ModuleInstance]:
        self.roots = self.discover_roots()
        multi = len(self.roots) > 1
        for root in self.roots:
            prefix = f"{root}::" if multi else ""
            self._instantiate(self.repo.modules[root], "", prefix, root, None, None, ())
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
                inst.children[name] = None
                inst.child_status[name] = (
                    f"module.{name} uses non-local source {source!r}; "
                    "remote modules are not fetched (no terraform init)"
                )
                continue
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
        auto = []
        if f"{prefix}terraform.tfvars" in self.repo.tfvars:
            auto.append(f"{prefix}terraform.tfvars")
        auto += sorted(
            f
            for f in self.repo.tfvars
            if f.startswith(prefix) and "/" not in f[len(prefix) :] and f.endswith(".auto.tfvars")
        )
        files = auto + [f for f in self.var_files if f in self.repo.tfvars]
        empty = _EmptyScope(self)
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


class _EmptyScope:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.location: Location | None = None

    def resolve(self, name: str, ops: Ops) -> tuple[Any, Ops]:
        return Unknown(f"references are not allowed in tfvars ({name})"), []

    def read_file(self, path: str) -> str | None:
        return None


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
