from types import SimpleNamespace

import aiohttp
import pytest

from backend.core.tools.ocr.mineru_ocr import MinerUOCR


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return {"task_id": "task-after-restart"}


class _FailingRequest:
    async def __aenter__(self):
        key = SimpleNamespace(host="mineru", port=8000, ssl=False)
        raise aiohttp.ClientConnectorError(key, OSError(111, "connection refused"))

    async def __aexit__(self, *_args):
        return None


class _RestartingSession:
    closed = False

    def __init__(self):
        self.calls = 0
        self.bodies = []

    def post(self, _url, *, data, timeout=None):
        self.calls += 1
        self.bodies.append(next(field[2] for field in data._fields if field[0].get("name") == "files"))
        if self.calls == 1:
            return _FailingRequest()
        return _Response()


@pytest.mark.asyncio
async def test_submit_retries_connector_error_and_reopens_file(tmp_path, monkeypatch):
    source = tmp_path / "restart.pdf"
    source.write_bytes(b"pdf")
    client = MinerUOCR(api_url="http://mineru:8000")
    session = _RestartingSession()

    async def get_session():
        return session

    async def no_wait(_seconds):
        return None

    monkeypatch.setenv("MINERU_CONNECT_RETRY_ATTEMPTS", "2")
    monkeypatch.setattr(client, "_get_session", get_session)
    monkeypatch.setattr("backend.core.tools.ocr.mineru_ocr.asyncio.sleep", no_wait)

    assert await client.submit_task_async(str(source)) == "task-after-restart"
    assert session.calls == 2
    # 上传体改为在工作线程预读的 bytes：重试各自重新读取文件，
    # 且不再有需要关闭的文件句柄（事件循环内同步 read 的隐患一并消除）。
    # timeout 形参由桩签名承接（显式请求级超时，见 _SUBMIT_TIMEOUT）。
    assert session.bodies == [b"pdf", b"pdf"]
