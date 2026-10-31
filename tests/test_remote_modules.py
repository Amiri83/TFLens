"""Remote modules materialized by terraform init (.terraform/modules/modules.json)."""

from __future__ import annotations

import json
import shutil

from conftest import FIXTURES

from tflens.output import run_analysis


def _module(m, address):
    def walk(mods):
        for mod in mods:
            if mod["address"] == address:
                return mod
            found = walk(mod["children"])
            if found:
                return found
        return None

    return walk(m.data["modules"])


def test_remote_module_files_are_loaded_but_not_roots(load):
    m = load("remote_module")
    assert m.data["roots"] == ["."]
    assert m.data["files"] == [
        ".terraform/modules/queues/main.tf",
        ".terraform/modules/queues/modules/dlq/main.tf",
        "main.tf",
    ]
    keys = {r["key"]: r for r in m.data["remote_modules"]}
    assert keys["queues"]["version"] == "1.2.0"
    assert keys["queues"]["status"] == "ok"
    assert "missing or has no .tf files" in keys["notify"]["status"]


def test_remote_module_inputs_and_outputs_resolve(load):
    m = load("remote_module")
    queues = _module(m, "module.queues")
    assert queues["analyzed"] is True
    assert queues["dir"] == ".terraform/modules/queues"
    assert queues["remote"]["source"] == "registry.terraform.io/example-org/queues/aws"
    assert queues["inputs"] == {"name": "queue-a"}
    assert queues["outputs"]["queue_name"] == {
        "$ref": "module.queues.aws_sqs_queue.this",
        "attr": "name",
        "value": "queue-a",
    }
    assert m.nodes["module.queues.aws_sqs_queue.this"]["properties"]["name"] == "queue-a"
    assert m.nodes["module.queues"]["properties"]["remote_version"] == "1.2.0"
    # variable default inside the remote module
    retention = m.nodes["module.queues.aws_sqs_queue.this"]["attributes"]
    assert retention["message_retention_seconds"] == 345600

    # a local submodule of the remote module, also listed in modules.json
    dlq = _module(m, "module.queues.module.dlq")
    assert dlq["analyzed"] is True
    assert dlq["inputs"] == {"name": "queue-a-dlq"}
    assert m.nodes["module.queues.module.dlq.aws_sqs_queue.this"]["properties"]["name"] == (
        "queue-a-dlq"
    )

    # remote outputs flow back into the root: access, trigger and evidence
    access = m.edge("ACCESS", "aws_lambda_function.fn", "module.queues.aws_sqs_queue.this")
    assert access["attributes"]["actions"] == ["sqs:ReceiveMessage", "sqs:DeleteMessage"]
    m.edge("TRIGGER", "module.queues.aws_sqs_queue.this", "aws_lambda_function.fn")
    redrive = m.edge(
        "CONFIG",
        "module.queues.aws_sqs_queue.this",
        "module.queues.module.dlq.aws_sqs_queue.this",
    )
    assert (redrive["evidence"][0]["file"], redrive["evidence"][0]["start_line"]) == (
        ".terraform/modules/queues/main.tf",
        23,
    )


def test_unresolvable_remote_modules_stay_unknown_with_reason(load):
    m = load("remote_module")
    (absent,) = m.unknowns("module.absent")
    assert absent["reason"] == (
        "module.absent uses non-local source 'example-org/absent/aws'; "
        "no entry 'absent' in .terraform/modules/modules.json (re-run terraform init)"
    )
    (notify,) = m.unknowns("module.notify")
    assert (
        "'.terraform/modules/notify' listed in .terraform/modules/modules.json"
        in (notify["reason"])
    )
    assert _module(m, "module.notify")["analyzed"] is False
    root_outputs = m.data["modules"][0]["outputs"]
    assert "$unknown" in root_outputs["notify_topic"]
    # an unset variable inside the remote module is unknown, not guessed
    (kms,) = m.unknowns("module.queues.aws_sqs_queue.this")
    assert kms["attribute"] == "kms_master_key_id"
    assert kms["reason"] == "variable var.kms_key has no default and no value in module call"


def test_without_manifest_remote_modules_are_not_fetched(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "remote_module", repo)
    shutil.rmtree(repo / ".terraform")
    model = run_analysis(repo).model
    assert model["remote_modules"] == []
    reasons = [u["reason"] for u in model["unknowns"] if u["subject"] == "module.queues"]
    assert reasons == [
        "module.queues uses non-local source 'example-org/queues/aws'; "
        "remote modules are not fetched (no .terraform/modules/modules.json)"
    ]


def test_bad_manifest_is_a_parse_error(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "remote_module", repo)
    (repo / ".terraform/modules/modules.json").write_text("{not json")
    model = run_analysis(repo).model
    assert [e["file"] for e in model["parse_errors"]] == [".terraform/modules/modules.json"]
    assert "module.queues.aws_sqs_queue.this" not in {n["id"] for n in model["nodes"]}


def test_manifest_dir_outside_repo_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "remote_module", repo)
    manifest = repo / ".terraform/modules/modules.json"
    data = json.loads(manifest.read_text())
    for entry in data["Modules"]:
        if entry["Key"] == "queues":
            entry["Dir"] = "../../elsewhere"
    manifest.write_text(json.dumps(data))
    model = run_analysis(repo).model
    (reason,) = [u["reason"] for u in model["unknowns"] if u["subject"] == "module.queues"]
    assert "outside the repository" in reason
