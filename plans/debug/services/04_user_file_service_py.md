# 审查：`backend/services/user_file_service.py`

**文件规模**: 675 行  
**职责**: 用户文件列表、工件登记（local/minio）、下载鉴权、下载审计

**形态**: 纯函数模块（无 `UserFileService` 类），与 `DirectoryService` 类风格不一致。

---

## 1. API 一览

| 函数 | 同步/异步 | Session 所有权 | 调用方 |
|------|-----------|----------------|--------|
| `list_active_files_for_thread_for_user` | async | 调用方 | `files` 路由 |
| `register_local_artifact_for_thread` | async | **自建** + commit | （潜在异步工件流） |
| `register_minio_artifact_for_thread` | async | 自建 + commit | 同上 |
| `register_local_artifact_for_thread_sync` | sync | 自建 + commit | `artifact_utils` |
| `register_minio_artifact_for_thread_sync` | sync | 自建 + commit | `artifact_utils` |
| `resolve_user_file_for_download` | async | 调用方 | `files` 路由 |
| `write_download_audit` | async | 调用方 commit | `files` 路由 |
| `list_download_audit_for_user` | async | 调用方 | `files` 路由 |

---

## 2. 高优先级（P1）

### P1-01 async/sync 四段近重复实现（~360 行重复）

`register_local_*` 与 `register_minio_*` 各两份，逻辑除 `await`/`session.execute` 外相同。

**风险**: 已出现字段不一致——

| 字段 | local 登记 | minio 登记 |
|------|-----------|------------|
| `size_bytes` | ✓ `stat()` | **未写入** |

**建议**: `_register_artifact(session, *, source_type, ...)` 单核心；MinIO 用 `head_object` 补 `size_bytes`。

---

### P1-02 登记函数自建 Session，与路由层事务隔离

```python
factory = get_async_session_factory()
async with factory() as session:
    ...
    await session.commit()
```

**优点**: Agent 同步回调无需 AsyncSession。  
**缺点**: 无法与同一请求内其他 DB 操作同一事务；失败时无法与上游一起 rollback。

---

### P1-03 `list_active_files_for_thread_for_user` 暴露 `local_abs_path`

返回 JSON 含服务器绝对路径：

```python
"local_abs_path": str(row["local_abs_path"]) if row.get("local_abs_path") else None,
```

**安全**: 授权用户可见路径泄露部署目录结构（如 `/home/.../data/artifacts/...`）。下载应仅通过 `download_url`。

**建议**: 列表 API 去掉 `local_abs_path`；详情接口按需脱敏。

---

### P1-04 去重查询仅匹配 `status='active'`

重复登记同一物理路径时，若旧记录 `inactive`，会 **INSERT 新 file_id**，产生重复逻辑文件。

---

## 3. 中优先级（P2）

### P2-01 `resolve_user_file_for_download` 权限模型

**已实现**:

- Owner → `ok`
- `user_file_permissions` 中 `read`/`download` 且未过期 → `ok`
- `inactive` → 拒绝但仍返回 `file_row`（便于审计区分）

**未覆盖**（需产品确认）:

- 管理员代下
- 会话成员共享（仅 thread 绑定未在 permissions 表）
- `source_type='local'` 路径在 owner 下载时是否校验路径仍在 artifacts 目录内（**下载时未 re-validate 路径**）

---

### P2-02 `write_download_audit` 无 commit 文档

依赖 `get_async_session` 请求结束 commit。函数 docstring 应写明契约，避免脚本直接调用丢审计。

---

### P2-03 `list_download_audit_for_user` 无分页总数

返回 `count: len(records)` 为当前页条数，非总命中数；前端无法做完整分页 UI。

---

### P2-04 SQL 全为 raw `text()`

无 ORM 模型校验，字段改名易碎。与项目其他处混用 ORM 风格不一致。

---

### P2-05 `parse_internal_thread_key` 失败静默返回 `None`

`user_id` 为空时 `register_*` 返回 `None`，调用方需处理；`artifact_utils` 若未检查会导致无下载链接。

---

## 4. 低优先级（P3）

### P3-01 路径安全（做得较好）

```python
local_path = (outputs_dir / safe_name).resolve()
local_path.relative_to(outputs_dir)  # ValueError → 拒绝
```

可防御 `../../../etc/passwd` 类路径穿越。

**缺口**: 未限制 `safe_name` 长度、未禁止特殊设备文件。

---

### P3-02 `file_id` 生成

`uf_{uuid.uuid4().hex}` — 128 bit 随机，碰撞可忽略。

---

### P3-03 `_build_download_url` 硬编码前缀

```python
return f"/api/v2/files/{file_id}/download"
```

未考虑反向代理 `root_path`、多租户域名；与全局 API 前缀配置耦合。

---

### P3-04 `UserFileResolved` dataclass

字段完整，但无 `metadata_`、无 `created_at`，下载路由若扩展元数据需再查库。

---

## 5. 与 `files` 路由协作审查

| 端点 | 审计 | commit |
|------|------|--------|
| `GET /{file_id}/meta` | allow/deny 均写 | 依赖 dependency ✓ |
| `GET /{file_id}/download` | 各分支写 | 依赖 dependency ✓ |
| `GET ""` 列表 | 无审计 | ✓ |

**注意**: deny 后 `raise HTTPException`，dependency 仍会 commit 审计记录（符合预期）。

MinIO 下载用同步 `client.get_object` + generator，在 async 路由中可能阻塞事件循环（**路由层问题**，非本文件）。

---

## 6. 建议测试用例

- 路径穿越 `artifact_filename`
- 重复登记同路径
- grant 过期权限
- minio 登记后 `size_bytes` 是否为 NULL
- 列表响应是否应含 `local_abs_path`

---

## 7. 本文件结论

权限与路径校验基础扎实，**最大技术债是重复代码与 minio 缺 size、列表泄露绝对路径**。建议优先 DRY 重构并收紧 API 返回字段。
