"""题库管理服务：上传批次（解析→确认两段式）、题目与知识点字典。

批次纪律：
- 解析阶段把图片落到 MinIO（``question-bank/{batch}/{placeholder}/{seq}``，
  placeholder 即题目的持久 id），parsed_json 只存占位符——确认前题目不进题目表；
- 确认阶段逐题写入 questions（error 题跳过），同一批次不可重复确认；
- 未确认批次可整体废弃（题目行从未产生，图片对象成为孤儿，留待后续清理任务）。

题库是课程共享教学资产：所有 teacher/admin 可管理（created_by + 审计追溯），
不做属主隔离（与教师分析报告的 PII 属主隔离决策不同）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import unicodedata
import uuid
import zipfile
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.question_bank import images as qb_images
from backend.application.question_bank.parser import (
    ParseResult,
    parse_question_docx,
    parsed_question_to_dict,
)
from backend.config.loggers import get_logger
from backend.db.models import (
    QuestionBankBatch,
    QuestionBankKnowledgePoint,
    QuestionBankQuestion,
)

logger = get_logger(__name__)

MAX_DOCX_BYTES = 20 * 1024 * 1024
# docx 解压炸弹防护：20MB 上限只约束压缩字节，中央目录里的解压尺寸在解析前
# 先行校验（单条目与总量），高压缩比恶意文档在 python-docx/pandoc 展开前拒绝。
_MAX_DOCX_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_DOCX_TOTAL_BYTES = 256 * 1024 * 1024

QUESTION_TYPE_VALUES = ("选择题", "填空题", "判断题", "计算题", "作图题", "简答题")
DIFFICULTY_VALUES = ("easy", "medium", "hard")
STATUS_VALUES = ("published", "disabled")

# 手工录入/编辑的字段上界（Text 列 64KB；上界防超大请求体与畸形数据入库）。
_MAX_STEM_CHARS = 8000
_MAX_SOLUTION_CHARS = 20000
_MAX_OPTIONS = 10
_MAX_OPTION_CHARS = 2000
_MAX_KNOWLEDGE_POINTS = 20
_MAX_KP_NAME_CHARS = 128


class QuestionBankError(ValueError):
    """题库管理操作的业务性拒绝（路由层转 4xx）。"""


def normalize_stem_hash(stem: str) -> str:
    """题干规范化哈希：NFKC + 去空白 + 去标点后 sha256，用于批内查重。"""
    normalized = unicodedata.normalize("NFKC", str(stem or "")).lower()
    normalized = re.sub(r"[\s\u3000]+", "", normalized)
    normalized = re.sub(
        r"[\u0021-\u002f\u003a-\u0040\u005b-\u0060\u007b-\u007e"
        r"\u3000-\u303f\uff00-\uffef\u2018\u2019\u201c\u201d\u2026]",
        "",
        normalized,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_docx_expansion(file_bytes: bytes) -> None:
    """解析前校验 docx 中央目录（防 zip 炸弹 + 伪 docx）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                # 合法 zip 但非 wordprocessingml（如 .zip/.doc 改名 .docx）：
                # python-docx 兜底路径会抛 KeyError 落到裸 500。
                raise QuestionBankError(
                    "仅支持 .docx 模板文件（未找到 Word 文档主体，请勿上传重命名的 zip/doc）"
                )
            total = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > _MAX_DOCX_ENTRY_BYTES:
                    raise QuestionBankError("文档内单个条目解压后超过 64MB，已拒绝")
                total += info.file_size
    except zipfile.BadZipFile as exc:
        raise QuestionBankError("仅支持 .docx 模板文件（zip 结构损坏）") from exc
    if total > _MAX_DOCX_TOTAL_BYTES:
        raise QuestionBankError("文档解压后总量超过 256MB，已拒绝")


# ── 批次 ─────────────────────────────────────────────────────────────────


async def create_batch(
    session: AsyncSession,
    *,
    file_name: str,
    file_bytes: bytes,
    created_by: str,
) -> QuestionBankBatch:
    """解析上传的 docx 并创建待确认批次。"""
    if not file_bytes:
        raise QuestionBankError("上传文件为空")
    if len(file_bytes) > MAX_DOCX_BYTES:
        raise QuestionBankError("文件超过 20MB 上限")
    if file_bytes[:2] != b"PK":
        raise QuestionBankError("仅支持 .docx 模板文件（检测到非 zip 格式）")
    _validate_docx_expansion(file_bytes)

    known_kps = {name for (name,) in (await session.execute(
        select(QuestionBankKnowledgePoint.name)
    )).all()}
    # docx 解析（pandoc 子进程）与逐图 MinIO 上传是秒级重 IO：先释放本请求的
    # DB 事务（连接回池），批次行在解析结束后才插入提交。
    await session.rollback()
    try:
        parse_result: ParseResult = await _run_parse(file_bytes, known_kps)
    except QuestionBankError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 解析器内部异常（畸形文档结构等）收敛为 422，不再裸 500。
        logger.warning("[QuestionBank] docx 解析异常: %s", type(exc).__name__)
        raise QuestionBankError(
            "文档解析失败：请确认使用标准模板（【题型】【题干】… + ==== 分隔行）的 .docx 文件"
        ) from exc
    if not parse_result.questions:
        raise QuestionBankError(
            "未解析到任何题目：请确认使用标准模板（【题型】【题干】… 标记 + ==== 分隔行）"
        )

    batch_id = uuid.uuid4().hex
    serialized: list[dict] = []
    for question in parse_result.questions:
        question_id = f"q{uuid.uuid4().hex[:16]}"
        image_entries: list[dict] = []
        for seq, image in enumerate(question.images, start=1):
            object_name = await qb_images.store_qb_image(
                image.data,
                batch_id=batch_id,
                question_id=question_id,
                seq=seq,
                ext=image.ext,
            )
            image_entries.append(
                {
                    "object_name": object_name,
                    "caption": "",
                    "seq": seq,
                    "placeholder_id": image.placeholder_id,
                }
            )
        serialized.append(parsed_question_to_dict(question, seq=len(serialized) + 1))
        serialized[-1]["id"] = question_id
        serialized[-1]["images"] = image_entries

    # 批内查重：同哈希多题只保留首个，其余标 error。
    seen_hashes: dict[str, int] = {}
    for item in serialized:
        stem_hash = normalize_stem_hash(item["stem"])
        if stem_hash in seen_hashes:
            item["has_error"] = True
            item["issues"].append(
                {
                    "level": "error",
                    "message": f"与本批第 {seen_hashes[stem_hash]} 题题干重复",
                }
            )
        elif item["stem"]:
            seen_hashes[stem_hash] = item["seq"]

    batch = QuestionBankBatch(
        id=batch_id,
        file_name=str(file_name or "upload.docx")[:255],
        file_key=None,
        status="parsed",
        total_count=len(serialized),
        valid_count=sum(1 for item in serialized if not item["has_error"]),
        parsed_json={"questions": serialized, "parser": parse_result.parser_path},
        created_by=created_by,
    )
    session.add(batch)
    await session.commit()
    return batch


async def _run_parse(file_bytes: bytes, known_kps: set[str]) -> ParseResult:
    import asyncio

    return await asyncio.to_thread(
        parse_question_docx, file_bytes, known_knowledge_points=known_kps
    )


def batch_to_dict(batch: QuestionBankBatch, *, include_questions: bool = False) -> dict:
    payload = {
        "id": batch.id,
        "file_name": batch.file_name,
        "status": batch.status,
        "total_count": batch.total_count,
        "valid_count": batch.valid_count,
        "error": batch.error,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "confirmed_at": batch.confirmed_at.isoformat() if batch.confirmed_at else None,
    }
    if include_questions:
        parsed = batch.parsed_json or {}
        payload["parser"] = parsed.get("parser")
        payload["questions"] = [
            _parsed_item_public(item) for item in parsed.get("questions") or []
        ]
    return payload


def _parsed_item_public(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "seq": item.get("seq"),
        "question_type": item.get("question_type"),
        "stem": item.get("stem"),
        "options": item.get("options") or [],
        "answer": item.get("answer"),
        "analysis": item.get("analysis"),
        "difficulty": item.get("difficulty"),
        "chapter": item.get("chapter"),
        "knowledge_points": item.get("knowledge_points") or [],
        "source_name": item.get("source_name"),
        "images": [
            {
                "object_name": image.get("object_name"),
                "caption": image.get("caption") or "",
                "seq": image.get("seq"),
                "url": qb_images.build_qb_image_url(image.get("object_name") or ""),
            }
            for image in item.get("images") or []
            if image.get("object_name")
        ],
        "issues": item.get("issues") or [],
        "has_error": bool(item.get("has_error")),
    }


async def get_batch(session: AsyncSession, batch_id: str) -> QuestionBankBatch | None:
    return await session.get(QuestionBankBatch, batch_id)


async def confirm_batch(
    session: AsyncSession, batch: QuestionBankBatch
) -> tuple[list[QuestionBankQuestion], int]:
    """把已解析批次的合法题目写入题目表（默认 published）。

    返回 ``(inserted, skipped_duplicates)``。跨批次题干查重命中的合法题跳过
    入库（warning 记入该题 issues），避免同一道题在练习列表与统计里分裂成
    两份；批次状态用条件 UPDATE 做 CAS，并发双击确认不再以主键冲突 500。
    """
    if batch.status != "parsed":
        raise QuestionBankError("仅待确认批次可执行入库")
    claimed = await session.execute(
        update(QuestionBankBatch)
        .where(
            QuestionBankBatch.id == batch.id,
            QuestionBankBatch.status == "parsed",
        )
        .values(status="confirming")
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise QuestionBankError("批次状态已变化（可能已被确认），请刷新后查看")

    parsed = batch.parsed_json or {}
    items = [
        item
        for item in parsed.get("questions") or []
        if not item.get("has_error")
    ]
    hashes = [normalize_stem_hash(item.get("stem") or "") for item in items]
    existing_by_hash: dict[str, str] = {}
    if hashes:
        rows = await session.execute(
            select(QuestionBankQuestion.id, QuestionBankQuestion.stem_hash).where(
                QuestionBankQuestion.stem_hash.in_(hashes),
                QuestionBankQuestion.status != "disabled",
            )
        )
        existing_by_hash = {str(stem_hash): str(row_id) for row_id, stem_hash in rows.all()}

    inserted: list[QuestionBankQuestion] = []
    skipped_duplicates = 0
    for item in items:
        stem_hash = normalize_stem_hash(item.get("stem") or "")
        existing_id = existing_by_hash.get(stem_hash)
        if existing_id is not None:
            skipped_duplicates += 1
            item.setdefault("issues", []).append(
                {
                    "level": "warning",
                    "message": (
                        f"题干与已有题目 QB-{existing_id} 重复，入库时已跳过"
                        "（可在题目管理中直接复用该题）"
                    ),
                }
            )
            item["has_error"] = True
            continue
        question = QuestionBankQuestion(
            id=item["id"],
            question_type=item.get("question_type") or "简答题",
            stem=item.get("stem") or "",
            options_json=item.get("options") or None,
            answer=item.get("answer") or None,
            analysis=item.get("analysis") or None,
            difficulty=item.get("difficulty"),
            chapter=(item.get("chapter") or None),
            knowledge_points_json=item.get("knowledge_points") or None,
            images_json=[
                {
                    "object_name": image["object_name"],
                    "caption": image.get("caption") or "",
                    "seq": image.get("seq", index + 1),
                }
                for index, image in enumerate(item.get("images") or [])
                if image.get("object_name")
            ]
            or None,
            status="published",
            source_batch_id=batch.id,
            source_name=(item.get("source_name") or None),
            stem_hash=stem_hash,
            created_by=batch.created_by,
        )
        session.add(question)
        inserted.append(question)
    batch.parsed_json = parsed
    batch.status = "confirmed"
    batch.confirmed_at = datetime.now()
    batch.valid_count = len(inserted)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise QuestionBankError(
            "批次确认失败：题目可能已被其他会话入库，请刷新后查看"
        ) from exc
    return inserted, skipped_duplicates


async def delete_batch(session: AsyncSession, batch: QuestionBankBatch) -> None:
    if batch.status == "confirmed":
        raise QuestionBankError("已确认批次请通过题目管理下线题目")
    await session.delete(batch)
    await session.commit()


# ── 题目 ─────────────────────────────────────────────────────────────────


async def question_page(
    session: AsyncSession,
    *,
    question_type: str | None = None,
    difficulty: str | None = None,
    knowledge_point: str | None = None,
    chapter: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[QuestionBankQuestion], int]:
    stmt = select(QuestionBankQuestion)
    if question_type:
        stmt = stmt.where(QuestionBankQuestion.question_type == question_type)
    if difficulty:
        stmt = stmt.where(QuestionBankQuestion.difficulty == difficulty)
    if status:
        stmt = stmt.where(QuestionBankQuestion.status == status)
    else:
        stmt = stmt.where(QuestionBankQuestion.status != "disabled")
    if chapter:
        stmt = stmt.where(QuestionBankQuestion.chapter.like(f"%{_escape_like(chapter)}%", escape="\\"))
    if knowledge_point:
        # 知识点按 JSON 数组元素精确匹配（两侧引号定界），「弯曲内力」不再
        # 误命中标注为「弯曲内力图」的题。字典侧已禁止名称含引号/反斜杠。
        # 方言注记：MySQL JSON 转文本是原始 CJK，本过滤在生产有效；SQLite
        # 方言把 JSON 存成 ensure_ascii 转义文本，中文 LIKE 在测试库不命中
        #（与 delete_knowledge_point 改 Python 侧判断的原因相同）。
        needle = f'%"{_escape_like(knowledge_point)}"%'
        stmt = stmt.where(
            QuestionBankQuestion.knowledge_points_json.like(needle, escape="\\")
        )
    if keyword:
        clause = f"%{_escape_like(keyword)}%"
        stmt = stmt.where(
            QuestionBankQuestion.stem.like(clause, escape="\\")
            | QuestionBankQuestion.analysis.like(clause, escape="\\")
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await session.execute(
            stmt.order_by(QuestionBankQuestion.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows), int(total)


def question_to_dict(question: QuestionBankQuestion, *, with_solution: bool = True) -> dict:
    payload = {
        "id": question.id,
        "question_type": question.question_type,
        "stem": question.stem,
        "options": question.options_json or [],
        "score": question.score,
        "difficulty": question.difficulty,
        "chapter": question.chapter,
        "knowledge_points": question.knowledge_points_json or [],
        "images": [
            {
                "object_name": image.get("object_name"),
                "caption": image.get("caption") or "",
                "seq": image.get("seq", index + 1),
                "url": qb_images.build_qb_image_url(image.get("object_name") or ""),
            }
            for index, image in enumerate(question.images_json or [])
            if image.get("object_name")
        ],
        "status": question.status,
        "source_batch_id": question.source_batch_id,
        "source_name": question.source_name,
        "created_at": question.created_at.isoformat() if question.created_at else None,
        "updated_at": question.updated_at.isoformat() if question.updated_at else None,
    }
    if with_solution:
        payload["answer"] = question.answer
        payload["analysis"] = question.analysis
    return payload


async def get_question(
    session: AsyncSession, question_id: str
) -> QuestionBankQuestion | None:
    return await session.get(QuestionBankQuestion, question_id)


_EDITABLE_FIELDS = {
    "question_type",
    "stem",
    "options",
    "answer",
    "analysis",
    "score",
    "difficulty",
    "chapter",
    "knowledge_points",
    "source_name",
    "status",
}


def _validate_question_fields(fields: dict, *, partial: bool) -> None:
    """create/update 共用的字段校验（枚举 + 长度/条数上界）。"""
    if not partial or "question_type" in fields:
        if fields.get("question_type") not in QUESTION_TYPE_VALUES:
            raise QuestionBankError(f"题型仅支持 {'/'.join(QUESTION_TYPE_VALUES)}")
    if not partial or "difficulty" in fields:
        if fields.get("difficulty") not in {None, "", *DIFFICULTY_VALUES}:
            raise QuestionBankError(f"难度仅支持 {'/'.join(DIFFICULTY_VALUES)}")
    if "status" in fields and fields["status"] not in STATUS_VALUES:
        raise QuestionBankError(f"状态仅支持 {'/'.join(STATUS_VALUES)}")
    if not partial or "stem" in fields:
        stem = str(fields.get("stem") or "")
        if not stem.strip():
            raise QuestionBankError("题干不能为空")
        if len(stem) > _MAX_STEM_CHARS:
            raise QuestionBankError(f"题干超过 {_MAX_STEM_CHARS} 字上限")
    if "answer" in fields and len(str(fields.get("answer") or "")) > _MAX_SOLUTION_CHARS:
        raise QuestionBankError(f"答案超过 {_MAX_SOLUTION_CHARS} 字上限")
    if "analysis" in fields and len(str(fields.get("analysis") or "")) > _MAX_SOLUTION_CHARS:
        raise QuestionBankError(f"解析超过 {_MAX_SOLUTION_CHARS} 字上限")
    if "options" in fields and fields.get("options") is not None:
        options = fields["options"]
        if not isinstance(options, list) or len(options) > _MAX_OPTIONS:
            raise QuestionBankError(f"选项必须是列表且不超过 {_MAX_OPTIONS} 项")
        for option in options:
            if not isinstance(option, dict) or set(option) - {"key", "text"}:
                raise QuestionBankError("选项条目只允许 key/text 两个字段")
            if len(str(option.get("text") or "")) > _MAX_OPTION_CHARS:
                raise QuestionBankError(f"选项内容超过 {_MAX_OPTION_CHARS} 字上限")
    if "knowledge_points" in fields and fields.get("knowledge_points") is not None:
        points = fields["knowledge_points"]
        if not isinstance(points, list) or len(points) > _MAX_KNOWLEDGE_POINTS:
            raise QuestionBankError(
                f"知识点必须是列表且不超过 {_MAX_KNOWLEDGE_POINTS} 项"
            )
        for name in points:
            if len(str(name or "").strip()) > _MAX_KP_NAME_CHARS:
                raise QuestionBankError(f"单个知识点名称超过 {_MAX_KP_NAME_CHARS} 字")


async def _assert_stem_not_duplicated(
    session: AsyncSession, *, stem: str, exclude_question_id: str | None = None
) -> None:
    """跨批次题干查重：与非 disabled 的已有题目同哈希时拒绝（422）。"""
    stem_hash = normalize_stem_hash(stem)
    stmt = select(QuestionBankQuestion.id).where(
        QuestionBankQuestion.stem_hash == stem_hash,
        QuestionBankQuestion.status != "disabled",
    )
    if exclude_question_id is not None:
        stmt = stmt.where(QuestionBankQuestion.id != exclude_question_id)
    existing_id = await session.scalar(stmt.limit(1))
    if existing_id is not None:
        raise QuestionBankError(
            f"题干与已有题目 QB-{existing_id} 重复（题库按题干全局查重，请编辑原有题目）"
        )


async def update_question(
    session: AsyncSession,
    question: QuestionBankQuestion,
    *,
    changes: dict,
) -> QuestionBankQuestion:
    unknown = set(changes) - _EDITABLE_FIELDS
    if unknown:
        raise QuestionBankError(f"不可编辑字段: {', '.join(sorted(unknown))}")
    _validate_question_fields(changes, partial=True)

    if "stem" in changes:
        await _assert_stem_not_duplicated(
            session, stem=str(changes["stem"]), exclude_question_id=question.id
        )

    if "question_type" in changes:
        question.question_type = changes["question_type"]
    if "stem" in changes:
        question.stem = str(changes["stem"]).strip()
        question.stem_hash = normalize_stem_hash(question.stem)
    if "options" in changes:
        question.options_json = changes["options"] or None
    if "answer" in changes:
        question.answer = str(changes["answer"] or "").strip() or None
    if "analysis" in changes:
        question.analysis = str(changes["analysis"] or "").strip() or None
    if "score" in changes:
        question.score = str(changes["score"] or "").strip()[:32] or None
    if "difficulty" in changes:
        question.difficulty = (str(changes["difficulty"] or "").strip() or None)
    if "chapter" in changes:
        question.chapter = str(changes["chapter"] or "").strip()[:128] or None
    if "knowledge_points" in changes:
        question.knowledge_points_json = list(changes["knowledge_points"] or []) or None
    if "source_name" in changes:
        question.source_name = str(changes["source_name"] or "").strip()[:255] or None
    if "status" in changes:
        question.status = changes["status"]
    question.updated_at = datetime.now()
    await session.commit()
    return question


async def create_question(
    session: AsyncSession,
    *,
    created_by: str,
    fields: dict,
    image_blobs: list[tuple[bytes, str, str]] | None = None,
) -> QuestionBankQuestion:
    """手工单题录入（含直接贴图）。``image_blobs``: [(bytes, ext, caption)]。"""
    _validate_question_fields(fields, partial=False)
    stem = str(fields.get("stem") or "").strip()
    await _assert_stem_not_duplicated(session, stem=stem)

    question_id = uuid.uuid4().hex
    images_json: list[dict] | None = None
    if image_blobs:
        entries = []
        for seq, (blob, ext, caption) in enumerate(image_blobs, start=1):
            object_name = await qb_images.store_qb_image(
                blob,
                batch_id="manual",
                question_id=question_id,
                seq=seq,
                ext=ext,
            )
            entries.append({"object_name": object_name, "caption": caption or "", "seq": seq})
        images_json = entries

    question = QuestionBankQuestion(
        id=question_id,
        question_type=fields.get("question_type"),
        stem=stem,
        options_json=fields.get("options") or None,
        answer=str(fields.get("answer") or "").strip() or None,
        analysis=str(fields.get("analysis") or "").strip() or None,
        score=str(fields.get("score") or "").strip()[:32] or None,
        difficulty=(str(fields.get("difficulty") or "").strip() or None),
        chapter=str(fields.get("chapter") or "").strip()[:128] or None,
        knowledge_points_json=list(fields.get("knowledge_points") or []) or None,
        images_json=images_json,
        status="published",
        source_batch_id=None,
        source_name=str(fields.get("source_name") or "").strip()[:255] or None,
        stem_hash=normalize_stem_hash(stem),
        created_by=created_by,
    )
    session.add(question)
    await session.commit()
    return question


# 补图的读-改-写锁：images_json 是整列覆盖写，Agent API 单 worker 下进程内
# 按题加锁即可防并发补图互相覆盖（丢图后 MinIO 里就是永久孤儿）。
_IMAGE_WRITE_LOCKS: dict[str, asyncio.Lock] = {}


def _image_write_lock(question_id: str) -> asyncio.Lock:
    lock = _IMAGE_WRITE_LOCKS.get(question_id)
    if lock is None:
        lock = asyncio.Lock()
        if len(_IMAGE_WRITE_LOCKS) > 4096:
            # 有界：只保留活跃锁，防恶意 id 刷键膨胀（锁本身无状态，重建无害）。
            _IMAGE_WRITE_LOCKS.clear()
        _IMAGE_WRITE_LOCKS[question_id] = lock
    return lock


async def append_question_image(
    session: AsyncSession,
    question: QuestionBankQuestion,
    *,
    blob: bytes,
    ext: str,
    caption: str = "",
) -> dict:
    async with _image_write_lock(question.id):
        # 锁内重读最新 images_json：路由加载的快照可能已被并发补图写新。
        await session.refresh(question, attribute_names=["images_json"])
        images = list(question.images_json or [])
        seq = len(images) + 1
        object_name = await qb_images.store_qb_image(
            blob,
            batch_id=question.source_batch_id or "manual",
            question_id=question.id,
            seq=seq,
            ext=ext,
        )
        images.append({"object_name": object_name, "caption": caption or "", "seq": seq})
        question.images_json = images
        question.updated_at = datetime.now()
        await session.commit()
    return {
        "object_name": object_name,
        "caption": caption or "",
        "seq": seq,
        "url": qb_images.build_qb_image_url(object_name),
    }


async def remove_question_image(
    session: AsyncSession,
    question: QuestionBankQuestion,
    *,
    seq: int,
) -> None:
    images = [image for image in (question.images_json or []) if image.get("seq") != seq]
    question.images_json = images or None
    question.updated_at = datetime.now()
    await session.commit()


# ── 知识点字典 ───────────────────────────────────────────────────────────


async def list_knowledge_points(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            select(QuestionBankKnowledgePoint).order_by(
                QuestionBankKnowledgePoint.sort.asc(),
                QuestionBankKnowledgePoint.created_at.asc(),
            )
        )
    ).scalars().all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "chapter": row.chapter,
            "parent_id": row.parent_id,
            "sort": row.sort,
        }
        for row in rows
    ]


def _validate_kp_name(name: str) -> str:
    """知识点名称清洗：非空、限长，且禁止引号/反斜杠。

    名称会被拼进 ``knowledge_points_json`` 的引号定界精确匹配（列表筛选），
    含引号/反斜杠的名称无法被精确检索，也不会破坏定界语义。
    """
    cleaned = str(name or "").strip()[:_MAX_KP_NAME_CHARS]
    if not cleaned:
        raise QuestionBankError("知识点名称不能为空")
    if '"' in cleaned or "\\" in cleaned:
        raise QuestionBankError('知识点名称不能包含引号或反斜杠')
    return cleaned


async def create_knowledge_point(
    session: AsyncSession, *, name: str, chapter: str | None = None, created_by: str | None = None
) -> QuestionBankKnowledgePoint:
    cleaned = _validate_kp_name(name)
    exists = await session.scalar(
        select(QuestionBankKnowledgePoint).where(
            QuestionBankKnowledgePoint.name == cleaned
        )
    )
    if exists is not None:
        raise QuestionBankError(f"知识点「{cleaned}」已存在")
    point = QuestionBankKnowledgePoint(
        name=cleaned,
        chapter=(str(chapter or "").strip()[:128] or None),
        created_by=created_by,
    )
    session.add(point)
    await session.commit()
    return point


async def update_knowledge_point(
    session: AsyncSession,
    point: QuestionBankKnowledgePoint,
    *,
    changes: dict,
) -> QuestionBankKnowledgePoint:
    if "name" in changes:
        cleaned = _validate_kp_name(changes["name"])
        if cleaned != point.name:
            exists = await session.scalar(
                select(QuestionBankKnowledgePoint).where(
                    QuestionBankKnowledgePoint.name == cleaned
                )
            )
            if exists is not None:
                raise QuestionBankError(f"知识点「{cleaned}」已存在")
            point.name = cleaned
    if "chapter" in changes:
        point.chapter = str(changes["chapter"] or "").strip()[:128] or None
    if "sort" in changes:
        try:
            point.sort = int(changes["sort"])
        except (TypeError, ValueError):
            raise QuestionBankError("排序必须为整数") from None
    await session.commit()
    return point


async def delete_knowledge_point(
    session: AsyncSession, point: QuestionBankKnowledgePoint
) -> None:
    """删除字典项前校验引用：题目标注仍指向该名称时拒绝（防悬空标注）。

    引用判断在 Python 侧做精确成员比较而非 JSON 列 LIKE：SQLite 方言把
    JSON 序列化成 ensure_ascii 的 \\u 转义文本（MySQL 是原始 CJK 文本），
    任何中文 LIKE 模式都无法同时命中两种存储形态。删除是教师低频操作，
    只投影 knowledge_points_json 一列的代价可接受。
    """
    rows = await session.execute(
        select(QuestionBankQuestion.knowledge_points_json).where(
            QuestionBankQuestion.status != "disabled",
            QuestionBankQuestion.knowledge_points_json.isnot(None),
        )
    )
    referenced = sum(
        1
        for (points,) in rows.all()
        if points and any(str(name) == point.name for name in points)
    )
    if referenced:
        raise QuestionBankError(
            f"知识点「{point.name}」仍被 {referenced} 道题目标注引用，请先清理标注"
        )
    await session.delete(point)
    await session.commit()


async def knowledge_point_names(session: AsyncSession) -> list[str]:
    rows = await session.execute(select(QuestionBankKnowledgePoint.name))
    return [str(name) for (name,) in rows.all()]
