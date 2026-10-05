"""Recovery records for legacy single-writer preflight commits.

Records are not a second workflow state machine or a cross-file transaction.
Unknown partial writes remain blocked; a completed checkpoint can be read back.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DIRECTORY = '.preflight-operations'
PENDING = {'PREPARED', 'CHECKPOINTED'}


def digest(value: Any) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(record, dict):
        raise ValueError('预检操作恢复记录格式无效。')
    checksum = record.pop('record_sha256', None)
    if record.get('schema_version') != 1 or checksum != digest(record):
        raise ValueError('预检操作恢复记录完整性校验失败，需核对正式证据。')
    return record


def sealed(record: dict[str, Any]) -> dict[str, Any]:
    value = {**record, 'schema_version': 1}
    return {**value, 'record_sha256': digest(value)}


def pending(project_dir: Path) -> list[dict[str, Any]]:
    results = []
    for path in sorted((project_dir / DIRECTORY).glob('*.json')):
        record = read(path)
        if record and record.get('status') in PENDING:
            results.append(record)
    return results


def recovery_diagnostics(
    project_dir: Path, checkpoint_reader: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Inspect unresolved commits without repairing, authorizing, or executing them.

    ``checkpoint_reader(stage)`` must use the workflow's existing checkpoint
    implementation. A stage label (including PASSED) is never recovery evidence.
    The result deliberately excludes tokens, payloads and saved model results.
    """
    from datetime import datetime

    results = []
    for path in sorted((project_dir / DIRECTORY).glob('*.json')):
        item = {'operation_id': path.stem, 'writes_performed': False,
                'automatic_recovery_authorized': False}
        try:
            if path.is_symlink():
                raise ValueError('operation_symlink')
            record = read(path)
            if not record:
                raise ValueError('operation_missing')
            status = record.get('status')
            if status not in PENDING | {'COMMITTED', 'FAILED', 'ABORTED'}:
                raise ValueError('operation_status_invalid')
            if (record.get('operation_id') != path.stem
                    or record.get('project_id') != project_dir.name
                    or record.get('stage') not in {'S1', 'S2', 'S3', 'S4', 'S5', 'S6'}):
                raise ValueError('operation_identity_invalid')
            if status not in PENDING:
                continue
            item.update(stage=record['stage'], status=status)
            receipt_path = project_dir / '.preflight-submissions' / path.name
            if receipt_path.is_symlink():
                raise ValueError('receipt_symlink')
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            if (not isinstance(receipt, dict)
                    or receipt.get('project_id') != project_dir.name
                    or receipt.get('stage') != record['stage']
                    or not isinstance(receipt.get('payload'), dict)
                    or receipt.get('payload_sha256') != digest(receipt['payload'])):
                raise ValueError('receipt_identity_or_payload_invalid')
            revision = int(receipt.get('project_revision') or 0)
            request_hash = digest({'project_id': project_dir.name, 'stage': record['stage'],
                                   'revision': revision, 'payload': receipt['payload_sha256']})
            if record.get('request_hash') != request_hash:
                raise ValueError('operation_request_mismatch')
            item['original_revision'] = revision
            saved = record.get('before' if status == 'PREPARED' else 'checkpoint')
            if not isinstance(saved, dict) or not isinstance(saved.get('files'), dict):
                raise ValueError('checkpoint_invalid')
            current = checkpoint_reader(record['stage'])
            if saved != current:
                item.update(diagnosis='RECONCILIATION_REQUIRED',
                            checkpoint_matches=False, recovery_mode='NONE',
                            reason='正式证据与操作检查点不一致，保留阻塞并核对部分写入；禁止重跑。')
            elif status == 'CHECKPOINTED':
                failure = record.get('failure')
                if failure and (not isinstance(failure, dict)
                                or not failure.get('gate') or not failure.get('message')):
                    raise ValueError('failure_evidence_invalid')
                item.update(diagnosis='FAILURE_REPLAY_AVAILABLE' if failure else 'RESULT_REPLAY_AVAILABLE',
                            checkpoint_matches=True, recovery_mode='ORIGINAL_TOKEN_REPLAY',
                            reason='检查点完全匹配，可由原提交入口回读已记录失败。' if failure
                            else '检查点完全匹配，可由原提交入口恢复结果，无需重新执行阶段。')
            elif receipt.get('used_at'):
                item.update(diagnosis='RECONCILIATION_REQUIRED', checkpoint_matches=True,
                            recovery_mode='NONE', reason='未执行检查点与已消费回执冲突，需核对正式证据。')
            else:
                expires = datetime.fromisoformat(str(receipt['expires_at']))
                if expires.tzinfo is None:
                    raise ValueError('receipt_expiry_timezone_missing')
                expired = expires <= datetime.now().astimezone()
                state = json.loads((project_dir / 'workflow-state.json').read_text(encoding='utf-8'))
                if int(state.get('revision') or 0) != revision:
                    item.update(diagnosis='RECONCILIATION_REQUIRED', checkpoint_matches=True,
                                recovery_mode='NONE', reason='当前版本与原预检输入不同，禁止重跑。')
                else:
                    item.update(diagnosis='EXPIRED_UNEXECUTED' if expired else 'RETRY_SAFE_UNCHANGED',
                                checkpoint_matches=True,
                                recovery_mode='ORIGINAL_TOKEN_ABORT' if expired else 'ORIGINAL_TOKEN_RETRY',
                                reason='未发现正式写入，原入口可将过期操作标记终止；之后重新预检。' if expired
                                else '完整检查点未变化，原提交可安全重试；仍须检查用户暂停和执行授权。')
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            item.update(diagnosis='EVIDENCE_INVALID', recovery_mode='NONE',
                        reason='恢复记录、预检回执或检查点证据不可验证，保留阻塞并由平台核对。')
        results.append(item)
    return {'schema_version': 1, 'writes_performed': False,
            'automatic_recovery_authorized': False,
            'operations': results,
            'requires_reconciliation': any(item['recovery_mode'] == 'NONE' for item in results)}
