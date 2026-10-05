"""The formal S2 candidate artifact shared by downstream engineering readers."""
from __future__ import annotations

import yaml

from services.ontology_contracts.errors import WorkflowError

ONTOLOGY_CANDIDATES_PATH = "02-semantic-recognition/ontology-candidates.yaml"


def parse_ontology_candidates(raw: bytes | str) -> list[dict]:
    """Decode record_semantic_candidates' envelope, never a parallel JSON copy."""
    try:
        value = yaml.safe_load(raw)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise WorkflowError(f"S2正式候选产物不是有效 YAML：{ONTOLOGY_CANDIDATES_PATH}") from exc
    if (not isinstance(value, dict) or not isinstance(value.get("candidates"), list)
            or any(not isinstance(candidate, dict) for candidate in value["candidates"])):
        raise WorkflowError(f"S2正式候选产物须包含 candidates 对象列表：{ONTOLOGY_CANDIDATES_PATH}")
    return value["candidates"]
