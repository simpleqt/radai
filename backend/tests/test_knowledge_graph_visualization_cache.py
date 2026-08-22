import inspect
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Annotated

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.api.routers import knowledge_base as kb_router
from backend.api.routers import knowledge_base_graph as graph_router


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.values[key] = value
        self.ttls[key] = ttl

    async def aclose(self):
        return None


def _kb(**overrides):
    values = {
        "kb_id": "kb-1",
        "nebula_space": "space-1",
        "redis_key_prefix": "kb:test",
        "entity_count": 100,
        "relation_count": 250,
        "updated_at": datetime(2026, 1, 1, 12, 0, 0),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_graph_route_accepts_frontend_visualization_limits():
    parameters = inspect.signature(kb_router.get_knowledge_base_graph).parameters

    for name, maximum in (("node_limit", 5000), ("edge_limit", 20000)):
        query = parameters[name].default
        constrained_int = Annotated[int, *query.metadata]

        assert TypeAdapter(constrained_int).validate_python(maximum) == maximum
        with pytest.raises(ValidationError):
            TypeAdapter(constrained_int).validate_python(maximum + 1)


def test_graph_cache_key_changes_with_graph_version_and_limits():
    base = graph_router._knowledge_graph_cache_key(_kb(), 1200, 4000)
    same = graph_router._knowledge_graph_cache_key(_kb(), 1200, 4000)
    newer = graph_router._knowledge_graph_cache_key(
        _kb(updated_at=datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=1)),
        1200,
        4000,
    )
    larger = graph_router._knowledge_graph_cache_key(_kb(), 2400, 8000)

    assert base == same
    assert base != newer
    assert base != larger
    assert base.startswith("kb:test:admin:knowledge-graph:v7:")


@pytest.mark.asyncio
async def test_graph_slice_round_trips_through_redis(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(graph_router.settings, "redis_host", "127.0.0.1")
    monkeypatch.setattr(graph_router.settings, "knowledge_graph_cache_ttl_seconds", 321)
    monkeypatch.setattr(graph_router, "get_redis", lambda: fake_redis)
    payload = {
        "nodes": [{"id": "n1", "label": "n1", "degree": 1}],
        "edges": [{"source": "n1", "target": "n2"}],
    }

    await graph_router._write_knowledge_graph_cache("graph-key", payload)
    cached = await graph_router._read_knowledge_graph_cache("graph-key")

    assert cached == payload
    assert fake_redis.ttls["graph-key"] == 321
    assert json.loads(fake_redis.values["graph-key"]) == payload


@pytest.mark.asyncio
async def test_legacy_graph_slice_falls_back_to_entity_sampling():
    class _Entity:
        entity_name = "Alpha"
        entity_type = "term"
        description = "node desc"
        source_id = "doc-1"
        file_path = "alpha.md"

    class _Manager:
        async def _fetch_entities_from_kb(self, kb):
            return [_Entity()]

    result = await graph_router._fetch_legacy_graph_slice(
        _Manager(),
        _kb(nebula_space=""),
        node_limit=20,
        edge_limit=0,
    )

    assert result["edges"] == []
    assert result["nodes"] == [
        {
            "id": "Alpha",
            "label": "Alpha",
            "entity_type": "term",
            "description": "node desc",
            "source_id": "doc-1",
            "file_path": "alpha.md",
            "degree": 0,
        }
    ]


class _NebulaValue:
    def __init__(self, value):
        self.value = value

    def is_empty(self):
        return self.value is None

    def is_string(self):
        return isinstance(self.value, str)

    def as_string(self):
        return self.value

    def is_int(self):
        return isinstance(self.value, int) and not isinstance(self.value, bool)

    def as_int(self):
        return self.value

    def is_double(self):
        return isinstance(self.value, float)

    def as_double(self):
        return self.value

    def is_bool(self):
        return isinstance(self.value, bool)

    def as_bool(self):
        return self.value


class _NebulaResult:
    def __init__(self, rows=None, succeeded=True):
        self.rows = rows or []
        self.succeeded = succeeded

    def is_succeeded(self):
        return self.succeeded

    def error_msg(self):
        return "query failed"

    def row_size(self):
        return len(self.rows)

    def row_values(self, index):
        return [_NebulaValue(value) for value in self.rows[index]]


class _FakeNebulaSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str):
        self.queries.append(query)
        if query.startswith("USE"):
            return _NebulaResult()
        if query.startswith("MATCH (src:entity)"):
            return _NebulaResult([["n1", "n2", "depends_on", "edge desc", "doc-1", 0.8]])
        if query.startswith("FETCH PROP ON entity"):
            return _NebulaResult(
                [
                    ["n1", "concept", "node one", "doc-1", "one.md"],
                    ["n2", "concept", "node two", "doc-1", "two.md"],
                ]
            )
        raise AssertionError(f"unexpected query: {query}")

    def release(self):
        return None


class _FakeConnectionPool:
    session = _FakeNebulaSession()

    def init(self, addresses, config):
        return True

    def get_session(self, user, password):
        return self.session

    def close(self):
        return None


def test_nebula_graph_query_fetches_vertex_properties_once(monkeypatch):
    import nebula3.gclient.net

    _FakeConnectionPool.session = _FakeNebulaSession()
    monkeypatch.setattr(nebula3.gclient.net, "ConnectionPool", _FakeConnectionPool)

    result = graph_router._fetch_knowledge_graph_from_nebula(_kb(), 2, 10)

    assert len(result["nodes"]) == 2
    assert result["nodes"][0]["degree"] == 1
    assert result["edges"] == [
        {
            "source": "n1",
            "target": "n2",
            "relation_type": "depends_on",
            "description": "edge desc",
            "source_id": "doc-1",
            "weight": 0.8,
        }
    ]
    edge_query = next(query for query in _FakeConnectionPool.session.queries if query.startswith("MATCH"))
    assert "src.entity.description" not in edge_query
    assert any(query.startswith("FETCH PROP ON entity") for query in _FakeConnectionPool.session.queries)


class _FakeSelfNebulaSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str):
        self.queries.append(query)
        if query.startswith("USE"):
            return _NebulaResult()
        if "MATCH (src)-[e:relation_v2]->(dst)" in query:
            return _NebulaResult(
                [
                    ["ent-a", "ent-b", "related_to", "edge desc", '["chk-1"]', 0.8],
                ]
            )
        if query.startswith("FETCH PROP ON entity_v2"):
            return _NebulaResult(
                [
                    [
                        "ent-a",
                        "Alpha",
                        "term",
                        "node desc",
                        "doc-1",
                        '["chk-1"]',
                    ],
                    [
                        "ent-b",
                        "Beta",
                        "term",
                        "node two",
                        "doc-1",
                        '["chk-2"]',
                    ],
                ]
            )
        if "MATCH (n)" in query and "entity_v2.entity_id" in query:
            return _NebulaResult()
        raise AssertionError(f"unexpected query: {query}")

    def release(self):
        return None


def test_self_v2_graph_uses_property_match_and_coalesce_edge_filter(monkeypatch):
    import nebula3.gclient.net

    fake_session = _FakeSelfNebulaSession()
    monkeypatch.setattr(
        nebula3.gclient.net,
        "ConnectionPool",
        type(
            "_Pool",
            (),
            {
                "init": staticmethod(lambda addresses, config: True),
                "get_session": staticmethod(lambda user, password: fake_session),
                "close": staticmethod(lambda: None),
            },
        ),
    )

    result = graph_router._fetch_self_graph_from_nebula_v2(_kb(), 10, 10)

    assert len(result["nodes"]) == 2
    assert result["edges"][0]["source"] == "ent-a"
    edge_query = next(q for q in fake_session.queries if "relation_v2" in q and q.startswith("MATCH"))
    assert "entity_v2)-[e" not in edge_query
    assert "coalesce(e.relation_v2.is_visible" in edge_query
