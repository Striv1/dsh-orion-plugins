"""Strict process-local reuse of parsed NT graphs; unsupported inputs fall back."""

import hashlib
import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from rdflib import Graph, Literal, URIRef
from rdflib.plugins.serializers.nt import NTSerializer
from rdflib.plugins.stores.memory import Memory

_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:[^\s\x00-\x20\x7f-\x9f<>"{}|^`\\]*$')
_VALUES = {type(None), str, bool, int, float, Decimal, date, datetime, time, timedelta, bytes}


def _stable_literal(term):
    if type(term.value) not in _VALUES:
        return False
    if term.datatype is not None and not _IRI.fullmatch(str(term.datatype)):
        return False
    # The NT parser constructs exactly this lexical Literal under the current
    # rdflib normalization policy; value flags must retain validation semantics.
    parsed = Literal(str(term), lang=term.language, datatype=term.datatype)
    if parsed != term or type(parsed.value) is not type(term.value):
        return False
    if (parsed.ill_typed is True) != (term.ill_typed is True):
        return False
    if isinstance(term.value, float) and math.isnan(term.value):
        return math.isnan(parsed.value)
    if isinstance(term.value, Decimal) and term.value.is_nan():
        return parsed.value.is_nan() and term.value.is_snan() == parsed.value.is_snan()
    return parsed.value == term.value


class _DigestSink:
    def __init__(self):
        self.digest = hashlib.sha256()

    def write(self, data):
        self.digest.update(data)


def native_graph_matches(graph, content, *, format="turtle"):
    """Return False, never waive parsing, when reuse cannot be proven.

    Only for synchronous platform-owned graphs. This is not a security boundary
    against arbitrary code in the same Python process or concurrent ABA writes.
    Call before validation and again before sealing its result; handoff must own
    all mutable aliases for the entire validation interval.
    """
    if format != "nt" or type(graph) is not Graph or type(graph.store) is not Memory:
        return False
    try:
        for subject, predicate, obj in graph:
            if type(subject) is not URIRef or type(predicate) is not URIRef:
                return False
            if not _IRI.fullmatch(str(subject)) or not _IRI.fullmatch(str(predicate)):
                return False
            if type(obj) is URIRef:
                if not _IRI.fullmatch(str(obj)):
                    return False
            elif type(obj) is Literal:
                if not _stable_literal(obj):
                    return False
            else:
                return False
        expected = hashlib.sha256()
        for start in range(0, len(content), 1024 * 1024):
            expected.update(content[start:start + 1024 * 1024].encode("utf-8"))
        streamed = _DigestSink()
        NTSerializer(graph).serialize(streamed)
        return expected.digest() == streamed.digest.digest()
    except Exception:
        return False
