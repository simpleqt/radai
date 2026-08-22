from backend.services.chat_file_index import ChatFileIndex, prepare_search_inputs


def test_search_merges_results_from_multiple_keyword_attempts() -> None:
    index = ChatFileIndex(thread_id="thread-search")
    index.add_document(
        file_id="file-1",
        file_name="paper.md",
        content=(
            "Alpha section explains retrieval augmentation in detail.\n\n"
            "Beta section focuses on chunk reranking strategies.\n\n"
            "Gamma section describes citation grounding and answer synthesis."
        ),
        chunk_size=60,
        chunk_overlap=0,
    )

    results = index.search(
        query="请查找这篇论文里关于 Alpha Beta Gamma 的内容",
        keywords=["Alpha", "Beta", "Gamma"],
        top_k=3,
    )

    contents = "\n".join(result.content for result in results)
    assert "Alpha section" in contents
    assert "Beta section" in contents
    assert "Gamma section" in contents


def test_search_falls_back_to_head_only_after_all_attempts_miss() -> None:
    index = ChatFileIndex(thread_id="thread-fallback")
    index.add_document(
        file_id="file-2",
        file_name="doc.md",
        content=(
            "Introduction overview.\n\n"
            "Method details.\n\n"
            "Experiment results."
        ),
        chunk_size=40,
        chunk_overlap=0,
    )

    results = index.search(
        query="completely unrelated query",
        keywords=["nonexistent-term"],
        top_k=2,
    )

    assert results
    assert results[0].content.startswith("Introduction overview")
    assert all(result.score == 0.0 for result in results)


def test_prepare_search_inputs_rewrites_natural_language_image_question() -> None:
    query, keywords = prepare_search_inputs("论文有什么图片")

    assert query != "论文有什么图片"
    assert "figure" in query
    assert "image" in query
    assert "图片" in keywords


def test_prepare_search_inputs_extracts_precise_terms_from_question() -> None:
    query, keywords = prepare_search_inputs("QChunker与现有文本分块方法的效果对比图是什么样的？")

    assert "QChunker" in keywords
    assert any("文本分块方法" in kw for kw in keywords)
    assert "figure" in query


def test_search_uses_pattern_matching_for_figure_queries() -> None:
    index = ChatFileIndex(thread_id="thread-figures")
    index.add_document(
        file_id="file-3",
        file_name="paper-with-figures.md",
        content=(
            "Abstract overview.\n\n"
            "[图片: Figure 1 Architecture of QChunker]\n\n"
            "Figure 2: Experimental results on domain rag.\n\n"
            "Conclusion paragraph."
        ),
        chunk_size=80,
        chunk_overlap=0,
    )

    results = index.search(
        query="论文有什么图片",
        keywords=["Figure", "图片"],
        top_k=2,
    )

    assert results
    merged = "\n".join(item.content for item in results)
    assert "Figure 1 Architecture of QChunker" in merged
