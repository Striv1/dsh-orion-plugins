"""Per-run verified fact-package reuse for the existing reasoning fact route."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rdflib import Graph

from services.realtime_qa.reasoning import document_evidence_facts


def _identity(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def materialize_reviewed_document_facts(
    *, runtime_dir: Path, runtime: dict[str, Any],
    term_kinds: dict[str, str], term_datatypes: dict[str, str], strict_types: bool,
    validate_bindings: Callable[..., None], materialize_fact: Callable[..., Any],
    metrics: dict[str, int] | None = None,
) -> tuple[Graph, int]:
    """Reuse only identical bytes and term contracts inside this fixed snapshot.

    Count retains the historical per-capability coverage denominator; skipping a
    duplicate graph insertion must not silently change audit coverage semantics.
    All declared checksums and every capability's bindings remain validated.
    """
    graph = Graph()
    count = 0
    packages: dict[Path, tuple[str, list[dict[str, Any]], tuple[int, ...]]] = {}
    parsed_content: dict[str, list[dict[str, Any]]] = {}
    instantiated: set[tuple[str, str]] = set()
    stats = {"package_reads": 0, "package_parses": 0, "materialized_fact_calls": 0,
             "reused_capabilities": 0, "covered_fact_references": 0}
    root = runtime_dir.resolve()
    capabilities = list((runtime.get("reasoning_capabilities") or {}).values())
    capabilities.extend(
        {**query, "evidence_query": name}
        for name, query in (runtime.get("document_fact_queries") or {}).items()
        if query.get("cq_bindings")
    )
    for capability in capabilities:
        validate_bindings(capability, term_kinds)
        terms = capability.get("ontology_terms") or {}
        query = (runtime.get("document_fact_queries") or {}).get(str(capability.get("evidence_query") or "")) or {}
        artifact = str(query.get("fact_artifact") or "")
        if not artifact:
            continue
        relative = Path(artifact)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("S6 文档事实包必须位于正式 runtime 目录内。")
        path = root / relative
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and root in parent.parents):
            raise RuntimeError("S6 文档事实包不能是符号链接。")
        path = path.resolve()
        if root not in path.parents or not path.is_file():
            raise RuntimeError("S6 文档事实包不存在或超出正式目录。")
        if path not in packages:
            before = _identity(path)
            data = path.read_bytes()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if _identity(path) != before:
                raise RuntimeError("S6 文档事实包读取期间发生漂移。")
            if digest not in parsed_content:
                package = json.loads(data)
                facts = package.get("facts") if isinstance(package, dict) else None
                if not isinstance(facts, list) or any(not isinstance(item, dict) for item in facts):
                    raise RuntimeError("S6 文档事实包 facts 结构不合法。")
                parsed_content[digest] = facts
                stats["package_parses"] += 1
            packages[path] = (digest, parsed_content[digest], before)
            stats["package_reads"] += 1
        digest, facts, _ = packages[path]
        expected = query.get("fact_sha256")
        if (strict_types or expected) and expected != digest:
            raise RuntimeError("S6 文档事实包与已审 fact_sha256 不一致。")
        # Other arguments are fixed for the whole invocation. Do not reuse a
        # package under a different predicate->ontology IRI mapping.
        fact_bindings = capability.get("fact_bindings") or []
        parameters = (capability.get("runtime_validation") or {}).get("parameters") or {}
        key = (digest, json.dumps([terms, fact_bindings, parameters], sort_keys=True, ensure_ascii=False))
        count += len(facts)
        if key in instantiated:
            stats["reused_capabilities"] += 1
            continue
        symbols: dict[str, str] = {}
        if strict_types and not fact_bindings:
            raise RuntimeError("S6 文档事实缺少正式 fact_bindings，不能猜测上线转换语义。")
        bound_facts = document_evidence_facts(facts, fact_bindings, symbols, parameters=parameters) if fact_bindings else [str(item.get("fact") or "").strip() for item in facts]
        for expression in bound_facts:
            materialize_fact(graph, expression, terms, symbol_table=symbols,
                             term_kinds=term_kinds, term_datatypes=term_datatypes,
                             strict_types=strict_types)
            stats["materialized_fact_calls"] += 1
        instantiated.add(key)
    for path, (_, _, identity) in packages.items():
        if path.is_symlink() or _identity(path) != identity:
            raise RuntimeError("S6 文档事实包在实例化期间发生漂移，不能复用该结果。")
    stats["covered_fact_references"] = count
    if metrics is not None:
        metrics.update(stats)
    return graph, count
