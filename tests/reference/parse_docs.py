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
    # Data-region bullets that are really params (EDIT_I06_DOC style) or root tables.
    roots_doc: dict[str, str] = {}
    example_roots = set(data_roots)
    for d in data_entries:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", d["name"]):
            if example_roots and d["name"] not in example_roots:
                continue
            roots_doc[d["name"]] = d["description"]
            data_roots.setdefault(d["name"], {"fields": [], "is_list": False})
        elif not any(p["name"] == d["name"] for p in params):
            params.append(d)
    params = [p for p in params if p["name"] not in {"data", "method", "params"}]
    names = {p["name"].lower() for p in params}
    for k in example_params:
        if k.lower() not in names:
            names.add(k.lower())
            params.append({"name": k, "description": "", "options": [], "notes": [], "table": ""})
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
