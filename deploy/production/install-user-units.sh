#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
UNIT_SOURCE_DIR="${SCRIPT_DIR}/systemd"
UNIT_TARGET_DIR="${DOC_AGENT_PRODUCTION_USER_UNIT_DIR:-/home/inspur/.config/systemd/user}"
RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
CREDENTIAL_FILE="${DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_FILE:-${RUNTIME_ROOT}/config/git-auth/credentials}"
CREDENTIAL_SOURCE="${DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_SOURCE:-/home/inspur/myenv/runtime/doc-agent-test/config/git-auth/credentials}"
START_TIMER="${DOC_AGENT_PRODUCTION_START_TIMER:-false}"
ENABLE_TASK_OUTBOX="${DOC_AGENT_PRODUCTION_ENABLE_TASK_OUTBOX:-false}"
RETIRE_LEGACY_FRONTEND_4173="${DOC_AGENT_PRODUCTION_RETIRE_LEGACY_4173:-false}"
GROUNDWATER_BUILTIN_ROOT="${DOC_AGENT_GROUNDWATER_BUILTIN_ROOT:-/home/inspur/myenv/runtime/doc-agent-shared/groundwater/liutang}"
LEGACY_FRONTEND_UNIT="doc-agent-prod-frontend-4173.service"
UNITS=(
  doc-agent-prod-agent-api.service
  doc-agent-prod-guest-api.service
  doc-agent-prod-knowledge-api.service
  doc-agent-prod-task-worker.service
  doc-agent-prod-ingest-worker.service
  doc-agent-prod-frontend-5173.service
  doc-agent-prod-frontend-5177.service
  doc-agent-prod-frontend-5174.service
  doc-agent-prod-frontend-5176.service
  doc-agent-production.target
  doc-agent-production-auto-deploy.service
  doc-agent-production-auto-deploy.timer
  doc-agent-production-healthcheck.service
  doc-agent-production-healthcheck.timer
)
RUNTIME_SCRIPTS=(
  changed_units.py
  deploy-now.sh
  deploy-release.sh
  manage.sh
  mysql_table_snapshot.py
  prepare-release.sh
  preflight-release.sh
  production-healthcheck.sh
  render_service_env.py
  run-frontend-5177.sh
  run-frontend-dist.sh
  run_with_env.py
  sqlite_snapshot.py
  verify_unit_transition.py
)

log() {
  printf '[production-unit-install] %s\n' "$*"
}

fail() {
  printf '[production-unit-install] ERROR: %s\n' "$*" >&2
  exit 1
}

private_file() {
  local path="$1" mode mode_value owner_uid
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  owner_uid="$(stat -c '%u' "${path}")"
  [[ "${owner_uid}" == "$(id -u)" ]] || return 1
  mode="$(stat -c '%a' "${path}")"
  mode_value=$((8#${mode}))
  (( (mode_value & 077) == 0 ))
}

retire_legacy_frontend_4173() {
  local unit_file="${UNIT_TARGET_DIR}/${LEGACY_FRONTEND_UNIT}" listeners

  log "仅退役冗余兼容前端 ${LEGACY_FRONTEND_UNIT}；不重启其他业务服务"
  systemctl --user stop "${LEGACY_FRONTEND_UNIT}" >/dev/null 2>&1 || true
  systemctl --user disable "${LEGACY_FRONTEND_UNIT}" >/dev/null 2>&1 || true
  if [[ -e "${unit_file}" || -L "${unit_file}" ]]; then
    [[ -f "${unit_file}" && ! -L "${unit_file}" ]] \
      || fail "拒绝删除非普通 legacy 单元文件：${unit_file}"
    unlink -- "${unit_file}"
  fi
  systemctl --user daemon-reload
  ! systemctl --user is-active --quiet "${LEGACY_FRONTEND_UNIT}" \
    || fail "legacy 4173 单元仍处于 active 状态"
  listeners="$(ss -H -ltn 'sport = :4173' 2>/dev/null || true)"
  [[ -z "${listeners}" ]] || fail "4173 端口仍存在监听，拒绝报告退役成功"
  log "legacy 4173 单元已停止并移除，端口无监听"
}

[[ "${START_TIMER}" == "true" || "${START_TIMER}" == "false" ]] \
  || fail "DOC_AGENT_PRODUCTION_START_TIMER 必须是 true 或 false"
[[ "${ENABLE_TASK_OUTBOX}" == "true" || "${ENABLE_TASK_OUTBOX}" == "false" ]] \
  || fail "DOC_AGENT_PRODUCTION_ENABLE_TASK_OUTBOX 必须是 true 或 false"
[[ "${RETIRE_LEGACY_FRONTEND_4173}" == "true" \
  || "${RETIRE_LEGACY_FRONTEND_4173}" == "false" ]] \
  || fail "DOC_AGENT_PRODUCTION_RETIRE_LEGACY_4173 必须是 true 或 false"
for command in install systemctl unlink; do
  command -v "${command}" >/dev/null || fail "缺少命令 ${command}"
done
if [[ "${RETIRE_LEGACY_FRONTEND_4173}" == "true" ]]; then
  command -v ss >/dev/null || fail "退役 legacy 4173 前端需要 ss 命令"
fi
[[ -d "${RUNTIME_ROOT}/source.git" ]] || fail "生产 bare Git 仓库不存在"
[[ -f "${RUNTIME_ROOT}/config/backend.env" ]] || fail "生产稳定配置不存在"
[[ -f "${RUNTIME_ROOT}/config/service.env" ]] || fail "生产服务配置不存在"
[[ -d "${GROUNDWATER_BUILTIN_ROOT}" ]] || fail "生产地下水内置数据目录不存在"
[[ -f "${GROUNDWATER_BUILTIN_ROOT}/manifest.json" ]] \
  || fail "生产地下水内置数据缺少 manifest.json"

install -d -m 0755 "${UNIT_TARGET_DIR}"
install -d -m 0700 "${RUNTIME_ROOT}/bin" "${RUNTIME_ROOT}/config/git-auth"
if [[ ! -f "${CREDENTIAL_FILE}" ]]; then
  private_file "${CREDENTIAL_SOURCE}" || fail "可复用的运行时 Git 凭据不存在或权限不安全"
  install -m 0600 "${CREDENTIAL_SOURCE}" "${CREDENTIAL_FILE}"
  log "已创建生产专用运行时 Git 凭据文件（未输出内容）"
fi
private_file "${CREDENTIAL_FILE}" || fail "生产 Git 凭据权限不安全"

for script in "${RUNTIME_SCRIPTS[@]}"; do
  [[ -f "${SCRIPT_DIR}/${script}" ]] || fail "缺少部署组件 ${script}"
  install -m 0755 "${SCRIPT_DIR}/${script}" "${RUNTIME_ROOT}/bin/.${script}.${BASHPID}"
  mv -f "${RUNTIME_ROOT}/bin/.${script}.${BASHPID}" "${RUNTIME_ROOT}/bin/${script}"
done
for unit in "${UNITS[@]}"; do
  [[ -f "${UNIT_SOURCE_DIR}/${unit}" ]] || fail "缺少单元模板 ${unit}"
  install -m 0644 "${UNIT_SOURCE_DIR}/${unit}" "${UNIT_TARGET_DIR}/${unit}"
done

render_args=(
  --source "${RUNTIME_ROOT}/config/service.env"
  --output "${RUNTIME_ROOT}/config/service.env"
  --runtime-root "${RUNTIME_ROOT}"
  --groundwater-builtin-root "${GROUNDWATER_BUILTIN_ROOT}"
)
if [[ "${ENABLE_TASK_OUTBOX}" == "true" ]]; then
  render_args+=(--enable-task-outbox)
fi
"${RUNTIME_ROOT}/bin/render_service_env.py" "${render_args[@]}" >/dev/null
log "生产服务安全配置已原子更新；本步骤未重启业务服务"

systemctl --user daemon-reload
if [[ "${RETIRE_LEGACY_FRONTEND_4173}" == "true" ]]; then
  retire_legacy_frontend_4173
fi
systemctl --user enable doc-agent-production-auto-deploy.timer >/dev/null
systemctl --user enable --now doc-agent-production-healthcheck.timer >/dev/null
if [[ "${START_TIMER}" == "true" ]]; then
  systemctl --user start doc-agent-production-auto-deploy.timer
  log "正式自动部署定时器已启动；计划每天 Asia/Shanghai 03:00 检查 origin/production"
else
  log "正式自动部署定时器已安装并启用，未在本次安装中启动"
fi
log "只读健康巡检已启用（每五分钟检查一次，不自动重启业务服务）"
log "主动部署入口：${RUNTIME_ROOT}/bin/deploy-now.sh [PRODUCTION_TIP_FULL_SHA]"
