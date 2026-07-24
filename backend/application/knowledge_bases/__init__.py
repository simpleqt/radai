"""Knowledge-base application services."""

from backend.application.knowledge_bases.catalog_service import (
    load_knowledge_base_catalog,
    resolve_knowledge_base_labels,
)

__all__ = ["load_knowledge_base_catalog", "resolve_knowledge_base_labels"]
