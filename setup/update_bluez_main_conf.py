#!/usr/bin/env python3
"""Update BlueZ main.conf for hfp-mcp without duplicating INI sections."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


_SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*$")


def _is_comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _is_experimental_assignment(line: str) -> bool:
    return bool(re.match(r"^\s*#?\s*Experimental\s*=.*$", line))


def remove_legacy_experimental_sections(lines: list[str]) -> list[str]:
    """
    Remove duplicate [General] sections that old installers appended solely to
    set Experimental=true. Preserve the first [General] section and any section
    that contains other meaningful settings.
    """
    sections: list[tuple[int, int, str | None]] = []
    current_start = 0
    current_name: str | None = None

    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        sections.append((current_start, index, current_name))
        current_start = index
        current_name = match.group(1)
    sections.append((current_start, len(lines), current_name))

    seen_general = False
    keep = [True] * len(lines)
    for start, end, name in sections:
        if name != "General":
            continue
        if not seen_general:
            seen_general = True
            continue
        body = lines[start + 1 : end]
        meaningful = [line for line in body if not _is_comment_or_blank(line)]
        if meaningful and all(_is_experimental_assignment(line) for line in meaningful):
            for index in range(start, end):
                keep[index] = False

    cleaned = [line for index, line in enumerate(lines) if keep[index]]
    while len(cleaned) >= 2 and not cleaned[-1].strip() and not cleaned[-2].strip():
        cleaned.pop()
    return cleaned


def set_key(lines: list[str], section: str, key: str, value: str) -> list[str]:
    key_re = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=.*$")
    header_index = None
    insert_index = len(lines)
    in_section = False

    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            if in_section:
                insert_index = index
                break
            in_section = match.group(1) == section
            if in_section:
                header_index = index
                insert_index = index + 1
            continue
        if in_section and key_re.match(line):
            lines[index] = f"{key}={value}"
            return lines

    if header_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[{section}]", f"{key}={value}"])
    else:
        lines.insert(insert_index, f"{key}={value}")
    return lines


def update_content(content: str) -> str:
    lines = content.splitlines()
    lines = remove_legacy_experimental_sections(lines)
    lines = set_key(lines, "Policy", "AutoEnable", "true")
    return "\n".join(lines).rstrip() + "\n"


def update_file(path: Path) -> None:
    content = path.read_text() if path.exists() else ""
    path.write_text(update_content(content))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    update_file(args.path)


if __name__ == "__main__":
    main()
