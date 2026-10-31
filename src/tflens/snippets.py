"""Line-numbered, highlighted evidence snippets (shared by report.md and the web UI).

A snippet is a list of ``{"line": int | None, "text": str, "highlight": bool}``.
Highlighted lines are exactly the evidence range; up to ``context`` lines around
it are added (not highlighted) when the source file is available. Output depends
only on the evidence and the file contents, so it is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SourceLines = Callable[[str], "list[str] | None"]

DEFAULT_CONTEXT = 2
MAX_LINES = 40


def snippet_lines(
    evidence: dict[str, Any], source: SourceLines | None = None, context: int = DEFAULT_CONTEXT
) -> list[dict[str, Any]]:
    """Lines of an evidence (or location) dict, highlighted, with optional context."""
    file, start, end = evidence["file"], evidence["start_line"], evidence["end_line"]
    text = evidence.get("snippet")
    lines = source(file) if source is not None else None
    if text is None and lines is not None and 1 <= start <= len(lines):
        # a bare location (node definition): take the range from the file
        body = lines[start - 1 : end]
        if len(body) > MAX_LINES:
            body = body[:MAX_LINES] + [f"... ({end - start + 1 - MAX_LINES} more lines)"]
        text = "\n".join(body)
    out: list[dict[str, Any]] = []
    if text is None:
        return out
    body = text.split("\n")
    usable = lines is not None and 1 <= start <= len(lines)
    if usable:
        assert lines is not None
        for n in range(max(1, start - context), start):
            out.append({"line": n, "text": lines[n - 1], "highlight": False})
    truncated = False
    for i, line in enumerate(body):
        if i == len(body) - 1 and line.startswith("... (") and line.endswith(" more lines)"):
            out.append({"line": None, "text": line, "highlight": False})
            truncated = True
            break
        out.append({"line": start + i, "text": line, "highlight": True})
    if usable and not truncated:
        assert lines is not None
        for n in range(end + 1, min(len(lines), end + context) + 1):
            out.append({"line": n, "text": lines[n - 1], "highlight": False})
    return out


def format_snippet(lines: list[dict[str, Any]]) -> str:
    """Plain-text rendering: ``>`` marks highlighted lines, then the line number."""
    numbered = [ln["line"] for ln in lines if ln["line"] is not None]
    width = len(str(max(numbered))) if numbered else 1
    out = []
    for ln in lines:
        mark = ">" if ln["highlight"] else " "
        num = str(ln["line"]) if ln["line"] is not None else ""
        out.append(f"{mark} {num:>{width}} | {ln['text']}".rstrip())
    return "\n".join(out)


def location_label(loc: dict[str, Any] | None) -> str:
    if not loc:
        return "?"
    if loc["start_line"] == loc["end_line"]:
        return f"{loc['file']}:{loc['start_line']}"
    return f"{loc['file']}:{loc['start_line']}-{loc['end_line']}"
