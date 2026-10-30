"""Command line: ``tflens --dir PATH [--out model.json] [--report report.md]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .output import dumps, run_analysis
from .report import render_report


def _parse_var(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {s!r}")
    name, value = s.split("=", 1)
    return name, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tflens",
        description="Static, read-only Terraform analyzer (no terraform, no cloud calls).",
    )
    parser.add_argument("--dir", required=True, type=Path, help="Terraform repository to analyze")
    parser.add_argument("--out", type=Path, help="write model JSON to this file")
    parser.add_argument("--report", type=Path, help="write the markdown report to this file")
    parser.add_argument(
        "--var-file",
        action="append",
        default=[],
        help="tfvars file (relative to --dir) applied to root modules; repeatable",
    )
    parser.add_argument(
        "--var", action="append", default=[], type=_parse_var, help="NAME=VALUE; repeatable"
    )
    parser.add_argument("--version", action="version", version=f"tflens {__version__}")
    args = parser.parse_args(argv)

    root: Path = args.dir
    if not root.is_dir():
        print(f"tflens: error: --dir {root} is not a directory", file=sys.stderr)
        return 2

    analysis = run_analysis(root, args.var_file, dict(args.var))
    text = dumps(analysis.model)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if args.report:
        args.report.write_text(render_report(analysis.model, root.resolve().name), encoding="utf-8")
    if not args.out and not args.report:
        sys.stdout.write(text)
    else:
        s = analysis.model["summary"]
        edges = ", ".join(f"{k}={v}" for k, v in s["edges"].items()) or "none"
        print(
            f"tflens: {s['files']} files, {s['modules']} modules, "
            f"{sum(s['nodes'].values())} nodes, edges: {edges}, "
            f"{s['unknowns']} unknowns, {s['parse_errors']} parse errors",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
