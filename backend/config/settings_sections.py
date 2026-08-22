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
    # Production self retrieval uses HanLP-tokenized Redis BM25 to seed graph
    # entities.  Chunk-level Milvus retrieval remains enabled independently.
    # The 53-question groundwater ablation on 2026-08-04 selected a 16-token
    # keyword budget (composite 0.7584 / retrieval 0.8075 / 0.962s) over the
    # unlimited budget (0.7565 / 0.7835 / 1.578s).
    self_seed_weight_mode: Literal["vector", "bm25", "hybrid"] = "bm25"
    self_seed_vector_ratio: float = 0.0
    self_seed_keyword_limit: int | None = 16
    # Fusion v2 (2026-08-12 path-composition baseline): score-aware RRF with a
    # keyword admission gate, an entity-weight demotion (entity exclusive gold
    # was 0 across all datasets) and a vector floor applied before hydration
    # truncation so strong dense hits can no longer be crowded out of the
    # candidate cut by rank-only fusion.  Default ON since 2026-08-13: the
    # five-dataset acceptance had v2 winning two sets (+1.3/+2.1 composite),
    # tying two and trailing one 10-question set within judge noise, while
    # recovering MRR (nehard 0.248->0.321); the 39-question crossdoc set has
    # v2 +1.7 over v1, and the now-default query decomposition was validated
    # exclusively on v2 (the unmeasured qd+v1 combination retrieves 71 vs 75
    # head gold because v1 lacks the pre-hydration vector floor).
    self_fusion_v2_enabled: bool = True
    self_fusion_keyword_min_norm: float = 0.25
    # Multi-evidence retrieval (Phase 2): two-hop typed graph expansion kept
    # out of rank fusion, one protected partner-evidence injection slot
    # (cosine noise filter + lexical-led ranking), bounded evidence-chain
    # promotion for the remaining budget and a per-document diversity cap in
    # the final top-k.  The 2026-08-12 cross-document diagnosis found 36% of
    # gold chunks were never recalled (one-hop expansion cannot reach the
    # second edge of a cross-document path).  Improves evidence hit/coverage
    # on cross-document questions at extra graph latency; composite parity —
    # keep opt-in per KB.
    self_multi_evidence_enabled: bool = False
    # Bilateral query decomposition (P2, 2026-08-13): explicitly two-sided
    # questions ("在一项研究中…;另一项研究中…") retrieve each side with its
    # own dense vector and the final head guarantees one best hit per side.
    # The 39-question crossdoc baseline measured all-gold-docs hit at only
    # 0.49-0.54 while any-doc hit was 0.95+ — one averaged query vector
    # cannot surface the weaker side's document.  Default ON since
    # 2026-08-13: across 213 evaluated questions only genuinely two-sided
    # ones trigger (36/39 crossdoc, 2 statement-style multi-aspect — both
    # neutral-or-better), sub-question pairs are blocked by the
    # question-mark rule, untriggered queries pay one regex, and triggered
    # warm latency matches the non-decomposed baseline (420ms).
    self_query_decomposition_enabled: bool = True
    # Graph novel-frontier rescoring (2026-08-21): the uncorroborated graph
    # frontier is ranked by edge weight (= evidence-count popularity, hub-
    # biased) and its admitted chunks never survive fusion (measured 0
    # exclusive slots — pure co-signing).  With this flag the frontier is
    # re-ranked by query-side cosine computed from stored Milvus chunk
    # vectors (one filtered search, no extra embedding call), and the best
    # candidate above the similarity floor gets a protected tail slot
    # (victim = weakest single-core-path head entry).  REFUTED on the
    # journal-KB hard sets (2026-08-21, 58 questions): the 1-hop
    # uncorroborated frontier contains zero gold chunks — gold sits at 2-3
    # typed hops — and query cosine ranks same-topic neighbours above gold,
    # so injections displaced 17%-gold tail slots with 0%-gold picks.  Keep
    # off; useful only with a frontier drawn from multi-hop chain-critical
    # edges and a stronger prior than query cosine.
    self_graph_novel_rescore_enabled: bool = False
    self_graph_novel_rescore_pool: int = 32
    self_graph_novel_similarity_floor: float = 0.55
    self_graph_novel_inject_slots: int = 1
    # PPR evidence discovery (2026-08-21, deep-retrieval channel): typed
    # 3-hop subgraph PPR with verbatim-linked entities as personalization
    # (3.0) over BM25 seeds (0.1), uniform edge weights (the edge weight =
    # evidence-count semantics measurably exclude gold), chunk score = min
    # of endpoint PageRank, top pool judged by the full-text LLM evidence
    # judge, best pick swapped into one protected tail slot.  Journal-KB
    # 58-question accounting: blind 1-hop frontier 0% gold -> tuned PPR pool
    # 39.7% gold-bearing, judge precision@1 17.2% vs 17% displaced-tail
    # gold — first configuration to cross the injection break-even line.
    # Costs one Nebula subgraph fetch + one judge LLM call per request.
    # Production wiring: the per-request deep-retrieval toggle composes
    # this channel with the boundary judge (96-q five-arm, 2026-08-21:
    # judge-only +0.004, PPR-only +0.0085, combined +0.0230 super-additive,
    # residual unsaturated questions +0.05+, latency ~1.7s -> ~8.5s).  This
    # global default stays off so normal requests are untouched.
    self_ppr_evidence_enabled: bool = False
    self_ppr_evidence_pool: int = 20
    self_ppr_linked_weight: float = 3.0
    self_ppr_seed_weight: float = 0.1
    self_ppr_damping: float = 0.85
    self_ppr_edge_limit: int = 20000
    self_ppr_judge_max_picks: int = 1
    # Link-count gate attempt (2026-08-21), refuted by experiment: the
    # offline "0-anchor hurts" population was an artifact of seed-restricted
    # linking — production ``_link_query_entities`` matches the full entity
    # catalog verbatim and links >=1 entity on 96/96 eval questions, so the
    # min_linked=1 gate barely fires (46 -> 45 injections) and 1-linked
    # questions actually gain the most (+0.038 vs +0.007 for 2+).  Linked
    # count is not a usable gate feature; the default keeps a zero-anchor
    # guard only as a cheap safety net for queries that link nothing at
    # all (PPR would then personalize on weak BM25 seeds alone).
    self_ppr_min_linked: int = 1
    # P3 (2026-08-13): stored relation evidence is citation-frequency ranked
    # by the normalizer and capped here (0 = unlimited).  Retrieval probes
    # showed same-edge sibling evidence chunks indistinguishable at query
    # time; ranking must happen at ingestion where citation counts exist.
    # Takes effect for newly ingested/re-ingested documents only.
    self_relation_evidence_max_chunks: int = 8
    # Query-entity linking (2026-08-13): entity-ANN candidates whose catalog
    # name appears verbatim in the question are force-ranked to the front of
    # the graph seed frontier.  The three-hop chain probe showed gold chain
    # entities never entering the BM25-derived seeds (0/16) even when the
    # question spells their names, leaving every graph mechanism walking
    # from the wrong start.  Default off until the ablation verdict lands.
    self_entity_linking_enabled: bool = False
    # Full-text LLM boundary judge (deep retrieval, 2026-08-13): a local
    # chat model reads 1200-char full candidate content and reorders head
    # positions keep_head..top_k+band_extra by evidence-ness.  Excerpt-level
    # variants (320 chars) measured no gain; full text cracked the
    # fused-order wall (two-hop head 78->82, three-hop 28->32, RAGAS
    # composite 0.674->0.706) at ~+1.7s retrieval latency.  Opt-in per
    # deployment; empty model name falls back to the default chat model.
    self_boundary_judge_enabled: bool = False
    self_boundary_judge_model: str = "qwen3.6-35b"
    self_boundary_judge_keep_head: int = 4
    # 2026-08-13 pool ablation: with the judge active, widening the
    # hydration pool to 36 and letting the band cover it converts
    # hydration-cut gold into head gold (two-hop 79->82, three-hop 33->35);
    # pool 48 / band 38 regresses (judge drowns in same-topic neighbours).
    # Expansion without the judge stays flat, so the wider pool applies
    # only while the judge runs.
    self_boundary_judge_band_extra: int = 26
    self_boundary_judge_excerpt_chars: int = 1200
    # Only the first few judged passages can reach the head, so an
    # unbounded evidence list pushes the real decision back onto fused
    # order.  Asking for at most N forces the judge to choose: two-hop
    # head gold 87/88/89 -> 92/90 and three-hop 77/77 -> 78/78, while a
    # cap of 4 is too tight (87) and 8 no better than 6 (91).
    self_boundary_judge_max_picks: int = 6
    self_boundary_judge_pool_limit: int = 36
    # Synthetic-query side index (doc2query, 2026-08-13 P7).  The
    # hypothesis probe on vocabulary-mismatch gold (absent from dense
    # top-200 under every query vector) showed separate synthetic-query
    # vectors rescue 9/24 while concatenating them into the chunk text
    # rescues 0/24.  Retrieval reads the side collection only when enabled
    # AND the KB's synq collection exists.
    self_synth_query_enabled: bool = False
    # Generate the questions during ingestion so a new document is
    # searchable through path-F without an offline backfill.  Runs as a
    # best-effort commit side stage; existing corpora still need
    # backfill_synth_queries.py once.
    self_synth_query_ingest_enabled: bool = False
    self_synth_query_model: str = "qwen3.6-35b"
    self_synth_query_count: int = 5
    self_synth_query_search_limit: int = 12
    # Side hits form their own fusion channel (path-F) instead of joining
    # the dense channel: folding them into path-C either re-ranked it
    # (three-hop head gold 35->29) or spent its pool budget (two-hop
    # 91->84).  The RRF weight bounds how many slots path-F may win.
    self_synth_query_rrf_weight: float = 0.5
    # Restricting path-F to chunks no other channel recalled erases its
    # benefit (two-hop head gold 88->83, outside the +-1 noise band of that
    # set): most of the gain is a better ORDER for already-recalled chunks,
    # because a synthetic question matches the user's question
    # symmetrically while a raw chunk does not.
    self_synth_query_exclusive_only: bool = False
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
    self_redis_bm25_cjk_max_ngram: int = 2
    self_redis_bm25_max_unique_tokens_per_chunk: int = 512
    self_redis_bm25_store_chunk_corpus: bool = False
    milvus_visibility_batch_size: int = 200
    milvus_insert_batch_size: int = 100
    milvus_rpc_timeout_seconds: float = 30.0
    milvus_force_flush: bool = False
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

    nebula_host: str = "202.115.138.101"
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
