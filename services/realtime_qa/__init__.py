"""Release-bound realtime evidence retrieval for ontology Q&A.

Public conveniences are loaded lazily so low-level clients can import a single
validation module without initializing the complete realtime runtime graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "RealtimeAnswerService": ("services.realtime_qa.answer", "RealtimeAnswerService"),
    "OntopDeploymentVerifier": (
        "services.realtime_qa.binding",
        "OntopDeploymentVerifier",
    ),
    "ReleaseBindingLoader": ("services.realtime_qa.binding", "ReleaseBindingLoader"),
    "S0IncrementalDocumentIndexer": (
        "services.realtime_qa.document_index",
        "S0IncrementalDocumentIndexer",
    ),
    "DocumentCurrentRegistry": (
        "services.realtime_qa.incremental_pipeline",
        "DocumentCurrentRegistry",
    ),
    "DocumentPromotionConflict": (
        "services.realtime_qa.incremental_pipeline",
        "DocumentPromotionConflict",
    ),
    "MinioOriginalSnapshotStore": (
        "services.realtime_qa.incremental_pipeline",
        "MinioOriginalSnapshotStore",
    ),
    "S0IncrementalPipeline": (
        "services.realtime_qa.incremental_pipeline",
        "S0IncrementalPipeline",
    ),
    "PostgresDocumentCurrentRegistry": (
        "services.realtime_qa.postgres_registry",
        "PostgresDocumentCurrentRegistry",
    ),
    "RealtimeRuntimeRegistry": ("services.realtime_qa.runtime", "RealtimeRuntimeRegistry"),
    "ReviewedEntityLinks": ("services.realtime_qa.s0_adapter", "ReviewedEntityLinks"),
    "S0BundleAdapter": ("services.realtime_qa.s0_adapter", "S0BundleAdapter"),
    "S0RealtimePublisher": ("services.realtime_qa.s0_adapter", "S0RealtimePublisher"),
    "HybridRealtimeEvidenceService": (
        "services.realtime_qa.service",
        "HybridRealtimeEvidenceService",
    ),
    "SemanticaReasoningClient": (
        "services.realtime_qa.reasoning",
        "SemanticaReasoningClient",
    ),
}

_MODEL_EXPORTS = {
    "DocumentIndexReceipt",
    "DocumentPageEvidence",
    "DocumentQueryRequest",
    "EvidenceBundle",
    "EvidenceCitation",
    "IncrementalIngestionResult",
    "OntologyReleaseBinding",
    "RealtimeAnswer",
    "RealtimeAnswerRequest",
    "RealtimeEvidenceRequest",
    "RealtimeReleaseExpectation",
    "ReasoningQueryRequest",
    "RealtimeRuntimeBinding",
    "RealtimeRuntimeCatalog",
    "RealtimeRuntimeRegistration",
    "RealtimeRuntimeRegistryConfig",
    "RealtimeSessionAnswer",
    "RealtimeSessionAnswerRequest",
    "S0DocumentVersion",
    "StructuredQueryRequest",
}

__all__ = sorted([*_EXPORTS, *_MODEL_EXPORTS])


def __getattr__(name: str) -> Any:
    if name in _MODEL_EXPORTS:
        module_name, attribute_name = "services.realtime_qa.models", name
    elif name in _EXPORTS:
        module_name, attribute_name = _EXPORTS[name]
    else:
        raise AttributeError(name)
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
