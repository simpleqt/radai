"""CRUD 模块"""

from backend.db.crud.document_crud import DocumentCRUD, document_crud
from backend.db.crud.extraction_crud import ExtractionCRUD, extraction_crud
from backend.db.crud.task_crud import TaskCRUD, task_crud
from backend.db.crud.file_info_crud import FileInfoCRUD, get_file_info_crud
from backend.db.crud.knowledge_base_crud import (
    KnowledgeBaseCRUD,
    KnowledgeBaseDocumentCRUD,
    knowledge_base_crud,
    kb_document_crud,
)

__all__ = [
    "document_crud",
    "DocumentCRUD",
    "task_crud",
    "TaskCRUD",
    "extraction_crud",
    "ExtractionCRUD",
    "FileInfoCRUD",
    "get_file_info_crud",
    "KnowledgeBaseCRUD",
    "KnowledgeBaseDocumentCRUD",
    "knowledge_base_crud",
    "kb_document_crud",
]
