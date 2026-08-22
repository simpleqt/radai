import time

from backend.api.routers.chat_files import (
    _build_chat_image_signature,
    _verify_chat_file_image_url,
)


def test_chat_file_image_signature_verification_round_trip() -> None:
    expires_at = int(time.time()) + 600
    sig = _build_chat_image_signature(
        file_id="uf_demo",
        image_name="figure_1.png",
        owner_user_id="user_1",
        thread_id="thread_1",
        expires_at=expires_at,
    )

    assert _verify_chat_file_image_url(
        file_id="uf_demo",
        image_name="figure_1.png",
        owner_user_id="user_1",
        thread_id="thread_1",
        exp=expires_at,
        sig=sig,
    )


def test_chat_file_image_signature_rejects_wrong_file() -> None:
    expires_at = int(time.time()) + 600
    sig = _build_chat_image_signature(
        file_id="uf_demo",
        image_name="figure_1.png",
        owner_user_id="user_1",
        thread_id="thread_1",
        expires_at=expires_at,
    )

    assert not _verify_chat_file_image_url(
        file_id="uf_other",
        image_name="figure_1.png",
        owner_user_id="user_1",
        thread_id="thread_1",
        exp=expires_at,
        sig=sig,
    )
