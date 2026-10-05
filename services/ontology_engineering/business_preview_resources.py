"""Recover only journaled, owned preview resources after a worker has ended."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from services.ontology_contracts.errors import WorkflowError


def recover_resources(job_dir, record, read):
    candidate = job_dir / "candidate"
    if candidate.is_symlink():
        raise WorkflowError("试运行资源目录不安全，已保留以供核对。")
    resource = read(candidate / "candidate-resource.json")
    if resource is None:
        return
    expected = "business-preview-" + hashlib.sha256(record["input_fingerprint"].encode()).hexdigest()[:20]
    container = resource.get("container_name", "")
    if resource.get("candidate_id") != expected or not re.fullmatch(r"orion-s6-[a-z0-9_-]{1,8}-[a-f0-9]{8}", container):
        raise WorkflowError("试运行资源身份不符，不能自动清理或重复启动。")
    try:
        if type(record.get("pid")) is int and record.get("status") == "INTERRUPTED":
            processes = subprocess.run(["ps", "-ax", "-o", "pgid=", "-o", "stat=", "-o", "command="],
                                       capture_output=True, text=True, timeout=5, check=False)
            if processes.returncode != 0:
                raise WorkflowError("前次试运行子进程状态无法确认，暂不能重复启动。")
            for line in processes.stdout.splitlines():
                fields = line.strip().split(maxsplit=2)
                if len(fields) >= 2 and fields[0] == str(record["pid"]) and not fields[1].startswith("Z"):
                    raise WorkflowError("前次试运行子进程仍在收尾；等待其结束后再试，避免迟到的资源创建。")
        inspected = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", container],
                                   capture_output=True, text=True, timeout=5, check=False)
        if inspected.returncode == 0:
            labels = json.loads(inspected.stdout)
            if not isinstance(labels, dict) or labels.get("orion.managed-preview") != expected:
                raise WorkflowError("容器不属于当前试运行，已保留，不能继续启动。")
            removed = subprocess.run(["docker", "rm", "--force", container], capture_output=True,
                                     text=True, timeout=15, check=False)
            if removed.returncode != 0:
                raise WorkflowError("试运行资源暂无法回收，请恢复容器服务后重试。")
        elif "no such object" not in inspected.stderr.lower() and "no such container" not in inspected.stderr.lower():
            raise WorkflowError("无法核对前次试运行资源，恢复容器服务后再重试。")
        temporary = Path(resource["temp_dir"])
        if (temporary.is_symlink() or temporary.parent.resolve() != Path(tempfile.gettempdir()).resolve()
                or not temporary.name.startswith("orion-s6-ontop-")):
            raise WorkflowError("临时资源目录身份不符，已保留。")
        if temporary.exists():
            if read(temporary / "candidate-owner.json") != resource:
                raise WorkflowError("临时资源所属回执不符，已保留。")
            shutil.rmtree(temporary)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise WorkflowError("前次试运行资源尚未完成回收，请恢复执行环境后重试。") from exc
