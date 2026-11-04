"""The only way this extension reaches Rivile.

One ``POST https://api.manorivile.lt/client/v2`` per call, with the method,
params and data as JSON and the answer read as XML (Rivile's default, available
on every Manorivile-Gateway version). Requests go out through the public Flow
Steward extension SDK, which pins DNS, verifies the connected peer, refuses
redirects and enforces the timeout.

Rate limits (gidas.rivile.lt …/api_rest/limitai): 10 requests per second per
key, 30 at peak, reported in ``X-RateLimit-*`` headers. On HTTP 429 a client
must stop, wait at least one second and back off exponentially, or the key can
be blocked for longer. A request Rivile answers with 429 was not executed, and
neither was one refused because another user holds the record (423, RGI_RAK),
so both are retried here, within a bounded budget, for reads and writes alike.

Pacing is per process: one step paces its own requests. Steps running in
parallel against the same key share Rivile's budget without seeing each other.
"""

from __future__ import annotations

import http.client
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

from flowsteward_extension_sdk import PinnedPeerError, open_pinned_url, parse_retry_after

from . import errors, xml_response
from .connection import ENDPOINT, Connection
from .errors import ExtensionError
from .xml_response import ParsedResponse

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 60 * 1024 * 1024
#: Error bodies are read up to this size; a 400 for a large FULL document can
#: list many dataErrors.
MAX_ERROR_BODY_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
#: Rivile's nominal limit is 10 requests per second per key.
MIN_INTERVAL_SECONDS = 0.11
#: 429/423 retries: wait 1, 2, 4, 8 seconds (Rivile asks for at least 1 s, doubling).
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
_USER_AGENT = "FlowSteward-RivileGama/1.0"
_LOCKED_RE = re.compile(r"^\s*423\b|užrakintas", re.IGNORECASE)


@dataclass
class RateLimit:
    remaining: int | None = None
    burst_capacity: int | None = None
    replenish_rate: int | None = None
    requested_tokens: int | None = None

    def must_wait(self) -> bool:
        """Rivile's own guidance: keep remaining above burst - replenish - 1."""
        if self.remaining is None or self.burst_capacity is None or self.replenish_rate is None:
            return False
        return self.remaining <= self.burst_capacity - self.replenish_rate - 1


@dataclass
class Reply:
    status: int
    parsed: ParsedResponse
    rate_limit: RateLimit = field(default_factory=RateLimit)


def _int_header(headers: Any, name: str) -> int | None:
    try:
        raw = headers.get(name) if headers is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
    text = str(raw).strip() if raw is not None else ""
    return int(text) if re.fullmatch(r"[0-9]{1,9}", text) else None


def _rate_limit(headers: Any) -> RateLimit:
    return RateLimit(
        remaining=_int_header(headers, "X-RateLimit-Remaining"),
        burst_capacity=_int_header(headers, "X-RateLimit-Burst-Capacity"),
        replenish_rate=_int_header(headers, "X-RateLimit-Replenish-Rate"),
        requested_tokens=_int_header(headers, "X-RateLimit-Requested-Tokens"),
    )


class RivileTransport:
    """Sends Rivile requests for one connection and paces them."""

    def __init__(
        self,
        connection: Connection,
        *,
        opener: Callable[..., Any] = open_pinned_url,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._connection = connection
        self._opener = opener
        self._sleep = sleeper
        self.clock = clock
        self._timeout_seconds = timeout_seconds
        self._last_sent: float | None = None
        self.last_rate_limit = RateLimit()
        #: True once any request may have left this process.
        self.attempted = False

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
        *,
        mutating: bool,
    ) -> Reply:
        body: dict[str, Any] = {"method": method}
        if params:
            body["params"] = dict(params)
        if data:
            body["data"] = dict(data)
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BODY_BYTES:
            raise ExtensionError(
                errors.INVALID_PAYLOAD,
                "The request is larger than the 25 MB this extension sends to Rivile",
                definitely_no_external_effect=True,
            )
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            try:
                return self._send(encoded, mutating=mutating)
            except ExtensionError as exc:
                retry = exc.code in {errors.RATE_LIMITED, errors.RECORD_LOCKED}
                if not retry or attempt == len(BACKOFF_SECONDS):
                    raise
                wait = BACKOFF_SECONDS[attempt]
                if exc.retry_after_seconds:
                    wait = max(wait, min(float(exc.retry_after_seconds), 30.0))
                self._sleep(wait)
                # The backoff already spaced the requests; no extra pacing sleep.
                self._last_sent = None
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(self, encoded: bytes, *, mutating: bool) -> Reply:
        request = Request(
            ENDPOINT,
            method="POST",
            data=encoded,
            headers={
                "Accept": "application/xml",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": _USER_AGENT,
                **self._connection.headers(),
            },
        )
        self._pace()
        status, raw, headers = self._open(request, mutating=mutating)
        rate_limit = _rate_limit(headers)
        self.last_rate_limit = rate_limit
        try:
            parsed = xml_response.parse(raw, trim=self._connection.trim_values)
        except ExtensionError as exc:
            if mutating:
                raise ExtensionError(
                    errors.TIMEOUT_UNKNOWN,
                    "Rivile's answer to the write could not be read; check the document in Rivile",
                    failure_class="provider",
                    retryable=False,
                    definitely_no_external_effect=False,
                    external_effect_status="timeout_unknown",
                    http_status=status,
                ) from exc
            raise
        _redact_parsed(parsed, self._connection)
        return Reply(status=status, parsed=parsed, rate_limit=rate_limit)

    # -- pacing ------------------------------------------------------------

    def _pace(self) -> None:
        if self._last_sent is not None:
            wait = MIN_INTERVAL_SECONDS - (self.clock() - self._last_sent)
            if self.last_rate_limit.must_wait():
                wait = max(wait, 1.0)
            if wait > 0:
                self._sleep(wait)
        self._last_sent = self.clock()

    # -- network -----------------------------------------------------------

    def _open(self, request: Request, *, mutating: bool) -> tuple[int, bytes, Any]:
        try:
            self.attempted = True
            with self._opener(
                request,
                timeout_seconds=self._timeout_seconds,
                purpose="Rivile GAMA API",
            ) as response:
                status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
                headers = getattr(response, "headers", None)
                raw = read_bounded(response, MAX_RESPONSE_BYTES, mutating=mutating)
        except HTTPError as exc:
            raise _http_error(exc, self._connection, mutating=mutating) from exc
        except PinnedPeerError as exc:
            # Raised while resolving and pinning, before anything was sent.
            if "cannot be resolved" in str(exc):
                raise ExtensionError(
                    errors.CONNECTION_FAILED,
                    "api.manorivile.lt could not be resolved; check the network",
                    failure_class="transient",
                    retryable=True,
                    definitely_no_external_effect=True,
                    external_effect_status="failed",
                ) from exc
            raise ExtensionError(
                errors.BLOCKED_ADDRESS,
                "api.manorivile.lt is not reachable under the host network policy",
                definitely_no_external_effect=True,
                external_effect_status="failed",
            ) from exc
        except TimeoutError as exc:
            raise ExtensionError(
                errors.TIMEOUT_UNKNOWN if mutating else errors.TIMEOUT,
                "Rivile did not answer in time",
                **_transport_failure_facts(mutating=mutating),
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ExtensionError(
                errors.TIMEOUT_UNKNOWN if mutating else errors.CONNECTION_FAILED,
                "The connection to Rivile failed",
                **_transport_failure_facts(mutating=mutating),
            ) from exc
        if 300 <= status < 400:
            raise ExtensionError(
                errors.REDIRECT_REJECTED,
                "Rivile redirected the request and it was not followed",
                definitely_no_external_effect=not mutating,
                external_effect_status="failed" if not mutating else "timeout_unknown",
            )
        if status >= 400:
            raise _status_error(
                status, raw[:MAX_ERROR_BODY_BYTES], headers, self._connection, mutating=mutating
            )
        return status, raw, headers


def read_bounded(response: Any, limit: int, *, mutating: bool = False) -> bytes:
    """Read at most ``limit`` bytes; an oversized answer to a write is an unknown outcome."""
    buffer = bytearray()
    while True:
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise ExtensionError(
                errors.TIMEOUT_UNKNOWN if mutating else errors.RESPONSE_TOO_LARGE,
                "The Rivile response exceeds its safe limit; narrow it with fil",
            )
    return bytes(buffer)


def _redact(text: str, connection: Connection) -> str:
    for secret in connection.secret_fragments():
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def _redact_parsed(parsed: ParsedResponse, connection: Connection) -> None:
    parsed.error_message = _redact(parsed.error_message, connection)
    for item in parsed.data_errors:
        for key in ("tag", "code", "message", "data"):
            if isinstance(item.get(key), str):
                item[key] = _redact(item[key], connection)


def _http_error(exc: HTTPError, connection: Connection, *, mutating: bool) -> ExtensionError:
    try:
        raw = exc.read(MAX_ERROR_BODY_BYTES)
    except Exception:  # pragma: no cover - upstream body is optional
        raw = b""
    return _status_error(int(exc.code), raw, exc.headers, connection, mutating=mutating)


def _parse_error_body(raw: bytes, connection: Connection) -> ParsedResponse:
    if not raw:
        return ParsedResponse()
    try:
        parsed = xml_response.parse(raw)
    except ExtensionError:
        # Cut or malformed: recover what can be read rather than lose every dataError.
        parsed = xml_response.recover_errors(raw)
    _redact_parsed(parsed, connection)
    return parsed


def _is_locked(status: int, parsed: ParsedResponse) -> bool:
    if status == 423:
        return True
    texts = [parsed.error_message] + [
        f"{item.get('code', '')} {item.get('message', '')}" for item in parsed.data_errors
    ]
    return any(_LOCKED_RE.search(text or "") for text in texts)


def _status_error(
    status: int, raw: bytes, headers: Any, connection: Connection, *, mutating: bool
) -> ExtensionError:
    parsed = _parse_error_body(raw, connection)
    upstream = parsed.error_message
    detail = f": {upstream}" if upstream else ""
    retry_after = None
    try:
        retry_after = parse_retry_after(headers.get("Retry-After")) if headers is not None else None
    except (AttributeError, TypeError, ValueError):
        retry_after = None

    def failure(code: str, message: str, *, retryable: bool, no_effect: bool) -> ExtensionError:
        return ExtensionError(
            code,
            message,
            failure_class="provider",
            retryable=retryable,
            retry_after_seconds=retry_after,
            definitely_no_external_effect=no_effect,
            external_effect_status="failed" if no_effect else "timeout_unknown",
            http_status=status,
            data_errors=parsed.data_errors,
        )

    if status in {401, 403}:
        return failure(
            errors.AUTHENTICATION_FAILED,
            f"Rivile refused the API key (wrong, expired or not activated){detail}",
            retryable=False,
            no_effect=True,
        )
    if _is_locked(status, parsed):
        # RGI_RAK: the record is locked by another user; nothing was changed.
        return failure(
            errors.RECORD_LOCKED,
            f"Another Rivile user is editing this record; try again later{detail}",
            retryable=True,
            no_effect=True,
        )
    if status == 400:
        lines = [errors.format_data_error(item) for item in parsed.data_errors[:10]]
        joined = "; ".join(line for line in lines if line)
        message = f"Rivile rejected the request{detail}"
        if joined:
            message = f"{message} — {joined}"
        return failure(errors.UPSTREAM_VALIDATION_FAILED, message, retryable=False, no_effect=True)
    if status == 429:
        return failure(
            errors.RATE_LIMITED,
            "Rivile is rate limiting this API key (10 requests per second)",
            retryable=True,
            no_effect=True,
        )
    if status in {500, 502, 503, 504, 408}:
        if mutating:
            # The gateway or Rivile may have applied the write before failing to
            # answer ("No response received from client gateway" is ambiguous).
            return failure(
                errors.TIMEOUT_UNKNOWN,
                "Rivile did not confirm the write; check the document in Rivile before retrying"
                f"{detail}",
                retryable=False,
                no_effect=False,
            )
        if status in {502, 503}:
            return failure(
                errors.GATEWAY_UNAVAILABLE,
                "Manorivile-Gateway is not running or cannot reach the Rivile GAMA database"
                f"{detail}",
                retryable=True,
                no_effect=True,
            )
        return failure(
            errors.TIMEOUT if status in {504, 408} else errors.UPSTREAM_FAILURE,
            f"Rivile could not complete the request{detail}",
            retryable=True,
            no_effect=True,
        )
    return failure(
        errors.UPSTREAM_FAILURE,
        f"Rivile answered HTTP {status}{detail}",
        retryable=False,
        no_effect=True,
    )


def _transport_failure_facts(*, mutating: bool) -> dict[str, Any]:
    return {
        "failure_class": "transient",
        "retryable": not mutating,
        "definitely_no_external_effect": not mutating,
        "external_effect_status": "timeout_unknown" if mutating else "failed",
    }


__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ERROR_BODY_BYTES",
    "MAX_REQUEST_BODY_BYTES",
    "MAX_RESPONSE_BYTES",
    "MIN_INTERVAL_SECONDS",
    "RateLimit",
    "Reply",
    "RivileTransport",
    "read_bounded",
]
