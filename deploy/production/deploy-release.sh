#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
SOURCE_GIT="${RUNTIME_ROOT}/source.git"
CURRENT="${RUNTIME_ROOT}/current"
STATE_DIR="${RUNTIME_ROOT}/state"
REPORT_DIR="${RUNTIME_ROOT}/reports"
BRANCH="${DOC_AGENT_PRODUCTION_BRANCH:-production}"
CREDENTIAL_FILE="${DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_FILE:-${RUNTIME_ROOT}/config/git-auth/credentials}"
PREPARE_SCRIPT="${DOC_AGENT_PRODUCTION_PREPARE_SCRIPT:-${RUNTIME_ROOT}/bin/prepare-release.sh}"
MANAGER="${DOC_AGENT_PRODUCTION_MANAGER:-${RUNTIME_ROOT}/bin/manage.sh}"
IMPACT_MAPPER="${DOC_AGENT_PRODUCTION_IMPACT_MAPPER:-${RUNTIME_ROOT}/bin/changed_units.py}"
TRANSITION_VERIFIER="${DOC_AGENT_PRODUCTION_TRANSITION_VERIFIER:-${RUNTIME_ROOT}/bin/verify_unit_transition.py}"
PREFLIGHT_SCRIPT="${DOC_AGENT_PRODUCTION_PREFLIGHT_SCRIPT:-${RUNTIME_ROOT}/bin/preflight-release.sh}"
SMOKE_RELATIVE_PATH="deploy/production/production_smoke.py"
PUBLIC_URLS="${DOC_AGENT_PRODUCTION_PUBLIC_URLS:-https://radai2.cdut.edu.cn/ https://csh-ai.cdut.edu.cn/ https://fmca.cdut.edu.cn/}"
SUCCESS_SHA_FILE="${STATE_DIR}/production-deploy.success-sha"
FAILED_SHA_FILE="${STATE_DIR}/production-deploy.failed-sha"
LOCK_FILE="${STATE_DIR}/production-deploy.lock"
MODE="${1:-}"
REQUESTED_SHA="${2:-}"
ALL_UNITS=(
  doc-agent-prod-knowledge-api.service
  doc-agent-prod-agent-api.service
  doc-agent-prod-guest-api.service
  doc-agent-prod-frontend-5173.service
  doc-agent-prod-frontend-5177.service
  doc-agent-prod-frontend-5174.service
  doc-agent-prod-frontend-5176.service
  doc-agent-prod-task-worker.service
  doc-agent-prod-ingest-worker.service
)

target_sha=""
old_sha=""
plan_file=""
before_snapshot=""
rollback_needed=false

log() {
  printf '[production-deploy:%s] %s\n' "${MODE:-unknown}" "$*"
}

fail() {
  printf '[production-deploy:%s] ERROR: %s\n' "${MODE:-unknown}" "$*" >&2
  return 1
}

record_sha() {
  local output_file="$1" sha="$2" temporary_file="${1}.${BASHPID}"
  printf '%s\n' "${sha}" >"${temporary_file}"
  chmod 600 "${temporary_file}"
  mv -f "${temporary_file}" "${output_file}"
}

snapshot_units() {
  local output_file="$1" temporary_file="${1}.${BASHPID}" unit pid cwd
  : >"${temporary_file}"
  chmod 600 "${temporary_file}"
  for unit in "${ALL_UNITS[@]}"; do
    pid="$(systemctl --user show "${unit}" --property MainPID --value)"
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || fail "正式单元没有有效 MainPID：${unit}"
    cwd="$(readlink -f "/proc/${pid}/cwd")"
    case "${cwd}" in
      "${RUNTIME_ROOT}/releases"/*) ;;
      *) fail "正式单元未运行在不可变 release：${unit}" ;;
    esac
    printf '%s\t%s\t%s\n' "${unit}" "${pid}" "${cwd}" >>"${temporary_file}"
  done
  mv -f "${temporary_file}" "${output_file}"
}

verify_transition() {
  local before="$1" after="$2" plan="$3" expected_sha="$4"
  python3 "${TRANSITION_VERIFIER}" \
    --before "${before}" \
    --after "${after}" \
    --plan "${plan}" \
    --target-sha "${expected_sha}" \
    --runtime-root "${RUNTIME_ROOT}"
}

check_public_urls() {
  local url code failed=0
  for url in ${PUBLIC_URLS}; do
    code="$(curl -L -sS -o /dev/null -w '%{http_code}' \
      --connect-timeout 5 --max-time 30 --max-redirs 3 "${url}" 2>/dev/null || true)"
    if [[ "${code}" != "200" ]]; then
      # 瞬时公网抖动不应回滚一个业务冒烟全绿的新 release：隔 5s 重试一次。
      sleep 5
      code="$(curl -L -sS -o /dev/null -w '%{http_code}' \
        --connect-timeout 5 --max-time 30 --max-redirs 3 "${url}" 2>/dev/null || true)"
    fi
    log "正式公开地址 ${url} HTTP ${code}"
    [[ "${code}" == "200" ]] || failed=1
  done
  (( failed == 0 ))
}

on_error() {
  local status=$? line="$1" rollback_status=0 after_rollback=""
  trap - ERR
  set +e
  log "部署在脚本第 ${line} 行失败"
  if [[ -n "${target_sha}" && "${target_sha}" =~ ^[0-9a-f]{40}$ ]]; then
    record_sha "${FAILED_SHA_FILE}" "${target_sha}"
  fi
  if [[ "${rollback_needed}" == "true" ]]; then
    log "回滚 current，并且只重启原计划内单元"
    "${MANAGER}" rollback-selected "${old_sha}" "${plan_file}"
    rollback_status=$?
    if (( rollback_status == 0 )); then
      after_rollback="${STATE_DIR}/production-units-after-rollback.tsv"
      snapshot_units "${after_rollback}"
      rollback_status=$?
      if (( rollback_status == 0 )); then
        verify_transition \
          "${before_snapshot}" "${after_rollback}" "${plan_file}" "${old_sha}"
        rollback_status=$?
      fi
    fi
    if (( rollback_status == 0 )); then
      log "同集合自动回滚验证通过，正式 current 已恢复 ${old_sha}"
    else
      log "ERROR: 同集合自动回滚验证失败，需要立即人工处理"
    fi
  fi
  (( status != 0 )) || status=1
  exit "${status}"
}

trap 'on_error ${LINENO}' ERR INT TERM

[[ "${MODE}" == "manual" || "${MODE}" == "automatic" ]] \
  || { printf '用法：%s {manual|automatic} [PRODUCTION_TIP_FULL_SHA]\n' "$0" >&2; exit 2; }
[[ $# -le 2 ]] || { printf '参数过多\n' >&2; exit 2; }
[[ "${BRANCH}" == "production" ]] || fail "正式部署分支必须为 production"
[[ -d "${SOURCE_GIT}" ]] || fail "生产 bare Git 仓库不存在"
[[ -L "${CURRENT}" ]] || fail "生产 current release 不存在"
[[ -f "${CREDENTIAL_FILE}" && ! -L "${CREDENTIAL_FILE}" ]] \
  || fail "生产 Git 凭据必须是普通文件"
[[ "$(stat -c '%u' "${CREDENTIAL_FILE}")" == "$(id -u)" ]] \
  || fail "生产 Git 凭据文件所有者不正确"
[[ -x "${MANAGER}" ]] || fail "生产部署组件不可执行：${MANAGER}"

credential_mode="$(stat -c '%a' "${CREDENTIAL_FILE}")"
credential_mode_value=$((8#${credential_mode}))
(( (credential_mode_value & 077) == 0 )) \
  || fail "生产 Git 凭据文件不能允许 group/other 读取"

mkdir -p "${STATE_DIR}" "${REPORT_DIR}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "已有主动或定时部署正在运行，本次不重复执行"
  exit 0
fi

export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.helper
export GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1="store --file=${CREDENTIAL_FILE}"

log "获取唯一正式部署分支 origin/${BRANCH}"
git --git-dir="${SOURCE_GIT}" fetch \
  --prune \
  origin \
  "+refs/heads/${BRANCH}:refs/heads/${BRANCH}"
target_sha="$(git --git-dir="${SOURCE_GIT}" rev-parse "refs/heads/${BRANCH}")"
[[ "${target_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "production tip 不是完整 SHA"
if [[ -n "${REQUESTED_SHA}" ]]; then
  [[ "${REQUESTED_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "主动部署必须使用完整 SHA"
  [[ "${REQUESTED_SHA}" == "${target_sha}" ]] \
    || fail "主动部署 SHA 必须等于当前 origin/production tip"
fi

current_release="$(readlink -f "${CURRENT}")"
case "${current_release}" in
  "${RUNTIME_ROOT}/releases"/*) ;;
  *) fail "生产 current 未指向不可变 releases 目录" ;;
esac
old_sha="$(git -C "${current_release}" rev-parse HEAD)"
[[ "${old_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "当前正式 release SHA 无效"

if [[ "${old_sha}" == "${target_sha}" ]]; then
  "${MANAGER}" assert-running
  check_public_urls
  record_sha "${SUCCESS_SHA_FILE}" "${target_sha}"
  [[ ! -f "${FAILED_SHA_FILE}" ]] || unlink "${FAILED_SHA_FILE}"
  log "正式环境已经位于 origin/production ${target_sha}；健康检查通过，零服务重启"
  exit 0
fi

for executable in \
  "${PREPARE_SCRIPT}" \
  "${IMPACT_MAPPER}" \
  "${TRANSITION_VERIFIER}" \
  "${PREFLIGHT_SCRIPT}"; do
  [[ -x "${executable}" ]] || fail "生产部署组件不可执行：${executable}"
done
git --git-dir="${SOURCE_GIT}" merge-base --is-ancestor "${old_sha}" "${target_sha}" \
  || fail "origin/production 不是当前正式 SHA 的快进，拒绝自动覆盖或降级"

log "准备不可变正式 release ${target_sha}"
"${PREPARE_SCRIPT}" "${target_sha}"
target_release="${RUNTIME_ROOT}/releases/${target_sha}"
smoke_script="${target_release}/${SMOKE_RELATIVE_PATH}"
[[ -x "${target_release}/.venv/bin/python" ]] \
  || fail "目标 release Python 解释器不存在或不可执行"
[[ -f "${smoke_script}" && ! -L "${smoke_script}" ]] \
  || fail "目标 release 生产业务冒烟脚本不存在或不是普通文件"

timestamp="$(date +%Y%m%d-%H%M%S)"
plan_file="${STATE_DIR}/production-restart-plan"
impact_report="${REPORT_DIR}/production-impact-${timestamp}-${target_sha}.json"
before_snapshot="${STATE_DIR}/production-units-before.tsv"
after_snapshot="${STATE_DIR}/production-units-after.tsv"
python3 "${IMPACT_MAPPER}" \
  --git-dir "${SOURCE_GIT}" \
  --plan "${plan_file}" \
  --report "${impact_report}" \
  "${old_sha}" "${target_sha}"
snapshot_units "${before_snapshot}"

planned_count="$(awk 'NF {count += 1} END {print count + 0}' "${plan_file}")"
log "差异分析完成：计划重启 ${planned_count} 个正式单元"
log "执行迁移账本、会话目录与 SQLite 备份预检"
"${PREFLIGHT_SCRIPT}" "${target_sha}"
"${MANAGER}" switch-selected "${target_sha}" "${plan_file}"
rollback_needed=true

snapshot_units "${after_snapshot}"
verify_transition \
  "${before_snapshot}" "${after_snapshot}" "${plan_file}" "${target_sha}"

log "运行正式业务冒烟（仅写生产 SQLite 会话/检查点，不写 MySQL 会话元数据）"
"${target_release}/.venv/bin/python" "${smoke_script}"
check_public_urls
"${MANAGER}" assert-running

rollback_needed=false
record_sha "${SUCCESS_SHA_FILE}" "${target_sha}"
[[ ! -f "${FAILED_SHA_FILE}" ]] || unlink "${FAILED_SHA_FILE}"
log "正式部署完成：${old_sha} -> ${target_sha}；仅重启 ${planned_count} 个受影响单元"
