"""Review 2026-11-17, item 11: the step deadline bounds the whole exchange.

A name resolution that hangs, headers that do not come and a read that stalls all
end at the deadline (plus its minimum window): a read with no effect, a write with
an unknown outcome. The limits are made small here so the test runs in a second.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from conftest import payload

from flowsteward_extension_sdk import PinnedPeerError
from runtime import operations, transport
from runtime.operations import handle_runtime

#: Step budget left after the margin, and the window after it, in seconds.
BUDGET = 1.0
WINDOW = 0.2
#: How long a stalled call blocks if nothing stops it.
STALL = 4.0
#: The longest the step may take: budget, window and a little scheduling slack.
LONGEST = BUDGET + WINDOW + 1.0


@pytest.fixture(autouse=True)
def _short_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operations, "STEP_TIMEOUT_SECONDS", operations.STEP_MARGIN_SECONDS + BUDGET)
    monkeypatch.setattr(transport, "MIN_CALL_WINDOW_SECONDS", WINDOW)


class _Stalled:
    """A response whose first byte comes, then the next read blocks."""

    status = 200
    headers = {"Content-Type": "application/xml"}

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self._sent = False

    def read(self, size: int = -1) -> bytes:
        if not self._sent:
            self._sent = True
            return b"<RET_DOK>"
        if self._release.is_set():
            return b""
        self._release.wait(STALL)
        self._release.set()  # one stall, then the body ends: the old code finishes too
        return b"</RET_DOK>"

    def __enter__(self) -> _Stalled:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _run(operation_id: str, inputs: dict[str, Any], opener: Any) -> tuple[dict[str, Any], float]:
    def factory(connection: Any) -> transport.RivileTransport:
        return transport.RivileTransport(connection, opener=opener, sleeper=lambda _s: None)

    started = time.monotonic()
    response = handle_runtime(payload(operation_id, inputs), transport_factory=factory)
    return response, time.monotonic() - started


def _hanging_dns(request: Any, *, timeout_seconds: float, purpose: str) -> Any:
    time.sleep(STALL)  # getaddrinfo has no timeout of its own
    raise PinnedPeerError("api.manorivile.lt cannot be resolved")


def _slow_headers(request: Any, *, timeout_seconds: float, purpose: str) -> Any:
    time.sleep(STALL)
    raise TimeoutError("no headers")


def _stalled_body(request: Any, *, timeout_seconds: float, purpose: str) -> Any:
    return _Stalled(threading.Event())


READ = ("get_n08_list", {"list": "a"})
WRITE = ("edit_n08", {"oper": "i", "data": {"N08": {"N08_KODAS_KS": "1", "N08_PAV": "UAB"}}})
STALLS = [
    pytest.param(_hanging_dns, id="hanging-name-resolution"),
    pytest.param(_slow_headers, id="slow-headers"),
    pytest.param(_stalled_body, id="stalled-body"),
]


@pytest.mark.parametrize("opener", STALLS)
def test_11_a_stalled_read_ends_at_the_deadline_with_no_effect(opener: Any) -> None:
    response, took = _run(*READ, opener)
    assert took < LONGEST, f"the read took {took:.1f} s"
    assert response["error_code"] == "timeout", response
    assert response["definitely_no_external_effect"] is True


@pytest.mark.parametrize("opener", STALLS)
def test_11_a_stalled_write_ends_at_the_deadline_as_unknown(opener: Any) -> None:
    response, took = _run(*WRITE, opener)
    assert took < LONGEST, f"the write took {took:.1f} s"
    assert response["error_code"] == "timeout_unknown", response
    assert response["definitely_no_external_effect"] is False
    assert response["external_effect_status"] == "timeout_unknown"
