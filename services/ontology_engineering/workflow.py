from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from difflib import unified_diff
from importlib.metadata import version as dependency_version
from pathlib import Path
from typing import Any

import httpx
import psycopg
import yaml
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import DCTERMS, SH

from services.ontology_contracts import cq_answers
from services.ontology_contracts.errors import WorkflowError as WorkflowError
from services.ontology_contracts.errors import WorkflowGateError as WorkflowGateError
from services.ontology_contracts.schema_snapshot import (
    normalize_s1_columns,
    normalize_s1_schema_snapshot,
)
from services.realtime_qa.capabilities import (
    CLOSED_WORLD_SET_DIFFERENCE_V1,
    platform_capability_catalog,
    reasoning_execution_capabilities,
    supports_reasoning_capability,
)
from services.realtime_qa.cq_contract import (
    CQBindingError,
    compile_reviewed_cq,
)
from services.realtime_qa.deployment_automation import (
    S7DeploymentAutomation,
    build_s7_deployment_automation,
)
from services.realtime_qa.runtime_release import (
    RuntimeReleaseError,
    finalize_runtime_review,
    normalize_runtime_submission,
    package_realtime_runtime,
    write_runtime_review_assets,
)
from services.realtime_qa.sparql_terms import sparql_has_only_scalar_aggregate_outputs
from services.realtime_qa.sparql_terms import sparql_references_iri as _sparql_references_iri

from . import business_modeling_contract as business_contract
from . import (
    capability_planning,
    closed_world_diagnostics,
    preflight_operations,
    s6_validation_receipts,
)
from . import cq_graph_reachability as cq_reach
from . import stage_submission_validation as submission_validation
from .axiom_applicability import logical_axiom_applicability
from .axiom_checks import has_disjoint_axiom
from .capability_planning import FACT_AGGREGATION_PATTERN
from .capability_planning import requires_reasoning as _requires_reasoning
from .contract_chain import CONTRACT_CHAIN_VERSION, validate_project_contract_chain
from .cq_semantics import build_review as build_cq_semantic_review
from .cq_semantics import validate_assessments
from .delivery_packages import (
    DeliveryPackageError,
    regenerate_s5_rule_review_assets,
    write_engineering_delivery_assets,
)
from .dependency_graph import component_revalidation_plan, normalize_changed_components
from .design_localization import normalize_ontology_design_localization
from .execution_policy import finalize_execution_directive
from .formal_facts import non_binary_property_bindings, validate_materialization_bindings
from .iri_terms import describe_undeclared, graph_signature, reasoning_terms_absent_from
from .joint_design import (
    canonicalize_design,
    freeze_joint_design,
    prepare_joint_design,
    validate_joint_approval,
    verify_joint_baseline,
    verify_joint_review,
)
from .mapping_preflight import (
    collect_mapping_runtime_issues,
)
from .mapping_runtime_coverage import runtime_uncovered_targets as _runtime_uncovered_targets
from .mapping_source_probe import normalize_s3_obda as _normalize_s3_obda
from .ontology_types import normalize_datatype_iri, validate_ontology_types
from .reporting import (
    ONTOLOGY_NAME_LABELS,
    REPORT_RENDERER_MARKER,
    render_change_report,
    render_s0_report,
    render_s0_scope_report,
    render_s1_report,
    render_s1_scope_report,
    render_s2_report,
    render_s3_report,
    render_s4_report,
    render_s5_report,
    render_s6_report,
    render_s7_report,
)
from .revision_draft_inheritance import draft_recovery_action
from .rule_scenarios import RuleScenarioError, ground_horn_scenario_fires
from .rule_term_contract import rule_conclusion_type_issues
from .semantica import PublishedOntologySemanticaSync, requires_instance_exploration
from .source_scope import (
    SourceScopeError,
    normalize_source_scope,
    reconcile_file_source_ids,
    validate_database_sources,
    validate_document_sources,
)
from .stage_contracts import (
    STAGE_CONTRACT_VERSION,
    project_stage_contract_version,
    stage_contract,
    stage_contract_catalog,
)
from .stage_liveness import classify_stage_liveness
from .stage_submission_validation import (
    DATABASE_CLASS_MAPPING_TYPES,
    DATABASE_DATA_MAPPING_TYPES,
    DOCUMENT_MAPPING_TYPES,
    RULE_CLASS_MAPPING_TYPES,
    SEMANTIC_STATUSES,
    SUPPORTED_MAPPING_TYPES,
    enforce_rule_class_bindings,
)
from .stage_submission_validation import (
    DATABASE_OBJECT_MAPPING_TYPES as DATABASE_OBJECT_MAPPING_TYPES,
)
from .stage_tool_contract import (
    BLOCKED_HUMAN_WRITE_TOOLS,
    COMMON_WRITE_TOOLS,
    S1_DOCUMENT_ONLY_WRITE_TOOLS,
    STAGE_WRITE_TOOLS,
)
from .storage import PostgresWorkflowMetadataStore

WORKFLOW_VERSION = "0.5.0"
FORMAL_ARTIFACT_FINGERPRINT_PROFILE = "formal-artifacts-v1"
PRODUCTION_GATE_POLICY_VERSION = "production-gates-v2"
STAGES = ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7")
CQ_INTAKE_MODES = {"USER_PROVIDED", "USER_PLUS_AI", "AI_GENERATED"}
SERVER_EXECUTED_CQ_VALIDATION_MODES = {
    "SERVER_EXECUTED_ANSWER_CONTRACT",
    "SERVER_EXECUTED_SEMANTIC_ANSWER_CONTRACT",
    "SERVER_EXECUTED_BASE_RELEASE_FULL_SOURCE_ONTOP",
    "SERVER_EXECUTED_PRE_RELEASE_FULL_SOURCE_ONTOP",
}
STAGE_FOLDERS = {
    "S0": "00-document-evidence",
    "S1": "01-data-understanding",
    "S2": "02-semantic-recognition",
    "S3": "03-mapping-review",
    "S4": "04-ontology-design",
    "S5": "05-ontology-build",
    "S6": "06-quality-validation",
    "S7": "07-release",
}
MAX_BLOCKING_CONFIRMATIONS = 3
PREFLIGHT_TOKEN_TTL_MINUTES = 30
S6_VALIDATION_SUBGATES = (
    "HERMIT",
    "MAPPING",
    "SEMANTIC",
    "SEMANTICA",
    "SHACL",
    "CQ",
    "PRODUCTION_COVERAGE",
)
WRITE_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|MERGE|CALL)\b",
    re.IGNORECASE,
)
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$")
SHA256_PATTERN = re.compile(r"^sha256:[a-fA-F0-9]{64}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
TEMPLATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
BUILTIN_TEMPLATE_ROOT = Path(__file__).parents[2] / "harness" / "ontology-templates"
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
REVISION_NAME_SUFFIX_PATTERN = re.compile(r"(?:\s*(?:[·•]\s*)?(?:[（(]\s*)?修订(?:\s*[）)])?)+\s*$")
BUSINESS_DIMENSION_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("subject", "主体", r"(?:义务|责任)?主体|由谁"),
    ("obligation", "义务", r"所负义务|法定义务|承担什么义务"),
    (
        "trigger",
        "触发条件",
        r"触发条件|什么条件触发|(?:生效|启动|发生|导致|满足)条件",
    ),
    ("deadline", "期限", r"期限|期限要求"),
    ("exception", "例外", r"例外情形|例外"),
    ("violation", "违法行为", r"违法行为|哪些行为构成"),
    ("liability", "法律责任", r"法律责任|对应责任"),
    ("treatment", "处理方式或措施", r"处理方式|处理措施"),
    ("supervision_subject", "监督主体", r"监督主体|管理部门如何"),
    ("supervision_object", "监督对象", r"监督对象"),
    ("supervision_method", "监督方式", r"监督方式|监督与检查"),
    ("archive_type", "档案类型", r"档案类型"),
    ("action_type", "移交开放利用类型", r"移交[、/与和]?开放[、/与和]?利用"),
)
RELATIONSHIP_REQUEST_PATTERN = re.compile(
    r"谁.*(?:义务|责任)|主体.*义务|义务.*(?:对象|条件|期限|例外)|"
    r"行为.*(?:责任|处理)|监督.*(?:对象|方式|措施)|档案.*(?:移交|开放|利用).*(?:期限|例外)",
    re.IGNORECASE,
)
DERIVED_STAGE_ARTIFACT_NAMES = frozenset(
    {
        "README.md",
        "semantica-sync.json",
    }
)


def _contains_chinese(value: Any) -> bool:
    return bool(CJK_PATTERN.search(str(value or "")))


def _revision_project_name(value: Any) -> str:
    """Return a stable revision name without accumulating suffixes."""

    source_name = str(value or "").strip()
    base_name = REVISION_NAME_SUFFIX_PATTERN.sub("", source_name).strip()
    return f"{base_name or source_name} · 修订"


def _required_business_dimensions(question: str, expected: str) -> list[dict[str, str]]:
    semantic_text = f"{question}\n{expected}"
    dimensions: list[dict[str, str]] = []
    for dimension, label_zh, pattern in BUSINESS_DIMENSION_PATTERNS:
        if re.search(pattern, semantic_text, re.IGNORECASE):
            dimensions.append({"dimension": dimension, "label_zh": label_zh})
    return dimensions


def _is_relationship_question(question: str, expected: str) -> bool:
    dimensions = _required_business_dimensions(question, expected)
    return len(dimensions) >= 2 or bool(
        RELATIONSHIP_REQUEST_PATTERN.search(f"{question}\n{expected}")
    )


def _cq_has_relationship_outputs(
    question: str, expected: str, sparql: str, object_property_iris: set[str],
) -> bool:
    # Explicit relationship semantics win over a caller's count-only query.
    if _is_relationship_question(question, expected):
        return True
    if (
        FACT_AGGREGATION_PATTERN.search(f"{question}\n{expected}")
        and sparql_has_only_scalar_aggregate_outputs(sparql)
    ):
        return False
    return any(_sparql_references_iri(sparql, iri) for iri in object_property_iris)


def _relationship_competency_questions(
    questions: list[dict[str, Any]], object_properties: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Share S4 answer classification with S6 graph checks and S7 release gates.

    Internal joins in proven scalar totals do not require relationship outputs;
    explicit business relationship semantics still override a count-only query.
    """
    object_property_iris = {
        str(item.get("iri") or "") for item in object_properties if isinstance(item, dict)
    }
    return [question for question in questions if _cq_has_relationship_outputs(
        str(question.get("question") or ""), str(question.get("expected") or ""),
        str(question.get("sparql") or ""), object_property_iris,
    )]


def _split_qualified_column(source: str) -> tuple[str, str]:
    """Split schema-qualified/quoted table expressions from their final column."""

    normalized = source.strip().removeprefix("table:")
    match = re.fullmatch(
        r'(?P<table>(?:[A-Za-z_][\w$]*\.)?"?[A-Za-z_][\w$]*"?)\.(?P<column>"?[A-Za-z_][\w$]*"?)',
        normalized,
    )
    if match is None:
        return normalized.split("(", 1)[0], ""
    return match.group("table"), match.group("column").strip('"')


_sparql_query_type = cq_answers._sparql_query_type


def _runtime_prefix_iri(mapping_obda: str) -> str | None:
    match = re.search(r"(?m)^\s*:\s*([^\s]+)\s*$", mapping_obda)
    return match.group(1).strip() if match else None


def _structured_markdown_name(
    item: dict[str, Any],
    used_names: set[str],
) -> str:
    document_id = str(item["document_id"])
    requested = str(item.get("structured_markdown_filename") or "").strip()
    candidate = Path(requested).name if requested else f"{document_id}.md"
    if (
        not candidate.lower().endswith(".md")
        or candidate in {".md", "..md"}
        or len(candidate.encode("utf-8")) > 240
    ):
        candidate = f"{document_id}.md"
    normalized = candidate.casefold()
    if normalized in used_names:
        candidate = f"{Path(candidate).stem}__{document_id.removeprefix('DOC-')[-8:]}.md"
        normalized = candidate.casefold()
    used_names.add(normalized)
    return candidate


STAGE_GUIDE = {
    "00-document-evidence": (
        "S0",
        "资料接入与证据整理",
        "把 PDF、图片和文档整理为可追溯的 Markdown、质量报告和证据索引。",
    ),
    "01-data-understanding": ("S1", "数据理解", "确认数据源、适用业务表、字段、质量与关系证据。"),
    "02-semantic-recognition": ("S2", "业务语义", "区分来源证据事实、AI 理解和规则候选。"),
    "03-mapping-review": ("S3", "映射评审", "形成来源证据到本体语义的正式映射（Mapping）。"),
    "04-ontology-design": ("S4", "本体设计", "形成 Class、Property、IRI、约束与业务问题施工图。"),
    "05-ontology-build": ("S5", "本体构建", "通过 Protégé 生成并验证 OWL、TTL 与 SHACL。"),
    "06-quality-validation": ("S6", "质量验证", "验证逻辑一致性、实例约束、业务问题与运行推理。"),
    "07-release": ("S7", "评审发布", "经明确审批后生成可校验的本体工程发布包。"),
}

ARTIFACT_GUIDE = {
    "README.md": ("阶段产物说明", "解释本阶段每份文件的作用和阅读顺序。", "说明文档"),
    "document-register.json": (
        "资料登记簿",
        "记录输入资料的标识、来源、页数、哈希和处理方式。",
        "输入证据",
    ),
    "ingestion-quality-report.json": (
        "接入质量报告",
        "记录页面处理、低置信度复核和失败页统计。",
        "质量证据",
    ),
    "evidence-index.json": (
        "证据索引",
        "把结构化 Markdown 段落追溯到原始文档页码或定位符。",
        "追溯证据",
    ),
    "processing-trace.json": (
        "文档处理轨迹",
        "记录 PaddleOCR 等 MCP 工具的运行编号、调用和时间。",
        "工具证据",
    ),
    "cq-intake.json": (
        "业务问题输入",
        "记录工程开始时由负责人提出的业务问题、正确结果和后续补充策略。",
        "需求证据",
    ),
    "document-evidence-report.html": (
        "S0 资料与证据报告",
        "汇总资料接入、结构化结果、质量边界和证据追溯。",
        "HTML 报告",
    ),
    "scope-decision.json": (
        "阶段接入范围判定",
        "记录纯数据库项目为何跳过 S0，或资料建模项目为何跳过 S1，并保留判定人、原因和证据范围。",
        "范围证据",
    ),
    "revision.json": ("调整记录", "记录调整阶段、原因、发起人和重新验证范围。", "审计证据"),
    "diff.json": ("调整差异清单", "记录调整前后所有受影响产物的哈希和变更类型。", "差异证据"),
    "change-report.html": ("调整差异报告", "展示阶段调整前后状态、产物和报告差异。", "HTML 报告"),
    "datasource-inventory.json": (
        "数据源选择清单",
        "记录发现了哪些数据库、为什么选择当前数据库以及排除了哪些内部表。",
        "数据证据",
    ),
    "scope-reconciliation.json": (
        "数据库范围差异",
        "记录初始表范围与当前只读 Schema 回读结果的新增、移除和修正原因。",
        "差异证据",
    ),
    "schema-snapshot.json": (
        "数据库结构快照",
        "固化适用表的字段、主键与外键，作为后续建模的结构事实。",
        "数据证据",
    ),
    "data-profile.json": (
        "数据画像",
        "记录各表数据量、枚举分布和质量观察，帮助判断业务概念是否有真实实例。",
        "数据证据",
    ),
    "relation-candidates.json": (
        "关系候选",
        "把数据库外键翻译为可审查的业务关系候选。",
        "语义候选",
    ),
    "evidence-sql.json": (
        "证据 SQL 台账",
        "保存只读查询及其用途，使关键判断可以回到数据库复核。",
        "追溯证据",
    ),
    "data-understanding-report.html": (
        "S1 数据理解报告",
        "用中文汇总数据库选择、适用表、数据规模、关系和证据边界。",
        "HTML 报告",
    ),
    "ontology-candidates.yaml": (
        "本体候选清单",
        "列出类、关系、数据属性及其事实等级，明确哪些来自文件或数据库证据、哪些属于 AI 理解。",
        "语义候选",
    ),
    "business-rule-candidates.json": (
        "业务规则候选",
        "保存 AI 推测的业务规则及证据，不把推测直接写入正式本体。",
        "AI 候选",
    ),
    "business-semantics-report.html": (
        "S2 业务语义报告",
        "以中文解释业务对象、关系、属性、AI 推测和置信度。",
        "HTML 报告",
    ),
    "mapping-draft.yaml": (
        "映射草案（Mapping）",
        "等待门禁检查的来源证据到本体映射施工草案。",
        "映射资产",
    ),
    "pending-confirmations.json": (
        "待确认事项",
        "记录仍需人工判断的高影响语义；为空表示没有新的阻塞项。",
        "评审记录",
    ),
    "automatic-decisions.json": (
        "自动建模决定",
        "记录依据明确、可由策略自动接受的建模决定和理由。",
        "评审记录",
    ),
    "decisions.jsonl": (
        "人工决定流水",
        "逐条保存人工裁决及修改内容，形成不可覆盖的审计轨迹。",
        "评审记录",
    ),
    "mapping.yaml": (
        "正式映射（Mapping）",
        "S3 通过后唯一允许进入本体设计的正式映射。",
        "正式资产",
    ),
    "mapping-review-report.html": (
        "S3 映射评审报告",
        "展示映射覆盖、自动决定、人工决定与门禁结论。",
        "网页报告（HTML）",
    ),
    "ontology-design.yaml": (
        "本体施工图",
        "定义业务类、对象属性、数据属性、本体唯一标识（IRI）、约束和验收问题。",
        "正式资产",
    ),
    "ontology-design-draft.yaml": (
        "本体施工图草案",
        "等待业务问题评审的设计草案；安全场景可由策略自动通过。",
        "设计草案",
    ),
    "competency-question-review.json": (
        "业务问题评审",
        "记录系统建议、人工修改、确认人和确认前后差异。",
        "人工评审记录",
    ),
    "ontology-design-report.html": (
        "S4 本体设计报告",
        "用中文解释施工图如何落实正式映射。",
        "网页报告（HTML）",
    ),
    "ontology.owl": ("OWL 本体", "供 Protégé 和 OWL 工具加载的正式 XML/RDF 本体文件。", "本体资产"),
    "ontology.ttl": ("TTL 本体", "便于审阅、版本比较和运行时加载的 Turtle 本体文件。", "本体资产"),
    "shapes.ttl": ("SHACL 约束", "定义真实实例必须满足的数据结构与取值约束。", "约束资产"),
    "protege-build-report.json": (
        "Protégé 构建证据",
        "记录汉化版 Protégé MCP 的构建、HermiT 推理和导出结果。",
        "工具证据",
    ),
    "ontology-build-report.html": (
        "S5 本体构建报告",
        "汇总本体规模、Protégé 证据、HermiT 结论和导出资产。",
        "HTML 报告",
    ),
    "materialized.ttl": (
        "真实实例图",
        "把已批准的文件或数据库事实按正式映射物化为资源描述框架（RDF）实例。",
        "实例资产",
    ),
    "protege-model-instance-preview.owl": (
        "模型与真实实例样本（Protégé）", "仅供阅读的独立本体，含同版 S6 有界真实实例；非全量、非正式发布模型。", "实例预览资产",
    ),
    "protege-instance-preview.json": (
        "Protégé 实例样本范围", "记录快照指纹、模型版本、逐类选取对象及截断限制。", "实例预览证据",
    ),
    "class-instance-validation.json": (
        "逐类实例验收", "核对实例合同与完整 S6 快照的显式类型数量；不冒充推理结果。", "质量证据",
    ),
    "hermit-report.json": ("HermiT 报告", "证明本体逻辑是否一致、是否存在不可满足类。", "质量证据"),
    "mapping-report.json": (
        "映射覆盖报告",
        "验证正式映射是否全部进入设计、构建和实例物化。",
        "质量证据",
    ),
    "semantic-quality-report.json": (
        "语义质量报告",
        "汇总实体定义和 Protégé 结构审计结果。",
        "质量证据",
    ),
    "competency-question-report.json": (
        "业务问题验证报告",
        "记录每个业务问题对应 SPARQL 的真实执行结果。",
        "质量证据",
    ),
    "semantica-report.json": (
        "图谱运行服务（Semantica）验证",
        "记录真实实例导入、规则推理和新知识推导结果。",
        "运行证据",
    ),
    "shacl-report.ttl": ("SHACL 机器报告", "供工具继续处理的实例约束验证结果。", "质量证据"),
    "shacl-report.txt": ("SHACL 可读报告", "供工程人员快速阅读实例约束是否通过。", "质量证据"),
    "quality-summary.json": (
        "S6 质量摘要",
        "汇总逻辑推理、数据约束、映射、验收问题和图谱运行服务的最终门禁指标。",
        "质量证据",
    ),
    "quality-validation-report.html": (
        "S6 质量验证报告",
        "用中文集中展示全部质量结论和运行验证证据。",
        "HTML 报告",
    ),
    "gate-results.json": (
        "阶段门禁结果",
        "记录本阶段每一道自动校验是否通过及关键指标。",
        "门禁证据",
    ),
    "publication.json": ("发布记录", "记录版本、批准人、发布时间与正式工程包位置。", "发布证据"),
    "release-snapshot.json": (
        "不可变发布快照",
        "固化发布前 S0-S6 正式产物指纹、审计链头和批准摘要。",
        "发布证据",
    ),
    "semantica-sync.json": (
        "Semantica 同步回执",
        "记录正式版本是否已进入可解释图谱运行时；失败不会改变发布包真实性。",
        "运行索引",
    ),
    "release-decision.json": (
        "发布决定",
        "记录暂不发布、恢复审批等人工决定和原因。",
        "发布控制记录",
    ),
    "release-decision-report.html": (
        "暂不发布总结报告",
        "说明暂不发布的负责人、原因、阶段状态和后续选择。",
        "网页报告",
    ),
    "release-resumption.json": (
        "恢复发布评审记录",
        "记录恢复发布评审的负责人、原因和时间。",
        "发布控制记录",
    ),
    "release-resumption-report.html": (
        "恢复发布评审总结报告",
        "说明为什么恢复评审，以及当前仍未形成发布批准。",
        "网页报告",
    ),
    "release-revocation.json": (
        "发布撤回记录",
        "声明已发布版本不再建议使用；原发布包仍保留供审计。",
        "发布控制记录",
    ),
    "release-revocation-report.html": (
        "发布撤回总结报告",
        "汇总撤回原因、原发布版本、全阶段成果和修订入口。",
        "网页报告",
    ),
    "based-on-release.json": (
        "修订来源",
        "记录新修订项目基于哪个已发布版本和哪些原始校验码。",
        "修订证据",
    ),
    "manifest.json": ("发布包清单", "保存发布包全部文件的 SHA-256，用于完整性校验。", "发布证据"),
    "release-report.html": (
        "S7 评审与发布总结报告",
        "汇总 S0 至 S7 的阶段成果、审批、版本、交付文件和完整性校验结果。",
        "网页报告",
    ),
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _file_checksum(path: Path) -> str:
    with path.open("rb") as handle:
        return f"sha256:{hashlib.file_digest(handle, 'sha256').hexdigest()}"


def _validate_reasoning_term_declarations(
    *,
    question_id: str,
    capability: dict[str, Any],
    allowed_predicates: set[str],
    entity_iris: set[str],
) -> None:
    """Fail S4 before build when runtime rule terms are absent from the TBox."""

    ontology_terms = capability.get("ontology_terms") or {}
    capability_predicates = {
        str(binding.get("predicate") or "").strip()
        for binding in capability.get("fact_bindings") or []
        if isinstance(binding, dict)
    } | allowed_predicates
    missing_term_names = sorted(
        name for name in capability_predicates if not str(ontology_terms.get(name) or "").strip()
    )
    undeclared_term_iris = sorted(
        {
            str(ontology_terms[name])
            for name in capability_predicates
            if str(ontology_terms.get(name) or "").strip()
            and str(ontology_terms[name]) not in entity_iris
        }
    )
    if missing_term_names or undeclared_term_iris:
        details: list[str] = []
        if missing_term_names:
            details.append("缺少 IRI：" + ", ".join(missing_term_names))
        if undeclared_term_iris:
            details.append(
                "未进入本体实体：" + describe_undeclared(undeclared_term_iris, entity_iris)
            )
        raise WorkflowGateError(
            "G-S4-REASONING-TERMS",
            f"能力问题 {question_id} 的规则前提/结论术语未完整进入 S4 本体：" + "；".join(details),
        )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:48] or "ontology-project"


class OntologyWorkflowService:
    """S0-S7 工作流、人工关口、审计链和本体工程资产的持久化实现。"""

    @staticmethod
    def _joint_design_enabled(state: dict[str, Any]) -> bool:
        return project_stage_contract_version(state) == STAGE_CONTRACT_VERSION

    def _verify_joint_design_for_execution(self, project_dir: Path) -> None:
        state_path = project_dir / "workflow-state.json"
        # Standalone schema validators can be used without an engineering state;
        # every formal stage entry separately requires an existing project.
        if state_path.is_file() and self._joint_design_enabled(self._read_state(project_dir)):
            try:
                verify_joint_baseline(project_dir)
            except (ValueError, OSError) as exc:
                raise WorkflowGateError("G-S4-JOINT-DESIGN", str(exc)) from exc

    def _validate_source_scope(
        self, project_dir: Path, *, stage: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        state = self._read_state(project_dir)
        if not self._joint_design_enabled(state):
            return None
        scope_path = project_dir / "00-document-evidence/source-scope.json"
        scope = self._read_json(scope_path)
        try:
            if stage == "S0":
                return validate_document_sources(
                    scope, payload.get("documents") or [],
                    processing_trace=payload.get("processing_trace"),
                )
            document_path = project_dir / "00-document-evidence/document-register.json"
            documents = self._read_json(document_path) if document_path.is_file() else []
            return validate_database_sources(scope, payload, documents=documents)
        except SourceScopeError as exc:
            raise WorkflowGateError(exc.gate, str(exc)) from exc

    def __init__(
        self,
        root: str | Path,
        *,
        metadata_database_url: str | None = None,
        metadata_required: bool | None = None,
        release_deployment_automation: S7DeploymentAutomation | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._project_lock_state = threading.local()
        self._metadata_required = (
            metadata_required
            if metadata_required is not None
            else os.getenv("ORION_WORKFLOW_METADATA_REQUIRED", "false").lower()
            in {"1", "true", "yes"}
        )
        database_url = (
            metadata_database_url
            if metadata_database_url is not None
            else os.getenv("ORION_WORKFLOW_DATABASE_URL")
        )
        self._metadata_configured = bool(database_url)
        self._metadata_store: PostgresWorkflowMetadataStore | None = None
        if database_url:
            try:
                self._metadata_store = PostgresWorkflowMetadataStore.from_env(database_url)
                self._metadata_store.ensure_schema()
            except Exception as exc:
                if self._metadata_required:
                    raise WorkflowError(
                        "PostgreSQL 工程账本初始化失败；为避免产生未入账操作，工作流已停止。"
                    ) from exc
                self._write_storage_status(
                    {
                        "status": "DEGRADED",
                        "message": "PostgreSQL 工程账本暂不可用，当前继续使用文件工作区。",
                        "error_type": type(exc).__name__,
                        "checked_at": _now(),
                    }
                )
        self._release_deployment_automation = (
            release_deployment_automation
            if release_deployment_automation is not None
            else build_s7_deployment_automation(
                self.root,
                status_listener=self._record_realtime_deployment_status,
                ontology_synchronizer=self._sync_release_ontology_for_deployment,
            )
        )
        if release_deployment_automation is not None and hasattr(
            release_deployment_automation,
            "set_status_listener",
        ):
            release_deployment_automation.set_status_listener(
                self._record_realtime_deployment_status
            )
        if release_deployment_automation is not None and hasattr(
            release_deployment_automation,
            "set_ontology_synchronizer",
        ):
            release_deployment_automation.set_ontology_synchronizer(
                self._sync_release_ontology_for_deployment
            )

    @staticmethod
    def _normalize_initial_competency_questions(
        questions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if questions is None:
            return []
        if not isinstance(questions, list):
            raise WorkflowError("initial_competency_questions 必须是数组。")
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(questions, start=1):
            if not isinstance(item, dict):
                raise WorkflowError(f"第 {index} 个业务问题必须是对象。")
            question_id = str(item.get("id") or f"CQ-INTAKE-{index:03d}").strip()
            question = str(item.get("question") or "").strip()
            expected = str(item.get("expected") or "").strip()
            priority = str(item.get("priority") or "MEDIUM").strip().upper()
            if not question_id or question_id in seen_ids:
                raise WorkflowError(f"第 {index} 个业务问题编号缺失或重复。")
            if not question or not expected:
                raise WorkflowError(f"业务问题 {question_id} 必须填写中文问题和“怎样算回答正确”。")
            if not _contains_chinese(question):
                raise WorkflowError(f"业务问题 {question_id} 必须使用中文表述。")
            if priority not in {"HIGH", "MEDIUM", "LOW"}:
                raise WorkflowError(
                    f"业务问题 {question_id} 的 priority 必须是 HIGH、MEDIUM 或 LOW。"
                )
            seen_ids.add(question_id)
            normalized.append(
                {
                    "id": question_id,
                    "question": question,
                    "expected": expected,
                    "priority": priority,
                    "example_entities": [
                        str(value).strip()
                        for value in item.get("example_entities") or []
                        if str(value).strip()
                    ],
                    "source": "USER_PROVIDED",
                }
            )
        return normalized

    @staticmethod
    def _load_builtin_template(template_id: str | None) -> dict[str, Any] | None:
        if template_id is None:
            return None
        if not TEMPLATE_ID_PATTERN.fullmatch(template_id):
            raise WorkflowError("template_id 格式不合法。")
        catalog_path = BUILTIN_TEMPLATE_ROOT / "catalog.json"
        try:
            catalog = OntologyWorkflowService._read_json(catalog_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowError("ORION 行业模板清单无法读取。") from exc
        entry = next(
            (item for item in catalog.get("templates") or [] if item.get("id") == template_id),
            None,
        )
        if not entry:
            raise WorkflowError("未找到指定的 ORION 行业模板。")
        if entry.get("kind") != "ORION_TEMPLATE" or entry.get("create_enabled") is not True:
            raise WorkflowError("行业参考只能查看，不能直接作为 ORION 工程模板。")
        template_root = (BUILTIN_TEMPLATE_ROOT / template_id).resolve()
        if not template_root.is_dir() or not str(template_root).startswith(
            str(BUILTIN_TEMPLATE_ROOT.resolve()) + os.sep
        ):
            raise WorkflowError("行业模板目录不合法。")
        declared_paths = [
            str(entry.get("ontology_path") or ""),
            "04-ontology-design/ontology-design.yaml",
            "02-semantic-recognition/business-rule-candidates.json",
        ]
        files: list[dict[str, Any]] = []
        for relative in declared_paths:
            source = (template_root / relative).resolve()
            if (
                not relative
                or not source.is_file()
                or not str(source).startswith(str(template_root) + os.sep)
            ):
                raise WorkflowError(f"行业模板缺少受控基线文件：{relative or 'ontology'}")
            files.append(
                {
                    "path": relative,
                    "sha256": f"sha256:{_file_checksum(source)}",
                    "bytes": source.stat().st_size,
                }
            )
        return {
            "template_id": template_id,
            "template_name": str(entry.get("name") or template_id),
            "template_version": str(entry.get("version") or ""),
            "template_kind": "ORION_TEMPLATE",
            "source_ref": str(entry.get("source_ref") or ""),
            "source_url": str(entry.get("source_url") or ""),
            "license": str(entry.get("license") or ""),
            "ontology_sha256": files[0]["sha256"],
            "files": files,
            "immutable": True,
        }

    @staticmethod
    def _snapshot_template_baseline(
        project_dir: Path,
        template_binding: dict[str, Any],
    ) -> None:
        template_root = (BUILTIN_TEMPLATE_ROOT / str(template_binding["template_id"])).resolve()
        baseline_root = project_dir / "template-baseline"
        baseline_root.mkdir(parents=True, exist_ok=False)
        for item in template_binding.get("files") or []:
            relative = str(item.get("path") or "")
            source = (template_root / relative).resolve()
            target = (baseline_root / relative).resolve()
            if not relative or not str(source).startswith(str(template_root) + os.sep):
                raise WorkflowError("行业模板基线文件路径不合法。")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            observed = f"sha256:{_file_checksum(target)}"
            if observed != item.get("sha256"):
                raise WorkflowError(f"行业模板基线复制校验失败：{relative}")
        OntologyWorkflowService._write_json(
            baseline_root / "template-manifest.json",
            {**template_binding, "snapshotted_at": _now()},
        )

    def create_project(
        self,
        *,
        project_name: str,
        domain: str,
        datasource_label: str | None = None,
        table_scope: list[str] | None = None,
        template_id: str | None = None,
        intake_mode: str = "HYBRID",
        intake_rationale: str | None = None,
        cq_mode: str = "USER_PLUS_AI",
        initial_competency_questions: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        source_scope: dict[str, Any] | None = None,
        _stage_contract_version: str = STAGE_CONTRACT_VERSION,
        _initial_project_status: str = "IN_PROGRESS",
    ) -> dict[str, Any]:
        with self._lock, self._root_operation_lock():
            return self._create_project_unlocked(
                project_name=project_name,
                domain=domain,
                datasource_label=datasource_label,
                table_scope=table_scope,
                template_id=template_id,
                intake_mode=intake_mode,
                intake_rationale=intake_rationale,
                cq_mode=cq_mode,
                initial_competency_questions=initial_competency_questions,
                request_id=request_id,
                source_scope=source_scope,
                _stage_contract_version=_stage_contract_version,
                _initial_project_status=_initial_project_status,
            )

    def _create_project_unlocked(
        self,
        *,
        project_name: str,
        domain: str,
        datasource_label: str | None = None,
        table_scope: list[str] | None = None,
        template_id: str | None = None,
        intake_mode: str = "HYBRID",
        intake_rationale: str | None = None,
        cq_mode: str = "USER_PLUS_AI",
        initial_competency_questions: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        source_scope: dict[str, Any] | None = None,
        _stage_contract_version: str = STAGE_CONTRACT_VERSION,
        _initial_project_status: str = "IN_PROGRESS",
    ) -> dict[str, Any]:
        """Create a project while the caller holds the root operation lock."""

        if not project_name.strip():
            raise WorkflowError("project_name 不能为空。")
        if not _contains_chinese(project_name):
            raise WorkflowError(
                "project_name 必须使用中文业务名称；英文缩写可作为补充，但不能替代中文名称。"
            )
        if not domain.strip():
            raise WorkflowError("domain 不能为空。")
        normalized_intake_mode = intake_mode.strip().upper()
        if normalized_intake_mode not in {
            "DOCUMENT_ONLY",
            "DATABASE_ONLY",
            "HYBRID",
        }:
            raise WorkflowError("intake_mode 必须是 DOCUMENT_ONLY、DATABASE_ONLY 或 HYBRID。")
        normalized_rationale = str(intake_rationale or "").strip()
        contract_version = project_stage_contract_version(
            {"stage_contract_version": _stage_contract_version}
        )
        try:
            normalized_scope = normalize_source_scope(
                intake_mode=normalized_intake_mode,
                datasource_label=datasource_label,
                table_scope=table_scope,
                source_scope=source_scope,
            )
        except SourceScopeError as exc:
            raise WorkflowGateError(exc.gate, str(exc)) from exc
        normalized_cq_mode = str(cq_mode or "USER_PLUS_AI").strip().upper()
        if normalized_cq_mode not in CQ_INTAKE_MODES:
            raise WorkflowError("cq_mode 必须是 USER_PROVIDED、USER_PLUS_AI 或 AI_GENERATED。")
        normalized_initial_questions = self._normalize_initial_competency_questions(
            initial_competency_questions
        )
        if normalized_cq_mode == "USER_PROVIDED" and not normalized_initial_questions:
            raise WorkflowError("USER_PROVIDED 模式至少需要填写 1 个业务问题。")
        if normalized_cq_mode == "AI_GENERATED" and normalized_initial_questions:
            raise WorkflowError("AI_GENERATED 模式不能同时提交人工业务问题。")
        normalized_request_id = str(request_id or "").strip() or None
        normalized_template_id = str(template_id or "").strip() or None
        template_binding = self._load_builtin_template(normalized_template_id)
        if _initial_project_status not in {"IN_PROGRESS", "INITIALIZING"}:
            raise WorkflowError("内部工程初始化状态不合法。")
        if normalized_request_id and not REQUEST_ID_PATTERN.fullmatch(normalized_request_id):
            raise WorkflowError("request_id 必须为 8～128 位字母、数字、点、下划线、冒号或连字符。")
        creation_request = {
            "stage_contract_version": contract_version,
            "source_scope": normalized_scope,
            "project_name": project_name.strip(),
            "domain": domain.strip(),
            "datasource_label": datasource_label,
            "table_scope": table_scope or [],
            "template_binding": template_binding,
            "intake_mode": normalized_intake_mode,
            "intake_rationale": normalized_rationale or None,
            "cq_mode": normalized_cq_mode,
            "initial_competency_questions": normalized_initial_questions,
        }
        if _initial_project_status == "INITIALIZING":
            creation_request["creation_kind"] = "REVISION_INITIALIZING"
        creation_fingerprint = _fingerprint(creation_request)

        with ExitStack() as project_locks:
            if normalized_request_id:
                for project_path in sorted(self.root.glob("*/project.json")):
                    existing_project = self._read_json(project_path)
                    if existing_project.get("creation_request_id") != normalized_request_id:
                        continue
                    if existing_project.get("creation_request_fingerprint") != creation_fingerprint:
                        raise WorkflowError(
                            "request_id 已用于另一组新建参数；为避免误复用工程，本次请求已拒绝。"
                        )
                    state_path = project_path.parent / "workflow-state.json"
                    if not state_path.exists():
                        raise WorkflowError(
                            "相同 request_id 对应的工程仍在初始化；请稍后使用同一 request_id 重试。"
                        )
                    return {
                        **self._status_payload(
                            project_path.parent,
                            self._read_state(project_path.parent),
                        ),
                        "request_id": normalized_request_id,
                        "idempotent_replay": True,
                    }
            project_id = f"{_slug(domain)}-{uuid.uuid4().hex[:8]}"
            project_dir = self.root / project_id
            project_dir.mkdir(parents=True, exist_ok=False)
            project_locks.enter_context(self._project_operation_lock(project_dir))
            for folder in (
                "00-document-evidence",
                "01-data-understanding",
                "02-semantic-recognition",
                "03-mapping-review",
                "04-ontology-design",
                "05-ontology-build",
                "06-quality-validation",
                "07-release",
                "events",
                "revisions",
            ):
                (project_dir / folder).mkdir()

            created_at = _now()
            project = {
                "stage_contract_version": contract_version,
                "source_scope": normalized_scope,
                "project_id": project_id,
                "project_name": project_name.strip(),
                "domain": domain.strip(),
                "intake_mode": normalized_intake_mode,
                "intake_rationale": normalized_rationale or None,
                "cq_mode": normalized_cq_mode,
                "initial_competency_question_count": len(normalized_initial_questions),
                "assurance_profile": "PRODUCTION",
                "production_gate_policy_version": PRODUCTION_GATE_POLICY_VERSION,
                "datasource_label": datasource_label,
                "table_scope": table_scope or [],
                "template_binding": template_binding,
                "current_stage": "S0",
                "status": _initial_project_status,
                "created_at": created_at,
                "updated_at": created_at,
                "creation_request_id": normalized_request_id,
                "creation_request_fingerprint": creation_fingerprint,
            }
            state = {
                **({"business_modeling_contract_version": business_contract.VERSION} if _initial_project_status != "INITIALIZING" and contract_version == STAGE_CONTRACT_VERSION else {}),
                "stage_contract_version": contract_version,
                "stage_contracts": stage_contract_catalog(contract_version),
                "workflow_id": "orion-ontology-engineer",
                "workflow_version": WORKFLOW_VERSION,
                "revision": 1,
                "project_id": project_id,
                "project_name": project_name.strip(),
                "intake_mode": normalized_intake_mode,
                "intake_rationale": normalized_rationale or None,
                "cq_mode": normalized_cq_mode,
                "initial_competency_question_count": len(normalized_initial_questions),
                "assurance_profile": "PRODUCTION",
                "production_gate_policy_version": PRODUCTION_GATE_POLICY_VERSION,
                "current_stage": "S0",
                "project_status": _initial_project_status,
                "stage_statuses": {stage: "PENDING" for stage in STAGES},
                "stage_fingerprints": {},
                "blocking": None,
                "last_error": None,
                "resume_point": (
                    "修订工程正在从不可变发布版本初始化"
                    if _initial_project_status == "INITIALIZING"
                    else "S0: 登记资料并形成可追溯的结构化 Markdown 与质量证据"
                ),
                "created_at": created_at,
                "updated_at": created_at,
                "creation_request_id": normalized_request_id,
                "creation_request_fingerprint": creation_fingerprint,
                "template_binding": template_binding,
            }
            state["stage_statuses"]["S0"] = "RUNNING"
            self._write_json(project_dir / "project.json", project)
            self._write_json(project_dir / "workflow-state.json", state)
            if self._joint_design_enabled(state):
                self._write_json(project_dir / "00-document-evidence/source-scope.json", normalized_scope)
            if template_binding:
                self._snapshot_template_baseline(project_dir, template_binding)
            cq_intake = {
                "schema_version": 1,
                "mode": normalized_cq_mode,
                "status": "RECORDED",
                "questions": normalized_initial_questions,
                "question_count": len(normalized_initial_questions),
                "technical_query_stage": "S4",
                "created_at": created_at,
            }
            cq_intake["questions_sha256"] = _fingerprint(normalized_initial_questions)
            self._write_json(project_dir / "00-document-evidence/cq-intake.json", cq_intake)
            self._append_event(
                project_dir,
                "PROJECT_CREATED",
                state,
                {
                    "domain": domain,
                    "stage": "S0",
                    "intake_mode": normalized_intake_mode,
                    "intake_rationale": normalized_rationale or None,
                    "cq_mode": normalized_cq_mode,
                    "initial_competency_question_count": len(normalized_initial_questions),
                    "request_id": normalized_request_id,
                    "template_binding": template_binding,
                    "actor": "Harness",
                },
            )
            self._refresh_manifest(project_dir)
            return {
                **self._status_payload(project_dir, state),
                "request_id": normalized_request_id,
                "idempotent_replay": False,
            }

    def list_projects(self) -> dict[str, Any]:
        projects: list[dict[str, Any]] = []
        for state_path in sorted(self.root.glob("*/workflow-state.json")):
            state = self._normalize_state(self._read_json(state_path))
            project_path = state_path.parent / "project.json"
            project = self._read_json(project_path) if project_path.exists() else {}
            publication_path = state_path.parent / "07-release/publication.json"
            publication = self._read_json(publication_path) if publication_path.exists() else {}
            projects.append(
                {
                    "project_id": state["project_id"],
                    "project_name": state["project_name"],
                    "domain": project.get("domain"),
                    "current_stage": state["current_stage"],
                    "project_status": state["project_status"],
                    "revision": state.get("revision", 0),
                    "stage_statuses": state.get("stage_statuses", {}),
                    "intake_mode": project.get("intake_mode")
                    or state.get("intake_mode")
                    or "HYBRID",
                    "parent_project_id": project.get("parent_project_id"),
                    "based_on_release_version": project.get("based_on_release_version"),
                    "suggested_release_version": project.get("suggested_release_version"),
                    "release_version": publication.get("release_version"),
                    "updated_at": state["updated_at"],
                    "stage_liveness": classify_stage_liveness(state_path.parent, state),
                }
            )
        projects.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"count": len(projects), "projects": projects}

    def archive_project(
        self,
        *,
        project_id: str,
        archived_by: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """归档工程但保留全部文件、报告、事件和数据库账本记录。"""

        if not archived_by.strip():
            raise WorkflowError("archived_by 不能为空。")
        if not reason.strip():
            raise WorkflowError("reason 不能为空。")
        with self._project_mutation_lock(project_id) as project_dir:
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            if state.get("project_status") == "ARCHIVED":
                raise WorkflowError("该工程已经归档，重复提交已拒绝。")
            blockers = self._project_archive_blockers(project_dir, state)
            if blockers:
                summary = "；".join(str(item["message"]) for item in blockers)
                raise WorkflowError(f"工程当前不能移入回收站：{summary}")
            state["archived_from_status"] = state.get("project_status") or "IN_PROGRESS"
            state["archived_running_stages"] = sorted(
                stage
                for stage, status in (state.get("stage_statuses") or {}).items()
                if status == "RUNNING"
            )
            state["resume_point_before_archive"] = state.get("resume_point")
            state["project_status"] = "ARCHIVED"
            state["archived_at"] = _now()
            state["archived_by"] = archived_by.strip()
            state["archive_reason"] = reason.strip()
            state["resume_point"] = "工程已归档；恢复后从原阶段继续。"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "PROJECT_ARCHIVED",
                state,
                {
                    "stage": state.get("current_stage") or "S0",
                    "archived_by": archived_by.strip(),
                    "reason": reason.strip(),
                    "previous_project_status": state["archived_from_status"],
                    "expected_revision": expected_revision,
                },
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def restore_project(
        self,
        *,
        project_id: str,
        restored_by: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """恢复已归档工程，并从归档前的阶段和状态继续。"""

        if not restored_by.strip():
            raise WorkflowError("restored_by 不能为空。")
        if not reason.strip():
            raise WorkflowError("reason 不能为空。")
        with self._project_mutation_lock(
            project_id,
            allow_archived=True,
        ) as project_dir:
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            if state.get("project_status") != "ARCHIVED":
                raise WorkflowError("只有已归档工程可以恢复；重复或过期操作已拒绝。")
            restored_status = str(state.get("archived_from_status") or "IN_PROGRESS")
            state["project_status"] = restored_status
            state["resume_point"] = state.get("resume_point_before_archive") or (
                f"{state.get('current_stage') or 'S0'}: 继续原阶段工作"
            )
            state["last_restored_at"] = _now()
            state["last_restored_by"] = restored_by.strip()
            state["last_restore_reason"] = reason.strip()
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "PROJECT_RESTORED",
                state,
                {
                    "stage": state.get("current_stage") or "S0",
                    "restored_by": restored_by.strip(),
                    "reason": reason.strip(),
                    "restored_project_status": restored_status,
                    "expected_revision": expected_revision,
                },
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def get_status(self, project_id: str | None = None) -> dict[str, Any]:
        project_dir = self._resolve_project(project_id)
        state = self._read_state(project_dir)
        return self._status_payload(project_dir, state)

    def get_design_workspace(self, project_id: str) -> dict[str, Any]:
        from .design_workspace_api import get_design_workspace
        return get_design_workspace(self, project_id)

    def preflight_design_patch(self, *, project_id: str, expected_revision: int,
                               expected_snapshot_sha256: str, stage: str,
                               operations: list[dict[str, Any]]) -> dict[str, Any]:
        from .design_workspace_api import preflight_design_patch
        return preflight_design_patch(
            self, project_id=project_id, expected_revision=expected_revision,
            expected_snapshot_sha256=expected_snapshot_sha256, stage=stage,
            operations=operations,
        )

    def get_revision_reuse_plan(self, project_id: str) -> dict[str, Any]:
        from .revision_reuse import get_revision_reuse_plan
        project_dir = self._resolve_project(project_id)
        reference_path = project_dir / "based-on-release.json"
        if not reference_path.is_file():
            raise WorkflowError("当前工程没有正式修订来源，无法证明跨版本复用。")
        reference = self._read_json(reference_path)
        source_id = reference.get("source_project_id") or reference.get("project_id")
        if not source_id or source_id == project_dir.name:
            raise WorkflowError("修订来源身份不完整。")
        source_dir = self._resolve_project(source_id)
        publication = source_dir / "07-release/publication.json"
        if (not publication.is_file() or reference.get("source_publication_sha256")
                != _file_checksum(publication)):
            raise WorkflowError("原发布凭据已变化，无法验证修订来源；需先对账。")
        from .revision_reuse import verify_revision_origin
        origin = verify_revision_origin(self, source_dir, reference)
        return {**get_revision_reuse_plan(source_dir, project_dir), "source_release_verification": origin}

    def _cq_semantic_review(self, project_dir: Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read-only design review, also shared by S2 preflight and commit."""
        def read(relative: str, default: Any) -> Any:
            path = project_dir / relative
            return self._read_json(path) if path.is_file() else default

        state = self._read_state(project_dir)
        questions = read("00-document-evidence/cq-intake.json", {}).get("questions", [])
        if payload is None:
            candidate_path = project_dir / "02-semantic-recognition/ontology-candidates.yaml"
            candidate_doc = yaml.safe_load(candidate_path.read_text(encoding="utf-8")) if candidate_path.is_file() else {}
            plan = read("02-semantic-recognition/capability-plan.json", {})
            payload = {
                "ontology_candidates": (candidate_doc or {}).get("candidates", []),
                "business_rule_candidates": read("02-semantic-recognition/business-rule-candidates.json", []),
                "cq_semantic_assessments": plan.get("cq_semantic_assessments"),
            }
        candidates = payload.get("ontology_candidates") or []
        rules = payload.get("business_rule_candidates") or []
        assessments = payload.get("cq_semantic_assessments")
        known_refs: set[str] = set()
        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"id", "evidence_id", "document_id", "source_id", "dataset_id", "query_id"} and isinstance(item, str):
                        known_refs.add(item)
                    if key in {"table_name", "table"} and isinstance(item, str):
                        known_refs.add("table:" + item)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        for relative in ("00-document-evidence/evidence-index.json", "00-document-evidence/source-scope.json",
                         "01-data-understanding/data-profile.json", "01-data-understanding/schema-snapshot.json"):
            collect(read(relative, {}))
        for evidence in read("01-data-understanding/evidence-sql.json", []):
            if (isinstance(evidence, dict) and evidence.get("status") == "PASSED"
                    and evidence.get("executed_at") and evidence.get("result_sha256")
                    and isinstance(evidence.get("id"), str)):
                known_refs.add(evidence["id"])
        decisions = []
        decision_path = project_dir / "03-mapping-review/decisions.jsonl"
        if decision_path.is_file():
            decisions = [json.loads(line) for line in decision_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            collect(decisions)
        try:
            validate_assessments(assessments, questions=questions, candidates=candidates, rules=rules, known_refs=known_refs)
        except ValueError as exc:
            raise WorkflowGateError("G-S2-CQ-SEMANTICS", str(exc)) from exc
        runtime = read("03-mapping-review/runtime/runtime-source.json", {})
        result = build_cq_semantic_review(
            questions=questions, candidates=candidates, rules=rules, assessments=assessments,
            intake_mode=str(state.get("intake_mode") or "HYBRID"),
            structured_source_present=(project_dir / "01-data-understanding/schema-snapshot.json").is_file(),
            document_instances_present=bool(runtime.get("document_fact_queries")),
        )
        result["existing_business_decisions"] = decisions
        result["known_source_refs"] = sorted(known_refs)
        result["cq_output_requirements"] = [{
            "question_id": question.get("id"),
            "required_business_dimensions": _required_business_dimensions(
                str(question.get("question") or ""), str(question.get("expected") or "")),
            "relationship_from_question": _is_relationship_question(
                str(question.get("question") or ""), str(question.get("expected") or "")),
        } for question in questions if isinstance(question, dict)]
        result["cq_output_requirement_scope"] = (
            "现有 S3/S4 门禁从原始问题推导的最低输出要求，不代替完整业务范围或事实依据。"
            "REQUIRED 维度同时填写 SELECT 返回变量 binding 和已声明完整 IRI ontology_term；"
            "关系问题至少两个维度，非 subject 的适用维度需提供查询实际引用的单个属性 IRI path，"
            "并以有来源的具体结果断言覆盖。查询本身的对象关系还可能触发关系检查。"
            "不能为通过检查删除维度或编造测试答案。"
        )
        return result

    def get_cq_semantic_review(self, project_id: str) -> dict[str, Any]:
        project_dir = self._resolve_project(project_id)
        with self._project_operation_lock(project_dir):
            state = self._read_state(project_dir)
            review = self._cq_semantic_review(project_dir)
            return {"project_id": project_dir.name, "revision": int(state.get("revision") or 0),
                    "stage_statuses": dict(state.get("stage_statuses") or {}),
                    "writes_performed": False, **review}

    def get_next_action(self, project_id: str | None = None) -> dict[str, Any]:
        """Return a deterministic execution directive instead of asking an LLM to plan stages."""

        project_dir = self._resolve_project(project_id)
        state = self._read_state(project_dir)
        directive = self._get_next_action_directive(project_dir, state)
        publication_path = project_dir / "07-release/publication.json"
        publication = (
            self._read_json(publication_path)
            if state.get("current_stage") == "S7" and publication_path.is_file()
            else None
        )
        return finalize_execution_directive(directive, state=state, publication=publication)

    def _get_next_action_directive(
        self, project_dir: Path, state: dict[str, Any]
    ) -> dict[str, Any]:
        stage = str(state.get("current_stage") or "").upper() or None
        stage_status = (
            str((state.get("stage_statuses") or {}).get(stage) or "UNKNOWN").upper()
            if stage
            else None
        )
        base: dict[str, Any] = {
            "schema_version": 1,
            "project_id": project_dir.name,
            "project_name": state.get("project_name"),
            "intake_mode": state.get("intake_mode"),
            "project_status": state.get("project_status"),
            "current_stage": stage,
            "stage_status": stage_status,
            # Native progress must reflect the same revision as this directive.
            # A current-stage label alone cannot prove earlier stages passed.
            "stage_statuses": dict(state.get("stage_statuses") or {}),
            "revision": int(state.get("revision") or 0),
            "writes_performed": False,
            "business_modeling_contract_version": state.get("business_modeling_contract_version"),
            "business_modeling_requirements": ({
                "order": "业务对象与身份、关系 → 执行映射 → 分析与规则",
                "class_contract_field": "instance_contract",
                "required_fields": ["business_role", "instance_meaning", "generation_mode", "identity_rule", "mapping_refs", "empty_policy", "empty_reason"],
                "empty_policies": sorted(business_contract.EMPTY),
                "generation_modes": sorted(business_contract.MODES),
                "business_roles": sorted(business_contract.ROLES),
                "internal_evidence_default_business_exploration": False,
                "mapping_refs_status": "声明引用；执行正确性还须通过运行映射、SHACL、CQ及来源验收",
            } if business_contract.enabled(state) else None),
            "stage_contract_version": project_stage_contract_version(state),
            "stage_contracts": stage_contract_catalog(project_stage_contract_version(state)),
            "stage_collaboration": (
                stage_contract(stage or "S4", project_stage_contract_version(state))
                if stage is None or stage in STAGES else None
            ),
            "policy": {
                "planner": "ORION_WORKFLOW_STATE_MACHINE",
                "model_may_choose_stage": False,
                "model_may_invent_contract_fields": False,
                "technical_missing_input_owner": "PLATFORM_OR_ENGINEERING_AGENT",
                "human_input_boundary": "BUSINESS_DECISION_OR_RELEASE_APPROVAL_ONLY",
            },
        }
        recovery = preflight_operations.recovery_diagnostics(
            project_dir, lambda item_stage: self._preflight_checkpoint(project_dir, item_stage)
        )
        base["preflight_recovery"] = recovery
        if recovery["requires_reconciliation"]:
            return {**base, "action": "READ_STATUS_AND_INTEGRITY",
                    "allowed_write_tools": [],
                    "input_owner": "PLATFORM_OR_ENGINEERING_AGENT",
                    "reason": "恢复证据需要对账；停止重复提交，保留原操作与正式产物。"}
        try:
            pending = preflight_operations.pending(project_dir)
        except (ValueError, OSError):
            return {**base, "action": "READ_STATUS_AND_INTEGRITY",
                    "reason": "预检恢复记录损坏，需平台核对；禁止继续写入。"}
        if pending:
            return {**base, "action": "RECOVER_PREFLIGHT_OPERATION",
                    "recommended_tool": "commit_preflight_stage_submission",
                    "allowed_write_tools": ["commit_preflight_stage_submission"],
                    "input_owner": "PLATFORM_OR_ENGINEERING_AGENT",
                    "pending_operations": [{"operation_id": item["operation_id"],
                                            "stage": item["stage"], "status": item["status"]}
                                           for item in pending],
                    "reason": "使用原 preflight_token 恢复原提交结果；若返回需对账则停止重试，不能重建载荷。"}
        if stage in {"S2", "S3", "S4"} or state.get("project_status") == "S1_S3_READY":
            base["design_workspace_tools"] = {
                "read": "get_design_workspace", "patch_preflight": "preflight_design_patch",
                "instruction": "先读共用设计索引；修改已有组件时按ID取必要条目，携带revision、快照及条目指纹局部预检。正式提交仍用原token；不重写无关大JSON，不继承批准。",
            }
            if (project_dir / "based-on-release.json").is_file():
                base["revision_impact_tool"] = "get_revision_reuse_plan"
            base["cq_semantic_review_tool"] = "get_cq_semantic_review"
            base["cq_semantic_review_instruction"] = (
                "先逐 CQ 核对定义、模型和实例缺口；缺政策复用 S3 决策卡，缺来源先完成正式来源登记。"
                "只读评估不批准阶段；已知缺口无变化时不重复预检或读取源码猜测。"
            )
        if str(state.get("project_status") or "").upper() == "ARCHIVED":
            return {
                **base,
                "action": "RESTORE_PROJECT_IF_REQUESTED",
                "recommended_tool": "restore_ontology_project",
                "allowed_write_tools": ["restore_ontology_project"],
                "input_owner": "HUMAN",
                "reason": "工程已归档；只有用户明确要求继续时才恢复。",
            }
        if str(state.get("project_status") or "").upper() == "PUBLISHED":
            return {
                **base,
                "action": "USE_RELEASE_OR_CREATE_REVISION",
                "recommended_tool": "create_revision_from_release",
                "allowed_write_tools": [
                    "create_revision_from_release",
                    "revoke_ontology_release",
                    "sync_published_ontology_to_semantica",
                    "archive_ontology_project",
                ],
                "input_owner": "HUMAN",
                "reason": "正式发布包不可变；内容变化必须创建独立修订工程。",
            }
        if (
            str(state.get("project_status") or "").upper() == "S1_S3_READY"
            and stage is None
            and str((state.get("stage_statuses") or {}).get("S3") or "").upper() == "PASSED"
            and str((state.get("stage_statuses") or {}).get("S4") or "").upper()
            in {"PENDING", "INVALIDATED"}
        ):
            return {
                **base,
                "current_stage": "S4",
                "stage_status": "PENDING",
                "action": "GENERATE_AND_PREFLIGHT_ONTOLOGY_DESIGN",
                "recommended_tool": "generate_ontology_design",
                "allowed_write_tools": [
                    "generate_ontology_design",
                    "prepare_ontology_design_review",
                    "archive_ontology_project",
                    "reopen_stage_for_correction",
                ],
                "input_owner": "PLATFORM_COMPILER",
                "source_artifacts": [
                    "00-document-evidence/cq-intake.json",
                    "03-mapping-review/mapping.yaml",
                    "03-mapping-review/realtime-runtime.json",
                ],
                "transition_state": "S1_S3_READY",
                "reason": (
                    "S1-S3 已完成并冻结。先准备有来源的 logical_axioms，使用 S4 generation_request"
                    "预检后提交 token；平台从已审 cq_bindings 编译完整 CQ 并受控启动 S4。"
                    "不能空载荷反复生成、猜造公理或手写答案契约。"
                ),
            }
        if stage not in STAGES:
            return {
                **base,
                "action": "READ_STATUS_AND_INTEGRITY",
                "recommended_tool": "get_ontology_workflow_status",
                "allowed_write_tools": [],
                "input_owner": "PLATFORM",
                "reason": "当前没有可写阶段；先回读状态与完整性，不得猜测恢复点。",
            }

        common_write_tools = list(COMMON_WRITE_TOOLS)
        if stage_status == "RUNNING":
            liveness = classify_stage_liveness(project_dir, state)
            base["stage_liveness"] = liveness
            if liveness["state"] == "SUSPECTED_INTERRUPTED":
                # Keep the stage's normal tools: its runners resume from
                # checkpoints with the same input fingerprint. The notice only
                # forces a status read-back before any write.
                base["recovery_notice"] = {
                    "kind": "SUSPECTED_INTERRUPTED",
                    "read_first_tool": "get_ontology_workflow_status",
                    "correction_tools": ["preview_stage_rollback", "reopen_stage_for_correction"],
                    "reason": (
                        f"{stage} 仍标记为执行中，但执行器证据显示疑似中断"
                        f"（{liveness.get('evidence')}）。先回读状态与检查点，确认没有活跃执行器，"
                        "再按本阶段推荐工具以同一输入续跑；"
                        "输入需要修正时先预览退回范围再退回。未回读前不得重复提交。"
                    ),
                }
        if stage_status == "FAILED":
            return {
                **base,
                "action": "REPAIR_THEN_RETRY_FAILED_STAGE",
                "recommended_tool": "retry_failed_stage",
                "allowed_write_tools": ["retry_failed_stage", *common_write_tools],
                "input_owner": "PLATFORM_OR_ENGINEERING_AGENT",
                "blocking": state.get("blocking"),
                "last_error": state.get("last_error"),
                "reason": (
                    "先根据门禁编号修正平台生成或工具执行输入，再恢复同一阶段；"
                    "不得把内部字段转问给业务用户，也不得原样重复失败载荷。"
                ),
            }

        if stage_status == "BLOCKED_HUMAN":
            if stage == "S3":
                recommended = "resolve_mapping_option"
                reason = "等待用户选择已展示的高影响业务建模方案。"
                allowed = list(BLOCKED_HUMAN_WRITE_TOOLS["S3"])
            elif stage == "S4":
                recommended = "resolve_competency_question_review"
                reason = (
                    "等待用户确认或退回本体、映射、规则和验收问题组成的完整联合设计。"
                    if self._joint_design_enabled(state)
                    else "等待用户确认、修改或退回已生成的业务问题清单。"
                )
                allowed = list(BLOCKED_HUMAN_WRITE_TOOLS["S4"])
            else:
                recommended = "get_ontology_workflow_status"
                reason = "存在人工阻塞，但该阶段没有开放任意技术字段填写入口。"
                allowed = []
            return {
                **base,
                "action": "WAIT_FOR_BUSINESS_DECISION",
                "competency_question_review": (
                    self._read_json(project_dir / "04-ontology-design/competency-question-review.json")
                    if stage == "S4" else None
                ),
                "recommended_tool": recommended,
                "allowed_write_tools": [*allowed, *common_write_tools],
                "input_owner": "HUMAN",
                "blocking": state.get("blocking"),
                "reason": reason,
            }

        if stage == "S3":
            rule_candidates_path = (
                project_dir / "02-semantic-recognition/business-rule-candidates.json"
            )
            rule_candidates = (
                self._read_json(rule_candidates_path) if rule_candidates_path.is_file() else []
            )
            production_rules = [
                item
                for item in rule_candidates
                if isinstance(item, dict)
                and str(item.get("status") or "").upper() in {"DATABASE_FACT", "DOCUMENT_EVIDENCE"}
                and not bool(item.get("review_required"))
            ]
            legacy_rule_ids = sorted(
                str(item.get("id") or "").strip()
                for item in production_rules
                if not str(item.get("formal_expression") or "").strip()
                or not list(item.get("premise_predicates") or [])
                or not str(item.get("conclusion_predicate") or "").strip()
                or not list(item.get("test_cases") or [])
            )
            legacy_rule_ids = [rule_id for rule_id in legacy_rule_ids if rule_id]
            if legacy_rule_ids:
                return {
                    **base,
                    "action": "REOPEN_S2_FOR_PRODUCTION_RULE_CONTRACT_UPGRADE",
                    "recommended_tool": "preview_stage_rollback",
                    "allowed_write_tools": [
                        "preview_stage_rollback",
                        "reopen_stage_for_correction",
                        "archive_ontology_project",
                    ],
                    "input_owner": "PLATFORM_MIGRATION",
                    "target_stage": "S2",
                    "legacy_rule_ids": legacy_rule_ids,
                    "preflight_required": False,
                    "preflight_tool": None,
                    "reason": (
                        "S3 生产推理门禁要求 S2 正式规则包含表达式、前提/结论谓词和测试用例；"
                        "当前继承规则仍是旧契约。必须先通过一次性预览令牌受控重开 S2，"
                        "由平台升级规则契约后再进入 S3，禁止在 S3 猜测或绕过。"
                    ),
                }

        stage_actions: dict[str, dict[str, Any]] = {
            "S0": {
                "action": "INGEST_DOCUMENT_EVIDENCE",
                "recommended_tool": (
                    "record_s0_scope_decision"
                    if str(state.get("intake_mode") or "").upper() == "DATABASE_ONLY"
                    else "start_document_ingestion_job"
                ),
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S0"]),
                "input_owner": "PLATFORM_TOOL_EXECUTOR",
                "source_artifacts": ["project.json", "00-document-evidence/cq-intake.json"],
            },
            "S1": {
                "action": "BUILD_DATA_UNDERSTANDING_FROM_REGISTERED_DATASETS",
                "recommended_tool": "record_data_understanding_from_datasets",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S1"]),
                "input_owner": "PLATFORM_TOOL_EXECUTOR",
                "source_artifacts": ["registered dataset catalog"],
            },
            "S2": {
                "action": "COMPILE_SEMANTIC_CANDIDATES_AND_RULES",
                "recommended_tool": "record_semantic_candidates",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S2"]),
                "input_owner": "ENGINEERING_AGENT",
                "instruction": "由业务来源确定规则含义；自动比较规则在condition_contract一次声明前提属性、比较算子和固定阈值/明确允许的参数。S3只绑定字段，不重新决定业务条件。缺少依据时保留缺口，不为继续阶段而发明条件。",
                "source_artifacts": [
                    "00-document-evidence/cq-intake.json",
                    "00-document-evidence/evidence-index.json",
                    "01-data-understanding/data-profile.json",
                ],
            },
            "S3": {
                "action": "PREPARE_MAPPING_AND_RUNTIME_REVIEW",
                "recommended_tool": "prepare_mapping_review",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S3"]),
                "mapping_skeleton_available": (project_dir / "01-data-understanding/schema-snapshot.json").is_file(),
                "business_preview": {
                    "read_tool": "get_business_preview", "start_tool": "start_business_preview",
                    "scope": "S3_BUSINESS_PREVIEW", "formal_state_changed": False,
                    "instruction": "business_query_plans 逐项保存后，用当前 payload_file 和独立验收用例试运行；根据具体诊断 patch 局部计划。刷新或中断先读取回执，相同输入失败只允许显式再试一次，不循环重跑。试运行通过仅说明这一项快照能力与用例符合约定，不替代完整 S3 预检或 S6/S7 验收。",
                },
                "input_responsibility": "先 get_stage_draft 恢复当前修订；无草稿时 generate_mapping_skeleton，按 get_stage_input_contract 的 derivation 合同显式绑定真实表/列/身份，再 compile_mapping_runtime。新增字段再次增量编译会保留已有查询与规则，禁止删除 runtime 重建。每个 CQ 回读原始问题的业务维度，先用 query_source_evidence(describe_table) 读取当前快照真实列名和存储类型，再用 query_source_evidence 的 query_plan 核验跨表/时间/聚合真实基线；普通分组使用 table/group_by，不能把截断样本当全量。查询参数读取 realtime_runtime 合同中的 execution_semantics，不猜注入语法或历史日期兜底。长 OBDA 用 patch_stage_submission 的 replace_text 唯一匹配局部编辑。逐个查询/规则保存，处理 open_items 后按最新 payload_file 预检与提交；confirmations/automatic_decisions 每批最多 3 条，不能内联重传整份映射。来源核验和静态编译均不代表 S6/S7 运行验收。",
                "input_owner": "ENGINEERING_AGENT",
                "source_artifacts": [
                    "02-semantic-recognition/ontology-candidates.yaml",
                    "02-semantic-recognition/business-rule-candidates.json",
                ],
            },
            "S4": {
                "action": "GENERATE_AND_PREFLIGHT_ONTOLOGY_DESIGN",
                "recommended_tool": "generate_ontology_design",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S4"]),
                "input_owner": "PLATFORM_COMPILER",
                "input_responsibility": "先准备有来源且引用已声明实体的 logical_axioms，S4 generation_request 预检通过后提交 token。CQ 由 S3 cq_bindings 编译；缺来源回 S3 补齐，禁止空生成或猜造公理。",
                "source_artifacts": [
                    "00-document-evidence/cq-intake.json",
                    "03-mapping-review/mapping.yaml",
                    "03-mapping-review/realtime-runtime.json",
                ],
            },
            "S5": {
                "action": "BUILD_ONTOLOGY_WITH_PROTEGE_AND_VERIFY",
                "recommended_tool": "start_managed_stage_execution",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S5"]),
                "input_owner": "PLATFORM_TOOL_EXECUTOR",
                "source_artifacts": ["04-ontology-design/ontology-design.yaml"],
            },
            "S6": {
                "action": "RUN_FULL_QUALITY_AND_REASONING_VALIDATION",
                "recommended_tool": "start_managed_stage_execution",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S6"]),
                "input_owner": "PLATFORM_TOOL_EXECUTOR",
                "source_artifacts": [
                    "05-ontology-build/ontology.owl",
                    "05-ontology-build/ontology.ttl",
                    "05-ontology-build/shapes.ttl",
                ],
            },
            "S7": {
                "action": "WAIT_FOR_RELEASE_DECISION",
                "recommended_tool": "publish_ontology_package",
                "allowed_write_tools": list(STAGE_WRITE_TOOLS["S7"]),
                "input_owner": "HUMAN",
                "source_artifacts": ["06-quality-validation/quality-summary.json"],
            },
        }
        directive = draft_recovery_action(stage_actions[stage], stage=stage, active=state.get("active_revision") or {}, project=project_dir, revision=state["revision"])
        if stage in {"S5", "S6"}:
            repository = Path(__file__).resolve().parents[2]
            available = (repository / "Makefile").is_file()
            directive = {
                **directive,
                "managed_execution": {
                    "available": available,
                    "kind": "PLATFORM_MCP_JOB",
                    "start_tool": "start_managed_stage_execution" if available else None,
                    "status_tool": "get_managed_stage_execution" if available else None,
                    "submission_protocol": (
                        "SERVICE_COMMIT_WITH_INTERNAL_GATES" if stage == "S5"
                        else "PREFLIGHT_TOKEN_COMMIT"
                    ),
                    "instruction": (
                        "从 3081 启动已绑定工程的受管任务，平台内部完成真实工具执行和正式提交；"
                        "已有任务时观察心跳，不重复启动，不展开凭据或重复提交。"
                        if available else
                        "本机受管任务入口不可用；按实际挂载工具执行，不猜测本机路径。"
                    ),
                },
            }
        if stage == "S3" and (state.get("s3_runtime_review") or {}).get("status") == "AWAITING_RUNTIME_COMPILATION":
            directive = {
                **directive,
                "action": "COMPILE_RUNTIME_FROM_BUSINESS_DECISIONS",
                "source_artifacts": [
                    *directive["source_artifacts"],
                    "03-mapping-review/mapping-draft.yaml",
                    "03-mapping-review/pending-confirmations.json",
                    "03-mapping-review/decisions.jsonl",
                ],
                "runtime_review": state["s3_runtime_review"],
            }
        if stage == "S1" and self._joint_design_enabled(state) and state.get("intake_mode") == "DOCUMENT_ONLY":
            directive = {
                "action": "REVALIDATE_DOCUMENT_UNDERSTANDING",
                "recommended_tool": "record_document_understanding",
                "allowed_write_tools": list(S1_DOCUMENT_ONLY_WRITE_TOOLS),
                "input_owner": "PLATFORM_COMPILER",
                "source_artifacts": ["00-document-evidence/document-register.json", "00-document-evidence/evidence-index.json"],
            }
        return {
            **base,
            **directive,
            "allowed_write_tools": [
                *directive["allowed_write_tools"],
                *common_write_tools,
            ],
            "preflight_required": stage in {"S1", "S2", "S3", "S4"} and directive["recommended_tool"] != "record_document_understanding",
            "preflight_tool": (
                "preflight_stage_submission" if stage in {"S1", "S2", "S3", "S4"} and directive["recommended_tool"] != "record_document_understanding" else None
            ),
            "reason": (
                "按真实业务决定准备完整运行设计并保留原业务卡，平台复用已确认项；若 S2 规则或测试需修改，先使用正式回退流程返回 S2。"
                if directive["action"] == "COMPILE_RUNTIME_FROM_BUSINESS_DECISIONS"
                else "动作由当前持久化阶段和正式上游产物确定；模型只补业务语义，技术契约由工具 Schema、平台编译器和阶段门禁负责。"
            ),
        }

    def verify_project_integrity(self, project_id: str | None = None) -> dict[str, Any]:
        """Recompute formal artifact fingerprints and the append-only event hash chain."""

        project_dir = self._resolve_project(project_id)
        return self._verify_project_integrity(project_dir, self._read_state(project_dir))

    def get_storage_status(self, project_id: str | None = None) -> dict[str, Any]:
        project_dir = self._resolve_project(project_id) if project_id is not None else None
        project_id = project_dir.name if project_dir is not None else None
        status_path = (
            project_dir / ".storage-sync-status.json"
            if project_dir is not None
            else self.root / ".storage-sync-status.json"
        )
        if not status_path.exists():
            local_status = {
                "status": "FILESYSTEM_ONLY",
                "sync_status": "MISSING",
                "project_id": project_id,
                "message": (
                    "当前工程文件可回读，但尚无该工程的 PostgreSQL/MinIO 同步回执。"
                    if project_id
                    else "尚无全局存储同步回执。"
                ),
            }
        else:
            local_status = self._read_json(status_path)
        if project_dir is not None and status_path.exists():
            receipt_project_id = str(local_status.get("project_id") or "").strip()
            if receipt_project_id != project_id:
                local_status = {
                    "status": "DEGRADED",
                    "sync_status": "PROJECT_MISMATCH",
                    "project_id": project_id,
                    "message": "项目级同步回执与当前工程不匹配，已拒绝借用其成功状态。",
                }
            else:
                workflow_updated_at = str(
                    self._read_state(project_dir).get("updated_at") or ""
                ).strip()
                receipt_checked_at = str(local_status.get("checked_at") or "").strip()
                try:
                    checked_at_value = datetime.fromisoformat(receipt_checked_at)
                    updated_at_value = datetime.fromisoformat(workflow_updated_at)
                    receipt_is_current = (
                        bool(workflow_updated_at and receipt_checked_at)
                        and checked_at_value.utcoffset() is not None
                        and updated_at_value.utcoffset() is not None
                        and checked_at_value >= updated_at_value
                    )
                except (TypeError, ValueError):
                    receipt_is_current = False
                if not receipt_is_current:
                    local_status = {
                        **local_status,
                        "status": "DEGRADED",
                        "sync_status": "STALE",
                        "project_id": project_id,
                        "workflow_updated_at": workflow_updated_at or None,
                        "message": "项目同步回执早于当前 workflow-state，持久化回读已过期。",
                    }
        pending_entries = self._pending_metadata_outbox_entries(project_id)
        if pending_entries:
            local_status = {
                **local_status,
                "status": "COMMITTED_SYNC_PENDING",
                "pending_count": len(pending_entries),
                "pending": pending_entries,
            }
        if self._metadata_store is None:
            return local_status
        try:
            read_back = self._metadata_store.status(project_id)
        except Exception as exc:
            return {
                **local_status,
                "status": ("COMMITTED_SYNC_PENDING" if pending_entries else "DEGRADED"),
                "error_type": type(exc).__name__,
            }
        local_is_current = local_status.get("status") == "SYNCED" and local_status.get(
            "sync_status"
        ) not in {"MISSING", "STALE", "PROJECT_MISMATCH"}
        return {
            **local_status,
            "status": (
                "COMMITTED_SYNC_PENDING"
                if pending_entries
                else "CONNECTED"
                if local_is_current
                else local_status.get("status", "DEGRADED")
            ),
            "read_back": read_back,
        }

    def reconcile_metadata_outbox(
        self,
        *,
        reconciled_by: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Replay durable metadata sync work and prove the event head by read-back."""

        if not reconciled_by.strip():
            raise WorkflowError("reconciled_by 不能为空。")
        if self._metadata_store is None:
            raise WorkflowError("尚未配置 PostgreSQL 工程账本，不能执行补同步。")
        # Every operation that needs both locks follows one order: root first,
        # then the project lock. Revision creation uses the same order.
        with self._lock, self._root_operation_lock():
            pending = self._pending_metadata_outbox_entries(project_id)
            if not pending:
                return {
                    "status": "SYNCED",
                    "pending_count": 0,
                    "results": [],
                    "idempotent_replay": True,
                    "reconciled_by": reconciled_by.strip(),
                }
            results: list[dict[str, Any]] = []
            for entry in pending:
                target_project_id = str(entry["project_id"])
                project_dir = self._resolve_project(target_project_id)
                with self._project_operation_lock(project_dir):
                    results.append(
                        self._attempt_metadata_sync(
                            project_dir,
                            reconciled_by=reconciled_by.strip(),
                        )
                    )
            remaining = self._pending_metadata_outbox_entries(project_id)
            return {
                "status": ("SYNCED" if not remaining else "COMMITTED_SYNC_PENDING"),
                "pending_count": len(remaining),
                "results": results,
                "idempotent_replay": False,
                "reconciled_by": reconciled_by.strip(),
            }

    def get_revision_history(self, project_id: str | None = None) -> dict[str, Any]:
        project_dir = self._resolve_project(project_id)
        revisions: list[dict[str, Any]] = []
        for revision_path in sorted(
            (project_dir / "revisions").glob("*/revision.json"), reverse=True
        ):
            revision = self._read_json(revision_path)
            diff_path = revision_path.with_name("diff.json")
            if diff_path.exists():
                revision["diff_summary"] = self._read_json(diff_path).get("summary", {})
            revision["report_path"] = (
                revision_path.parent.relative_to(project_dir) / "change-report.html"
            ).as_posix()
            revisions.append(revision)
        return {
            "project_id": project_dir.name,
            "count": len(revisions),
            "revisions": revisions,
        }

    def record_document_evidence(
        self,
        *,
        project_id: str,
        documents: list[dict[str, Any]],
        quality_report: dict[str, Any],
        evidence_index: list[dict[str, Any]],
        processing_trace: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """记录 S0 资料接入结果；资料建模项目随后留痕跳过 S1 并进入 S2。"""

        payload = {
            "documents": documents,
            "quality_report": quality_report,
            "evidence_index": evidence_index,
            "processing_trace": processing_trace,
        }
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S0")
            self._require_expected_revision(state, expected_revision)
            intake_mode = str(state.get("intake_mode") or "HYBRID")
            if intake_mode == "DATABASE_ONLY":
                raise WorkflowGateError(
                    "G-S0-SCOPE",
                    "DATABASE_ONLY 项目不执行 OCR；请调用 record_s0_scope_decision。",
                )
            self._reject_identical_failed_submission(state, "S0", payload)
            self._append_event(
                project_dir,
                "STAGE_EXECUTION_ATTEMPTED",
                state,
                {
                    "stage": "S0",
                    "input_fingerprint": _fingerprint(payload),
                    "document_count": len(documents),
                    "actor": processing_trace.get("actor") or "Harness / PaddleOCR MCP",
                },
            )
            try:
                metrics = self._validate_s0(payload)
                source_review = self._validate_source_scope(project_dir, stage="S0", payload=payload)
            except WorkflowGateError as exc:
                self._mark_failed(project_dir, state, "S0", exc, input_payload=payload)
                raise

            stage_dir = project_dir / "00-document-evidence"
            structured_dir = stage_dir / "structured-markdown"
            structured_dir.mkdir(parents=True, exist_ok=True)
            register: list[dict[str, Any]] = []
            regenerated_paths = {
                "00-document-evidence/README.md",
                "00-document-evidence/document-register.json",
                "00-document-evidence/ingestion-quality-report.json",
                "00-document-evidence/evidence-index.json",
                "00-document-evidence/processing-trace.json",
                "00-document-evidence/gate-results.json",
                "00-document-evidence/document-evidence-report.html",
            }
            used_markdown_names: set[str] = set()
            for item in documents:
                markdown_name = _structured_markdown_name(item, used_markdown_names)
                markdown_path = f"structured-markdown/{markdown_name}"
                self._atomic_write(stage_dir / markdown_path, str(item["structured_markdown"]))
                regenerated_paths.add(f"00-document-evidence/{markdown_path}")
                register.append(
                    {key: value for key, value in item.items() if key != "structured_markdown"}
                    | {"structured_markdown_path": markdown_path}
                )

            recorded_trace = {**processing_trace, "recorded_at": _now()}
            if source_review is not None:
                self._write_json(stage_dir / "source-scope-validation.json", source_review)
                regenerated_paths.add("00-document-evidence/source-scope-validation.json")
            self._write_json(stage_dir / "document-register.json", register)
            self._write_json(stage_dir / "ingestion-quality-report.json", quality_report)
            self._write_json(stage_dir / "evidence-index.json", evidence_index)
            self._write_json(stage_dir / "processing-trace.json", recorded_trace)
            gate_results = {
                "stage": "S0",
                "status": "PASSED",
                "gates": [
                    {"id": "G-S0-DOCUMENT-IDENTITY", "status": "PASSED"},
                    {"id": "G-S0-STRUCTURED-MARKDOWN", "status": "PASSED"},
                    {"id": "G-S0-EVIDENCE-TRACE", "status": "PASSED"},
                    {"id": "G-S0-QUALITY", "status": "PASSED"},
                    {"id": "G-S0-TOOL-TRACE", "status": "PASSED"},
                ],
                "metrics": metrics,
                "checked_at": _now(),
            }
            self._write_json(stage_dir / "gate-results.json", gate_results)
            self._atomic_write(
                stage_dir / "document-evidence-report.html",
                render_s0_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    register,
                    quality_report,
                    evidence_index,
                    recorded_trace,
                    gate_results,
                ),
            )
            if intake_mode == "DOCUMENT_ONLY":
                project = self._read_json(project_dir / "project.json")
                decision_actor = str(processing_trace.get("actor") or "Harness / 文档处理工具")
                s1_decision = {
                    "stage": "S1",
                    "status": "NOT_APPLICABLE",
                    "intake_mode": intake_mode,
                    "rationale": str(
                        state.get("intake_rationale")
                        or "本项目仅使用已在 S0 登记的文件资料进行本体建模，不连接业务数据库。"
                    ),
                    "decided_by": decision_actor,
                    "document_refs": [str(item["document_id"]) for item in register],
                    "source_stage": "S0",
                    "next_stage": "S2",
                    "decided_at": _now(),
                }
                s1_gate_results = {
                    "stage": "S1",
                    "status": "NOT_APPLICABLE",
                    "gates": [{"id": "G-S1-SCOPE", "status": "PASSED"}],
                    "metrics": {
                        "document_count": len(register),
                        "database_count": 0,
                        "next_stage": "S2",
                    },
                    "checked_at": _now(),
                }
                s1_dir = project_dir / "01-data-understanding"
                if self._joint_design_enabled(state):
                    s1_decision.update({
                        "status": "PASSED",
                        "database_task_status": "NOT_APPLICABLE",
                        "rationale": "已复核资料来源、定位证据和解析质量；本工程不连接数据库。",
                    })
                    s1_gate_results["status"] = "PASSED"
                    self._write_json(s1_dir / "source-understanding.json", {
                        "status": "PASSED",
                        "basis": "VALIDATED_S0_DOCUMENT_EVIDENCE",
                        "document_count": len(register),
                        "evidence_count": len(evidence_index),
                        "database_task_status": "NOT_APPLICABLE",
                        "source_artifacts": {name: _file_checksum(stage_dir / name) for name in (
                            "document-register.json", "evidence-index.json", "ingestion-quality-report.json",
                        )},
                    })
                    regenerated_paths.add("01-data-understanding/source-understanding.json")
                self._write_json(s1_dir / "scope-decision.json", s1_decision)
                self._write_json(s1_dir / "gate-results.json", s1_gate_results)
                self._atomic_write(
                    s1_dir / "data-understanding-report.html",
                    render_s1_scope_report(s1_dir, project, s1_decision, s1_gate_results),
                )
                regenerated_paths.update(
                    {
                        "01-data-understanding/scope-decision.json",
                        "01-data-understanding/README.md",
                        "01-data-understanding/gate-results.json",
                        "01-data-understanding/data-understanding-report.html",
                    }
                )
                state["stage_statuses"]["S0"] = "PASSED"
                state["stage_statuses"]["S1"] = s1_decision["status"]
                state["stage_statuses"]["S2"] = "RUNNING"
                state["current_stage"] = "S2"
                state["project_status"] = "IN_PROGRESS"
                state["blocking"] = None
                state["last_error"] = None
                state["stage_fingerprints"]["S0"] = {
                    "input": _fingerprint(payload),
                    "output": self._stage_fingerprint(stage_dir),
                    "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
                }
                state["stage_fingerprints"]["S1"] = {
                    "input": _fingerprint(s1_decision),
                    "output": self._stage_fingerprint(s1_dir),
                    "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
                }
                state["s1_disposition"] = s1_decision
                state["resume_point"] = (
                    "S1 已完成资料理解；S2: 从文件证据识别业务语义候选"
                    if self._joint_design_enabled(state)
                    else "S1 已按资料建模范围留痕跳过；S2: 从文件证据识别业务语义候选"
                )
            else:
                self._pass_stage(project_dir, state, "S0", "S1", payload)
                state["resume_point"] = "S1: 调用 Chat2DB 完成只读数据理解"
            self._mark_artifacts_regenerated(state, regenerated_paths)
            self._save_state(project_dir, state)
            if processing_trace.get("batch_job_id"):
                processing_methods: dict[str, int] = {}
                for item in register:
                    method = str(item.get("processing_method") or "未记录")
                    processing_methods[method] = processing_methods.get(method, 0) + 1
                self._append_event(
                    project_dir,
                    "DOCUMENT_BATCH_COMPLETED",
                    state,
                    {
                        "stage": "S0",
                        "batch_job_id": processing_trace.get("batch_job_id"),
                        "document_count": len(documents),
                        "evidence_count": len(evidence_index),
                        "processing_methods": processing_methods,
                        "tool_calls": processing_trace.get("tool_calls") or [],
                        "actor": processing_trace.get("actor") or "Harness / 文档处理工具",
                    },
                )
            self._append_event(
                project_dir,
                "DOCUMENT_EVIDENCE_RECORDED",
                state,
                {"stage": "S0", **metrics, "run_id": processing_trace.get("run_id")},
            )
            self._append_event(project_dir, "STAGE_PASSED", state, {"stage": "S0"})
            if intake_mode == "DOCUMENT_ONLY":
                self._append_event(
                    project_dir,
                    "S1_SCOPE_DECISION_RECORDED",
                    state,
                    {
                        "stage": "S1",
                        "status": "NOT_APPLICABLE",
                        "next_stage": "S2",
                        "document_count": len(documents),
                        "evidence_count": len(evidence_index),
                        "rationale": state["s1_disposition"]["rationale"],
                        "actor": state["s1_disposition"]["decided_by"],
                    },
                )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def amend_competency_questions(
        self, *, project_id: str, questions: list[dict[str, Any]],
        expected_revision: int, actor: str, reason: str,
    ) -> dict[str, Any]:
        """Record an explicit replacement CQ baseline before S2 submission.

        Sources and passed S0/S1 evidence remain intact. Later stages must use
        the formal reopen path first; this operation never rolls a stage back.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise WorkflowError("expected_revision 必须为当前工程的正整数修订号。")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("actor 和 reason 必须说明需求变更的责任人与原因。")
        if not isinstance(questions, list) or not questions:
            raise WorkflowError("questions 必须是完整且非空的 CQ 列表。")
        if any(not isinstance(q, dict) or not isinstance(q.get("id"), str) or not q["id"].strip() for q in questions):
            raise WorkflowError("每个 CQ 必须具有明确的非空 id；不得自动生成替代原编号。")
        normalized = self._normalize_initial_competency_questions(questions)
        with self._project_mutation_lock(project_id) as project_dir:
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            if (state.get("project_status") == "PUBLISHED"
                or state.get("current_stage") != "S2"
                or state.get("stage_statuses", {}).get("S2") != "RUNNING"
                or any(state.get("stage_statuses", {}).get(s) != "PASSED" for s in ("S0", "S1"))
                or state.get("stage_fingerprints", {}).get("S2")):
                raise WorkflowGateError("G-CQ-AMENDMENT-STAGE", "CQ 变更仅允许 S0/S1 已通过且 S2 RUNNING、尚未正式提交 S2 的非发布工程；后续阶段请先正式重开 S2，已发布工程请创建修订。")
            intake_path = project_dir / "00-document-evidence/cq-intake.json"
            before = self._read_json(intake_path)
            project = self._read_json(project_dir / "project.json")
            if project.get("initial_competency_question_count") != len(before.get("questions", [])):
                raise WorkflowGateError("G-CQ-AMENDMENT-INTEGRITY", "工程需求与 CQ 登记不一致，需先核实基线。")
            if normalized == before.get("questions"):
                return {**self._status_payload(project_dir, state), "cq_amendment": {"status": "UNCHANGED", "question_count": len(normalized)}}
            s0_fingerprint = state.get("stage_fingerprints", {}).get("S0") or {}
            if s0_fingerprint.get("output") != self._stage_fingerprint(intake_path.parent):
                raise WorkflowGateError("G-CQ-AMENDMENT-INTEGRITY", "S0 正式产物指纹已变化，不能将未知变更纳入需求修订。")
            audit_dir = project_dir / "revisions/cq-amendments" / f"revision-{expected_revision + 1}"
            if audit_dir.exists():
                raise WorkflowError("该版本 CQ 变更审计已存在，请核实上次写入结果。")
            old_by_id = {q["id"]: q for q in before.get("questions", [])}
            new_by_id = {q["id"]: q for q in normalized}
            receipt = {
                "schema_version": 1, "status": "AMENDED", "project_id": project_id,
                "previous_revision": expected_revision, "revision": expected_revision + 1,
                "actor": actor.strip(), "reason": reason.strip(), "amended_at": _now(),
                "previous_s0_fingerprint": dict(s0_fingerprint),
                "previous_questions_sha256": _fingerprint(before.get("questions", [])),
                "questions_sha256": _fingerprint(normalized), "question_count": len(normalized),
                "added_ids": sorted(new_by_id.keys() - old_by_id.keys()),
                "removed_ids": sorted(old_by_id.keys() - new_by_id.keys()),
                "modified_ids": sorted(k for k in old_by_id.keys() & new_by_id.keys() if old_by_id[k] != new_by_id[k]),
                "required_revalidation_stages": list(STAGES[2:]),
                "audit_path": (audit_dir / "amendment.json").relative_to(project_dir).as_posix(),
            }
            # Preserve local transaction inputs. External metadata synchronization
            # follows the normal manifest/outbox path only after local commit.
            transaction_paths = [intake_path, project_dir / "project.json", project_dir / "workflow-state.json",
                                 project_dir / "events/agent-trace.jsonl"]
            original_bytes = {path: path.read_bytes() if path.exists() else None for path in transaction_paths}
            try:
                self._atomic_write(audit_dir / "cq-intake-before.json", intake_path.read_text(encoding="utf-8"))
                revised = {**before, "questions": normalized, "question_count": len(normalized),
                           "questions_sha256": receipt["questions_sha256"], "updated_at": receipt["amended_at"],
                           "amendment_path": receipt["audit_path"]}
                self._write_json(intake_path, revised)
                project["initial_competency_question_count"] = len(normalized)
                self._write_json(project_dir / "project.json", project)
                self._write_json(audit_dir / "amendment.json", receipt)
                state["initial_competency_question_count"] = len(normalized)
                state["stage_fingerprints"]["S0"] = {**s0_fingerprint, "output": self._stage_fingerprint(intake_path.parent)}
                state["cq_baseline_sha256"] = receipt["questions_sha256"]
                self._save_state(project_dir, state)
                self._append_event(project_dir, "COMPETENCY_QUESTIONS_AMENDED", state, {"stage": "S2", **receipt})
            except Exception:
                for path, content in original_bytes.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._atomic_write(path, content.decode("utf-8"))
                if audit_dir.exists():
                    shutil.rmtree(audit_dir)
                raise
            self._refresh_manifest(project_dir)
            return {**self._status_payload(project_dir, state), "cq_amendment": receipt}

    def replace_document_sources(
        self, *, project_id: str, source_path: str, expected_revision: int,
        actor: str, reason: str,
    ) -> dict[str, Any]:
        """Replace the complete S0 document selection through verified receipts."""
        from .source_changes import replace_document_sources
        return replace_document_sources(
            self, project_id=project_id, source_path=source_path,
            expected_revision=expected_revision, actor=actor, reason=reason,
        )

    def reconcile_document_source_identities(
        self, *, project_id: str, source_path: str, expected_revision: int,
        actor: str, reason: str,
    ) -> dict[str, Any]:
        """Correct invented file IDs without changing a verified S0 selection."""
        from services.ingestion.document_jobs import (
            document_input_root,
            document_source_manifest,
            document_source_scope,
            read_document_job,
            verified_document_source,
        )

        if not actor.strip() or not reason.strip():
            raise WorkflowError("actor 和 reason 必须说明来源编号修复的责任人与原因。")
        if type(expected_revision) is not int or expected_revision < 1:
            raise WorkflowError("expected_revision 必须为当前工程的正整数修订号。")
        with self._project_mutation_lock(project_id) as project_dir:
            _, state = self._require_stage(project_id, "S0")
            self._require_expected_revision(state, expected_revision)
            stage_dir = project_dir / "00-document-evidence"
            if not self._joint_design_enabled(state) or state.get("intake_mode") == "DATABASE_ONLY":
                raise WorkflowGateError("G-S0-SOURCE-SCOPE", "仅新版文件工程可修复尚未接入的来源编号。")
            if state.get("stage_fingerprints", {}).get("S0") or any(
                (stage_dir / name).exists()
                for name in ("document-register.json", "processing-trace.json", "scope-decision.json", "source-scope-validation.json")
            ) or any(
                event.get("event_type") in {"DOCUMENT_EVIDENCE_RECORDED", "S0_SCOPE_DECISION_RECORDED"}
                or (event.get("event_type") == "STAGE_PASSED" and event.get("details", {}).get("stage") == "S0")
                for event in self._read_events(project_dir)
            ):
                raise WorkflowGateError("G-S0-SOURCE-SCOPE", "S0 已有提交记录或解析产物，不能原地修复创建来源编号。")
            root = document_input_root()
            job_ids = set()
            marker = project_dir / ".s0-document-job.json"
            if marker.exists():
                job_ids.add(str(self._read_json(marker).get("job_id") or ""))
            for request_path in (root / ".orion-s0-jobs").glob("JOB-*/request.json"):
                request = self._read_json(request_path)
                if request.get("project_id") == project_id:
                    job_ids.add(request_path.parent.name)
            for job_id in job_ids:
                job = read_document_job(project_id=project_id, job_id=job_id, root=root)
                if job.get("status") not in {"FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"} or job.get("runner_state") in {"QUEUED", "STARTING", "RUNNING"}:
                    raise WorkflowGateError("G-S0-SOURCE-SCOPE", "存在活动或待复核资料任务，不能变更其来源声明。")
            scope_path = stage_dir / "source-scope.json"
            before_bytes = scope_path.read_bytes()
            before = json.loads(before_bytes)
            project = self._read_json(project_dir / "project.json")
            if project.get("source_scope") != before:
                raise WorkflowGateError("G-S0-SOURCE-SCOPE", "project.json 与 S0 来源声明不一致，不能据此修复编号。")
            snapshot = verified_document_source(root, source_path)
            try:
                revised, differences = reconcile_file_source_ids(
                    before, document_source_scope(snapshot, source_path=source_path)["sources"]
                )
            except SourceScopeError as exc:
                raise WorkflowGateError(exc.gate, str(exc)) from exc
            # Verify the same complete batch again immediately before committing.
            if document_source_manifest(verified_document_source(root, source_path)) != document_source_manifest(snapshot):
                raise WorkflowGateError("G-S0-SOURCE-SCOPE", "修复期间资料批次已变化，未写入来源声明。")
            audit_dir = stage_dir / "source-identity-reconciliations" / f"revision-{expected_revision + 1}"
            if audit_dir.exists():
                raise WorkflowError("该修订的来源编号修复记录已存在，必须先核对工程审计。")
            receipt = {
                "schema_version": 1, "project_id": project_id,
                "operation": "RECONCILE_DOCUMENT_SOURCE_IDENTITIES",
                "status": "RECONCILED", "stage_status": "RUNNING",
                "previous_revision": expected_revision, "revision": expected_revision + 1,
                "actor": actor.strip(), "reason": reason.strip(), "reconciled_at": _now(),
                "source_path": source_path, "source_snapshot": document_source_manifest(snapshot),
                "before_scope_sha256": "sha256:" + hashlib.sha256(before_bytes).hexdigest(),
                "changes": differences, "authorization_scope_changed": False,
                "identity_basis": "EXACT_PATH_SHA256_NAME_KIND",
                "previous_scope_path": (audit_dir / "source-scope-before.json").relative_to(project_dir).as_posix(),
            }
            self._atomic_write(audit_dir / "source-scope-before.json", before_bytes.decode("utf-8"))
            self._write_json(scope_path, revised)
            receipt["source_scope_sha256"] = _file_checksum(scope_path)
            self._write_json(audit_dir / "reconciliation.json", receipt)
            project["source_scope"] = revised
            self._write_json(project_dir / "project.json", project)
            self._atomic_write(
                stage_dir / "document-evidence-report.html",
                self._render_stage_report(
                    title="S0 目标与来源登记报告", subtitle=project["project_name"],
                    metrics=[("阶段", "S0 待处理"), ("来源文件", len(revised["sources"])), ("编号修复", len(differences))],
                    sections='<section><h2>来源编号已核对</h2><p>仅移除与真实文件回执不一致的技术编号；原有路径、内容哈希、名称、类型和范围保持不变。资料解析与质量门禁尚未执行，S0 未通过。</p>'
                    '<p><a href="source-scope.json">当前来源范围</a> · '
                    f'<a href="source-identity-reconciliations/revision-{expected_revision + 1}/reconciliation.json">修复依据与差异</a></p>'
                    f'<p>{html.escape(reason.strip())}</p></section>',
                ),
            )
            self._save_state(project_dir, state)
            self._append_event(project_dir, "DOCUMENT_SOURCE_IDENTITIES_RECONCILED", state, {
                "stage": "S0", "actor": actor.strip(), "reason": reason.strip(),
                "receipt_path": (audit_dir / "reconciliation.json").relative_to(project_dir).as_posix(),
                "manifest_sha256": snapshot["manifest_sha256"], "changes": differences,
                "authorization_scope_changed": False,
            })
            self._refresh_manifest(project_dir)
            return {**self._status_payload(project_dir, state), "source_identity_reconciliation": receipt}

    def record_s0_scope_decision(
        self,
        *,
        project_id: str,
        intake_mode: str,
        rationale: str,
        decided_by: str,
        datasource_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """为纯数据库项目正式记录 S0 不适用，而不是静默跳过。"""

        if intake_mode != "DATABASE_ONLY":
            raise WorkflowGateError(
                "G-S0-SCOPE",
                "无文档分支当前只允许 intake_mode=DATABASE_ONLY。",
            )
        if not rationale.strip():
            raise WorkflowGateError("G-S0-SCOPE", "rationale 必须说明为何没有文档资料。")
        if not decided_by.strip():
            raise WorkflowGateError("G-S0-SCOPE", "decided_by 不能为空。")
        refs = list(
            dict.fromkeys(
                str(item).strip() for item in (datasource_refs or []) if str(item).strip()
            )
        )
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S0")
            project_intake_mode = str(state.get("intake_mode") or "HYBRID")
            if project_intake_mode != "DATABASE_ONLY":
                raise WorkflowGateError(
                    "G-S0-SCOPE",
                    "只有创建为 DATABASE_ONLY 的项目才能把 S0 判定为不适用。",
                )
            stage_dir = project_dir / "00-document-evidence"
            decision = {
                "stage": "S0",
                "status": "NOT_APPLICABLE",
                "intake_mode": intake_mode,
                "rationale": rationale.strip(),
                "decided_by": decided_by.strip(),
                "datasource_refs": refs,
                "decided_at": _now(),
            }
            if self._joint_design_enabled(state):
                scope = self._read_json(stage_dir / "source-scope.json")
                known_refs = {
                    str(source.get(key))
                    for source in scope.get("sources") or []
                    for key in ("source_id", "database", "datasource_label") if source.get(key)
                }
                if scope.get("declaration") == "EXPLICIT" and (not known_refs or set(refs) - known_refs):
                    raise WorkflowGateError("G-S0-SOURCE-SCOPE", "数据库引用必须来自 S0 声明的来源范围。")
                if not refs:
                    refs = sorted(known_refs) or [str(scope.get("legacy_hints", {}).get("datasource_label") or "").strip()]
                    refs = [ref for ref in refs if ref]
                if not refs:
                    raise WorkflowGateError("G-S0-SOURCE-SCOPE", "需要明确本工程使用的数据库来源。")
                decision.update({"status": "PASSED", "document_task_status": "NOT_APPLICABLE", "datasource_refs": refs})
            gate_results = {
                "stage": "S0",
                "status": decision["status"],
                "gates": [{"id": "G-S0-SCOPE", "status": "PASSED"}],
                "metrics": {
                    "document_count": 0,
                    "datasource_ref_count": len(refs),
                    "intake_mode": intake_mode,
                },
                "checked_at": _now(),
            }
            self._write_json(stage_dir / "scope-decision.json", decision)
            self._write_json(stage_dir / "gate-results.json", gate_results)
            self._atomic_write(
                stage_dir / "document-evidence-report.html",
                render_s0_scope_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    decision,
                    gate_results,
                ),
            )
            state["stage_statuses"]["S0"] = decision["status"]
            state["stage_statuses"]["S1"] = "RUNNING"
            state["current_stage"] = "S1"
            state["project_status"] = "IN_PROGRESS"
            state["blocking"] = None
            state["last_error"] = None
            state["s0_disposition"] = decision
            state["stage_fingerprints"]["S0"] = {
                "input": _fingerprint(decision),
                "output": self._stage_fingerprint(stage_dir),
                "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
            }
            state["resume_point"] = "S1: 调用 Chat2DB 完成只读数据理解"
            self._mark_artifacts_regenerated(
                state,
                {
                    "00-document-evidence/scope-decision.json",
                    "00-document-evidence/README.md",
                    "00-document-evidence/gate-results.json",
                    "00-document-evidence/document-evidence-report.html",
                },
            )
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "S0_SCOPE_DECISION_RECORDED",
                state,
                {
                    "stage": "S0",
                    "status": decision["status"],
                    "intake_mode": intake_mode,
                    "rationale": rationale.strip(),
                    "datasource_refs": refs,
                    "actor": decided_by.strip(),
                },
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def record_document_understanding(
        self, *, project_id: str, expected_revision: int,
    ) -> dict[str, Any]:
        """Revalidate v2 S1 from immutable S0 evidence after a scoped correction."""
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S1")
            self._require_expected_revision(state, expected_revision)
            if not self._joint_design_enabled(state) or state.get("intake_mode") != "DOCUMENT_ONLY":
                raise WorkflowGateError("G-S1-SCOPE", "资料理解重验仅适用于 v2 纯资料工程。")
            s0_dir = project_dir / "00-document-evidence"
            if state["stage_statuses"].get("S0") != "PASSED" or self._stage_fingerprint(s0_dir) != state["stage_fingerprints"]["S0"]["output"]:
                raise WorkflowGateError("G-S1-SOURCE-SCOPE", "S0 资料证据未通过或已改变。")
            register = self._read_json(s0_dir / "document-register.json")
            evidence = self._read_json(s0_dir / "evidence-index.json")
            stage_dir = project_dir / "01-data-understanding"
            decision = {
                "stage": "S1", "status": "PASSED", "intake_mode": "DOCUMENT_ONLY",
                "database_task_status": "NOT_APPLICABLE", "decided_by": "ORION_WORKFLOW",
                "decided_at": _now(), "document_refs": [item["document_id"] for item in register],
                "rationale": "依据已验证的 S0 来源、证据定位和解析质量完成资料理解。",
            }
            understanding = {
                "status": "PASSED", "basis": "VALIDATED_S0_DOCUMENT_EVIDENCE",
                "document_count": len(register), "evidence_count": len(evidence),
                "database_task_status": "NOT_APPLICABLE",
                "source_artifacts": {name: _file_checksum(s0_dir / name) for name in (
                    "document-register.json", "evidence-index.json", "ingestion-quality-report.json",
                )},
            }
            gates = {"stage": "S1", "status": "PASSED", "gates": [{"id": "G-S1-DOCUMENT-EVIDENCE", "status": "PASSED"}]}
            for name, value in (("scope-decision.json", decision), ("source-understanding.json", understanding), ("gate-results.json", gates)):
                self._write_json(stage_dir / name, value)
            self._atomic_write(stage_dir / "data-understanding-report.html", render_s1_scope_report(stage_dir, self._read_json(project_dir / "project.json"), decision, gates))
            self._mark_artifacts_regenerated(state, {f"01-data-understanding/{name}" for name in (
                "scope-decision.json", "source-understanding.json", "gate-results.json", "data-understanding-report.html", "README.md",
            )})
            self._pass_stage(project_dir, state, "S1", "S2", understanding)
            state["s1_disposition"] = decision
            self._save_state(project_dir, state)
            self._append_event(project_dir, "STAGE_PASSED", state, {"stage": "S1", "basis": "DOCUMENT_EVIDENCE"})
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def record_data_understanding(
        self,
        *,
        project_id: str,
        datasource_inventory: dict[str, Any],
        schema_snapshot: dict[str, Any] | list[dict[str, Any]],
        data_profile: dict[str, Any],
        relation_candidates: list[dict[str, Any]],
        evidence_sql: list[dict[str, Any]],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        normalized_schema_snapshot = self._normalize_s1_schema_snapshot(schema_snapshot)
        payload = {
            "datasource_inventory": datasource_inventory,
            "schema_snapshot": normalized_schema_snapshot,
            "data_profile": data_profile,
            "relation_candidates": relation_candidates,
            "evidence_sql": evidence_sql,
        }
        with self._project_mutation_lock(project_id) as project_dir:
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            project_dir, state = self._require_stage(project_id, "S1")
            self._reject_identical_failed_submission(state, "S1", payload)
            try:
                self._validate_s1(payload, project_id=project_id)
                source_review = self._validate_source_scope(project_dir, stage="S1", payload=payload)
            except WorkflowGateError as exc:
                self._mark_failed(project_dir, state, "S1", exc, input_payload=payload)
                raise

            stage_dir = project_dir / "01-data-understanding"
            if source_review is not None:
                self._write_json(stage_dir / "source-understanding.json", source_review)
                self._mark_artifacts_regenerated(state, {"01-data-understanding/source-understanding.json"})
            self._write_json(stage_dir / "datasource-inventory.json", datasource_inventory)
            self._write_json(stage_dir / "schema-snapshot.json", normalized_schema_snapshot)
            self._write_json(stage_dir / "data-profile.json", data_profile)
            self._write_json(stage_dir / "relation-candidates.json", relation_candidates)
            self._write_json(stage_dir / "evidence-sql.json", evidence_sql)
            project_path = project_dir / "project.json"
            project = self._read_json(project_path)
            proposed_scope = list(project.get("table_scope") or [])
            observed_scope = list(
                datasource_inventory.get("business_tables_scope")
                or datasource_inventory.get("tables_in_scope")
                or []
            )
            if observed_scope and observed_scope != proposed_scope:
                scope_revision = {
                    "stage": "S1",
                    "reason": "以当前只读 Schema 回读结果修正项目初始范围",
                    "before": proposed_scope,
                    "after": observed_scope,
                    "added": sorted(set(observed_scope) - set(proposed_scope)),
                    "removed": sorted(set(proposed_scope) - set(observed_scope)),
                    "observed_at": data_profile.get("profiled_at") or _now(),
                }
                project["table_scope"] = observed_scope
                project.setdefault("scope_revisions", []).append(scope_revision)
                self._write_json(project_path, project)
                self._write_json(stage_dir / "scope-reconciliation.json", scope_revision)
                self._append_event(
                    project_dir,
                    "S1_SCOPE_RECONCILED",
                    state,
                    {
                        "stage": "S1",
                        "added_count": len(scope_revision["added"]),
                        "removed_count": len(scope_revision["removed"]),
                        "reason": scope_revision["reason"],
                        "actor": "ORION_WORKFLOW",
                    },
                )
            else:
                # A previous S1 run may have needed scope reconciliation.  Once a
                # corrected rerun already matches the project scope, that optional
                # artifact must no longer remain current (the revision snapshot
                # still preserves its historical bytes).
                (stage_dir / "scope-reconciliation.json").unlink(missing_ok=True)
            self._write_json(
                stage_dir / "gate-results.json",
                {
                    "stage": "S1",
                    "status": "PASSED",
                    "gates": [
                        {"id": "G-S1-REQUIRED", "status": "PASSED"},
                        {"id": "G-S1-READONLY", "status": "PASSED"},
                        {"id": "G-S1-EVIDENCE", "status": "PASSED"},
                        {"id": "G-S1-PRODUCTION-PROFILE", "status": "PASSED"},
                        {"id": "G-S1-RECONCILIATION", "status": "PASSED"},
                        {"id": "G-S1-SOURCE-LINEAGE", "status": "PASSED"},
                        *(
                            [{"id": "G-S1-CATALOG-READBACK", "status": "PASSED"}]
                            if self._metadata_required
                            or os.getenv("ORION_S1_CATALOG_READBACK_REQUIRED", "false").lower()
                            in {"1", "true", "yes"}
                            else []
                        ),
                        {"id": "G-S1-EVIDENCE-EXECUTION", "status": "PASSED"},
                    ]
                    + (
                        [
                            {"id": "G-S1-MULTI-SOURCE-INVENTORY", "status": "PASSED"},
                            {"id": "G-S1-SOURCE-READONLY", "status": "PASSED"},
                            {"id": "G-S1-SOURCE-SCOPE", "status": "PASSED"},
                            {"id": "G-S1-SNAPSHOT-COMPLETE", "status": "PASSED"},
                            {"id": "G-S1-SNAPSHOT-RECONCILIATION", "status": "PASSED"},
                            {
                                "id": "G-S1-CROSS-SOURCE-TIME-CONSISTENCY",
                                "status": "PASSED",
                            },
                        ]
                        if int(datasource_inventory.get("datasource_count") or 0) > 1
                        else []
                    ),
                    "checked_at": _now(),
                },
            )
            self._atomic_write(
                stage_dir / "data-understanding-report.html",
                render_s1_report(
                    stage_dir,
                    project,
                    payload,
                ),
            )
            regenerated_paths = {
                "01-data-understanding/README.md",
                "01-data-understanding/datasource-inventory.json",
                "01-data-understanding/schema-snapshot.json",
                "01-data-understanding/data-profile.json",
                "01-data-understanding/relation-candidates.json",
                "01-data-understanding/evidence-sql.json",
                "01-data-understanding/gate-results.json",
                "01-data-understanding/data-understanding-report.html",
                "01-data-understanding/scope-reconciliation.json",
            }
            self._mark_artifacts_regenerated(state, regenerated_paths)
            self._pass_stage(project_dir, state, "S1", "S2", payload)
            state["resume_point"] = "S2: 生成业务对象、属性、关系和规则候选"
            self._save_state(project_dir, state)
            self._append_event(project_dir, "STAGE_PASSED", state, {"stage": "S1"})
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def record_data_understanding_from_datasets(
        self,
        *,
        project_id: str,
        dataset_ids: list[str],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Build and submit S1 from catalog truth instead of model-transcribed JSON."""

        from services.structured_data import StructuredDataPipeline

        reader_url = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
        if not reader_url:
            raise WorkflowError("ORION_SOURCE_DATA_READER_URL 未配置；不能从目录回读生产 S1。")
        project_dir = self._resolve_project(project_id)
        with self._project_operation_lock(project_dir):
            handoff = StructuredDataPipeline(reader_url).build_handoff_for_datasets(
                project_id=project_id,
                dataset_ids=dataset_ids,
            )
        s1 = handoff["s1"]
        # Compiler/input errors are preflight failures, not formal stage failures.
        # Keep the existing gate intact, but do not mutate the project on a bad
        # platform-generated handoff before formal submission starts.
        self._validate_s1(s1, project_id=project_id)
        self._validate_source_scope(project_dir, stage="S1", payload=s1)
        return self.record_data_understanding(
            project_id=project_id,
            datasource_inventory=s1["datasource_inventory"],
            schema_snapshot=s1["schema_snapshot"],
            data_profile=s1["data_profile"],
            relation_candidates=s1["relation_candidates"],
            evidence_sql=s1["evidence_sql"],
            expected_revision=expected_revision,
        )

    def query_source_evidence(
        self, *, project_id: str, expected_revision: int, table: str | None = None,
        group_by: list[str] | None = None, equals: dict[str, Any] | None = None,
        max_groups: int = 100, query_plan: dict[str, Any] | None = None, describe_table: str | None = None,
    ) -> dict[str, Any]:
        """Read approved S1 source versions without reopening or rewriting a stage."""
        from services.structured_data.pipeline import StructuredDataImportError
        from services.structured_data.source_evidence import query_source_evidence_counts

        if describe_table is not None and (query_plan is not None or table is not None or group_by is not None or equals is not None or max_groups != 100):
            raise WorkflowGateError("G-SOURCE-EVIDENCE", "describe_table 与查询参数不能混用。")
        if query_plan is not None and (table is not None or group_by is not None or equals is not None or max_groups != 100):
            raise WorkflowGateError("G-SOURCE-EVIDENCE", "query_plan 与 table/group_by/equals/max_groups 不能混用。")
        project_dir = self._resolve_project(project_id)
        with self._project_operation_lock(project_dir):
            state = self._read_state(project_dir)
            if type(expected_revision) is not int:
                raise WorkflowGateError("G-SOURCE-EVIDENCE", "必须提供当前工程的整数 revision。")
            self._require_expected_revision(state, expected_revision)
            if (state.get("current_stage") not in {"S2", "S3", "S4", "S5", "S6", "S7"}
                    or state.get("stage_statuses", {}).get("S1") != "PASSED"):
                raise WorkflowGateError("G-SOURCE-EVIDENCE", "来源补查仅限 S1 已通过的 S2–S7 工程。")
            s1_dir = project_dir / "01-data-understanding"
            expected_hash = state.get("stage_fingerprints", {}).get("S1", {}).get("output")
            if not expected_hash or self._stage_fingerprint(s1_dir) != expected_hash:
                raise WorkflowGateError("G-SOURCE-EVIDENCE", "S1 正式来源资产指纹不一致，不能据此授权补查。")
            names = ("datasource-inventory", "schema-snapshot", "data-profile")
            for name in names:
                entry = state.get("artifact_lifecycle", {}).get(f"01-data-understanding/{name}.json", {})
                if entry.get("status") == "INVALIDATED":
                    raise WorkflowGateError("G-SOURCE-EVIDENCE", "S1 来源资产已失效，不能执行补查。")
            s1 = {name.replace("-", "_"): self._read_json(s1_dir / f"{name}.json") for name in names}
            self._validate_source_scope(project_dir, stage="S1", payload=s1)
            try:
                context = dict(reader_url=str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip(),
                               project_id=project_id, schema_snapshot=s1["schema_snapshot"],
                               datasource_inventory=s1["datasource_inventory"])
                if describe_table is not None:
                    from services.structured_data.source_metadata import describe_snapshot_source
                    result = describe_snapshot_source(**context, describe_table=describe_table)
                elif query_plan is not None:
                    from services.structured_data.source_query_plan import query_source_plan
                    result = query_source_plan(**context, query_plan=query_plan)
                else:
                    result = query_source_evidence_counts(**context, table=table,
                               group_by=group_by, equals=equals, max_groups=max_groups)
            except StructuredDataImportError as exc:
                raise WorkflowGateError("G-SOURCE-EVIDENCE", str(exc)) from exc
            result.update({"revision": state["revision"], "current_stage": state["current_stage"],
                           "s1_fingerprint": expected_hash})
            return result

    _normalize_s1_columns = staticmethod(normalize_s1_columns)
    _normalize_s1_schema_snapshot = staticmethod(normalize_s1_schema_snapshot)
    _classify_capability_question = staticmethod(capability_planning.classify_capability_question)
    _require_ready_capability_plan = staticmethod(capability_planning.require_ready_capability_plan)

    def _build_capability_plan(
        self,
        project_dir: Path,
        *,
        intake_mode: str,
        business_rules: list[dict[str, Any]],
        semantic_review: dict[str, Any] | None = None,
        cq_semantic_assessments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        intake_path = project_dir / "00-document-evidence/cq-intake.json"
        questions = (
            self._read_json(intake_path).get("questions") or [] if intake_path.is_file() else []
        )
        available_sources = set()
        if intake_mode in {"DATABASE_ONLY", "HYBRID"} and all(
            (project_dir / relative).is_file()
            for relative in (
                "01-data-understanding/schema-snapshot.json",
                "01-data-understanding/data-profile.json",
            )
        ):
            available_sources.add("VERSIONED_READ_ONLY_DATASET")
        if intake_mode in {"DOCUMENT_ONLY", "HYBRID"} and (
            project_dir / "00-document-evidence/evidence-index.json"
        ).is_file():
            available_sources.add("VERSIONED_DOCUMENT_EVIDENCE")
        catalog = platform_capability_catalog()
        return {
            **capability_planning.build_capability_plan(
                questions=questions,
                intake_mode=intake_mode,
                business_rules=business_rules,
                catalog=catalog,
                available_sources=available_sources,
                semantic_review=semantic_review,
                cq_semantic_assessments=cq_semantic_assessments,
            ),
            "project_id": project_dir.name,
            "capability_catalog_sha256": _fingerprint(catalog),
            "generated_at": _now(),
        }

    def record_semantic_candidates(
        self,
        *,
        project_id: str,
        ontology_candidates: list[dict[str, Any]],
        business_rule_candidates: list[dict[str, Any]] | None = None,
        cq_semantic_assessments: list[dict[str, Any]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "ontology_candidates": ontology_candidates,
            "business_rule_candidates": business_rule_candidates or [],
        }
        if cq_semantic_assessments is not None:
            payload["cq_semantic_assessments"] = cq_semantic_assessments
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S2")
            self._require_expected_revision(state, expected_revision)
            self._reject_identical_failed_submission(state, "S2", payload)
            try:
                self._validate_s2(
                    payload,
                    project_dir=project_dir,
                    intake_mode=str(state.get("intake_mode") or "HYBRID"),
                )
            except WorkflowGateError as exc:
                self._mark_failed(project_dir, state, "S2", exc, input_payload=payload)
                raise

            semantic_review = self._cq_semantic_review(project_dir, payload)
            capability_plan = self._build_capability_plan(
                project_dir,
                intake_mode=str(state.get("intake_mode") or "HYBRID"),
                business_rules=business_rule_candidates or [],
                semantic_review=semantic_review,
                cq_semantic_assessments=cq_semantic_assessments,
            )
            try:
                self._require_ready_capability_plan(capability_plan)
            except WorkflowGateError as exc:
                self._mark_failed(project_dir, state, "S2", exc, input_payload=payload)
                raise

            stage_dir = project_dir / "02-semantic-recognition"
            self._write_yaml(
                stage_dir / "ontology-candidates.yaml",
                {"workflow_version": WORKFLOW_VERSION, "candidates": ontology_candidates},
            )
            self._write_json(
                stage_dir / "business-rule-candidates.json",
                business_rule_candidates or [],
            )
            self._write_json(stage_dir / "capability-plan.json", capability_plan)
            self._write_json(
                stage_dir / "gate-results.json",
                {
                    "stage": "S2",
                    "status": "PASSED",
                    "gates": [
                        {"id": "G-S2-STATUS", "status": "PASSED"},
                        {"id": "G-S2-EVIDENCE", "status": "PASSED"},
                        {"id": "G-S2-UNIQUE", "status": "PASSED"},
                        {"id": "G-S2-RULE", "status": "PASSED"},
                        {"id": "G-S2-HYBRID-EVIDENCE", "status": "PASSED"},
                        {"id": "G-S2-RULE-COVERAGE", "status": "PASSED"},
                        {"id": "G-S2-RULE-CONTRACT", "status": "PASSED"},
                        {"id": "G-S2-UNSUPPORTED-OPERATOR", "status": "PASSED"},
                        {"id": "G-S2-INPUT-INCOMPLETE", "status": "PASSED"},
                        {"id": "G-S2-SOURCE-NOT-BOUND", "status": "PASSED"},
                        {"id": "G-S2-TEST-EVIDENCE-FORBIDDEN", "status": "PASSED"},
                        {"id": "G-S2-PLATFORM-CAPABILITY", "status": "PASSED"},
                    ],
                    "checked_at": _now(),
                },
            )
            self._atomic_write(
                stage_dir / "business-semantics-report.html",
                render_s2_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    {**payload, "cq_semantic_review": semantic_review},
                ),
            )
            self._mark_artifacts_regenerated(
                state,
                {
                    "02-semantic-recognition/README.md",
                    "02-semantic-recognition/ontology-candidates.yaml",
                    "02-semantic-recognition/business-rule-candidates.json",
                    "02-semantic-recognition/capability-plan.json",
                    "02-semantic-recognition/gate-results.json",
                    "02-semantic-recognition/business-semantics-report.html",
                },
            )
            self._pass_stage(project_dir, state, "S2", "S3", payload)
            state["resume_point"] = "S3: 准备 Mapping 草案和人工确认项"
            self._save_state(project_dir, state)
            self._append_event(project_dir, "STAGE_PASSED", state, {"stage": "S2"})
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    @staticmethod
    def _normalize_s3_submission(payload: dict[str, Any]) -> dict[str, Any]:
        """为低风险自动决定补齐稳定 ID，避免格式缺失污染阶段失败审计。"""

        normalized_automatic: list[dict[str, Any]] = []
        for index, item in enumerate(payload.get("automatic_decisions") or [], start=1):
            if not isinstance(item, dict):
                raise WorkflowGateError(
                    "G-S3-AUTOMATIC-DECISION",
                    f"第 {index} 个自动决定必须是对象。",
                )
            normalized = dict(item)
            if not str(normalized.get("id") or "").strip():
                identity = {
                    "topic": str(normalized.get("topic") or "").strip(),
                    "decision": str(normalized.get("decision") or "").strip(),
                    "reason": str(normalized.get("reason") or "").strip(),
                    "source_refs": normalized.get("source_refs") or [],
                    "affected_mapping_ids": normalized.get("affected_mapping_ids") or [],
                }
                normalized["id"] = (
                    "S3-AUTO-" + hashlib.sha256(_canonical_json(identity)).hexdigest()[:12].upper()
                )
            normalized_automatic.append(normalized)
        return {**payload, "automatic_decisions": normalized_automatic}

    @staticmethod
    def _decision_scope(state: dict[str, Any]) -> str:
        active_revision = state.get("active_revision") or {}
        return str(active_revision.get("revision_id") or "BASE")

    @staticmethod
    def _confirmation_basis_fingerprint(confirmation: dict[str, Any]) -> str:
        basis = {
            "id": confirmation.get("id"),
            "title": confirmation.get("title"),
            "question": confirmation.get("question"),
            "business_question": confirmation.get("business_question"),
            "evidence": confirmation.get("evidence"),
            "options": [
                {
                    "id": option.get("id"),
                    "label": option.get("label"),
                    "summary": option.get("summary"),
                    "action": option.get("action"),
                    "mapping_updates": option.get("mapping_updates") or [],
                }
                for option in confirmation.get("options") or []
                if isinstance(option, dict)
            ],
        }
        return _fingerprint(basis)

    def _reuse_s3_confirmations(
        self,
        *,
        project_dir: Path,
        state: dict[str, Any],
        confirmations: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        decisions_path = project_dir / "03-mapping-review/decisions.jsonl"
        if not decisions_path.is_file():
            for item in confirmations:
                item["decision_basis_fingerprint"] = self._confirmation_basis_fingerprint(item)
            return confirmations, []
        scope = self._decision_scope(state)
        reusable: dict[str, dict[str, Any]] = {}
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                decision = json.loads(line)
            except json.JSONDecodeError:
                continue
            fingerprint = str(decision.get("decision_basis_fingerprint") or "")
            if fingerprint:
                reusable[fingerprint] = decision

        pending: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        for item in confirmations:
            fingerprint = self._confirmation_basis_fingerprint(item)
            item["decision_basis_fingerprint"] = fingerprint
            previous = reusable.get(fingerprint)
            if item.get("id") == "S3-OVERALL-MAPPING-REVIEW":
                pending.append(item)
                continue
            option_ids = {
                str(option.get("id") or "")
                for option in item.get("options") or []
                if isinstance(option, dict)
            }
            selected_option_id = str((previous or {}).get("selected_option_id") or "")
            selected_option = next(
                (
                    option
                    for option in item.get("options") or []
                    if isinstance(option, dict) and option.get("id") == selected_option_id
                ),
                None,
            )
            if (
                previous is None
                or selected_option_id not in option_ids
                or str(previous.get("selected_action") or "APPLY").upper() != "APPLY"
                or str((selected_option or {}).get("action") or "APPLY").upper() != "APPLY"
            ):
                pending.append(item)
                continue
            reused.append(
                {
                    "id": f"S3-REUSED-{hashlib.sha256(fingerprint.encode()).hexdigest()[:12].upper()}",
                    "topic": str(item.get("title") or item.get("id") or "建模决定"),
                    "decision": str(previous.get("decision") or "复用已确认决定"),
                    "reason": "决定依据、候选方案和修订范围指纹均未变化，平台自动复用。",
                    "source_refs": ["03-mapping-review/decisions.jsonl"],
                    "affected_mapping_ids": sorted(
                        {
                            str(update.get("id"))
                            for option in item.get("options") or []
                            for update in option.get("mapping_updates") or []
                            if isinstance(update, dict) and update.get("id")
                        }
                    ),
                    "decided_by": str(previous.get("decided_by") or "ORION_POLICY"),
                    "status": "REUSED_ACCEPTED",
                    "decision_basis_fingerprint": fingerprint,
                    "decision_scope": scope,
                    "selected_option_id": previous.get("selected_option_id"),
                    "mapping_updates": (selected_option or {}).get("mapping_updates") or [],
                    "original_decided_at": previous.get("decided_at"),
                    "decided_at": _now(),
                }
            )
        return pending, reused

    def _s3_business_review_only(self, state: dict[str, Any], payload: dict[str, Any]) -> bool:
        """Only an explicit, nonempty v2 business review can defer runtime compilation."""
        return (
            self._joint_design_enabled(state)
            and payload.get("realtime_runtime") is None
            and isinstance(payload.get("confirmations"), list)
            and bool(payload["confirmations"])
        )

    def _s3_confirmation_limit(self, state: dict[str, Any]) -> int:
        return MAX_BLOCKING_CONFIRMATIONS if self._joint_design_enabled(state) else MAX_BLOCKING_CONFIRMATIONS - 1

    def _require_s3_review_cards_retained(
        self, project_dir: Path, state: dict[str, Any], confirmations: list[dict[str, Any]],
    ) -> None:
        if not self._joint_design_enabled(state) or not state.get("s3_runtime_review"):
            return
        path = project_dir / "03-mapping-review/pending-confirmations.json"
        if not path.is_file() or (
            state.get("artifact_lifecycle", {}).get("03-mapping-review/pending-confirmations.json", {}).get("status") == "INVALIDATED"
        ):
            return
        previous_ids = {item["id"] for item in self._read_json(path)}
        submitted_ids = {item.get("id") for item in confirmations if isinstance(item, dict)}
        if previous_ids - submitted_ids:
            raise WorkflowGateError(
                "G-S3-CONFIRMATION-PENDING",
                "重新提交必须保留已展示的业务卡；已决定项由平台按依据指纹复用，不能通过删除卡片跳过业务确认。",
            )

    def _await_s3_runtime_compilation(self, project_dir: Path, state: dict[str, Any]) -> None:
        state["s3_runtime_review"] = {
            "status": "AWAITING_RUNTIME_COMPILATION",
            "validated": False,
            "message": "业务决定已记录；请按批准口径编译并预检运行映射与规则。若需修改 S2 规则，先正式退回 S2。",
        }
        state["stage_statuses"]["S3"] = "RUNNING"
        state["project_status"] = "IN_PROGRESS"
        state["blocking"] = None
        state["resume_point"] = "S3: 业务决定后编译运行设计并完成全部门禁"
        state["stage_fingerprints"]["S3"] = {
            "output": self._stage_fingerprint(project_dir / "03-mapping-review"),
            "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
        }
        self._save_state(project_dir, state)
        self._append_event(project_dir, "S3_RUNTIME_COMPILATION_REQUIRED", state, {"stage": "S3"})

    def prepare_mapping_review(
        self,
        *,
        project_id: str,
        mapping_draft: dict[str, Any],
        confirmations: list[dict[str, Any]],
        automatic_decisions: list[dict[str, Any]] | None = None,
        realtime_runtime: dict[str, Any] | None = None,
        review_policy: str = "HUMAN_REQUIRED",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        normalized_review_policy = review_policy.strip().upper()
        if normalized_review_policy not in {"HUMAN_REQUIRED", "AUTO_APPROVE_EVIDENCE_BACKED"}:
            raise WorkflowError(
                "review_policy 只能是 HUMAN_REQUIRED 或 AUTO_APPROVE_EVIDENCE_BACKED。"
            )
        project_dir = self._resolve_project(project_id)
        current_state = self._read_state(project_dir)
        intake_mode = str(current_state.get("intake_mode") or "HYBRID")
        business_review_only = self._s3_business_review_only(
            current_state, {"confirmations": confirmations, "realtime_runtime": realtime_runtime}
        )
        runtime_submission = dict(realtime_runtime or {}) if realtime_runtime is not None else None
        if runtime_submission is not None and intake_mode != "DOCUMENT_ONLY":
            normalized_obda, _compiler_decisions = _normalize_s3_obda(
                str(runtime_submission.get("mapping_obda") or ""), check_sources=not business_review_only
            )
            runtime_submission["mapping_obda"] = normalized_obda
        try:
            normalized_runtime = None if business_review_only else normalize_runtime_submission(
                runtime_submission,
                intake_mode=intake_mode,
                require_explicit_capabilities=intake_mode != "DOCUMENT_ONLY",
            )
        except RuntimeReleaseError as exc:
            raise WorkflowGateError("G-S3-RUNTIME", str(exc), path=exc.path, reason_code=exc.reason_code) from exc
        payload = self._normalize_s3_submission(
            {
                "mapping_draft": mapping_draft,
                "confirmations": confirmations,
                "automatic_decisions": automatic_decisions or [],
                "realtime_runtime": normalized_runtime,
                "review_policy": normalized_review_policy,
            }
        )
        automatic_decisions = payload["automatic_decisions"]
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S3")
            self._require_expected_revision(state, expected_revision)
            self._require_s3_review_cards_retained(project_dir, state, confirmations)
            self._reject_identical_failed_submission(state, "S3", payload)
            if normalized_runtime is None and not business_review_only:
                raise WorkflowGateError(
                    "G-S3-RUNTIME",
                    "所有新本体都必须声明只读实时问答能力；结构化项目不能省略 runtime。",
                )
            try:
                normalized, normalized_automatic = self._validate_s3(
                    payload,
                    intake_mode=str(state.get("intake_mode") or "HYBRID"),
                    project_dir=project_dir,
                )
                normalized, reused_decisions = self._reuse_s3_confirmations(
                    project_dir=project_dir,
                    state=state,
                    confirmations=normalized,
                )
                normalized_automatic.extend(reused_decisions)
                if business_review_only and not any(
                    item.get("id") != "S3-OVERALL-MAPPING-REVIEW" for item in normalized
                ):
                    raise WorkflowGateError("G-S3-RUNTIME", "业务卡已决定；必须提交完整运行设计并通过预检，不能重复提交仅业务卡。")
                if normalized_review_policy == "AUTO_APPROVE_EVIDENCE_BACKED" or self._joint_design_enabled(state):
                    normalized = [
                        item for item in normalized if item.get("id") != "S3-OVERALL-MAPPING-REVIEW"
                    ]
                    if not normalized:
                        mapping_ids = sorted(
                            str(item.get("id"))
                            for item in mapping_draft.get("mappings") or []
                            if item.get("id")
                        )
                        normalized_automatic.append(
                            {
                                "id": "S3-OVERALL-MAPPING-AUTO",
                                "topic": "总体映射检查",
                                "decision": (
                                    "证据和映射门禁通过，形成已审候选；完整设计在 S4 统一批准。"
                                    if self._joint_design_enabled(state)
                                    else "证据和映射门禁通过，自动冻结为正式映射。"
                                ),
                                "reason": "当前不存在需要业务负责人选择的高影响歧义；按最小确认策略自动继续。",
                                "source_refs": [
                                    "03-mapping-review/mapping-draft.yaml",
                                    (
                                        "00-document-evidence/evidence-index.json"
                                        if state.get("intake_mode") == "DOCUMENT_ONLY"
                                        else "01-data-understanding/schema-snapshot.json"
                                    ),
                                ],
                                "affected_mapping_ids": mapping_ids,
                                "decided_by": "ORION_POLICY",
                                "status": "AUTO_ACCEPTED",
                                "decided_at": _now(),
                            }
                        )
            except WorkflowGateError as exc:
                self._mark_failed(project_dir, state, "S3", exc, input_payload=payload)
                raise

            stage_dir = project_dir / "03-mapping-review"
            self._write_yaml(stage_dir / "mapping-draft.yaml", mapping_draft)
            for reused in reused_decisions:
                reused["applied_mapping_updates"] = self._apply_mapping_updates(
                    project_dir,
                    reused.get("mapping_updates") or [],
                )
            write_runtime_review_assets(
                stage_dir,
                normalized_runtime,
                prepared_at=_now(),
            )
            self._write_json(stage_dir / "pending-confirmations.json", normalized)
            self._write_json(
                stage_dir / "automatic-decisions.json",
                normalized_automatic,
            )
            if self._joint_design_enabled(state):
                state["s3_runtime_review"] = {
                    "status": "AWAITING_BUSINESS_DECISIONS" if normalized else "VALIDATED",
                    "validated": not normalized and normalized_runtime is not None,
                    "message": (
                        "等待业务决定后编译并完整预检运行设计；当前仅为业务评审草案，S3 尚未通过。"
                        if normalized else "运行设计已通过 S3 全部门禁。"
                    ),
                }
                if not normalized:
                    # Reused decisions may patch mapping targets/types after the first validation.
                    self._validate_s3(
                        {**payload, "mapping_draft": yaml.safe_load((stage_dir / "mapping-draft.yaml").read_text(encoding="utf-8"))},
                        intake_mode=intake_mode, project_dir=project_dir,
                    )
            regenerated_paths = {
                "03-mapping-review/README.md",
                "03-mapping-review/mapping-draft.yaml",
                "03-mapping-review/pending-confirmations.json",
                "03-mapping-review/automatic-decisions.json",
            }
            regenerated_paths.update(self._mapping_runtime_artifact_paths(stage_dir, state))
            self._mark_artifacts_regenerated(state, regenerated_paths)
            if normalized:
                first = normalized[0]
                state["stage_statuses"]["S3"] = "BLOCKED_HUMAN"
                state["project_status"] = "BLOCKED_HUMAN"
                state["blocking"] = {
                    "gate": "GATE-1",
                    "confirmation_id": first["id"],
                    "reason": first["question"],
                }
                state["resume_point"] = f"S3/GATE-1: 等待回答 {first['id']}"
                state["stage_fingerprints"]["S3"] = {
                    "input": _fingerprint(payload),
                    "output": self._stage_fingerprint(stage_dir),
                    "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
                }
                self._save_state(project_dir, state)
                self._append_event(
                    project_dir,
                    "HUMAN_GATE_BLOCKED",
                    state,
                    {"stage": "S3", "confirmation_id": first["id"]},
                )
            else:
                self._finalize_mapping(project_dir, state)
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def resolve_confirmation(
        self,
        *,
        project_id: str,
        confirmation_id: str,
        decision: str,
        decided_by: str,
        rationale: str,
        selected_option_id: str | None = None,
        mapping_updates: list[dict[str, Any]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not decision.strip() or not decided_by.strip() or not rationale.strip():
            raise WorkflowError("decision、decided_by 和 rationale 都不能为空。")

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            if state["current_stage"] != "S3" or state["stage_statuses"]["S3"] != "BLOCKED_HUMAN":
                raise WorkflowError("项目当前不在 S3 人工评审关口。")

            pending_path = project_dir / "03-mapping-review/pending-confirmations.json"
            confirmations = self._read_json(pending_path)
            target = next(
                (item for item in confirmations if item["id"] == confirmation_id),
                None,
            )
            if target is None:
                raise WorkflowError(f"不存在确认项：{confirmation_id}")
            if target["status"] == "RESOLVED":
                raise WorkflowError(f"确认项已处理：{confirmation_id}")

            options = target.get("options") or []
            option_ids = {str(item.get("id")) for item in options}
            if options and selected_option_id not in option_ids:
                raise WorkflowError(f"确认项 {confirmation_id} 必须选择有效的 selected_option_id。")
            selected_option = next(
                (item for item in options if item.get("id") == selected_option_id),
                {},
            )
            selected_action = str(selected_option.get("action") or "APPLY").upper()

            decided_at = _now()
            applied_updates = self._apply_mapping_updates(
                project_dir,
                mapping_updates or [],
            )
            target.update(
                {
                    "status": "RESOLVED",
                    "decision": decision.strip(),
                    "selected_option_id": selected_option_id,
                    "selected_action": selected_action,
                    "decided_by": decided_by.strip(),
                    "rationale": rationale.strip(),
                    "mapping_updates": applied_updates,
                    "decided_at": decided_at,
                }
            )
            self._write_json(pending_path, confirmations)
            self._append_jsonl(
                project_dir / "03-mapping-review/decisions.jsonl",
                {
                    "confirmation_id": confirmation_id,
                    "question": target["question"],
                    "decision": decision.strip(),
                    "selected_option_id": selected_option_id,
                    "selected_action": selected_action,
                    "decided_by": decided_by.strip(),
                    "rationale": rationale.strip(),
                    "mapping_updates": applied_updates,
                    "decision_basis_fingerprint": (
                        target.get("decision_basis_fingerprint")
                        or self._confirmation_basis_fingerprint(target)
                    ),
                    "decision_scope": self._decision_scope(state),
                    "decided_at": decided_at,
                },
            )

            if selected_action == "RETURN_TO_S2":
                self._append_event(
                    project_dir,
                    "HUMAN_DECISION_RECORDED",
                    state,
                    {
                        "stage": "S3",
                        "confirmation_id": confirmation_id,
                        "selected_action": selected_action,
                        "actor": decided_by.strip(),
                    },
                )
                return self._apply_stage_rollback(
                    project_dir,
                    state,
                    stage="S2",
                    reason=f"S3 总体评审退回：{rationale.strip()}",
                    requested_by=decided_by.strip(),
                    rollback_origin="HUMAN_REVIEW_REJECTION",
                )

            unresolved = [item for item in confirmations if item["status"] != "RESOLVED"]
            if unresolved:
                next_item = unresolved[0]
                state["blocking"] = {
                    "gate": "GATE-1",
                    "confirmation_id": next_item["id"],
                    "reason": next_item["question"],
                }
                state["resume_point"] = f"S3/GATE-1: 等待回答 {next_item['id']}"
                self._save_state(project_dir, state)
                self._append_event(
                    project_dir,
                    "HUMAN_DECISION_RECORDED",
                    state,
                    {"confirmation_id": confirmation_id},
                )
            else:
                if self._joint_design_enabled(state):
                    self._await_s3_runtime_compilation(project_dir, state)
                else:
                    self._finalize_mapping(project_dir, state)
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def resolve_confirmation_option(
        self,
        *,
        project_id: str,
        confirmation_id: str,
        selected_option_id: str,
        decided_by: str,
        rationale: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """按 S3 已审计选项提交决定，供工程页和对话页共用。"""

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            pending_path = project_dir / "03-mapping-review/pending-confirmations.json"
            if not pending_path.exists():
                raise WorkflowError("项目尚未形成 S3 待确认问题。")
            confirmations = self._read_json(pending_path)
            target = next(
                (item for item in confirmations if item.get("id") == confirmation_id),
                None,
            )
            if target is None:
                raise WorkflowError(f"不存在确认项：{confirmation_id}")
            option = next(
                (
                    item
                    for item in target.get("options") or []
                    if item.get("id") == selected_option_id
                ),
                None,
            )
            if option is None:
                raise WorkflowError(f"确认项 {confirmation_id} 不存在选项：{selected_option_id}")
            decision = f"{option['label']}：{option['summary']}"
            mapping_updates = option.get("mapping_updates") or []
            return self.resolve_confirmation(
                project_id=project_id,
                confirmation_id=confirmation_id,
                decision=decision,
                selected_option_id=selected_option_id,
                decided_by=decided_by,
                rationale=rationale,
                mapping_updates=mapping_updates,
                expected_revision=expected_revision,
            )

    def retry_failed_stage(self, *, project_id: str) -> dict[str, Any]:
        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            stage = state.get("current_stage")
            if stage not in STAGES or state["stage_statuses"][stage] != "FAILED":
                raise WorkflowError("当前没有可重试的失败阶段。")
            state["stage_statuses"][stage] = "RUNNING"
            state["project_status"] = "IN_PROGRESS"
            state["last_error"] = None
            state["blocking"] = None
            state["resume_point"] = f"{stage}: 修正输入后重新执行阶段记录"
            self._save_state(project_dir, state)
            self._append_event(project_dir, "STAGE_RETRY_STARTED", state, {"stage": stage})
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def resume_active_revision(self, *, project_id: str) -> dict[str, Any]:
        """Resume the first incomplete stage declared by the active component revision."""

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            active_revision = state.get("active_revision") or {}
            if active_revision.get("status") != "IN_PROGRESS":
                raise WorkflowError("当前没有需要恢复的活动修订。")
            required = list(active_revision.get("required_revalidation_stages") or [])
            next_stage = next(
                (
                    stage
                    for stage in required
                    if state.get("stage_statuses", {}).get(stage)
                    not in {"PASSED", "NOT_APPLICABLE"}
                ),
                None,
            )
            if next_stage is None:
                raise WorkflowError("活动修订的必需阶段均已完成。")
            current = str(state.get("current_stage") or "")
            if current and current != next_stage:
                raise WorkflowError(f"工程当前位于 {current}，不能恢复到 {next_stage}。")
            stage_status = state.get("stage_statuses", {}).get(next_stage)
            if current == next_stage and stage_status == "RUNNING":
                return self._status_payload(project_dir, state)
            if stage_status not in {"PENDING", "RUNNING"}:
                raise WorkflowError(
                    f"{next_stage} 当前状态为 {stage_status}；请使用对应的评审或失败重试入口。"
                )
            state["stage_statuses"][next_stage] = "RUNNING"
            state["current_stage"] = next_stage
            state["project_status"] = "IN_PROGRESS"
            state["blocking"] = None
            state["last_error"] = None
            state["resume_point"] = f"{next_stage}: 从活动修订检查点继续"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ACTIVE_REVISION_RESUMED",
                state,
                {
                    "stage": next_stage,
                    "revision_id": active_revision.get("revision_id"),
                    "required_revalidation_stages": required,
                },
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def record_ontology_design(
        self,
        *,
        project_id: str,
        ontology_design: dict[str, Any],
    ) -> dict[str, Any]:
        """记录经过 Mapping 约束的本体施工图，并推进到 S5。"""

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            self._start_s4_if_ready(project_dir, state)
            project_dir, state = self._require_stage(project_id, "S4")
            review_path = project_dir / "04-ontology-design/competency-question-review.json"
            review = self._read_json(review_path) if review_path.exists() else {}
            if not self._joint_design_enabled(state):
                ontology_design = self._normalize_ontology_design_localization(
                    project_dir, ontology_design,
                )
            if self._joint_design_enabled(state):
                try:
                    verify_joint_review(project_dir, ontology_design, review)
                    if review.get("approval_mode") != "HUMAN":
                        raise ValueError("联合设计需要负责人明确批准。")
                except ValueError as exc:
                    raise WorkflowGateError("G-S4-JOINT-DESIGN", str(exc)) from exc
            questions = ontology_design.get("competency_questions") or []
            if review.get("status") != "APPROVED" or review.get(
                "approved_questions_sha256"
            ) != _fingerprint(questions):
                raise WorkflowGateError(
                    "GATE-S4-BUSINESS-QUESTIONS",
                    "S4 业务问题尚未由负责人确认，不能生成正式本体施工图。",
                )
            try:
                design_metrics = self._validate_s4(project_dir, ontology_design)
            except WorkflowGateError as exc:
                self._mark_failed(
                    project_dir,
                    state,
                    "S4",
                    exc,
                    input_payload=ontology_design,
                )
                raise

            stage_dir = project_dir / "04-ontology-design"
            stage_dir.mkdir(parents=True, exist_ok=True)
            document = {
                "workflow_version": WORKFLOW_VERSION,
                "design_status": "APPROVED_FOR_BUILD",
                "generated_at": _now(),
                **ontology_design,
            }
            self._write_yaml(stage_dir / "ontology-design.yaml", document)
            if self._joint_design_enabled(state):
                try:
                    baseline = freeze_joint_design(project_dir, ontology_design, review)
                except ValueError as exc:
                    raise WorkflowGateError("G-S4-JOINT-DESIGN", str(exc)) from exc
                state["joint_design_fingerprint"] = baseline["joint_design_fingerprint"]
                self._mark_artifacts_regenerated(
                    state,
                    {path.relative_to(project_dir).as_posix()
                     for path in (stage_dir / "joint-design-inputs").rglob("*") if path.is_file()}
                    | {"04-ontology-design/joint-design-baseline.json"},
                )
            self._write_json(
                stage_dir / "gate-results.json",
                {
                    "stage": "S4",
                    "status": "PASSED",
                    "gates": [
                        {"id": "G-S4-REQUIRED", "status": "PASSED"},
                        {"id": "G-S4-UNIQUE-IRI", "status": "PASSED"},
                        {"id": "G-S4-MAPPING-COVERAGE", "status": "PASSED"},
                        {"id": "G-S4-CHINESE", "status": "PASSED"},
                        {"id": "G-S4-CQ", "status": "PASSED"},
                        {"id": "G-S4-LOGIC", "status": "PASSED"},
                        {"id": "G-S4-CQ-PRODUCTION", "status": "PASSED"},
                        {"id": "G-S4-CQ-REASONING", "status": "PASSED"},
                        {"id": "G-S4-FACT-ARITY", "status": "PASSED"},
                        {"id": "G-S4-PRODUCTION-LOGIC", "status": "PASSED"},
                        {"id": "G-S4-RELATION-COVERAGE", "status": "PASSED"},
                        {"id": "G-S4-CQ-CONTRACT-COVERAGE", "status": "PASSED"},
                    ],
                    "metrics": design_metrics,
                    "checked_at": _now(),
                },
            )
            self._atomic_write(
                stage_dir / "ontology-design-report.html",
                render_s4_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    document,
                    self._read_json(stage_dir / "gate-results.json"),
                ),
            )
            self._mark_artifacts_regenerated(
                state,
                {
                    "04-ontology-design/README.md",
                    "04-ontology-design/ontology-design.yaml",
                    "04-ontology-design/gate-results.json",
                    "04-ontology-design/ontology-design-report.html",
                },
            )
            self._pass_stage(project_dir, state, "S4", "S5", ontology_design)
            state["resume_point"] = (
                "S5: 按 ontology-design.yaml 调用 Protégé 构建并导出 OWL/TTL/SHACL"
                if state["current_stage"] == "S5"
                else f"{state['current_stage']}: 复用未变化构建制品，继续必需验证"
            )
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ONTOLOGY_DESIGN_COMPLETED",
                state,
                {"stage": "S4", **design_metrics},
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def generate_ontology_design(
        self,
        *,
        project_id: str,
        ontology_iri: str | None = None,
        version: str = "0.1.0",
        competency_questions: list[dict[str, Any]] | None = None,
        logical_axioms: list[dict[str, Any]] | None = None,
        review_policy: str = "HUMAN_REQUIRED",
    ) -> dict[str, Any]:
        """从正式 Mapping 和对应来源证据确定性生成 S4 施工图并执行门禁。"""

        with self._project_mutation_lock(project_id):
            return self._generate_ontology_design_unlocked(
                project_id=project_id,
                ontology_iri=ontology_iri,
                version=version,
                competency_questions=competency_questions,
                logical_axioms=logical_axioms,
                review_policy=review_policy,
            )

    def _generate_ontology_design_unlocked(
        self,
        *,
        project_id: str,
        ontology_iri: str | None = None,
        version: str = "0.1.0",
        competency_questions: list[dict[str, Any]] | None = None,
        logical_axioms: list[dict[str, Any]] | None = None,
        review_policy: str = "HUMAN_REQUIRED",
        preview_only: bool = False,
    ) -> dict[str, Any]:
        """从正式 Mapping 和对应来源证据确定性生成 S4 施工图。

        ``preview_only`` 仅供只读预检复用同一生成路径；不会保存草案、改变状态
        或追加审计事件。
        """

        project_dir = self._resolve_project(project_id)
        state = self._read_state(project_dir)
        if state.get("stage_statuses", {}).get("S3") != "PASSED":
            raise WorkflowError("只有 S3 Mapping 评审通过后才能生成本体施工图。")
        mapping = yaml.safe_load(
            (project_dir / "03-mapping-review/mapping.yaml").read_text(encoding="utf-8")
        )
        reviewed_runtime_path = project_dir / "03-mapping-review/runtime/mapping.obda"
        reviewed_obda = reviewed_runtime_path.read_text(encoding="utf-8") if reviewed_runtime_path.exists() else ""
        reviewed_runtime_prefix = (
            _runtime_prefix_iri(reviewed_obda)
            if reviewed_runtime_path.exists()
            else None
        )
        mapping_namespace = str(mapping.get("namespace") or "").strip()
        if mapping_namespace:
            prefix_block = reviewed_obda.split("[MappingDeclaration]", 1)[0]
            declared_namespaces = set(re.findall(r"(?m)^\s*[A-Za-z_][\w-]*:\s*(https?://\S+)\s*$|^\s*:\s*(https?://\S+)\s*$", prefix_block))
            namespace_values = {value for pair in declared_namespaces for value in pair if value}
            if reviewed_obda and mapping_namespace not in namespace_values:
                raise WorkflowGateError("G-S4-NAMESPACE", "正式 Mapping namespace 与 OBDA 声明不一致。")
            if ontology_iri and ontology_iri.rstrip("#/") != mapping_namespace.rstrip("#/"):
                raise WorkflowGateError("G-S4-NAMESPACE", "S4 ontology_iri 不能偏离 S3 已审 namespace。")
            reviewed_runtime_prefix = mapping_namespace
        if ontology_iri:
            iri = ontology_iri.rstrip("#/")
            namespace = (
                reviewed_runtime_prefix
                if reviewed_runtime_prefix and reviewed_runtime_prefix.rstrip("#/") == iri
                else f"{iri}#"
            )
        elif reviewed_runtime_prefix:
            namespace = reviewed_runtime_prefix
            iri = reviewed_runtime_prefix.rstrip("#/")
        else:
            iri = f"https://orion.local/ontology/{project_id}"
            namespace = f"{iri}#"
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            raise WorkflowGateError("G-S4-REQUIRED", "version 必须使用语义化版本号。")

        mappings = mapping.get("mappings") or []
        intake_mode = str(state.get("intake_mode") or "HYBRID")
        data_property_target_counts: dict[str, int] = {}
        for mapping_item in mappings:
            if str(mapping_item.get("mapping_type") or "").upper() not in (
                DATABASE_DATA_MAPPING_TYPES | {"EVIDENCE_TO_DATA_PROPERTY"}
            ):
                continue
            mapped_target = str(mapping_item.get("target") or "").split(" (")[0].strip()
            data_property_target_counts[mapped_target] = (
                data_property_target_counts.get(mapped_target, 0) + 1
            )
        table_classes = {
            str(item["source"]): str(item["target"])
            for item in mappings
            if item.get("mapping_type") == "TABLE_TO_CLASS"
        }
        relation_path = project_dir / "01-data-understanding/relation-candidates.json"
        relations = self._read_json(relation_path) if relation_path.exists() else []
        relation_index: dict[tuple[str, str], dict[str, Any]] = {}
        for relation_item in relations:
            relation = dict(relation_item)
            source_table = str(relation.get("source_table") or "").strip()
            source_column = str(relation.get("source_column") or "").strip()
            if not source_table or not source_column:
                source_table, source_column = _split_qualified_column(
                    str(relation.get("from") or relation.get("source") or "")
                )
            if not str(relation.get("target_table") or "").strip():
                target_table, _ = _split_qualified_column(
                    str(relation.get("to") or relation.get("target") or "")
                )
                if target_table:
                    relation["target_table"] = target_table
            if source_table and source_column:
                relation_index[(source_table, source_column)] = relation

        classes: list[dict[str, Any]] = []
        object_properties: list[dict[str, Any]] = []
        data_properties: list[dict[str, Any]] = []
        constraints: list[dict[str, Any]] = []

        def ontology_entity_iri(value: Any) -> str:
            raw = str(value or "").strip()
            if raw.startswith(("http://", "https://")):
                return raw
            return f"{namespace}{raw}"

        def localization_fields(
            mapping_item: dict[str, Any],
            target_name: str,
            kind_label: str,
        ) -> dict[str, str]:
            label_zh = str(
                mapping_item.get("target_label_zh")
                or mapping_item.get("label_zh")
                or mapping_item.get("business_name_zh")
                or ONTOLOGY_NAME_LABELS.get(target_name)
                or ""
            ).strip()
            comment_zh = str(
                mapping_item.get("target_comment_zh")
                or mapping_item.get("comment_zh")
                or mapping_item.get("definition_zh")
                or mapping_item.get("definition")
                or ""
            ).strip()
            result: dict[str, str] = {}
            if label_zh:
                result["label_zh"] = label_zh
                result["comment_zh"] = (
                    comment_zh
                    if _contains_chinese(comment_zh)
                    else f"{label_zh}的{kind_label}定义；来源于正式 Mapping。"
                )
            return result

        def data_property_identity(
            mapping_item: dict[str, Any],
            target_name: str,
            domain_name: str,
        ) -> tuple[str, str]:
            if data_property_target_counts.get(target_name, 0) <= 1:
                return target_name, ontology_entity_iri(target_name)
            domain_local_name = re.split(r"[#/]", domain_name.rstrip("#/"))[-1]
            if not domain_local_name:
                raise WorkflowGateError(
                    "G-S4-MAPPING-COVERAGE",
                    f"无法为重名数据属性 {mapping_item.get('id')} 确定所属类。",
                )
            scoped_name = domain_local_name[:1].lower() + domain_local_name[1:]
            scoped_name += target_name[:1].upper() + target_name[1:]
            return scoped_name, ontology_entity_iri(scoped_name)

        for item in mappings:
            mapping_id = str(item["id"])
            mapping_type = str(item.get("mapping_type")).upper()
            target = str(item.get("target") or "").split(" (")[0].strip()
            target_iri = ontology_entity_iri(target)
            if mapping_type == "EVIDENCE_TO_CLASS":
                classes.append(
                    {
                        "name": target,
                        "iri": target_iri,
                        "source_evidence": item.get("source"),
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "业务类"),
                    }
                )
                continue

            if mapping_type == "EVIDENCE_TO_OBJECT_PROPERTY":
                domain_name = str(item.get("domain") or "").strip()
                range_name = str(item.get("range") or "").strip()
                if not domain_name or not range_name:
                    raise WorkflowGateError(
                        "G-S4-MAPPING-COVERAGE",
                        f"资料关系映射 {mapping_id} 必须明确 domain 和 range。",
                    )
                object_properties.append(
                    {
                        "name": target,
                        "iri": target_iri,
                        "domain": ontology_entity_iri(domain_name),
                        "range": ontology_entity_iri(range_name),
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "对象属性"),
                    }
                )
                continue

            if mapping_type == "EVIDENCE_TO_DATA_PROPERTY":
                domain_name = str(item.get("domain") or "").strip()
                if not domain_name:
                    raise WorkflowGateError(
                        "G-S4-MAPPING-COVERAGE",
                        f"资料属性映射 {mapping_id} 必须明确 domain。",
                    )
                property_name, property_iri = data_property_identity(item, target, domain_name)
                datatype = str(item.get("datatype") or "http://www.w3.org/2001/XMLSchema#string")
                data_properties.append(
                    {
                        "name": property_name,
                        "iri": property_iri,
                        **({"mapping_target": target} if property_name != target else {}),
                        "domain": ontology_entity_iri(domain_name),
                        "range": datatype,
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "数据属性"),
                    }
                )
                constraints.append(
                    {
                        "id": f"CON-{mapping_id.removeprefix('MAP-')}",
                        "class": ontology_entity_iri(domain_name),
                        "property": property_iri,
                        "datatype": datatype,
                        "source_mapping_ids": [mapping_id],
                    }
                )
                continue

            if mapping_type in DATABASE_CLASS_MAPPING_TYPES | RULE_CLASS_MAPPING_TYPES:
                classes.append(
                    {
                        "name": target,
                        "iri": target_iri,
                        (
                            "source_table"
                            if mapping_type == "TABLE_TO_CLASS"
                            else "source_expression"
                        ): item.get("source"),
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "业务类"),
                    }
                )
                continue

            source = str(item.get("source") or "")
            source_table, source_column = _split_qualified_column(source)
            if mapping_type in {
                "COLUMN_VALUE_TO_OBJECT_PROPERTY",
                "SQL_TO_OBJECT_PROPERTY",
                "CANDIDATE_JOIN_TO_OBJECT_PROPERTY",
            }:
                domain_name = str(item.get("domain") or "").strip()
                range_name = str(item.get("range") or "").strip()
                if not domain_name or not range_name:
                    raise WorkflowGateError(
                        "G-S4-MAPPING-COVERAGE",
                        f"语义关系映射 {mapping_id} 必须明确 domain 和 range。",
                    )
                object_properties.append(
                    {
                        "name": target,
                        "iri": target_iri,
                        "domain": ontology_entity_iri(domain_name),
                        "range": ontology_entity_iri(range_name),
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "对象属性"),
                    }
                )
                continue

            if mapping_type in {"FK_TO_OBJECT_PROPERTY", "FOREIGN_KEY_TO_OBJECT_PROPERTY"}:
                # S3 是已评审的正式语义合同；明确端点必须优先于数据库 FK 重新推断。
                domain_name = str(item.get("domain") or "").strip()
                range_name = str(item.get("range") or "").strip()
                if not domain_name or not range_name:
                    inferred_domain: str | None = None
                    inferred_range: str | None = None
                    if "(" in source and len(item.get("source_refs") or []) >= 2:
                        endpoint_classes: list[str] = []
                        for ref in item.get("source_refs") or []:
                            raw = str(ref).removeprefix("table:")
                            if "." not in raw:
                                continue
                            table, column = raw.split(".", 1)
                            relation = relation_index.get((table, column)) or {}
                            endpoint_class = table_classes.get(str(relation.get("target_table")))
                            if endpoint_class:
                                endpoint_classes.append(endpoint_class)
                        inferred_domain = (
                            endpoint_classes[0]
                            if endpoint_classes
                            else table_classes.get(source_table)
                        )
                        inferred_range = endpoint_classes[1] if len(endpoint_classes) > 1 else None
                    else:
                        relation = relation_index.get((source_table, source_column)) or {}
                        inferred_domain = table_classes.get(source_table)
                        inferred_range = table_classes.get(str(relation.get("target_table")))
                        # 旧 Mapping 未冻结端点时，hasX 仍保留历史兼容推断。
                        if target.startswith("has") and inferred_domain and inferred_range:
                            inferred_domain, inferred_range = inferred_range, inferred_domain
                    domain_name = domain_name or inferred_domain
                    range_name = range_name or inferred_range
                if not domain_name or not range_name:
                    raise WorkflowGateError(
                        "G-S4-MAPPING-COVERAGE",
                        f"无法根据外键证据确定 {mapping_id} 的 domain/range。",
                    )
                object_properties.append(
                    {
                        "name": target,
                        "iri": target_iri,
                        "domain": ontology_entity_iri(domain_name),
                        "range": ontology_entity_iri(range_name),
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "对象属性"),
                    }
                )
                continue

            if mapping_type in DATABASE_DATA_MAPPING_TYPES:
                domain_name = str(
                    item.get("domain") or item.get("class") or ""
                ).strip() or table_classes.get(source_table)
                if not domain_name:
                    raise WorkflowGateError(
                        "G-S4-MAPPING-COVERAGE",
                        f"无法确定 {mapping_id} 的所属业务类。",
                    )
                lowered = source_column.lower()
                explicit_datatype = str(item.get("datatype") or "").strip()
                if explicit_datatype:
                    datatype = explicit_datatype
                elif lowered.endswith("_at") or lowered.endswith("_time"):
                    datatype = "http://www.w3.org/2001/XMLSchema#dateTime"
                elif lowered.endswith("_days") or lowered in {"revision", "line_number"}:
                    datatype = "http://www.w3.org/2001/XMLSchema#integer"
                elif any(
                    token in lowered
                    for token in ("quantity", "price", "rate", "score", "stock", "amount")
                ):
                    datatype = "http://www.w3.org/2001/XMLSchema#decimal"
                else:
                    datatype = "http://www.w3.org/2001/XMLSchema#string"
                property_name, property_iri = data_property_identity(item, target, domain_name)
                data_properties.append(
                    {
                        "name": property_name,
                        "iri": property_iri,
                        **({"mapping_target": target} if property_name != target else {}),
                        "domain": ontology_entity_iri(domain_name),
                        "range": datatype,
                        "source_mapping_ids": [mapping_id],
                        **localization_fields(item, target, "数据属性"),
                    }
                )
                constraints.append(
                    {
                        "id": f"CON-{mapping_id.removeprefix('MAP-')}",
                        "class": ontology_entity_iri(domain_name),
                        "property": property_iri,
                        "datatype": datatype,
                        "source_mapping_ids": [mapping_id],
                    }
                )

        # S3 Mapping only covers facts that can be materialized from a source.
        # Rule conclusions are nevertheless first-class ontology entities and
        # must not be dropped (or disguised as database mappings) at S4.
        # Several source projections can instantiate one business class.  Keep
        # one OWL declaration and retain every reviewed source expression.
        class_by_iri: dict[str, dict[str, Any]] = {}
        mappings_by_id = {str(item["id"]): item for item in mappings}
        for declaration in classes:
            existing = class_by_iri.setdefault(declaration["iri"], dict(declaration))
            existing["source_mapping_ids"] = list(dict.fromkeys([
                *existing.get("source_mapping_ids", []), *declaration["source_mapping_ids"],
            ]))
        for declaration in class_by_iri.values():
            declaration["mapping_sources"] = [
                dict(mappings_by_id[mapping_id]) for mapping_id in declaration["source_mapping_ids"]
            ]
        classes = list(class_by_iri.values())

        semantic_path = project_dir / "02-semantic-recognition/ontology-candidates.yaml"
        rule_path = project_dir / "02-semantic-recognition/business-rule-candidates.json"
        semantic_document = (
            yaml.safe_load(semantic_path.read_text(encoding="utf-8"))
            if semantic_path.is_file()
            else {}
        ) or {}
        semantic_candidates = semantic_document.get("candidates") or []
        business_rules = self._read_json(rule_path) if rule_path.is_file() else []
        rules_by_conclusion = {
            str(rule.get("conclusion_predicate") or "").strip(): rule
            for rule in business_rules
            if str(rule.get("conclusion_predicate") or "").strip()
        }
        declared_class_names = {str(item.get("name") or "") for item in classes}
        for candidate in semantic_candidates:
            if str(candidate.get("kind") or "").upper() != "CLASS":
                continue
            name = str(candidate.get("name") or "").strip()
            candidate_id = str(candidate.get("id") or "").strip()
            rule = rules_by_conclusion.get(name)
            if not name or not candidate_id or name in declared_class_names or rule is None:
                continue
            rule_name = str(rule.get("name") or name).strip()
            label_zh = re.sub(r"(?:判定|推导|识别)$", "", rule_name).strip()
            if not _contains_chinese(label_zh):
                raise WorkflowGateError(
                    "G-S4-CHINESE",
                    f"规则推导类 {name} 缺少可审计的中文业务名称。",
                )
            classes.append(
                {
                    "name": name,
                    "iri": ontology_entity_iri(name),
                    "source_semantic_ids": [candidate_id],
                    "source_rule_ids": [str(rule.get("id") or "").strip()],
                    "source_refs": list(
                        dict.fromkeys(
                            str(value)
                            for value in [
                                *(candidate.get("source_refs") or []),
                                *(rule.get("source_refs") or []),
                            ]
                            if str(value).strip()
                        )
                    ),
                    "label_zh": label_zh,
                    "comment_zh": str(rule.get("description") or rule_name),
                }
            )
            declared_class_names.add(name)

        cq_intake_path = project_dir / "00-document-evidence/cq-intake.json"
        cq_intake = (
            self._read_json(cq_intake_path)
            if cq_intake_path.exists()
            else {"mode": "AI_GENERATED", "questions": []}
        )
        intake_questions = cq_intake.get("questions") or []
        runtime_source_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
        runtime_query_bindings = (
            self._reviewed_cq_runtime_bindings(project_dir, self._read_json(runtime_source_path))
            if runtime_source_path.exists() else {}
        )
        questions = self._compile_competency_questions(
            intake_questions=intake_questions,
            cq_mode=str(cq_intake.get("mode") or "AI_GENERATED"),
            object_properties=object_properties,
            classes=classes,
            ontology_iri=iri,
            runtime_query_bindings=runtime_query_bindings,
        )
        if competency_questions is not None:
            questions = self._merge_competency_question_overrides(
                intake_questions=intake_questions,
                compiled_questions=questions,
                overrides=competency_questions,
            )
        if business_contract.enabled(state):
            try:
                business_contract.attach_contracts(classes, mappings)
            except ValueError as exc:
                raise WorkflowGateError("G-S4-INSTANCE-CONTRACT", str(exc)) from exc
        design = {
            "ontology_iri": iri,
            "version": version,
            "generation_policy": (
                "DETERMINISTIC_FROM_REVIEWED_EVIDENCE_MAPPING"
                if intake_mode == "DOCUMENT_ONLY"
                else "DETERMINISTIC_FROM_REVIEWED_MAPPING"
            ),
            "classes": classes,
            "object_properties": object_properties,
            "data_properties": data_properties,
            "constraints": constraints,
            "logical_axioms": list(logical_axioms or []),
            "competency_questions": questions,
        }
        if preview_only:
            return design
        return self.prepare_ontology_design_review(
            project_id=project_id,
            ontology_design=design,
            review_policy=review_policy,
        )

    @staticmethod
    def _cq_source_question_sha256(question: dict[str, Any]) -> str:
        return _fingerprint(
            {
                "id": str(question.get("id") or "").strip(),
                "question": str(question.get("question") or "").strip(),
                "expected": str(question.get("expected") or "").strip(),
            }
        )

    _cq_answer_contract = staticmethod(cq_answers.cq_answer_contract)

    _normalize_required_business_dimensions = staticmethod(cq_answers.normalize_required_business_dimensions)

    _normalize_cq_result_assertions = staticmethod(cq_answers.normalize_cq_result_assertions)

    _derive_cq_result_assertions = staticmethod(cq_answers.derive_cq_result_assertions)

    _cq_required_sparql_fragments = staticmethod(cq_answers.cq_required_sparql_fragments)

    _cq_explicitly_requires_ng = staticmethod(cq_answers.cq_explicitly_requires_ng)

    @staticmethod
    def _compile_competency_questions(
        *,
        intake_questions: list[dict[str, Any]],
        cq_mode: str,
        object_properties: list[dict[str, Any]],
        classes: list[dict[str, Any]],
        ontology_iri: str,
        runtime_query_bindings: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """把业务人员的自然语言验收题编译为可执行的只读 SPARQL。"""

        normalized_mode = str(cq_mode or "AI_GENERATED").upper()
        class_labels = {
            str(item.get("iri") or ""): str(item.get("label_zh") or item.get("name") or "")
            for item in classes
        }

        def property_terms(prop: dict[str, Any]) -> set[str]:
            values = {
                str(prop.get("name") or ""),
                str(prop.get("label_zh") or ""),
                class_labels.get(str(prop.get("domain") or ""), ""),
                class_labels.get(str(prop.get("range") or ""), ""),
                str(prop.get("domain") or "").split("#")[-1],
                str(prop.get("range") or "").split("#")[-1],
            }
            return {value for value in values if len(value) >= 2}

        compiled: list[dict[str, Any]] = []
        matched_property_iris: set[str] = set()
        semantic_review_terms = {
            "如何",
            "流程",
            "材料",
            "渠道",
            "条件",
            "条款",
            "标准",
            "原因",
            "依据",
            "政策",
            "优惠",
        }
        for index, item in enumerate(intake_questions, start=1):
            question = str(item.get("question") or "").strip()
            expected = str(item.get("expected") or "").strip()
            context = f"{question} {expected} {' '.join(item.get('example_entities') or [])}"
            source_question_id = str(item.get("id") or "").strip()
            runtime_binding = dict((runtime_query_bindings or {}).get(source_question_id) or {})
            ranked = sorted(
                (
                    (sum(term in context for term in property_terms(prop)), prop)
                    for prop in object_properties
                ),
                key=lambda pair: pair[0],
                reverse=True,
            )
            score, prop = ranked[0] if ranked else (0, None)
            if runtime_binding:
                sparql = str(runtime_binding["sparql"])
                coverage_status = "DIRECT"
                generation_note = (
                    "已复用 S3 正式运行时查询 "
                    f"{runtime_binding['query_name']}；业务边界与发布查询保持一致。"
                )
                try:
                    reviewed_cq = compile_reviewed_cq(
                        question_id=source_question_id,
                        query_name=runtime_binding["query_name"],
                        query=sparql,
                        capability=runtime_binding.get("capability") or {},
                    )
                except (CQBindingError, ValueError) as exc:
                    raise WorkflowGateError("G-S4-CQ-BINDING", str(exc)) from exc
                if reviewed_cq:
                    if runtime_binding.get("reasoning_source_sha256"):
                        reviewed_cq["answer_contract"]["reviewed_runtime_binding"][
                            "reasoning_source_sha256"
                        ] = runtime_binding["reasoning_source_sha256"]
                    if runtime_binding.get("document_source_sha256"):
                        reviewed_cq["answer_contract"]["reviewed_runtime_binding"][
                            "document_source_sha256"
                        ] = runtime_binding["document_source_sha256"]
                    sparql = reviewed_cq["sparql"]
            elif prop is not None and score > 0:
                sparql = (
                    "SELECT ?source ?target WHERE { "
                    f"?source <{prop['iri']}> ?target . "
                    "} LIMIT 100"
                )
                coverage_status = "DIRECT"
                matched_property_iris.add(str(prop["iri"]))
                generation_note = "已根据正式 Mapping 中与问题最相关的业务关系生成查询。"
            else:
                ranked_classes = sorted(
                    (
                        (
                            sum(
                                term in context
                                for term in {
                                    str(candidate.get("name") or ""),
                                    str(candidate.get("label_zh") or ""),
                                }
                                if len(term) >= 2
                            ),
                            candidate,
                        )
                        for candidate in classes
                    ),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                class_score, matched_class = ranked_classes[0] if ranked_classes else (0, None)
                if matched_class is not None and class_score > 0:
                    sparql = (
                        f"SELECT ?item WHERE {{ ?item a <{matched_class['iri']}> . }} LIMIT 100"
                    )
                    coverage_status = "DIRECT"
                    generation_note = "已根据正式 Mapping 中与问题最相关的业务类生成查询。"
                else:
                    sparql = (
                        "SELECT ?subject ?predicate ?object WHERE { "
                        "?subject ?predicate ?object . "
                        f'FILTER(STRSTARTS(STR(?predicate), "{ontology_iri}#")) '
                        "} LIMIT 100"
                    )
                    coverage_status = "NEEDS_REVIEW"
                    generation_note = "未找到唯一对应关系，已生成本体范围查询；S4 必须人工复核。"
            if any(term in context for term in semantic_review_terms):
                coverage_status = "NEEDS_REVIEW"
                generation_note += (
                    " 当前查询只证明结果形状；流程、材料、条件等业务语义仍需人工确认答案契约。"
                )
            compiled.append(
                {
                    "id": f"CQ-{index:03d}",
                    "question": question,
                    "sparql": sparql,
                    "expected": expected,
                    "source": "USER_PROVIDED",
                    "source_question_id": source_question_id,
                    "source_question_sha256": OntologyWorkflowService._cq_source_question_sha256(
                        item
                    ),
                    "priority": item.get("priority", "MEDIUM"),
                    "example_entities": list(item.get("example_entities") or []),
                    "coverage_status": coverage_status,
                    "generation_note": generation_note,
                }
            )
            if runtime_binding and reviewed_cq:
                compiled[-1]["answer_contract"] = reviewed_cq["answer_contract"]

        # USER_PLUS_AI may suggest additional questions in the UI, but an
        # unreviewed relation query has no concrete result/boundary assertions
        # or evidence-bound business dimensions.  Promoting such a suggestion
        # into the formal S4 contract creates exactly the kind of CQ false
        # positive that the production gates prohibit.  Only the no-user-input
        # AI_GENERATED mode may synthesize the formal starting set here; user
        # questions remain the complete lineage for USER_PLUS_AI until an
        # explicit, fully contracted supplement is submitted as an override.
        if normalized_mode == "AI_GENERATED":
            candidates = [
                prop
                for prop in object_properties
                if str(prop.get("iri") or "") not in matched_property_iris
            ]
            for prop in candidates[: max(0, 5 - len(compiled))]:
                index = len(compiled) + 1
                compiled.append(
                    {
                        "id": f"CQ-{index:03d}",
                        "question": (
                            f"查询 {class_labels.get(str(prop.get('domain')), str(prop.get('domain')).split('#')[-1])}"
                            f"与 {class_labels.get(str(prop.get('range')), str(prop.get('range')).split('#')[-1])}"
                            f"之间的{prop.get('label_zh') or prop.get('name')}关系。"
                        ),
                        "sparql": (
                            "SELECT ?source ?target WHERE { "
                            f"?source <{prop['iri']}> ?target . "
                            "} LIMIT 100"
                        ),
                        "expected": "返回由正式 Mapping 物化得到的业务对象关系；没有实例时返回空集而不是报错。",
                        "source": "AI_SUPPLEMENT" if intake_questions else "AI_GENERATED",
                        "coverage_status": "DIRECT",
                        "generation_note": "由正式 Mapping 确定性生成。",
                    }
                )

        if not compiled and classes:
            first_class = classes[0]
            compiled.append(
                {
                    "id": "CQ-001",
                    "question": f"系统中有哪些{first_class.get('label_zh') or first_class.get('name')}？",
                    "sparql": (
                        f"SELECT ?item WHERE {{ ?item a <{first_class['iri']}> . }} LIMIT 100"
                    ),
                    "expected": "返回该业务对象的真实实例；没有实例时返回空集而不是报错。",
                    "source": "AI_GENERATED",
                    "coverage_status": "DIRECT",
                    "generation_note": "由正式 Mapping 中的业务类确定性生成。",
                }
            )
        return compiled

    @staticmethod
    def _merge_competency_question_overrides(
        *,
        intake_questions: list[dict[str, Any]],
        compiled_questions: list[dict[str, Any]],
        overrides: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Apply optional S4 query overrides without replacing or orphaning S0 questions."""

        if not isinstance(overrides, list):
            raise WorkflowGateError("G-S4-CQ", "competency_questions 必须是数组。")
        merged = [dict(question) for question in compiled_questions]
        intake_by_id = {
            str(question.get("id") or "").strip(): question
            for question in intake_questions
            if str(question.get("id") or "").strip()
        }
        compiled_by_id = {
            str(question.get("id") or "").strip(): index
            for index, question in enumerate(merged)
            if str(question.get("id") or "").strip()
        }
        compiled_by_source_id = {
            str(question.get("source_question_id") or "").strip(): index
            for index, question in enumerate(merged)
            if str(question.get("source_question_id") or "").strip()
        }
        compiled_by_business_text = {
            (
                str(question.get("question") or "").strip(),
                str(question.get("expected") or "").strip(),
            ): index
            for index, question in enumerate(merged)
        }
        overridden_indexes: set[int] = set()
        for position, override in enumerate(overrides, start=1):
            if not isinstance(override, dict):
                raise WorkflowGateError(
                    "G-S4-CQ", f"第 {position} 个 competency_questions 覆盖项必须是对象。"
                )
            override_id = str(override.get("id") or "").strip()
            source_id = str(override.get("source_question_id") or "").strip()
            business_key = (
                str(override.get("question") or "").strip(),
                str(override.get("expected") or "").strip(),
            )
            matched_index = compiled_by_source_id.get(source_id) if source_id else None
            if matched_index is None and override_id:
                matched_index = compiled_by_id.get(override_id)
            if matched_index is None and override_id:
                matched_index = compiled_by_source_id.get(override_id)
            if matched_index is None and all(business_key):
                matched_index = compiled_by_business_text.get(business_key)
            if matched_index is None:
                merged.append(dict(override))
                continue
            if matched_index in overridden_indexes:
                raise WorkflowGateError(
                    "G-S4-CQ",
                    f"S0 业务问题 {merged[matched_index].get('source_question_id')} 存在重复覆盖。",
                )
            overridden_indexes.add(matched_index)
            base = merged[matched_index]
            source_question_id = str(base.get("source_question_id") or "").strip()
            intake_question = intake_by_id.get(source_question_id)
            combined = {**base, **override}
            if intake_question is not None:
                combined["source"] = str(intake_question.get("source") or "USER_PROVIDED")
                combined["source_question_id"] = source_question_id
                combined["source_question_sha256"] = (
                    OntologyWorkflowService._cq_source_question_sha256(intake_question)
                )
                for field in ("question", "expected", "priority", "example_entities"):
                    if override.get(field) in (None, ""):
                        combined[field] = intake_question.get(field)
            merged[matched_index] = combined
        return merged

    def _complete_review_questions(
        self,
        draft: dict[str, Any],
        questions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        existing = {str(item.get("id")): item for item in draft.get("competency_questions") or []}
        prepared: list[dict[str, Any]] = []
        needs_compilation: list[dict[str, Any]] = []
        for index, item in enumerate(questions, start=1):
            candidate = dict(item)
            candidate.setdefault("id", f"CQ-{index:03d}")
            previous = existing.get(str(candidate.get("id")))
            if previous:
                for field in (
                    "source",
                    "source_question_id",
                    "source_question_sha256",
                    "priority",
                    "example_entities",
                    "coverage_status",
                    "generation_note",
                    "answer_contract",
                ):
                    candidate.setdefault(field, previous.get(field))
            unchanged_business_question = bool(previous) and all(
                str(candidate.get(field) or "").strip() == str(previous.get(field) or "").strip()
                for field in ("question", "expected")
            )
            if (
                not str(candidate.get("sparql") or "").strip()
                and previous
                and (unchanged_business_question or previous.get("answer_contract"))
            ):
                # A wording-only review must not silently recompile a different
                # technical query or lose its approved semantic assertions.
                candidate["sparql"] = previous.get("sparql")
                candidate["answer_contract"] = previous.get("answer_contract")
            if str(candidate.get("sparql") or "").strip():
                deterministic = self._compile_competency_questions(
                    intake_questions=[candidate],
                    cq_mode="USER_PROVIDED",
                    object_properties=draft.get("object_properties") or [],
                    classes=draft.get("classes") or [],
                    ontology_iri=str(draft.get("ontology_iri") or "").rstrip("#/"),
                )[0]
                supplied_query = " ".join(str(candidate["sparql"]).split())
                deterministic_query = " ".join(str(deterministic["sparql"]).split())
                if supplied_query == deterministic_query:
                    candidate["coverage_status"] = deterministic["coverage_status"]
                    candidate["generation_note"] = deterministic["generation_note"]
                else:
                    candidate["coverage_status"] = "NEEDS_REVIEW"
                    candidate["generation_note"] = (
                        "显式查询与服务端根据正式 Mapping 编译的查询不同；"
                        "必须由负责人确认它确实回答业务问题。"
                    )
                prepared.append(candidate)
            else:
                needs_compilation.append(candidate)
        for candidate in needs_compilation:
            generated = self._compile_competency_questions(
                intake_questions=[candidate],
                cq_mode="USER_PROVIDED",
                object_properties=draft.get("object_properties") or [],
                classes=draft.get("classes") or [],
                ontology_iri=str(draft.get("ontology_iri") or "").rstrip("#/"),
            )[0]
            generated["id"] = candidate["id"]
            for field in (
                "source",
                "source_question_id",
                "source_question_sha256",
                "priority",
                "example_entities",
                "answer_contract",
            ):
                if candidate.get(field) not in (None, ""):
                    generated[field] = candidate[field]
            if candidate.get("source_question_id") in (None, ""):
                generated.pop("source_question_id", None)
                generated.pop("source_question_sha256", None)
            prepared.append(generated)
        return prepared

    def prepare_ontology_design_review(
        self,
        *,
        project_id: str,
        ontology_design: dict[str, Any],
        review_policy: str = "HUMAN_REQUIRED",
        expected_revision: int | None = None,
        _replacing_pending_review: bool = False,
    ) -> dict[str, Any]:
        """保存 S4 设计草案，并按指定策略完成人工或自动业务问题评审。"""

        normalized_review_policy = review_policy.strip().upper()
        if normalized_review_policy not in {"HUMAN_REQUIRED", "AUTO_APPROVE_EVIDENCE_BACKED"}:
            raise WorkflowError(
                "review_policy 只能是 HUMAN_REQUIRED 或 AUTO_APPROVE_EVIDENCE_BACKED。"
            )

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            self._start_s4_if_ready(project_dir, state)
            replacing_joint_review = (
                _replacing_pending_review
                and self._joint_design_enabled(state)
                and state.get("current_stage") == "S4"
                and state["stage_statuses"].get("S4") == "BLOCKED_HUMAN"
                and (state.get("blocking") or {}).get("type") == "COMPETENCY_QUESTION_REVIEW"
            )
            if not replacing_joint_review:
                project_dir, state = self._require_stage(project_id, "S4")
            ontology_design = self._normalize_ontology_design_localization(
                project_dir,
                ontology_design,
            )
            ontology_design = {
                **ontology_design,
                "competency_questions": self._complete_review_questions(
                    ontology_design,
                    ontology_design.get("competency_questions") or [],
                ),
            }
            self._reject_identical_failed_submission(state, "S4", ontology_design)
            try:
                self._validate_s4(project_dir, ontology_design)
                questions = self._validate_competency_questions(
                    ontology_design.get("competency_questions") or []
                )
            except WorkflowGateError as exc:
                if not replacing_joint_review:
                    self._mark_failed(
                        project_dir, state, "S4", exc, input_payload=ontology_design,
                    )
                raise
            design = {**ontology_design, "competency_questions": questions}
            design = canonicalize_design(design)
            self._validate_s4(project_dir, design)
            joint_review = self._joint_design_enabled(state)
            joint = None
            if joint_review:
                try:
                    joint = prepare_joint_design(project_dir, design)
                except ValueError as exc:
                    raise WorkflowGateError("G-S4-JOINT-DESIGN", str(exc)) from exc
            stage_dir = project_dir / "04-ontology-design"
            stage_dir.mkdir(parents=True, exist_ok=True)
            previous_path = stage_dir / "competency-question-review.json"
            previous_review = self._read_json(previous_path) if previous_path.is_file() else {}
            if replacing_joint_review or previous_review.get("status") == "APPROVED":
                self._write_json(
                    stage_dir / "review-history" / f"{uuid.uuid4().hex}.json",
                    previous_review,
                )
            self._write_yaml(stage_dir / "ontology-design-draft.yaml", design)
            review = {
                "stage": "S4",
                "gate": "GATE-S4-BUSINESS-QUESTIONS",
                "status": "PENDING",
                "prepared_at": _now(),
                "instructions": (
                    "这些问题用于验收本体是否真正解决业务需求。可以采用建议、修改文字和预期结果，"
                    "也可以增删问题；确认前不会进入 S5。"
                ),
                "draft_questions": questions,
                "draft_questions_sha256": _fingerprint(questions),
            }
            if joint_review:
                assert joint is not None
                self._write_json(stage_dir / "joint-design-draft.json", joint)
                review.update({
                    "review_scope": "JOINT_DESIGN",
                    "joint_design_fingerprint": joint["joint_design_fingerprint"],
                    "design_sha256": joint["design_sha256"],
                    "source_artifacts": joint["source_artifacts"],
                    "joint_design_summary": joint["joint_design_summary"],
                    "instructions": (
                        "请确认完整业务设计：本体概念、关系、正式映射、业务规则、来源范围及验收预期。"
                        + (
                            "批准理由须逐项写明待评审规则："
                            + "、".join(joint["joint_design_summary"]["pending_rule_review_ids"])
                            + "。"
                            if joint["joint_design_summary"]["pending_rule_review_ids"] else ""
                        )
                        + "批准后统一冻结；发布另行批准。"
                    ),
                })
                self._mark_artifacts_regenerated(state, {"04-ontology-design/joint-design-draft.json"})
            requires_human_review = joint_review or any(
                item.get("coverage_status") != "DIRECT" for item in questions
            )
            if (
                normalized_review_policy == "AUTO_APPROVE_EVIDENCE_BACKED"
                and not requires_human_review
            ):
                review.update(
                    {
                        "status": "APPROVED",
                        "decision": "APPROVED",
                        "approval_mode": "AUTOMATIC_POLICY",
                        "decided_by": "ORION_POLICY",
                        "rationale": (
                            "业务问题由已评审 Mapping 确定性生成，且全部通过可执行性和证据门禁；"
                            "按最小确认策略自动批准，发布仍需负责人明确确认。"
                        ),
                        "decided_at": _now(),
                        "approved_questions": questions,
                        "approved_questions_sha256": _fingerprint(questions),
                        "difference": {
                            "before_count": len(questions),
                            "after_count": len(questions),
                            "changed_item_count": 0,
                        },
                    }
                )
            self._write_json(stage_dir / "competency-question-review.json", review)
            self._mark_artifacts_regenerated(
                state,
                {
                    "04-ontology-design/README.md",
                    "04-ontology-design/ontology-design-draft.yaml",
                    "04-ontology-design/competency-question-review.json",
                },
            )
            if (
                normalized_review_policy == "AUTO_APPROVE_EVIDENCE_BACKED"
                and not requires_human_review
            ):
                self._save_state(project_dir, state)
                self._append_jsonl(
                    stage_dir / "decisions.jsonl",
                    {
                        "decision_id": f"S4-CQ-AUTO-{uuid.uuid4().hex[:8].upper()}",
                        "item_id": "S4-BUSINESS-QUESTIONS",
                        "decision": "APPROVED",
                        "decided_by": "ORION_POLICY",
                        "rationale": review["rationale"],
                        "before": questions,
                        "after": questions,
                        "decided_at": review["decided_at"],
                    },
                )
                self._append_event(
                    project_dir,
                    "COMPETENCY_QUESTION_REVIEW_AUTO_APPROVED",
                    state,
                    {
                        "stage": "S4",
                        "question_count": len(questions),
                        "actor": "ORION_POLICY",
                    },
                )
                return self.record_ontology_design(
                    project_id=project_id,
                    ontology_design=design,
                )

            state["stage_statuses"]["S4"] = "BLOCKED_HUMAN"
            state["project_status"] = "BLOCKED_HUMAN"
            state["blocking"] = {
                "stage": "S4",
                "gate": "GATE-S4-BUSINESS-QUESTIONS",
                "type": "COMPETENCY_QUESTION_REVIEW",
                "reason": "等待负责人确认本体、映射、规则与验收预期的联合设计。" if joint_review else "等待负责人确认本体必须回答的业务问题。",
                "review_scope": "JOINT_DESIGN" if joint_review else "COMPETENCY_QUESTIONS",
            }
            state["resume_point"] = "S4: 确认联合设计后冻结本体、映射、规则和验收预期" if joint_review else "S4: 修改或确认业务问题后冻结本体施工图"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "COMPETENCY_QUESTION_REVIEW_REQUIRED",
                state,
                {
                    "stage": "S4",
                    "question_count": len(questions),
                    "actor": "ORION_WORKFLOW",
                },
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def resolve_competency_question_review(
        self,
        *,
        project_id: str,
        questions: list[dict[str, Any]],
        decision: str,
        decided_by: str,
        rationale: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """确认或退回 S4 业务问题；批准后才生成正式本体施工图。"""

        normalized_decision = decision.strip().upper()
        if normalized_decision not in {"APPROVED", "RETURN_TO_S3"}:
            raise WorkflowError("decision 只能是 APPROVED 或 RETURN_TO_S3。")
        if not decided_by.strip() or not rationale.strip():
            raise WorkflowError("decided_by 和 rationale 都不能为空。")

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            self._require_expected_revision(state, expected_revision)
            stage_dir = project_dir / "04-ontology-design"
            review_path = stage_dir / "competency-question-review.json"
            draft_path = stage_dir / "ontology-design-draft.yaml"
            if (
                normalized_decision == "APPROVED"
                and state.get("current_stage") == "S4"
                and state.get("stage_statuses", {}).get("S4") == "RUNNING"
                and review_path.is_file() and draft_path.is_file()
            ):
                approved = self._read_json(review_path)
                if approved.get("status") == "APPROVED" and approved.get("approval_mode") == "HUMAN":
                    if self._joint_design_enabled(state) and expected_revision is None:
                        raise WorkflowGateError("G-S4-JOINT-DESIGN", "恢复批准必须绑定当前工程 revision。")
                    saved_questions = approved.get("approved_questions") or []
                    fields = ("id", "question", "expected")
                    if questions and [tuple(q.get(k) for k in fields) for q in questions] != [
                        tuple(q.get(k) for k in fields) for q in saved_questions
                    ]:
                        raise WorkflowGateError("G-S4-JOINT-DESIGN", "恢复请求与已批准业务问题不一致。")
                    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
                    return self.record_ontology_design(project_id=project_id, ontology_design=draft)
            if (
                state.get("current_stage") != "S4"
                or state.get("stage_statuses", {}).get("S4") != "BLOCKED_HUMAN"
                or state.get("blocking", {}).get("type") != "COMPETENCY_QUESTION_REVIEW"
            ):
                raise WorkflowError("项目当前不在 S4 业务问题人工确认关口。")
            if not review_path.exists() or not draft_path.exists():
                raise WorkflowError("S4 业务问题草案不完整，请重新生成本体设计草案。")
            review = self._read_json(review_path)

            if self._joint_design_enabled(state) and expected_revision is None:
                raise WorkflowGateError("G-S4-JOINT-DESIGN", "联合设计决定必须绑定当前工程 revision。")

            if normalized_decision == "RETURN_TO_S3":
                review.update(
                    {
                        "status": "RETURNED",
                        "decision": normalized_decision,
                        "decided_by": decided_by.strip(),
                        "rationale": rationale.strip(),
                        "decided_at": _now(),
                    }
                )
                self._write_json(review_path, review)
                self._append_jsonl(
                    stage_dir / "decisions.jsonl",
                    {
                        "decision_id": f"S4-CQ-{uuid.uuid4().hex[:8].upper()}",
                        "item_id": "S4-BUSINESS-QUESTIONS",
                        "decision": normalized_decision,
                        "decided_by": decided_by.strip(),
                        "rationale": rationale.strip(),
                        "decided_at": review["decided_at"],
                    },
                )
                return self._apply_stage_rollback(
                    project_dir,
                    state,
                    stage="S3",
                    reason=f"S4 业务问题评审退回：{rationale.strip()}",
                    requested_by=decided_by.strip(),
                    rollback_origin="HUMAN_REVIEW_REJECTION",
                )

            draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
            if self._joint_design_enabled(state):
                try:
                    verify_joint_review(project_dir, draft, review)
                except ValueError as exc:
                    raise WorkflowGateError("G-S4-JOINT-DESIGN", str(exc)) from exc
            normalized_questions = self._validate_competency_questions(
                self._complete_review_questions(draft, questions)
            )
            before = review.get("draft_questions") or []
            before_by_id = {str(item.get("id")): item for item in before}
            after_by_id = {str(item.get("id")): item for item in normalized_questions}
            changed = sum(
                before_by_id.get(question_id) != after_by_id.get(question_id)
                for question_id in set(before_by_id) | set(after_by_id)
            )
            if self._joint_design_enabled(state) and (changed or before != normalized_questions):
                # Changed questions may change the meaning and coverage of the
                # design. Recompile/validate a new review instead of transferring
                # the approval of the previous complete design to it.
                draft["competency_questions"] = normalized_questions
                return self.prepare_ontology_design_review(
                    project_id=project_id,
                    ontology_design=draft,
                    review_policy="HUMAN_REQUIRED",
                    expected_revision=expected_revision,
                    _replacing_pending_review=True,
                )
            review.update(
                {
                    "status": "APPROVED",
                    "approval_mode": "HUMAN",
                    "decision": normalized_decision,
                    "decided_by": decided_by.strip(),
                    "rationale": rationale.strip(),
                    "decided_at": _now(),
                    "approved_questions": normalized_questions,
                    "approved_questions_sha256": _fingerprint(normalized_questions),
                    "difference": {
                        "before_count": len(before),
                        "after_count": len(normalized_questions),
                        "changed_item_count": changed,
                    },
                }
            )
            draft["competency_questions"] = normalized_questions
            if self._joint_design_enabled(state):
                try:
                    validate_joint_approval(project_dir, draft, review)
                except ValueError as exc:
                    raise WorkflowGateError("G-S4-JOINT-DESIGN", str(exc)) from exc
            self._validate_s4(project_dir, draft)
            self._write_json(review_path, review)
            self._append_jsonl(
                stage_dir / "decisions.jsonl",
                {
                    "decision_id": f"S4-CQ-{uuid.uuid4().hex[:8].upper()}",
                    "item_id": "S4-BUSINESS-QUESTIONS",
                    "decision": normalized_decision,
                    "decided_by": decided_by.strip(),
                    "rationale": rationale.strip(),
                    "before": before,
                    "after": normalized_questions,
                    "decided_at": review["decided_at"],
                },
            )
            state["stage_statuses"]["S4"] = "RUNNING"
            state["project_status"] = "IN_PROGRESS"
            state["blocking"] = None
            state["resume_point"] = "S4: 依据已确认业务问题冻结本体施工图"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "COMPETENCY_QUESTION_REVIEW_APPROVED",
                state,
                {
                    "stage": "S4",
                    "question_count": len(normalized_questions),
                    "changed_item_count": changed,
                    "actor": decided_by.strip(),
                },
            )
            return self.record_ontology_design(project_id=project_id, ontology_design=draft)

    def preview_stage_rollback(
        self,
        *,
        project_id: str,
        target_stage: str,
        requested_by: str | None = None,
        changed_components: list[str] | None = None,
    ) -> dict[str, Any]:
        """只读计算回退影响，并签发绑定当前工程 revision 的一次性确认令牌。"""

        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            try:
                components = normalize_changed_components(changed_components)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            impact = self._stage_rollback_impact(
                project_dir,
                state,
                target_stage,
                changed_components=components,
            )
            preview_id = f"RBP-{uuid.uuid4().hex[:16].upper()}"
            secret = secrets.token_urlsafe(32)
            token = f"{preview_id}.{secret}"
            expires_at = (datetime.now().astimezone() + timedelta(minutes=10)).isoformat(
                timespec="seconds"
            )
            preview = {
                **impact,
                "preview_id": preview_id,
                "project_id": project_id,
                "project_revision": int(state.get("revision") or 0),
                "preview_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "expires_at": expires_at,
                "used_at": None,
                "created_at": _now(),
                "requested_by": str(requested_by or "ORION_WORKFLOW"),
            }
            preview_dir = project_dir / ".operation-previews"
            preview_dir.mkdir(exist_ok=True)
            self._write_json(preview_dir / f"{preview_id}.json", preview)
            return {
                key: value for key, value in preview.items() if key != "preview_token_sha256"
            } | {"preview_token": token}

    def reopen_stage_for_correction(
        self,
        *,
        project_id: str,
        stage: str,
        reason: str,
        requested_by: str,
        preview_token: str,
        project_revision: int,
        changed_components: list[str] | None = None,
    ) -> dict[str, Any]:
        """校验一次性预览令牌后，留痕回退并使已有下游结果失效。"""

        if not reason.strip():
            raise WorkflowError("回退原因不能为空。")
        if not requested_by.strip():
            raise WorkflowError("requested_by 不能为空。")
        if not preview_token.strip():
            raise WorkflowError("缺少阶段回退 preview token。")
        with self._project_mutation_lock(project_id) as project_dir:
            state = self._read_state(project_dir)
            preview_id = preview_token.split(".", 1)[0]
            preview_path = project_dir / ".operation-previews" / f"{preview_id}.json"
            if not preview_path.is_file():
                raise WorkflowError("阶段回退 preview token 无效或已不存在。")
            preview = self._read_json(preview_path)
            if preview.get("used_at"):
                raise WorkflowError("阶段回退 preview token 已使用，重复提交已拒绝。")
            if not secrets.compare_digest(
                str(preview.get("preview_token_sha256") or ""),
                hashlib.sha256(preview_token.encode("utf-8")).hexdigest(),
            ):
                raise WorkflowError("阶段回退 preview token 校验失败。")
            if datetime.fromisoformat(str(preview["expires_at"])) <= datetime.now().astimezone():
                raise WorkflowError("阶段回退 preview token 已过期，请重新预览影响。")
            if preview.get("project_id") != project_id or preview.get("target_stage") != stage:
                raise WorkflowError("阶段回退目标与 preview 不一致。")
            try:
                components = normalize_changed_components(changed_components)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            if list(components) != list(preview.get("changed_components") or []):
                raise WorkflowError("changed_components 与回退预览不一致。")
            preview_revision = int(preview.get("project_revision") or 0)
            if project_revision != preview_revision:
                raise WorkflowError("提交的 project revision 与 preview 不一致。")
            self._require_expected_revision(state, project_revision)
            if preview.get("commit_allowed") is not True:
                raise WorkflowError(
                    str(preview.get("release_impact", {}).get("message") or "当前状态不允许回退。")
                )
            result = self._apply_stage_rollback(
                project_dir,
                state,
                stage=stage,
                reason=reason.strip(),
                requested_by=requested_by.strip(),
                rollback_origin="PREVIEW_CONFIRMED",
                preview_id=preview_id,
                changed_components=components,
            )
            preview["used_at"] = _now()
            preview["committed_revision"] = result.get("revision")
            self._write_json(preview_path, preview)
            return result

    def _apply_stage_rollback(
        self,
        project_dir: Path,
        state: dict[str, Any],
        *,
        stage: str,
        reason: str,
        requested_by: str,
        rollback_origin: str,
        preview_id: str | None = None,
        changed_components: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        impact = self._stage_rollback_impact(
            project_dir,
            state,
            stage,
            changed_components=changed_components,
        )
        if impact["commit_allowed"] is not True:
            raise WorkflowError(str(impact["release_impact"]["message"]))
        target_index = STAGES.index(stage)
        required_revalidation = [
            stage,
            *[
                item["stage"]
                for item in impact["affected_downstream"]
                if item["disposition"] in {"INVALIDATED", "PENDING_UPSTREAM"}
                and state["stage_statuses"].get(item["stage"]) != "NOT_APPLICABLE"
            ],
        ]
        revision = self._create_revision_snapshot(
            project_dir,
            state,
            target_stage=stage,
            reason=reason,
            requested_by=requested_by,
            required_revalidation=required_revalidation,
        )
        artifact_lifecycle = dict(state.get("artifact_lifecycle") or {})
        for item in impact["artifact_impact"]["current_artifacts"]:
            artifact_lifecycle[item["path"]] = {
                "status": "INVALIDATED",
                "stage": item["stage"],
                "invalidated_at": _now(),
                "invalidated_by_revision": revision["revision_id"],
                "historical_snapshot": f"revisions/{revision['revision_id']}/before/{item['path']}",
            }
        state["artifact_lifecycle"] = artifact_lifecycle
        if self._joint_design_enabled(state) and (
            stage == "S3" or any(
                item["stage"] == "S3" and item["disposition"] != "REUSED_UNCHANGED"
                for item in impact["affected_downstream"]
            )
        ):
            state["s3_runtime_review"] = {
                "status": "REVALIDATION_REQUIRED",
                "validated": False,
                "message": "上游语义或映射已重开；旧运行评审已失效，必须重新核对业务口径并完整预检。",
            }
        state["stage_statuses"][stage] = "RUNNING"
        state.get("stage_fingerprints", {}).pop(stage, None)
        for item in impact["affected_downstream"]:
            downstream = item["stage"]
            if item["disposition"] == "INVALIDATED":
                state["stage_statuses"][downstream] = "INVALIDATED"
                state.get("stage_fingerprints", {}).pop(downstream, None)
            elif (
                item["disposition"] == "PENDING_UPSTREAM"
                and STAGES.index(downstream) > target_index
                and state["stage_statuses"].get(downstream) != "NOT_APPLICABLE"
            ):
                state["stage_statuses"][downstream] = "PENDING"
        state["current_stage"] = stage
        state["project_status"] = "IN_PROGRESS"
        state["blocking"] = None
        state["last_error"] = None
        state["active_revision"] = {
            "revision_id": revision["revision_id"],
            "target_stage": stage,
            "required_revalidation_stages": required_revalidation,
            "status": "IN_PROGRESS",
        }
        state["resume_point"] = f"{stage}: 因‘{reason}’重新生成并验证正式资产"
        self._save_state(project_dir, state)
        self._append_event(
            project_dir,
            "STAGE_ROLLBACK_COMMITTED",
            state,
            {
                "stage": stage,
                "reason": reason,
                "requested_by": requested_by,
                "revision_id": revision["revision_id"],
                "required_revalidation_stages": required_revalidation,
                "invalidated_stages": [
                    item["stage"]
                    for item in impact["affected_downstream"]
                    if item["disposition"] == "INVALIDATED"
                ],
                "preview_id": preview_id,
                "rollback_origin": rollback_origin,
                "changed_components": list(changed_components),
                "dependency_policy_version": (impact.get("dependency_plan") or {}).get(
                    "policy_version"
                ),
                "actor": requested_by,
            },
        )
        self._append_event(
            project_dir,
            "STAGE_REOPENED_FOR_CORRECTION",
            state,
            {
                "stage": stage,
                "reason": reason,
                "requested_by": requested_by,
                "revision_id": revision["revision_id"],
                "required_revalidation_stages": required_revalidation,
                "actor": requested_by,
            },
        )
        self._refresh_manifest(project_dir)
        return self._status_payload(project_dir, state)

    def record_ontology_build(
        self,
        *,
        project_id: str,
        ontology_owl: str,
        ontology_ttl: str,
        shapes_ttl: str,
        protege_build_report: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """记录 Protégé 构建产物；服务端重新解析并核对设计覆盖。"""

        payload = {
            "ontology_owl": ontology_owl,
            "ontology_ttl": ontology_ttl,
            "shapes_ttl": shapes_ttl,
            "protege_build_report": protege_build_report,
        }
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S5")
            self._require_expected_revision(state, expected_revision)
            self._reject_identical_failed_submission(state, "S5", payload)
            try:
                build_metrics = self._validate_s5(project_dir, payload)
            except WorkflowGateError as exc:
                self._mark_failed(project_dir, state, "S5", exc, input_payload=payload)
                raise

            stage_dir = project_dir / "05-ontology-build"
            stage_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(stage_dir / "ontology.owl", ontology_owl)
            self._atomic_write(stage_dir / "ontology.ttl", ontology_ttl)
            self._atomic_write(stage_dir / "shapes.ttl", shapes_ttl)
            rule_review_regeneration = None
            if self._joint_design_enabled(state):
                try:
                    rule_review_regeneration = regenerate_s5_rule_review_assets(
                        project_dir / "04-ontology-design/joint-design-inputs/03-mapping-review/runtime",
                        stage_dir / "rule-review", project_id=project_id,
                        source_rules_path=project_dir / "02-semantic-recognition/business-rule-candidates.json",
                    )
                except DeliveryPackageError as exc:
                    raise WorkflowGateError("G-S5-RULE-DELIVERY", str(exc)) from exc
                self._mark_artifacts_regenerated(state, {
                    path.relative_to(project_dir).as_posix()
                    for path in (stage_dir / "rule-review").rglob("*") if path.is_file()
                })
            self._write_json(
                stage_dir / "protege-build-report.json",
                {**protege_build_report, "verified_metrics": build_metrics, "recorded_at": _now(),
                 "rule_review_regeneration": rule_review_regeneration},
            )
            self._write_json(
                stage_dir / "gate-results.json",
                {
                    "stage": "S5",
                    "status": "PASSED",
                    "gates": [
                        {"id": "G-S5-PROTEGE-EVIDENCE", "status": "PASSED"},
                        {"id": "G-S5-RDF-PARSE", "status": "PASSED"},
                        {"id": "G-S5-ENTITY-TYPES", "status": "PASSED"},
                        {"id": "G-S5-DESIGN-COVERAGE", "status": "PASSED"},
                        {"id": "G-S5-CHINESE", "status": "PASSED"},
                        {"id": "G-S5-SHAPES", "status": "PASSED"},
                        {"id": "G-S5-LOGIC", "status": "PASSED"},
                        {"id": "G-S5-REASONER", "status": "PASSED"},
                        {"id": "G-S5-REASONING-TERMS", "status": "PASSED"},
                    ],
                    "metrics": build_metrics,
                    "checked_at": _now(),
                },
            )
            self._atomic_write(
                stage_dir / "ontology-build-report.html",
                render_s5_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    self._read_json(stage_dir / "protege-build-report.json"),
                    self._read_json(stage_dir / "gate-results.json"),
                ),
            )
            self._mark_artifacts_regenerated(
                state,
                {
                    "05-ontology-build/README.md",
                    "05-ontology-build/ontology.owl",
                    "05-ontology-build/ontology.ttl",
                    "05-ontology-build/shapes.ttl",
                    "05-ontology-build/protege-build-report.json",
                    "05-ontology-build/gate-results.json",
                    "05-ontology-build/ontology-build-report.html",
                },
            )
            self._pass_stage(project_dir, state, "S5", "S6", payload)
            state["resume_point"] = (
                "S6: 执行 HermiT、SHACL、Mapping、语义、CQ 与 Semantica 运行验证"
            )
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ONTOLOGY_BUILD_COMPLETED",
                state,
                {"stage": "S5", **build_metrics},
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    _preflight_issue = staticmethod(submission_validation.preflight_issue)
    _s2_rule_contract_issues = staticmethod(submission_validation.s2_rule_contract_issues)

    def _s3_downstream_contract_issues(
        self, project_dir: Path | None, state: dict[str, Any], payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Collect only deterministic build requirements, without approving semantics."""
        if self._s3_business_review_only(state, payload):
            return []
        raw_mapping = payload.get("mapping_draft")
        mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
        raw_mappings = mapping.get("mappings")
        mappings = [item for item in raw_mappings if isinstance(item, dict)] if isinstance(raw_mappings, list) else []
        issues: list[dict[str, Any]] = []
        for index, item in enumerate(mappings):
            kind = str(item.get("mapping_type") or "").upper()
            fields = ("domain", "range") if kind in {
                "COLUMN_VALUE_TO_OBJECT_PROPERTY", "SQL_TO_OBJECT_PROPERTY",
                "CANDIDATE_JOIN_TO_OBJECT_PROPERTY", "EVIDENCE_TO_OBJECT_PROPERTY",
            } else (
                ("domain",) if kind == "EVIDENCE_TO_DATA_PROPERTY" else ()
            )
            for field in fields:
                if not str(item.get(field) or "").strip():
                    issues.append(self._preflight_issue(
                        "G-S3-MAPPING", f"映射 {item.get('id', index)} 必须明确 {field}。",
                        path=f"mapping_draft.mappings[{index}].{field}",
                    ))
            if kind in DATABASE_DATA_MAPPING_TYPES | {"EVIDENCE_TO_DATA_PROPERTY"} and item.get("datatype"):
                try:
                    normalize_datatype_iri(item["datatype"])
                except ValueError as exc:
                    issues.append(self._preflight_issue(
                        "G-S3-MAPPING-DATATYPE", f"映射 {item.get('id', index)}：{exc}",
                        path=f"mapping_draft.mappings[{index}].datatype",
                    ))
        runtime = payload.get("realtime_runtime") or {}
        if not isinstance(runtime, dict) or project_dir is None:
            return issues
        from .business_runtime_validation import business_runtime_issues

        issues.extend(business_runtime_issues(project_dir, mapping, runtime, intake_mode=state.get("intake_mode", "HYBRID")))
        namespace = str(mapping.get("namespace") or _runtime_prefix_iri(str(runtime.get("mapping_obda") or ""))
                        or f"https://orion.local/ontology/{project_dir.name}#")

        def iri(value: Any) -> str:
            name = str(value or "").split(" (")[0].strip()
            return name if name.startswith(("http://", "https://")) else namespace + name

        if business_contract.enabled(state) and state.get("intake_mode") == "DOCUMENT_ONLY":
            from .document_class_coverage import missing_document_class_bindings

            for missing in missing_document_class_bindings(mappings, runtime, resolve_iri=iri):
                issues.append(self._preflight_issue(
                    "G-S3-DOCUMENT-CLASS-COVERAGE",
                    f"类 {missing['class_iri']} 要求从资料生成非空实例，但没有被实际消费的事实绑定。"
                    "请在带 CQ 绑定的文档查询或消费证据包的推理能力中补齐有来源的事实、fact_bindings 和 ontology_terms；"
                    "仅登记类、孤立事实包或修改验收报告不能产生实例。绑定齐全也仍须通过 S6 实际验收。",
                    path="realtime_runtime",
                ))

        entity_iris = {iri(item.get("target")) for item in mappings if item.get("target")}
        # Include S4's deterministic domain-scoped names for repeated data targets.
        # Final S4 still validates the precise generated declarations.
        data_mappings = [item for item in mappings if str(item.get("mapping_type") or "").upper()
                         in DATABASE_DATA_MAPPING_TYPES | {"EVIDENCE_TO_DATA_PROPERTY"}]
        table_classes = {str(item.get("source")): str(item.get("target")) for item in mappings
                         if item.get("mapping_type") == "TABLE_TO_CLASS"}
        scoped_data_iris: set[str] = set()
        for item in data_mappings:
            target = str(item.get("target") or "").split(" (")[0].strip()
            if sum(str(other.get("target") or "").split(" (")[0].strip() == target
                   for other in data_mappings) < 2:
                continue
            source_table, _ = _split_qualified_column(str(item.get("source") or ""))
            domain = str(item.get("domain") or item.get("class") or table_classes.get(source_table) or "")
            local = re.split(r"[#/]", domain.rstrip("#/"))[-1]
            if local:
                scoped_data_iris.add(iri(local[:1].lower() + local[1:] + target[:1].upper() + target[1:]))
        entity_iris.update(scoped_data_iris)
        # S4 also emits source-independent rule conclusion classes from S2.
        semantic_path = project_dir / "02-semantic-recognition/ontology-candidates.yaml"
        rules_path = project_dir / "02-semantic-recognition/business-rule-candidates.json"
        candidates = (yaml.safe_load(semantic_path.read_text()) or {}).get("candidates", []) if semantic_path.is_file() else []
        rules = self._read_json(rules_path) if rules_path.is_file() else []
        conclusions = {str(rule.get("conclusion_predicate") or "") for rule in rules}
        entity_iris.update(iri(item.get("name")) for item in candidates
                           if str(item.get("kind") or "").upper() == "CLASS"
                           and item.get("name") in conclusions)
        object_property_iris = {iri(item.get("target")) for item in mappings
                                if str(item.get("mapping_type") or "").upper() in {
                                    "COLUMN_VALUE_TO_OBJECT_PROPERTY", "SQL_TO_OBJECT_PROPERTY",
                                    "CANDIDATE_JOIN_TO_OBJECT_PROPERTY", "EVIDENCE_TO_OBJECT_PROPERTY",
                                }}
        property_iris = object_property_iris | scoped_data_iris | {iri(item.get("target")) for item in data_mappings}
        from services.realtime_qa.fact_binding_types import fact_binding_type_issues

        issues.extend(fact_binding_type_issues(runtime, {
            **dict.fromkeys(property_iris - object_property_iris, "DATA_PROPERTY"),
            **dict.fromkeys(object_property_iris, "OBJECT_PROPERTY"),
        }))
        issues.extend(cq_reach.s3_runtime_unreachable_cq_issues(runtime))
        issues.extend(self._s3_cq_contract_issues(
            project_dir, runtime, mappings, entity_iris=entity_iris,
            property_iris=property_iris, object_property_iris=object_property_iris,
            require_complete=(project_stage_contract_version(state) == STAGE_CONTRACT_VERSION
                              and str(state.get("assurance_profile") or "PRODUCTION") == "PRODUCTION"
                              and str(state.get("cq_mode") or "") in {"USER_PROVIDED", "USER_PLUS_AI"}
                              and (state.get("intake_mode") == "DOCUMENT_ONLY"
                                   or bool(runtime.get("reasoning_capabilities")))),
        ))
        capabilities = runtime.get("reasoning_capabilities")
        if capabilities is None:
            capabilities = {}
        if not isinstance(capabilities, dict):
            issues.append(self._preflight_issue(
                "G-S3-RUNTIME", "reasoning_capabilities 必须是对象。",
                path="realtime_runtime.reasoning_capabilities",
            ))
            return issues
        for name, capability in capabilities.items():
            base = f"realtime_runtime.reasoning_capabilities.{name}"
            if not isinstance(capability, dict):
                issues.append(self._preflight_issue(
                    "G-S3-RUNTIME", "推理能力必须是对象。", path=base,
                ))
                continue
            bindings = capability.get("fact_bindings")
            results = capability.get("result_predicates")
            if isinstance(bindings, list) and isinstance(results, list):
                seeded = sorted(
                    {item.get("predicate") for item in bindings if isinstance(item, dict)
                     and isinstance(item.get("predicate"), str)}
                    & {item for item in results if isinstance(item, str)}
                )
                if seeded:
                    issue = self._preflight_issue(
                        "G-S3-RUNTIME",
                        f"推理能力 {name} 将规则结论预置为输入事实：{', '.join(seeded)}。fact_bindings 只绑定有来源的前提；结论由规则产生。",
                        path=f"{base}.fact_bindings",
                    )
                    issue["reason_code"] = "RULE_CONCLUSION_AS_INPUT"
                    issues.append(issue)
            invalid_fields = []
            for field, expected_type in (("ontology_terms", dict), ("fact_bindings", list), ("result_predicates", list)):
                if field in capability and not isinstance(capability[field], expected_type):
                    invalid_fields.append(field)
                    issues.append(self._preflight_issue(
                        "G-S3-RUNTIME", f"推理能力 {field} 必须是{'对象' if expected_type is dict else '数组'}。",
                        path=f"{base}.{field}",
                    ))
            if invalid_fields:
                continue
            if any(not isinstance(value, str) for value in capability.get("result_predicates") or []):
                issues.append(self._preflight_issue(
                    "G-S3-RUNTIME", "result_predicates 必须仅包含谓词字符串。",
                    path=f"{base}.result_predicates",
                ))
                continue
            if bad := non_binary_property_bindings(capability, dict.fromkeys(property_iris, "DATA_PROPERTY")):
                issues.append(self._preflight_issue("G-S3-FACT-ARITY", f"推理能力 {name}：以下谓词映射到属性 IRI，S4 要求二元 arguments [主体, 取值]；一元条件请改映射到类或派生谓词：" + "；".join(bad), path=f"{base}.fact_bindings"))
            try:
                _validate_reasoning_term_declarations(
                    question_id=name, capability=capability,
                    allowed_predicates=set(capability.get("result_predicates") or []),
                    entity_iris=entity_iris,
                )
            except WorkflowGateError as exc:
                issues.append(self._preflight_issue(
                    "G-S3-REASONING-TERMS", str(exc),
                    path=f"realtime_runtime.reasoning_capabilities.{name}.ontology_terms",
                ))
        return issues

    def _s3_cq_contract_issues(
        self, project_dir: Path, runtime: dict[str, Any], mappings: list[dict[str, Any]],
        *, entity_iris: set[str], property_iris: set[str], object_property_iris: set[str],
        require_complete: bool = False,
    ) -> list[dict[str, Any]]:
        """Compile submitted CQ contracts with S4 validators before S3 is committed.

        Uses inline submission queries, never the previous revision's runtime files.
        This is contract validation only: no query execution or invented expectations.
        """
        from services.realtime_qa.cq_contract import normalize_cq_bindings
        from services.realtime_qa.query_capabilities import _normalize_validation_cases

        intake_path = project_dir / "00-document-evidence/cq-intake.json"
        if not intake_path.is_file():
            return []
        intake = {str(item.get("id") or ""): item
                  for item in self._read_json(intake_path).get("questions") or []}
        known_refs = {str(item.get("id") or "") for item in mappings}
        known_refs.update(str(ref) for item in mappings for ref in item.get("source_refs") or [])
        rules_path = project_dir / "02-semantic-recognition/business-rule-candidates.json"
        rules = self._read_json(rules_path) if rules_path.is_file() else []
        if isinstance(rules, dict):
            rules = rules.get("rules") or []
        from services.ontology_engineering.business_source_contract import business_evidence_refs

        answer_refs = business_evidence_refs(mappings, rules)
        issues: list[dict[str, Any]] = []
        has_explicit_contracts = False
        owners: dict[str, str] = {}
        for kind in ("query_capabilities", "reasoning_capabilities", "document_fact_queries"):
            capabilities = runtime.get(kind) or {}
            if not isinstance(capabilities, dict):
                continue  # Runtime shape validation supplies the precise diagnostic.
            for name, cap in capabilities.items():
                if not isinstance(cap, dict):
                    continue
                for index, case in enumerate(cap.get("validation_cases") or []):
                    if not isinstance(case, dict) or not isinstance(case.get("nullable_bindings", {}), dict):
                        continue  # The case normalizer reports malformed contracts.
                    if any(isinstance(item, dict) and not set(item.get("source_refs") or []).issubset(answer_refs)
                           for item in case.get("nullable_bindings", {}).values()):
                        issues.append({
                            **self._preflight_issue(
                                "G-S3-CQ-CONTRACT", "查询验收用例的条件空值引用了未知来源证据。",
                                path=f"realtime_runtime.{kind}.{name}.validation_cases.{index}.nullable_bindings",
                            ),
                            "reason_code": "UNKNOWN_NULLABLE_SOURCE_REFS",
                        })
                cq_bindings = cap.get("cq_bindings") or {}
                ids = cap.get("business_question_ids") or []
                if (not isinstance(cq_bindings, dict) or not isinstance(ids, list)
                        or any(not isinstance(qid, str) for qid in [*ids, *cq_bindings])):
                    continue
                has_explicit_contracts = has_explicit_contracts or bool(cq_bindings)
                for qid in dict.fromkeys([*ids, *cq_bindings]):
                    base = f"realtime_runtime.{kind}.{name}.cq_bindings.{qid}"
                    def add(message: str, code: str, *, path: str = base, qid: str = qid) -> None:
                        issues.append({
                            **self._preflight_issue("G-S3-CQ-CONTRACT", message, path=path),
                            "source_question_id": qid, "reason_code": code,
                            "validation_scope": "S4_CQ_CONTRACT_COMPILATION_ONLY",
                        })
                    if qid in owners:
                        add(f"业务问题 {qid} 被多个能力绑定：{owners[qid]}、{name}。", "AMBIGUOUS_CQ_BINDING")
                        continue
                    owners[qid] = str(name)
                    if qid not in intake:
                        add(f"业务问题 {qid} 未在当前 S0 登记。", "UNKNOWN_SOURCE_QUESTION")
                        continue
                    # Legacy database query associations without an explicit answer
                    # contract can still use S4's reviewed override path. Document
                    # and reasoning CQ compilation has no such fallback.
                    if qid not in cq_bindings:
                        if require_complete or kind != "query_capabilities":
                            add(f"业务问题 {qid} 缺少已审 cq_bindings；请在 S3 补齐后提交。", "MISSING_CQ_BINDING")
                        continue
                    try:
                        cases = _normalize_validation_cases(name, cap.get("validation_cases") or [], cap.get("result_fields") or [])
                        binding = normalize_cq_bindings(
                            {qid: cq_bindings[qid]}, question_ids=ids,
                            cases=cases, fields=cap.get("result_fields") or [],
                        )
                        query = ((runtime.get("ontop_queries") or {}).get(name)
                                 if kind == "query_capabilities" else cap.get("sparql"))
                        compiled = compile_reviewed_cq(
                            question_id=qid, query_name=name, query=query or "",
                            capability={**cap, "validation_cases": cases, "cq_bindings": binding},
                        )
                        question = self._validate_competency_questions([{
                            **intake[qid], **compiled, "source_question_id": qid,
                        }])[0]
                    except CQBindingError as exc:
                        path = base
                        if exc.field == "query":
                            if cq_bindings[qid].get("cq_sparql"):
                                path = base + ".cq_sparql"
                            elif kind == "query_capabilities":
                                path = f"realtime_runtime.ontop_queries.{name}"
                            else:
                                path = f"realtime_runtime.{kind}.{name}.sparql"
                        add(str(exc), exc.reason_code, path=path)
                        continue
                    except (ValueError, WorkflowGateError, KeyError, TypeError) as exc:
                        add(str(exc), "INVALID_CQ_CONTRACT")
                        continue
                    for issue in self._cq_dimension_contract_issues(
                        [question], entity_iris=entity_iris, property_iris=property_iris,
                        object_property_iris=object_property_iris, known_evidence_refs=known_refs,
                    ):
                        suffix = issue["path"].split(".answer_contract", 1)[-1]
                        add(issue["message"], issue["reason_code"], path=base + suffix)
                    contract = question["answer_contract"]
                    if not set(contract.get("source_refs") or []).issubset(answer_refs):
                        add(f"业务问题 {qid} 的回答范围引用了未知来源证据。", "UNKNOWN_ANSWER_SOURCE_REFS", path=base + ".source_refs")
                    if any(not set(item["source_refs"]).issubset(answer_refs)
                           for item in (contract.get("nullable_bindings") or {}).values()):
                        add(f"业务问题 {qid} 的条件空值引用了未知来源证据。", "UNKNOWN_NULLABLE_SOURCE_REFS", path=base + ".nullable_bindings")
                    if "expected_rows" not in contract and (not contract.get("result_assertions") or not contract.get("boundary_assertions")):
                        add(f"业务问题 {qid} 必须冻结完整预期集合，或同时冻结实际结果断言与边界断言。", "MISSING_PRODUCTION_ASSERTIONS")
        if require_complete or has_explicit_contracts:
            for qid in sorted(set(intake) - set(owners)):
                issues.append({
                    **self._preflight_issue("G-S3-CQ-CONTRACT", f"业务问题 {qid} 缺少运行时回答能力绑定。",
                                           path="realtime_runtime"),
                    "source_question_id": qid, "reason_code": "UNBOUND_SOURCE_QUESTION",
                    "validation_scope": "S4_CQ_CONTRACT_COMPILATION_ONLY",
                })
        return issues

    def _collect_stage_preflight_issues(
        self,
        project_dir: Path,
        state: dict[str, Any],
        stage: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Combine project-aware checks with read-only submission diagnostics."""
        if stage == "S3":
            # Resolve project evidence and review policy here; the payload
            # validator has no filesystem, workflow or approval authority.
            return [
                *self._s3_downstream_contract_issues(project_dir, state, payload),
                *submission_validation.mapping_submission_issues(
                    payload,
                    intake_mode=state.get("intake_mode"),
                    confirmation_limit=self._s3_confirmation_limit(state),
                    business_review_only=self._s3_business_review_only(state, payload),
                ),
            ]
        validator = {
            "S2": submission_validation.semantic_submission_issues,
            "S4": submission_validation.design_submission_issues,
            "S5": submission_validation.build_submission_issues,
            "S6": submission_validation.validation_submission_issues,
        }.get(stage)
        return validator(payload) if validator is not None else []

    def _issue_stage_preflight_token(
        self,
        *,
        project_dir: Path,
        state: dict[str, Any],
        stage: str,
        normalized_payload: dict[str, Any],
        metrics: dict[str, Any],
        s6_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preview_id = f"PFL-{uuid.uuid4().hex[:16].upper()}"
        secret = secrets.token_urlsafe(32)
        token = f"{preview_id}.{secret}"
        expires_at = (
            datetime.now().astimezone() + timedelta(minutes=PREFLIGHT_TOKEN_TTL_MINUTES)
        ).isoformat(timespec="seconds")
        patch_basis = getattr(self._project_lock_state, "design_patch_basis", None)
        if patch_basis is not None:
            if patch_basis.get("project_id") != project_dir.name or patch_basis.get("stage") != stage:
                raise WorkflowError("局部修订上下文与预检工程或阶段不一致。")
            # Part of the existing token-bound payload digest, rather than
            # unsigned metadata that could be detached from the proposal.
            normalized_payload = {**normalized_payload, "_design_patch_basis": patch_basis}
        receipt = {
            "schema_version": 1,
            "preflight_id": preview_id,
            "project_id": project_dir.name,
            "project_revision": int(state.get("revision") or 0),
            "stage": stage,
            "payload": normalized_payload,
            "payload_sha256": _fingerprint(normalized_payload),
            "metrics": metrics,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "created_at": _now(),
            "expires_at": expires_at,
            "used_at": None,
        }
        if stage == "S6" and s6_inputs is not None:
            receipt["s6_validation_receipt"] = {
                "version": s6_validation_receipts.RECEIPT_VERSION,
                "inputs": s6_inputs,
                "input_fingerprint": _fingerprint(s6_inputs),
                "materialized_ttl_sha256": "sha256:" + hashlib.sha256(
                    str(normalized_payload["materialized_ttl"]).encode("utf-8")
                ).hexdigest(),
                "result_sha256": _fingerprint(metrics),
                "validated_at": metrics.get("validated_at") or _now(),
                "reusable": str((normalized_payload.get("competency_question_report") or {}).get("validation_mode") or "")
                != "BASE_RELEASE_FULL_SOURCE_ONTOP",
            }
            receipt["s6_validation_seal"] = s6_validation_receipts.seal(self.root, receipt)
        receipt_dir = project_dir / ".preflight-submissions"
        receipt_dir.mkdir(exist_ok=True)
        self._write_json(receipt_dir / f"{preview_id}.json", receipt)
        return {
            "preflight_token": token,
            "preflight_id": preview_id,
            "preflight_expires_at": expires_at,
            "normalized_payload_sha256": receipt["payload_sha256"],
        }

    def _s6_validation_inputs(self, project_dir: Path) -> dict[str, Any]:
        """Snapshot actual formal inputs, including source manifests, without querying data."""
        state = self._read_state(project_dir)
        validator_paths = [
            Path(__file__), Path(s6_validation_receipts.__file__),
            Path(__file__).with_name("joint_design.py"),
            Path(__file__).with_name("contract_chain.py"),
            Path(__file__).with_name("stage_contracts.py"),
            Path(submission_validation.__file__),
            Path(__file__).with_name("capability_planning.py"),
            Path(__file__).parents[1] / "ontology_contracts/schema_snapshot.py",
            Path(__file__).parents[1] / "realtime_qa/capabilities.py",
            Path(__file__).with_name("graph_union.py"),
            Path(__file__).with_name("sparql_paths.py"),
            Path(__file__).with_name("materialized_graph.py"),
            Path(__file__).with_name("shacl_validation.py"),
            Path(__file__).with_name("sparql_execution.py"),
            Path(__file__).with_name("cq_validation.py"),
            Path(__file__).with_name("cq_failure_collector.py"),
            Path(__file__).parents[1] / "ontology_contracts/cq_answers.py",
            Path(__file__).parents[1] / "ontology_contracts/sparql_syntax.py",
            Path(__file__).parents[1] / "ontology_contracts/rule_syntax.py",
            Path(__file__).parents[1] / "ontology_contracts/rule_conditions.py",
            Path(__file__).with_name("rule_condition_binding.py"),
            Path(__file__).with_name("business_source_contract.py"),
            Path(__file__).parents[1] / "ontology_contracts/nullable_bindings.py",
            Path(__file__).parents[1] / "ontology_contracts/errors.py",
            Path(__file__).parents[1] / "ontology_contracts/obda.py",
            Path(__file__).parents[1] / "realtime_qa/cq_contract.py",
            Path(__file__).parents[1] / "realtime_qa/query_capabilities.py",
            Path(__file__).parents[1] / "realtime_qa/reasoning_contract.py",
            Path(__file__).parents[1] / "realtime_qa/deployment_automation.py",
            Path(__file__).parents[1] / "realtime_qa/rdf_results.py",
            Path(__file__).parents[1] / "ontop_client/backend_validation.py",
            Path(__file__).parents[1] / "ontop_client/query_execution.py",
            Path(__file__).parents[1] / "ontop_client/client.py",
        ]
        plan_module = Path(__file__).with_name("validation_plan.py")
        if plan_module.is_file():
            validator_paths.append(plan_module)
        return {
            "project_id": project_dir.name,
            "project_revision": int(state.get("revision") or 0),
            "intake_mode": state.get("intake_mode"),
            "stage_contract_version": project_stage_contract_version(state),
            "stage_statuses": {stage: (state.get("stage_statuses") or {}).get(stage) for stage in STAGES[:6]},
            "recorded_stage_fingerprints": {stage: (state.get("stage_fingerprints") or {}).get(stage) for stage in STAGES[:6]},
            "actual_stage_artifact_fingerprints": {
                stage: self._stage_fingerprint(project_dir / STAGE_FOLDERS[stage])
                for stage in STAGES[:6]
            },
            "based_on_release_sha256": _file_checksum(project_dir / "based-on-release.json")
            if (project_dir / "based-on-release.json").is_file() else None,
            "validator": {
                "policy": PRODUCTION_GATE_POLICY_VERSION,
                "receipt_version": s6_validation_receipts.RECEIPT_VERSION,
                "code": {path.name: _file_checksum(path) for path in validator_paths},
                "rdflib": dependency_version("rdflib"),
                "pyshacl": dependency_version("pyshacl"),
                "owlrl": dependency_version("owlrl"),
            },
        }

    def _reusable_s6_validation(
        self, project_dir: Path, payload: dict[str, Any], receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        cached = receipt.get("s6_validation_receipt")
        if not cached:
            return None  # Existing tokens retain the original full validation path.
        if (
            not s6_validation_receipts.verify(self.root, receipt)
            or cached.get("version") != s6_validation_receipts.RECEIPT_VERSION
            or receipt.get("payload_sha256") != _fingerprint(payload)
            or cached.get("result_sha256") != _fingerprint(receipt.get("metrics"))
            or cached.get("materialized_ttl_sha256") != "sha256:" + hashlib.sha256(
                str(payload["materialized_ttl"]).encode("utf-8")
            ).hexdigest()
        ):
            raise WorkflowGateError("G-S6-VALIDATION-RECEIPT", "平台 S6 验证回执完整性或签名不符，请重新预检。")
        current = self._s6_validation_inputs(project_dir)
        if cached.get("inputs") != current or cached.get("input_fingerprint") != _fingerprint(current):
            raise WorkflowGateError("G-S6-VALIDATION-STALE", "S6 验证后来源、正式资产或验证器已变化，请重新预检。")
        self._verify_joint_design_for_execution(project_dir)
        # Candidate identity is cheap to re-read. Base-release CQs may query
        # changing remote data, so they deliberately retain fresh validation.
        if not cached.get("reusable"):
            return None
        self._validated_s6_base_release_endpoint(project_dir, payload.get("competency_question_report") or {})
        if self._s6_validation_inputs(project_dir) != current:
            raise WorkflowGateError("G-S6-VALIDATION-STALE", "候选身份复核期间 S6 输入已变化，请重新预检。")
        quality = json.loads(json.dumps(receipt["metrics"]))
        if (quality.get("target_backend_validation") or {}).get("policy"):
            # The signed graph receipt can be reused; a mutable candidate endpoint
            # cannot inherit old backend execution proof merely from its marker.
            from services.ontop_client.backend_validation import validate_candidate_queries

            cq = payload.get("competency_question_report") or {}
            try:
                quality["target_backend_validation"] = validate_candidate_queries(
                    project_dir=project_dir, endpoint=str(cq.get("validation_endpoint") or ""),
                    candidate_id=str(cq.get("candidate_id") or ""), purpose="COMMIT_BACKEND_REEXECUTION",
                    receipt_path=project_dir / ".stage-executions/S6-artifacts/target-backend-commit-validation.json",
                )
            except Exception as exc:
                raise WorkflowGateError(
                    "G-S6-MAPPING-TARGET-BACKEND", f"提交时目标后端复验失败：{type(exc).__name__}",
                ) from exc
        if quality.get("status") != "PASSED":
            raise WorkflowGateError("G-S6-VALIDATION-RECEIPT", "平台 S6 回执未记录通过结果。")
        quality["validation_execution"] = {
            "mode": "REUSED_PREFLIGHT", "execution_reused": True,
            "preflight_id": receipt["preflight_id"],
            "validated_at": cached["validated_at"], "reused_at": _now(),
            "payload_sha256": receipt["payload_sha256"],
            "result_sha256": cached["result_sha256"],
            "input_fingerprint": cached["input_fingerprint"],
            "validator_fingerprint": _fingerprint(current["validator"]),
        }
        return quality

    def preflight_stage_submission(
        self,
        *,
        project_id: str,
        stage: str,
        payload: dict[str, Any],
        _progress: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ) -> dict[str, Any]:
        """预检高返工阶段并签发短期快照令牌。

        预检不改变正式阶段状态、产物或审计事件；只在工程隐藏缓存目录
        保存一次性载荷快照，供 ``commit_preflight_stage_submission`` 使用。
        """

        normalized_stage = str(stage or "").strip().upper()
        checked_at = _now()
        result: dict[str, Any] = {
            "project_id": project_id,
            "stage": normalized_stage,
            "status": "FAILED",
            "writes_performed": False,
            "checked_at": checked_at,
            "input_fingerprint": _fingerprint(payload),
            "issues": [],
            "diagnostics_mode": "COLLECT_ALL_STRUCTURAL_THEN_FORMAL_GATE",
        }
        diagnostic_issues: list[dict[str, Any]] = []
        if normalized_stage not in {"S1", "S2", "S3", "S4", "S5", "S6"}:
            result["issues"] = [
                {
                    "gate": "G-PREFLIGHT-STAGE",
                    "message": "当前支持 S1–S6 提交前预检；S0 使用资料任务的解析与复核入口。",
                }
            ]
            return result
        try:
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            contract_chain = validate_project_contract_chain(project_dir)
            if contract_chain["error_count"]:
                result["contract_chain"] = contract_chain
                result["issues"] = [
                    self._preflight_issue(
                        "G-CROSS-STAGE-CONTRACT",
                        issue["message"],
                        path="/".join(issue["stages"]),
                        owner="WORKFLOW_KERNEL",
                    )
                    for issue in contract_chain["issues"]
                    if issue["severity"] == "ERROR"
                ]
                result["message"] = (
                    f"S0-S7 跨阶段合同发现 {contract_chain['error_count']} 个冲突；"
                    "工程正式状态未改变。"
                )
                return result
            current_stage = str(state.get("current_stage") or "")
            s4_ready_from_review = (
                normalized_stage == "S4"
                and not current_stage
                and state.get("stage_statuses", {}).get("S3") == "PASSED"
            )
            if current_stage != normalized_stage and not s4_ready_from_review:
                raise WorkflowGateError(
                    "G-PREFLIGHT-STAGE",
                    f"项目当前位于 {state.get('current_stage')}，不能预检 {normalized_stage} 提交。",
                )
            diagnostic_issues = self._collect_stage_preflight_issues(
                project_dir=project_dir,
                state=state,
                stage=normalized_stage,
                payload=payload,
            )
            if diagnostic_issues:
                result["issues"] = diagnostic_issues
                result["message"] = (
                    f"{normalized_stage} 输入发现 {len(diagnostic_issues)} 个可一次修复的问题；"
                    "工程正式状态未改变。"
                )
                return result
            if normalized_stage == "S2":
                result["cq_semantic_review"] = self._cq_semantic_review(project_dir, payload)
            normalized_payload: dict[str, Any] | None = None
            s6_inputs: dict[str, Any] | None = None
            if normalized_stage == "S1":
                if "dataset_ids" in payload:
                    from services.structured_data import StructuredDataPipeline
                    from services.structured_data.pipeline import StructuredDataImportError

                    reader_url = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
                    if not reader_url:
                        raise WorkflowGateError("G-S1-CATALOG-READBACK", "未配置只读数据目录连接。")
                    try:
                        normalized_payload = StructuredDataPipeline(reader_url).build_handoff_for_datasets(
                            project_id=project_id, dataset_ids=payload["dataset_ids"]
                        )["s1"]
                    except StructuredDataImportError as exc:
                        raise WorkflowGateError("G-S1-CATALOG-READBACK", str(exc)) from exc
                else:
                    normalized_payload = {
                        key: payload[key] for key in (
                            "datasource_inventory", "schema_snapshot", "data_profile",
                            "relation_candidates", "evidence_sql",
                        )
                    }
                normalized_payload["schema_snapshot"] = self._normalize_s1_schema_snapshot(
                    normalized_payload["schema_snapshot"]
                )
                self._validate_s1(normalized_payload, project_id=project_id)
                self._validate_source_scope(project_dir, stage="S1", payload=normalized_payload)
                metrics = {"table_count": normalized_payload["data_profile"]["table_count"],
                           "total_rows": normalized_payload["data_profile"]["total_rows"]}
            elif normalized_stage == "S2":
                normalized_payload = {
                    "ontology_candidates": payload.get("ontology_candidates") or [],
                    "business_rule_candidates": payload.get("business_rule_candidates") or [],
                }
                if payload.get("cq_semantic_assessments") is not None:
                    normalized_payload["cq_semantic_assessments"] = payload["cq_semantic_assessments"]
                self._validate_s2(
                    normalized_payload,
                    project_dir=project_dir,
                    intake_mode=str(state.get("intake_mode") or "HYBRID"),
                )
                capability_plan = self._build_capability_plan(
                    project_dir,
                    intake_mode=str(state.get("intake_mode") or "HYBRID"),
                    business_rules=normalized_payload["business_rule_candidates"],
                    semantic_review=result["cq_semantic_review"],
                    cq_semantic_assessments=normalized_payload.get("cq_semantic_assessments"),
                )
                self._require_ready_capability_plan(capability_plan)
                metrics = {
                    "capability_routing": {
                        "status": capability_plan["status"],
                        "route_count": len(capability_plan["routes"]),
                        "scope": capability_plan["readiness_scope"],
                    },
                    "ontology_candidate_count": len(normalized_payload["ontology_candidates"]),
                    "business_rule_count": len(normalized_payload["business_rule_candidates"]),
                    "cq_semantic_review": result["cq_semantic_review"],
                }
            elif normalized_stage == "S3":
                intake_mode = str(state.get("intake_mode") or "HYBRID")
                business_review_only = self._s3_business_review_only(state, payload)
                self._require_s3_review_cards_retained(project_dir, state, payload.get("confirmations") or [])
                runtime_submission = dict(payload.get("realtime_runtime") or {})
                compiler_decisions = []
                if intake_mode != "DOCUMENT_ONLY":
                    normalized_obda, compiler_decisions = _normalize_s3_obda(
                        str(runtime_submission.get("mapping_obda") or ""), check_sources=not business_review_only
                    )
                    runtime_submission["mapping_obda"] = normalized_obda
                try:
                    normalized_runtime = None if business_review_only else normalize_runtime_submission(
                        runtime_submission,
                        intake_mode=intake_mode,
                        require_explicit_capabilities=intake_mode != "DOCUMENT_ONLY",
                    )
                except RuntimeReleaseError as exc:
                    raise WorkflowGateError("G-S3-RUNTIME", str(exc), path=exc.path, reason_code=exc.reason_code) from exc
                if normalized_runtime is None and not business_review_only:
                    raise WorkflowGateError(
                        "G-S3-RUNTIME",
                        "所有新本体都必须声明只读实时问答能力；结构化项目不能省略 runtime。",
                    )
                normalized_payload = self._normalize_s3_submission(
                    {**payload, "realtime_runtime": normalized_runtime}
                )
                normalized_confirmations, normalized_automatic = self._validate_s3(
                    normalized_payload,
                    intake_mode=intake_mode,
                    project_dir=project_dir,
                )
                if business_review_only:
                    remaining, _ = self._reuse_s3_confirmations(
                        project_dir=project_dir, state=state, confirmations=normalized_confirmations,
                    )
                    if not any(item.get("id") != "S3-OVERALL-MAPPING-REVIEW" for item in remaining):
                        raise WorkflowGateError("G-S3-RUNTIME", "业务卡已决定；必须提交完整运行设计并通过预检，不能重复提交仅业务卡。")
                metrics = {
                    "mapping_count": len(
                        normalized_payload.get("mapping_draft", {}).get("mappings") or []
                    ),
                    "confirmation_count": len(normalized_confirmations),
                    "automatic_decision_count": len(normalized_automatic),
                    "runtime_compiler_decision_count": len(compiler_decisions),
                    "validation_scope": "BUSINESS_REVIEW_DRAFT_ONLY" if business_review_only else "FULL_S3_SUBMISSION",
                    "runtime_validated": not business_review_only,
                }
            elif normalized_stage == "S4":
                generation_fields = {
                    "ontology_iri",
                    "version",
                    "competency_questions",
                    "logical_axioms",
                    "review_policy",
                }
                generation_request = payload.get("generation_request")
                if generation_request is None and "classes" not in payload:
                    # ``generate_ontology_design`` 接收的是生成参数，不是已经包含
                    # classes/object_properties 的完整设计。历史上直接把这些参数
                    # 交给 S4 预检会误报 classes 为空；这里让预检和正式生成复用
                    # 同一条确定性构建链路。
                    payload_fields = set(payload)
                    if payload_fields and payload_fields <= generation_fields:
                        generation_request = payload
                if generation_request is not None:
                    if not isinstance(generation_request, dict):
                        raise WorkflowGateError(
                            "G-S4-PAYLOAD",
                            "generation_request 必须是对象。",
                        )
                    unknown_fields = set(generation_request) - generation_fields
                    if unknown_fields:
                        raise WorkflowGateError(
                            "G-S4-PAYLOAD",
                            "generation_request 包含不支持的字段："
                            + ", ".join(sorted(unknown_fields)),
                        )
                    review_policy = (
                        str(generation_request.get("review_policy") or "HUMAN_REQUIRED")
                        .strip()
                        .upper()
                    )
                    if review_policy not in {
                        "HUMAN_REQUIRED",
                        "AUTO_APPROVE_EVIDENCE_BACKED",
                    }:
                        raise WorkflowGateError(
                            "G-S4-PAYLOAD",
                            "review_policy 只能是 HUMAN_REQUIRED 或 AUTO_APPROVE_EVIDENCE_BACKED。",
                        )
                    design = self._generate_ontology_design_unlocked(
                        project_id=project_id,
                        ontology_iri=generation_request.get("ontology_iri"),
                        version=str(generation_request.get("version") or "0.1.0"),
                        competency_questions=generation_request.get("competency_questions"),
                        logical_axioms=generation_request.get("logical_axioms"),
                        review_policy=review_policy,
                        preview_only=True,
                    )
                    normalized_payload = {
                        **generation_request,
                        "review_policy": review_policy,
                    }
                    submission_mode = "GENERATION_REQUEST"
                else:
                    design = payload.get("ontology_design", payload)
                    submission_mode = "COMPLETE_DESIGN"
                design = self._normalize_ontology_design_localization(project_dir, design)
                design = {
                    **design,
                    "competency_questions": self._complete_review_questions(
                        design,
                        design.get("competency_questions") or [],
                    ),
                }
                cq_issues = self._collect_s4_cq_contract_issues(project_dir, design)
                if cq_issues:
                    result.update(
                        issues=cq_issues[:30], issue_count=len(cq_issues),
                        issues_truncated=len(cq_issues) > 30,
                        message=f"S4 CQ 合同发现 {len(cq_issues)} 个可一次修复的问题；工程正式状态未改变。",
                    )
                    return result
                metrics = self._validate_s4(project_dir, design)
                if generation_request is None:
                    normalized_payload = (
                        {**payload, "ontology_design": design}
                        if "ontology_design" in payload
                        else design
                    )
                metrics = {
                    **metrics,
                    "submission_mode": submission_mode,
                    "generated_design_fingerprint": _fingerprint(design),
                }
            elif normalized_stage == "S5":
                metrics = self._validate_s5(project_dir, payload)
            else:
                s6_inputs = self._s6_validation_inputs(project_dir)
                if _progress is None:
                    metrics = self._validate_s6(project_dir, payload)
                else:
                    active_subgate = "PREFLIGHT"
                    progress_warning_logged = False

                    def report_progress(
                        subgate: str,
                        status: str,
                        details: dict[str, Any] | None = None,
                    ) -> None:
                        nonlocal active_subgate, progress_warning_logged
                        active_subgate = subgate
                        try:
                            # Observers cannot alter validator-owned details or results.
                            _progress(subgate, status, json.loads(json.dumps(details or {})))
                        except Exception as exc:
                            # Progress is best effort; no exception text or credentials.
                            if not progress_warning_logged:
                                import logging

                                logging.getLogger(__name__).warning(
                                    "S6 preflight progress could not be recorded (%s)",
                                    type(exc).__name__,
                                )
                                progress_warning_logged = True

                    try:
                        metrics = self._validate_s6(project_dir, payload, progress=report_progress)
                    except Exception as exc:
                        gate = getattr(exc, "gate_id", None)
                        failed_subgate = str(gate).removeprefix("G-S6-").replace("-", "_") if gate else active_subgate
                        report_progress(failed_subgate, "FAILED", {
                            "gate": gate,
                            "error_type": type(exc).__name__,
                        })
                        raise
            result.update(
                {
                    "status": "PASSED",
                    "metrics": metrics,
                    **(
                        {"normalized_payload": normalized_payload}
                        if normalized_payload is not None
                        else {}
                    ),
                    "issues": [],
                    "message": f"{normalized_stage} 输入已通过只读预检，可以提交正式阶段记录。",
                }
            )
            if normalized_stage == "S3" and metrics.get("validation_scope") == "BUSINESS_REVIEW_DRAFT_ONLY":
                result["message"] = "映射候选与业务卡预检通过，可提交待确认草案；运行设计尚未提交，S3 未通过，业务决定后必须完整预检。"
            with self._project_operation_lock(project_dir):
                latest_state = self._read_state(project_dir)
                if int(latest_state.get("revision") or 0) != int(state.get("revision") or 0):
                    raise WorkflowGateError(
                        "G-PREFLIGHT-STALE",
                        "预检期间工程 revision 已变化，请基于最新状态重新预检。",
                    )
                if s6_inputs is not None and s6_inputs != self._s6_validation_inputs(project_dir):
                    raise WorkflowGateError("G-S6-VALIDATION-STALE", "S6 预检期间来源、正式资产或验证器已变化，请重新预检。")
                token_details = self._issue_stage_preflight_token(
                    project_dir=project_dir,
                    state=latest_state,
                    stage=normalized_stage,
                    normalized_payload=normalized_payload or payload,
                    metrics=metrics,
                    s6_inputs=s6_inputs,
                )
            result.update(
                {
                    **token_details,
                    "cache_write_performed": True,
                    "formal_state_unchanged": True,
                    "message": (
                        f"{normalized_stage} 输入已通过预检；正式提交只需使用 preflight_token，"
                        "无需再次传输完整载荷。"
                    ),
                }
            )
        except WorkflowGateError as exc:
            if normalized_stage == "S6":
                result["status"] = "FAILED"
            issue = self._preflight_issue(exc.gate_id, str(exc), path=exc.path)
            if exc.reason_code:
                issue["reason_code"] = exc.reason_code
            result["issues"] = [*diagnostic_issues]
            if not any(
                item.get("gate") == issue["gate"] and item.get("message") == issue["message"]
                for item in result["issues"]
            ):
                result["issues"].append(issue)
            result["message"] = f"{normalized_stage} 输入尚未通过预检；工程状态未改变。"
            if normalized_stage in {"S2", "S3", "S4"}:
                result["recovery"] = {
                    "review_tool": "get_cq_semantic_review",
                    "repeat_unchanged_input": False,
                    "reason": "先核对 CQ 的业务定义、模型表达和来源缺口；规则政策用已有业务卡确认，新增来源正式登记。不得用示例事实或弱断言消除门禁。",
                }
        except (KeyError, TypeError, ValueError) as exc:
            result["issues"] = [
                {
                    "gate": f"G-{normalized_stage}-PAYLOAD",
                    "message": f"提交数据结构不完整：{exc}",
                }
            ]
            result["message"] = f"{normalized_stage} 输入尚未通过预检；工程状态未改变。"
        return result

    def commit_preflight_stage_submission(
        self,
        *,
        project_id: str,
        stage: str,
        preflight_token: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Commit once; authentic retries recover the same durable operation result."""
        normalized_stage = str(stage or "").strip().upper()
        if normalized_stage not in {"S1", "S2", "S3", "S4", "S5", "S6"}:
            raise WorkflowError("preflight token 只支持提交 S1-S6。")
        preflight_id = preflight_token.split(".", 1)[0]
        if not re.fullmatch(r"PFL-[A-F0-9]{16}", preflight_id) or "." not in preflight_token:
            raise WorkflowError("preflight_token 格式无效。")
        with self._project_mutation_lock(
            project_id, recovery_preflight_id=preflight_id
        ) as project_dir:
            receipt_path = project_dir / ".preflight-submissions" / f"{preflight_id}.json"
            if not receipt_path.is_file():
                raise WorkflowError("preflight_token 无效或已不存在。")
            receipt = self._read_json(receipt_path)
            if not secrets.compare_digest(
                str(receipt.get("token_sha256") or ""),
                hashlib.sha256(preflight_token.encode()).hexdigest(),
            ):
                raise WorkflowError("preflight_token 校验失败。")
            if receipt.get("project_id") != project_id or receipt.get("stage") != normalized_stage:
                raise WorkflowError("preflight_token 与工程或阶段不一致。")
            payload = receipt.get("payload")
            if not isinstance(payload, dict) or receipt.get("payload_sha256") != _fingerprint(
                payload
            ):
                raise WorkflowGateError("G-PREFLIGHT-INTEGRITY", "预检载荷快照完整性校验失败。")
            receipt_revision = int(receipt.get("project_revision") or 0)
            if expected_revision is not None and int(expected_revision) != receipt_revision:
                raise WorkflowError("请求 revision 与预检输入版本不一致；恢复须使用原提交参数。")
            operation_path = project_dir / preflight_operations.DIRECTORY / f"{preflight_id}.json"
            request_hash = _fingerprint(
                {
                    "project_id": project_id,
                    "stage": normalized_stage,
                    "revision": receipt_revision,
                    "payload": receipt["payload_sha256"],
                }
            )
            try:
                operation = preflight_operations.read(operation_path)
            except (ValueError, OSError) as exc:
                raise WorkflowError(str(exc)) from exc
            if operation and operation.get("request_hash") != request_hash:
                raise WorkflowError("预检操作身份已绑定另一组输入，拒绝重用。")
            if getattr(self._project_lock_state, "managed_recovery", None) and (
                normalized_stage != self._project_lock_state.managed_recovery[4]
                or not operation or operation.get("status") not in {"CHECKPOINTED", "COMMITTED"}
            ):
                raise WorkflowError("托管恢复只允许回读已有提交检查点，禁止重新执行阶段。")
            if operation and operation["status"] == "COMMITTED":
                # Expiry bounds a new execution, not retrieval of an authenticated result.
                return self._finish_preflight_operation(
                    receipt_path, receipt, operation_path, operation
                )
            if operation and operation["status"] == "CHECKPOINTED":
                if operation["checkpoint"] != self._preflight_checkpoint(
                    project_dir, normalized_stage
                ):
                    raise WorkflowError("预检提交结果与检查点不一致，需对账；禁止重新执行。")
                if operation.get("failure"):
                    operation["status"] = "FAILED"
                    self._write_preflight_operation(operation_path, operation)
                    error = operation["failure"]
                    raise WorkflowGateError(error["gate"], error["message"])
                operation["result"] = self._status_payload(
                    project_dir, self._read_state(project_dir)
                )
                return self._finish_preflight_operation(
                    receipt_path, receipt, operation_path, operation
                )
            if operation and operation["status"] == "FAILED":
                error = operation["failure"]
                raise WorkflowGateError(error["gate"], error["message"])
            if receipt.get("used_at"):
                raise WorkflowError("旧版 preflight_token 已使用且没有可恢复结果，重复提交已拒绝。")
            if (
                operation
                and operation["status"] == "PREPARED"
                and operation["before"] != self._preflight_checkpoint(project_dir, normalized_stage)
            ):
                raise WorkflowError("预检操作存在未确认的部分写入，需对账；禁止重新执行。")
            if datetime.fromisoformat(str(receipt["expires_at"])) <= datetime.now().astimezone():
                if operation and operation["status"] == "PREPARED":
                    operation["status"] = "ABORTED"
                    self._write_preflight_operation(operation_path, operation)
                raise WorkflowError("preflight_token 已过期，请重新预检。")
            self._require_expected_revision(self._read_state(project_dir), receipt_revision)
            if "_design_patch_basis" in payload:
                from .design_workspace_api import verify_commit_basis

                try:
                    verify_commit_basis(project_dir, normalized_stage, payload["_design_patch_basis"])
                except (ValueError, OSError) as exc:
                    raise WorkflowGateError("G-DESIGN-PATCH-STALE", str(exc)) from exc
            operation = {
                "operation_id": preflight_id,
                "project_id": project_id,
                "stage": normalized_stage,
                "request_hash": request_hash,
                "status": "PREPARED",
                "started_at": _now(),
                "before": self._preflight_checkpoint(project_dir, normalized_stage),
            }
            self._write_preflight_operation(operation_path, operation)
            previous_context = getattr(self._project_lock_state, "preflight_context", None)
            self._project_lock_state.preflight_context = {
                "project_dir": str(project_dir),
                "path": operation_path,
                "operation": operation,
            }
            try:
                result = self._execute_preflight_payload(
                    project_id, normalized_stage, receipt, payload
                )
                operation["result"] = result
                return self._finish_preflight_operation(
                    receipt_path, receipt, operation_path, operation
                )
            except Exception:
                if operation.get("status") != "COMMITTED":
                    if operation.get("status") == "CHECKPOINTED" and operation.get("failure"):
                        operation["status"] = "FAILED"
                        self._write_preflight_operation(operation_path, operation)
                    elif operation["before"] == self._preflight_checkpoint(
                        project_dir, normalized_stage
                    ):
                        operation["status"] = "ABORTED"
                        self._write_preflight_operation(operation_path, operation)
                raise
            finally:
                self._project_lock_state.preflight_context = previous_context

    def _execute_preflight_payload(
        self,
        project_id: str,
        normalized_stage: str,
        receipt: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        receipt_revision = int(receipt.get("project_revision") or 0)
        if normalized_stage == "S1":
            result = self.record_data_understanding(
                project_id=project_id, expected_revision=receipt_revision, **payload
            )
        elif normalized_stage == "S2":
            result = self.record_semantic_candidates(
                project_id=project_id,
                ontology_candidates=payload.get("ontology_candidates") or [],
                business_rule_candidates=payload.get("business_rule_candidates") or [],
                cq_semantic_assessments=payload.get("cq_semantic_assessments"),
                expected_revision=receipt_revision,
            )
        elif normalized_stage == "S3":
            result = self.prepare_mapping_review(
                project_id=project_id,
                mapping_draft=payload.get("mapping_draft") or {},
                confirmations=payload.get("confirmations") or [],
                automatic_decisions=payload.get("automatic_decisions") or [],
                realtime_runtime=payload.get("realtime_runtime"),
                review_policy=str(payload.get("review_policy") or "HUMAN_REQUIRED"),
                expected_revision=receipt_revision,
            )
        elif normalized_stage == "S4":
            if receipt.get("metrics", {}).get("submission_mode") == "GENERATION_REQUEST":
                result = self.generate_ontology_design(
                    project_id=project_id,
                    ontology_iri=payload.get("ontology_iri"),
                    version=str(payload.get("version") or "0.1.0"),
                    competency_questions=payload.get("competency_questions"),
                    logical_axioms=payload.get("logical_axioms"),
                    review_policy=str(payload.get("review_policy") or "HUMAN_REQUIRED"),
                )
            else:
                result = self.prepare_ontology_design_review(
                    project_id=project_id,
                    ontology_design=payload.get("ontology_design", payload),
                    review_policy=str(payload.get("review_policy") or "HUMAN_REQUIRED"),
                    expected_revision=receipt_revision,
                    # A token-bound draft revision may replace the current
                    # pending joint review, but cannot approve or reopen S5.
                    _replacing_pending_review=(
                        str(payload.get("review_policy") or "HUMAN_REQUIRED").strip().upper()
                        == "HUMAN_REQUIRED"
                    ),
                )
        elif normalized_stage == "S5":
            result = self.record_ontology_build(
                project_id=project_id,
                ontology_owl=str(payload.get("ontology_owl") or ""),
                ontology_ttl=str(payload.get("ontology_ttl") or ""),
                shapes_ttl=str(payload.get("shapes_ttl") or ""),
                protege_build_report=payload.get("protege_build_report") or {},
                expected_revision=receipt_revision,
            )
        else:
            result = self._record_quality_validation_payload(
                project_id=project_id,
                payload=payload,
                preflight_receipt=receipt,
                expected_revision=receipt_revision,
            )
        return result

    def _finish_preflight_operation(
        self,
        receipt_path: Path,
        receipt: dict[str, Any],
        operation_path: Path,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            **operation["result"],
            "preflight_id": operation["operation_id"],
            "operation_id": operation["operation_id"],
            "preflight_stage": operation["stage"],
            "preflight_payload_sha256": receipt["payload_sha256"],
            "preflight_consumed": True,
        }
        if operation["status"] != "COMMITTED":
            operation.update(status="COMMITTED", result=result, completed_at=_now())
            self._write_preflight_operation(operation_path, operation)
        if not receipt.get("used_at"):
            receipt.update(
                used_at=operation["completed_at"], committed_revision=result.get("revision")
            )
            self._write_json(receipt_path, receipt)
        return result

    def _write_preflight_operation(self, path: Path, record: dict[str, Any]) -> None:
        self._write_json(path, preflight_operations.sealed(record))
        self._fsync_directory(path.parent)
        self._fsync_directory(path.parent.parent)

    def _preflight_checkpoint(self, project_dir: Path, stage: str) -> dict[str, Any]:
        paths = ["workflow-state.json", "project.json", "events/agent-trace.jsonl"]
        return {
            "files": {
                name: _file_checksum(project_dir / name) if (project_dir / name).is_file() else None
                for name in paths
            },
            "stage_artifacts": self._stage_fingerprint(project_dir / STAGE_FOLDERS[stage]),
        }

    def _checkpoint_preflight_operation(self, project_dir: Path) -> None:
        context = getattr(self._project_lock_state, "preflight_context", None)
        if not context or context["project_dir"] != str(project_dir):
            return
        operation = context["operation"]
        state = self._read_state(project_dir)
        events = self._read_events(project_dir)
        owned = [
            e
            for e in events
            if e.get("details", {}).get("preflight_operation_id") == operation["operation_id"]
        ]
        stage_status = state["stage_statuses"].get(operation["stage"])
        if not owned or stage_status not in {"PASSED", "BLOCKED_HUMAN", "FAILED"}:
            return
        operation.update(
            status="CHECKPOINTED",
            checkpoint=self._preflight_checkpoint(project_dir, operation["stage"]),
        )
        if stage_status == "FAILED":
            error = state.get("last_error") or {}
            operation["failure"] = {
                "gate": error.get("gate") or "G-PREFLIGHT-FAILED",
                "message": error.get("message") or "正式阶段检查失败。",
            }
        self._write_preflight_operation(context["path"], operation)

    def record_quality_validation(
        self,
        *,
        project_id: str,
        materialized_ttl: str,
        hermit_report: dict[str, Any],
        mapping_report: dict[str, Any],
        semantic_quality_report: dict[str, Any],
        competency_question_report: dict[str, Any],
        semantica_report: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """核验真实实例图与外部验证证据；全部通过后进入发布评审。"""

        payload = {
            "materialized_ttl": materialized_ttl,
            "hermit_report": hermit_report,
            "mapping_report": mapping_report,
            "semantic_quality_report": semantic_quality_report,
            "competency_question_report": competency_question_report,
            "semantica_report": semantica_report,
        }
        return self._record_quality_validation_payload(
            project_id=project_id, payload=payload, expected_revision=expected_revision,
        )

    def _record_quality_validation_payload(
        self, *, project_id: str, payload: dict[str, Any],
        expected_revision: int | None = None,
        preflight_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        materialized_ttl = str(payload["materialized_ttl"])
        hermit_report = payload["hermit_report"]
        mapping_report = payload["mapping_report"]
        semantic_quality_report = payload["semantic_quality_report"]
        semantica_report = payload["semantica_report"]
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S6")
            self._require_expected_revision(state, expected_revision)
            self._reject_identical_failed_submission(state, "S6", payload)
            quality = self._reusable_s6_validation(project_dir, payload, preflight_receipt) if preflight_receipt else None
            run_id = self._start_s6_validation_run(project_dir, payload)

            def progress(
                subgate: str,
                status: str,
                details: dict[str, Any] | None = None,
            ) -> None:
                self._update_s6_validation_run(
                    project_dir,
                    run_id=run_id,
                    subgate=subgate,
                    status=status,
                    details=details or {},
                )

            try:
                if quality is None:
                    quality = self._validate_s6(project_dir, payload, progress=progress)
                    quality["validation_execution"] = {
                        "mode": "EXECUTED_AT_COMMIT", "execution_reused": False,
                        "validated_at": quality.get("validated_at") or _now(),
                        "payload_sha256": _fingerprint(payload),
                    }
                else:
                    for subgate in S6_VALIDATION_SUBGATES:
                        progress(subgate, "PASSED", {
                            "execution_reused": True,
                            "preflight_id": quality["validation_execution"]["preflight_id"],
                            "message": "复用平台已执行且完整性复核通过的 S6 验证回执。",
                        })
            except WorkflowGateError as exc:
                self._fail_s6_validation_run(project_dir, run_id=run_id, error=exc)
                self._mark_failed(project_dir, state, "S6", exc, input_payload=payload)
                raise

            self._complete_s6_validation_run(project_dir, run_id=run_id)
            run_path = project_dir / "06-quality-validation/run-status.json"
            run = self._read_json(run_path)
            run["validation_execution"] = quality["validation_execution"]
            self._write_json(run_path, run)

            stage_dir = project_dir / "06-quality-validation"
            stage_dir.mkdir(parents=True, exist_ok=True)
            server_cq_report = quality.pop("competency_question_report")
            server_cq_report["validation_execution"] = quality["validation_execution"]
            self._atomic_write(stage_dir / "materialized.ttl", materialized_ttl)
            if business_contract.enabled(state):
                quality["validated_graph_sha256"] = _file_checksum(stage_dir / "materialized.ttl")
            self._write_json(stage_dir / "hermit-report.json", hermit_report)
            self._write_json(stage_dir / "mapping-report.json", mapping_report)
            self._write_json(stage_dir / "semantic-quality-report.json", semantic_quality_report)
            self._write_json(
                stage_dir / "competency-question-report.json",
                server_cq_report,
            )
            self._write_json(stage_dir / "semantica-report.json", semantica_report)
            self._atomic_write(stage_dir / "shacl-report.ttl", quality.pop("shacl_report_ttl"))
            self._atomic_write(stage_dir / "shacl-report.txt", quality.pop("shacl_report_text"))
            if quality.get("class_instance_validation"):
                self._write_json(stage_dir / "class-instance-validation.json", quality["class_instance_validation"])
            if quality.get("protege_instance_preview_owl"):
                self._atomic_write(stage_dir / "protege-model-instance-preview.owl", quality.pop("protege_instance_preview_owl"))
                self._write_json(stage_dir / "protege-instance-preview.json", quality["protege_instance_preview"])
            self._write_json(stage_dir / "quality-summary.json", quality)
            self._write_json(
                stage_dir / "gate-results.json",
                {
                    "stage": "S6",
                    "status": "PASSED",
                    "gates": [
                        {"id": "G-S6-HERMIT", "status": "PASSED"},
                        {"id": "G-S6-SHACL", "status": "PASSED"},
                        {"id": "G-S6-MAPPING", "status": "PASSED"},
                        {"id": "G-S6-SEMANTIC", "status": "PASSED"},
                        {"id": "G-S6-CQ", "status": "PASSED"},
                        {"id": "G-S6-SEMANTICA", "status": "PASSED"},
                        {"id": "G-S6-REASONING", "status": "PASSED"},
                        {"id": "G-S6-PRODUCTION-COVERAGE", "status": "PASSED"},
                        {"id": "G-S6-GRAPH-RELATIONSHIP", "status": "PASSED"},
                        {"id": "G-S6-CQ-BUSINESS-SEMANTIC", "status": "PASSED"},
                    ],
                    "metrics": quality,
                    "checked_at": _now(),
                },
            )
            self._atomic_write(
                stage_dir / "quality-validation-report.html",
                render_s6_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    {
                        "hermit_report": hermit_report,
                        "mapping_report": mapping_report,
                        "semantic_quality_report": semantic_quality_report,
                        "competency_question_report": server_cq_report,
                        "semantica_report": semantica_report,
                    },
                    quality,
                    self._read_json(stage_dir / "gate-results.json"),
                ),
            )
            self._mark_artifacts_regenerated(
                state,
                {
                    "06-quality-validation/README.md",
                    "06-quality-validation/materialized.ttl",
                    "06-quality-validation/hermit-report.json",
                    "06-quality-validation/mapping-report.json",
                    "06-quality-validation/semantic-quality-report.json",
                    "06-quality-validation/competency-question-report.json",
                    "06-quality-validation/semantica-report.json",
                    "06-quality-validation/shacl-report.ttl",
                    "06-quality-validation/shacl-report.txt",
                    "06-quality-validation/quality-summary.json",
                    "06-quality-validation/gate-results.json",
                    "06-quality-validation/quality-validation-report.html",
                    "06-quality-validation/run-status.json",
                } | ({
                    "06-quality-validation/class-instance-validation.json",
                    "06-quality-validation/protege-model-instance-preview.owl",
                    "06-quality-validation/protege-instance-preview.json",
                } if quality.get("protege_instance_preview") else set()),
            )
            self._pass_stage(project_dir, state, "S6", "S7", payload)
            state["resume_point"] = "S7: 审阅质量证据并由负责人明确批准发布"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "QUALITY_VALIDATION_COMPLETED",
                state,
                {"stage": "S6", **quality},
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def _start_s6_validation_run(
        self,
        project_dir: Path,
        payload: dict[str, Any],
    ) -> str:
        run_id = "S6RUN-" + hashlib.sha256(_canonical_json(payload)).hexdigest()[:16].upper()
        started_at = _now()
        self._write_json(
            project_dir / "06-quality-validation/run-status.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "stage": "S6",
                "status": "RUNNING",
                "started_at": started_at,
                "updated_at": started_at,
                "last_heartbeat_at": started_at,
                "input_fingerprint": _fingerprint(payload),
                "subgates": [
                    {
                        "id": subgate,
                        "status": "PENDING",
                        "updated_at": started_at,
                    }
                    for subgate in S6_VALIDATION_SUBGATES
                ],
            },
        )
        return run_id

    def _update_s6_validation_run(
        self,
        project_dir: Path,
        *,
        run_id: str,
        subgate: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        path = project_dir / "06-quality-validation/run-status.json"
        run = self._read_json(path)
        if run.get("run_id") != run_id:
            raise WorkflowError("S6 validation run_id 已变化，拒绝写入旧任务进度。")
        now = _now()
        updated = False
        for item in run.get("subgates") or []:
            if item.get("id") == subgate:
                item.update({**details, "status": status, "updated_at": now})
                if status == "RUNNING" and not item.get("started_at"):
                    item["started_at"] = now
                if status in {"PASSED", "FAILED"}:
                    item["completed_at"] = now
                updated = True
                break
        if not updated:
            raise WorkflowError(f"未知 S6 子门禁：{subgate}")
        run["updated_at"] = now
        run["last_heartbeat_at"] = now
        self._write_json(path, run)

    def _fail_s6_validation_run(
        self,
        project_dir: Path,
        *,
        run_id: str,
        error: WorkflowGateError,
    ) -> None:
        path = project_dir / "06-quality-validation/run-status.json"
        run = self._read_json(path)
        gate = error.gate_id.upper()
        subgate = next(
            (item for item in S6_VALIDATION_SUBGATES if item in gate),
            "CQ" if "ANSWER" in gate else None,
        )
        now = _now()
        if subgate:
            for item in run.get("subgates") or []:
                if item.get("id") == subgate:
                    item.update(
                        {
                            "status": "FAILED",
                            "message": str(error),
                            "updated_at": now,
                            "completed_at": now,
                        }
                    )
        run.update(
            {
                "status": "FAILED",
                "failed_gate": error.gate_id,
                "message": str(error),
                "updated_at": now,
                "last_heartbeat_at": now,
                "completed_at": now,
            }
        )
        self._write_json(path, run)

    def _complete_s6_validation_run(self, project_dir: Path, *, run_id: str) -> None:
        path = project_dir / "06-quality-validation/run-status.json"
        run = self._read_json(path)
        if run.get("run_id") != run_id:
            raise WorkflowError("S6 validation run_id 已变化，不能完成旧任务。")
        now = _now()
        for item in run.get("subgates") or []:
            if item.get("status") == "PENDING":
                item.update({"status": "PASSED", "updated_at": now, "completed_at": now})
        run.update(
            {
                "status": "PASSED",
                "updated_at": now,
                "last_heartbeat_at": now,
                "completed_at": now,
            }
        )
        self._write_json(path, run)

    def publish_ontology_package(
        self,
        *,
        project_id: str,
        release_version: str,
        approval_decision: str,
        approved_by: str,
        release_notes: str,
        expected_revision: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        # Release identity is global, not project-local. Hold the root lock before
        # the project lock so two projects cannot publish the same
        # (ontology IRI, semantic version) concurrently.
        with (
            self._engineering_project_guard(project_id),
            self._lock,
            self._root_operation_lock(),
            self._project_mutation_lock(project_id) as project_dir,
        ):
            self._verify_joint_design_for_execution(project_dir)
            normalized_operation_id = self._normalize_operation_id(operation_id)
            request_fingerprint = _fingerprint(
                {
                    "operation": "publish_ontology_package",
                    "project_id": project_id,
                    "release_version": release_version,
                    "approval_decision": approval_decision,
                    "approved_by": approved_by.strip(),
                    "release_notes": release_notes.strip(),
                }
            )
            replay = self._operation_replay(
                project_dir,
                normalized_operation_id,
                request_fingerprint,
            )
            if replay is not None:
                deployment = self._enqueue_realtime_deployment(project_dir, release_version)
                self._record_realtime_deployment_status(
                    project_dir,
                    release_version,
                    deployment,
                )
                return {
                    **self._status_payload(project_dir, self._read_state(project_dir)),
                    "operation_id": normalized_operation_id,
                    "idempotent_replay": True,
                    "realtime_deployment": deployment,
                }

            state = self._read_state(project_dir)
            publication_path = project_dir / "07-release/publication.json"
            if (
                state.get("project_status")
                in {
                    "PACKAGE_PUBLISHED",
                    "RUNTIME_VERIFYING",
                    "RUNTIME_FAILED",
                    "PACKAGE_READY_RUNTIME_BLOCKED",
                    "PUBLISHED",
                }
                and publication_path.exists()
            ):
                publication = self._read_json(publication_path)
                existing_fingerprint = _fingerprint(
                    {
                        "operation": "publish_ontology_package",
                        "project_id": project_id,
                        "release_version": publication.get("release_version"),
                        "approval_decision": publication.get("approval_decision"),
                        "approved_by": publication.get("approved_by"),
                        "release_notes": publication.get("release_notes"),
                    }
                )
                if existing_fingerprint != request_fingerprint:
                    raise WorkflowError("项目已发布，重复请求参数与原发布记录不一致。")
                self._record_operation_receipt(
                    project_dir,
                    normalized_operation_id,
                    request_fingerprint,
                    "publish_ontology_package",
                )
                deployment = self._enqueue_realtime_deployment(project_dir, release_version)
                self._record_realtime_deployment_status(
                    project_dir,
                    release_version,
                    deployment,
                )
                return {
                    **self._status_payload(project_dir, self._read_state(project_dir)),
                    "operation_id": normalized_operation_id,
                    "idempotent_replay": True,
                    "realtime_deployment": deployment,
                }

            self._require_expected_revision(state, expected_revision)
            contract_chain = validate_project_contract_chain(project_dir)
            if contract_chain["error_count"]:
                raise WorkflowGateError(
                    "G-S7-CROSS-STAGE-CONTRACT",
                    "发布前跨阶段合同未闭合："
                    + "；".join(
                        issue["message"]
                        for issue in contract_chain["issues"]
                        if issue["severity"] == "ERROR"
                    ),
                )
            self._assert_release_identity_available(
                project_dir=project_dir,
                release_version=release_version,
            )
            self._publish_ontology_package_unlocked(
                project_id=project_id,
                release_version=release_version,
                approval_decision=approval_decision,
                approved_by=approved_by,
                release_notes=release_notes,
            )
            self._record_operation_receipt(
                project_dir,
                normalized_operation_id,
                request_fingerprint,
                "publish_ontology_package",
            )
            deployment = self._enqueue_realtime_deployment(project_dir, release_version)
            self._record_realtime_deployment_status(
                project_dir,
                release_version,
                deployment,
            )
            return {
                **self._status_payload(project_dir, self._read_state(project_dir)),
                "operation_id": normalized_operation_id,
                "idempotent_replay": False,
                "realtime_deployment": deployment,
            }

    def retry_release_runtime_deployment(self, *, project_id: str, requested_by: str | None = None) -> dict[str, Any]:
        from .s7_runtime_retry import retry_release_runtime_deployment
        return retry_release_runtime_deployment(self, project_id=project_id, requested_by=requested_by)

    def _enqueue_realtime_deployment(
        self,
        project_dir: Path,
        release_version: str,
    ) -> dict[str, Any]:
        automation = self._release_deployment_automation
        if automation is None:
            publication_path = project_dir / "07-release/publication.json"
            publication = self._read_json(publication_path) if publication_path.exists() else {}
            if (
                publication.get("realtime_query_capability") != "PACKAGED_ARTIFACT_VERIFIED"
                and publication.get("document_runtime_capability")
                != "CURRENT_VERSION_SEARCH_PACKAGED"
            ):
                return {
                    "schema_version": 1,
                    "project_id": project_dir.name,
                    "release_version": release_version,
                    "state": "NOT_APPLICABLE",
                    "artifact_verified": True,
                    "runtime_verified": False,
                    "message": "This release has no runtime contract.",
                }
            return {
                "schema_version": 1,
                "project_id": project_dir.name,
                "release_version": release_version,
                "state": "AUTOMATION_DISABLED",
                "artifact_verified": False,
                "runtime_verified": False,
                "degraded_reason": "ORION_S7_AUTO_DEPLOY is not enabled",
            }
        try:
            return automation.enqueue(
                project_dir=project_dir,
                release_version=release_version,
            )
        except Exception as exc:
            return {
                "schema_version": 1,
                "project_id": project_dir.name,
                "release_version": release_version,
                "state": "AUTOMATION_FAILED_TO_QUEUE",
                "artifact_verified": False,
                "runtime_verified": False,
                "degraded_reason": f"{type(exc).__name__}: {exc}",
            }

    def _record_realtime_deployment_status(
        self,
        project_dir: Path,
        release_version: str,
        deployment: dict[str, Any],
    ) -> None:
        """Project runtime readiness into the workflow without rewriting the release package."""

        if not project_dir.is_dir() or not release_version:
            return
        deployment_state = str(deployment.get("state") or "NOT_QUEUED")
        ready_states = {"ONTOP_READY", "DOCUMENT_RUNTIME_READY", "NOT_APPLICABLE"}
        active_states = {"DEPLOYMENT_QUEUED", "DEPLOYING", "RUNTIME_VERIFYING"}
        failed_states = {
            "AUTOMATION_DISABLED",
            "WAITING_CONFIGURATION",
            "DEPLOYMENT_FAILED",
            "DOCUMENT_RUNTIME_FAILED",
            "AUTOMATION_FAILED_TO_QUEUE",
        }
        if deployment_state not in ready_states | active_states | failed_states:
            return

        with self._lock, self._project_operation_lock(project_dir):
            publication_path = project_dir / "07-release/publication.json"
            if not publication_path.exists():
                return
            publication = self._read_json(publication_path)
            if str(publication.get("release_version") or "") != release_version:
                return
            state = self._read_state(project_dir)
            exploration_required = requires_instance_exploration(
                project_dir, release_version, state,
            )
            sync_receipt = deployment.get("semantica_sync") or {}
            exploration_blocked = deployment.get("instance_exploration_gate_failed") is True or (
                exploration_required and deployment_state in ready_states and (
                sync_receipt.get("exploration_status") != "VERIFIED"
                or sync_receipt.get("status") != "SYNCED"
                or sync_receipt.get("registry_verified") is not True
                )
            )
            if exploration_blocked:
                deployment_state = "DEPLOYMENT_FAILED"
                deployment = {**deployment, "state": deployment_state,
                              "runtime_verified": False,
                              "instance_exploration_gate_failed": True,
                              "degraded_reason": "此发布版本要求实例探索回读通过；模型登记不等于实例可用。"}
            projected = {
                "release_version": release_version,
                "state": deployment_state,
                "artifact_verified": bool(deployment.get("artifact_verified")),
                "runtime_verified": bool(deployment.get("runtime_verified")),
                "updated_at": deployment.get("updated_at") or _now(),
                "degraded_reason": deployment.get("degraded_reason"),
                "query_validation": deployment.get("query_validation"),
                "reasoning_validation": deployment.get("reasoning_validation"),
                "validation_progress": deployment.get("validation_progress"),
                "reasoning_validation_progress": deployment.get("reasoning_validation_progress"),
                "failure_category": deployment.get("failure_category"),
                "contract_error": deployment.get("contract_error"),
                "semantica_sync": deployment.get("semantica_sync"),
                "endpoint": deployment.get("endpoint"),
            }
            previous = state.get("release_runtime_status") or {}
            if all(previous.get(key) == value for key, value in projected.items()):
                return

            self._write_json(
                project_dir / "07-release/realtime-deployment-status.json",
                {**deployment, "projected_at": _now()},
            )
            state["release_runtime_status"] = projected
            state["current_stage"] = None if deployment_state in ready_states else "S7"
            if deployment_state in ready_states:
                state["stage_statuses"]["S7"] = "PASSED"
                state["project_status"] = "PUBLISHED"
                state["blocking"] = None
                state["last_error"] = None
                state["resume_point"] = (
                    f"已发布 Ontology Engineering Package {release_version}，运行时已就绪"
                )
                event_type = "REALTIME_DEPLOYMENT_READY"
                gate_status = "PASSED"
            elif deployment_state in active_states:
                state["stage_statuses"]["S7"] = "RUNNING"
                state["project_status"] = (
                    "PACKAGE_PUBLISHED"
                    if deployment_state == "DEPLOYMENT_QUEUED"
                    else "RUNTIME_VERIFYING"
                )
                state["blocking"] = None
                state["last_error"] = None
                state["resume_point"] = f"发布包 {release_version} 已生成，等待运行时验证完成"
                event_type = (
                    "REALTIME_DEPLOYMENT_QUEUED"
                    if deployment_state == "DEPLOYMENT_QUEUED"
                    else "REALTIME_DEPLOYMENT_VERIFYING"
                )
                gate_status = "PENDING"
            else:
                reason = str(
                    deployment.get("degraded_reason") or "发布包已生成，但运行时验证未通过。"
                )
                semantica_sync = deployment.get("semantica_sync")
                contract_invalid = deployment.get("failure_category") == "RELEASE_CONTRACT_INVALID"
                failed_gate = (
                    "G-S7-RELEASE-CONTRACT"
                    if contract_invalid
                    else "G-S7-INSTANCE-EXPLORATION"
                    if exploration_blocked or (
                        exploration_required and sync_receipt.get("exploration_status") == "FAILED"
                    )
                    else "G-S7-SEMANTICA-SYNC"
                    if isinstance(semantica_sync, dict) and (
                        semantica_sync.get("status") not in {"SYNCED", "NOT_REQUIRED"}
                        or (semantica_sync.get("status") == "SYNCED"
                            and semantica_sync.get("registry_verified") is not True)
                    )
                    else "G-S7-RUNTIME"
                )
                # Preserve the release and original approval. Typed contract
                # failures require a new revision; service failures reuse it.
                state["stage_statuses"]["S7"] = "RUNNING"
                state["project_status"] = "PACKAGE_READY_RUNTIME_BLOCKED"
                diagnostics = {
                    "failure_category": deployment.get("failure_category"),
                    "contract_error": deployment.get("contract_error"),
                }
                state["blocking"] = {"gate": failed_gate, "reason": reason, **diagnostics}
                state["last_error"] = {
                    "gate": failed_gate,
                    "message": reason,
                    "at": _now(),
                    **diagnostics,
                }
                state["resume_point"] = (
                    f"S7: 发布包 {release_version} 的冻结事实合同无效；预览影响并创建独立 S3 修订，禁止原样重试"
                    if contract_invalid else
                    f"S7: 发布包 {release_version} 已就绪；仅重试运行时部署与验证"
                )
                event_type = "REALTIME_DEPLOYMENT_FAILED"
                gate_status = "FAILED"

            gate_path = project_dir / "07-release/gate-results.json"
            gate_results = (
                self._read_json(gate_path)
                if gate_path.exists()
                else {
                    "stage": "S7",
                    "gates": [],
                }
            )
            gates = [
                item
                for item in list(gate_results.get("gates") or [])
                if item.get("id") not in {"G-S7-RUNTIME", "G-S7-SEMANTICA-SYNC", "G-S7-INSTANCE-EXPLORATION", "G-S7-RELEASE-CONTRACT"}
            ]
            semantica_sync = deployment.get("semantica_sync")
            if isinstance(semantica_sync, dict) and semantica_sync.get("status") != "NOT_REQUIRED":
                semantica_passed = (
                    semantica_sync.get("status") == "SYNCED"
                    and semantica_sync.get("registry_verified") is True
                )
                gates.append(
                    {
                        "id": "G-S7-SEMANTICA-SYNC",
                        "status": "PASSED" if semantica_passed else gate_status,
                        "release_version": release_version,
                        "source_sha256": semantica_sync.get("source_sha256"),
                        "registry_verified": bool(semantica_sync.get("registry_verified")),
                    }
                )
                receipt = {
                    **semantica_sync,
                    "synced_by": "ORION_S7_PLATFORM_GATE",
                    "recorded_at": _now(),
                    "source_publication_sha256": _file_checksum(publication_path),
                    "source_package_manifest_sha256": publication.get("package_manifest_sha256"),
                }
                self._write_json(
                    project_dir / "07-release/semantica-sync.json",
                    receipt,
                )
                state["last_semantica_sync"] = {
                    "status": receipt.get("status"),
                    "release_version": release_version,
                    "registry_verified": bool(receipt.get("registry_verified")),
                    "recorded_at": receipt["recorded_at"],
                }
            if exploration_required:
                exploration_status = (
                    "FAILED" if exploration_blocked or sync_receipt.get("exploration_status") == "FAILED"
                    else "PASSED" if sync_receipt.get("exploration_status") == "VERIFIED"
                    else "PENDING"
                )
                gates.append({"id": "G-S7-INSTANCE-EXPLORATION",
                              "status": exploration_status,
                              "snapshot_sha256": sync_receipt.get("snapshot_sha256"),
                              "checks": sync_receipt.get("exploration_checks", [])})
            if deployment.get("failure_category") == "RELEASE_CONTRACT_INVALID":
                gates.append({"id": "G-S7-RELEASE-CONTRACT", "status": "FAILED",
                              "contract_error": deployment.get("contract_error")})
            gates.append(
                {
                    "id": "G-S7-RUNTIME",
                    "status": gate_status,
                    "deployment_state": deployment_state,
                }
            )
            gate_results.update(
                {
                    "status": gate_status,
                    "gates": gates,
                    "runtime_checked_at": _now(),
                }
            )
            self._write_json(gate_path, gate_results)
            package_dir = (
                project_dir / "07-release" / f"ontology-engineering-package-{release_version}"
            )
            if package_dir.is_dir():
                package_manifest = self._verify_release_package(package_dir)
                self._atomic_write(
                    project_dir / "07-release/release-report.html",
                    render_s7_report(
                        project_dir / "07-release",
                        self._read_json(project_dir / "project.json"),
                        publication,
                        package_manifest,
                        state["stage_statuses"],
                    ),
                )
            if deployment_state in ready_states:
                state["stage_fingerprints"]["S7"] = {
                    "input": _fingerprint(publication),
                    "output": self._stage_fingerprint(project_dir / "07-release"),
                    "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
                }
            else:
                state["stage_fingerprints"].pop("S7", None)
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                event_type,
                state,
                {
                    "stage": "S7",
                    "release_version": release_version,
                    "deployment_state": deployment_state,
                    "artifact_verified": bool(deployment.get("artifact_verified")),
                    "runtime_verified": bool(deployment.get("runtime_verified")),
                    "query_validation_status": (
                        (deployment.get("query_validation") or {}).get("status")
                    ),
                    "reasoning_validation_status": (
                        (deployment.get("reasoning_validation") or {}).get("status")
                    ),
                    "semantica_sync_status": (
                        (deployment.get("semantica_sync") or {}).get("status")
                    ),
                    "semantica_registry_verified": (
                        (deployment.get("semantica_sync") or {}).get("registry_verified")
                    ),
                    "degraded_reason": deployment.get("degraded_reason"),
                },
            )
            self._refresh_manifest(project_dir)

    def _sync_release_ontology_for_deployment(
        self,
        *,
        project_dir: Path,
        release_version: str,
        semantica_url: str,
    ) -> dict[str, Any]:
        """Sync only an integrity-verified immutable package during S7 promotion."""

        publication_path = project_dir / "07-release/publication.json"
        publication = self._read_json(publication_path)
        if str(publication.get("release_version") or "") != release_version:
            raise WorkflowError("Semantica 同步版本与 publication.json 不一致。")
        package_dir = project_dir / "07-release" / f"ontology-engineering-package-{release_version}"
        self._verify_release_package(package_dir)
        package_manifest_sha256 = _file_checksum(package_dir / "manifest.json")
        if publication.get("package_manifest_sha256") != package_manifest_sha256:
            raise WorkflowError("Semantica 同步源发布包指纹与 publication.json 不一致。")
        result = PublishedOntologySemanticaSync(
            base_url=semantica_url,
            enabled=True,
        ).sync(
            project_dir=project_dir,
            project_id=project_dir.name,
            project_name=str(
                self._read_json(project_dir / "project.json").get("project_name")
                or project_dir.name
            ),
            release_version=release_version,
        )
        return {
            **result,
            "source_integrity_status": "PASSED",
            "source_package_manifest_sha256": package_manifest_sha256,
        }

    @staticmethod
    def _next_patch_version(version: str) -> str | None:
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?", version)
        if not match:
            return None
        major, minor, patch = (int(value) for value in match.groups())
        return f"{major}.{minor}.{patch + 1}"

    def _project_ontology_iri(self, project_dir: Path) -> str:
        publication_path = project_dir / "07-release/publication.json"
        if publication_path.exists():
            publication = self._read_json(publication_path)
            published_iri = str(publication.get("ontology_iri") or "").strip()
            if published_iri:
                return published_iri
        design_path = project_dir / "04-ontology-design/ontology-design.yaml"
        if design_path.exists():
            design = yaml.safe_load(design_path.read_text(encoding="utf-8")) or {}
            design_iri = str(design.get("ontology_iri") or "").strip()
            if design_iri:
                return design_iri
        build_path = project_dir / "05-ontology-build/protege-build-report.json"
        if build_path.exists():
            build = self._read_json(build_path)
            return str(build.get("ontology_iri") or "").strip()
        return ""

    def _assert_release_identity_available(
        self,
        *,
        project_dir: Path,
        release_version: str,
    ) -> None:
        ontology_iri = self._project_ontology_iri(project_dir)
        if not ontology_iri:
            raise WorkflowGateError(
                "G-S7-ONTOLOGY-IDENTITY",
                "正式发布前必须从 S4/S5 读回稳定的 ontology_iri。",
            )
        for publication_path in sorted(self.root.glob("*/07-release/publication.json")):
            other_dir = publication_path.parent.parent
            if other_dir == project_dir:
                continue
            publication = self._read_json(publication_path)
            if str(publication.get("release_version") or "").strip() != release_version:
                continue
            if self._project_ontology_iri(other_dir) != ontology_iri:
                continue
            revoked = (other_dir / "07-release/release-revocation.json").exists()
            suggestion = self._next_patch_version(release_version)
            suggestion_text = f"；建议改为 {suggestion}" if suggestion else ""
            status_text = "已撤回但仍须保留" if revoked else "仍在使用"
            raise WorkflowGateError(
                "G-S7-RELEASE-IDENTITY",
                f"本体 {ontology_iri} 的版本 {release_version} 已由工程 "
                f"{other_dir.name} 发布（{status_text}），版本号不可复用{suggestion_text}。",
            )

    @staticmethod
    def _release_snapshot_contract(
        *,
        project_id: str,
        release_version: str,
        approval_decision: str,
        approved_by: str,
        release_notes: str,
        integrity: dict[str, Any],
        cq_release_lineage: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": project_id,
            "release_version": release_version,
            "fingerprint_profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
            "formal_stage_fingerprints": {
                item["stage"]: {
                    "stage_status": item["stage_status"],
                    "profile": item["profile"],
                    "recorded_sha256": item["recorded_output"],
                    "verified_sha256": item["current_output"],
                    "verification_status": item["status"],
                }
                for item in integrity["stages"]
            },
            "pre_publish_chain": {
                "verification_status": integrity["audit"]["status"],
                "last_sequence": integrity["audit"]["last_sequence"],
                "last_event_id": integrity["audit"]["last_event_id"],
                "last_event_hash": integrity["audit"]["last_event_hash"],
            },
            "approval_summary": {
                "decision": approval_decision,
                "approved_by": approved_by.strip(),
                "release_notes_sha256": _fingerprint(release_notes.strip()),
            },
            "competency_question_lineage": cq_release_lineage,
            "integrity_status": integrity["status"],
        }

    def _verify_recovered_release_snapshot(
        self,
        package_dir: Path,
        expected_contract: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot_path = package_dir / "04-发布信息/release-snapshot.json"
        if not snapshot_path.exists():
            raise WorkflowGateError(
                "G-S7-RECOVERY-SNAPSHOT",
                "已有原子发布包缺少不可变 release-snapshot.json，拒绝恢复。",
            )
        snapshot = self._read_json(snapshot_path)
        actual_contract = {key: snapshot.get(key) for key in expected_contract}
        if actual_contract != expected_contract:
            raise WorkflowGateError(
                "G-S7-RECOVERY-SNAPSHOT",
                "已有原子发布包绑定的是旧阶段指纹、旧审计头或旧 CQ 结果；"
                "该版本不可覆盖，请修正后使用新的发布版本号。",
            )
        return snapshot

    def _verify_production_readiness(self, project_dir: Path) -> dict[str, Any]:
        """在打包前复核 S6 已签发生产级、全量且版本绑定的资格回执。"""

        quality_path = project_dir / "06-quality-validation/quality-summary.json"
        if not quality_path.is_file():
            raise WorkflowGateError(
                "G-S7-PRODUCTION-READINESS",
                "发布前缺少 S6 quality-summary.json。",
            )
        quality = self._read_json(quality_path)
        coverage = quality.get("production_coverage") or {}
        reasoning = quality.get("reasoning_capability_validation") or {}
        capabilities = reasoning.get("capabilities") or []
        allowed_scopes = {
            "FULL_SOURCE_VALIDATION",
            "FULL_SOURCE_VALIDATION_VIA_BASE_RELEASE_RUNTIME",
        }
        if (
            quality.get("production_ready") is not True
            or quality.get("production_gate_policy_version") != PRODUCTION_GATE_POLICY_VERSION
            or coverage.get("status") != "VERIFIED"
            or coverage.get("validation_scope") != "FULL_SOURCE_VALIDATION"
            or any(
                str(item.get("execution_scope") or "").upper() != "FULL_QUERY_RESULT"
                or str(item.get("validation_scope") or "").upper() not in allowed_scopes
                for item in capabilities
                if isinstance(item, dict)
            )
        ):
            raise WorkflowGateError(
                "G-S7-PRODUCTION-READINESS",
                "S6 未形成当前 production-gates-v1 的全量生产资格回执；"
                "请从最早不满足的阶段重新执行，不能在 S7 补写结论。",
            )
        if str(reasoning.get("requirement") or "").upper() == "REQUIRED" and (
            int(reasoning.get("declared", 0)) < 1
            or int(reasoning.get("validated", 0)) != int(reasoning.get("declared", 0))
        ):
            raise WorkflowGateError(
                "G-S7-PRODUCTION-READINESS",
                "S3 声明必须推理，但 S6 未验证全部推理能力。",
            )
        production_evidence = self._verify_s7_production_evidence(
            project_dir,
            quality=quality,
        )
        return {
            "status": "VERIFIED",
            "policy_version": PRODUCTION_GATE_POLICY_VERSION,
            "quality_summary_sha256": _file_checksum(quality_path),
            "coverage": coverage,
            "production_evidence": production_evidence,
            "reasoning": {
                "requirement": reasoning.get("requirement"),
                "declared": int(reasoning.get("declared", 0)),
                "validated": int(reasoning.get("validated", 0)),
            },
        }

    def _verify_s7_production_evidence(
        self,
        project_dir: Path,
        *,
        quality: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind release eligibility to real versioned inputs and executable artifacts."""

        state = self._read_state(project_dir)
        intake_mode = str(state.get("intake_mode") or "").upper()
        sources: list[dict[str, Any]] = []
        if intake_mode != "DATABASE_ONLY":
            register_path = project_dir / "00-document-evidence/document-register.json"
            evidence_path = project_dir / "00-document-evidence/evidence-index.json"
            if not register_path.is_file() or not evidence_path.is_file():
                raise WorkflowGateError(
                    "G-S7-PRODUCTION-EVIDENCE",
                    "文档生产证据缺少原件登记或证据索引。",
                )
            documents = self._read_json(register_path)
            evidence = self._read_json(evidence_path)
            for document in documents:
                source_sha256 = str(document.get("source_sha256") or "")
                if (
                    not SHA256_PATTERN.fullmatch(source_sha256)
                    or document.get("original_storage_backend") != "minio"
                    or str(document.get("original_snapshot_sha256") or "") != source_sha256
                    or not str(document.get("source_path") or "").strip()
                ):
                    raise WorkflowGateError(
                        "G-S7-PRODUCTION-EVIDENCE",
                        "文档生产证据未绑定 source_path、MinIO 原件快照和一致 SHA-256。",
                    )
                sources.append(
                    {
                        "source_type": "DOCUMENT",
                        "document_id": document.get("document_id"),
                        "source_sha256": source_sha256,
                        "snapshot_uri": document.get("original_source_uri"),
                        "evidence_unit_count": sum(
                            1
                            for item in evidence
                            if item.get("document_id") == document.get("document_id")
                        ),
                    }
                )
            if not sources or any(item["evidence_unit_count"] < 1 for item in sources):
                raise WorkflowGateError(
                    "G-S7-PRODUCTION-EVIDENCE",
                    "文档来源未形成可定位的生产证据单元。",
                )

        if intake_mode != "DOCUMENT_ONLY":
            inventory_path = project_dir / "01-data-understanding/datasource-inventory.json"
            if not inventory_path.is_file():
                raise WorkflowGateError(
                    "G-S7-PRODUCTION-EVIDENCE",
                    "结构化生产证据缺少 datasource-inventory.json。",
                )
            inventory = self._read_json(inventory_path)
            datasets = inventory.get("datasets") or []
            for dataset in datasets:
                # File imports record 'version'; Snapshot Hub datasets record
                # 'snapshot_version'. Both identify the exact frozen snapshot.
                dataset_version = str(
                    dataset.get("version") or dataset.get("snapshot_version") or ""
                ).strip()
                if (
                    str(dataset.get("status") or "").upper() != "READY"
                    or not str(dataset.get("dataset_id") or "").strip()
                    or not dataset_version
                    or not SHA256_PATTERN.fullmatch(str(dataset.get("source_sha256") or ""))
                    or int(dataset.get("row_count", -1)) < 0
                ):
                    raise WorkflowGateError(
                        "G-S7-PRODUCTION-EVIDENCE",
                        "结构化来源未绑定 READY 数据集、版本、精确行数和 SHA-256。",
                    )
                sources.append(
                    {
                        "source_type": "DATABASE_DATASET",
                        "dataset_id": dataset.get("dataset_id"),
                        "source_sha256": dataset.get("source_sha256"),
                        "snapshot_version": dataset_version,
                        "row_count": int(dataset.get("row_count", 0)),
                    }
                )
            if not datasets:
                raise WorkflowGateError(
                    "G-S7-PRODUCTION-EVIDENCE",
                    "结构化来源没有任何 READY 生产数据集。",
                )

        runtime_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
        runtime = self._read_json(runtime_path) if runtime_path.is_file() else {}
        capability_bindings: list[dict[str, Any]] = []
        for name, capability in sorted((runtime.get("reasoning_capabilities") or {}).items()):
            closed_inputs = capability.get("closed_world_inputs") or []
            required_source = capability.get("required_set_source")
            if closed_inputs:
                bound_sources = [required_source, *closed_inputs]
                if any(
                    not isinstance(source, dict)
                    or source.get("dataset_type") != "PRODUCTION_EVIDENCE"
                    or source.get("production_evidence") is not True
                    or not SHA256_PATTERN.fullmatch(str(source.get("source_sha256") or ""))
                    or not str(source.get("snapshot_version") or "").strip()
                    for source in bound_sources
                ):
                    raise WorkflowGateError(
                        "G-S7-PRODUCTION-EVIDENCE",
                        f"推理能力 {name} 仍引用 TEST_ONLY 或不完整闭世界来源。",
                    )
            capability_bindings.append(
                {
                    "capability_name": name,
                    "rule_sha256": capability.get("rule_sha256"),
                    "closed_world_source_count": len(closed_inputs),
                }
            )

        relationship = quality.get("relationship_validation") or {}
        if (project_dir / "04-ontology-design/ontology-design.yaml").is_file():
            design = yaml.safe_load(
                (project_dir / "04-ontology-design/ontology-design.yaml").read_text(
                    encoding="utf-8"
                )
            )
            if (
                _relationship_competency_questions(
                    design.get("competency_questions") or [],
                    design.get("object_properties") or [],
                )
                and relationship.get("status") != "VERIFIED"
            ):
                raise WorkflowGateError(
                    "G-S7-PRODUCTION-EVIDENCE",
                    "关系型本体缺少 S6 实际关系图生产证据。",
                )

        binding = {
            "ontology_sha256": _file_checksum(project_dir / "05-ontology-build/ontology.ttl"),
            "mapping_sha256": _file_checksum(project_dir / "03-mapping-review/mapping.yaml"),
            "runtime_contract_sha256": (
                _file_checksum(runtime_path) if runtime_path.is_file() else None
            ),
            "executor_capabilities_sha256": _fingerprint(reasoning_execution_capabilities()),
            "s6_quality_sha256": _fingerprint(quality),
        }
        return {
            "status": "VERIFIED",
            "production_evidence": True,
            "test_only_evidence_accepted": False,
            "sources": sources,
            "capability_bindings": capability_bindings,
            "artifact_binding": binding,
        }

    def _publish_ontology_package_unlocked(
        self,
        *,
        project_id: str,
        release_version: str,
        approval_decision: str,
        approved_by: str,
        release_notes: str,
    ) -> dict[str, Any]:
        """显式批准后生成可校验、可追溯、可版本化的工程发布包。"""

        if approval_decision != "APPROVED":
            raise WorkflowGateError("G-S7-HUMAN-APPROVAL", "S7 只有明确 APPROVED 才能发布。")
        if not approved_by.strip() or not release_notes.strip():
            raise WorkflowGateError(
                "G-S7-HUMAN-APPROVAL",
                "approved_by 和 release_notes 不能为空。",
            )
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", release_version):
            raise WorkflowGateError("G-S7-VERSION", "release_version 必须使用语义化版本号。")

        with self._lock:
            project_dir, state = self._require_stage(project_id, "S7")
            production_readiness = self._verify_production_readiness(project_dir)
            ontology_iri = self._project_ontology_iri(project_dir)
            if not ontology_iri:
                raise WorkflowGateError(
                    "G-S7-ONTOLOGY-IDENTITY",
                    "正式发布前必须从 S4/S5 读回稳定的 ontology_iri。",
                )
            integrity = self._verify_project_integrity(
                project_dir,
                state,
                stages=("S0", "S1", "S2", "S3", "S4", "S5", "S6"),
            )
            if integrity["status"] == "FAILED":
                raise WorkflowGateError(
                    "G-S7-INTEGRITY",
                    "正式阶段产物或审计哈希链校验失败，发布已拒绝。",
                )
            cq_release_lineage = self._verify_competency_question_release_lineage(project_dir)
            release_snapshot_contract = self._release_snapshot_contract(
                project_id=project_id,
                release_version=release_version,
                approval_decision=approval_decision,
                approved_by=approved_by,
                release_notes=release_notes,
                integrity=integrity,
                cq_release_lineage=cq_release_lineage,
            )
            stage_dir = project_dir / "07-release"
            stage_dir.mkdir(parents=True, exist_ok=True)
            package_name = f"ontology-engineering-package-{release_version}"
            package_dir = stage_dir / package_name
            for stale_temporary in stage_dir.glob(f".{package_name}.tmp-*"):
                if stale_temporary.is_dir():
                    shutil.rmtree(stale_temporary, ignore_errors=True)

            recovered_package = package_dir.exists()
            if recovered_package:
                package_manifest = self._verify_release_package(package_dir)
                publication = self._read_json(package_dir / "04-发布信息/publication.json")
                expected_publication = {
                    "release_version": release_version,
                    "approval_decision": approval_decision,
                    "approved_by": approved_by.strip(),
                    "release_notes": release_notes.strip(),
                }
                if any(
                    publication.get(key) != value for key, value in expected_publication.items()
                ):
                    raise WorkflowError(f"发布版本已经存在且批准参数不一致：{release_version}")
                self._verify_recovered_release_snapshot(
                    package_dir,
                    release_snapshot_contract,
                )
            else:
                build_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".{package_name}.tmp-",
                        dir=stage_dir,
                    )
                )
                try:
                    model_dir = build_dir / "01-本体模型"
                    engineering_dir = build_dir / "02-工程定义"
                    quality_dir = build_dir / "03-质量结论"
                    release_info_dir = build_dir / "04-发布信息"
                    for folder in (
                        model_dir,
                        engineering_dir,
                        quality_dir,
                        release_info_dir,
                    ):
                        folder.mkdir()

                    core_artifacts = {
                        project_dir / "03-mapping-review/mapping.yaml": engineering_dir
                        / "mapping.yaml",
                        project_dir / "04-ontology-design/ontology-design.yaml": engineering_dir
                        / "ontology-design.yaml",
                        project_dir / "05-ontology-build/ontology.owl": model_dir / "ontology.owl",
                        project_dir / "05-ontology-build/ontology.ttl": model_dir / "ontology.ttl",
                        project_dir / "05-ontology-build/shapes.ttl": model_dir / "shapes.ttl",
                    }
                    quality_artifacts = {
                        project_dir / "06-quality-validation/quality-summary.json": quality_dir
                        / "quality-summary.json",
                        project_dir
                        / "06-quality-validation/competency-question-report.json": quality_dir
                        / "competency-question-report.json",
                        project_dir / "06-quality-validation/hermit-report.json": quality_dir
                        / "hermit-report.json",
                    }
                    for source, target in {**core_artifacts, **quality_artifacts}.items():
                        shutil.copy2(source, target)

                    runtime_contract = package_realtime_runtime(
                        project_dir,
                        build_dir,
                        project_id=project_id,
                        release_version=release_version,
                        ontology_iri=ontology_iri,
                    )

                    publication = {
                        "project_id": project_id,
                        "ontology_iri": ontology_iri,
                        "release_version": release_version,
                        "approval_decision": approval_decision,
                        "approved_by": approved_by.strip(),
                        "release_notes": release_notes.strip(),
                        "published_at": _now(),
                        "workflow_version": WORKFLOW_VERSION,
                        "package_profile": "MODEL_DELIVERY",
                        "instance_data_included": False,
                        "document_evidence_included": False,
                        "audit_bundle_included": False,
                        "realtime_query_capability": (
                            "PACKAGED_ARTIFACT_VERIFIED"
                            if runtime_contract is not None
                            and runtime_contract.get("structured_query_enabled") is not False
                            else "NOT_PACKAGED"
                        ),
                        "document_runtime_capability": (
                            "CURRENT_VERSION_SEARCH_PACKAGED"
                            if runtime_contract is not None
                            and runtime_contract.get("document_query_capabilities")
                            else "NOT_PACKAGED"
                        ),
                        "ontop_deployment_id": (
                            runtime_contract.get("ontop_deployment_id")
                            if runtime_contract is not None
                            else None
                        ),
                        "integrity_verification_status": integrity["status"],
                        "fingerprint_profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
                        "assurance_profile": "PRODUCTION",
                        "production_gate_policy_version": PRODUCTION_GATE_POLICY_VERSION,
                        "production_readiness": production_readiness,
                    }
                    if self._joint_design_enabled(state):
                        fact_dir = build_dir / "05-运行时/document-facts"
                        has_fact_snapshots = fact_dir.is_dir() and any(
                            path.is_file() for path in fact_dir.rglob("*")
                        )
                        publication.update(
                            package_profile="ENGINEERING_DELIVERY",
                            package_contract="package-contract.json",
                            lifecycle_contract_version=project_stage_contract_version(state),
                            instance_data_included=has_fact_snapshots,
                            reviewed_fact_snapshots_included=has_fact_snapshots,
                            full_instance_graph_included=False,
                            raw_document_evidence_included=False,
                        )
                    release_snapshot = {
                        **release_snapshot_contract,
                        "captured_at": publication["published_at"],
                    }
                    self._write_json(
                        release_info_dir / "publication.json",
                        publication,
                    )
                    self._write_json(
                        release_info_dir / "release-snapshot.json",
                        release_snapshot,
                    )
                    hermit_report = self._read_json(quality_dir / "hermit-report.json")
                    owl_dl_review_contract = {
                        "schema_version": 1,
                        "project_id": project_id,
                        "release_version": release_version,
                        "ontology_artifact": "01-本体模型/ontology.owl",
                        "ontology_sha256": _file_checksum(model_dir / "ontology.owl"),
                        "reasoner": "HERMIT",
                        "expected_status": str(hermit_report.get("status") or ""),
                        "expected_consistent": hermit_report.get("consistent") is True,
                        "expected_unsatisfiable_class_count": int(
                            hermit_report.get("unsatisfiable_class_count") or 0
                        ),
                        "source_validation_run_id": hermit_report.get("run_id"),
                        "review_mode": "COPY_BEFORE_DESKTOP_REVIEW",
                    }
                    self._write_json(
                        release_info_dir / "owl-dl-review-contract.json",
                        owl_dl_review_contract,
                    )
                    self._atomic_write(
                        release_info_dir / "Protégé-HermiT复核说明.md",
                        self._render_protege_hermit_review_guide(
                            state=state,
                            publication=publication,
                            contract=owl_dl_review_contract,
                            workflow_root=self.root,
                        ),
                    )
                    release_snapshot_sha256 = _file_checksum(
                        release_info_dir / "release-snapshot.json"
                    )
                    audit_reference = {
                        "project_id": project_id,
                        "release_version": release_version,
                        "audit_storage": "PROJECT_WORKSPACE",
                        "audit_bundle_included": False,
                        "instance_data_included": False,
                        "document_evidence_included": False,
                        "immutable_release_snapshot": {
                            "path": "04-发布信息/release-snapshot.json",
                            "sha256": release_snapshot_sha256,
                        },
                        "workspace_audit_locations": {
                            "artifact_manifest": "artifact-manifest.json",
                            "event_trace": "events/agent-trace.jsonl",
                        },
                        "note": (
                            "发布真实性以包内不可变 release-snapshot.json 和 manifest.json 为准；"
                            "项目工作区中的 manifest 与事件流会随后续审计继续变化。"
                        ),
                    }
                    if self._joint_design_enabled(state):
                        audit_reference.update(
                            instance_data_included=publication["instance_data_included"],
                            reviewed_fact_snapshots_included=publication["reviewed_fact_snapshots_included"],
                            full_instance_graph_included=False,
                            raw_document_evidence_included=False,
                            package_contract="package-contract.json",
                        )
                    self._write_json(
                        release_info_dir / "audit-reference.json",
                        audit_reference,
                    )
                    report = (
                        f"# 本体工程发布包 {release_version}\n\n"
                        f"- 项目：{state['project_name']} (`{project_id}`)\n"
                        f"- 批准人：{approved_by.strip()}\n"
                        f"- 发布决定：已批准\n"
                        f"- 发布时间：{publication['published_at']}\n\n"
                        "## 发布说明\n\n"
                        f"{release_notes.strip()}\n\n"
                        "## 包含内容\n\n"
                        "默认交付包包含本体模型、工程定义、质量结论和发布信息。"
                        + (
                            "本版本同时包含已评审的结构化与文档只读问答能力合同。\n\n"
                            if runtime_contract is not None
                            and runtime_contract.get("structured_query_enabled") is not False
                            else "本版本包含已评审的纯文档只读问答能力合同。\n\n"
                        )
                        + "## 审计边界\n\n"
                        "真实实例图、OCR 原文、阶段技术报告和 MCP 明细仍保留在项目工作区，"
                        "不随普通交付包分发；需要时应经过授权单独导出审计包。\n"
                    )
                    if self._joint_design_enabled(state):
                        report += (
                            "\n## 工程追溯与规则\n\n"
                            "本版本包含 S0—S7 阶段产物索引、联合设计基线和业务规则目录，"
                            "复制资产与仅引用资产见 package-contract.json。"
                            "已评审的文档事实快照如进入运行包，将在该清单中单独标明；"
                            "阶段技术报告、原始资料和完整审计仍通过工程引用查看。\n"
                        )
                    self._atomic_write(build_dir / "发布说明.md", report)
                    self._atomic_write(
                        build_dir / "打开查看.html",
                        self._render_release_summary(state, publication),
                    )
                    if self._joint_design_enabled(state):
                        try:
                            write_engineering_delivery_assets(
                                project_dir, build_dir, project_id=project_id,
                                release_version=release_version,
                                stage_statuses=state["stage_statuses"],
                                lifecycle_contract_version=project_stage_contract_version(state),
                            )
                        except DeliveryPackageError as exc:
                            raise WorkflowGateError("G-S7-ENGINEERING-DELIVERY", str(exc)) from exc
                    package_files = [
                        {
                            "path": path.relative_to(build_dir).as_posix(),
                            "sha256": _file_checksum(path),
                        }
                        for path in sorted(build_dir.rglob("*"))
                        if path.is_file() and path.name != "manifest.json"
                    ]
                    self._write_json(
                        build_dir / "manifest.json",
                        {
                            **publication,
                            "file_count": len(package_files),
                            "files": package_files,
                        },
                    )
                    package_manifest = self._verify_release_package(build_dir)
                    for file_path in sorted(build_dir.rglob("*")):
                        if file_path.is_file():
                            with file_path.open("rb") as handle:
                                os.fsync(handle.fileno())
                    self._fsync_directory(build_dir)
                    os.replace(build_dir, package_dir)
                    self._fsync_directory(stage_dir)
                except FileNotFoundError as exc:
                    shutil.rmtree(build_dir, ignore_errors=True)
                    raise WorkflowGateError(
                        "G-S7-PACKAGE-COMPLETE",
                        f"发布包缺少必需资产：{Path(exc.filename or '').name}",
                    ) from exc
                except RuntimeReleaseError as exc:
                    shutil.rmtree(build_dir, ignore_errors=True)
                    raise WorkflowGateError("G-S7-RUNTIME", str(exc)) from exc
                except Exception:
                    shutil.rmtree(build_dir, ignore_errors=True)
                    raise

            package_manifest = self._verify_release_package(package_dir)
            package_manifest_sha256 = _file_checksum(package_dir / "manifest.json")
            release_snapshot_sha256 = _file_checksum(
                package_dir / "04-发布信息/release-snapshot.json"
            )
            workspace_publication = {
                **publication,
                "package_path": package_dir.relative_to(project_dir).as_posix(),
                "package_manifest_sha256": package_manifest_sha256,
                "release_snapshot_sha256": release_snapshot_sha256,
                "recovered_atomic_package": recovered_package,
            }
            self._write_json(stage_dir / "publication.json", workspace_publication)
            pending_statuses = {**state["stage_statuses"], "S7": "RUNNING"}
            self._atomic_write(
                stage_dir / "release-report.html",
                render_s7_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    workspace_publication,
                    package_manifest,
                    pending_statuses,
                ),
            )
            semantica_sync = PublishedOntologySemanticaSync().sync(
                project_dir=project_dir,
                project_id=project_id,
                project_name=str(state.get("project_name") or project_id),
                release_version=release_version,
            )
            self._write_json(stage_dir / "semantica-sync.json", semantica_sync)
            self._write_json(
                stage_dir / "gate-results.json",
                {
                    "stage": "S7",
                    "status": "PENDING",
                    "gates": [
                        {"id": "G-S7-HUMAN-APPROVAL", "status": "PASSED"},
                        {"id": "G-S7-INTEGRITY", "status": "PASSED"},
                        {"id": "G-S7-PACKAGE-COMPLETE", "status": "PASSED"},
                        {"id": "G-S7-MANIFEST", "status": "PASSED"},
                        {"id": "G-S7-CQ-LINEAGE", "status": "PASSED"},
                        {"id": "G-S7-PRODUCTION-READINESS", "status": "PASSED"},
                        {"id": "G-S7-PRODUCTION-EVIDENCE", "status": "PASSED"},
                        {"id": "G-S7-RECOVERY-SNAPSHOT", "status": "PASSED"},
                        {
                            "id": "G-S7-RUNTIME",
                            "status": "PENDING",
                        },
                    ],
                    "package_path": package_dir.relative_to(project_dir).as_posix(),
                    "package_manifest_sha256": package_manifest_sha256,
                    "release_snapshot_sha256": release_snapshot_sha256,
                    "checked_at": _now(),
                },
            )
            state["stage_statuses"]["S7"] = "RUNNING"
            state["stage_fingerprints"].pop("S7", None)
            state["last_integrity_verification"] = {
                "status": integrity["status"],
                "verified_at": integrity["verified_at"],
                "fingerprint_profile": integrity["fingerprint_profile"],
                "audit_status": integrity["audit"]["status"],
            }
            state["current_stage"] = "S7"
            state["project_status"] = "PACKAGE_PUBLISHED"
            state["blocking"] = None
            state["last_error"] = None
            state["resume_point"] = f"发布包 {release_version} 已生成，等待运行时验证完成"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ONTOLOGY_PACKAGE_PUBLISHED",
                state,
                {
                    "stage": "S7",
                    "release_version": release_version,
                    "semantica_sync_status": semantica_sync["status"],
                    "package_manifest_sha256": package_manifest_sha256,
                    "release_snapshot_sha256": release_snapshot_sha256,
                },
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def sync_published_ontology_to_semantica(
        self,
        *,
        project_id: str,
        synced_by: str,
        semantica_url: str | None = None,
        expected_revision: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Synchronize a published release through the locked workflow write path."""

        if not synced_by.strip():
            raise WorkflowError("synced_by 不能为空。")
        with self._project_mutation_lock(project_id) as project_dir:
            state = self._read_state(project_dir)
            if state.get("project_status") != "PUBLISHED":
                raise WorkflowError("只有已发布且未撤回的工程可以补同步到 Semantica。")
            self._require_expected_revision(state, expected_revision)
            publication_path = project_dir / "07-release/publication.json"
            if not publication_path.exists():
                raise WorkflowError("已发布工程缺少 publication.json，不能补同步。")
            publication = self._read_json(publication_path)
            release_version = str(publication.get("release_version") or "")
            normalized_operation_id = self._normalize_operation_id(operation_id)
            request_fingerprint = _fingerprint(
                {
                    "operation": "sync_published_ontology_to_semantica",
                    "project_id": project_id,
                    "release_version": release_version,
                    "semantica_url": str(semantica_url or "").rstrip("/"),
                }
            )
            replay = self._operation_replay(
                project_dir,
                normalized_operation_id,
                request_fingerprint,
            )
            receipt_path = project_dir / "07-release/semantica-sync.json"
            replay_receipt = (
                self._read_json(receipt_path)
                if replay is not None and receipt_path.exists()
                else None
            )

            integrity = self._verify_project_integrity(
                project_dir,
                state,
                stages=("S5",),
            )
            if integrity["status"] == "FAILED":
                raise WorkflowGateError(
                    "G-SEMANTICA-SOURCE-INTEGRITY",
                    "S5 正式本体完整性校验失败，已拒绝把可能被篡改的文件同步到 Semantica。",
                )
            result = PublishedOntologySemanticaSync(
                base_url=semantica_url,
                enabled=True,
            ).sync(
                project_dir=project_dir,
                project_id=project_id,
                project_name=str(state.get("project_name") or project_id),
                release_version=release_version,
            )
            if (
                replay_receipt is not None
                and result.get("status") == "SYNCED"
                and result.get("idempotent_replay") is True
            ):
                # An operation receipt proves that ORION completed a previous
                # write, not that an external Semantica runtime still contains
                # it. Always perform the safe registry read-back above. Only
                # reuse the receipt after the current runtime confirms the exact
                # project, release, source hash and importer profile.
                return {
                    **replay_receipt,
                    **result,
                    "operation_id": normalized_operation_id,
                    "idempotent_replay": True,
                    "live_verified_at": _now(),
                }
            package_dir = (
                project_dir / "07-release" / f"ontology-engineering-package-{release_version}"
            )
            package_manifest_path = package_dir / "manifest.json"
            receipt = {
                **result,
                "synced_by": synced_by.strip(),
                "recorded_at": _now(),
                "source_publication_sha256": _file_checksum(publication_path),
                "source_package_manifest_sha256": (
                    _file_checksum(package_manifest_path)
                    if package_manifest_path.exists()
                    else None
                ),
                "source_integrity_status": integrity["status"],
                "operation_id": normalized_operation_id,
            }
            self._write_json(receipt_path, receipt)
            state["last_semantica_sync"] = {
                "status": receipt["status"],
                "release_version": release_version,
                "recorded_at": receipt["recorded_at"],
            }
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "PUBLISHED_ONTOLOGY_SEMANTICA_SYNCED",
                state,
                {
                    "stage": "S7",
                    "release_version": release_version,
                    "status": receipt["status"],
                    "semantica_url": receipt.get("semantica_url"),
                    "synced_by": synced_by.strip(),
                    "operation_id": normalized_operation_id,
                },
            )
            self._refresh_manifest(project_dir)
            if receipt["status"] == "SYNCED":
                self._record_operation_receipt(
                    project_dir,
                    normalized_operation_id,
                    request_fingerprint,
                    "sync_published_ontology_to_semantica",
                )
            return receipt

    def defer_ontology_publication(
        self,
        *,
        project_id: str,
        decided_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """在 S7 明确选择暂不发布，不生成发布包。"""

        if not decided_by.strip() or not reason.strip():
            raise WorkflowError("decided_by 和 reason 都不能为空。")
        with self._project_mutation_lock(project_id):
            project_dir, state = self._require_stage(project_id, "S7")
            decision = {
                "decision": "DEFERRED",
                "decision_label": "暂不发布",
                "decided_by": decided_by.strip(),
                "reason": reason.strip(),
                "decided_at": _now(),
            }
            self._write_json(project_dir / "07-release/release-decision.json", decision)
            state["stage_statuses"]["S7"] = "DEFERRED"
            state["project_status"] = "RELEASE_DEFERRED"
            state["current_stage"] = "S7"
            state["blocking"] = None
            state["resume_point"] = "S7 已暂缓：可恢复发布审批，或回退前序阶段继续调整"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ONTOLOGY_PUBLICATION_DEFERRED",
                state,
                {
                    "stage": "S7",
                    "reason": reason.strip(),
                    "actor": decided_by.strip(),
                },
            )
            stage_dir = project_dir / "07-release"
            self._atomic_write(
                stage_dir / "release-decision-report.html",
                render_s7_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    {},
                    {},
                    state["stage_statuses"],
                    {**decision, "source_path": "release-decision.json"},
                ),
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def resume_ontology_publication(
        self,
        *,
        project_id: str,
        resumed_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """恢复被暂缓的 S7 发布审批，仍需再次明确批准才能发布。"""

        if not resumed_by.strip() or not reason.strip():
            raise WorkflowError("resumed_by 和 reason 都不能为空。")
        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            if state.get("stage_statuses", {}).get("S7") != "DEFERRED":
                raise WorkflowError("当前项目没有处于暂不发布状态。")
            resumption = {
                "status": "RUNNING",
                "decision_label": "恢复发布评审",
                "resumed_by": resumed_by.strip(),
                "reason": reason.strip(),
                "resumed_at": _now(),
                "source_path": "release-resumption.json",
            }
            self._write_json(project_dir / "07-release/release-resumption.json", resumption)
            state["stage_statuses"]["S7"] = "RUNNING"
            state["project_status"] = "IN_PROGRESS"
            state["current_stage"] = "S7"
            state["resume_point"] = "S7: 已恢复，等待负责人重新确认是否发布"
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ONTOLOGY_PUBLICATION_RESUMED",
                state,
                {
                    "stage": "S7",
                    "reason": reason.strip(),
                    "actor": resumed_by.strip(),
                },
            )
            stage_dir = project_dir / "07-release"
            self._atomic_write(
                stage_dir / "release-resumption-report.html",
                render_s7_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    {},
                    {},
                    state["stage_statuses"],
                    resumption,
                ),
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def revoke_ontology_release(
        self,
        *,
        project_id: str,
        release_version: str,
        revoked_by: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._project_mutation_lock(project_id) as project_dir:
            self._require_expected_revision(self._read_state(project_dir), expected_revision)
            return self._revoke_ontology_release_unlocked(
                project_id=project_id,
                release_version=release_version,
                revoked_by=revoked_by,
                reason=reason,
            )

    def _revoke_ontology_release_unlocked(
        self,
        *,
        project_id: str,
        release_version: str,
        revoked_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """撤回已发布版本的使用资格；原包不可修改、不可删除。"""

        if not revoked_by.strip() or not reason.strip():
            raise WorkflowError("revoked_by 和 reason 都不能为空。")
        with self._lock:
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            publication_path = project_dir / "07-release/publication.json"
            if (
                state.get("project_status")
                not in {
                    "PACKAGE_PUBLISHED",
                    "RUNTIME_VERIFYING",
                    "RUNTIME_FAILED",
                    "PACKAGE_READY_RUNTIME_BLOCKED",
                    "PUBLISHED",
                }
                or not publication_path.exists()
            ):
                raise WorkflowError("只有已生成发布包的项目才能执行发布撤回。")
            publication = self._read_json(publication_path)
            if str(publication.get("release_version")) != release_version:
                raise WorkflowError(f"项目没有已发布版本：{release_version}")
            revocation_path = project_dir / "07-release/release-revocation.json"
            if revocation_path.exists():
                raise WorkflowError(f"版本 {release_version} 已经撤回。")
            revocation = {
                "project_id": project_id,
                "release_version": release_version,
                "status": "REVOKED",
                "revoked_by": revoked_by.strip(),
                "reason": reason.strip(),
                "revoked_at": _now(),
                "original_publication_sha256": _file_checksum(publication_path),
                "package_preserved": True,
                "note": "撤回只改变使用状态；原发布包和原发布记录保持不变，供审计回读。",
            }
            self._write_json(revocation_path, revocation)
            state["stage_statuses"]["S7"] = "REVOKED"
            state["project_status"] = "RELEASE_REVOKED"
            state["current_stage"] = None
            state["resume_point"] = (
                f"版本 {release_version} 已撤回；如需修正，请基于该版本新建修订项目"
            )
            self._save_state(project_dir, state)
            self._append_event(
                project_dir,
                "ONTOLOGY_RELEASE_REVOKED",
                state,
                {
                    "stage": "S7",
                    "release_version": release_version,
                    "reason": reason.strip(),
                    "actor": revoked_by.strip(),
                },
            )
            stage_dir = project_dir / "07-release"
            self._atomic_write(
                stage_dir / "release-revocation-report.html",
                render_s7_report(
                    stage_dir,
                    self._read_json(project_dir / "project.json"),
                    publication,
                    {},
                    state["stage_statuses"],
                    {**revocation, "source_path": "release-revocation.json"},
                ),
            )
            self._refresh_manifest(project_dir)
            return self._status_payload(project_dir, state)

    def create_revision_from_release(
        self,
        *,
        project_id: str,
        release_version: str,
        target_stage: str,
        reason: str,
        requested_by: str,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with (
            self._engineering_project_guard(project_id),
            self._lock,
            self._root_operation_lock(),
            self._project_mutation_lock(project_id) as source_dir,
        ):
            self._require_expected_revision(self._read_state(source_dir), expected_revision)
            return self._create_revision_from_release_unlocked(
                project_id=project_id,
                release_version=release_version,
                target_stage=target_stage,
                reason=reason,
                requested_by=requested_by,
                request_id=request_id,
            )

    def _create_revision_from_release_unlocked(
        self,
        *,
        project_id: str,
        release_version: str,
        target_stage: str,
        reason: str,
        requested_by: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """从不可变发布版本创建独立修订项目，并回到指定阶段继续。"""

        if target_stage not in {"S0", "S1", "S2", "S3", "S4", "S5", "S6"}:
            raise WorkflowError("修订项目只能回到 S0 至 S6。")
        if not reason.strip() or not requested_by.strip():
            raise WorkflowError("reason 和 requested_by 都不能为空。")
        with self._lock:
            source_dir = self._resolve_project(project_id)
            source_state = self._read_state(source_dir)
            if target_stage == "S1" and source_state.get("intake_mode") == "DOCUMENT_ONLY":
                raise WorkflowError("资料建模项目的 S1 不适用；修订时请选择 S0 或 S2。")
            publication_path = source_dir / "07-release/publication.json"
            if not publication_path.exists():
                raise WorkflowError("源项目没有正式发布记录。")
            publication = self._read_json(publication_path)
            if str(publication.get("release_version")) != release_version:
                raise WorkflowError(f"源项目没有已发布版本：{release_version}")
            suggested_release_version = self._next_patch_version(release_version)
            source_project = self._read_json(source_dir / "project.json")
            source_cq_intake_path = source_dir / "00-document-evidence/cq-intake.json"
            source_cq_intake = (
                self._read_json(source_cq_intake_path)
                if source_cq_intake_path.exists()
                else {"mode": "USER_PLUS_AI", "questions": []}
            )
            source_cq_mode = str(
                source_cq_intake.get("mode")
                or source_project.get("cq_mode")
                or source_state.get("cq_mode")
                or "USER_PLUS_AI"
            )
            source_cq_questions = list(source_cq_intake.get("questions") or [])
            created = self._create_project_unlocked(
                project_name=_revision_project_name(source_project.get("project_name", project_id)),
                domain=str(source_project.get("domain") or project_id),
                datasource_label=source_project.get("datasource_label"),
                table_scope=list(source_project.get("table_scope") or []),
                intake_mode=str(
                    source_project.get("intake_mode") or source_state.get("intake_mode") or "HYBRID"
                ),
                intake_rationale=(
                    f"基于 {project_id} v{release_version} 回到 {target_stage} 修订："
                    f"{reason.strip()}"
                ),
                cq_mode=source_cq_mode,
                initial_competency_questions=source_cq_questions,
                request_id=request_id,
                source_scope=source_project.get("source_scope"),
                _stage_contract_version=project_stage_contract_version(source_state),
                _initial_project_status="INITIALIZING",
            )
            revision_project_id = str(created["project_id"])
            revision_dir = self.root / revision_project_id
            with self._project_operation_lock(revision_dir):
                existing_state = self._read_state(revision_dir)
                source_reference_path = revision_dir / "based-on-release.json"
                completion_path = revision_dir / ".revision-initialization-complete.json"
                initialization_event_exists = any(
                    event.get("event_type") == "REVISION_PROJECT_CREATED_FROM_RELEASE"
                    for event in self._read_events(revision_dir)
                )
                if (
                    existing_state.get("project_status") != "INITIALIZING"
                    and source_reference_path.exists()
                    and initialization_event_exists
                    and completion_path.exists()
                ):
                    return {
                        **self._status_payload(revision_dir, existing_state),
                        "source_project_id": project_id,
                        "source_release_version": release_version,
                        "request_id": request_id,
                        "idempotent_replay": bool(created.get("idempotent_replay")),
                    }

                target_index = STAGES.index(target_stage)
                for stage in STAGES[:target_index]:
                    source_stage = source_dir / STAGE_FOLDERS[stage]
                    target_stage_dir = revision_dir / STAGE_FOLDERS[stage]
                    if source_stage.exists():
                        shutil.copytree(source_stage, target_stage_dir, dirs_exist_ok=True)
                if source_cq_intake_path.exists():
                    shutil.copy2(
                        source_cq_intake_path,
                        revision_dir / "00-document-evidence/cq-intake.json",
                    )

                revision_state = self._read_state(revision_dir)
                for index, stage in enumerate(STAGES):
                    if index < target_index:
                        revision_state["stage_statuses"][stage] = source_state[
                            "stage_statuses"
                        ].get(stage, "PASSED")
                        if stage in source_state.get("stage_fingerprints", {}):
                            revision_state["stage_fingerprints"][stage] = source_state[
                                "stage_fingerprints"
                            ][stage]
                    elif index == target_index:
                        revision_state["stage_statuses"][stage] = "RUNNING"
                        revision_state["stage_fingerprints"].pop(stage, None)
                    else:
                        revision_state["stage_statuses"][stage] = "PENDING"
                        revision_state["stage_fingerprints"].pop(stage, None)
                revision_state["current_stage"] = target_stage
                revision_state["project_status"] = "IN_PROGRESS"
                revision_state["blocking"] = None
                revision_state["workflow_version"] = WORKFLOW_VERSION
                revision_state["cq_mode"] = source_cq_mode
                revision_state["initial_competency_question_count"] = len(source_cq_questions)
                revision_state["based_on_release"] = {
                    "project_id": project_id,
                    "release_version": release_version,
                }
                revision_state["suggested_release_version"] = suggested_release_version
                revision_state["resume_point"] = (
                    f"{target_stage}: 基于 {project_id} v{release_version} 修订，原因：{reason.strip()}"
                )
                revision_project = self._read_json(revision_dir / "project.json")
                revision_project["parent_project_id"] = project_id
                revision_project["based_on_release_version"] = release_version
                revision_project["suggested_release_version"] = suggested_release_version
                revision_project["current_stage"] = target_stage
                revision_project["status"] = "IN_PROGRESS"
                revision_project["cq_mode"] = source_cq_mode
                revision_project["initial_competency_question_count"] = len(source_cq_questions)
                self._write_json(revision_dir / "project.json", revision_project)
                package_manifest = (
                    source_dir
                    / "07-release"
                    / f"ontology-engineering-package-{release_version}"
                    / "manifest.json"
                )
                source_reference = {
                    "source_project_id": project_id,
                    "source_release_version": release_version,
                    "suggested_release_version": suggested_release_version,
                    "target_stage": target_stage,
                    "reason": reason.strip(),
                    "requested_by": requested_by.strip(),
                    "created_at": _now(),
                    "source_publication_sha256": _file_checksum(publication_path),
                    "source_manifest_sha256": (
                        _file_checksum(package_manifest) if package_manifest.exists() else None
                    ),
                    "source_package_preserved": True,
                }
                self._write_json(source_reference_path, source_reference)
                self._save_state(revision_dir, revision_state)
                if not initialization_event_exists:
                    self._append_event(
                        revision_dir,
                        "REVISION_PROJECT_CREATED_FROM_RELEASE",
                        revision_state,
                        {
                            "stage": target_stage,
                            "source_project_id": project_id,
                            "source_release_version": release_version,
                            "reason": reason.strip(),
                            "actor": requested_by.strip(),
                        },
                    )
                self._refresh_manifest(revision_dir)
                self._write_json(
                    completion_path,
                    {
                        "schema_version": 1,
                        "status": "COMPLETE",
                        "source_project_id": project_id,
                        "source_release_version": release_version,
                        "target_stage": target_stage,
                        "completed_at": _now(),
                    },
                )
                return {
                    **self._status_payload(revision_dir, revision_state),
                    "source_project_id": project_id,
                    "source_release_version": release_version,
                    "suggested_release_version": suggested_release_version,
                    "request_id": request_id,
                    "idempotent_replay": bool(created.get("idempotent_replay")),
                }

    def export_published_delivery_package(
        self,
        *,
        project_id: str,
        release_version: str,
        exported_by: str,
    ) -> dict[str, Any]:
        """为已发布版本生成不含实例数据和审计底稿的精简交付副本。"""

        if not exported_by.strip():
            raise WorkflowError("exported_by 不能为空。")
        with self._project_mutation_lock(project_id):
            project_dir = self._resolve_project(project_id)
            state = self._read_state(project_dir)
            if state.get("project_status") != "PUBLISHED":
                raise WorkflowError("只有已发布项目才能生成精简交付副本。")
            workspace_publication = self._read_json(project_dir / "07-release/publication.json")
            if workspace_publication.get("release_version") != release_version:
                raise WorkflowError(f"不存在已发布版本：{release_version}")

            source_package = (
                project_dir / "07-release" / f"ontology-engineering-package-{release_version}"
            )
            source_manifest = self._verify_release_package(source_package)
            source_manifest_sha256 = _file_checksum(source_package / "manifest.json")
            if (
                source_manifest.get("project_id") != project_id
                or source_manifest.get("release_version") != release_version
                or workspace_publication.get("package_manifest_sha256") != source_manifest_sha256
            ):
                raise WorkflowGateError(
                    "G-S7-DELIVERY-SOURCE-CONTRACT",
                    "不可变发布包与项目、版本或工作区发布回执不一致，拒绝生成交付副本。",
                )

            source_publication = self._read_json(source_package / "04-发布信息/publication.json")
            source_snapshot_path = source_package / "04-发布信息/release-snapshot.json"
            source_cq_report_path = source_package / "03-质量结论/competency-question-report.json"
            if not source_snapshot_path.exists() or not source_cq_report_path.exists():
                raise WorkflowGateError(
                    "G-S7-DELIVERY-CQ-CONTRACT",
                    "该历史发布包缺少不可变发布快照或 CQ 执行报告；"
                    "不能按新版合同标记为已验证，请从旧版本创建修订后重新完成 S4-S7。",
                )
            source_snapshot_sha256 = _file_checksum(source_snapshot_path)
            source_snapshot = self._read_json(source_snapshot_path)
            source_cq_report = self._read_json(source_cq_report_path)
            cq_lineage = source_snapshot.get("competency_question_lineage") or {}
            if (
                source_publication.get("project_id") != project_id
                or source_publication.get("release_version") != release_version
                or workspace_publication.get("release_snapshot_sha256") != source_snapshot_sha256
                or source_snapshot.get("project_id") != project_id
                or source_snapshot.get("release_version") != release_version
                or source_snapshot.get("integrity_status") != "PASSED"
                or cq_lineage.get("status") != "VERIFIED"
                or cq_lineage.get("contract_version") not in {"cq-answer-v1", "cq-answer-v2"}
                or int(source_cq_report.get("schema_version") or 0) < 2
                or source_cq_report.get("status") != "PASSED"
                or source_cq_report.get("validation_mode")
                not in SERVER_EXECUTED_CQ_VALIDATION_MODES
            ):
                raise WorkflowGateError(
                    "G-S7-DELIVERY-CQ-CONTRACT",
                    "不可变发布包未通过新版 CQ 服务端答案合同与发布快照校验；"
                    "请基于该版本创建修订并重新完成 S4-S7。",
                )

            export_dir = project_dir / "07-release" / f"ontology-model-delivery-{release_version}"
            if export_dir.exists():
                raise WorkflowError(f"精简交付副本已经存在：{release_version}")
            model_dir = export_dir / "01-本体模型"
            engineering_dir = export_dir / "02-工程定义"
            quality_dir = export_dir / "03-质量结论"
            release_info_dir = export_dir / "04-发布信息"
            for folder in (model_dir, engineering_dir, quality_dir, release_info_dir):
                folder.mkdir(parents=True)

            artifacts = {
                source_package / "01-本体模型/ontology.owl": model_dir / "ontology.owl",
                source_package / "01-本体模型/ontology.ttl": model_dir / "ontology.ttl",
                source_package / "01-本体模型/shapes.ttl": model_dir / "shapes.ttl",
                source_package / "02-工程定义/mapping.yaml": engineering_dir / "mapping.yaml",
                source_package / "02-工程定义/ontology-design.yaml": engineering_dir
                / "ontology-design.yaml",
                source_package / "03-质量结论/quality-summary.json": quality_dir
                / "quality-summary.json",
                source_cq_report_path: quality_dir / "competency-question-report.json",
            }
            try:
                for source, target in artifacts.items():
                    shutil.copy2(source, target)
            except FileNotFoundError as exc:
                shutil.rmtree(export_dir, ignore_errors=True)
                raise WorkflowGateError(
                    "G-S7-DELIVERY-EXPORT-COMPLETE",
                    f"精简交付副本缺少必需资产：{Path(exc.filename or '').name}",
                ) from exc

            publication = {
                **source_publication,
                "package_profile": "MODEL_DELIVERY",
                "instance_data_included": False,
                "document_evidence_included": False,
                "audit_bundle_included": False,
                "exported_at": _now(),
                "exported_by": exported_by.strip(),
                "source_package_manifest_sha256": source_manifest_sha256,
                "source_release_snapshot_sha256": source_snapshot_sha256,
            }
            audit_reference = {
                "project_id": project_id,
                "release_version": release_version,
                "audit_storage": "PROJECT_WORKSPACE",
                "audit_bundle_included": False,
                "instance_data_included": False,
                "document_evidence_included": False,
                "source_release_package": (
                    f"07-release/ontology-engineering-package-{release_version}"
                ),
                "source_package_manifest_sha256": source_manifest_sha256,
                "source_release_snapshot_sha256": source_snapshot_sha256,
                "source_cq_contract_version": cq_lineage["contract_version"],
                "note": (
                    "该目录完全复制自已校验的不可变发布包；原始发布包和完整审计底稿均未改写。"
                ),
            }
            self._write_json(release_info_dir / "publication.json", publication)
            self._write_json(release_info_dir / "audit-reference.json", audit_reference)
            self._atomic_write(
                export_dir / "发布说明.md",
                (
                    f"# 本体工程精简交付副本 {release_version}\n\n"
                    f"- 项目：{state['project_name']} (`{project_id}`)\n"
                    f"- 原批准人：{publication['approved_by']}\n"
                    f"- 导出人：{exported_by.strip()}\n"
                    f"- 导出时间：{publication['exported_at']}\n\n"
                    "本副本只包含本体模型、工程定义、质量结论和发布信息。"
                    "真实实例、OCR 原文、阶段技术报告及 MCP 明细继续保留在原工程中。\n"
                ),
            )
            self._atomic_write(
                export_dir / "打开查看.html",
                self._render_release_summary(state, publication),
            )
            export_files = [
                {
                    "path": path.relative_to(export_dir).as_posix(),
                    "sha256": _file_checksum(path),
                }
                for path in sorted(export_dir.rglob("*"))
                if path.is_file() and path.name != "manifest.json"
            ]
            manifest = {**publication, "file_count": len(export_files), "files": export_files}
            self._write_json(export_dir / "manifest.json", manifest)
            self._append_event(
                project_dir,
                "MODEL_DELIVERY_PACKAGE_EXPORTED",
                state,
                {
                    "stage": "S7",
                    "release_version": release_version,
                    "exported_by": exported_by.strip(),
                    "package_path": export_dir.relative_to(project_dir).as_posix(),
                    "file_count": len(export_files),
                    "manifest_sha256": _file_checksum(export_dir / "manifest.json"),
                    "actor": exported_by.strip(),
                },
            )
            self._refresh_manifest(project_dir)
            return {
                "project_id": project_id,
                "release_version": release_version,
                "package_path": export_dir.relative_to(project_dir).as_posix(),
                "file_count": len(export_files),
                "manifest_sha256": _file_checksum(export_dir / "manifest.json"),
            }

    def _start_s4_if_ready(self, project_dir: Path, state: dict[str, Any]) -> None:
        if (
            state.get("project_status") == "S1_S3_READY"
            and state.get("current_stage") is None
            and state["stage_statuses"].get("S4") in {"PENDING", "INVALIDATED"}
        ):
            self._ensure_legacy_stage_reports(project_dir)
            for stage_dir in (
                "04-ontology-design",
                "05-ontology-build",
                "06-quality-validation",
                "07-release",
            ):
                (project_dir / stage_dir).mkdir(parents=True, exist_ok=True)
            state["current_stage"] = "S4"
            state["workflow_version"] = WORKFLOW_VERSION
            state["project_status"] = "IN_PROGRESS"
            state["stage_statuses"]["S4"] = "RUNNING"
            state["resume_point"] = "S4: 形成 ontology-design.yaml 本体施工图"
            self._save_state(project_dir, state)
            self._append_event(project_dir, "STAGE_STARTED", state, {"stage": "S4"})

    def _ensure_legacy_stage_reports(self, project_dir: Path) -> None:
        s1_dir = project_dir / "01-data-understanding"
        s1_report = s1_dir / "data-understanding-report.html"
        if (
            not s1_report.exists()
            or REPORT_RENDERER_MARKER not in s1_report.read_text(encoding="utf-8")[:160]
        ):
            s1_payload = {
                "datasource_inventory": self._read_json(s1_dir / "datasource-inventory.json"),
                "schema_snapshot": self._read_json(s1_dir / "schema-snapshot.json"),
                "data_profile": self._read_json(s1_dir / "data-profile.json"),
                "relation_candidates": self._read_json(s1_dir / "relation-candidates.json"),
                "evidence_sql": self._read_json(s1_dir / "evidence-sql.json"),
            }
            self._atomic_write(
                s1_report,
                render_s1_report(
                    s1_dir,
                    self._read_json(project_dir / "project.json"),
                    s1_payload,
                ),
            )

        s2_dir = project_dir / "02-semantic-recognition"
        s2_report = s2_dir / "business-semantics-report.html"
        if (
            not s2_report.exists()
            or REPORT_RENDERER_MARKER not in s2_report.read_text(encoding="utf-8")[:160]
        ):
            candidate_doc = yaml.safe_load(
                (s2_dir / "ontology-candidates.yaml").read_text(encoding="utf-8")
            )
            s2_payload = {
                "ontology_candidates": candidate_doc.get("candidates") or [],
                "business_rule_candidates": self._read_json(
                    s2_dir / "business-rule-candidates.json"
                ),
            }
            self._atomic_write(
                s2_report,
                render_s2_report(
                    s2_dir,
                    self._read_json(project_dir / "project.json"),
                    s2_payload,
                ),
            )

        s3_dir = project_dir / "03-mapping-review"
        s3_report = s3_dir / "mapping-review-report.html"
        mapping_path = s3_dir / "mapping.yaml"
        if mapping_path.exists() and (
            not s3_report.exists()
            or REPORT_RENDERER_MARKER not in s3_report.read_text(encoding="utf-8")[:160]
        ):
            pending_path = s3_dir / "pending-confirmations.json"
            automatic_path = s3_dir / "automatic-decisions.json"
            gate_path = s3_dir / "gate-results.json"
            self._atomic_write(
                s3_report,
                render_s3_report(
                    s3_dir,
                    self._read_json(project_dir / "project.json"),
                    yaml.safe_load(mapping_path.read_text(encoding="utf-8")),
                    self._read_json(pending_path) if pending_path.exists() else [],
                    self._read_json(automatic_path) if automatic_path.exists() else [],
                    self._read_json(gate_path),
                ),
            )

    @staticmethod
    def _validate_competency_questions(
        questions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(questions, list) or not questions:
            raise WorkflowGateError("G-S4-CQ", "至少需要 1 个业务问题。")
        normalized: list[dict[str, Any]] = []
        question_ids: set[str] = set()
        for index, question in enumerate(questions, start=1):
            question_id = str(question.get("id") or f"CQ-{index:03d}").strip()
            question_text = str(question.get("question") or "").strip()
            sparql = str(question.get("sparql") or "").strip()
            expected = str(question.get("expected") or "").strip()
            if not question_id or question_id in question_ids:
                raise WorkflowGateError("G-S4-CQ", f"第 {index} 个业务问题编号缺失或重复。")
            if not question_text or not sparql or not expected:
                raise WorkflowGateError(
                    "G-S4-CQ",
                    f"业务问题 {question_id} 必须包含问题、查询语句和预期结果。",
                )
            if _sparql_query_type(sparql) is None:
                raise WorkflowGateError(
                    "G-S4-CQ",
                    f"业务问题 {question_id} 的查询必须是只读 SPARQL。",
                )
            question_ids.add(question_id)
            normalized_question = {
                "id": question_id,
                "question": question_text,
                "sparql": sparql,
                "expected": expected,
            }
            coverage_status = str(question.get("coverage_status") or "NEEDS_REVIEW").strip().upper()
            if coverage_status not in {"DIRECT", "NEEDS_REVIEW"}:
                raise WorkflowGateError(
                    "G-S4-CQ",
                    f"业务问题 {question_id} 的 coverage_status 不合法。",
                )
            normalized_question["coverage_status"] = coverage_status
            for field in (
                "source",
                "source_question_id",
                "source_question_sha256",
                "priority",
                "example_entities",
                "generation_note",
            ):
                value = question.get(field)
                if value not in (None, ""):
                    normalized_question[field] = value
            normalized_question["answer_contract"] = OntologyWorkflowService._cq_answer_contract(
                {**normalized_question, "answer_contract": question.get("answer_contract")}
            )
            normalized.append(normalized_question)
        return normalized

    def _validate_competency_question_lineage(
        self,
        project_dir: Path,
        questions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        intake_path = project_dir / "00-document-evidence/cq-intake.json"
        intake = self._read_json(intake_path) if intake_path.exists() else {"questions": []}
        intake_questions = intake.get("questions") or []
        expected_sources = {
            str(item.get("id") or "").strip(): self._cq_source_question_sha256(item)
            for item in intake_questions
            if str(item.get("id") or "").strip()
        }
        observed_sources: dict[str, str] = {}
        for question in questions:
            source_id = str(question.get("source_question_id") or "").strip()
            if not source_id:
                continue
            source_sha256 = str(question.get("source_question_sha256") or "").strip()
            if source_id in observed_sources and observed_sources[source_id] != source_sha256:
                raise WorkflowGateError(
                    "G-S4-CQ-LINEAGE",
                    f"S0 业务问题 {source_id} 被多个不一致的 S4 问题引用。",
                )
            observed_sources[source_id] = source_sha256
        missing = set(expected_sources) - set(observed_sources)
        mismatched = {
            source_id
            for source_id, expected_sha256 in expected_sources.items()
            if observed_sources.get(source_id) not in (None, expected_sha256)
        }
        unknown = set(observed_sources) - set(expected_sources)
        if missing:
            raise WorkflowGateError(
                "G-S4-CQ-LINEAGE",
                "S0 人工业务问题不能在 S4 静默删除：" + ", ".join(sorted(missing)),
            )
        if mismatched:
            raise WorkflowGateError(
                "G-S4-CQ-LINEAGE",
                "S4 业务问题来源摘要与 S0 不一致：" + ", ".join(sorted(mismatched)),
            )
        if unknown:
            raise WorkflowGateError(
                "G-S4-CQ-LINEAGE",
                "S4 引用了 S0 不存在的业务问题：" + ", ".join(sorted(unknown)),
            )
        return {
            "intake_question_count": len(expected_sources),
            "linked_question_count": len(observed_sources),
            "intake_questions_sha256": str(
                intake.get("questions_sha256") or _fingerprint(intake_questions)
            ),
            "approved_questions_sha256": _fingerprint(questions),
        }

    def _verify_competency_question_release_lineage(
        self,
        project_dir: Path,
    ) -> dict[str, Any]:
        design_path = project_dir / "04-ontology-design/ontology-design.yaml"
        review_path = project_dir / "04-ontology-design/competency-question-review.json"
        execution_path = project_dir / "06-quality-validation/competency-question-report.json"
        if not design_path.exists() or not review_path.exists() or not execution_path.exists():
            raise WorkflowGateError(
                "G-S7-CQ-LINEAGE",
                "发布前缺少 S4 CQ 设计/批准记录或 S6 服务端执行报告。",
            )
        design = yaml.safe_load(design_path.read_text(encoding="utf-8"))
        questions = self._validate_competency_questions(design.get("competency_questions") or [])
        lineage = self._validate_competency_question_lineage(project_dir, questions)
        questions_sha256 = _fingerprint(questions)
        review = self._read_json(review_path)
        execution = self._read_json(execution_path)
        contract_versions = {
            str((question.get("answer_contract") or {}).get("contract_version") or "")
            for question in questions
        }
        if contract_versions != {"cq-answer-v2"}:
            raise WorkflowGateError(
                "G-S7-CQ-LINEAGE",
                "S7 只接受 cq-answer-v2 业务语义答案契约；请从 S4 重新冻结并执行 S6。",
            )
        if review.get("status") != "APPROVED":
            raise WorkflowGateError("G-S7-CQ-LINEAGE", "S4 CQ 尚未正式批准。")
        if str(review.get("approved_questions_sha256") or "") != questions_sha256:
            raise WorkflowGateError(
                "G-S7-CQ-LINEAGE",
                "S4 批准的 CQ 摘要与正式 ontology-design.yaml 不一致。",
            )
        if (
            int(execution.get("schema_version", 0)) < 3
            or execution.get("status") != "PASSED"
            or execution.get("validation_mode")
            not in {
                "SERVER_EXECUTED_SEMANTIC_ANSWER_CONTRACT",
                "SERVER_EXECUTED_BASE_RELEASE_FULL_SOURCE_ONTOP",
            }
            or str(execution.get("questions_sha256") or "") != questions_sha256
            or str(execution.get("intake_questions_sha256") or "")
            != lineage["intake_questions_sha256"]
            or int(execution.get("total", -1)) != len(questions)
            or int(execution.get("passed", -1)) != len(questions)
        ):
            raise WorkflowGateError(
                "G-S7-CQ-LINEAGE",
                "S6 CQ 报告未绑定 S0 输入、S4 批准版本和服务端答案契约执行结果。",
            )
        return {
            "status": "VERIFIED",
            "contract_version": "cq-answer-v2",
            "intake_questions_sha256": lineage["intake_questions_sha256"],
            "approved_questions_sha256": questions_sha256,
            "execution_questions_sha256": execution["questions_sha256"],
            "execution_results_sha256": execution.get("results_sha256"),
            "execution_report_sha256": _file_checksum(execution_path),
            "question_count": len(questions),
        }

    _normalize_ontology_design_localization = staticmethod(normalize_ontology_design_localization)

    def _reviewed_cq_runtime_bindings(
        self, project_dir: Path, runtime: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """One authoritative S3 binding catalog for compilation and gate read-back."""
        root = (project_dir / "03-mapping-review/runtime").resolve()
        bindings: dict[str, dict[str, Any]] = {}

        def artifact(path: Any) -> Path:
            target = (root / str(path or "")).resolve()
            if root not in target.parents or not target.is_file():
                raise WorkflowGateError("G-S4-CQ-BINDING", "CQ 的 S3 来源文件无法安全回读。")
            return target

        def checked_source(item: dict[str, Any], path_key: str, hash_key: str) -> dict[str, Any]:
            result = dict(item)
            if item.get(path_key):
                actual = _file_checksum(artifact(item[path_key]))
                if item.get(hash_key) and item[hash_key] != actual:
                    raise WorkflowGateError("G-S4-CQ-BINDING", "CQ 的 S3 规则或事实产物校验值发生变化。")
                result["actual_artifact_sha256"] = actual
            return result

        for kind in ("query_capabilities", "reasoning_capabilities", "document_fact_queries"):
            for name, cap in (runtime.get(kind) or {}).items():
                question_ids = list(dict.fromkeys([
                    *(cap.get("business_question_ids") or []),
                    *(cap.get("cq_bindings") or {}),
                ]))
                for qid in question_ids:
                    qid = str(qid).strip()
                    if not qid:
                        continue
                    if qid in bindings:
                        raise WorkflowGateError("G-S4-CQ-BINDING", f"业务问题 {qid} 被多个运行时能力绑定，必须先在 S3 消除歧义。")
                    if kind == "query_capabilities":
                        query_artifact = (runtime.get("ontop_queries") or {}).get(name) or {}
                        query = artifact(query_artifact.get("path")).read_text(encoding="utf-8")
                        binding = {"query_name": name, "sparql": query, "capability": cap}
                    elif kind == "document_fact_queries":
                        if not cap.get("sparql") or qid not in (cap.get("cq_bindings") or {}):
                            raise WorkflowGateError("G-S4-CQ-BINDING", f"文档事实查询 {name} 缺少正式 SELECT/CQ 绑定。")
                        source = checked_source(cap, "fact_artifact", "fact_sha256")
                        binding = {
                            "query_name": name, "sparql": cap["sparql"], "capability": cap,
                            "document_source_sha256": _fingerprint(source),
                        }
                    else:
                        cq = (cap.get("cq_bindings") or {}).get(qid)
                        if not cq or not cq.get("cq_sparql"):
                            raise WorkflowGateError("G-S4-CQ-BINDING", f"推理能力 {name} 的业务问题 {qid} 缺少已审 CQ 查询。")
                        evidence_name = cap.get("evidence_query")
                        evidence = (runtime.get("document_fact_queries") or {}).get(evidence_name)
                        if evidence is not None:
                            evidence_source = checked_source(evidence, "fact_artifact", "fact_sha256")
                        else:
                            query_artifact = (runtime.get("ontop_queries") or {}).get(evidence_name) or {}
                            evidence_source = {
                                "query": artifact(query_artifact.get("path")).read_text(encoding="utf-8"),
                                "capability": (runtime.get("query_capabilities") or {}).get(evidence_name),
                            }
                        source = {
                            "capability": checked_source(cap, "rule_artifact", "rule_sha256"),
                            "evidence": evidence_source,
                            "runtime_mode": runtime.get("runtime_mode"),
                            "reasoning_requirement": runtime.get("reasoning_requirement"),
                        }
                        binding = {
                            "query_name": name, "sparql": cq["cq_sparql"], "capability": cap,
                            "reasoning_source_sha256": _fingerprint(source),
                        }
                    bindings[qid] = binding
        return bindings

    def _verify_reviewed_cq_binding(
        self, project_dir: Path, question: dict[str, Any], runtime: dict[str, Any]
    ) -> bool:
        """Recompile from S3; caller-authored flags never authorize fact scope."""
        question_id = str(question.get("source_question_id") or "")
        contract = question["answer_contract"]
        binding = self._reviewed_cq_runtime_bindings(project_dir, runtime).get(question_id)
        if not binding or question_id not in (binding["capability"].get("cq_bindings") or {}):
            if not contract.get("reviewed_runtime_binding"):
                return False
            raise WorkflowGateError("G-S4-CQ-BINDING", "CQ 缺少唯一的 S3 已审回答绑定。")
        try:
            compiled = compile_reviewed_cq(
                question_id=question_id, query_name=binding["query_name"],
                query=binding["sparql"], capability=binding["capability"],
            )
            assert compiled is not None
            if binding.get("reasoning_source_sha256"):
                compiled["answer_contract"]["reviewed_runtime_binding"][
                    "reasoning_source_sha256"
                ] = binding["reasoning_source_sha256"]
            if binding.get("document_source_sha256"):
                compiled["answer_contract"]["reviewed_runtime_binding"][
                    "document_source_sha256"
                ] = binding["document_source_sha256"]
            expected = self._cq_answer_contract({**question, **compiled})
        except (CQBindingError, ValueError) as exc:
            raise WorkflowGateError("G-S4-CQ-BINDING", str(exc)) from exc
        if question["sparql"].strip() != compiled["sparql"].strip() or contract != expected:
            raise WorkflowGateError(
                "G-S4-CQ-BINDING", "CQ 查询或答案契约偏离 S3 已审用例；请在 S3 更新来源绑定后重新编译。"
            )
        return True

    @staticmethod
    def _cq_dimension_contract_issues(
        questions: list[dict[str, Any]], *, entity_iris: set[str],
        property_iris: set[str], object_property_iris: set[str],
        known_evidence_refs: set[str], index_offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Collect the same dimension failures used by formal S4 validation."""
        issues: list[dict[str, Any]] = []
        for index, question in enumerate(questions, start=index_offset):
            contract = question.get("answer_contract") or {}
            declared = list(contract.get("required_business_dimensions") or [])
            base = f"competency_questions[{index}].answer_contract"
            question_id = question["id"]

            def add(
                message: str, suffix: str, code: str, *, base: str = base,
                question_id: str = question_id,
                source_question_id: str | None = question.get("source_question_id"),
            ) -> None:
                issues.append({
                    **OntologyWorkflowService._preflight_issue(
                        "G-S4-CQ-CONTRACT-COVERAGE", message, path=f"{base}.{suffix}",
                    ),
                    "question_id": question_id,
                    "source_question_id": source_question_id,
                    "reason_code": code,
                })

            required_names = {item["dimension"] for item in _required_business_dimensions(
                str(question.get("question") or ""), str(question.get("expected") or ""),
            )}
            declared_names = {str(item.get("dimension") or "") for item in declared}
            if not required_names.issubset(declared_names):
                add(f"能力问题 {question_id} 的 S0 业务输出维度未完整冻结："
                    f"至少需要 {sorted(required_names)}，实际 {sorted(declared_names)}。",
                    "required_business_dimensions", "MISSING_BUSINESS_DIMENSIONS")
            sparql = str(question.get("sparql") or "")
            relationship = _cq_has_relationship_outputs(
                str(question.get("question") or ""), str(question.get("expected") or ""),
                sparql, object_property_iris,
            )
            if relationship and len(declared) < 2:
                add(f"能力问题 {question_id} 是关系查询，必须显式冻结至少两个业务输出维度。",
                    "required_business_dimensions", "INSUFFICIENT_RELATIONSHIP_DIMENSIONS")
            for dimension_index, dimension in enumerate(declared):
                prefix = f"required_business_dimensions[{dimension_index}]"
                label = f"能力问题 {question_id} 的维度 {dimension.get('dimension')}"
                if not set(dimension.get("evidence_refs") or []).issubset(known_evidence_refs):
                    add(f"{label} 引用了未知证据。", f"{prefix}.evidence_refs", "UNKNOWN_EVIDENCE_REFS")
                if dimension.get("applicability") == "NOT_APPLICABLE":
                    continue
                if str(dimension.get("ontology_term") or "") not in entity_iris:
                    add(f"{label} 未绑定正式本体术语。", f"{prefix}.ontology_term", "UNKNOWN_ONTOLOGY_TERM")
                path = str(dimension.get("path") or "")
                if relationship and dimension.get("dimension") != "subject" and (
                    not path or path not in property_iris or not _sparql_references_iri(sparql, path)
                ):
                    add(f"{label} 的 path 必须是已声明且在该 CQ SPARQL 中引用的单个属性完整 IRI（对象或数据属性）；不接受局部名、类名、点号链或 / 属性链。多跳关系在查询中表达，本字段引用支撑该维度的实际属性。", f"{prefix}.path", "MISSING_OR_UNQUERIED_PATH")
            if relationship:
                assertions = [*(contract.get("result_assertions") or []),
                              *(contract.get("boundary_assertions") or [])]
                if any(str(item.get("operator") or "").upper() == "NE"
                       and item.get("expected") == "" for item in assertions):
                    add(f"能力问题 {question_id} 不能用 NE ''/ANY 等弱断言代替具体业务语义验证。",
                        "result_assertions", "WEAK_DIMENSION_ASSERTION")
                asserted = {str(item.get("binding") or "") for item in assertions}
                if "expected_rows" in contract:
                    asserted.update(contract["expected_row_fields"])
                missing = sorted(str(item.get("binding") or "") for item in declared
                                 if item.get("applicability") == "REQUIRED"
                                 and str(item.get("binding") or "") not in asserted)
                if missing:
                    add(f"能力问题 {question_id} 的业务维度缺少具体值/集合断言：" + ", ".join(missing),
                        "required_business_dimensions", "MISSING_DIMENSION_ASSERTION")
        return issues

    def _collect_s4_cq_contract_issues(
        self, project_dir: Path, design: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Read-only diagnostics after generation, before the formal S4 gate."""
        mapping = yaml.safe_load((project_dir / "03-mapping-review/mapping.yaml").read_text()) or {}
        mappings = [item for item in mapping.get("mappings") or [] if isinstance(item, dict)]
        known_refs = {str(item.get("id") or "") for item in mappings}
        known_refs.update(str(ref) for item in mappings for ref in item.get("source_refs") or [])
        iris = {
            key: {str(item.get("iri") or "") for item in design.get(key) or []
                  if isinstance(item, dict)}
            for key in ("classes", "object_properties", "data_properties")
        }
        issues: list[dict[str, Any]] = cq_reach.project_unreachable_cq_class_issues(
            project_dir, list(design.get("competency_questions") or []))
        for index, question in enumerate(design.get("competency_questions") or []):
            try:
                normalized = self._validate_competency_questions([question])
            except WorkflowGateError as exc:
                issues.append({
                    **self._preflight_issue(exc.gate_id, str(exc),
                                           path=f"competency_questions[{index}].answer_contract"),
                    "question_id": question.get("id"),
                    "source_question_id": question.get("source_question_id"),
                    "reason_code": "INVALID_CQ_CONTRACT",
                })
                continue
            issues.extend(self._cq_dimension_contract_issues(
                normalized, entity_iris=set().union(*iris.values()),
                property_iris=iris["object_properties"] | iris["data_properties"],
                object_property_iris=iris["object_properties"],
                known_evidence_refs=known_refs, index_offset=index,
            ))
        return issues

    def _logical_axiom_applicability(
        self, project_dir: Path, design: dict[str, Any],
        *, verified_question_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        if design.get("logical_axioms"):
            return logical_axiom_applicability(
                state={}, design=design, intake={}, capability_plan={}, runtime={}, rules=None,
                verified_question_ids=set(), source_sha256={}, missing_sources=[],
            )
        paths = {
            "intake": "00-document-evidence/cq-intake.json",
            "capability_plan": "02-semantic-recognition/capability-plan.json",
            "runtime": "03-mapping-review/runtime/runtime-source.json",
            "rules": "02-semantic-recognition/business-rule-candidates.json",
        }
        values: dict[str, Any] = {}
        checksums: dict[str, str] = {}
        missing: list[str] = []
        for key, relative in paths.items():
            path = project_dir / relative
            if path.is_file():
                values[key] = self._read_json(path)
                checksums[relative] = _file_checksum(path)
            else:
                values[key] = None if key == "rules" else {}
                missing.append(relative)
        if verified_question_ids is None:
            verified_question_ids = set()
            for question in self._validate_competency_questions(design.get("competency_questions") or []):
                if isinstance(values["runtime"], dict) and self._verify_reviewed_cq_binding(project_dir, question, values["runtime"]):
                    verified_question_ids.add(str(question.get("source_question_id") or ""))
        runtime = values.get("runtime")
        if isinstance(runtime, dict):
            runtime_dir = project_dir / "03-mapping-review/runtime"
            capabilities = runtime.get("reasoning_capabilities") or {}
            if isinstance(capabilities, dict):
                for name, capability in capabilities.items():
                    if not isinstance(capability, dict):
                        missing.append(f"reasoning_capabilities.{name}")
                        continue
                    relative = str(capability.get("rule_artifact") or "")
                    rule_path = (runtime_dir / relative).resolve()
                    rule_ref = f"03-mapping-review/runtime/{relative}"
                    if (
                        not relative
                        or not rule_path.is_relative_to(runtime_dir.resolve())
                        or not rule_path.is_file()
                    ):
                        missing.append(rule_ref)
                        continue
                    checksum = _file_checksum(rule_path)
                    if checksum != capability.get("rule_sha256"):
                        missing.append(f"{rule_ref} 哈希与 S3 正式索引不一致")
                        continue
                    try:
                        rule_package = self._read_json(rule_path)
                    except (OSError, ValueError):
                        missing.append(f"{rule_ref} 无法解析")
                        continue
                    if (
                        not isinstance(rule_package, dict)
                        or rule_package.get("capability_name") != name
                        or not isinstance(rule_package.get("rules"), list)
                    ):
                        missing.append(f"{rule_ref} 能力身份或规则数组无效")
                        continue
                    capability["rules"] = rule_package["rules"]
                    checksums[rule_ref] = checksum
        state_path = project_dir / "workflow-state.json"
        state = self._read_state(project_dir) if state_path.is_file() else {}
        if not state_path.is_file():
            missing.append("workflow-state.json")
        return logical_axiom_applicability(
            state=state, design=design, **values,
            verified_question_ids=verified_question_ids,
            source_sha256=checksums, missing_sources=missing,
        )

    def _validate_s4(
        self,
        project_dir: Path,
        ontology_design: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_unchanged_build_for_reuse(project_dir, ontology_design)
        if (project_dir / "workflow-state.json").is_file() and business_contract.enabled(self._read_state(project_dir)):
            try:
                business_contract.validate_design(ontology_design.get("classes") or [], (yaml.safe_load((project_dir / "03-mapping-review/mapping.yaml").read_text(encoding="utf-8")) or {}).get("mappings") or [])
            except ValueError as exc:
                raise WorkflowGateError("G-S4-INSTANCE-CONTRACT", str(exc)) from exc
        for field in ("ontology_iri", "version"):
            if not str(ontology_design.get(field) or "").strip():
                raise WorkflowGateError("G-S4-REQUIRED", f"ontology_design 缺少 {field}。")
        if not re.match(r"^https?://", str(ontology_design["ontology_iri"])):
            raise WorkflowGateError("G-S4-REQUIRED", "ontology_iri 必须是 HTTP(S) IRI。")
        for field, label in (("title_zh", "本体中文标题"), ("comment_zh", "本体中文注释")):
            value = str(ontology_design.get(field) or "").strip()
            if not value or not _contains_chinese(value):
                raise WorkflowGateError("G-S4-CHINESE", f"{label}不能为空且必须包含中文。")

        collections = {
            "classes": ontology_design.get("classes"),
            "object_properties": ontology_design.get("object_properties"),
            "data_properties": ontology_design.get("data_properties"),
        }
        if not isinstance(collections["classes"], list) or not collections["classes"]:
            raise WorkflowGateError("G-S4-REQUIRED", "ontology_design.classes 不能为空。")
        if not isinstance(collections["object_properties"], list):
            raise WorkflowGateError("G-S4-REQUIRED", "object_properties 必须是数组。")
        if not isinstance(collections["data_properties"], list):
            raise WorkflowGateError("G-S4-REQUIRED", "data_properties 必须是数组。")

        seen_iris: set[str] = set()
        covered_mapping_ids: set[str] = set()
        covered_semantic_ids: set[str] = set()
        for collection_name, entities in collections.items():
            for index, entity in enumerate(entities, start=1):
                name = str(entity.get("name") or "").strip()
                iri = str(entity.get("iri") or "").strip()
                mapping_ids = entity.get("source_mapping_ids") or []
                semantic_ids = entity.get("source_semantic_ids") or []
                if not name or not iri or not (mapping_ids or semantic_ids):
                    raise WorkflowGateError(
                        "G-S4-REQUIRED",
                        f"{collection_name} 第 {index} 项缺少 name、iri 或来源追溯字段。",
                    )
                label_zh = str(entity.get("label_zh") or "").strip()
                comment_zh = str(entity.get("comment_zh") or "").strip()
                if not _contains_chinese(label_zh):
                    raise WorkflowGateError(
                        "G-S4-CHINESE",
                        f"{collection_name} 中的 {name} 缺少有业务含义的中文 label_zh。",
                    )
                if not _contains_chinese(comment_zh):
                    raise WorkflowGateError(
                        "G-S4-CHINESE",
                        f"{collection_name} 中的 {name} 缺少中文 comment_zh。",
                    )
                if iri in seen_iris:
                    raise WorkflowGateError("G-S4-UNIQUE-IRI", f"设计 IRI 重复：{iri}")
                seen_iris.add(iri)
                covered_mapping_ids.update(str(item) for item in mapping_ids)
                covered_semantic_ids.update(str(item) for item in semantic_ids)
                if collection_name == "object_properties" and (
                    not entity.get("domain") or not entity.get("range")
                ):
                    raise WorkflowGateError(
                        "G-S4-REQUIRED",
                        f"对象属性 {name} 必须定义 domain 和 range。",
                    )
                if collection_name == "data_properties" and (
                    not entity.get("domain") or not entity.get("range")
                ):
                    raise WorkflowGateError(
                        "G-S4-REQUIRED",
                        f"数据属性 {name} 必须定义 domain 和 range。",
                    )

        mapping = yaml.safe_load(
            (project_dir / "03-mapping-review/mapping.yaml").read_text(encoding="utf-8")
        )
        mapping_items = [item for item in mapping.get("mappings") or [] if isinstance(item, dict)]
        mapping_ids = {str(item.get("id")) for item in mapping_items}
        mapping_by_id = {str(item.get("id")): item for item in mapping_items}
        unknown = covered_mapping_ids - mapping_ids
        missing = mapping_ids - covered_mapping_ids
        if unknown:
            raise WorkflowGateError(
                "G-S4-MAPPING-COVERAGE",
                f"设计引用了不存在的 Mapping：{', '.join(sorted(unknown))}",
            )
        if missing:
            raise WorkflowGateError(
                "G-S4-MAPPING-COVERAGE",
                f"仍有 {len(missing)} 条 Mapping 未进入本体施工图。",
            )

        semantic_path = project_dir / "02-semantic-recognition/ontology-candidates.yaml"
        semantic_document = (
            yaml.safe_load(semantic_path.read_text(encoding="utf-8"))
            if semantic_path.is_file()
            else {}
        ) or {}
        semantic_ids = {
            str(item.get("id") or "")
            for item in semantic_document.get("candidates") or []
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        unknown_semantic_ids = covered_semantic_ids - semantic_ids
        if unknown_semantic_ids:
            raise WorkflowGateError(
                "G-S4-SEMANTIC-PROVENANCE",
                "设计引用了不存在的 S2 语义候选：" + ", ".join(sorted(unknown_semantic_ids)),
            )

        questions = self._validate_competency_questions(
            ontology_design.get("competency_questions") or []
        )
        declared_object_property_iris = {
            str(item.get("iri") or "") for item in collections["object_properties"]
        }
        relationship_questions = _relationship_competency_questions(
            questions, collections["object_properties"],
        )
        taxonomy_only = str(ontology_design.get("modeling_profile") or "").upper() == (
            "TAXONOMY_ONLY"
        )
        if relationship_questions and not collections["object_properties"]:
            raise WorkflowGateError(
                "G-S4-RELATION-COVERAGE",
                "S0/CQ 存在主体-义务、行为-责任、监督或期限例外等关系型诉求，"
                "object_properties 不能为空；本项目不符合 TAXONOMY_ONLY 豁免。",
            )
        if not collections["object_properties"] and not taxonomy_only:
            raise WorkflowGateError(
                "G-S4-RELATION-COVERAGE",
                "零对象属性设计必须显式声明 modeling_profile=TAXONOMY_ONLY 并由纯分类 CQ 证明。",
            )
        if taxonomy_only and relationship_questions:
            raise WorkflowGateError(
                "G-S4-RELATION-COVERAGE",
                "关系型 CQ 不能声明 TAXONOMY_ONLY。",
            )

        class_names = {str(item.get("name") or "") for item in collections["classes"]}
        class_iris = {str(item.get("iri") or "") for item in collections["classes"]}
        property_iris = {
            str(item.get("iri") or "")
            for item in [
                *collections["object_properties"],
                *collections["data_properties"],
            ]
        }
        known_evidence_refs = set(mapping_ids)
        for mapping_item in mapping_items:
            known_evidence_refs.update(
                str(value) for value in mapping_item.get("source_refs") or []
            )
        for prop in collections["object_properties"]:
            if (
                str(prop.get("domain") or "") not in class_names | class_iris
                or str(prop.get("range") or "") not in class_names | class_iris
            ):
                raise WorkflowGateError(
                    "G-S4-RELATION-COVERAGE",
                    f"对象属性 {prop.get('name')} 的 domain/range 未绑定已声明业务类。",
                )
            prop_mappings = [
                mapping_by_id.get(str(value)) for value in prop.get("source_mapping_ids") or []
            ]
            if not prop_mappings or any(
                not item or not item.get("source_refs") for item in prop_mappings
            ):
                raise WorkflowGateError(
                    "G-S4-RELATION-COVERAGE",
                    f"对象属性 {prop.get('name')} 缺少可回读的 Mapping 证据。",
                )

        dimension_issues = self._cq_dimension_contract_issues(
            questions, entity_iris=seen_iris, property_iris=property_iris,
            object_property_iris=declared_object_property_iris,
            known_evidence_refs=known_evidence_refs,
        )
        if dimension_issues:
            first = dimension_issues[0]
            raise WorkflowGateError(first["gate"], first["message"])
        cq_lineage = self._validate_competency_question_lineage(project_dir, questions)
        logical_axioms = self._validate_logical_axioms(
            ontology_design.get("logical_axioms") or [],
            entity_iris=seen_iris,
        )

        runtime_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
        runtime = self._read_json(runtime_path) if runtime_path.is_file() else {}
        reasoning_requirement = str(runtime.get("reasoning_requirement") or "").upper()
        capabilities = runtime.get("reasoning_capabilities") or {}
        term_kinds = {str(item["iri"]): kind for field, kind in (
            ("classes", "CLASS"), ("object_properties", "OBJECT_PROPERTY"),
            ("data_properties", "DATA_PROPERTY"),
        ) for item in ontology_design.get(field) or []}
        binding_errors = []
        for name, capability in capabilities.items():
            try:
                validate_materialization_bindings(capability, term_kinds, stage="S4")
            except RuntimeError as exc:
                binding_errors.append(f"推理能力 {name}：{exc}")
        try:
            binding_errors.extend(rule_conclusion_type_issues(
                project_dir / "03-mapping-review/runtime", capabilities, term_kinds,
            ))
        except ValueError as exc:
            raise WorkflowGateError("G-S4-FACT-ARITY", str(exc)) from exc
        if binding_errors:
            raise WorkflowGateError("G-S4-FACT-ARITY", "；".join(binding_errors))
        verified_fact_question_ids: set[str] = set()
        for name, document_query in (runtime.get("document_fact_queries") or {}).items():
            if document_query.get("cq_bindings"):
                missing = set((document_query.get("ontology_terms") or {}).values()) - seen_iris
                if missing:
                    raise WorkflowGateError("G-S4-CQ-BINDING", f"文档事实查询 {name} 的术语未进入正式本体：{sorted(missing)}")
        for question in questions:
            contract = question["answer_contract"]
            question_id = str(question["id"])
            reviewed_binding = self._verify_reviewed_cq_binding(project_dir, question, runtime)
            if reviewed_binding:
                verified_fact_question_ids.add(str(question.get("source_question_id") or ""))
            if contract.get("nullable_bindings") and not reviewed_binding:
                raise WorkflowGateError("G-S4-CQ-BINDING", "条件空值必须来自 S3 已审绑定，不能在 S4 临时放宽。")
            if reviewed_binding:
                rule_path = project_dir / "02-semantic-recognition/business-rule-candidates.json"
                rules = self._read_json(rule_path) if rule_path.is_file() else []
                if isinstance(rules, dict):
                    rules = rules.get("rules") or []
                from services.ontology_engineering.business_source_contract import (
                    business_evidence_refs,
                )

                known_refs = business_evidence_refs(mapping_items, rules)
                if not set(contract.get("source_refs") or []).issubset(known_refs):
                    raise WorkflowGateError("G-S4-CQ-BINDING", "CQ 回答范围引用了未知来源证据。")
                if any(
                    not set(item["source_refs"]).issubset(known_refs)
                    for item in (contract.get("nullable_bindings") or {}).values()
                ):
                    raise WorkflowGateError("G-S4-CQ-BINDING", "条件空值引用了未知来源证据。")
            if str(contract.get("query_type")) == "SELECT" and "expected_rows" not in contract and (
                not contract.get("result_assertions") or not contract.get("boundary_assertions")
            ):
                raise WorkflowGateError(
                    "G-S4-CQ-PRODUCTION",
                    f"能力问题 {question_id} 必须冻结完整预期集合，或同时冻结实际结果断言与边界断言，不能只检查返回变量或非空。",
                )
            is_reasoning_question = _requires_reasoning(
                str(question.get("question") or ""),
                str(question.get("expected") or ""),
            )
            answer_mode = str(contract.get("answer_mode") or "")
            if reviewed_binding and answer_mode in {"FACT_QUERY", "EVIDENCE_QUERY"}:
                # An evidence-bound lookup/aggregation is not rule execution.
                # Explicit requests to reason still cannot be downgraded.
                explicit_inference = re.search(
                    r"推理|推断|推导|推出|合规判定|责任判定|处置判定|执行[^。；]*规则",
                    f"{question.get('question', '')}\n{question.get('expected', '')}",
                )
                classified = self._classify_capability_question(
                    str(question.get("question") or ""), str(question.get("expected") or "")
                )
                is_reasoning_question = bool(explicit_inference) or classified in {
                    "CLOSED_WORLD_INFERENCE", "OWL_INFERENCE",
                }
            is_reasoning_question = is_reasoning_question or answer_mode in {
                "RULE_INFERENCE", "OWL_INFERENCE",
            }
            capability_name = str(contract.get("reasoning_capability") or "")
            derived_predicates = set(contract.get("derived_predicates") or [])
            if not is_reasoning_question:
                continue
            if reasoning_requirement != "REQUIRED":
                raise WorkflowGateError(
                    "G-S4-CQ-REASONING",
                    f"能力问题 {question_id} 要求业务判断，但 S3 未声明 reasoning_requirement=REQUIRED。",
                )
            if answer_mode not in {"RULE_INFERENCE", "OWL_INFERENCE"}:
                raise WorkflowGateError(
                    "G-S4-CQ-REASONING",
                    f"能力问题 {question_id} 是推理题，不能降级为事实查询。",
                )
            capability = capabilities.get(capability_name)
            if not isinstance(capability, dict):
                raise WorkflowGateError(
                    "G-S4-CQ-REASONING",
                    f"能力问题 {question_id} 未绑定 S3 正式推理能力。",
                )
            allowed_predicates = set(capability.get("result_predicates") or [])
            if not derived_predicates or not derived_predicates.issubset(allowed_predicates):
                raise WorkflowGateError(
                    "G-S4-CQ-REASONING",
                    f"能力问题 {question_id} 未声明由正式规则产生的 derived_predicates。",
                )
            ontology_terms = capability.get("ontology_terms") or {}
            _validate_reasoning_term_declarations(
                question_id=question_id,
                capability=capability,
                allowed_predicates=allowed_predicates,
                entity_iris=seen_iris,
            )
            required_iris = [str(ontology_terms.get(name) or "") for name in derived_predicates]
            sparql = str(question.get("sparql") or "")
            if any(not _sparql_references_iri(sparql, iri) for iri in required_iris):
                raise WorkflowGateError(
                    "G-S4-CQ-REASONING",
                    f"能力问题 {question_id} 的 SPARQL 未查询规则派生结论。",
                )

        axiom_applicability = self._logical_axiom_applicability(
            project_dir, ontology_design, verified_question_ids=verified_fact_question_ids,
        )
        if not logical_axioms and axiom_applicability["status"] != "NOT_APPLICABLE":
            raise WorkflowGateError(
                "G-S4-PRODUCTION-LOGIC",
                "缺少有来源逻辑公理，且尚未证明完整结构化事实查询适用性：" + axiom_applicability["reason"],
            )
        if axiom_applicability["status"] == "NOT_APPLICABLE":
            # Server-derived receipt is part of the design hashed by joint review.
            # The caller cannot supply an exemption flag; every call recomputes it.
            ontology_design["logical_axiom_applicability"] = axiom_applicability
        elif "logical_axiom_applicability" in ontology_design:
            ontology_design["logical_axiom_applicability"] = axiom_applicability
        return {
            "logical_axiom_applicability": axiom_applicability,
            "class_count": len(collections["classes"]),
            "object_property_count": len(collections["object_properties"]),
            "data_property_count": len(collections["data_properties"]),
            "mapping_coverage_count": len(covered_mapping_ids),
            "competency_question_count": len(questions),
            "cq_intake_question_count": cq_lineage["intake_question_count"],
            "cq_linked_question_count": cq_lineage["linked_question_count"],
            "logical_axiom_count": len(logical_axioms),
            "relationship_question_count": len(relationship_questions),
            "required_business_dimension_count": sum(
                len(
                    (question.get("answer_contract") or {}).get("required_business_dimensions")
                    or []
                )
                for question in questions
            ),
        }

    @staticmethod
    def _validate_logical_axioms(
        payload: Any,
        *,
        entity_iris: set[str],
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise WorkflowGateError("G-S4-LOGIC", "logical_axioms 必须是数组。")
        allowed = {
            "SUBCLASS_OF",
            "DISJOINT_WITH",
            "EQUIVALENT_DATA_HAS_VALUE",
            "EQUIVALENT_OBJECT_SOME_VALUES_FROM",
        }
        seen_ids: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(payload, start=1):
            if not isinstance(raw, dict):
                raise WorkflowGateError("G-S4-LOGIC", f"逻辑公理第 {index} 项必须是对象。")
            item = dict(raw)
            axiom_id = str(item.get("id") or "").strip()
            axiom_type = str(item.get("axiom_type") or "").strip().upper()
            source_refs = item.get("source_refs") or []
            if (
                not axiom_id
                or axiom_id in seen_ids
                or axiom_type not in allowed
                or not isinstance(source_refs, list)
                or not source_refs
            ):
                raise WorkflowGateError(
                    "G-S4-LOGIC", f"逻辑公理第 {index} 项缺少有效 ID、类型或来源。"
                )
            seen_ids.add(axiom_id)
            required_fields = {
                "SUBCLASS_OF": ("child", "parent"),
                "DISJOINT_WITH": ("class", "other"),
                "EQUIVALENT_DATA_HAS_VALUE": ("class", "base_class", "property", "value"),
                "EQUIVALENT_OBJECT_SOME_VALUES_FROM": (
                    "class",
                    "base_class",
                    "property",
                    "filler",
                ),
            }[axiom_type]
            missing = [field for field in required_fields if field not in item]
            iri_fields = [field for field in required_fields if field != "value"]
            unknown = [
                str(item[field])
                for field in iri_fields
                if field in item and str(item[field]) not in entity_iris
            ]
            if missing or unknown:
                raise WorkflowGateError(
                    "G-S4-LOGIC",
                    f"逻辑公理 {axiom_id} 缺字段或引用未声明实体："
                    + ", ".join([*missing, *unknown]),
                )
            normalized.append({**item, "id": axiom_id, "axiom_type": axiom_type})
        return normalized

    def _validate_s5(
        self,
        project_dir: Path,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._verify_joint_design_for_execution(project_dir)
        report = payload["protege_build_report"]
        if report.get("status") != "PASSED" or report.get("builder") != "PROTEGE_MCP":
            raise WorkflowGateError(
                "G-S5-PROTEGE-EVIDENCE",
                "S5 必须提供 builder=PROTEGE_MCP 且 status=PASSED 的真实构建报告。",
            )
        if not str(report.get("run_id") or "").strip():
            raise WorkflowGateError(
                "G-S5-PROTEGE-EVIDENCE",
                "Protégé 构建报告必须包含可追踪的 run_id。",
            )
        tool_calls = [str(item) for item in report.get("tool_calls") or []]
        if (
            not tool_calls
            or not any(
                token in call
                for call in tool_calls
                for token in ("create_", "apply_changes", "load_ontology")
            )
            or not any(
                token in call
                for call in tool_calls
                for token in ("save_ontology", "export_", "prepare_release")
            )
        ):
            raise WorkflowGateError(
                "G-S5-PROTEGE-EVIDENCE",
                "Protégé 构建报告必须记录实际构建与导出工具调用。",
            )
        exported_formats = {str(item).upper() for item in report.get("exported_formats") or []}
        if not {"OWL", "TTL", "SHACL"}.issubset(exported_formats):
            raise WorkflowGateError(
                "G-S5-PROTEGE-EVIDENCE",
                "Protégé 构建报告必须证明已导出 OWL、TTL 和 SHACL。",
            )
        reasoner_report = report.get("reasoner_report") or {}
        if (
            not isinstance(reasoner_report, dict)
            or str(reasoner_report.get("status") or "").upper() != "CONSISTENT"
            or reasoner_report.get("consistent") is not True
            or reasoner_report.get("inconsistent") is not False
            or "HERMIT" not in str(reasoner_report.get("reasoner") or "").upper()
            or not any("run_reasoner" in call for call in tool_calls)
        ):
            raise WorkflowGateError(
                "G-S5-REASONER",
                "Protégé 必须真实执行 HermiT，且 status/consistent/inconsistent 三个字段必须一致证明本体可满足。",
            )
        submitted_shacl = report.get("shacl_report") or {}
        if (
            not isinstance(submitted_shacl, dict)
            or submitted_shacl.get("conforms") is not True
            or int(submitted_shacl.get("node_shape_count") or 0) < 1
        ):
            raise WorkflowGateError(
                "G-S5-SHAPES",
                "Protégé 构建报告必须包含真实 SHACL 通过回执和非零 NodeShape。",
            )
        annotation_summary = report.get("ontology_annotation_summary") or {}
        annotation_count = int(annotation_summary.get("count") or 0)
        live_comment_zh = str(annotation_summary.get("comment_zh") or "").strip()
        if annotation_count < 3:
            raise WorkflowGateError(
                "G-S5-ONTOLOGY-ANNOTATIONS",
                "Protégé 本体总览必须实时读回至少 3 条 Ontology Annotation，不能只在导出文件中写普通注释取值。",
            )
        if not _contains_chinese(live_comment_zh) or len(live_comment_zh) < 40:
            raise WorkflowGateError(
                "G-S5-ONTOLOGY-ANNOTATIONS",
                "Protégé 本体总览必须包含不少于 40 个字符的中文详细说明，写清用途、范围、推理方式、来源或使用边界。",
            )
        try:
            ttl_graph = Graph().parse(data=payload["ontology_ttl"], format="turtle")
            owl_graph = Graph().parse(data=payload["ontology_owl"], format="xml")
            shapes_graph = Graph().parse(data=payload["shapes_ttl"], format="turtle")
        except Exception as exc:
            raise WorkflowGateError("G-S5-RDF-PARSE", f"RDF 产物无法解析：{exc}") from exc

        design = yaml.safe_load(
            (project_dir / "04-ontology-design/ontology-design.yaml").read_text(encoding="utf-8")
        )
        type_issues = [
            {**issue, "format": name}
            for name, graph in (("TTL", ttl_graph), ("OWL", owl_graph))
            for issue in validate_ontology_types(graph, design)
        ]
        if type_issues:
            raise WorkflowGateError(
                "G-S5-ENTITY-TYPES",
                "本体类型契约不一致：" + "；".join(
                    f"{issue['format']}: {issue['message']}" for issue in type_issues
                ),
            )
        design_iris = {
            str(entity["iri"])
            for key in ("classes", "object_properties", "data_properties")
            for entity in design.get(key) or []
        }
        ttl_subjects = {str(subject) for subject in ttl_graph.subjects()}
        owl_subjects = {str(subject) for subject in owl_graph.subjects()}
        missing_ttl = design_iris - ttl_subjects
        missing_owl = design_iris - owl_subjects
        if missing_ttl or missing_owl:
            raise WorkflowGateError(
                "G-S5-DESIGN-COVERAGE",
                "构建产物未覆盖全部设计 IRI："
                f"TTL 缺 {len(missing_ttl)}，OWL 缺 {len(missing_owl)}。",
            )
        reasoning_term_count = self._validate_s5_reasoning_terms(project_dir, ttl_graph)

        logical_axioms = self._validate_logical_axioms(
            design.get("logical_axioms") or [],
            entity_iris=design_iris,
        )
        axiom_applicability = self._logical_axiom_applicability(project_dir, design)
        if not logical_axioms and (
            axiom_applicability["status"] != "NOT_APPLICABLE"
            or design.get("logical_axiom_applicability") != axiom_applicability
        ):
            raise WorkflowGateError(
                "G-S5-LOGIC",
                "生产本体缺少 S4 公理或已评审且未漂移的事实查询适用性证据：" + axiom_applicability["reason"],
            )
        missing_logical_axioms: list[str] = []
        for axiom in logical_axioms:
            axiom_type = axiom["axiom_type"]
            subject = URIRef(str(axiom.get("class") or axiom.get("child")))
            if axiom_type == "SUBCLASS_OF":
                present = (
                    subject,
                    RDFS.subClassOf,
                    URIRef(str(axiom["parent"])),
                ) in ttl_graph
            elif axiom_type == "DISJOINT_WITH":
                present = has_disjoint_axiom(ttl_graph, subject, URIRef(str(axiom["other"])))
            else:
                present = False
                for expression in ttl_graph.objects(subject, OWL.equivalentClass):
                    members_node = ttl_graph.value(expression, OWL.intersectionOf)
                    if members_node is None:
                        continue
                    members = list(Collection(ttl_graph, members_node))
                    if URIRef(str(axiom["base_class"])) not in members:
                        continue
                    for restriction in members:
                        if (
                            restriction,
                            RDF.type,
                            OWL.Restriction,
                        ) not in ttl_graph or ttl_graph.value(
                            restriction, OWL.onProperty
                        ) != URIRef(str(axiom["property"])):
                            continue
                        if axiom_type == "EQUIVALENT_DATA_HAS_VALUE":
                            datatype = URIRef(
                                str(
                                    axiom.get("datatype")
                                    or "http://www.w3.org/2001/XMLSchema#string"
                                )
                            )
                            present = (
                                restriction,
                                OWL.hasValue,
                                Literal(axiom["value"], datatype=datatype),
                            ) in ttl_graph
                        else:
                            present = (
                                restriction,
                                OWL.someValuesFrom,
                                URIRef(str(axiom["filler"])),
                            ) in ttl_graph
                        if present:
                            break
                    if present:
                        break
            if not present:
                missing_logical_axioms.append(str(axiom["id"]))
        if missing_logical_axioms:
            raise WorkflowGateError(
                "G-S5-LOGIC",
                "构建产物未实现 S4 逻辑公理：" + ", ".join(missing_logical_axioms),
            )

        ontology_iri = str(design["ontology_iri"]).rstrip("#/")
        ontology_subject = next(
            (
                subject
                for subject in ttl_graph.subjects(RDF.type, OWL.Ontology)
                if str(subject).rstrip("#/") == ontology_iri
            ),
            None,
        )
        if ontology_subject is None:
            raise WorkflowGateError("G-S5-CHINESE", "RDF 产物缺少正式 owl:Ontology 声明。")

        def chinese_values(subject: Any, predicates: tuple[Any, ...]) -> list[str]:
            values: list[str] = []
            for predicate in predicates:
                for value in ttl_graph.objects(subject, predicate):
                    language = str(getattr(value, "language", "") or "").lower()
                    if language.startswith("zh") and _contains_chinese(value):
                        values.append(str(value))
            return values

        ontology_titles = chinese_values(ontology_subject, (DCTERMS.title, RDFS.label))
        if not ontology_titles:
            raise WorkflowGateError(
                "G-S5-CHINESE",
                "本体必须包含中文 dcterms:title 或 rdfs:label（@zh）。",
            )
        if not chinese_values(ontology_subject, (RDFS.comment,)):
            raise WorkflowGateError("G-S5-CHINESE", "本体必须包含中文 rdfs:comment（@zh）。")

        entity_types = (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.NamedIndividual)
        localized_entities = {
            subject
            for entity_type in entity_types
            for subject in ttl_graph.subjects(RDF.type, entity_type)
            if str(subject).startswith(ontology_iri)
        }
        missing_labels = sorted(
            str(subject)
            for subject in localized_entities
            if not chinese_values(subject, (RDFS.label,))
        )
        missing_comments = sorted(
            str(subject)
            for subject in localized_entities
            if not chinese_values(subject, (RDFS.comment,))
        )
        if missing_labels:
            raise WorkflowGateError(
                "G-S5-CHINESE",
                f"仍有 {len(missing_labels)} 个本体实体缺少中文 rdfs:label（@zh）：{', '.join(missing_labels[:3])}",
            )
        if missing_comments:
            raise WorkflowGateError(
                "G-S5-CHINESE",
                f"仍有 {len(missing_comments)} 个本体实体缺少中文 rdfs:comment（@zh）：{', '.join(missing_comments[:3])}",
            )
        shape_count = len(set(shapes_graph.subjects(RDF.type, SH.NodeShape)))
        if shape_count < 1:
            raise WorkflowGateError("G-S5-SHAPES", "shapes.ttl 至少需要一个 sh:NodeShape。")

        return {
            "ttl_triple_count": len(ttl_graph),
            "owl_triple_count": len(owl_graph),
            "class_count": len(set(ttl_graph.subjects(RDF.type, OWL.Class))),
            "object_property_count": len(set(ttl_graph.subjects(RDF.type, OWL.ObjectProperty))),
            "data_property_count": len(set(ttl_graph.subjects(RDF.type, OWL.DatatypeProperty))),
            "ontology_title_zh": ontology_titles[0],
            "chinese_label_count": len(localized_entities),
            "chinese_comment_count": len(localized_entities) + 1,
            "node_shape_count": shape_count,
            "logical_axiom_applicability": axiom_applicability,
            "logical_axiom_count": len(logical_axioms),
            "restriction_count": len(set(ttl_graph.subjects(RDF.type, OWL.Restriction))),
            "reasoning_term_count": reasoning_term_count,
        }

    def _validate_s5_reasoning_terms(self, project_dir: Path, ttl_graph: Graph) -> int:
        """Fail S5 when reviewed reasoning terms are absent from the built TTL.

        Mirrors the S7 release binding check so the mismatch is caught where it
        can still be corrected, instead of after a package has been published.
        """

        runtime_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
        if not runtime_path.is_file():
            return 0
        capabilities = self._read_json(runtime_path).get("reasoning_capabilities") or {}
        if not isinstance(capabilities, dict):
            return 0
        signature = graph_signature(ttl_graph)
        missing = reasoning_terms_absent_from(capabilities, signature)
        if missing:
            raise WorkflowGateError(
                "G-S5-REASONING-TERMS",
                "推理能力引用的术语未出现在构建后的 ontology.ttl，发布绑定必然失败："
                + "；".join(
                    f"{name}: {describe_undeclared(iris, signature)}"
                    for name, iris in missing.items()
                )
                + "。请回到 S3 修正 ontology_terms 或在 S4/S5 补齐对应实体。",
            )
        return sum(
            len(capability.get("ontology_terms") or {})
            for capability in capabilities.values()
            if isinstance(capability, dict)
        )

    @staticmethod
    def _require_passed_report(
        report: dict[str, Any],
        gate_id: str,
        label: str,
    ) -> None:
        if report.get("status") != "PASSED":
            raise WorkflowGateError(gate_id, f"{label} 报告未通过。")

    _cq_assertion_matches = staticmethod(cq_answers.cq_assertion_matches)

    _validate_cq_required_bindings = staticmethod(cq_answers.validate_cq_required_bindings)

    _validate_cq_select_semantics = staticmethod(cq_answers.validate_cq_select_semantics)

    def _validated_s6_base_release_endpoint(
        self,
        project_dir: Path,
        cq_report: dict[str, Any],
    ) -> str | None:
        """Bind full-source CQ checks to an immutable base or S5 candidate runtime."""

        validation_mode = str(cq_report.get("validation_mode") or "")
        if validation_mode == "PRE_RELEASE_FULL_SOURCE_ONTOP":
            endpoint = str(cq_report.get("validation_endpoint") or "").strip()
            if not re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}/sparql", endpoint):
                raise WorkflowGateError(
                    "G-S6-CQ",
                    "S5 候选运行时只能使用本机临时只读 Ontop endpoint。",
                )
            state = self._read_state(project_dir)
            expected_fingerprints = {
                stage: str(
                    (state.get("stage_fingerprints") or {}).get(stage, {}).get("output") or ""
                )
                for stage in ("S0", "S1", "S2", "S3", "S4", "S5")
            }
            submitted_fingerprints = {
                str(key): str(value)
                for key, value in dict(cq_report.get("source_stage_fingerprints") or {}).items()
            }
            mapping_path = project_dir / "03-mapping-review/runtime/mapping.obda"
            ontology_path = project_dir / "05-ontology-build/ontology.ttl"
            expected_mapping_sha256 = _file_checksum(mapping_path)
            expected_ontology_sha256 = _file_checksum(ontology_path)
            candidate_id = str(cq_report.get("candidate_id") or "").strip()
            if (
                not candidate_id
                or cq_report.get("database_access_mode") != "READ_ONLY"
                or cq_report.get("source_mapping_sha256") != expected_mapping_sha256
                or cq_report.get("ontology_sha256") != expected_ontology_sha256
                or submitted_fingerprints != expected_fingerprints
                or any(not value for value in expected_fingerprints.values())
            ):
                raise WorkflowGateError(
                    "G-S6-CQ",
                    "S5 候选运行时未绑定当前 S0-S5 指纹、正式 Mapping 或只读数据库身份。",
                )
            try:
                identity = self._execute_s6_read_only_sparql(
                    endpoint,
                    """SELECT ?deployment_id ?source_mapping_sha256 ?access_mode WHERE {
  <urn:orion:ontop:deployment-marker>
    <urn:orion:ontop:deploymentId> ?deployment_id ;
    <urn:orion:ontop:sourceMappingSha256> ?source_mapping_sha256 ;
    <urn:orion:ontop:accessMode> ?access_mode .
}""",
                    "SELECT",
                )
            except Exception as exc:
                raise WorkflowGateError(
                    "G-S6-CQ",
                    f"S5 候选 Ontop 身份回读失败：{exc}",
                ) from exc
            rows = identity.get("rows") or []
            if not any(
                row.get("deployment_id") == candidate_id
                and row.get("source_mapping_sha256") == expected_mapping_sha256
                and row.get("access_mode") == "READ_ONLY"
                for row in rows
            ):
                raise WorkflowGateError(
                    "G-S6-CQ",
                    "S5 候选 Ontop 回读身份与当前工程不一致。",
                )
            return endpoint

        if validation_mode != "BASE_RELEASE_FULL_SOURCE_ONTOP":
            return None
        reference_path = project_dir / "based-on-release.json"
        if not reference_path.exists():
            raise WorkflowGateError("G-S6-CQ", "全量 CQ 验证缺少基线发布版本锚。")
        reference = self._read_json(reference_path)
        source_project_id = str(reference.get("source_project_id") or "")
        source_release_version = str(reference.get("source_release_version") or "")
        source_dir = self._resolve_project(source_project_id)
        publication = self._read_json(source_dir / "07-release/publication.json")
        binding_path = (
            self.root.parent
            / ".orion-runtime/realtime-business"
            / source_project_id
            / source_release_version
            / "deployment-binding.json"
        )
        if not binding_path.is_file():
            raise WorkflowGateError("G-S6-CQ", "基线发布版本缺少运行时绑定。")
        binding = self._read_json(binding_path)
        expected_endpoint = str(binding.get("endpoint") or "").strip()
        expected_fingerprint = str(publication.get("package_manifest_sha256") or "")
        submitted_endpoint = str(cq_report.get("validation_endpoint") or "").strip()
        if (
            not expected_endpoint
            or submitted_endpoint != expected_endpoint
            or cq_report.get("base_release_project_id") != source_project_id
            or cq_report.get("base_release_version") != source_release_version
            or cq_report.get("base_release_fingerprint") != expected_fingerprint
            or binding.get("project_id") != source_project_id
            or binding.get("release_version") != source_release_version
            or binding.get("release_fingerprint") != expected_fingerprint
            or binding.get("database_access_mode") != "READ_ONLY"
        ):
            raise WorkflowGateError(
                "G-S6-CQ",
                "全量 CQ 验证端点或基线版本锚与不可变发布记录不一致。",
            )
        return expected_endpoint

    @staticmethod
    def _execute_s6_read_only_sparql(
        endpoint: str,
        sparql: str,
        query_type: str,
    ) -> dict[str, Any]:
        normalized_type = query_type.upper()
        if normalized_type not in {"SELECT", "ASK", "CONSTRUCT", "DESCRIBE"}:
            raise WorkflowError("S6 CQ 查询类型不受支持。")
        if re.search(
            r"\b(?:INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b",
            sparql,
            re.IGNORECASE,
        ):
            raise WorkflowError("S6 CQ 只允许只读 SPARQL。")
        accept = (
            "application/sparql-results+json"
            if normalized_type in {"SELECT", "ASK"}
            else "text/turtle"
        )
        with httpx.Client(timeout=60.0, trust_env=False) as client:
            response = client.post(
                endpoint,
                data={"query": sparql},
                headers={"Accept": accept},
            )
        response.raise_for_status()
        if normalized_type == "SELECT":
            from .cq_validation import decode_s6_select
            return decode_s6_select(response.json())
        if normalized_type == "ASK":
            return {"boolean": bool(response.json().get("boolean"))}
        return {"graph": Graph().parse(data=response.text, format="turtle")}

    def _validate_s6(
        self,
        project_dir: Path,
        payload: dict[str, Any],
        *,
        progress: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ) -> dict[str, Any]:
        self._verify_joint_design_for_execution(project_dir)
        emit = progress or (lambda _subgate, _status, _details=None: None)
        hermit = payload["hermit_report"]
        mapping = payload["mapping_report"]
        semantic = payload["semantic_quality_report"]
        cq = payload["competency_question_report"]
        semantica = payload["semantica_report"]
        # A new S3 execution policy requires server-produced live evidence.
        # Submitted mapping_report flags cannot satisfy this gate.
        runtime_source_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
        runtime_source = self._read_json(runtime_source_path) if runtime_source_path.is_file() else {}
        backend_validation = {"status": "NOT_REQUIRED_BY_FROZEN_CONTRACT"}
        if (runtime_source.get("target_backend_validation") or {}).get("policy"):
            from services.ontop_client.backend_validation import POLICY, validate_candidate_queries

            if runtime_source["target_backend_validation"]["policy"] != POLICY:
                raise WorkflowGateError("G-S6-MAPPING-TARGET-BACKEND", "目标执行验证策略已改变，须重新预检 S3。")
            if cq.get("validation_mode") != "PRE_RELEASE_FULL_SOURCE_ONTOP":
                raise WorkflowGateError("G-S6-MAPPING-TARGET-BACKEND", "正式运行查询必须使用当前 S3/S5 候选 Ontop 实际验证。")
            emit("MAPPING", "RUNNING", {"phase": "TARGET_BACKEND"})
            endpoint = self._validated_s6_base_release_endpoint(project_dir, cq)
            try:
                backend_validation = validate_candidate_queries(
                    project_dir=project_dir, endpoint=endpoint,
                    candidate_id=str(cq.get("candidate_id") or ""),
                    purpose="FORMAL_GATE_REEXECUTION",
                    receipt_path=project_dir / ".stage-executions/S6-artifacts/target-backend-server-validation.json",
                )
            except Exception as exc:
                raise WorkflowGateError(
                    "G-S6-MAPPING-TARGET-BACKEND", f"候选 Ontop 正式查询用例执行失败：{type(exc).__name__}",
                ) from exc
            emit("MAPPING", "RUNNING", {"target_backend_validation": backend_validation})
        emit("HERMIT", "RUNNING", None)
        self._require_passed_report(hermit, "G-S6-HERMIT", "HermiT")
        if (
            hermit.get("consistent") is not True
            or str(hermit.get("reasoner") or "").upper() != "HERMIT"
            or not str(hermit.get("run_id") or "").strip()
        ):
            raise WorkflowGateError("G-S6-HERMIT", "HermiT 未证明本体一致。")
        emit("HERMIT", "PASSED", {"run_id": hermit.get("run_id")})
        emit("MAPPING", "RUNNING", None)
        self._require_passed_report(mapping, "G-S6-MAPPING", "Mapping")
        design = yaml.safe_load(
            (project_dir / "04-ontology-design/ontology-design.yaml").read_text(encoding="utf-8")
        )
        mapping_document = yaml.safe_load(
            (project_dir / "03-mapping-review/mapping.yaml").read_text(encoding="utf-8")
        )
        expected_mapping_count = len(mapping_document.get("mappings") or [])
        if (
            int(mapping.get("unmapped_count", 0)) != 0
            or int(mapping.get("mapped_count", -1)) != expected_mapping_count
        ):
            raise WorkflowGateError("G-S6-MAPPING", "Mapping 仍存在未覆盖项。")
        emit("MAPPING", "PASSED", {"mapped_count": expected_mapping_count})
        emit("SEMANTIC", "RUNNING", None)
        self._require_passed_report(semantic, "G-S6-SEMANTIC", "语义质量")
        expected_entity_count = sum(
            len(design.get(key) or [])
            for key in ("classes", "object_properties", "data_properties")
        )
        if (
            int(semantic.get("high_severity_issue_count", 0)) != 0
            or int(semantic.get("checked_entity_count", -1)) < expected_entity_count
        ):
            raise WorkflowGateError("G-S6-SEMANTIC", "语义质量仍存在高严重度问题。")
        emit("SEMANTIC", "PASSED", {"checked_entity_count": expected_entity_count})
        design_questions = self._validate_competency_questions(
            design.get("competency_questions") or []
        )
        cq_lineage = self._validate_competency_question_lineage(
            project_dir,
            design_questions,
        )
        total_questions = len(design_questions)
        declared_total = cq.get("total")
        if declared_total is not None and int(declared_total) != total_questions:
            raise WorkflowGateError(
                "G-S6-CQ",
                "调用方提交的 CQ 数量与 S4 正式设计不一致；服务端拒绝采用该报告。",
            )
        declared_ids = cq.get("question_ids")
        design_ids = [str(question["id"]) for question in design_questions]
        if declared_ids is not None and list(declared_ids) != design_ids:
            raise WorkflowGateError(
                "G-S6-CQ",
                "调用方提交的 CQ 编号或顺序与 S4 正式设计不一致。",
            )
        emit("SEMANTICA", "RUNNING", None)
        self._require_passed_report(semantica, "G-S6-SEMANTICA", "Semantica")
        semantica_source_mode = self._validate_s6_semantica_source_mode(
            project_dir,
            semantica,
        )
        reasoning_capability_validation = self._validate_s6_reasoning_capabilities(
            project_dir,
            semantica,
        )
        emit(
            "SEMANTICA",
            "PASSED",
            {
                "run_id": semantica.get("run_id"),
                "instance_count": semantica.get("instance_count"),
            },
        )

        # Reject missing/invalid relationship contracts before expensive graph work.
        self._validate_s6_graph_relationships(
            project_dir=project_dir, design=design, mapping_document=mapping_document,
            data_graph=None, ontology_graph=None, semantica=semantica,
            contract_only=True,
        )
        emit("SHACL", "RUNNING", None)
        try:
            from .materialized_graph import parse_materialized_graph

            data_graph = parse_materialized_graph(
                payload["materialized_ttl"], format=semantica.get("materialized_format", "turtle"),
            )
            ontology_graph = Graph().parse(
                project_dir / "05-ontology-build/ontology.ttl",
                format="turtle",
            )
            shapes_graph = Graph().parse(
                project_dir / "05-ontology-build/shapes.ttl",
                format="turtle",
            )
            from .shacl_validation import validate_complete_graph

            (conforms, report_graph, report_text), shacl_execution = validate_complete_graph(
                data_graph, shapes_graph, ontology_graph,
            )
        except Exception as exc:
            raise WorkflowGateError("G-S6-SHACL", f"SHACL 验证执行失败：{exc}") from exc
        if not conforms:
            raise WorkflowGateError("G-S6-SHACL", "真实实例图未通过 SHACL 约束。")
        emit("SHACL", "PASSED", {
            "materialized_triple_count": len(data_graph),
            "shacl_execution": shacl_execution,
        })

        from .graph_union import ReadOnlyGraphUnion
        from .validation_plan import uses_capability_validation

        combined_graph = (
            ReadOnlyGraphUnion([ontology_graph, data_graph])
            if uses_capability_validation(project_dir, semantica)
            else ontology_graph + data_graph
        )
        relationship_validation = self._validate_s6_graph_relationships(
            project_dir=project_dir,
            design=design,
            mapping_document=mapping_document,
            data_graph=data_graph,
            ontology_graph=ontology_graph,
            semantica=semantica,
        )
        submitted_cq_validation_mode = str(cq.get("validation_mode") or "")
        remote_cq_endpoint = self._validated_s6_base_release_endpoint(project_dir, cq)
        from .cq_validation import validate_cq_answers

        cq_execution = validate_cq_answers(
            self, project_dir=project_dir, questions=design_questions, combined_graph=combined_graph,
            remote_cq_endpoint=remote_cq_endpoint, submitted_cq_validation_mode=submitted_cq_validation_mode,
            emit=emit, fingerprint=_fingerprint,
        )

        emit("PRODUCTION_COVERAGE", "RUNNING", None)
        production_coverage = self._validate_s6_production_coverage(
            project_dir,
            semantica,
        )
        emit("PRODUCTION_COVERAGE", "PASSED", {"status": production_coverage.get("status")})

        server_cq_report = {
            "schema_version": 3,
            "status": "PASSED",
            "validation_mode": (
                "SERVER_EXECUTED_BASE_RELEASE_FULL_SOURCE_ONTOP"
                if submitted_cq_validation_mode == "BASE_RELEASE_FULL_SOURCE_ONTOP"
                and remote_cq_endpoint
                else "SERVER_EXECUTED_SEMANTIC_ANSWER_CONTRACT"
            ),
            "total": total_questions,
            "passed": len(cq_execution),
            "question_ids": design_ids,
            "questions_sha256": _fingerprint(design_questions),
            "intake_questions_sha256": cq_lineage["intake_questions_sha256"],
            "results_sha256": _fingerprint(cq_execution),
            "results": cq_execution,
            "validated_at": _now(),
            "submitted_report_status": cq.get("status"),
        }

        quality = {
            "status": "PASSED",
            "production_ready": True,
            "production_gate_policy_version": PRODUCTION_GATE_POLICY_VERSION,
            "validated_at": _now(),
            "materialized_triple_count": len(data_graph),
            "shacl_conforms": True,
            "shacl_execution": shacl_execution,
            "competency_question_total": total_questions,
            "semantica_instance_count": int(semantica["instance_count"]),
            "semantica_source_mode": semantica_source_mode,
            "cq_validation_mode": (
                "BASE_RELEASE_FULL_SOURCE_ONTOP"
                if submitted_cq_validation_mode == "BASE_RELEASE_FULL_SOURCE_ONTOP"
                and remote_cq_endpoint
                else "FULL_SOURCE_MATERIALIZED_GRAPH"
            ),
            "production_coverage": production_coverage,
            "reasoning_capability_validation": reasoning_capability_validation,
            "relationship_validation": relationship_validation,
            "mapping_coverage_count": expected_mapping_count,
            "target_backend_validation": backend_validation,
            "semantic_checked_entity_count": int(semantic["checked_entity_count"]),
            "competency_question_execution": cq_execution,
            "competency_question_report": server_cq_report,
            "competency_question_lineage": cq_lineage,
            "shacl_report_ttl": str(report_graph.serialize(format="turtle")),
            "shacl_report_text": str(report_text),
        }
        if (project_dir / "workflow-state.json").is_file() and business_contract.enabled(self._read_state(project_dir)):
            try:
                instance_validation = business_contract.validate_instances(design.get("classes") or [], data_graph)
            except ValueError as exc:
                raise WorkflowGateError("G-S6-CLASS-INSTANCES", str(exc)) from exc
            if instance_validation["status"] != "PASSED":
                missing = [row["class_iri"] for row in instance_validation["classes"] if row["status"] == "FAILED"]
                raise WorkflowGateError("G-S6-CLASS-INSTANCES", f"要求有实例的类没有生成显式成员：{missing}")
            quality["class_instance_validation"] = instance_validation
            from .mapping_quality import build_mapping_quality_report
            quality["mapping_quality_report"] = build_mapping_quality_report(
                data_graph, design, mapping_document.get("mappings") or [])
            if quality["mapping_quality_report"]["error_count"]:
                codes = sorted({item["code"] for item in quality["mapping_quality_report"]["findings"] if item["severity"] == "ERROR"})
                raise WorkflowGateError("G-S6-MAPPING-INTEGRITY", "映射与实例结构核验失败：" + ", ".join(codes))
            from .protege_instance_preview import build_preview
            preview_owl, preview_report = build_preview(
                ontology_graph=ontology_graph, data_graph=data_graph,
                classes=design.get("classes") or [], project_id=project_dir.name,
                model_version=str(design.get("version") or ""),
                snapshot_sha256="sha256:" + hashlib.sha256(str(payload["materialized_ttl"]).encode("utf-8")).hexdigest(),
            )
            preview_report["model_sha256"] = _file_checksum(project_dir / "05-ontology-build/ontology.ttl")
            quality["protege_instance_preview"] = preview_report
            quality["protege_instance_preview_owl"] = preview_owl
        from .validation_plan import build_s6_validation_plan

        try:
            plan = build_s6_validation_plan(project_dir, payload, quality)
        except ValueError as exc:
            raise WorkflowGateError("G-S6-EXECUTION-PLAN", str(exc)) from exc
        if plan is not None:
            quality["validation_plan"] = plan
        return quality

    def _validate_s6_graph_relationships(
        self,
        *,
        project_dir: Path,
        design: dict[str, Any],
        mapping_document: dict[str, Any],
        data_graph: Graph | None,
        ontology_graph: Graph | None,
        semantica: dict[str, Any],
        contract_only: bool = False,
    ) -> dict[str, Any]:
        """Fail closed when a relationship CQ is backed only by nodes or class labels."""

        questions = design.get("competency_questions") or []
        relationship_questions = _relationship_competency_questions(
            questions, design.get("object_properties") or [],
        )
        if not relationship_questions:
            return {
                "status": "NOT_APPLICABLE",
                "relationship_question_count": 0,
                "reason": "S4 没有声明关系型业务问题。",
            }

        properties = {
            str(item.get("iri") or ""): item
            for item in design.get("object_properties") or []
            if isinstance(item, dict)
        }
        declared_dimension_paths = {
            str(dimension.get("path") or "")
            for question in relationship_questions
            for dimension in (
                (question.get("answer_contract") or {}).get("required_business_dimensions") or []
            )
            if dimension.get("applicability") == "REQUIRED"
            and dimension.get("dimension") != "subject"
        }
        data_property_iris = {
            str(item.get("iri") or "")
            for item in design.get("data_properties") or []
            if isinstance(item, dict)
        }
        unknown_dimension_paths = declared_dimension_paths - (set(properties) | data_property_iris)
        if unknown_dimension_paths:
            raise WorkflowGateError(
                "G-S6-GRAPH-RELATIONSHIP",
                "关系型 CQ 引用了未声明的属性路径：" + ", ".join(sorted(unknown_dimension_paths)),
            )
        # A relationship answer can legitimately expose both object links and
        # literal dimensions (amount, severity, status, etc.).  Only object
        # properties belong in the Semantica edge/domain/range read-back; data
        # properties are validated by the CQ business-dimension contract.
        from .sparql_paths import graph_predicates

        try:
            query_paths = {
                path for question in relationship_questions
                for path in graph_predicates(str(question.get("sparql") or ""))
            } & set(properties)
        except Exception as exc:
            raise WorkflowGateError(
                "G-S6-GRAPH-RELATIONSHIP", "无法解析已冻结 CQ 的实际对象属性路径。",
            ) from exc
        # Output dimensions may be literal labels while their frozen query joins
        # traverse actual object properties. Both are approved contract evidence.
        required_paths = (declared_dimension_paths & set(properties)) | query_paths
        if not required_paths:
            raise WorkflowGateError(
                "G-S6-GRAPH-RELATIONSHIP",
                "关系型 CQ 没有冻结至少一条对象属性路径。",
            )

        if contract_only:
            return {"status": "CONTRACT_VALIDATED", "required_paths": sorted(required_paths)}
        if data_graph is None or ontology_graph is None:
            raise WorkflowGateError("G-S6-GRAPH-RELATIONSHIP", "关系验收缺少实际实例图。")

        if int(semantica.get("relationship_count", 0)) < 1:
            raise WorkflowGateError(
                "G-S6-GRAPH-RELATIONSHIP",
                "关系型本体的实际物化图没有对象属性实例关系。",
            )

        mapping_by_id = {
            str(item.get("id") or ""): item
            for item in mapping_document.get("mappings") or []
            if isinstance(item, dict)
        }
        predicate_results: list[dict[str, Any]] = []
        for path in sorted(required_paths):
            predicate = URIRef(path)
            triples = list(data_graph.triples((None, predicate, None)))
            if not triples:
                raise WorkflowGateError(
                    "G-S6-GRAPH-RELATIONSHIP",
                    f"关系型 CQ 所需谓词 {path} 未在实际物化图中出现。",
                )
            prop = properties[path]
            domain = URIRef(str(prop.get("domain") or ""))
            range_iri = URIRef(str(prop.get("range") or ""))
            if (
                (predicate, RDF.type, OWL.ObjectProperty) not in ontology_graph
                or (predicate, RDFS.domain, domain) not in ontology_graph
                or (predicate, RDFS.range, range_iri) not in ontology_graph
            ):
                raise WorkflowGateError(
                    "G-S6-GRAPH-RELATIONSHIP",
                    f"谓词 {path} 的 OWL ObjectProperty/domain/range 与 S4 不一致。",
                )
            invalid = [
                (str(subject), str(obj))
                for subject, _, obj in triples
                if (subject, RDF.type, domain) not in data_graph
                or (obj, RDF.type, range_iri) not in data_graph
            ]
            if invalid:
                raise WorkflowGateError(
                    "G-S6-GRAPH-RELATIONSHIP",
                    f"谓词 {path} 的实例端点未按 domain/range 显式定型。",
                )
            source_mapping_ids = [str(value) for value in prop.get("source_mapping_ids") or []]
            evidence_refs = sorted(
                {
                    str(ref)
                    for mapping_id in source_mapping_ids
                    for ref in (mapping_by_id.get(mapping_id) or {}).get("source_refs", [])
                }
            )
            if not evidence_refs:
                raise WorkflowGateError(
                    "G-S6-GRAPH-RELATIONSHIP",
                    f"谓词 {path} 没有可审计来源证据。",
                )
            predicate_results.append(
                {
                    "predicate": path,
                    "triple_count": len(triples),
                    "domain": str(domain),
                    "range": str(range_iri),
                    "source_mapping_ids": source_mapping_ids,
                    "evidence_refs": evidence_refs,
                }
            )

        state = self._read_state(project_dir)
        s5_fingerprint = str(
            (state.get("stage_fingerprints") or {}).get("S5", {}).get("output") or ""
        )
        if not s5_fingerprint:
            raise WorkflowGateError(
                "G-S6-GRAPH-RELATIONSHIP",
                "关系验证缺少当前 S5 发布候选指纹。",
            )
        expected_relationships = sorted(
            [
                {
                    "subject": str(subject),
                    "predicate": str(predicate),
                    "object": str(obj),
                }
                for subject, predicate, obj in data_graph
                if str(predicate) in properties
                and isinstance(subject, URIRef)
                and isinstance(obj, URIRef)
            ],
            key=lambda item: (
                item["subject"],
                item["predicate"],
                item["object"],
            ),
        )
        expected_relationships_sha256 = _fingerprint(expected_relationships)
        from .validation_plan import uses_capability_validation

        if uses_capability_validation(project_dir, semantica):
            # The service has checked the relationship CQs' required paths,
            # endpoint types and sources above. Persisting a copy into Explorer
            # is deployment work, not proof that these relationships exist.
            return {
                "status": "VERIFIED",
                "validation_scope": "SERVER_VALIDATED_FULL_SOURCE_GRAPH",
                "relationship_question_count": len(relationship_questions),
                "materialized_relationship_count": len(expected_relationships),
                "relationship_set_sha256": expected_relationships_sha256,
                "required_predicates": predicate_results,
                "ontology_sha256": _file_checksum(project_dir / "05-ontology-build/ontology.ttl"),
                "release_candidate_fingerprint": s5_fingerprint,
                "external_graph_persistence": "NOT_REQUIRED_FOR_VALIDATION",
            }
        relationship_import = semantica.get("relationship_import_result") or {}
        verification = semantica.get("candidate_relationship_verification") or {}
        verified_relationships = verification.get("verified_relationships") or []
        verified_predicates = {
            str(value) for value in verification.get("verified_predicates") or []
        }
        edges_added = int(relationship_import.get("edges_added", 0))
        expected_mode = "NEW_RELATIONSHIPS_ADDED" if edges_added > 0 else "IDEMPOTENT_READBACK"
        if (
            verification.get("status") != "VERIFIED"
            or verification.get("mode") != expected_mode
            or verification.get("release_candidate_fingerprint") != s5_fingerprint
            or verification.get("relationship_set_sha256") != expected_relationships_sha256
            or verification.get("verified_relationships_sha256") != expected_relationships_sha256
            or verified_relationships != expected_relationships
            or int(verification.get("expected_relationship_count", -1))
            != len(expected_relationships)
            or int(verification.get("verified_relationship_count", -1))
            != len(expected_relationships)
            or int(verification.get("candidate_relationship_count", -1))
            != len(expected_relationships)
            or not required_paths.issubset(verified_predicates)
        ):
            raise WorkflowGateError(
                "G-S6-GRAPH-RELATIONSHIP",
                "Semantica 未按当前 S5 候选指纹逐条回读全部业务关系；"
                "重复导入只有在关系集合哈希及主体/谓词/客体完全一致时才可通过。",
            )
        return {
            "status": "VERIFIED",
            "relationship_question_count": len(relationship_questions),
            "materialized_relationship_count": int(semantica.get("relationship_count", 0)),
            "semantica_relationship_edges_added": edges_added,
            "semantica_relationship_import_mode": expected_mode,
            "relationship_set_sha256": expected_relationships_sha256,
            "semantica_readback_receipt_sha256": verification.get("receipt_sha256"),
            "required_predicates": predicate_results,
            "ontology_sha256": _file_checksum(project_dir / "05-ontology-build/ontology.ttl"),
            "release_candidate_fingerprint": s5_fingerprint,
        }

    def _validate_s6_cq_business_dimensions(
        self,
        *,
        project_dir: Path,
        question: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        dimensions = list(
            (question.get("answer_contract") or {}).get("required_business_dimensions") or []
        )
        state = self._read_state(project_dir)
        candidate_fingerprint = str(
            (state.get("stage_fingerprints") or {}).get("S5", {}).get("output") or ""
        )
        results: list[dict[str, Any]] = []
        for dimension in dimensions:
            if dimension.get("applicability") == "NOT_APPLICABLE":
                results.append(
                    {
                        **dimension,
                        "status": "NOT_APPLICABLE_WITH_EVIDENCE",
                    }
                )
                continue
            binding = str(dimension.get("binding") or "")
            # A required dimension must occur at least once; None/"" elsewhere
            # is accepted only under reviewed same-row nullable_bindings.
            values = cq_answers.cq_business_dimension_values(
                question_id=question.get("id"), contract=question.get("answer_contract") or {},
                dimension=dimension, rows=rows,
            )
            results.append(
                {
                    "dimension": dimension.get("dimension"),
                    "label_zh": dimension.get("label_zh"),
                    "binding": binding,
                    "ontology_term": dimension.get("ontology_term"),
                    "path": dimension.get("path"),
                    "evidence_refs": dimension.get("evidence_refs"),
                    "value_count": len(values),
                    "values_sha256": _fingerprint(values),
                    "status": "VERIFIED",
                }
            )
        return {
            "status": "VERIFIED" if dimensions else "NOT_APPLICABLE",
            "required_business_dimension_count": len(dimensions),
            "dimensions": results,
            "release_candidate_fingerprint": candidate_fingerprint or None,
        }

    def _validate_s6_semantica_source_mode(
        self,
        project_dir: Path,
        semantica: dict[str, Any],
    ) -> str:
        from .validation_plan import uses_capability_validation

        local_validation = uses_capability_validation(project_dir, semantica)
        expected_engine = "ORION_LOCAL_VALIDATION" if local_validation else "SEMANTICA_MCP"
        if (
            str(semantica.get("engine") or "") != expected_engine
            or not str(semantica.get("run_id") or "").strip()
        ):
            raise WorkflowGateError("G-S6-SEMANTICA", "实例与规则验证报告的执行器或运行标识不符合当前策略。")
        instance_count = int(semantica.get("instance_count", -1))
        if instance_count > 0:
            return "BUSINESS_INSTANCES"

        state_path = project_dir / "workflow-state.json"
        state = self._read_state(project_dir) if state_path.is_file() else {}
        if str(state.get("intake_mode") or "").upper() == "DOCUMENT_ONLY":
            document_fact_count = int(semantica.get("document_fact_count", -1))
            evidence_unit_count = int(semantica.get("document_evidence_unit_count", -1))
            if document_fact_count > 0 and evidence_unit_count > 0:
                return "DOCUMENT_EVIDENCE_FACTS"
            raise WorkflowGateError(
                "G-S6-SEMANTICA",
                "纯资料工程必须把完整证据索引筛选结果及正式文档事实送入 Semantica 验证。",
            )

        profile_path = project_dir / "01-data-understanding/data-profile.json"
        if not profile_path.exists():
            raise WorkflowGateError("G-S6-SEMANTICA", "Semantica 报告缺少真实实例验证。")
        profile = self._read_json(profile_path)
        table_count = int(profile.get("table_count", -1))
        empty_table_count = int(profile.get("empty_table_count", -1))
        declared_total_rows = int(profile.get("total_rows", profile.get("total_row_count", -1)))
        empty_source_proven = (
            declared_total_rows == 0
            and table_count > 0
            and empty_table_count == table_count
            and (
                profile.get("profile_mode") == "FULL_IMPORT_WITH_EXACT_COUNTS"
                or profile.get("status") == "EMPTY_SOURCE"
            )
        )
        empty_runtime_proven = (
            instance_count == 0
            and int(semantica.get("source_row_count", -1)) == 0
            and int(semantica.get("relationship_count", -1)) == 0
            and semantica.get("reasoning_probe_mode")
            in {"EMPTY_SOURCE_BOUNDARY", "FULL_SOURCE_RULE_PACKAGE"}
            and semantica.get("materialization_scope", "FULL_SOURCE_VALIDATION")
            == "FULL_SOURCE_VALIDATION"
        )
        if not empty_source_proven or not empty_runtime_proven:
            raise WorkflowGateError("G-S6-SEMANTICA", "Semantica 报告缺少真实实例验证。")
        return "EMPTY_SOURCE_BOUNDARY"

    def _validate_s6_reasoning_capabilities(
        self,
        project_dir: Path,
        semantica: dict[str, Any],
    ) -> dict[str, Any]:
        runtime_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
        if not runtime_path.exists():
            return {"declared": 0, "validated": 0, "capabilities": []}
        runtime = self._read_json(runtime_path)
        capabilities = runtime.get("reasoning_capabilities") or {}
        if not isinstance(capabilities, dict):
            raise WorkflowGateError(
                "G-S6-REASONING",
                "S3 推理能力契约不是有效对象。",
            )
        requirement = str(runtime.get("reasoning_requirement") or "").strip().upper()
        if int(runtime.get("schema_version") or 1) >= 4:
            if requirement not in {"REQUIRED", "NOT_APPLICABLE"}:
                raise WorkflowGateError(
                    "G-S6-REASONING",
                    "S3 缺少 REQUIRED / NOT_APPLICABLE 推理政策。",
                )
            if requirement == "REQUIRED" and not capabilities:
                raise WorkflowGateError(
                    "G-S6-REASONING",
                    "S3 声明必须推理，但没有正式 reasoning_capabilities。",
                )
            if requirement == "NOT_APPLICABLE":
                reason = str(runtime.get("reasoning_not_applicable_reason") or "").strip()
                if capabilities or len(reason) < 12:
                    raise WorkflowGateError(
                        "G-S6-REASONING",
                        "S3 推理不适用判定与能力集合或理由不一致。",
                    )
        if not capabilities:
            return {
                "requirement": requirement or "LEGACY_UNDECLARED",
                "not_applicable_reason": runtime.get("reasoning_not_applicable_reason"),
                "declared": 0,
                "validated": 0,
                "capabilities": [],
            }

        submitted = semantica.get("reasoning_capability_results")
        if not isinstance(submitted, list):
            raise WorkflowGateError(
                "G-S6-REASONING",
                "Semantica 报告缺少正式推理能力验证结果。",
            )
        by_name = {
            str(item.get("capability_name") or ""): item
            for item in submitted
            if isinstance(item, dict)
        }
        if set(by_name) != set(capabilities):
            raise WorkflowGateError(
                "G-S6-REASONING",
                "Semantica 推理能力验证集合与 S3 正式契约不一致。",
            )

        accepted_scopes = {
            "FULL_SOURCE_VALIDATION",
            "FULL_SOURCE_VALIDATION_VIA_BASE_RELEASE_RUNTIME",
        }
        if any(
            str(item.get("validation_scope") or "").upper()
            == "FULL_SOURCE_VALIDATION_VIA_BASE_RELEASE_RUNTIME"
            for item in by_name.values()
        ):
            anchor = semantica.get("full_source_validation") or {}
            self._validated_s6_base_release_endpoint(
                project_dir,
                {
                    "validation_mode": "BASE_RELEASE_FULL_SOURCE_ONTOP",
                    "validation_endpoint": anchor.get("endpoint"),
                    "base_release_project_id": anchor.get("project_id"),
                    "base_release_version": anchor.get("release_version"),
                    "base_release_fingerprint": anchor.get("release_fingerprint"),
                },
            )
        validated: list[dict[str, Any]] = []
        for name, capability in sorted(capabilities.items()):
            result = by_name[name]
            validation_scope = str(result.get("validation_scope") or "").upper()
            execution_scope = str(capability.get("execution_scope") or "").upper()
            expected_sha256 = str(capability.get("rule_sha256") or "")
            runtime_root = (project_dir / "03-mapping-review/runtime").resolve()
            rule_path = (runtime_root / str(capability.get("rule_artifact") or "")).resolve()
            if runtime_root not in rule_path.parents or not rule_path.is_file():
                raise WorkflowGateError(
                    "G-S6-REASONING",
                    f"推理能力 {name} 的规则发布物路径无效。",
                )
            actual_sha256 = _file_checksum(rule_path)
            separated_evidence = result.get("result_evidence_version") == 2
            if separated_evidence:
                expected_outcome = str(
                    (capability.get("runtime_validation") or {}).get("expected_live_outcome") or "POSITIVE"
                ).upper()
                derived = result.get("derived_facts")
                live_trace = result.get("trace")
                scenarios = result.get("controlled_scenarios") or {}
                positive = scenarios.get("positive") or {}
                negative = scenarios.get("negative") or {}
                positive_facts = positive.get("derived_facts") or []
                positive_trace = positive.get("trace") or []
                removal = negative.get("transformation") or {}
                result_predicates = set(capability.get("result_predicates") or [])

                def result_conclusions(trace, predicates=result_predicates):
                    return {
                        str(item.get("conclusion") or "") for item in trace
                        if isinstance(item, dict)
                        and str(item.get("conclusion") or "").split("(", 1)[0] in predicates
                    }

                evidence_valid = (
                    result.get("derived_fact_scope") == "SOURCE_BACKED"
                    and expected_outcome in {"POSITIVE", "NEGATIVE"}
                    and result.get("expected_live_outcome") == expected_outcome
                    and isinstance(derived, list) and isinstance(live_trace, list)
                    and len(derived) == result.get("result_fact_count") == result.get("live_result_fact_count")
                    and set(derived) == result_conclusions(live_trace)
                    and int(result.get("rules_fired", -1)) >= 0
                    and (bool(derived) if expected_outcome == "POSITIVE" else not derived)
                    and positive.get("status") == "PASSED"
                    and positive.get("scope") == (
                        "SOURCE_BACKED" if expected_outcome == "POSITIVE" else "COUNTERFACTUAL_FIXTURE"
                    )
                    and positive_facts and isinstance(positive_trace, list)
                    and set(positive_facts) == result_conclusions(positive_trace)
                    and int(positive.get("input_fact_count", 0)) > 0
                    and int(positive.get("rules_fired", 0)) > 0
                    and (expected_outcome != "POSITIVE" or (positive_facts == derived and positive_trace == live_trace))
                    and negative.get("scope") == "COUNTERFACTUAL_FIXTURE"
                    and negative.get("status") == "PASSED"
                    and removal.get("kind") == "REMOVE_TARGET_PREMISES"
                    and removal.get("removed_facts")
                    and removal.get("target") in positive_facts
                    and removal.get("target") not in (negative.get("derived_facts") or [])
                    and removal.get("target") not in result_conclusions(negative.get("trace") or [])
                )
            else:
                evidence_valid = (
                    int(result.get("result_fact_count", 0)) > 0
                    and int(result.get("rules_fired", 0)) > 0
                    and isinstance(result.get("trace"), list)
                    and bool(result.get("trace"))
                )
            if (
                result.get("status") != "PASSED"
                or execution_scope != "FULL_QUERY_RESULT"
                or validation_scope not in accepted_scopes
                or str(result.get("rule_sha256") or "") != expected_sha256
                or actual_sha256 != expected_sha256
                or int(result.get("input_fact_count", 0)) < 1
                or not evidence_valid
                or int(result.get("positive_case_count", 0)) < 1
                or int(result.get("negative_case_count", 0)) < 1
                or result.get("live_outcome_verified") is not True
            ):
                raise WorkflowGateError(
                    "G-S6-REASONING",
                    f"推理能力 {name} 未完成规则哈希一致的正例、反例和轨迹验证。{result.get('failure_reason') or ''}",
                )
            validated.append(
                {
                    "capability_name": name,
                    "execution_scope": capability.get("execution_scope"),
                    "validation_scope": validation_scope,
                    "rule_sha256": expected_sha256,
                    "input_fact_count": int(result["input_fact_count"]),
                    "result_fact_count": int(result["result_fact_count"]),
                    "rules_fired": int(result["rules_fired"]),
                    "expected_live_outcome": result.get("expected_live_outcome"),
                    "live_result_fact_count": int(result.get("live_result_fact_count", 0)),
                    "positive_case_count": int(result["positive_case_count"]),
                    "negative_case_count": int(result["negative_case_count"]),
                    **({"result_evidence_version": 2, "derived_fact_scope": "SOURCE_BACKED"} if separated_evidence else {}),
                }
            )
        return {
            "requirement": requirement or "REQUIRED",
            "not_applicable_reason": None,
            "declared": len(capabilities),
            "validated": len(validated),
            "capabilities": validated,
        }

    def _validate_s6_production_coverage(
        self,
        project_dir: Path,
        semantica: dict[str, Any],
    ) -> dict[str, Any]:
        """证明 S6 覆盖当前完整来源，而不是代表性样本。"""

        state = self._read_state(project_dir)
        intake_mode = str(state.get("intake_mode") or "").upper()
        expected_database_rows = 0
        if intake_mode != "DOCUMENT_ONLY":
            profile_path = project_dir / "01-data-understanding/data-profile.json"
            if not profile_path.is_file():
                raise WorkflowGateError(
                    "G-S6-PRODUCTION-COVERAGE",
                    "全量验证缺少 S1 数据画像。",
                )
            profile = self._read_json(profile_path)
            expected_database_rows = int(profile.get("total_rows", -1))
            if expected_database_rows < 0:
                raise WorkflowGateError(
                    "G-S6-PRODUCTION-COVERAGE",
                    "S1 数据画像缺少可对账的 total_rows。",
                )

        expected_document_evidence = 0
        if intake_mode != "DATABASE_ONLY":
            s0_gate_path = project_dir / "00-document-evidence/gate-results.json"
            if not s0_gate_path.is_file():
                raise WorkflowGateError(
                    "G-S6-PRODUCTION-COVERAGE",
                    "全量验证缺少 S0 文档证据门禁结果。",
                )
            s0_gate = self._read_json(s0_gate_path)
            expected_document_evidence = int(
                (s0_gate.get("metrics") or {}).get("evidence_count", -1)
            )
            if expected_document_evidence < 1:
                raise WorkflowGateError(
                    "G-S6-PRODUCTION-COVERAGE",
                    "S0 没有可用于生产验证的文档证据单元。",
                )

        coverage = semantica.get("production_coverage")
        if not isinstance(coverage, dict):
            raise WorkflowGateError(
                "G-S6-PRODUCTION-COVERAGE",
                "Semantica 报告缺少 production_coverage 全量覆盖回执。",
            )
        if (
            coverage.get("status") != "VERIFIED"
            or str(coverage.get("validation_scope") or "").upper() != "FULL_SOURCE_VALIDATION"
        ):
            raise WorkflowGateError(
                "G-S6-PRODUCTION-COVERAGE",
                "生产覆盖回执必须声明 VERIFIED / FULL_SOURCE_VALIDATION。",
            )

        populations = coverage.get("populations")
        if not isinstance(populations, dict):
            raise WorkflowGateError(
                "G-S6-PRODUCTION-COVERAGE",
                "生产覆盖回执缺少 populations 对账明细。",
            )
        expected_populations = {
            "database_rows": expected_database_rows,
            "document_evidence_units": expected_document_evidence,
        }
        normalized_populations: dict[str, dict[str, int]] = {}
        for population_name, expected_count in expected_populations.items():
            population = populations.get(population_name)
            if not isinstance(population, dict):
                raise WorkflowGateError(
                    "G-S6-PRODUCTION-COVERAGE",
                    f"生产覆盖回执缺少 {population_name} 对账。",
                )
            declared_expected = int(population.get("expected", -1))
            evaluated = int(population.get("evaluated", -1))
            failed = int(population.get("failed", -1))
            if declared_expected != expected_count or evaluated != expected_count or failed != 0:
                raise WorkflowGateError(
                    "G-S6-PRODUCTION-COVERAGE",
                    f"{population_name} 未完成全量闭合："
                    f"S0/S1={expected_count}，回执 expected={declared_expected}、"
                    f"evaluated={evaluated}、failed={failed}。",
                )
            normalized_populations[population_name] = {
                "expected": declared_expected,
                "evaluated": evaluated,
                "failed": failed,
            }

        submitted_fingerprints = coverage.get("source_stage_fingerprints")
        if not isinstance(submitted_fingerprints, dict):
            raise WorkflowGateError(
                "G-S6-PRODUCTION-COVERAGE",
                "生产覆盖回执缺少 S0-S5 来源指纹。",
            )
        expected_fingerprints = {
            stage: str((state.get("stage_fingerprints") or {}).get(stage, {}).get("output") or "")
            for stage in ("S0", "S1", "S2", "S3", "S4", "S5")
        }
        if any(not value for value in expected_fingerprints.values()):
            raise WorkflowGateError(
                "G-S6-PRODUCTION-COVERAGE",
                "当前工程缺少完整 S0-S5 正式阶段指纹。",
            )
        if {
            str(key): str(value) for key, value in submitted_fingerprints.items()
        } != expected_fingerprints:
            raise WorkflowGateError(
                "G-S6-PRODUCTION-COVERAGE",
                "生产覆盖回执绑定的 S0-S5 指纹不是当前正式版本。",
            )

        return {
            "status": "VERIFIED",
            "validation_scope": "FULL_SOURCE_VALIDATION",
            "populations": normalized_populations,
            "source_stage_fingerprints": expected_fingerprints,
            "receipt_id": str(coverage.get("receipt_id") or "").strip() or None,
        }

    def _validate_s0(self, payload: dict[str, Any]) -> dict[str, Any]:
        documents = payload["documents"]
        quality = payload["quality_report"]
        evidence_index = payload["evidence_index"]
        trace = payload["processing_trace"]
        if not documents:
            raise WorkflowGateError("G-S0-DOCUMENT-IDENTITY", "至少需要登记一份资料。")

        document_ids: set[str] = set()
        total_units = 0
        total_pages = 0
        total_sheets = 0
        for index, item in enumerate(documents, start=1):
            document_id = str(item.get("document_id") or "").strip()
            if not DOCUMENT_ID_PATTERN.fullmatch(document_id) or document_id in document_ids:
                raise WorkflowGateError(
                    "G-S0-DOCUMENT-IDENTITY",
                    f"第 {index} 份资料的 document_id 缺失、重复或格式不安全。",
                )
            document_ids.add(document_id)
            if not str(item.get("source_name") or "").strip():
                raise WorkflowGateError(
                    "G-S0-DOCUMENT-IDENTITY",
                    f"资料 {document_id} 缺少 source_name。",
                )
            if not SHA256_PATTERN.fullmatch(str(item.get("source_sha256") or "")):
                raise WorkflowGateError(
                    "G-S0-DOCUMENT-IDENTITY",
                    f"资料 {document_id} 缺少有效的 SHA-256。",
                )
            source_type = str(item.get("source_type") or "DOCUMENT").strip().upper()
            page_count = int(item.get("page_count") or 0)
            sheet_count = int(item.get("sheet_count") or 0)
            content_unit_count = int(item.get("content_unit_count") or 0)
            unit_count = content_unit_count or sheet_count or page_count
            if unit_count < 1:
                raise WorkflowGateError(
                    "G-S0-DOCUMENT-IDENTITY",
                    f"资料 {document_id} 缺少有效的内容规模；PDF 请提供 page_count，Word 可提供 page_count 或 content_unit_count，Excel 请提供 sheet_count，其他资料可提供 content_unit_count。",
                )
            if source_type in {"EXCEL", "XLS", "XLSX", "CSV"} and not (
                sheet_count or content_unit_count
            ):
                raise WorkflowGateError(
                    "G-S0-DOCUMENT-IDENTITY",
                    f"表格资料 {document_id} 请使用 sheet_count 或 content_unit_count 记录内容规模，不能把工作表当成页码。",
                )
            total_units += unit_count
            total_pages += page_count
            total_sheets += sheet_count
            markdown = str(item.get("structured_markdown") or "").strip()
            if not markdown or len(markdown) < 20:
                raise WorkflowGateError(
                    "G-S0-STRUCTURED-MARKDOWN",
                    f"资料 {document_id} 尚未形成有效的结构化 Markdown。",
                )

        evidence_ids: set[str] = set()
        evidence_documents: set[str] = set()
        for index, item in enumerate(evidence_index, start=1):
            evidence_id = str(item.get("evidence_id") or "").strip()
            document_id = str(item.get("document_id") or "").strip()
            locator = item.get("source_page") or item.get("source_locator")
            if not evidence_id or evidence_id in evidence_ids:
                raise WorkflowGateError(
                    "G-S0-EVIDENCE-TRACE",
                    f"第 {index} 条证据的 evidence_id 缺失或重复。",
                )
            if document_id not in document_ids or locator in (None, ""):
                raise WorkflowGateError(
                    "G-S0-EVIDENCE-TRACE",
                    f"证据 {evidence_id} 无法追溯到已登记资料和原始页码/定位符。",
                )
            if not str(item.get("markdown_section") or "").strip():
                raise WorkflowGateError(
                    "G-S0-EVIDENCE-TRACE",
                    f"证据 {evidence_id} 缺少 markdown_section。",
                )
            evidence_ids.add(evidence_id)
            evidence_documents.add(document_id)
        missing_evidence = sorted(document_ids - evidence_documents)
        if missing_evidence:
            raise WorkflowGateError(
                "G-S0-EVIDENCE-TRACE",
                f"以下资料没有任何可追溯证据：{', '.join(missing_evidence)}",
            )

        if quality.get("status") != "PASSED":
            raise WorkflowGateError("G-S0-QUALITY", "资料接入质量报告尚未通过。")
        processed_units = int(quality.get("processed_units") or quality.get("processed_pages") or 0)
        failed_units = int(quality.get("failed_units") or quality.get("failed_pages") or 0)
        unreviewed = int(
            quality.get("unreviewed_low_confidence_units")
            or quality.get("unreviewed_low_confidence_pages")
            or 0
        )
        if processed_units != total_units or failed_units or unreviewed:
            raise WorkflowGateError(
                "G-S0-QUALITY",
                "内容单元未全部处理，或仍有处理失败/未经复核的低置信度内容。",
            )
        if not str(trace.get("run_id") or "").strip() or not trace.get("tool_calls"):
            raise WorkflowGateError(
                "G-S0-TOOL-TRACE",
                "processing_trace 必须包含 run_id 和真实 tool_calls。",
            )

        return {
            "document_count": len(documents),
            "content_unit_count": total_units,
            "page_count": total_pages,
            "sheet_count": total_sheets,
            "evidence_count": len(evidence_index),
            "low_confidence_units": int(
                quality.get("low_confidence_units") or quality.get("low_confidence_pages") or 0
            ),
            "reviewed_low_confidence_units": int(
                quality.get("reviewed_low_confidence_units")
                or quality.get("reviewed_low_confidence_pages")
                or 0
            ),
        }

    def _validate_s1(self, payload: dict[str, Any], *, project_id: str) -> None:
        inventory = payload["datasource_inventory"]
        schema = payload["schema_snapshot"]
        profile = payload["data_profile"]
        relations = payload["relation_candidates"]
        evidence = payload["evidence_sql"]
        if not isinstance(inventory, dict) or not inventory:
            raise WorkflowGateError("G-S1-REQUIRED", "数据源清单必须是非空对象。")
        if not isinstance(schema, dict) or not schema:
            raise WorkflowGateError("G-S1-REQUIRED", "数据源清单和 Schema 快照不能为空。")
        if not isinstance(profile, dict):
            raise WorkflowGateError("G-S1-PROFILE", "数据画像必须是对象。")
        if int(inventory.get("datasource_count") or 0) > 1:
            self._validate_multi_source_s1(
                inventory=inventory,
                profile=profile,
                project_id=project_id,
            )
        tables = schema.get("tables") or []
        table_columns = schema.get("table_columns") or {}
        if not isinstance(tables, list) or not isinstance(table_columns, dict):
            raise WorkflowGateError(
                "G-S1-SCHEMA",
                "Schema 快照必须包含 tables 数组或 table_columns 对象。",
            )
        if not tables and not table_columns:
            raise WorkflowGateError("G-S1-SCHEMA", "Schema 快照至少需要一张数据表。")
        if not isinstance(relations, list) or any(not isinstance(item, dict) for item in relations):
            raise WorkflowGateError("G-S1-SCHEMA", "关系候选必须是对象数组。")
        if not isinstance(evidence, list) or not evidence:
            raise WorkflowGateError("G-S1-EVIDENCE", "至少需要一条只读证据 SQL。")
        evidence_ids: set[str] = set()
        for index, item in enumerate(evidence, start=1):
            if not isinstance(item, dict):
                raise WorkflowGateError(
                    "G-S1-EVIDENCE",
                    f"第 {index} 条证据必须是对象。",
                )
            evidence_id = str(item.get("id") or "").strip()
            query = str(item.get("sql") or item.get("query") or "").strip()
            if not evidence_id or not query:
                raise WorkflowGateError(
                    "G-S1-EVIDENCE",
                    f"第 {index} 条证据必须包含 id 和 sql/query。",
                )
            if evidence_id in evidence_ids:
                raise WorkflowGateError("G-S1-EVIDENCE", f"证据编号重复：{evidence_id}")
            evidence_ids.add(evidence_id)
            sql = re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", "", query, flags=re.DOTALL)
            if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE) or WRITE_SQL_PATTERN.search(
                sql
            ):
                raise WorkflowGateError(
                    "G-S1-READONLY",
                    f"证据 {evidence_id} 不是只读 SELECT/WITH 查询。",
                )

        # Production projects are fail-closed: an agent-authored narrative or a
        # syntactically read-only query is not evidence that the source was
        # actually profiled.  The importer/Chat2DB handoff must close the source,
        # schema, row-count and execution-receipt loop before S2 can start.
        if str(profile.get("profile_mode") or "") != "FULL_IMPORT_WITH_EXACT_COUNTS":
            raise WorkflowGateError(
                "G-S1-PRODUCTION-PROFILE",
                "生产工程必须使用 FULL_IMPORT_WITH_EXACT_COUNTS 精确画像，禁止抽样或仅描述性画像。",
            )
        scope = inventory.get("business_tables_scope") or inventory.get("tables_in_scope")
        if (
            not isinstance(scope, list)
            or not scope
            or any(not str(value).strip() for value in scope)
        ):
            raise WorkflowGateError(
                "G-S1-RECONCILIATION",
                "生产工程必须声明非空且精确的 business_tables_scope。",
            )
        normalized_scope = [str(value).strip() for value in scope]
        if len(normalized_scope) != len(set(normalized_scope)):
            raise WorkflowGateError("G-S1-RECONCILIATION", "business_tables_scope 存在重复表。")
        schema_names = [str(item.get("name") or item.get("table") or "").strip() for item in tables]
        if set(schema_names) != set(normalized_scope):
            raise WorkflowGateError(
                "G-S1-RECONCILIATION",
                "Schema 表集合与 business_tables_scope 不一致。",
            )

        profile_tables = profile.get("tables")
        if not isinstance(profile_tables, list) or not profile_tables:
            raise WorkflowGateError(
                "G-S1-PRODUCTION-PROFILE",
                "精确画像必须逐表提供 tables 与 row_count。",
            )
        profiled_counts: dict[str, int] = {}
        for index, item in enumerate(profile_tables, start=1):
            if not isinstance(item, dict):
                raise WorkflowGateError(
                    "G-S1-PRODUCTION-PROFILE",
                    f"第 {index} 个表画像必须是对象。",
                )
            table_name = str(item.get("table") or item.get("name") or "").strip()
            row_count = item.get("row_count")
            if (
                not table_name
                or table_name in profiled_counts
                or isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count < 0
            ):
                raise WorkflowGateError(
                    "G-S1-PRODUCTION-PROFILE",
                    f"第 {index} 个表画像缺少唯一表名或非负整数 row_count。",
                )
            profiled_counts[table_name] = row_count
        if set(profiled_counts) != set(normalized_scope):
            raise WorkflowGateError(
                "G-S1-RECONCILIATION",
                "逐表画像集合与 business_tables_scope 不一致。",
            )
        declared_table_count = profile.get("table_count")
        declared_total_rows = profile.get("total_rows")
        declared_empty_count = profile.get("empty_table_count")
        expected_table_count = len(normalized_scope)
        expected_total_rows = sum(profiled_counts.values())
        expected_empty_count = sum(value == 0 for value in profiled_counts.values())
        if (
            declared_table_count != expected_table_count
            or declared_total_rows != expected_total_rows
            or declared_empty_count != expected_empty_count
        ):
            raise WorkflowGateError(
                "G-S1-RECONCILIATION",
                "table_count、total_rows 或 empty_table_count 与逐表精确画像不闭合。",
            )

        datasets = inventory.get("datasets")
        if not isinstance(datasets, list) or not datasets:
            raise WorkflowGateError(
                "G-S1-SOURCE-LINEAGE",
                "生产工程必须绑定结构化导入数据集及源文件版本。",
            )
        dataset_rows = 0
        for index, dataset in enumerate(datasets, start=1):
            if not isinstance(dataset, dict):
                raise WorkflowGateError(
                    "G-S1-SOURCE-LINEAGE", f"第 {index} 个数据集回执必须是对象。"
                )
            if (
                str(dataset.get("project_id") or "") != project_id
                or not str(dataset.get("dataset_id") or "").strip()
                or not SHA256_PATTERN.fullmatch(str(dataset.get("source_sha256") or ""))
                or str(dataset.get("status") or "") != "READY"
                or isinstance(dataset.get("row_count"), bool)
                or not isinstance(dataset.get("row_count"), int)
                or int(dataset["row_count"]) < 0
            ):
                raise WorkflowGateError(
                    "G-S1-SOURCE-LINEAGE",
                    f"第 {index} 个数据集未绑定当前 project_id、READY 状态、SHA-256 或精确行数。",
                )
            dataset_rows += int(dataset["row_count"])
        if dataset_rows != expected_total_rows:
            raise WorkflowGateError(
                "G-S1-SOURCE-LINEAGE",
                "数据集回执行数与逐表画像总行数不一致。",
            )
        self._validate_s1_catalog_readback(
            project_id=project_id,
            datasets=datasets,
        )

        evidence_by_table: set[str] = set()
        for item in evidence:
            source_tables = item.get("source_tables") or []
            if isinstance(source_tables, str):
                source_tables = [source_tables]
            evidence_by_table.update(
                str(value).strip() for value in source_tables if str(value).strip()
            )
            expected_row_count = item.get("expected_row_count")
            actual_row_count = item.get("actual_row_count")
            receipt_issues = []
            if item.get("status") != "PASSED":
                receipt_issues.append("status 必须为 PASSED")
            if item.get("executed_via") not in {
                "CHAT2DB_MCP", "ORION_STRUCTURED_DATA_PIPELINE", "ORION_SNAPSHOT_HUB"
            }:
                receipt_issues.append("executed_via 不是受支持的执行器")
            if not str(item.get("executed_at") or "").strip():
                receipt_issues.append("executed_at 缺失")
            if not SHA256_PATTERN.fullmatch(str(item.get("result_sha256") or "")):
                receipt_issues.append("result_sha256 必须是 sha256: 加 64 位十六进制摘要")
            if type(expected_row_count) is not int or expected_row_count < 0:
                receipt_issues.append("expected_row_count 必须是非负整数")
            if type(actual_row_count) is not int or actual_row_count < 0:
                receipt_issues.append("actual_row_count 必须是非负整数")
            if actual_row_count != expected_row_count:
                receipt_issues.append("actual_row_count 与 expected_row_count 不一致（COUNT 的结果值不是返回行数）")
            if receipt_issues:
                raise WorkflowGateError(
                    "G-S1-EVIDENCE-EXECUTION",
                    f"evidence_sql[{item.get('id')}]：" + "；".join(receipt_issues),
                )
        if not set(normalized_scope).issubset(evidence_by_table):
            raise WorkflowGateError(
                "G-S1-EVIDENCE-EXECUTION",
                "每张范围表都必须至少有一条带真实回执的只读证据 SQL。",
            )

    def _validate_s1_catalog_readback(
        self,
        *,
        project_id: str,
        datasets: list[dict[str, Any]],
    ) -> None:
        """Verify that caller-supplied S1 receipts exist in the promoted catalog.

        Chat2DB profiling proves that a source can be queried; it does not prove
        that ORION imported an immutable dataset or snapshot. Production
        workflow instances therefore cross-read the data catalog before S1 may
        pass. Local/unit workflow instances can opt in explicitly.
        """

        required = self._metadata_required or os.getenv(
            "ORION_S1_CATALOG_READBACK_REQUIRED", "false"
        ).lower() in {"1", "true", "yes"}
        if not required:
            return
        reader_url = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
        if not reader_url:
            raise WorkflowGateError(
                "G-S1-CATALOG-READBACK",
                "生产 S1 缺少 ORION_SOURCE_DATA_READER_URL，无法回读正式数据目录。",
            )
        dataset_ids = [str(item["dataset_id"]) for item in datasets]
        try:
            records = self._read_s1_catalog_records(reader_url, dataset_ids)
        except Exception as exc:
            raise WorkflowGateError(
                "G-S1-CATALOG-READBACK",
                f"正式数据目录只读回读失败（{type(exc).__name__}）。",
            ) from exc
        for dataset in datasets:
            dataset_id = str(dataset["dataset_id"])
            record = records.get(dataset_id)
            if record is None:
                raise WorkflowGateError(
                    "G-S1-CATALOG-READBACK",
                    f"数据集 {dataset_id} 未在 orion_catalog 中找到正式导入记录。",
                )
            if (
                record["project_id"] != project_id
                or record["source_sha256"] != str(dataset["source_sha256"])
                or record["row_count"] != int(dataset["row_count"])
                or not record["ready"]
                or not record["promoted"]
                or not record["production_evidence"]
            ):
                raise WorkflowGateError(
                    "G-S1-CATALOG-READBACK",
                    f"数据集 {dataset_id} 的项目、SHA-256、行数、READY/晋升状态或生产证据标记与目录不一致。",
                )

    @staticmethod
    def _read_s1_catalog_records(
        database_url: str,
        dataset_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Read file-import and Snapshot Hub catalogs in one read-only transaction."""

        normalized_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        records: dict[str, dict[str, Any]] = {}
        with psycopg.connect(normalized_url) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT d.dataset_id, d.project_id, d.source_sha256, d.row_count,
                       d.status = 'READY' AS ready,
                       EXISTS (
                           SELECT 1 FROM orion_catalog.current_datasets c
                           WHERE c.dataset_id=d.dataset_id AND c.project_id=d.project_id
                       ) AS promoted
                FROM orion_catalog.datasets d
                WHERE d.dataset_id = ANY(%s)
                """,
                (dataset_ids,),
            )
            for dataset_id, owner, source_sha256, row_count, ready, promoted in cursor.fetchall():
                records[str(dataset_id)] = {
                    "project_id": str(owner),
                    "source_sha256": str(source_sha256),
                    "row_count": int(row_count),
                    "ready": bool(ready),
                    "promoted": bool(promoted),
                    "production_evidence": True,
                }
            missing_ids = [value for value in dataset_ids if value not in records]
            if missing_ids:
                cursor.execute(
                    """
                    SELECT s.dataset_id, s.project_id, s.source_sha256,
                           s.snapshot_complete, s.production_evidence, s.manifest,
                           EXISTS (
                               SELECT 1
                               FROM orion_catalog.current_snapshot_sets c
                               JOIN orion_catalog.snapshot_set_members m
                                 ON m.snapshot_set_id=c.snapshot_set_id
                               WHERE c.project_id=s.project_id
                                 AND m.dataset_id=s.dataset_id
                           ) AS promoted
                    FROM orion_catalog.source_snapshots s
                    WHERE s.dataset_id = ANY(%s)
                    """,
                    (missing_ids,),
                )
                for (
                    dataset_id,
                    owner,
                    source_sha256,
                    snapshot_complete,
                    production_evidence,
                    manifest,
                    promoted,
                ) in cursor.fetchall():
                    tables = (manifest or {}).get("tables") or []
                    records[str(dataset_id)] = {
                        "project_id": str(owner),
                        "source_sha256": str(source_sha256),
                        "row_count": sum(
                            int(item.get("snapshot_row_count") or 0)
                            for item in tables
                            if isinstance(item, dict)
                        ),
                        "ready": bool(snapshot_complete),
                        "promoted": bool(promoted),
                        "production_evidence": bool(production_evidence),
                    }
        return records

    @staticmethod
    def _validate_multi_source_s1(
        *,
        inventory: dict[str, Any],
        profile: dict[str, Any],
        project_id: str,
    ) -> None:
        from services.structured_data.multi_source import (
            CrossSourceSnapshotSet,
            MultiSourceContractError,
            SourceBinding,
            SourceProfileReceipt,
            build_cross_source_snapshot_set,
        )

        raw_bindings = inventory.get("source_bindings")
        count = inventory.get("datasource_count")
        if (
            not isinstance(raw_bindings, list)
            or len(raw_bindings) < 2
            or count != len(raw_bindings)
        ):
            raise WorkflowGateError(
                "G-S1-MULTI-SOURCE-INVENTORY",
                "多源工程必须提供至少两个且数量闭合的 source_bindings。",
            )
        try:
            bindings = [SourceBinding.model_validate(item) for item in raw_bindings]
        except ValueError as exc:
            message = str(exc)
            gate = (
                "G-S1-SOURCE-READONLY" if "G-S1-SOURCE-READONLY" in message else "G-S1-SOURCE-SCOPE"
            )
            raise WorkflowGateError(gate, message) from exc
        if any(item.project_id != project_id for item in bindings):
            raise WorkflowGateError("G-S1-SOURCE-SCOPE", "source binding 不属于当前 project_id。")
        if len({item.source_id for item in bindings}) != len(bindings):
            raise WorkflowGateError("G-S1-MULTI-SOURCE-INVENTORY", "source_id 重复。")
        raw_profiles = profile.get("source_profiles")
        if not isinstance(raw_profiles, list) or len(raw_profiles) != len(bindings):
            raise WorkflowGateError(
                "G-S1-SNAPSHOT-RECONCILIATION",
                "每个 source binding 必须有一份精确画像回执。",
            )
        try:
            profiles = [SourceProfileReceipt.model_validate(item) for item in raw_profiles]
        except ValueError as exc:
            raise WorkflowGateError("G-S1-SNAPSHOT-RECONCILIATION", str(exc)) from exc
        if {item.source_id for item in profiles} != {item.source_id for item in bindings}:
            raise WorkflowGateError(
                "G-S1-SNAPSHOT-RECONCILIATION",
                "source profile 集合与 source binding 集合不一致。",
            )
        try:
            snapshot_set = CrossSourceSnapshotSet.model_validate(
                inventory.get("cross_source_snapshot_set")
            )
        except ValueError as exc:
            raise WorkflowGateError("G-S1-SNAPSHOT-COMPLETE", str(exc)) from exc
        if snapshot_set.project_id != project_id:
            raise WorkflowGateError("G-S1-SOURCE-SCOPE", "snapshot set 不属于当前 project_id。")
        snapshot_source_ids = {item.source_id for item in snapshot_set.source_snapshots}
        if snapshot_source_ids != {item.source_id for item in bindings}:
            raise WorkflowGateError(
                "G-S1-SNAPSHOT-COMPLETE",
                "snapshot set 未覆盖全部活动数据源。",
            )
        try:
            rebuilt = build_cross_source_snapshot_set(
                project_id=project_id,
                bindings=bindings,
                snapshots=snapshot_set.source_snapshots,
            )
        except MultiSourceContractError as exc:
            message = str(exc)
            gate = (
                message.split(":", 1)[0] if message.startswith("G-") else ("G-S1-SNAPSHOT-COMPLETE")
            )
            raise WorkflowGateError(gate, message) from exc
        if (
            rebuilt.snapshot_set_id != snapshot_set.snapshot_set_id
            or rebuilt.manifest_sha256 != snapshot_set.manifest_sha256
        ):
            raise WorkflowGateError(
                "G-S1-SNAPSHOT-RECONCILIATION",
                "snapshot set 指纹与逐源 manifest 不一致。",
            )
        if not snapshot_set.production_evidence:
            raise WorkflowGateError(
                "G-S7-PRODUCTION-EVIDENCE",
                "TEST_ONLY 或未证明真实性的 snapshot set 不能推进生产工作流。",
            )

    def _validate_s2(
        self,
        payload: dict[str, Any],
        *,
        project_dir: Path,
        intake_mode: str,
    ) -> None:
        candidates = payload["ontology_candidates"]
        if (project_dir / "workflow-state.json").is_file() and business_contract.enabled(self._read_state(project_dir)):
            try:
                business_contract.validate_candidates(candidates)
            except ValueError as exc:
                raise WorkflowGateError("G-S2-BUSINESS-MODELING", str(exc)) from exc
        if not candidates:
            raise WorkflowGateError("G-S2-REQUIRED", "本体候选不能为空。")
        ids: set[str] = set()
        for index, item in enumerate(candidates, start=1):
            candidate_id = str(item.get("id") or "").strip()
            if not candidate_id or candidate_id in ids:
                raise WorkflowGateError("G-S2-UNIQUE", f"第 {index} 条候选 id 缺失或重复。")
            ids.add(candidate_id)
            if item.get("status") not in SEMANTIC_STATUSES:
                raise WorkflowGateError(
                    "G-S2-STATUS",
                    f"候选 {candidate_id} 必须标注数据库事实、资料证据事实、AI推测或待人工确认。",
                )
            if not item.get("source_refs"):
                raise WorkflowGateError(
                    "G-S2-EVIDENCE",
                    f"候选 {candidate_id} 缺少 source_refs。",
                )
            if not item.get("name") or not item.get("kind"):
                raise WorkflowGateError(
                    "G-S2-REQUIRED",
                    f"候选 {candidate_id} 缺少 name 或 kind。",
                )
        observed_statuses = {str(item.get("status") or "") for item in candidates}
        if intake_mode == "HYBRID" and not {
            "DATABASE_FACT",
            "DOCUMENT_EVIDENCE",
        }.issubset(observed_statuses):
            raise WorkflowGateError(
                "G-S2-HYBRID-EVIDENCE",
                "HYBRID 工程必须同时包含 DATABASE_FACT 与 DOCUMENT_EVIDENCE，禁止只使用单侧来源冒充融合。",
            )

        intake_path = project_dir / "00-document-evidence/cq-intake.json"
        intake_questions = (
            self._read_json(intake_path).get("questions") or [] if intake_path.is_file() else []
        )
        intake_question_ids = {
            str(item.get("id") or "") for item in intake_questions if isinstance(item, dict)
        }
        reasoning_question_ids = {
            str(item.get("id") or "")
            for item in intake_questions
            if isinstance(item, dict)
            and _requires_reasoning(
                str(item.get("question") or ""),
                str(item.get("expected") or ""),
            )
        }
        reasoning_required = bool(reasoning_question_ids)
        rules = payload.get("business_rule_candidates") or []
        if reasoning_required and not rules:
            raise WorkflowGateError(
                "G-S2-RULE-COVERAGE",
                "S0 业务问题包含判定或推理诉求，S2 必须形成可执行、可测试的业务规则候选。",
            )

        rule_ids: set[str] = set()
        covered_question_ids: set[str] = set()
        for index, item in enumerate(rules, start=1):
            from services.ontology_engineering.rule_condition_binding import rule_condition_issues
            condition_issues = rule_condition_issues(item, payload.get("ontology_candidates") or [])
            if condition_issues:
                raise WorkflowGateError("G-S2-RULE-CONDITIONS", condition_issues[0]["message"])
            rule_id = str(item.get("id") or "").strip()
            if not rule_id or rule_id in rule_ids:
                raise WorkflowGateError(
                    "G-S2-RULE",
                    f"第 {index} 条业务规则候选 id 缺失或重复。",
                )
            if item.get("status") not in SEMANTIC_STATUSES:
                raise WorkflowGateError(
                    "G-S2-RULE",
                    f"业务规则候选 {rule_id} 必须标注事实等级。",
                )
            if (
                not str(item.get("name") or "").strip()
                or not str(item.get("rule_type") or "").strip()
            ):
                raise WorkflowGateError(
                    "G-S2-RULE",
                    f"业务规则候选 {rule_id} 缺少 name 或 rule_type。",
                )
            if not str(item.get("description") or "").strip() or not item.get("source_refs"):
                raise WorkflowGateError(
                    "G-S2-RULE",
                    f"业务规则候选 {rule_id} 缺少 description 或 source_refs。",
                )
            business_question_ids = item.get("business_question_ids")
            if intake_question_ids and (
                not isinstance(business_question_ids, list)
                or not business_question_ids
                or any(
                    not str(value).strip() or str(value).strip() not in intake_question_ids
                    for value in business_question_ids
                )
            ):
                raise WorkflowGateError(
                    "G-S2-RULE-COVERAGE",
                    f"业务规则候选 {rule_id} 必须绑定当前 S0 的 business_question_ids。",
                )
            covered_question_ids.update(str(value).strip() for value in business_question_ids or [])
            expression = " ".join(str(item.get("formal_expression") or "").split())
            premises = item.get("premise_predicates")
            conclusion = str(item.get("conclusion_predicate") or "").strip()
            cases = item.get("test_cases")
            if (
                not expression.upper().startswith("IF ")
                or " THEN " not in expression.upper()
                or not isinstance(premises, list)
                or not premises
                or any(not str(value).strip() for value in premises)
                or not conclusion
                or not isinstance(cases, list)
            ):
                raise WorkflowGateError(
                    "G-S2-RULE-CONTRACT",
                    f"业务规则候选 {rule_id} 必须包含 formal_expression、premise_predicates、conclusion_predicate 和 test_cases。",
                )
            expression_parts = re.split(r"\s+THEN\s+", expression, maxsplit=1, flags=re.I)
            contract_issues = self._s2_rule_contract_issues(item, f"business_rule_candidates[{index - 1}]")
            body_issues = [issue for issue in contract_issues if ".test_cases[" not in issue["path"]]
            if body_issues:
                first = body_issues[0]
                raise WorkflowGateError(first["gate"], f"{first['path']}：{first['message']}")
            predicate_pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(")
            fact_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\([^()]*\)$")
            premise_expression = re.sub(r"^IF\s+", "", expression_parts[0], flags=re.I)
            expression_premises = predicate_pattern.findall(premise_expression)
            negated_predicates = set(
                re.findall(
                    r"\bNOT\s+([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(",
                    premise_expression,
                    flags=re.I,
                )
            )
            positive_predicates = set(expression_premises) - negated_predicates
            expression_conclusions = predicate_pattern.findall(expression_parts[1])
            declared_premises = [str(value).strip() for value in premises]
            if (
                not expression_premises
                or len(expression_conclusions) != 1
                or set(expression_premises) != set(declared_premises)
                or expression_conclusions[0] != conclusion
            ):
                raise WorkflowGateError(
                    "G-S2-RULE-SEMANTICS",
                    f"业务规则候选 {rule_id} 的 formal_expression 与前提/结论谓词声明不一致；生产规则仅接受单结论 Horn 规则。",
                )
            if negated_predicates:
                required_capabilities = {
                    str(value).strip()
                    for value in item.get("required_capabilities") or []
                    if str(value).strip()
                }
                if (
                    CLOSED_WORLD_SET_DIFFERENCE_V1 not in required_capabilities
                    or not supports_reasoning_capability(CLOSED_WORLD_SET_DIFFERENCE_V1)
                ):
                    raise WorkflowGateError(
                        "G-S2-UNSUPPORTED-OPERATOR",
                        f"业务规则候选 {rule_id} 需要 {CLOSED_WORLD_SET_DIFFERENCE_V1}，但规则未声明或执行器未发现该能力。",
                    )
                if str(item.get("rule_type") or "").upper() != "CLOSED_WORLD_SET_DIFFERENCE":
                    raise WorkflowGateError(
                        "G-S2-UNSUPPORTED-OPERATOR",
                        f"业务规则候选 {rule_id} 使用 NOT 时必须声明 rule_type=CLOSED_WORLD_SET_DIFFERENCE。",
                    )
                closed_world_inputs = item.get("closed_world_inputs")
                if not isinstance(closed_world_inputs, list) or not closed_world_inputs:
                    raise WorkflowGateError(
                        "G-S2-INPUT-INCOMPLETE",
                        f"业务规则候选 {rule_id} 必须为否定谓词声明完整来源快照；缺少 closed_world_inputs。",
                    )
                declared_closed_world: set[str] = set()
                for closed_world_input in closed_world_inputs:
                    predicate = str(closed_world_input.get("predicate") or "").strip()
                    problems = closed_world_diagnostics.submitted_snapshot_problems(
                        closed_world_input
                    )
                    binding_problems = [
                        problem
                        for problem in problems
                        if problem.startswith(("dataset_id", "source_refs", "source_sha256"))
                    ]
                    if binding_problems:
                        raise WorkflowGateError(
                            "G-S2-SOURCE-NOT-BOUND",
                            f"业务规则候选 {rule_id} 的闭世界输入 {predicate or 'UNKNOWN'} 未绑定来源："
                            + closed_world_diagnostics.describe(binding_problems),
                        )
                    if (
                        str(closed_world_input.get("dataset_type") or "").upper()
                        != "PRODUCTION_EVIDENCE"
                        or closed_world_input.get("production_evidence") is not True
                    ):
                        raise WorkflowGateError(
                            "G-S2-TEST-EVIDENCE-FORBIDDEN",
                            f"业务规则候选 {rule_id} 的生产门禁禁止使用 TEST_ONLY 或未确认的闭世界输入。",
                        )
                    remaining = [
                        problem
                        for problem in problems
                        if not problem.startswith(
                            ("dataset_type", "production_evidence")
                        )
                    ]
                    if remaining:
                        raise WorkflowGateError(
                            "G-S2-INPUT-INCOMPLETE",
                            f"业务规则候选 {rule_id} 的闭世界输入 {predicate or 'UNKNOWN'} 尚未满足 "
                            f"{len(remaining)} 项要求："
                            + closed_world_diagnostics.describe(remaining),
                        )
                    declared_closed_world.add(predicate)
                if declared_closed_world != negated_predicates:
                    missing = sorted(negated_predicates - declared_closed_world)
                    extra = sorted(declared_closed_world - negated_predicates)
                    raise WorkflowGateError(
                        "G-S2-INPUT-INCOMPLETE",
                        f"业务规则候选 {rule_id} 的闭世界输入谓词必须与 NOT 谓词精确一致。"
                        + (f"缺少输入的 NOT 谓词：{'、'.join(missing)}。" if missing else "")
                        + (f"多余的输入谓词：{'、'.join(extra)}。" if extra else ""),
                    )
                required_set_source = item.get("required_set_source")
                if not isinstance(required_set_source, dict) or (
                    str(required_set_source.get("predicate") or "").strip()
                    not in positive_predicates
                ):
                    raise WorkflowGateError(
                        "G-S2-SOURCE-NOT-BOUND",
                        f"业务规则候选 {rule_id} 必须把 required_set_source.predicate 绑定到本规则的"
                        f"正向前提谓词之一（可选：{'、'.join(sorted(positive_predicates)) or '无'}；"
                        f"当前：{str(required_set_source.get('predicate') or '').strip() or '缺失'}）。",
                    )
                required_problems = closed_world_diagnostics.required_set_problems(
                    required_set_source
                )
                if required_problems:
                    raise WorkflowGateError(
                        "G-S2-SOURCE-NOT-BOUND",
                        f"业务规则候选 {rule_id} 的 required_set_source 尚未满足 "
                        f"{len(required_problems)} 项要求："
                        + closed_world_diagnostics.describe(required_problems),
                    )
                if (
                    str(required_set_source.get("dataset_type") or "").upper()
                    != "PRODUCTION_EVIDENCE"
                    or required_set_source.get("production_evidence") is not True
                ):
                    raise WorkflowGateError(
                        "G-S2-TEST-EVIDENCE-FORBIDDEN",
                        f"业务规则候选 {rule_id} 的生产门禁禁止使用 TEST_ONLY required 集合。",
                    )
            elif item.get("closed_world_inputs") or item.get("required_set_source"):
                raise WorkflowGateError(
                    "G-S2-INPUT-INCOMPLETE",
                    f"业务规则候选 {rule_id} 不含 NOT 前提，不应登记 closed_world_inputs 或 required_set_source；"
                    "只有规则实际消费的否定谓词才需要闭世界来源快照。",
                )
            case_types: set[str] = set()
            for case_index, case in enumerate(cases):
                if (
                    not isinstance(case, dict)
                    or not str(case.get("id") or "").strip()
                    or str(case.get("case_type") or "").upper()
                    not in {"POSITIVE", "NEGATIVE", "BOUNDARY"}
                    or not isinstance(case.get("facts"), list)
                    or not case.get("facts")
                    or str(case.get("expected_outcome") or "").upper() not in {"FIRE", "NO_FIRE"}
                ):
                    raise WorkflowGateError(
                        "G-S2-RULE-CONTRACT",
                        f"业务规则候选 {rule_id} 的测试用例必须包含 id、case_type、facts 和 expected_outcome。",
                    )
                case_type = str(case["case_type"]).upper()
                expected_outcome = str(case["expected_outcome"]).upper()
                case_path = f"business_rule_candidates[{index - 1}].test_cases[{case_index}]."
                case_issues = [issue for issue in contract_issues if issue["path"].startswith(case_path)]
                if case_issues:
                    first = case_issues[0]
                    raise WorkflowGateError(first["gate"], f"{first['path']}：{first['message']}")
                if case_type == "POSITIVE" and expected_outcome != "FIRE":
                    raise WorkflowGateError(
                        "G-S2-RULE-SEMANTICS",
                        f"业务规则候选 {rule_id} 的正例必须期望 FIRE。",
                    )
                if case_type == "NEGATIVE" and expected_outcome != "NO_FIRE":
                    raise WorkflowGateError(
                        "G-S2-RULE-SEMANTICS",
                        f"业务规则候选 {rule_id} 的反例必须期望 NO_FIRE。",
                    )
                fact_predicates: set[str] = set()
                for fact in case["facts"]:
                    matched = fact_pattern.fullmatch(str(fact).strip())
                    if matched is None:
                        raise WorkflowGateError(
                            "G-S2-RULE-SEMANTICS",
                            f"业务规则候选 {rule_id} 的测试事实不是合法谓词表达式：{fact}",
                        )
                    fact_predicates.add(matched.group(1))
                try:
                    would_fire = ground_horn_scenario_fires(expression, case["facts"])
                except RuleScenarioError as error:
                    raise WorkflowGateError("G-S2-RULE-SEMANTICS", str(error)) from error
                if would_fire != (expected_outcome == "FIRE"):
                    raise WorkflowGateError(
                        "G-S2-RULE-SEMANTICS",
                        f"业务规则候选 {rule_id} 的 {case_type} 用例与声明结果不一致。",
                    )
                case_types.add(case_type)
            if case_types != {"POSITIVE", "NEGATIVE", "BOUNDARY"}:
                raise WorkflowGateError(
                    "G-S2-RULE-CONTRACT",
                    f"业务规则候选 {rule_id} 必须同时定义正例、反例和边界用例。",
                )
            if item.get("status") == "AI_INFERENCE" and item.get("review_required") is not True:
                raise WorkflowGateError(
                    "G-S2-RULE-CONTRACT",
                    f"AI 推测规则 {rule_id} 必须显式标记 review_required=true。",
                )
            rule_ids.add(rule_id)
        uncovered_questions = sorted(reasoning_question_ids - covered_question_ids)
        if uncovered_questions:
            raise WorkflowGateError(
                "G-S2-RULE-COVERAGE",
                "S0 中要求判定/推理的业务问题必须在 S2 绑定正式规则，不能留到 S4-S7："
                + ", ".join(uncovered_questions),
            )

    def _validate_s3(
        self,
        payload: dict[str, Any],
        *,
        intake_mode: str = "HYBRID",
        project_dir: Path | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        s3_state = self._read_state(project_dir) if project_dir is not None and (project_dir / "workflow-state.json").is_file() else {}
        if project_dir is not None:
            self._require_unchanged_mapping_for_reuse(
                project_dir, payload["mapping_draft"], draft=True
            )
        mapping = payload.get("mapping_draft")
        mappings = mapping.get("mappings") if isinstance(mapping, dict) else None
        if not isinstance(mappings, list) or not mappings:
            raise WorkflowGateError("G-S3-MAPPING", "mapping_draft.mappings 不能为空。")
        if business_contract.enabled(s3_state):
            try:
                business_contract.validate_mapping(mappings)
            except ValueError as exc:
                raise WorkflowGateError("G-S3-INSTANCE-CONTRACT", str(exc)) from exc
        downstream_issues = self._s3_downstream_contract_issues(project_dir, s3_state, payload)
        if downstream_issues:
            raise WorkflowGateError(
                downstream_issues[0]["gate"],
                "；".join(issue["message"] for issue in downstream_issues),
            )
        mapping_ids: set[str] = set()
        for index, item in enumerate(mappings, start=1):
            mapping_id = str(item.get("id") or "").strip()
            if not mapping_id or mapping_id in mapping_ids:
                raise WorkflowGateError("G-S3-MAPPING", f"第 {index} 条映射 id 缺失或重复。")
            if not item.get("source_refs"):
                raise WorkflowGateError("G-S3-EVIDENCE", f"映射 {mapping_id} 缺少 source_refs。")
            target = str(item.get("target") or "").split(" (")[0].strip()
            mapping_type = str(item.get("mapping_type") or "").strip().upper()
            if mapping_type not in SUPPORTED_MAPPING_TYPES:
                raise WorkflowGateError(
                    "G-S3-MAPPING-TYPE",
                    f"映射 {mapping_id} 的 mapping_type 不受支持：{mapping_type or 'EMPTY'}。",
                )
            if not target:
                raise WorkflowGateError("G-S3-MAPPING", f"映射 {mapping_id} 缺少 target。")
            if intake_mode == "DOCUMENT_ONLY" and mapping_type not in DOCUMENT_MAPPING_TYPES | RULE_CLASS_MAPPING_TYPES:
                raise WorkflowGateError(
                    "G-S3-MAPPING-TYPE",
                    f"纯资料工程的映射 {mapping_id} 不能使用数据库映射类型。",
                )
            if intake_mode == "DATABASE_ONLY" and mapping_type in DOCUMENT_MAPPING_TYPES:
                raise WorkflowGateError(
                    "G-S3-MAPPING-TYPE",
                    f"纯数据库工程的映射 {mapping_id} 不能使用资料映射类型。",
                )
            if mapping_type in {
                "COLUMN_VALUE_TO_OBJECT_PROPERTY",
                "SQL_TO_OBJECT_PROPERTY",
                "CANDIDATE_JOIN_TO_OBJECT_PROPERTY",
                "EVIDENCE_TO_OBJECT_PROPERTY",
            } and (
                not str(item.get("domain") or "").strip()
                or not str(item.get("range") or "").strip()
            ):
                raise WorkflowGateError(
                    "G-S3-MAPPING",
                    f"对象属性映射 {mapping_id} 必须明确 domain 和 range。",
                )
            target_label_zh = str(
                item.get("target_label_zh")
                or item.get("label_zh")
                or ONTOLOGY_NAME_LABELS.get(target)
                or ""
            ).strip()
            if not _contains_chinese(target_label_zh):
                raise WorkflowGateError(
                    "G-S3-CHINESE",
                    f"映射 {mapping_id} 缺少有业务含义的中文 target_label_zh。",
                )
            target_comment_zh = str(
                item.get("target_comment_zh")
                or item.get("comment_zh")
                or item.get("definition_zh")
                or item.get("definition")
                or ""
            ).strip()
            if not _contains_chinese(target_comment_zh):
                target_comment_zh = f"{target_label_zh}的业务语义定义；来源于正式证据与 Mapping。"
            item["target_label_zh"] = target_label_zh
            item["target_comment_zh"] = target_comment_zh
            mapping_ids.add(mapping_id)

        runtime = payload.get("realtime_runtime")
        enforce_rule_class_bindings(mappings, runtime, str(mapping.get("namespace") or ""),
                                    project_dir, self._read_json)
        if runtime is not None:
            requirement = str(runtime.get("reasoning_requirement") or "").upper()
            capabilities = runtime.get("reasoning_capabilities") or {}
            if requirement == "REQUIRED":
                rule_candidates_path = (
                    project_dir / "02-semantic-recognition/business-rule-candidates.json"
                    if project_dir is not None
                    else None
                )
                rule_candidates = (
                    self._read_json(rule_candidates_path)
                    if rule_candidates_path is not None and rule_candidates_path.is_file()
                    else []
                )
                rule_candidate_ids = {
                    str(item.get("id") or "") for item in rule_candidates if isinstance(item, dict)
                }
                rule_candidate_expressions = {
                    str(item.get("id") or ""): re.sub(
                        r"\s+", " ", str(item.get("formal_expression") or "")
                    ).strip()
                    for item in rule_candidates
                    if isinstance(item, dict)
                }
                rule_candidate_closed_world_inputs = {
                    str(item.get("id") or ""): list(item.get("closed_world_inputs") or [])
                    for item in rule_candidates
                    if isinstance(item, dict)
                }
                rule_candidate_negated_predicates = {
                    rule_id: set(
                        re.findall(
                            r"\bNOT\s+([A-Za-z_][A-Za-z0-9_:-]{0,127})\s*\(",
                            expression,
                            flags=re.I,
                        )
                    )
                    for rule_id, expression in rule_candidate_expressions.items()
                }
                rule_candidate_required_set_sources = {
                    str(item.get("id") or ""): item.get("required_set_source")
                    for item in rule_candidates
                    if isinstance(item, dict)
                }
                production_rule_ids = {
                    str(item.get("id") or "")
                    for item in rule_candidates
                    if isinstance(item, dict)
                    and str(item.get("status") or "").upper()
                    in {"DATABASE_FACT", "DOCUMENT_EVIDENCE"}
                    and not bool(item.get("review_required"))
                }
                bound_rule_ids: set[str] = set()
                for name, capability in capabilities.items():
                    if str(capability.get("execution_scope") or "").upper() != "FULL_QUERY_RESULT":
                        raise WorkflowGateError(
                            "G-S3-PRODUCTION-REASONING",
                            f"生产推理能力 {name} 必须使用 FULL_QUERY_RESULT，代表性探针只能用于诊断。",
                        )
                    source_rule_ids = {
                        str(value) for value in capability.get("source_rule_ids") or []
                    }
                    if not source_rule_ids or not source_rule_ids.issubset(rule_candidate_ids):
                        raise WorkflowGateError(
                            "G-S3-PRODUCTION-REASONING",
                            f"生产推理能力 {name} 必须完整绑定 S2 正式业务规则候选。",
                        )
                    for runtime_rule in capability.get("rules") or []:
                        runtime_rule_id = str(runtime_rule.get("rule_id") or "")
                        runtime_expression = re.sub(
                            r"\s+", " ", str(runtime_rule.get("expression") or "")
                        ).strip()
                        if runtime_expression != rule_candidate_expressions.get(runtime_rule_id):
                            raise WorkflowGateError(
                                "G-S3-PRODUCTION-REASONING",
                                f"生产推理能力 {name} 的规则 {runtime_rule_id} 与 S2 正式表达式不一致。",
                            )
                    registered_closed_world_inputs = [
                        closed_world_diagnostics.canonicalize_registered_snapshot(value)
                        for rule_id in source_rule_ids
                        for value in rule_candidate_closed_world_inputs.get(rule_id, [])
                        if str(value.get("predicate") or "").strip()
                        in rule_candidate_negated_predicates.get(rule_id, set())
                    ]
                    submitted_closed_world_inputs = capability.get("closed_world_inputs") or []
                    expected_closed_world_inputs = {
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        for value in registered_closed_world_inputs
                    }
                    actual_closed_world_inputs = {
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        for value in submitted_closed_world_inputs
                    }
                    if actual_closed_world_inputs != expected_closed_world_inputs:
                        differences = closed_world_diagnostics.snapshot_contract_differences(
                            registered_closed_world_inputs, submitted_closed_world_inputs
                        )
                        raise WorkflowGateError(
                            "G-S3-PRODUCTION-REASONING",
                            f"生产推理能力 {name} 的 closed_world_inputs 与 S2 完整快照契约不一致。"
                            + (" 差异：" + "；".join(differences) if differences else ""),
                            path=f"realtime_runtime.reasoning_capabilities.{name}.closed_world_inputs",
                            reason_code="S2_CLOSED_WORLD_INPUT_MISMATCH",
                        )
                    expected_required_sources = {
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        for rule_id in source_rule_ids
                        if rule_candidate_negated_predicates.get(rule_id)
                        if (value := rule_candidate_required_set_sources.get(rule_id))
                    }
                    actual_required_source = capability.get("required_set_source")
                    actual_required_sources = (
                        {
                            json.dumps(
                                actual_required_source,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        }
                        if actual_required_source
                        else set()
                    )
                    if actual_required_sources != expected_required_sources:
                        raise WorkflowGateError(
                            "G-S3-PRODUCTION-REASONING",
                            f"生产推理能力 {name} 的 required_set_source 与 S2 目录快照契约不一致。",
                        )
                    bound_rule_ids.update(source_rule_ids)
                missing_rule_ids = sorted(production_rule_ids - bound_rule_ids)
                if missing_rule_ids:
                    raise WorkflowGateError(
                        "G-S3-PRODUCTION-REASONING",
                        "S2 已确认的生产规则必须在 S3 全部绑定到可执行推理能力；"
                        "不能把无法执行的规则留到 S6/S7 才发现：" + ", ".join(missing_rule_ids[:8]),
                    )

        if intake_mode != "DOCUMENT_ONLY" and runtime is not None:
            mapping_obda = str(runtime.get("mapping_obda") or "")
            uncovered_targets = _runtime_uncovered_targets(mappings, mapping_obda)
            if uncovered_targets:
                raise WorkflowGateError(
                    "G-S3-RUNTIME-COVERAGE",
                    "Ontop OBDA 未覆盖正式 Mapping 目标：" + ", ".join(uncovered_targets[:5]),
                )
            datatype_issues = collect_mapping_runtime_issues(mappings, mapping_obda)
            if datatype_issues:
                details = "；".join(issue["message"] for issue in datatype_issues[:8])
                if len(datatype_issues) > 8:
                    details += f"；另有 {len(datatype_issues) - 8} 项"
                raise WorkflowGateError("G-S3-RUNTIME-DATATYPE", details)
            if (
                str(runtime.get("reasoning_requirement") or "").upper() == "NOT_APPLICABLE"
                and project_dir is not None
            ):
                rule_candidates_path = (
                    project_dir / "02-semantic-recognition/business-rule-candidates.json"
                )
                rule_candidates = (
                    self._read_json(rule_candidates_path) if rule_candidates_path.is_file() else []
                )
                if rule_candidates:
                    candidate_ids = [
                        str(item.get("id") or "")
                        for item in rule_candidates
                        if isinstance(item, dict)
                    ]
                    raise WorkflowGateError(
                        "G-S3-REASONING-POLICY",
                        "S2 已识别业务规则候选，不能把生产推理声明为不适用："
                        + ", ".join(candidate_ids[:5]),
                    )

        confirmations = payload["confirmations"]
        if len(confirmations) > self._s3_confirmation_limit(s3_state):
            raise WorkflowGateError(
                "G-S3-CONFIRMATION-LIMIT",
                f"高影响建模问题最多 {self._s3_confirmation_limit(s3_state)} 项。"
                + ("总体设计在 S4 联合确认。" if self._joint_design_enabled(s3_state) else "系统还会追加 1 项总体映射确认。"),
            )

        normalized: list[dict[str, Any]] = []
        confirmation_ids: set[str] = set()
        for index, item in enumerate(confirmations, start=1):
            confirmation_id = str(item.get("id") or "").strip()
            if not confirmation_id or confirmation_id in confirmation_ids:
                raise WorkflowGateError("G-S3-CONFIRMATION", f"第 {index} 个确认项 id 缺失或重复。")
            if confirmation_id == "S3-OVERALL-MAPPING-REVIEW":
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION",
                    "S3-OVERALL-MAPPING-REVIEW 是系统保留的总体评审编号。",
                )
            for field in ("title", "business_question"):
                if not str(item.get(field) or "").strip():
                    raise WorkflowGateError(
                        "G-S3-CONFIRMATION",
                        f"确认项 {confirmation_id} 缺少 {field}。",
                    )

            evidence = item.get("evidence") or {}
            database_facts = evidence.get("database_facts") or []
            business_materials = evidence.get("business_materials") or []
            customer_interviews = evidence.get("customer_interviews") or []
            if not isinstance(database_facts, list):
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION-EVIDENCE",
                    f"确认项 {confirmation_id} 的 database_facts 必须是数组。",
                )
            if intake_mode == "DOCUMENT_ONLY" and not (business_materials or customer_interviews):
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION-EVIDENCE",
                    f"确认项 {confirmation_id} 至少需要一条可追溯的资料证据。",
                )
            if intake_mode == "DOCUMENT_ONLY":
                for material in business_materials:
                    if (
                        not isinstance(material, dict)
                        or not str(material.get("summary") or "").strip()
                        or not material.get("source_refs")
                    ):
                        raise WorkflowGateError(
                            "G-S3-CONFIRMATION-EVIDENCE",
                            f"确认项 {confirmation_id} 的资料证据必须包含 summary 和 source_refs。",
                        )
            if intake_mode != "DOCUMENT_ONLY" and not database_facts:
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION-EVIDENCE",
                    f"确认项 {confirmation_id} 至少需要一条数据库事实。",
                )
            for fact in database_facts:
                if not str(fact.get("summary") or "").strip() or not fact.get("source_refs"):
                    raise WorkflowGateError(
                        "G-S3-CONFIRMATION-EVIDENCE",
                        f"确认项 {confirmation_id} 的数据库事实必须包含 summary 和 source_refs。",
                    )
            ai_inference = str(evidence.get("ai_inference") or "").strip()
            if not ai_inference:
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION-EVIDENCE",
                    f"确认项 {confirmation_id} 缺少 AI 判断说明。",
                )

            options = item.get("options") or []
            if not isinstance(options, list) or len(options) != 2:
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION-OPTIONS",
                    f"确认项 {confirmation_id} 必须且只能提供两个方案。",
                )
            option_ids: set[str] = set()
            recommended_ids: list[str] = []
            normalized_options: list[dict[str, Any]] = []
            for option in options:
                option_id = str(option.get("id") or "").strip()
                if not option_id or option_id in option_ids:
                    raise WorkflowGateError(
                        "G-S3-CONFIRMATION-OPTIONS",
                        f"确认项 {confirmation_id} 的方案 id 缺失或重复。",
                    )
                for field in ("label", "summary", "impact"):
                    if not str(option.get(field) or "").strip():
                        raise WorkflowGateError(
                            "G-S3-CONFIRMATION-OPTIONS",
                            f"确认项 {confirmation_id} 的方案 {option_id} 缺少 {field}。",
                        )
                option_updates = option.get("mapping_updates") or []
                if not isinstance(option_updates, list):
                    raise WorkflowGateError(
                        "G-S3-CONFIRMATION-OPTIONS",
                        f"确认项 {confirmation_id} 的方案 {option_id} mapping_updates 必须是数组。",
                    )
                for update in option_updates:
                    update_id = str(update.get("id") or "")
                    if update_id not in mapping_ids:
                        raise WorkflowGateError(
                            "G-S3-CONFIRMATION-OPTIONS",
                            f"确认项 {confirmation_id} 的方案 {option_id} 引用了不存在的 Mapping：{update_id}。",
                        )
                option_ids.add(option_id)
                if option.get("recommended") is True:
                    recommended_ids.append(option_id)
                normalized_options.append(
                    {
                        "id": option_id,
                        "label": str(option["label"]).strip(),
                        "summary": str(option["summary"]).strip(),
                        "impact": str(option["impact"]).strip(),
                        "recommended": option.get("recommended") is True,
                        "mapping_updates": option_updates,
                        "action": str(option.get("action") or "APPLY").strip().upper(),
                    }
                )
            if len(recommended_ids) != 1:
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION-OPTIONS",
                    f"确认项 {confirmation_id} 必须且只能有一个推荐方案。",
                )

            confidence = item.get("confidence")
            if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION",
                    f"确认项 {confirmation_id} 的 confidence 必须在 0 到 1 之间。",
                )
            affected = item.get("affected_mapping_ids") or []
            if any(mapping_id not in mapping_ids for mapping_id in affected):
                raise WorkflowGateError(
                    "G-S3-CONFIRMATION",
                    f"确认项 {confirmation_id} 引用了不存在的 mapping id。",
                )
            confirmation_ids.add(confirmation_id)
            normalized.append(
                {
                    "id": confirmation_id,
                    "title": str(item["title"]).strip(),
                    "question": str(item["business_question"]).strip(),
                    "business_question": str(item["business_question"]).strip(),
                    "impact_level": "HIGH",
                    "evidence": {
                        "database_facts": database_facts,
                        "business_materials": business_materials,
                        "customer_interviews": customer_interviews,
                        "ai_inference": ai_inference,
                    },
                    "confidence": confidence,
                    "options": normalized_options,
                    "recommended_option_id": recommended_ids[0],
                    "technical_impact": item.get("technical_impact") or [],
                    "decision_basis": item.get("decision_basis")
                    or (
                        ["资料证据", "本体工程判断"]
                        if intake_mode == "DOCUMENT_ONLY"
                        else ["数据库证据", "本体工程判断"]
                    ),
                    "affected_mapping_ids": affected,
                    "status": "PENDING",
                }
            )

        overall_question = (
            "是否确认当前资料证据到本体的全部映射，可以作为下一阶段本体设计的正式输入？"
            if intake_mode == "DOCUMENT_ONLY"
            else "是否确认当前数据库到本体的全部映射，可以作为下一阶段本体设计的正式输入？"
        )
        overall_facts = (
            []
            if intake_mode == "DOCUMENT_ONLY"
            else [
                {
                    "summary": f"当前映射草案共 {len(mappings)} 条，每条都已关联数据库结构或查询证据。",
                    "source_refs": [
                        "03-mapping-review/mapping-draft.yaml",
                        "01-data-understanding/schema-snapshot.json",
                    ],
                }
            ]
        )
        overall_materials = (
            [
                {
                    "summary": f"当前映射草案共 {len(mappings)} 条，每条都保留 S0 文件资料或原文定位证据。",
                    "source_refs": [
                        "03-mapping-review/mapping-draft.yaml",
                        "00-document-evidence/evidence-index.json",
                    ],
                }
            ]
            if intake_mode == "DOCUMENT_ONLY"
            else []
        )
        if not self._joint_design_enabled(s3_state):
            normalized.append(
                {
                    "id": "S3-OVERALL-MAPPING-REVIEW",
                    "title": "总体映射确认",
                    "question": overall_question,
                    "business_question": overall_question,
                    "impact_level": "HIGH",
                    "evidence": {
                        "database_facts": overall_facts,
                        "business_materials": overall_materials,
                        "customer_interviews": [],
                        "ai_inference": (
                            "系统建议先整体确认；一旦通过，S4 只能基于这份正式映射生成施工图。"
                            "如果业务含义仍不准确，应退回 S2 调整并重新评审。"
                        ),
                    },
                    "confidence": 1.0,
                    "options": [
                        {
                            "id": "APPROVE-MAPPING",
                            "label": "确认映射并继续",
                            "summary": "冻结当前映射，进入 S4 本体设计。",
                            "impact": "后续设计、构建和验证都以当前正式映射为唯一输入。",
                            "recommended": True,
                            "mapping_updates": [],
                            "action": "APPROVE",
                        },
                        {
                            "id": "RETURN-TO-SEMANTICS",
                            "label": "退回业务语义调整",
                            "summary": "回到 S2 修正业务概念、关系或命名，再重新生成映射。",
                            "impact": "保留当前版本和报告，创建调整记录，并重新执行 S2、S3。",
                            "recommended": False,
                            "mapping_updates": [],
                            "action": "RETURN_TO_S2",
                        },
                    ],
                    "recommended_option_id": "APPROVE-MAPPING",
                    "technical_impact": ["S3", "S4", "S5", "S6", "S7"],
                    "decision_basis": [
                        "资料证据" if intake_mode == "DOCUMENT_ONLY" else "数据库证据",
                        "映射完整性",
                        "人工总体确认",
                    ],
                    "affected_mapping_ids": sorted(mapping_ids),
                    "status": "PENDING",
                }
            )

        normalized_automatic: list[dict[str, Any]] = []
        automatic_ids: set[str] = set()
        for index, item in enumerate(payload["automatic_decisions"], start=1):
            decision_id = str(item.get("id") or "").strip()
            if not decision_id or decision_id in automatic_ids or decision_id in confirmation_ids:
                raise WorkflowGateError(
                    "G-S3-AUTOMATIC-DECISION",
                    f"第 {index} 个自动决定 id 缺失或重复。",
                )
            for field in ("topic", "decision", "reason"):
                if not str(item.get(field) or "").strip():
                    raise WorkflowGateError(
                        "G-S3-AUTOMATIC-DECISION",
                        f"自动决定 {decision_id} 缺少 {field}。",
                    )
            source_refs = item.get("source_refs") or []
            if not source_refs:
                raise WorkflowGateError(
                    "G-S3-AUTOMATIC-DECISION",
                    f"自动决定 {decision_id} 缺少 source_refs。",
                )
            affected = item.get("affected_mapping_ids") or []
            if any(mapping_id not in mapping_ids for mapping_id in affected):
                raise WorkflowGateError(
                    "G-S3-AUTOMATIC-DECISION",
                    f"自动决定 {decision_id} 引用了不存在的 mapping id。",
                )
            automatic_ids.add(decision_id)
            normalized_automatic.append(
                {
                    "id": decision_id,
                    "topic": str(item["topic"]).strip(),
                    "decision": str(item["decision"]).strip(),
                    "reason": str(item["reason"]).strip(),
                    "source_refs": source_refs,
                    "affected_mapping_ids": affected,
                    "decided_by": "ORION_POLICY",
                    "status": "AUTO_ACCEPTED",
                    "decided_at": _now(),
                }
            )
        return normalized, normalized_automatic

    def _require_unchanged_build_for_reuse(
        self, project_dir: Path, design: dict[str, Any]
    ) -> None:
        from .rdf_builder import BUILD_DESIGN_FIELDS

        if not (project_dir / "workflow-state.json").is_file():
            return
        state = self._read_state(project_dir)
        active = state.get("active_revision") or {}
        if (active.get("status") != "IN_PROGRESS"
                or "S5" in (active.get("required_revalidation_stages") or [])
                or state.get("stage_statuses", {}).get("S5") != "PASSED"):
            return
        baseline = (project_dir / "revisions" / str(active["revision_id"])
                    / "before/04-ontology-design/ontology-design.yaml")
        if not baseline.is_file():
            raise WorkflowGateError("G-REUSE-BUILD", "缺少修订前施工图，不能证明 S5 可复用。")
        previous = yaml.safe_load(baseline.read_text(encoding="utf-8")) or {}
        changed = [key for key in BUILD_DESIGN_FIELDS if previous.get(key) != design.get(key)]
        if changed:
            raise WorkflowGateError(
                "G-REUSE-BUILD",
                "本体构建输入已变化，必须将 S5 纳入重验范围：" + ", ".join(changed),
            )

    def _require_unchanged_mapping_for_reuse(
        self, project_dir: Path, mapping: dict[str, Any], *, draft: bool = False
    ) -> None:
        if not (project_dir / "workflow-state.json").is_file():
            return  # Standalone payload validation has no reusable stage state.
        state = self._read_state(project_dir)
        active = state.get("active_revision") or {}
        required = set(active.get("required_revalidation_stages") or [])
        if active.get("status") != "IN_PROGRESS" or active.get("target_stage") != "S3":
            return
        reused = [stage for stage in ("S4", "S5") if stage not in required
                  and state.get("stage_statuses", {}).get(stage) == "PASSED"]
        if not reused:
            return
        filename = "mapping-draft.yaml" if draft else "mapping.yaml"
        baseline = (project_dir / "revisions" / str(active["revision_id"])
                    / "before" / "03-mapping-review" / filename)
        if not baseline.is_file():
            raise WorkflowGateError("G-REUSE-MAPPING", "缺少修订前 Mapping，不能证明 S4/S5 可复用。")
        previous = yaml.safe_load(baseline.read_text(encoding="utf-8")) or {}
        ignored = {"review", "mapping_version"}
        if _fingerprint({k: v for k, v in previous.items() if k not in ignored}) != _fingerprint(
            {k: v for k, v in mapping.items() if k not in ignored}
        ):
            raise WorkflowGateError(
                "G-REUSE-MAPPING",
                "本次修订声明复用 S4/S5，但 Mapping 语义已经变化；请按 MAPPING 组件重新计算影响范围。",
            )

    def _finalize_mapping(self, project_dir: Path, state: dict[str, Any]) -> None:
        stage_dir = project_dir / "03-mapping-review"
        if self._joint_design_enabled(state) and (state.get("s3_runtime_review") or {}).get("validated") is not True:
            raise WorkflowGateError("G-S3-RUNTIME", "运行设计尚未按业务决定完成全部 S3 预检，不能完成或冻结 S3。")
        mapping = yaml.safe_load((stage_dir / "mapping-draft.yaml").read_text(encoding="utf-8"))
        self._require_unchanged_mapping_for_reuse(project_dir, mapping)
        runtime_mapping_path = stage_dir / "runtime/mapping.obda"
        if state.get("intake_mode") != "DOCUMENT_ONLY" and runtime_mapping_path.exists():
            uncovered_targets = _runtime_uncovered_targets(
                mapping.get("mappings") or [],
                runtime_mapping_path.read_text(encoding="utf-8"),
            )
            if uncovered_targets:
                raise WorkflowGateError(
                    "G-S3-RUNTIME-COVERAGE",
                    "Mapping 评审修改后的目标未进入 Ontop OBDA："
                    + ", ".join(uncovered_targets[:5]),
                )
        pending = self._read_json(stage_dir / "pending-confirmations.json")
        automatic_path = stage_dir / "automatic-decisions.json"
        automatic = self._read_json(automatic_path) if automatic_path.exists() else []
        mapping["review"] = {
            "status": "REVIEWED",
            "reviewed_at": _now(),
            "confirmation_count": len(pending),
            "decision_ids": [item["id"] for item in pending],
            "automatic_decision_count": len(automatic),
            "automatic_decision_ids": [item["id"] for item in automatic],
        }
        if self._joint_design_enabled(state):
            mapping["review"].update({
                "review_scope": "SEMANTICS_AND_MAPPING_FEASIBILITY",
                "design_disposition": "REVIEWED_CANDIDATE",
                "final_design_stage": "S4",
            })
        self._write_yaml(stage_dir / "mapping.yaml", mapping)
        overall_review = next(
            (
                item
                for item in pending
                if item.get("id") == "S3-OVERALL-MAPPING-REVIEW"
                and item.get("status") == "RESOLVED"
            ),
            None,
        )
        automatic_review = next(
            (item for item in automatic if item.get("id") == "S3-OVERALL-MAPPING-AUTO"),
            None,
        )
        review_record = overall_review or automatic_review or {}
        finalize_runtime_review(
            stage_dir,
            reviewed_by=str(review_record.get("decided_by") or "ORION_POLICY"),
            reviewed_at=str(review_record.get("decided_at") or mapping["review"]["reviewed_at"]),
        )
        self._write_json(
            stage_dir / "gate-results.json",
            {
                "stage": "S3",
                "status": "PASSED",
                "gates": [
                    {"id": "G-S3-MAPPING", "status": "PASSED"},
                    {"id": "G-S3-EVIDENCE", "status": "PASSED"},
                    {"id": "G-S3-CHINESE", "status": "PASSED"},
                    {"id": "G-S3-RUNTIME-COVERAGE", "status": "PASSED"},
                    {"id": "G-S3-REASONING-POLICY", "status": "PASSED"},
                    {"id": "G-S3-PRODUCTION-REASONING", "status": "PASSED"},
                    {"id": "GATE-1", "status": "PASSED"},
                ],
                "checked_at": _now(),
            },
        )
        self._atomic_write(
            stage_dir / "mapping-review-report.html",
            render_s3_report(
                stage_dir,
                self._read_json(project_dir / "project.json"),
                mapping,
                pending,
                automatic,
                self._read_json(stage_dir / "gate-results.json"),
            ),
        )
        regenerated_paths = {
            "03-mapping-review/README.md",
            "03-mapping-review/mapping.yaml",
            "03-mapping-review/gate-results.json",
            "03-mapping-review/mapping-review-report.html",
        }
        regenerated_paths.update(self._mapping_runtime_artifact_paths(stage_dir, state))
        self._mark_artifacts_regenerated(state, regenerated_paths)
        state["stage_statuses"]["S3"] = "PASSED"
        if state["stage_statuses"].get("S4") == "INVALIDATED":
            state["stage_statuses"]["S4"] = "PENDING"
        state["stage_fingerprints"]["S3"] = {
            "output": self._stage_fingerprint(stage_dir),
            "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
        }
        active_revision = state.get("active_revision") or {}
        required = set(active_revision.get("required_revalidation_stages") or [])
        selected_next_stage: str | None = None
        reused_stages: list[str] = []
        if required and active_revision.get("status") == "IN_PROGRESS":
            for candidate in STAGES[STAGES.index("S3") + 1 :]:
                candidate_status = state["stage_statuses"].get(candidate)
                if candidate in required or candidate_status not in {"PASSED", "NOT_APPLICABLE"}:
                    selected_next_stage = candidate
                    break
                if state["stage_statuses"].get(candidate) == "PASSED":
                    reused_stages.append(candidate)
            if reused_stages:
                active_revision["reused_unchanged_stages"] = list(
                    dict.fromkeys(
                        [
                            *(active_revision.get("reused_unchanged_stages") or []),
                            *reused_stages,
                        ]
                    )
                )
        if selected_next_stage and selected_next_stage != "S4":
            state["stage_statuses"][selected_next_stage] = "RUNNING"
            state["project_status"] = "IN_PROGRESS"
            state["current_stage"] = selected_next_stage
        else:
            if selected_next_stage == "S4":
                state["stage_statuses"]["S4"] = "PENDING"
            state["project_status"] = "S1_S3_READY"
            state["current_stage"] = None
        state["blocking"] = None
        state["last_error"] = None
        state["resume_point"] = (
            f"{selected_next_stage}: 复用未变化阶段后继续修订验证"
            if selected_next_stage and selected_next_stage != "S4"
            else (
                "S0、S2、S3 已完成，S1 已按资料范围留痕跳过；可继续 S4 本体设计"
                if state.get("intake_mode") == "DOCUMENT_ONLY"
                else "S1～S3 已完成；可继续 S4 本体设计"
            )
        )
        self._save_state(project_dir, state)
        self._append_event(project_dir, "FIRST_VERSION_COMPLETED", state, {"stage": "S3"})

    @staticmethod
    def _render_protege_hermit_review_guide(
        *,
        state: dict[str, Any],
        publication: dict[str, Any],
        contract: dict[str, Any],
        workflow_root: Path,
    ) -> str:
        expected = "一致" if contract.get("expected_consistent") is True else "不一致"
        # Keep the virtualenv executable path: resolving its symlink would select
        # the base interpreter and discard the installed runtime dependencies.
        python = Path(
            os.environ.get("ORION_WORKFLOW_PYTHON", "").strip() or sys.executable
        ).expanduser().absolute()
        workflow_root = workflow_root.expanduser().resolve()
        command = shlex.join(
            [
                str(python),
                str(Path(__file__).resolve().parents[2] / "scripts/open_release_in_protege.py"),
                "--project-id",
                str(publication["project_id"]),
                "--release-version",
                str(publication["release_version"]),
                "--workflow-root",
                str(workflow_root),
                "--review-root",
                str(workflow_root.parent / ".orion-runtime/protege-release-review"),
            ]
        )
        return (
            "# 在 Protégé 中复核 OWL DL / HermiT\n\n"
            f"- 项目：{state['project_name']} (`{publication['project_id']}`)\n"
            f"- 发布版本：`{publication['release_version']}`\n"
            f"- 复核文件：`../01-本体模型/ontology.owl`\n"
            f"- 文件 SHA-256：`{contract['ontology_sha256']}`\n"
            f"- S6 预期：HermiT {expected}，不可满足类数量 "
            f"`{contract['expected_unsatisfiable_class_count']}`\n"
            f"- S6 运行编号：`{contract.get('source_validation_run_id') or '未记录'}`\n\n"
            "## 推荐打开方式\n\n"
            "不要直接保存覆盖发布包。使用当前运行时及工程目录执行：\n\n"
            "```bash\n"
            f"{command}\n"
            "```\n\n"
            "该命令先校验发布包 manifest 和 OWL 哈希，再复制到独立复核目录并打开；"
            "桌面修改不会污染不可变发布物。\n\n"
            "## 在 Protégé 中运行\n\n"
            "1. 确认当前打开的是复制出的 `ontology.owl`。\n"
            "2. 选择 `Reasoner` -> `HermiT`。如果列表没有 HermiT，先在插件管理中启用。\n"
            "3. 选择 `Reasoner` -> `Start reasoner`。\n"
            "4. 查看状态栏是否报告 ontology consistent。\n"
            "5. 在 `Entities` -> `Classes` 的 inferred hierarchy 中检查新增分类；"
            "检查 `owl:Nothing` 下是否出现不可满足类。\n"
            "6. 将结果与 `../03-质量结论/hermit-report.json` 和"
            " `owl-dl-review-contract.json` 对照。\n\n"
            "## 结果解释\n\n"
            "HermiT 负责 OWL DL 的一致性、类层级和限制公理推理；"
            "它不是生产数据问答链路。生产运行时仍由 Ontop 取实时事实，"
            "再由 Semantica 执行版本化规则推理并返回统一证据包。"
            "如果复核结果与 S6 不一致，不应修改发布包，应创建新修订并从受影响阶段重跑。\n"
        )

    def _render_release_summary(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
    ) -> str:
        stage_names = {
            "S0": "资料接入与证据整理",
            "S1": "数据理解",
            "S2": "业务语义",
            "S3": "映射评审",
            "S4": "本体设计",
            "S5": "本体构建",
            "S6": "质量验证",
            "S7": "评审发布",
        }
        if self._joint_design_enabled(state):
            stage_names = {
                stage: stage_contract(stage)["name"] for stage in STAGES
            }
        stage_rows = "".join(
            "<tr>"
            f"<td><code>{stage}</code></td>"
            f"<td>{html.escape(stage_names[stage])}</td>"
            f'<td><span class="tag">{html.escape(str(state["stage_statuses"][stage]))}</span></td>'
            "</tr>"
            for stage in STAGES
        )
        release_assets = [
            ("01-本体模型/ontology.owl", "正式本体（OWL）"),
            ("01-本体模型/ontology.ttl", "便于审阅的本体（TTL）"),
            ("01-本体模型/shapes.ttl", "数据约束（SHACL）"),
            ("02-工程定义/mapping.yaml", "正式映射"),
            ("02-工程定义/ontology-design.yaml", "本体施工图"),
            ("03-质量结论/quality-summary.json", "质量结论"),
            ("03-质量结论/competency-question-report.json", "业务问题验证结果"),
            ("03-质量结论/hermit-report.json", "HermiT OWL DL 验证结果"),
            ("04-发布信息/Protégé-HermiT复核说明.md", "Protégé / HermiT 复核说明"),
            ("04-发布信息/owl-dl-review-contract.json", "OWL DL 复核契约"),
            ("04-发布信息/audit-reference.json", "审计底稿位置与校验码"),
            ("manifest.json", "发布包完整性清单"),
        ]
        if publication.get("package_profile") == "ENGINEERING_DELIVERY":
            release_assets.extend([
                ("package-contract.json", "工程包与发布包资产合同"),
                ("06-工程追溯/阶段产物索引.json", "S0—S7 阶段产物索引"),
                ("02-工程定义/规则目录/业务规则目录.md", "业务规则与执行器说明"),
            ])
        asset_rows = "".join(
            f'<li><a href="{href}">{html.escape(label)}</a></li>' for href, label in release_assets
        )
        return self._render_stage_report(
            title="本体工程发布包",
            subtitle=(
                f"{state['project_name']} · v{publication['release_version']} · "
                f"批准人 {publication['approved_by']}"
            ),
            metrics=[
                ("发布版本", publication["release_version"]),
                (
                    "阶段完成",
                    f"{sum(value in {'PASSED', 'NOT_APPLICABLE'} for value in state['stage_statuses'].values())} / 8",
                ),
                ("发布决定", publication["approval_decision"]),
                ("工作流版本", publication["workflow_version"]),
            ],
            sections=(
                '<section><h2>阶段结论</h2><div class="table-wrap"><table>'
                f"<thead><tr><th>阶段</th><th>名称</th><th>状态</th></tr></thead><tbody>{stage_rows}</tbody>"
                "</table></div></section>"
                f"<section><h2>发布说明</h2><p>{html.escape(publication['release_notes'])}</p></section>"
                f"<section><h2>工程资产</h2><ul>{asset_rows}</ul></section>"
            ),
        )

    @staticmethod
    def _render_stage_report(
        *,
        title: str,
        subtitle: str,
        metrics: list[tuple[str, Any]],
        sections: str,
    ) -> str:
        metric_cards = "".join(
            f"<article><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></article>"
            for label, value in metrics
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--ink:#15203b;--muted:#69758c;--line:#e4e9f2;--blue:#2d67f6;--surface:#fff;--wash:#f6f8fc}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--wash);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{width:min(1180px,calc(100% - 40px));margin:40px auto 72px}}header{{padding:36px 40px;background:linear-gradient(135deg,#101c3b,#284990);color:#fff;border-radius:22px;box-shadow:0 16px 44px #18295824}}
header small{{font-size:12px;letter-spacing:.14em;text-transform:uppercase;opacity:.68}}h1{{margin:8px 0 4px;font-size:32px}}header p{{margin:0;opacity:.78}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0}}
.metrics article,section{{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 28px #1b31500a}}.metrics article{{padding:18px 20px}}.metrics span{{display:block;color:var(--muted);font-size:13px}}.metrics strong{{display:block;margin-top:5px;font-size:23px}}
section{{padding:26px 28px;margin-top:16px}}h2{{margin:0 0 16px;font-size:18px}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:12px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:12px;font-weight:600;letter-spacing:.04em}}code{{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}}pre{{margin:0;overflow:auto;padding:18px;background:#f7f9fd;border-radius:12px;color:#31405d}}.tag{{display:inline-block;padding:2px 8px;border-radius:999px;background:#edf3ff;color:#2457c7;font-size:11px}}
footer{{margin-top:20px;color:var(--muted);font-size:12px;text-align:center}}@media(max-width:760px){{main{{width:min(100% - 20px,1180px);margin-top:10px}}header{{padding:24px;border-radius:16px}}.metrics{{grid-template-columns:repeat(2,1fr)}}section{{padding:20px 16px}}}}
</style></head><body><main><header><small>ORION Ontology Engineer</small><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></header>
<div class="metrics">{metric_cards}</div>{sections}<footer>由 ORION workflow MCP 基于阶段正式资产确定性生成 · {_now()}</footer></main></body></html>
"""

    def _apply_mapping_updates(
        self,
        project_dir: Path,
        updates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not updates:
            return []
        mapping_path = project_dir / "03-mapping-review/mapping-draft.yaml"
        mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
        mappings = mapping.get("mappings") or []
        index = {str(item.get("id")): item for item in mappings}
        applied: list[dict[str, Any]] = []
        for update in updates:
            mapping_id = str(update.get("id") or "").strip()
            if mapping_id not in index:
                raise WorkflowError(f"mapping_updates 引用了不存在的映射：{mapping_id}")
            changes = {key: value for key, value in update.items() if key != "id"}
            if not changes:
                raise WorkflowError(f"mapping_updates 没有提供变更内容：{mapping_id}")
            revised = {**index[mapping_id], **changes, "id": mapping_id}
            if not revised.get("source_refs"):
                raise WorkflowError(f"更新后的映射缺少 source_refs：{mapping_id}")
            index[mapping_id].clear()
            index[mapping_id].update(revised)
            applied.append({"id": mapping_id, "changes": changes})
        self._write_yaml(mapping_path, mapping)
        return applied

    def _stage_rollback_impact(
        self,
        project_dir: Path,
        state: dict[str, Any],
        target_stage: str,
        *,
        changed_components: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if state.get("project_status") == "ARCHIVED":
            raise WorkflowError("已归档工程不能直接回退；请先恢复工程并重新生成回退预览。")
        if target_stage not in {"S0", "S1", "S2", "S3", "S4", "S5", "S6"}:
            raise WorkflowError("只允许回退到 S0-S6；S7 发布状态必须使用发布控制。")
        if target_stage == "S1" and state.get("intake_mode") == "DOCUMENT_ONLY" and not self._joint_design_enabled(state):
            raise WorkflowError(
                "资料建模项目不执行 S1；请回到 S0 重整资料，或从 S2 重新识别业务语义。"
            )
        target_index = STAGES.index(target_stage)
        current = state.get("current_stage")
        progressed_statuses = {
            "PASSED",
            "RUNNING",
            "FAILED",
            "BLOCKED_HUMAN",
            "DEFERRED",
            "REVOKED",
            "INVALIDATED",
        }
        progressed = any(
            state.get("stage_statuses", {}).get(item) in progressed_statuses
            for item in STAGES[target_index:]
        )
        if not progressed and current != target_stage:
            raise WorkflowError(f"项目尚未推进到 {target_stage}，无需回退。")
        publication_path = project_dir / "07-release/publication.json"
        published = state.get("project_status") == "PUBLISHED" or publication_path.exists()
        publication = self._read_json(publication_path) if publication_path.exists() else None
        release_impact = {
            "published": bool(published),
            "release_version": (publication or {}).get("release_version"),
            "requires_release_action": bool(published),
            "message": (
                "该工程已有正式发布版本；必须先明确撤回并基于原版本创建新修订，不能原地回退覆盖正式资产。"
                if published
                else "当前没有正式发布版本；确认后会使受影响的未发布下游结果失效。"
            ),
        }
        dependency_plan = (
            component_revalidation_plan(changed_components, target_stage=target_stage)
            if changed_components
            else None
        )
        required_stages = set(
            dependency_plan["required_revalidation_stages"]
            if dependency_plan
            else STAGES[target_index:]
        )
        if self._joint_design_enabled(state) and target_index <= STAGES.index("S4"):
            required_stages.add("S4")
        # Unchanged is not equivalent to completed: an earlier failed or pending
        # stage cannot be skipped merely because this component does not affect it.
        required_stages.update(
            stage for stage in STAGES[target_index + 1 :]
            if state.get("stage_statuses", {}).get(stage) not in {"PASSED", "NOT_APPLICABLE"}
        )
        affected_downstream: list[dict[str, Any]] = []
        for downstream in STAGES[target_index + 1 :]:
            status = str(state.get("stage_statuses", {}).get(downstream) or "PENDING")
            has_artifacts = any(
                path.is_file()
                for path in (project_dir / STAGE_FOLDERS[downstream]).glob("*")
                if path.name != "README.md"
            )
            if downstream not in required_stages and status == "PASSED":
                disposition = "REUSED_UNCHANGED"
            else:
                disposition = (
                    "INVALIDATED"
                    if downstream in required_stages
                    and (status in progressed_statuses or has_artifacts)
                    else "PENDING_UPSTREAM"
                )
            affected_downstream.append(
                {
                    "stage": downstream,
                    "current_status": status,
                    "disposition": disposition,
                }
            )
        current_artifacts = []
        preserved_inputs: list[dict[str, Any]] = []
        for path in self._revision_files(project_dir, target_stage):
            relative = path.relative_to(project_dir).as_posix()
            if (
                target_stage == "S0"
                and relative in {"00-document-evidence/cq-intake.json", "00-document-evidence/source-scope.json"}
            ) or (
                Path(relative).name == "decisions.jsonl"
                and relative.startswith(("03-mapping-review/", "04-ontology-design/"))
            ):
                preserved_inputs.append(
                    {
                        "path": relative,
                        "current_sha256": _file_checksum(path),
                        "after_commit": (
                            "PRESERVED_AUDIT_DECISIONS"
                            if Path(relative).name == "decisions.jsonl"
                            else "PRESERVED_ENGINEERING_INPUT"
                        ),
                    }
                )
                continue
            stage = next(item for item in STAGES if relative.startswith(f"{STAGE_FOLDERS[item]}/"))
            if stage not in required_stages:
                continue
            current_artifacts.append(
                {
                    "path": relative,
                    "stage": stage,
                    "current_sha256": _file_checksum(path),
                    "after_commit": "HISTORICAL_INVALIDATED",
                }
            )
        active_revision = state.get("active_revision") or {}
        return {
            "target_stage": target_stage,
            "changed_components": list(changed_components),
            "target_current_status": state.get("stage_statuses", {}).get(target_stage, "PENDING"),
            "affected_downstream": affected_downstream,
            "artifact_impact": {
                "current_artifacts": current_artifacts,
                "current_count": len(current_artifacts),
                "preserved_inputs": preserved_inputs,
                "history_preserved": True,
                "replacement_policy": (
                    "目标阶段重跑后产生当前产物；旧产物只在 revision 历史中可读；"
                    "S0 的 CQ intake 是工程输入，保持当前并继续参与后续血缘校验。"
                ),
            },
            "version_impact": {
                "current_project_revision": int(state.get("revision") or 0),
                "active_revision_id": active_revision.get("revision_id"),
                "new_revision_snapshot_required": True,
                "published_version_immutable": True,
            },
            "release_impact": release_impact,
            "dependency_plan": dependency_plan,
            "commit_allowed": not published,
        }

    @staticmethod
    def _require_expected_revision(
        state: dict[str, Any],
        expected_revision: int | None,
    ) -> None:
        if expected_revision is None:
            return
        current_revision = int(state.get("revision") or 0)
        if int(expected_revision) != current_revision:
            raise WorkflowError(
                f"工程状态已变化：请求 revision={expected_revision}，当前 revision={current_revision}。请刷新后重试。"
            )

    @contextmanager
    def _project_operation_lock(self, project_dir: Path):
        lock_key = str(project_dir.resolve())
        held_locks = getattr(self._project_lock_state, "held_locks", None)
        if held_locks is None:
            held_locks = {}
            self._project_lock_state.held_locks = held_locks
        if lock_key in held_locks:
            held_locks[lock_key] += 1
            try:
                yield
            finally:
                held_locks[lock_key] -= 1
            return

        lock_path = project_dir / ".orion-operation.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held_locks[lock_key] = 1
            try:
                yield
            finally:
                held_locks.pop(lock_key, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _project_mutation_lock(
        self, project_id: str, *, allow_archived: bool = False,
        recovery_preflight_id: str | None = None,
    ):
        recovery = getattr(self._project_lock_state, "managed_recovery", None)
        if recovery and (recovery_preflight_id != recovery[2] or project_id != recovery[3]):
            raise WorkflowError("托管恢复上下文只允许原预检操作回读。")
        with (
            self._engineering_project_guard(project_id),
            self._unguarded_project_mutation_lock(
                project_id, allow_archived=allow_archived,
                recovery_preflight_id=recovery_preflight_id,
            ) as project_dir,
        ):
            yield project_dir

    @contextmanager
    def _engineering_project_guard(self, project_id):
        # Opted-in task authority precedes workflow locks. The same instance is
        # supplied by the managed runner so its commit guard can nest safely.
        from .engineering_tasks import EngineeringTasks

        project_dir = self._resolve_project(project_id)
        recovery = getattr(self._project_lock_state, "managed_recovery", None)
        if recovery:
            if project_id != recovery[3]:
                raise WorkflowError("托管恢复工程身份不一致。")
            with recovery[0].recovery_guard(recovery[1]):
                yield
            return
        directory = self.root / ".engineering-control" / project_dir.name
        context = getattr(self._project_lock_state, "managed_execution", None)
        tasks = context[0] if context else None
        token = context[1] if context else None
        if tasks is None and (directory / "tasks.json").is_file():
            tasks = EngineeringTasks(directory)
        if tasks is not None and tasks.directory.resolve() != directory.resolve():
            raise WorkflowError("Managed context belongs to a different project")
        depth = getattr(self._project_lock_state, "engineering_guard_depth", 0)
        with ExitStack() as stack:
            if tasks is not None and depth == 0:
                stack.enter_context(tasks.project_guard(
                    project_id,
                    lambda: self._read_state(project_dir).get("current_stage"),
                    token=token,
                ))
            self._project_lock_state.engineering_guard_depth = depth + 1
            try:
                yield
            finally:
                self._project_lock_state.engineering_guard_depth = depth

    @contextmanager
    def managed_execution(self, tasks, token):
        """Internal runner context, never exposed as model-supplied tool fields."""
        if tasks.directory.resolve().parent != (self.root / ".engineering-control").resolve():
            raise WorkflowError("Task authority belongs to another workflow home")
        previous = getattr(self._project_lock_state, "managed_execution", None)
        self._project_lock_state.managed_execution = (tasks, token)
        try:
            yield
        finally:
            self._project_lock_state.managed_execution = previous

    @contextmanager
    def managed_recovery(self, tasks, task_id, operation_id):
        """Internal checkpoint replay only; no approval or stage execution authority."""
        task = tasks.read(task_id)
        expected = self.root / ".engineering-control" / task["project"]
        if tasks.directory.resolve() != expected.resolve():
            raise WorkflowError("恢复任务不属于该工程。")
        if not re.fullmatch(r"PFL-[A-F0-9]{16}", str(operation_id)):
            raise WorkflowError("恢复操作身份无效。")
        previous = getattr(self._project_lock_state, "managed_recovery", None)
        self._project_lock_state.managed_recovery = (tasks, task_id, operation_id, task["project"], task["stage"])
        try:
            yield
        finally:
            self._project_lock_state.managed_recovery = previous

    @contextmanager
    def _unguarded_project_mutation_lock(
        self,
        project_id: str,
        *,
        allow_archived: bool = False,
        recovery_preflight_id: str | None = None,
    ):
        """Serialize one public project mutation before it can read project state."""

        with self._lock:
            project_dir = self._resolve_project(project_id)
            with self._project_operation_lock(project_dir):
                project_status = self._read_state(project_dir).get("project_status")
                if project_status == "INITIALIZING":
                    raise WorkflowError(
                        "修订工程仍在从不可变发布版本初始化；当前写操作已拒绝，请稍后重试。"
                    )
                if project_status == "ARCHIVED" and not allow_archived:
                    raise WorkflowError(
                        "工程已归档；必须先执行恢复操作，不能由阶段写入静默重新激活。"
                    )
                context = getattr(self._project_lock_state, "preflight_context", None)
                active_id = (context["operation"]["operation_id"] if context
                             and context["project_dir"] == str(project_dir) else None)
                try:
                    pending = preflight_operations.pending(project_dir)
                except (ValueError, OSError) as exc:
                    raise WorkflowError("预检操作记录损坏，必须先核对正式证据。") from exc
                if any(item["operation_id"] not in {recovery_preflight_id, active_id} for item in pending):
                    raise WorkflowError("存在未完成预检操作，请使用原 preflight_token 恢复或对账，禁止并行修改工程。")
                self._require_current_storage_before_mutation(project_dir)
                yield project_dir

    def _require_current_storage_before_mutation(self, project_dir: Path) -> None:
        """Fail closed when an externally-managed project has stale persistence."""

        receipt_path = project_dir / ".storage-sync-status.json"
        receipt = self._read_json(receipt_path) if receipt_path.exists() else None
        postgres_status = str(
            ((receipt or {}).get("services") or {}).get("postgresql", {}).get("status") or ""
        )
        # A durable receipt is the boundary: once a project has been read back
        # from PostgreSQL, later writes may not let workflow-state run ahead of
        # that ledger.  Do not use metadata_required alone here; an outage on a
        # newly-created/local-only project still relies on the existing outbox
        # recovery path.
        externally_managed = bool(
            postgres_status == "CONNECTED"
            or (receipt or {}).get("read_back", {}).get("storage") == "postgresql"
        )
        if not externally_managed:
            return
        if self._metadata_store is None:
            raise WorkflowError(
                "当前工程已接入 PostgreSQL 工程账本，但本次写操作没有可用的账本连接；"
                "为避免 workflow-state 领先于持久化回读，操作已拒绝。"
            )

        status = self.get_storage_status(project_dir.name)
        if status.get("status") in {"SYNCED", "CONNECTED"} and status.get("sync_status") not in {
            "MISSING",
            "STALE",
            "PROJECT_MISMATCH",
        }:
            return

        # Rebuild the manifest and replay the project-scoped outbox once while the
        # project lock is held.  Only a verified read-back unlocks the mutation.
        self._refresh_manifest(project_dir)
        status = self.get_storage_status(project_dir.name)
        if status.get("status") not in {"SYNCED", "CONNECTED"} or status.get("sync_status") in {
            "MISSING",
            "STALE",
            "PROJECT_MISMATCH",
        }:
            raise WorkflowError(
                "当前工程的 PostgreSQL/MinIO 回读仍落后于 workflow-state；"
                "必须先完成项目级持久化同步，不能继续阶段写入。"
            )

    @contextmanager
    def _root_operation_lock(self):
        lock_path = self.root / ".orion-root-operation.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _project_archive_blockers(
        self,
        project_dir: Path,
        state: dict[str, Any],
    ) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        blocking = state.get("blocking") or {}
        if state.get("project_status") == "BLOCKED_HUMAN" or blocking:
            blockers.append(
                {
                    "type": "PENDING_DECISION",
                    "message": f"待人工确认：{str(blocking.get('reason') or '存在尚未完成的人工决定')}",
                }
            )
        pending_path = project_dir / "03-mapping-review/pending-confirmations.json"
        if pending_path.exists():
            unresolved = [
                item for item in self._read_json(pending_path) if item.get("status") != "RESOLVED"
            ]
            if unresolved and not any(item["type"] == "PENDING_DECISION" for item in blockers):
                blockers.append(
                    {
                        "type": "PENDING_DECISION",
                        "message": f"S3 仍有 {len(unresolved)} 项人工决定未完成",
                    }
                )
        if (
            state.get("current_stage") == "S7"
            and state.get("stage_statuses", {}).get("S7") == "RUNNING"
        ):
            blockers.append(
                {
                    "type": "PENDING_RELEASE_OPERATION",
                    "message": "S7 发布决定尚未完成，请先发布或明确暂不发布",
                }
            )
        input_root_value = os.getenv("ORION_DOCUMENT_INPUT_ROOT")
        if input_root_value:
            jobs_root = Path(input_root_value).expanduser().resolve() / ".orion-s0-jobs"
            for status_path in sorted(jobs_root.glob("*/status.json")):
                try:
                    job = self._read_json(status_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                request_path = status_path.parent / "request.json"
                request = self._read_json(request_path) if request_path.exists() else {}
                linked_project = str(job.get("project_id") or request.get("project_id") or "")
                if linked_project != state.get("project_id"):
                    continue
                job_status = str(job.get("status") or "UNKNOWN")
                if job_status in {"QUEUED", "RUNNING", "READY_FOR_REVIEW"}:
                    blockers.append(
                        {
                            "type": "ACTIVE_DOCUMENT_JOB",
                            "message": (
                                f"资料任务 {job.get('job_id') or status_path.parent.name} 状态为 {job_status}，"
                                "请先完成、取消或提交复核"
                            ),
                        }
                    )
        return blockers

    def _require_stage(self, project_id: str, stage: str) -> tuple[Path, dict[str, Any]]:
        project_dir = self._resolve_project(project_id)
        state = self._read_state(project_dir)
        if state["current_stage"] != stage or state["stage_statuses"][stage] != "RUNNING":
            raise WorkflowError(
                f"项目当前不能写入 {stage}；当前阶段={state['current_stage']}，"
                f"状态={state['stage_statuses'][stage]}。"
            )
        return project_dir, state

    @staticmethod
    def _mark_artifacts_regenerated(
        state: dict[str, Any],
        regenerated_paths: set[str],
    ) -> None:
        """Make only rewritten paths current while revision snapshots preserve old bytes."""

        lifecycle = dict(state.get("artifact_lifecycle") or {})
        state["artifact_lifecycle"] = {
            path: metadata for path, metadata in lifecycle.items() if path not in regenerated_paths
        }

    @staticmethod
    def _mapping_runtime_artifact_paths(
        stage_dir: Path,
        state: dict[str, Any],
    ) -> set[str]:
        """Return current and superseded S3 runtime paths handled by one rewrite."""

        prefix = "03-mapping-review/runtime/"
        paths = {
            path for path in (state.get("artifact_lifecycle") or {}) if path.startswith(prefix)
        }
        runtime_dir = stage_dir / "runtime"
        if runtime_dir.exists():
            paths.update(
                f"{prefix}{path.relative_to(runtime_dir).as_posix()}"
                for path in runtime_dir.rglob("*")
                if path.is_file()
            )
        return paths

    @staticmethod
    def _normalize_operation_id(operation_id: str | None) -> str | None:
        normalized = str(operation_id or "").strip() or None
        if normalized and not REQUEST_ID_PATTERN.fullmatch(normalized):
            raise WorkflowError(
                "operation_id 必须为 8～128 位字母、数字、点、下划线、冒号或连字符。"
            )
        return normalized

    def _operation_replay(
        self,
        project_dir: Path,
        operation_id: str | None,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        if operation_id is None:
            return None
        receipt_path = project_dir / ".operation-receipts" / f"{operation_id}.json"
        if not receipt_path.exists():
            return None
        receipt = self._read_json(receipt_path)
        if receipt.get("request_fingerprint") != request_fingerprint:
            raise WorkflowError("operation_id 已用于另一组参数，重复提交已拒绝。")
        return receipt

    def _record_operation_receipt(
        self,
        project_dir: Path,
        operation_id: str | None,
        request_fingerprint: str,
        operation: str,
    ) -> None:
        if operation_id is None:
            return
        self._write_json(
            project_dir / ".operation-receipts" / f"{operation_id}.json",
            {
                "operation_id": operation_id,
                "operation": operation,
                "request_fingerprint": request_fingerprint,
                "completed_at": _now(),
            },
        )

    def _pass_stage(
        self,
        project_dir: Path,
        state: dict[str, Any],
        stage: str,
        next_stage: str,
        payload: dict[str, Any],
    ) -> None:
        stage_dir = project_dir / STAGE_FOLDERS[stage]
        state["stage_statuses"][stage] = "PASSED"
        selected_next_stage = next_stage
        active_revision = state.get("active_revision") or {}
        required = set(active_revision.get("required_revalidation_stages") or [])
        reused_stages: list[str] = []
        if required and active_revision.get("status") == "IN_PROGRESS":
            for candidate in STAGES[STAGES.index(stage) + 1 :]:
                candidate_status = state["stage_statuses"].get(candidate)
                if candidate in required or candidate_status not in {"PASSED", "NOT_APPLICABLE"}:
                    selected_next_stage = candidate
                    break
                reused_stages.append(candidate)
            if reused_stages:
                active_revision.setdefault("reused_unchanged_stages", [])
                active_revision["reused_unchanged_stages"] = list(
                    dict.fromkeys([*active_revision["reused_unchanged_stages"], *reused_stages])
                )
        state["stage_statuses"][selected_next_stage] = "RUNNING"
        state["current_stage"] = selected_next_stage
        state["project_status"] = "IN_PROGRESS"
        state["blocking"] = None
        state["last_error"] = None
        state.pop("last_failed_submission", None)
        state["stage_fingerprints"][stage] = {
            "input": _fingerprint(payload),
            "output": self._stage_fingerprint(stage_dir),
            "profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
        }

    @staticmethod
    def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
        """让 S0 上线前的项目保持真实，不反向伪造已经完成的证据阶段。"""

        normalized = dict(state)
        statuses = dict(normalized.get("stage_statuses") or {})
        if "S0" not in statuses:
            statuses = {"S0": "NOT_APPLICABLE", **statuses}
            normalized["legacy_without_s0"] = True
        normalized["stage_statuses"] = statuses
        normalized.setdefault("revision", 0)
        normalized.setdefault("stage_fingerprints", {})
        normalized.setdefault("artifact_lifecycle", {})
        normalized.setdefault("assurance_profile", "PRODUCTION")
        normalized.setdefault(
            "production_gate_policy_version",
            PRODUCTION_GATE_POLICY_VERSION,
        )
        return normalized

    def _read_state(self, project_dir: Path) -> dict[str, Any]:
        return self._normalize_state(self._read_json(project_dir / "workflow-state.json"))

    def _mark_failed(
        self,
        project_dir: Path,
        state: dict[str, Any],
        stage: str,
        error: WorkflowGateError,
        *,
        input_payload: Any | None = None,
    ) -> None:
        if input_payload is not None:
            state["last_failed_submission"] = {
                "stage": stage,
                "gate": error.gate_id,
                "input_fingerprint": _fingerprint(input_payload),
                "validator_fingerprint": self._validator_fingerprint(),
                "recorded_at": _now(),
            }
        state["stage_statuses"][stage] = "FAILED"
        state["project_status"] = "QA_FAILED"
        state["last_error"] = {"gate": error.gate_id, "message": str(error), "at": _now()}
        state["blocking"] = {"gate": error.gate_id, "reason": str(error)}
        state["resume_point"] = f"{stage}: 修正门禁失败后调用 retry_failed_stage"
        self._save_state(project_dir, state)
        self._append_event(
            project_dir,
            "STAGE_FAILED",
            state,
            {
                "stage": stage,
                "gate": error.gate_id,
                "message": str(error),
                "execution_mode": "ATOMIC_GATE",
                "partial_success_supported": False,
                "current_artifacts_promoted": False,
                "diagnostic_artifacts_policy": "PRESERVE_NON_CURRENT",
            },
        )
        self._refresh_manifest(project_dir)

    @staticmethod
    def _validator_fingerprint() -> str:
        """Identify the deployed validator tree behind a failed submission."""

        manifest_path = Path(__file__).resolve().parents[2] / "artifacts/build-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            fingerprint = str(manifest.get("source", {}).get("tree_sha256") or "")
            if re.fullmatch(r"sha256:[a-f0-9]{64}", fingerprint):
                return fingerprint
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return _file_checksum(Path(__file__))

    def _reject_identical_failed_submission(
        self,
        state: dict[str, Any],
        stage: str,
        input_payload: Any,
    ) -> None:
        previous = state.get("last_failed_submission") or {}
        if (
            previous.get("stage") == stage
            and previous.get("input_fingerprint") == _fingerprint(input_payload)
            and previous.get("validator_fingerprint") == self._validator_fingerprint()
        ):
            raise WorkflowGateError(
                "G-REPEATED-FAILED-SUBMISSION",
                f"{stage} 的相同载荷已被门禁 {previous.get('gate') or 'UNKNOWN'} 拒绝；"
                "平台已阻止原样重试。请先修正技术输入并通过只读预检。",
            )

    def _verify_project_integrity(
        self,
        project_dir: Path,
        state: dict[str, Any],
        *,
        stages: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        selected_stages = stages or STAGES
        stage_results: list[dict[str, Any]] = []
        failed = False
        legacy = False
        fingerprints = state.get("stage_fingerprints") or {}
        statuses = state.get("stage_statuses") or {}
        for stage in selected_stages:
            stage_status = str(statuses.get(stage) or "PENDING")
            if stage_status not in {"PASSED", "NOT_APPLICABLE"}:
                continue
            recorded = fingerprints.get(stage) or {}
            profile = str(recorded.get("profile") or "")
            recorded_output = str(recorded.get("output") or "")
            current_output = self._stage_fingerprint(project_dir / STAGE_FOLDERS[stage])
            if profile != FORMAL_ARTIFACT_FINGERPRINT_PROFILE:
                result_status = "LEGACY_UNVERIFIED"
                legacy = True
            elif not recorded_output:
                result_status = "MISSING_FINGERPRINT"
                failed = True
            elif recorded_output != current_output:
                result_status = "MISMATCH"
                failed = True
            else:
                result_status = "VERIFIED"
            stage_results.append(
                {
                    "stage": stage,
                    "stage_status": stage_status,
                    "status": result_status,
                    "profile": profile or "legacy-whole-directory",
                    "recorded_output": recorded_output or None,
                    "current_output": current_output,
                }
            )

        archived_audit = sorted(
            path for path, metadata in (state.get("artifact_lifecycle") or {}).items()
            if str(metadata.get("stage") or "") in selected_stages
            and self._verified_archived_review(project_dir, state, path, metadata)
        )
        invalidated = [
            path
            for path, metadata in (state.get("artifact_lifecycle") or {}).items()
            if str(metadata.get("stage") or "") in selected_stages
            and metadata.get("status") == "INVALIDATED" and path not in archived_audit
        ]
        if invalidated:
            failed = True

        trace_path = project_dir / "events/agent-trace.jsonl"
        events: list[dict[str, Any]] = []
        parse_errors: list[str] = []
        if trace_path.exists():
            for line_number, line in enumerate(
                trace_path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    parse_errors.append(f"第 {line_number} 行不是有效 JSON")

        chain_errors = list(parse_errors)
        hashed_event_count = 0
        legacy_event_count = 0
        previous: dict[str, Any] | None = None
        for index, event in enumerate(events, start=1):
            if int(event.get("sequence") or 0) != index:
                chain_errors.append(f"事件 {index} 的 sequence 不连续")
            expected_previous = (
                previous.get("event_hash") or _fingerprint(previous)
                if previous is not None
                else None
            )
            if event.get("previous_event_hash") != expected_previous:
                chain_errors.append(f"事件 {index} 的 previous_event_hash 不匹配")
            stored_hash = str(event.get("event_hash") or "")
            if stored_hash:
                hashed_event_count += 1
                body = dict(event)
                body.pop("event_hash", None)
                if stored_hash != _fingerprint(body):
                    chain_errors.append(f"事件 {index} 的 event_hash 不匹配")
            else:
                legacy_event_count += 1
            previous = event
        if chain_errors:
            audit_status = "FAILED"
            failed = True
        elif legacy_event_count:
            audit_status = "PARTIAL_LEGACY_ANCHORED"
            legacy = True
        else:
            audit_status = "VERIFIED"

        overall = "FAILED" if failed else "PARTIAL" if legacy else "PASSED"
        return {
            "project_id": project_dir.name,
            "status": overall,
            "fingerprint_profile": FORMAL_ARTIFACT_FINGERPRINT_PROFILE,
            "stages": stage_results,
            "invalidated_artifacts": sorted(invalidated),
            "archived_audit_artifacts": archived_audit,
            "audit": {
                "status": audit_status,
                "event_count": len(events),
                "hashed_event_count": hashed_event_count,
                "legacy_event_count": legacy_event_count,
                "errors": chain_errors,
                "last_sequence": events[-1].get("sequence") if events else None,
                "last_event_id": events[-1].get("event_id") if events else None,
                "last_event_hash": events[-1].get("event_hash") if events else None,
            },
            "writes_performed": False,
            "verified_at": _now(),
        }

    def _status_payload(self, project_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
        payload = self._normalize_state(state)
        payload["project_path"] = str(project_dir)
        current_stage = str(
            payload.get("current_stage")
            or next(
                (stage for stage in STAGES
                 if payload.get("stage_statuses", {}).get(stage)
                 not in {"PASSED", "NOT_APPLICABLE"}),
                "S7",
            )
        )
        contract_version = project_stage_contract_version(state)
        payload["stage_contract_version"] = contract_version
        payload["stage_contracts"] = stage_contract_catalog(contract_version)
        payload["current_stage_contract"] = stage_contract(current_stage, contract_version)
        source_scope_path = project_dir / "00-document-evidence/source-scope.json"
        if source_scope_path.is_file():
            payload["source_scope"] = self._read_json(source_scope_path)
        design_review_path = project_dir / "04-ontology-design/competency-question-review.json"
        if design_review_path.is_file():
            payload["competency_question_review"] = self._read_json(design_review_path)
        payload["contract_chain_version"] = CONTRACT_CHAIN_VERSION
        payload["contract_chain"] = validate_project_contract_chain(project_dir)
        payload["stage_execution_policy"] = {
            "stages": [f"S{index}" for index in range(1, 8)],
            "mode": "ATOMIC_GATE",
            "partial_success_supported": False,
            "failure_effect": "STAGE_FAILED",
            "diagnostic_artifacts_policy": "PRESERVE_NON_CURRENT",
            "current_artifacts_require_stage_pass": True,
            "preflight_commit": "TOKENIZED_SNAPSHOT",
            "diagnostics": "COLLECT_ALL_STRUCTURAL_THEN_FORMAL_GATE",
            "rollback_scope": "COMPONENT_DEPENDENCY_WHEN_DECLARED",
        }
        capability_plan_path = project_dir / "02-semantic-recognition/capability-plan.json"
        if capability_plan_path.is_file():
            payload["capability_plan"] = self._read_json(capability_plan_path)
        payload["platform_capability_catalog"] = platform_capability_catalog()
        document_job_path = project_dir / ".s0-document-job.json"
        if document_job_path.is_file():
            from services.ingestion.document_jobs import read_document_job

            reference = self._read_json(document_job_path)
            try:
                document_job = read_document_job(
                    project_id=project_dir.name, job_id=str(reference.get("job_id") or "")
                )
                if (reference.get("project_revision") == state.get("revision")
                        or (document_job.get("workflow") or {}).get("revision") == state.get("revision")):
                    payload["document_ingestion_job"] = document_job
            except WorkflowError as exc:
                if reference.get("project_revision") == state.get("revision"):
                    payload["document_ingestion_job"] = {"status": "UNAVAILABLE", "message": str(exc)}
        s6_run_path = project_dir / "06-quality-validation/run-status.json"
        if s6_run_path.is_file():
            payload["s6_validation_run"] = self._read_json(s6_run_path)
        execution_path = project_dir / ".stage-executions" / f"{current_stage}.json"
        if execution_path.is_file():
            execution = self._read_json(execution_path)
            payload["stage_execution"] = {
                key: execution.get(key)
                for key in (
                    "stage", "status", "execution_id", "attempt", "heartbeat_at",
                    "lease_expires_at", "checkpoints", "last_error",
                )
            }
        payload["stage_liveness"] = classify_stage_liveness(project_dir, state)
        publication_path = project_dir / "07-release/publication.json"
        if publication_path.exists():
            publication = self._read_json(publication_path)
            payload["publication"] = publication
            payload["release_contract"] = self._release_contract_summary(
                project_dir,
                publication,
            )
            release_version = str(publication.get("release_version") or "")
            automation = self._release_deployment_automation
            if automation is not None and release_version:
                payload["realtime_deployment"] = automation.status(
                    project_dir=project_dir,
                    release_version=release_version,
                )
            elif state.get("release_runtime_status"):
                payload["realtime_deployment"] = state["release_runtime_status"]
            elif (
                publication.get("realtime_query_capability") != "PACKAGED_ARTIFACT_VERIFIED"
                and publication.get("document_runtime_capability")
                != "CURRENT_VERSION_SEARCH_PACKAGED"
            ):
                payload["realtime_deployment"] = {
                    "schema_version": 1,
                    "project_id": project_dir.name,
                    "release_version": release_version,
                    "state": "NOT_APPLICABLE",
                    "artifact_verified": True,
                    "runtime_verified": False,
                }
            else:
                payload["realtime_deployment"] = {
                    "schema_version": 1,
                    "project_id": project_dir.name,
                    "release_version": release_version,
                    "state": "AUTOMATION_DISABLED",
                    "artifact_verified": False,
                    "runtime_verified": False,
                    "degraded_reason": "ORION_S7_AUTO_DEPLOY is not enabled",
                }
        blocking = state.get("blocking") or {}
        if blocking.get("gate") == "GATE-1":
            confirmations = self._read_json(
                project_dir / "03-mapping-review/pending-confirmations.json"
            )
            payload["next_confirmation"] = next(
                (item for item in confirmations if item["status"] != "RESOLVED"),
                None,
            )
            resolved_count = sum(item["status"] == "RESOLVED" for item in confirmations)
            payload["confirmation_progress"] = {
                "current": resolved_count + 1,
                "total": len(confirmations),
                "resolved": resolved_count,
            }
        trace_path = project_dir / "events/agent-trace.jsonl"
        events: list[dict[str, Any]] = []
        if trace_path.exists():
            events = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        hashed_event_count = sum(bool(event.get("event_hash")) for event in events)
        hash_chain_status = (
            "PRESENT_UNVERIFIED"
            if events and hashed_event_count == len(events)
            else "PARTIAL_LEGACY_UNVERIFIED"
            if hashed_event_count
            else "LEGACY_UNCHAINED"
            if events
            else "EMPTY"
        )
        revision_history = self.get_revision_history(project_dir.name)
        payload["audit"] = {
            "event_count": len(events),
            "hash_chain": hash_chain_status,
            "hashed_event_count": hashed_event_count,
            "last_event_id": events[-1].get("event_id") if events else None,
            "last_event_hash": events[-1].get("event_hash") if events else None,
            "revision_count": revision_history["count"],
            "latest_revision": (
                revision_history["revisions"][0] if revision_history["revisions"] else None
            ),
        }
        payload["integrity"] = state.get("last_integrity_verification") or {
            "status": "NOT_VERIFIED",
            "message": "完整校验仅在显式校验或发布前执行，状态轮询不会全量重哈希。",
        }
        return payload

    def _release_contract_summary(
        self,
        project_dir: Path,
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        """标记历史发布包，避免把旧版 CQ 总数通过误称为新版合同验证。"""

        release_version = str(publication.get("release_version") or "").strip()
        package_dir = project_dir / "07-release" / f"ontology-engineering-package-{release_version}"
        legacy = {
            "status": "LEGACY_UNVERIFIED",
            "contract_version": None,
            "verification_scope": "METADATA_PRECHECK",
            "delivery_export_precheck": "BLOCKED",
            "message": (
                "该版本没有记录新版 CQ 服务端答案合同；"
                "正式资产保持不可变，如需新版验收请创建修订并重跑 S4-S7。"
            ),
        }
        if not release_version:
            return legacy
        snapshot_path = package_dir / "04-发布信息/release-snapshot.json"
        cq_report_path = package_dir / "03-质量结论/competency-question-report.json"
        manifest_path = package_dir / "manifest.json"
        if not all(path.is_file() for path in (snapshot_path, cq_report_path, manifest_path)):
            return legacy
        try:
            snapshot = self._read_json(snapshot_path)
            cq_report = self._read_json(cq_report_path)
            lineage = snapshot.get("competency_question_lineage") or {}
            current_hashes_match = publication.get("package_manifest_sha256") == _file_checksum(
                manifest_path
            ) and publication.get("release_snapshot_sha256") == _file_checksum(snapshot_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return legacy
        if (
            current_hashes_match
            and snapshot.get("project_id") == project_dir.name
            and snapshot.get("release_version") == release_version
            and self._release_snapshot_supports_new_contract(snapshot)
            and lineage.get("status") == "VERIFIED"
            and lineage.get("contract_version") in {"cq-answer-v1", "cq-answer-v2"}
            and int(cq_report.get("schema_version") or 0) >= 2
            and cq_report.get("status") == "PASSED"
            and cq_report.get("validation_mode") in SERVER_EXECUTED_CQ_VALIDATION_MODES
        ):
            contract_version = str(lineage.get("contract_version"))
            return {
                "status": (
                    "SEMANTIC_CONTRACT_RECORDED"
                    if contract_version == "cq-answer-v2"
                    else "NEW_CONTRACT_RECORDED"
                ),
                "contract_version": contract_version,
                "verification_scope": "METADATA_PRECHECK",
                "delivery_export_precheck": "ELIGIBLE_FOR_FULL_VERIFICATION",
                "message": (
                    "发布时已记录新版 CQ 合同和不可变快照；实际导出仍会重新校验发布包全部文件哈希。"
                ),
            }
        return legacy

    @staticmethod
    def _release_snapshot_supports_new_contract(snapshot: dict[str, Any]) -> bool:
        """Accept an audited S4-S7 revision even when S0-S3 evidence is inherited.

        A full PASSED snapshot remains the strongest signal. PARTIAL is accepted
        only when its partiality is limited to legacy S0-S3 fingerprints, while
        the ontology design, build, validation and pre-publish audit chain are all
        verified under the current formal-artifact profile.
        """

        integrity_status = str(snapshot.get("integrity_status") or "")
        if integrity_status == "PASSED":
            return True
        if integrity_status != "PARTIAL":
            return False
        fingerprints = snapshot.get("formal_stage_fingerprints") or {}
        if not isinstance(fingerprints, dict):
            return False
        for stage in ("S4", "S5", "S6"):
            item = fingerprints.get(stage) or {}
            if (
                item.get("verification_status") != "VERIFIED"
                or item.get("profile") != FORMAL_ARTIFACT_FINGERPRINT_PROFILE
            ):
                return False
        for stage, item in fingerprints.items():
            verification_status = str((item or {}).get("verification_status") or "")
            if verification_status not in {"VERIFIED", "LEGACY_UNVERIFIED"}:
                return False
            if verification_status == "LEGACY_UNVERIFIED" and stage not in {
                "S0",
                "S1",
                "S2",
                "S3",
            }:
                return False
        return (snapshot.get("pre_publish_chain") or {}).get("verification_status") == "VERIFIED"

    def _resolve_project(self, project_id: str | None) -> Path:
        if project_id is None:
            projects = self.list_projects()["projects"]
            if not projects:
                raise WorkflowError("还没有 ORION 本体工程项目。")
            project_id = projects[0]["project_id"]
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise WorkflowError("project_id 格式无效。")
        project_dir = (self.root / project_id).resolve()
        if project_dir.parent != self.root or not (project_dir / "workflow-state.json").exists():
            raise WorkflowError(f"不存在项目：{project_id}")
        return project_dir

    def _verified_archived_review(self, project_dir: Path, state: dict[str, Any],
                                  relative: str, metadata: dict[str, Any]) -> bool:
        """Recognize only immutable S4 review archives, never current design assets."""
        if (not re.fullmatch(r"04-ontology-design/review-history/[a-f0-9]{32}\.json", relative)
                or metadata.get("stage") != "S4" or metadata.get("status") != "INVALIDATED"
                or state.get("stage_statuses", {}).get("S4") != "PASSED"):
            return False
        revision_id = str(metadata.get("invalidated_by_revision") or "")
        if not re.fullmatch(r"REV-\d{8}T\d{6}-[A-F0-9]{8}", revision_id):
            return False
        snapshot_relative = f"revisions/{revision_id}/before/{relative}"
        if metadata.get("historical_snapshot") != snapshot_relative:
            return False
        current = project_dir / relative
        snapshot = project_dir / snapshot_relative
        manifest = project_dir / "revisions" / revision_id / "before-manifest.json"
        try:
            for path in (current, snapshot, manifest):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(project_dir.resolve()):
                    return False
                if any(parent.is_symlink() for parent in path.parents if parent != project_dir and parent.is_relative_to(project_dir)):
                    return False
            entries = self._read_json(manifest).get("files", [])
            matching = [entry for entry in entries if entry.get("path") == relative]
            if len(matching) != 1:
                return False
            entry = matching[0]
            return (entry.get("snapshot_path") == f"before/{relative}"
                    and entry.get("size") == current.stat().st_size == snapshot.stat().st_size
                    and entry.get("sha256") == _file_checksum(current) == _file_checksum(snapshot))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _save_state(self, project_dir: Path, state: dict[str, Any]) -> None:
        lifecycle = dict(state.get("artifact_lifecycle") or {})
        for relative, metadata in list(lifecycle.items()):
            stage = str(metadata.get("stage") or "")
            if stage not in STAGES or state.get("stage_statuses", {}).get(stage) != "PASSED":
                continue
            if (
                Path(relative).name == "decisions.jsonl"
                and relative.startswith(("03-mapping-review/", "04-ontology-design/"))
            ):
                # Decision journals are append-only audit inputs. A rollback must
                # preserve them so basis-fingerprint logic can decide whether an
                # earlier choice is reusable. Once the owning stage passes again,
                # the unchanged journal is revalidated rather than left as a stale
                # INVALIDATED artifact that poisons whole-project integrity.
                lifecycle.pop(relative, None)
                continue
            if relative.startswith("04-ontology-design/review-history/"):
                if self._verified_archived_review(project_dir, state, relative, metadata):
                    lifecycle.pop(relative, None)
                # A modified or unverifiable archive is not a freshly rebuilt
                # active artifact; preserve its invalidation for reconciliation.
                continue
            current_path = project_dir / relative
            snapshot_relative = str(metadata.get("historical_snapshot") or "")
            snapshot_path = project_dir / snapshot_relative if snapshot_relative else None
            if (
                current_path.is_file()
                and snapshot_path is not None
                and snapshot_path.is_file()
                and _file_checksum(current_path) != _file_checksum(snapshot_path)
            ):
                lifecycle.pop(relative, None)
        state["artifact_lifecycle"] = lifecycle
        active_revision = state.get("active_revision") or {}
        required = active_revision.get("required_revalidation_stages") or []
        has_invalidated_required_artifacts = any(
            str(metadata.get("stage") or "") in required for metadata in lifecycle.values()
        )
        if (
            required
            and all(
                state.get("stage_statuses", {}).get(stage) == "PASSED"
                or (
                    stage == "S0" and state.get("stage_statuses", {}).get(stage) == "NOT_APPLICABLE"
                )
                for stage in required
            )
            and not has_invalidated_required_artifacts
        ):
            active_revision["status"] = "COMPLETED"
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = _now()
        self._write_json(project_dir / "workflow-state.json", state)
        project_path = project_dir / "project.json"
        project = self._read_json(project_path)
        project["current_stage"] = state["current_stage"]
        project["status"] = state["project_status"]
        project["updated_at"] = state["updated_at"]
        self._write_json(project_path, project)

    def _append_event(
        self,
        project_dir: Path,
        event_type: str,
        state: dict[str, Any],
        details: dict[str, Any],
    ) -> None:
        trace_path = project_dir / "events/agent-trace.jsonl"
        previous: dict[str, Any] | None = None
        sequence = 1
        if trace_path.exists():
            lines = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
            if lines:
                previous = json.loads(lines[-1])
                sequence = int(previous.get("sequence") or len(lines)) + 1
        event_details = dict(details)
        context = getattr(self._project_lock_state, "preflight_context", None)
        if context and context["project_dir"] == str(project_dir):
            event_details["preflight_operation_id"] = context["operation"]["operation_id"]
        actor = str(
            event_details.get("actor")
            or event_details.get("requested_by")
            or event_details.get("decided_by")
            or "ORION_WORKFLOW"
        )
        previous_hash = None
        if previous:
            previous_hash = previous.get("event_hash") or _fingerprint(previous)
        event = {
            "event_id": f"EVT-{uuid.uuid4().hex[:12].upper()}",
            "sequence": sequence,
            "previous_event_hash": previous_hash,
            "event_type": event_type,
            "at": _now(),
            "actor": actor,
            "project_id": state["project_id"],
            "current_stage": state["current_stage"],
            "project_status": state["project_status"],
            "state_fingerprint": _fingerprint(
                {
                    "current_stage": state.get("current_stage"),
                    "project_status": state.get("project_status"),
                    "stage_statuses": state.get("stage_statuses"),
                    "stage_fingerprints": state.get("stage_fingerprints"),
                }
            ),
            "details": event_details,
        }
        event["event_hash"] = _fingerprint(event)
        self._append_jsonl(trace_path, event)

    def _revision_files(self, project_dir: Path, target_stage: str) -> list[Path]:
        files: list[Path] = []
        target_index = STAGES.index(target_stage)
        for stage in STAGES[target_index:]:
            stage_dir = project_dir / STAGE_FOLDERS[stage]
            if not stage_dir.exists():
                continue
            for path in sorted(stage_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative_parts = path.relative_to(stage_dir).parts
                if "_shared" in relative_parts or "assets" in relative_parts:
                    continue
                files.append(path)
        return files

    def _create_revision_snapshot(
        self,
        project_dir: Path,
        state: dict[str, Any],
        *,
        target_stage: str,
        reason: str,
        requested_by: str,
        required_revalidation: list[str],
    ) -> dict[str, Any]:
        revision_id = (
            f"REV-{datetime.now().astimezone():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8].upper()}"
        )
        revision_dir = project_dir / "revisions" / revision_id
        before_dir = revision_dir / "before"
        before_dir.mkdir(parents=True, exist_ok=False)
        manifest: list[dict[str, Any]] = []
        for source in self._revision_files(project_dir, target_stage):
            relative = source.relative_to(project_dir)
            target = before_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest.append(
                {
                    "path": relative.as_posix(),
                    "sha256": _file_checksum(source),
                    "size": source.stat().st_size,
                    "snapshot_path": target.relative_to(revision_dir).as_posix(),
                }
            )
        revision = {
            "revision_id": revision_id,
            "target_stage": target_stage,
            "reason": reason,
            "requested_by": requested_by,
            "status": "IN_PROGRESS",
            "required_revalidation_stages": required_revalidation,
            "created_at": _now(),
        }
        self._write_json(revision_dir / "revision.json", revision)
        self._write_json(revision_dir / "before-state.json", state)
        self._write_json(revision_dir / "before-manifest.json", {"files": manifest})
        return revision

    def _update_active_revision(self, project_dir: Path) -> None:
        state = self._read_state(project_dir)
        active = state.get("active_revision") or {}
        revision_id = active.get("revision_id")
        if not revision_id:
            return
        revision_dir = project_dir / "revisions" / str(revision_id)
        revision_path = revision_dir / "revision.json"
        if not revision_path.exists():
            return
        revision = self._read_json(revision_path)
        before_state = self._read_json(revision_dir / "before-state.json")
        before_entries = self._read_json(revision_dir / "before-manifest.json").get("files", [])
        before_map = {item["path"]: item for item in before_entries}
        after_entries = [
            {
                "path": path.relative_to(project_dir).as_posix(),
                "sha256": _file_checksum(path),
                "size": path.stat().st_size,
            }
            for path in self._revision_files(project_dir, revision["target_stage"])
        ]
        after_map = {item["path"]: item for item in after_entries}
        changes: list[dict[str, Any]] = []
        counts = {"added": 0, "modified": 0, "removed": 0, "unchanged": 0}
        text_suffixes = {".json", ".yaml", ".yml", ".md", ".ttl", ".txt", ".owl"}
        for relative in sorted(set(before_map) | set(after_map)):
            before = before_map.get(relative)
            after = after_map.get(relative)
            if before is None:
                change_type = "ADDED"
            elif after is None:
                change_type = "REMOVED"
            elif before["sha256"] != after["sha256"]:
                change_type = "MODIFIED"
            else:
                counts["unchanged"] += 1
                continue
            counts[change_type.lower()] += 1
            item: dict[str, Any] = {
                "change_type": change_type,
                "path": relative,
                "before_sha256": before.get("sha256") if before else None,
                "after_sha256": after.get("sha256") if after else None,
                "before_size": before.get("size") if before else None,
                "after_size": after.get("size") if after else None,
            }
            suffix = Path(relative).suffix.lower()
            if suffix == ".html":
                item["note"] = "HTML 报告已保留调整前版本，并用前后 SHA-256 标记变化。"
            elif before and after and suffix in text_suffixes:
                before_path = revision_dir / str(before["snapshot_path"])
                after_path = project_dir / relative
                if before_path.stat().st_size <= 524288 and after_path.stat().st_size <= 524288:
                    before_lines = before_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    after_lines = after_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    excerpt = list(
                        unified_diff(
                            before_lines,
                            after_lines,
                            fromfile=f"before/{relative}",
                            tofile=f"after/{relative}",
                            lineterm="",
                        )
                    )
                    item["diff_excerpt"] = "\n".join(excerpt[:160])
                    if len(excerpt) > 160:
                        item["diff_excerpt"] += "\n... 差异过长，已截断 ..."
            changes.append(item)

        desired_status = str(active.get("status") or "IN_PROGRESS")
        transitioned = revision.get("status") != desired_status
        revision["status"] = desired_status
        if desired_status == "COMPLETED" and not revision.get("completed_at"):
            revision["completed_at"] = _now()
        self._write_json(revision_path, revision)
        self._write_json(revision_dir / "after-state.json", state)
        diff_payload = {
            "revision_id": revision_id,
            "generated_at": _now(),
            "summary": counts,
            "changes": changes,
        }
        self._write_json(revision_dir / "diff.json", diff_payload)
        self._atomic_write(
            revision_dir / "change-report.html",
            render_change_report(
                revision_dir,
                self._read_json(project_dir / "project.json"),
                revision,
                diff_payload,
                before_state,
                state,
            ),
        )
        if transitioned and desired_status == "COMPLETED":
            self._append_event(
                project_dir,
                "REVISION_COMPLETED",
                state,
                {
                    "stage": revision["target_stage"],
                    "revision_id": revision_id,
                    "summary": counts,
                    "actor": revision["requested_by"],
                },
            )

    def _refresh_manifest(self, project_dir: Path) -> None:
        self._update_active_revision(project_dir)
        self._refresh_readmes(project_dir)
        files: list[dict[str, str]] = []
        state = self._read_state(project_dir)
        lifecycle = state.get("artifact_lifecycle") or {}
        for path in sorted(project_dir.rglob("*")):
            if not path.is_file() or path.name == "artifact-manifest.json":
                continue
            relative = path.relative_to(project_dir).as_posix()
            if (
                relative == ".orion-operation.lock"
                or relative == ".artifact-manifest.pending.json"
                or relative == ".storage-sync-status.json"
                or relative.startswith(".operation-previews/")
                or relative.startswith(".operation-receipts/")
                or relative.startswith(preflight_operations.DIRECTORY + "/")
                or relative.startswith(".preflight-submissions/")
                or any(
                    part.startswith(".ontology-engineering-package-")
                    for part in Path(relative).parts
                )
            ):
                continue
            display_name, purpose, artifact_type = self._artifact_description(relative)
            lifecycle_entry = lifecycle.get(relative) or {}
            files.append(
                {
                    "path": relative,
                    "sha256": _file_checksum(path),
                    "display_name": display_name,
                    "purpose": purpose,
                    "artifact_type": artifact_type,
                    "lifecycle_status": lifecycle_entry.get("status") or "CURRENT",
                    "historical_snapshot": lifecycle_entry.get("historical_snapshot"),
                }
            )
        manifest = {
            "project_id": project_dir.name,
            "generated_at": _now(),
            "files": files,
        }
        manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        manifest_path = project_dir / "artifact-manifest.json"
        if self._metadata_configured:
            prepared_manifest_path = project_dir / ".artifact-manifest.pending.json"
            self._atomic_write(prepared_manifest_path, manifest_content)
            self._fsync_directory(project_dir)
            expected_manifest_sha256 = _file_checksum(prepared_manifest_path)
            self._enqueue_metadata_sync(
                project_dir,
                enqueued_by="ORION_WORKFLOW_WRITE_AHEAD",
                artifact_manifest_sha256=expected_manifest_sha256,
                prepared_manifest_path=prepared_manifest_path.name,
            )
            os.replace(prepared_manifest_path, manifest_path)
            self._fsync_directory(project_dir)
        else:
            self._atomic_write(manifest_path, manifest_content)
        self._sync_metadata_store(project_dir)
        self._checkpoint_preflight_operation(project_dir)

    def _pending_metadata_outbox_entries(
        self,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if project_id and not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise WorkflowError("project_id 格式无效。")
        outbox_dir = self.root / ".storage-outbox"
        paths = (
            [outbox_dir / f"{project_id}.json"] if project_id else sorted(outbox_dir.glob("*.json"))
        )
        entries: list[dict[str, Any]] = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                entry = self._read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                entries.append(
                    {
                        "schema_version": 1,
                        "status": "CORRUPT_OUTBOX",
                        "project_id": path.stem,
                        "message": "metadata outbox 无法解析，需由 reconcile 重建。",
                        "outbox_path": path.relative_to(self.root).as_posix(),
                    }
                )
                continue
            entries.append(
                {
                    **entry,
                    "outbox_path": path.relative_to(self.root).as_posix(),
                }
            )
        return entries

    def _metadata_sync_target(
        self,
        project_dir: Path,
        *,
        artifact_manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        state = self._read_state(project_dir)
        manifest_path = project_dir / "artifact-manifest.json"
        trace_path = project_dir / "events/agent-trace.jsonl"
        event_head: dict[str, Any] = {}
        if trace_path.exists():
            lines = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
            if lines:
                event_head = json.loads(lines[-1])
        return {
            "project_revision": int(state.get("revision") or 0),
            "project_updated_at": state.get("updated_at"),
            "artifact_manifest_sha256": (
                artifact_manifest_sha256
                or (_file_checksum(manifest_path) if manifest_path.exists() else None)
            ),
            "event_sequence": event_head.get("sequence"),
            "event_id": event_head.get("event_id"),
            "event_hash": event_head.get("event_hash"),
        }

    def _enqueue_metadata_sync(
        self,
        project_dir: Path,
        *,
        enqueued_by: str,
        artifact_manifest_sha256: str | None = None,
        prepared_manifest_path: str | None = None,
    ) -> dict[str, Any]:
        outbox_path = self.root / ".storage-outbox" / f"{project_dir.name}.json"
        try:
            previous = self._read_json(outbox_path) if outbox_path.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
        entry = {
            "schema_version": 1,
            "status": "COMMITTED_SYNC_PENDING",
            "project_id": project_dir.name,
            "target": self._metadata_sync_target(
                project_dir,
                artifact_manifest_sha256=artifact_manifest_sha256,
            ),
            "prepared_manifest_path": prepared_manifest_path,
            "attempt_count": int(previous.get("attempt_count") or 0),
            "first_pending_at": previous.get("first_pending_at") or _now(),
            "last_enqueued_at": _now(),
            "last_attempt_at": previous.get("last_attempt_at"),
            "last_error": previous.get("last_error"),
            "last_reconciled_by": enqueued_by,
        }
        self._write_json(outbox_path, entry)
        self._fsync_directory(outbox_path.parent)
        return entry

    def _record_metadata_sync_pending(
        self,
        project_dir: Path,
        exc: Exception,
        *,
        reconciled_by: str,
    ) -> dict[str, Any]:
        outbox_path = self.root / ".storage-outbox" / f"{project_dir.name}.json"
        try:
            previous = self._read_json(outbox_path) if outbox_path.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            previous = self._enqueue_metadata_sync(
                project_dir,
                enqueued_by=reconciled_by,
            )
        entry = {
            "schema_version": 1,
            "status": "COMMITTED_SYNC_PENDING",
            "project_id": project_dir.name,
            "target": previous.get("target") or self._metadata_sync_target(project_dir),
            "prepared_manifest_path": previous.get("prepared_manifest_path"),
            "attempt_count": int(previous.get("attempt_count") or 0) + 1,
            "first_pending_at": previous.get("first_pending_at") or _now(),
            "last_enqueued_at": previous.get("last_enqueued_at") or _now(),
            "last_attempt_at": _now(),
            "last_error": {
                "error_type": type(exc).__name__,
                "message": "PostgreSQL 或 MinIO 同步/回读尚未完成。",
            },
            "last_reconciled_by": reconciled_by,
        }
        self._write_json(outbox_path, entry)
        self._fsync_directory(outbox_path.parent)
        receipt = {
            **entry,
            "message": "本地工作流已提交，外部账本同步待重放；阶段结果保持成功。",
            "checked_at": _now(),
        }
        self._write_storage_status(receipt)
        return receipt

    def _attempt_metadata_sync(
        self,
        project_dir: Path,
        *,
        reconciled_by: str,
    ) -> dict[str, Any]:
        if self._metadata_store is None:
            raise WorkflowError("尚未配置 PostgreSQL 工程账本。")
        outbox_path = self.root / ".storage-outbox" / f"{project_dir.name}.json"
        try:
            pending = self._read_json(outbox_path) if outbox_path.exists() else None
        except (OSError, ValueError, json.JSONDecodeError):
            pending = None
        if not pending:
            pending = self._enqueue_metadata_sync(
                project_dir,
                enqueued_by=reconciled_by,
            )
        target = pending["target"]
        try:
            expected_manifest_sha256 = target.get("artifact_manifest_sha256")
            manifest_path = project_dir / "artifact-manifest.json"
            current_target = self._metadata_sync_target(project_dir)
            current_manifest_sha256 = current_target.get("artifact_manifest_sha256")
            if expected_manifest_sha256 != current_manifest_sha256:
                source_coordinates = (
                    "project_revision",
                    "project_updated_at",
                    "event_sequence",
                    "event_id",
                    "event_hash",
                )
                if any(
                    current_target.get(field) != target.get(field) for field in source_coordinates
                ):
                    raise WorkflowError("预备清单生成后本地工程又发生变化，暂不安装旧清单。")
                prepared_name = str(pending.get("prepared_manifest_path") or "")
                if prepared_name != ".artifact-manifest.pending.json":
                    raise WorkflowError("待同步清单与本地清单不一致，且没有可恢复的预备清单。")
                prepared_path = project_dir / prepared_name
                if (
                    not prepared_path.is_file()
                    or _file_checksum(prepared_path) != expected_manifest_sha256
                ):
                    raise WorkflowError("预备清单缺失或校验失败，暂不执行外部同步。")
                os.replace(prepared_path, manifest_path)
                self._fsync_directory(project_dir)

            counts = self._metadata_store.sync_project(project_dir)
            read_back = self._metadata_store.status(project_dir.name)
            expected_event_hash = target.get("event_hash")
            expected_event_sequence = target.get("event_sequence")
            latest_event = read_back.get("latest_event") or {}
            if expected_event_hash and latest_event.get("event_hash") != expected_event_hash:
                raise WorkflowError("PostgreSQL 回读的事件链头与本地提交不一致。")
            if (
                expected_event_sequence is not None
                and latest_event.get("sequence") != expected_event_sequence
            ):
                raise WorkflowError("PostgreSQL 回读的事件序号与本地提交不一致。")
            current_target = self._metadata_sync_target(project_dir)
            if current_target != target:
                self._enqueue_metadata_sync(
                    project_dir,
                    enqueued_by=reconciled_by,
                )
                raise WorkflowError("外部同步期间本地提交发生变化，已保留最新补同步任务。")
            latest_pending = self._read_json(outbox_path)
            if latest_pending.get("target") != target:
                raise WorkflowError("补同步期间出现更新任务，当前任务不会清除队列。")
        except Exception as exc:
            return self._record_metadata_sync_pending(
                project_dir,
                exc,
                reconciled_by=reconciled_by,
            )

        if outbox_path.exists():
            outbox_path.unlink()
            self._fsync_directory(outbox_path.parent)
        artifact_store = getattr(self._metadata_store, "artifact_store", None)
        receipt = {
            "status": "SYNCED",
            "project_id": project_dir.name,
            "message": "文件工作区、PostgreSQL 工程账本与对象存储已同步并回读。",
            "target": target,
            "counts": counts,
            "read_back": read_back,
            "reconciled_by": reconciled_by,
            "services": {
                "postgresql": {
                    "status": "CONNECTED",
                    "purpose": "工程元数据、阶段状态、审计事件、决策与产物索引",
                    "schema": "orion_workflow",
                },
                "minio": {
                    "status": "CONNECTED" if artifact_store is not None else "DISABLED",
                    "purpose": "PDF、图片、压缩包和超过阈值的大文件对象",
                    "bucket": (
                        getattr(artifact_store, "bucket", None)
                        if artifact_store is not None
                        else None
                    ),
                    "object_references": counts.get("object_artifacts", 0),
                },
            },
            "checked_at": _now(),
        }
        self._write_storage_status(receipt)
        return receipt

    def _sync_metadata_store(self, project_dir: Path) -> None:
        if self._metadata_store is None:
            if self._metadata_configured:
                self._record_metadata_sync_pending(
                    project_dir,
                    WorkflowError("已配置的 PostgreSQL 工程账本当前不可用。"),
                    reconciled_by="ORION_WORKFLOW_AUTO_SYNC",
                )
            return
        self._attempt_metadata_sync(
            project_dir,
            reconciled_by="ORION_WORKFLOW_AUTO_SYNC",
        )

    def _write_storage_status(self, value: dict[str, Any]) -> None:
        self._write_json(self.root / ".storage-sync-status.json", value)
        project_id = str(value.get("project_id") or "").strip()
        if project_id and Path(project_id).name == project_id:
            project_dir = self.root / project_id
            if project_dir.is_dir():
                self._write_json(project_dir / ".storage-sync-status.json", value)

    def _refresh_readmes(self, project_dir: Path) -> None:
        state_path = project_dir / "workflow-state.json"
        project_path = project_dir / "project.json"
        if not state_path.exists() or not project_path.exists():
            return
        state = self._normalize_state(self._read_json(state_path))
        project = self._read_json(project_path)
        stage_rows: list[str] = []
        for folder, (stage_id, stage_name, goal) in STAGE_GUIDE.items():
            if self._joint_design_enabled(state):
                contract = stage_contract(stage_id)
                stage_name, goal = contract["name"], contract["purpose"]
            stage_dir = project_dir / folder
            artifacts = [
                path
                for path in sorted(stage_dir.iterdir() if stage_dir.exists() else [])
                if path.is_file() and path.name != "README.md"
            ]
            if artifacts:
                lines = [
                    f"# {stage_id} {stage_name} · 产物说明",
                    "",
                    f"> 阶段目标：{goal}",
                    "",
                    "| 产物 | 类型 | 用途 |",
                    "| --- | --- | --- |",
                ]
                for artifact in artifacts:
                    display_name, purpose, artifact_type = self._artifact_description(
                        f"{folder}/{artifact.name}"
                    )
                    lines.append(
                        f"| [{display_name}]({artifact.name}) | {artifact_type} | {purpose} |"
                    )
                lines.extend(
                    [
                        "",
                        "## 阅读建议",
                        "",
                        "先看本阶段 HTML 报告掌握结论，再按需要打开正式资产和 JSON/YAML 证据复核。",
                        "数据库事实、AI 理解和人工决定在报告中分别标注，不把 AI 推测冒充数据库事实。",
                        "",
                    ]
                )
                self._atomic_write(stage_dir / "README.md", "\n".join(lines))
            artifact_entry = (
                f"[{folder}/README.md]({folder}/README.md)" if artifacts else "尚未生成"
            )
            stage_rows.append(
                f"| {stage_id} | {stage_name} | {state.get('stage_statuses', {}).get(stage_id, 'PENDING')} | "
                f"{artifact_entry} |"
            )

        root_lines = [
            f"# {project.get('project_name', project_dir.name)} · 本体工程产物索引",
            "",
            f"- 项目编号：`{project_dir.name}`",
            f"- 业务领域：{project.get('domain', '—')}",
            f"- 当前阶段：{state.get('current_stage') or '阶段间待推进'}",
            f"- 项目状态：{state.get('project_status', '—')}",
            f"- 数据源：{project.get('datasource_label') or '—'}",
            "",
            "## 阶段索引",
            "",
            "| 阶段 | 名称 | 状态 | 产物说明 |",
            "| --- | --- | --- | --- |",
            *stage_rows,
            "",
            "## 如何阅读",
            "",
            "每个阶段目录都有独立 `README.md` 和 HTML 报告。右侧工程面板优先展示这两个入口：",
            "HTML 用于快速阅读结论，README 用于理解每个原始产物是做什么的。",
            "",
        ]
        self._atomic_write(project_dir / "README.md", "\n".join(root_lines))

    @staticmethod
    def _artifact_description(relative_path: str) -> tuple[str, str, str]:
        name = Path(relative_path).name
        if relative_path == "README.md":
            return (
                "本体工程产物索引",
                "汇总全部阶段状态，并提供每个阶段 README 与 HTML 报告的入口。",
                "说明文档",
            )
        if name in ARTIFACT_GUIDE:
            return ARTIFACT_GUIDE[name]
        if "/_shared/" in relative_path or "/assets/" in relative_path:
            return (name, "HTML 报告的离线字体、样式或图表资源。", "报告资源")
        suffix = Path(name).suffix.lower()
        artifact_type = {
            ".html": "HTML 报告",
            ".json": "JSON 证据",
            ".jsonl": "审计流水",
            ".yaml": "YAML 资产",
            ".yml": "YAML 资产",
            ".ttl": "RDF 资产",
            ".owl": "本体资产",
            ".md": "说明文档",
        }.get(suffix, "工程产物")
        return (name, "本体工程工作流生成或保留的辅助产物。", artifact_type)

    @staticmethod
    def _stage_fingerprint(stage_dir: Path) -> str:
        """Hash only formal machine-readable assets, not regenerated presentation files."""

        files = {
            path.relative_to(stage_dir).as_posix(): _file_checksum(path)
            for path in sorted(stage_dir.rglob("*"))
            if path.is_file()
            and path.name not in DERIVED_STAGE_ARTIFACT_NAMES
            and path.suffix.lower() != ".html"
            and not any(part in {"_shared", "assets"} for part in path.relative_to(stage_dir).parts)
            and not any(
                part.startswith("ontology-model-delivery-")
                for part in path.relative_to(stage_dir).parts
            )
        }
        return _fingerprint(files)

    @staticmethod
    def _verify_release_package(package_dir: Path) -> dict[str, Any]:
        manifest_path = package_dir / "manifest.json"
        if not manifest_path.is_file():
            raise WorkflowGateError("G-S7-MANIFEST", "发布包缺少 manifest.json。")
        try:
            manifest = OntologyWorkflowService._read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowGateError("G-S7-MANIFEST", "发布包 manifest.json 无法解析。") from exc
        declared = {
            str(item.get("path") or ""): str(item.get("sha256") or "")
            for item in manifest.get("files") or []
        }
        actual = {
            path.relative_to(package_dir).as_posix(): _file_checksum(path)
            for path in sorted(package_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
        if int(manifest.get("file_count") or -1) != len(actual):
            raise WorkflowGateError("G-S7-MANIFEST", "发布包文件数量与 manifest 不一致。")
        if declared != actual:
            raise WorkflowGateError("G-S7-MANIFEST", "发布包文件或 SHA-256 与 manifest 不一致。")
        return manifest

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        OntologyWorkflowService._atomic_write(
            path,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _write_yaml(path: Path, value: Any) -> None:
        OntologyWorkflowService._atomic_write(
            path,
            yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        )

    @staticmethod
    def _read_events(project_dir: Path) -> list[dict[str, Any]]:
        trace_path = project_dir / "events/agent-trace.jsonl"
        if not trace_path.exists():
            return []
        return [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
