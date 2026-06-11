# 第七遍补充：`directories.py` 路由（`directory_service` 消费方）

> 第二遍已审 `directory_service.py`；本文件补 **HTTP 层** 与 Pydantic 契约。

**文件**：`backend/api/routers/directories.py`（~477 行）

---

## 1. 端点与 service 映射

| HTTP | Service 方法 | commit |
|------|--------------|--------|
| GET `.../directories` | `get_directory_tree` | 依赖 get_db_session |
| POST create | `create_directory` | CRUD 内 commit |
| GET `.../{path}` | `crud.get_by_path` **穿透** | — |
| PATCH update | `update_directory` | 路由 commit? 需读后半 |
| POST rename/move | `rename_directory` / `move_directory` | service 内 commit |
| DELETE | `delete_directory` | service 内 commit |
| POST sync | `sync_from_documents` | service 内 commit |
| POST recalculate | `recalculate_doc_counts` | service 内 commit |

---

## 2. 路由层问题

| ID | 级别 | 描述 |
|----|------|------|
| DR-01 | P1 | `DirectoryTreeNode` 无 `is_virtual` 字段，前端无法区分日期虚拟目录 |
| DR-02 | P1 | `id: int \| str` 混合物理 id 与 `implied-*` / `v-*` 字符串 |
| DR-03 | P2 | GET 单目录走 `directory_service.crud` 破坏分层 |
| DR-04 | P2 | 与 `knowledge_base_documents` deprecated tree API 双轨 |
| DR-05 | P2 | 全部 `require_admin`，无普通用户只读（若产品需要） |
| DR-06 | P3 | `_flatten_tree` 在 directories 与 kb_docs 各一份，逻辑略异 |

---

## 3. 与 service P0 的联动

- `rename` / `move` 路由直接调 `directory_service.rename_directory` → 受 **func.replace** 影响（pass3/06 P0-05）。  
- 修复应在 service 层；路由无需改路径，但应加集成测试经 HTTP 调用。

---

## 4. 响应模型建议（文档）

`DirectoryTreeResponse.total_directories` 使用 `_flatten_tree` 计数，含虚拟节点——与物理表行数不一致，管理端需知悉。

---

*第七遍 — directories 路由*
