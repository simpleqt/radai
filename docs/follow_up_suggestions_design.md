# 智能追问建议当前实现

> 最后同步：2026-06-08
> 状态：已实现
> 当前实现位于 application 层，不再依赖旧 agent API 或 `custom/agents/support`。

## 1. 目标

主回答流式输出过程中，平台根据问题、可见回答、工具摘要和 Skill 上下文生成 2 至 4 条可继续追问的问题。追问生成失败不得影响主回答。

## 2. 当前模块

| 职责 | 当前路径 |
| :--- | :--- |
| 生成与回退 | `backend/application/suggestions/follow_up_service.py` |
| Prompt 构建 | `backend/application/suggestions/follow_up_prompts.py` |
| 标准事件映射 | `backend/domain/agents/events.py` |
| 访客流接入 | `backend/api/routers/guest_agent.py` |
| 登录态标准流 | `backend/application/agent_executors/` |

## 3. 输入上下文

`FollowUpContext` 包含：

- `user_question`
- `assistant_answer`
- `tool_summary`
- `context_summary`
- `agent_mode`
- `selected_skills`
- `model_name`
- `locale`
- `use_llm`

服务会限制上下文长度，避免追问生成消耗过多 token。

## 4. 生成策略

```text
启动异步追问任务
      │
      ├── 有可用上下文且启用 LLM
      │      └── 轻量模型生成 JSON suggestions
      │
      └── 超时 / 解析失败 / 模型异常
             └── 规则模板回退
                    │
                    ▼
             去重、长度过滤、占位符过滤
                    │
                    ▼
             follow_up.created
```

默认行为：

- 数量限制为 2 至 4 条；
- 模型思考关闭；
- 生成设置独立超时；
- 支持答案正文和早期上下文两种 Prompt；
- 学术检索、知识库问答和通用问答使用不同回退模板；
- 超过 60 字、重复、占位符或已经包含在回答中的建议会被过滤。

## 5. 流式时序

`FollowUpStreamState` 独立运行追问任务，并记录：

- 首个可见回答时间；
- 当前可见字符数；
- 是否已经发送。

只有满足最小展示延迟、最小可见字符数或主流结束强制收尾时，追问才会发出，避免建议出现在正文之前。

标准事件：

```json
{
  "type": "follow_up.created",
  "payload": {
    "suggestions": ["问题一", "问题二", "问题三"],
    "metadata": {
      "source": "llm",
      "latency_ms": 320,
      "model": "configured-model"
    }
  }
}
```

Guest 路由在边缘层临时映射为访客前端格式，平台内部仍使用标准 `AgentEvent`。

## 6. Workflow 差异

追问侧重点按当前 workflow 和 Skill 决定：

| Workflow / Skill | 建议侧重 |
| :--- | :--- |
| `lightweight` / guest | 概念延伸、应用场景、简明解释 |
| `full` | 文献细节、标准依据、实验数据 |
| `deep_research` | 证据链、研究限制、补充维度 |
| `plan_solve` | 计划假设、缺失条件、验证步骤 |
| `multi_agent_parallel` | 各子任务结论差异、证据合并 |
| `multi_agent_alternating` | 轮次分歧、收敛依据、待确认问题 |
| `academic-search` Skill | 作者、方法、相关工作、最新研究 |

## 7. 持久化

建议可以 best-effort 写入最新 assistant message 的 checkpoint metadata：

```json
{
  "follow_up_suggestions": [
    {"id": "sug_xxx", "text": "后续问题"}
  ]
}
```

持久化失败只记录调试日志，不影响流式回答。

## 8. 配置

主要配置位于 `backend/config/settings.py` 对应的 settings：

- `follow_up_suggestions_enabled`
- `follow_up_suggestion_count`
- `follow_up_suggestion_max_tokens`
- `follow_up_suggestion_min_display_delay_ms`
- `follow_up_suggestion_min_visible_chars`
- `follow_up_guest_enabled`

追问任务不再使用固定毫秒超时。它与主回答并行运行，以主回答流结束作为截止点；
若届时模型仍未返回，则立即取消追问模型调用并使用模板回退，不延迟主回答结束。

## 9. 验证要求

至少覆盖：

- LLM JSON 对象和数组解析；
- `<think>`、代码块和脏输出清理；
- 超时与异常回退；
- 重复、超长和占位符过滤；
- 追问不会早于主回答展示；
- 事件只发送一次；
- checkpoint 持久化失败不影响主流；
- 协同 workflow 能使用对应模式上下文。
