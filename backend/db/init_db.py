"""数据库初始化脚本"""

import argparse

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

    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS {database_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cursor.execute(f"USE {database_name}")

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
    try:
        Base.metadata.create_all(bind=engine)
        print("所有表创建成功！")
    except Exception as e:
        print(f"部分表创建失败（通常为 agent_kb_bindings FK 排序规则冲突）：{e}")
        print("请参考 docs/docs_backend/agent_kb_binding_module.md 手动建表。")

    tables = [
        "users",
        "api_keys",
        "documents",
        "tasks",
        "extraction_results",
        "task_logs",
        "agents",
        "agent_kb_bindings",
    ]

    for table in tables:
        print(f"  ✓ {table}")


def create_default_user():
    """创建默认用户（如果不存在）

    注意：生成的 API Key 为随机值，创建后会在控制台打印一次。
    请及时保存，不会再次显示。
    """
    import secrets
    import uuid

    from backend.db.connection import get_session

    session = get_session()

    try:
        from backend.db.models import ApiKey, User

        existing_user = session.query(User).first()

        if not existing_user:
            # 生成随机 API Key，不再使用硬编码默认值
            generated_api_key = f"radai-{secrets.token_hex(24)}"

            user = User(
                user_id=str(uuid.uuid4()),
                username="admin",
                email="",
                api_key=generated_api_key,
                is_active=True,
            )
            session.add(user)
            session.flush()

            api_key = ApiKey(
                api_key=generated_api_key,
                user_id=user.user_id,
                key_name="初始化默认 Key",
                is_active=True,
                rate_limit=1000,
            )
            session.add(api_key)

            session.commit()
            print("创建默认用户: admin")
            print(f"创建默认 API Key: {generated_api_key}")
            print("⚠️  请立即保存上述 API Key，不会再次显示！")
        else:
            print(f"用户已存在: {existing_user.username}")

    except Exception as e:
        session.rollback()
        print(f"创建默认用户失败: {e}")
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
    print("数据库初始化工具")
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
