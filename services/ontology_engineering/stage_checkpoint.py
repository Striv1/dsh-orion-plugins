"""Revision-bound draft discovery; checkpoints never grant a formal commit token."""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import suppress
from typing import Any

from services.ontology_contracts.errors import WorkflowError


def write_checkpoint(directory_fd: int, stage: str, revision: int, receipt: dict[str, Any]) -> None:
    name = f"checkpoint-{stage}-r{revision}.json"
    # A local patch saves a new payload, but it is not evidence of repair.
    # Preserve the bounded diagnostic history through those saves.
    if 'repair_progress' not in receipt:
        try:
            old_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(old_fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise WorkflowError('检查点索引不是普通文件。')
                raw = stream.read(65537)
            try:
                previous = json.loads(raw) if len(raw) <= 65536 else None
            except (ValueError, UnicodeError) as exc:
                raise WorkflowError('检查点索引损坏，不能清除修订历史。') from exc
            if not isinstance(previous, dict) or any(previous.get(key) != receipt.get(key)
                    for key in ('project_id', 'stage', 'revision')):
                raise WorkflowError('检查点索引身份不匹配，不能清除修订历史。')
            if isinstance(previous.get('repair_progress'), dict):
                receipt['repair_progress'] = previous['repair_progress']
    temporary = f".checkpoint-{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def read_checkpoint_view(tools, project_dir, state, stage, pointer, offset, limit):
    if stage not in {"S1", "S2", "S3", "S4", "S5", "S6"}:
        raise WorkflowError("草稿阶段必须是 S1-S6。")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
        raise WorkflowError("草稿分页 offset 必须非负，limit 必须为 1-12000。")
    base = {"project_id": state["project_id"], "stage": stage, "revision": state["revision"],
            "formal_state_changed": False, "validated": False}
    relative = f".submission-drafts/checkpoint-{stage}-r{state['revision']}.json"
    try:
        with tools._open_workspace_source(project_dir, relative) as stream:
            raw = stream.read(65537)
    except WorkflowError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return {**base, "status": "NO_CURRENT_CHECKPOINT",
                    "instruction": "当前修订无检查点；按当前来源与合同分批开始，不复用旧修订草稿。"}
        raise
    if len(raw) > 65536:
        raise WorkflowError("检查点索引超出大小限制。")
    try:
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            raise ValueError("invalid receipt")
        if any(receipt.get(key) != base[key] for key in ("project_id", "stage", "revision")):
            raise ValueError("identity mismatch")
        ref = receipt["payload_file"]
        if not ref["file_name"].startswith(stage.lower() + "-"):
            raise ValueError("stage mismatch")
    except (ValueError, KeyError, TypeError) as exc:
        raise WorkflowError("检查点索引身份不匹配或已损坏。") from exc
    payload, _ = tools._read_submission_payload(project_dir, ref)
    if not isinstance(payload, dict):
        raise WorkflowError("检查点必须为 JSON 对象。")
    result = {**base, "status": "CHECKPOINT_AVAILABLE", "payload_file": ref,
              "fields": {key: {"type": type(value).__name__, "count": len(value) if isinstance(value, list | dict) else None}
                         for key, value in payload.items()},
              "instruction": "按 payload_file 增量修补；齐备后完整预检。检查点本身不证明任何业务内容通过。"}
    result['last_preflight'] = receipt.get('last_preflight')
    if 'last_compilation' in receipt:
        result['last_compilation'] = receipt['last_compilation']
    if isinstance(receipt.get('repair_progress'), dict):
        from .repair_progress import public_progress
        result['repair_progress'] = public_progress(receipt['repair_progress'])
    inventory = []
    for name, value in payload.items():
        if len(inventory) >= 50:
            break
        children = value.items() if name in {'mapping_draft', 'realtime_runtime'} and isinstance(value, dict) else []
        for child, content in children:
            if len(inventory) >= 50:
                break
            path = '/' + '/'.join(token.replace('~', '~0').replace('/', '~1') for token in (name, child))
            if len(path) > 2048:
                continue
            inventory.append({'pointer': path, 'type': type(content).__name__,
                              'count': len(content) if isinstance(content, list | dict) else None,
                              'read': {'tool': 'get_stage_draft', 'args': {
                                  'project_id': state['project_id'], 'stage': stage, 'pointer': path,
                              }}})
    result['saved_sections'] = inventory
    result['saved_sections_limit'] = 50
    if pointer is None:
        return result
    if not isinstance(pointer, str) or len(pointer) > 2048 or (pointer and not pointer.startswith("/")) or re.search(r"~(?:[^01]|$)", pointer):
        raise WorkflowError("pointer 必须是有效 JSON Pointer。")
    value = payload
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
        raise WorkflowError("请求的草稿字段不存在。") from exc
    content = json.dumps(value, ensure_ascii=False, sort_keys=True)
    end = min(len(content), offset + limit)
    return {**result, "pointer": pointer, "content": content[offset:end], "encoding": "json-text",
            "offset": offset, "total_characters": len(content), "truncated": end < len(content),
            "next_offset": end if end < len(content) else None}


def record_checkpoint_preflight(tools, project_dir, state, stage, reference, result):
    _record_checkpoint_repair(tools, project_dir, state, stage, reference, result,
                              source='PREFLIGHT', issues=result.get('issues') or [])


def record_checkpoint_compilation(tools, project_dir, state, reference, result):
    """Count a failed compile without changing its business payload or runtime.

    The caller holds the project mutation lock, just as for checkpoint preflight.
    Successful compilation is not a full preflight and must not reset history.
    """
    if (result.get('status') != 'BUSINESS_PLAN_REQUIRES_REPAIR'
            or state.get('current_stage') != 'S3'
            or result.get('formal_state_changed') is not False
            or result.get('draft_saved') is not False
            or any(result.get(key) != value for key, value in (
                ('project_id', state['project_id']), ('revision', state['revision']),
                ('stage', 'S3'), ('payload_file', reference),
            ))):
        return
    issues = [
        {'gate': 'G-S3-BUSINESS-COMPILATION', 'path': item.get('path'),
         'reason_code': item.get('reason_code'), 'message': item.get('detail')}
        for item in result.get('open_items') or []
        if isinstance(item, dict) and item.get('kind') == 'BUSINESS_PLAN_REVIEW_REQUIRED'
    ]
    _record_checkpoint_repair(tools, project_dir, state, 'S3', reference, result,
                              source='COMPILATION', issues=issues)


def _record_checkpoint_repair(tools, project_dir, state, stage, reference, result, *, source, issues):
    """Retain budget and diagnostics only for the exact current draft baseline."""
    relative = f".submission-drafts/checkpoint-{stage}-r{state['revision']}.json"
    try:
        with tools._open_workspace_source(project_dir, relative) as stream:
            raw = stream.read(65537)
    except WorkflowError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return
        raise
    if len(raw) > 65536:
        raise WorkflowError('检查点索引超出大小限制。')
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or any(receipt.get(key) != expected for key, expected in (
        ('project_id', state['project_id']), ('revision', state['revision']),
        ('stage', stage), ('payload_file', reference),
    )):
        return
    from .repair_progress import advance, public_progress

    receipt['repair_progress'] = advance(
        receipt.get('repair_progress'),
        {'status': 'FAILED' if source == 'COMPILATION' else result.get('status'), 'issues': issues},
        tools.service._validator_fingerprint(), source=source)
    result['repair_progress'] = public_progress(receipt['repair_progress'])
    if receipt['repair_progress']['automatic_continuation'] == 'STOP':
        result['automatic_repair_stop'] = {
            'code': 'REPAIR_NOT_CONVERGING', 'stage': stage, 'project_id': state['project_id'],
            'revision': state['revision'],
            'message': '当前修订未收敛，已保留草稿与诊断；停止本轮自动修订，由平台维护核对字段合同或执行条件。',
            'formal_state_changed': False,
        }
    diagnostic_key = 'last_compilation' if source == 'COMPILATION' else 'last_preflight'
    receipt[diagnostic_key] = {
        'status': result.get('status'), 'issue_count': len(issues),
        'issues': [{key: str(issue[key])[:256] for key in ('gate', 'path', 'message', 'reason', 'reason_code')
                    if issue.get(key) is not None}
                   for issue in issues[:10] if isinstance(issue, dict)],
        'issues_truncated': len(issues) > 10,
        'issue_details_truncated': any(len(str(issue.get(key, ''))) > 256
            for issue in issues[:10] if isinstance(issue, dict)
            for key in ('gate', 'path', 'message', 'reason', 'reason_code')),
        'instruction': ('这是当前草稿最近一次编译诊断；编译失败未修改业务草稿或运行内容。'
                        if source == 'COMPILATION' else '这是当前草稿最近一次预检结果；')
                       + '先按问题位置局部修订。完整预检通过后才能提交，不能凭此摘要批准。',
    }
    # Both summaries share the existing 64 KiB checkpoint limit. Trim older
    # details first; never drop the budget or silently write an unreadable index.
    older_key = 'last_preflight' if source == 'COMPILATION' else 'last_compilation'
    for key in (older_key, diagnostic_key):
        diagnostic = receipt.get(key)
        while (isinstance(diagnostic, dict) and diagnostic.get('issues')
               and len(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()) > 65536):
            diagnostic['issues'].pop()
            diagnostic['issues_truncated'] = True
    root_fd = os.open(project_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        draft_fd = os.open('.submission-drafts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            write_checkpoint(draft_fd, stage, state['revision'], receipt)
        finally:
            os.close(draft_fd)
    finally:
        os.close(root_fd)
