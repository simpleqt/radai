# 13 · 模拟面试卷

> 按岗位场景编排，含**时间分配**、**追问链**、**评分要点**。面试官可直接按脚本提问。

---

## 卷 A：AI 后端通用岗（60 分钟）

### 第一节：架构鸟瞰（10 min）

| 顺序 | 题目 | 层次 | 参考模块 |
|------|------|------|----------|
| 1 | 描述从用户点击发送到看到 SSE 第一个字的完整链路 | L1 | 01-Q1, 02-Q1, 04-Q2 |
| 2 | 为什么有 8082 和 8083 两个服务？ | L2 | 01-Q2, 01-L3-Q9 |
| 3 | **追问** | 若合并单服务，你会怎么设计 checkpoint？ | L3 |

**评分：** 能画出 API→Router→Agent→RAG→SSE 四层得基础分；提到双 checkpoint 隔离得加分。

---

### 第二节：Agent 与上下文（15 min）

| 顺序 | 题目 | 层次 | 参考模块 |
|------|------|------|----------|
| 4 | CSH Router 三条路径的判定条件 | L2 | 02-Q2 |
| 5 | ContextPruningMiddleware 原理，不用它会怎样？ | L2 | 02-Q5 |
| 6 | **追问** | JIT 裁剪 8000 字符是否影响多跳推理？如何缓解？ | L3 | 02-L3-Q11 |
| 7 | 子 Agent 为什么导致 async MySQL 用 NullPool？ | L3 | 05-Q5, 02-Q8 |

**评分：** 说清 State Elevation + JIT 得满分；只答「裁剪消息」扣半。

---

### 第三节：RAG 深度（15 min）

| 顺序 | 题目 | 层次 | 参考模块 |
|------|------|------|----------|
| 8 | 画出 Stage-1 四 Path + Stage-2 过滤 | L2 | 03-Q2, 03-Q5, 03-Q6 |
| 9 | domain vs general_qa Profile 差异及原因 | L3 | 03-Q3, 03-L3-Q11 |
| 10 | 用户反馈双库检索慢，你的排查清单 | L3 | 08-场景A, 03-Q7 |

**评分：** 必须提到 `MULTI_KB_PATH_A_QUERY_ONLY` 或关键词复用之一。

---

### 第四节：工程与安全（10 min）

| 顺序 | 题目 | 层次 | 参考模块 |
|------|------|------|----------|
| 11 | ToolGuardrail Allowlist 与 Prompt 约束的区别 | L3 | 11-L3-Q11 |
| 12 | RAG 间接 Prompt Injection 是什么？项目如何缓解？ | L3 | 11-L3-Q13 |
| 13 | 已知技术债你最想先修哪一项？为什么？ | L3 | 01-L3-Q10, 08-Q11 |

---

### 第五节：编码/设计题（10 min，二选一）

**题 1：** 设计 Skill manifest，使 `html-ppt` Skill 自动注入 `html_ppt_tool`，无需改 `ToolAssemblyPipeline` if-else。

**题 2：** 设计 PostgreSQL checkpointer 迁移方案，解决 SQLite 多 worker 写锁。

**评分维度：** 接口设计、迁移步骤、回滚、对线上影响。

---

## 卷 B：RAG 专项（45 分钟）

| 时间 | 题目编号 | 主题 |
|------|----------|------|
| 5 min | 03-L1-Q1~Q3 | LightRAG 栈与 Profile |
| 10 min | 03-L2-Q5~Q8 | 四 Path、过滤、关键词 hybrid |
| 10 min | 03-L2-Q7 + 10-Q6 | multi_kb 优化 + KB 生命周期 |
| 10 min | 03-L3-Q11~Q12 | Profile 消融、RRF 跨库 |
| 10 min | 现场白板 | 画一次 `retrieve_context` 完整数据流 |

**压轴追问：** Nebula 挂掉时 Path-B/D 行为？有无自动降级？（03-L3-Q15）

---

## 卷 C：Agent 框架专项（45 分钟）

| 时间 | 题目编号 | 主题 |
|------|----------|------|
| 5 min | 02-L1-Q1~Q4 | Router、Checkpointer |
| 15 min | 02-L2-Q5~Q9 | Middleware、深研、ToolAssembly |
| 10 min | 02-L3-Q10~Q12 | Router 设计权衡、Skill 扩展 |
| 15 min | 02-L3-Q13 + 08-L3-Q14 | 新域接入 + 子 Agent 注册化 |

**编码题：** 伪代码实现 `GuardrailProvider`：guest 用户 deny `async_web_search`。

---

## 卷 D：Junior 新人（30 分钟）

仅 L1，每题 3–4 分钟：

1. 01-Q1 技术栈
2. 01-Q3 CLI 命令
3. 02-Q2 Router 三模式
4. 03-Q1 RAG 存储
5. 05-Q1 数据库分工
6. 05-Q2 认证方式
7. 06-Q1 典型工具
8. 06-Q2 Skill vs Tool
9. 04-Q2 SSE 是什么
10. 07-Q1 有哪些 Worker

**通过线：** 10 题对 7 题；允许看笔记但不能混淆 Skill 与 Tool。

---

## 卷 E：架构师 / Tech Lead（90 分钟）

| 阶段 | 内容 |
|------|------|
| 30 min | 卷 A 第二~四节全部 L3 |
| 20 min | 08-L3-Q11~Q15 + 01-L3-Q10 技术债路线图 |
| 20 min | 白板：目标架构 SkillWorkflowExecutor vs 现状对比（参照 agent_architecture_redesign.md） |
| 20 min | 生产部署：多 worker、Kafka、Milvus、checkpoint 完整 checklist（08-Q12） |

**决策题：** 1000 QPS 访客问答，你会如何扩容？哪些组件先水平扩展，哪些必须改架构？

**参考思路：**
- 8083 无状态 API 水平扩展
- Redis/Milvus/MySQL 连接池与分区
- SQLite checkpoint → PostgreSQL 或 sticky session
- RAG 只读副本 / 检索服务独立
- LLM 网关队列与限流

---

## 追问话术模板（面试官用）

| 候选人回答后 | 可追问 |
|--------------|--------|
| 提到了 Router | 「没选 Skill 但需要联网怎么办？」 |
| 提到了 RRF | 「两库评分尺度不同，RRF 有何局限？」 |
| 提到了 NullPool | 「那连接数爆炸怎么办？」 |
| 提到了 SSE | 「客户端断连后深度研究任务怎么停？」 |
| 提到了 LightRAG patch | 「升级上游 LightRAG 版本的风险？」 |

---

## 红旗信号（扣分项）

- 把 Skill 说成可执行 Tool
- 认为 checkpoint 存在 MySQL conversations 表
- 不知道 lightweight 与 full 的区别
- 说 RAG 只有向量检索、没有图谱
- 认为 config.yaml 的 key 可以提交 Git
- 把 Explore API 与主 Agent 混为同一条 LangGraph 路径

---

## 绿旗信号（加分项）

- 主动提 ContextPruning、RetrievalProfile 等业务特有优化
- 能区分已落地 vs 规划架构（agent_architecture_redesign）
- 排障先查日志文件与 env 开关
- 安全答 RAG indirect injection + Allowlist 纵深
- 部署答 SQLite checkpoint 多 worker 瓶颈
