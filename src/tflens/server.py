"""``tflens serve``: local web UI on the standard library's http.server.

A thin wrapper over :class:`tflens.webapi.Api`. Binds to loopback unless
``--allow-remote`` is given; makes no outbound network calls (the page is served
with a ``default-src 'self'`` content security policy and loads only vendored
assets).
"""

from __future__ import annotations

import ipaddress
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .webapi import Api, Response

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_of(header: str) -> str:
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def make_handler(api: Api, allow_remote: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tflens"
        sys_version = ""

        def _send(self, resp: Response, head: bool = False) -> None:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(resp.body)

        def _guard(self, method: str) -> Response | None:
            # DNS-rebinding guard: a loopback server only answers loopback Host names
            if not allow_remote and not is_loopback(_host_of(self.headers.get("Host", ""))):
                return Response(403, "text/plain; charset=utf-8", b"forbidden host\n")
            # cross-site POSTs cannot set custom headers without a (refused) preflight
            if method == "POST" and self.headers.get("X-TFLens") != "1":
                return Response(403, "text/plain; charset=utf-8", b"missing X-TFLens header\n")
            return None

        def _dispatch(self, method: str) -> None:
            denied = self._guard(method)
            self._send(denied or api.handle(method, self.path), head=method == "HEAD")

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            self._dispatch("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("HEAD")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 1 << 16))
            self._dispatch("POST")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            sys.stderr.write(f"tflens serve: {self.address_string()} {format % args}\n")

    return Handler


def make_server(api: Api, host: str, port: int, allow_remote: bool = False) -> ThreadingHTTPServer:
    if not allow_remote and not is_loopback(host):
        raise ValueError(
            f"refusing to bind to non-loopback host {host!r} without --allow-remote "
            "(the UI exposes repository source code)"
        )
    server_cls = ThreadingHTTPServer
    if ":" in host:
        import socket

        class _V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_cls = _V6
    server = server_cls((host, port), make_handler(api, allow_remote))
    server.daemon_threads = True
    return server


def url_of(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in str(host) else str(host)
    return f"http://{shown}:{port}/"


def serve_forever(server: ThreadingHTTPServer, open_browser: bool) -> None:
    url = url_of(server)
    print(f"tflens serve: {url} (Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
