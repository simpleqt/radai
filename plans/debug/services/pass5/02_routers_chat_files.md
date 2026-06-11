# 第五遍：`backend/api/routers/chat_files.py`

**行数**：~413  
**依赖 services**：`ChatFileParser`, `get_index_manager`, `clean_surrogates`

---

## 端点矩阵

| 方法 | 路径 | 鉴权 | Session 提交 |
|------|------|------|--------------|
| POST | `/chat-files/upload` | `get_current_user` | `get_async_session` 自动 commit |
| GET | `/chat-files/{thread_id}` | 同左 | 只读 |
| DELETE | `/chat-files/{file_id}` | 同左 | delete session_file |

---

## 逻辑审查

### 上传循环（核心）

1. 扩展名白名单  
2. 50MB 限制  
3. `parser.parse`  
4. long_doc → `index.add_document`（**try/except 仅 log**）  
5. MinIO upload  
6. `UserFile` + `SessionFile` + commit  

| 问题 | 级别 | 说明 |
|------|------|------|
| 索引失败仍 `strategy=indexed` | P1 | metadata 与真实索引不一致 |
| 失败项用随机 `file_id` 在错误分支 | — | 不写 DB，OK |
| multimodal base64 进 JSON | P1 | 大图片撑爆 JSON |
| 无病毒/内容扫描 | P3 | 方案未要求 |

### 列表 GET

- JOIN `SessionFile` + `UserFile`：**正确模式**（对比 `files.py` thread_id 过滤）。
- 不返回 `content` 预览，减载。

### DELETE

- 解绑 `SessionFile` + `remove_document`：**良好**。
- 不删 `UserFile`（个人库仍保留）——符合「仅解绑会话」文案。

### 模块级工具

| 函数 | 用途 | 问题 |
|------|------|------|
| `get_file_meta` | agent 同步读 | 同步 factory，高并发连接压力 P2 |
| `get_files_by_thread` | agent | JOIN 正确 |

---

## 与 services 契约

- `chunk_size` / `overlap` 用默认，路由不可配置。  
- `thread_id` Form 字段直接作 index key，须与 Agent `self._thread_id` 一致。

---

## 建议改动（路由层）

1. 索引失败：`strategy=context` 或 `index_pending`，或 HTTP 207 部分成功。  
2. multimodal：content 不进 DB，仅 MinIO + 短 metadata。  
3. `get_file_meta` 改 async 或缓存。

---

## 测试用例

- 上传 11 个文件 → 400  
- long_doc 模拟 index 抛错 → 响应/DB 策略字段  
- DELETE 后 search 无块

---

*第五遍 — chat_files 路由*
