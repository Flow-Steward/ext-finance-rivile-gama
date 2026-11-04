"""Render the UI pages that restate the operation registry, so they cannot drift.

* ``ui/pages/operations.yaml``        — the everyday operations, in full detail, and how
  operations behave
* ``ui/pages/advanced-methods.yaml``  — every raw Rivile method, grouped by area, and coverage
* ``ui/pages/changelog.yaml``         — ``CHANGELOG.md``

The platform renders page bodies as simple markdown: headings, paragraphs, bullet
lists and code. Tables are not rendered, so no page uses them
(``tests/test_contracts.py`` checks that).

Called by ``build_contracts.py``; the inputs are the operations it builds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS_BASE = "https://gidas.rivile.lt/internetiniai_moduliai/api_rest/"
ADVANCED_PREFIX = "Advanced · "

#: The Rivile methods each everyday operation calls.
FRIENDLY_METHODS: dict[str, tuple[str, ...]] = {
    "test_connection": ("GET_N45_LIST",),
    "find_or_create_client": ("GET_N08_LIST", "EDIT_N08"),
    "create_sales_invoice": (
        "GET_N08_LIST",
        "EDIT_N08",
        "GET_I06_LIST",
        "EDIT_I06_FULL",
        "EDIT_I06",
    ),
    "create_purchase_invoice": (
        "GET_N08_LIST",
        "EDIT_N08",
        "GET_I06_LIST",
        "EDIT_I06_FULL",
        "EDIT_I06",
    ),
    "attach_file_to_document": ("EDIT_I06_DOC",),
    "download_document_file": ("GET_I06_DOC",),
    "register_payment": ("EDIT_I13",),
    "find_or_create_product": ("GET_N17_LIST", "GET_N37_LIST", "GET_N40_LIST", "EDIT_N17_FULL"),
    "stock_levels": ("GET_I17_LIST",),
    "product_prices": ("GET_PS_KAIN",),
    "changes_since": (
        "GET_CLIENTS_TIME",
        "GET_PROD_BY_BARCODE_TIME",
        "GET_PRICES_TIME",
        "GET_I17_LIST_TIME",
        "GET_I06_LIST",
    ),
    "client_debt": ("GET_T03_LIST",),
}

#: Results every raw method returns; listed once at the top of the Advanced page.
COMMON_RESULTS = (
    "method",
    "records",
    "record",
    "record_type",
    "other_tables",
    "count",
    "data_errors",
    "partial_success",
    "http_status",
    "duration_ms",
)
WRITE_RESULTS = ("external_effect_status", "definitely_no_external_effect")
LIST_RESULTS = ("page_number", "next_page", "pages_fetched", "has_more")

BEHAVIOUR = """\
### Inputs

- Every step needs a **connection**: the Rivilė API key it runs with.
- Everyday steps take plain inputs. Codes your company sets up its own way — the account relation code for new clients (`PT001`) and products (`PR001`), the tax table (`PVM`), money accounts — are inputs; the defaults are the Rivilė guide's examples, so replace them with your own.
- Advanced methods take Rivilė's params as separate inputs and a `data` block with Rivilė's own field names, as in the Rivilė guide, for example `{"N08": {"N08_KODAS_KS": "121411842", "N08_PAV": "UAB X"}}`. Document methods ending in `_FULL` take their lines inside the header.

### Results

- Values are text exactly as Rivilė sent them. With **Trim values** on, only trailing padding is removed; leading spaces in Rivilė codes are kept.
- Rivilė's refusals per field are in `data_errors` (tag, code, message).

### Lists

- Rivilė returns 100 records per page. Set `page_number`, or turn on `fetch_all_pages` (up to `max_pages`).
- A step output has a size limit. A long read stops at the last page that fits and returns `has_more` and `next_page`. For whole tables use **Export a Rivile list to a JSONL file**.

### Files

- **Attach file to document** takes a workflow file, for example the invoice PDF from an earlier step.
- **Download document file** saves the attachment as a workflow file.

### Writes, retries and errors

- Every write goes through Flow Steward's external-effect record, so a retried step does not send the same write twice. Test runs never write.
- **HTTP 400:** Rivilė refused the data and nothing was saved. The step fails with Rivilė's message.
- **HTTP 207:** part of a document was saved. The step succeeds with `partial_success: true`; check `data_errors`.
- **Gateway unavailable (502/503) on a read:** retried.
- **Timeout or 5xx on a write:** Rivilė may have saved it. The step fails as `timeout_unknown` and is not retried; check the document in Rivilė first.
- **Record locked (423):** another user is editing the record. Retried after 1, 2, 4 and 8 seconds.
- **Rate limit (429):** Rivilė allows 10 requests per second per key. The extension paces itself and backs off. Steps running in parallel on one key share that limit.
- If a step that writes several things fails half-way, it says exactly what was already created and is not retried.

### Empty values: MS SQL and Sybase

- **Field left out:** Rivilė applies its default on both servers.
- **Empty text `""`:** MS SQL stores it empty; Sybase treats it like a left-out field and applies the default.
- **One space `" "`:** on Sybase, the way to store an empty value.
"""


class _PageDumper(yaml.SafeDumper):
    """Multi-line strings as ``|`` blocks, so the page files read like markdown."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_PageDumper.add_representer(str, _represent_str)


def dump_page(path: Path, header: str, page: dict[str, Any]) -> None:
    text = yaml.dump(page, Dumper=_PageDumper, sort_keys=False, allow_unicode=True, width=1000)
    path.write_text(header + text, encoding="utf-8")


def _names(fields: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in fields) if fields else "none"


def _inputs(op: dict[str, Any]) -> str:
    parts = []
    for item in op["inputs"]:
        if item["name"] == "connection_ref":
            continue
        parts.append(f"`{item['name']}`" + ("" if item.get("required") else " (optional)"))
    return ", ".join(parts) if parts else "none"


def _results(op: dict[str, Any]) -> list[str]:
    return [name.removeprefix("result.") for name in op["result_fields"]]


def _kind(op: dict[str, Any]) -> str:
    return "write" if op["operation_kind"] == "action" else "read"


#: Rivile method → its page in the Rivile guide; filled from the parsed reference.
PAGES: dict[str, str] = {}


def _method_link(method: str) -> str:
    return f"[{method}]({DOCS_BASE}{PAGES.get(method, method.lower())})"


def _remember_pages(rows: list[dict[str, Any]]) -> None:
    PAGES.update({row["method"]: row["page"] for row in rows})


def _plain_title(op: dict[str, Any]) -> str:
    return str(op["display_name"]).removeprefix(ADVANCED_PREFIX)


def _everyday_section(op: dict[str, Any]) -> list[str]:
    methods = FRIENDLY_METHODS[op["operation_id"]]
    return [
        f"### {op['display_name']}",
        "",
        str(op["description"]),
        "",
        f"- Action id: `{op['operation_id']}`",
        f"- Kind: {_kind(op)}",
        f"- Rivilė methods: {', '.join(_method_link(m) for m in methods)}",
        f"- Inputs: {_inputs(op)}",
        f"- Results: {_names(_results(op))}",
        "",
    ]


def operations_page(ops: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    _remember_pages(rows)
    everyday = [op for op in ops if op["operation_id"] in FRIENDLY_METHODS]
    lines = [
        "These steps take plain inputs and make the Rivilė calls for you. Each one names the "
        "Rivilė methods it uses, with a link to the Rivilė guide. Every Rivilė method is also "
        "available on its own, as **Advanced · …** — see **Rivilė API — advanced methods**.",
        "",
    ]
    for op in everyday:
        lines.extend(_everyday_section(op))
    return {
        "page_id": "operations",
        "title": "Rivilė API — operations",
        "description": "The everyday Rivilė steps: what each one does, needs and returns.",
        "components": [
            {
                "component_id": "operations_everyday",
                "type": "markdown",
                "title": "Everyday operations",
                "body": "\n".join(lines).rstrip() + "\n",
            },
            {
                "component_id": "operations_behaviour",
                "type": "markdown",
                "title": "How operations behave",
                "body": BEHAVIOUR,
            },
        ],
    }


def _advanced_section(op: dict[str, Any], row: dict[str, Any] | None) -> list[str]:
    results = _results(op)
    extra = [r for r in results if r not in COMMON_RESULTS + WRITE_RESULTS + LIST_RESULTS]
    if row is None:
        summary = str(op["description"])
        method = "any — you name it"
        results_line = _names(results)
    else:
        summary = f"Rivilė: {row['lt_description']}" if row.get("lt_description") else ""
        method = _method_link(row["method"])
        base = "standard results"
        if any(r in LIST_RESULTS for r in results):
            base += " + list results"
        if any(r in WRITE_RESULTS for r in results):
            base += " + write results"
        results_line = base + (f", {_names(extra)}" if extra else "")
    lines = [f"### {_plain_title(op)}", ""]
    if summary:
        lines += [summary, ""]
    lines += [
        f"- Action id: `{op['operation_id']}` · {_kind(op)} · Rivilė method: {method}",
        f"- Inputs: {_inputs(op)}",
        f"- Results: {results_line}",
        "",
    ]
    return lines


def coverage_body(rows: list[dict[str, Any]]) -> str:
    params = sum(len(row["params"]) for row in rows)
    return (
        "\n".join(
            [
                f"- **Covered:** all {len(rows)} methods documented in the Rivilė GAMA API v2 guide, "
                "each as its own operation.",
                f"- **Params:** every documented param ({params} in all) is its own input, with the "
                "guide's description, allowed values and required flag. The `data` input takes the "
                "guide's field names; extra fields (`PAP_*`, `I49_DIM*`) pass through.",
                "- **Newer than this release:** `extra_params` passes a param the guide added later; "
                "**Call any Rivile method** sends any method (always treated as a write).",
                "- **Not offered: JSON answers.** Answers are requested as XML, Rivilė's default; "
                "values stay text, so codes such as `301` and `301.` stay distinct.",
                "- **Not offered: automatic paging by last key.** Rivilė recommends filtering by the "
                "last key received; set that filter in `fil`. The extension pages by `page_number`.",
                "- **Not offered: rate coordination across steps.** Pacing works within one step; "
                "use a separate API key per integration.",
                "- **Not offered: gateway settings.** Manorivile-Gateway is installed and configured "
                "on your server.",
                "- **Not offered: file content in write answers.** Download a file with **Download "
                "document file**.",
            ]
        )
        + "\n"
    )


def advanced_page(ops: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    _remember_pages(rows)
    by_id = {row["operation_id"]: row for row in rows}
    groups: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        row = by_id.get(op["operation_id"])
        if row is not None:
            groups.setdefault(row["group"], []).append(op)
    tools = [op for op in ops if op["operation_id"] in ("call_method", "export_list")]
    intro = (
        "\n".join(
            [
                "Every Rivilė GAMA API v2 method as its own step, named **Advanced · …** in the step "
                "picker, grouped below by area. Each links to its page in the Rivilė guide.",
                "",
                "Results every method returns:",
                "",
                "- **Standard results:** " + _names(list(COMMON_RESULTS)) + ".",
                "- **List results** (methods that page): " + _names(list(LIST_RESULTS)) + ".",
                "- **Write results** (methods that change data): "
                + _names(list(WRITE_RESULTS))
                + ".",
                "",
                "`records` is the main returned table and `record` its first row — for a write, the "
                "saved record with its new code.",
            ]
        )
        + "\n"
    )
    components: list[dict[str, Any]] = [
        {
            "component_id": "advanced_intro",
            "type": "markdown",
            "title": "Advanced methods",
            "body": intro,
        },
        {
            "component_id": "advanced_coverage",
            "type": "markdown",
            "title": "Coverage",
            "body": coverage_body(rows),
        },
    ]
    for index, group in enumerate(sorted(groups)):
        lines: list[str] = []
        for op in sorted(groups[group], key=lambda o: _plain_title(o)):
            lines.extend(_advanced_section(op, by_id[op["operation_id"]]))
        components.append(
            {
                "component_id": f"advanced_group_{index + 1}",
                "type": "markdown",
                "title": group,
                "body": "\n".join(lines).rstrip() + "\n",
            }
        )
    lines = []
    for op in tools:
        lines.extend(_advanced_section(op, None))
    components.append(
        {
            "component_id": "advanced_any_method",
            "type": "markdown",
            "title": "Any method and exports",
            "body": "\n".join(lines).rstrip() + "\n",
        }
    )
    return {
        "page_id": "advanced-methods",
        "title": "Rivilė API — advanced methods",
        "description": "Every Rivilė GAMA API v2 method, grouped by area, with its inputs.",
        "components": components,
    }


def changelog_page() -> dict[str, Any]:
    """The Changelog page renders CHANGELOG.md, so the two cannot disagree."""
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return {
        "page_id": "changelog",
        "title": "Changelog",
        "description": "What changed in each release of this extension.",
        "components": [
            {
                "component_id": "changelog_entries",
                "type": "markdown",
                "title": "Release history",
                "body": "\n".join(line.rstrip() for line in lines).strip() + "\n",
            }
        ],
    }
