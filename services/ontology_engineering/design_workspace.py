"""Derived typed design workspace; formal stage artifacts remain authoritative.

Patches are proposals returned in memory. Existing workflow preflight, review,
approval and commit APIs must accept them before any formal artifact can change.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .cq_semantics import validate_assessments
from .joint_design import _checksum, _digest, _inputs

# Each collection has one existing owner, rather than a new business schema.
COLLECTIONS = {
    'cq': ('00-document-evidence/cq-intake.json', 'questions'),
    'candidate': ('02-semantic-recognition/ontology-candidates.yaml', 'candidates'),
    'rule': ('02-semantic-recognition/business-rule-candidates.json', None),
    'assessment': ('02-semantic-recognition/capability-plan.json', 'cq_semantic_assessments'),
    'mapping': ('03-mapping-review/mapping.yaml', 'mappings'),
    **{kind: ('04-ontology-design/ontology-design.yaml', field) for kind, field in (
        ('class', 'classes'), ('object_property', 'object_properties'),
        ('data_property', 'data_properties'), ('constraint', 'constraints'),
        ('axiom', 'logical_axioms'), ('query', 'competency_questions'))},
}
DESIGN_PATH = '04-ontology-design/ontology-design.yaml'
DESIGN_DRAFT_PATH = '04-ontology-design/ontology-design-draft.yaml'
DESIGN_KINDS = {kind for kind, (path, _) in COLLECTIONS.items() if path == DESIGN_PATH}

S3_CONTEXT_PATHS = (
    '03-mapping-review/mapping-draft.yaml', '03-mapping-review/pending-confirmations.json',
    '03-mapping-review/automatic-decisions.json',
)
EVIDENCE_PATHS = (
    '00-document-evidence/evidence-index.json', '00-document-evidence/source-scope.json',
    '01-data-understanding/data-profile.json', '01-data-understanding/schema-snapshot.json',
    '01-data-understanding/evidence-sql.json',
)


def _identity(kind: str, value: dict) -> str:
    key = 'question_id' if kind == 'assessment' else 'id'
    identity = value.get(key)
    if kind in {'class', 'object_property', 'data_property'}:
        identity = value.get('iri') or value.get('name')
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError(f'{kind} 缺少稳定标识。')
    return identity


def _semantic(kind: str, value: dict) -> dict:
    value = deepcopy(value)
    # Only this explicit OWL symmetric construct is normalized. Never reorder
    # rule premises, SPARQL, business text, or general arrays by assumption.
    if (kind == 'axiom' and value.get('axiom_type') == 'DISJOINT_WITH'
            and isinstance(value.get('class'), str) and isinstance(value.get('other'), str)):
        value['class'], value['other'] = sorted((value['class'], value['other']))
    return value


def _components(documents: dict, sources: dict, selected_sources: dict) -> tuple[dict, list]:
    components, issues = {}, []
    for kind, (path, field) in COLLECTIONS.items():
        path = selected_sources.get(kind, path)
        if kind == 'mapping' and path not in documents and S3_CONTEXT_PATHS[0] in documents:
            path = S3_CONTEXT_PATHS[0]
        if path not in documents:
            continue
        doc = documents[path]
        values = doc.get(field, []) if isinstance(doc, dict) and field else doc
        if values is None and kind == 'assessment':
            continue
        if not isinstance(values, list):
            issues.append({'code': 'INVALID_COLLECTION', 'path': path, 'field': field})
            continue
        for index, value in enumerate(values):
            try:
                if not isinstance(value, dict):
                    raise ValueError('entry_not_object')
                identity = _identity(kind, value)
                key = f'{kind}:{identity}'
                if key in components:
                    raise ValueError('duplicate_identity')
                components[key] = {'kind': kind, 'id': identity, 'value': deepcopy(value),
                                   'content_sha256': _digest(value),
                                   'semantic_sha256': _digest(_semantic(kind, value)),
                                   'source': {'path': path, 'field': field, 'index': index,
                                              'sha256': sources.get(path)}}
            except ValueError:
                issues.append({'code': 'INVALID_OR_DUPLICATE_ID', 'path': path, 'field': field, 'index': index})
    return components, issues


def _reference_issues(components: dict, documents: dict) -> list:
    issues = []
    ids = {kind: {c['id'] for c in components.values() if c['kind'] == kind} for kind in COLLECTIONS}
    fields = {'source_question_id': 'cq', 'business_question_ids': 'cq',
              'required_candidate_ids': 'candidate', 'required_rule_ids': 'rule',
              'source_mapping_ids': 'mapping', 'mapping_refs': 'mapping'}
    for key, component in components.items():
        def check(value: Any, component_key: str = key) -> None:
            if isinstance(value, dict):
                for field, child in value.items():
                    if field in fields:
                        refs = child if isinstance(child, list) else [child]
                        for ref in refs:
                            if not isinstance(ref, str) or ref not in ids[fields[field]]:
                                issues.append({'code': 'MISSING_REFERENCE', 'component': component_key,
                                               'field': field, 'target_kind': fields[field], 'target': ref})
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(component['value'])
    # Reuse the established CQ semantic contract, including source provenance.
    assessments = [c['value'] for c in components.values() if c['kind'] == 'assessment']
    if assessments:
        refs = set()
        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {'id', 'evidence_id', 'document_id', 'source_id', 'dataset_id', 'query_id'} and isinstance(child, str):
                        refs.add(child)
                    if key in {'table_name', 'table'} and isinstance(child, str):
                        refs.add('table:' + child)
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        for path in EVIDENCE_PATHS[:-1]:
            collect(documents.get(path, {}))
        for evidence in documents.get(EVIDENCE_PATHS[-1], []) or []:
            if isinstance(evidence, dict) and evidence.get('status') == 'PASSED' and evidence.get('executed_at') and evidence.get('result_sha256'):
                refs.add(evidence.get('id'))
        try:
            validate_assessments(assessments,
                                 questions=[c['value'] for c in components.values() if c['kind'] == 'cq'],
                                 candidates=[c['value'] for c in components.values() if c['kind'] == 'candidate'],
                                 rules=[c['value'] for c in components.values() if c['kind'] == 'rule'], known_refs=refs)
        except (ValueError, KeyError, TypeError) as exc:
            issues.append({'code': 'CQ_CONTRACT_INVALID', 'reason': str(exc)})
    return issues


def _assemble(project_id: str, documents: dict, sources: dict, selected_sources: dict | None = None) -> dict:
    selected_sources = selected_sources or {}
    components, issues = _components(documents, sources, selected_sources)
    value = {'schema_version': 1, 'authority': 'DERIVED_REVIEW_ONLY', 'project_id': project_id,
             'source_artifacts': deepcopy(sources), 'documents': deepcopy(documents),
             'selected_sources': deepcopy(selected_sources),
             'components': components, 'issues': issues + _reference_issues(components, documents),
             'approval_inherited': False, 'writes_performed': False}
    return {**value, 'snapshot_sha256': _digest(value)}


def verify_sources(project_dir: Path, snapshot: dict) -> None:
    """Reject stale proposals before calling existing formal stage preflight."""
    validate_snapshot_hash(snapshot)
    if snapshot['project_id'] != project_dir.name:
        raise ValueError('共享设计工程身份不一致。')
    current = build_snapshot(project_dir)
    if current['source_artifacts'] != snapshot['source_artifacts']:
        raise ValueError('共享设计来源已变化，请重新读取并生成变更。')


def build_snapshot(project_dir: Path) -> dict:
    sources, documents = {}, {}
    state_path = project_dir / 'workflow-state.json'
    state = {}
    if state_path.exists():
        if state_path.is_symlink() or not state_path.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError('共享设计状态来源必须位于工程内。')
        state = yaml.safe_load(state_path.read_text(encoding='utf-8')) or {}
        if not isinstance(state, dict):
            raise ValueError('共享设计状态格式无效。')
    s3_reopened = (state.get('current_stage') == 'S3'
                   and (state.get('active_revision') or {}).get('target_stage') == 'S3'
                   and (state.get('stage_statuses') or {}).get('S3') == 'RUNNING')
    paths = {path for path, _ in COLLECTIONS.values()} | set(EVIDENCE_PATHS) | set(S3_CONTEXT_PATHS) | {DESIGN_DRAFT_PATH}
    if s3_reopened:
        paths = {name for name in paths if not name.startswith(('03-mapping-review/', '04-ontology-design/'))}
        sources['workflow-state.json'] = _checksum(state_path)
    for name in sorted(paths):
        path = project_dir / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError('共享设计来源必须位于工程内。')
        sources[name] = _checksum(path)
        documents[name] = yaml.safe_load(path.read_text(encoding='utf-8'))
    # Incorporate the exact source/runtime scope already governed by joint design.
    if '03-mapping-review/mapping.yaml' in documents:
        joint_sources = _inputs(project_dir)
        if any(name in sources and sources[name] != value for name, value in joint_sources.items()):
            raise ValueError('读取期间来源发生变化，请重新读取。')
        sources.update(joint_sources)
    runtime_dir = project_dir / '03-mapping-review/runtime'
    if runtime_dir.exists() and not s3_reopened:
        for path in runtime_dir.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(project_dir.resolve()):
                raise ValueError('共享设计运行来源必须位于工程内。')
            if path.is_file():
                name = path.relative_to(project_dir).as_posix()
                checksum = _checksum(path)
                if name in sources and sources[name] != checksum:
                    raise ValueError('读取期间来源发生变化，请重新读取。')
                sources[name] = checksum
    selected = {}
    if DESIGN_DRAFT_PATH in documents:
        if state_path.exists():
            sources['workflow-state.json'] = _checksum(state_path)
        pending = (state.get('current_stage') == 'S4'
                   and (state.get('stage_statuses') or {}).get('S4') in {'RUNNING', 'BLOCKED_HUMAN'})
        approved = (state.get('stage_statuses') or {}).get('S4') == 'PASSED'
        if pending or (DESIGN_PATH not in documents and not approved):
            selected = {kind: DESIGN_DRAFT_PATH for kind in DESIGN_KINDS}
        elif not approved and documents.get(DESIGN_PATH) != documents[DESIGN_DRAFT_PATH]:
            raise ValueError('设计草案与正式设计并存且当前状态无法确认使用范围，需正式对账。')
    for name, expected in sources.items():
        if _checksum(project_dir / name) != expected:
            raise ValueError('读取期间来源发生变化，请重新读取。')
    return _assemble(project_dir.name, documents, sources, selected)


def validate_snapshot_hash(snapshot: dict) -> None:
    body = {key: value for key, value in snapshot.items() if key != 'snapshot_sha256'}
    if snapshot.get('schema_version') != 1 or snapshot.get('snapshot_sha256') != _digest(body):
        raise ValueError('共享设计快照校验失败。')


def extract_components(snapshot: dict) -> dict:
    validate_snapshot_hash(snapshot)
    return deepcopy(snapshot['components'])


def diff_snapshots(before: dict, after: dict) -> dict:
    a, b = extract_components(before), extract_components(after)
    if before['project_id'] != after['project_id']:
        raise ValueError('不能对不同工程直接继承变更基线。')
    changes = []
    for key in sorted(a.keys() | b.keys()):
        if key not in a or key not in b:
            changes.append({'component': key, 'change': 'ADDED' if key in b else 'REMOVED', 'classification': 'BUSINESS_CHANGE'})
        elif a[key]['content_sha256'] != b[key]['content_sha256']:
            changes.append({'component': key, 'change': 'MODIFIED', 'classification': 'STRUCTURAL_EQUIVALENT'
                            if a[key]['semantic_sha256'] == b[key]['semantic_sha256'] else 'BUSINESS_CHANGE'})
    # Non-indexed fields and runtime assets still matter; no silent approval reuse.
    def unindexed(snapshot):
        documents = deepcopy(snapshot['documents'])
        for kind, (path, field) in COLLECTIONS.items():
            path = snapshot.get('selected_sources', {}).get(kind, path)
            if path in documents:
                if field and isinstance(documents[path], dict):
                    documents[path].pop(field, None)
                elif not field:
                    documents[path] = []
        return documents
    other_changed = unindexed(before) != unindexed(after)
    return {'changes': changes, 'source_artifacts_changed': before['source_artifacts'] != after['source_artifacts'],
            'documents_changed': before['documents'] != after['documents'],
            'approval_inherited': False, 'formal_preflight_required': True,
            'unindexed_fields_changed': other_changed,
            'business_change': other_changed or any(c['classification'] == 'BUSINESS_CHANGE' for c in changes),
            'scope': 'INDEXED_COLLECTIONS_ONLY_OTHER_FIELDS_REQUIRE_FORMAL_REVIEW'}


def propose_patch(snapshot: dict, operations: list[dict], *, expected_snapshot_sha256: str) -> dict:
    """Apply stable-ID compare-and-swap patches to an in-memory proposal only."""
    validate_snapshot_hash(snapshot)
    if expected_snapshot_sha256 != snapshot['snapshot_sha256']:
        raise ValueError('共享设计快照已变化。')
    if snapshot['issues']:
        raise ValueError('共享设计存在结构或引用缺口，先修复正式输入再生成局部变更。')
    documents = deepcopy(snapshot['documents'])
    touched = set()
    for op in operations:
        kind, identity, action = op['kind'], op['id'], op['op']
        if kind not in COLLECTIONS or action not in {'add', 'replace', 'remove'}:
            raise ValueError('局部变更操作不受支持。')
        key = f'{kind}:{identity}'
        if key in touched:
            raise ValueError('一次变更不能重复修改同一标识。')
        touched.add(key)
        old = snapshot['components'].get(key)
        if (action == 'add' and old) or (action != 'add' and not old):
            raise ValueError('局部变更标识已存在或不存在。')
        if action != 'add' and op.get('expected_content_sha256') != old['content_sha256']:
            raise ValueError('局部变更条目已变化。')
        path, field = COLLECTIONS[kind]
        path = snapshot.get('selected_sources', {}).get(kind, path)
        if old:
            path = old['source']['path']
        elif kind == 'mapping' and path not in documents and S3_CONTEXT_PATHS[0] in documents:
            path = S3_CONTEXT_PATHS[0]
        if path not in documents:
            raise ValueError('阶段输入尚未建立，请使用原阶段录入入口。')
        values = documents[path].setdefault(field, []) if field else documents[path]
        if action != 'remove':
            value = deepcopy(op['value'])
            if not isinstance(value, dict) or _identity(kind, value) != identity:
                raise ValueError('局部变更不能隐式修改稳定标识。')
        if action == 'add':
            values.append(value)
        else:
            index = next(i for i, v in enumerate(values) if _identity(kind, v) == identity)
            if action == 'remove':
                values.pop(index)
            else:
                values[index] = value
    proposed = _assemble(snapshot['project_id'], documents, snapshot['source_artifacts'], snapshot.get('selected_sources'))
    if proposed['issues']:
        raise ValueError('局部变更引入结构或引用缺口：' + str(proposed['issues']))
    return {'status': 'PROPOSAL_ONLY', 'base_snapshot_sha256': snapshot['snapshot_sha256'],
            'snapshot': proposed, 'diff': diff_snapshots(snapshot, proposed),
            'formal_artifact_writes': False, 'approval_inherited': False}
