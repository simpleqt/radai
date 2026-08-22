from urllib.parse import parse_qs, urlsplit

from backend.api.routers.files import _minio_image_object_candidates
from backend.storage import (
    extract_kb_image_object_name,
    get_minio_image_proxy_url,
    verify_kb_image_proxy_signature,
)


def test_get_minio_image_proxy_url_preserves_literal_percent_object_key() -> None:
    url = (
        "https://radai2.cdut.edu.cn/files/kb-id/"
        "%E6%A0%B8%E6%8A%80%E6%9C%AF/auto/images/example.jpg"
    )

    proxy_url = get_minio_image_proxy_url(url)
    assert proxy_url is not None
    query = parse_qs(urlsplit(proxy_url).query)
    object_name = query["object_name"][0]
    expires_at = int(query["exp"][0])
    signature = query["sig"][0]

    assert object_name == "kb-id/%E6%A0%B8%E6%8A%80%E6%9C%AF/auto/images/example.jpg"
    assert verify_kb_image_proxy_signature(object_name, expires_at, signature)


def test_existing_expired_proxy_url_is_refreshed(monkeypatch) -> None:
    monkeypatch.setattr("backend.storage.time.time", lambda: 1_800_000_000)
    expired = (
        "/api/v2/files/kb-image?object_name=staging%2Fkb-id%2Fjob%2Fimages%2Ffigure.jpg"
        "&exp=1&sig=expired"
    )

    refreshed = get_minio_image_proxy_url(expired)
    assert refreshed is not None
    assert extract_kb_image_object_name(refreshed) == (
        "staging/kb-id/job/images/figure.jpg"
    )
    query = parse_qs(urlsplit(refreshed).query)
    assert int(query["exp"][0]) > 1_800_000_000
    assert query["sig"][0] != "expired"
    assert verify_kb_image_proxy_signature(
        query["object_name"][0],
        int(query["exp"][0]),
        query["sig"][0],
    )


def test_minio_image_object_candidates_support_literal_and_unicode_keys() -> None:
    candidates = _minio_image_object_candidates(
        "kb-id/%E6%A0%B8%E6%8A%80%E6%9C%AF/image.jpg"
    )

    assert candidates == [
        "kb-id/%E6%A0%B8%E6%8A%80%E6%9C%AF/image.jpg",
        "kb-id/核技术/image.jpg",
    ]


def test_minio_image_object_candidates_support_mixed_double_encoding() -> None:
    candidates = _minio_image_object_candidates(
        "kb-id/%E8%BE%90%E5%B0%84%E6%8E%A2%E6%B5%8B%2520/image.jpg"
    )

    assert candidates == [
        "kb-id/%E8%BE%90%E5%B0%84%E6%8E%A2%E6%B5%8B%2520/image.jpg",
        "kb-id/辐射探测%20/image.jpg",
        "kb-id/辐射探测 /image.jpg",
    ]


def test_minio_image_object_candidates_reject_path_traversal() -> None:
    assert _minio_image_object_candidates("kb-id/../secret.jpg") == []
    assert _minio_image_object_candidates("kb-id/%2e%2e/secret.jpg") == []
    assert _minio_image_object_candidates("kb-id/%252e%252e/secret.jpg") == []
