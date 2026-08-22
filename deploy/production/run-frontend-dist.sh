#!/usr/bin/env bash
# 正式前端启动器：优先用构建产物（vite preview，几请求/亚 MB 首屏），
# release 缺 dist 或 vite.config 未声明 preview（回滚到早期 release）时
# 退回 vite dev 源码服务——与切换前的行为一致，保证回滚安全。
set -Eeuo pipefail

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
FRONTEND_NAME="${1:?usage: run-frontend-dist.sh <frontend-dir> <port>}"
PORT="${2:?usage: run-frontend-dist.sh <frontend-dir> <port>}"

FRONTEND_DIR="${RUNTIME_ROOT}/current/${FRONTEND_NAME}"
[[ -x "${FRONTEND_DIR}/node_modules/.bin/vite" ]] || {
  printf '[frontend-%s] ERROR: 当前 release 缺少 %s 前端\n' "${PORT}" "${FRONTEND_NAME}" >&2
  exit 1
}

cd "${FRONTEND_DIR}"
if [[ -f "${FRONTEND_DIR}/dist/index.html" ]] && grep -q 'preview: {' "${FRONTEND_DIR}/vite.config.ts"; then
  exec "${FRONTEND_DIR}/node_modules/.bin/vite" preview \
    --host 0.0.0.0 --port "${PORT}" --strictPort
fi

printf '[frontend-%s] WARN: release 缺少 dist/preview 配置，回退 vite dev 源码服务\n' "${PORT}" >&2
exec "${FRONTEND_DIR}/node_modules/.bin/vite" \
  --host 0.0.0.0 --port "${PORT}" --strictPort
