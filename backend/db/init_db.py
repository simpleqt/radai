"""数据库初始化脚本"""

import argparse
import re
import uuid

from backend.config.settings import settings
from backend.db.connection import close_db, get_engine
from backend.db.models import Base


def create_database():
    """创建数据库（如果不存在）"""
    import pymysql

    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        charset="utf8mb4",
    )

    cursor = conn.cursor()
    database_name = settings.mysql_database
    if not re.fullmatch(r"[A-Za-z0-9_]+", database_name or ""):
        raise ValueError("MYSQL_DATABASE 只能包含字母、数字和下划线")
    quoted_database = f"`{database_name}`"

    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS {quoted_database} "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cursor.execute(f"USE {quoted_database}")

    print(f"数据库 '{database_name}' 创建/确认成功")

    cursor.close()
    conn.close()


def create_tables():
    """创建所有表

    注意：`agent_kb_bindings` 因与 `knowledge_bases` 表排序规则（collation）不一致，
    ORM 层无法自动建立 DB 级外键约束；若该表创建失败，请手动执行
    docs/docs_backend/agent_kb_binding_module.md 中提供的 SQL DDL。
    """

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    print("所有表创建成功！")

    tables = [
        "users",
        "api_keys",
        "documents",
        "tasks",
        "extraction_results",
        "task_logs",
        "admin_audit_logs",
        "agents",
        "agent_kb_bindings",
        "skills",
        "skill_versions",
        "skill_acl",
        "agent_skill_bindings",
        "skill_audit_logs",
        "learning_style_attempts",
        "learning_style_profiles",
        "learning_plans",
        "learning_plan_nodes",
        "learning_node_attempts",
        "learning_plan_adjustments",
    ]

    for table in tables:
        print(f"  ✓ {table}")


def create_default_user():
    """创建默认管理员（如果不存在）。"""
    import os

    from backend.db.connection import get_session
    from werkzeug.security import generate_password_hash

    session = get_session()

    try:
        from backend.db.models import ApiKey, User

        existing_user = session.query(User).filter(User.username == "admin").first()
        initial_password = os.getenv("ADMIN_INITIAL_PASSWORD")
        initial_api_key = os.getenv("ADMIN_INITIAL_API_KEY")

        if not existing_user:
            if not initial_password:
                raise RuntimeError("创建默认管理员必须显式设置 ADMIN_INITIAL_PASSWORD")
            if not initial_api_key:
                raise RuntimeError("创建默认管理员必须显式设置 ADMIN_INITIAL_API_KEY")
            user = User(
                user_id=str(uuid.uuid4()),
                username="admin",
                password=generate_password_hash(initial_password),
                email="admin@example.com",
                api_key=initial_api_key,
                role="admin",
                is_active=True,
            )
            session.add(user)
            session.flush()

            api_key = ApiKey(
                api_key=initial_api_key,
                user_id=user.user_id,
                key_name="初始化管理员 Key",
                is_active=True,
                rate_limit=1000,
            )
            session.add(api_key)

            session.commit()
            print("创建默认用户: admin")
            print("默认密码和 API Key 来自安全环境变量")
        else:
            repaired = []
            if existing_user.role != "admin":
                existing_user.role = "admin"
                repaired.append("管理员角色")
            if not existing_user.password:
                if not initial_password:
                    raise RuntimeError("修复管理员密码必须显式设置 ADMIN_INITIAL_PASSWORD")
                existing_user.password = generate_password_hash(initial_password)
                repaired.append("登录密码")
            if repaired:
                session.commit()
                print(f"修复默认管理员: {', '.join(repaired)}")
            else:
                print(f"管理员已存在: {existing_user.username}")

    except Exception as e:
        session.rollback()
        print(f"创建默认用户失败: {e}")
        raise
    finally:
        session.close()


def drop_all_tables():
    """删除所有表（危险操作）"""
    from backend.db.models import Base

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    print("所有表已删除")


def show_tables():
    """显示所有表"""
    engine = get_engine()

    from sqlalchemy import inspect

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    print("\n当前数据库中的表：")
    for table in tables:
        print(f"  - {table}")

    print(f"\n共 {len(tables)} 个表")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="数据库初始化工具")
    parser.add_argument("--create-db", action="store_true", help="创建数据库")
    parser.add_argument("--create-tables", action="store_true", help="创建所有表")
    parser.add_argument(
        "--init", action="store_true", help="初始化数据库（创建数据库和表）"
    )
    parser.add_argument("--drop-tables", action="store_true", help="删除所有表")
    parser.add_argument("--create-user", action="store_true", help="创建默认用户")
    parser.add_argument("--show-tables", action="store_true", help="显示所有表")
    parser.add_argument(
        "--all", action="store_true", help="执行所有操作（初始化+创建用户）"
    )

    args = parser.parse_args()

    print("=" * 50)
    print("文档处理智能体 - 数据库初始化工具")
    print("=" * 50)
    print(f"MySQL Host: {settings.mysql_host}:{settings.mysql_port}")
    print(f"Database: {settings.mysql_database}")
    print("=" * 50)

    if args.drop_tables:
        print("\n[危险] 准备删除所有表...")
        confirm = input("确认删除所有表？(输入 DELETE 确认): ")
        if confirm == "DELETE":
            drop_all_tables()
            print("表已删除")
        else:
            print("取消删除")
        return

    if args.create_db or args.init or args.all:
        print("\n[1/2] 创建数据库...")
        create_database()

    if args.create_tables or args.init or args.all:
        print("\n[2/2] 创建表...")
        create_tables()

    if args.create_user or args.all:
        print("\n[可选] 创建默认用户...")
        create_default_user()

    if args.show_tables:
        show_tables()

    if not any(
        [
            args.create_db,
            args.create_tables,
            args.init,
            args.drop_tables,
            args.create_user,
            args.show_tables,
            args.all,
        ]
    ):
        parser.print_help()

    print("\n完成！")

    close_db()


if __name__ == "__main__":
    main()
