"""Recoverable candidate graphs; never substitutes for formal S6 gate evidence."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from rdflib import Graph
from rdflib.exceptions import ParserError

from .rdf_terms import canonicalize_numeric_literals


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()


def load_graph_checkpoint(
    directory: Path, *, input_fingerprint: str, source_fingerprint: str
) -> Graph | None:
    manifest_path = directory / "materialization.json"
    graph_path = directory / "materialization.nt"
    if not manifest_path.is_file() or not graph_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("input_fingerprint") != input_fingerprint
            or manifest.get("source_fingerprint") != source_fingerprint
            or manifest.get("sha256") != _sha256(graph_path)
        ):
            return None
        graph = Graph().parse(graph_path, format="nt")
        if len(graph) != manifest.get("triple_count"):
            return None
        # Checkpoints written before canonicalization may hold duplicate lexical
        # forms of one numeric value; the in-memory candidate never does.
        canonicalize_numeric_literals(graph)
        return graph
    except (OSError, ValueError, SyntaxError, ParserError):
        # An incomplete or damaged cache is not a stage failure or a pass.
        return None


def save_graph_checkpoint(
    directory: Path, graph: Graph, *, input_fingerprint: str, source_fingerprint: str
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".materialization-", dir=directory)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            graph.serialize(destination=handle, format="nt")
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {
            "schema_version": 1,
            "input_fingerprint": input_fingerprint,
            "source_fingerprint": source_fingerprint,
            "sha256": _sha256(temporary_path),
            "triple_count": len(graph),
        }
        os.replace(temporary_path, directory / "materialization.nt")
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=directory)
        temporary_path = Path(temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, directory / "materialization.json")
    finally:
        temporary_path.unlink(missing_ok=True)
