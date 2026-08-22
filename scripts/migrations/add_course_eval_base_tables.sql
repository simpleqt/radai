-- 课程试卷分析系统 · 基础档案表（M1 T1-02）
-- ce_courses / ce_semester_tasks / ce_course_goals / ce_exam_papers / ce_exam_sections / ce_exam_items / ce_students

CREATE TABLE IF NOT EXISTS ce_courses (
  course_id VARCHAR(36) NOT NULL,
  code VARCHAR(64) NOT NULL,
  name VARCHAR(255) NOT NULL,
  college VARCHAR(128) NULL,
  department VARCHAR(128) NULL,
  credit DECIMAL(4,1) NULL,
  hours INT NULL,
  course_nature VARCHAR(64) NULL,
  exam_mode VARCHAR(32) NULL,
  majors_json JSON NULL,
  owner_user_id VARCHAR(36) NOT NULL,
  is_deleted TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (course_id),
  UNIQUE KEY uk_ce_course_code (code),
  KEY idx_ce_course_owner (owner_user_id),
  CONSTRAINT fk_ce_course_owner FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程档案';

CREATE TABLE IF NOT EXISTS ce_semester_tasks (
  semester_task_id VARCHAR(36) NOT NULL,
  course_id VARCHAR(36) NOT NULL,
  semester VARCHAR(32) NOT NULL,
  class_no VARCHAR(128) NOT NULL,
  grade VARCHAR(32) NULL,
  major VARCHAR(128) NULL,
  exam_date DATE NULL,
  exam_duration_minutes INT NULL,
  expected_students INT NULL,
  absent_students INT NULL,
  violation_students INT NULL,
  teacher_name VARCHAR(64) NULL,
  analyzer_name VARCHAR(64) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'draft',
  config_json JSON NULL,
  owner_user_id VARCHAR(36) NOT NULL,
  is_deleted TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (semester_task_id),
  UNIQUE KEY uk_ce_task (course_id, semester, class_no),
  KEY idx_ce_task_status (status),
  KEY idx_ce_task_course (course_id),
  KEY idx_ce_task_owner (owner_user_id),
  CONSTRAINT fk_ce_task_course FOREIGN KEY (course_id) REFERENCES ce_courses(course_id),
  CONSTRAINT fk_ce_task_owner FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程评价学期任务';

CREATE TABLE IF NOT EXISTS ce_course_goals (
  goal_id VARCHAR(36) NOT NULL,
  course_id VARCHAR(36) NOT NULL,
  goal_code VARCHAR(32) NOT NULL,
  description TEXT NULL,
  graduation_req_no VARCHAR(32) NULL,
  graduation_req_point TEXT NULL,
  weight_homework DECIMAL(5,2) NULL,
  weight_class DECIMAL(5,2) NULL,
  weight_exam DECIMAL(5,2) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'syllabus_draft',
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (goal_id),
  UNIQUE KEY uk_ce_goal_course_code (course_id, goal_code),
  KEY idx_ce_goal_course (course_id),
  CONSTRAINT fk_ce_goal_course FOREIGN KEY (course_id) REFERENCES ce_courses(course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='课程目标';

CREATE TABLE IF NOT EXISTS ce_exam_papers (
  paper_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  paper_code VARCHAR(16) NOT NULL DEFAULT 'A',
  total_score_by_major JSON NULL,
  structure_basic_pct DECIMAL(5,2) NULL,
  structure_comprehensive_pct DECIMAL(5,2) NULL,
  structure_advanced_pct DECIMAL(5,2) NULL,
  coverage_level VARCHAR(16) NULL,
  item_count_total INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (paper_id),
  KEY idx_ce_paper_task (semester_task_id),
  CONSTRAINT fk_ce_paper_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='试卷';

CREATE TABLE IF NOT EXISTS ce_exam_sections (
  section_id VARCHAR(36) NOT NULL,
  paper_id VARCHAR(36) NOT NULL,
  order_no INT NOT NULL,
  report_order_no INT NULL,
  question_type VARCHAR(32) NOT NULL,
  item_count INT NULL,
  score_pattern VARCHAR(64) NULL,
  total_score DECIMAL(6,2) NULL,
  applicable_majors JSON NULL,
  difficulty_level VARCHAR(16) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (section_id),
  KEY idx_ce_section_paper (paper_id, order_no),
  CONSTRAINT fk_ce_section_paper FOREIGN KEY (paper_id) REFERENCES ce_exam_papers(paper_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='试卷大题';

CREATE TABLE IF NOT EXISTS ce_exam_items (
  item_id VARCHAR(36) NOT NULL,
  section_id VARCHAR(36) NOT NULL,
  order_no INT NOT NULL,
  full_score DECIMAL(6,2) NOT NULL,
  knowledge_points JSON NULL,
  stem_digest VARCHAR(512) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (item_id),
  KEY idx_ce_item_section (section_id, order_no),
  CONSTRAINT fk_ce_item_section FOREIGN KEY (section_id) REFERENCES ce_exam_sections(section_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='试卷小题';

CREATE TABLE IF NOT EXISTS ce_students (
  student_id VARCHAR(36) NOT NULL,
  semester_task_id VARCHAR(36) NOT NULL,
  student_no VARCHAR(32) NOT NULL,
  name VARCHAR(64) NOT NULL,
  regular_score DECIMAL(6,2) NULL,
  final_score DECIMAL(6,2) NULL,
  total_score DECIMAL(6,2) NULL,
  flag VARCHAR(16) NOT NULL DEFAULT '',
  in_grade_sheet TINYINT(1) NOT NULL DEFAULT 0,
  in_paper_sample TINYINT(1) NOT NULL DEFAULT 0,
  source VARCHAR(16) NOT NULL DEFAULT 'grade_sheet',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (student_id),
  UNIQUE KEY uk_ce_student (semester_task_id, student_no),
  KEY idx_ce_student_task (semester_task_id),
  CONSTRAINT fk_ce_student_task FOREIGN KEY (semester_task_id) REFERENCES ce_semester_tasks(semester_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生';
