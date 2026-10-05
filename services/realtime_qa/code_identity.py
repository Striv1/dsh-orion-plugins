from __future__ import annotations

import hashlib
from pathlib import Path


def service_code_fingerprint(root: Path | None = None) -> str:
    """Return a deterministic identity for the local realtime-QA Python code."""

    services_root = root or Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(services_root.rglob("*.py")):
        relative = path.relative_to(services_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
