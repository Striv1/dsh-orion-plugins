from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
from rdflib import Graph, Literal

ALLOWED_GRAPHS = {
    "urn:graph:ontology",
    "urn:graph:documents",
    "urn:graph:inferred",
    "urn:graph:provenance",
    "urn:graph:actions",
}
VERSIONED_DOCUMENT_GRAPH = re.compile(r"^urn:graph:documents:[A-Za-z0-9._~%:-]{1,512}$")
ENTITY_IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")


class FusekiClient:
    def __init__(
        self, dataset_url: str, timeout: float = 15.0, *, health_timeout: float | None = None
    ) -> None:
        self.dataset_url = dataset_url.rstrip("/")
        self.timeout = timeout
        self.health_timeout = timeout if health_timeout is None else health_timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, trust_env=False)

    @staticmethod
    def _validate_graph_uri(graph_uri: str) -> None:
        if graph_uri not in ALLOWED_GRAPHS and not VERSIONED_DOCUMENT_GRAPH.fullmatch(
            graph_uri
        ):
            raise ValueError("named graph is not allowed")

    def put_graph(self, graph_uri: str, graph: Graph) -> None:
        self._validate_graph_uri(graph_uri)
        with self._client() as client:
            response = client.put(
                f"{self.dataset_url}/data?graph={quote(graph_uri, safe='')}",
                content=graph.serialize(format="turtle"),
                headers={"Content-Type": "text/turtle"},
            )
            response.raise_for_status()

    def clear_graph(self, graph_uri: str) -> None:
        self._validate_graph_uri(graph_uri)
        with self._client() as client:
            response = client.delete(f"{self.dataset_url}/data?graph={quote(graph_uri, safe='')}")
            response.raise_for_status()

    def select(self, query: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
        if not query.lstrip().upper().startswith("SELECT") and "SELECT" not in query.upper()[:200]:
            raise ValueError("Fuseki client only accepts read-only SELECT queries")
        with self._client() as client:
            response = client.post(
                f"{self.dataset_url}/sparql", data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.timeout if timeout is None else timeout,
            )
            response.raise_for_status()
        return [{key: value.get("value") for key, value in row.items()} for row in response.json()["results"]["bindings"]]

    def documents_for_entity(self, entity_iri: str) -> list[dict[str, Any]]:
        """Read versioned S0 evidence linked to a reviewed ontology entity."""
        if not ENTITY_IRI.fullmatch(entity_iri):
            raise ValueError("invalid entity IRI")
        query = f"""
        PREFIX ev: <https://orion.local/ontology/evidence#>
        SELECT DISTINCT ?graph ?document ?document_id ?version ?title ?document_type
                        ?source_uri ?sha256 ?processed_at ?text ?page ?page_text
                        ?locator ?confidence WHERE {{
          GRAPH ?graph {{
            ?document a ev:DocumentVersion ;
                      ev:documentId ?document_id ;
                      ev:version ?version ;
                      ev:title ?title ;
                      ev:documentType ?document_type ;
                      ev:sourceUri ?source_uri ;
                      ev:fileSha256 ?sha256 ;
                      ev:processedAt ?processed_at ;
                      ev:mentionsEntity <{entity_iri}> .
            OPTIONAL {{
              ?document ev:hasPageEvidence ?page_evidence .
              ?page_evidence ev:pageNumber ?page ;
                             ev:evidenceText ?page_text .
              OPTIONAL {{ ?page_evidence ev:sourceLocator ?locator . }}
              OPTIONAL {{ ?page_evidence ev:confidence ?confidence . }}
            }}
            OPTIONAL {{
              ?document ev:documentText ?text .
              FILTER(!BOUND(?page_evidence))
            }}
          }}
          FILTER(
            STR(?graph) = "urn:graph:documents" ||
            STRSTARTS(STR(?graph), "urn:graph:documents:")
          )
        }}
        ORDER BY ?document_id DESC(?processed_at) ?page
        """
        return self.select(query)

    def search_documents(
        self,
        project_id: str,
        text: str,
        *,
        document_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search only versioned S0 evidence; current-version filtering is downstream."""

        if not project_id or len(project_id) > 160:
            raise ValueError("invalid project id")
        normalized = text.strip()
        if len(normalized) < 2 or len(normalized) > 200:
            raise ValueError("document search text must contain 2 to 200 characters")
        if limit < 1 or limit > 50:
            raise ValueError("document search limit must be between 1 and 50")
        ids = list(dict.fromkeys(document_ids or []))
        if len(ids) > 20 or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,159}", value)
            for value in ids
        ):
            raise ValueError("invalid document id filter")
        project_literal = Literal(project_id).n3()
        text_literal = Literal(normalized).n3()
        document_filter = ""
        if ids:
            values = " ".join(Literal(value).n3() for value in ids)
            document_filter = f"VALUES ?document_id {{ {values} }}"
        query = f"""
        PREFIX ev: <https://orion.local/ontology/evidence#>
        SELECT DISTINCT ?graph ?document ?document_id ?version ?title ?document_type
                        ?source_uri ?sha256 ?processed_at ?page ?page_text ?locator
                        ?confidence WHERE {{
          GRAPH ?graph {{
            ?document a ev:DocumentVersion ;
                      ev:projectId {project_literal} ;
                      ev:documentId ?document_id ;
                      ev:version ?version ;
                      ev:title ?title ;
                      ev:documentType ?document_type ;
                      ev:sourceUri ?source_uri ;
                      ev:fileSha256 ?sha256 ;
                      ev:processedAt ?processed_at .
            {document_filter}
            OPTIONAL {{
              ?document ev:hasPageEvidence ?page_evidence .
              ?page_evidence ev:pageNumber ?page ;
                             ev:evidenceText ?page_text .
              OPTIONAL {{ ?page_evidence ev:sourceLocator ?locator . }}
              OPTIONAL {{ ?page_evidence ev:confidence ?confidence . }}
            }}
            OPTIONAL {{ ?document ev:documentText ?document_text . }}
            BIND(COALESCE(?page_text, ?document_text) AS ?search_text)
            FILTER(CONTAINS(LCASE(STR(?search_text)), LCASE({text_literal})))
          }}
          FILTER(STRSTARTS(STR(?graph), "urn:graph:documents:"))
        }}
        ORDER BY ?document_id DESC(?processed_at) ?page
        LIMIT {min(limit * 5, 250)}
        """
        return self.select(query)

    def health(self) -> dict[str, Any]:
        query = "SELECT (COUNT(*) AS ?count) WHERE { GRAPH ?g { ?s ?p ?o } }"
        try:
            rows = self.select(query, timeout=self.health_timeout)
            return {"status": "healthy", "triple_count": int(rows[0]["count"]) if rows else 0}
        except (httpx.HTTPError, KeyError, ValueError, OSError) as exc:
            return {"status": "unavailable", "error": str(exc)}
