-- 学校短信注册绑定手机号
-- 执行: mysql -u ... -p ... < scripts/migrations/add_user_telephone.sql

ALTER TABLE `users`
  ADD COLUMN `telephone` VARCHAR(20) NULL
    COMMENT '绑定手机号（学校短信注册）'
    AFTER `display_name`,
  ADD UNIQUE KEY `uq_users_telephone` (`telephone`);
