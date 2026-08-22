-- 为 llm_models 增加最大输入 token（上下文窗口）字段，默认 128K
-- 执行: mysql -u ... -p ... < scripts/migrations/add_llm_models_max_input_tokens.sql

ALTER TABLE `llm_models`
  ADD COLUMN `max_input_tokens` INT UNSIGNED NOT NULL DEFAULT 131072
    COMMENT '最大输入 token（上下文窗口，默认 128K）'
    AFTER `max_tokens`;

UPDATE `llm_models` SET `max_input_tokens` = 131072;
