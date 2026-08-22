"""上下文台账:token 预算、共享校准与计数缓存(拆自 context/ledger.py)。"""

from __future__ import annotations

import logging

import math
import os
import threading

from backend.core.agents.context.summary import normalize_text
from collections import OrderedDict
from dataclasses import dataclass

try:  # pragma: no cover - exercised when optional wheel is present.
    import tiktoken
except Exception:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LedgerBudget:
    model_max_input_tokens: int = 8192
    reserved_output_tokens: int = 2048
    safety_margin_tokens: int = 768

    @property
    def input_budget_tokens(self) -> int:
        return max(
            1024,
            self.model_max_input_tokens
            - self.reserved_output_tokens
            - self.safety_margin_tokens,
        )

    @classmethod
    def from_model_name(
        cls,
        model_name: str | None,
        *,
        safety_margin_tokens: int = 768,
    ) -> LedgerBudget:
        """Build a ledger budget from configured model limits, preferring MySQL."""

        try:
            from backend.core.models.context_limits import (
                resolve_max_input_tokens,
                resolve_max_tokens,
            )

            max_input = resolve_max_input_tokens(model_name=model_name)
            reserved_output = resolve_max_tokens(model_name=model_name)
        except Exception:
            # A silent 8K fallback squeezes large models into aggressive
            # compaction; deployments that know their smallest model can raise
            # the floor without code changes.
            raw_fallback = os.getenv("MODEL_INPUT_BUDGET_FALLBACK_TOKENS", "").strip()
            try:
                fallback_input = (
                    max(2048, int(raw_fallback))
                    if raw_fallback
                    else cls.model_max_input_tokens
                )
            except ValueError:
                fallback_input = cls.model_max_input_tokens
            logger.warning(
                "Failed to resolve model context limits; falling back to "
                "%d input tokens: model=%s",
                fallback_input,
                model_name,
                exc_info=True,
            )
            max_input = fallback_input
            reserved_output = cls.reserved_output_tokens
        resolved_input = max(2048, int(max_input or cls.model_max_input_tokens))
        resolved_margin = max(0, int(safety_margin_tokens))
        max_reserved = max(256, resolved_input - resolved_margin - 1024)
        resolved_output = min(
            max_reserved,
            max(256, int(reserved_output or cls.reserved_output_tokens)),
        )
        return cls(
            model_max_input_tokens=resolved_input,
            reserved_output_tokens=resolved_output,
            safety_margin_tokens=resolved_margin,
        )


_CALIBRATION_SAFETY = 1.10


_CALIBRATION_EMA_ALPHA = 0.3


_CALIBRATION_MIN_FACTOR = 0.2


_CALIBRATION_MAX_FACTOR = 2.5


# Downward corrections need a trustworthy sample; upward (safety) corrections
# are always accepted regardless of sample size.
_CALIBRATION_MIN_DOWNWARD_SAMPLE_TOKENS = 1000


# A single observation may lower the factor by at most 30%.
_CALIBRATION_MAX_DOWNWARD_STEP = 0.7


# A single observation may raise the factor by at most 50%. Genuine
# underestimates repeat and still converge within a few observations (and a
# real provider overflow raises the factor through note_context_overflow
# independently), while one bogus gateway usage report can no longer poison
# the process-shared factor straight to the 2.5 ceiling.
_CALIBRATION_MAX_UPWARD_STEP = 1.5


_COUNT_CACHE_MAX_ENTRIES = 2048


_COUNT_CACHE_MIN_CHARS = 128


class _SharedCalibration:
    """Process-wide token calibration state for one (method, model) pair."""

    __slots__ = ("lock", "factor", "raw_ratio_ema", "samples")

    def __init__(self, factor: float) -> None:
        self.lock = threading.Lock()
        self.factor = float(factor)
        # EMA of actual_tokens / UNCALIBRATED estimate, stable across factor
        # changes so samples taken under different factors stay comparable.
        self.raw_ratio_ema = 0.0
        self.samples = 0


_SHARED_CALIBRATION_LOCK = threading.Lock()


_SHARED_CALIBRATIONS: dict[str, _SharedCalibration] = {}


def _shared_calibration_enabled() -> bool:
    return (
        os.getenv("AGENT_TOKEN_CALIBRATION_SHARED", "on").strip().lower()
        not in {"off", "0", "false"}
    )


def _shared_calibration(key: str, default_factor: float) -> _SharedCalibration:
    with _SHARED_CALIBRATION_LOCK:
        state = _SHARED_CALIBRATIONS.get(key)
        if state is None:
            state = _SharedCalibration(default_factor)
            _SHARED_CALIBRATIONS[key] = state
        return state


def reset_shared_token_calibration() -> None:
    """Clear process-wide calibration state (tests and controlled restarts)."""

    with _SHARED_CALIBRATION_LOCK:
        _SHARED_CALIBRATIONS.clear()


def count_message_tokens(counter, message: object) -> int:
    """Shared per-message token formula for history fitting and compaction.

    Only requires a ``count`` method so duck-typed counters keep working.
    """

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and "tool_calls" in additional_kwargs:
        # OpenAI-compatible gateways mirror ``.tool_calls`` into
        # ``additional_kwargs["tool_calls"]``; counting both double-charged
        # every tool round and skewed compaction triggers upward.
        additional_kwargs = {
            key: value
            for key, value in additional_kwargs.items()
            if key != "tool_calls"
        }
    return (
        counter.count(getattr(message, "content", message))
        + counter.count(getattr(message, "tool_calls", None))
        + counter.count(additional_kwargs)
        + counter.count(getattr(message, "name", None))
        + 6
    )


class TokenCounter:
    """Use a model tokenizer when known, otherwise a calibrated conservative estimate.

    Calibration is bidirectional and shared per (method, model name) across the
    process: underestimates raise the factor immediately (safety first), while
    consistently observed overestimates converge the factor downward toward the
    real ratio plus a safety margin. Without sharing, every request-scoped
    counter restarted from the static default and CJK-heavy deployments paid a
    2-3x context tax. ``AGENT_TOKEN_CALIBRATION_SHARED=off`` restores the
    legacy per-instance, upward-only behavior.
    """

    def __init__(self, model_name: str | None = None):
        self._encoding = None
        self._method = "mixed_language_heuristic"
        default_factor = 1.15
        if tiktoken is not None:
            try:
                # No configured model historically means the OpenAI-compatible
                # default. Named non-OpenAI providers must not silently use it.
                self._encoding = tiktoken.encoding_for_model(model_name or "gpt-4")
                default_factor = 1.0
                self._method = "tiktoken_model"
            except Exception:
                # Qwen/GLM/DeepSeek/MiniMax tokenization is not cl100k. Using
                # that encoding would look exact while being model-inaccurate.
                self._encoding = None
                default_factor = 1.15
        self._default_factor = default_factor
        self._shared: _SharedCalibration | None = None
        normalized_name = str(model_name or "").strip().lower()
        if normalized_name and _shared_calibration_enabled():
            self._shared = _shared_calibration(
                f"{self._method}:{normalized_name}",
                default_factor,
            )
        self._calibration_factor = (
            self._shared.factor if self._shared is not None else default_factor
        )
        self._cache_lock = threading.Lock()
        self._raw_count_cache: OrderedDict[str, int] = OrderedDict()

    @property
    def method(self) -> str:
        calibrated = abs(self._calibration_factor - self._default_factor) > 0.01
        suffix = "+server_calibrated" if calibrated else ""
        return f"{self._method}{suffix}"

    def observe(self, *, estimated_tokens: int, actual_tokens: int) -> None:
        if estimated_tokens <= 0 or actual_tokens <= 0:
            return
        factor = max(self._calibration_factor, 1e-6)
        # actual vs UNCALIBRATED estimate; comparable across factor changes.
        raw_ratio = (actual_tokens / estimated_tokens) * factor
        underestimated = actual_tokens > estimated_tokens
        if not underestimated and (
            estimated_tokens < _CALIBRATION_MIN_DOWNWARD_SAMPLE_TOKENS
        ):
            return
        if self._shared is None:
            # Legacy behavior without the shared registry: raise-only.
            if underestimated:
                self._calibration_factor = min(
                    _CALIBRATION_MAX_FACTOR,
                    max(
                        factor,
                        min(
                            raw_ratio * 1.05,
                            factor * _CALIBRATION_MAX_UPWARD_STEP,
                        ),
                    ),
                )
            return
        with self._shared.lock:
            self._shared.samples += 1
            if self._shared.raw_ratio_ema <= 0:
                self._shared.raw_ratio_ema = raw_ratio
            else:
                self._shared.raw_ratio_ema += _CALIBRATION_EMA_ALPHA * (
                    raw_ratio - self._shared.raw_ratio_ema
                )
            if underestimated:
                self._shared.factor = min(
                    _CALIBRATION_MAX_FACTOR,
                    max(
                        self._shared.factor,
                        min(
                            raw_ratio * 1.05,
                            self._shared.factor * _CALIBRATION_MAX_UPWARD_STEP,
                        ),
                    ),
                )
            elif self._shared.samples >= 2:
                target = min(
                    _CALIBRATION_MAX_FACTOR,
                    max(
                        _CALIBRATION_MIN_FACTOR,
                        self._shared.raw_ratio_ema * _CALIBRATION_SAFETY,
                    ),
                )
                if target < self._shared.factor:
                    self._shared.factor = max(
                        target,
                        self._shared.factor * _CALIBRATION_MAX_DOWNWARD_STEP,
                    )
            shared_factor = self._shared.factor
        if underestimated:
            self._calibration_factor = min(
                _CALIBRATION_MAX_FACTOR,
                max(
                    factor,
                    min(
                        raw_ratio * 1.05,
                        factor * _CALIBRATION_MAX_UPWARD_STEP,
                    ),
                    shared_factor,
                ),
            )
        else:
            self._calibration_factor = shared_factor

    def note_context_overflow(self) -> None:
        self._calibration_factor = min(
            _CALIBRATION_MAX_FACTOR, self._calibration_factor * 1.25
        )
        if self._shared is None:
            return
        with self._shared.lock:
            self._shared.factor = max(
                self._shared.factor, self._calibration_factor
            )
            # A real overflow invalidates downward confidence: require fresh
            # samples before lowering again, and keep the EMA from instantly
            # undoing the correction.
            self._shared.samples = 0
            self._shared.raw_ratio_ema = max(
                self._shared.raw_ratio_ema,
                self._shared.factor / _CALIBRATION_SAFETY,
            )

    def count(self, text: object) -> int:
        content = normalize_text(text)
        if not content:
            return 0
        raw_count = self._raw_count(content)
        return max(1, math.ceil(raw_count * self._calibration_factor))

    def _raw_count(self, content: str) -> int:
        """Cache uncalibrated counts so repeated fitting passes stay cheap.

        The cache stores pre-calibration values, so calibration-factor changes
        never invalidate it; keys are the message strings themselves, which
        are alive in the conversation anyway and whose hash Python caches.
        """

        if len(content) < _COUNT_CACHE_MIN_CHARS:
            return self._compute_raw_count(content)
        with self._cache_lock:
            cached = self._raw_count_cache.get(content)
            if cached is not None:
                self._raw_count_cache.move_to_end(content)
                return cached
        value = self._compute_raw_count(content)
        with self._cache_lock:
            self._raw_count_cache[content] = value
            self._raw_count_cache.move_to_end(content)
            while len(self._raw_count_cache) > _COUNT_CACHE_MAX_ENTRIES:
                self._raw_count_cache.popitem(last=False)
        return value

    def _compute_raw_count(self, content: str) -> int:
        if self._encoding is not None:
            return len(self._encoding.encode(content))
        cjk = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
        other = max(0, len(content) - cjk)
        # Unknown providers must use an upper-biased estimate. Modern CJK
        # tokenizers can consume more than one token per Han character,
        # while JSON, identifiers and punctuation are denser than prose.
        return max(1, math.ceil(cjk * 1.5 + other / 3.0))
