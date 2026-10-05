"""Public ORION workflow MCP tool schemas (TOOLS).

Kept apart from the dispatcher in harness/orion_workflow_mcp.py so schema
changes and runtime dispatch evolve independently.
"""

from __future__ import annotations

from typing import Any

from harness.orion_workflow_contracts import (
    BUSINESS_RULE_CANDIDATE_SCHEMA,
    COMPETENCY_QUESTION_OVERRIDE_SCHEMA,
    CQ_SEMANTIC_ASSESSMENT_SCHEMA,
    INITIAL_COMPETENCY_QUESTION_SCHEMA,
    LOGICAL_AXIOM_SCHEMA,
    ONTOLOGY_CANDIDATE_SCHEMA,
    S3_AUTOMATIC_DECISION_SCHEMA,
    S3_CARD_SCHEMA,
    S3_RUNTIME_SUBMISSION_SCHEMA,
)
from harness.source_query_contract import SOURCE_QUERY_PLAN_SCHEMA


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


_SCOPE_STRING_LIST = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
_SCOPE_TABLE_FIELDS = {
    "authorized_tables": {
        **_SCOPE_STRING_LIST,
        "description": "原始库表或文件工作表的明确范围，空列表不等于全部授权；数据库表优先使用 schema.table。",
    },
    "authorized_columns": {"type": "object", "additionalProperties": _SCOPE_STRING_LIST},
    "excluded_tables": _SCOPE_STRING_LIST,
    "excluded_columns": {"type": "object", "additionalProperties": _SCOPE_STRING_LIST},
}
_SCOPE_FILE_FIELDS = {
    "source_id": {"type": "string", "minLength": 1, "description": "可选的平台已有文档/来源标识；推荐直接使用快照或预检返回的 source_scope 并省略此字段，禁止模型自行编造。"},
    "source_path": {"type": "string", "minLength": 1, "description": "现有受控资料快照路径。"},
    "path_scope": {"type": "string", "enum": ["FILE", "DIRECTORY"], "default": "FILE"},
    "source_sha256": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
    "source_name": {"type": "string", "minLength": 1},
}
SOURCE_SCOPE_SCHEMA = _object_schema(
    {
        "business_goal": {
            "type": "string",
            "minLength": 1,
            "description": "用户希望解决的业务问题与结果范围，由当前需求提炼；不代替原始 CQ。",
        },
        "sources": {
            "type": "array",
            "description": "逐个登记明确来源；多库使用多个 DATABASE 条目，不能拼成一个展示标签。",
            "items": {
                "oneOf": [
                    {
                        **_object_schema(
                            {"kind": {"const": "DOCUMENT"}, **_SCOPE_FILE_FIELDS}, ["kind"]
                        ),
                        "anyOf": [{"required": [key]} for key in ("source_id", "source_path", "source_sha256")],
                    },
                    {
                        **_object_schema(
                            {"kind": {"const": "STRUCTURED_FILE"}, **_SCOPE_FILE_FIELDS, **_SCOPE_TABLE_FIELDS},
                            ["kind"],
                        ),
                        "anyOf": [{"required": [key]} for key in ("source_id", "source_path", "source_sha256")],
                    },
                    {
                        **_object_schema(
                            {
                                "kind": {"const": "DATABASE"},
                                "source_id": {"type": "string", "minLength": 1, "description": "既有 SourceBinding/连接目录中的来源 ID。"},
                                "datasource_label": {"type": "string", "minLength": 1},
                                "database": {"type": "string", "minLength": 1},
                                "schemas": _SCOPE_STRING_LIST,
                                **_SCOPE_TABLE_FIELDS,
                            },
                            ["kind"],
                        ),
                        "anyOf": [{"required": [key]} for key in ("source_id", "datasource_label", "database")],
                    },
                ]
            },
        },
        "excluded_source_refs": {
            **_SCOPE_STRING_LIST,
            "description": "明确排除的来源 ID、库名、文件路径或哈希；以斜线结尾的路径排除该目录内的资料。",
        },
    },
    ["sources"],
)
SOURCE_SCOPE_SCHEMA["description"] = (
    "由平台/模型依据用户自然语言、受控资料快照与已发现连接生成的来源范围，"
    "文件来源直接使用 snapshot_workspace_sources 或 preflight_workspace_snapshot 返回的 source_scope，"
    "按已验证的路径、名称、哈希和类型登记，不补写自编 source_id。"
    "业务用户无需填写技术 JSON。DATABASE_ONLY 禁文件来源，DOCUMENT_ONLY 禁外部数据库；"
    "表格结构导入保留 STRUCTURED_FILE 血缘。可选以兼容旧调用，缺省保持 UNRESOLVED，"
    "不能宣称已确定范围，后续依真实回执闭合；显式范围不得被 S0/S1 静默扩大。"
)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_design_workspace",
        "description": "读取共用设计索引与快照指纹；默认只返回索引，component_ids可选取最多30条完整内容。来源仍是正式阶段产物，不授予审批。",
        "inputSchema": _object_schema({"project_id": {"type": "string"},
            "component_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 30}}, ["project_id"]),
    },
    {
        "name": "get_revision_reuse_plan",
        "description": "按正式修订来源比较真实输入与制品，解释影响和可复用候选；不跳过质量验证、不继承批准、不改变旧发布版本。",
        "inputSchema": _object_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "preflight_design_patch",
        "description": "按稳定组件ID局部修改服务端共用设计并执行原S2/S3/S4预检。需当前revision、快照和条目指纹；通过后仍须原token提交和适用人工批准。CQ登记必须走amend_competency_questions。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"}, "stage": {"type": "string", "enum": ["S2", "S3", "S4"]},
            "expected_revision": {"type": "integer", "minimum": 0},
            "expected_snapshot_sha256": {"type": "string"},
            "operations": {"type": "array", "minItems": 1, "maxItems": 30,
                "items": _object_schema({"op": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "kind": {"type": "string"}, "id": {"type": "string"},
                    "expected_content_sha256": {"type": "string"}, "value": {"type": "object"}}, ["op", "kind", "id"])},
        }, ["project_id", "stage", "expected_revision", "expected_snapshot_sha256", "operations"]),
    },
    {
        "name": "amend_competency_questions",
        "description": "正式变更当前工程 CQ 需求基线。用户授权新增/修改需求后，提交包含原有与新增问题的完整列表，显式保留原 id。仅限非发布工程 S0/S1 已通过、S2 RUNNING 且尚未正式提交 S2；后续阶段必须先正式重开 S2，已发布工程先创建修订。不改来源、不跳门禁，保存旧登记及增删改审计并提升 revision；之后重新预检 S2。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string", "minLength": 1},
            "questions": {"type": "array", "minItems": 1, "items": {**INITIAL_COMPETENCY_QUESTION_SCHEMA, "required": ["id", "question", "expected"]}},
            "expected_revision": {"type": "integer", "minimum": 1},
            "actor": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        }, ["project_id", "questions", "expected_revision", "actor", "reason"]),
    },
    {
        "name": "create_ontology_project",
        "description": "创建一个可持久化、可断点续跑的 ORION 本体工程项目，并进入 S0 资料接入与证据整理。工程模式会话在用户明确要求构建且必要输入可推断时可幂等创建一次；已有 project_id 时必须继续原工程，禁止重复创建。",
        "inputSchema": _object_schema(
            {
                "project_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "面向用户展示的中文业务名称；允许附带英文缩写，但不能只使用英文技术短名。",
                },
                "domain": {"type": "string", "minLength": 1},
                "datasource_label": {"type": "string"},
                "table_scope": {"type": "array", "items": {"type": "string"}},
                "source_scope": SOURCE_SCOPE_SCHEMA,
                "source_snapshot_path": {"type": "string", "minLength": 1, "description": "DOCUMENT_ONLY 可直接引用受管完整批次 source_path，平台验证并生成全部 source_scope。与 source_scope 互斥，不接受单文件或任意目录。"},
                "template_id": {
                    "type": "string",
                    "minLength": 3,
                    "description": "可选的 ORION 内置模板编号。创建时会校验模板清单与源文件 SHA-256，并把不可变基线复制到工程。行业参考不能直接用于新建工程。",
                },
                "intake_mode": {
                    "type": "string",
                    "enum": ["DOCUMENT_ONLY", "DATABASE_ONLY", "HYBRID"],
                    "default": "HYBRID",
                    "description": "按真实来源选择：DOCUMENT_ONLY 仅资料，DATABASE_ONLY 一个或多个数据库，HYBRID 资料与结构化数据。v2 三种路线都完成 S0 目标/来源和 S1 理解，DOCUMENT_ONLY 仅数据库子任务不适用；历史 v1 才保留整阶段 S1 跳过，不自动迁移旧工程。",
                },
                "intake_rationale": {
                    "type": "string",
                    "minLength": 1,
                    "description": "选择当前接入模式的业务原因；进入项目审计记录，并用于不适用阶段的范围判定。",
                },
                "cq_mode": {
                    "type": "string",
                    "enum": ["USER_PROVIDED", "USER_PLUS_AI", "AI_GENERATED"],
                    "default": "USER_PLUS_AI",
                    "description": "USER_PROVIDED 只采用人工问题；USER_PLUS_AI 保留人工问题并由系统补充；AI_GENERATED 由系统根据证据生成。",
                },
                "initial_competency_questions": {
                    "type": "array",
                    "items": INITIAL_COMPETENCY_QUESTION_SCHEMA,
                    "description": "工程开始时提供的中文业务问题。每项填写 question、expected，可选 priority 和 example_entities；无需填写 SPARQL。",
                },
                "request_id": {
                    "type": "string",
                    "minLength": 8,
                    "maxLength": 128,
                    "description": "调用方生成的幂等请求编号；网络重试或重复提交必须复用同一编号。",
                },
            },
            ["project_name", "domain"],
        ),
    },
    {
        "name": "get_message_attachment_candidates",
        "description": "只读取得宿主绑定当前真实用户消息的附件候选回执；仅接受系统注入的 batch_id。候选不是来源授权，不扫描附件目录。",
        "inputSchema": _object_schema({"batch_id": {"type": "string", "pattern": "^MESSAGE-[A-F0-9]{32}$"}}, ["batch_id"]),
    },
    {
        "name": "snapshot_message_attachments",
        "description": "根据用户明确来源范围，将真实消息附件候选中的显式 attachment_ids 子集固化到 ORION 受管 S0 快照，返回真实 source_snapshot_path、hash 和摘要；创建 DOCUMENT_ONLY 工程时引用此路径，平台验证并生成完整 source_scope。不得默认全选；排除用户未授权文件。不会创建或推进工程。",
        "inputSchema": _object_schema({
            "batch_id": {"type": "string", "pattern": "^MESSAGE-[A-F0-9]{32}$"},
            "attachment_ids": {"type": "array", "minItems": 1, "maxItems": 5000, "uniqueItems": True, "items": {"type": "string", "pattern": "^ATT-[0-9]{4}$"}},
        }, ["batch_id", "attachment_ids"]),
    },
    {
        "name": "snapshot_workspace_sources",
        "description": "把用户在当前消息中通过 Harness 原生 @ 明确引用的工作区文件或文件夹固化到 ORION 受控资料区，供 S0 解析和审计。验证完整批次后返回可直接用于创建工程的 source_scope，文件来源按真实路径、名称、类型与哈希登记，不含自编 source_id。只接受允许工作区内的相对路径；不得用于扫描未被用户引用的目录。",
        "inputSchema": _object_schema(
            {
                "workspace_root": {
                    "type": "string",
                    "minLength": 1,
                    "description": "当前 Harness 工作区的绝对根路径。",
                },
                "references": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "string", "minLength": 1},
                    "description": "当前用户消息中明确 @ 的工作区相对文件或文件夹路径。",
                },
            },
            ["workspace_root", "references"],
        ),
    },
    {
        "name": "preflight_workspace_snapshot",
        "description": "只读验证工作区 REFERENCE 或浏览器 UPLOAD 完整批次的真实清单与内容回执，统计表格规模和资料类型，返回 source_snapshot 供平台绑定入队及 source_scope 直接用于创建工程。文件来源按真实路径、名称、类型与哈希登记，不补写自编 source_id。此回执不扩展已有工程授权。只能检查 ORION 受控上传区内的 source_path。",
        "inputSchema": _object_schema(
            {
                "source_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "snapshot_workspace_sources 或浏览器上传返回的完整批次 source_path。",
                },
            },
            ["source_path"],
        ),
    },
    {
        "name": "reconcile_document_source_identities",
        "description": "在已有 S0 工程中按受控 REFERENCE 或浏览器 UPLOAD 完整批次校验来源技术标识，只移除与平台真实 DOC-hash 标识不一致的自编 source_id，保留来源范围原件、前后差异和真实标识依据。保持路径、哈希、名称、类型与授权范围不变，不创建或删除工程，不直接改账本。完成后回读当前工程与修订，再继续原资料任务。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "source_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "snapshot_workspace_sources 或浏览器上传返回的受控 REFERENCE/UPLOAD 完整批次 source_path，不能传单文件、任意目录或扩大已有授权范围。",
                },
                "expected_revision": {"type": "integer", "minimum": 0},
                "actor": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            ["project_id", "source_path", "expected_revision", "actor", "reason"],
        ),
    },
    {
        "name": "start_document_ingestion_job",
        "description": "将已有 S0 工程的受控 REFERENCE 或浏览器 UPLOAD 批次提交到 3081 持久队列；先验证真实清单、内容回执和工程来源范围，执行前重验绑定清单。返回 job_id 后用 get_document_ingestion_job 查询。不创建工程，不绕过资料复核或 S0 门禁。相同工程修订和资料批次复用同一任务。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"}, "source_path": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0}, "actor": {"type": "string", "minLength": 1},
            "max_runtime_seconds": {"type": "integer", "minimum": 1, "maximum": 14400},
        }, ["project_id", "source_path", "expected_revision", "actor"]),
    },
    {
        "name": "get_document_ingestion_job",
        "description": "只读回读当前工程资料任务的文件、页数、失败项、复核状态与执行器心跳。review 提供真实队列复核产物路径、质量报告和有限文档摘录；摘录不替代全文复核。READY_FOR_REVIEW 产物仍在队列目录，未正式提交 S0，不要猜测工程目录下的 Markdown 或报告路径；按 review.artifacts 回读并复核后使用 commit_document_ingestion_job。QUEUED 不代表正在解析，READY_FOR_REVIEW 不代表 S0 已通过。",
        "inputSchema": _object_schema({"project_id": {"type": "string"}, "job_id": {"type": "string"}}, ["project_id", "job_id"]),
    },
    {
        "name": "commit_document_ingestion_job",
        "description": "复核 READY_FOR_REVIEW 资料任务后按绑定的工程修订正式提交 S0；HYBRID 表格可指定 IMPORT 执行全量数据库导入。旧修订任务不能覆盖当前修订。不得在未检查解析结果时接受 warnings。大批量 IMPORT 可能需要较长时间，应先查询任务状态，禁止因调用超时重复导入。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"}, "job_id": {"type": "string"},
            "reviewed_by": {"type": "string", "minLength": 1}, "rationale": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
            "accept_warnings": {"type": "boolean"}, "structured_data_action": {"type": "string", "enum": ["DOCUMENT_ONLY", "IMPORT"]},
        }, ["project_id", "job_id", "reviewed_by", "rationale", "expected_revision"]),
    },
    {
        "name": "record_document_evidence",
        "description": "记录 S0 的 PDF、Word、Excel 等资料、结构化 Markdown、质量报告、证据索引和真实 MCP 处理轨迹；资料建模项目随后留痕跳过 S1 并进入 S2，混合工程进入 S1。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "documents": {"type": "array", "items": {"type": "object"}},
                "quality_report": {"type": "object"},
                "evidence_index": {"type": "array", "items": {"type": "object"}},
                "processing_trace": {"type": "object"},
            },
            [
                "project_id",
                "documents",
                "quality_report",
                "evidence_index",
                "processing_trace",
            ],
        ),
    },
    {
        "name": "record_s0_scope_decision",
        "description": "纯数据库项目在 S0 正式记录 DATABASE_ONLY 范围判定，生成报告与审计后进入 S1；不是静默跳过。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "intake_mode": {"type": "string", "enum": ["DATABASE_ONLY"]},
                "rationale": {"type": "string", "minLength": 1},
                "decided_by": {"type": "string", "minLength": 1},
                "datasource_refs": {"type": "array", "items": {"type": "string"}},
            },
            ["project_id", "intake_mode", "rationale", "decided_by"],
        ),
    },
    {
        "name": "record_data_understanding",
        "description": "记录 S1 生产级只读摸排结果；必须提交 FULL_IMPORT_WITH_EXACT_COUNTS、范围/Schema/逐表行数/数据集闭合，以及每条 SQL 的真实执行与哈希回执。通过后自动进入 S2，禁止写 SQL、抽样画像或描述性代替回读。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "datasource_inventory": {"type": "object"},
                "schema_snapshot": {"type": "object"},
                "data_profile": {"type": "object"},
                "relation_candidates": {"type": "array", "items": {"type": "object"}},
                "evidence_sql": {"type": "array", "items": {"type": "object"}},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            [
                "project_id",
                "datasource_inventory",
                "schema_snapshot",
                "data_profile",
                "relation_candidates",
                "evidence_sql",
            ],
        ),
    },
    {
        "name": "record_document_understanding",
        "description": "仅供新版 DOCUMENT_ONLY 工程在 S1 重验资料理解：平台回读 S0 已通过的资料与来源范围，结构化数据库子任务不适用；不接收 dataset_ids，也不伪造数据库画像。正常 S0 完成后由平台自动执行，只有平台推荐重验时才调用。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "expected_revision"],
        ),
    },
    {
        "name": "list_source_connections",
        "description": "列出平台只读连接引用与 Chat2DB 接入就绪状态，不返回凭据。用户已在3081选择Chat2DB数据源时，优先复用 datasource_id 调用 capture_database_snapshot；不要求重复配置数据库账号。",
        "inputSchema": _object_schema({}, []),
    },
    {
        "name": "capture_database_snapshot",
        "description": "S1 数据库来源接入：复用用户在3081选择的Chat2DB PostgreSQL数据源ID（chat2db_datasource_id），或已登记connection_env，两者恰选其一。凭据始终留在连接管理端。按授权库/表登记正式来源、校验只读与全量分页、原子提升版本快照，返回dataset_ids后调用record_data_understanding_from_datasets。表必须逐个列出；PRODUCTION须有真实授权依据，不能以TEST_ONLY冒充。Chat2DB服务未就绪、非只读连接或其他未支持引擎明确拒绝，不回退采样。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$", "maxLength": 48},
                "connection_env": {"type": "string", "pattern": "^ORION_[A-Z0-9_]+_SOURCE_URL$"},
                "chat2db_datasource_id": {"type": "integer", "minimum": 1, "description": "3081选择数据库返回的Chat2DB数据源ID；与connection_env互斥。"},
                "database": {"type": "string", "minLength": 1},
                "schema": {"type": "string", "default": "public"},
                "tables": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 60, "uniqueItems": True},
                "exclude_columns": {"type": "array", "items": {"type": "string", "pattern": "^[^:]+:[^:]+$"}, "description": "table:column 形式的排除字段。"},
                "pii_scope": {"type": "string", "minLength": 1},
                "owner": {"type": "string", "minLength": 1},
                "dataset_type": {"type": "string", "enum": ["PRODUCTION", "TEST_ONLY"], "description": "进入 S1 生产门禁的工程必须用 PRODUCTION；TEST_ONLY 仅用于试采，record_data_understanding_from_datasets 会拒绝。"},
                "production_evidence_basis": {"type": "string", "minLength": 8},
                "snapshot_version": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
            },
            ["project_id", "source_id", "database", "tables", "pii_scope", "owner"],
        ),
    },
    {
        "name": "record_data_understanding_from_datasets",
        "description": "按已登记且属于当前工程的 READY dataset_id，从只读 PostgreSQL 目录服务端重建完整 S1 handoff 并执行生产门禁；避免模型转抄大体积 Schema/画像/SQL 回执。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "dataset_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "dataset_ids"],
        ),
    },
    {
        "name": "query_source_evidence",
        "description": "S2–S7 对当前工程 S1 已登记的文件导入数据版本或快照中心数据库快照表做只读证据核验，无须重开 S1。支持三种互斥输入：⓪ describe_table 只读回读一个受控快照的真实业务列、快照类型与版本身份，先读结构再查询，不猜列名；① query_plan 结构化跨表计划（受控数据库快照，最多4表、100行，支持等值JOIN、比较/空值过滤、大小写归一、数值/日期转换、分组和count/count_distinct/sum/avg/min/max聚合）；②传统计数，用 Schema 中表名（current 视图、physical_version_table，或快照表的 ms_ 物理名/来源表名）、group_by 多列分组和 equals 等值过滤；两种方式均不接受 SQL、不连接额外来源。query_plan 可取得 CQ 的真实行基线，但不代表 Ontop/规则验收；结果含 total_row_count、truncated、来源与结果哈希。返回完整匹配行数/分组数、每组真实计数和 truncated；截断仅影响返回组数，不能把返回组当全部分布。只用专用只读连接，核对工程/revision/S1指纹/READY目录，不写工程状态，也不代替业务决定或 S6 运行验收。未登记外部库或无版本绑定表不在此入口范围。",
        "inputSchema": {**_object_schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "query_plan": SOURCE_QUERY_PLAN_SCHEMA,
                "describe_table": {"type": "string", "minLength": 1, "description": "受控数据库快照的 S1 表名/source_id.table/物理名；仅回读结构，不查数据，不与其他查询参数混用"},
                "table": {"type": "string", "minLength": 1},
                "group_by": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 6, "uniqueItems": True},
                "equals": {"type": "object", "maxProperties": 12, "additionalProperties": {"type": ["string", "number", "boolean", "null"]}},
                "max_groups": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            ["project_id", "expected_revision"],
        ), "oneOf": [
            {"required": ["table", "group_by"], "not": {"anyOf": [{"required": ["query_plan"]}, {"required": ["describe_table"]}]}},
            {"required": ["query_plan"], "not": {"anyOf": [{"required": [key]} for key in ("table", "group_by", "equals", "max_groups", "describe_table")]}},
            {"required": ["describe_table"], "not": {"anyOf": [{"required": [key]} for key in ("table", "group_by", "equals", "max_groups", "query_plan")]}},
        ]},
    },
    {
        "name": "record_semantic_candidates",
        "description": "记录 S2 业务语义候选，并用 cq_semantic_assessments 逐条检查全部原始 CQ 的业务定义、模型覆盖和数据缺口。候选区分 DATABASE_FACT、DOCUMENT_EVIDENCE、AI_INFERENCE、NEEDS_HUMAN_CONFIRMATION；HYBRID 覆盖两路证据。派生判断规则用 business_question_ids 绑定 CQ，含 IF/THEN、前提/结论谓词和正反边界测试。自动比较规则在 condition_contract 唯一声明前提属性、算子和固定常量/业务允许的参数；S3只绑定字段，不能改变阈值或把固定规则改成任意参数。缺定义不发明政策，缺实例不伪造事实，不用较弱问题替代；平台计算就绪状态，语义评估不代替现有生产门禁。每个数据库来源的候选都应填 source_binding（类填 table，数据属性填 table+column+declared_sql_type，对象属性填 join 的四个端点），S3 才能由平台直接生成映射骨架；缺 source_binding 会退回散文推断并逐条要求人工核对。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "ontology_candidates": {
                    "type": "array",
                    "items": ONTOLOGY_CANDIDATE_SCHEMA,
                    "minItems": 1,
                },
                "business_rule_candidates": {
                    "type": "array",
                    "items": BUSINESS_RULE_CANDIDATE_SCHEMA,
                },
                "cq_semantic_assessments": {
                    "type": "array",
                    "items": CQ_SEMANTIC_ASSESSMENT_SCHEMA,
                    "description": "新增提交应逐 CQ 填写；提供时须覆盖全部原始 CQ。省略仅为历史兼容，回读显示 UNASSESSED，不默认就绪。不得提交自报 READY/VALIDATED 状态。",
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "ontology_candidates"],
        ),
    },
    {
        "name": "get_cq_semantic_review",
        "description": "只读检查当前工程全部 CQ 的业务定义、模型覆盖、数据依据和验证状态，返回缺失规则/前提及正式恢复建议。S3 业务确认建议仍须通过现有决策卡留痕；工具不批准设计、不更新阶段、不将模型声明视为真实验证。旧工程未提交语义评估时显示 UNASSESSED。",
        "inputSchema": _object_schema(
            {"project_id": {"type": "string", "minLength": 1}},
            ["project_id"],
        ),
    },
    {
        "name": "prepare_mapping_review",
        "description": "payload_file 与内联 mapping_draft/confirmations/automatic_decisions/realtime_runtime 二选一；文件模式必须是当前修订的最新 S3 检查点并提供 expected_revision。文件模式可选 review_scope=BUSINESS_ONLY 先登记业务卡，保留未完成运行草稿并接续新修订；默认 FULL 检查全部运行设计。写入 Mapping 草案和高影响业务卡；v2 最多 3 张、v1 最多 2 张并追加总体确认。v2 有未决业务卡时可先省略 realtime_runtime，严格预检并保存待确认草案；决定后须编译完整运行设计并再次预检，不能据此通过 S3。",
        "inputSchema": {**_object_schema(
            {
                "project_id": {"type": "string"},
                "payload_file": _object_schema({"file_name": {"type": "string"}, "sha256": {"type": "string"}}, ["file_name", "sha256"]),
                "review_scope": {"type": "string", "enum": ["FULL", "BUSINESS_ONLY"], "default": "FULL",
                    "description": "仅传输层选项，不写入 S3 内容。BUSINESS_ONLY 仅文件模式、v2、非空业务卡可用；不清空草稿运行设计、不批准 S3。确认工具接续同阶段检查点，退回 S2 不接续。"},
                "mapping_draft": {"type": "object"},
                "confirmations": {
                    "type": "array",
                    "items": S3_CARD_SCHEMA,
                    "description": "真实未决高影响业务卡；v2 最多 3 项、v1 最多 2 项，无歧义传 []。字段与 S3 预检 payload 相同，按 schema 一次填齐；复验时保留原卡与平台回读元数据。",
                },
                "automatic_decisions": {
                    "type": "array",
                    "items": S3_AUTOMATIC_DECISION_SCHEMA,
                    "description": "有依据的自动决定；无自动决定也必须传 []。每项必填 topic/decision/reason/source_refs；建议填 id，省略由平台生成。affected_mapping_ids 可省略或为空。",
                },
                "realtime_runtime": {
                    **S3_RUNTIME_SUBMISSION_SCHEMA,
                    "description": (
                        "S3 最终通过必需的运行设计；v2 仅在有合格未决业务卡时允许暂缺，业务决定后必须补齐并完整预检。结构化项目的 Ontop 只读运行时：ontop_deployment_id、"
                        "database_access_mode=READ_ONLY、prepared_by、mapping_obda "
                        "、ontop_queries、逐查询 query_capabilities，以及"
                        "document_query_capabilities。每个项目必须声明"
                        "reasoning_requirement=REQUIRED 或 NOT_APPLICABLE；"
                        "REQUIRED 时必须提交 reasoning_capabilities，NOT_APPLICABLE "
                        "时必须提交具体 reasoning_not_applicable_reason。"
                        "reasoning_capabilities 需逐能力声明 evidence_query、"
                        "fact_bindings、正式 rules、result_predicates、source_rule_ids、"
                        "execution_scope=FULL_QUERY_RESULT 和示例问法。参数必须声明类型、范围、"
                        "中文含义和示例问法；每个新查询必须声明至少一个"
                        "validation_cases 真实结果验收样例。结构化 CQ 使用 query_capabilities.<query_name>.cq_bindings；"
                        "文档事实与聚合 CQ 使用 document_fact_queries.<query_name>.cq_bindings，声明 sparql、ontology_terms 与真实用例；"
                        "规则 CQ（含纯资料）可使用 reasoning_capabilities.<capability_name>.cq_bindings，"
                        "同能力补齐 business_question_ids、parameters、result_fields、validation_cases。"
                        "按原 S0 业务问题 id 登记所选真实行结果用例、回答范围、来源、边界及维度；"
                        "平台据此渲染参数并编译 S4，不要求模型手写 answer_contract。"
                        "有来源的条件空值可用 nullable_bindings:{字段:{when:{同一行其他必填字段:具体值},reason_zh,source_refs}}。"
                        "规则能力级 CQ 必须 RULE_INFERENCE，绑定自身能力、真实派生谓词和 cq_sparql，"
                        "不能将 runtime_validation 的事实计数冒充行结果用例。与 S3 Mapping 一并评审，"
                        "S6 在完整实例和派生图执行；S7 对精确发布运行时回读。"
                    ),
                },
                "review_policy": {
                    "type": "string",
                    "enum": ["HUMAN_REQUIRED", "AUTO_APPROVE_EVIDENCE_BACKED"],
                    "default": "HUMAN_REQUIRED",
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id"],
        ), "oneOf": [
            {"required": ["mapping_draft", "confirmations", "automatic_decisions"],
             "not": {"required": ["payload_file"]}},
            {"required": ["payload_file", "expected_revision"],
             "not": {"anyOf": [{"required": [key]} for key in (
                 "mapping_draft", "confirmations", "automatic_decisions", "realtime_runtime"
             )]}},
        ]},
    },
    {
        "name": "resolve_mapping_confirmation",
        "description": "记录用户对 S3 单个语义问题的真实决定；v2 全部业务决定后仍须补齐并完整预检运行设计，不能仅凭决定生成 REVIEWED mapping.yaml 或通过 S3。v1 保留原流程。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "confirmation_id": {"type": "string"},
                "decision": {"type": "string", "minLength": 1},
                "selected_option_id": {"type": "string", "minLength": 1},
                "decided_by": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "mapping_updates": {"type": "array", "items": {"type": "object"}},
            },
            [
                "project_id",
                "confirmation_id",
                "decision",
                "selected_option_id",
                "decided_by",
                "rationale",
            ],
        ),
    },
    {
        "name": "resolve_mapping_option",
        "description": "按 S3 已展示的双选项记录决定；工程页和对话页共用同一状态与审计链。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "confirmation_id": {"type": "string"},
                "selected_option_id": {"type": "string", "minLength": 1},
                "decided_by": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            [
                "project_id",
                "confirmation_id",
                "selected_option_id",
                "decided_by",
                "rationale",
            ],
        ),
    },
    {
        "name": "preview_stage_rollback",
        "description": "只读预览回退 S0-S6 对下游阶段、当前产物、历史版本和发布状态的影响，并签发十分钟内有效的一次性确认令牌。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "target_stage": {
                    "type": "string",
                    "enum": ["S0", "S1", "S2", "S3", "S4", "S5", "S6"],
                },
                "changed_components": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": [
                            "SOURCE_EVIDENCE",
                            "DATA_PROFILE",
                            "SEMANTIC_MODEL",
                            "MAPPING",
                            "RUNTIME_MAPPING",
                            "RUNTIME_RULES",
                            "COMPETENCY_QUESTIONS",
                            "ONTOLOGY_SCHEMA",
                            "ONTOLOGY_BINARY",
                            "VALIDATION_EVIDENCE",
                            "DEPLOYMENT_CONFIG",
                        ],
                    },
                    "description": "可选。声明实际变化的组件后，平台只使依赖它的阶段失效。",
                },
            },
            ["project_id", "target_stage"],
        ),
    },
    {
        "name": "reopen_stage_for_correction",
        "description": "携带一次性预览令牌和原工程 revision，二次确认后留痕回退 S0-S6；旧产物转为历史，不再作为当前结果。默认 response_mode=SUMMARY 返回紧凑正式状态（制品生命周期只给计数，明细用 get_project_artifact）；FULL 返回完整状态。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "stage": {"type": "string", "enum": ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]},
                "reason": {"type": "string", "minLength": 1},
                "requested_by": {"type": "string", "minLength": 1},
                "preview_token": {"type": "string", "minLength": 1},
                "project_revision": {"type": "integer", "minimum": 0},
                "changed_components": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string"},
                    "description": "必须与 preview_stage_rollback 返回的组件范围完全一致。",
                },
                "response_mode": {"type": "string", "enum": ["FULL", "SUMMARY"], "default": "SUMMARY"},
            },
            ["project_id", "stage", "reason", "requested_by", "preview_token", "project_revision"],
        ),
    },
    {
        "name": "generate_ontology_design",
        "description": "根据正式 mapping.yaml 编译 S4 施工图及完整 S0 业务问题；S3 query_capabilities.<query_name>.cq_bindings 提供结构化绑定，reasoning_capabilities.<capability_name>.cq_bindings 提供规则绑定（含纯资料）。先用 preflight_stage_submission(stage=S4,payload={generation_request:{logical_axioms:[有来源公理],review_policy:HUMAN_REQUIRED}}) 预检，再提交 token。生产公理不自动虚构；不要空载荷反复生成。平台从已审用例渲染参数和结果/边界/推理契约。v2 必须整体确认后才能冻结。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "ontology_iri": {"type": "string"},
                "version": {"type": "string", "default": "0.1.0"},
                "competency_questions": {
                    "type": "array",
                    "items": COMPETENCY_QUESTION_OVERRIDE_SCHEMA,
                    "description": (
                        "专家兼容入口；CQ 优先从 S3 query_capabilities、document_fact_queries 或 reasoning_capabilities 的 cq_bindings 编译。任何覆盖仍须通过来源、语义维度、推理及已审绑定一致性预检。"
                        "已有已审绑定的 CQ 不允许通过此字段改写查询或答案契约；应回到 S3 修正。"
                        "不得新增无 S0 来源的问题。"
                    ),
                },
                "logical_axioms": {
                    "type": "array",
                    "items": LOGICAL_AXIOM_SCHEMA,
                },
                "review_policy": {
                    "type": "string",
                    "enum": ["HUMAN_REQUIRED", "AUTO_APPROVE_EVIDENCE_BACKED"],
                    "default": "HUMAN_REQUIRED",
                },
            },
            ["project_id"],
        ),
    },
    {
        "name": "resolve_competency_question_review",
        "description": "v2 确认或退回 S4 整套联合设计（本体、公理、映射、规则、CQ）。questions 可只回传原序原文的 id/question/expected（SUMMARY 的 approval_questions）；明确批准后冻结并进入 S5。若批准已保存但 S4 仍 RUNNING，可用当前 revision 再调用 APPROVED 恢复冻结，不会新增决定；questions 需与已批准原文一致。v1 保留原业务问题评审。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "questions": {"type": "array", "items": {"type": "object"}},
                "decision": {
                    "type": "string",
                    "enum": ["APPROVED", "RETURN_TO_S3"],
                },
                "decided_by": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "questions", "decision", "decided_by", "rationale"],
        ),
    },
    {
        "name": "prepare_ontology_design_review",
        "description": "专家入口：保存外部形成的 S4 设计草案；必须包含中文 title_zh/comment_zh 以及实体 label_zh/comment_zh。正常流程优先平台编译。v2 必须整体明确批准，AUTO_APPROVE 不能跳过联合确认；v1 保留原策略。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "ontology_design": {"type": "object"},
                "expected_revision": {"type": "integer", "minimum": 0},
                "review_policy": {
                    "type": "string",
                    "enum": ["HUMAN_REQUIRED", "AUTO_APPROVE_EVIDENCE_BACKED"],
                    "default": "HUMAN_REQUIRED",
                },
            },
            ["project_id", "ontology_design"],
        ),
    },
    {
        "name": "start_managed_stage_execution",
        "description": "从当前已通过的阶段基线启动 S5 Protégé 构建或 S6 全量质量验收的受管后台任务。仅接受当前工程/阶段/revision；复用平台固定 runner、执行租约和正式预检门禁，不需模型传入 OWL、TTL、SHACL 或私密配置。返回 job_id 后用 get_managed_stage_execution 观察终态，不重复启动。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["S5", "S6"]},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, ["project_id", "stage", "expected_revision"]),
    },
    {
        "name": "get_managed_stage_execution",
        "description": "只读查询 S5/S6 受管后台任务的 job_id、状态、完成时间、退出码与正式工作流 revision。RUNNING 时用 wait_seconds（最多 60）让平台在服务端等待到终态或超时再返回，重复调用直到终态；不要用 bash sleep 等待受管任务。FAILED 或 NEEDS_RECONCILIATION 时先定位原因，不重复施工。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["S5", "S6"]},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60},
        }, ["project_id", "stage"]),
    },
    {
        "name": "get_runtime_capability",
        "description": "只读回看当前工程已提交的 S3 运行能力索引；不传 capability_name 返回能力目录，传名称返回该能力的正式 fact_bindings、ontology_terms、CQ 绑定和经哈希校验的外置规则包。回退修订后标注制品生命周期，历史能力不可当成当前已批准结果。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "capability_name": {"type": "string", "minLength": 1},
        }, ["project_id"]),
    },
    {
        "name": "get_project_artifact",
        "description": "只读分页读取当前工程的正式阶段制品与诊断：不传 path 列出 00-07 阶段目录、stage-jobs 与 S5/S6 诊断；传 path 读取文本（JSON 可用 pointer 取子树），长文本用 offset/limit 翻页，凭据已脱敏。用于核对 S3 运行期查询/映射、S4 生成设计中的 CQ SPARQL、S6 失败诊断等；草稿用 get_stage_draft，修订快照不可读。读取结果不是批准或验收。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "path": {"type": "string", "minLength": 1},
            "pointer": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
            "prefix": {"type": "string"},
        }, ["project_id"]),
    },
    {
        "name": "record_ontology_build",
        "description": "记录 S5 Protégé MCP 构建产物；服务端重新解析 OWL/TTL/SHACL，核对设计与逻辑公理覆盖、中文语义，并强制校验真实 HermiT 一致性和 SHACL 回执。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "ontology_owl": {"type": "string", "minLength": 1},
                "ontology_ttl": {"type": "string", "minLength": 1},
                "shapes_ttl": {"type": "string", "minLength": 1},
                "protege_build_report": {"type": "object"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            [
                "project_id",
                "ontology_owl",
                "ontology_ttl",
                "shapes_ttl",
                "protege_build_report",
            ],
        ),
    },
    {
        "name": "get_stage_input_contract",
        "description": "只读正式阶段合同。先 section=overview 获取当前草稿和可用分段，再按返回的 section 读取所需字段合同；省略 section 保留完整合同。骨架不是已验证事实，不含虚构规则或业务决定，无需翻阅源码猜字段。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["S2", "S3", "S4"]},
            "section": {"type": "string", "description": "overview 或 overview 返回的 section 名；按需读取。"},
        }, ["project_id", "stage"]),
    },
    {
        "name": "replace_document_sources",
        "description": "更正 S0 未验收资料：以已验证的完整快照批次替换全部文档选择，可表达增删或替换。保留目标、CQ 和数据库授权，记录修订及原因。活动解析任务不能替换；后续阶段先预览并回退 S0；不修改数据库来源范围。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "source_path": {"type": "string"},
            "expected_revision": {"type": "integer"},
            "actor": {"type": "string"},
            "reason": {"type": "string"},
        }, ["project_id", "source_path", "expected_revision", "actor", "reason"]),
    },
    {
        "name": "get_business_quality",
        "description": "只读检查当前工程已记录的业务对象、关系、映射与 CQ 质量缺口。返回有限摘要和产物引用，不运行全图或修改正式状态；未核验不等于通过。",
        "inputSchema": _object_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "get_stage_draft",
        "description": "只读恢复当前 revision 的最新阶段草稿；默认仅返回字段摘要和引用。pointer 指定 JSON Pointer 后分页读取该部分，不重传全文。CHECKPOINT 不代表预检通过或业务批准。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["S1", "S2", "S3", "S4", "S5", "S6"]},
            "pointer": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
        }, ["project_id", "stage"]),
    },
    {
        "name": "get_business_preview",
        "description": "只读查看当前 S3 草稿的单项业务能力试运行与可恢复进度。结果绑定 revision、草稿和来源指纹；旧输入结果标为 STALE。仅当前数据库/混合快照能力可试运行，不能代替正式阶段或实时问答验收。",
        "inputSchema": _object_schema({"project_id": {"type": "string", "minLength": 1}}, ["project_id"]),
    },
    {
        "name": "start_business_preview",
        "description": "从最新 S3 草稿选一项 business_query_plan 和独立验收用例，启动受限真实快照映射查询/规则/CQ试运行。只写隔离试运行回执，不提交阶段、不批准或发布。相同输入复用任务与终态；失败显式 retry=true 最多再试一次，禁止循环继续。平台最多两项、同工程一项并发、180秒；结果超出200行不进行全量结论。诊断后用 patch_stage_submission 局部修订，改变输入再验证。get_business_preview 恢复状态，勿反复 start。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
            "payload_file": _object_schema({"file_name": {"type": "string"}, "sha256": {"type": "string"}}, ["file_name", "sha256"]),
            "plan_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,40}$"},
            "case_id": {"type": "string", "minLength": 1},
            "retry": {"type": "boolean", "default": False},
        }, ["project_id", "expected_revision", "payload_file", "plan_id"]),
    },
    {
        "name": "fork_s2_draft_from_history",
        "description": "S2 正式回退后，从本次回退前已预检通过且与不可变候选/规则快照一致的历史草稿创建当前 revision 检查点。只继承编辑起点，不继承原审批或预检结论；仅在当前 S2 无检查点时可用。随后必须按来源增量修订并重新完整预检。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "from_revision_id": {"type": "string", "pattern": r"^REV-\d{8}T\d{6}-[A-F0-9]{8}$"},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, ["project_id", "from_revision_id", "expected_revision"]),
    },
    {
        "name": "fork_s3_draft_from_history",
        "description": "S3 正式回退后，或 S2 正式回退且重审通过进入 S3 后，从本次回退的验签映射快照及匹配的历史草稿创建当前 revision 检查点。S3 回退要求旧草稿曾预检通过；S2 回退仅接受紧邻上一 revision 的 S3 检查点。只继承编辑起点，不继承原审批或预检结论；仅在当前 S3 无检查点时可用，须增量修订并重新完整预检。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "from_revision_id": {"type": "string", "pattern": r"^REV-\d{8}T\d{6}-[A-F0-9]{8}$"},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, ["project_id", "from_revision_id", "expected_revision"]),
    },
    {
        "name": "compile_mapping_runtime",
        "description": "从当前修订最新 S3 检查点和 S1 快照确定性编译 OBDA、实例查询及运行元数据，保存 CHECKPOINT，只返回摘要与 open_items。复核映射后，可在 mapping_draft.business_query_plans 以映射ID声明对象关系、属性、类型条件、S2规则引用及独立验收期望；平台生成业务SPARQL、事实绑定和规则CQ，不手工同步重复执行字段。首版支持快照、显式关系遍历和同一对象正向一元规则，不支持的计划返回待办。业务计划有错时返回 BUSINESS_PLAN_REQUIRES_REPAIR 与字段诊断，不合并或保存不完整运行内容；先局部修正计划再编译。已有编译清单时只更新未被手改的受管产物，手工冲突明确阻断；历史无清单运行设计沿用保守增量，不删除后重建。未改输入重复编译不写新草稿。静态编译不代表业务批准或运行验收。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, ["project_id", "expected_revision"]),
    },
    {
        "name": "generate_mapping_skeleton",
        "description": "由平台从本工程已落盘的 S1 结构快照与 S2 业务语义候选，机械生成 S3 映射骨架并存为 CHECKPOINT 草稿，只返回覆盖率与待人工确认项，不回传整份草稿。适用于表较多、手写整份 mapping_draft 容易在模型侧被截断的工程。骨架优先读候选的 source_binding（table/column/join/declared_sql_type）；没有结构化绑定时按描述推断并逐条列为待核对项，绝不编造列名或关联方向。多对多关系、技术列、快照类型被放宽为 text 的数值/时间列都会列入 open_items 而不是猜值。骨架是待评审起点，不是已批准映射：仍须逐条复核中文业务命名与定义、处理 open_items、补齐 confirmations/automatic_decisions/realtime_runtime，再走 preflight_stage_submission，不推进阶段也不发 token。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 0},
            "namespace": {"type": "string", "minLength": 1, "description": "工程本体命名空间；省略时沿用当前 S3 草稿中的 namespace。"},
        }, ["project_id", "expected_revision"]),
    },
    {
        "name": "save_stage_submission",
        "description": "分步保存阶段草稿。首次尽早使用 validation_mode=CHECKPOINT 保存已有业务内容（可不完整）；后续 patch_stage_submission 分批补充。CHECKPOINT 不预检、不发 token、不推进阶段；内容齐备后 PREFLIGHT 完整预检。中断后 get_stage_draft 恢复当前 revision 草稿。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["S1", "S2", "S3", "S4", "S5", "S6"]},
            "payload": {"type": "object"},
            "validation_mode": {"type": "string", "enum": ["CHECKPOINT", "PREFLIGHT"], "default": "PREFLIGHT"},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, ["project_id", "stage", "payload", "expected_revision"]),
    },
    {
        "name": "patch_stage_submission",
        "description": "按已保存草稿的file_name和sha256局部修正；CHECKPOINT 仅保存增量，PREFLIGHT 完整预检，返回新payload_file及通过时的一次性token。避免重传整份语义/映射草稿。operations支持JSON Pointer路径的add/replace/remove/test/replace_text；数组add可用/-追加。长文本局部编辑用replace_text，value={old:唯一匹配的非空原文,new:替换文本}，不接受正则；缺失或多处匹配原子拒绝。OBDA追加可唯一匹配末尾]]并替换为新映射块加]]，无需重传已有大文本。单次调用最多 12 个 operations（超过会被拒绝并给出拆批方式），建议不超过 32768 字节：参数过大会在模型侧被截断成非法 JSON 而整轮失败，平台无法拦截，只能分多轮补齐。返回 draft_batch_usage 回执本次用量，便于自校准下一批。confirmations 和 automatic_decisions 每批最多 3 条；推理/查询能力逐项保存。基线不可变，失败不改原草稿或正式阶段；业务批准仍单独执行。",
        "inputSchema": _object_schema({
            "project_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["S1", "S2", "S3", "S4", "S5", "S6"]},
            "payload_file": _object_schema({"file_name": {"type": "string"}, "sha256": {"type": "string"}}, ["file_name", "sha256"]),
            "operations": {"type": "array", "minItems": 1, "maxItems": 200, "items": _object_schema({
                "op": {"type": "string", "enum": ["add", "replace", "remove", "test", "replace_text"]},
                "path": {"type": "string", "description": "JSON Pointer，例如 /ontology_candidates/2/name；~1表示/，~0表示~"},
                "value": {},
            }, ["op", "path"])},
            "validation_mode": {"type": "string", "enum": ["CHECKPOINT", "PREFLIGHT"], "default": "PREFLIGHT"},
            "expected_revision": {"type": "integer", "minimum": 0},
        }, ["project_id", "stage", "payload_file", "operations", "expected_revision"]),
    },
    {
        "name": "preflight_stage_submission",
        "description": "预检 S1-S6 提交载荷。payload 与 payload_file 二选一：大载荷请完整保存到当前工程 .submission-drafts/<file_name>.json，只传 payload_file={file_name,sha256}，禁止为缩短消息删减业务内容。S1 可只传 payload.dataset_ids，由平台编译真实回执。通过后返回 preflight_token；文件预检成功时用 normalized_payload_ref 提供已核验的完整固化载荷审阅路径及 /payload，不重复回传全文，不要猜审阅文件名。不改变工程阶段，正式提交只传 token。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "stage": {"type": "string", "enum": ["S1", "S2", "S3", "S4", "S5", "S6"]},
                "payload": {"type": "object"},
                "payload_file": _object_schema(
                    {
                        "file_name": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$"},
                        "sha256": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
                    },
                    ["file_name", "sha256"],
                ),
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "stage"],
        ),
    },
    {
        "name": "commit_preflight_stage_submission",
        "description": "使用一次性 preflight_token 提交平台已验证并固化的 S1-S6 载荷。Agent 优先 response_mode=SUMMARY：返回真实状态、提交回执及校验后的 S4 review_ref/轻量审批 questions；完整正式资产不变。默认 FULL 保留旧回执。token 与工程、阶段、revision、载荷哈希和有效期绑定。调用返回丢失时用原令牌和原 expected_revision 重试，回读同一持久提交结果，不重复执行；返回需对账时停止重试。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "stage": {"type": "string", "enum": ["S1", "S2", "S3", "S4", "S5", "S6"]},
                "preflight_token": {"type": "string", "minLength": 20},
                "response_mode": {"type": "string", "enum": ["FULL", "SUMMARY"], "default": "FULL"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "stage", "preflight_token"],
        ),
    },
    {
        "name": "record_quality_validation",
        "description": "执行 S6 生产质量门禁：服务端重跑 SHACL，核验 CQ 语义、正式规则哈希与轨迹，并要求 production_coverage 对账 S0 文档证据、S1 全部数据行和当前 S0-S5 指纹；代表性探针不能通过。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "materialized_ttl": {"type": "string", "minLength": 1},
                "hermit_report": {"type": "object"},
                "mapping_report": {"type": "object"},
                "semantic_quality_report": {"type": "object"},
                "competency_question_report": {"type": "object"},
                "semantica_report": {"type": "object"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            [
                "project_id",
                "materialized_ttl",
                "hermit_report",
                "mapping_report",
                "semantic_quality_report",
                "competency_question_report",
                "semantica_report",
            ],
        ),
    },
    {
        "name": "publish_ontology_package",
        "description": "在 S7 明确人工批准后发布；只接受 S6 已签发 production-gates-v1 全量生产资格回执，S7 不补写或降级生产证据。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "release_version": {"type": "string"},
                "approval_decision": {"type": "string", "enum": ["APPROVED"]},
                "approved_by": {"type": "string", "minLength": 1},
                "release_notes": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "operation_id": {"type": "string", "minLength": 8, "maxLength": 128},
            },
            [
                "project_id",
                "release_version",
                "approval_decision",
                "approved_by",
                "release_notes",
            ],
        ),
    },
    {
        "name": "sync_published_ontology_to_semantica",
        "description": "把已发布本体通过受锁工作流补同步到 Semantica，并统一记录回执、审计事件和产物清单。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "synced_by": {"type": "string", "minLength": 1},
                "semantica_url": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 0},
                "operation_id": {"type": "string", "minLength": 8, "maxLength": 128},
            },
            ["project_id", "synced_by"],
        ),
    },
    {
        "name": "defer_ontology_publication",
        "description": "在 S7 选择暂不发布；不生成发布包，并保留负责人、原因和审计事件。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "decided_by": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            ["project_id", "decided_by", "reason"],
        ),
    },
    {
        "name": "resume_ontology_publication",
        "description": "恢复被暂缓的 S7 审批；恢复后仍需再次明确批准才会生成发布包。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "resumed_by": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            ["project_id", "resumed_by", "reason"],
        ),
    },
    {
        "name": "revoke_ontology_release",
        "description": "撤回已发布版本的使用资格；原包和原发布记录保持不变，供审计回读。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "release_version": {"type": "string"},
                "revoked_by": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "release_version", "revoked_by", "reason"],
        ),
    },
    {
        "name": "create_revision_from_release",
        "description": "以不可变发布版本为基线创建独立修订项目，并从指定 S0-S6 阶段继续。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "release_version": {"type": "string"},
                "target_stage": {
                    "type": "string",
                    "enum": ["S0", "S1", "S2", "S3", "S4", "S5", "S6"],
                },
                "reason": {"type": "string", "minLength": 1},
                "requested_by": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "request_id": {"type": "string", "minLength": 8, "maxLength": 128},
            },
            ["project_id", "release_version", "target_stage", "reason", "requested_by"],
        ),
    },
    {
        "name": "archive_ontology_project",
        "description": "归档本体工程但不删除原文件、报告、发布包和审计记录；归档后仍可回读和恢复。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "archived_by": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "archived_by", "reason"],
        ),
    },
    {
        "name": "restore_ontology_project",
        "description": "恢复已归档工程，并从归档前保存的阶段和项目状态继续。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "restored_by": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ["project_id", "restored_by", "reason"],
        ),
    },
    {
        "name": "get_ontology_workflow_status",
        "description": "读取指定项目或最近项目的阶段状态、阻塞原因和下一个人工确认问题。只读。Agent 可用 response_mode=SUMMARY 一次回读紧凑状态与同 revision 的 next_action，保留经核验的完整审批文件引用；核验失败明确退回完整状态。默认 FULL 兼容旧调用。",
        "inputSchema": _object_schema(
            {"project_id": {"type": "string"}, "response_mode": {"type": "string", "enum": ["FULL", "SUMMARY"], "default": "FULL"}},
            [],
        ),
    },
    {
        "name": "get_next_workflow_action",
        "description": (
            "由工作流状态机计算当前唯一推荐动作、允许工具、输入责任方和正式来源；"
            "只读。模型在继续工程前应先调用，禁止自行猜测下一阶段或技术契约。"
        ),
        "inputSchema": _object_schema(
            {"project_id": {"type": "string"}},
            [],
        ),
    },
    {
        "name": "list_ontology_projects",
        "description": "列出本机 ORION 本体工程项目及当前阶段。只读。",
        "inputSchema": _object_schema({}, []),
    },
    {
        "name": "get_workflow_storage_status",
        "description": "回读 PostgreSQL 工程账本、审计事件、产物索引和大文件存储连接状态。只读。",
        "inputSchema": _object_schema(
            {"project_id": {"type": "string"}},
            [],
        ),
    },
    {
        "name": "reconcile_workflow_metadata_outbox",
        "description": "受锁重放已本地提交但尚未同步到 PostgreSQL/MinIO 的 durable outbox；回读事件链头成功后清除队列。",
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "reconciled_by": {"type": "string", "minLength": 1},
            },
            ["reconciled_by"],
        ),
    },
    {
        "name": "get_project_revision_history",
        "description": "读取项目阶段调整历史、发起人、原因和前后差异报告路径。只读。",
        "inputSchema": _object_schema(
            {"project_id": {"type": "string"}},
            [],
        ),
    },
    {
        "name": "verify_ontology_project_integrity",
        "description": "显式重算正式阶段产物指纹并校验审计事件哈希链；只读，但会扫描阶段正式文件。",
        "inputSchema": _object_schema(
            {"project_id": {"type": "string"}},
            [],
        ),
    },
    {
        "name": "retry_failed_stage",
        "description": "修正门禁输入后，把当前失败的 S1～S7 阶段恢复为 RUNNING。不会跳过门禁。",
        "inputSchema": _object_schema(
            {"project_id": {"type": "string"}},
            ["project_id"],
        ),
    },
    {
        "name": "retry_release_runtime_deployment",
        "description": (
            "S7 发布包已批准但运行时部署或验证失败时，复用原批准和不可变发布包重新部署并验证运行时。"
            "不重新审批、不重新生成发布包、不回退 S6。"
        ),
        "inputSchema": _object_schema(
            {
                "project_id": {"type": "string"},
                "requested_by": {"type": "string", "minLength": 1},
            },
            ["project_id"],
        ),
    },
    {
        "name": "resume_active_revision",
        "description": (
            "恢复已经创建但因进程中断而停滞的工程修订。平台依据 active_revision、"
            "required_revalidation_stages 和当前阶段状态计算唯一续跑点；不会重开未受影响的阶段，"
            "也不会创建新工程或绕过任何门禁。"
        ),
        "inputSchema": _object_schema(
            {"project_id": {"type": "string", "minLength": 1}},
            ["project_id"],
        ),
    },
]
