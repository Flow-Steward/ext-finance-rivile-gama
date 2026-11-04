"""Run one workflow operation against Rivile GAMA API v2."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    find_artifact_descriptor,
    read_artifact_bytes,
    write_artifact_stream,
)

from . import budget, catalog, errors, friendly
from .connection import Connection, connection_from_payload
from .errors import ExtensionError, error_response
from .models import Method
from .transport import DeadlineReached, Reply, RivileTransport

PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 10
MAX_PAGES = 100
EXPORT_MAX_PAGES = 2000
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_EXPORT_BYTES = 200 * 1024 * 1024
MAX_PARAM_TEXT = 30 * 1024 * 1024
#: The entrypoint is given 600 s (extension.yaml). Every request must start early
#: enough to finish inside it, with a margin for the response; an operation that
#: saves a file keeps the artifact write's own timeout in reserve, so it can
#: always save what it read.
STEP_TIMEOUT_SECONDS = 600.0
STEP_MARGIN_SECONDS = 30.0
ARTIFACT_WRITE_TIMEOUT_SECONDS = 180.0

#: Must equal the content types granted in contracts/artifact_policies.yaml: the
#: SDK refuses an upload whose type differs from the signed grant.
DOWNLOAD_CONTENT_TYPE = "application/octet-stream"
EXPORT_CONTENT_TYPE = "application/x-ndjson"
EXPORT_FILENAME = "rivile-export.jsonl"

_ALLOWED_ACTION_KEYS = frozenset(
    {"action_id", "operation_id", "page_id", "component_id", "context", "input", "target"}
)
_UPPER_PARAMS = frozenset({"oper", "erroraction", "list", "listtype"})
_DIGITS = re.compile(r"[0-9]{1,9}")
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
#: File signatures used when a workflow file has no extension in its name.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"<?xml", ".xml"),
)

#: Operations that save a workflow file after reading.
_FILE_WRITING_OPS = frozenset(
    {catalog.EXPORT_LIST, "download_document_file", *(m.lower() for m in catalog.DOWNLOAD_METHODS)}
)

TransportFactory = Callable[[Connection], RivileTransport]
ArtifactWriter = Callable[..., Mapping[str, Any]]


def _invalid(message: str) -> ExtensionError:
    return ExtensionError(
        errors.INVALID_PAYLOAD,
        message,
        definitely_no_external_effect=True,
        external_effect_status="failed",
    )


# ---------------------------------------------------------------------------
# Entry point


def handle_runtime(
    payload: Mapping[str, Any],
    *,
    transport_factory: TransportFactory = RivileTransport,
    artifact_reader: Callable[..., bytes] = read_artifact_bytes,
    artifact_writer: ArtifactWriter = write_artifact_stream,
) -> dict[str, Any]:
    """Dispatch one invocation through the closed registry."""
    transport: RivileTransport | None = None
    try:
        operation_id, raw_input = _envelope(payload)
        if operation_id not in catalog.OPERATION_IDS:
            raise ExtensionError(
                errors.UNSUPPORTED_OPERATION,
                "That operation is not part of this extension",
                definitely_no_external_effect=True,
                external_effect_status="failed",
            )
        operation_input = _as_input(raw_input)
        write = operation_id in catalog.WRITE_OPERATION_IDS
        if write and _test_mode(payload):
            # The host suppresses test-mode writes before spawning the extension;
            # this is the backstop. Validated first, then nothing is sent.
            _validate_only(operation_id, operation_input)
            return _ok(_neutral_result(operation_id))
        connection = connection_from_payload(
            payload, connection_ref=_text(operation_input.get("connection_ref"))
        )
        transport = transport_factory(connection)
        reserve = ARTIFACT_WRITE_TIMEOUT_SECONDS if operation_id in _FILE_WRITING_OPS else 0.0
        transport.deadline = (
            transport.clock() + STEP_TIMEOUT_SECONDS - STEP_MARGIN_SECONDS - reserve
        )
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
        if operation_id in catalog.FRIENDLY_IDS:
            return _ok(_run_friendly(context, operation_id))
        method = catalog.METHODS_BY_OPERATION[operation_id]
        return _ok(_run_method(context, method))
    except DeadlineReached:
        return error_response(
            errors.TIMEOUT,
            "The step ran out of time before Rivile could be called",
            retry_facts={"definitely_no_external_effect": True, "external_effect_status": "failed"},
        )
    except ExtensionError as exc:
        _mark_unsent(exc, transport)
        return error_response(
            exc.code, exc.message, retry_facts=exc.retry_facts(), data_errors=exc.data_errors
        )
    except Exception:
        unsent = transport is None or not transport.attempted
        response = error_response(errors.INTERNAL_ERROR, "The operation could not be completed")
        if unsent:
            response["definitely_no_external_effect"] = True
            response["external_effect_status"] = "failed"
        return response


def _mark_unsent(exc: ExtensionError, transport: RivileTransport | None) -> None:
    """A failure before any request left the process cannot have had an effect."""
    if transport is not None and transport.attempted:
        return
    if exc.definitely_no_external_effect is None:
        exc.definitely_no_external_effect = True
    if exc.definitely_no_external_effect and exc.external_effect_status is None:
        exc.external_effect_status = "failed"


class _Context:
    def __init__(
        self,
        *,
        payload: Mapping[str, Any],
        operation_input: Mapping[str, Any],
        connection: Connection,
        transport: RivileTransport,
        artifact_reader: Callable[..., bytes],
        artifact_writer: ArtifactWriter,
    ) -> None:
        self.payload = payload
        self.operation_input = operation_input
        self.connection = connection
        self.transport = transport
        self.artifact_reader = artifact_reader
        self.artifact_writer = artifact_writer

    def clock(self) -> float:
        clock = getattr(self.transport, "clock", None)
        return float(clock()) if callable(clock) else time.monotonic()


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


def _whole(name: str, value: Any, *, default: int, maximum: int) -> int:
    """A whole number from 1 to ``maximum``; ASCII digit strings are accepted."""
    if value is None:
        return default
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        value = int(value.strip())
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


def _normalize_param(name: str, wire: str, value: Any) -> Any:
    value = _scalar(name, value)
    if wire.lower() in _UPPER_PARAMS and isinstance(value, str):
        value = value.strip().upper()
    if wire.lower() == "pagenumber":
        # Rivile's own examples send the page both as 1 and as "1".
        value = _whole(name, value, default=1, maximum=1000000)
    return value


def _method_request(
    method: Method, operation_input: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Typed inputs -> the Rivile ``params`` and ``data`` blocks."""
    unknown = set(operation_input) - _allowed_inputs(method)
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")
    missing = [
        name
        for name in method.required
        if name not in operation_input or operation_input[name] in ("", None)
    ]
    if missing:
        raise _invalid(f"Required input missing: {', '.join(missing)}")
    params: dict[str, Any] = {}
    for name, wire in method.params:
        if name in operation_input:
            params[wire] = _normalize_param(name, wire, operation_input[name])
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
    if method.method in catalog.SHOW_DOC_DEFAULT_ZERO:
        wire = method.wire_name("show_doc")
        if wire and wire not in params:
            params[wire] = 0
    return params, data


def _validate_only(operation_id: str, operation_input: Mapping[str, Any]) -> None:
    if operation_id in catalog.FRIENDLY_IDS:
        friendly.validate(operation_id, operation_input)
        return
    if operation_id == catalog.CALL_METHOD:
        _call_method_request(operation_input)
        return
    method = catalog.METHODS_BY_OPERATION[operation_id]
    params, _data = _method_request(method, operation_input)
    if method.method in catalog.UPLOAD_METHODS:
        _upload_needs_content(params, has_file=bool(_text(operation_input.get("document_file"))))


# ---------------------------------------------------------------------------
# Results


def _base_result(method: str, reply: Reply | None) -> dict[str, Any]:
    parsed = reply.parsed if reply else None
    data_errors = list(parsed.data_errors) if parsed else []
    if parsed and parsed.error_message and not data_errors and reply and reply.status != 200:
        data_errors.append({"tag": "", "code": "", "message": parsed.error_message, "data": ""})
    records = parsed.records if parsed else []
    other = (
        {tag: rows for tag, rows in parsed.tables.items() if tag != parsed.record_type}
        if parsed
        else {}
    )
    return {
        "method": method,
        "records": records,
        "record": records[0] if records else None,
        "record_type": parsed.record_type if parsed else None,
        "other_tables": other,
        "count": len(records),
        "data_errors": data_errors,
        "partial_success": bool(reply and reply.status == 207),
        "http_status": reply.status if reply else 0,
        "duration_ms": parsed.duration_ms if parsed else None,
    }


def _neutral_result(operation_id: str) -> dict[str, Any]:
    method_row = catalog.METHODS_BY_OPERATION.get(operation_id)
    result = _base_result(method_row.method if method_row else "", None)
    if method_row is not None and method_row.paged:
        result.update(
            {"page_number": None, "next_page": None, "pages_fetched": 0, "has_more": False}
        )
    # Nothing was sent. "failed" + definitely-no-effect is the pair Core accepts
    # for "no external effect happened"; an extension may not report "suppressed".
    result["external_effect_status"] = "failed"
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


def _strip_documents(rows: Iterable[Any]) -> None:
    """Drop inline Base64 file content (DOC) from returned rows."""
    for row in rows:
        if isinstance(row, dict) and row.get("DOC"):
            row["DOC"] = None


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
    if method.method in catalog.UPLOAD_METHODS or (
        method.method in catalog.SHOW_DOC_DEFAULT_ZERO and params.get("show_doc") != 1
    ):
        # A write never hands file content back: an echoed multi-MB upload would
        # overflow the step output after Rivile had already attached the file.
        _strip_documents(result["records"])
    if method.write:
        return _mark_write(_fit_write_result(result))
    budget.ensure_fits(result)
    return result


def _fit_write_result(result: dict[str, Any]) -> dict[str, Any]:
    """Shrink a write's echo until it fits; the write itself already happened.

    Failing here would turn a saved document into an error the workflow might
    retry, so the result keeps its success and loses detail instead: first the
    extra rows, then the record's content except its codes.
    """
    if budget.fits(result):
        return result
    result["records"] = result["records"][:1]
    result["other_tables"] = {}
    result["count"] = len(result["records"])
    result["data_errors"] = result["data_errors"][:20]
    if not budget.fits(result):
        record = result.get("record")
        keys = {k: v for k, v in record.items() if "KODAS" in k} if isinstance(record, dict) else {}
        result["records"] = [keys] if keys else []
        result["record"] = keys or None
        result["count"] = len(result["records"])
    result["data_errors"].append(
        {
            "tag": "",
            "code": "",
            "message": "Rivile's answer was too large for the step output and was shortened; "
            "the write was applied.",
            "data": "",
        }
    )
    return result


def _page_size(method: Method, params: Mapping[str, Any]) -> int:
    """Records per page: 100, or the documented ``top`` when the step sets it."""
    wire = method.wire_name("top")
    value = params.get(wire) if wire else None
    if value is None:
        return PAGE_SIZE
    return _whole("top", value, default=PAGE_SIZE, maximum=PAGE_SIZE)


def _run_paged(
    context: _Context, method: Method, params: dict[str, Any], data: dict[str, Any] | None
) -> dict[str, Any]:
    inputs = context.operation_input
    fetch_all = _bool("fetch_all_pages", inputs.get("fetch_all_pages"), False)
    max_pages = _whole(
        "max_pages", inputs.get("max_pages"), default=DEFAULT_MAX_PAGES, maximum=MAX_PAGES
    )
    page_param = method.page_param or "pagenumber"
    page_size = _page_size(method, params)
    if not fetch_all:
        reply = context.transport.call(method.method, params, data, mutating=method.write)
        result = _base_result(method.method, reply)
        has_more = reply.parsed.top_level_count >= page_size
        page = int(params.get(page_param) or 1)
        result["page_number"] = page
        result["next_page"] = page + 1 if has_more else None
        result["pages_fetched"] = 1
        result["has_more"] = has_more
        budget.ensure_fits(result)
        return result
    page = int(params.get(page_param, 1))
    records: list[dict[str, Any]] = []
    other: dict[str, list[dict[str, Any]]] = {}
    record_type: str | None = None
    data_errors: list[dict[str, str]] = []
    # The fixed result fields are metered once up front, the pages as they come.
    meter = budget.Meter()
    meter.admit({**_base_result(method.method, None), "page_number": 0, "next_page": 0})
    last: Reply | None = None
    last_page: int | None = None
    fetched = 0
    has_more = False
    while fetched < max_pages:
        try:
            reply = context.transport.call(
                method.method, {**params, page_param: page}, data, mutating=False
            )
        except DeadlineReached:
            if fetched == 0:
                raise
            has_more = True  # out of time: continue from next_page
            break
        record_type = record_type or reply.parsed.record_type
        page_rows = reply.parsed.tables.get(record_type or "", [])
        page_other = {t: r for t, r in reply.parsed.tables.items() if t != record_type}
        page_errors = list(reply.parsed.data_errors)
        if not meter.admit(
            {"records": page_rows, "other_tables": page_other, "data_errors": page_errors}
        ):
            if fetched == 0:
                raise budget.too_large()
            # This page does not fit in one step output: stop before it and let
            # the workflow continue from next_page.
            has_more = True
            break
        last, last_page = reply, page
        fetched += 1
        records.extend(page_rows)
        for tag, rows in page_other.items():
            other.setdefault(tag, []).extend(rows)
        data_errors.extend(page_errors)
        has_more = reply.parsed.top_level_count >= page_size
        if not has_more:
            break
        page += 1
    result = _base_result(method.method, last)
    result.update(
        {
            "records": records,
            "record": records[0] if records else None,
            "record_type": record_type,
            "other_tables": other,
            "count": len(records),
            "data_errors": data_errors,
            "page_number": last_page,
            "next_page": ((last_page or page - 1) + 1) if has_more else None,
            "pages_fetched": fetched,
            "has_more": has_more,
        }
    )
    budget.ensure_fits(result)  # the whole object, as the host will see it
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
    known = catalog.METHODS_BY_NAME.get(method)
    mutating = known.write if known else not method.startswith(("GET_", "MGAMA_GET_"))
    reply = context.transport.call(method, params, data, mutating=mutating)
    result = _base_result(method, reply)
    if not mutating:
        budget.ensure_fits(result)
        return _mark_write(result)
    # call_method is a declared external effect whatever it sends; "succeeded"
    # with "may have an effect" is the consistent, conservative pair.
    return _mark_write(_fit_write_result(result))


def _export_list(context: _Context) -> dict[str, Any]:
    inputs = context.operation_input
    unknown = set(inputs) - {"connection_ref", "method", "params", "max_pages", "start_page"}
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
    params: dict[str, Any] = {}
    for key, value in raw_params.items():
        key_text = str(key).strip()
        if key_text.lower() == "pagenumber":
            continue
        params[method.wire_name(key_text.lower()) or key_text] = _normalize_param(
            f"params.{key_text}", key_text, value
        )
    max_pages = _whole(
        "max_pages", inputs.get("max_pages"), default=EXPORT_MAX_PAGES, maximum=EXPORT_MAX_PAGES
    )
    page = _whole("start_page", inputs.get("start_page"), default=1, maximum=1000000)
    page_size = _page_size(method, params)
    # Records are buffered (one copy, at most 200 MB) and written once at the end:
    # streaming the upload while paging Rivile would tie the file to every
    # request, and a failure mid-way would lose the part already read.
    chunks: list[bytes] = []
    total = count = fetched = 0
    complete = False
    record_type: str | None = None
    while fetched < max_pages:
        try:
            reply = context.transport.call(
                method.method, {**params, page_param: page}, None, mutating=False
            )
        except DeadlineReached:
            if fetched == 0:
                raise
            break  # save what was read; resume_page continues
        page_chunks = [
            (json.dumps({"_table": tag, **record}, ensure_ascii=False) + "\n").encode()
            for tag in reply.parsed.order
            for record in reply.parsed.tables[tag]
        ]
        page_bytes = sum(len(chunk) for chunk in page_chunks)
        if total + page_bytes > MAX_EXPORT_BYTES:
            if fetched == 0:
                raise ExtensionError(
                    errors.RESPONSE_TOO_LARGE,
                    "One Rivile page exceeds the 200 MB export limit",
                    definitely_no_external_effect=True,
                    external_effect_status="failed",
                )
            break  # the file is full: save it; resume_page continues
        fetched += 1
        record_type = record_type or reply.parsed.record_type
        chunks.extend(page_chunks)
        total += page_bytes
        count += len(page_chunks)
        page += 1
        if reply.parsed.top_level_count < page_size:
            complete = True
            break
    handle = _write_artifact(
        context,
        chunks,
        binding_key=catalog.EXPORT_BINDING,
        content_type=EXPORT_CONTENT_TYPE,
        what="The export file",
    )
    return {
        "artifact_handle": handle,
        "filename": EXPORT_FILENAME,
        "content_type": EXPORT_CONTENT_TYPE,
        "record_count": count,
        "pages_fetched": fetched,
        "complete": complete,
        "resume_page": None if complete else page,
        "record_type": record_type,
    }


# ---------------------------------------------------------------------------
# Friendly operations


def _run_friendly(context: _Context, operation_id: str) -> dict[str, Any]:
    call = context.transport.call
    inputs = {k: v for k, v in context.operation_input.items() if k != "connection_ref"}
    friendly.validate(operation_id, inputs)
    progress = friendly.Progress()
    result: dict[str, Any]
    if operation_id == "find_or_create_client":
        result = friendly.find_or_create_client(call, inputs, progress)
    elif operation_id in {"create_sales_invoice", "create_purchase_invoice"}:
        op_type = (
            friendly.SALES_OP_TYPE
            if operation_id == "create_sales_invoice"
            else friendly.PURCHASE_OP_TYPE
        )
        result = friendly.create_invoice(call, inputs, progress, op_type=op_type)
    elif operation_id == "attach_file_to_document":
        result = _attach_file_to_document(context, inputs)
    elif operation_id == "download_document_file":
        result = _download_document_file(context, inputs)
    elif operation_id == "register_payment":
        result = friendly.register_payment(call, inputs)
    elif operation_id == "find_or_create_product":
        result = friendly.find_or_create_product(call, inputs, progress)
    elif operation_id == "stock_levels":
        result = friendly.stock_levels(call, inputs)
    elif operation_id == "product_prices":
        result = friendly.product_prices(call, inputs)
    elif operation_id == "changes_since":
        result = friendly.changes_since(call, inputs)
    else:  # client_debt
        result = friendly.client_debt(call, inputs)
    if operation_id in catalog.FRIENDLY_WRITE_IDS:
        if not budget.fits(result):
            result["record"] = None
        return _mark_write(result)
    budget.ensure_fits(result)
    return result


def _attach_file_to_document(context: _Context, inputs: Mapping[str, Any]) -> dict[str, Any]:
    code = friendly._text(inputs, "document_code", required=True, limit=12)
    params: dict[str, Any] = {
        "oper": "I",
        "kodas_po": code,
        "show_doc": 0,
        "overwrite": 1 if friendly._bool(inputs, "replace_existing", False) else 0,
    }
    ext = friendly._text(inputs, "file_extension", limit=10)
    if ext:
        params["ext"] = ext if ext.startswith(".") else f".{ext}"
    _attach_upload(context, params)
    reply = context.transport.call("EDIT_I06_DOC", params, None, mutating=True)
    records = reply.parsed.records
    _strip_documents(records)
    return {
        "document_code": code,
        "file_extension": params.get("ext", ""),
        "record": records[0] if records else None,
    }


def _download_document_file(context: _Context, inputs: Mapping[str, Any]) -> dict[str, Any]:
    code = friendly._text(inputs, "document_code", required=True, limit=12)
    reply = context.transport.call("GET_I06_DOC", {"kodas_po": code}, None, mutating=False)
    base = _base_result("GET_I06_DOC", reply)
    _save_download(context, base, binding_key=catalog.FRIENDLY_DOWNLOAD_BINDING)
    return {
        "document_code": code,
        "found": base["document_file"] is not None,
        "document_file": base["document_file"],
        "original_filename": base["original_filename"],
        "original_content_type": base["original_content_type"],
        "link": friendly._value(base["record"], "NUORODA"),
    }


# ---------------------------------------------------------------------------
# Files


def _write_artifact(
    context: _Context, chunks: list[bytes], *, binding_key: str, content_type: str, what: str
) -> str:
    try:
        written = context.artifact_writer(
            dict(context.payload),
            chunks,
            binding_key=binding_key,
            content_type=content_type,
            timeout_seconds=ARTIFACT_WRITE_TIMEOUT_SECONDS,
        )
    except ExtensionError:
        raise
    except Exception as exc:
        raise ExtensionError(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE,
            f"{what} could not be saved",
            definitely_no_external_effect=True,
            external_effect_status="failed",
        ) from exc
    handle = _text(written.get("artifact_handle")) if isinstance(written, Mapping) else ""
    if not handle:
        raise ExtensionError(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE,
            f"{what} could not be saved",
            definitely_no_external_effect=True,
            external_effect_status="failed",
        )
    return handle


def _upload_needs_content(params: Mapping[str, Any], *, has_file: bool) -> None:
    """Attaching (oper I) needs content; updating metadata (U) or deleting (D) does not."""
    oper = str(params.get("oper") or "I").upper()
    if oper == "I" and not has_file and not params.get("doc") and not params.get("nuoroda"):
        raise _invalid("Give document_file, doc (Base64) or nuoroda (a link)")
    if has_file and params.get("doc"):
        raise _invalid("Give either document_file or doc, not both")


def _attach_upload(context: _Context, params: dict[str, Any]) -> None:
    handle = _text(context.operation_input.get("document_file"))
    _upload_needs_content(params, has_file=bool(handle))
    if not handle:
        return
    try:
        body = context.artifact_reader(dict(context.payload), artifact_id=handle)
    except ExtensionError:
        raise
    except Exception as exc:
        raise ExtensionError(
            errors.ARTIFACT_INPUT_UNAVAILABLE,
            "document_file could not be read from Flow Steward storage",
            definitely_no_external_effect=True,
            external_effect_status="failed",
        ) from exc
    if not isinstance(body, (bytes, bytearray)) or not body:
        raise _invalid("document_file is empty")
    if len(body) > MAX_UPLOAD_BYTES:
        raise _invalid("document_file is larger than 15 MB")
    if not params.get("ext"):
        ext = _extension_of(context.payload, handle) or _sniff_extension(bytes(body[:16]))
        if not ext:
            raise _invalid("The file type of document_file is unknown; set ext, for example .pdf")
        params["ext"] = ext
    params["doc"] = base64.b64encode(bytes(body)).decode("ascii")


def _sniff_extension(head: bytes) -> str:
    for signature, ext in _SIGNATURES:
        if head.startswith(signature):
            return ext
    return ""


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


def _save_download(
    context: _Context, result: dict[str, Any], *, binding_key: str = catalog.DOWNLOAD_BINDING
) -> None:
    result["document_file"] = None
    result["original_filename"] = None
    result["original_content_type"] = None
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
    handle = _write_artifact(
        context,
        [body],
        binding_key=binding_key,
        content_type=DOWNLOAD_CONTENT_TYPE,
        what="The document file",
    )
    # The file travels as a handle; the Base64 copy is dropped from the output.
    _strip_documents(result["records"])
    result["document_file"] = handle
    result["original_filename"] = f"{code}{ext}"
    result["original_content_type"] = _CONTENT_TYPES.get(ext, DOWNLOAD_CONTENT_TYPE)


__all__ = [
    "DEFAULT_MAX_PAGES",
    "DOWNLOAD_CONTENT_TYPE",
    "EXPORT_CONTENT_TYPE",
    "MAX_PAGES",
    "PAGE_SIZE",
    "handle_runtime",
]
