"""Shared bits of ``tflens serve``: the loopback check and the content security policy.

The server itself is waitress over the Flask app (:mod:`tflens.web`, started by
:mod:`tflens.cli`). It binds to loopback unless ``--allow-remote`` is given and makes
no outbound network calls: pages are served with a ``default-src 'self'`` content
security policy and load only vendored assets.
"""

from __future__ import annotations

import ipaddress

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
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
