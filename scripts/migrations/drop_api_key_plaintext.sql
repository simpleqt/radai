-- Retire the verbatim API-key column after the digest backfill is accepted.
--
-- Deliberately NOT registered in backend/db/migrate.py: dropping the column is
-- irreversible and removes the rollback path for releases that predate digest
-- lookup. Run it manually, and only after confirming on the target database:
--
--   SELECT COUNT(*) FROM api_keys WHERE api_key_hash IS NULL;   -- must be 0
--   SELECT COUNT(*) FROM api_keys WHERE api_key IS NOT NULL;    -- legacy rows
--
-- and after the new release has authenticated successfully for a full
-- acceptance window (all live keys resolve through `api_key_hash`).

ALTER TABLE `api_keys`
  DROP INDEX `api_key`,
  DROP COLUMN `api_key`;
