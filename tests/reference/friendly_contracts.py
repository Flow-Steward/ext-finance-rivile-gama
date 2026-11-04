"""Contracts of the friendly operations (runtime/friendly.py), listed first in the step picker."""

from __future__ import annotations

from typing import Any

CONNECTION = {"name": "connection_ref", "value_type": "connection_ref", "required": True}
OBJ: dict[str, Any] = {"type": "object", "additionalProperties": True}


def text(
    name: str, title: str, description: str, *, required: bool = False, **extra: Any
) -> dict[str, Any]:
    schema = {"type": "string", "title": title, "description": description, "maxLength": 150}
    schema.update(extra)
    return {"name": name, "value_type": "text", "required": required, "schema": schema}


def number(name: str, title: str, description: str, *, required: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "value_type": "number",
        "required": required,
        "schema": {"type": "number", "title": title, "description": description},
    }


def flag(name: str, title: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "value_type": "boolean",
        "required": False,
        "schema": {"type": "boolean", "title": title, "description": description},
    }


def out(name: str, value_type: str, schema_type: Any, description: str = "") -> dict[str, Any]:
    if value_type == "artifact_handle":
        return {"name": name, "value_type": value_type}
    schema: dict[str, Any] = {"type": schema_type}
    if description:
        schema["description"] = description
    return {"name": name, "value_type": value_type, "schema": schema}


CLIENT_LOOKUP = [
    text(
        "company_code", "Company code", "Registration code (Rivile N08_IM_KODAS). Looked up first."
    ),
    text(
        "vat_code",
        "VAT code",
        "VAT payer code, e.g. LT214118411 (N08_PVM_KODAS). Looked up second.",
    ),
    text(
        "client_code",
        "Client code",
        "Rivile client code (N08_KODAS_KS). Looked up last; used as the new code when creating (else Rivile numbers it).",
    ),
]
CLIENT_NEW = [
    text("name", "Name", "Client name, used when the client has to be created (N08_PAV)."),
    text("address", "Address", "N08_ADR."),
    text(
        "location_code",
        "Location code",
        "Rivile location (vietovė) code, N08_KODAS_VS. Rivile has no country field.",
    ),
    text("email", "E-mail", "N08_E_MAIL."),
    text("phone", "Phone", "N08_TEL."),
    text(
        "client_type",
        "Client type",
        "customer, supplier or both (N08_RUSIS 1/2/3).",
        enum=["customer", "supplier", "both"],
    ),
    flag(
        "legal_entity",
        "Legal entity",
        "true: company (N08_TIPAS 1, default); false: natural person (2).",
    ),
    text(
        "account_relation_code",
        "Account relation code",
        "N08_KODAS_DS, required by Rivile for buyers and suppliers. Default PT001 (the guide's example) — use your company's code.",
    ),
    text(
        "tax_table_code",
        "Tax table code",
        "N08_KODAS_XS_P / _T. Default PVM (the guide's example).",
    ),
]
CLIENT_OUT = [
    out("client_code", "text", "string", "The Rivile client code."),
    out("created", "boolean", "boolean", "true when the client was created by this step."),
    out(
        "matched_by", "text", "string", "company_code, vat_code or client_code; empty when created."
    ),
    out(
        "match_count",
        "integer",
        "integer",
        "How many clients matched (Rivile may allow duplicates).",
    ),
    out("record", "object", ["object", "null"], "The Rivile N08 record."),
]
LINE_SCHEMA = {
    "type": "array",
    "title": "Lines",
    "description": "Document lines.",
    "minItems": 1,
    "maxItems": 500,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["product_code", "quantity", "unit_price"],
        "properties": {
            "product_code": {
                "type": "string",
                "description": "Product or service code (I07_KODAS).",
            },
            "line_type": {
                "type": "string",
                "enum": ["product", "service"],
                "description": "I07_TIPAS 1/2; default product.",
            },
            "quantity": {
                "type": ["number", "string"],
                "description": "Quantity in decimals (KIEKIS_U).",
            },
            "unit": {
                "type": "string",
                "description": "Unit code, e.g. VNT (I07_KODAS_US); default the product's unit.",
            },
            "unit_price": {
                "type": ["number", "string"],
                "description": "Price per unit (I07_KAINA_BE, I07_KAINA_SU with prices_include_vat, I07_VAL_KAINA with currency).",
            },
            "vat_rate": {
                "type": ["number", "string"],
                "description": "VAT % (I07_MOKESTIS=1, I07_MOKESTIS_P). Omit to use the product/client tax table.",
            },
            "vat_code": {
                "type": "string",
                "description": "VAT classifier code (I07_KODAS_KL, Rivile 109+).",
            },
            "discount": {"type": ["number", "string"], "description": "Discount % (I07_NUOLAIDA)."},
            "department_code": {
                "type": "string",
                "description": "Department / warehouse (I07_KODAS_IS); overrides the document's.",
            },
            "name": {"type": "string", "description": "Line name (I07_PAV)."},
        },
    },
}


def invoice(kind: str) -> dict[str, Any]:
    sale = kind == "sales"
    op_type = 51 if sale else 1
    inputs = [
        CONNECTION,
        *CLIENT_LOOKUP,
        *CLIENT_NEW,
        text(
            "number",
            "Number",
            "Document number (I06_DOK_NR); omit when Rivile numbers documents automatically.",
        ),
        text(
            "date",
            "Date",
            "Document date YYYY-MM-DD (I06_OP_DATA, I06_DOK_DATA); default today.",
            format="date",
        ),
        text(
            "due_date",
            "Due date",
            "Payment due date YYYY-MM-DD; sent as I08 payment terms (100 % after N days).",
            format="date",
        ),
        text(
            "currency",
            "Currency",
            "Currency code (I06_KODAS_VL); line prices are then sent as I07_VAL_KAINA.",
        ),
        text("department_code", "Department / warehouse", "Default I07_KODAS_IS for every line."),
        flag(
            "prices_include_vat",
            "Prices include VAT",
            "Line prices are gross (I06_PVM_TIP=1, I07_KAINA_SU).",
        ),
        text("notes", "Notes", "I06_PASTABOS (40 characters)."),
        {"name": "lines", "value_type": "array", "required": True, "schema": LINE_SCHEMA},
        flag(
            "post",
            "Post the document",
            "After creating it, post it (EDIT_I06 oper P). A failed posting leaves the document unposted and is reported in post_error.",
        ),
    ]
    outputs = [
        out("document_code", "text", "string", "Rivile operation code (I06_KODAS_PO)."),
        out("number", "text", "string"),
        out("client_code", "text", "string"),
        out("client_created", "boolean", "boolean"),
        out("total_net", "number", ["number", "null"]),
        out("total_vat", "number", ["number", "null"]),
        out("total", "number", ["number", "null"]),
        out(
            "posted",
            "boolean",
            ["boolean", "null"],
            "true posted; false not posted; null outcome unknown.",
        ),
        out("post_error", "text", "string"),
        out("record", "object", ["object", "null"]),
    ]
    title = "Create sales invoice" if sale else "Create purchase invoice"
    return {
        "operation_id": f"create_{kind}_invoice",
        "display_name": title,
        "description": (
            f"Creates a {'sales' if sale else 'purchase'} invoice (EDIT_I06_FULL, I06_OP_TIP {op_type}) "
            "with its lines in one request, finding or creating the "
            f"{'customer' if sale else 'supplier'} first when company_code or vat_code is given; "
            "optionally posts it. A failed line rolls the whole document back."
        ),
        "kind": "action",
        "inputs": inputs,
        "outputs": outputs,
    }


def operations() -> list[dict[str, Any]]:
    paging = {
        "name": "max_pages",
        "value_type": "integer",
        "required": False,
        "schema": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "title": "Max pages",
            "description": "Pages of 100 to read; default 10.",
        },
    }
    more = [out("has_more", "boolean", "boolean"), out("next_page", "integer", ["integer", "null"])]
    item_list = out("items", "array", "array", "Flat rows.")
    return [
        {
            "operation_id": "find_or_create_client",
            "display_name": "Find or create customer / supplier",
            "description": "Looks the client up by company code, then VAT code, then client code (GET_N08_LIST); creates it (EDIT_N08) only when none matches, so running it again is safe.",
            "kind": "action",
            "inputs": [CONNECTION, *CLIENT_LOOKUP, *CLIENT_NEW],
            "outputs": CLIENT_OUT,
        },
        invoice("sales"),
        invoice("purchase"),
        {
            "operation_id": "attach_file_to_document",
            "display_name": "Attach file to document",
            "description": "Attaches a workflow file (e.g. the invoice PDF) to a purchase or sales document (EDIT_I06_DOC). Rivile is not asked to echo the file back.",
            "kind": "action",
            "inputs": [
                CONNECTION,
                text(
                    "document_code",
                    "Document code",
                    "Rivile operation code (I06_KODAS_PO), e.g. from Create sales invoice.",
                    required=True,
                ),
                {
                    "name": "document_file",
                    "value_type": "artifact_handle",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "maxLength": 512,
                        "title": "File",
                        "description": "A workflow file, up to 15 MB.",
                    },
                },
                text(
                    "file_extension",
                    "File extension",
                    "e.g. .pdf; default from the file name or content.",
                ),
                flag(
                    "replace_existing",
                    "Replace existing",
                    "Overwrite a file already attached (overwrite=1). Default false: Rivile refuses if one exists.",
                ),
            ],
            "outputs": [
                out("document_code", "text", "string"),
                out("file_extension", "text", "string"),
                out("record", "object", ["object", "null"]),
            ],
        },
        {
            "operation_id": "download_document_file",
            "display_name": "Download document file",
            "description": "Downloads the file attached to a purchase or sales document (GET_I06_DOC) as a workflow file.",
            "kind": "source",
            "inputs": [
                CONNECTION,
                text(
                    "document_code",
                    "Document code",
                    "Rivile operation code (I06_KODAS_PO).",
                    required=True,
                ),
            ],
            "outputs": [
                out("document_code", "text", "string"),
                out("found", "boolean", "boolean"),
                out("document_file", "artifact_handle", None),
                out("original_filename", "text", ["string", "null"]),
                out("original_content_type", "text", ["string", "null"]),
                out(
                    "link", "text", "string", "NUORODA when Rivile stores a link instead of a file."
                ),
            ],
        },
        {
            "operation_id": "register_payment",
            "display_name": "Register payment",
            "description": "Records a payment against a purchase or sales document (EDIT_I13: I13_KODAS_PO, I13_KODAS_SS, I13_SUMA).",
            "kind": "action",
            "inputs": [
                CONNECTION,
                text(
                    "document_code",
                    "Document code",
                    "Rivile operation code (I06_KODAS_PO).",
                    required=True,
                ),
                number("amount", "Amount", "Paid amount (I13_SUMA).", required=True),
                text(
                    "money_account_code",
                    "Money account",
                    "Rivile money account code (I13_KODAS_SS), e.g. the bank account.",
                    required=True,
                ),
                number(
                    "currency_amount", "Amount in currency", "I13_SUMA_VAL for currency documents."
                ),
                text("description", "Description", "I13_PAV (40 characters)."),
            ],
            "outputs": [
                out("document_code", "text", "string"),
                out("line_number", "text", "string"),
                out("record", "object", ["object", "null"]),
            ],
        },
        {
            "operation_id": "find_or_create_product",
            "display_name": "Find or create product",
            "description": "Looks a product up by code (GET_N17_LIST) or barcode (GET_N37_LIST, then GET_N40_LIST); creates it with its unit and barcode (EDIT_N17_FULL) only when none matches.",
            "kind": "action",
            "inputs": [
                CONNECTION,
                text("product_code", "Product code", "N17_KODAS_PS; required to create."),
                text(
                    "barcode",
                    "Barcode",
                    "Looked up in N37 and N40; added as an N40 barcode when creating.",
                ),
                text("name", "Name", "N17_PAV, required to create."),
                text(
                    "unit", "Unit", "Base unit code, e.g. VNT (N17_KODAS_US), required to create."
                ),
                text(
                    "product_type",
                    "Type",
                    "product or service (N17_TIPAS 1/2).",
                    enum=["product", "service"],
                ),
                text(
                    "account_relation_code",
                    "Account relation code",
                    "N17_KODAS_DS, required by Rivile. Default PR001 (the guide's example) — use your company's code.",
                ),
            ],
            "outputs": [
                out("product_code", "text", "string"),
                out("created", "boolean", "boolean"),
                out("matched_by", "text", "string"),
                out("record", "object", ["object", "null"]),
            ],
        },
        {
            "operation_id": "stock_levels",
            "display_name": "Stock levels",
            "description": "Stock per product and warehouse (GET_I17_LIST grouped by I17_KODAS_IS), as plain decimal quantities.",
            "kind": "source",
            "inputs": [
                CONNECTION,
                text("product_code", "Product code", "Only this product."),
                text(
                    "warehouse_code",
                    "Warehouse / department",
                    "Only this department (I17_KODAS_IS).",
                ),
                paging,
            ],
            "outputs": [item_list, out("count", "integer", "integer"), *more],
        },
        {
            "operation_id": "product_prices",
            "display_name": "Product prices",
            "description": "Every price, price list, promotion and discount Rivile resolves for a product (GET_PS_KAIN), flattened.",
            "kind": "source",
            "inputs": [
                CONNECTION,
                text("product_code", "Product code", "kodas_ps; give this or barcode."),
                text("barcode", "Barcode", "bar_kodas."),
                text("unit", "Unit", "kodas_us."),
                text("warehouse_code", "Warehouse / department", "kodas_is."),
                text("client_code", "Client code", "Prices for this client (kodas_ks)."),
            ],
            "outputs": [item_list, out("count", "integer", "integer")],
        },
        {
            "operation_id": "changes_since",
            "display_name": "Changes since",
            "description": "Clients, products, prices or stock changed since a date (the *_TIME methods; run Enable change tracking (INIT_TRIGGER) once first), or documents corrected since then (GET_I06_LIST by I06_R_DATE).",
            "kind": "source",
            "inputs": [
                CONNECTION,
                text(
                    "entity",
                    "What",
                    "clients, products, prices, stock or documents.",
                    required=True,
                    enum=["clients", "products", "prices", "stock", "documents"],
                ),
                text("since", "Since", "YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS].", required=True),
                paging,
            ],
            "outputs": [
                out("entity", "text", "string"),
                out("method", "text", "string"),
                out("records", "array", "array"),
                out("count", "integer", "integer"),
                *more,
            ],
        },
        {
            "operation_id": "client_debt",
            "display_name": "Client debt",
            "description": "Debt totals of a client or supplier (GET_T03_LIST): debit, credit and balance (debit − credit).",
            "kind": "source",
            "inputs": [
                CONNECTION,
                text("client_code", "Client code", "N08_KODAS_KS.", required=True),
            ],
            "outputs": [
                out("client_code", "text", "string"),
                out("debit", "number", "number"),
                out("credit", "number", "number"),
                out("balance", "number", "number"),
                out("rows", "array", "array"),
            ],
        },
    ]
