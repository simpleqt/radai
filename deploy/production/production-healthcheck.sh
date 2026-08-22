#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
MANAGER="${DOC_AGENT_PRODUCTION_MANAGER:-${RUNTIME_ROOT}/bin/manage.sh}"
STATE_DIR="${RUNTIME_ROOT}/state"
LOCK_FILE="${STATE_DIR}/production-healthcheck.lock"
SUCCESS_FILE="${STATE_DIR}/production-healthcheck.success"
PUBLIC_URLS="${DOC_AGENT_PRODUCTION_PUBLIC_URLS:-https://radai2.cdut.edu.cn/ https://csh-ai.cdut.edu.cn/ https://fmca.cdut.edu.cn/}"
CURRENT="${RUNTIME_ROOT}/current"

log() {
  printf '[production-healthcheck] %s\n' "$*"
}

[[ -x "${MANAGER}" ]] || { log "ERROR: manager 不可执行" >&2; exit 1; }
mkdir -p "${STATE_DIR}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "已有健康巡检正在运行，本次跳过"
  exit 0
fi

# 磁盘水位：/health 对磁盘满静默，写满盘会让部署链停摆。
DISK_USAGE_THRESHOLD="${DISK_USAGE_THRESHOLD:-90}"
for mount_point in /home; do
  usage="$(df -P "${mount_point}" | awk 'NR==2 {gsub("%", "", $5); print $5}')"
  log "磁盘使用 ${mount_point} ${usage}%（阈值 ${DISK_USAGE_THRESHOLD}%）"
  (( usage < DISK_USAGE_THRESHOLD )) || exit 1
done

# Redis 只读探活：登录/会话/限流全面依赖 Redis，宕机数小时 /health 仍绿。
# 探针脚本用带引号定界符的 heredoc 落盘（正文零展开），路径经 argv 传入；
# Redis 连接参数在 backend.env，经 run_with_env 注入，部署 shell 永不
# source 环境文件。get_redis() 返回 asyncio 客户端，必须 asyncio.run 驱动
# ping 并显式 aclose——直接同步调用只会拿到恒为真的协程对象，探针空转。
redis_probe="${STATE_DIR}/.redis-probe.${BASHPID}.py"
redis_probe_cleanup() { rm -f -- "${redis_probe}"; }
trap redis_probe_cleanup EXIT
cat >"${redis_probe}" <<'PYEOF'
import asyncio
import sys

release_root = sys.argv[1]
sys.path.insert(0, release_root)


async def _probe() -> bool:
    from backend.db.redis import get_redis

    client = get_redis()
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


try:
    print("ok" if asyncio.run(_probe()) else "fail")
except Exception:
    print("fail")
PYEOF
REDIS_CHECK="$("${CURRENT}/.venv/bin/python" \
  "${CURRENT}/deploy/production/run_with_env.py" \
  --env-file "${RUNTIME_ROOT}/config/backend.env" -- \
  "${CURRENT}/.venv/bin/python" "${redis_probe}" "${CURRENT}" 2>/dev/null || echo fail)"
redis_probe_cleanup
trap - EXIT
log "Redis ping: ${REDIS_CHECK}"
[[ "${REDIS_CHECK}" == "ok" ]] || exit 1

"${MANAGER}" assert-running
for url in ${PUBLIC_URLS}; do
  code="$(curl -L -sS -o /dev/null -w '%{http_code}' \
    --connect-timeout 5 --max-time 30 --max-redirs 3 "${url}" 2>/dev/null || true)"
  log "${url} HTTP ${code}"
  [[ "${code}" == "200" ]] || exit 1
done

temporary="${SUCCESS_FILE}.${BASHPID}"
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${temporary}"
chmod 600 "${temporary}"
mv -f "${temporary}" "${SUCCESS_FILE}"
log "本机服务与正式 HTTPS 地址均健康；未执行自动重启"
