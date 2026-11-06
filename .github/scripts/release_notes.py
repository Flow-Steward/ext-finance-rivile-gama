#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version, as release notes.

    python .github/scripts/release_notes.py 0.2.0 > notes.md

The section starts at a heading for that version, in any of these forms:
``## 0.2.0``, ``## 0.2.0 — 2026-11-06``, ``## [0.2.0]``, ``## v0.2.0``,
and ends at the next ``## `` heading. Wrapped lines are joined, because GitHub
shows every line break in a release text. Exits 1 when the section is missing
or has no text, so a tag without notes is refused before anything is built.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+\[?v?(?P<version>[0-9][^\]\s]*)\]?(\s|$)")


def section(text: str, version: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            match = HEADING.match(line)
            inside = bool(match and match.group("version") == version)
            continue
        if inside:
            lines.append(line.rstrip())
    return lines


def unwrap(lines: list[str]) -> str:
    """Join continuation lines to their paragraph or list item."""
    out: list[str] = []
    for line in lines:
        continues = (
            out
            and out[-1].strip()
            and line.strip()
            and not re.match(r"^\s*([-*+]|\d+\.)\s", line)
            and not line.lstrip().startswith(("#", ">", "|", "```"))
        )
        if continues:
            out[-1] = f"{out[-1]} {line.strip()}"
        else:
            out.append(line)
    return "\n".join(out).strip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_notes.py <version>", file=sys.stderr)
        return 2
    version = sys.argv[1].removeprefix("v")
    changelog = Path("CHANGELOG.md")
    if not changelog.is_file():
        print("::error::CHANGELOG.md is missing", file=sys.stderr)
        return 1
    lines = section(changelog.read_text(encoding="utf-8"), version)
    if not any(line.strip() for line in lines):
        print(f"::error::CHANGELOG.md has no text for version {version}", file=sys.stderr)
        return 1
    sys.stdout.write(unwrap(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
