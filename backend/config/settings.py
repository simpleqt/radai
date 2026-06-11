"""Composed application configuration with flat-access compatibility."""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings_sections import (
    AuthSettings,
    ModelSettings,
    RagSettings,
    ServerSettings,
    StorageSettings,
)


def normalize_milvus_uri(uri: str | None) -> str | None:
    """Normalize Milvus URIs to a pymilvus-compatible scheme."""
    if not uri:
        return None
    value = uri.strip()
    if not value:
        return None
    if value.startswith("grpc://"):
        return "tcp://" + value[len("grpc://"):]
    if value.startswith("grpcs://"):
        return "tcp://" + value[len("grpcs://"):]
    if "://" not in value:
        return f"tcp://{value}"
    return value


class Settings(BaseModel):
    """Top-level composition of domain settings.

    New code should prefer grouped access such as ``settings.storage.redis_host``.
    Flat access such as ``settings.redis_host`` remains supported while callers
    migrate incrementally.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    models: ModelSettings = Field(default_factory=ModelSettings)
    rag: RagSettings = Field(default_factory=RagSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)

    _section_types: ClassVar[dict[str, type[BaseModel]]] = {
        "models": ModelSettings,
        "rag": RagSettings,
        "storage": StorageSettings,
        "auth": AuthSettings,
        "server": ServerSettings,
    }
    _field_owners: ClassVar[dict[str, str]] = {
        field_name: section_name
        for section_name, section_type in _section_types.items()
        for field_name in section_type.model_fields
    }

    def __init__(self, **data: Any) -> None:
        """Accept both grouped overrides and legacy flat keyword overrides."""
        grouped: dict[str, BaseModel] = {}
        remaining = dict(data)
        for section_name, section_type in self._section_types.items():
            supplied = remaining.pop(section_name, None)
            if isinstance(supplied, section_type):
                grouped[section_name] = supplied
                continue

            overrides = dict(supplied or {}) if isinstance(supplied, dict) else {}
            for field_name in section_type.model_fields:
                if field_name in remaining:
                    overrides[field_name] = remaining.pop(field_name)
            grouped[section_name] = section_type(**overrides)
        super().__init__(**grouped, **remaining)

    def __getattr__(self, name: str) -> Any:
        try:
            return super().__getattr__(name)
        except AttributeError:
            section_name = self._field_owners.get(name)
            if section_name is None:
                raise
            return getattr(super().__getattribute__(section_name), name)

    def __setattr__(self, name: str, value: Any) -> None:
        section_name = self._field_owners.get(name)
        if section_name is not None and name not in self.__class__.model_fields:
            setattr(super().__getattribute__(section_name), name, value)
            return
        super().__setattr__(name, value)

    def model_post_init(self, __context: Any) -> None:
        if self.models.max_tokens is None:
            from backend.core.models.context_limits import resolve_max_tokens

            self.models.max_tokens = resolve_max_tokens(self.active_llm_config)

    @property
    def log_dir_path(self) -> str:
        backend_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
        if not os.path.isabs(self.server.log_dir):
            log_dir = self.server.log_dir
            if log_dir.startswith("backend/"):
                log_dir = log_dir[len("backend/"):]
            return os.path.join(backend_dir, log_dir)
        return self.server.log_dir

    @property
    def log_archive_dir(self) -> str:
        return os.path.join(self.log_dir_path, "archive")

    @property
    def log_temp_dir(self) -> str:
        return os.path.join(self.log_dir_path, "temp")

    @property
    def milvus_connection_uri(self) -> str | None:
        return normalize_milvus_uri(self.storage.milvus_uri)

    @property
    def milvus_connection_db_name(self) -> str | None:
        db_name = (self.storage.milvus_db_name or "").strip()
        if not db_name or db_name.lower() == "default":
            return None
        return db_name

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+pymysql://{self.storage.mysql_user}:{self.storage.mysql_password}"
            f"@{self.storage.mysql_host}:{self.storage.mysql_port}/"
            f"{self.storage.mysql_database}?charset=utf8mb4"
        )

    @property
    def async_mysql_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.storage.mysql_user}:{self.storage.mysql_password}"
            f"@{self.storage.mysql_host}:{self.storage.mysql_port}/"
            f"{self.storage.mysql_database}?charset=utf8mb4"
        )

    def _enrich_llm_config(self, config: dict) -> dict:
        from backend.core.models.context_limits import enrich_llm_config

        return enrich_llm_config(config)

    def _provider_config(self, provider: str) -> dict:
        model = self.models
        if provider == "deepseek":
            return {
                "api_key": model.deepseek_api_key or model.openai_api_key,
                "base_url": model.deepseek_api_base,
                "model": model.deepseek_model,
            }
        if provider == "minimax":
            return {
                "api_key": model.minimax_api_key,
                "base_url": model.minimax_api_base,
                "model": model.minimax_model,
            }
        if provider == "zhipu":
            return {
                "api_key": model.zhipu_api_key,
                "base_url": model.zhipu_api_base,
                "model": model.zhipu_model,
            }
        if provider == "glm4_flash":
            return {
                "api_key": model.glm4_flash_api_key,
                "base_url": model.glm4_flash_api_base,
                "model": model.glm4_flash_model,
            }
        if provider == "qwen":
            return {
                "api_key": model.dashscope_api_key,
                "base_url": model.dashscope_api_base,
                "model": model.dashscope_model,
            }
        return {
            "api_key": model.openai_api_key,
            "base_url": model.openai_api_base,
            "model": model.openai_model,
        }

    @property
    def active_llm_config(self) -> dict:
        return self._enrich_llm_config(
            self._provider_config(self.models.llm_provider)
        )

    @property
    def ingestion_llm_config(self) -> dict:
        return self._enrich_llm_config(
            self._provider_config(self.models.ingestion_llm_provider)
        )

    @property
    def active_vlm_config(self) -> dict:
        return {
            "api_key": self.models.vlm_api_key,
            "base_url": self.models.vlm_api_base,
            "model": self.models.vlm_model or "glm-4.6v",
            "max_length": self.models.vlm_max_length or 16384,
        }

    @property
    def embedding_config(self) -> dict:
        return {
            "api_key": self.models.embedding_api_key,
            "api_base": self.models.embedding_api_base,
            "model": self.models.embedding_model,
            "max_token_size": self.models.embedding_max_token_size or 8192,
            "dim": self.models.embedding_dim,
        }

    @property
    def vl_embedding_config(self) -> dict:
        return {
            "api_base": self.models.vl_embedding_api_base,
            "model": self.models.vl_embedding_model,
        }

    @property
    def rerank_config(self) -> dict:
        return {
            "api_key": self.models.rerank_api_key,
            "api_base": self.models.rerank_api_base,
            "model": self.models.rerank_model,
            "max_length": self.models.rerank_max_length,
        }

    @property
    def rag_ingestion_config(self) -> dict:
        rag = self.rag
        return {
            "llm": self.ingestion_llm_config,
            "vlm": self.active_vlm_config,
            "embedding": self.embedding_config,
            "vl_embedding": self.vl_embedding_config,
            "rerank": self.rerank_config,
            "concurrency": {
                "max_concurrent_files": rag.max_concurrent_files or 5,
                "max_concurrent_entity_batches": rag.max_concurrent_entity_batches or 5,
                "max_concurrent_entity_nodes": rag.max_concurrent_entity_nodes or 5,
                "llm_model_max_async": rag.llm_model_max_async or 5,
                "entity_extraction_batch_size": rag.entity_extraction_batch_size or 5,
                "max_concurrent_text_processing": rag.max_concurrent_text_processing or 20,
                "max_concurrent_vision_processing": rag.max_concurrent_vision_processing or 40,
            },
            "chunking": {
                "rag_chunk_token_size": rag.rag_chunk_token_size or 1000,
                "rag_chunk_overlap_token_size": rag.rag_chunk_overlap_token_size or 200,
                "embedding_batch_size": rag.embedding_batch_size or 200,
                "use_semantic_chunking": (
                    rag.use_semantic_chunking
                    if rag.use_semantic_chunking is not None
                    else True
                ),
                "semantic_chunking_threshold": rag.semantic_chunking_threshold or 95,
            },
            "multimodal": {
                "enable_image_processing": (
                    rag.enable_image_processing
                    if rag.enable_image_processing is not None
                    else True
                ),
                "enable_table_processing": (
                    rag.enable_table_processing
                    if rag.enable_table_processing is not None
                    else True
                ),
                "enable_equation_processing": (
                    rag.enable_equation_processing
                    if rag.enable_equation_processing is not None
                    else False
                ),
            },
            "timeout": {"llm_timeout": rag.llm_timeout or 900},
            "work_dir": rag.rag_work_dir,
        }

    def get_available_models(self) -> list[dict]:
        default_models = [
            {
                "id": "qwen3-max",
                "name": "Qwen3 Max",
                "provider": "qwen",
                "api_key": self.models.dashscope_api_key,
                "api_base": self.models.dashscope_api_base,
                "model": "qwen3-max",
                "description": "通义千问3 Max版本，阿里云DashScope",
            }
        ]
        if self.models.available_models:
            try:
                custom_models = json.loads(self.models.available_models)
                if isinstance(custom_models, list):
                    return custom_models
            except (json.JSONDecodeError, TypeError):
                pass
        return default_models

    def get_model_config(self, model_id: str) -> dict | None:
        from backend.core.models.context_limits import (
            enrich_llm_config,
            load_chat_llm_config_from_db,
        )

        db_config = load_chat_llm_config_from_db(name=model_id)
        if db_config:
            return enrich_llm_config(db_config)

        for model in self.get_available_models():
            if model.get("id") == model_id:
                return enrich_llm_config(
                    {
                        "name": model.get("id"),
                        "api_key": model.get("api_key"),
                        "base_url": model.get("api_base"),
                        "model": model.get("model"),
                        "provider": model.get("provider"),
                        "temperature": model.get("temperature"),
                    }
                )
        return None


settings = Settings()
