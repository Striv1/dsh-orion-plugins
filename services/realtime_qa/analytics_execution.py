"""Execute Wren in the source database; never materialize an input sample."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from services.realtime_qa.wren_analysis import WREN_PYTHON
from services.structured_data.pipeline import StructuredDataPipeline

ROOT = Path(__file__).resolve().parents[2]
SLOTS = threading.BoundedSemaphore(2)


def postgres_execution(binding, descriptor) -> dict:
    """Check frozen catalog ownership through the platform's read-only account.

    No raw rows or passwords are returned to clients. Catalog checks are small
    irrespective of fact-table size; analytical scans happen in PostgreSQL.
    """
    reader = os.getenv("ORION_SOURCE_DATA_READER_URL", "").strip()
    if not reader:
        raise ValueError("未配置受控快照的只读查询连接，不能回退使用写入账号。")
    if not binding.snapshot_set_id or not binding.cross_source_snapshot_set:
        raise ValueError("当前版本没有完整冻结的数据库快照合同；请通过工程修订发布后启用来源分析。")
    url = StructuredDataPipeline(reader).database_url
    try:
        with psycopg.connect(url, connect_timeout=10) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = 10000")
            cursor.execute("SHOW transaction_read_only")
            if cursor.fetchone()[0] != "on":
                raise ValueError("数据连接未能启用只读事务。")
            cursor.execute("SELECT project_id, snapshot_complete, manifest_sha256 FROM orion_catalog.snapshot_sets WHERE snapshot_set_id=%s", (binding.snapshot_set_id,))
            actual = cursor.fetchone()
            expected = binding.cross_source_snapshot_set
            if (not actual or actual[0] != binding.project_id or actual[1] is not True
                    or actual[2] != expected.get("manifest_sha256")):
                raise ValueError("发布版本与当前快照目录身份或完整性不一致。")
            checked = set()
            for table in descriptor["source_tables"]:
                source_id = table["source_id"]
                manifest = binding.snapshot_manifests.get(source_id, {})
                source = binding.source_bindings.get(source_id, {})
                candidates = [t for t in manifest.get("tables", [])
                              if t.get("target_table") == table["physical_table"] and t.get("table") == table["source_table"]]
                if (len(candidates) != 1 or manifest.get("dataset_id") != table["dataset_id"]
                        or manifest.get("project_id") != binding.project_id or manifest.get("snapshot_complete") is not True
                        or source.get("status") != "ACTIVE" or source.get("access_mode") != "READ_ONLY"
                        or not set(table["columns"]) <= set(source.get("authorized_columns", {}).get(table["source_table"], []))):
                    raise ValueError("分析模型的来源表、字段或快照未得到本发布版本授权。")
                if source_id in checked:
                    continue
                cursor.execute("SELECT s.project_id, s.source_id, s.source_sha256, s.snapshot_complete, s.manifest FROM orion_catalog.source_snapshots s JOIN orion_catalog.snapshot_set_members m ON m.dataset_id=s.dataset_id WHERE s.dataset_id=%s AND m.snapshot_set_id=%s AND m.source_id=%s", (manifest["dataset_id"], binding.snapshot_set_id, source_id))
                row = cursor.fetchone()
                if (not row or row[0] != binding.project_id or row[1] != source_id
                        or row[2] != manifest.get("source_sha256") or row[3] is not True
                        or row[4] != manifest):
                    raise ValueError("当前来源目录内容与已发布快照清单不一致。")
                checked.add(source_id)
    except psycopg.Error as exc:
        raise ValueError(f"受控来源校验失败（SQLSTATE={exc.sqlstate or 'unknown'}）；未执行分析。") from None
    params = conninfo_to_dict(url)
    required = {"host", "dbname", "user", "password"}
    if not required <= params.keys():
        raise ValueError("Wren PostgreSQL 连接需要明确的主机、库及只读账号配置。")
    kwargs = {key: params[key] for key in ("sslmode", "sslrootcert", "sslcert", "sslkey") if key in params}
    kwargs.update(connect_timeout="10", options="-c default_transaction_read_only=on -c statement_timeout=25000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=30000")
    return {"data_source": "postgres", "connection_info": {
        "host": params["host"], "port": params.get("port", "5432"), "database": params["dbname"],
        "user": params["user"], "password": params["password"], "kwargs": kwargs,
    }}


def run_project_query(binding, descriptor, request) -> dict:
    if not WREN_PYTHON.is_file():
        raise ValueError("Wren 运行环境尚未安装。")
    if not SLOTS.acquire(blocking=False):
        raise ValueError("分析执行槽位已满，请稍后重试。")
    try:
        if descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE":
            from services.realtime_qa.live_analytics import live_execution
            execution = live_execution(descriptor)
        else:
            execution = postgres_execution(binding, descriptor)
        plan = {"sql": request.sql, "parameters": request.parameters} if request.sql is not None else {"cube_query": request.cube_query}
        payload = {"descriptor": {k: v for k, v in descriptor.items() if not k.startswith("_")}, "execution": execution, "plan": plan, "limit": request.limit}
        process = subprocess.run([str(WREN_PYTHON), str(ROOT / "services/realtime_qa/wren_project_worker.py")],
            input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, timeout=55,
            cwd=ROOT, env={key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "TMPDIR"}})
        # stderr is deliberately never forwarded: drivers can include connection
        # strings in errors. Child stdout is a bounded, structured contract.
        if len(process.stdout.encode()) > 4 * 1024 * 1024:
            raise ValueError("分析结果超过输出预算；请减少返回列或结果行数，源数据统计范围不变。")
        try:
            output = json.loads(process.stdout)
        except (ValueError, TypeError):
            raise ValueError("Wren 未返回有效执行结果；请核对运行环境。") from None
        if process.returncode or output.get("error"):
            message = str(output.get("error") or "Wren 查询失败")[:1200]
            for value in execution["connection_info"].values():
                if isinstance(value, str) and len(value) >= 4:
                    message = message.replace(value, "[连接信息]")
            raise ValueError(message)
        if (not isinstance(output.get("rows"), list) or len(output["rows"]) > request.limit
                or type(output.get("truncated")) is not bool
                or output.get("engine_version") != "0.15.0"):
            raise ValueError("Wren 响应不满足结果合同。")
        if "data_freshness" in execution:
            output.setdefault("data_freshness", execution["data_freshness"])
        return output
    except subprocess.TimeoutExpired:
        raise TimeoutError("分析超时，数据库语句也受超时限制；未将部分数据当作完整结果。") from None
    finally:
        SLOTS.release()
