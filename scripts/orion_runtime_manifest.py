"""Verify a frozen ORION runtime source/resource manifest without importing the app."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ALGORITHM = "sha256-path-content-v1"
MANIFEST_PATH = "contracts/runtime-source-manifest.json"
FORBIDDEN = {".git", ".venv", "node_modules", "__pycache__", "data", "state", ".orion-workflows", ".orion-runtime"}
SECRET = re.compile(r"^(?:\.env(?:\..*)?|\.credentials(?:\..*)?|\.npmrc|\.netrc|id_rsa|id_ed25519|.*\.(?:pem|key|p12|pfx))$", re.I)


def source_file(root: Path, value: str) -> Path:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value.startswith("/") or any(part in {"", ".", ".."} | FORBIDDEN
                                           or SECRET.fullmatch(part) for part in value.split("/"))):
        raise ValueError("unsafe runtime source path")
    current = root
    for part in value.split("/"):
        current /= part
        if current.is_symlink():
            raise ValueError("runtime source contains a symlink")
    if not current.is_file() or current.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("runtime source is missing or exceeds its size limit")
    return current


def verify_runtime(root: Path) -> dict[str, Any]:
    root = root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("runtime root must be a regular directory")
    manifest = json.loads(source_file(root, MANIFEST_PATH).read_text(encoding="utf-8"))
    if (manifest.get("schemaVersion") != 1 or manifest.get("algorithm") != ALGORITHM
            or not isinstance(manifest.get("files"), list)
            or not isinstance(manifest.get("required"), list)
            or not re.fullmatch(r"[a-f0-9]{64}", manifest.get("fingerprint", ""))):
        raise ValueError("invalid runtime source manifest")
    records, seen = [], set()
    for record in manifest["files"]:
        value = record.get("path")
        if value in seen or not re.fullmatch(r"[a-f0-9]{64}", record.get("sha256", "")):
            raise ValueError("invalid or duplicate runtime source record")
        if not isinstance(record.get("bytes"), int) or isinstance(record.get("bytes"), bool) or record["bytes"] < 0:
            raise ValueError("invalid runtime source size")
        body = source_file(root, value).read_bytes()
        checksum = hashlib.sha256(body).hexdigest()
        if len(body) != record["bytes"] or checksum != record["sha256"]:
            raise ValueError("runtime source content has changed: " + value)
        seen.add(value)
        records.append((value, checksum))
    for value in manifest["required"]:
        source_file(root, value)
        if value not in seen:
            raise ValueError("required runtime source is outside the manifest")
    digest = hashlib.sha256()
    for value, checksum in sorted(records):
        digest.update((value + "\x00" + checksum + "\n").encode())
    if digest.hexdigest() != manifest["fingerprint"]:
        raise ValueError("runtime source fingerprint mismatch")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = verify_runtime(args.root)
    print(json.dumps({"valid": True, "runtimeVersion": manifest["runtimeVersion"],
                      "algorithm": manifest["algorithm"], "fingerprint": manifest["fingerprint"],
                      "fileCount": len(manifest["files"]), "resourcesVerified": True,
                      "appImported": False, "serviceStarted": False}))


if __name__ == "__main__":
    main()
