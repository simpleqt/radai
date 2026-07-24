"""
命令行入口
提供便捷的命令行工具
"""

import argparse
import json
import subprocess
import sys
from contextlib import redirect_stdout


def _run_commands(*commands: list[str]) -> int:
    """Run commands in order and propagate the first non-zero exit status."""
    for command in commands:
        result = subprocess.run(command, check=False)
        if result.returncode:
            return int(result.returncode)
    return 0


def main(argv: list[str] | None = None) -> int:
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
  doc-agent agent validate    # 校验全部 Agent 定义
  doc-agent agent init demo --name "Demo Agent" --domain demo-domain
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # run-api 命令
    api_parser = subparsers.add_parser("run-api", help="启动 API 服务")
    api_parser.add_argument("--host", default="0.0.0.0", help="服务器地址")
    api_parser.add_argument("--port", type=int, default=8082, help="服务器端口")
    api_parser.add_argument("--reload", action="store_true", help="自动重载")
    api_parser.add_argument(
        "--service-role",
        choices=("all", "agent", "knowledge"),
        default="all",
        help="API 服务角色：all（兼容模式）、agent（轻量智能体）、knowledge（RAG/知识库）",
    )
    api_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="API Worker 数量（后台任务当前要求为 1）",
    )

    # run-worker 命令
    worker_parser = subparsers.add_parser("run-worker", help="启动 Worker")
    worker_parser.add_argument("--workers", type=int, default=1, help="Worker 数量")

    # run-ingest-worker 命令
    ingest_worker_parser = subparsers.add_parser(
        "run-ingest-worker", help="启动知识库入库 Worker"
    )
    ingest_worker_parser.add_argument(
        "--workers", type=int, default=1, help="Worker 数量"
    )

    # test 命令
    test_parser = subparsers.add_parser("test", help="运行测试")
    test_parser.add_argument("--cov", action="store_true", help="生成覆盖率报告")
    test_parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    test_parser.add_argument(
        "path", nargs="?", default="backend/tests/", help="测试路径"
    )

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
    migrate_parser = subparsers.add_parser("migrate", help="运行数据库迁移")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="只列出待执行迁移"
    )
    migrate_parser.add_argument(
        "--mark-existing",
        action="store_true",
        help="仅写入迁移基线，不执行迁移",
    )

    # seed 命令
    subparsers.add_parser("seed", help="填充测试数据")

    # clean 命令
    subparsers.add_parser("clean", help="清理临时文件")

    # version 命令
    subparsers.add_parser("version", help="显示版本信息")

    # agent 命令
    agent_parser = subparsers.add_parser("agent", help="智能体运行调试工具")
    agent_subparsers = agent_parser.add_subparsers(
        dest="agent_command",
        help="agent 子命令",
    )
    replay_parser = agent_subparsers.add_parser("replay", help="重放智能体 trace")
    replay_parser.add_argument("--run-id", required=True, help="要重放的 run_id")
    replay_parser.add_argument(
        "--mode",
        choices=("dry-run", "tool-cache", "full"),
        default="dry-run",
        help=("重放模式；tool-cache/full 为离线 trace 重放，不执行外部模型或工具"),
    )
    replay_parser.add_argument("--from-unit", default=None, help="只重放指定 unit")
    replay_parser.add_argument(
        "--trace-root",
        default=None,
        help="trace JSONL 根目录",
    )
    eval_parser = agent_subparsers.add_parser(
        "eval",
        help="评估智能体 trace 数据集",
    )
    eval_parser.add_argument(
        "--dataset",
        required=True,
        help="JSONL eval 数据集路径",
    )
    eval_parser.add_argument("--trace-root", default=None, help="trace JSONL 根目录")
    eval_parser.add_argument(
        "--record",
        action="store_true",
        help="将 eval 结果写入历史",
    )
    eval_parser.add_argument(
        "--version",
        default="unversioned",
        help="本次 eval 版本号",
    )
    eval_parser.add_argument(
        "--history-root",
        default=None,
        help="eval 历史 JSONL 根目录",
    )
    acceptance_parser = agent_subparsers.add_parser(
        "acceptance-metrics",
        help="汇总智能体运行验收指标",
    )
    acceptance_parser.add_argument(
        "--run-id",
        action="append",
        default=None,
        help="只统计指定 run_id；可重复传入，默认统计全部 run",
    )
    acceptance_parser.add_argument(
        "--trace-root",
        default=None,
        help="trace 后端根目录",
    )
    acceptance_parser.add_argument(
        "--observations-json",
        default=None,
        help="取消、恢复和并发压测观测 JSON 文件",
    )
    eval_history_parser = agent_subparsers.add_parser(
        "eval-history",
        help="查看智能体 eval 趋势历史",
    )
    eval_history_parser.add_argument(
        "--history-root",
        default=None,
        help="eval 历史根目录",
    )
    eval_history_parser.add_argument(
        "--dataset",
        default=None,
        help="按数据集过滤",
    )
    eval_history_parser.add_argument("--version", default=None, help="按版本过滤")
    eval_history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="最多返回记录数",
    )
    eval_compare_parser = agent_subparsers.add_parser(
        "eval-compare",
        help="比较两个 eval 版本",
    )
    eval_compare_parser.add_argument(
        "--history-root",
        default=None,
        help="eval 历史根目录",
    )
    eval_compare_parser.add_argument(
        "--dataset",
        default=None,
        help="按数据集过滤",
    )
    eval_compare_parser.add_argument(
        "--baseline-version",
        required=True,
        help="基线版本",
    )
    eval_compare_parser.add_argument(
        "--candidate-version",
        required=True,
        help="候选版本",
    )
    validate_parser = agent_subparsers.add_parser(
        "validate",
        help="跨 Profile/Prompt/Tool/Skill/Workflow/Guardrail 校验智能体",
    )
    validate_parser.add_argument(
        "agent_code",
        nargs="?",
        default=None,
        help="智能体 code；省略时校验全部 Profile",
    )
    validate_parser.add_argument(
        "--project-root",
        default=None,
        help="从指定兼容源码树读取全部 Agent 注册表",
    )
    init_agent_parser = agent_subparsers.add_parser(
        "init",
        help="生成可验证的新智能体 Profile、Prompt、Skill 和 ToolPolicy",
    )
    init_agent_parser.add_argument("agent_code", help="新智能体 code")
    init_agent_parser.add_argument("--name", required=True, help="前端展示名称")
    init_agent_parser.add_argument(
        "--domain",
        default=None,
        help="领域 code；默认与 agent_code 相同",
    )
    init_agent_parser.add_argument("--description", default=None, help="智能体描述")
    init_agent_parser.add_argument(
        "--default-workflow",
        default="full",
        help="默认工作流；候选值从目标项目 Workflow registry 动态读取",
    )
    init_agent_parser.add_argument(
        "--project-root",
        default=None,
        help="项目源码根目录；默认使用当前工作目录（wheel 安装目录不可修改）",
    )
    init_agent_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查目标和冲突，不写文件",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # 执行命令
    if args.command == "run-api":
        import os

        import uvicorn

        if not args.reload and args.workers != 1:
            parser.error(
                "run-api --workers 当前必须为 1：可重连智能体任务使用进程内注册表"
            )

        os.environ["API_SERVICE_ROLE"] = args.service_role
        kwargs = {
            # Keep the knowledge service private when it is started on 127.0.0.1.
            "host": args.host,
            "port": args.port,
            "reload": args.reload,
        }

        if args.reload:
            kwargs["reload_dirs"] = ["backend"]
            kwargs["reload_excludes"] = [
                "backend/logs",
                "backend/data",
                "*.log",
                "*.md",
                "*.xlsx",
            ]

        # 仅当 reload 不是 True 时，UVicorn 才允许配置 multiple workers
        if not args.reload:
            kwargs["workers"] = args.workers

        uvicorn.run("backend.api.http_server:app", **kwargs)

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
        return _run_commands(cmd)

    elif args.command == "format":
        if args.check:
            return _run_commands(
                [sys.executable, "-m", "black", "--check", args.path],
                [sys.executable, "-m", "isort", "--check-only", args.path],
            )
        else:
            return _run_commands(
                [sys.executable, "-m", "black", args.path],
                [sys.executable, "-m", "isort", args.path],
            )

    elif args.command == "lint":
        return _run_commands(
            [sys.executable, "-m", "flake8", args.path],
            [sys.executable, "-m", "mypy", args.path],
        )

    elif args.command == "init-db":
        return _run_commands([sys.executable, "-m", "backend.db.init_db"])

    elif args.command == "migrate":
        cmd = [sys.executable, "-m", "backend.db.migrate"]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.mark_existing:
            cmd.append("--mark-existing")
        return _run_commands(cmd)

    elif args.command == "seed":
        return _run_commands([sys.executable, "-m", "backend.db.seed"])

    elif args.command == "clean":
        return _run_commands(["make", "clean"])

    elif args.command == "version":
        return _run_commands(["make", "version"])

    elif args.command == "agent":
        if args.agent_command == "replay":
            from backend.application.agent_trace import (
                AgentRunReplay,
                create_agent_trace_store,
            )

            store = create_agent_trace_store(
                args.trace_root,
                backend="jsonl" if args.trace_root else None,
            )
            summary = AgentRunReplay(store).replay(
                args.run_id,
                mode=args.mode,
                from_unit=args.from_unit,
            )
            print(
                json.dumps(
                    summary.to_json_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.agent_command == "eval":
            from backend.application.agent_trace import (
                AgentTraceEvaluator,
                EvalHistoryStore,
                create_agent_trace_store,
            )

            store = create_agent_trace_store(
                args.trace_root,
                backend="jsonl" if args.trace_root else None,
            )
            summary = AgentTraceEvaluator(store).evaluate_dataset(args.dataset)
            payload = summary.to_json_dict()
            if args.record:
                record = EvalHistoryStore(args.history_root).append(
                    summary,
                    version=args.version,
                )
                payload["history_record"] = record.to_json_dict()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        elif args.agent_command == "acceptance-metrics":
            from dataclasses import asdict
            from pathlib import Path

            from backend.application.agent_trace import (
                AgentTraceAcceptanceObservations,
                create_agent_trace_store,
                evaluate_acceptance_metrics,
            )

            store = create_agent_trace_store(
                args.trace_root,
                backend="jsonl" if args.trace_root else None,
            )
            requested_run_ids = args.run_id or store.list_run_ids()
            run_ids = list(dict.fromkeys(requested_run_ids))
            records = [
                record for run_id in run_ids for record in store.read_records(run_id)
            ]
            observations = None
            if args.observations_json:
                raw_observations = json.loads(
                    Path(args.observations_json).read_text(encoding="utf-8")
                )
                if not isinstance(raw_observations, dict):
                    raise ValueError("observations JSON must be an object")
                observations = AgentTraceAcceptanceObservations(**raw_observations)
            metrics = evaluate_acceptance_metrics(
                records,
                observations=observations,
            )
            print(
                json.dumps(
                    {"run_ids": run_ids, "metrics": asdict(metrics)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.agent_command == "eval-history":
            from backend.application.agent_trace import EvalHistoryStore

            trend = EvalHistoryStore(args.history_root).trend(
                dataset=args.dataset,
                version=args.version,
                limit=args.limit,
            )
            print(
                json.dumps(
                    trend.to_json_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.agent_command == "eval-compare":
            from backend.application.agent_trace import EvalHistoryStore

            comparison = EvalHistoryStore(args.history_root).compare(
                baseline_version=args.baseline_version,
                candidate_version=args.candidate_version,
                dataset=args.dataset,
            )
            print(
                json.dumps(
                    comparison.to_json_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.agent_command == "validate":
            # Some imported providers initialize application logging on
            # stdout. Keep stdout a strict one-document JSON interface for CI.
            try:
                with redirect_stdout(sys.stderr):
                    from backend.application.agents.definition import (
                        AgentDefinitionValidator,
                    )
                    if args.project_root:
                        from pathlib import Path

                        from backend.application.agents.scaffolder import (
                            AgentScaffolder,
                        )

                        scaffolder = AgentScaffolder(
                            Path(args.project_root),
                            declarative_provider_validation=True,
                        )
                        with scaffolder.definition_validator() as validator:
                            reports = (
                                [validator.validate(args.agent_code)]
                                if args.agent_code
                                else validator.validate_all()
                            )
                    else:
                        validator = AgentDefinitionValidator(
                            declarative_provider_validation=True
                        )
                        reports = (
                            [validator.validate(args.agent_code)]
                            if args.agent_code
                            else validator.validate_all()
                        )
            except Exception as exc:  # noqa: BLE001 - stable CLI contract
                print(
                    json.dumps(
                        {"valid": False, "reports": [], "error": str(exc)},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 2
            valid = bool(reports) and all(report.valid for report in reports)
            print(
                json.dumps(
                    {
                        "valid": valid,
                        "reports": [report.to_json_dict() for report in reports],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if valid else 2
        elif args.agent_command == "init":
            from pathlib import Path

            try:
                with redirect_stdout(sys.stderr):
                    from backend.application.agents.scaffolder import AgentScaffolder

                    scaffolder = AgentScaffolder(
                        Path(args.project_root) if args.project_root else None,
                        declarative_provider_validation=True,
                    )
                    result = scaffolder.create(
                        agent_code=args.agent_code,
                        name=args.name,
                        domain=args.domain,
                        description=args.description,
                        default_workflow=args.default_workflow,
                        validate=not args.dry_run,
                        dry_run=args.dry_run,
                    )
            except Exception as exc:  # noqa: BLE001 - CLI emits a stable failure payload
                print(
                    json.dumps(
                        {
                            "valid": False,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 2
            print(
                json.dumps(
                    result.to_json_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            agent_parser.print_help()
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
