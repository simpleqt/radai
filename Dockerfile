# 使用 Python slim 镜像作为基础镜像
FROM python:3.14-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY pyproject.toml requirements.txt ./

# 安装 Python 依赖
RUN pip install --no-cache-dir -e ".[dev]"

# 复制项目代码
COPY backend/ ./backend/
COPY .env.example .env

# 创建必要的目录
RUN mkdir -p backend/logs backend/data/documents backend/data/results backend/data/cache backend/data/uploads_temp

# 暴露端口
EXPOSE ${SERVER_PORT:-8082}

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:${SERVER_PORT:-8082}/health')"

# 启动命令
CMD ["python", "-m", "backend.api.http_server"]
