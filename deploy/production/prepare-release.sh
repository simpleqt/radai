#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
SOURCE_GIT="${RUNTIME_ROOT}/source.git"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
CONFIG_ENV="${RUNTIME_ROOT}/config/backend.env"
CREDENTIAL_FILE="${DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_FILE:-${RUNTIME_ROOT}/config/git-auth/credentials}"
DEPLOYMENT_BRANCH="${DOC_AGENT_PRODUCTION_BRANCH:-production}"
TARGET_SHA="${1:-}"
FORCE_REBUILD=false
for arg in "$@"; do
  [[ "${arg}" == "--force-rebuild" ]] && FORCE_REBUILD=true
done

log() {
  printf '[production-prepare] %s\n' "$*"
}

fail() {
  printf '[production-prepare] ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${DEPLOYMENT_BRANCH}" == "production" ]] \
  || fail "正式部署分支必须为 production"
[[ -d "${SOURCE_GIT}" ]] || fail "生产 bare 仓库不存在：${SOURCE_GIT}"
[[ -f "${CONFIG_ENV}" ]] || fail "生产配置不存在：${CONFIG_ENV}"
[[ -f "${CREDENTIAL_FILE}" && ! -L "${CREDENTIAL_FILE}" ]] \
  || fail "生产 Git 凭据必须是普通文件"
[[ "$(stat -c '%u' "${CREDENTIAL_FILE}")" == "$(id -u)" ]] \
  || fail "生产 Git 凭据文件所有者不正确"
credential_mode="$(stat -c '%a' "${CREDENTIAL_FILE}")"
credential_mode_value=$((8#${credential_mode}))
(( (credential_mode_value & 077) == 0 )) \
  || fail "生产 Git 凭据文件不能允许 group/other 读取"

export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.helper
export GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1="store --file=${CREDENTIAL_FILE}"

log "获取 origin/${DEPLOYMENT_BRANCH}"
git --git-dir="${SOURCE_GIT}" fetch \
  --prune \
  origin \
  "+refs/heads/${DEPLOYMENT_BRANCH}:refs/heads/${DEPLOYMENT_BRANCH}"
deployment_tip="$(git --git-dir="${SOURCE_GIT}" rev-parse "refs/heads/${DEPLOYMENT_BRANCH}")"
if [[ -z "${TARGET_SHA}" ]]; then
  TARGET_SHA="${deployment_tip}"
fi

[[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "必须提供完整的 40 位提交 SHA"
git --git-dir="${SOURCE_GIT}" cat-file -e "${TARGET_SHA}^{commit}"
[[ "${TARGET_SHA}" == "${deployment_tip}" ]] \
  || fail "目标提交不是 origin/${DEPLOYMENT_BRANCH} 当前 tip，禁止准备正式 release"

RELEASE_DIR="${RELEASES_DIR}/${TARGET_SHA}"
if [[ ! -d "${RELEASE_DIR}" ]]; then
  git --git-dir="${SOURCE_GIT}" worktree prune
  git --git-dir="${SOURCE_GIT}" worktree add --detach "${RELEASE_DIR}" "${TARGET_SHA}"
fi
[[ "$(git -C "${RELEASE_DIR}" rev-parse HEAD)" == "${TARGET_SHA}" ]] \
  || fail "已有 release 目录与目标 SHA 不一致"

# 已存在的 release 默认只补缺（env 链接），不重建依赖与 dist：
# 构建期间 vite preview 直接读 dist，在位重建会让线上前端行为抖动，
# 违反不可变 release 红线。显式 --force-rebuild 才允许重建（且拒绝
# 当前 current 指向的 release）。
if [[ "${FORCE_REBUILD}" != "true" && -x "${RELEASE_DIR}/.venv/bin/python" ]] \
  && [[ -x "${RELEASE_DIR}/frontend/node_modules/.bin/vite" ]] \
  && [[ -d "${RELEASE_DIR}/frontend/dist" ]]; then
  log "release 已存在且构建产物完整，跳过重建（--force-rebuild 可覆盖）"
  SKIP_REBUILD=true
else
  SKIP_REBUILD=false
fi
if [[ "${FORCE_REBUILD}" == "true" ]]; then
  # current 链接在 RUNTIME_ROOT 下（deploy-release/cleanup-releases 同源），
  # 不是 RELEASES_DIR/current；比较错了路径守卫永不生效。
  if [[ "$(readlink -f "${RUNTIME_ROOT}/current" 2>/dev/null)" == "${RELEASE_DIR}" ]]; then
    fail "拒绝在位重建当前服役的 release（current 指向目标目录）"
  fi
  SKIP_REBUILD=false
fi

if [[ ! -e "${RELEASE_DIR}/backend/.env" ]]; then
  ln -s "${CONFIG_ENV}" "${RELEASE_DIR}/backend/.env"
fi
[[ -L "${RELEASE_DIR}/backend/.env" ]] \
  || fail "release backend/.env 必须是稳定配置软链接"
[[ "$(readlink -f "${RELEASE_DIR}/backend/.env")" == "$(readlink -f "${CONFIG_ENV}")" ]] \
  || fail "release backend/.env 未指向正式稳定配置"

frontend_tree_of() {
  git -C "$1" rev-parse "HEAD:$2" 2>/dev/null || true
}

# 前端构建是 prepare 最慢的阶段（5 × npm ci + vite build），而常见 release
# 只改后端。前端源树（git 内容寻址 tree SHA）与某个既有 release 完全一致
# 时，硬链接复用其 node_modules + dist：npm ci 与 vite build 都是先删后写
# （unlink 语义，硬链接安全），release 之间共享 inode 而互不写入，符合
# 不可变红线。donor 必须自带 marker 且与其自身源树一致，防止手改过的
# donor 被误信。
reuse_frontend_build() {
  local frontend="$1" new_tree donor donor_tree marker current_target
  new_tree="$(frontend_tree_of "${RELEASE_DIR}" "${frontend}")"
  [[ -n "${new_tree}" ]] || return 1
  local -a candidates=()
  current_target="$(readlink -f "${RUNTIME_ROOT}/current" 2>/dev/null || true)"
  if [[ -n "${current_target}" && "${current_target}" != "${RELEASE_DIR}" ]]; then
    candidates+=("${current_target}")
  fi
  while IFS= read -r donor; do
    [[ "${donor}" == "${RELEASE_DIR}" ]] && continue
    candidates+=("${RELEASES_DIR}/${donor}")
  done < <(ls -1t "${RELEASES_DIR}" 2>/dev/null | head -8)
  for donor in "${candidates[@]}"; do
    [[ -d "${donor}/${frontend}/dist" ]] || continue
    [[ -x "${donor}/${frontend}/node_modules/.bin/vite" ]] || continue
    marker="$(cat "${donor}/${frontend}/.prepared-frontend-tree" 2>/dev/null || true)"
    [[ "${marker}" == "${new_tree}" ]] || continue
    donor_tree="$(frontend_tree_of "${donor}" "${frontend}")"
    [[ "${donor_tree}" == "${new_tree}" ]] || continue
    rm -rf "${RELEASE_DIR:?}/${frontend}/node_modules" \
      "${RELEASE_DIR:?}/${frontend}/dist"
    cp -al "${donor}/${frontend}/node_modules" \
      "${RELEASE_DIR}/${frontend}/node_modules"
    cp -al "${donor}/${frontend}/dist" "${RELEASE_DIR}/${frontend}/dist"
    printf '%s\n' "${new_tree}" \
      > "${RELEASE_DIR}/${frontend}/.prepared-frontend-tree"
    log "复用 ${frontend} 构建产物（源树未变，硬链接自 $(basename "${donor}")）"
    return 0
  done
  return 1
}

if [[ "${SKIP_REBUILD}" != "true" ]]; then
  log "安装锁定的 Python 依赖"
  (
    cd "${RELEASE_DIR}"
    uv sync --frozen --no-install-project
  )

  for frontend in frontend frontend_audit frontend_csh_ai frontend_fmca guest; do
    if reuse_frontend_build "${frontend}"; then
      continue
    fi
    log "安装 ${frontend} 锁定依赖"
    (
      cd "${RELEASE_DIR}/${frontend}"
      npm ci --no-audit --no-fund
      npm run build
    )
    new_tree="$(frontend_tree_of "${RELEASE_DIR}" "${frontend}")"
    if [[ -n "${new_tree}" ]]; then
      printf '%s\n' "${new_tree}" \
        > "${RELEASE_DIR}/${frontend}/.prepared-frontend-tree"
    fi
  done
fi

[[ "$(git -C "${RELEASE_DIR}" rev-parse HEAD)" == "${TARGET_SHA}" ]]
[[ -x "${RELEASE_DIR}/.venv/bin/python" ]]
for frontend in frontend frontend_audit frontend_csh_ai frontend_fmca guest; do
  [[ -x "${RELEASE_DIR}/${frontend}/node_modules/.bin/vite" ]]
  [[ -d "${RELEASE_DIR}/${frontend}/dist" ]]
done

log "${DEPLOYMENT_BRANCH} release 已准备：${TARGET_SHA}"
printf '%s\n' "${RELEASE_DIR}"
