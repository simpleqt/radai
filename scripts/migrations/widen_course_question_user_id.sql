-- 访客 user_id 形如 guest_<32位hex> 共 38 字符，超过 course_question_records.user_id
-- 原有的 VARCHAR(36)，访客提问采集因此被 MySQL 1406 拒绝。加宽到 VARCHAR(64)。
-- MODIFY 为幂等定义（重复执行结果一致）。

ALTER TABLE course_question_records
    MODIFY COLUMN user_id VARCHAR(64) NOT NULL;
