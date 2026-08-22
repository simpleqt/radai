"""Shared HanLP tokenization for lexical retrieval."""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Sequence

from backend.config.loggers import get_logger

logger = get_logger(__name__)

DEFAULT_HANLP_MODEL = (
    "https://download.hanlp.com/tok/extra/"
    "msra_crf_electra_base_20220507_113936.zip"
)
MAX_TOKEN_LENGTH = 48

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CJK_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_OPAQUE_IDENTIFIER_RE = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9a-f]{24,})",
    re.IGNORECASE,
)
_FALLBACK_WORD_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:[-_'][A-Za-z0-9]+)*"
)
_NUMBER_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    r"(?:[eE][+-]?\d+)?"
    r"(?:\s*(?:"
    r"mg/l|g/l|ug/l|μg/l|m/d|m/s|km/h|kg/m(?:3|³)|g/cm(?:3|³)|"
    r"kev|mev|gev|tev|ev|ns|us|μs|ms|s|mm|cm|km|m|"
    r"kpa|mpa|gpa|pa|khz|mhz|ghz|hz|mv|kv|v|ma|ka|a|mw|kw|w|"
    r"mol/l|mmol/l|%|‰|℃|°c|"
    r"毫克/升|克/升|微克/升|米/天|米/秒|千米/时|"
    r"纳秒|微秒|毫秒|秒|毫米|厘米|千米|米|摄氏度"
    r"))?"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

_MODEL_LOCK = threading.RLock()

# HanLP 推理在 _MODEL_LOCK 下串行。所有 off-loop 分词统一走这个专属单线程 executor，
# 而不是 asyncio 默认线程池：等锁的线程会长时间占住池内 worker，把同池的 Milvus 微批
# flush 挤到队列超时，而该超时被上层吞成空结果——向量通道会在高负载下静默消失。
# 单 worker 与 _MODEL_LOCK 的串行语义一致，排队因此发生在本 executor 而非共享池。
_TOKENIZE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="hanlp-tokenize"
)


async def run_tokenization(func, *args):
    """Run a tokenization-bound callable on the dedicated tokenizer executor."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_TOKENIZE_EXECUTOR, func, *args)


def get_hanlp_model_ref() -> str:
    return os.getenv("RAG_HANLP_MODEL", DEFAULT_HANLP_MODEL).strip()


def get_hanlp_tokenizer_metadata(*, strict: bool) -> dict[str, object]:
    return {
        "engine": "hanlp",
        "model": get_hanlp_model_ref(),
        "strict": strict,
        "preserved_tokens": ["number_unit", "cjk_bigram"],
    }


def is_cjk_token(token: str) -> bool:
    return bool(_CJK_TOKEN_RE.fullmatch(str(token or "")))


def is_number_unit_token(token: str) -> bool:
    return bool(_NUMBER_UNIT_RE.fullmatch(str(token or "")))


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available() and torch.cuda.device_count() > 0)
    except Exception:
        return False


def _hanlp_device() -> int | list[int]:
    raw = os.getenv("RAG_HANLP_DEVICE", "auto").strip().lower()
    if raw in {"", "auto"}:
        return 0 if _cuda_available() else -1
    if "," in raw:
        try:
            return [int(value.strip()) for value in raw.split(",") if value.strip()]
        except ValueError:
            return -1
    try:
        return int(raw)
    except ValueError:
        return -1


def _hanlp_batch_size() -> int:
    try:
        return max(1, int(os.getenv("RAG_HANLP_BATCH_SIZE", "16") or "16"))
    except (TypeError, ValueError):
        return 16


def _hanlp_num_threads() -> int:
    """Bound HanLP/OpenMP worker threads to avoid thread exhaustion at fan-out."""
    try:
        return max(1, int(os.getenv("RAG_HANLP_NUM_THREADS", "4") or "4"))
    except (TypeError, ValueError):
        return 4


def _configure_hanlp_threading() -> None:
    """Set process-wide numerical/HanLP thread bounds before model load."""
    threads = _hanlp_num_threads()
    for name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[name] = str(threads)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        import torch

        torch.set_num_threads(threads)
    except Exception:
        pass


# 成功的模型进程级缓存;失败不永久缓存(R4-LM-11):启动瞬间的一次性故障
# (模型文件锁、GPU OOM、下载超时)若被 lru_cache 负缓存,整个进程的分词会
# 终身退化到正则+bigram,BM25 词元与既有 HanLP 索引不匹配、召回质量静默下降
# 直到重启。失败后按冷却窗口重试。
_HANLP_MODEL: Any | None = None
_HANLP_MODEL_LOADED = False
_HANLP_NEXT_RETRY_AT = 0.0
_HANLP_LOAD_LOCK = threading.Lock()
_HANLP_RETRY_COOLDOWN_SECONDS = float(
    os.getenv("RAG_HANLP_RETRY_COOLDOWN_SECONDS", "300") or 300
)


def _load_hanlp_model() -> Any | None:
    global _HANLP_MODEL, _HANLP_MODEL_LOADED, _HANLP_NEXT_RETRY_AT
    if _HANLP_MODEL_LOADED:
        return _HANLP_MODEL
    with _HANLP_LOAD_LOCK:
        if _HANLP_MODEL_LOADED:
            return _HANLP_MODEL
        if time.monotonic() < _HANLP_NEXT_RETRY_AT:
            return None
        try:
            _configure_hanlp_threading()
            import hanlp

            model_ref = get_hanlp_model_ref()
            device = _hanlp_device()
            logger.info("[分词] 加载 HanLP 模型: %s, device=%s", model_ref, device)
            model = hanlp.load(model_ref, devices=device)
            logger.info("[分词] HanLP 模型加载完成: device=%s", device)
            _HANLP_MODEL = model
            _HANLP_MODEL_LOADED = True
            return model
        except Exception:
            _HANLP_NEXT_RETRY_AT = time.monotonic() + _HANLP_RETRY_COOLDOWN_SECONDS
            logger.exception(
                "[分词] HanLP 模型加载失败,%s 秒后允许重试;期间使用正则和"
                "中文 bigram 兜底",
                int(_HANLP_RETRY_COOLDOWN_SECONDS),
            )
            return None


def _mask_opaque_identifiers(text: str) -> str:
    return _OPAQUE_IDENTIFIER_RE.sub(lambda match: " " * len(match.group()), text)


def _normalize_token(value: object) -> str | None:
    token = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not token:
        return None
    if _NUMBER_UNIT_RE.fullmatch(token):
        return " ".join(token.split())[:MAX_TOKEN_LENGTH]
    while token and unicodedata.category(token[0])[0] in {"P", "Z"}:
        token = token[1:]
    while token and unicodedata.category(token[-1])[0] in {"P", "Z"}:
        token = token[:-1]
    token = token.strip()
    if not token or _OPAQUE_IDENTIFIER_RE.fullmatch(token):
        return None
    if not any(char.isalnum() for char in token):
        return None
    if len(token) == 1 and not token.isdigit():
        return None
    return token[:MAX_TOKEN_LENGTH]


def _normalized_tokens(values: Iterable[object]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        token = _normalize_token(value)
        if token:
            tokens.append(token)
    return tokens


def _number_unit_tokens(text: str) -> list[str]:
    return _normalized_tokens(
        " ".join(match.group().split()) for match in _NUMBER_UNIT_RE.finditer(text)
    )


def _cjk_bigram_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _CJK_RE.finditer(text):
        span = match.group()
        tokens.extend(span[index : index + 2] for index in range(len(span) - 1))
    return tokens


def _fallback_tokens(text: str) -> list[str]:
    return _normalized_tokens(_FALLBACK_WORD_RE.findall(text))


def _merge_token_counts(*sources: Sequence[str]) -> list[str]:
    """Merge overlapping token sources without double-counting the same occurrence."""

    counts: Counter[str] = Counter()
    order: list[str] = []
    seen: set[str] = set()
    for source in sources:
        source_counts = Counter(source)
        for token in source:
            if token not in seen:
                seen.add(token)
                order.append(token)
        counts |= source_counts

    merged: list[str] = []
    for token in order:
        merged.extend([token] * counts[token])
    return merged


def _coerce_batch_output(output: object, expected: int) -> list[list[object]]:
    if not isinstance(output, (list, tuple)):
        raise TypeError("HanLP tokenizer returned a non-sequence batch")
    if expected == 1 and (not output or isinstance(output[0], str)):
        return [list(output)]
    if len(output) != expected:
        raise ValueError(
            f"HanLP tokenizer returned {len(output)} results for {expected} inputs"
        )
    return [list(item) for item in output]


def _hanlp_tokens_for_texts(
    texts: Sequence[str],
    *,
    require_hanlp: bool = False,
) -> list[list[str]]:
    results: list[list[str]] = [[] for _ in texts]
    active: list[tuple[int, str]] = []
    for index, text in enumerate(texts):
        masked = _mask_opaque_identifiers(text)
        if masked.strip():
            active.append((index, masked))
    if not active:
        return results

    with _MODEL_LOCK:
        model = _load_hanlp_model()
    if model is None:
        if require_hanlp:
            raise RuntimeError("HanLP model is required but could not be loaded")
        return results

    batch_size = _hanlp_batch_size()
    for offset in range(0, len(active), batch_size):
        batch = active[offset : offset + batch_size]
        try:
            with _MODEL_LOCK:
                raw_output = model([text for _, text in batch])
            token_lists = _coerce_batch_output(raw_output, len(batch))
        except Exception as exc:
            if require_hanlp:
                raise RuntimeError(
                    f"HanLP inference failed for batch starting at offset {offset}"
                ) from exc
            logger.exception("[分词] HanLP 批量推理失败，本批次使用兜底分词")
            continue
        for (index, _), raw_tokens in zip(batch, token_lists, strict=True):
            results[index] = _normalized_tokens(raw_tokens)
    return results


def tokenize_fallback_texts(texts: Sequence[str]) -> list[list[str]]:
    """Tokenize without loading HanLP, for legacy cache migration."""

    results: list[list[str]] = []
    for value in texts:
        masked = _mask_opaque_identifiers(str(value or ""))
        results.append(
            _merge_token_counts(
                _fallback_tokens(masked),
                _number_unit_tokens(masked),
                _cjk_bigram_tokens(masked),
            )
        )
    return results


def tokenize_mixed_texts(
    texts: Sequence[str],
    *,
    require_hanlp: bool = False,
) -> list[list[str]]:
    """Tokenize mixed-language text with HanLP and retrieval-oriented fallbacks."""

    values = [str(text or "") for text in texts]
    hanlp_tokens = _hanlp_tokens_for_texts(
        values,
        require_hanlp=require_hanlp,
    )
    results: list[list[str]] = []
    for text, model_tokens in zip(values, hanlp_tokens, strict=True):
        masked = _mask_opaque_identifiers(text)
        results.append(
            _merge_token_counts(
                model_tokens or _fallback_tokens(masked),
                _number_unit_tokens(masked),
                _cjk_bigram_tokens(masked),
            )
        )
    return results


def tokenize_mixed_text(
    text: str,
    *,
    require_hanlp: bool = False,
) -> list[str]:
    return tokenize_mixed_texts(
        [text],
        require_hanlp=require_hanlp,
    )[0]


def prewarm_hanlp_tokenizer() -> list[str]:
    """Load HanLP and run one inference before the API accepts requests."""
    return tokenize_mixed_text(
        "知识库检索服务分词模型启动预热",
        require_hanlp=True,
    )
