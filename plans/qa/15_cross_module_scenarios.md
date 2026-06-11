# 15 · 综合场景题（跨模块）

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 3、4、8、15、16、17、20

> 每题按 **现象 → 根因分析 → 逐步排查 → 修复方案 → 预防** 展开，适合模拟「线上告警值班」和深度技术面。

---

## 场景 1：首 token 延迟 8 秒

**现象：** 用户未选 Skill，问一句简单定义题，SSE 约 8 秒才出现第一个字。

**一句话结论：** 大概率不是 LLM 本身慢，而是**路由走错路径**、**RAG 冷启动**或**关键词提取**在首请求叠加了秒级延迟。

#### 根因分析（按概率排序）

| 序号 | 可能原因 | 典型耗时 | 如何确认 |
|------|----------|----------|----------|
| 1 | WorkflowRegistry 误走 `full` 而非 `lightweight` | +多轮 tool loop | 日志 `[WorkflowRegistry] resolved=full` |
| 2 | lightweight 仍做双库 `multi_kb_search` | 2–5s/KB | 日志 `multi_kb_search`、Path-A 次数 |
| 3 | RAG/KB 实例冷启动 | 1–3s 首请求 | 同问题第二次是否变快 |
| 4 | hybrid 关键词走 LLM 慢路径 | ~2.5s | keywords 模块日志 |
| 5 | LLM 网关排队 | 不定 | `LLMPerformanceCallback` 首 token ms |

```text
首 token 延迟 8s
        │
        ▼
① grep "[WorkflowRegistry] resolved="  → 应为 lightweight
        │
        ▼
② lightweight 是否仍有 astream_events 多轮 tool?
        │  (应为「预检索 + 单次 LLM」)
        ▼
③ multi_kb 日志：几个 KB？Path-A 几次 vector？
        │
        ▼
④ 同一问题立刻再问 → 第二次是否变快？
        │  是 → 冷启动；否 → 检索/网关
        ▼
⑤ LLMPerformanceCallback 首 token ms
```



```
1. grep api.log: "[WorkflowRegistry] 解析结果: workflow="
   → 应为 lightweight

2. 若为 lightweight，看是否仍有 astream_events（full 才有）
   → lightweight 应是「预检索 + 单次 LLM」，无多轮 tool

3. 看 multi_kb 日志：几个 KB？Path-A 几次 vector 搜索？
   → 默认 MULTI_KB_PATH_A_QUERY_ONLY=1 时双库约 2 次

4. 同一问题立刻再问一次
   → 第二次快 → 冷启动；仍慢 → 检索/网关问题

5. 查 LLM 网关监控与 request_timeout
```

#### 修复方向

- 确认未选 Skill 时 API 不传空数组以外的 `selected_skills` 误触发 full
- 调低 `MULTI_KB_SEARCH_MAX_TOP_K`；确保 `MULTI_KB_PATH_A_QUERY_ONLY` 开启
- lifespan 预热 RAG + 活跃 KB（`http_server.py`）
- 轻量路径绑定单 KB（若产品允许）

#### 预防

- 监控 P95：Router mode 分布、首 token 延迟、RAG init 耗时
- 集成测试：lightweight 请求断言 mode + 最大 tool 轮次

**相关模块：** 02-Q1/Q2, 03-Q7, 09-Q6, 04-Q6

---

## 场景 2：回答引用了「核电子学库」而非具体论文

**现象：** 用户要求「给出处」，回答里 citation 是「核电子学库」，不是 PDF/论文标题。

**一句话结论：** chunk **元数据里缺少可展示的文献名**，工具层 fallback 到了 KB 标签名。

#### 数据流

**Citation 回退链路（ASCII 架构图）：**

```text
┌───────────────────────┐
│ Milvus chunk metadata │
└───────────────────────┘

  Milvus chunk metadata ──────────────► EX

  EX ──────────────► BL

  BL ──────────────► FB

  FB ──────────────► LLM 写入参考文献

  EX ──(解析失败)──► BL

```

```
Milvus chunk.metadata.source
  = "<kb_id_ne_...>::某路径"
        │
        ▼
┌─────────────────────────────┐
│ multi_kb_search             │
│ _extract_doc_name_from_chunk│
└──────────────┬──────────────┘
               │
       ┌───────┴───────┐
    解析成功          解析失败 / 命中黑名单
       │               │
       ▼               ▼
  论文/PDF 文件名    回退 KB_LABELS["核电子学库"]
       │               │
       └───────┬───────┘
               ▼
        LLM 把展示名写进参考文献
```

#### 排查

1. 打开工具返回的 raw chunk，看 `file_path` / `source` / `source_id` 字段
2. 入库链路是否写入完整文件名（RAGAnything ingest）
3. Prompt 是否明确要求「禁止以知识库名称作文献名」

#### 修复

- 修 `_normalize_doc_display_name` / `_extract_doc_name_from_chunk`（`multi_kb_search_tool.py`）
- ingest 时统一 `source` 格式为 `doc_id::human_readable_title.pdf`
- Agent Prompt 加 hard rule + 示例 citation 格式

#### 常见误区

- ❌ 以为是 LLM「不会引用」→ 往往是工具给的 display name 就是库名
- ❌ 只改 Prompt 不改 metadata → 模型无正确信息可引

**相关模块：** 06-Q6, 10-Q3

---

## 场景 3：深度研究确认后无进度

**现象：** 用户确认大纲后，SSE 长期 `running`，无 `process` 阶段更新。

**一句话结论：** 后台 **runner Task 未启动、已 crash 或状态机未推进** — 与 Kafka 无关。

#### 正确组件链

**深度研究状态机（ASCII 架构图）：**

```text
用户确认大纲
        │
        ▼
┌─────────────────────────┐
│ ResearchWorkflowState   │
│ awaiting_confirmation   │
│        → running        │
└────────────┬────────────┘
             │
             ▼
 run_deep_research_background() / TaskManager
             │
             ▼
 custom/tasks/runner.py  (API 进程内 asyncio，非 Kafka)
             │
             ▼
 SSE process 事件 → 前端进度条

✗ 不要查：Kafka consumer、Ingest Worker
```



**不要查：** Kafka consumer、Ingest Worker（深研不走这条线）。

```text
DBA 更新 llm_models
        │
        ▼
① base_url / api_key / model 字段可连通？
        │
        ▼
② is_active 是否误关唯一可用模型？
        │
        ▼
③ Redis 模型变更订阅是否推送不完整 JSON？
        │
        ▼
④ 新模型是否不支持 extra_body.thinking → 400？
        │
        ▼
⑤ 分开看：401=认证 vs 502=网关（勿混为一谈）
```



1. 新行的 `base_url`、`api_key`、`model` 字段是否可连通
2. `is_active` 是否误关掉了唯一可用模型
3. Redis 模型变更订阅是否推送了不完整 JSON
4. 新模型是否不支持 `extra_body.thinking` — 网关返回 400
5. 401 是认证问题还是 LLM 502 是网关问题 — **分开看 HTTP status**

#### 修复流程

1. 回滚 DB 行或切 `MODEL_CONFIG_SOURCE=yaml` 应急
2. 在 staging 用 `create_chat_model(name=...)` 单测连通
3. 变更走审批 + 自动 health check

**相关模块：** 09-Q2, 09-L3-Q11

---

## 场景 5：同一问题，NE 准 CSH 胡编

**现象：** 同一问法在 RadAI（NE）准确，在 CSH-AI 像通用模型乱答。

**一句话结论：** **域配置四件套**未对齐 — KB、Profile、Prompt、kb_ids 任一错都会「像没开 RAG」。

#### 四件套检查

**域配置对齐（ASCII 架构图）：**

```text
┌────────────┐
│ Q          │
└────────────┘

  Q ──────────────► NE

  NE ──────────────► CSH

  CSH ──────────────► 准确 grounded

  准确 grounded ──────────────► ERR

  ERR ──────────────► 像没开 RAG 胡编

  ERR ──(kb_ids/Prompt/Profile)──► 像没开 RAG 胡编

```

```
同一问题「渗透系数如何测定？」
        │
        ├─ RadAI (NE) ─────────────────────────────────────┐
        │   kb_ids → 核电子学 + 论文库                      │
        │   prompt_domain → ne                             │
        │   prompts.py                                     │
        │   RetrievalProfile → domain                      │
        │   ✓ 检索命中 → 准确回答                           │
        │                                                  │
        └─ CSH-AI (CSH) ───────────────────────────────────┤
            kb_ids → 仍传 NE 库 / 空  ← 常见错误            │
            prompt_domain → 未切 csh                        │
            未用 csh_prompts.py                             │
            Profile → general_qa 误配                       │
            ✗ 无 CSH chunk → 像通用模型乱答                  │
```

| 项 | NE 预期 | CSH 常见错误 |
|----|---------|--------------|
| `kb_ids` | 核电子学 + 论文库 | 仍传 NE 库或空 |
| `prompt_domain` | `ne` | 未切 `csh` |
| Prompt 文件 | `prompts.py` | 未用 `csh_prompts.py` |
| RetrievalProfile | `domain` | 误用 `general_qa` |

#### 排查

- API 请求 JSON 里的 `domain`、`kb_ids`
- 日志检索是否 hit CSH 库 chunk
- `retrieval_profile.py` 中 CSH kb_id 是否在 `_KNOWN_KB_PROFILES`

**相关模块：** 01-Q8, 03-Q3, 12-Q10

---

## 场景 6：工具调用 web_search 被拒绝

**现象：** 用户看到 Guardrail denied 或 Agent 说无法联网。

**一句话结论：** **Allowlist 预期行为** — 当前会话工具集里没有 `async_web_search`。

#### 判定树

**Guardrail 拒绝联网（ASCII 架构图）：**

```text
Guardrail denied + tool=async_web_search
        │
        ▼
┌─────────────────────────┐
│ 查 ToolAssembly 装配结果  │  本次会话 allowed_tools 含 web_search?
└────────────┬────────────┘
             │
     ┌───────┴───────┐
    否              是（罕见误配）
     │               │
     ▼               ▼
 use_search=false?   查 AllowlistProvider bug
 lightweight 路径?
     │
     ▼
 非 bug：需前端开「联网」或 full + use_search=true
 （Prompt 写「必须联网」仍会被 Allowlist 硬拦截）
```



#### 常见误区

- ❌ 以为是 Guardrail bug → 是安全设计
- ❌ 在 Prompt 里强制联网 → 仍会被 Allowlist 拦

**相关模块：** 11-Q2/Q6, 06-ToolAssembly

---

## 场景 7：Uvicorn 4 worker 后会话随机「失忆」

**现象：** 多轮对话偶发不记得上一轮，或 thread 内容回滚。

**一句话结论：** **SQLite checkpoint 多进程写 + 无 sticky session** — 经典分布式坑。

#### 机制

```
请求 1 → worker A → 写 checkpoints.db thread T
请求 2 → worker B → 读 thread T（可能读到旧 snapshot 或锁等待）
```

```
                    ┌─────────────────┐
                    │ Load Balancer   │
                    └────────┬────────┘
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   ┌──────────┐        ┌──────────┐        ┌──────────┐
   │ Worker 1 │        │ Worker 2 │  ...   │ Worker 4 │
   └────┬─────┘        └────┬─────┘        └────┬─────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                            ▼
                 ┌─────────────────────┐
                 │ checkpoints.db      │
                 │ SQLite 多进程写竞争   │
                 └─────────────────────┘

请求1 thread T → Worker1 写 checkpoint
请求2 thread T → Worker2 读 → 可能旧 snapshot → 「失忆」
```

```text
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ 请求2 thread T   │  │ LB             │  │ 请求1 thread T   │
└────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ W2             │  │ W1             │  │ Worker 3       │  │ Worker 4       │
└────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
         (读旧 snapshot)
           │
           ▼
           │
           ▼
           │
           ▼
         (写)
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌───────────────────────────┐
│ 会话「失忆」         │  │ (checkpoints.db / 多进程写竞争) │
└────────────────┘  └───────────────────────────┘

```

#### 解决方案（选一或组合）

| 方案 | 说明 |
|------|------|
| PostgreSQL checkpointer | LangGraph 官方支持，多写者更稳 |
| Sticky session | LB 按 cookie 粘到同一 worker |
| `--workers 1` | 临时应急，吞吐下降 |
| thread 存储迁 Redis/DB | 架构改动大 |

**相关模块：** 05-L3-Q11, 04-L3-Q12

---

## 场景 8：Ingest 成功但检索为空

**现象：** `PendingFile` 已成功，Agent 仍「库中未找到」。

**一句话结论：** **「入库成功」≠「可检索就绪」** — 向量、keyword 索引、错误 kb_id 都可能。

#### 分层验证

**检索为空排查（ASCII）：**

```
PendingFile status=2（入库成功）
        │
        ▼
① Milvus collection vector count 是否增加？
        │ 否 → ingest 部分失败
        ▼
② keyword_index 是否 build 完成？（Path-A 依赖）
        │ 否 → Path-A 零召回
        ▼
③ Agent/API 检索 kb_id == ingest kb_id？
        │ 否 → 查错库
        ▼
④ Profile 是否 RAW + Stage-1 零召回？
        │ 是 → 调 Profile / Stage-1
        ▼
⑤ rag/sync 调度器是否 lag？
        │ 是 → 索引 drift
        ▼
   仍空 → 其他 bug
```

```
1. Milvus：该 kb collection 是否有 vector count 增加？
2. keyword_index：Path-A 依赖的索引是否 build 完成？
3. 检索 kb_id 是否与 ingest kb_id 一致？
4. Profile 是否 RAW + Stage-1 零召回？
5. rag/sync 调度器是否 lag？
```

```text
POST /api/v2/explore/quiz
        │
        ▼
┌─────────────────────┐
│ retrieve_context    │  top_k / kb_id / domain
└──────────┬──────────┘
           │
   ┌───────┴───────┐
  成功            失败或 chunk 过少
   │               │
   ▼               ▼
 LLM 基于 chunk   silent fallback 纯 LLM
 生成客观题       → 习题与 KB 章节无关
   │               │
   └───────┬───────┘
           ▼
    前端展示（用户难以区分是否 grounded）
```



#### 修复

- 检索失败时 API 显式 4xx/5xx，不 silent 生成
- 提高 explore 专用 top_k
- 客观题优先规则校验；简答题给要点 rubric
- 前端标注「AI 生成，请以 KB 原文为准」

**相关模块：** 12-Q4/Q7

---

## 场景 10：追问建议总是三句泛化话

**现象：** Follow-up 与刚检索的论文无关，永远是「能否详细说明…」。

**一句话结论：** **`tool_summary` 为空 → JSON 解析失败走模板兜底**。

#### 追问建议 fallback 链

**Follow-up 降级（ASCII 架构图）：**

```text
Agent SSE 主回答完成
        │
        ▼
FollowUpSuggestionService.note_process_event
  累积 tool_start/end 摘要
        │
        ▼
 build_tool_summary() 是否为空？
        │
   ┌────┴────┐
  非空       空
   │         │
   ▼         ▼
 LLM 生成   直接走 generic 模板
 JSON 追问   「能否详细说明…」×3
   │
   ├─ 解析成功 → 相关追问
   └─ 解析失败 → 同上模板兜底
```



#### 排查

1. SSE 是否有 `tool_end` / `tool_result` 被 `FollowUpSuggestionService.note_process_event` 消费
2. `build_tool_summary()` 是否为空
3. LLM 返回是否非 JSON → 正则解析失败
4. `use_llm=false` 或用了过小模型

#### 修复

- 确保 StreamHandler 转发 tool metadata（papers/titles）
- 加强 follow_up prompt 的 JSON 约束 + 重试
- 无 tool 时改用基于 answer 的 heuristic 追问而非 generic 模板

**相关模块：** 12-Q6/Q7

---

## 场景 11：Ingest Worker OOM

**现象：** 大 PDF 入库时 `ingest_worker` 被系统 kill。

**根因：** 单进程解析+向量化+批量写 Milvus 峰值内存过高。

#### 内存峰值链路

**Ingest OOM（ASCII 架构图）：**

```text
┌────────────┐
│ 大 PDF      │
└────────────┘

  大 PDF ──────────────► MU

  MU ──────────────► CH

  CH ──────────────► EM

  EM ──────────────► Milvus upsert

  Milvus upsert ──────────────► 内存峰值 OOM kill

```

```
大 PDF (500+ 页)
        │
        ▼
┌─────────────────┐
│ MinerU 解析      │  ← 峰值 1：OCR/布局
└────────┬────────┘
         ▼
┌─────────────────┐
│ chunk 批量内存   │  ← 峰值 2：全量 chunk 在 RAM
└────────┬────────┘
         ▼
┌─────────────────┐
│ embedding 批量   │  ← 峰值 3：向量矩阵
└────────┬────────┘
         ▼
┌─────────────────┐
│ Milvus upsert    │
└────────┬────────┘
         ▼
   ingest_worker OOM kill
```

**缓解：** 降低 ingest `--workers` 并发；调 chunk batch size；大文件拆分；容器 memory limit + 失败重试；MinerU 独立进程限内存。

**相关模块：** 07-L3-Q12, 10-Q3

---

## 场景 12：CAS 登录跳转到恶意站点

**现象：** 安全扫描报告 CAS 回调开放重定向。

**根因：** `redirect` 参数未校验为相对路径/白名单域名。

#### 开放重定向攻击面

**CAS 回调校验（ASCII 架构图）：**

```text
GET /api/v1/auth/cas/callback?ticket=...&redirect=...
        │
        ▼
┌─────────────────────────┐
│ 解析 redirect 目标 URL   │
└────────────┬────────────┘
             │
     ┌───────┴───────┐
  相对路径 /foo      绝对 URL //evil.com
     │               │
     ▼               ▼
  允许跳转          开放重定向漏洞
                    → 钓鱼 / 恶意站点

修复：只允许 / 开头相对路径 或 ALLOWED_REDIRECT_HOSTS 白名单
回归：//evil.com、\evil、%2F%2Fevil 等 bypass
```



**修复：** 只允许 `/` 开头相对路径或配置 `ALLOWED_REDIRECT_HOSTS`；回归测试 `//evil.com`、`\evil` 等 bypass。

**相关模块：** 05-Q7, 11-Q8

---

## 设计题 A：一天内支持「仅 RAG、无 LLM 重排」廉价 FAQ 模式

**需求：** 最低成本回答高频 FAQ。

**参考方案：**

**廉价 FAQ 模式（ASCII 架构图）：**

```text
┌────────────┐
│ FAQ Query  │
└────────────┘

  FAQ Query ──────────────► R

  R ──────────────► MK

  MK ──────────────► TPL

  TPL ──────────────► SSE 直出

```

```
Router 新增 retrieve_only（或 lightweight 子模式）
        │
        ▼
┌─────────────────┐
│ multi_kb_search │  Profile=lightweight，关联网
│ /retrieve_context│
└────────┬────────┘
         ▼
  模板拼 answer（cite top-3 chunk）
         │
         ▼
  跳过 LangGraph 多轮 loop → SSE 直出
         │
         ▼
  可选：query hash → Redis 缓存 chunks
```

**权衡：** 无 LLM 润色，UX 生硬但成本极低；需防 cache 陈旧。

---

## 设计题 B：Skill 驱动 PPT 生成端到端

**现有组件：** `html-ppt` Skill + `html_ppt_tool` + ToolAssembly + SSE artifact

**缺口：** Skill→Workflow 映射、artifact 持久化 MinIO、失败重试、版式校验

**路线图：**

**PPT 端到端（ASCII 架构图）：**

```text
┌────────────────┐
│ html-ppt Skill │
└────────────────┘

  html-ppt Skill ──────────────► R

  R ──────────────► TA

  TA ──────────────► AG

  AG ──────────────► PPT

  PPT ──────────────► ART

  ART ──────────────► MinIO 持久化

  MinIO 持久化 ──────────────► 前端预览

```

```
用户选 html-ppt Skill
        │
        ▼
Router → full + ToolAssembly 注入 html_ppt + read_local_skill_file
        │
        ▼
Agent: read_local_skill_file → generate_html_ppt
        │
        ▼
StreamHandler → SSE artifact + MinIO URL
        │
        ▼
OutputValidator 检查 HTML 结构 → 前端预览 / 下载
```

1. Skill frontmatter 声明 `tools: [html_ppt_tool]`
2. Router：选 Skill → 强制 full + 限定 max tool 轮次
3. StreamHandler 发 `artifact` + MinIO URL
4. OutputValidator 检查 HTML 结构
5. 前端预览 + 下载

**相关模块：** 06-Q15, 02-L3-Q10, 08-附录
