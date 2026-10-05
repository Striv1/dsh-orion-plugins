from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Components describe what changed. Stages describe what must be revalidated.
# Keeping this mapping declarative makes rollback impact explainable and lets the
# workflow preserve unrelated, fingerprinted outputs.
COMPONENT_STAGE_IMPACT: dict[str, tuple[str, ...]] = {
    "SOURCE_EVIDENCE": ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"),
    "DATA_PROFILE": ("S1", "S2", "S3", "S4", "S5", "S6", "S7"),
    "SEMANTIC_MODEL": ("S2", "S3", "S4", "S5", "S6", "S7"),
    "MAPPING": ("S3", "S4", "S5", "S6", "S7"),
    "RUNTIME_MAPPING": ("S3", "S6", "S7"),
    "RUNTIME_RULES": ("S3", "S4", "S6", "S7"),
    "COMPETENCY_QUESTIONS": ("S4", "S6", "S7"),
    "ONTOLOGY_SCHEMA": ("S4", "S5", "S6", "S7"),
    "ONTOLOGY_BINARY": ("S5", "S6", "S7"),
    "VALIDATION_EVIDENCE": ("S6", "S7"),
    "DEPLOYMENT_CONFIG": ("S7",),
}


def normalize_changed_components(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized = tuple(
        dict.fromkeys(
            str(value or "").strip().upper() for value in values if str(value or "").strip()
        )
    )
    unknown = sorted(set(normalized) - set(COMPONENT_STAGE_IMPACT))
    if unknown:
        raise ValueError("不支持的变更组件：" + ", ".join(unknown))
    return normalized


def component_revalidation_plan(
    changed_components: Iterable[str],
    *,
    target_stage: str | None = None,
) -> dict[str, Any]:
    components = normalize_changed_components(changed_components)
    impacted = {stage for component in components for stage in COMPONENT_STAGE_IMPACT[component]}
    if target_stage:
        impacted.add(target_stage)
    ordered = [f"S{index}" for index in range(8) if f"S{index}" in impacted]
    return {
        "policy_version": "component-dependency-v1",
        "changed_components": list(components),
        "required_revalidation_stages": ordered,
        "reusable_stages": [f"S{index}" for index in range(8) if f"S{index}" not in impacted],
        "component_stage_impact": {
            component: list(COMPONENT_STAGE_IMPACT[component]) for component in components
        },
    }
