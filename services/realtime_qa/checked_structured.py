"""Release-isolated transport for already AST-checked structured SELECT queries.

Only checked_query calls this internal adapter. No caller-selected endpoint,
SQL credentials, graph selector, or mapping path is part of its public contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef

from services.ontology_contracts.obda import parse_obda_prefixes, parse_obda_targets
from services.realtime_qa.deployment import verify_ontop_deployment_binding

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 100
TIMEOUT = 10.0


def _artifact(binding, relative):
    root = Path(binding.package_path).resolve()
    if not relative or Path(relative).is_absolute():
        raise ValueError("checked structured artifact must be package-relative")
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("checked structured artifact escapes package")
    data = path.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != binding.artifact_checksums.get(relative):
        raise ValueError("checked structured protected artifact hash mismatch")
    return data


def _vocabulary(binding, unavailable_terms=None):
    mapping = _artifact(binding, binding.mapping_artifact).decode("utf-8")
    ontology = Graph().parse(data=_artifact(binding, binding.ontology_artifact), format="turtle")
    if any(ontology.triples((None, OWL.imports, None))):
        raise ValueError("checked structured unresolved ontology imports")
    kinds = {}
    for rdfkind, name in [
        (OWL.Class, "CLASS"),
        (OWL.ObjectProperty, "OBJECT_PROPERTY"),
        (OWL.DatatypeProperty, "DATA_PROPERTY"),
    ]:
        for term in ontology.subjects(RDF.type, rdfkind):
            if not isinstance(term, URIRef):
                continue
            if str(term) in kinds:
                raise ValueError("checked structured ambiguous ontology term kind")
            kinds[str(term)] = name
    blocks = parse_obda_targets(mapping)
    if not blocks or len(blocks) != len(re.findall(r"(?m)^\s*mappingId\s+", mapping)):
        raise ValueError("checked structured incomplete static mapping signature")
    marker = "ORION_CHECKED_MAPPING_VARIABLE"
    prefix_map = parse_obda_prefixes(mapping)
    prefixes = "\n".join(f"@prefix {key}: <{value}> ." for key, value in prefix_map.items())
    mapped = set()

    def replace(match):
        token = match.group()
        if token.startswith("{"):
            if not re.fullmatch(r"\{[^{}]+\}(?:\^\^(?:<[^>]+>|[^\s]+)|@[\w-]+)?", token):
                raise ValueError("checked structured unsupported mapping placeholder")
            return f'"{marker}"'
        replaced = re.sub(r"\{[^{}]+\}", marker, token)
        # Ontop accepts prefixed IRI templates such as ex:record/{id}; Turtle
        # does not allow the slash in that prefixed local name. Expand only
        # unquoted template tokens using the mapping's declared prefix.
        if marker in replaced and not replaced.startswith(("<", '"')):
            prefix, separator, local = replaced.partition(":")
            if separator and prefix in prefix_map:
                return "<" + prefix_map[prefix] + local + ">"
        return replaced

    for block in blocks:
        template = re.sub(r'<[^>]*>|"(?:\\.|[^"\\])*"|[^\s]+', replace, block)
        target = Graph().parse(data=prefixes + "\n" + template, format="turtle")
        for subject, predicate, obj in target:
            if isinstance(subject, BNode) or isinstance(obj, BNode) or marker in str(predicate):
                raise ValueError("checked structured dynamic or blank-node mapping target")
            if str(predicate).startswith("urn:orion:ontop:"):
                continue  # Internal deployment marker is never discoverable business vocabulary.
            if predicate in {RDFS.label, RDFS.comment}:
                # Annotation targets carry display text, not a declared OWL
                # business data property. They do not widen checked vocabulary.
                if not isinstance(obj, Literal):
                    raise ValueError("checked structured annotation target must be a literal")
                if unavailable_terms is not None:
                    unavailable_terms.append({"iri": str(predicate), "reason": "ANNOTATION_NOT_BUSINESS_QUERY_TERM"})
                continue
            term = obj if predicate == RDF.type else predicate
            if not isinstance(term, URIRef) or marker in str(term):
                raise ValueError(
                    f"checked structured mapping target has no static typed business term: {term}"
                )
            if str(term) not in kinds:
                if unavailable_terms is not None:
                    unavailable_terms.append({"iri": str(term), "reason": "NO_DECLARED_UNIQUE_OWL_BUSINESS_TYPE"})
                continue
            if predicate == RDF.type and kinds[str(term)] != "CLASS":
                raise ValueError("checked structured rdf:type target is not a class")
            mapped.add(str(term))
    if not mapped:
        raise ValueError("checked structured mapping has no business vocabulary")
    return {term: kinds[term] for term in sorted(mapped)}


def prepare_checked_structured(runtime, dataset="structured") -> dict[str, Any]:
    if dataset not in (None, "structured"):
        raise ValueError("checked structured dataset must be structured")
    binding, client = runtime.binding, runtime.ontop
    identity = getattr(client, "deployment_identity", None) or {}
    path = getattr(runtime, "deployment_binding_path", None)
    if (
        not binding.structured_query_enabled
        or binding.integrity_status != "verified"
        or not path
        or client.runtime_verification_status != "VERIFIED"
        or client.bound_release_fingerprint != binding.release_fingerprint
    ):
        raise ValueError("checked structured requires a verified isolated deployment")
    expected = {
        "endpoint": client.endpoint,
        "release_fingerprint": binding.release_fingerprint,
        "mapping_sha256": binding.artifact_checksums[binding.mapping_artifact],
        "deployment_id": binding.ontop_deployment_id,
        "access_mode": "READ_ONLY",
        "status": "READY",
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("checked structured deployment identity mismatch")
    selected = {
        key: getattr(binding, key)
        for key in (
            "project_id",
            "release_version",
            "release_fingerprint",
            "ontop_deployment_id",
            "mapping_artifact",
            "ontology_artifact",
            "source_mapping_sha256",
            "package_path",
            "ontop_identity_query_artifact",
            "artifact_checksums",
        )
    }
    config = {
        "binding": selected,
        "deployment_binding_path": str(path),
        "endpoint": client.endpoint,
    }
    _verify(config)
    unavailable_terms = []
    config["term_kinds"] = _vocabulary(binding, unavailable_terms)
    config["unavailable_terms"] = [dict(iri=iri, reason=reason) for iri, reason in sorted({(item["iri"], item["reason"]) for item in unavailable_terms})]
    ontology = Graph().parse(data=_artifact(binding, binding.ontology_artifact), format="turtle")
    config["term_datatypes"] = {}
    for term, kind in config["term_kinds"].items():
        if kind == "DATA_PROPERTY":
            ranges = set(ontology.objects(URIRef(term), RDFS.range))
            if len(ranges) == 1 and all(isinstance(value, URIRef) for value in ranges):
                config["term_datatypes"][term] = str(next(iter(ranges)))
    config["domains"] = {
        term: {
            str(value)
            for value in ontology.objects(URIRef(term), RDFS.domain)
            if isinstance(value, URIRef)
        }
        for term in config["term_kinds"]
    }
    config["disjoint_classes"] = {
        frozenset((str(a), str(b)))
        for a, b in ontology.subject_objects(OWL.disjointWith)
        if isinstance(a, URIRef) and isinstance(b, URIRef)
    }
    return config


def _verify(config):
    binding = SimpleNamespace(**config["binding"])
    deployment = verify_ontop_deployment_binding(binding, Path(config["deployment_binding_path"]))
    if deployment.get("endpoint") != config["endpoint"]:
        raise ValueError("checked structured endpoint changed")
    url = urlsplit(config["endpoint"])
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("checked structured invalid bound endpoint")
    _artifact(binding, binding.mapping_artifact)
    _artifact(binding, binding.ontology_artifact)
    return binding


def discover_structured_space(runtime):
    config = prepare_checked_structured(runtime)
    binding = config["binding"]
    ontology = Graph().parse(
        data=_artifact(SimpleNamespace(**binding), binding["ontology_artifact"]), format="turtle"
    )
    terms = []
    for iri, kind in config["term_kinds"].items():
        node = URIRef(iri)
        terms.append(
            {
                "iri": iri,
                "kind": kind,
                "labels": [
                    {"value": str(value), "language": value.language}
                    for value in ontology.objects(node, RDFS.label)
                    if isinstance(value, Literal)
                ],
                "comments": [
                    {"value": str(value), "language": value.language}
                    for value in ontology.objects(node, RDFS.comment)
                    if isinstance(value, Literal)
                ],
                "domains": sorted(
                    str(value)
                    for value in ontology.objects(node, RDFS.domain)
                    if isinstance(value, URIRef)
                ),
                "ranges": sorted(
                    str(value)
                    for value in ontology.objects(node, RDFS.range)
                    if isinstance(value, URIRef)
                ),
                "datatype": config["term_datatypes"].get(iri),
            }
        )
    return {
        "dataset_id": "structured",
        "source_kind": "structured_db",
        "release_fingerprint": binding["release_fingerprint"],
        "mapping_sha256": binding["artifact_checksums"][binding["mapping_artifact"]],
        "term_kinds": config["term_kinds"],
        "allowed_iris": sorted(config["term_kinds"]),
        "terms": terms,
        "unavailable_terms": config["unavailable_terms"],
        "semantic_metadata_source": "PROTECTED_RELEASE_OWL_EXPLICIT_ANNOTATIONS",
        "max_rows": MAX_ROWS,
        "timeout_seconds": TIMEOUT,
        "max_response_bytes": MAX_BYTES,
    }


discover_checked_structured = discover_structured_space


def _select(endpoint, query, max_rows):
    with (
        httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client,
        client.stream(
            "POST",
            endpoint,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        ) as response,
    ):
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError("checked structured response byte limit exceeded")
    payload = json.loads(content)
    variables = payload.get("head", {}).get("vars")
    bindings = payload.get("results", {}).get("bindings")
    if (
        not isinstance(variables, list)
        or not all(isinstance(v, str) for v in variables)
        or len(set(variables)) != len(variables)
        or not isinstance(bindings, list)
        or len(bindings) > max_rows
    ):
        raise ValueError("checked structured invalid SELECT response or row limit exceeded")
    rows = []
    terms = []
    for row in bindings:
        if not isinstance(row, dict) or set(row) - set(variables):
            raise ValueError("checked structured malformed row")
        values = {v: None for v in variables}
        typed = {}
        for key, term in row.items():
            if (
                not isinstance(term, dict)
                or term.get("type") not in {"uri", "literal", "typed-literal"}
                or not isinstance(term.get("value"), str)
            ):
                raise ValueError("checked structured unsupported RDF result term")
            if any(k not in {"type", "value", "datatype", "xml:lang"} for k in term):
                raise ValueError("checked structured unsupported RDF term metadata")
            value = term["value"]
            if term["type"] != "uri":
                datatype = term.get("datatype")
                lang = term.get("xml:lang")
                if (
                    (datatype is not None and not isinstance(datatype, str))
                    or (lang is not None and not isinstance(lang, str))
                    or (datatype and lang)
                ):
                    raise ValueError("checked structured invalid literal metadata")
                parsed = Literal(value, datatype=URIRef(datatype) if datatype else None, lang=lang)
                if getattr(parsed, "ill_typed", False):
                    raise ValueError("checked structured invalid typed literal")
                native = parsed.toPython()
                if isinstance(native, float) and not math.isfinite(native):
                    raise ValueError("checked structured non-finite numeric scalar")
                if type(native) in {str, int, float, bool}:
                    value = native
            values[key] = value
            typed[key] = dict(term)
        rows.append(values)
        terms.append(typed)
    return {"variables": variables, "rows": rows, "row_terms": terms}


def _marker(config, binding):
    query = _artifact(binding, binding.ontop_identity_query_artifact).decode("utf-8")
    result = _select(config["endpoint"], query, 1)
    expected = {
        "deployment_id": binding.ontop_deployment_id,
        "source_mapping_sha256": binding.source_mapping_sha256,
        "access_mode": "READ_ONLY",
    }
    if len(result["rows"]) != 1 or result["rows"][0] != expected:
        raise ValueError("checked structured live mapping marker mismatch")


def execute_checked_structured(runtime, sparql, limit):
    """Internal: sparql must be the output of checked_query.validate_checked_select."""
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError("checked structured limit must be 1..100")
    config = runtime if isinstance(runtime, dict) else prepare_checked_structured(runtime)
    binding = _verify(config)
    _marker(config, binding)
    # checked_query owns AST validation and adds/checks a top-level LIMIT <= limit+1.
    query = sparql
    result = _select(config["endpoint"], query, limit + 1)
    truncated = len(result["rows"]) > limit
    result["rows"] = result["rows"][:limit]
    result["row_terms"] = result["row_terms"][:limit]
    _verify(config)
    _marker(config, binding)
    return {
        **result,
        "truncated": truncated,
        "row_count": len(result["rows"]),
        "source_kind": "structured_db",
        "dataset_id": "structured",
        "release_fingerprint": binding.release_fingerprint,
        "mapping_sha256": binding.artifact_checksums[binding.mapping_artifact],
        "query_sha256": "sha256:" + hashlib.sha256(sparql.encode()).hexdigest(),
        "execution_query_sha256": "sha256:" + hashlib.sha256(query.encode()).hexdigest(),
        "deployment_id": binding.ontop_deployment_id,
        "row_limit": limit,
    }
