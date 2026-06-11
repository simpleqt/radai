# 第三遍补充：`directory_service.py`

## 1. API 路由映射

| 路由 | Service 方法 | commit 责任 |
|------|--------------|-------------|
| `GET .../tree` | `get_directory_tree` | 路由/session 依赖 |
| `POST create` | `create_directory` | **CRUD.create 内部 commit** |
| `PATCH update` | `update_directory` | 路由 commit |
| `POST rename` | `rename_directory` | **service 内 commit** |
| `POST move` | `move_directory` | service 内 commit |
| `DELETE` | `delete_directory` | service 内 commit |
| `POST sync` | `sync_from_documents` | service 内 commit |
| `POST recalculate` | `recalculate_doc_counts` | service 内 commit |

**第三遍（P1-NEW）事务边界混乱**：

- `DirectoryCRUD.create` 在 CRUD 层 `await session.commit()`（见 `directory_crud.py:68`）。
- `create_directory` 又 `ensure_parent_directories` + `upsert`，路由若再 commit → **双 commit** 或会话状态不一致。

---

## 2. `get_directory_tree` 数据流（细化）

```
物理目录表 list_all_by_kb
    ↓
path_to_dir[path] = node
    ↓
GROUP BY 文档 category_path → 补全隐含中间路径
    ↓
GROUP BY (category_path, document_date) → 虚拟日期节点挂 children
    ↓
物理节点父子 link → root_nodes
    ↓
sort_children 递归
```

**虚拟节点**不在 `knowledge_base_directories` 表，仅 API 响应存在；前端 DELETE/MOVE 若对虚拟节点发请求会 404。

---

## 3. `func.replace` 路径替换 — 反例扩充

| old_path | new_path | 文档路径 | REPLACE 结果 | 是否正确 |
|----------|----------|----------|--------------|----------|
| `a` | `b` | `a/b/doc` | `b/b/doc` | ✗ 误替换前缀 |
| `期刊` | `杂志` | `期刊/2024` | `杂志/2024` | ✓ |
| `foo` | `bar` | `notfoo/x` | `notbar/x` | ✗ |

当前 `WHERE` 使用精确匹配或 `LIKE old_path/%`，但 `SET replace(列, old, new)` 对**整列**子串替换，与 WHERE 不匹配时仍可能误伤边界情况。

**正确模式**（文档建议）：

```sql
-- 伪代码
UPDATE ... SET category_path = CONCAT(:new, SUBSTRING(category_path, LENGTH(:old)+1))
WHERE category_path = :old OR category_path LIKE :old || '/%'
```

---

## 4. `rename_directory` / `move_directory` 与 CRUD 封装

- 直接 `session.execute(update(KnowledgeBaseDirectory)...)`，绕过 `DirectoryCRUD.update`。
- `affected_count = len(descendants)` 含自身；日志「影响 N 个目录」语义正确。
- **未**在操作后调用 `recalculate_doc_counts`；依赖已有 `doc_count` 字段准确性。

---

## 5. `delete_directory` 语义

| 检查 | 行为 |
|------|------|
| `is_system` | 拒绝 |
| 物理子目录 `get_children` | 拒绝 |
| `doc_count > 0` 且非 `delete_documents` | 拒绝 |
| 删除文档 | 仅 **精确** `category_path`，不含子路径 |

若文档在 `parent/child` 而删 `parent`，且 `delete_documents=true`，**子路径文档不删**。

---

## 6. `sync_from_documents` vs `get_directory_tree` 隐含目录

- `sync` 写 DB 物理行。
- `get_directory_tree` 步骤 2 可在**无 DB 行**时内存补节点。

两者不同步时：树上有「自动发现的分类」，DB 无对应行 → 管理端编辑会失败。

---

## 7. 性能：大 KB

设 N_doc = 10⁴，唯一 path = 10²：

| 操作 | 查询次数 |
|------|----------|
| `get_directory_tree` | 2 次 GROUP BY + O(path×深度) Python |
| `recalculate_doc_counts` | 目录数 D × 2 COUNT |
| `update_doc_counts_for_path` | 深度 × 2 COUNT |

10⁴ 文档 × 200 目录 → `recalculate` ~400 次 COUNT，>1s 常见。

---

## 8. 第三遍：CRUD 穿透

`directories.py`：

```python
directory = await directory_service.crud.get_by_path(...)
```

**问题**：路由依赖 CRUD 实现细节（表结构、commit 策略），`DirectoryService` Facade 不完整。

---

## 9. 与 RAG manager 联动

`rag/knowledge_base/manager.py` 在创建文档时 `directory_service.create_directory`——入库路径会自动补目录，与 `sync_from_documents` 可能重复创建（`upsert` 缓解）。

---

## 10. 第三遍新增问题汇总

| ID | 级别 | 描述 |
|----|------|------|
| NEW-01 | P1 | `func.replace` 子串替换路径 |
| NEW-02 | P1 | CRUD.create 与 service 双层 commit |
| NEW-03 | P2 | 虚拟目录不可 CRUD |
| NEW-04 | P2 | 删除不级联子路径文档 |
| NEW-05 | P2 | 树计数与 `recalculate` 双源真相 |
| NEW-06 | P3 | 路由直接使用 `.crud` |

---

## 11. 第三遍结论

业务功能完整，**路径替换 SQL 与事务边界**为生产数据损坏类风险，优先于排序/注释类问题。建议为 `rename/move` 增加路径边界单测（含中文路径、`a` vs `ab`）。
