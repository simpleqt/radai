#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${DOC_AGENT_DOCKER_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-docker-test}"
HOST_TEST_ROOT="${DOC_AGENT_HOST_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
SOURCE_GIT_DIR="${DOC_AGENT_DOCKER_SOURCE_GIT:-${HOST_TEST_ROOT}/source.git}"
SOURCE_RUNTIME_ENV="${DOC_AGENT_DOCKER_SOURCE_ENV:-${HOST_TEST_ROOT}/config/runtime.env}"
RUNTIME_ENV="${RUNTIME_ROOT}/config/runtime.env"
FRONTEND_ENV="${RUNTIME_ROOT}/config/frontend.env"
GROUNDWATER_BUILTIN_ROOT="${DOC_AGENT_GROUNDWATER_BUILTIN_ROOT:-/home/inspur/myenv/runtime/doc-agent-shared/groundwater/liutang}"
GROUNDWATER_INSTALLER="${SCRIPT_DIR}/../shared-data/install_groundwater.py"
STATE_DIR="${RUNTIME_ROOT}/state"
REPORT_DIR="${RUNTIME_ROOT}/reports"
DEPLOYMENT_ENV="${STATE_DIR}/deployment.env"
IMPACT_PLAN="${STATE_DIR}/impact-plan"
IMPACT_REPORT="${STATE_DIR}/last-impact.json"
COMPOSE_FILE="${SCRIPT_DIR}/compose.shadow.yml"
COMPOSE_AUDIT_FILE="${SCRIPT_DIR}/compose.audit.yml"
QUALITY_POLICY="${REPO_ROOT}/deploy/quality-gate.json"
BROWSER_SMOKE_IMAGE="ghcr.io/firecrawl/playwright-service@sha256:6359b0d9070f27400b4a9b615509be06919e40121fc1fdc42d4efeddf02653d2"
PROJECT_NAME="radai-docker-test-shadow"
TARGET_BRANCH="${DOC_AGENT_DOCKER_TEST_BRANCH:-dev}"

ALL_SERVICES=(
  agent-api
  knowledge-api
  guest-api
  frontend
  audit-frontend
  csh-frontend
  fmca-frontend
  landslide-frontend
  guest-frontend
)
BACKEND_SERVICES=(agent-api knowledge-api guest-api)
PRODUCTION_PORTS=(5173 5174 5176 5177 8082 8083 8086)
HOST_TEST_PORTS=(15173 15174 15176 15177 18082 18083 18084 18086)
PROTECTED_PORTS=()
STATE_GUARD_API_PORTS=()

configure_deployment_mode() {
  local requested_mode="$1"
  case "${requested_mode}" in
    shadow)
      DEPLOYMENT_MODE=shadow
      BIND_ADDRESS=127.0.0.1
      TEST_AGENT_PORT=28082
      TEST_GUEST_API_PORT=28083
      TEST_KNOWLEDGE_PORT=28086
      TEST_FRONTEND_PORT=25173
      TEST_AUDIT_FRONTEND_PORT=25177
      TEST_CSH_FRONTEND_PORT=25174
      TEST_FMCA_FRONTEND_PORT=25176
      TEST_LANDSLIDE_FRONTEND_PORT=25175
      TEST_GUEST_FRONTEND_PORT=28084
      PROTECTED_PORTS=("${PRODUCTION_PORTS[@]}" "${HOST_TEST_PORTS[@]}")
      ;;
    primary)
      DEPLOYMENT_MODE=primary
      BIND_ADDRESS="${DOC_AGENT_DOCKER_TEST_BIND_ADDRESS:-0.0.0.0}"
      [[ "${BIND_ADDRESS}" == 0.0.0.0 || "${BIND_ADDRESS}" == 127.0.0.1 ]] \
        || fail "Docker 主测试绑定地址只允许 0.0.0.0 或 127.0.0.1"
      TEST_AGENT_PORT=18082
      TEST_GUEST_API_PORT=18083
      TEST_KNOWLEDGE_PORT=18086
      TEST_FRONTEND_PORT=15173
      TEST_AUDIT_FRONTEND_PORT=15177
      TEST_CSH_FRONTEND_PORT=15174
      TEST_FMCA_FRONTEND_PORT=15176
      TEST_LANDSLIDE_FRONTEND_PORT=15175
      TEST_GUEST_FRONTEND_PORT=18084
      PROTECTED_PORTS=("${PRODUCTION_PORTS[@]}")
      ;;
    *)
      fail "未知 Docker 测试部署模式：${requested_mode}"
      ;;
  esac
  STATE_GUARD_API_PORTS=(
    "${TEST_AGENT_PORT}"
    "${TEST_GUEST_API_PORT}"
    "${TEST_KNOWLEDGE_PORT}"
  )
}

active_deployment_mode() {
  local mode=""
  if [[ -f "${DEPLOYMENT_ENV}" ]]; then
    mode="$(awk -F= '$1 == "DEPLOYMENT_MODE" {print $2}' "${DEPLOYMENT_ENV}")"
  fi
  printf '%s\n' "${mode:-shadow}"
}

log() {
  printf '[radai-docker-test] %s\n' "$*"
}

fail() {
  printf '[radai-docker-test] ERROR: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

compose_command() {
  if command -v docker-compose >/dev/null 2>&1; then
    printf '%s\n' docker-compose
    return
  fi
  if docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'docker compose'
    return
  fi
  fail "缺少 Docker Compose v2"
}

compose_with_env() {
  local environment_file="$1"
  shift
  local compose_bin
  local -a compose_files=(--file "${COMPOSE_FILE}")
  compose_bin="$(compose_command)"
  # deployment.env created before the dedicated audit frontend existed does not
  # contain its image. Keep those environments operable during the one-time
  # transition; new candidates always opt into the overlay explicitly.
  if awk -F= '$1 == "AUDIT_FRONTEND_IMAGE" && length($2) > 0 {found=1} END {exit !found}' \
    "${environment_file}"; then
    compose_files+=(--file "${COMPOSE_AUDIT_FILE}")
  fi
  local -a clean_compose_env=(
    env
    -u BIND_ADDRESS
    -u TEST_AGENT_PORT
    -u TEST_GUEST_API_PORT
    -u TEST_KNOWLEDGE_PORT
    -u TEST_FRONTEND_PORT
    -u TEST_AUDIT_FRONTEND_PORT
    -u TEST_CSH_FRONTEND_PORT
    -u TEST_FMCA_FRONTEND_PORT
    -u TEST_LANDSLIDE_FRONTEND_PORT
    -u TEST_GUEST_FRONTEND_PORT
  )
  if [[ "${compose_bin}" == "docker-compose" ]]; then
    "${clean_compose_env[@]}" docker-compose \
      --project-name "${PROJECT_NAME}" \
      --env-file "${environment_file}" \
      "${compose_files[@]}" \
      "$@"
  else
    "${clean_compose_env[@]}" docker compose \
      --project-name "${PROJECT_NAME}" \
      --env-file "${environment_file}" \
      "${compose_files[@]}" \
      "$@"
  fi
}

assert_runtime_layout() {
  [[ -d "${SOURCE_GIT_DIR}" ]] || fail "测试 bare Git 仓库不存在：${SOURCE_GIT_DIR}"
  [[ -f "${SOURCE_RUNTIME_ENV}" ]] || fail "测试运行时配置不存在：${SOURCE_RUNTIME_ENV}"
  [[ -f "${COMPOSE_FILE}" ]] || fail "Compose 文件不存在：${COMPOSE_FILE}"
  [[ -f "${COMPOSE_AUDIT_FILE}" ]] \
    || fail "审计前端 Compose 文件不存在：${COMPOSE_AUDIT_FILE}"
  [[ -f "${SCRIPT_DIR}/backend.Dockerfile" ]] || fail "后端 Dockerfile 不存在"
  [[ -f "${SCRIPT_DIR}/frontend.Dockerfile" ]] || fail "前端 Dockerfile 不存在"
  [[ -f "${SCRIPT_DIR}/audit-frontend.Dockerfile" ]] \
    || fail "审计前端 Dockerfile 不存在"
  [[ -f "${SCRIPT_DIR}/quality.Dockerfile" ]] || fail "质量门禁 Dockerfile 不存在"
  [[ -f "${SCRIPT_DIR}/run_quality_gate.py" ]] || fail "质量门禁执行器不存在"
  [[ -f "${SCRIPT_DIR}/browser_smoke.js" ]] || fail "浏览器冒烟脚本不存在"
  [[ -f "${QUALITY_POLICY}" ]] || fail "质量门禁策略不存在"
  [[ -f "${REPO_ROOT}/deploy/local-test/render_runtime_env.py" ]] \
    || fail "缺少测试配置渲染器"
  [[ -f "${REPO_ROOT}/deploy/local-test/changed_services.py" ]] \
    || fail "缺少服务影响分析器"
  [[ -f "${SCRIPT_DIR}/../local-test/smoke_test.py" ]] \
    || fail "缺少业务冒烟脚本"
  [[ -f "${GROUNDWATER_INSTALLER}" ]] || fail "缺少地下水共享数据校验器"
  python3 "${GROUNDWATER_INSTALLER}" verify \
    --shared-root "${GROUNDWATER_BUILTIN_ROOT%/*}" \
    --alias "${GROUNDWATER_BUILTIN_ROOT##*/}" \
    >/dev/null || fail "地下水内置数据集完整性校验失败"
}

init_directories() {
  umask 077
  mkdir -p \
    "${RUNTIME_ROOT}/config" \
    "${RUNTIME_ROOT}/data/checkpoints" \
    "${RUNTIME_ROOT}/data/backend-app" \
    "${RUNTIME_ROOT}/data/conversations" \
    "${RUNTIME_ROOT}/data/rag" \
    "${RUNTIME_ROOT}/data/storage/documents" \
    "${RUNTIME_ROOT}/data/storage/results" \
    "${RUNTIME_ROOT}/data/storage/cache" \
    "${RUNTIME_ROOT}/data/storage/uploads" \
    "${RUNTIME_ROOT}/data/traces" \
    "${RUNTIME_ROOT}/data/traces-indexed" \
    "${RUNTIME_ROOT}/logs/backend" \
    "${RUNTIME_ROOT}/backups" \
    "${REPORT_DIR}" \
    "${STATE_DIR}"
  chmod 700 \
    "${RUNTIME_ROOT}/config" \
    "${RUNTIME_ROOT}/data" \
    "${RUNTIME_ROOT}/logs" \
    "${RUNTIME_ROOT}/backups" \
    "${REPORT_DIR}" \
    "${STATE_DIR}"
}

render_runtime_config() {
  local rendered="${RUNTIME_ENV}.rendered.$$"
  local frontend_rendered="${FRONTEND_ENV}.rendered.$$"
  local combined_no_proxy files_target
  local configured_agent_port="${TEST_AGENT_PORT}"
  local configured_guest_api_port="${TEST_GUEST_API_PORT}"
  local configured_knowledge_port="${TEST_KNOWLEDGE_PORT}"
  local configured_frontend_port="${TEST_FRONTEND_PORT}"
  local configured_audit_frontend_port="${TEST_AUDIT_FRONTEND_PORT}"
  local configured_csh_frontend_port="${TEST_CSH_FRONTEND_PORT}"
  local configured_fmca_frontend_port="${TEST_FMCA_FRONTEND_PORT}"
  local configured_landslide_frontend_port="${TEST_LANDSLIDE_FRONTEND_PORT}"
  local configured_guest_frontend_port="${TEST_GUEST_FRONTEND_PORT}"
  umask 077
  python3 "${REPO_ROOT}/deploy/local-test/render_runtime_env.py" \
    --source "${SOURCE_RUNTIME_ENV}" \
    --output "${rendered}" \
    --runtime-root "${RUNTIME_ROOT}" \
    >/dev/null

  set -a
  # The renderer emits a trusted, owner-only shell environment.
  # shellcheck disable=SC1090
  source "${rendered}"
  set +a
  files_target="${TEST_FILES_PROXY_TARGET:-}"
  [[ "${files_target}" == http://* || "${files_target}" == https://* ]] \
    || fail "TEST_FILES_PROXY_TARGET 必须是 HTTP(S) 地址"
  combined_no_proxy="${NO_PROXY:-}"
  if [[ -n "${combined_no_proxy}" ]]; then
    combined_no_proxy+=","
  fi
  combined_no_proxy+="127.0.0.1,localhost,agent-api,guest-api,knowledge-api,langchain-firecrawl-gateway"

  {
    printf 'DOC_AGENT_TEST_RUNTIME_ROOT=%q\n' "${RUNTIME_ROOT}"
    printf 'DOC_AGENT_TEST_REPORT_DIR=%q\n' "${REPORT_DIR}"
    printf 'TEST_AGENT_PORT=%s\n' "${configured_agent_port}"
    printf 'TEST_GUEST_API_PORT=%s\n' "${configured_guest_api_port}"
    printf 'TEST_KNOWLEDGE_PORT=%s\n' "${configured_knowledge_port}"
    printf 'TEST_FRONTEND_PORT=%s\n' "${configured_frontend_port}"
    printf 'TEST_AUDIT_FRONTEND_PORT=%s\n' "${configured_audit_frontend_port}"
    printf 'TEST_CSH_FRONTEND_PORT=%s\n' "${configured_csh_frontend_port}"
    printf 'TEST_FMCA_FRONTEND_PORT=%s\n' "${configured_fmca_frontend_port}"
    printf 'TEST_LANDSLIDE_FRONTEND_PORT=%s\n' "${configured_landslide_frontend_port}"
    printf 'TEST_GUEST_FRONTEND_PORT=%s\n' "${configured_guest_frontend_port}"
    printf 'GROUNDWATER_BUILTIN_ROOT=%q\n' "${GROUNDWATER_BUILTIN_ROOT}"
    printf 'NO_PROXY=%q\n' "${combined_no_proxy}"
    printf 'no_proxy=%q\n' "${combined_no_proxy}"
  } >>"${rendered}"
  chmod 600 "${rendered}"
  mv -f "${rendered}" "${RUNTIME_ENV}"
  chmod 600 "${RUNTIME_ENV}"

  printf 'FILES_PROXY_TARGET=%q\n' "${files_target}" >"${frontend_rendered}"
  chmod 600 "${frontend_rendered}"
  mv -f "${frontend_rendered}" "${FRONTEND_ENV}"
  chmod 600 "${FRONTEND_ENV}"
  log "已生成独立 Docker 测试配置（未输出任何凭据）"
}

seed_state() {
  local -a guard_args=()
  local port
  for port in "${STATE_GUARD_API_PORTS[@]}"; do
    guard_args+=(--guard-port "${port}")
  done
  python3 "${SCRIPT_DIR}/seed_state.py" \
    --source-checkpoints "${HOST_TEST_ROOT}/data/checkpoints" \
    --source-conversations "${HOST_TEST_ROOT}/data/conversations" \
    --target-checkpoints "${RUNTIME_ROOT}/data/checkpoints" \
    --target-conversations "${RUNTIME_ROOT}/data/conversations" \
    --target-state "${STATE_DIR}" \
    "${guard_args[@]}"
}

capture_protected() {
  local output="$1" port
  : >"${output}"
  for port in "${PROTECTED_PORTS[@]}"; do
    ss -H -ltnp "sport = :${port}" >>"${output}" || true
  done
  sort -o "${output}" "${output}"
  chmod 600 "${output}"
}

assert_protected_unchanged() {
  local before="$1" after="$2"
  capture_protected "${after}"
  if ! cmp --silent "${before}" "${after}"; then
    diff -u "${before}" "${after}" >&2 || true
    printf '[radai-docker-test] ERROR: 现有测试或生产监听端口/PID 发生变化\n' >&2
    return 1
  fi
  log "现有测试与生产监听端口/PID 保持不变"
}

capture_container_ids() {
  local environment_file="$1" output="$2"
  compose_with_env "${environment_file}" ps -q "${ALL_SERVICES[@]}" \
    | sort >"${output}"
  chmod 600 "${output}"
  [[ "$(wc -l <"${output}")" -eq "${#ALL_SERVICES[@]}" ]] \
    || fail "Docker 测试栈容器数量不完整"
}

assert_container_ids_unchanged() {
  local before="$1" after="$2"
  if ! cmp --silent "${before}" "${after}"; then
    diff -u "${before}" "${after}" >&2 || true
    printf '[radai-docker-test] ERROR: 无运行影响的提交触发了容器替换\n' >&2
    return 1
  fi
  log "无运行影响提交未替换任何 Docker 测试容器"
}

resolve_target_sha() {
  local target="${1:-refs/heads/${TARGET_BRANCH}}" resolved
  resolved="$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse --verify "${target}^{commit}")" \
    || fail "无法解析目标提交：${target}"
  [[ "${resolved}" =~ ^[0-9a-f]{40}$ ]] || fail "目标必须解析为完整 Git SHA"
  printf '%s\n' "${resolved}"
}

deployment_spec_sha() {
  local -a files=(
    "${SCRIPT_DIR}/backend.Dockerfile"
    "${SCRIPT_DIR}/backend.dockerignore"
    "${SCRIPT_DIR}/auto-update.sh"
    "${SCRIPT_DIR}/audit-frontend.Dockerfile"
    "${SCRIPT_DIR}/compose.audit.yml"
    "${SCRIPT_DIR}/compose.shadow.yml"
    "${SCRIPT_DIR}/cutover-host-test.sh"
    "${SCRIPT_DIR}/frontend.Dockerfile"
    "${SCRIPT_DIR}/install-user-units.sh"
    "${SCRIPT_DIR}/manage.sh"
    "${SCRIPT_DIR}/quality.Dockerfile"
    "${SCRIPT_DIR}/run_quality_gate.py"
    "${SCRIPT_DIR}/browser_smoke.js"
    "${SCRIPT_DIR}/seed_state.py"
    "${SCRIPT_DIR}/state_snapshot.py"
    "${GROUNDWATER_INSTALLER}"
    "${SCRIPT_DIR}/systemd/doc-agent-docker-test-auto-update.service"
    "${SCRIPT_DIR}/systemd/doc-agent-docker-test-auto-update.timer"
    "${SCRIPT_DIR}/nginx/app.conf.template"
    "${SCRIPT_DIR}/nginx/audit-entrypoint.sh"
    "${SCRIPT_DIR}/nginx/audit.conf.template"
    "${SCRIPT_DIR}/nginx/docker-entrypoint.sh"
    "${SCRIPT_DIR}/nginx/fmca.conf.template"
    "${SCRIPT_DIR}/nginx/guest.conf.template"
    "${SCRIPT_DIR}/nginx/nginx.conf"
    "${SCRIPT_DIR}/nginx/proxy-common.conf.template"
    "${REPO_ROOT}/deploy/local-test/changed_services.py"
    "${REPO_ROOT}/deploy/local-test/render_runtime_env.py"
    "${SCRIPT_DIR}/../local-test/smoke_test.py"
    "${QUALITY_POLICY}"
  )
  sha256sum "${files[@]}" | sha256sum | awk '{print $1}'
}

plan_has() {
  local expected="$1" item
  while IFS= read -r item; do
    [[ "${item}" == "${expected}" ]] && return 0
  done <"${IMPACT_PLAN}"
  return 1
}

write_initial_plan() {
  local service
  : >"${IMPACT_PLAN}"
  for service in "${ALL_SERVICES[@]}"; do
    printf '%s\n' "${service}" >>"${IMPACT_PLAN}"
  done
  chmod 600 "${IMPACT_PLAN}"
}

calculate_plan() {
  local target_sha="$1" target_mode="$2" old_sha=""
  local old_spec_sha="" old_mode="shadow"
  if [[ -f "${DEPLOYMENT_ENV}" ]]; then
    # deployment.env is generated owner-only state and contains no credentials.
    # shellcheck disable=SC1090
    source "${DEPLOYMENT_ENV}"
    old_sha="${ACTIVE_SOURCE_SHA:-}"
    old_spec_sha="${DEPLOYMENT_SPEC_SHA:-}"
    old_mode="${DEPLOYMENT_MODE:-shadow}"
  fi
  if [[ -z "${old_sha}" ]]; then
    write_initial_plan
    return
  fi
  if [[ "${old_sha}" != "${target_sha}" ]]; then
    git --git-dir="${SOURCE_GIT_DIR}" merge-base --is-ancestor "${old_sha}" "${target_sha}" \
      || fail "拒绝非快进 Docker 测试部署：${old_sha} -> ${target_sha}"
  fi
  if [[ "${old_mode}" != "${target_mode}" ]]; then
    log "Docker 测试部署模式变化：${old_mode} -> ${target_mode}；重新验证完整服务集合"
    write_initial_plan
    return
  fi
  if [[ "${old_sha}" == "${target_sha}" ]]; then
    if [[ "${old_spec_sha}" != "${CURRENT_DEPLOYMENT_SPEC_SHA}" ]]; then
      log "当前 SHA 的 Docker 部署规格发生漂移；重新验证完整服务集合"
      write_initial_plan
      return
    fi
    : >"${IMPACT_PLAN}"
    chmod 600 "${IMPACT_PLAN}"
    return
  fi
  python3 "${REPO_ROOT}/deploy/local-test/changed_services.py" \
    --git-dir "${SOURCE_GIT_DIR}" \
    --plan "${IMPACT_PLAN}" \
    --report "${IMPACT_REPORT}" \
    "${old_sha}" \
    "${target_sha}"
}

new_build_root() {
  mktemp -d /tmp/radai-docker-build.XXXXXXXX
}

cleanup_build_root() {
  local build_root="$1"
  [[ "${build_root}" == /tmp/radai-docker-build.* ]] \
    || fail "拒绝清理未验证的构建目录：${build_root}"
  [[ -d "${build_root}" ]] || return 0
  find "${build_root}" -mindepth 1 -delete
  rmdir -- "${build_root}"
}

materialize_backend_context() {
  local target_sha="$1" context="$2"
  mkdir -p "${context}"
  git --git-dir="${SOURCE_GIT_DIR}" archive "${target_sha}" \
    pyproject.toml uv.lock backend \
    | tar -x -C "${context}"
  cp "${SCRIPT_DIR}/backend.dockerignore" "${context}/.dockerignore"
}

materialize_frontend_context() {
  local target_sha="$1" context="$2"
  mkdir -p "${context}/.docker-build-assets"
  git --git-dir="${SOURCE_GIT_DIR}" archive "${target_sha}" \
    frontend frontend_audit frontend_csh_ai frontend_fmca frontend_landslide guest \
    | tar -x -C "${context}"
  cp -a "${SCRIPT_DIR}/nginx" "${context}/.docker-build-assets/nginx"
}

materialize_quality_context() (
  local target_sha="$1" context="$2"
  # The manager normally runs with umask 077.  The build root itself is
  # owner-only, but Git's executable bits must survive extraction so the
  # offline deployment-contract tests can execute versioned scripts.
  umask 022
  mkdir -p "${context}"
  git --git-dir="${SOURCE_GIT_DIR}" archive "${target_sha}" \
    | tar -x -C "${context}"
  printf '%s\n' "${target_sha}" >"${context}/.source-sha"
  chmod 600 "${context}/.source-sha"
)

build_backend_image() {
  local target_sha="$1" context="$2" image="radai/doc-agent-backend-test:${target_sha}"
  docker build \
    --file "${SCRIPT_DIR}/backend.Dockerfile" \
    --build-arg "SOURCE_SHA=${target_sha}" \
    --tag "${image}" \
    "${context}" 1>&2
  [[ "$(docker image inspect "${image}" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "${target_sha}" ]] \
    || fail "后端镜像 SHA 标签校验失败"
  docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --tmpfs /tmp:rw,exec,nosuid,nodev \
    "${image}" \
    sh -c 'command -v libreoffice >/dev/null && fc-match "Noto Sans CJK SC" | grep -qi "NotoSansCJK"' \
    || fail "后端镜像缺少 Word 渲染器或中文字体"
  printf '%s\n' "${image}"
}

build_frontend_image() {
  local target_sha="$1" context="$2" service="$3"
  local dockerfile="${SCRIPT_DIR}/frontend.Dockerfile"
  local frontend_dir frontend_kind image
  case "${service}" in
    frontend)
      frontend_dir=frontend
      frontend_kind=app
      image="radai/doc-agent-frontend-test:${target_sha}"
      ;;
    audit-frontend)
      dockerfile="${SCRIPT_DIR}/audit-frontend.Dockerfile"
      frontend_dir=frontend_audit
      frontend_kind=audit
      image="radai/doc-agent-audit-frontend-test:${target_sha}"
      ;;
    csh-frontend)
      frontend_dir=frontend_csh_ai
      frontend_kind=app
      image="radai/doc-agent-csh-frontend-test:${target_sha}"
      ;;
    fmca-frontend)
      frontend_dir=frontend_fmca
      frontend_kind=fmca
      image="radai/doc-agent-fmca-frontend-test:${target_sha}"
      ;;
    landslide-frontend)
      frontend_dir=frontend_landslide
      frontend_kind=app
      image="radai/doc-agent-landslide-frontend-test:${target_sha}"
      ;;
    guest-frontend)
      frontend_dir=guest
      frontend_kind=guest
      image="radai/doc-agent-guest-frontend-test:${target_sha}"
      ;;
    *)
      fail "未知前端服务：${service}"
      ;;
  esac
  docker build \
    --file "${dockerfile}" \
    --build-arg "FRONTEND_DIR=${frontend_dir}" \
    --build-arg "FRONTEND_KIND=${frontend_kind}" \
    --build-arg "SOURCE_SHA=${target_sha}" \
    --tag "${image}" \
    "${context}" 1>&2
  [[ "$(docker image inspect "${image}" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "${target_sha}" ]] \
    || fail "${service} 镜像 SHA 标签校验失败"
  printf '%s\n' "${image}"
}

build_quality_image() {
  local target_sha="$1" context="$2" base_backend_image="$3"
  local image="radai/doc-agent-quality-test:${target_sha}"
  docker build \
    --file "${SCRIPT_DIR}/quality.Dockerfile" \
    --build-arg "BASE_BACKEND_IMAGE=${base_backend_image}" \
    --build-arg "SOURCE_SHA=${target_sha}" \
    --tag "${image}" \
    "${context}" 1>&2
  [[ "$(docker image inspect "${image}" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "${target_sha}" ]] \
    || fail "质量门禁镜像 SHA 标签校验失败"
  printf '%s\n' "${image}"
}

write_candidate_env() {
  local candidate="$1" target_sha="$2"
  umask 077
  {
    printf 'ACTIVE_SOURCE_SHA=%s\n' "${target_sha}"
    printf 'DEPLOYMENT_SPEC_SHA=%s\n' "${CURRENT_DEPLOYMENT_SPEC_SHA}"
    printf 'DEPLOYMENT_MODE=%s\n' "${DEPLOYMENT_MODE}"
    printf 'AGENT_API_SHA=%s\n' "${AGENT_API_SHA}"
    printf 'KNOWLEDGE_API_SHA=%s\n' "${KNOWLEDGE_API_SHA}"
    printf 'GUEST_API_SHA=%s\n' "${GUEST_API_SHA}"
    printf 'FRONTEND_SHA=%s\n' "${FRONTEND_SHA}"
    printf 'AUDIT_FRONTEND_SHA=%s\n' "${AUDIT_FRONTEND_SHA}"
    printf 'CSH_FRONTEND_SHA=%s\n' "${CSH_FRONTEND_SHA}"
    printf 'FMCA_FRONTEND_SHA=%s\n' "${FMCA_FRONTEND_SHA}"
    printf 'LANDSLIDE_FRONTEND_SHA=%s\n' "${LANDSLIDE_FRONTEND_SHA}"
    printf 'GUEST_FRONTEND_SHA=%s\n' "${GUEST_FRONTEND_SHA}"
    printf 'AGENT_API_IMAGE=%s\n' "${AGENT_API_IMAGE}"
    printf 'KNOWLEDGE_API_IMAGE=%s\n' "${KNOWLEDGE_API_IMAGE}"
    printf 'GUEST_API_IMAGE=%s\n' "${GUEST_API_IMAGE}"
    printf 'FRONTEND_IMAGE=%s\n' "${FRONTEND_IMAGE}"
    printf 'AUDIT_FRONTEND_IMAGE=%s\n' "${AUDIT_FRONTEND_IMAGE}"
    printf 'CSH_FRONTEND_IMAGE=%s\n' "${CSH_FRONTEND_IMAGE}"
    printf 'FMCA_FRONTEND_IMAGE=%s\n' "${FMCA_FRONTEND_IMAGE}"
    printf 'LANDSLIDE_FRONTEND_IMAGE=%s\n' "${LANDSLIDE_FRONTEND_IMAGE}"
    printf 'GUEST_FRONTEND_IMAGE=%s\n' "${GUEST_FRONTEND_IMAGE}"
    printf 'RUNTIME_ROOT=%s\n' "${RUNTIME_ROOT}"
    printf 'RUNTIME_ENV=%s\n' "${RUNTIME_ENV}"
    printf 'FRONTEND_ENV=%s\n' "${FRONTEND_ENV}"
    printf 'HOST_UID=%s\n' "$(id -u)"
    printf 'HOST_GID=%s\n' "$(id -g)"
    printf 'GROUNDWATER_BUILTIN_ROOT=%s\n' "${GROUNDWATER_BUILTIN_ROOT}"
    printf 'BIND_ADDRESS=%s\n' "${BIND_ADDRESS}"
    printf 'TEST_AGENT_PORT=%s\n' "${TEST_AGENT_PORT}"
    printf 'TEST_GUEST_API_PORT=%s\n' "${TEST_GUEST_API_PORT}"
    printf 'TEST_KNOWLEDGE_PORT=%s\n' "${TEST_KNOWLEDGE_PORT}"
    printf 'TEST_FRONTEND_PORT=%s\n' "${TEST_FRONTEND_PORT}"
    printf 'TEST_AUDIT_FRONTEND_PORT=%s\n' "${TEST_AUDIT_FRONTEND_PORT}"
    printf 'TEST_CSH_FRONTEND_PORT=%s\n' "${TEST_CSH_FRONTEND_PORT}"
    printf 'TEST_FMCA_FRONTEND_PORT=%s\n' "${TEST_FMCA_FRONTEND_PORT}"
    printf 'TEST_LANDSLIDE_FRONTEND_PORT=%s\n' "${TEST_LANDSLIDE_FRONTEND_PORT}"
    printf 'TEST_GUEST_FRONTEND_PORT=%s\n' "${TEST_GUEST_FRONTEND_PORT}"
  } >"${candidate}"
  chmod 600 "${candidate}"
}

build_candidate() {
  local target_sha="$1" candidate="$2"
  local build_root backend_context frontend_context backend_image service image
  build_root="$(new_build_root)"
  backend_context="${build_root}/backend"
  frontend_context="${build_root}/frontend"
  trap 'cleanup_build_root "${build_root}"' RETURN ERR

  AGENT_API_SHA="${AGENT_API_SHA:-}"
  KNOWLEDGE_API_SHA="${KNOWLEDGE_API_SHA:-}"
  GUEST_API_SHA="${GUEST_API_SHA:-}"
  FRONTEND_SHA="${FRONTEND_SHA:-}"
  AUDIT_FRONTEND_SHA="${AUDIT_FRONTEND_SHA:-}"
  CSH_FRONTEND_SHA="${CSH_FRONTEND_SHA:-}"
  FMCA_FRONTEND_SHA="${FMCA_FRONTEND_SHA:-}"
  LANDSLIDE_FRONTEND_SHA="${LANDSLIDE_FRONTEND_SHA:-}"
  GUEST_FRONTEND_SHA="${GUEST_FRONTEND_SHA:-}"
  AGENT_API_IMAGE="${AGENT_API_IMAGE:-}"
  KNOWLEDGE_API_IMAGE="${KNOWLEDGE_API_IMAGE:-}"
  GUEST_API_IMAGE="${GUEST_API_IMAGE:-}"
  FRONTEND_IMAGE="${FRONTEND_IMAGE:-}"
  AUDIT_FRONTEND_IMAGE="${AUDIT_FRONTEND_IMAGE:-}"
  CSH_FRONTEND_IMAGE="${CSH_FRONTEND_IMAGE:-}"
  FMCA_FRONTEND_IMAGE="${FMCA_FRONTEND_IMAGE:-}"
  LANDSLIDE_FRONTEND_IMAGE="${LANDSLIDE_FRONTEND_IMAGE:-}"
  GUEST_FRONTEND_IMAGE="${GUEST_FRONTEND_IMAGE:-}"

  if plan_has agent-api || plan_has knowledge-api || plan_has guest-api; then
    log "从不可变 Git SHA 构建后端镜像：${target_sha}"
    materialize_backend_context "${target_sha}" "${backend_context}"
    backend_image="$(build_backend_image "${target_sha}" "${backend_context}")"
    if plan_has agent-api; then
      AGENT_API_SHA="${target_sha}"
      AGENT_API_IMAGE="${backend_image}"
    fi
    if plan_has knowledge-api; then
      KNOWLEDGE_API_SHA="${target_sha}"
      KNOWLEDGE_API_IMAGE="${backend_image}"
    fi
    if plan_has guest-api; then
      GUEST_API_SHA="${target_sha}"
      GUEST_API_IMAGE="${backend_image}"
    fi
  fi

  if plan_has frontend || plan_has audit-frontend || plan_has csh-frontend || plan_has fmca-frontend || plan_has landslide-frontend || plan_has guest-frontend; then
    materialize_frontend_context "${target_sha}" "${frontend_context}"
    for service in frontend audit-frontend csh-frontend fmca-frontend landslide-frontend guest-frontend; do
      plan_has "${service}" || continue
      log "构建 ${service} 镜像：${target_sha}"
      image="$(build_frontend_image "${target_sha}" "${frontend_context}" "${service}")"
      case "${service}" in
        frontend)
          FRONTEND_SHA="${target_sha}"
          FRONTEND_IMAGE="${image}"
          ;;
        audit-frontend)
          AUDIT_FRONTEND_SHA="${target_sha}"
          AUDIT_FRONTEND_IMAGE="${image}"
          ;;
        csh-frontend)
          CSH_FRONTEND_SHA="${target_sha}"
          CSH_FRONTEND_IMAGE="${image}"
          ;;
        fmca-frontend)
          FMCA_FRONTEND_SHA="${target_sha}"
          FMCA_FRONTEND_IMAGE="${image}"
          ;;
        landslide-frontend)
          LANDSLIDE_FRONTEND_SHA="${target_sha}"
          LANDSLIDE_FRONTEND_IMAGE="${image}"
          ;;
        guest-frontend)
          GUEST_FRONTEND_SHA="${target_sha}"
          GUEST_FRONTEND_IMAGE="${image}"
          ;;
      esac
    done
  fi

  local required_value
  for required_value in \
    "${AGENT_API_IMAGE}" \
    "${KNOWLEDGE_API_IMAGE}" \
    "${GUEST_API_IMAGE}" \
    "${FRONTEND_IMAGE}" \
    "${AUDIT_FRONTEND_IMAGE}" \
    "${CSH_FRONTEND_IMAGE}" \
    "${FMCA_FRONTEND_IMAGE}" \
    "${LANDSLIDE_FRONTEND_IMAGE}" \
    "${GUEST_FRONTEND_IMAGE}"; do
    [[ -n "${required_value}" ]] || fail "候选部署缺少服务镜像"
  done
  write_candidate_env "${candidate}" "${target_sha}"
  trap - RETURN ERR
  cleanup_build_root "${build_root}"
}

run_quality_gate() (
  local target_sha="$1" candidate="$2"
  local build_root context image output_dir report_path final_report run_status=0
  local transient_file service
  local -a affected_args=()

  # The generated deployment environment contains no credentials.
  # shellcheck disable=SC1090
  source "${candidate}"
  build_root="$(new_build_root)"
  context="${build_root}/quality"
  output_dir="$(mktemp -d "${REPORT_DIR}/.quality-output.XXXXXXXX")"
  chmod 700 "${output_dir}"
  trap 'cleanup_build_root "${build_root}"; if [[ -d "${output_dir}" ]]; then find "${output_dir}" -mindepth 1 -delete; rmdir -- "${output_dir}"; fi' EXIT

  materialize_quality_context "${target_sha}" "${context}"
  log "构建与候选后端依赖一致的离线全量测试镜像：${target_sha}"
  image="$(build_quality_image "${target_sha}" "${context}" "${AGENT_API_IMAGE}")"
  while IFS= read -r service; do
    [[ -n "${service}" ]] && affected_args+=(--affected-service "${service}")
  done <"${IMPACT_PLAN}"

  report_path="${output_dir}/quality.json"
  # 命名容器 + 客户端超时：挂死的测试不会留下无限期占用资源的无名
  # 门禁容器（daemon 子进程不随 systemd 单元超时退出）。
  docker rm -f "radai-quality-gate-${target_sha}" >/dev/null 2>&1 || true
  timeout 2400 docker run --rm \
    --name "radai-quality-gate-${target_sha}" \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=4g \
    --tmpfs /workspace/backend/data:rw,nosuid,nodev,noexec,mode=1777,size=128m \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 2048 \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=${output_dir},dst=/quality-output" \
    "${image}" \
    --source-sha "${target_sha}" \
    --output /quality-output/quality.json \
    "${affected_args[@]}" || run_status=$?
  [[ -f "${report_path}" ]] || fail "质量门禁未生成审计报告"
  final_report="${REPORT_DIR}/quality-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  mv -f "${report_path}" "${final_report}"
  chmod 600 "${final_report}"
  for transient_file in "${output_dir}/coverage.json" "${output_dir}/junit.xml"; do
    [[ -e "${transient_file}" ]] && unlink -- "${transient_file}"
  done
  if ((run_status != 0)); then
    log "质量门禁失败，报告：${final_report}"
    return "${run_status}"
  fi
  log "离线全量质量门禁通过，报告：${final_report}"
)

wait_readiness() {
  local environment_file="$1" deadline=$((SECONDS + 900))
  # The generated deployment environment contains no credentials.
  # shellcheck disable=SC1090
  source "${environment_file}"
  local -a urls=(
    "http://127.0.0.1:${TEST_AGENT_PORT}/health"
    "http://127.0.0.1:${TEST_GUEST_API_PORT}/health"
    "http://127.0.0.1:${TEST_KNOWLEDGE_PORT}/health"
  )
  local url all_ready
  while ((SECONDS < deadline)); do
    all_ready=true
    for url in "${urls[@]}"; do
      if ! curl --fail --silent --show-error --max-time 5 "${url}" >/dev/null 2>&1; then
        all_ready=false
      fi
    done
    if [[ "${all_ready}" == true ]]; then
      log "三个 API 就绪检查通过"
      return
    fi
    sleep 5
  done
  printf '[radai-docker-test] ERROR: Docker 测试 API 在 900 秒内未全部就绪\n' >&2
  return 1
}

run_business_smoke() {
  local candidate="$1" python_bin="${HOST_TEST_ROOT}/current/.venv/bin/python"
  [[ -x "${python_bin}" ]] || fail "缺少宿主机测试 Python：${python_bin}"
  # shellcheck disable=SC1090
  source "${candidate}"
  set -a
  # shellcheck disable=SC1090
  source "${RUNTIME_ENV}"
  set +a
  export DOC_AGENT_TEST_RELEASE_SHA="${ACTIVE_SOURCE_SHA}"
  export DOC_AGENT_TEST_RUNTIME_ROOT="${RUNTIME_ROOT}"
  export DOC_AGENT_TEST_REPORT_DIR="${REPORT_DIR}"
  "${python_bin}" "${REPO_ROOT}/deploy/local-test/smoke_test.py"
}

run_browser_smoke() (
  local candidate="$1" output_dir report_path final_report run_status=0
  # The generated deployment environment contains no credentials.
  # shellcheck disable=SC1090
  source "${candidate}"
  output_dir="$(mktemp -d "${REPORT_DIR}/.browser-output.XXXXXXXX")"
  chmod 700 "${output_dir}"
  trap 'if [[ -d "${output_dir}" ]]; then find "${output_dir}" -mindepth 1 -delete; rmdir -- "${output_dir}"; fi' EXIT
  report_path="${output_dir}/browser.json"

  docker run --rm \
    --network host \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp \
    --env NODE_PATH=/usr/src/app/node_modules \
    --mount "type=bind,src=${SCRIPT_DIR}/browser_smoke.js,dst=/radai/browser_smoke.js,readonly" \
    --mount "type=bind,src=${output_dir},dst=/browser-output" \
    --entrypoint node \
    "${BROWSER_SMOKE_IMAGE}" \
    /radai/browser_smoke.js \
    --git-sha "${ACTIVE_SOURCE_SHA}" \
    --output /browser-output/browser.json \
    --target "frontend=http://127.0.0.1:${TEST_FRONTEND_PORT}" \
    --target "audit-frontend=http://127.0.0.1:${TEST_AUDIT_FRONTEND_PORT}" \
    --target "csh-frontend=http://127.0.0.1:${TEST_CSH_FRONTEND_PORT}" \
    --target "fmca-frontend=http://127.0.0.1:${TEST_FMCA_FRONTEND_PORT}" \
    --target "landslide-frontend=http://127.0.0.1:${TEST_LANDSLIDE_FRONTEND_PORT}" \
    --target "guest-frontend=http://127.0.0.1:${TEST_GUEST_FRONTEND_PORT}" \
    || run_status=$?
  [[ -f "${report_path}" ]] || fail "真实浏览器冒烟未生成审计报告"
  final_report="${REPORT_DIR}/browser-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  mv -f "${report_path}" "${final_report}"
  chmod 600 "${final_report}"
  if ((run_status != 0)); then
    log "真实浏览器冒烟失败，报告：${final_report}"
    return "${run_status}"
  fi
  log "六套前端真实浏览器冒烟通过，报告：${final_report}"
)

run_full_smoke() {
  local candidate="$1"
  run_business_smoke "${candidate}"
  run_browser_smoke "${candidate}"
}

capture_candidate_logs() {
  local candidate="$1" phase="$2"
  local timestamp output temporary
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  output="${REPORT_DIR}/failed-${phase}-${timestamp}.log"
  temporary="${output}.tmp.$$"
  umask 077
  compose_with_env "${candidate}" logs --no-color --tail 500 >"${temporary}" 2>&1 || true
  chmod 600 "${temporary}"
  mv -f "${temporary}" "${output}"
  log "已保存候选服务失败日志：${output}"
}

rollback_candidate() {
  local candidate="$1" previous="$2"
  shift 2
  local -a services=("$@")
  local -a restore_services=()
  local service previous_has_audit=0 introduced_audit=0
  if [[ -f "${previous}" ]]; then
    if awk -F= '$1 == "AUDIT_FRONTEND_IMAGE" && length($2) > 0 {found=1} END {exit !found}' \
      "${previous}"; then
      previous_has_audit=1
    fi
    for service in "${services[@]}"; do
      if [[ "${service}" == "audit-frontend" && "${previous_has_audit}" == "0" ]]; then
        introduced_audit=1
      else
        restore_services+=("${service}")
      fi
    done
    if [[ "${introduced_audit}" == "1" ]]; then
      log "移除本次首次创建但未通过门禁的审计前端容器"
      compose_with_env "${candidate}" rm -s -f audit-frontend \
        || log "警告：移除候选审计前端容器失败，请检查 Docker 服务"
    fi
    if ((${#restore_services[@]} == 0)); then
      log "候选部署未替换既有 Docker 测试服务，无需恢复"
      return
    fi
    log "候选部署失败，恢复上一个 Docker 测试服务镜像"
    compose_with_env "${previous}" up -d --no-deps --wait --wait-timeout 900 "${restore_services[@]}" \
      || log "警告：自动恢复未完全成功，请检查 Docker 服务"
  else
    log "首次候选部署失败，停止新建的 Docker 测试栈"
    compose_with_env "${candidate}" down --remove-orphans \
      || log "警告：停止候选栈失败，请检查 Docker 服务"
  fi
}

advance_unaffected_target() {
  local target_sha="$1" requested_mode="$2"
  local candidate before after containers_before containers_after transient_file
  [[ -f "${DEPLOYMENT_ENV}" ]] || fail "无运行影响部署缺少当前状态"
  # deployment.env is generated owner-only state and contains no credentials.
  # shellcheck disable=SC1090
  source "${DEPLOYMENT_ENV}"
  configure_deployment_mode "${requested_mode}"
  candidate="${STATE_DIR}/deployment.candidate.$$"
  before="${STATE_DIR}/protected.before.$$"
  after="${STATE_DIR}/protected.after.$$"
  containers_before="${STATE_DIR}/containers.before.$$"
  containers_after="${STATE_DIR}/containers.after.$$"

  build_candidate "${target_sha}" "${candidate}"
  compose_with_env "${candidate}" config --quiet
  run_quality_gate "${target_sha}" "${candidate}" \
    || fail "无运行影响候选的离线全量质量门禁失败"
  capture_protected "${before}"
  capture_container_ids "${DEPLOYMENT_ENV}" "${containers_before}"
  if ! wait_readiness "${candidate}"; then
    fail "无运行影响候选的 API 就绪验证失败"
  fi
  if ! run_full_smoke "${candidate}"; then
    fail "无运行影响候选的业务冒烟失败"
  fi
  assert_protected_unchanged "${before}" "${after}" \
    || fail "无运行影响候选改变了受保护服务"
  capture_container_ids "${DEPLOYMENT_ENV}" "${containers_after}"
  assert_container_ids_unchanged "${containers_before}" "${containers_after}" \
    || fail "无运行影响候选错误替换了容器"

  mv -f "${candidate}" "${DEPLOYMENT_ENV}"
  chmod 600 "${DEPLOYMENT_ENV}"
  for transient_file in \
    "${before}" "${after}" "${containers_before}" "${containers_after}"; do
    [[ -e "${transient_file}" ]] && unlink -- "${transient_file}"
  done
  log "提交 ${target_sha} 不影响运行服务；已完成回归且未构建、未重启"
}

deploy_target() {
  # 中途失败（门禁/readiness/smoke/隔离验证）不留 candidate.$$/previous.$$
  # 等临时状态文件；成功路径在尾部显式 unlink。
  trap 'rm -f -- "${STATE_DIR}/deployment.candidate.$$" "${STATE_DIR}/deployment.previous.$$" "${STATE_DIR}/protected.before.$$" "${STATE_DIR}/protected.after.$$"' EXIT
  local requested="${1:-refs/heads/${TARGET_BRANCH}}"
  local requested_mode="${2:-}" target_sha candidate previous before after
  local -a services=()
  if [[ -z "${requested_mode}" ]]; then
    requested_mode="$(active_deployment_mode)"
  fi
  configure_deployment_mode "${requested_mode}"
  target_sha="$(resolve_target_sha "${requested}")"
  CURRENT_DEPLOYMENT_SPEC_SHA="$(deployment_spec_sha)"
  init_directories
  calculate_plan "${target_sha}" "${requested_mode}"
  configure_deployment_mode "${requested_mode}"
  mapfile -t services <"${IMPACT_PLAN}"
  if ((${#services[@]} == 0)); then
    local active_sha=""
    if [[ -f "${DEPLOYMENT_ENV}" ]]; then
      active_sha="$(awk -F= '$1 == "ACTIVE_SOURCE_SHA" {print $2}' "${DEPLOYMENT_ENV}")"
    fi
    if [[ "${active_sha}" == "${target_sha}" ]]; then
      log "Git SHA 与 Docker 部署规格均未变化；不构建、不重启、不部署"
      return
    fi
    advance_unaffected_target "${target_sha}" "${requested_mode}"
    return
  fi
  log "本次受影响服务：${services[*]}"

  render_runtime_config
  seed_state
  candidate="${STATE_DIR}/deployment.candidate.$$"
  previous="${STATE_DIR}/deployment.previous.$$"
  before="${STATE_DIR}/protected.before.$$"
  after="${STATE_DIR}/protected.after.$$"
  if [[ -f "${DEPLOYMENT_ENV}" ]]; then
    cp "${DEPLOYMENT_ENV}" "${previous}"
    chmod 600 "${previous}"
    # shellcheck disable=SC1090
    source "${DEPLOYMENT_ENV}"
  fi
  configure_deployment_mode "${requested_mode}"
  capture_protected "${before}"
  build_candidate "${target_sha}" "${candidate}"
  compose_with_env "${candidate}" config --quiet
  run_quality_gate "${target_sha}" "${candidate}" \
    || fail "候选部署的离线全量质量门禁失败；尚未替换任何测试容器"

  if ! compose_with_env "${candidate}" up -d --no-deps --wait --wait-timeout 900 "${services[@]}"; then
    capture_candidate_logs "${candidate}" compose-start
    rollback_candidate "${candidate}" "${previous}" "${services[@]}"
    fail "Docker Compose 候选服务启动失败"
  fi
  if ! wait_readiness "${candidate}"; then
    capture_candidate_logs "${candidate}" readiness
    rollback_candidate "${candidate}" "${previous}" "${services[@]}"
    fail "Docker API 就绪验证失败"
  fi
  if ! run_full_smoke "${candidate}"; then
    capture_candidate_logs "${candidate}" business-smoke
    rollback_candidate "${candidate}" "${previous}" "${services[@]}"
    fail "Docker 业务冒烟失败"
  fi
  if ! assert_protected_unchanged "${before}" "${after}"; then
    capture_candidate_logs "${candidate}" isolation
    rollback_candidate "${candidate}" "${previous}" "${services[@]}"
    fail "现有服务隔离验证失败"
  fi

  mv -f "${candidate}" "${DEPLOYMENT_ENV}"
  chmod 600 "${DEPLOYMENT_ENV}"
  local transient_file
  for transient_file in "${previous}" "${before}" "${after}"; do
    [[ -e "${transient_file}" ]] && unlink -- "${transient_file}"
  done
  log "Docker 测试候选部署完成：${target_sha}"
}

start_current() {
  local before="${STATE_DIR}/protected.start.before.$$"
  local after="${STATE_DIR}/protected.start.after.$$"
  local mode
  [[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态尚未生成"
  mode="$(active_deployment_mode)"
  # The generated deployment environment contains no credentials.
  # shellcheck disable=SC1090
  source "${DEPLOYMENT_ENV}"
  configure_deployment_mode "${mode}"
  render_runtime_config
  capture_protected "${before}"
  if ! compose_with_env "${DEPLOYMENT_ENV}" up -d --wait --wait-timeout 900; then
    capture_candidate_logs "${DEPLOYMENT_ENV}" current-start
    return 1
  fi
  wait_readiness "${DEPLOYMENT_ENV}"
  run_full_smoke "${DEPLOYMENT_ENV}"
  assert_protected_unchanged "${before}" "${after}"
  local transient_file
  for transient_file in "${before}" "${after}"; do
    [[ -e "${transient_file}" ]] && unlink -- "${transient_file}"
  done
  log "Docker 测试栈已按 ${mode} 模式启动"
}

sync_remote() {
  log "检查 origin/${TARGET_BRANCH} 是否有新提交"
  GIT_TERMINAL_PROMPT=0 git --git-dir="${SOURCE_GIT_DIR}" fetch \
    --prune \
    origin \
    "+refs/heads/${TARGET_BRANCH}:refs/heads/${TARGET_BRANCH}"
  deploy_target "refs/heads/${TARGET_BRANCH}"
}

status() {
  [[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态尚未生成"
  compose_with_env "${DEPLOYMENT_ENV}" ps
  awk -F= '/^(DEPLOYMENT_MODE|BIND_ADDRESS|TEST_(AGENT|GUEST_API|KNOWLEDGE|FRONTEND|AUDIT_FRONTEND|CSH_FRONTEND|FMCA_FRONTEND|LANDSLIDE_FRONTEND|GUEST_FRONTEND)_PORT)=/ {print "  " $1 "=" $2}' \
    "${DEPLOYMENT_ENV}"
  printf 'service_shas:\n'
  awk -F= '/^(ACTIVE_SOURCE|DEPLOYMENT_SPEC|AGENT_API|KNOWLEDGE_API|GUEST_API|FRONTEND|AUDIT_FRONTEND|CSH_FRONTEND|FMCA_FRONTEND|LANDSLIDE_FRONTEND|GUEST_FRONTEND)_SHA=/ {print "  " $1 "=" $2}' \
    "${DEPLOYMENT_ENV}"
}

usage() {
  cat <<'EOF'
用法：deploy/docker-test/manage.sh COMMAND [ARG]

命令：
  deploy [FULL_SHA] [shadow|primary]
                     从不可变 dev SHA 构建并增量部署；默认保留当前模式
  sync               获取 origin/dev；无新提交时不构建、不重启
  smoke              对当前 Docker 测试栈执行完整业务冒烟
  status             查看容器与每个服务使用的 Git SHA
  logs [SERVICE]     查看当前测试栈日志
  up                 按当前已验证部署状态启动并运行完整冒烟
  down               仅停止 Docker 影子测试栈
  config             只验证当前 Compose 配置
EOF
}

main() {
  need docker
  need git
  need tar
  need python3
  need curl
  need ss
  need sha256sum
  assert_runtime_layout
  # 状态变更类命令与 5 分钟一次的 auto-update 互斥（同一把锁）：否则
  # 双方各写 candidate.$$ 后 mv -f 互相覆盖 deployment.env，且
  # assert_protected_unchanged 会捕捉到对方重启的容器误判隔离失败。
  # auto-update 持锁后以 fd 9 调用本脚本：flock 锁属于 open file
  # description，继承的 fd 指向同一锁文件时不自我冲突，直接复用；
  # 若重新 exec 打开会新建描述符，与父进程死锁（4c7a74e 部署失败根因）。
  # 手工调用时 fd 9 通常未打开，readlink 结果不匹配，正常抢锁。
  case "${1:-}" in
    deploy|sync|up|down|smoke|rollback)
      inherited_lock="$(readlink "/proc/${BASHPID}/fd/9" 2>/dev/null || true)"
      expected_lock="$(readlink -f "${STATE_DIR}/auto-update.lock" 2>/dev/null \
        || printf '%s' "${STATE_DIR}/auto-update.lock")"
      if [[ "${inherited_lock}" != "${expected_lock}" ]]; then
        mkdir -p "${STATE_DIR}"
        chmod 700 "${STATE_DIR}" 2>/dev/null || true
        exec 9>"${STATE_DIR}/auto-update.lock"
        if ! flock -n 9; then
          fail "另一部署/自动更新进程持有锁（${STATE_DIR}/auto-update.lock），请稍后重试"
        fi
      fi
      ;;
  esac
  case "${1:-}" in
    deploy)
      deploy_target "${2:-refs/heads/${TARGET_BRANCH}}" "${3:-}"
      ;;
    sync)
      sync_remote
      ;;
    smoke)
      [[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态尚未生成"
      wait_readiness "${DEPLOYMENT_ENV}"
      run_full_smoke "${DEPLOYMENT_ENV}"
      ;;
    status)
      status
      ;;
    logs)
      [[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态尚未生成"
      if [[ -n "${2:-}" ]]; then
        compose_with_env "${DEPLOYMENT_ENV}" logs --tail 200 "${2}"
      else
        compose_with_env "${DEPLOYMENT_ENV}" logs --tail 80
      fi
      ;;
    up)
      start_current
      ;;
    down)
      [[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态尚未生成"
      compose_with_env "${DEPLOYMENT_ENV}" down --remove-orphans
      ;;
    config)
      [[ -f "${DEPLOYMENT_ENV}" ]] || fail "Docker 测试部署状态尚未生成"
      compose_with_env "${DEPLOYMENT_ENV}" config --quiet
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
