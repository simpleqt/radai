#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
TARGET_SHA="${1:-}"
RELEASE="${RUNTIME_ROOT}/releases/${TARGET_SHA}"
BACKEND_ENV="${RUNTIME_ROOT}/config/backend.env"
SERVICE_ENV="${RUNTIME_ROOT}/config/service.env"
RUN_WITH_ENV="${RUNTIME_ROOT}/bin/run_with_env.py"
CATALOG="${RUNTIME_ROOT}/data/conversations/conversations.db"
CATALOG_MARKER="${RUNTIME_ROOT}/state/conversation-catalog-migration.json"
CATALOG_BACKUPS="${RUNTIME_ROOT}/backups/conversation-catalog"
SQLITE_BACKUPS="${RUNTIME_ROOT}/backups/sqlite"
GROUNDWATER_BUILTIN_ROOT="${DOC_AGENT_GROUNDWATER_BUILTIN_ROOT:-/home/inspur/myenv/runtime/doc-agent-shared/groundwater/liutang}"

log() {
  printf '[production-preflight] %s\n' "$*"
}

fail() {
  printf '[production-preflight] ERROR: %s\n' "$*" >&2
  exit 1
}

require_setting() {
  local expected="$1"
  grep -Fqx -- "${expected}" "${SERVICE_ENV}" \
    || fail "生产 service.env 缺少安全设置：${expected%%=*}"
}

require_boolean_setting() {
  local key="$1"
  local key_count valid_count
  key_count="$(grep -Ec "^${key}=" "${SERVICE_ENV}" || true)"
  valid_count="$(grep -Ec "^${key}=(true|false)$" "${SERVICE_ENV}" || true)"
  [[ "${key_count}" == "1" && "${valid_count}" == "1" ]] \
    || fail "生产 service.env 缺少或包含无效布尔安全设置：${key}"
}

[[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "必须提供完整目标 SHA"
[[ -d "${RELEASE}" ]] || fail "目标 release 尚未准备"
[[ "$(git -C "${RELEASE}" rev-parse HEAD)" == "${TARGET_SHA}" ]] \
  || fail "目标 release 与 SHA 不一致"
[[ -x "${RELEASE}/.venv/bin/python" ]] || fail "目标 release Python 不可执行"
[[ -f "${BACKEND_ENV}" && ! -L "${BACKEND_ENV}" ]] \
  || fail "生产 backend.env 必须是普通文件"
[[ -f "${SERVICE_ENV}" && ! -L "${SERVICE_ENV}" ]] \
  || fail "生产 service.env 必须是普通文件"
[[ -x "${RUN_WITH_ENV}" ]] || fail "安全环境加载器不可执行"

require_setting "DOC_AGENT_RUNTIME_ROOT=${RUNTIME_ROOT}"
require_setting "AGENT_CONVERSATION_STORE_BACKEND=sqlite"
require_setting "AGENT_CONVERSATION_SQLITE_PATH=${CATALOG}"
require_setting "MYSQL_SESSION_READ_ONLY=false"
require_setting "STARTUP_SCHEMA_INITIALIZATION_ENABLED=false"
require_boolean_setting "TASK_DISPATCH_OUTBOX_ENABLED"
require_setting "API_DOCS_ENABLED=false"
require_setting "RAG_WORK_DIR=${RUNTIME_ROOT}/data/rag"
require_setting "GROUNDWATER_BUILTIN_ROOT=${GROUNDWATER_BUILTIN_ROOT}"
[[ -d "${GROUNDWATER_BUILTIN_ROOT}" ]] \
  || fail "生产地下水内置数据目录不存在"
[[ -f "${GROUNDWATER_BUILTIN_ROOT}/manifest.json" \
    && ! -L "${GROUNDWATER_BUILTIN_ROOT}/manifest.json" ]] \
  || fail "生产地下水内置数据缺少普通 manifest.json"

log "离线校验目标提交的迁移包"
(
  cd "${RELEASE}"
  "${RELEASE}/.venv/bin/python" -m backend.db.migrate --verify-package >/dev/null
)

log "只读确认正式 MySQL 迁移账本无待执行版本"
(
  cd "${RELEASE}"
  "${RUN_WITH_ENV}" --env-file "${BACKEND_ENV}" -- \
    "${RELEASE}/.venv/bin/python" -m backend.db.migrate --check
)

log "只读确认模型用途隔离所需内部配置已准备"
(
  cd "${RELEASE}"
  "${RUN_WITH_ENV}" --env-file "${BACKEND_ENV}" -- \
    "${RELEASE}/.venv/bin/python" \
    "${RELEASE}/deploy/production/model_usage_transition.py" verify >/dev/null
)

mkdir -p \
  "${RUNTIME_ROOT}/data/conversations" \
  "${CATALOG_BACKUPS}" \
  "${SQLITE_BACKUPS}" \
  "${RUNTIME_ROOT}/state"
chmod 700 \
  "${RUNTIME_ROOT}/data/conversations" \
  "${RUNTIME_ROOT}/backups" \
  "${CATALOG_BACKUPS}" \
  "${SQLITE_BACKUPS}" \
  "${RUNTIME_ROOT}/state"

if [[ -f "${CATALOG_MARKER}" ]]; then
  log "验证既有生产会话目录及迁移基线"
  "${RUN_WITH_ENV}" --env-file "${BACKEND_ENV}" -- \
    "${RELEASE}/.venv/bin/python" \
    "${RELEASE}/deploy/local-test/conversation_catalog_migration.py" \
    verify --target "${CATALOG}" --marker "${CATALOG_MARKER}"
else
  log "以只读一致性快照初始化生产会话目录"
  "${RUN_WITH_ENV}" --env-file "${BACKEND_ENV}" -- \
    "${RELEASE}/.venv/bin/python" \
    "${RELEASE}/deploy/local-test/conversation_catalog_migration.py" \
    migrate \
    --target "${CATALOG}" \
    --marker "${CATALOG_MARKER}" \
    --backup-dir "${CATALOG_BACKUPS}"
fi

log "创建切换前生产 SQLite 一致性备份"
"${RELEASE}/.venv/bin/python" \
  "${RUNTIME_ROOT}/bin/sqlite_snapshot.py" create \
  --source-dir "${RUNTIME_ROOT}/data" \
  --backup-root "${SQLITE_BACKUPS}" \
  --state-dir "${RUNTIME_ROOT}/state"

log "目标 release、会话迁移、数据库账本和切换前备份均已通过"
