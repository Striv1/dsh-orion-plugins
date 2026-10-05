from __future__ import annotations

from typing import Any

CLOSED_WORLD_SET_DIFFERENCE_V1 = "CLOSED_WORLD_SET_DIFFERENCE_V1"
SEMANTICA_FORWARD_V1 = "SEMANTICA_FORWARD_V1"
OWL_RL_CLOSURE_V1 = "OWL_RL_CLOSURE_V1"
HERMIT_OWL_DL_VALIDATION_V1 = "HERMIT_OWL_DL_VALIDATION_V1"
SHACL_VALIDATION_V1 = "SHACL_VALIDATION_V1"
MULTI_SOURCE_SNAPSHOT_V1 = "MULTI_SOURCE_SNAPSHOT_V1"
REALTIME_SOURCE_QUERY_V1 = "REALTIME_SOURCE_QUERY_V1"
CROSS_SOURCE_IDENTITY_V1 = "CROSS_SOURCE_IDENTITY_V1"


def reasoning_execution_capabilities() -> dict[str, dict[str, Any]]:
    """Return the locally implemented, testable reasoning execution contract."""

    return {
        SEMANTICA_FORWARD_V1: {
            "version": 1,
            "executor": "SEMANTICA_FORWARD",
            "operators": ["POSITIVE_HORN_RULE"],
            "requires": [],
        },
        OWL_RL_CLOSURE_V1: {
            "version": 1,
            "executor": "ORION_OWL_RL",
            "operators": [
                "CLASS_SUBSUMPTION",
                "DOMAIN_RANGE_ENTAILMENT",
                "EQUIVALENT_CLASS",
                "TRANSITIVE_PROPERTY",
            ],
            "requires": ["VERSIONED_ONTOLOGY_GRAPH", "READ_ONLY_WORKING_GRAPH"],
            "execution_scope": "RUNTIME_READ_ONLY",
        },
        HERMIT_OWL_DL_VALIDATION_V1: {
            "version": 1,
            "executor": "PROTEGE_HERMIT",
            "operators": ["OWL_DL_CONSISTENCY", "UNSATISFIABLE_CLASS_CHECK"],
            "requires": ["VERSIONED_ONTOLOGY_ARTIFACT", "ISOLATED_PROTEGE_WORKSPACE"],
            "execution_scope": "BUILD_AND_RELEASE_VALIDATION",
        },
        SHACL_VALIDATION_V1: {
            "version": 1,
            "executor": "ORION_PYSHACL",
            "operators": ["SHAPE_CONSTRAINT_VALIDATION"],
            "requires": ["VERSIONED_SHAPES_GRAPH", "SOURCE_BOUND_DATA_GRAPH"],
            "execution_scope": "QUALITY_VALIDATION",
        },
        CLOSED_WORLD_SET_DIFFERENCE_V1: {
            "version": 1,
            "executor": "ORION_SAFE_ANTI_JOIN_THEN_SEMANTICA_FORWARD",
            "operators": ["SET_DIFFERENCE", "ANTI_JOIN", "NOT_EXISTS"],
            "requires": [
                "EXPLICIT_CLOSED_WORLD_PREDICATE",
                "POSITIVE_VARIABLE_BINDING",
                "COMPLETE_CASE_SNAPSHOT",
                "VERSIONED_SOURCE_IDENTITY",
                "PII_MINIMIZATION_SCOPE",
            ],
        },
    }


def supports_reasoning_capability(name: str) -> bool:
    return name in reasoning_execution_capabilities()


def data_execution_capabilities() -> dict[str, dict[str, Any]]:
    """Describe implemented multi-source data-plane capabilities.

    Real-time access is deliberately a constrained query-gateway contract.  It
    is not a claim that arbitrary cross-database SQL federation is available.
    """

    return {
        MULTI_SOURCE_SNAPSHOT_V1: {
            "version": 1,
            "executor": "ORION_SNAPSHOT_HUB",
            "engines": ["POSTGRESQL", "MYSQL"],
            "atomic_promotion": True,
            "requires": [
                "PROJECT_SCOPED_SOURCE_BINDING",
                "READ_ONLY_ATTESTATION",
                "AUTHORIZED_TABLE_SCOPE",
                "COMPLETE_VERSIONED_SNAPSHOT",
                "ROW_COUNT_RECONCILIATION",
            ],
        },
        REALTIME_SOURCE_QUERY_V1: {
            "version": 1,
            "executor": "ORION_SOURCE_QUERY_GATEWAY",
            "arbitrary_sql": False,
            "requires": [
                "WHITELISTED_PARAMETERIZED_TEMPLATE",
                "READ_ONLY_ATTESTATION",
                "TIMEOUT_AND_ROW_LIMIT",
            ],
        },
        CROSS_SOURCE_IDENTITY_V1: {
            "version": 1,
            "executor": "ORION_EXPLICIT_IDENTITY_CONTRACT",
            "fuzzy_identity": False,
            "requires": [
                "EXPLICIT_JOIN_KEYS",
                "CARDINALITY",
                "COLLISION_POLICY",
                "PII_HANDLING",
            ],
        },
    }


def platform_capability_catalog() -> dict[str, Any]:
    """One platform-level inventory used by planning, gates and status APIs."""

    return {
        "schema_version": 1,
        "reasoning": reasoning_execution_capabilities(),
        "data": data_execution_capabilities(),
        "query_engines": {
            "DOCUMENT_FACT_CQ_V1": {
                "executor": "ORION_RELEASE_BOUND_DOCUMENT_SPARQL",
                "supports": ["FACT_QUERY", "EVIDENCE_QUERY", "AGGREGATION", "RELATION_TRAVERSAL"],
                "requires": ["REVIEWED_CQ_BINDINGS", "MANIFEST_BOUND_FACTS"],
            },
            "ONTOP_FACT_QUERY_V1": {
                "executor": "ONTOP_SPARQL",
                "supports": ["FACT_QUERY", "AGGREGATION", "RELATION_TRAVERSAL"],
            },
            "FUSEKI_DOCUMENT_EVIDENCE_V1": {
                "executor": "FUSEKI_SPARQL",
                "supports": ["DOCUMENT_EVIDENCE", "PROVENANCE_LOOKUP"],
            },
        },
    }
