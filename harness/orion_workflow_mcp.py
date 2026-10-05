from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from harness.mcp_argument_validation import describe_handler_type_error, validate_tool_arguments
from harness.orion_workflow_contracts import (
    BUSINESS_RULE_CANDIDATE_SCHEMA,
    CQ_SEMANTIC_ASSESSMENT_SCHEMA,
    ONTOLOGY_CANDIDATE_SCHEMA,
    S3_RUNTIME_SUBMISSION_SCHEMA,
    s3_runtime_structure_diagnostics,
)
from harness.orion_workflow_tool_schemas import (  # noqa: F401  (re-exported)
    SOURCE_SCOPE_SCHEMA,
    TOOLS,
    _object_schema,
)
from services.ingestion.source_preflight import inspect_source
from services.ontology_engineering import (
    OntologyWorkflowService,
    WorkflowError,
    WorkflowGateError,
)
from services.ontology_engineering.project_artifact_read import read_project_artifact
from services.ontology_engineering.revision_draft_inheritance import (
    fork_s2_draft_from_history,
    fork_s3_draft_from_history,
)
from services.ontology_engineering.runtime_capability_read import read_runtime_capability
from services.ontology_engineering.stage_jobs import read_stage_job, start_stage_job
from services.ontology_engineering.workflow import (
    WORKFLOW_VERSION,
)
from services.structured_data.snapshot_capture import list_source_connections

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_WORKFLOW_HOME = Path(__file__).parents[1] / ".orion-workflows"
DEFAULT_DOCUMENT_INGESTION_ROOT = Path(__file__).parents[1] / "data" / "unstructured"
SUPPORTED_WORKSPACE_SOURCE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".xlsx",
        ".xlsm",
        ".csv",
        ".tsv",
        ".txt",
        ".md",
        ".html",
        ".htm",
        ".xml",
        ".json",
        ".yaml",
        ".yml",
        ".eml",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
    }
)
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "get_ontology_workflow_status",
        "get_next_workflow_action",
        "get_stage_input_contract",
        "get_stage_draft",
        "get_business_quality",
        "get_business_preview",
        "list_ontology_projects",
        "get_workflow_storage_status",
        "get_project_revision_history",
        "verify_ontology_project_integrity",
        "preflight_stage_submission",
        "preflight_workspace_snapshot",
        "get_message_attachment_candidates",
        "get_document_ingestion_job",
        "get_managed_stage_execution",
        "get_runtime_capability",
        "get_project_artifact",
        "query_source_evidence",
        "get_cq_semantic_review",
        "get_design_workspace",
        "get_revision_reuse_plan",
        "list_source_connections",
    }
)


def _read_only_mode() -> bool:
    return os.getenv("ORION_MCP_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_TOOL_INPUT_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


class OrionWorkflowTools:
    def __init__(self, service: OntologyWorkflowService | None = None) -> None:
        root = os.getenv("ORION_WORKFLOW_HOME", str(DEFAULT_WORKFLOW_HOME))
        self.service = service or OntologyWorkflowService(root)
        self._failed_write_fingerprints: dict[tuple[str, str], str] = {}

    @staticmethod
    def _call_fingerprint(name: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"name": name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def _project_revision_token(self, project_id: str) -> str:
        """A gate rejection only applies to the formal state it was judged against."""
        if not project_id:
            return ""
        try:
            state = self.service._read_state(self.service._resolve_project(project_id))
        except Exception:  # noqa: BLE001 - unknown project: handler reports the real error
            return ""
        return str(state.get("revision") or "")

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, Any]:
        from services.ingestion.document_jobs import (
            commit_document_job,
            document_input_root,
            enqueue_document_job,
            read_document_job,
        )
        from services.ingestion.message_attachments import (
            get_message_attachment_candidates,
            snapshot_message_attachments,
        )

        if any(str(key).startswith("_") for key in arguments):
            raise WorkflowGateError(
                "G-MCP-PUBLIC-PARAMETERS",
                "MCP 仅接受公开工具参数，不能指定平台内部流程版本或内部评审标志。",
            )

        if _read_only_mode() and name not in READ_ONLY_TOOL_NAMES:
            raise WorkflowGateError(
                "G-MCP-READ-ONLY",
                f"候选灰度处于只读模式，不允许执行工具：{name}",
            )
        if name == "create_ontology_project" and os.getenv(
            "ORION_REQUIRE_UI_CREATE_CONFIRMATION", ""
        ).strip().lower() in {"1", "true", "yes", "on"}:
            raise WorkflowGateError(
                "G-CREATE-UI-CONFIRM",
                "工程尚未获得页面确认：请先展示工程建设确认卡，等待用户点击“确认创建工程”。"
                "对话模型无权直接创建或启动 S0。",
            )
        if name == "create_ontology_project":
            from services.ingestion.document_jobs import (
                validate_declared_document_source_identities,
            )

            arguments = dict(arguments)
            if "source_snapshot_path" in arguments:
                if "source_scope" in arguments:
                    raise WorkflowError("source_snapshot_path 与 source_scope 互斥")
                if arguments.get("intake_mode") != "DOCUMENT_ONLY" or arguments.get("datasource_label") or arguments.get("table_scope"):
                    raise WorkflowError("source_snapshot_path 仅支持无数据库范围的 DOCUMENT_ONLY 工程")
                snapshot_path = arguments.pop("source_snapshot_path")
                arguments["source_scope"] = self._preflight_workspace_snapshot(snapshot_path)["source_scope"]
            validate_declared_document_source_identities(arguments.get("source_scope"))
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "create_ontology_project": self.service.create_project,
            "get_message_attachment_candidates": lambda **args: get_message_attachment_candidates(root=document_input_root(), **args),
            "snapshot_message_attachments": lambda **args: snapshot_message_attachments(root=document_input_root(), **args),
            "snapshot_workspace_sources": self._snapshot_workspace_sources,
            "preflight_workspace_snapshot": self._preflight_workspace_snapshot,
            "amend_competency_questions": self.service.amend_competency_questions,
            "reconcile_document_source_identities": self.service.reconcile_document_source_identities,
            "start_document_ingestion_job": lambda **args: enqueue_document_job(self.service, **args),
            "get_document_ingestion_job": read_document_job,
            "commit_document_ingestion_job": lambda **args: commit_document_job(self.service, **args),
            "record_document_evidence": self.service.record_document_evidence,
            "record_s0_scope_decision": self.service.record_s0_scope_decision,
            "record_data_understanding": self.service.record_data_understanding,
            "record_document_understanding": self.service.record_document_understanding,
            "list_source_connections": lambda: list_source_connections(),
            "capture_database_snapshot": self._capture_database_snapshot,
            "record_data_understanding_from_datasets": self.service.record_data_understanding_from_datasets,
            "query_source_evidence": self.service.query_source_evidence,
            "record_semantic_candidates": self.service.record_semantic_candidates,
            "get_cq_semantic_review": self.service.get_cq_semantic_review,
            "get_design_workspace": self._get_design_workspace,
            "get_revision_reuse_plan": self.service.get_revision_reuse_plan,
            "preflight_design_patch": self.service.preflight_design_patch,
            "prepare_mapping_review": self._prepare_mapping_review,
            "resolve_mapping_confirmation": lambda **args: self._resolve_mapping_with_checkpoint(option_only=False, **args),
            "resolve_mapping_option": lambda **args: self._resolve_mapping_with_checkpoint(option_only=True, **args),
            "preview_stage_rollback": self.service.preview_stage_rollback,
            "reopen_stage_for_correction": self._reopen_stage_for_correction,
            "generate_ontology_design": self.service.generate_ontology_design,
            "prepare_ontology_design_review": self.service.prepare_ontology_design_review,
            "resolve_competency_question_review": self.service.resolve_competency_question_review,
            "record_ontology_build": self.service.record_ontology_build,
            "start_managed_stage_execution": lambda **args: start_stage_job(self.service, **args),
            "get_managed_stage_execution": lambda **args: read_stage_job(self.service, **args),
            "get_runtime_capability": lambda **args: read_runtime_capability(self.service, **args),
            "get_project_artifact": lambda **args: read_project_artifact(self.service, **args),
            "preflight_stage_submission": self._preflight_stage_submission,
            "get_stage_input_contract": self._get_stage_input_contract,
            "save_stage_submission": self._save_stage_submission,
            "generate_mapping_skeleton": self._generate_mapping_skeleton,
            "compile_mapping_runtime": self._compile_mapping_runtime,
            "get_stage_draft": self._get_stage_draft,
            "fork_s2_draft_from_history": self._fork_s2_draft_from_history,
            "fork_s3_draft_from_history": self._fork_s3_draft_from_history,
            "get_business_quality": self._get_business_quality,
            "get_business_preview": self._get_business_preview,
            "start_business_preview": self._start_business_preview,
            "replace_document_sources": self.service.replace_document_sources,
            "patch_stage_submission": self._patch_stage_submission,
            "commit_preflight_stage_submission": self._commit_preflight_stage_submission,
            "record_quality_validation": self.service.record_quality_validation,
            "publish_ontology_package": self.service.publish_ontology_package,
            "sync_published_ontology_to_semantica": self.service.sync_published_ontology_to_semantica,
            "defer_ontology_publication": self.service.defer_ontology_publication,
            "resume_ontology_publication": self.service.resume_ontology_publication,
            "revoke_ontology_release": self.service.revoke_ontology_release,
            "create_revision_from_release": self.service.create_revision_from_release,
            "archive_ontology_project": self.service.archive_project,
            "restore_ontology_project": self.service.restore_project,
            "get_ontology_workflow_status": self._get_workflow_status,
            "get_next_workflow_action": self.service.get_next_action,
            "list_ontology_projects": self.service.list_projects,
            "get_workflow_storage_status": self.service.get_storage_status,
            "reconcile_workflow_metadata_outbox": self.service.reconcile_metadata_outbox,
            "get_project_revision_history": self.service.get_revision_history,
            "verify_ontology_project_integrity": self.service.verify_project_integrity,
            "retry_failed_stage": self.service.retry_failed_stage,
            "retry_release_runtime_deployment": self.service.retry_release_runtime_deployment,
            "resume_active_revision": self.service.resume_active_revision,
        }
        handler = handlers.get(name)
        if handler is None:
            raise WorkflowError(f"不允许的工具：{name}")
        validate_tool_arguments(name, arguments, _TOOL_INPUT_SCHEMAS)
        project_id = str(arguments.get("project_id") or "").strip()
        failure_key = (project_id, name)
        call_fingerprint = self._call_fingerprint(
            name, {**arguments, "__state_revision__": self._project_revision_token(project_id),
                   "__validator__": self.service._validator_fingerprint()},
        )
        if (
            name not in READ_ONLY_TOOL_NAMES
            and self._failed_write_fingerprints.get(failure_key) == call_fingerprint
        ):
            raise WorkflowGateError(
                "G-REPEATED-FAILED-SUBMISSION",
                "这组参数已被同一门禁拒绝，平台已熔断原样重试。请先根据 gate 修正载荷并"
                "通过 preflight_stage_submission；不得把内部契约字段转问给业务用户。",
            )
        try:
            result = handler(**arguments)
        except WorkflowGateError:
            if name not in READ_ONLY_TOOL_NAMES:
                self._failed_write_fingerprints[failure_key] = call_fingerprint
            raise
        except TypeError as error:
            raise describe_handler_type_error(name, error) from error
        else:
            self._failed_write_fingerprints.pop(failure_key, None)
        return self._summary(name, result), result

    def _capture_database_snapshot(self, *, project_id: str, **arguments: Any) -> dict[str, Any]:
        from services.ontology_engineering.source_scope import (
            SourceScopeError,
            normalize_source_scope,
            validate_database_capture_scope,
        )
        from services.structured_data.multi_source import MultiSourceContractError
        from services.structured_data.snapshot_capture import (
            SnapshotCaptureError,
            capture_database_snapshot,
        )

        if "scope_preflight" in arguments:
            raise WorkflowGateError("G-S1-SOURCE-SCOPE", "来源范围预检由平台生成，不能通过工具参数替换。")
        try:
            with self.service._project_mutation_lock(project_id) as project_dir:
                scope_path = project_dir / "00-document-evidence/source-scope.json"
                project = self.service._read_json(project_dir / "project.json")
                if scope_path.is_file():
                    scope = self.service._read_json(scope_path)
                elif project.get("source_scope"):
                    scope = project["source_scope"]
                else:
                    scope = normalize_source_scope(intake_mode=project.get("intake_mode", "HYBRID"))

                def preflight(binding, profile):
                    validate_database_capture_scope(scope, binding.model_dump(mode="json"),
                        profile.model_dump(mode="json") if profile is not None else None)

                return capture_database_snapshot(project_id=project_id, scope_preflight=preflight, **arguments)
        except SourceScopeError as error:
            raise WorkflowGateError(error.gate, str(error)) from error
        except (SnapshotCaptureError, MultiSourceContractError) as error:
            raise WorkflowGateError("G-S1-SOURCE-CAPTURE", str(error)) from error

    def _get_design_workspace(self, *, project_id: str, component_ids: list[str] | None = None):
        if component_ids is not None and (not isinstance(component_ids, list) or len(component_ids) > 30):
            raise WorkflowError("component_ids最多30条。")
        result = self.service.get_design_workspace(project_id)
        workspace = result.pop("workspace")
        components = workspace["components"]
        if any(key not in components for key in component_ids or []):
            raise WorkflowError("请求的设计组件不存在，请刷新共用设计索引。")
        return {**result, "snapshot_sha256": workspace["snapshot_sha256"],
                "authority": workspace["authority"], "issues": workspace["issues"],
                "approval_inherited": False,
                "components": {key: components[key] for key in component_ids} if component_ids
                else {key: {"kind": item["kind"], "id": item["id"],
                            "content_sha256": item["content_sha256"]} for key, item in components.items()}}

    def _get_workflow_status(
        self, *, project_id: str | None = None, response_mode: str = "FULL",
    ) -> dict[str, Any]:
        if response_mode not in {"FULL", "SUMMARY"}:
            raise WorkflowError("response_mode 必须是 FULL 或 SUMMARY。")
        if response_mode == "FULL":
            return self.service.get_status(project_id)
        project_dir = self.service._resolve_project(project_id)
        def observe():
            result = self.service.get_status(project_dir.name)
            compact = self._compact_commit_result(project_dir, result)
            action = self.service.get_next_action(project_dir.name) if compact is not result else None
            return result, compact, action

        (result, compact, action), snapshot, stable = self._read_only_snapshot(project_dir, observe)
        if not stable:
            return {**result, "response_mode": "FULL", "requested_response_mode": "SUMMARY",
                    "summary_fallback": "OBSERVATION_CHANGED_DURING_READ",
                    "status_refresh_required": True, "next_action_refresh_required": True}
        if compact is result:
            return {**result, "response_mode": "FULL", "requested_response_mode": "SUMMARY",
                    "summary_fallback": "REVIEW_REFERENCE_UNVERIFIED"}
        if (action.get("project_id") != result.get("project_id")
                or action.get("revision") != result.get("revision")
                or snapshot.get("revision") != result.get("revision")
                or snapshot.get("project_id") != result.get("project_id")):
            compact.update(next_action_refresh_required=True,
                           next_action_unavailable_reason="OBSERVATION_IDENTITY_MISMATCH")
            return compact
        compact["next_action"] = {key: value for key, value in action.items() if key != "stage_contracts"}
        compact["next_action_refresh_required"] = False
        return compact

    @staticmethod
    def _read_only_snapshot(project_dir: Path, observe: Callable[[], Any]) -> tuple[Any, dict[str, Any], bool]:
        """Bounded read consistency without writable ledger/lock prerequisites."""
        for _ in range(2):
            before = (project_dir / "workflow-state.json").read_bytes()
            result = observe()
            after = (project_dir / "workflow-state.json").read_bytes()
            if before == after:
                return result, json.loads(after), True
        return result, json.loads(after), False

    def _get_stage_input_contract(self, *, project_id: str, stage: str, section: str | None = None) -> dict[str, Any]:
        if stage not in {"S2", "S3", "S4"}:
            raise WorkflowError("输入合同仅支持 S2、S3、S4。")
        project_dir = self.service._resolve_project(project_id)
        contract, snapshot, stable = self._read_only_snapshot(
            project_dir, lambda: self._build_stage_input_contract(project_dir, stage),
        )
        if (not stable or contract["revision"] != snapshot.get("revision")
                or contract["project_id"] != snapshot.get("project_id")):
            return {"project_id": project_id, "stage": stage, "status": "STATUS_REFRESH_REQUIRED",
                    "status_refresh_required": True, "reason": "OBSERVATION_CHANGED_DURING_READ"}
        from services.ontology_engineering.input_contract_view import project_contract
        return project_contract(contract, section)

    def _build_stage_input_contract(self, project_dir: Path, stage: str) -> dict[str, Any]:
        project_id = project_dir.name
        stage_tools = {"S2": "record_semantic_candidates", "S3": "prepare_mapping_review", "S4": "generate_ontology_design"}
        state = self.service._read_state(project_dir)
        schema = json.loads(json.dumps(next(tool["inputSchema"] for tool in TOOLS if tool["name"] == stage_tools[stage])))
        for key in ("project_id", "expected_revision"):
            schema["properties"].pop(key, None)
            if key in schema.get("required", []):
                schema["required"].remove(key)
        mode = state.get("intake_mode")
        if stage == "S2" and state.get("business_modeling_contract_version"):
            from harness.orion_workflow_contracts import S2_INSTANCE_CONTRACT_REQUIRED
            from services.ontology_engineering import business_modeling_contract as business
            if business.enabled(state):
                item = schema["properties"]["ontology_candidates"]["items"]
                strict = dict(item["properties"]["instance_contract"], required=list(S2_INSTANCE_CONTRACT_REQUIRED))
                item["allOf"] = [{
                    "if": {"properties": {"kind": {"const": "CLASS"}}, "required": ["kind"]},
                    "then": {"required": ["instance_contract"], "properties": {"instance_contract": strict}},
                }]
        if stage == "S3":
            # This schema describes draft content, not the MCP transport envelope.
            schema.pop("oneOf", None)
            schema["properties"].pop("payload_file", None)
            schema["properties"].pop("review_scope", None)
            schema["required"] = ["mapping_draft", "confirmations", "automatic_decisions"]
            from harness.mapping_authoring_contract import mapping_draft_schema
            schema["properties"]["mapping_draft"] = mapping_draft_schema(
                mode, business_modeling_contract_version=state.get("business_modeling_contract_version"),
            )
        skeletons = {
            "S2": {"ontology_candidates": [], "business_rule_candidates": [], "cq_semantic_assessments": []},
            "S3": {"mapping_draft": {"mappings": []}, "confirmations": [], "automatic_decisions": [], "realtime_runtime": {"document_fact_queries": {}, "reasoning_capabilities": {}}},
            "S4": {"generation_request": {"logical_axioms": [], "review_policy": "HUMAN_REQUIRED"}},
        }
        if stage == "S3" and mode == "DOCUMENT_ONLY":
            runtime = schema["properties"].get("realtime_runtime", {})
            for field in ("mapping_obda", "ontop_queries", "query_capabilities", "database_binding", "database_access_mode"):
                runtime.get("properties", {}).pop(field, None)
        if stage == "S4":
            schema = _object_schema({"generation_request": schema}, ["generation_request"])
        refs = []
        for relative in ("00-document-evidence/source-scope.json", "00-document-evidence/cq-intake.json", "00-document-evidence/document-evidence.json", "01-data-understanding/source-understanding.json", "02-semantic-recognition/capability-plan.json", "03-mapping-review/mapping.yaml", "03-mapping-review/pending-confirmations.json"):
            try:
                with self._open_workspace_source(project_dir, relative) as stream:
                    byte_count = os.fstat(stream.fileno()).st_size
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                refs.append({"path": relative, "sha256": "sha256:" + digest, "byte_count": byte_count})
            except WorkflowError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    continue
                raise
        from services.ontology_engineering.stage_execution_guidance import (
            build_stage_execution_guidance,
        )
        return {
            "project_id": project_id, "revision": state["revision"], "stage": stage,
            "execution_semantics": build_stage_execution_guidance(stage, intake_mode=mode),
            "intake_mode": mode, "status": "INPUT_CONTRACT_ONLY", "payload_schema": schema,
            "payload_skeleton": skeletons[stage], "source_refs": refs,
            "cq_requirements_read": {"tool": "get_cq_semantic_review", "args": {"project_id": project_id},
                "instruction": "读取 cq_output_requirements 获取与现有校验器一致的 CQ 输出要求及真实证据索引，不必翻源码猜维度。"},
            "draft_checkpoint": self._get_stage_draft(project_id=project_id, stage=stage),
            "assembly_plan": {
                "S2": ["ontology_candidates", "business_rule_candidates", "cq_semantic_assessments"],
                "S3": ["mapping_draft", "realtime_runtime", "confirmations", "automatic_decisions"],
                "S4": ["generation_request"],
            }[stage],
            "checkpoint_policy": {"validation_mode": "CHECKPOINT", "formal_commit_requires": "PREFLIGHT_PASSED_TOKEN",
                "instruction": "先保存一小批有来源的内容，再分批 patch；未完成字段可留空，不补造事实。每批完成即落盘，不在一次输出里穷尽全部设计。S2 描述业务语义与能力缺口，S3 完成执行绑定，S4 联合批准，S6 验证真实结果。"},
            "cq_binding_support": {
                "status": "DOCUMENT_AND_REASONING_CQ_BINDING" if mode == "DOCUMENT_ONLY" else "STRUCTURED_DOCUMENT_AND_REASONING_CQ_BINDING",
                "supported_path": "realtime_runtime.document_fact_queries.<query_name>.cq_bindings" if mode == "DOCUMENT_ONLY" else "realtime_runtime.query_capabilities.<query_name>.cq_bindings",
                "supported_document_path": "realtime_runtime.document_fact_queries.<query_name>.cq_bindings",
                "supported_reasoning_path": "realtime_runtime.reasoning_capabilities.<capability_name>.cq_bindings",
                "unsupported_path": "realtime_runtime.cq_bindings",
                "unsupported_document_fact_query": False,
                "instruction": "文档事实与聚合 CQ 使用文档能力的只读 sparql、ontology_terms、位置 field 绑定和真实行结果用例；规则 CQ 使用真实规则及 cq_sparql。两者均验证完整维度、来源、实例与边界，不为普通查询制造规则。",
            },
            "instruction_zh": "骨架中的空对象/数组不是可提交的业务事实。依据当前工程正式来源填齐语义、规则与 CQ，保留完整范围；结构化查询的 S4 绑定来自 realtime_runtime.query_capabilities.<query_name>.cq_bindings；文档事实与聚合 CQ 来自 realtime_runtime.document_fact_queries.<query_name>.cq_bindings，使用 sparql 和 ontology_terms；规则 CQ（含纯资料）来自 realtime_runtime.reasoning_capabilities.<capability_name>.cq_bindings，补齐同能力的 business_question_ids、parameters、result_fields 和 validation_cases。禁止顶层 cq_bindings，不编造 Ontop 资产或规则。首次使用 save_stage_submission(validation_mode=CHECKPOINT) 尽早保存部分草稿，分批 patch_stage_submission(validation_mode=CHECKPOINT)；齐备后 preflight_stage_submission 完整预检；错误修正用 patch_stage_submission，仅传变化字段和基线payload_file，不重写全稿；只有 PASSED 后才提交 token。",
        }

    def _get_business_quality(self, *, project_id: str) -> dict[str, Any]:
        from services.ontology_engineering.business_quality import build_business_quality
        project_dir = self.service._resolve_project(project_id)
        result, _, stable = self._read_only_snapshot(project_dir, lambda: build_business_quality(
            self, project_dir, self.service._read_state(project_dir)))
        if not stable:
            raise WorkflowError("工程状态在检查期间变化，请重新读取业务质量。")
        return result

    def _get_business_preview(self, *, project_id: str) -> dict[str, Any]:
        from services.ontology_engineering.business_preview import get_business_preview
        return get_business_preview(self, project_id=project_id)

    def _start_business_preview(self, **arguments) -> dict[str, Any]:
        from services.ontology_engineering.business_preview import start_business_preview
        return start_business_preview(self, **arguments)

    def _get_stage_draft(self, *, project_id: str, stage: str, pointer: str | None = None,
                         offset: int = 0, limit: int = 6000) -> dict[str, Any]:
        from services.ontology_engineering.stage_checkpoint import read_checkpoint_view
        project_dir = self.service._resolve_project(project_id)
        def observe():
            state = self.service._read_state(project_dir)
            return read_checkpoint_view(self, project_dir, state, stage, pointer, offset, limit)
        result, _, stable = self._read_only_snapshot(project_dir, observe)
        if not stable:
            return {"project_id": project_id, "stage": stage, "status": "STATUS_REFRESH_REQUIRED"}
        return result

    def _fork_s3_draft_from_history(self, *, project_id: str, from_revision_id: str,
                                    expected_revision: int) -> dict[str, Any]:
        return fork_s3_draft_from_history(self, project_id=project_id,
                                         from_revision_id=from_revision_id,
                                         expected_revision=expected_revision)

    def _fork_s2_draft_from_history(self, *, project_id: str, from_revision_id: str,
                                    expected_revision: int) -> dict[str, Any]:
        return fork_s2_draft_from_history(self, project_id=project_id,
                                         from_revision_id=from_revision_id,
                                         expected_revision=expected_revision)

    def _save_stage_submission(self, *, project_id: str, stage: str,
                               payload: dict[str, Any], expected_revision: int,
                               validation_mode: str = "PREFLIGHT",
                               _base_payload_file: dict[str, Any] | None = None,
                               _must_have_no_checkpoint: bool = False,
                               _allow_business_review_handoff: bool = False) -> dict[str, Any]:
        if validation_mode not in {"CHECKPOINT", "PREFLIGHT"}:
            raise WorkflowError("validation_mode 必须是 CHECKPOINT 或 PREFLIGHT。")
        if stage not in {"S1", "S2", "S3", "S4", "S5", "S6"} or not isinstance(payload, dict):
            raise WorkflowError("草稿必须是 S1-S6 的 JSON 对象。")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise WorkflowError("草稿必须绑定当前 expected_revision。")
        try:
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise WorkflowError("草稿必须为有限数值的有效 JSON。") from exc
        if len(content) > 8 * 1024 * 1024:
            raise WorkflowError("预检草稿超过 8 MiB 限制。")
        digest = hashlib.sha256(content).hexdigest()
        name = f"{stage.lower()}-{digest}.json"
        # Use no-follow directory descriptors: callers cannot choose paths or
        # overwrite formal artifacts, even if a draft directory was replaced.
        with self.service._project_mutation_lock(project_id):
            project_dir = self.service._resolve_project(project_id)
            state = self.service._read_state(project_dir)
            self.service._require_expected_revision(state, expected_revision)
            if validation_mode == "CHECKPOINT":
                action = self.service.get_next_action(project_id)
                review_handoff = (_allow_business_review_handoff and stage == "S3"
                    and state.get("current_stage") == "S3" and self.service._joint_design_enabled(state)
                    and state.get("stage_statuses", {}).get("S3") in {"RUNNING", "BLOCKED_HUMAN"})
                if not review_handoff and (stage not in {"S2", "S3", "S4"} or "save_stage_submission" not in action.get("allowed_write_tools", []) or (
                    state.get("current_stage") != stage and not (stage == "S4" and state.get("project_status") == "S1_S3_READY")
                )):
                    raise WorkflowError("当前阶段或执行状态不允许保存分步检查点。")
            if _base_payload_file is not None:
                latest = self._get_stage_draft(project_id=project_id, stage=stage)
                if latest.get("payload_file") and latest["payload_file"] != _base_payload_file:
                    raise WorkflowError("草稿已更新，请先 get_stage_draft 回读最新基线，禁止覆盖其他增量。")
            if _must_have_no_checkpoint and self._get_stage_draft(
                project_id=project_id, stage=stage,
            ).get("status") != "NO_CURRENT_CHECKPOINT":
                raise WorkflowError("当前修订已有检查点，禁止历史草稿覆盖。")
            root_fd = os.open(project_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with suppress(FileExistsError):
                    os.mkdir(".submission-drafts", mode=0o700, dir_fd=root_fd)
                draft_fd = os.open(".submission-drafts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    try:
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=draft_fd)
                    except FileExistsError:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=draft_fd)
                        with os.fdopen(fd, "rb") as stream:
                            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(len(content) + 1) != content:
                                raise WorkflowError("同名内容寻址草稿与载荷不一致。") from None
                    else:
                        try:
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(content)
                                stream.flush()
                                os.fsync(stream.fileno())
                        except BaseException:
                            os.unlink(name, dir_fd=draft_fd)
                            raise
                    from services.ontology_engineering.stage_checkpoint import write_checkpoint
                    write_checkpoint(draft_fd, stage, expected_revision, {
                        "project_id": project_id, "stage": stage, "revision": expected_revision,
                        "payload_file": {"file_name": name, "sha256": "sha256:" + digest},
                        "validation_mode": validation_mode,
                    })
                finally:
                    os.close(draft_fd)
            finally:
                os.close(root_fd)
        reference = {"file_name": name, "sha256": "sha256:" + digest}
        if validation_mode == "CHECKPOINT":
            return {"project_id": project_id, "stage": stage, "revision": expected_revision,
                    "status": "CHECKPOINT_SAVED", "draft_saved": True, "validated": False,
                    "payload_file": reference, "formal_state_changed": False,
                    "next_step": "分批 patch_stage_submission；齐备后 preflight_stage_submission，不得直接提交检查点。"}
        result = self._preflight_stage_submission(project_id=project_id, stage=stage, payload_file=reference, expected_revision=expected_revision)
        return {**result, "payload_file": reference, "draft_saved": True}

    def _resolve_mapping_with_checkpoint(self, *, option_only: bool, project_id: str, **kwargs):
        from harness.orion_s3_checkpoint_handoff import resolve_with_checkpoint
        return resolve_with_checkpoint(self, option_only=option_only, project_id=project_id, **kwargs)

    def _compile_mapping_runtime(self, *, project_id: str, expected_revision: int) -> dict[str, Any]:
        from harness.orion_mapping_runtime_tools import compile_current_mapping
        return compile_current_mapping(self, project_id=project_id, expected_revision=expected_revision)

    def _prepare_mapping_review(self, *, project_id: str, payload_file=None, **kwargs):
        from harness.orion_mapping_runtime_tools import prepare_mapping_from_draft
        return prepare_mapping_from_draft(self, project_id=project_id, payload_file=payload_file, **kwargs)

    def _generate_mapping_skeleton(self, *, project_id: str, expected_revision: int,
                                   namespace: str | None = None) -> dict[str, Any]:
        """Derive the S3 mapping skeleton from stored S1/S2 artefacts.

        The model reviews a summary instead of authoring every mapping by hand:
        a full draft is tens of thousands of characters and has repeatedly been
        truncated into invalid JSON on the model side, which no server-side
        limit can catch.  The skeleton is saved as a CHECKPOINT draft and only
        coverage plus open items come back.
        """
        import yaml

        from services.ontology_engineering.mapping_skeleton import build_mapping_skeleton

        project_dir = self.service._resolve_project(project_id)
        snapshot_path = project_dir / "01-data-understanding/schema-snapshot.json"
        candidate_path = project_dir / "02-semantic-recognition/ontology-candidates.yaml"
        if not snapshot_path.is_file():
            raise WorkflowError("缺少 S1 结构快照，请先完成数据理解；纯文档工程请手工编写资料映射。")
        if not candidate_path.is_file():
            raise WorkflowError("缺少 S2 业务语义候选，请先完成语义识别阶段。")
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            candidate_doc = yaml.safe_load(candidate_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise WorkflowError(f"读取 S1/S2 产物失败：{exc}") from exc
        candidates = candidate_doc.get("candidates") if isinstance(candidate_doc, dict) else None
        if not isinstance(snapshot, dict) or not isinstance(candidates, list) or not candidates:
            raise WorkflowError("S1/S2 产物结构不符合预期，无法生成映射骨架。")

        resolved_namespace = str(namespace or "").strip()
        if not resolved_namespace:
            existing = self._get_stage_draft(project_id=project_id, stage="S3")
            reference = existing.get("payload_file")
            payload = self._read_submission_payload(project_dir, reference)[0] if reference else None
            draft = (payload or {}).get("mapping_draft") if isinstance(payload, dict) else None
            resolved_namespace = str((draft or {}).get("namespace") or "").strip()
        if not resolved_namespace:
            raise WorkflowError("请提供本体命名空间 namespace，或先保存含 namespace 的 S3 草稿。")

        skeleton = build_mapping_skeleton(namespace=resolved_namespace, schema_snapshot=snapshot,
                                          candidates=candidates)
        saved = self._save_stage_submission(project_id=project_id, stage="S3",
                                            payload={"mapping_draft": skeleton["mapping_draft"], "confirmations": [], "automatic_decisions": []},
                                            expected_revision=expected_revision,
                                            validation_mode="CHECKPOINT")
        counts: dict[str, int] = {}
        for item in skeleton["open_items"]:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return {
            "project_id": project_id,
            "stage": "S3",
            "revision": expected_revision,
            "contract_version": skeleton["contract_version"],
            "status": "SKELETON_SAVED_PENDING_REVIEW",
            "validated": False,
            "formal_state_changed": False,
            "payload_file": saved.get("payload_file"),
            "namespace": resolved_namespace,
            "coverage": skeleton["coverage"],
            "open_item_counts": counts,
            "open_items": skeleton["open_items"],
            "review_note": skeleton["review_note"],
            "next_step": "用 get_stage_draft 分页回读骨架逐条复核业务命名与定义，用 patch_stage_submission 处理 open_items"
                         "（补绑列、拆多对多关系、确认数值/时间列的显式转换），补齐 confirmations、automatic_decisions"
                         "；调用 compile_mapping_runtime 生成运行设计草稿，处理其待办后再 preflight_stage_submission；骨架本身不是已批准映射。",
        }

    def _patch_stage_submission(self, *, project_id: str, stage: str,
                                payload_file: dict[str, Any], operations: list[dict[str, Any]],
                                expected_revision: int, validation_mode: str = "PREFLIGHT") -> dict[str, Any]:
        from services.ontology_engineering.draft_batch import (
            oversized_patch_problem,
            patch_batch_receipt,
        )
        from services.ontology_engineering.draft_patch import apply_draft_patch

        oversized = oversized_patch_problem(operations)
        if oversized is not None:
            raise WorkflowError(oversized)
        with self.service._project_mutation_lock(project_id):
            project_dir = self.service._resolve_project(project_id)
            state = self.service._read_state(project_dir)
            self.service._require_expected_revision(state, expected_revision)
            payload, source_receipt = self._read_submission_payload(project_dir, payload_file)
        if not isinstance(payload, dict):
            raise WorkflowError("基线草稿必须为JSON对象")
        try:
            patched = apply_draft_patch(payload, operations)
        except (ValueError, TypeError, RecursionError) as exc:
            raise WorkflowError(str(exc)) from exc
        result = self._save_stage_submission(project_id=project_id, stage=stage, payload=patched,
                                             expected_revision=expected_revision, validation_mode=validation_mode,
                                             _base_payload_file=payload_file)
        return {**result, "base_payload_file": payload_file, "patch_operation_count": len(operations),
                "base_payload_bytes": source_receipt["byte_count"],
                "draft_batch_usage": patch_batch_receipt(operations)}

    def _commit_preflight_stage_submission(
        self, *, project_id: str, stage: str, preflight_token: str,
        expected_revision: int | None = None, response_mode: str = "FULL",
    ) -> dict[str, Any]:
        if response_mode not in {"FULL", "SUMMARY"}:
            raise WorkflowError("response_mode 必须是 FULL 或 SUMMARY。")
        result = self.service.commit_preflight_stage_submission(
            project_id=project_id, stage=stage, preflight_token=preflight_token,
            expected_revision=expected_revision,
        )
        if response_mode == "FULL":
            return result
        try:
            project_dir = self.service._resolve_project(project_id)
        except (WorkflowError, OSError):
            return result
        return self._compact_commit_result(project_dir, result)

    def _compact_commit_result(self, project_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        review = result.get("competency_question_review")
        review_ref = None
        if review is not None:
            try:
                relative = "04-ontology-design/competency-question-review.json"
                with self._open_workspace_source(project_dir, relative) as stream:
                    content = stream.read(16 * 1024 * 1024 + 1)
                with self._open_workspace_source(project_dir, "workflow-state.json") as stream:
                    current = json.load(stream)
                if (len(content) > 16 * 1024 * 1024 or json.loads(content) != review
                        or current.get("revision") != result.get("revision")
                        or current.get("project_id") != result.get("project_id")):
                    return result
                questions = review.get("draft_questions") or []
                question_hash = "sha256:" + hashlib.sha256(json.dumps(
                    questions, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if question_hash != review.get("draft_questions_sha256"):
                    return result
                light = [{key: item[key] for key in ("id", "question", "expected")} for item in questions]
                if review.get("status") == "PENDING":
                    with self._open_workspace_source(project_dir, "04-ontology-design/ontology-design-draft.yaml") as stream:
                        draft = yaml.safe_load(stream)
                    if draft.get("competency_questions") != questions:
                        return result
                    from services.ontology_engineering.joint_design import verify_joint_review
                    if review.get("review_scope") == "JOINT_DESIGN":
                        verify_joint_review(project_dir, draft, review)
                    expanded = self.service._validate_competency_questions(self.service._complete_review_questions(draft, light))
                    if expanded != questions:
                        return result
                review_ref = {
                    "path": relative, "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "project_id": result["project_id"], "project_revision": result["revision"],
                    "joint_design_fingerprint": review.get("joint_design_fingerprint"),
                    "draft_questions_sha256": question_hash,
                    "instruction_zh": "按此完整正式审阅文件核验本体、映射、规则与 CQ；批准时原序原文回传 approval_questions 和当前 expected_revision，平台复用完整查询与答案契约。此引用不替代批准。",
                }
            except (WorkflowError, OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
                return result
        omitted = {"stage_contracts", "source_scope", "contract_chain", "capability_plan", "platform_capability_catalog",
                   "competency_question_review", "artifact_lifecycle"}
        compact = {key: value for key, value in result.items() if key not in omitted}
        lifecycle = result.get("artifact_lifecycle")
        if isinstance(lifecycle, dict):
            counts: dict[str, int] = {}
            for item in lifecycle.values():
                status = str((item or {}).get("status") or "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
                counts[status] = counts.get(status, 0) + 1
            compact["artifact_lifecycle_summary"] = {
                "total": len(lifecycle), "by_status": counts,
                "details_read": {"tool": "get_project_artifact", "arguments": {"project_id": result.get("project_id")}},
            }
        job = compact.get("document_ingestion_job")
        if isinstance(job, dict):
            compact["document_ingestion_job"] = {
                key: value for key, value in job.items()
                if key not in {"files", "source_preflight", "discovered_paths", "workflow", "warnings"}
            }
            if isinstance(job.get("warnings"), list):
                compact["document_ingestion_job"]["warning_count"] = len(job["warnings"])
            compact["document_ingestion_job"]["details_read"] = {
                "tool": "get_document_ingestion_job",
                "arguments": {"project_id": result.get("project_id"), "job_id": job.get("job_id")},
            }
        compact.update(response_mode="SUMMARY", full_status_read={"tool": "get_ontology_workflow_status", "arguments": {"project_id": result.get("project_id")}},
                       next_action_read={"tool": "get_next_workflow_action", "arguments": {"project_id": result.get("project_id")}})
        if review_ref:
            compact["review_ref"] = review_ref
            compact["competency_question_review"] = {key: value for key, value in review.items() if key not in {"joint_design_summary", "draft_questions", "approved_questions"}}
            if review.get("status") == "PENDING":
                compact["competency_question_review"]["approval_questions"] = light
                compact["competency_question_review"]["question_count"] = len(light)
        return compact

    def _reopen_stage_for_correction(self, *, response_mode: str = "SUMMARY", **arguments: Any) -> dict[str, Any]:
        """Reopen returns the full project status (hundreds of KB); agents get a verified compact receipt."""
        if response_mode not in {"FULL", "SUMMARY"}:
            raise WorkflowError("response_mode 必须是 FULL 或 SUMMARY。")
        result = self.service.reopen_stage_for_correction(**arguments)
        if response_mode == "FULL":
            return result
        try:
            project_dir = self.service._resolve_project(str(arguments.get("project_id") or ""))
        except (WorkflowError, OSError):
            return result
        return self._compact_commit_result(project_dir, result)

    def _read_submission_payload(self, project_dir: Path, payload_file: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(payload_file, dict) or set(payload_file) != {"file_name", "sha256"}:
            raise WorkflowError("payload_file 必须只包含 file_name、sha256。")
        name = payload_file["file_name"]
        checksum = payload_file["sha256"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json", name):
            raise WorkflowError("payload_file.file_name 必须是当前工程草稿目录中的 JSON 文件名。")
        if not isinstance(checksum, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", checksum):
            raise WorkflowError("payload_file.sha256 格式不合法。")
        relative_path = f".submission-drafts/{name}"
        limit = 8 * 1024 * 1024
        with self._open_workspace_source(project_dir, relative_path) as stream:
            before = self._file_state(os.fstat(stream.fileno()))
            if before[2] > limit:
                raise WorkflowError("预检草稿超过 8 MiB 限制。")
            content = stream.read(limit + 1)
            after = self._file_state(os.fstat(stream.fileno()))
        if len(content) > limit or before != after:
            raise WorkflowError("预检草稿读取期间发生变化或超过大小限制。")
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual != checksum:
            raise WorkflowError("预检草稿 SHA-256 不匹配，请重新核对当前文件。")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise WorkflowError("预检草稿必须是有效的 UTF-8 JSON 对象。") from exc
        source_receipt = {"path": relative_path, "sha256": actual, "byte_count": len(content)}
        return payload, source_receipt

    def _preflight_stage_submission(
        self, *, project_id: str, stage: str,
        payload: dict[str, Any] | None = None,
        payload_file: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if (payload is None) == (payload_file is None):
            raise WorkflowError("payload 与 payload_file 必须且只能提供一个。")
        source_receipt = None
        if payload_file is not None:
            project_dir = self.service._resolve_project(project_id)
            payload, source_receipt = self._read_submission_payload(project_dir, payload_file)
        if not isinstance(payload, dict):
            raise WorkflowError("预检载荷必须是 JSON 对象。")
        # The token must describe the same revision that was checked, even
        # when another client is also submitting work for this project.
        with self.service._project_mutation_lock(project_id):
            project_dir = self.service._resolve_project(project_id)
            state = self.service._read_state(project_dir)
            self.service._require_expected_revision(state, expected_revision)
            result = self.service.preflight_stage_submission(
                project_id=project_id, stage=stage, payload=payload,
            )
        if stage == "S2" and result.get("status") == "FAILED":
            paths = [str(issue.get("path") or "") for issue in result.get("issues", [])]
            fragments = {}
            if any(path.startswith("business_rule_candidates") for path in paths):
                fragments["business_rule_candidates_item"] = BUSINESS_RULE_CANDIDATE_SCHEMA
            if any(path.startswith("ontology_candidates") for path in paths):
                fragments["ontology_candidates_item"] = ONTOLOGY_CANDIDATE_SCHEMA
            if any(issue.get("gate") == "G-S2-CQ-SEMANTICS" for issue in result.get("issues", [])):
                fragments["cq_semantic_assessments_item"] = CQ_SEMANTIC_ASSESSMENT_SCHEMA
            if fragments:
                result["repair_contract"] = {
                    "source": "CURRENT_MCP_INPUT_SCHEMA",
                    "schemas": fragments,
                    "instruction": "按此正式字段合同修正草稿；只修改报告的问题，保留全部业务范围。"
                                   "大载荷使用 payload_file，修正后重新预检；不要猜字段名或原样重传。",
                }
        if (stage == "S3" and result.get("status") == "FAILED"
                and (isinstance(payload.get("realtime_runtime"), dict)
                     or any(issue.get("gate") == "G-S3-BUSINESS-COMPILATION" for issue in result.get("issues") or []))):
            diagnostics = (s3_runtime_structure_diagnostics(payload["realtime_runtime"])
                           if isinstance(payload.get("realtime_runtime"), dict) else [])
            result["repair_contract"] = {
                "source": "CURRENT_MCP_INPUT_SCHEMA",
                "schemas": {"realtime_runtime": S3_RUNTIME_SUBMISSION_SCHEMA},
                "diagnostics": diagnostics,
                "instruction": "按 prepare_mapping_review 的 realtime_runtime 正式合同修正 payload_file 草稿。"
                    "ontop_queries 每个值必须是 SPARQL 字符串；query_capabilities 键与其完全一致，"
                    "每项包含 description_zh、parameters 对象（无参数用 {}）、result_fields 字符串数组、"
                    "question_examples 字符串数组与 validation_cases 对象数组；真实用例使用 question、parameters、"
                    "expected_fields、min_rows、expected_first_row，不能用 expected_result 等自拟字段。"
                    "顶层 realtime_runtime.cq_bindings 不受支持，不能原样重试；结构化CQ在 query_capabilities.<query_name>.cq_bindings。"
                    "文档事实与聚合CQ在 document_fact_queries.<query_name>.cq_bindings，以FACT_QUERY绑定本能力sparql，补齐ontology_terms、parameters及真实行用例；"
                    "规则CQ在 reasoning_capabilities.<capability_name>.cq_bindings，并补齐同能力 business_question_ids、parameters、result_fields、validation_cases；"
                    "DOCUMENT_ONLY 禁止 Ontop 字段，可使用文档事实或真实规则CQ路线，不编造规则或用计数代替行结果用例；DATABASE_ONLY 无文档能力传 []。"
                    "REQUIRED 必须有正式推理能力；NOT_APPLICABLE 必须有至少12字具体依据。"
                    "形状正确不代表来源或生产验证通过；保留原门禁和全部 CQ，不读取源码猜字段或造结果。",
            }
            if not diagnostics and result.get("issues"):
                # The draft already satisfies the runtime shape. Re-sending the
                # whole schema on every semantic retry exhausts long agent
                # sessions; point to the authoritative sections instead.
                result["repair_contract"] = self._s3_section_repair_contract(
                    project_id, result["repair_contract"]["instruction"], result.get("issues") or [])
            elif diagnostics and any(issue.get("gate") == "G-S3-BUSINESS-COMPILATION" for issue in result.get("issues") or []):
                result["repair_contract"]["section_reads"] = [{"tool": "get_stage_input_contract", "args": {
                    "project_id": project_id, "stage": "S3", "section": "mapping_draft.business_query_plans"}}]
        # Presentation-only narrowing: retain every authoritative issue and token.
        # Shape errors or mixed gates need the complete runtime repair contract.
        issues = result.get("issues") or []
        repair = result.get("repair_contract") or {}
        if (stage == "S3" and result.get("status") == "FAILED" and issues
                and all(isinstance(issue, dict) and issue.get("gate") in {"G-S3-CQ-CONTRACT", "G-S3-EVIDENCE"}
                        for issue in issues)
                and isinstance(payload.get("realtime_runtime"), dict)
                and "diagnostics" in repair and not repair["diagnostics"]):
            paths = [str(issue.get("path") or "") for issue in issues if issue.get("gate") == "G-S3-CQ-CONTRACT"]
            kinds = ("query_capabilities", "document_fact_queries", "reasoning_capabilities")
            affected = [kind for kind in kinds
                        if any(path.startswith(f"realtime_runtime.{kind}.") for path in paths)]
            # Missing capability bindings may only identify the runtime root.
            # Offer all official variants in that case, without guessing a route.
            if not affected or any(not any(path.startswith(f"realtime_runtime.{kind}.")
                                           for kind in kinds) for path in paths):
                affected = list(kinds)
            fragments = {}
            for kind in affected:
                properties = S3_RUNTIME_SUBMISSION_SCHEMA["properties"][kind]["additionalProperties"]["properties"]
                fragments[f"{kind}.cq_bindings_item"] = properties["cq_bindings"]["additionalProperties"]
                fragments[f"{kind}.validation_cases_item"] = properties["validation_cases"]["items"]
            evidence_missing = any(issue.get("gate") == "G-S3-EVIDENCE" for issue in issues)
            if evidence_missing:
                fragments["mapping.source_refs"] = {"type": "array", "items": {"type": "string", "minLength": 1},
                    "minItems": 1, "description": "每条映射引用真实来源证据；source 描述不能替代 source_refs，不可编造引用。"}
            result["repair_contract"] = {
                "source": "CURRENT_MCP_INPUT_SCHEMA",
                "scope": "CQ_AND_MAPPING_EVIDENCE" if evidence_missing else "CQ_CONTRACT_ONLY",
                "schemas": fragments,
                "diagnostics": [],
                "instruction": "按 issues 的 path 局部补齐映射真实 source_refs，或修补 CQ binding、validation_cases，并同步相关查询的返回变量。"
                    "保留全部 CQ、业务输出维度、证据和边界条件；不得删减 result_fields 或断言来规避门禁，"
                    "不得编造验证结果。已有 payload_file 使用 patch_stage_submission，仅提交变化字段及基线引用；"
                    "没有文件引用时先保存修正草稿。修正后重新预检，只有 PASSED 才能提交新 token。",
            }
        if result.get("status") == "FAILED":
            from services.ontology_engineering.preflight_repair import build_preflight_repair_plan
            result["repair_plan"] = build_preflight_repair_plan(
                project_id=project_id, stage=stage, revision=state["revision"], payload=payload,
                issues=result.get("issues") or [], payload_file=payload_file,
            )
            if result["repair_plan"].get("managed_runtime_diagnostics"):
                result.setdefault("repair_contract", {})["instruction"] = result["repair_plan"]["instruction"]
        if source_receipt is not None:
            result["payload_source"] = source_receipt
            result = self._compact_file_preflight_result(project_dir, stage, result)
            from services.ontology_engineering.stage_checkpoint import record_checkpoint_preflight
            with self.service._project_mutation_lock(project_id):
                current = self.service._read_state(project_dir)
                if current["revision"] == state["revision"]:
                    record_checkpoint_preflight(self, project_dir, current, stage, payload_file, result)
        return result

    @staticmethod
    def _s3_section_repair_contract(
        project_id: str, instruction: str, issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Shape-valid drafts get on-demand section reads, not the full schema."""
        runtime_keys = tuple(S3_RUNTIME_SUBMISSION_SCHEMA.get("properties", {}))
        sections: list[str] = []
        business_failure = False
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("gate") == "G-S3-BUSINESS-COMPILATION":
                business_failure = True
                section = "mapping_draft.business_query_plans"
                if section not in sections:
                    sections.append(section)
            text_ = " ".join(str(issue.get(key) or "") for key in ("path", "message"))
            for key in runtime_keys:
                if re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text_):
                    section = f"realtime_runtime.{key}"
                    if section not in sections:
                        sections.append(section)
        if business_failure:
            business_instruction = (
                "业务计划字段读取 mapping_draft.business_query_plans 完整合同；按 repair_plan 区分局部计划修订、"
                "重新编译和来源核查。规则证据的 optional=true 位于 fields 每项内，不猜别名或删除业务范围。"
                "生成内容过期须 compile_mapping_runtime 后用最新 payload_file 完整预检，不手改生成查询、规则或 OBDA。"
            )
            if all(isinstance(issue, dict) and issue.get("gate") == "G-S3-BUSINESS-COMPILATION" for issue in issues):
                instruction = business_instruction
            else:
                instruction = business_instruction + instruction
        else:
            instruction = ("当前草稿的 realtime_runtime 结构已符合正式合同，失败来自语义或运行门禁；"
                           "请按 issues 的 message 与 path 局部修补，确需字段合同时再按 section_reads 读取对应分段，"
                           "不要重复读取完整合同。" + instruction)
        return {
            "source": "CURRENT_MCP_INPUT_SCHEMA",
            "scope": "SECTION_READ_ON_DEMAND",
            "schemas": {},
            "diagnostics": [],
            "section_reads": [
                {"tool": "get_stage_input_contract",
                 "args": {"project_id": project_id, "stage": "S3", "section": section}}
                for section in (sections or ["overview"])
            ],
            "instruction": instruction,
        }

    def _compact_file_preflight_result(
        self, project_dir: Path, stage: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        """Reference the actual frozen payload; retain the full receipt on any doubt."""
        preview_id = result.get("preflight_id")
        token = result.get("preflight_token")
        if (
            result.get("status") != "PASSED"
            or "normalized_payload" not in result
            or not isinstance(preview_id, str)
            or not re.fullmatch(r"PFL-[A-F0-9]{16}", preview_id)
            or not isinstance(token, str)
            or not token.startswith(preview_id + ".")
        ):
            return result
        relative_path = f".preflight-submissions/{preview_id}.json"
        try:
            # Reuse the no-follow dirfd reader instead of trusting a receipt path.
            limit = 16 * 1024 * 1024
            with self._open_workspace_source(project_dir, relative_path) as stream:
                before = self._file_state(os.fstat(stream.fileno()))
                if before[2] > limit:
                    return result
                content = stream.read(limit + 1)
                after = self._file_state(os.fstat(stream.fileno()))
            if before != after or len(content) != before[2] or len(content) > limit:
                return result
            snapshot = json.loads(content.decode("utf-8"))
            if not isinstance(snapshot, dict):
                return result
            checksum = "sha256:" + hashlib.sha256(json.dumps(
                snapshot.get("payload"), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            expires = datetime.fromisoformat(str(snapshot.get("expires_at") or ""))
            if (
                snapshot.get("project_id") != project_dir.name
                or result.get("project_id") != project_dir.name
                or snapshot.get("stage") != str(stage).strip().upper()
                or snapshot.get("preflight_id") != preview_id
                or snapshot.get("payload") != result["normalized_payload"]
                or snapshot.get("payload_sha256") != checksum
                or result.get("normalized_payload_sha256") != checksum
                or snapshot.get("token_sha256") != hashlib.sha256(token.encode("utf-8")).hexdigest()
                or snapshot.get("used_at") is not None
                or snapshot.get("expires_at") != result.get("preflight_expires_at")
                or expires.tzinfo is None
                or expires <= datetime.now().astimezone()
            ):
                return result
        except (WorkflowError, OSError, ValueError, TypeError, RecursionError):
            return result
        compact = dict(result)
        compact.pop("normalized_payload")
        compact["normalized_payload_ref"] = {
            "path": relative_path,
            "json_pointer": "/payload",
            "sha256": checksum,
            "hash_scope": "CANONICAL_JSON_PAYLOAD",
            "snapshot_sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "snapshot_byte_count": len(content),
            "instruction_zh": "这是当前工程本次预检的完整固化载荷；按此路径读取 JSON 的 /payload 审阅。原始草稿可能继续变化，正式提交使用本次 token；此引用不替代阶段审批。",
        }
        return compact

    @staticmethod
    def _configured_workspace_roots() -> tuple[Path, ...]:
        configured = os.getenv("ORION_WORKSPACE_REFERENCE_ROOTS", "").strip()
        if configured:
            candidates = [item for item in configured.split(os.pathsep) if item.strip()]
        else:
            user_home = Path.home()
            candidates = [user_home / "Desktop", user_home / "Documents", user_home / "Downloads"]
        return tuple(Path(item).expanduser().resolve() for item in candidates)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        return path == root or path.is_relative_to(root)

    @staticmethod
    def _file_state(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )

    @staticmethod
    @contextmanager
    def _open_workspace_source(workspace: Path, relative_path: str) -> Iterator[Any]:
        """Open one workspace file through no-follow dirfds, confined to workspace."""

        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise WorkflowError(f"@ 引用文件路径不安全：{relative_path}")
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_descriptors: list[int] = []
        file_descriptor: int | None = None
        try:
            directory_descriptors.append(os.open(workspace, directory_flags))
            for part in relative.parts[:-1]:
                directory_descriptors.append(
                    os.open(part, directory_flags, dir_fd=directory_descriptors[-1])
                )
            file_descriptor = os.open(
                relative.parts[-1],
                file_flags,
                dir_fd=directory_descriptors[-1],
            )
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise WorkflowError(f"@ 引用不是普通文件：{relative_path}")
            with os.fdopen(file_descriptor, "rb") as stream:
                file_descriptor = None
                yield stream
        except OSError as error:
            raise WorkflowError(
                f"@ 引用文件已变化、包含符号链接或无法安全读取：{relative_path}"
            ) from error
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            for descriptor in reversed(directory_descriptors):
                os.close(descriptor)

    @classmethod
    def _hash_workspace_source(
        cls,
        workspace: Path,
        relative_path: str,
    ) -> tuple[int, str, tuple[int, int, int, int, int]]:
        digest = hashlib.sha256()
        byte_count = 0
        with cls._open_workspace_source(workspace, relative_path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > 2 * 1024**3:
                raise WorkflowError(f"单个资料超过 2GB：{relative_path}")
            while chunk := stream.read(1024 * 1024):
                byte_count += len(chunk)
                if byte_count > 2 * 1024**3:
                    raise WorkflowError(f"单个资料超过 2GB：{relative_path}")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        state = cls._file_state(before)
        if cls._file_state(after) != state or byte_count != before.st_size:
            raise WorkflowError(f"@ 引用源文件在读取期间发生变化：{relative_path}")
        return byte_count, digest.hexdigest(), state

    @classmethod
    def _copy_workspace_source(
        cls,
        workspace: Path,
        destination: Path,
        relative_path: str,
        expected_bytes: int,
        expected_sha256: str,
        expected_state: tuple[int, int, int, int, int],
    ) -> None:
        digest = hashlib.sha256()
        byte_count = 0
        with cls._open_workspace_source(workspace, relative_path) as source_stream:
            before = os.fstat(source_stream.fileno())
            if cls._file_state(before) != expected_state:
                raise WorkflowError(
                    f"@ 引用源文件在哈希后、固化前发生变化：{relative_path}；本次未落地快照"
                )
            with destination.open("xb") as destination_stream:
                while chunk := source_stream.read(1024 * 1024):
                    written = destination_stream.write(chunk)
                    if written != len(chunk):
                        raise WorkflowError(f"受控资料写入不完整：{relative_path}")
                    byte_count += written
                    digest.update(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            after = os.fstat(source_stream.fileno())

        copied_sha256 = digest.hexdigest()
        if (
            cls._file_state(after) != expected_state
            or byte_count != expected_bytes
            or copied_sha256 != expected_sha256
        ):
            raise WorkflowError(f"@ 引用源文件在固化期间发生变化：{relative_path}；本次未落地快照")
        if destination.is_symlink() or destination.stat().st_size != expected_bytes:
            raise WorkflowError(f"受控资料写入校验失败：{relative_path}")

    @classmethod
    def _verify_workspace_snapshot(
        cls,
        target: Path,
        expected_manifest: dict[str, Any],
    ) -> None:
        reference_id = expected_manifest["reference_id"]

        def fail(reason: str) -> None:
            raise WorkflowError(f"受控资料快照完整性校验失败（{reference_id}）：{reason}")

        if target.is_symlink() or not target.is_dir():
            fail("快照根目录不存在、不是目录或已被替换为符号链接")
        manifest_path = target / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            fail("manifest.json 不存在、不是文件或已被替换为符号链接")
        try:
            stored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fail(f"manifest.json 无法读取：{exc}")
        if not isinstance(stored_manifest, dict):
            fail("manifest.json 顶层必须是对象")

        for key in ("reference_id", "source_path", "file_count", "total_bytes"):
            if stored_manifest.get(key) != expected_manifest[key]:
                fail(f"manifest.json 的 {key} 与当前内容指纹不一致")
        stored_workspace = stored_manifest.get("workspace_root")
        if not isinstance(stored_workspace, str) or not Path(stored_workspace).is_absolute():
            fail("manifest.json 的 workspace_root 不是绝对路径")

        stored_files = stored_manifest.get("files")
        if not isinstance(stored_files, list):
            fail("manifest.json 的 files 不是数组")
        expected_files = {item["path"]: item for item in expected_manifest["files"]}
        stored_files_by_path: dict[str, dict[str, Any]] = {}
        for item in stored_files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                fail("manifest.json 包含格式无效的文件项")
            relative_path = item.get("path")
            if not isinstance(relative_path, str):
                fail("manifest.json 包含无效文件路径")
            relative = Path(relative_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative_path in stored_files_by_path
            ):
                fail(f"manifest.json 包含不安全或重复的文件路径：{relative_path}")
            stored_files_by_path[relative_path] = item
        if stored_files_by_path != expected_files:
            fail("manifest.json 的文件清单、大小或 SHA-256 与当前内容指纹不一致")

        actual_files: set[str] = set()
        for item in target.rglob("*"):
            relative_path = item.relative_to(target).as_posix()
            if item.is_symlink():
                fail(f"快照中出现符号链接：{relative_path}")
            if item.is_file() and relative_path != "manifest.json":
                actual_files.add(relative_path)
        if actual_files != set(expected_files):
            fail("快照实际文件集合与 manifest.json 不一致")

        for relative_path, item in sorted(expected_files.items()):
            source = target / relative_path
            digest = hashlib.sha256()
            byte_count = 0
            try:
                with source.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    while chunk := stream.read(1024 * 1024):
                        byte_count += len(chunk)
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
            except OSError as exc:
                fail(f"无法读取快照文件 {relative_path}：{exc}")
            if cls._file_state(before) != cls._file_state(after):
                fail(f"校验期间快照文件发生变化：{relative_path}")
            if byte_count != item["bytes"] or f"sha256:{digest.hexdigest()}" != item["sha256"]:
                fail(f"快照文件大小或 SHA-256 不一致：{relative_path}")

    @classmethod
    def _snapshot_workspace_sources(
        cls,
        workspace_root: str,
        references: list[str],
    ) -> dict[str, Any]:
        from services.ingestion.document_jobs import (
            document_source_scope,
            verified_document_source,
        )

        workspace = Path(workspace_root).expanduser()
        if not workspace.is_absolute():
            raise WorkflowError("workspace_root 必须是当前 Harness 工作区的绝对路径")
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise WorkflowError(f"工作区不存在或不是目录：{workspace}")
        if not any(cls._is_within(workspace, root) for root in cls._configured_workspace_roots()):
            raise WorkflowError("当前工作区不在 ORION 允许的 @ 引用根目录内")

        from services.ingestion.document_jobs import document_input_root

        ingestion_root = document_input_root()
        files: dict[str, Path] = {}
        for raw_reference in references:
            reference = raw_reference.strip().strip("\"'")
            if reference.startswith("@"):
                reference = reference[1:]
            reference = reference.rstrip("/")
            relative = Path(reference or ".")
            if relative.is_absolute() or ".." in relative.parts:
                raise WorkflowError(f"@ 引用必须是工作区内的相对路径：{raw_reference}")
            candidate = workspace.joinpath(relative)
            cursor = workspace
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise WorkflowError(f"@ 引用不能包含符号链接：{raw_reference}")
            source = candidate.resolve()
            if not cls._is_within(source, workspace):
                raise WorkflowError(f"@ 引用越过了当前工作区：{raw_reference}")
            if not source.exists():
                raise WorkflowError(f"@ 引用不存在：{raw_reference}")
            if cls._is_within(ingestion_root, source):
                raise WorkflowError("不能把包含 ORION 受控资料区的目录作为 @ 引用")
            candidates = [source] if source.is_file() else sorted(source.rglob("*"))
            for item in candidates:
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix.lower() not in SUPPORTED_WORKSPACE_SOURCE_EXTENSIONS:
                    continue
                rel = item.relative_to(workspace).as_posix()
                if rel == "manifest.json":
                    raise WorkflowError("@ 引用根目录中的 manifest.json 为保留文件名，请先重命名")
                files[rel] = item
                if len(files) > 5000:
                    raise WorkflowError("@ 引用文件超过 5000 份，请缩小本次资料范围")
        if not files:
            raise WorkflowError("@ 引用中没有 ORION 支持处理的资料文件")

        manifest_files: list[dict[str, Any]] = []
        source_states: dict[str, tuple[int, int, int, int, int]] = {}
        total_bytes = 0
        fingerprint = hashlib.sha256()
        for relative_path in sorted(files):
            size, sha256, state = cls._hash_workspace_source(workspace, relative_path)
            source_states[relative_path] = state
            total_bytes += size
            if total_bytes > 20 * 1024**3:
                raise WorkflowError("本次 @ 引用资料总量超过 20GB，请分批处理")
            fingerprint.update(f"{relative_path}\0{size}\0{sha256}\n".encode())
            manifest_files.append(
                {"path": relative_path, "bytes": size, "sha256": f"sha256:{sha256}"}
            )

        reference_id = f"REFERENCE-{fingerprint.hexdigest()[:20].upper()}"
        relative_snapshot = Path(".orion-s0-uploads") / reference_id
        target = ingestion_root / relative_snapshot
        manifest = {
            "reference_id": reference_id,
            "workspace_root": str(workspace),
            "source_path": relative_snapshot.as_posix(),
            "file_count": len(manifest_files),
            "total_bytes": total_bytes,
            "files": manifest_files,
        }
        if os.path.lexists(target):
            cls._verify_workspace_snapshot(target, manifest)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{reference_id}-", dir=target.parent))
            try:
                for item in manifest_files:
                    destination = temporary / item["path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    cls._copy_workspace_source(
                        workspace,
                        destination,
                        item["path"],
                        item["bytes"],
                        item["sha256"].removeprefix("sha256:"),
                        source_states[item["path"]],
                    )
                (temporary / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                for file_path in temporary.rglob("*"):
                    if file_path.is_file():
                        file_path.chmod(0o400)
                for directory in sorted(
                    (path for path in temporary.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    directory.chmod(0o500)
                temporary.chmod(0o500)
                os.replace(temporary, target)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        source_path = relative_snapshot.as_posix()
        snapshot = verified_document_source(ingestion_root, source_path)
        return {
            **manifest,
            "snapshot_root": str(target),
            "source_scope": document_source_scope(snapshot, source_path=source_path),
        }

    @classmethod
    def _preflight_workspace_snapshot(cls, source_path: str) -> dict[str, Any]:
        from services.ingestion.document_jobs import (
            document_input_root,
            document_source_manifest,
            document_source_scope,
            verified_document_source,
        )

        ingestion_root = document_input_root()
        relative = Path(str(source_path).strip())
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkflowError("source_path 必须是 ORION 受控资料区内的相对路径")
        source = (ingestion_root / relative).resolve()
        uploads_root = (ingestion_root / ".orion-s0-uploads").resolve()
        if not cls._is_within(source, uploads_root):
            raise WorkflowError("source_path 不属于 ORION 受控上传快照")
        if not source.exists():
            raise WorkflowError("受控资料快照不存在")
        snapshot = verified_document_source(ingestion_root, source_path)
        return {
            **inspect_source(source),
            "source_snapshot": document_source_manifest(snapshot),
            "source_scope": document_source_scope(snapshot, source_path=source_path),
        }

    @staticmethod
    def _summary(name: str, result: dict[str, Any]) -> str:
        if name in {"get_design_workspace", "get_revision_reuse_plan"}:
            return json.dumps(result, ensure_ascii=False)
        if name == "list_source_connections":
            refs = [item["connection_env"] for item in result.get("connections", [])]
            return "已登记只读连接：" + ("、".join(refs) if refs else "无") + "。只返回变量名，不含凭据。"
        if name == "capture_database_snapshot":
            return (
                f"数据库快照已提升：{result['snapshot_version']}，dataset_ids={result['dataset_ids']}，"
                f"类型 {result['dataset_type']}，共 {sum(s['snapshot_rows'] for s in result['sources'])} 行。"
                + (
                    "注意：TEST_ONLY 快照不能通过 S1 生产门禁，正式推进请以 PRODUCTION 重新采集。"
                    if result["dataset_type"] == "TEST_ONLY"
                    else "下一步调用 record_data_understanding_from_datasets。"
                )
            )
        if name == "get_cq_semantic_review":
            return (
                f"CQ 语义检查：{result.get('status', 'NEEDS_REVIEW')}；"
                f"覆盖 {len(result.get('questions') or [])} 条问题，工程 revision={result.get('revision', '—')}。"
                "仅评估业务定义、模型与数据缺口；未修改阶段，不代表实例验收通过。"
            )
        if name == "reconcile_document_source_identities":
            return (
                "来源技术标识已按真实资料批次核对，授权范围保持不变；"
                f"工程 revision={result.get('revision', '—')}。"
            )
        if name in {"start_document_ingestion_job", "get_document_ingestion_job", "commit_document_ingestion_job"}:
            phase = result.get("status") or result.get("current_stage", "已提交")
            return f"资料任务 {result.get('job_id', '')}：{phase}。{result.get('message', '')}"
        if name == "get_message_attachment_candidates":
            return f"当前真实消息包含 {result['attachment_count']} 份附件候选；请依据用户授权选择明确子集。"
        if name == "snapshot_message_attachments":
            return f"已固化 {result['file_count']} 份所选消息附件；真实来源路径 {result['source_path']}。未推进工程。"
        if name == "snapshot_workspace_sources":
            return (
                f"已将 {result['file_count']} 份 @ 引用资料固化到 ORION 受控批次 "
                f"{result['reference_id']}。"
            )
        if name == "preflight_workspace_snapshot":
            return (
                f"资料预检完成：{result['file_count']} 份文件，"
                f"建议 {result['recommended_intake_mode']} / "
                f"{result['recommended_structured_data_action']}。"
            )
        if name == "list_ontology_projects":
            return f"当前共有 {result['count']} 个 ORION 本体工程项目。"
        if name == "get_project_revision_history":
            return f"项目 {result['project_id']} 共有 {result['count']} 次留痕调整。"
        if name in {"start_managed_stage_execution", "get_managed_stage_execution"}:
            execution = result.get("stage_execution") or {}
            parts = [
                f"{result.get('stage', '阶段')} 受管任务 {result.get('job_id') or '—'}："
                f"{result.get('status', 'UNKNOWN')}"
            ]
            if result.get("exit_code") is not None:
                parts.append(f"exit_code={result['exit_code']}")
            if result.get("workflow_stage") or result.get("workflow_revision") is not None:
                parts.append(
                    f"任务结束时工程阶段={result.get('workflow_stage') or '—'}、"
                    f"revision={result.get('workflow_revision', '—')}"
                )
            if result.get("error_message_zh"):
                parts.append(f"错误（{result.get('error_code')}）：{result['error_message_zh']}")
            elif execution.get("last_error"):
                if execution.get("belongs_to_current_job") is False:
                    parts.append(f"上一次尝试的执行错误（非本任务结果）：{execution['last_error']}")
                else:
                    parts.append(f"错误：{execution['last_error']}")
            status = result.get("status")
            if status in {"STARTED", "RUNNING", "ALREADY_RUNNING"}:
                parts.append("任务仍在运行，请继续调用 get_managed_stage_execution（wait_seconds=60）等待到终态，不要用 bash sleep")
            elif status == "COMPLETED":
                parts.append("任务已结束；继续前请回读 get_ontology_workflow_status 确认正式阶段与 revision")
            elif status in {"FAILED", "STALE", "NEEDS_RECONCILIATION", "FAILED_TO_START"}:
                parts.append("先回读正式状态，再按 get_next_workflow_action 给出的恢复动作处理")
            return "；".join(parts) + "。"
        if name == "get_next_workflow_action":
            return (
                f"项目 {result['project_id']} 的平台推荐动作："
                f"{result.get('action', 'READ_STATUS')}；"
                f"推荐工具 {result.get('recommended_tool', '无')}；"
                f"输入责任方 {result.get('input_owner', 'PLATFORM')}。"
            )
        if name == "record_document_understanding":
            return (
                "资料理解已由平台重新核验，结构化数据库子任务不适用；"
                f"当前阶段 {result.get('current_stage', '未知')}，"
                f"工程 revision={result.get('revision', '—')}。"
            )
        if name == "get_workflow_storage_status":
            return f"工作流存储状态：{result.get('status', 'UNKNOWN')}。"
        if name == "reconcile_workflow_metadata_outbox":
            return (
                f"工作流账本补同步：{result.get('status', 'UNKNOWN')}，"
                f"剩余 {result.get('pending_count', 0)} 项。"
            )
        if name == "verify_ontology_project_integrity":
            return f"项目完整性校验结果：{result.get('status', 'UNKNOWN')}。"
        if name == "sync_published_ontology_to_semantica":
            return f"Semantica 补同步结果：{result.get('status', 'UNKNOWN')}。"
        if name == "preview_stage_rollback":
            return (
                f"已预览回退到 {result['target_stage']} 的影响："
                f"{len(result.get('affected_downstream') or [])} 个下游阶段，"
                f"{result.get('artifact_impact', {}).get('current_count', 0)} 个当前产物；"
                f"project_revision={result['project_revision']}；"
                f"preview_token={result['preview_token']}；"
                f"expires_at={result['expires_at']}。"
            )
        if name == "get_stage_input_contract":
            return f"{result['stage']} 当前模式输入合同，revision={result['revision']}；骨架未填充，不代表阶段通过。"
        if name == "get_business_quality":
            return f"业务质量只读检查：{result['status']}；未改变正式阶段或审批。"
        if name in {"get_business_preview", "start_business_preview"}:
            return f"业务能力试运行：{result['status']}；仅验证所选草稿能力，不改变正式阶段、审批或发布。"
        if name == "compile_mapping_runtime":
            summary = result.get("summary") or {}
            stopped = (result.get("repair_progress") or {}).get("automatic_continuation") == "STOP"
            return (f"S3 运行设计编译：{result.get('status', '待核对')}；"
                    f"已编译 {summary.get('compiled_mapping_count', 0)} 条映射、"
                    f"{summary.get('business_query_count', 0)} 项业务查询、"
                    f"{summary.get('compiled_business_rule_count', 0)} 项业务规则，"
                    f"剩余 {len(result.get('open_items') or [])} 项待处理。"
                    "正式工程阶段未改变；静态编译不代表数据验收或发布通过。"
                    + ("本轮自动修订已停止，草稿与诊断已保留。" if stopped else ""))
        if name == "get_stage_draft":
            return f"{result['stage']} 草稿：{result['status']}；未改变正式阶段。"
        if result.get("status") == "CHECKPOINT_SAVED":
            return f"{result['stage']} 分步草稿已保存；尚未预检通过，不代表正式成果。"
        if name in {"preflight_stage_submission", "save_stage_submission", "patch_stage_submission", "preflight_design_patch"}:
            issue_count = len(result.get("issues") or [])
            summary = (
                f"{result.get('stage', '阶段')} 只读预检{result.get('status', 'UNKNOWN')}；"
                f"发现 {issue_count} 个问题，工程状态未改变。"
            )
            # Some MCP clients expose only text content to the model. Keep the
            # actionable receipt here as well as in structuredContent.
            if result.get("status") == "PASSED" and result.get("preflight_token"):
                summary += (
                    f" preflight_token={result['preflight_token']}；"
                    f"preflight_id={result.get('preflight_id', '—')}；"
                    f"expires_at={result.get('preflight_expires_at', '—')}。"
                    "请用此 token 提交本次固化载荷，不得借用其他操作的令牌。"
                )
            issues = result.get("issues") or []
            if issues:
                first = issues[0]
                summary += (
                    f" 首个问题：{first.get('gate', 'UNKNOWN')} — "
                    f"{first.get('message', '未提供说明')}"
                )
            return summary
        if name == "commit_preflight_stage_submission":
            return (
                f"{result.get('preflight_stage', result.get('stage', '本次阶段'))} 已使用 "
                f"preflight {result.get('preflight_id', '—')} 的固化载荷提交；"
                f"工程 revision={result.get('revision', '—')}。"
            )
        if name == "query_source_evidence":
            if "source_schema" in result:
                return f"来源结构已核验：{result['source_schema']['source_table']}，{len(result['source_schema']['columns'])} 个业务列；未查询数据行。"
            if result.get("validation_scope") == "SOURCE_EVIDENCE_ONLY":
                return (f"来源只读核验：完整结果 {result['total_row_count']} 行；返回 {result['returned_row_count']} 行，"
                        f"截断={result['truncated']}。工程状态未改变；尚未验证本体运行时。")
            return (
                f"来源只读核验：完整匹配 {result['matched_row_count']} 行、{result['total_group_count']} 组；"
                f"返回 {result['returned_group_count']} 组，截断={result['truncated']}。"
                "工程状态未改变；结果不代替业务口径确认。"
            )
        project_id = result.get("project_id", "—")
        status = result.get("project_status", "UNKNOWN")
        current_stage = result.get("current_stage") or (
            "已发布" if status == "PUBLISHED" else "阶段间待推进"
        )
        lines = [
            "## ORION 本体工程状态",
            "",
            f"- 项目：`{project_id}`",
            f"- 当前阶段：**{current_stage}**",
            f"- 项目状态：**{status}**",
        ]
        source_scope = result.get("source_scope")
        if isinstance(source_scope, dict):
            scope_status = {
                "UNRESOLVED": "待明确，不能视为全部授权",
                "PARTIAL": "已登记部分来源，仍待闭合",
                "DECLARED": "已登记范围，仍需实际来源验收",
                "OBSERVED": "已回读实际来源，未追认显式授权",
                "VERIFIED": "已按声明范围验收",
            }.get(str(source_scope.get("scope_status")), "待核验")
            lines.append(f"- 来源范围：{scope_status}")
        deployment = result.get("realtime_deployment")
        if isinstance(deployment, dict):
            deployment_state = deployment.get("state", "UNKNOWN")
            lines.extend(
                [
                    f"- Ontop 部署：**{deployment_state}**",
                    (
                        "- 结构化实时绑定：发布物校验 "
                        f"`{str(bool(deployment.get('artifact_verified'))).lower()}`，"
                        "运行时握手 "
                        f"`{str(bool(deployment.get('runtime_verified'))).lower()}`"
                    ),
                ]
            )
            if deployment.get("degraded_reason"):
                lines.append(f"- 未就绪原因：{deployment['degraded_reason']}")
        confirmation = result.get("next_confirmation")
        if confirmation:
            progress = result.get("confirmation_progress") or {}
            evidence = confirmation.get("evidence") or {}
            options = confirmation.get("options") or []
            recommended_id = confirmation.get("recommended_option_id")
            confidence = confirmation.get("confidence")
            confidence_text = (
                f"{round(confidence * 100)}%" if isinstance(confidence, int | float) else "未提供"
            )
            lines.extend(
                [
                    "",
                    (
                        "### S3 建模决策 "
                        f"{progress.get('current', 1)}/{progress.get('total', 1)}"
                        " · 高影响"
                    ),
                    "",
                    f"**{confirmation.get('title', '待确认的业务语义')}**",
                    "",
                    confirmation["question"],
                    "",
                    "#### 已掌握的证据",
                    "",
                ]
            )
            source_evidence = [
                *(evidence.get("database_facts") or []),
                *(evidence.get("business_materials") or []),
            ]
            if source_evidence:
                for fact in source_evidence:
                    summary = fact.get("summary") if isinstance(fact, dict) else str(fact)
                    refs = (
                        "、".join(str(ref) for ref in (fact.get("source_refs") or []))
                        if isinstance(fact, dict)
                        else ""
                    )
                    lines.append(f"- {summary or '—'}（来源：{refs or '未标注'}）")
            else:
                lines.append("- 暂未提供可追溯的来源证据")
            customer_interviews = evidence.get("customer_interviews") or []
            lines.extend(["", "**补充业务依据**"])
            if customer_interviews:
                for item in customer_interviews:
                    summary = item.get("summary") if isinstance(item, dict) else str(item)
                    lines.append(f"- {summary}")
            else:
                lines.append("- 暂未提供补充访谈记录")
            lines.extend(
                [
                    "",
                    f"**AI 判断**：{evidence.get('ai_inference', '—')}",
                    f"**置信度**：{confidence_text}",
                    "",
                    "#### 可选方案",
                    "",
                ]
            )
            for option in options:
                marker = "（推荐）" if option.get("id") == recommended_id else ""
                lines.extend(
                    [
                        f"**{option.get('label', option.get('id'))}{marker}**",
                        f"- 方案：{option.get('summary', '—')}",
                        f"- 影响：{option.get('impact', '—')}",
                        "",
                    ]
                )
            technical_impact = confirmation.get("technical_impact") or []
            if technical_impact:
                lines.extend(
                    [
                        "#### 技术影响",
                        "",
                        *[f"- {item}" for item in technical_impact],
                        "",
                    ]
                )
            basis = " + ".join(confirmation.get("decision_basis") or [])
            lines.extend(
                [
                    f"决策依据：{basis or '数据库证据 + 本体工程判断'}",
                    "",
                    "请选择一个方案。采用推荐方案可直接继续下一项建模决策。",
                ]
            )
        else:
            lines.extend(["", f"下一步：{result.get('resume_point', '—')}"])
        return "\n".join(lines)


def native_workflow_receipt(result: dict[str, Any]) -> str:
    """Keep the native progress projection intact when large MCP output spills."""
    stages = [f"S{index}" for index in range(8)]
    statuses = result.get("stage_statuses")
    allowed = {"PENDING", "RUNNING", "IN_PROGRESS", "PASSED", "SKIPPED", "NOT_APPLICABLE",
               "BLOCKED_HUMAN", "FAILED", "INVALIDATED", "DEFERRED"}
    published = (result.get("current_stage") is None and result.get("project_status") == "PUBLISHED"
                 and isinstance(statuses, dict)
                 and all(statuses.get(stage) in {"PASSED", "SKIPPED", "NOT_APPLICABLE"} for stage in stages))
    design_ready = (result.get("current_stage") is None and result.get("project_status") == "S1_S3_READY"
                    and isinstance(statuses, dict) and statuses.get("S3") == "PASSED")
    if (not isinstance(result.get("project_id"), str) or not result["project_id"]
            or len(result["project_id"]) > 120
            or any(char in result["project_id"] for char in "\r\n\0")
            or type(result.get("revision")) is not int or result["revision"] < 0
            or (result.get("current_stage") not in stages and not published and not design_ready)
            or not isinstance(statuses, dict)
            or any(statuses.get(stage) not in allowed for stage in stages)
            or result.get("status_refresh_required") is True):
        return ""
    receipt = {key: result[key] for key in ("project_id", "revision", "current_stage")}
    receipt["project_name"] = str(result.get("project_name") or "").replace("\r", " ").replace("\n", " ").replace("\0", " ")[:200]
    receipt["project_status"] = str(result.get("project_status") or "")[:80]
    receipt["stage_statuses"] = {stage: statuses[stage] for stage in stages}
    contracts = result.get("stage_contracts")
    if isinstance(contracts, dict) and isinstance(contracts.get("stages"), list):
        receipt["stage_names"] = {
            item["stage"]: item["name"].replace("\r", " ").replace("\n", " ").replace("\0", " ")[:120]
            for item in contracts["stages"]
            if isinstance(item, dict) and item.get("stage") in stages and isinstance(item.get("name"), str)
        }
    # Display state only: no next-action instructions, source facts, or approval.
    line = "ORION_WORKFLOW_RECEIPT_V1 " + json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    return line + "\n" if len(line) <= 4096 else ""


class McpServer:
    def __init__(self, tools: OrionWorkflowTools | None = None) -> None:
        self.tools = tools or OrionWorkflowTools()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        if request_id is None:
            return None
        method = message.get("method")
        try:
            if method == "initialize":
                params = message.get("params") or {}
                result = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "orion-ontology-workflow", "version": WORKFLOW_VERSION},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = (
                    [tool for tool in TOOLS if tool["name"] in READ_ONLY_TOOL_NAMES]
                    if _read_only_mode()
                    else TOOLS
                )
                result = {"tools": tools}
            elif method == "tools/call":
                params = message.get("params") or {}
                text_value, structured = self.tools.call(
                    str(params.get("name") or ""), params.get("arguments") or {}
                )
                result = {
                    # MCP 2025-06-18 recommends serialized structured results in
                    # TextContent for older clients. Harness's model projection
                    # reads content only, so summaries or field allowlists lose
                    # paths, revisions, preflight issues and approval payloads.
                    "content": [
                        {"type": "text", "text": native_workflow_receipt(structured) + text_value},
                        {
                            "type": "text",
                            "text": json.dumps(
                                structured, ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                    ],
                    "structuredContent": structured,
                    "isError": False,
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (WorkflowError, ValueError, TypeError) as exc:
            error: dict[str, Any] = {"code": -32000, "message": str(exc)}
            if isinstance(exc, WorkflowGateError):
                error["data"] = {
                    "gate": exc.gate_id,
                    "category": "WORKFLOW_GATE",
                    "retry_policy": (
                        "CHANGE_INPUT_THEN_PREFLIGHT"
                        if exc.gate_id != "G-REPEATED-FAILED-SUBMISSION"
                        else "IDENTICAL_RETRY_BLOCKED"
                    ),
                    "technical_input_owner": "PLATFORM_OR_ENGINEERING_AGENT",
                    "ask_business_user_for_internal_contract": False,
                }
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": error,
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"工作流服务内部错误（{type(exc).__name__}）：{exc}",
                },
            }


def main() -> None:
    server = McpServer()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = server.handle(message)
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
