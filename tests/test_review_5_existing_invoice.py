"""Review 2026-11-17, item 5: returning a found invoice requires one match with the
same date, currency and amount. Posting an existing draft requires a separate action."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeOpener, payload, ret_dok

from runtime.operations import handle_runtime

LINES = [
    {"product_code": "004", "quantity": 10, "unit_price": 1000, "unit": "VNT"},
    {"product_code": "005", "quantity": "10", "unit_price": "250", "discount": 5, "unit": "VNT"},
]
# 10 × 1000 + 10 × 250 × 0.95 = 12 375.00 without VAT
INPUTS = {
    "client_code": "121411842",
    "number": "DOK-007",
    "date": "2026-04-22",
    "department_code": "01",
    "lines": LINES,
    "post": True,
}


def _i06(code: str = "010R00000050", **fields: str) -> str:
    values = {
        "I06_DOK_NR": "DOK-007",
        "I06_DOK_DATA": "2026-04-22",
        "I06_KODAS_VL": "",
        "I06_SUMA": "12375.00",
        "I06_PERKELTA": "1",
        **fields,
    }
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in values.items())
    return f"<I06><I06_KODAS_PO>{code}</I06_KODAS_PO>{inner}</I06>"


def _run(
    make_factory,
    *answers: Any,
    inputs: dict[str, Any] = INPUTS,
    kind: str = "sales",
) -> tuple[dict, FakeOpener]:
    opener = FakeOpener(*answers)
    response = handle_runtime(
        payload(f"create_{kind}_invoice", inputs), transport_factory=make_factory(opener)
    )
    return response, opener


def _methods(opener: FakeOpener) -> list[str]:
    return [r["body"]["method"] for r in opener.requests]


@pytest.mark.parametrize("kind", ["sales", "purchase"])
def test_post_requires_confirmation_for_a_matching_existing_invoice(make_factory, kind) -> None:
    response, opener = _run(make_factory, (200, ret_dok(_i06())), (200, ret_dok(_i06())), kind=kind)
    assert response["error_code"] == "conflict", response
    assert "010R00000050" in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert _methods(opener) == ["GET_I06_LIST"]


def test_5_two_invoices_with_the_number_are_a_conflict(make_factory) -> None:
    found = ret_dok(_i06("010R00000050"), _i06("010R00000099"))
    response, opener = _run(make_factory, (200, found))
    assert response["error_code"] == "conflict", response
    assert "010R00000050" in response["error"] and "010R00000099" in response["error"]
    assert _methods(opener) == ["GET_I06_LIST"]


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"I06_DOK_DATA": "2025-04-22"}, id="another-year"),
        pytest.param({"I06_KODAS_VL": "USD"}, id="another-currency"),
        pytest.param({"I06_SUMA": "12000.00"}, id="another-amount"),
        pytest.param({"I06_DOK_DATA": ""}, id="no-date"),
        pytest.param({"I06_SUMA": ""}, id="no-amount"),
    ],
)
def test_5_an_invoice_that_differs_is_a_conflict_and_not_posted(make_factory, fields) -> None:
    response, opener = _run(make_factory, (200, ret_dok(_i06(**fields))))
    assert response["error_code"] == "conflict", response
    assert "010R00000050" in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert _methods(opener) == ["GET_I06_LIST"]


def test_5_other_lines_with_the_same_number_are_a_conflict(make_factory) -> None:
    changed = {**INPUTS, "lines": [{**LINES[0], "quantity": 20}, LINES[1]]}
    response, opener = _run(make_factory, (200, ret_dok(_i06())), inputs=changed)
    assert response["error_code"] == "conflict", response
    assert _methods(opener) == ["GET_I06_LIST"]


def test_5_an_amount_that_cannot_be_compared_is_a_conflict(make_factory) -> None:
    gross = {**INPUTS, "prices_include_vat": True}
    response, opener = _run(make_factory, (200, ret_dok(_i06())), inputs=gross)
    assert response["error_code"] == "conflict", response
    assert "cannot be compared" in response["error"]
    assert _methods(opener) == ["GET_I06_LIST"]
