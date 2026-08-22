#!/usr/bin/env bash
set -Eeuo pipefail

CALLER_REPO_ROOT="$(git rev-parse --show-toplevel)"
RUNTIME_ROOT="${DOC_AGENT_TEST_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
SOURCE_GIT_DIR="${RUNTIME_ROOT}/source.git"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
CURRENT_LINK="${RUNTIME_ROOT}/current"
STATE_DIR="${RUNTIME_ROOT}/state"
TARGET_BRANCH="${DOC_AGENT_TEST_BRANCH:-dev}"
REMOTE_URL="${DOC_AGENT_TEST_REMOTE_URL:-$(git -C "${CALLER_REPO_ROOT}" remote get-url origin)}"
RUNTIME_UNIT="${DOC_AGENT_TEST_RUNTIME_UNIT:-doc-agent-test-runtime.service}"
RESTART_PLAN="${STATE_DIR}/restart-plan"
IMPACT_REPORT="${STATE_DIR}/last-service-impact.json"
TEST_PIDS_BEFORE="${STATE_DIR}/test-pids.before"
TEST_PIDS_AFTER="${STATE_DIR}/test-pids.after"
PROTECTED_PORTS=(5173 5174 5176 5177 8082 8083 8086)
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

log() {
  printf '[doc-agent-test-release] %s\n' "$*"
}

fail() {
  printf '[doc-agent-test-release] ERROR: %s\n' "$*" >&2
  exit 1
}

capture_protected_services() {
  local output_file="$1" port
  : >"${output_file}"
  for port in "${PROTECTED_PORTS[@]}"; do
    ss -H -ltnp "sport = :${port}" >>"${output_file}" || true
  done
  sort -o "${output_file}" "${output_file}"
}

assert_protected_unchanged() {
  local before="$1" after="$2"
  capture_protected_services "${after}"
  if ! cmp --silent "${before}" "${after}"; then
    diff -u "${before}" "${after}" >&2 || true
    fail "现有服务监听端口或 PID 发生变化"
  fi
  log "现有服务端口与 PID 基线保持不变"
}

install_runtime_manager() {
  local release_dir="$1" temporary="${RUNTIME_ROOT}/bin/.manage.sh.$$"
  [[ -x "${release_dir}/deploy/local-test/manage.sh" ]] \
    || fail "目标 release 缺少 runtime 管理器"
  bash -n "${release_dir}/deploy/local-test/manage.sh"
  install -d -m 0700 "${RUNTIME_ROOT}/bin"
  install -m 0755 \
    "${release_dir}/deploy/local-test/manage.sh" \
    "${temporary}"
  mv -f "${temporary}" "${RUNTIME_ROOT}/bin/manage.sh"
}

write_all_restart_plan() {
  local temporary="${RESTART_PLAN}.tmp.$$" name
  umask 077
  : >"${temporary}"
  for name in "${ALL_SERVICES[@]}"; do
    printf '%s\n' "${name}" >>"${temporary}"
  done
  chmod 600 "${temporary}"
  mv -f "${temporary}" "${RESTART_PLAN}"
}

calculate_restart_plan() {
  local old_release="$1" new_release="$2" old_sha new_sha
  new_sha="$(git -C "${new_release}" rev-parse HEAD)"
  if [[ -z "${old_release}" ]]; then
    write_all_restart_plan
    log "首次启动测试 runtime：计划启动全部服务"
    return
  fi
  old_sha="$(git -C "${old_release}" rev-parse HEAD)"
  python3 "${new_release}/deploy/local-test/changed_services.py" \
    --git-dir "${SOURCE_GIT_DIR}" \
    --plan "${RESTART_PLAN}" \
    --report "${IMPACT_REPORT}" \
    "${old_sha}" \
    "${new_sha}"
  local -a services=()
  mapfile -t services <"${RESTART_PLAN}"
  if ((${#services[@]} == 0)); then
    log "Git 差异不影响运行中服务：本次只切换 current 并执行验证"
  else
    log "受影响服务：${services[*]}"
  fi
}

capture_test_pids() {
  local output_file="$1" name pid_file pid
  local temporary="${output_file}.tmp.$$"
  umask 077
  : >"${temporary}"
  for name in "${ALL_SERVICES[@]}"; do
    pid_file="${STATE_DIR}/pids/${name}.pid"
    [[ -f "${pid_file}" ]] || continue
    pid="$(<"${pid_file}")"
    [[ "${pid}" =~ ^[0-9]+$ ]] || continue
    kill -0 "${pid}" 2>/dev/null || continue
    printf '%s %s\n' "${name}" "${pid}" >>"${temporary}"
  done
  chmod 600 "${temporary}"
  mv -f "${temporary}" "${output_file}"
}

pid_from_snapshot() {
  local name="$1" snapshot="$2"
  awk -v expected="${name}" '$1 == expected { print $2; exit }' "${snapshot}"
}

restart_plan_contains() {
  grep -Fxq "$1" "${RESTART_PLAN}"
}

assert_incremental_pid_impact() {
  local before="$1" after="$2" name before_pid after_pid
  for name in "${ALL_SERVICES[@]}"; do
    before_pid="$(pid_from_snapshot "${name}" "${before}")"
    after_pid="$(pid_from_snapshot "${name}" "${after}")"
    [[ -n "${after_pid}" ]] || fail "部署后 ${name} 没有运行 PID"
    if restart_plan_contains "${name}"; then
      if [[ -n "${before_pid}" && "${before_pid}" == "${after_pid}" ]]; then
        fail "受影响服务 ${name} 未更换 PID"
      fi
    elif [[ "${before_pid}" != "${after_pid}" ]]; then
      fail "未受影响服务 ${name} 的 PID 意外变化：${before_pid:-none} -> ${after_pid}"
    fi
  done
  log "PID 影响范围验证通过：仅计划内服务发生重启"
}

ensure_source_repo() {
  mkdir -p "${RUNTIME_ROOT}" "${RELEASES_DIR}" "${STATE_DIR}"
  if [[ ! -d "${SOURCE_GIT_DIR}" ]]; then
    log "首次从远端克隆代码仓库（凭据由 Git 交互读取，不写入代码或配置）"
    git clone --bare --branch "${TARGET_BRANCH}" "${REMOTE_URL}" "${SOURCE_GIT_DIR}"
  fi
  git --git-dir="${SOURCE_GIT_DIR}" remote set-url origin "${REMOTE_URL}"
  git --git-dir="${SOURCE_GIT_DIR}" fetch \
    --prune \
    origin \
    "+refs/heads/${TARGET_BRANCH}:refs/heads/${TARGET_BRANCH}"
}

materialize_release() {
  local sha="$1" release_dir="${RELEASES_DIR}/$1"
  if [[ ! -d "${release_dir}" ]]; then
    git --git-dir="${SOURCE_GIT_DIR}" worktree prune
    git --git-dir="${SOURCE_GIT_DIR}" worktree add \
      --detach \
      "${release_dir}" \
      "${sha}" 1>&2
  fi
  printf '%s' "${release_dir}"
}

switch_current() {
  local release_dir="$1" tmp_link="${RUNTIME_ROOT}/.current.$$"
  ln -s "${release_dir}" "${tmp_link}"
  mv -Tf "${tmp_link}" "${CURRENT_LINK}"
}

remove_current_link() {
  if [[ -L "${CURRENT_LINK}" ]]; then
    unlink "${CURRENT_LINK}"
  fi
}

assert_runtime_unit_installed() {
  command -v systemctl >/dev/null || fail "缺少 systemctl，无法托管测试进程"
  systemctl --user cat "${RUNTIME_UNIT}" >/dev/null 2>&1 \
    || fail "缺少 ${RUNTIME_UNIT}；请先安装 deploy/local-test/systemd 下的用户单元"
}

assert_runtime_unit_reloadable() {
  assert_runtime_unit_installed
  [[ "$(systemctl --user show "${RUNTIME_UNIT}" -p CanReload --value)" == "yes" ]] \
    || fail "${RUNTIME_UNIT} 尚未安装增量重载配置，请先运行 install-user-units.sh"
}

start_runtime() {
  assert_runtime_unit_installed
  systemctl --user start "${RUNTIME_UNIT}"
}

reload_runtime() {
  assert_runtime_unit_reloadable
  systemctl --user reload "${RUNTIME_UNIT}"
}

stop_runtime() {
  local release_dir="$1"
  assert_runtime_unit_installed
  if systemctl --user is-active --quiet "${RUNTIME_UNIT}"; then
    systemctl --user stop "${RUNTIME_UNIT}"
  elif [[ -x "${RUNTIME_ROOT}/bin/manage.sh" ]]; then
    "${RUNTIME_ROOT}/bin/manage.sh" stop-apps
  else
    "${release_dir}/deploy/local-test/manage.sh" stop-apps
  fi
}

run_smoke() {
  local release_dir="$1"
  set -a
  # shellcheck disable=SC1090
  source "${RUNTIME_ROOT}/config/runtime.env"
  set +a
  export DOC_AGENT_TEST_RELEASE_SHA
  DOC_AGENT_TEST_RELEASE_SHA="$(git -C "${release_dir}" rev-parse HEAD)"
  "${release_dir}/.venv/bin/python" \
    "${release_dir}/deploy/local-test/smoke_test.py"
}

validate_running_release() {
  local release_dir="$1"
  local before_file="${STATE_DIR}/protected.before" after_file="${STATE_DIR}/protected.after"
  capture_protected_services "${before_file}"
  "${release_dir}/deploy/local-test/manage.sh" init-config
  "${release_dir}/deploy/local-test/manage.sh" preflight
  "${release_dir}/deploy/local-test/manage.sh" check-external
  "${release_dir}/deploy/local-test/manage.sh" verify-state
  assert_runtime_unit_installed
  systemctl --user is-active --quiet "${RUNTIME_UNIT}" \
    || fail "${RUNTIME_UNIT} 未运行"
  "${release_dir}/deploy/local-test/manage.sh" assert-running
  run_smoke "${release_dir}"
  assert_protected_unchanged "${before_file}" "${after_file}"
}

deploy_release() {
  local release_dir="$1" old_release="${2:-}"
  local before_file="${STATE_DIR}/protected.before" after_file="${STATE_DIR}/protected.after"
  local runtime_was_active=false switched=false full_start=false
  local -a restart_services=()

  capture_protected_services "${before_file}"
  "${release_dir}/deploy/local-test/manage.sh" init-config
  "${release_dir}/deploy/local-test/manage.sh" prepare
  assert_runtime_unit_installed

  "${release_dir}/deploy/local-test/manage.sh" preflight
  "${release_dir}/deploy/local-test/manage.sh" check-external
  "${release_dir}/deploy/local-test/manage.sh" migrate-state
  "${release_dir}/deploy/local-test/manage.sh" verify-state
  calculate_restart_plan "${old_release}" "${release_dir}"
  install_runtime_manager "${release_dir}"
  if systemctl --user is-active --quiet "${RUNTIME_UNIT}"; then
    runtime_was_active=true
    if [[ -s "${RESTART_PLAN}" ]]; then
      assert_runtime_unit_reloadable
    fi
  else
    write_all_restart_plan
    full_start=true
    log "测试 runtime 当前未运行：本次必须启动全部服务"
  fi
  capture_test_pids "${TEST_PIDS_BEFORE}"

  rollback() {
    local exit_code=$? rollback_failed=false
    trap - ERR
    log "部署未完成，按相同影响范围恢复上一 test release"
    if [[ "${switched}" == "true" && -n "${old_release}" && -d "${old_release}" ]]; then
      switch_current "${old_release}"
      if ! "${RUNTIME_ROOT}/bin/manage.sh" init-config; then
        log "ERROR: 恢复旧 release 配置失败"
        rollback_failed=true
      fi
      if [[ "${runtime_was_active}" == "true" && -s "${RESTART_PLAN}" ]]; then
        if ! reload_runtime; then
          log "ERROR: 受影响服务回滚重载失败；未扩大到其他服务"
          rollback_failed=true
        fi
        mapfile -t restart_services <"${RESTART_PLAN}"
        if ((${#restart_services[@]} > 0)); then
          if ! "${RUNTIME_ROOT}/bin/manage.sh" assert-current \
            "${restart_services[@]}"; then
            log "ERROR: 回滚后受影响服务未全部恢复旧 release"
            rollback_failed=true
          fi
        fi
      elif [[ "${full_start}" == "true" ]]; then
        if ! systemctl --user stop "${RUNTIME_UNIT}"; then
          log "ERROR: 恢复部署前的停止状态失败"
          rollback_failed=true
        fi
      fi
    elif [[ "${switched}" == "true" ]]; then
      if ! systemctl --user stop "${RUNTIME_UNIT}"; then
        log "ERROR: 首次部署失败后停止测试 runtime 失败"
        rollback_failed=true
      fi
      remove_current_link
    fi
    if ! assert_protected_unchanged "${before_file}" "${after_file}"; then
      rollback_failed=true
    fi
    if [[ "${rollback_failed}" == "false" ]]; then
      : >"${RESTART_PLAN}"
      chmod 600 "${RESTART_PLAN}"
    else
      log "ERROR: 回滚未完全通过，保留 owner-only 重启计划供人工复核"
    fi
    exit "${exit_code}"
  }
  trap rollback ERR

  switch_current "${release_dir}"
  switched=true
  if [[ "${runtime_was_active}" == "true" ]]; then
    if [[ -s "${RESTART_PLAN}" ]]; then
      reload_runtime
    else
      log "没有受影响的运行中服务，PID 全部保持不变"
    fi
  else
    start_runtime
  fi
  "${RUNTIME_ROOT}/bin/manage.sh" assert-running
  if [[ -s "${RESTART_PLAN}" ]]; then
    mapfile -t restart_services <"${RESTART_PLAN}"
    "${RUNTIME_ROOT}/bin/manage.sh" assert-current "${restart_services[@]}"
  fi
  capture_test_pids "${TEST_PIDS_AFTER}"
  assert_incremental_pid_impact "${TEST_PIDS_BEFORE}" "${TEST_PIDS_AFTER}"
  run_smoke "${release_dir}"
  assert_protected_unchanged "${before_file}" "${after_file}"
  trap - ERR
  : >"${RESTART_PLAN}"
  chmod 600 "${RESTART_PLAN}"
  log "部署成功：$(git -C "${release_dir}" rev-parse HEAD)"
}

update_release() {
  local expected_sha="${1:-}"
  if [[ -n "${expected_sha}" && ! "${expected_sha}" =~ ^[0-9a-f]{40}$ ]]; then
    fail "update 的期望提交必须是完整的 40 位 SHA"
  fi
  ensure_source_repo
  local sha release_dir old_release=""
  sha="$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse "refs/heads/${TARGET_BRANCH}")"
  if [[ -n "${expected_sha}" && "${sha}" != "${expected_sha}" ]]; then
    fail "origin/${TARGET_BRANCH} 已从 ${expected_sha} 移动到 ${sha}，等待下一次检查"
  fi
  release_dir="$(materialize_release "${sha}")"
  if [[ -L "${CURRENT_LINK}" ]]; then
    old_release="$(readlink -f "${CURRENT_LINK}")"
  fi
  if [[ "${release_dir}" == "${old_release}" ]]; then
    log "当前已经是 ${sha}，只执行检查和业务冒烟，不重启任何服务"
    capture_test_pids "${TEST_PIDS_BEFORE}"
    validate_running_release "${release_dir}"
    capture_test_pids "${TEST_PIDS_AFTER}"
    cmp --silent "${TEST_PIDS_BEFORE}" "${TEST_PIDS_AFTER}" \
      || fail "无代码更新时测试服务 PID 发生变化"
    log "无代码更新验证通过：全部测试服务 PID 保持不变"
    return
  fi
  deploy_release "${release_dir}" "${old_release}"
}

rollback_release() {
  local sha="${1:-}"
  [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || fail "rollback 需要完整的 40 位提交 SHA"
  local release_dir="${RELEASES_DIR}/${sha}" old_release=""
  [[ -d "${release_dir}" ]] || fail "本机不存在 release ${sha}"
  if [[ -L "${CURRENT_LINK}" ]]; then
    old_release="$(readlink -f "${CURRENT_LINK}")"
  fi
  if [[ "${release_dir}" == "${old_release}" ]]; then
    validate_running_release "${release_dir}"
    return
  fi
  deploy_release "${release_dir}" "${old_release}"
}

main() {
  case "${1:-}" in
    update) update_release "${2:-}" ;;
    rollback) rollback_release "${2:-}" ;;
    status)
      [[ -L "${CURRENT_LINK}" ]] || fail "尚未部署 current release"
      "${CURRENT_LINK}/deploy/local-test/manage.sh" status
      ;;
    smoke)
      [[ -L "${CURRENT_LINK}" ]] || fail "尚未部署 current release"
      validate_running_release "$(readlink -f "${CURRENT_LINK}")"
      ;;
    down)
      [[ -L "${CURRENT_LINK}" ]] || fail "尚未部署 current release"
      stop_runtime "$(readlink -f "${CURRENT_LINK}")"
      ;;
    *)
      cat <<'EOF'
Usage: deploy/local-test/release.sh COMMAND

Commands:
  update [SHA]    Fetch origin/dev and deploy it; optional SHA rejects a moved ref.
  rollback SHA    Deploy an already prepared full-SHA release.
  status          Show current isolated test-only application state.
  smoke           Re-run read-only external checks and the full business smoke.
  down            Stop the isolated test runtime unit and retain test data.
EOF
      exit 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
