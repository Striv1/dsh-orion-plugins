"""Complete predicate-partitioned Ontop extraction with resumable verified chunks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import httpx
from rdflib import OWL, RDF, BNode, Graph, URIRef
from rdflib.exceptions import ParserError

from services.ontology_contracts.obda import parse_obda_prefixes, parse_obda_targets

from .rdf_terms import canonicalize_numeric_literals

PROFILE = "ontop-predicate-partitions-v1"


def compile_predicate_partition_plan(mapping_obda: str, ontology_ttl: str) -> dict[str, Any]:
    """An exhaustive static signature, including inferred properties and identity.

    Ontop's named ABox predicates come from mapping targets or ontology entity
    IRIs (including inverse/equivalent/super-properties), plus type and sameAs.
    Keeping every ontology IRI is a deliberate safe superset; empty partitions
    are harmless. Dynamic predicates, unresolved imports and blank-node targets
    cannot use this proof and must use a different complete extraction strategy.
    """
    ontology = Graph().parse(data=ontology_ttl, format="turtle")
    if any(ontology.triples((None, OWL.imports, None))):
        raise ValueError("S6_STATIC_SIGNATURE_IMPORTS: resolve the complete ontology import closure first")
    blocks = parse_obda_targets(mapping_obda)
    if not blocks or len(blocks) != len(re.findall(r"(?m)^\s*mappingId\s+", mapping_obda)):
        raise ValueError("S6_STATIC_SIGNATURE_TARGETS: not every OBDA target was parsed")
    marker = "ORION_MAPPING_VARIABLE_PLACEHOLDER"
    prefixes = "\n".join(f"@prefix {p}: <{iri}> ." for p, iri in parse_obda_prefixes(mapping_obda).items())
    predicates = {str(RDF.type), str(OWL.sameAs)}
    predicates.update(str(term) for triple in ontology for term in triple if isinstance(term, URIRef))
    mapped_predicates = set()

    def replace_token(match: re.Match[str]) -> str:
        token = match.group()
        if token.startswith("{"):
            if not re.fullmatch(r"\{[^{}]+\}(?:\^\^(?:<[^>]+>|[^\s]+)|@[\w-]+)?", token):
                raise ValueError("S6_STATIC_SIGNATURE_TARGETS: unsupported target placeholder")
            return f'"{marker}"'
        return re.sub(r"\{[^{}]+\}", marker, token)

    for block in blocks:
        template = re.sub(r'<[^>]*>|"(?:\\.|[^"\\])*"|[^\s]+', replace_token, block)
        target = Graph().parse(data=prefixes + "\n" + template, format="turtle")
        for subject, predicate, object_ in target:
            if marker in str(predicate):
                raise ValueError("S6_STATIC_SIGNATURE_DYNAMIC_PREDICATE: predicate depends on a database value")
            if isinstance(subject, BNode) or isinstance(object_, BNode):
                raise ValueError("S6_PARTITION_BLANK_NODE: target blank nodes require a single-snapshot materializer")
            mapped_predicates.add(str(predicate))
    predicates.update(mapped_predicates)
    return {
        "mode": "COMPLETE_STATIC_MAPPING_AND_ONTOLOGY_SIGNATURE",
        "mapping_sha256": "sha256:" + hashlib.sha256(mapping_obda.encode()).hexdigest(),
        "ontology_sha256": "sha256:" + hashlib.sha256(ontology_ttl.encode()).hexdigest(),
        "mapping_target_count": len(blocks), "mapped_predicate_count": len(mapped_predicates),
        "predicates": sorted(predicates),
    }


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_partition(graph: Graph, predicate: str) -> None:
    expected = URIRef(predicate)
    for subject, actual, object_ in graph:
        if actual != expected:
            raise ValueError("Ontop predicate partition returned an unexpected predicate")
        if isinstance(subject, BNode) or isinstance(object_, BNode):
            raise ValueError(
                "S6_PARTITION_BLANK_NODE: cross-query blank-node identity is not stable; "
                "a single-snapshot materializer is required for this mapping"
            )


def materialize_single_ontop_graph(*, endpoint: str, directory: Path, timeout_seconds: float = 180.0) -> Graph:
    """Retain full-query semantics when a mapping cannot be safely partitioned."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".full-graph-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream, httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
            with client.stream(
                "POST", endpoint, data={"query": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"},
                headers={"Accept": "text/turtle"},
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    stream.write(chunk)
            stream.flush()
        graph = Graph().parse(temporary, format="turtle")
        canonicalize_numeric_literals(graph)
        return graph
    finally:
        Path(temporary).unlink(missing_ok=True)


def materialize_ontop_partitions(
    *, endpoint: str, directory: Path, input_fingerprint: str, source_fingerprint: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
    timeout_seconds: float = 180.0,
    predicate_plan: dict[str, Any] | None = None,
    max_workers: int | None = None,
) -> tuple[Graph, dict[str, Any]]:
    """Partition the complete graph by actual predicate IRI, never by row samples.

    The caller must verify the full source-content fingerprint again afterwards.
    Each cache belongs to one mapping/model/source fingerprint. The caller either
    compiles a fresh exhaustive static signature or this call discovers it live.
    """
    if max_workers is None:
        max_workers = int(os.getenv("ORION_S6_MATERIALIZATION_WORKERS", "2"))
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 4:
        raise ValueError("materialization max_workers must be an integer from 1 to 4")
    identity = {
        "profile": PROFILE, "input_fingerprint": input_fingerprint,
        "source_fingerprint": source_fingerprint,
        "predicate_plan_sha256": hashlib.sha256(json.dumps(predicate_plan, sort_keys=True).encode()).hexdigest() if predicate_plan else None,
    }
    cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = directory / cache_key
    cache.mkdir(parents=True, exist_ok=True)
    graph = Graph()
    partitions = []
    with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
        if predicate_plan is None:
            response = client.post(
                endpoint, data={"query": "SELECT DISTINCT ?p WHERE { ?s ?p ?o }"},
                headers={"Accept": "application/sparql-results+json"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("head", {}).get("vars") != ["p"]:
                raise ValueError("Ontop predicate discovery did not return its declared projection")
            values = []
            for row in payload["results"]["bindings"]:
                binding = row.get("p") or {}
                if binding.get("type") != "uri" or not binding.get("value"):
                    raise ValueError("Ontop predicate discovery returned a non-IRI predicate")
                values.append(binding["value"])
        else:
            values = predicate_plan["predicates"]
        for value in values:
            URIRef(value).n3()  # Reject characters that could alter generated SPARQL.
        predicates = sorted(set(values))
        if progress:
            progress({"phase": "PREDICATES_DISCOVERED", "total_partitions": len(predicates),
                      "completed_partitions": 0, "triple_count": 0})
    def extract_partition(predicate: str) -> tuple[Graph, dict[str, Any]]:
        key = hashlib.sha256(predicate.encode()).hexdigest()
        graph_path, receipt_path = cache / f"{key}.nt", cache / f"{key}.json"
        partition = None
        reused = False
        if graph_path.is_file() and receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text())
                if (receipt.get("identity") == identity
                        and receipt.get("predicate") == predicate
                        and receipt.get("sha256") == _digest(graph_path)):
                    candidate = Graph().parse(graph_path, format="nt")
                    _check_partition(candidate, predicate)
                    if len(candidate) == receipt.get("triple_count"):
                        partition, reused = candidate, True
            except (OSError, ValueError, SyntaxError, ParserError):
                pass  # A damaged cache never supplies validation evidence.
        if partition is None:
            fd, temporary = tempfile.mkstemp(prefix=".partition-", dir=cache)
            try:
                iri = URIRef(predicate).n3()
                query = f"CONSTRUCT {{ ?s {iri} ?o }} WHERE {{ ?s {iri} ?o }}"
                with os.fdopen(fd, "wb") as stream:
                    with httpx.Client(timeout=timeout_seconds, trust_env=False) as partition_client, partition_client.stream(
                        "POST", endpoint, data={"query": query},
                        headers={"Accept": "application/n-triples"},
                    ) as result:
                        result.raise_for_status()
                        media_type = result.headers.get("content-type", "").split(";")[0].strip()
                        formats = {"application/n-triples": "nt", "text/turtle": "turtle",
                                   "application/x-turtle": "turtle"}
                        if media_type not in formats:
                            raise ValueError("Ontop returned an unsupported RDF response format")
                        for chunk in result.iter_bytes():
                            stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                partition = Graph().parse(temporary, format=formats[media_type])
                _check_partition(partition, predicate)
                # Normalize each completed part to one portable cached format.
                if formats[media_type] != "nt":
                    partition.serialize(destination=temporary, format="nt")
                os.replace(temporary, graph_path)
                _atomic_json(receipt_path, {
                    "identity": identity, "predicate": predicate,
                    "sha256": _digest(graph_path), "triple_count": len(partition),
                })
            finally:
                Path(temporary).unlink(missing_ok=True)
        return partition, {"predicate": predicate, "triple_count": len(partition),
                           "sha256": _digest(graph_path), "reused": reused}

    # Bounded submission limits completed-but-unmerged Graphs to max_workers.
    # Each worker owns its client, file and Graph; only this thread mutates the union.
    pending_predicates = iter(predicates)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ontop-partition") as executor:
        pending = {}
        for _ in range(min(max_workers, len(predicates))):
            predicate = next(pending_predicates)
            pending[executor.submit(extract_partition, predicate)] = predicate
        try:
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                # Check every completed outcome before scheduling more work.
                ready = [(future, future.result()) for future in completed]
                for future, (partition, row) in ready:
                    pending.pop(future)
                    # Cached and fresh partitions keep Ontop's lexical output;
                    # the union uses the shared canonical numeric terms.
                    canonicalize_numeric_literals(partition)
                    graph += partition
                    partitions.append(row)
                    if progress:
                        progress({"phase": "PARTITION_COMPLETED", "total_partitions": len(predicates),
                                  "completed_partitions": len(partitions), "triple_count": len(graph),
                                  "predicate": row["predicate"], "partition_reused": row["reused"]})
                refill = len(ready)
                # Completed Future objects own their Graph results. Release those
                # references before admitting another batch of partitions.
                del ready, completed, future, partition, row
                for _ in range(refill):
                    predicate = next(pending_predicates, None)
                    if predicate is not None:
                        pending[executor.submit(extract_partition, predicate)] = predicate
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    partitions.sort(key=lambda row: row["predicate"])
    receipt = {
        **identity, "status": "EXTRACTED_AWAITING_SOURCE_RECHECK",
        "predicate_count": len(predicates), "completed_partition_count": len(partitions),
        "triple_count": len(graph), "partitions": partitions,
        "coverage": "COMPLETE_STATIC_SIGNATURE_NO_LIMIT_OR_OFFSET" if predicate_plan else "COMPLETE_DISCOVERED_PREDICATE_SET_NO_LIMIT_OR_OFFSET",
        "predicate_plan": predicate_plan,
        "max_workers": max_workers,
    }
    _atomic_json(cache / "extraction.json", receipt)
    return graph, receipt
