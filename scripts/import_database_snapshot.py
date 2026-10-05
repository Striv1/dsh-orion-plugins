#!/usr/bin/env python3
"""CLI wrapper for services.structured_data.snapshot_capture.

The same capture is available to the 3081 agent as the workflow MCP tool
`capture_database_snapshot`; prefer the platform tool for normal projects.
"""

from __future__ import annotations

import argparse
import json

from services.structured_data.snapshot_capture import (
    SnapshotCaptureError,
    capture_database_snapshot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="将一个只读数据库源导入 ORION Snapshot Hub")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", action="append", required=True)
    parser.add_argument("--exclude-column", action="append", default=[])
    parser.add_argument("--pii-scope", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--snapshot-version")
    parser.add_argument("--dataset-type", choices=["PRODUCTION", "TEST_ONLY"], default="TEST_ONLY")
    parser.add_argument("--production-evidence-basis", default=None)
    args = parser.parse_args()
    try:
        receipt = capture_database_snapshot(
            project_id=args.project_id,
            source_id=args.source_id,
            connection_env=args.source_env,
            database=args.database,
            schema=args.schema,
            tables=args.table,
            exclude_columns=args.exclude_column,
            pii_scope=args.pii_scope,
            owner=args.owner,
            snapshot_version=args.snapshot_version,
            dataset_type=args.dataset_type,
            production_evidence_basis=args.production_evidence_basis,
        )
    except SnapshotCaptureError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
