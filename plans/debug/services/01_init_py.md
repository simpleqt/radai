# 审查：`backend/services/__init__.py`

**文件规模**: 12 行  
**职责**: 服务层包入口，声明对外导出符号

---

## 1. 当前实现

```python
from backend.services.directory_service import DirectoryService
from backend.services.startup_task_processor import StartupTaskProcessor, get_startup_task_processor

__all__ = [
    "DirectoryService",
    "StartupTaskProcessor",
    "get_startup_task_processor",
]
```

---

## 2. 问题清单

### P2-01 导出范围与实际使用严重脱节

| 被使用但未从包根导出 | 直接 import 路径 |
|---------------------|------------------|
| `ChatFileIndex` / `get_index_manager` | `backend.services.chat_file_index` |
| `ChatFileParser` / `clean_surrogates` / `clean_document_images` | `backend.services.chat_file_parser` |
| `user_file_service` 全部函数 | `backend.services.user_file_service` |
| `directory_service` 单例 | `backend.services.directory_service` |

**影响**: 新开发者误以为 `from backend.services import X` 即完整服务 API；IDE「包公开接口」与真实边界不一致。

**建议**: 要么扩展 `__all__` 收录常用符号，要么在模块 docstring 明确「请按子模块 import，本包仅导出 KB 目录与启动任务」。

---

### P2-02 导出风格不统一

- `DirectoryService` 导出**类**，调用方多通过 `directory_service` 单例（在 `directory_service.py` 末尾）使用。
- `StartupTaskProcessor` 导出类 + `get_startup_task_processor()` 工厂。
- 其余服务为**函数模块**，无类封装。

**建议**: 文档说明「类服务 vs 函数模块」约定，避免混用。

---

### P3-01 包级 docstring 过于简略

仅一行「服务层模块」，未描述子模块划分、线程模型（内存索引单进程）、依赖方向（services 不应 import routers）。

---

## 3. 依赖关系（只读）

```
__init__.py
  ├── directory_service.py
  └── startup_task_processor.py
```

未形成循环依赖；`chat_file_*` / `user_file_service` 为叶子模块，由 API / Agent 直接引用。

---

## 4. 测试

无针对 `__init__.py` 的测试；可通过 `import backend.services` 冒烟测试验证导出符号可 import。

---

## 5. 本文件结论

| 维度 | 评分 | 说明 |
|------|------|------|
| 正确性 | 无运行时错误 | 导出本身正确 |
| 可维护性 | 中 | 与项目 import 习惯不一致 |
| 安全性 | N/A | |
| 建议动作 | 文档/导出对齐 | 非紧急 |
