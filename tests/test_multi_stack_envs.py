"""Per-stack environment detection, one inventory column per environment, no double counting.

Fixture ``multi_stack_envs``: three stacks (stacks/api, stacks/worker, stacks/edge), each
with dev / uat / prod tfvars nested below its own directory at different depths
(``envs/``, ``config/envs/``), backend-<env>.hcl files, a wrapper module calling another
module, a reusable module nothing calls, and examples/ and tests/ trees with their own
Lambda functions. Each environment deploys 4 Lambda functions (api: 2, worker: 1, edge: 1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tflens.envs import env_count_lines, inventory
from tflens.output import run_analysis
from tflens.overview import build_overview, render_overview_markdown
from tflens.webapi import environments_view

FIXTURE = Path(__file__).parent / "fixtures" / "multi_stack_envs"
STACKS = ["stacks/api", "stacks/edge", "stacks/worker"]


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False).model


def _column(model, name):
    return next(c for c in model["environment_analysis"]["columns"] if c["id"] == name)


def test_envs_detected_per_stack_at_any_depth(model):
    by_stack = {s["id"]: s for s in model["stacks"]}
    for sid in STACKS:
        names = sorted(e.rsplit(":", 1)[1] for e in by_stack[sid]["environments"])
        assert names == ["dev", "prod", "uat"], sid
    # worker's tfvars are two levels below the stack: bound to it, named by the file stem
    worker = {t["file"]: t for t in by_stack["stacks/worker"]["env_files"]}
    t = worker["stacks/worker/config/envs/uat.tfvars"]
    assert t["accepted"] and t["env"] == "uat" and t["stack"] == "stacks/worker"
    assert "file stem" in t["reason"]
    # backend-<env>.hcl below the stack binds to it too
    edge = {t["file"]: t for t in by_stack["stacks/edge"]["env_files"]}
    assert edge["stacks/edge/deploy/backend/backend-prod.hcl"]["accepted"]
    assert edge["stacks/edge/deploy/backend/backend-prod.hcl"]["env"] == "prod"


def test_one_column_per_environment_four_lambdas_each(model):
    cols = model["environment_analysis"]["columns"]
    assert [c["id"] for c in cols] == ["dev", "uat", "prod"]  # promotion order
    for name in ("dev", "uat", "prod"):
        c = _column(model, name)
        assert c["counts"]["aws_lambda_function"] == 4, name
        assert c["inventory"]["lambda"]["count"] == 4, name
        assert c["stacks"] == STACKS and c["missing"] == []
    # never 14 (or 12, 13…): no sum across environments, nothing from examples / tests /
    # unused modules
    assert all(c["counts"]["aws_lambda_function"] != 14 for c in cols)


def test_default_view_never_sums_across_environments(model):
    view = environments_view(model)
    assert [c["id"] for c in view["columns"]] == ["dev", "uat", "prod"]
    assert all(c["id"].lower() != "total" for c in view["columns"])
    lam = next(r for r in view["inventory"] if r["key"] == "lambda")
    assert not lam["differs"]
    texts = [ln["text"] for ln in env_count_lines(model)]
    assert not any(t.lower().startswith("total") for t in texts)
    assert not any("12 Lambda" in t or "14 Lambda" in t for t in texts)
    md = render_overview_markdown(build_overview(model))
    assert "14 Lambda" not in md and "12 Lambda" not in md


def test_module_call_and_its_resources_are_one_item(model):
    items = _column(model, "dev")["inventory"]["lambda"]["items"]
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids)), "an item listed twice"
    assert not any(".module." in i and i.endswith(("module.fn", "module.worker")) for i in ids)
    assert sum(it["created"] for it in items) == 4
    api = next(it for it in items if it["stack"] == "stacks/api")
    assert api["created"] == 2  # for_each over 2 handlers, through 2 nested module calls
    assert [v["id"].rsplit("::", 1)[-1] for v in api["via"]] == [
        "module.handlers",
        "module.handlers.module.fn",
    ]


def test_inventory_counts_a_module_resource_once():
    """Regression: a module call node and the resource inside it (and the same resource
    appearing twice in the node list) must give one item of one instance."""
    loc = {"file": "modules/lambda/main.tf", "start_line": 15, "end_line": 21}
    call = {
        "id": "module.fn",
        "kind": "module",
        "type": "module",
        "name": "fn",
        "parent": None,
        "location": {"file": "main.tf", "start_line": 1, "end_line": 4},
        "attributes": {},
    }
    res = {
        "id": "module.fn.aws_lambda_function.this",
        "kind": "resource",
        "type": "aws_lambda_function",
        "name": "this",
        "parent": "module.fn",
        "location": loc,
        "attributes": {},
    }
    inv = inventory([call, res, dict(res)])
    assert inv["lambda"]["count"] == 1
    assert [it["id"] for it in inv["lambda"]["items"]] == ["module.fn.aws_lambda_function.this"]


def test_excluded_and_unused_trees_never_count(model):
    for c in model["environment_analysis"]["columns"]:
        for it in c["inventory"]["lambda"]["items"]:
            f = it["location"]["file"]
            assert "/examples/" not in f and "/tests/" not in f, f
            assert not f.startswith("modules/legacy_fn"), f
    legacy = next(s for s in model["stacks"] if s["id"] == "modules/legacy_fn")
    assert legacy["library"]
    assert "modules/legacy_fn" in model["environment_analysis"]["library_roots"]


def test_breakdown_items_name_stack_location_and_env(model):
    for name in ("dev", "uat", "prod"):
        for it in _column(model, name)["inventory"]["lambda"]["items"]:
            assert it["stack"] in STACKS
            assert it["env"] == name
            assert it["location"]["file"].endswith(".tf") and it["location"]["start_line"] > 0
    edge = next(
        it
        for it in _column(model, "prod")["inventory"]["lambda"]["items"]
        if it["stack"] == "stacks/edge"
    )
    assert edge["location"]["file"] == "stacks/edge/main.tf"


def test_no_environment_says_why(tmp_path):
    (tmp_path / "main.tf").write_text(
        'variable "env" {\n  type    = string\n  default = "dev"\n}\n'
    )
    m = run_analysis(tmp_path, git=False).model
    assert m["structure"]["environments"] == []
    s = m["stacks"][0]
    assert s["environments"] == []
    assert s["no_env_reason"].startswith("no candidates")


def test_rejected_candidate_reason_is_listed(tmp_path):
    (tmp_path / "main.tf").write_text('variable "x" {\n  default = 1\n}\n')
    (tmp_path / "terraform.tfvars").write_text("x = 2\n")
    m = run_analysis(tmp_path, git=False).model
    s = m["stacks"][0]
    assert s["environments"] == []
    files = {t["file"]: t for t in s["env_files"]}
    assert not files["terraform.tfvars"]["accepted"]
    assert "loaded automatically" in files["terraform.tfvars"]["reason"]
    assert s["no_env_reason"].startswith("candidates rejected")
