# 第三遍补充：`chat_file_parser.py`

## 1. 双路由白名单差异

| 扩展名 | `chat_files.ALLOWED` | `my_files.ALLOWED` |
|--------|---------------------|-------------------|
| `.ini`, `.cfg` | ✗ | ✗（parser 支持，路由未放行） |
| `.toml` | ✗ | ✗ |
| 其余 parser 集合 | 基本一致 | 基本一致 |

`PYTHON_PARSEABLE_EXTENSIONS` 含 `.ini/.cfg/.toml`，但两路由均未加入 `ALLOWED_EXTENSIONS`——**解析能力闲置**。

---

## 2. 数据流与存储耦合

```text
┌────────────┐
│ file_bytes │
└────────────┘

  file_bytes ──────────────► parse

  parse ──────────────► PR

  PR ──────────────► meta

  meta ──────────────► ChatFileIndex 仅 long_doc

  ChatFileIndex 仅 long_doc ──────────────► (MySQL JSON)

  (MySQL JSON) ──────────────► multimodal: image_base64 原图

```

| category | content 进 DB | 原二进制 |
|----------|---------------|----------|
| multimodal | `image_base64` | MinIO |
| short_doc | 全文 ≤5k | MinIO |
| long_doc | 全文 ≤500k + 索引块 | MinIO |

**风险**：500k 字符 JSON + base64 多模态（体积≈4/3×原图）可触 MySQL `max_allowed_packet` / JSON 列限制。

---

## 3. MinerU 与知识库入库 Worker 对比

| 维度 | `ChatFileParser._try_mineru` | `PendingIngestProcessor._process_pdf_file` |
|------|------------------------------|---------------------------------------------|
| 触发 | 对话上传解析失败 | KB `PendingFile` status=1 |
| 入口 | `MinerUOCR.process_file_async` | `rag_service.process_multimodal` |
| 缓存路径 | tempfile | `data/cache/{file_name}`（同名冲突） |
| 输出 | 文本进会话 metadata | 向量/图谱进 KB |

两套 OCR/解析链路**不共用**缓存与配额，运维需分别监控 MinerU QPS。

---

## 4. `_parse_excel` 与 `.xls`

`openpyxl` **不支持**旧版 `.xls`（BIFF）。当前分支：

```python
elif ext in (".xlsx", ".xls"):
    return self._parse_excel(data)
```

失败 → 非 MinerU 白名单 → **直接 failed**，用户看到「无法解析」。

---

## 5. `_parse_ppt` vs `.ppt`

仅 `(".pptx", ".ppt")` 进 `_parse_pptx`，旧 `.ppt` 二进制格式 `python-pptx` **不支持**，行为同 `.xls`。

---

## 6. `clean_surrogates` 与 JSON

路由在写入 DB 前对 **整个 metadata dict** 调用，含 `image_base64` 时：

- 对已是合法 UTF-8 的 base64 字符串做 utf-16 往返，**开销大**（大图上传 CPU 峰值）。
- 建议在 multimodal 分支仅清理非 base64 字段，或跳过 base64 键。

---

## 7. 异常与可观测性

| 失败点 | 用户可见 | 日志级别 |
|--------|----------|----------|
| 文本解码失败 | failed + error | warning |
| 可选库缺失 | 走 MinerU 或 failed | debug |
| MinerU 未配置 | failed「MinerU 服务未配置」 | warning |
| MinerU 异常 | failed + `str(e)` | error |

**缺口**：无 metrics（解析耗时、method 分布、MinerU 成功率）。

---

## 8. 第三遍新增问题

### P1-NEW 个人文件夹上传不校验「索引策略」一致性

`my_files` 上传 `long_doc` 时不建索引；用户 attach 时才建。若 `metadata.content` 被后续 API 截断或清空，attach 静默失败（仅 error log）。

### P2-NEW `get_file_category_hint` 未被路由使用

路由用独立 `ALLOWED_EXTENSIONS`，hint 函数可能死代码或仅前端预检用——需确认调用方。

### P2-NEW CSV 无行列上限

恶意大 CSV 可生成极大字符串，在 `to_thread` 中占满内存。

### P2-NEW SVG 多模态

`image/svg+xml` 送 VL 模型，部分模型不支持或存在 XSS 面（若模型渲染 SVG）——产品/安全需确认。

---

## 9. 建议测试矩阵（扩充）

| case | 断言 |
|------|------|
| 空 PDF | failed 或 MinerU |
| 扫描版 PDF 乱码 | `_is_valid_content` false → MinerU |
| 5MB DOCX | 耗时 < 阈值，category 正确 |
| metadata 含 surrogate 的 short_doc | DB insert 成功 |
| `.xls` 样本 | failed 信息明确 |

---

## 10. 第三遍结论

解析器职责边界清晰，主要风险在 **路由白名单⊂解析能力、DB 巨型 JSON、与 KB ingest 双轨 MinerU**。优先统一白名单、long_doc 存储策略（索引-only vs 全文入库）。
