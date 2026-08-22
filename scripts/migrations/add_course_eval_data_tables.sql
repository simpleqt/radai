-- 课程试卷分析系统 · 数据与映射表（M1 T1-02）
-- ce_score_records / ce_goal_mappings / ce_achievement_configs / ce_materials / ce_improvement_items / ce_audit_logs
-- 注意：item_id 用空串表示大题级（NOT NULL DEFAULT ''），唯一索引才有效，不允许 NULL。

CREATE TABLE IF NOT EXISTS ce_score_records (
  record_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  student_id VARCHAR(36) NOT NULL,
  section_id VARCHAR(36) NOT NULL,
  item_id VARCHAR(36) NOT NULL DEFAULT '',
  full_score DECIMAL(6,2) NOT NULL,
  score DECIMAL(6,2) NOT NULL,
  raw_score DECIMAL(6,2) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'ocr_table',
  channel_agree TINYINT(1) NULL,
  confidence DECIMAL(4,3) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending_review',
  page_ref VARCHAR(128) NULL,
  extract_version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (record_id),
  UNIQUE KEY uk_ce_score (semester_task_id, student_id, section_id, item_id, extract_version),
  KEY idx_ce_score_status (semester_task_id, status),
  KEY idx_ce_score_student (student_id),
  KEY idx_ce_score_section (section_id),
  CONSTRAINT fk_ce_score_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id),
  CONSTRAINT fk_ce_score_student FOREIGN KEY (student_id) REFERENCES ce_students(student_id),
  CONSTRAINT fk_ce_score_section FOREIGN KEY (section_id) REFERENCES ce_exam_sections(section_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='得分记录';

CREATE TABLE IF NOT EXISTS ce_goal_mappings (
  mapping_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  goal_id VARCHAR(36) NOT NULL,
  section_id VARCHAR(36) NOT NULL,
  item_id VARCHAR(36) NOT NULL DEFAULT '',
  weight DECIMAL(4,3) NOT NULL DEFAULT 1.000,
  auto_suggested TINYINT(1) NOT NULL DEFAULT 0,
  suggest_reason VARCHAR(512) NULL,
  confirmed_by VARCHAR(36) NULL,
  confirmed_at DATETIME NULL,
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (mapping_id),
  KEY idx_ce_mapping_task_goal (semester_task_id, goal_id),
  KEY idx_ce_mapping_section (section_id),
  KEY idx_ce_mapping_goal (goal_id),
  CONSTRAINT fk_ce_mapping_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id),
  CONSTRAINT fk_ce_mapping_goal FOREIGN KEY (goal_id) REFERENCES ce_course_goals(goal_id),
  CONSTRAINT fk_ce_mapping_section FOREIGN KEY (section_id) REFERENCES ce_exam_sections(section_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='试题-目标映射';

CREATE TABLE IF NOT EXISTS ce_achievement_configs (
  config_id VARCHAR(36) NOT NULL,
  course_id VARCHAR(36) NOT NULL,
  method VARCHAR(16) NOT NULL DEFAULT 'course_average',
  pass_line DECIMAL(4,2) NOT NULL DEFAULT 0.60,
  regular_weight DECIMAL(4,2) NOT NULL DEFAULT 0.40,
  exam_weight DECIMAL(4,2) NOT NULL DEFAULT 0.60,
  homework_weight_in_regular DECIMAL(4,2) NOT NULL DEFAULT 0.80,
  class_weight_in_regular DECIMAL(4,2) NOT NULL DEFAULT 0.20,
  weak_threshold DECIMAL(4,2) NOT NULL DEFAULT 0.60,
  regular_achievement_source VARCHAR(24) NOT NULL DEFAULT 'grade_sheet_avg',
  score_bands_json JSON NULL,
  paper_full_score DECIMAL(6,2) NOT NULL DEFAULT 100.00,
  standard_total_score DECIMAL(6,2) NOT NULL DEFAULT 100.00,
  ps06_tolerance DECIMAL(4,2) NOT NULL DEFAULT 0.50,
  version INT NOT NULL DEFAULT 1,
  active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (config_id),
  KEY idx_ce_config_course_active (course_id, active),
  CONSTRAINT fk_ce_config_course FOREIGN KEY (course_id) REFERENCES ce_courses(course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='达成度计算口径配置';

CREATE TABLE IF NOT EXISTS ce_materials (
  material_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  material_type VARCHAR(32) NOT NULL,
  file_id VARCHAR(64) NOT NULL,
  original_filename VARCHAR(255) NULL,
  mime VARCHAR(128) NULL,
  size_bytes BIGINT NULL,
  version_no INT NOT NULL DEFAULT 1,
  is_current TINYINT(1) NOT NULL DEFAULT 1,
  uploaded_by VARCHAR(36) NULL,
  deleted_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (material_id),
  KEY idx_ce_material_task_type (semester_task_id, material_type, is_current),
  KEY idx_ce_material_file (file_id),
  CONSTRAINT fk_ce_material_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程评价资料';

CREATE TABLE IF NOT EXISTS ce_improvement_items (
  improvement_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  content TEXT NOT NULL,
  target_goal_id VARCHAR(36) NULL,
  proposed_semester VARCHAR(32) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'planned',
  effect_note TEXT NULL,
  verified_semester VARCHAR(32) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (improvement_id),
  KEY idx_ce_improvement_task (semester_task_id, status),
  CONSTRAINT fk_ce_improvement_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='持续改进台账';

CREATE TABLE IF NOT EXISTS ce_audit_logs (
  log_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NULL,
  operator_id VARCHAR(36) NULL,
  action VARCHAR(64) NOT NULL,
  target_type VARCHAR(32) NULL,
  target_id VARCHAR(64) NULL,
  detail_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (log_id),
  KEY idx_ce_audit_task (semester_task_id, created_at),
  KEY idx_ce_audit_operator (operator_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程评价审计日志';
