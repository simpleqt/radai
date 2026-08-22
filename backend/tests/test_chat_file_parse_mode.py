from backend.services.chat_file_parser import ChatFileParser, ParseResult
from types import SimpleNamespace


async def test_parse_mode_mineru_forces_rich_document_path(monkeypatch) -> None:
    parser = ChatFileParser(mineru_api_url="http://mineru.test")

    async def _fake_try_mineru(data: bytes, filename: str) -> ParseResult:
        return ParseResult(
            category="long_doc",
            method="mineru",
            content="MinerU extracted content with figures",
            content_length=38,
        )

    monkeypatch.setattr(parser, "_try_mineru", _fake_try_mineru)

    result = await parser.parse(
        b"%PDF-1.4 fake",
        "paper.pdf",
        "application/pdf",
        parse_mode="mineru",
    )

    assert result.method == "mineru"
    assert result.category == "long_doc"


def test_extract_mineru_images_reads_remote_urls_from_content_list() -> None:
    parser = ChatFileParser(mineru_api_url="http://mineru.test")
    detail = SimpleNamespace(
        images=None,
        content_list=[
            {
                "type": "image",
                "img_path": "images/figure-1.png",
                "image_caption": ["Figure 1 architecture"],
            }
        ],
    )

    images, remote_urls = parser._extract_mineru_images(
        detail,
        "![Figure 1 architecture](images/figure-1.png)",
        markdown_url="https://mineru.example/results/doc.md",
    )

    assert images == []
    assert len(remote_urls) == 1
    assert remote_urls[0]["url"] == "https://mineru.example/results/images/figure-1.png"
    assert remote_urls[0]["caption"] == "Figure 1 architecture"


def test_extract_mineru_images_falls_back_to_markdown_remote_refs() -> None:
    parser = ChatFileParser(mineru_api_url="http://mineru.test")
    detail = SimpleNamespace(images=None, content_list=None)

    images, remote_urls = parser._extract_mineru_images(
        detail,
        "正文\n![Figure 2 result](https://cdn.example.com/fig2.png)\n",
    )

    assert images == []
    assert len(remote_urls) == 1
    assert remote_urls[0]["url"] == "https://cdn.example.com/fig2.png"
    assert remote_urls[0]["caption"] == "Figure 2 result"
