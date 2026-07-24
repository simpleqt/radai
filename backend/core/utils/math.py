"""Shared numerical helpers."""

from __future__ import annotations

import numpy as np


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return cosine similarity, treating zero vectors as unrelated."""
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


__all__ = ["cosine_similarity"]
