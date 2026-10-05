"""Bounded readback of immutable query results, never a new query execution."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from services.realtime_qa.evidence_receipts import EvidenceReceiptError, EvidenceReceiptStore
from services.realtime_qa.models import RealtimeReleaseExpectation


class ResultPageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(pattern=r'^session-[a-f0-9-]{36}$')
    expected_release: RealtimeReleaseExpectation
    receipt_id: str = Field(pattern=r'^EVD-[a-f0-9]{32}$')
    receipt_sha256: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    query_id: str = Field(min_length=1, max_length=128)
    source_question_id: str | None = Field(default=None, min_length=1, max_length=160)
    capability_name: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{1,63}$')
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)


def verify_live_release(binding: Any, expected: RealtimeReleaseExpectation) -> None:
    if any(getattr(binding, key) != value for key, value in expected.model_dump().items()):
        raise ValueError('receipt release is not the currently configured release')
    # Fixed server-owned paths only. Recheck withdrawal even if registry cache
    # has not been rebuilt since the release was withdrawn.
    package = Path(binding.package_path).resolve()
    release = package.parent
    if release.name != '07-release' or release.parent.name != binding.project_id:
        raise ValueError('current release path is not a formal project package')
    publication = json.loads((release/'publication.json').read_text())
    gates = json.loads((release/'gate-results.json').read_text())
    revoke = release/'release-revocation.json'
    if revoke.exists() and json.loads(revoke.read_text()).get('status') == 'REVOKED':
        raise ValueError('current release has been revoked')
    if (gates.get('stage') != 'S7' or gates.get('status') != 'PASSED'
        or publication.get('approval_decision') != 'APPROVED'
        or publication.get('integrity_verification_status') != 'PASSED'
        or publication.get('release_version') != expected.release_version
        or (release.parent / str(publication.get('package_path') or '')).resolve() != package
        or 'sha256:' + hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest() != expected.release_fingerprint):
        raise ValueError('current release is not published and integrity verified')


def result_page(store: EvidenceReceiptStore, runtime: Any, request: ResultPageRequest) -> dict[str, Any]:
    verify_live_release(runtime.binding, request.expected_release)
    reference = {**request.expected_release.model_dump(), 'session_id': request.session_id,
                 'query_id': request.query_id, 'receipt_id': request.receipt_id, 'sha256': request.receipt_sha256}
    envelope = json.loads(store.read(reference))
    bundle = envelope['response']['answer']['evidence_bundle']
    results = []
    for record in bundle.get('evidence') or []:
        payload = record.get('payload') or {}
        if record.get('source_kind') not in {'structured_db', 'document', 'reasoning'}:
            continue
        candidates = list(payload.get('cq_results') or [])
        if record.get('source_kind') == 'structured_db' and isinstance(payload.get('rows'), list):
            candidates.append({'capability_name': payload.get('query_template'), 'rows': payload['rows'],
                **{key: payload[key] for key in ('row_count', 'truncated', 'variables', 'row_terms') if key in payload},
                'rdf_lexical_rows': 'row_terms' in payload})
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get('rows'), list):
                continue
            if request.source_question_id and candidate.get('source_question_id') != request.source_question_id:
                continue
            if request.capability_name and candidate.get('capability_name') != request.capability_name:
                continue
            results.append({'source_question_id': candidate.get('source_question_id'),
                'capability_name': candidate.get('capability_name'), 'rows': candidate['rows'],
                'source_kind': record['source_kind'], 'source_ref': record.get('source_ref'),
                'evidence_id': record.get('evidence_id'),
                'query_sha256': candidate.get('query_sha256'), 'fact_sha256': candidate.get('fact_sha256'),
                'reported_row_count': candidate.get('row_count'),
                'source_refs': candidate.get('source_refs') or [], 'answer_scope_zh': candidate.get('answer_scope_zh'),
                **{key: candidate[key] for key in ('semantic_status', 'truncated', 'variables', 'row_terms', 'rdf_lexical_rows') if key in candidate}})
    if not results:
        raise ValueError('no executed result matches the requested receipt filters')
    if len(results) > 1:
        return {'session_id': request.session_id, 'expected_release': request.expected_release.model_dump(),
            'release_match': 'verified', 'receipt_id': request.receipt_id, 'receipt_sha256': request.receipt_sha256,
            'query_id': request.query_id, 'selection_required': True,
            'results': [{key: value for key, value in item.items() if key not in {'rows', 'row_terms'}} | {'total': len(item['rows'])}
                        for item in results[:30]], 'result_count': len(results)}
    selected = results[0]
    rows = selected.pop('rows')
    row_terms = selected.pop('row_terms', None)
    if row_terms is not None and (not isinstance(row_terms, list) or len(row_terms) != len(rows)):
        raise ValueError('SELECT result and RDF metadata row counts disagree')
    page, byte_count = [], 0
    for row in rows[request.offset:request.offset+request.limit]:
        # Preserve full scalar rows and associations; never silently truncate
        # values. Stop at a byte budget and let the next offset continue.
        size = len(json.dumps(row, ensure_ascii=False).encode())
        if row_terms is not None:
            size += len(json.dumps(row_terms[request.offset + len(page)], ensure_ascii=False).encode())
        if size > 100_000:
            raise ValueError('a result row exceeds the bounded page size')
        if byte_count + size > 100_000:
            break
        page.append(row)
        byte_count += size
    next_offset = request.offset + len(page)
    return {'session_id': request.session_id, 'expected_release': request.expected_release.model_dump(),
        'release_match': 'verified', 'receipt_id': request.receipt_id, 'receipt_sha256': request.receipt_sha256,
        'query_id': request.query_id, 'selection_required': False, **selected, 'rows': page,
        'total': len(rows), 'offset': request.offset, 'returned': len(page),
        **({'row_terms': row_terms[request.offset:next_offset]} if row_terms is not None else {}),
        'next_offset': next_offset if next_offset < len(rows) else None,
        'bundle_complete': bundle.get('complete') is True, 'scope': 'IMMUTABLE_EXECUTED_RESULT_ROWS',
        'query_reexecuted': False}


def register_result_page_api(app: FastAPI, store: EvidenceReceiptStore, resolve_runtime: Any) -> None:
    @app.post('/ontology/realtime/session-result-page')
    def read_result_page(request: ResultPageRequest) -> dict[str, Any]:
        try:
            return result_page(store, resolve_runtime(request.expected_release.project_id), request)
        except EvidenceReceiptError as exc:
            raise HTTPException(404, 'receipt identity or integrity does not match') from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
