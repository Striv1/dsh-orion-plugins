from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def requires_instance_exploration(project_dir: Path, release_version: str, state: dict) -> bool:
    """Use the frozen release contract, including releases from older workflows."""
    if state.get("business_modeling_contract_version") == "business-first-v1":
        return True
    quality_path = project_dir / "07-release" / f"ontology-engineering-package-{release_version}" / "03-质量结论/quality-summary.json"
    if not quality_path.exists():
        return False
    try:
        quality = json.loads(quality_path.read_text())
        return bool(quality.get("validated_graph_sha256")) or int(quality.get("semantica_instance_count") or 0) > 0
    except (OSError, ValueError, TypeError, AttributeError):
        # An unreadable contract cannot establish that this is a model-only release.
        return True


class PublishedOntologySemanticaSync:
    """Versioned index sync for immutable published ontology versions.

    Semantica is an explanatory runtime, not the source of truth.  The caller
    decides whether a failed sync is advisory or a release gate; this client
    always performs an exact registry read-back before reporting ``SYNCED``.
    """

    IMPORT_PROFILE = "zh-first-v1"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        enabled: bool | None = None,
        timeout_seconds: float = 4.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = str(
            base_url or os.getenv("SEMANTICA_API_URL") or "http://127.0.0.1:8001"
        ).rstrip("/")
        self.enabled = (
            enabled
            if enabled is not None
            else _enabled(os.getenv("SEMANTICA_AUTO_SYNC"))
        )
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def sync(
        self, *, project_dir: Path, project_id: str, project_name: str,
        release_version: str,
    ) -> dict[str, Any]:
        state_path = project_dir / "workflow-state.json"
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (OSError, ValueError) as exc:
            return {"status": "DEFERRED", "registry_verified": False,
                    "detail": f"工程版本合同无法读取：{exc}"}
        model = self._sync_model(project_dir=project_dir, project_id=project_id,
                                 project_name=project_name, release_version=release_version)
        if not requires_instance_exploration(project_dir, release_version, state):
            return model
        model["model_status"] = model.get("status")
        if model.get("status") != "SYNCED":
            return {**model, "exploration_status": "NOT_CONNECTED"}
        try:
            if state.get("business_modeling_contract_version") == "business-first-v1":
                exploration = self._sync_exploration(
                    project_dir=project_dir, project_id=project_id,
                    release_version=release_version, model=model,
                )
            else:
                from .release_instance_connection import connect
                index_root = Path(os.getenv("ORION_EXPLORATION_INDEX_ROOT") or
                                  Path(__file__).resolve().parents[2] / ".orion-runtime/instance-exploration")
                receipt = connect(project_dir, index_root, self.base_url,
                                  model_binding=model, transport=self.transport)
                exploration = {
                    "exploration_status": "VERIFIED",
                    "snapshot_sha256": "sha256:" + receipt["snapshot_sha256"],
                    "exploration_checks": receipt["readback_checks"],
                    "exploration_counts": receipt["counts"],
                    "data_mode": "S6_VALIDATION_SNAPSHOT",
                }
            return {**model, **exploration,
                    "detail": "模型已登记，版本化验收快照的类、实例和详情已回读核验。"}
        except (httpx.HTTPError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            return {**model, "status": "DEFERRED", "exploration_status": "BLOCKED",
                    "error_type": type(exc).__name__, "error_detail": str(exc),
                    "detail": "模型已登记，但实例探索未通过；保留同一版本，等待受管恢复。"}

    def _sync_exploration(
        self, *, project_dir: Path, project_id: str, release_version: str,
        model: dict[str, Any],
    ) -> dict[str, Any]:
        from services.ontology_engineering.instance_exploration import ensure_instance_index

        quality_dir = project_dir / "06-quality-validation"
        package_dir = project_dir / "07-release" / f"ontology-engineering-package-{release_version}"
        # The immutable release, not a later mutable S6 run, authorizes the
        # snapshot attached to this version. Never fall back to workspace reports.
        quality = json.loads((package_dir / "03-质量结论/quality-summary.json").read_text())
        if quality.get("status") != "PASSED":
            raise ValueError("发布包实例快照尚未通过 S6")
        expected = str(quality.get("validated_graph_sha256") or "").removeprefix("sha256:")
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("正式发布包缺少已验证实例快照指纹")
        model_path = package_dir / "01-本体模型/ontology.ttl"
        if not model_path.is_file():
            raise ValueError("新流程只能接入正式版本包中的模型")
        index_root = Path(os.getenv("ORION_EXPLORATION_INDEX_ROOT") or
                          Path(__file__).resolve().parents[2] / ".orion-runtime/instance-exploration")
        index = ensure_instance_index(
            model_path=model_path, snapshot_path=quality_dir / "materialized.ttl",
            index_dir=index_root, project_id=project_id, release_version=release_version,
            snapshot_sha256=expected,
            model_sha256=str(model["source_sha256"]).removeprefix("sha256:"),
            ontology_uri=model["ontology_uri"],
        )
        scope = {"project_id": project_id, "release_version": release_version,
                 "ontology_uri": model["ontology_uri"],
                 "model_sha256": str(model["source_sha256"]).removeprefix("sha256:"),
                 "snapshot_sha256": expected}
        with httpx.Client(base_url=self.base_url, timeout=max(30, self.timeout_seconds),
                          transport=self.transport, trust_env=False,
                          headers=({"X-API-Key": os.environ["SEMANTICA_API_KEY"]} if os.getenv("SEMANTICA_API_KEY") else {})) as client:
            registered = client.post("/api/orion/exploration/register",
                                     json={**scope, "index_path": str(index["index_path"])})
            registered.raise_for_status()
            stats_response = client.get("/api/orion/exploration/stats", params=scope)
            stats_response.raise_for_status()
            classes_response = client.get("/api/orion/exploration/classes", params=scope)
            classes_response.raise_for_status()
            stats = stats_response.json()
            if any(stats.get("scope", {}).get(key) != value for key, value in scope.items()):
                raise ValueError("实例回读范围与当前本体版本不一致")
            classes = classes_response.json()
            # Probe a nonempty class and its actual instance detail, not only health/counts.
            items = classes if isinstance(classes, list) else classes.get("classes", classes.get("items", []))
            probe = next((item for item in items if int(item.get("instance_count", item.get("count", 0))) > 0), None)
            checks = ["stats", "classes"]
            if probe:
                class_iri = probe.get("iri") or probe.get("class_iri")
                response = client.get("/api/orion/exploration/instances", params={**scope, "class_iri": class_iri, "limit": 1})
                response.raise_for_status()
                result = response.json()
                instances = result.get("items", result.get("instances", []))
                if not instances:
                    raise ValueError("类实例数量与实例列表不一致")
                iri = instances[0].get("iri") or instances[0].get("id")
                detail = client.get("/api/orion/exploration/node", params={**scope, "iri": iri})
                detail.raise_for_status()
                checks += ["instances", "node"]
        return {"exploration_status": "VERIFIED", "snapshot_sha256": f"sha256:{expected}",
                "exploration_scope": scope, "exploration_checks": checks,
                "exploration_stats": stats, "data_mode": "S6_VALIDATION_SNAPSHOT"}

    def _sync_model(
        self,
        *,
        project_dir: Path,
        project_id: str,
        project_name: str,
        release_version: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {
                "status": "DISABLED",
                "project_id": project_id,
                "release_version": release_version,
                "detail": "Semantica 自动同步未启用；正式发布包不受影响。",
            }

        package_ontology_path = (
            project_dir
            / "07-release"
            / f"ontology-engineering-package-{release_version}"
            / "01-本体模型"
            / "ontology.ttl"
        )
        workspace_ontology_path = project_dir / "05-ontology-build" / "ontology.ttl"
        ontology_path = (
            package_ontology_path
            if package_ontology_path.is_file()
            else workspace_ontology_path
        )
        if not ontology_path.is_file():
            return {
                "status": "DEFERRED",
                "project_id": project_id,
                "release_version": release_version,
                "detail": "正式 ontology.ttl 不存在，未同步到 Semantica。",
            }

        ontology_bytes = ontology_path.read_bytes()
        source_sha256 = hashlib.sha256(ontology_bytes).hexdigest()
        tags = [
            "orion",
            f"orion-project:{project_id}",
            f"orion-release:{release_version}",
            f"orion-source-sha256:{source_sha256}",
            f"orion-import-profile:{self.IMPORT_PROFILE}",
        ]
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
                trust_env=False,
                headers=({"X-API-Key": os.environ["SEMANTICA_API_KEY"]} if os.getenv("SEMANTICA_API_KEY") else {}),
            ) as client:
                health = client.get("/api/health")
                health.raise_for_status()
                registry_response = client.get("/api/ontology/registry")
                registry_response.raise_for_status()
                registry = registry_response.json()
                existing = next(
                    (
                        item
                        for item in registry
                        if set(tags[1:]).issubset(set(item.get("tags") or []))
                    ),
                    None,
                )
                if existing is not None:
                    return {
                        "status": "SYNCED",
                        "project_id": project_id,
                        "release_version": release_version,
                        "ontology_uri": existing.get("uri"),
                        "semantica_url": self.base_url,
                        "source_path": ontology_path.relative_to(project_dir).as_posix(),
                        "source_sha256": f"sha256:{source_sha256}",
                        "registry_verified": True,
                        "idempotent_replay": True,
                        "detail": "Semantica 已存在同一 ORION 发布版本。",
                    }

                loaded = client.post(
                    "/api/ontology/load",
                    json={
                        "content": ontology_bytes.decode("utf-8"),
                        "format": "turtle",
                        "name": project_name,
                        "description": (
                            f"ORION 正式本体 {project_id} · v{release_version}"
                        ),
                        "tags": tags,
                    },
                )
                loaded.raise_for_status()
                result = loaded.json()
                registry_response = client.get("/api/ontology/registry")
                registry_response.raise_for_status()
                read_back = next(
                    (
                        item
                        for item in registry_response.json()
                        if set(tags[1:]).issubset(set(item.get("tags") or []))
                    ),
                    None,
                )
                if read_back is None:
                    raise ValueError(
                        "Semantica registry read-back is missing the exact release tags"
                    )
                return {
                    "status": "SYNCED",
                    "project_id": project_id,
                    "release_version": release_version,
                    "ontology_uri": read_back.get("uri") or result.get("uri"),
                    "semantica_url": self.base_url,
                    "source_path": ontology_path.relative_to(project_dir).as_posix(),
                    "source_sha256": f"sha256:{source_sha256}",
                    "nodes_added": result.get("nodes_added", 0),
                    "edges_added": result.get("edges_added", 0),
                    "registry_verified": True,
                    "idempotent_replay": False,
                    "detail": "正式本体版本已同步到 Semantica 解释运行时。",
                }
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return {
                "status": "DEFERRED",
                "project_id": project_id,
                "release_version": release_version,
                "semantica_url": self.base_url,
                "error_type": type(exc).__name__,
                "error_detail": str(exc),
                "registry_verified": False,
                "detail": "Semantica 当前不可用；正式发布已保留，可稍后重试同步。",
            }
