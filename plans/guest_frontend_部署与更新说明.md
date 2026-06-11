# 访客前端（guest）部署与更新说明

> **文档类型**：🔧 运维手册（2026-05）  
> **关联文档**：[docs/docs_backend/guest_module.md](../docs/docs_backend/guest_module.md)

本文说明如何在服务器上部署与更新 **访客知识库对话前端**（目录：`<project_name>/guest`），以及配套的 **访客专用精简后端**（端口 **8083**）。该前端以 Docker 容器运行静态资源 + 容器内 Nginx，宿主机 Nginx 负责 HTTPS 与 `/api/`、`/files/` 反代及跨域。

---

## 1. 架构与端口

| 组件 | 主机 | 端口 | 说明 |
|------|------|------|------|
| **访客前端容器** | `<frontend_server_ip>` | **8084** | Docker 镜像内 Nginx |
| **访客专用后端** | `<api_server_ip>` | **8083** | 登录 + 问答 + 会话（见 §3.1） |
| **主业务后端** | `<api_server_ip>` | **8082** | 全量 API，**不与 <your_domain> 直连** |
| **宿主机 Nginx** | `<frontend_server_ip>` | 443 | `https://<your_domain>` |
| **文件存储 MinIO** | `<minio_server_ip>` | 9004 | `/files/` 反代目标 |

### 1.1 为何拆分 8083 访客后端

- 主后端（8082）功能复杂，本地开发频繁变更，容易影响线上访客站点。
- 访客后端复用同一套 `backend/` 代码，但独立进程、独立端口、**只注册 guest 前端所需路由**。
- 访客前端永远只调用 8083，主后端升级与访客站点解耦。

```
浏览器 ──HTTPS──► 宿主机 Nginx (<your_domain>)
                    ├─ /        ──► guest 前端容器 :8084
                    ├─ /api/    ──► 访客后端 :8083  (auth + agent + conversations)
                    └─ /files/  ──► MinIO :<minio_port>

主后端 :8082（管理端 / 主前端 / 入库等）◄── 不经过 <your_domain> /api/
```

仓库内可参考的 Nginx 站点片段：`<your_domain>.conf.deploy`（部署到服务器时需放到 Nginx 配置目录并 `nginx -t` / `reload`）。

---

## 2. 服务器信息

### 2.1 前端 + Nginx 主机（<frontend_server_ip>）

| 项 | 值 |
|----|-----|
| 地址 | `<frontend_server_ip>` |
| 用户名 | `ubuntu` |
| 职责 | guest 前端 Docker 容器（8084）、<your_domain> HTTPS Nginx |

> 服务器凭据请向运维获取，**勿在文档中明文存储密码**。

```bash
ssh ubuntu@<frontend_server_ip>
```

### 2.2 业务后端主机（<api_server_ip>）

| 项 | 值 |
|----|-----|
| 地址 | `<api_server_ip>` |
| 项目路径 | `<project_root>` |
| 职责 | 主后端 8082 + 访客后端 8083（同机不同进程） |

访客后端与主后端共用 `backend/.env`（MySQL / Milvus / Redis / LLM）。

---

## 3. 访客专用后端（8083）

### 3.1 入口与 API 清单

**说明**：当前 `guest/App.vue` 要求**先登录**（账号密码），登录后走已认证用户链路，不是匿名访客模式。

| 项 | 值 |
|----|-----|
| 入口文件 | `backend/api/guest_http_server.py` |
| 守护进程脚本 | `scripts/guest_backend_service.sh`（推荐，`start/stop/restart/status`） |
| 前台启动 | `make run-guest-api` 或 `scripts/run_guest_backend.sh` |
| 健康检查 | `GET /health` |

#### 需要（guest 前端会调用）

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/v1/auth/login` | 登录 |
| GET | `/api/v1/auth/verify` | Token 校验（前端定时检查） |
| POST | `/api/v1/auth/logout` | 退出 |
| GET | `/api/v2/agent/info` | 加载模型/知识库信息 |
| POST | `/api/v2/agent/query` | 登录后流式问答（SSE） |
| GET | `/api/v2/conversations` | 历史会话列表 |
| GET | `/api/v2/conversations/{thread_id}/messages` | 加载会话消息 |
| GET | `/api/v2/files?thread_id=...` | 会话附件/工件 |
| POST | `/api/v2/guest/query` | 匿名访客问答（当前 App 未启用，保留备用） |

#### 不需要（主后端 8082 才提供）

知识库 CRUD、文档入库/处理、MCP、Worker、索引同步调度、CAS SSO、文件上传、管理端、模型管理等。

`/files/` 图片资源由 Nginx 反代 MinIO，不经过 Python 后端。

### 3.2 环境变量

与主后端共用 `backend/.env`（LLM、数据库、Milvus 等），以下可单独覆盖：

| 变量 | 默认 | 说明 |
|------|------|------|
| `GUEST_SERVER_PORT` | `8083` | 监听端口 |
| `GUEST_SERVER_WORKERS` | `1` | uvicorn worker 数（守护进程默认 1） |
| `GUEST_CHECKPOINT_DB` | `guest_checkpoints.db` | **独立**于 8082 的 `checkpoints.db`，访客消息历史单独存储 |

#### 数据隔离说明（重要）

| 存储 | 8082 主后端 | 8083 访客后端 | 是否共用 |
|------|-------------|---------------|----------|
| MySQL（用户/会话元数据） | ✓ | ✓ | **共用** |
| SQLite checkpoint（消息正文） | `checkpoints.db` | `guest_checkpoints.db` | **隔离** |
| MinIO `/files/` | ✓ | ✓ | 共用 |

因此：

- 在 **<your_domain>（8083）** 新建的对话：列表与消息均可正常读写。
- 在 **主站（8082）** 的历史对话：**不会**出现在访客站点消息中（设计如此，非 bug）。
- 访客后端的 `/api/v2/conversations` 已过滤：仅返回 `guest_checkpoints.db` 中确有消息的会话，避免「有标题、点进去为空」。

其他可覆盖环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `STARTUP_TASK_PROCESSOR_ENABLED` | `false` | 访客后端不启动任务处理器 |
| `STARTUP_RAG_PREWARM_ENABLED` | `true` | 启动时预热活跃知识库 RAG 实例 |

### 3.3 本地启动（守护进程，推荐）

关闭终端不会中断服务：

```bash
# 启动 / 停止 / 重启 / 状态
bash scripts/guest_backend_service.sh start
bash scripts/guest_backend_service.sh status
bash scripts/guest_backend_service.sh stop
bash scripts/guest_backend_service.sh restart
```

PID 文件：`backend/data/guest-api.pid`  
日志：`backend/logs/guest-api.log`

### 3.4 前台调试启动

```bash
make run-guest-api
# 或
GUEST_SERVER_PORT=8083 venv314/bin/python -m backend.api.guest_http_server
```

### 3.5 服务器上部署 / 重启访客后端

在 **<api_server_ip>** 上，代码更新后执行（推荐使用守护进程脚本）：

```bash
cd <project_root>

# 重启（发版最常用）
bash scripts/guest_backend_service.sh restart

# 或首次启动
bash scripts/guest_backend_service.sh start
bash scripts/guest_backend_service.sh status
```

验证：

```bash
curl http://<guest_api_host>:<guest_port>/health
# 期望: {"status":"healthy","service":"guest-qa-api","port":8083}
```

**注意**：

- 8083 与 8082 可同时运行，互不占用端口。
- 启动约需 15–30 秒（RAG 预热），脚本会自动轮询 `/health`。
- 若 8083 被 `deerflow-provisioner` 等容器占用：`docker stop deerflow-provisioner` 后再启动。
- 更新代码后只需 `restart` 8083，**无需**重启 8082。

### 3.6 本地联调（前端 + 后端）

| 服务 | 端口 | 命令 |
|------|------|------|
| 访客后端 | 8083 | `make run-guest-api` |
| 访客前端 dev | 8085 | `cd guest && npm run dev` |

`guest/vite.config.js` 已将 `/api` 代理到 `http://<guest_api_host>:<guest_port>`。如需指向远程访客后端：

```bash
GUEST_BACKEND_URL=http://<api_server_ip>:8083 npm run dev
```

---

## 4. 构建镜像的前置条件（重要）

Docker 构建上下文必须同时包含：

- `guest/` 下全部源码与 `Dockerfile`、`nginx.conf.template`、`package.json` 等；
- **`frontend/src/`**：`guest` 通过路径别名引用主前端组件。

若只拷贝 `guest` 而不含 `frontend/src`，构建会失败。

---

## 5. 容器环境变量

| 变量 | 含义 | 示例默认值 |
|------|------|------------|
| `PORT` | 容器内 Nginx 监听端口 | `8084` |
| `BACKEND_URL` | 容器内反代 `/api/` 的上游 | `http://<api_server_ip>:8083` |
| `FILES_URL` | 容器内反代 `/files/` 的上游 | `http://<minio_server_ip>:<minio_port>` |

生产环境若由**宿主机 Nginx**直接反代 `/api/`、`/files/`，浏览器访问 `https://<your_domain>/api/...` 时由宿主机转发到 **8083**；容器内 `BACKEND_URL` 主要用于直连容器 IP 端口调试。

---

## 6. 本地打包（在开发机 `RadAI` 根目录）

```bash
cd <project_root>

tar czvf guest-deploy.tar.gz \
  guest/Dockerfile \
  guest/nginx.conf.template \
  guest/package.json \
  guest/package-lock.json \
  guest/vite.config.js \
  guest/index.html \
  guest/.dockerignore \
  guest/public \
  guest/src \
  frontend/src
```

或使用一键脚本（会同步更新容器 `BACKEND_URL` 为 8083）：

```bash
bash scripts/deploy_guest.sh
```

---

## 7. 服务器上构建与运行前端容器

```bash
cd /home/ubuntu
mkdir -p guest-build && tar xzvf guest-deploy.tar.gz -C guest-build

cd guest-build
cp -a frontend guest/frontend

cd guest
sudo docker build -t guest-frontend:latest .
sudo docker rm -f guest-frontend 2>/dev/null || true

sudo docker run -d \
  --name guest-frontend \
  --restart unless-stopped \
  -p 8084:8084 \
  -e PORT=8084 \
  -e BACKEND_URL=http://<api_server_ip>:8083 \
  -e FILES_URL=http://<minio_server_ip>:<minio_port> \
  guest-frontend:latest
```

验证：

```bash
sudo docker ps | grep guest-frontend
curl -I http://<guest_frontend_host>:<guest_frontend_port>
curl http://<api_server_ip>:8083/health
```

---

## 8. 宿主机 Nginx（HTTPS / 反代 / CORS）

配置文件模板：`<your_domain>.conf.deploy`（仓库根目录）。

关键 `proxy_pass`：

| location | 目标 |
|----------|------|
| `/` | `http://<guest_frontend_host>:<guest_frontend_port>` |
| `/api/` | `http://<api_server_ip>:8083` |
| `/files/` | `http://<minio_server_ip>:<minio_port>` |

### 8.1 从开发机一键更新 Nginx（重要：生效文件是 conf.d）

现网实际加载的是 **`/etc/nginx/conf.d/<your_domain>.conf`**，不是 `sites-available`。

```bash
sshpass -p 'chenzhiqian69.' scp -o StrictHostKeyChecking=accept-new \
  <your_domain>.conf.deploy ubuntu@<frontend_server_ip>:/tmp/<your_domain>.conf.deploy

sshpass -p 'chenzhiqian69.' ssh -o StrictHostKeyChecking=accept-new ubuntu@<frontend_server_ip> \
  'sudo cp /tmp/<your_domain>.conf.deploy /etc/nginx/conf.d/<your_domain>.conf && \
   sudo nginx -t && sudo systemctl reload nginx && \
   grep proxy_pass /etc/nginx/conf.d/<your_domain>.conf'
```

确认 `/api/` 行是 **`8083`** 而不是 `8082`。

### 8.2 在服务器上手动更新

```bash
sudo cp <your_domain>.conf.deploy /etc/nginx/conf.d/<your_domain>.conf
sudo nginx -t && sudo systemctl reload nginx
grep proxy_pass /etc/nginx/conf.d/<your_domain>.conf
```

---

## 9. 更新部署的标准流程（每次发版）

### 9.0 一键完整发版（推荐）

在 **<api_server_ip>** 开发机上，于项目根目录依次执行：

```bash
cd <project_root>

# ① 部署访客前端（打包 → 上传 <frontend_server_ip> → Docker 构建替换容器）
bash scripts/deploy_guest.sh

# ② 重启访客后端（加载最新 backend 代码）
bash scripts/guest_backend_service.sh restart

# ③ 更新 Nginx（必须写入 conf.d，确保 /api/ → 8083）
sshpass -p 'chenzhiqian69.' scp -o StrictHostKeyChecking=accept-new \
  <your_domain>.conf.deploy ubuntu@<frontend_server_ip>:/tmp/<your_domain>.conf.deploy
sshpass -p 'chenzhiqian69.' ssh -o StrictHostKeyChecking=accept-new ubuntu@<frontend_server_ip> \
  'sudo cp /tmp/<your_domain>.conf.deploy /etc/nginx/conf.d/<your_domain>.conf && \
   sudo nginx -t && sudo systemctl reload nginx'
```

### 9.1 发版后验证清单

```bash
# 前端容器（在 <frontend_server_ip> 上，或由 deploy 脚本自动验证）
curl -I http://<frontend_server_ip>:8084

# 访客后端
curl http://<api_server_ip>:8083/health

# HTTPS 站点
curl -skI https://<your_domain>/ | head -3
```

浏览器手动验证：

1. 打开 https://<your_domain>
2. 账号密码登录
3. 发起对话，确认 SSE 流式回答
4. 切换历史会话，确认消息加载
5. 含图片回答时检查 `/files/` 展示

### 9.2 仅更新访客前端

```bash
bash scripts/deploy_guest.sh
# 可选指定版本标签
bash scripts/deploy_guest.sh --tag 20260522
```

### 9.3 仅更新访客后端（8083）

```bash
cd <project_root>
bash scripts/guest_backend_service.sh restart
curl http://<guest_api_host>:<guest_port>/health
```

### 9.4 主后端（8082）升级

- 正常升级 8082，**不影响**已指向 8083 的访客站点。
- 若 agent / RAG 相关代码有变更，需同步 `guest_backend_service.sh restart`。

### 9.5 回滚

- **前端**：在 <frontend_server_ip> 上用旧镜像 tag 重新 `docker run`，例如 `guest-frontend:202605221044` 之前的版本。
- **后端**：`git checkout` 到上一版本后 `bash scripts/guest_backend_service.sh restart`。

---

## 10. 部署记录

| 日期 | 操作 | 结果 |
|------|------|------|
| 2026-05-22 | 拆分访客后端 8083，扩展 auth/agent/conversations 路由 | `guest_http_server.py` 上线 |
| 2026-05-22 | 前端部署 `bash scripts/deploy_guest.sh` | 镜像 `guest-frontend:202605221044`，容器 `guest-frontend` Up |
| 2026-05-22 | 后端 `guest_backend_service.sh restart` | 8083 healthy |
| 2026-05-22 | Nginx 同步 `<your_domain>.conf.deploy` 并重载 | `/api/` → 8083，`https://<your_domain>` 200 |

**当前现网状态（2026-05-22）**：

| 检查项 | 命令 / 地址 | 期望 |
|--------|-------------|------|
| 前端 | `http://<frontend_server_ip>:8084` | HTTP 200 |
| 后端 | `http://<api_server_ip>:8083/health` | `guest-qa-api` healthy |
| 站点 | `https://<your_domain>` | HTTP 200，可登录对话 |

---

## 11. 常见问题

| 现象 | 排查方向 |
|------|----------|
| 构建报找不到 `@/views/...` | 打包是否包含 `frontend/src` |
| 页面空白或 404 | 容器是否运行、8084 是否与 Nginx 一致 |
| 对话 502 / 超时 | 8083 是否启动；`curl :8083/health`；安全组是否放行 8083（内网访问通常不需要公网放行） |
| 仍打到 8082 管理端 | 检查 Nginx `/api/`、`BACKEND_URL`、vite proxy 是否都已改为 **8083** |
| 8083 端口被占用 | 本机可能跑着 `deerflow-provisioner` 等 Docker 容器；`docker ps \| grep 8083` 后停止冲突容器 |
| 图片 `/files/` 404 | 宿主机 Nginx `/files/` 与 MinIO Host 头配置 |
| 本地 dev 端口冲突 | 访客后端用 **8083**，前端 dev 用 **8085** |

---

## 12. 相关文件索引

| 路径 | 说明 |
|------|------|
| `backend/api/guest_http_server.py` | 访客专用精简后端入口（8083） |
| `backend/custom/api/guest_conversations.py` | 访客会话列表（过滤无 guest checkpoint 的条目） |
| `scripts/guest_backend_service.sh` | 守护进程启停（`start/stop/restart/status`） |
| `scripts/run_guest_backend.sh` | 前台日志启动（不推荐生产） |
| `scripts/deploy_guest.sh` | 一键打包上传部署前端 |
| `Makefile` | `make run-guest-api` / `make run-guest-api-daemon` |
| `guest/Dockerfile` | 前端多阶段构建 + 容器内 Nginx |
| `guest/nginx.conf.template` | 容器内站点模板 |
| `guest/vite.config.js` | 本地 dev：8085，代理 `/api` → 8083 |
| `<your_domain>.conf.deploy` | 宿主机 Nginx（`/api/` → 8083） |

---

*文档版本：2026-05-22（含首次完整发版记录）；IP、端口、域名请以现网为准。*
