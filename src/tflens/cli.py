"""Command line.

``tflens --dir PATH [--out model.json] [--report report.md]`` analyzes a repository;
``tflens serve --dir PATH [--host 127.0.0.1] [--port N]`` starts the local web UI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .output import dumps, run_analysis
from .report import render_report

DEFAULT_PORT = 8765


def _parse_var(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {s!r}")
    name, value = s.split("=", 1)
    return name, value


def _add_var_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--var-file",
        action="append",
        default=[],
        help="tfvars file (relative to --dir) applied to root modules; repeatable",
    )
    parser.add_argument(
        "--var", action="append", default=[], type=_parse_var, help="NAME=VALUE; repeatable"
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        return serve_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="tflens",
        description="Static, read-only Terraform analyzer (no terraform, no cloud calls).",
        epilog="Web UI: tflens serve --dir PATH [--host 127.0.0.1] [--port N]",
    )
    parser.add_argument("--dir", required=True, type=Path, help="Terraform repository to analyze")
    parser.add_argument("--out", type=Path, help="write model JSON to this file")
    parser.add_argument("--report", type=Path, help="write the markdown report to this file")
    _add_var_options(parser)
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
        report = render_report(analysis.model, root.resolve().name, analysis.repo.lines)
        args.report.write_text(report, encoding="utf-8")
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


def serve_main(argv: list[str]) -> int:
    from .server import is_loopback, make_server, serve_forever
    from .webapi import Api, ModelStore, default_model_path

    parser = argparse.ArgumentParser(
        prog="tflens serve",
        description="Local, read-only web UI over the model (graph, repo tree, evidence).",
    )
    parser.add_argument("--dir", required=True, type=Path, help="Terraform repository")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT}; 0 = any)"
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="model.json to serve (default <dir>/tflens-out/model.json; generated if missing)",
    )
    parser.add_argument(
        "--regenerate", action="store_true", help="re-analyze the repository at startup"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--allow-remote", action="store_true", help="allow binding to a non-loopback --host"
    )
    _add_var_options(parser)
    args = parser.parse_args(argv)

    root: Path = args.dir
    if not root.is_dir():
        print(f"tflens serve: error: --dir {root} is not a directory", file=sys.stderr)
        return 2
    if not is_loopback(args.host) and not args.allow_remote:
        print(
            f"tflens serve: error: refusing non-loopback --host {args.host} without --allow-remote",
            file=sys.stderr,
        )
        return 2
    store = ModelStore(
        root, args.model or default_model_path(root.resolve()), args.var_file, dict(args.var)
    )
    if args.regenerate:
        store.regenerate()
    else:
        store.model()
    print(
        f"tflens serve: model {store.model_path}"
        + (f" ({store.note})" if store.note else " (loaded)"),
        file=sys.stderr,
    )
    try:
        server = make_server(Api(store), args.host, args.port, args.allow_remote)
    except OSError as exc:
        print(
            f"tflens serve: error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr
        )
        return 1
    serve_forever(server, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
