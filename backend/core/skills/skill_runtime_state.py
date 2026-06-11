from __future__ import annotations

from contextvars import ContextVar

_LOADED_SKILLS: ContextVar[set[str] | None] = ContextVar("loaded_skills", default=None)
_REQUIRED_SKILLS: ContextVar[set[str] | None] = ContextVar("required_skills", default=None)


def set_request_skill_state(required_skills: set[str] | None):
    """初始化当前请求的 skill 状态。"""
    token_loaded = _LOADED_SKILLS.set(set())
    token_required = _REQUIRED_SKILLS.set(set(required_skills or set()))
    return token_loaded, token_required


def reset_request_skill_state(tokens) -> None:
    """恢复当前请求的 skill 状态上下文。"""
    if not isinstance(tokens, tuple) or len(tokens) != 2:
        return
    token_loaded, token_required = tokens
    _LOADED_SKILLS.reset(token_loaded)
    _REQUIRED_SKILLS.reset(token_required)


def mark_skill_loaded(skill_name: str) -> None:
    loaded = _LOADED_SKILLS.get()
    if loaded is None:
        return
    loaded.add(skill_name)


def has_loaded_skill(skill_name: str) -> bool:
    loaded = _LOADED_SKILLS.get()
    if loaded is None:
        return False
    return skill_name in loaded


def get_missing_required_skills() -> set[str]:
    required = _REQUIRED_SKILLS.get() or set()
    loaded = _LOADED_SKILLS.get() or set()
    return required - loaded
