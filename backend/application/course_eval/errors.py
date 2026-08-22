"""Course-eval domain errors.

All errors inherit ApplicationError so the global handler in http_server.py
renders them as `{"detail": public_detail, "code": error_code}`.
"""

from __future__ import annotations

from backend.application.exceptions import ApplicationError


class CourseEvalError(ApplicationError):
    """Base error for the course-eval module."""

    status_code = 400
    public_detail = "课程评价请求无效"
    error_code = "course_eval_error"


class CourseEvalStateError(CourseEvalError):
    """非法状态迁移（前端据此灰化按钮，body 带 allowed_actions）。"""

    status_code = 409
    public_detail = "当前状态不允许该操作"
    error_code = "course_eval_state_error"

    def __init__(self, current: str, target: str, allowed: frozenset[str]) -> None:
        super().__init__(
            f"state transition {current} -> {target} is not allowed; "
            f"allowed: {sorted(allowed)}"
        )
        self.current = current
        self.target = target
        self.allowed = allowed


class TaskNotFound(CourseEvalError):
    status_code = 404
    public_detail = "学期任务不存在"
    error_code = "course_eval_task_not_found"


class TaskArchived(CourseEvalError):
    """归档状态下拒绝改数据（409：状态冲突而非权限问题）。"""

    status_code = 409
    public_detail = "任务已归档，不能修改数据；如需修改请先回退状态"
    error_code = "course_eval_task_archived"


class CourseNotFound(CourseEvalError):
    status_code = 404
    public_detail = "课程不存在"
    error_code = "course_eval_course_not_found"


class CourseEvalAccessDenied(CourseEvalError):
    """跨教师访问他人课程评价任务（非属主且非管理员）。"""

    status_code = 403
    public_detail = "无权访问该课程评价任务"
    error_code = "course_eval_access_denied"


