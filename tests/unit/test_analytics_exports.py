import hashlib
import json
from types import SimpleNamespace

import pytest

from services.realtime_qa.analytics_exports import AnalyticsExportJobs

SCOPE = {"session_id": "session-11111111-1111-1111-1111-111111111111", "project_id": "test-project",
         "release_version": "1.0", "release_fingerprint": "sha256:" + "a" * 64}
JOB_ID = "EXP-" + "b" * 32


def completed(tmp_path):
    jobs = AnalyticsExportJobs(tmp_path / "exports")
    data = b'id,name\r\n1,hello\r\n'
    job = {**SCOPE, "job_id": JOB_ID, "status": "COMPLETED", "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}
    jobs._write(job)
    (jobs.root / (JOB_ID + ".csv")).write_bytes(data)
    return jobs, data


def test_download_is_identity_and_checksum_bound(tmp_path):
    jobs, data = completed(tmp_path)
    stream, job = jobs.download(SCOPE, JOB_ID)
    with stream:
        assert stream.read() == data
    with pytest.raises(ValueError, match="会话"):
        jobs.download({**SCOPE, "session_id": "other"}, JOB_ID)
    (jobs.root / (JOB_ID + ".csv")).write_bytes(b'changed')
    with pytest.raises(ValueError, match="完整性"):
        jobs.download(SCOPE, JOB_ID)


def test_restart_marks_running_job_interrupted_and_removes_partial(tmp_path):
    jobs = AnalyticsExportJobs(tmp_path / "exports")
    jobs._write({**SCOPE, "job_id": JOB_ID, "status": "RUNNING"})
    partial = jobs.root / (JOB_ID + ".csv.partial")
    partial.write_text("partial data")
    assert jobs.status(SCOPE, JOB_ID)["status"] == "INTERRUPTED"
    assert not partial.exists()
    with pytest.raises(ValueError, match="尚未完成"):
        jobs.download(SCOPE, JOB_ID)


def test_export_file_symlink_is_rejected(tmp_path):
    jobs, _ = completed(tmp_path)
    path = jobs.root / (JOB_ID + ".csv")
    path.unlink()
    original = tmp_path / "untouched.csv"
    original.write_text("keep")
    path.symlink_to(original)
    with pytest.raises(OSError):
        jobs.download(SCOPE, JOB_ID)
    assert original.read_text() == "keep"


def test_running_job_cancel_does_not_publish_partial(tmp_path):
    import threading
    jobs = AnalyticsExportJobs(tmp_path / "exports")
    jobs._write({**SCOPE, "job_id": JOB_ID, "status": "RUNNING"})
    calls = []
    jobs.tasks[JOB_ID] = {"cancel": threading.Event(), "process": SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append("terminate"))}
    assert jobs.cancel(SCOPE, JOB_ID)["status"] == "CANCELLING"
    assert calls == ["terminate"]
    assert jobs.tasks[JOB_ID]["cancel"].is_set()
    assert json.loads((jobs.root / (JOB_ID + ".json")).read_text())["status"] == "RUNNING"
