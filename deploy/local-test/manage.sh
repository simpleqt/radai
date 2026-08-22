#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNTIME_ROOT="${DOC_AGENT_TEST_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
if [[ -n "${DOC_AGENT_TEST_RELEASE_ROOT:-}" ]]; then
  REPO_ROOT="$(readlink -f "${DOC_AGENT_TEST_RELEASE_ROOT}")"
elif [[ "${SCRIPT_DIR}" == "${RUNTIME_ROOT}/bin" ]]; then
  [[ -L "${RUNTIME_ROOT}/current" ]] \
    || { printf '[doc-agent-test] ERROR: 测试 current release 不存在\n' >&2; exit 1; }
  REPO_ROOT="$(readlink -f "${RUNTIME_ROOT}/current")"
else
  REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
fi
SOURCE_ENV="${DOC_AGENT_TEST_SOURCE_ENV:-/home/inspur/myenv/langchian_project/backend/.env}"
SOURCE_CHECKPOINT_DIR="${DOC_AGENT_TEST_SOURCE_CHECKPOINT_DIR:-/home/inspur/myenv/langchian_project/backend/data}"
CONFIG_DIR="${RUNTIME_ROOT}/config"
ENV_FILE="${CONFIG_DIR}/runtime.env"
STATE_DIR="${RUNTIME_ROOT}/state"
PID_DIR="${STATE_DIR}/pids"
LOG_DIR="${RUNTIME_ROOT}/logs"
DATA_DIR="${RUNTIME_ROOT}/data"
REPORT_DIR="${RUNTIME_ROOT}/reports"
MIGRATION_MARKER="${STATE_DIR}/checkpoint-migration.json"
MIGRATION_BACKUP_DIR="${STATE_DIR}/checkpoint-backups"
CATALOG_PATH="${DATA_DIR}/conversations/conversations.db"
CATALOG_MARKER="${STATE_DIR}/conversation-catalog-migration.json"
CATALOG_BACKUP_DIR="${STATE_DIR}/conversation-catalog-backups"
RESTART_PLAN="${STATE_DIR}/restart-plan"
RUNTIME_UNIT_NAME="${DOC_AGENT_TEST_RUNTIME_UNIT:-doc-agent-test-runtime.service}"
ALL_SERVICES=(
  agent-api
  knowledge-api
  guest-api
  frontend
  csh-frontend
  fmca-frontend
  landslide-frontend
  guest-frontend
)
STOP_SERVICES=(
  guest-frontend
  landslide-frontend
  fmca-frontend
  csh-frontend
  frontend
  guest-api
  knowledge-api
  agent-api
)
SELECTED_SERVICES=()

log() {
  printf '[doc-agent-test] %s\n' "$*"
}

fail() {
  printf '[doc-agent-test] ERROR: %s\n' "$*" >&2
  exit 1
}

ensure_runtime_dirs() {
  umask 077
  mkdir -p \
    "${CONFIG_DIR}" \
    "${PID_DIR}" \
    "${LOG_DIR}/backend" \
    "${REPORT_DIR}" \
    "${DATA_DIR}/storage/documents" \
    "${DATA_DIR}/storage/results" \
    "${DATA_DIR}/storage/cache" \
    "${DATA_DIR}/storage/uploads" \
    "${DATA_DIR}/rag" \
    "${DATA_DIR}/checkpoints" \
    "${DATA_DIR}/conversations" \
    "${DATA_DIR}/traces" \
    "${MIGRATION_BACKUP_DIR}" \
    "${CATALOG_BACKUP_DIR}"
}

init_config() {
  ensure_runtime_dirs
  [[ -f "${SOURCE_ENV}" ]] || fail "现有服务配置不存在：${SOURCE_ENV}"
  local args=(
    --source "${SOURCE_ENV}"
    --output "${ENV_FILE}"
    --runtime-root "${RUNTIME_ROOT}"
  )
  if [[ -n "${DOC_AGENT_TEST_PUBLIC_HOST:-}" ]]; then
    args+=(--public-host "${DOC_AGENT_TEST_PUBLIC_HOST}")
  fi
  python3 "${REPO_ROOT}/deploy/local-test/render_runtime_env.py" "${args[@]}"
  chmod 600 "${ENV_FILE}"
  log "已从现有配置生成测试运行环境（未输出任何凭据）"
}

load_config() {
  init_config
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

port_is_listening() {
  local port="$1"
  ss -H -ltn "sport = :${port}" | grep -q .
}

pid_release_sha() {
  local pid="$1" cwd relative sha
  cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
  relative="${cwd#"${RUNTIME_ROOT}/releases/"}"
  [[ "${relative}" != "${cwd}" ]] || return 1
  sha="${relative%%/*}"
  [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || return 1
  printf '%s\n' "${sha}"
}

pid_is_owned() {
  local name="$1" pid_file="${PID_DIR}/$1.pid" pid
  [[ -f "${pid_file}" ]] || return 1
  pid="$(<"${pid_file}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  pid_release_sha "${pid}" >/dev/null || return 1
  if [[ "${DOC_AGENT_TEST_REQUIRE_RUNTIME_CGROUP:-true}" == "true" ]]; then
    grep -Fq "/${RUNTIME_UNIT_NAME}" "/proc/${pid}/cgroup" 2>/dev/null \
      || return 1
  fi
}

select_services() {
  local candidate known selected
  SELECTED_SERVICES=()
  if (($# == 0)); then
    SELECTED_SERVICES=("${ALL_SERVICES[@]}")
    return
  fi
  for candidate in "$@"; do
    known=false
    for selected in "${ALL_SERVICES[@]}"; do
      if [[ "${candidate}" == "${selected}" ]]; then
        known=true
        break
      fi
    done
    [[ "${known}" == "true" ]] || fail "未知测试服务：${candidate}"
    for selected in "${SELECTED_SERVICES[@]}"; do
      [[ "${candidate}" != "${selected}" ]] || fail "重启计划包含重复服务：${candidate}"
    done
    SELECTED_SERVICES+=("${candidate}")
  done
}

service_is_selected() {
  local expected="$1" selected
  for selected in "${SELECTED_SERVICES[@]}"; do
    [[ "${expected}" == "${selected}" ]] && return 0
  done
  return 1
}

service_port() {
  case "$1" in
    agent-api) printf '%s\n' "${TEST_AGENT_PORT}" ;;
    knowledge-api) printf '%s\n' "${TEST_KNOWLEDGE_PORT}" ;;
    guest-api) printf '%s\n' "${TEST_GUEST_API_PORT}" ;;
    frontend) printf '%s\n' "${TEST_FRONTEND_PORT}" ;;
    csh-frontend) printf '%s\n' "${TEST_CSH_FRONTEND_PORT}" ;;
    fmca-frontend) printf '%s\n' "${TEST_FMCA_FRONTEND_PORT}" ;;
    landslide-frontend) printf '%s\n' "${TEST_LANDSLIDE_FRONTEND_PORT}" ;;
    guest-frontend) printf '%s\n' "${TEST_GUEST_FRONTEND_PORT}" ;;
    *) fail "未知测试服务：$1" ;;
  esac
}

assert_port_available() {
  local name="$1" port="$2"
  if port_is_listening "${port}" && ! pid_is_owned "${name}"; then
    fail "测试端口 ${port} 已被非本 release 进程占用，拒绝启动 ${name}"
  fi
}

preflight() {
  load_config
  local command
  for command in git uv npm curl python3 ss; do
    command -v "${command}" >/dev/null || fail "缺少命令：${command}"
  done
  [[ "${DOC_AGENT_TEST_CONFIG_MODE}" == "shared-existing-v1" ]] \
    || fail "运行配置不是 shared-existing-v1"
  [[ "${MYSQL_SESSION_READ_ONLY}" == "true" ]] \
    || fail "MYSQL_SESSION_READ_ONLY 必须为 true"
  [[ "${AGENT_CONVERSATION_WRITES_ENABLED}" == "true" ]] \
    || fail "隔离 SQLite 会话目录必须允许写入"
  [[ "${AGENT_CONVERSATION_STORE_BACKEND}" == "sqlite" ]] \
    || fail "AGENT_CONVERSATION_STORE_BACKEND 必须为 sqlite"
  [[ "${AGENT_CONVERSATION_SQLITE_PATH}" == "${CATALOG_PATH}" ]] \
    || fail "会话目录必须位于测试 runtime data 目录"
  [[ "${AGENT_SHARED_OBJECT_STORAGE_READ_ONLY}" == "true" ]] \
    || fail "共享对象存储必须保持只读"
  [[ "${FOLLOW_UP_SUGGESTIONS_ENABLED}" == "true" ]] \
    || fail "测试环境必须启用后续问题建议"
  [[ "${STARTUP_RAG_INITIALIZATION_ENABLED}" == "true" ]] \
    || fail "Knowledge RAG 必须复用现有知识库并完成初始化"
  [[ "${GUEST_RAG_STARTUP_ENABLED}" == "true" ]] \
    || fail "Guest RAG 必须复用现有知识库并完成初始化"
  [[ "${HEALTH_ALLOW_EXPECTED_DEGRADED}" == "false" ]] \
    || fail "功能同构测试不接受预期 degraded 状态"
  [[ "${AUTH_REDIS_DB}" =~ ^[0-9]+$ ]] \
    || fail "AUTH_REDIS_DB 必须是 Redis DB 编号"
  (( AUTH_REDIS_DB >= 0 && AUTH_REDIS_DB <= 15 )) \
    || fail "AUTH_REDIS_DB 必须在 0..15 范围内"
  [[ "${AUTH_REDIS_DB}" != "${REDIS_DB:-0}" ]] \
    || fail "测试登录态 Redis DB 不能与共享知识索引 Redis DB 相同"
  [[ "${TEST_ENABLE_WORKERS}" == "false" ]] \
    || fail "共享外部服务模式禁止启动 Worker"

  assert_port_available agent-api "${TEST_AGENT_PORT}"
  assert_port_available guest-api "${TEST_GUEST_API_PORT}"
  assert_port_available knowledge-api "${TEST_KNOWLEDGE_PORT}"
  assert_port_available frontend "${TEST_FRONTEND_PORT}"
  assert_port_available csh-frontend "${TEST_CSH_FRONTEND_PORT}"
  assert_port_available fmca-frontend "${TEST_FMCA_FRONTEND_PORT}"
  assert_port_available landslide-frontend "${TEST_LANDSLIDE_FRONTEND_PORT}"
  assert_port_available guest-frontend "${TEST_GUEST_FRONTEND_PORT}"

  local protected_ports=(5173 5174 5176 5177 8082 8083 8086)
  local port
  for port in "${protected_ports[@]}"; do
    port_is_listening "${port}" \
      || fail "现有服务基线端口 ${port} 未监听，停止部署并要求人工确认"
  done
  log "预检通过：测试端口独立，现有服务基线全部在监听"
}

prepare() {
  load_config
  log "按锁文件创建当前 release 的独立 Python 环境"
  (
    cd "${REPO_ROOT}"
    uv sync --frozen --no-install-project
  )

  local frontend_dir
  for frontend_dir in frontend frontend_audit frontend_csh_ai frontend_fmca frontend_landslide guest; do
    log "安装 ${frontend_dir} 前端依赖"
    (
      cd "${REPO_ROOT}/${frontend_dir}"
      npm ci --no-audit --no-fund
    )
  done
}

check_external() {
  load_config
  [[ -x "${REPO_ROOT}/.venv/bin/python" ]] \
    || fail "缺少独立 Python 环境，请先执行 prepare"
  local report="${REPORT_DIR}/external-services-$(date +%Y%m%d-%H%M%S).json"
  "${REPO_ROOT}/.venv/bin/python" \
    "${REPO_ROOT}/deploy/local-test/external_services_check.py" \
    | tee "${report}"
  chmod 600 "${report}"
  log "现有外部服务只读检查通过：${report}"
}

migrate_state() {
  load_config
  python3 "${REPO_ROOT}/deploy/local-test/state_migration.py" migrate \
    --source-dir "${SOURCE_CHECKPOINT_DIR}" \
    --target-dir "${DATA_DIR}/checkpoints" \
    --marker "${MIGRATION_MARKER}" \
    --backup-dir "${MIGRATION_BACKUP_DIR}"
  "${REPO_ROOT}/.venv/bin/python" \
    "${REPO_ROOT}/deploy/local-test/conversation_catalog_migration.py" migrate \
    --target "${CATALOG_PATH}" \
    --marker "${CATALOG_MARKER}" \
    --backup-dir "${CATALOG_BACKUP_DIR}"
  log "当前 checkpoint 与会话目录已迁移或保留；正式 MySQL 继续只读"
}

force_migrate_state() {
  load_config
  python3 "${REPO_ROOT}/deploy/local-test/state_migration.py" migrate \
    --source-dir "${SOURCE_CHECKPOINT_DIR}" \
    --target-dir "${DATA_DIR}/checkpoints" \
    --marker "${MIGRATION_MARKER}" \
    --backup-dir "${MIGRATION_BACKUP_DIR}" \
    --force
  "${REPO_ROOT}/.venv/bin/python" \
    "${REPO_ROOT}/deploy/local-test/conversation_catalog_migration.py" migrate \
    --target "${CATALOG_PATH}" \
    --marker "${CATALOG_MARKER}" \
    --backup-dir "${CATALOG_BACKUP_DIR}" \
    --force
  log "已强制刷新测试 checkpoint 与会话目录，原测试文件已在线备份"
}

verify_state() {
  load_config
  python3 "${REPO_ROOT}/deploy/local-test/state_migration.py" verify \
    --target-dir "${DATA_DIR}/checkpoints" \
    --marker "${MIGRATION_MARKER}" \
    --quick
  "${REPO_ROOT}/.venv/bin/python" \
    "${REPO_ROOT}/deploy/local-test/conversation_catalog_migration.py" verify \
    --target "${CATALOG_PATH}" \
    --marker "${CATALOG_MARKER}" \
    --quick
}

start_process() {
  local name="$1" cwd="$2"
  shift 2
  local pid_file="${PID_DIR}/${name}.pid" log_file="${LOG_DIR}/${name}.log" pid
  if pid_is_owned "${name}"; then
    log "${name} 已运行（PID $(<"${pid_file}")）"
    return
  fi
  rm -f "${pid_file}"
  (
    cd "${cwd}"
    nohup "$@" >>"${log_file}" 2>&1 </dev/null &
    pid=$!
    printf '%s\n' "${pid}" >"${pid_file}.tmp"
    mv "${pid_file}.tmp" "${pid_file}"
  )
  sleep 1
  if ! pid_is_owned "${name}"; then
    tail -n 100 "${log_file}" >&2 || true
    fail "${name} 启动失败"
  fi
  log "已启动 ${name}（PID $(<"${pid_file}")）"
}

stop_process() {
  local name="$1" pid_file="${PID_DIR}/$1.pid" pid deadline
  [[ -f "${pid_file}" ]] || return
  if ! pid_is_owned "${name}"; then
    rm -f "${pid_file}"
    return
  fi
  pid="$(<"${pid_file}")"
  kill -TERM "${pid}"
  deadline=$((SECONDS + 20))
  while kill -0 "${pid}" 2>/dev/null && ((SECONDS < deadline)); do
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill -KILL "${pid}"
  fi
  rm -f "${pid_file}"
  log "已停止 ${name}（仅处理测试 PID ${pid}）"
}

start_apps() {
  select_services "$@"
  load_config
  [[ -x "${REPO_ROOT}/.venv/bin/python" ]] \
    || fail "缺少独立 Python 环境，请先执行 prepare"
  verify_state >/dev/null

  local name
  for name in "${SELECTED_SERVICES[@]}"; do
    assert_port_available "${name}" "$(service_port "${name}")"
  done

  for name in "${ALL_SERVICES[@]}"; do
    service_is_selected "${name}" || continue
    case "${name}" in
      agent-api)
        start_process agent-api "${REPO_ROOT}" \
          "${REPO_ROOT}/.venv/bin/python" -m backend.cli run-api \
          --host 0.0.0.0 \
          --port "${TEST_AGENT_PORT}" \
          --service-role agent \
          --workers 1
        ;;
      knowledge-api)
        [[ "${TEST_ENABLE_KNOWLEDGE_API}" == "true" ]] \
          || fail "当前拓扑要求启用 Knowledge API"
        start_process knowledge-api "${REPO_ROOT}" \
          "${REPO_ROOT}/.venv/bin/python" -m backend.cli run-api \
          --host 0.0.0.0 \
          --port "${TEST_KNOWLEDGE_PORT}" \
          --service-role knowledge \
          --workers 1
        ;;
      guest-api)
        start_process guest-api "${REPO_ROOT}" \
          "${REPO_ROOT}/.venv/bin/python" -m backend.api.guest_http_server
        ;;
      frontend)
        export VITE_API_PROXY_TARGET="http://127.0.0.1:${TEST_AGENT_PORT}"
        export VITE_KNOWLEDGE_API_PROXY_TARGET="http://127.0.0.1:${TEST_KNOWLEDGE_PORT}"
        export VITE_FILES_PROXY_TARGET="${TEST_FILES_PROXY_TARGET}"
        export VITE_ALLOWED_HOSTS="${DOC_AGENT_TEST_PUBLIC_HOST}"
        start_process frontend "${REPO_ROOT}/frontend" \
          "${REPO_ROOT}/frontend/node_modules/.bin/vite" \
          --host 0.0.0.0 --port "${TEST_FRONTEND_PORT}" --strictPort
        ;;
      csh-frontend)
        export VITE_API_PROXY_TARGET="http://127.0.0.1:${TEST_AGENT_PORT}"
        export VITE_KNOWLEDGE_API_PROXY_TARGET="http://127.0.0.1:${TEST_KNOWLEDGE_PORT}"
        export VITE_FILES_PROXY_TARGET="${TEST_FILES_PROXY_TARGET}"
        export VITE_ALLOWED_HOSTS="${DOC_AGENT_TEST_PUBLIC_HOST}"
        start_process csh-frontend "${REPO_ROOT}/frontend_csh_ai" \
          "${REPO_ROOT}/frontend_csh_ai/node_modules/.bin/vite" \
          --host 0.0.0.0 --port "${TEST_CSH_FRONTEND_PORT}" --strictPort
        ;;
      fmca-frontend)
        export VITE_API_PROXY_TARGET="http://127.0.0.1:${TEST_AGENT_PORT}"
        export VITE_KNOWLEDGE_API_PROXY_TARGET="http://127.0.0.1:${TEST_KNOWLEDGE_PORT}"
        export VITE_FILES_PROXY_TARGET="${TEST_FILES_PROXY_TARGET}"
        export VITE_ALLOWED_HOSTS="${DOC_AGENT_TEST_PUBLIC_HOST}"
        export VITE_API_FORWARDED_HOST="${DOC_AGENT_TEST_PUBLIC_HOST}:${TEST_FMCA_FRONTEND_PORT}"
        export VITE_API_FORWARDED_PROTO=http
        start_process fmca-frontend "${REPO_ROOT}/frontend_fmca" \
          "${REPO_ROOT}/frontend_fmca/node_modules/.bin/vite" \
          --host 0.0.0.0 --port "${TEST_FMCA_FRONTEND_PORT}" --strictPort
        ;;
      landslide-frontend)
        export VITE_API_PROXY_TARGET="http://127.0.0.1:${TEST_AGENT_PORT}"
        export VITE_KNOWLEDGE_API_PROXY_TARGET="http://127.0.0.1:${TEST_KNOWLEDGE_PORT}"
        export VITE_FILES_PROXY_TARGET="${TEST_FILES_PROXY_TARGET}"
        export VITE_ALLOWED_HOSTS="${DOC_AGENT_TEST_PUBLIC_HOST}"
        start_process landslide-frontend "${REPO_ROOT}/frontend_landslide" \
          "${REPO_ROOT}/frontend_landslide/node_modules/.bin/vite" \
          --host 0.0.0.0 --port "${TEST_LANDSLIDE_FRONTEND_PORT}" --strictPort
        ;;
      guest-frontend)
        export GUEST_BACKEND_URL="http://127.0.0.1:${TEST_GUEST_API_PORT}"
        start_process guest-frontend "${REPO_ROOT}/guest" \
          "${REPO_ROOT}/guest/node_modules/.bin/vite" \
          --host 0.0.0.0 --port "${TEST_GUEST_FRONTEND_PORT}" --strictPort
        ;;
    esac
  done
}

stop_apps() {
  select_services "$@"
  load_config
  local name
  for name in "${STOP_SERVICES[@]}"; do
    service_is_selected "${name}" || continue
    stop_process "${name}"
  done
}

wait_http() {
  local name="$1" url="$2" deadline=$((SECONDS + 240))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error --max-time 8 "${url}" >/dev/null 2>&1; then
      log "${name} 已就绪：${url}"
      return
    fi
    sleep 2
  done
  fail "${name} 未在 240 秒内就绪：${url}"
}

wait_apps() {
  select_services "$@"
  load_config
  local name
  for name in "${ALL_SERVICES[@]}"; do
    service_is_selected "${name}" || continue
    case "${name}" in
      agent-api)
        wait_http agent-live "http://127.0.0.1:${TEST_AGENT_PORT}/live"
        wait_http agent-health "http://127.0.0.1:${TEST_AGENT_PORT}/health"
        ;;
      knowledge-api)
        [[ "${TEST_ENABLE_KNOWLEDGE_API}" == "true" ]] \
          || fail "当前拓扑要求启用 Knowledge API"
        wait_http knowledge-health "http://127.0.0.1:${TEST_KNOWLEDGE_PORT}/health"
        ;;
      guest-api)
        wait_http guest-health "http://127.0.0.1:${TEST_GUEST_API_PORT}/health"
        ;;
      frontend)
        wait_http frontend "http://127.0.0.1:${TEST_FRONTEND_PORT}/"
        ;;
      csh-frontend)
        wait_http csh-frontend "http://127.0.0.1:${TEST_CSH_FRONTEND_PORT}/"
        ;;
      fmca-frontend)
        wait_http fmca-frontend "http://127.0.0.1:${TEST_FMCA_FRONTEND_PORT}/"
        ;;
      landslide-frontend)
        wait_http landslide-frontend "http://127.0.0.1:${TEST_LANDSLIDE_FRONTEND_PORT}/"
        ;;
      guest-frontend)
        wait_http guest-frontend "http://127.0.0.1:${TEST_GUEST_FRONTEND_PORT}/"
        ;;
    esac
  done
}

assert_http() {
  local name="$1" url="$2"
  curl --fail --silent --show-error --max-time 8 "${url}" >/dev/null \
    || fail "${name} 健康检查失败：${url}"
  log "${name} 健康：${url}"
}

assert_process() {
  local name="$1" port="$2"
  pid_is_owned "${name}" || fail "${name} 没有由测试 runtime 单元托管"
  port_is_listening "${port}" || fail "${name} 测试端口 ${port} 未监听"
}

assert_apps_running() {
  select_services "$@"
  load_config
  local name
  for name in "${ALL_SERVICES[@]}"; do
    service_is_selected "${name}" || continue
    assert_process "${name}" "$(service_port "${name}")"
    case "${name}" in
      agent-api)
        assert_http agent-live "http://127.0.0.1:${TEST_AGENT_PORT}/live"
        assert_http agent-health "http://127.0.0.1:${TEST_AGENT_PORT}/health"
        ;;
      knowledge-api)
        [[ "${TEST_ENABLE_KNOWLEDGE_API}" == "true" ]] \
          || fail "当前拓扑要求启用 Knowledge API"
        assert_http knowledge-health "http://127.0.0.1:${TEST_KNOWLEDGE_PORT}/health"
        ;;
      guest-api)
        assert_http guest-health "http://127.0.0.1:${TEST_GUEST_API_PORT}/health"
        ;;
      frontend)
        assert_http frontend "http://127.0.0.1:${TEST_FRONTEND_PORT}/"
        ;;
      csh-frontend)
        assert_http csh-frontend "http://127.0.0.1:${TEST_CSH_FRONTEND_PORT}/"
        ;;
      fmca-frontend)
        assert_http fmca-frontend "http://127.0.0.1:${TEST_FMCA_FRONTEND_PORT}/"
        ;;
      landslide-frontend)
        assert_http landslide-frontend "http://127.0.0.1:${TEST_LANDSLIDE_FRONTEND_PORT}/"
        ;;
      guest-frontend)
        assert_http guest-frontend "http://127.0.0.1:${TEST_GUEST_FRONTEND_PORT}/"
        ;;
    esac
  done
}

assert_current_services() {
  (($# > 0)) || fail "assert-current 至少需要一个服务名"
  select_services "$@"
  load_config
  local expected_sha name pid actual_sha
  expected_sha="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
  for name in "${SELECTED_SERVICES[@]}"; do
    assert_process "${name}" "$(service_port "${name}")"
    pid="$(<"${PID_DIR}/${name}.pid")"
    actual_sha="$(pid_release_sha "${pid}")" \
      || fail "无法识别 ${name} PID ${pid} 的 release"
    [[ "${actual_sha}" == "${expected_sha}" ]] \
      || fail "${name} 仍运行 ${actual_sha}，期望 ${expected_sha}"
  done
  log "受影响服务已全部运行当前 release ${expected_sha}"
}

reload_selected() {
  local plan="${1:-${RESTART_PLAN}}" owner mode candidate
  [[ -f "${plan}" && ! -L "${plan}" ]] || fail "增量重启计划不存在或不是普通文件：${plan}"
  owner="$(stat -c '%u' "${plan}")"
  mode="$(stat -c '%a' "${plan}")"
  [[ "${owner}" == "$(id -u)" ]] || fail "增量重启计划所有者不正确"
  (( (8#${mode} & 8#077) == 0 )) || fail "增量重启计划不能授予组/其他用户权限"

  local -a planned=()
  mapfile -t planned <"${plan}"
  if ((${#planned[@]} == 0)); then
    log "增量重启计划为空，不重启业务服务"
    return
  fi
  for candidate in "${planned[@]}"; do
    [[ "${candidate}" =~ ^[a-z0-9-]+$ ]] \
      || fail "增量重启计划包含空行、空白或非法服务名"
  done
  select_services "${planned[@]}"
  log "按影响范围重启：${SELECTED_SERVICES[*]}"
  stop_apps "${SELECTED_SERVICES[@]}"
  start_apps "${SELECTED_SERVICES[@]}"
  wait_apps "${SELECTED_SERVICES[@]}"
  assert_current_services "${SELECTED_SERVICES[@]}"
}

status() {
  load_config
  local name pid sha
  for name in "${ALL_SERVICES[@]}"; do
    if pid_is_owned "${name}"; then
      pid="$(<"${PID_DIR}/${name}.pid")"
      sha="$(pid_release_sha "${pid}")"
      printf '%-20s running pid=%s release=%s\n' "${name}" "${pid}" "${sha}"
    else
      printf '%-20s stopped\n' "${name}"
    fi
  done
}

run_smoke() {
  load_config
  export DOC_AGENT_TEST_RELEASE_SHA
  DOC_AGENT_TEST_RELEASE_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
  "${REPO_ROOT}/.venv/bin/python" \
    "${REPO_ROOT}/deploy/local-test/smoke_test.py"
}

up() {
  preflight
  prepare
  check_external
  migrate_state
  start_apps
  wait_apps
  run_smoke
}

case "${1:-}" in
  init-config) init_config ;;
  preflight) preflight ;;
  prepare) prepare ;;
  check-external) check_external ;;
  migrate-state) migrate_state ;;
  force-migrate-state) force_migrate_state ;;
  verify-state) verify_state ;;
  start-apps) shift; start_apps "$@" ;;
  stop-apps | down) shift; stop_apps "$@" ;;
  wait) shift; wait_apps "$@" ;;
  assert-running) shift; assert_apps_running "$@" ;;
  assert-current) shift; assert_current_services "$@" ;;
  reload-selected) reload_selected "${2:-}" ;;
  up) up ;;
  status) status ;;
  smoke) run_smoke ;;
  *)
    cat <<'EOF'
Usage: deploy/local-test/manage.sh COMMAND

Commands:
  init-config         Copy existing external-service settings and add safe overrides.
  preflight           Verify test ports, safety switches and current-service baseline.
  prepare             Install locked Python/frontend dependencies for this release.
  check-external      Run read-only checks against existing shared infrastructure.
  migrate-state       Snapshot current checkpoints and MySQL conversation catalog once.
  force-migrate-state Refresh both isolated state stores after stopping test APIs.
  verify-state        Verify checkpoint/catalog integrity and baseline row counts.
  start-apps [NAME]   Start all services, or only the named test services.
  stop-apps/down [NAME]
                      Stop all services, or only the named test services.
  wait [NAME]         Wait for all endpoints, or only the named services.
  assert-running [NAME]
                      Verify all services, or only the named services.
  assert-current NAME Verify named services run from this release.
  reload-selected [PLAN]
                      Restart only services listed in the owner-only plan file.
  up                  Prepare, verify, migrate, start and smoke-test the full safe flow.
  status              Show test-only process state.
  smoke               Re-run safe proxy/auth/chat/checkpoint tests.
EOF
    exit 2
    ;;
esac
