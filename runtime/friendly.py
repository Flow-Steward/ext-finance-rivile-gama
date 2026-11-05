"""Friendly operations: plain inputs, several Rivile calls in one step.

Every mapping below comes from the Rivile GAMA API v2 guide (method pages and
their "Lentelės" field tables). Mappings the guide leaves to each company's own
setup (account relation and tax table codes, money accounts) are explicit
inputs with the guide's example value as default; README lists them as
unverified without a live key.

Composite writes report exactly what they did. When a later call fails, or the
step runs out of time, after an earlier write succeeded, the step fails with
"may have an effect", is not retried automatically, and its message lists what
was already created.

Reruns: the find-or-create operations look up before they create, and an
invoice with a ``number`` is looked up by number, client and type first, so
running a step again *after* the first run has finished is safe. Two runs at
the same moment are not: both can miss the lookup and both create (Rivile
numbers new clients itself and allows duplicate company codes unless
RGI_N08_UNIK forbids them). Do not run the same find-or-create in parallel.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import budget, errors
from .errors import ExtensionError
from .inputs import flag, whole
from .inputs import invalid as _invalid
from .transport import DeadlineReached, Reply

#: Rivile client kinds (N08_RUSIS): 1 buyer, 2 supplier, 3 buyer and supplier.
CLIENT_TYPES = {"customer": 1, "supplier": 2, "both": 3}
#: I06_OP_TIP: purchases 1 (waybill/invoice), sales 51 (waybill/invoice).
SALES_OP_TYPE = 51
PURCHASE_OP_TYPE = 1
#: I07_TIPAS / N17_TIPAS: 1 product, 2 service.
LINE_TYPES = {"product": 1, "service": 2}
#: Defaults the guide shows in its examples; each company sets its own codes.
DEFAULT_TAX_TABLE = "PVM"
DEFAULT_CLIENT_ACCOUNT_RELATION = "PT001"
DEFAULT_PRODUCT_ACCOUNT_RELATION = "PR001"
MAX_LINES = 500
MAX_TEXT = 150
#: Rivile precisions (field tables): prices N(12,4), decimal quantity N(12,3),
#: percentages N(6,2), money N(12,2).
PRICE = Decimal("0.0001")
QUANTITY = Decimal("0.001")
PERCENT = Decimal("0.01")
MONEY = Decimal("0.01")
CHANGE_SOURCES: dict[str, tuple[str, dict[str, str]]] = {
    # GET_CLIENTS_TIME / GET_PROD_BY_BARCODE_TIME: list H = only what changed.
    "clients": ("GET_CLIENTS_TIME", {"list": "H"}),
    "products": ("GET_PROD_BY_BARCODE_TIME", {"list": "H"}),
    # GET_PRICES_TIME: list H = only products with barcodes, A = every product.
    "prices": ("GET_PRICES_TIME", {"list": "A"}),
    "stock": ("GET_I17_LIST_TIME", {}),
    "documents": ("GET_I06_LIST", {"list": "H"}),
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")
#: Codes that go into a T-SQL ``fil`` condition. ASCII only: a look-alike quote
#: (U+02BC, U+FF07) could be "best-fit" converted into ' by a legacy code page.
_CODE_RE = re.compile(r"^[A-Za-z0-9 ._/-]+$")
_NUMBER_RE = re.compile(r"^-?[0-9]{0,15}([.,][0-9]{1,6})?$")


# ---------------------------------------------------------------------------
# Shared helpers


def _text(
    inputs: Mapping[str, Any], name: str, *, required: bool = False, limit: int = MAX_TEXT
) -> str:
    value = inputs.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _invalid(f"{name} is required")
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _invalid(f"{name} must be text")
    text = str(value).strip()
    if len(text) > limit or any(ord(ch) < 32 for ch in text):
        raise _invalid(f"{name} must be at most {limit} printable characters")
    return text


def _code(inputs: Mapping[str, Any], name: str, *, limit: int, required: bool = False) -> str:
    """A Rivile code: letters, digits, space and . _ / - only."""
    text = _text(inputs, name, required=required, limit=limit)
    if text and not _CODE_RE.fullmatch(text):
        raise _invalid(f"{name} may contain only letters, digits, spaces and . _ / -")
    return text


def _decimal_input(
    value: Any,
    name: str,
    step: Decimal,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise _invalid(f"{name} must be a number")
    if isinstance(value, (int, float)):
        text = repr(value) if isinstance(value, float) else str(value)
    elif isinstance(value, str) and value.strip() and _NUMBER_RE.fullmatch(value.strip()):
        text = value.strip().replace(",", ".")
    else:
        raise _invalid(f"{name} must be a number")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise _invalid(f"{name} must be a number") from exc
    if not number.is_finite():
        raise _invalid(f"{name} must be a number")
    number = number.quantize(step, rounding=ROUND_HALF_UP)
    if minimum is not None and number < minimum:
        raise _invalid(f"{name} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise _invalid(f"{name} must be at most {maximum}")
    return number


def _wire(number: Decimal) -> int | float:
    """A JSON number with exactly the rounded value."""
    return int(number) if number == number.to_integral_value() else float(number)


def _date(value: Any, name: str, *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise _invalid(f"{name} is required (YYYY-MM-DD)")
        return ""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise _invalid(f"{name} must be a date YYYY-MM-DD")
    try:
        date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _invalid(f"{name} is not a valid date") from exc
    return value.strip()


def _bool(inputs: Mapping[str, Any], name: str, default: bool) -> bool:
    return flag(name, inputs.get(name), default)


def sql_literal(value: str) -> str:
    """A T-SQL string literal for a Rivile ``fil`` condition (allowlisted codes only)."""
    if not _CODE_RE.fullmatch(value) or len(value) > MAX_TEXT:
        raise _invalid("A lookup value may contain only letters, digits, spaces and . _ / -")
    return "'" + value.replace("'", "''") + "'"


def _allowed(inputs: Mapping[str, Any], names: set[str]) -> None:
    unknown = set(inputs) - names - {"connection_ref"}
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")


def field(record: Mapping[str, Any] | None, name: str) -> Any:
    """A field of a Rivile record, matched case-insensitively.

    Rivile mixes cases in its answers (``<t03_suma_db>``, ``<des_likutis_us>``,
    ``<kodas_ps>`` next to ``<N08_KODAS_KS>``), so names are never compared as is.
    """
    if not isinstance(record, Mapping):
        return None
    if name in record:
        return record[name]
    lowered = name.lower()
    for key, value in record.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def _value(record: Mapping[str, Any] | None, *names: str) -> str:
    for name in names:
        value = field(record, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _decimal(record: Mapping[str, Any] | None, name: str) -> Decimal | None:
    raw = field(record, name)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not re.fullmatch(r"-?([0-9]+(\.[0-9]*)?|\.[0-9]+)", text):
        return None
    return Decimal(text)


def _out(number: Decimal | None, step: Decimal = MONEY) -> float | None:
    return None if number is None else float(number.quantize(step, rounding=ROUND_HALF_UP))


def _first(reply: Reply) -> dict[str, Any] | None:
    records = reply.parsed.records
    return records[0] if records else None


class Progress:
    """The writes a composite step has completed, for an honest failure message."""

    def __init__(self) -> None:
        self.done: list[str] = []

    def add(self, text: str) -> None:
        self.done.append(text)

    def fail(self, exc: ExtensionError, step: str) -> ExtensionError:
        if not self.done:
            return exc
        message = (
            f"{step} failed: {exc.message}. Already done in Rivile: {'; '.join(self.done)}. "
            "Check Rivile before running this step again."
        )
        # The failing call's own code stays (validation, timeout…); what changes
        # is the effect: earlier writes happened, so this step may not be rerun
        # blindly.
        return ExtensionError(
            exc.code,
            message,
            failure_class="provider",
            retryable=False,
            definitely_no_external_effect=False,
            external_effect_status="timeout_unknown",
            http_status=exc.http_status,
            data_errors=exc.data_errors,
        )

    def guard(self, step: str, run: Callable[[], Any]) -> Any:
        """Run one part of a composite step; a failure after a write reports the write."""
        try:
            return run()
        except ExtensionError as exc:
            raise self.fail(exc, step) from exc
        except DeadlineReached as exc:
            if not self.done:
                raise
            raise self.fail(
                ExtensionError(errors.TIMEOUT_UNKNOWN, "the step ran out of time"), step
            ) from exc


# ---------------------------------------------------------------------------
# Clients


CLIENT_INPUTS = {
    "company_code",
    "vat_code",
    "client_code",
    "name",
    "address",
    "location_code",
    "email",
    "phone",
    "client_type",
    "legal_entity",
    "account_relation_code",
    "tax_table_code",
}
#: Lookup order and the Rivile field each identifier is matched against.
#: N08_IM_KODAS and N08_PVM_KODAS are C(35), N08_KODAS_KS is C(12).
_CLIENT_KEYS = (
    ("company_code", "N08_IM_KODAS", 35),
    ("vat_code", "N08_PVM_KODAS", 35),
    ("client_code", "N08_KODAS_KS", 12),
)


def _identifiers(inputs: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    found = []
    for name, column, limit in _CLIENT_KEYS:
        value = _code(inputs, name, limit=limit)
        if value:
            found.append((name, column, value))
    return found


def _same(left: str, right: str) -> bool:
    return left.replace(" ", "").upper() == right.replace(" ", "").upper()


def _find_client(
    call: Callable[..., Reply], inputs: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """The one client the given identifiers point to, or none; never a guess.

    The first identifier with any match decides. More than one match, or a
    match whose other identifiers contradict the ones given, is refused before
    anything is written.
    """
    given = _identifiers(inputs)
    for name, column, value in given:
        reply = call(
            "GET_N08_LIST",
            {"list": "H", "fil": f"{column.lower()}={sql_literal(value)}"},
            None,
            mutating=False,
        )
        records = reply.parsed.records
        if not records:
            continue
        if len(records) > 1:
            codes = ", ".join(_value(r, "N08_KODAS_KS") for r in records[:5])
            raise _invalid(
                f"{len(records)} Rivile clients have this {name} ({codes}); give client_code"
            )
        record = records[0]
        for other, other_column, other_value in given:
            actual = _value(record, other_column)
            if other != name and actual and not _same(actual, other_value):
                raise _invalid(
                    f"The client found by {name} has {other} {actual}, not {other_value}"
                )
        return record, name
    return None, ""


def _client_payload(inputs: Mapping[str, Any], *, require_name: bool = True) -> dict[str, Any]:
    client_type = (_text(inputs, "client_type") or "customer").lower()
    if client_type not in CLIENT_TYPES:
        raise _invalid("client_type must be customer, supplier or both")
    name = _text(inputs, "name", required=require_name)
    tax_table = _code(inputs, "tax_table_code", limit=12) or DEFAULT_TAX_TABLE
    record: dict[str, Any] = {
        "N08_PAV": name,
        "N08_RUSIS": CLIENT_TYPES[client_type],
        "N08_TIPAS": 1 if _bool(inputs, "legal_entity", True) else 2,
        "N08_KODAS_DS": _code(inputs, "account_relation_code", limit=12)
        or DEFAULT_CLIENT_ACCOUNT_RELATION,
    }
    if client_type in {"customer", "both"}:
        record["N08_KODAS_XS_P"] = tax_table
    if client_type in {"supplier", "both"}:
        record["N08_KODAS_XS_T"] = tax_table
    for name_, column, limit in _CLIENT_KEYS:
        value = _code(inputs, name_, limit=limit)
        if value:
            record[column] = value
    for name_, column in (
        ("address", "N08_ADR"),
        ("location_code", "N08_KODAS_VS"),
        ("email", "N08_E_MAIL"),
        ("phone", "N08_TEL"),
    ):
        value = _text(inputs, name_)
        if value:
            record[column] = value
    return record


def find_or_create_client(
    call: Callable[..., Reply], inputs: Mapping[str, Any], progress: Progress
) -> dict[str, Any]:
    if not _identifiers(inputs):
        raise _invalid("Give company_code, vat_code or client_code to look the client up")
    found, matched_by = _find_client(call, inputs)
    if found is not None:
        return {
            "client_code": _value(found, "N08_KODAS_KS"),
            "created": False,
            "matched_by": matched_by,
            "match_count": 1,
            "record": found,
        }
    payload = _client_payload(inputs)
    reply = call("EDIT_N08", {"oper": "I"}, {"N08": payload}, mutating=True)
    record = _first(reply) or {}
    code = _value(record, "N08_KODAS_KS") or str(payload.get("N08_KODAS_KS", ""))
    progress.add(f"client {code or payload['N08_PAV']} created (EDIT_N08)")
    if not code:
        # Rivile numbered the client itself and did not echo the code: read it back.
        again, _matched = progress.guard(
            "Reading the new client's code", lambda: _find_client(call, inputs)
        )
        code = _value(again, "N08_KODAS_KS")
        record = again or record
    return {
        "client_code": code,
        "created": True,
        "matched_by": "",
        "match_count": 0,
        "record": record,
    }


# ---------------------------------------------------------------------------
# Invoices


INVOICE_INPUTS = CLIENT_INPUTS | {
    "number",
    "date",
    "due_date",
    "currency",
    "foreign_currency",
    "department_code",
    "prices_include_vat",
    "notes",
    "lines",
    "post",
}
LINE_FIELDS = {
    "product_code",
    "line_type",
    "quantity",
    "unit",
    "unit_price",
    "vat_rate",
    "vat_code",
    "discount",
    "department_code",
    "name",
}
HUNDRED = Decimal(100)


def _invoice_lines(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = inputs.get("lines")
    if not isinstance(raw, list) or not raw:
        raise _invalid("lines must be a non-empty list")
    if len(raw) > MAX_LINES:
        raise _invalid(f"At most {MAX_LINES} lines per document")
    header_department = _code(inputs, "department_code", limit=12)
    with_vat = _bool(inputs, "prices_include_vat", False)
    foreign = _foreign_currency(inputs)
    lines = []
    for index, item in enumerate(raw):
        where = f"lines[{index}]"
        if not isinstance(item, Mapping):
            raise _invalid(f"{where} must be an object")
        unknown = set(item) - LINE_FIELDS
        if unknown:
            raise _invalid(f"{where}: unknown field {', '.join(sorted(unknown))}")
        line_type = (_text(item, "line_type") or "product").lower()
        if line_type not in LINE_TYPES:
            raise _invalid(f"{where}.line_type must be product or service")
        department = _code(item, "department_code", limit=12) or header_department
        if not department:
            raise _invalid(f"{where}: department_code is required (Rivile I07_KODAS_IS)")
        unit = _code(item, "unit", limit=12)
        if not unit:
            raise _invalid(f"{where}.unit is required (the unit the quantity is in, e.g. VNT)")
        quantity = _decimal_input(
            item.get("quantity"), f"{where}.quantity", QUANTITY, minimum=Decimal(0)
        )
        price = _decimal_input(
            item.get("unit_price"), f"{where}.unit_price", PRICE, minimum=Decimal(0)
        )
        # KIEKIS_U is a decimal quantity in the line's sale/receipt unit
        # (I07_KODAS_US_P / _US_A). Setting all three unit codes to the same unit,
        # as the EDIT_I06_FULL example does, makes the quantity mean that unit.
        line: dict[str, Any] = {
            "I07_TIPAS": LINE_TYPES[line_type],
            "I07_KODAS": _code(item, "product_code", required=True, limit=12),
            "I07_KODAS_IS": department,
            "I07_KODAS_US": unit,
            "I07_KODAS_US_P": unit,
            "I07_KODAS_US_A": unit,
            "KIEKIS_U": _wire(quantity),
        }
        if foreign:
            line["I07_VAL_KAINA"] = _wire(price)
        else:
            line["I07_KAINA_SU" if with_vat else "I07_KAINA_BE"] = _wire(price)
        if item.get("discount") is not None:
            line["I07_NUOLAIDA"] = _wire(
                _decimal_input(
                    item.get("discount"),
                    f"{where}.discount",
                    PERCENT,
                    minimum=Decimal(0),
                    maximum=HUNDRED,
                )
            )
        if item.get("vat_rate") is not None:
            line["I07_MOKESTIS"] = 1
            line["I07_MOKESTIS_P"] = _wire(
                _decimal_input(
                    item.get("vat_rate"),
                    f"{where}.vat_rate",
                    PERCENT,
                    minimum=Decimal(0),
                    maximum=HUNDRED,
                )
            )
        vat_code = _code(item, "vat_code", limit=12)
        if vat_code:
            line["I07_KODAS_KL"] = vat_code
        name = _text(item, "name", limit=40)
        if name:
            line["I07_PAV"] = name
        lines.append(line)
    return lines


def _foreign_currency(inputs: Mapping[str, Any]) -> bool:
    """Whether the document is in a currency other than the company's base one.

    Only then are prices sent in currency (I07_VAL_KAINA, "Tik valiutinio
    dokumento atveju") and I06_VAL_POZ set. Rivile's currency price has no
    "with VAT" twin, so foreign_currency and prices_include_vat are exclusive.
    """
    foreign = _bool(inputs, "foreign_currency", False)
    currency = _code(inputs, "currency", limit=12)
    if foreign and not currency:
        raise _invalid("foreign_currency needs currency (the document's currency code)")
    if currency and not foreign:
        # I06_KODAS_VL alone makes Rivile treat a non-base currency as a currency
        # document (I06_VAL_POZ=1) while the prices were sent as base amounts.
        raise _invalid(
            "currency is only for foreign-currency documents: set foreign_currency to true, "
            "or leave currency empty for the company's base currency"
        )
    if foreign and _bool(inputs, "prices_include_vat", False):
        raise _invalid(
            "Foreign-currency prices are net; prices_include_vat cannot be used with them"
        )
    return foreign


def _invoice_header(inputs: Mapping[str, Any], op_type: int, client_code: str) -> dict[str, Any]:
    doc_date = _date(inputs.get("date"), "date") or date.today().isoformat()
    header: dict[str, Any] = {
        "I06_OP_TIP": op_type,
        "I06_KODAS_KS": client_code,
        "I06_OP_DATA": doc_date,
        "I06_DOK_DATA": doc_date,
    }
    number = _code(inputs, "number", limit=20)
    if number:
        header["I06_DOK_NR"] = number
    currency = _code(inputs, "currency", limit=12)
    if currency:
        header["I06_KODAS_VL"] = currency
    if _foreign_currency(inputs):
        header["I06_VAL_POZ"] = 1
    elif _bool(inputs, "prices_include_vat", False):
        header["I06_PVM_TIP"] = 1
    notes = _text(inputs, "notes", limit=40)
    if notes:
        header["I06_PASTABOS"] = notes
    due = _date(inputs.get("due_date"), "due_date")
    if due:
        days = (date.fromisoformat(due) - date.fromisoformat(doc_date)).days
        if days < 0:
            raise _invalid("due_date is before date")
        # I08 payment terms: 100 % payable after the given number of days.
        header["I08"] = [{"I08_EIL_NR": 1, "I08_MOK_P": 100, "I08_MOK_D": days}]
    return header


def _existing_invoice(
    call: Callable[..., Reply], number: str, client_code: str, op_type: int
) -> dict[str, Any] | None:
    fil = (
        f"i06_dok_nr={sql_literal(number)} and i06_kodas_ks={sql_literal(client_code)} "
        f"and i06_op_tip={op_type}"
    )
    reply = call("GET_I06_LIST", {"list": "H", "fil": fil}, None, mutating=False)
    return reply.parsed.records[0] if reply.parsed.records else None


def _invoice_result(
    record: Mapping[str, Any],
    header: Mapping[str, Any],
    client_code: str,
    *,
    created: bool,
    client_created: bool,
    posted: bool | None,
    post_error: str,
) -> dict[str, Any]:
    net = _decimal(record, "I06_SUMA")
    vat = _decimal(record, "I06_SUMA_PVM")
    return {
        "document_code": _value(record, "I06_KODAS_PO"),
        "number": _value(record, "I06_DOK_NR") or str(header.get("I06_DOK_NR", "")),
        "created": created,
        "client_code": client_code,
        "client_created": client_created,
        "total_net": _out(net),
        "total_vat": _out(vat),
        "total": None if net is None or vat is None else _out(net + vat),
        "posted": posted,
        "post_error": post_error,
        "record": dict(record),
    }


#: I06_PERKELTA: 2 when the document is posted (moved), as the step reads it.
_POSTED = "2"


def _post(call: Callable[..., Reply], code: str) -> tuple[bool | None, str]:
    """Post one document; the invoice exists either way, so a failure is reported, not raised."""
    try:
        call("EDIT_I06", {"oper": "P"}, {"I06": {"I06_KODAS_PO": code}}, mutating=True)
        return True, ""
    except ExtensionError as exc:
        # Unknown stays unknown: an ambiguous answer may have posted it.
        return (None if exc.code in errors.AMBIGUOUS_ERROR_CODES else False), exc.message
    except DeadlineReached:
        return False, "The step ran out of time before posting"


def _post_existing(
    call: Callable[..., Reply], existing: Mapping[str, Any], inputs: Mapping[str, Any]
) -> tuple[bool | None, str]:
    """The real posted state of a document found by number, posting it if asked.

    A rerun after a failed post finds the document unposted: with post=true it is
    posted now instead of being returned as is.
    """
    if _value(existing, "I06_PERKELTA") == _POSTED:
        return True, ""
    if not _bool(inputs, "post", False):
        return False, ""
    code = _value(existing, "I06_KODAS_PO")
    if not code:
        return False, "Rivile did not return the document code; not posted"
    return _post(call, code)


def create_invoice(
    call: Callable[..., Reply], inputs: Mapping[str, Any], progress: Progress, *, op_type: int
) -> dict[str, Any]:
    lines = _invoice_lines(inputs)
    client_code = _code(inputs, "client_code", limit=12)
    client_created = False
    has_lookup = bool(
        _code(inputs, "company_code", limit=35) or _code(inputs, "vat_code", limit=35)
    )
    if has_lookup or (client_code and _text(inputs, "name")):
        defaults = {"client_type": "customer" if op_type == SALES_OP_TYPE else "supplier"}
        client = find_or_create_client(call, {**defaults, **inputs}, progress)
        client_code, client_created = client["client_code"], client["created"]
    if not client_code:
        if progress.done:
            raise progress.fail(
                ExtensionError(errors.UPSTREAM_FAILURE, "Rivile did not return the client's code"),
                "Creating the invoice",
            )
        raise _invalid("Give client_code, or company_code / vat_code to find or create the client")
    header = _invoice_header(inputs, op_type, client_code)
    number = str(header.get("I06_DOK_NR", ""))
    if number:
        # A rerun after an unknown outcome must not create the document twice.
        existing = progress.guard(
            "Looking for an existing invoice",
            lambda: _existing_invoice(call, number, client_code, op_type),
        )
        if existing is not None:
            found_posted, found_error = _post_existing(call, existing, inputs)
            return _invoice_result(
                existing,
                header,
                client_code,
                created=False,
                client_created=client_created,
                posted=found_posted,
                post_error=found_error,
            )
    header["I07"] = lines
    reply = progress.guard(
        "Creating the invoice",
        lambda: call("EDIT_I06_FULL", {"errorAction": "ROLLBACK"}, {"I06": header}, mutating=True),
    )
    record = _first(reply) or {}
    code = _value(record, "I06_KODAS_PO")
    progress.add(f"document {code or number or '(no code returned)'} created (EDIT_I06_FULL)")
    posted: bool | None = None
    post_error = ""
    if _bool(inputs, "post", False):
        if not code:
            posted, post_error = False, "Rivile did not return the document code; not posted"
        else:
            posted, post_error = _post(call, code)
    return _invoice_result(
        record,
        header,
        client_code,
        created=True,
        client_created=client_created,
        posted=posted,
        post_error=post_error,
    )


# ---------------------------------------------------------------------------
# Payments, products, stock, prices, changes, debts


def _payment(inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(
        inputs, {"document_code", "amount", "money_account_code", "currency_amount", "description"}
    )
    payment: dict[str, Any] = {
        "I13_KODAS_PO": _code(inputs, "document_code", required=True, limit=12),
        "I13_KODAS_SS": _code(inputs, "money_account_code", required=True, limit=12),
        "I13_SUMA": _wire(_decimal_input(inputs.get("amount"), "amount", MONEY, minimum=MONEY)),
    }
    if inputs.get("currency_amount") is not None:
        payment["I13_SUMA_VAL"] = _wire(
            _decimal_input(
                inputs.get("currency_amount"), "currency_amount", MONEY, minimum=Decimal(0)
            )
        )
    description = _text(inputs, "description", limit=40)
    if description:
        payment["I13_PAV"] = description
    return payment


def _payment_rows(call: Callable[..., Reply], document_code: str) -> list[dict[str, Any]]:
    """The document's I13 payment rows, from GET_I06_LIST with list=A (I06+I07+I08+I13)."""
    reply = call(
        "GET_I06_LIST",
        {"list": "A", "fil": f"i06_kodas_po={sql_literal(document_code)}"},
        None,
        mutating=False,
    )
    rows: list[dict[str, Any]] = []
    for record in reply.parsed.records:
        nested = field(record, "I13")
        if isinstance(nested, list):
            rows.extend(dict(r) for r in nested if isinstance(r, Mapping))
    rows.extend(dict(r) for r in reply.parsed.tables.get("I13", []) if isinstance(r, Mapping))
    return rows


def _same_payment(row: Mapping[str, Any], payment: Mapping[str, Any]) -> bool:
    if not _same(_value(row, "I13_KODAS_SS"), str(payment["I13_KODAS_SS"])):
        return False
    if _decimal(row, "I13_SUMA") != Decimal(str(payment["I13_SUMA"])):
        return False
    if "I13_SUMA_VAL" in payment:
        return _decimal(row, "I13_SUMA_VAL") == Decimal(str(payment["I13_SUMA_VAL"]))
    return True


def register_payment(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    payment = _payment(inputs)
    # EDIT_I13 has no idempotency key: a rerun after an unknown outcome looks for
    # the same payment (document, money account, amount) before adding another.
    for row in _payment_rows(call, payment["I13_KODAS_PO"]):
        if _same_payment(row, payment):
            return {
                "document_code": payment["I13_KODAS_PO"],
                "line_number": _value(row, "I13_EIL_NR"),
                "created": False,
                "record": dict(row),
            }
    reply = call("EDIT_I13", {"oper": "I"}, {"I13": payment}, mutating=True)
    record = _first(reply) or {}
    return {
        "document_code": payment["I13_KODAS_PO"],
        "line_number": _value(record, "I13_EIL_NR"),
        "created": True,
        "record": record,
    }


PRODUCT_INPUTS = {
    "product_code",
    "barcode",
    "name",
    "unit",
    "product_type",
    "account_relation_code",
}


def _product_by_barcode(
    call: Callable[..., Reply], barcode: str
) -> tuple[dict[str, Any] | None, str]:
    for method, column, key in (
        ("GET_N37_LIST", "n37_bar_kodas", "N37_KODAS_PS"),
        ("GET_N40_LIST", "n40_bar_kodas", "N40_KODAS_PS"),
    ):
        reply = call(method, {"fil": f"{column}={sql_literal(barcode)}"}, None, mutating=False)
        if reply.parsed.records:
            record = reply.parsed.records[0]
            return record, _value(record, key)
    return None, ""


def find_or_create_product(
    call: Callable[..., Reply], inputs: Mapping[str, Any], progress: Progress
) -> dict[str, Any]:
    _allowed(inputs, PRODUCT_INPUTS)
    code = _code(inputs, "product_code", limit=12)
    barcode = _code(inputs, "barcode", limit=25)
    if not code and not barcode:
        raise _invalid("Give product_code or barcode")
    notes: list[str] = []
    if code:
        reply = call(
            "GET_N17_LIST", {"fil": f"n17_kodas_ps={sql_literal(code)}"}, None, mutating=False
        )
        if reply.parsed.records:
            if barcode:
                by_barcode, owner = _product_by_barcode(call, barcode)
                if by_barcode is not None and owner and not _same(owner, code):
                    raise _invalid(f"Barcode {barcode} belongs to product {owner}, not {code}")
                if by_barcode is None:
                    notes.append(
                        f"Barcode {barcode} is not registered for {code}; nothing was changed"
                    )
            return {
                "product_code": code,
                "created": False,
                "matched_by": "product_code",
                "notes": notes,
                "record": reply.parsed.records[0],
            }
    if barcode:
        record, owner = _product_by_barcode(call, barcode)
        if record is not None:
            if code and owner and not _same(owner, code):
                raise _invalid(f"Barcode {barcode} belongs to product {owner}, not {code}")
            return {
                "product_code": owner,
                "created": False,
                "matched_by": "barcode",
                "notes": notes,
                "record": record,
            }
    if not code:
        raise _invalid("No product has this barcode; give product_code to create one")
    product_type = (_text(inputs, "product_type") or "product").lower()
    if product_type not in LINE_TYPES:
        raise _invalid("product_type must be product or service")
    unit = _code(inputs, "unit", required=True, limit=12)
    product: dict[str, Any] = {
        "N17_KODAS_PS": code,
        "N17_TIPAS": LINE_TYPES[product_type],
        "N17_PAV": _text(inputs, "name", required=True, limit=120),
        "N17_KODAS_US": unit,
        "N17_KODAS_DS": _code(inputs, "account_relation_code", limit=12)
        or DEFAULT_PRODUCT_ACCOUNT_RELATION,
    }
    if barcode:
        product["N40"] = {"N40_KODAS_US": unit, "N40_BAR_KODAS": barcode}
    reply = call("EDIT_N17_FULL", {"errorAction": "ROLLBACK"}, {"N17": product}, mutating=True)
    progress.add(f"product {code} created (EDIT_N17_FULL)")
    return {
        "product_code": code,
        "created": True,
        "matched_by": "",
        "notes": notes,
        "record": _first(reply) or {},
    }


def _pages(
    call: Callable[..., Reply],
    method: str,
    params: dict[str, Any],
    *,
    page_param: str,
    max_pages: int,
    start: int = 1,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Read pages until a short page, max_pages, the deadline or the output budget.

    Returns the rows, whether more remain, and the page to continue from.
    """
    rows: list[dict[str, Any]] = []
    meter = budget.Meter()
    page = start
    has_more = False
    for _ in range(max_pages):
        try:
            reply = call(method, {**params, page_param: page}, None, mutating=False)
        except DeadlineReached:
            if page == start:
                raise
            has_more = True
            break
        batch = reply.parsed.records
        if not meter.admit({"items": batch}):
            if page == start:
                raise budget.too_large()
            has_more = True
            break
        rows.extend(batch)
        has_more = reply.parsed.top_level_count >= 100
        page += 1
        if not has_more:
            break
    return rows, has_more, page if has_more else None


def _quantity(
    record: Mapping[str, Any], fractional: str, fraction: str, decimal_tag: str = ""
) -> float | None:
    """Rivile keeps quantities as integer parts of a unit fraction (I17_FRAKCIJA)."""
    direct = _decimal(record, decimal_tag) if decimal_tag else None
    if direct is not None:
        return _out(direct, QUANTITY)
    amount = _decimal(record, fractional)
    parts = _decimal(record, fraction) or Decimal(1)
    return None if amount is None else _out(amount / parts, QUANTITY)


def stock_levels(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(inputs, {"product_code", "warehouse_code", "page", "max_pages"})
    conditions = []
    product = _code(inputs, "product_code", limit=12)
    warehouse = _code(inputs, "warehouse_code", limit=12)
    if product:
        conditions.append(f"i17_kodas_ps={sql_literal(product)}")
    if warehouse:
        conditions.append(f"i17_kodas_is={sql_literal(warehouse)}")
    params: dict[str, Any] = {"group": "I17_KODAS_IS"}
    if conditions:
        params["fil"] = " and ".join(conditions)
    rows, has_more, next_page = _pages(
        call,
        "GET_I17_LIST",
        params,
        page_param="pagenumber",
        max_pages=_max_pages(inputs),
        start=_start_page(inputs),
    )
    items = [
        {
            "product_code": _value(r, "I17_KODAS_PS"),
            "unit": _value(r, "I17_KODAS_US"),
            "alt_unit": _value(r, "I17_KODAS_US_A"),
            "warehouse_code": _value(r, "I17_KODAS_IS"),
            # FIFO balance (I17_KIEKIS) in the base unit; Rivile's own decimal
            # figure when it sends one.
            "quantity": _quantity(r, "I17_KIEKIS", "I17_FRAKCIJA", "des_likutis_us"),
            "reserved": _quantity(r, "I17_REZERVAS", "I17_FRAKCIJA"),
        }
        for r in rows
    ]
    return {"items": items, "count": len(items), "has_more": has_more, "next_page": next_page}


def product_prices(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(inputs, {"product_code", "barcode", "unit", "warehouse_code", "client_code"})
    params: dict[str, Any] = {}
    for name, wire, limit in (
        ("product_code", "kodas_ps", 12),
        ("barcode", "bar_kodas", 25),
        ("unit", "kodas_us", 12),
        ("warehouse_code", "kodas_is", 12),
        ("client_code", "kodas_ks", 12),
    ):
        value = _code(inputs, name, limit=limit)
        if value:
            params[wire] = value
    if "kodas_ps" not in params and "bar_kodas" not in params:
        raise _invalid("Give product_code or barcode")
    reply = call("GET_PS_KAIN", params, None, mutating=False)
    items = [
        {
            "product_code": _value(r, "kodas_ps"),
            "unit": _value(r, "kodas_us"),
            "warehouse_code": _value(r, "kodas_is"),
            "barcode": _value(r, "bar_kodas"),
            "price": _out(_decimal(r, "kaina"), PRICE),
            "price_type": _value(r, "kain_tipas"),
            "priority": _value(r, "prioritetas"),
        }
        for r in reply.parsed.records
    ]
    result = {"items": items, "count": len(items)}
    budget.ensure_fits(result)
    return result


def _since(inputs: Mapping[str, Any]) -> tuple[str, str]:
    """(the documented lastDate form, a T-SQL datetime literal body)."""
    raw = _text(inputs, "since", required=True, limit=25)
    if not (_DATE_RE.fullmatch(raw) or _DATETIME_RE.fullmatch(raw)):
        raise _invalid("since must be YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _invalid("since is not a valid date") from exc
    if _DATE_RE.fullmatch(raw):
        return raw, raw
    # The guide's form: 2022-02-15T16:29:00.
    return moment.strftime("%Y-%m-%dT%H:%M:%S"), moment.strftime("%Y-%m-%d %H:%M:%S")


def changes_since(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(inputs, {"entity", "since", "page", "max_pages"})
    entity = _text(inputs, "entity", required=True).lower()
    if entity not in CHANGE_SOURCES:
        raise _invalid(f"entity must be one of {', '.join(CHANGE_SOURCES)}")
    last_date, sql_moment = _since(inputs)
    method, fixed = CHANGE_SOURCES[entity]
    params: dict[str, Any] = dict(fixed)
    if entity == "documents":
        params["fil"] = f"i06_r_date>='{sql_moment}'"
    else:
        params["lastDate"] = last_date
    rows, has_more, next_page = _pages(
        call,
        method,
        params,
        page_param="pagenumber",
        max_pages=_max_pages(inputs),
        start=_start_page(inputs),
    )
    return {
        "entity": entity,
        "method": method,
        "records": rows,
        "count": len(rows),
        "has_more": has_more,
        "next_page": next_page,
    }


def client_debt(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(inputs, {"client_code"})
    client = _code(inputs, "client_code", required=True, limit=12)
    # T03 codes can come padded (the guide's own example is ' 110065392 '),
    # so the comparison trims both sides.
    fil = f"ltrim(rtrim(t03_kodas_ks))={sql_literal(client)}"
    reply = call("GET_T03_LIST", {"list": "H", "fil": fil}, None, mutating=False)
    debit = credit = Decimal(0)
    for row in reply.parsed.records:
        debit += _decimal(row, "T03_SUMA_DB") or Decimal(0)
        credit += _decimal(row, "T03_SUMA_CR") or Decimal(0)
    return {
        "client_code": client,
        "debit": _out(debit),
        "credit": _out(credit),
        "balance": _out(debit - credit),
        "rows": reply.parsed.records,
    }


def validate(operation_id: str, inputs: Mapping[str, Any]) -> None:
    """Check a friendly operation's inputs without calling Rivile (test-mode backstop)."""
    inputs = {k: v for k, v in inputs.items() if k != "connection_ref"}
    if operation_id == "find_or_create_client":
        _allowed(inputs, CLIENT_INPUTS)
        if not _identifiers(inputs):
            raise _invalid("Give company_code, vat_code or client_code to look the client up")
        _client_payload(inputs, require_name=False)  # name is needed only to create
    elif operation_id in {"create_sales_invoice", "create_purchase_invoice"}:
        _allowed(inputs, INVOICE_INPUTS)
        _invoice_lines(inputs)
        _invoice_header(inputs, SALES_OP_TYPE, "X")
        if not _identifiers(inputs):
            raise _invalid(
                "Give client_code, or company_code / vat_code to find or create the client"
            )
    elif operation_id == "attach_file_to_document":
        _allowed(inputs, {"document_code", "document_file", "file_extension", "replace_existing"})
        _code(inputs, "document_code", required=True, limit=12)
        _text(inputs, "document_file", required=True, limit=512)
    elif operation_id == "download_document_file":
        _allowed(inputs, {"document_code"})
        _code(inputs, "document_code", required=True, limit=12)
    elif operation_id == "register_payment":
        _payment(inputs)
    elif operation_id == "find_or_create_product":
        _allowed(inputs, PRODUCT_INPUTS)
        if not _code(inputs, "product_code", limit=12) and not _code(inputs, "barcode", limit=25):
            raise _invalid("Give product_code or barcode")


#: Every declared output of each friendly write, as a test run returns it:
#: nothing was sent, so nothing was found or created.
NEUTRAL_OUTPUTS: dict[str, dict[str, Any]] = {
    "find_or_create_client": {
        "client_code": "",
        "created": False,
        "matched_by": "",
        "match_count": 0,
        "record": None,
    },
    "create_sales_invoice": {
        "document_code": "",
        "number": "",
        "created": False,
        "client_code": "",
        "client_created": False,
        "total_net": None,
        "total_vat": None,
        "total": None,
        "posted": False,
        "post_error": "",
        "record": None,
    },
    "attach_file_to_document": {"document_code": "", "file_extension": "", "record": None},
    "register_payment": {
        "document_code": "",
        "line_number": "",
        "created": False,
        "record": None,
    },
    "find_or_create_product": {
        "product_code": "",
        "created": False,
        "matched_by": "",
        "notes": [],
        "record": None,
    },
}
NEUTRAL_OUTPUTS["create_purchase_invoice"] = dict(NEUTRAL_OUTPUTS["create_sales_invoice"])


def neutral_result(operation_id: str) -> dict[str, Any]:
    return dict(NEUTRAL_OUTPUTS[operation_id])


def _max_pages(inputs: Mapping[str, Any]) -> int:
    return whole("max_pages", inputs.get("max_pages"), default=10, maximum=100)


def _start_page(inputs: Mapping[str, Any]) -> int:
    return whole("page", inputs.get("page"), default=1, maximum=1000000)


__all__ = [
    "CHANGE_SOURCES",
    "CLIENT_INPUTS",
    "CLIENT_TYPES",
    "INVOICE_INPUTS",
    "NEUTRAL_OUTPUTS",
    "Progress",
    "changes_since",
    "client_debt",
    "create_invoice",
    "field",
    "find_or_create_client",
    "find_or_create_product",
    "neutral_result",
    "product_prices",
    "register_payment",
    "sql_literal",
    "stock_levels",
    "validate",
]
