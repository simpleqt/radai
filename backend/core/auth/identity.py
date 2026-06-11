import os
from langchain_core.runnables.config import RunnableConfig

from backend.core.agents.thread_key import parse_internal_thread_key

def get_current_identity(config: RunnableConfig) -> tuple[str | None, str]:
    """
    从 config['configurable']['thread_id'] 解析出 (user_id, external_thread_id)
    
    注意：parse_internal_thread_key() 返回 tuple[str | None, str]，
    user_id 可能为 None（当 thread_id 不符合 u_{uid}__t_{tid} 格式时）。
    调用方必须使用 require_user_id() 校验。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "")
    return parse_internal_thread_key(thread_id)

def require_user_id(config: RunnableConfig) -> str:
    """
    从 config 中解析 user_id，若为 None 则抛出 PermissionError
    """
    user_id, thread_id = get_current_identity(config)
    if user_id is None:
        raise PermissionError(
            f"无法从 thread_id 解析出 user_id，拒绝操作: {thread_id}"
        )
    return user_id
