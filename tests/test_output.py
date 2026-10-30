"""Determinism, CLI and report."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import FIXTURES

from tflens.cli import main
from tflens.output import dumps, run_analysis
from tflens.report import render_report

ALL = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


@pytest.mark.parametrize("name", ALL)
def test_two_runs_produce_identical_model(name):
    first = dumps(run_analysis(FIXTURES / name).model)
    second = dumps(run_analysis(FIXTURES / name).model)
    assert first == second


def test_cli_determinism_and_relative_paths(tmp_path):
    # a copy at a different absolute path must give byte-identical output
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURES / "multi_module", copy)
    outs = []
    for i, src in enumerate((FIXTURES / "multi_module", copy)):
        out = tmp_path / f"model{i}.json"
        assert main(["--dir", str(src), "--out", str(out)]) == 0
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert str(tmp_path).encode() not in outs[1]


def test_cli_writes_model_and_report(tmp_path, capsys):
    out, rep = tmp_path / "model.json", tmp_path / "report.md"
    assert main(["--dir", str(FIXTURES / "lambda"), "--out", str(out), "--report", str(rep)]) == 0
    model = json.loads(out.read_text())
    assert model["schema_version"] == "1"
    assert set(model) >= {"nodes", "edges", "modules", "variables", "unknowns", "summary"}
    assert [n["id"] for n in model["nodes"]] == sorted(n["id"] for n in model["nodes"])
    text = rep.read_text()
    assert "### `aws_lambda_function.worker`" in text
    assert "tflens:" in capsys.readouterr().err


def test_cli_stdout_and_bad_dir(tmp_path, capsys):
    assert main(["--dir", str(FIXTURES / "sqs")]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["files"] == 1
    assert main(["--dir", str(tmp_path / "missing")]) == 2


def test_report_sections(load):
    m = load("ec2")
    text = render_report(m.data, "ec2")
    section = text.split("### `aws_instance.web`")[1].split("\n### ")[0]
    assert "**What it is:** EC2 instance." in section
    assert "**Where defined:** `main.tf:63-72`" in section
    assert "`aws_s3_bucket.artifacts` — Allow: s3:GetObject, s3:ListBucket" in section
    assert "Network reachability through attached security groups" in section
    assert "0.0.0.0/0 (internet)" in section
    bucket = text.split("### `aws_s3_bucket.artifacts`")[1].split("\n### ")[0]
    assert "**Who can access it:**" in bucket
    assert "`aws_instance.web` — Allow: s3:GetObject" in bucket


def test_parse_error_is_reported(tmp_path):
    (tmp_path / "ok.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "example"\n}\n')
    (tmp_path / "broken.tf").write_text('resource "aws_sqs_queue" "x" {\n  name = \n')
    model = run_analysis(tmp_path).model
    assert [e["file"] for e in model["parse_errors"]] == ["broken.tf"]
    assert "aws_sqs_queue.q" in {n["id"] for n in model["nodes"]}
