"""
集成模块初始化
"""

from backend.integrations.llm import LLMClient, get_llm_client
from backend.integrations.vlm import VLMClient, get_vlm_client

__all__ = ["VLMClient", "get_vlm_client", "LLMClient", "get_llm_client"]
