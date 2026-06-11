# `backend/agents/*` 旧链路清理方案

## 1. 结论

`backend/agents/*` 这套老链路目前**没有被现有前端主流程直接使用**，它实际只被一个遗留的 v1 SSE 接口拖住：

- `backend/api/routers/rag.py:13-14` 仍直接导入 `get_agent_factory` 和 `NuclearElectronicsAgent`
- `backend/api/routers/rag.py:231-242` 通过 `_get_nuclear_electronics_agent()` 创建/复用旧 agent
- `backend/api/routers/rag.py:1357-1435` 暴露了遗留接口 `POST /api/v1/rag/agents/nuclear_electronics/stream`

相对地，当前主链路已经是：

- 新后端入口：`backend/custom/api/v2/agent.py:415-470`，核心实现为 `NEFullFeatureAgent`
- 现有前端调用：`frontend/src/views/ne-full/composables/useAgentChat.ts:964`，实际请求 `/api/v2/agent/query` 或 `/api/v2/guest/query`

因此，**推荐策略不是先删整个 `rag.py`，而是先把“旧接口对旧类的依赖”切断，再删除 `backend/agents/*`**。

---

## 2. 当前使用链路

### 2.1 旧链路入口

遗留调用链如下：

```text
POST /api/v1/rag/agents/nuclear_electronics/stream
  -> backend/api/routers/rag.py::_generate_agent_stream()
  -> _get_nuclear_electronics_agent()
  -> backend/agents/base/agent_factory.py
  -> backend/agents/nuclear_electronics_agent.py::NuclearElectronicsAgent
  -> NuclearElectronicsAgent.aquery_stream()
  -> backend/api/routers/rag.py::retrieve_context()
  -> LLM 流式生成
```

关键证据：

- `backend/api/routers/rag.py:1368-1390`：旧 SSE 生成器直接调用 `agent.aquery_stream()`
- `backend/agents/nuclear_electronics_agent.py:129-220`：旧 agent 本体先调 `retrieve_context()`，再自行组 prompt 调 LLM
- `backend/agents/nuclear_electronics_agent.py:12-13`：旧 agent 依赖 `BaseAgent` 和旧工厂

### 2.2 新链路入口

当前新主链路已经不经过 `backend/agents/*`：

- `backend/custom/api/v2/agent.py:419-425`：实例化 `NEFullFeatureAgent`
- `backend/custom/api/v2/agent.py:458-470`：`POST /api/v2/agent/query`
- `frontend/src/views/ne-full/composables/useAgentChat.ts:964`：前端实际使用 v2 接口

这说明：

- `backend/agents/base/base_agent.py`
- `backend/agents/base/agent_factory.py`
- `backend/agents/nuclear_electronics_agent.py`

都不是现有主产品链路的基础设施，而是**被遗留兼容接口独占的历史实现**。

---

## 3. 不能一起删的部分

虽然旧 agent 可以清掉，但 **`backend/api/routers/rag.py` 本身不能随 `backend/agents/*` 一起删除**，因为它已经演变成统一检索入口。

当前仍有新代码在复用 `retrieve_context()`：

- `backend/custom/tools/multi_kb_search_tool.py:432-446`
- `backend/custom/services/explore_learning.py:215-224`
- 文档也已将其标注为统一入口：`docs/知识库算法和架构.md:107-113`

所以本次清理的正确边界应是：

### 可以清理

- `backend/agents/nuclear_electronics_agent.py`
- `backend/agents/base/__init__.py`
- `backend/agents/base/base_agent.py`
- `backend/agents/base/agent_factory.py`
- `backend/api/routers/rag.py` 中与旧 agent 绑定的那一小段代码

### 需要保留

- `backend/api/routers/rag.py` 中的 `retrieve_context()`、`_extract_keywords()`、`_get_default_kb_id()`、`/retrieve`、`/retrieve_internal`、`/query_stream` 等通用能力

---

## 4. 风险判断

### 4.1 内部风险较低

仓库内检索没有发现前端或测试在调用：

- `/api/v1/rag/agents/nuclear_electronics/stream`
- `NuclearElectronicsAgent`
- `backend.agents.base.*`

也就是说，**仓库内主流程迁移基本已经完成**。

### 4.2 外部调用风险仍需确认

仍然不能只靠代码检索就断言“线上 0 流量”，因为旧接口是 HTTP 暴露面，可能还有：

- 第三方脚本
- Postman/运营后台收藏接口
- 早期集成方
- 文档外部抄用

尤其这个接口当前还是：

- 无认证：`backend/api/routers/rag.py:1403-1408`
- 路径稳定：`/api/v1/rag/agents/nuclear_electronics/stream`

因此建议在真正删除前，先看最近 14 到 30 天访问日志。

---

## 5. 推荐清理路径

推荐采用 **“先解耦、后删除、最后下线路径”** 的三阶段方案。

### 阶段 A：先把旧接口和旧类解耦

目标：即使还要暂时保留旧 URL，也不再依赖 `backend/agents/*`。

建议做法：

1. 删除 `backend/api/routers/rag.py` 顶部这两个导入
   - `from backend.agents.base.agent_factory import get_agent_factory`
   - `from backend.agents.nuclear_electronics_agent import NuclearElectronicsAgent`
2. 删除 `_get_nuclear_electronics_agent()`：`backend/api/routers/rag.py:231-242`
3. 改写 `_generate_agent_stream()`：不要再调用 `agent.aquery_stream()`，直接复用现有 `retrieve_context()` + 现有 LLM 流式逻辑
4. 如果要尽量复用现有代码，优先把旧接口转成对 `_rag_stream_generator()` 的适配层，而不是继续保留旧 Agent 类

这样做的效果：

- 旧 URL 还能临时活着
- `backend/agents/*` 已不再是运行时依赖
- 后续删除老目录时风险最小

### 阶段 B：删除 `backend/agents/*`

当阶段 A 完成后，可以直接删：

- `backend/agents/nuclear_electronics_agent.py`
- `backend/agents/base/__init__.py`
- `backend/agents/base/base_agent.py`
- `backend/agents/base/agent_factory.py`

删除后再做一次全仓检索，目标是 `rg "backend\\.agents|NuclearElectronicsAgent|BaseAgent"` 只剩历史文档命中，不再有运行时代码命中。

### 阶段 C：下线遗留 URL

如果日志确认旧接口无流量，直接删除：

- `AgentStreamRequest`
- `_generate_agent_stream()`
- `@router.post("/agents/nuclear_electronics/stream")`

如果日志显示还有调用，则建议：

1. 先保留路径一个过渡周期
2. 响应头或日志中加 deprecated 标记
3. 通知调用方切换到：
   - 登录态场景：`/api/v2/agent/query`
   - 访客场景：`/api/v2/guest/query`
4. 观察一轮后再彻底移除路径

---

## 6. 为什么推荐“先保留路径、先删旧类”

因为当前老接口和新接口并不是完全等价：

- 旧接口请求体：`question/mode/model_id/top_k/chunk_top_k/...`
- 新接口主入口：`/api/v2/agent/query`
- 新接口还带：
  - 用户身份
  - thread_id
  - selected_skills
  - deep research / artifact / follow-up 等更完整的会话语义

这意味着：

- **删旧类** 与 **删旧 URL** 不是必须同时发生
- 完全可以先把旧 URL 变成一个轻量兼容层
- 这样可以先完成代码债清理，再单独管理 API 下线节奏

这是这次清理里最稳的切法。

---

## 7. 具体改动清单

### 7.1 第一批应修改的文件

- `backend/api/routers/rag.py`
  - 去掉旧 agent import
  - 去掉 `_get_nuclear_electronics_agent()`
  - 改写旧 stream 接口实现

### 7.2 第一批应删除的文件

- `backend/agents/nuclear_electronics_agent.py`
- `backend/agents/base/__init__.py`
- `backend/agents/base/base_agent.py`
- `backend/agents/base/agent_factory.py`

### 7.3 第二批应更新的文档

至少更新这些“当前架构说明”文件，避免继续把旧类当活跃链路：

- `docs/知识库算法和架构.md`
- `plans/qa/01_overview_architecture.md`
- `plans/qa/04_api_streaming.md`

历史方案类文档（如早期重构设计稿）可以不强求逐篇清洗，只要明确其为历史资料即可。

---

## 8. 建议验证步骤

### 代码级验证

1. 全仓确认无运行时代码再引用旧类
2. 启动 HTTP 服务
3. 验证以下接口仍正常：
   - `/api/v1/rag/retrieve`
   - `/api/v1/rag/query_stream`
   - `/api/v2/agent/query`
   - `/api/v2/guest/query`

### 行为级验证

至少覆盖这几类场景：

- 单知识库问答
- 多知识库检索
- 访客问答
- 登录态问答
- 探索式学习引用 `retrieve_context()` 的路径

### 运营级验证

- 核查网关/应用日志中 `/api/v1/rag/agents/nuclear_electronics/stream` 的近 14 至 30 天访问量
- 若访问量非 0，给出下线公告窗口

---

## 9. 推荐执行顺序

1. 先查旧 URL 的线上访问量
2. 无论访问量是否为 0，都先把旧 URL 和 `backend/agents/*` 解耦
3. 删除 `backend/agents/*`
4. 回归验证 `retrieve_context()` 相关新链路
5. 最后决定是否直接删除 `/api/v1/rag/agents/nuclear_electronics/stream`

---

## 10. 最终判断

这条旧线路已经满足“**代码上可拆**”的条件，但是否“**本周可直接删 URL**”，关键取决于外部调用日志。

如果只问“如何移除 `backend/agents/*` 这条旧实现”，结论是：

- **可以移除**
- **不应连带删除整个 `rag.py`**
- **最佳做法是先把旧 URL 从旧类实现切到统一检索/流式逻辑，再删目录**

这会比“一刀切删除 `backend/agents/*` + 旧路由”更稳，也更符合当前项目已经完成的新旧架构过渡状态。
