"""Friendly operations, with request/response shapes taken from the Rivile guide's examples."""

from __future__ import annotations

import base64
import json
from typing import Any

import yaml
from conftest import API_KEY, BUNDLE_ROOT, FakeOpener, payload, ret_dok
from test_operations import GrantWriter

from runtime import catalog
from runtime.operations import handle_runtime

N08_RIVILE = (
    "<N08><N08_KODAS_KS>121411842</N08_KODAS_KS><N08_RUSIS>3</N08_RUSIS>"
    "<N08_PVM_KODAS>LT214118411</N08_PVM_KODAS><N08_IM_KODAS>121411842</N08_IM_KODAS>"
    '<N08_PAV>UAB "Rivilė"</N08_PAV></N08>'
)
I06_CREATED = (
    "<I06><I06_KODAS_PO>010R00000050</I06_KODAS_PO><I06_OP_TIP>51</I06_OP_TIP>"
    "<I06_DOK_NR>DOK-007</I06_DOK_NR><I06_SUMA>2250.00</I06_SUMA><I06_SUMA_PVM>472.50</I06_SUMA_PVM></I06>"
)
LINES = [
    {"product_code": "004", "quantity": 10, "unit_price": 1000, "vat_rate": 21, "unit": "VNT"},
    {
        "product_code": "005",
        "quantity": "10",
        "unit_price": "250",
        "discount": 5,
        "line_type": "service",
        "unit": "VNT",
    },
]


def run(make_factory, opener: FakeOpener, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    response = handle_runtime(body, transport_factory=make_factory(opener), **kwargs)
    assert API_KEY not in json.dumps(response, ensure_ascii=False)
    return response


def bodies(opener: FakeOpener) -> list[dict[str, Any]]:
    return [r["body"] for r in opener.requests]


# --------------------------------------------------------------------------- clients


def test_existing_client_is_found_without_writing(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(N08_RIVILE)))
    response = run(
        make_factory, opener, payload("find_or_create_client", {"company_code": "121411842"})
    )
    result = response["result"]
    assert result["client_code"] == "121411842" and result["created"] is False
    assert result["matched_by"] == "company_code"
    assert bodies(opener) == [
        {"method": "GET_N08_LIST", "params": {"list": "H", "fil": "n08_im_kodas='121411842'"}}
    ]
    assert response["external_effect_status"] == "succeeded"


def test_missing_client_is_created_with_documented_fields(make_factory) -> None:
    opener = FakeOpener((200, ret_dok()), (200, ret_dok()), (200, ret_dok(N08_RIVILE)))
    response = run(
        make_factory,
        opener,
        payload(
            "find_or_create_client",
            {
                "company_code": "121411842",
                "vat_code": "LT214118411",
                "name": 'UAB "Rivilė"',
                "client_type": "both",
            },
        ),
    )
    sent = bodies(opener)
    assert [b["method"] for b in sent] == ["GET_N08_LIST", "GET_N08_LIST", "EDIT_N08"]
    assert sent[1]["params"]["fil"] == "n08_pvm_kodas='LT214118411'"
    assert sent[2] == {
        "method": "EDIT_N08",
        "params": {"oper": "I"},
        "data": {
            "N08": {
                "N08_PAV": 'UAB "Rivilė"',
                "N08_RUSIS": 3,
                "N08_TIPAS": 1,
                "N08_KODAS_DS": "PT001",
                "N08_KODAS_XS_P": "PVM",
                "N08_KODAS_XS_T": "PVM",
                "N08_IM_KODAS": "121411842",
                "N08_PVM_KODAS": "LT214118411",
            }
        },
    }
    assert response["result"]["created"] is True
    assert response["result"]["client_code"] == "121411842"


def test_lookup_values_outside_the_code_alphabet_are_refused(make_factory) -> None:
    for value in ("O'Neil", "LT\u02bc1", "LT\uff071", "Ąžuolas"):
        opener = FakeOpener()
        response = run(
            make_factory, opener, payload("find_or_create_client", {"company_code": value})
        )
        assert response["error_code"] == "invalid_payload", value
        assert opener.requests == []


def test_several_matches_are_refused_without_writing(make_factory) -> None:
    two = ret_dok(
        N08_RIVILE, N08_RIVILE.replace("121411842</N08_KODAS_KS>", "999</N08_KODAS_KS>", 1)
    )
    opener = FakeOpener((200, two))
    response = run(
        make_factory,
        opener,
        payload("find_or_create_client", {"company_code": "121411842", "name": "X"}),
    )
    assert response["error_code"] == "invalid_payload" and "give client_code" in response["error"]
    assert [b["method"] for b in bodies(opener)] == ["GET_N08_LIST"]


def test_conflicting_identifiers_are_refused_without_writing(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(N08_RIVILE)))
    response = run(
        make_factory,
        opener,
        payload("find_or_create_client", {"company_code": "121411842", "vat_code": "LT999999999"}),
    )
    assert response["error_code"] == "invalid_payload"
    assert "LT214118411" in response["error"]
    assert len(opener.requests) == 1


# --------------------------------------------------------------------------- invoices


def test_sales_invoice_maps_header_lines_terms_and_posts(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok(N08_RIVILE)),
        (200, ret_dok()),
        (200, ret_dok(I06_CREATED)),
        (200, ret_dok(I06_CREATED)),
    )
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {
                "company_code": "121411842",
                "number": "DOK-007",
                "date": "2020-04-22",
                "due_date": "2020-05-02",
                "department_code": "01",
                "lines": LINES,
                "post": True,
            },
        ),
    )
    sent = bodies(opener)
    assert [b["method"] for b in sent] == [
        "GET_N08_LIST",
        "GET_I06_LIST",
        "EDIT_I06_FULL",
        "EDIT_I06",
    ]
    assert sent[1]["params"]["fil"] == (
        "i06_dok_nr='DOK-007' and i06_kodas_ks='121411842' and i06_op_tip=51"
    )
    sent = [sent[0], *sent[2:]]
    invoice = sent[1]
    assert invoice["params"] == {"errorAction": "ROLLBACK"}
    header = invoice["data"]["I06"]
    assert header["I06_OP_TIP"] == 51 and header["I06_KODAS_KS"] == "121411842"
    assert header["I06_DOK_NR"] == "DOK-007" and header["I06_OP_DATA"] == "2020-04-22"
    assert header["I08"] == [{"I08_EIL_NR": 1, "I08_MOK_P": 100, "I08_MOK_D": 10}]
    assert header["I07"] == [
        {
            "I07_TIPAS": 1,
            "I07_KODAS": "004",
            "I07_KODAS_IS": "01",
            "I07_KODAS_US": "VNT",
            "I07_KODAS_US_P": "VNT",
            "I07_KODAS_US_A": "VNT",
            "KIEKIS_U": 10,
            "I07_KAINA_BE": 1000,
            "I07_MOKESTIS": 1,
            "I07_MOKESTIS_P": 21,
        },
        {
            "I07_TIPAS": 2,
            "I07_KODAS": "005",
            "I07_KODAS_IS": "01",
            "I07_KODAS_US": "VNT",
            "I07_KODAS_US_P": "VNT",
            "I07_KODAS_US_A": "VNT",
            "KIEKIS_U": 10,
            "I07_KAINA_BE": 250,
            "I07_NUOLAIDA": 5,
        },
    ]
    assert sent[2] == {
        "method": "EDIT_I06",
        "params": {"oper": "P"},
        "data": {"I06": {"I06_KODAS_PO": "010R00000050"}},
    }
    result = response["result"]
    assert result["document_code"] == "010R00000050"
    assert (result["total_net"], result["total_vat"], result["total"]) == (2250.0, 472.5, 2722.5)
    assert result["posted"] is True and result["client_created"] is False
    assert result["created"] is True
    assert response["external_effect_status"] == "succeeded"


def test_rerun_with_a_number_returns_the_existing_invoice(make_factory) -> None:
    existing = I06_CREATED.replace("</I06>", "<I06_PERKELTA>2</I06_PERKELTA></I06>")
    opener = FakeOpener((200, ret_dok(existing)))
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {
                "client_code": "121411842",
                "number": "DOK-007",
                "department_code": "01",
                "lines": LINES,
            },
        ),
    )
    result = response["result"]
    assert [b["method"] for b in bodies(opener)] == ["GET_I06_LIST"]
    assert result["created"] is False and result["document_code"] == "010R00000050"
    assert result["posted"] is True


def test_posting_timeout_is_reported_as_unknown(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(I06_CREATED)), TimeoutError())
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {"client_code": "1", "department_code": "01", "lines": LINES[:1], "post": True},
        ),
    )
    assert response["ok"] is True
    assert response["result"]["posted"] is None and response["result"]["post_error"]


def test_no_document_code_means_no_posting(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<I06><I06_DOK_NR>X</I06_DOK_NR></I06>")))
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {"client_code": "1", "department_code": "01", "lines": LINES[:1], "post": True},
        ),
    )
    assert len(opener.requests) == 1
    assert response["result"]["posted"] is False


def test_running_out_of_time_after_creating_the_client_reports_it(make_factory) -> None:
    from runtime.transport import DeadlineReached

    class Flaky(FakeOpener):
        def __call__(self, request, *, timeout_seconds, purpose):
            if json.loads(request.data)["method"] == "EDIT_I06_FULL":
                raise AssertionError("must not be sent")
            return super().__call__(request, timeout_seconds=timeout_seconds, purpose=purpose)

    opener = Flaky((200, ret_dok()), (200, ret_dok(N08_RIVILE)))

    def factory(connection):
        transport = make_factory(opener)(connection)
        original = transport.call

        def call(method, params, data, *, mutating):
            if method == "EDIT_I06_FULL":
                raise DeadlineReached
            return original(method, params, data, mutating=mutating)

        transport.call = call
        return transport

    response = handle_runtime(
        payload(
            "create_sales_invoice",
            {
                "company_code": "121411842",
                "name": "UAB X",
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
        transport_factory=factory,
    )
    assert response["ok"] is False
    assert "client 121411842 created" in response["error"]
    assert response["definitely_no_external_effect"] is False
    assert response["external_effect_status"] == "timeout_unknown"


def test_client_created_without_an_echoed_code_is_read_back(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok()),
        (200, ret_dok("<N08><N08_PAV>X</N08_PAV></N08>")),
        (200, ret_dok(N08_RIVILE)),
    )
    response = run(
        make_factory,
        opener,
        payload("find_or_create_client", {"company_code": "121411842", "name": "X"}),
    )
    assert (
        response["result"]["created"] is True and response["result"]["client_code"] == "121411842"
    )
    assert response["external_effect_status"] == "succeeded"


def test_purchase_invoice_uses_purchase_type_and_creates_a_supplier(make_factory) -> None:
    opener = FakeOpener((200, ret_dok()), (200, ret_dok(N08_RIVILE)), (200, ret_dok(I06_CREATED)))
    run(
        make_factory,
        opener,
        payload(
            "create_purchase_invoice",
            {
                "company_code": "121411842",
                "name": "UAB Tiekėjas",
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
    )
    sent = bodies(opener)
    assert sent[1]["data"]["N08"]["N08_RUSIS"] == 2
    assert (
        "N08_KODAS_XS_T" in sent[1]["data"]["N08"]
        and "N08_KODAS_XS_P" not in sent[1]["data"]["N08"]
    )
    assert sent[2]["data"]["I06"]["I06_OP_TIP"] == 1


def test_currency_and_gross_prices(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(I06_CREATED)))
    run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {
                "client_code": "121411842",
                "currency": "USD",
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
    )
    header = bodies(opener)[0]["data"]["I06"]
    # A currency code alone (e.g. the base currency) keeps the base price fields.
    assert header["I06_KODAS_VL"] == "USD" and header["I07"][0]["I07_KAINA_BE"] == 1000
    assert "I06_VAL_POZ" not in header and "I07_VAL_KAINA" not in header["I07"][0]
    opener = FakeOpener((200, ret_dok(I06_CREATED)))
    run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {
                "client_code": "1",
                "currency": "USD",
                "foreign_currency": True,
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
    )
    header = bodies(opener)[0]["data"]["I06"]
    assert header["I06_VAL_POZ"] == 1 and header["I07"][0]["I07_VAL_KAINA"] == 1000
    assert "I07_KAINA_BE" not in header["I07"][0]
    refused = run(
        make_factory,
        FakeOpener(),
        payload(
            "create_sales_invoice",
            {
                "client_code": "1",
                "currency": "USD",
                "foreign_currency": True,
                "prices_include_vat": True,
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
    )
    assert refused["error_code"] == "invalid_payload"
    opener = FakeOpener((200, ret_dok(I06_CREATED)))
    run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {
                "client_code": "1",
                "prices_include_vat": True,
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
    )
    header = bodies(opener)[0]["data"]["I06"]
    assert header["I06_PVM_TIP"] == 1 and header["I07"][0]["I07_KAINA_SU"] == 1000


def test_invoice_failure_after_creating_the_client_says_what_exists(make_factory) -> None:
    refused = (
        "<RET_DOK><errorMessage>Nepavyko</errorMessage><errors><error><dataErrors><dataError>"
        "<tag>I07_KODAS</tag><code>19014</code><message>Nerastas prekės/paslaugos kodas</message>"
        "</dataError></dataErrors></error></errors></RET_DOK>"
    )
    opener = FakeOpener((200, ret_dok()), (200, ret_dok(N08_RIVILE)), (400, refused))
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {
                "company_code": "121411842",
                "name": "UAB X",
                "department_code": "01",
                "lines": LINES[:1],
            },
        ),
    )
    assert response["ok"] is False
    assert "client 121411842 created" in response["error"]
    assert "Nerastas prekės/paslaugos kodas" in response["error"]
    assert response["definitely_no_external_effect"] is False
    assert response["external_effect_status"] == "timeout_unknown"
    assert response["retryable"] is False
    assert len(opener.requests) == 3  # nothing resent


def test_invoice_refused_before_any_write_is_a_clean_failure(make_factory) -> None:
    opener = FakeOpener((400, "<RET_DOK><errorMessage>Nepavyko</errorMessage></RET_DOK>"))
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {"client_code": "1", "department_code": "01", "lines": LINES[:1]},
        ),
    )
    assert response["error_code"] == "upstream_validation_failed"
    assert response["definitely_no_external_effect"] is True


def test_failed_posting_keeps_the_invoice_and_reports_it(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok(I06_CREATED)),
        (400, "<RET_DOK><errorMessage>Negalima perkelti</errorMessage></RET_DOK>"),
    )
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {"client_code": "1", "department_code": "01", "lines": LINES[:1], "post": True},
        ),
    )
    result = response["result"]
    assert response["ok"] is True and result["document_code"] == "010R00000050"
    assert result["posted"] is False and "Negalima perkelti" in result["post_error"]


def test_invoice_inputs_are_checked_before_anything_is_sent(make_factory) -> None:
    for inputs in (
        {"client_code": "1", "lines": LINES[:1]},  # no department
        {"client_code": "1", "department_code": "01", "lines": []},
        {"department_code": "01", "lines": LINES[:1]},  # no client
        {
            "client_code": "1",
            "department_code": "01",
            "lines": LINES[:1],
            "date": "2020-05-02",
            "due_date": "2020-05-01",
        },
        {
            "client_code": "1",
            "department_code": "01",
            "lines": [{"product_code": "1", "quantity": 1, "unit_price": 1, "colour": "red"}],
        },
    ):
        opener = FakeOpener()
        response = run(make_factory, opener, payload("create_sales_invoice", inputs))
        assert response["error_code"] == "invalid_payload", inputs
        assert response["definitely_no_external_effect"] is True
        assert opener.requests == []


# --------------------------------------------------------------------------- files, payments, products


def test_attach_file_to_document(make_factory) -> None:
    pdf = b"%PDF-1.4 invoice"
    opener = FakeOpener((200, ret_dok("<I06><KODAS_PO>010R00000170</KODAS_PO></I06>")))
    response = run(
        make_factory,
        opener,
        payload(
            "attach_file_to_document",
            {"document_code": "010R00000170", "document_file": "artifact://pdf"},
        ),
        artifact_reader=lambda _p, *, artifact_id: pdf,
    )
    params = bodies(opener)[0]["params"]
    assert bodies(opener)[0]["method"] == "EDIT_I06_DOC"
    assert params["kodas_po"] == "010R00000170" and base64.b64decode(params["doc"]) == pdf
    assert (params["ext"], params["show_doc"], params["overwrite"], params["oper"]) == (
        ".pdf",
        0,
        0,
        "I",
    )
    assert response["result"]["file_extension"] == ".pdf"


def test_download_document_file_under_its_grant(make_factory) -> None:
    pdf = b"%PDF-1.4 stored"
    xml = ret_dok(
        f"<I06><KODAS_PO>010000001252</KODAS_PO><DOC>{base64.b64encode(pdf).decode()}</DOC><EXT>.PDF</EXT></I06>"
    )
    writer = GrantWriter("download_document_file")
    response = run(
        make_factory,
        FakeOpener((200, xml)),
        payload("download_document_file", {"document_code": "010000001252"}),
        artifact_writer=writer,
    )
    result = response["result"]
    assert writer.calls[0]["body"] == pdf
    assert result["found"] is True and result["original_filename"] == "010000001252.pdf"


def test_register_payment_as_documented(make_factory) -> None:
    opener = FakeOpener(
        (
            200,
            ret_dok(
                "<I13><I13_KODAS_PO>010R00001041</I13_KODAS_PO><I13_EIL_NR>1</I13_EIL_NR></I13>"
            ),
        )
    )
    response = run(
        make_factory,
        opener,
        payload(
            "register_payment",
            {"document_code": "010R00001041", "money_account_code": "2703", "amount": 19.62},
        ),
    )
    assert bodies(opener)[0] == {
        "method": "EDIT_I13",
        "params": {"oper": "I"},
        "data": {
            "I13": {"I13_KODAS_PO": "010R00001041", "I13_KODAS_SS": "2703", "I13_SUMA": 19.62}
        },
    }
    assert response["result"]["line_number"] == "1"
    assert response["external_effect_status"] == "succeeded"


def test_product_found_by_alternative_barcode(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok()), (200, ret_dok("<N40><N40_KODAS_PS>PASLX</N40_KODAS_PS></N40>"))
    )
    response = run(
        make_factory, opener, payload("find_or_create_product", {"barcode": "010000010000"})
    )
    assert [b["method"] for b in bodies(opener)] == ["GET_N37_LIST", "GET_N40_LIST"]
    assert response["result"] == {
        "product_code": "PASLX",
        "created": False,
        "matched_by": "barcode",
        "notes": [],
        "record": {"N40_KODAS_PS": "PASLX"},
        "external_effect_status": "succeeded",
        "definitely_no_external_effect": False,
    }


def test_missing_product_is_created_with_unit_and_barcode(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok()),
        (200, ret_dok()),
        (200, ret_dok()),
        (200, ret_dok("<N17><N17_KODAS_PS>PASLX</N17_KODAS_PS></N17>")),
    )
    response = run(
        make_factory,
        opener,
        payload(
            "find_or_create_product",
            {
                "product_code": "PASLX",
                "barcode": "010000010000",
                "name": "Paslauga X",
                "unit": "KG",
                "product_type": "service",
            },
        ),
    )
    create = bodies(opener)[3]
    assert create == {
        "method": "EDIT_N17_FULL",
        "params": {"errorAction": "ROLLBACK"},
        "data": {
            "N17": {
                "N17_KODAS_PS": "PASLX",
                "N17_TIPAS": 2,
                "N17_PAV": "Paslauga X",
                "N17_KODAS_US": "KG",
                "N17_KODAS_DS": "PR001",
                "N40": {"N40_KODAS_US": "KG", "N40_BAR_KODAS": "010000010000"},
            }
        },
    }
    assert response["result"]["created"] is True


def test_product_found_by_code_but_barcode_of_another_product(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok("<N17><N17_KODAS_PS>A1</N17_KODAS_PS></N17>")),
        (200, ret_dok("<N37><N37_KODAS_PS>B2</N37_KODAS_PS></N37>")),
    )
    response = run(
        make_factory,
        opener,
        payload("find_or_create_product", {"product_code": "A1", "barcode": "4770000000001"}),
    )
    assert (
        response["error_code"] == "invalid_payload" and "belongs to product B2" in response["error"]
    )


def test_product_found_by_code_with_an_unregistered_barcode_notes_it(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok("<N17><N17_KODAS_PS>A1</N17_KODAS_PS></N17>")),
        (200, ret_dok()),
        (200, ret_dok()),
    )
    response = run(
        make_factory,
        opener,
        payload("find_or_create_product", {"product_code": "A1", "barcode": "4770000000001"}),
    )
    assert response["result"]["created"] is False
    assert "not registered" in response["result"]["notes"][0]


def test_amounts_are_rounded_and_bounded(make_factory) -> None:
    opener = FakeOpener(
        (200, ret_dok("<I13><I13_EIL_NR>1</I13_EIL_NR><I13_SUMA_VAL>.00</I13_SUMA_VAL></I13>"))
    )
    run(
        make_factory,
        opener,
        payload(
            "register_payment",
            {"document_code": "1", "money_account_code": "2703", "amount": "19.625"},
        ),
    )
    assert bodies(opener)[0]["data"]["I13"]["I13_SUMA"] == 19.63
    for amount in (-5, 0, "abc"):
        bad = run(
            make_factory,
            FakeOpener(),
            payload(
                "register_payment",
                {"document_code": "1", "money_account_code": "2703", "amount": amount},
            ),
        )
        assert bad["error_code"] == "invalid_payload", amount
    line = {"product_code": "A", "quantity": 1, "unit": "VNT", "unit_price": 1, "discount": 150}
    bad = run(
        make_factory,
        FakeOpener(),
        payload(
            "create_sales_invoice", {"client_code": "1", "department_code": "01", "lines": [line]}
        ),
    )
    assert bad["error_code"] == "invalid_payload"


# --------------------------------------------------------------------------- reads


def test_stock_levels_as_decimal_quantities(make_factory) -> None:
    xml = ret_dok(
        "<I17><I17_KODAS_PS>PREKE01</I17_KODAS_PS><I17_KODAS_US>KG</I17_KODAS_US><I17_KODAS_IS>01</I17_KODAS_IS>"
        "<I17_FRAKCIJA>1000</I17_FRAKCIJA><I17_KIEKIS>5000</I17_KIEKIS><des_likutis_us>5.0000</des_likutis_us></I17>"
        "<I17><I17_KODAS_PS>PREKE02</I17_KODAS_PS><I17_KODAS_IS>01</I17_KODAS_IS>"
        "<I17_FRAKCIJA>10</I17_FRAKCIJA><I17_KIEKIS>25</I17_KIEKIS><I17_REZERVAS>10</I17_REZERVAS></I17>"
    )
    opener = FakeOpener((200, xml))
    response = run(make_factory, opener, payload("stock_levels", {"warehouse_code": "01"}))
    assert bodies(opener)[0]["params"] == {
        "group": "I17_KODAS_IS",
        "fil": "i17_kodas_is='01'",
        "pagenumber": 1,
    }
    items = response["result"]["items"]
    assert (
        items[0]["quantity"] == 5.0 and items[1]["quantity"] == 2.5 and items[1]["reserved"] == 1.0
    )
    assert response["result"]["has_more"] is False


def test_product_prices_from_the_documented_example(make_factory) -> None:
    xml = ret_dok(
        "<KAINOS><Row_number>1</Row_number><kodas_ps>PR001 </kodas_ps><kodas_us>PAK </kodas_us>"
        "<kodas_is>999999999999</kodas_is><bar_kodas>000000000012</bar_kodas><kaina>1.0000</kaina>"
        "<kain_tipas>KAIN_I33 </kain_tipas><prioritetas>4</prioritetas></KAINOS>"
    )
    opener = FakeOpener((200, xml))
    response = run(make_factory, opener, payload("product_prices", {"product_code": "PR001"}))
    assert bodies(opener)[0]["params"] == {"kodas_ps": "PR001"}
    assert response["result"]["items"] == [
        {
            "product_code": "PR001",
            "unit": "PAK",
            "warehouse_code": "999999999999",
            "barcode": "000000000012",
            "price": 1.0,
            "price_type": "KAIN_I33",
            "priority": "4",
        }
    ]


def test_changes_since_uses_the_time_methods_or_r_date(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(N08_RIVILE)))
    response = run(
        make_factory, opener, payload("changes_since", {"entity": "clients", "since": "2022-01-31"})
    )
    assert bodies(opener)[0] == {
        "method": "GET_CLIENTS_TIME",
        "params": {"list": "H", "lastDate": "2022-01-31", "pagenumber": 1},
    }
    assert response["result"]["count"] == 1
    opener = FakeOpener((200, ret_dok()))
    run(
        make_factory,
        opener,
        payload("changes_since", {"entity": "documents", "since": "2022-01-31T08:00"}),
    )
    assert bodies(opener)[0]["params"]["fil"] == "i06_r_date>='2022-01-31 08:00:00'"
    opener = FakeOpener((200, ret_dok()))
    run(
        make_factory,
        opener,
        payload("changes_since", {"entity": "prices", "since": "2022-02-15T16:29"}),
    )
    # The guide's datetime form, and list A: prices of every product, not only barcoded ones.
    assert bodies(opener)[0]["params"] == {
        "list": "A",
        "lastDate": "2022-02-15T16:29:00",
        "pagenumber": 1,
    }
    bad = run(
        make_factory,
        FakeOpener(),
        payload("changes_since", {"entity": "clients", "since": "yesterday"}),
    )
    assert bad["error_code"] == "invalid_payload"


def test_client_debt_totals(make_factory) -> None:
    xml = ret_dok(
        # The guide's own response: lower-case tags, padded code.
        "<T03><Row_number>1</Row_number><t03_kodas_ks> 110065392 </t03_kodas_ks>"
        "<t03_dok_nr>LIKUTIS </t03_dok_nr><t03_kodas_vl>LIT </t03_kodas_vl>"
        "<t03_suma_db_vl>0.00</t03_suma_db_vl><t03_suma_cr_vl>7200.00</t03_suma_cr_vl>"
        "<t03_suma_db>.50</t03_suma_db><t03_suma_cr>7200.00</t03_suma_cr></T03>"
    )
    opener = FakeOpener((200, xml))
    response = run(make_factory, opener, payload("client_debt", {"client_code": "110065392"}))
    assert bodies(opener)[0]["params"] == {
        "list": "H",
        "fil": "ltrim(rtrim(t03_kodas_ks))='110065392'",
    }
    assert (
        response["result"]["debit"],
        response["result"]["credit"],
        response["result"]["balance"],
    ) == (0.5, 7200.0, -7199.5)


# --------------------------------------------------------------------------- contracts


def test_friendly_writes_are_suppressed_in_test_mode(make_factory) -> None:
    opener = FakeOpener()
    response = run(
        make_factory,
        opener,
        payload(
            "create_sales_invoice",
            {"client_code": "1", "department_code": "01", "lines": LINES},
            test_mode=True,
        ),
    )
    assert response["ok"] is True and opener.requests == []
    assert response["definitely_no_external_effect"] is True


def test_friendly_operations_come_first_and_raw_methods_are_advanced() -> None:
    ops = yaml.safe_load((BUNDLE_ROOT / "contracts/operation_manifest.yaml").read_text("utf-8"))[
        "operations"
    ]
    ids = [op["operation_id"] for op in ops]
    friendly = list(ids[1 : 1 + len(catalog.FRIENDLY_IDS)])
    assert set(friendly) == set(catalog.FRIENDLY_IDS)
    for op in ops:
        if (
            op["operation_id"] in catalog.METHODS_BY_OPERATION
            or op["operation_id"] == "call_method"
        ):
            assert op["display_name"].startswith("Advanced · "), op["operation_id"]
        elif op["operation_id"] in catalog.FRIENDLY_IDS:
            assert not op["display_name"].startswith("Advanced")
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text("utf-8"))
    effects = {e["operation_id"] for e in manifest["external_effects"]}
    assert catalog.FRIENDLY_WRITE_IDS <= effects
    assert not (catalog.FRIENDLY_READ_IDS & effects)
