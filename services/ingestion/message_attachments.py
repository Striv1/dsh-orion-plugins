"""Bridge admitted native message attachments to explicit ORION source subsets.

Registration is an internal host API, never a model-callable MCP tool. It only
accepts durable attachment references captured by the host from one real message.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from services.ingestion.document_jobs import document_input_root, verified_document_source
from services.ingestion.s0_batch_executor import EXECUTABLE_EXTENSIONS
from services.ontology_engineering import WorkflowError

_BATCH = re.compile(r"MESSAGE-[A-F0-9]{32}\Z")
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_MAX_FILES = 5000
_MAX_BYTES = 20 * 1024**3


def _directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise WorkflowError("受管附件目录必须是可信绝对目录，不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


def _reference(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowError("原生附件引用必须为完整 FileAttachmentRef")
    digest, name, size = raw.get("attachmentId"), raw.get("name"), raw.get("bytes")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise WorkflowError("原生附件缺少合法内容标识")
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(char in name for char in ("/", "\\", "\0"))):
        raise WorkflowError("原生附件 name 必须是宿主已净化的单个文件名")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise WorkflowError("原生附件缺少精确字节数")
    return {"attachmentId": digest, "name": name, "bytes": size}


def register_message_attachment_candidates(
    *, root: Path, attachment_root: Path, session_id: str, message_id: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Host-only registration: metadata only, no attachment contents are read."""
    if not isinstance(session_id, str) or not session_id.strip() or not isinstance(message_id, str) or not message_id.strip():
        raise WorkflowError("候选附件批次必须绑定真实 session_id/message_id")
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= _MAX_FILES:
        raise WorkflowError("单消息候选附件数量必须在 1..5000 之间")
    references = [_reference(item) for item in attachments]
    if sum(item["bytes"] for item in references) > _MAX_BYTES:
        raise WorkflowError("候选附件总量超过 20GB")
    root = _directory(root)
    if not attachment_root.is_absolute() or attachment_root.is_symlink() or not attachment_root.is_dir():
        raise WorkflowError("宿主附件根目录必须存在且不能是符号链接")
    registry = _directory(root / ".orion-message-attachments")
    batch_id = "MESSAGE-" + uuid.uuid4().hex.upper()
    entries = [{"id": f"ATT-{index:04d}", **item,
                "supported": Path(item["name"]).suffix.lower() in EXECUTABLE_EXTENSIONS}
               for index, item in enumerate(references, start=1)]
    record = {"schema_version": 1, "batch_id": batch_id, "session_id": session_id,
              "message_id": message_id, "attachment_root": str(attachment_root.resolve()),
              "attachments": entries}
    path = registry / f"{batch_id}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(record, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return _public_candidates(record)


def _public_candidates(record: dict[str, Any]) -> dict[str, Any]:
    return {"status": "CANDIDATES_ONLY", "batch_id": record["batch_id"],
            "session_id": record["session_id"], "message_id": record["message_id"],
            "attachment_count": len(record["attachments"]), "source_authorized": False,
            "attachments": [{"attachment_id": item["id"], "file_name": item["name"],
                             "bytes": item["bytes"], "sha256": item["attachmentId"],
                             "supported": item["supported"]} for item in record["attachments"]],
            "instruction": "这些仅是当前消息实际附件候选，不代表全部获准作为工程来源。依据用户明确范围选择 attachment_ids，排除汇总、说明或其他未授权文件；再调用 snapshot_message_attachments。不要扫描宿主附件目录或猜测 source_path。"}


def _load(root: Path, batch_id: str) -> dict[str, Any]:
    if not isinstance(batch_id, str) or not _BATCH.fullmatch(batch_id):
        raise WorkflowError("batch_id 必须来自当前消息宿主提供的候选附件回执")
    registry = root / ".orion-message-attachments"
    path = registry / f"{batch_id}.json"
    if not root.is_absolute() or root.is_symlink() or registry.is_symlink() or path.is_symlink() or not path.is_file():
        raise WorkflowError("受管候选附件回执不存在或不可信")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise WorkflowError("受管候选附件回执无法读取") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("batch_id") != batch_id:
        raise WorkflowError("候选附件回执身份不一致")
    if not record.get("session_id") or not record.get("message_id") or not isinstance(record.get("attachments"), list):
        raise WorkflowError("候选附件回执缺少消息绑定")
    rows = record["attachments"]
    if (not 1 <= len(rows) <= _MAX_FILES
            or not isinstance(record.get("attachment_root"), str)
            or not isinstance(record["session_id"], str) or not isinstance(record["message_id"], str)):
        raise WorkflowError("候选附件回执结构失效")
    for index, row in enumerate(rows, start=1):
        ref = _reference(row)
        if row.get("id") != f"ATT-{index:04d}" or row.get("supported") != (Path(ref["name"]).suffix.lower() in EXECUTABLE_EXTENSIONS):
            raise WorkflowError("候选附件回执条目失效")
    if sum(row["bytes"] for row in rows) > _MAX_BYTES:
        raise WorkflowError("候选附件回执超过大小限制")
    return record


def get_message_attachment_candidates(*, root: Path, batch_id: str) -> dict[str, Any]:
    return _public_candidates(_load(root, batch_id))


def _copy_attachment(root: Path, ref: dict[str, Any], target: Path) -> None:
    digest = ref["attachmentId"].removeprefix("sha256:")
    parts = ("files", digest[:2], digest, ref["name"])
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(source_fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != ref["bytes"]:
            raise WorkflowError("宿主附件不是普通文件或字节数已漂移")
        actual = hashlib.sha256()
        copied = 0
        with target.open("xb") as destination:
            while chunk := source.read(1024 * 1024):
                copied += len(chunk)
                if copied > ref["bytes"]:
                    raise WorkflowError("宿主附件复制期间字节数已漂移")
                actual.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        after = os.fstat(source.fileno())
        if (copied != ref["bytes"] or actual.hexdigest() != digest
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise WorkflowError("宿主附件内容与原消息 hash 不一致或复制期间发生漂移")


def snapshot_message_attachments(
    *, root: Path, batch_id: str, attachment_ids: list[str],
) -> dict[str, Any]:
    """Copy only the explicit subset from a host-registered message into S0."""
    record = _load(root, batch_id)
    if (not isinstance(attachment_ids, list) or not attachment_ids
            or any(not isinstance(item, str) for item in attachment_ids)
            or len(set(attachment_ids)) != len(attachment_ids)):
        raise WorkflowError("必须明确给出不重复的非空 attachment_ids 子集，不能默认使用整批附件")
    entries = {item["id"]: item for item in record["attachments"]}
    if set(attachment_ids) - set(entries):
        raise WorkflowError("attachment_ids 包含不属于此消息批次的附件，未复制任何文件")
    selected = [entries[key] for key in sorted(attachment_ids)]
    references = [_reference(item) for item in selected]
    if any(Path(item["name"]).suffix.lower() not in EXECUTABLE_EXTENSIONS for item in references):
        raise WorkflowError("所选子集含 S0 不支持的附件格式，未部分接入")
    attachment_root = Path(record["attachment_root"])
    if not attachment_root.is_absolute() or attachment_root.is_symlink():
        raise WorkflowError("宿主附件根目录已失效")
    files = [{"path": f"{batch_id}/{item['id']}/{ref['name']}", "bytes": ref["bytes"], "sha256": ref["attachmentId"]}
             for item, ref in zip(selected, references, strict=True)]
    fingerprint = hashlib.sha256()
    for item in files:
        fingerprint.update(f"{item['path']}\0{item['bytes']}\0{item['sha256'].removeprefix('sha256:')}\n".encode())
    reference_id = f"REFERENCE-{fingerprint.hexdigest()[:20].upper()}"
    source_path = f".orion-s0-uploads/{reference_id}"
    manifest = {"reference_id": reference_id, "source_path": source_path,
                "file_count": len(files), "total_bytes": sum(item["bytes"] for item in files),
                "files": files, "message_attachment_origin": {
                    "batch_id": batch_id, "session_id": record["session_id"],
                    "message_id": record["message_id"], "selected_attachment_ids": sorted(attachment_ids)}}
    uploads = _directory(root / ".orion-s0-uploads")
    target = root / source_path
    if not os.path.lexists(target):
        temporary = Path(tempfile.mkdtemp(prefix=".message-attachments-", dir=uploads))
        try:
            for ref, item in zip(references, files, strict=True):
                destination = temporary / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_attachment(attachment_root, ref, destination)
            (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            # Atomic publication into a new content-addressed directory.
            os.rename(temporary, target)
        except (OSError, ValueError, WorkflowError) as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(exc, WorkflowError):
                raise
            raise WorkflowError(f"受管附件快照未完成：{type(exc).__name__}") from exc
    verified_document_source(root, source_path)
    for file in target.rglob("*"):
        if file.is_file():
            file.chmod(0o400)
    for directory in sorted((item for item in target.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        directory.chmod(0o500)
    target.chmod(0o500)
    return {"status": "SNAPSHOT_READY", "reference_id": reference_id,
            "source_path": source_path, "source_snapshot_path": source_path,
            "file_count": len(files), "total_bytes": manifest["total_bytes"],
            "manifest_sha256": "sha256:" + hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest(),
            "message_attachment_origin": manifest["message_attachment_origin"],
            "excluded_attachment_ids": sorted(set(entries) - set(attachment_ids)),
            "scope_note": "仅固化明确子集。创建 DOCUMENT_ONLY 工程时直接传 source_snapshot_path，平台验证完整批次并生成 source_scope；不要抄写或拆批。未创建、授权或推进工程。"}


def main() -> int:
    """Private host stdin interface; never infer references from directories."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or set(payload) - {"root", "attachment_root", "session_id", "message_id", "attachments"}:
            raise WorkflowError("内部附件注册参数不合法")
        result = register_message_attachment_candidates(
            root=Path(payload["root"]) if payload.get("root") else document_input_root(),
            attachment_root=Path(payload["attachment_root"]),
            session_id=payload["session_id"], message_id=payload["message_id"],
            attachments=payload["attachments"],
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (WorkflowError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"附件候选注册失败：{str(exc) if isinstance(exc, WorkflowError) else type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
