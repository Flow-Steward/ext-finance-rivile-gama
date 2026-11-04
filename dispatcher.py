"""Closed dispatcher for the Rivile GAMA extension.

``OPERATION_IDS`` is the whole routable surface. An operation id that is not in
it is refused before the connection is read and before any transport exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flowsteward_extension_sdk import runtime_context_from_payload

from runtime import errors
from runtime.catalog import OPERATION_IDS
from runtime.errors import error_response
from runtime.operations import handle_runtime


def dispatch_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    """Route one runtime invocation, or refuse it."""
    try:
        runtime_context_from_payload(payload)
    except ValueError as exc:
        response = error_response(errors.INVALID_HOST_ENVELOPE, str(exc))
        response["definitely_no_external_effect"] = True
        response["external_effect_status"] = "failed"
        return response
    if _operation_id(payload) not in OPERATION_IDS:
        response = error_response(
            errors.UNSUPPORTED_OPERATION, "That operation is not part of this extension"
        )
        response["definitely_no_external_effect"] = True
        response["external_effect_status"] = "failed"
        return response
    return handle_runtime(payload)


def _operation_id(payload: Any) -> str:
    if not isinstance(payload, Mapping) or payload.get("mode") != "action":
        return ""
    action = payload.get("action")
    if not isinstance(action, Mapping):
        return ""
    value = action.get("action_id") or action.get("operation_id")
    return value.strip() if isinstance(value, str) else ""


__all__ = ["OPERATION_IDS", "dispatch_runtime"]
