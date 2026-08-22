#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_DOCKER_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-docker-test}"
HOST_TEST_ROOT="${DOC_AGENT_HOST_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
SOURCE_GIT_DIR="${DOC_AGENT_DOCKER_SOURCE_GIT:-${HOST_TEST_ROOT}/source.git}"
CREDENTIAL_FILE="${DOC_AGENT_DOCKER_GIT_CREDENTIAL_FILE:-${HOST_TEST_ROOT}/config/git-auth/credentials}"
TARGET_BRANCH="${DOC_AGENT_DOCKER_TEST_BRANCH:-dev}"
STATE_DIR="${RUNTIME_ROOT}/state"
CONTROL_RELEASES="${RUNTIME_ROOT}/control-releases"
CONTROL_LINK="${RUNTIME_ROOT}/control"
DEPLOYMENT_ENV="${STATE_DIR}/deployment.env"
FAILED_SHA_FILE="${STATE_DIR}/auto-update.failed-sha"
SUCCESS_SHA_FILE="${STATE_DIR}/auto-update.success-sha"
LOCK_FILE="${STATE_DIR}/auto-update.lock"
FAILURE_RETRY_SECONDS="${DOC_AGENT_DOCKER_FAILURE_RETRY_SECONDS:-3600}"

log() {
  printf '[radai-docker-test-auto-update] %s\n' "$*"
}

fail() {
  printf '[radai-docker-test-auto-update] ERROR: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

cleanup_control_candidate() {
  local candidate="$1"
  case "${candidate}" in
    "${CONTROL_RELEASES}"/.*) ;;
    *) fail "拒绝清理未验证的控制面临时目录" ;;
  esac
  [[ -d "${candidate}" ]] || return 0
  find "${candidate}" -mindepth 1 -delete
  rmdir -- "${candidate}"
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

materialize_control() {
  local sha="$1" target="${CONTROL_RELEASES}/${1}" candidate
  if [[ ! -d "${target}" ]]; then
    candidate="${CONTROL_RELEASES}/.${sha}.${BASHPID}"
    [[ ! -e "${candidate}" ]] || fail "控制面临时目录已存在"
    install -d -m 0700 "${candidate}"
    if ! git --git-dir="${SOURCE_GIT_DIR}" archive "${sha}" \
      deploy/docker-test deploy/local-test deploy/shared-data \
      deploy/quality-gate.json \
      | tar -x -C "${candidate}"; then
      cleanup_control_candidate "${candidate}"
      fail "无法从目标 SHA 提取 Docker 测试控制面"
    fi
    if [[ ! -x "${candidate}/deploy/docker-test/manage.sh" ]]; then
      cleanup_control_candidate "${candidate}"
      fail "目标提交缺少 Docker 测试控制器"
    fi
    if [[ ! -f "${candidate}/deploy/quality-gate.json" ]]; then
      cleanup_control_candidate "${candidate}"
      fail "目标提交缺少质量门禁策略"
    fi
    printf '%s\n' "${sha}" >"${candidate}/.source-sha"
    chmod 600 "${candidate}/.source-sha"
    mv -T "${candidate}" "${target}"
  fi
  [[ -f "${target}/.source-sha" ]] \
    && [[ "$(<"${target}/.source-sha")" == "${sha}" ]] \
    || fail "不可变 Docker 测试控制面 SHA 标记无效"
  [[ -x "${target}/deploy/docker-test/manage.sh" ]] \
    || fail "不可变 Docker 测试控制面不完整"
  [[ -f "${target}/deploy/quality-gate.json" ]] \
    || fail "不可变 Docker 测试控制面缺少质量门禁策略"
  printf '%s\n' "${target}"
}

activate_control() {
  local control_release="$1" temporary_link temporary_script
  if [[ -L "${CONTROL_LINK}" ]] \
      && [[ "$(readlink -f "${CONTROL_LINK}")" == "${control_release}" ]] \
      && [[ -f "${RUNTIME_ROOT}/bin/auto-update.sh" ]] \
      && cmp -s \
        "${control_release}/deploy/docker-test/auto-update.sh" \
        "${RUNTIME_ROOT}/bin/auto-update.sh"; then
    return
  fi
  temporary_link="${RUNTIME_ROOT}/.control.${BASHPID}"
  [[ ! -e "${temporary_link}" && ! -L "${temporary_link}" ]] \
    || fail "控制面临时链接已存在"
  ln -s "${control_release}" "${temporary_link}"
  mv -Tf "${temporary_link}" "${CONTROL_LINK}"
  temporary_script="${RUNTIME_ROOT}/bin/.auto-update.sh.${BASHPID}"
  install -m 0755 \
    "${control_release}/deploy/docker-test/auto-update.sh" \
    "${temporary_script}"
  mv -f "${temporary_script}" "${RUNTIME_ROOT}/bin/auto-update.sh"
}

[[ "${FAILURE_RETRY_SECONDS}" =~ ^[0-9]+$ ]] \
  || fail "DOC_AGENT_DOCKER_FAILURE_RETRY_SECONDS 必须是非负整数"
for required_command in cmp date find flock git install ln mkdir mv readlink rmdir stat tar unlink; do
  need "${required_command}"
done
[[ -d "${SOURCE_GIT_DIR}" ]] || fail "测试 bare Git 仓库不存在：${SOURCE_GIT_DIR}"
[[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态不存在"
[[ -f "${CREDENTIAL_FILE}" ]] || fail "测试 Git 凭据文件不存在"

credential_mode="$(stat -c '%a' "${CREDENTIAL_FILE}")"
credential_mode_value=$((8#${credential_mode}))
(( (credential_mode_value & 077) == 0 )) \
  || fail "测试 Git 凭据文件不能允许 group/other 读取"

mkdir -p "${STATE_DIR}" "${CONTROL_RELEASES}" "${RUNTIME_ROOT}/bin"
chmod 700 "${STATE_DIR}" "${CONTROL_RELEASES}" "${RUNTIME_ROOT}/bin"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "已有一次检查、部署或切换正在运行，本次跳过"
  exit 0
fi

deployment_mode="$(awk -F= '$1 == "DEPLOYMENT_MODE" {print $2}' "${DEPLOYMENT_ENV}")"
[[ "${deployment_mode}" == primary ]] \
  || fail "自动更新只允许管理已经切换完成的 Docker 主测试模式"
current_sha="$(awk -F= '$1 == "ACTIVE_SOURCE_SHA" {print $2}' "${DEPLOYMENT_ENV}")"
[[ "${current_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "当前 Docker 测试 SHA 无效"

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

if [[ "${current_sha}" == "${remote_sha}" ]]; then
  control_release="$(materialize_control "${remote_sha}")"
  activate_control "${control_release}"
  [[ ! -f "${FAILED_SHA_FILE}" ]] || unlink -- "${FAILED_SHA_FILE}"
  record_sha "${SUCCESS_SHA_FILE}" "${remote_sha}"
  log "Docker 测试已经运行 origin/${TARGET_BRANCH} ${remote_sha}，无需重启"
  exit 0
fi

if git --git-dir="${SOURCE_GIT_DIR}" merge-base --is-ancestor "${remote_sha}" "${current_sha}"; then
  [[ ! -f "${FAILED_SHA_FILE}" ]] || unlink -- "${FAILED_SHA_FILE}"
  log "origin/${TARGET_BRANCH} ${remote_sha} 落后于 Docker 测试提交 ${current_sha}，拒绝自动降级"
  exit 0
fi

if ! git --git-dir="${SOURCE_GIT_DIR}" merge-base --is-ancestor "${current_sha}" "${remote_sha}"; then
  record_sha "${FAILED_SHA_FILE}" "${remote_sha}"
  fail "Docker 测试提交 ${current_sha} 与 origin/${TARGET_BRANCH} ${remote_sha} 已分叉"
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

log "发现可快进的新提交 ${remote_sha}，开始 Docker 主测试增量部署"
control_release="$(materialize_control "${remote_sha}")"
manager="${DOC_AGENT_DOCKER_TEST_MANAGER:-${control_release}/deploy/docker-test/manage.sh}"
[[ -x "${manager}" ]] || fail "Docker 测试控制器不可执行：${manager}"
if ! "${manager}" deploy "${remote_sha}" primary; then
  record_sha "${FAILED_SHA_FILE}" "${remote_sha}"
  fail "提交 ${remote_sha} 部署失败；控制器已执行 Docker 测试回滚"
fi

deployed_sha="$(awk -F= '$1 == "ACTIVE_SOURCE_SHA" {print $2}' "${DEPLOYMENT_ENV}")"
if [[ "${deployed_sha}" != "${remote_sha}" ]]; then
  # 不记录 failed-sha 会让该 SHA 绕过 1 小时节流，每 5 分钟全量重建。
  record_sha "${FAILED_SHA_FILE}" "${remote_sha}"
  fail "部署后 Docker 测试 SHA ${deployed_sha} 与目标 ${remote_sha} 不一致"
fi
activate_control "${control_release}"
[[ ! -f "${FAILED_SHA_FILE}" ]] || unlink -- "${FAILED_SHA_FILE}"
record_sha "${SUCCESS_SHA_FILE}" "${deployed_sha}"
log "自动同步完成：origin/${TARGET_BRANCH} ${deployed_sha}"
