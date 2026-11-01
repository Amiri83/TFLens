"""Explicit graph model consumed by the report and (later) the web UI.

Nodes are resources, data sources, modules and external entities (CIDRs, AWS
services, runtimes, ARNs outside the repo). Edges carry a kind, source/target
node ids, attributes and the exact evidence (file, line range, snippet).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .values import Location

SCHEMA_VERSION = "1"

EDGE_KINDS = ("ACCESS", "TRIGGER", "NETWORK", "CONFIG", "RUNS_ON")


@dataclass(frozen=True)
class Evidence:
    file: str
    start_line: int
    end_line: int
    snippet: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
        }
        if self.note:
            d["note"] = self.note
        return d

    @property
    def where(self) -> str:
        return str(Location(self.file, self.start_line, self.end_line))


@dataclass
class Node:
    id: str
    kind: str  # resource | data | module | external
    type: str
    name: str
    service: str = ""
    category: str = "resource"  # resource | relationship | policy | external | module | data
    parent: str | None = None
    module: str = ""
    location: Location | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "type": self.type,
            "name": self.name,
            "service": self.service,
            "category": self.category,
            "parent": self.parent,
            "module": self.module,
            "location": self.location.to_dict() if self.location else None,
            "properties": _sorted(self.properties),
            "attributes": _sorted(self.attributes),
        }


@dataclass
class Edge:
    kind: str
    source: str
    target: str
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    via: list[str] = field(default_factory=list)
    id: str = ""

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.kind,
            self.source,
            self.target,
            self.label,
            json.dumps(self.attributes, sort_keys=True, default=str),
            tuple(e.where for e in self.evidence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "attributes": _sorted(self.attributes),
            "via": list(self.via),
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass(frozen=True)
class UnknownEntry:
    subject: str
    attribute: str
    reason: str
    location: Location | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "attribute": self.attribute,
            "reason": self.reason,
            "location": self.location.to_dict() if self.location else None,
        }

    def sort_key(self) -> tuple[Any, ...]:
        loc = self.location
        return (
            self.subject,
            self.attribute,
            self.reason,
            (loc.file, loc.start_line) if loc else ("", 0),
        )


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    unknowns: list[UnknownEntry] = field(default_factory=list)
    # blocks (and module calls) whose count / for_each is known to be 0: no node
    not_created: list[dict[str, Any]] = field(default_factory=list)
    _edge_keys: set[tuple[Any, ...]] = field(default_factory=set, repr=False)
    _unknown_keys: set[UnknownEntry] = field(default_factory=set, repr=False)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is not None:
            return existing
        self.nodes[node.id] = node
        return node

    def external(self, ext_kind: str, value: str, label: str | None = None) -> str:
        node_id = f"external:{ext_kind}:{value}"
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(
                id=node_id,
                kind="external",
                type=ext_kind,
                name=label or value,
                category="external",
            )
        return node_id

    def add_edge(self, edge: Edge) -> None:
        key = edge.sort_key()
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append(edge)

    def add_unknown(
        self, subject: str, attribute: str, reason: str, location: Location | None
    ) -> None:
        entry = UnknownEntry(subject, attribute, reason, location)
        if entry not in self._unknown_keys:
            self._unknown_keys.add(entry)
            self.unknowns.append(entry)

    def omit(self, subject: str, reason: str, location: Location | None) -> None:
        self.not_created.append(
            {
                "subject": subject,
                "reason": reason,
                "location": location.to_dict() if location else None,
            }
        )

    def finalize(self) -> None:
        self.not_created.sort(key=lambda d: d["subject"])
        self.edges.sort(key=Edge.sort_key)
        counts: dict[tuple[str, str, str], int] = {}
        for e in self.edges:
            k = (e.kind, e.source, e.target)
            counts[k] = counts.get(k, 0) + 1
            e.id = f"{e.kind}:{e.source}->{e.target}#{counts[k]}"
        self.unknowns.sort(key=UnknownEntry.sort_key)

    def out_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id and (kind is None or e.kind == kind)]

    def in_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        return [e for e in self.edges if e.target == node_id and (kind is None or e.kind == kind)]

    def unknowns_of(self, node_id: str) -> list[UnknownEntry]:
        return [u for u in self.unknowns if u.subject == node_id]


def _sorted(d: dict[str, Any]) -> dict[str, Any]:
    return {k: d[k] for k in sorted(d)}
