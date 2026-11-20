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
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from xml.etree.ElementTree import TreeBuilder  # nosec B405

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from . import errors
from .errors import ExtensionError

_META_TAGS = frozenset({"id", "durationMs", "errorMessage", "errors", "document"})
MAX_DEPTH = 32
MAX_XML_ELEMENTS = 250_000
MAX_XML_ATTRIBUTES = 65_536
# The largest document response contains up to 40 MiB of file data encoded as Base64.
MAX_XML_TEXT = 60 * 1024 * 1024
MAX_DATA_ERRORS = 200
_XML_FEED_BYTES = 64 * 1024


@dataclass
class ParsedResponse:
    """One Rivile response reduced to data."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    data_errors: list[dict[str, str]] = field(default_factory=list)
    error_message: str = ""
    duration_ms: int | None = None
    #: "RET_DOK", or "Map" for the platform's error body.
    root: str = "RET_DOK"

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


class _BoundedTreeBuilder(TreeBuilder):
    """Apply quotas as DefusedXMLParser constructs the response tree."""

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._elements = 0
        self._attributes = 0
        self._text = 0

    def _consume_text(self, size: int) -> None:
        self._text += size
        if self._text > MAX_XML_TEXT:
            raise ExtensionError(
                errors.INVALID_XML_RESPONSE, "The Rivile XML exceeds its text limit"
            )

    def _consume_attributes(self, count: int) -> None:
        self._attributes += count
        if self._attributes > MAX_XML_ATTRIBUTES:
            raise ExtensionError(
                errors.INVALID_XML_RESPONSE, "The Rivile XML exceeds its attribute limit"
            )

    def start(self, tag: str, attrs: dict[str, str]) -> Any:
        self._depth += 1
        self._elements += 1
        if self._depth > MAX_DEPTH or self._elements > MAX_XML_ELEMENTS:
            raise ExtensionError(
                errors.INVALID_XML_RESPONSE, "The Rivile XML exceeds its structural limits"
            )
        self._consume_attributes(len(attrs))
        self._consume_text(len(tag) + sum(len(key) + len(value) for key, value in attrs.items()))
        return super().start(tag, attrs)

    def start_ns(self, prefix: str, uri: str) -> None:
        self._consume_attributes(1)
        self._consume_text(len(prefix) + len(uri))

    def end(self, tag: str) -> Any:
        self._depth -= 1
        return super().end(tag)

    def data(self, data: str) -> None:
        self._consume_text(len(data))
        super().data(data)


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


def _error_details(element: Any) -> Iterator[Any]:
    for child in element:
        tag = _local(child.tag)
        if tag == "error":
            continue
        if tag == "dataError":
            yield child
        yield from _error_details(child)


def _data_errors(element: Any, limit: int) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for error in element.iter():
        if _local(error.tag) != "error":
            continue
        data = ""
        message = ""
        for child in error:
            tag = _local(child.tag)
            if tag == "data":
                data = errors.safe_text(child.text, 500)
            elif tag in {"message", "errorMessage"}:
                message = errors.safe_text(child.text, 300)
        has_detail = False
        for item in _error_details(error):
            has_detail = True
            entry = {"tag": "", "code": "", "message": "", "data": data}
            for part in item:
                key = _local(part.tag)
                if key in {"tag", "code", "message"}:
                    entry[key] = errors.safe_text(part.text, 300)
            found.append(entry)
            if len(found) == limit:
                return found
        if not has_detail and message:
            found.append({"tag": "", "code": "", "message": message, "data": data})
            if len(found) == limit:
                return found
    return found


def parse(raw: bytes, *, trim: bool = True) -> ParsedResponse:
    """Parse one response body; refuse anything that is not well-formed, safe XML in
    Rivile's envelope: ``<RET_DOK>``, or the platform's ``<Map>`` error body. An empty
    body is no answer: an empty result is an empty ``<RET_DOK/>``."""
    if not raw.strip():
        raise ExtensionError(errors.INVALID_XML_RESPONSE, "Rivile returned an empty response")
    try:
        parser = SafeElementTree.DefusedXMLParser(target=_BoundedTreeBuilder(), forbid_dtd=True)
        for offset in range(0, len(raw), _XML_FEED_BYTES):
            parser.feed(raw[offset : offset + _XML_FEED_BYTES])
        root = parser.close()
    except (DefusedXmlException, SafeElementTree.ParseError, ValueError) as exc:
        raise ExtensionError(
            errors.INVALID_XML_RESPONSE, "Rivile returned a response that is not valid XML"
        ) from exc
    parsed = ParsedResponse()
    root_tag = _local(root.tag)
    if root_tag == "Map":
        # The platform's generic error body (HTTP 500): <Map><status/><error/><message/></Map>
        values = {_local(c.tag): c.text for c in list(root)}
        parsed.root = "Map"
        parsed.error_message = (
            errors.safe_text(values.get("message") or values.get("error") or "", 300)
            or "Rivile returned an error"
        )
        return parsed
    if root_tag != "RET_DOK":
        raise ExtensionError(
            errors.INVALID_XML_RESPONSE,
            f"Rivile returned XML that is not its answer (root <{errors.safe_text(root_tag, 40)}>)",
        )
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
            remaining = MAX_DATA_ERRORS - len(parsed.data_errors)
            if remaining:
                parsed.data_errors.extend(_data_errors(child, remaining))
            continue
        if tag in _META_TAGS:
            continue
        record = _record(child, trim) if len(child) else {"value": _leaf(child, trim)}
        if tag not in parsed.tables:
            parsed.tables[tag] = []
            parsed.order.append(tag)
        parsed.tables[tag].append(record)
    return parsed


__all__ = ["MAX_DEPTH", "ParsedResponse", "parse"]
