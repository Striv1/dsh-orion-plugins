from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from rdflib import Graph, URIRef

from services.ontop_client.client import OntopClient
from services.realtime_qa.deployment import (
    OntopDeploymentContractError,
    verify_ontop_deployment_binding,
)
from services.realtime_qa.models import OntologyReleaseBinding, OntopDeploymentIdentity
from services.realtime_qa.query_capabilities import (
    QueryCapabilityError,
    normalize_document_query_capabilities,
    normalize_query_capabilities,
)
from services.realtime_qa.reasoning_contract import (
    ReasoningCapabilityError,
    normalize_document_fact_queries,
    normalize_reasoning_capabilities,
    normalize_rule_package,
    validate_ontology_term_binding,
)
from services.realtime_qa.source_provenance import (
    INVENTORY_TRACE,
    SCHEMA_TRACE,
    SOURCE_TRACE,
    protected_source_trace,
)
from services.structured_data.release_contract import (
    MultiSourceReleaseContractError,
    validate_multi_source_release_contract,
)


class ReleaseBindingError(ValueError):
    pass


class OntopRuntimeVerificationError(RuntimeError):
    pass


class ReleaseBindingLoader:
    """Load runtime files only when the approved S7 package protects them."""

    RUNTIME_CONTRACT = "05-运行时/realtime-runtime.json"

    def validate_publication(
        self, project_dir: Path, *, binding: OntologyReleaseBinding | None = None,
        allow_pre_runtime: bool = False,
    ) -> dict[str, Any]:
        """Recheck mutable authority without loading or silently replacing the package."""
        release_dir = project_dir / "07-release"
        gates = self._read_json(release_dir / "gate-results.json")
        publication = self._read_json(release_dir / "publication.json")

        if gates.get("stage") != "S7":
            raise ReleaseBindingError("S7 release gate has not passed")
        if gates.get("status") != "PASSED" and not (
            allow_pre_runtime and self._pre_runtime_gates_passed(gates)
        ):
            raise ReleaseBindingError("S7 release gate has not passed")
        if publication.get("approval_decision") != "APPROVED":
            raise ReleaseBindingError("S7 publication has not been approved")
        revocation_path = release_dir / "release-revocation.json"
        if revocation_path.exists():
            revocation = self._read_json(revocation_path)
            if revocation.get("status") == "REVOKED":
                raise ReleaseBindingError("S7 release has been revoked")
        if publication.get("integrity_verification_status") != "PASSED":
            raise ReleaseBindingError("release integrity is not verified")

        if binding is not None and (
            publication.get("project_id") != binding.project_id
            or publication.get("release_version") != binding.release_version
            or publication.get("package_manifest_sha256") != binding.release_fingerprint
        ):
            raise ReleaseBindingError("cached runtime no longer matches the published release")
        return publication

    def load(
        self,
        project_dir: Path,
        *,
        allow_pre_runtime: bool = False,
    ) -> OntologyReleaseBinding:
        publication = self.validate_publication(project_dir, allow_pre_runtime=allow_pre_runtime)

        package_relative = str(publication.get("package_path") or "")
        package_dir = self._safe_relative(project_dir, package_relative)
        manifest_path = package_dir / "manifest.json"
        expected_manifest_checksum = str(publication.get("package_manifest_sha256") or "")
        if self._checksum(manifest_path) != expected_manifest_checksum:
            raise ReleaseBindingError("package manifest checksum mismatch")
        manifest = self._read_json(manifest_path)
        if (
            manifest.get("project_id") != publication.get("project_id")
            or manifest.get("release_version") != publication.get("release_version")
            or manifest.get("approval_decision") != "APPROVED"
        ):
            raise ReleaseBindingError("package manifest does not match publication")
        checksums = self._verify_manifest(package_dir, manifest)

        snapshot_artifact = "04-发布信息/release-snapshot.json"
        expected_snapshot_checksum = str(publication.get("release_snapshot_sha256") or "")
        if checksums.get(snapshot_artifact) != expected_snapshot_checksum:
            raise ReleaseBindingError("release snapshot is not protected by the package")
        if self.RUNTIME_CONTRACT not in checksums:
            raise ReleaseBindingError("runtime contract is not protected by the package")
        runtime = self._read_json(package_dir / self.RUNTIME_CONTRACT)
        contract_migrations: list[str] = []

        if runtime.get("project_id") != publication.get("project_id"):
            raise ReleaseBindingError("runtime project_id does not match publication")
        if runtime.get("release_version") != publication.get("release_version"):
            raise ReleaseBindingError("runtime release_version does not match publication")

        ontology_artifact = str(runtime.get("ontology_artifact") or "")
        database_access_mode = str(runtime.get("database_access_mode") or "")
        if database_access_mode != "READ_ONLY":
            raise ReleaseBindingError("runtime database access is not READ_ONLY")
        structured_query_enabled = runtime.get("structured_query_enabled") is not False
        runtime_mode = str(
            runtime.get("runtime_mode")
            or ("HYBRID" if structured_query_enabled else "DOCUMENT_ONLY")
        )
        try:
            document_query_capabilities = normalize_document_query_capabilities(
                runtime.get("document_query_capabilities"),
                legacy_default=True,
            )
        except QueryCapabilityError as exc:
            raise ReleaseBindingError(str(exc)) from exc
        mapping_artifact: str | None = None
        identity_query_artifact: str | None = None
        source_mapping_sha256: str | None = None
        deployment_id: str | None = None
        query_artifacts: dict[str, str] = {}
        query_capabilities: dict[str, dict[str, Any]] = {}
        reasoning_capabilities: dict[str, dict[str, Any]] = {}
        reasoning_rule_packages: dict[str, dict[str, Any]] = {}
        document_fact_queries: dict[str, dict[str, Any]] = {}
        protected_artifacts = {ontology_artifact}
        if structured_query_enabled:
            mapping_artifact = str(runtime.get("mapping_artifact") or "")
            identity_query_artifact = str(runtime.get("identity_query_artifact") or "")
            source_mapping_sha256 = str(runtime.get("source_mapping_sha256") or "")
            deployment_id = str(runtime.get("ontop_deployment_id") or "")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", source_mapping_sha256):
                raise ReleaseBindingError("runtime source mapping checksum is invalid")
            queries = runtime.get("ontop_queries")
            if not isinstance(queries, dict) or not queries:
                raise ReleaseBindingError("runtime Ontop query artifacts are missing")
            query_artifacts = {str(name): str(path) for name, path in queries.items()}
            try:
                query_capabilities = normalize_query_capabilities(
                    runtime.get("query_capabilities"),
                    set(query_artifacts),
                    allow_legacy=int(runtime.get("schema_version") or 1) < 2,
                )
                document_fact_queries = normalize_document_fact_queries(
                    runtime.get("document_fact_queries"),
                    require_explicit=False,
                )
                migrated_reasoning = self._migrate_legacy_reasoning_capabilities(
                    runtime,
                    package_dir,
                    contract_migrations,
                )
                reasoning_capabilities = normalize_reasoning_capabilities(
                    migrated_reasoning,
                    set(query_artifacts),
                    require_rules=False,
                    document_fact_query_names=set(document_fact_queries),
                )
            except (QueryCapabilityError, ReasoningCapabilityError) as exc:
                raise ReleaseBindingError(str(exc)) from exc
            protected_artifacts.update(
                {
                    mapping_artifact,
                    identity_query_artifact,
                    *query_artifacts.values(),
                }
            )
            for name, capability in reasoning_capabilities.items():
                artifact = str(capability["rule_artifact"])
                if checksums.get(artifact) != capability["rule_sha256"]:
                    raise ReleaseBindingError(
                        f"reasoning rule artifact is not protected by the package: {name}"
                    )
                try:
                    reasoning_rule_packages[name] = normalize_rule_package(
                        self._read_json(package_dir / artifact),
                        capability_name=name,
                    )
                except ReasoningCapabilityError as exc:
                    raise ReleaseBindingError(str(exc)) from exc
                validate_ontology_term_binding(
                    name,
                    capability,
                    reasoning_rule_packages[name]["rules"],
                )
                protected_artifacts.add(artifact)
            for name, query in list(document_fact_queries.items()):
                artifact = str(query.get("fact_artifact") or "")
                expected = str(query.get("fact_sha256") or "")
                if not artifact or checksums.get(artifact) != expected:
                    raise ReleaseBindingError(
                        f"document fact artifact is not protected by the package: {artifact}"
                    )
                fact_package = self._read_json(package_dir / artifact)
                hydrated_input = {
                    **query,
                    "facts": fact_package.get("facts"),
                }
                hydrated_input.pop("fact_artifact", None)
                hydrated_input.pop("fact_sha256", None)
                try:
                    hydrated = normalize_document_fact_queries(
                        {name: hydrated_input},
                        require_explicit=False,
                    )[name]
                except ReasoningCapabilityError as exc:
                    raise ReleaseBindingError(str(exc)) from exc
                document_fact_queries[name] = {
                    **hydrated,
                    "fact_artifact": artifact,
                    "fact_sha256": expected,
                }
                protected_artifacts.add(artifact)
        elif runtime_mode != "DOCUMENT_ONLY" or not document_query_capabilities:
            raise ReleaseBindingError("document-only runtime contract is incomplete")
        else:
            # DOCUMENT_ONLY runtime: load reasoning capabilities sourced from the
            # materialized document fact layer (never from an Ontop query).
            try:
                migrated_reasoning = self._migrate_legacy_reasoning_capabilities(
                    runtime,
                    package_dir,
                    contract_migrations,
                )
                reasoning_capabilities = normalize_reasoning_capabilities(
                    migrated_reasoning,
                    set(),
                    require_rules=False,
                    document_fact_query_names=set(
                        runtime.get("document_fact_queries") or {}
                    ),
                )
                document_fact_queries = normalize_document_fact_queries(
                    runtime.get("document_fact_queries"),
                    require_explicit=False,
                )
            except (QueryCapabilityError, ReasoningCapabilityError) as exc:
                raise ReleaseBindingError(str(exc)) from exc
            for name, capability in reasoning_capabilities.items():
                if capability["evidence_query"] not in document_fact_queries:
                    raise ReleaseBindingError(
                        f"document-only reasoning capability must reference a "
                        f"document_fact_query: {name}"
                    )
                artifact = str(capability["rule_artifact"])
                if checksums.get(artifact) != capability["rule_sha256"]:
                    raise ReleaseBindingError(
                        f"reasoning rule artifact is not protected by the "
                        f"package: {name}"
                    )
                try:
                    reasoning_rule_packages[name] = normalize_rule_package(
                        self._read_json(package_dir / artifact),
                        capability_name=name,
                    )
                except ReasoningCapabilityError as exc:
                    raise ReleaseBindingError(str(exc)) from exc
                validate_ontology_term_binding(
                    name,
                    capability,
                    reasoning_rule_packages[name]["rules"],
                )
                protected_artifacts.add(artifact)
            document_fact_artifacts = {
                str(query.get("fact_artifact") or ""): str(query.get("fact_sha256") or "")
                for query in document_fact_queries.values()
            }
            for artifact, expected in document_fact_artifacts.items():
                if not artifact or checksums.get(artifact) != expected:
                    raise ReleaseBindingError(
                        f"document fact artifact is not protected by the package: "
                        f"{artifact}"
                    )
                protected_artifacts.add(artifact)
            for name, query in list(document_fact_queries.items()):
                artifact = str(query.get("fact_artifact") or "")
                fact_package = self._read_json(package_dir / artifact)
                hydrated_input = {
                    **query,
                    "facts": fact_package.get("facts"),
                }
                hydrated_input.pop("fact_artifact", None)
                hydrated_input.pop("fact_sha256", None)
                try:
                    hydrated = normalize_document_fact_queries(
                        {name: hydrated_input},
                        require_explicit=False,
                    )[name]
                except ReasoningCapabilityError as exc:
                    raise ReleaseBindingError(str(exc)) from exc
                document_fact_queries[name] = {
                    **hydrated,
                    "fact_artifact": artifact,
                    "fact_sha256": str(query.get("fact_sha256") or ""),
                }
        if int(runtime.get("schema_version") or 1) >= 4:
            reasoning_requirement = str(
                runtime.get("reasoning_requirement") or ""
            ).strip().upper()
            if reasoning_requirement not in {"REQUIRED", "NOT_APPLICABLE"}:
                raise ReleaseBindingError(
                    "runtime reasoning requirement is missing or invalid"
                )
            if reasoning_requirement == "REQUIRED" and not reasoning_capabilities:
                raise ReleaseBindingError(
                    "runtime requires reasoning but no capability is packaged"
                )
            if reasoning_requirement == "NOT_APPLICABLE":
                rationale = str(
                    runtime.get("reasoning_not_applicable_reason") or ""
                ).strip()
                if reasoning_capabilities or len(rationale) < 12:
                    raise ReleaseBindingError(
                        "runtime reasoning not-applicable decision is incomplete"
                    )
        if int(runtime.get("schema_version") or 1) >= 5:
            try:
                parsed_snapshot, *_ = validate_multi_source_release_contract(
                    project_id=str(publication.get("project_id") or ""),
                    snapshot_set_payload=runtime.get("cross_source_snapshot_set"),
                    source_bindings_payload=runtime.get("source_bindings"),
                    snapshot_manifests_payload=runtime.get("snapshot_manifests"),
                    identity_contracts_payload=runtime.get("identity_contracts") or [],
                    query_capabilities=query_capabilities,
                    query_template_hashes_payload=runtime.get("query_template_hashes")
                    or {},
                    query_templates_payload=runtime.get("source_query_templates") or {},
                    require_production=True,
                )
            except MultiSourceReleaseContractError as exc:
                raise ReleaseBindingError(str(exc)) from exc
            if runtime.get("snapshot_set_id") != parsed_snapshot.snapshot_set_id:
                raise ReleaseBindingError(
                    "G-S6-SNAPSHOT-BINDING: runtime snapshot_set_id is inconsistent"
                )
        if "" in protected_artifacts or not protected_artifacts.issubset(checksums):
            raise ReleaseBindingError("runtime artifact is not protected by the package")
        if reasoning_capabilities:
            try:
                ontology_graph = Graph().parse(
                    package_dir / ontology_artifact,
                    format="turtle",
                )
            except Exception as exc:
                raise ReleaseBindingError(
                    f"runtime ontology artifact cannot be parsed: {exc}"
                ) from exc
            ontology_signature = {
                str(term)
                for triple in ontology_graph
                for term in triple
                if isinstance(term, URIRef)
            }
            for name, capability in reasoning_capabilities.items():
                missing_terms = sorted(
                    set(capability["ontology_terms"].values()) - ontology_signature
                )
                if missing_terms:
                    raise ReleaseBindingError(
                        f"reasoning capability uses terms absent from ontology: {name}: "
                        + ", ".join(missing_terms)
                    )

        # Only immutable, manifest-verified traces may supplement this view.
        # A partial S1 trace is not a complete execution snapshot binding.
        source_trace = {}
        if SCHEMA_TRACE in checksums:
            source_trace = protected_source_trace(
                self._read_json(self._safe_relative(package_dir, SCHEMA_TRACE)),
                self._read_json(self._safe_relative(package_dir, SOURCE_TRACE)) if SOURCE_TRACE in checksums else {},
                query_capabilities, checksums,
                inventory=(self._read_json(self._safe_relative(package_dir, INVENTORY_TRACE))
                           if INVENTORY_TRACE in checksums else None),
                project_id=publication.get("project_id"),
            )
            protected_artifacts.update(path for path in (SCHEMA_TRACE, SOURCE_TRACE, INVENTORY_TRACE) if path in checksums)
        payload = {
            **runtime,
            "release_fingerprint": expected_manifest_checksum,
            "ontology_artifact": ontology_artifact,
            "mapping_artifact": mapping_artifact,
            "source_mapping_sha256": source_mapping_sha256,
            "database_access_mode": database_access_mode,
            "runtime_mode": runtime_mode,
            "structured_query_enabled": structured_query_enabled,
            "ontop_identity_query_artifact": identity_query_artifact,
            "ontop_query_names": set(query_artifacts),
            "ontop_query_artifacts": query_artifacts,
            "ontop_query_capabilities": query_capabilities,
            "reasoning_capabilities": reasoning_capabilities,
            "reasoning_rule_packages": reasoning_rule_packages,
            "document_fact_queries": document_fact_queries,
            "document_query_capabilities": document_query_capabilities,
            "document_query_examples": list(runtime.get("document_query_examples") or []),
            "ontop_deployment_id": deployment_id,
            "artifact_checksums": {path: checksums[path] for path in sorted(protected_artifacts)},
            "package_path": str(package_dir),
            "integrity_status": "verified",
            "published_at": publication.get("published_at"),
            "contract_migrations": contract_migrations,
            "source_trace": source_trace,
        }
        return OntologyReleaseBinding.model_validate(payload)

    def _migrate_legacy_reasoning_capabilities(
        self,
        runtime: dict[str, Any],
        package_dir: Path,
        migrations: list[str],
    ) -> Any:
        """Hydrate v1-v3 packaged rule ids without mutating immutable releases."""

        raw = runtime.get("reasoning_capabilities")
        if int(runtime.get("schema_version") or 1) >= 4 or not isinstance(raw, dict):
            return raw
        migrated = deepcopy(raw)
        changed = False
        for _name, capability in migrated.items():
            if not isinstance(capability, dict) or capability.get("source_rule_ids"):
                continue
            artifact = str(capability.get("rule_artifact") or "").strip()
            if not artifact:
                continue
            rule_package = self._read_json(self._safe_relative(package_dir, artifact))
            rules = rule_package.get("rules")
            rule_ids = [
                str(rule.get("rule_id") or "").strip()
                for rule in (rules if isinstance(rules, list) else [])
                if isinstance(rule, dict) and str(rule.get("rule_id") or "").strip()
            ]
            if not rule_ids:
                continue
            capability["source_rule_ids"] = rule_ids
            changed = True
        if changed:
            migrations.append("RUNTIME_V1_V3_HYDRATE_SOURCE_RULE_IDS_FROM_PROTECTED_PACKAGE")
        return migrated

    @staticmethod
    def _pre_runtime_gates_passed(gates: dict[str, Any]) -> bool:
        """Permit deployment retries only after every non-runtime S7 gate passed."""

        if gates.get("status") not in {"PENDING", "FAILED"}:
            return False
        by_id = {
            str(item.get("id") or ""): str(item.get("status") or "")
            for item in gates.get("gates") or []
            if isinstance(item, dict)
        }
        prerequisites = {
            "G-S7-HUMAN-APPROVAL",
            "G-S7-INTEGRITY",
            "G-S7-PACKAGE-COMPLETE",
            "G-S7-MANIFEST",
            "G-S7-CQ-LINEAGE",
            "G-S7-RECOVERY-SNAPSHOT",
        }
        return all(
            by_id.get(gate_id) == "PASSED" for gate_id in prerequisites
        ) and by_id.get("G-S7-RUNTIME") in {"PENDING", "FAILED"}

    def build_ontop_client(
        self,
        binding: OntologyReleaseBinding,
        endpoint: str,
        *,
        timeout: float = 10.0,
    ) -> OntopClient:
        if not binding.structured_query_enabled:
            raise ReleaseBindingError("document-only release has no Ontop client")
        package_dir = Path(binding.package_path)
        query_files = {
            name: self._safe_relative(package_dir, artifact)
            for name, artifact in binding.ontop_query_artifacts.items()
        }
        return OntopClient(
            endpoint,
            timeout=timeout,
            allowed_queries=binding.ontop_query_names,
            query_files=query_files,
            query_checksums={
                name: binding.artifact_checksums[artifact]
                for name, artifact in binding.ontop_query_artifacts.items()
            },
            query_capabilities=binding.ontop_query_capabilities,
            bound_release_fingerprint=binding.release_fingerprint,
        )

    def _verify_manifest(
        self,
        package_dir: Path,
        manifest: dict[str, Any],
    ) -> dict[str, str]:
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise ReleaseBindingError("package manifest files are missing")
        checksums: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReleaseBindingError("package manifest entry is invalid")
            relative = str(entry.get("path") or "")
            expected = str(entry.get("sha256") or "")
            if not relative or relative in checksums:
                raise ReleaseBindingError("package manifest path is empty or duplicated")
            path = self._safe_relative(package_dir, relative)
            if self._checksum(path) != expected:
                raise ReleaseBindingError(f"package artifact checksum mismatch: {relative}")
            checksums[relative] = expected
        return checksums

    @staticmethod
    def _safe_relative(root: Path, relative: str) -> Path:
        if not relative or Path(relative).is_absolute():
            raise ReleaseBindingError("release artifact path is not relative")
        root = root.resolve()
        target = (root / relative).resolve()
        if root not in target.parents:
            raise ReleaseBindingError("release artifact path escapes its package")
        return target

    @staticmethod
    def _checksum(path: Path) -> str:
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ReleaseBindingError(f"release artifact is missing: {path.name}") from exc
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReleaseBindingError(f"release contract is missing: {path.name}") from exc
        except json.JSONDecodeError as exc:
            raise ReleaseBindingError(f"release contract is invalid JSON: {path.name}") from exc
        if not isinstance(payload, dict):
            raise ReleaseBindingError(f"release contract must be an object: {path.name}")
        return payload


class OntopDeploymentVerifier:
    """Verify an attestation read back from the deployed Ontop environment."""

    def verify_endpoint(
        self,
        client: OntopClient,
        binding: OntologyReleaseBinding,
        deployment_binding_path: Path,
    ) -> OntopDeploymentIdentity:
        try:
            deployment = verify_ontop_deployment_binding(
                binding,
                deployment_binding_path,
            )
        except OntopDeploymentContractError as exc:
            raise OntopRuntimeVerificationError(
                f"Ontop deployment binding verification failed: {exc}"
            ) from exc
        if str(deployment.get("endpoint") or "").rstrip("/") != client.endpoint.rstrip("/"):
            raise OntopRuntimeVerificationError(
                "Ontop deployment binding endpoint does not match the client"
            )
        package_dir = Path(binding.package_path)
        identity_path = ReleaseBindingLoader._safe_relative(
            package_dir,
            binding.ontop_identity_query_artifact,
        )
        probe = OntopClient(
            client.endpoint,
            timeout=client.timeout,
            allowed_queries={"orion_deployment_identity"},
            query_files={"orion_deployment_identity": identity_path},
            query_checksums={
                "orion_deployment_identity": binding.artifact_checksums[
                    binding.ontop_identity_query_artifact
                ]
            },
        )
        try:
            rows = probe.select("orion_deployment_identity")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise OntopRuntimeVerificationError(
                f"Ontop deployment marker query failed: {exc}"
            ) from exc
        expected = {
            "deployment_id": binding.ontop_deployment_id,
            "source_mapping_sha256": binding.source_mapping_sha256,
            "access_mode": binding.database_access_mode,
        }
        if len(rows) != 1 or any(rows[0].get(key) != value for key, value in expected.items()):
            raise OntopRuntimeVerificationError(
                "Ontop endpoint is not serving the approved release mapping marker"
            )
        identity = OntopDeploymentIdentity(
            deployment_id=binding.ontop_deployment_id,
            endpoint=client.endpoint,
            release_fingerprint=binding.release_fingerprint,
            mapping_sha256=binding.artifact_checksums[binding.mapping_artifact],
            access_mode="READ_ONLY",
            status="READY",
            verified_at=datetime.now(UTC),
        )
        client._mark_runtime_verified(identity.model_dump(mode="json"))
        return identity
