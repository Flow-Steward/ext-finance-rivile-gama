"""Friendly operations: plain inputs, several Rivile calls in one step.

Every mapping below comes from the Rivile GAMA API v2 guide (method pages and
their "Lentelės" field tables). Mappings the guide leaves to each company's own
setup (account relation and tax table codes, money accounts) are explicit
inputs with the guide's example value as default; README lists them as
unverified without a live key.

Composite writes report exactly what they did. When a later call fails after an
earlier write succeeded, the step fails with "may have an effect", is not
retried automatically, and its message lists what was already created. The
find-or-create operations look up first, so running them again is safe.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from . import budget, catalog, errors
from .errors import ExtensionError
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
CHANGE_SOURCES = {
    "clients": ("GET_CLIENTS_TIME", "lastDate"),
    "products": ("GET_PROD_BY_BARCODE_TIME", "lastDate"),
    "prices": ("GET_PRICES_TIME", "lastDate"),
    "stock": ("GET_I17_LIST_TIME", "lastDate"),
    "documents": ("GET_I06_LIST", ""),
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")


# ---------------------------------------------------------------------------
# Shared helpers


def _invalid(message: str) -> ExtensionError:
    return ExtensionError(
        errors.INVALID_PAYLOAD,
        message,
        definitely_no_external_effect=True,
        external_effect_status="failed",
    )


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


def _number(value: Any, name: str, *, minimum: float | None = None) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str) and re.fullmatch(
            r"-?[0-9]{1,15}([.,][0-9]{1,6})?", value.strip()
        ):
            value = float(value.strip().replace(",", "."))
        else:
            raise _invalid(f"{name} must be a number")
    if minimum is not None and value < minimum:
        raise _invalid(f"{name} must be at least {minimum}")
    return value


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
    value = inputs.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid(f"{name} must be true or false")
    return value


def sql_literal(value: str) -> str:
    """A T-SQL string literal for a Rivile ``fil`` condition."""
    if any(ord(ch) < 32 for ch in value) or len(value) > MAX_TEXT:
        raise _invalid("A lookup value contains characters that cannot be searched")
    return "'" + value.replace("'", "''") + "'"


def _allowed(inputs: Mapping[str, Any], names: set[str]) -> None:
    unknown = set(inputs) - names - {"connection_ref"}
    if unknown:
        raise _invalid(f"Unknown input: {', '.join(sorted(unknown))}")


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


def _first(reply: Reply) -> dict[str, Any] | None:
    records = reply.parsed.records
    return records[0] if records else None


def _value(record: Mapping[str, Any] | None, *names: str) -> str:
    if not isinstance(record, Mapping):
        return ""
    for name in names:
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _decimal(record: Mapping[str, Any], name: str) -> float | None:
    raw = record.get(name)
    if not isinstance(raw, str) or not re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", raw.strip()):
        return None
    return float(raw.strip())


def _quantity(
    record: Mapping[str, Any], fractional: str, fraction: str, decimal_tag: str
) -> float | None:
    """Rivile keeps quantities as integer parts of a unit fraction (I07_FRAKCIJA)."""
    direct = _decimal(record, decimal_tag)
    if direct is not None:
        return direct
    amount = _decimal(record, fractional)
    parts = _decimal(record, fraction) or 1.0
    return None if amount is None else amount / parts


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


def _find_client(
    call: Callable[..., Reply], inputs: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str, int]:
    for name, column in (
        ("company_code", "n08_im_kodas"),
        ("vat_code", "n08_pvm_kodas"),
        ("client_code", "n08_kodas_ks"),
    ):
        value = _text(inputs, name, limit=35)
        if not value:
            continue
        reply = call(
            "GET_N08_LIST",
            {"list": "H", "fil": f"{column}={sql_literal(value)}"},
            None,
            mutating=False,
        )
        if reply.parsed.records:
            return reply.parsed.records[0], name, len(reply.parsed.records)
    return None, "", 0


def _client_payload(inputs: Mapping[str, Any], *, require_name: bool = True) -> dict[str, Any]:
    client_type = (_text(inputs, "client_type") or "customer").lower()
    if client_type not in CLIENT_TYPES:
        raise _invalid("client_type must be customer, supplier or both")
    name = _text(inputs, "name", required=require_name)
    tax_table = _text(inputs, "tax_table_code", limit=12) or DEFAULT_TAX_TABLE
    record: dict[str, Any] = {
        "N08_PAV": name,
        "N08_RUSIS": CLIENT_TYPES[client_type],
        "N08_TIPAS": 1 if _bool(inputs, "legal_entity", True) else 2,
        "N08_KODAS_DS": _text(inputs, "account_relation_code", limit=12)
        or DEFAULT_CLIENT_ACCOUNT_RELATION,
    }
    if client_type in {"customer", "both"}:
        record["N08_KODAS_XS_P"] = tax_table
    if client_type in {"supplier", "both"}:
        record["N08_KODAS_XS_T"] = tax_table
    for name_, field in (
        ("client_code", "N08_KODAS_KS"),
        ("company_code", "N08_IM_KODAS"),
        ("vat_code", "N08_PVM_KODAS"),
        ("address", "N08_ADR"),
        ("location_code", "N08_KODAS_VS"),
        ("email", "N08_E_MAIL"),
        ("phone", "N08_TEL"),
    ):
        value = _text(inputs, name_)
        if value:
            record[field] = value
    return record


def find_or_create_client(
    call: Callable[..., Reply], inputs: Mapping[str, Any], progress: Progress
) -> dict[str, Any]:
    if not any(_text(inputs, n, limit=35) for n in ("company_code", "vat_code", "client_code")):
        raise _invalid("Give company_code, vat_code or client_code to look the client up")
    found, matched_by, matches = _find_client(call, inputs)
    if found is not None:
        return {
            "client_code": _value(found, "N08_KODAS_KS"),
            "created": False,
            "matched_by": matched_by,
            "match_count": matches,
            "record": found,
        }
    payload = _client_payload(inputs)
    reply = call("EDIT_N08", {"oper": "I"}, {"N08": payload}, mutating=True)
    record = _first(reply) or {}
    code = _value(record, "N08_KODAS_KS") or str(payload.get("N08_KODAS_KS", ""))
    progress.add(f"client {code or payload['N08_PAV']} created (EDIT_N08)")
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


def _invoice_lines(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = inputs.get("lines")
    if not isinstance(raw, list) or not raw:
        raise _invalid("lines must be a non-empty list")
    if len(raw) > MAX_LINES:
        raise _invalid(f"At most {MAX_LINES} lines per document")
    header_department = _text(inputs, "department_code", limit=12)
    with_vat = _bool(inputs, "prices_include_vat", False)
    in_currency = bool(_text(inputs, "currency", limit=12))
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
        department = _text(item, "department_code", limit=12) or header_department
        if not department:
            raise _invalid(f"{where}: department_code is required (Rivile I07_KODAS_IS)")
        line: dict[str, Any] = {
            "I07_TIPAS": LINE_TYPES[line_type],
            "I07_KODAS": _text(item, "product_code", required=True, limit=12),
            "I07_KODAS_IS": department,
            "KIEKIS_U": _number(item.get("quantity"), f"{where}.quantity", minimum=0),
        }
        price = _number(item.get("unit_price"), f"{where}.unit_price", minimum=0)
        if in_currency:
            line["I07_VAL_KAINA"] = price
        else:
            line["I07_KAINA_SU" if with_vat else "I07_KAINA_BE"] = price
        unit = _text(item, "unit", limit=12)
        if unit:
            line["I07_KODAS_US"] = unit
        if item.get("discount") is not None:
            line["I07_NUOLAIDA"] = _number(item.get("discount"), f"{where}.discount", minimum=0)
        if item.get("vat_rate") is not None:
            line["I07_MOKESTIS"] = 1
            line["I07_MOKESTIS_P"] = _number(item.get("vat_rate"), f"{where}.vat_rate", minimum=0)
        vat_code = _text(item, "vat_code", limit=12)
        if vat_code:
            line["I07_KODAS_KL"] = vat_code
        name = _text(item, "name", limit=40)
        if name:
            line["I07_PAV"] = name
        lines.append(line)
    return lines


def _invoice_header(inputs: Mapping[str, Any], op_type: int, client_code: str) -> dict[str, Any]:
    doc_date = _date(inputs.get("date"), "date") or date.today().isoformat()
    header: dict[str, Any] = {
        "I06_OP_TIP": op_type,
        "I06_KODAS_KS": client_code,
        "I06_OP_DATA": doc_date,
        "I06_DOK_DATA": doc_date,
    }
    number = _text(inputs, "number", limit=20)
    if number:
        header["I06_DOK_NR"] = number
    currency = _text(inputs, "currency", limit=12)
    if currency:
        header["I06_KODAS_VL"] = currency
    if _bool(inputs, "prices_include_vat", False):
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


def create_invoice(
    call: Callable[..., Reply], inputs: Mapping[str, Any], progress: Progress, *, op_type: int
) -> dict[str, Any]:
    lines = _invoice_lines(inputs)
    client_code = _text(inputs, "client_code", limit=12)
    client_created = False
    has_lookup = any(_text(inputs, n, limit=35) for n in ("company_code", "vat_code"))
    if has_lookup or (client_code and _text(inputs, "name")):
        defaults = {"client_type": "customer" if op_type == SALES_OP_TYPE else "supplier"}
        client = find_or_create_client(call, {**defaults, **inputs}, progress)
        client_code, client_created = client["client_code"], client["created"]
    if not client_code:
        raise _invalid("Give client_code, or company_code / vat_code to find or create the client")
    header = _invoice_header(inputs, op_type, client_code)
    header["I07"] = lines
    try:
        reply = call("EDIT_I06_FULL", {"errorAction": "ROLLBACK"}, {"I06": header}, mutating=True)
    except ExtensionError as exc:
        raise progress.fail(exc, "Creating the invoice") from exc
    record = _first(reply) or {}
    code = _value(record, "I06_KODAS_PO")
    progress.add(f"document {code or header.get('I06_DOK_NR', '')} created (EDIT_I06_FULL)")
    posted: bool | None = None
    post_error = ""
    if _bool(inputs, "post", False):
        try:
            call("EDIT_I06", {"oper": "P"}, {"I06": {"I06_KODAS_PO": code}}, mutating=True)
            posted = True
        except ExtensionError as exc:
            # The invoice exists; posting it can be done again by hand or by a
            # later step. Unknown outcomes stay unknown.
            posted = None if exc.code in errors.AMBIGUOUS_ERROR_CODES else False
            post_error = exc.message
    net = _decimal(record, "I06_SUMA")
    vat = _decimal(record, "I06_SUMA_PVM")
    return {
        "document_code": code,
        "number": _value(record, "I06_DOK_NR") or header.get("I06_DOK_NR", ""),
        "client_code": client_code,
        "client_created": client_created,
        "total_net": net,
        "total_vat": vat,
        "total": None if net is None or vat is None else round(net + vat, 2),
        "posted": posted,
        "post_error": post_error,
        "record": record,
    }


# ---------------------------------------------------------------------------
# Payments, products, stock, prices, changes, debts


def _payment(inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(
        inputs, {"document_code", "amount", "money_account_code", "currency_amount", "description"}
    )
    payment: dict[str, Any] = {
        "I13_KODAS_PO": _text(inputs, "document_code", required=True, limit=12),
        "I13_KODAS_SS": _text(inputs, "money_account_code", required=True, limit=12),
        "I13_SUMA": _number(inputs.get("amount"), "amount"),
    }
    if inputs.get("currency_amount") is not None:
        payment["I13_SUMA_VAL"] = _number(inputs.get("currency_amount"), "currency_amount")
    description = _text(inputs, "description", limit=40)
    if description:
        payment["I13_PAV"] = description
    return payment


def register_payment(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    payment = _payment(inputs)
    reply = call("EDIT_I13", {"oper": "I"}, {"I13": payment}, mutating=True)
    record = _first(reply) or {}
    return {
        "document_code": payment["I13_KODAS_PO"],
        "line_number": _value(record, "I13_EIL_NR"),
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


def find_or_create_product(
    call: Callable[..., Reply], inputs: Mapping[str, Any], progress: Progress
) -> dict[str, Any]:
    _allowed(inputs, PRODUCT_INPUTS)
    code = _text(inputs, "product_code", limit=12)
    barcode = _text(inputs, "barcode", limit=25)
    if not code and not barcode:
        raise _invalid("Give product_code or barcode")
    lookups = []
    if code:
        lookups.append(
            ("GET_N17_LIST", f"n17_kodas_ps={sql_literal(code)}", "product_code", "N17_KODAS_PS")
        )
    if barcode:
        lookups.append(
            ("GET_N37_LIST", f"n37_bar_kodas={sql_literal(barcode)}", "barcode", "N37_KODAS_PS")
        )
        lookups.append(
            ("GET_N40_LIST", f"n40_bar_kodas={sql_literal(barcode)}", "barcode", "N40_KODAS_PS")
        )
    for method, fil, matched_by, key in lookups:
        reply = call(method, {"fil": fil}, None, mutating=False)
        if reply.parsed.records:
            record = reply.parsed.records[0]
            return {
                "product_code": _value(record, key),
                "created": False,
                "matched_by": matched_by,
                "record": record,
            }
    if not code:
        raise _invalid("No product has this barcode; give product_code to create one")
    product_type = (_text(inputs, "product_type") or "product").lower()
    if product_type not in LINE_TYPES:
        raise _invalid("product_type must be product or service")
    unit = _text(inputs, "unit", required=True, limit=12)
    product: dict[str, Any] = {
        "N17_KODAS_PS": code,
        "N17_TIPAS": LINE_TYPES[product_type],
        "N17_PAV": _text(inputs, "name", required=True, limit=120),
        "N17_KODAS_US": unit,
        "N17_KODAS_DS": _text(inputs, "account_relation_code", limit=12)
        or DEFAULT_PRODUCT_ACCOUNT_RELATION,
    }
    if barcode:
        product["N40"] = {"N40_KODAS_US": unit, "N40_BAR_KODAS": barcode}
    reply = call("EDIT_N17_FULL", {"errorAction": "ROLLBACK"}, {"N17": product}, mutating=True)
    progress.add(f"product {code} created (EDIT_N17_FULL)")
    return {"product_code": code, "created": True, "matched_by": "", "record": _first(reply) or {}}


def _pages(
    call: Callable[..., Reply],
    method: str,
    params: dict[str, Any],
    *,
    page_param: str,
    max_pages: int,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Read pages until a short page, max_pages, the deadline or the output budget.

    Returns the rows, whether more remain, and the page to continue from.
    """
    rows: list[dict[str, Any]] = []
    meter = budget.Meter()
    page = 1
    has_more = False
    for _ in range(max_pages):
        try:
            reply = call(method, {**params, page_param: page}, None, mutating=False)
        except DeadlineReached:
            if page == 1:
                raise
            has_more = True
            break
        batch = reply.parsed.records
        if not meter.admit({"items": batch}):
            if page == 1:
                raise budget.too_large()
            has_more = True
            break
        rows.extend(batch)
        has_more = reply.parsed.top_level_count >= 100
        page += 1
        if not has_more:
            break
    return rows, has_more, page if has_more else None


def stock_levels(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(inputs, {"product_code", "warehouse_code", "max_pages"})
    conditions = []
    product = _text(inputs, "product_code", limit=12)
    warehouse = _text(inputs, "warehouse_code", limit=12)
    if product:
        conditions.append(f"i17_kodas_ps={sql_literal(product)}")
    if warehouse:
        conditions.append(f"i17_kodas_is={sql_literal(warehouse)}")
    params: dict[str, Any] = {"group": "I17_KODAS_IS"}
    if conditions:
        params["fil"] = " and ".join(conditions)
    rows, has_more, next_page = _pages(
        call, "GET_I17_LIST", params, page_param="pagenumber", max_pages=_max_pages(inputs)
    )
    items = [
        {
            "product_code": _value(r, "I17_KODAS_PS"),
            "unit": _value(r, "I17_KODAS_US"),
            "warehouse_code": _value(r, "I17_KODAS_IS"),
            "quantity": _quantity(r, "I17_KIEKIS", "I17_FRAKCIJA", "des_likutis_us"),
            "reserved": _quantity(r, "I17_REZERVAS", "I17_FRAKCIJA", "des_rezervas_us"),
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
        value = _text(inputs, name, limit=limit)
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
            "price": _decimal(r, "kaina"),
            "price_type": _value(r, "kain_tipas"),
            "priority": _value(r, "prioritetas"),
        }
        for r in reply.parsed.records
    ]
    result = {"items": items, "count": len(items)}
    budget.ensure_fits(result)
    return result


def changes_since(call: Callable[..., Reply], inputs: Mapping[str, Any]) -> dict[str, Any]:
    _allowed(inputs, {"entity", "since", "max_pages"})
    entity = _text(inputs, "entity", required=True).lower()
    if entity not in CHANGE_SOURCES:
        raise _invalid(f"entity must be one of {', '.join(CHANGE_SOURCES)}")
    since_raw = _text(inputs, "since", required=True, limit=25)
    if not (_DATE_RE.fullmatch(since_raw) or _DATETIME_RE.fullmatch(since_raw)):
        raise _invalid("since must be YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]")
    try:
        datetime.fromisoformat(since_raw)
    except ValueError as exc:
        raise _invalid("since is not a valid date") from exc
    since = since_raw.replace("T", " ")
    method, date_param = CHANGE_SOURCES[entity]
    params: dict[str, Any] = (
        {date_param: since}
        if date_param
        else {"list": "H", "fil": f"i06_r_date>={sql_literal(since)}"}
    )
    rows, has_more, next_page = _pages(
        call, method, params, page_param="pagenumber", max_pages=_max_pages(inputs)
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
    client = _text(inputs, "client_code", required=True, limit=12)
    reply = call(
        "GET_T03_LIST",
        {"list": "H", "fil": f"t03_kodas_ks={sql_literal(client)}"},
        None,
        mutating=False,
    )
    debit = credit = 0.0
    for row in reply.parsed.records:
        debit += _decimal(row, "T03_SUMA_DB") or 0.0
        credit += _decimal(row, "T03_SUMA_CR") or 0.0
    return {
        "client_code": client,
        "debit": round(debit, 2),
        "credit": round(credit, 2),
        "balance": round(debit - credit, 2),
        "rows": reply.parsed.records,
    }


def validate(operation_id: str, inputs: Mapping[str, Any]) -> None:
    """Check a friendly operation's inputs without calling Rivile (test-mode backstop)."""
    inputs = {k: v for k, v in inputs.items() if k != "connection_ref"}
    if operation_id == "find_or_create_client":
        _allowed(inputs, CLIENT_INPUTS)
        if not any(_text(inputs, n, limit=35) for n in ("company_code", "vat_code", "client_code")):
            raise _invalid("Give company_code, vat_code or client_code to look the client up")
        _client_payload(inputs, require_name=False)  # name is needed only to create
    elif operation_id in {"create_sales_invoice", "create_purchase_invoice"}:
        _allowed(inputs, INVOICE_INPUTS)
        _invoice_lines(inputs)
        _invoice_header(inputs, SALES_OP_TYPE, "X")
        if not any(_text(inputs, n, limit=35) for n in ("client_code", "company_code", "vat_code")):
            raise _invalid(
                "Give client_code, or company_code / vat_code to find or create the client"
            )
    elif operation_id == "attach_file_to_document":
        _allowed(inputs, {"document_code", "document_file", "file_extension", "replace_existing"})
        _text(inputs, "document_code", required=True, limit=12)
        _text(inputs, "document_file", required=True, limit=512)
    elif operation_id == "download_document_file":
        _allowed(inputs, {"document_code"})
        _text(inputs, "document_code", required=True, limit=12)
    elif operation_id == "register_payment":
        _payment(inputs)
    elif operation_id == "find_or_create_product":
        _allowed(inputs, PRODUCT_INPUTS)
        if not _text(inputs, "product_code", limit=12) and not _text(inputs, "barcode", limit=25):
            raise _invalid("Give product_code or barcode")


def _max_pages(inputs: Mapping[str, Any]) -> int:
    value = inputs.get("max_pages", 10)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise _invalid("max_pages must be a whole number from 1 to 100")
    return value


__all__ = [
    "CHANGE_SOURCES",
    "CLIENT_INPUTS",
    "CLIENT_TYPES",
    "INVOICE_INPUTS",
    "Progress",
    "catalog",
    "changes_since",
    "client_debt",
    "create_invoice",
    "find_or_create_client",
    "find_or_create_product",
    "product_prices",
    "register_payment",
    "sql_literal",
    "stock_levels",
    "validate",
]
