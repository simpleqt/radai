"""Domain-specific application settings.

Every section reads the same flat environment namespace so existing `.env`
files remain compatible while ownership is split by domain.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv


ENV_FILE = os.path.join(
    os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
    ".env",
)

load_dotenv(ENV_FILE, override=False)


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

    rag_chunk_token_size: int | None = 600
    rag_chunk_overlap_token_size: int | None = 120
    long_doc_prefix_length: int = 1000
    embedding_batch_size: int | None = 200
    embedding_max_concurrency: int = 4
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
    time_aware_undated_doc_beta: float = 0.7
    time_aware_created_at_fallback_beta: float = 0.1

    rag_work_dir: str | None = None
    rag_default_retrieval_profile: str = "default"
    rag_kb_retrieval_profile_map: str = "{}"
    default_kb_code: str = "default"

    self_ingestion_enabled: bool = False
    self_retriever_enabled: bool = False
    self_ingestion_allowlist: str = ""
    self_retriever_allowlist: str = ""
    self_ingestion_kb_allowlist: str = ""
    self_retriever_kb_allowlist: str = ""
    self_graph_retrieval_profile: Literal[
        "legacy", "seed_only", "routed", "filtered", "optimized", "pathrag"
    ] = "optimized"
    # PathRAG uses generic clause-level entity recall followed by bounded,
    # evidence-grounded paths between retrieved seed entities.  All limits are
    # independent of query wording so the profile remains suitable for a real
    # ablation rather than a benchmark-specific router.
    self_pathrag_seed_limit: int = 12
    self_pathrag_query_variants: int = 4
    self_pathrag_max_hops: int = 3
    self_pathrag_max_paths: int = 6
    self_pathrag_edge_limit: int = 600
    self_pathrag_node_limit: int = 240
    self_pathrag_flow_alpha: float = 0.65
    self_pathrag_prune_threshold: float = 0.008
    self_pathrag_diversity: float = 0.35
    # Domain reranker remains opt-in: the 2026-07-17 ablation improved easy
    # precision but reduced hard-query recall and doubled retrieval latency.
    self_rerank_enabled: bool = False
    self_rerank_candidate_multiplier: int = 2
    self_rerank_max_candidates: int = 24
    self_rerank_weight: float = 0.72
    self_rerank_max_chars: int = 6000
    # Expand high-confidence parent anchors into bounded structural evidence
    # windows.  Single anchors require rank/score plus independent retrieval
    # support; this avoids query-word special cases while recovering formula and
    # table continuations that occur just outside the initially recalled chunk.
    self_parent_expansion_enabled: bool = True
    self_parent_expansion_doc_limit: int = 2
    self_parent_expansion_anchor_limit: int = 2
    self_parent_expansion_radius: int = 1
    self_parent_expansion_media_radius: int = 5
    self_parent_expansion_max_chars: int = 4800
    self_parent_expansion_single_anchor_enabled: bool = True
    self_parent_expansion_single_anchor_max_rank: int = 3
    self_parent_expansion_single_anchor_score_ratio: float = 0.78
    self_parent_expansion_single_anchor_min_paths: int = 2
    self_parent_expansion_render_max_rows: int = 4
    self_parent_expansion_table_max_rows: int = 6
    self_parent_expansion_promotion_enabled: bool = True
    self_parent_expansion_promotion_max_rank: int = 6
    self_parent_expansion_promotion_min_score: float = 2.0
    self_parent_expansion_promotion_margin: float = 1.25
    self_extraction_mode: Literal["simple", "schema_aware", "llm"] = "llm"
    self_extraction_llm_model: str | None = None
    self_extraction_llm_auto: bool = True
    self_extraction_require_llm: bool = True
    self_extraction_llm_timeout: int = 45
    self_extraction_llm_max_tokens: int = 8192
    self_extraction_batch_chunks: int = 8
    self_extraction_batch_retries: int = 1
    self_extraction_failed_batch_retries: int = 3
    self_extraction_max_batch_failure_ratio: float = 0.25
    self_extraction_orphan_linking: bool = True
    self_extraction_orphan_max_links: int = 3
    self_extraction_orphan_typed_first: bool = True
    self_redis_bm25_mode: Literal["inverted", "scan", "redisearch"] = "inverted"
    self_ingestion_metrics_enabled: bool = True
    self_ingestion_nebula_write_evidence: bool = False
    self_ingestion_nebula_insert_batch_size: int = 80
    self_ingestion_nebula_write_workers: int = 4
    self_ingestion_commit_external_parallel: bool = True
    self_ingestion_commit_mysql_max_concurrency: int = 1
    self_ingestion_commit_mysql_retries: int = 8
    self_ingestion_commit_mysql_retry_base_delay: float = 1.0
    self_ingestion_embed_entities_during_ingest: bool = True
    self_redis_bm25_pipe_batch: int = 2000
    milvus_visibility_batch_size: int = 200
    milvus_insert_batch_size: int = 100
    self_vision_model: str | None = None
    self_multimodal_block_type_weights: str = (
        '{"text": 1.0, "image": 1.2, "table": 1.3, "formula": 1.1}'
    )
    default_ingestion_backend: Literal["legacy_lightrag", "self_ingestion"] = (
        "legacy_lightrag"
    )
    default_retrieval_backend: Literal[
        "legacy_lightrag",
        "self_ingestion",
        "hybrid",
    ] = "legacy_lightrag"

    mineru_api_url: str
    mineru_backend: str
    mineru_lang: str
    mineru_method: str
    mineru_formula_enable: bool
    mineru_table_enable: bool
    self_ingestion_mineru_max_concurrency: int = 2
    self_ingestion_mineru_timeout: int = 3600

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
    redis_port: int | None = 6379
    redis_password: str | None = None
    redis_db: int | None = 0
    knowledge_graph_cache_ttl_seconds: int = 600

    nebula_host: str = ""
    nebula_graph_port: int = 9669
    nebula_user: str = "root"
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
    kafka_producer_max_request_size: int = 10 * 1024 * 1024
    kafka_producer_compression_type: str = "gzip"
    kafka_topic_max_message_bytes: int = 10 * 1024 * 1024
    kafka_result_text_max_chars: int = 256 * 1024
    kafka_result_max_items: int = 200

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
