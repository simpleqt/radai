-- 回答反馈表：用户对单条智能体回答的赞/踩 + 原因 + 评论。
-- (user_id, thread_id, message_key) 唯一，重复反馈按 upsert 覆盖。

CREATE TABLE IF NOT EXISTS message_feedback (
    id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(100) NOT NULL,
    agent_code VARCHAR(32) NOT NULL DEFAULT 'ne',
    message_key VARCHAR(128) NOT NULL,
    rating VARCHAR(8) NOT NULL,
    reasons JSON NULL,
    comment TEXT NULL,
    model_name VARCHAR(128) NULL,
    question_excerpt VARCHAR(500) NULL,
    answer_excerpt VARCHAR(1000) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_feedback_user_message (user_id, thread_id, message_key),
    KEY ix_message_feedback_user_id (user_id),
    KEY ix_message_feedback_created_at (created_at),
    KEY idx_feedback_agent_rating (agent_code, rating, created_at),
    CONSTRAINT fk_message_feedback_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
