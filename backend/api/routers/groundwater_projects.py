"""Groundwater project user access and administrator management API."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, require_admin, verify_api_key
from backend.application.blocking import run_blocking
from backend.application.groundwater import GroundwaterProjectService

router = APIRouter(prefix="/groundwater", tags=["groundwater-projects-v2"])


class GroundwaterProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    project_id: str | None = Field(default=None, max_length=64)
    region: str = Field(default="", max_length=255)
    coordinate_system: str = Field(default="", max_length=100)


class GroundwaterProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=255)
    coordinate_system: str | None = Field(default=None, max_length=100)


class GroundwaterProjectBindingsRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/projects")
async def list_groundwater_projects(
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    service = GroundwaterProjectService()
    return {
        "projects": await run_blocking(
            service.list_projects,
            auth.user_id,
            is_admin=(auth.role or "").lower() == "admin",
        )
    }


@router.get("/projects/{project_id}")
async def get_groundwater_project(
    project_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    try:
        context = await run_blocking(
            GroundwaterProjectService().resolve,
            auth.user_id,
            project_id,
            is_admin=(auth.role or "").lower() == "admin",
        )
        return {
            "project_id": context.project_id,
            "name": context.project_name,
            "manifest": context.manifest,
            "read_only": context.read_only,
        }
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/projects/{project_id}/resources/{resource}")
async def read_groundwater_project_resource(
    project_id: str,
    resource: str,
    limit: int = 100,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    try:
        service = GroundwaterProjectService()
        context = await run_blocking(
            service.resolve,
            auth.user_id,
            project_id,
            is_admin=(auth.role or "").lower() == "admin",
        )
        return await run_blocking(service.read_resource, context, resource, limit=limit)
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/admin/projects")
async def list_admin_groundwater_projects(
    _auth: ApiKeyAuth = Depends(require_admin),
):
    return {
        "projects": await run_blocking(GroundwaterProjectService().list_admin_projects)
    }


@router.post("/admin/projects")
async def create_groundwater_project(
    payload: GroundwaterProjectCreateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    try:
        context = await run_blocking(
            GroundwaterProjectService().create_project,
            auth.user_id,
            name=payload.name,
            project_id=payload.project_id,
            region=payload.region,
            coordinate_system=payload.coordinate_system,
        )
        return {
            "project_id": context.project_id,
            "name": context.project_name,
            "data_version": context.data_version,
            "read_only": context.read_only,
        }
    except Exception as exc:
        raise _service_error(exc) from exc


@router.patch("/admin/projects/{project_id}")
async def update_groundwater_project(
    project_id: str,
    payload: GroundwaterProjectUpdateRequest,
    _auth: ApiKeyAuth = Depends(require_admin),
):
    try:
        context = await run_blocking(
            GroundwaterProjectService().update_project,
            project_id,
            name=payload.name,
            region=payload.region,
            coordinate_system=payload.coordinate_system,
        )
        return {
            "project_id": context.project_id,
            "name": context.project_name,
            "data_version": context.data_version,
            "manifest": context.manifest,
            "read_only": context.read_only,
        }
    except Exception as exc:
        raise _service_error(exc) from exc


@router.delete("/admin/projects/{project_id}")
async def delete_groundwater_project(
    project_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
):
    try:
        return await run_blocking(
            GroundwaterProjectService().archive_project, project_id
        )
    except Exception as exc:
        raise _service_error(exc) from exc


@router.post("/admin/projects/{project_id}/upload")
async def upload_groundwater_project_package(
    project_id: str,
    file: UploadFile = File(...),
    auth: ApiKeyAuth = Depends(require_admin),
):
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="当前接口只接受标准 ZIP 项目包")
    try:
        service = GroundwaterProjectService()

        def import_package():
            context = service.resolve(auth.user_id, project_id, is_admin=True)
            with tempfile.NamedTemporaryFile(suffix=".zip") as temp_file:
                shutil.copyfileobj(file.file, temp_file)
                temp_file.flush()
                return service.import_zip(context, Path(temp_file.name))

        updated = await run_blocking(import_package)
        return {
            "project_id": updated.project_id,
            "data_version": updated.data_version,
            "manifest": updated.manifest,
        }
    except Exception as exc:
        raise _service_error(exc) from exc


@router.get("/admin/users")
async def list_groundwater_users(
    _auth: ApiKeyAuth = Depends(require_admin),
):
    return {"users": await run_blocking(GroundwaterProjectService().list_users)}


@router.get("/admin/projects/{project_id}/bindings")
async def list_groundwater_project_bindings(
    project_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
):
    try:
        return {
            "bindings": await run_blocking(
                GroundwaterProjectService().list_bindings, project_id
            )
        }
    except Exception as exc:
        raise _service_error(exc) from exc


@router.put("/admin/projects/{project_id}/bindings")
async def replace_groundwater_project_bindings(
    project_id: str,
    payload: GroundwaterProjectBindingsRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    try:
        bindings = await run_blocking(
            GroundwaterProjectService().replace_bindings,
            project_id,
            payload.user_ids,
            granted_by=auth.user_id,
        )
        return {"project_id": project_id, "bindings": bindings}
    except Exception as exc:
        raise _service_error(exc) from exc
