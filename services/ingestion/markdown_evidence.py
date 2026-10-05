"""Deterministic, source-only Markdown evidence spans (no semantic inference)."""
from __future__ import annotations

import re
from typing import Any

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")


def _cells(line: str) -> list[str]:
    return re.split(r"(?<!\\)\|", line.strip().strip("|"))


def _separator(line: str) -> bool:
    cells = _cells(line)
    return len(cells) > 1 and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def markdown_evidence(markdown: str) -> list[dict[str, Any]]:
    """Keep exact line positions; each table row includes its actual header context."""
    lines = markdown.splitlines()
    result: list[dict[str, Any]] = []
    headings: list[tuple[int, str]] = []

    def emit(start: int, end: int, header: int | None = None) -> None:
        locator = f"文本第 {start + 1}-{end} 行"
        content = "\n".join(lines[start:end])
        if header is not None:
            locator += f"；表头第 {header + 1}-{header + 2} 行"
            content = "\n".join(lines[header:header + 2]) + "\n" + content
        result.append({"locator": locator, "section": " / ".join(title for _, title in headings) or "正文", "content": content})

    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        heading = _HEADING.match(lines[i])
        if heading:
            level = len(heading[1])
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, heading[2]))
            emit(i, i + 1)
            i += 1
            continue
        fence = _FENCE.match(lines[i])
        if fence:
            start, marker = i, fence[1]
            i += 1
            closing = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
            while i < len(lines):
                i += 1
                if closing.match(lines[i - 1]):
                    break
            emit(start, i)
            continue
        if i + 1 < len(lines) and "|" in lines[i] and _separator(lines[i + 1]):
            header = i
            i += 2
            emit(header, i)
            while i < len(lines) and lines[i].strip() and "|" in lines[i] and not _HEADING.match(lines[i]):
                emit(i, i + 1, header)
                i += 1
            continue
        start = i
        i += 1
        while i < len(lines) and lines[i].strip():
            if _HEADING.match(lines[i]) or _FENCE.match(lines[i]) or _LIST.match(lines[i]):
                break
            if i + 1 < len(lines) and "|" in lines[i] and _separator(lines[i + 1]):
                break
            i += 1
        emit(start, i)
    return result
