"""Connect immutable release snapshots and verify actual instance read-back."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def bare(value):
    return str(value or '').removeprefix('sha256:')


def verify(project_dir, model_binding=None):
    from services.ontology_engineering.workflow import OntologyWorkflowService
    project_dir = Path(project_dir).resolve()
    project_id = project_dir.name
    publication = read(project_dir / '07-release/publication.json')
    version = publication['release_version']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', version):
        raise ValueError('Invalid release version')
    if publication.get('project_id') != project_id or publication.get('approval_decision') != 'APPROVED':
        raise ValueError('Missing approved release identity')
    package = project_dir / '07-release' / ('ontology-engineering-package-' + version)
    manifest_path = package / 'manifest.json'
    manifest_hash = sha(manifest_path)
    if manifest_hash != bare(publication.get('package_manifest_sha256')):
        raise ValueError('Publication manifest fingerprint mismatch')
    manifest = read(manifest_path)
    if manifest.get('project_id') != project_id or manifest.get('release_version') != version:
        raise ValueError('Package identity mismatch')
    files = {item['path']: bare(item['sha256']) for item in manifest['files']}

    def verified_file(name):
        path = package / name
        if name not in files or sha(path) != files[name]:
            raise ValueError('Frozen package file fingerprint mismatch: ' + name)
        return path

    model = verified_file('01-本体模型/ontology.ttl')
    quality = read(verified_file('03-质量结论/quality-summary.json'))
    if quality.get('status') != 'PASSED':
        raise ValueError('Frozen S6 quality result is not PASSED')
    quality_dir = project_dir / '06-quality-validation'
    snapshot = quality_dir / 'materialized.ttl'
    snapshot_hash = sha(snapshot)
    evidence = {'kind': 'direct_snapshot_hash'}
    expected = bare(quality.get('validated_graph_sha256'))
    if expected:
        if snapshot_hash != expected:
            raise ValueError('Frozen instance snapshot fingerprint mismatch')
    else:
        frozen = read(verified_file('04-发布信息/release-snapshot.json'))
        stage = frozen.get('formal_stage_fingerprints', {}).get('S6', {})
        stage_hash = OntologyWorkflowService._stage_fingerprint(quality_dir)
        if (stage.get('stage_status') != 'PASSED' or stage.get('verification_status') != 'VERIFIED'
                or stage.get('profile') != 'formal-artifacts-v1'
                or stage.get('recorded_sha256') != stage_hash or stage.get('verified_sha256') != stage_hash):
            raise ValueError('Current S6 artifacts do not match the frozen release stage fingerprint')
        evidence = {'kind': 'frozen_s6_stage_fingerprint', 'stage_fingerprint': stage_hash,
                    'release_snapshot_sha256': files['04-发布信息/release-snapshot.json']}
    report_path = quality_dir / 'semantica-report.json'
    report = read(report_path)
    if report.get('status') != 'PASSED':
        raise ValueError('S6 instance validation did not pass')
    if report.get('validated_graph_sha256') and bare(report['validated_graph_sha256']) != snapshot_hash:
        raise ValueError('S6 instance validation fingerprint mismatch')
    receipt = model_binding if model_binding is not None else read(project_dir / '07-release/semantica-sync.json')
    if (receipt.get('status') != 'SYNCED' or receipt.get('registry_verified') is not True
            or receipt.get('project_id') != project_id or receipt.get('release_version') != version
            or bare(receipt.get('source_sha256')) != sha(model)
            or receipt.get('ontology_uri') != publication.get('ontology_iri')):
        raise ValueError('Existing Semantica model binding does not match the release')
    return {'project_id': project_id, 'release_version': version,
            'ontology_uri': receipt['ontology_uri'], 'model_sha256': sha(model),
            'snapshot_sha256': snapshot_hash, 'model_path': str(model),
            'snapshot_path': str(snapshot), 'package_manifest_sha256': manifest_hash,
            'evidence_mode': 'release_bound_snapshot', 'evidence': evidence,
            'stage_fingerprint': evidence.get('stage_fingerprint'),
            'materialization_scope': report.get('materialization_scope', 'S6_VALIDATED_SCOPE')}


def connect(project_dir, index_root, base_url, *, model_binding=None, transport=None):
    import httpx

    from services.ontology_engineering.instance_exploration import ensure_instance_index
    plan = verify(project_dir, model_binding)
    scope = {key: plan[key] for key in ('project_id', 'release_version', 'ontology_uri', 'model_sha256', 'snapshot_sha256')}
    index = ensure_instance_index(model_path=plan['model_path'], snapshot_path=plan['snapshot_path'],
                                  index_dir=index_root, **scope)
    # Recheck the evidence after indexing, before registering an immutable scope.
    if verify(project_dir, model_binding) != plan:
        raise ValueError('Release evidence changed during indexing')
    headers = {'X-API-Key': os.environ['SEMANTICA_API_KEY']} if os.getenv('SEMANTICA_API_KEY') else {}
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False, headers=headers, transport=transport) as client:
        response = client.post('/api/orion/exploration/register', json={**scope,
            'index_path': index['index_path'], 'evidence_mode': plan['evidence_mode'],
            'materialization_scope': plan['materialization_scope']})
        response.raise_for_status()
        response = client.get('/api/orion/exploration/stats', params=scope)
        response.raise_for_status()
        actual = response.json()
        if actual.get('scope') != scope or actual.get('stats', {}).get('scope', {}).get('counts') != index['counts']:
            raise ValueError('Registered snapshot read-back mismatch')
        response = client.get('/api/orion/exploration/classes', params=scope)
        response.raise_for_status()
        classes = response.json()
        selected = next((item for item in classes if item['instance_count'] > 0), None)
        checks = ['stats', 'classes']
        if selected:
            response = client.get('/api/orion/exploration/instances', params={**scope, 'class_iri': selected['iri'], 'limit': 1})
            response.raise_for_status()
            items = response.json()['items']
            if not items:
                raise ValueError('Nonempty class returned no instances')
            response = client.get('/api/orion/exploration/node', params={**scope, 'iri': items[0]['iri']})
            response.raise_for_status()
            checks.extend(['instances', 'node'])
    receipt = {**plan, 'schema_version': 1, 'status': 'VERIFIED', 'index_path': index['index_path'],
               'counts': index['counts'], 'readback_checks': checks,
               'verified_at': datetime.now(UTC).isoformat()}
    directory = Path(index_root) / 'connections'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (plan['project_id'] + '.json')
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    return receipt


