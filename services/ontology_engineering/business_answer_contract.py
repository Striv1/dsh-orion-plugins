"""Freeze structural CQ dimensions from declared business projections.

Aliases, entity/field mappings and joins are authored choices. Only their
technical answer metadata is derived here: no query execution, expected-value
generation, textual intent guessing or replacement of explicit CQ dimensions.
"""
from __future__ import annotations

from copy import deepcopy


def compile_business_answer_bindings(plan, mappings, namespace, *, bindings, inference=False):
    """Return owned bindings with dimensions for each declared business output.

    Each path is one property actually used by the query, as required by the
    shared CQ contract (not a synthesized property chain). For an entity with
    several incident joins, the first declared join supplies the path and all
    incident mapping IDs remain evidence. They are all conjunctive query edges.

    Explicit dimensions, including an explicit empty list, remain authoritative
    and must pass the existing S3/S4 gates. Required fields and independent
    assertions are never extended to make generated dimensions pass those gates.
    """
    result = deepcopy(bindings)
    if not any('required_business_dimensions' not in binding for binding in result.values()):
        return result
    by_id = {item['id']: item for item in mappings}
    entities = {item['as']: item for item in plan['entities']}
    fields = {item['as']: item for item in plan.get('fields', [])}
    outputs = [plan['rule']['subject']] if inference else plan['select']
    dimensions = []
    for alias in outputs:
        if inference:
            mapping = by_id[plan['rule']['conclusion_mapping_ref']]
            supporting = [mapping]
            path = ''  # The conclusion CQ contains a type triple, no source joins.
        elif alias in fields:
            field = fields[alias]
            mapping = by_id[field['mapping_ref']]
            supporting = [mapping, by_id[entities[field['entity']]['mapping_ref']]]
            path = namespace + mapping['target']
        else:
            mapping = by_id[entities[alias]['mapping_ref']]
            joins = [by_id[link['mapping_ref']] for link in plan.get('relations', [])
                     if alias in (link['from'], link['to'])]
            supporting = [mapping, *joins]
            path = namespace + joins[0]['target'] if joins else ''
        dimensions.append({
            'dimension': alias, 'binding': alias, 'applicability': 'REQUIRED',
            'label_zh': mapping.get('target_label_zh') or mapping.get('label_zh')
                        or f"业务输出（{mapping['target']}）",
            'ontology_term': namespace + mapping['target'], 'path': path,
            'evidence_refs': list(dict.fromkeys(item['id'] for item in supporting)),
        })
    for binding in result.values():
        if 'required_business_dimensions' not in binding:
            binding['required_business_dimensions'] = deepcopy(dimensions)
    return result
