"""Admission checks must observe authority changes without registry reloads."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.realtime_qa.api import CoreRealtimeRuntimeProvider, create_core_realtime_app
from services.realtime_qa.evidence_receipts import EvidenceReceiptStore
from services.realtime_qa.runtime import (
    DocumentOnlyOntopClient,
    RealtimeEvidenceRuntime,
    RealtimeRuntimeRegistry,
    RealtimeRuntimeUnavailable,
)
from tests.unit.test_core_realtime_api import _Runtime
from tests.unit.test_realtime_evidence_receipts import request


def setup_runtime(tmp_path):
    service = _Runtime()
    release = tmp_path / 'project' / '07-release'
    release.mkdir(parents=True)
    publication = {
        'project_id': service.binding.project_id,
        'release_version': service.binding.release_version,
        'package_manifest_sha256': service.binding.release_fingerprint,
        'approval_decision': 'APPROVED', 'integrity_verification_status': 'PASSED',
    }
    (release / 'publication.json').write_text(json.dumps(publication))
    (release / 'gate-results.json').write_text(json.dumps({'stage': 'S7', 'status': 'PASSED'}))
    runtime = RealtimeEvidenceRuntime(
        binding=service.binding, ontop=DocumentOnlyOntopClient(service.binding.release_fingerprint),
        fuseki=None, current_registry=service.current_registry, service=service,
        project_dir=release.parent,
    )
    registry = RealtimeRuntimeRegistry.from_single(runtime)
    provider = SimpleNamespace(resolve=registry.resolve, catalog=registry.catalog, health=lambda: {})
    client = TestClient(create_core_realtime_app(provider, evidence_receipts=EvidenceReceiptStore(tmp_path / 'receipts')))
    return release, publication, runtime, registry, client


def test_hot_revocation_blocks_new_queries_and_capabilities_but_keeps_receipts(tmp_path):
    release, _, runtime, registry, client = setup_runtime(tmp_path)
    response = client.post('/ontology/realtime/session-answer', json=request())
    assert response.status_code == 200
    receipt = response.json()['evidence_receipt']
    assert registry.resolve() is runtime
    (release / 'release-revocation.json').write_text('{"status":"REVOKED"}')
    assert client.post('/ontology/realtime/session-answer', json=request()).status_code == 503
    assert client.get('/ontology/realtime/binding').status_code == 503
    assert client.post('/ontology/realtime/session-capability', json={
        'session_id': request()['session_id'], 'expected_release': request()['expected_release'],
        'kind': 'document', 'name': 'anything',
    }).status_code == 503
    catalog = client.get('/ontology/realtime/runtimes').json()
    assert catalog['runtimes'] == []
    assert 'revoked' in catalog['unavailable_projects'][runtime.binding.project_id]
    with pytest.raises(RealtimeRuntimeUnavailable, match='revoked'):
        runtime.collect(None)
    assert client.get('/ontology/realtime/evidence-receipts/' + receipt['receipt_id'], params=receipt).status_code == 200


@pytest.mark.parametrize('change', ['version', 'fingerprint', 'approval', 'gate', 'missing', 'invalid'])
def test_hot_authority_changes_fail_closed_without_rebinding(tmp_path, change):
    release, publication, runtime, registry, client = setup_runtime(tmp_path)
    original = runtime.binding
    if change in ('version', 'fingerprint', 'approval'):
        key = {'version':'release_version', 'fingerprint':'package_manifest_sha256', 'approval':'approval_decision'}[change]
        publication[key] = 'changed'
        (release / 'publication.json').write_text(json.dumps(publication))
    elif change == 'gate':
        (release / 'gate-results.json').write_text('{"stage":"S7","status":"FAILED"}')
    elif change == 'missing':
        (release / 'publication.json').unlink()
    else:
        (release / 'release-revocation.json').write_text('{')
    with pytest.raises(RealtimeRuntimeUnavailable):
        registry.resolve()
    assert runtime.binding is original
    assert client.post('/ontology/realtime/session-answer', json=request()).status_code == 503


def test_registry_config_failure_does_not_bypass_fresh_authority(tmp_path, monkeypatch):
    release, _, runtime, registry, _ = setup_runtime(tmp_path)
    monkeypatch.setattr('services.realtime_qa.api.build_realtime_runtime_registry', lambda **kwargs: registry)
    registry_path = tmp_path / 'registry.json'
    registry_path.write_text('{}')
    provider = CoreRealtimeRuntimeProvider(SimpleNamespace(
        realtime_runtime_registry_path=str(registry_path), database_url='', fuseki_url='', semantica_url='',
    ))
    assert provider.resolve(None) is runtime
    registry_path.unlink()
    assert provider.resolve(None) is runtime  # Last verified version is retained.
    (release / 'release-revocation.json').write_text('{"status":"REVOKED"}')
    with pytest.raises(RealtimeRuntimeUnavailable, match='revoked'):
        provider.resolve(None)
    assert provider.last_error
