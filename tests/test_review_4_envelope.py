"""Review 2026-11-17, item 4: only a RET_DOK envelope without an error is an answer.

An empty body, another XML root (``<html/>``, the platform's ``<Map>`` error) and a
RET_DOK carrying ``errorMessage`` are errors whatever the HTTP status: a read stops
with nothing changed, a write that was sent is an unknown outcome, never a success.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeOpener, payload, ret_dok

from runtime.operations import handle_runtime

BAD = [
    pytest.param(b"", id="empty"),
    pytest.param(b"  \n", id="whitespace"),
    pytest.param(b"<html/>", id="html"),
    pytest.param(b"<root><N08><A>1</A></N08></root>", id="other-root"),
    pytest.param(b"<Map><message>failure</message></Map>", id="map-error"),
    pytest.param(ret_dok(extra="<errorMessage>Klaida</errorMessage>"), id="ret-dok-error"),
]


def _run(make_factory, body: Any, operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
    opener = FakeOpener((200, body))
    return handle_runtime(payload(operation, inputs), transport_factory=make_factory(opener))


@pytest.mark.parametrize("body", BAD)
def test_4_a_read_answered_without_a_clean_envelope_fails(make_factory, body: Any) -> None:
    response = _run(make_factory, body, "get_n08_list", {"list": "a"})
    assert response["ok"] is False, response
    assert response["definitely_no_external_effect"] is True


@pytest.mark.parametrize("body", BAD)
def test_4_a_write_answered_without_a_clean_envelope_is_unknown(make_factory, body: Any) -> None:
    data = {"N08": {"N08_KODAS_KS": "121411842", "N08_PAV": "UAB"}}
    response = _run(make_factory, body, "edit_n08", {"oper": "i", "data": data})
    assert response["ok"] is False, response
    assert response["external_effect_status"] == "timeout_unknown"
    assert response["definitely_no_external_effect"] is False


@pytest.mark.parametrize("body", BAD)
def test_4_the_connection_test_does_not_pass_on_a_foreign_answer(make_factory, body: Any) -> None:
    response = _run(make_factory, body, "test_connection", {})
    assert response["ok"] is False, response


@pytest.mark.parametrize("body", BAD)
def test_4_no_client_is_created_after_a_foreign_search_answer(make_factory, body: Any) -> None:
    opener = FakeOpener((200, body), (200, ret_dok("<N08><N08_KODAS_KS>9</N08_KODAS_KS></N08>")))
    response = handle_runtime(
        payload(
            "find_or_create_client",
            {"company_code": "121411842", "name": "UAB", "client_type": "customer"},
        ),
        transport_factory=make_factory(opener),
    )
    assert response["ok"] is False, response
    assert len(opener.requests) == 1


def test_4_an_empty_envelope_is_still_an_empty_read(make_factory) -> None:
    response = _run(make_factory, ret_dok(), "get_n08_list", {"list": "a"})
    assert response["ok"] is True and response["result"]["count"] == 0
