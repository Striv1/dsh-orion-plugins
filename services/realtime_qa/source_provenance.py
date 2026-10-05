"""Release-owned source provenance, separate from query execution freshness."""
from __future__ import annotations

import re
from copy import deepcopy

SCHEMA_TRACE = '06-工程追溯/01-data-understanding/schema-snapshot.json'
SOURCE_TRACE = '06-工程追溯/01-data-understanding/source-understanding.json'
INVENTORY_TRACE = '06-工程追溯/01-data-understanding/datasource-inventory.json'


def _file_identity_trace(schema, understanding, *, project_id):
    """Join protected file identities, without creating an executable source schema.

    Inventory-only guarantees (READY, ownership, import time, access policy) are
    unavailable here. These rows are exclusively for the partial provenance view.
    """
    database = schema.get('database')
    observed = understanding.get('observed_sources') if isinstance(understanding, dict) else None
    if (not isinstance(database, str) or not database.strip()
            or not isinstance(observed, list) or not all(isinstance(o, dict) for o in observed)
            or ('project_id' in schema and schema['project_id'] != project_id)):
        return None
    rows = []
    logical_tables, physical_tables, dataset_sheets = set(), set(), set()
    dataset_sources = {}
    for table in schema['tables']:
        identity = re.fullmatch(r'dataset:(DS-[A-F0-9]{20}):sheet:(.+)', str(table.get('source_ref') or ''))
        if not identity:
            return None
        dataset_id, sheet = identity.groups()
        source_id = table.get('source_id')
        logical, physical = table.get('source_table'), table.get('physical_version_table')
        if (not isinstance(source_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{2,63}', source_id)
                or not isinstance(logical, str) or not re.fullmatch(r'current_[a-z0-9_]{1,55}', logical)
                or not isinstance(physical, str) or not re.fullmatch(r'ds_' + dataset_id[3:15].lower() + r'_\d{3}', physical)
                or table.get('dataset_id') != dataset_id or table.get('source_sheet') != sheet
                or table.get('schema') != 'orion_data'
                or (table.get('name') or table.get('table')) not in (logical, physical)
                or any(table[key] not in (logical, physical) for key in ('name', 'table') if key in table)
                or (source_id, logical) in logical_tables or physical in physical_tables
                or (dataset_id, sheet) in dataset_sheets
                or dataset_sources.setdefault(dataset_id, source_id) != source_id):
            return None
        matches = [item for item in observed if item.get('dataset_id') == dataset_id]
        if len(matches) != 1:
            return None
        origin = matches[0]
        document_id, sha = origin.get('document_id'), origin.get('source_sha256')
        if (origin.get('kind') != 'STRUCTURED_FILE'
                or not isinstance(document_id, str) or not document_id.strip()
                or not isinstance(sha, str) or not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', sha)
                or any(origin[key] != value for key, value in (
                    ('source_id', source_id), ('database', database), ('project_id', project_id),
                ) if key in origin)
                or ('document_id' in table and table['document_id'] != document_id)
                or ('source_sha256' in table and str(table['source_sha256']).removeprefix('sha256:') != sha.removeprefix('sha256:'))):
            return None
        logical_tables.add((source_id, logical))
        physical_tables.add(physical)
        dataset_sheets.add((dataset_id, sheet))
        rows.append({
            'source_id': source_id, 'source_table': logical, 'name': physical, 'dataset_id': dataset_id,
            'file_import': {'document_id': document_id, 'source_sheet': sheet,
                            'source_sha256': 'sha256:' + sha.removeprefix('sha256:')},
        })
    return {'database': database, 'tables': rows}


def protected_source_trace(schema, understanding, capabilities, checksums, *, inventory=None, project_id=None):
    """Project only unambiguous identities from already verified package files.

    This does not certify that a runtime executed against the S1 snapshot. In
    particular it never populates the binding's execution snapshot_set_id or
    creates a complete SourceSnapshotManifest from a schema inventory.
    """
    if SCHEMA_TRACE not in checksums or not isinstance(schema, dict):
        return {}
    if not isinstance(schema.get('tables'), list) or not all(isinstance(t, dict) for t in schema['tables']):
        return {}
    file_sources = any(t.get('physical_version_table') for t in schema.get('tables', []))
    if file_sources:
        if INVENTORY_TRACE in checksums:
            if not inventory or not project_id:
                return {}
            from services.structured_data.execution_source import execution_schema
            schema = execution_schema(schema, inventory, project_id=project_id)
        else:
            if SOURCE_TRACE not in checksums or not project_id:
                return {}
            schema = _file_identity_trace(schema, understanding, project_id=project_id)
            if schema is None:
                return {}
    elif not schema.get('snapshot_set_id'):
        return {}
    scoped = {}
    for cap in capabilities.values():
        for source_id, tables in (cap.get('source_tables_by_id') or {}).items():
            scoped.setdefault(source_id, set()).update(tables)
    sources = {}
    for source_id, tables in scoped.items():
        matched = []
        for table in sorted(tables):
            rows = [item for item in schema.get('tables', [])
                    if item.get('source_id') == source_id and item.get('source_table') == table]
            if len(rows) != 1 or not rows[0].get('dataset_id') or not rows[0].get('name'):
                break
            matched.append(rows[0])
        else:
            observed = [item for item in (understanding or {}).get('observed_sources', [])
                        if item.get('source_id') == source_id]
            location = observed[0] if len(observed) == 1 else {}
            datasets = sorted({item['dataset_id'] for item in matched})
            sources[source_id] = {
                'kind': 'STRUCTURED_FILE' if file_sources else location.get('kind') or 'UNKNOWN',
                'database': schema.get('database') if file_sources else location.get('database') or 'UNKNOWN',
                'dataset_ids': datasets,
                'dataset_id': datasets[0] if len(datasets) == 1 else None,
                'tables': [{key: row[key] for key in ('source_table', 'name', 'dataset_id')}
                           | ({'file_import': row['file_import']} if file_sources else {})
                           for row in matched],
            }
    return {
        'scope': 'PROTECTED_S1_TRACE_ONLY', 'snapshot_set_id': schema.get('snapshot_set_id'),
        'sources': sources,
        'artifacts': {path: checksums[path] for path in (SCHEMA_TRACE, SOURCE_TRACE, INVENTORY_TRACE) if path in checksums},
    }


def snapshot_contract_for_publication(source, inventory, schema, *, project_id):
    """Freeze formal S1 metadata only while building a new release package.

    Explicit reviewed runtime contracts win. Existing packages never call this
    function and can never be repaired by reading the mutable project inventory.
    Validation of the returned full contract stays with the native release gate.
    """
    if source.get('cross_source_snapshot_set') is not None or source.get('source_bindings'):
        return source
    snapshot = inventory.get('cross_source_snapshot_set')
    capabilities = source.get('query_capabilities') or {}
    if not snapshot or not any(cap.get('source_ids') and cap.get('query_mode') == 'SNAPSHOT_ONLY'
                               for cap in capabilities.values()):
        return source
    if (snapshot.get('project_id') != project_id
            or snapshot.get('snapshot_set_id') != schema.get('snapshot_set_id')):
        raise ValueError('S1 snapshot identity does not match the release project/schema')
    bindings = inventory.get('source_bindings') or []
    manifests = snapshot.get('source_snapshots') or []
    if (len({item['source_id'] for item in bindings}) != len(bindings)
            or len({item['source_id'] for item in manifests}) != len(manifests)):
        raise ValueError('S1 snapshot has ambiguous source identities')
    expected = {(item['source_id'], table['table'], table['target_table'], item['dataset_id'])
                for item in manifests for table in item['tables']}
    actual = [(item.get('source_id'), item.get('source_table'), item.get('name'), item.get('dataset_id'))
              for item in schema.get('tables', [])]
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError('S1 source/table/dataset identities do not match snapshot manifests')
    return {**source, 'cross_source_snapshot_set': deepcopy(snapshot),
            'source_bindings': {item['source_id']: deepcopy(item) for item in bindings},
            'snapshot_manifests': {item['source_id']: deepcopy(item) for item in manifests}}
