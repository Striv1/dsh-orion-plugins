"""Invocation-local RDFLib BGP planning; no global evaluator or query text edits.

RDFLib 7.1.4 sorts an entire BGP once by its initial bindings. Independent
``rdf:type`` patterns may then precede their connecting edge, producing a large
Cartesian intermediate. Equivalent singleton BGPs joined lazily preserve the
bindings from every preceding step, and keep all outer SPARQL operators intact.
Deterministic filters may additionally be copied below joins/optional expansion
when all referenced variables are already guaranteed bound. Their original
expressions and positions remain part of the plan; query text never changes.
Unsupported contracts use the original query evaluator.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from importlib.metadata import version
from time import perf_counter
from typing import Any

from rdflib import BNode, ConjunctiveGraph, Dataset, Literal, URIRef, Variable
from rdflib.plugins.sparql import CUSTOM_EVALS, prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

POLICY = "connected-singleton-bgp-v4-substitutable-join"
# Join operands that may receive outer bindings without changing SPARQL join
# semantics: a pure BGP/Join/UNION subtree has no FILTER, BIND, OPTIONAL,
# MINUS, VALUES, subquery or aggregate scope that could observe them.
SUBSTITUTABLE_NODES = {"BGP", "Join", "Union"}
VERIFIED_RDFLIB = "7.1.4"
FILTER_POLICY = "guaranteed-bound-filter-copy-v1"
EXISTS_NODES = {"Builtin_EXISTS", "Builtin_NOTEXISTS"}
SUPPORTED_NODES = {
    "AggregateJoin",
    "Group",
    "Aggregate_GroupConcat",
    "Aggregate_Sample",
    # Order-independent aggregates over the complete input bag. MIN/MAX follow
    # RDFLib's own term ordering; SUM/AVG over xsd:double may differ only in
    # floating-point rounding, which SPARQL leaves implementation-defined.
    "Aggregate_Count",
    "Aggregate_Min",
    "Aggregate_Max",
    "Aggregate_Sum",
    "Aggregate_Avg",
    # (NOT) EXISTS keeps its native, correlated inner evaluation; see
    # _algebra_nodes and _copy_filters_down for the scope rules.
    "Builtin_EXISTS",
    "Builtin_NOTEXISTS",
    "SelectQuery",
    "AskQuery",
    "ConstructQuery",
    "Project",
    "OrderBy",
    "OrderCondition",
    "Filter",
    "Extend",
    "BGP",
    "Join",
    "LeftJoin",
    "Union",
    "Minus",
    "ToMultiSet",
    "values",
    "TrueFilter",
    "Distinct",
    "ConditionalAndExpression",
    "ConditionalOrExpression",
    "RelationalExpression",
    "AdditiveExpression",
    "MultiplicativeExpression",
    "UnaryNot",
    "UnaryPlus",
    "UnaryMinus",
    "Builtin_BOUND",
    "Builtin_IF",
    "Builtin_COALESCE",
    "Builtin_STR",
    "Builtin_CONCAT",
    "Builtin_IRI",
    "Builtin_URI",
    "Builtin_SHA256",
    # Deterministic, row-local builtins (no volatile or custom functions).
    "Builtin_UCASE",
    "Builtin_LCASE",
    "Builtin_STRLEN",
    "Builtin_SUBSTR",
    "Builtin_CONTAINS",
    "Builtin_STRSTARTS",
    "Builtin_STRENDS",
    "Builtin_STRBEFORE",
    "Builtin_STRAFTER",
    "Builtin_REGEX",
    "Builtin_REPLACE",
    "Builtin_LANG",
    "Builtin_DATATYPE",
    "Builtin_isIRI",
    "Builtin_isURI",
    "Builtin_isLITERAL",
    "Builtin_isBLANK",
    "Builtin_isNUMERIC",
    "Builtin_sameTerm",
    "Builtin_ABS",
    "Builtin_ROUND",
    "Builtin_CEIL",
    "Builtin_FLOOR",
    "Builtin_YEAR",
    "Builtin_MONTH",
    "Builtin_DAY",
}


def _variables(triple):
    return {term for term in triple if isinstance(term, Variable)}


def _connected_order(triples, initially_bound=()):
    remaining = list(triples)
    frequency = Counter(term for triple in remaining for term in _variables(triple))
    bound = set(initially_bound)
    ordered = []
    while remaining:

        def rank(index):
            variables = _variables(remaining[index])
            return (
                len(variables - bound),
                -len(variables & bound),
                -sum(frequency[term] for term in variables),
                index,
            )

        triple = remaining.pop(min(range(len(remaining)), key=rank))
        ordered.append(triple)
        bound.update(_variables(triple))
    return ordered


def _algebra_nodes(value):
    """Yield evaluated algebra nodes.

    RDFLib stores the translated (NOT) EXISTS pattern as an instance attribute
    while the dict item keeps the untranslated parse tree, which is never
    evaluated. Walk the evaluated pattern so the whitelist sees what runs.
    """
    if isinstance(value, CompValue):
        yield value
        if value.name in EXISTS_NODES:
            yield from _algebra_nodes(value.__dict__.get("graph"))
            return
        for item in value.values():
            yield from _algebra_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _algebra_nodes(item)


def _expression_variables(value):
    if isinstance(value, Variable):
        return {value}
    if isinstance(value, CompValue):
        return set().union(
            *(_expression_variables(item) for key, item in value.items() if key != "_vars")
        )
    if isinstance(value, list | tuple):
        return set().union(*(_expression_variables(item) for item in value))
    return set()


def _guaranteed_bound(node):
    """Under-approximate bindings; never use RDFLib's possible-variable _vars."""
    if node.name == "BGP":
        return set().union(*(_variables(triple) for triple in node.triples))
    if node.name == "Join":
        return _guaranteed_bound(node.p1) | _guaranteed_bound(node.p2)
    if node.name == "Union":
        return _guaranteed_bound(node.p1) & _guaranteed_bound(node.p2)
    if node.name == "LeftJoin":
        return _guaranteed_bound(node.p1)
    if node.name == "Extend":
        # BIND may fail. Never assume its target is bound or unchanged, even
        # when an invalid/rebound target happens to be accepted by the parser.
        return _guaranteed_bound(node.p) - {node.var}
    if node.name == "Filter":
        return _guaranteed_bound(node.p)
    return set()


def _copy_filters_down(algebra, receipt):
    """Copy a deterministic filter only across bag-preserving proven boundaries.

    Filter selection distributes over UNION and joins if all its variables are
    already bound on the selected input. For LEFT JOIN only the mandatory left
    input qualifies. A BIND may be crossed only for inherited variables. Original
    filters and expressions stay intact, including STR literal/IRI semantics.
    No Project, subquery, MINUS, ordering, slice or aggregate boundary is crossed.
    The whole-plan whitelist has already excluded volatile/custom expressions.
    """
    filters = [node for node in _algebra_nodes(algebra) if node.name == "Filter"]
    for original in filters:
        receipt["filter_candidate_count"] += 1
        if any(node.name in EXISTS_NODES for node in _algebra_nodes(original.expr)):
            # A correlated pattern reads every in-scope binding of its row.
            receipt["filter_skip_reasons"]["CORRELATED_EXISTS_SCOPE"] += 1
            continue
        refs = _expression_variables(original.expr)
        if not refs or not refs <= _guaranteed_bound(original.p):
            receipt["filter_skip_reasons"]["VARIABLES_NOT_GUARANTEED_BOUND"] += 1
            continue

        def copy_into(node, depth=0, *, refs=refs, expression=original.expr):
            children = []
            if node.name in {"Filter", "Extend"}:
                children = ["p"]
            elif node.name in {"Join", "Union"}:
                children = ["p1", "p2"]
            elif node.name == "LeftJoin":
                children = ["p1"]
            eligible = [key for key in children if refs <= _guaranteed_bound(node[key])]
            if node.name == "Union" and len(eligible) != 2:
                eligible = []
            if eligible:
                for key in eligible:
                    node[key] = copy_into(node[key], depth + 1)
                return node
            if not depth:
                return node
            receipt["copied_filter_count"] += 1
            return CompValue(
                "Filter", expr=expression, p=node,
                _vars=set(node._vars or ()) | refs,
                no_isolated_scope=False,
            )

        before = receipt["copied_filter_count"]
        original["p"] = copy_into(original.p)
        if receipt["copied_filter_count"] == before:
            receipt["filter_skip_reasons"]["NO_SAFE_EARLIER_BOUNDARY"] += 1


def prepare_local_query(sparql: str, *, init_ns=None, filter_pushdown=True):
    """Return an invocation-owned plan and its receipt; never mutate shared plans.

    Only deterministic supported operators are transformed. Slice, unsupported
    aggregates, volatile/custom functions, SERVICE, named graph operations, and
    query BNodes/paths deliberately retain the original evaluator. This preserves
    all duplicate bindings; explicit ORDER BY remains at its original position.
    Without ORDER BY, SPARQL specifies a multiset rather than a row sequence.
    GROUP_CONCAT preserves the complete input bag, but its internal string
    ordering is unspecified by SPARQL, even with an outer ORDER BY.
    ``filter_pushdown=False`` retains the previous BGP-only plan for measured
    differentials and controlled rollback; it does not disable the older planner.
    """
    started = perf_counter()
    library = version("rdflib")
    receipt: dict[str, Any] = {
        "policy_version": POLICY,
        "mode": "ORIGINAL_RDFLIB",
        "reason": "NO_MULTI_TRIPLE_BGP",
        "query_sha256": "sha256:" + sha256(sparql.encode()).hexdigest(),
        "rdflib_version": library,
        "transformed_bgp_count": 0,
        "filter_policy_version": FILTER_POLICY,
        "filter_pushdown_enabled": filter_pushdown,
        "filter_candidate_count": 0,
        "copied_filter_count": 0,
        "filter_skip_reasons": Counter(),
        "aggregate_policy": ensure_spec_aggregates(),
    }
    if library != VERIFIED_RDFLIB:
        receipt["reason"] = "UNVERIFIED_RDFLIB_VERSION"
        return sparql, receipt
    if CUSTOM_EVALS:
        receipt["reason"] = "CUSTOM_EVALUATOR_REGISTERED"
        return sparql, receipt
    prepared = prepareQuery(sparql, initNs=init_ns)
    nodes = list(_algebra_nodes(prepared.algebra))
    unsupported = sorted({node.name for node in nodes} - SUPPORTED_NODES)
    if unsupported:
        receipt["reason"] = "UNSUPPORTED_ALGEBRA: " + ", ".join(unsupported)
        return sparql, receipt
    # SAMPLE synthesized for a grouped key is constant within its group.
    # Explicit SAMPLE of a non-key value depends on input order; keep it native.
    for node in nodes:
        if node.name == "AggregateJoin":
            group = node.p
            expressions = group.expr or () if group.name == "Group" else ()
            if any(not isinstance(expression, Variable) for expression in expressions):
                receipt["reason"] = "UNSUPPORTED_GROUP_EXPRESSION"
                return sparql, receipt
            keys = set(expressions)
            if any(aggregate.name == "Aggregate_Sample" and aggregate.vars not in keys
                   for aggregate in node.A):
                receipt["reason"] = "ORDER_DEPENDENT_SAMPLE"
                return sparql, receipt
    receipt["group_concat_order"] = (
        "SPARQL_UNSPECIFIED" if any(node.name == "Aggregate_GroupConcat" for node in nodes) else None
    )
    bgps = [node for node in nodes if node.name == "BGP"]
    if any(
        isinstance(term, BNode) or not isinstance(term, Variable | URIRef | Literal)
        for node in bgps
        for triple in node.triples
        for term in triple
    ):
        receipt["reason"] = "QUERY_BNODE_OR_PROPERTY_PATH"
        return sparql, receipt

    receipt["lazy_join_count"] = 0

    def substitutable(node):
        return all(item.name in SUBSTITUTABLE_NODES for item in _algebra_nodes(node))

    def rewrite(value, bound=frozenset()):
        """bound is only non-empty inside a substitutable lazy Join operand."""
        if isinstance(value, CompValue):
            if value.name in EXISTS_NODES:
                # Correlated inner patterns keep RDFLib's binding-aware plan.
                return value
            if value.name == "Join":
                # A lazy Join evaluates p2 once per p1 row with that row bound.
                # RDFLib ordered p2's BGP without knowing those bindings, so a
                # large p2 restarted from its most selective *unbound* triple
                # for every outer row (UNION x BGP became |p1| x |p2|). Order
                # p2 from the variables p1 guarantees. A non-lazy Join
                # materialises both sides; bag join is commutative, so a
                # substitutable operand may be moved to p2 and evaluated lazily.
                if not substitutable(value.p2) and substitutable(value.p1):
                    value["p1"], value["p2"] = value.p2, value.p1
                if substitutable(value.p2):
                    outer = bound | _guaranteed_bound(value.p1)
                    value["p1"] = rewrite(value.p1, bound)
                    value["p2"] = rewrite(value.p2, frozenset(outer))
                    if outer or not value.get("lazy"):
                        receipt["lazy_join_count"] += 1
                    value["lazy"] = True
                    return value
            if bound and value.name == "Union":
                value["p1"] = rewrite(value.p1, bound)
                value["p2"] = rewrite(value.p2, bound)
                return value
            if value.name == "BGP":
                if len(value.triples) < 2:
                    return value
                chain = None
                for triple in _connected_order(value.triples, bound):
                    one = CompValue("BGP", triples=[triple], _vars=_variables(triple))
                    chain = (
                        one
                        if chain is None
                        else CompValue(
                            "Join",
                            p1=chain,
                            p2=one,
                            lazy=True,
                            _vars=chain._vars | one._vars,
                        )
                    )
                receipt["transformed_bgp_count"] += 1
                return chain
            # Preserve original expression evaluator objects and outer operators.
            for key, item in list(value.items()):
                value[key] = rewrite(item)
        elif isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    prepared.algebra = rewrite(prepared.algebra)
    if filter_pushdown:
        _copy_filters_down(prepared.algebra, receipt)
    else:
        receipt["filter_skip_reasons"]["DISABLED_FOR_COMPARISON"] += 1
    receipt["planning_elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    if (receipt["transformed_bgp_count"] or receipt["copied_filter_count"]
            or receipt["lazy_join_count"]):
        receipt.update(mode="CONNECTED_LAZY_BGP", reason="INVOCATION_LOCAL_EQUIVALENT_JOIN_PLAN")
        return prepared, receipt
    return sparql, receipt


def execute_local_query(graph, sparql: str, *, filter_pushdown=True):
    """Evaluate the complete local graph using the original frozen query contract."""
    query, receipt = prepare_local_query(
        sparql, init_ns=dict(graph.namespaces()), filter_pushdown=filter_pushdown
    )
    if isinstance(graph, Dataset | ConjunctiveGraph):
        query = sparql
        receipt.update(
            mode="ORIGINAL_RDFLIB", reason="NAMED_GRAPH_DATASET", transformed_bgp_count=0,
            copied_filter_count=0,
        )
    if receipt["mode"] == "CONNECTED_LAZY_BGP":
        return graph.query(query, use_store_provided=False), receipt
    return graph.query(sparql), receipt


AGGREGATE_POLICY = "sparql11-distinct-aggregate-unbound-skip-v1"
_aggregates_guarded = False


def ensure_spec_aggregates() -> str | None:
    """Make DISTINCT aggregates skip unbound/error inputs as SPARQL 1.1 requires.

    SPARQL 1.1 section 18.5 removes expression errors (for example an unbound
    OPTIONAL variable) from every aggregate's input. RDFLib 7.1.4 does this for
    non-DISTINCT aggregates, but its DISTINCT membership test evaluates outside
    the error guard, so GROUP_CONCAT/SUM/AVG(DISTINCT ?v) over an empty OPTIONAL
    group raise NotBoundError and abort the query. Only that test is guarded;
    accumulators stay native. Idempotent; returns None on unverified RDFLib.
    """
    global _aggregates_guarded
    if _aggregates_guarded:
        return AGGREGATE_POLICY
    if version("rdflib") != VERIFIED_RDFLIB:
        return None
    from rdflib.plugins.sparql import aggregates
    from rdflib.plugins.sparql.evalutils import _eval
    from rdflib.plugins.sparql.sparql import NotBoundError, SPARQLTypeError

    def use_row(self, row):  # noqa: ANN001 - RDFLib accumulator callback
        try:
            return _eval(self.expr, row) not in self.seen
        except (NotBoundError, SPARQLTypeError):
            return False

    aggregates.Accumulator.use_row = use_row
    _aggregates_guarded = True
    return AGGREGATE_POLICY


ensure_spec_aggregates()
