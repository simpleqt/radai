"""Shared enum for the multi-KB search tool contract.

Vendored from omnigraph ``rag.content_filter`` so the remote HTTP client can
serialize the filter mode without importing the knowledge service internals.
"""

from __future__ import annotations

from enum import Enum


class FilterMode(str, Enum):
    PPR_ONLY = "ppr_only"
    PPR_RERANK = "ppr_rerank"
    PPR_MMR = "ppr_mmr"
    FULL = "full"
    MMR_ONLY = "mmr_only"
    MMR_TIME_AWARE = "mmr_time_aware"
    RERANK_MMR = "rerank_mmr"
    RERANK = "rerank"
    PPR_RERANK_MMR = "ppr_rerank_mmr"
    RAW = "raw"
