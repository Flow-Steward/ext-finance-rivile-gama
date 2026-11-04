"""Read one Rivile ``<RET_DOK>`` (or error ``<Map>``) XML body into plain data.

Rivile answers every method in the same envelope::

    <RET_DOK>
      <id/> <durationMs>16</durationMs> <errorMessage>…</errorMessage>
      <N08>…record…</N08> <N08>…record…</N08>
      <errors><error><data>…</data>
        <dataErrors><dataError><tag/><code/><message/></dataError></dataErrors>
      </error></errors>
    </RET_DOK>

Parsing goes through ``defusedxml``: DTDs, entity declarations and external
references are refused rather than expanded.

The shape returned is stable for workflows: every top-level table is a list of
records; inside a record a leaf field is a string (or null) and a nested table
(``N33`` inside ``N08``, ``I07`` inside ``I06``) is always a list of records,
however many rows Rivile sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from . import errors
from .errors import ExtensionError

_META_TAGS = frozenset({"id", "durationMs", "errorMessage", "errors", "document"})
MAX_DEPTH = 32


@dataclass
class ParsedResponse:
    """One Rivile response reduced to data."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    data_errors: list[dict[str, str]] = field(default_factory=list)
    error_message: str = ""
    duration_ms: int | None = None

    @property
    def record_type(self) -> str | None:
        return self.order[0] if self.order else None

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.tables.get(self.order[0], []) if self.order else []

    @property
    def top_level_count(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


def _local(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _leaf(element: Any, trim: bool) -> str | None:
    text = element.text
    if text is None:
        return None
    # Rivile pads fixed-width codes on the right. Trailing spaces carry no meaning,
    # leading ones do ('A01' = 'A01 ', but 'A01' != ' A01'; guide §2.9), so only
    # the right side is trimmed.
    return text.rstrip() if trim else text


def _record(element: Any, trim: bool, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_DEPTH:
        raise ExtensionError(
            errors.INVALID_XML_RESPONSE, "The Rivile response is nested too deeply"
        )
    record: dict[str, Any] = {}
    for child in list(element):
        tag = _local(child.tag)
        if len(child):
            bucket = record.get(tag)
            if not isinstance(bucket, list):
                bucket = []
                record[tag] = bucket
            bucket.append(_record(child, trim, depth + 1))
            continue
        value = _leaf(child, trim)
        if tag in record:
            existing = record[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                record[tag] = [existing, value]
        else:
            record[tag] = value
    return record


def _data_errors(element: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for error in element.iter():
        if _local(error.tag) != "error":
            continue
        data = ""
        for child in list(error):
            if _local(child.tag) == "data":
                data = errors.safe_text(child.text, 500)
        nested = [n for n in error.iter() if _local(n.tag) == "dataError"]
        if not nested:
            message = ""
            for child in list(error):
                if _local(child.tag) in {"message", "errorMessage"}:
                    message = errors.safe_text(child.text, 300)
            if message:
                found.append({"tag": "", "code": "", "message": message, "data": data})
            continue
        for item in nested:
            entry = {"tag": "", "code": "", "message": "", "data": data}
            for part in list(item):
                key = _local(part.tag)
                if key in {"tag", "code", "message"}:
                    entry[key] = errors.safe_text(part.text, 300)
            found.append(entry)
    return found[:200]


def parse(raw: bytes, *, trim: bool = True) -> ParsedResponse:
    """Parse one response body; refuse anything that is not well-formed, safe XML."""
    if not raw.strip():
        return ParsedResponse()
    try:
        root = SafeElementTree.fromstring(raw, forbid_dtd=True)
    except (DefusedXmlException, SafeElementTree.ParseError, ValueError) as exc:
        raise ExtensionError(
            errors.INVALID_XML_RESPONSE, "Rivile returned a response that is not valid XML"
        ) from exc
    parsed = ParsedResponse()
    root_tag = _local(root.tag)
    if root_tag == "Map":
        # The platform's generic error body (HTTP 500): <Map><status/><error/><message/></Map>
        values = {_local(c.tag): c.text for c in list(root)}
        parsed.error_message = errors.safe_text(
            values.get("message") or values.get("error") or "", 300
        )
        return parsed
    for child in list(root):
        tag = _local(child.tag)
        if tag == "durationMs":
            text = (child.text or "").strip()
            parsed.duration_ms = int(text) if re.fullmatch(r"[0-9]{1,12}", text) else None
            continue
        if tag == "errorMessage":
            parsed.error_message = errors.safe_text(child.text, 300)
            continue
        if tag == "errors":
            parsed.data_errors.extend(_data_errors(child))
            continue
        if tag in _META_TAGS:
            continue
        record = _record(child, trim) if len(child) else {"value": _leaf(child, trim)}
        if tag not in parsed.tables:
            parsed.tables[tag] = []
            parsed.order.append(tag)
        parsed.tables[tag].append(record)
    return parsed


#: Upper bound on how much of a broken body is searched, and on entries kept.
_RECOVER_SCAN_BYTES = 4 * 1024 * 1024
_RECOVER_MAX_ENTRIES = 200


def _between(raw: bytes, start: int, open_tag: bytes, close_tag: bytes) -> tuple[bytes, int] | None:
    """Content of the next ``open_tag``…``close_tag`` from ``start``, in linear time."""
    begin = raw.find(open_tag, start)
    if begin < 0:
        return None
    begin += len(open_tag)
    end = raw.find(close_tag, begin)
    if end < 0:
        return None
    return raw[begin:end], end + len(close_tag)


def recover_errors(raw: bytes) -> ParsedResponse:
    """Best-effort errorMessage and dataErrors from a cut or malformed error body.

    Each tag is located with one forward ``find``, so a body full of unclosed
    tags costs a single pass rather than a regex backtrack per start.
    """
    raw = raw[:_RECOVER_SCAN_BYTES]
    parsed = ParsedResponse()

    def text(value: bytes) -> str:
        return errors.safe_text(value.decode("utf-8", "replace"), 300)

    found = _between(raw, 0, b"<errorMessage>", b"</errorMessage>")
    if found:
        parsed.error_message = text(found[0])
    position = 0
    while len(parsed.data_errors) < _RECOVER_MAX_ENTRIES:
        block = _between(raw, position, b"<dataError>", b"</dataError>")
        if block is None:
            break
        content, position = block
        entry = {"tag": "", "code": "", "message": "", "data": ""}
        for key in ("tag", "code", "message"):
            part = _between(content, 0, f"<{key}>".encode(), f"</{key}>".encode())
            if part:
                entry[key] = text(part[0])
        parsed.data_errors.append(entry)
    return parsed


__all__ = ["MAX_DEPTH", "ParsedResponse", "parse", "recover_errors"]
