"""
对话文件检索 Agent 工具

当用户上传长文档时，动态注入 chat_file_search 工具到当前会话的 Agent 工具集。
使用会话级 ChatFileIndex（模式匹配 + 关键词 + BM25 + TF-IDF）进行轻量混合检索。
"""

from __future__ import annotations

from langchain_core.tools import tool

from backend.config.loggers import get_logger
from backend.services.chat_file_index import get_index_manager, prepare_search_inputs

logger = get_logger(__name__)


def create_scoped_chat_file_search_tool(thread_id: str):
    """为指定会话创建带作用域的文档检索工具

    工具绑定到 thread_id 对应的索引，不同会话互不影响。
    """
    index_manager = get_index_manager()

    @tool("chat_file_search")
    def chat_file_search(
        query: str,
        keywords: list[str] | None = None,
        top_k: int = 5,
    ) -> str:
        """搜索用户在当前对话中上传的文档内容。

        【重要】如何获得最佳检索效果：
        1. 必须提供 keywords（关键词列表）——从 query 中提取文档中可能出现的具体词汇。
           - 中文问题时：请同时提取对应的英文术语（因文档可能是英文）。
           - 例如："这篇论文讲了什么" → keywords=["Biglog", "log", "unsupervised", "pre-training"]
           - 例如："前置放大器的增益" → keywords=["前置放大器", "增益", "preamplifier", "gain"]
        2. 对“图片/图表/Figure/表格”等结构化问题，会优先做模式匹配和正则直查。
        3. keywords 未命中时自动回退到 BM25 语义检索，再回退到文档开头。
        4. 可多次调用此工具，每次用不同关键词组合探索文档不同部分。

        Args:
            query:    自然语言查询描述（任意语言）
            keywords: 从 query 中提取的精确关键词/术语列表，支持中英文混合。
                     优先做精确子串匹配，大小写不敏感。
            top_k:   返回片段数量，默认 5，最多 10
        """
        top_k = max(1, min(top_k, 10))
        index = index_manager.get(thread_id)

        if not index or index.chunk_count == 0:
            return "当前对话中没有已上传的文档，或文档尚未建立索引。"

        from backend.services.chat_file_parser import clean_document_images

        prepared_query, prepared_keywords = prepare_search_inputs(query, keywords or [])
        results = index.search(prepared_query, top_k=top_k, keywords=prepared_keywords)
        if not results:
            return (
                f"在上传的 {index.file_count} 个文档中未找到相关内容。"
                f"建议尝试：1) 换用更具体的 keywords；"
                f"2) 若文档是英文，直接提供英文关键词。"
            )

        is_fallback = all(r.score == 0.0 for r in results)
        is_kw_match = (prepared_keywords and not is_fallback
                       and any(r.score >= 0.5 for r in results))

        # 收集检索结果中涉及的文件图片 URL，注册为 [IMAGE:xxx] token
        from backend.application.agent_executors.image_url_registry import register_image_url
        from backend.api.routers.chat_files import get_file_meta
        registered_file_images: dict[str, list[str]] = {}  # file_id -> [token, ...]

        parts: list[str] = []
        for i, r in enumerate(results, 1):
            if is_fallback:
                label = "（文档开头）"
            elif is_kw_match:
                label = f"（关键词命中：{r.score:.0%}）"
            else:
                label = f"（相关度：{r.score:.2f}）"
            # 清理本地图片引用，避免模型输出无效图片语法
            clean_content = clean_document_images(r.content)

            # 注册该文件的图片 URL
            fid = getattr(r, 'file_id', '')
            if fid and fid not in registered_file_images:
                meta = get_file_meta(fid)
                if meta:
                    doc_imgs = meta.get("image_urls") or []
                    tokens = []
                    for img_info in doc_imgs[:10]:  # 限制每文件最多10张
                        tok = register_image_url(
                            img_info["url"],
                            img_info.get("caption", "图片"),
                        )
                        tokens.append(f"{tok} {img_info.get('caption', '')}")
                    registered_file_images[fid] = tokens

            img_section = ""
            fid = getattr(r, 'file_id', '')
            tokens = registered_file_images.get(fid, [])
            if tokens:
                img_section = "\n\n[文档图片引用] " + ", ".join(tokens)

            parts.append(
                f"### 片段 {i}（来自：{r.file_name}{label}）\n\n{clean_content}{img_section}"
            )

        if is_fallback:
            header = (
                f"> 未找到精确匹配，已返回文档开头内容（摘要/引言）。"
                f"如需继续探索，请提供更具体的 keywords（优先使用文档语言的词汇）。\n\n"
            )
        elif is_kw_match:
            header = (
                f"> 已按检索关键词/模式匹配命中，从 {index.file_count} 个文档中找到 {len(results)} 个片段：\n\n"
            )
        else:
            header = (
                f"> 已按混合检索完成（模式匹配 + 关键词 + BM25/TF-IDF），从 {index.file_count} 个文档中找到 {len(results)} 个相关片段：\n\n"
            )
        if prepared_query != " ".join((query or "").split()).strip() or prepared_keywords != (keywords or []):
            header += (
                f"> 检索改写：query=`{prepared_query}`"
                + (f"，keywords={prepared_keywords[:8]}" if prepared_keywords else "")
                + "\n\n"
            )
        return header + "\n\n---\n\n".join(parts)

    return chat_file_search


def has_thread_file_index(thread_id: str) -> bool:
    """检查指定会话是否有已建立索引的长文档"""
    return get_index_manager().has_files(thread_id)
