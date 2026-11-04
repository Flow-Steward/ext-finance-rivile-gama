"""Operation registry: every Rivile method plus the four extension operations."""

from __future__ import annotations

import re

from .methods import METHODS
from .models import Method

TEST_CONNECTION = "test_connection"
CALL_METHOD = "call_method"
EXPORT_LIST = "export_list"
SPECIAL_OPERATION_IDS: frozenset[str] = frozenset({TEST_CONNECTION, CALL_METHOD, EXPORT_LIST})

METHODS_BY_OPERATION: dict[str, Method] = {m.operation_id: m for m in METHODS}
METHODS_BY_NAME: dict[str, Method] = {m.method: m for m in METHODS}
PAGED_METHODS: frozenset[str] = frozenset(m.method for m in METHODS if m.paged)
OPERATION_IDS: frozenset[str] = frozenset(METHODS_BY_OPERATION) | SPECIAL_OPERATION_IDS
WRITE_OPERATION_IDS: frozenset[str] = frozenset(
    {m.operation_id for m in METHODS if m.write} | {CALL_METHOD}
)

#: Files attach to / come from purchase and sales documents.
UPLOAD_METHODS: frozenset[str] = frozenset({"EDIT_I06_DOC", "EDIT_I06_DOCS"})
DOWNLOAD_METHODS: frozenset[str] = frozenset({"GET_I06_DOC"})
UPLOAD_BINDING = "rivile_document_upload"
DOWNLOAD_BINDING = "rivile_document_download"
EXPORT_BINDING = "rivile_export"

METHOD_NAME_RE = re.compile(r"^(MGAMA_)?(GET|EDIT)_[A-Z0-9_]{2,60}$|^INIT_TRIGGER$")

__all__ = [
    "CALL_METHOD",
    "DOWNLOAD_BINDING",
    "DOWNLOAD_METHODS",
    "EXPORT_BINDING",
    "EXPORT_LIST",
    "METHODS_BY_NAME",
    "METHODS_BY_OPERATION",
    "METHOD_NAME_RE",
    "OPERATION_IDS",
    "PAGED_METHODS",
    "SPECIAL_OPERATION_IDS",
    "TEST_CONNECTION",
    "UPLOAD_BINDING",
    "UPLOAD_METHODS",
    "WRITE_OPERATION_IDS",
]
