"""Pure OBDA prefix and target extraction shared by build and query validation.

These helpers preserve the existing supported syntax; they do not certify a
complete mapping. Callers still enforce coverage, types and release integrity.
"""

from __future__ import annotations

import re


def parse_obda_prefixes(mapping_obda: str) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    prefix_section = mapping_obda.split("[MappingDeclaration]", 1)[0]
    for match in re.finditer(
        r"(?m)^\s*(?P<prefix>[A-Za-z_][\w-]*|):\s*(?P<iri>\S+)\s*$",
        prefix_section,
    ):
        prefixes[match.group("prefix")] = match.group("iri").strip("<>")
    return prefixes


def parse_obda_targets(mapping_obda: str) -> list[str]:
    return [
        match.group("target").strip()
        for match in re.finditer(
            r"(?ms)^\s*target\s+(?P<target>.*?)^\s*source\s+",
            str(mapping_obda or ""),
        )
    ]


_MAPPING_ID_LINE = re.compile(r"^\s*mappingId\s+(?P<id>\S+)\s*$")
_TARGET_LINE = re.compile(r"^\s*target\s+")
_SOURCE_LINE = re.compile(r"^\s*source\s+")


def normalize_obda_block_separation(mapping_obda: str) -> tuple[str, list[str]]:
    """Insert the blank line OBDA requires before every mappingId line.

    Ontop reads a source until the next blank line, so a mappingId written
    directly after a source line is silently merged into the previous SQL.
    Adding the separator is purely syntactic and never changes mapping
    semantics. Returns the normalized text and the separated mapping ids.
    """
    text = str(mapping_obda or "")
    if "[MappingDeclaration]" not in text:
        return text, []
    head, body = text.split("[MappingDeclaration]", 1)
    out: list[str] = []
    fixed: list[str] = []
    for line in body.split("\n"):
        match = _MAPPING_ID_LINE.match(line)
        if match and out and out[-1].strip() and not out[-1].rstrip().endswith("[["):
            out.append("")
            fixed.append(match.group("id"))
        out.append(line)
    return head + "[MappingDeclaration]" + "\n".join(out), fixed


def parse_obda_blocks(mapping_obda: str) -> list[dict]:
    """Split the mapping collection into blocks the way Ontop reads them.

    Blocks are separated by blank lines. Each block reports its mapping ids,
    target lines and the full (possibly multi-line) SQL source.
    """
    text = str(mapping_obda or "")
    if "[MappingDeclaration]" not in text:
        return []
    body = text.split("[MappingDeclaration]", 1)[1]
    start, end = body.find("[["), body.rfind("]]")
    if start < 0 or end < 0:
        return []
    blocks: list[dict] = []
    for raw in re.split(r"\n\s*\n", body[start + 2:end]):
        lines = [line for line in raw.split("\n") if line.strip()]
        if not lines:
            continue
        ids = [m.group("id") for m in (_MAPPING_ID_LINE.match(line) for line in lines) if m]
        source_index = next((i for i, line in enumerate(lines) if _SOURCE_LINE.match(line)), None)
        source = None
        if source_index is not None:
            source = " ".join(
                [re.sub(r"^\s*source\s+", "", lines[source_index]).strip()]
                + [line.strip() for line in lines[source_index + 1:]]
            ).strip()
        blocks.append({
            "mapping_id": ids[0] if ids else None,
            "mapping_ids": ids,
            "targets": [line for line in lines if _TARGET_LINE.match(line)],
            "source": source,
            "source_line_count": sum(1 for line in lines if _SOURCE_LINE.match(line)),
        })
    return blocks


def obda_structure_issues(mapping_obda: str) -> list[dict]:
    """Return structural defects that make Ontop merge or reject blocks."""
    issues: list[dict] = []
    seen: set[str] = set()
    for block in parse_obda_blocks(mapping_obda):
        mapping_id = block["mapping_id"] or "(missing mappingId)"
        if len(block["mapping_ids"]) > 1:
            issues.append({
                "mapping_id": mapping_id, "code": "OBDA_BLOCKS_MERGED",
                "message": "映射块之间缺少空行，Ontop 会把后续块并入前一个 SQL："
                + ", ".join(block["mapping_ids"]),
            })
            continue
        if not block["mapping_ids"]:
            issues.append({"mapping_id": mapping_id, "code": "OBDA_MAPPING_ID_MISSING",
                           "message": "映射块缺少 mappingId。"})
        elif mapping_id in seen:
            issues.append({"mapping_id": mapping_id, "code": "OBDA_MAPPING_ID_DUPLICATE",
                           "message": "mappingId 重复。"})
        seen.add(mapping_id)
        if not block["targets"]:
            issues.append({"mapping_id": mapping_id, "code": "OBDA_TARGET_MISSING",
                           "message": "映射块缺少 target。"})
        if block["source_line_count"] != 1:
            issues.append({"mapping_id": mapping_id, "code": "OBDA_SOURCE_COUNT",
                           "message": f"映射块必须恰有一个 source，实际 {block['source_line_count']} 个。"})
    return issues
