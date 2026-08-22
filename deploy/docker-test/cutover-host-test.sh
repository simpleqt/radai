#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${DOC_AGENT_DOCKER_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-docker-test}"
HOST_TEST_ROOT="${DOC_AGENT_HOST_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
MANAGER="${SCRIPT_DIR}/manage.sh"
DEPLOYMENT_ENV="${RUNTIME_ROOT}/state/deployment.env"
HOST_RELEASE_SCRIPT="${HOST_TEST_ROOT}/current/deploy/local-test/release.sh"
OLD_RUNTIME_UNIT=doc-agent-test-runtime.service
OLD_TIMER=doc-agent-test-auto-update.timer
OLD_UPDATE_SERVICE=doc-agent-test-auto-update.service
NEW_TIMER=doc-agent-docker-test-auto-update.timer
NEW_UPDATE_SERVICE=doc-agent-docker-test-auto-update.service
PRODUCTION_PORTS=(5173 5174 5176 5177 8082 8083 8086)
PRIMARY_TEST_PORTS=(15173 15174 15176 18082 18083 18084 18086)
SHADOW_PORTS=(25173 25174 25176 28082 28083 28084 28086)
ALL_DOCKER_API_PORTS=(18082 18083 18086 28082 28083 28086)

log() {
  printf '[radai-docker-test-cutover] %s\n' "$*"
}

fail() {
  printf '[radai-docker-test-cutover] ERROR: %s\n' "$*" >&2
  exit 1
}

capture_ports() {
  local output="$1"
  shift
  local port
  : >"${output}"
  for port in "$@"; do
    ss -H -ltnp "sport = :${port}" >>"${output}" || true
  done
  sort -o "${output}" "${output}"
  chmod 600 "${output}"
}

assert_ports_free() {
  local label="$1"
  shift
  local port listening=()
  for port in "$@"; do
    if ss -H -ltn "sport = :${port}" | grep -q .; then
      listening+=("${port}")
    fi
  done
  ((${#listening[@]} == 0)) || fail "${label} 端口仍被占用：${listening[*]}"
}

wait_service_inactive() {
  local service="$1" deadline=$((SECONDS + 180))
  while systemctl --user is-active --quiet "${service}"; do
    ((SECONDS < deadline)) || fail "等待 ${service} 结束超时"
    sleep 2
  done
}

unit_state() {
  systemctl --user is-enabled "$1" 2>/dev/null || true
}

restore_unit_enablement() {
  local unit="$1" state="$2"
  if [[ "${state}" == enabled ]]; then
    systemctl --user enable "${unit}" >/dev/null 2>&1 || true
  else
    systemctl --user disable "${unit}" >/dev/null 2>&1 || true
  fi
}

old_timer_state="$(unit_state "${OLD_TIMER}")"
old_runtime_state="$(unit_state "${OLD_RUNTIME_UNIT}")"
new_timer_state="$(unit_state "${NEW_TIMER}")"
host_was_stopped=false
docker_was_stopped=false
cutover_succeeded=false
backup_root=""
previous_deployment_env=""
docker_snapshot=""

rollback_cutover() {
  local exit_code=$?
  [[ "${cutover_succeeded}" == false ]] || return 0
  trap - ERR INT TERM
  set +e
  log "切换未完成，开始恢复原宿主机测试与 Docker 影子栈"
  systemctl --user disable --now "${NEW_TIMER}" >/dev/null 2>&1
  systemctl --user stop "${NEW_UPDATE_SERVICE}" >/dev/null 2>&1
  if [[ -f "${DEPLOYMENT_ENV}" ]]; then
    "${MANAGER}" down >/dev/null 2>&1
  fi
  if [[ -n "${docker_snapshot}" && -f "${docker_snapshot}/manifest.json" ]]; then
    local -a restore_args=()
    local port
    for port in "${ALL_DOCKER_API_PORTS[@]}"; do
      restore_args+=(--guard-port "${port}")
    done
    python3 "${SCRIPT_DIR}/state_snapshot.py" restore \
      --snapshot-dir "${docker_snapshot}" \
      --target-root "${RUNTIME_ROOT}" \
      "${restore_args[@]}" >/dev/null 2>&1
  fi
  if [[ -n "${previous_deployment_env}" && -f "${previous_deployment_env}" ]]; then
    install -m 0600 "${previous_deployment_env}" "${DEPLOYMENT_ENV}"
  fi
  if [[ "${host_was_stopped}" == true ]]; then
    systemctl --user start "${OLD_RUNTIME_UNIT}"
  fi
  restore_unit_enablement "${OLD_RUNTIME_UNIT}" "${old_runtime_state}"
  if [[ -n "${previous_deployment_env}" && -f "${previous_deployment_env}" ]]; then
    "${MANAGER}" up >/dev/null 2>&1
  fi
  if [[ "${old_timer_state}" == enabled ]]; then
    systemctl --user enable --now "${OLD_TIMER}" >/dev/null 2>&1
  fi
  restore_unit_enablement "${NEW_TIMER}" "${new_timer_state}"
  log "已尝试恢复原测试服务；切换失败日志和 SQLite 快照保留在 ${backup_root:-${RUNTIME_ROOT}/backups}"
  exit "${exit_code}"
}

trap rollback_cutover ERR INT TERM

for command_name in docker git python3 curl ss systemctl flock install; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "缺少命令：${command_name}"
done
[[ -x "${MANAGER}" ]] || fail "Docker 测试控制器不可执行"
[[ -x "${HOST_RELEASE_SCRIPT}" ]] || fail "宿主机测试发布器不可执行"
[[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态不存在"

mkdir -p "${RUNTIME_ROOT}/state" "${RUNTIME_ROOT}/backups"
chmod 700 "${RUNTIME_ROOT}/state" "${RUNTIME_ROOT}/backups"
exec 9>"${RUNTIME_ROOT}/state/auto-update.lock"
flock -n 9 || fail "Docker 测试自动更新或切换正在运行"

current_mode="$(awk -F= '$1 == "DEPLOYMENT_MODE" {print $2}' "${DEPLOYMENT_ENV}")"
current_mode="${current_mode:-shadow}"
active_sha="$(awk -F= '$1 == "ACTIVE_SOURCE_SHA" {print $2}' "${DEPLOYMENT_ENV}")"
[[ "${active_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "Docker 测试活动 SHA 无效"

if [[ "${current_mode}" == primary ]]; then
  log "Docker 已是主测试模式；不重复切换"
  "${MANAGER}" smoke
  assert_ports_free "旧 Docker 影子" "${SHADOW_PORTS[@]}"
  "${SCRIPT_DIR}/install-user-units.sh"
  cutover_succeeded=true
  trap - ERR INT TERM
  exit 0
fi
[[ "${current_mode}" == shadow ]] || fail "不支持从 ${current_mode} 模式切换"

host_release="$(readlink -f "${HOST_TEST_ROOT}/current")"
host_sha="$(git -C "${host_release}" rev-parse HEAD)"
[[ "${host_sha}" == "${active_sha}" ]] \
  || fail "宿主机测试 SHA ${host_sha} 与 Docker ${active_sha} 不一致，拒绝切换"

log "切换前重新运行 Docker 影子业务冒烟"
"${MANAGER}" smoke

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_root="${RUNTIME_ROOT}/backups/host-cutover-${timestamp}"
install -d -m 0700 "${backup_root}"
previous_deployment_env="${backup_root}/deployment.shadow.env"
install -m 0600 "${DEPLOYMENT_ENV}" "${previous_deployment_env}"
capture_ports "${backup_root}/production.before" "${PRODUCTION_PORTS[@]}"
capture_ports "${backup_root}/host-test.before" "${PRIMARY_TEST_PORTS[@]}"

log "暂停原宿主机测试自动更新定时器"
systemctl --user disable --now "${OLD_TIMER}" >/dev/null
wait_service_inactive "${OLD_UPDATE_SERVICE}"
exec 8>"${HOST_TEST_ROOT}/state/auto-update.lock"
flock -n 8 || fail "原宿主机测试更新仍持有部署锁"

log "停止 Docker 影子栈并创建其切换前 SQLite 快照"
"${MANAGER}" down
docker_was_stopped=true
assert_ports_free "旧 Docker 影子" "${SHADOW_PORTS[@]}"
docker_snapshot="${backup_root}/docker-before"
python3 "${SCRIPT_DIR}/state_snapshot.py" snapshot \
  --source-root "${RUNTIME_ROOT}" \
  --output-dir "${docker_snapshot}"

log "停止原宿主机测试进程并创建最终 SQLite 快照"
host_was_stopped=true
"${HOST_RELEASE_SCRIPT}" down
systemctl --user disable "${OLD_RUNTIME_UNIT}" >/dev/null
assert_ports_free "原宿主机测试" "${PRIMARY_TEST_PORTS[@]}"
python3 "${SCRIPT_DIR}/state_snapshot.py" snapshot \
  --source-root "${HOST_TEST_ROOT}" \
  --output-dir "${backup_root}/host-before"

log "将宿主机测试最终状态迁入 Docker 独立数据目录"
seed_args=()
for port in "${ALL_DOCKER_API_PORTS[@]}"; do
  seed_args+=(--guard-port "${port}")
done
python3 "${SCRIPT_DIR}/seed_state.py" \
  --source-checkpoints "${HOST_TEST_ROOT}/data/checkpoints" \
  --source-conversations "${HOST_TEST_ROOT}/data/conversations" \
  --target-checkpoints "${RUNTIME_ROOT}/data/checkpoints" \
  --target-conversations "${RUNTIME_ROOT}/data/conversations" \
  --target-state "${RUNTIME_ROOT}/state" \
  --force \
  "${seed_args[@]}"

log "Docker 接管现有测试端口并执行完整业务验证"
"${MANAGER}" deploy "${active_sha}" primary

capture_ports "${backup_root}/production.after" "${PRODUCTION_PORTS[@]}"
cmp --silent "${backup_root}/production.before" "${backup_root}/production.after" \
  || fail "正式服务监听端口或 PID 在测试切换中发生变化"
assert_ports_free "旧 Docker 影子" "${SHADOW_PORTS[@]}"
systemctl --user is-active --quiet "${OLD_RUNTIME_UNIT}" \
  && fail "原宿主机测试 unit 仍处于活动状态"

for port in "${PRIMARY_TEST_PORTS[@]}"; do
  ss -H -ltn "sport = :${port}" | grep -q . || fail "Docker 主测试端口未监听：${port}"
done

log "安装 Docker 主测试自动更新定时器"
"${SCRIPT_DIR}/install-user-units.sh"
systemctl --user is-active --quiet "${NEW_TIMER}" || fail "Docker 测试自动更新 timer 未激活"

{
  printf 'CUTOVER_STATUS=complete\n'
  printf 'CUTOVER_AT_UTC=%s\n' "${timestamp}"
  printf 'ACTIVE_SOURCE_SHA=%s\n' "${active_sha}"
  printf 'PREVIOUS_HOST_RELEASE=%s\n' "${host_release}"
  printf 'ROLLBACK_SNAPSHOT=%s\n' "${backup_root}"
} >"${RUNTIME_ROOT}/state/host-test-cutover.env.tmp.$$"
chmod 600 "${RUNTIME_ROOT}/state/host-test-cutover.env.tmp.$$"
mv -f \
  "${RUNTIME_ROOT}/state/host-test-cutover.env.tmp.$$" \
  "${RUNTIME_ROOT}/state/host-test-cutover.env"

systemctl --user stop doc-agent-test-release-a7f277a.service >/dev/null 2>&1 || true
cutover_succeeded=true
trap - ERR INT TERM
log "Docker 已接管现有测试端口；旧 25xxx/28xxx 影子端口已关闭"
log "回滚快照：${backup_root}"
