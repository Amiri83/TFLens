"""Command line.

``tflens --dir PATH [--out model.json] [--report report.md] [--overview overview.md]``
analyzes a repository (exit status 1 when there is nothing to analyze);
``tflens serve --dir PATH`` serves the web UI over that repository's model (no data
directory, no database); ``tflens serve`` alone runs the full local app (Settings,
Accounts, Repos, Runs, Logs) over the data directory ``$TFLENS_HOME`` / ``~/.tflens``.
``tflens`` with no arguments is the same as ``tflens serve``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import __version__
from .output import dumps, listing_text, run_analysis
from .overview import build_overview, render_overview_markdown
from .report import render_report
from .settings import DEFAULT_HOST, DEFAULT_PORT

# waitress worker threads serving requests.
THREADS = 8
# Bind addresses that mean "every interface": the browser URL uses 127.0.0.1 for them.
WILDCARD_HOSTS = ("0.0.0.0", "::")  # noqa: S104 - names, nothing is bound here


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
    if not argv:  # bare ``tflens`` / ``python -m tflens``: same as ``tflens serve``
        return serve_main([])
    if argv[0] == "serve":
        return serve_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="tflens",
        description="Static, read-only Terraform analyzer (no terraform, no cloud calls).",
        epilog="Web UI: tflens serve --dir PATH [--host 127.0.0.1] [--port N]",
    )
    parser.add_argument("--dir", required=True, type=Path, help="Terraform repository to analyze")
    parser.add_argument("--out", type=Path, help="write model JSON to this file")
    parser.add_argument("--report", type=Path, help="write the markdown report to this file")
    parser.add_argument(
        "--overview", type=Path, help="write the markdown overview (what the repo deploys)"
    )
    parser.add_argument(
        "--handoff",
        type=Path,
        help="write handoff.md (overview + Mermaid diagrams) for Claude Code",
    )
    parser.add_argument(
        "--diagrams",
        type=Path,
        help="write the diagrams (architecture, module-tree, env-matrix) as .mmd and .svg "
        "into this directory (.png too when an offline SVG renderer is installed)",
    )
    parser.add_argument(
        "--no-git", action="store_true", help="do not read the git history (git log)"
    )
    _add_var_options(parser)
    parser.add_argument("--version", action="version", version=f"tflens {__version__}")
    args = parser.parse_args(argv)

    root: Path = args.dir
    if not root.is_dir():
        print(f"tflens: error: --dir {root} is not a directory", file=sys.stderr)
        return 2

    analysis = run_analysis(root, args.var_file, dict(args.var), git=not args.no_git)
    text = dumps(analysis.model)
    title = root.resolve().name
    listing = listing_text(analysis.model["discovery"]["inventory"])
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if args.report:
        report = render_report(analysis.model, title, analysis.repo.lines)
        args.report.write_text(report, encoding="utf-8")
    if args.overview:
        ov = build_overview(analysis.model, title, analysis.repo.lines)
        args.overview.write_text(render_overview_markdown(ov, listing), encoding="utf-8")
    if args.handoff:
        from .handoff import render_handoff

        handoff = render_handoff(analysis.model, title, analysis.repo.lines)
        args.handoff.write_text(handoff, encoding="utf-8")
    if args.diagrams:
        from .visuals import export_diagrams

        exported = export_diagrams(analysis.model, args.diagrams, title)
        print(f"tflens: diagrams in {args.diagrams}: {exported['png']}", file=sys.stderr)
    for s in analysis.repo.skipped:
        if s.kind == "unreadable":
            print(f"tflens: warning: skipped {s.path}/: {s.reason}", file=sys.stderr)
    if analysis.failed:
        print(f"tflens: error: {analysis.failure}\n{listing}", file=sys.stderr)
        return 1
    if not any((args.out, args.report, args.overview, args.handoff, args.diagrams)):
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


def browse_url(host: str, port: int) -> str:
    from .server import is_loopback

    if is_loopback(host) or host in WILDCARD_HOSTS:
        return f"http://127.0.0.1:{port}/"
    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


def remote_warning(host: str, port: int) -> str:
    return (
        f"WARNING: TFLens is listening on {host}:{port} and is reachable from other "
        "machines. It has NO authentication: anyone who can reach this port can read the "
        "analyzed repositories' source code. Use --allow-remote only on a trusted network."
    )


def desktop_session() -> bool:
    """True if a browser can be opened for the user (not over a bare SSH session)."""
    if sys.platform in ("darwin", "win32"):
        return True
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        # Without a display only an explicitly configured browser is used.
        return bool(os.environ.get("BROWSER"))
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def open_browser(url: str) -> None:
    threading.Thread(
        target=webbrowser.open, args=(url,), name="tflens-browser", daemon=True
    ).start()


@contextlib.contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    """Treat SIGTERM like Ctrl+C so ``kill`` shuts down just as cleanly."""

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def serve_waitress(app: Any, host: str, port: int, ready: Callable[[int], None]) -> None:
    """Serve ``app`` with waitress until Ctrl+C / SIGTERM (no ``Server`` header);
    ``ready`` is called with the bound port (``--port 0`` picks a free one)."""
    from waitress import create_server

    server = create_server(app, host=host, port=port, threads=THREADS, ident="")
    try:
        ready(int(server.effective_port))
        server.run()
    finally:
        server.close()


def serve_main(argv: list[str]) -> int:
    from .server import is_loopback

    parser = argparse.ArgumentParser(
        prog="tflens serve",
        description="Local, read-only web UI. With --dir: the graph / repo / evidence views "
        "of one repository (no data directory). Without: the full app over the data "
        "directory (Settings, Accounts, Repos, Runs, Logs).",
    )
    parser.add_argument("--dir", type=Path, help="Terraform repository (standalone mode)")
    parser.add_argument(
        "--home", default=None, help="data directory (default: $TFLENS_HOME or ~/.tflens)"
    )
    parser.add_argument(
        "--host", default=None, help=f"bind address (default {DEFAULT_HOST}, local only)"
    )
    parser.add_argument(
        "--port", type=int, default=None, help=f"port (default {DEFAULT_PORT}; 0 = any)"
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="with --dir: model.json to serve (default <dir>/tflens-out/model.json; "
        "generated if missing)",
    )
    parser.add_argument(
        "--regenerate", action="store_true", help="with --dir: re-analyze at startup"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback --host (there is NO authentication)",
    )
    _add_var_options(parser)
    args = parser.parse_args(argv)

    standalone = args.dir is not None
    if not standalone and (args.model or args.regenerate or args.var_file or args.var):
        parser.error("--model, --regenerate, --var-file and --var need --dir")
    if standalone and args.home:
        parser.error("--home has no effect with --dir (standalone mode uses no data directory)")

    if standalone:
        host = args.host or DEFAULT_HOST
        port = DEFAULT_PORT if args.port is None else args.port
        allow_remote = args.allow_remote
        if not args.dir.is_dir():
            print(f"tflens serve: error: --dir {args.dir} is not a directory", file=sys.stderr)
            return 2
    else:
        from .config import default_paths
        from .settings import SettingsStore, effective_server

        # Reads the stored server settings; never creates the database.
        stored = SettingsStore(default_paths(args.home).db_path).load()
        host, port, allow_remote = effective_server(stored, args.host, args.port, args.allow_remote)
    remote = not is_loopback(host)
    if remote and not allow_remote:
        print(
            f"tflens serve: error: refusing to listen on non-loopback host {host!r}: TFLens "
            "has no authentication. Pass --allow-remote to do it anyway.",
            file=sys.stderr,
        )
        return 2

    from .web import create_app

    if standalone:
        from .webapi import ModelStore, default_model_path

        root = args.dir.resolve()
        store = ModelStore(
            root, args.model or default_model_path(root), args.var_file, dict(args.var)
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
        app = create_app(store=store, allow_remote=allow_remote)
    else:
        app = create_app(args.home, allow_remote=allow_remote)

    def ready(bound: int) -> None:
        # The Host check needs the real port (--port 0).
        app.extensions["tflens"]["port"] = bound
        url = browse_url(host, bound)
        where = ""
        if not standalone:
            ext = app.extensions["tflens"]
            where = f" · data: {ext['paths'].home} · log: {ext['log_dir']}"
        print(
            f"tflens serve: TFLens {__version__} at {url} (Ctrl+C to stop){where}",
            file=sys.stderr,
            flush=True,
        )
        if remote:
            print(remote_warning(host, bound), file=sys.stderr, flush=True)
        if not args.no_browser and desktop_session():
            open_browser(url)

    try:
        with _sigterm_as_interrupt(), contextlib.suppress(KeyboardInterrupt):
            serve_waitress(app, host, port, ready)
    except OSError as exc:  # e.g. the port is already in use
        print(
            f"tflens serve: error: cannot listen on {host}:{port}: {exc.strerror or exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
