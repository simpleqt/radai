import asyncio
import mimetypes
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.db.connection import get_async_session
from backend.db.models import Conversation, User
from backend.core.config import get_paths, build_internal_thread_key

router = APIRouter()


@router.get("/{thread_id}/artifacts/{path:path}")
async def get_thread_artifact(
    thread_id: str,
    path: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user.user_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="无权访问该资源或资源不存在")

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)

    paths = get_paths()
    try:
        output_dir = paths.thread_artifacts_dir(internal_thread_id)
        actual_path, outputs_root = await asyncio.gather(
            asyncio.to_thread((output_dir / path).resolve),
            asyncio.to_thread(output_dir.resolve),
        )
        actual_path.relative_to(outputs_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径越权拦截")

    exists, is_file = await asyncio.gather(
        asyncio.to_thread(actual_path.exists),
        asyncio.to_thread(actual_path.is_file),
    )
    if not exists or not is_file:
        raise HTTPException(status_code=404, detail="工件不存在")

    mime_type, _ = mimetypes.guess_type(str(actual_path))
    return FileResponse(
        actual_path, media_type=mime_type or "application/octet-stream"
    )
