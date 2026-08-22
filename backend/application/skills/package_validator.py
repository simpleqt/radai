"""Validation and safe extraction for uploaded Skill ZIP packages."""

from __future__ import annotations

import hashlib
import io
import math
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.core.utils.normalization import normalize_string_list

MAX_PACKAGE_SIZE = 20 * 1024 * 1024
MAX_EXTRACTED_SIZE = 100 * 1024 * 1024
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_SKILL_MD_SIZE = 1024 * 1024
MAX_FILES = 200
MAX_PATH_DEPTH = 8
ALLOWED_KB_BINDING_MODES = {
    "agent_or_user_selected",
    "agent_selected",
    "user_selected",
    "fixed",
}
# Skill keys are persisted as identifiers and directory names, so keep the
# character set deliberately narrow while allowing Chinese package names.
# The two ranges cover CJK Unified Ideographs and Extension A.
SKILL_KEY_RE = re.compile(
    r"^[a-z\u3400-\u4dbf\u4e00-\u9fff]"
    r"[a-z0-9\u3400-\u4dbf\u4e00-\u9fff-]{2,63}$"
)
SKILL_KEY_FORMAT_HINT = (
    "3-64 个字符，只能包含小写字母、数字、连字符或中文，且不能以数字或连字符开头"
)
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ALLOWED_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".html",
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
}
BLOCKED_SUFFIXES = {
    ".py",
    ".pyc",
    ".pyo",
    ".sh",
    ".bash",
    ".so",
    ".dll",
    ".exe",
    ".jar",
    ".class",
    ".bin",
}


class SkillPackageValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_SKILL_PACKAGE"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedSkillPackage:
    skill_key: str
    version: str
    description: str
    domain: str
    display_name: str
    manifest: dict[str, Any]
    file_index: list[dict[str, Any]]
    package_sha256: str
    package_size: int
    root_prefix: str
    warnings: list[str]


class SkillPackageValidator:
    def __init__(self, *, manifest_loader: SkillManifestLoader | None = None):
        self._manifest_loader = manifest_loader or SkillManifestLoader()

    def validate(self, content: bytes) -> ValidatedSkillPackage:
        if not content:
            raise SkillPackageValidationError("Skill ZIP 不能为空")
        if len(content) > MAX_PACKAGE_SIZE:
            raise SkillPackageValidationError("Skill ZIP 超过 20MB 限制")

        package_sha256 = hashlib.sha256(content).hexdigest()
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise SkillPackageValidationError("上传文件不是有效 ZIP") from exc

        with archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if not infos:
                raise SkillPackageValidationError("Skill ZIP 中没有文件")
            if len(infos) > MAX_FILES:
                raise SkillPackageValidationError(f"文件数量超过限制: {len(infos)} > {MAX_FILES}")

            normalized_paths: dict[str, zipfile.ZipInfo] = {}
            total_size = 0
            for info in infos:
                path = self._validate_member(info)
                canonical = unicodedata.normalize("NFC", path.as_posix()).casefold()
                if canonical in normalized_paths:
                    raise SkillPackageValidationError(f"ZIP 包含重复路径: {path}")
                normalized_paths[canonical] = info
                total_size += info.file_size
                if total_size > MAX_EXTRACTED_SIZE:
                    raise SkillPackageValidationError("ZIP 解压后总大小超过 100MB")

            skill_entries = [
                (canonical, info)
                for canonical, info in normalized_paths.items()
                if PurePosixPath(info.filename.replace("\\", "/")).name == "SKILL.md"
            ]
            if len(skill_entries) != 1:
                raise SkillPackageValidationError("ZIP 必须且只能包含一个 SKILL.md")

            skill_info = skill_entries[0][1]
            if skill_info.file_size > MAX_SKILL_MD_SIZE:
                raise SkillPackageValidationError("SKILL.md 超过 1MB 限制")
            skill_path = PurePosixPath(skill_info.filename.replace("\\", "/"))
            if len(skill_path.parts) > 2:
                raise SkillPackageValidationError("SKILL.md 只能位于 ZIP 根目录或单层根目录中")
            root_prefix = "" if len(skill_path.parts) == 1 else f"{skill_path.parts[0]}/"
            for info in infos:
                normalized = info.filename.replace("\\", "/")
                if root_prefix and not normalized.startswith(root_prefix):
                    raise SkillPackageValidationError("ZIP 只能包含一个 Skill 根目录")

            raw_skill = archive.read(skill_info)
            try:
                skill_text = raw_skill.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillPackageValidationError("SKILL.md 必须使用 UTF-8 编码") from exc
            raw_manifest = self._parse_frontmatter(skill_text)
            normalized_manifest = self._normalize_manifest(raw_manifest)

            file_index = []
            for info in infos:
                logical_path = info.filename.replace("\\", "/")
                if root_prefix:
                    logical_path = logical_path[len(root_prefix) :]
                data = archive.read(info)
                file_index.append(
                    {
                        "path": logical_path,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )

        return ValidatedSkillPackage(
            skill_key=normalized_manifest["name"],
            version=normalized_manifest["version"],
            description=normalized_manifest["description"],
            domain=normalized_manifest["domain"],
            display_name=str(raw_manifest.get("display_name") or normalized_manifest["name"]),
            manifest=normalized_manifest,
            file_index=file_index,
            package_sha256=package_sha256,
            package_size=len(content),
            root_prefix=root_prefix,
            warnings=[],
        )

    def extract(self, content: bytes, destination: Path, *, root_prefix: str) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                self._validate_member(info)
                logical = info.filename.replace("\\", "/")
                if root_prefix:
                    if not logical.startswith(root_prefix):
                        raise SkillPackageValidationError("ZIP 根目录在解压时发生变化")
                    logical = logical[len(root_prefix) :]
                target = (destination / logical).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise SkillPackageValidationError("检测到 ZIP 路径穿越") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))

    def _validate_member(self, info: zipfile.ZipInfo) -> PurePosixPath:
        raw = info.filename.replace("\\", "/")
        if not raw or "\x00" in raw or re.match(r"^[A-Za-z]:", raw):
            raise SkillPackageValidationError(f"非法 ZIP 路径: {raw!r}")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SkillPackageValidationError(f"非法 ZIP 路径: {raw}")
        if len(path.parts) > MAX_PATH_DEPTH:
            raise SkillPackageValidationError(f"路径层级超过限制: {raw}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type and file_type != stat.S_IFREG:
            raise SkillPackageValidationError(f"不允许链接或特殊文件: {raw}")
        if info.flag_bits & 0x1:
            raise SkillPackageValidationError("不支持加密 ZIP")
        if info.file_size > MAX_FILE_SIZE:
            raise SkillPackageValidationError(f"单文件超过 10MB: {raw}")
        suffix = path.suffix.lower()
        if suffix in BLOCKED_SUFFIXES or (suffix and suffix not in ALLOWED_SUFFIXES):
            raise SkillPackageValidationError(f"不允许的文件类型: {raw}")
        if info.compress_size > 0 and info.file_size / info.compress_size > 200:
            raise SkillPackageValidationError(f"文件压缩比异常，疑似 ZIP bomb: {raw}")
        return path

    def _parse_frontmatter(self, content: str) -> dict[str, Any]:
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
        if not match:
            raise SkillPackageValidationError("SKILL.md 缺少 YAML frontmatter")
        try:
            raw = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise SkillPackageValidationError("SKILL.md frontmatter YAML 非法") from exc
        if not isinstance(raw, dict):
            raise SkillPackageValidationError("SKILL.md frontmatter 必须是对象")
        return raw

    def _normalize_manifest(self, raw: dict[str, Any]) -> dict[str, Any]:
        name = str(raw.get("name") or "").strip()
        version = str(raw.get("version") or "").strip()
        description = str(raw.get("description") or "").strip()
        domain = str(raw.get("domain") or "").strip()
        if not SKILL_KEY_RE.fullmatch(name):
            raise SkillPackageValidationError(
                f"Skill name 格式错误：{SKILL_KEY_FORMAT_HINT}"
            )
        if not SEMVER_RE.fullmatch(version):
            raise SkillPackageValidationError("Skill version 必须是合法 SemVer")
        if not description or len(description) > 1000:
            raise SkillPackageValidationError("description 必填且不能超过 1000 字符")
        if not domain or len(domain) > 64:
            raise SkillPackageValidationError("domain 必填且不能超过 64 字符")

        tools = self._manifest_loader._parse_tools(raw.get("tools"))
        if self._manifest_loader._validate_tools:
            for tool_name in tools:
                try:
                    self._manifest_loader._tool_registry.get(tool_name)
                except Exception as exc:
                    raise SkillPackageValidationError(f"Skill 声明了未知工具: {tool_name}") from exc
        workflows = raw.get("workflows") or {}
        if isinstance(workflows, dict):
            allowed = normalize_string_list(workflows.get("allowed"))
            preferred = str(workflows.get("preferred") or "").strip() or None
        else:
            allowed = normalize_string_list(workflows)
            preferred = None
        from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS

        unknown = sorted(set(allowed) - set(SUPPORTED_WORKFLOWS))
        if unknown:
            raise SkillPackageValidationError(f"Skill 声明了未知 workflow: {', '.join(unknown)}")
        if preferred and preferred not in set(allowed or SUPPORTED_WORKFLOWS):
            raise SkillPackageValidationError("preferred workflow 必须包含在 allowed 中")
        kb_policy = raw.get("kb_policy") or {}
        if not isinstance(kb_policy, dict):
            raise SkillPackageValidationError("kb_policy 必须是对象")
        binding_mode = str(
            kb_policy.get("binding_mode") or "agent_or_user_selected"
        ).strip()
        if binding_mode not in ALLOWED_KB_BINDING_MODES:
            raise SkillPackageValidationError(
                f"kb_policy.binding_mode 不受支持: {binding_mode}"
            )
        guardrails = normalize_string_list(raw.get("guardrails"))
        invalid_guardrails = [
            item for item in guardrails if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", item)
        ]
        if invalid_guardrails:
            raise SkillPackageValidationError(
                f"guardrails 名称非法: {', '.join(invalid_guardrails)}"
            )
        dependencies = normalize_string_list(raw.get("dependencies"))
        conflicts = normalize_string_list(raw.get("conflicts"))
        capabilities = normalize_string_list(raw.get("capabilities"))
        invalid_refs = [
            item
            for item in [*dependencies, *conflicts]
            if not SKILL_KEY_RE.fullmatch(item)
        ]
        if invalid_refs:
            raise SkillPackageValidationError(
                f"dependencies/conflicts 包含非法 Skill 名称: {', '.join(invalid_refs)}"
            )
        if name in dependencies or name in conflicts:
            raise SkillPackageValidationError("Skill 不能依赖或冲突于自身")
        invalid_capabilities = [
            item for item in capabilities if not re.fullmatch(r"[a-z][a-z0-9_.:-]{1,127}", item)
        ]
        if invalid_capabilities:
            raise SkillPackageValidationError(
                f"capabilities 名称非法: {', '.join(invalid_capabilities)}"
            )
        raw_budget = raw.get("resource_budget") or {}
        if not isinstance(raw_budget, dict):
            raise SkillPackageValidationError("resource_budget 必须是对象")
        resource_budget: dict[str, int | float] = {}
        for key, value in raw_budget.items():
            normalized_key = str(key or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", normalized_key):
                raise SkillPackageValidationError(
                    f"resource_budget 键非法: {normalized_key}"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                raise SkillPackageValidationError(
                    f"resource_budget.{normalized_key} 必须是非负有限数字"
                )
            resource_budget[normalized_key] = value
        return {
            "name": name,
            "version": version,
            "description": description,
            "domain": domain,
            "license": str(raw.get("license") or "").strip() or None,
            "tools": tools,
            "allowed_workflows": allowed or list(SUPPORTED_WORKFLOWS),
            "preferred_workflow": preferred,
            "kb_required": bool(kb_policy.get("required", False)),
            "kb_binding_mode": binding_mode,
            "guardrails": guardrails,
            "dependencies": dependencies,
            "conflicts": conflicts,
            "capabilities": capabilities,
            "resource_budget": resource_budget,
        }
