from __future__ import annotations

import json

import pytest

from scripts.run_protege_build_stage import (
    ConstructionLeaseTimeout,
    construction_lease,
    normalize_validation_reports,
    ontology_annotation_payloads,
    ontology_annotation_summary,
    quarantine_loaded_build_target,
    verify_exported_data_property_ranges,
)

VALID_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <urn:test:> .
ex:ThingShape a sh:NodeShape ; sh:targetClass ex:Thing .
"""


def _valid_tool_results() -> dict:
    return {
        "run_reasoner": {
            "started": True,
            "completed": True,
            "classification_failed": False,
            "reasoner": "HermiT",
            "status": "INITIALIZED",
            "inconsistent": False,
            "unsatisfiable_count": 0,
        },
        "unsatisfiable": {"count": 0, "items": [], "coherent": True},
        "validate_ontology": {
            "reasoner": {
                "status": "INITIALIZED",
                "results_available": True,
                "consistent": True,
                "unsatisfiable_count": 0,
            }
        },
        "shacl_schema": {"conforms": True, "violations": 0, "results": []},
    }


def test_normalize_validation_reports_emits_s5_gate_contract() -> None:
    reasoner, shacl = normalize_validation_reports(_valid_tool_results(), VALID_SHAPES)

    assert reasoner == {
        "status": "CONSISTENT",
        "consistent": True,
        "inconsistent": False,
        "reasoner": "HermiT",
        "unsatisfiable_count": 0,
        "run_status": "INITIALIZED",
        "validation_status": "INITIALIZED",
    }
    assert shacl["conforms"] is True
    assert shacl["node_shape_count"] == 1


def test_normalize_validation_reports_rejects_inconsistent_reasoner() -> None:
    results = _valid_tool_results()
    results["run_reasoner"]["inconsistent"] = True

    with pytest.raises(RuntimeError, match="HermiT"):
        normalize_validation_reports(results, VALID_SHAPES)


def test_normalize_validation_reports_rejects_missing_node_shape() -> None:
    with pytest.raises(RuntimeError, match="NodeShape"):
        normalize_validation_reports(_valid_tool_results(), "@prefix ex: <urn:test:> .")


def test_ontology_annotations_are_detailed_and_live_readable() -> None:
    design = {
        "title_zh": "大学校园知识图谱 v3",
        "comment_zh": "统一高校教学、人员、组织、选课和科研语义。",
        "version": "3.0.0",
        "classes": [{"name": "Student"}] * 45,
        "object_properties": [{"name": "studiesIn"}] * 23,
        "data_properties": [{"name": "studentId"}] * 18,
    }

    payloads = ontology_annotation_payloads(design)
    comment = next(item for item in payloads if item["property"] == "rdfs:comment")
    assert "45 个业务类" in comment["value"]
    assert "23 个对象属性" in comment["value"]
    assert "18 个数据属性" in comment["value"]
    assert "统一检查" in comment["value"]
    assert len(payloads) == 7

    context = {
        "ontology_annotations": [
            {
                "property_iri": "http://www.w3.org/2000/01/rdf-schema#comment",
                "value": comment["value"],
                "lang": "zh",
            }
        ]
    }
    summary = ontology_annotation_summary(context, {"ontology_annotations": 7})
    assert summary["count"] == 7
    assert summary["comment_zh"] == comment["value"]


def test_quarantine_loaded_build_target_frees_exact_version_iri() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = []

        def tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"name": name, **arguments}

    client = FakeClient()
    receipt = quarantine_loaded_build_target(
        client,
        ontology_iri="https://example.com/fraud",
        version="0.2.0",
        run_id="protege-test",
    )

    assert receipt["status"] == "QUARANTINED"
    assert client.calls[0] == (
        "set_active_ontology",
        {"ontology_iri": "https://example.com/fraud/0.2.0"},
    )
    assert client.calls[1][0] == "set_ontology_id"
    assert ".orion-stale/protege-test" in client.calls[1][1]["ontology_iri"]


def test_verify_exported_data_property_ranges_fails_on_stale_model(tmp_path) -> None:
    ttl = tmp_path / "ontology.ttl"
    ttl.write_text(
        """
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        <https://example.com/balance> a owl:DatatypeProperty ; rdfs:range xsd:string .
        """,
        encoding="utf-8",
    )
    design = {
        "data_properties": [
            {
                "iri": "https://example.com/balance",
                "range": "http://www.w3.org/2001/XMLSchema#decimal",
            }
        ]
    }

    with pytest.raises(RuntimeError, match="range 与 S4 施工图不一致"):
        verify_exported_data_property_ranges(ttl, design)


def test_verify_exported_data_property_ranges_accepts_exact_ranges(tmp_path) -> None:
    ttl = tmp_path / "ontology.ttl"
    ttl.write_text(
        """
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        <https://example.com/balance> a owl:DatatypeProperty ; rdfs:range xsd:decimal .
        """,
        encoding="utf-8",
    )
    design = {
        "data_properties": [
            {
                "iri": "https://example.com/balance",
                "range": "http://www.w3.org/2001/XMLSchema#decimal",
            }
        ]
    }

    receipt = verify_exported_data_property_ranges(ttl, design)
    assert receipt == {
        "status": "PASSED",
        "checked_data_properties": 1,
        "mismatches": [],
    }


def test_construction_lease_records_owner_and_releases(tmp_path) -> None:
    path = tmp_path / "protege-construction.lock"

    with construction_lease(
        path,
        owner="session-a",
        project_id="project-a",
        job_id="job-a",
        timeout_seconds=0,
    ) as lease:
        active = json.loads(path.read_text(encoding="utf-8"))
        assert active["status"] == "ACTIVE"
        assert active["owner"] == "session-a"
        assert active["project_id"] == "project-a"
        assert active["job_id"] == "job-a"
        assert active["pid"] > 1
        assert lease == active

    released = json.loads(path.read_text(encoding="utf-8"))
    assert lease["status"] == "RELEASED"
    assert released["status"] == "RELEASED"
    assert released["released_at"]


def test_construction_lease_times_out_without_writing(tmp_path) -> None:
    path = tmp_path / "protege-construction.lock"

    with construction_lease(
        path,
        owner="session-a",
        project_id="project-a",
        job_id="job-a",
        timeout_seconds=0,
    ):
        with (
            pytest.raises(ConstructionLeaseTimeout, match="本次 S5 未执行任何写入"),
            construction_lease(
                path,
                owner="session-b",
                project_id="project-b",
                job_id="job-b",
                timeout_seconds=0.02,
                poll_seconds=0.005,
            ),
        ):
            pytest.fail("contending lease must not be acquired")
        active = json.loads(path.read_text(encoding="utf-8"))
        assert active["owner"] == "session-a"
        assert active["status"] == "ACTIVE"


def test_construction_lease_is_reusable_after_owner_error(tmp_path) -> None:
    path = tmp_path / "protege-construction.lock"

    with (
        pytest.raises(ValueError, match="simulated failure"),
        construction_lease(
            path,
            owner="session-a",
            project_id="project-a",
            job_id="job-a",
            timeout_seconds=0,
        ),
    ):
        raise ValueError("simulated failure")

    failed = json.loads(path.read_text(encoding="utf-8"))
    assert failed["status"] == "RELEASED_AFTER_ERROR"
    assert failed["error_type"] == "ValueError"

    with construction_lease(
        path,
        owner="session-b",
        project_id="project-b",
        job_id="job-b",
        timeout_seconds=0,
    ):
        assert json.loads(path.read_text(encoding="utf-8"))["owner"] == "session-b"


def test_export_validator_rejects_double_type_even_when_range_matches(tmp_path):
    ttl = tmp_path / "ontology.ttl"
    ttl.write_text('''
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
    <urn:test:count> a owl:DatatypeProperty, owl:ObjectProperty; rdfs:range xsd:integer .
    ''')
    with pytest.raises(RuntimeError, match="PROPERTY_TYPE_COLLISION"):
        verify_exported_data_property_ranges(ttl, {"data_properties": [{"iri": "urn:test:count", "range": "xsd:integer"}]})
