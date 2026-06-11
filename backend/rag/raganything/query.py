"""
Query functionality for RAGAnything

Contains all query-related methods for both text and multimodal queries
"""

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx
from lightrag import QueryParam

from .prompt import PROMPTS
from .utils import (
    encode_image_to_base64,
    get_processor_for_type,
    is_remote_image_path,
    validate_image_file,
)


def _extract_first_http_url(value: str) -> str:
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    m = re.search(r"https?://[^\s\"'<>]+", s)
    if not m:
        return ""
    return m.group(0).rstrip(").,]}>\"'")


def _redact_url_query(url: str) -> str:
    if not isinstance(url, str):
        return ""
    return url.split("?", 1)[0]


def _strip_presigned_query(url: str) -> str:
    if not isinstance(url, str):
        return ""
    if "X-Amz-" not in url and "x-amz-" not in url:
        return url
    return url.split("?", 1)[0]


def _refresh_minio_presigned_url(url: str) -> str:
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    path = parsed.path or ""
    if not path.startswith("/"):
        return ""
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return ""
    bucket_from_url = parts[0]
    object_name = "/".join(parts[1:])
    try:
        from backend.config import settings as cfg
        from backend.storage import get_presigned_url
    except Exception:
        return ""
    if bucket_from_url != getattr(cfg, "minio_bucket_name", None):
        return ""
    refreshed = get_presigned_url(object_name)
    return refreshed or ""


class QueryMixin:
    """Query Mixin"""

    def __init__(self):
        pass

    async def _adjust_rerank_params_by_length(
        self, query: str, mode: str, **kwargs
    ) -> Dict[str, Any]:
        """
        根据上下文长度动态调整 rerank 参数

        先获取检索上下文，根据长度动态调整 top_k 和 chunk_top_k 值，
        避免返回过多或过少的上下文

        Args:
            query: 查询文本
            mode: 查询模式
            **kwargs: 原始参数

        Returns:
            调整后的参数字典
        """
        default_top_k = getattr(self.config, "query_default_top_k", 60)
        default_chunk_top_k = getattr(self.config, "query_default_chunk_top_k", 15)

        if "top_k" in kwargs and "chunk_top_k" in kwargs:
            self.logger.info(
                f"[Rerank] 用户指定参数: top_k={kwargs['top_k']}, chunk_top_k={kwargs['chunk_top_k']}"
            )
            return kwargs

        test_top_k = getattr(self.config, "query_test_top_k", 30)
        test_chunk_top_k = getattr(self.config, "query_test_chunk_top_k", 10)

        test_param = QueryParam(
            mode=mode,
            only_need_prompt=True,
            top_k=test_top_k,
            chunk_top_k=test_chunk_top_k,
        )

        try:
            test_context = await self.lightrag.aquery(query, param=test_param)
            context_length = len(test_context) if test_context else 0

            self.logger.info(
                f"[Rerank] 测试检索: top_k={test_top_k}, chunk_top_k={test_chunk_top_k}, 上下文长度={context_length}"
            )

            target_length = getattr(self.config, "query_target_context_length", 12000)
            ratio = target_length / context_length if context_length > 0 else 1

            ratio_min = getattr(self.config, "query_ratio_min", 0.5)
            ratio_max = getattr(self.config, "query_ratio_max", 2.0)
            ratio = max(ratio_min, min(ratio_max, ratio))

            if mode == "local":
                base_top_k = 50
                base_chunk_top_k = 10
            elif mode == "global":
                base_top_k = 70
                base_chunk_top_k = 15
            elif mode == "hybrid":
                base_top_k = 60
                base_chunk_top_k = 12
            elif mode == "mix":
                base_top_k = 60
                base_chunk_top_k = 15
            elif mode == "naive":
                base_top_k = 40
                base_chunk_top_k = 10
            else:
                base_top_k = 60
                base_chunk_top_k = 15

            min_top_k = getattr(self.config, "query_min_top_k", 20)
            max_top_k = getattr(self.config, "query_max_top_k", 100)
            min_chunk_top_k = getattr(self.config, "query_min_chunk_top_k", 5)
            max_chunk_top_k = getattr(self.config, "query_max_chunk_top_k", 30)

            if "top_k" not in kwargs:
                kwargs["top_k"] = int(base_top_k * ratio)
                kwargs["top_k"] = max(min_top_k, min(max_top_k, kwargs["top_k"]))

            if "chunk_top_k" not in kwargs:
                kwargs["chunk_top_k"] = int(base_chunk_top_k * ratio)
                kwargs["chunk_top_k"] = max(min_chunk_top_k, min(max_chunk_top_k, kwargs["chunk_top_k"]))

            self.logger.info(
                f"[Rerank] 动态调整: 比例={ratio:.2f}, top_k={kwargs.get('top_k')}, chunk_top_k={kwargs.get('chunk_top_k')}"
            )

        except Exception as e:
            self.logger.warning(f"[Rerank] 动态调整失败，使用默认值: {e}")
            if "top_k" not in kwargs:
                kwargs["top_k"] = default_top_k
            if "chunk_top_k" not in kwargs:
                kwargs["chunk_top_k"] = default_chunk_top_k

        return kwargs

    def _generate_multimodal_cache_key(
        self, query: str, multimodal_content: List[Dict[str, Any]], mode: str, **kwargs
    ) -> str:
        """
        Generate cache key for multimodal query

        Args:
            query: Base query text
            multimodal_content: List of multimodal content
            mode: Query mode
            **kwargs: Additional parameters

        Returns:
            str: Cache key hash
        """
        # Create a normalized representation of the query parameters
        cache_data: Dict[str, Any] = {
            "query": query.strip(),
            "mode": mode,
        }

        # Normalize multimodal content for stable caching
        normalized_content = []
        if multimodal_content:
            for item in multimodal_content:
                if isinstance(item, dict):
                    normalized_item = {}
                    for key, value in item.items():
                        # For file paths, use basename to make cache more portable
                        if key in [
                            "img_path",
                            "image_path",
                            "file_path",
                        ] and isinstance(value, str):
                            normalized_item[key] = Path(value).name
                        # For large content, create a hash instead of storing directly
                        elif (
                            key in ["table_data", "table_body"]
                            and isinstance(value, str)
                            and len(value) > 200
                        ):
                            normalized_item[f"{key}_hash"] = hashlib.md5(
                                value.encode()
                            ).hexdigest()
                        else:
                            normalized_item[key] = value
                    normalized_content.append(normalized_item)
                else:
                    normalized_content.append(item)

        cache_data["multimodal_content"] = normalized_content

        # Add relevant kwargs to cache data
        relevant_kwargs = {
            k: v
            for k, v in kwargs.items()
            if k
            in [
                "stream",
                "response_type",
                "top_k",
                "max_tokens",
                "temperature",
                # "only_need_context",
                # "only_need_prompt",
            ]
        }
        cache_data.update(relevant_kwargs)

        # Generate hash from the cache data
        cache_str = json.dumps(cache_data, sort_keys=True, ensure_ascii=False)
        cache_hash = hashlib.md5(cache_str.encode()).hexdigest()

        return f"multimodal_query:{cache_hash}"

    async def aquery(
        self, query: str, mode: str = "mix", system_prompt: str | None = None, **kwargs
    ) -> str:
        """
        Pure text query - directly calls LightRAG's query functionality

        Args:
            query: Query text
            mode: Query mode ("local", "global", "hybrid", "naive", "mix", "bypass")
            system_prompt: Optional system prompt to include.
            **kwargs: Other query parameters, will be passed to QueryParam
                - vlm_enhanced: bool, default True when vision_model_func is available.
                  If True, will parse image paths in retrieved context and replace them
                  with base64 encoded images for VLM processing.

        Returns:
            str: Query result
        """
        if self.lightrag is None:
            raise ValueError(
                "No LightRAG instance available. Please process documents first or provide a pre-initialized LightRAG instance."
            )

        setattr(self, "_last_query_images", [])

        # Check if VLM enhanced query should be used
        vlm_enhanced = kwargs.pop("vlm_enhanced", None)

        # Auto-determine VLM enhanced based on availability
        if vlm_enhanced is None:
            vlm_enhanced = (
                hasattr(self, "vision_model_func")
                and self.vision_model_func is not None
            )

        # Use VLM enhanced query if enabled and available
        if (
            vlm_enhanced
            and hasattr(self, "vision_model_func")
            and self.vision_model_func
        ):
            return await self.aquery_vlm_enhanced(
                query, mode=mode, system_prompt=system_prompt, **kwargs
            )
        elif vlm_enhanced and (
            not hasattr(self, "vision_model_func") or not self.vision_model_func
        ):
            self.logger.warning(
                "VLM enhanced query requested but vision_model_func is not available, falling back to normal query"
            )

        # 动态调整 top_k 和 chunk_top_k 参数（根据上下文长度）
        kwargs = await self._adjust_rerank_params_by_length(
            query, mode, **kwargs
        )

        # Create query parameters
        query_param = QueryParam(mode=mode, **kwargs)

        self.logger.info(f"Executing text query: {query[:100]}...")
        self.logger.info(f"Query mode: {mode}")

        # 详细计时开始
        total_start_time = time.time()
        self.logger.info(f"[计时] 查询开始 | query_id={id(query)} | mode={mode}")

        # Call LightRAG's query method
        llm_start_time = time.time()
        result = await self.lightrag.aquery(
            query, param=query_param, system_prompt=system_prompt
        )
        llm_end_time = time.time()
        llm_duration = llm_end_time - llm_start_time

        total_end_time = time.time()
        total_duration = total_end_time - total_start_time

        # 输出详细计时日志
        self.logger.info(f"[计时] 查询完成 | query_id={id(query)} | mode={mode}")
        self.logger.info(f"[计时] LLM生成耗时: {llm_duration:.3f}s")
        self.logger.info(f"[计时] 总耗时: {total_duration:.3f}s")

        self.logger.info("Text query completed")
        return result

    async def aquery_with_multimodal(
        self,
        query: str,
        multimodal_content: List[Dict[str, Any]] = None,
        mode: str = "mix",
        **kwargs,
    ) -> str:
        """
        Multimodal query - combines text and multimodal content for querying

        Args:
            query: Base query text
            multimodal_content: List of multimodal content, each element contains:
                - type: Content type ("image", "table", "equation", etc.)
                - Other fields depend on type (e.g., img_path, table_data, latex, etc.)
            mode: Query mode ("local", "global", "hybrid", "naive", "mix", "bypass")
            **kwargs: Other query parameters, will be passed to QueryParam

        Returns:
            str: Query result

        Examples:
            # Pure text query
            result = await rag.query_with_multimodal("What is machine learning?")

            # Image query
            result = await rag.query_with_multimodal(
                "Analyze the content in this image",
                multimodal_content=[{
                    "type": "image",
                    "img_path": "./image.jpg"
                }]
            )

            # Table query
            result = await rag.query_with_multimodal(
                "Analyze the data trends in this table",
                multimodal_content=[{
                    "type": "table",
                    "table_data": "Name,Age\nAlice,25\nBob,30"
                }]
            )
        """
        # Ensure LightRAG is initialized
        await self._ensure_lightrag_initialized()

        self.logger.info(f"Executing multimodal query: {query[:100]}...")
        self.logger.info(f"Query mode: {mode}")

        # If no multimodal content, fallback to pure text query
        if not multimodal_content:
            self.logger.info("No multimodal content provided, executing text query")
            return await self.aquery(query, mode=mode, **kwargs)

        # Generate cache key for multimodal query
        cache_key = self._generate_multimodal_cache_key(
            query, multimodal_content, mode, **kwargs
        )

        # Check cache if available and enabled
        cached_result = None
        if (
            hasattr(self, "lightrag")
            and self.lightrag
            and hasattr(self.lightrag, "llm_response_cache")
            and self.lightrag.llm_response_cache
        ):
            if self.lightrag.llm_response_cache.global_config.get(
                "enable_llm_cache", True
            ):
                try:
                    cached_result = await self.lightrag.llm_response_cache.get_by_id(
                        cache_key
                    )
                    if cached_result and isinstance(cached_result, dict):
                        result_content = cached_result.get("return")
                        if result_content:
                            self.logger.info(
                                f"Multimodal query cache hit: {cache_key[:16]}..."
                            )
                            return result_content
                except Exception as e:
                    self.logger.debug(f"Error accessing multimodal query cache: {e}")

        # Process multimodal content to generate enhanced query text
        enhanced_query = await self._process_multimodal_query_content(
            query, multimodal_content
        )

        self.logger.info(
            f"Generated enhanced query length: {len(enhanced_query)} characters"
        )

        # Execute enhanced query
        result = await self.aquery(enhanced_query, mode=mode, **kwargs)

        # Save to cache if available and enabled
        if (
            hasattr(self, "lightrag")
            and self.lightrag
            and hasattr(self.lightrag, "llm_response_cache")
            and self.lightrag.llm_response_cache
        ):
            if self.lightrag.llm_response_cache.global_config.get(
                "enable_llm_cache", True
            ):
                try:
                    # Create cache entry for multimodal query
                    cache_entry = {
                        "return": result,
                        "cache_type": "multimodal_query",
                        "original_query": query,
                        "multimodal_content_count": len(multimodal_content),
                        "mode": mode,
                    }

                    await self.lightrag.llm_response_cache.upsert(
                        {cache_key: cache_entry}
                    )
                    self.logger.info(
                        f"Saved multimodal query result to cache: {cache_key[:16]}..."
                    )
                except Exception as e:
                    self.logger.debug(f"Error saving multimodal query to cache: {e}")

        # Ensure cache is persisted to disk
        if (
            hasattr(self, "lightrag")
            and self.lightrag
            and hasattr(self.lightrag, "llm_response_cache")
            and self.lightrag.llm_response_cache
        ):
            try:
                await self.lightrag.llm_response_cache.index_done_callback()
            except Exception as e:
                self.logger.debug(f"Error persisting multimodal query cache: {e}")

        self.logger.info("Multimodal query completed")
        return result

    async def aquery_vlm_enhanced(
        self, query: str, mode: str = "mix", system_prompt: str | None = None, **kwargs
    ) -> str:
        """
        VLM enhanced query - replaces image paths in retrieved context with base64 encoded images for VLM processing

        Args:
            query: User query
            mode: Underlying LightRAG query mode
            system_prompt: Optional system prompt to include
            **kwargs: Other query parameters

        Returns:
            str: VLM query result
        """
        # Ensure VLM is available
        if not hasattr(self, "vision_model_func") or not self.vision_model_func:
            raise ValueError(
                "VLM enhanced query requires vision_model_func. "
                "Please provide a vision model function when initializing RAGAnything."
            )

        # Ensure LightRAG is initialized
        await self._ensure_lightrag_initialized()

        self.logger.info(f"Executing VLM enhanced query: {query[:100]}...")

        # Clear previous image cache
        if hasattr(self, "_current_images_base64"):
            delattr(self, "_current_images_base64")
        if hasattr(self, "_current_images_paths"):
            delattr(self, "_current_images_paths")
        setattr(self, "_last_query_images", [])

        # 动态调整 top_k 和 chunk_top_k 参数（根据上下文长度）
        kwargs = await self._adjust_rerank_params_by_length(
            query, mode, **kwargs
        )

        # 1. Get original retrieval prompt (without generating final answer)
        vlm_max_images = kwargs.pop("vlm_max_images", None)
        # Remove vlm_enhanced from kwargs as it's not a valid QueryParam parameter
        kwargs.pop("vlm_enhanced", None)
        try:
            if isinstance(vlm_max_images, str) and vlm_max_images.strip().isdigit():
                vlm_max_images = int(vlm_max_images.strip())
            elif isinstance(vlm_max_images, (int, float)):
                vlm_max_images = int(vlm_max_images)
            else:
                vlm_max_images = None
        except Exception:
            vlm_max_images = None

        # 获取检索上下文或 prompt
        query_param = QueryParam(mode=mode, only_need_prompt=True, **kwargs)
        raw_prompt = await self.lightrag.aquery(query, param=query_param)

        self.logger.debug("Retrieved raw prompt from LightRAG")

        # 2. Extract and process image paths
        enhanced_prompt, images_found = await self._process_image_paths_for_vlm(
            raw_prompt, max_images=vlm_max_images
        )

        if not images_found:
            self.logger.info("No valid images found, falling back to normal query")
            # Fallback to normal query
            query_param = QueryParam(mode=mode, **kwargs)
            return await self.lightrag.aquery(
                query, param=query_param, system_prompt=system_prompt
            )

        self.logger.info(f"Processed {images_found} images for VLM")
        image_paths = getattr(self, "_current_images_paths", []) or []
        self._last_query_images = [
            {"marker": f"IMAGE_{i+1}", "path": p} for i, p in enumerate(image_paths)
        ]

        # 3. Build VLM message format
        messages = self._build_vlm_messages_with_images(
            enhanced_prompt, query, system_prompt
        )

        # 4. Call VLM for question answering
        result = await self._call_vlm_with_multimodal_content(messages)

        self.logger.info("VLM enhanced query completed")
        return result

    async def _process_multimodal_query_content(
        self, base_query: str, multimodal_content: List[Dict[str, Any]]
    ) -> str:
        """
        Process multimodal query content to generate enhanced query text

        Args:
            base_query: Base query text
            multimodal_content: List of multimodal content

        Returns:
            str: Enhanced query text
        """
        self.logger.info("Starting multimodal query content processing...")

        enhanced_parts = [f"User query: {base_query}"]

        for i, content in enumerate(multimodal_content):
            content_type = content.get("type", "unknown")
            self.logger.info(
                f"Processing {i+1}/{len(multimodal_content)} multimodal content: {content_type}"
            )

            try:
                # Get appropriate processor
                processor = get_processor_for_type(self.modal_processors, content_type)

                if processor:
                    # Generate content description
                    description = await self._generate_query_content_description(
                        processor, content, content_type
                    )
                    enhanced_parts.append(
                        f"\nRelated {content_type} content: {description}"
                    )
                else:
                    # If no appropriate processor, use basic description
                    basic_desc = str(content)[:200]
                    enhanced_parts.append(
                        f"\nRelated {content_type} content: {basic_desc}"
                    )

            except Exception as e:
                self.logger.error(f"Error processing multimodal content: {str(e)}")
                # Continue processing other content
                continue

        enhanced_query = "\n".join(enhanced_parts)
        enhanced_query += PROMPTS["QUERY_ENHANCEMENT_SUFFIX"]

        self.logger.info("Multimodal query content processing completed")
        return enhanced_query

    async def _generate_query_content_description(
        self, processor, content: Dict[str, Any], content_type: str
    ) -> str:
        """
        Generate content description for query

        Args:
            processor: Multimodal processor
            content: Content data
            content_type: Content type

        Returns:
            str: Content description
        """
        try:
            if content_type == "image":
                return await self._describe_image_for_query(processor, content)
            elif content_type == "table":
                return await self._describe_table_for_query(processor, content)
            elif content_type == "equation":
                return await self._describe_equation_for_query(processor, content)
            else:
                return await self._describe_generic_for_query(
                    processor, content, content_type
                )

        except Exception as e:
            self.logger.error(f"Error generating {content_type} description: {str(e)}")
            return f"{content_type} content: {str(content)[:100]}"

    async def _describe_image_for_query(
        self, processor, content: Dict[str, Any]
    ) -> str:
        """Generate image description for query"""
        image_path = content.get("img_path")
        captions = content.get("image_caption", content.get("img_caption", []))
        footnotes = content.get("image_footnote", content.get("img_footnote", []))

        if image_path and Path(image_path).exists():
            # If image exists, use vision model to generate description
            image_base64 = await processor._encode_image_to_base64(image_path)
            if image_base64:
                prompt = PROMPTS["QUERY_IMAGE_DESCRIPTION"]
                description = await processor.modal_caption_func(
                    prompt,
                    image_data=image_base64,
                    system_prompt=PROMPTS["QUERY_IMAGE_ANALYST_SYSTEM"],
                )
                return description

        # If image doesn't exist or processing failed, use existing information
        parts = []
        if image_path:
            parts.append(f"Image path: {image_path}")
        if captions:
            parts.append(f"Image captions: {', '.join(captions)}")
        if footnotes:
            parts.append(f"Image footnotes: {', '.join(footnotes)}")

        return "; ".join(parts) if parts else "Image content information incomplete"

    async def _describe_table_for_query(
        self, processor, content: Dict[str, Any]
    ) -> str:
        """Generate table description for query"""
        table_data = content.get("table_data", "")
        table_caption = content.get("table_caption", "")

        prompt = PROMPTS["QUERY_TABLE_ANALYSIS"].format(
            table_data=table_data, table_caption=table_caption
        )

        description = await processor.modal_caption_func(
            prompt, system_prompt=PROMPTS["QUERY_TABLE_ANALYST_SYSTEM"]
        )

        return description

    async def _describe_equation_for_query(
        self, processor, content: Dict[str, Any]
    ) -> str:
        """Generate equation description for query"""
        latex = content.get("latex", "")
        equation_caption = content.get("equation_caption", "")

        prompt = PROMPTS["QUERY_EQUATION_ANALYSIS"].format(
            latex=latex, equation_caption=equation_caption
        )

        description = await processor.modal_caption_func(
            prompt, system_prompt=PROMPTS["QUERY_EQUATION_ANALYST_SYSTEM"]
        )

        return description

    async def _describe_generic_for_query(
        self, processor, content: Dict[str, Any], content_type: str
    ) -> str:
        """Generate generic content description for query"""
        content_str = str(content)

        prompt = PROMPTS["QUERY_GENERIC_ANALYSIS"].format(
            content_type=content_type, content_str=content_str
        )

        description = await processor.modal_caption_func(
            prompt,
            system_prompt=PROMPTS["QUERY_GENERIC_ANALYST_SYSTEM"].format(
                content_type=content_type
            ),
        )

        return description

    async def _process_image_paths_for_vlm(
        self, prompt: str, max_images: int | None = None
    ) -> tuple[str, int]:
        """
        Process image paths in prompt, keeping original paths and adding VLM markers

        Args:
            prompt: Original prompt

        Returns:
            tuple: (processed prompt, image count)
        """
        if not isinstance(prompt, str):
            prompt = (
                json.dumps(prompt, ensure_ascii=False) if prompt is not None else ""
            )

        if max_images is None:
            max_images = getattr(self.config, "vlm_max_images", 10)

        enhanced_prompt = prompt
        images_processed = 0

        # Initialize image cache
        self._current_images_base64 = []
        self._current_images_paths = []
        self._current_images_urls = []

        # Enhanced regex pattern for matching image paths
        # Matches only the path ending with image file extensions
        image_path_pattern = r"Image Path:\s*([^\r\n]+?\.(?:jpg|jpeg|png|gif|bmp|webp|tiff|tif)(?:\?[^\r\n]*)?)"

        matches = list(re.finditer(image_path_pattern, prompt))
        self.logger.info(f"Found {len(matches)} image path matches in prompt")

        parts: list[str] = []
        last_end = 0
        for match in matches:
            parts.append(prompt[last_end : match.start()])
            last_end = match.end()

            original = match.group(0)
            image_path = match.group(1).strip()
            self.logger.debug(f"Processing image path: '{image_path}'")

            if (
                max_images is not None
                and max_images > 0
                and images_processed >= max_images
            ):
                parts.append(original)
                continue

            if not image_path or len(image_path) < 3:
                self.logger.warning(f"Invalid image path format: {image_path}")
                parts.append(original)
                continue

            self.logger.debug(f"Calling validate_image_file for: {image_path}")
            is_valid = validate_image_file(image_path)
            self.logger.debug(f"Validation result for {image_path}: {is_valid}")

            if not is_valid:
                self.logger.warning(f"Image validation failed for: {image_path}")
                parts.append(original)
                continue

            try:
                image_base64 = ""
                image_url = ""
                if is_remote_image_path(image_path):
                    image_base64 = await self._fetch_remote_image_to_base64(image_path)
                    if not image_base64:
                        parts.append(original)
                        continue
                else:
                    self.logger.debug(f"Attempting to encode image: {image_path}")
                    image_base64 = encode_image_to_base64(image_path)
                    if not image_base64:
                        self.logger.error(f"Failed to encode image: {image_path}")
                        parts.append(original)
                        continue

                images_processed += 1
                self._current_images_base64.append(image_base64)
                self._current_images_paths.append(image_path)
                self._current_images_urls.append(image_url)

                parts.append(
                    f"Image Path: {image_path}\n" f"[VLM_IMAGE_{images_processed}]"
                )
            except Exception as e:
                self.logger.error(f"Failed to process image {image_path}: {e}")
                parts.append(original)

        parts.append(prompt[last_end:])
        enhanced_prompt = "".join(parts)

        return enhanced_prompt, images_processed

    async def _fetch_remote_image_to_base64(self, image_url: str) -> str:
        timeout = getattr(self.config, "vlm_remote_image_timeout", 20.0)
        max_mb = getattr(self.config, "vlm_remote_image_max_mb", 15)
        max_bytes = max(1, max_mb) * 1024 * 1024

        raw = image_url or ""
        s = raw.strip()
        if not s:
            return ""
        low = s.lower()
        if low.startswith("data:image/"):
            comma = s.find(",")
            if comma <= 0:
                return ""
            header = s[:comma].lower()
            if ";base64" not in header:
                return ""
            payload = s[comma + 1 :].strip()
            return payload if payload else ""

        url = _extract_first_http_url(s)
        if not url:
            return ""

        preferred_url = _strip_presigned_query(url) or url
        refreshed_url = ""
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.get(preferred_url)
                resp.raise_for_status()
                data = resp.content or b""
        except Exception as e:
            status = None
            try:
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
            except Exception:
                status = None
            if status in (401, 403) and preferred_url != url:
                try:
                    async with httpx.AsyncClient(
                        timeout=timeout, follow_redirects=True
                    ) as client:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = resp.content or b""
                except Exception:
                    data = b""
            if status in (401, 403):
                refreshed_url = _refresh_minio_presigned_url(url)
                if refreshed_url and refreshed_url != url:
                    try:
                        async with httpx.AsyncClient(
                            timeout=timeout, follow_redirects=True
                        ) as client:
                            resp = await client.get(refreshed_url)
                            resp.raise_for_status()
                            data = resp.content or b""
                    except Exception:
                        data = b""
            safe_url = _redact_url_query(url)
            if status is not None:
                self.logger.warning(
                    f"Failed to fetch remote image (status={status}, url={safe_url})"
                )
            else:
                self.logger.warning(
                    f"Failed to fetch remote image ({type(e).__name__}, url={safe_url})"
                )
            if not data:
                return ""

        if not data or len(data) > max_bytes:
            return ""
        try:
            return base64.b64encode(data).decode("utf-8")
        except Exception:
            return ""

    def _build_vlm_messages_with_images(
        self, enhanced_prompt: str, user_query: str, system_prompt: str | None
    ) -> List[Dict]:
        """
        Build VLM message format, using markers to correspond images with text positions

        Args:
            enhanced_prompt: Enhanced prompt with image markers
            user_query: User query

        Returns:
            List[Dict]: VLM message format
        """
        images_base64 = getattr(self, "_current_images_base64", []) or []
        images_paths = getattr(self, "_current_images_paths", []) or []
        images_urls = getattr(self, "_current_images_urls", []) or []

        has_any_image = False
        for i, p in enumerate(images_paths):
            b = images_base64[i] if i < len(images_base64) else ""
            u = images_urls[i] if i < len(images_urls) else ""
            if b or u or is_remote_image_path(p):
                has_any_image = True
                break

        if not has_any_image:
            # Pure text mode
            return [
                {
                    "role": "user",
                    "content": f"Context:\n{enhanced_prompt}\n\nUser Question: {user_query}",
                }
            ]

        # Build multimodal content
        content_parts: List[Dict[str, Any]] = []

        # Split text at image markers and insert images
        text_parts = enhanced_prompt.split("[VLM_IMAGE_")

        for i, text_part in enumerate(text_parts):
            if i == 0:
                # First text part
                if text_part.strip():
                    content_parts.append({"type": "text", "text": text_part})
            else:
                # Find marker number and insert corresponding image
                marker_match = re.match(r"(\d+)\](.*)", text_part, re.DOTALL)
                if marker_match:
                    image_num = (
                        int(marker_match.group(1)) - 1
                    )  # Convert to 0-based index
                    remaining_text = marker_match.group(2)

                    # Insert corresponding image
                    if (
                        0
                        <= image_num
                        < max(len(images_paths), len(images_base64), len(images_urls))
                    ):
                        b64 = (
                            images_base64[image_num]
                            if image_num < len(images_base64)
                            else ""
                        )
                        url = (
                            images_urls[image_num]
                            if image_num < len(images_urls)
                            else ""
                        )
                        if not url and image_num < len(images_paths):
                            p = images_paths[image_num]
                            if is_remote_image_path(p):
                                url = p
                        if b64:
                            url = f"data:image/jpeg;base64,{b64}"
                        if url:
                            content_parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": url},
                                }
                            )

                    # Insert remaining text
                    if remaining_text.strip():
                        content_parts.append({"type": "text", "text": remaining_text})

        # Add user question
        content_parts.append(
            {
                "type": "text",
                "text": f"\n\nUser Question: {user_query}\n\nPlease answer based on the context and images provided.",
            }
        )
        base_system_prompt = (
            "You are a helpful assistant that can analyze both text and image content to provide comprehensive answers."
            " Use the provided images to support your answer."
            " When you choose to include an image in the answer, insert it directly using Markdown image syntax like: ![简短说明](URL)."
            " Do not output placeholder markers like [IMAGE_1] unless the user explicitly asks for markers."
        )

        if system_prompt:
            full_system_prompt = base_system_prompt + " " + system_prompt
        else:
            full_system_prompt = base_system_prompt

        return [
            {
                "role": "system",
                "content": full_system_prompt,
            },
            {
                "role": "user",
                "content": content_parts,
            },
        ]

    async def _call_vlm_with_multimodal_content(self, messages: List[Dict]) -> str:
        """
        Call VLM to process multimodal content

        Args:
            messages: VLM message format

        Returns:
            str: VLM response result
        """
        try:
            user_message = messages[1]
            content = user_message["content"]
            system_prompt = messages[0]["content"]

            if isinstance(content, str):
                # Pure text mode
                result = await self.vision_model_func(
                    content, system_prompt=system_prompt
                )
            else:
                # Multimodal mode - pass complete messages directly to VLM
                result = await self.vision_model_func(
                    "",  # Empty prompt since we're using messages format
                    messages=messages,
                )

            return result

        except Exception as e:
            self.logger.error(f"VLM call failed: {e}")
            raise

    # Synchronous versions of query methods
    def query(self, query: str, mode: str = "mix", **kwargs) -> str:
        """
        Synchronous version of pure text query (已废弃，请使用 aquery)

        Args:
            query: Query text
            mode: Query mode ("local", "global", "hybrid", "naive", "mix", "bypass")
            **kwargs: Other query parameters, will be passed to QueryParam
                - vlm_enhanced: bool, default True when vision_model_func is available.
                  If True, will parse image paths in retrieved context and replace them
                  with base64 encoded images for VLM processing.

        Returns:
            str: Query result
        """
        import warnings
        warnings.warn(
            "query is deprecated, use aquery instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.aquery(query, mode=mode, **kwargs))
        finally:
            loop.close()

    def query_with_multimodal(
        self,
        query: str,
        multimodal_content: List[Dict[str, Any]] = None,
        mode: str = "mix",
        **kwargs,
    ) -> str:
        """
        Synchronous version of multimodal query (已废弃，请使用 aquery_with_multimodal)

        Args:
            query: Base query text
            multimodal_content: List of multimodal content, each element contains:
                - type: Content type ("image", "table", "equation", etc.)
                - Other fields depend on type (e.g., img_path, table_data, latex, etc.)
            mode: Query mode ("local", "global", "hybrid", "naive", "mix", "bypass")
            **kwargs: Other query parameters, will be passed to QueryParam

        Returns:
            str: Query result
        """
        import warnings
        warnings.warn(
            "query_with_multimodal is deprecated, use aquery_with_multimodal instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.aquery_with_multimodal(query, multimodal_content, mode=mode, **kwargs)
            )
        finally:
            loop.close()
