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
import threading
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
_USER_AGENT = "FlowSteward-RivileGama/0.1"
#: RGI_RAK: "423: Įrašas užrakintas kito vartotojo [VARTOTOJAS]. Bandykite vėliau",
#: as the whole errorMessage of a refused request. Only that position counts: a
#: dataError's echoed data is the caller's own text.
_LOCKED_MESSAGE_RE = re.compile(r"^\s*423\s*:")
#: Methods whose answer carries a whole file as Base64.
_FILE_METHODS = frozenset({"GET_I06_DOC", "GET_I06_DOCS"})
#: Shortest window still worth starting a request in.
MIN_CALL_WINDOW_SECONDS = 15.0


class DeadlineReached(Exception):
    """The invocation's time budget leaves no room for another request."""


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
        #: Monotonic time after which no request may start (set by the operation).
        self.deadline: float | None = None
        self._call_timeout = timeout_seconds
        #: Message for an oversized read; set per call (file downloads differ).
        self._size_hint = ""

    def remaining(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - self.clock()

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
        *,
        mutating: bool,
    ) -> Reply:
        self._size_hint = (
            "The attached file is larger than this extension downloads (40 MB)"
            if method in _FILE_METHODS
            else ""
        )
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
            remaining = self.remaining()
            if remaining < MIN_CALL_WINDOW_SECONDS:
                raise DeadlineReached
            self._call_timeout = min(self._timeout_seconds, remaining)
            try:
                return self._send(encoded, mutating=mutating)
            except ExtensionError as exc:
                retry = exc.code in {errors.RATE_LIMITED, errors.RECORD_LOCKED}
                if not retry or attempt == len(BACKOFF_SECONDS):
                    raise
                wait = BACKOFF_SECONDS[attempt]
                if exc.retry_after_seconds:
                    wait = max(wait, min(float(exc.retry_after_seconds), 30.0))
                if wait > self.remaining() - MIN_CALL_WINDOW_SECONDS:
                    raise  # no time left to wait and try again
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
        status, raw, headers = self._bounded_open(request, mutating=mutating)
        raw = _redact_bytes(raw, self._connection)
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
            raise ExtensionError(
                exc.code,
                exc.message,
                failure_class="provider",
                retryable=False,
                definitely_no_external_effect=True,
                external_effect_status="failed",
                http_status=status,
            ) from exc
        _redact_parsed(parsed, self._connection)
        if status != 207 and (parsed.root != "RET_DOK" or parsed.error_message):
            # An error inside a success status: Rivile did not do what was asked, or (a
            # write) it is not known how far it got.
            raise _error_in_success(status, parsed, mutating=mutating)
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

    def _read_expiry(self) -> float | None:
        """When the exchange must stop: the request deadline plus its minimum window."""
        return None if self.deadline is None else self.deadline + MIN_CALL_WINDOW_SECONDS

    def _bounded_open(self, request: Request, *, mutating: bool) -> tuple[int, bytes, Any]:
        """``_open`` with the step deadline over the whole exchange.

        Name resolution (the SDK resolves before it opens, with no timeout of its own),
        the wait for headers and every read run in a daemon worker thread; the step waits
        for it only until the deadline plus its minimum window. A read still waiting then
        has no effect; a write's outcome is unknown and it is not retried.
        """
        expires = self._read_expiry()
        if expires is None:
            return self._open(request, mutating=mutating)
        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["value"] = self._open(request, mutating=mutating)
            except BaseException as exc:  # handed to the caller below
                outcome["error"] = exc

        worker = threading.Thread(target=work, name="rivile-gama-call", daemon=True)
        worker.start()
        while worker.is_alive():
            left = expires - self.clock()
            if left <= 0:
                break
            worker.join(min(0.25, left))
        if worker.is_alive():
            _late(mutating)
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def _open(self, request: Request, *, mutating: bool) -> tuple[int, bytes, Any]:
        try:
            self.attempted = True
            with self._opener(
                request,
                timeout_seconds=self._call_timeout,
                purpose="Rivile GAMA API",
            ) as response:
                status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
                headers = getattr(response, "headers", None)
                raw = read_bounded(
                    response,
                    MAX_RESPONSE_BYTES,
                    mutating=mutating,
                    hint=self._size_hint,
                    expires=self._read_expiry(),
                    clock=self.clock,
                )
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
        if status >= 300:
            # The SDK refuses redirects by raising HTTPError, so a 3xx normally
            # arrives in _http_error; this covers an opener that returns it.
            raise _status_error(
                status, raw[:MAX_ERROR_BODY_BYTES], headers, self._connection, mutating=mutating
            )
        return status, raw, headers


def read_bounded(
    response: Any,
    limit: int,
    *,
    mutating: bool = False,
    hint: str = "",
    expires: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """Read at most ``limit`` bytes before ``expires``.

    Before every read the socket's timeout is set to the time left, so one read cannot
    wait past ``expires``. An oversized or too slow answer to a write is an unknown
    outcome; to a read it is "out of time" (DeadlineReached) or "too large".
    """
    buffer = bytearray()
    sock = _socket_of(response)
    while True:
        if expires is not None:
            left = expires - clock()
            if left <= 0:
                _late(mutating)
            if sock is not None:
                try:
                    sock.settimeout(max(0.5, left))
                except OSError:  # pragma: no cover - a closed socket fails the read below
                    pass
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        if expires is not None and clock() > expires:
            _late(mutating)
        if len(buffer) > limit:
            raise ExtensionError(
                errors.TIMEOUT_UNKNOWN if mutating else errors.RESPONSE_TOO_LARGE,
                (
                    "Rivile's answer to the write was too large to read; check in Rivile "
                    "whether it was applied"
                    if mutating
                    else hint or "The Rivile response exceeds its safe limit; narrow it with fil"
                ),
            )
    return bytes(buffer)


def _late(mutating: bool) -> None:
    """The step's time ran out during an exchange."""
    if not mutating:
        raise DeadlineReached
    raise ExtensionError(
        errors.TIMEOUT_UNKNOWN,
        "Rivile's answer to the write did not arrive in time; check in Rivile whether it "
        "was applied before retrying",
        failure_class="transient",
        retryable=False,
        definitely_no_external_effect=False,
        external_effect_status="timeout_unknown",
    )


def _socket_of(response: Any) -> Any:
    for path in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock"), ("_sock",)):
        node: Any = response
        for name in path:
            node = getattr(node, name, None)
        if node is not None and hasattr(node, "settimeout"):
            return node
    return None


def _xml_escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _redact_bytes(raw: bytes, connection: Connection) -> bytes:
    """Remove the key from a body before anything parses or truncates it."""
    for secret in connection.secret_fragments():
        escaped = _xml_escaped(secret)
        for form in {secret, escaped, escaped.replace('"', "&quot;")}:
            encoded = form.encode("utf-8")
            if encoded and encoded in raw:
                raw = raw.replace(encoded, b"[redacted]")
    return raw


def _redact(text: str, connection: Connection) -> str:
    for secret in connection.secret_fragments():
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def _redact_parsed(parsed: ParsedResponse, connection: Connection) -> None:
    """The key removed from everything read: errors and every record's names and values.

    The raw bytes were cleaned before parsing, but XML character references (``&#65;``)
    are read only by the parser, so the parsed text is cleaned again.
    """
    parsed.error_message = _redact(parsed.error_message, connection)
    for item in parsed.data_errors:
        for key in ("tag", "code", "message", "data"):
            if isinstance(item.get(key), str):
                item[key] = _redact(item[key], connection)
    parsed.tables = {
        _redact(tag, connection): [_redact_value(row, connection) for row in rows]
        for tag, rows in parsed.tables.items()
    }
    parsed.order = [_redact(tag, connection) for tag in parsed.order]


def _redact_value(value: Any, connection: Connection) -> Any:
    if isinstance(value, str):
        return _redact(value, connection)
    if isinstance(value, dict):
        return {
            _redact(key, connection) if isinstance(key, str) else key: _redact_value(v, connection)
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v, connection) for v in value]
    return value


def _http_error(exc: HTTPError, connection: Connection, *, mutating: bool) -> ExtensionError:
    try:
        raw = exc.read(MAX_ERROR_BODY_BYTES)
    except Exception:  # pragma: no cover - upstream body is optional
        raw = b""
    return _status_error(int(exc.code), raw, exc.headers, connection, mutating=mutating)


def _parse_error_body(raw: bytes, connection: Connection) -> ParsedResponse:
    if not raw:
        return ParsedResponse()
    raw = _redact_bytes(raw, connection)
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
    return status == 400 and bool(_LOCKED_MESSAGE_RE.match(parsed.error_message or ""))


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
    if 300 <= status < 400:
        # Redirects are never followed. A write may still have been applied by
        # whatever answered with the redirect.
        return failure(
            errors.REDIRECT_REJECTED,
            f"Rivile redirected the request (HTTP {status}) and it was not followed",
            retryable=False,
            no_effect=not mutating,
        )
    if mutating:
        # Only 400 (refused data), 401/403 (key), 423 (record locked) and 429
        # (rate limit) are documented as "nothing was done". Any other answer to a
        # write — 5xx, 501, 520, 524 or anything unexpected — may follow an applied
        # write, so it must not be retried blindly.
        return failure(
            errors.TIMEOUT_UNKNOWN,
            f"Rivile did not confirm the write (HTTP {status}); check the document in Rivile "
            f"before retrying{detail}",
            retryable=False,
            no_effect=False,
        )
    if status in {500, 502, 503, 504, 408}:
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


def _error_in_success(status: int, parsed: ParsedResponse, *, mutating: bool) -> ExtensionError:
    detail = f": {parsed.error_message}" if parsed.error_message else ""
    if mutating:
        return ExtensionError(
            errors.TIMEOUT_UNKNOWN,
            f"Rivile answered the write with an error (HTTP {status}){detail}; it is not known "
            "whether anything was saved: check the document in Rivile before retrying",
            failure_class="provider",
            retryable=False,
            definitely_no_external_effect=False,
            external_effect_status="timeout_unknown",
            http_status=status,
            data_errors=parsed.data_errors,
        )
    return ExtensionError(
        errors.UPSTREAM_FAILURE,
        f"Rivile answered with an error (HTTP {status}){detail}",
        failure_class="provider",
        retryable=False,
        definitely_no_external_effect=True,
        external_effect_status="failed",
        http_status=status,
        data_errors=parsed.data_errors,
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
    "MIN_CALL_WINDOW_SECONDS",
    "DeadlineReached",
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
