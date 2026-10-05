"""Immutable, version-bound RDF snapshot indexes for business instance exploration.

Source files are never changed. Queries open SQLite read-only and must provide the
scope supplied at registration. A model alone is never a substitute for instances.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.plugins.parsers.ntriples import W3CNTriplesParser
from rdflib.store import Store

RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
RDFS_LABEL = 'http://www.w3.org/2000/01/rdf-schema#label'
OWL_CLASS = 'http://www.w3.org/2002/07/owl#Class'
RDFS_CLASS = 'http://www.w3.org/2000/01/rdf-schema#Class'
SCHEMA_VERSION = 1


class InstanceScopeError(ValueError):
    """Missing or mismatched registered instance scope (fail closed)."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _term(term):
    return '_:' + str(term) if isinstance(term, BNode) else str(term)


class _SQLiteSink(Store):
    context_aware = False
    formula_aware = False

    def __init__(self, connection, source):
        super().__init__()
        self.connection, self.source = connection, source

    def triple(self, subject, predicate, obj):
        literal = isinstance(obj, Literal)
        self.connection.execute(
            'INSERT OR IGNORE INTO triples VALUES (?,?,?,?,?,?,?)',
            (self.source, _term(subject), str(predicate), _term(obj),
             'literal' if literal else 'iri' if isinstance(obj, URIRef) else 'bnode',
             str(obj.datatype or '') if literal else '', str(obj.language or '') if literal else ''),
        )

    def add(self, triple, context, quoted=False):
        self.triple(*triple)


def _parse(connection, path, source):
    # Materializations are often N-Triples despite the .ttl extension. Neither
    # parser accumulates a Python graph: both feed the SQLite-backed sink.
    sink = _SQLiteSink(connection, source)
    connection.execute('SAVEPOINT parse_rdf')
    try:
        with path.open('r', encoding='utf-8') as stream:
            W3CNTriplesParser(sink=sink).parse(stream)
    except Exception:
        connection.execute('ROLLBACK TO parse_rdf')
        Graph(store=sink).parse(path, format='turtle')
    finally:
        connection.execute('RELEASE parse_rdf')


def ensure_instance_index(model_path, snapshot_path, index_dir, project_id,
                          release_version, snapshot_sha256=None, model_sha256=None,
                          ontology_uri=None):
    """Build/reuse a fingerprint-addressed index; return JSON registration metadata.

    snapshot_id is the full snapshot sha256. Expected hashes, when supplied, must
    match the source bytes. ontology_uri must be a declared model ontology IRI.
    """
    if not project_id or not release_version:
        raise InstanceScopeError('project_id and release_version are required')
    model_path, snapshot_path = Path(model_path).resolve(), Path(snapshot_path).resolve()
    actual_model, actual_snapshot = _hash(model_path), _hash(snapshot_path)
    if model_sha256 and model_sha256 != actual_model:
        raise InstanceScopeError('model fingerprint mismatch')
    if snapshot_sha256 and snapshot_sha256 != actual_snapshot:
        raise InstanceScopeError('snapshot fingerprint mismatch')
    scope = dict(project_id=str(project_id), release_version=str(release_version),
                 model_sha256=actual_model, snapshot_sha256=actual_snapshot,
                 snapshot_id=actual_snapshot, ontology_uri=str(ontology_uri or ''),
                 schema_version=SCHEMA_VERSION, data_mode='s6_snapshot')
    identity = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    directory = Path(index_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (identity + '.sqlite')
    import fcntl
    with (directory / (identity + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            return InstanceExplorer(destination, project_id, release_version, actual_snapshot,
                                    actual_model, ontology_uri).metadata
        fd, temporary = tempfile.mkstemp(prefix=identity + '.', suffix='.tmp', dir=directory)
        os.close(fd)
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript('''
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE triples (source TEXT, s TEXT, p TEXT, o TEXT, kind TEXT,
                  datatype TEXT, lang TEXT, PRIMARY KEY(source,s,p,o,kind,datatype,lang)) WITHOUT ROWID;
            ''')
            _parse(connection, model_path, 'model')
            if ontology_uri and not connection.execute(
                'SELECT 1 FROM triples WHERE source=? AND s=? AND p=? AND o=?',
                ('model', ontology_uri, RDF_TYPE, 'http://www.w3.org/2002/07/owl#Ontology')
            ).fetchone():
                raise InstanceScopeError('ontology_uri is not declared by model')
            _parse(connection, snapshot_path, 'snapshot')
            # Detect source replacement while parsing; never register mixed bytes.
            if _hash(model_path) != actual_model or _hash(snapshot_path) != actual_snapshot:
                raise InstanceScopeError('source changed during indexing')
            connection.executescript('''
                CREATE INDEX triple_predicate ON triples(source,p,o,s);
                CREATE INDEX triple_object ON triples(source,o,s,p);
                CREATE TABLE types(class_iri TEXT, iri TEXT, PRIMARY KEY(class_iri,iri)) WITHOUT ROWID;
                CREATE TABLE resources(iri TEXT PRIMARY KEY, label TEXT NOT NULL) WITHOUT ROWID;
            ''')
            connection.execute('INSERT OR IGNORE INTO types SELECT o,s FROM triples WHERE source=? AND p=? AND kind=?',
                               ('snapshot', RDF_TYPE, 'iri'))
            # Label preference: explicit labels, SKOS prefLabel, then common
            # business identity/name fields; deterministic within each priority.
            connection.execute('''INSERT INTO resources
                SELECT s, o FROM (
                  SELECT s,o, ROW_NUMBER() OVER (PARTITION BY s ORDER BY
                    CASE WHEN p=? AND lang LIKE 'zh%' THEN 0 WHEN p=? THEN 1
                    WHEN p LIKE '%prefLabel' THEN 2 ELSE 3 END, p, o) AS rank
                  FROM triples WHERE kind='literal' AND
                    (p=? OR p LIKE '%prefLabel' OR lower(p) LIKE '%name'
                     OR lower(p) LIKE '%code' OR lower(p) LIKE '%batchno'
                     OR lower(p) LIKE '%identifier')) WHERE rank=1''',
                               (RDFS_LABEL, RDFS_LABEL, RDFS_LABEL))
            scope['counts'] = dict(
                triple_count=connection.execute("SELECT COUNT(*) FROM triples WHERE source='snapshot'").fetchone()[0],
                instance_count=connection.execute('SELECT COUNT(DISTINCT iri) FROM types').fetchone()[0],
                class_count=connection.execute('SELECT COUNT(DISTINCT class_iri) FROM types').fetchone()[0],
                declared_class_count=connection.execute("SELECT COUNT(DISTINCT s) FROM triples WHERE source='model' AND p=? AND o IN (?,?) AND substr(s,1,2)!='_:'", (RDF_TYPE, OWL_CLASS, RDFS_CLASS)).fetchone()[0])
            scope.update(index_path=str(destination), model_path=str(model_path),
                         snapshot_path=str(snapshot_path))
            connection.executemany('INSERT INTO metadata VALUES (?,?)',
                                   [(key, json.dumps(value, ensure_ascii=False)) for key, value in scope.items()])
            connection.commit()
            connection.close()
            os.replace(temporary, destination)
        except BaseException:
            connection.close()
            Path(temporary).unlink(missing_ok=True)
            raise
    return scope


class InstanceExplorer:
    """Each public query reopens the registered database in read-only mode."""

    def __init__(self, index_path, project_id, release_version, snapshot_sha256,
                 model_sha256=None, ontology_uri=None):
        if not all((project_id, release_version, snapshot_sha256)):
            raise InstanceScopeError('complete project/release/snapshot scope is required')
        self.path = Path(index_path).resolve()
        with self._connection() as db:
            self.metadata = {row[0]: json.loads(row[1]) for row in db.execute('SELECT key,value FROM metadata')}
        expected = dict(project_id=project_id, release_version=release_version,
                        snapshot_sha256=snapshot_sha256, schema_version=SCHEMA_VERSION)
        if model_sha256 is not None:
            expected['model_sha256'] = model_sha256
        if ontology_uri is not None:
            expected['ontology_uri'] = ontology_uri
        for key, value in expected.items():
            if self.metadata.get(key) != value:
                raise InstanceScopeError('instance scope mismatch: ' + key)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _page(limit, offset=0):
        return max(1, min(int(limit), 200)), max(0, int(offset))

    def stats(self):
        return dict(scope=self.metadata, **self.metadata['counts'], membership_mode='asserted_direct')

    def classes(self):
        with self._connection() as db:
            rows = db.execute('''WITH classes AS (
                SELECT s AS iri FROM triples WHERE source='model' AND p=? AND o IN (?,?)
                UNION SELECT class_iri FROM types)
              SELECT c.iri,COALESCE(r.label,c.iri) AS label,
                (SELECT COUNT(*) FROM types t WHERE t.class_iri=c.iri) AS instance_count,
                EXISTS(SELECT 1 FROM triples m WHERE m.source='model' AND m.s=c.iri
                  AND m.p=? AND m.o IN (?,?)) AS declared_in_model
              FROM classes c LEFT JOIN resources r ON r.iri=c.iri
              WHERE substr(c.iri,1,2)!='_:' ORDER BY label,c.iri''', (RDF_TYPE, OWL_CLASS, RDFS_CLASS, RDF_TYPE, OWL_CLASS, RDFS_CLASS))
            return [dict(row, membership_mode='asserted_direct',
                         state='available' if row['instance_count'] else 'no_asserted_instances') for row in rows]

    def instances(self, class_iri, limit=50, offset=0, search=''):
        limit, offset = self._page(limit, offset)
        if not class_iri:
            raise ValueError('class_iri required')
        # Literal substring search: %, _ are not wildcard instructions.
        needle = str(search).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where = "t.class_iri=? AND (?='' OR t.iri LIKE ? ESCAPE '\\' OR r.label LIKE ? ESCAPE '\\')"
        args = (class_iri, search, '%' + needle + '%', '%' + needle + '%')
        with self._connection() as db:
            total = db.execute('SELECT COUNT(*) FROM types t LEFT JOIN resources r ON r.iri=t.iri WHERE ' + where, args).fetchone()[0]
            rows = db.execute('SELECT t.iri,COALESCE(r.label,t.iri) AS label FROM types t LEFT JOIN resources r ON r.iri=t.iri WHERE ' + where + ' ORDER BY t.iri LIMIT ? OFFSET ?', (*args, limit, offset))
            return dict(class_iri=class_iri, total=total, limit=limit, offset=offset,
                        items=[dict(row) for row in rows], membership_mode='asserted_direct',
                        data_mode='s6_snapshot')

    def node(self, iri, limit=100):
        limit, _ = self._page(limit)
        with self._connection() as db:
            exists = db.execute("SELECT 1 FROM triples WHERE source='snapshot' AND s=? LIMIT 1", (iri,)).fetchone()
            if not exists:
                exists = db.execute("SELECT 1 FROM triples WHERE source='snapshot' AND o=? AND kind!='literal' LIMIT 1", (iri,)).fetchone()
            if not exists:
                raise KeyError('instance does not exist in selected snapshot')
            outgoing = db.execute("""SELECT t.p AS predicate,t.o AS value,t.kind,t.datatype,t.lang,
                COALESCE(p.label,t.p) AS predicate_label,COALESCE(v.label,t.o) AS value_label
                FROM triples t LEFT JOIN resources p ON p.iri=t.p
                LEFT JOIN resources v ON v.iri=t.o AND t.kind!='literal'
                WHERE t.source='snapshot' AND t.s=? ORDER BY t.p,t.o LIMIT ?""", (iri, limit + 1)).fetchall()
            incoming = db.execute("""SELECT t.s AS iri,t.p AS predicate,
                COALESCE(s.label,t.s) AS label,COALESCE(p.label,t.p) AS predicate_label
                FROM triples t LEFT JOIN resources s ON s.iri=t.s
                LEFT JOIN resources p ON p.iri=t.p
                WHERE t.source='snapshot' AND t.o=? AND t.kind!='literal'
                ORDER BY t.s,t.p LIMIT ?""", (iri, limit + 1)).fetchall()
            label = db.execute('SELECT label FROM resources WHERE iri=?', (iri,)).fetchone()
            return dict(iri=iri, label=label[0] if label else iri,
                        properties=[dict(row) for row in outgoing[:limit]],
                        incoming=[dict(row) for row in incoming[:limit]],
                        outgoing_truncated=len(outgoing)>limit, incoming_truncated=len(incoming)>limit,
                        data_mode='s6_snapshot')
