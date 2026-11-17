"""Review 2026-11-17, item 8: a second real payment of the same amount is never dropped.

With ``payment_reference`` (kept at the start of I13_PAV) a payment is a repeat only
when that reference is already on the document. Without it, a payment of the same
account and amount is a conflict unless ``matching_payment`` says what it is.
"""

from __future__ import annotations

from typing import Any

from conftest import FakeOpener, payload, ret_dok

from runtime.operations import handle_runtime

DOC = "010R00001041"
NEW_ROW = f"<I13><I13_KODAS_PO>{DOC}</I13_KODAS_PO><I13_EIL_NR>3</I13_EIL_NR></I13>"


def _document(*rows: str) -> str:
    return ret_dok(f"<I06><I06_KODAS_PO>{DOC}</I06_KODAS_PO>{''.join(rows)}</I06>")


def _row(line: int, amount: str = "100.00", name: str = "") -> str:
    pav = f"<I13_PAV>{name}</I13_PAV>" if name else ""
    return (
        f"<I13><I13_KODAS_SS>2703</I13_KODAS_SS><I13_SUMA>{amount}</I13_SUMA>"
        f"<I13_EIL_NR>{line}</I13_EIL_NR>{pav}</I13>"
    )


def _run(make_factory, opener: FakeOpener, **inputs: Any) -> dict[str, Any]:
    given = {"document_code": DOC, "money_account_code": "2703", "amount": "100", **inputs}
    return handle_runtime(
        payload("register_payment", given), transport_factory=make_factory(opener)
    )


def _methods(opener: FakeOpener) -> list[str]:
    return [r["body"]["method"] for r in opener.requests]


def test_8_a_same_amount_without_a_reference_is_a_conflict(make_factory) -> None:
    opener = FakeOpener((200, _document(_row(1))), (200, ret_dok(NEW_ROW)))
    response = _run(make_factory, opener)
    assert response["error_code"] == "conflict", response
    assert "matching_payment" in response["error"]
    assert _methods(opener) == ["GET_I06_LIST"]


def test_8_a_second_payment_with_its_own_reference_is_added(make_factory) -> None:
    opener = FakeOpener((200, _document(_row(1, name="BANK-1"))), (200, ret_dok(NEW_ROW)))
    response = _run(make_factory, opener, payment_reference="BANK-2")
    assert response["ok"] is True, response
    assert response["result"]["created"] is True
    sent = opener.requests[1]["body"]["data"]["I13"]
    assert sent["I13_PAV"] == "BANK-2"


def test_8_the_same_reference_again_is_the_same_payment(make_factory) -> None:
    opener = FakeOpener((200, _document(_row(1, name="BANK-1 Invoice 7"))))
    response = _run(make_factory, opener, payment_reference="BANK-1", description="Invoice 7")
    assert response["ok"] is True, response
    assert response["result"]["created"] is False and response["result"]["line_number"] == "1"
    assert _methods(opener) == ["GET_I06_LIST"]


def test_8_a_reference_used_for_another_amount_is_a_conflict(make_factory) -> None:
    opener = FakeOpener((200, _document(_row(1, amount="50.00", name="BANK-1"))))
    response = _run(make_factory, opener, payment_reference="BANK-1")
    assert response["error_code"] == "conflict", response
    assert _methods(opener) == ["GET_I06_LIST"]


def test_8_said_to_be_a_new_payment_it_is_added(make_factory) -> None:
    opener = FakeOpener((200, _document(_row(1))), (200, ret_dok(NEW_ROW)))
    response = _run(make_factory, opener, matching_payment="is_new")
    assert response["ok"] is True, response
    assert response["result"]["created"] is True
    assert _methods(opener) == ["GET_I06_LIST", "EDIT_I13"]


def test_8_said_to_be_a_repeat_it_is_returned(make_factory) -> None:
    opener = FakeOpener((200, _document(_row(2))))
    response = _run(make_factory, opener, matching_payment="is_repeat")
    assert response["ok"] is True, response
    assert response["result"]["created"] is False and response["result"]["line_number"] == "2"


def test_8_a_reference_and_a_description_must_fit_i13_pav(make_factory) -> None:
    opener = FakeOpener()
    response = _run(make_factory, opener, payment_reference="R" * 20, description="D" * 30)
    assert response["error_code"] == "invalid_payload", response
    assert opener.requests == []
