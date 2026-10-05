"""Profile paths are respected by real service components in a fresh interpreter."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_profile_runtime_paths_and_explicit_store_override(tmp_path):
    profile = tmp_path / "profile"
    variables = {
        "ORION_WREN_PYTHON": str(profile / ".venvs/wren/bin/python"),
        "ORION_WREN_CACHE_ROOT": str(profile / "cache/wren-projects"),
        "ORION_WREN_SOURCE_REGISTRY_ROOT": str(profile / "state/wren-sources"),
        "ORION_ANALYTICS_EXPORT_ROOT": str(profile / "state/analytics-exports"),
        "ORION_ANALYSIS_ASSET_ROOT": str(profile / "state/analysis-assets"),
    }
    program = """
import json, os, sys
from pathlib import Path
from fastapi import FastAPI
from services.realtime_qa import analytics_api, wren_analysis, wren_project
from services.realtime_qa.analytics_exports import AnalyticsExportJobs
from services.structured_data import source_registration
profile = Path(sys.argv[1])
exports = AnalyticsExportJobs()
fd = exports._directory()
os.close(fd)
manual = AnalyticsExportJobs(profile / 'explicit-exports')
roots = []
actual = analytics_api.AnalysisAssetStore
def capture(path):
    roots.append(str(path))
    return actual(path)
analytics_api.AnalysisAssetStore = capture
analytics_api.register_analytics_api(FastAPI(), None, lambda *_: None, exports=exports)
try:
    source_registration.load_service_source('ontology-project-test', 'source-test')
except FileNotFoundError as exc:
    missing = exc.filename
else:
    raise AssertionError('Must not borrow a source from the original registry')
print(json.dumps({'python': str(wren_analysis.WREN_PYTHON),
                  'cache': str(wren_project.CACHE_ROOT),
                  'registry': str(source_registration.DEFAULT_REGISTRY_ROOT),
                  'source_read': missing, 'exports': str(exports.root),
                  'manual': str(manual.root), 'assets': roots[0],
                  'exports_created': exports.root.is_dir()}))
exports.pool.shutdown()
manual.pool.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(profile)],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, **variables, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=True, timeout=20,
    )
    actual = json.loads(result.stdout)
    assert actual["python"] == variables["ORION_WREN_PYTHON"]
    assert actual["cache"] == variables["ORION_WREN_CACHE_ROOT"]
    assert actual["registry"] == variables["ORION_WREN_SOURCE_REGISTRY_ROOT"]
    assert actual["source_read"] == str(profile / "state/wren-sources/ontology-project-test/source-test.json")
    assert actual["exports"] == variables["ORION_ANALYTICS_EXPORT_ROOT"]
    assert actual["manual"] == str(profile / "explicit-exports")
    assert actual["assets"] == variables["ORION_ANALYSIS_ASSET_ROOT"]
    assert actual["exports_created"] is True
