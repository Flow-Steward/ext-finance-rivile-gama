"""The committed contracts are exactly what the generator builds from the Rivile reference."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import build_contracts
import pytest
import yaml
from conftest import BUNDLE_ROOT, payload

from runtime import catalog
from runtime.methods import METHODS
from runtime.operations import _method_request, handle_runtime

SPEC = json.loads((BUNDLE_ROOT / "tests/reference/rivile_api_v2_spec.json").read_text("utf-8"))


def _load(relative: str) -> Any:
    return yaml.safe_load((BUNDLE_ROOT / relative).read_text("utf-8"))


@pytest.fixture(scope="module")
def built() -> dict[str, Any]:
    return build_contracts.outputs()


def test_operation_manifest_is_generated(built) -> None:
    assert _load("contracts/operation_manifest.yaml") == {"operations": built["operations"]}


def test_actions_step_ui_policies_and_reference_are_generated(built) -> None:
    assert _load("ui/actions/actions.yaml") == {"actions": built["actions"]}
    assert _load("contracts/step_ui_manifest.yaml") == {"forms": built["forms"]}
    assert _load("contracts/artifact_policies.yaml") == {
        "artifact_policies": built["artifact_policies"]
    }
    assert _load("ui/pages/api-reference.yaml") == built["reference_page"]
    committed = (BUNDLE_ROOT / "runtime/methods.py").read_text("utf-8")
    # Compared as syntax trees: the formatter may re-wrap the generated source.
    assert ast.dump(ast.parse(committed)) == ast.dump(ast.parse(built["registry"]))


def test_external_effects_cover_every_write(built) -> None:
    manifest = _load("extension.yaml")
    assert manifest["external_effects"] == built["effects"]
    effect_ids = {e["operation_id"] for e in manifest["external_effects"]}
    assert effect_ids == set(catalog.WRITE_OPERATION_IDS)
    for effect in manifest["external_effects"]:
        assert effect["idempotency"] == "required"
        assert effect["test_mode_behavior"] == "suppress"


def test_every_documented_method_is_an_operation() -> None:
    documented = {item["method"] for item in SPEC}
    assert len(documented) == 209
    assert documented == {m.method for m in METHODS}
    ops = {op["operation_id"] for op in _load("contracts/operation_manifest.yaml")["operations"]}
    assert ops == set(catalog.OPERATION_IDS)
    forms = {f["operation_id"] for f in _load("contracts/step_ui_manifest.yaml")["forms"]}
    actions = {a["action_id"] for a in _load("ui/actions/actions.yaml")["actions"]}
    assert forms == actions == ops


def test_actions_and_operations_agree_on_visibility_and_writes() -> None:
    operations = {
        op["operation_id"]: op for op in _load("contracts/operation_manifest.yaml")["operations"]
    }
    for action in _load("ui/actions/actions.yaml")["actions"]:
        op = operations[action["action_id"]]
        assert action["workflow_visible"] is op["workflow_visible"] is True
        write = op["operation_kind"] == "action"
        assert action["mutates_platform"] is write
        assert (action["action_id"] in catalog.WRITE_OPERATION_IDS) is write
        assert [p["name"] for p in action["parameters"]] == [i["name"] for i in op["inputs"]]


def _example_input(method_name: str, example: dict[str, Any]) -> dict[str, Any]:
    method = catalog.METHODS_BY_NAME[method_name]
    wire_to_name = {wire.lower(): name for name, wire in method.params}
    operation_input: dict[str, Any] = {"connection_ref": "c"}
    extra: dict[str, Any] = {}
    for key, value in (example.get("params") or {}).items():
        name = wire_to_name.get(key.lower())
        if name is None:
            extra[key] = value
        elif name not in operation_input:
            operation_input[name] = value
    if extra:
        operation_input["extra_params"] = extra
    if isinstance(example.get("data"), dict):
        operation_input["data"] = example["data"]
    return operation_input


@pytest.mark.parametrize(
    ("method_name", "example"),
    [
        (item["method"], example)
        for item in SPEC
        for example in item["examples"]
        if isinstance(example, dict) and "method" in example
    ],
)
def test_every_documented_example_is_accepted(method_name: str, example: dict[str, Any]) -> None:
    """Rivile's own request examples map onto the typed inputs and back unchanged."""
    method = catalog.METHODS_BY_NAME[method_name]
    operation_input = _example_input(method_name, example)
    if method.write and method.data_roots and not operation_input.get("data"):
        pytest.skip("the documented example has no data block")
    params, data = _method_request(method, operation_input)
    documented = {k.lower() for k in (example.get("params") or {})}
    assert {k.lower() for k in params} == documented
    if isinstance(example.get("data"), dict):
        assert data == example["data"]


def test_neutral_test_mode_result_has_every_declared_output() -> None:
    operations = {
        op["operation_id"]: op for op in _load("contracts/operation_manifest.yaml")["operations"]
    }
    for operation_id in sorted(catalog.WRITE_OPERATION_IDS - {"call_method"}):
        method = catalog.METHODS_BY_OPERATION[operation_id]
        data: dict[str, Any] | None = {method.data_roots[0]: {}} if method.data_roots else None
        inputs: dict[str, Any] = {"data": data} if data else {}
        if method.method in catalog.UPLOAD_METHODS:
            inputs["doc"] = "AA=="
        response = handle_runtime(payload(operation_id, inputs, test_mode=True))
        assert response["ok"] is True, (operation_id, response)
        declared = {o["name"] for o in operations[operation_id]["outputs"]}
        assert set(response["result"]) == declared, operation_id


def test_no_forbidden_json_authoring_files() -> None:
    forbidden = [
        p
        for p in BUNDLE_ROOT.rglob("*.json")
        if "tests" not in p.relative_to(BUNDLE_ROOT).parts and ".fs-python" not in p.parts
    ]
    assert forbidden == []


def test_health_command() -> None:
    proc = subprocess.run(
        [sys.executable, "health.py"], cwd=BUNDLE_ROOT, capture_output=True, text=True, check=True
    )
    health = json.loads(proc.stdout)
    assert health["status"] == "healthy"
    assert health["rivile_methods"] == 209
    assert health["declared_operations"] == 212


def test_main_refuses_another_host_contract() -> None:
    body = payload("get_n08_list")
    body["contract_version"] = "extension_host_v1"
    proc = subprocess.run(
        [sys.executable, "main.py"],
        cwd=BUNDLE_ROOT,
        input=json.dumps(body),
        capture_output=True,
        text=True,
        check=False,
    )
    response = json.loads(proc.stdout)
    assert response["ok"] is False
    assert response["error_code"] == "invalid_host_envelope"


def test_icon_is_present() -> None:
    assert Path(BUNDLE_ROOT / "icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
