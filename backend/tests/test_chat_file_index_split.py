import pytest

from backend.services.chat_file_index import split_text


def test_split_text_applies_overlap_between_adjacent_chunks():
    text = "第一句内容很长很长。第二句内容也很长很长。第三句继续补充信息。"

    chunks = split_text(text, chunk_size=14, chunk_overlap=4)

    assert len(chunks) >= 2
    assert chunks[1].startswith(chunks[0][-4:])


def test_split_text_zero_overlap_keeps_original_chunk_boundaries():
    text = "第一句内容很长很长。第二句内容也很长很长。第三句继续补充信息。"

    without_overlap = split_text(text, chunk_size=14, chunk_overlap=0)
    with_overlap = split_text(text, chunk_size=14, chunk_overlap=4)

    assert len(without_overlap) == len(with_overlap)
    assert without_overlap[1] != with_overlap[1]
    assert not without_overlap[1].startswith(without_overlap[0][-4:])


def test_split_text_rejects_non_positive_chunk_size():
    with pytest.raises(ValueError):
        split_text("text", chunk_size=0, chunk_overlap=10)
