-- 基础力学课程智能体 · 教师提问分析表（两张）。
-- course_question_records: 学生提问旁路快照（仅 agent_code='fmca'）。
-- course_question_reports: 教师触发的汇总分析报告快照。

CREATE TABLE IF NOT EXISTS course_question_records (
    id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    username VARCHAR(50) NOT NULL DEFAULT '',
    agent_code VARCHAR(32) NOT NULL,
    thread_id VARCHAR(100) NOT NULL,
    question TEXT NOT NULL,
    question_len INT NOT NULL,
    workflow VARCHAR(32) NULL,
    model_name VARCHAR(128) NULL,
    is_guest TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_qr_agent_time (agent_code, created_at),
    KEY idx_qr_user_time (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='基础力学智能体学生提问快照';

CREATE TABLE IF NOT EXISTS course_question_reports (
    id VARCHAR(36) NOT NULL,
    agent_code VARCHAR(32) NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    range_start DATETIME NOT NULL,
    range_end DATETIME NOT NULL,
    question_count INT NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    result_json JSON NULL,
    model_name VARCHAR(128) NULL,
    error VARCHAR(500) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_qrpt_agent (agent_code, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='基础力学智能体提问分析报告';
