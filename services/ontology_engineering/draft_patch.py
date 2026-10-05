"""Small atomic edits to an already stored draft; formal preflight stays mandatory."""
from __future__ import annotations

import copy
import json
import re
from typing import Any


def apply_draft_patch(payload: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 200:
        raise ValueError("草稿修正需要1至200个操作")
    if len(json.dumps(operations, ensure_ascii=False, allow_nan=False).encode()) > 8 * 1024 * 1024:
        raise ValueError("草稿修正超过8 MiB")
    result = copy.deepcopy(payload)
    for index, operation in enumerate(operations):
        try:
            _apply_one(result, operation)
        except ValueError as error:
            raise ValueError(f"operations[{index}] {error}") from None
    return result


def _apply_one(result: dict[str, Any], operation: dict[str, Any]) -> None:
    if not isinstance(operation, dict):
        raise ValueError("每个修正必须是对象")
    op, path = operation.get("op"), operation.get("path")
    if op not in {"add", "replace", "remove", "test", "replace_text"}:
        raise ValueError("修正仅支持add、replace、remove、test、replace_text")
    keys = {"op", "path"} if op == "remove" else {"op", "path", "value"}
    if set(operation) != keys:
        raise ValueError("修正操作字段不完整或包含未知字段")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 2048:
        raise ValueError("path必须是指向草稿字段的JSON Pointer，不能替换整个草稿")
    if re.search(r"~(?:[^01]|$)", path):
        raise ValueError("JSON Pointer转义无效")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    parent: Any = result
    for depth, part in enumerate(parts[:-1]):
        if isinstance(parent, dict) and part in parent:
            parent = parent[part]
        elif isinstance(parent, list) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(parent):
            parent = parent[int(part)]
        else:
            raise ValueError(
                f"path={path} 的父字段不存在：{_locate(parts, depth)} 不可达。{_available(parent)}"
                f"{_init_hint(parts, depth)}"
            )
    key = parts[-1]
    if isinstance(parent, list):
        if key == "-" and op == "add":
            index = len(parent)
        elif re.fullmatch(r"0|[1-9][0-9]*", key):
            index = int(key)
        else:
            raise ValueError(f"path={path} 的数组索引无效；追加请用 /-，定位请用0至{max(len(parent) - 1, 0)}")
        if index > len(parent) or (op != "add" and index == len(parent)):
            raise ValueError(f"path={path} 数组索引越界；当前长度{len(parent)}，追加请用 /-")
        if op == "add":
            parent.insert(index, copy.deepcopy(operation["value"]))
        elif op == "replace":
            parent[index] = copy.deepcopy(operation["value"])
        elif op == "replace_text":
            parent[index] = _replace_text(parent[index], operation["value"], path)
        elif op == "remove":
            del parent[index]
        elif not _same_json(parent[index], operation["value"]):
            raise ValueError(f"path={path} 的test前置条件与草稿当前值不一致，草稿未修改；请先用get_stage_draft读回基线")
    elif isinstance(parent, dict):
        if op != "add" and key not in parent:
            raise ValueError(f"path={path} 的字段不存在；{_available(parent)}新增字段请用op=add")
        if op in {"add", "replace"}:
            parent[key] = copy.deepcopy(operation["value"])
        elif op == "replace_text":
            parent[key] = _replace_text(parent[key], operation["value"], path)
        elif op == "remove":
            del parent[key]
        elif not _same_json(parent[key], operation["value"]):
            raise ValueError(f"path={path} 的test前置条件与草稿当前值不一致，草稿未修改；请先用get_stage_draft读回基线")
    else:
        raise ValueError(f"path={path} 的父字段是标量，不能在其下修正；请改为替换该标量本身")


def _locate(parts: list[str], depth: int) -> str:
    return "/" + "/".join(parts[: depth + 1])


def _available(parent: Any) -> str:
    if isinstance(parent, dict):
        names = sorted(parent)
        shown = "、".join(names[:12]) if names else "（空对象）"
        more = f"等{len(names)}个" if len(names) > 12 else ""
        return f"该层已有字段：{shown}{more}。"
    if isinstance(parent, list):
        return f"该层是数组，长度{len(parent)}。"
    return "该层不是对象或数组。"


def _init_hint(parts: list[str], depth: int) -> str:
    if depth == 0 and len(parts) > 1:
        container = "[]" if re.fullmatch(r"0|[1-9][0-9]*|-", parts[1]) else "{}"
        return f"顶层字段 /{parts[0]} 尚未初始化，请先提交 {{\"op\":\"add\",\"path\":\"/{parts[0]}\",\"value\":{container}}} 再追加内容。"
    return ""


def _same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def _replace_text(current: Any, value: Any, path: str) -> str:
    """Exact, unique text edit; no regex, implicit append or first-match guessing."""
    if not isinstance(current, str):
        raise ValueError(f"path={path} 的replace_text目标必须是文本")
    if (not isinstance(value, dict) or set(value) != {"old", "new"}
            or not isinstance(value["old"], str) or not value["old"]
            or not isinstance(value["new"], str)):
        raise ValueError("replace_text的value必须是{old:非空原文,new:替换文本}，使用精确文本而非正则")
    old = value["old"]
    start = current.find(old)
    if start < 0 or current.find(old, start + 1) >= 0:
        raise ValueError(f"path={path} 的old必须恰好匹配一处（包含重叠匹配）；草稿未修改，请回读并扩大唯一原文范围")
    updated = current[:start] + value["new"] + current[start + len(old):]
    if len(updated.encode()) > 8 * 1024 * 1024:
        raise ValueError("replace_text结果超过8 MiB")
    return updated
