#!/usr/bin/env python3
"""Install and verify the immutable shared groundwater dataset."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_SHARED_ROOT = Path(
    "/home/inspur/myenv/runtime/doc-agent-shared/groundwater"
)
DEFAULT_ALIAS = "liutang"
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
REQUIRED_MANIFEST_KEYS = {
    "schema_version",
    "project",
    "data_version",
    "datasets",
    "record_counts",
}


class SharedDataError(RuntimeError):
    """A shared-data integrity or safety condition failed."""


def _record(digest: Any, kind: bytes, relative: Path) -> None:
    encoded = os.fsencode(relative.as_posix())
    digest.update(kind)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _copy_hash(source: BinaryIO, digest: Any) -> None:
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)


def tree_digest(root: Path) -> str:
    """Return a deterministic digest of paths, file sizes, and file contents."""

    if root.is_symlink() or not root.is_dir():
        raise SharedDataError("dataset root must be a regular directory")
    digest = hashlib.sha256(b"radai-groundwater-tree-v1\0")
    entries = sorted(
        root.rglob("*"),
        key=lambda path: os.fsencode(path.relative_to(root).as_posix()),
    )
    for path in entries:
        relative = path.relative_to(root)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SharedDataError(f"dataset contains a symbolic link: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            _record(digest, b"D", relative)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SharedDataError(f"dataset contains an unsupported entry: {relative}")
        _record(digest, b"F", relative)
        digest.update(metadata.st_size.to_bytes(8, "big"))
        with path.open("rb") as source:
            _copy_hash(source, digest)
    return digest.hexdigest()


def validate_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SharedDataError("dataset must contain a regular manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SharedDataError("dataset manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or not REQUIRED_MANIFEST_KEYS.issubset(manifest):
        raise SharedDataError("dataset manifest contract is incomplete")
    if not isinstance(manifest.get("project"), dict):
        raise SharedDataError("dataset manifest project must be an object")
    return manifest


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise SharedDataError(f"shared-data path is not a regular directory: {path}")
    if path.stat().st_uid != os.getuid():
        raise SharedDataError(f"shared-data path is not owned by the current user: {path}")
    os.chmod(path, 0o700)


def _make_tree_read_only(root: Path) -> None:
    entries = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in entries:
        if path.is_symlink():
            raise SharedDataError("copied dataset unexpectedly contains a symbolic link")
        os.chmod(path, 0o550 if path.is_dir() else 0o440)
    os.chmod(root, 0o550)


def _assert_tree_read_only(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SharedDataError("installed dataset contains a symbolic link")
        if stat.S_IMODE(metadata.st_mode) & 0o222:
            raise SharedDataError("installed dataset contains a writable entry")


def _remove_staging_tree(root: Path) -> None:
    """Restore owner write bits only on a verified staging path, then remove it."""

    for directory, directory_names, file_names in os.walk(root):
        os.chmod(directory, 0o700)
        for name in directory_names:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
        for name in file_names:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, 0o600)
    shutil.rmtree(root)


def _validate_alias(alias: str) -> None:
    if not ALIAS_PATTERN.fullmatch(alias):
        raise SharedDataError("dataset alias contains unsupported characters")


def _resolved_shared_root(path: Path, *, create: bool) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise SharedDataError("shared root must be an absolute path")
    if create:
        _ensure_private_directory(expanded)
    if expanded.is_symlink() or not expanded.is_dir():
        raise SharedDataError("shared root must be a regular directory")
    return expanded.resolve(strict=True)


def verify_dataset(shared_root: Path, alias: str = DEFAULT_ALIAS) -> dict[str, Any]:
    _validate_alias(alias)
    root = _resolved_shared_root(shared_root, create=False)
    versions = root / "versions"
    alias_path = root / alias
    if not alias_path.is_symlink():
        raise SharedDataError("stable dataset path must be a symbolic link")
    target = alias_path.resolve(strict=True)
    try:
        target.relative_to(versions.resolve(strict=True))
    except ValueError as exc:
        raise SharedDataError("stable dataset link escapes the versions directory") from exc
    validate_manifest(target)
    digest = tree_digest(target)
    if target.name != digest:
        raise SharedDataError("dataset version directory does not match its content digest")
    _assert_tree_read_only(target)
    return {
        "status": "verified",
        "alias": str(alias_path),
        "version": str(target),
        "tree_sha256": digest,
        "read_only": True,
    }


def install_dataset(
    source: Path,
    shared_root: Path,
    alias: str = DEFAULT_ALIAS,
) -> dict[str, Any]:
    _validate_alias(alias)
    if source.expanduser().is_symlink():
        raise SharedDataError("dataset source must not be a symbolic link")
    source_root = source.expanduser().resolve(strict=True)
    validate_manifest(source_root)
    source_digest = tree_digest(source_root)

    root = _resolved_shared_root(shared_root, create=True)
    versions = root / "versions"
    _ensure_private_directory(versions)
    lock_path = root / ".install.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        version = versions / source_digest
        reused = version.exists()
        if reused:
            if version.is_symlink() or not version.is_dir():
                raise SharedDataError("dataset version path is not a regular directory")
            validate_manifest(version)
            if tree_digest(version) != source_digest:
                raise SharedDataError("existing dataset version failed digest verification")
            _assert_tree_read_only(version)
        else:
            staging = Path(
                tempfile.mkdtemp(prefix=f".{source_digest}.", dir=versions)
            )
            try:
                shutil.copytree(source_root, staging, dirs_exist_ok=True)
                validate_manifest(staging)
                if tree_digest(staging) != source_digest:
                    raise SharedDataError("copied dataset digest does not match source")
                _make_tree_read_only(staging)
                os.replace(staging, version)
            finally:
                if staging.exists():
                    _remove_staging_tree(staging)

        alias_path = root / alias
        if os.path.lexists(alias_path) and not alias_path.is_symlink():
            raise SharedDataError("stable dataset path exists and is not a symbolic link")
        temporary_alias = root / f".{alias}.{os.getpid()}.tmp"
        if os.path.lexists(temporary_alias):
            raise SharedDataError("temporary dataset alias already exists")
        try:
            temporary_alias.symlink_to(Path("versions") / source_digest)
            os.replace(temporary_alias, alias_path)
        finally:
            if temporary_alias.is_symlink():
                temporary_alias.unlink()

    result = verify_dataset(root, alias)
    result.update({"status": "installed", "version_reused": reused})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--source", type=Path, required=True)
    install.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED_ROOT)
    install.add_argument("--alias", default=DEFAULT_ALIAS)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED_ROOT)
    verify.add_argument("--alias", default=DEFAULT_ALIAS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "install":
            result = install_dataset(args.source, args.shared_root, args.alias)
        else:
            result = verify_dataset(args.shared_root, args.alias)
    except (OSError, SharedDataError, UnicodeError) as exc:
        print(f"groundwater shared-data operation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
