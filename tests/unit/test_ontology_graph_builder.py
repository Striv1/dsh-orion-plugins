from pathlib import Path

from scripts.build_ontology_graph import build


def test_graph_exposes_classes_properties_and_instances_as_real_nodes(
    tmp_path: Path,
) -> None:
    (tmp_path / "05-ontology-build").mkdir()
    (tmp_path / "04-ontology-design").mkdir()
    (tmp_path / "02-semantic-recognition").mkdir()
    (tmp_path / "05-ontology-build" / "ontology.ttl").write_text(
        """
        @prefix ex: <https://example.org/onto#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        ex:Product a owl:Class .
        ex:Station a owl:Class .
        ex:processedAt a owl:ObjectProperty, owl:FunctionalProperty ; rdfs:domain ex:Product ; rdfs:range ex:Station .
        ex:serialNumber a owl:DatatypeProperty ; rdfs:domain ex:Product ; rdfs:range xsd:string .
        ex:product-1 a owl:NamedIndividual, ex:Product ;
          ex:serialNumber "A-001" ; ex:processedAt ex:station-1 .
        ex:station-1 a owl:NamedIndividual, ex:Station .
        """,
        encoding="utf-8",
    )

    payload = build(tmp_path)
    nodes = {node["id"]: node for node in payload["nodes"]}
    edges = payload["edges"]

    assert {node["kind"] for node in nodes.values()} == {
        "class",
        "objectProperty",
        "dataProperty",
        "individual",
    }
    assert nodes["https://example.org/onto#processedAt"]["kind"] == "objectProperty"
    assert nodes["https://example.org/onto#serialNumber"]["kind"] == "dataProperty"
    assert nodes["https://example.org/onto#Product"]["schemaAttributes"] == [
        {"property": "serialNumber", "value": "string"}
    ]
    schema_edge = next(
        edge
        for edge in edges
        if edge["source"] == "https://example.org/onto#Product"
        and edge["target"] == "https://example.org/onto#Station"
        and edge["label"] == "processedAt"
        and edge["kind"] == "schema"
    )
    assert schema_edge["cardinality"] == "N:1"
    assert schema_edge["cardinalityBasis"] == "OWL 约束"
    relationship_edge = next(
        edge
        for edge in edges
        if edge["source"] == "https://example.org/onto#product-1"
        and edge["target"] == "https://example.org/onto#station-1"
        and edge["kind"] == "relationship"
    )
    assert relationship_edge["cardinality"] == "N:1"
    assert relationship_edge["cardinalityBasis"] == "OWL 约束"
    assert any(
        edge["source"] == "https://example.org/onto#Product"
        and edge["target"] == "https://example.org/onto#Station"
        and edge["label"] == "processedAt"
        and edge["kind"] == "schema"
        for edge in edges
    )
    assert any(
        edge["source"] == "https://example.org/onto#processedAt"
        and edge["target"] == "https://example.org/onto#Product"
        and edge["kind"] == "domain"
        for edge in edges
    )
    assert any(
        edge["source"] == "https://example.org/onto#processedAt"
        and edge["target"] == "https://example.org/onto#Station"
        and edge["kind"] == "range"
        for edge in edges
    )
    assert any(
        edge["source"] == "https://example.org/onto#serialNumber"
        and edge["target"] == "https://example.org/onto#Product"
        and edge["kind"] == "domain"
        for edge in edges
    )
    assert any(
        edge["source"] == "https://example.org/onto#product-1"
        and edge["target"] == "https://example.org/onto#station-1"
        and edge["kind"] == "relationship"
        for edge in edges
    )
    assert payload["stats"]["objectProperty"] == 1
    assert payload["stats"]["dataProperty"] == 1


def _published_instance_project(tmp_path, *, formal=True, nt=False, count=1):
    import hashlib
    import json

    root = tmp_path / "example-project"
    model = root / "05-ontology-build/ontology.ttl"
    model.parent.mkdir(parents=True)
    model.write_text(
        "@prefix ex: <https://example.org/onto#> . @prefix owl: <http://www.w3.org/2002/07/owl#> . ex:Product a owl:Class . ex:name a owl:DatatypeProperty ."
    )
    relative = ("06-quality-validation" if formal else "05-ontology-build") + "/materialized.ttl"
    materialized = root / relative
    materialized.parent.mkdir(exist_ok=True)
    materialized.write_text(
        "".join(
            f'<https://example.org/onto#product-{i}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://example.org/onto#Product> .\n<https://example.org/onto#product-{i}> <https://example.org/onto#name> "real-{i}" .\n'
            for i in range(count)
        )
    )
    state = {
        "project_id": root.name,
        "project_status": "PUBLISHED",
        "stage_statuses": {"S6": "PASSED"},
    }
    (root / "workflow-state.json").write_text(json.dumps(state))
    publication = root / "07-release/publication.json"
    publication.parent.mkdir()
    publication.write_text(json.dumps({"project_id": root.name, "release_version": "0.1.0"}))
    (root / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "project_id": root.name,
                "files": [
                    {
                        "path": relative,
                        "sha256": "sha256:" + hashlib.sha256(materialized.read_bytes()).hexdigest(),
                        "lifecycle_status": "CURRENT",
                    },
                    {
                        "path": "05-ontology-build/ontology.ttl",
                        "sha256": "sha256:" + hashlib.sha256(model.read_bytes()).hexdigest(),
                        "lifecycle_status": "CURRENT",
                    },
                ],
            }
        )
    )
    if nt:
        report = root / "06-quality-validation/semantica-report.json"
        report.parent.mkdir(exist_ok=True)
        report.write_text(json.dumps({"materialized_format": "nt"}))
    return root, materialized


def test_rdf_descriptions_prefer_chinese_and_keep_class_attribute_details(tmp_path):
    model = tmp_path / "05-ontology-build/ontology.ttl"
    model.parent.mkdir()
    model.write_text(
        """
        @prefix ex: <https://example.org/onto#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        ex:Product a owl:Class ;
            rdfs:comment "English product description"@en ;
            skos:definition "业务产品的定义"@zh-CN .
        ex:Station a owl:Class ; rdfs:comment "工位说明"@zh .
        ex:Undocumented a owl:Class .
        ex:processedAt a owl:ObjectProperty ;
            rdfs:domain ex:Product ; rdfs:range ex:Station ;
            rdfs:comment "加工使用的工位"@zh, "Processing station"@en .
        ex:serialNumber a owl:DatatypeProperty ;
            rdfs:label "序列号"@zh ; skos:definition "产品序列号" ;
            rdfs:domain ex:Product ; rdfs:range xsd:string .
        ex:product-1 a ex:Product ; ex:processedAt ex:station-1 .
        ex:station-1 a ex:Station .
        """,
        encoding="utf-8",
    )

    payload = build(tmp_path)
    nodes = {node["id"].rsplit("#", 1)[-1]: node for node in payload["nodes"]}
    assert nodes["Product"]["summary"] == "业务产品的定义"
    assert nodes["Station"]["summary"] == "工位说明"
    assert nodes["serialNumber"]["summary"] == "产品序列号"
    assert "summary" not in nodes["Undocumented"]
    assert nodes["Product"]["schemaAttributes"] == [
        {"property": "序列号", "value": "string"}
    ]
    relations = [edge for edge in payload["edges"] if edge["kind"] in {"schema", "relationship"}]
    assert {edge["kind"] for edge in relations} == {"schema", "relationship"}
    assert all(edge["summary"] == "加工使用的工位" for edge in relations)
    assert all("summary" not in edge for edge in payload["edges"] if edge["kind"] == "domain")


def test_formal_s6_instances_take_priority_over_legacy_s5(tmp_path):
    root, _ = _published_instance_project(tmp_path)
    (root / "05-ontology-build/materialized.ttl").write_text(
        "<https://example.org/onto#stale> a <https://example.org/onto#Product> ."
    )
    payload = build(root)
    assert payload["stats"]["individual"] == 1
    assert payload["instance_preview"]["source_path"] == "06-quality-validation/materialized.ttl"
    assert all(node["id"] != "https://example.org/onto#stale" for node in payload["nodes"])
    assert (
        next(node for node in payload["nodes"] if node["kind"] == "individual")["attributes"][0][
            "value"
        ]
        == "real-0"
    )


def test_legacy_s5_instance_graph_remains_supported(tmp_path):
    root, _ = _published_instance_project(tmp_path, formal=False)
    payload = build(root)
    assert payload["stats"]["individual"] == 1
    assert payload["instance_preview"]["source_path"] == "05-ontology-build/materialized.ttl"


def test_no_instance_graph_keeps_model_available(tmp_path):
    root, graph = _published_instance_project(tmp_path)
    graph.unlink()
    payload = build(root)
    assert payload["stats"]["class"] == 1
    assert payload["instance_preview"]["status"] == "UNAVAILABLE"
    assert not payload["stats"].get("individual")


def test_nt_preview_is_bounded_without_full_graph_parse(tmp_path, monkeypatch):
    from rdflib import Graph

    root, graph = _published_instance_project(tmp_path, nt=True, count=251)
    original = Graph.parse

    def guarded(self, source=None, *args, **kwargs):
        assert str(source) != str(graph), "must not parse the full instance file into an RDF graph"
        return original(self, source, *args, **kwargs)

    monkeypatch.setattr(Graph, "parse", guarded)
    payload = build(root)
    assert payload["stats"]["individual"] == 200
    assert payload["instance_preview"]["selected_subject_count"] == 200
    assert payload["instance_preview"]["complete"] is False
    assert payload["instance_preview"]["preview_triple_count"] == 400


def test_changed_instance_bytes_are_not_displayed(tmp_path):
    root, graph = _published_instance_project(tmp_path)
    graph.write_text(
        graph.read_text()
        + "<https://example.org/onto#injected> a <https://example.org/onto#Product> ."
    )
    payload = build(root)
    assert not payload["stats"].get("individual")
    assert "checksum" in payload["instance_preview"]["reason"]


def test_invalidated_revision_instances_are_not_displayed(tmp_path):
    import json

    root, _ = _published_instance_project(tmp_path)
    state_path = root / "workflow-state.json"
    state = json.loads(state_path.read_text())
    state["stage_statuses"]["S6"] = "INVALIDATED"
    state_path.write_text(json.dumps(state))
    payload = build(root)
    assert not payload["stats"].get("individual")


def test_cross_project_instance_symlink_is_not_displayed(tmp_path):
    root, graph = _published_instance_project(tmp_path)
    outside = tmp_path / "other-project.ttl"
    graph.rename(outside)
    graph.symlink_to(outside)
    payload = build(root)
    assert not payload["stats"].get("individual")
    assert "outside" in payload["instance_preview"]["reason"]


def test_release_snapshot_must_match_current_s6_revision(tmp_path):
    import hashlib
    import json

    root, _ = _published_instance_project(tmp_path)
    package = "07-release/ontology-engineering-package-0.1.0"
    snapshot_path = root / package / "04-发布信息/release-snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot = {
        "project_id": root.name,
        "release_version": "0.1.0",
        "formal_stage_fingerprints": {
            s: {"verification_status": "VERIFIED", "verified_sha256": "sha256:old"}
            for s in ["S5", "S6"]
        },
    }
    snapshot_path.write_text(json.dumps(snapshot))
    pub_path = root / "07-release/publication.json"
    pub = json.loads(pub_path.read_text())
    pub.update(
        {
            "package_path": package,
            "release_snapshot_sha256": "sha256:"
            + hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        }
    )
    pub_path.write_text(json.dumps(pub))
    state_path = root / "workflow-state.json"
    state = json.loads(state_path.read_text())
    state["stage_fingerprints"] = {s: {"output": "sha256:old"} for s in ["S5", "S6"]}
    state_path.write_text(json.dumps(state))
    assert build(root)["stats"]["individual"] == 1
    state["stage_fingerprints"]["S6"]["output"] = "sha256:new"
    state_path.write_text(json.dumps(state))
    payload = build(root)
    assert not payload["stats"].get("individual")
    assert "revision" in payload["instance_preview"]["reason"]
