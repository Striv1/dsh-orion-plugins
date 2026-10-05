"""Managed S7 runtime retry: re-run deployment for an already approved release.

The immutable package and the original approval are reused. This action never
re-approves, regenerates the package, or reopens S6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.ontology_contracts.errors import WorkflowError, WorkflowGateError

if TYPE_CHECKING:  # pragma: no cover
    from .workflow import OntologyWorkflowService

RUNTIME_RETRYABLE_STATUSES = frozenset({"PACKAGE_READY_RUNTIME_BLOCKED", "RUNTIME_FAILED"})


def retry_release_runtime_deployment(
    service: OntologyWorkflowService,
    *,
    project_id: str,
    requested_by: str | None = None,
) -> dict[str, Any]:
    with service._project_mutation_lock(project_id) as project_dir:
        state = service._read_state(project_dir)
        status = str(state.get("project_status") or "")
        if status not in RUNTIME_RETRYABLE_STATUSES:
            raise WorkflowGateError(
                "G-S7-RUNTIME-RETRY-STATE",
                f"当前状态 {status or '未知'} 不需要重试运行时部署；"
                "只有“发布包就绪、运行时待恢复”的工程可以重试。",
            )
        publication_path = project_dir / "07-release/publication.json"
        if not publication_path.exists():
            raise WorkflowGateError("G-S7-RUNTIME-RETRY-EVIDENCE", "缺少原发布记录 publication.json，不能重试运行时。")
        publication = service._read_json(publication_path)
        release_version = str(publication.get("release_version") or "").strip()
        if publication.get("approval_decision") != "APPROVED" or not release_version:
            raise WorkflowGateError("G-S7-RUNTIME-RETRY-EVIDENCE", "原发布记录没有有效批准，不能重试运行时。")
        if (project_dir / "07-release/release-revocation.json").exists():
            raise WorkflowError(f"版本 {release_version} 已撤回，不能再部署运行时。")
        receipts = [state.get("release_runtime_status") or {}]
        deployment_path = project_dir / "07-release/realtime-deployment-status.json"
        if deployment_path.is_file():
            receipts.append(service._read_json(deployment_path))
        automation = getattr(service, "_release_deployment_automation", None)
        if automation is not None:
            receipts.append(automation.status(project_dir=project_dir, release_version=release_version))
        if any(
            receipt.get("failure_category") == "RELEASE_CONTRACT_INVALID"
            and receipt.get("release_version", release_version) == release_version
            for receipt in receipts
        ):
            raise WorkflowGateError(
                "G-S7-RELEASE-CONTRACT",
                "冻结发布包的事实合同无效，禁止原样重试。请先 preview_stage_rollback"
                "(target_stage=S3)，再按已有授权通过 create_revision_from_release"
                " 创建独立修订；保留原包并重新完成适用审批与验收。",
            )
        previous_error = (state.get("last_error") or {}).get("message")
        service._append_event(
            project_dir,
            "REALTIME_DEPLOYMENT_RETRY_REQUESTED",
            state,
            {
                "release_version": release_version,
                "requested_by": (requested_by or "").strip() or None,
                "previous_status": status,
                "previous_error": previous_error,
            },
        )
        deployment = service._enqueue_realtime_deployment(project_dir, release_version)
        service._record_realtime_deployment_status(project_dir, release_version, deployment)
        return {
            **service._status_payload(project_dir, service._read_state(project_dir)),
            "release_version": release_version,
            "realtime_deployment": deployment,
            "reused_approval": True,
        }
