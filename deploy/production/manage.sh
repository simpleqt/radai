#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
CURRENT="${RUNTIME_ROOT}/current"
TARGET="doc-agent-production.target"
API_UNITS=(
  doc-agent-prod-knowledge-api.service
  doc-agent-prod-agent-api.service
  doc-agent-prod-guest-api.service
)
FRONTEND_UNITS=(
  doc-agent-prod-frontend-5173.service
  doc-agent-prod-frontend-5177.service
  doc-agent-prod-frontend-5174.service
  doc-agent-prod-frontend-5176.service
)
WORKER_UNITS=(
  doc-agent-prod-task-worker.service
  doc-agent-prod-ingest-worker.service
)
ALL_UNITS=("${API_UNITS[@]}" "${FRONTEND_UNITS[@]}" "${WORKER_UNITS[@]}")
STOP_UNITS=("${FRONTEND_UNITS[@]}" "${WORKER_UNITS[@]}" "${API_UNITS[@]}")
SELECTED_UNITS=()

log() {
  printf '[production-manage] %s\n' "$*"
}

fail() {
  printf '[production-manage] ERROR: %s\n' "$*" >&2
  exit 1
}

require_full_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]] || fail "必须提供完整的 40 位提交 SHA"
}

release_path() {
  require_full_sha "$1"
  printf '%s/releases/%s\n' "${RUNTIME_ROOT}" "$1"
}

validate_release() {
  local sha="$1" release
  release="$(release_path "${sha}")"
  [[ -d "${release}" ]] || fail "release 未准备：${release}"
  [[ "$(git -C "${release}" rev-parse HEAD)" == "${sha}" ]] \
    || fail "release 目录与目标 SHA 不一致"
  [[ -x "${release}/.venv/bin/python" ]] || fail "release 缺少 Python 运行环境"
  [[ -L "${release}/backend/.env" ]] || fail "release 缺少稳定 backend/.env 链接"
  local frontend
  for frontend in frontend frontend_csh_ai frontend_fmca; do
    [[ -x "${release}/${frontend}/node_modules/.bin/vite" ]] \
      || fail "release 缺少 ${frontend} 运行依赖"
  done
  if [[ -f "${release}/frontend_audit/package.json" ]]; then
    [[ -x "${release}/frontend_audit/node_modules/.bin/vite" ]] \
      || fail "release 缺少 frontend_audit 运行依赖"
  else
    log "目标 release 早于独立审计前端，5177 将仅用于同集合自动回滚兼容"
  fi
}

current_release() {
  [[ -L "${CURRENT}" ]] || fail "production current release 不存在"
  local release
  release="$(readlink -f "${CURRENT}")"
  case "${release}" in
    "${RUNTIME_ROOT}/releases"/*) ;;
    *) fail "production current 未指向隔离 releases 目录" ;;
  esac
  printf '%s\n' "${release}"
}

atomic_point_current() {
  local release="$1" temporary="${RUNTIME_ROOT}/.current.${BASHPID}"
  [[ "${release}" == "${RUNTIME_ROOT}/releases/"* ]] \
    || fail "拒绝将 current 指向正式 releases 目录以外"
  [[ -d "${release}" ]] || fail "目标 release 目录不存在"
  [[ ! -e "${temporary}" && ! -L "${temporary}" ]] \
    || fail "临时 current 链接已存在"
  ln -s "${release}" "${temporary}"
  mv -Tf "${temporary}" "${CURRENT}"
}

known_unit() {
  local expected="$1" unit
  for unit in "${ALL_UNITS[@]}"; do
    [[ "${expected}" != "${unit}" ]] || return 0
  done
  return 1
}

load_plan() {
  local plan="$1" unit existing mode mode_value
  [[ -f "${plan}" ]] || fail "重启计划不存在：${plan}"
  mode="$(stat -c '%a' "${plan}")"
  mode_value=$((8#${mode}))
  (( (mode_value & 077) == 0 )) || fail "重启计划不能允许 group/other 读取"
  SELECTED_UNITS=()
  while IFS= read -r unit || [[ -n "${unit}" ]]; do
    [[ -n "${unit}" ]] || continue
    known_unit "${unit}" || fail "重启计划包含未知正式单元：${unit}"
    for existing in "${SELECTED_UNITS[@]}"; do
      [[ "${existing}" != "${unit}" ]] || fail "重启计划包含重复单元：${unit}"
    done
    SELECTED_UNITS+=("${unit}")
  done <"${plan}"
}

unit_is_selected() {
  local expected="$1" unit
  for unit in "${SELECTED_UNITS[@]}"; do
    [[ "${expected}" != "${unit}" ]] || return 0
  done
  return 1
}

wait_http() {
  local port="$1" path="$2" timeout="$3" deadline code=000
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --connect-timeout 3 --max-time 10 \
      "http://127.0.0.1:${port}${path}" 2>/dev/null || true)"
    if [[ "${code}" == "200" ]]; then
      log "端口 ${port}${path} 已就绪（HTTP 200）"
      return 0
    fi
    sleep 2
  done
  printf '[production-manage] 端口 %s%s 未在 %ss 内就绪，最后 HTTP=%s\n' \
    "${port}" "${path}" "${timeout}" "${code}" >&2
  return 1
}

wait_unit_active() {
  local unit="$1" timeout="$2" deadline
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    systemctl --user is-active --quiet "${unit}" && return 0
    sleep 1
  done
  printf '[production-manage] 单元未在 %ss 内 active：%s\n' \
    "${timeout}" "${unit}" >&2
  return 1
}

wait_unit_ready() {
  local unit="$1"
  wait_unit_active "${unit}" 30 || return 1
  case "${unit}" in
    doc-agent-prod-knowledge-api.service) wait_http 8086 /health 600 ;;
    doc-agent-prod-agent-api.service) wait_http 8082 /health 180 ;;
    doc-agent-prod-guest-api.service) wait_http 8083 /health 180 ;;
    doc-agent-prod-frontend-5173.service) wait_http 5173 / 120 ;;
    doc-agent-prod-frontend-5177.service) wait_http 5177 /audit 120 ;;
    doc-agent-prod-frontend-5174.service) wait_http 5174 / 120 ;;
    doc-agent-prod-frontend-5176.service) wait_http 5176 / 120 ;;
    doc-agent-prod-task-worker.service|doc-agent-prod-ingest-worker.service) return 0 ;;
    *) return 1 ;;
  esac
}

check_health() {
  local port path code failed=0
  for port in 8082 8083 8086; do
    path=/health
    code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --connect-timeout 3 --max-time 10 \
      "http://127.0.0.1:${port}${path}" 2>/dev/null || true)"
    printf '%s%s HTTP %s\n' "${port}" "${path}" "${code}"
    [[ "${code}" == "200" ]] || failed=1
  done
  for port in 5173 5174 5176 5177; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' \
      --connect-timeout 3 --max-time 10 \
      "http://127.0.0.1:${port}/" 2>/dev/null || true)"
    printf '%s/ HTTP %s\n' "${port}" "${code}"
    [[ "${code}" == "200" ]] || failed=1
  done
  return "${failed}"
}

assert_running() {
  local unit
  for unit in "${ALL_UNITS[@]}"; do
    if ! systemctl --user is-active --quiet "${unit}"; then
      printf '[production-manage] 正式单元未运行：%s\n' "${unit}" >&2
      return 1
    fi
  done
  if ! check_health; then
    printf '[production-manage] 正式本机健康检查未全部通过\n' >&2
    return 1
  fi
}

restart_selected() {
  local unit
  if ((${#SELECTED_UNITS[@]} == 0)); then
    log "代码差异不影响正式业务单元，本次不重启服务"
    return 0
  fi
  for unit in "${ALL_UNITS[@]}"; do
    unit_is_selected "${unit}" || continue
    log "重启受影响单元 ${unit}"
    systemctl --user restart "${unit}" || return 1
    wait_unit_ready "${unit}" || return 1
  done
}

activate_selected() {
  restart_selected && assert_running
}

switch_selected() {
  local sha="$1" plan="$2" release old_release
  validate_release "${sha}"
  load_plan "${plan}"
  release="$(release_path "${sha}")"
  old_release="$(current_release)"
  assert_running || fail "切换前正式服务基线不健康"
  if [[ "${old_release}" == "${release}" ]]; then
    log "current 已是 ${sha}，不重启任何服务"
    return 0
  fi

  atomic_point_current "${release}"
  if activate_selected; then
    log "增量切换成功：${sha}；仅重启 ${#SELECTED_UNITS[@]} 个受影响单元"
    return 0
  fi

  log "增量切换健康检查失败，current 回退并仅重启同一计划内单元"
  atomic_point_current "${old_release}"
  if activate_selected; then
    log "增量回滚完成：$(basename "${old_release}")"
    return 1
  fi
  fail "同集合自动回滚未恢复健康，必须人工处理"
}

rollback_selected() {
  local sha="$1" plan="$2" release current
  validate_release "${sha}"
  load_plan "${plan}"
  release="$(release_path "${sha}")"
  current="$(current_release)"

  # A rollback is entered specifically because the new release may already be
  # unhealthy.  Do not apply the normal pre-switch health gate here: point
  # current back first, then recycle exactly the original impact set.
  if [[ "${current}" != "${release}" ]]; then
    atomic_point_current "${release}"
  fi
  if activate_selected; then
    log "同集合回滚完成：${sha}；仅重启 ${#SELECTED_UNITS[@]} 个原计划单元"
    return 0
  fi
  fail "同集合回滚未恢复健康，必须人工处理"
}

start_services() {
  local sha
  sha="$(basename "$(current_release)")"
  validate_release "${sha}"
  systemctl --user daemon-reload || return 1
  log "启动三套正式 API"
  systemctl --user start "${API_UNITS[@]}" || return 1
  wait_http 8082 /health 180 || return 1
  wait_http 8083 /health 180 || return 1
  log "Agent 与 Guest 就绪，启动四套正式前端"
  systemctl --user start "${FRONTEND_UNITS[@]}" || return 1
  wait_http 5173 / 120 || return 1
  wait_http 5174 / 120 || return 1
  wait_http 5176 / 120 || return 1
  wait_http 5177 /audit 120 || return 1
  wait_http 8086 /health 600 || return 1
  log "Knowledge 就绪，启动两个 Worker"
  systemctl --user start "${WORKER_UNITS[@]}" || return 1
  assert_running
}

stop_services() {
  log "停止正式业务单元"
  systemctl --user stop "${STOP_UNITS[@]}" || true
}

switch_all() {
  local sha="$1" release old_release
  validate_release "${sha}"
  release="$(release_path "${sha}")"
  old_release="$(current_release)"
  stop_services
  atomic_point_current "${release}"
  if start_services; then
    log "全量应急切换成功：${sha}"
    return 0
  fi
  log "全量切换失败，执行全量应急回滚"
  stop_services
  atomic_point_current "${old_release}"
  start_services
  return 1
}

show_status() {
  local release unit pid cwd relative sha
  if [[ -L "${CURRENT}" ]]; then
    release="$(current_release)"
    printf 'current=%s\n' "${release}"
    printf 'current_sha=%s\n' "$(git -C "${release}" rev-parse HEAD)"
  else
    printf 'current=unset\n'
  fi
  for unit in "${ALL_UNITS[@]}"; do
    pid="$(systemctl --user show "${unit}" --property MainPID --value 2>/dev/null || true)"
    cwd=""
    sha="unknown"
    if [[ "${pid}" =~ ^[1-9][0-9]*$ ]]; then
      cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
      relative="${cwd#"${RUNTIME_ROOT}/releases/"}"
      if [[ "${relative}" != "${cwd}" && "${relative%%/*}" =~ ^[0-9a-f]{40}$ ]]; then
        sha="${relative%%/*}"
      fi
    fi
    printf 'unit=%s active=%s pid=%s sha=%s\n' \
      "${unit}" "$(systemctl --user is-active "${unit}" 2>/dev/null || true)" \
      "${pid:-0}" "${sha}"
  done
  check_health || true
}

case "${1:-}" in
  start) start_services ;;
  stop) stop_services ;;
  restart)
    stop_services
    start_services
    ;;
  switch|rollback)
    [[ -n "${2:-}" ]] || fail "用法：$0 $1 FULL_SHA"
    switch_all "$2"
    ;;
  switch-selected)
    [[ -n "${2:-}" && -n "${3:-}" ]] \
      || fail "用法：$0 $1 FULL_SHA OWNER_ONLY_PLAN"
    switch_selected "$2" "$3"
    ;;
  rollback-selected)
    [[ -n "${2:-}" && -n "${3:-}" ]] \
      || fail "用法：$0 $1 FULL_SHA OWNER_ONLY_PLAN"
    rollback_selected "$2" "$3"
    ;;
  assert-running) assert_running ;;
  health) check_health ;;
  status) show_status ;;
  *)
    printf '用法：%s {start|stop|restart|switch SHA|rollback SHA|switch-selected SHA PLAN|rollback-selected SHA PLAN|assert-running|health|status}\n' "$0" >&2
    exit 2
    ;;
esac
