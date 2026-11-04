"""Run one workflow operation against Rivile GAMA API v2."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    find_artifact_descriptor,
    read_artifact_bytes,
    write_artifact_bytes,
)

from . import catalog, errors
from .connection import Connection, connection_from_payload
from .errors import ExtensionError, error_response
from .models import Method
from .transport import Reply, RivileTransport

PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 10
MAX_PAGES = 100
EXPORT_MAX_PAGES = 2000
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_EXPORT_BYTES = 200 * 1024 * 1024
MAX_PARAM_TEXT = 30 * 1024 * 1024

_ALLOWED_ACTION_KEYS = frozenset(
    {"action_id", "operation_id", "page_id", "component_id", "context", "input", "target"}
)
_UPPER_PARAMS = frozenset({"oper", "erroraction", "list", "listtype"})
_EXT_RE = re.compile(r"^\.?[A-Za-z0-9]{1,10}$")
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".xml": "application/xml",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

TransportFactory = Callable[[Connection], RivileTransport]


def _invalid(message: str) -> ExtensionError:
    return ExtensionError(errors.INVALID_PAYLOAD, message, definitely_no_external_effect=True)


# ---------------------------------------------------------------------------
# Entry point


def handle_runtime(
    payload: Mapping[str, Any],
    *,
    transport_factory: TransportFactory = RivileTransport,
    artifact_reader: Callable[..., bytes] = read_artifact_bytes,
    artifact_writer: Callable[..., Mapping[str, Any]] = write_artifact_bytes,
) -> dict[str, Any]:
    """Dispatch one invocation through the closed registry."""
    try:
        operation_id, raw_input = _envelope(payload)
        if operation_id not in catalog.OPERATION_IDS:
            raise ExtensionError(
                errors.UNSUPPORTED_OPERATION, "That operation is not part of this extension"
            )
        operation_input = _as_input(raw_input)
        write = operation_id in catalog.WRITE_OPERATION_IDS
        if write and _test_mode(payload):
            # Validated first, then suppressed before the connection is even read:
            # a rehearsal can never reach Rivile.
            _validate_only(operation_id, operation_input)
            return _ok(_neutral_result(operation_id))
        connection = connection_from_payload(
            payload, connection_ref=_text(operation_input.get("connection_ref"))
        )
        transport = transport_factory(connection)
        context = _Context(
            payload=payload,
            operation_input=operation_input,
            connection=connection,
            transport=transport,
            artifact_reader=artifact_reader,
            artifact_writer=artifact_writer,
        )
        if operation_id == catalog.TEST_CONNECTION:
            return _ok(_test_connection(context))
        if operation_id == catalog.CALL_METHOD:
            return _ok(_call_method(context))
        if operation_id == catalog.EXPORT_LIST:
            return _ok(_export_list(context))
        method = catalog.METHODS_BY_OPERATION[operation_id]
        return _ok(_run_method(context, method))
    except ExtensionError as exc:
        return error_response(
            exc.code, exc.message, retry_facts=exc.retry_facts(), data_errors=exc.data_errors
        )
    except Exception:
        return error_response(errors.INTERNAL_ERROR, "The operation could not be completed")


class _Context:
    def __init__(
        self,
        *,
        payload: Mapping[str, Any],
        operation_input: Mapping[str, Any],
        connection: Connection,
        transport: RivileTransport,
        artifact_reader: Callable[..., bytes],
        artifact_writer: Callable[..., Mapping[str, Any]],
    ) -> None:
        self.payload = payload
        self.operation_input = operation_input
        self.connection = connection
        self.transport = transport
        self.artifact_reader = artifact_reader
        self.artifact_writer = artifact_writer


# ---------------------------------------------------------------------------
# Envelope and inputs


def _envelope(payload: Mapping[str, Any]) -> tuple[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("mode") != "action":
        raise _invalid("Request mode must be action")
    action = payload.get("action")
    if not isinstance(action, Mapping) or set(action) - _ALLOWED_ACTION_KEYS:
        raise _invalid("The action envelope is invalid")
    operation_id = action.get("action_id") or action.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise _invalid("An operation id is required")
    return operation_id.strip(), action.get("input")


def _as_input(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _invalid("The operation input must be an object")
    return {str(k): v for k, v in raw.items() if v is not None}


def _test_mode(payload: Mapping[str, Any]) -> bool:
    context = payload.get("runtime_context")
    if not isinstance(context, Mapping):
        return False
    value = context.get("test_mode", False)
    if not isinstance(value, bool):
        raise _invalid("runtime_context.test_mode must be boolean")
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _scalar(name: str, value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_PARAM_TEXT:
            raise _invalid(f"{name} is too long")
        return value
    raise _invalid(f"{name} must be text or a number")


def _positive_int(name: str, value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise _invalid(f"{name} must be a whole number from 1 to {maximum}")
    return value


def _bool(name: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid(f"{name} must be true or false")
    return value


def _allowed_inputs(method: Method) -> set[str]:
    names = {"connection_ref", "extra_params"} | {name for name, _wire in method.params}
    if method.data_roots:
        names.add("data")
    if method.paged:
        names |= {"fetch_all_pages", "max_pages"}
    if method.method in catalog.UPLOAD_METHODS:
        names.add("document_file")
    if method.method in catalog.DOWNLOAD_METHODS:
        names.add("save_document_file")
    return names


def _method_request(
    method: Method, operation_input: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Typed inputs -> the Rivile ``params`` and ``data`` blocks."""
    unknown = set(operation_input) - _allowed_inputs(method)
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")
    params: dict[str, Any] = {}
    for name, wire in method.params:
        if name not in operation_input:
            continue
        value = _scalar(name, operation_input[name])
        if wire.lower() in _UPPER_PARAMS and isinstance(value, str):
            value = value.strip().upper()
        if wire.lower() == "pagenumber":
            # Rivile's own examples send the page both as 1 and as "1".
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            value = _positive_int(name, value, default=1, maximum=1000000)
        params[wire] = value
    extra = operation_input.get("extra_params")
    if extra is not None:
        if not isinstance(extra, Mapping):
            raise _invalid("extra_params must be an object")
        documented = {wire.lower() for _n, wire in method.params} | {"method"}
        for key, value in extra.items():
            key_text = str(key).strip()
            if not key_text or key_text.lower() in documented:
                raise _invalid(f"extra_params may not repeat a documented param: {key_text}")
            params[key_text] = _scalar(f"extra_params.{key_text}", value)
    data = operation_input.get("data")
    if data is not None:
        if not isinstance(data, Mapping):
            raise _invalid("data must be an object")
        data = dict(data)
    if method.write and method.data_roots and not data:
        roots = ", ".join(method.data_roots)
        raise _invalid(f"data is required: give the {roots} block")
    return params, data


def _validate_only(operation_id: str, operation_input: Mapping[str, Any]) -> None:
    if operation_id == catalog.CALL_METHOD:
        _call_method_request(operation_input)
        return
    method = catalog.METHODS_BY_OPERATION[operation_id]
    _method_request(method, operation_input)


# ---------------------------------------------------------------------------
# Results


def _base_result(method: str, reply: Reply | None) -> dict[str, Any]:
    parsed = reply.parsed if reply else None
    data_errors = list(parsed.data_errors) if parsed else []
    if parsed and parsed.error_message and not data_errors and reply and reply.status != 200:
        data_errors.append({"tag": "", "code": "", "message": parsed.error_message, "data": ""})
    records = parsed.records if parsed else []
    return {
        "method": method,
        "records": records,
        "record": records[0] if records else None,
        "record_type": parsed.record_type if parsed else None,
        "tables": dict(parsed.tables) if parsed else {},
        "count": len(records),
        "data_errors": data_errors,
        "partial_success": bool(reply and reply.status == 207),
        "http_status": reply.status if reply else 0,
        "duration_ms": parsed.duration_ms if parsed else None,
    }


def _neutral_result(operation_id: str) -> dict[str, Any]:
    method = (
        catalog.METHODS_BY_OPERATION[operation_id].method
        if operation_id in catalog.METHODS_BY_OPERATION
        else ""
    )
    result = _base_result(method, None)
    method_row = catalog.METHODS_BY_OPERATION.get(operation_id)
    if method_row is not None and method_row.paged:
        result.update({"page_number": None, "pages_fetched": 0, "has_more": False})
    result["external_effect_status"] = "suppressed"
    result["definitely_no_external_effect"] = True
    return result


def _mark_write(result: dict[str, Any]) -> dict[str, Any]:
    result["external_effect_status"] = "succeeded"
    result["definitely_no_external_effect"] = False
    return result


def _ok(result: Mapping[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "result": dict(result),
        "error_code": None,
        "error": None,
        "errors": [],
    }
    if "external_effect_status" in result:
        response["external_effect_status"] = result["external_effect_status"]
        response["definitely_no_external_effect"] = result["definitely_no_external_effect"]
    return response


# ---------------------------------------------------------------------------
# Operations


def _test_connection(context: _Context) -> dict[str, Any]:
    unknown = set(context.operation_input) - {"connection_ref"}
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")
    reply = context.transport.call("GET_N45_LIST", {"pagenumber": 1}, None, mutating=False)
    return {
        "connected": True,
        "api_user": context.connection.api_user,
        "currencies": len(reply.parsed.records),
        "duration_ms": reply.parsed.duration_ms,
        "http_status": reply.status,
    }


def _run_method(context: _Context, method: Method) -> dict[str, Any]:
    params, data = _method_request(method, context.operation_input)
    if method.method in catalog.UPLOAD_METHODS:
        _attach_upload(context, params)
    if method.paged:
        return _run_paged(context, method, params, data)
    reply = context.transport.call(method.method, params, data, mutating=method.write)
    result = _base_result(method.method, reply)
    if method.method in catalog.DOWNLOAD_METHODS:
        _save_download(context, result)
    return _mark_write(result) if method.write else result


def _run_paged(
    context: _Context, method: Method, params: dict[str, Any], data: dict[str, Any] | None
) -> dict[str, Any]:
    inputs = context.operation_input
    fetch_all = _bool("fetch_all_pages", inputs.get("fetch_all_pages"), False)
    max_pages = _positive_int(
        "max_pages", inputs.get("max_pages"), default=DEFAULT_MAX_PAGES, maximum=MAX_PAGES
    )
    page_param = method.page_param
    if not fetch_all:
        reply = context.transport.call(method.method, params, data, mutating=False)
        result = _base_result(method.method, reply)
        result["page_number"] = params.get(page_param) if page_param else None
        result["pages_fetched"] = 1
        result["has_more"] = reply.parsed.top_level_count >= PAGE_SIZE
        return result
    page = int(params.get(page_param, 1)) if page_param else 1
    merged: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    data_errors: list[dict[str, str]] = []
    last: Reply | None = None
    fetched = 0
    has_more = False
    while fetched < max_pages:
        page_params = {**params, page_param or "pagenumber": page}
        last = context.transport.call(method.method, page_params, data, mutating=False)
        fetched += 1
        for tag in last.parsed.order:
            if tag not in merged:
                merged[tag] = []
                order.append(tag)
            merged[tag].extend(last.parsed.tables[tag])
        data_errors.extend(last.parsed.data_errors)
        has_more = last.parsed.top_level_count >= PAGE_SIZE
        if not has_more:
            break
        page += 1
    result = _base_result(method.method, last)
    records = merged.get(order[0], []) if order else []
    result.update(
        {
            "records": records,
            "record": records[0] if records else None,
            "record_type": order[0] if order else None,
            "tables": merged,
            "count": len(records),
            "data_errors": data_errors,
            "page_number": page,
            "pages_fetched": fetched,
            "has_more": has_more,
        }
    )
    return result


def _call_method_request(
    operation_input: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    unknown = set(operation_input) - {"connection_ref", "method", "params", "data"}
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")
    method = _text(operation_input.get("method")).upper()
    if not catalog.METHOD_NAME_RE.fullmatch(method):
        raise _invalid("method must be a Rivile method name such as GET_N08_LIST or EDIT_N08")
    params = operation_input.get("params") or {}
    data = operation_input.get("data")
    if not isinstance(params, Mapping):
        raise _invalid("params must be an object")
    if data is not None and not isinstance(data, Mapping):
        raise _invalid("data must be an object")
    return method, dict(params), dict(data) if data else None


def _call_method(context: _Context) -> dict[str, Any]:
    method, params, data = _call_method_request(context.operation_input)
    mutating = not method.startswith(("GET_", "MGAMA_GET_"))
    reply = context.transport.call(method, params, data, mutating=mutating)
    result = _base_result(method, reply)
    if mutating:
        return _mark_write(result)
    result["external_effect_status"] = "succeeded"
    result["definitely_no_external_effect"] = True
    return result


def _export_list(context: _Context) -> dict[str, Any]:
    inputs = context.operation_input
    unknown = set(inputs) - {"connection_ref", "method", "params", "max_pages"}
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")
    method_name = _text(inputs.get("method")).upper()
    method = catalog.METHODS_BY_NAME.get(method_name)
    if method is None or not method.paged:
        raise _invalid("method must be one of the Rivile list methods")
    raw_params = inputs.get("params") or {}
    if not isinstance(raw_params, Mapping):
        raise _invalid("params must be an object")
    page_param = method.page_param or "pagenumber"
    params = {
        str(k): _scalar(f"params.{k}", v)
        for k, v in raw_params.items()
        if str(k).lower() != "pagenumber"
    }
    max_pages = _positive_int(
        "max_pages", inputs.get("max_pages"), default=EXPORT_MAX_PAGES, maximum=EXPORT_MAX_PAGES
    )
    chunks: list[bytes] = []
    total = 0
    count = 0
    fetched = 0
    complete = False
    record_type: str | None = None
    page = 1
    while fetched < max_pages:
        reply = context.transport.call(
            method.method, {**params, page_param: page}, None, mutating=False
        )
        fetched += 1
        record_type = record_type or reply.parsed.record_type
        for tag in reply.parsed.order:
            for record in reply.parsed.tables[tag]:
                line = json.dumps({"_table": tag, **record}, ensure_ascii=False) + "\n"
                encoded = line.encode("utf-8")
                total += len(encoded)
                if total > MAX_EXPORT_BYTES:
                    raise ExtensionError(
                        errors.RESPONSE_TOO_LARGE,
                        "The export exceeds 200 MB; narrow it with a fil filter",
                    )
                chunks.append(encoded)
                count += 1
        if reply.parsed.top_level_count < PAGE_SIZE:
            complete = True
            break
        page += 1
    try:
        written = context.artifact_writer(
            dict(context.payload),
            b"".join(chunks),
            binding_key=catalog.EXPORT_BINDING,
            content_type="application/x-ndjson",
            timeout_seconds=300.0,
        )
    except ExtensionError:
        raise
    except Exception as exc:
        raise ExtensionError(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE, "The export file could not be saved"
        ) from exc
    handle = _text(written.get("artifact_handle")) if isinstance(written, Mapping) else ""
    if not handle:
        raise ExtensionError(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE, "The export file could not be saved"
        )
    return {
        "artifact_handle": handle,
        "filename": f"rivile-{method.method.lower()}.jsonl",
        "content_type": "application/x-ndjson",
        "record_count": count,
        "pages_fetched": fetched,
        "complete": complete,
        "record_type": record_type,
    }


# ---------------------------------------------------------------------------
# Files


def _attach_upload(context: _Context, params: dict[str, Any]) -> None:
    handle = _text(context.operation_input.get("document_file"))
    if not handle:
        if not params.get("doc") and not params.get("nuoroda"):
            oper = str(params.get("oper") or "I").upper()
            if oper != "D":
                raise _invalid("Give document_file, doc (Base64) or nuoroda (a link)")
        return
    if params.get("doc"):
        raise _invalid("Give either document_file or doc, not both")
    try:
        body = context.artifact_reader(dict(context.payload), artifact_id=handle)
    except ExtensionError:
        raise
    except Exception as exc:
        raise ExtensionError(
            errors.ARTIFACT_INPUT_UNAVAILABLE,
            "document_file could not be read from Flow Steward storage",
            definitely_no_external_effect=True,
        ) from exc
    if not isinstance(body, (bytes, bytearray)) or not body:
        raise _invalid("document_file is empty")
    if len(body) > MAX_UPLOAD_BYTES:
        raise _invalid("document_file is larger than 15 MB")
    params["doc"] = base64.b64encode(bytes(body)).decode("ascii")
    if not params.get("ext"):
        params["ext"] = _extension_of(context.payload, handle) or ".pdf"


def _extension_of(payload: Mapping[str, Any], handle: str) -> str:
    try:
        descriptor = find_artifact_descriptor(dict(payload), artifact_id=handle)
    except Exception:
        return ""
    for key in ("filename", "name"):
        value = descriptor.get(key) if isinstance(descriptor, Mapping) else None
        if isinstance(value, str) and "." in value:
            ext = "." + value.rsplit(".", 1)[-1]
            if _EXT_RE.fullmatch(ext):
                return ext.lower()
    return ""


def _save_download(context: _Context, result: dict[str, Any]) -> None:
    result["document_file"] = None
    result["document_filename"] = None
    if not _bool("save_document_file", context.operation_input.get("save_document_file"), True):
        return
    record = result.get("record")
    if not isinstance(record, dict):
        return
    encoded = record.get("DOC")
    if not isinstance(encoded, str) or not encoded.strip():
        return
    try:
        body = base64.b64decode("".join(encoded.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExtensionError(
            errors.INVALID_XML_RESPONSE, "Rivile returned a document that is not valid Base64"
        ) from exc
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ExtensionError(
            errors.RESPONSE_TOO_LARGE, "The attached document is larger than 50 MB"
        )
    ext = str(record.get("EXT") or "").strip().lower()
    ext = ext if _EXT_RE.fullmatch(ext or "-") else ""
    if ext and not ext.startswith("."):
        ext = "." + ext
    code = re.sub(r"[^A-Za-z0-9_-]+", "", str(record.get("KODAS_PO") or "document")) or "document"
    filename = f"{code}{ext}"
    try:
        written = context.artifact_writer(
            dict(context.payload),
            body,
            binding_key=catalog.DOWNLOAD_BINDING,
            content_type=_CONTENT_TYPES.get(ext, "application/octet-stream"),
            timeout_seconds=120.0,
        )
    except Exception as exc:
        raise ExtensionError(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE, "The document file could not be saved"
        ) from exc
    handle = _text(written.get("artifact_handle")) if isinstance(written, Mapping) else ""
    if not handle:
        raise ExtensionError(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE, "The document file could not be saved"
        )
    # The file now travels as a handle; the Base64 copy is dropped so the step
    # output stays small.
    for row in result["records"]:
        if isinstance(row, dict) and "DOC" in row:
            row["DOC"] = None
    result["document_file"] = handle
    result["document_filename"] = filename


__all__ = ["DEFAULT_MAX_PAGES", "MAX_PAGES", "PAGE_SIZE", "handle_runtime"]
