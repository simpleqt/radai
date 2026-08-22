"""DOCX → PDF 转换（LibreOffice headless）。

跨切面服务：通用导出工具（`export_document`）与 course_eval 报告 PDF 导出共用
同一实现，避免两处各自 spawn LibreOffice 时对 profile 目录、超时和产物校验做出
不同假设。

调用方必须把本函数放在写事务之外：LibreOffice 冷启动是秒级的，包在持行锁的
事务里会长时间阻塞同任务的其他写操作（与 MinerU OCR 同类约束）。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from backend.config.loggers import get_logger

logger = get_logger(__name__)

LIBREOFFICE_TIMEOUT_SECONDS = 90.0


def libreoffice_executable() -> str | None:
    """返回可用的 LibreOffice 可执行文件路径，未安装时返回 None。"""
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_docx_to_pdf(docx_bytes: bytes) -> bytes | None:
    """把 DOCX 字节转成 PDF 字节；不可用或失败时返回 None 供调用方降级。

    每次转换使用独立的 UserInstallation profile 目录：LibreOffice 是单实例
    架构，共享 profile 时并发转换会互相抢锁甚至串扰产物。
    """
    soffice = libreoffice_executable()
    if not soffice:
        logger.warning("[DocumentConversion] 未找到 LibreOffice，无法转换 PDF")
        return None
    if not docx_bytes:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="docx2pdf-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_docx = tmp_path / "doc.docx"
            input_docx.write_bytes(docx_bytes)
            profile_dir = tmp_path / "lo-profile"
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--nologo",
                    "--nofirststartwizard",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmp_path),
                    str(input_docx),
                ],
                capture_output=True,
                text=True,
                timeout=LIBREOFFICE_TIMEOUT_SECONDS,
            )
            output_pdf = tmp_path / "doc.pdf"
            if not output_pdf.is_file():
                logger.warning(
                    "[DocumentConversion] LibreOffice 未产出 PDF: %s",
                    (result.stderr or result.stdout or "").strip()[:500],
                )
                return None
            data = output_pdf.read_bytes()
            if data[:5] != b"%PDF-":
                logger.warning("[DocumentConversion] 产物不是有效 PDF，已丢弃")
                return None
            return data
    except Exception as exc:  # noqa: BLE001 - 转换失败一律降级，不上抛
        logger.warning("[DocumentConversion] PDF 转换失败: %s", exc)
        return None
