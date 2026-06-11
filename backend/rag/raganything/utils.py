"""
Utility functions for RAGAnything

Contains helper functions for content separation, text insertion, and other utilities
"""

import base64
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from lightrag.utils import logger

from backend.rag.raganything.chunking import SemanticSplitter


def is_remote_image_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip().lower()
    return s.startswith(("http://", "https://", "data:image/"))


def _sanitize_utf8_string(value: str) -> str:
    if value and re.search(r"[\ud800-\udfff]", value):
        value = re.sub(r"[\ud800-\udfff]", "?", value)
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def _sanitize_utf8(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return _sanitize_utf8_string(value)
    if isinstance(value, list):
        return [_sanitize_utf8(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_utf8(v) for v in value)
    if isinstance(value, dict):
        return {
            (_sanitize_utf8_string(k) if isinstance(k, str) else k): _sanitize_utf8(v)
            for k, v in value.items()
        }
    return value


def separate_content(
    content_list: List[Dict[str, Any]],
    process_equation_as_text: bool = True,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Separate text content and multimodal content

    Args:
        content_list: Content list from MinerU parsing
        process_equation_as_text: If True, treat equation as text content instead of multimodal

    Returns:
        (text_content, multimodal_items): Pure text content and multimodal items list
    """
    text_parts = []
    multimodal_items = []

    for item in content_list:
        content_type = item.get("type", "text")

        if content_type == "text":
            # Text content
            text = item.get("text", "")
            if text.strip():
                text_parts.append(text)
        elif content_type == "equation" and process_equation_as_text:
            # Treat equation as text content to avoid separate LLM processing
            equation_text = item.get("text", "")
            if equation_text.strip():
                # Format equation as LaTeX block
                formatted_equation = f"$$ {equation_text} $$"
                text_parts.append(formatted_equation)
        elif content_type == "discarded":
            # Skip discarded content entirely to avoid unnecessary processing
            continue
        else:
            # Multimodal content (image, table, etc.)
            multimodal_items.append(item)

    # Merge all text content
    text_content = "\n\n".join(text_parts)

    logger.info("Content separation complete:")
    logger.info(f"  - Text content length: {len(text_content)} characters")
    logger.info(f"  - Multimodal items count: {len(multimodal_items)}")

    # Count multimodal types
    modal_types = {}
    for item in multimodal_items:
        modal_type = item.get("type", "unknown")
        modal_types[modal_type] = modal_types.get(modal_type, 0) + 1

    if modal_types:
        logger.info(f"  - Multimodal type distribution: {modal_types}")

    return text_content, multimodal_items


def encode_image_to_base64(image_path: str) -> str:
    """
    Encode image file to base64 string

    Args:
        image_path: Path to the image file

    Returns:
        str: Base64 encoded string, empty string if encoding fails
    """
    try:
        if is_remote_image_path(image_path):
            return ""
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
        return encoded_string
    except Exception as e:
        logger.error(f"Failed to encode image {image_path}: {e}")
        return ""


def validate_image_file(image_path: str, max_size_mb: int = 50) -> bool:
    """
    Validate if a file is a valid image file

    Args:
        image_path: Path to the image file
        max_size_mb: Maximum file size in MB

    Returns:
        bool: True if valid, False otherwise
    """
    try:
        if is_remote_image_path(image_path):
            return True
        path = Path(image_path)

        logger.debug(f"Validating image path: {image_path}")
        logger.debug(f"Resolved path object: {path}")
        logger.debug(f"Path exists check: {path.exists()}")

        # Check if file exists
        if not path.exists():
            logger.warning(f"Image file not found: {image_path}")
            return False

        # Check file extension
        image_extensions = [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".webp",
            ".tiff",
            ".tif",
        ]

        path_lower = str(path).lower()
        has_valid_extension = any(path_lower.endswith(ext) for ext in image_extensions)
        logger.debug(
            f"File extension check - path: {path_lower}, valid: {has_valid_extension}"
        )

        if not has_valid_extension:
            logger.warning(f"File does not appear to be an image: {image_path}")
            return False

        # Check file size
        file_size = path.stat().st_size
        max_size = max_size_mb * 1024 * 1024
        logger.debug(
            f"File size check - size: {file_size} bytes, max: {max_size} bytes"
        )

        if file_size > max_size:
            logger.warning(f"Image file too large ({file_size} bytes): {image_path}")
            return False

        logger.debug(f"Image validation successful: {image_path}")
        return True

    except Exception as e:
        logger.error(f"Error validating image file {image_path}: {e}")
        return False


async def insert_text_content(
    lightrag,
    input: str | list[str],
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    ids: str | list[str] | None = None,
    file_paths: str | list[str] | None = None,
    use_semantic_chunking: bool = False,
    semantic_chunking_threshold: int = 95,
    embedding_batch_size: int = 100,
) -> dict:
    """
    Insert pure text content into LightRAG

    Args:
        lightrag: LightRAG instance
        input: Single document string or list of document strings
        split_by_character: if split_by_character is not None, split the string by character, if chunk longer than
        chunk_token_size, it will be split again by token size.
        split_by_character_only: if split_by_character_only is True, split the string by character only, when
        split_by_character is None, this parameter is ignored.
        ids: single string of the document ID or list of unique document IDs, if not provided, MD5 hash IDs will be generated
        file_paths: single string of the file path or list of file paths, used for citation
        use_semantic_chunking: Whether to use semantic chunking
        semantic_chunking_threshold: Percentile threshold for semantic chunking
        embedding_batch_size: Batch size for embedding calculations in semantic chunking
    
    Returns:
        dict with 'chunk_count' key
    """
    chunk_count = 0
    
    if use_semantic_chunking:
        if hasattr(lightrag, "embedding_func") and lightrag.embedding_func:
            logger.info(
                f"Using semantic chunking with threshold {semantic_chunking_threshold}"
            )
            tokenizer = getattr(lightrag, "tokenizer", None)
            max_token_size = getattr(lightrag, "chunk_token_size", 1000)
            
            splitter = SemanticSplitter(
                embedding_func=lightrag.embedding_func,
                tokenizer=tokenizer,
                breakpoint_percentile_threshold=semantic_chunking_threshold,
                max_token_size=max_token_size,
                embedding_batch_size=embedding_batch_size,
            )

            inputs = input if isinstance(input, list) else [input]
            new_inputs = []

            for i, text in enumerate(inputs):
                logger.info(
                    f"Semantic chunking input text {i+1}/{len(inputs)} length: {len(text)}"
                )
                chunks = await splitter.split_text(text)
                chunk_count += len(chunks)
                logger.info(f"Semantic chunking result: {len(chunks)} chunks generated")
                combined = "######SEMANTIC_SPLIT######".join(chunks)
                new_inputs.append(combined)

            input = new_inputs if isinstance(input, list) else new_inputs[0]
            split_by_character = "######SEMANTIC_SPLIT######"
            split_by_character_only = True
        else:
            logger.warning(
                "Semantic chunking requested but embedding_func is missing or invalid in LightRAG instance"
            )
    else:
        logger.info("Semantic chunking disabled")

    logger.info("Starting text content insertion into LightRAG...")

    input = _sanitize_utf8(input)
    ids = _sanitize_utf8(ids)
    file_paths = _sanitize_utf8(file_paths)

    # Use LightRAG's insert method with all parameters
    await lightrag.ainsert(
        input=input,
        file_paths=file_paths,
        split_by_character=split_by_character,
        split_by_character_only=split_by_character_only,
        ids=ids,
    )

    logger.info("Text content insertion complete")
    
    return {"chunk_count": chunk_count}


async def insert_text_content_with_multimodal_content(
    lightrag,
    input: str | list[str],
    multimodal_content: list[dict[str, Any]] | None = None,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    ids: str | list[str] | None = None,
    file_paths: str | list[str] | None = None,
    scheme_name: str | None = None,
    use_semantic_chunking: bool = False,
    semantic_chunking_threshold: int = 95,
    embedding_batch_size: int = 100,
):
    """
    Insert pure text content into LightRAG

    Args:
        lightrag: LightRAG instance
        input: Single document string or list of document strings
        multimodal_content: Multimodal content list (optional)
        split_by_character: if split_by_character is not None, split the string by character, if chunk longer than
        chunk_token_size, it will be split again by token size.
        split_by_character_only: if split_by_character_only is True, split the string by character only, when
        split_by_character is None, this parameter is ignored.
        ids: single string of the document ID or list of unique document IDs, if not provided, MD5 hash IDs will be generated
        file_paths: single string of the file path or list of file paths, used for citation
        scheme_name: scheme name (optional)
        use_semantic_chunking: Whether to use semantic chunking
        semantic_chunking_threshold: Percentile threshold for semantic chunking
        embedding_batch_size: Batch size for embedding calculations in semantic chunking
    """
    if (
        use_semantic_chunking
        and hasattr(lightrag, "embedding_func")
        and lightrag.embedding_func
    ):
        logger.info(
            f"Using semantic chunking with threshold {semantic_chunking_threshold}"
        )
        tokenizer = getattr(lightrag, "tokenizer", None)
        max_token_size = getattr(lightrag, "chunk_token_size", 1000)
        
        splitter = SemanticSplitter(
            embedding_func=lightrag.embedding_func,
            tokenizer=tokenizer,
            breakpoint_percentile_threshold=semantic_chunking_threshold,
            max_token_size=max_token_size,
            embedding_batch_size=embedding_batch_size,
        )

        inputs = input if isinstance(input, list) else [input]
        new_inputs = []

        for text in inputs:
            chunks = await splitter.split_text(text)
            combined = "######SEMANTIC_SPLIT######".join(chunks)
            new_inputs.append(combined)

        input = new_inputs if isinstance(input, list) else new_inputs[0]
        split_by_character = "######SEMANTIC_SPLIT######"
        split_by_character_only = True

    logger.info("Starting text content insertion into LightRAG...")

    input = _sanitize_utf8(input)
    multimodal_content = _sanitize_utf8(multimodal_content)
    ids = _sanitize_utf8(ids)
    file_paths = _sanitize_utf8(file_paths)

    # Use LightRAG's insert method with supported parameters only
    # Note: LightRAG's ainsert doesn't support multimodal_content or scheme_name
    try:
        await lightrag.ainsert(
            input=input,
            file_paths=file_paths,
            split_by_character=split_by_character,
            split_by_character_only=split_by_character_only,
            ids=ids,
        )
    except Exception as e:
        logger.error(f"Error during ainsert: {e}")
        raise

    logger.info("Text content insertion complete")


def get_processor_for_type(modal_processors: Dict[str, Any], content_type: str):
    """
    Get appropriate processor based on content type

    Args:
        modal_processors: Dictionary of available processors
        content_type: Content type

    Returns:
        Corresponding processor instance
    """
    # Direct mapping to corresponding processor
    if content_type == "image":
        return modal_processors.get("image")
    elif content_type == "table":
        return modal_processors.get("table")
    elif content_type == "equation":
        return modal_processors.get("equation")
    else:
        # For other types, use generic processor
        return modal_processors.get("generic")


def get_processor_supports(proc_type: str) -> List[str]:
    """Get processor supported features"""
    supports_map = {
        "image": [
            "Image content analysis",
            "Visual understanding",
            "Image description generation",
            "Image entity extraction",
        ],
        "table": [
            "Table structure analysis",
            "Data statistics",
            "Trend identification",
            "Table entity extraction",
        ],
        "equation": [
            "Mathematical formula parsing",
            "Variable identification",
            "Formula meaning explanation",
            "Formula entity extraction",
        ],
        "generic": [
            "General content analysis",
            "Structured processing",
            "Entity extraction",
        ],
    }
    return supports_map.get(proc_type, ["Basic processing"])
