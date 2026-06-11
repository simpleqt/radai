"""
MD单文件入库器 - 直接从MD文件入库，无需contentlist.json

特点：
1. 直接解析MD文件中的<img>标签提取图片
2. 提取图片周围的文本作为上下文
3. 图片说明通常位于图片下一行
4. 复用原有的ImageModalProcessor进行图片入库
"""

import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import httpx
from lightrag.utils import compute_mdhash_id

from backend.config.loggers import get_logger
from backend.rag.raganything.utils import (
    insert_text_content,
)

logger = get_logger(__name__)


@dataclass
class ImageInfo:
    """图片信息"""
    original_src: str
    absolute_url: str
    alt_text: str
    position: int  # 在文本中的位置索引
    before_text: str = ""  # 图片前的文本
    after_text: str = ""   # 图片后的文本（通常包含图注）
    caption: str = ""      # 提取的图注


@dataclass
class ParsedMD:
    """解析后的MD内容"""
    text: str  # 纯文本内容（图片替换为占位符）
    images: List[ImageInfo] = field(default_factory=list)


class MDIngestor:
    """MD单文件入库器"""

    def __init__(self, rag_anything_instance, config: Dict = None):
        """
        初始化MD入库器

        Args:
            rag_anything_instance: RAGAnything实例
            config: 配置项
                - context_window: 上下文窗口大小（前后各多少字符）
                - caption_pattern: 图注匹配正则
        """
        self.rag = rag_anything_instance
        self.config = config or {}
        self.context_window = self.config.get('context_window', 500)
        self.caption_pattern = self.config.get('caption_pattern', r'[图表]\s*\d+.*|Fig\.?\s*\d+.*')
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取HTTP客户端"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=120.0)
        return self._http_client

    async def close(self):
        """关闭资源"""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def ingest_from_url(
        self,
        md_url: str,
        doc_id: str = None,
        file_name: str = None
    ) -> Dict[str, Any]:
        """
        从URL读取MD文件并入库

        Args:
            md_url: MD文件URL
            doc_id: 文档ID，不传则自动生成
            file_name: 文件名（用于显示）

        Returns:
            入库结果信息
        """
        start_time = time.time()
        logger.info(f"开始入库MD文件: {md_url}")

        try:
            # 1. 下载MD内容
            md_content = await self._fetch_md_content(md_url)
            logger.info(f"MD内容下载完成，长度: {len(md_content)} 字符")

            # 2. 解析MD内容
            parsed = self._parse_md_content(md_content, base_url=md_url)
            logger.info(f"MD解析完成，提取 {len(parsed.images)} 张图片")

            # 3. 处理图片（下载并转换为base64）
            if parsed.images:
                await self._process_images(parsed.images)

            # 4. 生成doc_id
            if doc_id is None:
                doc_id = self._generate_doc_id(md_content)

            # 5. 入库
            result = await self._insert_to_rag(
                parsed=parsed,
                doc_id=doc_id,
                file_path=file_name or md_url
            )

            elapsed_time = time.time() - start_time
            logger.info(f"MD文件入库完成，耗时: {elapsed_time:.2f}s")

            return {
                'success': True,
                'doc_id': doc_id,
                'file_path': file_name or md_url,
                'text_length': len(parsed.text),
                'images_count': len(parsed.images),
                'elapsed_time': elapsed_time,
                **result
            }

        except Exception as e:
            logger.error(f"MD文件入库失败: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'file_path': file_name or md_url
            }

    async def ingest_from_content(
        self,
        md_content: str,
        doc_id: str = None,
        file_path: str = None,
        base_url: str = None
    ) -> Dict[str, Any]:
        """
        直接从MD内容入库

        Args:
            md_content: MD文件内容
            doc_id: 文档ID
            file_path: 文件路径（用于显示）
            base_url: 基础URL（用于解析相对路径）

        Returns:
            入库结果信息
        """
        start_time = time.time()
        logger.info(f"开始入库MD内容，长度: {len(md_content)} 字符")

        try:
            # 1. 解析MD内容
            parsed = self._parse_md_content(md_content, base_url=base_url)
            logger.info(f"MD解析完成，提取 {len(parsed.images)} 张图片")

            # 2. 处理图片
            if parsed.images:
                await self._process_images(parsed.images)

            # 3. 生成doc_id
            if doc_id is None:
                doc_id = self._generate_doc_id(md_content)

            # 4. 入库
            result = await self._insert_to_rag(
                parsed=parsed,
                doc_id=doc_id,
                file_path=file_path or doc_id
            )

            elapsed_time = time.time() - start_time
            logger.info(f"MD内容入库完成，耗时: {elapsed_time:.2f}s")

            return {
                'success': True,
                'doc_id': doc_id,
                'file_path': file_path or doc_id,
                'text_length': len(parsed.text),
                'images_count': len(parsed.images),
                'elapsed_time': elapsed_time,
                **result
            }

        except Exception as e:
            logger.error(f"MD内容入库失败: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'file_path': file_path or doc_id
            }

    async def _fetch_md_content(self, md_url: str) -> str:
        """从URL获取MD内容"""
        client = await self._get_http_client()
        response = await client.get(md_url)
        response.raise_for_status()
        return response.text

    def _parse_md_content(self, md_content: str, base_url: str = None) -> ParsedMD:
        """
        解析MD内容，提取文本和图片

        策略：
        1. 提取所有<img>标签
        2. 记录图片位置，提取前后文本作为上下文
        3. 图片下一行通常包含图注（如"图1 xxx"或"Fig.1 xxx"）

        Args:
            md_content: MD内容
            base_url: 基础URL（用于解析相对路径）

        Returns:
            ParsedMD对象
        """
        images = []
        text_parts = []
        last_end = 0
        position = 0

        # 匹配<img>标签的正则
        img_pattern = re.compile(
            r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*(?:alt=["\']([^"\']*)["\'])?[^>]*>',
            re.IGNORECASE | re.DOTALL
        )

        for match in img_pattern.finditer(md_content):
            # 1. 提取图片前的文本
            before_text = md_content[last_end:match.start()]
            text_parts.append(before_text)

            # 2. 提取图片信息
            src = match.group(1)
            alt = match.group(2) or ""
            absolute_url = self._resolve_url(src, base_url)

            # 3. 提取图片后的文本（用于获取图注）
            after_start = match.end()
            # 获取图片后的一段文本（用于提取图注）
            after_text = md_content[after_start:after_start + self.context_window]

            # 4. 提取图注（通常在图片后的第一行）
            caption = self._extract_caption(after_text)

            image_info = ImageInfo(
                original_src=src,
                absolute_url=absolute_url,
                alt_text=alt,
                position=position,
                before_text=before_text[-self.context_window:] if before_text else "",
                after_text=after_text,
                caption=caption
            )
            images.append(image_info)

            # 5. 添加图片占位符到文本
            placeholder = f"\n[图片{position+1}]: {caption or alt or '图片'}\n"
            text_parts.append(placeholder)

            last_end = match.end()
            position += 1

        # 添加剩余的文本
        text_parts.append(md_content[last_end:])

        # 合并文本
        full_text = "".join(text_parts)

        # 清理多余的空行
        full_text = re.sub(r'\n{3,}', '\n\n', full_text)

        return ParsedMD(text=full_text, images=images)

    def _resolve_url(self, url: str, base_url: str = None) -> str:
        """解析为绝对URL，并规范化 MinIO URL 的百分号编码。"""
        if not url:
            return ""

        # 已经是绝对URL
        if url.startswith(('http://', 'https://', 'data:')):
            # 对 MinIO HTTP URL 做幂等规范化：单编码 → 双编码
            try:
                from backend.storage import normalize_minio_url
                return normalize_minio_url(url)
            except Exception:
                return url

        # 有基础URL，进行拼接后规范化
        if base_url:
            resolved = urljoin(base_url, url)
            try:
                from backend.storage import normalize_minio_url
                return normalize_minio_url(resolved)
            except Exception:
                return resolved

        return url

    def _extract_caption(self, after_text: str) -> str:
        """
        从图片后的文本中提取图注

        图注通常在图片后的第一行，格式如：
        - 图1 xxx
        - Fig.1 xxx
        - Figure 1 xxx
        """
        # 获取第一行（去除空行）
        lines = [line.strip() for line in after_text.split('\n') if line.strip()]
        if not lines:
            return ""

        first_line = lines[0]

        # 匹配图注模式
        caption_patterns = [
            r'^[图表]\s*\d+[\.\s]*(.+)',  # 图1 xxx, 表1 xxx
            r'^Fig\.?\s*\d+[\.\s]*(.+)',  # Fig.1 xxx, Fig1 xxx
            r'^Figure\s*\d+[\.\s]*(.+)',   # Figure 1 xxx
        ]

        for pattern in caption_patterns:
            match = re.match(pattern, first_line, re.IGNORECASE)
            if match:
                return first_line

        return ""

    async def _process_images(self, images: List[ImageInfo]):
        """处理图片：下载并转换为base64"""
        logger.info(f"开始处理 {len(images)} 张图片")

        async def process_single(image_info: ImageInfo) -> Tuple[str, str]:
            """处理单张图片"""
            try:
                base64_data = await self._download_image_base64(image_info.absolute_url)
                return image_info.absolute_url, base64_data
            except Exception as e:
                logger.error(f"图片处理失败 {image_info.absolute_url}: {e}")
                return image_info.absolute_url, ""

        # 并发处理所有图片
        tasks = [process_single(img) for img in images]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 将结果存储到image_info中
        for image_info, result in zip(images, results):
            if isinstance(result, Exception):
                logger.error(f"图片下载失败: {result}")
                image_info.base64_data = ""
            else:
                _, base64_data = result
                # 动态添加base64_data属性
                image_info.base64_data = base64_data

        logger.info("图片处理完成")

    async def _download_image_base64(self, image_url: str) -> str:
        """下载图片并转换为base64 - 处理URL编码路径
        
        MinIO存储的路径已经是URL编码的（%20代替空格），
        访问时需要双重编码（%2520）才能匹配存储路径。
        """
        if not image_url:
            return ""

        if image_url.startswith('data:image'):
            try:
                _, b64 = image_url.split(',', 1)
                return b64
            except Exception:
                return ""

        if image_url.startswith(('http://', 'https://')):
            client = await self._get_http_client()
            parsed = urlparse(image_url)
            path = parsed.path
            # Use _encode_minio_path_for_url for correct double-encoding.
            # If path already contains %25 (double-encoded by get_public_url),
            # keep as-is to avoid triple-encoding.
            if '%25' in path:
                double_encoded_path = path
            else:
                from backend.storage import _encode_minio_path_for_url
                double_encoded_path = _encode_minio_path_for_url(path)
            encoded_url = parsed._replace(path=double_encoded_path).geturl()
            try:
                response = await client.get(encoded_url)
                response.raise_for_status()
                return base64.b64encode(response.content).decode('utf-8')
            except Exception:
                try:
                    response = await client.get(image_url)
                    response.raise_for_status()
                    return base64.b64encode(response.content).decode('utf-8')
                except Exception as e2:
                    logger.error(f"图片下载失败 {image_url}: {e2}")
                    return ""

        try:
            with open(image_url, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logger.error(f"读取本地图片失败 {image_url}: {e}")
            return ""

    def _generate_doc_id(self, content: str) -> str:
        """生成文档ID"""
        return compute_mdhash_id(content, prefix="doc-")

    async def _insert_to_rag(
        self,
        parsed: ParsedMD,
        doc_id: str,
        file_path: str
    ) -> Dict[str, Any]:
        """
        将内容插入RAG

        策略：
        1. 先删除已存在的文档（如果存在）
        2. 插入文本内容（使用semantic chunking）
        3. 处理图片（使用ImageModalProcessor）
        """
        logger.info(f"开始插入RAG，doc_id: {doc_id}")

        # 使用 RAGAnything 的 lightrag 属性
        lightrag = getattr(self.rag, 'lightrag', self.rag)

        # 0. 删除已存在的文档（如果存在）
        try:
            logger.info(f"尝试删除已存在的文档 {doc_id}")
            await lightrag.adelete_by_doc_id(doc_id)
            logger.info(f"旧文档 {doc_id} 已删除（如果存在）")
        except Exception as e:
            # 文档可能不存在，忽略错误继续
            logger.debug(f"删除旧文档时出错（可能不存在）: {e}")

        # 1. 插入文本内容
        logger.info(f"插入文本内容，长度: {len(parsed.text)}")
        insert_result = await insert_text_content(
            lightrag,
            input=parsed.text,
            ids=doc_id,
            file_paths=file_path,
            use_semantic_chunking=True
        )
        chunk_count = insert_result.get('chunk_count', 0)

        # 2. 处理图片
        image_results = []
        if parsed.images:
            logger.info(f"处理 {len(parsed.images)} 张图片")
            image_results = await self._insert_images(
                images=parsed.images,
                doc_id=doc_id,
                file_path=file_path
            )

        # 3. 获取实体和关系统计数据
        entity_count = 0
        relation_count = 0
        
        try:
            if hasattr(lightrag, 'full_entities'):
                entity_data = await lightrag.full_entities.get_by_id(doc_id)
                if entity_data:
                    entity_count = entity_data.get('count', 0)
            
            if hasattr(lightrag, 'full_relations'):
                relation_data = await lightrag.full_relations.get_by_id(doc_id)
                if relation_data:
                    relation_count = relation_data.get('count', 0)
            
            logger.info(f"文档 {doc_id} 统计: 实体={entity_count}, 关系={relation_count}, 分块={chunk_count}")
        except Exception as e:
            logger.warning(f"获取统计数据失败: {e}")

        return {
            'text_inserted': True,
            'images_processed': len([r for r in image_results if r.get('success')]),
            'images_failed': len([r for r in image_results if not r.get('success')]),
            'entity_count': entity_count,
            'relation_count': relation_count,
            'chunk_count': chunk_count,
        }

    async def _insert_images(
        self,
        images: List[ImageInfo],
        doc_id: str,
        file_path: str
    ) -> List[Dict]:
        """
        插入图片到RAG

        使用RAGAnything的modal_processors处理图片
        """
        results = []

        # 从RAGAnything获取图片处理器
        # RAGAnything在初始化时会创建modal_processors字典
        modal_processors = getattr(self.rag, 'modal_processors', {})
        processor = modal_processors.get('image')

        if not processor:
            logger.warning("RAGAnything未初始化图片处理器，尝试使用vision_model_func创建")
            from backend.rag.raganything.modalprocessors import (
                ContextExtractor,
                ImageModalProcessor,
            )

            # 使用 RAGAnything 的 lightrag 属性
            lightrag = getattr(self.rag, 'lightrag', self.rag)

            # 获取vision_model_func或llm_model_func
            modal_caption_func = getattr(self.rag, 'vision_model_func', None) or \
                                getattr(self.rag, 'llm_model_func', None)

            if not modal_caption_func:
                logger.error("未找到可用的model_func，跳过图片处理")
                return [{'success': False, 'error': 'No model_func available'} for _ in images]

            processor = ImageModalProcessor(
                lightrag=lightrag,
                modal_caption_func=modal_caption_func,
                context_extractor=getattr(self.rag, 'context_extractor', ContextExtractor()),
            )

        # 获取并发控制配置（与原有流程一致）
        max_concurrent_vision = getattr(
            self.rag.config, 'max_concurrent_vision_processing', 40
        )
        vision_semaphore = asyncio.Semaphore(max_concurrent_vision)

        async def process_single_image(idx: int, image_info: ImageInfo) -> Dict:
            """处理单张图片"""
            async with vision_semaphore:
                try:
                    # 构建图片内容数据（与原有content_list格式兼容）
                    modal_content = {
                        'img_path': image_info.absolute_url,
                        'image_caption': [image_info.caption] if image_info.caption else [],
                        'img_caption': [image_info.caption] if image_info.caption else [],
                        'base64_data': getattr(image_info, 'base64_data', ''),
                    }

                    # 构建上下文信息
                    item_info = {
                        'index': idx,
                        'position': image_info.position,
                        'type': 'image',
                        'context_text': image_info.before_text + '\n' + image_info.after_text,
                    }

                    # 处理图片
                    enhanced_caption, entity_info = await processor.process_multimodal_content(
                        modal_content=modal_content,
                        content_type='image',
                        file_path=file_path,
                        item_info=item_info,
                        batch_mode=True,
                        doc_id=doc_id,
                        chunk_order_index=idx,
                    )

                    logger.info(f"图片 {idx+1}/{len(images)} 处理完成: {entity_info.get('entity_name', 'Unknown')}")

                    return {
                        'success': True,
                        'image_url': image_info.absolute_url,
                        'entity_name': entity_info.get('entity_name'),
                        'caption': enhanced_caption[:200] if enhanced_caption else '',
                    }

                except Exception as e:
                    logger.error(f"图片 {idx+1} 处理失败: {e}")
                    return {
                        'success': False,
                        'image_url': image_info.absolute_url,
                        'error': str(e)
                    }

        try:
            # 并发处理所有图片
            tasks = [
                process_single_image(idx, image_info)
                for idx, image_info in enumerate(images)
            ]
            results = await asyncio.gather(*tasks)

        finally:
            await processor.close()

        return results
