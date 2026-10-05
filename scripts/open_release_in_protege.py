from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW_ROOT = ROOT / ".orion-workflows"
DEFAULT_REVIEW_ROOT = ROOT / ".orion-runtime" / "protege-release-review"
ONTOLOGY_ARTIFACT = Path("01-本体模型/ontology.owl")
REVIEW_CONTRACT = Path("04-发布信息/owl-dl-review-contract.json")
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")


def checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def verify_package(package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / "manifest.json"
    manifest = read_json(manifest_path)
    declared = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in manifest.get("files") or []
        if isinstance(item, dict)
    }
    actual = {
        path.relative_to(package_dir).as_posix(): checksum(path)
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and path != manifest_path and path.name != ".DS_Store"
    }
    if declared != actual or int(manifest.get("file_count") or -1) != len(actual):
        raise ValueError("release package manifest or file checksum mismatch")
    return manifest


def discover_protege() -> Path:
    user_root = Path.home()
    candidates = [
        Path("/Applications/Protégé-5.6.9-全面汉化版.app"),
        user_root
        / "FDE/FDE-Ontology-Studio-0.1.0-macOS-arm64 2/FDE Protégé 团队版.app",
        user_root / "Downloads/Protege-5.6.9/Protégé.app",
        Path("/Applications/Protégé.app"),
        Path("/Applications/Protege.app"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "未找到 Protégé 应用；请用 --application 指定 .app 路径。"
    )


def prepare_review_copy(args: argparse.Namespace) -> dict[str, Any]:
    if not PROJECT_ID_PATTERN.fullmatch(args.project_id):
        raise ValueError("project_id 格式不安全。")
    if not VERSION_PATTERN.fullmatch(args.release_version):
        raise ValueError("release_version 必须是语义化版本号。")
    workflow_root = Path(args.workflow_root).expanduser().resolve()
    project_dir = workflow_root / args.project_id
    package_dir = (
        project_dir
        / "07-release"
        / f"ontology-engineering-package-{args.release_version}"
    ).resolve()
    if workflow_root not in package_dir.parents or not package_dir.is_dir():
        raise FileNotFoundError("未找到指定项目版本的正式发布包。")
    manifest = verify_package(package_dir)
    if (
        manifest.get("project_id") != args.project_id
        or manifest.get("release_version") != args.release_version
        or manifest.get("approval_decision") != "APPROVED"
    ):
        raise ValueError("发布包身份、版本或批准状态不匹配。")

    source_ontology = package_dir / ONTOLOGY_ARTIFACT
    contract_path = package_dir / REVIEW_CONTRACT
    contract = (
        read_json(contract_path)
        if contract_path.is_file()
        else {
            "schema_version": 0,
            "project_id": args.project_id,
            "release_version": args.release_version,
            "ontology_sha256": checksum(source_ontology),
            "review_mode": "LEGACY_RELEASE_COPY_BEFORE_DESKTOP_REVIEW",
        }
    )
    if checksum(source_ontology) != contract.get("ontology_sha256"):
        raise ValueError("ontology.owl 与 OWL DL 复核契约哈希不一致。")

    review_root = Path(args.review_root).expanduser().resolve()
    review_dir = review_root / args.project_id / args.release_version
    review_dir.mkdir(parents=True, exist_ok=True)
    review_ontology = review_dir / "ontology.owl"
    shutil.copy2(source_ontology, review_ontology)
    (review_dir / REVIEW_CONTRACT.name).write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    packaged_hermit = package_dir / "03-质量结论/hermit-report.json"
    workspace_hermit = project_dir / "06-quality-validation/hermit-report.json"
    hermit_source = (
        packaged_hermit
        if packaged_hermit.is_file()
        else (workspace_hermit if workspace_hermit.is_file() else None)
    )
    if hermit_source is not None:
        shutil.copy2(hermit_source, review_dir / "hermit-report.json")
    receipt = {
        "schema_version": 1,
        "project_id": args.project_id,
        "release_version": args.release_version,
        "source_package": str(package_dir),
        "source_manifest_sha256": checksum(package_dir / "manifest.json"),
        "source_ontology_sha256": checksum(source_ontology),
        "review_ontology": str(review_ontology),
        "review_ontology_sha256": checksum(review_ontology),
        "review_contract_profile": contract.get("review_mode"),
        "hermit_reference_source": (
            "RELEASE_PACKAGE"
            if hermit_source == packaged_hermit
            else ("PROJECT_WORKSPACE" if hermit_source == workspace_hermit else "UNAVAILABLE")
        ),
        "prepared_at": datetime.now().astimezone().isoformat(),
        "release_artifact_modified": False,
    }
    (review_dir / "review-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验正式发布包，复制 ontology.owl 到独立目录并用 Protégé 打开。"
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--workflow-root", default=str(DEFAULT_WORKFLOW_ROOT))
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    parser.add_argument("--application")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    receipt = prepare_review_copy(args)
    if not args.no_open:
        application = (
            Path(args.application).expanduser().resolve()
            if args.application
            else discover_protege()
        )
        if not application.is_dir() or application.suffix != ".app":
            raise FileNotFoundError("Protégé application path is not a .app bundle")
        subprocess.run(
            ["open", "-a", str(application), receipt["review_ontology"]],
            check=True,
        )
        receipt["application"] = str(application)
        receipt["opened"] = True
    else:
        receipt["opened"] = False
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
