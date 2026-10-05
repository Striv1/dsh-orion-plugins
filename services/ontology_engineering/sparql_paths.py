"""Read mandatory graph predicates from frozen SPARQL, not text mentions."""

from rdflib import URIRef
from rdflib.paths import AlternativePath, InvPath, MulPath, SequencePath
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery


def graph_predicates(query: str) -> set[str]:
    if not query.strip():
        return set()

    def predicate(value):
        if isinstance(value, URIRef):
            return {str(value)}
        if isinstance(value, SequencePath):
            return set().union(*(predicate(item) for item in value.args))
        if isinstance(value, AlternativePath):
            return set.intersection(*(predicate(item) for item in value.args))
        if isinstance(value, InvPath):
            return predicate(value.arg)
        if isinstance(value, MulPath) and value.mod == '+':
            return predicate(value.path)
        # Variables, negated paths and zero-length paths prove no required edge.
        return set()

    def walk(value):
        if not isinstance(value, dict):
            return set()
        name = getattr(value, 'name', None)
        if name == 'BGP':
            return set().union(*(predicate(path) for _, path, _ in value.get('triples', [])))
        if name in {'LeftJoin', 'Minus'}:
            return walk(value['p1'])
        if name == 'Union':
            return walk(value['p1']) & walk(value['p2'])
        # Traverse graph algebra only: FILTER/BIND expressions and IRI constants
        # must not create proof of an edge. OPTIONAL/UNION arms need not match.
        return set().union(*(walk(value[key]) for key in ('p', 'p1', 'p2') if key in value))

    return walk(translateQuery(parseQuery(query)).algebra)
