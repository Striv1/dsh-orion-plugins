import pytest

from services.realtime_qa.reasoning import document_evidence_facts, semantic_facts


def test_document_facts_reorder_constants_and_real_parameters_restore_source_symbols():
    symbols = {}
    facts = document_evidence_facts(
        [{'fact': 'Participation(候选人/A, 项目 B)'}, {'fact': 'Other(ignored)'}],
        [{'predicate': 'Participation', 'arguments': [
            {'field': 1}, {'field': 0}, {'constant': '确认/通过'}, {'parameter': 'window'}]}],
        symbols, parameters={'window': '2024 / 2026'},
    )
    assert facts == ['Participation(项目_B, 候选人_A, 确认_通过, 2024_2026)']
    restored = semantic_facts(facts, {'Participation': 'urn:audit:Participation'}, symbols)
    assert restored[0]['arguments'] == ['项目 B', '候选人/A', '确认/通过', '2024 / 2026']


@pytest.mark.parametrize('parameters', [None, {}, {'window': None}, {'window': ''}])
def test_missing_document_parameter_fails_closed(parameters):
    with pytest.raises(ValueError, match='required parameter|empty value'):
        document_evidence_facts([{'fact': 'Item(a)'}],
            [{'predicate': 'Item', 'arguments': [{'parameter': 'window'}]}],
            parameters=parameters)


@pytest.mark.parametrize('field', [-1, 1, True, '0'])
def test_selected_document_fact_invalid_field_cannot_be_dropped(field):
    with pytest.raises(ValueError, match='zero-based integer'):
        document_evidence_facts([{'fact': 'Item(a)'}],
            [{'predicate': 'Item', 'arguments': [{'field': field}]}])


@pytest.mark.parametrize('expression', ['bad', 'Item()', 'Item(a,,b)', 'Item(?x)', 'Item(a,)'])
def test_malformed_document_fact_cannot_claim_complete_input(expression):
    with pytest.raises(ValueError, match='format|unbound or empty'):
        document_evidence_facts([{'fact': expression}],
            [{'predicate': 'Item', 'arguments': [{'field': 0}]}])


def test_unselected_predicate_is_deliberately_excluded_and_old_positional_call_works():
    symbols = {}
    assert document_evidence_facts(
        [{'fact': 'Other(a)'}, {'fact': 'Item(b)'}, {'fact': 'Item(b)'}],
        [{'predicate': 'Item', 'arguments': [{'field': 0}]}], symbols,
    ) == ['Item(b)']
    assert symbols == {'b': 'b'}


def test_document_false_and_zero_parameters_are_values():
    assert document_evidence_facts([{'fact': 'Item(a)'}],
        [{'predicate': 'Item', 'arguments': [{'parameter': 'flag'}, {'parameter': 'count'}]}],
        parameters={'flag': False, 'count': 0}) == ['Item(false, 0)']
