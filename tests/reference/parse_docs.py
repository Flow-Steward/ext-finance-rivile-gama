"""Turn the Rivile GAMA API v2 guide pages (text) into one structured method spec (JSON).

Usage: python3 parse_docs.py <md-dir> <out.json>
"""

import json
import re
import sys
from pathlib import Path


NON_METHOD = {
    "_index",
    "duk",
    "duk__duk_001_blogi_simboliai",
    "duk__duk_010_dovanu_cekiai",
    "geroji_praktika",
    "limitai",
    "manorivile-gateway",
    "naudojimo_pagrindai",
    "parametrai",
}
PARAM_NAME_RE = re.compile(r"^-\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*(?:\(([^)]*)\))?\s*[:\-–]?\s*(.*)$")


def section(text: str, start: str, stops: tuple[str, ...]) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    rest = text[i + len(start) :]
    end = len(rest)
    for stop in stops:
        j = rest.find(stop)
        if 0 <= j < end:
            end = j
    return rest[:end]


def json_examples(text: str) -> list[dict]:
    found = []
    for block in re.findall(r"```\n(.*?)\n```", text, flags=re.S):
        b = block.strip()
        if not b.startswith("{") or '"method"' not in b:
            continue
        cleaned = re.sub(r"//[^\n\"]*$", "", b, flags=re.M)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        cleaned = cleaned.replace("…", "")
        cleaned = re.sub(r"^\s*\.\.\.\s*,?\s*$", "", cleaned, flags=re.M)
        cleaned = re.sub(r'\\(?![\\"/bfnrtu])', r"\\\\", cleaned)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            found.append(json.loads(cleaned))
        except json.JSONDecodeError:
            found.append({"_unparsed": b})
    return found


def xml_records(text: str) -> list[str]:
    roots: list[str] = []
    for block in re.findall(r"```\n(.*?)\n```", text, flags=re.S):
        if "<RET_DOK>" not in block:
            continue
        inner = block.split("<RET_DOK>", 1)[1]
        depth = 0
        for m in re.finditer(r"<(/?)([A-Za-z_][A-Za-z0-9_]*)[^>]*?(/?)>", inner):
            closing, tag, selfclose = m.group(1), m.group(2), m.group(3)
            if tag == "RET_DOK":
                break
            if closing:
                depth -= 1
                continue
            if (
                depth == 0
                and tag not in roots
                and tag
                not in {
                    "id",
                    "durationMs",
                    "errorMessage",
                    "errors",
                    "document",
                    "Row_number",
                }
            ):
                roots.append(tag)
            if not selfclose:
                depth += 1
    return roots


_TAG_RE = re.compile(r"<(/?)([A-Za-z_][A-Za-z0-9_]*)[^>]*?(/?)>")


def _xml_tree(fragment: str) -> list:
    """A tolerant tag tree of one documentation XML fragment: [(tag, children)]."""
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    fragment = re.sub(r"<!\[CDATA\[.*?\]\]>", "", fragment, flags=re.S)
    fragment = re.sub(r"<\?.*?\?>", "", fragment, flags=re.S)
    root: list = []
    stack: list[list] = [root]
    for m in _TAG_RE.finditer(fragment):
        closing, tag, selfclose = m.group(1), m.group(2), m.group(3)
        if closing:
            if len(stack) > 1:
                stack.pop()
            continue
        node: tuple = (tag, [])
        stack[-1].append(node)
        if not selfclose:
            stack.append(node[1])
    return root


def xml_examples(text: str) -> list[dict]:
    """Request examples written as XML: their params names and data tree."""
    found = []
    for block in re.findall(r"```\n(.*?)\n```", text, flags=re.S):
        if "<method>" not in block or "<RET_DOK>" in block:
            continue
        tree = _xml_tree(block)
        body = next((children for tag, children in tree if tag == "body"), tree)
        params = [tag for tag, children in _first(body, "params")]
        data = _first(body, "data")
        found.append({"params": params, "data": data})
    return found


def _first(nodes: list, name: str) -> list:
    for tag, children in nodes:
        if tag == name:
            return children
    return []


def merge_xml_fields(target: dict, nodes: list) -> None:
    for tag, children in nodes:
        if children and any(grand for _t, grand in children):
            child = target.setdefault("children", {}).setdefault(
                tag, {"fields": [], "is_list": True}
            )
            merge_xml_fields(child, children)
        elif children:
            child = target.setdefault("children", {}).setdefault(
                tag, {"fields": [], "is_list": True}
            )
            for leaf, _ in children:
                if leaf not in child["fields"]:
                    child["fields"].append(leaf)
        elif tag not in target["fields"]:
            target["fields"].append(tag)


def field_tables(text: str) -> dict[str, list[dict]]:
    """The field tables under "### Lentelės": field, type, description, EDIT notes."""
    tables: dict[str, list[dict]] = {}
    i = text.find("### Lentelės")
    if i < 0:
        return tables
    for section_match in re.finditer(
        r"\n#### ([A-Za-z0-9_]+)\n(.*?)(?=\n#### |\n### |\Z)", text[i:], flags=re.S
    ):
        tag, body = section_match.group(1).upper(), section_match.group(2)
        rows = []
        for raw_row in body.split("\n|\n")[1:]:
            cells: list[str] = []
            current: list[str] = []
            for line in raw_row.splitlines():
                if line.endswith("|"):
                    current.append(line[:-1].strip())
                    cells.append(" ".join(c for c in current if c))
                    current = []
                elif line.strip():
                    current.append(line.strip())
            if not cells or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", cells[0]):
                continue
            if cells[0] in {"Laukas", "Lauko"}:
                continue
            rows.append(
                {
                    "name": cells[0],
                    "type": cells[1] if len(cells) > 1 else "",
                    "description": cells[2] if len(cells) > 2 else "",
                    "notes": cells[3] if len(cells) > 3 else "",
                }
            )
        if rows:
            tables[tag] = rows
    return tables


def is_required(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"(?<!ne)privalom", lowered)) and "privalomas ar ne" not in lowered


def list_values(params_block: str) -> list[str]:
    i = params_block.find("`list`")
    if i < 0:
        return []
    window = params_block[i : i + 900]
    segment_text = "\n- ".join(window.split("\n- ", 2)[:2])
    values = re.findall(r"\n([A-Z])(?: \(default\))? \|", segment_text)
    values += re.findall(r"`([A-Z])`", segment_text)
    values += re.findall(r"(?:^|[\s:])([A-Z]) [-–]", segment_text)
    ordered = []
    for value in values:
        if value not in ordered:
            ordered.append(value)
    return ordered


def merge_fields(target: dict, value) -> None:
    """Collect field names of a data root from one example value."""
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, sub in item.items():
            if isinstance(sub, (dict, list)) and any(
                isinstance(x, dict) for x in (sub if isinstance(sub, list) else [sub])
            ):
                child = target.setdefault("children", {}).setdefault(
                    key, {"fields": [], "is_list": isinstance(sub, list)}
                )
                merge_fields(child, sub)
            elif key not in target["fields"]:
                target["fields"].append(key)


def parse_bullets(block: str) -> list[dict]:
    """Bullets of a params/data region, with option sub-items folded into their parent."""
    entries: list[dict] = []
    lines = [ln.strip() for ln in block.splitlines()]
    joined: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if joined and joined[-1] == "-":
            joined[-1] = "- " + ln
        else:
            joined.append(ln)
    for ln in joined:
        if not ln.startswith("-"):
            if entries:
                entries[-1]["notes"].append(ln)
            continue
        m = PARAM_NAME_RE.match(ln)
        if not m:
            if entries:
                entries[-1]["notes"].append(ln.lstrip("- "))
            continue
        name, paren, desc = m.group(1), m.group(2), m.group(3).strip(" :-–")
        is_option = (
            bool(re.fullmatch(r"[A-Z0-9*]{1,4}", name))
            and entries
            and not entries[-1].get("is_root")
        )
        if is_option and entries:
            entries[-1]["options"].append({"value": name, "label": desc})
            continue
        entries.append(
            {
                "name": name,
                "description": desc or (paren or ""),
                "options": [],
                "notes": [],
                "table": paren or "",
            }
        )
    return entries


def parse_page(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ").strip()
    if not re.fullmatch(r"[A-Z0-9_]+", title):
        return None
    desc = section(text, "### Aprašymas", ("### ", "##### ")).strip()
    body = section(
        text,
        "##### Body",
        (
            "pvz `application",
            "##### XML",
            "##### JSON",
            "XML Request",
            "JSON Request",
            "##### Response",
        ),
    )
    params_block = section(body, "`params`", ("- `data`", "\n#####", "\n###"))
    data_block = section(body, "- `data`", ("\n#",))
    params = list(parse_bullets(params_block))
    data_entries = parse_bullets(data_block.split("\n", 1)[1] if "\n" in data_block else "")
    examples = json_examples(text)
    xml_requests = xml_examples(text)
    tables = field_tables(text)
    example_params: list[str] = []
    data_roots: dict[str, dict] = {}
    for ex in examples:
        for k in (ex.get("params") or {}) if isinstance(ex.get("params"), dict) else {}:
            if k not in example_params:
                example_params.append(k)
        data = ex.get("data")
        if isinstance(data, dict):
            for root, value in data.items():
                entry = data_roots.setdefault(root, {"fields": [], "is_list": False})
                if isinstance(value, list):
                    entry["is_list"] = True
                if isinstance(value, (dict, list)):
                    merge_fields(entry, value)
    for ex in xml_requests:
        for k in ex["params"]:
            if k not in example_params:
                example_params.append(k)
        for root, children in ex["data"]:
            entry = data_roots.setdefault(root, {"fields": [], "is_list": False})
            merge_xml_fields(entry, children)
    # Field tables complete what the examples show (keys for update/delete…).
    for root, entry in data_roots.items():
        for row in tables.get(root.upper(), []):
            if row["name"] not in entry["fields"] and row["name"] not in entry.get("children", {}):
                entry["fields"].append(row["name"])
        for child, child_entry in entry.get("children", {}).items():
            for row in tables.get(child.upper(), []):
                if row["name"] not in child_entry["fields"]:
                    child_entry["fields"].append(row["name"])

    def root_fields(root: dict) -> set[str]:
        names = set(root["fields"])
        for child in root.get("children", {}).values():
            names |= root_fields(child)
        return names

    example_fields = (
        set().union(*(root_fields(r) for r in data_roots.values())) if data_roots else set()
    )
    # Data-region bullets that are really params (EDIT_I06_DOC style) or root tables.
    roots_doc: dict[str, str] = {}
    example_roots = set(data_roots)
    json_params = {k.lower() for k in example_params}
    current_root = ""
    for d in data_entries:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", d["name"]):
            if example_roots and d["name"] not in example_roots:
                continue
            roots_doc[d["name"]] = d["description"]
            data_roots.setdefault(d["name"], {"fields": [], "is_list": False})
            current_root = d["name"]
        elif d["name"] in example_fields:
            continue
        elif current_root and d["name"].lower() not in json_params:
            # A field listed under a data root (EDIT_PAP: PAP.forma, PAP.kodas1…).
            if d["name"] not in data_roots[current_root]["fields"]:
                data_roots[current_root]["fields"].append(d["name"])
        elif not any(p["name"] == d["name"] for p in params):
            params.append(d)
    for p in params:
        p["required"] = is_required(" ".join([p.get("description", ""), *p.get("notes", [])]))
    lvalues = list_values(params_block)
    params = [p for p in params if p["name"] not in {"data", "method", "params"}]
    names = {p["name"].lower() for p in params}
    for k in example_params:
        if k.lower() not in names:
            names.add(k.lower())
            params.append(
                {
                    "name": k,
                    "description": "",
                    "options": [],
                    "notes": [],
                    "table": "",
                    "required": False,
                }
            )
    method = title
    kind = (
        "read"
        if re.match(r"^(MGAMA_)?GET_", method)
        else ("write" if re.match(r"^(MGAMA_)?EDIT_", method) else "setup")
    )
    return {
        "method": method,
        "page": path.stem,
        "kind": kind,
        "description": desc,
        "params": [{k: v for k, v in p.items() if k != "is_root"} for p in params],
        "data_roots": {
            k: {**v, "description": roots_doc.get(k, "")} for k, v in data_roots.items()
        },
        "list_values": lvalues,
        "field_tables": tables,
        "response_roots": xml_records(text),
        "examples": examples,
        "body_text": body.strip(),
    }


def main() -> None:
    md_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    methods = []
    for path in sorted(md_dir.glob("*.md")):
        if path.stem in NON_METHOD:
            continue
        spec = parse_page(path)
        if spec:
            methods.append(spec)
    methods.sort(key=lambda m: m["method"])
    out.write_text(json.dumps(methods, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(len(methods), "methods")


if __name__ == "__main__":
    main()
