from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote

from rdflib import RDF, XSD, Graph, Literal, Namespace, URIRef

from services.fuseki_client.client import FusekiClient
from services.realtime_qa.models import DocumentIndexReceipt, S0DocumentVersion

EV = Namespace("https://orion.local/ontology/evidence#")


class S0IncrementalDocumentIndexer:
    """Publish one immutable S0 document version to its own Fuseki named graph."""

    def __init__(
        self,
        fuseki: FusekiClient,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.fuseki = fuseki
        self.now = now or (lambda: datetime.now(UTC))

    def index(self, document: S0DocumentVersion) -> DocumentIndexReceipt:
        graph_uri = self.graph_uri(document)
        graph = self.build_graph(document)
        self.fuseki.put_graph(graph_uri, graph)
        return DocumentIndexReceipt(
            project_id=document.project_id,
            document_id=document.document_id,
            version=document.version,
            graph_uri=graph_uri,
            file_sha256=document.file_sha256,
            triple_count=len(graph),
            indexed_at=self.now(),
        )

    @staticmethod
    def graph_uri(document: S0DocumentVersion) -> str:
        project = quote(document.project_id, safe="")
        document_id = quote(document.document_id, safe="")
        return f"urn:graph:documents:{project}:{document_id}:{document.file_sha256}"

    @staticmethod
    def build_graph(document: S0DocumentVersion) -> Graph:
        graph = Graph()
        graph.bind("ev", EV)
        subject = URIRef(
            "urn:orion:document-version:"
            f"{quote(document.project_id, safe='')}:"
            f"{quote(document.document_id, safe='')}:"
            f"{document.file_sha256}"
        )
        graph.add((subject, RDF.type, EV.DocumentVersion))
        graph.add((subject, EV.projectId, Literal(document.project_id)))
        graph.add((subject, EV.documentId, Literal(document.document_id)))
        graph.add((subject, EV.version, Literal(document.version)))
        graph.add((subject, EV.title, Literal(document.title)))
        graph.add((subject, EV.documentType, Literal(document.document_type)))
        graph.add((subject, EV.sourceUri, Literal(document.source_uri)))
        graph.add((subject, EV.fileSha256, Literal(document.file_sha256)))
        graph.add(
            (
                subject,
                EV.processedAt,
                Literal(document.processed_at.isoformat(), datatype=XSD.dateTime),
            )
        )
        graph.add((subject, EV.documentText, Literal(document.full_text)))
        for entity_iri in document.mentions_entities:
            graph.add((subject, EV.mentionsEntity, URIRef(entity_iri)))
        for page in document.pages:
            locator = page.source_locator or f"page:{page.page_number}"
            locator_digest = hashlib.sha256(locator.encode("utf-8")).hexdigest()[:16]
            page_subject = URIRef(f"{subject}:evidence:{locator_digest}")
            graph.add((subject, EV.hasPageEvidence, page_subject))
            graph.add((page_subject, RDF.type, EV.PageEvidence))
            graph.add(
                (
                    page_subject,
                    EV.pageNumber,
                    Literal(page.page_number, datatype=XSD.integer),
                )
            )
            graph.add((page_subject, EV.evidenceText, Literal(page.text)))
            if page.source_locator:
                graph.add((page_subject, EV.sourceLocator, Literal(page.source_locator)))
            if page.confidence is not None:
                graph.add(
                    (
                        page_subject,
                        EV.confidence,
                        Literal(page.confidence, datatype=XSD.decimal),
                    )
                )
        return graph
