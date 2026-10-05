"""Version- and session-scoped analytical knowledge, reports and evaluation cases.

These append-only assets contain data, never executable instructions. User confirmation
is not ontology/business approval. Callers must verify every evidence reference using
EvidenceReceiptStore and verify the current release before accepting a mutation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "orion-analysis-asset-v1"
CONFIRMATION = "USER_CONFIRMED_NOT_BUSINESS_APPROVED"
UNAPPROVED = "NOT_BUSINESS_APPROVED"
IDENTITY_FIELDS = ("project_id", "release_version", "release_fingerprint", "session_id")
ASSET_ID = re.compile(r"^ANA-[a-f0-9]{32}$")
SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
SESSION_ID = re.compile(r"^session-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
KINDS = frozenset({"memory", "knowledge", "report", "evaluation"})
EVALUATION_BASIS_KINDS = frozenset({
    "FIXED_ACCEPTANCE_DATASET", "PUBLISHED_SNAPSHOT", "DYNAMIC_LIVE_SOURCE", "UNKNOWN",
})
MAX_ASSET_BYTES = 256 * 1024
MAX_ASSETS_PER_SCOPE = 1000
_CREDENTIAL_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "clientsecret", "apikey", "accesstoken",
    "refreshtoken", "authorization", "privatekey", "credentials", "connectionstring",
    "databaseurl", "dsn", "connectioninfo",
})
_CREDENTIAL_VALUE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@|"
    r"\b(?:password|passwd|pwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9_.~+/-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class AnalysisAssetError(ValueError):
    """An asset is invalid, outside the active scope, or unavailable."""


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AnalysisAssetError("asset must contain finite JSON data") from exc


def result_digest(rows: list[dict[str, Any]]) -> str:
    """Hash actual JSON rows, preserving row order, types and unknown/null values."""
    if (not isinstance(rows, list) or len(rows) > 100000
            or any(not isinstance(row, dict) or len(row) > 200 for row in rows)):
        raise AnalysisAssetError("evaluation rows must be a list of objects")
    total = 0
    digest = hashlib.sha256()
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(rows):
            encoded = chunk.encode("utf-8")
            total += len(encoded)
            if total > 16 * 1024 * 1024:
                raise AnalysisAssetError("evaluation result exceeds 16 MiB")
            digest.update(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise AnalysisAssetError("evaluation result must be bounded finite JSON data") from exc
    return "sha256:" + digest.hexdigest()


def _text(value: Any, name: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AnalysisAssetError(f"invalid {name}")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise AnalysisAssetError(f"invalid control character in {name}")
    if _CREDENTIAL_VALUE.search(value):
        raise AnalysisAssetError("credential material must not be stored in analysis assets")
    return value.strip()


def _data(value: Any, name: str, maximum_bytes: int, *, require_object: bool = False) -> Any:
    if require_object and (not isinstance(value, dict) or not value):
        raise AnalysisAssetError(f"{name} must be a non-empty JSON object")
    nodes = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 10 or nodes > 20000:
            raise AnalysisAssetError(f"{name} is too complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise AnalysisAssetError(f"invalid key in {name}")
                normalized = re.sub(r"[^a-z]", "", key.lower())
                if normalized in _CREDENTIAL_KEYS:
                    raise AnalysisAssetError("credential fields must not be stored in analysis assets")
                walk(key, depth + 1)
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > 32768 or _CREDENTIAL_VALUE.search(item):
                raise AnalysisAssetError(f"unsafe or oversized text in {name}")
        elif item is not None and type(item) not in (bool, int, float):
            raise AnalysisAssetError(f"invalid JSON value in {name}")
        elif isinstance(item, float) and not math.isfinite(item):
            raise AnalysisAssetError(f"non-finite number in {name}")

    walk(value, 0)
    encoded = _json_bytes(value)
    if len(encoded) > maximum_bytes:
        raise AnalysisAssetError(f"{name} is too large")
    return json.loads(encoded)


def _identity(value: dict[str, Any]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise AnalysisAssetError("analysis identity requires project, version, fingerprint and session")
    if (not NAME.fullmatch(str(value.get("project_id") or ""))
            or not NAME.fullmatch(str(value.get("release_version") or ""))
            or not SHA256.fullmatch(str(value.get("release_fingerprint") or ""))
            or not SESSION_ID.fullmatch(str(value.get("session_id") or ""))
            or any(not isinstance(value[field], str) for field in IDENTITY_FIELDS)):
        raise AnalysisAssetError("invalid analysis identity")
    return {field: value[field] for field in IDENTITY_FIELDS}


def _reference(value: dict[str, Any], identity: dict[str, str]) -> dict[str, str]:
    if (not isinstance(value, dict)
            or any(value.get(field) != identity[field] for field in IDENTITY_FIELDS)
            or not re.fullmatch(r"EVD-[a-f0-9]{32}", str(value.get("receipt_id") or ""))
            or not SHA256.fullmatch(str(value.get("sha256") or ""))
            or not NAME.fullmatch(str(value.get("query_id") or ""))):
        raise AnalysisAssetError("receipt reference does not match analysis identity")
    return {field: value[field] for field in (*IDENTITY_FIELDS, "receipt_id", "sha256", "query_id")}


def _references(values: list[dict[str, Any]], identity: dict[str, str]) -> list[dict[str, str]]:
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise AnalysisAssetError("an asset requires between 1 and 50 evidence references")
    refs = [_reference(value, identity) for value in values]
    if len({ref["receipt_id"] for ref in refs}) != len(refs):
        raise AnalysisAssetError("duplicate evidence references")
    return refs


def _tokens(text: str) -> Counter[str]:
    """Transparent lexical matching; this is deliberately not embedding retrieval."""
    tokens = re.findall(r"[a-z0-9_]+", text.casefold())
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(segment[index:index + 2] for index in range(len(segment) - 1))
        if len(segment) == 1:
            tokens.append(segment)
    return Counter(tokens)


def _evaluation_basis(value: dict[str, Any] | None) -> dict[str, Any]:
    """The API attests provenance; the asset store never infers immutable data."""
    if value is None:
        return {"kind": "UNKNOWN"}
    value = _data(value, "evaluation basis", 4096, require_object=True)
    if (value.get("kind") not in EVALUATION_BASIS_KINDS
            or set(value) - {"kind", "dataset_id", "version", "sha256", "note_zh"}):
        raise AnalysisAssetError("invalid evaluation data basis")
    for field in ("dataset_id", "version"):
        if field in value and not NAME.fullmatch(str(value[field])):
            raise AnalysisAssetError("invalid evaluation dataset identity")
    if "sha256" in value and not SHA256.fullmatch(str(value["sha256"])):
        raise AnalysisAssetError("invalid evaluation dataset fingerprint")
    if (value["kind"] in {"FIXED_ACCEPTANCE_DATASET", "PUBLISHED_SNAPSHOT"}
            and not all(value.get(field) for field in ("dataset_id", "version", "sha256"))):
        raise AnalysisAssetError("fixed evaluation data requires dataset, version and fingerprint")
    return value


class AnalysisAssetStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().absolute()

    def _directory(self, identity: dict[str, str], *, create: bool = False) -> int:
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        scope = hashlib.sha256(_json_bytes(identity)).hexdigest()
        try:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(scope, mode=0o700, dir_fd=root)
                os.fsync(root)
            return os.open(scope, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        finally:
            os.close(root)

    def _save(self, identity: dict[str, str], kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        asset = {
            "schema_version": SCHEMA_VERSION, "asset_id": "ANA-" + uuid4().hex,
            "kind": kind, "identity": identity, "created_at": datetime.now(UTC).isoformat(),
            "approval_status": CONFIRMATION if kind in {"memory", "knowledge"} else UNAPPROVED,
            "payload": payload,
        }
        checksum = "sha256:" + hashlib.sha256(_json_bytes(asset)).hexdigest()
        envelope = {**asset, "sha256": checksum}
        content = _json_bytes(envelope)
        if len(content) > MAX_ASSET_BYTES:
            raise AnalysisAssetError("analysis asset is too large")
        directory = self._directory(identity, create=True)
        temporary = "." + uuid4().hex + ".tmp"
        lock = None
        try:
            # Initialize once so concurrent creators use the explicit
            # FileExistsError path before opening the stable lock file.
            try:
                lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=directory)
            except FileExistsError:
                lock = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise AnalysisAssetError("asset lock is not a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            if len(self._names(directory)) >= MAX_ASSETS_PER_SCOPE:
                raise AnalysisAssetError("analysis asset scope is full")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, asset["asset_id"] + ".json", src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            if lock is not None:
                os.close(lock)
            os.close(directory)
        return envelope

    @staticmethod
    def _names(directory: int) -> list[str]:
        names = [name for name in os.listdir(directory)
                 if name.endswith(".json") and ASSET_ID.fullmatch(name[:-5])]
        if len(names) > MAX_ASSETS_PER_SCOPE:
            raise AnalysisAssetError("analysis asset scope exceeds its safe listing limit")
        return names

    @staticmethod
    def _read(directory: int, identity: dict[str, str], asset_id: str) -> dict[str, Any]:
        fd = os.open(asset_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ASSET_BYTES:
                raise AnalysisAssetError("invalid analysis asset file")
            content = stream.read(MAX_ASSET_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise AnalysisAssetError("analysis asset changed during read")
        envelope = json.loads(content)
        if not isinstance(envelope, dict):
            raise AnalysisAssetError("invalid analysis asset envelope")
        checksum = envelope.get("sha256")
        asset = {key: value for key, value in envelope.items() if key != "sha256"}
        if (len(content) > MAX_ASSET_BYTES or asset.get("asset_id") != asset_id
                or asset.get("schema_version") != SCHEMA_VERSION or asset.get("identity") != identity
                or asset.get("kind") not in KINDS
                or asset.get("approval_status") != (CONFIRMATION if asset.get("kind") in {"memory", "knowledge"} else UNAPPROVED)
                or checksum != "sha256:" + hashlib.sha256(_json_bytes(asset)).hexdigest()):
            raise AnalysisAssetError("analysis asset integrity or identity does not match")
        return envelope

    def save_memory(self, identity: dict[str, Any], question: str, query: dict[str, Any],
                    receipt_ref: dict[str, Any], confirmed: bool) -> dict[str, Any]:
        identity = _identity(identity)
        if confirmed is not True:
            raise AnalysisAssetError("query memory requires explicit user confirmation")
        return self._save(identity, "memory", {
            "question": _text(question, "question"),
            "query": _data(query, "query", 64 * 1024, require_object=True),
            "receipt_ref": _reference(receipt_ref, identity), "user_confirmed": True,
            "execution_policy": "REFERENCE_ONLY_REVALIDATE_BEFORE_EXECUTION",
        })

    def save_knowledge(self, identity: dict[str, Any], title: str, definition: str,
                       receipt_refs: list[dict[str, Any]], confirmed: bool) -> dict[str, Any]:
        identity = _identity(identity)
        if confirmed is not True:
            raise AnalysisAssetError("analysis knowledge requires explicit user confirmation")
        return self._save(identity, "knowledge", {
            "title": _text(title, "title", 200), "definition": _text(definition, "definition", 10000),
            "receipt_refs": _references(receipt_refs, identity), "user_confirmed": True,
            "execution_policy": "REFERENCE_ONLY_NOT_A_BUSINESS_RULE",
        })

    def save_report(self, identity: dict[str, Any], title: str,
                    receipt_refs: list[dict[str, Any]], layout: dict[str, Any]) -> dict[str, Any]:
        identity = _identity(identity)
        return self._save(identity, "report", {
            "title": _text(title, "title", 200), "receipt_refs": _references(receipt_refs, identity),
            "layout": _data(layout, "layout", 32 * 1024, require_object=True),
            "execution_policy": "RENDER_VERIFIED_RECEIPTS_ONLY",
        })

    def save_evaluation(self, identity: dict[str, Any], name: str, question: str,
                        query: dict[str, Any], expected_result_sha256: str, expected_row_count: int,
                        receipt_ref: dict[str, Any], *, basis: dict[str, Any] | None = None) -> dict[str, Any]:
        identity = _identity(identity)
        if (not isinstance(expected_result_sha256, str) or not SHA256.fullmatch(expected_result_sha256)
                or type(expected_row_count) is not int or not 0 <= expected_row_count <= 10**12):
            raise AnalysisAssetError("invalid evaluation expectation")
        return self._save(identity, "evaluation", {
            "name": _text(name, "name", 200), "question": _text(question, "question"),
            "query": _data(query, "query", 64 * 1024, require_object=True),
            "expected_result_sha256": expected_result_sha256, "expected_row_count": expected_row_count,
            "receipt_ref": _reference(receipt_ref, identity), "row_order": "SIGNIFICANT",
            "execution_policy": "COMPARE_ACTUAL_RESULTS_ONLY",
            "evaluation_basis": _evaluation_basis(basis),
        })

    def get(self, identity: dict[str, Any], asset_id: str,
            expected_sha256: str | None = None) -> dict[str, Any]:
        identity = _identity(identity)
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            raise AnalysisAssetError("invalid analysis asset id")
        try:
            directory = self._directory(identity)
            try:
                asset = self._read(directory, identity, asset_id)
            finally:
                os.close(directory)
            if expected_sha256 is not None and asset["sha256"] != expected_sha256:
                raise AnalysisAssetError("analysis asset checksum does not match reference")
            return asset
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AnalysisAssetError("analysis asset is invalid or unavailable in this scope") from exc

    def list_assets(self, identity: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
        identity = _identity(identity)
        if kind is not None and kind not in KINDS:
            raise AnalysisAssetError("invalid analysis asset kind")
        try:
            directory = self._directory(identity)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise AnalysisAssetError("analysis asset scope is unavailable") from exc
        try:
            assets = [self._read(directory, identity, name[:-5]) for name in self._names(directory)]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AnalysisAssetError("analysis asset scope contains an invalid asset") from exc
        finally:
            os.close(directory)
        return sorted((asset for asset in assets if kind is None or asset["kind"] == kind),
                      key=lambda asset: (asset["created_at"], asset["asset_id"]), reverse=True)

    def _search(self, identity: dict[str, Any], question: str, kind: str,
                limit: int) -> list[dict[str, Any]]:
        question = _text(question, "question")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise AnalysisAssetError("search limit must be between 1 and 50")
        query_tokens = _tokens(question)
        matches = []
        for asset in self.list_assets(identity, kind):
            payload = asset["payload"]
            text = payload["question"] if kind == "memory" else payload["title"] + " " + payload["definition"]
            candidate = _tokens(text)
            overlap = query_tokens & candidate
            if not overlap:
                continue
            denominator = math.sqrt(sum(value * value for value in query_tokens.values())
                                    * sum(value * value for value in candidate.values()))
            score = sum(query_tokens[token] * candidate[token] for token in overlap) / denominator
            matches.append({"asset": asset, "score": round(score, 6),
                            "match_method": "CHINESE_BIGRAM_ENGLISH_TOKEN_COSINE",
                            "matched_terms": sorted(overlap), "semantic_embedding": False})
        matches.sort(key=lambda match: (match["score"], match["asset"]["created_at"]), reverse=True)
        return matches[:limit]

    def search_memories(self, identity: dict[str, Any], question: str,
                        limit: int = 10) -> list[dict[str, Any]]:
        return self._search(identity, question, "memory", limit)

    def search_knowledge(self, identity: dict[str, Any], question: str,
                         limit: int = 10) -> list[dict[str, Any]]:
        return self._search(identity, question, "knowledge", limit)

    def evaluate(self, identity: dict[str, Any], asset_id: str,
                 rows: list[dict[str, Any]], *, actual_basis: dict[str, Any] | None = None) -> dict[str, Any]:
        asset = self.get(identity, asset_id)
        if asset["kind"] != "evaluation":
            raise AnalysisAssetError("asset is not an evaluation case")
        actual_hash = result_digest(rows)
        expected = asset["payload"]
        matches_hash = actual_hash == expected["expected_result_sha256"]
        matches_count = len(rows) == expected["expected_row_count"]
        baseline = _evaluation_basis(expected.get("evaluation_basis"))
        current = _evaluation_basis(actual_basis)
        fixed_kinds = {"FIXED_ACCEPTANCE_DATASET", "PUBLISHED_SNAPSHOT"}
        same_fixed_data = (baseline["kind"] in fixed_kinds and current["kind"] in fixed_kinds
                           and all(baseline[field] == current[field] for field in ("dataset_id", "version", "sha256")))
        same_result = matches_hash and matches_count
        if same_fixed_data:
            status = "PASSED" if same_result else "FAILED"
            interpretation = "相同固定数据上的结果比较；差异需要调查，不能单凭结果哈希推定错误原因。"
        elif baseline["kind"] == "DYNAMIC_LIVE_SOURCE" and current["kind"] == "DYNAMIC_LIVE_SOURCE":
            status = "OBSERVATION_MATCH" if same_result else "DATA_CHANGED"
            interpretation = "动态源的两次查询观察；结果变化不代表模型回归失败。正式回归验收需固定数据集。"
        else:
            status = "INCOMPARABLE"
            interpretation = "未证明两次查询使用相同固定数据，不能据此签发回归通过或失败结论。"
        return {
            "asset_id": asset_id, "asset_sha256": asset["sha256"], "identity": asset["identity"],
            "status": status, "regression_comparable": same_fixed_data,
            "baseline_basis": baseline, "actual_basis": current, "interpretation_zh": interpretation,
            "actual_row_count": len(rows), "expected_row_count": expected["expected_row_count"],
            "actual_result_sha256": actual_hash, "expected_result_sha256": expected["expected_result_sha256"],
            "hash_matches": matches_hash, "count_matches": matches_count,
            "row_order": "SIGNIFICANT", "approval_status": asset["approval_status"],
        }
