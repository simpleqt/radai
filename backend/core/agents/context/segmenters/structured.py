"""Segmenters for project-table and groundwater-quality results."""

from __future__ import annotations

import json
import time
from typing import Any

from backend.core.agents.context.objects import (
    ContextObject,
    Segment,
    make_context_object,
    make_segment,
    sanitize_sid,
    sid_content_digest,
)


class ProjectTableSegmenter:
    schema = "table/v1"

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject:
        payload = _as_dict(result)
        segments: list[Segment] = []
        rows = payload.get("rows")
        # _as_dict 恒返回 dict:对非 JSON 文本或 error 载荷,原先无条件生成
        # "project_id=None; ..." 的垃圾 schema 段,raw 兜底永不触发,原文
        # 不进任何段。只有载荷带真实表格键时才生成 schema 段。
        if any(
            payload.get(key) is not None
            for key in (
                "project_id",
                "data_version",
                "query_kind",
                "row_count",
                "columns",
                "rows",
            )
        ):
            columns = list(payload.get("columns") or [])
            schema_text = (
                f"project_id={payload.get('project_id')}; "
                f"data_version={payload.get('data_version')}; "
                f"query_kind={payload.get('query_kind')}; "
                f"row_count={payload.get('row_count', len(rows) if isinstance(rows, list) else 0)}; "
                f"columns={','.join(str(item) for item in columns)}"
            )
            segments.append(
                make_segment(
                    # sid 掺内容指纹:同请求内两次不同参数的表查询各自产生
                    # tbl:schema/tbl:rows:N,固定名会被台账裸 sid 去重误判为
                    # 重复片段,第二次查询的数据整体指针化丢失。
                    sid=f"tbl:schema:{sid_content_digest(schema_text)}",
                    text=schema_text,
                    title="表结构与查询说明",
                    locator="schema",
                    score=1.0,
                    tags=["table", "schema"],
                )
            )
        if isinstance(rows, list):
            block_size = 20
            for index in range(0, max(1, len(rows)), block_size):
                block = rows[index : index + block_size]
                text = json.dumps(block, ensure_ascii=False, default=str)
                segments.append(
                    make_segment(
                        sid=(
                            f"tbl:rows:{index // block_size + 1}:"
                            f"{sid_content_digest(text)}"
                        ),
                        text=text,
                        title=f"行样本块 {index // block_size + 1}",
                        locator=f"rows[{index}:{index + len(block)}]",
                        score=max(0.1, 1.0 - (index // block_size) * 0.1),
                        tags=["table", "rows"],
                    )
                )
        if not segments:
            # 非 JSON 载荷解析为 {}:序列化空 dict 会丢原文,兜底段必须
            # 保留原始文本。
            raw_text = (
                json.dumps(payload, ensure_ascii=False, default=str)
                if payload
                else str(result or "")
            )
            segments.append(
                make_segment(
                    sid=f"tbl:raw:{sid_content_digest(raw_text)}",
                    text=raw_text,
                    title="表格原始结果",
                    locator="raw",
                    score=0.5,
                )
            )
        return make_context_object(
            schema=self.schema,
            origin={
                "tool": "query_project_tables",
                "args_digest": "",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "created_at": time.time(),
                "last_seen_at": time.time(),
            },
            index={"stats": {"segments": len(segments), "rows": len(rows) if isinstance(rows, list) else 0}},
            segments=segments,
        )


class GroundwaterQualitySegmenter:
    schema = "groundwater_quality_check/v1"

    def segment(
        self,
        result: Any,
        *,
        tool_args: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
        user_id: str | None,
    ) -> ContextObject:
        payload = _as_dict(result)
        segments: list[Segment] = []
        # 与 ProjectTableSegmenter 同理:非 JSON 载荷解析为 {} 时不生成
        # 全 None 汇总段,走 raw 兜底保留原文。
        if (
            isinstance(payload, dict)
            and not payload.get("error")
            and any(
                payload.get(key) is not None
                for key in (
                    "project_id",
                    "point_code",
                    "standard_class",
                    "layer_completeness",
                    "exceedances",
                )
            )
        ):
            point_code = str(payload.get("point_code") or "unknown")
            summary_lines = [
                "GROUNDWATER_QUALITY_CHECK_RESULT",
                (
                    f"project_id={payload.get('project_id')}; "
                    f"data_version={payload.get('data_version')}; "
                    f"point_code={point_code}; "
                    f"standard_class={payload.get('standard_class')}"
                ),
                (
                    "completeness="
                    f"present={dict(payload.get('layer_completeness') or {}).get('present_layers')}; "
                    f"missing={dict(payload.get('layer_completeness') or {}).get('missing_layers')}"
                ),
            ]
            summary_text = "\n".join(summary_lines)
            segments.append(
                make_segment(
                    sid=f"gwq:{point_code}:summary:{sid_content_digest(summary_text)}",
                    text=summary_text,
                    title="水质检查汇总",
                    locator=point_code,
                    score=1.0,
                    tags=["groundwater", "quality", "summary"],
                )
            )
            for index, row in enumerate(payload.get("exceedances") or [], start=1):
                if not isinstance(row, dict):
                    continue
                param = str(
                    row.get("parameter_name")
                    or row.get("parameter_code")
                    or f"row-{index}"
                )
                text = json.dumps(row, ensure_ascii=False, default=str)
                sid = sanitize_sid(
                    f"gwq:{point_code}:{param}:{sid_content_digest(text)}"
                )
                segments.append(
                    make_segment(
                        sid=sid,
                        text=text,
                        title=f"超标记录 {param}",
                        locator=f"{point_code}:{param}",
                        score=max(0.7, 1.0 - index * 0.05),
                        tags=["groundwater", "quality", "exceedance"],
                    )
                )
        if not segments:
            raw_text = (
                json.dumps(payload, ensure_ascii=False, default=str)
                if payload
                else str(result or "")
            )
            segments.append(
                make_segment(
                    sid=f"gwq:raw:{sid_content_digest(raw_text)}",
                    text=raw_text,
                    title="水质检查原始结果",
                    locator="raw",
                    score=0.5,
                )
            )
        return make_context_object(
            schema=self.schema,
            origin={
                "tool": "groundwater_quality_check",
                "args_digest": "",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "created_at": time.time(),
                "last_seen_at": time.time(),
            },
            index={"stats": {"segments": len(segments)}},
            segments=segments,
        )


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}
    return {}


__all__ = ["GroundwaterQualitySegmenter", "ProjectTableSegmenter"]
