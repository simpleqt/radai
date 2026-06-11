# 知识库服务移除影响检查

## 1. 结论

**会影响，而且影响面不小。**

如果这里说的“知识库服务内容”包含以下任一层：

- `backend/rag/service.py`
- `backend/rag/knowledge_base/manager.py`
- `backend/api/routers/rag.py` 中的统一检索函数 `retrieve_context()`
- 关键词索引 / 内容过滤 / DirectRetriever 这一整套检索栈

那么不仅会影响传统 `/api/v1/rag/*` 接口，**还会直接影响当前较新的服务链路**，包括：

- `/api/v2/agent/query`
- `/api/v2/guest/query`
- `/api/v2/explore/detail`
- `NEFullFeatureAgent / CSHFullFeatureAgent / LightweightQAAgent`
- `multi_kb_search` 工具链
- 知识库管理、目录、文档删除、关键词初始化等后台管理能力
- 应用启动阶段的 RAG / 关键词索引预热

换句话说，**知识库服务已经不是“老接口专属模块”，而是当前新服务的底座之一**。

---

## 2. 影响判断总表

| 移除层级 | 是否影响新服务 | 影响说明 |
|---|---|---|
| 只移除旧的 `backend/agents/*` | 否，基本不影响 | 主要影响遗留 `nuclear_electronics_agent` 链路 |
| 只移除 `/api/v1/rag/agents/nuclear_electronics/stream` | 对新服务影响很小 | 这是遗留 URL，不是现主链路 |
| 只移除 `/api/v1/rag/query_stream` 这类 v1 HTTP 路由 | 可能不影响 v2 主链路 | 但前提是保留 `retrieve_context()` 和底层 RAG 能力 |
| 移除 `retrieve_context()` / `_extract_keywords()` / DirectRetriever / content_filter | **会影响** | 新 agent、explore、multi_kb_search 都直接或间接依赖 |
| 移除 `backend/rag/service.py` | **会影响** | 新服务启动预热、文档删除、embedding 读取、多模态入库都会受影响 |
| 移除 `backend/rag/knowledge_base/*` | **会影响** | 新旧管理端、关键词能力、默认知识库解析、RAG 实例获取都会受影响 |

---

## 3. 直接依赖知识库服务的新链路

### 3.1 新版 Agent 问答链路依赖知识库检索

当前主智能体不是绕开知识库服务运行的，而是通过 `multi_kb_search` 工具接入统一检索栈。

关键证据：

- `backend/custom/agents/ne_full_feature_agent.py:320-332`
  - `NEFullFeatureAgent` 通过 `ToolAssemblyPipeline` 装配工具
- `backend/custom/agents/support/tool_assembly.py:77-126`
  - 注入 `multi_kb_search`
- `backend/custom/agents/support/tool_assembly.py:143-149`
  - 只要有 `kb_ids`，就启用 `multi_kb_search`
- `backend/custom/tools/multi_kb_search_tool.py:432-446`
  - `multi_kb_search` 直接调用 `backend/api/routers/rag.py::retrieve_context()`
- `backend/custom/tools/multi_kb_search_tool.py:483-499`
  - 还会通过 `get_rag_service()` 取 embedding 能力

因此，移除知识库检索底座后：

- `/api/v2/agent/query` 的知识库问答会退化或直接报错
- 深度研究中需要 `multi_kb_search` 的流程也会受影响
- 不是只有旧 `/api/v1/rag/*` 会坏

### 3.2 轻量问答和访客问答也依赖同一套知识库栈

关键证据：

- `backend/custom/api/v2/guest.py:109-149`
  - 访客问答直接实例化 `NEFullFeatureAgent(lightweight_mode=True, kb_ids=...)`
- `backend/custom/agents/csh_full_feature_agent.py:572-585`
  - 轻量模式下转入 `LightweightQAAgent`
- `backend/custom/agents/lightweight_qa_agent.py:1515-1527`
  - `LightweightQAAgent` 直接调用 `execute_multi_kb_search()`

结论：

- `/api/v2/guest/query` 不是一个“独立于知识库服务的新系统”
- 它只是轻量包装后的同一知识库问答底座

### 3.3 Explore 新服务也依赖知识库服务

关键证据：

- `backend/custom/api/v2/explore.py:210-245`
  - `/api/v2/explore/detail` 直接复用轻量问答智能体
- `backend/custom/services/explore_learning.py:209-228`
  - explore 学习链路自己也直接调用 `retrieve_context()`

这意味着：

- `explore/detail` 会受影响
- `explore` 中资源、知识点展开、基于 KB 的详情回答也会退化

---

## 4. 管理端与后台能力的影响

### 4.1 知识库管理接口会直接失效

关键证据：

- `backend/api/routers/knowledge_base.py:12`
  - 管理路由直接依赖 `get_kb_manager`
- `backend/api/routers/knowledge_base.py:127-145`
  - 创建知识库走 `manager.create_knowledge_base()`
- `backend/api/routers/knowledge_base.py:152-181`
  - 列表查询走 `manager.list_knowledge_bases()`
- `backend/api/routers/knowledge_base.py:184-208`
  - 默认库 / 详情查询都走 `get_kb_manager()`

如果移除 `backend/rag/knowledge_base/*`：

- 后台知识库列表页
- 新建知识库
- 默认知识库查询
- 知识库详情与更新

都会直接失效。

### 4.2 文档管理、目录管理会受影响

关键证据：

- `backend/api/routers/knowledge_base_documents.py:246-267`
  - 上传文档前先校验知识库存在与状态
- `backend/api/routers/knowledge_base_documents.py:596-618`
  - 批量删文档既调用 `get_kb_manager()`，也调用 `get_rag_service().delete_doc_by_id()`
- `backend/api/routers/knowledge_base_documents.py:704-719`
  - 单文档删除同样依赖 KB manager + rag service
- `backend/api/routers/directories.py:107-154`
  - 目录树接口先校验知识库，再查询目录结构

因此移除后会影响：

- 后台文档上传/删除
- 目录树查看
- 批量移动、目录重命名等管理动作

### 4.3 关键词初始化与索引构建会失效

关键证据：

- `backend/api/routers/keywords.py:95-167`
  - 关键词初始化任务走 `kb_manager.initialize_keyword_components()`
- `backend/api/routers/keywords.py:183-207`
  - 构建关键词索引走 `kb_manager.build_keyword_index()`
- `backend/api/routers/keywords.py:216-245`
  - 提取关键词走 `kb_manager.get_keyword_extractor()`

所以如果动到：

- `backend/rag/knowledge_base/manager.py`
- `backend/rag/keywords/*`

那么后台关键词能力也会一起坏掉。

---

## 5. 启动与运行时影响

### 5.1 主服务启动会受影响

关键证据：

- `backend/api/http_server.py:222-257`
  - 启动时会初始化 `get_rag_service()` 并预热所有活跃知识库的 RAG 实例
- `backend/api/http_server.py:261-320`
  - 还会预热关键词索引和关键词图缓存

结论：

- 如果底层 KB/RAG 服务被移除，主服务启动阶段就可能报错或退化
- 即便接口未被调用，应用启动时也已经依赖这套能力

### 5.2 Guest 服务启动同样依赖

关键证据：

- `backend/api/guest_http_server.py:80-111`
  - 启动时初始化 `get_rag_service()` 并预热 guest 使用的知识库
- `backend/api/guest_http_server.py:113-135`
  - 预热 retrieval stack：关键词索引 + DirectRetriever

结论：

- 访客服务不是“前台 UI 独立跑”
- 它启动时就依赖知识库底座

---

## 6. 统一检索入口是新服务共享点，不能轻易删

当前很多新代码并不直接调用 `RagService.aquery()`，但它们会复用 `retrieve_context()` 这条统一检索入口。

关键证据：

- `backend/api/routers/rag.py:463-620`
  - `retrieve_context()` 聚合了：
    - 默认知识库解析
    - 关键词提取
    - Retrieval Profile
    - DirectRetriever / LightRAG 回退
    - 内容过滤
- `backend/custom/tools/multi_kb_search_tool.py:432-446`
  - 直接复用 `retrieve_context()`
- `backend/custom/services/explore_learning.py:215-224`
  - 直接复用 `retrieve_context()`

所以：

- `backend/api/routers/rag.py` 不是只有 HTTP 路由作用
- 其中的检索函数已经成为新服务共享基础能力

---

## 7. 哪些新服务影响最大

### 高影响

- `/api/v2/agent/query`
- `/api/v2/guest/query`
- `/api/v2/explore/detail`
- `NEFullFeatureAgent`
- `CSHFullFeatureAgent`
- `LightweightQAAgent`
- `multi_kb_search`

这些都与“知识库问答”直接绑定。

### 中高影响

- `/knowledge_bases/*`
- `/knowledge_bases/{kb_id}/documents/*`
- `/knowledge_bases/{kb_id}/directories/*`
- `/keywords/*`
- `/api/v1/rag/retrieve`
- `/api/v1/rag/query_stream`

这些是管理端、检索端和辅助检索能力。

### 低影响或基本不受影响

从当前代码关系看，下列能力不以知识库服务为主依赖：

- 认证
- 会话元数据管理本身
- 文件中心的纯文件元数据操作
- 不依赖 KB 的联网搜索
- 不依赖 KB 的工件展示

但要注意：**一旦这些能力是被 Agent 主流程串起来使用，它们所在页面仍可能因为 KB 问答失效而“功能不完整”**。

---

## 8. 如果只是想清理“旧知识库接口”，边界应该怎么切

如果目标不是“去掉知识库能力”，而只是“清理旧接口/旧实现”，那可以分层看：

### 可以优先清理，风险相对小

- 遗留的旧 agent 链路
- 遗留 URL：`/api/v1/rag/agents/nuclear_electronics/stream`
- 部分仅作兼容保留的 v1 包装层

### 不能直接删

- `backend/rag/service.py`
- `backend/rag/knowledge_base/*`
- `backend/api/routers/rag.py::retrieve_context()`
- `backend/rag/keywords/*`
- `backend/rag/content_filter/*`
- `backend/rag/retriever/*`

因为这些已经被新服务复用。

---

## 9. 最终判断

如果“知识库服务内容移除”指的是**底层 KB / RAG / 检索栈本身**，答案是：

**会影响其他新的服务，而且会影响当前主链路。**

如果“移除”指的是**只下线旧的 v1 HTTP 包装接口或旧 agent 实现**，则：

- 可以做
- 但要保留底层知识库服务与统一检索能力
- 否则新的 `v2 agent / guest / explore / 多知识库检索` 也会一起受影响

## 10. 建议

建议先明确你打算移除的是哪一层：

1. 旧 URL / 旧 Agent 包装层
2. RAG HTTP 路由层
3. 统一检索函数层
4. 底层知识库与 RAG 服务层

这四层的风险完全不同。

如果你愿意，我下一步可以继续把它细化成一份“**哪些文件可以直接删、哪些必须保留、怎样拆才不影响新服务**”的清理清单。  
