from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

import httpx

from services.ontology_contracts.rule_syntax import PARSED_FACT, require_safe_negation
from services.ontology_contracts.rule_syntax import parse_atom as _parse_atom
from services.realtime_qa.row_fact_conditions import row_matches_condition

SAFE_TOKEN = re.compile(
    r"[^\w\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff.:-]+",
    re.UNICODE,
)
RULE_SPLIT = re.compile(r"^IF\s+(?P<premises>.+?)\s+THEN\s+(?P<conclusion>.+)$", re.I)
RULE_AND = re.compile(r"\s+AND\s+", re.I)

RULE_CONCLUSION_BOUNDARY_ZH = (
    "规则结论仅限执行回执实际推出的事实。条件为false或未满足，只表示该条件不成立，"
    "不等于推出目标结论的否定；条件缺失仍为未知。没有推出某结论，不等于推出相反结论。"
    "明确的否定业务结论必须有已发布规则及其实际推导回执支持；"
    "闭世界输入仅适用于声明的谓词和已验证来源范围，不能推广到所有业务结论。"
)


def rule_conclusion_contract(closed_world_predicates: list[str]) -> dict[str, Any]:
    """Describe entailment, without turning failed premises into business negation."""
    return {
        "scope": "RETURNED_RULE_CONCLUSIONS_ONLY",
        "failed_premise_entails_opposite": False,
        "missing_premise": "UNKNOWN",
        "closed_world_input_predicates": sorted(set(closed_world_predicates)),
        "boundary_zh": RULE_CONCLUSION_BOUNDARY_ZH,
    }


class SemanticaReasoningClient:
    """Read-only adapter for Semantica's deterministic forward reasoner."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        api_key: str | None = None,
        health_timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("SEMANTICA_API_KEY", "")
        self.timeout = timeout
        self.health_timeout = timeout if health_timeout is None else health_timeout
        self.transport = transport

    def health(self) -> dict[str, Any]:
        with self._client() as client:
            response = client.get("/api/health", timeout=self.health_timeout)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Semantica health response must be an object")
        return payload

    def run_forward(
        self,
        *,
        facts: list[str],
        rules: list[str],
        closed_world_predicates: list[str] | None = None,
    ) -> dict[str, Any]:
        negated_rules = [rule for rule in rules if re.search(r"\bNOT\s+", rule, re.I)]
        if not negated_rules:
            return self._run_remote(facts=facts, rules=rules)

        allowed = {
            str(predicate).strip()
            for predicate in (closed_world_predicates or [])
            if str(predicate).strip()
        }
        if not allowed:
            raise ValueError(
                "closed-world negation requires explicit closed_world_predicates"
            )

        positive_rules = [rule for rule in rules if rule not in negated_rules]
        preliminary = (
            self._run_remote(facts=facts, rules=positive_rules)
            if positive_rules
            else {
                "inferred_facts": [],
                "rules_fired": 0,
                "trace": [],
                "warnings": [],
                "mutated": False,
                "added_edges": 0,
                "updated_properties": 0,
            }
        )
        known_facts = list(
            dict.fromkeys([*facts, *map(str, preliminary["inferred_facts"])])
        )
        rewritten_rules, witnesses, witness_labels, original_rules = (
            _prepare_closed_world_rules(
                facts=known_facts,
                positive_rules=positive_rules,
                negated_rules=negated_rules,
                allowed_predicates=allowed,
            )
        )
        final = self._run_remote(
            facts=[*known_facts, *witnesses],
            rules=rewritten_rules,
        )
        trace = [*list(preliminary.get("trace") or []), *list(final["trace"])]
        for item in trace:
            if not isinstance(item, dict):
                continue
            rewritten = str(item.get("rule_text") or "")
            item["rule_text"] = original_rules.get(rewritten, rewritten)
            premises = [str(value) for value in item.get("premises") or []]
            negations = [witness_labels[value] for value in premises if value in witness_labels]
            item["premises"] = premises
            if negations:
                item["closed_world_negations"] = negations

        result = dict(final)
        result["inferred_facts"] = list(
            dict.fromkeys(
                [
                    *map(str, preliminary.get("inferred_facts") or []),
                    *map(str, final["inferred_facts"]),
                ]
            )
        )
        result["trace"] = trace
        result["rules_fired"] = int(preliminary.get("rules_fired") or 0) + int(
            final.get("rules_fired") or 0
        )
        result["warnings"] = list(
            dict.fromkeys(
                [
                    *map(str, preliminary.get("warnings") or []),
                    *map(str, final.get("warnings") or []),
                    "ORION applied explicit closed-world anti-join witnesses before Semantica forward reasoning.",
                ]
            )
        )
        result["closed_world"] = {
            "operator": "ORION_SAFE_ANTI_JOIN_V1",
            "predicates": sorted(allowed),
            "witness_count": len(witnesses),
            "materialized_facts": witnesses,
        }
        return result

    def _run_remote(self, *, facts: list[str], rules: list[str]) -> dict[str, Any]:
        normalized_facts = [_canonical_atom(fact) for fact in facts]
        normalized_rules = [_canonical_rule(rule) for rule in rules]
        aliases: dict[str, list[str]] = {}
        for normalized, original in zip(normalized_facts, facts, strict=True):
            aliases.setdefault(normalized, []).append(original)
        with self._client() as client:
            response = client.post(
                "/api/reason",
                json={
                    "facts": normalized_facts,
                    "rules": normalized_rules,
                    "mode": "forward",
                    "apply_to_graph": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
            engine_build = response.headers.get("X-Semantica-Engine-SHA256")
        if not isinstance(payload, dict):
            raise ValueError("Semantica reasoning response must be an object")
        if engine_build is not None and not re.fullmatch(r"sha256:[a-f0-9]{64}", engine_build):
            raise ValueError("Semantica engine build identity header is invalid")
        payload["engine_build_sha256"] = engine_build or "UNKNOWN"
        if payload.get("mutated") is True or payload.get("added_edges") or payload.get(
            "updated_properties"
        ):
            raise RuntimeError("Semantica read-only reasoning unexpectedly mutated the graph")
        inferred = payload.get("inferred_facts")
        trace = payload.get("trace")
        if not isinstance(inferred, list) or not isinstance(trace, list):
            raise ValueError("Semantica reasoning response is incomplete")
        # Execution spelling is canonical; audit spelling remains the submitted
        # rule. Ground premises retain every original alias so controlled
        # negative cases remove the actual input facts, including whitespace variants.
        payload["inferred_facts"] = [_canonical_atom(fact) for fact in inferred]
        restored = []
        for item in trace:
            if not isinstance(item, dict):
                raise ValueError("Semantica trace item must be an object")
            normalized_rule = _canonical_rule(str(item.get("rule_text") or ""))
            matches = [index for index, rule in enumerate(normalized_rules) if rule == normalized_rule]
            rule_id = re.fullmatch(r"rule_([1-9][0-9]*)", str(item.get("rule_id") or ""))
            indexed = int(rule_id[1]) - 1 if rule_id else -1
            if indexed in matches:
                selected = indexed
            elif len(matches) == 1:
                selected = matches[0]
            else:
                raise ValueError("Semantica trace rule cannot be bound to submitted rules")
            premises = item.get("premises")
            if not isinstance(premises, list):
                raise ValueError("Semantica trace premises must be a list")
            original_premises = []
            for premise in premises:
                canonical = _canonical_atom(premise)
                original_premises.extend(aliases.get(canonical, [canonical]))
            restored.append({**item, "rule_text": rules[selected],
                             "premises": list(dict.fromkeys(original_premises)),
                             "conclusion": _canonical_atom(item.get("conclusion"))})
        payload["trace"] = restored
        return payload

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            headers={"X-API-Key": self.api_key} if self.api_key else None,
            trust_env=False,
        )


def _canonical_atom(expression: str) -> str:
    predicate, arguments = _parse_atom(expression)
    return f"{predicate}({', '.join(arguments)})"


def _canonical_rule(expression: str) -> str:
    # The upstream engine splits rule keywords with regexes. Explicitly reject
    # quoted keyword/variable ambiguities instead of silently changing literals.
    if not isinstance(expression, str):
        raise ValueError("reasoning rule must be a string")
    match = RULE_SPLIT.fullmatch(expression.strip())
    if match is None:
        raise ValueError("reasoning rule must use IF ... THEN ...")
    premises = RULE_AND.split(match["premises"])
    atoms = [*premises, match["conclusion"]]
    for atom in atoms:
        _, arguments = _parse_atom(atom)
        for argument in arguments:
            if argument[0] in "\"'" and (re.search(r"\?(\w+)", argument)
                    or re.search(r"\b(?:AND|THEN)\b", argument, re.I)):
                raise ValueError("quoted rule literal is ambiguous for the Semantica engine")
    return "IF " + " AND ".join(_canonical_atom(atom) for atom in premises) + " THEN " + _canonical_atom(match["conclusion"])


def match_ground_atom(
    pattern: tuple[str, list[str]],
    fact: tuple[str, list[str]],
    bindings: dict[str, str],
) -> dict[str, str] | None:
    if pattern[0] != fact[0] or len(pattern[1]) != len(fact[1]):
        return None
    matched = dict(bindings)
    for expected, actual in zip(pattern[1], fact[1], strict=True):
        if expected.startswith("?"):
            previous = matched.get(expected)
            if previous is not None and previous != actual:
                return None
            matched[expected] = actual
        elif expected != actual:
            return None
    return matched


def _instantiate_atom(atom: tuple[str, list[str]], bindings: dict[str, str]) -> str:
    return f"{atom[0]}({', '.join(bindings.get(value, value) for value in atom[1])})"


def _prepare_closed_world_rules(
    *,
    facts: list[str],
    positive_rules: list[str],
    negated_rules: list[str],
    allowed_predicates: set[str],
) -> tuple[list[str], list[str], dict[str, str], dict[str, str]]:
    parsed_facts = [
        parsed
        for value in facts
        if (parsed := _parse_atom(value)) is not None
    ]
    rewritten_rules = list(positive_rules)
    witnesses: list[str] = []
    witness_labels: dict[str, str] = {}
    original_rules: dict[str, str] = {}

    for rule in negated_rules:
        matched_rule = RULE_SPLIT.fullmatch(" ".join(rule.split()))
        if matched_rule is None:
            raise ValueError(f"invalid closed-world rule: {rule}")
        premise_texts = RULE_AND.split(matched_rule.group("premises"))
        positive_atoms: list[tuple[str, list[str]]] = []
        negative_atoms: list[tuple[str, list[str]]] = []
        negative_positions: dict[int, tuple[str, list[str]]] = {}
        for position, text_value in enumerate(premise_texts):
            stripped = text_value.strip()
            is_negative = bool(re.match(r"^NOT\s+", stripped, re.I))
            atom = _parse_atom(re.sub(r"^NOT\s+", "", stripped, flags=re.I))
            if is_negative:
                if atom[0] not in allowed_predicates:
                    raise ValueError(
                        f"closed-world predicate is not authorized: {atom[0]}"
                    )
                negative_atoms.append(atom)
                negative_positions[position] = atom
            else:
                positive_atoms.append(atom)
        require_safe_negation(positive_atoms, negative_atoms)

        bindings: list[dict[str, str]] = [{}]
        for atom in positive_atoms:
            next_bindings: list[dict[str, str]] = []
            for current in bindings:
                for fact in parsed_facts:
                    resolved = match_ground_atom(atom, fact, current)
                    if resolved is not None:
                        next_bindings.append(resolved)
            bindings = next_bindings
            if not bindings:
                break

        replacement_atoms: dict[int, tuple[str, list[str]]] = {}
        for position, atom in negative_positions.items():
            local_predicate = re.sub(r"[^A-Za-z0-9_]", "_", atom[0])
            witness_predicate = f"Not{local_predicate}"
            if any(fact[0] == witness_predicate for fact in parsed_facts):
                raise ValueError(
                    f"closed-world witness predicate cannot be supplied as input: {witness_predicate}"
                )
            replacement_atoms[position] = (witness_predicate, atom[1])
        for current in bindings:
            for position, atom in negative_positions.items():
                if any(
                    match_ground_atom(atom, fact, current) is not None
                    for fact in parsed_facts
                ):
                    continue
                witness = _instantiate_atom(replacement_atoms[position], current)
                label = "NOT " + _instantiate_atom(atom, current)
                witnesses.append(witness)
                witness_labels[witness] = label

        rewritten_premises = [
            _instantiate_atom(replacement_atoms[position], {})
            if position in replacement_atoms
            else text_value.strip()
            for position, text_value in enumerate(premise_texts)
        ]
        rewritten = (
            "IF "
            + " AND ".join(rewritten_premises)
            + " THEN "
            + matched_rule.group("conclusion")
        )
        rewritten_rules.append(rewritten)
        original_rules[rewritten] = rule

    return (
        rewritten_rules,
        list(dict.fromkeys(witnesses)),
        witness_labels,
        original_rules,
    )


def facts_from_rows(
    rows: list[dict[str, Any]],
    parameters: dict[str, str | int | float | bool],
    fact_bindings: list[dict[str, Any]],
    symbol_table: dict[str, str] | None = None,
) -> list[str]:
    facts: list[str] = []
    for row in rows:
        for binding in fact_bindings:
            condition = binding.get("when")
            if condition is not None and not row_matches_condition(row, condition):
                continue
            arguments: list[str] = []
            missing = False
            for source in binding["arguments"]:
                if "field" in source:
                    value = row.get(str(source["field"]))
                elif "parameter" in source:
                    value = parameters.get(str(source["parameter"]))
                else:
                    value = source.get("constant")
                if value is None:
                    missing = True
                    break
                arguments.append(_safe_token(value, symbol_table))
            if not missing:
                facts.append(f"{binding['predicate']}({', '.join(arguments)})")
    return list(dict.fromkeys(facts))


def document_evidence_facts(
    materialized_facts: list[dict[str, Any]],
    fact_bindings: list[dict[str, Any]],
    symbol_table: dict[str, str] | None = None,
    parameters: dict[str, Any] | None = None,
) -> list[str]:
    """Bind selected document facts without silently losing source assertions.

    Fields address source arguments by zero-based integer position. Parameters
    resolve against the supplied runtime values, never their names. Unselected
    predicates are intentionally excluded; malformed facts or selected bindings
    that cannot be fulfilled fail closed.
    """
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict):
        raise ValueError("document evidence parameters must be an object")
    facts: list[str] = []
    for fact_index, materialized in enumerate(materialized_facts):
        expression = str(materialized.get("fact") or "").strip()
        match = PARSED_FACT.fullmatch(expression)
        if match is None:
            raise ValueError(f"document evidence fact[{fact_index}] has invalid format")
        raw_arguments = [value.strip() for value in match.group("arguments").split(",")]
        if any(not value or value.startswith("?") for value in raw_arguments):
            raise ValueError(f"document evidence fact[{fact_index}] has unbound or empty arguments")
        for binding in fact_bindings:
            predicate = str(binding.get("predicate") or "").strip()
            if predicate != match.group("predicate"):
                continue
            context = f"document evidence fact[{fact_index}] binding {predicate}"
            sources = binding.get("arguments")
            if not isinstance(sources, list) or not sources:
                raise ValueError(f"{context} requires argument sources")
            if binding.get("when") is not None:
                raise ValueError(f"{context} does not support row-field when conditions")
            arguments: list[str] = []
            for position, source in enumerate(sources):
                if not isinstance(source, dict):
                    raise ValueError(f"{context} argument[{position}] has invalid source")
                keys = [key for key in ("field", "parameter", "constant") if key in source]
                if len(keys) != 1:
                    raise ValueError(f"{context} argument[{position}] requires exactly one source")
                if keys[0] == "field":
                    index = source["field"]
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(raw_arguments):
                        raise ValueError(f"{context} argument[{position}] field must be a valid zero-based integer index")
                    value = raw_arguments[index]
                elif keys[0] == "parameter":
                    key = source["parameter"]
                    if not isinstance(key, str) or not key.strip() or key not in parameters:
                        raise ValueError(f"{context} argument[{position}] is missing its required parameter")
                    value = parameters[key]
                else:
                    value = source["constant"]
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ValueError(f"{context} argument[{position}] resolves to an empty value")
                if not isinstance(value, str | int | float | bool):
                    raise ValueError(f"{context} argument[{position}] must resolve to a scalar value")
                arguments.append(_safe_token(value, symbol_table))
            facts.append(f"{predicate}({', '.join(arguments)})")
    return list(dict.fromkeys(facts))


def semantic_facts(
    facts: list[str],
    ontology_terms: dict[str, str],
    symbol_table: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Expose every symbolic fact together with its release-bound ontology term."""

    resolved: list[dict[str, Any]] = []
    for fact in facts:
        match = PARSED_FACT.fullmatch(str(fact).strip())
        if match is None:
            raise ValueError(f"Semantica returned an invalid fact expression: {fact}")
        predicate = match.group("predicate")
        predicate_iri = ontology_terms.get(predicate)
        if not predicate_iri:
            raise ValueError(
                f"Semantica returned a predicate without ontology binding: {predicate}"
            )
        engine_arguments = [
            value.strip()
            for value in match.group("arguments").split(",")
            if value.strip()
        ]
        arguments = [
            (symbol_table or {}).get(value, value) for value in engine_arguments
        ]
        resolved.append(
            {
                "fact": str(fact),
                "predicate": predicate,
                "predicate_iri": predicate_iri,
                "arguments": arguments,
                "engine_arguments": engine_arguments,
            }
        )
    return resolved


def enrich_reasoning_trace(
    trace: list[Any],
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    by_expression = {rule["expression"]: rule for rule in rules}
    for item in trace:
        if not isinstance(item, dict):
            continue
        engine_rule_id = str(item.get("rule_id") or "")
        formal_rule = by_expression.get(str(item.get("rule_text") or ""))
        if formal_rule is None and engine_rule_id.startswith("rule_"):
            index = engine_rule_id.removeprefix("rule_")
            if index.isdigit() and 0 < int(index) <= len(rules):
                formal_rule = rules[int(index) - 1]
        enriched.append(
            {
                "rule_id": (
                    formal_rule["rule_id"] if formal_rule is not None else engine_rule_id
                ),
                "rule_description_zh": (
                    formal_rule["description_zh"] if formal_rule is not None else None
                ),
                "rule_expression": (
                    formal_rule["expression"]
                    if formal_rule is not None
                    else item.get("rule_text")
                ),
                "premises": list(item.get("premises") or []),
                "conclusion": item.get("conclusion"),
                "engine_confidence": item.get("confidence"),
                "declared_rule_confidence": (
                    formal_rule["confidence"] if formal_rule is not None else None
                ),
                "closed_world_negations": list(
                    item.get("closed_world_negations") or []
                ),
            }
        )
    return enriched


def reasoning_input_sha256(*, facts: list[str], rules: list[dict[str, Any]]) -> str:
    content = json.dumps(
        {"facts": facts, "rules": rules},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def build_decision_record(
    *,
    query_id: str,
    capability_name: str,
    project_id: str,
    release_version: str,
    release_fingerprint: str,
    evidence_query: str,
    parameters: dict[str, Any],
    input_facts_sha256: str,
    rule_sha256: str,
    semantic_result_facts: list[dict[str, Any]],
    trace: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Create the immutable evidence -> rule -> conclusion -> confidence envelope."""

    if not semantic_result_facts:
        return None
    confidences = [
        float(item["declared_rule_confidence"])
        for item in trace
        if item.get("declared_rule_confidence") is not None
    ]
    identity = {
        "query_id": query_id,
        "capability_name": capability_name,
        "release_fingerprint": release_fingerprint,
        "input_facts_sha256": input_facts_sha256,
        "rule_sha256": rule_sha256,
        "outcomes": [item["fact"] for item in semantic_result_facts],
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "decision_id": "DEC-" + digest[:24].upper(),
        "category": capability_name,
        "scenario": {
            "evidence_query": evidence_query,
            "parameters": parameters,
        },
        "evidence": {
            "input_facts_sha256": input_facts_sha256,
        },
        "rules": {
            "rule_sha256": rule_sha256,
            "fired_rule_ids": list(
                dict.fromkeys(
                    str(item.get("rule_id"))
                    for item in trace
                    if item.get("rule_id")
                )
            ),
        },
        "conclusions": semantic_result_facts,
        "confidence": min(confidences) if confidences else None,
        "version_anchor": {
            "project_id": project_id,
            "release_version": release_version,
            "release_fingerprint": release_fingerprint,
        },
        "persistence_status": "EVIDENCE_BUNDLE_RECORDED",
    }


def result_facts(inferred_facts: list[Any], predicates: list[str]) -> list[str]:
    prefixes = tuple(f"{predicate}(" for predicate in predicates)
    return [str(fact) for fact in inferred_facts if str(fact).startswith(prefixes)]


def _safe_token(value: Any, symbol_table: dict[str, str] | None = None) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    original = str(value).strip()
    text = SAFE_TOKEN.sub("_", original).strip("_")
    token = text[:200] or "empty"
    if symbol_table is not None:
        previous = symbol_table.get(token)
        if previous is not None and previous != original:
            raise ValueError(
                "two source values normalize to the same Semantica symbol: " + token
            )
        symbol_table[token] = original
    return token
