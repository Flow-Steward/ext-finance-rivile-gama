#!/usr/bin/env python3
"""Rivile GAMA subprocess entrypoint."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from dispatcher import dispatch_runtime

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_SERIALIZED_RESPONSE_BYTES = 9 * 512 * 1024
_CONTROL_TRANSLATION = {
    codepoint: " " for codepoint in (*range(32), *range(127, 160)) if codepoint not in {9, 10, 13}
}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        response = _error_response("invalid_payload", "The request exceeds its safe limit")
    else:
        try:
            payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _error_response("invalid_payload", "The request is not valid JSON")
        else:
            response = dispatch_runtime(payload)
    final_response, serialized = _bounded_serialized_response(response)
    sys.stdout.buffer.write(serialized)
    sys.stdout.buffer.write(b"\n")
    return 0 if final_response.get("ok") is True else 2


def _bounded_serialized_response(response: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    sanitized, changed = _sanitize_json_value(dict(response))
    if not isinstance(sanitized, dict):
        raise TypeError("extension response must be an object")
    if changed:
        sanitized["output_sanitized"] = True
    serialized = _json_bytes(sanitized)
    if len(serialized) + 1 <= MAX_SERIALIZED_RESPONSE_BYTES:
        return sanitized, serialized
    error_response = _error_response(
        "response_too_large", "The extension response exceeds its safe output limit"
    )
    return error_response, _json_bytes(error_response)


def _sanitize_json_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        sanitized = value.translate(_CONTROL_TRANSLATION)
        return sanitized, sanitized != value
    if isinstance(value, Mapping):
        mapped: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            sanitized, item_changed = _sanitize_json_value(item)
            mapped[key] = sanitized
            changed = changed or item_changed
        return mapped, changed
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items: list[Any] = []
        changed = False
        for item in value:
            sanitized, item_changed = _sanitize_json_value(item)
            items.append(sanitized)
            changed = changed or item_changed
        return items, changed
    return value, False


def _json_bytes(response: Mapping[str, Any]) -> bytes:
    return json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _error_response(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "result": {},
        "error_code": code,
        "error": message,
        "errors": [{"code": code, "message": message}],
    }


if __name__ == "__main__":
    raise SystemExit(main())
