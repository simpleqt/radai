from pathlib import Path
import ast

from backend.application.agent_workflows.adaptive_scheduler import select_next_wave
from backend.application.tools.result_metadata import parse_tool_result_metadata
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy
from backend.domain.knowledge_base import LEGACY_BACKEND
from backend.domain.knowledge_base.catalog import KnowledgeBaseCatalogEntry


class _Task:
    def __init__(
        self,
        key: str,
        priority: int,
        dependencies: tuple[str, ...] = (),
        budget: int = 1,
    ):
        self.key = key
        self.priority = priority
        self.dependencies = dependencies
        self.budget = budget


def test_scheduler_decision_is_independent_from_workflow_execution():
    tasks = [
        (1, _Task("base", 10)),
        (2, _Task("dependent", 100, ("base",))),
    ]
    first = select_next_wave(
        tasks,
        known_keys={"base", "dependent"},
        terminal_keys=set(),
        capacity=2,
    )
    assert [task.key for _index, task in first.wave] == ["base"]
    assert first.dependency_fallback is False


def test_scheduler_budget_does_not_consume_parallel_executor_slots():
    tasks = [
        (index, _Task(f"t{index}", 100 - index, budget=5)) for index in range(1, 5)
    ]

    decision = select_next_wave(
        tasks,
        known_keys={task.key for _index, task in tasks},
        terminal_keys=set(),
        capacity=4,
    )

    assert [task.key for _index, task in decision.wave] == ["t1", "t2", "t3", "t4"]


def test_scheduler_launches_dependent_after_upstream_reaches_failed_terminal_state():
    tasks = [
        (1, _Task("dependent", 100, ("failed-base",))),
    ]

    decision = select_next_wave(
        tasks,
        known_keys={"failed-base", "dependent"},
        terminal_keys={"failed-base"},
        capacity=2,
    )

    assert [task.key for _index, task in decision.wave] == ["dependent"]
    assert decision.dependency_fallback is False


def test_scheduler_reports_dependency_cycle_when_no_task_can_progress():
    tasks = [
        (1, _Task("a", 100, ("b",))),
        (2, _Task("b", 90, ("a",))),
    ]

    decision = select_next_wave(
        tasks,
        known_keys={"a", "b"},
        terminal_keys=set(),
        capacity=2,
    )

    assert decision.wave == []
    assert decision.dependency_fallback is True


def test_retrieval_runtime_policy_centralizes_tool_arguments():
    policy = RetrievalRuntimePolicy.for_mode(deep_research=True)
    assert policy.kb_tool_args("q") == {
        "query": "q",
        "top_k": 8,
        "chunk_top_k": 5,
        "merge_strategy": "rrf",
        "max_result_chars": 6500,
    }
    assert policy.tool_budgets()["multi_kb_search"] == 3
    assert policy.tool_budgets()["web_search"] == 5
    assert policy.tool_budgets()["read_context_artifact"] == 1


def test_context_runtime_components_are_constructed_only_by_context_engine():
    """Prevent full, lightweight and writer paths from drifting again."""

    component_names = {
        "ContextCaptureRegistry",
        "ContextLedger",
        "HistoryCompactor",
        "ResultSink",
    }
    allowed = Path("backend/core/agents/context/engine.py")
    offenders: list[str] = []
    for path in Path("backend").rglob("*.py"):
        if path == allowed or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in component_names:
                offenders.append(f"{path}:{node.lineno}:{node.func.id}")

    assert offenders == []


def test_tool_result_metadata_hides_string_protocol_from_stream_handler():
    metadata = parse_tool_result_metadata(
        "multi_kb_search",
        "<target_document_match>false</target_document_match>知识库检索次数已达上限",
    )
    assert metadata.target_document_match is False
    assert metadata.budget_exhausted is True


def test_knowledge_base_catalog_contract_has_no_fixed_runtime_ids():
    entry = KnowledgeBaseCatalogEntry(
        kb_id="kb-1",
        kb_code="dynamic",
        kb_name="Dynamic KB",
        owner_id=None,
        is_default=True,
        status="active",
    )
    assert entry.kb_name == "Dynamic KB"
    source = Path("backend/domain/knowledge_base/catalog.py").read_text(
        encoding="utf-8"
    )
    assert "DEFAULT_KB_ID" not in source


def test_knowledge_base_backend_policy_lives_in_domain_layer():
    assert LEGACY_BACKEND == "legacy_lightrag"
    source = Path("backend/domain/knowledge_base/backend_policy.py").read_text(
        encoding="utf-8"
    )
    forbidden_imports = [
        "backend.api",
        "backend.application",
        "backend.db",
        "backend.rag",
        "backend.services",
        "backend.storage",
    ]
    offenders = [item for item in forbidden_imports if item in source]
    assert offenders == []


def test_database_layer_does_not_import_rag_runtime():
    offenders = []
    for path in Path("backend/db").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "backend.rag" in source:
            offenders.append(str(path))
    assert offenders == []


def test_database_layer_does_not_import_shared_services():
    offenders = []
    for path in Path("backend/db").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "backend.services" in source:
            offenders.append(str(path))
    assert offenders == []


def test_shared_services_do_not_import_api_layer():
    offenders = []
    for path in Path("backend/services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "backend.api" in source:
            offenders.append(str(path))
    assert offenders == []


def test_rag_api_router_does_not_own_retrieval_pipeline_helpers():
    source = Path("backend/api/routers/rag.py").read_text(encoding="utf-8")
    forbidden_symbols = [
        "def _ensure_query_type",
        "def _compute_stage1_recall_window",
        "def _clamp_filtered_chunks",
        "def _legacy_extract_keywords",
        "def _stage1_recall",
        "def _stage1_direct",
        "def _get_embedding_func_for_kb",
        "def _legacy_retrieve_context",
        "def _extract_images_from_chunks",
    ]
    offenders = [symbol for symbol in forbidden_symbols if symbol in source]
    assert offenders == []


def test_rag_api_router_uses_application_retrieval_facade():
    source = Path("backend/api/routers/rag.py").read_text(encoding="utf-8")
    forbidden_imports = [
        "backend.rag",
        "backend.db.connection",
        "backend.db.models.llm_model",
        "from sqlalchemy import select",
    ]
    offenders = [item for item in forbidden_imports if item in source]
    assert offenders == []


def test_retrieval_facade_has_no_eager_rag_runtime_imports():
    tree = ast.parse(
        Path("backend/application/retrieval/query_service.py").read_text(
            encoding="utf-8"
        )
    )
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name.startswith("backend.rag")
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "backend.rag"
        ):
            offenders.append(node.module or "")
    assert offenders == []


def test_guest_entrypoint_does_not_import_rag_api_router_for_prewarm():
    source = Path("backend/api/guest_http_server.py").read_text(encoding="utf-8")
    assert "backend.api.routers.rag" not in source


def test_skill_api_router_does_not_import_package_storage_adapter():
    source = Path("backend/api/routers/skills.py").read_text(encoding="utf-8")
    assert "backend.services.skill_package_storage" not in source


def test_skill_api_router_does_not_import_access_policy_service():
    source = Path("backend/api/routers/skills.py").read_text(encoding="utf-8")
    assert "backend.application.skills.access_service" not in source


def test_application_layer_does_not_import_rag_api_router():
    offenders = []
    for path in Path("backend/application").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "backend.api.routers.rag" in source:
            offenders.append(str(path))
    assert offenders == []


def test_application_layer_does_not_import_api_routers():
    offenders = []
    for path in Path("backend/application").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "backend.api.routers" in source:
            offenders.append(str(path))
    assert offenders == []


def test_runtime_does_not_embed_production_knowledge_base_ids():
    fixed_ids = {
        "c056bf59-6091-45ed-98cb-e2e860580a69",
        "de4ccde7-b286-4e49-bd8c-9dd37bd61878",
        "b4c3f145-de5f-42fb-88de-2d4856d57460",
        "7e2975ec-8cf4-46c8-b782-0413d64c8a58",
    }
    roots = [
        Path("backend/api"),
        Path("backend/application"),
        Path("backend/core"),
        Path("backend/domain"),
        Path("backend/rag"),
        Path("backend/config/agents"),
    ]
    offenders = []
    for root in roots:
        for path in (*root.rglob("*.py"), *root.rglob("*.yaml")):
            source = path.read_text(encoding="utf-8")
            if any(kb_id in source for kb_id in fixed_ids):
                offenders.append(str(path))
    assert offenders == []
