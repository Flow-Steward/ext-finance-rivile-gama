from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from conftest import API_KEY, FakeOpener, n08, payload, ret_dok

from runtime.operations import handle_runtime


def run(make_factory, opener: FakeOpener, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    response = handle_runtime(body, transport_factory=make_factory(opener), **kwargs)
    # The API key must never travel back in any form.
    assert API_KEY not in json.dumps(response, ensure_ascii=False)
    assert "s3cr3t" not in json.dumps(response, ensure_ascii=False)
    return response


def test_read_method_sends_documented_request_and_shapes_records(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(n08("121411842"), n08("300000001", "UAB B"))))
    response = run(
        make_factory,
        opener,
        payload("get_n08_list", {"list": "a", "fil": "n08_kodas_ks='121411842'"}),
    )
    assert response["ok"] is True
    request = opener.requests[0]
    assert request["url"] == "https://api.manorivile.lt/client/v2"
    assert request["method"] == "POST"
    assert request["headers"]["apikey"] == API_KEY
    assert request["headers"]["accept"] == "application/xml"
    assert request["body"] == {
        "method": "GET_N08_LIST",
        "params": {"list": "A", "fil": "n08_kodas_ks='121411842'"},
    }
    result = response["result"]
    assert result["record_type"] == "N08"
    assert result["count"] == 2
    assert result["records"][0]["N08_KODAS_KS"] == "121411842"  # padding trimmed
    assert result["records"][0]["N08_ADR"] is None
    assert result["record"]["N08_PAV"] == 'UAB "Rivilė"'
    assert result["has_more"] is False and result["pages_fetched"] == 1
    assert result["duration_ms"] == 12
    assert "external_effect_status" not in response


def test_trim_values_can_be_turned_off(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(n08("121411842"))))
    response = run(make_factory, opener, payload("get_n08_list", config={"trim_values": False}))
    assert response["result"]["record"]["N08_KODAS_KS"] == "121411842 "


def test_nested_tables_are_always_lists(make_factory) -> None:
    xml = ret_dok(
        "<I06><I06_KODAS_PO>010R00000170</I06_KODAS_PO>"
        "<I07><I07_KODAS>PREKE</I07_KODAS></I07></I06>"
    )
    response = run(make_factory, FakeOpener((200, xml)), payload("get_i06_list", {"list": "A"}))
    record = response["result"]["record"]
    assert record["I07"] == [{"I07_KODAS": "PREKE"}]


def test_write_method_sends_data_and_marks_effect(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(n08("121411842"))))
    data = {"N08": {"N08_KODAS_KS": "121411842", "N08_PAV": "UAB", "PAP_1": "x"}}
    response = run(make_factory, opener, payload("edit_n08", {"oper": "i", "data": data}))
    assert opener.requests[0]["body"] == {
        "method": "EDIT_N08",
        "params": {"oper": "I"},
        "data": data,
    }
    assert response["external_effect_status"] == "succeeded"
    assert response["definitely_no_external_effect"] is False
    assert response["result"]["record"]["N08_KODAS_KS"] == "121411842"


def test_write_requires_data(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("edit_n08", {"oper": "I"}))
    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"


def test_test_mode_suppresses_writes_before_any_request(make_factory) -> None:
    opener = FakeOpener()
    data = {"I06": {"I06_OP_TIP": "51", "I07": [{"I07_KODAS": "A"}]}}
    response = run(
        make_factory,
        opener,
        payload("edit_i06_full", {"error_action": "EXIT", "data": data}, test_mode=True),
    )
    assert response["ok"] is True
    assert response["external_effect_status"] == "suppressed"
    assert response["definitely_no_external_effect"] is True
    assert opener.requests == []


def test_test_mode_still_validates_writes(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("edit_n08", {"bogus": 1}, test_mode=True))
    assert response["error_code"] == "invalid_payload"


def test_test_mode_still_runs_reads(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(n08("1"))))
    response = run(make_factory, opener, payload("get_n08_list", test_mode=True))
    assert response["ok"] is True and len(opener.requests) == 1


ERROR_400 = (
    "<?xml version='1.0' encoding='UTF-8'?><RET_DOK><durationMs>16</durationMs>"
    "<errorMessage>Nepavyko įvykdyti metodo</errorMessage><document/><errors><error>"
    "<data>&lt;I06>&lt;I06_OP_TIP>9&lt;/I06_OP_TIP>&lt;/I06></data><dataErrors><dataError>"
    "<tag>I06_OP_TIP</tag><code>16016</code><message>Blogas operacijos tipas</message>"
    "</dataError></dataErrors></error></errors></RET_DOK>"
)


def test_400_reports_rivile_data_errors_and_no_effect(make_factory) -> None:
    response = run(
        make_factory,
        FakeOpener((400, ERROR_400)),
        payload("edit_i06", {"data": {"I06": {"I06_OP_TIP": "9"}}}),
    )
    assert response["ok"] is False
    assert response["error_code"] == "upstream_validation_failed"
    assert "Blogas operacijos tipas" in response["error"]
    assert "I06_OP_TIP" in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert response["retryable"] is False
    detail = response["errors"][1]
    assert detail["rivile_tag"] == "I06_OP_TIP" and detail["rivile_code"] == "16016"


def test_207_is_partial_success(make_factory) -> None:
    xml = (
        "<RET_DOK><I06><I06_KODAS_PO>010R00000050</I06_KODAS_PO></I06><errors><error>"
        "<data>&lt;I07/></data><dataErrors><dataError><tag>I07_KODAS</tag><code>19014</code>"
        "<message>Nerastas prekės/paslaugos kodas</message></dataError></dataErrors>"
        "</error></errors></RET_DOK>"
    )
    response = run(
        make_factory,
        FakeOpener((207, xml)),
        payload("edit_i06_full", {"error_action": "continue", "data": {"I06": {"I07": []}}}),
    )
    assert response["ok"] is True
    result = response["result"]
    assert result["partial_success"] is True
    assert result["record"]["I06_KODAS_PO"] == "010R00000050"
    assert result["data_errors"][0]["code"] == "19014"
    assert response["external_effect_status"] == "succeeded"


@pytest.mark.parametrize(
    ("status", "operation", "code", "retryable", "no_effect"),
    [
        (401, "get_n08_list", "authentication_failed", False, True),
        (502, "edit_n08", "gateway_unavailable", True, True),
        (429, "get_n08_list", "rate_limited", True, True),
        (504, "get_n08_list", "timeout", True, True),
        (504, "edit_n08", "timeout_unknown", False, False),
        (500, "edit_n08", "timeout_unknown", False, False),
        (500, "get_n08_list", "upstream_failure", True, True),
    ],
)
def test_status_classification(make_factory, status, operation, code, retryable, no_effect) -> None:
    body = (
        "<RET_DOK><id/><durationMs>0</durationMs>"
        "<errorMessage>No response received from client gateway</errorMessage></RET_DOK>"
    )
    inputs = {"data": {"N08": {"N08_PAV": "x"}}} if operation.startswith("edit") else {}
    response = run(make_factory, FakeOpener((status, body)), payload(operation, inputs))
    assert response["ok"] is False
    assert response["error_code"] == code
    assert response["retryable"] is retryable
    assert response["definitely_no_external_effect"] is no_effect
    assert response["http_status"] == status


def test_network_timeout_on_write_is_unknown(make_factory) -> None:
    response = run(
        make_factory,
        FakeOpener(TimeoutError()),
        payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}),
    )
    assert response["error_code"] == "timeout_unknown"
    assert response["external_effect_status"] == "timeout_unknown"


def test_fetch_all_pages_follows_until_short_page(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    short = ret_dok(n08("last"))
    opener = FakeOpener((200, full), (200, full), (200, short))
    factory = make_factory(opener)
    response = handle_runtime(
        payload("get_n08_list", {"fetch_all_pages": True}), transport_factory=factory
    )
    result = response["result"]
    assert [r["body"]["params"]["pagenumber"] for r in opener.requests] == [1, 2, 3]
    assert result["count"] == 201 and result["pages_fetched"] == 3
    assert result["has_more"] is False
    assert factory.sleeps and all(s <= 0.11 for s in factory.sleeps)


def test_fetch_all_pages_stops_at_max_pages(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    opener = FakeOpener((200, full), (200, full))
    response = run(
        make_factory, opener, payload("get_n08_list", {"fetch_all_pages": True, "max_pages": 2})
    )
    assert response["result"]["has_more"] is True
    assert response["result"]["pages_fetched"] == 2


def test_rate_limit_headers_slow_the_next_page(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    low = {
        "X-RateLimit-Remaining": "5",
        "X-RateLimit-Burst-Capacity": "30",
        "X-RateLimit-Replenish-Rate": "10",
    }
    opener = FakeOpener((200, full, low), (200, ret_dok()))
    factory = make_factory(opener)
    handle_runtime(payload("get_n08_list", {"fetch_all_pages": True}), transport_factory=factory)
    assert factory.sleeps and max(factory.sleeps) >= 1.0


def test_documented_page_param_name_is_used(make_factory) -> None:
    opener = FakeOpener((200, ret_dok()))
    run(make_factory, opener, payload("get_n07_list", {"page_number": 3}))
    assert opener.requests[0]["body"]["params"] == {"pageNumber": 3}


def test_unknown_input_and_duplicate_extra_params_are_refused(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("get_n08_list", {"nope": 1}))
    assert response["error_code"] == "invalid_payload"
    response = run(
        make_factory, FakeOpener(), payload("get_n08_list", {"extra_params": {"FIL": "1=1"}})
    )
    assert response["error_code"] == "invalid_payload"


def test_extra_params_pass_through(make_factory) -> None:
    opener = FakeOpener((200, ret_dok()))
    run(make_factory, opener, payload("get_n08_list", {"extra_params": {"newOption": "1"}}))
    assert opener.requests[0]["body"]["params"] == {"newOption": "1"}


def test_connection_must_match_and_carry_a_key(make_factory) -> None:
    body = payload("get_n08_list")
    body["action"]["input"]["connection_ref"] = "other"
    assert run(make_factory, FakeOpener(), body)["error_code"] == "invalid_connection"
    response = run(make_factory, FakeOpener(), payload("get_n08_list", secrets={}))
    assert response["error_code"] == "invalid_connection"
    response = run(
        make_factory, FakeOpener(), payload("get_n08_list", secrets={"api_key": "a b c d e f g h"})
    )
    assert response["error_code"] == "invalid_connection"


def test_test_connection(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<N45><N45_KODAS>EUR</N45_KODAS></N45>")))
    response = run(make_factory, opener, payload("test_connection"))
    assert response["result"] == {
        "connected": True,
        "api_user": "APIUSER",
        "currencies": 1,
        "duration_ms": 12,
        "http_status": 200,
    }
    assert opener.requests[0]["body"]["method"] == "GET_N45_LIST"


def test_upload_reads_workflow_file_into_doc(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<I06><KODAS_PO>010R00000170</KODAS_PO></I06>")))
    pdf = b"%PDF-1.4 fixture"

    def reader(_payload, *, artifact_id):
        assert artifact_id == "artifact://inv"
        return pdf

    response = run(
        make_factory,
        opener,
        payload("edit_i06_doc", {"kodas_po": "010R00000170", "document_file": "artifact://inv"}),
        artifact_reader=reader,
    )
    params = opener.requests[0]["body"]["params"]
    assert base64.b64decode(params["doc"]) == pdf
    assert params["ext"] == ".pdf"
    assert response["external_effect_status"] == "succeeded"


def test_upload_needs_a_file_doc_or_link(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("edit_i06_doc", {"kodas_po": "1"}))
    assert response["error_code"] == "invalid_payload"


def test_download_saves_workflow_file_and_drops_base64(make_factory) -> None:
    pdf = b"%PDF-1.4 attached"
    xml = ret_dok(
        f"<I06><KODAS_PO>010R00000170</KODAS_PO><DOC>{base64.b64encode(pdf).decode()}</DOC>"
        "<EXT>.PDF</EXT></I06>"
    )
    written: dict[str, Any] = {}

    def writer(_payload, body, *, binding_key, content_type, timeout_seconds):
        written.update(body=body, binding_key=binding_key, content_type=content_type)
        return {"artifact_handle": "artifact://out"}

    response = run(
        make_factory,
        FakeOpener((200, xml)),
        payload("get_i06_doc", {"kodas_po": "010R00000170"}),
        artifact_writer=writer,
    )
    result = response["result"]
    assert written == {
        "body": pdf,
        "binding_key": "rivile_document_download",
        "content_type": "application/pdf",
    }
    assert result["document_file"] == "artifact://out"
    assert result["document_filename"] == "010R00000170.pdf"
    assert result["record"]["DOC"] is None


def test_export_list_writes_jsonl(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    opener = FakeOpener((200, full), (200, ret_dok(n08("x"))))
    captured: dict[str, Any] = {}

    def writer(_payload, body, *, binding_key, content_type, timeout_seconds):
        captured.update(body=body, binding_key=binding_key)
        return {"artifact_handle": "artifact://export"}

    response = run(
        make_factory,
        opener,
        payload("export_list", {"method": "GET_N08_LIST", "params": {"fil": "1=1"}}),
        artifact_writer=writer,
    )
    result = response["result"]
    lines = captured["body"].decode().splitlines()
    assert len(lines) == 101 and json.loads(lines[0])["_table"] == "N08"
    assert result["record_count"] == 101 and result["complete"] is True
    assert captured["binding_key"] == "rivile_export"
    assert opener.requests[1]["body"]["params"] == {"fil": "1=1", "pagenumber": 2}


def test_call_method_sends_raw_method(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<X01><A>1</A></X01>")))
    response = run(
        make_factory,
        opener,
        payload(
            "call_method", {"method": "edit_x01", "params": {"oper": "I"}, "data": {"X01": {}}}
        ),
    )
    assert opener.requests[0]["body"]["method"] == "EDIT_X01"
    assert response["external_effect_status"] == "succeeded"
    bad = run(make_factory, FakeOpener(), payload("call_method", {"method": "DROP TABLE"}))
    assert bad["error_code"] == "invalid_payload"


def test_unsupported_operation(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("delete_everything"))
    assert response["error_code"] == "unsupported_operation"
