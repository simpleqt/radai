# backend/services 相关测试计划（汇总）

> 当前测试：`backend/tests/test_surrogate_cleaning.py` 仅覆盖 `clean_surrogates`。  
> 以下为建议新增套件，按优先级排列。

---

## 1. 单元测试（pytest）

### `chat_file_index` — `tests/services/test_chat_file_index.py`

| 用例 | 断言 |
|------|------|
| `test_split_text_overlap` | 相邻块有重叠文本 |
| `test_split_text_empty` | 返回 `[]` |
| `test_add_document_idempotent` | 同 file_id 两次，块数不翻倍 |
| `test_remove_document` | 块数减少，search 无该文件 |
| `test_keyword_search_priority` | keywords 命中时不误触 head_fallback |
| `test_generic_query_fallback` | 「这篇文档讲了什么」→ head chunks |
| `test_multi_file_expand` | 两文件交错块，扩展不串文件 |
| `test_tokenize_consistency` | （若统一分词后）BM25 与 TF-IDF 共享词表 |

### `chat_file_parser` — `tests/services/test_chat_file_parser.py`

| 用例 | 断言 |
|------|------|
| `test_short_long_threshold` | 5000/5001 字符 category |
| `test_truncation_flag` | 超长 content_length ≤ MAX |
| `test_clean_document_images` | 本地 MD 图 → 图注 |
| `test_invalid_extension` | unknown → failed 或 mineru mock |
| `test_csv_parse` | 制表符连接 |

Fixtures：最小 PDF/DOCX 字节样本放 `tests/fixtures/`。

### `directory_service` — `tests/services/test_directory_service.py`

| 用例 | 断言 |
|------|------|
| `test_rename_path_boundary` | `a`→`b` 不改 `ab/x` |
| `test_move_into_self` | 返回 false |
| `test_sync_creates_parents` | 缺父路径自动创建 |

需 async DB：pytest-asyncio + sqlite 或 testcontainers MySQL。

---

## 2. 集成测试（API）

| 文件 | 场景 |
|------|------|
| `tests/api/test_chat_files_upload.py` | multipart 上传 long_doc，GET 列表含 file_id |
| `tests/api/test_my_files_attach.py` | upload → attach → 内部 search 有结果 |
| `tests/api/test_my_files_delete_index.py` | attach → delete → search 空 |
| `tests/api/test_files_download_auth.py` | 403/200 鉴权 |

标记：`@pytest.mark.integration`，CI 可选 job。

---

## 3. Worker / 入库

| 用例 | 环境 |
|------|------|
| 双实例 ingest 单 file_id | Redis + MySQL |
| 同名 file_name 双 file_id | 断言 MinIO 内容不同 |

Mock：`get_rag_service().process_multimodal`。

---

## 4. 前端 E2E（可选）

见 [pass5/01_frontend_integration.md](./01_frontend_integration.md)。

---

## 5. CI 建议

```yaml
# 伪配置
- unit: pytest tests/services -q
- integration: pytest tests/api -m integration --maxfail=3
```

覆盖率目标（首期）：`chat_file_index.split_text/search` 行覆盖 >70%。

---

## 6. 与 PR 映射

| PR | 必测用例 |
|----|----------|
| PR-4 删除+索引 | `test_my_files_delete_index` |
| PR-3 目录 replace | `test_rename_path_boundary` |
| PR-5 overlap | `test_split_text_overlap` |
| PR-6 锁 | 并发 test（可选 stress） |

---

*2026-05-30*
