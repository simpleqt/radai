import pytest
from fastapi import HTTPException

from backend.api.dependencies.auth import ApiKeyAuth, require_admin


@pytest.mark.asyncio
async def test_require_admin_allows_admin_user() -> None:
    auth = ApiKeyAuth(api_key="k", user_id="u1", role="admin")
    result = await require_admin(auth)
    assert result.user_id == "u1"
    assert result.role == "admin"


@pytest.mark.asyncio
async def test_require_admin_rejects_non_admin_user() -> None:
    auth = ApiKeyAuth(api_key="k", user_id="u2", role="user")
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(auth)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "需要管理员权限"
