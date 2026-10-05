"""Read-only ZIP export of one manifest-bound, published engineering package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .delivery_packages import _assert_no_credential_fields

MAX_FILES = 2048
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
ROOT_FILES = {
    "manifest.json",
    "package-contract.json",
    "工程包与发布包说明.md",
    "发布说明.md",
    "打开查看.html",
}
ROOT_DIRS = {"01-本体模型", "02-工程定义", "03-质量结论", "04-发布信息", "05-运行时", "06-工程追溯"}
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _expected_sha(value):
    value = str(value or "").removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("发布包缺少有效 SHA-256")
    return value


def _parts(relative):
    if (
        not relative
        or "\\" in relative
        or "\0" in relative
        or any(p in {"", ".", ".."} for p in relative.split("/"))
    ):
        raise ValueError("发布清单包含非法路径")
    path = PurePosixPath(relative)
    if path.is_absolute() or path.as_posix() != relative:
        raise ValueError("发布清单包含越界路径")
    return path.parts


@contextmanager
def _open_beneath(root_fd, relative):
    """Open every ancestor with NOFOLLOW, including during concurrent changes."""
    parts = _parts(relative)
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("发布资产必须为普通文件")
            yield source
    finally:
        os.close(directory)


def _read(root_fd, path):
    with _open_beneath(root_fd, path) as source:
        data = source.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("发布元数据超过下载校验限制")
    return data


def _assert_deliverable(path):
    parts = _parts(path)
    if path not in ROOT_FILES and (len(parts) < 2 or parts[0] not in ROOT_DIRS):
        raise ValueError("发布清单含非正式交付目录")
    if any(p.startswith(".") for p in parts) or Path(path).suffix.lower() in {
        ".properties",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
    }:
        raise ValueError("发布清单包含运行配置或凭据文件")


def _verify_members(root_fd, files):
    directories = {"."}
    for path in files:
        directories.update(str(parent) for parent in PurePosixPath(path).parents)
    observed = set()

    def visit(fd, prefix=""):
        for name in os.listdir(fd):
            relative = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            # Finder metadata is not a release asset and is never added to the ZIP.
            # Keep rejecting directories and symlinks even with this exact name.
            if name == ".DS_Store" and stat.S_ISREG(info.st_mode) and relative not in files:
                continue
            if stat.S_ISREG(info.st_mode) and relative in files:
                observed.add(relative)
            elif stat.S_ISDIR(info.st_mode) and relative in directories:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, relative + "/")
                finally:
                    os.close(child)
            else:
                raise ValueError("发布包含未声明文件、目录或符号链接")

    visit(root_fd)
    if observed != files:
        raise ValueError("发布包文件与清单不完整一致")


def write_verified_release_zip(
    project_dir: Path, release_version: str, target: Path, expected_manifest_sha256: str
) -> dict:
    """Stage outside the project, verify every byte, then permit HTTP streaming."""
    if not VERSION.fullmatch(release_version):
        raise ValueError("发布版本格式不合法")
    project_dir = project_dir.absolute()
    if target.resolve().is_relative_to(project_dir.resolve()):
        raise ValueError("下载临时归档不得写入正式工程")
    if target.exists() or target.is_symlink():
        raise ValueError("下载临时归档目标必须为新文件")
    root_fd = os.open(project_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    package_fd = None
    try:
        state_bytes = _read(root_fd, "workflow-state.json")
        publication_bytes = _read(root_fd, "07-release/publication.json")
        state, publication = json.loads(state_bytes), json.loads(publication_bytes)
        if (
            state.get("project_status") != "PUBLISHED"
            or state.get("project_id") != project_dir.name
        ):
            raise ValueError("仅允许下载该工程已发布的完整版本")
        if (
            publication.get("project_id") != project_dir.name
            or publication.get("release_version") != release_version
        ):
            raise ValueError("请求版本与正式发布身份不一致")
        expected = _expected_sha(expected_manifest_sha256)
        if _expected_sha(publication.get("package_manifest_sha256")) != expected:
            raise ValueError("发布清单指纹与下载请求不一致")
        package_name = f"ontology-engineering-package-{release_version}"
        release_fd = os.open(
            "07-release", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        try:
            package_fd = os.open(
                package_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=release_fd
            )
        finally:
            os.close(release_fd)
        manifest_bytes = _read(package_fd, "manifest.json")
        if _sha(manifest_bytes) != expected:
            raise ValueError("发布清单已漂移，拒绝下载")
        manifest = json.loads(manifest_bytes)
        if (
            manifest.get("project_id") != project_dir.name
            or manifest.get("release_version") != release_version
        ):
            raise ValueError("包内清单身份与发布版本不一致")
        entries = {}
        for item in manifest.get("files") or []:
            path = str(item.get("path") or "")
            _assert_deliverable(path)
            if path == "manifest.json" or path in entries:
                raise ValueError("发布清单包含重复文件")
            entries[path] = _expected_sha(item.get("sha256"))
        if not entries or len(entries) > MAX_FILES or manifest.get("file_count") != len(entries):
            raise ValueError("发布清单文件数量不合法")
        if not {
            "01-本体模型/ontology.owl",
            "01-本体模型/ontology.ttl",
            "04-发布信息/publication.json",
            "04-发布信息/release-snapshot.json",
            "03-质量结论/competency-question-report.json",
        }.issubset(entries):
            raise ValueError("发布清单缺少必需正式资产")
        _verify_members(package_fd, set(entries) | {"manifest.json"})
        for identity_path in ("04-发布信息/publication.json", "04-发布信息/release-snapshot.json"):
            identity = json.loads(_read(package_fd, identity_path))
            if (
                identity.get("project_id") != project_dir.name
                or identity.get("release_version") != release_version
            ):
                raise ValueError("包内发布回执身份不一致")
        total = 0
        with zipfile.ZipFile(
            target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for relative, expected_file in sorted({**entries, "manifest.json": expected}.items()):
                with _open_beneath(package_fd, relative) as source:
                    before = os.fstat(source.fileno())
                    total += before.st_size
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError("完整工程包超过 512 MiB 下载限制")
                    metadata = None
                    if Path(relative).suffix in {".json", ".yaml", ".yml"}:
                        metadata = source.read(MAX_METADATA_BYTES + 1)
                        if len(metadata) > MAX_METADATA_BYTES:
                            raise ValueError("发布元数据超过下载校验限制")
                        try:
                            _assert_no_credential_fields(metadata, relative)
                        except Exception as exc:
                            raise ValueError(f"发布文件的凭据或格式校验未通过：{relative}") from exc
                        source.seek(0)
                    digest = hashlib.sha256()
                    previous = b""
                    with archive.open(
                        f"{package_name}/{relative}", "w", force_zip64=True
                    ) as destination:
                        while chunk := source.read(1024 * 1024):
                            scan = previous + chunk
                            if re.search(
                                rb"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", scan, re.I
                            ) or re.search(
                                rb"(?im)^\s*(?:jdbc[._-]?)?(?:password|passwd|api[._-]?key|access[._-]?token)\s*[=:]\s*\S+",
                                scan,
                            ):
                                raise ValueError("发布文件包含运行凭据，拒绝下载")
                            previous = scan[-8192:]
                            digest.update(chunk)
                            destination.write(chunk)
                    after = os.fstat(source.fileno())
                    if digest.hexdigest() != expected_file or (
                        before.st_size,
                        before.st_mtime_ns,
                    ) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("发布文件已漂移，拒绝下载")
            # Verify the source is still the same release after archive assembly.
            if (
                _sha(_read(package_fd, "manifest.json")) != expected
                or _read(root_fd, "07-release/publication.json") != publication_bytes
                or json.loads(_read(root_fd, "workflow-state.json")).get("project_status")
                != "PUBLISHED"
            ):
                raise ValueError("下载过程中发布身份发生变化")
            _verify_members(package_fd, set(entries) | {"manifest.json"})
            for relative, expected_file in entries.items():
                with _open_beneath(package_fd, relative) as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != expected_file:
                    raise ValueError("归档完成前来源文件发生漂移")
        return {
            "project_id": project_dir.name,
            "release_version": release_version,
            "manifest_sha256": expected,
            "files": len(entries) + 1,
            "source_bytes": total,
            "archive_bytes": target.stat().st_size,
        }
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if package_fd is not None:
            os.close(package_fd)
        os.close(root_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = write_verified_release_zip(
            args.project_dir, args.release_version, args.output, args.manifest_sha256
        )
    except Exception as exc:
        raise SystemExit(f"完整工程包下载校验失败：{exc}") from exc
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
