#!/usr/bin/env python3
"""Offline smoke check: health, a suppressed write and a refused operation, no network."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"group_id": None, "for_each_id": None, "step_id": None, "step_execution_id": None}


def _run(args: list[str], payload: dict | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = proc.stdout.strip()
    if not output:
        raise RuntimeError(f"{args} printed nothing: {proc.stderr}")
    return json.loads(output)


def _action(operation_id: str, operation_input: dict, *, test_mode: bool) -> dict:
    return {
        "contract_version": "extension_host_v2",
        "mode": "action",
        "runtime_context": {**SCOPE, "test_mode": test_mode},
        "action": {
            "action_id": operation_id,
            "input": {"connection_ref": "smoke", **operation_input},
            "target": {
                "connection": {
                    "connection_id": "smoke",
                    "connection_type_id": "rivile_gama",
                    "config": {},
                    "secrets": {"api_key": "SMOKE.not-a-real-key"},
                }
            },
        },
    }


def main() -> int:
    health = _run(["health.py"])
    if health.get("status") != "healthy" or health.get("rivile_methods") != 209:
        raise RuntimeError(f"health failed: {health}")
    suppressed = _run(
        ["main.py"],
        _action("edit_n08", {"oper": "I", "data": {"N08": {"N08_PAV": "Smoke"}}}, test_mode=True),
    )
    if not suppressed.get("ok") or suppressed.get("external_effect_status") != "suppressed":
        raise RuntimeError(f"test-mode write was not suppressed: {suppressed}")
    refused = _run(["main.py"], _action("not_an_operation", {}, test_mode=False))
    if refused.get("error_code") != "unsupported_operation":
        raise RuntimeError(f"unknown operation was not refused: {refused}")
    print(json.dumps({"ok": True, "extension_id": "flowsteward.rivile-gama"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
