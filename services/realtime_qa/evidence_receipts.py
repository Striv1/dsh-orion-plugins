"""Immutable local receipts for already executed, session-bound Q&A evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from services.realtime_qa.models import RealtimeSessionAnswer

RECEIPT_ID = re.compile(r"^EVD-[a-f0-9]{32}$")
SESSION_ID = re.compile(r"^session-[a-f0-9-]{36}$")
SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
IDENTITY_FIELDS = ("session_id", "query_id", "project_id", "release_version", "release_fingerprint")


class EvidenceReceiptError(ValueError):
    pass


class EvidenceReceiptStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(os.getenv(
            "ORION_REALTIME_EVIDENCE_ROOT",
            str(Path(__file__).resolve().parents[2] / ".orion-runtime/realtime-qa-evidence"),
        )).expanduser()

    def _directory(self, *, create: bool = False) -> int:
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Only platform-owned opaque basenames are opened relative to this fd.
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def save(self, response: RealtimeSessionAnswer) -> dict[str, Any]:
        bundle = response.answer.evidence_bundle
        release = bundle.release
        if not SESSION_ID.fullmatch(response.session_id):
            raise EvidenceReceiptError("invalid receipt session")
        receipt_id = "EVD-" + uuid4().hex
        envelope = {
            "schema_version": "orion-evidence-receipt-v1",
            "receipt_id": receipt_id,
            "session_id": response.session_id,
            "query_id": bundle.query_id,
            "project_id": release.project_id,
            "release_version": release.release_version,
            "release_fingerprint": release.release_fingerprint,
            "created_at": datetime.now(UTC).isoformat(),
            "response": response.model_dump(mode="json", exclude={"evidence_receipt"}),
        }
        content = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        checksum = "sha256:" + hashlib.sha256(content).hexdigest()
        directory = self._directory(create=True)
        temporary = "." + uuid4().hex + ".tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # link is atomic and refuses an existing receipt; no overwrite path.
            os.link(temporary, receipt_id + ".json", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)
        return {
            "schema_version": envelope["schema_version"],
            "receipt_id": receipt_id,
            "sha256": checksum,
            "byte_count": len(content),
            **{field: envelope[field] for field in IDENTITY_FIELDS},
            "created_at": envelope["created_at"],
            "stored_full_response": True,
        }

    def read(self, reference: dict[str, Any]) -> bytes:
        receipt_id = reference.get("receipt_id")
        if (not isinstance(receipt_id, str) or not RECEIPT_ID.fullmatch(receipt_id)
                or not SESSION_ID.fullmatch(str(reference.get("session_id") or ""))
                or not SHA256.fullmatch(str(reference.get("sha256") or ""))
                or not SHA256.fullmatch(str(reference.get("release_fingerprint") or ""))
                or any(not isinstance(reference.get(field), str) or not reference[field] for field in IDENTITY_FIELDS)):
            raise EvidenceReceiptError("receipt reference is invalid or unavailable")
        try:
            directory = self._directory()
            try:
                fd = os.open(receipt_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise EvidenceReceiptError("receipt is not a regular file")
                content = stream.read()
                after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise EvidenceReceiptError("receipt changed during read")
            if "sha256:" + hashlib.sha256(content).hexdigest() != reference["sha256"]:
                raise EvidenceReceiptError("receipt integrity check failed")
            envelope = json.loads(content)
            if (envelope.get("receipt_id") != receipt_id
                    or envelope.get("schema_version") != "orion-evidence-receipt-v1"
                    or any(envelope.get(field) != reference[field] for field in IDENTITY_FIELDS)):
                raise EvidenceReceiptError("receipt identity does not match")
            response = envelope.get("response") or {}
            bundle = (response.get("answer") or {}).get("evidence_bundle") or {}
            release = bundle.get("release") or {}
            if (response.get("session_id") != reference["session_id"] or bundle.get("query_id") != reference["query_id"]
                    or any(release.get(field) != reference[field] for field in ("project_id", "release_version", "release_fingerprint"))):
                raise EvidenceReceiptError("receipt payload identity does not match")
            return content
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceReceiptError("receipt reference is invalid or unavailable") from exc


def persist_session_answer(store: EvidenceReceiptStore, response: RealtimeSessionAnswer) -> RealtimeSessionAnswer:
    try:
        receipt = store.save(response)
    except (OSError, ValueError) as exc:
        # Never advertise a complete audit receipt when durable storage failed.
        raise HTTPException(503, "完整问答证据回执未能持久化，未签发详情引用。") from exc
    return response.model_copy(update={"evidence_receipt": receipt})


def register_evidence_receipt_api(app: FastAPI, store: EvidenceReceiptStore) -> None:
    @app.get("/ontology/realtime/evidence-receipts/{receipt_id}")
    def read_evidence_receipt(
        receipt_id: str, session_id: str, query_id: str, project_id: str,
        release_version: str, release_fingerprint: str, sha256: str,
    ) -> Response:
        reference = {
            "receipt_id": receipt_id, "session_id": session_id, "query_id": query_id,
            "project_id": project_id, "release_version": release_version,
            "release_fingerprint": release_fingerprint, "sha256": sha256,
        }
        try:
            content = store.read(reference)
        except EvidenceReceiptError as exc:
            raise HTTPException(404, "未找到完整性与会话、查询和发布身份一致的证据回执。") from exc
        return Response(content, media_type="application/json", headers={"Cache-Control": "no-store"})
