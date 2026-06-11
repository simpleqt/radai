# 第三遍补充：`__init__.py`

## 依赖关系图

```text
┌───────────────────────────────────────────────────────────────┐
│                         经 __init__ 导出                         │
│ DirectoryService  │  StartupTaskProcessor  │  get_startup_tas │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                        业务直接 import 子模块                        │
│ chat_file_index  │  chat_file_parser  │  user_file_service  │ │
└───────────────────────────────────────────────────────────────┘

连接:
  api/routers ──────► 业务直接 import 子模块
  custom/agents ──────► 业务直接 import 子模块
  exported ──────► DirectoryService
  StartupTaskProcessor ──────► get_startup_task_processor
  http_server ──────► get_startup_task_processor

```

## 与项目分层的一致性

| 层级 | 本包角色 | 风险 |
|------|----------|------|
| `api/routers` | 应薄，当前部分路由内联 200+ 行 | 解析/索引逻辑已下沉到 services，尚可 |
| `services` | 领域服务 | 与 `rag/` 知识库链路命名易混（`add_document` 两处含义不同） |
| `db/crud` | 目录 CRUD 被 `DirectoryService` 包装 | `directory_service.crud` 被路由穿透 |

## 建议的 `__all__` 扩展（文档级，非代码）

若保持包入口有用，建议至少：

```python
# 文档建议项，非已实现
__all__ = [
    "DirectoryService", "directory_service",
    "StartupTaskProcessor", "get_startup_task_processor",
    "ChatFileParser", "ParseResult",
    "get_index_manager", "ChatFileIndex",
    # user_file_service 函数较多，建议保持子模块 import
]
```

## 命名冲突警示

| 符号 | services 包 | rag 包 |
|------|-------------|--------|
| `add_document` | `ChatFileIndex.add_document` 会话块索引 | `KBManager.add_document` 知识库入库 |
| `remove_document` | 会话索引删块 | 知识库删文档 |

新成员 onboarding 时应在 `__init__.py` docstring 中写明区分。

## 第三遍结论

本文件本身无运行时缺陷；**包边界文档缺失**是主要可维护性问题。优先级低于子模块 P0。
