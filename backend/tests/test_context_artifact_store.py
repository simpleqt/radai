from backend.core.agents.context.artifact_store import (
    ARTIFACT_URI_PREFIX,
    ArtifactStore,
    FallbackContextArtifactStore,
)


def test_context_artifact_store_writes_metadata_and_rejects_path_escape(tmp_path):
    store = ArtifactStore(tmp_path)

    raw_ref = store.store(
        "artifact payload",
        thread_id="../thread one",
        source_tool="tool/name",
        suffix="../json",
        attributes={"run_id": "run-1"},
    )

    assert raw_ref.startswith(ARTIFACT_URI_PREFIX)
    assert store.read(raw_ref, max_chars=8) == "artifact"
    assert store.read(f"{ARTIFACT_URI_PREFIX}../../etc/passwd") is None

    metadata = store.metadata(raw_ref)
    assert metadata is not None
    assert metadata.thread_id == "thread_one"
    assert metadata.source_tool == "tool_name"
    assert metadata.suffix == ".json"
    assert metadata.attributes["run_id"] == "run-1"


def test_context_artifact_store_reads_bounded_head_tail_excerpt(tmp_path):
    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "HEAD-" + ("x" * 2000) + "-TAIL",
        thread_id="thread-1",
        source_tool="tool",
    )

    excerpt = store.read_excerpt(raw_ref, max_chars=300)

    assert excerpt is not None
    assert len(excerpt) <= 300
    assert excerpt.startswith("HEAD-")
    assert excerpt.endswith("-TAIL")
    assert "middle omitted" in excerpt


def test_context_artifact_store_falls_back_when_primary_store_fails(tmp_path):
    class BrokenPrimary:
        def store(self, content, **kwargs):
            raise RuntimeError("primary unavailable")

        def read(self, raw_ref, *, max_chars=None):
            return None

        def metadata(self, raw_ref):
            return None

    fallback = ArtifactStore(tmp_path)
    store = FallbackContextArtifactStore(BrokenPrimary(), fallback=fallback)

    raw_ref = store.store("abcdef", thread_id="thread-1", source_tool="tool")

    assert store.read(raw_ref, max_chars=3) == "abc"
    assert store.metadata(raw_ref) is not None


class _MinioLikeUnavailablePrimary:
    """Primary that mints MinIO-form refs but holds no objects.

    Mirrors the production failure mode: the write fell back to the local
    store, while ``object_ref`` keeps returning the primary reference and
    ``remove_object`` reports idempotent success for absent objects.
    """

    def store(self, content, **kwargs):
        raise RuntimeError("minio down")

    def read(self, raw_ref, *, max_chars=None):
        return None

    def read_excerpt(self, raw_ref, *, max_chars):
        return None

    def metadata(self, raw_ref):
        return None

    def object_ref(self, oid, prefix="context_object"):
        digest = str(oid or "")[:20]
        return (
            f"{ARTIFACT_URI_PREFIX}minio/bucket/context-artifacts/"
            f"cos/{prefix}-{digest}.json"
        )

    def scan_metadata(self):
        return []

    def delete(self, raw_ref):
        return True


def test_fallback_written_cos_object_readable_and_deletable_via_primary_ref(
    tmp_path,
):
    from backend.core.agents.context.object_store import ContextObjectStore
    from backend.core.agents.context.objects import (
        make_context_object,
        make_segment,
    )

    fallback = ArtifactStore(tmp_path)
    cos = ContextObjectStore(
        FallbackContextArtifactStore(
            _MinioLikeUnavailablePrimary(),
            fallback=fallback,
        )
    )
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="回退正文", score=0.9)],
    )
    oid = cos.put(obj)

    # Force a storage read (bypass the request-level cache): the MinIO-form
    # reference must resolve to the fallback-written local file.
    cos._read_cache.clear()
    restored = cos.read(oid)
    assert restored is not None
    assert restored["segments"][0]["text"] == "回退正文"

    # Deleting through the MinIO-form reference must remove the local copy;
    # previously MinIO reported idempotent success while the file leaked.
    assert cos.delete(oid) is True
    cos._read_cache.clear()
    assert cos.read(oid) is None
    assert list(tmp_path.rglob("context_object-*.json")) == []
