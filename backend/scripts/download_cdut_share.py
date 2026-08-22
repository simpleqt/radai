"""Safely download a password-protected Chengli Pan share.

The pickup code is deliberately accepted only through an owner-only file.  It is
never accepted as a command-line value, written to a manifest, or logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx

ALLOWED_SHARE_HOST = "pan.cdut.edu.cn"
UTC = timezone.utc  # noqa: UP017 - production runtime is Python 3.10
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_CANDIDATES = 1_000
DEFAULT_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_MAX_HTML_BYTES = 32 * 1024 * 1024
DEFAULT_ANYSHARE_POLL_ATTEMPTS = 120
EXPECTED_AUDIT_ROOT_FOLDER = "审计知识库原料"
MAX_LOCAL_COMPONENT_BYTES = 240
_SHARE_PATH_RE = re.compile(r"^/link/[A-Fa-f0-9]{32,64}/?$")
_DOWNLOAD_HINT_RE = re.compile(
    r"(?:download|export|archive|打包下载|全部下载|下载)", re.IGNORECASE
)
_JSON_URL_RE = re.compile(
    r"""["'](?:download_?url|downloadUrl|archive_?url|archiveUrl)["']\s*:\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


class DownloadError(RuntimeError):
    """Raised when the remote share cannot be downloaded safely."""


@dataclass(frozen=True)
class HtmlForm:
    action: str
    method: str
    fields: dict[str, str]
    form_id: str = ""


@dataclass(frozen=True)
class DownloadCandidate:
    url: str
    method: str = "GET"
    fields: dict[str, str] | None = None


@dataclass(frozen=True)
class DownloadedArtifact:
    relative_path: str
    size: int
    sha256: str
    content_type: str
    source_url: str
    source_path: str | None = None


@dataclass(frozen=True)
class AnyShareFile:
    relative_path: str
    remote_relative_path: str
    docid: str
    size: int


def _public_source_url(url: str) -> str:
    """Keep provenance without persisting signed query parameters or fragments."""

    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _sanitize_error(exc: BaseException) -> str:
    value = str(exc)
    value = re.sub(r"(https://[^\s?#]+)[?#][^\s]+", r"\1?<redacted>", value)
    value = re.sub(
        r"(?i)(password|token|secret|api[_-]?key)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2***",
        value,
    )
    return value[:2000]


class _ShareHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[HtmlForm] = []
        self.links: list[tuple[str, str, bool]] = []
        self._form: dict[str, object] | None = None
        self._anchor_href = ""
        self._anchor_download = False
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "form":
            self._form = {
                "action": values.get("action", ""),
                "method": values.get("method", "GET").upper(),
                "fields": {},
                "form_id": values.get("id", ""),
            }
        elif tag == "input" and self._form is not None:
            name = values.get("name", "").strip()
            input_type = values.get("type", "text").lower()
            if name and input_type not in {"submit", "button", "file", "image"}:
                fields = self._form["fields"]
                assert isinstance(fields, dict)
                fields[name] = values.get("value", "")
        elif tag == "a":
            self._anchor_href = values.get("href", "").strip()
            self._anchor_download = "download" in values
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form" and self._form is not None:
            self.forms.append(
                HtmlForm(
                    action=str(self._form["action"]),
                    method=str(self._form["method"]),
                    fields=dict(self._form["fields"]),
                    form_id=str(self._form["form_id"]),
                )
            )
            self._form = None
        elif tag == "a" and self._anchor_href:
            self.links.append(
                (
                    self._anchor_href,
                    " ".join(self._anchor_text).strip(),
                    self._anchor_download,
                )
            )
            self._anchor_href = ""
            self._anchor_download = False
            self._anchor_text = []


def validate_share_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_SHARE_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not _SHARE_PATH_RE.fullmatch(parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise DownloadError(
            "分享地址必须是 https://pan.cdut.edu.cn/link/<32至64位十六进制分享ID>"
        )
    return url


def validate_remote_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not (host == "cdut.edu.cn" or host.endswith(".cdut.edu.cn"))
    ):
        raise DownloadError(f"拒绝访问非成都理工大学 HTTPS 地址: {host or '<empty>'}")
    return url


def read_pickup_code(path: Path) -> str:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise DownloadError(f"提取码文件不存在: {path}") from exc
    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
        raise DownloadError("提取码路径必须是普通文件且不能是符号链接")
    if file_stat.st_uid != os.getuid():
        raise DownloadError("提取码文件必须由当前用户拥有")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise DownloadError("提取码文件权限过宽，请设置为 600")
    if file_stat.st_size > 256:
        raise DownloadError("提取码文件异常：内容过长")
    code = path.read_text(encoding="utf-8").strip()
    if not code or any(char in code for char in "\r\n\x00"):
        raise DownloadError("提取码文件必须只包含一行非空文本")
    return code


def parse_forms(html: str, base_url: str) -> list[HtmlForm]:
    parser = _ShareHtmlParser()
    parser.feed(html)
    return [
        HtmlForm(
            action=urljoin(base_url, form.action or base_url),
            method=form.method,
            fields=form.fields,
            form_id=form.form_id,
        )
        for form in parser.forms
    ]


def find_password_form(html: str, base_url: str) -> HtmlForm:
    forms = parse_forms(html, base_url)
    for form in forms:
        if "password" in form.fields:
            validate_remote_url(form.action)
            return form
    raise DownloadError("分享页中未找到提取码验证表单")


def discover_download_candidates(html: str, base_url: str) -> list[DownloadCandidate]:
    parser = _ShareHtmlParser()
    parser.feed(html)
    candidates: list[DownloadCandidate] = []

    for href, text, has_download_attr in parser.links:
        if (
            has_download_attr
            or _DOWNLOAD_HINT_RE.search(href)
            or _DOWNLOAD_HINT_RE.search(text)
        ):
            candidates.append(DownloadCandidate(url=urljoin(base_url, href)))

    for form in parser.forms:
        action = urljoin(base_url, form.action or base_url)
        if _DOWNLOAD_HINT_RE.search(action) or _DOWNLOAD_HINT_RE.search(form.form_id):
            candidates.append(
                DownloadCandidate(
                    url=action,
                    method=form.method,
                    fields=form.fields,
                )
            )

    for raw_url in _JSON_URL_RE.findall(html):
        candidates.append(
            DownloadCandidate(url=urljoin(base_url, raw_url.replace("\\/", "/")))
        )

    unique: list[DownloadCandidate] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for candidate in candidates:
        validated = validate_remote_url(candidate.url)
        fields = tuple(sorted((candidate.fields or {}).items()))
        key = (candidate.method.upper(), validated, fields)
        if key not in seen:
            seen.add(key)
            unique.append(
                DownloadCandidate(
                    validated, candidate.method.upper(), dict(fields) or None
                )
            )
    return unique


def _validate_response_chain(response: httpx.Response) -> None:
    for item in [*response.history, response]:
        validate_remote_url(str(item.url))


def authenticate_share(
    client: httpx.Client, share_url: str, pickup_code: str
) -> httpx.Response:
    initial = client.get(validate_share_url(share_url))
    _validate_response_chain(initial)
    initial.raise_for_status()
    form = find_password_form(initial.text, str(initial.url))
    payload = dict(form.fields)
    payload["password"] = pickup_code
    # The page embeds a large cosmetic logo field.  Browsers submit it, but the
    # server-side authentication contract does not require it.
    payload.pop("logo", None)
    if form.method == "POST":
        response = client.post(form.action, data=payload)
    else:
        response = client.get(form.action, params=payload)
    _validate_response_chain(response)
    response.raise_for_status()
    if any(
        "password" in candidate.fields
        for candidate in parse_forms(response.text, str(response.url))
    ):
        raise DownloadError("提取码验证失败或分享已失效")
    return response


def safe_file_name(value: str, fallback: str = "download.bin") -> str:
    value = unquote(value).replace("\\", "/")
    name = PurePosixPath(value).name.strip().replace("\x00", "")
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    if name in {"", ".", ".."}:
        name = fallback
    return name[:240]


def response_file_name(response: httpx.Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return safe_file_name(filename)
    return safe_file_name(urlparse(str(response.url)).path, "share-download.bin")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stream_response_to_file(
    response: httpx.Response,
    destination_dir: Path,
    *,
    max_bytes: int,
) -> DownloadedArtifact:
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_name = response_file_name(response)
    destination = destination_dir / file_name
    content_length = int(response.headers.get("content-length") or 0)
    if content_length > max_bytes:
        raise DownloadError(f"下载文件超过限制: {content_length} > {max_bytes}")

    fd, temporary_name = tempfile.mkstemp(prefix=f".{file_name}.", dir=destination_dir)
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as file_obj:
            for chunk in response.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise DownloadError(f"下载内容超过限制: {max_bytes} bytes")
                digest.update(chunk)
                file_obj.write(chunk)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        temporary = Path(temporary_name)
        if destination.exists():
            existing_hash = _sha256_file(destination)
            if existing_hash != digest.hexdigest():
                raise DownloadError(f"目标文件已存在且内容不同: {destination}")
            temporary.unlink()
        else:
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise

    return DownloadedArtifact(
        relative_path=file_name,
        size=size,
        sha256=digest.hexdigest(),
        content_type=response.headers.get(
            "content-type", "application/octet-stream"
        ).split(";", 1)[0],
        source_url=_public_source_url(str(response.url)),
    )


def _safe_entry_name(value: object) -> str:
    if not isinstance(value, str):
        raise DownloadError("云盘目录包含非文本文件名")
    if (
        not value
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or len(value) > 240
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DownloadError("云盘目录包含不安全的文件名")
    return value


def _local_entry_name(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_LOCAL_COMPONENT_BYTES:
        return value
    suffix = PurePosixPath(value).suffix
    if len(suffix.encode("utf-8")) > 32:
        suffix = ""
    stem = value[: -len(suffix)] if suffix else value
    marker = f"__{hashlib.sha256(encoded).hexdigest()[:12]}"
    byte_budget = MAX_LOCAL_COMPONENT_BYTES - len((marker + suffix).encode("utf-8"))
    prefix = stem
    while prefix and len(prefix.encode("utf-8")) > byte_budget:
        prefix = prefix[:-1]
    if not prefix:
        prefix = "item"
    shortened = f"{prefix}{marker}{suffix}"
    if len(shortened.encode("utf-8")) > MAX_LOCAL_COMPONENT_BYTES:
        raise DownloadError("无法为超长云盘文件名生成安全本地名称")
    return shortened


def _anyshare_object_id(docid: object) -> str:
    if not isinstance(docid, str) or not docid.startswith("gns://"):
        raise DownloadError("云盘返回了无效的文档标识")
    object_id = docid.replace("gns://", "", 1).rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", object_id):
        raise DownloadError("云盘返回了无效的文档标识")
    return object_id


def _anyshare_token(client: httpx.Client, share_url: str) -> str:
    share_id = (
        urlparse(validate_share_url(share_url)).path.rstrip("/").rsplit("/", 1)[-1]
    )
    cookie_name = f"link_token:{share_id}"
    values = {
        cookie.value
        for cookie in client.cookies.jar
        if cookie.name == cookie_name and cookie.value
    }
    if len(values) != 1:
        raise DownloadError("提取码验证成功，但未获得唯一的云盘授权会话")
    return values.pop()


def _request_anyshare_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, object] | None = None,
) -> object:
    validated_url = validate_remote_url(url)
    response = client.request(
        method,
        validated_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        json=payload,
    )
    _validate_response_chain(response)
    response.raise_for_status()
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise DownloadError("云盘 API 返回了非 JSON 响应") from exc


def _discover_anyshare_files(
    client: httpx.Client,
    authenticated_url: str,
    *,
    token: str,
    expected_root_folder: str,
    max_files: int,
    max_bytes: int,
) -> list[AnyShareFile]:
    entry_url = urljoin(authenticated_url, "/api/efast/v1/entry-item")
    root_items = _request_anyshare_json(client, "GET", entry_url, token=token)
    if not isinstance(root_items, list) or len(root_items) != 1:
        raise DownloadError("云盘分享必须只包含一个指定根目录")
    root = root_items[0]
    if not isinstance(root, dict) or root.get("type") != "folder":
        raise DownloadError("云盘分享根项不是文件夹")
    raw_root_name = root.get("name")
    if not isinstance(raw_root_name, str):
        raise DownloadError("云盘分享根目录名无效")
    root_leaf = raw_root_name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if root_leaf != expected_root_folder:
        raise DownloadError("云盘分享根目录与授权导入范围不一致")
    root_docid = root.get("id")
    _anyshare_object_id(root_docid)

    list_url = urljoin(authenticated_url, "/api/efast/v1/dir/list")
    queue: deque[tuple[str, PurePosixPath, PurePosixPath]] = deque(
        [(str(root_docid), PurePosixPath("."), PurePosixPath("."))]
    )
    visited_docids: set[str] = set()
    seen_paths: set[str] = set()
    seen_remote_paths: set[str] = set()
    files: list[AnyShareFile] = []
    total_bytes = 0
    while queue:
        docid, local_parent, remote_parent = queue.popleft()
        if docid in visited_docids:
            raise DownloadError("云盘目录存在循环或重复引用")
        visited_docids.add(docid)
        if len(visited_docids) > max_files:
            raise DownloadError(f"云盘目录数量超过限制: {max_files}")
        listing = _request_anyshare_json(
            client,
            "POST",
            list_url,
            token=token,
            payload={"docid": docid, "sort": "asc", "by": "name"},
        )
        if (
            not isinstance(listing, dict)
            or not isinstance(listing.get("dirs"), list)
            or not isinstance(listing.get("files"), list)
        ):
            raise DownloadError("云盘目录列表格式不符合预期")

        for item in listing["dirs"]:
            if not isinstance(item, dict):
                raise DownloadError("云盘返回了无效的目录项")
            name = _safe_entry_name(item.get("name"))
            local_name = _local_entry_name(name)
            child_docid = item.get("docid")
            _anyshare_object_id(child_docid)
            relative_path = (local_parent / local_name).as_posix()
            remote_relative_path = (remote_parent / name).as_posix()
            if relative_path in seen_paths:
                raise DownloadError("云盘目录包含重复路径")
            if remote_relative_path in seen_remote_paths:
                raise DownloadError("云盘目录包含重复原始路径")
            seen_paths.add(relative_path)
            seen_remote_paths.add(remote_relative_path)
            queue.append(
                (
                    str(child_docid),
                    local_parent / local_name,
                    remote_parent / name,
                )
            )

        for item in listing["files"]:
            if not isinstance(item, dict):
                raise DownloadError("云盘返回了无效的文件项")
            name = _safe_entry_name(item.get("name"))
            local_name = _local_entry_name(name)
            child_docid = item.get("docid")
            _anyshare_object_id(child_docid)
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise DownloadError("云盘返回了无效的文件大小")
            relative_path = (local_parent / local_name).as_posix()
            remote_relative_path = (remote_parent / name).as_posix()
            if relative_path in seen_paths:
                raise DownloadError("云盘目录包含重复路径")
            if remote_relative_path in seen_remote_paths:
                raise DownloadError("云盘目录包含重复原始路径")
            seen_paths.add(relative_path)
            seen_remote_paths.add(remote_relative_path)
            total_bytes += size
            if len(files) + 1 > max_files:
                raise DownloadError(f"云盘文件数量超过限制: {max_files}")
            if total_bytes > max_bytes:
                raise DownloadError(f"云盘文件总大小超过限制: {max_bytes} bytes")
            files.append(
                AnyShareFile(
                    relative_path=relative_path,
                    remote_relative_path=remote_relative_path,
                    docid=str(child_docid),
                    size=size,
                )
            )

    if not files:
        raise DownloadError("云盘指定目录中没有可下载文件")
    return sorted(files, key=lambda item: item.relative_path)


def stream_response_to_path(
    response: httpx.Response,
    destination_root: Path,
    relative_path: str,
    *,
    max_bytes: int,
    expected_size: int | None = None,
    source_path: str | None = None,
) -> DownloadedArtifact:
    relative = PurePosixPath(relative_path)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise DownloadError("拒绝写入不安全的相对路径")
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination_root.is_symlink() or not destination_root.is_dir():
        raise DownloadError("下载根目录必须是非符号链接目录")
    destination = destination_root.joinpath(*relative.parts)
    current = destination_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise DownloadError(f"下载路径存在不安全的父目录: {current}")
        current.mkdir(exist_ok=True, mode=0o700)

    content_length = int(response.headers.get("content-length") or 0)
    if content_length > max_bytes:
        raise DownloadError(f"下载文件超过限制: {content_length} > {max_bytes}")
    fd, temporary_name = tempfile.mkstemp(prefix=".download-", dir=current)
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as file_obj:
            for chunk in response.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise DownloadError(f"下载内容超过限制: {max_bytes} bytes")
                digest.update(chunk)
                file_obj.write(chunk)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if expected_size is not None and size != expected_size:
            raise DownloadError("云盘文件大小与目录清单不一致")
        temporary = Path(temporary_name)
        if destination.exists():
            if (
                destination.is_symlink()
                or not destination.is_file()
                or _sha256_file(destination) != digest.hexdigest()
            ):
                raise DownloadError(f"目标文件已存在且内容不同: {destination}")
            temporary.unlink()
        else:
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise

    return DownloadedArtifact(
        relative_path=relative.as_posix(),
        size=size,
        sha256=digest.hexdigest(),
        content_type=response.headers.get(
            "content-type", "application/octet-stream"
        ).split(";", 1)[0],
        source_url=_public_source_url(str(response.url)),
        source_path=source_path,
    )


def _download_anyshare_file(
    client: httpx.Client,
    authenticated_url: str,
    source_dir: Path,
    file_item: AnyShareFile,
    *,
    token: str,
    max_bytes: int,
) -> DownloadedArtifact:
    prepare_url = urljoin(authenticated_url, "/api/open-doc/v1/file-download")
    payload: dict[str, object] = {
        "name": safe_file_name(PurePosixPath(file_item.relative_path).name),
        "doc": [{"id": _anyshare_object_id(file_item.docid)}],
    }
    direct_url = ""
    for attempt in range(DEFAULT_ANYSHARE_POLL_ATTEMPTS):
        status = _request_anyshare_json(
            client,
            "POST",
            prepare_url,
            token=token,
            payload=payload,
        )
        if not isinstance(status, dict):
            raise DownloadError("云盘下载状态格式不符合预期")
        progress = status.get("progress")
        if progress == 100:
            items = status.get("items")
            if not isinstance(items, list) or len(items) != 1:
                raise DownloadError("云盘单文件下载返回了非唯一结果")
            result = items[0]
            if (
                not isinstance(result, dict)
                or result.get("status") != "completed"
                or not isinstance(result.get("url"), str)
                or not result["url"]
            ):
                raise DownloadError("云盘拒绝或无法准备指定文件")
            direct_url = validate_remote_url(urljoin(prepare_url, result["url"]))
            break
        if not isinstance(progress, int) or isinstance(progress, bool):
            raise DownloadError("云盘下载进度格式不符合预期")
        if attempt + 1 < DEFAULT_ANYSHARE_POLL_ATTEMPTS:
            time.sleep(1)
    if not direct_url:
        raise DownloadError("云盘准备文件超时")

    # The prepared URL is already narrowly scoped and signed.  AnyShare's object
    # storage endpoint rejects the API bearer header, matching the browser's
    # ordinary navigation/download behavior.
    request = client.build_request("GET", direct_url)
    response = client.send(request, stream=True, follow_redirects=True)
    try:
        _validate_response_chain(response)
        response.raise_for_status()
        if _is_html(response):
            raise DownloadError("云盘文件下载返回了 HTML 页面")
        artifact = stream_response_to_path(
            response,
            source_dir,
            file_item.relative_path,
            max_bytes=max_bytes,
            expected_size=file_item.size,
            source_path=file_item.remote_relative_path,
        )
    finally:
        response.close()
    return artifact


def download_anyshare_folder(
    client: httpx.Client,
    authenticated_url: str,
    share_url: str,
    source_dir: Path,
    *,
    expected_root_folder: str,
    max_files: int,
    max_download_bytes: int,
    max_extracted_bytes: int,
) -> list[DownloadedArtifact]:
    token = _anyshare_token(client, share_url)
    max_bytes = min(max_download_bytes, max_extracted_bytes)
    files = _discover_anyshare_files(
        client,
        authenticated_url,
        token=token,
        expected_root_folder=expected_root_folder,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    artifacts: list[DownloadedArtifact] = []
    downloaded_bytes = 0
    for file_item in files:
        remaining = max_download_bytes - downloaded_bytes
        if remaining <= 0:
            raise DownloadError("累计下载内容超过安全限制")
        artifact = _download_anyshare_file(
            client,
            authenticated_url,
            source_dir,
            file_item,
            token=token,
            max_bytes=remaining,
        )
        downloaded_bytes += artifact.size
        artifacts.append(artifact)
    return artifacts


def extract_zip_safely(
    archive: Path,
    output_dir: Path,
    *,
    max_files: int,
    max_extracted_bytes: int,
) -> list[DownloadedArtifact]:
    output_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts: list[DownloadedArtifact] = []
    with zipfile.ZipFile(archive) as zip_file:
        members = zip_file.infolist()
        if len(members) > max_files:
            raise DownloadError(f"压缩包文件数量超过限制: {len(members)} > {max_files}")

        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        seen_paths: set[str] = set()
        total_size = 0
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if (
                not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or "\x00" in member.filename
            ):
                raise DownloadError(f"压缩包包含不安全路径: {member.filename}")
            unix_mode = member.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise DownloadError(f"压缩包包含符号链接: {member.filename}")
            if member.is_dir():
                continue
            file_type = stat.S_IFMT(unix_mode)
            if file_type and file_type != stat.S_IFREG:
                raise DownloadError(f"压缩包包含非普通文件: {member.filename}")
            total_size += member.file_size
            if total_size > max_extracted_bytes:
                raise DownloadError(
                    f"解压后总大小超过限制: {total_size} > {max_extracted_bytes}"
                )
            relative = Path(*path.parts)
            normalized = relative.as_posix()
            if normalized in seen_paths:
                raise DownloadError(f"压缩包包含重复路径: {member.filename}")
            seen_paths.add(normalized)
            planned.append((member, relative))

        with tempfile.TemporaryDirectory(
            prefix=".audit-extract-", dir=output_dir.parent
        ) as temporary_name:
            temporary_root = Path(temporary_name)
            for member, relative in planned:
                destination = temporary_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                size = 0
                with zip_file.open(member) as source, destination.open("xb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > member.file_size:
                            raise DownloadError(
                                f"压缩包条目解压大小异常: {member.filename}"
                            )
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size != member.file_size:
                    raise DownloadError(f"压缩包条目大小不一致: {member.filename}")
                os.chmod(destination, 0o600)
                artifacts.append(
                    DownloadedArtifact(
                        relative_path=relative.as_posix(),
                        size=size,
                        sha256=digest.hexdigest(),
                        content_type="application/octet-stream",
                        source_url=f"archive:{archive.name}",
                    )
                )

            # Validate every collision before publishing any member from this archive.
            for artifact in artifacts:
                existing = output_dir / artifact.relative_path
                if existing.exists() and (
                    not existing.is_file()
                    or existing.is_symlink()
                    or _sha256_file(existing) != artifact.sha256
                ):
                    raise DownloadError(f"解压目标已存在且内容不同: {existing}")

            output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            for artifact in artifacts:
                source = temporary_root / artifact.relative_path
                destination = output_dir / artifact.relative_path
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(source, destination)
    return artifacts


def _is_html(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").lower().startswith("text/html")


def _read_html_limited(
    response: httpx.Response, *, max_bytes: int = DEFAULT_MAX_HTML_BYTES
) -> str:
    content_length = int(response.headers.get("content-length") or 0)
    if content_length > max_bytes:
        raise DownloadError(f"HTML 响应超过限制: {content_length} > {max_bytes}")
    content = bytearray()
    for chunk in response.iter_bytes(1024 * 1024):
        content.extend(chunk)
        if len(content) > max_bytes:
            raise DownloadError(f"HTML 响应超过限制: {max_bytes} bytes")
    encoding = response.encoding or "utf-8"
    return bytes(content).decode(encoding, errors="replace")


def request_candidate(
    client: httpx.Client, candidate: DownloadCandidate
) -> httpx.Response:
    method = candidate.method.upper()
    request_kwargs: dict[str, object] = {}
    if method == "POST":
        request_kwargs["data"] = candidate.fields or {}
    else:
        request_kwargs["params"] = candidate.fields or None
    request = client.build_request(method, candidate.url, **request_kwargs)
    # Candidate responses can be multi-gigabyte archives.  Keep them streamed so
    # the configured byte limit is enforced before the body is buffered.
    response = client.send(request, stream=True, follow_redirects=True)
    _validate_response_chain(response)
    response.raise_for_status()
    return response


def download_share(
    share_url: str,
    pickup_code_file: Path,
    output_root: Path,
    *,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> dict[str, object]:
    if max_download_bytes <= 0 or max_extracted_bytes <= 0 or max_files <= 0:
        raise DownloadError("下载大小、解压大小和文件数量限制必须为正整数")
    if output_root.is_symlink():
        raise DownloadError(f"输出目录不能是符号链接: {output_root}")
    pickup_code = read_pickup_code(pickup_code_file)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_dir = output_root / "archives"
    source_dir = output_root / "source"
    capture_path = output_root / "authenticated-page.html"

    headers = {
        "User-Agent": "RADAI-controlled-import/1.0",
        "Accept": "text/html,application/xhtml+xml,application/zip,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    downloaded: list[DownloadedArtifact] = []
    direct_extracted: list[DownloadedArtifact] | None = None
    with httpx.Client(headers=headers, follow_redirects=True, timeout=120.0) as client:
        authenticated = authenticate_share(client, share_url, pickup_code)
        candidates = discover_download_candidates(
            authenticated.text, str(authenticated.url)
        )
        if not candidates:
            try:
                direct_extracted = download_anyshare_folder(
                    client,
                    str(authenticated.url),
                    share_url,
                    source_dir,
                    expected_root_folder=EXPECTED_AUDIT_ROOT_FOLDER,
                    max_files=max_files,
                    max_download_bytes=max_download_bytes,
                    max_extracted_bytes=max_extracted_bytes,
                )
            except BaseException:
                capture_path.write_text(authenticated.text, encoding="utf-8")
                os.chmod(capture_path, 0o600)
                raise
        else:
            queued = list(candidates)
            visited: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
            downloaded_bytes = 0
            while queued:
                if len(visited) >= DEFAULT_MAX_CANDIDATES:
                    raise DownloadError(
                        f"下载入口数量超过安全限制: {DEFAULT_MAX_CANDIDATES}"
                    )
                candidate = queued.pop(0)
                visit_key = (
                    candidate.method,
                    candidate.url,
                    tuple(sorted((candidate.fields or {}).items())),
                )
                if visit_key in visited:
                    continue
                visited.add(visit_key)
                response = request_candidate(client, candidate)
                try:
                    if _is_html(response):
                        nested = discover_download_candidates(
                            _read_html_limited(response), str(response.url)
                        )
                        queued.extend(nested)
                        continue
                    remaining_bytes = max_download_bytes - downloaded_bytes
                    if remaining_bytes <= 0:
                        raise DownloadError(
                            f"累计下载内容超过限制: {max_download_bytes} bytes"
                        )
                    artifact = stream_response_to_file(
                        response,
                        archive_dir,
                        max_bytes=remaining_bytes,
                    )
                    downloaded_bytes += artifact.size
                    downloaded.append(artifact)
                finally:
                    response.close()

    extracted: list[DownloadedArtifact] = direct_extracted or []
    extracted_bytes = sum(item.size for item in extracted)
    extracted_files = len(extracted)
    if direct_extracted is None:
        for artifact in downloaded:
            archive = archive_dir / artifact.relative_path
            if zipfile.is_zipfile(archive):
                remaining_files = max_files - extracted_files
                remaining_bytes = max_extracted_bytes - extracted_bytes
                if remaining_files <= 0 or remaining_bytes <= 0:
                    raise DownloadError("累计解压内容超过安全限制")
                archive_files = extract_zip_safely(
                    archive,
                    source_dir,
                    max_files=remaining_files,
                    max_extracted_bytes=remaining_bytes,
                )
                extracted.extend(archive_files)
                extracted_files += len(archive_files)
                extracted_bytes += sum(item.size for item in archive_files)
            else:
                if extracted_files + 1 > max_files:
                    raise DownloadError(f"源文件数量超过限制: {max_files}")
                if extracted_bytes + artifact.size > max_extracted_bytes:
                    raise DownloadError(
                        f"源文件累计大小超过限制: {max_extracted_bytes} bytes"
                    )
                source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination = source_dir / artifact.relative_path
                if destination.exists():
                    if _sha256_file(destination) != artifact.sha256:
                        raise DownloadError(f"源文件已存在且内容不同: {destination}")
                else:
                    os.link(archive, destination)
                extracted.append(
                    DownloadedArtifact(
                        relative_path=artifact.relative_path,
                        size=artifact.size,
                        sha256=artifact.sha256,
                        content_type=artifact.content_type,
                        source_url=f"download:{artifact.relative_path}",
                    )
                )
                extracted_files += 1
                extracted_bytes += artifact.size

    manifest: dict[str, object] = {
        "schema_version": 1,
        "share_url": validate_share_url(share_url),
        "downloaded_at": datetime.now(UTC).isoformat(),
        "archive_count": len(downloaded),
        "source_file_count": len(extracted),
        "archives": [asdict(item) for item in downloaded],
        "files": [asdict(item) for item in extracted],
    }
    manifest_path = output_root / "download-manifest.json"
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.", dir=manifest_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            file_obj.write(payload)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, manifest_path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    capture_path.unlink(missing_ok=True)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全下载成理云盘受保护分享")
    parser.add_argument("--url", required=True, help="成理云盘分享地址")
    parser.add_argument(
        "--pickup-code-file",
        type=Path,
        required=True,
        help="权限必须为 600 的提取码文件（提取码不能作为命令行参数）",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument(
        "--max-download-bytes", type=int, default=DEFAULT_MAX_DOWNLOAD_BYTES
    )
    parser.add_argument(
        "--max-extracted-bytes", type=int, default=DEFAULT_MAX_EXTRACTED_BYTES
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = download_share(
            args.url,
            args.pickup_code_file.absolute(),
            args.output_root.absolute(),
            max_download_bytes=args.max_download_bytes,
            max_extracted_bytes=args.max_extracted_bytes,
            max_files=args.max_files,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": _sanitize_error(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": "completed",
                "source_file_count": manifest["source_file_count"],
                "manifest": str(args.output_root.resolve() / "download-manifest.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
