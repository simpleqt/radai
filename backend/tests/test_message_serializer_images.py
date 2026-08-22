from urllib.parse import parse_qs, urlsplit

from langchain_core.messages import AIMessage

from backend.application.conversations.message_serializer import (
    serialize_checkpoint_messages,
)
from backend.storage import verify_kb_image_proxy_signature


def test_history_serialization_generates_fresh_url_from_durable_object_name() -> None:
    serialized = serialize_checkpoint_messages(
        [AIMessage(content="![上轮图片]([IMAGE:001])")],
        registered_images={
            "[IMAGE:001]": {
                "object_name": "staging/kb-id/job/images/figure.jpg",
                "caption": "上轮图片",
                "index": 1,
            }
        },
    )

    content = serialized[0]["content"]
    image_url = content.removeprefix("![上轮图片](").removesuffix(")")
    query = parse_qs(urlsplit(image_url).query)
    assert query["object_name"][0] == "staging/kb-id/job/images/figure.jpg"
    assert verify_kb_image_proxy_signature(
        query["object_name"][0],
        int(query["exp"][0]),
        query["sig"][0],
    )


def test_history_serialization_refreshes_legacy_expired_url_in_table() -> None:
    expired = (
        "/api/v2/files/kb-image?object_name=staging%2Fkb-id%2Fjob%2Fimages%2Ffigure.jpg"
        "&exp=1&sig=expired"
    )
    serialized = serialize_checkpoint_messages(
        [
            AIMessage(
                content=(
                    "| 图片标识 | 描述 |\n" "|---|---|\n" f"| {expired} | 审计流程 |"
                )
            )
        ],
    )

    content = serialized[0]["content"]
    assert "sig=expired" not in content
    assert "| ![知识库图片](</api/v2/files/kb-image?" in content
    assert "| /api/v2/files/kb-image?" not in content
