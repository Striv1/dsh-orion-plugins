from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import SH

from scripts.managed_stage_bridge import (
    commit_managed,
    guarded_client,
    opted_in_tasks,
    start_managed,
)
from scripts.protege_role_router import ProtegeRoutingError, _read_secret, resolve_role
from services.ontology_engineering import OntologyWorkflowService
from services.ontology_engineering.joint_design import verify_joint_baseline
from services.ontology_engineering.ontology_types import (
    normalize_datatype_iri,
    validate_ontology_types,
)
from services.ontology_engineering.rdf_builder import build_rdf_artifacts
from services.ontology_engineering.stage_contracts import (
    STAGE_CONTRACT_VERSION,
    project_stage_contract_version,
)
from services.ontology_engineering.stage_execution import StageExecutionLease


class ProtegeTargetNotLoaded(RuntimeError):
    """The server rejected target selection before changing its active ontology."""


class ProtegeMcpClient:
    def __init__(self, url: str, secret_path: Path) -> None:
        self.url = url
        self.secret = _read_secret(secret_path)
        self.session: str | None = None
        self.request_id = 0

    def _request(
        self, method: str, params: dict[str, Any], *, notification: bool = False
    ) -> dict[str, Any]:
        self.request_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.request_id
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                self.session = response.headers.get("Mcp-Session-Id") or self.session
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if notification and exc.code in {202, 204}:
                return {}
            raise
        if notification or not raw:
            return {}
        if raw.startswith("id:"):
            raw = "".join(
                line[5:].lstrip() for line in raw.splitlines() if line.startswith("data:")
            )
        payload = json.loads(raw)
        if "error" in payload:
            raise RuntimeError(f"Protégé MCP {method} 失败：{payload['error']}")
        return payload["result"]

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "orion-workflow", "version": "0.2.0"},
            },
        )
        self._request("notifications/initialized", {}, notification=True)

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._request("tools/call", {"name": name, "arguments": arguments})
        except RuntimeError as error:
            if name == "set_active_ontology" and "No loaded ontology matches" in str(error):
                raise ProtegeTargetNotLoaded(str(error)) from error
            raise
        if result.get("isError"):
            if name == "set_active_ontology" and "No loaded ontology matches" in str(result.get("content")):
                raise ProtegeTargetNotLoaded(f"Protégé MCP {name} 失败：{result.get('content')}")
            raise RuntimeError(f"Protégé MCP {name} 失败：{result.get('content')}")
        return result.get("structuredContent") or result


class ConstructionLeaseTimeout(RuntimeError):
    """The shared construction window is still owned by another S5 run."""


def quarantine_loaded_build_target(
    client: ProtegeMcpClient,
    *,
    ontology_iri: str,
    version: str,
    run_id: str,
) -> dict[str, Any]:
    """Free an already-loaded ontology id before rebuilding the same version."""

    target_version_iri = f"{ontology_iri.rstrip('/')}/{version}"
    try:
        selected = client.tool(
            "set_active_ontology", {"ontology_iri": target_version_iri}
        )
    except RuntimeError as exc:
        if "No loaded ontology matches" in str(exc):
            return {
                "status": "NOT_LOADED",
                "target_version_iri": target_version_iri,
            }
        raise

    quarantine_iri = f"{ontology_iri.rstrip('/')}/.orion-stale/{run_id}"
    quarantine_version_iri = f"{quarantine_iri}/{version}"
    changed = client.tool(
        "set_ontology_id",
        {
            "ontology_iri": quarantine_iri,
            "version_iri": quarantine_version_iri,
        },
    )
    return {
        "status": "QUARANTINED",
        "target_version_iri": target_version_iri,
        "quarantine_iri": quarantine_iri,
        "quarantine_version_iri": quarantine_version_iri,
        "selected": selected,
        "changed": changed,
    }


def verify_exported_data_property_ranges(
    ttl_path: Path, design: dict[str, Any]
) -> dict[str, Any]:
    """Fail closed when Protégé exports ranges different from S4 design."""

    graph = Graph().parse(ttl_path, format="turtle")
    mismatches: list[dict[str, Any]] = []
    for item in design.get("data_properties") or []:
        property_iri = str(item["iri"])
        expected = normalize_datatype_iri(item["range"])
        actual = sorted(
            str(value) for value in graph.objects(URIRef(property_iri), RDFS.range)
        )
        if actual != [expected]:
            mismatches.append(
                {
                    "property_iri": property_iri,
                    "expected": expected,
                    "actual": actual,
                }
            )
    if mismatches:
        raise RuntimeError(
            "Protégé 导出的数据属性 range 与 S4 施工图不一致："
            + json.dumps(mismatches, ensure_ascii=False)
        )
    type_issues = validate_ontology_types(graph, design)
    if type_issues:
        raise RuntimeError("Protégé 导出本体类型校验失败：" + json.dumps(type_issues, ensure_ascii=False))
    return {
        "status": "PASSED",
        "checked_data_properties": len(design.get("data_properties") or []),
        "mismatches": [],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ontology_annotation_payloads(design: dict[str, Any]) -> list[dict[str, str]]:
    """Build the real Protégé ontology-header annotations required by S5."""

    title = str(design.get("title_zh") or "ORION 企业本体").strip()
    version = str(design.get("version") or "").strip()
    class_count = len(design.get("classes") or [])
    object_property_count = len(design.get("object_properties") or [])
    data_property_count = len(design.get("data_properties") or [])
    source_comment = str(design.get("comment_zh") or "").strip()
    detail = (
        f"本版本包含 {class_count} 个业务类、{object_property_count} 个对象属性和 "
        f"{data_property_count} 个数据属性。对象属性用于连接业务实体，数据属性用于描述实体字段；"
        "类层级、等价类、互斥类、属性特征和约束由 Protégé 中选定的推理器对整套本体统一检查，"
        "而不是按类或属性分别推理。推理结论只反映本体公理和已载入实例，不替代业务系统实时事实。"
    )
    comment = f"{source_comment.rstrip('。')}。{detail}" if source_comment else detail
    description = (
        f"{title}是由 ORION S0-S4 正式证据、Mapping 与本体施工图确定性构建的可推理语义模型；"
        "用于统一业务术语、关系、字段和约束，并支持查询、校验、知识图谱集成与后续推理。"
    )
    payloads = [
        {"property": "rdfs:label", "value": title, "lang": "zh"},
        {"property": "rdfs:comment", "value": comment, "lang": "zh"},
        {
            "property": "http://purl.org/dc/terms/title",
            "value": title,
            "lang": "zh",
        },
        {
            "property": "http://purl.org/dc/terms/description",
            "value": description,
            "lang": "zh",
        },
        {
            "property": "http://purl.org/dc/terms/source",
            "value": "ORION S0-S4 正式证据、mapping.yaml 与 ontology-design.yaml",
            "lang": "zh",
        },
        {
            "property": "http://purl.org/dc/terms/creator",
            "value": "ORION 本体工程工作台",
            "lang": "zh",
        },
    ]
    if version:
        payloads.append(
            {
                "property": "http://www.w3.org/2002/07/owl#versionInfo",
                "value": version,
            }
        )
    return payloads


def ontology_annotation_summary(
    context: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    items = context.get("ontology_annotations") or []
    comments = [
        str(item.get("value") or "")
        for item in items
        if str(item.get("property_iri") or "")
        == "http://www.w3.org/2000/01/rdf-schema#comment"
        and str(item.get("lang") or "").lower().startswith("zh")
    ]
    return {
        "count": int(summary.get("ontology_annotations") or len(items)),
        "comment_zh": comments[0] if comments else "",
        "items": items,
    }


def normalize_validation_reports(
    results: dict[str, Any], shapes_ttl: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize real Protégé receipts into the S5 gate contract.

    The MCP returns reasoner details across ``run_reasoner``,
    ``get_unsatisfiable_classes`` and ``validate_ontology``. S5 consumes one
    stable top-level summary, so only issue a passing summary when every
    underlying receipt agrees.
    """

    run = results.get("run_reasoner") or {}
    unsatisfiable = results.get("unsatisfiable") or {}
    validation = (results.get("validate_ontology") or {}).get("reasoner") or {}
    reasoner = str(run.get("reasoner") or "")
    reasoner_ok = all(
        (
            run.get("started") is True,
            run.get("completed") is True,
            run.get("classification_failed") is False,
            run.get("inconsistent") is False,
            int(run.get("unsatisfiable_count", -1)) == 0,
            "HERMIT" in reasoner.upper(),
            unsatisfiable.get("coherent") is True,
            int(unsatisfiable.get("count", -1)) == 0,
            validation.get("results_available") is True,
            validation.get("consistent") is True,
            int(validation.get("unsatisfiable_count", -1)) == 0,
        )
    )
    if not reasoner_ok:
        raise RuntimeError("Protégé HermiT 回执未共同证明本体一致且无不可满足类。")

    shapes_graph = Graph()
    shapes_graph.parse(data=shapes_ttl, format="turtle")
    node_shape_count = len(set(shapes_graph.subjects(RDF.type, SH.NodeShape)))
    raw_shacl = results.get("shacl_schema") or {}
    if (
        raw_shacl.get("conforms") is not True
        or int(raw_shacl.get("violations", -1)) != 0
        or node_shape_count < 1
    ):
        raise RuntimeError("Protégé SHACL 回执未证明约束通过，或 shapes.ttl 不含 NodeShape。")

    reasoner_report = {
        "status": "CONSISTENT",
        "consistent": True,
        "inconsistent": False,
        "reasoner": reasoner,
        "unsatisfiable_count": 0,
        "run_status": run.get("status"),
        "validation_status": validation.get("status"),
    }
    shacl_report = {
        **raw_shacl,
        "node_shape_count": node_shape_count,
    }
    return reasoner_report, shacl_report


def _read_lease_metadata(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_lease_metadata(handle: Any, payload: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


def _lease_owner_summary(payload: dict[str, Any]) -> str:
    if not payload:
        return "当前持有者信息不可用"
    return (
        f"owner={payload.get('owner') or 'unknown'}，"
        f"project={payload.get('project_id') or 'unknown'}，"
        f"job={payload.get('job_id') or 'unknown'}，"
        f"pid={payload.get('pid') or 'unknown'}"
    )


@contextmanager
def construction_lease(
    path: Path,
    *,
    owner: str,
    project_id: str,
    job_id: str,
    timeout_seconds: float,
    poll_seconds: float = 0.2,
) -> Iterator[dict[str, Any]]:
    """Serialize all mutations of the one construction Protégé window.

    The lock is an OS-level advisory lock, so a crashed process releases it
    automatically.  The JSON body is audit metadata only; it is intentionally
    kept after release so an operator can identify the last owner.
    """

    if timeout_seconds < 0:
        raise ValueError("施工租约等待时间不能小于 0 秒")
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    active = _read_lease_metadata(path)
                    raise ConstructionLeaseTimeout(
                        f"Protégé construction 施工窗口在 {timeout_seconds:g} 秒内仍被占用："
                        f"{_lease_owner_summary(active)}。本次 S5 未执行任何写入。"
                    ) from exc
                time.sleep(min(max(poll_seconds, 0.01), remaining))

        lease = {
            "schema_version": 1,
            "status": "ACTIVE",
            "lock_path": str(path),
            "owner": owner,
            "project_id": project_id,
            "job_id": job_id,
            "pid": os.getpid(),
            "acquired_at": _now(),
        }
        _write_lease_metadata(handle, lease)
        try:
            yield lease
        except BaseException as exc:
            lease.update(
                {
                    "status": "RELEASED_AFTER_ERROR",
                    "released_at": _now(),
                    "error_type": type(exc).__name__,
                }
            )
            _write_lease_metadata(handle, lease)
            raise
        else:
            lease.update({"status": "RELEASED", "released_at": _now()})
            _write_lease_metadata(handle, lease)
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def verify_s5_joint_design(project_dir: Path) -> str | None:
    """Reject changed approved inputs before the construction window is touched."""
    state = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    if project_stage_contract_version(state) != STAGE_CONTRACT_VERSION:
        return None
    if state.get("current_stage") != "S5" or (state.get("stage_statuses") or {}).get("S5") not in {
        "RUNNING", "FAILED",
    }:
        raise RuntimeError("S5 当前不可施工；请先通过联合设计评审或正式恢复 S5。")
    return str(verify_joint_baseline(project_dir)["joint_design_fingerprint"])


def _run_protege_build(execution_state) -> None:
    parser = argparse.ArgumentParser(description="执行 ORION S5 Protégé MCP 确定性构建。")
    parser.add_argument("project_id")
    parser.add_argument("--workflow-home", default=".orion-workflows")
    parser.add_argument("--mcp-url", help="显式覆盖角色路由，仅用于受控诊断。")
    parser.add_argument("--secret", default="~/.protege-mcp/secret")
    parser.add_argument("--routing-file", default=".orion-mcp-control/protege-routing.json")
    parser.add_argument("--role", default="construction")
    parser.add_argument("--expected-bundle-id", default="edu.stanford.protege.zh.full")
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="仅把真实 MCP 结果与构建产物写成 3081 action 参数，不直接推进工程。",
    )
    parser.add_argument(
        "--lease-file",
        type=Path,
        help="construction Protégé 全局施工租约文件；默认位于角色路由文件旁。",
    )
    parser.add_argument(
        "--lease-owner",
        default="orion-workflow",
        help="写入施工租约的调用方标识。",
    )
    parser.add_argument(
        "--lease-timeout-seconds",
        type=float,
        default=300,
        help="等待其他 S5 施工完成的最长时间，默认 300 秒。",
    )
    args = parser.parse_args()

    workflow_home = Path(args.workflow_home).resolve()
    project_dir = workflow_home / args.project_id
    joint_design_fingerprint = verify_s5_joint_design(project_dir)
    tasks = opted_in_tasks(workflow_home, args.project_id)
    managed = None
    stage_lease = None
    if tasks is not None:
        fingerprint = hashlib.sha256((str(joint_design_fingerprint) +
            (project_dir / "04-ontology-design/ontology-design.yaml").read_text() +
            Path(__file__).read_text()).encode()).hexdigest()
        stage_lease = StageExecutionLease.acquire(project_dir=project_dir, stage="S5",
            input_fingerprint=fingerprint, executor_id=f"orion-platform:{os.getpid()}")
        execution_state["lease"] = stage_lease
        stage_lease.start_heartbeat()
        managed = start_managed(tasks, project=args.project_id, stage="S5", fingerprint=fingerprint,
                                lease=stage_lease, project_dir=project_dir)
        execution_state["managed"] = managed
    service = OntologyWorkflowService(workflow_home)

    def work():
        design = yaml.safe_load(
            (project_dir / "04-ontology-design/ontology-design.yaml").read_text(encoding="utf-8")
        )
        artifacts = build_rdf_artifacts(design)
        temporary = Path(tempfile.mkdtemp(prefix="orion-protege-"))
        source = temporary / "ontology-input.ttl"
        shapes = temporary / "shapes.ttl"
        ttl_output = temporary / "ontology.ttl"
        owl_output = temporary / "ontology.owl"
        source.write_text(str(artifacts["ontology_ttl"]), encoding="utf-8")
        shapes.write_text(str(artifacts["shapes_ttl"]), encoding="utf-8")

        secret_path = Path(args.secret).expanduser()
        route: dict[str, Any] = {}
        if args.mcp_url:
            mcp_url = args.mcp_url
        else:
            secret = _read_secret(secret_path)
            try:
                route = resolve_role(
                    role=args.role,
                    broker="http://127.0.0.1:8123",
                    secret=secret,
                    routing_file=Path(args.routing_file),
                )
            except ProtegeRoutingError as exc:
                raise SystemExit(f"S5 拒绝使用默认 Protégé 窗口：{exc}") from exc
            if route.get("bundle_id") != args.expected_bundle_id:
                raise SystemExit(
                    "S5 构建窗口应用身份不匹配："
                    f"期望 {args.expected_bundle_id}，实际 {route.get('bundle_id')}"
                )
            if route.get("role") != args.role:
                raise SystemExit(f"S5 构建窗口角色不匹配：期望 {args.role}，实际 {route.get('role')}")
            mcp_url = str(route["mcp_url"])

        run_id = f"protege-{uuid.uuid4().hex[:12]}"
        lease_path = args.lease_file or (
            Path(args.routing_file).expanduser().resolve().parent / "protege-construction.lock"
        )
        client = guarded_client(managed, ProtegeMcpClient(mcp_url, secret_path),
                                known_rejections=(ProtegeTargetNotLoaded,))
        with construction_lease(
            lease_path,
            owner=str(args.lease_owner),
            project_id=args.project_id,
            job_id=run_id,
            timeout_seconds=args.lease_timeout_seconds,
        ) as lease:
            if joint_design_fingerprint and verify_s5_joint_design(project_dir) != joint_design_fingerprint:
                raise RuntimeError("S5 等待施工租约期间联合设计已改变；本次未执行 Protégé 写入。")
            client.initialize()
            active_before = client.tool("get_active_ontology", {})
            results: dict[str, Any] = {}
            results["stale_target_quarantine"] = quarantine_loaded_build_target(
                client,
                ontology_iri=str(design["ontology_iri"]),
                version=str(design["version"]),
                run_id=run_id,
            )
            results["load_ontology"] = client.tool(
                "load_ontology",
                {
                    "source": str(source),
                    "network": "deny",
                    "missing_imports": "error",
                },
            )
            if (
                int(results["load_ontology"].get("already_loaded", -1)) != 0
                or int(results["load_ontology"].get("added_ontologies", -1)) != 1
            ):
                raise RuntimeError(
                    "S5 拒绝复用 Protégé 窗口内的旧模型："
                    f"{results['load_ontology']}"
                )
            results["set_ontology_id"] = client.tool(
                "set_ontology_id",
                {
                    "ontology_iri": design["ontology_iri"],
                    "version_iri": f"{design['ontology_iri']}/{design['version']}",
                },
            )
            results["add_ontology_annotations"] = [
                client.tool("add_ontology_annotation", payload)
                for payload in ontology_annotation_payloads(design)
            ]
            results["ontology_context"] = client.tool("get_ontology_context", {"limit": 10})
            results["ontology_summary"] = client.tool(
                "summarize_ontology", {"include_imports": False, "limit": 80}
            )
            results["set_reasoner"] = client.tool("set_reasoner", {"reasoner": "HermiT"})
            results["run_reasoner"] = client.tool("run_reasoner", {"timeout_ms": 120000})
            results["unsatisfiable"] = client.tool("get_unsatisfiable_classes", {})
            results["validate_ontology"] = client.tool(
                "validate_ontology",
                {"with_reasoner": True, "limit": 100, "timeout_ms": 120000},
            )
            results["save_ttl"] = client.tool(
                "save_ontology",
                {
                    "path": str(ttl_output),
                    "verify_round_trip": True,
                    "atomic": True,
                    "on_lossy": "fail",
                },
            )
            results["save_owl"] = client.tool(
                "save_ontology",
                {
                    "path": str(owl_output),
                    "verify_round_trip": True,
                    "atomic": True,
                    "on_lossy": "fail",
                },
            )
            results["exported_data_property_ranges"] = (
                verify_exported_data_property_ranges(ttl_output, design)
            )
            owl_type_issues = validate_ontology_types(Graph().parse(owl_output, format="xml"), design)
            if owl_type_issues:
                raise RuntimeError("Protégé OWL 导出本体类型校验失败：" + json.dumps(owl_type_issues, ensure_ascii=False))
            results["exported_ontology_types"] = {"status": "PASSED", "formats": ["TTL", "OWL"], "issues": []}
            results["shacl_schema"] = client.tool(
                "shacl_validate",
                {
                    "shapes_path": str(shapes),
                    "include_inferred": True,
                    "limit": 100,
                    "timeout_ms": 120000,
                },
            )
            active_after = client.tool("get_active_ontology", {})
            model_revision = client.tool("get_model_revision", {})
        reasoner_report, shacl_report = normalize_validation_reports(
            results, str(artifacts["shapes_ttl"])
        )
        report = {
            "builder": "PROTEGE_MCP",
            "status": "PASSED",
            "run_id": run_id,
            "application": route.get("application_name", "显式 MCP URL"),
            "application_path": route.get("application"),
            "application_bundle_id": route.get("bundle_id"),
            "application_version": route.get("application_version"),
            "application_role": route.get("role", args.role),
            "mcp_window_id": route.get("instance_id"),
            "mcp_endpoint": mcp_url,
            "construction_lease": lease,
            "joint_design_fingerprint": joint_design_fingerprint,
            "active_ontology_before": active_before,
            "active_ontology_after": active_after,
            "model_revision": model_revision,
            "reasoner_report": reasoner_report,
            "shacl_report": shacl_report,
            "ontology_annotation_summary": ontology_annotation_summary(
                results["ontology_context"], results["ontology_summary"]
            ),
            "exported_formats": ["OWL", "TTL", "SHACL"],
            "tool_calls": [
                "load_ontology",
                "set_ontology_id",
                "add_ontology_annotation",
                "get_ontology_context",
                "summarize_ontology",
                "set_reasoner",
                "run_reasoner",
                "get_unsatisfiable_classes",
                "validate_ontology",
                "save_ontology:TTL",
                "save_ontology:OWL",
                "shacl_validate:SHACL",
            ],
            "tool_results": results,
            "source_design": "04-ontology-design/ontology-design.yaml",
        }
        action_arguments = {
            "project_id": args.project_id,
            "ontology_owl": owl_output.read_text(encoding="utf-8"),
            "ontology_ttl": ttl_output.read_text(encoding="utf-8"),
            "shapes_ttl": shapes.read_text(encoding="utf-8"),
            "protege_build_report": report,
        }
        if args.evidence_output:
            args.evidence_output.write_text(
                json.dumps(action_arguments, ensure_ascii=False),
                encoding="utf-8",
            )
            status = service.get_status(args.project_id)
        elif managed is None:
            status = service.record_ontology_build(**action_arguments)
        else:
            revision = int(service.get_status(args.project_id)["revision"])
            preflight = service.preflight_stage_submission(project_id=args.project_id, stage="S5",
                payload={key: value for key, value in action_arguments.items() if key != "project_id"})
            if preflight.get("status") != "PASSED":
                raise SystemExit(json.dumps(preflight, ensure_ascii=False))
            status = commit_managed(managed, lambda: service.commit_preflight_stage_submission(
                project_id=args.project_id, stage="S5", preflight_token=preflight["preflight_token"],
                expected_revision=revision), preflight=preflight, expected_revision=revision)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "ontology_triples": artifacts["ontology_triple_count"],
                    "shape_triples": artifacts["shape_triple_count"],
                    "evidence_output": str(args.evidence_output) if args.evidence_output else None,
                    "current_stage": status["current_stage"],
                    "stage_statuses": status["stage_statuses"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    if managed is None:
        work()
    else:
        with service.managed_execution(tasks, managed.token):
            managed.effect(work)
        stage_lease.complete({"evidence_only": bool(args.evidence_output),
                              "managed_receipt": managed.receipt_reference if not args.evidence_output else None})


def main() -> None:
    execution_state = {}
    previous = signal.getsignal(signal.SIGTERM)
    def interrupt(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        _run_protege_build(execution_state)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        lease = execution_state.get("lease")
        if lease is not None and not lease._finished:
            lease.fail(type(error).__name__, {"stage": "S5"})
        raise
    finally:
        managed = execution_state.get("managed")
        try:
            if managed is not None:
                managed.__exit__(None, None, None)
        finally:
            lease = execution_state.get("lease")
            if lease is not None and not lease._finished:
                lease.mark_interrupted()
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
