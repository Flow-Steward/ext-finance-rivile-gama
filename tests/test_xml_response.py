from __future__ import annotations

import pytest

from runtime import xml_response
from runtime.errors import ExtensionError


def test_documented_list_response() -> None:
    raw = (
        b"<?xml version='1.0' encoding='UTF-8' ?><RET_DOK><N08><Row_number>1</Row_number>"
        b"<N08_KODAS_KS>121411842</N08_KODAS_KS><N33><N33_EIL_NR>1</N33_EIL_NR></N33>"
        b"<N33><N33_EIL_NR>2</N33_EIL_NR></N33></N08></RET_DOK>"
    )
    parsed = xml_response.parse(raw)
    assert parsed.record_type == "N08"
    record = parsed.records[0]
    assert record["Row_number"] == "1"
    assert [row["N33_EIL_NR"] for row in record["N33"]] == ["1", "2"]


def test_multiple_tables_keep_their_order() -> None:
    parsed = xml_response.parse(b"<RET_DOK><N37><A>1</A></N37><N40><B>2</B></N40></RET_DOK>")
    assert parsed.order == ["N37", "N40"]
    assert parsed.tables["N40"] == [{"B": "2"}]


def test_empty_result() -> None:
    parsed = xml_response.parse(b"<RET_DOK/>")
    assert parsed.records == [] and parsed.record_type is None


def test_platform_500_map() -> None:
    parsed = xml_response.parse(
        b"<Map><status>500</status><error>Internal Server Error</error>"
        b"<message>No message available</message></Map>"
    )
    assert parsed.error_message == "No message available"


@pytest.mark.parametrize(
    "raw",
    [
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><RET_DOK>&a;</RET_DOK>',
        b'<!DOCTYPE r SYSTEM "file:///etc/passwd"><RET_DOK/>',
        b"<RET_DOK><unclosed></RET_DOK>",
    ],
)
def test_unsafe_or_broken_xml_is_refused(raw: bytes) -> None:
    with pytest.raises(ExtensionError) as info:
        xml_response.parse(raw)
    assert info.value.code == "invalid_xml_response"


def test_control_characters_are_removed_from_error_text() -> None:
    parsed = xml_response.parse(
        b"<RET_DOK><errorMessage>bad\t thing\n\nhere</errorMessage></RET_DOK>"
    )
    assert parsed.error_message == "bad thing here"
