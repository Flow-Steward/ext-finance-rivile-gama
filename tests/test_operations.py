from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from typing import Any

import pytest
import yaml
from conftest import API_KEY, BUNDLE_ROOT, FakeOpener, n08, payload, ret_dok

from runtime import budget, catalog
from runtime.operations import handle_runtime
from runtime.transport import BACKOFF_SECONDS

POLICIES = {
    p["operation_id"]: p
    for p in yaml.safe_load((BUNDLE_ROOT / "contracts/artifact_policies.yaml").read_text())[
        "artifact_policies"
    ]
}


def run(make_factory, opener: FakeOpener, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    response = handle_runtime(body, transport_factory=make_factory(opener), **kwargs)
    text = json.dumps(response, ensure_ascii=False)
    # The API key must never travel back in any form.
    assert API_KEY not in text and "s3cr3t-fixture-key" not in text
    return response


class GrantWriter:
    """Behaves like the SDK under a signed platform grant: the content type must match."""

    def __init__(self, operation_id: str) -> None:
        self.grant = POLICIES[operation_id]["outputs"][0]
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        _payload,
        chunks: Iterable[bytes],
        *,
        binding_key: str,
        content_type: str,
        timeout_seconds: float,
    ):
        if binding_key != self.grant["binding_key"]:
            raise RuntimeError("no descriptor for that binding")
        if content_type != self.grant["content_type"]:
            raise RuntimeError("Artifact content type does not match the platform upload grant")
        body = b"".join(chunks)
        self.calls.append({"body": body, "binding_key": binding_key})
        return {"artifact_handle": "artifact://out", "content_type": content_type}


# --------------------------------------------------------------------------- reads


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
    assert result["records"][0]["N08_KODAS_KS"] == "121411842"  # trailing padding trimmed
    assert result["records"][0]["N08_ADR"] is None
    assert result["record"]["N08_PAV"] == 'UAB "Rivilė"'
    assert result["other_tables"] == {}
    assert result["has_more"] is False and result["next_page"] is None
    assert result["duration_ms"] == 12
    assert "external_effect_status" not in response


def test_leading_spaces_in_codes_are_kept(make_factory) -> None:
    xml = ret_dok("<I17><I17_KODAS_PS> PREKE01 </I17_KODAS_PS></I17>")
    response = run(make_factory, FakeOpener((200, xml)), payload("get_i17_list"))
    assert response["result"]["record"]["I17_KODAS_PS"] == " PREKE01"


def test_trim_values_can_be_turned_off(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(n08("121411842"))))
    response = run(make_factory, opener, payload("get_n08_list", config={"trim_values": False}))
    assert response["result"]["record"]["N08_KODAS_KS"] == "121411842 "


def test_main_table_is_not_repeated_in_other_tables(make_factory) -> None:
    xml = ret_dok("<N37><A>1</A></N37><N40><B>2</B></N40>")
    response = run(make_factory, FakeOpener((200, xml)), payload("get_n37_list"))
    result = response["result"]
    assert result["records"] == [{"A": "1"}]
    assert result["other_tables"] == {"N40": [{"B": "2"}]}


def test_nested_tables_are_always_lists(make_factory) -> None:
    xml = ret_dok(
        "<I06><I06_KODAS_PO>010R00000170</I06_KODAS_PO>"
        "<I07><I07_KODAS>PREKE</I07_KODAS></I07></I06>"
    )
    response = run(make_factory, FakeOpener((200, xml)), payload("get_i06_list", {"list": "A"}))
    assert response["result"]["record"]["I07"] == [{"I07_KODAS": "PREKE"}]


def test_n17_list_accepts_f(make_factory) -> None:
    opener = FakeOpener((200, ret_dok()))
    run(make_factory, opener, payload("get_n17_list", {"list": "f"}))
    assert opener.requests[0]["body"]["params"] == {"list": "F"}


# --------------------------------------------------------------------------- writes


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


def test_edit_pap_sends_fields_inside_data(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<lpap><pap><id>1</id></pap></lpap>")))
    data = {"PAP": {"forma": "KSN08", "kodas1": "121411842", "papXML": "<lpap/>"}}
    run(make_factory, opener, payload("edit_pap", {"data": data}))
    assert opener.requests[0]["body"] == {"method": "EDIT_PAP", "data": data}


def test_user_procedure_is_a_write() -> None:
    assert "get_user_proc" in catalog.WRITE_OPERATION_IDS
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text("utf-8"))
    assert "get_user_proc" in {e["operation_id"] for e in manifest["external_effects"]}


def test_write_requires_data_and_required_params(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("edit_n08", {"oper": "I"}))
    assert response["error_code"] == "invalid_payload"
    response = run(make_factory, FakeOpener(), payload("edit_bus", {"module": "RO"}))
    assert response["error_code"] == "invalid_payload"
    assert "kodas_op" in response["error"]
    response = run(make_factory, FakeOpener(), payload("get_views_list", {"view_name": "v_RGI_x"}))
    assert "order_by" in response["error"]


def test_failures_before_sending_are_definitely_no_effect(make_factory) -> None:
    for body in (
        payload("edit_n08", {"data": {"N08": {}}}, secrets={}),
        payload("edit_n08", {"bogus": 1}),
        payload("not_an_operation"),
    ):
        response = run(make_factory, FakeOpener(), body)
        assert response["ok"] is False
        assert response["definitely_no_external_effect"] is True, response
        assert response["external_effect_status"] == "failed"


def test_test_mode_backstop_reports_no_effect_without_suppressed(make_factory) -> None:
    opener = FakeOpener()
    data = {"I06": {"I06_OP_TIP": "51", "I07": [{"I07_KODAS": "A"}]}}
    response = run(
        make_factory,
        opener,
        payload("edit_i06_full", {"error_action": "EXIT", "data": data}, test_mode=True),
    )
    assert response["ok"] is True
    # Core treats an extension-reported "suppressed" as a metadata conflict.
    assert response["external_effect_status"] == "failed"
    assert response["definitely_no_external_effect"] is True
    assert opener.requests == []


def test_test_mode_still_validates_writes(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("edit_n08", {"bogus": 1}, test_mode=True))
    assert response["error_code"] == "invalid_payload"


def test_test_mode_still_runs_reads(make_factory) -> None:
    opener = FakeOpener((200, ret_dok(n08("1"))))
    response = run(make_factory, opener, payload("get_n08_list", test_mode=True))
    assert response["ok"] is True and len(opener.requests) == 1


# --------------------------------------------------------------------------- errors

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
    assert response["error_code"] == "upstream_validation_failed"
    assert "Blogas operacijos tipas" in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert response["retryable"] is False
    detail = response["errors"][1]
    assert detail["rivile_tag"] == "I06_OP_TIP" and detail["rivile_code"] == "16016"


def test_large_and_truncated_400_bodies_keep_their_data_errors(make_factory) -> None:
    errors_xml = "".join(
        f"<error><data>{'x' * 600}</data><dataErrors><dataError><tag>I07_KODAS</tag>"
        f"<code>19014</code><message>Line {i} not found</message></dataError></dataErrors></error>"
        for i in range(600)
    )
    big = f"<RET_DOK><errorMessage>Nepavyko</errorMessage><errors>{errors_xml}</errors></RET_DOK>"
    assert len(big) > 256 * 1024
    response = run(
        make_factory, FakeOpener((400, big)), payload("edit_i06", {"data": {"I06": {"A": 1}}})
    )
    assert "Line 0 not found" in response["error"]
    cut = big[: len(big) // 2]  # not well-formed any more
    response = run(
        make_factory, FakeOpener((400, cut)), payload("edit_i06", {"data": {"I06": {"A": 1}}})
    )
    assert response["error_code"] == "upstream_validation_failed"
    assert "Line 0 not found" in response["error"]


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
    result = response["result"]
    assert result["partial_success"] is True
    assert result["data_errors"][0]["code"] == "19014"
    assert response["external_effect_status"] == "succeeded"


@pytest.mark.parametrize(
    ("status", "operation", "code", "retryable", "no_effect"),
    [
        (401, "get_n08_list", "authentication_failed", False, True),
        (502, "get_n08_list", "gateway_unavailable", True, True),
        (503, "get_n08_list", "gateway_unavailable", True, True),
        (502, "edit_n08", "timeout_unknown", False, False),
        (503, "edit_n08", "timeout_unknown", False, False),
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
    assert response["error_code"] == code
    assert response["retryable"] is retryable
    assert response["definitely_no_external_effect"] is no_effect
    assert response["http_status"] == status


def test_429_backs_off_and_then_succeeds(make_factory) -> None:
    opener = FakeOpener((429, "<RET_DOK/>"), (429, "<RET_DOK/>"), (200, ret_dok(n08("1"))))
    factory = make_factory(opener)
    response = handle_runtime(payload("get_n08_list"), transport_factory=factory)
    assert response["ok"] is True
    assert [s for s in factory.sleeps if s >= 1.0] == [1.0, 2.0]


def test_429_gives_up_after_the_backoff_budget(make_factory) -> None:
    opener = FakeOpener(*[(429, "<RET_DOK/>")] * (len(BACKOFF_SECONDS) + 1))
    factory = make_factory(opener)
    response = handle_runtime(
        payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}), transport_factory=factory
    )
    assert response["error_code"] == "rate_limited"
    assert response["definitely_no_external_effect"] is True
    assert [s for s in factory.sleeps if s >= 1.0] == list(BACKOFF_SECONDS)


def test_record_locked_is_retried_and_reported_without_effect(make_factory) -> None:
    locked = (
        "<RET_DOK><errorMessage>423: Įrašas užrakintas kito vartotojo [ANA]. "
        "Bandykite vėliau</errorMessage></RET_DOK>"
    )
    opener = FakeOpener((400, locked), (200, ret_dok(n08("1"))))
    response = run(make_factory, opener, payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}))
    assert response["ok"] is True and len(opener.requests) == 2
    opener = FakeOpener(*[(423, "<RET_DOK/>")] * (len(BACKOFF_SECONDS) + 1))
    response = run(make_factory, opener, payload("get_n08_list"))
    assert response["error_code"] == "record_locked"
    assert response["retryable"] is True


def test_rivile_error_text_never_carries_the_key(make_factory) -> None:
    body = f"<RET_DOK><errorMessage>Api key {API_KEY} is not valid</errorMessage></RET_DOK>"
    response = run(make_factory, FakeOpener((401, body)), payload("get_n08_list"))
    assert "[redacted]" in response["error"]


def test_network_timeout_on_write_is_unknown(make_factory) -> None:
    response = run(
        make_factory,
        FakeOpener(TimeoutError()),
        payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}),
    )
    assert response["error_code"] == "timeout_unknown"
    assert response["external_effect_status"] == "timeout_unknown"


def test_incomplete_read_is_a_connection_failure(make_factory) -> None:
    import http.client

    response = run(
        make_factory, FakeOpener(http.client.IncompleteRead(b"")), payload("get_n08_list")
    )
    assert response["error_code"] == "connection_failed"


def test_dns_failure_is_retryable_not_blocked(make_factory) -> None:
    from flowsteward_extension_sdk import PinnedPeerError

    error = PinnedPeerError("Rivile GAMA API host 'api.manorivile.lt' cannot be resolved: x")
    response = run(
        make_factory, FakeOpener(error), payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}})
    )
    assert response["error_code"] == "connection_failed"
    assert response["retryable"] is True
    assert response["definitely_no_external_effect"] is True


# --------------------------------------------------------------------------- paging


def test_fetch_all_pages_follows_until_short_page(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    opener = FakeOpener((200, full), (200, full), (200, ret_dok(n08("last"))))
    response = run(make_factory, opener, payload("get_n08_list", {"fetch_all_pages": True}))
    result = response["result"]
    assert [r["body"]["params"]["pagenumber"] for r in opener.requests] == [1, 2, 3]
    assert result["count"] == 201 and result["pages_fetched"] == 3
    assert result["page_number"] == 3 and result["next_page"] is None
    assert result["has_more"] is False


def test_max_pages_reports_the_last_page_read(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    response = run(
        make_factory,
        FakeOpener((200, full)),
        payload("get_n08_list", {"fetch_all_pages": True, "max_pages": 1}),
    )
    result = response["result"]
    assert result["page_number"] == 1 and result["next_page"] == 2 and result["has_more"] is True


def _wide_page(start: int, fields: int) -> str:
    rows = "".join(
        "<N17>"
        + "".join(f"<N17_F{f:02d}>v{start + r}-{f}</N17_F{f:02d}>" for f in range(fields))
        + "</N17>"
        for r in range(100)
    )
    return ret_dok(rows)


def test_wide_multi_page_reads_stay_within_the_host_secret_scan(
    make_factory, host_secret_guard
) -> None:
    pages = [(200, _wide_page(i * 100, 45)) for i in range(10)]
    response = run(
        make_factory, FakeOpener(*pages), payload("get_n17_list", {"fetch_all_pages": True})
    )
    assert response["ok"] is True
    result = response["result"]
    assert result["has_more"] is True and result["next_page"] == result["page_number"] + 1
    assert not host_secret_guard(response, [API_KEY])


def test_one_page_too_wide_for_a_step_points_to_export(make_factory) -> None:
    response = run(make_factory, FakeOpener((200, _wide_page(0, 130))), payload("get_n17_list"))
    assert response["error_code"] == "response_too_large"
    assert "export_list" in response["error"]


def test_budget_matches_the_host_guard(host_secret_guard) -> None:
    fits = [{f"F{f}": f"v{r}-{f}" for f in range(50)} for r in range(110)]
    assert budget.measure({"records": fits})[0] <= budget.MAX_CANDIDATE_STRINGS
    assert not host_secret_guard({"result": {"records": fits}}, [API_KEY])
    over = [{f"F{f}": f"v{r}-{f}" for f in range(50)} for r in range(170)]
    assert budget.measure({"records": over})[0] > budget.MAX_CANDIDATE_STRINGS
    assert host_secret_guard({"result": {"records": over}}, [API_KEY])  # what the budget prevents


def test_top_sets_the_page_size(make_factory) -> None:
    page = ret_dok("<PREKE><A>1</A></PREKE>" * 20)
    opener = FakeOpener((200, page), (200, page), (200, ret_dok("<PREKE><A>1</A></PREKE>")))
    response = run(
        make_factory,
        opener,
        payload("get_prod_by_barcode_time", {"top": "20", "fetch_all_pages": True}),
    )
    assert response["result"]["pages_fetched"] == 3


def test_documented_page_param_name_is_used(make_factory) -> None:
    opener = FakeOpener((200, ret_dok()))
    run(make_factory, opener, payload("get_n07_list", {"page_number": "3"}))
    assert opener.requests[0]["body"]["params"] == {"pageNumber": 3}


def test_non_ascii_digits_are_refused(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("get_n08_list", {"page_number": "²"}))
    assert response["error_code"] == "invalid_payload"


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
    assert (
        run(make_factory, FakeOpener(), payload("get_n08_list", secrets={}))["error_code"]
        == "invalid_connection"
    )


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


# --------------------------------------------------------------------------- files


def test_upload_reads_workflow_file_and_asks_rivile_not_to_echo_it(make_factory) -> None:
    pdf = b"%PDF-1.4 fixture"
    echoed = base64.b64encode(pdf).decode()
    xml = ret_dok(f"<I06><KODAS_PO>010R00000170</KODAS_PO><DOC>{echoed}</DOC></I06>")
    opener = FakeOpener((200, xml))

    def reader(_payload, *, artifact_id, **_kw):
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
    assert params["ext"] == ".pdf"  # from the file's signature
    assert params["show_doc"] == 0
    assert response["result"]["record"]["DOC"] is None
    assert response["external_effect_status"] == "succeeded"


def test_upload_of_unknown_type_needs_ext(make_factory) -> None:
    response = run(
        make_factory,
        FakeOpener(),
        payload("edit_i06_doc", {"kodas_po": "1", "document_file": "artifact://x"}),
        artifact_reader=lambda _p, *, artifact_id, **_kw: b"\x00\x01unknown",
    )
    assert response["error_code"] == "invalid_payload"
    assert "ext" in response["error"]


def test_upload_metadata_update_needs_no_file(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<I06><KODAS_PO>1</KODAS_PO></I06>")))
    response = run(
        make_factory,
        opener,
        payload(
            "edit_i06_docs",
            {"oper": "U", "kodas_po": "1", "kodas_op_dok": "2", "aprasymas": "Invoice"},
        ),
    )
    assert response["ok"] is True
    assert opener.requests[0]["body"]["params"]["oper"] == "U"
    response = run(make_factory, FakeOpener(), payload("edit_i06_doc", {"kodas_po": "1"}))
    assert response["error_code"] == "invalid_payload"


def test_get_i06_docs_defaults_to_no_inline_files(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<I06><KODAS_PO>1</KODAS_PO></I06>")))
    run(make_factory, opener, payload("get_i06_docs", {"kodas_po": "1"}))
    assert opener.requests[0]["body"]["params"] == {"kodas_po": "1", "show_doc": 0}


def test_download_writes_under_the_granted_content_type(make_factory) -> None:
    pdf = b"%PDF-1.4 attached"
    xml = ret_dok(
        f"<I06><KODAS_PO>010R00000170</KODAS_PO><DOC>{base64.b64encode(pdf).decode()}</DOC>"
        "<EXT>.PDF</EXT></I06>"
    )
    writer = GrantWriter("get_i06_doc")
    response = run(
        make_factory,
        FakeOpener((200, xml)),
        payload("get_i06_doc", {"kodas_po": "010R00000170"}),
        artifact_writer=writer,
    )
    result = response["result"]
    assert response["ok"] is True, response
    assert writer.calls == [{"body": pdf, "binding_key": "rivile_document_download"}]
    assert result["document_file"] == "artifact://out"
    assert result["original_filename"] == "010R00000170.pdf"
    assert result["original_content_type"] == "application/pdf"
    assert result["record"]["DOC"] is None


def test_export_list_writes_jsonl_under_the_grant(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    opener = FakeOpener((200, full), (200, ret_dok(n08("x"))))
    writer = GrantWriter("export_list")
    response = run(
        make_factory,
        opener,
        payload("export_list", {"method": "GET_N08_LIST", "params": {"fil": "1=1", "list": "a"}}),
        artifact_writer=writer,
    )
    result = response["result"]
    lines = writer.calls[0]["body"].decode().splitlines()
    assert len(lines) == 101 and json.loads(lines[0])["_table"] == "N08"
    assert result["record_count"] == 101 and result["complete"] is True
    assert result["resume_page"] is None
    assert result["filename"] == POLICIES["export_list"]["outputs"][0]["filename"]
    assert opener.requests[1]["body"]["params"] == {"fil": "1=1", "list": "A", "pagenumber": 2}


def test_export_list_stops_before_the_deadline_and_saves_what_it_has(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    opener = FakeOpener((200, full), (200, full), (200, full))
    now = [0.0]

    def clock() -> float:
        now[0] += 50.0  # every look at the clock costs 50 s
        return now[0]

    def factory(connection):
        transport = make_factory(opener)(connection)
        transport.clock = clock
        return transport

    writer = GrantWriter("export_list")
    response = handle_runtime(
        payload("export_list", {"method": "GET_N08_LIST", "start_page": 5}),
        transport_factory=factory,
        artifact_writer=writer,
    )
    result = response["result"]
    assert result["complete"] is False
    assert result["record_count"] == len(writer.calls[0]["body"].decode().splitlines())
    assert result["resume_page"] == 5 + result["pages_fetched"]


# --------------------------------------------------------------------------- call_method


def test_call_method_reports_a_consistent_effect_pair(make_factory) -> None:
    opener = FakeOpener((200, ret_dok("<X01><A>1</A></X01>")), (200, ret_dok(n08("1"))))
    response = run(
        make_factory,
        opener,
        payload(
            "call_method", {"method": "edit_x01", "params": {"oper": "I"}, "data": {"X01": {}}}
        ),
    )
    assert opener.requests[0]["body"]["method"] == "EDIT_X01"
    assert response["external_effect_status"] == "succeeded"
    assert response["definitely_no_external_effect"] is False
    response = run(make_factory, opener, payload("call_method", {"method": "GET_N08_LIST"}))
    assert (response["external_effect_status"], response["definitely_no_external_effect"]) == (
        "succeeded",
        False,
    )
    bad = run(make_factory, FakeOpener(), payload("call_method", {"method": "DROP TABLE"}))
    assert bad["error_code"] == "invalid_payload"


def test_unsupported_operation(make_factory) -> None:
    response = run(make_factory, FakeOpener(), payload("delete_everything"))
    assert response["error_code"] == "unsupported_operation"


# --------------------------------------------------------------------------- review round 2


def test_locked_text_never_overrides_an_ambiguous_write(make_factory) -> None:
    body = "<RET_DOK><errorMessage>Įrašas užrakintas, gateway timeout</errorMessage></RET_DOK>"
    opener = FakeOpener((500, body))
    response = run(make_factory, opener, payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}))
    assert response["error_code"] == "timeout_unknown"
    assert response["retryable"] is False
    assert len(opener.requests) == 1  # never resent


def test_locked_words_in_echoed_data_are_a_plain_rejection(make_factory) -> None:
    body = (
        "<RET_DOK><errorMessage>Nepavyko</errorMessage><errors><error>"
        "<data>423: užrakintas pavadinimas</data><dataErrors><dataError><tag>N08_PAV</tag>"
        "<code>1</code><message>423 užrakintas</message></dataError></dataErrors>"
        "</error></errors></RET_DOK>"
    )
    opener = FakeOpener((400, body))
    response = run(make_factory, opener, payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}))
    assert response["error_code"] == "upstream_validation_failed"
    assert len(opener.requests) == 1


def test_key_across_the_truncation_boundary_is_redacted(make_factory) -> None:
    message = "x" * (300 - len(API_KEY) // 2) + API_KEY + " tail"
    body = f"<RET_DOK><errorMessage>{message}</errorMessage></RET_DOK>"
    response = run(make_factory, FakeOpener((400, body)), payload("get_n08_list"))
    text = json.dumps(response)
    assert API_KEY[: len(API_KEY) // 2] not in text
    secret = API_KEY.split(".", 1)[1]
    assert secret[:8] not in text


def test_single_page_without_page_number_reports_page_one(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    response = run(make_factory, FakeOpener((200, full)), payload("get_n08_list"))
    assert response["result"]["page_number"] == 1 and response["result"]["next_page"] == 2


def _ticking(make_factory, opener: FakeOpener, step: float):
    now = [0.0]

    def clock() -> float:
        now[0] += step
        return now[0]

    def factory(connection):
        transport = make_factory(opener)(connection)
        transport.clock = clock
        return transport

    return factory


def test_fetch_all_pages_stops_at_the_step_deadline(make_factory) -> None:
    full = ret_dok(*[n08(str(i)) for i in range(100)])
    opener = FakeOpener(*[(200, full)] * 10)
    response = handle_runtime(
        payload("get_n08_list", {"fetch_all_pages": True, "max_pages": 10}),
        transport_factory=_ticking(make_factory, opener, 60.0),
    )
    result = response["result"]
    assert response["ok"] is True
    assert result["has_more"] is True and 0 < result["pages_fetched"] < 10
    assert result["next_page"] == result["page_number"] + 1


def test_backoff_never_waits_past_the_deadline(make_factory) -> None:
    opener = FakeOpener(*[(429, "<RET_DOK/>")] * 5)
    factory = _ticking(make_factory, opener, 200.0)
    response = handle_runtime(payload("get_n08_list"), transport_factory=factory)
    assert response["error_code"] in {"rate_limited", "timeout"}
    assert len(opener.requests) < 5


def test_export_saves_what_it_has_when_the_file_is_full(make_factory, monkeypatch) -> None:
    from runtime import operations

    full = ret_dok(*[n08(str(i)) for i in range(100)])
    one_page = sum(
        len((json.dumps({"_table": "N08", **r}, ensure_ascii=False) + "\n").encode())
        for r in [
            {
                "Row_number": "1",
                "N08_KODAS_KS": str(i),
                "N08_PAV": 'UAB "Rivilė"',
                "N08_ADR": None,
                "N08_KODAS_DS": None,
            }
            for i in range(100)
        ]
    )
    monkeypatch.setattr(operations, "MAX_EXPORT_BYTES", int(one_page * 2.5))
    writer = GrantWriter("export_list")
    response = run(
        make_factory,
        FakeOpener(*[(200, full)] * 3),
        payload("export_list", {"method": "GET_N08_LIST"}),
        artifact_writer=writer,
    )
    result = response["result"]
    assert result["complete"] is False and result["pages_fetched"] == 2
    assert result["resume_page"] == 3 and result["record_count"] == 200


def test_fetch_all_meters_data_errors_too(make_factory, host_secret_guard) -> None:
    errors_xml = "".join(
        f"<error><data>d{i}</data><dataErrors><dataError><tag>T{i}</tag><code>{i}</code>"
        f"<message>m{i}</message></dataError></dataErrors></error>"
        for i in range(200)
    )
    page = ret_dok(*[n08(str(i)) for i in range(100)], extra=f"<errors>{errors_xml}</errors>")
    response = run(
        make_factory,
        FakeOpener(*[(200, page)] * 10),
        payload("get_n08_list", {"fetch_all_pages": True}),
    )
    assert response["ok"] is True and response["result"]["has_more"] is True
    assert not host_secret_guard(response, [API_KEY])


def test_oversized_write_echo_is_shortened_not_failed(make_factory) -> None:
    wide = "".join(f"<F{f:03d}>{'v' * 40}</F{f:03d}>" for f in range(200))
    rows = "".join(f"<N08><N08_KODAS_KS>{i}</N08_KODAS_KS>{wide}</N08>" for i in range(80))
    response = run(
        make_factory,
        FakeOpener((200, ret_dok(rows))),
        payload("edit_n08", {"data": {"N08": {"N08_PAV": "x"}}}),
    )
    assert response["ok"] is True
    assert response["external_effect_status"] == "succeeded"
    assert response["result"]["record"]["N08_KODAS_KS"] == "0"
    assert budget.fits(response["result"])
    assert "shortened" in response["result"]["data_errors"][-1]["message"]


def test_recovering_errors_from_a_hostile_body_is_linear() -> None:
    import time

    from runtime import xml_response

    hostile = b"<errorMessage>" + b"<dataError><tag>" * 250_000
    started = time.monotonic()
    parsed = xml_response.recover_errors(hostile)
    assert time.monotonic() - started < 2.0
    assert parsed.data_errors == []


def test_any_unlisted_status_on_a_write_is_an_unknown_outcome(make_factory) -> None:
    for status in (501, 520, 524, 404, 409):
        response = run(
            make_factory,
            FakeOpener((status, "")),
            payload("edit_n08", {"oper": "I", "data": {"N08": {"N08_PAV": "X"}}}),
        )
        assert response["error_code"] == "timeout_unknown", status
        assert response["external_effect_status"] == "timeout_unknown"
        assert response["definitely_no_external_effect"] is False
        assert response["retryable"] is False


def test_a_redirect_is_refused_and_unknown_on_a_write(make_factory) -> None:
    response = run(
        make_factory,
        FakeOpener((307, "", {"Location": "https://elsewhere.example/"})),
        payload("edit_n08", {"oper": "I", "data": {"N08": {"N08_PAV": "X"}}}),
    )
    assert response["error_code"] == "redirect_rejected"
    assert response["definitely_no_external_effect"] is False
    response = run(
        make_factory,
        FakeOpener((302, "", {"Location": "https://elsewhere.example/"})),
        payload("get_n08_list", {}),
    )
    assert response["error_code"] == "redirect_rejected"
    assert response["definitely_no_external_effect"] is True


def test_show_doc_as_text_keeps_the_files(make_factory) -> None:
    doc = base64.b64encode(b"%PDF").decode()
    opener = FakeOpener((200, ret_dok(f"<I06><KODAS_PO>1</KODAS_PO><DOC>{doc}</DOC></I06>")))
    response = run(
        make_factory, opener, payload("get_i06_docs", {"kodas_po": "1", "show_doc": "1"})
    )
    assert response["result"]["records"][0]["DOC"] == doc


def test_download_reads_file_fields_case_insensitively(make_factory) -> None:
    doc = base64.b64encode(b"%PDF-1.4").decode()
    xml = ret_dok(f"<I06><kodas_po>010R1</kodas_po><doc>{doc}</doc><ext>.pdf</ext></I06>")
    writer = GrantWriter("get_i06_doc")
    response = run(
        make_factory,
        FakeOpener((200, xml)),
        payload("get_i06_doc", {"kodas_po": "010R1"}),
        artifact_writer=writer,
    )
    assert response["result"]["original_filename"] == "010R1.pdf"
    assert response["result"]["records"][0]["doc"] is None


def test_an_oversized_upload_is_refused_before_it_is_read(make_factory, monkeypatch) -> None:
    from runtime import operations

    monkeypatch.setattr(
        operations, "find_artifact_descriptor", lambda *_a, **_k: {"size_bytes": 16 * 1024 * 1024}
    )

    def reader(*_a, **_k):
        raise AssertionError("must not be read")

    response = run(
        make_factory,
        FakeOpener(),
        payload("edit_i06_doc", {"kodas_po": "1", "document_file": "artifact://big"}),
        artifact_reader=reader,
    )
    assert response["error_code"] == "invalid_payload"
    assert "15 MB" in response["error"]
