"""Excel 引擎连接域:DuckDB 连接 LRU 缓存与查询锁、单/多文件与命名表加载、租借与缓存失效。(拆自 excel_engine.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.services.minio_paths import parse_minio_source
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
import duckdb
import hashlib
import os
import pandas as pd
import threading
import time
from backend.application.tools.providers.excel_engine_safety import (
    _file_hash,
    _new_safe_connection,
    _sanitize_table_name,
    is_allowed_local_table_path,
)

logger = get_logger(__name__)


_cache: "OrderedDict[str, duckdb.DuckDBPyConnection]" = OrderedDict()


_multi_table_maps: dict[str, dict[str, list[str]]] = {}  # cache_key → table_map


_connection_query_locks: dict[int, threading.RLock] = {}


_cache_sources: dict[str, set[str]] = {}


_cache_last_used: dict[str, float] = {}


_cache_lock = threading.RLock()


# Per-cache-key single-flight build locks: file loading (pd.read_excel can
# take seconds) must not run under the global cache lock, or every other
# Excel lookup in the process serializes behind one large load.
_load_locks: dict[str, threading.Lock] = {}


_CACHE_MAX_ENTRIES = max(1, int(os.getenv("EXCEL_ENGINE_CACHE_MAX_ENTRIES", "32")))


_CACHE_TTL_SECONDS = max(60.0, float(os.getenv("EXCEL_ENGINE_CACHE_TTL_SECONDS", "900")))


def _max_file_bytes() -> int:
    """Byte ceiling for a single table load (guards process memory/disk)."""

    try:
        return max(
            1_000_000,
            int(os.getenv("EXCEL_ENGINE_MAX_FILE_BYTES", str(200 * 1024 * 1024))),
        )
    except (TypeError, ValueError):
        return 200 * 1024 * 1024


def _enforce_file_size_limit(file_path: str) -> None:
    """Reject an oversized table before pandas loads it fully into memory."""

    try:
        size = os.path.getsize(file_path)
    except OSError:
        return
    limit = _max_file_bytes()
    if size > limit:
        raise ValueError(
            f"表格文件过大（{size} 字节，上限 {limit} 字节）；"
            "请拆分或先筛选后再分析。"
        )


def _close_cache_entry_locked(cache_key: str) -> bool:
    """Close an idle entry without waiting while the global cache lock is held."""

    conn = _cache.get(cache_key)
    if conn is not None:
        query_lock = _connection_query_locks.get(id(conn))
        if query_lock is not None and not query_lock.acquire(blocking=False):
            return False
        _cache.pop(cache_key, None)
        try:
            conn.close()
        except Exception:
            logger.debug("[ExcelEngine] 关闭缓存连接失败", exc_info=True)
        finally:
            if query_lock is not None:
                query_lock.release()
            _connection_query_locks.pop(id(conn), None)
    _multi_table_maps.pop(cache_key, None)
    _cache_sources.pop(cache_key, None)
    _cache_last_used.pop(cache_key, None)
    _load_locks.pop(cache_key, None)
    return True


def _prune_cache_locked() -> None:
    now = time.monotonic()
    expired = [
        key
        for key, last_used in _cache_last_used.items()
        if now - last_used > _CACHE_TTL_SECONDS
    ]
    for key in expired:
        _close_cache_entry_locked(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        for oldest_key in list(_cache):
            if _close_cache_entry_locked(oldest_key):
                break
        else:
            # All entries are leased. A later lookup will retry pruning.
            break


def _remember_cache_entry_locked(cache_key: str, conn, source_paths: set[str]) -> None:
    _cache[cache_key] = conn
    _cache.move_to_end(cache_key)
    _cache_sources[cache_key] = set(source_paths)
    _cache_last_used[cache_key] = time.monotonic()
    _prune_cache_locked()


def _touch_cache_entry_locked(cache_key: str) -> None:
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        _cache_last_used[cache_key] = time.monotonic()


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """清洗 DataFrame：处理 Excel 中常见的数据质量问题。

    主要处理：
    1. 非标准空白字符（\xa0 不间断空格、\u3000 全角空格等）
       → 统一替换为普通空格后 strip()
    2. 空字符串统一转为 NaN
    3. 字符串列数值推断（无损模式）：
       - 仅当全部非空值都能转为数值时才转换整列
       - 混合文本/数值列保持原值，避免静默把合法文本改成 NaN
    """
    _WHITESPACE = str.maketrans({
        "\xa0": " ",   # non-breaking space（最常见原因）
        "\u3000": " ",  # 全角空格
        "\u200b": "",  # zero-width space
        "\u200c": "",  # zero-width non-joiner
        "\u200d": "",  # zero-width joiner
        "\ufeff": "",  # BOM
        "\t": " ",     # 制表符
    })

    for col in df.columns:
        if df[col].dtype != object:
            continue

        # Step 1: 清洗非标准空白
        cleaned = df[col].apply(
            lambda x: x.translate(_WHITESPACE).strip() if isinstance(x, str) else x
        )

        # Step 2: 空字符串 → NaN
        cleaned = cleaned.replace("", pd.NA)

        # Step 3: 尝试数值推断
        try:
            numeric = pd.to_numeric(cleaned, errors="coerce")
            non_null_mask = cleaned.notna()
            if not non_null_mask.any():
                df[col] = cleaned
                continue

            all_numeric = bool(numeric[non_null_mask].notna().all())

            if all_numeric:
                df[col] = numeric
            else:
                df[col] = cleaned
        except Exception:
            df[col] = cleaned

    return df


def _load_file_to_duckdb(file_path: str) -> duckdb.DuckDBPyConnection:
    """将 Excel/CSV 文件加载到新的 DuckDB 内存连接"""
    # Covers both a local table path and a just-downloaded MinIO object; a
    # model-selected huge file otherwise loads fully into pandas and can OOM
    # the shared Agent API process.
    _enforce_file_size_limit(file_path)
    conn = _new_safe_connection()
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".csv":
        df = pd.read_csv(file_path)
        df.columns = [str(c).strip() for c in df.columns]
        df = _clean_dataframe(df)
        conn.register("data", df)
        logger.info("[ExcelEngine] CSV 已加载: %s, rows=%d", file_path, len(df))
    else:
        # Excel：逐 sheet 加载
        try:
            sheets = pd.read_excel(file_path, sheet_name=None)
        except Exception as e:
            raise ValueError(f"无法读取 Excel 文件 {file_path}: {e}") from e

        for sheet_name, df in sheets.items():
            table_name = _sanitize_table_name(sheet_name)
            # 标准化列名：去除首尾空格和非标准空白
            df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
            # 清洗数据值
            df = _clean_dataframe(df)
            conn.register(table_name, df)
            # 同时注册原始名（如果不同）
            if table_name != sheet_name:
                try:
                    conn.register(sheet_name, df)
                except Exception:
                    pass
            logger.info(
                "[ExcelEngine] Sheet 已加载: '%s' -> table '%s', rows=%d, cols=%d",
                sheet_name, table_name, len(df), len(df.columns),
            )

    return conn


def _resolve_local_path(
    file_path: str,
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> str:
    """将 MinIO object_name 下载到本地缓存目录，返回本地路径；本地路径直接返回"""
    # 已是本地文件
    if Path(file_path).exists():
        if not is_allowed_local_table_path(
            file_path,
            authorized_roots=authorized_roots,
        ):
            raise PermissionError("本地表格路径不在允许的上传/数据集目录内")
        return str(Path(file_path).expanduser().resolve())

    # 视为 MinIO object_name，下载到本地缓存
    try:
        from backend.storage import get_user_minio_bucket_name

        bucket_from_source, object_name = parse_minio_source(file_path)
        if not object_name:
            raise FileNotFoundError("MinIO object_name 为空")

        cache_dir = Path(__file__).resolve().parents[2] / "data" / "dataset_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        local_fname = Path(object_name).name or "table_file"
        cache_key = hashlib.md5(f"{bucket_from_source or ''}/{object_name}".encode()).hexdigest()[:12]
        local_path = cache_dir / f"{cache_key}_{local_fname}"

        if local_path.exists():
            return str(local_path)

        from backend.storage import _get_minio_client

        client = _get_minio_client()
        bucket = bucket_from_source or get_user_minio_bucket_name()
        # Pre-check the remote size so an oversized object is not written to
        # the cache disk before the load-time ceiling rejects it. Best-effort:
        # if stat is unavailable the load-time check still guards memory.
        try:
            stat = client.stat_object(bucket, object_name)
            remote_size = int(getattr(stat, "size", 0) or 0)
            if remote_size and remote_size > _max_file_bytes():
                raise ValueError(
                    f"表格文件过大（{remote_size} 字节，上限 {_max_file_bytes()} "
                    "字节）；请拆分或先筛选后再分析。"
                )
        except ValueError:
            raise
        except Exception:
            logger.debug("[ExcelEngine] MinIO stat 预检跳过", exc_info=True)
        # 下载发生在按 cache_key 的单飞构建锁之前(cache_key 需要本地文件哈希),
        # 并发冷查询会同时下载同一对象:直接写最终路径会交错写坏缓存文件。
        # 下载到进程+线程唯一的临时名,os.replace 原子落位;输家只是覆盖同内容。
        tmp_path = local_path.with_name(
            f"{local_path.name}.{os.getpid()}-{threading.get_ident()}.part"
        )
        try:
            client.fget_object(bucket, object_name, str(tmp_path))
            os.replace(tmp_path, local_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        logger.info("[ExcelEngine] MinIO 文件已下载到本地缓存: bucket=%s, object=%s, local=%s", bucket, object_name, local_path)
        return str(local_path)
    except Exception as e:
        logger.warning("[ExcelEngine] MinIO 下载失败: path=%s, error=%s", file_path, e)
        raise FileNotFoundError(f"文件不存在且 MinIO 下载失败: {file_path}") from e


def _build_lock_for(cache_key: str) -> threading.Lock:
    with _cache_lock:
        return _load_locks.setdefault(cache_key, threading.Lock())


def get_connection(file_path: str) -> duckdb.DuckDBPyConnection:
    """获取（或创建）文件对应的 DuckDB 连接（线程安全、哈希缓存）

    file_path 可以是：
    - 本地绝对路径
    - MinIO object_name（自动下载到本地缓存再加载）

    加载在按 cache_key 单飞的构建锁内、全局缓存锁外进行,大文件加载
    不再阻塞其他文件的缓存查找。
    """
    local_path = _resolve_local_path(file_path)
    cache_key = _file_hash(local_path)
    with _cache_lock:
        _prune_cache_locked()
        if cache_key in _cache:
            _touch_cache_entry_locked(cache_key)
            return _cache[cache_key]
    with _build_lock_for(cache_key):
        with _cache_lock:
            if cache_key in _cache:
                _touch_cache_entry_locked(cache_key)
                return _cache[cache_key]
        logger.info("[ExcelEngine] 创建新 DuckDB 连接: %s", local_path)
        conn = _load_file_to_duckdb(local_path)
        with _cache_lock:
            if cache_key in _cache:
                # 构建锁在条目关闭时被重建,极端情况下两个线程各自加载:
                # 保留已缓存连接,弃置本次加载,避免覆盖导致连接泄漏。
                try:
                    conn.close()
                except Exception:
                    logger.debug("[ExcelEngine] 关闭竞态加载连接失败", exc_info=True)
                _touch_cache_entry_locked(cache_key)
                return _cache[cache_key]
            _remember_cache_entry_locked(cache_key, conn, {local_path})
            return _cache[cache_key]


def _multi_cache_key(file_paths: list[str], prefixes: list[str]) -> str:
    """为多文件联合连接生成唯一缓存键"""
    parts = [_file_hash(fp) + ":" + pfx for fp, pfx in zip(file_paths, prefixes)]
    combined = "|".join(sorted(parts))
    return "multi_" + hashlib.md5(combined.encode()).hexdigest()


def _load_multi_files_to_duckdb(
    file_paths: list[str],
    prefixes: list[str],
) -> tuple[duckdb.DuckDBPyConnection, dict[str, list[str]]]:
    """将多个 Excel/CSV 文件加载到同一 DuckDB 连接。

    表命名策略：
    1. 每个 sheet 必注册带前缀名：{prefix}__{safe_sheet_name}
    2. 若同名 sheet 只出现在一个文件中，额外注册不带前缀的短名（方便 SQL）

    Returns:
        (conn, table_map)  其中 table_map = {prefix: [table_names_in_this_file]}
    """
    conn = _new_safe_connection()
    # 收集所有 sheet 名（用于冲突检测）
    all_sheet_names: list[tuple[str, str, str, pd.DataFrame]] = []  # (prefix, sheet_name, safe_name, df)

    for file_path, prefix in zip(file_paths, prefixes):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".csv":
            df = pd.read_csv(file_path)
            df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
            df = _clean_dataframe(df)
            all_sheet_names.append((prefix, "data", _sanitize_table_name("data"), df))
        else:
            try:
                sheets = pd.read_excel(file_path, sheet_name=None)
            except Exception as e:
                raise ValueError(f"无法读取 Excel 文件 {file_path}: {e}") from e
            for sheet_name, df in sheets.items():
                df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
                df = _clean_dataframe(df)
                all_sheet_names.append((prefix, sheet_name, _sanitize_table_name(sheet_name), df))

    # 统计 safe_name 出现次数（跨文件冲突检测）
    from collections import Counter
    name_count = Counter(safe for _, _, safe, _ in all_sheet_names)

    table_map: dict[str, list[str]] = {p: [] for p in prefixes}

    for prefix, sheet_name, safe_name, df in all_sheet_names:
        # 带前缀的完整表名（必定唯一）
        full_name = f"{prefix}__{safe_name}"
        conn.register(full_name, df)
        table_map[prefix].append(full_name)

        # 若短名无冲突，额外注册不带前缀的别名
        if name_count[safe_name] == 1:
            try:
                conn.register(safe_name, df)
                # sheet 名与 safe_name 不同时也注册原始名
                if safe_name != sheet_name:
                    conn.register(sheet_name, df)
            except Exception:
                pass

        logger.info(
            "[ExcelEngine] Multi-Sheet 已加载: '%s' -> '%s' (alias: %s), rows=%d, cols=%d",
            sheet_name, full_name,
            safe_name if name_count[safe_name] == 1 else "无短名（冲突）",
            len(df), len(df.columns),
        )

    return conn, table_map


def get_multi_connection(
    file_paths: list[str],
    prefixes: list[str],
) -> tuple[duckdb.DuckDBPyConnection, dict[str, list[str]]]:
    """获取多文件联合 DuckDB 连接（线程安全、哈希缓存）。

    Args:
        file_paths: 本地文件路径列表（按顺序与 prefixes 对应）
        prefixes:   每个文件的表名前缀（通常为 dataset_code），必须唯一

    Returns:
        (conn, table_map)  其中 table_map = {prefix: [full_table_names]}
    """
    if len(file_paths) != len(prefixes):
        raise ValueError("file_paths 和 prefixes 长度必须一致")
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("prefixes 中存在重复值，每个数据集前缀必须唯一")

    local_paths = [_resolve_local_path(fp) for fp in file_paths]
    cache_key = _multi_cache_key(local_paths, prefixes)

    with _cache_lock:
        _prune_cache_locked()
        if cache_key in _cache:
            _touch_cache_entry_locked(cache_key)
            return _cache[cache_key], _multi_table_maps.get(cache_key, {})
    with _build_lock_for(cache_key):
        with _cache_lock:
            if cache_key in _cache:
                _touch_cache_entry_locked(cache_key)
                return _cache[cache_key], _multi_table_maps.get(cache_key, {})
        logger.info("[ExcelEngine] 创建多文件 DuckDB 连接: prefixes=%s", prefixes)
        conn, table_map = _load_multi_files_to_duckdb(local_paths, prefixes)
        with _cache_lock:
            if cache_key in _cache:
                try:
                    conn.close()
                except Exception:
                    logger.debug("[ExcelEngine] 关闭竞态加载连接失败", exc_info=True)
                _touch_cache_entry_locked(cache_key)
                return _cache[cache_key], _multi_table_maps.get(cache_key, {})
            _remember_cache_entry_locked(cache_key, conn, set(local_paths))
            _multi_table_maps[cache_key] = table_map
            return conn, table_map


def _named_tables_cache_key(table_paths: dict[str, str]) -> str:
    parts = [f"{name}:{_file_hash(path)}" for name, path in sorted(table_paths.items())]
    return "named_" + hashlib.md5("|".join(parts).encode()).hexdigest()


def get_named_tables_connection(
    table_paths: dict[str, str],
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> duckdb.DuckDBPyConnection:
    """Load authorized CSV/Excel files as explicitly named DuckDB tables."""
    if not table_paths:
        raise ValueError("没有可查询的表格")

    local_paths = {
        name: _resolve_local_path(path, authorized_roots=authorized_roots)
        for name, path in table_paths.items()
    }
    cache_key = _named_tables_cache_key(local_paths)
    with _cache_lock:
        _prune_cache_locked()
        if cache_key in _cache:
            _touch_cache_entry_locked(cache_key)
            return _cache[cache_key]

    with _build_lock_for(cache_key):
        with _cache_lock:
            if cache_key in _cache:
                _touch_cache_entry_locked(cache_key)
                return _cache[cache_key]
        conn = _new_safe_connection()
        for raw_name, file_path in local_paths.items():
            table_name = _sanitize_table_name(raw_name)
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".csv":
                df = pd.read_csv(file_path)
                df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
                df = _clean_dataframe(df)
                conn.register(table_name, df)
            else:
                sheets = pd.read_excel(file_path, sheet_name=None)
                if len(sheets) != 1:
                    raise ValueError(
                        f"命名表 {raw_name} 必须对应 CSV 或单工作表 Excel"
                    )
                df = next(iter(sheets.values()))
                df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
                conn.register(table_name, _clean_dataframe(df))
            logger.info(
                "[ExcelEngine] 命名表已加载: %s -> %s, rows=%d",
                file_path,
                table_name,
                len(df),
            )
        with _cache_lock:
            if cache_key in _cache:
                try:
                    conn.close()
                except Exception:
                    logger.debug("[ExcelEngine] 关闭竞态加载连接失败", exc_info=True)
                _touch_cache_entry_locked(cache_key)
                return _cache[cache_key]
            _remember_cache_entry_locked(cache_key, conn, set(local_paths.values()))
            _connection_query_locks.setdefault(id(conn), threading.RLock())
            return conn


@contextmanager
def _leased_connection(loader):
    """Pin a cached connection from lookup through the end of its query.

    ``loader()`` runs outside the global cache lock: the getters serialize
    loading through per-key build locks, and holding the cache lock across a
    loader that itself takes a build lock would deadlock against a builder
    waiting for the cache lock.
    """

    while True:
        loaded = loader()
        conn = loaded[0] if isinstance(loaded, tuple) else loaded
        with _cache_lock:
            if not any(cached is conn for cached in _cache.values()):
                # 加载与租借之间被失效(TTL/invalidate):重新获取连接。
                continue
            query_lock = _connection_query_locks.setdefault(
                id(conn),
                threading.RLock(),
            )
            if query_lock.acquire(blocking=False):
                break
        query_lock.acquire()
        with _cache_lock:
            if any(cached is conn for cached in _cache.values()):
                break
        query_lock.release()
    try:
        yield loaded
    finally:
        query_lock.release()


def invalidate_cache(file_path: str) -> None:
    """清除指定文件的缓存（文件更新后调用）"""
    try:
        local_path = _resolve_local_path(file_path)
    except Exception:
        local_path = str(Path(file_path).expanduser().resolve())
    with _cache_lock:
        matching = [
            key for key, paths in _cache_sources.items()
            if local_path in paths
        ]
        closed = 0
        pending = 0
        for cache_key in matching:
            if _close_cache_entry_locked(cache_key):
                closed += 1
            else:
                # Make the busy entry immediately eligible for pruning after
                # its current lease ends.
                _cache_last_used[cache_key] = 0.0
                pending += 1
        if matching:
            logger.info(
                "[ExcelEngine] 缓存失效: %s closed=%d pending=%d",
                file_path,
                closed,
                pending,
            )
