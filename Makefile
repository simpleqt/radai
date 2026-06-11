.PHONY: help install install-dev install-all clean test test-cov test-unit test-integration lint format format-check check run-api run-worker run-all docker-up docker-down docker-logs docker-build init-db migrate seed pre-commit-install pre-commit-run docs-build docs-serve deps-update deps-check setup version

PYTHON ?= python
PIP ?= $(PYTHON) -m pip

help: ## 显示帮助信息
	@echo "可用命令:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## 安装核心依赖
	$(PIP) install -r requirements.txt

install-dev: ## 安装开发依赖
	$(PIP) install -r requirements-dev.txt

install-all: ## 安装所有依赖（核心 + 开发）
	$(PIP) install -e ".[dev]"

clean: ## 清理临时文件
	find backend scripts -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find backend scripts -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find backend scripts -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find backend scripts -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find backend scripts -type d -name .black_cache -exec rm -rf {} + 2>/dev/null || true
	find backend scripts -type f -name "*.pyc" -delete
	find backend scripts -type f -name "*.pyo" -delete
	find backend scripts -type f -name "*.pyd" -delete
	find backend scripts -type f -name "*.so" -delete
	rm -rf build/ dist/ *.egg-info .eggs/

test: ## 运行所有测试
	$(PYTHON) -m pytest backend/tests/ -v

test-cov: ## 运行测试并生成覆盖率报告
	$(PYTHON) -m pytest backend/tests/ --cov=backend --cov-report=html --cov-report=term-missing

test-unit: ## 运行单元测试
	$(PYTHON) -m pytest backend/tests/ -v

test-integration: ## 运行集成测试
	$(PYTHON) -m pytest backend/tests/ -v

lint: ## 运行代码检查（flake8 + mypy）
	$(PYTHON) -m flake8 backend/
	$(PYTHON) -m mypy backend/

format: ## 格式化代码（black + isort）
	$(PYTHON) -m black backend/
	$(PYTHON) -m isort backend/

format-check: ## 检查代码格式（不修改文件）
	$(PYTHON) -m black --check backend/
	$(PYTHON) -m isort --check-only backend/

check: format-check lint ## 运行所有检查（格式 + lint）

run-api: ## 启动 API 服务
	$(PYTHON) -m backend.api.http_server

run-guest-api: ## 启动访客专用 API（8083，前台调试）
	GUEST_SERVER_PORT=8083 GUEST_SERVER_WORKERS=1 $(PYTHON) -m backend.api.guest_http_server

run-guest-api-daemon: ## 守护进程启动访客 API（关闭终端不中断）
	bash scripts/guest_backend_service.sh start

run-worker: ## 启动 Worker
	$(PYTHON) -m backend.worker.task_worker

run-ingest-worker: ## 启动入库 Worker
        $(PYTHON) -m backend.worker.ingest_worker

run-all: ## 启动所有服务（API + Worker + Ingest Worker）
        @echo "启动 API 服务..."
        @$(PYTHON) -m backend.api.http_server &
        @echo "启动 Worker..."
        @$(PYTHON) -m backend.worker.task_worker &
        @echo "启动 Ingest Worker..."
        @$(PYTHON) -m backend.worker.ingest_worker

docker-logs: ## 查看 Docker 日志
	docker-compose logs -f

docker-build: ## 构建 Docker 镜像
	docker-compose build

init-db: ## 初始化数据库
	$(PYTHON) -m backend.db.init_db

migrate: ## 运行数据库迁移
	$(PYTHON) -m backend.db.migrate

seed: ## 填充测试数据
	$(PYTHON) -m backend.db.seed

pre-commit-install: ## 安装 pre-commit hooks
	pre-commit install

pre-commit-run: ## 运行 pre-commit 检查
	pre-commit run --all-files

docs-build: ## 构建文档
	mkdocs build

docs-serve: ## 启动文档服务器
	mkdocs serve

deps-update: ## 更新依赖到最新版本
	$(PIP) install --upgrade -r requirements.txt
	$(PIP) install --upgrade -r requirements-dev.txt
	$(PIP) freeze > requirements.txt
	$(PIP) freeze > requirements-dev.txt

deps-check: ## 检查依赖安全性
	pip-audit
	safety check

setup: ## 初始化开发环境
	$(PIP) install -e ".[dev]"
	pre-commit install
	cp .env.example .env
	@echo "开发环境初始化完成！"
	@echo "请编辑 .env 文件配置环境变量"

version: ## 显示版本信息
	@echo "Python 版本:"
	@$(PYTHON) --version
	@echo ""
	@echo "核心依赖版本:"
	@$(PYTHON) -m pip show langchain | grep Version
	@$(PYTHON) -m pip show langgraph | grep Version
	@$(PYTHON) -m pip show fastapi | grep Version
	@$(PYTHON) -m pip show pydantic | grep Version