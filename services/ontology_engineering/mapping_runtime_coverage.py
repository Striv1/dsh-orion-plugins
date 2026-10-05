"""Shared static Mapping/OBDA coverage gate; never claims data execution."""
from __future__ import annotations

import re
from typing import Any

from services.ontology_contracts.obda import parse_obda_targets

from .stage_submission_validation import (
    DATABASE_CLASS_MAPPING_TYPES,
    DOCUMENT_MAPPING_TYPES,
    RULE_CLASS_MAPPING_TYPES,
)


def runtime_covers_target(mapping_obda: str, target: str) -> bool:
    escaped = re.escape(target)
    return bool(
        re.search(
            rf"(?<![\w-])(?:[A-Za-z_][\w-]*:|:){escaped}(?![\w-])",
            mapping_obda,
        )
        or re.search(rf"[\/#]{escaped}>", mapping_obda)
    )


def runtime_covers_mapping(mapping_obda: str, mapping: dict[str, Any]) -> bool:
    """Require a mapped class to occur as rdf:type, or a property as predicate.

    A term mentioned in an unrelated target position is not an executable
    mapping for that term. This is a static coverage check, not a data readback.
    """
    target = str(mapping.get("target") or "").split(" (")[0].strip()
    if not target:
        return False
    term = rf"(?:(?:[A-Za-z_][\w-]*:|:){re.escape(target)}(?![\w-])|<[^>\s]*[/#:]{re.escape(target)}>)"
    if str(mapping.get("mapping_type") or "").upper() in DATABASE_CLASS_MAPPING_TYPES:
        pattern = re.compile(rf"(?:\ba|\brdf:type|<http://www\.w3\.org/1999/02/22-rdf-syntax-ns#type>)\s+{term}(?=\s|[;,.]|$)")
    else:
        pattern = re.compile(rf"(?<![\w-]){term}\s+(?:\{{|<|[A-Za-z_][\w-]*:|:)")
    return any(pattern.search(block) for block in parse_obda_targets(mapping_obda))


def runtime_uncovered_targets(
    mappings: list[dict[str, Any]],
    mapping_obda: str,
) -> list[str]:
    return sorted(
        {
            str(item.get("target") or "").split(" (")[0].strip()
            for item in mappings
            if str(item.get("mapping_type") or "").upper() not in DOCUMENT_MAPPING_TYPES | RULE_CLASS_MAPPING_TYPES
            and not runtime_covers_mapping(mapping_obda, item)
        }
    )
