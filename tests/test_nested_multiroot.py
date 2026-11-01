"""Real-repo robustness on a nested multi-root layout (tests/fixtures/nested_multiroot).

Three stacks at different depths (platform/network, platform/services/api,
platform/services/worker), environments next to each (envs/<env>.tfvars +
envs/backend-<env>.hcl, vars/<env>.tfvars), a module used by two stacks (modules/queue,
with examples/ and tests/ that are not deployed), a tagging module (modules/labels),
terraform_remote_state links between stacks (and one to a state outside the repository),
tags from default_tags and merge() locals, and a README / docs page that are wrong on
purpose (they name resources the code does not have and skip some it has).
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

import pytest
from conftest import FIXTURES

from tflens.detect import backend_files
from tflens.envs import select_stacks, stacks
from tflens.loader import excluded_dir, load_repo
from tflens.output import run_analysis
from tflens.overview import build_overview, render_overview_markdown
from tflens.report import render_report
from tflens.webapi import Api, ModelStore, environments_view

FIXTURE = FIXTURES / "nested_multiroot"
NETWORK, API, WORKER = "platform/network", "platform/services/api", "platform/services/worker"
# Measured on this fixture with the analysis as it was before this change (same fixture,
# git disabled): every stack analyzed with no environment applied -> 35 unknown values,
# and the per-environment analyses -> 13 in total. With the default environment applied,
# only one of the three stacks was shown at all.
BASELINE_ALL_STACKS_UNKNOWNS = 35
BASELINE_PER_ENV_UNKNOWNS = 13


@cache
def _model(**kwargs: Any) -> dict[str, Any]:
    return run_analysis(FIXTURE, git=False, **kwargs).model


def model() -> dict[str, Any]:
    return _model(auto_env=True)


def nodes(m: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in m["nodes"]}


# ------------------------------------------------------------------ 1. crawler


def test_examples_tests_and_fixtures_are_excluded_but_test_environments_are_not():
    assert excluded_dir("examples", "modules/queue", ["examples", "tests"])
    assert excluded_dir("tests", "modules/queue", [])
    assert excluded_dir("fixtures", ".", [])
    assert excluded_dir("test", "modules/queue", ["test"])
    # an environment called test: under envs/, or next to other environments
    assert not excluded_dir("test", "envs", ["dev", "test"])
    assert not excluded_dir("test", ".", ["dev", "prod", "test"])
    assert not excluded_dir("modules", ".", [])

    repo = load_repo(FIXTURE)
    excluded = {s.path for s in repo.skipped if s.kind == "excluded"}
    assert excluded == {"modules/queue/examples", "modules/queue/tests"}
    assert not any("/examples/" in f or "/tests/" in f for f in repo.files)


def test_tool_directories_are_skipped(tmp_path):
    for d in ("dist", "node_modules", ".terraform", ".venv"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "main.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "x"\n}\n')
    (tmp_path / "main.tf").write_text('resource "aws_sns_topic" "t" {\n  name = "t"\n}\n')
    repo = load_repo(tmp_path)
    assert list(repo.modules) == ["."]
    assert {s.path for s in repo.skipped} >= {"dist", "node_modules", ".terraform", ".venv"}


def test_backend_configurations_are_found_by_content_anywhere(tmp_path):
    (tmp_path / "stack" / "deep" / "state").mkdir(parents=True)
    (tmp_path / "stack" / "main.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
    (tmp_path / "stack" / "deep" / "state" / "anything.hcl").write_text(
        'bucket = "example-state"\nkey    = "stack/terraform.tfstate"\n'
    )
    (tmp_path / "stack" / "other.hcl").write_text('name = "not a backend"\n')
    repo = load_repo(tmp_path)
    assert backend_files(repo) == ["stack/deep/state/anything.hcl"]


def test_module_examples_and_tests_are_neither_roots_nor_resources():
    m = model()
    assert m["roots"] == [NETWORK, API, WORKER]
    assert not any(
        n["location"]
        and n["location"]["file"].startswith(("modules/queue/examples", "modules/queue/tests"))
        for n in m["nodes"]
    )
    assert "aws_s3_bucket" not in {n["type"] for n in m["nodes"]}
    dirs = {d["dir"]: d["role"] for d in m["structure"]["module_dirs"]}
    assert dirs["modules/queue"] == "child" and dirs["modules/labels"] == "child"
    assert not any(d.startswith("modules/queue/") for d in dirs)


# ------------------------------------------------------------------ 2. multi-root


def test_every_stack_shows_up_with_its_own_root_and_environment():
    m = model()
    by_id = {s["id"]: s for s in m["stacks"]}
    assert list(by_id) == [NETWORK, API, WORKER]
    for sid, s in by_id.items():
        assert s["roots"] == [sid] and s["analyzed"] == [sid]
        assert s["environments"] == [f"{sid}:dev", f"{sid}:prod"]
        assert (s["selected"], s["default"]) == (f"{sid}:dev", True)
    # envs found at depth and bound to the nearest root, backend config included
    envs = {(e["root"], e["name"]): e for e in m["structure"]["environments"]}
    assert envs[(NETWORK, "prod")]["var_files"] == [f"{NETWORK}/envs/prod.tfvars"]
    assert envs[(NETWORK, "prod")]["backend_hcl"] == [f"{NETWORK}/envs/backend-prod.hcl"]
    assert envs[(WORKER, "dev")]["var_files"] == [f"{WORKER}/vars/dev.tfvars"]
    # each stack's own tfvars are applied to it (and only to it)
    n = nodes(m)
    assert n[f"{NETWORK}::aws_vpc.main"]["attributes"]["cidr_block"] == "10.0.0.0/16"
    assert n[f"{API}::aws_lambda_function.api"]["attributes"]["function_name"] == "dev-api"
    assert n[f"{WORKER}::aws_instance.worker"]["properties"].get("created", 1) == 1


def test_environment_selector_is_per_stack():
    m = run_analysis(FIXTURE, git=False, stack_envs={NETWORK: f"{NETWORK}:prod"}).model
    sel = {s["id"]: (s["selected"], s["default"]) for s in m["stacks"]}
    assert sel == {
        NETWORK: (f"{NETWORK}:prod", False),
        API: (f"{API}:dev", True),
        WORKER: (f"{WORKER}:dev", True),
    }
    n = nodes(m)
    assert n[f"{NETWORK}::aws_vpc.main"]["attributes"]["cidr_block"] == "10.1.0.0/16"
    assert n[f"{NETWORK}::aws_subnet.private"]["properties"]["created"] == 3
    assert n[f"{API}::aws_lambda_function.api"]["attributes"]["function_name"] == "dev-api"
    # no environment for one stack: its variables keep their defaults, the others not
    m = run_analysis(FIXTURE, git=False, stack_envs={API: ""}).model
    sel = {s["id"]: s["selected"] for s in m["stacks"]}
    assert sel == {NETWORK: f"{NETWORK}:dev", API: None, WORKER: f"{WORKER}:dev"}
    with pytest.raises(ValueError, match="unknown environment"):
        run_analysis(FIXTURE, git=False, stack_envs={API: f"{NETWORK}:dev"})


def test_environment_directories_are_one_stack_with_a_root_per_environment():
    envs = [
        {"name": n, "root": f"envs/{n}", "kinds": ["dir"], "roots": [f"envs/{n}"], "var_files": []}
        for n in ("dev", "prod")
    ]
    found = stacks(envs, ["envs/dev", "envs/prod", "global"])
    assert [(s["id"], s["roots"], s["environments"]) for s in found] == [
        ("envs/*", ["envs/dev", "envs/prod"], ["dev", "prod"]),
        ("global", ["global"], []),
    ]
    picked = select_stacks(found, envs, {"envs/*": "prod"})
    assert picked["exclude_roots"] == {"envs/dev"}
    assert [s["selected"] for s in picked["stacks"]] == ["prod", None]


def test_web_api_switches_one_stack_and_keeps_the_others(tmp_path):
    store = ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True)
    view = environments_view(store.model(), store)
    assert view["selected"] == [f"{s}:dev" for s in (NETWORK, API, WORKER)]
    assert [s["id"] for s in view["stacks"]] == [NETWORK, API, WORKER]
    api = Api(store)
    r = api.handle("POST", f"/api/env?stack={WORKER}&name={WORKER}:prod")
    assert r.status == 200
    body = json.loads(r.body)
    assert body["selected"] == [f"{NETWORK}:dev", f"{API}:dev", f"{WORKER}:prod"]
    r = api.handle("POST", f"/api/env?stack={API}&name={API}:prod")
    body = json.loads(r.body)
    assert body["selected"] == [f"{NETWORK}:dev", f"{API}:prod", f"{WORKER}:prod"]
    assert api.handle("POST", f"/api/env?stack={API}&name=nope").status == 404
    assert json.loads(api.handle("GET", "/api/environments").body)["selected"][1] == f"{API}:prod"


# ------------------------------------------------------------------ 4. evaluator


def test_count_indexing_element_and_join_resolve():
    n = nodes(model())
    subnet = n[f"{NETWORK}::aws_subnet.private"]
    assert subnet["properties"]["created"] == 2
    assert subnet["properties"]["instance_shown"] == "values shown for count.index = 0"
    assert subnet["attributes"]["cidr_block"] == "10.0.1.0/24"  # var.private_cidrs[count.index]
    assert subnet["attributes"]["availability_zone"] == "us-east-1a"  # element(var.azs, ...)
    assert subnet["attributes"]["tags"]["Name"] == "example-dev-private-0"
    assert n[f"{NETWORK}::aws_security_group.app"]["attributes"]["name"] == "example-dev-app"


def test_join_and_element_over_resource_references(tmp_path):
    (tmp_path / "main.tf").write_text(
        'resource "aws_subnet" "s" {\n  count  = 2\n  vpc_id = "vpc-1"\n}\n'
        'resource "aws_instance" "i" {\n  count     = 2\n'
        "  subnet_id = element(aws_subnet.s[*].id, count.index)\n"
        '  user_data = join(",", aws_subnet.s[*].id)\n}\n'
    )
    m = run_analysis(tmp_path, git=False).model
    assert m["unknowns"] == []
    edges = {(e["source"], e["target"], e["label"]) for e in m["edges"] if e["kind"] == "CONFIG"}
    assert ("aws_instance.i", "aws_subnet.s", "subnet_id") in edges
    assert ("aws_instance.i", "aws_subnet.s", "user_data") in edges


def test_remote_state_is_an_external_link_between_stacks_never_unknown():
    m = model()
    links = {
        (lk["from_stack"], lk["data_source"].rsplit(".", 1)[-1]): lk for lk in m["stack_links"]
    }
    net = links[(API, "network")]
    assert (net["to_stack"], net["env"], net["status"]) == (NETWORK, "dev", "linked")
    assert links[(WORKER, "network")]["to_stack"] == NETWORK
    billing = links[(WORKER, "billing")]
    assert (billing["to_stack"], billing["status"]) == (None, "external")
    assert billing["config"]["key"] == "billing/terraform.tfstate"
    assert not [u for u in m["unknowns"] if "remote_state" in u["subject"] + u["reason"]]
    edges = {(e["source"], e["target"]) for e in m["edges"]}
    # the data source links to the stack it reads, and the outputs it reads resolve to
    # that stack's resources
    assert (
        f"{API}::data.terraform_remote_state.network",
        f"external:terraform_stack:{NETWORK}:dev",
    ) in edges
    assert (f"{API}::aws_lambda_function.api", f"{NETWORK}::aws_subnet.private") in edges
    assert (f"{WORKER}::aws_instance.worker", f"{NETWORK}::aws_security_group.app") in edges
    assert (
        f"{API}::aws_lambda_function.api",
        f"{API}::data.terraform_remote_state.network",
    ) in edges
    assert (
        f"{WORKER}::data.terraform_remote_state.billing",
        "external:terraform_state:s3:example-tfstate-shared/billing/terraform.tfstate",
    ) in edges


def test_remote_outputs_of_a_stack_not_analyzed_are_external_nodes(tmp_path):
    # one stack alone (its environment's directory): the network stack is not analyzed
    m = run_analysis(FIXTURE, git=False, env=f"{API}:dev").model
    assert m["roots"] == [API] and m["unknowns"] == []
    lam = "aws_lambda_function.api"
    targets = {e["target"] for e in m["edges"] if e["source"] == lam and e["kind"] == "NETWORK"}
    assert targets == {
        f"external:remote_output:external:terraform_stack:{NETWORK}:dev#app_security_group_id"
    }


# ------------------------------------------------------------------ 5. tags


def test_tags_per_resource_from_default_tags_merge_locals_and_tag_modules():
    n = nodes(model())
    vpc = n[f"{NETWORK}::aws_vpc.main"]["properties"]
    assert vpc["tags"] == {
        "Name": "example-dev",
        "environment": "dev",
        "managed_by": "terraform",
        "owner": "platform-team",
    }
    assert vpc["tag_sources"]["Name"] == "resource"
    assert vpc["tag_sources"]["owner"] == "default_tags"
    assert vpc["tag_provider"] == "aws"
    assert vpc["tag_locations"]["default_tags"]["file"] == f"{NETWORK}/providers.tf"
    lam = n[f"{API}::aws_lambda_function.api"]["properties"]
    assert lam["tags"] == {
        "component": "api",
        "environment": "dev",
        "managed_by": "terraform",
        "tier": "backend",
    }
    # a module's resources: tags passed down through module inputs, plus default_tags
    q = n[f"{API}::module.jobs.aws_sqs_queue.this"]["properties"]
    assert q["tags"]["tier"] == "backend" and q["tag_sources"]["tier"] == "resource"
    # a tag read from a state outside the repository: shown as the reference
    worker = n[f"{WORKER}::aws_instance.worker"]["properties"]
    assert worker["tags"]["cost_center"].startswith("${")
    assert "terraform_remote_state.billing.outputs.cost_center" in worker["tags"]["cost_center"]


def test_tagging_is_detected_generically_per_stack():
    roots = {r["dir"]: r["tagging"] for r in model()["structure"]["roots"]}
    net = roots[NETWORK]
    assert [d["keys"] for d in net["default_tags"]] == [["environment", "managed_by", "owner"]]
    assert [m["name"] for m in net["merge_locals"]] == ["local.common_tags"]
    api = roots[API]
    assert [(t["address"], t["output"], t["dir"]) for t in api["tag_modules"]] == [
        ("module.labels", "tags", "modules/labels")
    ]
    assert "local.tags" in [m["name"] for m in api["merge_locals"]]
    assert roots[WORKER]["merge_locals"] == [] and len(roots[WORKER]["default_tags"]) == 1


# ------------------------------------------------------------------ 6. docs vs code


def test_docs_vs_code_diff():
    dd = model()["docs_diff"]
    assert dd["files"] == ["README.md", "docs/architecture.md", "modules/queue/README.md"]
    missing = {m["ref"]: m["locations"][0] for m in dd["documented_missing"]}
    assert set(missing) == {
        "aws_nat_gateway.main",
        "aws_dynamodb_table.sessions",
        "aws_sns_topic.alerts",
    }
    assert missing["aws_nat_gateway.main"]["file"] == "README.md"
    assert missing["aws_sns_topic.alerts"]["file"] == "docs/architecture.md"
    assert [u["ref"] for u in dd["undocumented"]] == [
        "aws_cloudwatch_log_group.api",
        "aws_instance.worker",
        "aws_security_group.app",
    ]
    documented = {d["ref"] for d in dd["documented"]}
    # by address, by type (`aws_sqs_queue` covers the module's queues), data sources
    assert {"aws_vpc.main", "aws_sqs_queue", "data.terraform_remote_state.network"} <= documented
    # the example code of modules/queue is not part of the code side
    assert "aws_s3_bucket.example" not in {u["ref"] for u in dd["undocumented"]}


def test_docs_diff_without_markdown(tmp_path):
    (tmp_path / "main.tf").write_text('resource "aws_sns_topic" "t" {\n  name = "t"\n}\n')
    dd = run_analysis(tmp_path, git=False).model["docs_diff"]
    assert dd["status"] == "no-docs" and dd["undocumented"] == [] and dd["documented_missing"] == []


# ------------------------------------------------------------------ 7. unknowns drop


def test_unknown_count_drops_sharply():
    m = model()
    assert len(m["unknowns"]) == 0 < BASELINE_ALL_STACKS_UNKNOWNS
    assert m["environment_analysis"]["total"]["unknown_count"] == 0 < BASELINE_PER_ENV_UNKNOWNS
    # with no environment at all the variables are unset (that stays unknown, honestly),
    # but a remote state output is never one of the unknowns
    bare = _model()
    assert bare["roots"] == [NETWORK, API, WORKER]
    assert not [u for u in bare["unknowns"] if "only known after apply" in u["reason"]]
    assert len(bare["unknowns"]) < BASELINE_ALL_STACKS_UNKNOWNS


# ------------------------------------------------------------------ UI / report


def test_overview_and_report_carry_stacks_tags_links_and_docs_diff():
    m = model()
    ov = build_overview(m)
    sections = {r["dir"]: {s["title"]: s["lines"] for s in r["sections"]} for r in ov["roots"]}
    assert set(sections) == {NETWORK, API, WORKER}
    api = sections[API]
    assert "Tags" in api and "Stack" in api
    assert api["Stack"][0]["text"].startswith(f"stack {API} (tfvars per environment)")
    assert any("tagging module module.labels" in ln["text"] for ln in api["Tags"])
    tag_lines = {ln["node"]: ln["text"] for ln in api["Tags"] if ln.get("node")}
    assert tag_lines[f"{API}::aws_lambda_function.api"] == (
        "aws_lambda_function.api: component=api, environment=dev, managed_by=terraform, "
        "tier=backend"
    )
    # a resource with no tags of its own: everything from the provider's default_tags
    assert tag_lines[f"{API}::aws_iam_role.api"] == (
        "aws_iam_role.api: component=api (default), environment=dev (default), "
        "managed_by=terraform (default)"
    )
    links = [ln["text"] for ln in api["Remote state (links between stacks)"]]
    assert links == [f"reads stack {NETWORK} (dev) via data.terraform_remote_state.network"]
    assert any(
        ln["text"].startswith("read by stack")
        for ln in sections[NETWORK]["Remote state (links between stacks)"]
    )
    diff = [ln["text"] for ln in ov["docs_diff"]]
    assert "Documented but missing from the code:" in diff and "aws_nat_gateway.main" in diff
    assert "In the code but not documented:" in diff and "aws_instance.worker" in diff
    md = render_overview_markdown(ov)
    assert "## Docs vs code" in md and "### Tags" in md
    report = render_report(m)
    assert "## Docs vs code" in report and "## Links between stacks" in report
    assert "**Tags:**" in report and "`owner` = platform-team (provider aws default_tags" in report


def test_front_end_renders_the_new_sections():
    js = (FIXTURES.parent.parent / "src/tflens/static/app.js").read_text()
    for needed in (
        "stacksTable(",
        "selectStackEnv(",
        "api/env?stack=",
        "nodeExtras(",
        "ov.docs_diff",
    ):
        assert needed in js
