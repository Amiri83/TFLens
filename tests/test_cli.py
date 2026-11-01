"""Top-level routing: bare ``tflens`` / ``python -m tflens`` is ``tflens serve``."""

from __future__ import annotations

import pytest
from conftest import FIXTURES

from tflens import cli
from tflens.cli import main
from tflens.server import is_loopback
from tflens.settings import DEFAULT_HOST, DEFAULT_PORT


@pytest.fixture
def serve_calls(monkeypatch):
    calls: list[list[str]] = []

    def fake(argv):
        calls.append(list(argv))
        return 42

    monkeypatch.setattr("tflens.cli.serve_main", fake)
    return calls


def test_no_arguments_routes_to_serve(serve_calls, monkeypatch):
    assert main([]) == 42  # python -m tflens
    monkeypatch.setattr("sys.argv", ["tflens"])
    assert main(None) == 42  # console script, no arguments
    assert main(["serve"]) == 42
    assert serve_calls == [[], [], []]


def test_serve_options_pass_through(serve_calls):
    assert main(["serve", "--port", "0", "--no-browser"]) == 42
    assert serve_calls == [["--port", "0", "--no-browser"]]


def test_analyze_and_other_arguments_do_not_serve(serve_calls, tmp_path, capsys):
    out = tmp_path / "model.json"
    assert main(["--dir", str(FIXTURES / "s3"), "--out", str(out)]) == 0
    assert out.is_file()
    # --help, --version and unknown arguments still go to the analyzer parser.
    for argv in (["--help"], ["--version"], ["bogus"], ["--port", "0"]):
        with pytest.raises(SystemExit):
            main(argv)
    assert "--dir" in capsys.readouterr().out  # analyzer help, not serve's
    assert serve_calls == []


def test_serve_help_is_still_serve_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--help"])
    assert exc.value.code == 0
    assert "--allow-remote" in capsys.readouterr().out


def test_no_arguments_serves_with_documented_defaults(monkeypatch, capsys):
    """``main([])`` runs the real serve_main; waitress is stubbed, nothing is bound."""
    assert DEFAULT_HOST == "127.0.0.1" and is_loopback(DEFAULT_HOST)
    calls = {}

    def fake(app, host, port, ready):
        calls.update(app=app, host=host, port=port)
        ready(port)

    monkeypatch.setattr("tflens.cli.serve_waitress", fake)
    monkeypatch.setattr("tflens.cli.open_browser", lambda url: calls.setdefault("opened", url))
    monkeypatch.setattr("tflens.cli.desktop_session", lambda: True)
    assert cli.main([]) == 0
    assert (calls["host"], calls["port"]) == (DEFAULT_HOST, DEFAULT_PORT)
    assert calls["app"].extensions["tflens"]["managed"] is True  # managed GUI, as `serve`
    assert calls["opened"] == f"http://127.0.0.1:{DEFAULT_PORT}/"
    assert f"http://127.0.0.1:{DEFAULT_PORT}/" in capsys.readouterr().err
