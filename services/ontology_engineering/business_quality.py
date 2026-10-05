"""Bounded, read-only quality projection over versioned engineering artifacts.

This view does not execute queries, parse instance graphs, approve business meaning,
refresh old artifacts or change formal gates. Expensive reconciliation belongs to S6.
"""
from __future__ import annotations

import hashlib
import json

import yaml

from services.ontology_contracts.errors import WorkflowError

MAX_ASSET_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ISSUES = 80


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _section(key, title, status, summary, metrics=(), issues=()):
    items = list(issues)
    return {"id": key, "title": title, "status": status, "summary": summary,
            "metrics": list(metrics), "issues": items[:MAX_ISSUES],
            "issue_count": len(items), "issues_truncated": len(items) > MAX_ISSUES}


def _issue(code, message, subject="", severity="WARNING", refs=()):
    return {"code": code, "severity": severity, "message": str(message)[:1400],
            "subject": str(subject)[:300], "source_refs": [str(r)[:400] for r in list(refs)[:10]]}


def build_business_quality(tools, project_dir, state):
    from .semantic_quality import build_semantic_quality
    used_bytes = 0
    assets, read_issues = [], []
    lifecycle = _dict(state.get("artifact_lifecycle"))

    def read(relative, default):
        nonlocal used_bytes
        if _dict(lifecycle.get(relative)).get("status") == "INVALIDATED":
            read_issues.append(_issue("ASSET_INVALIDATED", "该产物已失效，未用于本次质量检查。", relative))
            return default
        try:
            with tools._open_workspace_source(project_dir, relative) as stream:
                raw = stream.read(min(MAX_ASSET_BYTES, MAX_TOTAL_BYTES - used_bytes) + 1)
        except WorkflowError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return default
            read_issues.append(_issue("ASSET_UNREADABLE", "产物无法安全读取，未据此作出质量结论。", relative))
            return default
        if len(raw) > MAX_ASSET_BYTES or used_bytes + len(raw) > MAX_TOTAL_BYTES:
            read_issues.append(_issue("ASSET_TOO_LARGE", "产物超出交互检查预算，请通过分块产物或后台验收查看；此处未完整检查。", relative))
            return default
        used_bytes += len(raw)
        try:
            value = yaml.safe_load(raw) if relative.endswith(('.yaml', '.yml')) else json.loads(raw)
        except (ValueError, yaml.YAMLError, UnicodeError, RecursionError):
            read_issues.append(_issue("ASSET_INVALID", "产物格式无法解析，未据此作出质量结论。", relative))
            return default
        assets.append({"path": relative, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        return value

    intake = _dict(read('00-document-evidence/cq-intake.json', {}))
    candidates = _rows(_dict(read('02-semantic-recognition/ontology-candidates.yaml', {})).get('candidates'))
    rules = _rows(read('02-semantic-recognition/business-rule-candidates.json', []))
    capability = _dict(read('02-semantic-recognition/capability-plan.json', {}))
    design = _dict(read('04-ontology-design/ontology-design.yaml', {}))
    mapping_doc = _dict(read('03-mapping-review/mapping.yaml', {}))
    quality = _dict(read('06-quality-validation/quality-summary.json', {}))
    assessments = capability.get('cq_semantic_assessments')
    source_refs = set()
    source_registry_available = False
    for path in ('00-document-evidence/source-scope.json', '00-document-evidence/evidence-index.json',
                 '01-data-understanding/schema-snapshot.json', '01-data-understanding/data-profile.json'):
        data = read(path, None)
        if data is None:
            continue
        source_registry_available = True
        stack, seen = [data], set()
        while stack:
            item = stack.pop()
            if isinstance(item, dict | list):
                if id(item) in seen:
                    continue
                seen.add(id(item))
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in {'id', 'evidence_id', 'document_id', 'source_id', 'dataset_id', 'query_id'} and isinstance(value, str):
                        source_refs.add(value)
                    if key in {'table', 'table_name'} and isinstance(value, str):
                        source_refs.add('table:' + value)
                    if isinstance(value, dict | list):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)

    semantic = build_semantic_quality(candidates=candidates, rules=rules,
        questions=_rows(intake.get('questions')), assessments=assessments,
        known_source_refs=source_refs if source_registry_available and not read_issues else None,
        classes=_rows(design.get('classes')), relations=_rows(design.get('object_properties')))
    # Normalize diagnostic modules into one small presentation contract.
    semantic_issues = []
    design_frozen = _dict(state.get('stage_statuses')).get('S4') == 'PASSED'
    for item in semantic.get('issues', []):
        if item.get('code') in {'MISSING_DATA', 'MISSING_SEMANTICS', 'CQ_UNASSESSED', 'BUSINESS_CONFIRMATION_REQUIRED'}:
            continue  # CQ gaps have one dedicated section, not duplicate warnings.
        if design_frozen and str(item.get('component', '')).startswith('candidate:'):
            continue  # The reviewed S4 design supersedes tentative S2 descriptions.
        semantic_issues.append(_issue(str(item.get('code', 'SEMANTIC_REVIEW')),
            str(item.get('message') or item.get('reason') or '请核对业务语义。'),
            item.get('component') or item.get('subject') or item.get('subject_id') or item.get('entity_id') or '',
            item.get('severity', 'WARNING'), item.get('source_refs', [])))
    sem_count = len(_rows(design.get('classes'))) or len(candidates)
    sections = [_section('semantics', '业务对象与关系',
        'NOT_ASSESSED' if not sem_count else 'REVIEW_REQUIRED' if semantic_issues else 'CHECKED',
        '检查业务声明、证据引用和问题关联；声明完整不等于业务含义已获验证。',
        [{'label': '候选数量', 'value': len(candidates)}, {'label': '设计业务类', 'value': len(_rows(design.get('classes')))},
         {'label': '设计关系', 'value': len(_rows(design.get('object_properties')))}], semantic_issues)]

    reconciliation = _dict(quality.get('mapping_quality_report'))
    mapping_issues = []
    if not reconciliation:
        mapping_issues.append(_issue('RECONCILIATION_NOT_RUN', '尚无当前有效产物中的映射与实例对账回执。需在 S6 实际验收时计算；查看页面不会自动重跑工程。', severity='INFO'))
    finding_messages = {
        'DUPLICATE_MAPPING_ID': '映射标识重复，无法唯一定位生成规则。',
        'UNKNOWN_MAPPING_REFERENCE': '设计引用了不存在的映射。',
        'REQUIRED_CLASS_EMPTY': '要求非空的业务类没有直接实例。',
        'OBJECT_PROPERTY_LITERAL_TARGET': '业务关系连接到了字面值，而不是对象。',
        'TARGET_DESCRIPTION_ABSENT': '关系目标在当前快照中没有描述；可能是合法外部对象，需核对范围。',
    }
    for item in reconciliation.get('findings', []):
        mapping_issues.append(_issue(str(item.get('code', 'MAPPING_REVIEW')),
            finding_messages.get(item.get('code'), '请核对映射与实例。') +
            (f" 涉及数量：{item['count']}。" if 'count' in item else ''),
            item.get('iri') or item.get('mapping_id') or '',
            'ERROR' if item.get('severity') == 'ERROR' else 'WARNING'))
    if reconciliation:
        for code, field, message in [
            ('SOURCE_LINEAGE_UNAVAILABLE', 'source_to_instance_reconciliation', '尚无逐记录来源到实例的完整血缘回执，不能由实例数量推断来源处理覆盖率。'),
            ('IDENTITY_NOT_EVALUATED', 'identity_collision_check', '尚未执行来源主键到实例身份的碰撞核验，不能将去重后的 RDF 数量作为身份正确证明。'),
        ]:
            if _dict(reconciliation.get(field)).get('status') == 'NOT_EVALUATED':
                mapping_issues.append(_issue(code, message, severity='INFO'))
    sections.append(_section('mapping', '映射与实例对账',
        'NOT_ASSESSED' if not reconciliation else 'REVIEW_REQUIRED' if mapping_issues else 'CHECKED',
        '实例图计数与来源记录对账分别核验；缺少来源回执时不推测行数、重复率或转换覆盖率。',
        [{'label': '已登记映射', 'value': len(_rows(mapping_doc.get('mappings')))},
         {'label': 'S6 实例三元组', 'value': quality.get('materialized_triple_count', '未核验')},
         {'label': '已统计业务类', 'value': len(_rows(reconciliation.get('classes'))) if reconciliation else '未核验'},
         {'label': '已统计业务关系', 'value': len(_rows(reconciliation.get('relationships'))) if reconciliation else '未核验'}], mapping_issues))

    questions = _rows(intake.get('questions'))
    assessed = {row.get('question_id'): row for row in _rows(assessments)}
    cq_issues = []
    for q in questions:
        row = assessed.get(q.get('id'))
        if not row:
            cq_issues.append(_issue('CQ_NOT_ASSESSED', '尚未登记该业务问题的语义、数据需求与模型依赖。', q.get('id', ''), 'INFO'))
        else:
            if row.get('requires_business_confirmation') is True:
                cq_issues.append(_issue('BUSINESS_CONFIRMATION_REQUIRED', '该业务定义仍标记为需要负责人确认。', q.get('id', '')))
            for field, code in [('missing_semantics', 'CQ_SEMANTIC_GAP'), ('missing_data', 'CQ_DATA_GAP')]:
                for gap in row.get(field, []) if isinstance(row.get(field), list) else []:
                    cq_issues.append(_issue(code, str(gap), q.get('id', '')))
    if design_frozen:
        # S2 gap annotations are historical inputs; final design/validation takes precedence.
        cq_issues = [_issue('CQ_REVIEW_BASELINE', '联合设计已定稿；S2 的原始缺口不作为当前未决问题。实际答案与覆盖情况以同版 S6 验收为准。', severity='INFO')]
    cq_issues.append(_issue('INDEPENDENT_ACCEPTANCE', '本视图不证明验收预期来自独立业务依据；应核对批准用例的来源、反例与边界条件。', severity='INFO'))
    sections.append(_section('questions', '业务问题与验收依据', 'REVIEW_REQUIRED' if any(i['severity'] != 'INFO' for i in cq_issues) else 'NOT_ASSESSED',
        '问题到模型的引用检查与业务答案实际正确性分别展示。',
        [{'label': '原始业务问题', 'value': len(questions)}, {'label': '有语义评估的问题', 'value': sum(q.get('id') in assessed for q in questions)},
         {'label': '正式 S6 状态', 'value': {'PASSED': '已通过', 'PENDING': '待开始', 'RUNNING': '进行中', 'FAILED': '未通过'}.get(_dict(state.get('stage_statuses')).get('S6'), '未核验')}], cq_issues))
    if read_issues:
        sections.append(_section('scope', '本次检查范围', 'REVIEW_REQUIRED', '部分产物未纳入检查，不能把部分检查视为全量结论。', issues=read_issues))
    return {'schema_version': 1, 'project_id': state['project_id'], 'revision': state['revision'],
            'current_stage': state.get('current_stage'), 'formal_stage_status': _dict(state.get('stage_statuses')).get(state.get('current_stage'), 'PENDING'),
            'status': 'REVIEW_REQUIRED' if any(s['status'] == 'REVIEW_REQUIRED' for s in sections) else 'NOT_ASSESSED',
            'sections': sections, 'artifacts': assets, 'writes_performed': False,
            'limitations': ['本页为只读诊断，不授予审批、不推进阶段，也不重写历史验收。',
                '检查当前记录的产物；不在交互请求中重新执行来源查询、图计算或完整工程完整性验证。',
                '现有历史工程未自动迁移到新增诊断；未核验项不会显示为已通过。'],
            'read_bytes': used_bytes}
