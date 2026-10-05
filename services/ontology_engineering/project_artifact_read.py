"""Bounded read-only access to formal project artifacts and stage diagnostics.

The 3081 agent must be able to inspect committed stage outputs (for example
S3 runtime/mapping.obda or an S6 diagnostic) without a terminal. Access is
restricted to formal stage directories and diagnostics, paginated, and
credential-redacted. Drafts use get_stage_draft; bulky S6 payloads and revision
snapshots stay out of reach.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from services.ontology_contracts.errors import WorkflowError

STAGE_DIRECTORIES = (
    "00-document-evidence", "01-data-understanding", "02-semantic-recognition",
    "03-mapping-review", "04-ontology-design", "05-ontology-build",
    "06-quality-validation", "07-release",
)
DIAGNOSTIC_DIRECTORIES = (".stage-executions/S5-diagnostics", ".stage-executions/S6-diagnostics")
JOB_DIRECTORY = "stage-jobs"
TEXT_SUFFIXES = frozenset({
    ".json", ".jsonl", ".yaml", ".yml", ".obda", ".rq", ".sparql", ".ttl", ".owl",
    ".md", ".txt", ".csv", ".log", ".properties", ".html",
})
EXCLUDED_PARTS = frozenset({"assets", "review-history", "joint-design-inputs"})
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LIST = 400
DEFAULT_LIMIT = 6000
MAX_LIMIT = 12000

_URL_CREDENTIAL = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s\"']+):(?P<secret>[^@/\s\"']+)@")
_KEY_CREDENTIAL = re.compile(
    r"(?P<key>[\"']?(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|token)[\"']?\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\"'\s,}]+)",
    re.IGNORECASE,
)
_JDBC_PASSWORD = re.compile(r"(?P<key>jdbc\.password\s*=\s*)(?P<value>\S+)", re.IGNORECASE)


def redact(text: str) -> tuple[str, int]:
    count = 0

    def _url(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match['scheme']}{match['user']}:***@"

    def _key(match: re.Match[str]) -> str:
        nonlocal count
        if match["value"] in {"***", "null", "None", "true", "false"}:
            return match.group(0)
        count += 1
        return f"{match['key']}{match['quote']}***"

    def _jdbc(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match['key']}***"

    text = _URL_CREDENTIAL.sub(_url, text)
    text = _JDBC_PASSWORD.sub(_jdbc, text)
    text = _KEY_CREDENTIAL.sub(_key, text)
    return text, count


def _allowed_relative(relative: str) -> bool:
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or Path(relative).is_absolute():
        return False
    if EXCLUDED_PARTS.intersection(parts) or any(part.startswith(".") for part in parts[1:]):
        return False  # hidden subdirectories hold drafts/checkpoints (get_stage_draft owns those)
    if Path(relative).suffix.lower() not in TEXT_SUFFIXES:
        return False
    if parts[0] in STAGE_DIRECTORIES:
        return True
    if len(parts) == 2 and parts[0] == JOB_DIRECTORY:
        return True
    return len(parts) == 3 and "/".join(parts[:2]) in DIAGNOSTIC_DIRECTORIES


def _safe_file(project: Path, relative: str) -> Path:
    if not isinstance(relative, str) or len(relative) > 512 or not _allowed_relative(relative):
        raise WorkflowError(
            "path 不在可读范围：只允许 00-07 阶段目录的正式文本制品、stage-jobs/ 与 "
            ".stage-executions/S5|S6-diagnostics/ 下的文件；草稿请用 get_stage_draft。"
        )
    current = project
    for part in Path(relative).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise WorkflowError("请求的制品不存在；先不传 path 列出可读制品目录。") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkflowError("制品路径包含符号链接，拒绝读取。")
    if not stat.S_ISREG(os.lstat(current).st_mode):
        raise WorkflowError("请求的路径不是文件。")
    return current


def _catalog(project: Path, lifecycle: dict[str, Any], prefix: str | None) -> list[dict[str, Any]]:
    roots = [*STAGE_DIRECTORIES, JOB_DIRECTORY, *DIAGNOSTIC_DIRECTORIES]
    entries: list[dict[str, Any]] = []
    for root in roots:
        base = project / root
        if base.is_symlink() or not base.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_PARTS
                                 and not name.startswith(".")
                                 and not (Path(directory) / name).is_symlink())
            for name in sorted(filenames):
                path = Path(directory) / name
                relative = path.relative_to(project).as_posix()
                if path.is_symlink() or not _allowed_relative(relative):
                    continue
                if prefix and not relative.startswith(prefix):
                    continue
                entry = {"path": relative, "bytes": path.stat().st_size}
                status = (lifecycle.get(relative) or {}).get("status")
                if status:
                    entry["artifact_lifecycle"] = status
                entries.append(entry)
    if root_entries := [entry for entry in entries if entry["path"].startswith(".stage-executions")]:
        root_entries.sort(key=lambda entry: (project / entry["path"]).stat().st_mtime, reverse=True)
        entries = [entry for entry in entries if not entry["path"].startswith(".stage-executions")] + root_entries
    return entries


def _pointer_value(document: Any, pointer: str) -> Any:
    if len(pointer) > 2048 or (pointer and not pointer.startswith("/")) or re.search(r"~(?:[^01]|$)", pointer):
        raise WorkflowError("pointer 必须是有效 JSON Pointer。")
    value = document
    try:
        for token in pointer[1:].split("/") if pointer else []:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
                value = value[int(token)]
            elif isinstance(value, dict):
                value = value[token]
            else:
                raise KeyError(token)
    except (IndexError, KeyError) as exc:
        raise WorkflowError("请求的 JSON 字段不存在。") from exc
    return value


def _outline(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: {"type": type(item).__name__,
                      "count": len(item) if isinstance(item, list | dict) else None}
                for key, item in list(value.items())[:80]}
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    return {"type": type(value).__name__}


def read_project_artifact(
    service: Any, *, project_id: str, path: str | None = None, pointer: str | None = None,
    offset: int = 0, limit: int = DEFAULT_LIMIT, prefix: str | None = None,
) -> dict[str, Any]:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise WorkflowError(f"offset 必须非负，limit 必须为 1-{MAX_LIMIT}。")
    project = service._resolve_project(project_id)
    state = service._read_state(project)
    lifecycle = state.get("artifact_lifecycle") or {}
    base = {"project_id": project_id, "revision": state.get("revision"),
            "stage_statuses": state.get("stage_statuses"), "formal_state_changed": False}
    if path is None:
        entries = _catalog(project, lifecycle, prefix)
        return {**base, "status": "CATALOG", "artifacts": entries[:MAX_LIST],
                "total": len(entries), "truncated": len(entries) > MAX_LIST,
                "instruction": "传 path 读取正文；JSON 文件可再传 pointer 读取子字段；长文本按 next_offset 分页。"
                               "STALE/SUPERSEDED 生命周期的制品不能当作当前批准结果。"}
    target = _safe_file(project, path)
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise WorkflowError(f"制品 {size} 字节超过只读上限 {MAX_FILE_BYTES}；请读取更小的派生制品。")
    raw = target.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowError("制品不是 UTF-8 文本。") from exc
    result = {**base, "status": "ARTIFACT", "path": path, "bytes": size, "sha256": digest,
              "artifact_lifecycle": (lifecycle.get(path) or {}).get("status", "UNTRACKED")}
    if pointer is not None:
        if target.suffix.lower() != ".json":
            raise WorkflowError("pointer 只适用于 JSON 制品。")
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowError("JSON 制品无法解析。") from exc
        value = _pointer_value(document, pointer)
        text = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)
        result["pointer"] = pointer
        result["outline"] = _outline(value)
    elif target.suffix.lower() == ".json" and offset == 0:
        with contextlib.suppress(json.JSONDecodeError):
            result["outline"] = _outline(json.loads(text))
    text, redactions = redact(text)
    end = min(len(text), offset + limit)
    return {**result, "content": text[offset:end], "offset": offset,
            "total_characters": len(text), "truncated": end < len(text),
            "next_offset": end if end < len(text) else None, "redactions": redactions}

