"""Bounded, on-demand views of the authoritative stage schema; no new contracts."""
from __future__ import annotations

from copy import deepcopy

from services.ontology_contracts.errors import WorkflowError


def project_contract(contract: dict, section: str | None) -> dict:
    if section is None:
        return contract
    if not isinstance(section, str):
        raise WorkflowError('合同 section 必须是字符串。')
    properties = contract['payload_schema'].get('properties', {})
    sections = {key: value for key, value in properties.items() if key != 'realtime_runtime'}
    mapping = properties.get('mapping_draft', {}).get('properties', {})
    sections.update({f'mapping_draft.{key}': value for key, value in mapping.items()})
    runtime = properties.get('realtime_runtime', {}).get('properties', {})
    sections.update({f'realtime_runtime.{key}': value for key, value in runtime.items()})
    if section != 'overview' and section not in sections:
        raise WorkflowError('请求的合同分段不存在；先读取 section=overview 获取当前模式的可用分段。')
    base = {key: deepcopy(contract[key]) for key in (
        'project_id', 'revision', 'stage', 'intake_mode', 'status', 'draft_checkpoint',
        'checkpoint_policy', 'source_refs', 'assembly_plan', 'cq_requirements_read',
    ) if key in contract}
    base['section'] = section
    base['full_contract_read'] = {'tool': 'get_stage_input_contract', 'args': {
        'project_id': contract['project_id'], 'stage': contract['stage'],
    }}
    if section == 'overview':
        base['required_payload_fields'] = contract['payload_schema'].get('required', [])
        base['sections'] = [{
            'section': key, 'type': value.get('type'),
            'read': {'tool': 'get_stage_input_contract', 'args': {
                'project_id': contract['project_id'], 'stage': contract['stage'], 'section': key,
            }},
        } for key, value in sections.items()]
        base['instruction'] = ('先恢复当前草稿；仅按当前工作读取所需合同分段，再局部保存。'
                               '分段不代表阶段通过；完整预检和业务审批仍使用原有规则。')
    else:
        base['schema'] = deepcopy(sections[section])
        base['payload_pointer'] = '/' + '/'.join(section.split('.'))
        # The input shape alone does not describe packaging/consumption semantics.
        if section.startswith('realtime_runtime.'):
            base['execution_semantics'] = deepcopy(contract.get('execution_semantics', {}))
        base['instruction'] = '此 schema 对应 payload_pointer 下的值；保留其他草稿字段，最终完整预检后才能提交。'
    return base
