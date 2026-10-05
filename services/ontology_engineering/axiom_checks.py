"""Supported OWL encodings, without changing published bytes or approval hashes."""

from rdflib import OWL, RDF, Graph, URIRef


def has_disjoint_axiom(graph: Graph, left: URIRef, right: URIRef) -> bool:
    """Accept either binary direction or an explicit, well-formed disjoint list.

    This checks a narrow OWL axiom, not general graph/ontology equivalence.
    Directional predicates such as rdfs:subClassOf must never use this helper.
    """
    if (left, OWL.disjointWith, right) in graph or (right, OWL.disjointWith, left) in graph:
        return True
    for axiom in graph.subjects(RDF.type, OWL.AllDisjointClasses):
        heads = list(graph.objects(axiom, OWL.members))
        if len(heads) != 1:
            continue
        node = heads[0]
        visited, members = set(), []
        valid = True
        while node != RDF.nil:
            if node in visited:
                valid = False
                break
            visited.add(node)
            first, rest = list(graph.objects(node, RDF.first)), list(graph.objects(node, RDF.rest))
            if len(first) != 1 or len(rest) != 1:
                valid = False
                break
            members.append(first[0])
            node = rest[0]
        # Self-disjointness needs two occurrences, not one list member.
        if (valid and len(members) >= 2 and left in members and right in members
                and (left != right or members.count(left) >= 2)):
            return True
    return False
