"""Typed CQ values survive answer preview, HTTP JSON encoding, and audit storage."""
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from services.realtime_qa.answer import RealtimeAnswerService
from services.realtime_qa.evidence_receipts import EvidenceReceiptStore
from services.realtime_qa.models import EvidenceBundle, EvidenceRecord, RealtimeSessionAnswer
from tests.unit.test_realtime_answer import (
    NOW,
    release_binding,
    release_evidence,
    request,
    statuses,
)


def test_typed_cq_answer_and_receipt_preserve_dates_and_decimal_precision(tmp_path):
    row = {"start": date(2026, 9, 8), "at": datetime(2026, 9, 8, 1, 2, tzinfo=UTC),
           "amount": Decimal("12345678901234567890.1234567890"), "count": 1}
    bundle = EvidenceBundle(query_id="Q-ANSWER", mode="hybrid", release=release_binding(),
        generated_at=NOW, complete=True, evidence=[release_evidence(), EvidenceRecord(
            evidence_id="EVD-TYPED", source_kind="document", source_ref="document-fact:typed",
            observed_at=NOW, payload={"cq_results": [{"rows": [row], "row_count": 1}]})],
        source_status=statuses(structured="not_requested", document="fresh"))
    answer = RealtimeAnswerService().answer(request(), bundle)
    preview = answer.citations[1].facts["rows_preview"][0]
    assert preview["start"] == "2026-09-08"
    assert preview["at"] == "2026-09-08T01:02:00+00:00"
    assert preview["amount"] == "12345678901234567890.1234567890"
    assert answer.evidence_bundle.evidence[1].payload["cq_results"][0]["rows"][0] == row
    # HTTP response model and the receipt both use Pydantic JSON serialization.
    encoded = json.loads(answer.model_dump_json())
    assert encoded["evidence_bundle"]["evidence"][1]["payload"]["cq_results"][0]["rows"][0]["start"] == "2026-09-08"
    store = EvidenceReceiptStore(tmp_path / "receipts")
    response = RealtimeSessionAnswer(
        session_id="session-33333333-3333-3333-3333-333333333333", answer=answer,
        runtime_binding={"project_id": bundle.release.project_id, "release_version": bundle.release.release_version,
                         "release_fingerprint": bundle.release.release_fingerprint, "runtime_mode": "DOCUMENT_ONLY",
                         "structured_query_enabled": False, "artifact_verified": True, "runtime_verified": True,
                         "database_access_mode": "READ_ONLY", "ontop_query_names": []},
    )
    reference = store.save(response)
    restored = json.loads(store.read(reference))["response"]["answer"]
    assert restored == encoded


@pytest.mark.parametrize("value", [object(), Decimal("NaN"), Decimal("Infinity")])
def test_opaque_and_nonfinite_query_values_are_not_silently_stringified(value):
    with pytest.raises((TypeError, ValueError)):
        RealtimeAnswerService._bounded_facts({"value": value})
