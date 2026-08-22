"""SQLAlchemy 数据模型(radai:智能体平台侧 + 共享账号/模型/知识库目录只读)。

导入顺序即 mapper 注册顺序:``base`` 必须最先。子模块一律
``from backend.db.models.base import Base``,不要回头从本包导入。
"""

from backend.db.models.account import AdminAuditLog, ApiKey, User
from backend.db.models.agent import Agent, AgentKnowledgeBase
from backend.db.models.base import Base, DocumentType, TaskStatus
from backend.db.models.context_artifact import ContextArtifactRecord
from backend.db.models.conversation import Conversation
from backend.db.models.course_eval import (
    CeAchievementConfig,
    CeAuditLog,
    CeCourse,
    CeCourseGoal,
    CeEvalTask,
    CeExamItem,
    CeExamPaper,
    CeExamSection,
    CeGoalMapping,
    CeMaterial,
    CeReport,
    CeReportTemplate,
    CeReportVersion,
    CeScoreRecord,
    CeSemesterTask,
    CeStatsSnapshot,
    CeStudent,
)
from backend.db.models.custom_learning import (
    LearningNodeAttempt,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
)
from backend.db.models.dataset import ExperimentDataset, ExperimentDatasetAcl
from backend.db.models.feedback import MessageFeedback
from backend.db.models.groundwater_project import (
    GroundwaterProject,
    GroundwaterProjectUser,
)
from backend.db.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseAcl,
    KnowledgeBaseDirectory,
    KnowledgeBaseDocument,
    KnowledgeBaseStatus,
    PendingFile,
    PendingFileStatus,
    SelfIngestChunk,
    SelfIngestJob,
)
from backend.db.models.learning_style import (
    LearningStyleAttempt,
    LearningStyleProfile,
)
from backend.db.models.llm_model import LlmModel, LlmModelAccess
from backend.db.models.question_analytics import (
    CourseQuestionRecord,
    CourseQuestionReport,
)
from backend.db.models.question_bank import (
    QuestionBankAttempt,
    QuestionBankBatch,
    QuestionBankKnowledgePoint,
    QuestionBankQuestion,
)
from backend.db.models.skill import (
    AgentSkillBinding,
    ManagedSkill,
    SkillAcl,
    SkillAuditLog,
    SkillVersion,
)
from backend.db.models.user_file import SessionFile, UserFile, UserFilePermission

__all__ = [
    "Base",
    "DocumentType",
    "TaskStatus",
    "User",
    "ApiKey",
    "AdminAuditLog",
    "Agent",
    "AgentKnowledgeBase",
    "ContextArtifactRecord",
    "Conversation",
    "MessageFeedback",
    "ExperimentDataset",
    "ExperimentDatasetAcl",
    "GroundwaterProject",
    "GroundwaterProjectUser",
    "KnowledgeBase",
    "KnowledgeBaseAcl",
    "KnowledgeBaseDirectory",
    "KnowledgeBaseDocument",
    "KnowledgeBaseStatus",
    "PendingFile",
    "PendingFileStatus",
    "SelfIngestChunk",
    "SelfIngestJob",
    "LlmModel",
    "LlmModelAccess",
    "CourseQuestionRecord",
    "CourseQuestionReport",
    "QuestionBankAttempt",
    "QuestionBankBatch",
    "QuestionBankKnowledgePoint",
    "QuestionBankQuestion",
    "UserFile",
    "UserFilePermission",
    "SessionFile",
    "LearningStyleAttempt",
    "LearningStyleProfile",
    "LearningNodeAttempt",
    "LearningPlan",
    "LearningPlanAdjustment",
    "LearningPlanNode",
    "ManagedSkill",
    "SkillVersion",
    "SkillAcl",
    "SkillAuditLog",
    "AgentSkillBinding",
    "CeCourse",
    "CeSemesterTask",
    "CeCourseGoal",
    "CeExamPaper",
    "CeExamSection",
    "CeExamItem",
    "CeStudent",
    "CeScoreRecord",
    "CeGoalMapping",
    "CeAchievementConfig",
    "CeReportTemplate",
    "CeReport",
    "CeReportVersion",
    "CeStatsSnapshot",
    "CeEvalTask",
    "CeAuditLog",
    "CeMaterial",
]
