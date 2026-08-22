CREATE TABLE IF NOT EXISTS learning_style_attempts (
  attempt_id VARCHAR(36) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  questionnaire_version VARCHAR(32) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'in_progress',
  answers JSON NOT NULL,
  scores JSON NULL,
  result JSON NULL,
  current_dimension INT NOT NULL DEFAULT 0,
  completed_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (attempt_id),
  KEY idx_learning_style_attempt_user_status (user_id, status),
  KEY idx_learning_style_attempt_created (user_id, created_at),
  CONSTRAINT fk_learning_style_attempt_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS learning_style_profiles (
  user_id VARCHAR(36) NOT NULL,
  latest_attempt_id VARCHAR(36) NOT NULL,
  scores JSON NOT NULL,
  result JSON NOT NULL,
  apply_to_ai TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  KEY idx_learning_style_profile_attempt (latest_attempt_id),
  CONSTRAINT fk_learning_style_profile_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
  CONSTRAINT fk_learning_style_profile_attempt FOREIGN KEY (latest_attempt_id) REFERENCES learning_style_attempts(attempt_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
