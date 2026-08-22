import json
import time


from backend.application.tools.providers.context_read_tool import (
    capability_ledger_scope,
    context_read,
    get_capability_ledger,
    install_request_capability_ledger,
    uninstall_request_capability_ledger,
)
from backend.core.agents.context.artifact_store import (
    ArtifactStore,
    context_origin_scope_matches,
)
from backend.core.agents.context.capability_ledger import CapabilityLedger
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import (
    make_context_object,
    make_segment,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.context.projection import ProjectionCompiler
from backend.core.agents.context.segmenters import MultiKbSegmenter
from backend.core.agents.context.state_adapter import ContextStateAdapter
from backend.core.agents.state import context_object_ref_cap, merge_context_objects


def _sample_segments():
    return [
        make_segment(
            sid="kb_378:c11",
            title="378_Role of Diffusion",
            locator="p.7-8",
            text="装置示意：3cm 砂层 / 9cm 黏土层。" + ("正文补充 " * 50),
            score=0.83,
        ),
        make_segment(
            sid="kb_378:c12",
            title="378_Role of Diffusion",
            locator="p.9",
            text="另一个高分段正文。" + ("更多内容 " * 40),
            score=0.91,
        ),
    ]


def test_context_object_content_addressing_is_stable_and_store_roundtrips(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    first = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "thread_id": "t1"},
        segments=_sample_segments(),
    )
    second = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "thread_id": "t1"},
        segments=_sample_segments(),
    )

    assert first["oid"] == second["oid"]
    oid = store.put(first)

    assert oid == first["oid"]
    loaded = store.read(oid)
    assert loaded is not None
    assert loaded["oid"] == oid
    assert [segment["sid"] for segment in store.read_segments(oid)] == [
        "kb_378:c11",
        "kb_378:c12",
    ]
    assert store.read_segments(oid, ["kb_378:c12"])[0]["sid"] == "kb_378:c12"


def test_context_object_store_scan_and_gc(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    oid = store.put(
        make_context_object(
            schema="text/v1",
            origin={"tool": "x", "thread_id": "t1"},
            segments=[make_segment(sid="a:1", text="hello")],
        )
    )

    entries = store.scan()
    assert len(entries) == 1
    assert entries[0]["oid"] == oid

    report = store.gc(ttl_seconds=1, now=time.time() + 2)
    assert oid in report["removed"]
    assert store.read(oid) is None


def test_capability_ledger_handles_are_per_turn_and_dedup_segments():
    first_turn = CapabilityLedger(turn_id="turn-a")
    second_turn = CapabilityLedger(turn_id="turn-b")
    handle = first_turn.mint("oid-1", ["kb_378:c11"])

    assert first_turn.resolve(handle) is not None
    assert second_turn.resolve(handle) is None
    assert first_turn.resolve(handle).allows("kb_378:c11")  # type: ignore[union-attr]
    assert not first_turn.resolve(handle).allows("kb_378:c12")  # type: ignore[union-attr]
    assert first_turn.resolve_sid("kb_378:c11") is not None
    assert first_turn.resolve_sid("kb_378:c12") is None
    assert second_turn.resolve_sid("kb_378:c11") is None

    obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search"},
        segments=_sample_segments(),
    )
    first = first_turn.project([obj], budget_tokens=100000)
    assert first is not None and first.tokens_spent > 0
    second = first_turn.project([obj], budget_tokens=100000)
    assert second is not None and "重复片段见上文" in second.rendered
    assert first_turn.accounting()["navigator"]["dedup_hits"] > 0
    assert first_turn.accounting()["duplicate_segment_rate"] == 0.0


def test_ledger_rows_are_stable_and_sid_addressing_works(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "user_id": "u1", "thread_id": "t1"},
        segments=_sample_segments(),
    )
    obj["oid"] = store.put(obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        object_store=store,
        user_id="u1",
        thread_id="t1",
    )
    first = ledger.project([obj], budget_tokens=100000)
    assert first is not None
    # Score ordering: c12 (0.91) gets [1], c11 (0.83) gets [2].
    assert ledger.citation_number("kb_378:c12") == 1
    assert ledger.citation_number("kb_378:c11") == 2
    ledger.project([obj], budget_tokens=100000)
    assert ledger.citation_number("kb_378:c12") == 1
    assert ledger.citation_number("kb_378:c11") == 2
    rows = ledger.ledger_rows()
    assert [row["n"] for row in rows] == [1, 2]
    assert all(str(row["sid"]) for row in rows)

    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        result = context_read.invoke({"sids": ["kb_378:c11"], "max_tokens": 2000})
    finally:
        reset_request_capability_ledger(token, "")
    assert "正文补充" in result


def test_citation_numbers_do_not_reorder_after_publication(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    first_obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "user_id": "u1", "thread_id": "t1"},
        segments=[
            make_segment(sid="a:1", title="文档 A", text="正文 A", score=0.5),
            make_segment(sid="a:2", title="文档 A", text="正文 A2", score=0.4),
        ],
    )
    first_obj["oid"] = store.put(first_obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        object_store=store,
        user_id="u1",
        thread_id="t1",
    )
    ledger.project([first_obj], budget_tokens=100000)
    compiler = ProjectionCompiler()
    block, _cost = compiler.render_ledger(
        ledger.ledger_rows(),
        budget_tokens=100000,
        dynamic_budget=100000,
    )
    assert block
    ledger.mark_citation_numbers_published()

    second_obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="b:1", title="文档 B", text="正文 B", score=0.99)],
    )
    second_obj["oid"] = store.put(second_obj)
    ledger.register_objects([second_obj])

    assert ledger.citation_number("a:1") == 1
    assert ledger.citation_number("a:2") == 2
    assert ledger.citation_number("b:1") == 3
    ledger.reassign_citation_numbers()
    assert ledger.citation_number("a:1") == 1
    assert ledger.citation_number("a:2") == 2
    assert ledger.citation_number("b:1") == 3


def test_ledger_tokens_count_against_token_pool():
    ledger = CapabilityLedger(token_pool=1000)
    before = ledger.remaining_tokens
    ledger.record_ledger_tokens(120)
    assert ledger.remaining_tokens == before - 120
    assert ledger.accounting()["ledger_tokens"] == 120
    assert ledger.accounting()["tokens_spent"] == 120


def test_multi_kb_segmenter_sid_stability():
    segmenter = MultiKbSegmenter()
    payload = {
        "evidence_records": [
            {
                "source_id": "kb_378",
                "chunk_id": "c11",
                "content": "正文 A",
                "score": 0.8,
            }
        ]
    }
    first = segmenter.segment(
        payload,
        tool_args={},
        thread_id="t1",
        turn_id="turn-1",
        user_id="u1",
    )
    second = segmenter.segment(
        payload,
        tool_args={},
        thread_id="t1",
        turn_id="turn-2",
        user_id="u1",
    )
    assert first["oid"] == second["oid"]
    assert first["segments"][0]["sid"] == "kb_378:c11"


def test_state_adapter_promotes_legacy_shapes():
    policy = ContextPolicy.from_env(None)
    adapter = ContextStateAdapter(policy)
    objects = adapter.context_objects(
        {
            "context_records": [
                {
                    "id": "ev_1",
                    "source_tool": "multi_kb_search",
                    "content": "legacy evidence",
                }
            ],
            "loaded_skills": {"hydro": "skill rules"},
            "retrieved_evidence": ["### 片段 1（来自：kb）\n内容"],
            "project_evidence": ["project rows"],
            "project_bootstrap": {
                "project_id": "p1",
                "data_version": "v1",
                "tables": {},
                "table_routes": {},
            },
        }
    )
    schemas = {obj["schema"] for obj in objects}
    assert "legacy/record/v1" in schemas
    assert "skill/v1" in schemas
    assert "legacy/retrieval/v1" in schemas
    assert "legacy/project/v1" in schemas
    assert "contract/v1" in schemas


def test_context_read_rejects_object_owned_by_another_user(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "t", "user_id": "user-b", "thread_id": "thread-1"},
        segments=[make_segment(sid="a:1", text="secret for user-b")],
    )
    oid = store.put(obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        object_store=store,
        user_id="user-a",
        thread_id="thread-1",
    )
    handle = ledger.mint(oid, ["a:1"])
    from backend.application.tools.providers.context_read_tool import (
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        result = context_read.invoke(
            {"handle": handle, "sids": ["a:1"], "max_tokens": 2000}
        )
    finally:
        from backend.application.tools.providers.context_read_tool import (
            reset_request_capability_ledger,
        )

        reset_request_capability_ledger(token, "")
    assert "artifact_access_denied" in result


def test_context_read_sid_failure_includes_ledger_and_no_research_instruction(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "t", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="正文 A")],
    )
    obj["oid"] = store.put(obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        object_store=store,
        user_id="u1",
        thread_id="t1",
    )
    ledger.register_objects([obj])
    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        result = context_read.invoke(
            {"sids": ["kb_999:not_granted"], "max_tokens": 2000}
        )
    finally:
        reset_request_capability_ledger(token, "")
    assert "sid_not_granted" in result
    # 未授权 sid 默认仍禁止以重复检索绕过台账,但对"对象可能已过期"的
    # 情形保留一条有界的重新检索出路,不再是无条件死胡同。
    assert "禁止仅为换取同一 sid 重新检索" in result
    assert "已过期" in result
    assert "ledger_excerpt" in result
    assert '"sid": "a:1"' in result


class _CountingArtifactStore:
    """Wrap an ArtifactStore and count raw/metadata round trips."""

    def __init__(self, inner: ArtifactStore):
        self._inner = inner
        self.reads = 0
        self.metadata_reads = 0

    def store(self, content: str, **kwargs):
        return self._inner.store(content, **kwargs)

    def read(self, raw_ref: str | None, **kwargs):
        self.reads += 1
        return self._inner.read(raw_ref, **kwargs)

    def read_excerpt(self, raw_ref: str | None, **kwargs):
        self.reads += 1
        return self._inner.read_excerpt(raw_ref, **kwargs)

    def metadata(self, raw_ref: str | None):
        self.metadata_reads += 1
        return self._inner.metadata(raw_ref)

    def object_ref(self, oid: str, prefix: str = "context_object"):
        return self._inner.object_ref(oid, prefix=prefix)

    def scan_metadata(self):
        return self._inner.scan_metadata()

    def delete(self, raw_ref: str | None):
        return self._inner.delete(raw_ref)


def test_object_store_request_cache_avoids_repeated_storage_roundtrips(tmp_path):
    inner = ArtifactStore(tmp_path)
    counting = _CountingArtifactStore(inner)
    store = ContextObjectStore(counting)
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="hello cache")],
    )
    oid = store.put(obj)
    # put() backfills the request cache, so no storage round trip is needed.
    assert counting.reads == 0
    assert store.read(oid) is not None
    assert store.read(oid) is not None
    assert counting.reads == 0
    assert counting.metadata_reads == 0
    stats = store.stats()
    assert stats["objects_read"] == 0
    assert stats["object_cache_hits"] >= 2

    # A fresh store (e.g. a nested middleware instance) reads once and caches.
    fresh = ContextObjectStore(counting)
    assert fresh.read(oid) is not None
    assert counting.reads == 1
    assert counting.metadata_reads == 1
    assert fresh.read(oid) is not None
    assert counting.reads == 1
    assert counting.metadata_reads == 1


def test_object_store_put_refreshes_cached_origin(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    first = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1", "turn_id": "t1"},
        segments=[make_segment(sid="a:1", text="same body")],
    )
    oid = store.put(first)
    assert store.read(oid)["origin"]["turn_id"] == "t1"
    second = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1", "turn_id": "t2"},
        segments=[make_segment(sid="a:1", text="same body")],
    )
    assert second["oid"] == oid
    store.put(second)
    assert store.read(oid)["origin"]["turn_id"] == "t2"


def test_capability_ledger_scope_restores_outer_registry():
    # 其它单测直接调用中间件方法时会在 ContextVar 留下 unbound 台账
    # (生产路径由执行边界的 capability_ledger_scope 恢复,单测线程无此边界);
    # 本测试验证 scope 安装/恢复语义,先清空 ContextVar 基线再断言。
    from backend.application.tools.providers.context_read_tool import (
        _CAPABILITY_LEDGER,
    )

    _CAPABILITY_LEDGER.set(None)
    outer = CapabilityLedger(turn_id="outer")
    inner = CapabilityLedger(turn_id="inner")
    token = install_request_capability_ledger("req-scope", outer)
    try:
        with capability_ledger_scope("req-scope", inner):
            assert get_capability_ledger("req-scope") is inner
        assert get_capability_ledger("req-scope") is outer
    finally:
        uninstall_request_capability_ledger(token)
    assert get_capability_ledger("req-scope") is None


def test_context_read_reuses_request_cache(tmp_path):
    inner = ArtifactStore(tmp_path)
    counting = _CountingArtifactStore(inner)
    store = ContextObjectStore(counting)
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="hello cache navigator")],
    )
    store.put(obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        object_store=store,
        user_id="u1",
        thread_id="t1",
    )
    ledger.register_objects([obj])
    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        result = context_read.invoke({"sids": ["a:1"], "max_tokens": 2000})
    finally:
        reset_request_capability_ledger(token, "")
    assert "hello cache navigator" in result
    # put() backfilled the cache and the navigator reuses it: no extra round
    # trips to the underlying artifact store.
    assert counting.reads == 0
    assert counting.metadata_reads == 0


def test_context_read_dedups_repeated_sids_within_request(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[
            make_segment(sid="a:1", text="正文内容 " * 80, score=0.9)
        ],
    )
    obj["oid"] = store.put(obj)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        token_pool=100000,
        object_store=store,
        user_id="u1",
        thread_id="t1",
    )
    ledger.register_objects([obj])
    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        first = context_read.invoke({"sids": ["a:1"], "max_tokens": 2000})
        second = context_read.invoke({"sids": ["a:1"], "max_tokens": 2000})
    finally:
        reset_request_capability_ledger(token, "")
    first_payload = json.loads(first)
    second_payload = json.loads(second)
    assert "正文内容" in first_payload["content"]
    assert second_payload["deduped_sids"] == ["a:1"]
    assert "已读取（见上文）" in second_payload["content"]
    assert second_payload["tokens_spent"] < first_payload["tokens_spent"]
    assert ledger.accounting()["navigator"]["dedup_hits"] >= 1


def test_context_origin_scope_matches_fails_closed():
    assert context_origin_scope_matches(
        {"user_id": "u1", "thread_id": "t1"},
        user_id="u1",
        thread_id="t1",
    )
    assert not context_origin_scope_matches(
        {},
        user_id="u1",
        thread_id="t1",
    )
    assert not context_origin_scope_matches(
        {"user_id": "u2", "thread_id": "t1"},
        user_id="u1",
        thread_id="t1",
    )


def test_merge_context_objects_cap_and_last_seen_eviction(monkeypatch):
    monkeypatch.setenv("CONTEXT_OBJECT_REF_CAP", "10")
    objects = [
        make_context_object(
            schema="text/v1",
            origin={"tool": "x", "last_seen_at": float(index)},
            segments=[make_segment(sid=f"i:{index}", text="x")],
        )
        for index in range(12)
    ]
    merged = merge_context_objects([], objects)
    assert len(merged) == 10
    sids = {
        str(segment.get("sid") or "")
        for obj in merged
        for segment in obj.get("segments") or []
    }
    assert sids == {f"i:{index}" for index in range(2, 12)}
    assert context_object_ref_cap() == 10


# ---------------------------------------------------------------------------
# H1: COS 内容寻址去重的多属主授权（后写者不再夺走先前所有者）
# ---------------------------------------------------------------------------


def test_context_owner_allowed_matches_any_owner_and_rejects_others():
    from backend.core.agents.context.artifact_store import context_owner_allowed

    owners = [
        {"user_id": "u1", "thread_id": "tA"},
        {"user_id": "u1", "thread_id": "tB"},
    ]
    # 集合内任一属主放行
    assert context_owner_allowed({}, owners, user_id="u1", thread_id="tA")
    assert context_owner_allowed({}, owners, user_id="u1", thread_id="tB")
    # 非属主 fail-closed 拒绝
    assert not context_owner_allowed({}, owners, user_id="u1", thread_id="tC")
    assert not context_owner_allowed({}, owners, user_id="u2", thread_id="tA")
    # 向后兼容：owners 为空时回退到 origin（多属主追踪之前写入的旧对象）
    assert context_owner_allowed(
        {"user_id": "u1", "thread_id": "tA"}, [], user_id="u1", thread_id="tA"
    )
    assert not context_owner_allowed(
        {"user_id": "u1", "thread_id": "tA"}, [], user_id="u1", thread_id="tZ"
    )


def test_cos_dedup_keeps_all_owners_so_earlier_thread_can_still_read(tmp_path):
    from backend.core.agents.context.artifact_store import context_owner_allowed

    backend_store = ArtifactStore(tmp_path)

    # 线程 tA 检索到内容 X
    store_a = ContextObjectStore(backend_store)
    oid = store_a.put(
        make_context_object(
            schema="multi_kb_search/v1",
            origin={"tool": "multi_kb_search", "user_id": "u1", "thread_id": "tA"},
            segments=_sample_segments(),
        )
    )

    # 线程 tB 检索到逐字节相同的内容 X（同一 oid，覆盖同一 sidecar）
    store_b = ContextObjectStore(backend_store)
    assert (
        store_b.put(
            make_context_object(
                schema="multi_kb_search/v1",
                origin={
                    "tool": "multi_kb_search",
                    "user_id": "u1",
                    "thread_id": "tB",
                },
                segments=_sample_segments(),
            )
        )
        == oid
    )

    # 后续请求（全新 store，无 read cache）从 sidecar 读回属主集合
    loaded = ContextObjectStore(backend_store).read(oid)
    assert loaded is not None
    owners = list(loaded.get("owners") or [])
    origin = dict(loaded.get("origin") or {})

    # 先写线程 tA 仍是合法属主（修复前会被后写者 tB 覆盖而 fail-closed 拒绝）
    assert context_owner_allowed(origin, owners, user_id="u1", thread_id="tA")
    assert context_owner_allowed(origin, owners, user_id="u1", thread_id="tB")
    # 非属主仍被拒绝，fail-closed 未被削弱
    assert not context_owner_allowed(origin, owners, user_id="u1", thread_id="tX")
    assert not context_owner_allowed(origin, owners, user_id="u2", thread_id="tA")


def test_ledger_registry_survives_out_of_order_parallel_uninstall():
    """并行单元乱序卸载不得踩掉仍在运行的兄弟或复活已释放台账。"""

    parent = CapabilityLedger(turn_id="t0")
    unit_a = CapabilityLedger(turn_id="ta")
    unit_b = CapabilityLedger(turn_id="tb")
    token_parent = install_request_capability_ledger("req-1", parent)
    token_a = install_request_capability_ledger("req-1", unit_a)
    token_b = install_request_capability_ledger("req-1", unit_b)
    try:
        # A 先结束(与安装序相反):B 仍是最新安装,必须继续可解析。
        uninstall_request_capability_ledger(token_a)
        assert get_capability_ledger("req-1") is unit_b
        uninstall_request_capability_ledger(token_b)
        assert get_capability_ledger("req-1") is parent
    finally:
        uninstall_request_capability_ledger(token_parent)
    assert get_capability_ledger("req-1") is None


def test_ledger_registry_distinct_scope_keys_do_not_interfere():
    parent = CapabilityLedger(turn_id="t0")
    unit = CapabilityLedger(turn_id="tu")
    token_parent = install_request_capability_ledger("req-1::main", parent)
    token_unit = install_request_capability_ledger("req-1::unit-1", unit)
    try:
        assert get_capability_ledger("req-1::main") is parent
        assert get_capability_ledger("req-1::unit-1") is unit
        uninstall_request_capability_ledger(token_parent)
        # 父键卸载不影响单元键。
        assert get_capability_ledger("req-1::main") is None
        assert get_capability_ledger("req-1::unit-1") is unit
    finally:
        uninstall_request_capability_ledger(token_unit)


def test_scoped_context_read_resolves_by_context_scope_id(tmp_path):
    """子代理场景:工具带父 request_id,但必须按 scope id 命中子台账。"""

    from backend.application.tools.providers.context_read_tool import (
        get_scoped_context_read_tool,
    )
    from backend.domain.tools.runtime_context import ToolRuntimeContext

    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "t", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="子代理检索正文")],
    )
    obj["oid"] = store.put(obj)
    child = CapabilityLedger(
        turn_id="t1", object_store=store, user_id="u1", thread_id="t1"
    )
    child.register_objects([obj])
    parent = CapabilityLedger(
        turn_id="t0", object_store=store, user_id="u1", thread_id="t1"
    )
    token_parent = install_request_capability_ledger("req-1", parent)
    token_child = install_request_capability_ledger(
        "req-1::t1:subagent:task-9", child
    )
    try:
        tool = get_scoped_context_read_tool(
            ToolRuntimeContext(
                request_id="req-1",
                user_id="u1",
                agent_code="ne",
                domain="d",
                thread_id="t1",
                context_scope_id="req-1::t1:subagent:task-9",
            )
        )
        result = tool.invoke({"sids": ["a:1"], "max_tokens": 2000})
        # 修复前按父 request_id 命中父台账,该 sid 报 sid_not_granted。
        assert "子代理检索正文" in result
        assert "sid_not_granted" not in result
    finally:
        uninstall_request_capability_ledger(token_child)
        uninstall_request_capability_ledger(token_parent)


def test_context_read_reports_expired_object_instead_of_denial(tmp_path):
    """对象被 GC 后必须报 object_expired 并允许有界重检,而不是权限死胡同。"""

    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "t", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="将被清理的正文")],
    )
    oid = store.put(obj)
    obj["oid"] = oid
    ledger = CapabilityLedger(
        turn_id="t1", object_store=store, user_id="u1", thread_id="t1"
    )
    ledger.register_objects([obj])
    store.delete(oid)

    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        result = context_read.invoke({"sids": ["a:1"], "max_tokens": 2000})
    finally:
        reset_request_capability_ledger(token, "")
    payload = json.loads(result)
    assert payload["error"] == "object_expired"
    assert "重新发起一次知识库检索" in payload["instruction"]
