"""Useful worker failure identity without source values or exception messages."""
from __future__ import annotations

import hashlib
import json
import re
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def worker_failure(exc: Exception, phase: str | None) -> dict:
    # Error arguments, source code, absolute host paths and chained exception
    # messages may contain credentials. Retain only code-owned frame locations.
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            relative = Path(frame.filename).resolve().relative_to(ROOT)
        except ValueError:
            continue
        if relative.parts[0] not in {"services", "scripts", "harness"}:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", frame.name):
            continue
        frames.append({"file": relative.as_posix(), "function": frame.name, "line": frame.lineno})
    kind = next((base.__name__ for base in type(exc).__mro__ if base.__module__ == "builtins"
                 and issubclass(base, Exception)), "Exception")
    evidence = {"exception_type": kind, "frames": frames[-8:]}
    return {
        "code": "PREVIEW_WORKER_ERROR", "owner": "PLATFORM", "automatic_repair_allowed": False,
        "message": "试运行后台任务异常；不能据此判断业务设计错误。",
        "next_step": "保留当前草稿并停止自动修订，交由平台维护核对诊断；不要猜测或改写业务规则、映射和预期答案。",
        "diagnostic_id": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()[:20],
        "phase": phase if isinstance(phase, str) and re.fullmatch(r"[A-Z_]{1,64}", phase) else "UNKNOWN",
        **evidence,
    }
