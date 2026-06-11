"""Domain-specific application settings.

Every section reads the same flat environment namespace so existing `.env`
files remain compatible while ownership is split by domain.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = os.path.join(
    os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
    ".env",
)


class DomainSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ModelSettings(DomainSettings):
    llm_provider: Literal[
        "deepseek", "zhipu", "qwen", "openai", "minimax", "glm4_flash"
    ]
    ingestion_llm_provider: Literal[
        "deepseek", "zhipu", "qwen", "openai", "minimax", "glm4_flash"
    ] = "minimax"

    deepseek_api_key: str | None = None
    deepseek_api_base: str | None = None
    deepseek_model: str | None = None
    minimax_api_key: str | None = None
    minimax_api_base: str | None = None
    minimax_model: str | None = None
    minimax_max_tokens: int | None = 32000
    zhipu_api_key: str | None = None
    zhipu_api_base: str | None = None
    zhipu_model: str | None = None
    glm4_flash_api_key: str | None = None
    glm4_flash_api_base: str | None = None
    glm4_flash_model: str | None = None
    glm4_flash_max_tokens: int | None = 65536
    dashscope_api_key: str | None = None
    dashscope_api_base: str | None = None
    dashscope_model: str | None = None
    openai_api_key: str | None = None
    openai_api_base: str | None = None
    openai_model: str | None = None

    vlm_api_key: str | None = None
    vlm_api_base: str | None = None
    vlm_model: str | None = None
    vlm_max_length: int | None = 16384
    embedding_api_key: str | None = None
    embedding_api_base: str | None = None
    embedding_model: str | None = None
    embedding_max_token_size: int | None = 8192
    embedding_dim: int | None = None
    vl_embedding_api_base: str | None = None
    vl_embedding_model: str | None = None
    rerank_api_key: str | None = None
    rerank_api_base: str | None = None
    rerank_model: str | None = None
    rerank_max_length: int | None = None

    temperature: float
    max_tokens: int | None = None
    streaming: bool
    available_models: str | None = None


class RagSettings(DomainSettings):
    max_concurrent_files: int | None = 5
    max_concurrent_entity_batches: int | None = 5
    max_concurrent_entity_nodes: int | None = 5
    llm_model_max_async: int | None = 5
    entity_extraction_batch_size: int | None = 5
    max_concurrent_text_processing: int | None = 20
    max_concurrent_vision_processing: int | None = 40

    rag_chunk_token_size: int | None = 1000
    rag_chunk_overlap_token_size: int | None = 200
    long_doc_prefix_length: int = 1000
    embedding_batch_size: int | None = 200
    use_semantic_chunking: bool | None = True
    semantic_chunking_threshold: int | None = 95
    enable_image_processing: bool | None = True
    enable_table_processing: bool | None = True
    enable_equation_processing: bool | None = False
    llm_timeout: int | None = 900

    time_aware_enabled: bool = True
    time_aware_sigma: float = 5.0
    time_aware_alpha_base: float = 0.3
    time_aware_high_boost: float = 0.4
    time_aware_low_penalty: float = 0.3
    time_aware_undated_doc_beta: float = 0.0
    time_aware_created_at_fallback_beta: float = 0.1

    rag_work_dir: str | None = None
    rag_default_retrieval_profile: str = "default"
    rag_kb_retrieval_profile_map: str = "{}"
    default_kb_code: str = "default"

    mineru_api_url: str
    mineru_backend: str
    mineru_lang: str
    mineru_method: str
    mineru_formula_enable: bool
    mineru_table_enable: bool

    keyword_extraction_model: str = "qwen-max"
    keyword_extraction_api_key: str | None = None
    keyword_extraction_api_base: str | None = None
    keyword_extraction_temperature: float = 0.3
    keyword_extraction_max_tokens: int = 512
    keyword_extraction_mode: str = "hybrid"
    keyword_vector_top_k: int = 10
    keyword_bm25_top_k: int = 5
    keyword_rrf_k: int = 60
    keyword_min_score: float = 0.01
    keyword_index_collection: str = "rag_nuclear_electronics_keyword_index"
    keyword_index_cache_ttl: int = 3600
    keyword_index_auto_sync: bool = True
    keyword_index_insert_batch_size: int = 50
    keyword_index_insert_max_concurrency: int = 16


class StorageSettings(DomainSettings):
    milvus_uri: str | None = None
    milvus_user: str | None = None
    milvus_password: str | None = None
    milvus_db_name: str | None = "default"
    milvus_index_type: str = "IVF_PQ"
    milvus_hnsw_m: int = 16
    milvus_hnsw_ef_construction: int = 256
    milvus_ivf_nlist: int = 64
    milvus_pq_m: int = 64
    milvus_pq_nbits: int = 8

    redis_host: str | None = None
    redis_port: int | None = None
    redis_password: str | None = None
    redis_db: int | None = 0

    nebula_host: str = ""
    nebula_graph_port: int = 9669
    nebula_user: str = ""
    nebula_password: str = ""
    nebula_max_connection_pool_size: int = 100

    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_pool_size: int
    mysql_max_overflow: int

    storage_base_path: str
    documents_path: str
    results_path: str
    cache_path: str
    max_file_size: int
    file_expire_days: int
    cache_expire_hours: int
    disk_usage_warning_threshold: float
    save_results_locally: bool

    minio_endpoint: str
    minio_public_base_url: str | None = None
    minio_bucket_name: str
    minio_user_bucket_name: str | None = None
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool

    chunk_size: int
    upload_temp_dir: str
    upload_expire_hours: int


class AuthSettings(DomainSettings):
    internal_api_secret: str = ""
    cas_enabled: bool = False
    cas_base_url: str | None = None
    cas_login_url: str | None = None
    cas_logout_url: str | None = None
    cas_validate_url: str | None = None
    cas_service_url: str | None = None
    cas_frontend_success_url: str = "/login"
    cas_frontend_logout_redirect: str = "/login"
    cas_verify_ssl: bool = True


class ServerSettings(DomainSettings):
    server_host: str
    server_port: int
    server_reload: bool

    tavily_api_key: str
    amap_key: str

    follow_up_suggestions_enabled: bool = True
    follow_up_suggestion_count: int = 3
    follow_up_suggestion_model: str | None = None
    follow_up_suggestion_max_tokens: int = 4096
    # Deprecated compatibility field. The main answer stream is now the deadline.
    follow_up_suggestion_timeout_ms: int = 0
    follow_up_suggestion_min_display_delay_ms: int = 800
    follow_up_suggestion_min_visible_chars: int = 80
    follow_up_guest_enabled: bool = True

    log_level: str
    log_dir: str
    log_max_size: int
    log_backup_count: int
    log_format: str
    log_async: bool
    log_compression: bool
    log_file: str

    kafka_bootstrap_servers: str
    kafka_topics_prefix: str
    kafka_task_topic: str
    kafka_status_topic: str
    kafka_result_topic: str
    kafka_event_topic: str
    kafka_retry_topic: str
    kafka_dead_letter_topic: str
    kafka_consumer_group_prefix: str
    kafka_task_group_id: str
    kafka_status_group_id: str
    kafka_result_group_id: str
    kafka_event_group_id: str
    kafka_retry_group_id: str
    kafka_dead_letter_group_id: str
    kafka_num_partitions: int
    kafka_replication_factor: int
    kafka_max_poll_records: int
    kafka_session_timeout_ms: int
    kafka_heartbeat_interval_ms: int
    kafka_auto_offset_reset: str
    kafka_enable_auto_commit: bool
    kafka_producer_acks: str
    kafka_producer_retries: int
    kafka_producer_enable_idempotence: bool

    log_kafka_enabled: bool = False
    log_kafka_topic: str = "application-logs"
    log_kafka_error_topic: str = "application-logs-error"
    log_kafka_level: str = "INFO"
    log_service_name: str = "main-api"


SETTINGS_SECTION_TYPES = (
    ModelSettings,
    RagSettings,
    StorageSettings,
    AuthSettings,
    ServerSettings,
)
