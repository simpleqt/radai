-- Store API keys as a lookup digest instead of the verbatim credential.
--
-- Additive and backfill-only: `api_key` stays populated for the rollback window
-- so an older release can still authenticate existing keys. Application code
-- switches to digest lookup as soon as `api_key_hash` exists and stops writing
-- new plaintext. Retiring the plaintext column is a separate, operator-gated
-- step (scripts/migrations/drop_api_key_plaintext.sql).

ALTER TABLE `api_keys`
  ADD COLUMN `api_key_hash` CHAR(64) NULL
    COMMENT '原始 Key 的 SHA-256，用于查找（不可逆）'
    AFTER `api_key`,
  ADD COLUMN `api_key_prefix` VARCHAR(32) NULL
    COMMENT '展示用前缀，非凭据'
    AFTER `api_key_hash`;

UPDATE `api_keys`
   SET `api_key_hash` = SHA2(`api_key`, 256),
       `api_key_prefix` = LEFT(`api_key`, 12)
 WHERE `api_key_hash` IS NULL
   AND `api_key` IS NOT NULL;

ALTER TABLE `api_keys`
  ADD UNIQUE KEY `uk_api_keys_api_key_hash` (`api_key_hash`);

-- New rows carry only the digest, so the legacy column must accept NULL.
ALTER TABLE `api_keys`
  MODIFY COLUMN `api_key` VARCHAR(100) NULL;
