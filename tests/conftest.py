from __future__ import annotations

import io
import json
import sys
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))
GENERATOR_ROOT = BUNDLE_ROOT / "tests" / "reference"
if str(GENERATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(GENERATOR_ROOT))

from runtime.transport import RivileTransport  # noqa: E402


def _core_secret_guard():
    """The host's own value_exposes_secret, loaded from the Flow Steward checkout.

    Loaded by file path (no Core package import chain). The gate and the
    Development test runner mount the checkout at /app; FLOW_STEWARD_ROOT
    overrides that.
    """
    import importlib.util
    import os

    candidates = [os.environ.get("FLOW_STEWARD_ROOT", ""), "/app", *map(str, BUNDLE_ROOT.parents)]
    for root in candidates:
        path = Path(root) / "core/application/extensions/secret_reflection.py" if root else None
        if path and path.is_file():
            spec = importlib.util.spec_from_file_location("fs_core_secret_reflection", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.value_exposes_secret
    return None


@pytest.fixture
def host_secret_guard():
    guard = _core_secret_guard()
    if guard is None:
        pytest.skip("Flow Steward checkout not found; set FLOW_STEWARD_ROOT")
    return guard


CONNECTION_ID = "conn-rivile-1"
API_KEY = "APIUSER.s3cr3t-fixture-key-0001"  # pragma: allowlist secret
ROOT_SCOPE: dict[str, Any] = {
    "group_id": None,
    "for_each_id": None,
    "step_id": "rivile_step",
    "step_execution_id": "5c1d2e3f-4a5b-4c6d-8e7f-000000000001",
}


def payload(
    operation_id: str,
    operation_input: dict[str, Any] | None = None,
    *,
    test_mode: bool = False,
    secrets: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    descriptors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract_version": "extension_host_v2",
        "mode": "action",
        "runtime_context": {
            **ROOT_SCOPE,
            "test_mode": test_mode,
            "external_effect_id": "6a3f1c2e-7b45-4d0a-9c11-2f8e5b7a0d31",
        },
        "action": {
            "action_id": operation_id,
            "input": {"connection_ref": CONNECTION_ID, **(operation_input or {})},
            "target": {
                "connection": {
                    "connection_id": CONNECTION_ID,
                    "connection_type_id": "rivile_gama",
                    "config": config if config is not None else {"trim_values": True},
                    "secrets": secrets if secrets is not None else {"api_key": API_KEY},
                }
            },
        },
    }
    if descriptors is not None:
        body["artifacts"] = descriptors
    return body


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.code = status
        self._buffer = body
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        self.headers = message

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._buffer = self._buffer, b""
            return chunk
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    """Records every request and answers from a queue of (status, xml) pairs."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any, *, timeout_seconds: float, purpose: str) -> Any:
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": body,
            }
        )
        if not self.answers:
            raise AssertionError("unexpected request")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, text, *rest = answer
        headers = rest[0] if rest else {}
        raw = text.encode("utf-8") if isinstance(text, str) else text
        if status >= 400:
            message = Message()
            for key, value in headers.items():
                message[key] = value
            raise HTTPError(request.full_url, status, "error", message, io.BytesIO(raw))
        return FakeResponse(status, raw, headers)


@pytest.fixture
def make_factory():
    def factory(opener: FakeOpener):
        sleeps: list[float] = []

        def build(connection):
            return RivileTransport(connection, opener=opener, sleeper=sleeps.append)

        build.sleeps = sleeps  # type: ignore[attr-defined]
        return build

    return factory


def ret_dok(*records: str, extra: str = "") -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8' ?><RET_DOK><id/><durationMs>12</durationMs>"
        + "".join(records)
        + extra
        + "</RET_DOK>"
    )


def n08(code: str, name: str = 'UAB "Rivilė"') -> str:
    return (
        f"<N08><Row_number>1</Row_number><N08_KODAS_KS>{code} </N08_KODAS_KS>"
        f"<N08_PAV>{name}</N08_PAV><N08_ADR></N08_ADR><N08_KODAS_DS/></N08>"
    )
