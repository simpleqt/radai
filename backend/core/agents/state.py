"""Agent state definitions for LangGraph-based agent graphs.

Project-local thread state schema (pure data types, no I/O).
"""

from __future__ import annotations

from typing import Annotated
from typing_extensions import NotRequired, TypedDict

from langchain.agents import AgentState


class ThreadDataState(TypedDict):
    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class ViewedImageData(TypedDict):
    base64: str
    mime_type: str


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None:
        return new or []
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(
    existing: dict[str, ViewedImageData] | None,
    new: dict[str, ViewedImageData] | None,
) -> dict[str, ViewedImageData]:
    if existing is None:
        return new or {}
    if new is None:
        return existing
    if len(new) == 0:
        return {}
    return {**existing, **new}


def merge_loaded_skills(
    existing: dict[str, str] | None,
    new: dict[str, str] | None,
) -> dict[str, str]:
    if existing is None:
        return new or {}
    if new is None:
        return existing
    return {**existing, **new}


def merge_retrieved_evidence(
    existing: list[str] | None,
    new: list[str] | None,
) -> list[str]:
    if existing is None:
        return new or []
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


class ThreadState(AgentState):
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: NotRequired[list | None]
    uploaded_files: NotRequired[list[dict] | None]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    research_workflow: NotRequired[dict | None]
    clarification: NotRequired[dict | None]
    loaded_skills: Annotated[dict[str, str], merge_loaded_skills]
    retrieved_evidence: Annotated[list[str], merge_retrieved_evidence]
