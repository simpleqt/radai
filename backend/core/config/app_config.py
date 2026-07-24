"""Lightweight app config loader used by backend.core runtime."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Self

from backend.core.config.model_config import ModelConfig

load_dotenv()
logger = logging.getLogger(__name__)


def _default_config_candidates() -> tuple[Path, ...]:
    backend_dir = Path(__file__).resolve().parents[2]
    repo_root = backend_dir.parent
    return (backend_dir / 'config.yaml', repo_root / 'config.yaml')


class AppConfig(BaseModel):
    """Subset of application config used by backend.core runtime paths."""

    log_level: str = Field(default='info')
    models: list[ModelConfig] = Field(default_factory=list)
    #: RAG 用：与 ``models`` 中某项的 ``name`` 一致，对关键词候选做 LLM 精炼；不配置则不走 LLM
    keyword_extraction_chat_model: str | None = Field(default=None)
    subagents: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra='allow', frozen=False)

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path:
        if config_path:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"Config file specified by param `config_path` not found at {path}")
            return path
        env_path = os.getenv('BACKEND_CONFIG_PATH')
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise FileNotFoundError(
                    f"Config file specified by environment variable `BACKEND_CONFIG_PATH` not found at {path}"
                )
            return path
        for path in _default_config_candidates():
            if path.exists():
                return path
        raise FileNotFoundError('`config.yaml` file not found at the default backend or repository root locations')

    @classmethod
    def from_file(cls, config_path: str | None = None) -> Self:
        resolved_path = cls.resolve_config_path(config_path)
        with open(resolved_path, encoding='utf-8') as f:
            config_data = yaml.safe_load(f) or {}
        cls._check_config_version(config_data, resolved_path)
        config_data = cls.resolve_env_variables(config_data)
        return cls.model_validate(config_data)

    @classmethod
    def _check_config_version(cls, config_data: dict[str, Any], config_path: Path) -> None:
        try:
            user_version = int(config_data.get('config_version', 0))
        except (TypeError, ValueError):
            user_version = 0
        example_path = None
        search_dir = config_path.parent
        for _ in range(5):
            candidate = search_dir / 'config.example.yaml'
            if candidate.exists():
                example_path = candidate
                break
            parent = search_dir.parent
            if parent == search_dir:
                break
            search_dir = parent
        if example_path is None:
            return
        try:
            with open(example_path, encoding='utf-8') as f:
                example_data = yaml.safe_load(f)
            example_version = int((example_data or {}).get('config_version', 0))
        except Exception:
            return
        if user_version < example_version:
            logger.warning(
                'Your config.yaml (version %d) is outdated — the latest version is %d. Run `make config-upgrade` to merge new fields into your config.',
                user_version,
                example_version,
            )

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        if isinstance(config, str):
            if config.startswith('$'):
                env_value = os.getenv(config[1:])
                if env_value is None:
                    raise ValueError(f"Environment variable {config[1:]} not found for config value {config}")
                return env_value
            return config
        if isinstance(config, dict):
            return {k: cls.resolve_env_variables(v) for k, v in config.items()}
        if isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        return config

    def get_model_config(self, name: str) -> ModelConfig | None:
        return next((model for model in self.models if model.name == name), None)


_app_config: AppConfig | None = None
_app_config_path: Path | None = None
_app_config_mtime: float | None = None
_app_config_is_custom = False
_app_config_next_mtime_check = 0.0
_app_config_lock = threading.RLock()
_current_app_config: ContextVar[AppConfig | None] = ContextVar('core_current_app_config', default=None)
_current_app_config_stack: ContextVar[tuple[AppConfig | None, ...]] = ContextVar('core_current_app_config_stack', default=())


def _get_config_mtime(config_path: Path) -> float | None:
    try:
        return config_path.stat().st_mtime
    except OSError:
        return None


def _load_and_cache_app_config(config_path: str | None = None) -> AppConfig:
    global _app_config, _app_config_path, _app_config_mtime
    global _app_config_is_custom, _app_config_next_mtime_check
    resolved_path = AppConfig.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _app_config_path = resolved_path
    _app_config_mtime = _get_config_mtime(resolved_path)
    _app_config_is_custom = False
    _app_config_next_mtime_check = time.monotonic() + max(
        0.1, float(os.getenv("APP_CONFIG_MTIME_CHECK_INTERVAL_SECONDS", "1.0"))
    )
    return _app_config


def get_app_config() -> AppConfig:
    global _app_config, _app_config_path, _app_config_mtime
    global _app_config_next_mtime_check
    runtime_override = _current_app_config.get()
    if runtime_override is not None:
        return runtime_override
    if _app_config is not None and _app_config_is_custom:
        return _app_config
    now = time.monotonic()
    if _app_config is not None and now < _app_config_next_mtime_check:
        return _app_config
    with _app_config_lock:
        now = time.monotonic()
        if _app_config is not None and now < _app_config_next_mtime_check:
            return _app_config
        resolved_path = AppConfig.resolve_config_path()
        current_mtime = _get_config_mtime(resolved_path)
        should_reload = (
            _app_config is None
            or _app_config_path != resolved_path
            or _app_config_mtime != current_mtime
        )
        if should_reload:
            if (
                _app_config_path == resolved_path
                and _app_config_mtime is not None
                and current_mtime is not None
                and _app_config_mtime != current_mtime
            ):
                logger.info(
                    'Config file has been modified (mtime: %s -> %s), reloading AppConfig',
                    _app_config_mtime,
                    current_mtime,
                )
            return _load_and_cache_app_config(str(resolved_path))
        _app_config_next_mtime_check = now + max(
            0.1,
            float(os.getenv("APP_CONFIG_MTIME_CHECK_INTERVAL_SECONDS", "1.0")),
        )
        return _app_config


def reload_app_config(config_path: str | None = None) -> AppConfig:
    return _load_and_cache_app_config(config_path)


def reset_app_config() -> None:
    global _app_config, _app_config_path, _app_config_mtime
    global _app_config_is_custom, _app_config_next_mtime_check
    _app_config = None
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = False
    _app_config_next_mtime_check = 0.0


def set_app_config(config: AppConfig) -> None:
    global _app_config, _app_config_path, _app_config_mtime
    global _app_config_is_custom, _app_config_next_mtime_check
    _app_config = config
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = True
    _app_config_next_mtime_check = float("inf")


def peek_current_app_config() -> AppConfig | None:
    return _current_app_config.get()


def push_current_app_config(config: AppConfig) -> None:
    stack = _current_app_config_stack.get()
    _current_app_config_stack.set(stack + (_current_app_config.get(),))
    _current_app_config.set(config)


def pop_current_app_config() -> None:
    stack = _current_app_config_stack.get()
    if not stack:
        _current_app_config.set(None)
        return
    previous = stack[-1]
    _current_app_config_stack.set(stack[:-1])
    _current_app_config.set(previous)
