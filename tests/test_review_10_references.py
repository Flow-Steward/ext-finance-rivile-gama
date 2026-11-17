"""Review 2026-11-17, item 10: the key, or its secret part, written in Rivile's XML as
character references (&#65; / &#x41;) is removed from every record read, so it reaches
neither the step output nor the export file."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import API_KEY, FakeOpener, payload, ret_dok
from test_operations import GrantWriter

from runtime.operations import handle_runtime

SECRET = API_KEY.partition(".")[2]


def _decimal(text: str) -> str:
    return "".join(f"&#{ord(c)};" for c in text)


def _hex(text: str) -> str:
    return "".join(f"&#x{ord(c):x};" for c in text)


def _record(value: str) -> str:
    return (
        f"<N08><N08_KODAS_KS>1</N08_KODAS_KS><N08_PAV>{value}</N08_PAV>"
        f"<N33><N33_PASTABOS>see {value}</N33_PASTABOS></N33></N08>"
    )


def _padded(text: str) -> str:
    return "".join(f"&#{ord(c):012d};" for c in text)


def _padded_hex(text: str) -> str:
    return "".join(f"&#x{ord(c):010x};" for c in text)


CASES = [
    pytest.param(_padded(API_KEY), id="key-decimal-leading-zeros"),
    pytest.param(_padded_hex(SECRET), id="secret-hex-leading-zeros"),
    pytest.param(_decimal(API_KEY), id="key-decimal"),
    pytest.param(_hex(API_KEY), id="key-hex"),
    pytest.param(_decimal(SECRET), id="secret-decimal"),
    pytest.param("x" + _hex(SECRET) + "y", id="secret-hex-inside"),
]


def _clean(text: str) -> bool:
    return API_KEY not in text and SECRET not in text


@pytest.mark.parametrize("value", CASES)
def test_10_a_read_never_returns_the_key_from_references(make_factory, value: Any) -> None:
    opener = FakeOpener((200, ret_dok(_record(value))))
    response = handle_runtime(
        payload("get_n08_list", {"list": "a"}), transport_factory=make_factory(opener)
    )
    assert response["ok"] is True, response
    assert _clean(json.dumps(response, ensure_ascii=False))
    assert "[redacted]" in response["result"]["records"][0]["N08_PAV"]


@pytest.mark.parametrize("value", CASES)
def test_10_the_export_file_never_holds_the_key_from_references(make_factory, value: Any) -> None:
    opener = FakeOpener((200, ret_dok(_record(value))))
    writer = GrantWriter("export_list")
    response = handle_runtime(
        payload("export_list", {"method": "GET_N08_LIST", "params": {"list": "a"}}),
        transport_factory=make_factory(opener),
        artifact_writer=writer,
    )
    assert response["ok"] is True, response
    saved = writer.calls[0]["body"].decode("utf-8")
    assert _clean(saved)
    for line in saved.splitlines():
        assert _clean(json.dumps(json.loads(line), ensure_ascii=False))
