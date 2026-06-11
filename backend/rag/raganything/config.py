"""
Configuration classes for RAGAnything

Contains configuration dataclasses with environment variable support
"""

from dataclasses import dataclass, field
from typing import List

from lightrag.utils import get_env_value


@dataclass
class RAGAnythingConfig:
    """Configuration class for RAGAnything with environment variable support"""

    # Directory Configuration
    # ---
    working_dir: str = field(default=get_env_value("WORKING_DIR", "./rag_storage", str))
    """Directory where RAG storage and cache files are stored."""

    # Parser Configuration
    # ---
    parse_method: str = field(default=get_env_value("PARSE_METHOD", "auto", str))
    """Default parsing method for document parsing: 'auto', 'ocr', or 'txt'."""

    parser_output_dir: str = field(default=get_env_value("OUTPUT_DIR", "./output", str))
    """Default output directory for parsed content."""

    parser: str = field(default=get_env_value("PARSER", "mineru", str))
    """Parser selection: 'mineru' or 'docling'."""

    display_content_stats: bool = field(
        default=get_env_value("DISPLAY_CONTENT_STATS", True, bool)
    )
    """Whether to display content statistics during parsing."""

    # Multimodal Processing Configuration
    # ---
    enable_image_processing: bool = field(
        default=get_env_value("ENABLE_IMAGE_PROCESSING", True, bool)
    )
    """Enable image content processing."""

    enable_table_processing: bool = field(
        default=get_env_value("ENABLE_TABLE_PROCESSING", True, bool)
    )
    """Enable table content processing."""

    enable_equation_processing: bool = field(
        default=get_env_value("ENABLE_EQUATION_PROCESSING", True, bool)
    )
    """Enable equation content processing."""

    vision_model_max_length: int = field(
        default=get_env_value("VISION_MODEL_MAX_LENGTH", 16384, int)
    )
    """Maximum generation tokens for vision model (default: 16384, max: 16K)."""

    # Batch Processing Configuration
    # ---
    max_concurrent_files: int = field(
        default=get_env_value("MAX_CONCURRENT_FILES", 1, int)
    )
    """Maximum number of files to process concurrently."""

    supported_file_extensions: List[str] = field(
        default_factory=lambda: get_env_value(
            "SUPPORTED_FILE_EXTENSIONS",
            ".pdf,.jpg,.jpeg,.png,.bmp,.tiff,.tif,.gif,.webp,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md",
            str,
        ).split(",")
    )
    """List of supported file extensions for batch processing."""

    recursive_folder_processing: bool = field(
        default=get_env_value("RECURSIVE_FOLDER_PROCESSING", True, bool)
    )
    """Whether to recursively process subfolders in batch mode."""

    # Chunking Configuration
    # ---
    use_semantic_chunking: bool = field(
        default=get_env_value("USE_SEMANTIC_CHUNKING", True, bool)
    )
    """Whether to use semantic chunking."""

    semantic_chunking_threshold: int = field(
        default=get_env_value("SEMANTIC_CHUNKING_THRESHOLD", 95, int)
    )
    """Percentile threshold for semantic chunking (0-100)."""

    embedding_batch_size: int = field(
        default=get_env_value("EMBEDDING_BATCH_SIZE", 100, int)
    )
    """Batch size for embedding calculations in semantic chunking."""

    # Performance Optimization Configuration
    # ---
    entity_extraction_max_gleaning: int = field(
        default=get_env_value("ENTITY_EXTRACTION_MAX_GLEANING", 1, int)
    )
    """Maximum gleaning rounds for entity extraction (0 to disable, 1+ to enable additional extraction passes)."""

    entity_extraction_batch_size: int = field(
        default=get_env_value("ENTITY_EXTRACTION_BATCH_SIZE", 50, int)
    )
    """Number of chunks to process per batch during entity extraction."""

    max_concurrent_entity_batches: int = field(
        default=get_env_value("MAX_CONCURRENT_ENTITY_BATCHES", 15, int)
    )
    """Maximum number of concurrent entity extraction batches."""

    max_concurrent_entity_nodes: int = field(
        default=get_env_value("MAX_CONCURRENT_ENTITY_NODES", 20, int)
    )
    """Maximum number of concurrent entity node storage operations."""

    max_concurrent_text_processing: int = field(
        default=get_env_value("MAX_CONCURRENT_TEXT_PROCESSING", 40, int)
    )
    """Maximum number of concurrent text processing tasks in multimodal content processing."""

    max_concurrent_vision_processing: int = field(
        default=get_env_value("MAX_CONCURRENT_VISION_PROCESSING", 40, int)
    )
    """Maximum number of concurrent vision processing tasks (image, table, equation) in multimodal content processing."""

    # Context Extraction Configuration
    # ---
    context_window: int = field(default=get_env_value("CONTEXT_WINDOW", 1, int))
    """Number of pages/chunks to include before and after current item for context."""

    context_mode: str = field(default=get_env_value("CONTEXT_MODE", "page", str))
    """Context extraction mode: 'page' for page-based, 'chunk' for chunk-based."""

    max_context_tokens: int = field(
        default=get_env_value("MAX_CONTEXT_TOKENS", 2000, int)
    )
    """Maximum number of tokens in extracted context."""

    include_headers: bool = field(default=get_env_value("INCLUDE_HEADERS", True, bool))
    """Whether to include document headers and titles in context."""

    include_captions: bool = field(
        default=get_env_value("INCLUDE_CAPTIONS", True, bool)
    )
    """Whether to include image/table captions in context."""

    context_filter_content_types: List[str] = field(
        default_factory=lambda: get_env_value(
            "CONTEXT_FILTER_CONTENT_TYPES", "text", str
        ).split(",")
    )
    """Content types to include in context extraction (e.g., 'text', 'image', 'table')."""

    content_format: str = field(default=get_env_value("CONTENT_FORMAT", "minerU", str))
    """Default content format for context extraction when processing documents."""

    # Path Handling Configuration
    # ---
    use_full_path: bool = field(default=get_env_value("USE_FULL_PATH", False, bool))
    """Whether to use full file path (True) or just basename (False) for file references in LightRAG."""

    # Query Configuration
    # ---
    query_test_top_k: int = field(
        default=get_env_value("QUERY_TEST_TOP_K", 30, int)
    )
    """Top-k value used for test retrieval in dynamic rerank parameter adjustment."""

    query_test_chunk_top_k: int = field(
        default=get_env_value("QUERY_TEST_CHUNK_TOP_K", 10, int)
    )
    """Chunk top-k value used for test retrieval in dynamic rerank parameter adjustment."""

    query_target_context_length: int = field(
        default=get_env_value("QUERY_TARGET_CONTEXT_LENGTH", 12000, int)
    )
    """Target context length for dynamic rerank parameter adjustment."""

    query_default_top_k: int = field(
        default=get_env_value("QUERY_DEFAULT_TOP_K", 60, int)
    )
    """Default top-k value for query retrieval."""

    query_default_chunk_top_k: int = field(
        default=get_env_value("QUERY_DEFAULT_CHUNK_TOP_K", 15, int)
    )
    """Default chunk top-k value for query retrieval."""

    query_min_top_k: int = field(
        default=get_env_value("QUERY_MIN_TOP_K", 20, int)
    )
    """Minimum top-k value allowed in dynamic adjustment."""

    query_max_top_k: int = field(
        default=get_env_value("QUERY_MAX_TOP_K", 100, int)
    )
    """Maximum top-k value allowed in dynamic adjustment."""

    query_min_chunk_top_k: int = field(
        default=get_env_value("QUERY_MIN_CHUNK_TOP_K", 5, int)
    )
    """Minimum chunk top-k value allowed in dynamic adjustment."""

    query_max_chunk_top_k: int = field(
        default=get_env_value("QUERY_MAX_CHUNK_TOP_K", 30, int)
    )
    """Maximum chunk top-k value allowed in dynamic adjustment."""

    query_ratio_min: float = field(
        default=get_env_value("QUERY_RATIO_MIN", 0.5, float)
    )
    """Minimum ratio for dynamic adjustment."""

    query_ratio_max: float = field(
        default=get_env_value("QUERY_RATIO_MAX", 2.0, float)
    )
    """Maximum ratio for dynamic adjustment."""

    # Retry Configuration
    # ---
    retry_max_attempts: int = field(
        default=get_env_value("RETRY_MAX_ATTEMPTS", 3, int)
    )
    """Maximum number of retry attempts for transient failures."""

    retry_min_wait: float = field(
        default=get_env_value("RETRY_MIN_WAIT", 1.0, float)
    )
    """Minimum wait time between retries in seconds."""

    retry_max_wait: float = field(
        default=get_env_value("RETRY_MAX_WAIT", 30.0, float)
    )
    """Maximum wait time between retries in seconds."""

    retry_exponential_multiplier: float = field(
        default=get_env_value("RETRY_EXPONENTIAL_MULTIPLIER", 1.0, float)
    )
    """Multiplier for exponential backoff between retries."""

    # LLM Configuration
    # ---
    llm_model_max_async: int = field(
        default=get_env_value("LLM_MODEL_MAX_ASYNC", 80, int)
    )
    """Maximum concurrent async LLM calls for ingestion."""

    query_llm_model_max_async: int = field(
        default=get_env_value("QUERY_LLM_MODEL_MAX_ASYNC", 80, int)
    )
    """Maximum concurrent async LLM calls for query."""

    default_llm_timeout: int = field(
        default=get_env_value("LLM_TIMEOUT", 300, int)
    )
    """LLM timeout in seconds. Worker execution timeout = default_llm_timeout * 2."""

    # VLM Configuration
    # ---
    vlm_max_images: int = field(
        default=get_env_value("RAG_VLM_MAX_IMAGES", 10, int)
    )
    """Maximum number of images to process in VLM enhanced query."""

    vlm_remote_image_timeout: float = field(
        default=get_env_value("RAG_VLM_REMOTE_IMAGE_TIMEOUT", 20.0, float)
    )
    """Timeout in seconds for downloading remote images in VLM query."""

    vlm_remote_image_max_mb: int = field(
        default=get_env_value("RAG_VLM_REMOTE_IMAGE_MAX_MB", 15, int)
    )
    """Maximum size in MB for remote images in VLM query."""

    def __post_init__(self):
        """Post-initialization setup for backward compatibility"""
        # Support legacy environment variable names for backward compatibility
        legacy_parse_method = get_env_value("MINERU_PARSE_METHOD", None, str)
        if legacy_parse_method and not get_env_value("PARSE_METHOD", None, str):
            self.parse_method = legacy_parse_method
            import warnings

            warnings.warn(
                "MINERU_PARSE_METHOD is deprecated. Use PARSE_METHOD instead.",
                DeprecationWarning,
                stacklevel=2,
            )

    @property
    def mineru_parse_method(self) -> str:
        """
        Backward compatibility property for old code.

        .. deprecated::
           Use `parse_method` instead. This property will be removed in a future version.
        """
        import warnings

        warnings.warn(
            "mineru_parse_method is deprecated. Use parse_method instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.parse_method

    @mineru_parse_method.setter
    def mineru_parse_method(self, value: str):
        """Setter for backward compatibility"""
        import warnings

        warnings.warn(
            "mineru_parse_method is deprecated. Use parse_method instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.parse_method = value
