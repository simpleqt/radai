"""智能体配置及知识库绑定 ORM 定义"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.db.models import Base


class Agent(Base):
    """智能体展示与绑定记录表。

    本表不创建可执行智能体。每条记录必须引用一个已经通过统一验证的
    YAML Agent Profile；这里只保存前端展示、启停、排序和知识库绑定元数据。
    """

    __tablename__ = "agents"

    agent_code = Column(
        String(64),
        primary_key=True,
        comment="智能体唯一编码，引用运行时 Agent Profile code",
    )
    agent_name = Column(String(100), nullable=False, comment="前端展示名称")
    description = Column(Text, nullable=True, comment="智能体描述")
    is_active = Column(Boolean, nullable=False, default=True, comment="是否启用")
    sort_order = Column(SmallInteger, nullable=False, default=0, comment="排序权重（升序）")

    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    kb_bindings = relationship(
        "AgentKnowledgeBase",
        back_populates="agent",
        cascade="all, delete-orphan",
        order_by="AgentKnowledgeBase.sort_order",
        foreign_keys="AgentKnowledgeBase.agent_code",
        primaryjoin="Agent.agent_code == AgentKnowledgeBase.agent_code",
    )

    __table_args__ = (
        Index("idx_agent_active", "is_active"),
        Index("idx_agent_sort", "sort_order"),
        {"comment": "智能体展示与知识库绑定元数据表"},
    )

    def __repr__(self) -> str:
        return f"<Agent(agent_code={self.agent_code}, agent_name={self.agent_name})>"


class AgentKnowledgeBase(Base):
    """智能体-知识库绑定表

    记录每个智能体默认挂载哪些知识库，支持排序。
    """

    __tablename__ = "agent_kb_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 注意：因 knowledge_bases 表字符集排序规则与本表不同，FK 约束在 DB 层省略，
    # 完整性由应用层保证；agent_code 对 agents 表的 FK 保留。
    agent_code = Column(
        String(64),
        nullable=False,
        index=True,
        comment="关联智能体编码",
    )
    kb_id = Column(
        String(36),
        nullable=False,
        index=True,
        comment="关联知识库 ID",
    )
    sort_order = Column(
        SmallInteger, nullable=False, default=0, comment="展示排序（升序）"
    )
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    agent = relationship(
        "Agent",
        back_populates="kb_bindings",
        foreign_keys="[AgentKnowledgeBase.agent_code]",
        primaryjoin="AgentKnowledgeBase.agent_code == Agent.agent_code",
    )

    __table_args__ = (
        UniqueConstraint("agent_code", "kb_id", name="uk_agent_kb"),
        Index("idx_agent_kb_agent", "agent_code"),
        Index("idx_agent_kb_kb", "kb_id"),
        {"comment": "智能体-知识库绑定表"},
    )

    def __repr__(self) -> str:
        return (
            f"<AgentKnowledgeBase(agent_code={self.agent_code}, kb_id={self.kb_id})>"
        )
