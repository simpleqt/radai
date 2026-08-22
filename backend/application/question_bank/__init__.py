"""基础力学课程智能体 · 题库管理（application 层编排）。

- ``images``: 题图存储边界（稳定 object_name + HMAC 签名代理，MinIO 优先本地降级）。
- ``parser``: docx 模板解析（固定标记切分 + 逐题校验，pandoc/python-docx 双路径）。
- ``service``: 批次两段式入库、题目与知识点字典管理（教师侧）。
- ``practice``: 学生练习（脱敏下发、服务端判分、做题记录、错题本）。
"""

from backend.application.question_bank.service import (
    QuestionBankError,
    append_question_image,
    batch_to_dict,
    confirm_batch,
    create_batch,
    create_knowledge_point,
    create_question,
    delete_batch,
    delete_knowledge_point,
    get_batch,
    get_question,
    knowledge_point_names,
    list_knowledge_points,
    question_page,
    question_to_dict,
    remove_question_image,
    update_knowledge_point,
    update_question,
)

__all__ = [
    "QuestionBankError",
    "append_question_image",
    "batch_to_dict",
    "confirm_batch",
    "create_batch",
    "create_knowledge_point",
    "create_question",
    "delete_batch",
    "delete_knowledge_point",
    "get_batch",
    "get_question",
    "knowledge_point_names",
    "list_knowledge_points",
    "question_page",
    "question_to_dict",
    "remove_question_image",
    "update_knowledge_point",
    "update_question",
]
