"""Unknowns, the resolver and evidence, on the ``resolver_chain`` fixture.

Three stacks (``stacks/app`` reads ``stacks/network`` and ``stacks/shared`` through
``terraform_remote_state``), real environments dev and prod, and docs naming environments
and files that do not exist. Measured on this fixture, before this work -> after it:
``stacks/app`` dev 19 -> 3, prod 17 -> 3; ``stacks/network`` dev 1 -> 0, prod 1 -> 0; with no
environment applied 29 -> 14. What stays unknown is what the repository does not hold.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from conftest import FIXTURES

from tflens.ciscan import ci_var_inputs, env_commands
from tflens.envs import unknown_groups
from tflens.loader import load_repo
from tflens.model import unknown_category
from tflens.output import run_analysis
from tflens.webapi import STATIC_DIR, Api, ModelStore

FIXTURE = FIXTURES / "resolver_chain"
TEMPLATES = STATIC_DIR.parent / "templates"
APP = "stacks/app"
NETWORK = "stacks/network"
UI_DATA = (
    "summary",
    "environments",
    "graph",
    "nodes",
    "edges",
    "overview",
    "flow",
    "shapes",
    "stack-cards",
    "env-matrix",
)


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False).model


@pytest.fixture(scope="module")
def rows(model):
    return {r["id"]: r for r in model["environment_analysis"]["environments"]}


@pytest.fixture()
def api(tmp_path):
    return Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True))


def _get(api, path):
    resp = api.handle("GET", path)
    assert resp.status == 200, path
    return resp.body.decode()


def _shown(api, env):
    """The analysis with ``stacks/app`` in ``env``: its nodes by id, without the stack prefix."""
    resp = api.handle("POST", f"/api/env?stack={APP}&name={APP}:{env}", b"")
    assert resp.status == 200
    return {n["id"].split("::")[-1]: n for n in api.store.model()["nodes"]}


# -- the resolver ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_only_what_the_repository_does_not_hold_stays_unknown(rows, env):
    found = {
        (u["subject"].split("::")[-1], u["attribute"], u["category"]): (
            u["location"]["file"],
            u["location"]["start_line"],
            (u.get("defined_at") or {}).get("start_line"),
        )
        for u in rows[f"{APP}:{env}"]["unknowns"]
    }
    main = f"{APP}/main.tf"
    assert found == {
        ("aws_lambda_function.api", "image_uri", "set by CI at run time"): (main, 143, 23),
        ("aws_sns_topic_subscription.email", "endpoint", "missing variable"): (main, 149, 27),
        ("module.registry", "source", "module not analyzed"): (main, 73, None),
    }
    assert rows[f"{NETWORK}:{env}"]["unknowns"] == []


@pytest.mark.parametrize(
    ("env", "tier", "vpc", "topic", "instance_type", "instances"),
    [
        ("dev", "standard", "EXAMPLE-DEV-VPC", "example-dev-chat", "t3.small", 2),
        ("prod", "premium", "EXAMPLE-PROD-VPC", "example-prod-pager", "m5.large", 1),
    ],
)
def test_the_chain_resolves_per_environment(api, env, tier, vpc, topic, instance_type, instances):
    nodes = _shown(api, env)
    main = nodes["aws_sqs_queue.main"]["attributes"]
    # module output of a module with count -> local -> remote state of the shared stack -> tfvars
    assert main["name"] == f"example-{env}-orders-main"
    assert main["tags"]["Tier"] == tier  # tfvars, else the optional() default of the type
    assert main["tags"]["Vpc"] == vpc  # remote state output of the same environment
    assert main["tags"]["Workdir"] == APP  # path.cwd: the root module's directory
    assert main["tags"]["Space"] == "default"  # CI runs terraform and selects no workspace
    assert nodes["aws_sns_topic.alerts"]["attributes"]["name"] == topic  # %{if} in a template
    instance = nodes["aws_instance.app"]
    assert instance["attributes"]["instance_type"] == instance_type  # conditional on tfvars
    assert instance["properties"]["created"] == instances  # count through for_each -> module
    # templatefile() is rendered, %{for} included
    assert instance["attributes"]["user_data"].split() == [
        "#!/bin/sh",
        f"ENV={env}",
        "open_port",
        "443",
        "open_port",
        "8443",
    ]
    ingress = nodes["aws_security_group.app"]["attributes"]["ingress"]
    assert [i["from_port"] for i in ingress] == [443, 8443]  # dynamic block, expanded


def test_a_link_only_known_after_apply_is_not_an_unknown(api, rows):
    tags = _shown(api, "dev")["aws_sqs_queue.derived"]["attributes"]["tags"]
    assert tags["Peer"]["attr"] == "arn"  # coalesce(null, an ARN): the code shows the link
    assert tags["Remote"]["attr"] == "some_arn"  # output of a module whose code is not here
    assert tags["Region"]["refs"] == [f"{APP}::aws_sqs_queue.main"]  # split() over an ARN
    assert tags["Created"] == {"$template": "(known after apply)", "refs": []}  # timestamp()
    subjects = {u["subject"] for r in rows.values() for u in r["unknowns"]}
    assert not [s for s in subjects if "derived" in s or "aws_subnet" in s]


def test_the_summary_counts_the_environments_shown(api):
    summary = json.loads(_get(api, "/api/summary"))
    assert summary["summary"]["unknowns"] == 3


def test_a_variable_ci_sets_is_named_with_the_line_setting_it(rows):
    (entry,) = [u for u in rows[f"{APP}:dev"]["unknowns"] if u["attribute"] == "image_uri"]
    assert "set at run time by .github/workflows/deploy.yml:10" in entry["reason"]


# -- the grouped panel ----------------------------------------------------------------------


def test_remaining_unknowns_are_grouped_by_reason_with_real_file_lines(api):
    view = json.loads(_get(api, "/api/environments"))
    groups = view["current"]["unknown_groups"]
    assert sorted((g["category"], g["count"]) for g in groups) == [
        ("missing variable", 1),
        ("module not analyzed", 1),
        ("set by CI at run time", 1),
    ]
    for group in groups:
        for item in group["items"]:
            assert item["location"]["file"].endswith(".tf")
            assert (FIXTURE / item["location"]["file"]).is_file()
    for row in [*view["environments"], *view["columns"]]:
        assert sum(g["count"] for g in row["unknown_groups"]) == row["unknown_count"]


def test_groups_are_largest_first():
    reason = "variable var.a has no default and no value in tfvars/--var-file"
    entries = [
        {"subject": "a", "attribute": "x", "reason": reason, "location": None},
        {"subject": "b", "attribute": "x", "reason": reason, "location": None},
        {"subject": "c", "attribute": "x", "reason": "circular reference while", "location": None},
    ]
    assert [(g["category"], g["count"]) for g in unknown_groups(entries)] == [
        ("missing variable", 2),
        ("circular reference", 1),
    ]


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        (
            "variable var.a is set at run time by ci.yml:1 (not in the repository)",
            "set by CI at run time",
        ),
        ("conditional depends on unknown value (x)", "conditional without default"),
        ("dynamic block 'a' is not expanded statically", "dynamic block"),
        ("module.a is recursive (modules/a)", "module not analyzed"),
        ("function foo() is not evaluated statically", "function not evaluated"),
        ("reference to undeclared object a.b", "broken reference"),
        ("something nobody has seen", "other"),
    ],
)
def test_reason_categories(reason, category):
    assert unknown_category(reason) == category


# -- evidence: real files only --------------------------------------------------------------


def test_docs_create_nothing(model, rows):
    assert sorted(rows) == [f"{APP}:dev", f"{APP}:prod", f"{NETWORK}:dev", f"{NETWORK}:prod"]
    assert {e["name"] for e in model["structure"]["environments"]} == {"dev", "prod"}


def test_ui_data_never_names_a_doc_or_what_only_a_doc_says(api):
    phantom = re.compile(r"\.(md|markdown)\b|\b(staging|sandbox|qa|ghost)\b|stacks/legacy|\.gone\b")
    for name in UI_DATA:
        found = phantom.search(_get(api, f"/api/{name}"))
        assert found is None, (name, found and found.group())


def test_templates_and_the_ui_name_no_doc_file():
    for path in sorted(TEMPLATES.glob("*.html")):
        assert re.search(r"\b[\w-]+\.md\b", path.read_text()) is None, path.name


def test_the_ui_never_links_a_doc():
    js = (STATIC_DIR / "app.js").read_text()
    assert "if (!loc || DOC_FILE.test(loc.file)) return null;" in js
    assert (
        'if (DOC_FILE.test(node.path)) return el("span", { class: "file other" }, node.name);' in js
    )


# -- commands -------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_ci_commands_are_verified_against_the_real_files_once_per_step(rows, env):
    commands = rows[f"{APP}:{env}"]["commands"]
    workflow = ".github/workflows/deploy.yml"
    assert [
        (c["step"], c["source"], c["command"], c["location"]["start_line"]) for c in commands
    ] == [
        ("init", "ci", f"terraform init -backend-config=envs/backend-{env}.hcl", 14),
        ("plan", "ci", f"terraform plan -var-file=envs/{env}.tfvars -out=tfplan", 18),
        ("apply", "ci", "terraform apply -auto-approve tfplan", 23),
    ]
    assert {c["location"]["file"] for c in commands} == {workflow}


def test_a_stack_ci_never_runs_gets_the_commands_its_files_make(rows):
    commands = rows[f"{NETWORK}:dev"]["commands"]
    assert [(c["source"], c["command"]) for c in commands] == [
        ("files", f"terraform -chdir={NETWORK} init -backend-config=envs/backend-dev.hcl"),
        ("files", f"terraform -chdir={NETWORK} plan -var-file=envs/dev.tfvars"),
        ("files", f"terraform -chdir={NETWORK} apply -var-file=envs/dev.tfvars"),
    ]
    assert commands[0]["location"]["file"] == f"{NETWORK}/envs/backend-dev.hcl"


def test_a_line_naming_a_missing_file_only_reaches_the_debug_log(api, caplog):
    with caplog.at_level("DEBUG", logger="tflens.ciscan"):
        view = _get(api, "/api/environments")
    assert "ghost.tfvars" not in view
    assert "deploy.yml:29 not verified" in caplog.text


def test_the_environment_table_has_a_popover_and_no_ci_column(api):
    js = (STATIC_DIR / "app.js").read_text()
    table = js[js.index("function envFilesTable(envs)") : js.index("function envButton(envs, id)")]
    assert "commandsInfo(r)" in table
    heads = re.findall(r'el\("th", \{\}, "([^"]+)"\)', table)
    assert heads == ["Environment", "Stack", "Wired", "tfvars", "Backend config"]
    assert "CI / script" not in js
    for row in json.loads(_get(api, "/api/environments"))["environments"]:
        assert "var_file_cli" not in row
        assert not [w for w in row["wiring"] if w.startswith("CI")]


def _one_workflow(tmp_path, command):
    (tmp_path / "main.tf").write_text('variable "db_password" {\n  type = string\n}\n')
    (tmp_path / "dev.tfvars").write_text('db_password = "example-placeholder"\n')
    ci = tmp_path / ".github" / "workflows"
    ci.mkdir(parents=True)
    (ci / "deploy.yml").write_text(f"jobs:\n  a:\n    steps:\n      - run: {command}\n")
    return load_repo(tmp_path)


def test_no_secret_value_reaches_a_command_or_a_variable(tmp_path):
    repo = _one_workflow(
        tmp_path, "terraform plan -var-file=dev.tfvars -var db_password=example-placeholder"
    )
    env = {"name": "dev", "root": ".", "var_files": ["dev.tfvars"], "backend_hcl": []}
    plan = next(c for c in env_commands(repo, env) if c["step"] == "plan")
    assert plan["source"] == "ci"
    assert plan["command"] == "terraform plan -var-file=dev.tfvars -var db_password=***"
    assert ci_var_inputs(repo)["db_password"][0] is None


# -- counts and the selected row ------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["env_layout", "resolver_chain"])
def test_no_uncertain_count_is_shown_anywhere(tmp_path, fixture):
    api = Api(ModelStore(FIXTURES / fixture, tmp_path / "model.json", auto_env=True))
    for name in UI_DATA:
        assert re.search(r"\+\d+ uncertain|\(\+\d+\b", _get(api, f"/api/{name}")) is None, name
    assert re.search(r"\(\+\$\{|\+\d+ uncertain", (STATIC_DIR / "app.js").read_text()) is None


def test_only_the_selected_row_is_highlighted():
    js = (STATIC_DIR / "app.js").read_text()
    table = js[js.index("function envFilesTable(envs)") : js.index("function envButton(envs, id)")]
    assert 'isSelected(envs, r.id) ? " active" : ""' in table
    assert "isShown" not in table
    css = (STATIC_DIR / "app.css").read_text()
    assert "#environments tr.active td" in css


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_one_row_is_selected_when_two_stacks_show_an_environment():
    js = (STATIC_DIR / "app.js").read_text()
    start = js.index("let pickedEnv = null;")
    code = (
        js[start : js.index("function multiStack(envs)")]
        + """
const envs = { active: null, selected: ["app:dev", "net:dev"],
  environments: [{ id: "app:dev" }, { id: "app:prod" }, { id: "net:dev" }, { id: "net:prod" }] };
const marked = () => envs.environments.map((r) => r.id).filter((id) => isSelected(envs, id));
const out = [marked()];
pickedEnv = "net:dev";
out.push(marked());
pickedEnv = "dev";
envs.active = "app:prod";
out.push(marked());
console.log(JSON.stringify(out));
"""
    )
    done = subprocess.run([_NODE, "-e", code], capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == [[], ["net:dev"], ["app:prod"]]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_the_ui_script_parses():
    subprocess.run([_NODE, "--check", str(STATIC_DIR / "app.js")], check=True)
