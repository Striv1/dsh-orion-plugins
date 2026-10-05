from collections import Counter

import pytest
from rdflib import Graph, Namespace
from rdflib.graph import ModificationException

from services.ontology_engineering.graph_union import ReadOnlyGraphUnion

EX = Namespace("urn:union:")


@pytest.fixture
def union_graphs():
    left, right = Graph(), Graph()
    left.add((EX.a, EX.p, EX.b))
    right.add((EX.a, EX.p, EX.b))
    right.add((EX.b, EX.p, EX.c))
    right.add((EX.a, EX.q, EX.c))
    left.bind("ex", EX)
    right.bind("ex", EX)
    return left, right, ReadOnlyGraphUnion([left, right])


def test_union_iteration_count_membership_and_choices_match_materialized_union(union_graphs):
    left, right, view = union_graphs
    expected = left + right
    assert len(view) == len(expected) == 3
    assert Counter(view) == Counter(expected)
    assert (EX.a, EX.p, EX.b) in view
    assert (EX.a, None, None) in view
    assert (EX.c, None, None) not in view
    for choices in (([EX.a, EX.b], None, None), (None, [EX.p, EX.q], None), (None, None, [EX.b, EX.c])):
        assert Counter(view.triples_choices(choices)) == Counter(expected.triples_choices(choices))
    assert view.graphs[0] is left and view.graphs[1] is right
    assert len(view.store) == 0  # No copy of the full source graph.


@pytest.mark.parametrize("query", [
    "SELECT (COUNT(*) AS ?count) WHERE { ?s ?p ?o }",
    "SELECT ?s ?o WHERE { ?s ex:p ?o }",
    "SELECT ?s ?o WHERE { ?s ex:p/ex:p ?o }",
    "SELECT ?s ?o WHERE { ?s ex:p+ ?o }",
    "SELECT ?s ?o WHERE { ?s ^ex:p ?o }",
    "SELECT ?s ?o WHERE { ?s (ex:p|ex:q) ?o }",
    "ASK { ex:a ex:p/ex:p ex:c }",
    "CONSTRUCT { ?s ex:linked ?o } WHERE { ?s ex:p+ ?o }",
])
def test_real_sparql_default_graph_and_paths_match_union(union_graphs, query):
    left, right, view = union_graphs
    actual, expected = view.query(query), (left + right).query(query)
    if actual.type == "ASK":
        assert actual.askAnswer == expected.askAnswer
    elif actual.type == "CONSTRUCT":
        assert set(actual.graph) == set(expected.graph)
    else:
        assert Counter(actual) == Counter(expected)


@pytest.mark.parametrize("method,args", [
    ("add", ((EX.c, EX.p, EX.a),)), ("addN", ([],)), ("remove", ((None, None, None),)),
    ("set", ((EX.c, EX.p, EX.a),)), ("parse", ()), ("update", ("CLEAR ALL",)),
    ("__iadd__", (Graph(),)), ("__isub__", (Graph(),)),
])
def test_union_rejects_data_mutation(union_graphs, method, args):
    left, right, view = union_graphs
    with pytest.raises(ModificationException):
        getattr(view, method)(*args)
    assert len(left) == 1 and len(right) == 3
