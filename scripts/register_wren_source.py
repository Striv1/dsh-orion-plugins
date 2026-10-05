#!/usr/bin/env python3
"""Register a read-only service source against an approved S7 release.

Run with the service's environment already loaded. Config JSON contains only
identity, scope and ORION_*_SOURCE_URL variable names, never connection values.
This command does not publish/rewrite a release or grant database privileges.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.realtime_qa.binding import ReleaseBindingLoader  # noqa: E402
from services.structured_data.source_registration import (  # noqa: E402
    register_service_source,
    safe_source_summary,
)

CONFIG_FIELDS = {"project_id", "source_id", "connection_env", "database", "authorized_tables",
    "authorized_columns", "chat2db_datasource_id", "query_node", "freshness_threshold_ms",
    "primary_connection_env"}


def authorized_registration(config: dict, binding, expected_release: str) -> dict:
    """Fail before connecting if an administrator config exceeds its release."""
    if not isinstance(config, dict) or set(config) - CONFIG_FIELDS:
        raise ValueError("登记配置含未知字段；仅允许环境变量引用与业务来源范围。")
    if not expected_release or binding.release_fingerprint != expected_release:
        raise ValueError("当前发布指纹与预期版本不一致，请重新核对正式发布。")
    if binding.project_id != config.get("project_id"):
        raise ValueError("登记工程与正式发布工程不一致。")
    published = binding.source_bindings.get(config.get("source_id"), {})
    if (published.get("project_id") != binding.project_id
            or published.get("source_id") != config.get("source_id")
            or published.get("status") != "ACTIVE" or published.get("access_mode") != "READ_ONLY"
            or published.get("database") != config.get("database")):
        raise ValueError("来源身份、数据库或只读授权与正式发布不一致。")
    tables = config.get("authorized_tables")
    columns = config.get("authorized_columns")
    if not isinstance(tables, list) or not tables or len(tables) != len(set(tables)):
        raise ValueError("必须提供唯一的 schema.table 列表。")
    if not isinstance(columns, dict) or set(columns) != set(tables):
        raise ValueError("登记须为每张授权表明确列范围。")
    for qualified in tables:
        parts = str(qualified).split(".")
        if len(parts) != 2 or parts[0] not in published.get("schemas", []):
            raise ValueError("来源 Schema 不在正式发布范围内。")
        name = next((n for n in (qualified, parts[1]) if n in published.get("authorized_tables", [])), None)
        requested = columns[qualified]
        if name == parts[1] and len(published.get("schemas", [])) != 1:
            raise ValueError("发布来源的无 Schema 表名存在歧义，请先明确正式来源范围。")
        if (name is None or not isinstance(requested, list) or not requested
                or len(requested) != len(set(requested))
                or not set(requested) <= set(published.get("authorized_columns", {}).get(name, []))):
            raise ValueError("来源表或字段超出正式发布范围。")
    # Administrative code may bind a different read-only principal to the same
    # source, but publication identity and data responsibility remain unchanged.
    return {**config, "owner": published["owner"], "pii_scope": published["pii_scope"],
            "dataset_type": "PRODUCTION"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--expected-release", required=True, help="Approved package sha256 fingerprint")
    args = parser.parse_args()
    try:
        raw = args.config.read_bytes()
        if len(raw) > 128 * 1024:
            raise ValueError("登记配置超过大小预算。")
        config = json.loads(raw)
        binding = ReleaseBindingLoader().load(args.project_dir.resolve())
        record = register_service_source(**authorized_registration(config, binding, args.expected_release))
    except (OSError, ValueError, KeyError, TypeError):
        # Config/driver exceptions could contain URLs; do not print their text.
        print(json.dumps({"ok": False, "code": "SOURCE_REGISTRATION_REJECTED",
            "message": "登记未通过；请核对正式发布指纹、来源范围、服务环境及数据库只读权限。未改动发布或数据库权限。"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "source": safe_source_summary(record),
                      "validated_release_fingerprint": binding.release_fingerprint}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
