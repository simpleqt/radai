-- 为普通用户设置页增加可编辑显示名字段
-- 执行: mysql -u ... -p ... < scripts/migrations/add_user_display_name.sql

ALTER TABLE `users`
  ADD COLUMN `display_name` VARCHAR(50) NULL
    COMMENT '用户显示名'
    AFTER `username`;
