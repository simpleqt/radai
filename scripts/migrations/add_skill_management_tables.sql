CREATE TABLE IF NOT EXISTS skills (
  skill_id VARCHAR(36) NOT NULL,
  skill_key VARCHAR(64) NOT NULL,
  display_name VARCHAR(100) NOT NULL,
  description VARCHAR(1000) NOT NULL,
  display_description VARCHAR(1000) NOT NULL DEFAULT '',
  domain VARCHAR(64) NOT NULL,
  source_type VARCHAR(20) NOT NULL DEFAULT 'uploaded',
  visibility VARCHAR(20) NOT NULL DEFAULT 'private',
  owner_user_id VARCHAR(36) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'draft',
  active_version_id VARCHAR(36) NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (skill_id),
  UNIQUE KEY uk_skills_key (skill_key),
  KEY idx_skills_owner_status (owner_user_id, status),
  KEY idx_skills_domain (domain),
  KEY idx_skills_active_version (active_version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

UPDATE skills
SET
  display_name = CASE skill_key
    WHEN 'academic-search' THEN '学术文献检索'
    WHEN 'csh-hydrogeology' THEN '场地污染水文地质'
    WHEN 'deep-research' THEN '深度研究'
    WHEN 'excel-experiment-analysis' THEN '实验数据分析'
    WHEN 'general-table-analysis' THEN '通用表格分析'
    WHEN 'groundwater-project-analysis' THEN '地下水项目分析'
    WHEN 'html-ppt' THEN 'PPT 创建'
    WHEN 'nuclear-electronics' THEN '核电子学专业问答'
    ELSE display_name
  END,
  display_description = CASE skill_key
    WHEN 'academic-search' THEN '检索论文、DOI、arXiv、PubMed、OpenAlex、Crossref 等学术资源，适合综述和相关工作梳理。'
    WHEN 'csh-hydrogeology' THEN '面向地下水、土壤污染、污染场地调查评估、污染运移模拟和修复方案的专业问答技能。'
    WHEN 'deep-research' THEN '进入深度研究流程，进行多角度资料检索、分析和结构化研究报告生成。'
    WHEN 'excel-experiment-analysis' THEN '分析 Excel/CSV 实验数据，支持表结构探查、SQL 查询、统计分析和科研图表生成。'
    WHEN 'general-table-analysis' THEN '通用表格分析能力，支持 Excel/CSV 的查询、统计摘要、缺失值分析、分组聚合和可视化。'
    WHEN 'groundwater-project-analysis' THEN '读取地下水项目资料、钻井数据和水文地质背景，支持质量检查与新井预测分析。'
    WHEN 'html-ppt' THEN '根据内容生成专业静态 HTML 演示文稿，适合 PPT、幻灯片、汇报和分享材料。'
    WHEN 'nuclear-electronics' THEN '面向核辐射探测、粒子探测器、数据获取和电子学读出的专业知识检索与问答技能。'
    ELSE display_description
  END
WHERE skill_key IN (
  'academic-search',
  'csh-hydrogeology',
  'deep-research',
  'excel-experiment-analysis',
  'general-table-analysis',
  'groundwater-project-analysis',
  'html-ppt',
  'nuclear-electronics'
);

CREATE TABLE IF NOT EXISTS skill_versions (
  version_id VARCHAR(36) NOT NULL,
  skill_id VARCHAR(36) NOT NULL,
  version VARCHAR(32) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'validated',
  package_bucket VARCHAR(128) NOT NULL,
  package_object VARCHAR(512) NOT NULL,
  package_sha256 VARCHAR(64) NOT NULL,
  package_size BIGINT NOT NULL,
  manifest_json JSON NOT NULL,
  file_index_json JSON NOT NULL,
  review_comment TEXT NULL,
  created_by VARCHAR(36) NOT NULL,
  reviewed_by VARCHAR(36) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at DATETIME NULL,
  PRIMARY KEY (version_id),
  UNIQUE KEY uk_skill_version (skill_id, version),
  KEY idx_skill_versions_skill_status (skill_id, status),
  KEY idx_skill_versions_sha256 (package_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS skill_acl (
  id BIGINT NOT NULL AUTO_INCREMENT,
  skill_id VARCHAR(36) NOT NULL,
  subject_type VARCHAR(16) NOT NULL,
  subject_id VARCHAR(64) NOT NULL,
  permission VARCHAR(16) NOT NULL,
  effect VARCHAR(8) NOT NULL DEFAULT 'allow',
  expires_at DATETIME NULL,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_skill_acl (skill_id, subject_type, subject_id, permission),
  KEY idx_skill_acl_subject (subject_type, subject_id),
  KEY idx_skill_acl_skill (skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_skill_bindings (
  id BIGINT NOT NULL AUTO_INCREMENT,
  agent_code VARCHAR(64) NOT NULL,
  skill_id VARCHAR(36) NOT NULL,
  is_enabled TINYINT(1) NOT NULL DEFAULT 1,
  is_required TINYINT(1) NOT NULL DEFAULT 0,
  default_selected TINYINT(1) NOT NULL DEFAULT 0,
  is_selectable TINYINT(1) NOT NULL DEFAULT 1,
  sort_order BIGINT NOT NULL DEFAULT 0,
  created_by VARCHAR(36) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_agent_skill (agent_code, skill_id),
  KEY idx_agent_skill_enabled (agent_code, is_enabled),
  KEY idx_agent_skill_skill (skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS skill_audit_logs (
  audit_id BIGINT NOT NULL AUTO_INCREMENT,
  skill_id VARCHAR(36) NULL,
  version_id VARCHAR(36) NULL,
  actor_user_id VARCHAR(36) NOT NULL,
  action VARCHAR(32) NOT NULL,
  target_type VARCHAR(32) NULL,
  target_id VARCHAR(128) NULL,
  detail_json JSON NULL,
  request_id VARCHAR(64) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (audit_id),
  KEY idx_skill_audit_skill (skill_id),
  KEY idx_skill_audit_actor (actor_user_id),
  KEY idx_skill_audit_action (action),
  KEY idx_skill_audit_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
