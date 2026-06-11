from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar, cast

from langgraph.graph import END, StateGraph

from .state import BaseWorkflowState

StateT = TypeVar("StateT", bound=BaseWorkflowState)


class BaseWorkflow(ABC, Generic[StateT]):
    """
    工作流基类
    封装了 LangGraph 的构建过程
    """

    def __init__(self, state_class: type[StateT] | None = None):
        resolved_state_class = cast(
            type[StateT], BaseWorkflowState if state_class is None else state_class
        )
        self.state_class = resolved_state_class
        self.workflow = StateGraph(resolved_state_class)
        self.app: Any = None
        self._is_compiled = False

    @property
    def graph(self) -> StateGraph:
        return self.workflow

    @abstractmethod
    def build(self) -> None:
        """
        构建工作流图
        在此方法中添加节点和边
        """
        pass

    def compile(self) -> Any:
        """
        编译工作流
        """
        if not self._is_compiled:
            self.build()
            self.app = self.workflow.compile()
            self._is_compiled = True
        return self.app

    async def run(self, inputs: dict[str, Any]) -> Any:
        """
        运行工作流
        """
        if not self._is_compiled:
            self.compile()

        return await self.app.ainvoke(inputs)

    def add_node(self, name: str, action: Any) -> None:
        """添加节点"""
        self.workflow.add_node(name, action)

    def add_edge(self, start_key: str, end_key: str) -> None:
        """添加普通边"""
        self.workflow.add_edge(start_key, end_key)

    def add_conditional_edges(
        self,
        source_key: str,
        condition: Any,
        conditional_edge_mapping: dict[str, str] | None = None,
    ) -> None:
        """添加条件边"""
        self.workflow.add_conditional_edges(
            source_key, condition, conditional_edge_mapping
        )

    def set_entry_point(self, key: str) -> None:
        """设置入口点"""
        self.workflow.set_entry_point(key)

    def set_finish_point(self, key: str) -> None:
        """设置结束点（从该节点连接到 END）"""
        self.workflow.add_edge(key, END)
