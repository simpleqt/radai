"""
命令行入口
提供便捷的命令行工具
"""

import argparse
import subprocess
import sys


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="智能文档处理 Agent 系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  doc-agent run-api           # 启动 API 服务
  doc-agent run-worker        # 启动 Worker
  doc-agent test              # 运行测试
  doc-agent format            # 格式化代码
  doc-agent lint              # 检查代码
  doc-agent init-db           # 初始化数据库
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # run-api 命令
    api_parser = subparsers.add_parser("run-api", help="启动 API 服务")
    api_parser.add_argument("--host", default=None, help="服务器地址（默认从环境变量读取）")
    api_parser.add_argument("--port", type=int, default=None, help="服务器端口（默认从环境变量读取）")
    api_parser.add_argument("--reload", action="store_true", help="自动重载")
    api_parser.add_argument("--workers", type=int, default=4, help="Worker 数量")

    # run-worker 命令
    worker_parser = subparsers.add_parser("run-worker", help="启动 Worker")
    worker_parser.add_argument("--workers", type=int, default=1, help="Worker 数量")

    # run-ingest-worker 命令
    ingest_worker_parser = subparsers.add_parser("run-ingest-worker", help="启动知识库入库 Worker")
    ingest_worker_parser.add_argument("--workers", type=int, default=1, help="Worker 数量")

    # test 命令
    test_parser = subparsers.add_parser("test", help="运行测试")
    test_parser.add_argument("--cov", action="store_true", help="生成覆盖率报告")
    test_parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    test_parser.add_argument("path", nargs="?", default="tests/", help="测试路径")

    # format 命令
    format_parser = subparsers.add_parser("format", help="格式化代码")
    format_parser.add_argument("--check", action="store_true", help="只检查不修改")
    format_parser.add_argument("path", nargs="?", default="backend/", help="代码路径")

    # lint 命令
    lint_parser = subparsers.add_parser("lint", help="检查代码")
    lint_parser.add_argument("path", nargs="?", default="backend/", help="代码路径")

    # init-db 命令
    subparsers.add_parser("init-db", help="初始化数据库")

    # migrate 命令
    subparsers.add_parser("migrate", help="运行数据库迁移")

    # seed 命令
    subparsers.add_parser("seed", help="填充测试数据")

    # clean 命令
    subparsers.add_parser("clean", help="清理临时文件")

    # version 命令
    subparsers.add_parser("version", help="显示版本信息")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # 执行命令
    if args.command == "run-api":
        import uvicorn

        import os

        # 绑定地址从环境变量或命令行参数读取
        host = args.host or os.getenv("SERVER_HOST", "0.0.0.0")
        port = args.port or int(os.getenv("SERVER_PORT", "8082"))

        kwargs = {
            "host": host,
            "port": port,
            "reload": args.reload,
        }
        
        # 仅当 reload 不是 True 时，UVicorn 才允许配置 multiple workers
        if not args.reload:
            kwargs["workers"] = args.workers

        uvicorn.run(
            "backend.api.http_server:app",
            **kwargs
        )

    elif args.command == "run-worker":
        import asyncio

        from backend.worker.task_worker import run_worker

        asyncio.run(run_worker())

    elif args.command == "run-ingest-worker":
        import asyncio

        from backend.worker.ingest_worker import run_worker

        asyncio.run(run_worker())

    elif args.command == "test":
        cmd = [sys.executable, "-m", "pytest", args.path, "-v"]
        if args.cov:
            cmd.extend(
                ["--cov=backend", "--cov-report=html", "--cov-report=term-missing"]
            )
        subprocess.run(cmd)

    elif args.command == "format":
        if args.check:
            subprocess.run([sys.executable, "-m", "black", "--check", args.path])
            subprocess.run([sys.executable, "-m", "isort", "--check-only", args.path])
        else:
            subprocess.run([sys.executable, "-m", "black", args.path])
            subprocess.run([sys.executable, "-m", "isort", args.path])

    elif args.command == "lint":
        subprocess.run([sys.executable, "-m", "flake8", args.path])
        subprocess.run([sys.executable, "-m", "mypy", args.path])

    elif args.command == "init-db":
        subprocess.run([sys.executable, "-m", "backend.db.init_db"])

    elif args.command == "migrate":
        subprocess.run([sys.executable, "-m", "backend.db.migrate"])

    elif args.command == "seed":
        subprocess.run([sys.executable, "-m", "backend.db.seed"])

    elif args.command == "clean":
        subprocess.run(["make", "clean"])

    elif args.command == "version":
        subprocess.run(["make", "version"])


if __name__ == "__main__":
    main()