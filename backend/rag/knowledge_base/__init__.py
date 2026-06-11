"""
知识库管理模块

提供多知识库的创建、切换、删除等生命周期管理
"""

from backend.rag.knowledge_base.manager import KnowledgeBaseManager, get_kb_manager

__all__ = ["KnowledgeBaseManager", "get_kb_manager"]
