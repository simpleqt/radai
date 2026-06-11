from operator import add
from typing import Annotated, Any, TypedDict


class BaseWorkflowState(TypedDict):
    """
    工作流的基础状态定义
    所有具体的工作流状态都应该继承这个类
    """

    # 错误信息，用于记录工作流执行过程中的错误
    error: str | None

    # 消息历史，用于存储对话或处理过程中的消息
    # 使用 add 操作符支持追加模式
    messages: Annotated[list[Any], add]

    # 上下文数据，用于存储工作流的中间结果
    context: dict[str, Any]

    # 当前步骤
    current_step: str
