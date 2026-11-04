#!/usr/bin/env python3
"""Download the Rivile GAMA API v2 guide and reduce every page to plain text.

Offline reference tooling, not part of the runtime:

    python3 tests/reference/html_to_text.py <work-dir>          # download + convert
    python3 tests/reference/parse_docs.py <work-dir>/md tests/reference/rivile_api_v2_spec.json
    python3 tests/reference/build_contracts.py
"""

from __future__ import annotations

import html
import re
import sys
import urllib.request
from pathlib import Path

GUIDE = "https://gidas.rivile.lt"
INDEX = "/internetiniai_moduliai/api_rest"


def download(work: Path) -> Path:
    pages = work / "html"
    pages.mkdir(parents=True, exist_ok=True)
    index = _get(GUIDE + INDEX)
    (pages / "_index.html").write_text(index, encoding="utf-8")
    links = sorted(set(re.findall(r'href="(' + INDEX + r'/[^"#]+)"', index)))
    for link in links:
        name = link.split("/api_rest/", 1)[1].replace("/", "__")
        (pages / f"{name}.html").write_text(_get(GUIDE + link), encoding="utf-8")
    return pages


def _get(url: str) -> str:
    if not url.startswith(GUIDE + "/"):
        raise ValueError(url)
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310  # nosec B310
        return response.read().decode("utf-8", "replace")


def page_text(source: str) -> str | None:
    start = source.find('<div id="body-inner"')
    stop = source.find('<div id="navigation"')
    if start < 0:
        return None
    text = source[start : stop if stop > start else None]
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S)
    text = re.sub(
        r"<pre[^>]*>(.*?)</pre>",
        lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n",
        text,
        flags=re.S,
    )
    text = re.sub(r"<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text)
    text = re.sub(r"</h[1-6]>", "\n", text)
    text = re.sub(r"<tr[^>]*>", "\n|", text)
    text = re.sub(r"</t[dh]>", " |", text)
    text = re.sub(r"<li[^>]*>", "\n- ", text)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def convert(pages: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(pages.glob("*.html")):
        text = page_text(path.read_text(encoding="utf-8", errors="replace"))
        if text is None:
            continue
        (out / f"{path.stem}.md").write_text(text, encoding="utf-8")
        count += 1
    return count


def main() -> None:
    work = Path(sys.argv[1] if len(sys.argv) > 1 else "rivile-guide")
    pages = work / "html" if (work / "html").is_dir() else download(work)
    print(convert(pages, work / "md"), "pages")


if __name__ == "__main__":
    main()
