#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_TEST_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
SOURCE_GIT_DIR="${RUNTIME_ROOT}/source.git"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
CURRENT_LINK="${RUNTIME_ROOT}/current"
STATE_DIR="${RUNTIME_ROOT}/state"
TARGET_BRANCH="${DOC_AGENT_TEST_BRANCH:-dev}"
CREDENTIAL_FILE="${DOC_AGENT_TEST_GIT_CREDENTIAL_FILE:-${RUNTIME_ROOT}/config/git-auth/credentials}"
FAILED_SHA_FILE="${STATE_DIR}/auto-update.failed-sha"
SUCCESS_SHA_FILE="${STATE_DIR}/auto-update.success-sha"
LOCK_FILE="${STATE_DIR}/auto-update.lock"
FAILURE_RETRY_SECONDS="${DOC_AGENT_TEST_FAILURE_RETRY_SECONDS:-3600}"
AUTO_PROMOTE_PRODUCTION="${DOC_AGENT_TEST_AUTO_PROMOTE_PRODUCTION:-false}"

log() {
  printf '[doc-agent-test-auto-update] %s\n' "$*"
}

fail() {
  printf '[doc-agent-test-auto-update] ERROR: %s\n' "$*" >&2
  exit 1
}

record_sha() {
  local output_file="$1" sha="$2" temporary_file="${1}.$$"
  printf '%s\n' "${sha}" >"${temporary_file}"
  chmod 600 "${temporary_file}"
  mv -f "${temporary_file}" "${output_file}"
}

read_recorded_sha() {
  local input_file="$1" value=""
  if [[ -f "${input_file}" ]]; then
    IFS= read -r value <"${input_file}" || true
  fi
  printf '%s' "${value}"
}

promote_tested_sha() {
  local sha="$1" promotion_script="${CURRENT_LINK}/deploy/production/promote-tested.sh"
  if [[ "${AUTO_PROMOTE_PRODUCTION}" != "true" ]]; then
    log "自动 production 提升已关闭；已记录通过测试的 SHA ${sha}，等待授权人员提升"
    return 0
  fi
  if [[ ! -x "${promotion_script}" ]]; then
    log "当前 release 尚无 production 提升门禁，跳过分支提升"
    return 0
  fi
  "${promotion_script}" "${sha}"
}

[[ "${FAILURE_RETRY_SECONDS}" =~ ^[0-9]+$ ]] \
  || fail "DOC_AGENT_TEST_FAILURE_RETRY_SECONDS 必须是非负整数"
[[ "${AUTO_PROMOTE_PRODUCTION}" == "true" || "${AUTO_PROMOTE_PRODUCTION}" == "false" ]] \
  || fail "DOC_AGENT_TEST_AUTO_PROMOTE_PRODUCTION 必须是 true 或 false"
[[ -d "${SOURCE_GIT_DIR}" ]] || fail "测试 bare Git 仓库不存在：${SOURCE_GIT_DIR}"
[[ -f "${CREDENTIAL_FILE}" ]] || fail "测试 Git 凭据文件不存在"

credential_mode="$(stat -c '%a' "${CREDENTIAL_FILE}")"
credential_mode_value=$((8#${credential_mode}))
(( (credential_mode_value & 077) == 0 )) \
  || fail "测试 Git 凭据文件不能允许 group/other 读取"

mkdir -p "${STATE_DIR}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "已有一次检查或部署正在运行，本次跳过"
  exit 0
fi

export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.helper
export GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1="store --file=${CREDENTIAL_FILE}"

log "检查 origin/${TARGET_BRANCH}"
git --git-dir="${SOURCE_GIT_DIR}" fetch \
  --prune \
  origin \
  "+refs/heads/${TARGET_BRANCH}:refs/heads/${TARGET_BRANCH}"

remote_sha="$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse "refs/heads/${TARGET_BRANCH}")"
[[ "${remote_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "远端分支未解析为完整提交 SHA"

current_sha=""
current_release=""
if [[ -L "${CURRENT_LINK}" ]]; then
  current_release="$(readlink -f "${CURRENT_LINK}")"
  case "${current_release}" in
    "${RELEASES_DIR}"/*) ;;
    *) fail "current 未指向隔离测试 releases 目录" ;;
  esac
  current_sha="$(git -C "${current_release}" rev-parse HEAD)"
fi

if [[ "${current_sha}" == "${remote_sha}" ]]; then
  [[ ! -f "${FAILED_SHA_FILE}" ]] || unlink "${FAILED_SHA_FILE}"
  record_sha "${SUCCESS_SHA_FILE}" "${remote_sha}"
  promote_tested_sha "${remote_sha}"
  log "测试环境已经运行 origin/${TARGET_BRANCH} ${remote_sha}，无需重启"
  exit 0
fi

if [[ -n "${current_sha}" ]] \
  && git --git-dir="${SOURCE_GIT_DIR}" merge-base --is-ancestor "${remote_sha}" "${current_sha}"; then
  [[ ! -f "${FAILED_SHA_FILE}" ]] || unlink "${FAILED_SHA_FILE}"
  log "origin/${TARGET_BRANCH} ${remote_sha} 落后于测试提交 ${current_sha}，拒绝自动降级"
  exit 0
fi

failed_sha="$(read_recorded_sha "${FAILED_SHA_FILE}")"
if [[ "${failed_sha}" == "${remote_sha}" ]]; then
  now_epoch="$(date +%s)"
  failed_epoch="$(stat -c '%Y' "${FAILED_SHA_FILE}")"
  if (( now_epoch - failed_epoch < FAILURE_RETRY_SECONDS )); then
    log "提交 ${remote_sha} 上次部署失败，等待重试窗口后再试"
    exit 0
  fi
fi

if [[ -n "${current_sha}" ]] \
  && ! git --git-dir="${SOURCE_GIT_DIR}" merge-base --is-ancestor "${current_sha}" "${remote_sha}"; then
  record_sha "${FAILED_SHA_FILE}" "${remote_sha}"
  fail "测试提交 ${current_sha} 与 origin/${TARGET_BRANCH} ${remote_sha} 已分叉，拒绝自动覆盖"
fi

release_script="${CURRENT_LINK}/deploy/local-test/release.sh"
[[ -x "${release_script}" ]] || fail "当前测试 release 缺少可执行发布器"

log "发现可快进的新提交 ${remote_sha}，开始隔离测试部署"
set +e
(
  cd "${current_release:-${RUNTIME_ROOT}}"
  "${release_script}" update "${remote_sha}"
)
deploy_status=$?
set -e

if (( deploy_status != 0 )); then
  record_sha "${FAILED_SHA_FILE}" "${remote_sha}"
  fail "提交 ${remote_sha} 部署失败；发布器已执行测试环境回滚"
fi

deployed_release="$(readlink -f "${CURRENT_LINK}")"
deployed_sha="$(git -C "${deployed_release}" rev-parse HEAD)"
target_sha="$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse "refs/heads/${TARGET_BRANCH}")"
if [[ "${deployed_sha}" != "${target_sha}" ]]; then
  record_sha "${FAILED_SHA_FILE}" "${target_sha}"
  fail "部署后测试提交 ${deployed_sha} 与已获取目标 ${target_sha} 不一致"
fi

[[ ! -f "${FAILED_SHA_FILE}" ]] || unlink "${FAILED_SHA_FILE}"
record_sha "${SUCCESS_SHA_FILE}" "${deployed_sha}"
promote_tested_sha "${deployed_sha}"
log "自动同步完成：origin/${TARGET_BRANCH} ${deployed_sha}"
