-- Add API-key access scopes. Existing keys retain full access.
-- Execute: mysql -u ... -p ... < scripts/migrations/add_api_key_access_scope.sql

ALTER TABLE `api_keys`
  ADD COLUMN `access_scope` VARCHAR(20) NOT NULL DEFAULT 'full'
    COMMENT '访问范围: full/mcp_only'
    AFTER `key_name`;

UPDATE `api_keys`
SET `access_scope` = 'full'
WHERE `access_scope` IS NULL OR `access_scope` = '';
