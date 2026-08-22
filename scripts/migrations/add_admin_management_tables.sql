CREATE TABLE IF NOT EXISTS admin_audit_logs (
  audit_id BIGINT NOT NULL AUTO_INCREMENT,
  actor_user_id VARCHAR(36) NULL,
  action VARCHAR(64) NOT NULL,
  target_type VARCHAR(64) NOT NULL,
  target_id VARCHAR(128) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'success',
  method VARCHAR(10) NULL,
  path VARCHAR(512) NULL,
  ip_address VARCHAR(64) NULL,
  user_agent VARCHAR(512) NULL,
  detail_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (audit_id),
  KEY idx_admin_audit_actor (actor_user_id),
  KEY idx_admin_audit_action (action),
  KEY idx_admin_audit_target_type (target_type),
  KEY idx_admin_audit_target_id (target_id),
  KEY idx_admin_audit_status (status),
  KEY idx_admin_audit_created (created_at),
  KEY idx_admin_audit_actor_created (actor_user_id, created_at),
  KEY idx_admin_audit_target (target_type, target_id),
  CONSTRAINT fk_admin_audit_actor
    FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
