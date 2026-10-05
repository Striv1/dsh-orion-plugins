"""Conservative Ontop execution lowering; frozen query assets remain unchanged."""

import re
from hashlib import sha256

from pyparsing import ParseBaseException
from rdflib import BNode, Variable
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

POLICY = "bound-exists-distinct-semijoin-v1"
# FILTER NOT EXISTS with fully bound correlation keys lowers to an anti-join:
# MINUS over a DISTINCT key projection, appended at the end of the WHERE group.
NOT_EXISTS_POLICY = "bound-not-exists-minus-antijoin-v1"
_EXISTS = {"Builtin_EXISTS", "Builtin_NOTEXISTS"}
# Quoted strings, IRIs and comments are opaque when locating a parsed FILTER.
_TOKENS = re.compile(
    r"\"\"\"(?:\\.|(?!\"\"\")[\s\S])*\"\"\"|'''(?:\\.|(?!''')[\s\S])*'''"
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r'|<[^<>"{}|^`\\\s]*>|\#[^\r\n]*|[A-Za-z_][A-Za-z_0-9-]*|[^\s]',
)
_SCALAR = {
    "ConditionalAndExpression", "ConditionalOrExpression", "RelationalExpression",
    "Builtin_STR", "Builtin_CONCAT", "Builtin_BOUND", "UnaryNot", "TrueFilter",
}
_OUTER = {"SelectQuery", "Project", "OrderBy", "OrderCondition", "Filter", "BGP", "Join", "LeftJoin", "Union", "Extend", "Distinct", "Slice"}
# Grouping consumes the WHERE multiset, which both lowerings preserve exactly.
_OUTER |= {"AggregateJoin", "Group", "Aggregate_Count", "Aggregate_Sum", "Aggregate_Min",
           "Aggregate_Max", "Aggregate_Avg", "Aggregate_Sample"}


def _walk(value):
    if isinstance(value, CompValue):
        yield value
        for key, item in value.items():
            if key != "_vars":
                yield from _walk(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk(item)


def _variables(value):
    if isinstance(value, Variable):
        return {value}
    if isinstance(value, CompValue):
        return set().union(*( _variables(item) for key, item in value.items()
                             if key != "_vars" and value.name not in _EXISTS))
    if isinstance(value, list | tuple):
        return set().union(*(_variables(item) for item in value))
    return set()


def _bound(node):
    if node.name == "BGP":
        return _variables(node)
    if node.name == "Join":
        return _bound(node.p1) | _bound(node.p2)
    if node.name == "LeftJoin":
        return _bound(node.p1)
    if node.name == "Union":
        return _bound(node.p1) & _bound(node.p2)
    if node.name in {"Filter", "Extend"}:
        return _bound(node.p)
    return set()


def compile_ontop_select(query):
    """Lower one standalone (NOT) EXISTS with fully bound correlation keys.

    EXISTS becomes a DISTINCT semi-join; NOT EXISTS becomes MINUS over the same
    DISTINCT key projection. Both are equivalent only because every shared
    variable is certainly bound by the outer scope and the inner graph uses no
    outer-only variable. DISTINCT preserves the original outer bag even with multiple inner matches.
    Optional/unbound correlation, nested EXISTS, non-BGP inner graphs, arbitrary
    functions and nested query scopes deliberately retain the original query.
    """
    receipt = {"strategy": "ORIGINAL", "original_query_sha256": "sha256:" + sha256(query.encode()).hexdigest()}
    if "exists" not in query.lower():
        return query, receipt
    try:
        algebra = prepareQuery(query).algebra
        if algebra.name != "SelectQuery" or algebra.datasetClause:
            return query, receipt
        # Inspect EXISTS via its translated graph attribute; RDFLib also retains
        # its raw parsed graph in the dictionary, which is not executable algebra.
        nodes = list(_walk(algebra))
        exists = [node for node in nodes if node.name in _EXISTS]
        if len(exists) != 1:
            return query, receipt
        existence = exists[0]
        negated = existence.name == "Builtin_NOTEXISTS"
        inner = existence.graph
        inner_nodes = list(_walk(inner))
        if any(node.name not in {"BGP", "Filter", "Join"} | _SCALAR for node in inner_nodes):
            return query, receipt
        bgps = [node for node in inner_nodes if node.name == "BGP" and node.triples]
        if len(bgps) != 1 or any(isinstance(term, BNode) or not hasattr(term, "n3") for triple in bgps[0].triples for term in triple):
            return query, receipt
        inner_vars = _variables(inner)
        if not inner_vars <= _variables(bgps[0]):
            return query, receipt
        # Omit the raw EXISTS subtree while verifying the complete outer scope.
        def outer_walk(value):
            if isinstance(value, CompValue):
                yield value
                if value.name not in _EXISTS:
                    for key, item in value.items():
                        if key != "_vars":
                            yield from outer_walk(item)
            elif isinstance(value, list | tuple):
                for item in value:
                    yield from outer_walk(item)
        outer_nodes = list(outer_walk(algebra))
        if any(node.name not in _OUTER | _SCALAR | _EXISTS for node in outer_nodes):
            return query, receipt
        filters = [node for node in outer_nodes if node.name == "Filter" and (
            node.expr is existence or (node.expr.name == "ConditionalAndExpression" and existence in [node.expr.expr, *node.expr.other]))]
        if len(filters) != 1:
            return query, receipt
        keys = inner_vars & _variables(algebra)
        if not keys or not keys <= _bound(filters[0].p):
            return query, receipt
        tokens = [match for match in _TOKENS.finditer(query) if not match.group().startswith("#")]
        keyword = ["NOT", "EXISTS"] if negated else ["EXISTS"]
        width = len(keyword)
        depth, spans, group_end = 0, [], []
        for index, token in enumerate(tokens):
            text = token.group()
            if (depth == 1 and text.upper() == "FILTER" and index + width + 1 < len(tokens)
                    and [item.group().upper() for item in tokens[index + 1:index + 1 + width]] == keyword
                    and tokens[index + 1 + width].group() == "{"):
                balance = 1
                for end in range(index + 2 + width, len(tokens)):
                    balance += (tokens[end].group() == "{") - (tokens[end].group() == "}")
                    if balance == 0:
                        spans.append((token.start(), tokens[index + 1 + width].end(), tokens[end].start(), tokens[end].end()))
                        break
            depth += (text == "{") - (text == "}")
            if depth == 0 and text == "}":
                group_end.append(token.start())
        if len(spans) != 1 or not group_end:
            return query, receipt
        start, body, end_body, end = spans[0]
        projection = " ".join(key.n3() for key in sorted(keys, key=str))
        lowered = "{ SELECT DISTINCT " + projection + " WHERE {" + query[body:end_body] + "} }"
        if negated:
            # MINUS scopes over preceding group elements, so it moves to the end
            # of the WHERE group where every outer pattern is already joined.
            close = group_end[0]
            if close < end:
                return query, receipt
            compiled = query[:start] + query[end:close] + " MINUS " + lowered + " " + query[close:]
        else:
            compiled = query[:start] + lowered + query[end:]
        prepareQuery(compiled)
        return compiled, {**receipt, "strategy": NOT_EXISTS_POLICY if negated else POLICY, "executed_query_sha256": "sha256:" + sha256(compiled.encode()).hexdigest(), "correlation_keys": [str(key) for key in sorted(keys, key=str)]}
    except (ValueError, TypeError, KeyError, AttributeError, ParseBaseException):
        return query, receipt
