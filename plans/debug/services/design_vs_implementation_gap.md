# 设计方案 vs 实现差距对照

> 对照文档：  
> - [plans/个人文件夹文件管理与上传复用设计方案.md](../../个人文件夹文件管理与上传复用设计方案.md)  
> - [docs/docs_backend/personal_files_module.md](../../../docs/docs_backend/personal_files_module.md)  
> - 审查对象：`backend/services` + 直接调用它的路由

---

## 1. 总览

| 维度 | 方案要求 | 当前实现 | 差距 |
|------|----------|----------|------|
| 上传入个人库 | ✓ | `my_files` + `chat_files` 均写 `user_files` | 双入口，路径前缀不统一 |
| 会话附件解耦 | `session_files` | ✓ 已实现 | — |
| 从我的文件添加 | attach API | ✓ | — |
| 删除后会话再引用提示失效 | 软删 + 不可用 | 软删有；索引/metadata 仍可能可用 | **高** |
| last_used_at | attach 更新 | **未实现** | **中** |
| hash 去重 | 可选 | `sha256` 列未写 | **低** |
| 长文档索引联动 | 待完成（方案注记） | 部分：attach/upload 建索引 | **中**（多 worker/驱逐） |
| 统一上传路由 | 建议单路由 | `chat-files` + `my-files` 分离 | **低**（文档已标待办） |
| 权限默认私有 | 仅本人 | owner + grant 表 | ✓ 超出 V1（有 grant） |

---

## 2. 场景验收矩阵

### 场景 A：上传新文件（会话内）

| 步骤 | 方案 | 实现 | 状态 |
|------|------|------|------|
| 附加到当前会话 | ✓ | `chat_files` 写 `SessionFile` | ✅ |
| 落入个人文件夹 | ✓ | `UserFile` | ✅ |
| 来源标记 upload | ✓ | `attached_from=upload` | ✅ |
| my_files 路径上传不绑会话 | — | 需单独 attach | ⚠️ 产品设计如此 |

### 场景 B：复用历史文件

| 步骤 | 方案 | 实现 | 状态 |
|------|------|------|------|
| 选择器 | 前端 | `MyFilesPickerDialog` | ✅ |
| attach | ✓ | `POST .../attach` | ✅ |
| 更新 last_used_at | ✓ | 无 | ❌ |
| long_doc 建索引 | 方案待完成 | attach 时 `add_document` | ⚠️ |

### 场景 C：系统生成文件

| 步骤 | 方案 | 实现 | 状态 |
|------|------|------|------|
| save generated | ✓ | `generated-files/save` | ✅ |
| 自动归档策略 | 待完成 | 部分 manual | ❌ |
| artifact MinIO 登记 | — | `artifact_utils` + `user_file_service` | ⚠️ |

### 场景 D：删除文件

| 步骤 | 方案 | 实现 | 状态 |
|------|------|------|------|
| status=deleted | ✓ | ✓ | ✅ |
| 历史消息保留 | ✓ | 未删消息 | ✅ |
| 再次引用提示失效 | ✓ | 无统一「失效」；索引可能仍在 | ❌ |

---

## 3. 数据模型字段差距

| 方案字段 | 实际 `UserFile` | 说明 |
|----------|-----------------|------|
| user_id | `owner_user_id` | 命名不同，可接受 |
| storage_key | `storage_object` + bucket | ✓ |
| source upload/generated | `source_type` local/minio | 枚举值不完全对齐 |
| hash | `sha256` | **未写入** |
| last_used_at | **无列** | 可用 `updated_at` 替代或迁移 |
| extra_meta | `metadata_` JSON | ✓ 含 category/strategy/content |
| thread_id 在 user_files | 可空 | 个人库常为 NULL；列表 API 用 thread_id 过滤有问题 |

---

## 4. services 能力与方案「待完成」对齐

| 方案 / 模块文档表述 | services 现实 | 建议 |
|--------------------|-----------------|------|
| 会话级 BM25 ⚠️ 部分 | 已实现核心；overlap/多 worker/驱逐未完善 | 更新 `personal_files_module.md` 为「已实现核心，生产需约束」 |
| MinerU ❌ 待完成 | `ChatFileParser` 已接 MinerU，依赖配置 | 改为「可选依赖 MinerU」 |
| 与长文档索引联动待完成 | attach/upload 已联动；删除/驱逐未联动 | 方案 5.30 可勾选子项 |

---

## 5. 推荐方案文档修订（仅文档）

1. **§5.3 增加「会话索引生命周期」**：单 worker、驱逐、attach 重建。  
2. **§7.4 明确删除须清理 `session_files` + 内存索引**。  
3. **§8 接口补充**：attach 响应是否含 `indexed: bool`。  
4. **`personal_files_module.md` §8**：按上表更新状态列。  

---

## 6. 差距驱动的优先 backlog 映射

| 差距 | Backlog ID |
|------|------------|
| 删除后仍可查索引 | P0-06, P1-03 |
| last_used_at | P1-10 |
| 列表 thread_id | P1-07 |
| 长文档 DB 全文 | P1-05 |
| 三消费端 | P0-01 |

完整任务表：[backend_services_fix_backlog.md](../backend_services_fix_backlog.md)

---

*2026-05-30*
