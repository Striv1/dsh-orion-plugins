from __future__ import annotations

import re
from typing import Any

from services.ontology_contracts.obda import (
    parse_obda_prefixes as _prefixes,
)
from services.ontology_contracts.obda import (
    parse_obda_targets as _target_blocks,
)

DATA_MAPPING_TYPES = {
    "COLUMN_TO_DATA_PROPERTY",
    "SQL_TO_DATA_PROPERTY",
}


def _local_name(value: str) -> str:
    raw = str(value or "").split(" (")[0].strip().strip("<>")
    return raw.rsplit("#", 1)[-1].rsplit("/", 1)[-1]




def _resolve_datatype(token: str, prefixes: dict[str, str]) -> str:
    value = str(token or "").strip().strip("<>")
    if ":" not in value or value.startswith(("http://", "https://", "urn:")):
        return value
    prefix, local = value.split(":", 1)
    return prefixes.get(prefix, prefix + ":") + local




def normalize_known_literal_datatypes(mapping_obda: str) -> tuple[str, list[dict[str, str]]]:
    """Compile datatypes that are fixed by standard RDF vocabulary semantics."""

    source = str(mapping_obda or "")
    prefixes = _prefixes(source)
    if prefixes.get("rdfs") != "http://www.w3.org/2000/01/rdf-schema#":
        return source, []
    datatype = (
        "xsd:string" if prefixes.get("xsd", "http://www.w3.org/2001/XMLSchema#")
        == "http://www.w3.org/2001/XMLSchema#"
        else "<http://www.w3.org/2001/XMLSchema#string>"
    )
    count = 0

    def normalize_target(match: re.Match) -> str:
        nonlocal count
        normalized_target, changed = re.subn(
            r"(?<![\w-])rdfs:label(?![\w-])(?P<space>\s+)"
            r"(?P<object>\{[^}]+\})(?!\s*(?:\^\^|@))",
            r"rdfs:label\g<space>\g<object>^^" + datatype,
            match.group(0),
        )
        count += changed
        return normalized_target

    normalized = re.sub(
        r"(?ms)^\s*target\s+.*?(?=^\s*source\s+)", normalize_target, source
    )
    if count and "xsd" not in _prefixes(normalized):
        normalized = normalized.replace(
            "[PrefixDeclaration]",
            "[PrefixDeclaration]\nxsd: http://www.w3.org/2001/XMLSchema#",
            1,
        )
    decisions = []
    if count:
        decisions.append(
            {
                "compiler": "STANDARD_RDF_LITERAL_DATATYPE",
                "predicate": "rdfs:label",
                "datatype": "http://www.w3.org/2001/XMLSchema#string",
                "occurrence_count": str(count),
            }
        )
    return normalized, decisions


def collect_known_literal_runtime_issues(mapping_obda: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for index, block in enumerate(_target_blocks(mapping_obda)):
        match = re.search(
            r"(?<![\w-])rdfs:label(?![\w-])\s+"
            r"(?P<object>\{[^}]+\})(?!\s*(?:\^\^|@))",
            block,
        )
        if match:
            issues.append(
                {
                    "gate": "G-S3-RUNTIME-DATATYPE",
                    "path": "realtime_runtime.mapping_obda",
                    "mapping_id": f"OBDA-TARGET-{index + 1}",
                    "message": (
                        f"rdfs:label 的变量 {match.group('object')} 缺少显式 datatype；"
                        "平台编译结果必须使用 xsd:string 或显式语言标签。"
                    ),
                }
            )
    return issues


def collect_mapping_runtime_issues(
    mappings: list[dict[str, Any]],
    mapping_obda: str,
) -> list[dict[str, str]]:
    """Collect deterministic Mapping/OBDA datatype mismatches before S3 commit.

    Ontop production disables unsafe default datatype inference.  A variable
    used as a data-property object must therefore carry the exact datatype
    declared by the reviewed Mapping.  Returning all issues prevents the
    one-error-per-S6-loop behavior.
    """

    issues: list[dict[str, str]] = []
    prefixes = _prefixes(mapping_obda)
    blocks = _target_blocks(mapping_obda)
    for index, item in enumerate(mappings):
        mapping_type = str(item.get("mapping_type") or "").strip().upper()
        if mapping_type not in DATA_MAPPING_TYPES:
            continue
        mapping_id = str(item.get("id") or f"mapping-{index + 1}").strip()
        target = _local_name(str(item.get("target") or ""))
        datatype = str(item.get("datatype") or "").strip().strip("<>")
        path = f"mapping_draft.mappings[{index}].datatype"
        if not datatype:
            # Historical mappings intentionally use the S4 xsd:string default.
            # Keep them readable; new generators should emit datatype explicitly.
            continue
        if not target:
            continue
        predicate = re.compile(
            rf"(?<![\w-])(?:[A-Za-z_][\w-]*:|:){re.escape(target)}(?![\w-])"
            rf"\s+(?P<object>\{{[^}}]+\}})(?:\^\^(?P<datatype><[^>]+>|[^\s;,.]+))?"
        )
        occurrences = [match for block in blocks for match in predicate.finditer(block)]
        if not occurrences:
            # Coverage is reported by the existing G-S3-RUNTIME-COVERAGE gate.
            continue
        expected = _resolve_datatype(datatype, prefixes)
        for match in occurrences:
            actual_token = str(match.group("datatype") or "").strip()
            variable = str(match.group("object") or "").strip()
            if not actual_token:
                issues.append(
                    {
                        "gate": "G-S3-RUNTIME-DATATYPE",
                        "path": path,
                        "mapping_id": mapping_id,
                        "message": (
                            f"Ontop 目标 {target} 的变量 {variable} 缺少显式 datatype；"
                            f"应声明为 <{expected}>。"
                        ),
                    }
                )
                continue
            actual = _resolve_datatype(actual_token, prefixes)
            if actual != expected:
                issues.append(
                    {
                        "gate": "G-S3-RUNTIME-DATATYPE",
                        "path": path,
                        "mapping_id": mapping_id,
                        "message": (
                            f"Ontop 目标 {target} 的 datatype 为 <{actual}>，"
                            f"与 Mapping 声明 <{expected}> 不一致。"
                        ),
                    }
                )
    issues.extend(collect_known_literal_runtime_issues(mapping_obda))
    return issues
