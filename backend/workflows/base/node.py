from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from .state import BaseWorkflowState

StateT = TypeVar("StateT", bound=BaseWorkflowState)


class BaseNode(ABC, Generic[StateT]):
    """
    工作流节点的基类
    """

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    async def execute(self, state: StateT) -> dict[str, Any]:
        """
        执行节点逻辑

        Args:
            state: 当前工作流状态

        Returns:
            Dict[str, Any]: 状态更新
        """
        pass

    async def __call__(self, state: StateT) -> dict[str, Any]:
        """
        允许像函数一样调用节点
        """
        try:
            return await self.execute(state)
        except Exception as e:
            return {"error": str(e)}
