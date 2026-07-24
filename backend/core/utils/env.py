"""Environment parsing helpers shared across backend components."""

from __future__ import annotations

import math
import os


def positive_float_from_env(
    name: str, default: float, *, minimum: float = 1.0
) -> float:
    """Read a finite positive float without breaking module import."""
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value) if math.isfinite(value) else default
