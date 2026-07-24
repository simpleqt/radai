"""待入库文件处理组件（供独立 ingest worker 使用）。"""

from backend.application.ingestion import PendingIngestUseCase


class PendingIngestProcessor:
    """处理 kb_pending_files 中 status=1 的文件。"""

    def __init__(self):
        self._use_case = PendingIngestUseCase(
            source_label="ingest-worker",
            legacy_md_direct=True,
        )

    async def process_file(self, file_id: str) -> bool:
        return await self._use_case.process_file_id(file_id)
