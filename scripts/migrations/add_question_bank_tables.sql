-- 基础力学课程智能体 · 题库管理表（四张）。
-- question_bank_batches: 教师上传批次（解析结果暂存 parsed_json，确认后才写题目表）。
-- question_bank_questions: 题目一等业务实体（内容 + 标注 + 图片强绑定 images_json）。
-- question_bank_knowledge_points: 知识点受控字典（标注/筛选/分析聚合共用枚举）。
-- question_bank_attempts: 学生做题记录（练习判分 + 教学分析数据源）。

CREATE TABLE IF NOT EXISTS question_bank_batches (
    id VARCHAR(36) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_key VARCHAR(512) NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'parsed',
    total_count INT NOT NULL DEFAULT 0,
    valid_count INT NOT NULL DEFAULT 0,
    parsed_json JSON NULL,
    error VARCHAR(500) NULL,
    created_by VARCHAR(36) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_qbb_creator (created_by, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='题库上传批次';

CREATE TABLE IF NOT EXISTS question_bank_questions (
    id VARCHAR(36) NOT NULL,
    question_type VARCHAR(16) NOT NULL,
    stem TEXT NOT NULL,
    options_json JSON NULL,
    answer TEXT NULL,
    analysis TEXT NULL,
    score VARCHAR(32) NULL,
    difficulty VARCHAR(16) NULL,
    chapter VARCHAR(128) NULL,
    knowledge_points_json JSON NULL,
    images_json JSON NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'published',
    source_batch_id VARCHAR(36) NULL,
    source_name VARCHAR(255) NULL,
    stem_hash VARCHAR(64) NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_qbq_status_type (status, question_type),
    KEY idx_qbq_difficulty (difficulty),
    KEY idx_qbq_chapter (chapter),
    KEY idx_qbq_batch (source_batch_id),
    KEY idx_qbq_hash (stem_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='题库题目';

CREATE TABLE IF NOT EXISTS question_bank_knowledge_points (
    id VARCHAR(36) NOT NULL,
    name VARCHAR(128) NOT NULL,
    chapter VARCHAR(128) NULL,
    parent_id VARCHAR(36) NULL,
    sort INT NOT NULL DEFAULT 0,
    created_by VARCHAR(36) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_qbkp_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='题库知识点字典';

CREATE TABLE IF NOT EXISTS question_bank_attempts (
    id VARCHAR(36) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    username VARCHAR(50) NOT NULL DEFAULT '',
    question_id VARCHAR(36) NOT NULL,
    answer_json JSON NULL,
    is_correct TINYINT(1) NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'practice',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_qba_user_time (user_id, created_at),
    KEY idx_qba_question (question_id),
    KEY idx_qba_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生做题记录';
