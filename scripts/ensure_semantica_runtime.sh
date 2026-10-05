#!/bin/sh
set -eu

# Runtime metadata and logs may contain local filesystem details. Keep them
# private to the current user and never echo inherited credentials.
umask 077

# Re-read active selection on every supervisor invocation, even when its
# inherited environment still points at the previous installation.
CONFIG_ROOT="${ORION_RUNTIME_ROOT:-${ORION_PROJECT_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}}"
CORE_PYTHON="${ORION_WORKFLOW_PYTHON:-${PYTHON:-}}"
case "$CORE_PYTHON" in
  /*) test -x "$CORE_PYTHON" || { echo "Configured Core Python is unavailable." >&2; exit 2; } ;;
  *) echo "Configure an explicit absolute Core Python path." >&2; exit 2 ;;
esac
ACTIVE_CONFIG="${SEMANTICA_ACTIVE_RUNTIME_CONFIG:?Configure an explicit Profile Semantica runtime config}"
if [ "${SEMANTICA_RUNTIME_CONFIG_LOADED:-0}" != "1" ]; then
  exec "$CORE_PYTHON" "$CONFIG_ROOT/scripts/semantica_runtime_config.py" \
    --config "$ACTIVE_CONFIG" exec "$0" "$@"
fi

ACTION="${1:-ensure}"
if [ "$#" -gt 1 ]; then
  echo "用法：ensure_semantica_runtime.sh [ensure|check|adopt]" >&2
  exit 2
fi
case "$ACTION" in
  ensure|check|adopt) ;;
  *)
    echo "未知 Semantica runtime 操作：$ACTION" >&2
    exit 2
    ;;
esac

SEMANTICA_CLI="${SEMANTICA_CLI:?Configure the external Semantica CLI}"
SEMANTICA_PYTHON="${SEMANTICA_PYTHON:?Configure the external Semantica Python}"
# Keep the virtual-environment entry path for execution.  ``realpath`` is
# still used below for identity comparison, but invoking that resolved base
# interpreter loses the venv's site-packages in the clean child environment.
SEMANTICA_PYTHON_EXEC="$SEMANTICA_PYTHON"
SEMANTICA_PORT="${SEMANTICA_PORT:-8001}"
ORION_ROOT="$CONFIG_ROOT"
STATE_DIR="${SEMANTICA_STATE_DIR:?Configure the explicit Profile Semantica state directory}"
RUNTIME_MODE="${SEMANTICA_RUNTIME_MODE:-managed}"
RUNTIME_OWNER="${SEMANTICA_RUNTIME_OWNER:-orion-3081}"
GRAPH_PATH="${SEMANTICA_GRAPH_PATH:-$STATE_DIR/context-graph.json}"
OWNER_PATH="${SEMANTICA_OWNER_PATH:-$STATE_DIR/runtime-owner.json}"
PID_PATH="$STATE_DIR/explorer.pid"
LOG_PATH="$STATE_DIR/explorer.log"
SYNC_STATUS_PATH="$STATE_DIR/sync-status.json"
SYNC_LOG_PATH="$STATE_DIR/semantica-sync.log"
SYNC_PYTHON="${SEMANTICA_SYNC_PYTHON:-$CORE_PYTHON}"
SYNC_SCRIPT="${SEMANTICA_SYNC_SCRIPT:-$ORION_ROOT/scripts/sync_published_ontologies_to_semantica.py}"
HEALTH_URL="http://127.0.0.1:$SEMANTICA_PORT/api/health"
LSOF_BIN="${SEMANTICA_LSOF_BIN:-/usr/sbin/lsof}"
PS_BIN="${SEMANTICA_PS_BIN:-/bin/ps}"
CURL_BIN="${SEMANTICA_CURL_BIN:-/usr/bin/curl}"
NOHUP_BIN="${SEMANTICA_NOHUP_BIN:-/usr/bin/nohup}"
SLEEP_BIN="${SEMANTICA_SLEEP_BIN:-/bin/sleep}"

case "$RUNTIME_MODE" in
  managed|shared) ;;
  *)
    echo "SEMANTICA_RUNTIME_MODE 必须是 managed 或 shared。" >&2
    exit 2
    ;;
esac

canonical_path() {
  "$SEMANTICA_PYTHON" -c \
    'import os, sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' \
    "$1"
}

STATE_DIR="$(canonical_path "$STATE_DIR")"
GRAPH_PATH="$(canonical_path "$GRAPH_PATH")"
OWNER_PATH="$(canonical_path "$OWNER_PATH")"
SEMANTICA_PYTHON="$(canonical_path "$SEMANTICA_PYTHON")"
PID_PATH="$STATE_DIR/explorer.pid"
LOG_PATH="$STATE_DIR/explorer.log"
SYNC_STATUS_PATH="$STATE_DIR/sync-status.json"
SYNC_LOG_PATH="$STATE_DIR/semantica-sync.log"

if [ "$RUNTIME_MODE" = "managed" ]; then
  case "$GRAPH_PATH" in
    "$STATE_DIR"/*) ;;
    *)
      echo "managed 模式只允许使用 Semantica state 目录内的 graph。" >&2
      exit 1
      ;;
  esac
fi

listener_pids() {
  "$LSOF_BIN" -nP -t -iTCP:"$SEMANTICA_PORT" -sTCP:LISTEN 2>/dev/null \
    | /usr/bin/sort -u || true
}

single_listener_pid() {
  pids="$(listener_pids)"
  set -- $pids
  [ "$#" -eq 1 ] || return 1
  /usr/bin/printf '%s\n' "$1"
}

process_identity_matches() {
  pid="$1"
  command_line="$($PS_BIN -ww -p "$pid" -o command= 2>/dev/null || true)"
  [ -n "$command_line" ] || return 1
  "$SEMANTICA_PYTHON" -c '
import os
import shlex
import sys

command, expected_python, expected_graph, expected_port = sys.argv[1:]
try:
    argv = shlex.split(command)
except ValueError:
    raise SystemExit(1)
if "-m" in argv:
    module_flag = argv.index("-m")
    if module_flag > 1 and argv[module_flag + 1:module_flag + 2] == ["semantica.explorer"]:
        argv[:module_flag] = [" ".join(argv[:module_flag])]
if not argv or os.path.realpath(argv[0]) != os.path.realpath(expected_python):
    raise SystemExit(1)
module_ok = any(
    argv[index : index + 2] == ["-m", "semantica.explorer"]
    for index in range(len(argv) - 1)
)
entrypoint_ok = any(
    os.path.basename(argument) == "semantica-explorer" for argument in argv[1:3]
)
if not (module_ok or entrypoint_ok):
    raise SystemExit(1)

def option(name):
    for index, argument in enumerate(argv):
        if argument == name and index + 1 < len(argv):
            if name == "--graph":
                end = next((i for i in range(index + 1, len(argv)) if argv[i].startswith("--")), len(argv))
                return " ".join(argv[index + 1:end])
            return argv[index + 1]
        prefix = name + "="
        if argument.startswith(prefix):
            return argument[len(prefix) :]
    return None

graph = option("--graph")
port = option("--port")
if graph is None or os.path.realpath(graph) != os.path.realpath(expected_graph):
    raise SystemExit(1)
if port != expected_port:
    raise SystemExit(1)
' "$command_line" "$SEMANTICA_PYTHON" "$GRAPH_PATH" "$SEMANTICA_PORT"
}

health_ready() {
  health_payload="$($CURL_BIN --noproxy '*' --silent --fail --max-time 2 "$HEALTH_URL" 2>/dev/null)" \
    || return 1
  /usr/bin/printf '%s' "$health_payload" | "$SEMANTICA_PYTHON" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
if str(payload.get("status", "")).lower() not in {"ok", "healthy", "ready"}:
    raise SystemExit(1)
'
}

runtime_identity_ready() {
  pid="$(single_listener_pid)" || return 1
  process_identity_matches "$pid" || return 1
  health_ready || return 1
}

validate_owner_metadata() {
  [ -s "$OWNER_PATH" ] || return 1
  "$SEMANTICA_PYTHON" -c '
import json
import os
import sys

path, owner, mode, graph, port, python = sys.argv[1:]
try:
    payload = json.loads(open(path, encoding="utf-8").read())
except (OSError, json.JSONDecodeError):
    print("Semantica owner metadata 无法读取。", file=sys.stderr)
    raise SystemExit(1)
expected = {
    "schema_version": 1,
    "owner": owner,
    "mode": mode,
    "graph_path": os.path.realpath(graph),
    "port": int(port),
    "python": os.path.realpath(python),
}
actual = dict(payload)
actual["graph_path"] = os.path.realpath(str(actual.get("graph_path", "")))
actual["python"] = os.path.realpath(str(actual.get("python", "")))
for key, value in expected.items():
    if actual.get(key) != value:
        print(f"Semantica owner metadata 不匹配：{key}", file=sys.stderr)
        raise SystemExit(1)
' "$OWNER_PATH" "$RUNTIME_OWNER" "$RUNTIME_MODE" "$GRAPH_PATH" \
    "$SEMANTICA_PORT" "$SEMANTICA_PYTHON"
}

write_owner_metadata() {
  transition="$1"
  listener_pid="$2"
  "$SEMANTICA_PYTHON" -c '
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

path_arg, owner, mode, graph, port, python, transition, pid = sys.argv[1:]
path = Path(path_arg)
try:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except (OSError, json.JSONDecodeError):
    payload = {}
now = datetime.now(timezone.utc).isoformat()
payload.update(
    {
        "schema_version": 1,
        "owner": owner,
        "mode": mode,
        "graph_path": os.path.realpath(graph),
        "port": int(port),
        "python": os.path.realpath(python),
        "listener_pid": int(pid),
        "last_transition": transition,
        "updated_at": now,
    }
)
payload.setdefault("created_at", now)
if transition == "ADOPTED":
    payload.setdefault("adopted_at", now)
if transition == "STARTED":
    payload["last_started_at"] = now
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
' "$OWNER_PATH" "$RUNTIME_OWNER" "$RUNTIME_MODE" "$GRAPH_PATH" \
    "$SEMANTICA_PORT" "$SEMANTICA_PYTHON" "$transition" "$listener_pid"
}

write_sync_status() {
  sync_status="$1"
  sync_detail="$2"
  "$SEMANTICA_PYTHON" -c '
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

path_arg, status, detail, graph = sys.argv[1:]
path = Path(path_arg)
payload = {
    "schema_version": 1,
    "status": status,
    "detail": detail,
    "graph_path": os.path.realpath(graph),
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
' "$SYNC_STATUS_PATH" "$sync_status" "$sync_detail" "$GRAPH_PATH"
}

require_owner_metadata() {
  if [ ! -s "$OWNER_PATH" ]; then
    echo "Semantica owner metadata 不存在；拒绝静默接管。请先执行受控 adopt。" >&2
    exit 1
  fi
  if ! validate_owner_metadata; then
    echo "Semantica runtime 身份与已持久化 owner 不一致；拒绝接管。" >&2
    exit 1
  fi
}

require_shared_graph() {
  if [ ! -s "$GRAPH_PATH" ]; then
    echo "Semantica shared graph 不存在或为空：$GRAPH_PATH" >&2
    exit 1
  fi
}

run_sync_after_restart() {
  if [ "${SEMANTICA_AUTO_SYNC:-true}" = "false" ]; then
    write_sync_status "DISABLED" "显式关闭启动时全量同步；保留既有发布数据"
    return 0
  fi
  if [ -x "$SYNC_PYTHON" ] && [ -f "$SYNC_SCRIPT" ]; then
    if "$SYNC_PYTHON" "$SYNC_SCRIPT" --semantica-url "http://127.0.0.1:$SEMANTICA_PORT" \
      >> "$SYNC_LOG_PATH" 2>&1; then
      write_sync_status "SYNCED" "幂等 published ontology sync 已完成"
      return 0
    fi
    write_sync_status "PENDING" "runtime 已恢复，published ontology sync 执行失败"
    echo "Semantica 已恢复，但 published ontology sync 失败；已记录待同步状态。" >&2
    return 0
  fi
  write_sync_status "PENDING" "runtime 已恢复，sync 运行条件不可用"
  echo "Semantica 已恢复，但 sync 运行条件不可用；已记录待同步状态。" >&2
}

if [ "$RUNTIME_MODE" = "shared" ]; then
  require_shared_graph
fi

if [ "$ACTION" = "adopt" ]; then
  if [ "$RUNTIME_MODE" != "shared" ]; then
    echo "adopt 只允许用于 shared 模式。" >&2
    exit 1
  fi
  if [ -s "$OWNER_PATH" ] && ! validate_owner_metadata; then
    echo "已有 Semantica owner metadata 与本次 adopt 不一致；拒绝覆盖。" >&2
    exit 1
  fi
  listener_pid="$(single_listener_pid)" || {
    echo "Semantica adopt 要求端口上恰好有一个 listener。" >&2
    exit 1
  }
  if ! process_identity_matches "$listener_pid" || ! health_ready; then
    echo "Semantica listener 的 PID、command、graph 或 health 不匹配；拒绝 adopt。" >&2
    exit 1
  fi
  /bin/mkdir -p "$STATE_DIR"
  write_owner_metadata "ADOPTED" "$listener_pid"
  /usr/bin/printf '%s\n' "Semantica shared runtime 已受控采纳。"
  exit 0
fi

if [ "$ACTION" = "check" ]; then
  require_owner_metadata
  if ! runtime_identity_ready; then
    echo "Semantica identity canary 失败：listener PID、command、graph 或 health 不匹配。" >&2
    exit 1
  fi
  exit 0
fi

# `ensure` may claim only the private, project-local managed graph. Shared
# graphs always require the explicit `adopt` action above.
if [ ! -s "$OWNER_PATH" ]; then
  if [ "$RUNTIME_MODE" != "managed" ]; then
    require_owner_metadata
  fi
  if [ -n "$(listener_pids)" ]; then
    echo "8001 已有 listener 且无 owner metadata；拒绝静默接管。" >&2
    exit 1
  fi
  /bin/mkdir -p "$STATE_DIR"
  if [ ! -e "$GRAPH_PATH" ]; then
    /usr/bin/printf '%s\n' \
      '{"graph_id":"orion-semantica","nodes":[],"edges":[],"links":[]}' \
      > "$GRAPH_PATH"
  fi
  if [ ! -s "$GRAPH_PATH" ]; then
    echo "Semantica managed graph 为空；拒绝启动。" >&2
    exit 1
  fi
  write_owner_metadata "CLAIMED" "0"
else
  require_owner_metadata
fi

existing_pids="$(listener_pids)"
if [ -n "$existing_pids" ]; then
  set -- $existing_pids
  if [ "$#" -ne 1 ]; then
    echo "8001 检测到多个 listener；拒绝接管。" >&2
    exit 1
  fi
  if process_identity_matches "$1" && health_ready; then
    exit 0
  fi
  echo "8001 listener 与持久化 Semantica owner/graph 不一致；拒绝接管。" >&2
  exit 1
fi

/bin/mkdir -p "$STATE_DIR"
# The supervisor carries unrelated MCP and storage credentials. Start the
# long-lived explorer from a clean environment so those secrets are not
# inherited by Semantica.
/usr/bin/env -i \
  HOME="${HOME:-/tmp}" \
  PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  LANG="${LANG:-C}" \
  TMPDIR="${TMPDIR:-/tmp}" \
  SEMANTICA_ALLOW_ANONYMOUS=true \
  SEMANTICA_REQUEST_DIAGNOSTICS=1 \
  SEMANTICA_EXPLORER_AUTOSAVE_PATH="$GRAPH_PATH" \
  SEMANTICA_EXPLORER_STATE_DIR="${SEMANTICA_EXPLORER_STATE_DIR:-}" \
  SEMANTICA_EXPLORER_LEGACY_GRAPH="${SEMANTICA_EXPLORER_LEGACY_GRAPH:-}" \
  ORION_ROOT="$ORION_ROOT" \
  ORION_EXPLORATION_INDEX_ROOT="${ORION_EXPLORATION_INDEX_ROOT:-$ORION_ROOT/.orion-runtime/instance-exploration}" \
  ALLOWED_ORIGINS="http://127.0.0.1:3081,http://localhost:3081" \
  "$NOHUP_BIN" "$SEMANTICA_PYTHON_EXEC" -m semantica.explorer \
    --graph "$GRAPH_PATH" --port "$SEMANTICA_PORT" --host 127.0.0.1 --no-browser \
    > "$LOG_PATH" 2>&1 &
started_pid="$!"
/usr/bin/printf '%s\n' "$started_pid" > "$PID_PATH"

attempt=0
# Restoring the full persisted graph and its search index can exceed 30 seconds.
while [ "$attempt" -lt "${SEMANTICA_START_ATTEMPTS:-240}" ]; do
  if runtime_identity_ready; then
    listener_pid="$(single_listener_pid)"
    write_owner_metadata "STARTED" "$listener_pid"
    run_sync_after_restart
    exit 0
  fi
  attempt=$((attempt + 1))
  "$SLEEP_BIN" "${SEMANTICA_START_INTERVAL:-0.5}"
done

if /bin/kill -0 "$started_pid" 2>/dev/null; then
  /bin/kill "$started_pid" 2>/dev/null || true
fi
echo "Semantica 解释运行时未能以持久化 owner/graph 身份就绪。" >&2
exit 1
