"""独立入库 Worker：从 kb_pending_files 拉取 status=1 的任务进行处理。"""

import asyncio
import logging
import os

from sqlalchemy import select

from backend.config.loggers import get_logger
from backend.db.connection import async_session_context
from backend.db.models import PendingFile
from backend.worker.components.ingest_pending_processor import PendingIngestProcessor

logger = get_logger(__name__)


async def run_worker():
    processor = PendingIngestProcessor()
    poll_interval = float(os.getenv("INGEST_WORKER_POLL_INTERVAL", "2"))
    batch_size = int(os.getenv("INGEST_WORKER_BATCH_SIZE", "5"))
    candidate_multiplier = int(os.getenv("INGEST_WORKER_CANDIDATE_MULTIPLIER", "10"))
    candidate_limit = max(batch_size, batch_size * max(1, candidate_multiplier))

    logger.info(
        "ingest worker started: poll_interval=%s, batch_size=%s, candidate_limit=%s",
        poll_interval,
        batch_size,
        candidate_limit,
    )

    while True:
        try:
            async with async_session_context() as session:
                result = await session.execute(
                    select(PendingFile.file_id)
                    .where(PendingFile.status == 1)
                    .order_by(PendingFile.updated_at.asc(), PendingFile.created_at.asc())
                    .limit(candidate_limit)
                )
                file_ids = list(result.scalars().all())

            if not file_ids:
                await asyncio.sleep(poll_interval)
                continue

            processed_count = 0
            for file_id in file_ids:
                success = await processor.process_file(file_id)
                if success:
                    processed_count += 1

            # 如果这一批次没有任何文件拿到锁并成功处理，我们强制休眠，防止死循环刷日志
            if processed_count == 0:
                await asyncio.sleep(poll_interval)

        except Exception as exc:
            logger.error("ingest worker loop error: %s", exc, exc_info=True)
            await asyncio.sleep(poll_interval)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d - %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
