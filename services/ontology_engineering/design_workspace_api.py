"""Workflow adapters for server-owned design proposals and existing preflight."""
from __future__ import annotations

from copy import deepcopy

from services.realtime_qa.runtime_release import load_reviewed_runtime_submission

from . import design_workspace as workspace

STAGE_KINDS = {
    'S2': {'candidate', 'rule', 'assessment'},
    'S3': {'mapping'},
    'S4': {'class', 'object_property', 'data_property', 'constraint', 'axiom', 'query'},
}


def get_design_workspace(service, project_id: str) -> dict:
    project_dir = service._resolve_project(project_id)
    with service._project_operation_lock(project_dir):
        state = service._read_state(project_dir)
        return {'project_id': project_id, 'revision': state['revision'],
                'current_stage': state.get('current_stage'),
                'workspace': workspace.build_snapshot(project_dir),
                'writes_performed': False}


def _payload(project_dir, stage, before, after):
    docs = after['documents']
    if stage == 'S2':
        result = {'ontology_candidates': docs.get('02-semantic-recognition/ontology-candidates.yaml', {}).get('candidates', []),
                  'business_rule_candidates': docs.get('02-semantic-recognition/business-rule-candidates.json', [])}
        plan = docs.get('02-semantic-recognition/capability-plan.json', {})
        if plan.get('cq_semantic_assessments') is not None:
            result['cq_semantic_assessments'] = plan['cq_semantic_assessments']
        return deepcopy(result)
    if stage == 'S4':
        # Strip only the three persistence envelope fields already excluded by
        # joint_design._design_projection; preserve every business design field.
        design_path = after.get('selected_sources', {}).get('class', workspace.DESIGN_PATH)
        design = deepcopy(docs[design_path])
        for field in ('workflow_version', 'design_status', 'generated_at'):
            design.pop(field, None)
        return {'ontology_design': design, 'review_policy': 'HUMAN_REQUIRED'}
    draft_path = '03-mapping-review/mapping-draft.yaml'
    reviewed_path = '03-mapping-review/mapping.yaml'
    source_path = reviewed_path if reviewed_path in docs else draft_path
    draft = deepcopy(docs.get(draft_path, docs.get(reviewed_path)))
    if not isinstance(draft, dict):
        raise ValueError('S3 尚无映射输入，请先使用原阶段录入入口。')
    # Never merge an old reviewed mapping over a newer pending draft silently.
    if (draft_path in before['documents'] and reviewed_path in before['documents']
            and before['documents'][draft_path].get('mappings') != before['documents'][reviewed_path].get('mappings')):
        raise ValueError('S3 草案与已审映射不一致，请先通过原阶段入口对账。')
    draft['mappings'] = deepcopy(docs[source_path]['mappings'])
    runtime_path = project_dir / '03-mapping-review/runtime'
    runtime = load_reviewed_runtime_submission(runtime_path) if (runtime_path / 'runtime-source.json').is_file() else None
    return {'mapping_draft': draft,
            'confirmations': deepcopy(docs.get('03-mapping-review/pending-confirmations.json', [])),
            'automatic_decisions': deepcopy(docs.get('03-mapping-review/automatic-decisions.json', [])),
            'realtime_runtime': runtime, 'review_policy': 'HUMAN_REQUIRED'}


def preflight_design_patch(service, *, project_id: str, expected_revision: int,
                           expected_snapshot_sha256: str, stage: str,
                           operations: list[dict]) -> dict:
    stage = str(stage).upper().strip()
    if stage not in STAGE_KINDS or not operations:
        raise ValueError('局部设计变更仅支持非空 S2/S3/S4 操作。')
    if any(not isinstance(op, dict) or op.get('kind') not in STAGE_KINDS[stage] for op in operations):
        raise ValueError('禁止跨阶段局部变更；S0 CQ 必须使用原正式登记修订入口。')
    with service._project_mutation_lock(project_id) as project_dir:
        state = service._read_state(project_dir)
        service._require_expected_revision(state, expected_revision)
        before = workspace.build_snapshot(project_dir)
        proposal = workspace.propose_patch(before, operations,
                                            expected_snapshot_sha256=expected_snapshot_sha256)
        payload = _payload(project_dir, stage, before, proposal['snapshot'])
        workspace.verify_sources(project_dir, before)
        previous = getattr(service._project_lock_state, 'design_patch_basis', None)
        service._project_lock_state.design_patch_basis = {
            'project_id': project_id, 'stage': stage,
            'snapshot_sha256': before['snapshot_sha256'],
            'source_artifacts': before['source_artifacts'],
        }
        try:
            result = service.preflight_stage_submission(project_id=project_id, stage=stage, payload=payload)
        finally:
            service._project_lock_state.design_patch_basis = previous
        return {**result, 'design_patch': {
            'base_snapshot_sha256': before['snapshot_sha256'],
            'proposal_snapshot_sha256': proposal['snapshot']['snapshot_sha256'],
            'diff': proposal['diff'], 'approval_inherited': False,
            'formal_artifact_writes': False, 'commit_required': result.get('status') == 'PASSED',
        }}


def verify_commit_basis(project_dir, stage, basis):
    """Keep patch CAS valid until the first formal commit, not only preflight."""
    if (not isinstance(basis, dict) or basis.get('project_id') != project_dir.name
            or basis.get('stage') != stage or not isinstance(basis.get('source_artifacts'), dict)):
        raise ValueError('局部修订来源绑定无效。')
    current = workspace.build_snapshot(project_dir)
    if (current['source_artifacts'] != basis['source_artifacts']
            or current['snapshot_sha256'] != basis.get('snapshot_sha256')):
        raise ValueError('局部修订预检后来源已变化，拒绝覆盖，请重新读取设计并预检。')
