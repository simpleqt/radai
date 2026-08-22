-- 课程试卷分析系统 · 报告与任务表（M1 T1-02）
-- ce_report_templates / ce_reports / ce_report_versions / ce_stats_snapshots / ce_eval_tasks / ce_archives
-- 注意：ce_eval_tasks.active_flag 是 MySQL 生成列（ORM 不映射），
--       配合 uk_ce_task_active 保证同一任务最多只有一个 pending|running 活跃实例。

CREATE TABLE IF NOT EXISTS ce_report_templates (
  template_id VARCHAR(36) NOT NULL,
  template_type VARCHAR(16) NOT NULL,
  college VARCHAR(128) NULL,
  applicable_from_semester VARCHAR(32) NULL,
  version VARCHAR(32) NOT NULL DEFAULT '1',
  file_key VARCHAR(500) NULL,
  anchor_map_json JSON NULL,
  checkbox_options_json JSON NULL,
  number_format_json JSON NULL,
  active TINYINT(1) NOT NULL DEFAULT 1,
  remark VARCHAR(500) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (template_id),
  KEY idx_ce_template_type_active (template_type, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='报告模板版本';

CREATE TABLE IF NOT EXISTS ce_reports (
  report_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  template_id VARCHAR(36) NOT NULL,
  report_type VARCHAR(16) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'draft',
  current_version_no INT NOT NULL DEFAULT 0,
  stats_snapshot_id VARCHAR(36) NULL,
  stale TINYINT(1) NOT NULL DEFAULT 0,
  created_by VARCHAR(36) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (report_id),
  KEY idx_ce_report_task (semester_task_id, report_type),
  KEY idx_ce_report_template (template_id),
  CONSTRAINT fk_ce_report_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id),
  CONSTRAINT fk_ce_report_template FOREIGN KEY (template_id) REFERENCES ce_report_templates(template_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程评价报告';

CREATE TABLE IF NOT EXISTS ce_report_versions (
  version_id VARCHAR(36) NOT NULL,
  report_id VARCHAR(36) NOT NULL,
  version_no INT NOT NULL,
  content_json LONGTEXT NULL,
  docx_file_key VARCHAR(500) NULL,
  pdf_file_key VARCHAR(500) NULL,
  created_by VARCHAR(36) NULL,
  note TEXT NULL,
  qc_result_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (version_id),
  UNIQUE KEY uk_ce_report_version_no (report_id, version_no),
  CONSTRAINT fk_ce_report_version_report FOREIGN KEY (report_id) REFERENCES ce_reports(report_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='报告版本';

CREATE TABLE IF NOT EXISTS ce_stats_snapshots (
  snapshot_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  config_version INT NOT NULL DEFAULT 1,
  data_version INT NOT NULL DEFAULT 1,
  computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  result_json LONGTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (snapshot_id),
  KEY idx_ce_snapshot_task (semester_task_id, computed_at),
  CONSTRAINT fk_ce_snapshot_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='统计计算快照';

CREATE TABLE IF NOT EXISTS ce_eval_tasks (
  task_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  task_type VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  active_flag TINYINT GENERATED ALWAYS AS (IF(status IN ('pending','running'), 1, NULL)) STORED,
  progress INT NOT NULL DEFAULT 0,
  stage VARCHAR(255) NULL,
  checkpoint_json JSON NULL,
  result_ref VARCHAR(255) NULL,
  error_message TEXT NULL,
  worker_id VARCHAR(64) NULL,
  lease_expires_at DATETIME NULL,
  created_by VARCHAR(36) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id),
  UNIQUE KEY uk_ce_task_active (semester_task_id, task_type, active_flag),
  KEY idx_ce_task_poll (status, created_at),
  KEY idx_ce_task_task (semester_task_id),
  CONSTRAINT fk_ce_task_eval_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程评价异步任务';

CREATE TABLE IF NOT EXISTS ce_archives (
  archive_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  manifest_json LONGTEXT NULL,
  object_keys_json JSON NULL,
  archived_by VARCHAR(36) NULL,
  archived_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (archive_id),
  KEY idx_ce_archive_task (semester_task_id),
  CONSTRAINT fk_ce_archive_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='归档清单';
