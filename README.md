# RADAI — 领域智能体平台

**RADAI** 是面向高校课程与科研场景的智能体平台:把大模型变成可部署的
"课程助教 / 科研分析 / 题库练习"系统。它承载全部对话与业务前端,知识检索能力
由独立的知识库服务 [omnigraph](https://github.com/simpleqt/omnigraph) 提供
(经 HTTP 访问,本仓库不含任何 RAG/知识库实现)。

## 核心能力

### 1. 智能体运行时
- **LangGraph 智能体引擎**:流式输出、工具调用、运行预算控制、上下文账本
  (证据溯源)、断线重连的后台任务、SQLite/Redis 会话检查点
- **智能体配置化**:YAML 定义智能体(提示词/工具/知识库绑定/工作流),
  支持运行时注册与热加载;按 agent_code 多租户隔离
- **技能系统**:技能包(含 html-ppt、学术检索、深度研究等内置技能)、
  动态加载、技能-智能体绑定与 ACL
- **MCP 服务**:FastMCP(SSE)对外暴露检索/工具能力,API Key 作用域控制

### 2. 知识检索接入(经 omnigraph)
- `multi_kb_search` 多库检索工具(remote-only,经 `KNOWLEDGE_SERVICE_BASE_URL`)
- MCP 文本证据检索、知识库图片检索工具(图片白名单机制)
- 探索式学习检索、会话删除联动暂存清理
- 知识库管理页面(上传/文档/目录/关键词)经反向代理透传至 omnigraph

### 3. 课程业务
- **题库与练习**:批量题库导入、AI 批改(含图片题视觉评分)、错题分析
- **学情分析**:课堂提问记录采集与分析报告
- **课程评价**:试卷结构化、成绩达成度分析(`ce_*` 全套表与流程)
- **自定义学习**:学习计划生成/节点练习/调整闭环
- **探索式学习**:知识点探索、测验生成(检索增强)
- **实验数据分析**:Excel/CSV 数据集管理 + DuckDB 代码解释器智能分析

### 4. 多站点前端
- `frontend`(主站)、`frontend_csh_ai`、`frontend_fmca`、`frontend_landslide`、
  `frontend_audit`(院系定制)、`guest`(访客匿名问答)——同一后端,不同品牌配置

### 5. 平台与运维
- 会话管理(共享/置顶/回滚/工件)、消息反馈、访客短信认证、CAS 对接
- 管理后台:用户/API Key/审计/系统健康;上下文维护 GC;tracing
- docker-test 编排(灰度 compose)、发布与回滚脚本

## 架构一览

```
 浏览器 ──▶ 前端站点(主站/院系定制/访客)
              │ /api
              ▼
        radai 智能体平台 (8082 主 API / 8083 访客 API)
        ├─ 智能体运行时(LangGraph + 检查点 + 工具/技能)
        ├─ 课程业务(题库/学情/课评/自定义学习/数据集)
        ├─ MCP 服务(FastMCP SSE)
        └─ 知识库请求 ──HTTP──▶ omnigraph 知识服务 (8086)
             管理面: 反向代理透传        ├─ 自研入库(MinerU→Milvus/Nebula/BM25)
             检索面: X-Internal-Secret   └─ 自研检索(四路召回+Profile 过滤)
        共享设施: MySQL / Redis / MinIO
```

## 部署前提

1. 先部署 [omnigraph](https://github.com/simpleqt/omnigraph) 知识服务
2. 两服务配置**相同的非空** `INTERNAL_API_SECRET`
3. 本服务 `.env` 设置 `KNOWLEDGE_SERVICE_BASE_URL=http://127.0.0.1:8086`
4. 共享同一 MySQL/Redis/MinIO 实例

## 快速开始

```bash
cp .env.example .env    # 填写数据库/Redis/MinIO/LLM 与知识服务接入
make install
make init-db
make run-api            # 主 API(8082)
make run-guest-api      # 访客 API(8083)
```

前端构建见各 `frontend*/` 目录(`npm install && npm run build`,
产物由 API 静态挂载)。两服务拆分部署细节见
[docs/split_agent_rag_services.md](docs/split_agent_rag_services.md)。

## 目录结构

```
backend/
  api/            HTTP 入口(主/访客)、路由、knowledge_proxy(知识库代理)
  application/    智能体用例(执行器/工具/技能/题库/学情/课评/探索学习)
  core/           智能体引擎(agents 运行时/上下文账本/中间件)、模型工厂
  db/             会话/题库/课评/技能等平台侧模型
  services/       平台服务(会话文件/图表存储/URL 签名等)
frontend*/        主站与院系定制前端
guest/            访客前端
deploy/           docker-test 编排、发布/回滚脚本
docs/             设计文档(split_agent_rag_services.md 为拆分部署说明)
tools/            check_imports 拆分完整性门禁
```

## 开发

```bash
make check-imports   # 校验不引用已移出的知识库模块(拆分完整性门禁)
make lint
make format
```

## 相关仓库

- [omnigraph](https://github.com/simpleqt/omnigraph) — 知识库服务
  (自研入库 + 自研检索,含消融与 LightRAG 对比评测报告)
