from __future__ import annotations

from copy import deepcopy
from typing import Any

LEGACY_STAGE_CONTRACT_VERSION = "s0-s7-stage-contract-v1"
STAGE_CONTRACT_VERSION = "s0-s7-stage-contract-v2"


# This is the platform contract, not a project runbook.  Every ontology project
# is compiled against the same lifecycle semantics regardless of its domain.
STAGE_CONTRACTS: dict[str, dict[str, Any]] = {
    "S0": {
        "purpose": "固化原始资料、业务问题与证据定位。",
        "depends_on": [],
        "owner": "EVIDENCE_INGESTION",
        "failure_scope": ["SOURCE_EVIDENCE"],
        "execution": "ATOMIC",
    },
    "S1": {
        "purpose": "固化只读数据源、全量画像与不可变数据集谱系。",
        "depends_on": ["S0"],
        "owner": "DATA_PROFILING",
        "failure_scope": ["DATA_PROFILE"],
        "execution": "ATOMIC",
    },
    "S2": {
        "purpose": "形成可追溯语义、正式规则与平台能力执行计划。",
        "depends_on": ["S0", "S1"],
        "owner": "SEMANTIC_COMPILER",
        "failure_scope": ["SEMANTIC_MODEL"],
        "execution": "PREFLIGHT_THEN_COMMIT",
    },
    "S3": {
        "purpose": "编译 Mapping 与运行时合同，并完成类型和能力一致性预检。",
        "depends_on": ["S2"],
        "owner": "MAPPING_COMPILER",
        "failure_scope": ["MAPPING", "RUNTIME_MAPPING", "RUNTIME_RULES"],
        "execution": "PREFLIGHT_THEN_COMMIT",
    },
    "S4": {
        "purpose": "从已审 Mapping 确定性生成本体施工图和能力型 CQ。",
        "depends_on": ["S3"],
        "owner": "ONTOLOGY_DESIGN_COMPILER",
        "failure_scope": ["COMPETENCY_QUESTIONS", "ONTOLOGY_SCHEMA"],
        "execution": "PREFLIGHT_THEN_COMMIT",
    },
    "S5": {
        "purpose": "构建版本化 OWL、TTL、SHACL 并执行 HermiT。",
        "depends_on": ["S4"],
        "owner": "PLATFORM_BUILD",
        "failure_scope": ["ONTOLOGY_BINARY"],
        "execution": "PREFLIGHT_THEN_COMMIT",
    },
    "S6": {
        "purpose": "聚合执行 Mapping、HermiT、SHACL、CQ、Semantica 与真实来源回读。",
        "depends_on": ["S5"],
        "owner": "PLATFORM_VALIDATOR",
        "failure_scope": ["VALIDATION_EVIDENCE"],
        "execution": "CHECKPOINTED_PREFLIGHT_THEN_COMMIT",
    },
    "S7": {
        "purpose": "绑定不可变证据包，等待负责人明确批准后发布并回读运行时。",
        "depends_on": ["S6"],
        "owner": "RELEASE_CONTROL",
        "failure_scope": ["DEPLOYMENT_CONFIG"],
        "execution": "EXPLICIT_APPROVAL",
    },
}


LEGACY_STAGE_CONTRACTS = deepcopy(STAGE_CONTRACTS)

_STAGE_COLLABORATION = {
    "S0": ("目标与来源登记", "固化业务目标、允许的资料与数据库范围，并接入可追溯资料。", "SOURCE_SCOPE_AND_EVIDENCE", "说明目标和来源；明确要求不重复确认。", "直接使用受控快照返回的来源范围；技术标识失配通过恢复工具留痕移除自编 ID；用户明确更正资料时，以完整受控批次调用 replace_document_sources，保留 CQ 和数据库范围；不得自行扩展授权或重建工程。", ["create_ontology_project", "snapshot_workspace_sources", "preflight_workspace_snapshot", "reconcile_document_source_identities", "replace_document_sources", "start_document_ingestion_job", "get_document_ingestion_job", "commit_document_ingestion_job", "record_s0_scope_decision"], ["受控资料队列", "PaddleOCR 单页诊断"], ["source-scope.json", "cq-intake.json"]),
    "S1": ("资料与数据理解", "验证资料与单库、多库数据覆盖，保留来源身份和数据质量证据。", "SOURCE_UNDERSTANDING", "仅澄清来源、字段口径和完整性歧义。", "使用登记来源进行只读理解；资料工程由平台复用 S0 证据。", ["record_document_understanding", "record_data_understanding_from_datasets", "record_data_understanding"], ["Chat2DB 只读画像", "SourceBinding / SnapshotHub"], ["data-profile.json", "source-understanding.json"]),
    "S2": ("业务语义草案", "形成可追溯概念、规则候选与能力执行计划，供后续联合定稿。", "SEMANTIC_COMPILER", "补充缺失的关键业务政策；候选可查看。", "提出有来源的业务语义和规则，区分事实与模型推测。", ["record_semantic_candidates", "query_source_evidence"], ["模型语义识别", "平台能力目录"], ["ontology-candidates.yaml", "business-rule-candidates.json", "capability-plan.json"]),
    "S3": ("语义与映射评审", "确认业务口径并预检候选映射、运行规则与类型，供 S4 联合定稿。", "MAPPING_COMPILER", "裁决高影响业务歧义。", "按来源编制候选 Mapping；高影响歧义提交统一决策卡。", ["prepare_mapping_review", "resolve_mapping_option", "resolve_mapping_confirmation", "query_source_evidence"], ["平台映射与运行时预检", "受控只读事实验证"], ["mapping.yaml", "runtime/runtime-source.json", "pending-confirmations.json"]),
    "S4": ("联合设计定稿", "联合冻结本体设计、正式映射、规则、来源和业务验收问题。", "JOINT_DESIGN_COMPILER", "集中确认整套业务设计及预期结果。", "调用平台编译器生成完整设计摘要，等待绑定版本的明确批准。", ["generate_ontology_design", "prepare_ontology_design_review", "resolve_competency_question_review"], ["平台联合设计编译器"], ["ontology-design.yaml", "joint-design-baseline.json"]),
    "S5": ("构建与装配", "按批准基线构建 OWL/TTL/SHACL，装配映射和可审阅规则资产。", "PLATFORM_BUILD", "默认无需操作，可查看本体和规则。", "按批准设计调用 Protégé；不得临场改变业务语义。", ["record_ontology_build"], ["Protégé MCP", "HermiT", "SHACL", "规则资产装配"], ["ontology.owl", "ontology.ttl", "shapes.ttl"]),
    "S6": ("质量与业务验收", "基于同一批准基线验证逻辑、约束、映射、业务答案和真实推理证据。", "PLATFORM_VALIDATOR", "查看实际结果并提出业务异议；既定用例自动验证。", "执行真实验证并回读证据；技术失败由工程侧修复。", ["record_quality_validation"], ["HermiT", "SHACL", "Ontop", "Fuseki", "Semantica", "受控闭世界集合差"], ["quality-summary.json", "competency-question-report.json", "semantica-report.json"]),
    "S7": ("交付与发布", "形成工程与执行双视图交付包，经负责人批准发布并验证运行时。", "RELEASE_CONTROL", "明确批准具体版本，或选择暂缓。", "整理验收结果和发布摘要；不得代替负责人批准。", ["publish_ontology_package", "defer_ontology_publication", "resume_ontology_publication"], ["既有发布与运行时部署验证"], ["publication.json", "manifest.json", "package-contract.json"]),
}

for _stage, _details in _STAGE_COLLABORATION.items():
    _name, _purpose, _owner, _human, _model, _tools, _external, _artifacts = _details
    STAGE_CONTRACTS[_stage].update({
        "name": _name,
        "purpose": _purpose,
        "owner": _owner,
        "human_role": _human,
        "model_role": _model,
        "workflow_tools": _tools,
        "external_capabilities": _external,
        "artifacts": _artifacts,
        "human_confirmation": "REQUIRED" if _stage in {"S4", "S7"} else "ON_BUSINESS_AMBIGUITY",
    })
STAGE_CONTRACTS["S4"]["execution"] = "PREFLIGHT_THEN_JOINT_DESIGN_APPROVAL"

_HANDOFFS = {
    "S0": ("用户目标、当前资料引用与明确数据源范围", "目标与来源登记、资料证据及初始业务问题", "范围与输入模式一致；资料解析及定位通过；不适用仅落到子任务", "来源范围和资料原件", "登记、校验来源范围并执行受控资料处理"),
    "S1": ("S0 来源范围及真实资料/数据回执", "按来源可追溯的数据理解和完整性证据", "来源未越界，事实回执和覆盖范围闭合", "来源身份、理解报告和数据快照引用", "核验资料证据，或执行数据库全量画像与来源核对"),
    "S2": ("S0/S1 的业务问题及可定位证据", "概念、规则和能力计划草案", "语义有来源；派生判断有规则和边界用例；缺失能力明确暴露", "可追溯的设计候选", "校验候选结构、事实依据和能力可行性"),
    "S3": ("S2 语义及规则候选、S1 来源事实", "已审语义与候选映射、运行规则预检结果", "高影响歧义已裁决，映射类型与能力预检通过", "已审候选，最终设计在 S4 冻结", "执行映射预检、保存业务决定并编译运行候选"),
    "S4": ("S0 目标与来源、S3 已审映射与规则候选", "联合设计基线与负责人批准记录", "本体、映射、规则和验收预期一致并获得明确批准", "同一指纹下的本体、映射、规则与验收预期", "联合编译、生成中文摘要并冻结批准版本"),
    "S5": ("S4 已批准且未漂移的联合设计基线", "本体文件、约束与可审阅规则资产", "真实工具回执通过，构建产物覆盖批准设计", "可验证的候选构建资产", "检查基线并校验、装配构建制品"),
    "S6": ("S5 构建制品、批准的验收案例与真实来源", "质量、业务答案和推理执行证据", "全部适用质量门禁与实际业务验证通过", "绑定候选制品的验收证据", "执行逻辑、约束、映射及推理验证并汇总结果"),
    "S7": ("S6 通过回执、候选包及明确发布决定", "不可变工程交付包、发布记录和运行状态", "负责人批准具体版本；包校验和运行时可用分别验证", "不可覆盖的正式发布版本", "生成校验清单、发布并回读运行时"),
}
for _stage, (_inputs, _outputs, _acceptance, _freeze, _platform) in _HANDOFFS.items():
    STAGE_CONTRACTS[_stage].update({
        "inputs": [_inputs], "outputs": [_outputs], "acceptance": _acceptance,
        "freeze_boundary": _freeze, "platform_role": _platform,
    })


def project_stage_contract_version(state: dict[str, Any]) -> str:
    """Missing versions are historical v1, never silently upgraded on read."""
    version = str(state.get("stage_contract_version") or LEGACY_STAGE_CONTRACT_VERSION)
    if version not in {LEGACY_STAGE_CONTRACT_VERSION, STAGE_CONTRACT_VERSION}:
        raise ValueError(f"unsupported stage contract version: {version}")
    return version


def stage_contract(stage: str, contract_version: str = STAGE_CONTRACT_VERSION) -> dict[str, Any]:
    normalized = str(stage or "").strip().upper()
    version = project_stage_contract_version({"stage_contract_version": contract_version})
    contracts = STAGE_CONTRACTS if version == STAGE_CONTRACT_VERSION else LEGACY_STAGE_CONTRACTS
    if normalized not in contracts:
        raise ValueError(f"unknown ontology lifecycle stage: {stage}")
    return {
        "contract_version": version,
        "stage": normalized,
        **deepcopy(contracts[normalized]),
    }


def stage_contract_catalog(contract_version: str = STAGE_CONTRACT_VERSION) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "stages": [stage_contract(f"S{index}", contract_version) for index in range(8)],
    }
