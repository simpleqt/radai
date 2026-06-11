# 访客独立 API 端点实施方案

> **文档属性**：历史实施方案，保留用于追踪设计演进。  
> **实现状态**：✅ 已落地（后续已继续平台化收口）  
> **路径迁移（2026-06-08）**：文中 `backend/custom/` 路径已废弃，API 已迁入 `backend/api/routers/`，服务已迁入 `backend/application/`。  
> - 独立服务：`backend/api/guest_http_server.py`（端口 8083）  
> - 访客路由：`/api/v2/guest/query`  
> - Checkpointer：**AsyncSqliteSaver**（`guest_checkpoints.db`），非 Redis TTL  
> - 前端：`guest/`（端口 8085，IndexedDB 主存）
> - 当前实现：`/api/v2/guest/query` 直接调用 `AgentService.run_stream()`，不再复用 `NEFullFeatureAgent`

## 1. 目标与范围

### 1.1 目标
- 新建独立访客接口：/api/v2/guest/query。
- 访客能力与普通网页端对话体验一致（流式返回、上下文连续）。
- 访客对话记录仅保存在浏览器本地，不写入服务端持久化数据库。
- 服务端只提供对话推理服务与必要的短期会话态维护。

### 1.2 范围
- 本方案仅覆盖访客模式后端设计与实现。
- 不改动现有登录用户接口（现已统一为 `/api/v2/agent-runtime/query`）。
- 暂不支持深度研究模式（deep-research / deep_research）。

## 2. 总体架构

### 2.1 双通道架构
- 登录用户通道：现统一为 `/api/v2/agent-runtime/query`（用户认证、会话可入库）。
- 访客通道：新增 /api/v2/guest/query（无用户体系依赖、无数据库写入）。

### 2.2 核心原则
- 接口隔离：访客逻辑与用户逻辑彻底解耦，避免互相影响。
- 数据最小化：服务端不保存访客长期会话历史。
- 上下文可用：通过 thread_id + 短期检查点维持连续对话。
- 安全可控：限流、TTL、输入校验、防滥用。

## 3. API 设计

### 3.1 新增路由
- 方法：POST
- 路径：/api/v2/guest/query
- 鉴权：无需登录令牌
- 内容类型：application/json
- 返回：text/event-stream（SSE）

### 3.2 请求体
- question: string，必填，用户问题
- thread_id: string，可选，前端本地会话标识
- model: string，可选，模型名（需在白名单内）
- use_search: bool，可选，默认 true
- kb_ids: string[]，可选
- selected_skills: string[]，可选（仅允许访客白名单）
- thinking: bool，可选但忽略，后端统一关闭
- subagent_enabled: bool，可选但固定 false
- mode: string，可选但固定 normal
- task_type: string，可选但忽略

### 3.3 响应事件
- chunk：流式文本片段
- status：过程状态
- event：工具/流程事件
- done：结束标记（SSE 的 [DONE]）
- error：错误信息

### 3.4 约束策略
- 若请求显式携带 deep-research 相关字段，返回 400。
- 访客线程标识格式校验：仅允许字母、数字、下划线、横杠、点。
- 服务端对 thread_id 为空时自动生成 UUID 并回传首个 status 事件。

## 4. 上下文与存储策略

### 4.1 前端本地存储（主存）
- 使用 IndexedDB 存储访客对话列表与消息。
- 存储键建议：guest_conversations、guest_messages。
- 前端负责会话创建、重命名、删除、历史展示。

### 4.2 服务端短期态（辅存）
- 使用 checkpointer 或 Redis 保留短期上下文状态。
- 键设计：guest:{device_id}:{thread_id}
- TTL 建议：24 小时（可配置）。
- 到期自动清除，不做长期归档。

### 4.3 数据边界
- 不写 Conversation 表。
- 不写访客消息历史到关系型数据库。
- 日志中避免记录完整原文（按需脱敏与截断）。

## 5. 后端实现方案

### 5.1 文件与模块
- 新增文件：backend/custom/api/v2/guest.py
- 路由注册：backend/custom/api/v2/__init__.py 或对应 HTTP server 组装位置
- 可选新增：backend/custom/services/guest_session.py（封装 Redis/TTL 操作）

### 5.2 关键逻辑
1) 参数标准化
- 强制 subagent_enabled = false。
- 强制禁用 deep-research 相关模式。
- thinking 统一忽略。

2) 线程键生成
- external_thread_id = payload.thread_id or uuid
- internal_thread_id = g_{device_id}__t_{external_thread_id}
- device_id 来源优先级：X-Device-ID 请求头 > 客户端生成匿名 ID。

3) 智能体调用
- 调用 `AgentService.run_stream()`。
- 由 `WorkflowRegistry + StandardAgentWorkflow + ProfiledAgentExecutor` 执行 lightweight 请求。
- 在 `guest_agent.py` 路由边缘将标准 AgentEvent 局部映射为访客前端使用的事件格式。

4) 返回 SSE
- 与当前标准事件协议保持一致，并在 guest 路由边缘做最小格式映射，降低前端适配成本。

### 5.3 伪代码

```python
@router.post('/query')
async def query_guest(payload: GuestQueryRequest, request: Request):
    reject_if_deep_research(payload)

    device_id = resolve_device_id(request)
    external_tid = payload.thread_id or str(uuid.uuid4())
    internal_tid = f'g_{device_id}__t_{external_tid}'

    async def stream_generator():
        yield sse_status({'thread_id': external_tid, 'guest': True})
        async for event in agent_service.run_stream(
            build_guest_agent_request(payload, internal_tid)
        ):
            yield map_standard_event_to_guest(event)
        yield sse_done()

    return StreamingResponse(stream_generator(), media_type='text/event-stream')
```

## 6. 安全与风控

### 6.1 限流
- 基于 IP + device_id 的双维度限流。
- 建议策略：
  - 每分钟请求上限：20
  - 并发流式连接上限：3
  - 超限返回 429

### 6.2 输入保护
- question 长度上限（例如 8000 字符）。
- kb_ids 数量上限（例如 10）。
- selected_skills 仅允许白名单。

### 6.3 资源保护
- SSE 最大持续时长（例如 120 秒）。
- 单次输出 token 上限（由模型侧或代理层限制）。
- 超时中断并返回可读错误。

## 7. 兼容与前端协同

### 7.1 前端请求规则
- 访客模式统一调用 /api/v2/guest/query。
- 登录模式现调用 /api/v2/agent-runtime/query。
- 前端负责生成并持久化 device_id 与 thread_id。

### 7.2 事件兼容
- 访客接口事件字段与 agent 接口保持一致。
- 前端可复用同一套消息渲染和状态处理逻辑。

## 8. 错误码约定

- 400：参数非法（含 deep-research 禁止项）
- 413：输入过长
- 429：限流触发
- 500：服务内部异常
- 503：模型或下游服务不可用

## 9. 实施步骤

### 阶段 1：接口落地
- 新建 guest.py 路由与请求模型。
- 接入标准 `AgentService.run_stream()` 输出。
- 完成 deep-research 禁用逻辑。

### 阶段 2：会话短期态
- 接入访客 checkpointer 或 Redis TTL。
- 增加 device_id/thread_id 规范校验。

### 阶段 3：风控与观测
- 增加限流中间件。
- 增加结构化日志与基础指标（QPS、失败率、超时率）。

### 阶段 4：联调验收
- 与前端完成访客模式联调。
- 回归测试 SSE、上下文连续、异常处理。

## 10. 验收标准

- 访客无需登录即可进行连续多轮问答。
- 刷新页面后，前端可从本地恢复对话并继续同一 thread_id。
- 服务端数据库中无访客会话持久化记录。
- deep-research 请求在访客端点被明确拒绝。
- 高并发下可稳定返回并受限流策略保护。

## 11. 测试清单

- 单元测试
  - 参数校验与默认值
  - deep-research 拒绝逻辑
  - thread_id 生成与格式验证

- 集成测试
  - SSE 正常流结束
  - SSE 中断与超时
  - Redis TTL 到期后上下文失效

- 安全测试
  - 限流生效
  - 恶意超长输入拦截
  - 非白名单技能拦截

---

本方案通过独立访客端点实现“能力一致、数据隔离、实现解耦”的目标，能够在不干扰现有登录用户体系的前提下，快速上线移动端与网页端访客模式。
