"""Stable, safe error vocabulary for the Rivile GAMA extension.

Every failure maps to exactly one of these codes. No message carries the API
key. Rivile's own validation text (``errorMessage`` and ``dataError`` entries)
is business information an accountant needs to fix the document, so it is
passed on, bounded and stripped of control characters.
"""

from __future__ import annotations

from collections.abc import Mapping

INVALID_PAYLOAD = "invalid_payload"
INVALID_HOST_ENVELOPE = "invalid_host_envelope"
INVALID_CONNECTION = "invalid_connection"
UNSUPPORTED_OPERATION = "unsupported_operation"
AUTHENTICATION_FAILED = "authentication_failed"
RATE_LIMITED = "rate_limited"
UPSTREAM_VALIDATION_FAILED = "upstream_validation_failed"
UPSTREAM_FAILURE = "upstream_failure"
GATEWAY_UNAVAILABLE = "gateway_unavailable"
INVALID_XML_RESPONSE = "invalid_xml_response"
RESPONSE_TOO_LARGE = "response_too_large"
TIMEOUT = "timeout"
TIMEOUT_UNKNOWN = "timeout_unknown"
CONNECTION_FAILED = "connection_failed"
REDIRECT_REJECTED = "redirect_rejected"
BLOCKED_ADDRESS = "blocked_address"
ARTIFACT_INPUT_UNAVAILABLE = "artifact_input_unavailable"
ARTIFACT_OUTPUT_UNAVAILABLE = "artifact_output_unavailable"
INTERNAL_ERROR = "internal_error"

SAFE_ERROR_CODES: frozenset[str] = frozenset(
    {
        INVALID_PAYLOAD,
        INVALID_HOST_ENVELOPE,
        INVALID_CONNECTION,
        UNSUPPORTED_OPERATION,
        AUTHENTICATION_FAILED,
        RATE_LIMITED,
        UPSTREAM_VALIDATION_FAILED,
        UPSTREAM_FAILURE,
        GATEWAY_UNAVAILABLE,
        INVALID_XML_RESPONSE,
        RESPONSE_TOO_LARGE,
        TIMEOUT,
        TIMEOUT_UNKNOWN,
        CONNECTION_FAILED,
        REDIRECT_REJECTED,
        BLOCKED_ADDRESS,
        ARTIFACT_INPUT_UNAVAILABLE,
        ARTIFACT_OUTPUT_UNAVAILABLE,
        INTERNAL_ERROR,
    }
)

#: Codes that leave a write's outcome unknown to the caller.
AMBIGUOUS_ERROR_CODES: frozenset[str] = frozenset({TIMEOUT_UNKNOWN})

_MAX_MESSAGE = 1500


def safe_text(value: object, limit: int = 300) -> str:
    """Printable, single-line, bounded text from an upstream value."""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ExtensionError(Exception):
    """A failure already reduced to a safe code and a safe message."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure_class: str | None = None,
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        definitely_no_external_effect: bool | None = None,
        external_effect_status: str | None = None,
        http_status: int | None = None,
        data_errors: list[dict[str, str]] | None = None,
    ) -> None:
        safe_code = code if code in SAFE_ERROR_CODES else INTERNAL_ERROR
        bounded = safe_text(message, _MAX_MESSAGE)
        super().__init__(bounded)
        self.code = safe_code
        self.message = bounded
        self.failure_class = failure_class
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.definitely_no_external_effect = definitely_no_external_effect
        self.external_effect_status = external_effect_status
        self.http_status = http_status
        self.data_errors = list(data_errors or [])

    def retry_facts(self) -> dict[str, object]:
        """Closed, non-secret facts understood by Core."""
        facts: dict[str, object] = {}
        for key in (
            "failure_class",
            "retryable",
            "retry_after_seconds",
            "definitely_no_external_effect",
            "external_effect_status",
            "http_status",
        ):
            value = getattr(self, key)
            if value is not None:
                facts[key] = value
        if self.failure_class in {"provider", "transient"}:
            facts["provider_error_code"] = self.code
        return facts


def error_response(
    code: str,
    message: str,
    *,
    retry_facts: Mapping[str, object] | None = None,
    data_errors: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """The failing runtime envelope for one safe error.

    Each Rivile ``dataError`` becomes its own entry in ``errors`` so a workflow
    branch can show the operator exactly which field Rivile refused and why.
    """
    safe_code = code if code in SAFE_ERROR_CODES else INTERNAL_ERROR
    bounded = safe_text(message, _MAX_MESSAGE)
    entries: list[dict[str, object]] = [{"code": safe_code, "message": bounded}]
    for item in (data_errors or [])[:50]:
        detail = {"code": safe_code, "message": format_data_error(item)}
        for key in ("tag", "code"):
            if item.get(key):
                detail[f"rivile_{key}"] = safe_text(item[key], 64)
        entries.append(detail)
    response: dict[str, object] = {
        "ok": False,
        "result": {},
        "error_code": safe_code,
        "error": bounded,
        "errors": entries,
    }
    if safe_code in AMBIGUOUS_ERROR_CODES:
        response["external_effect_status"] = TIMEOUT_UNKNOWN
        response["definitely_no_external_effect"] = False
    if retry_facts:
        response.update(retry_facts)
    return response


def format_data_error(item: Mapping[str, str]) -> str:
    tag = safe_text(item.get("tag"), 64)
    code = safe_text(item.get("code"), 32)
    message = safe_text(item.get("message"), 300)
    head = " ".join(part for part in (tag, f"[{code}]" if code else "") if part)
    return f"{head}: {message}" if head else message


__all__ = [
    "AMBIGUOUS_ERROR_CODES",
    "SAFE_ERROR_CODES",
    "ExtensionError",
    "error_response",
    "format_data_error",
    "safe_text",
]
