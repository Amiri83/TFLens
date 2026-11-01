"""Variable / local / data / module-output resolution and UNKNOWN-with-reason."""

from __future__ import annotations


def _var(m, module, name):
    return next(v for v in m.data["variables"] if v["module"] == module and v["name"] == name)


def test_variables_resolved_from_tfvars_default_and_module_inputs(load):
    m = load("multi_module")
    env = _var(m, "(root)", "env")
    assert env["value"] == "dev"
    assert env["source"] == "terraform.tfvars"
    assert _var(m, "(root)", "project")["source"] == "default"
    # module input built from a local
    assert _var(m, "module.worker", "name")["value"] == "example-dev-worker"
    assert _var(m, "module.worker", "name")["source"].startswith("module input")


def test_locals_and_module_outputs(load):
    m = load("multi_module")
    assert m.nodes["aws_sqs_queue.jobs"]["properties"]["name"] == "example-dev-jobs"
    assert (
        m.nodes["module.storage.aws_s3_bucket.this"]["properties"]["bucket"] == "example-dev-data"
    )
    tags = m.nodes["module.storage.aws_s3_bucket.this"]["attributes"]["tags"]
    assert tags == {"Environment": "dev", "Project": "example"}

    # module.storage.bucket_arn flows into module.worker's policy document
    access = m.edge(
        "ACCESS", "module.worker.aws_lambda_function.this", "module.storage.aws_s3_bucket.this"
    )
    assert access["attributes"]["actions"] == ["s3:GetObject", "s3:PutObject"]
    assert access["evidence"][-1]["file"] == "modules/worker/main.tf"
    assert (access["evidence"][-1]["start_line"], access["evidence"][-1]["end_line"]) == (24, 27)

    # ARN built from data.aws_caller_identity (unknown account) + local -> matched by pattern
    m.edge("ACCESS", "module.worker.aws_iam_role.this", "aws_sqs_queue.jobs")

    root = m.data["modules"][0]
    worker = next(c for c in root["children"] if c["address"] == "module.worker")
    resolved = {
        "$ref": "module.worker.aws_lambda_function.this",
        "attr": "function_name",
        "value": "example-dev-worker",
    }
    assert worker["outputs"]["function_name"] == resolved
    assert root["outputs"]["worker_function"] == resolved
    # arn is computed by AWS: kept as a reference, not guessed
    assert worker["inputs"]["bucket_arn"] == {
        "$ref": "module.storage.aws_s3_bucket.this",
        "attr": "arn",
    }
    assert m.nodes["module.worker"]["kind"] == "module"
    assert m.nodes["module.worker.aws_lambda_function.this"]["parent"] == "module.worker"


def test_unknown_with_reason(load):
    m = load("multi_module")
    (unset,) = m.unknowns("aws_sns_topic_subscription.alerts_email")
    assert unset["attribute"] == "endpoint"
    assert (
        unset["reason"]
        == "variable var.alert_email has no default and no value in tfvars/--var-file"
    )
    assert unset["location"] == {"file": "main.tf", "start_line": 55, "end_line": 55}
    # no edge is invented for the unknown endpoint
    assert m.edges("TRIGGER", "aws_sns_topic.alerts") == []

    # for_each over a known set: no unknown, the node shows the first key's values
    assert m.unknowns("aws_sqs_queue.per_tenant") == []
    per_tenant = m.nodes["aws_sqs_queue.per_tenant"]
    assert per_tenant["properties"]["instance_shown"] == "values shown for each.key = a"

    (remote,) = m.unknowns("module.network")
    assert "non-local source 'terraform-aws-modules/vpc/aws'" in remote["reason"]
    network = next(c for c in m.data["modules"][0]["children"] if c["address"] == "module.network")
    assert network["analyzed"] is False

    alert = _var(m, "(root)", "alert_email")
    assert alert["value"] == {
        "$unknown": "variable var.alert_email has no default and no value in tfvars/--var-file"
    }


def test_cli_var_overrides_unknown(tmp_path):
    from conftest import FIXTURES

    from tflens.output import run_analysis

    model = run_analysis(
        FIXTURES / "multi_module", cli_vars={"alert_email": "ops@example.com"}
    ).model
    edges = [
        e
        for e in model["edges"]
        if e["kind"] == "TRIGGER" and e["source"] == "aws_sns_topic.alerts"
    ]
    assert [e["target"] for e in edges] == ["external:email_endpoint:ops@example.com"]
