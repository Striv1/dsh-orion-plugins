import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.realtime_qa.analysis_assets import (
    CONFIRMATION,
    AnalysisAssetError,
    AnalysisAssetStore,
    result_digest,
)


@pytest.fixture
def scope():
    return {
        "project_id": "ontology-project-test", "release_version": "0.1.0",
        "release_fingerprint": "sha256:" + "a" * 64,
        "session_id": "session-11111111-1111-1111-1111-111111111111",
    }


def receipt(scope, character="b"):
    return {**scope, "receipt_id": "EVD-" + character * 32,
            "sha256": "sha256:" + "c" * 64, "query_id": "Q-WREN-test"}


def memory(store, scope, question="按图书分类统计逾期借阅数量"):
    return store.save_memory(scope, question, {"metrics": [{"operation": "count_rows"}]},
                             receipt(scope), confirmed=True)


def test_memory_roundtrip_requires_confirmation_and_never_approves_business(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path / "assets")
    for confirmation in (False, None, 1, "true"):
        with pytest.raises(AnalysisAssetError, match="confirmation"):
            store.save_memory(scope, "逾期借阅", {"sql": "SELECT 1"}, receipt(scope), confirmation)
    asset = memory(store, scope)
    assert asset["approval_status"] == CONFIRMATION
    assert asset["payload"]["execution_policy"] == "REFERENCE_ONLY_REVALIDATE_BEFORE_EXECUTION"
    assert AnalysisAssetStore(store.root).get(scope, asset["asset_id"], asset["sha256"]) == asset
    assert list(store.root.rglob("*.tmp")) == []
    assert store.list_assets(scope) == [asset]


@pytest.mark.parametrize("field,value", [
    ("project_id", "other-project"), ("release_version", "0.2.0"),
    ("release_fingerprint", "sha256:" + "d" * 64),
    ("session_id", "session-22222222-2222-2222-2222-222222222222"),
])
def test_assets_and_receipts_cannot_cross_any_identity_boundary(tmp_path, scope, field, value):
    store = AnalysisAssetStore(tmp_path)
    asset = memory(store, scope)
    other = {**scope, field: value}
    assert store.list_assets(other) == []
    assert store.search_memories(other, "逾期借阅") == []
    with pytest.raises(AnalysisAssetError):
        store.get(other, asset["asset_id"])
    with pytest.raises(AnalysisAssetError, match="receipt"):
        store.save_memory(other, "逾期借阅", {"sql": "SELECT 1"}, receipt(scope), True)


def test_chinese_matching_explains_real_lexical_overlap_without_embedding_claim(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path)
    expected = memory(store, scope)
    memory(store, scope, "每月收入的增长情况")
    matches = store.search_memories(scope, "统计逾期借阅数量")
    assert len(matches) == 1
    assert matches[0]["asset"]["asset_id"] == expected["asset_id"]
    assert {"逾期", "借阅", "数量"} <= set(matches[0]["matched_terms"])
    assert matches[0]["semantic_embedding"] is False
    assert 0 < matches[0]["score"] <= 1
    assert store.search_memories(scope, "🚢") == []
    store.save_knowledge(scope, "逾期天数口径", "缺少到期日时标为未知，不能记为零。", [receipt(scope)], True)
    knowledge = store.search_knowledge(scope, "缺少到期日怎么办")
    assert knowledge[0]["asset"]["kind"] == "knowledge"
    assert knowledge[0]["asset"]["approval_status"] == CONFIRMATION


def test_report_pins_distinct_verified_reference_shapes_and_preserves_layout(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path)
    layout = {"blocks": [{"receipt_id": receipt(scope)["receipt_id"], "view": "line"}]}
    report = store.save_report(scope, "借阅月报", [receipt(scope)], layout)
    layout["blocks"][0]["view"] = "bar"
    assert store.get(scope, report["asset_id"])["payload"]["layout"]["blocks"][0]["view"] == "line"
    assert len(store.list_assets(scope, "report")) == 1
    for refs in ([], [receipt(scope), receipt(scope)], [receipt(scope)] * 51):
        with pytest.raises(AnalysisAssetError):
            store.save_report(scope, "报告", refs, {"view": "table"})


def test_evaluation_preserves_null_types_precision_order_and_checks_hash_and_count(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path)
    basis = {"kind": "FIXED_ACCEPTANCE_DATASET", "dataset_id": "DS-test", "version": "v1", "sha256": "sha256:" + "f" * 64}
    rows = [{"id": 1, "days": None, "amount": "12345678901234567890.001"},
            {"id": 2, "days": 0, "amount": "1.1"}]
    asset = store.save_evaluation(scope, "未知值回归", "统计未知值", {"model": "loans"},
                                  result_digest(rows), len(rows), receipt(scope), basis=basis)
    assert store.evaluate(scope, asset["asset_id"], rows, actual_basis=basis)["status"] == "PASSED"
    changed = [{**rows[0], "days": 0}, rows[1]]
    assert store.evaluate(scope, asset["asset_id"], changed, actual_basis=basis)["status"] == "FAILED"
    assert store.evaluate(scope, asset["asset_id"], list(reversed(rows)), actual_basis=basis)["status"] == "FAILED"
    missing = store.evaluate(scope, asset["asset_id"], rows[:1], actual_basis=basis)
    assert missing["count_matches"] is False and missing["hash_matches"] is False
    assert result_digest([{"a": 1, "b": 2}]) == result_digest([{"b": 2, "a": 1}])
    with pytest.raises(AnalysisAssetError):
        store.evaluate(scope, memory(store, scope)["asset_id"], rows)


def test_live_data_changes_are_observations_and_unknown_or_different_data_is_incomparable(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path)
    rows = [{"count": 7}]
    live = {"kind": "DYNAMIC_LIVE_SOURCE"}
    asset = store.save_evaluation(scope, "在线观察", "数量", {"model": "books"},
                                  result_digest(rows), 1, receipt(scope), basis=live)
    changed = store.evaluate(scope, asset["asset_id"], [{"count": 8}], actual_basis=live)
    assert changed["status"] == "DATA_CHANGED"
    assert changed["regression_comparable"] is False
    assert "不代表模型回归失败" in changed["interpretation_zh"]
    assert store.evaluate(scope, asset["asset_id"], rows, actual_basis=live)["status"] == "OBSERVATION_MATCH"
    assert store.evaluate(scope, asset["asset_id"], rows)["status"] == "INCOMPARABLE"
    fixed = {"kind": "PUBLISHED_SNAPSHOT", "dataset_id": "SS-1", "version": "v1", "sha256": "sha256:" + "e" * 64}
    baseline = store.save_evaluation(scope, "固定数据", "数量", {"model": "books"},
                                     result_digest(rows), 1, receipt(scope), basis=fixed)
    different = {**fixed, "sha256": "sha256:" + "f" * 64}
    assert store.evaluate(scope, baseline["asset_id"], rows, actual_basis=different)["status"] == "INCOMPARABLE"
    with pytest.raises(AnalysisAssetError, match="requires"):
        store.save_evaluation(scope, "无凭证", "数量", {"model": "books"}, result_digest(rows), 1,
                               receipt(scope), basis={"kind": "FIXED_ACCEPTANCE_DATASET"})


@pytest.mark.parametrize("query", [
    {"password": "something"}, {"nested": {"api_key": "something"}},
    {"connection": "postgresql://user:secret@localhost/db"},
    {"sql": "password=supersecret"}, {"headers": ["Bearer 1234567890"]},
    {"key": "-----BEGIN " + "PRIVATE KEY-----"}, {"value": float("nan")},
    {"value": float("inf")}, {"value": set()}, {"value": "a" * 32769},
])
def test_credential_material_and_invalid_json_are_rejected(tmp_path, scope, query):
    store = AnalysisAssetStore(tmp_path)
    with pytest.raises(AnalysisAssetError):
        store.save_memory(scope, "统计", query, receipt(scope), True)
    assert not list(tmp_path.rglob("*.json"))


def test_path_symlinks_tampering_and_wrong_external_hash_fail_closed(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path / "assets")
    asset = memory(store, scope)
    for asset_id in ("../../secrets", "ANA-" + "x" * 32):
        with pytest.raises(AnalysisAssetError):
            store.get(scope, asset_id)
    with pytest.raises(AnalysisAssetError):
        store.get(scope, asset["asset_id"], "sha256:" + "f" * 64)
    path = next(store.root.rglob("ANA-*.json"))
    original = path.read_text()
    data = json.loads(original)
    data["payload"]["question"] = "tampered"
    path.write_text(json.dumps(data))
    with pytest.raises(AnalysisAssetError, match="integrity"):
        store.get(scope, asset["asset_id"])
    with pytest.raises(AnalysisAssetError):
        store.list_assets(scope)
    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text(original)
    path.symlink_to(target)
    with pytest.raises(AnalysisAssetError):
        store.get(scope, asset["asset_id"])
    path.unlink()


def test_scope_symlink_and_non_regular_file_are_not_followed(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path / "assets")
    asset = memory(store, scope)
    path = next(store.root.rglob("ANA-*.json"))
    scope_dir = path.parent
    outside = tmp_path / "outside"
    scope_dir.rename(outside)
    scope_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AnalysisAssetError):
        store.get(scope, asset["asset_id"])
    with pytest.raises(AnalysisAssetError):
        store.list_assets(scope)


def test_concurrent_writes_are_distinct_atomic_and_readable(tmp_path, scope):
    store = AnalysisAssetStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        assets = list(executor.map(lambda index: memory(store, scope, f"第{index}次逾期统计"), range(20)))
    assert len({asset["asset_id"] for asset in assets}) == 20
    assert len(store.list_assets(scope)) == 20
    assert not list(tmp_path.rglob("*.tmp"))


def test_input_limits_and_store_capacity_fail_before_writing(tmp_path, scope, monkeypatch):
    store = AnalysisAssetStore(tmp_path)
    with pytest.raises(AnalysisAssetError):
        store.save_memory(scope, "q" * 2001, {"sql": "SELECT 1"}, receipt(scope), True)
    nested = {"a": "value"}
    for _ in range(11):
        nested = {"a": nested}
    with pytest.raises(AnalysisAssetError):
        store.save_memory(scope, "统计", nested, receipt(scope), True)
    for limit in (0, 51, True):
        with pytest.raises(AnalysisAssetError):
            store.search_memories(scope, "统计", limit)
    monkeypatch.setattr("services.realtime_qa.analysis_assets.MAX_ASSETS_PER_SCOPE", 1)
    memory(store, scope)
    with pytest.raises(AnalysisAssetError, match="full"):
        memory(store, scope)
    assert len(store.list_assets(scope)) == 1
