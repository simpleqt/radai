#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
CURRENT="${RUNTIME_ROOT}/current"
AUDIT_FRONTEND="${CURRENT}/frontend_audit"
LEGACY_FRONTEND="${CURRENT}/frontend"

if [[ -x "${AUDIT_FRONTEND}/node_modules/.bin/vite" ]]; then
  frontend_dir="${AUDIT_FRONTEND}"
elif [[ -x "${LEGACY_FRONTEND}/node_modules/.bin/vite" ]]; then
  # Rollback compatibility for immutable releases created before frontend_audit
  # became a standalone application. New releases always take the branch above.
  frontend_dir="${LEGACY_FRONTEND}"
  export VITE_AUDIT_ONLY=true
else
  printf '[frontend-5177] ERROR: 当前 release 缺少可执行的审计前端\n' >&2
  exit 1
fi

if [[ "${1:-}" == "--check" ]]; then
  exit 0
fi
[[ "$#" -eq 0 ]] || {
  printf '用法：%s [--check]\n' "$0" >&2
  exit 2
}

cd "${frontend_dir}"

# 正式服务走构建产物（vite preview）；发布准备阶段已为五个前端执行 npm run build。
# 回滚兼容：无 dist 的早期不可变 release 退回 vite dev 源码服务。
if [[ -f "${frontend_dir}/dist/index.html" ]]; then
  exec "${frontend_dir}/node_modules/.bin/vite" preview \
    --host 0.0.0.0 --port 5177 --strictPort
else
  printf '[frontend-5177] WARN: 缺少 dist 构建产物，回退 vite dev 源码服务\n' >&2
  exec "${frontend_dir}/node_modules/.bin/vite" \
    --host 0.0.0.0 --port 5177 --strictPort
fi
