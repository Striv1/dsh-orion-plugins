"""Authenticate platform-generated S6 results stored beside existing preflight payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

RECEIPT_VERSION = "s6-validation-receipt-v1"


def _key(root: Path, *, create: bool) -> bytes:
    path = root / ".s6-validation-receipt.key"
    if create and not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".s6-receipt-key-", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("S6 验证回执密钥必须是平台私有文件。")
        value = stream.read(33)
    if len(value) != 32:
        raise ValueError("S6 验证回执密钥不可用，请重新预检。")
    return value


def _signed_content(receipt: dict[str, Any]) -> bytes:
    # Bind the existing one-use token, identity, TTL and all validated content.
    # Large payload/metrics remain stored once; their digests are independently
    # verified before using the result.
    content = {
        key: receipt.get(key)
        for key in (
            "preflight_id", "project_id", "project_revision", "stage",
            "payload_sha256", "token_sha256", "created_at", "expires_at",
            "s6_validation_receipt",
        )
    }
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def seal(root: Path, receipt: dict[str, Any]) -> str:
    return hmac.new(_key(root, create=True), _signed_content(receipt), hashlib.sha256).hexdigest()


def verify(root: Path, receipt: dict[str, Any]) -> bool:
    try:
        expected = hmac.new(_key(root, create=False), _signed_content(receipt), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(receipt.get("s6_validation_seal") or ""))
    except (OSError, ValueError):
        return False
