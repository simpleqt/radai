"""课程试卷分析与教学目标达成度评价 ORM 模型。

表命名统一 ce_ 前缀、复数 snake_case；主键 String(36) UUID；软删除 is_deleted；
created_at/updated_at 跟随仓库约定（datetime.now，非 utcnow）。

注意：
- `ce_score_records.item_id` / `ce_goal_mappings.item_id` 用空串表示大题级
  （NOT NULL DEFAULT ''，唯一索引对 NULL 失效，不能用 NULL 承载"空=大题级"）。
- `ce_eval_tasks.active_flag` 是 MySQL 生成列（TINYINT GENERATED ALWAYS AS ... STORED），
  **不在 ORM 映射**（SQLite 内存库建不了生成列）；"是否有活跃任务"一律用
  status IN ('pending','running') 推导，唯一约束 uk_ce_task_active 只在建表 SQL 中定义。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from backend.db.models.base import Base


class CeCourse(Base):
    """课程档案。"""

    __tablename__ = "ce_courses"

    course_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    college = Column(String(128), nullable=True)
    department = Column(String(128), nullable=True)
    credit = Column(Numeric(4, 1), nullable=True)
    hours = Column(Integer, nullable=True)
    course_nature = Column(String(64), nullable=True)
    exam_mode = Column(String(32), nullable=True)
    majors_json = Column(JSON, nullable=True)
    owner_user_id = Column(
        String(36), ForeignKey("users.user_id"), nullable=False, index=True
    )
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        UniqueConstraint("code", name="uk_ce_course_code"),
        Index("idx_ce_course_owner", "owner_user_id"),
        {"comment": "课程档案"},
    )


class CeSemesterTask(Base):
    """学期评价任务。"""

    __tablename__ = "ce_semester_tasks"

    semester_task_id = Column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    course_id = Column(
        String(36), ForeignKey("ce_courses.course_id"), nullable=False, index=True
    )
    semester = Column(String(32), nullable=False)  # 2024-2025-1
    class_no = Column(String(128), nullable=False)
    grade = Column(String(32), nullable=True)  # 2023级
    major = Column(String(128), nullable=True)  # 决定试卷专业分支
    exam_date = Column(Date, nullable=True)
    exam_duration_minutes = Column(Integer, nullable=True)
    expected_students = Column(Integer, nullable=True)  # 应考人数
    absent_students = Column(Integer, nullable=True)  # 未考
    violation_students = Column(Integer, nullable=True)  # 违纪
    teacher_name = Column(String(64), nullable=True)
    analyzer_name = Column(String(64), nullable=True)  # 试卷分析人
    status = Column(String(32), nullable=False, default="draft", index=True)
    config_json = Column(JSON, nullable=True)
    owner_user_id = Column(
        String(36), ForeignKey("users.user_id"), nullable=False, index=True
    )
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        UniqueConstraint("course_id", "semester", "class_no", name="uk_ce_task"),
        Index("idx_ce_task_status", "status"),
        {"comment": "课程评价学期任务"},
    )


class CeCourseGoal(Base):
    """课程目标。"""

    __tablename__ = "ce_course_goals"

    goal_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(
        String(36), ForeignKey("ce_courses.course_id"), nullable=False, index=True
    )
    goal_code = Column(String(32), nullable=False)  # 目标1 / 目标2
    description = Column(Text, nullable=True)
    graduation_req_no = Column(String(32), nullable=True)
    graduation_req_point = Column(Text, nullable=True)
    weight_homework = Column(Numeric(5, 2), nullable=True)
    weight_class = Column(Numeric(5, 2), nullable=True)
    weight_exam = Column(Numeric(5, 2), nullable=True)
    source = Column(
        String(16), nullable=False, default="syllabus_draft"
    )  # syllabus_draft|syllabus|manual
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        UniqueConstraint("course_id", "goal_code", name="uk_ce_goal_course_code"),
        {"comment": "课程目标"},
    )


class CeExamPaper(Base):
    """试卷。"""

    __tablename__ = "ce_exam_papers"

    paper_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    paper_code = Column(String(16), nullable=False, default="A")  # A/B
    total_score_by_major = Column(
        JSON, nullable=True
    )  # {"工程力学": 110, "土木工程": 100}
    structure_basic_pct = Column(Numeric(5, 2), nullable=True)
    structure_comprehensive_pct = Column(Numeric(5, 2), nullable=True)
    structure_advanced_pct = Column(Numeric(5, 2), nullable=True)
    coverage_level = Column(String(16), nullable=True)  # wide|fairly_wide|partial
    item_count_total = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_paper_task", "semester_task_id"),
        {"comment": "试卷"},
    )


class CeExamSection(Base):
    """大题。"""

    __tablename__ = "ce_exam_sections"

    section_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    paper_id = Column(
        String(36), ForeignKey("ce_exam_papers.paper_id"), nullable=False, index=True
    )
    order_no = Column(Integer, nullable=False)  # 试卷原始序号 1..N
    report_order_no = Column(
        Integer, nullable=True
    )  # 报告视图序号（按专业过滤后重编号）
    question_type = Column(
        String(32), nullable=False
    )  # 填空题/证明题/作图题/计算题/汉译英
    item_count = Column(Integer, nullable=True)
    score_pattern = Column(String(64), nullable=True)  # "2" 或 "10*2+15*2"
    total_score = Column(Numeric(6, 2), nullable=True)
    applicable_majors = Column(JSON, nullable=True)  # null=全部；["工程力学"]=专业专属
    difficulty_level = Column(String(16), nullable=True)  # basic|comprehensive|advanced
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_section_paper", "paper_id", "order_no"),
        {"comment": "试卷大题"},
    )


class CeExamItem(Base):
    """小题（§1.5 结论 D 要求小题级映射与得分，非可选）。"""

    __tablename__ = "ce_exam_items"

    item_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    section_id = Column(
        String(36),
        ForeignKey("ce_exam_sections.section_id"),
        nullable=False,
        index=True,
    )
    order_no = Column(Integer, nullable=False)
    full_score = Column(Numeric(6, 2), nullable=False)
    knowledge_points = Column(JSON, nullable=True)
    stem_digest = Column(String(512), nullable=True)  # 题面摘要，供映射建议语义匹配
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_item_section", "section_id", "order_no"),
        {"comment": "试卷小题"},
    )


class CeStudent(Base):
    """学生。"""

    __tablename__ = "ce_students"

    student_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    student_no = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    regular_score = Column(Numeric(6, 2), nullable=True)
    final_score = Column(Numeric(6, 2), nullable=True)
    total_score = Column(Numeric(6, 2), nullable=True)
    flag = Column(String(16), nullable=False, default="")  # ''/缺考/违纪
    in_grade_sheet = Column(Boolean, nullable=False, default=False)
    in_paper_sample = Column(Boolean, nullable=False, default=False)
    source = Column(
        String(16), nullable=False, default="grade_sheet"
    )  # grade_sheet|ocr|import
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        UniqueConstraint("semester_task_id", "student_no", name="uk_ce_student"),
        {"comment": "学生"},
    )


class CeScoreRecord(Base):
    """得分记录（大题级 item_id 为空串；raw_score 永不覆盖）。"""

    __tablename__ = "ce_score_records"

    record_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    student_id = Column(
        String(36), ForeignKey("ce_students.student_id"), nullable=False, index=True
    )
    section_id = Column(
        String(36),
        ForeignKey("ce_exam_sections.section_id"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        String(36), nullable=False, default="", server_default=""
    )  # 空串=大题级
    full_score = Column(Numeric(6, 2), nullable=False)
    score = Column(Numeric(6, 2), nullable=False)
    raw_score = Column(Numeric(6, 2), nullable=True)  # 原始识别值，永不覆盖
    source = Column(
        String(16), nullable=False, default="ocr_table"
    )  # ocr_table|ocr_box|vlm|manual|import
    channel_agree = Column(Boolean, nullable=True)
    confidence = Column(Numeric(4, 3), nullable=True)
    status = Column(
        String(16), nullable=False, default="pending_review"
    )  # confirmed|pending_review|rejected
    page_ref = Column(String(128), nullable=True)  # p03L#x=120,y=88,w=60,h=40
    extract_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        UniqueConstraint(
            "semester_task_id",
            "student_id",
            "section_id",
            "item_id",
            "extract_version",
            name="uk_ce_score",
        ),
        Index("idx_ce_score_status", "semester_task_id", "status"),
        {"comment": "得分记录"},
    )


class CeGoalMapping(Base):
    """试题-目标映射。"""

    __tablename__ = "ce_goal_mappings"

    mapping_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    goal_id = Column(
        String(36), ForeignKey("ce_course_goals.goal_id"), nullable=False, index=True
    )
    section_id = Column(
        String(36),
        ForeignKey("ce_exam_sections.section_id"),
        nullable=False,
        index=True,
    )
    item_id = Column(
        String(36), nullable=False, default="", server_default=""
    )  # 空串=大题级
    weight = Column(Numeric(4, 3), nullable=False, default=1.000)
    auto_suggested = Column(Boolean, nullable=False, default=False)
    suggest_reason = Column(String(512), nullable=True)
    confirmed_by = Column(String(36), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_mapping_task_goal", "semester_task_id", "goal_id"),
        {"comment": "试题-目标映射"},
    )


class CeAchievementConfig(Base):
    """计算口径配置。"""

    __tablename__ = "ce_achievement_configs"

    config_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(
        String(36), ForeignKey("ce_courses.course_id"), nullable=False, index=True
    )
    method = Column(
        String(16), nullable=False, default="course_average"
    )  # quantitative|course_average
    pass_line = Column(Numeric(4, 2), nullable=False, default=0.60)
    regular_weight = Column(Numeric(4, 2), nullable=False, default=0.40)
    exam_weight = Column(Numeric(4, 2), nullable=False, default=0.60)
    homework_weight_in_regular = Column(Numeric(4, 2), nullable=False, default=0.80)
    class_weight_in_regular = Column(Numeric(4, 2), nullable=False, default=0.20)
    weak_threshold = Column(Numeric(4, 2), nullable=False, default=0.60)
    regular_achievement_source = Column(
        String(24), nullable=False, default="grade_sheet_avg"
    )  # grade_sheet_avg|imported_register
    score_bands_json = Column(JSON, nullable=True)
    paper_full_score = Column(Numeric(6, 2), nullable=False, default=100.00)
    standard_total_score = Column(Numeric(6, 2), nullable=False, default=100.00)
    ps06_tolerance = Column(Numeric(4, 2), nullable=False, default=0.50)
    version = Column(Integer, nullable=False, default=1)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_config_course_active", "course_id", "active"),
        {"comment": "达成度计算口径配置"},
    )


class CeReportTemplate(Base):
    """报告模板版本。"""

    __tablename__ = "ce_report_templates"

    template_id = Column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    template_type = Column(
        String(16), nullable=False
    )  # paper_analysis|appendix1|appendix2|appendix3
    college = Column(String(128), nullable=True)
    applicable_from_semester = Column(String(32), nullable=True)
    version = Column(String(32), nullable=False, default="1")
    file_key = Column(String(500), nullable=True)  # MinIO key
    anchor_map_json = Column(JSON, nullable=True)
    checkbox_options_json = Column(JSON, nullable=True)
    number_format_json = Column(JSON, nullable=True)  # 平均成绩等小数位配置
    active = Column(Boolean, nullable=False, default=True)
    remark = Column(String(500), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_template_type_active", "template_type", "active"),
        {"comment": "报告模板版本"},
    )


class CeReport(Base):
    """报告。"""

    __tablename__ = "ce_reports"

    report_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    template_id = Column(
        String(36),
        ForeignKey("ce_report_templates.template_id"),
        nullable=False,
        index=True,
    )
    report_type = Column(
        String(16), nullable=False
    )  # paper_analysis|appendix1|appendix2|appendix3
    status = Column(String(16), nullable=False, default="draft")  # draft|final
    current_version_no = Column(Integer, nullable=False, default=0)
    stats_snapshot_id = Column(String(36), nullable=True)
    stale = Column(Boolean, nullable=False, default=False)  # 回退后标记 stale 而非删除
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_report_task", "semester_task_id", "report_type"),
        {"comment": "课程评价报告"},
    )


class CeReportVersion(Base):
    """报告版本。"""

    __tablename__ = "ce_report_versions"

    version_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id = Column(
        String(36), ForeignKey("ce_reports.report_id"), nullable=False, index=True
    )
    version_no = Column(Integer, nullable=False)
    content_json = Column(
        Text, nullable=True
    )  # LONGTEXT（SQL 建表）；ORM 用 Text 兼容 SQLite
    docx_file_key = Column(String(500), nullable=True)
    pdf_file_key = Column(String(500), nullable=True)
    created_by = Column(String(36), nullable=True)
    note = Column(Text, nullable=True)
    qc_result_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("report_id", "version_no", name="uk_ce_report_version_no"),
        {"comment": "报告版本"},
    )


class CeStatsSnapshot(Base):
    """计算快照（报告必须引用不可变版本）。"""

    __tablename__ = "ce_stats_snapshots"

    snapshot_id = Column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    config_version = Column(Integer, nullable=False, default=1)
    data_version = Column(Integer, nullable=False, default=1)
    computed_at = Column(DateTime, nullable=False, default=datetime.now)
    result_json = Column(Text, nullable=True)  # LONGTEXT（SQL 建表）
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        Index("idx_ce_snapshot_task", "semester_task_id", "computed_at"),
        {"comment": "统计计算快照"},
    )


class CeEvalTask(Base):
    """异步任务（Worker 轮询）。active_flag 生成列只在 SQL 中定义，不映射。"""

    __tablename__ = "ce_eval_tasks"

    task_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    task_type = Column(
        String(32), nullable=False
    )  # extract_paper_scores|parse_grade_sheet|...
    status = Column(
        String(16), nullable=False, default="pending", index=True
    )  # pending|running|succeeded|failed|cancelled
    progress = Column(Integer, nullable=False, default=0)
    stage = Column(String(255), nullable=True)
    checkpoint_json = Column(JSON, nullable=True)  # 已完成页索引，支撑断点重试
    result_ref = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    worker_id = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index("idx_ce_task_poll", "status", "created_at"),
        {"comment": "课程评价异步任务"},
    )


class CeAuditLog(Base):
    """审计日志。"""

    __tablename__ = "ce_audit_logs"

    log_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    semester_task_id = Column(String(36), nullable=True, index=True)
    operator_id = Column(String(36), nullable=True)
    action = Column(String(64), nullable=False)
    target_type = Column(String(32), nullable=True)
    target_id = Column(String(64), nullable=True)
    detail_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)

    __table_args__ = (
        Index("idx_ce_audit_task", "semester_task_id", "created_at"),
        {"comment": "课程评价审计日志"},
    )


class CeMaterial(Base):
    """资料（关联 user_files.file_id；同类型重传自动升版本）。"""

    __tablename__ = "ce_materials"

    material_id = Column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    semester_task_id = Column(
        String(36),
        ForeignKey("ce_semester_tasks.semester_task_id"),
        nullable=False,
        index=True,
    )
    material_type = Column(String(32), nullable=False, index=True)
    file_id = Column(String(64), nullable=False, index=True)  # 关联 user_files.file_id
    original_filename = Column(String(255), nullable=True)
    mime = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    version_no = Column(Integer, nullable=False, default=1)
    is_current = Column(Boolean, nullable=False, default=True)
    uploaded_by = Column(String(36), nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    __table_args__ = (
        Index(
            "idx_ce_material_task_type",
            "semester_task_id",
            "material_type",
            "is_current",
        ),
        {"comment": "课程评价资料"},
    )
