# 06 · 工具与 Skills

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 6、7、11、**22、31**；正式文档 → [skills_tools_module.md](../../docs/docs_backend/skills_tools_module.md) · [datasets_module.md](../../docs/docs_backend/datasets_module.md)

> 本模块答案采用**分层讲解**：先给结论，再解释「为什么」，最后用项目里的真实代码和场景串起来。

---

## L1 初级 🟢

### Q1：本项目 Agent 有哪些典型工具？

**一句话结论：** Agent 的「手」在 `backend/application/tools/` 和 `backend/core/` 里，通过 LangChain `@tool` 装饰器定义，由 `ToolAssemblyPipeline` 按会话动态装配；核心能力是**搜知识库、读 Skill、联网、分析 Excel、生成 HTML PPT** 等。

#### 背景：为什么需要「工具」？

大模型本身只能生成文字，无法直接查 Milvus、读磁盘上的 Excel、或调用 Tavily 搜索。工具（Tool / Function Calling）是 Agent **连接外部世界的桥梁**：模型决定「调哪个工具、传什么参数」，后端执行后把结果塞回对话，模型再据此组织回答。

#### 详细解释：工具在本项目中的分类

**工具装配链路（ASCII 架构图）：**

```text
┌──────────────┐  kb_ids/skills   ┌──────────────────────┐
│ StandardAgent │ ───────────────► │ ToolAssemblyPipeline │
│ mode         │  use_search      │ .assemble()          │
└──────────────┘                  └──────────┬───────────┘
                                             │
              ┌──────────────┬───────────────┼───────────────┐
              ▼              ▼               ▼               ▼
      multi_kb_search   web_search   read_local_skill   generate_html_ppt
                        (Tavily)     _file 等           等 Skill 工具
              │              │               │               │
              └──────────────┴───────────────┴───────────────┘
                                             │
                                             ▼
                              ToolGuardrailMiddleware (白名单)
                                             │
                                             ▼
                                   LangGraph tools 节点
```



| 类别 | 代表工具 | 做什么 | 典型触发场景 |
|------|----------|--------|--------------|
| **RAG 检索** | `multi_kb_search` | 并发查多个 KB，RRF 融合结果 | 「核电子学前置放大器原理是什么？」 |
| **会话文件** | `chat_file_search` | 查当前 thread 上传的长文档 | 「我刚上传的 PDF 第 3 章说了什么？」 |
| **联网** | `web_search`（`async_web_search_tool`） | Tavily 等异步搜索 | 「2025 年最新核安全法规」 |
| **Skill 读取** | `read_local_skill_file`、`read_skill_reference` | 渐进加载 Markdown 工作流 | 用户选了 html-ppt Skill |
| **领域专用** | `generate_html_ppt`、`academic_search`、Excel 分析套件 | Skill 声明的专用能力 | 做演示文稿、搜论文、分析实验数据 |
| **深度研究** | `save_deep_research_report` | 保存研究报告 artifact | 深度研究执行阶段 |
| **限流辅助** | `request_limits`（非 Agent 可见工具） | 请求级预算与并发锁 | 防止 Agent 循环检索打爆配额 |

工具不是「全局固定列表」，而是**每次请求按 WorkflowRegistry 工作流、selected_skills、kb_ids、thread_id 动态组装**。

#### 项目实现

1. **定义层**：`application/tools/*.py` 用 `@tool` 装饰器声明函数签名与 docstring（模型读 docstring 决定何时调用）。
2. **装配层**：`ToolAssemblyPipeline.assemble()`（`application/tools/tool_assembly_pipeline.py`）根据会话参数注入工具。
3. **白名单层**：`ToolGuardrailMiddleware` + `AllowlistProvider` 只允许调用本次装配过的工具名。
4. **基础工具池**：`core/agents/tool_registry.py` 的 `get_available_tools()` 提供子 Agent、澄清等 core 层原生工具。

轻量模式（`lightweight_mode=True`）时 Pipeline **直接返回空列表**——LightweightQAAgent 内部自己绑 `multi_kb_search`，不走全量装配。

#### 具体例子

用户在前端选了 Skill `html-ppt`，并绑定核电子学 KB：

```
WorkflowRegistry 解析为 full
  → ToolAssemblyPipeline(selected_skills={"html-ppt"}, kb_ids=[...])
  → 注入：multi_kb_search, read_local_skill_file, generate_html_ppt, web_search ...
  → Agent 先 read_local_skill_file("html-ppt") 读工作流
  → 再 generate_html_ppt(...) 生成幻灯片
  → SSE 推送 artifact 事件给前端预览
```

#### 常见误区

- ❌ 「所有 Agent 都有联网工具」→ 只有 `use_search=True` 且 full 模式才注入 `web_search`。
- ❌ 「工具 = Skill」→ Skill 是 Markdown 说明书；工具是可执行 Python 函数。
- ❌ 「multi_kb_search 是独立文件里的固定工具」→ 实际是 Pipeline 内**闭包包装**的 `selected_multi_kb_search`，绑定了当前请求的 `kb_ids`。

#### 延伸追问（L2/L3）

- L2：Q6 `multi_kb_search` 返回什么格式？Q7 Skill 读取工具的区别？
- L3：Q12 ToolAssembly 如何扩展？Q13 多 KB 与会话文件如何共存？

---

### Q2：Skill 是什么？和 Tool 有什么区别？

**一句话结论：** **Skill 是「怎么做」的 Markdown 说明书，Tool 是「能执行」的 Python 函数**；Skill 通过 `read_local_skill_file` 渐进加载，Tool 通过 function calling 直接调用。

#### 背景：为什么不能把 Skill 也做成 Tool？

一个 html-ppt Skill 的 `SKILL.md` 加上 `references/` 模板可能有**数万 token**。若一次性塞进 System Prompt，会：
- 浪费 context 窗口（大部分内容本轮用不到）
- 增加首 token 延迟和 LLM 费用
- 降低模型对当前用户问题的注意力

因此项目采用 **Progressive Loading（渐进加载）**：Agent 先读入口 `SKILL.md`，需要时再读 `references/` 子文档。

#### 详细解释：对比表

| 维度 | Skill | Tool |
|------|-------|------|
| **形态** | 目录 + `SKILL.md` + 可选 `references/` | `@tool` 装饰的 async/sync 函数 |
| **位置** | `backend/skills/public/`、`application/` | `backend/application/tools/`、`core/` |
| **加载方式** | Agent 调用 `read_local_skill_file("skill-name")` | LangGraph 直接执行 function call |
| **内容性质** | 工作流步骤、输出规范、领域知识 | 副作用操作（检索、写文件、调 API） |
| **谁决定用** | 用户前端选择 + WorkflowRegistry 解析 full | 模型根据 docstring 自主决定 |
| **token 策略** | 按需读，大内容可被 ContextPruning 提升到 state | 返回结果可被裁剪/隔离 |

```text
┌────────────┐
│ 用户选择 Skill │
└────────────┘

  用户选择 Skill ──────────────► SKILL.md 说明书

  SKILL.md 说明书 ──────────────► Agent

  Agent ──────────────► read_local_skill_file

  read_local_skill_file ──────────────► multi_kb_search

  multi_kb_search ──────────────► generate_html_ppt

  generate_html_ppt ──────────────► (RAG 检索)

  (RAG 检索) ──────────────► HTML artifact

```

#### 项目实现

**Skill 加载链路（ASCII 架构图）：**

```text
┌────────────────────┐
│ 前端 selected_skills │
└────────────────────┘

  前端 selected_skills ──────────────► TA

  TA ──────────────► R

  R ──────────────► CP

  CP ──────────────► Agent 按 Skill 工作流执行

```

```
前端 selected_skills=["html-ppt"]
        │
        ▼
┌──────────────────────┐
│ ToolAssemblyPipeline │ 注入 read_local_skill_file / read_skill_reference
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐     大段 Markdown     ┌─────────────────┐
│ Agent 调用           │ ────────────────────► │ ContextPruning  │
│ read_local_skill_file│                     │ → loaded_skills │
└──────────┬───────────┘                     └────────┬────────┘
           │                                          │
           ▼                                          ▼
   generate_html_ppt 等声明工具              JIT 注入 SystemMessage
```

**Skill 加载链路（文字版）：**

```
前端 selected_skills=["html-ppt"]
  → ScopedSkillsProvider.get_available_skills_for_user() 过滤
  → ToolAssembly 注入 read_local_skill_file / read_skill_reference
  → System Prompt 写入 <available_skills> XML 列表（只含 name + description，不含全文）
  → Agent 运行时调用 read_local_skill_file("html-ppt")
  → mark_skill_loaded("html-ppt") 标记已加载
  → 部分 Skill 要求先加载才能调 multi_kb_search（如 nuclear-electronics）
```

**Skill 声明专用工具：** `SKILL.md` frontmatter 或 parser 解析出的 `tool_names` 列表，由 `_inject_declared_skill_tools()` 动态注入对应 Python 工具。

#### 具体例子

| 用户意图 | Skill 作用 | Tool 作用 |
|----------|-----------|-----------|
| 做 HTML 演示文稿 | `html-ppt/SKILL.md` 规定 deck 结构、主题、presenter mode | `generate_html_ppt` 实际写 HTML 文件 |
| Excel 实验分析 | `excel-experiment-analysis/SKILL.md` 规定分析步骤与图表规范 | `inspect_excel_data`、`visualize_excel_data` 等执行计算 |
| 核电子学问答 | `nuclear-electronics/SKILL.md` 规定引用格式、检索策略 | `multi_kb_search` 执行检索 |

#### 常见误区

- ❌ 「选了 Skill 就自动加载全文」→ 只注入**元数据**到 Prompt，全文需 Agent **主动调** `read_local_skill_file`。
- ❌ 「Skill 可以当函数名调用」→ Prompt 明确警告：**DO NOT call skill name as a tool**。
- ❌ 「Skill 和 Tool 完全独立」→ Skill 的 `tool_names` 声明会触发 Tool 注入，二者是**协作关系**。

#### 延伸追问（L2/L3）

- L2：Q4 Agent 如何加载 Skill？Q7 两个 read 工具的区别？
- L3：Q11 为何 Skill 还不能驱动独立工作流？

---

### Q3：有哪些公开的 Skill？

**一句话结论：** 公开 Skill 在 `backend/skills/public/`，每个子目录一个 `SKILL.md` 入口；与本项目业务强相关的主要有 **html-ppt、nuclear-electronics、excel-experiment-analysis、academic-search、deep-research** 等。

#### 背景

Skill 是**可插拔的能力包**。公开（`category=public`）Skill 对所有启用用户可见；后续可扩展 `custom` 类别做租户/用户级隔离（`ScopedSkillsProvider` 已预留 `user_id` 参数）。

#### 详细解释：主要 Skill 清单

| Skill 目录 | 用途 | 关联工具（若有） |
|------------|------|------------------|
| `html-ppt` | HTML 演示文稿生成规范 + 大量模板 | `generate_html_ppt` |
| `nuclear-electronics` | 核电子学领域问答与引用规范 | `multi_kb_search`（需先加载 Skill） |
| `excel-experiment-analysis` | 实验 Excel 分析流程 | Excel 分析工具套件 |
| `academic-search` | 学术文献搜索工作流 | `academic_search` |
| `deep-research` | 深度研究工作流说明 | 与深度研究 Orchestrator 配合 |
| `data-analysis` / `chart-visualization` | 通用数据分析与可视化 | 视装配而定 |
| `frontend-design` / `web-design-guidelines` | 前端/UI 设计规范 | 通常只需读 Skill，无专用 Tool |
| `literature-review-writing` | 文献综述写作 | — |
| 其他（`image-generation`、`video-generation` 等） | 扩展能力 | 部分尚未完全 Tool 化 |

完整列表以 `backend/skills/public/` 目录为准（当前约 19 个 Skill 入口）。

#### 项目实现

- **解析**：`core/skills/loader.py` 扫描 `config.yaml` → `skills.path: backend/skills` 下的目录。
- **元数据**：`core/skills/parser.py` 解析 frontmatter（name、description、tool_names 等）。
- **类型**：`core/skills/types.py` 的 `Skill` dataclass 封装路径、enabled 状态、category。

#### 具体例子

前端 Skill 选择器展示的名称 = Skill 的 `name` 字段（通常与目录名一致，如 `html-ppt`）。用户勾选后 API 传：

```json
{ "selected_skills": ["html-ppt", "nuclear-electronics"] }
```

WorkflowRegistry 解析有 Skill → 走 **full 工作流**，ToolAssembly 只暴露这两个 Skill 相关的读取工具与声明工具。

#### 常见误区

- ❌ 「public 下所有 Skill 都已生产可用」→ 部分 Skill 是旧架构模板迁移，需验证 Tool 是否已注册。
- ❌ 「Skill 名可以随意写」→ 必须与目录名、`read_local_skill_file` 参数一致。
- ❌ 「选多个 Skill 会合并成一个」→ 多个 Skill 并列注入，Agent 需判断本轮主要遵循哪个。

#### 延伸追问（L2/L3）

- L2：Q5 ScopedSkillsProvider 如何隔离？
- L3：Q15 如何新增 Skill？

---

### Q4：Agent 如何加载 Skill？

**一句话结论：** 用户前端选择 Skill → API 传 `selected_skills` → `ScopedSkillsProvider` 过滤 → `ToolAssemblyPipeline` 注入读取工具 + 声明工具 → Agent 运行时**主动**调用 `read_local_skill_file` 渐进加载。

#### 背景

Skill 加载是**拉模式（pull）**而非**推模式（push）**：系统不会在请求入口把 SKILL.md 全文塞进 Prompt，而是告诉 Agent「有哪些 Skill 可用、路径在哪」，由模型决定何时读取。

#### 详细解释：五步链路

```
① 用户选择
   前端 Skill 选择器 → QueryRequest.selected_skills: ["excel-experiment-analysis"]

② 作用域过滤
   ScopedSkillsProvider.get_available_skills_for_user(selected_skills=...)
   → 只保留 enabled + category in (public, custom) + 名称匹配的 Skill

③ Prompt 注入（元数据 only）
   build_skills_prompt_section() → <available_skills> XML
   含 name、description、container 路径；不含 SKILL.md 正文

④ 工具注入
   create_native_skill_tools() → read_local_skill_file, read_skill_reference, list_available_skills
   _inject_declared_skill_tools() → 按 Skill.tool_names 注入 generate_html_ppt 等

⑤ 运行时加载
   Agent 调 read_local_skill_file("excel-experiment-analysis")
   → skills_provider.read_skill_content() 读磁盘
   → mark_skill_loaded() 标记
   → ContextPruningMiddleware 可能将大内容提升到 retrieved_evidence / loaded_skills state
```

```text
① 前端 selected_skills=["html-ppt"]
        │
        ▼
② ScopedSkillsProvider 过滤 (enabled + 作用域)
        │
        ▼
③ build_skills_prompt_section → <available_skills> XML（仅元数据）
        │
        ▼
④ ToolAssemblyPipeline 注入 read_local_skill_file + 声明工具
        │
        ▼
⑤ Agent 运行时 read_local_skill_file → ContextPruning State Elevation
```



#### 项目实现

关键文件：

| 文件 | 职责 |
|------|------|
| `core/skills/scoped_skills_provider.py` | 过滤、读内容、路径遍历防护 |
| `core/skills/native_skill_tools.py` | 三个原生 Skill 工具 |
| `application/tools/tool_assembly_pipeline.py` | 装配 Skill 工具 + 声明工具 |
| `core/skills/skill_runtime_state.py` | `has_loaded_skill()` / `mark_skill_loaded()` |
| `config.yaml` → `skills.path` | Skill 根目录配置 |

**安全：** `read_skill_reference` 用 `resolve().relative_to(skill_dir)` 防路径遍历；`selected_skills` 不匹配时直接 `ValueError`。

#### 具体例子

核电子学 Skill 强制「先读后搜」：

```python
# tool_assembly.py 内 multi_kb_search 闭包
if "nuclear-electronics" in self._selected_skills and not has_loaded_skill("nuclear-electronics"):
    return json.dumps({"error": "skill_not_loaded", "message": "You MUST call read_local_skill_file(...)"})
```

Agent 若跳过读 Skill 直接检索，会收到 JSON 错误而非检索结果，Prompt 引导其先加载工作流。

#### 常见误区

- ❌ 「选了 Skill 就等于 loaded」→ `selected_skills` 只是**可见范围**；`has_loaded_skill` 才表示读过。
- ❌ 「Skill 路径用户可任意指定」→ 只能读 Provider 白名单内的 skill_name。
- ❌ 「lightweight 模式也能用 Skill」→ WorkflowRegistry 无 Skill 时解析 lightweight，**不注入** Skill 工具链。

#### 延伸追问（L2/L3）

- L2：Q7 `read_local_skill_file` vs `read_skill_reference`？
- L3：Q11 Skill 与工作流 Executor 的未来架构？

---

## L2 中级 🟡

### Q5：`ScopedSkillsProvider` 如何实现作用域隔离？

**一句话结论：** 它根据 `selected_skills`、Skill 的 `enabled` 与 `category` 过滤可见 Skill，并在读文件时做**路径遍历防护**；`user_id` 参数已预留，多租户按 org 分目录是天然扩展点。

#### 背景

多用户场景下，不能让 Agent A 读到租户 B 的 custom Skill，也不能让未选择的 Skill 被 `read_local_skill_file` 任意读取。Provider 是 Skill 层的**访问控制边界**。

#### 详细解释

**当前已实现的隔离：**

1. **选择集过滤**：`selected_skills is not None` 时，只返回名称交集内的 Skill。
2. **类别过滤**：仅 `category in ("public", "custom")` 且 `enabled=True`。
3. **读时校验**：`read_local_skill_file` 在 `native_skill_tools.py` 中检查 `skill_name not in selected_skills` → 拒绝。
4. **路径安全**：`read_skill_reference` 解析路径后必须 `relative_to(skill_dir_resolved)`，否则 `PermissionError`。

**尚未完全实现（预留）：**

```python
# scoped_skills_provider.py
_ = user_id  # 预留用于后续基于 user_id 的权限控制
```

未来可扩展：按 `org_id` 子目录、DB 绑定表、角色权限过滤 custom Skill。

#### 项目实现

```python
# get_available_skills_for_user 核心逻辑
enabled_skills = [s for s in all_skills if s.enabled and s.category in ("public", "custom")]
if selected_skills is not None:
    enabled_skills = [s for s in enabled_skills if s.name in selected_skills]
```

`build_skills_prompt_section()` 生成的 XML 只包含过滤后的 Skill，避免 Prompt 泄露未授权 Skill 的存在（若前端不传 selected_skills，则暴露所有 enabled public/custom——生产环境应始终传 selected_skills）。

#### 具体例子

| 场景 | selected_skills | 结果 |
|------|-----------------|------|
| 用户只选 html-ppt | `{"html-ppt"}` | 只能 read html-ppt；调 read_local_skill_file("deep-research") → ValueError |
| API 未传 selected_skills | `None` | 返回全部 enabled public/custom（开发便利，生产需谨慎） |
| Skill 被禁用 enabled=false | 任意 | 不出现在列表，读时 not found |

#### 常见误区

- ❌ 「Provider 等于 ToolGuardrail」→ Provider 管 **Skill 文件读权限**；ToolGuardrail 管 **工具调用白名单**。
- ❌ 「路径遍历只靠 Provider」→ 需与 `native_skill_tools`、MinIO 上传路径校验**纵深配合**。
- ❌ 「user_id 已生效」→ 当前代码中 user_id **被忽略**，面试应如实说明 + 提出扩展方案。

#### 延伸追问（L3）

- L3：Q15 新增 Skill 时如何测隔离？多租户 org 分目录怎么设计？

---

### Q6：`multi_kb_search_tool` 返回给 Agent 的数据格式是什么？

**一句话结论：** 返回**结构化 JSON 字符串**（或带说明的文本），含各 KB 标签、chunk 正文、文献展示名与元数据；过大时由 `ContextPruningMiddleware` 提升到 `retrieved_evidence`，消息里只留指针。

#### 背景

Agent 需要同时理解「哪个库来的」「哪篇文章」「原文片段是什么」，才能正确引用。工具层还要**屏蔽**「核电子学库」这类容器名被误当文献名。

#### 详细解释：返回结构要点

`execute_multi_kb_search()`（`application/tools/multi_kb_search_tool.py`）典型返回字段：

| 字段/区块 | 含义 |
|-----------|------|
| KB 标签 | `KB_LABELS` 映射，如「核电子学库」「期刊论文库」 |
| chunk 内容 | 检索到的文本片段 |
| 文献展示名 | 从 `file_path`/`source`/`metadata.title` 提取，经 `_KB_DISPLAY_NAMES` 黑名单过滤 |
| 融合策略 | `merge_strategy="rrf"` 倒数排名融合 |
| 性能/错误 | 单库超时隔离，失败 KB 不影响其他 |
| 提示语 | 结果过大时提示「勿重复检索，请基于 retrieved_evidence 回答」 |

**文献名提取逻辑：**

```python
_KB_DISPLAY_NAMES = frozenset({"核电子学库", "期刊论文库"})  # 禁止作为引用名
# _extract_doc_name_from_chunk() 从 file_path/source 等取 basename，去后缀
```

```text
用户 Query
    │
    ▼
┌─────────────────┐   首个 KB 提取    ┌──────────────────────────┐
│ execute_multi_  │ ───────────────► │ hybrid 关键词 + embedding │
│ kb_search       │   后续 KB 复用    │ (避免双库各 ~2.5s LLM)    │
└────────┬────────┘                  └────────────┬─────────────┘
         │                                        │
         │         ┌──────────────────────────────┴──────────────┐
         │         ▼                              ▼                │
         │   ┌──────────┐                  ┌──────────┐           │
         └──►│ KB1 检索  │  asyncio 并发  │ KB2 检索  │           │
             │retrieve_  │ ◄────────────► │retrieve_  │           │
             │context    │                │context    │           │
             └─────┬────┘                └─────┬────┘           │
                   └──────────────┬──────────────┘               │
                                  ▼                              │
                         跨库 RRF 融合 + 文献名提取                 │
                                  │                              │
                                  ▼                              │
                    JSON 字符串 ToolMessage ──────────────────────┘
                                  │
                                  ▼
              ContextPruning → state.retrieved_evidence (JIT 注入)
```



#### 项目实现

- **并发**：`asyncio` 多 KB 并行，`KB_SEARCH_TIMEOUT=60s` 单库，`TOTAL_SEARCH_TIMEOUT=90s` 总超时。
- **关键词复用**：未传 hl/ll 时，**仅首个 KB** 做 hybrid 关键词提取，避免双库各 ~2.5s 重复 LLM 调用。
- **Path-A 优化**：默认 `MULTI_KB_PATH_A_QUERY_ONLY=1`，共享关键词后每 KB Path-A 约 1 次 keyword_index（双库合计 ~2 次而非 8 次）。
- **限流**：`request_limits.py` 的 `TURN_MAX_EVIDENCE_CHARS`（默认 8000）限制单轮累计检索字符；并行锁禁止同轮并发 multi_kb_search。
- **Context 隔离**：`ContextPruningMiddleware` 识别 RAG 类 ToolMessage → 追加到 `state["retrieved_evidence"]`，原消息替换为 `[State Elevation: ...]` 短指针。

#### 具体例子

Agent Prompt 要求：「引用时写具体论文/文件名，不要写库名。」

工具返回 chunk 的 `source` 为 `<kb_id_paper>/某期刊论文.pdf` → 展示名「某期刊论文」；若只能识别到 KB 标签 → 展示名为空或回退 kb_label，Prompt 引导模型不要写「据核电子学库所知」。

#### 常见误区

- ❌ 「返回的是 Python dict」→ Agent 收到的是 **JSON 字符串**（ToolMessage content）。
- ❌ 「top_k 越大越快」→ 代码注释明确：**提高到 10 通常更慢**，应调低 `MULTI_KB_SEARCH_DEFAULT_TOP_K`。
- ❌ 「一次检索结果永远在 messages 里」→ 超阈值会 State Elevation，需靠 JIT Assembly 临时注入 SystemMessage。

#### 延伸追问（L3）

- L3：Q13 与 chat_file_search 共存策略？Q12 双库慢如何优化？

---

### Q7：`read_local_skill_file` 和 `read_skill_reference` 的区别？

**一句话结论：** 前者读 Skill **入口** `SKILL.md`（工作流总纲），后者读 `references/` 下**按需子文档**（模板、主题指南等）；二者配合实现渐进加载。

#### 背景

html-ppt Skill 的 `SKILL.md` 可能只有流程说明，而 `references/full-decks/`、`references/themes/` 下有大量模板。全读会爆 context；分步读才能平衡**完整性**与**窗口成本**。

#### 详细解释

**渐进加载（ASCII 架构图）：**

```text
┌───────────────────────┐
│ read_local_skill_file │
└───────────────────────┘

  read_local_skill_file ──────────────► B

  B ──────────────► C

  C ──────────────► D

  D ──────────────► 模板/主题细节

  read_local_skill_file ──(SKILL.md 入口)──► B
  D ──(references/ 子文档)──► 模板/主题细节

```

```
Agent 需要 Skill 内容
        │
        ├─ 第一步 ──► read_local_skill_file("html-ppt")
        │                  │
        │                  ▼
        │            读 SKILL.md 工作流总纲
        │            mark_skill_loaded() ✓
        │
        └─ 按需 ──► read_skill_reference("html-ppt", "references/themes/tech-sharing")
                           │
                           ▼
                     读 references/ 下模板/主题
                     (不标记 loaded，可多次读)
```

| 工具 | 读什么 | 何时调 | 副作用 |
|------|--------|--------|--------|
| `read_local_skill_file(skill_name)` | `{skill_dir}/SKILL.md` | 匹配 Skill 后**第一步** | `mark_skill_loaded(skill_name)` |
| `read_skill_reference(skill_name, reference_path)` | `references/` 下指定 md 或路径 | 执行中需要模板/细节时 | 无 loaded 标记 |
| `list_available_skills()` | 元数据 JSON 列表 | 不确定有哪些 Skill 时 | — |

**reference 路径解析特性：**

- 支持 shorthand：`"references"` 可解析为 `references.md` 或返回 `references/index.md` 目录
- 找不到文件时列出 `skill_dir.rglob("*.md")` 可用参考清单，引导 Agent 逐个加载
- 路径必须落在 `skill_dir` 内，否则 `PermissionError`

#### 项目实现

```python
# native_skill_tools.py
@tool("read_local_skill_file")
async def read_local_skill_file(skill_name: str) -> str:
    content = skills_provider.read_skill_content(skill_name)
    mark_skill_loaded(skill_name)
    return content

@tool("read_skill_reference")
async def read_skill_reference(skill_name: str, reference_path: str) -> str:
    return skills_provider.read_skill_reference(skill_name, reference_path)
```

`ContextPruningMiddleware` 对 `read_local_skill_file` 的大输出做 State Elevation → `loaded_skills` state，JIT 时再组装进 SystemMessage。

#### 具体例子

html-ppt 典型调用序列：

```
read_local_skill_file("html-ppt")        → 了解 deck 结构、必须步骤
read_skill_reference("html-ppt", "references/themes/tech-sharing")  → 加载主题 CSS
generate_html_ppt(title=..., slides=...)  → 执行生成
```

#### 常见误区

- ❌ 「reference 可以读 Skill 目录外文件」→ 有 `relative_to` 校验，路径遍历会被拒。
- ❌ 「读 reference 也算 skill loaded」→ 只有 `read_local_skill_file` 调 `mark_skill_loaded`。
- ❌ 「SKILL.md 包含所有 reference 全文」→ 设计上就是分离的，靠 Agent 自律按需读。

#### 延伸追问（L3）

- L3：Q11 若 Skill 驱动 Workflow，reference 是否预加载？

---

### Q8：工具限流 `request_limits` 如何实现？

**一句话结论：** 用 **ContextVar** 在单请求内维护工具调用预算与 KB 检索字符累计，配合 **asyncio 锁** 禁止同轮并行 `multi_kb_search`，防止 Agent 循环打爆 API 与 context。

#### 背景

Agent 在工具 loop 中可能：
- 连续 10 次 `multi_kb_search` 微调 query
- 并行发起多个检索（async 工具）
- 单次检索返回数万字 chunk

不限流会导致：Milvus/QPS 飙升、LLM context 爆炸、用户等待分钟级。

#### 详细解释：三层限制

| 机制 | 实现 | 作用 |
|------|------|------|
| **工具次数预算** | `set_tool_budgets()` + `consume_tool_budget(tool_name)` | per-request 限制某工具最多调 N 次 |
| **KB 字符预算** | `TURN_MAX_EVIDENCE_CHARS`（默认 8000）+ `record_turn_kb_chars()` | 单轮累计检索上下文上限 |
| **并行锁** | `_MULTI_KB_REQUEST_LOCK` ContextVar | 同轮 multi_kb_search 串行化 |

```text
新用户消息到达
        │
        ▼
 reset_turn_kb_context_budget()     ← API 层重置本轮预算
        │
        ▼
┌───────────────────┐
│ multi_kb_search   │
│ 第 N 次调用        │
└─────────┬─────────┘
          │
          ▼
   获取 _MULTI_KB_REQUEST_LOCK ?
          │
    ┌─────┴─────┐
   否(等待)     是
    │           ▼
    │    TURN_MAX_EVIDENCE_CHARS 超限?
    │           │
    │     ┌─────┴─────┐
    │    是           否
    │     │           ▼
    │     ▼      执行检索 + record_turn_kb_chars
    │  返回 JSON         │
    │  提示勿重复         ▼
    └────────────► 释放锁 → ToolMessage
```



API 层在新用户消息开始时调用 `reset_turn_kb_context_budget()` 重置计数。

#### 项目实现

文件：`backend/application/tools/request_limits.py`

```python
_TOOL_BUDGETS: ContextVar[dict[str, int] | None]  # 工具名 → 剩余次数
_TURN_KB_CHARS_USED: ContextVar[int]              # 本轮已用字符
_MULTI_KB_REQUEST_LOCK: ContextVar[asyncio.Lock | None]  # 请求级锁
```

`multi_kb_search_tool` 在执行前检查预算与锁；超限时返回友好 JSON 提示 Agent 基于已有 evidence 作答。

可与 `SubagentLimitMiddleware` 配合：主 Agent 限工具次数，子 Agent 限并发子任务数。

#### 具体例子

| 情况 | 行为 |
|------|------|
| 第 1 次 multi_kb_search 返回 5000 字 | 累计 5000，剩余 3000 |
| 第 2 次试图再拉 5000 字 | 可能被截断或拒绝，提示勿重复检索 |
| 同轮两个 parallel tool_call 都是 multi_kb_search | 第二个等待锁，串行执行 |
| budgets 未设置（None） | `consume_tool_budget` 返回 `(True, None)`，不限制 |

#### 常见误区

- ❌ 「限流是全局 QPS」→ 当前是 **per-request ContextVar**，不是全站 Redis 限流。
- ❌ 「限流会 HTTP 429」→ 通常返回 **ToolMessage 内容**让 Agent 自适应，不中断 SSE。
- ❌ 「session 级永久限额」→ 每轮用户消息会 reset turn budget。

#### 延伸追问（L3）

- L3：Q13 双库双调导致延迟加倍，限流 vs WorkflowRegistry 默认策略？

---

### Q9：Excel 分析工具链包含哪些组件？

**一句话结论：** **工具层**（`excel_data_analysis_tool.py` + `excel_engine.py` + `excel_chart_generator.py`）负责算与画图，**Skill 层**（`excel-experiment-analysis/SKILL.md`）负责规定分析流程与输出格式，**chat_file 索引**负责让 Agent 找到用户上传的 Excel。

#### 背景

实验数据分析是垂直场景：需要读 xlsx、做统计、出 chart artifact，还要符合实验报告规范。单靠通用 Prompt 不稳定，所以 Skill + 多工具协作。

#### 详细解释：组件分工

**Excel 分析链路（ASCII 架构图）：**

```text
用户上传 experiment.xlsx
        │
        ▼
┌─────────────────┐     选 Skill      ┌──────────────────────────┐
│ chat_file_index │ ◄──────────────── │ excel-experiment-analysis │
│ BM25 索引        │                   │ SKILL.md 分析规范          │
└────────┬────────┘                   └────────────┬─────────────┘
         │                                         │
         └─────────────────┬───────────────────────┘
                           ▼
              read_local_skill_file → inspect/query/summarize
                           │
                           ▼
              excel_engine.py 统计计算
                           │
                           ▼
              excel_chart_generator → SSE artifact 事件
```



| 组件 | 路径 | 职责 |
|------|------|------|
| Agent 工具入口 | `application/tools/excel_data_analysis_tool.py` | `get_excel_analysis_tools()` 返回工具列表 |
| 计算引擎 | `application/tools/providers/excel_engine.py` | 解析 sheet、聚合、统计 |
| 图表生成 | `application/tools/providers/excel_chart_generator.py` | 输出 chart 规格/图片 |
| Skill | `skills/public/excel-experiment-analysis/SKILL.md` | 步骤、命名、图表类型规范 |
| 文件索引 | `services/chat_file_index.py` | 会话上传 Excel 的 BM25 索引 |
| 装配 | `tool_assembly._load_declared_skill_tool("excel_data_analysis")` | 注入全套工具 |

工具名包括：`list_experiment_datasets`、`inspect_excel_data`、`query_excel_data`、`summarize_excel_data`、`visualize_excel_data`、`excel_data_analysis`。

#### 项目实现

典型流程：

```
用户上传 experiment.xlsx → chat_file_index 建索引
用户选 excel-experiment-analysis Skill → full mode
Agent read_local_skill_file → 读分析规范
Agent inspect_excel_data / query_excel_data → excel_engine 计算
Agent visualize_excel_data → chart_generator → SSE artifact
```

`ToolAssembly._validate_skill_tool_declarations()` 会检查 Skill 声明的工具是否都已注入，缺失则 warning 日志。

#### 具体例子

用户问：「对比 A、B 两组实验的均值与标准差，画柱状图。」

```
read_local_skill_file("excel-experiment-analysis")
inspect_excel_data(sheet="Sheet1")
query_excel_data(...)           → engine 返回统计量
visualize_excel_data(chart_type="bar", ...)
→ 前端收到 chart artifact 事件
```

#### 常见误区

- ❌ 「Excel 工具读 KB 里的 xlsx」→ 主要面向**会话上传**；KB 内文档走 multi_kb_search 文本 chunk，非结构化表格分析。
- ❌ 「一个 excel_data_analysis 工具搞定」→ 实际是**多工具细粒度**组合，便于 Agent 分步推理。
- ❌ 「不需要 Skill 也能分析」→ 无 Skill 时工具可能未注入（取决于 selected_skills 声明）。

#### 延伸追问（L3）

- L3：Q12 manifest 化后 Excel 工具如何自动注册？

---

### Q10：HTML PPT 工具如何工作？

**一句话结论：** **Skill `html-ppt`** 定义 deck 结构、主题与 presenter 规范，**工具 `generate_html_ppt`** 根据 Agent 指令生成 HTML 文件，经 SSE **artifact** 事件推前端预览或下载。

#### 背景

用户要的是「能演示的幻灯片」，不是 Markdown 大纲。HTML deck 可嵌入主题 CSS、动画、presenter notes，且浏览器即可播放，比 PPTX 生成链更轻。

#### 详细解释

**HTML PPT 生成链路（ASCII 架构图）：**

```text
┌────────────────┐
│ html-ppt Skill │
└────────────────┘

  html-ppt Skill ──────────────► R1

  R1 ──────────────► R2

  R2 ──────────────► GEN

  GEN ──────────────► ART

  ART ──────────────► 前端 iframe 预览

```

```
用户请求「做 8 页核电子学分享 PPT」
        │
        ▼
┌─────────────────┐     读规范      ┌─────────────────────┐
│ html-ppt Skill  │ ──────────────► │ read_local_skill_file│
│ SKILL.md        │                 └──────────┬──────────┘
└─────────────────┘                            │
                                               ▼
                              read_skill_reference (themes/templates)
                                               │
                                               ▼
                              generate_html_ppt(title, slides, theme)
                                               │
                                               ▼
                         SSE {"type":"artifact","artifact_type":"html",...}
                                               │
                                               ▼
                                    前端幻灯片预览 / 下载
```

**Skill 侧（规范）：**
- `skills/public/html-ppt/SKILL.md` — 总 workflow
- `references/full-decks/` — tech-sharing、pitch-deck 等完整模板
- `references/themes/` — 视觉主题指南

**Tool 侧（执行）：**
- `application/tools/providers/html_ppt_tool.py` → `generate_html_ppt_tool`
- Agent 传入 title、slides 结构、主题等参数
- 输出 HTML 文件路径或内容

**前端侧（展示）：**
- SSE `artifact` 事件类型
- 前端 iframe 预览或下载链接

#### 项目实现

装配路径：

```python
# tool_assembly._load_declared_skill_tool
if tool_name == "generate_html_ppt":
    from backend.custom.tools.html_ppt_tool import generate_html_ppt_tool
    return generate_html_ppt_tool
```

Skill frontmatter 的 `tool_names` 含 `generate_html_ppt` 时自动注入。Agent 需先 `read_local_skill_file("html-ppt")` 了解输出 schema，再调用工具。

#### 具体例子

```
用户：「用 tech-sharing 主题做 8 页核电子学入门分享」
→ read_local_skill_file("html-ppt")
→ read_skill_reference("html-ppt", "references/themes/tech-sharing")
→ generate_html_ppt(title="核电子学入门", slides=[...], theme="tech-sharing")
→ SSE: {"type": "artifact", "artifact_type": "html", ...}
→ 前端幻灯片预览
```

#### 常见误区

- ❌ 「工具内置所有模板全文」→ 模板在 Skill reference，需渐进读取。
- ❌ 「输出是 .pptx」→ 是 **HTML** deck，不是 Office 二进制。
- ❌ 「lightweight 也能生成 PPT」→ 需选 Skill 走 full + 工具注入。

#### 延伸追问（L3）

- L3：Q11 Skill 若绑定专用 Workflow，PPT 是否可一键触发？

---

## L3 高级 🔴

### Q11：为什么 Skill 只影响 Prompt+工具，还不能驱动独立工作流？

**一句话结论：** 当前架构是**「Markdown 说明书 + 模型自律」**的快速落地路径，缺少 `SkillWorkflowExecutor` 把 Skill manifest 映射到预定义 LangGraph 子图，因此复杂 Skill 行为不稳定、难保 SLA。

#### 背景

html-ppt、Excel 分析等场景用「读文档 + 调工具」已能工作。但随着 Skill 增多，问题暴露：
- 模型可能跳过 Skill 步骤
- 同一 Skill 不同会话输出质量方差大
- 无法保证「必须先 OCR 再分析」等硬顺序

#### 详细解释：现状 vs 目标

**现状（`agent_architecture_redesign.md`）：**

```
selected_skills → Router  mainly: 有 Skill → full / 无 → lightweight
Skill 内容 → Prompt 片段 + 工具注入
执行 → 通用 LangGraph ReAct loop，模型自由决策
```

**缺失能力：**
- 无 `SkillWorkflowExecutor`
- 无 Skill YAML frontmatter `workflow: data-analysis` → 直达专用子图
- Router 不区分「html-ppt 专用路径」与「generic full」

**目标架构：**

```
Skill frontmatter:
  workflow: html-ppt
  tools: [generate_html_ppt]
  steps: [load_theme, generate_deck, validate]

Router → SkillWorkflowExecutor → 预编译 LangGraph subgraph
→ 步骤固定、可测试、可 SLA
```

#### 项目实现（当前代码证据）

- `WorkflowRegistry.resolve_name()`：解析 `deep_research` / `lightweight` / `full` + 更多工作流，**YAML registry.yaml 驱动**
- `tool_assembly.py`：按 skill 名 if-else 加载工具，非 manifest 驱动
- `skill_runtime_state.has_loaded_skill`：软约束，非状态机硬 gate

#### 具体例子

| 场景 | 现状行为 | Workflow 化后 |
|------|----------|---------------|
| html-ppt | 模型可能不调 read_skill 直接 generate | 子图节点 1 强制 read，节点 2 generate |
| excel 分析 | 可能跳过 visualize | 固定 pipeline：inspect → query → visualize |
| deep-research | 已有独立 Orchestrator（特例） | 可统一为 Skill workflow 一种 |

#### 常见误区

- ❌ 「Skill 已经是 Workflow」→ 只是文档，不是可执行状态机。
- ❌ 「full mode = 有 Workflow」→ full 只是工具全，执行仍 ReAct。
- ❌ 「短期不能落地 Workflow」→ deep-research 已证明 Orchestrator 模式可行，缺的是通用化。

#### 延伸追问

- 如何设计 Skill manifest schema？与 LangGraph subgraph  handoff 如何对齐？

---

### Q12：ToolAssembly 的 if-else 扩展性问题如何解决？

**一句话结论：** 现状 `_load_declared_skill_tool()` 硬编码 skill→tool 映射；改进方向是 **Skill manifest 声明 tools** + **Registry 装饰器注册** + **Plugin 动态 import**，实现「加 Skill 不改 Pipeline 源码」。

#### 背景

每新增一个带专用工具的 Skill，开发者可能要改：
1. `tool_assembly.py` 的 if-else
2. Skill parser 的 tool_names
3. 测试与文档

这是典型 **Open-Closed 原则** 违反，Interview 常作系统设计题。

#### 详细解释：三种改进方案

**方案 1：Skill manifest（推荐优先）**

```yaml
# SKILL.md frontmatter
name: html-ppt
tools:
  - module: backend.custom.tools.html_ppt_tool
    export: generate_html_ppt_tool
```

`ToolAssembly` 遍历 `skill.tool_names` → 动态 import，**已有部分实现**（`_inject_declared_skill_tools` + parser），但 `_load_declared_skill_tool` 仍 fallback 到 if-else。

**方案 2：Registry 模式**

```python
@register_tool("html-ppt", "generate_html_ppt")
def get_html_ppt_tool(): ...
```

启动时扫描 registry，Pipeline 只查表。

**方案 3：Plugin 入口点**

`pyproject.toml` entry_points 或 `skills/public/*/tools.py` 约定导出。

#### 项目实现（现状）

```python
# tool_assembly.py — 硬编码示例
if tool_name == "generate_html_ppt":
    from backend.custom.tools.html_ppt_tool import generate_html_ppt_tool
    return generate_html_ppt_tool
if tool_name == "academic_search":
    ...
if tool_name in ("list_experiment_datasets", ...):
    return get_excel_analysis_tools()
```

`_validate_skill_tool_declarations()` 已能在运行时 warning 缺失工具——这是迁移到 registry 的前置观测点。

#### 具体例子

**新增 `my-chart` Skill 现状步骤：**
1. 写 `skills/public/my-chart/SKILL.md` + frontmatter tool_names
2. **改** `tool_assembly._load_declared_skill_tool` 加分支
3. 前端注册 skill id

**Registry 化后：**
1. 写 Skill + `@register_tool` 工具
2. 前端注册 — **不改 Pipeline**

#### 常见误区

- ❌ 「parser 已有 tool_names 就完全 registry 化了」→ import 路径仍硬编码。
- ❌ 「动态 import 无安全风险」→ 需白名单 module 前缀，禁止任意路径。
- ❌ 「Registry 与 ToolGuardrail 冲突」→ 无关；Registry 管装配，Guardrail 管执行。

#### 延伸追问

- manifest 中 tools 版本与 Skill 版本如何绑定？热更新 Skill 是否要重启 API？

---

### Q13：多 KB 检索工具与 chat_file_search 如何共存？

**一句话结论：** **multi_kb_search** 查企业持久 KB（Milvus/Nebula），**chat_file_search** 查当前会话上传文件（内存 BM25）；二者可同时装配，靠 Prompt 与 Router 引导 Agent 选对工具。

#### 背景

用户常说「帮我看看这份文件」——可能是刚上传的 PDF，也可能是 KB 里已有文献。两套索引**完全不同**，误用会导致「检索为空」或「延迟加倍」。

#### 详细解释：对比

| 维度 | multi_kb_search | chat_file_search |
|------|-----------------|------------------|
| 数据源 | MySQL 绑定的 KB | 当前 thread 上传文件 |
| 索引 | Milvus + 图谱 + keyword | 内存 BM25+TF-IDF（`chat_file_index.py`） |
| 持久性 | 多用户共享、长期 | 会话级 LRU |
| embedding | 调用 | **不调** embedding API |
| 注入条件 | `kb_ids` 非空 | `thread_id` 有文件索引 |
| 延迟 | 较高（秒级） | 较低（毫秒~百毫秒） |

```text
┌──────────────────────────────────────┐
│ 用户问题                                 │
└──────────────────────────────────────┘
           │
           ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌───────────────────┐
│ Agent          │  │ CF             │  │ 内存 BM25 索引     │  │ MK             │  │ (Milvus + Nebula) │
└────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘  └───────────────────┘

```

#### 项目实现

```python
# tool_assembly.assemble()
if self._kb_ids:
    tools_by_name["multi_kb_search"] = self._create_multi_kb_search_tool()
else:
    tools_by_name.pop("multi_kb_search", None)

if self._thread_id and has_thread_file_index(self._thread_id):
    tools_by_name["chat_file_search"] = create_scoped_chat_file_search_tool(self._thread_id)
```

冲突场景：用户问题模糊 → Agent **双调** → 延迟 ×2、context ×2。

**优化方向（设计题）：**
- Router 层：有 upload 无 kb_ids → 默认只开 chat_file
- Prompt：显式规则「企业知识用 multi_kb，刚上传用 chat_file」
- 意图分类：轻量 classifier 前置

#### 具体例子

| 用户问题 | 应选工具 | 错误选择后果 |
|----------|----------|--------------|
| 「期刊库里关于 HPGe 的论文」 | multi_kb_search | chat_file 无结果 |
| 「我刚传的实验数据.xlsx 均值多少」 | chat_file_search | multi_kb 找不到新文件 |
| 「对比 KB 论文和我上传的笔记」 | **两者** | 合理双调，需 Prompt 说明来源 |

#### 常见误区

- ❌ 「chat_file 也存在 Milvus」→ **纯内存**索引，不走向量库。
- ❌ 「multi_kb 能搜 upload」→ 上传文件未 ingest 进 KB 则搜不到。
- ❌ 「两者互斥装配」→ 代码上**可同时存在**。

#### 延伸追问

- 能否统一为「虚拟 KB = session files」？embedding 成本 vs 召回 trade-off？

---

### Q14：沙箱（sandbox）与工具有什么关系？

**一句话结论：** `application/tools/` 是**声明式 LangChain 工具**（检索、生成文件）；沙箱运行时（bash、写文件、跑 Python）可选但**主路径默认不用**——RAG+搜索场景默认移除沙箱工具，二者层次不同。

#### 背景

项目曾经使用 DeerFlow sandbox，现已统一到 `application/tools/` 层次。主站以 **RAG + 搜索 + Skill** 为主，不需要每个用户都能 bash，故 ToolAssembly **主动剔除**沙箱类工具。

#### 详细解释

**声明式工具（本项目主路径）：**
- 输入输出 schema 固定
- 副作用可控（查库、写 artifact）
- 由 `@tool` + Allowlist 约束

**沙箱运行时：**
- 动态执行用户/模型生成的代码
- 需隔离：网络、文件系统、CPU、 syscall
- 审计日志、命令白名单

#### 项目实现

```python
# tool_assembly.assemble()
sandbox_tools = ["bash", "ls", "write_file", "str_replace", "present_files", "read_file"]
for tool_name in sandbox_tools:
    tools_by_name.pop(tool_name, None)
```

即使 `get_available_tools()` 返回了沙箱工具，Assembly 也会移除。若未来启用，需：
- 独立子 Agent + Sandbox 容器
- 网络 egress 白名单
- 生产默认关闭或仅 admin role

#### 具体例子

| 能力需求 | 应用方案 | 不用沙箱的原因 |
|----------|----------|----------------|
| Excel 统计 | excel_engine | 专用工具更安全 |
| HTML PPT | generate_html_ppt | 模板化生成 |
|  arbitrary Python | sandbox（若启用） | 风险高，需隔离 |

#### 常见误区

- ❌ 「框架有 sandbox 所以生产默认开启」→ 本项目 Assembly **显式移除**。
- ❌ 「sandbox 等于 Docker」→ 取决于框架实现，可能是进程级隔离。
- ❌ 「read_file 工具读任意路径」→ 沙箱 read_file 与 Skill read 不同；主路径已 pop。

#### 延伸追问

- 若启用 sandbox，ToolGuardrail 如何按 role 开放 bash？与 MCP 工具暴露面如何统一？

---

### Q15：如何新增一个 Skill 并让 Agent 可用？

**一句话结论：** 在 `skills/public/<skill-id>/` 写 `SKILL.md`（+ 可选 `references/`），声明 `tool_names` 并实现对应 `application/tools/providers/` 工具，确保 ToolAssembly 能加载，前端注册 skill id，测通 `read_local_skill_file` 与 WorkflowRegistry full 路径。

#### 背景

Skill 是产品扩展能力的**低代码入口**：多数场景无需改 LangGraph 图，只需 Markdown + 工具。但步骤跨 **skills 目录、tools、assembly、前端** 四处，漏一步 Agent 就「看不到」或「调不到」。

#### 详细解释：完整 Checklist

**Step 1 — 创建 Skill 目录**

```
backend/skills/public/my-skill/
  SKILL.md          # 必需，含 frontmatter: name, description, tool_names
  references/       # 可选
```

**Step 2 — 实现专用工具（若需要）**

`backend/application/tools/providers/my_skill_tool.py` + `@tool` 装饰器

**Step 3 — 注册到 ToolAssembly**

- 理想：frontmatter `tool_names` + manifest import（未来）
- 现状：在 `_load_declared_skill_tool()` 增加分支，或确保 parser 解析的 name 已在 if-else 中

**Step 4 — 前端 Skill 选择器**

skill id = 目录名 = `read_local_skill_file` 参数

**Step 5 — 测试**

```bash
# 选 Skill 后发问答，日志应见：
[ToolAssembly] 已注入原生 Skill 工具集=['read_local_skill_file', ...]
[NativeSkillTools] read_local_skill_file called: skill=my-skill
[ToolAssembly] 组装完成: [..., 'my_tool']
```

**Step 6 — 大输出 Skill**

确认 `ContextPruningMiddleware` 拦截该工具名，避免撑爆 context。

**Step 7 — config**

`config.yaml` → `skills.path: backend/skills`（一般无需改）

#### 项目实现

`core/skills/loader.py` 启动时扫描；`ScopedSkillsProvider` 缓存 `_skills_cache`。无需重启若支持热加载目录（当前为进程内缓存，**新增 Skill 通常需重启 API** 或清缓存）。

Router：有 selected_skills → **full mode**（非 deep_research 时）。

#### 具体例子

新增 `lab-report` Skill：

1. `SKILL.md` 声明 `tool_names: [format_lab_report]`
2. 实现 `format_lab_report` 工具
3. `_load_declared_skill_tool` 注册
4. 前端勾选 lab-report
5. 验证：Agent 先 read skill → 调 format_lab_report → SSE artifact

#### 常见误区

- ❌ 「只写 SKILL.md 就够」→ 无 tool_names 且无通用工具覆盖时，Agent 只有文档无执行力。
- ❌ 「skill 名大小写随意」→ 与目录名、API selected_skills 必须一致。
- ❌ 「必须改 LangGraph」→ 除非要专用 Workflow（见 Q11），否则不需要。

#### 延伸追问

- Skill 版本升级如何兼容旧会话？custom category 多租户如何测 ScopedSkillsProvider？
