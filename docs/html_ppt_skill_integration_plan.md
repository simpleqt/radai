# HTML PPT Skill 当前实现

> 最后同步：2026-06-08
> 状态：已实现
> 旧 `NEFullFeatureAgent`、`custom/agents/support/tool_assembly.py` 和本地工件回退方案已废弃。

## 1. 当前入口

| 能力 | 当前路径 |
| :--- | :--- |
| Skill 声明 | `backend/skills/public/html-ppt/SKILL.md` |
| 工具注册 | `backend/config/tools/registry.yaml` |
| 工具策略 | `backend/config/tools/policies.yaml` |
| 工具实现 | `backend/application/tools/providers/html_ppt_tool.py` |
| 工具组装 | `backend/application/tools/tool_assembly_pipeline.py` |
| 工件元数据 | `backend/application/artifacts/artifact_utils.py` |
| 文件下载代理 | `backend/api/routers/files.py` |

## 2. 运行链路

```text
用户选择 html-ppt Skill
        │
        ▼
PromptBuilder 注入 PPT 强制规则
        │
        ▼
read_local_skill_file("html-ppt")
        │
        ▼
ToolAssemblyPipeline 注入 generate_html_ppt
        │
        ▼
generate_html_ppt_tool
        ├── 清理模型工具参数
        ├── 校验 template/theme
        ├── 解析或回退 slides_outline
        ├── 渲染完整 HTML deck
        ├── 上传 MinIO
        └── 返回工件 metadata
                │
                ▼
AgentStreamRuntime / artifact mapper
                │
                ▼
前端工件预览与下载
```

## 3. Skill 使用约束

PPT 请求必须：

1. 加载 `html-ppt` Skill；
2. 根据场景选择 template 和 theme；
3. 必要时先检索领域资料；
4. 调用 `generate_html_ppt` 交付最终工件；
5. 不能用 bash、本地写文件或普通正文替代工件交付。

标准调用参数：

```text
template: tech-sharing
theme: blueprint
title: 演示文稿标题
filename: optional-name.html
slides_outline: 每页使用 --- 分隔
```

## 4. slides_outline 规范

```text
# 封面
一句副标题
- 关键词 1
- 关键词 2
---
# 目录
- 第一部分
- 第二部分
---
# 核心内容
结论性说明。
- 要点 A
- 要点 B
```

工具会清理模型可能产生的 `</parameter>` 等残片。若大纲为空、过短或结构不足，会生成多页兜底大纲，避免输出只有封面的单页工件。

## 5. 工件与权限

HTML 文件上传到用户和线程隔离的 MinIO 路径：

```text
users/{user_id}/threads/{thread_id}/html-ppt/{uuid}-{filename}
```

前端不直接使用 MinIO 内部地址，而是通过文件代理和登记后的用户文件 metadata 预览或下载。工件必须保持：

- 用户隔离；
- 线程归属；
- HTML MIME 类型；
- 可预览标识；
- 可下载 URL；
- 可恢复的 artifact path。

## 6. Workflow 支持

`generate_html_ppt` 属于 full 能力工具，可用于：

- `full`
- `deep_research`
- `plan_solve`
- `multi_agent_parallel`
- `multi_agent_alternating`

是否实际注入仍取决于：

- agent ToolPolicy；
- 是否选择 `html-ppt` Skill；
- ToolSpec domain/workflow 限制。

## 7. 维护规则

新增模板或主题时：

1. 将资源放入 Skill 模板目录；
2. 确保工具可以从磁盘动态读取有效名称；
3. 更新 `SKILL.md` 推荐表；
4. 增加模板存在性和多页渲染测试；
5. 禁止在 Prompt 中维护与磁盘不一致的硬编码全集。

## 8. 验证

至少验证：

- template/theme 非法值明确报错；
- `</parameter>` 残片被清理；
- slides outline 不被错误归一化或截断；
- 弱大纲能回退为多页；
- MinIO 对象路径包含正确 user/thread；
- artifact metadata 可供前端预览；
- 生成失败不会返回伪造的本地路径。

标准化回归测试：

```bash
python \
  -m pytest backend/tests/test_agent_platform_standardization.py -q
```
