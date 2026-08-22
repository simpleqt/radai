"""Tool result segmenters and the default registry."""

from __future__ import annotations

from backend.core.agents.context.segmenters.base import Segmenter, SegmenterRegistry
from backend.core.agents.context.segmenters.fallback import FallbackSegmenter
from backend.core.agents.context.segmenters.multi_kb import MultiKbSegmenter
from backend.core.agents.context.segmenters.structured import (
    GroundwaterQualitySegmenter,
    ProjectTableSegmenter,
)
from backend.core.agents.context.segmenters.task import TaskSegmenter
from backend.core.agents.context.segmenters.web_search import WebSearchSegmenter


def create_default_segmenter_registry() -> SegmenterRegistry:
    return SegmenterRegistry(
        {
            "multi_kb_search": MultiKbSegmenter(),
            "web_search": WebSearchSegmenter(),
            "query_project_tables": ProjectTableSegmenter(),
            "inspect_project_tables": ProjectTableSegmenter(),
            "summarize_project_table": ProjectTableSegmenter(),
            "groundwater_quality_check": GroundwaterQualitySegmenter(),
            "task": TaskSegmenter(),
        },
        fallback=FallbackSegmenter(),
    )


__all__ = [
    "FallbackSegmenter",
    "GroundwaterQualitySegmenter",
    "MultiKbSegmenter",
    "ProjectTableSegmenter",
    "Segmenter",
    "SegmenterRegistry",
    "TaskSegmenter",
    "WebSearchSegmenter",
    "create_default_segmenter_registry",
]
