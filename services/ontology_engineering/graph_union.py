"""A default-graph set union without copying the constituent RDF graphs."""

from __future__ import annotations

from collections.abc import Iterable

from rdflib import Graph
from rdflib.graph import ModificationException
from rdflib.paths import Path


class ReadOnlyGraphUnion(Graph):
    """Read-only view with the same triple set as ``left + right``.

    Source graphs must remain unchanged while the view is queried. Membership
    lookups in earlier graphs suppress overlap without another full triple set.
    This is one default graph, not a dataset of named source graphs.
    """

    def __init__(self, graphs: Iterable[Graph]):
        super().__init__()
        self.graphs = tuple(graphs)
        if not all(isinstance(graph, Graph) for graph in self.graphs):
            raise TypeError("ReadOnlyGraphUnion requires RDF graphs")
        for prefix, uri in set(pair for graph in self.graphs for pair in graph.namespaces()):
            self.bind(prefix, uri)

    def triples(self, triple):
        subject, predicate, object_ = triple
        if isinstance(predicate, Path):
            # Evaluate paths once over the union, including hops across sources.
            for start, end in predicate.eval(self, subject, object_):
                yield start, predicate, end
            return
        for index, graph in enumerate(self.graphs):
            for item in graph.triples(triple):
                if not any(item in previous for previous in self.graphs[:index]):
                    yield item

    def triples_choices(self, triple, context=None):
        for index, graph in enumerate(self.graphs):
            for item in graph.triples_choices(triple):
                if not any(item in previous for previous in self.graphs[:index]):
                    yield item

    def __len__(self):
        return sum(1 for _ in self)

    def _deny_mutation(self, *args, **kwargs):
        raise ModificationException()

    add = addN = remove = set = parse = update = __iadd__ = __isub__ = _deny_mutation
