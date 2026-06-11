"""Unified v2 agent runtime router aggregation."""

from fastapi import APIRouter

from backend.api.routers.agent_runtime import router as agent_runtime_router
from backend.api.routers.explore import router as explore_router
from backend.api.routers.guest_agent import router as guest_router

api_router = APIRouter(prefix="/api/v2")
api_router.include_router(agent_runtime_router)
api_router.include_router(guest_router)
api_router.include_router(explore_router)

__all__ = ["api_router"]
