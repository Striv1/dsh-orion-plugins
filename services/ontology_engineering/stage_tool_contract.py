"""Single source of truth for which workflow write tools each stage exposes.

The workflow state machine (get_next_action) and the 3081 frontend stage
whitelist must agree; tests/unit/test_stage_tool_contract.py fails when the
frontend table in harness/plugins/branded-web-runtime/index.js drifts.
"""

from __future__ import annotations

STAGE_WRITE_TOOLS: dict[str, tuple[str, ...]] = {
    "S0": (
        "start_document_ingestion_job",
        "commit_document_ingestion_job",
        "record_document_evidence",
        "record_s0_scope_decision",
    ),
    "S1": (
        "record_data_understanding_from_datasets",
        "record_data_understanding",
    ),
    "S2": ("record_semantic_candidates", "fork_s2_draft_from_history"),
    "S3": ("prepare_mapping_review", "generate_mapping_skeleton", "compile_mapping_runtime",
           "fork_s3_draft_from_history", "start_business_preview"),
    "S4": ("generate_ontology_design", "prepare_ontology_design_review"),
    "S5": ("start_managed_stage_execution", "record_ontology_build"),
    "S6": ("start_managed_stage_execution", "record_quality_validation"),
    "S7": (
        "publish_ontology_package",
        "defer_ontology_publication",
        "resume_ontology_publication",
    ),
}

# Alternate directive used for DOCUMENT_ONLY joint-design S1.
S1_DOCUMENT_ONLY_WRITE_TOOLS: tuple[str, ...] = ("record_document_understanding",)

BLOCKED_HUMAN_WRITE_TOOLS: dict[str, tuple[str, ...]] = {
    "S3": ("resolve_mapping_option", "resolve_mapping_confirmation"),
    "S4": ("resolve_competency_question_review",),
}

COMMON_WRITE_TOOLS: tuple[str, ...] = (
    "archive_ontology_project",
    "reopen_stage_for_correction",
)

DRAFT_WRITE_TOOLS: tuple[str, ...] = ("save_stage_submission", "patch_stage_submission")
DRAFT_STAGES: frozenset[str] = frozenset({"S2", "S3", "S4"})


def stage_declared_write_tools(stage: str) -> set[str]:
    """Every write tool the state machine may hand out while *stage* is open."""
    stage = stage.upper()
    tools = set(STAGE_WRITE_TOOLS.get(stage, ()))
    tools |= set(BLOCKED_HUMAN_WRITE_TOOLS.get(stage, ()))
    tools |= set(COMMON_WRITE_TOOLS)
    tools |= {"retry_failed_stage", "commit_preflight_stage_submission"}
    if stage == "S1":
        tools |= set(S1_DOCUMENT_ONLY_WRITE_TOOLS)
    if stage in DRAFT_STAGES:
        tools |= set(DRAFT_WRITE_TOOLS)
    return tools
