import json
import zipfile

import pytest

from backend.core.tools.ocr.base_ocr import OCRStatus
from backend.core.tools.ocr.mineru_ocr import MinerUOCR


class _ResultResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return {
            "backend": "pipeline",
            "version": "3.4.4",
            "results": {
                "sample": {
                    "md_content": "# Sample",
                    "content_list": '[{"type": "text", "text": "Sample"}]',
                    "middle_json": '{"pdf_info": []}',
                    "images": {
                        "figure.jpg": "data:image/jpeg;base64,YWJj",
                    },
                }
            },
        }


class _ResultSession:
    closed = False

    def get(self, url, timeout=None):
        assert url == "http://mineru:8000/tasks/task-v2/result"
        return _ResultResponse()


@pytest.mark.asyncio
async def test_v2_result_is_normalized_to_ocr_result(monkeypatch):
    client = MinerUOCR(api_url="http://mineru:8000")
    session = _ResultSession()

    async def get_session():
        return session

    monkeypatch.setattr(client, "_get_session", get_session)
    result = await client.get_task_data_async("task-v2")

    assert result.status == OCRStatus.COMPLETED
    assert result.file_name == "sample"
    assert result.markdown == "# Sample"
    assert result.content_list == [{"type": "text", "text": "Sample"}]
    assert result.middle_json == {"pdf_info": []}
    assert result.images == {"figure.jpg": "YWJj"}
    assert result.metadata == {"backend": "pipeline", "version": "3.4.4"}


def test_zip_result_is_normalized_without_materializing_images(tmp_path):
    archive_path = tmp_path / "result.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("sample/auto/sample.md", "# Sample")
        archive.writestr(
            "sample/auto/sample_content_list.json",
            json.dumps([{"type": "text", "text": "Sample"}]),
        )
        archive.writestr("sample/auto/sample_middle.json", '{"pdf_info": []}')
        archive.writestr("sample/auto/images/figure.jpg", b"\xff\xd8image")

    result = MinerUOCR._normalize_zip_result(
        archive_path,
        task_id="task-zip",
        upload_images=False,
        image_object_prefix="staging/kb/job/mineru_images",
        image_bucket_name=None,
    )

    assert result.status == OCRStatus.COMPLETED
    assert result.file_name == "sample"
    assert result.markdown == "# Sample"
    assert result.content_list == [{"type": "text", "text": "Sample"}]
    assert result.middle_json == {"pdf_info": []}
    assert result.images is None
    assert result.metadata == {"transport": "zip_stream", "images_uploaded": 0}
