# 用户隔离与 Skills 动态加载安全方案

> **最后同步**：2026-06-01  
> 📊 安全分层 → [plans/qa/11_guardrails_security.md](../../plans/qa/11_guardrails_security.md) 图 13–14

本模块详述了核电子学智能助手的安全基座，特别是原生环境（非沙箱）下的用户数据隔离与 Skills 动态加载机制。

### 隔离总览

```text
HTTP 请求 (user_id 来自 Token)
        │
        ▼
build_internal_thread_key(user_id, thread_id)
        → u_{uid}__t_{tid}
        │
        ├─► ScopedStorage
        │     users/{uid}/threads/{tid}/artifacts/
        │     save_file 校验 object_name 必须以 users/{uid}/ 开头
        │
        ├─► ScopedSkillsProvider
        │     过滤 selected_skills ∩ 可见 Skill
        │     read_skill_reference 防路径穿越
        │
        └─► LangGraph Checkpointer（按 internal thread_id 分区）

工件 HTTP：GET /api/v2/threads/{id}/artifacts/{path}
        → 校验 Conversation.user_id（见 user_isolation 实施方案）
```

---

## 🛡️ 用户数据安全隔离 (Native User Isolation)

在原生环境下（禁用沙箱时），系统通过代码层的强制约束确保用户数据的私密性与安全性。核心设计由 `ScopedStorage` 类承载。

### 1. 作用域存储核心实现 (`ScopedStorage`)
*   **物理文件**: `backend/core/storage/scoped_storage.py`（封装 MinIO 操作；历史文档曾写作 `backend/storage/scoped_storage.py`）
*   **物理机制**: 封装了 MinIO 客户端操作。实例化时接收 `user_id` 和 `thread_id`，强制在所有的云端对象操作上添加以下安全路径前缀（即 "路径作用域沙箱"）：
    ```text
    self.prefix = f"users/{user_id}/threads/{thread_id}/artifacts/"
    ```

#### 1.1 关键 API 方法详解

*   **`read_file(filename) -> bytes`**:
    *   **双读兼容策略**: 系统优先尝试读取当前作用域下的完整路径：`users/{user_id}/threads/{thread_id}/artifacts/{filename}`。
    *   **历史回退兜底**: 若上述对象不存在，自动捕获异常并尝试回退读取 legacy 旧版共享路径 `artifacts/{thread_id}/{filename}`，以兼容旧版会话，确保数据迁移过渡的平滑性。
    *   若上述两阶段均不可达，抛出标准 `FileNotFoundError`。

*   **`save_file(filename, content, content_type) -> str`**:
    *   将传入的 `bytes` 写入用户隔离前缀下，并强行指定相应的 HTTP `Content-Type`（如 `application/pdf`, `image/png`），返回最终的对象存储键（object name）。

*   **`save_local_file(object_name, local_file_path) -> str`**:
    *   **P0 越权拦截**: 在上传本地物理临时文件时，方法会强制校验 `object_name`。如果检测到其不以 `users/{user_id}/` 开头，系统会抛出 `PermissionError` 拒绝写入。这彻底封堵了智能体通过篡改文件名越权篡改其他用户数据或系统根目录的隐患。

*   **`list_objects(prefix, recursive) -> list[str]`**:
    *   仅列出该用户当前 thread 下的 artifacts 列表，用于前端工件侧栏（ArtifactSidebar）进行动态渲染。

*   **`delete_all_files()`**:
    *   **级联物理删除**: 当会话被彻底删除时，此方法执行批量对象删除逻辑。它同时清空当前的规范前缀 `self.prefix` 和历史前缀 `artifacts/{self.thread_id}/`，使用 MinIO 批量删除接口 (`client.remove_objects`)，保证零数据残留。

---

## 🛠️ Skills 动态加载与隔离 (Dynamic Skills Loading)

为了在非沙箱环境下支持 Skills 的安全调用，系统实现了一套基于 `ScopedSkillsProvider` 的动态分发机制。

### 1. 动态技能提供器 (`ScopedSkillsProvider`)
*   **物理文件**: `backend/core/skills/scoped_skills_provider.py`
*   **物理机制**: 负责 Skills Markdown 文件及其附加资源的读取、可见性过滤及 Prompt 动态生成。

#### 1.1 关键 API 方法与安全机制

*   **`get_available_skills_for_user(user_id, selected_skills) -> list[Skill]`**:
    *   扫描并缓存 `skills/public/` 与 `skills/custom/` 目录下的所有 yaml/markdown 技能声明。
    *   根据入参 `selected_skills` 集合进行交集过滤，筛选出当前会话绑定的 Skill 实例列表。

*   **`build_skills_prompt_section(skills) -> str`**:
    *   **JIT 动态 Prompt 编译器**: 将过滤出的 Skill 列表动态编译成 XML 规范段落，注入至大模型的系统 Prompt 中：
    ```xml
    <skill_system>
    You have access to skills that provide optimized workflows for specific tasks...
    **Progressive Loading Pattern:**
    1. 💥 CRITICAL: DO NOT attempt to call the skill name as a tool or function! Skills are Markdown files, not executable tools.
    2. When a user query matches a skill's use case, you must immediately call the `read_local_skill_file` tool...
    
    <available_skills>
        <skill>
            <name>academic-search</name>
            <description>学术文献多源检索技能...</description>
            <location>/mnt/skills/public/academic-search/SKILL.md</location>
        </skill>
    </available_skills>
    </skill_system>
    ```
    *   这种设计通过严格的**渐进式加载模式**（Progressive Loading Pattern）引导大模型“按需读取”，减少首 Token 延迟。

*   **`read_skill_content(skill_name) -> str`**:
    *   读取技能的核心 `SKILL.md` 文本内容。

*   **`read_skill_reference(skill_name, reference_path) -> str`**:
    *   **深度路径遍历拦截 (Path Traversal Protection)**:
        当 Skill 的 `SKILL.md` 引用其同级文件夹下的参考资料时（如 `references/index.md`），智能体会调用该方法拉取。
    *   **安全防线**: 方法内部利用 `Path.resolve()` 将相对路径转换为绝对物理路径，并使用 `resolved.relative_to(skill_dir_resolved)` 强制校验其是否依然位于该 Skill 的根目录下。
    *   一旦检测到利用 `../` 企图读取外部目录敏感文件（如 `/etc/passwd`），立刻拦截并抛出 `PermissionError`，实现了非沙箱环境下绝对的技能文件访问安全。

---

## 📋 关联实施方案文档

| 方案名称 | 功能重点 | 链接 |
| :--- | :--- | :--- |
| **原生环境隔离方案** | MinIO 存储前缀、加固版工具、身份提取 | [查看详情](../原生环境_非沙箱_用户隔离实施方案.md) |
| **Skills 动态加载** | ScopedSkillsProvider、ToolAssemblyPipeline | [skill_dynamic_tool_loading_plan.md](../skill_dynamic_tool_loading_plan.md) |
| **基础文件系统隔离** | 沙箱容器隔离（针对文件系统工具） | [用户级隔离实施方案.md](../用户级隔离实施方案.md) |

---
© 2026 核电子学智能助手项目组.
